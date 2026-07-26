"""
In-process job scheduler for recurring diagnostic scans (Feature: scheduled
delta-aware monitoring).

`api/Dockerfile` runs uvicorn with no `--workers` flag — a single process per
container — so an in-process, thread-based scheduler is safe: no duplicate-run
risk from concurrent workers. If that ever changes (multi-worker or multi-
replica deployment), this needs revisiting (single-owner lock or move
scheduling to one designated replica).

BackgroundScheduler (thread-based), not AsyncIOScheduler: job bodies do
blocking oracledb/paramiko I/O, same as every other background task in this
codebase (e.g. deployments.execute_deployment_task) — running that on the
asyncio event loop would block all concurrent request handling for the
duration of each scan.
"""
from apscheduler.schedulers.background import BackgroundScheduler

scheduler = BackgroundScheduler(timezone="UTC")


def _job_id(environment_id: int) -> str:
    return f"perf_scan_env_{environment_id}"


def start() -> None:
    """Start the scheduler and reconcile jobs from persisted MonitoringSchedule
    rows. Called once from the FastAPI lifespan startup."""
    if scheduler.running:
        return
    scheduler.start()
    _reconcile_from_db()


def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


def _reconcile_from_db() -> None:
    from app.core import database
    from app import models

    db = database.SessionLocal()
    try:
        for row in db.query(models.MonitoringSchedule).filter(
            models.MonitoringSchedule.enabled == True  # noqa: E712
        ).all():
            reschedule_environment(row.environment_id, True, row.interval_minutes)
    finally:
        db.close()


def reschedule_environment(environment_id: int, enabled: bool, interval_minutes: int) -> None:
    """(Re)register or remove the periodic scan job for one environment. Safe
    to call whether or not a job already exists (replace_existing handles the
    update case; existing jobs are removed outright when disabling)."""
    job_id = _job_id(environment_id)
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    if not enabled:
        return

    from app.modules.dba.performance import monitoring_scheduler

    scheduler.add_job(
        monitoring_scheduler.run_scheduled_scan, "interval",
        minutes=max(1, interval_minutes), id=job_id, args=[environment_id],
        replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=300,
    )
