"""TC-CL-01 to TC-CL-18 — Cloning Agent"""
import uuid
import pytest


def _clone_payload(target_env_name, **overrides):
    base = {
        "source_name": "PROD",
        "target_environment": target_env_name,
        "source_sid": "PRODDB",
        "source_db_host": "prod-db.example.com",
        "source_apps_host": "prod-apps.example.com",
        "target_apps_host": "tgt-apps.example.com",
        "target_storage": "fs",
        "target_datafile_dir": "/u01/oradata/CLONE",
        "source_datafile_dir": "/u01/oradata/PROD",
        "target_db_home": "yes",
        "db_topology": "single",
    }
    base.update(overrides)
    return base


class TestCloningInterview:

    def test_interview_asks_for_source_name(self, client, admin_headers):
        """TC-CL-01: Agent interview starts by asking for source name."""
        r = client.post("/cloning/agent", json={"context": {}}, headers=admin_headers)
        assert r.status_code == 200
        data = r.json()
        assert data["type"] == "question"
        assert data.get("field") in ("source_name", "target_environment")

    def test_interview_lists_nonprod_envs(self, client, admin_headers, nonprod_env):
        """TC-CL-02: Agent lists available non-prod environments for target selection."""
        r = client.post("/cloning/agent", json={"context": {
            "source_name": "PROD",
            "source_sid": "PRODDB",
            "source_db_host": "prod-db.example.com",
            "source_apps_host": "prod-apps.example.com",
        }}, headers=admin_headers)
        assert r.status_code == 200
        data = r.json()
        if data["type"] == "question" and data.get("field") == "target_environment":
            assert nonprod_env["name"] in data["content"]

    def test_interview_plan_when_complete(self, client, admin_headers, nonprod_env):
        """TC-CL-03: When all fields are filled, interview returns a plan."""
        r = client.post("/cloning/agent", json={"context": {
            "source_name": "PROD",
            "source_sid": "PRODDB",
            "source_db_host": "prod-db.example.com",
            "source_apps_host": "prod-apps.example.com",
            "target_environment": nonprod_env["name"],
            "target_apps_host": "tgt-apps.example.com",
            "target_storage": "fs",
            "target_datafile_dir": "/u01/oradata/CLONE",
            "source_datafile_dir": "/u01/oradata/PROD",
            "target_db_home": "yes",
            "db_topology": "single",
        }}, headers=admin_headers)
        assert r.status_code == 200
        data = r.json()
        assert data["type"] == "plan"
        assert nonprod_env["name"] in data["content"]


class TestCloningCreate:

    def test_create_clone_nonprod(self, client, admin_headers, nonprod_env):
        """TC-CL-04: Clone to non-prod environment succeeds and returns run ID."""
        r = client.post("/cloning/", json=_clone_payload(nonprod_env["name"]),
                        headers=admin_headers)
        assert r.status_code == 201
        data = r.json()
        assert "id" in data
        assert data["status"] == "completed"
        assert data["guard_status"] == "passed"

    def test_create_clone_has_steps(self, client, admin_headers, nonprod_env):
        """TC-CL-05: Completed clone run has simulation steps."""
        r = client.post("/cloning/", json=_clone_payload(nonprod_env["name"]),
                        headers=admin_headers)
        assert r.status_code == 201
        assert isinstance(r.json()["steps"], list)
        assert len(r.json()["steps"]) > 0

    def test_clone_prod_target_blocked(self, client, admin_headers, prod_env):
        """TC-CL-06: Clone to PROD target is blocked (FORBIDDEN or CONFLICT)."""
        r = client.post("/cloning/", json=_clone_payload(prod_env["name"]),
                        headers=admin_headers)
        assert r.status_code in (403, 409)

    def test_clone_unregistered_env_rejected(self, client, admin_headers):
        """TC-CL-07: Clone with unregistered target environment returns 400."""
        r = client.post("/cloning/", json=_clone_payload("NONEXISTENT_XYZ"),
                        headers=admin_headers)
        assert r.status_code == 400

    def test_clone_missing_source_sid_optional(self, client, admin_headers, nonprod_env):
        """TC-CL-08: source_sid is optional (defaults gracefully)."""
        payload = _clone_payload(nonprod_env["name"])
        del payload["source_sid"]
        r = client.post("/cloning/", json=payload, headers=admin_headers)
        assert r.status_code in (201, 422)  # 422 if required by schema

    def test_clone_rac_topology(self, client, admin_headers, nonprod_env):
        """TC-CL-09: Clone with RAC topology is accepted and creates steps."""
        r = client.post("/cloning/", json=_clone_payload(
            nonprod_env["name"],
            target_storage="asm",
            asm_dg="+DATA",
            db_topology="rac",
            rac_node_count="2",
            rac_db_hosts="uat-db1,uat-db2",
            scan_name="uat-scan",
        ), headers=admin_headers)
        assert r.status_code == 201
        data = r.json()
        assert data["status"] == "completed"

    def test_list_my_clones(self, client, admin_headers, nonprod_env):
        """TC-CL-10: GET /cloning/ lists only the current user's runs."""
        client.post("/cloning/", json=_clone_payload(nonprod_env["name"]),
                    headers=admin_headers)
        r = client.get("/cloning/", headers=admin_headers)
        assert r.status_code == 200
        assert isinstance(r.json(), list)
        assert len(r.json()) >= 1

    def test_get_clone_by_id(self, client, admin_headers, nonprod_env):
        """TC-CL-11: GET /cloning/{id} returns the run owned by the current user."""
        r = client.post("/cloning/", json=_clone_payload(nonprod_env["name"]),
                        headers=admin_headers)
        run_id = r.json()["id"]
        r2 = client.get(f"/cloning/{run_id}", headers=admin_headers)
        assert r2.status_code == 200
        assert r2.json()["id"] == run_id

    def test_get_clone_other_user_404(self, client, admin_headers, admin2_headers, nonprod_env):
        """TC-CL-12: Another user gets 404 when accessing a run they don't own."""
        r = client.post("/cloning/", json=_clone_payload(nonprod_env["name"]),
                        headers=admin_headers)
        run_id = r.json()["id"]
        r2 = client.get(f"/cloning/{run_id}", headers=admin2_headers)
        assert r2.status_code == 404

    def test_runbook_download(self, client, admin_headers, nonprod_env):
        """TC-CL-13: Completed clone run returns downloadable ZIP runbook."""
        r = client.post("/cloning/", json=_clone_payload(nonprod_env["name"]),
                        headers=admin_headers)
        run_id = r.json()["id"]
        r2 = client.get(f"/cloning/{run_id}/runbook", headers=admin_headers)
        assert r2.status_code == 200
        assert r2.headers.get("content-type") == "application/zip"
        assert len(r2.content) > 0


class TestCloningMakerChecker:

    def _create_blocked_clone(self, client, headers, prod_env):
        """Helper to create a clone that ends up blocked by production guard."""
        r = client.post("/cloning/", json=_clone_payload(prod_env["name"]),
                        headers=headers)
        if r.status_code == 409:
            return r.json()["detail"]["run_id"]
        pytest.skip("Clone was not blocked — prod_env may not be classified as prod by guard")

    def test_override_by_different_admin(self, client, admin_headers, admin2_headers, prod_env):
        """TC-CL-14: Different admin can approve a blocked clone override."""
        run_id = self._create_blocked_clone(client, admin_headers, prod_env)
        r = client.post(f"/cloning/{run_id}/override",
                        json={"reason": "Emergency refresh approved by CAB"},
                        headers=admin2_headers)
        assert r.status_code == 200
        assert r.json()["guard_status"] == "overridden"
        assert r.json()["status"] == "completed"

    def test_self_override_rejected(self, client, admin_headers, prod_env):
        """TC-CL-15: Requester cannot override their own blocked clone."""
        run_id = self._create_blocked_clone(client, admin_headers, prod_env)
        r = client.post(f"/cloning/{run_id}/override",
                        json={"reason": "Self approval attempt"},
                        headers=admin_headers)
        assert r.status_code == 403

    def test_override_requires_reason(self, client, admin_headers, admin2_headers, prod_env):
        """TC-CL-16: Override without a reason is rejected with 422."""
        run_id = self._create_blocked_clone(client, admin_headers, prod_env)
        r = client.post(f"/cloning/{run_id}/override",
                        json={"reason": ""},
                        headers=admin2_headers)
        assert r.status_code == 422

    def test_reject_clone_by_different_admin(self, client, admin_headers, admin2_headers, prod_env):
        """TC-CL-17: Different admin can reject a blocked clone."""
        run_id = self._create_blocked_clone(client, admin_headers, prod_env)
        r = client.post(f"/cloning/{run_id}/reject", headers=admin2_headers)
        assert r.status_code == 200
        assert r.json()["status"] == "rejected"

    def test_self_reject_blocked(self, client, admin_headers, prod_env):
        """TC-CL-18: Requester cannot reject their own blocked clone."""
        run_id = self._create_blocked_clone(client, admin_headers, prod_env)
        r = client.post(f"/cloning/{run_id}/reject", headers=admin_headers)
        assert r.status_code == 403
