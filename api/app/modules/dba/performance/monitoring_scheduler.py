"""
Background diagnostic scan — the job body run either periodically by
core.scheduler (trigger='scheduled') or on-demand via BackgroundTasks from the
POST /monitoring/scan/{environment_id} endpoint (trigger='manual').

Owns its own DB session (mirrors deployments.execute_deployment_task's
background-task pattern) since it runs outside any request's dependency-
injected session. Never raises — every exception is caught and recorded on the
DiagnosticRun / MonitoringSchedule rows so a bad run can't kill the scheduler
thread or silently vanish.
"""
from datetime import datetime, timezone

from app import models
from app.core import database
from app.modules.dba.performance import performance_service, monitoring_service


def run_scheduled_scan(environment_id: int, trigger: str = "scheduled") -> None:
    db = database.SessionLocal()
    run = None
    try:
        env = db.query(models.EbsEnvironment).filter(
            models.EbsEnvironment.id == environment_id
        ).first()
        if not env:
            return

        schedule = db.query(models.MonitoringSchedule).filter(
            models.MonitoringSchedule.environment_id == environment_id
        ).first()
        areas = (schedule.areas if schedule and schedule.areas else None) or performance_service.ALL_AREAS

        run = models.DiagnosticRun(
            environment_id=env.id, environment_name=env.name, kind="performance",
            trigger=trigger, areas=areas, status="running",
        )
        db.add(run)
        db.flush()  # assign run.id for Finding FK references before commit

        sources_seen = set()
        for area in areas:
            if area not in performance_service.ALL_AREAS:
                continue
            data, source = performance_service.run_area(area, env, env.name)
            sources_seen.add(source)
            extracted = performance_service.extract_findings(area, data)
            monitoring_service.upsert_findings(db, run, env.id, env.name, "performance", area, extracted)

        monitoring_service.resolve_stale_findings(db, run, env.id, "performance", areas)

        run.data_source = "live" if sources_seen == {"live"} else (
            "simulated" if sources_seen == {"simulated"} else "mixed")
        run.status = "completed"
        run.completed_at = datetime.now(timezone.utc)

        if schedule:
            schedule.last_run_at = run.completed_at
            schedule.last_run_status = "ok"
            schedule.last_error = None

        db.commit()

    except Exception as exc:
        db.rollback()
        try:
            if run is not None:
                run.status = "failed"
                run.error = str(exc)[:2000]
                run.completed_at = datetime.now(timezone.utc)
            schedule = db.query(models.MonitoringSchedule).filter(
                models.MonitoringSchedule.environment_id == environment_id
            ).first()
            if schedule:
                schedule.last_run_status = "error"
                schedule.last_error = str(exc)[:2000]
            db.commit()
        except Exception:
            db.rollback()
    finally:
        db.close()
