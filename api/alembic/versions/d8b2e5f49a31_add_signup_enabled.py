"""Add signup_enabled to sso_settings (admin-controlled public sign-up)

Revision ID: d8b2e5f49a31
Revises: c5f1d8a3e207
Create Date: 2026-06-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd8b2e5f49a31'
down_revision: Union[str, Sequence[str], None] = 'c5f1d8a3e207'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('sso_settings', sa.Column('signup_enabled', sa.Boolean(),
                                            server_default=sa.text('FALSE'), nullable=False))


def downgrade() -> None:
    op.drop_column('sso_settings', 'signup_enabled')
