"""Admin API for EBS identity mappings.

The mapping is the grant behind every EBS tool call, so these tests pin the
things that decide whether access is correct and reversible: the persona rules
that the table's CheckConstraints enforce (restated here as readable errors),
one open grant per subject, and revocation that keeps the record.
"""
import uuid

import pytest


def _subject() -> str:
    return f"dba_{uuid.uuid4().hex[:6]}@example.com"


def _dba(**overrides) -> dict:
    body = {
        "entra_subject": _subject(),
        "environment": "prod",
        "target_system": "ebs_dba",
        "mapped_role": "Senior DBA",
    }
    body.update(overrides)
    return body


def _functional(**overrides) -> dict:
    body = {
        "entra_subject": _subject(),
        "environment": "prod",
        "target_system": "ebs",
        "mapped_role": "AP Clerk",
        "target_username": "JDOE",
        "domain": "finance",
    }
    body.update(overrides)
    return body


class TestMappingCreation:

    def test_dba_mapping_is_created_and_listed(self, client, admin_headers):
        body = _dba()
        r = client.post("/admin/identity-mappings", json=body, headers=admin_headers)
        assert r.status_code == 201, r.text
        created = r.json()
        assert created["entra_subject"] == body["entra_subject"]
        assert created["mapped_role"] == "Senior DBA"
        assert created["effective_end_date"] is None
        # created_by is NOT NULL on the table — the admin's identity fills it.
        assert created["created_by"]

        listed = client.get("/admin/identity-mappings", headers=admin_headers).json()
        assert any(m["id"] == created["id"] for m in listed)

    def test_subject_is_normalised_so_lookup_matches_login(self, client, admin_headers):
        """Resolution compares the subject verbatim, so a mapping typed in mixed
        case would silently never match the user's email."""
        r = client.post("/admin/identity-mappings",
                        json=_dba(entra_subject="  MixedCase@Example.COM "),
                        headers=admin_headers)
        assert r.status_code == 201
        assert r.json()["entra_subject"] == "mixedcase@example.com"

    def test_functional_mapping_keeps_username_and_domain(self, client, admin_headers):
        r = client.post("/admin/identity-mappings", json=_functional(), headers=admin_headers)
        assert r.status_code == 201, r.text
        assert r.json()["target_username"] == "JDOE"
        assert r.json()["domain"] == "finance"


class TestPersonaRules:
    """Each of these is a CheckConstraint on the table; the API states it in
    words first so an admin gets something they can act on."""

    def test_functional_mapping_requires_an_ebs_username(self, client, admin_headers):
        r = client.post("/admin/identity-mappings",
                        json=_functional(target_username=None), headers=admin_headers)
        assert r.status_code == 400
        assert "username" in r.json()["detail"].lower()

    def test_functional_mapping_requires_a_domain(self, client, admin_headers):
        r = client.post("/admin/identity-mappings",
                        json=_functional(domain=None), headers=admin_headers)
        assert r.status_code == 400
        assert "domain" in r.json()["detail"].lower()

    def test_dba_mapping_rejects_an_ebs_username(self, client, admin_headers):
        r = client.post("/admin/identity-mappings",
                        json=_dba(target_username="JDOE"), headers=admin_headers)
        assert r.status_code == 400

    def test_dba_mapping_rejects_org_scope(self, client, admin_headers):
        """ebs_dba is all-or-nothing: a resolved ebs_dba identity carries no Org
        IDs, so accepting them here would imply a limit that does not exist."""
        r = client.post("/admin/identity-mappings",
                        json=_dba(org_scope=["204"]), headers=admin_headers)
        assert r.status_code == 400
        assert "org" in r.json()["detail"].lower()


class TestInstanceScope:

    def test_restricting_to_no_instances_is_refused(self, client, admin_headers):
        r = client.post("/admin/identity-mappings",
                        json=_dba(instance_scope_restricted=True, instance_scope=[]),
                        headers=admin_headers)
        assert r.status_code == 400

    def test_unknown_instance_is_refused_naming_what_exists(self, client, admin_headers, nonprod_env):
        r = client.post("/admin/identity-mappings",
                        json=_dba(instance_scope_restricted=True, instance_scope=["NOPE"]),
                        headers=admin_headers)
        assert r.status_code == 400
        assert "NOPE" in r.json()["detail"]

    def test_known_instance_is_stored_uppercased(self, client, admin_headers, nonprod_env):
        name = nonprod_env["name"]
        r = client.post("/admin/identity-mappings",
                        json=_dba(instance_scope_restricted=True, instance_scope=[name.lower()]),
                        headers=admin_headers)
        assert r.status_code == 201, r.text
        assert r.json()["instance_scope"] == [name.upper()]


class TestOneOpenGrant:

    def test_second_open_mapping_for_the_same_subject_is_refused(self, client, admin_headers):
        body = _dba()
        assert client.post("/admin/identity-mappings", json=body,
                           headers=admin_headers).status_code == 201
        r = client.post("/admin/identity-mappings", json=body, headers=admin_headers)
        assert r.status_code == 409
        assert "close it" in r.json()["detail"].lower()

    def test_a_new_grant_is_allowed_once_the_old_one_is_closed(self, client, admin_headers):
        body = _dba()
        first = client.post("/admin/identity-mappings", json=body, headers=admin_headers).json()
        assert client.post(f"/admin/identity-mappings/{first['id']}/close",
                           json={"reason": "left the team"}, headers=admin_headers).status_code == 200
        r = client.post("/admin/identity-mappings", json=body, headers=admin_headers)
        assert r.status_code == 201


class TestRevocation:

    def test_closing_keeps_the_row_with_an_end_date(self, client, admin_headers):
        """Revocation must leave evidence — effective_end_date exists so the
        record of who could do what, and when, outlives the grant."""
        created = client.post("/admin/identity-mappings", json=_dba(),
                              headers=admin_headers).json()
        r = client.post(f"/admin/identity-mappings/{created['id']}/close",
                        json={"reason": "role change"}, headers=admin_headers)
        assert r.status_code == 200
        assert r.json()["effective_end_date"] is not None
        assert r.json()["updated_by"]

        still_there = client.get("/admin/identity-mappings", headers=admin_headers).json()
        assert any(m["id"] == created["id"] for m in still_there)

    def test_closed_mappings_are_excluded_by_open_only(self, client, admin_headers):
        created = client.post("/admin/identity-mappings", json=_dba(),
                              headers=admin_headers).json()
        client.post(f"/admin/identity-mappings/{created['id']}/close",
                    json={}, headers=admin_headers)
        open_rows = client.get("/admin/identity-mappings?open_only=true",
                               headers=admin_headers).json()
        assert all(m["id"] != created["id"] for m in open_rows)

    def test_closing_twice_is_refused(self, client, admin_headers):
        created = client.post("/admin/identity-mappings", json=_dba(),
                              headers=admin_headers).json()
        client.post(f"/admin/identity-mappings/{created['id']}/close", json={}, headers=admin_headers)
        r = client.post(f"/admin/identity-mappings/{created['id']}/close", json={},
                        headers=admin_headers)
        assert r.status_code == 400

    def test_delete_removes_it_entirely(self, client, admin_headers):
        created = client.post("/admin/identity-mappings", json=_dba(),
                              headers=admin_headers).json()
        assert client.delete(f"/admin/identity-mappings/{created['id']}",
                             headers=admin_headers).status_code == 204
        rows = client.get("/admin/identity-mappings", headers=admin_headers).json()
        assert all(m["id"] != created["id"] for m in rows)


class TestAccessControl:

    def test_options_describe_the_personas_the_form_must_render(self, client, admin_headers):
        opts = client.get("/admin/identity-mappings/options", headers=admin_headers).json()
        assert set(opts["target_systems"]) == {"ebs", "ebs_dba", "fusion"}
        assert opts["deploy_environment"]
        # The UI branches on these rather than keeping its own copy of the rules.
        assert opts["personas"]["ebs_dba"]["needs_username"] is False
        assert opts["personas"]["ebs"]["needs_username"] is True

    def test_non_admin_cannot_read_or_grant(self, client, regular_user_headers):
        assert client.get("/admin/identity-mappings",
                          headers=regular_user_headers).status_code == 403
        assert client.post("/admin/identity-mappings", json=_dba(),
                           headers=regular_user_headers).status_code == 403
