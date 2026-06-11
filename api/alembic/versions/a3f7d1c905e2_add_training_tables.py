"""Add local-model training tables (training_examples, training_jobs, agent_models)

Revision ID: a3f7d1c905e2
Revises: f9a2c6b48e35
Create Date: 2026-06-04 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = 'a3f7d1c905e2'
down_revision: Union[str, Sequence[str], None] = 'f9a2c6b48e35'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "training_examples",
        sa.Column("id", sa.Integer, primary_key=True, nullable=False),
        sa.Column("agent", sa.String(40), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False, server_default="sft"),
        sa.Column("source", sa.String(20), nullable=False, server_default="upload"),
        sa.Column("system", sa.Text, nullable=True),
        sa.Column("prompt", sa.Text, nullable=False),
        sa.Column("chosen", sa.Text, nullable=False),
        sa.Column("rejected", sa.Text, nullable=True),
        sa.Column("is_active", sa.Boolean, server_default="TRUE", nullable=False),
        sa.Column("created_by", sa.Integer, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_training_examples_agent", "training_examples", ["agent"])

    op.create_table(
        "training_jobs",
        sa.Column("id", sa.Integer, primary_key=True, nullable=False),
        sa.Column("agent", sa.String(40), nullable=False),
        sa.Column("base_model", sa.String(150), nullable=False),
        sa.Column("method", sa.String(10), nullable=False, server_default="both"),
        sa.Column("target_model_tag", sa.String(150), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("sft_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("dpo_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("params", JSONB, nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("created_by", sa.Integer, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("bundled_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("registered_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )

    op.create_table(
        "agent_models",
        sa.Column("id", sa.Integer, primary_key=True, nullable=False),
        sa.Column("agent", sa.String(40), nullable=False, unique=True),
        sa.Column("provider", sa.String(20), nullable=False, server_default="ollama"),
        sa.Column("model", sa.String(150), nullable=False),
        sa.Column("training_job_id", sa.Integer, sa.ForeignKey("training_jobs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("is_active", sa.Boolean, server_default="TRUE", nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("agent_models")
    op.drop_table("training_jobs")
    op.drop_index("ix_training_examples_agent", table_name="training_examples")
    op.drop_table("training_examples")
