"""
ComplAI — Extract routes
Check job status and retrieve/edit extraction results.

GET   /api/extract/status/{job_id}      → job progress
GET   /api/extract/result/{job_id}      → invoices + document
PATCH /api/extract/invoice/{invoice_id} → CA corrections
"""

from typing import Any, Dict, List, Optional
from datetime import datetime, date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models.db import (
    Document, ExtractionCorrection, Firm, Invoice, JobStatus,
    InvoiceStatus, get_db,
)
from routes.auth import get_current_firm

router = APIRouter()


# ── Schemas ────────────────────────────────────────────────

class JobStatusOut(BaseModel):
    job_id: str
    status: str
    chunks_done: int
    chunks_total: int
    error: Optional[str] = None


class InvoiceOut(BaseModel):
    id: int
    document_id: int
    client_id: int
    vendor_name: Optional[str]
    vendor_gstin: Optional[str]
    vendor_address: Optional[str]
    buyer_name: Optional[str]
    buyer_gstin: Optional[str]
    buyer_address: Optional[str]
    invoice_number: Optional[str]
    invoice_date: Optional[date]
    place_of_supply: Optional[str]
    line_items: Optional[List[Dict]]
    taxable_value: Optional[float]
    cgst: Optional[float]
    sgst: Optional[float]
    igst: Optional[float]
    total_gst: Optional[float]
    invoice_total: Optional[float]
    grand_total: Optional[float]
    transaction_type: Optional[str]
    bank_details: Optional[Dict]
    field_confidence: Optional[Dict]
    layer_used: Optional[str]
    retry_count: int
    manual_review_required: bool
    ocr_confidence: float
    claude_confidence: float
    combined_confidence: float
    needs_review: bool
    human_reviewed: bool
    back_calculated: bool
    back_calc_note: Optional[str]
    issues: Optional[List[str]]
    status: str
    source_pages: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


class DocumentOut(BaseModel):
    id: int
    client_id: int
    file_type: str
    status: str
    month_year: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


class ResultOut(BaseModel):
    document: DocumentOut
    invoices: List[InvoiceOut]


class InvoiceUpdate(BaseModel):
    """
    Partial update — only include fields you want to change.
    Any changed field is saved to extraction_corrections for audit.
    """
    vendor_name: Optional[str]      = None
    vendor_gstin: Optional[str]     = None
    vendor_address: Optional[str]   = None
    buyer_name: Optional[str]       = None
    buyer_gstin: Optional[str]      = None
    buyer_address: Optional[str]    = None
    invoice_number: Optional[str]   = None
    invoice_date: Optional[date]    = None
    place_of_supply: Optional[str]  = None
    taxable_value: Optional[float]  = None
    cgst: Optional[float]           = None
    sgst: Optional[float]           = None
    igst: Optional[float]           = None
    total_gst: Optional[float]      = None
    invoice_total: Optional[float]  = None
    grand_total: Optional[float]    = None
    transaction_type: Optional[str] = None
    line_items: Optional[List[Dict]] = None
    bank_details: Optional[Dict]    = None
    status: Optional[str]           = None


# ── Routes ─────────────────────────────────────────────────

@router.get("/status/{job_id}", response_model=JobStatusOut)
def get_job_status(
    job_id: str,
    firm: Firm = Depends(get_current_firm),
    db: Session = Depends(get_db),
):
    """
    Poll this endpoint every 3 seconds from the frontend.
    Returns chunk-level progress so the UI can show "Processing 2 of 6 pages…"
    """
    job = db.query(JobStatus).filter(
        JobStatus.id == job_id,
        JobStatus.firm_id == firm.id,
    ).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    return JobStatusOut(
        job_id=job.id,
        status=job.status.value,
        chunks_done=job.chunks_done,
        chunks_total=job.chunks_total,
        error=job.error_message,
    )


@router.get("/result/{job_id}", response_model=ResultOut)
def get_job_result(
    job_id: str,
    firm: Firm = Depends(get_current_firm),
    db: Session = Depends(get_db),
):
    """Return extracted invoices once the job is done."""
    job = db.query(JobStatus).filter(
        JobStatus.id == job_id,
        JobStatus.firm_id == firm.id,
    ).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    document = db.query(Document).filter(Document.id == job.document_id).first()
    invoices  = db.query(Invoice).filter(Invoice.document_id == job.document_id).all()

    return ResultOut(document=document, invoices=invoices)


@router.patch("/invoice/{invoice_id}", response_model=InvoiceOut)
def update_invoice(
    invoice_id: int,
    payload: InvoiceUpdate,
    firm: Firm = Depends(get_current_firm),
    db: Session = Depends(get_db),
):
    """
    CA corrects extracted fields. For each changed field we:
      1. Log the original value + corrected value to extraction_corrections
      2. Update the invoice record
      3. Mark human_reviewed = True, needs_review = False
    This audit trail drives future model fine-tuning.
    """
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    # Verify the invoice belongs to this firm via document→client chain
    doc = db.query(Document).filter(
        Document.id == invoice.document_id,
        Document.firm_id == firm.id,
    ).first()
    if not doc:
        raise HTTPException(status_code=403, detail="Not your invoice")

    # ── Log corrections & apply changes ────────────────────
    changed_fields = payload.model_dump(exclude_unset=True)
    for field, new_value in changed_fields.items():
        old_value = getattr(invoice, field, None)
        if str(old_value) != str(new_value):  # only log actual changes
            correction = ExtractionCorrection(
                invoice_id=invoice_id,
                firm_id=firm.id,
                field_name=field,
                extracted_value=str(old_value) if old_value is not None else None,
                corrected_value=str(new_value) if new_value is not None else None,
                confidence_at_extraction=invoice.combined_confidence,
                document_file_path=doc.file_path,
            )
            db.add(correction)
        setattr(invoice, field, new_value)

    # Mark as human reviewed
    invoice.human_reviewed = True
    invoice.needs_review   = False
    if invoice.status == InvoiceStatus.needs_review:
        invoice.status = InvoiceStatus.approved

    db.commit()
    db.refresh(invoice)
    return invoice
