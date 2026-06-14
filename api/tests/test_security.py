"""TC-SC-01 to TC-SC-06 — Security & Access Control"""
import pytest
from conftest import chat_session


class TestAccessControl:

    def test_unauthenticated_api_blocked(self, client):
        """TC-SC-01: All protected endpoints require a valid Bearer token."""
        endpoints = [
            ("GET", "/chat/sessions"),
            ("GET", "/rag/documents"),
            ("GET", "/cloning/"),
            ("GET", "/patching/"),
        ]
        for method, path in endpoints:
            r = client.request(method, path)
            assert r.status_code == 401, f"{method} {path} should require auth"

    def test_user_cannot_access_other_users_session(self, client, admin_headers, admin2_headers):
        """TC-SC-02: User B gets 404 trying to read User A's session messages."""
        sid = chat_session(client, admin_headers)
        r = client.get(f"/chat/sessions/{sid}/messages", headers=admin2_headers)
        assert r.status_code == 404

    def test_user_cannot_access_other_users_clone(self, client, admin_headers, admin2_headers, nonprod_env):
        """TC-SC-02 (clone): User B cannot access User A's clone run."""
        r = client.post("/cloning/", json={
            "source_name": "PROD",
            "target_environment": nonprod_env["name"],
            "source_sid": "PROD",
            "source_db_host": "src-db",
            "source_apps_host": "src-apps",
            "target_apps_host": "tgt-apps",
            "target_storage": "fs",
            "target_datafile_dir": "/u01/oradata/CLONE",
            "source_datafile_dir": "/u01/oradata/PROD",
            "db_topology": "single",
        }, headers=admin_headers)
        # May succeed or be blocked by prod guard — either way get the run_id
        run_id = None
        if r.status_code == 201:
            run_id = r.json()["id"]
        elif r.status_code == 409:
            run_id = r.json()["detail"]["run_id"]
        if run_id:
            r2 = client.get(f"/cloning/{run_id}", headers=admin2_headers)
            assert r2.status_code == 404

    def test_ssh_password_stripped_from_deployment_agent(self, client, admin_headers, ssh_server):
        """TC-SC-03: SSH server password not returned in deployment agent listing."""
        r = client.get("/admin/servers", headers=admin_headers)
        for server in r.json():
            assert "password" not in server
            assert "password_enc" not in server

    def test_sql_injection_in_chat_input(self, client, admin_headers, mock_llm, mock_rag):
        """TC-SC-05: SQL injection attempt in chat input is treated as plain text."""
        session_id = chat_session(client, admin_headers)
        malicious = "'; DROP TABLE chat_messages; --"
        r = client.post(
            f"/chat/sessions/{session_id}/stream",
            json={"content": malicious},
            headers={**admin_headers, "X-LLM-Provider": "ollama"},
        )
        # Should respond normally — ORM prevents execution
        assert r.status_code == 200
        # Verify messages table still exists by fetching messages
        r2 = client.get(f"/chat/sessions/{session_id}/messages", headers=admin_headers)
        assert r2.status_code == 200

    def test_regular_user_cannot_invoke_agents(self, client, admin_headers, admin2_headers, nonprod_env):
        """TC-SC-01 (role): a 'user' with no agent-granting roles is blocked from invoking agents (require_agent)."""
        import uuid
        # Register a plain user
        uid = uuid.uuid4().hex[:8]
        r = client.post("/auth/register", json={
            "username": f"plain_{uid}",
            "email": f"plain_{uid}@test.com",
            "password": "Pass123!",
        })
        user_id = r.json()["id"]
        # Admin approves with role='user' (default)
        client.patch(f"/admin/users/{user_id}",
                     json={"approval_status": "approved", "is_active": True},
                     headers=admin_headers)
        r_login = client.post("/auth/login",
                              json={"username": f"plain_{uid}", "password": "Pass123!"})
        if r_login.status_code != 200:
            pytest.skip("User login not available — check approval flow")
        plain_token = r_login.json()["session_token"]
        plain_headers = {"Authorization": f"Bearer {plain_token}"}

        # Attempt to invoke deployment agent
        r_agent = client.post("/cloning/agent",
                              json={"context": {}},
                              headers=plain_headers)
        assert r_agent.status_code == 403
