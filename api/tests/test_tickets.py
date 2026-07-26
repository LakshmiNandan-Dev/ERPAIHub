"""Findings-to-ticket workflow — manual creation, finding conversion, status
transitions, and the resolution_notes-required-on-close rule."""
import pytest

from app.modules.dba.performance.monitoring_scheduler import run_scheduled_scan


def _seed_finding(client, admin_headers, nonprod_env, monkeypatch):
    """Force a deterministic finding via a scan, return its id."""
    from app.modules.dba.performance import performance_service

    def _hot_tablespace(env):
        return {"query": "x", "rows": [{"tablespace_name": "APPS_TS_TX_DATA", "total_gb": 100,
                                        "used_gb": 97, "free_gb": 3, "used_pct": 97}]}
    monkeypatch.setitem(performance_service._SIM_RUNNERS, "tablespace", _hot_tablespace)

    run_scheduled_scan(nonprod_env["id"], trigger="manual")
    findings = client.get(
        f"/monitoring/findings?environment_id={nonprod_env['id']}&area=tablespace", headers=admin_headers
    ).json()
    assert len(findings) == 1
    return findings[0]["id"]


class TestManualTicketCreation:

    def test_create_ticket_requires_auth(self, client):
        r = client.post("/tickets/", json={"title": "Investigate slow batch job"})
        assert r.status_code == 401

    def test_create_ticket_minimal(self, client, admin_headers):
        r = client.post("/tickets/", json={"title": "Investigate slow batch job"}, headers=admin_headers)
        assert r.status_code == 201
        data = r.json()
        assert data["title"] == "Investigate slow batch job"
        assert data["status"] == "open"
        assert data["priority"] == "medium"

    def test_create_ticket_with_environment_snapshots_name(self, client, admin_headers, nonprod_env):
        r = client.post("/tickets/", json={
            "title": "Tablespace cleanup", "environment_id": nonprod_env["id"], "priority": "high",
        }, headers=admin_headers)
        assert r.status_code == 201
        data = r.json()
        assert data["environment_id"] == nonprod_env["id"]
        assert data["environment_name"] == nonprod_env["name"]


class TestTicketFromFinding:

    def test_convert_finding_requires_agent_grant(self, client, regular_user_headers, admin_headers, nonprod_env, monkeypatch):
        finding_id = _seed_finding(client, admin_headers, nonprod_env, monkeypatch)
        r = client.post(f"/tickets/from-finding/{finding_id}", json={}, headers=regular_user_headers)
        assert r.status_code == 403

    def test_convert_finding_unknown_404(self, client, admin_headers):
        r = client.post("/tickets/from-finding/999999", json={}, headers=admin_headers)
        assert r.status_code == 404

    def test_convert_finding_snapshots_source(self, client, admin_headers, nonprod_env, monkeypatch):
        finding_id = _seed_finding(client, admin_headers, nonprod_env, monkeypatch)
        r = client.post(f"/tickets/from-finding/{finding_id}", json={"priority": "critical"}, headers=admin_headers)
        assert r.status_code == 201
        data = r.json()
        assert data["finding_id"] == finding_id
        assert data["environment_id"] == nonprod_env["id"]
        assert data["priority"] == "critical"
        assert "Tablespace" in data["title"]


class TestTicketTransitions:

    def _create(self, client, admin_headers):
        return client.post("/tickets/", json={"title": "Fix it"}, headers=admin_headers).json()

    def test_assign_and_move_in_progress(self, client, admin_headers):
        t = self._create(client, admin_headers)
        r = client.patch(f"/tickets/{t['id']}", json={"status": "in_progress"}, headers=admin_headers)
        assert r.status_code == 200
        assert r.json()["status"] == "in_progress"

    def test_resolve_without_notes_rejected(self, client, admin_headers):
        t = self._create(client, admin_headers)
        r = client.patch(f"/tickets/{t['id']}", json={"status": "resolved"}, headers=admin_headers)
        assert r.status_code == 400

    def test_resolve_with_notes_stamps_resolver(self, client, admin_headers):
        t = self._create(client, admin_headers)
        r = client.patch(f"/tickets/{t['id']}", json={
            "status": "resolved", "resolution_notes": "Extended tablespace, added autoextend.",
        }, headers=admin_headers)
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "resolved"
        assert data["resolved_by"] is not None
        assert data["resolved_at"] is not None

    def test_dismiss_without_notes_rejected(self, client, admin_headers):
        t = self._create(client, admin_headers)
        r = client.patch(f"/tickets/{t['id']}", json={"status": "dismissed"}, headers=admin_headers)
        assert r.status_code == 400

    def test_list_filters_by_status(self, client, admin_headers):
        t1 = self._create(client, admin_headers)
        client.patch(f"/tickets/{t1['id']}", json={
            "status": "resolved", "resolution_notes": "done",
        }, headers=admin_headers)
        self._create(client, admin_headers)  # stays open

        open_tickets = client.get("/tickets/?status_filter=open", headers=admin_headers).json()
        assert all(t["status"] == "open" for t in open_tickets)
        assert any(t["id"] != t1["id"] for t in open_tickets)

    def test_delete_requires_admin(self, client, regular_user_headers, admin_headers):
        t = self._create(client, admin_headers)
        r = client.delete(f"/tickets/{t['id']}", headers=regular_user_headers)
        assert r.status_code == 403

    def test_delete_as_admin(self, client, admin_headers):
        t = self._create(client, admin_headers)
        r = client.delete(f"/tickets/{t['id']}", headers=admin_headers)
        assert r.status_code == 204
        assert client.get(f"/tickets/{t['id']}", headers=admin_headers).status_code == 404
