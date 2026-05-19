"""
ComplAI — Client routes
Raja manually adds clients (taxpayers) here. No self-service.

GET  /api/clients          → list all active clients
POST /api/clients          → create new client
GET  /api/clients/{id}     → get single client
PATCH /api/clients/{id}    → update client
DELETE /api/clients/{id}   → soft-delete (is_active=False)
"""

from typing import List, Optional
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models.db import Client, Firm, get_db
from routes.auth import get_current_firm

router = APIRouter()


# ── Schemas ────────────────────────────────────────────────

class ClientCreate(BaseModel):
    name: str
    gstin: Optional[str] = None
    whatsapp_number: Optional[str] = None
    email: Optional[str] = None


class ClientUpdate(BaseModel):
    name: Optional[str] = None
    gstin: Optional[str] = None
    whatsapp_number: Optional[str] = None
    email: Optional[str] = None
    is_active: Optional[bool] = None


class ClientOut(BaseModel):
    id: int
    firm_id: int
    name: str
    gstin: Optional[str]
    whatsapp_number: Optional[str]
    email: Optional[str]
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


# ── Routes ─────────────────────────────────────────────────

@router.get("", response_model=List[ClientOut])
def list_clients(
    firm: Firm = Depends(get_current_firm),
    db: Session = Depends(get_db),
):
    """Return all active clients for the authenticated firm."""
    return (
        db.query(Client)
        .filter(Client.firm_id == firm.id, Client.is_active == True)
        .order_by(Client.name)
        .all()
    )


@router.post("", response_model=ClientOut, status_code=status.HTTP_201_CREATED)
def create_client(
    payload: ClientCreate,
    firm: Firm = Depends(get_current_firm),
    db: Session = Depends(get_db),
):
    """Add a new client to the firm. GSTIN is optional (some clients may not be GST-registered)."""
    # Validate GSTIN format if provided
    if payload.gstin:
        import re
        pattern = r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$"
        if not re.match(pattern, payload.gstin.upper()):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid GSTIN format",
            )

    client = Client(
        firm_id=firm.id,
        name=payload.name,
        gstin=payload.gstin.upper() if payload.gstin else None,
        whatsapp_number=payload.whatsapp_number,
        email=payload.email,
    )
    db.add(client)
    db.commit()
    db.refresh(client)
    return client


@router.get("/{client_id}", response_model=ClientOut)
def get_client(
    client_id: int,
    firm: Firm = Depends(get_current_firm),
    db: Session = Depends(get_db),
):
    client = db.query(Client).filter(
        Client.id == client_id, Client.firm_id == firm.id
    ).first()
    if not client:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "Client not found", "error_code": "CLIENT_NOT_FOUND"},
        )
    return client


@router.patch("/{client_id}", response_model=ClientOut)
def update_client(
    client_id: int,
    payload: ClientUpdate,
    firm: Firm = Depends(get_current_firm),
    db: Session = Depends(get_db),
):
    client = db.query(Client).filter(
        Client.id == client_id, Client.firm_id == firm.id
    ).first()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(client, field, value)

    db.commit()
    db.refresh(client)
    return client


@router.delete("/{client_id}", status_code=status.HTTP_204_NO_CONTENT)
def deactivate_client(
    client_id: int,
    firm: Firm = Depends(get_current_firm),
    db: Session = Depends(get_db),
):
    """Soft delete — sets is_active=False, preserves all data."""
    client = db.query(Client).filter(
        Client.id == client_id, Client.firm_id == firm.id
    ).first()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    client.is_active = False
    db.commit()
