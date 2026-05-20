"""Extract plain text from a resume attachment. Tries fast text extraction
first (pypdf for PDF, python-docx for Word). If a PDF returns very little
text (likely a scanned image), falls back to OCR via Tesseract."""

from __future__ import annotations

import io
import logging

log = logging.getLogger(__name__)

# If pypdf extracts fewer than this many characters from a PDF, we treat it
# as image-based and OCR it instead.
OCR_FALLBACK_CHAR_THRESHOLD = 200


def _extract_pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    chunks = []
    for page in reader.pages:
        try:
            chunks.append(page.extract_text() or "")
        except Exception as e:
            log.warning("pypdf page extract failed: %s", e)
    return "\n".join(chunks).strip()


def _ocr_pdf(data: bytes) -> str:
    """Rasterize each page to an image and OCR. Requires tesseract + poppler
    installed on the runner."""
    try:
        from pdf2image import convert_from_bytes
        import pytesseract
    except ImportError as e:
        log.warning("OCR dependencies unavailable: %s", e)
        return ""
    try:
        pages = convert_from_bytes(data, dpi=200)
    except Exception as e:
        log.warning("pdf2image failed: %s", e)
        return ""
    out = []
    for img in pages:
        try:
            out.append(pytesseract.image_to_string(img) or "")
        except Exception as e:
            log.warning("OCR page failed: %s", e)
    return "\n".join(out).strip()


def _extract_docx_text(data: bytes) -> str:
    import docx
    doc = docx.Document(io.BytesIO(data))
    parts = [p.text for p in doc.paragraphs if p.text]
    # Also grab table cell text — resumes often use tables for layout
    for t in doc.tables:
        for row in t.rows:
            for cell in row.cells:
                if cell.text:
                    parts.append(cell.text)
    return "\n".join(parts).strip()


def extract(filename: str, mime_type: str, data: bytes) -> tuple[str, bool]:
    """Returns (text, used_ocr). Empty text means we couldn't read it."""
    name = (filename or "").lower()
    is_pdf = "pdf" in (mime_type or "") or name.endswith(".pdf")
    is_docx = "wordprocessingml" in (mime_type or "") or name.endswith(".docx")

    if is_pdf:
        text = _extract_pdf_text(data)
        if len(text) >= OCR_FALLBACK_CHAR_THRESHOLD:
            return text, False
        log.info("PDF text extraction returned %d chars; falling back to OCR", len(text))
        ocr_text = _ocr_pdf(data)
        return ocr_text or text, bool(ocr_text)

    if is_docx:
        return _extract_docx_text(data), False

    log.warning("Unsupported attachment type: %s (%s)", filename, mime_type)
    return "", False
