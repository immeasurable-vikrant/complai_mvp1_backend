"""Add media_url to documents (Twilio URL for requeue on failure)

Revision ID: 007
Revises: 006
Create Date: 2026-05-27
"""
from alembic import op
import sqlalchemy as sa

revision = '007'
down_revision = '006'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'documents',
        sa.Column('media_url', sa.String(500), nullable=True),
    )


def downgrade():
    op.drop_column('documents', 'media_url')
