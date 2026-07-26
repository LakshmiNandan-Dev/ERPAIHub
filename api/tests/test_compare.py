"""Environment-to-environment comparison — patch parity, live performance
diagnostics, and configuration drift between two DIFFERENT EBS environments."""
import uuid
import pytest

from app import models
from app.modules.dba.compare import compare_service


def _make_env(client, admin_headers, **overrides):
    body = {
        "name": f"ENV_{uuid.uuid4().hex[:6].upper()}", "tier": "nonprod",
        "db_host": "db.example.com", "db_sid": "TESTDB", "db_user": "apps",
    }
    body.update(overrides)
    r = client.post("/admin/environments", json=body, headers=admin_headers)
    assert r.status_code == 201, r.text
    return r.json()


class TestComputePatchDiff:

    def test_diff_reports_only_in_a_b_and_common(self, client, admin_headers, db_session):
        env_a = _make_env(client, admin_headers)
        env_b = _make_env(client, admin_headers)
        db_session.add(models.AppliedPatchSnapshot(
            environment_id=env_a["id"], component="db_home",
            applied_patches=["37123456", "38999999"], scan_status="ok",
        ))
        db_session.add(models.AppliedPatchSnapshot(
            environment_id=env_b["id"], component="db_home",
            applied_patches=["38999999", "40000000"], scan_status="ok",
        ))
        db_session.commit()

        diff = compare_service.compute_patch_diff(db_session, env_a["id"], env_b["id"])
        assert len(diff) == 1
        row = diff[0]
        assert row["component"] == "db_home"
        assert row["only_in_a"] == ["37123456"]
        assert row["only_in_b"] == ["40000000"]
        assert row["common_count"] == 1
        assert row["a_status"] == "ok"
        assert row["b_status"] == "ok"

    def test_never_scanned_side_reported_distinctly(self, client, admin_headers, db_session):
        env_a = _make_env(client, admin_headers)
        env_b = _make_env(client, admin_headers)
        db_session.add(models.AppliedPatchSnapshot(
            environment_id=env_a["id"], component="grid",
            applied_patches=["11112222"], scan_status="ok",
        ))
        db_session.commit()

        diff = compare_service.compute_patch_diff(db_session, env_a["id"], env_b["id"])
        assert len(diff) == 1
        assert diff[0]["a_status"] == "ok"
        assert diff[0]["b_status"] == "never_scanned"
        assert diff[0]["only_in_a"] == ["11112222"]
        assert diff[0]["only_in_b"] == []

    def test_no_snapshots_either_side_returns_empty(self, client, admin_headers, db_session):
        env_a = _make_env(client, admin_headers)
        env_b = _make_env(client, admin_headers)
        assert compare_service.compute_patch_diff(db_session, env_a["id"], env_b["id"]) == []


class TestDiffRowsPure:

    def test_disjoint_and_overlapping_names(self):
        rows_a = [{"name": "X", "value": "1"}, {"name": "Y", "value": "2"}]
        rows_b = [{"name": "Y", "value": "2"}, {"name": "Z", "value": "3"}]
        d = compare_service._diff_rows(rows_a, rows_b, "responsibilities")
        assert d["only_in_a"] == ["X"]
        assert d["only_in_b"] == ["Z"]
        assert d["different"] == []

    def test_same_name_different_value(self):
        rows_a = [{"name": "cursor_sharing", "value": "EXACT", "isdefault": "FALSE"}]
        rows_b = [{"name": "cursor_sharing", "value": "FORCE", "isdefault": "FALSE"}]
        d = compare_service._diff_rows(rows_a, rows_b, "db_parameters")
        assert d["different"] == [{"name": "cursor_sharing", "a": "EXACT", "b": "FORCE"}]

    def test_db_parameters_filters_default_values(self):
        """A parameter differing between A and B but left at its default on
        both sides must NOT show up as 'different' — that's environment
        identity/path noise (db_name, control_files, ...), not a real config
        difference worth surfacing."""
        rows_a = [{"name": "db_name", "value": "DEVDB", "isdefault": "TRUE"}]
        rows_b = [{"name": "db_name", "value": "PRODDB", "isdefault": "TRUE"}]
        d = compare_service._diff_rows(rows_a, rows_b, "db_parameters")
        assert d["different"] == []
        assert d["only_in_a"] == []
        assert d["only_in_b"] == []

    def test_non_db_parameters_category_ignores_isdefault(self):
        """The isdefault filter is db_parameters-specific — profile_options/
        responsibilities rows have no such column and must not be dropped."""
        rows_a = [{"name": "MO: Operating Unit", "value": "Vision Ops"}]
        rows_b = [{"name": "MO: Operating Unit", "value": "Vision Ops US"}]
        d = compare_service._diff_rows(rows_a, rows_b, "profile_options")
        assert d["different"] == [{"name": "MO: Operating Unit", "a": "Vision Ops", "b": "Vision Ops US"}]

    def test_cdb_aware_rows_key_by_container_and_name(self):
        """The same parameter name in two different containers (CDB$ROOT vs
        a PDB) must be compared independently, not conflated by name alone."""
        rows_a = [
            {"con_id": 1, "con_name": "CDB$ROOT", "name": "sga_target", "value": "4G", "isdefault": "FALSE"},
            {"con_id": 3, "con_name": "PDB1", "name": "sga_target", "value": "2G", "isdefault": "FALSE"},
        ]
        rows_b = [
            {"con_id": 1, "con_name": "CDB$ROOT", "name": "sga_target", "value": "4G", "isdefault": "FALSE"},
            {"con_id": 3, "con_name": "PDB1", "name": "sga_target", "value": "3G", "isdefault": "FALSE"},
        ]
        d = compare_service._diff_rows(rows_a, rows_b, "db_parameters")
        assert d["different"] == [{"name": "PDB1: sga_target", "a": "2G", "b": "3G"}]

    def test_cdb_aware_rows_still_filter_isdefault(self):
        rows_a = [{"con_id": 1, "con_name": "CDB$ROOT", "name": "db_name", "value": "DEVDB", "isdefault": "TRUE"}]
        rows_b = [{"con_id": 1, "con_name": "CDB$ROOT", "name": "db_name", "value": "PRODDB", "isdefault": "TRUE"}]
        d = compare_service._diff_rows(rows_a, rows_b, "db_parameters")
        assert d["different"] == []


class TestComputeConfigDiff:

    def test_diff_across_categories(self, client, admin_headers, db_session):
        env_a = _make_env(client, admin_headers)
        env_b = _make_env(client, admin_headers)
        db_session.add(models.EnvironmentConfigSnapshot(
            environment_id=env_a["id"], category="responsibilities",
            payload=[{"name": "SYSTEM_ADMIN", "value": "System Administrator"}], scan_status="ok",
        ))
        db_session.add(models.EnvironmentConfigSnapshot(
            environment_id=env_b["id"], category="responsibilities",
            payload=[{"name": "SYSTEM_ADMIN", "value": "System Administrator"},
                    {"name": "CUSTOM_RESP", "value": "Custom Responsibility"}], scan_status="ok",
        ))
        db_session.commit()

        diff = compare_service.compute_config_diff(db_session, env_a["id"], env_b["id"])
        assert set(diff.keys()) == {"db_parameters", "profile_options", "responsibilities"}
        resp = diff["responsibilities"]
        assert resp["a_status"] == "ok"
        assert resp["b_status"] == "ok"
        assert resp["diff"]["only_in_b"] == ["CUSTOM_RESP"]
        # Categories never scanned report that distinctly, not an empty diff pretending to match.
        assert diff["db_parameters"]["a_status"] == "never_scanned"
        assert diff["db_parameters"]["b_status"] == "never_scanned"


class TestRunConfigScan:

    def test_connection_failure_persists_error_snapshots_never_raises(self, client, admin_headers, db_session, monkeypatch):
        import oracledb
        env = _make_env(client, admin_headers, db_host="unreachable.invalid")

        def _raise(**kwargs):
            raise Exception("ORA-12541: TNS:no listener")
        monkeypatch.setattr(oracledb, "connect", _raise)

        compare_service.run_config_scan(env["id"], triggered_by=None)   # must not raise

        snaps = db_session.query(models.EnvironmentConfigSnapshot).filter(
            models.EnvironmentConfigSnapshot.environment_id == env["id"]
        ).all()
        assert len(snaps) == 3
        assert all(s.scan_status == "error" for s in snaps)
        assert {s.category for s in snaps} == {"db_parameters", "profile_options", "responsibilities"}

    def test_no_db_credentials_persists_error_without_connecting(self, client, admin_headers, db_session):
        env = _make_env(client, admin_headers, db_host=None, db_sid=None, db_user=None)
        compare_service.run_config_scan(env["id"], triggered_by=None)
        snaps = db_session.query(models.EnvironmentConfigSnapshot).filter(
            models.EnvironmentConfigSnapshot.environment_id == env["id"]
        ).all()
        assert len(snaps) == 3
        assert all(s.scan_status == "error" for s in snaps)

    def test_cdb_detected_uses_containers_aware_payload(self, client, admin_headers, db_session, monkeypatch):
        """When v$database.cdb = 'YES' and SYSTEM creds resolve, db_parameters
        is scanned via the CONTAINERS()-fan-out path instead of the plain
        single-container query — the other two categories are unaffected."""
        env = _make_env(client, admin_headers)

        class _FakeMainCursor:
            def execute(self, sql, *a): self._sql = sql
            def fetchone(self): return ("YES",)
            def fetchall(self): return []
            @property
            def description(self): return []

        class _FakeMainConn:
            def cursor(self): return _FakeMainCursor()
            def close(self): pass

        import oracledb
        monkeypatch.setattr(oracledb, "connect", lambda **kw: _FakeMainConn())
        monkeypatch.setattr(compare_service, "_cdb_aware_db_parameters", lambda env_row: [
            {"con_id": 1, "con_name": "CDB$ROOT", "name": "sga_target", "value": "4G", "isdefault": "FALSE"},
        ])

        compare_service.run_config_scan(env["id"], triggered_by=None)

        snap = db_session.query(models.EnvironmentConfigSnapshot).filter(
            models.EnvironmentConfigSnapshot.environment_id == env["id"],
            models.EnvironmentConfigSnapshot.category == "db_parameters",
        ).first()
        assert snap.scan_status == "ok"
        assert snap.payload[0]["con_name"] == "CDB$ROOT"

    def test_cdb_detected_without_system_creds_falls_back_to_plain_query(self, client, admin_headers, db_session, monkeypatch):
        """CDB detected but no SYSTEM password configured -> _cdb_aware_db_parameters
        returns None -> falls back to the existing plain v$parameter query,
        matching this codebase's graceful-degrade convention."""
        env = _make_env(client, admin_headers)   # no system_password set

        class _FakeMainCursor:
            def __init__(self):
                self.description = [("name",), ("value",), ("isdefault",)]
            def execute(self, sql, *a):
                self._sql = sql
            def fetchone(self): return ("YES",)
            def fetchall(self):
                if "v$database" in self._sql:
                    return [("YES",)]
                return [("cursor_sharing", "EXACT", "FALSE")]

        class _FakeMainConn:
            def cursor(self): return _FakeMainCursor()
            def close(self): pass

        import oracledb
        monkeypatch.setattr(oracledb, "connect", lambda **kw: _FakeMainConn())

        compare_service.run_config_scan(env["id"], triggered_by=None)

        snap = db_session.query(models.EnvironmentConfigSnapshot).filter(
            models.EnvironmentConfigSnapshot.environment_id == env["id"],
            models.EnvironmentConfigSnapshot.category == "db_parameters",
        ).first()
        assert snap.scan_status == "ok"
        assert snap.payload == [{"name": "cursor_sharing", "value": "EXACT", "isdefault": "FALSE"}]


class TestCdbAwareDbParameters:

    def test_returns_none_without_system_credentials(self, client, admin_headers, db_session):
        env = _make_env(client, admin_headers)   # no system_password set
        env_row = db_session.query(models.EbsEnvironment).filter(models.EbsEnvironment.id == env["id"]).first()
        assert compare_service._cdb_aware_db_parameters(env_row) is None

    def test_fans_out_across_containers(self, client, admin_headers, db_session, monkeypatch):
        env = _make_env(client, admin_headers)
        r = client.patch(f"/admin/environments/{env['id']}", json={"system_password": "sys_pw"}, headers=admin_headers)
        assert r.status_code == 200
        env_row = db_session.query(models.EbsEnvironment).filter(models.EbsEnvironment.id == env["id"]).first()

        class _FakeCursor:
            def execute(self, sql, *a): self._sql = sql
            def fetchall(self):
                if "v$containers" in self._sql:
                    return [(1, "CDB$ROOT"), (3, "PDB1")]
                return [(1, "sga_target", "4G", "FALSE"), (3, "sga_target", "2G", "FALSE")]

        class _FakeConn:
            def cursor(self): return _FakeCursor()
            def close(self): pass

        import oracledb
        monkeypatch.setattr(oracledb, "connect", lambda **kw: _FakeConn())

        rows = compare_service._cdb_aware_db_parameters(env_row)
        assert rows == [
            {"con_id": 1, "con_name": "CDB$ROOT", "name": "sga_target", "value": "4G", "isdefault": "FALSE"},
            {"con_id": 3, "con_name": "PDB1", "name": "sga_target", "value": "2G", "isdefault": "FALSE"},
        ]

    def test_query_failure_returns_none(self, client, admin_headers, db_session, monkeypatch):
        env = _make_env(client, admin_headers)
        client.patch(f"/admin/environments/{env['id']}", json={"system_password": "sys_pw"}, headers=admin_headers)
        env_row = db_session.query(models.EbsEnvironment).filter(models.EbsEnvironment.id == env["id"]).first()

        import oracledb
        def _raise(**kw):
            raise Exception("ORA-01031: insufficient privileges")
        monkeypatch.setattr(oracledb, "connect", _raise)
        assert compare_service._cdb_aware_db_parameters(env_row) is None


class TestIsCdb:

    def test_true_when_cdb_yes(self):
        class _Cur:
            def execute(self, sql): pass
            def fetchone(self): return ("YES",)
        class _Conn:
            def cursor(self): return _Cur()
        assert compare_service._is_cdb(_Conn()) is True

    def test_false_when_cdb_no(self):
        class _Cur:
            def execute(self, sql): pass
            def fetchone(self): return ("NO",)
        class _Conn:
            def cursor(self): return _Cur()
        assert compare_service._is_cdb(_Conn()) is False

    def test_false_on_query_failure(self):
        class _Cur:
            def execute(self, sql): raise Exception("no privilege")
        class _Conn:
            def cursor(self): return _Cur()
        assert compare_service._is_cdb(_Conn()) is False


class TestCompareRouterGating:

    def test_patches_requires_patching_agent(self, client, admin_headers, regular_user_headers):
        env_a = _make_env(client, admin_headers)
        env_b = _make_env(client, admin_headers)
        r = client.get(f"/compare/patches?environment_a_id={env_a['id']}&environment_b_id={env_b['id']}",
                       headers=regular_user_headers)
        assert r.status_code == 403

    def test_patches_unknown_environment_404(self, client, admin_headers):
        env_a = _make_env(client, admin_headers)
        r = client.get(f"/compare/patches?environment_a_id={env_a['id']}&environment_b_id=999999",
                       headers=admin_headers)
        assert r.status_code == 404

    def test_patches_ok_for_admin(self, client, admin_headers):
        env_a = _make_env(client, admin_headers)
        env_b = _make_env(client, admin_headers)
        r = client.get(f"/compare/patches?environment_a_id={env_a['id']}&environment_b_id={env_b['id']}",
                       headers=admin_headers)
        assert r.status_code == 200
        assert r.json() == []

    def test_config_requires_performance_agent(self, client, admin_headers, regular_user_headers):
        env_a = _make_env(client, admin_headers)
        env_b = _make_env(client, admin_headers)
        r = client.get(f"/compare/config?environment_a_id={env_a['id']}&environment_b_id={env_b['id']}",
                       headers=regular_user_headers)
        assert r.status_code == 403

    def test_config_scan_unknown_environment_404(self, client, admin_headers):
        r = client.post("/compare/config/scan/999999", headers=admin_headers)
        assert r.status_code == 404

    def test_config_scan_trigger_accepted(self, client, admin_headers):
        env = _make_env(client, admin_headers)
        r = client.post(f"/compare/config/scan/{env['id']}", headers=admin_headers)
        assert r.status_code == 202

    def test_performance_compare_requires_performance_agent(self, client, admin_headers, regular_user_headers):
        env_a = _make_env(client, admin_headers)
        env_b = _make_env(client, admin_headers)
        r = client.post("/compare/performance", json={
            "environment_a_id": env_a["id"], "environment_b_id": env_b["id"],
        }, headers=regular_user_headers)
        assert r.status_code == 403

    def test_performance_compare_unknown_environment_404(self, client, admin_headers):
        env_a = _make_env(client, admin_headers)
        r = client.post("/compare/performance", json={
            "environment_a_id": env_a["id"], "environment_b_id": 999999,
        }, headers=admin_headers)
        assert r.status_code == 404
