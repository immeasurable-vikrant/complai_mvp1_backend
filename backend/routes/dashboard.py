"""
ComplAI — Dashboard route
Firm-level overview: all clients with their document status for the current month.

GET /api/dashboard?month_year=2025-10
"""

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models.db import Client, Document, DocumentStatus, Firm, Invoice, get_db
from routes.auth import get_current_firm

router = APIRouter()


class ClientStatus(BaseModel):
    id: int
    name: str
    gstin: Optional[str]
    docs_received: int          # total documents uploaded this month
    docs_pending: int           # documents still processing
    invoices_extracted: int     # invoices successfully extracted
    invoices_review: int        # invoices needing human review
    status: str                 # "ok" | "processing" | "review_needed" | "no_data"


class DashboardOut(BaseModel):
    month_year: str
    total_clients: int
    total_invoices: int
    review_needed: int
    clients: List[ClientStatus]


@router.get("", response_model=DashboardOut)
def get_dashboard(
    month_year: str = Query(
        default=None,
        description='Month filter, e.g. "2025-10". Defaults to current month.',
    ),
    firm: Firm    = Depends(get_current_firm),
    db:   Session = Depends(get_db),
):
    """
    Returns a bird's-eye view for the CA firm.
    Used for the top section of the frontend dashboard.
    """
    if not month_year:
        month_year = datetime.now().strftime("%Y-%m")

    clients = (
        db.query(Client)
        .filter(Client.firm_id == firm.id, Client.is_active == True)
        .order_by(Client.name)
        .all()
    )

    client_statuses = []
    total_invoices  = 0
    total_review    = 0

    for client in clients:
        # Documents this month for this client
        docs = (
            db.query(Document)
            .filter(
                Document.client_id == client.id,
                Document.firm_id == firm.id,
                Document.month_year == month_year,
            )
            .all()
        )

        docs_received = len(docs)
        docs_pending  = sum(
            1 for d in docs if d.status in (DocumentStatus.queued, DocumentStatus.processing)
        )

        # Invoices for this client this month
        doc_ids = [d.id for d in docs]
        if doc_ids:
            invoices = (
                db.query(Invoice)
                .filter(Invoice.document_id.in_(doc_ids))
                .all()
            )
        else:
            invoices = []

        invoices_extracted = len(invoices)
        invoices_review    = sum(1 for inv in invoices if inv.needs_review)

        total_invoices += invoices_extracted
        total_review   += invoices_review

        # Derive client-level status
        if docs_pending > 0:
            client_status = "processing"
        elif invoices_review > 0:
            client_status = "review_needed"
        elif invoices_extracted > 0:
            client_status = "ok"
        else:
            client_status = "no_data"

        client_statuses.append(ClientStatus(
            id=client.id,
            name=client.name,
            gstin=client.gstin,
            docs_received=docs_received,
            docs_pending=docs_pending,
            invoices_extracted=invoices_extracted,
            invoices_review=invoices_review,
            status=client_status,
        ))

    return DashboardOut(
        month_year=month_year,
        total_clients=len(clients),
        total_invoices=total_invoices,
        review_needed=total_review,
        clients=client_statuses,
    )
