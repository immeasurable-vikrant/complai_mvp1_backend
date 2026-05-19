"""
ComplAI — Cost tracking route
GET /api/cost
"""

import os
from datetime import datetime, date

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from models.db import Document, DocumentStatus, Firm, get_db
from routes.auth import get_current_firm

router = APIRouter()

INR_PER_USD = 84.0
BUDGET_USD  = float(os.getenv("BUDGET_USD", "10.0"))


class CostOut(BaseModel):
    today_usd:            float
    month_usd:            float
    today_inr:            float
    month_inr:            float
    budget_usd:           float
    budget_remaining_usd: float
    budget_used_pct:      float
    docs_today:           int
    docs_month:           int
    claude_month_inr:     float
    docai_month_inr:      float


@router.get("", response_model=CostOut)
def get_cost(
    firm: Firm    = Depends(get_current_firm),
    db:   Session = Depends(get_db),
):
    now       = datetime.utcnow()
    month_str = now.strftime("%Y-%m")

    today_cost = (
        db.query(func.sum(Document.cost_usd))
        .filter(
            Document.firm_id == firm.id,
            func.date(Document.created_at) == date.today(),
        )
        .scalar() or 0.0
    )

    month_cost = (
        db.query(func.sum(Document.cost_usd))
        .filter(
            Document.firm_id == firm.id,
            func.to_char(Document.created_at, "YYYY-MM") == month_str,
        )
        .scalar() or 0.0
    )

    claude_month_cost = (
        db.query(func.sum(Document.claude_cost_usd))
        .filter(
            Document.firm_id == firm.id,
            func.to_char(Document.created_at, "YYYY-MM") == month_str,
        )
        .scalar() or 0.0
    )

    docai_month_cost = (
        db.query(func.sum(Document.docai_cost_usd))
        .filter(
            Document.firm_id == firm.id,
            func.to_char(Document.created_at, "YYYY-MM") == month_str,
        )
        .scalar() or 0.0
    )

    docs_today = (
        db.query(func.count(Document.id))
        .filter(
            Document.firm_id == firm.id,
            Document.status == DocumentStatus.done,
            func.date(Document.created_at) == date.today(),
        )
        .scalar() or 0
    )

    docs_month = (
        db.query(func.count(Document.id))
        .filter(
            Document.firm_id == firm.id,
            Document.status == DocumentStatus.done,
            func.to_char(Document.created_at, "YYYY-MM") == month_str,
        )
        .scalar() or 0
    )

    budget_remaining = max(0.0, BUDGET_USD - month_cost)
    budget_used_pct  = min(100.0, (month_cost / BUDGET_USD * 100) if BUDGET_USD > 0 else 0)

    return CostOut(
        today_usd=round(today_cost, 4),
        month_usd=round(month_cost, 4),
        today_inr=round(today_cost * INR_PER_USD, 2),
        month_inr=round(month_cost * INR_PER_USD, 2),
        budget_usd=BUDGET_USD,
        budget_remaining_usd=round(budget_remaining, 4),
        budget_used_pct=round(budget_used_pct, 1),
        docs_today=docs_today,
        docs_month=docs_month,
        claude_month_inr=round(claude_month_cost * INR_PER_USD, 2),
        docai_month_inr=round(docai_month_cost * INR_PER_USD, 2),
    )
