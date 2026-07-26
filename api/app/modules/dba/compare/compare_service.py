"""
Environment-to-environment comparison — patch parity, performance diagnostics,
and configuration drift. Point-in-time diff tooling: none of this writes to
the Finding/DiagnosticRun pipeline (models/monitoring.py), which is for
ongoing monitoring of a single environment, not pairwise comparison — see
the plan's Context section for why that pipeline's schema can't fit this.
"""
import json

from app import models
from app.core import database, crypto, prompts


# ══════════════════════════════════════════════════════════════════════════
# 1 — Patch parity (reuses existing AppliedPatchSnapshot data — no new scan
#     code; scans are triggered via the existing PatchGap
#     POST /patching/gap/scan/{environment_id})
# ══════════════════════════════════════════════════════════════════════════

def _latest_patch_snapshot(db, environment_id, component):
    return db.query(models.AppliedPatchSnapshot).filter(
        models.AppliedPatchSnapshot.environment_id == environment_id,
        models.AppliedPatchSnapshot.component == component,
    ).order_by(models.AppliedPatchSnapshot.scanned_at.desc()).first()


def compute_patch_diff(db, env_a_id: int, env_b_id: int) -> list:
    """Per component that has ANY snapshot on either side (not PatchTarget-driven
    — that's patch_gap_service.compute_gaps' baseline-vs-target job; this is
    snapshot-vs-snapshot)."""
    components = sorted({c for (c,) in db.query(models.AppliedPatchSnapshot.component).filter(
        models.AppliedPatchSnapshot.environment_id.in_([env_a_id, env_b_id])
    ).distinct().all()})

    out = []
    for component in components:
        snap_a = _latest_patch_snapshot(db, env_a_id, component)
        snap_b = _latest_patch_snapshot(db, env_b_id, component)
        applied_a = set(snap_a.applied_patches or []) if snap_a else set()
        applied_b = set(snap_b.applied_patches or []) if snap_b else set()
        out.append({
            "component": component,
            "only_in_a": sorted(applied_a - applied_b),
            "only_in_b": sorted(applied_b - applied_a),
            "common_count": len(applied_a & applied_b),
            "a_status": snap_a.scan_status if snap_a else "never_scanned",
            "b_status": snap_b.scan_status if snap_b else "never_scanned",
            "a_scanned_at": snap_a.scanned_at if snap_a else None,
            "b_scanned_at": snap_b.scanned_at if snap_b else None,
        })
    return out


# ══════════════════════════════════════════════════════════════════════════
# 2 — Performance compare (reuses performance_service.run_area; new prompt)
# ══════════════════════════════════════════════════════════════════════════

def _trim(d) -> str:
    t = json.dumps(d, indent=2, default=str)
    return t[:3500] + "\n...[truncated]" if len(t) > 3500 else t


def build_env_compare_prompt(env_a_name, env_b_name, diagnostics_a, diagnostics_b) -> list:
    system = prompts.get_prompt("performance.env_compare")
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": (
            f"## Environment A — {env_a_name}\n```json\n{_trim(diagnostics_a)}\n```\n\n"
            f"## Environment B — {env_b_name}\n```json\n{_trim(diagnostics_b)}\n```\n\n"
            "Compare these two environments."
        )},
    ]


# ══════════════════════════════════════════════════════════════════════════
# 3 — Configuration drift (greenfield — new EnvironmentConfigSnapshot model)
# ══════════════════════════════════════════════════════════════════════════

# v1 scope excludes concurrent program/manager comparison — a full
# FND_CONCURRENT_PROGRAMS dump is thousands of rows of mostly-identical seed
# data; without a reliable seed-vs-customization filter it's low
# signal-to-noise for diffing. Deferred, not forgotten.
def _run_query_rows(cur, sql: str) -> list:
    cur.execute(sql)
    cols = [d[0].lower() for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _is_cdb(conn) -> bool:
    try:
        cur = conn.cursor()
        cur.execute("SELECT cdb FROM v$database")
        row = cur.fetchone()
        return bool(row) and str(row[0]).upper() == "YES"
    except Exception:
        return False


def _cdb_aware_db_parameters(env_row) -> list | None:
    """CONTAINERS(v$parameter) fan-out across CDB$ROOT + every PDB, using the
    SYSTEM account — a common user with the privilege to see all containers.
    The plain apps-schema connection used for the single-container query is
    PDB-local and can't see other containers' parameters at all. Returns
    None (caller falls back to the plain single-container query) when SYSTEM
    creds aren't configured or the query fails for any reason — same
    graceful-degrade convention used throughout this codebase."""
    if not (env_row.system_password_enc and env_row.db_host and env_row.db_sid):
        return None
    try:
        import oracledb
        dsn = oracledb.makedsn(env_row.db_host, env_row.db_port or 1521, sid=env_row.db_sid)
        conn = oracledb.connect(user=env_row.system_user or "system",
                                password=crypto.decrypt(env_row.system_password_enc),
                                dsn=dsn, tcp_connect_timeout=8)
        try:
            cur = conn.cursor()
            cur.execute("SELECT con_id, name FROM v$containers")
            con_names = {row[0]: row[1] for row in cur.fetchall()}
            cur.execute("SELECT con_id, name, value, isdefault FROM CONTAINERS(v$parameter) ORDER BY con_id, name")
            return [
                {"con_id": con_id, "con_name": con_names.get(con_id, f"CON#{con_id}"),
                 "name": name, "value": value, "isdefault": isdefault}
                for con_id, name, value, isdefault in cur.fetchall()
            ]
        finally:
            conn.close()
    except Exception:
        return None


_CONFIG_SQL = {
    "db_parameters": "SELECT name, value, isdefault FROM v$parameter ORDER BY name",
    "profile_options": (
        "SELECT o.profile_option_name AS name, o.user_profile_option_name AS label, "
        "v.profile_option_value AS value "
        "FROM fnd_profile_option_values v JOIN fnd_profile_options_vl o "
        "ON o.profile_option_id = v.profile_option_id "
        "WHERE v.level_id = 10001 ORDER BY o.profile_option_name"       # 10001 = Site level
    ),
    "responsibilities": (
        "SELECT responsibility_key AS name, responsibility_name AS label "
        "FROM fnd_responsibility_vl WHERE end_date IS NULL OR end_date > SYSDATE "
        "ORDER BY responsibility_name"
    ),
}


def run_config_scan(environment_id: int, triggered_by: int | None = None) -> None:
    """BackgroundTasks target, own DB session (mirrors patch_gap_service.
    run_gap_scan / monitoring_scheduler.run_scheduled_scan). Direct oracledb
    connect (prod_guard/performance_service pattern — no SSH needed here,
    these are plain SELECTs). Never raises."""
    db = database.SessionLocal()
    try:
        env = db.query(models.EbsEnvironment).filter(models.EbsEnvironment.id == environment_id).first()
        if not env:
            return

        conn = None
        if env.db_host and env.db_sid and env.db_user:
            try:
                import oracledb
                dsn = oracledb.makedsn(env.db_host, env.db_port or 1521, sid=env.db_sid)
                conn = oracledb.connect(user=env.db_user, password=crypto.decrypt(env.db_password_enc),
                                        dsn=dsn, tcp_connect_timeout=8)
            except Exception:
                conn = None

        for category, sql in _CONFIG_SQL.items():
            snap = models.EnvironmentConfigSnapshot(environment_id=env.id, category=category, scanned_by=triggered_by)
            if conn is None:
                snap.scan_status = "error"
                snap.scan_error = "no DB credentials or connection failed"
                snap.payload = []
            else:
                cdb_rows = None
                if category == "db_parameters" and _is_cdb(conn):
                    cdb_rows = _cdb_aware_db_parameters(env)
                if cdb_rows is not None:
                    snap.payload = cdb_rows
                    snap.scan_status = "ok"
                else:
                    try:
                        snap.payload = _run_query_rows(conn.cursor(), sql)
                        snap.scan_status = "ok"
                    except Exception as exc:
                        snap.scan_status = "error"
                        snap.scan_error = str(exc)[:500]
                        snap.payload = []
            db.add(snap)

        if conn:
            conn.close()
        db.commit()

    except Exception:
        db.rollback()
    finally:
        db.close()


def _param_key(r: dict) -> str:
    """CDB-aware snapshots carry a con_name per row (see
    _cdb_aware_db_parameters) — fold it into the diff key so CDB-level and
    each PDB's values are compared independently instead of being conflated
    by parameter name alone. Non-CDB rows have no con_name and fall through
    to the plain name."""
    return f"{r['con_name']}: {r['name']}" if r.get("con_name") else r["name"]


def _diff_rows(rows_a: list, rows_b: list, category: str) -> dict:
    if category == "db_parameters":
        # Environment-identity/path parameters (db_name, control_files, ...)
        # always differ and would drown the handful of genuinely interesting
        # tuning differences — restrict the diff to explicitly-set params.
        rows_a = [r for r in rows_a if str(r.get("isdefault")).upper() == "FALSE"]
        rows_b = [r for r in rows_b if str(r.get("isdefault")).upper() == "FALSE"]
        a = {_param_key(r): r.get("value") for r in rows_a}
        b = {_param_key(r): r.get("value") for r in rows_b}
    else:
        a = {r["name"]: r.get("value") for r in rows_a}
        b = {r["name"]: r.get("value") for r in rows_b}
    return {
        "only_in_a": sorted(set(a) - set(b)),
        "only_in_b": sorted(set(b) - set(a)),
        "different": [{"name": k, "a": a[k], "b": b[k]} for k in sorted(set(a) & set(b)) if a[k] != b[k]],
    }


def _latest_config_snapshot(db, environment_id, category):
    return db.query(models.EnvironmentConfigSnapshot).filter(
        models.EnvironmentConfigSnapshot.environment_id == environment_id,
        models.EnvironmentConfigSnapshot.category == category,
    ).order_by(models.EnvironmentConfigSnapshot.scanned_at.desc()).first()


def compute_config_diff(db, env_a_id: int, env_b_id: int) -> dict:
    out = {}
    for category in _CONFIG_SQL:
        snap_a = _latest_config_snapshot(db, env_a_id, category)
        snap_b = _latest_config_snapshot(db, env_b_id, category)
        out[category] = {
            "a_status": snap_a.scan_status if snap_a else "never_scanned",
            "b_status": snap_b.scan_status if snap_b else "never_scanned",
            "a_scanned_at": snap_a.scanned_at if snap_a else None,
            "b_scanned_at": snap_b.scanned_at if snap_b else None,
            "diff": _diff_rows(snap_a.payload if snap_a else [], snap_b.payload if snap_b else [], category),
        }
    return out
