"""Add per-provider cost columns to documents

Revision ID: 005
Revises: 004
Create Date: 2026-05-17
"""
from alembic import op
import sqlalchemy as sa

revision = '005'
down_revision = '004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('documents', sa.Column('claude_cost_usd', sa.Float(), nullable=True, server_default='0.0'))
    op.add_column('documents', sa.Column('docai_cost_usd',  sa.Float(), nullable=True, server_default='0.0'))


def downgrade() -> None:
    op.drop_column('documents', 'docai_cost_usd')
    op.drop_column('documents', 'claude_cost_usd')
