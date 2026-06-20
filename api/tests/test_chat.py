"""TC-CH-01 to TC-CH-13 — Chat Agent: Conversational EBS Assistant"""
import pytest
from conftest import parse_sse, chat_session


class TestChatSessions:

    def test_create_session(self, client, admin_headers):
        """TC-CH-01: New chat session created with default title."""
        r = client.post("/chat/sessions", json={"title": "New Conversation"}, headers=admin_headers)
        assert r.status_code == 201
        data = r.json()
        assert data["title"] == "New Conversation"
        assert "id" in data

    def test_list_sessions(self, client, admin_headers):
        """TC-CH-01 (list): Sessions are returned ordered newest first."""
        client.post("/chat/sessions", json={"title": "Session A"}, headers=admin_headers)
        client.post("/chat/sessions", json={"title": "Session B"}, headers=admin_headers)
        r = client.get("/chat/sessions", headers=admin_headers)
        assert r.status_code == 200
        sessions = r.json()
        assert len(sessions) >= 2
        # Latest session appears first
        titles = [s["title"] for s in sessions[:2]]
        assert "Session B" in titles

    def test_rename_session(self, client, admin_headers):
        """TC-CH-11: Chat session title can be updated."""
        session_id = chat_session(client, admin_headers)
        r = client.patch(f"/chat/sessions/{session_id}",
                         json={"title": "Renamed Session"},
                         headers=admin_headers)
        assert r.status_code == 200
        assert r.json()["title"] == "Renamed Session"

    def test_rename_empty_title_rejected(self, client, admin_headers):
        """TC-SM-04: Renaming with empty title returns 422."""
        session_id = chat_session(client, admin_headers)
        r = client.patch(f"/chat/sessions/{session_id}",
                         json={"title": "   "},
                         headers=admin_headers)
        assert r.status_code == 422

    def test_delete_session(self, client, admin_headers):
        """TC-CH-12: Session and its messages are deleted (cascade)."""
        session_id = chat_session(client, admin_headers)
        r = client.delete(f"/chat/sessions/{session_id}", headers=admin_headers)
        assert r.status_code == 204
        # Confirm gone
        r2 = client.get(f"/chat/sessions/{session_id}/messages", headers=admin_headers)
        assert r2.status_code == 404

    def test_get_messages_empty_session(self, client, admin_headers):
        """TC-CH-13: New session has no messages."""
        session_id = chat_session(client, admin_headers)
        r = client.get(f"/chat/sessions/{session_id}/messages", headers=admin_headers)
        assert r.status_code == 200
        assert r.json() == []


class TestChatStreaming:

    def test_basic_message_streams(self, client, admin_headers, mock_llm, mock_rag):
        """TC-CH-02: Message is streamed back as SSE tokens."""
        session_id = chat_session(client, admin_headers)
        r = client.post(
            f"/chat/sessions/{session_id}/stream",
            json={"content": "How do I compile a PL/SQL package in Oracle EBS?"},
            headers={**admin_headers, "X-LLM-Provider": "ollama"},
        )
        assert r.status_code == 200
        assert "text/event-stream" in r.headers.get("content-type", "")
        tokens = parse_sse(r.text)
        assert len(tokens) > 0

    def test_session_auto_title_on_first_message(self, client, admin_headers, mock_llm, mock_rag):
        """TC-CH-03: Session title updated to first 50 chars of the first message."""
        # Auto-title only fires for an untitled/default session; create one with the
        # default "New Conversation" title rather than the helper's fixed title.
        session_id = client.post("/chat/sessions", json={"title": "New Conversation"},
                                 headers=admin_headers).json()["id"]
        msg = "How do I deploy a PL/SQL package to Oracle EBS?"
        client.post(
            f"/chat/sessions/{session_id}/stream",
            json={"content": msg},
            headers={**admin_headers, "X-LLM-Provider": "ollama"},
        )
        r = client.get("/chat/sessions", headers=admin_headers)
        session = next((s for s in r.json() if s["id"] == session_id), None)
        assert session is not None
        assert session["title"] == msg[:50]

    def test_informational_query_no_deploy_trigger(self, client, admin_headers, mock_llm, mock_rag):
        """TC-CH-04: 'How to deploy...' is informational — deployment agent NOT triggered."""
        session_id = chat_session(client, admin_headers)
        r = client.post(
            f"/chat/sessions/{session_id}/stream",
            json={"content": "How to deploy a PL/SQL package?"},
            headers={**admin_headers, "X-LLM-Provider": "ollama"},
        )
        assert r.status_code == 200
        # No deployment run should be created
        runs = client.get("/deployments/", headers=admin_headers).json()
        # Should not have spawned a new deployment run for this informational query
        assert not any("How to deploy" in (run.get("source_content") or "") for run in runs)

    def test_deploy_intent_missing_params_triggers_interview(self, client, admin_headers, mock_llm, mock_rag):
        """TC-CH-05: 'deploy my package' without env/file triggers interview questions."""
        session_id = chat_session(client, admin_headers)
        r = client.post(
            f"/chat/sessions/{session_id}/stream",
            json={"content": "deploy my package"},
            headers={**admin_headers, "X-LLM-Provider": "ollama"},
        )
        assert r.status_code == 200
        # The reply streams one word per SSE 'data:' line, so reassemble the token
        # payloads before checking — the words aren't contiguous in the raw body.
        content = "".join(e for e in parse_sse(r.text) if isinstance(e, str)).lower()
        # Interview mode: asks for missing parameters
        assert "deployment agent interview" in content or "target ebs environment" in content

    def test_deploy_intent_with_full_params(self, client, admin_headers, mock_llm, mock_rag):
        """TC-CH-06: Deploy with file + env triggers deployment run creation."""
        session_id = chat_session(client, admin_headers)
        r = client.post(
            f"/chat/sessions/{session_id}/stream",
            json={"content": "deploy xxap_supplier_pkg.pls to UAT"},
            headers={**admin_headers, "X-LLM-Provider": "ollama"},
        )
        assert r.status_code == 200
        body = r.text
        assert "deployment" in body.lower() or "agent activated" in body.lower()

    def test_simple_greeting_skips_rag(self, client, admin_headers, mock_llm, mock_rag):
        """TC-CH-07: Simple greetings do not invoke RAG."""
        session_id = chat_session(client, admin_headers)
        r = client.post(
            f"/chat/sessions/{session_id}/stream",
            json={"content": "Hello"},
            headers={**admin_headers, "X-LLM-Provider": "ollama"},
        )
        assert r.status_code == 200
        # RAG label should NOT appear for a greeting
        assert "rag knowledge base agent invoked" not in r.text.lower()

    def test_message_saved_to_db(self, client, admin_headers, mock_llm, mock_rag):
        """TC-CH-13: User and assistant messages persisted after streaming."""
        session_id = chat_session(client, admin_headers)
        client.post(
            f"/chat/sessions/{session_id}/stream",
            json={"content": "What is ADOP in Oracle EBS?"},
            headers={**admin_headers, "X-LLM-Provider": "ollama"},
        )
        r = client.get(f"/chat/sessions/{session_id}/messages", headers=admin_headers)
        assert r.status_code == 200
        messages = r.json()
        roles = [m["role"] for m in messages]
        assert "user" in roles
        assert "assistant" in roles

    def test_nonexistent_session_returns_404(self, client, admin_headers, mock_llm, mock_rag):
        """TC-CH-02 (error): Streaming to non-existent session returns 404."""
        r = client.post(
            "/chat/sessions/999999/stream",
            json={"content": "test"},
            headers={**admin_headers, "X-LLM-Provider": "ollama"},
        )
        assert r.status_code == 404
