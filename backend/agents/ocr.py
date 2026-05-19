"""
ComplAI — 3-Layer OCR (LangGraph Node 2)

Layer 1: pdfplumber   — free, perfect for digital PDFs
Layer 2: Google DocAI — for scanned PDFs and images (paid, per page)
Layer 3: Claude Vision — fallback when DocAI confidence < 0.80

Cost hierarchy: pdfplumber (₹0) → DocAI (₹5.5/page) → Claude Vision (₹0.25/page)
We always try the cheapest option first.
"""

import base64
import hashlib
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── OCR result cache (in-process, per-worker) ─────────────────────────────────
# Key: sha256 of image file bytes
# Value: (text, confidence, layer_used)
# Cleared on worker restart. Prevents re-OCR-ing the same page in the same session.
_OCR_CACHE: Dict[str, Tuple[str, float, str]] = {}

def _image_hash(img_path: str) -> str:
    """SHA-256 of image file bytes — used as cache key."""
    with open(img_path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()

# DocAI confidence threshold below which we fall back to Claude Vision.
# 0.80 = use DocAI text when conf ≥ 0.80; fall back to Claude Vision only for
# blurry/low-quality pages. Raised from 0.75 to reduce Claude Vision API spend
# (DocAI at 0.75–0.79 is accurate enough for printed invoices).
DOCAI_CONFIDENCE_THRESHOLD = float(os.getenv("DOCAI_CONFIDENCE_THRESHOLD", "0.80"))

# Session-level DocAI availability flag.
# None = not tested yet, True = working, False = skip (billing disabled / no creds)
# Set to False at module level if env vars are missing so we skip immediately.
_DOCAI_AVAILABLE: Optional[bool] = None


def _render_pdf_page_to_image(file_path: str, page_num: int) -> str:
    """Render a single PDF page to a PNG image using pypdfium2. Returns image path."""
    import pypdfium2 as pdfium
    import tempfile

    try:
        doc = pdfium.PdfDocument(file_path)
        page = doc[page_num]
        # Render at 4x scale → ~288 DPI (standard for document OCR quality)
        bitmap = page.render(scale=4.0)
        pil_image = bitmap.to_pil()

        # Save as JPEG (quality=88) instead of PNG.
        # PNG at scale=4.0 → 5-6 MB (exceeds Claude Vision's 5 MB limit).
        # JPEG at quality=88 → 0.5-1.5 MB, well within the limit, no visible quality loss for OCR.
        tmp = tempfile.NamedTemporaryFile(suffix='.jpg', delete=False)
        pil_image.convert("RGB").save(tmp.name, 'JPEG', quality=88, optimize=True)
        tmp.close()
        doc.close()

        size_kb = os.path.getsize(tmp.name) // 1024
        logger.info(f"[ocr] Rendered PDF page {page_num} → {tmp.name} ({size_kb} KB)")

        # Safety net: if somehow still > 4.5 MB, downscale to fit
        MAX_BYTES = 4_500_000
        if os.path.getsize(tmp.name) > MAX_BYTES:
            logger.warning(f"[ocr] Image {size_kb} KB > 4.5 MB limit — downscaling")
            pil_image.convert("RGB").save(tmp.name, 'JPEG', quality=70, optimize=True)

        return tmp.name
    except Exception as e:
        logger.error(f"[ocr:render] Failed to render page {page_num}: {e}")
        return file_path  # fallback to original


def extract_text_from_chunk(
    chunk: Dict[str, Any],
    doc_type: str,
) -> Tuple[str, float, str]:
    """
    Extract text from a single chunk using the appropriate OCR layer.

    Returns:
      (extracted_text, confidence_score, ocr_method_used)
    """
    file_path = chunk["file_path"]
    pages     = chunk.get("pages", [])

    if doc_type == "digital_pdf":
        text, confidence, _ = _layer1_pdfplumber(file_path, pages)
        return text, confidence, "pdfplumber"

    elif doc_type == "scanned_pdf":
        page_num = chunk.get("page_num", 0)
        img_path = _render_pdf_page_to_image(file_path, page_num)

        # ── Cache lookup (keyed on rendered image bytes) ──────────────────────
        cache_key = _image_hash(img_path) if os.path.exists(img_path) else None
        if cache_key and cache_key in _OCR_CACHE:
            cached_text, cached_conf, cached_layer = _OCR_CACHE[cache_key]
            logger.info(f"[ocr] Cache hit for {os.path.basename(img_path)} — skipping API call")
            _cleanup_temp(img_path, file_path)
            return cached_text, cached_conf, cached_layer

        docai_text, docai_conf = _layer2_google_docai(img_path, [])
        text, confidence = docai_text, docai_conf

        needs_vision = (confidence < DOCAI_CONFIDENCE_THRESHOLD) or (not text.strip())
        if needs_vision:
            reason = f"conf {confidence:.2f} < threshold" if confidence < DOCAI_CONFIDENCE_THRESHOLD else "empty text"
            logger.info(f"[ocr] DocAI {reason}, using Claude Vision")
            try:
                text, confidence = _layer3_claude_vision(img_path, [])
                _cleanup_temp(img_path, file_path)
                result_layer = "claude_vision"
                if cache_key:
                    _OCR_CACHE[cache_key] = (text, confidence, result_layer)
                return text, confidence, result_layer
            except Exception as vision_err:
                err_str = str(vision_err)
                is_billing = any(kw in err_str for kw in ("credit balance", "billing", "quota", "insufficient_quota"))
                if is_billing:
                    # Credit exhausted — if DocAI gave us any text, use it rather than
                    # failing the page entirely. Re-raise so the worker can abort the job.
                    if docai_text.strip():
                        logger.warning(
                            f"[ocr] Claude Vision billing error — using DocAI fallback "
                            f"(conf {docai_conf:.2f}) for this page"
                        )
                        _cleanup_temp(img_path, file_path)
                        result_layer = "google_docai"
                        if cache_key:
                            _OCR_CACHE[cache_key] = (docai_text, docai_conf, result_layer)
                        # Still propagate so the worker knows credits are gone
                        raise
                    else:
                        raise  # No DocAI text either — must abort
                # Non-billing Vision error — fall back to DocAI silently
                logger.warning(f"[ocr] Claude Vision failed ({vision_err}), using DocAI fallback")
                _cleanup_temp(img_path, file_path)
                result_layer = "google_docai"
                if cache_key:
                    _OCR_CACHE[cache_key] = (docai_text, docai_conf, result_layer)
                return docai_text, docai_conf, result_layer
        _cleanup_temp(img_path, file_path)
        result_layer = "google_docai"
        if cache_key:
            _OCR_CACHE[cache_key] = (text, confidence, result_layer)
        return text, confidence, result_layer

    elif doc_type == "image":
        # ── Cache lookup (keyed on image file bytes) ──────────────────────────
        cache_key = _image_hash(file_path) if os.path.exists(file_path) else None
        if cache_key and cache_key in _OCR_CACHE:
            cached_text, cached_conf, cached_layer = _OCR_CACHE[cache_key]
            logger.info(f"[ocr] Cache hit for {os.path.basename(file_path)} — skipping API call")
            return cached_text, cached_conf, cached_layer

        text, confidence = _layer2_google_docai(file_path, pages)
        if confidence < DOCAI_CONFIDENCE_THRESHOLD:
            logger.info(f"[ocr] DocAI conf {confidence:.2f} < threshold, using Claude Vision")
            text, confidence = _layer3_claude_vision(file_path, pages)
            result_layer = "claude_vision"
            if cache_key:
                _OCR_CACHE[cache_key] = (text, confidence, result_layer)
            return text, confidence, result_layer
        result_layer = "google_docai"
        if cache_key:
            _OCR_CACHE[cache_key] = (text, confidence, result_layer)
        return text, confidence, result_layer

    elif doc_type == "excel":
        return _extract_excel(file_path)

    else:
        logger.warning(f"[ocr] Unknown doc_type: {doc_type}")
        return "", 0.0, "unknown"


# ── Layer 1: pdfplumber (digital PDFs) ─────────────────────

def _layer1_pdfplumber(file_path: str, pages: List[int]) -> Tuple[str, float, str]:
    """
    Use pdfplumber to extract text and tables from digital PDFs.
    Confidence is 0.99 — if text is there, it's accurate.
    """
    import pdfplumber

    text_parts = []
    try:
        with pdfplumber.open(file_path) as pdf:
            target_pages = pages if pages else list(range(len(pdf.pages)))
            for page_idx in target_pages:
                if page_idx >= len(pdf.pages):
                    continue
                page = pdf.pages[page_idx]

                # Extract raw text
                page_text = page.extract_text() or ""

                # Extract tables and convert to text
                tables = page.extract_tables()
                for table in tables:
                    for row in table:
                        row_text = " | ".join(str(cell or "").strip() for cell in row)
                        if row_text.strip():
                            page_text += f"\n{row_text}"

                text_parts.append(f"--- Page {page_idx + 1} ---\n{page_text}")

        full_text = "\n\n".join(text_parts)
        logger.info(f"[ocr:pdfplumber] Extracted {len(full_text)} chars from pages {pages}")
        return full_text, 0.99, "pdfplumber"

    except Exception as e:
        logger.error(f"[ocr:pdfplumber] Failed: {e}")
        return "", 0.0, "pdfplumber_failed"


# ── Layer 2: Google Document AI (scanned PDFs + images) ────

def _layer2_google_docai(file_path: str, pages: List[int]) -> Tuple[str, float]:
    """
    Call Google Document AI Invoice Parser.
    Extracts raw text and per-entity confidence scores.
    Average confidence = mean of all entity confidences.

    Environment variables required:
      GOOGLE_APPLICATION_CREDENTIALS — path to service account JSON
      GOOGLE_PROJECT_ID
      GOOGLE_PROCESSOR_ID            — Invoice Parser processor ID

    Session-level bypass: if DocAI fails with BILLING_DISABLED or missing
    credentials, _DOCAI_AVAILABLE is set to False and all subsequent calls
    return immediately without an HTTP round-trip.
    """
    global _DOCAI_AVAILABLE

    # Fast-path: skip if we already know DocAI is unavailable
    if _DOCAI_AVAILABLE is False:
        return "", 0.0

    project_id    = os.getenv("GOOGLE_PROJECT_ID", "")
    processor_id  = os.getenv("GOOGLE_PROCESSOR_ID", "")
    creds_path    = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
    location      = "us"  # or "eu" depending on processor region

    # Pre-flight: mark unavailable if env vars or credentials file missing
    if not project_id or not processor_id:
        logger.warning("[ocr:docai] Missing GOOGLE_PROJECT_ID or GOOGLE_PROCESSOR_ID — skipping DocAI permanently")
        _DOCAI_AVAILABLE = False
        return "", 0.0

    if creds_path and not os.path.exists(creds_path):
        logger.warning(f"[ocr:docai] Credentials file not found: {creds_path} — skipping DocAI permanently")
        _DOCAI_AVAILABLE = False
        return "", 0.0

    try:
        from google.cloud import documentai

        client = documentai.DocumentProcessorServiceClient()
        processor_name = client.processor_path(project_id, location, processor_id)

        # Read file
        with open(file_path, "rb") as f:
            content = f.read()

        # Determine MIME type
        ext = file_path.rsplit(".", 1)[-1].lower()
        mime_map = {"pdf": "application/pdf", "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}
        mime_type = mime_map.get(ext, "application/pdf")

        raw_document = documentai.RawDocument(content=content, mime_type=mime_type)

        request = documentai.ProcessRequest(
            name=processor_name,
            raw_document=raw_document,
        )

        result = client.process_document(request=request)
        document = result.document

        # Extract full text
        full_text = document.text or ""

        # Calculate average confidence from entity-level scores
        confidences = []
        for entity in document.entities:
            if entity.confidence is not None:
                confidences.append(entity.confidence)

        avg_confidence = sum(confidences) / len(confidences) if confidences else 0.5
        _DOCAI_AVAILABLE = True  # confirmed working
        logger.info(
            f"[ocr:docai] Extracted {len(full_text)} chars, "
            f"entities={len(confidences)}, avg_conf={avg_confidence:.2f}"
        )
        return full_text, avg_confidence

    except Exception as e:
        error_str = str(e)
        # Permanently skip on billing / auth failures — no point retrying per page
        if any(kw in error_str for kw in ("BILLING_DISABLED", "credentials", "UNAUTHENTICATED", "403")):
            logger.warning(f"[ocr:docai] Permanently disabling DocAI for this session: {error_str[:120]}")
            _DOCAI_AVAILABLE = False
        else:
            logger.error(f"[ocr:docai] Failed: {e}")
        return "", 0.0


# ── Layer 3: Claude Vision (fallback for blurry/low-quality) ─

def _cleanup_temp(img_path: str, original_path: str):
    """Delete temp PNG after use to free disk space."""
    try:
        if img_path != original_path:
            import os as _os
            _os.unlink(img_path)
    except Exception:
        pass


def _layer3_claude_vision(file_path: str, pages: List[int]) -> Tuple[str, float]:
    """
    Use Claude Sonnet with vision to extract text from images.
    Used when DocAI confidence < 0.75 (blurry WhatsApp photos, etc.)
    Confidence is conservatively set to 0.70 (vision extraction is good but not perfect).
    """
    import anthropic
    from utils.cost_tracker import track_claude_cost

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    # Read and encode the file as base64
    with open(file_path, "rb") as f:
        image_data = base64.standard_b64encode(f.read()).decode("utf-8")

    ext = file_path.rsplit(".", 1)[-1].lower()
    media_type_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}
    media_type = media_type_map.get(ext, "image/jpeg")  # default jpeg (our rendered files are .jpg)

    prompt = (
        "Extract ALL text from this Indian GST invoice image. "
        "Preserve the layout — use newlines to separate rows, and spaces/tabs for columns. "
        "Be especially careful with:\n"
        "- GSTIN numbers (15-char alphanumeric, starts with 2-digit state code)\n"
        "- Invoice numbers, dates (DD/MM/YYYY format)\n"
        "- All monetary amounts (include paise/decimals)\n"
        "- HSN/SAC codes (numeric)\n"
        "- Tax amounts: CGST, SGST, IGST\n"
        "If text is blurry, make your best guess and include it — do not skip.\n"
        "Return ONLY the extracted text, no commentary."
    )

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",  # Haiku is 12x cheaper than Sonnet for vision
            max_tokens=3000,  # invoices can be text-heavy; 2048 was truncating
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_data,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }],
        )

        # Track cost
        track_claude_cost(
            model="claude-haiku",
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )

        extracted_text = response.content[0].text
        logger.info(f"[ocr:claude_vision] Extracted {len(extracted_text)} chars")
        return extracted_text, 0.70  # conservative confidence for vision

    except Exception as e:
        error_str = str(e)
        if any(kw in error_str for kw in ("credit balance", "billing", "quota", "insufficient_quota")):
            logger.error(f"[ocr:claude_vision] FATAL billing error — aborting: {e}")
            raise  # stop the whole job immediately, don't process more pages
        logger.error(f"[ocr:claude_vision] Failed: {e}")
        return "", 0.0


# ── Excel / CSV extraction ──────────────────────────────────

def _extract_excel(file_path: str) -> Tuple[str, float, str]:
    """
    Convert Excel/CSV to text representation for Claude extraction.
    Confidence is 0.99 — structured data is very reliable.
    """
    import pandas as pd

    try:
        if file_path.endswith(".csv"):
            df = pd.read_csv(file_path)
        else:
            df = pd.read_excel(file_path)

        # Convert to a readable text format with row/column labels
        text = df.to_string(index=False)
        logger.info(f"[ocr:excel] Extracted {len(text)} chars, shape={df.shape}")
        return text, 0.99, "pandas_excel"

    except Exception as e:
        logger.error(f"[ocr:excel] Failed: {e}")
        return "", 0.0, "excel_failed"


# ── Node function (called by LangGraph) ────────────────────

def ocr_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    LangGraph Node 2: ocr_node
    Runs OCR on all chunks and stores results in state.
    """
    doc_type = state.get("doc_type", "digital_pdf")
    chunks   = state.get("chunks", [])

    processed_chunks = []
    for i, chunk in enumerate(chunks):
        logger.info(f"[ocr] Processing chunk {i + 1}/{len(chunks)}")
        text, confidence, method = extract_text_from_chunk(chunk, doc_type)
        processed_chunks.append({
            **chunk,
            "text":       text,
            "confidence": confidence,
            "ocr_method": method,
            "layer_used": method,   # same as method, renamed for clarity
        })

    state["chunks"]      = processed_chunks
    state["current_chunk"] = 0
    return state
