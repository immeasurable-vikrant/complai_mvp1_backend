"""Widen GSTIN columns from VARCHAR(15) to VARCHAR(20) to tolerate OCR noise

Revision ID: 002
Revises: 001
Create Date: 2026-05-16
"""
from alembic import op
import sqlalchemy as sa

revision = '002'
down_revision = '001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # invoices table — vendor_gstin and buyer_gstin
    op.alter_column('invoices', 'vendor_gstin',
        existing_type=sa.String(length=15),
        type_=sa.String(length=20),
        existing_nullable=True)
    op.alter_column('invoices', 'buyer_gstin',
        existing_type=sa.String(length=15),
        type_=sa.String(length=20),
        existing_nullable=True)
    # clients table — gstin
    op.alter_column('clients', 'gstin',
        existing_type=sa.String(length=15),
        type_=sa.String(length=20),
        existing_nullable=True)


def downgrade() -> None:
    op.alter_column('invoices', 'vendor_gstin',
        existing_type=sa.String(length=20),
        type_=sa.String(length=15),
        existing_nullable=True)
    op.alter_column('invoices', 'buyer_gstin',
        existing_type=sa.String(length=20),
        type_=sa.String(length=15),
        existing_nullable=True)
    op.alter_column('clients', 'gstin',
        existing_type=sa.String(length=20),
        type_=sa.String(length=15),
        existing_nullable=True)
