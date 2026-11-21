"""Builds varied PDFs that stress the editor: different producers, fonts and quirks."""
import os
import sys

import fitz

OUT = sys.argv[1]
os.makedirs(OUT, exist_ok=True)
LOREM = ("The quick brown fox jumps over the lazy dog while the committee reviews every "
         "invoice submitted during the third quarter of the financial year and confirms "
         "that all amounts were reported correctly to the tax authority before the deadline.")


def picture(w=240, h=160, rgb=(40, 110, 200)):
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, w, h), False)
    pix.set_rect(pix.irect, rgb)
    pix.set_rect(fitz.IRect(0, 0, w // 2, h // 2), (240, 180, 40))
    return pix.tobytes("png")


# 1. reportlab: base-14 fonts (not embedded), paragraphs, table, image, tight lines
def reportlab_doc():
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.utils import ImageReader
    import io
    from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    body = ParagraphStyle("b", fontName="Times-Roman", fontSize=11, leading=12, alignment=4)
    left = ParagraphStyle("l", fontName="Helvetica", fontSize=10, leading=13)
    head = ParagraphStyle("h", fontName="Helvetica-Bold", fontSize=18, leading=22, textColor=colors.HexColor("#123a7a"))
    table = Table([["Item", "Qty", "Amount"], ["Consulting", "3", "1,200.00"], ["Licences", "12", "480.00"]],
                  colWidths=[220, 60, 90])
    table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#123a7a")),
                               ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                               ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                               ("ALIGN", (1, 0), (-1, -1), "RIGHT")]))
    SimpleDocTemplate(f"{OUT}/reportlab.pdf", pagesize=A4).build([
        Paragraph("Quarterly Report", head), Spacer(1, 8),
        Paragraph(LOREM + " " + LOREM, body), Spacer(1, 8),
        Paragraph("A sentence with <b>bold words</b> and <i>italic words</i> and <font color='#c00000'>red "
                  "words</font> inside it, long enough that it needs to wrap on to a second line of text.", left),
        Spacer(1, 10), table, Spacer(1, 10),
        Image(io.BytesIO(picture()), width=180, height=120),
    ])


# 2. PyMuPDF: embedded full TrueType font on a coloured background
def embedded_doc():
    doc = fitz.open()
    page = doc.new_page()
    page.draw_rect(fitz.Rect(40, 40, 560, 200), fill=(1, 0.95, 0.75), color=None)
    box = fitz.Rect(50, 50, 550, 190)
    page.insert_textbox(box, LOREM, fontsize=12, fontname="F0",
                        fontfile=r"C:\Windows\Fonts\georgia.ttf", color=(0.2, 0.1, 0.4))
    page.insert_image(fitz.Rect(50, 250, 250, 383), stream=picture())
    page.insert_image(fitz.Rect(300, 250, 400, 317), stream=picture(rgb=(200, 40, 90)))
    page.insert_text((50, 450), "Stacked caption over nothing", fontsize=14, fontname="helv")
    doc.save(f"{OUT}/embedded.pdf")


# 3. Rotated page, offset crop box, rotated text
def quirks_doc():
    doc = fitz.open()
    p = doc.new_page()
    p.insert_text((72, 100), "This page is stored rotated by ninety degrees", fontsize=14)
    p.insert_image(fitz.Rect(72, 150, 272, 283), stream=picture())
    p.set_rotation(90)
    p = doc.new_page(width=800, height=800)
    p.insert_text((150, 200), "Cropped page text sits here", fontsize=14)
    p.insert_image(fitz.Rect(150, 250, 350, 383), stream=picture())
    p.set_cropbox(fitz.Rect(100, 100, 700, 700))
    p = doc.new_page()
    p.insert_text((100, 400), "Slanted line of text", fontsize=16, rotate=90)
    p.insert_text((300, 400), "Second vertical line", fontsize=16, rotate=270)
    doc.save(f"{OUT}/quirks.pdf")
    doc = fitz.open()
    for rot in (90, 180, 270):
        p = doc.new_page(width=700, height=500)
        p.draw_rect(fitz.Rect(0, 0, 700, 500), fill=(0.9, 0.9, 1))
        p.insert_text((120, 150), f"Rotated {rot} with an uneven crop", fontsize=16)
        p.insert_image(fitz.Rect(120, 180, 320, 313), stream=picture())
        p.set_cropbox(fitz.Rect(60, 40, 500, 420))
        p.set_rotation(rot)
    doc.save(f"{OUT}/rotcrop.pdf")
    doc = fitz.open()
    p = doc.new_page(width=700, height=500)
    p.insert_text((120, 150), "Uneven crop level text", fontsize=16)
    p.insert_image(fitz.Rect(120, 180, 320, 313), stream=picture())
    p.set_cropbox(fitz.Rect(60, 40, 500, 420))
    doc.save(f"{OUT}/crop_uneven.pdf")


# 4. Image-only scan, and scan with an invisible OCR layer
def scan_docs():
    src = fitz.open()
    p = src.new_page()
    p.insert_text((72, 100), "Scanned Invoice Number 4471", fontsize=20)
    p.insert_textbox(fitz.Rect(72, 130, 520, 260), LOREM, fontsize=13)
    pix = p.get_pixmap(dpi=200, alpha=False)
    doc = fitz.open()
    q = doc.new_page(width=p.rect.width, height=p.rect.height)
    q.insert_image(q.rect, stream=pix.tobytes("jpeg", jpg_quality=85))
    doc.save(f"{OUT}/scan.pdf")
    q.insert_text((72, 100), "Scanned Invoice Number 4471", fontsize=20, render_mode=3)
    doc.save(f"{OUT}/scan_ocr_layer.pdf")


# 5. Form fields, password
def form_doc():
    doc = fitz.open()
    p = doc.new_page()
    p.insert_text((72, 90), "Application form", fontsize=16)
    t = fitz.Widget()
    t.field_name, t.field_type, t.rect, t.field_value = "name", fitz.PDF_WIDGET_TYPE_TEXT, fitz.Rect(72, 110, 300, 135), "Old value"
    p.add_widget(t)
    c = fitz.Widget()
    c.field_name, c.field_type, c.rect = "agree", fitz.PDF_WIDGET_TYPE_CHECKBOX, fitz.Rect(72, 150, 90, 168)
    p.add_widget(c)
    doc.save(f"{OUT}/form.pdf")
    plain = fitz.open()
    plain.new_page().insert_text((72, 90), "Secret document text", fontsize=14)
    plain.save(f"{OUT}/locked.pdf", encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="abc123", owner_pw="own")
    # Opens without a password, but only printing is allowed.
    plain.save(f"{OUT}/restricted.pdf", encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="own", permissions=fitz.PDF_PERM_PRINT)


# 6. Fixed-layout bank-style report: typewriter font, small and tightly spaced, with rules
def report_doc():
    rows = [
        (40, "TO:"), (47, "MRS.SAMPLE NAME"), (47, "OLDTOWN CITY"), (47, "AT-SOME STREET DISTRICT"),
        (47, ""), (47, "CITYNAME"), (47, "STATENAME"), (47, "COUNTRY - 751002"),
        (20, "-" * 130),
        (30, "Account No. : 12345678901234"),
        (30, "Scheme      : 78255            Last Rephased on :   02-12-2025       Schedule Number :  01"),
        (20, "-" * 130),
        (30, "Upfront installment Principal Amount     : 0"),
        (30, "Upfront Installment Interest Amount      : 0"),
        (30, "No of Upfront Installments               : 0"),
    ]
    doc = fitz.open()
    p = doc.new_page()
    for n, (x, line) in enumerate(rows):
        if line:
            p.insert_text((x, 60 + n * 6.8), line, fontsize=6, fontname="F0",
                          fontfile=r"C:\Windows\Fonts\cour.ttf")
    doc.subset_fonts()
    doc.save(f"{OUT}/report.pdf", garbage=3)


# 7. Several pages with text, shapes and pictures, to move parts between them
def area_doc():
    doc = fitz.open()
    p = doc.new_page()
    p.insert_text((72, 100), "Keep this heading", fontsize=14, fontname="helv")
    p.draw_rect(fitz.Rect(70, 200, 300, 330), color=(0, 0.4, 0.2), fill=(0.9, 1, 0.9), width=1)
    p.insert_text((80, 225), "Row one value 100", fontsize=10, fontname="F0", fontfile=r"C:\Windows\Fonts\georgia.ttf")
    p.insert_text((80, 245), "Row two value 200", fontsize=10, fontname="tibo", color=(0.7, 0, 0))
    p.draw_line(fitz.Point(80, 255), fitz.Point(290, 255), color=(0, 0, 1), width=2)
    p.insert_image(fitz.Rect(80, 262, 180, 325), stream=picture())
    p.insert_text((72, 500), "Stays behind on page one", fontsize=12, fontname="helv")
    q = doc.new_page()
    q.insert_text((72, 100), "Second page heading", fontsize=14, fontname="helv")
    q.insert_text((72, 700), "Second page footer", fontsize=9, fontname="helv")
    r = doc.new_page(width=700, height=500)
    r.insert_text((120, 150), "Third page cropped", fontsize=12, fontname="helv")
    r.set_cropbox(fitz.Rect(60, 40, 500, 420))
    doc.save(f"{OUT}/area.pdf")


reportlab_doc()
area_doc()
report_doc()
embedded_doc()
quirks_doc()
scan_docs()
form_doc()
print(sorted(os.listdir(OUT)))
