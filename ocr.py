"""Offline text recognition for scanned pages (loaded only when first used)."""
from __future__ import annotations

_engine = None


def recognise(pix):
    """Return [(text, (x0, y0, x1, y1))] in pixmap pixels."""
    global _engine
    try:
        import numpy as np
        from rapidocr import RapidOCR
    except ImportError as exc:
        raise RuntimeError(
            "Text recognition is not installed. Run: python -m pip install rapidocr onnxruntime"
        ) from exc
    if _engine is None:
        _engine = RapidOCR()
    image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    # The engine expects BGR.
    result = _engine(image[:, :, ::-1].copy())
    if result is None or result.boxes is None:
        return []
    out = []
    for box, text in zip(result.boxes, result.txts):
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        out.append((text, (min(xs), min(ys), max(xs), max(ys))))
    return out
