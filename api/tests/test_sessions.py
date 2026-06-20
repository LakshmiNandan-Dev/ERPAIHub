"""TC-SM-01 to TC-SM-04 — Session Management"""
import uuid
import pytest
from conftest import chat_session, _login


class TestSessionIsolation:

    def test_multiple_sessions_per_user(self, client, admin_headers):
        """TC-SM-01: Multiple sessions can exist for a single user."""
        ids = [chat_session(client, admin_headers) for _ in range(3)]
        r = client.get("/chat/sessions", headers=admin_headers)
        listed_ids = [s["id"] for s in r.json()]
        for sid in ids:
            assert sid in listed_ids

    def test_sessions_isolated_between_users(self, client, admin_headers, admin2_headers):
        """TC-SM-02: User B cannot see User A's sessions."""
        sid = chat_session(client, admin_headers)
        r = client.get("/chat/sessions", headers=admin2_headers)
        other_ids = [s["id"] for s in r.json()]
        assert sid not in other_ids

    def test_sessions_ordered_newest_first(self, client, admin_headers, db_session):
        """TC-SM-03: GET /chat/sessions returns sessions in descending created_at order."""
        from datetime import datetime, timedelta, timezone
        from app import models

        s1 = chat_session(client, admin_headers)
        s2 = chat_session(client, admin_headers)
        # Inside the test's single DB transaction every row shares the same now()
        # default, so back-date s1 to actually exercise the descending-order query.
        row1 = db_session.get(models.ChatSession, s1)
        row1.created_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        db_session.flush()

        r = client.get("/chat/sessions", headers=admin_headers)
        ids = [s["id"] for s in r.json()]
        # s2 was created later so it should appear before s1
        assert ids.index(s2) < ids.index(s1)

    def test_rename_empty_title_rejected(self, client, admin_headers):
        """TC-SM-04: PATCH session with empty title returns 422."""
        sid = chat_session(client, admin_headers)
        r = client.patch(f"/chat/sessions/{sid}", json={"title": ""}, headers=admin_headers)
        assert r.status_code == 422

    def test_cannot_access_another_users_session(self, client, admin_headers, admin2_headers):
        """TC-SC-02: User B gets 404 accessing User A's session."""
        sid = chat_session(client, admin_headers)
        r = client.get(f"/chat/sessions/{sid}/messages", headers=admin2_headers)
        assert r.status_code == 404
