"""
Oracle EBS Performance Analyzer Agent
Runs diagnostic queries against Oracle EBS database and streams AI analysis via SSE.
Falls back to realistic simulated data when a real Oracle connection is unavailable.
"""
import json
import random
import asyncio
import re
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, Request, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List

from app.core.auth.auth import get_current_user, require_agent
from app import models
from app.core.llm import llm_service
from app.core import database, config_service, telemetry, prompts

router = APIRouter(prefix="/performance", tags=["Performance Agent"])

ALL_AREAS = ["wait_events", "top_sql", "memory", "locks", "tablespace", "concurrent_manager", "statistics"]


def _record_perf_tokens(user, provider, model, messages, full_analysis, usage):
    """Fire-and-forget token telemetry for a performance analysis run."""
    try:
        prompt_text = "".join(m.get("content", "") for m in messages)
        prompt_tokens = usage.get("prompt_tokens") or telemetry.est_tokens(prompt_text)
        completion_tokens = usage.get("completion_tokens") or telemetry.est_tokens("".join(full_analysis))
        asyncio.get_event_loop().run_in_executor(
            None, telemetry.record_llm,
            user.id, user.username, "performance.analyze", provider, model,
            prompt_tokens, completion_tokens, len(prompt_text),
        )
    except Exception:
        pass


class PerformanceRequest(BaseModel):
    environment: Optional[str] = "EBS"
    db_host: Optional[str] = None
    db_port: Optional[int] = 1521
    db_sid: Optional[str] = None
    db_user: Optional[str] = None
    db_password: Optional[str] = None
    analysis_areas: List[str] = ALL_AREAS


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


# ── Simulated Oracle diagnostic data ─────────────────────────────────────────
# Each function seeds random with env so results are deterministic per environment.

def _wait_events(env: str) -> dict:
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


def _top_sql(env: str) -> dict:
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


def _memory(env: str) -> dict:
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


def _locks(env: str) -> dict:
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


def _tablespace(env: str) -> dict:
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


def _concurrent_manager(env: str) -> dict:
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


def _statistics(env: str) -> dict:
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


# ── LLM prompt ─────────────────────────────────────────────────────────────────

def _build_prompt(env: str, diagnostics: dict) -> list:
    system = prompts.get_prompt("performance.diagnostic")
    data_text = json.dumps(diagnostics, indent=2, default=str)
    if len(data_text) > 6000:
        data_text = data_text[:6000] + "\n...[truncated]"
    return [
        {"role": "system", "content": system},
        {"role": "user",   "content": f"Oracle EBS Environment: **{env}**\n\nDiagnostic Data:\n```json\n{data_text}\n```\n\nGenerate the performance report."},
    ]


# ── Endpoint ───────────────────────────────────────────────────────────────────

@router.post("/analyze")
async def analyze_performance(
    payload: PerformanceRequest,
    request: Request,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(require_agent("performance")),
):
    provider = request.headers.get("X-LLM-Provider", "ollama")
    model    = request.headers.get("X-LLM-Model") or None
    api_key  = request.headers.get("X-LLM-Api-Key") or None
    base_url = request.headers.get("X-LLM-Base-Url") or None
    provider, model, api_key, base_url = config_service.resolve_llm(
        provider, model, api_key, base_url, db, agent="performance",
        route_text="analyze Oracle EBS database performance diagnostics and recommend optimizations",
    )

    env   = payload.environment or "EBS"
    areas = payload.analysis_areas or ALL_AREAS

    _AREA_RUNNERS = {
        "wait_events":        (_wait_events,        "Querying V$SYSTEM_EVENT for top wait events..."),
        "top_sql":            (_top_sql,             "Querying V$SQLAREA for top SQL by elapsed time..."),
        "memory":             (_memory,              "Analyzing SGA / PGA memory configuration..."),
        "locks":              (_locks,               "Checking blocking sessions and lock contention..."),
        "tablespace":         (_tablespace,          "Scanning DBA_TABLESPACE_USAGE_METRICS..."),
        "concurrent_manager": (_concurrent_manager,  "Analyzing Concurrent Manager queue depth..."),
        "statistics":         (_statistics,          "Checking stale object statistics and invalid objects..."),
    }

    async def stream():
        yield _sse({"type": "phase", "content": f"🔍 Connecting to {env} — running Oracle diagnostic queries..."})
        await asyncio.sleep(0.05)

        diagnostics = {}
        for area in areas:
            if area not in _AREA_RUNNERS:
                continue
            fn, msg = _AREA_RUNNERS[area]
            yield _sse({"type": "progress", "area": area, "content": msg})
            await asyncio.sleep(0.25)
            diagnostics[area] = fn(env)

        yield _sse({"type": "diagnostics", "data": diagnostics})
        yield _sse({"type": "phase", "content": "🤖 Sending metrics to AI — generating performance report..."})
        await asyncio.sleep(0.05)

        messages = _build_prompt(env, diagnostics)
        full_analysis: list[str] = []
        usage: dict = {}
        try:
            async for token in llm_service.stream_tokens(messages, provider, model, api_key, base_url, usage=usage):
                full_analysis.append(token)
                yield _sse({"type": "analysis_token", "content": token})
        except Exception as exc:
            yield _sse({"type": "error", "content": f"AI analysis failed: {exc}"})
            return

        _record_perf_tokens(current_user, provider, model, messages, full_analysis, usage)
        yield _sse({"type": "analysis_complete", "content": "".join(full_analysis)})
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


# ══════════════════════════════════════════════════════════════════════════════
# AWR — Snapshot list, period comparison, and report upload / parse
# ══════════════════════════════════════════════════════════════════════════════

# Snap 10000 = Monday 2026-05-13 08:00  (1 snap = 1 hour)
_SNAP_BASE_ID = 10000
_SNAP_BASE_DT = datetime(2026, 5, 13, 8, 0)


def _snap_to_dt(snap_id: int) -> datetime:
    return _SNAP_BASE_DT + timedelta(hours=(snap_id - _SNAP_BASE_ID))


def _is_peak(snap_id: int) -> bool:
    dt = _snap_to_dt(snap_id)
    return dt.weekday() < 5 and 9 <= dt.hour <= 17


def _generate_snapshots(env: str) -> list:
    """Hourly AWR snapshots for the past 7 days (168 records)."""
    snaps = []
    for i in range(169):          # May 13 08:00 → May 20 08:00
        sid = _SNAP_BASE_ID + i
        dt  = _snap_to_dt(sid)
        snaps.append({
            "snap_id":        sid,
            "begin_interval": dt.strftime("%Y-%m-%d %H:%M"),
            "end_interval":   (dt + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M"),
            "label":          dt.strftime("%a %b %d  %H:%M"),
            "day_label":      dt.strftime("%A, %b %d"),
            "is_peak":        _is_peak(sid),
        })
    return snaps


def _awr_period_metrics(env: str, begin_snap: int, end_snap: int) -> dict:
    """Generate realistic AWR aggregate metrics for a snap range."""
    mid = (begin_snap + end_snap) // 2
    rng = random.Random(hash(f"{env}_{mid}") % 100000)
    lf  = rng.uniform(0.88, 1.25) if _is_peak(mid) else rng.uniform(0.22, 0.55)

    b_dt = _snap_to_dt(begin_snap)
    e_dt = _snap_to_dt(end_snap)
    return {
        "snap_range": f"{begin_snap}–{end_snap}",
        "period":     f"{b_dt.strftime('%a %b %d %H:%M')} – {e_dt.strftime('%a %b %d %H:%M')}",
        "is_peak":    _is_peak(mid),
        "load_profile": {
            "db_time_per_s":        round(lf * rng.uniform(6, 18), 2),
            "db_cpu_per_s":         round(lf * rng.uniform(3.5, 12), 2),
            "redo_bytes_per_s":     round(lf * rng.uniform(380000, 2100000)),
            "logical_reads_per_s":  round(lf * rng.uniform(38000, 210000)),
            "physical_reads_per_s": round(lf * rng.uniform(1200, 16000)),
            "user_calls_per_s":     round(lf * rng.uniform(70, 520), 1),
            "parses_per_s":         round(lf * rng.uniform(35, 320), 1),
            "hard_parses_per_s":    round(lf * rng.uniform(2, 55), 1),
            "transactions_per_s":   round(lf * rng.uniform(7, 85), 1),
        },
        "instance_efficiency": {
            "buffer_cache_hit_pct":  round(max(79, 99.5 - lf * rng.uniform(0.8, 9)), 2),
            "library_cache_hit_pct": round(max(81, 99.8 - lf * rng.uniform(0.4, 6)), 2),
            "soft_parse_pct":        round(max(68, 97  - lf * rng.uniform(1.5, 14)), 2),
            "execute_to_parse_pct":  round(rng.uniform(52, 87), 2),
            "latch_hit_pct":         round(max(99.0, 99.999 - lf * rng.uniform(0, 0.6)), 3),
        },
        "top_events": [
            {"event": "db file sequential read",       "waits": round(lf*rng.randint(18000,160000)), "time_s": round(lf*rng.uniform(55,650),1),  "avg_ms": rng.randint(2,16),  "pct_db_time": round(lf*rng.uniform(5,23),1)},
            {"event": "log file sync",                 "waits": round(lf*rng.randint(1800,22000)),   "time_s": round(lf*rng.uniform(7,110),1),   "avg_ms": rng.randint(1,11),  "pct_db_time": round(lf*rng.uniform(1,8),1)},
            {"event": "db file scattered read",        "waits": round(lf*rng.randint(2800,55000)),   "time_s": round(lf*rng.uniform(13,220),1),  "avg_ms": rng.randint(3,23),  "pct_db_time": round(lf*rng.uniform(2,11),1)},
            {"event": "enq: TX - row lock contention", "waits": round(lf*rng.randint(4,2200)),       "time_s": round(lf*rng.uniform(0.3,65),1),  "avg_ms": rng.randint(9,160), "pct_db_time": round(lf*rng.uniform(0.2,5),1)},
            {"event": "buffer busy waits",             "waits": round(lf*rng.randint(250,9000)),     "time_s": round(lf*rng.uniform(0.8,28),1),  "avg_ms": rng.randint(1,7),   "pct_db_time": round(lf*rng.uniform(0.1,2),1)},
        ],
        "top_sql": [
            {"sql_id": "abc123def4567", "elapsed_s": round(lf*rng.uniform(75,720),1),  "executions": round(lf*rng.randint(75,4200)),   "disk_reads": round(lf*rng.randint(900,42000)),  "sql_text": "SELECT /*+ FULL(mts) */ * FROM mtl_material_transactions WHERE organization_id=:1"},
            {"sql_id": "xyz789abc0123", "elapsed_s": round(lf*rng.uniform(55,520),1),  "executions": round(lf*rng.randint(140,6500)),  "disk_reads": round(lf*rng.randint(380,27000)),  "sql_text": "UPDATE ap_invoice_lines_all SET amount=:1 WHERE invoice_id=:2"},
            {"sql_id": "pqr456stu7890", "elapsed_s": round(lf*rng.uniform(35,370),1),  "executions": round(lf*rng.randint(35,1600)),   "disk_reads": round(lf*rng.randint(1400,65000)), "sql_text": "SELECT DISTINCT p.party_id,p.party_name FROM hz_parties p,hz_cust_accounts ca"},
            {"sql_id": "mno789pqr3456", "elapsed_s": round(lf*rng.uniform(22,260),1),  "executions": round(lf*rng.randint(180,8500)),  "disk_reads": round(lf*rng.randint(130,8500)),   "sql_text": "SELECT count(*) FROM fnd_concurrent_requests WHERE status_code IN('R','S','Q')"},
            {"sql_id": "def012ghi5678", "elapsed_s": round(lf*rng.uniform(13,160),1),  "executions": round(lf*rng.randint(750,16000)), "disk_reads": round(lf*rng.randint(70,4200)),    "sql_text": "SELECT poh.segment1,pol.quantity FROM po_headers_all poh,po_lines_all pol"},
        ],
    }


# ── AWR text / HTML report parser ─────────────────────────────────────────────

def _parse_awr_report(raw: str) -> dict:
    """
    Best-effort parse of an Oracle AWR text or HTML report.
    Always returns raw_text for LLM fallback even when structured parse fails.
    """
    # Strip HTML if needed
    if re.search(r'<(html|body|table)', raw, re.IGNORECASE):
        text = re.sub(r'<script[^>]*>.*?</script>', '', raw, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<style[^>]*>.*?</style>',  '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        for ent, char in [('&nbsp;', ' '), ('&lt;', '<'), ('&gt;', '>'), ('&amp;', '&'), ('&#160;', ' ')]:
            text = text.replace(ent, char)
        text = re.sub(r'[ \t]{2,}', ' ', text)
        text = re.sub(r'\n[ \t]+\n', '\n\n', text)
    else:
        text = raw

    result: dict = {"raw_text": text[:9000], "structured": {}, "parse_quality": "limited"}
    s: dict = {}

    # DB / instance header
    m = re.search(r'DB Name\s+DB Id\s+Instance.*?\n[-\s]+\n(\S+)\s+\d+\s+(\S+)', text, re.MULTILINE)
    if m:
        s["db_name"] = m.group(1)
        s["instance"] = m.group(2)

    # Snap range
    bm = re.search(r'Begin Snap.*?(\d{4,})\s+([\d\-\w:]+\s+[\d:]+)', text)
    em = re.search(r'End\s+Snap.*?(\d{4,})\s+([\d\-\w:]+\s+[\d:]+)', text)
    el = re.search(r'Elapsed.*?([\d,]+\.?\d*)\s+\(min', text, re.IGNORECASE)
    if bm: s["begin_snap"] = bm.group(1); s["begin_time"] = bm.group(2).strip()
    if em: s["end_snap"]   = em.group(1); s["end_time"]   = em.group(2).strip()
    if el: s["elapsed_mins"] = float(el.group(1).replace(',', ''))

    # Load Profile (per-second column — first numeric column after the label)
    lp_keys = {
        "db_time_per_s":        r'DB Time\s*\(s\)\s*:\s*([\d,]+\.?\d*)',
        "db_cpu_per_s":         r'DB CPU\s*\(s\)\s*:\s*([\d,]+\.?\d*)',
        "logical_reads_per_s":  r'Logical read.*?:\s*([\d,]+\.?\d*)',
        "physical_reads_per_s": r'Physical read[^s].*?:\s*([\d,]+\.?\d*)',
        "redo_bytes_per_s":     r'Redo size.*?:\s*([\d,]+\.?\d*)',
        "user_calls_per_s":     r'User calls.*?:\s*([\d,]+\.?\d*)',
        "transactions_per_s":   r'Transactions.*?:\s*([\d,]+\.?\d*)',
        "hard_parses_per_s":    r'Hard parses.*?:\s*([\d,]+\.?\d*)',
    }
    lp = {}
    for k, pat in lp_keys.items():
        m2 = re.search(pat, text, re.IGNORECASE | re.MULTILINE)
        if m2:
            try: lp[k] = float(m2.group(1).replace(',', ''))
            except ValueError: pass
    if lp:
        s["load_profile"] = lp

    # Instance Efficiency
    eff_keys = {
        "buffer_cache_hit_pct":  r'Buffer\s+(?:Nowait\s+)?Hit\s+%\s*:\s*([\d\.]+)',
        "library_cache_hit_pct": r'Library\s+Cache\s+Hit\s+%\s*:\s*([\d\.]+)',
        "soft_parse_pct":        r'Soft\s+Parse\s+%\s*:\s*([\d\.]+)',
        "execute_to_parse_pct":  r'Execute\s+to\s+Parse\s+%\s*:\s*([\d\.]+)',
    }
    eff = {}
    for k, pat in eff_keys.items():
        m2 = re.search(pat, text, re.IGNORECASE)
        if m2:
            try: eff[k] = float(m2.group(1))
            except ValueError: pass
    if eff:
        s["instance_efficiency"] = eff

    # Top Wait Events — grab lines that look like event rows
    ev_section = re.search(
        r'(?:Top \d+\s+)?Foreground Events.*?(?=SQL\s+ordered|Time\s+Model|Back\s*ground|$)',
        text, re.DOTALL | re.IGNORECASE
    )
    events = []
    if ev_section:
        for line in ev_section.group(0).split('\n'):
            nums = re.findall(r'[\d,]+\.?\d*', line)
            label = re.sub(r'[\d,\.\s|%\*]+', '', line).strip()
            if len(nums) >= 3 and len(label) >= 5 and not re.match(r'[-~=\s]', label):
                skip_words = ('event', 'wait', 'class', 'total', 'avg', '%', 'time')
                if not any(w in label.lower() for w in skip_words):
                    try:
                        events.append({
                            "event":  label,
                            "waits":  int(float(nums[0].replace(',', ''))),
                            "time_s": float(nums[1].replace(',', '')),
                            "avg_ms": float(nums[2].replace(',', '')),
                        })
                    except (ValueError, IndexError):
                        pass
    if events:
        s["top_events"] = events[:10]

    result["structured"] = s
    if s.get("load_profile") and s.get("instance_efficiency"):
        result["parse_quality"] = "good"
    elif s.get("load_profile") or s.get("db_name") or s.get("begin_snap"):
        result["parse_quality"] = "partial"

    return result


# ── AWR LLM prompts ────────────────────────────────────────────────────────────

def _awr_compare_prompt(env, baseline, compare, b_label, c_label) -> list:
    system = prompts.get_prompt("performance.awr_compare")
    def _trim(d): t = json.dumps(d, indent=2, default=str); return t[:3500] + "\n...[truncated]" if len(t) > 3500 else t
    return [
        {"role": "system", "content": system},
        {"role": "user",   "content": (
            f"Oracle EBS: **{env}**\n\n"
            f"## Baseline — {b_label}\n```json\n{_trim(baseline)}\n```\n\n"
            f"## Comparison — {c_label}\n```json\n{_trim(compare)}\n```\n\n"
            "Perform the period-over-period analysis."
        )},
    ]


def _awr_single_prompt(env, parsed, filename) -> list:
    system = prompts.get_prompt("performance.awr_single")
    raw  = parsed.get("raw_text", "")[:6500]
    strc = json.dumps(parsed.get("structured", {}), indent=2)
    return [
        {"role": "system", "content": system},
        {"role": "user",   "content": (
            f"Oracle EBS: {env}\nFile: {filename}\n\n"
            f"Extracted metrics:\n```json\n{strc}\n```\n\n"
            f"Raw AWR text:\n```\n{raw}\n```\n\nAnalyse this AWR report."
        )},
    ]


def _awr_upload_compare_prompt(env, p1, p2, f1, f2) -> list:
    system = prompts.get_prompt("performance.awr_upload_compare")
    def _trim(p): t = p.get("raw_text","")[:3000]; return t if t else json.dumps(p.get("structured",{}), indent=2)[:3000]
    return [
        {"role": "system", "content": system},
        {"role": "user",   "content": (
            f"Oracle EBS: {env}\n\n"
            f"## Baseline AWR: {f1}\n"
            f"Extracted:\n```json\n{json.dumps(p1.get('structured',{}), indent=2)[:1500]}\n```\n"
            f"Raw:\n```\n{_trim(p1)}\n```\n\n"
            f"## Comparison AWR: {f2}\n"
            f"Extracted:\n```json\n{json.dumps(p2.get('structured',{}), indent=2)[:1500]}\n```\n"
            f"Raw:\n```\n{_trim(p2)}\n```\n\nCompare these reports."
        )},
    ]


# ── AWR Endpoints ──────────────────────────────────────────────────────────────

@router.get("/snapshots")
async def list_snapshots(
    environment: str = "EBS",
    current_user: models.User = Depends(get_current_user),
):
    return _generate_snapshots(environment)


class AWRCompareRequest(BaseModel):
    environment:          Optional[str] = "EBS"
    baseline_begin_snap:  int
    baseline_end_snap:    int
    compare_begin_snap:   int
    compare_end_snap:     int


@router.post("/awr/compare")
async def awr_compare(
    payload: AWRCompareRequest,
    request: Request,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(require_agent("performance")),
):
    provider = request.headers.get("X-LLM-Provider", "ollama")
    model    = request.headers.get("X-LLM-Model") or None
    api_key  = request.headers.get("X-LLM-Api-Key") or None
    base_url = request.headers.get("X-LLM-Base-Url") or None
    provider, model, api_key, base_url = config_service.resolve_llm(
        provider, model, api_key, base_url, db, agent="performance",
        route_text="analyze Oracle EBS database performance diagnostics and recommend optimizations",
    )
    env = payload.environment or "EBS"

    async def stream():
        yield _sse({"type": "phase", "content": "📊 Querying DBA_HIST_* for baseline period..."})
        await asyncio.sleep(0.2)
        baseline = _awr_period_metrics(env, payload.baseline_begin_snap, payload.baseline_end_snap)
        yield _sse({"type": "baseline", "data": baseline})

        yield _sse({"type": "phase", "content": "📊 Querying DBA_HIST_* for comparison period..."})
        await asyncio.sleep(0.2)
        compare = _awr_period_metrics(env, payload.compare_begin_snap, payload.compare_end_snap)
        yield _sse({"type": "compare", "data": compare})

        yield _sse({"type": "phase", "content": "🤖 AI performing period-over-period analysis..."})
        await asyncio.sleep(0.05)

        b_label = f"Snaps {payload.baseline_begin_snap}–{payload.baseline_end_snap}  ({baseline['period']})"
        c_label = f"Snaps {payload.compare_begin_snap}–{payload.compare_end_snap}  ({compare['period']})"
        messages = _awr_compare_prompt(env, baseline, compare, b_label, c_label)

        try:
            async for token in llm_service.stream_tokens(messages, provider, model, api_key, base_url):
                yield _sse({"type": "analysis_token", "content": token})
        except Exception as exc:
            yield _sse({"type": "error", "content": f"AI analysis failed: {exc}"}); return

        yield _sse({"type": "analysis_complete"})
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })


@router.post("/awr/upload")
async def awr_upload(
    request: Request,
    file1: UploadFile = File(...),
    file2: Optional[UploadFile] = File(None),
    environment: str = Form(default="EBS"),
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(require_agent("performance")),
):
    provider = request.headers.get("X-LLM-Provider", "ollama")
    model    = request.headers.get("X-LLM-Model") or None
    api_key  = request.headers.get("X-LLM-Api-Key") or None
    base_url = request.headers.get("X-LLM-Base-Url") or None
    provider, model, api_key, base_url = config_service.resolve_llm(
        provider, model, api_key, base_url, db, agent="performance",
        route_text="analyze Oracle EBS database performance diagnostics and recommend optimizations",
    )

    content1 = (await file1.read()).decode("utf-8", errors="replace")
    parsed1  = _parse_awr_report(content1)

    has_file2 = file2 and file2.filename
    parsed2   = None
    if has_file2:
        content2 = (await file2.read()).decode("utf-8", errors="replace")
        parsed2  = _parse_awr_report(content2)

    async def stream():
        yield _sse({"type": "phase", "content": f"📄 Parsing {file1.filename} — quality: {parsed1['parse_quality']}"})
        yield _sse({"type": "parsed", "slot": "baseline", "data": parsed1["structured"]})
        await asyncio.sleep(0.1)

        if parsed2:
            yield _sse({"type": "phase", "content": f"📄 Parsing {file2.filename} — quality: {parsed2['parse_quality']}"})
            yield _sse({"type": "parsed", "slot": "compare", "data": parsed2["structured"]})
            await asyncio.sleep(0.1)

        yield _sse({"type": "phase", "content": "🤖 AI analysing AWR report(s)..."})
        await asyncio.sleep(0.05)

        if parsed2:
            messages = _awr_upload_compare_prompt(environment, parsed1, parsed2, file1.filename, file2.filename)
        else:
            messages = _awr_single_prompt(environment, parsed1, file1.filename)

        try:
            async for token in llm_service.stream_tokens(messages, provider, model, api_key, base_url):
                yield _sse({"type": "analysis_token", "content": token})
        except Exception as exc:
            yield _sse({"type": "error", "content": f"AI analysis failed: {exc}"}); return

        yield _sse({"type": "analysis_complete"})
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })
