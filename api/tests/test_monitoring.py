"""Scheduled, delta-aware monitoring — schedule config, on-demand scan trigger,
and the findings/run feed produced by monitoring_scheduler.run_scheduled_scan."""
import pytest

from app import models
from app.modules.dba.performance import performance_service, monitoring_service
from app.modules.dba.performance.monitoring_scheduler import run_scheduled_scan


class TestScheduleConfig:

    def test_list_schedules_includes_all_environments(self, client, admin_headers, nonprod_env):
        r = client.get("/monitoring/schedules", headers=admin_headers)
        assert r.status_code == 200
        names = [s["environment_name"] for s in r.json()]
        assert nonprod_env["name"] in names

    def test_schedules_require_admin(self, client, regular_user_headers, nonprod_env):
        r = client.get("/monitoring/schedules", headers=regular_user_headers)
        assert r.status_code == 403

    def test_upsert_schedule_enables_interval(self, client, admin_headers, nonprod_env):
        r = client.put(f"/monitoring/schedules/{nonprod_env['id']}", json={
            "enabled": True, "interval_minutes": 30, "areas": ["tablespace", "locks"],
        }, headers=admin_headers)
        assert r.status_code == 200
        data = r.json()
        assert data["enabled"] is True
        assert data["interval_minutes"] == 30
        assert data["areas"] == ["tablespace", "locks"]

    def test_upsert_schedule_unknown_area_rejected(self, client, admin_headers, nonprod_env):
        r = client.put(f"/monitoring/schedules/{nonprod_env['id']}", json={
            "enabled": True, "interval_minutes": 30, "areas": ["not_a_real_area"],
        }, headers=admin_headers)
        assert r.status_code == 400

    def test_upsert_schedule_unknown_environment_404(self, client, admin_headers):
        r = client.put("/monitoring/schedules/999999", json={
            "enabled": True, "interval_minutes": 30,
        }, headers=admin_headers)
        assert r.status_code == 404


class TestOnDemandScan:

    def test_trigger_scan_requires_agent_access(self, client, regular_user_headers, nonprod_env):
        r = client.post(f"/monitoring/scan/{nonprod_env['id']}", headers=regular_user_headers)
        assert r.status_code == 403

    def test_trigger_scan_unknown_environment_404(self, client, admin_headers):
        r = client.post("/monitoring/scan/999999", headers=admin_headers)
        assert r.status_code == 404

    def test_trigger_scan_accepted_and_produces_a_run(self, client, admin_headers, nonprod_env):
        r = client.post(f"/monitoring/scan/{nonprod_env['id']}", headers=admin_headers)
        assert r.status_code == 202

        runs = client.get(f"/monitoring/runs?environment_id={nonprod_env['id']}", headers=admin_headers).json()
        assert len(runs) == 1
        assert runs[0]["status"] == "completed"
        assert runs[0]["trigger"] == "manual"
        assert runs[0]["kind"] == "performance"


class TestFindingsDeltaLogic:
    """Exercise run_scheduled_scan directly (not via BackgroundTasks) against a
    real DB-backed environment row so we can assert on Finding rows across
    repeated runs — new / unchanged / resolved."""

    def test_repeated_scan_of_unchanged_env_reports_no_new_findings(self, client, admin_headers, nonprod_env, db_session):
        run_scheduled_scan(nonprod_env["id"], trigger="manual")
        run_scheduled_scan(nonprod_env["id"], trigger="manual")

        runs = client.get(f"/monitoring/runs?environment_id={nonprod_env['id']}", headers=admin_headers).json()
        assert len(runs) == 2
        # created_at can tie within a single DB transaction (server-side now());
        # /monitoring/runs breaks ties by id desc, so [0] is reliably the 2nd scan.
        second_run = runs[0]
        # Simulated data is deterministic per environment name, so a second
        # scan of the same (unchanged) environment must not report new/changed
        # findings — only re-confirmation of what's already open.
        assert second_run["new_findings_count"] == 0
        assert second_run["changed_findings_count"] == 0

    def test_scan_creates_findings_when_simulated_data_has_issues(self, client, admin_headers, monkeypatch, nonprod_env):
        """Force a deterministic 'issue' via a monkeypatched sim generator so
        the test doesn't depend on the specific hash-seeded random data for
        this environment name actually crossing a threshold."""
        def _hot_tablespace(env):
            return {"query": "x", "rows": [{"tablespace_name": "APPS_TS_TX_DATA", "total_gb": 100,
                                            "used_gb": 97, "free_gb": 3, "used_pct": 97}]}

        monkeypatch.setattr(performance_service, "sim_tablespace", _hot_tablespace)
        monkeypatch.setitem(performance_service._SIM_RUNNERS, "tablespace", _hot_tablespace)

        run_scheduled_scan(nonprod_env["id"], trigger="manual")

        findings = client.get(
            f"/monitoring/findings?environment_id={nonprod_env['id']}&area=tablespace",
            headers=admin_headers,
        ).json()
        assert len(findings) == 1
        assert findings[0]["severity"] == "critical"
        assert findings[0]["status"] == "open"

    def test_resolved_finding_is_marked_resolved_when_no_longer_detected(self, client, admin_headers, monkeypatch, nonprod_env):
        def _hot_tablespace(env):
            return {"query": "x", "rows": [{"tablespace_name": "APPS_TS_TX_DATA", "total_gb": 100,
                                            "used_gb": 97, "free_gb": 3, "used_pct": 97}]}

        def _healthy_tablespace(env):
            return {"query": "x", "rows": [{"tablespace_name": "APPS_TS_TX_DATA", "total_gb": 100,
                                            "used_gb": 40, "free_gb": 60, "used_pct": 40}]}

        monkeypatch.setitem(performance_service._SIM_RUNNERS, "tablespace", _hot_tablespace)
        run_scheduled_scan(nonprod_env["id"], trigger="manual")

        monkeypatch.setitem(performance_service._SIM_RUNNERS, "tablespace", _healthy_tablespace)
        run_scheduled_scan(nonprod_env["id"], trigger="manual")

        open_findings = client.get(
            f"/monitoring/findings?environment_id={nonprod_env['id']}&area=tablespace&status_filter=open",
            headers=admin_headers,
        ).json()
        assert open_findings == []

        resolved_findings = client.get(
            f"/monitoring/findings?environment_id={nonprod_env['id']}&area=tablespace&status_filter=resolved",
            headers=admin_headers,
        ).json()
        assert len(resolved_findings) == 1


class TestFindingsQueryFilters:
    """The 'q' title-search param and the area filter's exact->ilike change
    (frontend's area control is free-text, so exact match would silently
    return empty results on any case/substring mismatch)."""

    def _seed(self, db_session, environment_id):
        db_session.add(models.Finding(
            environment_id=environment_id, environment_name="ENV", source="patch_file_gap",
            area="ap", finding_key="filever:APXVCHKB.pls", severity="warning",
            title="APXVCHKB.pls: filesystem version 120.3 != AD_FILES version 120.5",
            detail={"filename": "APXVCHKB.pls", "fs_version": "120.3", "db_version": "120.5"},
            status="open",
        ))
        db_session.add(models.Finding(
            environment_id=environment_id, environment_name="ENV", source="patch_gap",
            area="db_home", finding_key="missing:37123456", severity="warning",
            title="db_home: patch 37123456 not applied",
            detail={"patch_number": "37123456"}, status="open",
        ))
        db_session.commit()

    def test_q_searches_title_substring(self, client, admin_headers, nonprod_env, db_session):
        self._seed(db_session, nonprod_env["id"])
        r = client.get(
            f"/monitoring/findings?environment_id={nonprod_env['id']}&q=APXVCHKB",
            headers=admin_headers,
        )
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert "APXVCHKB" in data[0]["title"]

    def test_area_filter_is_case_insensitive_substring(self, client, admin_headers, nonprod_env, db_session):
        self._seed(db_session, nonprod_env["id"])
        r = client.get(
            f"/monitoring/findings?environment_id={nonprod_env['id']}&area=AP",
            headers=admin_headers,
        )
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["area"] == "ap"

    def test_source_filter_still_exact(self, client, admin_headers, nonprod_env, db_session):
        self._seed(db_session, nonprod_env["id"])
        r = client.get(
            f"/monitoring/findings?environment_id={nonprod_env['id']}&source=patch_gap",
            headers=admin_headers,
        )
        data = r.json()
        assert len(data) == 1
        assert data[0]["source"] == "patch_gap"

    def test_detail_is_returned_for_drill_down(self, client, admin_headers, nonprod_env, db_session):
        self._seed(db_session, nonprod_env["id"])
        r = client.get(
            f"/monitoring/findings?environment_id={nonprod_env['id']}&source=patch_file_gap",
            headers=admin_headers,
        )
        data = r.json()
        assert data[0]["detail"]["fs_version"] == "120.3"
        assert data[0]["detail"]["db_version"] == "120.5"


class TestExtractFindingsPure:
    """Unit tests for the pure extraction/threshold logic — no DB/HTTP."""

    def test_tablespace_warning_threshold(self):
        findings = performance_service.extract_findings("tablespace", {
            "rows": [{"tablespace_name": "TS1", "used_pct": 90}],
        })
        assert len(findings) == 1
        assert findings[0]["severity"] == "warning"

    def test_tablespace_below_threshold_no_finding(self):
        findings = performance_service.extract_findings("tablespace", {
            "rows": [{"tablespace_name": "TS1", "used_pct": 50}],
        })
        assert findings == []

    def test_lock_wait_over_threshold(self):
        findings = performance_service.extract_findings("locks", {
            "blocking_sessions": [{"blocking_sid": 1, "blocked_sid": 2, "wait_seconds": 500}],
        })
        assert len(findings) == 1
        assert findings[0]["finding_key"] == "1:2"

    def test_value_hash_stable_for_same_fields(self):
        h1 = performance_service.value_hash({"used_pct": 90, "other": "x"}, "used_pct")
        h2 = performance_service.value_hash({"used_pct": 90, "other": "y"}, "used_pct")
        assert h1 == h2

    def test_value_hash_changes_when_field_changes(self):
        h1 = performance_service.value_hash({"used_pct": 90}, "used_pct")
        h2 = performance_service.value_hash({"used_pct": 91}, "used_pct")
        assert h1 != h2
