"""Add reference/mode to bank_transactions and create bank_statement_meta table

Revision ID: 003
Revises: 002
Create Date: 2026-05-16
"""
from alembic import op
import sqlalchemy as sa

revision = '003'
down_revision = '002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # ── bank_transactions: add reference + mode (idempotent) ──────────────────
    # PostgreSQL ADD COLUMN IF NOT EXISTS avoids failure if column already exists
    conn.execute(sa.text(
        "ALTER TABLE bank_transactions "
        "ADD COLUMN IF NOT EXISTS reference VARCHAR(200), "
        "ADD COLUMN IF NOT EXISTS mode VARCHAR(50)"
    ))

    # ── bank_statement_meta: create table if it doesn't exist ─────────────────
    # The table may already exist if SQLAlchemy create_all() ran before this migration
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS bank_statement_meta (
            id               SERIAL PRIMARY KEY,
            document_id      INTEGER NOT NULL UNIQUE REFERENCES documents(id),
            client_id        INTEGER NOT NULL REFERENCES clients(id),
            firm_id          INTEGER NOT NULL REFERENCES firms(id),
            account_number   VARCHAR(50),
            account_holder   VARCHAR(200),
            bank_name        VARCHAR(200),
            ifsc_code        VARCHAR(20),
            period_from      DATE,
            period_to        DATE,
            opening_balance  FLOAT,
            closing_balance  FLOAT,
            total_credits    FLOAT,
            total_debits     FLOAT,
            balance_matches  BOOLEAN,
            confidence       FLOAT,
            created_at       TIMESTAMP WITHOUT TIME ZONE
        )
    """))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP TABLE IF EXISTS bank_statement_meta"))
    conn.execute(sa.text(
        "ALTER TABLE bank_transactions "
        "DROP COLUMN IF EXISTS mode, "
        "DROP COLUMN IF EXISTS reference"
    ))
