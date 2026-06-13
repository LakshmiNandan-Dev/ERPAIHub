"""
Deployment artifact generator.

For a (successful) deployment run, build a portable, self-contained bundle the
user can copy elsewhere and run by hand:

    oraebs_deploy_<id>_<ENV>/
        deploy.sh          runnable script — full step sequence, passwords as variables
        README.md          how to set the password variables and run it
        manifest.json      machine-readable run metadata + steps
        execution_log.txt  the log output captured from the original successful run

The apps schema password is NEVER embedded — it is referenced as the required
environment variable APPS_PWD. Host/port/SID default to the source environment's
(non-secret) values but are overridable so the script can target another instance.
"""
import io
import re
import json
import zipfile
from datetime import datetime, timezone

# Credential patterns rewritten to shell variables
_CRED_TOOL_RE = re.compile(r'(?i)\b(sqlplus|fndload|wfload)\s+\S+/\S*(?:@\S+)?')
_USERID_RE    = re.compile(r'(?i)\bUserid=\S+/\S+')
_PWD_FLAG_RE  = re.compile(r'(?i)-password\s+\S+')
_USER_FLAG_RE = re.compile(r'(?i)-username\s+\S+')


def _parameterize(cmd: str) -> str:
    """Replace embedded apps credentials in a command with shell variables."""
    cmd = _CRED_TOOL_RE.sub(lambda m: f"{m.group(1)} ${{CONN}}", cmd)
    cmd = _USERID_RE.sub("Userid=${APPS_USER}/${APPS_PWD}", cmd)
    cmd = _PWD_FLAG_RE.sub("-password ${APPS_PWD}", cmd)
    cmd = _USER_FLAG_RE.sub("-username ${APPS_USER}", cmd)
    return cmd


def _step_command(step) -> str:
    """Return the runnable, parameterized shell command for a step."""
    raw = (step.command or "").strip()
    if step.execution_type == "plsql_package":
        # Bare PL/SQL — wrap in a SQL*Plus heredoc that fails on error.
        return (
            'sqlplus -s "${CONN}" <<\'SQL\'\n'
            "WHENEVER SQLERROR EXIT FAILURE\n"
            f"{raw}\n"
            "/\n"
            "SHOW ERRORS\n"
            "EXIT\n"
            "SQL"
        )
    return _parameterize(raw)


def _safe(name: str) -> str:
    return re.sub(r'[^A-Za-z0-9._-]', '_', str(name or ''))


def build_deploy_sh(deployment, steps) -> str:
    env = deployment.target_instance or "TARGET"
    total = len(steps)
    git = bool(deployment.git_repo_url)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines = []
    lines.append("#!/usr/bin/env bash")
    lines.append("#")
    lines.append(f"# OraEBS Agent — generated deployment artifact")
    lines.append(f"# Deployment run : #{deployment.id}")
    lines.append(f"# Source         : {deployment.source_doc_name or 'Deployment'}")
    lines.append(f"# Original target : {env}")
    lines.append(f"# Generated       : {ts}")
    lines.append("#")
    lines.append("# Run this on the Oracle EBS application tier with the environment sourced")
    lines.append("# (e.g.  . $APPL_TOP/APPSORA.env ). Set the password variable first:")
    lines.append("#     export APPS_PWD='your-apps-password'")
    lines.append("#     ./deploy.sh")
    lines.append("")
    lines.append("set -euo pipefail")
    lines.append("")
    lines.append("# ── Connection parameters (override via environment) ────────────────────────")
    lines.append('APPS_USER="${APPS_USER:-%s}"' % (deployment.db_user or "apps"))
    lines.append('APPS_PWD="${APPS_PWD:?Set APPS_PWD to the APPS schema password, e.g. export APPS_PWD=secret}"')
    lines.append('DB_HOST="${DB_HOST:-%s}"' % (deployment.db_host or ""))
    lines.append('DB_PORT="${DB_PORT:-%s}"' % (deployment.db_port or 1521))
    lines.append('DB_SID="${DB_SID:-%s}"' % (deployment.db_sid or ""))
    lines.append('# SQL*Plus / FNDLOAD / WFLOAD connect string')
    lines.append('CONN="${APPS_USER}/${APPS_PWD}@//${DB_HOST}:${DB_PORT}/${DB_SID}"')
    lines.append("")
    if git:
        lines.append("# ── Git source (override via environment) ───────────────────────────────────")
        lines.append('GIT_URL="${GIT_URL:-%s}"' % (deployment.git_repo_url or ""))
        lines.append('GIT_BRANCH="${GIT_BRANCH:-%s}"' % (deployment.git_branch or "main"))
        lines.append('GIT_TOKEN="${GIT_TOKEN:-}"   # set for private repositories')
        lines.append("")
    lines.append('log(){ echo; echo "=== [$(date "+%F %T")] $* ==="; }')
    lines.append("")
    lines.append('SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"')
    lines.append('cd "$SCRIPT_DIR"')
    lines.append("")
    if git:
        lines.append("# ── Fetch source files from Git ─────────────────────────────────────────────")
        lines.append('if [ -n "$GIT_URL" ]; then')
        lines.append('  log "Cloning $GIT_URL ($GIT_BRANCH)"')
        lines.append('  CLONE_URL="$GIT_URL"')
        lines.append('  if [ -n "$GIT_TOKEN" ]; then CLONE_URL="${GIT_URL/https:\\/\\//https://oauth2:${GIT_TOKEN}@}"; fi')
        lines.append('  rm -rf ./src && git clone --depth 1 --branch "$GIT_BRANCH" "$CLONE_URL" ./src')
        lines.append('  cd ./src')
        lines.append("fi")
        lines.append("")
    else:
        lines.append("# NOTE: place the referenced files (see README) next to this script before running.")
        lines.append("")
    lines.append("# ── Steps ───────────────────────────────────────────────────────────────────")
    for i, s in enumerate(steps, 1):
        lines.append(f'log "Step {i}/{total} — {_safe(s.file_name)}  [{s.execution_type}]"')
        lines.append(_step_command(s))
        lines.append("")
    lines.append('log "Deployment artifact completed successfully."')
    lines.append("")
    return "\n".join(lines)


def build_readme(deployment, steps) -> str:
    env = deployment.target_instance or "TARGET"
    files = "\n".join(f"- `{s.file_name}`  ({s.execution_type})" for s in steps) or "- (none)"
    git = bool(deployment.git_repo_url)
    src_note = (
        f"`deploy.sh` clones the source automatically from `{deployment.git_repo_url}` "
        f"(branch `{deployment.git_branch or 'main'}`). For a private repo, `export GIT_TOKEN=...`."
        if git else
        "Place the referenced files (listed below) in this directory next to `deploy.sh` before running."
    )
    return (
        f"# Deployment Artifact — #{deployment.id} ({env})\n\n"
        f"Generated by OraEBS Agent from deployment run #{deployment.id} "
        f"(source: {deployment.source_doc_name or 'Deployment'}).\n\n"
        "## Contents\n"
        "- `deploy.sh` — runnable script with the full step sequence; the APPS password is a variable, never embedded.\n"
        "- `manifest.json` — machine-readable run metadata and steps.\n"
        "- `execution_log.txt` — log output captured from the original successful run (reference).\n\n"
        "## How to run on another instance\n"
        "1. Copy this directory to the target Oracle EBS application tier.\n"
        "2. Source the EBS environment, e.g. `. $APPL_TOP/APPSORA.env`.\n"
        "3. Set the connection parameters (only the password is mandatory):\n"
        "   ```bash\n"
        "   export APPS_PWD='your-apps-password'\n"
        "   export DB_HOST=... DB_PORT=1521 DB_SID=...   # override the source defaults if targeting another DB\n"
        "   ```\n"
        f"4. {src_note}\n"
        "5. Run it:\n"
        "   ```bash\n"
        "   chmod +x deploy.sh && ./deploy.sh\n"
        "   ```\n\n"
        "The script uses `set -euo pipefail`, so it stops at the first failing step.\n\n"
        "## Files referenced\n"
        f"{files}\n"
    )


def build_manifest(deployment, steps) -> dict:
    return {
        "deployment_id": deployment.id,
        "source": deployment.source_doc_name,
        "original_target": deployment.target_instance,
        "git_repo_url": deployment.git_repo_url,
        "git_branch": deployment.git_branch,
        "status": deployment.status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "steps": [
            {
                "step_number": s.step_number,
                "file_name": s.file_name,
                "execution_type": s.execution_type,
                "command": s.command,
                "status": s.status,
            }
            for s in steps
        ],
    }


def build_artifact_zip(deployment, steps) -> tuple[bytes, str]:
    """Return (zip_bytes, suggested_filename)."""
    steps = sorted(steps, key=lambda s: s.step_number)
    root = f"oraebs_deploy_{deployment.id}_{_safe(deployment.target_instance)}"

    deploy_sh = build_deploy_sh(deployment, steps)
    readme = build_readme(deployment, steps)
    manifest = json.dumps(build_manifest(deployment, steps), indent=2)
    exec_log = "\n\n".join(
        f"===== Step {s.step_number}: {s.file_name} [{s.status}] =====\n{(s.log_output or '').strip()}"
        for s in steps
    ) or "(no log output captured)"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        # deploy.sh marked executable (0755)
        info = zipfile.ZipInfo(f"{root}/deploy.sh")
        info.external_attr = 0o100755 << 16
        z.writestr(info, deploy_sh)
        z.writestr(f"{root}/README.md", readme)
        z.writestr(f"{root}/manifest.json", manifest)
        z.writestr(f"{root}/execution_log.txt", exec_log)

    return buf.getvalue(), f"{root}.zip"
