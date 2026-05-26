"""
ComplAI MVP1 — SQLAlchemy Models
All database tables live here. Single-tenant: one CA firm per deployment.

Models:
  Firm                — the CA firm (owner of the system)
  Client              — firm's clients (taxpayers)
  Document            — uploaded file (PDF/image/Excel)
  Invoice             — extracted invoice record
  ExtractionCorrection— audit trail when CA corrects an extracted field
  BankTransaction     — individual bank statement row
  Notice              — GST notices tracker
  JobStatus           — async Celery job tracking
"""

import enum
import hashlib
from datetime import datetime, date

from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    BigInteger,
    String,
    Float,
    Boolean,
    Text,
    DateTime,
    Date,
    Enum as SAEnum,
    ForeignKey,
    JSON,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from sqlalchemy.dialects.postgresql import UUID
import uuid
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:selvester1@localhost:5432/complai_qa")

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,     # reconnect after idle disconnect
    pool_size=10,
    max_overflow=20,
    echo=False,             # set True to log all SQL (noisy)
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# ── Dependency helper (used in route files) ────────────────
def get_db():
    """FastAPI dependency: yields a DB session and closes it after request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ── Enums ──────────────────────────────────────────────────

class DocumentStatus(str, enum.Enum):
    queued     = "queued"
    processing = "processing"
    done       = "done"
    error      = "error"


class DocumentFileType(str, enum.Enum):
    pdf  = "pdf"
    jpg  = "jpg"
    png  = "png"
    heic = "heic"
    xlsx = "xlsx"
    csv  = "csv"


class DocumentSourceChannel(str, enum.Enum):
    upload    = "upload"
    whatsapp  = "whatsapp"
    email     = "email"


class InvoiceStatus(str, enum.Enum):
    pending       = "pending"
    auto_accepted = "auto_accepted"
    needs_review  = "needs_review"
    approved      = "approved"
    rejected      = "rejected"


class TransactionType(str, enum.Enum):
    intrastate = "intrastate"
    interstate = "interstate"


class VoucherType(str, enum.Enum):
    receipt = "receipt"
    payment = "payment"
    contra  = "contra"
    journal = "journal"


class NoticeStatus(str, enum.Enum):
    open        = "open"
    in_progress = "in_progress"
    replied     = "replied"


class JobStatusEnum(str, enum.Enum):
    queued     = "queued"
    processing = "processing"
    done       = "done"
    error      = "error"


# ── Models ─────────────────────────────────────────────────

class Firm(Base):
    """
    The CA firm. Single-tenant — there is exactly ONE firm per deployment.
    Raja manually seeds this row via Alembic seed or SQL.
    """
    __tablename__ = "firms"

    id               = Column(Integer, primary_key=True, index=True)
    name             = Column(String(255), nullable=False)
    email            = Column(String(255), unique=True, nullable=False, index=True)
    password_hash    = Column(String(255), nullable=False)  # bcrypt hash
    whatsapp_number  = Column(String(20), nullable=True)
    created_at       = Column(DateTime, default=datetime.utcnow)

    # Relationships
    clients   = relationship("Client",   back_populates="firm")
    documents = relationship("Document", back_populates="firm")
    jobs      = relationship("JobStatus", back_populates="firm")


class Client(Base):
    """
    A taxpayer client of the CA firm. Raja adds these manually.
    Each client has its own GSTIN and is tracked separately.
    """
    __tablename__ = "clients"

    id               = Column(Integer, primary_key=True, index=True)
    firm_id          = Column(Integer, ForeignKey("firms.id"), nullable=False)
    name             = Column(String(255), nullable=False)
    gstin            = Column(String(15), nullable=True, index=True)  # 15-char GSTIN
    whatsapp_number  = Column(String(20), nullable=True)
    email            = Column(String(255), nullable=True)
    is_active        = Column(Boolean, default=True)
    created_at       = Column(DateTime, default=datetime.utcnow)

    # Relationships
    firm              = relationship("Firm",     back_populates="clients")
    documents         = relationship("Document", back_populates="client")
    invoices          = relationship("Invoice",  back_populates="client")
    bank_transactions = relationship("BankTransaction", back_populates="client")
    notices           = relationship("Notice",   back_populates="client")


class Document(Base):
    """
    Represents one uploaded file. A document can contain multiple invoices.
    status tracks the async processing pipeline progress.
    """
    __tablename__ = "documents"

    id             = Column(Integer, primary_key=True, index=True)
    client_id      = Column(Integer, ForeignKey("clients.id"), nullable=False)
    firm_id        = Column(Integer, ForeignKey("firms.id"),   nullable=False)
    file_path      = Column(String(500), nullable=False)  # local path ./uploads/...
    file_type      = Column(SAEnum(DocumentFileType), nullable=False)
    source_channel = Column(SAEnum(DocumentSourceChannel), default=DocumentSourceChannel.upload)
    status         = Column(SAEnum(DocumentStatus), default=DocumentStatus.queued, index=True)
    month_year     = Column(String(7), nullable=True)   # "2025-10" format
    media_url      = Column(String(500), nullable=True) # Twilio media URL (WhatsApp uploads only — for requeue if worker crashes)
    cost_usd       = Column(Float, default=0.0)        # total API cost for this doc
    claude_cost_usd = Column(Float, default=0.0, nullable=True)  # Anthropic Claude cost
    docai_cost_usd  = Column(Float, default=0.0, nullable=True)  # Google DocAI cost
    created_at     = Column(DateTime, default=datetime.utcnow)

    # Relationships
    client   = relationship("Client", back_populates="documents")
    firm     = relationship("Firm",   back_populates="documents")
    invoices = relationship("Invoice", back_populates="document")
    job      = relationship("JobStatus", back_populates="document", uselist=False)


class Invoice(Base):
    """
    One extracted invoice record. A single Document can produce many invoices
    (e.g., a multi-invoice PDF).

    Confidence scoring:
      ocr_confidence    — quality of the text extraction (0-1)
      claude_confidence — Claude's self-rated extraction confidence (0-1)
      combined_confidence = ocr × claude (used for auto-accept threshold 0.80)

    back_calculated — True when GST values were derived from totals (not explicit)
    """
    __tablename__ = "invoices"

    id                  = Column(Integer, primary_key=True, index=True)
    document_id         = Column(Integer, ForeignKey("documents.id"), nullable=False)
    client_id           = Column(Integer, ForeignKey("clients.id"),   nullable=False)

    # ── Parties ────────────────────────────────────────────
    vendor_name         = Column(String(500), nullable=True)
    vendor_gstin        = Column(String(20),  nullable=True)   # 15-char GSTIN; 20 to tolerate OCR noise
    buyer_gstin         = Column(String(20),  nullable=True)

    # ── Invoice identity ───────────────────────────────────
    invoice_number      = Column(String(100), nullable=True, index=True)
    invoice_date        = Column(Date, nullable=True)

    # ── Line items (stored as JSONB array) ─────────────────
    # Each item: {description, hsn_sac, quantity, unit_price, taxable_value, gst_rate, gst_amount}
    line_items          = Column(JSON, default=list)

    # ── Amounts (in Indian Rupees, stored as float) ─────────
    taxable_value       = Column(Float, nullable=True)
    cgst                = Column(Float, nullable=True)
    sgst                = Column(Float, nullable=True)
    igst                = Column(Float, nullable=True)
    total_gst           = Column(Float, nullable=True)
    invoice_total       = Column(Float, nullable=True)

    # ── Classification ─────────────────────────────────────
    transaction_type    = Column(SAEnum(TransactionType), nullable=True)

    # ── Confidence ─────────────────────────────────────────
    ocr_confidence      = Column(Float, default=0.0)
    claude_confidence   = Column(Float, default=0.0)
    combined_confidence = Column(Float, default=0.0)

    # ── Review state ───────────────────────────────────────
    needs_review        = Column(Boolean, default=True)
    human_reviewed      = Column(Boolean, default=False)
    status              = Column(SAEnum(InvoiceStatus), default=InvoiceStatus.pending, index=True)

    # ── Back-calculation metadata ──────────────────────────
    back_calculated     = Column(Boolean, default=False)
    back_calc_note      = Column(Text, nullable=True)

    # ── Source tracking ────────────────────────────────────
    source_pages        = Column(Text, nullable=True)  # "1-4" page range in original PDF
    issues              = Column(JSON, default=list)   # list of issue strings

    # ── Dedup hash (invoice_number + vendor_gstin + total) ─
    dedup_hash          = Column(String(64), nullable=True, index=True)

    # ── Pipeline metadata ──────────────────────────────────
    layer_used             = Column(String(50),  nullable=True)   # pdfplumber / google_docai / claude_vision
    retry_count            = Column(Integer,     default=0)
    manual_review_required = Column(Boolean,     default=False)

    # ── Per-field confidence ───────────────────────────────
    field_confidence       = Column(JSON, nullable=True)  # {"vendor_name": 0.95, ...}

    # ── Extended extraction ────────────────────────────────
    vendor_address         = Column(Text,   nullable=True)
    buyer_name             = Column(String(500), nullable=True)
    buyer_address          = Column(Text,   nullable=True)
    place_of_supply        = Column(String(100), nullable=True)
    grand_total            = Column(Float,  nullable=True)
    bank_details           = Column(JSON,   nullable=True)  # {account_number, ifsc_code, bank_name, branch, account_type}

    created_at          = Column(DateTime, default=datetime.utcnow)
    updated_at          = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    document    = relationship("Document",  back_populates="invoices")
    client      = relationship("Client",    back_populates="invoices")
    corrections = relationship("ExtractionCorrection", back_populates="invoice")

    def compute_dedup_hash(self) -> str:
        """SHA-256 of invoice_number + vendor_gstin + invoice_total for duplicate detection."""
        raw = f"{self.invoice_number}_{self.vendor_gstin}_{self.invoice_total}"
        return hashlib.sha256(raw.encode()).hexdigest()


class ExtractionCorrection(Base):
    """
    Audit trail: every time a CA corrects an extracted field, we log it here.
    This powers future model fine-tuning and confidence calibration.
    """
    __tablename__ = "extraction_corrections"

    id                   = Column(Integer, primary_key=True, index=True)
    invoice_id           = Column(Integer, ForeignKey("invoices.id"), nullable=False)
    firm_id              = Column(Integer, ForeignKey("firms.id"),    nullable=False)
    field_name           = Column(String(100), nullable=False)   # e.g. "vendor_name"
    extracted_value      = Column(Text, nullable=True)           # what AI got
    corrected_value      = Column(Text, nullable=True)           # what CA changed it to
    confidence_at_extraction = Column(Float, nullable=True)
    corrected_at         = Column(DateTime, default=datetime.utcnow)
    document_file_path   = Column(String(500), nullable=True)    # for traceability

    # Relationships
    invoice = relationship("Invoice", back_populates="corrections")


class BankTransaction(Base):
    """
    One row from a bank statement. Fuzzy-matched to ledger names from invoices.
    voucher_type is classified automatically (receipt/payment/contra/journal).
    """
    __tablename__ = "bank_transactions"

    id               = Column(Integer, primary_key=True, index=True)
    client_id        = Column(Integer, ForeignKey("clients.id"), nullable=False)
    firm_id          = Column(Integer, ForeignKey("firms.id"),   nullable=False)
    document_id      = Column(Integer, ForeignKey("documents.id"), nullable=True, index=True)  # tracks source doc
    transaction_date = Column(Date, nullable=False)
    narration        = Column(Text, nullable=True)
    reference        = Column(String(200), nullable=True)  # UPI ref / cheque no / NEFT UTR
    mode             = Column(String(50),  nullable=True)  # UPI / NEFT / RTGS / IMPS / CHQ / ATM
    debit            = Column(Float, nullable=True)   # outflow
    credit           = Column(Float, nullable=True)   # inflow
    balance          = Column(Float, nullable=True)
    voucher_type     = Column(SAEnum(VoucherType), nullable=True)
    matched_ledger   = Column(String(500), nullable=True)  # fuzzy-matched party name
    match_confirmed  = Column(Boolean, default=False)       # CA confirmed the match
    month_year       = Column(String(7), nullable=True)    # "2025-10"
    created_at       = Column(DateTime, default=datetime.utcnow)

    # Relationships
    client = relationship("Client", back_populates="bank_transactions")


class BankStatementMeta(Base):
    """
    Statement-level metadata extracted from a bank statement document.
    One record per uploaded bank statement (document).
    """
    __tablename__ = "bank_statement_meta"

    id               = Column(Integer, primary_key=True, index=True)
    document_id      = Column(Integer, ForeignKey("documents.id"), nullable=False, unique=True)
    client_id        = Column(Integer, ForeignKey("clients.id"), nullable=False)
    firm_id          = Column(Integer, ForeignKey("firms.id"),   nullable=False)
    account_number   = Column(String(50),  nullable=True)
    account_holder   = Column(String(200), nullable=True)
    bank_name        = Column(String(200), nullable=True)
    ifsc_code        = Column(String(20),  nullable=True)
    period_from      = Column(Date, nullable=True)
    period_to        = Column(Date, nullable=True)
    opening_balance  = Column(Float, nullable=True)
    closing_balance  = Column(Float, nullable=True)
    total_credits    = Column(Float, nullable=True)
    total_debits     = Column(Float, nullable=True)
    balance_matches  = Column(Boolean, nullable=True)  # opening + credits - debits ≈ closing
    confidence       = Column(Float,   nullable=True)
    created_at       = Column(DateTime, default=datetime.utcnow)


class Notice(Base):
    """GST notice tracker — simple status board for the CA firm."""
    __tablename__ = "notices"

    id          = Column(Integer, primary_key=True, index=True)
    client_id   = Column(Integer, ForeignKey("clients.id"), nullable=False)
    firm_id     = Column(Integer, ForeignKey("firms.id"),   nullable=False)
    notice_type = Column(String(100), nullable=True)   # e.g. "DRC-01", "ASMT-10"
    notice_date = Column(Date, nullable=True)
    due_date    = Column(Date, nullable=True)
    status      = Column(SAEnum(NoticeStatus), default=NoticeStatus.open)
    notes       = Column(Text, nullable=True)
    created_at  = Column(DateTime, default=datetime.utcnow)

    # Relationships
    client = relationship("Client", back_populates="notices")


class JobStatus(Base):
    """
    Tracks async Celery processing jobs.
    The API returns job_id immediately after upload; the frontend polls this.
    chunks_done / chunks_total show page-level progress for large PDFs.
    """
    __tablename__ = "job_statuses"

    id            = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    firm_id       = Column(Integer, ForeignKey("firms.id"),    nullable=False)
    document_id   = Column(Integer, ForeignKey("documents.id"), nullable=False)
    status        = Column(SAEnum(JobStatusEnum), default=JobStatusEnum.queued, index=True)
    chunks_total  = Column(Integer, default=0)
    chunks_done   = Column(Integer, default=0)
    error_message = Column(Text, nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow)
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    firm     = relationship("Firm",     back_populates="jobs")
    document = relationship("Document", back_populates="job")
