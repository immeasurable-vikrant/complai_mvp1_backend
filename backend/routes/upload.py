"""
ComplAI — Upload route
Handles file upload → saves to disk → queues Celery job.
Never blocks the HTTP response: returns job_id immediately.

POST /api/upload    → {job_id}
"""

import os
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models.db import (
    Client, Document, DocumentFileType, DocumentSourceChannel,
    DocumentStatus, Firm, JobStatus, JobStatusEnum, get_db,
)
from routes.auth import get_current_firm
from workers.celery_app import process_document

router = APIRouter()

# ── Config ─────────────────────────────────────────────────
UPLOAD_DIR    = os.getenv("UPLOAD_DIR", "./uploads")
MAX_FILE_SIZE = 40 * 1024 * 1024  # 40 MB

ALLOWED_TYPES = {
    "application/pdf":                     DocumentFileType.pdf,
    "image/jpeg":                          DocumentFileType.jpg,
    "image/png":                           DocumentFileType.png,
    "image/heic":                          DocumentFileType.heic,
    "image/heif":                          DocumentFileType.heic,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": DocumentFileType.xlsx,
    "text/csv":                            DocumentFileType.csv,
}

ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".heic", ".heif", ".xlsx", ".csv"}


# ── Response schemas ───────────────────────────────────────

class UploadResponse(BaseModel):
    job_id: str
    document_id: int
    message: str


# ── Helpers ────────────────────────────────────────────────

def _detect_file_type(filename: str, content_type: str) -> DocumentFileType:
    """
    Detect file type from extension first (more reliable than content_type
    because browsers sometimes send generic MIME types for HEIC).
    """
    ext = Path(filename).suffix.lower()
    ext_map = {
        ".pdf":  DocumentFileType.pdf,
        ".jpg":  DocumentFileType.jpg,
        ".jpeg": DocumentFileType.jpg,
        ".png":  DocumentFileType.png,
        ".heic": DocumentFileType.heic,
        ".heif": DocumentFileType.heic,
        ".xlsx": DocumentFileType.xlsx,
        ".csv":  DocumentFileType.csv,
    }
    if ext in ext_map:
        return ext_map[ext]
    if content_type in ALLOWED_TYPES:
        return ALLOWED_TYPES[content_type]
    raise HTTPException(
        status_code=400,
        detail={
            "success": False,
            "error": f"File type not supported: {ext or content_type}",
            "error_code": "INVALID_FILE_TYPE",
        },
    )


def _save_file(
    file_bytes: bytes,
    filename: str,
    firm_id: int,
    client_id: int,
) -> str:
    """
    Save uploaded bytes to ./uploads/{firm_id}/{client_id}/{uuid}_{filename}.
    Returns the relative file path.
    """
    safe_name = Path(filename).name  # strip any directory traversal
    dest_dir  = Path(UPLOAD_DIR) / str(firm_id) / str(client_id)
    dest_dir.mkdir(parents=True, exist_ok=True)

    unique_name = f"{uuid.uuid4().hex}_{safe_name}"
    dest_path   = dest_dir / unique_name
    dest_path.write_bytes(file_bytes)
    return str(dest_path)


# ── Route ──────────────────────────────────────────────────

@router.post("", response_model=UploadResponse)
async def upload_invoice(
    file:       UploadFile = File(...),
    client_id:  int        = Form(...),
    month_year: str        = Form(...),   # "2025-10"
    firm:       Firm       = Depends(get_current_firm),
    db:         Session    = Depends(get_db),
):
    """
    Upload an invoice file (PDF/image/Excel) for a client.

    Flow:
      1. Validate file type & size
      2. Save to disk
      3. Create Document + JobStatus records
      4. Push async Celery task
      5. Return job_id immediately (no waiting for extraction)
    """
    # ── Verify client belongs to this firm ─────────────────
    client = db.query(Client).filter(
        Client.id == client_id,
        Client.firm_id == firm.id,
        Client.is_active == True,
    ).first()
    if not client:
        raise HTTPException(
            status_code=404,
            detail={
                "success": False,
                "error": "Client not found",
                "error_code": "CLIENT_NOT_FOUND",
            },
        )

    # ── Read file & validate ────────────────────────────────
    file_bytes = await file.read()

    if len(file_bytes) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=400,
            detail={
                "success": False,
                "error": "File too large. Maximum size is 20 MB.",
                "error_code": "FILE_TOO_LARGE",
            },
        )

    file_type = _detect_file_type(file.filename or "", file.content_type or "")

    # ── Save to disk ───────────────────────────────────────
    file_path = _save_file(file_bytes, file.filename or "upload", firm.id, client_id)

    # ── Create DB records ──────────────────────────────────
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
    db.flush()  # get document.id before job

    job = JobStatus(
        firm_id=firm.id,
        document_id=document.id,
        status=JobStatusEnum.queued,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    # ── Queue async Celery task ────────────────────────────
    # process_document is idempotent — safe to retry on failure
    process_document.delay(
        job_id=job.id,
        file_path=file_path,
        document_id=document.id,
        client_id=client_id,
        firm_id=firm.id,
    )

    return UploadResponse(
        job_id=job.id,
        document_id=document.id,
        message="File uploaded. Processing started.",
    )
