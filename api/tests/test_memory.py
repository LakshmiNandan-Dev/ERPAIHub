"""TC-MC-01 to TC-MC-08 — History Compression & Memory"""
import pytest
from unittest.mock import patch, AsyncMock
from conftest import chat_session, parse_sse


def _send(client, session_id, content, headers):
    return client.post(
        f"/chat/sessions/{session_id}/stream",
        json={"content": content},
        headers={**headers, "X-LLM-Provider": "ollama"},
    )


class TestHistoryCompression:

    def test_no_compression_for_short_sessions(self, client, admin_headers, mock_llm, mock_rag, db_session):
        """TC-MC-01: Sessions with ≤40 messages do not trigger summarization."""
        from app import models
        session_id = chat_session(client, admin_headers)

        # Send 5 messages — well below the 40-message threshold
        for i in range(5):
            _send(client, session_id, f"EBS question number {i}", admin_headers)

        # context_summary should still be NULL
        sess = db_session.query(models.ChatSession).filter(
            models.ChatSession.id == session_id
        ).first()
        assert sess.context_summary is None
        assert sess.summarized_through_id is None

    def test_compression_triggers_at_threshold(self, client, admin_headers, mock_llm, mock_rag, db_session):
        """TC-MC-02: Compression triggers when message count exceeds 40."""
        from app import models
        session_id = chat_session(client, admin_headers)

        # Pre-populate 42 messages directly in the DB to simulate a long session
        for i in range(42):
            db_session.add(models.ChatMessage(
                session_id=session_id,
                role="user" if i % 2 == 0 else "assistant",
                content=f"Message {i} about ADOP patching and DEV environment with sqlplus.",
            ))
        db_session.flush()

        # The next API call should trigger compression
        _send(client, session_id, "What should I do next?", admin_headers)

        db_session.expire_all()
        sess = db_session.query(models.ChatSession).filter(
            models.ChatSession.id == session_id
        ).first()
        # Summary should now be populated
        assert sess.context_summary is not None or sess.summarized_through_id is not None

    def test_key_facts_extracted_environment(self, client, admin_headers, mock_llm, mock_rag, db_session):
        """TC-MC-03: Rule-based extractor picks up EBS environment names."""
        from app.routers.chat import _extract_key_facts
        from app import models

        # Simulate messages that mention environments
        msgs = [
            models.ChatMessage(session_id=1, role="user",
                               content="Deploy to UAT environment, also need to check DEV and PROD."),
            models.ChatMessage(session_id=1, role="assistant",
                               content="Deploying to UAT now. DEV is already patched."),
        ]
        result = _extract_key_facts(msgs)
        assert "DEV" in result or "UAT" in result or "PROD" in result

    def test_key_facts_extracted_files_and_errors(self, client, admin_headers, mock_llm, mock_rag, db_session):
        """TC-MC-04: Rule-based extractor captures file names and ORA/PLS errors."""
        from app.routers.chat import _extract_key_facts
        from app import models

        msgs = [
            models.ChatMessage(session_id=1, role="user",
                               content="I'm getting ORA-00942 compiling xxap_supplier_pkg.pls"),
            models.ChatMessage(session_id=1, role="assistant",
                               content="The error PLS-00201 means table is missing. Use fndload for xxcustom.ldt"),
        ]
        result = _extract_key_facts(msgs)
        assert "xxap_supplier_pkg.pls" in result.lower() or "pls" in result.lower()
        assert "ORA-00942" in result or "PLS-00201" in result

    def test_key_facts_empty_for_no_entities(self):
        """TC-MC-03 (negative): Returns empty string when no EBS entities found."""
        from app.routers.chat import _extract_key_facts
        from app import models

        msgs = [models.ChatMessage(session_id=1, role="user", content="Hello, how are you?")]
        assert _extract_key_facts(msgs) == ""

    def test_context_block_injected_when_compressed(self, client, admin_headers, mock_rag, db_session):
        """TC-MC-07: When summary exists, [EARLIER CONVERSATION CONTEXT] block appears in LLM messages."""
        from app.routers.chat import _build_ollama_messages
        from app import models

        # Create 25 messages (more than _MAX_HISTORY_MESSAGES=20)
        msgs = [
            models.ChatMessage(session_id=1, role="user" if i % 2 == 0 else "assistant",
                               content=f"Content {i}")
            for i in range(25)
        ]
        context_block = "**Auto-extracted EBS context:**\n- Environments: DEV, UAT"
        result = _build_ollama_messages(msgs, "latest question", "", context_block)

        # Find the context injection
        contents = [m["content"] for m in result]
        assert any("EARLIER CONVERSATION CONTEXT" in c for c in contents)

    def test_sliding_window_trims_messages(self):
        """TC-MC-01 (window): _build_ollama_messages trims to last 20 messages."""
        from app.routers.chat import _build_ollama_messages, _MAX_HISTORY_MESSAGES
        from app import models

        msgs = [
            models.ChatMessage(session_id=1, role="user" if i % 2 == 0 else "assistant",
                               content=f"Message {i}")
            for i in range(50)
        ]
        result = _build_ollama_messages(msgs, "q", "", "")
        # system prompt (1) + user/assistant messages should total ≤ 1 + _MAX_HISTORY_MESSAGES
        non_system = [m for m in result if m["role"] != "system"]
        assert len(non_system) <= _MAX_HISTORY_MESSAGES
