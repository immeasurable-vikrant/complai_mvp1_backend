"""
ComplAI — Celery Worker
Async task queue powered by Redis broker.
All document processing runs here — never blocks HTTP responses.

Start worker with:
  celery -A workers.celery_app worker --loglevel=info --concurrency=4

Tasks:
  process_document       — full invoice extraction pipeline
  process_bank_statement — bank statement parsing pipeline

Design principles:
  - Idempotent: safe to retry if a task fails midway
  - Progress tracking: JobStatus updated at each pipeline stage
  - Error handling: never leaves jobs in "processing" state forever
"""

import logging
import os
import sys
from pathlib import Path
from typing import Optional

# ── Fix import paths FIRST — before any non-stdlib import ────────────────────
# On macOS Python 3.12 the multiprocessing start-method is "spawn", meaning
# worker processes start fresh and do NOT inherit sys.path.  We must insert
# backend/ into sys.path (and os.environ["PYTHONPATH"]) here at module level
# so that every spawned worker that reimports this module also gets the fix.
_BACKEND_DIR = Path(__file__).resolve().parent.parent   # …/backend/
_backend_str = str(_BACKEND_DIR)

if _backend_str not in sys.path:
    sys.path.insert(0, _backend_str)

# Propagate via environment so spawned child processes inherit it before they
# even import this module (os.environ IS inherited across spawn()).
_existing = os.environ.get("PYTHONPATH", "")
if _backend_str not in _existing:
    os.environ["PYTHONPATH"] = f"{_backend_str}:{_existing}" if _existing else _backend_str

from celery import Celery
from celery.signals import worker_process_init
from dotenv import load_dotenv

# Load .env from backend/ regardless of launch directory
load_dotenv(dotenv_path=_BACKEND_DIR / ".env", override=True)


@worker_process_init.connect
def _worker_process_init(**kwargs):
    """Re-insert backend/ into sys.path inside every spawned worker process.

    On macOS (spawn start-method) the child process reimports this module but
    os.environ["PYTHONPATH"] may not yet be reflected in sys.path because the
    Python interpreter reads PYTHONPATH only at startup.  This signal fires
    immediately after the worker process starts, guaranteeing the path is set.
    """
    if _backend_str not in sys.path:
        sys.path.insert(0, _backend_str)

logger = logging.getLogger(__name__)

# ── Celery app setup ───────────────────────────────────────
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery(
    "complai",
    broker=REDIS_URL,
    backend=REDIS_URL,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Kolkata",   # IST — important for date-based cost aggregation
    enable_utc=True,
    task_acks_late=True,       # acknowledge after completion (safer for retry)
    worker_prefetch_multiplier=1,  # one task at a time per worker (memory safety)
    task_track_started=True,
    result_expires=86400,      # keep results for 24 hours
)

# ── Helper: get a fresh DB session in worker context ───────

def _get_db_session():
    """Create a DB session for use inside Celery tasks."""
    from models.db import SessionLocal
    return SessionLocal()


# ── Task 1: Invoice extraction pipeline ────────────────────

@celery_app.task(
    bind=True,
    name="complai.process_document",
    max_retries=0,   # no auto-retry — incremental saves mean partial results are kept
)
def process_document(
    self,
    job_id: str,
    file_path: str,
    document_id: int,
    client_id: int,
    firm_id: int,
    whatsapp_sender: str = "",     # E.164 phone number — set when upload came from WhatsApp
    client_name: str = "",         # client display name for the reply message
    media_url: str = "",           # Twilio media URL — if set, worker downloads the file first
    account_sid: str = "",         # Twilio Account SID for download auth
):
    """
    Full invoice extraction pipeline for one document.

    Strategy:
    - digital_pdf : process & save each page independently so partial results
                    survive if a later page fails (e.g. 10-page PDF: pages 1-6
                    saved even if page 7 crashes).
    - scanned_pdf : combine all pages into ONE extraction call (a multi-page
                    scanned invoice is one document, not 10 separate invoices).
    - image/excel : single chunk, standard processing.

    Steps:
      1. Update JobStatus → processing
      2. Classify document → determine doc_type + chunks
      3. For each chunk: OCR → extract → validate → save immediately
      4. Update JobStatus → done (even if some chunks failed — partial results kept)
    """
    from models.db import Document, DocumentStatus, JobStatus, JobStatusEnum
    from agents.pipeline import pipeline as extraction_pipeline
    from agents.classifier import classify_document as classify_fn
    from utils.cost_tracker import reset_cost, get_accumulated_cost, get_accumulated_claude_cost, get_accumulated_docai_cost

    db  = _get_db_session()
    job = None

    try:
        # ── Step 0: Download media file (WhatsApp uploads only) ──────────────
        # The webhook no longer downloads the file — it queues us immediately so
        # the client gets a "Got it" reply within ~1 second.  We do the download
        # here where a timeout won't break the Twilio webhook response.
        if media_url and not Path(file_path).exists():
            logger.info(f"[worker] Downloading media from Twilio for job={job_id}")
            import httpx as _httpx
            _token = os.getenv("TWILIO_AUTH_TOKEN", "")
            try:
                with _httpx.Client(timeout=120.0) as _hclient:
                    resp = _hclient.get(
                        media_url,
                        auth=(account_sid, _token),
                        follow_redirects=True,
                    )
                    resp.raise_for_status()
                Path(file_path).parent.mkdir(parents=True, exist_ok=True)
                Path(file_path).write_bytes(resp.content)
                logger.info(f"[worker] Downloaded {len(resp.content):,} bytes → {file_path}")
            except Exception as dl_err:
                logger.error(f"[worker] Media download failed for job={job_id}: {dl_err}")
                # Mark the job as error so the dashboard shows it
                err_job = db.query(JobStatus).filter(JobStatus.id == job_id).first()
                if err_job:
                    err_job.status        = JobStatusEnum.error
                    err_job.error_message = f"File download failed: {dl_err}"
                    db.commit()
                if whatsapp_sender:
                    try:
                        from agents.whatsapp_sender import send_whatsapp as _send_wa
                        _send_wa(whatsapp_sender,
                                 "⚠️ We had trouble downloading your file. Please try sending it again.")
                    except Exception:
                        pass
                return

        # ── Step 1: mark job as processing ─────────────────
        job = db.query(JobStatus).filter(JobStatus.id == job_id).first()
        if not job:
            logger.error(f"[worker] Job {job_id} not found")
            return

        job.status = JobStatusEnum.processing
        db.commit()
        reset_cost()

        ext = Path(file_path).suffix.lstrip(".").lower()
        if ext == "jpeg":
            ext = "jpg"

        base_state = {
            "file_path":          file_path,
            "file_type":          ext,
            "document_id":        document_id,
            "client_id":          client_id,
            "firm_id":            firm_id,
            "chunks":             [],
            "current_chunk":      0,
            "extracted_invoices": [],
            "errors":             [],
            "db_session":         db,
        }

        # ── Step 2: classify to get doc_type + chunks ──────
        logger.info(f"[worker] Starting pipeline for job={job_id} doc={document_id}")
        classified = classify_fn(dict(base_state))
        doc_type     = classified.get("doc_type", "digital_pdf")
        all_chunks   = classified.get("chunks", [])
        chunks_total = len(all_chunks)

        job.chunks_total = chunks_total
        db.commit()

        chunk_errors = []

        # Cost cap: abort if a single job exceeds this limit (saves money on runaway jobs)
        COST_CAP_USD = float(os.getenv("JOB_COST_CAP_USD", "0.50"))  # ~₹42 default

        # ── Step 3a: image / excel — single combined pass (always 1 chunk) ──
        if doc_type in ("image", "excel"):
            try:
                current_cost = get_accumulated_cost()
                if current_cost > COST_CAP_USD:
                    logger.warning(f"[worker] Cost cap hit (${current_cost:.3f} > ${COST_CAP_USD}) before OCR — aborting job={job_id}")
                    chunk_errors.append(f"Cost cap exceeded: ${current_cost:.3f}")
                else:
                    final_state = extraction_pipeline.invoke({**base_state, "doc_type": doc_type, "chunks": all_chunks})
                    job.chunks_done = chunks_total
                    db.commit()
                    chunk_errors.extend(final_state.get("errors", []))
            except Exception as e:
                logger.error(f"[worker] Combined pass failed for job={job_id}: {e}", exc_info=True)
                chunk_errors.append(str(e))

        # ── Step 3b: scanned_pdf / digital_pdf — one page at a time ──────
        # Process each page individually so:
        #  • chunks_done updates after every page → progress bar moves in UI
        #  • if one page fails the rest still get processed and saved
        #  • cost cap is checked between pages
        # Each page is saved immediately. If page 7 of 10 fails, pages 1-6
        # and 8-10 are still processed and saved.
        #
        # IMPORTANT: we bypass the classify_node here because LangGraph always
        # runs classify first and it would re-detect ALL pages, undoing the
        # per-chunk split. Instead we call OCR → extract → validate → save
        # directly with a pre-classified single-chunk state.
        elif doc_type in ("scanned_pdf", "digital_pdf"):
            from agents.ocr import ocr_node as ocr_fn
            from agents.extractor import extract_node as extract_fn
            from agents.validator import validate_node as validate_fn
            from agents.pipeline import _route_and_save

            for i, chunk in enumerate(all_chunks):
                page_label = chunk.get("page_range", str(i + 1))
                # Check cost cap before each page
                current_cost = get_accumulated_cost()
                if current_cost > COST_CAP_USD:
                    logger.warning(f"[worker] Cost cap hit (${current_cost:.3f}) at page {page_label} — stopping early, saving {i} pages processed")
                    chunk_errors.append(f"Cost cap exceeded at page {page_label}")
                    job.chunks_done = i
                    db.commit()
                    break
                try:
                    logger.info(f"[worker] Processing page {page_label} ({i+1}/{chunks_total}) job={job_id}")

                    # Build a single-chunk state — skip classify entirely
                    chunk_state = {
                        **base_state,
                        "doc_type":           doc_type,
                        "chunks":             [chunk],
                        "extracted_invoices": [],
                        "errors":             [],
                    }
                    chunk_state = ocr_fn(chunk_state)
                    chunk_state = extract_fn(chunk_state)
                    chunk_state = validate_fn(chunk_state)
                    chunk_state = _route_and_save(chunk_state)
                    chunk_errors.extend(chunk_state.get("errors", []))
                    logger.info(f"[worker] Page {page_label} saved {chunk_state.get('saved_invoice_count', 0)} invoice(s)")

                except Exception as e:
                    err_str = str(e)
                    is_billing = any(kw in err_str for kw in (
                        "credit balance", "billing", "quota", "insufficient_quota", "UNAUTHENTICATED"
                    ))
                    if is_billing:
                        # Abort immediately — no point hammering 60 pages when credits are gone
                        logger.error(f"[worker] BILLING ERROR — aborting job {job_id}: {e}")
                        chunk_errors.append(f"Anthropic credits exhausted — job aborted at page {page_label}")
                        try:
                            job.status = "error"
                            job.error_message = "Anthropic credit balance too low. Please top up at console.anthropic.com."
                            job.chunks_done = i + 1
                            db.commit()
                        except Exception:
                            db.rollback()
                        return  # Exit process_document entirely
                    logger.error(f"[worker] Page {page_label} failed, continuing: {e}", exc_info=True)
                    chunk_errors.append(f"page {page_label}: {e}")
                    # CRITICAL: rollback the broken transaction so the next page
                    # gets a clean session — without this, ALL subsequent pages fail
                    try:
                        db.rollback()
                    except Exception:
                        pass

                finally:
                    # Always advance progress, even on failure
                    job.chunks_done = i + 1
                    try:
                        db.commit()
                    except Exception:
                        db.rollback()

        # ── Step 4: update cost + mark done ────────────────
        total_cost  = get_accumulated_cost()
        claude_cost = get_accumulated_claude_cost()
        docai_cost  = get_accumulated_docai_cost()
        doc = db.query(Document).filter(Document.id == document_id).first()
        if doc:
            doc.cost_usd        = total_cost
            doc.claude_cost_usd = claude_cost
            doc.docai_cost_usd  = docai_cost
            doc.status          = DocumentStatus.done
            db.commit()

        job.status = JobStatusEnum.done
        db.commit()

        if chunk_errors:
            logger.warning(f"[worker] Job {job_id} done with warnings: {chunk_errors}")
        else:
            logger.info(f"[worker] Job {job_id} completed successfully. Cost=${total_cost:.4f}")

        # ── Step 5: WhatsApp completion reply ───────────────
        # Only fires when the upload came from WhatsApp (web uploads have whatsapp_sender="")
        if whatsapp_sender:
            try:
                from agents.whatsapp_sender import send_whatsapp, format_completion_message
                from models.db import Invoice

                # Aggregate totals from all invoices extracted for this document
                invoices = db.query(Invoice).filter(Invoice.document_id == document_id).all()
                inv_count     = len(invoices)
                taxable_total = sum(i.taxable_value or 0 for i in invoices)
                gst_total     = sum(i.total_gst    or 0 for i in invoices)
                inv_total     = sum(i.invoice_total or 0 for i in invoices)

                msg = format_completion_message(
                    client_name=client_name or "there",
                    invoice_count=inv_count,
                    taxable_total=taxable_total,
                    gst_total=gst_total,
                    invoice_total=inv_total,
                    has_errors=bool(chunk_errors) or inv_count == 0,
                )
                send_whatsapp(whatsapp_sender, msg)
                logger.info(f"[worker] WhatsApp completion reply sent to {whatsapp_sender}")
            except Exception as wa_err:
                # Never let a WhatsApp glitch affect job completion status
                logger.error(f"[worker] WhatsApp reply failed (non-fatal): {wa_err}")

    except Exception as exc:
        logger.error(f"[worker] Job {job_id} failed: {exc}", exc_info=True)
        if job:
            job.status        = JobStatusEnum.error
            job.error_message = str(exc)[:500]
            db.commit()
            doc = db.query(Document).filter(Document.id == document_id).first()
            if doc:
                doc.status = DocumentStatus.error
                db.commit()

    finally:
        db.close()


# ── Task 2: Bank statement pipeline ────────────────────────

@celery_app.task(
    bind=True,
    name="complai.process_bank_statement",
    max_retries=0,   # no auto-retry — task is NOT idempotent (inserts rows on each run)
)
def process_bank_statement(
    self,
    job_id: str,
    file_path: str,
    document_id: int,
    client_id: int,
    firm_id: int,
    month_year: str,
    whatsapp_sender: str = "",   # set when upload came from WhatsApp
    client_name: str = "",
    media_url: str = "",         # Twilio media URL to download from
    account_sid: str = "",       # Twilio Basic Auth user
):
    """
    Bank statement extraction pipeline (no LangGraph — simpler flow).

    Steps:
      0. Download file from Twilio if WhatsApp upload (media_url set)
      1. Extract transactions + statement metadata (pdfplumber tables → Claude Haiku)
      2. Save BankStatementMeta record (account info, opening/closing balance, balance validation)
      3. Classify voucher types (receipt/payment/contra/journal)
      4. Fuzzy-match party names against known vendors
      5. Save BankTransaction records (with reference + mode fields)
      6. Send WhatsApp reply with summary (if WhatsApp upload)
    """
    from models.db import (
        BankTransaction, BankStatementMeta, Document, DocumentStatus,
        JobStatus, JobStatusEnum, VoucherType,
    )
    from utils.fuzzy import find_best_match, get_known_names_for_firm

    db = _get_db_session()
    job = None

    try:
        job = db.query(JobStatus).filter(JobStatus.id == job_id).first()
        if not job:
            return

        # ── Step 0: Download media file (WhatsApp uploads only) ──
        if media_url and not Path(file_path).exists():
            logger.info(f"[worker/bank] Downloading media from Twilio for job={job_id}")
            import httpx as _httpx
            _token = os.getenv("TWILIO_AUTH_TOKEN", "")
            try:
                with _httpx.Client(timeout=120.0) as _hclient:
                    resp = _hclient.get(
                        media_url,
                        auth=(account_sid, _token),
                        follow_redirects=True,
                    )
                    resp.raise_for_status()
                Path(file_path).parent.mkdir(parents=True, exist_ok=True)
                Path(file_path).write_bytes(resp.content)
                logger.info(f"[worker/bank] Downloaded {len(resp.content):,} bytes → {file_path}")
            except Exception as dl_err:
                logger.error(f"[worker/bank] Media download failed for job={job_id}: {dl_err}")
                job.status        = JobStatusEnum.error
                job.error_message = f"File download failed: {dl_err}"
                db.commit()
                if whatsapp_sender:
                    try:
                        from agents.whatsapp_sender import send_whatsapp as _send_wa
                        _send_wa(whatsapp_sender,
                                 "⚠️ We had trouble downloading your file. Please try sending it again.")
                    except Exception:
                        pass
                return

        job.status = JobStatusEnum.processing
        db.commit()

        # ── Step 1: Extract transactions + statement metadata ──
        transactions, stmt_meta = _extract_bank_transactions(file_path)
        job.chunks_total = len(transactions)
        db.commit()

        # ── Step 2: Save BankStatementMeta ─────────────────────
        total_credits = sum(t.get("credit") or 0 for t in transactions)
        total_debits  = sum(t.get("debit")  or 0 for t in transactions)

        # Normalise opening/closing: Claude sometimes returns formatted strings
        # like "9,000.75" instead of a plain float. _parse_amount strips commas.
        opening  = _parse_amount(stmt_meta.get("opening_balance"))
        closing  = _parse_amount(stmt_meta.get("closing_balance"))
        bal_ok   = None
        if opening is not None and closing is not None:
            expected = round(opening + total_credits - total_debits, 2)
            bal_ok   = abs(expected - closing) < 1.0
            logger.info(
                f"[bank] Balance check: {opening} + {total_credits} - {total_debits} = {expected} "
                f"vs closing {closing} → {'OK' if bal_ok else 'MISMATCH'}"
            )

        # Upsert: update existing row if document was re-processed, else insert
        meta_row = db.query(BankStatementMeta).filter(
            BankStatementMeta.document_id == document_id
        ).first()

        meta_values = dict(
            client_id       = client_id,
            firm_id         = firm_id,
            account_number  = stmt_meta.get("account_number"),
            account_holder  = stmt_meta.get("account_holder"),
            bank_name       = stmt_meta.get("bank_name"),
            ifsc_code       = stmt_meta.get("ifsc_code"),
            period_from     = _parse_date(stmt_meta.get("period_from")),
            period_to       = _parse_date(stmt_meta.get("period_to")),
            opening_balance = opening,
            closing_balance = closing,
            total_credits   = total_credits,
            total_debits    = total_debits,
            balance_matches = bal_ok,
            confidence      = stmt_meta.get("confidence", 0.8),
        )

        if meta_row:
            for k, v in meta_values.items():
                setattr(meta_row, k, v)
        else:
            meta_row = BankStatementMeta(document_id=document_id, **meta_values)
            db.add(meta_row)

        db.commit()

        # ── Step 3: Delete existing transactions for this document ──
        # This prevents duplicates when the same file is re-uploaded.
        deleted = db.query(BankTransaction).filter(
            BankTransaction.document_id == document_id
        ).delete(synchronize_session=False)
        if deleted:
            logger.info(f"[bank] Deleted {deleted} old transactions for doc {document_id} before re-insert")
        db.commit()

        # ── Step 4: Classify + fuzzy match ─────────────────────
        known_names = get_known_names_for_firm(firm_id, client_id, db)

        for i, tx in enumerate(transactions):
            # Classify voucher type
            voucher_type = _classify_voucher(tx)

            # Fuzzy match narration against known vendor names
            narration = tx.get("narration", "")
            matched_name, score, needs_review = find_best_match(narration, known_names)

            # Save to DB (includes new reference + mode fields)
            bank_tx = BankTransaction(
                client_id       = client_id,
                firm_id         = firm_id,
                document_id     = document_id,   # track source doc for deduplication
                transaction_date= tx.get("date"),
                narration       = narration,
                reference       = tx.get("reference"),
                mode            = tx.get("mode"),
                debit           = tx.get("debit"),
                credit          = tx.get("credit"),
                balance         = tx.get("balance"),
                voucher_type    = voucher_type,
                matched_ledger  = matched_name,
                match_confirmed = False,   # CA must confirm
                month_year      = month_year,
            )
            db.add(bank_tx)

            job.chunks_done = i + 1
            if (i + 1) % 10 == 0:
                db.commit()   # batch commits every 10 rows

        # ── Step 5: Finalize ───────────────────────────────────
        doc = db.query(Document).filter(Document.id == document_id).first()
        if doc:
            doc.status = DocumentStatus.done

        job.status = JobStatusEnum.done
        db.commit()
        logger.info(f"[worker] Bank job {job_id} done: {len(transactions)} transactions, balance_ok={bal_ok}")

        # ── Step 6: WhatsApp completion reply ──────────────────
        if whatsapp_sender:
            try:
                from agents.whatsapp_sender import send_whatsapp as _send_wa
                credits = sum(t.get("credit") or 0 for t in transactions)
                debits  = sum(t.get("debit")  or 0 for t in transactions)
                bal_line = ""
                if bal_ok is True:
                    bal_line = "\n✅ Opening → Closing balance verified"
                elif bal_ok is False:
                    bal_line = "\n⚠️ Balance mismatch — please check manually"
                name = client_name or "there"
                msg = (
                    f"🏦 Bank statement processed, {name}!\n\n"
                    f"📊 *{len(transactions)} transactions* found\n"
                    f"  Credits: ₹{credits:,.2f}\n"
                    f"  Debits:  ₹{debits:,.2f}"
                    f"{bal_line}\n\n"
                    "Login to the dashboard to review and export. 📋"
                )
                _send_wa(whatsapp_sender, msg)
                logger.info(f"[worker] WhatsApp bank reply sent to {whatsapp_sender}")
            except Exception as wa_err:
                logger.warning(f"[worker] WhatsApp bank reply failed: {wa_err}")

    except Exception as exc:
        logger.error(f"[worker] Bank job {job_id} failed: {exc}", exc_info=True)
        # Close the broken session first, then use a FRESH session to write
        # the error status — avoids PendingRollbackError swallowing the write
        try:
            db.close()
        except Exception:
            pass
        try:
            fresh_db = _get_db_session()
            err_job = fresh_db.query(JobStatus).filter(JobStatus.id == job_id).first()
            if err_job:
                err_job.status        = JobStatusEnum.error
                err_job.error_message = str(exc)[:500]
                fresh_db.commit()
            fresh_db.close()
        except Exception as inner:
            logger.error(f"[worker] Could not save error status for bank job {job_id}: {inner}")
        return   # skip the finally db.close() since we already closed it

    finally:
        try:
            db.close()
        except Exception:
            pass


def _extract_bank_transactions(file_path: str):
    """
    Extract rows from a bank statement PDF or Excel.
    Returns (transactions, statement_meta) where:
      transactions  — list of {date, narration, reference, mode, debit, credit, balance}
      statement_meta — dict with account info, opening/closing balance, period, etc.
    """
    ext = Path(file_path).suffix.lower()

    if ext in (".xlsx", ".csv"):
        txs, meta = _extract_bank_excel(file_path)
    elif ext == ".pdf":
        txs, meta = _extract_bank_pdf(file_path)
    else:
        logger.warning(f"[bank] Unknown file type: {ext}")
        txs, meta = [], {}

    return txs, meta


def _read_xls_or_html(file_path: str, header_row_idx: int):
    """
    Many Indian banks (ICICI, Axis, HDFC) export their 'Excel' statements
    as HTML tables saved with a .xls extension. xlrd cannot read these.

    Strategy:
      1. Peek at the first 512 bytes — if it looks like HTML, use read_html()
      2. Otherwise use xlrd (true binary XLS)
      3. Fallback: try openpyxl (some .xls are actually OOXML)
    """
    import pandas as pd

    # Peek at file header to detect HTML
    try:
        with open(file_path, "rb") as f:
            magic = f.read(512).lstrip()
        is_html = magic[:1] in (b"<", b"\xef")  # HTML or UTF-8 BOM + HTML
        if not is_html:
            # Also check for common HTML strings even if BOM-prefixed
            try:
                is_html = b"<html" in magic.lower() or b"<!doctype" in magic.lower()
            except Exception:
                pass
    except Exception:
        is_html = False

    logger.info(f"[bank:excel] .xls detected as {'HTML' if is_html else 'binary XLS'}")

    if is_html:
        # Parse as HTML — read_html returns a list of all tables found
        for enc in ("utf-8", "cp1252", "latin-1"):
            try:
                tables = pd.read_html(file_path, header=0, encoding=enc,
                                      flavor="html5lib")
                if not tables:
                    tables = pd.read_html(file_path, header=0, encoding=enc)
                break
            except Exception:
                try:
                    tables = pd.read_html(file_path, header=0, encoding=enc)
                    break
                except Exception:
                    continue
        else:
            raise ValueError("Could not parse HTML-disguised XLS file")

        # Pick the largest table (most likely the transaction table)
        df = max(tables, key=len)
        logger.info(f"[bank:excel] HTML tables found: {len(tables)}, using largest ({len(df)} rows)")
        return df

    # True binary XLS
    try:
        return pd.read_excel(file_path, engine="xlrd",
                             header=header_row_idx,
                             na_values=["", " ", "-", "--"])
    except Exception as e:
        logger.warning(f"[bank:excel] xlrd failed ({e}), trying openpyxl")
        return pd.read_excel(file_path, engine="openpyxl",
                             header=header_row_idx,
                             na_values=["", " ", "-", "--"])


def _extract_bank_excel(file_path: str):
    """
    Parse bank statement from Excel (.xlsx / .xls) or CSV.

    Key challenge: Excel stores dates as serial numbers internally.
    When read with dtype=str they become "46923.0" instead of a date.
    We read WITHOUT dtype=str so pandas converts dates to datetime objects,
    then handle both datetime objects and string dates in _parse_date_cell().

    Engine:
      .csv  → pandas read_csv
      .xlsx → openpyxl
      .xls  → xlrd  (ICICI Bank, older portals)

    Header detection: banks often put 2–10 rows of account info above the
    actual column headers. We scan for the first row with ≥2 keyword matches.

    Returns (transactions, stmt_meta) where stmt_meta has account info
    extracted from the top rows of the file.
    """
    import pandas as pd
    import re as _re
    from datetime import datetime as _dt, date as _date

    ext = Path(file_path).suffix.lower()

    def _norm_col(c):
        """Normalise a column header for matching."""
        c = str(c).lower()
        c = c.replace("\n", " ").replace("\r", " ")
        c = _re.sub(r"[.()\[\]/]", " ", c)
        c = _re.sub(r"\s+", " ", c).strip()
        return c

    def _parse_date_cell(val):
        """
        Convert an Excel cell value to a Python date.
        Handles: datetime objects, date objects, float serial numbers,
        and string dates in any Indian bank format.
        """
        if val is None:
            return None
        # Already a date/datetime (pandas converts Excel dates automatically)
        if isinstance(val, _dt):
            return val.date()
        if isinstance(val, _date):
            return val
        # Excel serial number stored as float (happens with some read modes)
        try:
            f = float(val)
            if 30000 < f < 60000:   # plausible Excel date range (1982–2064)
                import xlrd as _xlrd
                tup = _xlrd.xldate_as_tuple(f, 0)   # datemode 0 = 1900-based
                return _date(tup[0], tup[1], tup[2])
        except (ValueError, TypeError):
            pass
        # String date — fall through to _parse_date
        return _parse_date(str(val))

    def _parse_amount_cell(val):
        """Convert a cell value (float, int, or string) to a float amount."""
        if val is None:
            return None
        if isinstance(val, (int, float)):
            return float(val) if val != 0 else None
        return _parse_amount(str(val))

    HEADER_KEYWORDS = {
        "date", "narration", "description", "particulars",
        "withdrawal", "deposit", "debit", "credit", "balance",
        "remarks", "amount", "txn", "transaction", "cheque",
    }

    try:
        # ── Step 1: Raw read to detect header row & extract metadata ──
        if ext == ".csv":
            for enc in ("utf-8", "cp1252", "latin-1"):
                try:
                    df_raw = pd.read_csv(file_path, header=None, dtype=str, encoding=enc)
                    break
                except UnicodeDecodeError:
                    continue
            else:
                df_raw = pd.read_csv(file_path, header=None, dtype=str, errors="replace")
        elif ext == ".xlsx":
            df_raw = pd.read_excel(file_path, header=None, dtype=str, engine="openpyxl")
        elif ext == ".xls":
            # Read raw with dtype=str first just to scan for header row & meta
            df_raw = pd.read_excel(file_path, header=None, dtype=str, engine="xlrd")
        else:
            logger.warning(f"[bank:excel] Unknown extension: {ext}")
            return [], {}

        # ── Step 2: Find header row ────────────────────────────
        header_row_idx = 0
        meta_lines = []   # rows before the header = account info

        for idx in range(min(20, len(df_raw))):
            row = df_raw.iloc[idx]
            cells = [str(c).lower().strip() for c in row if pd.notna(c) and str(c).strip()]
            meta_lines.append(" ".join(cells))
            matched = sum(1 for c in cells if any(kw in c for kw in HEADER_KEYWORDS))
            if matched >= 2:
                header_row_idx = idx
                break

        logger.info(f"[bank:excel] Header row detected at index {header_row_idx}")

        # ── Step 3: Extract statement metadata from top rows ───
        meta_text = "\n".join(meta_lines)
        stmt_meta = {}
        # Account number: 9–18 digit sequence
        acno = _re.search(r'\b(\d{9,18})\b', meta_text)
        if acno:
            stmt_meta["account_number"] = acno.group(1)
        # Bank name heuristic
        for bname in ["icici", "hdfc", "sbi", "axis", "kotak", "jk bank", "pnb",
                       "bob", "canara", "union bank", "indusind", "yes bank"]:
            if bname in meta_text.lower():
                stmt_meta["bank_name"] = bname.upper().replace("ICICI", "ICICI Bank")
                break
        # Period: look for "from ... to ..." or date ranges
        period = _re.search(
            r'(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s*(?:to|-)\s*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})',
            meta_text, _re.IGNORECASE
        )
        if period:
            stmt_meta["period_from"] = period.group(1).replace(".", "/").replace("-", "/")
            stmt_meta["period_to"]   = period.group(2).replace(".", "/").replace("-", "/")

        logger.info(f"[bank:excel] Statement meta from header rows: {stmt_meta}")

        # ── Step 4: Re-read with proper header & NO dtype=str ─
        # Without dtype=str, pandas converts Excel date cells to datetime.
        read_kwargs = dict(header=header_row_idx, na_values=["", " ", "-", "--"])
        if ext == ".csv":
            for enc in ("utf-8", "cp1252", "latin-1"):
                try:
                    df = pd.read_csv(file_path, encoding=enc, **read_kwargs)
                    break
                except UnicodeDecodeError:
                    continue
            else:
                df = pd.read_csv(file_path, errors="replace", **read_kwargs)
        elif ext == ".xlsx":
            df = pd.read_excel(file_path, engine="openpyxl", **read_kwargs)
        else:
            # .xls — ICICI and many Indian banks export HTML tables with a
            # .xls extension. xlrd cannot read HTML. Detect and handle both.
            df = _read_xls_or_html(file_path, header_row_idx)

        # ── Step 5: Normalise column names ────────────────────
        df.columns = [_norm_col(c) for c in df.columns]
        logger.info(f"[bank:excel] Columns: {list(df.columns)}")

        # ── Step 6: Map to canonical names ────────────────────
        COL_VARIANTS = {
            "date":      ["date", "txn date", "value date", "transaction date",
                          "posting date", "tran date", "book date", "s no"],
            "narration": ["narration", "description", "particulars",
                          "transaction remarks", "remarks", "details",
                          "transaction description", "transaction detail"],
            "reference": ["cheque number", "cheque no", "chq no", "ref no",
                          "reference", "instrument no", "utr"],
            "debit":     ["withdrawal amount inr", "withdrawal amount",
                          "withdrawal", "debit", "dr amount", "debit amount",
                          "dr"],
            "credit":    ["deposit amount inr", "deposit amount",
                          "deposit", "credit", "cr amount", "credit amount",
                          "cr"],
            "balance":   ["balance inr", "balance", "running balance",
                          "available balance", "closing balance", "bal"],
        }

        col_lookup = {}
        for canonical, variants in COL_VARIANTS.items():
            for col in df.columns:
                col_s = col.strip()
                if col_s in variants or any(v == col_s or (len(v) > 4 and v in col_s) for v in variants):
                    if canonical not in col_lookup:   # first match wins
                        col_lookup[canonical] = col
                        break

        # Fallback: if "date" not found, look for first column whose values
        # look like dates (datetime objects from pandas)
        if "date" not in col_lookup:
            for col in df.columns:
                sample = df[col].dropna().head(5)
                if any(isinstance(v, (_dt, _date)) for v in sample):
                    col_lookup["date"] = col
                    break

        logger.info(f"[bank:excel] Column mapping: {col_lookup}")
        if "date" not in col_lookup:
            logger.warning("[bank:excel] Date column not found — transactions will be empty")

        # ── Step 7: Extract transactions ──────────────────────
        transactions = []
        for _, row in df.iterrows():
            tx = {}
            for canonical, col in col_lookup.items():
                val = row.get(col)
                if pd.isna(val) if not isinstance(val, (_dt, _date)) else False:
                    val = None

                if canonical == "date":
                    tx["date"] = _parse_date_cell(val)
                elif canonical in ("debit", "credit", "balance"):
                    tx[canonical] = _parse_amount_cell(val)
                else:
                    tx[canonical] = str(val).strip() if val is not None else None

            if tx.get("date"):
                transactions.append(tx)

        logger.info(f"[bank:excel] Extracted {len(transactions)} transactions")
        return transactions, stmt_meta

    except Exception as e:
        logger.error(f"[bank:excel] Failed: {e}", exc_info=True)
        return [], {}


def _extract_bank_pdf(file_path: str):
    """
    Parse bank statement from PDF.

    Strategy (in order):
      1. pdfplumber table extraction — with multiple settings/strategies
      2. pdfplumber text extraction  — column-aware line-by-line parsing
      3. Claude Haiku text parsing   — handles any Indian bank format
         (JK Bank, HDFC, SBI, ICICI, Axis, Kotak, IndusInd, etc.)

    Returns (transactions, statement_meta) where statement_meta contains
    account info, period, opening/closing balances.
    """
    import pdfplumber

    transactions = []
    stmt_meta    = {}

    try:
        with pdfplumber.open(file_path) as pdf:
            num_pages = len(pdf.pages)
            logger.info(f"[bank:pdf] Opened PDF: {num_pages} pages")

            # ── Collect page texts with different tolerances ───────────────────
            # Use wider y_tolerance so that bank statement rows (close-spaced)
            # don't get merged into a single line.
            page_texts = []
            for i, page in enumerate(pdf.pages):
                # Try different tolerance settings — wider y_tolerance groups
                # characters into lines better for dense bank statement layouts
                text = (
                    page.extract_text(x_tolerance=2, y_tolerance=2)
                    or page.extract_text(x_tolerance=5, y_tolerance=5)
                    or ""
                )
                if text.strip():
                    page_texts.append(f"[Page {i+1}]\n{text}")

            logger.info(f"[bank:pdf] Text extraction: {len(page_texts)}/{num_pages} pages with text")

            # ── Extract statement-level metadata ───────────────────────────────
            if page_texts:
                first_pages = page_texts[:2]
                last_pages  = page_texts[-2:] if len(page_texts) > 2 else []
                meta_pages  = first_pages + [p for p in last_pages if p not in first_pages]
                meta_text   = "\n\n".join(meta_pages)
                stmt_meta   = _extract_statement_meta_with_claude(meta_text)
                logger.info(f"[bank:pdf] Statement meta: {stmt_meta}")

            # ── Pass 1: table extraction (multiple pdfplumber strategies) ──────
            # Indian bank PDFs vary a lot — try several table settings
            TABLE_SETTINGS = [
                {},                                                         # default
                {"vertical_strategy": "text", "horizontal_strategy": "text"},   # text-based borders
                {"vertical_strategy": "lines_strict", "horizontal_strategy": "lines_strict"},  # strict lines
                {"snap_tolerance": 5, "join_tolerance": 5},                # more tolerant snapping
            ]

            table_txs = []
            for page in pdf.pages:
                for settings in TABLE_SETTINGS:
                    try:
                        tables = page.extract_tables(table_settings=settings) if settings else page.extract_tables()
                    except Exception:
                        continue

                    for table in tables:
                        if not table or len(table) < 2:
                            continue
                        # Find header row — the first row that looks like a bank statement header
                        header_row_idx = 0
                        headers = None
                        for row_idx, row in enumerate(table[:5]):   # check first 5 rows for header
                            candidate = [str(h or "").lower().strip() for h in row]
                            candidate_str = " ".join(candidate)
                            if any(kw in candidate_str for kw in [
                                "date", "narration", "debit", "credit", "balance",
                                "particular", "description", "withdrawal", "deposit",
                                "dr", "cr", "chq", "amount",
                            ]):
                                headers = candidate
                                header_row_idx = row_idx
                                break

                        if not headers:
                            continue

                        for row in table[header_row_idx + 1:]:
                            tx = _parse_bank_row(headers, row)
                            if tx and tx.get("date"):
                                table_txs.append(tx)

                    if table_txs:
                        break   # found tables on this page with these settings

            # Deduplicate by (date, narration, amount)
            seen = set()
            deduped = []
            for tx in table_txs:
                key = (tx.get("date"), tx.get("narration", "")[:30], tx.get("debit"), tx.get("credit"))
                if key not in seen:
                    seen.add(key)
                    deduped.append(tx)
            table_txs = deduped

            if table_txs:
                logger.info(f"[bank:pdf] Table extraction: {len(table_txs)} transactions")
                return table_txs, stmt_meta

            # ── Pass 2: Claude text parsing ────────────────────────────────────
            # Most Indian bank PDFs (SBI, JK Bank, BOB etc.) use space-delimited
            # text, not real table borders. Claude can read any format.
            logger.info("[bank:pdf] No tables found — falling back to Claude text parsing")

            if not page_texts:
                logger.warning("[bank:pdf] No text extracted at all (possibly scanned PDF)")
                # Last resort: try Claude Vision on each page image
                all_txs = _parse_bank_pdf_with_vision(file_path)
                logger.info(f"[bank:pdf] Vision fallback: {len(all_txs)} transactions")
                return all_txs, stmt_meta

            # Send to Claude in chunks of 3 pages
            PAGES_PER_CHUNK = 3
            all_txs = []
            for chunk_start in range(0, len(page_texts), PAGES_PER_CHUNK):
                chunk = page_texts[chunk_start: chunk_start + PAGES_PER_CHUNK]
                chunk_text = "\n\n".join(chunk)
                chunk_txs = _parse_bank_text_with_claude(chunk_text)
                all_txs.extend(chunk_txs)
                logger.info(
                    f"[bank:pdf] Claude text pages {chunk_start+1}–"
                    f"{chunk_start+len(chunk)}: {len(chunk_txs)} transactions"
                )

            if not all_txs:
                logger.warning("[bank:pdf] Claude text parsing returned 0 transactions")

            transactions = all_txs
            logger.info(f"[bank:pdf] Claude total: {len(transactions)} transactions")

    except Exception as e:
        logger.error(f"[bank:pdf] Failed: {e}", exc_info=True)

    return transactions, stmt_meta


def _parse_bank_pdf_with_vision(file_path: str) -> list:
    """
    Last-resort fallback for scanned/image-only bank PDFs.
    Converts each page to an image and sends to Claude Vision.
    """
    import anthropic
    import base64
    import json
    import re

    try:
        import fitz  # PyMuPDF
    except ImportError:
        logger.warning("[bank:vision] PyMuPDF not installed — cannot do vision fallback")
        return []

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    all_txs = []

    try:
        doc = fitz.open(file_path)
        for page_num in range(len(doc)):
            page = doc[page_num]
            pix  = page.get_pixmap(dpi=150)
            img_bytes = pix.tobytes("png")
            b64_img   = base64.b64encode(img_bytes).decode()

            prompt = """Extract ALL bank transactions visible in this bank statement page image.
Return a JSON array with objects:
{"date":"DD/MM/YYYY","narration":"full text","reference":null,"mode":null,"debit":null_or_number,"credit":null_or_number,"balance":null_or_number}
Skip header rows, page totals, and blank rows. Return ONLY the JSON array."""

            try:
                resp = client.messages.create(
                    model="claude-sonnet-4-5",
                    max_tokens=4000,
                    messages=[{"role": "user", "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64_img}},
                        {"type": "text",  "text": prompt},
                    ]}],
                )
                raw  = resp.content[0].text.strip()
                raw  = re.sub(r"^```(?:json)?\s*", "", raw)
                raw  = re.sub(r"\s*```$", "", raw)
                rows = json.loads(raw)
                if isinstance(rows, list):
                    for item in rows:
                        d = _parse_date(item.get("date", ""))
                        if d:
                            all_txs.append({
                                "date":      d,
                                "narration": str(item.get("narration") or "").strip(),
                                "reference": str(item.get("reference") or "").strip() or None,
                                "mode":      str(item.get("mode") or "").upper() or None,
                                "debit":     _parse_amount(item.get("debit")),
                                "credit":    _parse_amount(item.get("credit")),
                                "balance":   _parse_amount(item.get("balance")),
                            })
                logger.info(f"[bank:vision] Page {page_num+1}: {len(all_txs)} cumulative transactions")
            except Exception as page_err:
                logger.error(f"[bank:vision] Page {page_num+1} failed: {page_err}")

        doc.close()
    except Exception as e:
        logger.error(f"[bank:vision] Vision extraction failed: {e}", exc_info=True)

    return all_txs


def _extract_statement_meta_with_claude(text: str) -> dict:
    """
    Extract statement-level metadata from the first 1-2 pages of a bank statement.
    Returns dict: {account_number, account_holder, bank_name, ifsc_code,
                   period_from, period_to, opening_balance, closing_balance, confidence}
    """
    import anthropic
    import json
    import re

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    prompt = """Extract bank statement header and summary information from this text.
Return a single JSON object (not an array):
{
  "account_number":  "account number string or null",
  "account_holder":  "account holder / customer name or null",
  "bank_name":       "bank name (e.g. J&K Bank, HDFC Bank, SBI) or null",
  "ifsc_code":       "IFSC code (11-char alphanumeric, e.g. JAKA0EOSJAM) or null",
  "period_from":     "DD/MM/YYYY — start date of the statement period or null",
  "period_to":       "DD/MM/YYYY — end date of the statement period or null",
  "opening_balance": <number or null>,
  "closing_balance": <number or null>
}

How to find opening/closing balance:
- If the statement shows "Opening Balance: X" explicitly, use that.
- Many Indian bank PDFs (e.g. JK Bank) do NOT show opening balance explicitly.
  In that case look for a "Grand Total" line at the end, e.g.:
  "Grand Total : 383,570.35  375,548.00  2,097.40Cr"
  This means: Total Withdrawals | Total Deposits | Closing Balance
  Then compute: opening_balance = closing_balance + total_withdrawals - total_deposits
- The closing balance is the final running balance in the statement, often with Cr/Dr suffix.
- Remove Cr/Dr suffixes and commas before returning numbers.
- If closing balance shows "2,097.40Cr", return 2097.40 (positive = credit balance).

Rules:
- Amounts: remove commas and Cr/Dr suffix, return as plain positive numbers
- If you cannot find a field even after inference, return null
- Return ONLY the JSON object, no markdown

Statement text:
""" + text[:5000]

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )

        try:
            from utils.cost_tracker import track_claude_cost
            track_claude_cost(
                model="claude-haiku",
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )
        except Exception:
            pass

        raw = response.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        meta = json.loads(raw)
        meta["confidence"] = 0.85
        return meta

    except Exception as e:
        logger.error(f"[bank:meta] Statement meta extraction failed: {e}")
        return {}


def _parse_bank_text_with_claude(text: str) -> list:
    """
    Use Claude Haiku to extract bank transactions from any text format.
    Handles multi-line narrations, Dr/Cr suffixes, Indian number formatting.
    Returns list of {date, narration, reference, mode, debit, credit, balance} dicts.
    """
    import anthropic
    import json
    import re

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    prompt = """You are extracting bank transactions from an Indian bank statement text.
The text may be from SBI, HDFC, ICICI, Axis, JK Bank, BOB, PNB, Kotak, IndusInd, or any other Indian bank.
The text may be messy, have extra spaces, or have lines merged oddly due to PDF extraction.

Return a JSON array of ALL transactions. Each object:
{
  "date":      "date as DD/MM/YYYY (convert any format to DD/MM/YYYY)",
  "narration": "full narration/particulars — merge multi-line into one string",
  "reference": "cheque number OR UPI ref / UTR number (the long numeric part), or null",
  "mode":      "UPI | NEFT | RTGS | IMPS | CHQ | ATM | CASH | ECS | NACH | INT | null",
  "debit":     null or positive number  (money OUT — Withdrawals / Dr column),
  "credit":    null or positive number  (money IN  — Deposits / Cr column),
  "balance":   null or positive number  (running balance — strip Cr/Dr suffix)
}

Extraction rules:
1. A transaction row ALWAYS has a date, at least one amount (debit or credit), and a narration.
2. Do NOT skip any transaction — extract all of them including charges, fees, interest.
3. Amounts: remove commas (1,23,456.78 → 123456.78), remove Cr/Dr/Rs/₹ suffixes.
4. If balance shows "2,097.40Cr" that means positive balance — store as 2097.40.
5. If balance shows "500.00Dr" that means overdraft — store as 500.00 (positive number).
6. Mode detection: look for UPI/NEFT/RTGS/IMPS/ATM/CHQ/INT in the narration text.
7. UPI reference: extract the numeric UTR from narration like "UPI/426471291125/party/..." → "426471291125"
8. CHQ reference: if a CHQ.NO column exists or "Chq No" appears, put that value in reference.
9. Skip ONLY: table headers, page subtotals/grand totals, blank rows, account summary rows.
10. If a row has only narration text continuing from the row above (no date, no amount), merge it.

Return ONLY the JSON array — no markdown fences, no explanation.

Bank statement text:
""" + text

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=8000,
            messages=[{"role": "user", "content": prompt}],
        )

        # Track cost
        try:
            from utils.cost_tracker import track_claude_cost
            track_claude_cost(
                model="claude-haiku",
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )
        except Exception:
            pass

        raw = response.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            parsed = parsed.get("transactions", [])

        result = []
        for item in parsed:
            d = _parse_date(item.get("date", ""))
            if not d:
                continue
            result.append({
                "date":      d,
                "narration": str(item.get("narration") or "").strip(),
                "reference": str(item.get("reference") or "").strip() or None,
                "mode":      str(item.get("mode") or "").strip().upper() or None,
                "debit":     _parse_amount(item.get("debit")),
                "credit":    _parse_amount(item.get("credit")),
                "balance":   _parse_amount(item.get("balance")),
            })
        return result

    except Exception as e:
        logger.error(f"[bank:claude] Failed: {e}", exc_info=True)
        return []


def _parse_bank_row(headers, row):
    """
    Convert a PDF table row to a transaction dict.
    Handles many Indian bank column naming conventions, including ICICI Bank
    which uses dot-separated dates (01.04.2026) and multi-line cell headers
    like "Transaction\nDate" or "Withdrawal\nAmount (INR)".
    """
    tx = {}
    for i, header in enumerate(headers):
        if i >= len(row):
            break
        val = str(row[i] or "").strip()
        if not val:
            continue

        # Normalise header: strip newlines, punctuation, parentheses, extra spaces
        h = (header.lower()
             .replace("\n", " ").replace("\r", " ")
             .replace(".", "").replace("(", "").replace(")", "")
             .replace("_", " ").strip())
        # Collapse multiple spaces
        import re as _re2
        h = _re2.sub(r'\s+', ' ', h)

        # Date column — broad substring match handles variants like
        # "Transaction Date", "Txn Date", "Value Date", "Posting Date" etc.
        if "date" in h and "update" not in h:
            parsed = _parse_date(val)
            if parsed:
                tx["date"] = parsed

        # Narration / description column
        # ICICI Bank: "Transaction Remarks"
        # SBI/BOB: "Narration" or "Particulars"
        elif any(k in h for k in ["narration", "description", "particular",
                                   "remark", "detail", "transaction detail"]):
            tx["narration"] = val

        # Cheque / reference number
        elif any(k in h for k in ["chq", "cheque", "ref", "ref no", "instrument",
                                   "utr", "txn id", "transaction id"]):
            if not tx.get("reference"):
                tx["reference"] = val

        # Debit / withdrawal column
        # ICICI: "Withdrawal Amount (INR)" → normalised → "withdrawal amount inr"
        elif any(k in h for k in ["debit", "withdrawal", "drawn", "dr amount", "dr amt"]):
            amt = _parse_amount(val)
            if amt and amt > 0:
                tx["debit"] = amt

        # Credit / deposit column
        # ICICI: "Deposit Amount (INR)" → normalised → "deposit amount inr"
        elif any(k in h for k in ["credit", "deposit", "cr amount", "cr amt"]):
            amt = _parse_amount(val)
            if amt and amt > 0:
                tx["credit"] = amt

        # Balance column
        elif any(k in h for k in ["balance", "running bal", "available bal",
                                   "closing bal", " bal"]):
            tx["balance"] = _parse_amount(val)

        # Some banks combine debit/credit into one "Amount" column with Dr/Cr suffix
        elif h in ("amount", "amt"):
            val_lower = val.lower()
            amt = _parse_amount(val)
            if amt and amt > 0:
                if val_lower.endswith("dr") or "dr" in val_lower:
                    tx["debit"] = amt
                else:
                    tx["credit"] = amt

    # Some banks show a single "Withdrawals/Deposits" merged column
    # If only one amount and no debit/credit split, use balance direction to guess
    return tx


def _classify_voucher(tx: dict):
    """
    Classify a bank transaction into a voucher type:
      credit only           → receipt
      debit only            → payment
      both debit and credit → contra (same-bank transfer)
      narration has charges → journal
    """
    from models.db import VoucherType

    narration = (tx.get("narration") or "").lower()
    debit     = tx.get("debit")
    credit    = tx.get("credit")

    charge_keywords = ["charge", "interest", "fee", "penalty", "gst on charges"]
    if any(kw in narration for kw in charge_keywords):
        return VoucherType.journal

    if debit and credit:
        return VoucherType.contra

    if credit:
        return VoucherType.receipt

    if debit:
        return VoucherType.payment

    return VoucherType.journal   # fallback


def _parse_date(val):
    """
    Parse various Indian bank date formats.
    Covers formats used by SBI, HDFC, ICICI, Axis, JK Bank, BOB, PNB, Kotak etc.
    """
    from datetime import datetime
    import re as _re

    if not val:
        return None

    s = str(val).strip()
    # Strip trailing garbage like "Mon", "Tue" sometimes appended in exports
    s = _re.sub(r'\s+(Mon|Tue|Wed|Thu|Fri|Sat|Sun)$', '', s, flags=_re.IGNORECASE)
    # Normalize separators: "01.01.2025" → "01/01/2025"
    s = s.replace(".", "/")

    formats = [
        "%d/%m/%Y",    # 31/01/2025
        "%d-%m-%Y",    # 31-01-2025
        "%Y-%m-%d",    # 2025-01-31  (ISO)
        "%d/%m/%y",    # 31/01/25
        "%d-%m-%y",    # 31-01-25
        "%d %b %Y",    # 31 Jan 2025
        "%d-%b-%Y",    # 31-Jan-2025
        "%d/%b/%Y",    # 31/Jan/2025
        "%b %d, %Y",   # Jan 31, 2025
        "%B %d, %Y",   # January 31, 2025
        "%d %B %Y",    # 31 January 2025
        "%Y/%m/%d",    # 2025/01/31
    ]
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _parse_amount(val):
    """Parse amount strings like '1,23,456.78' or '1234.56 Dr'."""
    if not val:
        return None
    import re
    clean = re.sub(r"[,\s]", "", str(val))
    clean = re.sub(r"[A-Za-z]+$", "", clean)  # remove Dr/Cr suffix
    try:
        return float(clean) if clean else None
    except ValueError:
        return None


# ── Expose the Celery app for the CLI ─────────────────────
# Start with: celery -A workers.celery_app worker --loglevel=info
app = celery_app
