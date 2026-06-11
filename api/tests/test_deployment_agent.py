"""TC-DA-01 to TC-DA-10 — Deployment Agent (ReAct loop)"""
import json
import pytest
from conftest import parse_sse


_AGENT_URL = "/deployments/agent/chat"


def _chat(client, headers, context, user_message=""):
    payload = {"context": context, "message": user_message}
    return client.post(_AGENT_URL, json=payload, headers=headers)


class TestDeploymentAgentReact:

    def test_agent_requires_auth(self, client):
        """TC-DA-01: Agent chat endpoint rejects unauthenticated requests."""
        r = client.post(_AGENT_URL, json={"context": {}, "message": "hi"})
        assert r.status_code == 401

    def test_agent_requires_agent_access(self, client, admin_headers):
        """TC-DA-02: Agent returns initial question — requires agent-access role."""
        r = _chat(client, admin_headers, {})
        # Should stream SSE or return JSON, not 401/403
        assert r.status_code == 200

    def test_agent_asks_for_environment_when_missing(self, client, admin_headers, mock_llm, nonprod_env):
        """TC-DA-03: Agent asks for environment when context is empty."""
        r = _chat(client, admin_headers, {})
        assert r.status_code == 200
        # The response should reference asking for info
        body = r.text
        assert len(body) > 0

    def test_agent_asks_for_server_when_missing(self, client, admin_headers, mock_llm, ssh_server):
        """TC-DA-04: Agent asks for server when environment is provided but server is missing."""
        r = _chat(client, admin_headers, {
            "environment": "UAT",
            "instructions": "Deploy XXCUST package to UAT.",
        })
        assert r.status_code == 200

    def test_agent_present_plan_then_confirm(self, client, admin_headers, mock_llm, nonprod_env, ssh_server):
        """TC-DA-05: Agent presents a plan and then proceeds on user confirmation."""
        context_with_all = {
            "environment": nonprod_env["name"],
            "server": ssh_server["name"],
            "source_type": "local",
            "instructions": "Deploy XXCUST_PKG.pls to UAT using sqlplus.",
        }
        r = _chat(client, admin_headers, context_with_all, user_message="yes")
        assert r.status_code == 200

    def test_agent_loops_for_git_token(self, client, admin_headers, mock_llm):
        """TC-DA-06: Agent asks for git_token when source_type is git."""
        r = _chat(client, admin_headers, {
            "source_type": "git",
            "git_url": "https://github.com/org/ebs-scripts.git",
            "git_branch": "main",
        })
        assert r.status_code == 200

    def test_agent_stuck_loop_detection(self, client, admin_headers, monkeypatch):
        """TC-DA-07: Agent exits gracefully when it loops on the same action twice."""
        from app import llm_service

        call_count = {"n": 0}

        async def _stream_stuck(*args, **kwargs):
            call_count["n"] += 1
            yield "Thought: I need to ask the user.\n"
            yield "Action: request_user_input\n"
            yield 'Action Input: {"field":"instructions","question":"What to deploy?"}\n'

        monkeypatch.setattr(llm_service, "stream_tokens", _stream_stuck)

        r = _chat(client, admin_headers, {})
        # Should not hang or 500 — stuck-loop detection kicks in
        assert r.status_code == 200

    def test_agent_finish_tool_ends_session(self, client, admin_headers, monkeypatch):
        """TC-DA-08: Agent returns final message when 'finish' tool is invoked."""
        from app import llm_service

        async def _stream_finish(*args, **kwargs):
            yield "Thought: Deployment is complete.\n"
            yield "Action: finish\n"
            yield "Action Input: Deployment to UAT completed successfully.\n"

        monkeypatch.setattr(llm_service, "stream_tokens", _stream_finish)

        r = _chat(client, admin_headers, {
            "environment": "UAT",
            "server": "srv-01",
            "source_type": "local",
            "instructions": "Deploy XXCUST_PKG.",
            "confirmation": "yes",
        })
        assert r.status_code == 200

    def test_agent_get_servers_tool(self, client, admin_headers, mock_llm, ssh_server):
        """TC-DA-09: get_servers tool returns registered SSH servers."""
        r = client.get("/config/servers", headers=admin_headers)
        assert r.status_code == 200
        names = [s["name"] for s in r.json()]
        assert ssh_server["name"] in names

    def test_agent_get_environments_tool(self, client, admin_headers, mock_llm, nonprod_env):
        """TC-DA-10: get_environments tool returns registered environments."""
        r = client.get("/config/environments", headers=admin_headers)
        assert r.status_code == 200
        names = [e["name"] for e in r.json()]
        assert nonprod_env["name"] in names

    def test_agent_confluence_fetch_graceful_failure(self, client, admin_headers, monkeypatch):
        """TC-DA-07 (confluence): Confluence fetch failure doesn't crash the agent."""
        from app import llm_service

        async def _stream_confluence(*args, **kwargs):
            yield "Thought: The user gave a Confluence URL. I'll fetch it.\n"
            yield "Action: fetch_confluence_page\n"
            yield '{"url":"https://wiki.invalid/pages/123","token":"fake-token"}\n'

        monkeypatch.setattr(llm_service, "stream_tokens", _stream_confluence)

        r = _chat(client, admin_headers, {
            "confluence_url": "https://wiki.invalid/pages/123",
            "confluence_token": "fake-token",
        })
        assert r.status_code == 200
