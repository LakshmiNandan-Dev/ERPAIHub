"""
Local-model training API (Admin-only, Ollama-only).

Assemble a per-agent dataset from feedback / uploads / RAG / agent runs, emit a
runnable LoRA SFT->DPO bundle, then register the resulting GGUF as an Ollama model
and map it to the agent. Cloud providers are never trained.
"""
import os
import json
from datetime import datetime, timezone
from typing import Optional, List

import httpx
from fastapi import APIRouter, Depends, HTTPException, status, Response, UploadFile, File, Form
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import database, models, training_service
from app.routers.auth import get_current_admin

router = APIRouter(prefix="/training", tags=["Training"])

_OLLAMA_BASE = os.getenv("OLLAMA_URL", "http://localhost:11434")


def _agent_or_400(agent: str) -> str:
    if agent not in training_service.AGENTS:
        raise HTTPException(status_code=400, detail=f"Unknown agent '{agent}'. One of: {', '.join(training_service.AGENTS)}")
    return agent


# ── Base models (local Ollama only) ──────────────────────────────────────────────

@router.get("/base-models")
def base_models(_: models.User = Depends(get_current_admin)):
    """Local Ollama models available as fine-tuning bases. Training is local-only."""
    try:
        with httpx.Client(timeout=10.0) as c:
            r = c.get(f"{_OLLAMA_BASE.rstrip('/')}/api/tags")
            tags = [m["name"] for m in (r.json().get("models") or [])] if r.status_code == 200 else []
        return {"provider": "ollama", "models": tags}
    except Exception as exc:
        return {"provider": "ollama", "models": [], "error": str(exc)[:200]}


# ── Examples / dataset ───────────────────────────────────────────────────────────

@router.get("/overview")
def overview(db: Session = Depends(database.get_db), _: models.User = Depends(get_current_admin)):
    out = []
    maps = {m.agent: m for m in db.query(models.AgentModel).all()}
    for a in training_service.AGENTS:
        c = training_service.counts(db, a)
        am = maps.get(a)
        out.append({"agent": a, "counts": c,
                    "active_model": ({"model": am.model, "provider": am.provider} if am else None)})
    return {"agents": out, "sources": list(training_service.SOURCES)}


@router.get("/examples")
def list_examples(agent: str, limit: int = 50,
                  db: Session = Depends(database.get_db), _: models.User = Depends(get_current_admin)):
    _agent_or_400(agent)
    rows = (db.query(models.TrainingExample)
            .filter(models.TrainingExample.agent == agent)
            .order_by(models.TrainingExample.id.desc()).limit(min(limit, 200)).all())
    return {"counts": training_service.counts(db, agent),
            "examples": [{"id": r.id, "kind": r.kind, "source": r.source, "prompt": r.prompt,
                          "chosen": r.chosen, "rejected": r.rejected, "is_active": r.is_active}
                         for r in rows]}


class ExampleIn(BaseModel):
    agent: str
    kind: str = "sft"            # sft | dpo
    prompt: str
    chosen: str
    rejected: Optional[str] = None
    system: Optional[str] = None


@router.post("/examples", status_code=status.HTTP_201_CREATED)
def add_example(payload: ExampleIn, db: Session = Depends(database.get_db),
                admin: models.User = Depends(get_current_admin)):
    _agent_or_400(payload.agent)
    if payload.kind == "dpo" and not (payload.rejected or "").strip():
        raise HTTPException(status_code=400, detail="A DPO example requires a 'rejected' answer.")
    e = models.TrainingExample(
        agent=payload.agent, kind=("dpo" if payload.kind == "dpo" else "sft"), source="upload",
        system=payload.system or training_service.system_for(payload.agent),
        prompt=payload.prompt, chosen=payload.chosen, rejected=payload.rejected, created_by=admin.id)
    db.add(e); db.commit(); db.refresh(e)
    return {"id": e.id}


@router.post("/examples/upload")
def upload_examples(agent: str = Form(...), file: UploadFile = File(...),
                    db: Session = Depends(database.get_db), admin: models.User = Depends(get_current_admin)):
    """Bulk-upload JSONL. SFT rows: {prompt, completion|chosen}. DPO rows: {prompt, chosen, rejected}."""
    _agent_or_400(agent)
    raw = file.file.read().decode("utf-8", "replace")
    added = skipped = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            skipped += 1
            continue
        prompt = (o.get("prompt") or o.get("instruction") or "").strip()
        chosen = (o.get("chosen") or o.get("completion") or o.get("output") or o.get("answer") or "").strip()
        rejected = (o.get("rejected") or "").strip()
        if not prompt or not chosen:
            skipped += 1
            continue
        kind = "dpo" if rejected else "sft"
        db.add(models.TrainingExample(agent=agent, kind=kind, source="upload",
                                      system=o.get("system") or training_service.system_for(agent),
                                      prompt=prompt, chosen=chosen, rejected=rejected or None,
                                      created_by=admin.id))
        added += 1
    db.commit()
    return {"added": added, "skipped": skipped, "counts": training_service.counts(db, agent)}


class AssembleIn(BaseModel):
    agent: str
    sources: List[str] = ["feedback"]


@router.post("/assemble")
def assemble(payload: AssembleIn, db: Session = Depends(database.get_db),
             admin: models.User = Depends(get_current_admin)):
    _agent_or_400(payload.agent)
    res = training_service.assemble(db, payload.agent, payload.sources, created_by=admin.id)
    res["counts"] = training_service.counts(db, payload.agent)
    return res


@router.delete("/examples/{ex_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_example(ex_id: int, db: Session = Depends(database.get_db), _: models.User = Depends(get_current_admin)):
    e = db.query(models.TrainingExample).filter(models.TrainingExample.id == ex_id).first()
    if not e:
        raise HTTPException(status_code=404, detail="Example not found")
    db.delete(e); db.commit()


# ── Jobs ─────────────────────────────────────────────────────────────────────────

def _job_out(j: models.TrainingJob) -> dict:
    return {"id": j.id, "agent": j.agent, "base_model": j.base_model, "method": j.method,
            "target_model_tag": j.target_model_tag, "status": j.status,
            "sft_count": j.sft_count, "dpo_count": j.dpo_count, "notes": j.notes,
            "created_at": j.created_at, "bundled_at": j.bundled_at, "registered_at": j.registered_at}


class JobIn(BaseModel):
    agent: str
    base_model: str
    method: str = "both"        # sft | dpo | both
    target_model_tag: Optional[str] = None
    params: Optional[dict] = None


@router.get("/jobs")
def list_jobs(db: Session = Depends(database.get_db), _: models.User = Depends(get_current_admin)):
    rows = db.query(models.TrainingJob).order_by(models.TrainingJob.id.desc()).all()
    return [_job_out(j) for j in rows]


@router.post("/jobs", status_code=status.HTTP_201_CREATED)
def create_job(payload: JobIn, db: Session = Depends(database.get_db),
               admin: models.User = Depends(get_current_admin)):
    _agent_or_400(payload.agent)
    if "/" in payload.base_model or ":" not in payload.base_model and payload.base_model.startswith("http"):
        raise HTTPException(status_code=400, detail="Base model must be a local Ollama model (training is local-only).")
    c = training_service.counts(db, payload.agent)
    tag = (payload.target_model_tag or f"oraebs-{payload.agent}:v1").strip()
    method = payload.method if payload.method in ("sft", "dpo", "both") else "both"
    if method in ("sft", "both") and c["sft"] == 0 and c["dpo"] == 0:
        raise HTTPException(status_code=400, detail="No training examples for this agent yet — assemble or upload first.")
    j = models.TrainingJob(agent=payload.agent, base_model=payload.base_model, method=method,
                           target_model_tag=tag, status="draft",
                           sft_count=c["sft"], dpo_count=c["dpo"], params=payload.params or {},
                           created_by=admin.id)
    db.add(j); db.commit(); db.refresh(j)
    return _job_out(j)


@router.get("/jobs/{job_id}/bundle")
def download_bundle(job_id: int, db: Session = Depends(database.get_db), _: models.User = Depends(get_current_admin)):
    j = db.query(models.TrainingJob).filter(models.TrainingJob.id == job_id).first()
    if not j:
        raise HTTPException(status_code=404, detail="Job not found")
    examples = (db.query(models.TrainingExample)
                .filter(models.TrainingExample.agent == j.agent,
                        models.TrainingExample.is_active == True).all())  # noqa: E712
    data, name = training_service.build_bundle_zip(j, examples)
    if j.status == "draft":
        j.status = "bundled"; j.bundled_at = datetime.now(timezone.utc); db.commit()
    return Response(content=data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


class RegisterIn(BaseModel):
    gguf_path: str              # path readable by the Ollama host (e.g. on a mounted volume)


@router.post("/jobs/{job_id}/register")
def register_job(job_id: int, payload: RegisterIn, db: Session = Depends(database.get_db),
                 _: models.User = Depends(get_current_admin)):
    j = db.query(models.TrainingJob).filter(models.TrainingJob.id == job_id).first()
    if not j:
        raise HTTPException(status_code=404, detail="Job not found")
    res = training_service.register_in_ollama(_OLLAMA_BASE, j.target_model_tag,
                                              payload.gguf_path.strip(), training_service.system_for(j.agent))
    if not res.get("ok"):
        j.status = "failed"; db.commit()
        raise HTTPException(status_code=502, detail=res.get("message"))
    j.status = "registered"; j.registered_at = datetime.now(timezone.utc)
    # map the agent to the new model
    am = db.query(models.AgentModel).filter(models.AgentModel.agent == j.agent).first()
    if not am:
        am = models.AgentModel(agent=j.agent, provider="ollama", model=j.target_model_tag, training_job_id=j.id)
        db.add(am)
    else:
        am.provider = "ollama"; am.model = j.target_model_tag; am.training_job_id = j.id
        am.updated_at = datetime.now(timezone.utc)
    db.commit()
    return {"ok": True, "message": res["message"], "agent": j.agent, "model": j.target_model_tag}


# ── Per-agent model mapping ──────────────────────────────────────────────────────

@router.get("/agent-models")
def list_agent_models(db: Session = Depends(database.get_db), _: models.User = Depends(get_current_admin)):
    rows = db.query(models.AgentModel).all()
    return [{"agent": m.agent, "provider": m.provider, "model": m.model,
             "training_job_id": m.training_job_id, "is_active": m.is_active} for m in rows]


class AgentModelIn(BaseModel):
    model: str


@router.put("/agent-models/{agent}")
def set_agent_model(agent: str, payload: AgentModelIn, db: Session = Depends(database.get_db),
                    _: models.User = Depends(get_current_admin)):
    _agent_or_400(agent)
    am = db.query(models.AgentModel).filter(models.AgentModel.agent == agent).first()
    if not am:
        am = models.AgentModel(agent=agent, provider="ollama", model=payload.model.strip())
        db.add(am)
    else:
        am.model = payload.model.strip(); am.provider = "ollama"; am.updated_at = datetime.now(timezone.utc)
    db.commit()
    return {"agent": agent, "model": payload.model.strip()}


@router.delete("/agent-models/{agent}", status_code=status.HTTP_204_NO_CONTENT)
def clear_agent_model(agent: str, db: Session = Depends(database.get_db), _: models.User = Depends(get_current_admin)):
    db.query(models.AgentModel).filter(models.AgentModel.agent == agent).delete()
    db.commit()
