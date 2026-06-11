"""Add approval_status to users (admin approval for self sign-up)

Revision ID: e7c4a9b62d18
Revises: d8b2e5f49a31
Create Date: 2026-06-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e7c4a9b62d18'
down_revision: Union[str, Sequence[str], None] = 'd8b2e5f49a31'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing accounts default to 'approved'.
    op.add_column('users', sa.Column('approval_status', sa.String(length=20),
                                     server_default='approved', nullable=False))


def downgrade() -> None:
    op.drop_column('users', 'approval_status')
