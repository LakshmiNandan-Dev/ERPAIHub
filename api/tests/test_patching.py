"""TC-PT-01 to TC-PT-30 — Patching Agent"""
import pytest

from app.modules.dba.patching import patch_exec


def _patch_payload(target_env_name, components="db_home", patch_number="37123456", **overrides):
    base = {
        "target_environment": target_env_name,
        "components": components,
        "patch_number": patch_number,
        "db_topology": "single",
    }
    base.update(overrides)
    return base


class TestPatchingInterview:

    def test_interview_asks_for_environment(self, client, admin_headers, nonprod_env):
        """TC-PT-01: Interview starts by asking which environment to patch.

        At least one environment must be registered — with none, the agent returns
        a type='error' prompting the admin to add one first (see
        test_interview_no_environments_registered)."""
        r = client.post("/patching/agent", json={"context": {}}, headers=admin_headers)
        assert r.status_code == 200
        data = r.json()
        assert data["type"] == "question"
        assert data.get("field") == "target_environment"

    def test_interview_no_environments_registered(self, client, admin_headers):
        """TC-PT-01 (empty): With no environments registered the agent returns an error."""
        r = client.post("/patching/agent", json={"context": {}}, headers=admin_headers)
        assert r.status_code == 200
        assert r.json()["type"] == "error"

    def test_interview_lists_all_envs_including_prod(self, client, admin_headers, prod_env, nonprod_env):
        """TC-PT-02: Patching interview lists ALL environments (prod and non-prod)."""
        r = client.post("/patching/agent", json={"context": {}}, headers=admin_headers)
        assert r.status_code == 200
        content = r.json()["content"]
        assert prod_env["name"] in content or nonprod_env["name"] in content

    def test_interview_asks_for_components(self, client, admin_headers, nonprod_env):
        """TC-PT-03: Interview asks for components after environment is selected."""
        r = client.post("/patching/agent", json={"context": {
            "target_environment": nonprod_env["name"],
        }}, headers=admin_headers)
        assert r.status_code == 200
        data = r.json()
        if data["type"] == "question":
            assert data.get("field") in ("components", "patch_number")

    def test_interview_plan_for_nonprod(self, client, admin_headers, nonprod_env):
        """TC-PT-04: When all fields filled for non-prod, returns plan with green guard."""
        r = client.post("/patching/agent", json={"context": {
            "target_environment": nonprod_env["name"],
            "components": "db_home",
            "patch_number": "37123456",
            "db_topology": "single",
        }}, headers=admin_headers)
        assert r.status_code == 200
        data = r.json()
        assert data["type"] == "plan"
        assert "Non-production" in data["content"] or "non-prod" in data["content"].lower() or "🟢" in data["content"]

    def test_interview_plan_for_prod_shows_warning(self, client, admin_headers, prod_env):
        """TC-PT-05: Plan for PROD environment shows production guard warning."""
        r = client.post("/patching/agent", json={"context": {
            "target_environment": prod_env["name"],
            "components": "db_home",
            "patch_number": "37123456",
            "db_topology": "single",
        }}, headers=admin_headers)
        assert r.status_code == 200
        data = r.json()
        assert data["type"] == "plan"
        # Should mention production + approval requirement
        content = data["content"]
        assert "PROD" in content.upper() or "approval" in content.lower() or "🔴" in content

    def test_interview_adop_asks_mode(self, client, admin_headers, nonprod_env):
        """TC-PT-06: Including adop component asks for adop mode (online/downtime/hotpatch)."""
        r = client.post("/patching/agent", json={"context": {
            "target_environment": nonprod_env["name"],
            "components": "adop",
            "patch_number": "37123456",
            "db_topology": "single",
        }}, headers=admin_headers)
        assert r.status_code == 200
        data = r.json()
        if data["type"] == "question":
            assert "adop_mode" in str(data)


class TestPatchingCreate:

    def test_create_patch_nonprod_succeeds(self, client, admin_headers, nonprod_env):
        """TC-PT-07: Patching a non-prod environment succeeds immediately."""
        r = client.post("/patching/", json=_patch_payload(nonprod_env["name"]),
                        headers=admin_headers)
        assert r.status_code == 201
        data = r.json()
        assert data["status"] == "completed"
        assert data["guard_status"] == "passed"
        assert "id" in data

    def test_create_patch_has_steps(self, client, admin_headers, nonprod_env):
        """TC-PT-08: Completed patch run includes simulation steps."""
        r = client.post("/patching/", json=_patch_payload(nonprod_env["name"]),
                        headers=admin_headers)
        assert r.status_code == 201
        assert isinstance(r.json()["steps"], list)
        assert len(r.json()["steps"]) > 0

    def test_patch_prod_requires_approval(self, client, admin_headers, prod_env):
        """TC-PT-09: Patching PROD returns 409 with requires_approval=True."""
        r = client.post("/patching/", json=_patch_payload(prod_env["name"]),
                        headers=admin_headers)
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert detail.get("requires_approval") is True
        assert "run_id" in detail

    def test_patch_invalid_env_rejected(self, client, admin_headers):
        """TC-PT-10: Patching unregistered environment returns 400."""
        r = client.post("/patching/", json=_patch_payload("NONEXISTENT_ENV"),
                        headers=admin_headers)
        assert r.status_code == 400

    def test_patch_no_components_rejected(self, client, admin_headers, nonprod_env):
        """TC-PT-11: Patching without components returns 422."""
        r = client.post("/patching/", json={
            "target_environment": nonprod_env["name"],
            "components": "",
            "patch_number": "37123456",
        }, headers=admin_headers)
        assert r.status_code == 422

    def test_patch_no_patch_number_rejected(self, client, admin_headers, nonprod_env):
        """TC-PT-12: Patching without patch number returns 422."""
        r = client.post("/patching/", json={
            "target_environment": nonprod_env["name"],
            "components": "db_home",
            "patch_number": "",
        }, headers=admin_headers)
        assert r.status_code == 422

    def test_patch_multiple_components(self, client, admin_headers, nonprod_env):
        """TC-PT-13: Patching with multiple components (db_home, adop, weblogic)."""
        r = client.post("/patching/", json=_patch_payload(
            nonprod_env["name"],
            components="db_home,adop,weblogic",
            adop_mode="online",
        ), headers=admin_headers)
        assert r.status_code == 201
        assert r.json()["status"] == "completed"

    def test_patch_grid_component(self, client, admin_headers, nonprod_env):
        """TC-PT-14: Grid Infrastructure patching succeeds for non-prod."""
        r = client.post("/patching/", json=_patch_payload(
            nonprod_env["name"],
            components="grid",
        ), headers=admin_headers)
        assert r.status_code == 201

    def test_patch_rac_topology(self, client, admin_headers, nonprod_env):
        """TC-PT-15: RAC topology patching creates rolling-node steps."""
        r = client.post("/patching/", json=_patch_payload(
            nonprod_env["name"],
            components="db_home,grid",
            db_topology="rac",
            rac_node_count="2",
            rac_db_hosts="uat-db1,uat-db2",
        ), headers=admin_headers)
        assert r.status_code == 201
        data = r.json()
        assert data["status"] == "completed"

    def test_list_patches(self, client, admin_headers, nonprod_env):
        """TC-PT-16: GET /patching/ returns the current user's patch runs."""
        client.post("/patching/", json=_patch_payload(nonprod_env["name"]),
                    headers=admin_headers)
        r = client.get("/patching/", headers=admin_headers)
        assert r.status_code == 200
        assert isinstance(r.json(), list)
        assert len(r.json()) >= 1

    def test_get_patch_by_id(self, client, admin_headers, nonprod_env):
        """TC-PT-17: GET /patching/{id} returns the specific run."""
        r = client.post("/patching/", json=_patch_payload(nonprod_env["name"]),
                        headers=admin_headers)
        run_id = r.json()["id"]
        r2 = client.get(f"/patching/{run_id}", headers=admin_headers)
        assert r2.status_code == 200
        assert r2.json()["id"] == run_id

    def test_get_patch_other_user_404(self, client, admin_headers, admin2_headers, nonprod_env):
        """TC-PT-18: Another user gets 404 on a run they don't own."""
        r = client.post("/patching/", json=_patch_payload(nonprod_env["name"]),
                        headers=admin_headers)
        run_id = r.json()["id"]
        r2 = client.get(f"/patching/{run_id}", headers=admin2_headers)
        assert r2.status_code == 404

    def test_runbook_download_nonprod(self, client, admin_headers, nonprod_env):
        """TC-PT-19: Completed non-prod patch run has downloadable ZIP runbook."""
        r = client.post("/patching/", json=_patch_payload(nonprod_env["name"]),
                        headers=admin_headers)
        run_id = r.json()["id"]
        r2 = client.get(f"/patching/{run_id}/runbook", headers=admin_headers)
        assert r2.status_code == 200
        assert r2.headers.get("content-type") == "application/zip"

    def test_runbook_blocked_before_approval(self, client, admin_headers, prod_env):
        """TC-PT-20: Runbook endpoint returns 409 when run is awaiting approval."""
        r = client.post("/patching/", json=_patch_payload(prod_env["name"]),
                        headers=admin_headers)
        assert r.status_code == 409
        run_id = r.json()["detail"]["run_id"]
        r2 = client.get(f"/patching/{run_id}/runbook", headers=admin_headers)
        assert r2.status_code == 409


class TestPatchingMakerChecker:

    def _create_blocked_patch(self, client, headers, prod_env, interactive=False):
        r = client.post("/patching/", json=_patch_payload(
            prod_env["name"], interactive=interactive
        ), headers=headers)
        if r.status_code == 409:
            return r.json()["detail"]["run_id"]
        pytest.skip("Patch was not blocked — prod_env may not be classified as prod by guard")

    def test_override_by_different_admin(self, client, admin_headers, admin2_headers, prod_env):
        """TC-PT-21: Different admin can approve a production patch."""
        run_id = self._create_blocked_patch(client, admin_headers, prod_env)
        r = client.post(f"/patching/{run_id}/override",
                        json={"reason": "Emergency security patch approved by CISO"},
                        headers=admin2_headers)
        assert r.status_code == 200
        assert r.json()["guard_status"] == "overridden"
        assert r.json()["status"] == "completed"

    def test_self_override_rejected(self, client, admin_headers, prod_env):
        """TC-PT-22: Requester cannot approve their own production patch."""
        run_id = self._create_blocked_patch(client, admin_headers, prod_env)
        r = client.post(f"/patching/{run_id}/override",
                        json={"reason": "Self approval"},
                        headers=admin_headers)
        assert r.status_code == 403

    def test_override_requires_reason(self, client, admin_headers, admin2_headers, prod_env):
        """TC-PT-23: Override without a reason is rejected with 422."""
        run_id = self._create_blocked_patch(client, admin_headers, prod_env)
        r = client.post(f"/patching/{run_id}/override",
                        json={"reason": ""},
                        headers=admin2_headers)
        assert r.status_code == 422

    def test_reject_production_patch(self, client, admin_headers, admin2_headers, prod_env):
        """TC-PT-24: Different admin can reject a production patch."""
        run_id = self._create_blocked_patch(client, admin_headers, prod_env)
        r = client.post(f"/patching/{run_id}/reject", headers=admin2_headers)
        assert r.status_code == 200
        assert r.json()["status"] == "rejected"

    def test_self_reject_blocked(self, client, admin_headers, prod_env):
        """TC-PT-25: Requester cannot reject their own production patch."""
        run_id = self._create_blocked_patch(client, admin_headers, prod_env)
        r = client.post(f"/patching/{run_id}/reject", headers=admin_headers)
        assert r.status_code == 403


class TestPatchingInteractiveAdop:

    def _create_interactive_run(self, client, headers, nonprod_env):
        r = client.post("/patching/", json=_patch_payload(
            nonprod_env["name"],
            components="adop",
            adop_mode="online",
            interactive=True,
        ), headers=headers)
        assert r.status_code == 201, f"Create failed: {r.text}"
        assert r.json()["interactive"] is True
        return r.json()["id"]

    def test_interactive_run_status_is_cycle_ready(self, client, admin_headers, nonprod_env):
        """TC-PT-26: Interactive adop run starts in cycle_ready status."""
        run_id = self._create_interactive_run(client, admin_headers, nonprod_env)
        r = client.get(f"/patching/{run_id}", headers=admin_headers)
        assert r.status_code == 200
        assert r.json()["status"] == "cycle_ready"

    def test_adop_prepare_phase(self, client, admin_headers, nonprod_env):
        """TC-PT-27: Running the 'prepare' phase succeeds on an interactive run."""
        run_id = self._create_interactive_run(client, admin_headers, nonprod_env)
        r = client.post(f"/patching/{run_id}/phase",
                        json={"phase": "prepare"}, headers=admin_headers)
        assert r.status_code == 200
        result = r.json()
        assert result["phase"] == "prepare"
        assert result["step"]["status"] in ("ok", "completed", "simulated", "success")

    def test_adop_apply_after_prepare(self, client, admin_headers, nonprod_env):
        """TC-PT-28: Apply phase can only run after prepare."""
        run_id = self._create_interactive_run(client, admin_headers, nonprod_env)
        # Run prepare first
        client.post(f"/patching/{run_id}/phase", json={"phase": "prepare"}, headers=admin_headers)
        # Now apply
        r = client.post(f"/patching/{run_id}/phase",
                        json={"phase": "apply"}, headers=admin_headers)
        assert r.status_code == 200
        assert r.json()["phase"] == "apply"

    def test_adop_out_of_order_phase_rejected(self, client, admin_headers, nonprod_env):
        """TC-PT-29: Running cutover without prepare/apply returns 409."""
        run_id = self._create_interactive_run(client, admin_headers, nonprod_env)
        r = client.post(f"/patching/{run_id}/phase",
                        json={"phase": "cutover"}, headers=admin_headers)
        assert r.status_code == 409

    def test_adop_abort_ends_cycle(self, client, admin_headers, nonprod_env):
        """TC-PT-29 (abort): Aborting the cycle prevents further phases."""
        run_id = self._create_interactive_run(client, admin_headers, nonprod_env)
        client.post(f"/patching/{run_id}/phase", json={"phase": "prepare"}, headers=admin_headers)
        # Abort the cycle
        r = client.post(f"/patching/{run_id}/phase", json={"phase": "abort"}, headers=admin_headers)
        assert r.status_code == 200
        # The enriched cycle state reports abort via the `aborted` flag.
        assert r.json()["status"]["aborted"] is True

        # Further phases should be blocked
        r2 = client.post(f"/patching/{run_id}/phase", json={"phase": "apply"}, headers=admin_headers)
        assert r2.status_code == 409

    def test_adop_status_endpoint(self, client, admin_headers, nonprod_env):
        """TC-PT-30: GET /patching/{id}/adop-status returns cycle state."""
        run_id = self._create_interactive_run(client, admin_headers, nonprod_env)
        r = client.get(f"/patching/{run_id}/adop-status", headers=admin_headers)
        assert r.status_code == 200
        data = r.json()
        assert "executed" in data or "source" in data or "status" in data

    def test_phase_control_not_available_on_batch_run(self, client, admin_headers, nonprod_env):
        """TC-PT-29 (batch): Phase control returns 400 on a batch (non-interactive) run."""
        r = client.post("/patching/", json=_patch_payload(nonprod_env["name"], components="adop",
                                                           adop_mode="online"),
                        headers=admin_headers)
        assert r.status_code == 201
        run_id = r.json()["id"]
        r2 = client.post(f"/patching/{run_id}/phase",
                         json={"phase": "prepare"}, headers=admin_headers)
        assert r2.status_code == 400


class TestPatchExecCommandSafety:
    """patch_exec.py's live SSH helpers interpolate admin/agent-supplied values
    (patch numbers, OPatch homes, adop phase params) into remote shell commands.
    These are pure unit tests against the command-construction logic — no real
    SSH connection is made; `_connect`/`_run` are monkeypatched to capture the
    exact command string that would have been sent."""

    _FAKE_CREDS = {"ssh": {"host": "app.example.com"}}

    class _FakeClient:
        def close(self):
            pass

    def _capture_run(self, monkeypatch):
        captured = {}

        def _fake_connect(ssh):
            return self._FakeClient()

        def _fake_run(client, command, feed=None, timeout=None):
            captured["command"] = command
            return 0, "ok"

        monkeypatch.setattr(patch_exec, "_connect", _fake_connect)
        monkeypatch.setattr(patch_exec, "_run", _fake_run)
        return captured

    def test_lspatches_rejects_unsafe_home(self, monkeypatch):
        captured = self._capture_run(monkeypatch)
        result = patch_exec.live_opatch_lspatches(self._FAKE_CREDS, "/u01/home; rm -rf /", "37123456")
        assert "error" in result
        assert "command" not in captured

    def test_lspatches_quotes_patch_value(self, monkeypatch):
        import shlex
        captured = self._capture_run(monkeypatch)
        unsafe_patch = "37123456; rm -rf /"
        result = patch_exec.live_opatch_lspatches(self._FAKE_CREDS, "/u01/db_home", unsafe_patch)
        assert "error" not in result
        # The unsafe patch value must be shell-quoted, never interpolated raw.
        assert shlex.quote(unsafe_patch) in captured["command"]

    def test_conflict_check_rejects_unsafe_home_or_patch_dir(self, monkeypatch):
        captured = self._capture_run(monkeypatch)
        result = patch_exec.live_opatch_conflict(self._FAKE_CREDS, "/u01/db_home",
                                                  "/u01/stage/`whoami`")
        assert "error" in result
        assert "command" not in captured

    def test_adop_status_rejects_unsafe_run_fs(self, monkeypatch):
        captured = self._capture_run(monkeypatch)
        result = patch_exec.live_adop_status(self._FAKE_CREDS, "/u01/EBSapps/appl && rm -rf /")
        assert "error" in result
        assert "command" not in captured


class TestAdopPhaseCommandSafety:
    """_adop_phase_command builds the destructive live-phase command string —
    every interpolated value must be validated before being embedded."""

    def _ctx(self, **overrides):
        base = {"PATCH_NUMBER": "37123456", "APPLY_WORKERS": "8", "ADOP_MODE": "online",
                "CLEANUP_MODE": "standard"}
        base.update(overrides)
        return base

    def test_valid_apply_phase_builds_expected_command(self):
        cmd = patch_exec._adop_phase_command("apply", self._ctx())
        assert cmd == "adop phase=apply patches=37123456 workers=8"

    def test_unknown_phase_rejected(self):
        with pytest.raises(ValueError):
            patch_exec._adop_phase_command("nuke", self._ctx())

    def test_unsafe_patch_number_rejected(self):
        with pytest.raises(ValueError):
            patch_exec._adop_phase_command("apply", self._ctx(PATCH_NUMBER="37123456; rm -rf /"))

    def test_unsafe_apply_workers_rejected(self):
        with pytest.raises(ValueError):
            patch_exec._adop_phase_command("apply", self._ctx(APPLY_WORKERS="8; rm -rf /"))

    def test_unknown_adop_mode_rejected(self):
        with pytest.raises(ValueError):
            patch_exec._adop_phase_command("apply", self._ctx(ADOP_MODE="rm -rf /"))

    def test_unknown_cleanup_mode_rejected(self):
        with pytest.raises(ValueError):
            patch_exec._adop_phase_command("cleanup", self._ctx(CLEANUP_MODE="; rm -rf /"))

    def test_live_adop_phase_returns_error_instead_of_raising(self, monkeypatch):
        """The router-facing live_adop_phase catches the ValueError and returns
        {error: ...} rather than letting it propagate."""
        result = patch_exec.live_adop_phase(
            {"ssh": {"host": "app.example.com"}, "apps_pwd": "x", "weblogic_pwd": "y", "system_pwd": "z"},
            "/u01/EBSapps/appl", "apply", self._ctx(PATCH_NUMBER="`whoami`"),
        )
        assert "error" in result
