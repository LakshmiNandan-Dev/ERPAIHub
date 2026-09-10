"""Add EBS identity-mapping tables for embedded EBSMCP entitlement

Ported from EBSMCP's identity-service schema so PostgresIdentityResolver
can resolve a subject (email) -> mapped role + Org-ID scope + instance
scope against OraEBSAgent's own Postgres. Postgres-only (no Oracle-dialect
variant, unlike upstream). EBSMCP's admin_accounts / entra_registrations
are deliberately NOT ported — OraEBSAgent's IAM/RBAC replaces them. No
audit_log table either: OraEBSAgent already has one, and the vendored
AuditLogger emits to stdout.

Revision ID: b2c4f6a8d013
Revises: a7f3e1c02b98
Create Date: 2026-09-09 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'b2c4f6a8d013'
down_revision: Union[str, Sequence[str], None] = 'a7f3e1c02b98'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ENVIRONMENTS = "('dev', 'test', 'uat', 'prod')"
_TARGET_SYSTEMS = "('ebs', 'fusion', 'ebs_dba')"
_RESOLUTION_SOURCES = "('resolved_from_source', 'manually_overridden')"


def upgrade() -> None:
    op.create_table(
        'identity_mappings',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        # Holds the OraEBSAgent user's email (the authenticated subject).
        # Column name kept from upstream to match the vendored resolver.
        sa.Column('entra_subject', sa.String(length=320), nullable=False),
        sa.Column('environment', sa.String(length=10), nullable=False),
        sa.Column('target_system', sa.String(length=10), nullable=False),
        sa.Column('target_username', sa.String(length=100), nullable=True),
        sa.Column('domain', sa.String(length=40), nullable=True),
        sa.Column('mapped_role', sa.String(length=240), nullable=False),
        sa.Column('resolution_source', sa.String(length=24), nullable=False,
                  server_default='resolved_from_source'),
        sa.Column('effective_start_date', sa.DateTime(timezone=True), nullable=False),
        sa.Column('effective_end_date', sa.DateTime(timezone=True), nullable=True),
        sa.Column('instance_scope_restricted', sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('created_by', sa.String(length=320), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_by', sa.String(length=320), nullable=True),
        sa.CheckConstraint(f"environment IN {_ENVIRONMENTS}",
                           name='ck_identity_mappings_environment'),
        sa.CheckConstraint(f"target_system IN {_TARGET_SYSTEMS}",
                           name='ck_identity_mappings_target_system'),
        sa.CheckConstraint(f"resolution_source IN {_RESOLUTION_SOURCES}",
                           name='ck_identity_mappings_resolution_source'),
        sa.CheckConstraint(
            "effective_end_date IS NULL OR effective_end_date > effective_start_date",
            name='ck_identity_mappings_date_order'),
        sa.CheckConstraint("target_system = 'ebs_dba' OR target_username IS NOT NULL",
                           name='ck_identity_mappings_username_required_unless_dba'),
        sa.CheckConstraint("target_system = 'ebs_dba' OR domain IS NOT NULL",
                           name='ck_identity_mappings_domain_required_unless_dba'),
    )
    # At most one OPEN mapping per (subject, environment, target_system, domain).
    # domain coalesced to '' so NULL domains (ebs_dba) can't slip past a plain
    # unique index. Partial: only rows still in effect.
    op.execute(
        "CREATE UNIQUE INDEX ux_identity_mappings_open_ended "
        "ON identity_mappings (entra_subject, environment, target_system, "
        "COALESCE(domain, '')) WHERE effective_end_date IS NULL"
    )

    op.create_table(
        'identity_mapping_org_scope',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('identity_mapping_id', sa.Integer(),
                  sa.ForeignKey('identity_mappings.id', ondelete='CASCADE'), nullable=False),
        sa.Column('org_id', sa.String(length=64), nullable=False),
        sa.Column('org_name', sa.String(length=240), nullable=True),
        sa.Column('resolved_from_source', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.UniqueConstraint('identity_mapping_id', 'org_id', name='ux_mapping_org_scope_no_dupes'),
    )

    op.create_table(
        'identity_mapping_instance_scope',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('identity_mapping_id', sa.Integer(),
                  sa.ForeignKey('identity_mappings.id', ondelete='CASCADE'), nullable=False),
        # An ebs_environments.name (DEV / UAT / PROD ...) this mapping may reach,
        # meaningful only when the parent's instance_scope_restricted is true.
        sa.Column('instance_name', sa.String(length=64), nullable=False),
        sa.UniqueConstraint('identity_mapping_id', 'instance_name',
                            name='ux_mapping_instance_scope_no_dupes'),
    )


def downgrade() -> None:
    op.drop_table('identity_mapping_instance_scope')
    op.drop_table('identity_mapping_org_scope')
    op.execute("DROP INDEX IF EXISTS ux_identity_mappings_open_ended")
    op.drop_table('identity_mappings')
