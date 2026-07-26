"""Patch gap analysis — admin-defined target patch list vs. a live/stored
"applied patches" inventory (patch_exec.live_opatch_list_all, mocked here to
avoid a real SSH connection)."""
import pytest

from app.modules.dba.patching import patch_exec, patch_gap_service, patch_file_gap_service


class TestPatchTargetAdminCrud:

    def test_create_patch_target(self, client, admin_headers, nonprod_env):
        r = client.post("/admin/patch-targets", json={
            "environment_id": nonprod_env["id"], "component": "db_home",
            "home_path": "/u01/oracle/db_home", "target_patches": [
                {"patch_number": "37123456", "label": "Oct-2025 CPU"},
            ],
        }, headers=admin_headers)
        assert r.status_code == 201
        data = r.json()
        assert data["component"] == "db_home"
        assert data["target_patches"][0]["patch_number"] == "37123456"

    def test_create_duplicate_component_rejected(self, client, admin_headers, nonprod_env):
        payload = {"environment_id": nonprod_env["id"], "component": "db_home", "target_patches": []}
        r1 = client.post("/admin/patch-targets", json=payload, headers=admin_headers)
        assert r1.status_code == 201
        r2 = client.post("/admin/patch-targets", json=payload, headers=admin_headers)
        assert r2.status_code == 400

    def test_create_requires_admin(self, client, regular_user_headers, nonprod_env):
        r = client.post("/admin/patch-targets", json={
            "environment_id": nonprod_env["id"], "component": "db_home", "target_patches": [],
        }, headers=regular_user_headers)
        assert r.status_code == 403

    def test_update_patch_target(self, client, admin_headers, nonprod_env):
        created = client.post("/admin/patch-targets", json={
            "environment_id": nonprod_env["id"], "component": "grid", "target_patches": [],
        }, headers=admin_headers).json()
        r = client.patch(f"/admin/patch-targets/{created['id']}", json={
            "target_patches": [{"patch_number": "11112222", "label": "GI RU"}],
        }, headers=admin_headers)
        assert r.status_code == 200
        assert r.json()["target_patches"][0]["patch_number"] == "11112222"

    def test_delete_patch_target(self, client, admin_headers, nonprod_env):
        created = client.post("/admin/patch-targets", json={
            "environment_id": nonprod_env["id"], "component": "weblogic", "target_patches": [],
        }, headers=admin_headers).json()
        r = client.delete(f"/admin/patch-targets/{created['id']}", headers=admin_headers)
        assert r.status_code == 204
        r2 = client.get(f"/admin/patch-targets?environment_id={nonprod_env['id']}", headers=admin_headers)
        assert all(t["id"] != created["id"] for t in r2.json())


class TestGapScan:

    def _make_target(self, client, admin_headers, nonprod_env, patches):
        return client.post("/admin/patch-targets", json={
            "environment_id": nonprod_env["id"], "component": "db_home",
            "home_path": "/u01/oracle/db_home",
            "target_patches": [{"patch_number": p, "label": p} for p in patches],
        }, headers=admin_headers).json()

    def test_scan_without_targets_rejected(self, client, admin_headers, nonprod_env):
        r = client.post(f"/patching/gap/scan/{nonprod_env['id']}", headers=admin_headers)
        assert r.status_code == 400

    def test_scan_without_ssh_configured_marks_error_not_crash(self, client, admin_headers, nonprod_env):
        """No SSH server is linked to nonprod_env, so live_opatch_list_all
        returns {'error': ...} — the scan must record that gracefully, not
        raise, and every target patch shows as missing (unknown != applied)."""
        self._make_target(client, admin_headers, nonprod_env, ["37123456"])
        r = client.post(f"/patching/gap/scan/{nonprod_env['id']}", headers=admin_headers)
        assert r.status_code == 202

        gaps = client.get(f"/patching/gap/{nonprod_env['id']}", headers=admin_headers).json()
        assert len(gaps) == 1
        assert gaps[0]["scan_status"] == "error"
        assert gaps[0]["missing"] == ["37123456"]

    def test_scan_with_mocked_applied_patches_computes_gap(self, client, admin_headers, nonprod_env, monkeypatch):
        self._make_target(client, admin_headers, nonprod_env, ["37123456", "38999999"])

        def _fake_list_all(creds, home):
            return {"rc": 0, "patches": ["37123456"], "output": "37123456;description\n"}

        monkeypatch.setattr(patch_gap_service.patch_exec, "live_opatch_list_all", _fake_list_all)

        r = client.post(f"/patching/gap/scan/{nonprod_env['id']}", headers=admin_headers)
        assert r.status_code == 202

        gaps = client.get(f"/patching/gap/{nonprod_env['id']}", headers=admin_headers).json()
        assert gaps[0]["scan_status"] == "ok"
        assert gaps[0]["applied_patches"] == ["37123456"]
        assert gaps[0]["missing"] == ["38999999"]

        findings = client.get(
            f"/monitoring/findings?environment_id={nonprod_env['id']}&source=patch_gap", headers=admin_headers
        ).json()
        assert len(findings) == 1
        assert "38999999" in findings[0]["title"]

    def test_gap_closes_when_patch_becomes_applied(self, client, admin_headers, nonprod_env, monkeypatch):
        self._make_target(client, admin_headers, nonprod_env, ["37123456"])

        monkeypatch.setattr(patch_gap_service.patch_exec, "live_opatch_list_all",
                            lambda creds, home: {"rc": 0, "patches": [], "output": ""})
        client.post(f"/patching/gap/scan/{nonprod_env['id']}", headers=admin_headers)

        open_findings = client.get(
            f"/monitoring/findings?environment_id={nonprod_env['id']}&source=patch_gap&status_filter=open",
            headers=admin_headers,
        ).json()
        assert len(open_findings) == 1

        monkeypatch.setattr(patch_gap_service.patch_exec, "live_opatch_list_all",
                            lambda creds, home: {"rc": 0, "patches": ["37123456"], "output": "37123456;x\n"})
        client.post(f"/patching/gap/scan/{nonprod_env['id']}", headers=admin_headers)

        open_findings2 = client.get(
            f"/monitoring/findings?environment_id={nonprod_env['id']}&source=patch_gap&status_filter=open",
            headers=admin_headers,
        ).json()
        assert open_findings2 == []


class TestLiveOpatchListAll:
    """Unit tests for the hardened live_opatch_list_all (Feature D's first
    real caller of the command-hardening pattern from Feature C)."""

    def test_rejects_unsafe_home_path(self):
        result = patch_exec.live_opatch_list_all({"ssh": {"host": "x"}}, "/u01/home; rm -rf /")
        assert "error" in result

    def test_rejects_missing_ssh(self):
        result = patch_exec.live_opatch_list_all({"ssh": None}, "/u01/db_home")
        assert "error" in result

    def test_parses_patch_numbers_from_output(self, monkeypatch):
        class _FakeClient:
            def close(self): pass

        monkeypatch.setattr(patch_exec, "_connect", lambda ssh: _FakeClient())
        monkeypatch.setattr(patch_exec, "_run", lambda client, cmd, feed=None, timeout=None: (
            0, "37123456;Oct 2025 CPU\n38999999;Jan 2026 CPU\n"
        ))
        result = patch_exec.live_opatch_list_all({"ssh": {"host": "x"}}, "/u01/db_home")
        assert result["patches"] == ["37123456", "38999999"]


class TestAdopComponent:
    """'adop' is a PatchTarget component checked live against ad_bugs (SQL),
    not opatch lspatches (SSH) — application-tier patches aren't visible to
    any Oracle Home's OPatch inventory."""

    def _make_adop_target(self, client, admin_headers, nonprod_env, patches):
        return client.post("/admin/patch-targets", json={
            "environment_id": nonprod_env["id"], "component": "adop",
            "target_patches": [{"patch_number": p, "label": p} for p in patches],
        }, headers=admin_headers).json()

    def test_adop_scan_uses_ad_bugs_not_ssh(self, client, admin_headers, nonprod_env, monkeypatch):
        self._make_adop_target(client, admin_headers, nonprod_env, ["36420641", "38111111"])

        called = {}
        def _fake_ad_bugs(env_row, patch_numbers):
            called["patch_numbers"] = patch_numbers
            return {"patches": ["36420641"]}
        monkeypatch.setattr(patch_gap_service, "_live_ad_bugs_check", _fake_ad_bugs)

        def _fail_ssh(*a, **kw):
            raise AssertionError("adop component must not call the SSH opatch path")
        monkeypatch.setattr(patch_gap_service.patch_exec, "live_opatch_list_all", _fail_ssh)

        r = client.post(f"/patching/gap/scan/{nonprod_env['id']}", headers=admin_headers)
        assert r.status_code == 202

        gaps = client.get(f"/patching/gap/{nonprod_env['id']}", headers=admin_headers).json()
        assert gaps[0]["scan_status"] == "ok"
        assert gaps[0]["applied_patches"] == ["36420641"]
        assert gaps[0]["missing"] == ["38111111"]
        assert set(called["patch_numbers"]) == {"36420641", "38111111"}

    def test_ad_bugs_error_marks_scan_error(self, client, admin_headers, nonprod_env, monkeypatch):
        """No DB connection configured on nonprod_env's ad-hoc setup ->
        _live_ad_bugs_check itself reports an error; the scan must record
        that gracefully, matching the SSH-path error-handling behavior."""
        self._make_adop_target(client, admin_headers, nonprod_env, ["36420641"])
        r = client.post(f"/patching/gap/scan/{nonprod_env['id']}", headers=admin_headers)
        assert r.status_code == 202

        gaps = client.get(f"/patching/gap/{nonprod_env['id']}", headers=admin_headers).json()
        assert gaps[0]["scan_status"] == "error"
        assert gaps[0]["missing"] == ["36420641"]


class TestPatchFileScanConfigAdminCrud:

    def test_create_file_scan_config(self, client, admin_headers, nonprod_env):
        r = client.post("/admin/patch-file-scan-configs", json={
            "environment_id": nonprod_env["id"], "label": "Run edition APPL_TOP",
            "run_fs_path": "/u01/install/APPS/apps/apps_st/appl",
        }, headers=admin_headers)
        assert r.status_code == 201
        data = r.json()
        assert data["run_fs_path"] == "/u01/install/APPS/apps/apps_st/appl"
        assert "*.pls" in data["file_globs"]

    def test_create_duplicate_path_rejected(self, client, admin_headers, nonprod_env):
        payload = {"environment_id": nonprod_env["id"], "run_fs_path": "/u01/appl"}
        r1 = client.post("/admin/patch-file-scan-configs", json=payload, headers=admin_headers)
        assert r1.status_code == 201
        r2 = client.post("/admin/patch-file-scan-configs", json=payload, headers=admin_headers)
        assert r2.status_code == 400

    def test_create_requires_admin(self, client, regular_user_headers, nonprod_env):
        r = client.post("/admin/patch-file-scan-configs", json={
            "environment_id": nonprod_env["id"], "run_fs_path": "/u01/appl",
        }, headers=regular_user_headers)
        assert r.status_code == 403

    def test_update_file_scan_config(self, client, admin_headers, nonprod_env):
        created = client.post("/admin/patch-file-scan-configs", json={
            "environment_id": nonprod_env["id"], "run_fs_path": "/u01/appl",
        }, headers=admin_headers).json()
        r = client.patch(f"/admin/patch-file-scan-configs/{created['id']}", json={
            "file_globs": ["*.pls"],
        }, headers=admin_headers)
        assert r.status_code == 200
        assert r.json()["file_globs"] == ["*.pls"]

    def test_delete_file_scan_config(self, client, admin_headers, nonprod_env):
        created = client.post("/admin/patch-file-scan-configs", json={
            "environment_id": nonprod_env["id"], "run_fs_path": "/u01/appl",
        }, headers=admin_headers).json()
        r = client.delete(f"/admin/patch-file-scan-configs/{created['id']}", headers=admin_headers)
        assert r.status_code == 204
        r2 = client.get(f"/admin/patch-file-scan-configs?environment_id={nonprod_env['id']}", headers=admin_headers)
        assert all(c["id"] != created["id"] for c in r2.json())


class TestFileGapScan:
    """Whole-run-filesystem file-version drift scan — product is derived
    per-file from its path (EBS's $APPL_TOP/<product>/... convention), not
    pre-configured, so a single config covers every product in one pass."""

    def _make_config(self, client, admin_headers, nonprod_env, run_fs_path="/u01/appl"):
        return client.post("/admin/patch-file-scan-configs", json={
            "environment_id": nonprod_env["id"], "run_fs_path": run_fs_path,
        }, headers=admin_headers).json()

    def test_scan_requires_admin(self, client, regular_user_headers):
        r = client.post("/patching/gap/files/scan/999999", headers=regular_user_headers)
        assert r.status_code == 403

    def test_scan_unknown_config_404(self, client, admin_headers):
        r = client.post("/patching/gap/files/scan/999999", headers=admin_headers)
        assert r.status_code == 404

    def test_mismatch_creates_finding_grouped_by_product(self, client, admin_headers, nonprod_env, monkeypatch):
        config = self._make_config(client, admin_headers, nonprod_env)

        monkeypatch.setattr(patch_file_gap_service.patch_exec, "live_file_header_scan",
            lambda creds, run_fs_path, globs: {"files": [
                {"path": f"{run_fs_path}/ap/12.0.0/patch/115/sql/APXVCHKB.pls",
                 "filename": "APXVCHKB.pls", "fs_version": "120.3"},
            ], "truncated": False})
        monkeypatch.setattr(patch_file_gap_service, "_live_ad_file_versions",
            lambda env_row, filenames: {"versions": {"APXVCHKB.pls": "120.5"}})

        r = client.post(f"/patching/gap/files/scan/{config['id']}", headers=admin_headers)
        assert r.status_code == 202

        findings = client.get(
            f"/monitoring/findings?environment_id={nonprod_env['id']}&source=patch_file_gap",
            headers=admin_headers,
        ).json()
        assert len(findings) == 1
        assert findings[0]["area"] == "ap"
        assert "120.3" in findings[0]["title"] and "120.5" in findings[0]["title"]

    def test_file_missing_from_ad_files_creates_info_finding(self, client, admin_headers, nonprod_env, monkeypatch):
        config = self._make_config(client, admin_headers, nonprod_env)

        monkeypatch.setattr(patch_file_gap_service.patch_exec, "live_file_header_scan",
            lambda creds, run_fs_path, globs: {"files": [
                {"path": f"{run_fs_path}/gl/12.0.0/patch/115/sql/CUSTOM.pls",
                 "filename": "CUSTOM.pls", "fs_version": "1.0"},
            ], "truncated": False})
        monkeypatch.setattr(patch_file_gap_service, "_live_ad_file_versions",
            lambda env_row, filenames: {"versions": {}})

        r = client.post(f"/patching/gap/files/scan/{config['id']}", headers=admin_headers)
        assert r.status_code == 202

        findings = client.get(
            f"/monitoring/findings?environment_id={nonprod_env['id']}&source=patch_file_gap",
            headers=admin_headers,
        ).json()
        assert len(findings) == 1
        assert findings[0]["area"] == "gl"
        assert "not registered" in findings[0]["title"]

    def test_finding_resolves_once_versions_match(self, client, admin_headers, nonprod_env, monkeypatch):
        config = self._make_config(client, admin_headers, nonprod_env)

        monkeypatch.setattr(patch_file_gap_service.patch_exec, "live_file_header_scan",
            lambda creds, run_fs_path, globs: {"files": [
                {"path": f"{run_fs_path}/ap/12.0.0/patch/115/sql/APXVCHKB.pls",
                 "filename": "APXVCHKB.pls", "fs_version": "120.3"},
            ], "truncated": False})
        monkeypatch.setattr(patch_file_gap_service, "_live_ad_file_versions",
            lambda env_row, filenames: {"versions": {"APXVCHKB.pls": "120.5"}})
        client.post(f"/patching/gap/files/scan/{config['id']}", headers=admin_headers)

        open1 = client.get(
            f"/monitoring/findings?environment_id={nonprod_env['id']}&source=patch_file_gap&status_filter=open",
            headers=admin_headers,
        ).json()
        assert len(open1) == 1

        monkeypatch.setattr(patch_file_gap_service, "_live_ad_file_versions",
            lambda env_row, filenames: {"versions": {"APXVCHKB.pls": "120.3"}})
        client.post(f"/patching/gap/files/scan/{config['id']}", headers=admin_headers)

        open2 = client.get(
            f"/monitoring/findings?environment_id={nonprod_env['id']}&source=patch_file_gap&status_filter=open",
            headers=admin_headers,
        ).json()
        assert open2 == []

    def test_ssh_error_marks_run_error_never_raises(self, client, admin_headers, nonprod_env, monkeypatch):
        config = self._make_config(client, admin_headers, nonprod_env)
        monkeypatch.setattr(patch_file_gap_service.patch_exec, "live_file_header_scan",
            lambda creds, run_fs_path, globs: {"error": "no SSH server configured"})
        r = client.post(f"/patching/gap/files/scan/{config['id']}", headers=admin_headers)
        assert r.status_code == 202  # never raises — background task swallows it

    def test_matched_file_creates_inventory_row_but_no_finding(self, client, admin_headers, nonprod_env, monkeypatch):
        """A file whose fs_version matches AD_FILES must not become a
        Finding (only drift is a finding), but must still appear in the
        full inventory with match_status='match'."""
        config = self._make_config(client, admin_headers, nonprod_env)
        monkeypatch.setattr(patch_file_gap_service.patch_exec, "live_file_header_scan",
            lambda creds, run_fs_path, globs: {"files": [
                {"path": f"{run_fs_path}/ap/12.0.0/patch/115/sql/APXVCHKB.pls",
                 "filename": "APXVCHKB.pls", "fs_version": "120.3"},
            ], "truncated": False})
        monkeypatch.setattr(patch_file_gap_service, "_live_ad_file_versions",
            lambda env_row, filenames: {"versions": {"APXVCHKB.pls": "120.3"}})

        r = client.post(f"/patching/gap/files/scan/{config['id']}", headers=admin_headers)
        assert r.status_code == 202

        findings = client.get(
            f"/monitoring/findings?environment_id={nonprod_env['id']}&source=patch_file_gap",
            headers=admin_headers,
        ).json()
        assert findings == []

        inv = client.get(f"/patching/gap/files/inventory/{nonprod_env['id']}", headers=admin_headers).json()
        assert inv["total"] == 1
        row = inv["items"][0]
        assert row["filename"] == "APXVCHKB.pls"
        assert row["product"] == "ap"
        assert row["fs_version"] == "120.3"
        assert row["db_version"] == "120.3"
        assert row["match_status"] == "match"

    def test_second_scan_updates_inventory_row_in_place(self, client, admin_headers, nonprod_env, monkeypatch):
        config = self._make_config(client, admin_headers, nonprod_env)

        def _scan(fs_version):
            return lambda creds, run_fs_path, globs: {"files": [
                {"path": f"{run_fs_path}/ap/12.0.0/patch/115/sql/APXVCHKB.pls",
                 "filename": "APXVCHKB.pls", "fs_version": fs_version},
            ], "truncated": False}

        monkeypatch.setattr(patch_file_gap_service.patch_exec, "live_file_header_scan", _scan("120.3"))
        monkeypatch.setattr(patch_file_gap_service, "_live_ad_file_versions",
            lambda env_row, filenames: {"versions": {"APXVCHKB.pls": "120.5"}})
        client.post(f"/patching/gap/files/scan/{config['id']}", headers=admin_headers)

        inv1 = client.get(f"/patching/gap/files/inventory/{nonprod_env['id']}", headers=admin_headers).json()
        assert inv1["total"] == 1
        assert inv1["items"][0]["match_status"] == "mismatch"

        monkeypatch.setattr(patch_file_gap_service.patch_exec, "live_file_header_scan", _scan("120.5"))
        client.post(f"/patching/gap/files/scan/{config['id']}", headers=admin_headers)

        inv2 = client.get(f"/patching/gap/files/inventory/{nonprod_env['id']}", headers=admin_headers).json()
        assert inv2["total"] == 1   # updated in place, not duplicated
        assert inv2["items"][0]["match_status"] == "match"
        assert inv2["items"][0]["fs_version"] == "120.5"

    def test_inventory_filters_and_paginates(self, client, admin_headers, nonprod_env, monkeypatch):
        config = self._make_config(client, admin_headers, nonprod_env)
        monkeypatch.setattr(patch_file_gap_service.patch_exec, "live_file_header_scan",
            lambda creds, run_fs_path, globs: {"files": [
                {"path": f"{run_fs_path}/ap/12.0.0/patch/115/sql/APXVCHKB.pls",
                 "filename": "APXVCHKB.pls", "fs_version": "120.3"},
                {"path": f"{run_fs_path}/gl/12.0.0/patch/115/sql/GLXVCHKB.pls",
                 "filename": "GLXVCHKB.pls", "fs_version": "1.0"},
            ], "truncated": False})
        monkeypatch.setattr(patch_file_gap_service, "_live_ad_file_versions",
            lambda env_row, filenames: {"versions": {"APXVCHKB.pls": "120.3"}})   # GLXVCHKB.pls -> fs_only
        client.post(f"/patching/gap/files/scan/{config['id']}", headers=admin_headers)

        by_product = client.get(
            f"/patching/gap/files/inventory/{nonprod_env['id']}?product=ap", headers=admin_headers
        ).json()
        assert by_product["total"] == 1
        assert by_product["items"][0]["filename"] == "APXVCHKB.pls"

        by_status = client.get(
            f"/patching/gap/files/inventory/{nonprod_env['id']}?match_status=fs_only", headers=admin_headers
        ).json()
        assert by_status["total"] == 1
        assert by_status["items"][0]["filename"] == "GLXVCHKB.pls"

        by_name = client.get(
            f"/patching/gap/files/inventory/{nonprod_env['id']}?filename=GLXV", headers=admin_headers
        ).json()
        assert by_name["total"] == 1

        paged = client.get(
            f"/patching/gap/files/inventory/{nonprod_env['id']}?limit=1&offset=0", headers=admin_headers
        ).json()
        assert paged["total"] == 2
        assert len(paged["items"]) == 1

    def test_inventory_requires_auth(self, client, nonprod_env):
        r = client.get(f"/patching/gap/files/inventory/{nonprod_env['id']}")
        assert r.status_code == 401

    def test_inventory_unknown_environment_404(self, client, admin_headers):
        r = client.get("/patching/gap/files/inventory/999999", headers=admin_headers)
        assert r.status_code == 404
