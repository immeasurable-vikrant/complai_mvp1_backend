# ComplAI MVP1 — Invoice Extraction Tool for Indian CA Firms

Single-tenant invoice extraction system. One CA firm. Raja manually adds clients. No multi-tenant.

## Architecture

```
Browser (index.html)
    ↓ HTTP
FastAPI (main.py)  →  Celery worker (Redis broker)
    ↓                     ↓
PostgreSQL         LangGraph Pipeline (5 nodes)
                      ├── classify_node   → file type + PDF chunking
                      ├── ocr_node        → pdfplumber / DocAI / Claude Vision
                      ├── extract_node    → Claude Haiku (structured JSON)
                      ├── validate_node   → GSTIN / duplicate / total checks
                      └── route_node      → confidence scoring + DB save
```

## Quick Start

### 1. Prerequisites
- Python 3.11+
- PostgreSQL 14+
- Redis 7+

### 2. Install dependencies
```bash
cd backend
pip install -r requirements.txt
```

### 3. Configure environment
```bash
cp .env.example .env
# Edit .env with your API keys and DB URL
```

### 4. Create database
```bash
createdb complai
alembic upgrade head
```

### 5. Seed the firm + sample clients
```bash
python seed.py
# Edit seed.py first with your firm name and login credentials
```

### 6. Start the API server
```bash
uvicorn main:app --reload --port 8000
```

### 7. Start the Celery worker
```bash
celery -A workers.celery_app worker --loglevel=info --concurrency=4
```

### 8. Open the frontend
Visit `http://localhost:8000` in your browser.

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/auth/login` | JWT login |
| GET | `/api/dashboard` | Firm overview |
| GET/POST | `/api/clients` | Client CRUD |
| POST | `/api/upload` | Upload invoice document |
| GET | `/api/extract/status/{job_id}` | Poll job progress |
| GET | `/api/extract/result/{job_id}` | Get extracted invoices |
| PATCH | `/api/extract/invoice/{id}` | CA corrections |
| POST | `/api/bank/upload` | Upload bank statement |
| GET | `/api/bank/result/{job_id}` | Bank transactions |
| PATCH | `/api/bank/transaction/{id}` | Confirm ledger match |
| POST | `/api/export/busy` | Busy Excel download |
| POST | `/api/export/tally` | TallyPrime XML download |
| POST | `/api/export/bank` | Bank Excel download |
| GET | `/api/cost` | API cost tracker |

Interactive docs: `http://localhost:8000/docs`

---

## Confidence Scoring

| Score | Color | Action |
|-------|-------|--------|
| ≥ 80% | 🟢 Green | Auto-accepted — no action needed |
| 60–79% | 🟡 Amber | Review recommended |
| < 60% | 🔴 Red | Must review before export |

`combined_confidence = ocr_confidence × claude_confidence`

---

## Export Formats

### Busy Excel (Purchase Register)
Columns: Date, Vendor Name, Vendor GSTIN, Invoice No, Invoice Total, Taxable Value, CGST, SGST, IGST, Total GST, Transaction Type, HSN/SAC, Confidence %, Needs Review, Issues

### TallyPrime XML
Purchase vouchers with ledger entries for vendor, purchase account, and GST input accounts. Import via: Gateway of Tally → Import Data → Vouchers.

### Bank Excel
Sheet 1: All transactions with voucher type and ledger match.
Sheet 2: Summary (opening/closing balance, totals).

---

## File Structure

```
complai/
├── backend/
│   ├── main.py                 ← FastAPI app
│   ├── requirements.txt
│   ├── .env.example
│   ├── seed.py                 ← First-time setup
│   ├── alembic/
│   │   ├── env.py
│   │   └── versions/001_initial_schema.py
│   ├── models/
│   │   └── db.py               ← All SQLAlchemy models
│   ├── routes/
│   │   ├── auth.py
│   │   ├── clients.py
│   │   ├── upload.py
│   │   ├── extract.py
│   │   ├── bank.py
│   │   ├── export.py
│   │   ├── dashboard.py
│   │   └── cost.py
│   ├── agents/
│   │   ├── pipeline.py         ← LangGraph 5-node StateGraph
│   │   ├── classifier.py       ← Node 1: doc type + chunking
│   │   ├── ocr.py              ← Node 2: 3-layer OCR
│   │   ├── extractor.py        ← Node 3: Claude Haiku extraction
│   │   ├── validator.py        ← Node 4: validations
│   │   └── vision.py           ← Claude Vision wrapper
│   ├── utils/
│   │   ├── confidence.py       ← Combined confidence formula
│   │   ├── fuzzy.py            ← Party name matching
│   │   └── cost_tracker.py     ← API cost accumulation
│   └── workers/
│       └── celery_app.py       ← Celery tasks + bank pipeline
└── frontend/
    └── index.html              ← Complete single-file UI
```

---

## Cost Estimates (per document)

| Document type | OCR method | ~Cost |
|---------------|-----------|-------|
| Digital PDF | pdfplumber | ₹0 |
| 1-page scanned | Google DocAI | ₹5.50 |
| 1-page blurry | Claude Vision | ₹0.25 |
| Claude extraction | Haiku | ₹0.05 |

Budget is configurable via `BUDGET_USD` in `.env`.
