"""
TinyLLM NL->Oracle-SQL embedding — a per-environment QueryService pool plus the
background extract/fine-tune jobs that feed it.

TinyLLM (../../../../../tinyllm) is a standalone, from-scratch 8M-param
NL->Oracle-SQL model shipped in this same repo. QueryService is single-model
(tinyllm/tinyllm/serve/service.py: self.model/self.tok are ONE instance shared
across every registered schema_id — reload_model() swaps that ONE model
globally). Since each environment can have its own fine-tuned checkpoint, this
module keeps a POOL of QueryService instances keyed by environment_id, each
built lazily from that environment's latest extracted schema + latest
completed training run (or the shipped base checkpoint if none).

The pool caches only {model, tokenizer, schema} — never a live DB
connection/cursor. explain()/execute() always open a fresh, throwaway
oracledb connection per call and close it immediately after, the same
pattern used throughout this codebase (prod_guard, performance_service.
run_area, compare_service.run_config_scan) to avoid a shared connection
being closed out from under a concurrent caller or silently reused past an
idle timeout.
"""
import os
import json
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from tinyllm.serve.service import QueryService, QueryResult
from tinyllm.serve.jobs import TrainJob, _STEP
from tinyllm.db import OracleDb
from tinyllm.extract import MockCatalog, OracleCatalog, EbsExtractor
from tinyllm.schema_graph.serialize import schema_to_dict, schema_from_dict

from app import models
from app.core import database, crypto, prompts

# Defaults match the Docker image layout (api/Dockerfile COPYs tinyllm/ to
# /app/tinyllm, and docker-compose mounts a volume at /app/nlsql_data).
# Override for a non-Docker install, e.g. in api/.env:
#   NLSQL_DIR=/opt/oraebsagent/nlsql_data
#   NLSQL_BASE_CKPT=/opt/oraebsagent/tinyllm/artifacts/model_best.pt
#   NLSQL_BASE_TOK=/opt/oraebsagent/tinyllm/artifacts/tokenizer.json
NLSQL_DIR   = os.getenv("NLSQL_DIR", "/app/nlsql_data")
BASE_CKPT   = os.getenv("NLSQL_BASE_CKPT", "/app/tinyllm/artifacts/model_best.pt")
BASE_TOK    = os.getenv("NLSQL_BASE_TOK", "/app/tinyllm/artifacts/tokenizer.json")
SCHEMAS_DIR = os.path.join(NLSQL_DIR, "schemas")
MODELS_DIR  = os.path.join(NLSQL_DIR, "models")
LOGS_DIR    = os.path.join(NLSQL_DIR, "logs")

_POOL: dict[int, QueryService] = {}
_POOL_LOCK = threading.Lock()


def _schema_id(environment_id: int) -> str:
    return f"env_{environment_id}"


def get_chat_settings(db) -> "models.NlSqlChatSettings":
    """Singleton row (id=1) controlling NL-SQL reply verbosity in the general
    Chat Assistant. Created on first read with its column default (hidden)."""
    row = db.query(models.NlSqlChatSettings).filter(models.NlSqlChatSettings.id == 1).first()
    if not row:
        row = models.NlSqlChatSettings(id=1)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _latest_schema_snapshot(db, environment_id):
    return db.query(models.NlSqlSchemaSnapshot).filter(
        models.NlSqlSchemaSnapshot.environment_id == environment_id,
        models.NlSqlSchemaSnapshot.scan_status == "ok",
    ).order_by(models.NlSqlSchemaSnapshot.scanned_at.desc()).first()


def _latest_done_run(db, environment_id):
    return db.query(models.NlSqlTrainingRun).filter(
        models.NlSqlTrainingRun.environment_id == environment_id,
        models.NlSqlTrainingRun.status == "done",
    ).order_by(models.NlSqlTrainingRun.completed_at.desc()).first()


def _build_service(db, environment_id: int) -> QueryService:
    snap = _latest_schema_snapshot(db, environment_id)
    schema = schema_from_dict(snap.schema_json) if snap else EbsExtractor(MockCatalog()).extract()
    run = _latest_done_run(db, environment_id)
    ckpt = run.checkpoint_path if run else BASE_CKPT
    # Tokenizer is never retrained during fine-tune (BPETokenizer.load(args.tok)
    # is frozen) — every environment always shares the one base tokenizer.
    return QueryService.from_files(ckpt, BASE_TOK, {_schema_id(environment_id): schema},
                                    dbs=None, device="cpu")


def get_service(db, environment_id: int) -> QueryService:
    svc = _POOL.get(environment_id)
    if svc is not None:
        return svc
    with _POOL_LOCK:
        svc = _POOL.get(environment_id)
        if svc is None:
            svc = _build_service(db, environment_id)
            _POOL[environment_id] = svc
        return svc


def invalidate(environment_id: int) -> None:
    """Call after a new extraction (schema changed) or a training run completes."""
    _POOL.pop(environment_id, None)


def _live_conn(env: "models.EbsEnvironment"):
    """Fresh, request-scoped connection — never cached on the pooled service."""
    if not (env.db_host and env.db_sid and env.db_user):
        return None
    try:
        import oracledb
        dsn = oracledb.makedsn(env.db_host, env.db_port or 1521, sid=env.db_sid)
        return oracledb.connect(user=env.db_user, password=crypto.decrypt(env.db_password_enc),
                                dsn=dsn, tcp_connect_timeout=8)
    except Exception:
        return None


def propose(db, env: "models.EbsEnvironment", question: str) -> dict:
    svc = get_service(db, env.id)
    result: QueryResult = svc.query(question, _schema_id(env.id))   # svc.dbs is {} -> explain_ok=None here
    conn = _live_conn(env)
    if conn is not None:
        try:
            gr = OracleDb(conn.cursor()).explain(result.sql)
            result.explain_ok = gr.ok
            if not gr.ok:
                result.note = (result.note + "; " if result.note else "") + f"EXPLAIN rejected: {gr.error}"
        finally:
            conn.close()
    return result.to_dict()


def execute(env: "models.EbsEnvironment", sql: str, max_rows: int = 50) -> list:
    """The CONFIRMED step. Raises ValueError (non-SELECT) or RuntimeError (no
    connection) — the router maps both to HTTP 400."""
    conn = _live_conn(env)
    if conn is None:
        raise RuntimeError(f"no reachable DB connection for environment {env.id}")
    try:
        return OracleDb(conn.cursor()).run_readonly(sql, max_rows=max_rows)
    finally:
        conn.close()


def execute_with_columns(env: "models.EbsEnvironment", sql: str, max_rows: int = 50) -> list[dict]:
    """Same read-only guard + connection lifecycle as execute() (ValueError for
    non-SELECT, RuntimeError with no reachable connection), but zips the
    cursor's column names onto each row for callers that render a table
    (HCM/Performance Ask mode) — execute() itself is unchanged since chat.py's
    confirmed-SQL reply already depends on its raw positional-tuple shape."""
    conn = _live_conn(env)
    if conn is None:
        raise RuntimeError(f"no reachable DB connection for environment {env.id}")
    try:
        cur = conn.cursor()
        rows = OracleDb(cur).run_readonly(sql, max_rows=max_rows)
        cols = [d[0].lower() for d in cur.description] if cur.description else []
        out = []
        for r in rows:
            row = {}
            for i, v in enumerate(r):
                key = cols[i] if i < len(cols) else str(i)
                row[key] = v if (v is None or isinstance(v, (str, int, float, bool))) else str(v)
            out.append(row)
        return out
    finally:
        conn.close()


# ── One-shot data-question fallback for structured agents (HCM /ask,
#    Performance /ask, and any future one-shot agent that wants "use my own
#    hardcoded queries, fall back to NL-SQL when they don't cover the
#    question"). Distinct from chat.py's own multi-turn propose->confirm state
#    machine: these are one-shot request/response SSE endpoints with no chat
#    history, so this auto-executes (read-only, capped rows) instead of
#    asking for confirmation — mirroring the existing "auto-run" convention of
#    HCM's fixed Inquiry mode.
# ─────────────────────────────────────────────────────────────────────────────

DATA_QUESTION_MARKERS = (
    "how many", "how much", "count of", "total number of", "total amount of",
    "list all", "show me all", "show me the top", "top 5", "top 10", "top ten",
    "which vendor", "which supplier", "which customer", "which employee",
    "average ", "sum of",
)


def looks_like_data_question(text: str) -> bool:
    lower = (text or "").lower()
    return any(m in lower for m in DATA_QUESTION_MARKERS)


def try_answer_data_question(db, user: "models.User", env: Optional["models.EbsEnvironment"],
                              question: str) -> Optional[dict]:
    """Returns None whenever NL-SQL doesn't apply — not a data question, no
    resolved environment, the user lacks the 'nl_sql' grant, or the proposed
    SQL can't actually be run (no live connection / execute rejects it — NL-SQL
    has no synthetic-data simulator the way fixed inquiry catalogs do, so
    "can't run it" means "doesn't apply here," not "show fake numbers").
    Callers fall through to their own existing behavior unchanged.

    On success, returns:
        {"sql": str, "rows": list[dict], "row_count": int, "note": str,
         "graph_valid": bool, "explain_ok": Optional[bool], "source": "nl_sql"}
    and audit-logs the invocation (agent="nl_sql") internally.
    """
    if env is None or not looks_like_data_question(question):
        return None

    from app.core.auth.auth import effective_agents
    from app.core.audit import audit_service

    if "nl_sql" not in effective_agents(user):
        return None

    try:
        proposal = propose(db, env, question)
        rows = execute_with_columns(env, proposal["sql"], max_rows=50)
    except Exception:
        return None

    audit_service.log("agent_invoke", agent="nl_sql", user_id=user.id, username=user.username,
                      detail={"question": question, "env": env.name, "sql": proposal["sql"]})
    return {
        "sql": proposal["sql"], "rows": rows, "row_count": len(rows),
        "note": proposal.get("note", ""), "graph_valid": proposal.get("graph_valid"),
        "explain_ok": proposal.get("explain_ok"), "source": "nl_sql",
    }


def build_interpret_messages(question: str, result: dict) -> list[dict]:
    """Shared prompt-builder for narrating an nl_sql fallback answer. Callers
    (hcm_agent.ask, performance_agent.ask, future agents) resolve their own
    provider/model and stream this themselves via their own
    config_service.resolve_llm/llm_service.stream_tokens, so routing and
    telemetry stay agent-scoped — only the prompt text is centralized here."""
    system = prompts.get_prompt("nl_sql.interpret")
    data_text = json.dumps(result["rows"], indent=2, default=str)
    if len(data_text) > 6000:
        data_text = data_text[:6000] + "\n...[truncated]"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": (
            f"Question: {question}\n\nGenerated SQL:\n```sql\n{result['sql']}\n```\n\n"
            f"Rows returned: {result['row_count']}\n\n```json\n{data_text}\n```\n\n"
            "Interpret these results for the person who asked the question. Do not invent rows or "
            "values not present above."
        )},
    ]


# ── Background jobs ──────────────────────────────────────────────────────────

def run_extraction(environment_id: int, triggered_by: int | None,
                   mock: bool = False, extra_owners: list[str] | None = None) -> None:
    """BackgroundTasks target, own session — mirrors compare_service.
    run_config_scan. Never raises. Extraction is a handful of bulk SQL queries
    (OracleCatalog scopes to licensed/shared product owners, not an unbounded
    catalog dump), so it runs to completion within one background call, no
    subprocess needed (unlike training)."""
    db = database.SessionLocal()
    try:
        env = db.query(models.EbsEnvironment).filter(models.EbsEnvironment.id == environment_id).first()
        if not env:
            return
        snap = models.NlSqlSchemaSnapshot(environment_id=environment_id, scanned_by=triggered_by)
        try:
            if mock:
                schema = EbsExtractor(MockCatalog()).extract()
            else:
                conn = _live_conn(env)
                if conn is None:
                    raise RuntimeError("no DB credentials or connection failed")
                try:
                    schema = EbsExtractor(OracleCatalog(conn.cursor(), extra_owners=extra_owners or ())).extract()
                finally:
                    conn.close()
            snap.schema_json = schema_to_dict(schema)
            snap.table_count = len(schema.tables)
            snap.fk_count = len(schema.foreign_keys)
            snap.scan_status = "ok"
            if snap.table_count < 5:
                # SELECT_CATALOG_ROLE missing -> ALL_TABLES/ALL_TAB_COLUMNS silently
                # return fewer rows, not an ORA- error. Flag rather than "succeed" quietly.
                snap.scan_error = (f"Only {snap.table_count} table(s) extracted — the DB user likely "
                                   "lacks SELECT_CATALOG_ROLE for cross-owner visibility.")
        except Exception as exc:
            snap.scan_status, snap.scan_error = "error", str(exc)[:500]
        db.add(snap)
        db.commit()
        invalidate(environment_id)
    except Exception:
        db.rollback()
    finally:
        db.close()


def run_training(environment_id: int, run_id: int, steps: int, n_train: int) -> None:
    """Owns the ENTIRE training lifecycle inside one BackgroundTasks call — the
    background task itself sleep-polls the subprocess to completion and updates
    the DB row, rather than relying on the client to keep polling a status
    endpoint (unlike TinyLLM's own admin console, whose hot-swap only fires if
    someone keeps hitting /admin/train/status). Never raises."""
    db = database.SessionLocal()
    try:
        run = db.query(models.NlSqlTrainingRun).filter(models.NlSqlTrainingRun.id == run_id).first()
        if not run:
            return
        env_id = run.environment_id
        snap = _latest_schema_snapshot(db, env_id)
        if not snap:
            run.status, run.error = "error", "no schema snapshot; run extraction first"
            db.commit()
            return

        Path(SCHEMAS_DIR).mkdir(parents=True, exist_ok=True)
        Path(LOGS_DIR).mkdir(parents=True, exist_ok=True)
        schema_path = os.path.join(SCHEMAS_DIR, f"{env_id}.json")
        Path(schema_path).write_text(json.dumps(snap.schema_json))
        out_dir = os.path.join(MODELS_DIR, str(env_id), str(run_id))
        log_path = os.path.join(LOGS_DIR, f"{env_id}_{run_id}.log")

        run.status, run.log_path, run.schema_snapshot_id = "running", log_path, snap.id
        db.commit()

        job = TrainJob()
        job.start(schema_path=schema_path, schema_id=_schema_id(env_id),
                  init_ckpt=BASE_CKPT, init_tok=BASE_TOK, out_dir=out_dir,
                  steps=steps, n_train=n_train, log_path=log_path)
        while job.running():
            time.sleep(2)

        run = db.query(models.NlSqlTrainingRun).filter(models.NlSqlTrainingRun.id == run_id).first()
        ckpt_out = os.path.join(out_dir, "model_best.pt")
        if job.proc.returncode == 0 and os.path.exists(ckpt_out):
            run.status, run.checkpoint_path = "done", ckpt_out
            invalidate(env_id)
        else:
            run.status, run.error = "error", f"training subprocess exited {job.proc.returncode}"
        run.completed_at = datetime.now(timezone.utc)
        db.commit()
    except Exception as exc:
        db.rollback()
        try:
            db2 = database.SessionLocal()
            r = db2.query(models.NlSqlTrainingRun).filter(models.NlSqlTrainingRun.id == run_id).first()
            if r:
                r.status, r.error = "error", str(exc)[:500]
                db2.commit()
            db2.close()
        except Exception:
            pass
    finally:
        db.close()
