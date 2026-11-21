"""PDF editing engine: reads any PDF into editable paragraphs/images and writes
changes back into the real PDF content (no flattening, no overlay layer)."""
from __future__ import annotations

import math
import os
import re
import shutil
import statistics
import tempfile
import unicodedata

import fitz  # PyMuPDF

MAX_HISTORY = 40
TEXT_FLAGS = fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_MEDIABOX_CLIP

# Scripts that need shaping / bidi, which only the HTML writer can do.
COMPLEX_RANGES = [
    (0x0590, 0x08FF),  # Hebrew, Arabic, Syriac...
    (0x0900, 0x0DFF),  # Indic
    (0x0E00, 0x0EFF),  # Thai, Lao
    (0x0F00, 0x0FFF),  # Tibetan
    (0x1000, 0x109F),  # Myanmar
    (0x1780, 0x17FF),  # Khmer
    (0xFB1D, 0xFDFF),  # Presentation forms
    (0xFE70, 0xFEFF),
]

# Metric-compatible or very close stand-ins for fonts that are not installed.
FONT_ALIASES = {
    "helvetica": "Arial", "helveticaneue": "Arial", "arialmt": "Arial",
    "liberationsans": "Arial", "arimo": "Arial", "nimbussans": "Arial",
    "nimbussansl": "Arial", "freesans": "Arial", "swiss": "Arial",
    "times": "Times New Roman", "timesnewromanps": "Times New Roman",
    "timesroman": "Times New Roman", "liberationserif": "Times New Roman",
    "tinos": "Times New Roman", "nimbusroman": "Times New Roman",
    "nimbusromno9l": "Times New Roman", "freeserif": "Times New Roman",
    "courier": "Courier New", "couriernewps": "Courier New",
    "liberationmono": "Courier New", "cousine": "Courier New",
    "nimbusmono": "Courier New", "freemono": "Courier New",
    "carlito": "Calibri", "caladea": "Cambria",
    "dejavusans": "Verdana", "dejavuserif": "Georgia",
    "dejavusansmono": "Consolas", "helv": "Arial", "tiro": "Times New Roman",
    "cour": "Courier New", "roboto": "Arial", "opensans": "Segoe UI",
    "inter": "Segoe UI", "lato": "Calibri", "sfprotext": "Segoe UI",
    "sfprodisplay": "Segoe UI", "applesystem": "Segoe UI",
}
FALLBACK_CHAIN = [
    "Arial", "Segoe UI", "Segoe UI Symbol", "Nirmala UI", "Microsoft YaHei",
    "Yu Gothic", "Malgun Gothic", "Ebrima", "Segoe UI Emoji",
    "DejaVu Sans", "Noto Sans",  # Linux, e.g. the Docker image
]
# On Linux the common Windows fonts are usually missing; these free fonts have the same
# letter widths (or close), so text keeps its length and line breaks.
LINUX_STAND_INS = {
    "Arial": "Liberation Sans", "Times New Roman": "Liberation Serif", "Courier New": "Liberation Mono",
    "Calibri": "Carlito", "Cambria": "Caladea", "Verdana": "DejaVu Sans", "Tahoma": "DejaVu Sans",
    "Segoe UI": "DejaVu Sans", "Trebuchet MS": "DejaVu Sans", "Georgia": "DejaVu Serif",
    "Consolas": "DejaVu Sans Mono",
}
EDITOR_FONTS = [
    "Arial", "Calibri", "Cambria", "Consolas", "Courier New", "Georgia",
    "Segoe UI", "Tahoma", "Times New Roman", "Trebuchet MS", "Verdana",
]
# A line drawn with characters: ------ ====== ______ ......
RULE_LINE = re.compile(r"[-=_.*~+|\u2013\u2014\u2500\s]{4,}")
# Not "roman": it is part of the name in Times New Roman.
STYLE_WORDS = {"bold", "italic", "oblique", "regular", "normal"}


class PasswordRequired(Exception):
    pass


class EditError(Exception):
    pass


# ---------------------------------------------------------------- font lookup
def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def strip_subset(name: str) -> str:
    return re.sub(r"^(?:[A-Za-z0-9]+\+)+", "", name or "")


def family_key(name: str) -> str:
    """'ABCDEF+TimesNewRomanPS-BoldItalicMT' -> 'timesnewroman'."""
    n = strip_subset(name)
    n = re.sub(r"[,\-_ ]?(Bold|Italic|Oblique|Regular|Normal)", "", n, flags=re.I)
    n = re.sub(r"[,\-_ ](Roman|Book)$", "", n, flags=re.I)  # Times-Roman, but TimesNewRoman stays whole
    n = re.sub(r"(PSMT|PS|MT)$", "", n)
    return _norm(n)


class FontIndex:
    """Installed fonts: from the Windows registry (fast, no file scanning), or fontconfig on Linux."""

    def __init__(self):
        self.families: dict[str, dict] = {}
        self._cache: dict[tuple, fitz.Font] = {}
        self._load()

    def _add(self, label: str, path: str):
        label = re.sub(r"\s*\((TrueType|OpenType)\)\s*$", "", label, flags=re.I)
        for part in label.split(" & "):
            words = part.split()
            bold = italic = False
            while words and words[-1].lower() in STYLE_WORDS:
                w = words.pop().lower()
                bold |= w == "bold"
                italic |= w in ("italic", "oblique")
            if not words:
                continue
            family = " ".join(words)
            entry = self.families.setdefault(_norm(family), {"name": family, "faces": {}})
            entry["faces"].setdefault((bold, italic), path)

    def _load_linux(self):
        import subprocess
        try:
            out = subprocess.run(["fc-list", "--format", "%{file}\t%{family[0]}\t%{style[0]}\n"],
                                 capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            return
        plain = {"regular", "book", "normal", "roman", "bold", "italic", "oblique"}
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) != 3 or not parts[0].lower().endswith((".ttf", ".otf", ".ttc")):
                continue
            path, family, style = parts
            words = set(style.lower().split())
            if not words <= plain:
                continue  # light, condensed and similar faces would look wrong as a stand-in
            entry = self.families.setdefault(_norm(family), {"name": family, "faces": {}})
            entry["faces"].setdefault(("bold" in words, bool(words & {"italic", "oblique"})), path)
        for windows, free in LINUX_STAND_INS.items():
            if _norm(windows) not in self.families and _norm(free) in self.families:
                self.families[_norm(windows)] = {"name": windows, "faces": self.families[_norm(free)]["faces"]}

    def _load(self):
        try:
            import winreg
        except ImportError:
            self._load_linux()
            return
        windir = os.environ.get("WINDIR", r"C:\Windows")
        key = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(root, key) as k:
                    for i in range(winreg.QueryInfoKey(k)[1]):
                        label, value, _ = winreg.EnumValue(k, i)
                        path = value if os.path.isabs(value) else os.path.join(windir, "Fonts", value)
                        if path.lower().endswith((".ttf", ".otf", ".ttc")) and os.path.exists(path):
                            self._add(label, path)
            except OSError:
                continue

    def find_family(self, name: str, serif=False, mono=False) -> tuple[str, bool]:
        """Return (family name, exact_match) for a PDF font name."""
        key = family_key(name)
        if key in self.families:
            return self.families[key]["name"], True
        full = _norm(strip_subset(name))
        if full in self.families:
            return self.families[full]["name"], True
        alias = FONT_ALIASES.get(key) or FONT_ALIASES.get(full)
        if alias and _norm(alias) in self.families:
            return alias, True
        low = key
        if mono or "mono" in low or "courier" in low or "consol" in low:
            generic = "Consolas" if "consolas" in self.families else "Courier New"
        elif serif or ("serif" in low and "sans" not in low) or "times" in low:
            generic = "Times New Roman"
        else:
            generic = "Arial"
        return generic, False

    def load(self, family: str, bold=False, italic=False) -> fitz.Font:
        ckey = (_norm(family), bold, italic)
        if ckey in self._cache:
            return self._cache[ckey]
        entry = self.families.get(_norm(family))
        font = None
        if entry:
            faces = entry["faces"]
            path = (faces.get((bold, italic)) or faces.get((bold, False))
                    or faces.get((False, italic)) or faces.get((False, False))
                    or next(iter(faces.values())))
            try:
                font = fitz.Font(fontfile=path)
            except Exception:
                font = None
        if font is None:
            base = {"times new roman": "ti", "courier new": "co"}.get(family.lower(), "he")
            suffix = {(False, False): "ro" if base == "ti" else "lv" if base == "he" else "ur",
                      (True, False): "bo", (False, True): "it", (True, True): "bi"}[(bold, italic)]
            names = {"he": {"lv": "helv", "bo": "hebo", "it": "heit", "bi": "hebi"},
                     "ti": {"ro": "tiro", "bo": "tibo", "it": "tiit", "bi": "tibi"},
                     "co": {"ur": "cour", "bo": "cobo", "it": "coit", "bi": "cobi"}}
            font = fitz.Font(names[base][suffix])
        self._cache[ckey] = font
        return font

    def file_for(self, family: str, bold=False, italic=False) -> str | None:
        entry = self.families.get(_norm(family))
        if not entry:
            return None
        faces = entry["faces"]
        return (faces.get((bold, italic)) or faces.get((False, False))
                or next(iter(faces.values())))

    def fallback_for(self, ch: str, bold=False, italic=False) -> fitz.Font | None:
        cp = ord(ch)
        for family in FALLBACK_CHAIN:
            if _norm(family) in self.families:
                f = self.load(family, bold, italic)
                if f.has_glyph(cp):
                    return f
        return None


FONTS = FontIndex()


# ------------------------------------------------------------------ utilities
def hex_of(color) -> str:
    if isinstance(color, int):
        return f"#{color & 0xFFFFFF:06x}"
    r, g, b = color
    return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"


def rgb_of(hex_color: str):
    h = (hex_color or "#000000").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def is_complex(text: str) -> bool:
    return any(lo <= ord(c) <= hi for c in text for lo, hi in COMPLEX_RANGES)


def sample_background(page: fitz.Page, rect: fitz.Rect):
    """Median colour of the ring just outside rect, as rendered on the page."""
    clip = (fitz.Rect(rect) + (-3, -3, 3, 3)) & page.rect
    if clip.is_empty:
        return (1, 1, 1)
    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=clip, alpha=False, colorspace=fitz.csRGB)
    w, h, n, s = pix.width, pix.height, pix.n, pix.samples
    px = []
    for x in range(w):
        for y in (0, 1, h - 2, h - 1):
            if 0 <= y < h:
                px.append(s[(y * w + x) * n:(y * w + x) * n + 3])
    for y in range(h):
        for x in (0, 1, w - 2, w - 1):
            if 0 <= x < w:
                px.append(s[(y * w + x) * n:(y * w + x) * n + 3])
    if not px:
        return (1, 1, 1)
    return tuple(statistics.median(p[i] for p in px) / 255 for i in range(3))


def sample_ink(page: fitz.Page, rect: fitz.Rect):
    """Typical text colour inside rect: the median of the darkest pixels."""
    clip = fitz.Rect(rect) & page.rect
    if clip.is_empty:
        return (0, 0, 0)
    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=clip, alpha=False, colorspace=fitz.csRGB)
    s, n = pix.samples, pix.n
    px = sorted((tuple(s[i:i + 3]) for i in range(0, len(s), n)), key=sum)
    dark = px[:max(1, len(px) // 12)]
    return tuple(statistics.median(p[i] for p in dark) / 255 for i in range(3))


def advances(spans):
    """(character, width in ems) for neighbouring letters across sampled spans."""
    out = []
    for chars, size in spans:
        for a, b in zip(chars, chars[1:]):
            out.append((a["c"], math.dist(a["origin"], b["origin"]) / max(size, 0.1)))
    return out


def closest_by_metrics(spans, default, bold, italic):
    """The PDF's font is unusable and unknown: pick the installed font whose letter
    widths best reproduce the spacing seen on the page. Weight is never guessed
    from widths, as that would turn ordinary text bold."""
    pairs = [(c, seen) for c, seen in advances(spans) if not c.isspace()][:400]
    if len(pairs) < 6:
        return default
    best = (float("inf"), default)
    for family in EDITOR_FONTS:
        if _norm(family) not in FONTS.families:
            continue
        font = FONTS.load(family, bold, italic)
        err = sum(abs(font.glyph_advance(ord(c)) - seen) for c, seen in pairs) / len(pairs)
        if err < best[0]:
            best = (err, family)
    return best[1]


# ----------------------------------------------------------------- the document
class Document:
    def __init__(self, data: bytes, name: str, password: str | None = None):
        doc = fitz.open(stream=data, filetype="pdf")
        # "password": it needed one to open. "restrictions": it opened, but editing or copying was limited.
        self.unlocked = ("password" if doc.needs_pass
                         else "restrictions" if (doc.metadata or {}).get("encryption") else None)
        if doc.needs_pass:
            if not password or not doc.authenticate(password):
                doc.close()
                raise PasswordRequired()
        if doc.page_count == 0:
            raise EditError("This PDF has no pages.")
        self.name = name
        self.tmp = tempfile.mkdtemp(prefix="pdf_editor_")
        # Work on a decrypted copy: edits, undo and downloads need no password from here on.
        self.doc = fitz.open(stream=doc.tobytes(encryption=fitz.PDF_ENCRYPT_NONE), filetype="pdf")
        doc.close()
        # Rotated pages are normalised once so every coordinate means the same thing.
        for page in self.doc:
            if page.rotation:
                self._unrotate(page)
        self.undo_stack: list[str] = []
        self.redo_stack: list[str] = []
        self.revs = [1] * self.doc.page_count
        self.layouts: dict[tuple, dict] = {}
        self.embedded: dict[int, fitz.Font | None] = {}
        self.own_fonts: set[int] = set()
        self.recent_id: str | None = None
        self.saved = False
        self.counter = 0
        self.warnings: list[str] = []
        self.has_signature = any(
            w.field_type == fitz.PDF_WIDGET_TYPE_SIGNATURE and w.field_value
            for p in self.doc for w in (p.widgets() or []))

    def close(self):
        try:
            self.doc.close()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _unrotate(page):
        """Bake page rotation into the content, keeping the same visible area."""
        rot, mb, cb = page.rotation, page.mediabox, page.cropbox
        cropped = cb != mb
        if cropped:
            page.set_cropbox(mb)  # rotation removal miscalculates when a crop is set
        page.remove_rotation()
        if not cropped:
            return
        w, h = mb.width, mb.height
        x0, y0, x1, y1 = cb.x0 - mb.x0, cb.y0 - mb.y0, cb.x1 - mb.x0, cb.y1 - mb.y0
        # Crop boxes here are measured from the top-left corner.
        if rot == 90:
            box = (h - y1, x0, h - y0, x1)
        elif rot == 180:
            box = (w - x1, h - y1, w - x0, h - y0)
        else:
            box = (y0, w - x1, y1, w - x0)
        page.set_cropbox(fitz.Rect(box))

    # ---- state
    def info(self) -> dict:
        return {
            "name": self.name,
            "pages": [{"w": p.rect.width, "h": p.rect.height, "rev": self.revs[p.number]}
                      for p in self.doc],
            "canUndo": bool(self.undo_stack),
            "canRedo": bool(self.redo_stack),
            "fonts": [f for f in EDITOR_FONTS if _norm(f) in FONTS.families] or ["Arial"],
            "signed": self.has_signature,
            "unlocked": self.unlocked,
        }

    def _snapshot(self) -> str:
        self.counter += 1
        os.makedirs(self.tmp, exist_ok=True)  # Windows or a clean-up tool may have emptied the temp folder
        path = os.path.join(self.tmp, f"snap{self.counter}.pdf")
        self.doc.save(path, garbage=0, deflate=False)
        return path

    def _load(self, path: str):
        new = fitz.open(path)
        data_doc = fitz.open(stream=new.tobytes(), filetype="pdf")  # release the file handle
        new.close()
        self.doc.close()
        self.doc = data_doc
        self.layouts.clear()
        self.embedded.clear()

    def _bump_all(self):
        top = max(self.revs, default=0) + 1
        self.revs = [top + i for i in range(self.doc.page_count)]

    def run(self, op, touched_page: int | None = None):
        """Run an edit with an undo point; roll back fully if anything fails."""
        snap = self._snapshot()
        count_before = self.doc.page_count
        self.warnings = []
        try:
            result = op()
        except Exception:
            self._load(snap)
            self._bump_all()
            try:
                os.remove(snap)
            except OSError:
                pass
            raise
        self.undo_stack.append(snap)
        if len(self.undo_stack) > MAX_HISTORY:
            self._drop(self.undo_stack.pop(0))
        for path in self.redo_stack:
            self._drop(path)
        self.redo_stack.clear()
        if touched_page is None or self.doc.page_count != count_before:
            self._bump_all()
            self.layouts.clear()
        else:
            self.revs[touched_page] += 1
            self.layouts = {k: v for k, v in self.layouts.items() if k[0] != touched_page}
        return result

    @staticmethod
    def _drop(path):
        try:
            os.remove(path)
        except OSError:
            pass

    def _check_history(self, stack):
        if not os.path.exists(stack[-1]):
            self.undo_stack.clear()
            self.redo_stack.clear()
            raise EditError("The undo history was removed from this computer's temporary files, "
                            "so this step can't be undone. Your PDF is unchanged and you can keep editing.")

    def undo(self):
        if self.undo_stack:
            self._check_history(self.undo_stack)
            self.redo_stack.append(self._snapshot())
            self._load(self.undo_stack.pop())
            self._bump_all()

    def redo(self):
        if self.redo_stack:
            self._check_history(self.redo_stack)
            self.undo_stack.append(self._snapshot())
            self._load(self.redo_stack.pop())
            self._bump_all()

    def render(self, index: int, zoom: float) -> bytes:
        page = self._page(index)
        zoom = max(0.1, min(zoom, 6))
        return page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False, annots=True).tobytes("png")

    def export(self, compress=False, pages=None) -> bytes:
        # Standard: fonts are kept whole, so the same font can be reused when this file is
        # edited again later. Compressed: fonts are cut down to the letters used and photos
        # are resampled, for the smallest file.
        # Tidying renumbers objects, so it runs on a copy and never on the open document.
        clone = fitz.open(stream=self.doc.tobytes(garbage=0), filetype="pdf")
        try:
            if pages is not None:
                if not pages or any(not 0 <= i < clone.page_count for i in pages):
                    raise EditError("Those pages no longer exist.")
                clone.select(list(pages))
            if not compress:
                return clone.tobytes(garbage=3, deflate=True, deflate_fonts=True, deflate_images=False)
            for shrink in (lambda: clone.subset_fonts(),
                           lambda: clone.rewrite_images(dpi_threshold=200, dpi_target=150, quality=75)):
                try:
                    shrink()
                except Exception:
                    pass  # an unusual font or image simply stays as it is
            return clone.tobytes(garbage=4, deflate=True, deflate_fonts=True, deflate_images=True,
                                 clean=True, use_objstms=1)
        finally:
            clone.close()

    def _page(self, index: int) -> fitz.Page:
        if not 0 <= index < self.doc.page_count:
            raise EditError("That page no longer exists.")
        return self.doc[index]

    # ---- reading a page
    def _embedded_font(self, xref: int) -> fitz.Font | None:
        if xref not in self.embedded:
            font = None
            try:
                _, ext, _, buf = self.doc.extract_font(xref)
                if buf and ext and ext != "n/a":
                    font = fitz.Font(fontbuffer=buf)
            except Exception:
                font = None
            self.embedded[xref] = font
        return self.embedded[xref]

    def _match_embedded(self, candidates, span_font, spans) -> int:
        """Find the embedded font whose character map really matches this text.

        Subset fonts often have scrambled or missing maps, so a font is only
        trusted if its glyph widths reproduce the spacing seen on the page.
        All the page's text in that font is judged, not just one fragment."""
        def key(name):
            return _norm(strip_subset(name)).replace("regular", "")

        def weight(name):
            return (bool(re.search(r"bold|black|heavy", name, re.I)),
                    bool(re.search(r"italic|oblique", name, re.I)))
        target = key(span_font)
        printable = [c["c"] for chars, _ in spans for c in chars if not c["c"].isspace()]
        pairs = advances(spans)
        # Exact name first, then the same family in the same weight and slant.
        same_name = [c for c in candidates if key(c[1]) == target]
        same_family = [c for c in candidates if c not in same_name
                       and family_key(c[1]) == family_key(span_font) and weight(c[1]) == weight(span_font)]
        for xref, basefont in same_name + same_family:
            font = self._embedded_font(xref)
            if font is None or not printable:
                continue
            found = sum(1 for c in printable if font.has_glyph(ord(c)))
            if found / len(printable) < 0.97:
                continue
            good = sum(abs(font.glyph_advance(ord(c)) - seen) < 0.08 for c, seen in pairs)
            if not pairs or good / len(pairs) >= 0.7:
                return xref
        return 0

    def layout(self, index: int) -> dict:
        key = (index, self.revs[index])
        if key not in self.layouts:
            self.layouts[key] = self._build_layout(self._page(index), index)
        return self.layouts[key]

    def public_layout(self, index: int) -> dict:
        data = self.layout(index)
        return {
            "paras": [{k: v for k, v in p.items() if not k.startswith("_")} for p in data["paras"]],
            "images": [{k: v for k, v in i.items() if not k.startswith("_")} for i in data["images"]],
            "widgets": data["widgets"],
            "rev": self.revs[index],
        }

    def _raster_rects(self, page):
        """Text the eye cannot see: invisible OCR layers, or text under a scan."""
        out = []
        try:
            log = page.get_bboxlog()
        except Exception:
            return out
        texts = []
        for kind, box in log:
            r = fitz.Rect(box)
            if kind == "ignore-text":
                out.append(r)
            elif "text" in kind:
                texts.append(r)
            elif "image" in kind or "imgmask" in kind:
                for t in texts:
                    if r.contains(t):
                        out.append(t)
        return out

    def _build_layout(self, page, index) -> dict:
        raw = page.get_text("rawdict", flags=TEXT_FLAGS)
        candidates = [(f[0], f[3]) for f in page.get_fonts(full=True)]
        raster = self._raster_rects(page)
        paras = []
        style_cache: dict[tuple, dict] = {}
        fonts_seen: dict[str, list] = {}
        for block in raw["blocks"]:
            for line in block.get("lines", []) if block.get("type") == 0 else []:
                for span in line["spans"]:
                    fonts_seen.setdefault(span["font"], []).append((span["chars"], span["size"]))
        self._page_fonts = {name: self._analyse_font(name, spans, candidates, raw)
                            for name, spans in fonts_seen.items()}
        items, block_id = [], 0

        for block in raw["blocks"]:
            if block.get("type") != 0:
                continue
            block_items = []
            for line in block["lines"]:
                block_items.extend(self._line_items(line, candidates, style_cache))
            block_id += 1
            for item in self._merge_same_baseline(block_items):
                item["block"] = block_id
                items.append(item)
        for group in self._group_paragraphs(items):
            paras.append(self._make_para(group, len(paras), raster))

        self._detect_column_alignment(paras)
        return {"paras": paras, "images": self._images(page), "widgets": self._widgets(page)}

    @staticmethod
    def _detect_column_alignment(paras):
        """Numbers in a table are usually right-aligned, so an edited amount should
        grow to the left. Single lines reveal this by sharing only their right edge."""
        def numeric(p):
            text = "".join(r["text"] for r in p["runs"])
            return any(c.isdigit() for c in text) and sum(c.isalpha() for c in text) <= 3

        singles = [p for p in paras if p["lines"] == 1 and not p["angle"] and not p["rule"]]
        numbers = [p for p in singles if numeric(p)]
        for p in numbers:
            # Cells of equal width share both edges and say nothing. What counts is a
            # different-width number in the column, however far down the page it is.
            right = sum(1 for q in numbers if abs(q["x1"] - p["x1"]) < 1 and abs(q["x0"] - p["x0"]) >= 1)
            left = sum(1 for q in numbers if abs(q["x0"] - p["x0"]) < 1 and abs(q["x1"] - p["x1"]) >= 1)
            if right > left:
                p["align"] = "right"

    def _analyse_font(self, name, spans, candidates, raw):
        flags = 0
        for block in raw["blocks"]:
            for line in block.get("lines", []) if block.get("type") == 0 else []:
                for span in line["spans"]:
                    if span["font"] == name:
                        flags = span["flags"]
                        break
        bold = bool(flags & 16) or bool(re.search(r"bold|black|heavy", name, re.I))
        italic = bool(flags & 2) or bool(re.search(r"italic|oblique", name, re.I))
        xref = self._match_embedded(candidates, name, spans)
        mono = self._is_monospaced(flags, spans, xref)
        family, exact = FONTS.find_family(name, serif=bool(flags & 4), mono=mono)
        if not exact:
            family = closest_by_metrics(spans, family, bold, italic)
        return {"xref": xref, "mono": mono, "family": family, "exact": exact}

    def _style_for(self, span, candidates, cache):
        name = span["font"]
        flags = span["flags"]
        bold = bool(flags & 16) or bool(re.search(r"bold|black|heavy", name, re.I))
        italic = bool(flags & 2) or bool(re.search(r"italic|oblique", name, re.I))
        key = (name, round(span["size"], 2), span["color"], bold, italic, bool(flags & 1))
        if key not in cache:
            info = self._page_fonts[name]
            cache[key] = {
                "font": strip_subset(name), "xref": info["xref"], "family": info["family"],
                "exact": info["exact"], "size": round(span["size"], 2),
                "color": hex_of(span["color"]), "bold": bold, "italic": italic,
                "sup": bool(flags & 1), "mono": info["mono"],
            }
        return cache[key]

    def _is_monospaced(self, flags, spans, xref):
        if flags & 8:
            return True
        steps = sorted(seen for _, seen in advances(spans))
        if len(steps) >= 6:
            middle = steps[len(steps) // 2]
            if sum(abs(v - middle) < 0.02 for v in steps) / len(steps) > 0.95:
                return True
        font = self._embedded_font(xref) if xref else None
        if font is not None and font.has_glyph(ord("i")) and font.has_glyph(ord("W")):
            return abs(font.glyph_advance(ord("i")) - font.glyph_advance(ord("W"))) < 0.01
        return False

    def _line_items(self, line, candidates, cache):
        """Split a text line into fragments at wide gaps (table columns, tabs)."""
        dir_x, dir_y = line["dir"]
        angle = round(math.degrees(math.atan2(dir_y, dir_x)), 2)
        horizontal = abs(angle) < 0.5
        fragments, current, prev = [], [], None
        for span in line["spans"]:
            style = self._style_for(span, candidates, cache)
            for ch in span["chars"]:
                # A soft hyphen that is actually drawn on the page is a plain hyphen.
                c = "-" if ch["c"] in "­‐" else ch["c"]
                ch = dict(ch, c=c)
                if prev is not None and not c.isspace():
                    gap = (ch["origin"][0] - prev["bbox"][2]) if horizontal else 0
                    if gap > max(span["size"], prev["_s"]) * 2:
                        fragments.append(current)
                        current = []
                ch = dict(ch, _style=style, _s=span["size"])
                current.append(ch)
                if not c.isspace():
                    prev = ch
        if current:
            fragments.append(current)
        out = []
        for frag in fragments:
            while frag and frag[-1]["c"].isspace():
                frag.pop()
            while frag and frag[0]["c"].isspace():
                frag.pop(0)
            if not frag:
                continue
            xs0 = [c["bbox"][0] for c in frag]
            xs1 = [c["bbox"][2] for c in frag]
            ys0 = [c["bbox"][1] for c in frag]
            ys1 = [c["bbox"][3] for c in frag]
            sizes = [c["_s"] for c in frag]
            out.append({
                "chars": frag, "x0": min(xs0), "x1": max(xs1), "y0": min(ys0), "y1": max(ys1),
                "baseline": statistics.median(c["origin"][1] for c in frag),
                "origin": frag[0]["origin"], "size": statistics.median(sizes),
                "angle": angle, "horizontal": horizontal,
                "mono": frag[0]["_style"]["mono"],
                "rule": bool(RULE_LINE.fullmatch("".join(c["c"] for c in frag))),
            })
        return out

    @staticmethod
    def _merge_same_baseline(items):
        merged = []
        for it in items:
            last = merged[-1] if merged else None
            if (last and it["horizontal"] and last["horizontal"]
                    and abs(it["baseline"] - last["baseline"]) < 0.25 * it["size"]
                    and -0.5 < it["x0"] - last["x1"] < 0.6 * it["size"]):
                if it["x0"] - last["x1"] > 0.15 * it["size"]:
                    sp = dict(last["chars"][-1], c=" ", _synthetic=True)
                    last["chars"].append(sp)
                last["chars"].extend(it["chars"])
                last["x1"] = max(last["x1"], it["x1"])
                last["y0"] = min(last["y0"], it["y0"])
                last["y1"] = max(last["y1"], it["y1"])
            else:
                merged.append(it)
        return merged

    @staticmethod
    def _first_word_width(item):
        x = item["x0"]
        for c in item["chars"]:
            if c["c"].isspace():
                break
            x = c["bbox"][2]
        return x - item["x0"]

    def _group_paragraphs(self, items):
        """Join lines only when they are visibly one wrapped paragraph."""
        groups: list[list[dict]] = []
        for n, it in enumerate(items):
            group = groups[-1] if groups else None
            if group:
                # The right edge of this column, judged from the lines around it.
                last = group[-1]
                nearby = items[max(0, n - 12):n + 12]
                if it["block"] == last["block"]:
                    column = [q["x1"] for q in nearby if q["block"] == it["block"]]
                else:
                    column = [q["x1"] for q in nearby
                              if abs(q["x0"] - last["x0"]) < 3 * q["size"]
                              and abs(q["baseline"] - it["baseline"]) < 12 * q["size"]]
                right = max([last["x1"], *column])
            if group and self._continues(group, it, right):
                group.append(it)
            else:
                groups.append([it])
        return groups

    def _continues(self, group, it, block_right):
        prev = group[-1]
        if not (it["horizontal"] and prev["horizontal"]):
            return False
        if abs(it["size"] - prev["size"]) > 0.5:
            return False
        # Character rules are separators. Typewriter-style reports are laid out line
        # by line on a grid, so each of their lines stands alone.
        if it["rule"] or prev["rule"] or it["mono"] or prev["mono"]:
            return False
        step = it["baseline"] - prev["baseline"]
        if not 0.8 * it["size"] < step < 1.75 * it["size"]:
            return False
        if len(group) > 1:
            prior = prev["baseline"] - group[-2]["baseline"]
            if abs(step - prior) > 0.15 * it["size"]:
                return False
        if it["x0"] > prev["x1"] or it["x1"] < prev["x0"]:
            return False
        if re.match(r"^\s*([•\-–—*·▪◦]|\d{1,3}[.)])\s", "".join(c["c"] for c in it["chars"][:6])):
            return False
        # Some PDFs store every line separately: then only long lines count as wrapped text,
        # otherwise a list of short names or labels would be joined into one paragraph.
        if it["block"] != prev["block"] and prev["x1"] - prev["x0"] < 15 * it["size"]:
            return False
        # The previous line must have run out of room, as a wrapped line does.
        room = block_right - prev["x1"]
        word = self._first_word_width(it)
        centered = abs((it["x0"] + it["x1"]) - (prev["x0"] + prev["x1"])) < 2.5
        if word < room - 0.3 * it["size"] and not (centered and len(group) > 1):
            return False
        if centered:
            return True
        # Only a modest first-line indent or hanging indent counts as the same paragraph.
        limit = 3 * it["size"] if len(group) == 1 else 2
        return abs(it["x0"] - prev["x0"]) < limit

    def _make_para(self, group, pid, raster):
        first = group[0]
        x0 = min(i["x0"] for i in group)
        x1 = max(i["x1"] for i in group)
        y0 = min(i["y0"] for i in group)
        y1 = max(i["y1"] for i in group)

        # Runs: merge characters of the same style, joining lines with a space.
        runs, chars = [], []
        def push(text, style):
            if runs and runs[-1]["_style"] is style:
                runs[-1]["text"] += text
            else:
                runs.append({"text": text, "_style": style})
        for n, item in enumerate(group):
            for c in item["chars"]:
                push(" " if c["c"] == "\u00a0" else c["c"], c["_style"])
                if not c.get("_synthetic"):
                    chars.append(c["bbox"])
            if n + 1 < len(group):
                last = runs[-1]
                nxt = group[n + 1]["chars"][0]["c"]
                if last["text"].endswith("-") and last["text"][-2:-1].isalpha() and nxt.islower():
                    last["text"] = last["text"][:-1]  # undo end-of-line hyphenation
                else:
                    push(" ", item["chars"][-1]["_style"])

        align = "left"
        if len(group) > 1:
            lefts = [i["x0"] for i in group]
            rights = [i["x1"] for i in group]
            full_rights = rights[:-1]
            left_even = max(lefts[1:]) - min(lefts[1:]) < 2 if len(lefts) > 2 else abs(lefts[0] - lefts[1]) < 2
            right_even = max(full_rights) - min(full_rights) < 1.5
            centers = [(a + b) / 2 for a, b in zip(lefts, rights)]
            if max(centers) - min(centers) < 2 and max(lefts) - min(lefts) > 2:
                align = "center"
            elif max(rights) - min(rights) < 1.5 and max(lefts) - min(lefts) > 2:
                align = "right"
            elif left_even and right_even and len(group) > 2 and rights[-1] < max(full_rights) - 1:
                align = "justify"

        if len(group) > 1:
            steps = [b["baseline"] - a["baseline"] for a, b in zip(group, group[1:])]
            line_height = statistics.median(steps)
        else:
            line_height = max(y1 - y0, first["size"] * 1.15)

        rest_left = group[1]["x0"] if len(group) > 1 else first["x0"]
        if align == "left" and len(group) > 1:
            first_indent = first["x0"] - rest_left
        else:
            first_indent = 0
        left = min(first["x0"], rest_left) if align == "left" else x0

        # Judge by the letters themselves: a paragraph's middle can fall between lines.
        sample = chars[::max(1, len(chars) // 12)]
        hits = sum(any(r.contains(fitz.Point((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)) for r in raster)
                   for b in sample)
        is_raster = bool(sample) and hits * 2 >= len(sample)

        public_runs = []
        for r in runs:
            s = r["_style"]
            public_runs.append({"text": r["text"], **{k: s[k] for k in
                               ("font", "xref", "family", "exact", "size", "color", "bold", "italic", "sup")}})
        ox, oy = first["origin"]
        return {
            "id": pid, "x0": x0, "y0": y0, "x1": x1, "y1": y1,
            "left": left, "baseline": first["baseline"], "originX": ox, "originY": oy,
            "width": x1 - left, "lineHeight": line_height, "firstIndent": first_indent,
            "align": align, "angle": first["angle"], "wrap": len(group) > 1,
            "lines": len(group), "raster": is_raster, "runs": public_runs,
            "rule": first["rule"],
            "_chars": chars,
        }

    def _images(self, page):
        out = []
        counts: dict[int, int] = {}
        for info in page.get_image_info(xrefs=True):
            xref = info.get("xref", 0)
            nth = counts.get(xref, 0)
            counts[xref] = nth + 1
            r = fitz.Rect(info["bbox"])
            if r.is_empty or r.width < 2 or r.height < 2:
                continue
            area = (r & page.rect).get_area() / max(page.rect.get_area(), 1)
            out.append({
                "id": len(out), "x0": r.x0, "y0": r.y0, "x1": r.x1, "y1": r.y1,
                "background": area > 0.85,
                "_xref": xref, "_nth": nth, "_transform": tuple(info["transform"]),
            })
        return out

    def _widgets(self, page):
        out = []
        kinds = {
            fitz.PDF_WIDGET_TYPE_TEXT: "text", fitz.PDF_WIDGET_TYPE_CHECKBOX: "check",
            fitz.PDF_WIDGET_TYPE_RADIOBUTTON: "radio", fitz.PDF_WIDGET_TYPE_COMBOBOX: "choice",
            fitz.PDF_WIDGET_TYPE_LISTBOX: "choice",
        }
        for w in page.widgets() or []:
            kind = kinds.get(w.field_type)
            if not kind:
                continue
            r = w.rect
            out.append({
                "xref": w.xref, "kind": kind, "x0": r.x0, "y0": r.y0, "x1": r.x1, "y1": r.y1,
                "value": w.field_value if kind != "check" and kind != "radio"
                else w.field_value not in (False, "Off", None, ""),
                "options": [o if isinstance(o, str) else o[0] for o in (w.choice_values or [])],
                "multiline": bool(w.field_flags & 4096) if kind == "text" else False,
                "size": w.text_fontsize or 0,
            })
        return out

    # ---- writing text
    def _resolve_run(self, run):
        """Choose the font for a run: the PDF's own if it has every character,
        then the same family from Windows, then a close match."""
        bold, italic = bool(run.get("bold")), bool(run.get("italic"))
        text = run["text"]
        cps = [ord(c) for c in text if not c.isspace()]
        xref = int(run.get("xref") or 0)
        original = self._embedded_font(xref) if xref else None
        if original is not None and all(original.has_glyph(c) for c in cps):
            return original, False
        family = run.get("family") or "Arial"
        exact = bool(run.get("exact", True))
        system = FONTS.load(family, bold, italic)
        if original is not None and not exact:
            # Family not installed: keep the original look, borrow only missing letters.
            self.warnings.append(f"Some new letters are not in this PDF's font, so they use {family}.")
            return original, False
        if not exact:
            self.warnings.append(f"The original font can't be reused here, so {family}, the closest match, was used.")
        return system, False

    def _layout_lines(self, runs, width, first_indent, wrap):
        """Greedy word wrap. Returns lines of (char, font, advance, run)."""
        chars = []
        for run in runs:
            font, _ = self._resolve_run(run)
            size = float(run["size"])
            bold, italic = bool(run.get("bold")), bool(run.get("italic"))
            for ch in unicodedata.normalize("NFC", run["text"]):
                if ch == "\n":
                    chars.append(("\n", font, 0, run))
                    continue
                if ch in "\u00a0\t":
                    ch = " "
                f = font
                if not ch.isspace() and not font.has_glyph(ord(ch)):
                    similar = FONTS.load(run.get("family") or "Arial", bold, italic)
                    f = similar if similar.has_glyph(ord(ch)) else FONTS.fallback_for(ch, bold, italic) or font
                chars.append((ch, f, f.glyph_advance(ord(ch)) * size, run))

        lines, line, line_w, word_break = [], [], 0.0, None
        avail = lambda: width - (first_indent if not lines else 0)
        for item in chars:
            ch, _, adv, _ = item
            if ch == "\n":
                lines.append((line, True))
                line, line_w, word_break = [], 0.0, None
                continue
            if wrap and not ch.isspace() and line_w + adv > avail() + 0.25 and line:
                if word_break is not None:
                    head, tail = line[:word_break], line[word_break + 1:]
                    lines.append((head, False))
                    line, word_break = tail, None
                    line_w = sum(c[2] for c in line)
                else:
                    lines.append((line, False))
                    line, line_w = [], 0.0
            if ch == " ":
                word_break = len(line)
            line.append(item)
            line_w += adv
        lines.append((line, True))
        return lines

    def _write_runs(self, page, runs, *, left, baseline, width, line_height,
                    first_indent=0.0, align="left", wrap=True, angle=0.0, render_mode=0):
        runs = [r for r in runs if r["text"]]
        if not "".join(r["text"] for r in runs).strip():
            return
        if not page.is_wrapped:
            page.wrap_contents()

        all_text = "".join(r["text"] for r in runs)
        if is_complex(all_text) and render_mode == 0 and not angle:
            self._write_html(page, runs, left, baseline, width, line_height, align)
            return

        # The text writer measures from the opposite edge on unevenly cropped pages.
        mb, cb = page.mediabox, page.cropbox
        baseline -= mb.height - cb.y1 - cb.y0

        lines = self._layout_lines(runs, width, first_indent, wrap)
        # Kept in reading order: grouping by colour would scramble the text flow,
        # and the paragraph could no longer be recognised for the next edit.
        writers: list[tuple[str, fitz.TextWriter]] = []
        used_chars: dict[int, tuple] = {}
        pivot = fitz.Point(left, baseline)
        for n, (line, hard_end) in enumerate(lines):
            while line and line[-1][0].isspace():
                line = line[:-1]
            if not line:
                continue
            used = sum(c[2] for c in line)
            box_left = left + (first_indent if n == 0 else 0)
            box_width = width - (first_indent if n == 0 else 0)
            x, extra = box_left, 0.0
            if align == "center":
                x += (box_width - used) / 2
            elif align == "right":
                x += box_width - used
            elif align == "justify" and not hard_end:
                spaces = sum(1 for c in line if c[0] == " ")
                if spaces:
                    extra = max(0.0, box_width - used) / spaces
            y = baseline + n * line_height

            i = 0
            while i < len(line):
                ch, font, _, run = line[i]
                j, text, seg_w = i, "", 0.0
                # Spaces always end a segment when justifying so the gaps can stretch.
                while j < len(line) and line[j][1] is font and line[j][3] is run:
                    text += line[j][0]
                    seg_w += line[j][2]
                    j += 1
                    if extra and line[j - 1][0] == " ":
                        seg_w += extra
                        break
                rise = -0.33 * float(run["size"]) if run.get("sup") else 0
                color = run.get("color") or "#000000"
                if not writers or writers[-1][0] != color:
                    writers.append((color, fitz.TextWriter(page.rect)))
                writer = writers[-1][1]
                writer.append(fitz.Point(x, y + rise), text, font=font, fontsize=float(run["size"]))
                entry = used_chars.setdefault(id(font), (font, {}))
                for c in text:
                    entry[1].setdefault(font.has_glyph(ord(c)), ord(c))
                x += seg_w
                i = j

        before = {f[0] for f in page.get_fonts(full=True)}
        matrix = fitz.Matrix(-angle) if angle else None
        for color, writer in writers:
            kwargs = {"color": rgb_of(color), "render_mode": render_mode}
            if matrix is not None:
                kwargs["morph"] = (pivot, matrix)
            writer.write_text(page, **kwargs)
        self._fix_text_map(page, before, used_chars)

    def _fix_text_map(self, page, before, used_chars):
        """Fonts draw one shape for several characters (space and no-break space,
        hyphen and soft hyphen). Record the ones really typed so that copying and
        searching the saved PDF give back exactly this text."""
        for info in page.get_fonts(full=True):
            xref, basefont = info[0], strip_subset(info[3])
            if xref in before and xref not in self.own_fonts:
                continue
            for font, mapping in used_chars.values():
                if font.name != basefont or info[2] != "Type0":
                    continue
                kind, value = self.doc.xref_get_key(xref, "ToUnicode")
                if kind != "xref":
                    continue
                if xref not in self.own_fonts:
                    # The map is read only once per session, so settle every common
                    # character now, preferring the plain one when shapes are shared.
                    common = {}
                    for cp in reversed([*range(0x20, 0x250), *range(0x2000, 0x2123)]):
                        common[font.has_glyph(cp)] = cp
                    mapping = {**common, **mapping}
                    self.own_fonts.add(xref)
                stream_xref = int(value.split()[0])
                stream = self.doc.xref_stream(stream_xref)
                items = [(g, c) for g, c in sorted(mapping.items()) if g]
                extra = b""
                for i in range(0, len(items), 100):
                    chunk = items[i:i + 100]
                    body = b"".join(b"<%04x> <%s>\n" % (g, chr(c).encode("utf-16-be").hex().encode())
                                    for g, c in chunk)
                    extra += b"%d beginbfchar\n%sendbfchar\n" % (len(chunk), body)
                # Later entries take priority over the font's generic ranges.
                if b"endcmap" in stream:
                    self.doc.update_stream(stream_xref, stream.replace(b"endcmap", extra + b"endcmap", 1))

    def _write_html(self, page, runs, left, baseline, width, line_height, align):
        """Arabic, Hindi and other shaped scripts go through the HTML layout engine."""
        from html import escape
        size = max(float(r["size"]) for r in runs)
        fonts = {}
        spans = []
        for r in runs:
            family = r.get("family") or "Arial"
            if is_complex(r["text"]):
                family = "Nirmala UI" if any(0x0900 <= ord(c) <= 0x0DFF for c in r["text"]) else "Segoe UI"
            path = FONTS.file_for(family, bool(r.get("bold")), bool(r.get("italic")))
            face = f"f{len(fonts)}"
            if path:
                face = fonts.setdefault(path, face)
            spans.append(
                f'<span style="font-family:{face};font-size:{float(r["size"])}px;color:{r.get("color", "#000")};'
                f'font-weight:{"bold" if r.get("bold") else "normal"};'
                f'font-style:{"italic" if r.get("italic") else "normal"}">'
                f'{escape(r["text"]).replace(chr(10), "<br>")}</span>')
        css = "".join(f'@font-face{{font-family:{face};src:url("{os.path.basename(path)}")}}'
                      for path, face in fonts.items())
        css += f"body{{margin:0;line-height:{line_height}px;text-align:{align}}}"
        folder = os.path.dirname(next(iter(fonts), os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts", "x")))
        rect = fitz.Rect(left, baseline - size * 1.1, left + max(width, 40), page.rect.y1)
        page.insert_htmlbox(rect, "<div>" + "".join(spans) + "</div>", css=css,
                            archive=fitz.Archive(folder), overlay=True, scale_low=1)

    # ---- removing text
    @staticmethod
    def _glyph_boxes(page):
        """Every visible letter on the page, by position."""
        found = []
        for block in page.get_text("rawdict", flags=TEXT_FLAGS)["blocks"]:
            for line in block.get("lines", []) if block.get("type") == 0 else []:
                for span in line["spans"]:
                    for ch in span["chars"]:
                        if not ch["c"].isspace():
                            found.append(tuple(round(v, 1) for v in ch["bbox"]))
        return found

    def _remove_chars(self, page, boxes):
        """Delete exactly these glyphs from the content stream, leaving everything else.

        How much of a letter a removal mark must cover varies between PDFs, so marks
        start small, the result is checked, and only letters that remain are retried
        with a larger mark. New text is never printed over old text, and no other
        letter may be lost: if either can't be guaranteed, the edit is refused."""
        boxes = [tuple(b) for b in boxes]
        targets = {tuple(round(v, 1) for v in b) for b in boxes}
        before = self._glyph_boxes(page)
        others = [g for g in before if g not in targets]
        # Some fonts need a letter's full width covered. A shallow mark does that
        # without reaching the line above or below, even when lines overlap.
        for share_x, share_y in ((0.1, 0.1), (0.45, 0.45), (1, 0.3), (1, 0.5), (1.05, 0.6)):
            for b in boxes:
                cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
                w = max(0.05, (b[2] - b[0]) * share_x / 2)
                h = max(0.05, (b[3] - b[1]) * share_y / 2)
                page.add_redact_annot(fitz.Rect(cx - w, cy - h, cx + w, cy + h), fill=False, cross_out=False)
            if boxes:
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                      graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                                      text=fitz.PDF_REDACT_TEXT_REMOVE)
            remaining = set(self._glyph_boxes(page))
            boxes = [b for b in boxes if tuple(round(v, 1) for v in b) in remaining]
            if not boxes:
                break
        after = self._glyph_boxes(page)
        if boxes or len([g for g in after if g not in targets]) < len(others):
            raise EditError("This text could not be changed safely, so the PDF was left unchanged. "
                            "You can use Erase area and then Add text instead.")

    def _erase_region(self, page, rect, color=None):
        """Paint out an area and destroy what lies under it, images and text alike."""
        rect = fitz.Rect(rect) & page.rect
        if rect.is_empty:
            return
        color = color or sample_background(page, rect)
        page.add_redact_annot(rect, fill=color, cross_out=False)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS,
                              graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                              text=fitz.PDF_REDACT_TEXT_REMOVE)

    # ---- operations
    def replace_text(self, index, para_id, runs, align=None, dx=0.0, dy=0.0):
        def op():
            page = self._page(index)
            paras = self.layout(index)["paras"]
            if not 0 <= para_id < len(paras):
                raise EditError("That text has changed. Please click it again.")
            self._rewrite_para(page, paras[para_id], runs, align, dx, dy)
        return self.run(op, index)

    def _rewrite_para(self, page, p, runs, align=None, dx=0.0, dy=0.0):
        if True:
            rect = fitz.Rect(p["x0"], p["y0"], p["x1"], p["y1"])
            if p["raster"]:
                # Text inside a scan is pixels: erase them, then write real text.
                self._remove_chars(page, p["_chars"])
                self._erase_region(page, rect + (-1, -1, 1, 1))
            else:
                self._remove_chars(page, p["_chars"])
            cleaned = [dict(r, size=float(r.get("size") or 11)) for r in runs]
            if p["angle"]:
                # Slanted or vertical text: keep its start point and direction.
                reach = max(page.rect.width, page.rect.height) * 2
                self._write_runs(page, cleaned, left=p["originX"] + dx, baseline=p["originY"] + dy,
                                 width=reach, line_height=p["lineHeight"], wrap=False,
                                 angle=p["angle"])
                return
            width = p["width"]
            wrap = p["wrap"]
            if not wrap:
                # A single line may grow, but must not run off the page.
                width = max(page.rect.x1 - (p["left"] + dx) - 6, p["width"])
                wrap = True
            use_align = align or p["align"]
            if not p["wrap"] and use_align in ("center", "right"):
                width = p["width"]
                wrap = False
            self._write_runs(
                page, cleaned, left=p["left"] + dx, baseline=p["baseline"] + dy,
                width=width, line_height=p["lineHeight"], first_indent=p["firstIndent"],
                align=use_align, wrap=wrap, angle=p["angle"])

    # ---- find and replace
    @staticmethod
    def _matches(text, query, case):
        """Start positions of query in text. Lookalike spaces and hyphens count as the real ones."""
        def norm(t):
            t = t.replace("\xa0", " ").replace("\u00ad", "-")
            return t if case else "".join(c.lower() if len(c.lower()) == 1 else c for c in t)
        hay, needle = norm(text), norm(query)
        found, start = [], 0
        while needle:
            i = hay.find(needle, start)
            if i < 0:
                break
            found.append(i)
            start = i + len(needle)
        return found

    def find(self, query, case=False):
        hits = []
        if not query.strip():
            return hits
        for index in range(self.doc.page_count):
            page = self.doc[index]
            for p in self.layout(index)["paras"]:
                text = "".join(r["text"] for r in p["runs"])
                found = self._matches(text, query, case)
                if not found:
                    continue
                rect = fitz.Rect(p["x0"], p["y0"], p["x1"], p["y1"])
                loose = self._matches(text, query, False)
                boxes = page.search_for(query.replace("\xa0", " "), clip=rect + (-2, -2, 2, 2))
                for k, pos in enumerate(found):
                    j = loose.index(pos) if pos in loose else -1
                    box = boxes[j] if 0 <= j < len(boxes) and len(boxes) == len(loose) else rect
                    hits.append({"page": index, "x0": box.x0, "y0": box.y0, "x1": box.x1, "y1": box.y1,
                                 "para": [p["x0"], p["y0"]], "k": k})
        return hits

    def _replace_runs(self, runs, query, replacement, case, only=None):
        text = "".join(r["text"] for r in runs)
        found = self._matches(text, query, case)
        if only is not None:
            found = found[only:only + 1]
        if not found:
            return None, 0
        owner = [i for i, r in enumerate(runs) for _ in r["text"]]
        out, pos, starts = [], 0, set(found)
        while pos < len(text):
            if pos in starts:
                out += [(owner[pos], c) for c in replacement]  # takes the style where the match began
                pos += len(query)
            else:
                out.append((owner[pos], text[pos]))
                pos += 1
        new = []
        for i, c in out:
            if new and new[-1][0] == i:
                new[-1][1].append(c)
            else:
                new.append((i, [c]))
        return [dict(runs[i], text="".join(cs)) for i, cs in new], len(found)

    def _fresh_paras(self, index):
        self.layouts = {k: v for k, v in self.layouts.items() if k[0] != index}
        return self.layout(index)["paras"]

    def replace(self, query, replacement, case=False, target=None):
        """Replace every match, or only the one named by target ({page, para, k} from find)."""
        if not query:
            raise EditError("Type the text to find first.")

        def op():
            count = 0
            pages = [int(target["page"])] if target else range(self.doc.page_count)
            for index in pages:
                page = self._page(index)
                if target:
                    todo = [tuple(target["para"])]
                else:
                    todo = [(p["x0"], p["y0"]) for p in self._fresh_paras(index)
                            if self._matches("".join(r["text"] for r in p["runs"]), query, case)]
                for x0, y0 in todo:
                    p = next((q for q in self._fresh_paras(index)
                              if abs(q["x0"] - x0) < 1.5 and abs(q["y0"] - y0) < 1.5), None)
                    if p is None:
                        continue
                    runs, n = self._replace_runs(p["runs"], query, replacement, case,
                                                 int(target["k"]) if target else None)
                    if n:
                        self._rewrite_para(page, p, runs)
                        count += n
            if not count:
                raise EditError("That text has changed. Please search again." if target
                                else "Nothing matching was found to replace.")
            return count
        return self.run(op, int(target["page"]) if target else None)

    def add_text(self, index, x, y, width, runs, align="left"):
        def op():
            page = self._page(index)
            size = max(float(r.get("size") or 12) for r in runs)
            bold = bool(runs[0].get("bold"))
            italic = bool(runs[0].get("italic"))
            font = FONTS.load(runs[0].get("family") or "Arial", bold, italic)
            baseline = y + font.ascender * size
            w = float(width) if width else page.rect.x1 - x - 6
            self._write_runs(page, runs, left=x, baseline=baseline, width=w,
                             line_height=size * 1.2, align=align, wrap=True)
        return self.run(op, index)

    def _image_edit_stream(self, page, image, replacement):
        """Rewrite one image's drawing command in the page content, keeping its
        position in the stacking order. Returns False if it is not reachable."""
        xref = image["_xref"]
        if not xref:
            return False
        names = [i[7] for i in page.get_images(full=True) if i[0] == xref and i[9] == 0]
        if not names:
            return False
        page.clean_contents()
        contents = page.get_contents()
        if not contents:
            return False
        stream = self.doc.xref_stream(contents[0])
        pattern = re.compile(rb"/" + re.escape(names[0].encode()) + rb"\s+Do\b")
        matches = list(pattern.finditer(stream))
        if image["_nth"] >= len(matches):
            return False
        m = matches[image["_nth"]]
        new = replacement(m.group(0))
        self.doc.update_stream(contents[0], stream[:m.start()] + new + stream[m.end():])
        return True

    def _get_image(self, index, image_id):
        images = self.layout(index)["images"]
        if not 0 <= image_id < len(images):
            raise EditError("That image has changed. Please click it again.")
        return images[image_id]

    def move_image(self, index, image_id, rect):
        new_rect = fitz.Rect(rect)
        if new_rect.is_empty:
            raise EditError("The image would have no size.")
        def op():
            page = self._page(index)
            image = self._get_image(index, image_id)
            old = fitz.Rect(image["x0"], image["y0"], image["x1"], image["y1"])
            sx, sy = new_rect.width / old.width, new_rect.height / old.height
            # T moves the old box to the new one in page space. Pulled back through
            # the image's own matrix it becomes a local 'cm' valid for any rotation.
            T = fitz.Matrix(sx, 0, 0, sy, new_rect.x0 - old.x0 * sx, new_rect.y0 - old.y0 * sy)
            C = fitz.Matrix(1, 0, 0, -1, 0, 1) * fitz.Matrix(image["_transform"])
            if abs(C.a * C.d - C.b * C.c) < 1e-9:
                raise EditError("This image cannot be moved.")
            M = C * T * ~C
            cm = f"q {M.a:.6f} {M.b:.6f} {M.c:.6f} {M.d:.6f} {M.e:.6f} {M.f:.6f} cm ".encode()
            if not self._image_edit_stream(page, image, lambda cmd: cm + cmd + b" Q"):
                self._reinsert_image(page, image, old, new_rect)
        return self.run(op, index)

    def _reinsert_image(self, page, image, old, new_rect):
        pix = page.get_pixmap(matrix=fitz.Matrix(3, 3), clip=old, alpha=False)
        png = pix.tobytes("png")
        self._erase_region(page, old)
        page.insert_image(new_rect, stream=png, keep_proportion=False)

    def delete_image(self, index, image_id):
        def op():
            page = self._page(index)
            image = self._get_image(index, image_id)
            if not self._image_edit_stream(page, image, lambda cmd: b""):
                self._erase_region(page, fitz.Rect(image["x0"], image["y0"], image["x1"], image["y1"]))
        return self.run(op, index)

    def replace_image(self, index, image_id, data):
        def op():
            page = self._page(index)
            image = self._get_image(index, image_id)
            rect = fitz.Rect(image["x0"], image["y0"], image["x1"], image["y1"])
            if not self._image_edit_stream(page, image, lambda cmd: b""):
                self._erase_region(page, rect)
            if not page.is_wrapped:
                page.wrap_contents()
            page.insert_image(rect, stream=data, keep_proportion=True)
        return self.run(op, index)

    def add_image(self, index, rect, data):
        def op():
            page = self._page(index)
            if not page.is_wrapped:
                page.wrap_contents()
            page.insert_image(fitz.Rect(rect), stream=data, keep_proportion=False)
        return self.run(op, index)

    def erase(self, index, rect):
        return self.run(lambda: self._erase_region(self._page(index), fitz.Rect(rect)), index)

    def set_form(self, index, xref, value):
        def op():
            for w in self._page(index).widgets() or []:
                if w.xref == xref:
                    w.field_value = value
                    w.update()
                    return
            raise EditError("That form field could not be found.")
        return self.run(op, index)

    # ---- moving and copying an area
    def _collect_area(self, page, index, rect):
        """Everything inside an area, as real objects where that is possible."""
        self._build_layout(page, index)  # readies this page's font analysis
        candidates = [(f[0], f[3]) for f in page.get_fonts(full=True)]
        raster = self._raster_rects(page)
        area = {"pieces": [], "boxes": [], "images": [], "drawings": [], "pixels": False}
        cache: dict[tuple, dict] = {}

        def inside(b):
            return rect.contains(fitz.Point((b[0] + b[2]) / 2, (b[1] + b[3]) / 2))

        for block in page.get_text("rawdict", flags=TEXT_FLAGS)["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block["lines"]:
                for item in self._line_items(line, candidates, cache):
                    piece = None
                    for c in item["chars"]:
                        if not inside(c["bbox"]):
                            piece = None
                            continue
                        centre = fitz.Point((c["bbox"][0] + c["bbox"][2]) / 2, (c["bbox"][1] + c["bbox"][3]) / 2)
                        if not c["c"].isspace() and any(r.contains(centre) for r in raster):
                            area["pixels"] = True  # letters that are really part of a scan
                            continue
                        area["boxes"].append(c["bbox"])
                        if piece is None or piece["style"] is not c["_style"]:
                            if c["c"].isspace():
                                continue
                            piece = {"text": "", "x": c["origin"][0], "y": c["origin"][1],
                                     "angle": item["angle"], "style": c["_style"]}
                            area["pieces"].append(piece)
                        piece["text"] += " " if c["c"] == "\u00a0" else c["c"]

        for image in self._images(page):
            r = fitz.Rect(image["x0"], image["y0"], image["x1"], image["y1"])
            overlap = (r & rect).get_area() / max(r.get_area(), 0.01)
            if overlap < 0.02:
                continue
            a, b, c, d = image["_transform"][:4]
            plain = abs(b) < 1e-3 and abs(c) < 1e-3 and a > 0 and d > 0 and image["_xref"]
            if overlap >= 0.98 and plain:
                area["images"].append((image, r))
            else:
                area["pixels"] = True  # part of a picture, or one that can't be redrawn exactly

        margin = rect + (-1, -1, 1, 1)
        for path in page.get_drawings():
            if margin.contains(path["rect"]) and not fitz.Rect(path["rect"]).is_infinite:
                area["drawings"].append(path)
        return area

    @staticmethod
    def _redraw(page, path, dx, dy):
        shift = fitz.Matrix(1, 0, 0, 1, dx, dy)
        shape = page.new_shape()
        for item in path["items"]:
            kind = item[0]
            if kind == "l":
                shape.draw_line(item[1] * shift, item[2] * shift)
            elif kind == "re":
                shape.draw_rect(fitz.Rect(item[1]) * shift)
            elif kind == "qu":
                shape.draw_quad(fitz.Quad(item[1]) * shift)
            elif kind == "c":
                shape.draw_bezier(item[1] * shift, item[2] * shift, item[3] * shift, item[4] * shift)
        caps = path.get("lineCap") or (0,)
        shape.finish(
            fill=path.get("fill"), color=path.get("color"), dashes=path.get("dashes"),
            even_odd=bool(path.get("even_odd")), closePath=bool(path.get("closePath")),
            lineJoin=int(path.get("lineJoin") or 0), lineCap=max(caps),
            width=path.get("width") or 0, fill_opacity=path.get("fill_opacity") or 1,
            stroke_opacity=path.get("stroke_opacity") or 1)
        shape.commit()

    def transfer_area(self, index, rect, to_index, x, y, move=True):
        """Copy or move what lies in an area, to any place on any page of this PDF."""
        rect = fitz.Rect(rect) & self._page(index).rect
        if rect.is_empty or rect.width < 2 or rect.height < 2:
            raise EditError("Please drag a larger area.")
        target = self._page(to_index).rect
        if x < 0 or y < 0 or x + rect.width > target.width + 1 or y + rect.height > target.height + 1:
            raise EditError("The area would not fit on that page at this position.")

        def op():
            page, dest = self._page(index), self._page(to_index)
            dx, dy = x - rect.x0, y - rect.y0
            area = self._collect_area(page, index, rect)

            if area["pixels"]:
                # A scan, or part of a picture: only pixels can be taken from it.
                png = page.get_pixmap(matrix=fitz.Matrix(3, 3), clip=rect, alpha=False, annots=False).tobytes("png")
                if move:
                    self._erase_region(page, rect)
                if not dest.is_wrapped:
                    dest.wrap_contents()
                dest.insert_image(fitz.Rect(x, y, x + rect.width, y + rect.height), stream=png, keep_proportion=False)
                self.warnings.append("This area contains a scan or part of a picture, so it was moved as a picture "
                                     "and its text can't be edited directly.")
                return

            if not (area["pieces"] or area["images"] or area["drawings"]):
                raise EditError("There is nothing in that area to move or copy.")

            if move:
                self._remove_chars(page, area["boxes"])
                if area["drawings"]:
                    page.add_redact_annot(rect + (-1.5, -1.5, 1.5, 1.5), fill=False, cross_out=False)
                    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                          graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                                          text=fitz.PDF_REDACT_TEXT_NONE)
                # Last placements first, so that counting them stays valid.
                for image, _ in sorted(area["images"], key=lambda i: -i[0]["_nth"]):
                    if not self._image_edit_stream(page, image, lambda cmd: b""):
                        raise EditError("An image in that area can't be moved. Use Copy here instead.")

            if not dest.is_wrapped:
                dest.wrap_contents()
            for path in area["drawings"]:
                self._redraw(dest, path, dx, dy)
            for image, r in area["images"]:
                dest.insert_image(r + (dx, dy, dx, dy), xref=image["_xref"], keep_proportion=False)
            reach = max(dest.rect.width, dest.rect.height) * 2
            for piece in area["pieces"]:
                text = piece["text"].rstrip()
                if not text:
                    continue
                run = dict(piece["style"], text=text, sup=False)
                self._write_runs(dest, [run], left=piece["x"] + dx, baseline=piece["y"] + dy, width=reach,
                                 line_height=run["size"] * 1.2, wrap=False, angle=piece["angle"])
            self.warnings = sorted(set(self.warnings))
        return self.run(op, None)

    def rotate_page(self, index, degrees=90):
        def op():
            page = self._page(index)
            page.set_rotation(degrees)
            self._unrotate(page)
        return self.run(op, None)

    def delete_page(self, index):
        if self.doc.page_count <= 1:
            raise EditError("A PDF must keep at least one page.")
        return self.run(lambda: self.doc.delete_page(index), None)

    def insert_page(self, index):
        def op():
            ref = self._page(min(index, self.doc.page_count - 1)).rect
            self.doc.new_page(index + 1, width=ref.width, height=ref.height)
        return self.run(op, None)

    def arrange(self, order):
        """New page order as old page numbers: repeat one to duplicate it, leave one out to delete it."""
        n = self.doc.page_count
        order = [int(i) for i in order]
        if not order:
            raise EditError("A PDF must keep at least one page.")
        if any(not 0 <= i < n for i in order):
            raise EditError("The pages have changed. Please try again.")
        return self.run(lambda: self.doc.select(order), None)

    def rotate_pages(self, indices, degrees=90):
        if not indices or any(not 0 <= int(i) < self.doc.page_count for i in indices):
            raise EditError("Those pages no longer exist.")

        def op():
            for i in sorted({int(i) for i in indices}):
                page = self.doc[i]
                page.set_rotation(degrees)
                self._unrotate(page)
        return self.run(op, None)

    def merge(self, data, at, password=None, name="That file"):
        """Insert all pages of another PDF before page number `at` (0 = at the start)."""
        try:
            other = fitz.open(stream=data, filetype="pdf")
        except Exception as exc:
            raise EditError(f"{name} could not be read as a PDF.") from exc
        if other.needs_pass and (not password or not other.authenticate(password)):
            other.close()
            raise PasswordRequired()
        clean = fitz.open(stream=other.tobytes(encryption=fitz.PDF_ENCRYPT_NONE), filetype="pdf")
        other.close()
        count = clean.page_count
        if not count:
            raise EditError(f"{name} has no pages.")
        at = max(0, min(int(at), self.doc.page_count))

        def op():
            self.doc.insert_pdf(clean, start_at=at)
            for i in range(at, at + count):
                if self.doc[i].rotation:
                    self._unrotate(self.doc[i])
        try:
            self.run(op, None)
        finally:
            clean.close()
        return count

    def ocr(self, index):
        """Recognise text in a page's picture and add it as an invisible text layer,
        which then behaves like any other editable text on a scan."""
        from ocr import recognise

        def op():
            page = self._page(index)
            zoom = 3
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False, colorspace=fitz.csRGB)
            found = recognise(pix)
            added = 0
            for text, (x0, y0, x1, y1) in found:
                rect = fitz.Rect(x0 / zoom, y0 / zoom, x1 / zoom, y1 / zoom)
                if rect.is_empty or not text.strip():
                    continue
                arial = FONTS.load("Arial")
                natural = arial.text_length(text, 1) or 1
                size = min(rect.height * 0.82, rect.width / natural * 1.02)
                size = max(size, 3)
                baseline = rect.y1 - rect.height * 0.2
                ink = sample_ink(page, rect)
                self._write_runs(
                    page,
                    [{"text": text, "font": "Arial", "family": "Arial", "size": size,
                      "color": hex_of(ink), "xref": 0}],
                    left=rect.x0, baseline=baseline, width=page.rect.x1 - rect.x0,
                    line_height=size * 1.2, wrap=False, render_mode=3)
                added += 1
            return added
        return self.run(op, index)
