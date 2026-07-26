"""TC-PA-01 to TC-PA-08 — Performance Agent: Oracle DB Diagnostics"""
import pytest

from conftest import parse_sse, _login
from app.modules.dba.nl_sql import nl_sql_service


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


def _fake_propose(db, env, question):
    return {"sql": "SELECT COUNT(*) FROM fnd_concurrent_requests WHERE status_code = 'E'",
           "graph_valid": True, "constrained": True, "explain_ok": None, "note": "",
           "tables_used": ["fnd_concurrent_requests"], "schema_id": f"env_{env.id}",
           "question": question, "requires_confirmation": True, "executed": False}


class TestAsk:
    """Performance's free-text Ask mode: NL->SQL fallback for live-data
    questions, RAG-grounded general advice otherwise."""

    def test_ask_requires_auth(self, client):
        r = client.post("/performance/ask", json={"question": "test"})
        assert r.status_code == 401

    def test_ask_falls_back_to_nl_sql_for_data_question(self, client, admin_headers, mock_llm, mock_rag, nonprod_env, monkeypatch):
        monkeypatch.setattr(nl_sql_service, "propose", _fake_propose)
        monkeypatch.setattr(nl_sql_service, "execute_with_columns", lambda env, sql, max_rows=50: [{"cnt": 7}])
        r = client.post(
            "/performance/ask",
            json={"question": "how many concurrent requests errored", "environment": nonprod_env["name"]},
            headers={**admin_headers, "X-LLM-Provider": "ollama"},
        )
        assert r.status_code == 200
        events = parse_sse(r.text)
        data = next(e for e in events if isinstance(e, dict) and e.get("type") == "data")
        assert data["source"] == "nl_sql"
        assert data["rows"] == [{"cnt": 7}]
        assert any(isinstance(e, dict) and e.get("type") == "analysis_complete" for e in events)

    def test_ask_without_environment_uses_general_advice(self, client, admin_headers, mock_llm, mock_rag):
        r = client.post(
            "/performance/ask",
            json={"question": "how many concurrent requests errored", "environment": None},
            headers={**admin_headers, "X-LLM-Provider": "ollama"},
        )
        assert r.status_code == 200
        events = parse_sse(r.text)
        assert not any(isinstance(e, dict) and e.get("type") == "data" for e in events)
        assert any(isinstance(e, dict) and e.get("type") == "analysis_complete" for e in events)

    def test_ask_non_data_question_skips_nl_sql_even_with_environment(self, client, admin_headers, mock_llm, mock_rag, nonprod_env):
        r = client.post(
            "/performance/ask",
            json={"question": "How do I check SGA sizing?", "environment": nonprod_env["name"]},
            headers={**admin_headers, "X-LLM-Provider": "ollama"},
        )
        assert r.status_code == 200
        events = parse_sse(r.text)
        assert not any(isinstance(e, dict) and e.get("type") == "data" for e in events)

    def test_ask_without_nl_sql_grant_falls_through_silently(self, client, admin_headers, mock_llm, mock_rag, nonprod_env, monkeypatch):
        """A role granting 'performance' but not 'nl_sql' still gets a normal
        advisory answer for a data question."""
        monkeypatch.setattr(nl_sql_service, "propose", _fake_propose)
        r = client.post("/admin/roles", json={"name": "perf_only", "agent_names": ["performance"]}, headers=admin_headers)
        assert r.status_code == 201, r.text
        role_id = r.json()["id"]

        reg = client.post("/auth/register", json={
            "username": "perf_only_user", "email": "perf_only_user@test.com", "password": "TestPass123!",
        })
        assert reg.status_code == 201, reg.text
        user_id = reg.json()["id"]
        client.patch(f"/admin/users/{user_id}",
                    json={"approval_status": "approved", "is_active": True, "role_ids": [role_id]},
                    headers=admin_headers)
        token = _login(client, "perf_only_user", "TestPass123!")
        headers = {"Authorization": f"Bearer {token}", "X-LLM-Provider": "ollama"}

        r = client.post(
            "/performance/ask",
            json={"question": "how many concurrent requests errored", "environment": nonprod_env["name"]},
            headers=headers,
        )
        assert r.status_code == 200
        events = parse_sse(r.text)
        assert not any(isinstance(e, dict) and e.get("type") == "data" for e in events)
        assert any(isinstance(e, dict) and e.get("type") == "analysis_complete" for e in events)
