"""
Live execution for the patching agent's adop tools.

Resolves SSH + EBS credentials from a registered environment (APPS, SYSTEM /
EBS_SYSTEM, WebLogic, plus the linked SSH server) and runs **read-only** checks
live over SSH — `adop -status`, `opatch lspatches` (already-applied) and
`opatch prereq` (conflict). Destructive adop phases run live only when a run is
explicitly armed for live execution. Everything degrades to the simulator when
SSH or credentials are unavailable, so the agent never hard-fails.
"""
from app import models, crypto

SSH_TIMEOUT = 15.0
CMD_TIMEOUT = 600.0    # adop phases can run for many minutes


# ── Credential resolution (from the environment) ──────────────────────────────

def resolve_creds(env, db) -> dict:
    """Decrypted SSH + EBS credentials for an environment (None where unset)."""
    ssh = None
    if env.ssh_server_id:
        srv = db.query(models.SshServer).filter(models.SshServer.id == env.ssh_server_id).first()
        if srv:
            ssh = {"host": srv.hostname, "port": srv.port or 22,
                   "user": srv.username, "password": crypto.decrypt(srv.password_enc)}
    return {
        "ssh": ssh,
        "apps_user": env.db_user or "apps", "apps_pwd": crypto.decrypt(env.db_password_enc),
        "system_user": env.system_user or "system", "system_pwd": crypto.decrypt(env.system_password_enc),
        "weblogic_user": env.weblogic_user or "weblogic", "weblogic_pwd": crypto.decrypt(env.weblogic_password_enc),
        "db_host": env.db_host, "db_port": env.db_port or 1521, "db_sid": env.db_sid,
        "apps_os_user": env.apps_os_user,
    }


def readiness(env, db) -> dict:
    """What's available for live execution (no secrets leaked)."""
    c = resolve_creds(env, db)
    r = {
        "ssh": bool(c["ssh"] and c["ssh"].get("host")),
        "apps": bool(c["apps_pwd"]),
        "system": bool(c["system_pwd"]),
        "weblogic": bool(c["weblogic_pwd"]),
    }
    # Read-only `adop -status` / opatch checks need only SSH; destructive phases
    # also need the APPS (and usually WebLogic AdminServer) passwords.
    r["live_checks"] = r["ssh"]
    r["live_phases"] = r["ssh"] and r["apps"] and r["weblogic"]
    r["missing_for_phases"] = [k for k in ("ssh", "apps", "weblogic") if not r[k]]
    return r


# ── SSH plumbing ──────────────────────────────────────────────────────────────

def _connect(ssh: dict):
    import paramiko
    client = paramiko.SSHClient()
    # WarningPolicy logs unknown host keys (don't silently trust); harden with a
    # verified known_hosts + RejectPolicy for production MITM protection.
    client.set_missing_host_key_policy(paramiko.WarningPolicy())
    client.connect(hostname=ssh["host"], port=ssh["port"], username=ssh["user"],
                   password=ssh["password"], timeout=SSH_TIMEOUT)
    return client


def _run(client, command, feed=None, timeout=CMD_TIMEOUT):
    """Run a remote command; optionally feed stdin lines (adop password prompts)."""
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    if feed:
        try:
            stdin.write("\n".join(feed) + "\n")
            stdin.flush()
            stdin.channel.shutdown_write()
        except Exception:
            pass
    out = stdout.read().decode("utf-8", "ignore")
    err = stderr.read().decode("utf-8", "ignore")
    rc = stdout.channel.recv_exit_status()
    return rc, (out + ("\n" + err if err.strip() else "")).strip()


def _env_source(run_fs: str, edition: str = "run") -> str:
    return f"source {run_fs}/EBSapps.env {edition} >/dev/null 2>&1"


# ── Live read-only operations ─────────────────────────────────────────────────

def live_adop_status(creds: dict, run_fs: str) -> dict:
    """Run `adop -status` live (read-only). Returns {rc, output} or {error}."""
    if not (creds.get("ssh") and creds["ssh"].get("host")):
        return {"error": "no SSH server configured for this environment"}
    try:
        client = _connect(creds["ssh"])
        try:
            rc, out = _run(client, f"bash -lc '{_env_source(run_fs)}; adop -status'", timeout=180)
            return {"rc": rc, "output": out or "(no output)"}
        finally:
            client.close()
    except Exception as exc:
        return {"error": str(exc)[:300]}


def live_opatch_lspatches(creds: dict, home: str, patch: str) -> dict:
    """Is `patch` already present in `home`? (opatch lspatches) — read-only."""
    if not (creds.get("ssh") and creds["ssh"].get("host")):
        return {"error": "no SSH server configured"}
    try:
        client = _connect(creds["ssh"])
        try:
            rc, out = _run(client,
                f"{home}/OPatch/opatch lspatches 2>&1 | grep -F '{patch}' || true", timeout=120)
            return {"rc": rc, "present": bool(out), "output": out}
        finally:
            client.close()
    except Exception as exc:
        return {"error": str(exc)[:300]}


def live_opatch_conflict(creds: dict, home: str, patch_dir: str) -> dict:
    """opatch prereq conflict check for a home — read-only."""
    if not (creds.get("ssh") and creds["ssh"].get("host")):
        return {"error": "no SSH server configured"}
    try:
        client = _connect(creds["ssh"])
        try:
            rc, out = _run(client,
                f"{home}/OPatch/opatch prereq CheckConflictAgainstOHWithDetail -ph {patch_dir} -oh {home} 2>&1",
                timeout=180)
            return {"rc": rc, "conflict": ("conflict" in out.lower() and rc != 0), "output": out}
        finally:
            client.close()
    except Exception as exc:
        return {"error": str(exc)[:300]}


# ── Live destructive phase (opt-in) ───────────────────────────────────────────

def _adop_phase_command(phase: str, ctx: dict) -> str:
    """Concrete adop command (no ${VAR}) for a live phase run."""
    workers = ctx.get("APPLY_WORKERS", "8")
    patch = ctx.get("PATCH_NUMBER", "")
    mode = ctx.get("ADOP_MODE", "online")
    cleanup = ctx.get("CLEANUP_MODE", "standard")
    if phase == "apply":
        extra = " apply_mode=downtime" if mode == "downtime" else (" hotpatch=yes" if mode == "hotpatch" else "")
        return f"adop phase=apply patches={patch} workers={workers}{extra}"
    if phase == "cleanup":
        return f"adop phase=cleanup cleanup_mode={cleanup}"
    return f"adop phase={phase}"


def live_adop_phase(creds: dict, run_fs: str, phase: str, ctx: dict) -> dict:
    """Run a single adop phase live over SSH, feeding the prompted passwords.
    Returns {rc, output, command} or {error}. The prompt ORDER fed here is the
    common 12.2 default (APPS, then WebLogic AdminServer, then SYSTEM) — tune per
    site/version if your phase prompts differ."""
    if not (creds.get("ssh") and creds["ssh"].get("host")):
        return {"error": "no SSH server configured for this environment"}
    feed = [p for p in (creds.get("apps_pwd"), creds.get("weblogic_pwd"), creds.get("system_pwd")) if p]
    cmd = _adop_phase_command(phase, ctx)
    full = f"bash -lc '{_env_source(run_fs)}; {cmd}'"
    try:
        client = _connect(creds["ssh"])
        try:
            rc, out = _run(client, full, feed=feed, timeout=CMD_TIMEOUT)
            return {"rc": rc, "output": out or "(no output)", "command": cmd}
        finally:
            client.close()
    except Exception as exc:
        return {"error": str(exc)[:300], "command": cmd}
