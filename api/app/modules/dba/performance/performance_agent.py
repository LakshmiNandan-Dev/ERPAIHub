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
from app.core.rag import rag_service
from app.core import database, config_service, telemetry, prompts
from app.modules.dba.performance import performance_service as ps

router = APIRouter(prefix="/performance", tags=["Performance Agent"])

ALL_AREAS = ps.ALL_AREAS


def _record_perf_tokens(user, provider, model, messages, full_analysis, usage, endpoint="performance.analyze"):
    """Fire-and-forget token telemetry for a performance analysis/ask run."""
    try:
        prompt_text = "".join(m.get("content", "") for m in messages)
        prompt_tokens = usage.get("prompt_tokens") or telemetry.est_tokens(prompt_text)
        completion_tokens = usage.get("completion_tokens") or telemetry.est_tokens("".join(full_analysis))
        asyncio.get_event_loop().run_in_executor(
            None, telemetry.record_llm,
            user.id, user.username, endpoint, provider, model,
            prompt_tokens, completion_tokens, len(prompt_text),
        )
    except Exception:
        pass


class PerformanceRequest(BaseModel):
    environment: Optional[str] = "EBS"
    environment_id: Optional[int] = None
    db_host: Optional[str] = None
    db_port: Optional[int] = 1521
    db_sid: Optional[str] = None
    db_user: Optional[str] = None
    db_password: Optional[str] = None
    analysis_areas: List[str] = ALL_AREAS


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


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

    # When environment_id is supplied, resolve the registered EbsEnvironment so
    # run_area can attempt a real Oracle query (falling back to simulation on
    # any failure). Absent, behavior is unchanged: string-only env, simulated.
    env_row = None
    if payload.environment_id:
        env_row = db.query(models.EbsEnvironment).filter(
            models.EbsEnvironment.id == payload.environment_id
        ).first()
        if env_row:
            env = env_row.name

    _AREA_MESSAGES = {
        "wait_events":        "Querying V$SYSTEM_EVENT for top wait events...",
        "top_sql":            "Querying V$SQLAREA for top SQL by elapsed time...",
        "memory":             "Analyzing SGA / PGA memory configuration...",
        "locks":              "Checking blocking sessions and lock contention...",
        "tablespace":         "Scanning DBA_TABLESPACE_USAGE_METRICS...",
        "concurrent_manager": "Analyzing Concurrent Manager queue depth...",
        "statistics":         "Checking stale object statistics and invalid objects...",
    }

    async def stream():
        yield _sse({"type": "phase", "content": f"🔍 Connecting to {env} — running Oracle diagnostic queries..."})
        await asyncio.sleep(0.05)

        diagnostics = {}
        data_sources = {}
        for area in areas:
            if area not in _AREA_MESSAGES:
                continue
            yield _sse({"type": "progress", "area": area, "content": _AREA_MESSAGES[area]})
            await asyncio.sleep(0.25)
            diagnostics[area], data_sources[area] = ps.run_area(area, env_row, env)

        yield _sse({"type": "diagnostics", "data": diagnostics, "data_sources": data_sources})
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


# ── Ask (free-text, NL→SQL fallback + RAG-grounded general advice) ────────────

class AskRequest(BaseModel):
    question: str
    environment: Optional[str] = "EBS"


@router.post("/ask")
async def ask(
    payload: AskRequest,
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
        route_text="answer Oracle EBS DBA/performance questions and interpret live diagnostic data",
    )
    question = (payload.question or "").strip()

    async def stream():
        if not question:
            yield _sse({"type": "error", "content": "Please enter a question."})
            return

        env_row = None
        if payload.environment:
            env_row = db.query(models.EbsEnvironment).filter(
                models.EbsEnvironment.name == payload.environment,
                models.EbsEnvironment.is_active == True,   # noqa: E712
            ).first()

        from app.modules.dba.nl_sql import nl_sql_service
        nlsql_result = None
        if env_row is not None and nl_sql_service.looks_like_data_question(question):
            yield _sse({"type": "phase", "content": "🔎 Data question detected — trying NL→SQL..."})
            nlsql_result = nl_sql_service.try_answer_data_question(db, current_user, env_row, question)
            if nlsql_result is None:
                yield _sse({"type": "phase", "content": "↩️ NL→SQL unavailable for this question — falling back to general advice."})

        if nlsql_result is not None:
            yield _sse({"type": "data", "source": "nl_sql", "sql": nlsql_result["sql"],
                        "rows": nlsql_result["rows"]})
            messages = nl_sql_service.build_interpret_messages(question, nlsql_result)
            endpoint = "performance.ask.nl_sql"
        else:
            # Ground in the knowledge base when there's a confident match (web fallback off).
            rag_context = ""
            try:
                rag_context = rag_service.query_rag(question, allow_web_fallback=False)
            except Exception:
                rag_context = ""
            if rag_context:
                yield _sse({"type": "phase", "content": "📚 Grounding answer in the knowledge base..."})

            system = prompts.get_prompt("performance.ask")
            user_content = question if not rag_context else (
                f"[KNOWLEDGE BASE]\n{rag_context}\n[END KNOWLEDGE BASE]\n\n"
                "Answer the QUESTION using the knowledge base above as the authoritative source and "
                "cite the source filename for specifics. If it isn't covered, say so and give only "
                f"well-established Oracle DBA guidance.\n\nQUESTION: {question}"
            )
            messages = [{"role": "system", "content": system}, {"role": "user", "content": user_content}]
            endpoint = "performance.ask"

        full_analysis: list[str] = []
        usage: dict = {}
        try:
            async for token in llm_service.stream_tokens(messages, provider, model, api_key, base_url, usage=usage):
                full_analysis.append(token)
                yield _sse({"type": "analysis_token", "content": token})
        except Exception as exc:
            yield _sse({"type": "error", "content": f"AI answer failed: {exc}"})
            return

        _record_perf_tokens(current_user, provider, model, messages, full_analysis, usage, endpoint=endpoint)
        yield _sse({"type": "analysis_complete", "content": "".join(full_analysis)})
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
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
