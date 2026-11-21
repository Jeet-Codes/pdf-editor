"""End-to-end checks of the engine on real files. Usage: test_engine.py SAMPLE_DIR [extra.pdf ...]"""
import os
import sys
import traceback

import fitz

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from engine import Document, EditError, PasswordRequired  # noqa: E402

DIR = sys.argv[1]
EXTRA = sys.argv[2:]
results = []


def load(name, pw=None):
    path = name if os.path.isabs(name) else f"{DIR}/{name}"
    return Document(open(path, "rb").read(), os.path.basename(path), pw)


def reread(d):
    return fitz.open(stream=d.export(), filetype="pdf")


def para_with(d, page, needle):
    for p in d.layout(page)["paras"]:
        if needle in "".join(r["text"] for r in p["runs"]):
            return p
    raise AssertionError(f"no paragraph containing {needle!r}: "
                         f"{[''.join(r['text'] for r in p['runs'])[:40] for p in d.layout(page)['paras']]}")


def retext(p, old, new):
    runs = [dict(r) for r in p["runs"]]
    for r in runs:
        if old in r["text"]:
            r["text"] = r["text"].replace(old, new)
            return runs
    raise AssertionError(f"{old!r} not in a single run")


def diff_outside(before_png, after_png, rect, zoom=1.0):
    """Fraction of pixels changed outside rect (rect in page points)."""
    a, b = fitz.Pixmap(before_png), fitz.Pixmap(after_png)
    assert (a.width, a.height) == (b.width, b.height), "page size changed"
    r = fitz.Rect(rect) * zoom
    changed = 0
    sa, sb, n, w = a.samples, b.samples, a.n, a.width
    for y in range(a.height):
        for x in range(w):
            if r.x0 <= x <= r.x1 and r.y0 <= y <= r.y1:
                continue
            i = (y * w + x) * n
            if abs(sa[i] - sb[i]) + abs(sa[i + 1] - sb[i + 1]) + abs(sa[i + 2] - sb[i + 2]) > 40:
                changed += 1
    return changed / (a.width * a.height)


# Some checks build their samples from Windows font files or expect Windows font names.
WINDOWS_FONTS = os.path.isdir(r"C:\Windows\Fonts")
NEEDS_WINDOWS_FONTS = {
    "test_old_text_is_always_removed_at_any_size", "test_fixed_layout_report_lines_stay_separate",
    "test_edited_text_never_turns_bold", "test_number_columns_grow_to_the_left",
    "test_common_font_names_find_the_right_family", "test_area_moves_to_another_page_and_stays_real",
    "test_tight_lines_do_not_lose_letters", "test_complex_scripts",
    "test_same_font_on_later_pages_reads_back_correctly",
}


def test(fn):
    if not WINDOWS_FONTS and fn.__name__ in NEEDS_WINDOWS_FONTS:
        results.append(("SKIP", fn.__name__, "needs Windows fonts"))
        return
    try:
        note = fn()
        results.append(("PASS", fn.__name__, note or ""))
    except Exception as exc:
        traceback.print_exc()
        results.append(("FAIL", fn.__name__, repr(exc)[:200]))


def test_paragraph_edit_keeps_everything_else():
    d = load("reportlab.pdf")
    p = para_with(d, 0, "quick brown fox")
    assert p["lines"] >= 3 and p["align"] == "justify", (p["lines"], p["align"])
    before = d.render(0, 1)
    d.replace_text(0, p["id"], retext(p, "quick brown fox", "SLOW GREEN TURTLE"))
    text = reread(d)[0].get_text()
    assert "SLOW GREEN" in text and "quick brown" not in text.split("SLOW")[0]
    assert "Quarterly Report" in text and "Consulting" in text
    frac = diff_outside(before, d.render(0, 1), fitz.Rect(p["x0"], p["y0"], p["x1"], p["y1"] + 4))
    assert frac < 0.002, f"changed outside paragraph: {frac}"
    return f"outside diff {frac:.5f}"


def test_mixed_styles_survive():
    d = load("reportlab.pdf")
    p = para_with(d, 0, "bold words")
    styles = {(r["bold"], r["italic"], r["color"]) for r in p["runs"]}
    assert len(styles) >= 4, styles
    d.replace_text(0, p["id"], retext(p, "A sentence", "One changed sentence"))
    spans = [s for b in d.doc[0].get_text("dict")["blocks"] if b["type"] == 0
             for l in b["lines"] for s in l["spans"]]
    assert any("bold words" in s["text"] and s["flags"] & 16 for s in spans)
    assert any("red" in s["text"] and s["color"] == 0xC00000 for s in spans)
    again = para_with(d, 0, "One changed sentence")
    whole = "".join(r["text"] for r in again["runs"])
    assert "bold words" in whole and "second line" in whole and again["lines"] == p["lines"], (again["lines"], whole)
    d.replace_text(0, again["id"], retext(again, "One changed", "Twice changed"))
    assert "Twice changed sentence" in d.doc[0].get_text()


def test_table_cell_stays_a_cell():
    d = load("reportlab.pdf")
    p = para_with(d, 0, "1,200.00")
    assert "".join(r["text"] for r in p["runs"]) == "1,200.00"
    q = para_with(d, 0, "Consulting")
    assert "".join(r["text"] for r in q["runs"]) == "Consulting"
    d.replace_text(0, p["id"], retext(p, "1,200.00", "9,999.00"))
    assert "9,999.00" in d.doc[0].get_text() and "480.00" in d.doc[0].get_text()
    r = para_with(d, 0, "9,999.00")
    d.replace_text(0, r["id"], retext(r, "9,999.00", "1,239,999.00"))
    n = para_with(d, 0, "1,239,999.00")
    assert abs(n["x1"] - p["x1"]) < 1, ("right edge of the column moved", p["x1"], n["x1"])


def test_old_text_is_always_removed_at_any_size():
    for size in (4, 6, 8, 11, 16, 30):
        for name, kwargs in (("base", {"fontname": "cour"}),
                             ("embedded", {"fontname": "F0", "fontfile": r"C:\Windows\Fonts\cour.ttf"}),
                             ("proportional", {"fontname": "F0", "fontfile": r"C:\Windows\Fonts\georgia.ttf"})):
            doc = fitz.open()
            page = doc.new_page()
            page.insert_text((60, 100), "Before TOKEN after", fontsize=size, **kwargs)
            # Touching the line above, but in another column so it isn't the same paragraph.
            page.insert_text((60 + size * 12, 100 + size), "Next line stays", fontsize=size, **kwargs)
            path = f"{DIR}/size.pdf"
            doc.save(path)
            for variant in ("plain", "subset"):
                if variant == "subset":
                    doc.subset_fonts()
                    doc.save(path, garbage=3)
                d = load("size.pdf")
                p = para_with(d, 0, "TOKEN")
                d.replace_text(0, p["id"], [dict(p["runs"][0], text="Changed words")])
                text = d.doc[0].get_text().replace("\xa0", " ")
                assert "TOKEN" not in text and "Before" not in text, (size, name, variant, text)
                assert "Changed words" in text and "Next line" in text, (size, name, variant, text)


def test_fixed_layout_report_lines_stay_separate():
    d = load("report.pdf")
    texts = ["".join(r["text"] for r in p["runs"]) for p in d.layout(0)["paras"]]
    assert all(p["lines"] == 1 for p in d.layout(0)["paras"]), texts
    for wanted in ("MRS.SAMPLE NAME", "OLDTOWN CITY", "Schedule Number :  01", "COUNTRY - 751002"):
        assert wanted in texts, (wanted, texts)
    assert sum(t.startswith("-----") and set(t) == {"-"} for t in texts) == 2, texts
    assert all(p["align"] == "left" for p in d.layout(0)["paras"]), [(p["align"], p["runs"][0]["text"]) for p in d.layout(0)["paras"]]
    before = d.render(0, 2)

    # Letters the subset font never contained must still look like the same typewriter font.
    p = para_with(d, 0, "OLDTOWN CITY")
    d.replace_text(0, p["id"], retext(p, "OLDTOWN CITY", "NEWQUAY ZONE"))
    fonts = {s["font"] for b in d.doc[0].get_text("dict")["blocks"] if b["type"] == 0
             for l in b["lines"] for s in l["spans"] if "NEWQUAY" in s["text"] or "Q" in s["text"]}
    assert all("cour" in f.lower() for f in fonts), fonts
    assert "OLDTOWN" not in d.doc[0].get_text(), "the old text must be gone, not printed underneath"
    q = para_with(d, 0, "NEWQUAY ZONE")
    assert abs(q["x1"] - p["x1"]) < 1, "same character count in a fixed-width font keeps its width"

    p = para_with(d, 0, "Schedule Number")
    d.replace_text(0, p["id"], retext(p, "Schedule Number :  01", "Schedule Number :  02"))
    # The sample's untouched lines carry the generator's no-break spaces and soft hyphens.
    text = reread(d)[0].get_text().replace("\xa0", " ").replace("\xad", "-")
    for kept in ("MRS.SAMPLE NAME", "Account No. : 12345678901234", "78255", "02-12-2025", "Schedule Number :  02", "-" * 130):
        assert kept in text, (kept, text)
    frac = diff_outside(before, d.render(0, 2), fitz.Rect(0, 0, 0, 0), 2)
    assert frac < 0.002, f"too much of the page changed: {frac}"
    return f"page change {frac:.5f}"


def test_edited_text_never_turns_bold():
    # A first fragment whose spacing misleads the font check must not change the rest of the page.
    doc = fitz.open()
    p = doc.new_page()
    for i in range(8):
        p.insert_text((60, 80 + i * 14), f"0{i + 1}-10-2025     16,668.00", fontsize=6, fontname="F0",
                      fontfile=r"C:\Windows\Fonts\cour.ttf")
    doc.save(f"{DIR}/dates.pdf", garbage=3)
    d = load("dates.pdf")
    q = para_with(d, 0, "03-10-2025")
    assert q["runs"][0]["xref"] > 0 and not q["runs"][0]["bold"], q["runs"][0]
    d.replace_text(0, q["id"], retext(q, "03-10-2025", "05-10-2026"))
    spans = [s for b in d.doc[0].get_text("dict")["blocks"] if b["type"] == 0
             for l in b["lines"] for s in l["spans"] if "05-10-2026" in s["text"].replace("\xad", "-")]
    assert spans and not any(s["flags"] & 16 or "bold" in s["font"].lower() for s in spans), spans
    pairs = [("0", 0.6), ("1", 0.6), ("2", 0.6), ("5", 0.6), ("9", 0.6), ("7", 0.6), ("3", 0.6)]
    chars = [{"c": c, "origin": (i * 3.6, 0)} for i, (c, _) in enumerate(pairs)]
    from engine import closest_by_metrics
    family = closest_by_metrics([(chars, 6)], "Arial", False, False)
    assert family, family
    return f"digits-only sample picks {family}, regular weight"


def test_saved_pdf_can_be_edited_again_with_same_font():
    d = load("embedded.pdf")
    p = para_with(d, 0, "quick")
    d.replace_text(0, p["id"], retext(p, "quick", "rapid"))
    d.add_text(0, 60, 600, None, [{"text": "First session note", "family": "Calibri", "font": "Calibri",
                                   "size": 12, "color": "#000000", "xref": 0}])
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 20, 20), False)
    d.add_image(0, [50, 650, 100, 700], pix.tobytes("png"))
    images = len(d.layout(0)["images"])
    saved = d.export()

    later = Document(saved, "saved.pdf")
    q = para_with(later, 0, "rapid")
    assert q["runs"][0]["xref"] > 0, "the original font must still be reusable after saving"
    later.replace_text(0, q["id"], retext(q, "rapid", "ZQX"))  # letters not used before
    text = later.doc[0].get_text().replace("\xa0", " ")
    assert "ZQX" in text
    fonts = {f[3].lower() for f in later.doc[0].get_fonts()}
    assert any("georgia" in f for f in fonts) and not any("arial" in f for f in fonts), fonts
    note = para_with(later, 0, "First session note")
    assert note["runs"][0]["xref"] > 0, "text added earlier must keep its font too"
    later.replace_text(0, note["id"], retext(note, "First session", "Second session"))
    assert len(later.layout(0)["images"]) == images
    later.move_image(0, later.layout(0)["images"][-1]["id"], [200, 650, 250, 700])
    third = Document(later.export(), "again.pdf")
    assert "Second session note" in third.doc[0].get_text().replace("\xa0", " ")
    return f"{len(saved)} bytes"


def _server():
    import importlib
    import tempfile
    os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="pdf_library_test_")
    import app as server
    importlib.reload(server)
    return server, server.app.test_client()


def _open(c, name, filename=None):
    import io
    data = open(f"{DIR}/{name}", "rb").read()
    r = c.post("/api/open", data={"file": (io.BytesIO(data), filename or name)})
    assert r.status_code == 200, r.json
    return r.json["id"]


def _edit(c, tab, old, new):
    para = next(p for p in c.get(f"/api/page/0/layout?doc={tab}").json["paras"]
                if old in "".join(r["text"] for r in p["runs"]))
    runs = [dict(r, text=r["text"].replace(old, new)) for r in para["runs"]]
    r = c.post(f"/api/op?doc={tab}", json={"type": "replace_text", "page": 0, "id": para["id"], "runs": runs})
    assert r.status_code == 200, r.json
    return r.json


def _cleanup(server):
    import shutil
    for d in list(server.docs.values()):
        d.close()
    server.docs.clear()
    shutil.rmtree(os.environ["LOCALAPPDATA"], ignore_errors=True)


def test_tabs_edit_independently():
    server, c = _server()
    a, b = _open(c, "embedded.pdf", "a.pdf"), _open(c, "reportlab.pdf", "b.pdf")
    assert a != b and [t["name"] for t in c.get("/api/tabs").json["tabs"]] == ["a.pdf", "b.pdf"]
    _edit(c, a, "quick", "rapid")
    _edit(c, b, "Quarterly", "Yearly")
    assert "rapid" in server.docs[a].doc[0].get_text() and "Yearly" not in server.docs[a].doc[0].get_text()
    assert "Yearly" in server.docs[b].doc[0].get_text() and "rapid" not in server.docs[b].doc[0].get_text()
    # Undo in one tab must leave the other alone.
    assert c.post(f"/api/op?doc={a}", json={"type": "undo"}).json["canUndo"] is False
    assert "quick" in server.docs[a].doc[0].get_text() and "Yearly" in server.docs[b].doc[0].get_text()
    # Two copies of the same file are separate too.
    c1, c2 = _open(c, "embedded.pdf", "x.pdf"), _open(c, "embedded.pdf", "x.pdf")
    _edit(c, c1, "quick", "first")
    assert "first" not in server.docs[c2].doc[0].get_text()
    assert c.post(f"/api/close?doc={c1}").status_code == 200
    assert c.get(f"/api/page/0/layout?doc={c1}").status_code == 400
    assert "quick" in server.docs[c2].doc[0].get_text(), "closing a tab must not affect another"
    assert c.get("/api/page/0/layout").status_code == 400, "a request must name its tab"
    _cleanup(server)


def test_saved_pdfs_library():
    server, c = _server()
    tab = _open(c, "embedded.pdf", "mine.pdf")
    assert c.get("/api/library").json["items"] == [], "an unedited PDF is not stored"
    assert _edit(c, tab, "quick", "rapid")["saved"] is False
    items = c.get("/api/library").json["items"]
    assert len(items) == 1 and items[0]["name"] == "mine.pdf" and not items[0]["saved"] and items[0]["openTab"] == tab
    assert c.post(f"/api/save?doc={tab}").json["saved"] is True
    item = c.get("/api/library").json["items"][0]
    assert item["saved"] and item["id"] == items[0]["id"], "saving must not create a duplicate"
    # Opening what is already open goes to that tab.
    assert c.post(f"/api/library/{item['id']}/open").json["id"] == tab
    assert c.get(f"/api/library/{item['id']}/thumb.png").mimetype == "image/png"

    # Edits after saving survive closing and reopening, and it stays saved.
    _edit(c, tab, "rapid", "later")
    c.post(f"/api/close?doc={tab}")
    assert c.get("/api/library").json["items"][0]["openTab"] is None
    reopened = c.post(f"/api/library/{item['id']}/open").json
    assert reopened["saved"] is True and reopened["name"] == "mine.pdf"
    assert "later" in server.docs[reopened["id"]].doc[0].get_text()
    # ... and it can be edited again.
    _edit(c, reopened["id"], "later", "again")
    assert "again" in server.docs[reopened["id"]].doc[0].get_text()

    r = c.post(f"/api/library/{item['id']}/rename", json={"name": "bank/statement"})
    assert r.json["name"] == "bankstatement.pdf" and server.docs[reopened["id"]].name == "bankstatement.pdf"
    assert c.post(f"/api/library/{item['id']}/rename", json={"name": "  "}).status_code == 400
    dl = c.get(f"/api/library/{item['id']}/download")
    assert "bankstatement_edited.pdf" in dl.headers["Content-Disposition"]
    assert "again" in fitz.open(stream=dl.data, filetype="pdf")[0].get_text()
    assert c.post("/api/library/..%2f..%2fevil/open").status_code in (400, 404)
    assert c.post("/api/library/" + "0" * 32 + "/open").status_code == 400

    # Automatic clean-up removes old unsaved work only.
    for n in range(server.RECENT_LIMIT + 3):
        t = _open(c, "form.pdf", f"f{n}.pdf")
        c.post(f"/api/op?doc={t}", json={"type": "insert_page", "page": 0})
        c.post(f"/api/close?doc={t}")
    left = c.get("/api/library").json["items"]
    # The one closed last is trimmed on the next edit, so it may still be listed.
    assert sum(not i["saved"] for i in left) <= server.RECENT_LIMIT + 1, len(left)
    assert any(i["id"] == item["id"] for i in left), "a saved PDF must never be removed automatically"

    # Deleting an open item closes its tab.
    assert c.delete(f"/api/library/{item['id']}").json["closedTab"] == reopened["id"]
    assert reopened["id"] not in server.docs
    assert not any(i["id"] == item["id"] for i in c.get("/api/library").json["items"])
    _cleanup(server)


def test_number_columns_grow_to_the_left():
    doc = fitz.open()
    page = doc.new_page()
    right_edge = 300
    font = fitz.Font("helv")
    values = ["8,160.00"] * 6 + ["989.39", "1,46,879.96"]
    for n, value in enumerate(values):
        x = right_edge - font.text_length(value, 8)
        page.insert_text((x, 100 + n * 16), value, fontsize=8, fontname="helv")
        page.insert_text((60, 100 + n * 16), f"01.0{n + 1}.2025", fontsize=8, fontname="helv")
    doc.save(f"{DIR}/column.pdf")
    d = load("column.pdf")
    paras = d.layout(0)["paras"]
    assert all(p["align"] == "right" for p in paras if "," in p["runs"][0]["text"] or p["runs"][0]["text"] == "989.39"), paras
    assert all(p["align"] == "left" for p in paras if p["runs"][0]["text"].startswith("01.0")), "dates are not right-aligned"
    cell = next(p for p in paras if p["baseline"] == min(q["baseline"] for q in paras if "," in q["runs"][0]["text"]) and "," in p["runs"][0]["text"])
    d.replace_text(0, cell["id"], [dict(cell["runs"][0], text="12,34,567.00")])
    grown = para_with(d, 0, "12,34,567.00")
    assert abs(grown["x1"] - cell["x1"]) < 1, (grown["x1"], cell["x1"])
    assert grown["x0"] < cell["x0"] - 5, "a longer amount must grow to the left"


AREA = [70, 200, 300, 330]


def _text(d, page):
    # The sample generator's fonts read back spaces as no-break spaces.
    return d.doc[page].get_text().replace("\xa0", " ").replace("\xad", "-")


def _spans(d, page):
    return [s for b in d.doc[page].get_text("dict")["blocks"] if b["type"] == 0 for l in b["lines"] for s in l["spans"]]


def _pic_of(d, page, rect):
    return d.doc[page].get_pixmap(matrix=fitz.Matrix(2, 2), clip=fitz.Rect(rect), alpha=False)


def test_common_font_names_find_the_right_family():
    from engine import EDITOR_FONTS, FONTS, _norm
    assert "timesnewroman" in FONTS.families, "Times New Roman must be recognised as installed"
    cases = {"Times-Roman": "Times New Roman", "Times-Bold": "Times New Roman", "Times-BoldItalic": "Times New Roman",
             "TimesNewRomanPSMT": "Times New Roman", "TimesNewRomanPS-BoldItalicMT": "Times New Roman",
             "ABCDEF+TimesNewRomanPS-BoldMT": "Times New Roman", "Helvetica-Oblique": "Arial", "ArialMT": "Arial",
             "Arial-BoldMT": "Arial", "Courier-Bold": "Courier New", "CourierNewPS-ItalicMT": "Courier New",
             "CIDFont+Calibri,Bold": "Calibri", "LiberationSerif-Bold": "Times New Roman"}
    for name, family in cases.items():
        found, exact = FONTS.find_family(name)
        assert (found, exact) == (family, True), (name, found, exact)
    bold = FONTS.load("Times New Roman", True, False)
    assert "bold" in bold.name.lower() and "times" in bold.name.lower(), bold.name
    assert all(_norm(f) in FONTS.families for f in EDITOR_FONTS), [f for f in EDITOR_FONTS if _norm(f) not in FONTS.families]


def test_area_moves_to_another_page_and_stays_real():
    d = load("area.pdf")
    before = _pic_of(d, 0, AREA).samples
    d.transfer_area(0, AREA, 1, 100, 300, move=True)
    first, second = _text(d, 0), _text(d, 1)
    assert "Row one" not in first and "Row two" not in first
    assert "Keep this heading" in first and "Stays behind" in first
    assert "Row one value 100" in second and "Second page footer" in second
    # Fonts and colours came along, and the text can be edited at its new place.
    spans = _spans(d, 1)
    assert any("Georgia" in s["font"] and "Row one" in s["text"].replace("\xa0", " ") for s in spans), spans
    two = [s for s in spans if "Row two" in s["text"].replace("\xa0", " ")]
    assert two and all((s["color"] >> 16) > 150 and "times" in s["font"].lower() and s["flags"] & 16 for s in two), two
    p = para_with(d, 1, "Row one")
    assert abs(p["originX"] - 110) < 1 and abs(p["originY"] - 325) < 1, (p["originX"], p["originY"])
    d.replace_text(1, p["id"], retext(p, "100", "999"))
    assert "999" in d.doc[1].get_text()
    # Shapes and the picture moved too, and left page one.
    assert len(d.doc[0].get_image_info()) == 0 and len(d.doc[1].get_image_info()) == 1
    box = fitz.Rect(d.doc[1].get_image_info()[0]["bbox"])
    assert abs(box.x0 - 112.75) < 1 and abs(box.y0 - 362) < 1.5, box  # the picture sat at x=82.75
    assert not d.doc[0].get_drawings(), d.doc[0].get_drawings()
    assert len(d.doc[1].get_drawings()) == 2
    assert all(sum(_pic_of(d, 0, AREA).pixel(x, y)[:3]) > 750 for x in range(0, 460, 9) for y in range(0, 260, 9))
    # It looks the same at the destination, apart from the edit made above.
    d.undo(); d.redo()
    assert len(before) == len(_pic_of(d, 1, [100, 300, 330, 430]).samples)


def test_area_copy_keeps_the_original_and_looks_the_same():
    d = load("area.pdf")
    d.transfer_area(0, AREA, 1, 70, 200, move=False)
    assert "Row one" in _text(d, 0) and "Row one" in _text(d, 1)
    a, b = _pic_of(d, 0, AREA), _pic_of(d, 1, AREA)
    same = sum(abs(x - y) <= 60 for x, y in zip(a.samples, b.samples)) / len(a.samples)
    assert same > 0.985, f"copy differs: {same}"
    assert len(d.doc[0].get_image_info()) == 1 and len(d.doc[1].get_image_info()) == 1


def test_area_same_page_and_undo():
    d = load("area.pdf")
    original = d.render(0, 1)
    d.transfer_area(0, AREA, 0, 300, 560, move=True)
    assert d.render(0, 1) != original
    p = para_with(d, 0, "Row one")
    assert p["y0"] > 500
    d.undo()
    assert d.render(0, 1) == original
    d.redo()
    assert para_with(d, 0, "Row two")["y0"] > 500


def test_area_takes_only_selected_part_of_text():
    d = load("reportlab.pdf")
    p = para_with(d, 0, "Consulting")
    q = para_with(d, 0, "1,200.00")
    rect = [p["x0"] - 2, p["y0"] - 1, p["x1"] + 2, p["y1"] + 1]
    d.transfer_area(0, rect, 0, 50, 700, move=True)
    text = _text(d, 0)
    assert "1,200.00" in text and "Licences" in text
    moved = para_with(d, 0, "Consulting")
    assert abs(moved["left"] - 50) < 3 and moved["y0"] >= 699
    # The grid and header colour extend beyond the box, so they stay where they are.
    assert para_with(d, 0, "1,200.00")["y0"] == q["y0"]
    grid = sum(len(x["items"]) for x in d.doc[0].get_drawings())
    assert grid > 3


def test_area_with_scan_moves_as_picture():
    d = load("scan.pdf")
    rect = [60, 70, 400, 120]
    ink = lambda: sum(sum(_pic_of(d, 0, rect).pixel(x, y)[:3]) < 300 for x in range(0, 680, 3) for y in range(0, 100, 3))
    assert ink() > 10
    d.transfer_area(0, rect, 0, 100, 500, move=True)
    assert ink() == 0, "the scanned words must be gone from the old place"
    pic = _pic_of(d, 0, [100, 500, 440, 550])
    assert any(sum(pic.pixel(x, y)[:3]) < 300 for x in range(0, 680, 3) for y in range(0, 100, 3))
    assert any("picture" in w for w in d.warnings)


def test_area_rules_and_uneven_crop():
    d = load("area.pdf")
    # An empty area, or one that wouldn't fit, changes nothing.
    original = d.render(0, 1)
    for bad in ([300, 600, 500, 700, 1, 50, 50], [70, 200, 300, 330, 1, 500, 700]):
        try:
            d.transfer_area(0, bad[:4], bad[4], bad[5], bad[6], move=True)
            raise AssertionError("should be refused")
        except EditError:
            pass
    assert d.render(0, 1) == original
    d.transfer_area(0, AREA, 2, 50, 80, move=False)
    p = para_with(d, 2, "Row one")
    assert abs(p["originX"] - 60) < 1 and abs(p["originY"] - 105) < 1, (p["originX"], p["originY"])
    info = d.doc[2].get_image_info()[0]["bbox"]
    assert abs(info[0] - 62.75) < 1 and abs(info[1] - 142) < 1.5, info
    box = next(x for x in d.doc[2].get_drawings() if x.get("fill"))
    assert abs(box["rect"].x0 - 50) < 1.5 and abs(box["rect"].y0 - 80) < 1.5, box["rect"]


def test_white_on_colour_keeps_background():
    d = load("reportlab.pdf")
    p = para_with(d, 0, "Item")
    d.replace_text(0, p["id"], retext(p, "Item", "Product"))
    clip = fitz.Rect(p["x0"] + 60, p["y0"], p["x0"] + 100, p["y1"])
    pix = d.doc[0].get_pixmap(clip=clip)
    assert pix.pixel(pix.width // 2, pix.height // 2)[2] > pix.pixel(pix.width // 2, pix.height // 2)[0]
    r = para_with(d, 0, "Product")
    assert r["runs"][0]["color"].lower() == "#ffffff"


def test_embedded_font_reused():
    d = load("embedded.pdf")
    p = para_with(d, 0, "quick")
    assert p["runs"][0]["xref"] > 0, "embedded Georgia not trusted"
    before = len(d.doc[0].get_fonts())
    d.replace_text(0, p["id"], retext(p, "quick", "rapid"))
    assert "rapid" in d.doc[0].get_text()
    fonts = {f[3] for f in d.doc[0].get_fonts()}
    assert any("georgia" in f.lower() for f in fonts), fonts
    # Coloured box behind the text must survive: no white patch.
    pix = d.doc[0].get_pixmap(clip=fitz.Rect(45, 45, 55, 55))
    assert pix.pixel(5, 5)[2] < 230, pix.pixel(5, 5)
    return f"fonts {before}->{len(fonts)}"


def test_image_move_resize_delete_add():
    d = load("embedded.pdf")
    imgs = d.layout(0)["images"]
    assert len(imgs) == 2
    target = fitz.Rect(320, 400, 520, 500)
    d.move_image(0, imgs[0]["id"], list(target))
    got = [fitz.Rect(i["x0"], i["y0"], i["x1"], i["y1"]) for i in d.layout(0)["images"]]
    assert any(abs(g.x0 - 320) < .6 and abs(g.y1 - 500) < .6 and abs(g.width - 200) < .6 for g in got), got
    d.delete_image(0, d.layout(0)["images"][1]["id"])
    assert len(d.layout(0)["images"]) == 1
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 50, 50), False)
    pix.set_rect(pix.irect, (10, 200, 10))
    d.add_image(0, [50, 600, 150, 700], pix.tobytes("png"))
    assert len(d.layout(0)["images"]) == 2
    assert len(reread(d)[0].get_image_info()) == 2


def test_add_text_is_real_and_editable():
    d = load("embedded.pdf")
    d.add_text(0, 60, 600, 300, [{"text": "Brand new note with accents éàü €", "family": "Arial",
                                  "font": "Arial", "size": 14, "color": "#0a7f3f", "xref": 0}])
    assert "Brand new note with accents éàü €" in reread(d)[0].get_text()
    p = para_with(d, 0, "Brand new")
    assert abs(p["y0"] - 600) < 6 and abs(p["left"] - 60) < 1, (p["y0"], p["left"])
    d.replace_text(0, p["id"], retext(p, "Brand new", "Edited"))
    assert "Edited note" in d.doc[0].get_text() and "Brand new" not in d.doc[0].get_text()


def test_rotated_page():
    d = load("quirks.pdf")
    p = para_with(d, 0, "stored rotated")
    assert d.doc[0].rotation == 0
    before = fitz.Rect(p["x0"], p["y0"], p["x1"], p["y1"])
    d.replace_text(0, p["id"], retext(p, "stored rotated", "kept rotated"))
    q = para_with(d, 0, "kept rotated")
    assert fitz.Rect(q["x0"], q["y0"], q["x1"], q["y1"]).intersects(before)
    return f"angle {q['angle']}"


def test_rotated_crop_looks_identical():
    raw = fitz.open(f"{DIR}/rotcrop.pdf")
    d = load("rotcrop.pdf")
    for i in range(3):
        a, b = raw[i].get_pixmap(alpha=False), d.doc[i].get_pixmap(alpha=False)
        assert (a.width, a.height) == (b.width, b.height), (i, a.width, a.height, b.width, b.height)
        assert diff_outside(a.tobytes("png"), b.tobytes("png"), fitz.Rect(0, 0, 0, 0)) < 0.002, f"page {i}"
        p = para_with(d, i, "uneven crop")
        d.replace_text(i, p["id"], retext(p, "uneven", "odd"))
        q = para_with(d, i, "odd crop")
        assert abs(q["originX"] - p["originX"]) < 1.5 and abs(q["originY"] - p["originY"]) < 1.5


def test_uneven_crop_everything_lands_where_asked():
    d = load("crop_uneven.pdf")
    p = para_with(d, 0, "level text")
    d.replace_text(0, p["id"], retext(p, "level", "flat"))
    q = para_with(d, 0, "flat text")
    assert abs(q["originX"] - p["originX"]) < 1 and abs(q["originY"] - p["originY"]) < 1, (p["originY"], q["originY"])
    d.add_text(0, 30, 200, None, [{"text": "AddedHere", "family": "Arial", "font": "Arial", "size": 12, "color": "#000000", "xref": 0}])
    r = para_with(d, 0, "AddedHere")
    assert abs(r["left"] - 30) < 1 and abs(r["y0"] - 200) < 5, (r["left"], r["y0"])
    img = d.layout(0)["images"][0]
    d.move_image(0, img["id"], [10, 250, 110, 310])
    i = d.layout(0)["images"][0]
    assert abs(i["x0"] - 10) < 1 and abs(i["y0"] - 250) < 1 and abs(i["y1"] - 310) < 1, i
    d.add_image(0, [200, 20, 260, 80], fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 9, 9), False).tobytes("png"))
    assert any(abs(i["x0"] - 200) < 1 and abs(i["y0"] - 20) < 1 for i in d.layout(0)["images"])
    d.erase(0, [r["x0"] - 1, r["y0"] - 1, r["x1"] + 1, r["y1"] + 1])
    assert "AddedHere" not in d.doc[0].get_text()


def test_copy_paste_text_is_faithful():
    d = load("embedded.pdf")
    line = "well-known; a b-c \u00b7 price 5\u20ac"
    for fam in ("Arial", "Calibri", "Times New Roman"):
        d.add_text(0, 60, 500, 450, [{"text": line, "family": fam, "font": fam, "size": 12, "color": "#000000", "xref": 0}])
        got = reread(d)[0].get_text()
        assert line in got, (fam, [l for l in got.splitlines() if "known" in l])
        d.undo()


def test_tight_lines_do_not_lose_letters():
    doc = fitz.open()
    p = doc.new_page()
    for i in range(6):
        p.insert_text((72, 100 + i * 9), f"Line number {i} gjpqy ABCDEFG", fontsize=11)
    path = f"{DIR}/tight.pdf"
    doc.save(path)
    d = load("tight.pdf")
    q = para_with(d, 0, "Line number 3")
    d.replace_text(0, q["id"], retext(q, "number 3", "THREE"))
    text = d.doc[0].get_text()
    for i in (0, 1, 2, 4, 5):
        assert f"Line number {i} gjpqy ABCDEFG" in text, text
    assert "Line THREE gjpqy" in text


def test_crop_box_page():
    d = load("quirks.pdf")
    p = para_with(d, 1, "Cropped page")
    d.replace_text(1, p["id"], retext(p, "Cropped page", "Moved crop"))
    q = para_with(d, 1, "Moved crop")
    assert abs(q["originY"] - p["originY"]) < 1 and abs(q["left"] - p["left"]) < 1, (p["left"], q["left"], p["originY"], q["originY"])
    d.add_text(1, 40, 40, None, [{"text": "CropAdd", "family": "Arial", "font": "Arial", "size": 12, "color": "#000000", "xref": 0}])
    r = para_with(d, 1, "CropAdd")
    assert abs(r["left"] - 40) < 1 and abs(r["y0"] - 40) < 5, (r["left"], r["y0"])
    img = d.layout(1)["images"][0]
    d.move_image(1, img["id"], [10, 300, 210, 433])
    i = d.layout(1)["images"][0]
    assert abs(i["x0"] - 10) < 1 and abs(i["y0"] - 300) < 1, i
    d.add_image(1, [300, 300, 400, 400], fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 9, 9), False).tobytes("png"))
    assert any(abs(i["x0"] - 300) < 1 and abs(i["y0"] - 300) < 1 for i in d.layout(1)["images"])


def test_rotated_text_keeps_direction():
    d = load("quirks.pdf")
    for needle in ("Slanted", "Second vertical"):
        p = para_with(d, 2, needle)
        d.replace_text(2, p["id"], retext(p, needle, needle + " X"))
        q = para_with(d, 2, needle + " X")
        assert abs(q["angle"] - p["angle"]) < 1, (p["angle"], q["angle"])
        assert abs(q["originX"] - p["originX"]) < 1.5 and abs(q["originY"] - p["originY"]) < 1.5, (p["originX"], q["originX"], p["originY"], q["originY"])


def test_ocr_scan_then_edit():
    d = load("scan.pdf")
    assert not d.layout(0)["paras"]
    size0 = len(d.export())
    n = d.ocr(0)
    assert n >= 3, n
    p = para_with(d, 0, "Invoice")
    assert p["raster"], "OCR text should be flagged as inside the image"
    d.replace_text(0, p["id"], retext(p, "Invoice", "Receipt"))
    q = para_with(d, 0, "Receipt")
    assert not q["raster"], "written text should now be visible text"
    rect = fitz.Rect(q["x0"], q["y0"], q["x1"], q["y1"])
    pix = d.doc[0].get_pixmap(clip=rect, dpi=100)
    assert any(sum(pix.pixel(x, y)[:3]) < 200 for x in range(pix.width) for y in range(pix.height))
    body = para_with(d, 0, "quick brown")
    assert body["lines"] > 1 and body["raster"], "a multi-line scanned paragraph must count as scanned"
    d.replace_text(0, body["id"], [dict(body["runs"][0], text="Short replacement.")])
    box = fitz.Rect(body["x0"], body["y0"] + body["lineHeight"] * 1.2, body["x1"], body["y1"])
    pix = d.doc[0].get_pixmap(clip=box, dpi=100)
    assert all(sum(pix.pixel(x, y)[:3]) > 600 for x in range(pix.width) for y in range(pix.height)), "old scan text is still visible"
    return f"{n} lines, {size0}->{len(d.export())} bytes"


def test_invisible_ocr_layer_detected():
    d = load("scan_ocr_layer.pdf")
    p = para_with(d, 0, "Scanned Invoice")
    assert p["raster"], p
    before = d.render(0, 1)
    d.replace_text(0, p["id"], retext(p, "Scanned", "Changed"))
    assert d.render(0, 1) != before


def test_erase_truly_removes():
    d = load("reportlab.pdf")
    p = para_with(d, 0, "Quarterly")
    d.erase(0, [p["x0"] - 2, p["y0"] - 2, p["x1"] + 2, p["y1"] + 2])
    assert "Quarterly" not in reread(d)[0].get_text()


def test_forms():
    d = load("form.pdf")
    w = d.layout(0)["widgets"]
    assert len(w) == 2 and w[0]["value"] == "Old value", w
    d.set_form(0, w[0]["xref"], "New value")
    d.set_form(0, w[1]["xref"], True)
    doc = reread(d)
    vals = {x.field_name: x.field_value for x in doc[0].widgets()}
    assert vals["name"] == "New value" and vals["agree"] not in (False, "Off"), vals


def test_password():
    try:
        load("locked.pdf")
        raise AssertionError("should need a password")
    except PasswordRequired:
        pass
    try:
        load("locked.pdf", "wrong")
        raise AssertionError("wrong password accepted")
    except PasswordRequired:
        pass
    d = load("locked.pdf", "abc123")
    p = para_with(d, 0, "Secret")
    d.replace_text(0, p["id"], retext(p, "Secret", "Open"))
    d.undo()
    d.redo()
    assert "Open document" in reread(d)[0].get_text()


def test_unlock_removes_password_and_restrictions():
    def unlocked_copy(d):
        out = reread(d)
        return not out.needs_pass and not (out.metadata or {}).get("encryption")
    d = load("locked.pdf", "abc123")
    assert d.unlocked == "password" and d.info()["unlocked"] == "password"
    assert unlocked_copy(d), "the download must open without a password"
    r = load("restricted.pdf")
    assert r.unlocked == "restrictions"
    p = para_with(r, 0, "Secret")
    r.replace_text(0, p["id"], retext(p, "Secret", "Open"))
    out = reread(r)
    assert unlocked_copy(r) and "Open document" in out[0].get_text()
    assert out.permissions & fitz.PDF_PERM_MODIFY and out.permissions & fitz.PDF_PERM_COPY
    assert load("embedded.pdf").unlocked is None


def test_unlock_through_the_server():
    import io
    server, c = _server()
    try:
        data = open(f"{DIR}/locked.pdf", "rb").read()
        def send(pw):
            return c.post("/api/open", data={"file": (io.BytesIO(data), "locked.pdf"), **({"password": pw} if pw else {})})
        r = send(None)
        assert r.status_code == 401 and r.json == {"password": True, "wrong": False}, r.json
        r = send("nope")
        assert r.status_code == 401 and r.json["wrong"] is True
        r = send("abc123")
        assert r.status_code == 200 and r.json["unlocked"] == "password", r.json
        tab = r.json["id"]
        _edit(c, tab, "Secret", "Open")
        out = fitz.open(stream=c.get(f"/api/export?doc={tab}").data, filetype="pdf")
        assert not out.needs_pass and "Open document" in out[0].get_text()
        item = c.get("/api/library").json["items"][0]
        assert item["unlocked"] == "password"
        c.post(f"/api/close?doc={tab}")
        again = c.post(f"/api/library/{item['id']}/open").json
        assert again["unlocked"] == "password" and "Open" in server.docs[again["id"]].doc[0].get_text()
    finally:
        _cleanup(server)


def test_editing_survives_a_cleared_temp_folder():
    import shutil
    d = load("embedded.pdf")
    p = para_with(d, 0, "quick")
    d.replace_text(0, p["id"], retext(p, "quick", "rapid"))
    shutil.rmtree(d.tmp)  # what a second copy of the editor, or Disk Clean-up, used to do
    p = para_with(d, 0, "lazy")
    d.replace_text(0, p["id"], retext(p, "lazy", "sleepy"))
    d.undo()  # the newest undo point was made after the clear-up
    assert "lazy" in d.doc[0].get_text() and "rapid" in d.doc[0].get_text()
    try:
        d.undo()
        raise AssertionError("a lost undo point must be reported")
    except EditError as e:
        assert "undo history" in str(e)
    assert not d.undo_stack and not d.redo_stack and "rapid" in d.doc[0].get_text()
    p = para_with(d, 0, "lazy")
    d.replace_text(0, p["id"], retext(p, "lazy", "calm"))
    assert "calm" in reread(d)[0].get_text()


def test_second_start_does_not_disturb_the_first():
    import socket, subprocess
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); s.listen()
        port = s.getsockname()[1]
        marker = os.path.join(os.environ.get("TEMP", "."), "pdf_editor_keepme_test")
        os.makedirs(marker, exist_ok=True)
        try:
            r = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py"), "--no-browser"],
                               env=dict(os.environ, PDF_EDITOR_PORT=str(port)), capture_output=True, text=True, timeout=60)
            assert r.returncode == 0 and "already running" in r.stdout, (r.returncode, r.stdout, r.stderr)
            assert os.path.isdir(marker), "the first copy's undo files must be left alone"
        finally:
            os.rmdir(marker)


def test_find_and_replace():
    d = load("embedded.pdf")
    exact, loose = d.find("the", case=True), d.find("the")
    assert len(loose) > len(exact) >= 2, (len(loose), len(exact))
    h = exact[0]
    assert h["x1"] > h["x0"] and h["y1"] > h["y0"] and h["page"] == 0
    assert d.replace("the", "a", True, h) == 1
    assert len(d.find("the", case=True)) == len(exact) - 1
    assert d.replace("the", "THE", True) == len(exact) - 1
    assert not d.find("the", case=True) and "THE" in reread(d)[0].get_text()
    d.undo()
    assert len(d.find("the", case=True)) == len(exact) - 1
    for args in (("zzqq", "y"), ("", "y")):
        try:
            d.replace(*args)
            raise AssertionError("an empty or missing search must be reported")
        except EditError:
            pass
    # a match split across two differently styled pieces of text
    runs = [{"text": "Total due", "bold": True}, {"text": "date here", "bold": False}]
    new, n = d._replace_runs(runs, "duedate", "-", True)
    assert n == 1 and new == [{"text": "Total -", "bold": True}, {"text": " here", "bold": False}], new


def test_organise_pages():
    d = load("area.pdf")
    first = [p.get_text().split("\n")[0] for p in d.doc]
    w, h = d.doc[0].rect.width, d.doc[0].rect.height
    d.arrange([2, 0, 0, 1])
    assert [p.get_text().split("\n")[0] for p in d.doc] == [first[2], first[0], first[0], first[1]]
    d.rotate_pages([1, 3])
    assert (d.doc[1].rect.width, d.doc[1].rect.height, d.doc[1].rotation) == (h, w, 0)
    d.arrange([1, 2])
    assert d.doc.page_count == 2
    d.undo()
    assert d.doc.page_count == 4
    for bad in ([], [9]):
        try:
            d.arrange(bad)
            raise AssertionError("a bad page order must be refused")
        except EditError:
            pass
    part = fitz.open(stream=d.export(pages=[3, 1]), filetype="pdf")
    assert [p.get_text().split("\n")[0] for p in part] == [first[1], first[0]]
    data = open(f"{DIR}/locked.pdf", "rb").read()
    try:
        d.merge(data, 1)
        raise AssertionError("a locked PDF needs its password to be added")
    except PasswordRequired:
        pass
    assert d.merge(data, 1, "abc123") == 1 and d.doc.page_count == 5
    assert "Secret" in d.doc[1].get_text() and first[2] in d.doc[0].get_text()
    d.undo()
    assert d.doc.page_count == 4


def test_compressed_download_is_smaller_and_keeps_text_and_transparency():
    d = load("embedded.pdf")
    p = para_with(d, 0, "quick")
    d.replace_text(0, p["id"], retext(p, "quick", "rapid (Test) - 50%"))
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 80, 40), 1)
    pix.set_rect(fitz.IRect(0, 0, 80, 40), (0, 0, 0, 0))
    pix.set_rect(fitz.IRect(0, 0, 20, 40), (0, 0, 0, 255))
    d.add_image(0, [300, 600, 380, 640], pix.tobytes("png"))
    big, small = d.export(), d.export(compress=True)
    assert len(small) < len(big) * 0.6, (len(big), len(small))
    a, b = fitz.open(stream=big, filetype="pdf"), fitz.open(stream=small, filetype="pdf")
    assert a[0].get_text() == b[0].get_text() and "rapid (Test) - 50%" in b[0].get_text()
    shot = b[0].get_pixmap(clip=fitz.Rect(300, 600, 380, 640), alpha=False)
    assert shot.pixel(5, 20)[0] < 60 and min(shot.pixel(70, 20)) > 230, "transparency must survive"


def test_find_pages_and_downloads_through_the_server():
    import io
    server, c = _server()
    try:
        tab = _open(c, "area.pdf")
        hits = c.post(f"/api/find?doc={tab}", json={"q": "heading"}).json["hits"]
        assert hits, hits
        r = c.post(f"/api/op?doc={tab}", json={"type": "replace", "query": "heading", "replacement": "title"})
        assert r.status_code == 200 and any("Replaced" in w for w in r.json["warnings"]), r.json
        assert c.post(f"/api/op?doc={tab}", json={"type": "arrange", "order": [0, 0, 1, 2]}).json["pages"].__len__() == 4
        assert c.post(f"/api/op?doc={tab}", json={"type": "rotate_pages", "pages": [0]}).status_code == 200
        r = c.get(f"/api/export?doc={tab}&pages=0,2&split=1")
        import zipfile
        names = zipfile.ZipFile(io.BytesIO(r.data)).namelist()
        assert names == ["area_page_1.pdf", "area_page_3.pdf"], names
        r = c.get(f"/api/export?doc={tab}&pages=1,2,3")
        assert "area_pages_2-4.pdf" in r.headers["Content-Disposition"]
        assert fitz.open(stream=r.data, filetype="pdf").page_count == 3
        small = c.get(f"/api/export?doc={tab}&compress=1")
        assert small.status_code == 200 and fitz.open(stream=small.data, filetype="pdf").page_count == 4
        data = open(f"{DIR}/locked.pdf", "rb").read()
        send = lambda **extra: c.post(f"/api/merge?doc={tab}", data={"file": (io.BytesIO(data), "locked.pdf"), "at": "4", **extra})
        r = send()
        assert r.status_code == 401 and r.json["password"], r.json
        r = send(password="abc123")
        assert r.status_code == 200 and len(r.json["pages"]) == 5, r.json
    finally:
        _cleanup(server)


def test_undo_redo_and_failure_rollback():
    d = load("embedded.pdf")
    original = d.render(0, 1)
    p = para_with(d, 0, "quick")
    d.replace_text(0, p["id"], retext(p, "quick", "rapid"))
    edited = d.render(0, 1)
    assert edited != original
    d.undo()
    assert d.render(0, 1) == original
    d.redo()
    assert d.render(0, 1) == edited
    try:
        d.replace_text(0, 99999, [])
    except Exception:
        pass
    assert d.render(0, 1) == edited, "failed edit must not change the file"
    assert "rapid" in d.doc[0].get_text()


def test_pages():
    d = load("quirks.pdf")
    d.insert_page(0)
    assert d.doc.page_count == 4
    d.delete_page(1)
    assert d.doc.page_count == 3
    w, h = d.doc[1].rect.width, d.doc[1].rect.height
    d.rotate_page(1)
    assert d.doc[1].rotation == 0 and d.doc[1].rect.width == h and d.doc[1].rect.height == w
    assert "Cropped page" in d.doc[1].get_text()


def test_complex_scripts():
    d = load("embedded.pdf")
    p = para_with(d, 0, "Stacked")
    runs = [dict(p["runs"][0], text="नमस्ते दुनिया")]
    d.replace_text(0, p["id"], runs)
    text = reread(d)[0].get_text()
    assert "न" in text, text[-80:]
    arabic = para_with(d, 0, "quick")
    d.replace_text(0, arabic["id"], [dict(arabic["runs"][0], text="مرحبا بالعالم", xref=0)])
    assert any("\u0600" <= c <= "\u06ff" or "\ufb50" <= c <= "\ufeff" for c in reread(d)[0].get_text())


def test_wrap_when_text_grows():
    d = load("reportlab.pdf")
    p = para_with(d, 0, "quick brown fox")
    r = p["runs"]
    long = [dict(r[0], text=r[0]["text"] + " " + r[0]["text"])]
    d.replace_text(0, p["id"], long)
    q = para_with(d, 0, "quick brown fox")
    assert q["lines"] > p["lines"], (p["lines"], q["lines"])
    assert q["x1"] <= p["x1"] + 1.5, (q["x1"], p["x1"])
    assert all(w.isascii() for w in d.doc[0].get_text().split()[:3])


def test_export_leaves_open_document_intact():
    d = load("embedded.pdf")
    q = para_with(d, 0, "quick")
    d.replace_text(0, q["id"], retext(q, "quick", "rapid"))
    before = d.render(0, 1)
    d.export()
    assert d.render(0, 1) == before
    r = para_with(d, 0, "rapid")
    d.replace_text(0, r["id"], retext(r, "rapid", "(swift)"))
    d.export()
    assert "(swift)" in d.doc[0].get_text().replace("\xa0", " ")


def test_same_font_on_later_pages_reads_back_correctly():
    doc = fitz.open()
    for _ in range(3):
        doc.new_page().insert_text((72, 100), "Plain line", fontsize=12, fontname="F0",
                                   fontfile=r"C:\Windows\Fonts\segoeui.ttf")
    doc.save(f"{DIR}/three.pdf")
    d = load("three.pdf")
    line = "well-known (see note); price 5\u20ac"
    for i in range(3):
        d.add_text(i, 72, 200, 400, [{"text": line, "family": "Segoe UI", "font": "Segoe UI", "size": 12,
                                      "color": "#000000", "xref": 0}])
        assert line in d.doc[i].get_text(), (i, d.doc[i].get_text())
    assert all(line in p.get_text() for p in reread(d))


def test_real_file_any_edit(path=None):
    notes = []
    for path in EXTRA:
        d = load(path)
        assert any(p["lines"] > 1 for i in range(3) for p in d.layout(i)["paras"]), "paragraphs not recognised"
        for i in range(min(d.doc.page_count, 3)):
            paras = d.layout(i)["paras"]
            if not paras:
                continue
            p = max(paras, key=lambda x: sum(len(r["text"]) for r in x["runs"]))
            words = p["runs"][0]["text"].split()
            if not words:
                continue
            before = d.render(i, 1)
            d.replace_text(i, p["id"], [dict(p["runs"][0], text=p["runs"][0]["text"] + " EDITZ")] + p["runs"][1:])
            assert "EDITZ" in reread(d)[i].get_text().replace("\n", " ")
            frac = diff_outside(before, d.render(i, 1), fitz.Rect(p["x0"], p["y0"], p["x1"], p["y1"] + 24))
            notes.append(f"{os.path.basename(path)[:22]} p{i + 1}: {frac:.4f} xref={p['runs'][0]['xref']}")
            assert frac < 0.01, notes[-1]
    return "; ".join(notes)


for fn in [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]:
    test(fn)
print()
for status, name, note in results:
    print(f"{status}  {name}  {note}")
skipped = sum(r[0] == "SKIP" for r in results)
print(f"\n{sum(r[0] == 'PASS' for r in results)}/{len(results) - skipped} passed"
      + (f", {skipped} skipped because they need Windows fonts" if skipped else ""))
