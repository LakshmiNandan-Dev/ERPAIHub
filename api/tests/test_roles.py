"""TC-RBAC — Role definitions & per-agent permissions (Admin Console)."""
import uuid
import pytest


def _new_role(client, admin_headers, agent_names, name=None):
    name = name or f"role_{uuid.uuid4().hex[:8]}"
    r = client.post("/admin/roles",
                    json={"name": name, "description": "test role", "agent_names": agent_names},
                    headers=admin_headers)
    assert r.status_code == 201, r.text
    return r.json()


def _make_user(client, admin_headers):
    """Register + approve a plain user; return (user_id, headers)."""
    uid = uuid.uuid4().hex[:8]
    creds = {"username": f"u_{uid}", "email": f"u_{uid}@test.com", "password": "Pass123!"}
    r = client.post("/auth/register", json=creds)
    assert r.status_code == 201, r.text
    user_id = r.json()["id"]
    client.patch(f"/admin/users/{user_id}",
                 json={"approval_status": "approved", "is_active": True},
                 headers=admin_headers)
    tok = client.post("/auth/login", json={"username": creds["username"], "password": creds["password"]})
    assert tok.status_code == 200, tok.text
    return user_id, {"Authorization": f"Bearer {tok.json()['session_token']}"}


class TestRoleCrud:

    def test_role_crud_roundtrip(self, client, admin_headers):
        """Create → list → patch grants → delete."""
        role = _new_role(client, admin_headers, ["patching"])
        assert [a["name"] for a in role["agents"]] == ["patching"]
        assert role["user_count"] == 0

        listed = client.get("/admin/roles", headers=admin_headers).json()
        assert any(x["id"] == role["id"] for x in listed)

        # Re-grant: replace agents with cloning+patching
        r = client.patch(f"/admin/roles/{role['id']}",
                         json={"agent_names": ["cloning", "patching"]}, headers=admin_headers)
        assert r.status_code == 200, r.text
        assert sorted(a["name"] for a in r.json()["agents"]) == ["cloning", "patching"]

        r = client.delete(f"/admin/roles/{role['id']}", headers=admin_headers)
        assert r.status_code == 204
        listed = client.get("/admin/roles", headers=admin_headers).json()
        assert not any(x["id"] == role["id"] for x in listed)

    def test_unknown_agent_rejected(self, client, admin_headers):
        r = client.post("/admin/roles",
                        json={"name": f"bad_{uuid.uuid4().hex[:6]}", "agent_names": ["not_an_agent"]},
                        headers=admin_headers)
        assert r.status_code == 400

    def test_duplicate_role_name_conflict(self, client, admin_headers):
        name = f"dup_{uuid.uuid4().hex[:6]}"
        _new_role(client, admin_headers, [], name=name)
        r = client.post("/admin/roles", json={"name": name, "agent_names": []}, headers=admin_headers)
        assert r.status_code == 409

    def test_gated_agents_catalog(self, client, admin_headers):
        names = {a["name"] for a in client.get("/admin/agents", headers=admin_headers).json()}
        assert names == {"deployment", "performance", "cloning", "patching"}

    def test_roles_require_admin(self, client, admin_headers):
        """A non-admin user cannot manage roles."""
        _, headers = _make_user(client, admin_headers)
        assert client.get("/admin/roles", headers=headers).status_code == 403
        assert client.post("/admin/roles", json={"name": "x", "agent_names": []},
                           headers=headers).status_code == 403


class TestAgentPermissions:

    def test_admin_has_all_agents(self, client, admin_headers):
        me = client.get("/auth/getuser", headers=admin_headers).json()
        assert sorted(me["allowed_agents"]) == ["cloning", "deployment", "patching", "performance"]

    def test_user_without_role_has_no_agents(self, client, admin_headers):
        _, headers = _make_user(client, admin_headers)
        me = client.get("/auth/getuser", headers=headers).json()
        assert me["allowed_agents"] == []
        # And the gated route is blocked.
        assert client.post("/cloning/agent", json={"context": {}}, headers=headers).status_code == 403

    def test_role_grant_enforces_per_agent(self, client, admin_headers):
        """A user granted only 'cloning' can reach cloning but not patching."""
        role = _new_role(client, admin_headers, ["cloning"])
        user_id, headers = _make_user(client, admin_headers)

        r = client.patch(f"/admin/users/{user_id}", json={"role_ids": [role["id"]]}, headers=admin_headers)
        assert r.status_code == 200, r.text
        assert [x["name"] for x in r.json()["roles"]] == [role["name"]]

        # getuser reflects the grant
        me = client.get("/auth/getuser", headers=headers).json()
        assert me["allowed_agents"] == ["cloning"]

        # Granted agent: not blocked by the guard (any non-403 outcome is fine).
        assert client.post("/cloning/agent", json={"context": {}}, headers=headers).status_code != 403
        # Ungranted agent: blocked.
        assert client.post("/patching/agent", json={"context": {}}, headers=headers).status_code == 403

    def test_create_user_with_role_ids(self, client, admin_headers):
        role = _new_role(client, admin_headers, ["performance"])
        uid = uuid.uuid4().hex[:8]
        r = client.post("/admin/users", json={
            "username": f"p_{uid}", "email": f"p_{uid}@test.com",
            "password": "Pass123!", "role": "user", "role_ids": [role["id"]],
        }, headers=admin_headers)
        assert r.status_code == 201, r.text
        assert [x["name"] for x in r.json()["roles"]] == [role["name"]]

    def test_unknown_role_id_rejected(self, client, admin_headers):
        user_id, _ = _make_user(client, admin_headers)
        r = client.patch(f"/admin/users/{user_id}", json={"role_ids": [999999]}, headers=admin_headers)
        assert r.status_code == 400
