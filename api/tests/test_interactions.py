"""TC-IL-01..06 — Interaction audit trail (per-interaction monitoring log).

Covers the durable InteractionLog: telemetry capture, the admin monitoring
list/detail endpoints (with filters + access control), and feedback sync.
"""
from app.core import telemetry
from app import models


def _record(**kw):
    """Append one interaction-audit row with sensible defaults (override via kw)."""
    base = dict(
        user_id=None, username="admin", session_id=None, message_id=None,
        query="What is adop?", response="adop is online patching.",
        rag_context=None, rag_chunks=0, grounded=False,
        provider="ollama", model="llama3", prompt_key="chat.system",
        prompt_tokens=10, completion_tokens=20, latency_ms=123,
    )
    base.update(kw)
    telemetry.record_interaction(**base)


class TestInteractionLog:

    def test_record_and_list(self, db_session, client, admin_headers):
        """TC-IL-01: a recorded interaction appears in the monitoring list."""
        _record(query="Explain ADOP cutover", response="cutover finalizes patching")
        r = client.get("/admin/monitoring/interactions", headers=admin_headers)
        assert r.status_code == 200
        body = r.json()
        assert body["total"] >= 1
        assert any("ADOP cutover" in i["query"] for i in body["interactions"])

    def test_detail_returns_full_text(self, db_session, client, admin_headers):
        """TC-IL-02: detail endpoint returns the untruncated context/response."""
        ctx = "SOURCE #1\n\n---\n\n" + ("x" * 500)
        _record(query="grounded q", response="y" * 500, rag_context=ctx,
                rag_chunks=2, grounded=True)
        lst = client.get("/admin/monitoring/interactions", headers=admin_headers).json()
        iid = lst["interactions"][0]["id"]
        # List view truncates / omits the full context...
        assert lst["interactions"][0]["rag_context"] is None
        # ...the detail view returns everything verbatim for reproduction.
        r = client.get(f"/admin/monitoring/interactions/{iid}", headers=admin_headers)
        assert r.status_code == 200
        d = r.json()
        assert d["rag_context"] == ctx
        assert len(d["response"]) == 500
        assert d["grounded"] is True and d["rag_chunks"] == 2
        assert d["prompt_key"] == "chat.system" and d["prompt_version"]

    def test_detail_404(self, client, admin_headers):
        """TC-IL-03: an unknown interaction id returns 404."""
        r = client.get("/admin/monitoring/interactions/99999999", headers=admin_headers)
        assert r.status_code == 404

    def test_requires_admin(self, client, regular_user_headers):
        """TC-IL-04: a non-admin cannot read the audit trail."""
        r = client.get("/admin/monitoring/interactions", headers=regular_user_headers)
        assert r.status_code in (401, 403)

    def test_grounded_filter(self, db_session, client, admin_headers):
        """TC-IL-05: grounded=true returns only grounded interactions."""
        _record(query="grounded one", grounded=True, rag_chunks=1, rag_context="ctx")
        _record(query="abstained one", grounded=False)
        r = client.get("/admin/monitoring/interactions?grounded=true", headers=admin_headers)
        assert r.status_code == 200
        items = r.json()["interactions"]
        assert items and all(i["grounded"] for i in items)

    def test_feedback_syncs_onto_log(self, db_session, client, admin_headers):
        """TC-IL-06: feedback is denormalized onto the interaction-audit row."""
        from conftest import chat_session
        session_id = chat_session(client, admin_headers)
        msg = models.ChatMessage(session_id=session_id, role="assistant",
                                 content="answer text")
        db_session.add(msg)
        db_session.commit()

        _record(message_id=msg.id, query="rate me", session_id=session_id)
        telemetry.update_interaction_feedback(msg.id, 1)

        lst = client.get("/admin/monitoring/interactions", headers=admin_headers).json()
        row = next(i for i in lst["interactions"] if i["message_id"] == msg.id)
        assert row["feedback_rating"] == 1
