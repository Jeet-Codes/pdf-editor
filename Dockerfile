# PDF Editor in a container: open http://localhost:8765 after
#   docker run -d -p 127.0.0.1:8765:8765 -v pdf-editor-data:/data sandeep934/pdf-editor
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PDF_EDITOR_HOST=0.0.0.0 \
    PDF_EDITOR_PORT=8765 \
    PDF_EDITOR_DATA=/data

# Free fonts with the same letter widths as Arial, Times New Roman, Courier New, Calibri and
# Cambria, so edited text keeps its length. libglib is needed by the OCR image library.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      fontconfig fonts-liberation fonts-crosextra-carlito fonts-crosextra-caladea fonts-dejavu-core \
      libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 # Fetch the text-recognition models now, so scanned pages work without internet later.
 && python -c "from rapidocr import RapidOCR; RapidOCR()"

COPY app.py engine.py ocr.py ./
COPY static ./static

RUN useradd --create-home --uid 1000 editor && mkdir -p /data && chown editor /data
USER editor
VOLUME /data
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/')" || exit 1

CMD ["python", "app.py", "--no-browser"]
