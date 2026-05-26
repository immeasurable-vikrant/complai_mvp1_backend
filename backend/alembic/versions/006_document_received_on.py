"""Add received_on to documents (WhatsApp number the client sent to)

Revision ID: 006
Revises: 005
Create Date: 2026-05-26
"""
from alembic import op
import sqlalchemy as sa

revision = '006'
down_revision = '005'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'documents',
        sa.Column('received_on', sa.String(20), nullable=True),
    )


def downgrade():
    op.drop_column('documents', 'received_on')
