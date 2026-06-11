"""Add role to users (admin | dba | user) and backfill admins

Revision ID: f9a2c6b48e35
Revises: e7c4a9b62d18
Create Date: 2026-06-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f9a2c6b48e35'
down_revision: Union[str, Sequence[str], None] = 'e7c4a9b62d18'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('role', sa.String(length=20), server_default='user', nullable=False))
    # Existing administrators get the 'admin' role.
    op.execute("UPDATE users SET role = 'admin' WHERE is_admin = TRUE")


def downgrade() -> None:
    op.drop_column('users', 'role')
