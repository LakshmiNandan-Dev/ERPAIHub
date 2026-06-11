"""Add integration_credentials (Git / Confluence) table

Revision ID: f1a6b3d92c84
Revises: e9c2a7d51b03
Create Date: 2026-06-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f1a6b3d92c84'
down_revision: Union[str, Sequence[str], None] = 'e9c2a7d51b03'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'integration_credentials',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('host', sa.String(length=255), nullable=True),
        sa.Column('base_url', sa.String(length=512), nullable=True),
        sa.Column('username', sa.String(length=255), nullable=True),
        sa.Column('secret_enc', sa.Text(), nullable=True),
        sa.Column('is_default', sa.Boolean(), server_default=sa.text('FALSE'), nullable=False),
        sa.Column('is_active', sa.Boolean(), server_default=sa.text('TRUE'), nullable=False),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    )
    op.create_index('ix_integration_credentials_kind', 'integration_credentials', ['kind'])


def downgrade() -> None:
    op.drop_index('ix_integration_credentials_kind', table_name='integration_credentials')
    op.drop_table('integration_credentials')
