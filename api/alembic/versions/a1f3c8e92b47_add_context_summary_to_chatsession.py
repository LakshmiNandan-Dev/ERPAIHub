"""Add context_summary columns to ChatSession for history compression

Revision ID: a1f3c8e92b47
Revises: b9af27fc75ef
Create Date: 2026-06-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1f3c8e92b47'
down_revision: Union[str, Sequence[str], None] = 'b9af27fc75ef'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('chat_sessions', sa.Column('context_summary', sa.Text(), nullable=True))
    op.add_column('chat_sessions', sa.Column('summarized_through_id', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('chat_sessions', 'summarized_through_id')
    op.drop_column('chat_sessions', 'context_summary')
