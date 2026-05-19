"""
ComplAI — Document Classifier (LangGraph Node 1)
Determines document type and splits large PDFs into 4-page chunks.

Supported doc_types:
  digital_pdf  — PDF with extractable text (pdfplumber works)
  scanned_pdf  — PDF with no/little text (needs OCR)
  image        — JPG/PNG/HEIC
  excel        — XLSX or CSV
"""

import logging
import os
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1  # one page per chunk (each page → separate accordion in UI)


def classify_document(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    LangGraph Node 1: classify_node

    Input state keys used:
      file_path  — absolute path to uploaded file
      file_type  — e.g. "pdf", "jpg", "heic", "xlsx", "csv"

    Output state keys set:
      doc_type   — "digital_pdf" | "scanned_pdf" | "image" | "excel"
      chunks     — list of chunk dicts {pages: [i], file_path: str, page_num: i, page_range: str}
                   For non-PDF types, always one chunk covering the whole file.
    """
    file_path = state["file_path"]
    file_type = state.get("file_type", "").lower()

    logger.info(f"[classify] file_type={file_type} path={file_path}")

    # ── HEIC → JPG conversion ──────────────────────────────
    if file_type in ("heic", "heif"):
        file_path = _convert_heic_to_jpg(file_path)
        state["file_path"] = file_path
        file_type = "jpg"

    # ── Excel / CSV ────────────────────────────────────────
    if file_type in ("xlsx", "csv"):
        state["doc_type"] = "excel"
        state["chunks"]   = [{"pages": [], "file_path": file_path}]
        return state

    # ── Image ──────────────────────────────────────────────
    if file_type in ("jpg", "jpeg", "png"):
        state["doc_type"] = "image"
        state["chunks"]   = [{"pages": [], "file_path": file_path}]
        return state

    # ── PDF ────────────────────────────────────────────────
    if file_type == "pdf":
        state["doc_type"], state["chunks"] = _classify_pdf(file_path)
        return state

    # Fallback — treat as image and let OCR decide
    logger.warning(f"[classify] Unknown file_type '{file_type}', treating as image")
    state["doc_type"] = "image"
    state["chunks"]   = [{"pages": [], "file_path": file_path}]
    return state


def _convert_heic_to_jpg(heic_path: str) -> str:
    """
    Convert HEIC/HEIF to JPG using pillow-heif.
    Returns the path to the converted JPG.
    HEIC is common for WhatsApp photos taken on iPhones.
    """
    try:
        import pillow_heif
        from PIL import Image

        pillow_heif.register_heif_opener()
        img = Image.open(heic_path)
        jpg_path = heic_path.rsplit(".", 1)[0] + "_converted.jpg"
        img.save(jpg_path, "JPEG", quality=95)
        logger.info(f"[classify] HEIC→JPG: {jpg_path}")
        return jpg_path
    except Exception as e:
        logger.error(f"[classify] HEIC conversion failed: {e}")
        # Return original path and let OCR handle it
        return heic_path


def _classify_pdf(file_path: str):
    """
    Determine if a PDF is digital (has extractable text) or scanned.
    Rule: if total extracted text across first 3 pages > 100 chars → digital.
    Also splits the PDF into 4-page chunks for parallel processing.
    """
    import pdfplumber

    doc_type = "scanned_pdf"
    total_text_len = 0

    try:
        with pdfplumber.open(file_path) as pdf:
            num_pages = len(pdf.pages)

            # Sample first 3 pages to decide digital vs scanned
            sample_pages = min(3, num_pages)
            for i in range(sample_pages):
                text = pdf.pages[i].extract_text() or ""
                total_text_len += len(text.strip())

            if total_text_len > 100:
                doc_type = "digital_pdf"

            # One chunk per page
            chunks = []
            for i in range(num_pages):
                chunks.append({
                    "pages":      [i],
                    "file_path":  file_path,
                    "page_num":   i,
                    "page_range": str(i + 1),
                })

        logger.info(
            f"[classify] PDF: {doc_type}, pages={num_pages}, "
            f"chunks={len(chunks)}, text_sample={total_text_len}chars"
        )
        return doc_type, chunks

    except Exception as e:
        logger.error(f"[classify] PDF classification failed: {e}")
        # Fallback: treat as scanned, one chunk
        return "scanned_pdf", [{"pages": [0], "file_path": file_path, "page_range": "all"}]
