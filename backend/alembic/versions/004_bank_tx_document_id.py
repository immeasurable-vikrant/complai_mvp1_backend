"""Add document_id to bank_transactions

Revision ID: 004
Revises: 003
Create Date: 2026-05-17
"""
from alembic import op
import sqlalchemy as sa

revision = '004'
down_revision = '003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # Add document_id column (nullable so existing rows keep working)
    conn.execute(sa.text(
        "ALTER TABLE bank_transactions "
        "ADD COLUMN IF NOT EXISTS document_id INTEGER REFERENCES documents(id)"
    ))

    # Index for fast lookup by document_id (used when fetching results and deduplicating)
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_bank_transactions_document_id "
        "ON bank_transactions (document_id)"
    ))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP INDEX IF EXISTS ix_bank_transactions_document_id"))
    conn.execute(sa.text(
        "ALTER TABLE bank_transactions DROP COLUMN IF EXISTS document_id"
    ))
