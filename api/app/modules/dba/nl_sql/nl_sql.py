import os
from pathlib import Path
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks
from sqlalchemy.orm import Session
from pydantic import BaseModel

from app import models
from app.core import database
from app.core.auth.auth import require_agent
from app.modules.dba.nl_sql import nl_sql_service
from tinyllm.serve.jobs import _STEP

router = APIRouter(prefix="/nl-sql", tags=["NL to SQL"])


class ExtractRequest(BaseModel):
    mock: bool = False
    extra_owners: List[str] = []


class TrainRequest(BaseModel):
    steps: int = 300
    n_train: int = 800


class QueryRequest(BaseModel):
    environment_id: int
    question: str


class ExecuteRequest(BaseModel):
    environment_id: int
    sql: str
    max_rows: int = 50


def _get_env_or_404(db: Session, environment_id: int) -> models.EbsEnvironment:
    env = db.query(models.EbsEnvironment).filter(models.EbsEnvironment.id == environment_id).first()
    if not env:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Environment {environment_id} not found")
    return env


@router.post("/extract/{environment_id}", status_code=status.HTTP_202_ACCEPTED)
def trigger_extract(environment_id: int, payload: ExtractRequest, background_tasks: BackgroundTasks,
                    db: Session = Depends(database.get_db),
                    current_user: models.User = Depends(require_agent("nl_sql"))):
    _get_env_or_404(db, environment_id)
    background_tasks.add_task(nl_sql_service.run_extraction, environment_id, current_user.id,
                              payload.mock, payload.extra_owners)
    return {"status": "extraction_started", "environment_id": environment_id}


@router.get("/schema/{environment_id}")
def get_schema_status(environment_id: int, db: Session = Depends(database.get_db),
                      _: models.User = Depends(require_agent("nl_sql"))):
    snap = nl_sql_service._latest_schema_snapshot(db, environment_id)
    if not snap:
        # A schema may exist but be status='error' -- check the most recent row of any status.
        snap = db.query(models.NlSqlSchemaSnapshot).filter(
            models.NlSqlSchemaSnapshot.environment_id == environment_id
        ).order_by(models.NlSqlSchemaSnapshot.scanned_at.desc()).first()
        if not snap:
            return {"status": "never_extracted"}
    return {"status": snap.scan_status, "table_count": snap.table_count, "fk_count": snap.fk_count,
           "scan_error": snap.scan_error, "scanned_at": snap.scanned_at}


@router.post("/train/{environment_id}", status_code=status.HTTP_202_ACCEPTED)
def trigger_train(environment_id: int, payload: TrainRequest, background_tasks: BackgroundTasks,
                  db: Session = Depends(database.get_db),
                  current_user: models.User = Depends(require_agent("nl_sql"))):
    _get_env_or_404(db, environment_id)
    if not nl_sql_service._latest_schema_snapshot(db, environment_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="No schema extracted yet for this environment.")
    existing = db.query(models.NlSqlTrainingRun).filter(
        models.NlSqlTrainingRun.environment_id == environment_id,
        models.NlSqlTrainingRun.status.in_(["pending", "running"]),
    ).first()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=f"Training already in progress (run {existing.id}).")
    run = models.NlSqlTrainingRun(environment_id=environment_id, steps=payload.steps,
                                  n_train=payload.n_train, started_by=current_user.id)
    db.add(run)
    db.commit()
    db.refresh(run)
    background_tasks.add_task(nl_sql_service.run_training, environment_id, run.id,
                              payload.steps, payload.n_train)
    return {"status": "training_started", "run_id": run.id}


@router.get("/train/{environment_id}/status")
def get_train_status(environment_id: int, db: Session = Depends(database.get_db),
                     _: models.User = Depends(require_agent("nl_sql"))):
    run = db.query(models.NlSqlTrainingRun).filter(
        models.NlSqlTrainingRun.environment_id == environment_id
    ).order_by(models.NlSqlTrainingRun.started_at.desc()).first()
    if not run:
        return {"status": "never_trained"}
    out = {"run_id": run.id, "status": run.status, "steps": run.steps,
          "started_at": run.started_at, "completed_at": run.completed_at, "error": run.error}
    if run.status == "running" and run.log_path and os.path.exists(run.log_path):
        # Pure read, no side effects (unlike TinyLLM's own poll-driven hot-swap) --
        # completion + pool invalidation are already owned by run_training itself.
        text = Path(run.log_path).read_text(errors="ignore")
        matches = list(_STEP.finditer(text))
        if matches:
            out["current_step"], out["total_steps"] = int(matches[-1].group(1)), int(matches[-1].group(2))
    return out


@router.post("/query")
def query(payload: QueryRequest, db: Session = Depends(database.get_db),
         _: models.User = Depends(require_agent("nl_sql"))):
    env = _get_env_or_404(db, payload.environment_id)
    return nl_sql_service.propose(db, env, payload.question)


@router.post("/execute")
def execute(payload: ExecuteRequest, db: Session = Depends(database.get_db),
           _: models.User = Depends(require_agent("nl_sql"))):
    env = _get_env_or_404(db, payload.environment_id)
    try:
        rows = nl_sql_service.execute(env, payload.sql, payload.max_rows)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return {"rows": rows}
