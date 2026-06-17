"""TC-AU-01 to TC-AU-08 — Authentication & User Management"""
import uuid
import pytest


def _uid():
    return uuid.uuid4().hex[:8]


class TestRegistration:

    def test_successful_registration(self, client):
        """TC-AU-01: New user can register when signup is enabled."""
        uid = _uid()
        r = client.post("/auth/register", json={
            "username": f"user_{uid}",
            "email": f"user_{uid}@test.com",
            "password": "Password123!",
        })
        assert r.status_code == 201
        data = r.json()
        assert data["username"] == f"user_{uid}"
        # UserOut doesn't surface approval_status; a non-first self sign-up is created
        # inactive+pending, so is_active=False is the public-facing proof of that.
        assert data["is_active"] is False            # non-first users start pending/inactive
        assert "password_hash" not in data

    def test_duplicate_email_rejected(self, client):
        """TC-AU-02: Registration with an already-used email returns 400."""
        uid = _uid()
        payload = {"username": f"user_{uid}", "email": f"dup_{uid}@test.com", "password": "Pass123!"}
        assert client.post("/auth/register", json=payload).status_code == 201
        # Second registration with same email, different username
        r = client.post("/auth/register", json={
            "username": f"other_{uid}", "email": f"dup_{uid}@test.com", "password": "Pass123!",
        })
        assert r.status_code == 400
        assert "already registered" in r.json()["detail"].lower()

    def test_duplicate_username_rejected(self, client):
        """TC-AU-02 (variant): Duplicate username also returns 400."""
        uid = _uid()
        payload = {"username": f"user_{uid}", "email": f"a_{uid}@test.com", "password": "Pass123!"}
        assert client.post("/auth/register", json=payload).status_code == 201
        r = client.post("/auth/register", json={
            "username": f"user_{uid}", "email": f"b_{uid}@test.com", "password": "Pass123!",
        })
        assert r.status_code == 400


class TestLogin:

    def _register_and_approve(self, client, admin_headers):
        """Helper: register a user and admin-approve them."""
        uid = _uid()
        creds = {"username": f"active_{uid}", "email": f"active_{uid}@test.com", "password": "Pass123!"}
        r = client.post("/auth/register", json=creds)
        assert r.status_code == 201
        user_id = r.json()["id"]
        # Admin approves + activates the account
        client.patch(f"/admin/users/{user_id}",
                     json={"approval_status": "approved", "is_active": True},
                     headers=admin_headers)
        return creds

    def test_successful_login(self, client, admin_headers):
        """TC-AU-03: Valid credentials return a session_token."""
        creds = self._register_and_approve(client, admin_headers)
        r = client.post("/auth/login", json={"username": creds["username"], "password": creds["password"]})
        assert r.status_code == 200
        assert "session_token" in r.json()

    def test_wrong_password(self, client):
        """TC-AU-04: Wrong password returns 403."""
        uid = _uid()
        client.post("/auth/register", json={
            "username": f"user_{uid}", "email": f"user_{uid}@test.com", "password": "Pass123!"
        })
        r = client.post("/auth/login", json={"username": f"user_{uid}", "password": "WrongPass!"})
        assert r.status_code == 403
        assert "Invalid" in r.json()["detail"]

    def test_nonexistent_user(self, client):
        """TC-AU-05: Login for unknown user returns 403."""
        r = client.post("/auth/login", json={"username": "nobody_xyz", "password": "Pass123!"})
        assert r.status_code == 403

    def test_pending_user_cannot_login(self, client):
        """TC-AU-06 (variant): Pending (unapproved) users are blocked at login."""
        uid = _uid()
        client.post("/auth/register", json={
            "username": f"pending_{uid}", "email": f"pending_{uid}@test.com", "password": "Pass123!"
        })
        r = client.post("/auth/login", json={"username": f"pending_{uid}", "password": "Pass123!"})
        assert r.status_code == 403
        assert "pending" in r.json()["detail"].lower()

    def test_admin_login_succeeds(self, client):
        """TC-AU-03 (admin): The seeded admin user can log in."""
        r = client.post("/auth/login", json={"username": "admin", "password": "Admin123!"})
        assert r.status_code == 200
        assert "session_token" in r.json()


class TestSessionExpiry:

    def test_invalid_token_returns_401(self, client):
        """TC-AU-06: Requests with invalid/expired token return 401."""
        r = client.get("/chat/sessions", headers={"Authorization": "Bearer invalid-token-xyz"})
        assert r.status_code == 401

    def test_missing_token_returns_401(self, client):
        """TC-AU-01 (security): Missing auth header returns 401."""
        r = client.get("/chat/sessions")
        assert r.status_code == 401

    def test_logout_invalidates_token(self, client, admin_headers):
        """TC-AU-07: After logout, the session token is rejected."""
        # Logout takes the session_token in the body (embedded), not just the header.
        token = admin_headers["Authorization"].split()[1]
        lo = client.post("/auth/logout", json={"session_token": token}, headers=admin_headers)
        assert lo.status_code == 204
        r = client.get("/chat/sessions", headers=admin_headers)
        assert r.status_code == 401
