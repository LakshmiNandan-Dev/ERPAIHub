"""TC-HCM-01..08 — EBS R12 HCM/Payroll functional agent (read-only advisor + inquiry).

Covers the inquiry catalog, the simulator inquiry path + AI interpretation, the
free-text advisor, RBAC gating, required-param validation, and the SELECT-only guard.
"""
import pytest

from conftest import parse_sse, _login
from app.modules.functional.hcm import inquiries
from app.modules.functional.hcm import db as hcm_db
from app.modules.dba.nl_sql import nl_sql_service


class TestCatalog:

    def test_catalog_lists_inquiries(self, client, admin_headers):
        """TC-HCM-01: the inquiry catalog is returned to an authenticated user."""
        r = client.get("/hcm/inquiries", headers=admin_headers)
        assert r.status_code == 200
        ids = [q["id"] for q in r.json()["inquiries"]]
        assert "employee_assignment" in ids and "payroll_run_status" in ids

    def test_catalog_requires_auth(self, client):
        """TC-HCM-02: unauthenticated catalog access is rejected."""
        r = client.get("/hcm/inquiries")
        assert r.status_code == 401


class TestInquiry:

    def test_inquiry_simulator_path(self, client, admin_headers, mock_llm):
        """TC-HCM-03: with no DB configured the inquiry returns simulated rows and
        an AI interpretation."""
        r = client.post(
            "/hcm/inquiry",
            json={"inquiry_id": "employee_assignment", "params": {"employee_number": "12345"},
                  "interpret": True},
            headers={**admin_headers, "X-LLM-Provider": "ollama"},
        )
        assert r.status_code == 200
        events = parse_sse(r.text)
        data = next(e for e in events if isinstance(e, dict) and e.get("type") == "data")
        assert data["source"] == "simulated"
        assert isinstance(data["rows"], list) and data["rows"]
        assert any(isinstance(e, dict) and e.get("type") == "analysis_complete" for e in events)

    def test_inquiry_missing_required_param(self, client, admin_headers, mock_llm):
        """TC-HCM-04: a required parameter (assignment_id) that's omitted yields a
        clear error event, not a query."""
        r = client.post(
            "/hcm/inquiry",
            json={"inquiry_id": "element_entries", "params": {}},
            headers={**admin_headers, "X-LLM-Provider": "ollama"},
        )
        assert r.status_code == 200
        events = parse_sse(r.text)
        err = next(e for e in events if isinstance(e, dict) and e.get("type") == "error")
        assert "Missing required parameter" in err["content"]

    def test_inquiry_unknown_id(self, client, admin_headers, mock_llm):
        """TC-HCM-05: an unknown inquiry id is reported as an error."""
        r = client.post(
            "/hcm/inquiry",
            json={"inquiry_id": "does_not_exist", "params": {}},
            headers={**admin_headers, "X-LLM-Provider": "ollama"},
        )
        assert r.status_code == 200
        events = parse_sse(r.text)
        assert any(isinstance(e, dict) and e.get("type") == "error" for e in events)

    def test_inquiry_requires_agent_grant(self, db_session, client, mock_llm):
        """TC-HCM-06: a non-admin whose role grants no agents is refused (403)."""
        from app import models
        from app.common import utils
        # Approved, active, non-admin user with no role grants → no gated agents.
        u = models.User(username="hcm_nogrant", email="hcm_nogrant@test.com",
                        password_hash=utils.hash_password("x"), is_admin=False,
                        is_active=True, approval_status="approved")
        db_session.add(u)
        db_session.commit()
        tok = utils.create_session_token()
        db_session.add(models.UserSession(
            user_id=u.id, session_token=tok,
            expires_at=utils.get_session_expiration(hours=1)))
        db_session.commit()

        r = client.post(
            "/hcm/inquiry",
            json={"inquiry_id": "employee_assignment", "params": {}},
            headers={"Authorization": f"Bearer {tok}", "X-LLM-Provider": "ollama"},
        )
        assert r.status_code == 403


def _fake_propose(db, env, question):
    return {"sql": "SELECT COUNT(*) FROM per_all_people_f", "graph_valid": True,
           "constrained": True, "explain_ok": None, "note": "", "tables_used": ["per_all_people_f"],
           "schema_id": f"env_{env.id}", "question": question, "requires_confirmation": True, "executed": False}


class TestAsk:

    def test_ask_advisor_streams_answer(self, client, admin_headers, mock_llm, mock_rag):
        """TC-HCM-07: the free-text advisor streams an answer."""
        r = client.post(
            "/hcm/ask",
            json={"question": "What is the Oracle EBS payroll run to GL transfer flow?"},
            headers={**admin_headers, "X-LLM-Provider": "ollama"},
        )
        assert r.status_code == 200
        events = parse_sse(r.text)
        assert any(isinstance(e, dict) and e.get("type") == "analysis_complete" for e in events)

    def test_ask_falls_back_to_nl_sql_for_data_question(self, client, admin_headers, mock_llm, mock_rag, nonprod_env, monkeypatch):
        """A live-data question with a resolved environment tries NL->SQL
        before the general advisor — see nl_sql_service.try_answer_data_question."""
        monkeypatch.setattr(nl_sql_service, "propose", _fake_propose)
        monkeypatch.setattr(nl_sql_service, "execute_with_columns", lambda env, sql, max_rows=50: [{"cnt": 42}])
        r = client.post(
            "/hcm/ask",
            json={"question": "how many employees do we have", "environment": nonprod_env["name"]},
            headers={**admin_headers, "X-LLM-Provider": "ollama"},
        )
        assert r.status_code == 200
        events = parse_sse(r.text)
        data = next(e for e in events if isinstance(e, dict) and e.get("type") == "data")
        assert data["source"] == "nl_sql"
        assert data["rows"] == [{"cnt": 42}]
        assert any(isinstance(e, dict) and e.get("type") == "analysis_complete" for e in events)

    def test_ask_without_environment_uses_general_advice(self, client, admin_headers, mock_llm, mock_rag):
        """No environment named -> existing RAG-grounded advisor path, unchanged."""
        r = client.post(
            "/hcm/ask",
            json={"question": "how many employees do we have"},
            headers={**admin_headers, "X-LLM-Provider": "ollama"},
        )
        assert r.status_code == 200
        events = parse_sse(r.text)
        assert not any(isinstance(e, dict) and e.get("type") == "data" for e in events)
        assert any(isinstance(e, dict) and e.get("type") == "analysis_complete" for e in events)

    def test_ask_non_data_question_skips_nl_sql_even_with_environment(self, client, admin_headers, mock_llm, mock_rag, nonprod_env):
        r = client.post(
            "/hcm/ask",
            json={"question": "What is SSHR?", "environment": nonprod_env["name"]},
            headers={**admin_headers, "X-LLM-Provider": "ollama"},
        )
        assert r.status_code == 200
        events = parse_sse(r.text)
        assert not any(isinstance(e, dict) and e.get("type") == "data" for e in events)

    def test_ask_without_nl_sql_grant_falls_through_silently(self, client, admin_headers, mock_llm, mock_rag, nonprod_env, monkeypatch):
        """A role granting 'hcm' but not 'nl_sql' still gets a normal advisory
        answer for a data question — the fallback is additive, not required."""
        monkeypatch.setattr(nl_sql_service, "propose", _fake_propose)
        r = client.post("/admin/roles", json={"name": "hcm_only", "agent_names": ["hcm"]}, headers=admin_headers)
        assert r.status_code == 201, r.text
        role_id = r.json()["id"]

        reg = client.post("/auth/register", json={
            "username": "hcm_only_user", "email": "hcm_only_user@test.com", "password": "TestPass123!",
        })
        assert reg.status_code == 201, reg.text
        user_id = reg.json()["id"]
        client.patch(f"/admin/users/{user_id}",
                    json={"approval_status": "approved", "is_active": True, "role_ids": [role_id]},
                    headers=admin_headers)
        token = _login(client, "hcm_only_user", "TestPass123!")
        headers = {"Authorization": f"Bearer {token}", "X-LLM-Provider": "ollama"}

        r = client.post(
            "/hcm/ask",
            json={"question": "how many employees do we have", "environment": nonprod_env["name"]},
            headers=headers,
        )
        assert r.status_code == 200
        events = parse_sse(r.text)
        assert not any(isinstance(e, dict) and e.get("type") == "data" for e in events)
        assert any(isinstance(e, dict) and e.get("type") == "analysis_complete" for e in events)


class TestReadOnlyGuard:

    def test_catalog_sql_is_select_only(self):
        """TC-HCM-08: every catalog statement passes the SELECT-only guard, and
        DML is rejected."""
        for q in inquiries.INQUIRIES:
            hcm_db.assert_select_only(q["sql"])  # must not raise
        for bad in ("UPDATE per_all_people_f SET x=1",
                    "DELETE FROM pay_element_entries_f",
                    "SELECT 1; DROP TABLE x"):
            with pytest.raises(ValueError):
                hcm_db.assert_select_only(bad)
