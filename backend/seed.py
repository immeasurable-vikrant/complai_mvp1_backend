"""
ComplAI — Seed Script
Creates the initial firm (CA firm) and one sample client.
Run once after `alembic upgrade head`.

Usage:
  python seed.py

Edit the FIRM and CLIENT dicts below before running.
"""

import os
from dotenv import load_dotenv
from passlib.context import CryptContext

load_dotenv()

from models.db import SessionLocal, Firm, Client, Base, engine

Base.metadata.create_all(bind=engine)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ── Edit these before running ──────────────────────────────
FIRM = {
    "name":             "Raja & Associates",
    "email":            "raja@caoffice.in",
    "password":         "change-me-123",        # raw password — gets hashed
    "whatsapp_number":  "+91 98765 43210",
}

CLIENTS = [
    {"name": "Sharma Trading Co.",         "gstin": "27AABCS1429B1Z3"},
    {"name": "M/s XYZ Pvt Ltd",            "gstin": "29AABCX5129C1Z4"},
    {"name": "Acme Enterprises",           "gstin": None},           # not GST-registered
]


def main():
    db = SessionLocal()
    try:
        # Check if firm already exists
        existing = db.query(Firm).filter(Firm.email == FIRM["email"]).first()
        if existing:
            print(f"⚠ Firm '{existing.name}' already exists (id={existing.id})")
            firm = existing
        else:
            firm = Firm(
                name=FIRM["name"],
                email=FIRM["email"],
                password_hash=pwd_context.hash(FIRM["password"]),
                whatsapp_number=FIRM.get("whatsapp_number"),
            )
            db.add(firm)
            db.flush()
            print(f"✓ Created firm: {firm.name} (id={firm.id})")

        for client_data in CLIENTS:
            existing_client = db.query(Client).filter(
                Client.firm_id == firm.id,
                Client.name == client_data["name"],
            ).first()
            if not existing_client:
                client = Client(firm_id=firm.id, **client_data)
                db.add(client)
                print(f"  + Client: {client_data['name']}")

        db.commit()
        print("\n✅ Seed complete! Login with:")
        print(f"   Email:    {FIRM['email']}")
        print(f"   Password: {FIRM['password']}")

    finally:
        db.close()


if __name__ == "__main__":
    main()
