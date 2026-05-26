"""
ComplAI — WhatsApp Sender
Thin wrapper around Twilio's REST API to send WhatsApp messages back to clients.

Why a separate module?
  - Keeps Twilio imports isolated (not everyone needs them)
  - Easy to swap provider later (e.g. Meta Cloud API) without touching routes/workers
  - Can be imported by both routes/whatsapp.py AND workers/celery_app.py
"""

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# ── Twilio credentials (loaded from .env) ──────────────────
# TWILIO_ACCOUNT_SID  = "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
# TWILIO_AUTH_TOKEN   = "your_auth_token"
# TWILIO_WHATSAPP_NUMBER = "+14155238886"   ← sandbox number OR your verified business number
TWILIO_ACCOUNT_SID     = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN      = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER", "")


def send_whatsapp(to_number: str, message: str) -> bool:
    """
    Send a WhatsApp message to a client.

    Args:
        to_number: E.164 phone number WITHOUT 'whatsapp:' prefix (e.g. "+919876543210")
        message:   Plain text message body

    Returns:
        True if sent successfully, False on error (never raises — silent failure
        so a WhatsApp glitch never kills the processing job).

    Why silent failure?
        The invoice was already extracted and saved in the DB. Failing to send
        a WhatsApp notification should NOT roll back or break anything.
    """
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_NUMBER]):
        logger.warning("[whatsapp] Twilio credentials not configured — skipping send")
        return False

    try:
        # Lazy import — don't crash the whole app if twilio isn't installed yet
        from twilio.rest import Client as TwilioClient  # noqa: PLC0415

        client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        msg = client.messages.create(
            from_=f"whatsapp:{TWILIO_WHATSAPP_NUMBER}",
            to=f"whatsapp:{to_number}",
            body=message,
        )
        logger.info(f"[whatsapp] Sent to {to_number} — SID: {msg.sid}")
        return True

    except ImportError:
        logger.error("[whatsapp] 'twilio' package not installed. Run: pip install twilio")
        return False
    except Exception as e:
        logger.error(f"[whatsapp] Failed to send to {to_number}: {e}")
        return False


def format_completion_message(
    client_name: str,
    invoice_count: int,
    taxable_total: float,
    gst_total: float,
    invoice_total: float,
    has_errors: bool,
) -> str:
    """
    Build the WhatsApp reply sent to a client after processing completes.

    Example output:
        ✅ Done, Ramesh!
        📄 1 invoice extracted

        💰 Taxable value : ₹4,820
        🔢 Total GST     : ₹867.60
        💵 Invoice total : ₹5,687.60

        Your CA can see the details. Let us know if anything looks wrong.
    """
    if has_errors or invoice_count == 0:
        return (
            f"⚠️ Hi {client_name}, we received your document but had trouble reading it.\n"
            "Please ask your CA to check — they can see the issue in the dashboard."
        )

    inv_word = "invoice" if invoice_count == 1 else "invoices"
    lines = [
        f"✅ Done, {client_name}!",
        f"📄 {invoice_count} {inv_word} extracted\n",
    ]
    if taxable_total:
        lines.append(f"💰 Taxable value : ₹{taxable_total:,.2f}")
    if gst_total:
        lines.append(f"🔢 Total GST     : ₹{gst_total:,.2f}")
    if invoice_total:
        lines.append(f"💵 Invoice total : ₹{invoice_total:,.2f}")
    lines.append("\nYour CA can see the full details. Let us know if anything looks wrong.")
    return "\n".join(lines)
