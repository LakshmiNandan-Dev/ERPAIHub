"""
Oracle EBS R12 HCM / Payroll Functional Agent — read-only advisor + inquiry.

Two read-only capabilities, both streamed over SSE:
  • /hcm/inquiry — run a fixed, parameterized SELECT from the inquiry catalog
    (live via oracledb, or a deterministic simulator when no DB is configured)
    and stream an AI interpretation with an HCM/Payroll functional lens.
  • /hcm/ask     — free-text HCM/Payroll functional question, RAG-grounded.

Modelled on app.modules.dba.performance.performance_agent (read-only, SSE,
simulator fallback); the live inquiry path reuses oracledb thin-mode like
app.modules.dba.deployment.deployments._execute_sql_directly.
"""
import json
import asyncio
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional

from app.core.auth.auth import get_current_user, require_agent
from app import models
from app.core.llm import llm_service
from app.core.rag import rag_service
from app.core import database, config_service, telemetry, prompts
from app.modules.functional.hcm import inquiries, db as hcm_db

router = APIRouter(prefix="/hcm", tags=["HCM Functional Agent"])

_ROUTE_TEXT = "answer Oracle EBS HCM and Payroll functional questions and interpret HR/payroll inquiry data"


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, default=str)}\n\n"


def _record_tokens(user, provider, model, endpoint, messages, output, usage):
    """Fire-and-forget token telemetry for an HCM agent run."""
    try:
        prompt_text = "".join(m.get("content", "") for m in messages)
        pt = usage.get("prompt_tokens") or telemetry.est_tokens(prompt_text)
        ct = usage.get("completion_tokens") or telemetry.est_tokens("".join(output))
        asyncio.get_event_loop().run_in_executor(
            None, telemetry.record_llm,
            user.id, user.username, endpoint, provider, model, pt, ct, len(prompt_text),
        )
    except Exception:
        pass


def _resolve_llm(request: Request, db: Session):
    provider = request.headers.get("X-LLM-Provider", "ollama")
    model = request.headers.get("X-LLM-Model") or None
    api_key = request.headers.get("X-LLM-Api-Key") or None
    base_url = request.headers.get("X-LLM-Base-Url") or None
    return config_service.resolve_llm(
        provider, model, api_key, base_url, db, agent="hcm", route_text=_ROUTE_TEXT,
    )


# ── Inquiry ─────────────────────────────────────────────────────────────────────

class InquiryRequest(BaseModel):
    inquiry_id: str
    params: dict = {}
    environment: Optional[str] = None
    interpret: bool = True
    # Optional inline connection (else a stored EbsEnvironment, else simulator)
    db_host: Optional[str] = None
    db_port: Optional[int] = 1521
    db_sid: Optional[str] = None
    db_user: Optional[str] = None
    db_password: Optional[str] = None


def _build_inquiry_prompt(inquiry: dict, rows: list, source: str) -> list:
    system = prompts.get_prompt("hcm.inquiry_summarize")
    data_text = json.dumps(rows, indent=2, default=str)
    if len(data_text) > 6000:
        data_text = data_text[:6000] + "\n...[truncated]"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": (
            f"Inquiry: **{inquiry['label']}** — {inquiry['description']}\n"
            f"Data source: {source}\nRows returned: {len(rows)}\n\n"
            f"```json\n{data_text}\n```\n\n"
            "Interpret these results for an Oracle EBS HCM/Payroll functional consultant: "
            "summarise what they show, flag anything notable (e.g. errored payroll actions, "
            "suspended assignments), and suggest read-only next checks. Do not invent rows."
        )},
    ]


@router.get("/inquiries")
async def list_inquiries(current_user: models.User = Depends(get_current_user)):
    """Read-only inquiry catalog (id, label, description, params)."""
    return {"inquiries": inquiries.catalog()}


@router.post("/inquiry")
async def run_inquiry(
    payload: InquiryRequest,
    request: Request,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(require_agent("hcm")),
):
    provider, model, api_key, base_url = _resolve_llm(request, db)

    inquiry = inquiries.get(payload.inquiry_id)
    env = payload.environment or "EBS"

    async def stream():
        if not inquiry:
            yield _sse({"type": "error", "content": f"Unknown inquiry '{payload.inquiry_id}'."})
            return

        # Validate/normalize params (surface missing required ones clearly).
        try:
            binds = inquiries.normalize_params(inquiry, payload.params)
        except ValueError as exc:
            yield _sse({"type": "error", "content": str(exc)})
            return

        # Resolve a live connection, else fall back to the deterministic simulator.
        conn = hcm_db.resolve_conn(db, payload.environment, payload)
        source = "simulated"
        rows = []
        if conn:
            yield _sse({"type": "phase", "content": f"🔌 Connecting to {env} (read-only) — running inquiry..."})
            await asyncio.sleep(0.05)
            try:
                rows = await asyncio.get_event_loop().run_in_executor(
                    None, hcm_db.run_inquiry_live, conn, inquiry["sql"], binds
                )
                source = "live"
            except Exception as exc:
                yield _sse({"type": "phase", "content": f"⚠️ Live query failed ({exc}); using simulated data."})
                rows = inquiries.simulate(inquiry["id"], env, binds)
        else:
            yield _sse({"type": "phase", "content": "🧪 No database configured — using simulated HCM data."})
            await asyncio.sleep(0.05)
            rows = inquiries.simulate(inquiry["id"], env, binds)

        yield _sse({"type": "data", "inquiry": inquiry["id"], "label": inquiry["label"],
                    "source": source, "rows": rows})

        if not payload.interpret:
            yield "data: [DONE]\n\n"
            return

        yield _sse({"type": "phase", "content": "🤖 Interpreting results..."})
        messages = _build_inquiry_prompt(inquiry, rows, source)
        out, usage = [], {}
        try:
            async for token in llm_service.stream_tokens(messages, provider, model, api_key, base_url, usage=usage):
                out.append(token)
                yield _sse({"type": "analysis_token", "content": token})
        except Exception as exc:
            yield _sse({"type": "error", "content": f"AI interpretation failed: {exc}"})
            return

        _record_tokens(current_user, provider, model, "hcm.inquiry", messages, out, usage)
        yield _sse({"type": "analysis_complete", "content": "".join(out)})
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })


# ── Advisor (free-text, RAG-grounded) ───────────────────────────────────────────

class AskRequest(BaseModel):
    question: str


@router.post("/ask")
async def ask(
    payload: AskRequest,
    request: Request,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(require_agent("hcm")),
):
    provider, model, api_key, base_url = _resolve_llm(request, db)
    question = (payload.question or "").strip()

    async def stream():
        if not question:
            yield _sse({"type": "error", "content": "Please enter a question."})
            return

        # Ground in the knowledge base when there's a confident match (web fallback off).
        rag_context = ""
        try:
            rag_context = rag_service.query_rag(question, allow_web_fallback=False)
        except Exception:
            rag_context = ""
        if rag_context:
            yield _sse({"type": "phase", "content": "📚 Grounding answer in the knowledge base..."})

        system = prompts.get_prompt("hcm.system")
        user_content = question if not rag_context else (
            f"[KNOWLEDGE BASE]\n{rag_context}\n[END KNOWLEDGE BASE]\n\n"
            "Answer the QUESTION using the knowledge base above as the authoritative source and "
            "cite the source filename for specifics. If it isn't covered, say so and give only "
            f"well-established EBS HCM guidance.\n\nQUESTION: {question}"
        )
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user_content}]

        out, usage = [], {}
        try:
            async for token in llm_service.stream_tokens(messages, provider, model, api_key, base_url, usage=usage):
                out.append(token)
                yield _sse({"type": "analysis_token", "content": token})
        except Exception as exc:
            yield _sse({"type": "error", "content": f"AI answer failed: {exc}"})
            return

        _record_tokens(current_user, provider, model, "hcm.ask", messages, out, usage)
        yield _sse({"type": "analysis_complete", "content": "".join(out)})
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })
