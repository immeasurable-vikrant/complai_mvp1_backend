"""
ComplAI — Bank Statement routes
Upload bank statement PDFs/Excel → classify transactions → fuzzy match parties.

POST  /api/bank/upload                   → {job_id}
GET   /api/bank/status/{job_id}          → job progress
GET   /api/bank/result/{job_id}          → {transactions, meta, totals}
PATCH /api/bank/transaction/{tx_id}      → confirm/reject fuzzy match
"""

import os
import uuid
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime, date

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models.db import (
    BankStatementMeta, BankTransaction, Client, Document, DocumentFileType,
    DocumentSourceChannel, DocumentStatus, Firm, JobStatus, JobStatusEnum, get_db,
)
from routes.auth import get_current_firm
from workers.celery_app import process_bank_statement

router = APIRouter()

UPLOAD_DIR    = os.getenv("UPLOAD_DIR", "./uploads")
MAX_FILE_SIZE = 40 * 1024 * 1024  # 40 MB


# ── Schemas ────────────────────────────────────────────────

class UploadResponse(BaseModel):
    job_id: str
    document_id: int
    message: str


class BankTransactionOut(BaseModel):
    id: int
    client_id: int
    document_id: Optional[int] = None
    transaction_date: date
    narration: Optional[str]
    reference: Optional[str]
    mode: Optional[str]
    debit: Optional[float]
    credit: Optional[float]
    balance: Optional[float]
    voucher_type: Optional[str]
    matched_ledger: Optional[str]
    match_confirmed: bool
    month_year: Optional[str]

    class Config:
        from_attributes = True


class BankStatementMetaOut(BaseModel):
    account_number: Optional[str]
    account_holder: Optional[str]
    bank_name: Optional[str]
    ifsc_code: Optional[str]
    period_from: Optional[date]
    period_to: Optional[date]
    opening_balance: Optional[float]
    closing_balance: Optional[float]
    total_credits: Optional[float]
    total_debits: Optional[float]
    balance_matches: Optional[bool]
    confidence: Optional[float]

    class Config:
        from_attributes = True


class BankResultOut(BaseModel):
    transactions: List[BankTransactionOut]
    total_credits: float
    total_debits: float
    transaction_count: int
    unmatched_count: int
    statement_meta: Optional[BankStatementMetaOut] = None


class MatchUpdate(BaseModel):
    matched_ledger: Optional[str] = None
    match_confirmed: bool = True


# ── Routes ─────────────────────────────────────────────────

@router.post("/upload", response_model=UploadResponse)
async def upload_bank_statement(
    file:       UploadFile = File(...),
    client_id:  int        = Form(...),
    month_year: str        = Form(...),
    firm:       Firm       = Depends(get_current_firm),
    db:         Session    = Depends(get_db),
):
    """
    Upload a bank statement (PDF or Excel).
    Returns job_id immediately — processing happens async.
    """
    # Verify client
    client = db.query(Client).filter(
        Client.id == client_id, Client.firm_id == firm.id
    ).first()
    if not client:
        raise HTTPException(status_code=404, detail={"error": "Client not found", "error_code": "CLIENT_NOT_FOUND"})

    file_bytes = await file.read()
    if len(file_bytes) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail={"error": "File too large", "error_code": "FILE_TOO_LARGE"})

    # Detect type
    ext = Path(file.filename or "").suffix.lower()
    ext_map = {
        ".pdf":  DocumentFileType.pdf,
        ".xlsx": DocumentFileType.xlsx,
        ".xls":  DocumentFileType.xlsx,   # legacy Excel — processed same way
        ".csv":  DocumentFileType.csv,
    }
    if ext not in ext_map:
        raise HTTPException(status_code=400, detail={"error": "Only PDF/XLS/XLSX/CSV allowed", "error_code": "INVALID_FILE_TYPE"})
    file_type = ext_map[ext]

    # Save
    dest_dir = Path(UPLOAD_DIR) / "bank" / str(firm.id) / str(client_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    file_path = str(dest_dir / f"{uuid.uuid4().hex}_{Path(file.filename or 'bank').name}")
    Path(file_path).write_bytes(file_bytes)

    # DB records
    document = Document(
        client_id=client_id,
        firm_id=firm.id,
        file_path=file_path,
        file_type=file_type,
        source_channel=DocumentSourceChannel.upload,
        status=DocumentStatus.queued,
        month_year=month_year,
    )
    db.add(document)
    db.flush()

    job = JobStatus(firm_id=firm.id, document_id=document.id, status=JobStatusEnum.queued)
    db.add(job)
    db.commit()
    db.refresh(job)

    # Queue
    process_bank_statement.delay(
        job_id=job.id,
        file_path=file_path,
        document_id=document.id,
        client_id=client_id,
        firm_id=firm.id,
        month_year=month_year,
    )

    return UploadResponse(job_id=job.id, document_id=document.id, message="Bank statement uploaded. Processing started.")


@router.get("/status/{job_id}")
def get_bank_status(
    job_id: str,
    firm: Firm = Depends(get_current_firm),
    db: Session = Depends(get_db),
):
    job = db.query(JobStatus).filter(JobStatus.id == job_id, JobStatus.firm_id == firm.id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job_id": job.id, "status": job.status.value, "chunks_done": job.chunks_done, "chunks_total": job.chunks_total, "error": job.error_message}


@router.get("/result/{job_id}", response_model=BankResultOut)
def get_bank_result(
    job_id: str,
    firm: Firm = Depends(get_current_firm),
    db: Session = Depends(get_db),
):
    job = db.query(JobStatus).filter(JobStatus.id == job_id, JobStatus.firm_id == firm.id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    doc = db.query(Document).filter(Document.id == job.document_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    # Always scope to this specific document — never bleed across uploads.
    # If this document has no transactions yet (still processing or agent failed),
    # return an empty list rather than showing another document's transactions.
    txs = db.query(BankTransaction).filter(
        BankTransaction.document_id == doc.id,
    ).order_by(BankTransaction.transaction_date, BankTransaction.id).all()

    total_credits = sum(t.credit or 0 for t in txs)
    total_debits  = sum(t.debit  or 0 for t in txs)
    unmatched     = sum(1 for t in txs if not t.matched_ledger)

    # Fetch statement-level metadata for this document (may be None for old records)
    meta_row = db.query(BankStatementMeta).filter(
        BankStatementMeta.document_id == doc.id
    ).first()

    return BankResultOut(
        transactions     = txs,
        total_credits    = total_credits,
        total_debits     = total_debits,
        transaction_count= len(txs),
        unmatched_count  = unmatched,
        statement_meta   = meta_row,
    )


@router.patch("/transaction/{tx_id}")
def update_bank_match(
    tx_id: int,
    payload: MatchUpdate,
    firm: Firm = Depends(get_current_firm),
    db: Session = Depends(get_db),
):
    """CA confirms or overrides the fuzzy party match for a bank transaction."""
    tx = db.query(BankTransaction).filter(BankTransaction.id == tx_id, BankTransaction.firm_id == firm.id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")

    if payload.matched_ledger is not None:
        tx.matched_ledger = payload.matched_ledger
    tx.match_confirmed = payload.match_confirmed

    db.commit()
    db.refresh(tx)
    return {"id": tx.id, "matched_ledger": tx.matched_ledger, "match_confirmed": tx.match_confirmed}
