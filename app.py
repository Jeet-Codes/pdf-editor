"""Local PDF Editor server. Runs only on this computer; files never leave it."""
from __future__ import annotations

import glob
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
import zipfile
import webbrowser

import fitz
from flask import Flask, abort, jsonify, request, send_file, send_from_directory

from engine import Document, EditError, PasswordRequired

PORT = int(os.environ.get("PDF_EDITOR_PORT", 8765))
# The Docker image listens on 0.0.0.0 inside the container; run it with -p 127.0.0.1:8765:8765.
HOST = os.environ.get("PDF_EDITOR_HOST", "127.0.0.1")
ALLOWED_HOSTS = {"127.0.0.1", "localhost"} | {
    h.strip().lower() for h in os.environ.get("PDF_EDITOR_ALLOWED_HOSTS", "").split(",") if h.strip()}
HERE = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024  # 1 GB
lock = threading.RLock()  # the PDF library is not thread-safe
docs: dict[str, Document] = {}  # every open tab
# Kept outside OneDrive on purpose, so private documents are not synced to the cloud.
LIBRARY_DIR = (os.path.join(os.environ["PDF_EDITOR_DATA"], "recent") if os.environ.get("PDF_EDITOR_DATA")
               else os.path.join(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(), "PDF Editor", "recent"))
RECENT_LIMIT = 12  # PDFs the user chose to save are never removed automatically


# ------------------------------------------------------------------ library
def write_meta(item_id, meta):
    tmp = os.path.join(LIBRARY_DIR, item_id + ".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f)
    os.replace(tmp, os.path.join(LIBRARY_DIR, item_id + ".json"))


def remember(doc: Document):
    """Keep the latest state of an edited PDF so it can be continued later."""
    os.makedirs(LIBRARY_DIR, exist_ok=True)
    doc.recent_id = doc.recent_id or uuid.uuid4().hex
    path = os.path.join(LIBRARY_DIR, doc.recent_id + ".pdf")
    doc.doc.save(path + ".tmp", garbage=0, deflate=True)
    os.replace(path + ".tmp", path)
    write_meta(doc.recent_id, {"name": doc.name, "time": time.time(),
                               "pages": doc.doc.page_count, "saved": doc.saved, "unlocked": doc.unlocked})
    open_ids = {d.recent_id for d in docs.values()}
    extra = [i for i in library_items() if not i.get("saved") and i["id"] not in open_ids]
    for old in extra[RECENT_LIMIT:]:
        forget(old["id"])


def library_items():
    items = []
    if os.path.isdir(LIBRARY_DIR):
        for entry in os.listdir(LIBRARY_DIR):
            if entry.endswith(".json") and os.path.exists(os.path.join(LIBRARY_DIR, entry[:-5] + ".pdf")):
                try:
                    with open(os.path.join(LIBRARY_DIR, entry), encoding="utf-8") as f:
                        items.append({"saved": False} | json.load(f) | {"id": entry[:-5]})
                except (OSError, ValueError):
                    continue
    return sorted(items, key=lambda i: i.get("time", 0), reverse=True)


def forget(item_id):
    for ext in (".pdf", ".json"):
        try:
            os.remove(os.path.join(LIBRARY_DIR, item_id + ext))
        except OSError:
            pass


def library_item(item_id):
    if not re.fullmatch(r"[0-9a-f]{32}", item_id):
        raise EditError("That PDF could not be found.")
    try:
        with open(os.path.join(LIBRARY_DIR, item_id + ".json"), encoding="utf-8") as f:
            meta = {"saved": False} | json.load(f)
    except (OSError, ValueError) as exc:
        raise EditError("That PDF could not be found.") from exc
    path = os.path.join(LIBRARY_DIR, item_id + ".pdf")
    if not os.path.exists(path):
        raise EditError("That PDF could not be found.")
    return meta, path


def open_tab_for(item_id):
    return next((tab for tab, d in docs.items() if d.recent_id == item_id), None)


def download_name(name):
    stem = os.path.splitext(name)[0]
    return f"{stem}.pdf" if stem.endswith("_edited") else f"{stem}_edited.pdf"


# ---------------------------------------------------------------- requests
@app.before_request
def only_local():
    # Refuse pages from other websites that try to reach this server via a fake hostname.
    host = request.host.rsplit(":", 1)[0].strip("[]").lower()
    if host not in ALLOWED_HOSTS:
        abort(403)


@app.errorhandler(EditError)
def on_edit_error(err):
    return jsonify(error=str(err)), 400


@app.errorhandler(Exception)
def on_error(err):
    if hasattr(err, "code") and isinstance(err.code, int):
        return jsonify(error=str(err)), err.code
    app.logger.exception(err)
    return jsonify(error=f"That edit could not be applied, so the PDF was left unchanged. ({err})"), 500


def need_doc() -> Document:
    doc = docs.get(request.args.get("doc", ""))
    if doc is None:
        raise EditError("That tab is no longer open.")
    return doc


def describe(tab_id, doc):
    return doc.info() | {"id": tab_id, "saved": doc.saved, "warnings": sorted(set(doc.warnings))}


def state():
    doc = need_doc()
    return jsonify(describe(request.args["doc"], doc))


def add_tab(doc):
    tab_id = uuid.uuid4().hex
    docs[tab_id] = doc
    return jsonify(describe(tab_id, doc))


@app.get("/")
def index():
    return send_from_directory(os.path.join(HERE, "static"), "index.html")


@app.get("/static/<path:name>")
def static_file(name):
    resp = send_from_directory(os.path.join(HERE, "static"), name)
    resp.headers["Cache-Control"] = "no-cache"  # always pick up a new version on refresh
    return resp


@app.get("/api/tabs")
def list_tabs():
    with lock:
        return jsonify(tabs=[describe(t, d) for t, d in docs.items()])


@app.post("/api/open")
def open_pdf():
    upload = request.files.get("file")
    if not upload:
        raise EditError("No file received.")
    data = upload.read()
    with lock:
        try:
            doc = Document(data, upload.filename or "document.pdf", request.form.get("password"))
        except PasswordRequired:
            return jsonify(password=True, wrong=bool(request.form.get("password"))), 401
        except EditError:
            raise
        except Exception as exc:
            raise EditError(f"{upload.filename} could not be read as a PDF.") from exc
        return add_tab(doc)


@app.post("/api/close")
def close_tab():
    with lock:
        need_doc()
        docs.pop(request.args["doc"]).close()
        return jsonify(ok=True)


@app.post("/api/save")
def save_to_library():
    with lock:
        doc = need_doc()
        doc.saved = True
        remember(doc)
        return state()


# ---- saved and recent PDFs
@app.get("/api/library")
def list_library():
    with lock:
        return jsonify(items=[i | {"openTab": open_tab_for(i["id"])} for i in library_items()])


@app.post("/api/library/<item_id>/open")
def open_library_item(item_id):
    with lock:
        meta, path = library_item(item_id)
        tab = open_tab_for(item_id)
        if tab:
            return jsonify(describe(tab, docs[tab]))
        with open(path, "rb") as f:
            doc = Document(f.read(), meta["name"])
        doc.recent_id = item_id
        doc.saved = bool(meta.get("saved"))
        doc.unlocked = meta.get("unlocked")  # the stored copy is already unlocked; remember that it was locked
        return add_tab(doc)


@app.post("/api/library/<item_id>/rename")
def rename_library_item(item_id):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", str(request.get_json(force=True).get("name", ""))).strip()[:200]
    if not os.path.splitext(name)[0]:
        raise EditError("Please enter a name.")
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    with lock:
        meta, _ = library_item(item_id)
        write_meta(item_id, meta | {"name": name})
        tab = open_tab_for(item_id)
        if tab:
            docs[tab].name = name
        return jsonify(name=name, openTab=tab)


@app.delete("/api/library/<item_id>")
def delete_library_item(item_id):
    with lock:
        library_item(item_id)
        tab = open_tab_for(item_id)
        if tab:
            docs.pop(tab).close()
        forget(item_id)
        return jsonify(closedTab=tab)


@app.get("/api/library/<item_id>/thumb.png")
def library_thumb(item_id):
    with lock:
        _, path = library_item(item_id)
        with fitz.open(path) as doc:
            page = doc[0]
            scale = 320 / max(page.rect.width, 1)
            png = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).tobytes("png")
    return send_file(io.BytesIO(png), mimetype="image/png")


@app.get("/api/library/<item_id>/download")
def library_download(item_id):
    with lock:
        meta, path = library_item(item_id)
        tab = open_tab_for(item_id)
        if tab:
            data = docs[tab].export()
        else:
            with fitz.open(path) as doc:
                data = doc.tobytes(garbage=3, deflate=True, deflate_fonts=True, deflate_images=False)
    return send_file(io.BytesIO(data), mimetype="application/pdf",
                     as_attachment=True, download_name=download_name(meta["name"]))


# ---- editing one tab
@app.get("/api/page/<int:index>.png")
def page_png(index):
    zoom = float(request.args.get("z", 1.5))
    with lock:
        png = need_doc().render(index, zoom)
    resp = send_file(io.BytesIO(png), mimetype="image/png")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/page/<int:index>/layout")
def page_layout(index):
    with lock:
        return jsonify(need_doc().public_layout(index))


@app.post("/api/op")
def operation():
    b = request.get_json(force=True)
    kind, page = b.get("type"), b.get("page")
    with lock:
        d = need_doc()
        if kind == "replace_text":
            d.replace_text(page, b["id"], b["runs"], b.get("align"), b.get("dx", 0), b.get("dy", 0))
        elif kind == "add_text":
            d.add_text(page, b["x"], b["y"], b.get("width"), b["runs"], b.get("align", "left"))
        elif kind == "move_image":
            d.move_image(page, b["id"], b["rect"])
        elif kind == "delete_image":
            d.delete_image(page, b["id"])
        elif kind == "erase":
            d.erase(page, b["rect"])
        elif kind == "area":
            d.transfer_area(page, b["rect"], b["toPage"], b["x"], b["y"], bool(b.get("move")))
        elif kind == "set_form":
            d.set_form(page, b["xref"], b["value"])
        elif kind == "rotate_page":
            d.rotate_page(page, b.get("degrees", 90))
        elif kind == "delete_page":
            d.delete_page(page)
        elif kind == "insert_page":
            d.insert_page(page)
        elif kind == "replace":
            n = d.replace(str(b.get("query", "")), str(b.get("replacement", "")),
                          bool(b.get("matchCase")), b.get("target"))
            d.warnings.append(f"Replaced {n} match{'' if n == 1 else 'es'}.")
        elif kind == "arrange":
            d.arrange(b["order"])
        elif kind == "rotate_pages":
            d.rotate_pages(b["pages"], b.get("degrees", 90))
        elif kind == "ocr":
            found = d.ocr(page)
            if not found:
                d.warnings.append("No text was found on this page.")
        elif kind == "undo":
            d.undo()
        elif kind == "redo":
            d.redo()
        else:
            raise EditError("Unknown action.")
        remember(d)
        return state()


@app.post("/api/image")
def image_op():
    upload = request.files.get("file")
    if not upload:
        raise EditError("No image received.")
    data = upload.read()
    page = int(request.form["page"])
    with lock:
        d = need_doc()
        if "id" in request.form:
            d.replace_image(page, int(request.form["id"]), data)
        else:
            rect = [float(request.form[k]) for k in ("x0", "y0", "x1", "y1")]
            d.add_image(page, rect, data)
        remember(d)
        return state()


@app.post("/api/find")
def find_text():
    b = request.get_json(force=True)
    with lock:
        return jsonify(hits=need_doc().find(str(b.get("q", ""))[:500], bool(b.get("case"))))


@app.post("/api/merge")
def merge_pdf():
    upload = request.files.get("file")
    if not upload:
        raise EditError("No file received.")
    data, name = upload.read(), upload.filename or "That file"
    with lock:
        d = need_doc()
        try:
            n = d.merge(data, request.form.get("at", d.doc.page_count), request.form.get("password"), name)
        except PasswordRequired:
            return jsonify(password=True, wrong=bool(request.form.get("password"))), 401
        d.warnings.append(f"Added {n} page{'' if n == 1 else 's'} from {name}.")
        remember(d)
        return state()


def pages_label(pages):
    parts, start = [], None
    for i, n in enumerate(pages):
        start = n if start is None else start
        if i + 1 == len(pages) or pages[i + 1] != n + 1:
            parts.append(f"{start + 1}" if start == n else f"{start + 1}-{n + 1}")
            start = None
    label = "_".join(parts)
    return label if len(label) <= 40 else f"{len(pages)}_pages"


@app.get("/api/export")
def export():
    compress = request.args.get("compress") == "1"
    split = request.args.get("split") == "1"
    try:
        pages = [int(x) for x in request.args["pages"].split(",")] if request.args.get("pages") else None
    except ValueError as exc:
        raise EditError("Those pages no longer exist.") from exc
    with lock:
        doc = need_doc()
        stem = os.path.splitext(doc.name)[0]
        if split:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for i in pages if pages is not None else range(doc.doc.page_count):
                    z.writestr(f"{stem}_page_{i + 1}.pdf", doc.export(compress, [i]))
            buf.seek(0)
            return send_file(buf, mimetype="application/zip", as_attachment=True,
                             download_name=f"{stem}_pages.zip")
        data = doc.export(compress, pages)
        if pages is None:
            remember(doc)
            name = download_name(doc.name)
        else:
            name = f"{stem}_pages_{pages_label(pages)}.pdf"
    return send_file(io.BytesIO(data), mimetype="application/pdf", as_attachment=True, download_name=name)


def already_running(port):
    import socket
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


if __name__ == "__main__":
    url = f"http://127.0.0.1:{PORT}/"
    if already_running(PORT):
        # A second copy would clear the first one's undo files and share its port, so just show the first.
        print(f"PDF Editor is already running at {url}")
        if "--no-browser" not in sys.argv:
            webbrowser.open(url)
        sys.exit(0)
    # Undo snapshots from an earlier run stay behind when its window is simply closed.
    for leftover in glob.glob(os.path.join(tempfile.gettempdir(), "pdf_editor_*")):
        shutil.rmtree(leftover, ignore_errors=True)
    if "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print(f"PDF Editor is running at {url}\nClose this window to stop it.")
    app.run(host=HOST, port=PORT, threaded=True, debug=False)
