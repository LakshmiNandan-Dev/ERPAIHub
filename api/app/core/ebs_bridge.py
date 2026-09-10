"""Glue between OraEBSAgent and the embedded EBSMCP library (app.ebsmcp).

This module is OraEBSAgent's own code — NOT part of the vendored package —
so app.ebsmcp stays a clean, re-vendorable copy of upstream. It supplies
the two things the library needs from its host:

  1. WHO is calling — bind_ebs_subject() asserts the authenticated user's
     email as the EBSMCP subject, per request (the authentication seam).
  2. WHICH databases it may reach — build_ebs_connectors() reads the
     canonical ebs_environments registry and builds one read-only Oracle
     connector per environment, replacing EBSMCP's own env-var config.
"""

from __future__ import annotations

import os

from fastapi import Depends
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from app.core import crypto, database
from app.core.auth.auth import get_current_user
from app.ebsmcp.connectors import (
    EBSConnector,
    OracleEBSConnector,
    init_thick_mode_if_configured,
)
from app.ebsmcp.context import set_current_subject
from app.ebsmcp.audit import AuditLogger
from app.ebsmcp.identity import IdentityResolver, PostgresIdentityResolver
from app.ebsmcp.policy import EntitlementFilter
from app.ebsmcp.tools import ToolContext
from app.models.infra import EbsEnvironment

# Thick mode (python-oracledb + Oracle Instant Client) is needed only for EBS
# accounts the server authenticates with the 10g verifier — i.e. instances
# running SEC_CASE_SENSITIVE_LOGON=FALSE, which thin mode rejects with
# DPY-3015. It requires the Instant Client to be present in OraEBSAgent's api
# image (not there by default), so it is OPT-IN: set EBS_ORACLE_THICK_MODE=true
# only once the client is installed and the target instance actually needs it.
# Default false = thin mode, which needs no client and works for
# modern-verifier accounts.
_THICK_MODE = os.getenv("EBS_ORACLE_THICK_MODE", "false").lower() in ("1", "true", "yes")


def bind_ebs_subject(user=Depends(get_current_user)) -> str:
    """FastAPI dependency: assert the authenticated user as the EBSMCP subject.

    Chain it AFTER authentication on any route that calls EBSMCP tools:

        @router.post("/ask", dependencies=[Depends(bind_ebs_subject)])

    `email` is the join key into EBSMCP's identity_mappings and the audit
    subject — the same identity OraEBSAgent already authenticated.
    """
    set_current_subject(user.email)
    return user.email


def build_ebs_connectors(db: Session | None = None) -> dict[str, EBSConnector]:
    """One read-only OracleEBSConnector per active EBS environment.

    Reads connection details from ebs_environments (the single source of
    truth, encrypted at rest), using the dedicated read-only account — never
    APPS. Keyed by the uppercased environment name, which is exactly what a
    tool's `instance` parameter expects. Environments without a read-only
    credential configured are skipped (with the reason), rather than silently
    falling back to a privileged account.
    """
    init_thick_mode_if_configured(_THICK_MODE)

    own_session = db is None
    db = db or database.SessionLocal()
    try:
        rows = db.query(EbsEnvironment).filter(EbsEnvironment.is_active.is_(True)).all()
        connectors: dict[str, EBSConnector] = {}
        for env in rows:
            if not env.readonly_user or not env.readonly_password_enc:
                continue  # not onboarded for read-only access yet
            if not (env.db_host and env.db_sid):
                continue
            password = crypto.decrypt(env.readonly_password_enc)
            if not password:
                continue  # undecryptable — degrade rather than connect wrong
            dsn = f"{env.db_host}:{env.db_port or 1521}/{env.db_sid}"
            connectors[env.name.upper()] = OracleEBSConnector(
                dsn=dsn, user=env.readonly_user, password=password
            )
        return connectors
    finally:
        if own_session:
            db.close()


# EBSMCP's identity_mappings.environment is the DEPLOY STAGE (dev/test/uat/
# prod) — a DIFFERENT axis from which EBS database a call targets (that is
# the instance / ebs_environments.name). OraEBSAgent is one embedded
# deployment, so this is a single configured value; every identity mapping
# must use it. Instance scope (below) is what restricts which EBS databases
# a subject may reach.
EBS_DEPLOY_ENVIRONMENT = os.getenv("EBS_DEPLOY_ENVIRONMENT", "prod")

# OraEBSAgent's own database — the single Postgres that also holds the
# identity_mappings tables (see migration b2c4f6a8d013).
_IDENTITY_DB_URL = os.getenv("DATABASE_URL")


# One engine for the whole process, created on first use and shared by every
# resolver after that. PostgresIdentityResolver would otherwise call
# create_engine() in its constructor — and a context is built per chat turn, so
# that is a fresh connection pool per request, none of them ever disposed,
# until Postgres starts refusing connections. The resolver takes an engine
# precisely so the host application can own its lifetime; this is that owner.
_identity_engine: Engine | None = None


def _get_identity_engine() -> Engine:
    global _identity_engine
    if _identity_engine is None:
        _identity_engine = create_engine(_IDENTITY_DB_URL, pool_pre_ping=True)
    return _identity_engine


def build_identity_resolver(environment: str = EBS_DEPLOY_ENVIRONMENT) -> IdentityResolver:
    """Resolve subjects against the identity_mappings tables in OraEBSAgent's
    own Postgres. Same physical DB as everything else; the resolver just
    opens its own indexed reads on the request hot path, over the shared
    engine above.
    """
    return PostgresIdentityResolver(db_url=_IDENTITY_DB_URL, environment=environment,
                                    engine=_get_identity_engine())


def build_tool_context(db: Session | None = None,
                       environment: str = EBS_DEPLOY_ENVIRONMENT) -> ToolContext:
    """Assemble the full EBSMCP request pipeline for OraEBSAgent to call tools
    through: read-only connectors per EBS environment, the Postgres identity
    resolver, the entitlement filter, and the (stdout) audit logger.

    The caller's subject is supplied separately, per request, via
    set_current_subject / bind_ebs_subject — not baked into this context.
    dev_subject is only a last-resort fallback and should never be hit in a
    real request, since bind_ebs_subject always sets a real subject.
    """
    return ToolContext(
        connectors=build_ebs_connectors(db),
        identity_resolver=build_identity_resolver(environment),
        entitlement=EntitlementFilter(),
        audit=AuditLogger(),
        environment=environment,
        dev_subject="unauthenticated@local",
    )
