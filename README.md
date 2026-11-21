# PDF Editor

Edit any PDF on your own computer. Nothing is uploaded anywhere.

## Start it

Double-click **Start PDF Editor.bat**. It opens in your browser. Keep the black window open while you work, and close it when you finish. Starting it again while it's running just opens the running copy.

### With Docker

```bash
docker run -d --name pdf-editor -p 127.0.0.1:8765:8765 -v pdf-editor-data:/data sandeep934/pdf-editor
```

Then open http://localhost:8765. Saved PDFs are kept in the `pdf-editor-data` volume. Or build and run it from this folder with `docker compose up -d`.

- **Keep `127.0.0.1:` in the port mapping.** The editor has no sign-in, so it must only be reachable from your own computer.
- **Fonts:** the image uses free fonts with the same letter widths as Arial, Times New Roman, Courier New, Calibri and Cambria. Other fonts are replaced by the closest one.
- **Settings:** `PDF_EDITOR_PORT` (default 8765), `PDF_EDITOR_DATA` (default `/data`) and `PDF_EDITOR_ALLOWED_HOSTS`, a comma-separated list of extra host names allowed in the address bar.

## What you can do

| To do this | Do this |
|---|---|
| Change existing text | Click the text and type. Click **Done** when finished. |
| Style some words | Select them while editing, then use the font, size, B, I and colour controls. |
| Move text | While editing, drag the blue bar above the box. |
| Add new text | Choose **Add text**, then click on the page. Drag instead to set the width. |
| Add a photo | Choose **Add photo**, drop an image on the page, or paste with Ctrl+V. It is selected straight away. |
| Move or resize an image | In **Edit**, click it, then drag it or its handles, or type a width and height in the bar at the top. |
| Align an image | Click it, then choose Left, Centre, Right, Top, Middle or Bottom. |
| Replace or delete an image | Click it, then choose **Replace** or **Delete**. |
| Edit a scanned page | Click **Make scanned text editable** on the page, then click the text. |
| Move or copy part of a page | Choose **Select area** and drag around it. Drag the box to its new place, or pick a page and an alignment, then click **Move here** or **Copy here**. Arrow keys nudge it. |
| Remove anything | Choose **Erase area** and drag over it. |
| Fill in a form | Click the field. |
| Rotate, insert or delete pages | Hover over a thumbnail on the left, or use **Pages**. |
| Organise pages | **Pages** shows every page. Drag to reorder; select pages to rotate, duplicate, delete, **Extract** (one new PDF) or **Split** (one PDF per page, in a zip). **Add PDF** merges another PDF in. |
| Find and replace | **Find** or Ctrl+F. Type a word to highlight every match, then **Replace this one** or **Replace all**. |
| Sign | **Sign**: draw, type or upload a signature, or add today's date, then click on the page. Signatures can be remembered on this computer. |
| Get the file | **Download** puts `yourfile_edited.pdf` in your Downloads folder. Choose **Compressed** for the smallest file. Your original is not changed. |
| Edit a locked PDF | Open it and enter its password once. The edited copy opens without one. |
| Keep a PDF to edit later | Click **Save**. It appears under **Saved PDFs**, where you can open, rename, download or delete it. |
| Edit several PDFs at once | Each PDF opens in its own tab. Use **+**, or choose or drop several files. Drag tabs to reorder them. |
| Go back to the start | **Home** shows everything you've been working on, under **Continue editing**. |

Shortcuts: Ctrl+Z undo, Ctrl+Y redo, Ctrl+S save, Ctrl+Enter finish editing, Esc cancel, Ctrl+K find any action. Light and dark themes are switched from the left rail.

## Good to know

- **Saved work:** every edit is kept automatically, even if you close the editor without clicking Save. PDFs you clicked **Save** on stay until you delete them. Others are listed under **Continue editing**, which keeps the last 12. Later changes update the saved copy too, so there is one copy to keep track of. To keep a version as it is now, click **Download**.
- **Where it's stored:** on this computer in `%LOCALAPPDATA%\PDF Editor\recent`, outside OneDrive. Deleting one there removes that copy only, not files you downloaded. Undo history lasts until a tab is closed.
- **File size:** standard downloads keep their fonts whole so they stay fully editable, which can make them larger when new fonts were added. **Compressed** downloads cut fonts down to the letters used and reduce photos.
- **Fonts:** the PDF's own font is reused when it has the letters you typed. Otherwise the same or closest Windows font is used, and a message tells you. Some PDFs can't have their font reused, so the look may change slightly.
- **Select area:** letters count as selected when their middle is inside the box. Lines and shapes must be fully inside it. Text and whole photos stay real and editable. An area with a scan or only part of a photo moves as a picture, with a solid background.
- **Long paragraphs:** edited text re-wraps inside its paragraph. If it grows a lot, it can overlap what comes below, so shorten it or move it.
- **Scans:** recognised text can contain mistakes, so check it. Replacement text is placed over a patch matching the nearby colour, which can show on textured or photographed paper.
- **Drawn shapes and text inside pictures:** charts and logos made of lines can't be edited as objects. Use **Erase area**, then add new text or a photo.
- **Digital signatures:** any edit invalidates an existing signature.
- **Passwords:** the saved copy has no password, even if the original had one. Forgotten passwords can't be recovered.
- **Find and replace:** totals and other numbers are not recalculated.

## Tests

```bash
python tests/make_samples.py samples
```

```bash
python tests/test_engine.py samples
```
