"""
ComplAI — Auth routes
Simple JWT login for the single CA firm.
One shared login for the whole team — no per-user accounts in MVP1.

POST /api/auth/login   → {token, firm_id, firm_name}
"""

import os
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models.db import Firm, get_db

router = APIRouter()

# ── Config ─────────────────────────────────────────────────
JWT_SECRET    = os.getenv("JWT_SECRET", "change-me-please")
ALGORITHM     = "HS256"
TOKEN_EXPIRE  = 60 * 24  # 24 hours in minutes

pwd_context   = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


# ── Schemas ────────────────────────────────────────────────
class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    token: str
    firm_id: int
    firm_name: str


class TokenData(BaseModel):
    firm_id: Optional[int] = None


# ── Helpers ────────────────────────────────────────────────

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def create_access_token(data: dict) -> str:
    payload = data.copy()
    expire  = datetime.utcnow() + timedelta(minutes=TOKEN_EXPIRE)
    payload.update({"exp": expire})
    return jwt.encode(payload, JWT_SECRET, algorithm=ALGORITHM)


def get_current_firm(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> Firm:
    """FastAPI dependency — decodes JWT and returns the Firm object."""
    credentials_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload  = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
        firm_id: int = payload.get("firm_id")
        if firm_id is None:
            raise credentials_exc
    except JWTError:
        raise credentials_exc

    firm = db.query(Firm).filter(Firm.id == firm_id).first()
    if firm is None:
        raise credentials_exc
    return firm


# ── Routes ─────────────────────────────────────────────────

@router.post("/login", response_model=TokenResponse)
def login(req: LoginRequest, db: Session = Depends(get_db)):
    """
    Authenticate the firm. Returns a JWT valid for 24 hours.
    Email + password are checked against the firms table.
    """
    firm = db.query(Firm).filter(Firm.email == req.email).first()

    if not firm or not verify_password(req.password, firm.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )

    token = create_access_token({"firm_id": firm.id})
    return TokenResponse(token=token, firm_id=firm.id, firm_name=firm.name)


@router.post("/login/form")
def login_form(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    """OAuth2 form-compatible login (username = email). Used by Swagger UI."""
    firm = db.query(Firm).filter(Firm.email == form_data.username).first()

    if not firm or not verify_password(form_data.password, firm.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )

    token = create_access_token({"firm_id": firm.id})
    return {"access_token": token, "token_type": "bearer"}
