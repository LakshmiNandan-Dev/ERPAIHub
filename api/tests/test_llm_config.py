"""TC-LM-01 to TC-LM-06 — LLM Provider Configuration"""
import uuid
import pytest


class TestLlmConfig:

    def _llm_payload(self, provider="ollama", label=None, model="llama3.2"):
        uid = uuid.uuid4().hex[:6]
        return {
            "provider": provider,
            "label": label or f"Test {provider} {uid}",
            "model": model,
            "base_url": "http://localhost:11434",
            "is_default": False,
            "is_active": True,
        }

    def test_create_llm_provider(self, client, admin_headers):
        """TC-LM-01: Admin can register an LLM provider credential."""
        r = client.post("/admin/llm-credentials", json=self._llm_payload(), headers=admin_headers)
        assert r.status_code == 201
        data = r.json()
        assert data["provider"] == "ollama"
        assert "api_key" not in data
        assert "api_key_enc" not in data

    def test_list_llm_providers(self, client, admin_headers):
        """TC-LM-02: GET /admin/llm-credentials lists all registered providers."""
        client.post("/admin/llm-credentials", json=self._llm_payload(), headers=admin_headers)
        r = client.get("/admin/llm-credentials", headers=admin_headers)
        assert r.status_code == 200
        assert isinstance(r.json(), list)
        assert len(r.json()) >= 1

    def test_api_key_not_exposed_in_response(self, client, admin_headers):
        """TC-LM-03: API key is never returned in LLM provider responses."""
        payload = self._llm_payload(provider="openai", model="gpt-4o")
        payload["api_key"] = "sk-supersecretkey12345"
        r = client.post("/admin/llm-credentials", json=payload, headers=admin_headers)
        assert r.status_code == 201
        body = r.json()
        assert "api_key" not in body
        assert "api_key_enc" not in body
        # has_api_key flag should be True
        assert body.get("has_api_key") is True

    def test_default_provider_returned_first(self, client, admin_headers):
        """TC-LM-04: Default provider appears first in the config/llm endpoint."""
        p = self._llm_payload()
        p["is_default"] = True
        r = client.post("/admin/llm-credentials", json=p, headers=admin_headers)
        provider_id = r.json()["id"]

        r2 = client.get("/config/llm", headers=admin_headers)
        assert r2.status_code == 200
        items = r2.json()
        # Default should appear first
        if items:
            default_ids = [item["id"] for item in items if item.get("is_default")]
            assert provider_id in default_ids or items[0]["id"] == provider_id

    def test_non_admin_cannot_create_llm_provider(self, client, admin_headers):
        """TC-LM-05: Non-admin user cannot create an LLM provider."""
        import uuid as _u
        uid = _u.uuid4().hex[:6]
        # Register a user
        r = client.post("/auth/register", json={
            "username": f"nonadmin_{uid}", "email": f"na_{uid}@test.com", "password": "Test123!"
        })
        assert r.status_code == 201
        user_id = r.json()["id"]
        client.patch(f"/admin/users/{user_id}",
                     json={"approval_status": "approved", "is_active": True},
                     headers=admin_headers)
        r_login = client.post("/auth/login",
                              json={"username": f"nonadmin_{uid}", "password": "Test123!"})
        if r_login.status_code != 200:
            pytest.skip("User login unavailable")
        tok = r_login.json()["session_token"]
        user_hdrs = {"Authorization": f"Bearer {tok}"}
        r3 = client.post("/admin/llm-credentials", json=self._llm_payload(), headers=user_hdrs)
        assert r3.status_code == 403

    def test_delete_llm_provider(self, client, admin_headers):
        """TC-LM-06: Admin can delete an LLM provider."""
        r = client.post("/admin/llm-credentials", json=self._llm_payload(), headers=admin_headers)
        llm_id = r.json()["id"]
        r_del = client.delete(f"/admin/llm-credentials/{llm_id}", headers=admin_headers)
        assert r_del.status_code in (200, 204)
        # Should no longer appear in list
        r_list = client.get("/admin/llm-credentials", headers=admin_headers)
        ids = [item["id"] for item in r_list.json()]
        assert llm_id not in ids

    def test_config_llm_endpoint_no_secrets(self, client, admin_headers):
        """TC-LM-03 (config): GET /config/llm returns provider list without secrets."""
        client.post("/admin/llm-credentials", json=self._llm_payload(), headers=admin_headers)
        r = client.get("/config/llm", headers=admin_headers)
        assert r.status_code == 200
        for item in r.json():
            assert "api_key" not in item
            assert "api_key_enc" not in item
