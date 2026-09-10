"""Identity-mapping tables for the embedded EBSMCP entitlement layer.

Declarative models so create_all provisions them the same way as every
other OraEBSAgent table (see core/bootstrap.py — the live path is
create_all, not Alembic). Schema matches the ported EBSMCP identity-service
tables and migration b2c4f6a8d013; the vendored PostgresIdentityResolver
reads them via its own Core metadata (app.ebsmcp.identity.tables), so the
column names here must stay in step with that.

entra_subject holds the authenticated OraEBSAgent user's email. environment
is the deploy stage (a single configured value, EBS_DEPLOY_ENVIRONMENT),
distinct from which EBS database a call targets (instance scope below,
= ebs_environments.name).
"""

from sqlalchemy import (
    Boolean, CheckConstraint, Column, ForeignKey, Index, Integer, String, Text,
    TIMESTAMP, UniqueConstraint, false, true, func, text,
)
from sqlalchemy.orm import relationship

from app.core.database import Base

_ENVIRONMENTS = "('dev', 'test', 'uat', 'prod')"
_TARGET_SYSTEMS = "('ebs', 'fusion', 'ebs_dba')"
_RESOLUTION_SOURCES = "('resolved_from_source', 'manually_overridden')"


class IdentityMapping(Base):
    __tablename__ = "identity_mappings"

    id = Column(Integer, primary_key=True, nullable=False)
    entra_subject = Column(String(320), nullable=False)   # the user's email
    environment = Column(String(10), nullable=False)      # deploy stage
    target_system = Column(String(10), nullable=False)    # ebs | fusion | ebs_dba
    target_username = Column(String(100), nullable=True)  # FND_USER (not for ebs_dba)
    domain = Column(String(40), nullable=True)            # finance/scm/... (not for ebs_dba)
    mapped_role = Column(String(240), nullable=False)
    resolution_source = Column(String(24), nullable=False, server_default="resolved_from_source")
    effective_start_date = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    effective_end_date = Column(TIMESTAMP(timezone=True), nullable=True)  # NULL = open
    instance_scope_restricted = Column(Boolean, nullable=False, server_default=false())
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    created_by = Column(String(320), nullable=False)
    updated_at = Column(TIMESTAMP(timezone=True), nullable=True, onupdate=func.now())
    updated_by = Column(String(320), nullable=True)

    org_scope = relationship("IdentityMappingOrgScope", cascade="all, delete-orphan", backref="mapping")
    instance_scope = relationship("IdentityMappingInstanceScope", cascade="all, delete-orphan", backref="mapping")

    __table_args__ = (
        CheckConstraint(f"environment IN {_ENVIRONMENTS}", name="ck_identity_mappings_environment"),
        CheckConstraint(f"target_system IN {_TARGET_SYSTEMS}", name="ck_identity_mappings_target_system"),
        CheckConstraint(f"resolution_source IN {_RESOLUTION_SOURCES}", name="ck_identity_mappings_resolution_source"),
        CheckConstraint("effective_end_date IS NULL OR effective_end_date > effective_start_date",
                        name="ck_identity_mappings_date_order"),
        CheckConstraint("target_system = 'ebs_dba' OR target_username IS NOT NULL",
                        name="ck_identity_mappings_username_required_unless_dba"),
        CheckConstraint("target_system = 'ebs_dba' OR domain IS NOT NULL",
                        name="ck_identity_mappings_domain_required_unless_dba"),
        # One OPEN mapping per (subject, env, target_system, domain); NULL domain
        # coalesced so ebs_dba rows can't slip past. Partial unique index.
        Index("ux_identity_mappings_open_ended",
              "entra_subject", "environment", "target_system",
              func.coalesce(text("domain"), text("''")),
              unique=True, postgresql_where=text("effective_end_date IS NULL")),
    )


class IdentityMappingOrgScope(Base):
    __tablename__ = "identity_mapping_org_scope"

    id = Column(Integer, primary_key=True, nullable=False)
    identity_mapping_id = Column(Integer, ForeignKey("identity_mappings.id", ondelete="CASCADE"), nullable=False)
    org_id = Column(String(64), nullable=False)
    org_name = Column(String(240), nullable=True)
    resolved_from_source = Column(Boolean, nullable=False, server_default=true())

    __table_args__ = (
        UniqueConstraint("identity_mapping_id", "org_id", name="ux_mapping_org_scope_no_dupes"),
    )


class IdentityMappingInstanceScope(Base):
    __tablename__ = "identity_mapping_instance_scope"

    id = Column(Integer, primary_key=True, nullable=False)
    identity_mapping_id = Column(Integer, ForeignKey("identity_mappings.id", ondelete="CASCADE"), nullable=False)
    instance_name = Column(String(64), nullable=False)  # an ebs_environments.name

    __table_args__ = (
        UniqueConstraint("identity_mapping_id", "instance_name", name="ux_mapping_instance_scope_no_dupes"),
    )
