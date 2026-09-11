"""PostgresIdentityResolver — multiple open mappings resolve to their union.

The schema's partial unique index keys on (entra_subject, environment,
target_system, coalesce(domain,'')), so a functional user holding both a
finance and an scm grant is two valid open rows, not a data error. The
resolver used to take .first() and silently return whichever the database
handed back, giving a caller the wrong domain's role and org scope with
nothing saying so.

SQLite in-memory against the real table definitions, same rationale as the
upstream resolver test: this SQL is plain SELECT/WHERE/IN/IS NULL with no
dialect-specific construct, so it executes honestly here. Self-contained
because oraebsagent's conftest is Postgres-backed and has no engine fixture.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, insert
from sqlalchemy.pool import StaticPool

from app.ebsmcp.identity import PostgresIdentityResolver
from app.ebsmcp.identity.tables import (
    identity_mapping_instance_scope,
    identity_mapping_org_scope,
    identity_mappings,
    metadata,
)


@pytest.fixture()
def engine():
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    metadata.create_all(eng)
    return eng


@pytest.fixture()
def resolver(engine):
    return PostgresIdentityResolver(db_url="unused", environment="prod", engine=engine)


@pytest.fixture()
def seed(engine):
    def _seed(*, subject="jdoe@corp.com", target_system="ebs", domain=None,
              mapped_role="ROLE", org_ids=(), instances=None, closed=False):
        from datetime import datetime, timezone
        with engine.begin() as conn:
            mid = conn.execute(
                insert(identity_mappings).values(
                    entra_subject=subject, environment="prod",
                    target_system=target_system, mapped_role=mapped_role,
                    domain=domain,
                    effective_end_date=datetime(2020, 1, 1, tzinfo=timezone.utc) if closed else None,
                    instance_scope_restricted=instances is not None,
                )
            ).inserted_primary_key[0]
            for org in org_ids:
                conn.execute(insert(identity_mapping_org_scope).values(
                    identity_mapping_id=mid, org_id=org, resolved_from_source=True))
            for name in instances or ():
                conn.execute(insert(identity_mapping_instance_scope).values(
                    identity_mapping_id=mid, instance_name=name))
        return mid
    return _seed


# ── the single-mapping path must not change ──────────────────────────────────

def test_one_mapping_behaves_exactly_as_before(resolver, seed):
    seed(mapped_role="AP_MANAGER", org_ids=["204", "207"])
    identity = resolver.resolve("jdoe@corp.com", "ebs")
    assert identity.mapped_role == "AP_MANAGER"
    assert set(identity.allowed_org_ids) == {"204", "207"}
    assert identity.allowed_instances is None


def test_unmapped_subject_still_denies(resolver):
    with pytest.raises(LookupError):
        resolver.resolve("nobody@corp.com", "ebs")


def test_closed_mappings_are_excluded_from_the_union(resolver, seed):
    """A revoked grant must not come back through the union."""
    seed(domain="finance", mapped_role="AP_MANAGER", org_ids=["204"])
    seed(domain="scm", mapped_role="INV_MANAGER", org_ids=["999"], closed=True)
    identity = resolver.resolve("jdoe@corp.com", "ebs")
    assert set(identity.allowed_org_ids) == {"204"}
    assert identity.mapped_role == "AP_MANAGER"


# ── the union itself ─────────────────────────────────────────────────────────

def test_org_scope_is_unioned_across_domains(resolver, seed):
    seed(domain="finance", mapped_role="AP_MANAGER", org_ids=["204", "207"])
    seed(domain="scm", mapped_role="INV_MANAGER", org_ids=["301"])
    identity = resolver.resolve("jdoe@corp.com", "ebs")
    assert set(identity.allowed_org_ids) == {"204", "207", "301"}


def test_both_roles_are_reported_not_just_one(resolver, seed):
    """mapped_role is echoed on every answer — showing one of two grants
    would misrepresent who the caller is."""
    seed(domain="finance", mapped_role="AP_MANAGER", org_ids=["204"])
    seed(domain="scm", mapped_role="INV_MANAGER", org_ids=["301"])
    assert resolver.resolve("jdoe@corp.com", "ebs").mapped_role == "AP_MANAGER, INV_MANAGER"


def test_overlapping_org_ids_are_not_duplicated(resolver, seed):
    seed(domain="finance", mapped_role="A", org_ids=["204", "207"])
    seed(domain="scm", mapped_role="B", org_ids=["207", "301"])
    ids = resolver.resolve("jdoe@corp.com", "ebs").allowed_org_ids
    assert sorted(ids) == ["204", "207", "301"]
    assert len(ids) == len(set(ids))


# ── instance scope: union means BROADER ──────────────────────────────────────

def test_one_unrestricted_mapping_makes_the_union_unrestricted(resolver, seed):
    """None means "not instance-scoped" and is the wider grant. Treating it
    as an empty set would silently narrow access instead of widening it."""
    seed(domain="finance", mapped_role="A", org_ids=["204"], instances=None)
    seed(domain="scm", mapped_role="B", org_ids=["301"], instances=["DEV"])
    assert resolver.resolve("jdoe@corp.com", "ebs").allowed_instances is None


def test_all_restricted_mappings_union_their_allowlists(resolver, seed):
    seed(domain="finance", mapped_role="A", org_ids=["204"], instances=["DEV"])
    seed(domain="scm", mapped_role="B", org_ids=["301"], instances=["UAT"])
    assert set(resolver.resolve("jdoe@corp.com", "ebs").allowed_instances) == {"DEV", "UAT"}


def test_explicit_deny_all_does_not_shrink_a_real_allowlist(resolver, seed):
    """An empty tuple is an explicit zero-instance grant; unioned with a real
    allowlist the broader one wins."""
    seed(domain="finance", mapped_role="A", org_ids=["204"], instances=[])
    seed(domain="scm", mapped_role="B", org_ids=["301"], instances=["DEV"])
    assert set(resolver.resolve("jdoe@corp.com", "ebs").allowed_instances) == {"DEV"}


# ── personas stay separated ──────────────────────────────────────────────────

def test_a_different_persona_is_never_unioned_in(resolver, seed):
    """ebs_dba is all-or-nothing and must not absorb a functional grant's
    org scope, nor the reverse."""
    seed(domain="finance", target_system="ebs", mapped_role="AP_MANAGER", org_ids=["204"])
    seed(domain=None, target_system="ebs_dba", mapped_role="Senior DBA", org_ids=[])
    dba = resolver.resolve("jdoe@corp.com", "ebs_dba")
    assert dba.mapped_role == "Senior DBA"
    assert dba.allowed_org_ids == ()
    assert resolver.resolve("jdoe@corp.com", "ebs").mapped_role == "AP_MANAGER"


def test_ebs_dba_single_mapping_is_unchanged(resolver, seed):
    """domain is always NULL for ebs_dba, so at most one mapping is ever
    open — the path every mounted tool takes today."""
    seed(domain=None, target_system="ebs_dba", mapped_role="Senior DBA", org_ids=[])
    identity = resolver.resolve("jdoe@corp.com", "ebs_dba")
    assert identity.mapped_role == "Senior DBA"
    assert identity.allowed_org_ids == ()
