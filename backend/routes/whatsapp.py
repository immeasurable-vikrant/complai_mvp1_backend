"""
ComplAI — WhatsApp Webhook Route
Receives incoming WhatsApp messages via Twilio and queues them for processing.

POST /api/whatsapp/webhook   ← Twilio calls this for every incoming message
GET  /api/whatsapp/webhook   ← Twilio calls this to verify the endpoint is alive

Full flow:
  1. Client sends invoice photo/PDF on WhatsApp to the CA firm's Twilio number
  2. Twilio forwards the message here as a POST with form-encoded data
  3. We validate the Twilio signature (security — reject spoofed requests)
  4. We parse the sender's phone number and look up the matching client in the DB
  5. We download the media file (image/PDF) from Twilio's CDN
  6. We save it to ./uploads/ exactly like a web upload
  7. We create Document + JobStatus records in the DB
  8. We queue a Celery processing job (same pipeline as web upload)
  9. We send an immediate WhatsApp reply: "📄 Received! Processing your invoice..."
  10. When Celery finishes, it sends a second reply with the extracted totals
"""

import hashlib
import hmac
import logging
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Form, Header, HTTPException, Request, Response
from sqlalchemy.orm import Session

from models.db import (
    Client, Document, DocumentFileType, DocumentSourceChannel,
    DocumentStatus, Firm, JobStatus, JobStatusEnum, get_db,
)
from workers.celery_app import process_document

logger = logging.getLogger(__name__)
router = APIRouter()

# ── Config ─────────────────────────────────────────────────
# All set in .env — never hardcode credentials here
TWILIO_AUTH_TOKEN      = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER", "")
UPLOAD_DIR             = os.getenv("UPLOAD_DIR", "./uploads")

# ── Month / year extraction from caption text ──────────────
# Clients can write captions like:
#   "October invoice"  →  2025-10
#   "oct 24"           →  2024-10
#   "for jan 2025"     →  2025-01
#   "feb"              →  current-year-02
#   (nothing useful)   →  current month  ← fallback

_MONTH_NAMES = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

def _extract_month_year(text: str) -> str:
    """
    Try to find a month (and optional year) in the WhatsApp caption.
    Returns "YYYY-MM" string. Falls back to current month if nothing found.

    Patterns handled (case-insensitive):
      • "october"           → current year, month=10
      • "oct 24"            → 2024-10
      • "oct 2024"          → 2024-10
      • "jan 25"            → 2025-01
      • "for november 2024" → 2024-11
      • "invoice feb"       → current year, month=02
      • "2024-10"           → 2024-10  (already formatted)
      • "10/2024"           → 2024-10
    """
    if not text:
        return _current_month_year()

    now = datetime.utcnow()
    text_lower = text.lower()

    # ── Pattern 1: numeric "YYYY-MM" or "MM-YYYY" or "MM/YYYY" ──────────
    m = re.search(r'\b(20\d{2})[-/](0?[1-9]|1[0-2])\b', text_lower)
    if m:
        yr, mo = int(m.group(1)), int(m.group(2))
        return f"{yr:04d}-{mo:02d}"

    m = re.search(r'\b(0?[1-9]|1[0-2])[-/](20\d{2})\b', text_lower)
    if m:
        mo, yr = int(m.group(1)), int(m.group(2))
        return f"{yr:04d}-{mo:02d}"

    # ── Pattern 2: month-name optionally followed by 2- or 4-digit year ──
    month_pattern = r'\b(' + '|'.join(_MONTH_NAMES.keys()) + r')\b'
    m = re.search(month_pattern + r'[\s,\-]*(\d{2,4})?', text_lower)
    if m:
        mo = _MONTH_NAMES[m.group(1)]
        yr_raw = m.group(2)
        if yr_raw:
            yr = int(yr_raw)
            if yr < 100:                # "24" → 2024
                yr += 2000
        else:
            yr = now.year
        return f"{yr:04d}-{mo:02d}"

    # ── Fallback ──────────────────────────────────────────────────────────
    return _current_month_year()


def _current_month_year() -> str:
    return datetime.utcnow().strftime("%Y-%m")


# ── Supported media types from WhatsApp ────────────────────
# WhatsApp compresses images to JPEG. PDFs may arrive as application/pdf
# OR as application/octet-stream (Twilio sandbox sometimes does this).
MEDIA_TYPE_MAP = {
    "image/jpeg":                "jpg",
    "image/jpg":                 "jpg",
    "image/png":                 "png",
    "application/pdf":           "pdf",
    "application/octet-stream":  "pdf",   # Twilio sandbox sends PDFs with this type
    "image/heic":                "heic",
}

FILE_TYPE_MAP = {
    "jpg":  (DocumentFileType.jpg,  ".jpg"),
    "png":  (DocumentFileType.png,  ".png"),
    "pdf":  (DocumentFileType.pdf,  ".pdf"),
    "heic": (DocumentFileType.heic, ".heic"),
}


# ── Security: Twilio signature validation ──────────────────
def _validate_twilio_signature(
    request_url: str,
    post_data: dict,
    x_twilio_signature: str,
    auth_token: str,
) -> bool:
    """
    Twilio signs every webhook request with HMAC-SHA1.
    We recompute the signature and compare — rejects spoofed requests.

    How it works:
      1. Start with the full request URL (e.g. https://abc.ngrok.io/api/whatsapp/webhook)
      2. Sort the POST parameters alphabetically by key
      3. Append each key+value to the URL string
      4. HMAC-SHA1 sign with your Twilio Auth Token
      5. Base64 encode → compare to X-Twilio-Signature header

    Why this matters:
      Without this check, ANYONE could POST to your webhook and inject fake invoices.
    """
    if not auth_token:
        # In dev/test without a real token, skip validation (log a warning)
        logger.warning("[whatsapp] TWILIO_AUTH_TOKEN not set — skipping signature validation (dev mode)")
        return True

    # Build the signed string: URL + sorted params concatenated
    s = request_url
    for key in sorted(post_data.keys()):
        s += key + (post_data[key] or "")

    # Compute expected signature
    mac = hmac.new(auth_token.encode("utf-8"), s.encode("utf-8"), hashlib.sha1)
    import base64
    expected = base64.b64encode(mac.digest()).decode("utf-8")

    return hmac.compare_digest(expected, x_twilio_signature)


# ── Media download ─────────────────────────────────────────
async def _download_media(
    media_url: str,
    account_sid: str,
    auth_token: str,
    dest_path: Path,
) -> None:
    """
    Download media from Twilio's CDN to local disk.

    Why Twilio credentials for download?
      Twilio media URLs require HTTP Basic Auth (account_sid:auth_token).
      Without credentials the request returns 403.

    Why httpx and not requests?
      httpx supports async — we're inside an async FastAPI route.
    """
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(
            media_url,
            auth=(account_sid, auth_token),
            follow_redirects=True,
        )
        response.raise_for_status()
        dest_path.write_bytes(response.content)
        logger.info(f"[whatsapp] Downloaded media: {len(response.content):,} bytes → {dest_path}")


# ── Phone number normalisation ─────────────────────────────
def _normalise_phone(raw: str) -> str:
    """
    Twilio sends: "whatsapp:+919876543210"
    DB stores:    "+919876543210"

    Strip the "whatsapp:" prefix and return E.164 format.
    """
    return raw.replace("whatsapp:", "").strip()


# ── Client lookup ──────────────────────────────────────────
def _find_client_by_phone(phone: str, db: Session) -> Optional[tuple]:
    """
    Look up (client, firm) by the sender's WhatsApp number.

    Why (client, firm)?
      Our system is single-tenant (one firm) but we still need the firm_id
      for Document records. The Firm table always has exactly one row.

    If no client matches: return (None, None) — we'll reply telling them
    they're not registered.
    """
    # Normalise stored numbers the same way (some may be stored without +)
    client = db.query(Client).filter(
        Client.whatsapp_number == phone,
        Client.is_active == True,
    ).first()
    if not client:
        return None, None

    firm = db.query(Firm).filter(Firm.id == client.firm_id).first()
    return client, firm


# ── Webhook: GET (health check / Twilio verify) ────────────
@router.get("/webhook")
async def whatsapp_webhook_verify():
    """
    Twilio does a GET to verify the endpoint is alive when you first configure it.
    Just return 200 OK.
    """
    return Response(content="OK", media_type="text/plain")


# ── Webhook: POST (incoming message) ──────────────────────
def _twiml(message: str = "") -> Response:
    """
    Build a TwiML response.
    Using <Message> inside <Response> tells Twilio to send a WhatsApp reply
    *as part of the HTTP response* — zero latency, no extra API call required.
    Passing message="" returns an empty <Response/> (no reply sent).
    """
    if message:
        # Escape XML special chars so the message body is safe
        safe = (message
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;"))
        body = f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{safe}</Message></Response>'
    else:
        body = "<Response/>"
    return Response(content=body, media_type="application/xml")


@router.post("/webhook")
async def whatsapp_webhook(
    request:             Request,
    db:                  Session = Depends(get_db),
    x_twilio_signature:  str = Header(default="", alias="X-Twilio-Signature"),
):
    """
    Main webhook: called by Twilio for every incoming WhatsApp message.

    KEY DESIGN: This handler returns in < 200 ms.
      • All replies go back as TwiML <Message> — no outbound API call needed.
      • Media download happens inside the Celery worker (not here).
        This avoids Twilio's 15-second webhook timeout and means the client
        gets the "Got it" reply instantly even if download later fails.

    Twilio POST body (form-encoded) key fields:
      From              = "whatsapp:+919876543210"
      Body              = "Invoice for October"   ← caption (may be empty)
      NumMedia          = "1"
      MediaUrl0         = "https://api.twilio.com/..."
      MediaContentType0 = "image/jpeg" | "application/pdf" | "application/octet-stream"
    """
    # ── Parse the form body ────────────────────────────────
    form  = await request.form()
    data  = dict(form)

    sender_raw   = data.get("From", "")
    body_text    = data.get("Body", "").strip()
    num_media    = int(data.get("NumMedia", "0"))
    media_url    = data.get("MediaUrl0", "")
    media_type   = data.get("MediaContentType0", "")
    account_sid  = os.getenv("TWILIO_ACCOUNT_SID", "")
    sender_phone = _normalise_phone(sender_raw)

    logger.info(
        f"[whatsapp] Incoming from {sender_phone} | media={num_media} "
        f"| type={media_type!r} | body='{body_text[:80]}'"
    )

    # ── 1. Validate Twilio signature ──────────────────────
    skip_validation = os.getenv("TWILIO_VALIDATE_SIG", "false").lower() != "true"
    if not skip_validation:
        if not _validate_twilio_signature(str(request.url), data, x_twilio_signature, TWILIO_AUTH_TOKEN):
            logger.warning(f"[whatsapp] Invalid Twilio signature from {sender_phone} — rejecting")
            return _twiml()
    else:
        logger.info("[whatsapp] Signature validation skipped (dev/ngrok mode)")

    # ── 2. Look up the client ─────────────────────────────
    client, firm = _find_client_by_phone(sender_phone, db)

    if not client:
        logger.info(f"[whatsapp] Unknown number {sender_phone} — not registered")
        return _twiml(
            "❌ Sorry, your number is not registered in our system.\n"
            "Please contact your CA firm to get added."
        )

    # ── 3. Text-only message (no attachment) ──────────────
    if num_media == 0:
        if body_text:
            logger.info(f"[whatsapp] Text-only from {client.name} — sending instructions")
            return _twiml(
                f"👋 Hi {client.name}! To submit an invoice, just send it as a "
                "photo or PDF attachment. We'll extract and process it automatically. 📄"
            )
        return _twiml()

    # ── 4. Validate media type ─────────────────────────────
    media_type_clean = media_type.split(";")[0].strip().lower()

    # Twilio Sandbox sometimes omits MediaContentType0 entirely for PDF documents.
    # Treat a missing content type the same as octet-stream — it maps to PDF below.
    if not media_type_clean and media_url:
        logger.info("[whatsapp] MediaContentType0 missing — defaulting to application/octet-stream")
        media_type_clean = "application/octet-stream"

    logger.info(f"[whatsapp] Media type: {media_type_clean!r}")

    # Twilio Sandbox often sends PDFs as application/octet-stream.
    # Try to refine using URL extension; fall through to MEDIA_TYPE_MAP otherwise.
    if media_type_clean == "application/octet-stream" and media_url:
        url_lower = media_url.lower().split("?")[0]
        if url_lower.endswith(".pdf"):
            media_type_clean = "application/pdf"
        elif url_lower.endswith((".jpg", ".jpeg")):
            media_type_clean = "image/jpeg"
        elif url_lower.endswith(".png"):
            media_type_clean = "image/png"
        # If URL has no extension, leave as octet-stream — MEDIA_TYPE_MAP maps it to pdf

    ext_key = MEDIA_TYPE_MAP.get(media_type_clean)
    if not ext_key:
        logger.warning(f"[whatsapp] Unsupported media type: {media_type_clean!r} from {sender_phone}")
        return _twiml(
            f"⚠️ Hi {client.name}, we received your file but the format isn't supported.\n"
            "Please send invoices as *photos (JPEG/PNG)* or *PDF* files."
        )

    file_type, ext = FILE_TYPE_MAP[ext_key]
    logger.info(f"[whatsapp] file_type={file_type} ext={ext}")

    # ── 5. Pre-allocate destination path ──────────────────
    # File does NOT exist yet — the Celery worker downloads it.
    # Pre-allocating the path here lets us create the DB record immediately.
    dest_dir = Path(UPLOAD_DIR) / str(firm.id) / str(client.id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename  = f"wa_{uuid.uuid4().hex}{ext}"
    dest_path = dest_dir / filename

    # ── 6. Create DB records ───────────────────────────────
    # Try to parse month from the caption ("october invoice", "jan 25", etc.)
    month_year = _extract_month_year(body_text)
    logger.info(f"[whatsapp] month_year={month_year!r} (parsed from caption: {body_text!r})")
    document   = Document(
        client_id      = client.id,
        firm_id        = firm.id,
        file_path      = str(dest_path),
        file_type      = file_type,
        source_channel = DocumentSourceChannel.whatsapp,
        status         = DocumentStatus.queued,
        month_year     = month_year,
    )
    db.add(document)
    db.flush()

    job = JobStatus(
        firm_id     = firm.id,
        document_id = document.id,
        status      = JobStatusEnum.queued,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    logger.info(
        f"[whatsapp] Queued doc={document.id} job={job.id} "
        f"client={client.name} ({sender_phone})"
    )

    # ── 7. Queue Celery job ────────────────────────────────
    # Pass media_url + account_sid so the worker can download the file itself.
    process_document.delay(
        job_id          = job.id,
        file_path       = str(dest_path),
        document_id     = document.id,
        client_id       = client.id,
        firm_id         = firm.id,
        whatsapp_sender = sender_phone,
        client_name     = client.name,
        media_url       = media_url,      # ← worker downloads the file
        account_sid     = account_sid,    # ← Twilio Basic Auth user
    )

    # ── 8. Reply instantly via TwiML ──────────────────────
    # Show the detected month so the client can catch any misparse immediately.
    try:
        yr, mo = month_year.split("-")
        month_label = datetime(int(yr), int(mo), 1).strftime("%B %Y")   # e.g. "October 2025"
    except Exception:
        month_label = month_year

    return _twiml(
        f"📄 Got it, {client.name}! Your document is being processed for *{month_label}*.\n"
        "I'll send you the details once it's done — single invoices usually take 1-2 mins, "
        "larger documents (50+ pages) can take 15-20 mins. Please wait. ⏳\n\n"
        "_If the month above is wrong, just reply with the correct month (e.g. \"October\") "
        "and resend the file._"
    )
