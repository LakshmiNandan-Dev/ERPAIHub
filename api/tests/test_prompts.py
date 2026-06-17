"""TC-PROMPT — admin-editable agent prompts (registry + override CRUD)."""
import pytest
from app.core import prompts


# ── Registry unit tests (no DB needed) ──────────────────────────────────────────

class TestRegistry:

    def test_get_prompt_default_and_substitution(self):
        out = prompts.get_prompt("knowledge_base.no_context", question="HOW DO I COMPILE?")
        assert "HOW DO I COMPILE?" in out
        assert "{question}" not in out          # token substituted

    def test_replace_preserves_literal_braces(self):
        # The deployment prompt has literal { } in JSON examples — replace (not
        # str.format) must leave them intact while filling {tools}/{context}.
        out = prompts.get_prompt("deployment.system", tools="TOOLS_DOC", context="CTX")
        assert "TOOLS_DOC" in out and "CTX" in out
        assert '{"field"' in out                # literal JSON brace survived
        assert "{tools}" not in out and "{context}" not in out

    def test_missing_placeholders(self):
        assert prompts.missing_placeholders("knowledge_base.no_context", "no token") == ["question"]
        assert prompts.missing_placeholders("knowledge_base.no_context", "ok {question}") == []
        assert prompts.missing_placeholders("chat.system", "anything") == []   # no required tokens

    def test_unknown_key(self):
        assert prompts.definition("nope.nope") is None
        assert prompts.get_prompt("nope.nope") == ""


# ── Admin CRUD (via API) ─────────────────────────────────────────────────────────

class TestPromptAdmin:

    def test_list_prompts(self, client, admin_headers):
        rows = client.get("/admin/prompts", headers=admin_headers).json()
        keys = {r["key"] for r in rows}
        assert {"chat.system", "deployment.system", "performance.diagnostic"} <= keys
        chat = next(r for r in rows if r["key"] == "chat.system")
        assert chat["is_overridden"] is False
        assert chat["content"] == chat["default"]

    def test_override_and_reset_roundtrip(self, client, admin_headers):
        key = "chat.system"
        new_text = "You are a TEST EBS assistant. Be terse."
        r = client.put(f"/admin/prompts/{key}", json={"content": new_text}, headers=admin_headers)
        assert r.status_code == 200, r.text
        assert r.json()["is_overridden"] is True
        assert r.json()["content"] == new_text

        # GET reflects the override (read back through the request session)
        rows = client.get("/admin/prompts", headers=admin_headers).json()
        assert next(p for p in rows if p["key"] == key)["content"] == new_text

        # reset → back to default
        r = client.delete(f"/admin/prompts/{key}", headers=admin_headers)
        assert r.status_code == 200
        assert r.json()["is_overridden"] is False
        assert r.json()["content"] == r.json()["default"]
        rows = client.get("/admin/prompts", headers=admin_headers).json()
        assert next(p for p in rows if p["key"] == key)["is_overridden"] is False

    def test_missing_placeholder_rejected(self, client, admin_headers):
        # Dropping {question} from the no-context guard must be blocked.
        r = client.put("/admin/prompts/knowledge_base.no_context",
                       json={"content": "no placeholder here"}, headers=admin_headers)
        assert r.status_code == 400
        assert "question" in r.text

    def test_empty_content_rejected(self, client, admin_headers):
        r = client.put("/admin/prompts/chat.system", json={"content": "   "}, headers=admin_headers)
        assert r.status_code == 400

    def test_unknown_key_404(self, client, admin_headers):
        assert client.put("/admin/prompts/bogus.key", json={"content": "x"},
                          headers=admin_headers).status_code == 404

    def test_requires_admin(self, client, admin_headers):
        import uuid
        uid = uuid.uuid4().hex[:8]
        client.post("/auth/register", json={"username": f"pp_{uid}", "email": f"pp_{uid}@t.com", "password": "Pass123!"})
        # not approved/elevated → still just a 'user'; ensure non-admin is blocked
        # (login may fail if approval required; treat 401/403 as 'blocked')
        tok = client.post("/auth/login", json={"username": f"pp_{uid}", "password": "Pass123!"})
        if tok.status_code != 200:
            pytest.skip("self sign-up not auto-active in this config")
        h = {"Authorization": f"Bearer {tok.json()['session_token']}"}
        assert client.get("/admin/prompts", headers=h).status_code == 403
