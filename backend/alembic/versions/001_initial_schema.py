"""Initial schema — all ComplAI tables

Revision ID: 001
Revises:
Create Date: 2025-01-01 00:00:00
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # firms
    op.create_table('firms',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('password_hash', sa.String(length=255), nullable=False),
        sa.Column('whatsapp_number', sa.String(length=20), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_firms_id', 'firms', ['id'])
    op.create_index('ix_firms_email', 'firms', ['email'], unique=True)

    # clients
    op.create_table('clients',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('firm_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('gstin', sa.String(length=15), nullable=True),
        sa.Column('whatsapp_number', sa.String(length=20), nullable=True),
        sa.Column('email', sa.String(length=255), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['firm_id'], ['firms.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_clients_id', 'clients', ['id'])
    op.create_index('ix_clients_gstin', 'clients', ['gstin'])

    # documents
    op.create_table('documents',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('client_id', sa.Integer(), nullable=False),
        sa.Column('firm_id', sa.Integer(), nullable=False),
        sa.Column('file_path', sa.String(length=500), nullable=False),
        sa.Column('file_type', sa.Enum('pdf','jpg','png','heic','xlsx','csv', name='documentfiletype'), nullable=False),
        sa.Column('source_channel', sa.Enum('upload','whatsapp','email', name='documentsourcechannel'), nullable=True),
        sa.Column('status', sa.Enum('queued','processing','done','error', name='documentstatus'), nullable=True),
        sa.Column('month_year', sa.String(length=7), nullable=True),
        sa.Column('cost_usd', sa.Float(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ),
        sa.ForeignKeyConstraint(['firm_id'], ['firms.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_documents_id', 'documents', ['id'])
    op.create_index('ix_documents_status', 'documents', ['status'])

    # invoices
    op.create_table('invoices',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('document_id', sa.Integer(), nullable=False),
        sa.Column('client_id', sa.Integer(), nullable=False),
        sa.Column('vendor_name', sa.String(length=500), nullable=True),
        sa.Column('vendor_gstin', sa.String(length=15), nullable=True),
        sa.Column('buyer_gstin', sa.String(length=15), nullable=True),
        sa.Column('invoice_number', sa.String(length=100), nullable=True),
        sa.Column('invoice_date', sa.Date(), nullable=True),
        sa.Column('line_items', postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column('taxable_value', sa.Float(), nullable=True),
        sa.Column('cgst', sa.Float(), nullable=True),
        sa.Column('sgst', sa.Float(), nullable=True),
        sa.Column('igst', sa.Float(), nullable=True),
        sa.Column('total_gst', sa.Float(), nullable=True),
        sa.Column('invoice_total', sa.Float(), nullable=True),
        sa.Column('transaction_type', sa.Enum('intrastate','interstate', name='transactiontype'), nullable=True),
        sa.Column('ocr_confidence', sa.Float(), nullable=True),
        sa.Column('claude_confidence', sa.Float(), nullable=True),
        sa.Column('combined_confidence', sa.Float(), nullable=True),
        sa.Column('needs_review', sa.Boolean(), nullable=True),
        sa.Column('human_reviewed', sa.Boolean(), nullable=True),
        sa.Column('status', sa.Enum('pending','auto_accepted','needs_review','approved','rejected', name='invoicestatus'), nullable=True),
        sa.Column('back_calculated', sa.Boolean(), nullable=True),
        sa.Column('back_calc_note', sa.Text(), nullable=True),
        sa.Column('source_pages', sa.Text(), nullable=True),
        sa.Column('issues', postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column('dedup_hash', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_invoices_id', 'invoices', ['id'])
    op.create_index('ix_invoices_invoice_number', 'invoices', ['invoice_number'])
    op.create_index('ix_invoices_status', 'invoices', ['status'])
    op.create_index('ix_invoices_dedup_hash', 'invoices', ['dedup_hash'])

    # extraction_corrections
    op.create_table('extraction_corrections',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('invoice_id', sa.Integer(), nullable=False),
        sa.Column('firm_id', sa.Integer(), nullable=False),
        sa.Column('field_name', sa.String(length=100), nullable=False),
        sa.Column('extracted_value', sa.Text(), nullable=True),
        sa.Column('corrected_value', sa.Text(), nullable=True),
        sa.Column('confidence_at_extraction', sa.Float(), nullable=True),
        sa.Column('corrected_at', sa.DateTime(), nullable=True),
        sa.Column('document_file_path', sa.String(length=500), nullable=True),
        sa.ForeignKeyConstraint(['firm_id'], ['firms.id'], ),
        sa.ForeignKeyConstraint(['invoice_id'], ['invoices.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )

    # bank_transactions
    op.create_table('bank_transactions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('client_id', sa.Integer(), nullable=False),
        sa.Column('firm_id', sa.Integer(), nullable=False),
        sa.Column('transaction_date', sa.Date(), nullable=False),
        sa.Column('narration', sa.Text(), nullable=True),
        sa.Column('debit', sa.Float(), nullable=True),
        sa.Column('credit', sa.Float(), nullable=True),
        sa.Column('balance', sa.Float(), nullable=True),
        sa.Column('voucher_type', sa.Enum('receipt','payment','contra','journal', name='vouchertype'), nullable=True),
        sa.Column('matched_ledger', sa.String(length=500), nullable=True),
        sa.Column('match_confirmed', sa.Boolean(), nullable=True),
        sa.Column('month_year', sa.String(length=7), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ),
        sa.ForeignKeyConstraint(['firm_id'], ['firms.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )

    # notices
    op.create_table('notices',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('client_id', sa.Integer(), nullable=False),
        sa.Column('firm_id', sa.Integer(), nullable=False),
        sa.Column('notice_type', sa.String(length=100), nullable=True),
        sa.Column('notice_date', sa.Date(), nullable=True),
        sa.Column('due_date', sa.Date(), nullable=True),
        sa.Column('status', sa.Enum('open','in_progress','replied', name='noticestatus'), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ),
        sa.ForeignKeyConstraint(['firm_id'], ['firms.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )

    # job_statuses
    op.create_table('job_statuses',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('firm_id', sa.Integer(), nullable=False),
        sa.Column('document_id', sa.Integer(), nullable=False),
        sa.Column('status', sa.Enum('queued','processing','done','error', name='jobstatusenum'), nullable=True),
        sa.Column('chunks_total', sa.Integer(), nullable=True),
        sa.Column('chunks_done', sa.Integer(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ),
        sa.ForeignKeyConstraint(['firm_id'], ['firms.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_job_statuses_status', 'job_statuses', ['status'])


def downgrade() -> None:
    op.drop_table('job_statuses')
    op.drop_table('notices')
    op.drop_table('bank_transactions')
    op.drop_table('extraction_corrections')
    op.drop_table('invoices')
    op.drop_table('documents')
    op.drop_table('clients')
    op.drop_table('firms')
