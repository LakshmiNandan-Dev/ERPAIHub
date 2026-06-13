"""
Production guard for the cloning agent.

You cannot trust a label: someone may register a real production database under a
non-prod name. So we identify production from what the database *actually is*:

  1. Registry tier        — environment classified PROD by an admin (locked, audited).
  2. Intrinsic identity    — v$database.DBID / GLOBAL_NAME match a known PROD database
                             (DBID survives renames and cannot be faked by a label).
  3. Behavioural signals   — live EBS "production smell": connected users (ICX_SESSIONS),
                             running/pending concurrent requests, recent distinct logins,
                             named DB sessions, open_mode, and the apps context / SITENAME.

A clone TARGET that trips any of these is blocked (an admin may override with a reason).
"""
from app import models
from app.core import crypto

# Read-only probes (best-effort; each wrapped so a missing object just omits a signal)
_PROBE_SQL = {
    "dbid":             "select to_char(dbid) from v$database",
    "global_name":      "select global_name from global_name",
    "open_mode":        "select open_mode from v$database",
    "startup":          "select to_char(startup_time,'YYYY-MM-DD HH24:MI') from v$instance",
    "named_sessions":   "select count(*) from v$session where username is not null "
                        "and username not in ('SYS','SYSTEM','DBSNMP','XDB','SYSMAN')",
    # connected EBS application users
    "icx_active":       "select count(*) from apps.icx_sessions "
                        "where disabled_flag='N' and last_connect > sysdate - 1",
    # concurrent processing activity (R=running, P=pending)
    "running_requests": "select count(*) from apps.fnd_concurrent_requests where phase_code in ('R','P')",
    # distinct real users that logged in over the last week
    "recent_logins":    "select count(distinct user_id) from applsys.fnd_logins where start_time > sysdate - 7",
    # apps context name + site marker (context variable on the application node)
    "context_name":     "select name from (select name from fnd_oam_context_files "
                        "where ctx_type='A' order by last_synchronized desc) where rownum = 1",
    "sitename":         "select substr(fnd_profile.value('SITENAME'),1,60) from dual",
}


def probe_target(env) -> dict:
    """Connect read-only to an environment and collect identity + behavioural signals."""
    out = {"reachable": False}
    if not (env.db_host and env.db_sid and env.db_user):
        out["note"] = "no DB credentials configured for live verification"
        return out
    try:
        import oracledb
        dsn = oracledb.makedsn(env.db_host, env.db_port or 1521, sid=env.db_sid)
        conn = oracledb.connect(user=env.db_user, password=crypto.decrypt(env.db_password_enc), dsn=dsn)
        out["reachable"] = True
        cur = conn.cursor()
        for key, sql in _PROBE_SQL.items():
            try:
                cur.execute(sql)
                row = cur.fetchone()
                out[key] = row[0] if row else None
            except Exception:
                out[key] = None
        conn.close()
    except Exception as exc:
        out["error"] = str(exc)[:200]
    return out


def capture_identity(env) -> dict:
    """Read just DBID + global_name (used when registering/testing an environment)."""
    p = probe_target(env)
    return {"reachable": p.get("reachable", False),
            "db_id": p.get("dbid"), "global_name": p.get("global_name"),
            "error": p.get("error")}


def _known_prod(db):
    return db.query(models.EbsEnvironment).filter(
        models.EbsEnvironment.tier == "prod",
        models.EbsEnvironment.is_active == True,  # noqa: E712
    ).all()


def evaluate(target_env, db) -> dict:
    """Return {production, blocked, reasons[], probe_reachable, signals{}}."""
    reasons, production = [], False
    prod = _known_prod(db)
    prod_dbids = {p.db_id for p in prod if p.db_id}
    prod_gnames = {(p.global_name or "").upper() for p in prod if p.global_name}

    # 1. registry tier
    if (target_env.tier or "").lower() == "prod":
        production = True
        reasons.append(f"Target environment '{target_env.name}' is classified PROD in the registry.")

    # 2. stored intrinsic identity (captured when the env was registered/tested)
    if target_env.db_id and target_env.db_id in prod_dbids:
        production = True
        reasons.append(f"Target DBID {target_env.db_id} matches a registered PRODUCTION database.")
    if target_env.global_name and target_env.global_name.upper() in prod_gnames:
        production = True
        reasons.append(f"Target global_name {target_env.global_name} matches a registered PRODUCTION database.")

    # 3. live probe (intrinsic + behavioural)
    probe = probe_target(target_env)
    if probe.get("reachable"):
        pid, pg = probe.get("dbid"), (probe.get("global_name") or "").upper()
        if pid and pid in prod_dbids:
            production = True
            reasons.append(f"LIVE: target DBID {pid} matches a registered PRODUCTION database (mis-labelled).")
        if pg and pg in prod_gnames:
            production = True
            reasons.append(f"LIVE: target global_name {probe.get('global_name')} matches a registered PRODUCTION database.")

        strong = 0
        ns = probe.get("named_sessions") or 0
        if ns > 25:
            strong += 1; reasons.append(f"{ns} named database sessions connected.")
        icx = probe.get("icx_active") or 0
        if icx > 10:
            strong += 1; reasons.append(f"{icx} active ICX (EBS user) sessions.")
        rr = probe.get("running_requests") or 0
        if rr > 5:
            strong += 1; reasons.append(f"{rr} concurrent requests running/pending.")
        rl = probe.get("recent_logins") or 0
        if rl > 25:
            strong += 1; reasons.append(f"{rl} distinct users logged in over the last 7 days.")
        ctx = ((probe.get("sitename") or "") + " " + (probe.get("context_name") or "")).strip()
        if "PROD" in ctx.upper():
            reasons.append(f"Apps context / SITENAME contains 'PROD' ({ctx}).")
        if strong >= 2:
            production = True
            reasons.append("Behavioural profile resembles a live production system.")
    else:
        reasons.append("Live verification unavailable (target DB not reachable) — "
                       "relied on registry tier + stored identity. Verify manually.")

    signals = {k: probe.get(k) for k in
               ["dbid", "global_name", "open_mode", "named_sessions", "icx_active",
                "running_requests", "recent_logins", "sitename", "context_name"]
               if probe.get(k) is not None}
    return {"production": production, "blocked": production, "reasons": reasons,
            "probe_reachable": probe.get("reachable", False), "signals": signals}
