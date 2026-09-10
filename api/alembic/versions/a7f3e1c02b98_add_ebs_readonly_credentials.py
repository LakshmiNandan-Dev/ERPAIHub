"""Add read-only EBS credentials to ebs_environments (for embedded EBSMCP)

The embedded EBSMCP tools (app.ebsmcp) must connect with a dedicated
least-privilege, read-only account — not the APPS account in
db_user/db_password_enc, which is far too privileged for AI-driven reads.
EBSMCP's safety rests on the database refusing writes, so it needs its own
credential per environment.

Revision ID: a7f3e1c02b98
Revises: c8e2f5a91d04
Create Date: 2026-09-09 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a7f3e1c02b98'
down_revision: Union[str, Sequence[str], None] = 'c8e2f5a91d04'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('ebs_environments', sa.Column('readonly_user', sa.String(length=100), nullable=True))
    op.add_column('ebs_environments', sa.Column('readonly_password_enc', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('ebs_environments', 'readonly_password_enc')
    op.drop_column('ebs_environments', 'readonly_user')
