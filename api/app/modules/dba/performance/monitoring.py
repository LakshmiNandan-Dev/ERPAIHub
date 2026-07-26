"""
Scheduled, delta-aware monitoring — admin schedule config, on-demand scan
trigger, and the findings/run feed. Distinct from the unrelated
`/admin/monitoring` router (app/platform_api/monitoring.py, LLM/chat usage
telemetry) — this one is diagnostic scan findings, prefixed plain `/monitoring`.
"""
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks
from sqlalchemy.orm import Session
from pydantic import BaseModel

from app import models
from app.core import database
from app.core.auth.auth import get_current_user, get_current_admin, require_agent
from app.core import scheduler as scheduler_module
from app.modules.dba.performance import monitoring_scheduler, performance_service

router = APIRouter(prefix="/monitoring", tags=["Monitoring"])


# ── Schemas ────────────────────────────────────────────────────────────────────

class ScheduleUpdate(BaseModel):
    enabled: bool
    interval_minutes: int = 60
    areas: Optional[List[str]] = None


def _schedule_out(env, schedule) -> dict:
    return {
        "environment_id": env.id,
        "environment_name": env.name,
        "enabled": bool(schedule.enabled) if schedule else False,
        "interval_minutes": schedule.interval_minutes if schedule else 60,
        "areas": schedule.areas if schedule else None,
        "last_run_at": schedule.last_run_at if schedule else None,
        "last_run_status": schedule.last_run_status if schedule else None,
        "last_error": schedule.last_error if schedule else None,
    }


def _finding_out(f) -> dict:
    return {
        "id": f.id, "environment_id": f.environment_id, "environment_name": f.environment_name,
        "source": f.source, "area": f.area, "finding_key": f.finding_key,
        "severity": f.severity, "title": f.title, "detail": f.detail,
        "status": f.status, "first_seen_at": f.first_seen_at, "last_seen_at": f.last_seen_at,
        "last_changed_at": f.last_changed_at, "resolved_at": f.resolved_at,
        "occurrence_count": f.occurrence_count,
    }


def _run_out(r) -> dict:
    return {
        "id": r.id, "environment_id": r.environment_id, "environment_name": r.environment_name,
        "kind": r.kind, "trigger": r.trigger, "areas": r.areas, "data_source": r.data_source,
        "status": r.status, "new_findings_count": r.new_findings_count,
        "changed_findings_count": r.changed_findings_count,
        "resolved_findings_count": r.resolved_findings_count,
        "error": r.error, "created_at": r.created_at, "completed_at": r.completed_at,
    }


# ── Schedule config (admin) ─────────────────────────────────────────────────────

@router.get("/schedules")
def list_schedules(db: Session = Depends(database.get_db),
                   _: models.User = Depends(get_current_admin)):
    envs = db.query(models.EbsEnvironment).order_by(models.EbsEnvironment.name.asc()).all()
    schedules = {s.environment_id: s for s in db.query(models.MonitoringSchedule).all()}
    return [_schedule_out(e, schedules.get(e.id)) for e in envs]


@router.put("/schedules/{environment_id}")
def upsert_schedule(environment_id: int, payload: ScheduleUpdate,
                    db: Session = Depends(database.get_db),
                    admin: models.User = Depends(get_current_admin)):
    env = db.query(models.EbsEnvironment).filter(models.EbsEnvironment.id == environment_id).first()
    if not env:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Environment not found")

    bad_areas = [a for a in (payload.areas or []) if a not in performance_service.ALL_AREAS]
    if bad_areas:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Unknown analysis area(s): {bad_areas}")

    schedule = db.query(models.MonitoringSchedule).filter(
        models.MonitoringSchedule.environment_id == environment_id
    ).first()
    if not schedule:
        schedule = models.MonitoringSchedule(environment_id=environment_id, created_by=admin.id)
        db.add(schedule)

    schedule.enabled = payload.enabled
    schedule.interval_minutes = max(1, payload.interval_minutes)
    schedule.areas = payload.areas or None
    db.commit()
    db.refresh(schedule)

    scheduler_module.reschedule_environment(environment_id, schedule.enabled, schedule.interval_minutes)
    return _schedule_out(env, schedule)


# ── On-demand scan trigger ───────────────────────────────────────────────────────

@router.post("/scan/{environment_id}", status_code=status.HTTP_202_ACCEPTED)
def trigger_scan(environment_id: int, background_tasks: BackgroundTasks,
                 db: Session = Depends(database.get_db),
                 current_user: models.User = Depends(require_agent("performance"))):
    env = db.query(models.EbsEnvironment).filter(models.EbsEnvironment.id == environment_id).first()
    if not env:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Environment not found")
    background_tasks.add_task(monitoring_scheduler.run_scheduled_scan, environment_id, "manual")
    return {"status": "scan_started", "environment_id": environment_id}


# ── Findings / runs feed ─────────────────────────────────────────────────────────

@router.get("/findings")
def list_findings(environment_id: Optional[int] = None, source: Optional[str] = None,
                  area: Optional[str] = None, status_filter: Optional[str] = "open",
                  severity: Optional[str] = None, q: Optional[str] = None,
                  db: Session = Depends(database.get_db),
                  _: models.User = Depends(get_current_user)):
    query = db.query(models.Finding)
    if environment_id is not None:
        query = query.filter(models.Finding.environment_id == environment_id)
    if source:
        query = query.filter(models.Finding.source == source)
    if area:
        query = query.filter(models.Finding.area.ilike(f"%{area}%"))
    if status_filter:
        query = query.filter(models.Finding.status == status_filter)
    if severity:
        query = query.filter(models.Finding.severity == severity)
    if q:
        query = query.filter(models.Finding.title.ilike(f"%{q}%"))
    rows = query.order_by(models.Finding.last_seen_at.desc(), models.Finding.id.desc()).limit(500).all()
    return [_finding_out(f) for f in rows]


@router.get("/findings/{finding_id}")
def get_finding(finding_id: int, db: Session = Depends(database.get_db),
                _: models.User = Depends(get_current_user)):
    f = db.query(models.Finding).filter(models.Finding.id == finding_id).first()
    if not f:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Finding not found")
    return _finding_out(f)


@router.get("/runs")
def list_runs(environment_id: Optional[int] = None, kind: Optional[str] = None,
             db: Session = Depends(database.get_db),
             _: models.User = Depends(get_current_user)):
    q = db.query(models.DiagnosticRun)
    if environment_id is not None:
        q = q.filter(models.DiagnosticRun.environment_id == environment_id)
    if kind:
        q = q.filter(models.DiagnosticRun.kind == kind)
    # created_at ties (server-side now(), possible within one fast transaction
    # or one DB clock tick) need a stable tiebreaker — id always increases in
    # creation order regardless of timestamp resolution.
    rows = q.order_by(models.DiagnosticRun.created_at.desc(), models.DiagnosticRun.id.desc()).limit(200).all()
    return [_run_out(r) for r in rows]


@router.get("/runs/{run_id}")
def get_run(run_id: int, db: Session = Depends(database.get_db),
           _: models.User = Depends(get_current_user)):
    r = db.query(models.DiagnosticRun).filter(models.DiagnosticRun.id == run_id).first()
    if not r:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return _run_out(r)
