"""Add sso_settings table for Entra ID (OIDC) SSO configuration

Revision ID: d4b8f1027e6a
Revises: c7e1a4d2f9b8
Create Date: 2026-06-03 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd4b8f1027e6a'
down_revision: Union[str, Sequence[str], None] = 'c7e1a4d2f9b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'sso_settings',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('provider', sa.String(length=50), server_default='entra', nullable=False),
        sa.Column('enabled', sa.Boolean(), server_default=sa.text('FALSE'), nullable=False),
        sa.Column('tenant_id', sa.String(length=255), nullable=True),
        sa.Column('client_id', sa.String(length=255), nullable=True),
        sa.Column('client_secret_enc', sa.Text(), nullable=True),
        sa.Column('redirect_uri', sa.String(length=512), nullable=True),
        sa.Column('auto_provision', sa.Boolean(), server_default=sa.text('TRUE'), nullable=False),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('sso_settings')
