"""
Performance Agent diagnostic data — simulation generators, the real-Oracle
query path, and deterministic finding extraction.

Pure logic module (no FastAPI/DB-session coupling), mirroring cloning_service.py.
`performance_agent.py` imports the sim_* generators back for the existing
on-demand `/performance/analyze` SSE endpoint (unchanged behavior). The real
query path and finding extraction are new — used by the scheduled monitoring
scan (monitoring_scheduler.py) and, when `environment_id` is supplied, by
`/performance/analyze` itself.
"""
import random
import hashlib

ALL_AREAS = ["wait_events", "top_sql", "memory", "locks", "tablespace", "concurrent_manager", "statistics"]


# ── Simulated Oracle diagnostic data ─────────────────────────────────────────
# Each function seeds random with env so results are deterministic per environment.

def sim_wait_events(env: str) -> dict:
    rng = random.Random(hash(env) % 10000)
    events = [
        {"event": "db file sequential read",       "waits": rng.randint(40000, 250000), "time_ms": rng.randint(120000, 900000), "avg_ms": rng.randint(2, 18)},
        {"event": "db file scattered read",        "waits": rng.randint(8000,  90000),  "time_ms": rng.randint(30000,  400000), "avg_ms": rng.randint(3, 22)},
        {"event": "log file sync",                 "waits": rng.randint(3000,  35000),  "time_ms": rng.randint(8000,   150000), "avg_ms": rng.randint(1, 10)},
        {"event": "enq: TX - row lock contention", "waits": rng.randint(10,    4000),   "time_ms": rng.randint(100,    90000),  "avg_ms": rng.randint(5, 120)},
        {"event": "buffer busy waits",             "waits": rng.randint(500,   25000),  "time_ms": rng.randint(1000,  50000),   "avg_ms": rng.randint(1, 6)},
        {"event": "library cache lock",            "waits": rng.randint(50,    6000),   "time_ms": rng.randint(200,   60000),   "avg_ms": rng.randint(1, 40)},
        {"event": "free buffer waits",             "waits": rng.randint(0,     800),    "time_ms": rng.randint(0,     10000),   "avg_ms": rng.randint(0, 25)},
        {"event": "SQL*Net message from client",   "waits": rng.randint(100000,1000000),"time_ms": rng.randint(2000000,8000000),"avg_ms": rng.randint(600,3000)},
    ]
    return {
        "query": "SELECT event, total_waits, time_waited_micro/1000 time_ms, average_wait/1000 avg_ms FROM v$system_event WHERE wait_class != 'Idle' ORDER BY time_waited_micro DESC",
        "rows": sorted(events, key=lambda x: x["time_ms"], reverse=True),
    }


def sim_top_sql(env: str) -> dict:
    rng = random.Random(hash(env + "sql") % 10000)
    sql_texts = [
        "SELECT /*+ FULL(mts) */ * FROM mtl_material_transactions mts WHERE organization_id = :1 AND transaction_date > :2",
        "SELECT count(*) FROM fnd_concurrent_requests WHERE status_code IN ('R','S','Q') AND requested_start_date < sysdate - 1/24",
        "UPDATE ap_invoice_lines_all SET amount = :1 WHERE invoice_id = :2 AND line_number = :3",
        "SELECT DISTINCT p.party_id, p.party_name FROM hz_parties p, hz_cust_accounts ca WHERE ca.party_id = p.party_id AND p.status = 'A'",
        "SELECT /*+ USE_NL(poh pol) */ poh.segment1, pol.quantity FROM po_headers_all poh, po_lines_all pol WHERE poh.po_header_id = pol.po_header_id AND poh.org_id = :1",
    ]
    sql_ids = ["".join(rng.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=13)) for _ in sql_texts]
    rows = []
    for sid, txt in zip(sql_ids, sql_texts):
        elapsed = rng.randint(300000, 9000000)
        rows.append({
            "sql_id":        sid,
            "executions":    rng.randint(50, 60000),
            "elapsed_sec":   round(elapsed / 1e6, 2),
            "cpu_sec":       round(elapsed * rng.uniform(0.4, 0.9) / 1e6, 2),
            "disk_reads":    rng.randint(2000, 250000),
            "buffer_gets":   rng.randint(20000, 3000000),
            "rows_processed":rng.randint(500, 600000),
            "sql_text":      txt[:120],
        })
    return {
        "query": "SELECT sql_id, executions, elapsed_time/1e6 elapsed_sec, cpu_time/1e6 cpu_sec, disk_reads, buffer_gets, sql_text FROM v$sqlarea ORDER BY elapsed_time DESC FETCH FIRST 5 ROWS ONLY",
        "rows": sorted(rows, key=lambda x: x["elapsed_sec"], reverse=True),
    }


def sim_memory(env: str) -> dict:
    rng = random.Random(hash(env + "mem") % 10000)
    sga_mb = rng.choice([4096, 8192, 12288, 16384])
    pga_mb = rng.choice([1024, 2048, 4096])
    bc_hit  = round(rng.uniform(84, 99.5), 2)
    sp_hit  = round(rng.uniform(88, 99.8), 2)
    return {
        "sga": {
            "query": "SELECT name, bytes/1024/1024 mb FROM v$sga",
            "rows": [
                {"name": "Fixed Size",        "mb": round(rng.uniform(1.5, 2.5), 2)},
                {"name": "Variable Size",     "mb": round(sga_mb * 0.35, 2)},
                {"name": "Database Buffers",  "mb": round(sga_mb * 0.60, 2)},
                {"name": "Redo Buffers",      "mb": round(rng.uniform(8, 64), 2)},
                {"name": "TOTAL",             "mb": sga_mb},
            ],
        },
        "pga": {
            "query": "SELECT name, value/1024/1024 mb FROM v$pgastat WHERE name IN ('total PGA allocated','maximum PGA allocated','PGA target parameter')",
            "rows": [
                {"name": "PGA target parameter",   "mb": pga_mb},
                {"name": "total PGA allocated",    "mb": round(pga_mb * rng.uniform(0.55, 0.95), 2)},
                {"name": "maximum PGA allocated",  "mb": round(pga_mb * rng.uniform(0.85, 1.25), 2)},
            ],
        },
        "buffer_cache_hit_pct": bc_hit,
        "shared_pool_hit_pct":  sp_hit,
    }


def sim_locks(env: str) -> dict:
    rng = random.Random(hash(env + "lck") % 10000)
    n = rng.randint(0, 6)
    sqls = [
        "UPDATE ap_invoices_all SET payment_status_flag = :1 WHERE invoice_id = :2",
        "INSERT INTO fnd_concurrent_requests VALUES (:1, :2, :3, :4)",
        "DELETE FROM wf_item_activity_statuses WHERE item_type = :1 AND item_key = :2",
    ]
    blocking = [
        {
            "blocking_sid":    rng.randint(100, 600),
            "blocking_user":   rng.choice(["APPS", "APPSRO", "XXCUST"]),
            "blocked_sid":     rng.randint(100, 600),
            "blocked_user":    rng.choice(["APPS", "APPSRO"]),
            "wait_seconds":    rng.randint(10, 4000),
            "sql_text":        rng.choice(sqls),
        }
        for _ in range(n)
    ]
    return {
        "query": "SELECT blocking_session blocking_sid, s.username blocking_user, s.sid blocked_sid, s.username blocked_user, s.seconds_in_wait wait_seconds, q.sql_text FROM v$session s, v$sql q WHERE s.blocking_session IS NOT NULL AND s.sql_id = q.sql_id(+)",
        "blocking_sessions": blocking,
        "total_active":  rng.randint(15, 180),
        "total_waiting": rng.randint(3, 50),
    }


def sim_tablespace(env: str) -> dict:
    rng = random.Random(hash(env + "tbs") % 10000)
    specs = [
        ("SYSTEM",          rng.uniform(55, 88)),
        ("SYSAUX",          rng.uniform(45, 92)),
        ("APPS_TS_TX_DATA", rng.uniform(68, 99)),
        ("APPS_TS_TX_IDX",  rng.uniform(60, 97)),
        ("APPS_TS_SEED",    rng.uniform(35, 72)),
        ("APPS_TS_TOOLS",   rng.uniform(25, 65)),
        ("APPS_TS_MEDIA",   rng.uniform(15, 88)),
        ("UNDOTBS1",        rng.uniform(25, 78)),
        ("TEMP",            rng.uniform(8,  55)),
    ]
    rows = []
    for name, pct in specs:
        total = round(rng.uniform(20, 200), 2)
        used  = round(total * pct / 100, 2)
        rows.append({"tablespace_name": name, "total_gb": total, "used_gb": used,
                     "free_gb": round(total - used, 2), "used_pct": round(pct, 2)})
    return {
        "query": "SELECT tablespace_name, ROUND(used_space*8192/1024/1024/1024,2) used_gb, ROUND(tablespace_size*8192/1024/1024/1024,2) total_gb FROM dba_tablespace_usage_metrics",
        "rows": sorted(rows, key=lambda x: x["used_pct"], reverse=True),
    }


def sim_concurrent_manager(env: str) -> dict:
    rng = random.Random(hash(env + "cm") % 10000)
    programs = [
        "ARXRWMAI - AutoInvoice Master Program", "RAXMTR - Revenue Recognition",
        "XLAACCPB - Transfer to GL", "INVMRP - MRP Planning", "POXCON - PO Approval",
        "XXAP_CUSTOM_REPORT - Custom AP Report",
    ]
    long_running = [
        {"request_id": rng.randint(1000000, 9999999), "program": rng.choice(programs),
         "requestor": rng.choice(["SYSADMIN", "APPS", "APPSRO"]),
         "running_minutes": rng.randint(35, 500)}
        for _ in range(rng.randint(0, 5))
    ]
    error_programs = [
        {"program": rng.choice(programs), "error_count_1h": rng.randint(1, 12)}
        for _ in range(rng.randint(0, 4))
    ]
    return {
        "queue": {
            "pending":       rng.randint(0, 60),
            "running":       rng.randint(0, 35),
            "completed_1h":  rng.randint(10, 400),
            "errored_1h":    rng.randint(0, 25),
        },
        "long_running_requests": long_running,
        "frequently_erroring":   error_programs,
    }


def sim_statistics(env: str) -> dict:
    rng = random.Random(hash(env + "sts") % 10000)
    all_tables = [
        "MTL_MATERIAL_TRANSACTIONS", "AP_INVOICES_ALL", "RA_CUSTOMER_TRX_ALL",
        "PO_HEADERS_ALL", "WF_ITEM_ACTIVITY_STATUSES", "FND_CONCURRENT_REQUESTS",
        "OE_ORDER_HEADERS_ALL", "GL_JE_LINES", "INV_TRANSACTION_TYPES",
        "AR_PAYMENT_SCHEDULES_ALL", "AP_INVOICE_DISTRIBUTIONS_ALL",
    ]
    stale = [
        {"table_name": t, "num_rows": rng.randint(50000, 60000000),
         "last_analyzed": f"2026-{rng.randint(1,4):02d}-{rng.randint(1,28):02d}",
         "days_since_analyzed": rng.randint(7, 90),
         "stale_pct": round(rng.uniform(10, 85), 2)}
        for t in rng.sample(all_tables, rng.randint(2, 7))
    ]
    pkg_names = ["XXAP_SUPPLIER_PKG", "XXGL_CUSTOM_PKG", "XXOM_ORDER_PKG", "XXINV_STOCK_PKG", "XXAR_AGING_PKG"]
    obj_types  = ["PACKAGE BODY", "PACKAGE", "PROCEDURE", "TRIGGER"]
    invalid = [
        {"object_name": rng.choice(pkg_names), "object_type": rng.choice(obj_types), "status": "INVALID"}
        for _ in range(rng.randint(0, 8))
    ]
    return {
        "stale_statistics": {
            "query": "SELECT table_name, num_rows, last_analyzed, stale_stats FROM dba_tab_statistics WHERE stale_stats='YES' AND owner='APPS' ORDER BY num_rows DESC",
            "rows": sorted(stale, key=lambda x: x["num_rows"], reverse=True),
        },
        "invalid_objects": {
            "query": "SELECT object_name, object_type, status FROM dba_objects WHERE status='INVALID' AND owner='APPS'",
            "rows": invalid,
        },
    }


_SIM_RUNNERS = {
    "wait_events":        sim_wait_events,
    "top_sql":            sim_top_sql,
    "memory":             sim_memory,
    "locks":              sim_locks,
    "tablespace":         sim_tablespace,
    "concurrent_manager": sim_concurrent_manager,
    "statistics":         sim_statistics,
}


# ── Real Oracle query path ────────────────────────────────────────────────────
# Mirrors app.core.safety.prod_guard.probe_target: per-sub-query try/except so a
# missing grant on one view doesn't kill the whole area, tcp_connect_timeout=8.
# concurrent_manager's queries (cm_*) are best-effort against the standard R12
# FND_CONCURRENT_REQUESTS shape — validate against your instance/EBS release
# before relying on it, same "best-effort, hardcoded, not admin-editable"
# status as every other query here.

_REAL_SQL = {
    "wait_events": "SELECT event, total_waits, time_waited_micro, average_wait "
                    "FROM v$system_event WHERE wait_class != 'Idle' "
                    "ORDER BY time_waited_micro DESC FETCH FIRST 10 ROWS ONLY",
    "top_sql":     "SELECT sql_id, executions, elapsed_time, cpu_time, disk_reads, "
                    "buffer_gets, rows_processed, sql_text FROM v$sqlarea "
                    "ORDER BY elapsed_time DESC FETCH FIRST 5 ROWS ONLY",
    "memory_sga":  "SELECT name, bytes FROM v$sga",
    "memory_pga":  "SELECT name, value FROM v$pgastat WHERE name IN "
                    "('total PGA allocated','maximum PGA allocated','PGA target parameter')",
    "locks":       "SELECT blocking_session, s.sid, s.username, s.seconds_in_wait, q.sql_text "
                    "FROM v$session s LEFT JOIN v$sql q ON s.sql_id = q.sql_id "
                    "WHERE s.blocking_session IS NOT NULL",
    "tablespace":  "SELECT tablespace_name, used_percent, tablespace_size, used_space "
                    "FROM dba_tablespace_usage_metrics",
    "stale_stats": "SELECT table_name, num_rows, last_analyzed FROM dba_tab_statistics "
                    "WHERE stale_stats='YES' AND owner='APPS' AND ROWNUM <= 20",
    "invalid_objs":"SELECT object_name, object_type, status FROM dba_objects "
                    "WHERE status='INVALID' AND owner='APPS' AND ROWNUM <= 30",
    # Queue summary — current pending/running plus the last hour's completions.
    "cm_queue":    "SELECT "
                    "SUM(CASE WHEN phase_code = 'P' THEN 1 ELSE 0 END) AS pending, "
                    "SUM(CASE WHEN phase_code = 'R' THEN 1 ELSE 0 END) AS running, "
                    "SUM(CASE WHEN phase_code = 'C' AND status_code = 'C' "
                    "    AND actual_completion_date > SYSDATE - 1/24 THEN 1 ELSE 0 END) AS completed_1h, "
                    "SUM(CASE WHEN phase_code = 'C' AND status_code IN ('E','G') "
                    "    AND actual_completion_date > SYSDATE - 1/24 THEN 1 ELSE 0 END) AS errored_1h "
                    "FROM fnd_concurrent_requests "
                    "WHERE phase_code IN ('P','R') OR actual_completion_date > SYSDATE - 1/24",
    # Currently-running requests over 30 minutes, correlated to the Oracle
    # session (and its current wait event) via the standard EBS technique:
    # FND_CONCURRENT_REQUESTS.ORACLE_PROCESS_ID -> V$PROCESS.SPID -> V$SESSION.PADDR.
    "cm_long_running":
                    "SELECT fcr.request_id, fcp.user_concurrent_program_name AS program, "
                    "fu.user_name AS requestor, "
                    "ROUND((SYSDATE - fcr.actual_start_date) * 24 * 60) AS running_minutes, "
                    "s.sid, s.serial# AS serial_num, s.event AS wait_event, "
                    "s.wait_class, s.seconds_in_wait "
                    "FROM fnd_concurrent_requests fcr "
                    "JOIN fnd_concurrent_programs_vl fcp "
                    "  ON fcr.concurrent_program_id = fcp.concurrent_program_id "
                    " AND fcr.program_application_id = fcp.application_id "
                    "LEFT JOIN fnd_user fu ON fcr.requested_by = fu.user_id "
                    "LEFT JOIN v$process p ON TO_CHAR(p.spid) = fcr.oracle_process_id "
                    "LEFT JOIN v$session s ON s.paddr = p.addr "
                    "WHERE fcr.phase_code = 'R' AND fcr.actual_start_date IS NOT NULL "
                    "AND (SYSDATE - fcr.actual_start_date) * 24 * 60 >= 30 "
                    "ORDER BY running_minutes DESC FETCH FIRST 20 ROWS ONLY",
    "cm_erroring": "SELECT fcp.user_concurrent_program_name AS program, COUNT(*) AS error_count_1h "
                    "FROM fnd_concurrent_requests fcr "
                    "JOIN fnd_concurrent_programs_vl fcp "
                    "  ON fcr.concurrent_program_id = fcp.concurrent_program_id "
                    " AND fcr.program_application_id = fcp.application_id "
                    "WHERE fcr.phase_code = 'C' AND fcr.status_code IN ('E','G') "
                    "AND fcr.actual_completion_date > SYSDATE - 1/24 "
                    "GROUP BY fcp.user_concurrent_program_name "
                    "ORDER BY error_count_1h DESC FETCH FIRST 10 ROWS ONLY",
}

_REAL_AREA_QUERIES = {
    "wait_events": ["wait_events"],
    "top_sql": ["top_sql"],
    "memory": ["memory_sga", "memory_pga"],
    "locks": ["locks"],
    "tablespace": ["tablespace"],
    "statistics": ["stale_stats", "invalid_objs"],
    "concurrent_manager": ["cm_queue", "cm_long_running", "cm_erroring"],
}


def _run_real_query(cursor, key: str) -> list | None:
    try:
        cursor.execute(_REAL_SQL[key])
        cols = [d[0].lower() for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]
    except Exception:
        return None


def _real_area_query(cursor, area: str) -> dict | None:
    """Run every sub-query for `area` and shape the result to match the sim_*
    return shape as closely as reasonably possible. Returns None only if EVERY
    sub-query for the area failed (caller falls back to simulation)."""
    keys = _REAL_AREA_QUERIES.get(area)
    if not keys:
        return None
    parts = {k: _run_real_query(cursor, k) for k in keys}
    if all(v is None for v in parts.values()):
        return None

    if area == "wait_events":
        rows = parts["wait_events"] or []
        return {"query": _REAL_SQL["wait_events"], "rows": [
            {"event": r.get("event"), "waits": r.get("total_waits"),
             "time_ms": (r.get("time_waited_micro") or 0) / 1000,
             "avg_ms": r.get("average_wait")} for r in rows
        ]}
    if area == "top_sql":
        rows = parts["top_sql"] or []
        return {"query": _REAL_SQL["top_sql"], "rows": [
            {"sql_id": r.get("sql_id"), "executions": r.get("executions"),
             "elapsed_sec": (r.get("elapsed_time") or 0) / 1e6,
             "cpu_sec": (r.get("cpu_time") or 0) / 1e6,
             "disk_reads": r.get("disk_reads"), "buffer_gets": r.get("buffer_gets"),
             "rows_processed": r.get("rows_processed"),
             "sql_text": (r.get("sql_text") or "")[:120]} for r in rows
        ]}
    if area == "memory":
        sga_rows = parts.get("memory_sga") or []
        pga_rows = parts.get("memory_pga") or []
        total = next((r["bytes"] / 1024 / 1024 for r in sga_rows if (r.get("name") or "") == "Total System Global Area"), None)
        return {
            "sga": {"query": _REAL_SQL["memory_sga"],
                    "rows": [{"name": r.get("name"), "mb": (r.get("bytes") or 0) / 1024 / 1024} for r in sga_rows]},
            "pga": {"query": _REAL_SQL["memory_pga"],
                    "rows": [{"name": r.get("name"), "mb": (r.get("value") or 0) / 1024 / 1024} for r in pga_rows]},
            "buffer_cache_hit_pct": None,   # not derivable from this query set without v$sysstat ratios; left for a future pass
            "shared_pool_hit_pct": None,
        }
    if area == "locks":
        rows = parts["locks"] or []
        blocking = [
            {"blocking_sid": r.get("blocking_session"), "blocking_user": None,
             "blocked_sid": r.get("sid"), "blocked_user": r.get("username"),
             "wait_seconds": r.get("seconds_in_wait"), "sql_text": r.get("sql_text")}
            for r in rows
        ]
        return {"query": _REAL_SQL["locks"], "blocking_sessions": blocking,
                "total_active": len(rows), "total_waiting": len(rows)}
    if area == "tablespace":
        rows = parts["tablespace"] or []
        out_rows = []
        for r in rows:
            total = r.get("tablespace_size") or 0
            used = r.get("used_space") or 0
            out_rows.append({
                "tablespace_name": r.get("tablespace_name"),
                "total_gb": round(total / 1024, 2) if total else 0,
                "used_gb": round(used / 1024, 2) if used else 0,
                "free_gb": round((total - used) / 1024, 2) if total else 0,
                "used_pct": r.get("used_percent"),
            })
        return {"query": _REAL_SQL["tablespace"], "rows": out_rows}
    if area == "statistics":
        stale_rows = parts.get("stale_stats") or []
        invalid_rows = parts.get("invalid_objs") or []
        return {
            "stale_statistics": {"query": _REAL_SQL["stale_stats"], "rows": [
                {"table_name": r.get("table_name"), "num_rows": r.get("num_rows"),
                 "last_analyzed": str(r.get("last_analyzed"))} for r in stale_rows
            ]},
            "invalid_objects": {"query": _REAL_SQL["invalid_objs"], "rows": [
                {"object_name": r.get("object_name"), "object_type": r.get("object_type"),
                 "status": r.get("status")} for r in invalid_rows
            ]},
        }
    if area == "concurrent_manager":
        queue_rows = parts.get("cm_queue") or []
        q = queue_rows[0] if queue_rows else {}
        lr_rows = parts.get("cm_long_running") or []
        err_rows = parts.get("cm_erroring") or []
        return {
            "queue": {
                "pending": q.get("pending") or 0, "running": q.get("running") or 0,
                "completed_1h": q.get("completed_1h") or 0, "errored_1h": q.get("errored_1h") or 0,
            },
            "long_running_requests": [
                {"request_id": r.get("request_id"), "program": r.get("program"),
                 "requestor": r.get("requestor"), "running_minutes": r.get("running_minutes"),
                 # Session identity + current wait — the "why is it stuck" detail.
                 # sid/serial_num double as the ALTER SYSTEM KILL SESSION target.
                 "sid": r.get("sid"), "serial_num": r.get("serial_num"),
                 "wait_event": r.get("wait_event"), "wait_class": r.get("wait_class"),
                 "seconds_in_wait": r.get("seconds_in_wait")}
                for r in lr_rows
            ],
            "frequently_erroring": [
                {"program": r.get("program"), "error_count_1h": r.get("error_count_1h")}
                for r in err_rows
            ],
        }
    return None


def run_area(area: str, env_row, env_name: str) -> tuple[dict, str]:
    """Try a real query against `env_row` (an EbsEnvironment ORM row) first —
    falls back to the sim_* generator on any failure or missing credentials.
    Returns (data, 'live'|'simulated'); `data`'s shape matches the sim_*
    return shape (same keys) so callers (the LLM prompt builder, finding
    extraction) don't care which source produced it."""
    if area not in _SIM_RUNNERS:
        raise ValueError(f"Unknown diagnostic area '{area}'")

    if env_row is not None and area in _REAL_AREA_QUERIES and \
            env_row.db_host and env_row.db_sid and env_row.db_user:
        try:
            import oracledb
            from app.core import crypto
            dsn = oracledb.makedsn(env_row.db_host, env_row.db_port or 1521, sid=env_row.db_sid)
            conn = oracledb.connect(user=env_row.db_user, password=crypto.decrypt(env_row.db_password_enc),
                                    dsn=dsn, tcp_connect_timeout=8)
            try:
                cursor = conn.cursor()
                data = _real_area_query(cursor, area)
                if data is not None:
                    return data, "live"
            finally:
                conn.close()
        except Exception:
            pass

    return _SIM_RUNNERS[area](env_name), "simulated"


# ── Deterministic finding extraction ──────────────────────────────────────────
# Independent of the LLM narrative — used for the scheduled scan's delta/dedup
# feed, where a per-run LLM call would be slow, costly, and non-deterministic.

_THRESHOLDS = {
    "tablespace_warning_pct": 85,
    "tablespace_critical_pct": 95,
    "wait_event_avg_ms_warning": 20,
    "lock_wait_seconds_warning": 60,
    "buffer_cache_hit_warning_pct": 90,
    "long_running_minutes_warning": 60,
}


def _finding(finding_key, severity, title, detail):
    return {"finding_key": finding_key, "severity": severity, "title": title, "detail": detail}


def extract_findings(area: str, data: dict) -> list:
    """Pure function — no DB/IO. Returns a list of finding dicts. `finding_key`
    is stable across runs for the same underlying issue so repeated detections
    upsert the same Finding row instead of creating duplicates."""
    findings = []

    if area == "tablespace":
        for row in (data.get("rows") or []):
            pct = row.get("used_pct")
            name = row.get("tablespace_name")
            if pct is None or name is None:
                continue
            if pct >= _THRESHOLDS["tablespace_critical_pct"]:
                findings.append(_finding(name, "critical", f"Tablespace {name} is {pct}% full", row))
            elif pct >= _THRESHOLDS["tablespace_warning_pct"]:
                findings.append(_finding(name, "warning", f"Tablespace {name} is {pct}% full", row))

    elif area == "wait_events":
        for row in (data.get("rows") or []):
            avg_ms = row.get("avg_ms")
            event = row.get("event")
            if avg_ms is None or event is None:
                continue
            if avg_ms >= _THRESHOLDS["wait_event_avg_ms_warning"]:
                findings.append(_finding(event, "warning",
                    f"Wait event '{event}' averaging {avg_ms}ms", row))

    elif area == "locks":
        for row in (data.get("blocking_sessions") or []):
            wait_s = row.get("wait_seconds")
            if wait_s is None or wait_s < _THRESHOLDS["lock_wait_seconds_warning"]:
                continue
            key = f"{row.get('blocking_sid')}:{row.get('blocked_sid')}"
            findings.append(_finding(key, "warning" if wait_s < 300 else "critical",
                f"Session {row.get('blocked_sid')} blocked {wait_s}s by session {row.get('blocking_sid')}", row))

    elif area == "top_sql":
        for row in (data.get("rows") or []):
            elapsed = row.get("elapsed_sec")
            sql_id = row.get("sql_id")
            if elapsed is None or sql_id is None or elapsed < 60:
                continue
            findings.append(_finding(sql_id, "warning",
                f"SQL {sql_id} elapsed {elapsed}s total", row))

    elif area == "memory":
        bc = data.get("buffer_cache_hit_pct")
        if bc is not None and bc < _THRESHOLDS["buffer_cache_hit_warning_pct"]:
            findings.append(_finding("buffer_cache_hit_pct", "warning",
                f"Buffer cache hit ratio low: {bc}%", {"buffer_cache_hit_pct": bc}))
        sp = data.get("shared_pool_hit_pct")
        if sp is not None and sp < _THRESHOLDS["buffer_cache_hit_warning_pct"]:
            findings.append(_finding("shared_pool_hit_pct", "warning",
                f"Shared pool hit ratio low: {sp}%", {"shared_pool_hit_pct": sp}))

    elif area == "concurrent_manager":
        for row in (data.get("long_running_requests") or []):
            minutes = row.get("running_minutes")
            req_id = row.get("request_id")
            if minutes is None or req_id is None or minutes < _THRESHOLDS["long_running_minutes_warning"]:
                continue
            findings.append(_finding(f"req:{req_id}", "warning",
                f"Concurrent request {req_id} ({row.get('program')}) running {minutes} min", row))
        for row in (data.get("frequently_erroring") or []):
            program = row.get("program")
            if not program:
                continue
            findings.append(_finding(f"prog:{program}", "warning",
                f"Program '{program}' erroring frequently ({row.get('error_count_1h')}/hr)", row))

    elif area == "statistics":
        # Both the real query (WHERE stale_stats='YES') and the simulated rows
        # are already pre-filtered to stale tables — no extra threshold needed.
        for row in ((data.get("stale_statistics") or {}).get("rows") or []):
            table = row.get("table_name")
            if table is None:
                continue
            findings.append(_finding(f"stale:{table}", "info",
                f"Stale statistics on {table}", row))
        for row in ((data.get("invalid_objects") or {}).get("rows") or []):
            obj = row.get("object_name")
            if not obj:
                continue
            findings.append(_finding(f"invalid:{obj}", "warning",
                f"Invalid object {obj} ({row.get('object_type')})", row))

    return findings


def value_hash(detail: dict, *fields: str) -> str:
    """sha256 of the given fields from `detail`, stable field ordering — used
    to detect whether a Finding's underlying data actually changed between
    runs (vs. just being re-detected unchanged)."""
    parts = [f"{f}={detail.get(f)}" for f in sorted(fields)]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
