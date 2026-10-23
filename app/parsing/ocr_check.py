"""app/parsing/ocr_check.py
Optional OCR cross-check using Tesseract.

Rasterizes PDF pages, OCRs them, and compares with the text layer.
Degrades gracefully if Tesseract is not installed.
"""

import logging
from pathlib import Path

from rapidfuzz import fuzz

from app.parsing.normalize import normalize_text

logger = logging.getLogger(__name__)

# Try to import pytesseract
_tesseract_available = False
try:
    import pytesseract

    _tesseract_available = True
except ImportError:
    logger.info("pytesseract not installed - OCR cross-check disabled")


def is_available() -> bool:
    """Check if Tesseract OCR is available."""
    if not _tesseract_available:
        return False
    try:
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        logger.info("Tesseract binary not found - OCR cross-check disabled")
        return False


def ocr_crosscheck(
    pdf_path: str | Path,
    text_layer_pages: list[str],
    dpi: int = 300,
    similarity_threshold: float = 0.85,
    tesseract_cmd: str | None = None,
) -> dict:
    """Compare OCR output against the text layer.

    Returns a report with per-page similarity scores.
    """
    if not is_available():
        return {
            "available": False,
            "message": "Tesseract not available - skipped OCR cross-check",
            "pages": [],
        }

    try:
        import pymupdf
    except ImportError:
        return {
            "available": False,
            "message": "PyMuPDF not available for page rendering",
            "pages": [],
        }

    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd

    pdf_path = Path(pdf_path)
    doc = pymupdf.open(str(pdf_path))

    page_results = []
    low_similarity_pages = []

    for page_idx, page in enumerate(doc):
        try:
            # Render page to image
            pix = page.get_pixmap(dpi=dpi)
            img_bytes = pix.tobytes("png")

            # OCR the image
            import io

            from PIL import Image

            img = Image.open(io.BytesIO(img_bytes))
            ocr_text = pytesseract.image_to_string(img)

            # Normalize both texts
            text_layer = normalize_text(text_layer_pages[page_idx]) if page_idx < len(text_layer_pages) else ""
            ocr_normalized = normalize_text(ocr_text)

            # Compare
            similarity = fuzz.ratio(text_layer, ocr_normalized) / 100.0

            page_result = {
                "page": page_idx + 1,
                "similarity": round(similarity, 3),
                "below_threshold": similarity < similarity_threshold,
            }
            page_results.append(page_result)

            if similarity < similarity_threshold:
                low_similarity_pages.append(page_idx + 1)
                logger.warning(
                    "OCR cross-check: page %d similarity %.1f%% < threshold %.1f%%",
                    page_idx + 1,
                    similarity * 100,
                    similarity_threshold * 100,
                )
        except Exception as e:
            logger.error("OCR cross-check failed for page %d: %s", page_idx + 1, e)
            page_results.append(
                {
                    "page": page_idx + 1,
                    "error": str(e),
                }
            )

    doc.close()

    return {
        "available": True,
        "pages": page_results,
        "low_similarity_pages": low_similarity_pages,
        "message": (
            f"OCR cross-check complete: {len(low_similarity_pages)} pages below threshold"
            if low_similarity_pages
            else "OCR cross-check passed: all pages above threshold"
        ),
    }
