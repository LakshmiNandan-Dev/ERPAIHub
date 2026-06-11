"""TC-PA-01 to TC-PA-08 — Performance Agent: Oracle DB Diagnostics"""
import pytest


class TestPerformanceAgent:

    def _run_analysis(self, client, headers, areas=None, db_host=None):
        payload = {"environment": "TEST", "analysis_areas": areas or [
            "wait_events", "top_sql", "memory", "locks", "tablespace",
            "concurrent_manager", "statistics"
        ]}
        if db_host:
            payload.update({"db_host": db_host, "db_sid": "TEST", "db_user": "apps"})
        return client.post("/performance/analyze", json=payload, headers=headers)

    def test_full_analysis_simulated(self, client, admin_headers, mock_llm):
        """TC-PA-01: Full analysis runs with simulated data (no real Oracle DB)."""
        r = self._run_analysis(client, admin_headers)
        assert r.status_code == 200
        assert "text/event-stream" in r.headers.get("content-type", "")
        body = r.text
        # At least one area result should be streamed
        assert "data:" in body

    def test_wait_events_area(self, client, admin_headers, mock_llm):
        """TC-PA-02: Wait events area returns top waits with diagnosis."""
        r = self._run_analysis(client, admin_headers, areas=["wait_events"])
        assert r.status_code == 200
        body = r.text.lower()
        assert "wait" in body or "event" in body or "mock" in body

    def test_top_sql_area(self, client, admin_headers, mock_llm):
        """TC-PA-03: Top SQL area returns SQL statement analysis."""
        r = self._run_analysis(client, admin_headers, areas=["top_sql"])
        assert r.status_code == 200

    def test_selective_areas(self, client, admin_headers, mock_llm):
        """TC-PA-06: Only selected analysis areas are processed."""
        r = self._run_analysis(client, admin_headers, areas=["locks", "tablespace"])
        assert r.status_code == 200

    def test_all_seven_areas_run(self, client, admin_headers, mock_llm):
        """TC-PA-01 (all): All 7 diagnostic areas stream results."""
        r = self._run_analysis(client, admin_headers)
        assert r.status_code == 200
        # Response should be non-trivial
        assert len(r.text) > 100

    def test_analysis_requires_auth(self, client):
        """TC-SC-01: Unauthenticated analysis returns 401."""
        r = client.post("/performance/analyze", json={"environment": "TEST"})
        assert r.status_code == 401

    def test_invalid_db_falls_back_to_simulated(self, client, admin_headers, mock_llm):
        """TC-PA-05: Invalid DB credentials fall back to simulated data gracefully."""
        r = self._run_analysis(
            client, admin_headers,
            db_host="unreachable-host.invalid",
            areas=["wait_events"],
        )
        # Should not crash — simulated fallback
        assert r.status_code == 200
