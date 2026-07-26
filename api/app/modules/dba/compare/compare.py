"""
Environment-to-environment comparison — patch parity, live performance
diagnostics, and configuration drift between two DIFFERENT EBS environments.

Gating is split per dimension (not one blanket gate) since the underlying
GATED_AGENTS grants (deployment/performance/cloning/patching/hcm) are
independent — a DBA with `patching` but not `performance` access is a
realistic role split:
  - GET /compare/patches                        -> require_agent("patching")
  - POST /compare/performance                    -> require_agent("performance")
  - POST /compare/config/scan/{id}, GET /compare/config -> require_agent("performance")
"""
import asyncio
import json
from typing import List
from fastapi import APIRouter, Depends, HTTPException, Request, status, BackgroundTasks
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel

from app import models
from app.core import database, config_service
from app.core.llm import llm_service
from app.core.auth.auth import require_agent
from app.modules.dba.performance import performance_service
from app.modules.dba.compare import compare_service

router = APIRouter(prefix="/compare", tags=["Environment Compare"])


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _get_env_or_404(db: Session, environment_id: int) -> models.EbsEnvironment:
    env = db.query(models.EbsEnvironment).filter(models.EbsEnvironment.id == environment_id).first()
    if not env:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Environment {environment_id} not found")
    return env


# ══════════════════════════════════════════════════════════════════════════
# 1 — Patch parity
# ══════════════════════════════════════════════════════════════════════════

@router.get("/patches")
def compare_patches(environment_a_id: int, environment_b_id: int,
                    db: Session = Depends(database.get_db),
                    _: models.User = Depends(require_agent("patching"))):
    _get_env_or_404(db, environment_a_id)
    _get_env_or_404(db, environment_b_id)
    return compare_service.compute_patch_diff(db, environment_a_id, environment_b_id)


# ══════════════════════════════════════════════════════════════════════════
# 2 — Performance compare
# ══════════════════════════════════════════════════════════════════════════

class EnvCompareRequest(BaseModel):
    environment_a_id: int
    environment_b_id: int
    analysis_areas: List[str] = performance_service.ALL_AREAS


@router.post("/performance")
async def compare_performance(
    payload: EnvCompareRequest,
    request: Request,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(require_agent("performance")),
):
    env_a = _get_env_or_404(db, payload.environment_a_id)
    env_b = _get_env_or_404(db, payload.environment_b_id)

    provider = request.headers.get("X-LLM-Provider", "ollama")
    model    = request.headers.get("X-LLM-Model") or None
    api_key  = request.headers.get("X-LLM-Api-Key") or None
    base_url = request.headers.get("X-LLM-Base-Url") or None
    provider, model, api_key, base_url = config_service.resolve_llm(
        provider, model, api_key, base_url, db, agent="performance",
        route_text="compare Oracle EBS performance diagnostics between two environments",
    )

    areas = [a for a in (payload.analysis_areas or performance_service.ALL_AREAS)
            if a in performance_service.ALL_AREAS]

    async def stream():
        diagnostics_a, sources_a = {}, {}
        diagnostics_b, sources_b = {}, {}

        yield _sse({"type": "phase", "content": f"🔍 Connecting to {env_a.name}..."})
        for area in areas:
            yield _sse({"type": "progress", "environment": "a", "area": area,
                       "content": f"Collecting {area} from {env_a.name}..."})
            await asyncio.sleep(0.05)
            diagnostics_a[area], sources_a[area] = performance_service.run_area(area, env_a, env_a.name)
        yield _sse({"type": "data_a", "data": diagnostics_a, "data_sources": sources_a})

        yield _sse({"type": "phase", "content": f"🔍 Connecting to {env_b.name}..."})
        for area in areas:
            yield _sse({"type": "progress", "environment": "b", "area": area,
                       "content": f"Collecting {area} from {env_b.name}..."})
            await asyncio.sleep(0.05)
            diagnostics_b[area], sources_b[area] = performance_service.run_area(area, env_b, env_b.name)
        yield _sse({"type": "data_b", "data": diagnostics_b, "data_sources": sources_b})

        yield _sse({"type": "phase", "content": "🤖 AI comparing environments..."})
        messages = compare_service.build_env_compare_prompt(env_a.name, env_b.name, diagnostics_a, diagnostics_b)
        full_analysis: list[str] = []
        try:
            async for token in llm_service.stream_tokens(messages, provider, model, api_key, base_url):
                full_analysis.append(token)
                yield _sse({"type": "analysis_token", "content": token})
        except Exception as exc:
            yield _sse({"type": "error", "content": f"AI comparison failed: {exc}"})
            return

        yield _sse({"type": "analysis_complete", "content": "".join(full_analysis)})
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


# ══════════════════════════════════════════════════════════════════════════
# 3 — Configuration drift
# ══════════════════════════════════════════════════════════════════════════

@router.post("/config/scan/{environment_id}", status_code=status.HTTP_202_ACCEPTED)
def trigger_config_scan(environment_id: int, background_tasks: BackgroundTasks,
                        db: Session = Depends(database.get_db),
                        current_user: models.User = Depends(require_agent("performance"))):
    _get_env_or_404(db, environment_id)
    background_tasks.add_task(compare_service.run_config_scan, environment_id, current_user.id)
    return {"status": "scan_started", "environment_id": environment_id}


@router.get("/config")
def compare_config(environment_a_id: int, environment_b_id: int,
                   db: Session = Depends(database.get_db),
                   _: models.User = Depends(require_agent("performance"))):
    _get_env_or_404(db, environment_a_id)
    _get_env_or_404(db, environment_b_id)
    return compare_service.compute_config_diff(db, environment_a_id, environment_b_id)
