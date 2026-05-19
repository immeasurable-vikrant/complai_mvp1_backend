"""
ComplAI — LangGraph Pipeline (5-node StateGraph)
Orchestrates the full invoice extraction flow with LangSmith tracing.

Node execution order:
  classify_node → ocr_node → extract_node → validate_node → route_node

State object flows through each node, accumulating results.
LangSmith traces every document — set LANGCHAIN_TRACING_V2=true.
"""

from __future__ import annotations  # Python 3.9 compatibility for TypedDict + Any

import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

# TypedDict import: use typing_extensions for Python 3.9 compatibility
try:
    from typing import TypedDict
except ImportError:
    from typing_extensions import TypedDict

# LangGraph import — handle both old (0.1.x) and new (0.2.x) APIs
try:
    from langgraph.graph import StateGraph, END
except ImportError:
    from langgraph.graph import Graph as StateGraph  # fallback
    END = "__end__"

from agents.classifier import classify_document
from agents.ocr import ocr_node
from agents.extractor import extract_node
from agents.validator import validate_node
from utils.confidence import compute_combined_confidence

logger = logging.getLogger(__name__)


# ── State schema ───────────────────────────────────────────

class PipelineState(TypedDict):
    # Input
    file_path:   str
    file_type:   str
    document_id: int
    client_id:   int
    firm_id:     int

    # Set by classify_node
    doc_type: str
    chunks:   List[Dict]

    # Iteration counter
    current_chunk: int

    # Set by extract_node
    extracted_invoices: List[Dict]

    # Set by validate_node (same list, issues added)
    # extracted_invoices is updated in place

    # DB session (injected by Celery task, not part of graph data)
    db_session: Any

    # Accumulated errors
    errors: List[str]


# ── Node wrappers (add error handling around each agent) ────

def _safe_classify(state: PipelineState) -> PipelineState:
    """Node 1: Classify document type and split into chunks."""
    try:
        logger.info(f"[pipeline] classify_node — doc_id={state['document_id']}")
        return classify_document(state)
    except Exception as e:
        logger.error(f"[pipeline] classify_node failed: {e}")
        state.setdefault("errors", []).append(f"classify: {e}")
        state["doc_type"] = "digital_pdf"
        state["chunks"]   = [{"pages": [], "file_path": state["file_path"]}]
        return state


def _safe_ocr(state: PipelineState) -> PipelineState:
    """Node 2: Extract text from each chunk via 3-layer OCR."""
    try:
        logger.info(f"[pipeline] ocr_node — {len(state.get('chunks', []))} chunks")
        return ocr_node(state)
    except Exception as e:
        logger.error(f"[pipeline] ocr_node failed: {e}")
        state.setdefault("errors", []).append(f"ocr: {e}")
        return state


def _safe_extract(state: PipelineState) -> PipelineState:
    """Node 3: Send OCR text to Claude Haiku for structured extraction."""
    try:
        logger.info("[pipeline] extract_node")
        return extract_node(state)
    except Exception as e:
        logger.error(f"[pipeline] extract_node failed: {e}")
        state.setdefault("errors", []).append(f"extract: {e}")
        state.setdefault("extracted_invoices", [])
        return state


def _safe_validate(state: PipelineState) -> PipelineState:
    """Node 4: Validate GSTINs, check duplicates, verify totals."""
    try:
        logger.info(f"[pipeline] validate_node — {len(state.get('extracted_invoices', []))} invoices")
        return validate_node(state)
    except Exception as e:
        logger.error(f"[pipeline] validate_node failed: {e}")
        state.setdefault("errors", []).append(f"validate: {e}")
        return state


def _route_and_save(state: PipelineState) -> PipelineState:
    """
    Node 5: route_node
    Compute combined confidence, decide auto-accept vs review,
    then save all invoices to the database.

    Auto-accept rules:
      combined_confidence >= 0.80  AND  no critical issues → auto_accepted
      otherwise                                             → needs_review
    """
    from models.db import (
        ExtractionCorrection, Invoice, InvoiceStatus,
        JobStatus, JobStatusEnum, Document, DocumentStatus,
        TransactionType,
    )
    from datetime import date as date_type

    db  = state.get("db_session")
    if not db:
        logger.error("[pipeline] route_node: no db_session in state")
        return state

    invoices_data    = state.get("extracted_invoices", [])
    document_id      = state["document_id"]
    client_id        = state["client_id"]
    firm_id          = state["firm_id"]

    # Critical issues that block auto-acceptance
    critical_issue_keywords = {
        "Duplicate invoice",
        "Missing invoice number",
        "Missing invoice date",
        "Missing vendor name",
    }

    def _clean_gstin(raw: str | None) -> str | None:
        """Trim whitespace; if OCR produced >15 chars it's noise — keep as-is up to 20 chars."""
        if not raw:
            return None
        cleaned = raw.strip().upper()
        return cleaned[:20]  # hard cap at column width; validator will flag invalid format

    saved_count = 0
    for inv_data in invoices_data:
        # ── Confidence ─────────────────────────────────────
        ocr_conf    = float(inv_data.get("_ocr_confidence", 0.5))
        claude_conf = float(inv_data.get("confidence_score", 0.5))
        combined    = compute_combined_confidence(ocr_conf, claude_conf)

        # ── Decide status ──────────────────────────────────
        issues = inv_data.get("issues", [])
        is_placeholder = bool(inv_data.get("_placeholder", False))
        has_critical = is_placeholder or any(
            any(kw in issue for kw in critical_issue_keywords)
            for issue in issues
        )

        CONFIDENCE_TARGET = 0.75
        if is_placeholder:
            # Blank page / OCR failure — always flag for manual entry
            needs_review = True
            manual_review_required = True
            inv_status   = InvoiceStatus.needs_review
        elif combined >= 0.80 and not has_critical:
            needs_review = False
            manual_review_required = False
            inv_status   = InvoiceStatus.auto_accepted
        elif combined >= CONFIDENCE_TARGET and not has_critical:
            needs_review = True
            manual_review_required = False
            inv_status   = InvoiceStatus.needs_review
        else:
            needs_review = True
            manual_review_required = True
            inv_status   = InvoiceStatus.needs_review

        # ── Parse date ────────────────────────────────────
        invoice_date = None
        raw_date = inv_data.get("invoice_date")
        if raw_date:
            for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y"):
                try:
                    invoice_date = datetime.strptime(raw_date, fmt).date()
                    break
                except (ValueError, TypeError):
                    continue

        # ── Transaction type ──────────────────────────────
        tx_type = None
        raw_tx = inv_data.get("transaction_type")
        if raw_tx == "intrastate":
            tx_type = TransactionType.intrastate
        elif raw_tx == "interstate":
            tx_type = TransactionType.interstate

        # ── Create Invoice record ─────────────────────────
        invoice = Invoice(
            document_id=document_id,
            client_id=client_id,
            vendor_name=inv_data.get("vendor_name"),
            vendor_gstin=_clean_gstin(inv_data.get("vendor_gstin")),
            buyer_gstin=_clean_gstin(inv_data.get("buyer_gstin")),
            invoice_number=inv_data.get("invoice_number"),
            invoice_date=invoice_date,
            line_items=inv_data.get("line_items", []),
            taxable_value=_safe_float(inv_data.get("taxable_value")),
            cgst=_safe_float(inv_data.get("cgst")),
            sgst=_safe_float(inv_data.get("sgst")),
            igst=_safe_float(inv_data.get("igst")),
            total_gst=_safe_float(inv_data.get("total_gst")),
            invoice_total=_safe_float(inv_data.get("invoice_total")),
            transaction_type=tx_type,
            ocr_confidence=ocr_conf,
            claude_confidence=claude_conf,
            combined_confidence=combined,
            needs_review=needs_review,
            human_reviewed=False,
            status=inv_status,
            back_calculated=bool(inv_data.get("back_calculated", False)),
            back_calc_note=inv_data.get("back_calc_note"),
            source_pages=inv_data.get("_source_pages"),
            issues=issues,
            dedup_hash=inv_data.get("_dedup_hash"),
            layer_used=inv_data.get("_layer_used"),
            retry_count=inv_data.get("_retry_count", 0),
            manual_review_required=manual_review_required,
            field_confidence=inv_data.get("field_confidence"),
            vendor_address=inv_data.get("vendor_address"),
            buyer_name=inv_data.get("buyer_name"),
            buyer_address=inv_data.get("buyer_address"),
            place_of_supply=inv_data.get("place_of_supply"),
            grand_total=_safe_float(inv_data.get("grand_total")),
            bank_details=inv_data.get("bank_details"),
        )
        db.add(invoice)
        db.flush()  # get invoice.id

        # ── Audit trail: save every extracted field ────────
        # This creates the baseline for future correction tracking
        _save_initial_extractions(db, invoice, inv_data, firm_id)

        saved_count += 1

    # ── Update document status ─────────────────────────────
    doc = db.query(Document).filter(Document.id == document_id).first()
    if doc:
        doc.status = DocumentStatus.done

    db.commit()
    logger.info(f"[pipeline] route_node: saved {saved_count} invoices for doc_id={document_id}")

    state["saved_invoice_count"] = saved_count
    return state


def _save_initial_extractions(db, invoice, inv_data: Dict, firm_id: int):
    """
    Save every extracted field to extraction_corrections with corrected_value=None.
    When the CA edits a field, PATCH /extract/invoice updates corrected_value.
    """
    from models.db import ExtractionCorrection

    trackable_fields = [
        "vendor_name", "vendor_gstin", "buyer_gstin", "invoice_number",
        "invoice_date", "taxable_value", "cgst", "sgst", "igst",
        "total_gst", "invoice_total", "transaction_type",
    ]
    for field in trackable_fields:
        value = inv_data.get(field)
        if value is not None:
            db.add(ExtractionCorrection(
                invoice_id=invoice.id,
                firm_id=firm_id,
                field_name=field,
                extracted_value=str(value),
                corrected_value=None,  # filled in when CA corrects
                confidence_at_extraction=invoice.combined_confidence,
            ))


def _safe_float(val) -> Optional[float]:
    """Convert to float, return None on failure."""
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


# ── Build the LangGraph StateGraph ─────────────────────────

def build_pipeline() -> StateGraph:
    """
    Construct the 5-node extraction pipeline.
    Returns a compiled LangGraph graph ready to invoke.
    """
    graph = StateGraph(PipelineState)

    # Add nodes
    graph.add_node("classify_node", _safe_classify)
    graph.add_node("ocr_node",      _safe_ocr)
    graph.add_node("extract_node",  _safe_extract)
    graph.add_node("validate_node", _safe_validate)
    graph.add_node("route_node",    _route_and_save)

    # Linear edges
    graph.set_entry_point("classify_node")
    graph.add_edge("classify_node", "ocr_node")
    graph.add_edge("ocr_node",      "extract_node")
    graph.add_edge("extract_node",  "validate_node")
    graph.add_edge("validate_node", "route_node")
    graph.add_edge("route_node",    END)

    return graph.compile()


# Module-level compiled pipeline (import and call .invoke())
pipeline = build_pipeline()
