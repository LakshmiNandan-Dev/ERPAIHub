"""
Patch gap analysis — compares a live "applied patches" inventory (scanned via
patch_exec.live_opatch_list_all) against an admin-defined target patch list
per environment/component (PatchTarget), surfacing missing patches as
Findings through the same DiagnosticRun/Finding pipeline the performance
monitoring scan uses (models/monitoring.py, monitoring_service.py).

v1 scope: on-demand only (triggered via BackgroundTasks from the router), not
scheduled — reuses the shared findings pipeline without taking on scheduler
complexity for this feature.
"""
from datetime import datetime, timezone

from app import models
from app.core import crypto, database
from app.modules.dba.patching import patch_exec
from app.modules.dba.performance import monitoring_service


def _live_ad_bugs_check(env_row, patch_numbers: list) -> dict:
    """EBS application-tier patch check — live SQL against AD_BUGS (bug fixes/
    one-offs/RUPs applied via adpatch/adop at the product layer aren't
    recorded in any Oracle Home's OPatch inventory at all, only here).
    Returns {"patches": [...bug numbers found present...]} — same shape as
    patch_exec.live_opatch_list_all so run_gap_scan's downstream diff logic
    doesn't care which source produced it — or {"error": ...}."""
    if not (env_row.db_host and env_row.db_sid and env_row.db_user):
        return {"error": "no DB connection configured for this environment"}
    if not patch_numbers:
        return {"patches": []}
    try:
        import oracledb
        dsn = oracledb.makedsn(env_row.db_host, env_row.db_port or 1521, sid=env_row.db_sid)
        conn = oracledb.connect(user=env_row.db_user, password=crypto.decrypt(env_row.db_password_enc),
                                dsn=dsn, tcp_connect_timeout=8)
        try:
            cur = conn.cursor()
            binds = {f"p{i}": n for i, n in enumerate(patch_numbers)}
            placeholders = ", ".join(f":{k}" for k in binds)
            cur.execute(f"SELECT bug_number FROM ad_bugs WHERE bug_number IN ({placeholders})", binds)
            return {"patches": [str(r[0]) for r in cur.fetchall()]}
        finally:
            conn.close()
    except Exception as exc:
        return {"error": str(exc)[:300]}


def compute_gaps(db, environment_id: int) -> list:
    """[{component, home_path, target_patches, applied_patches, missing,
    last_scanned_at, scan_status}] — latest AppliedPatchSnapshot per
    component joined against PatchTarget."""
    targets = db.query(models.PatchTarget).filter(
        models.PatchTarget.environment_id == environment_id
    ).order_by(models.PatchTarget.component.asc()).all()

    out = []
    for target in targets:
        snapshot = db.query(models.AppliedPatchSnapshot).filter(
            models.AppliedPatchSnapshot.environment_id == environment_id,
            models.AppliedPatchSnapshot.component == target.component,
        ).order_by(models.AppliedPatchSnapshot.scanned_at.desc()).first()

        applied = set((snapshot.applied_patches or [])) if snapshot else set()
        target_numbers = [t.get("patch_number") for t in (target.target_patches or []) if t.get("patch_number")]
        missing = [p for p in target_numbers if p not in applied]

        out.append({
            "component": target.component,
            "home_path": target.home_path,
            "target_patches": target.target_patches or [],
            "applied_patches": sorted(applied),
            "missing": missing,
            "last_scanned_at": snapshot.scanned_at if snapshot else None,
            "scan_status": snapshot.scan_status if snapshot else "never_scanned",
        })
    return out


def _gap_findings(component: str, target_patches: list, applied: set) -> list:
    findings = []
    for t in target_patches:
        patch_number = t.get("patch_number")
        if not patch_number or patch_number in applied:
            continue
        label = t.get("label") or patch_number
        findings.append({
            "finding_key": f"missing:{patch_number}",
            "severity": "warning",
            "title": f"{component}: patch {label} not applied",
            "detail": {"patch_number": patch_number, "label": label,
                      "required_by": t.get("required_by"), "component": component},
        })
    return findings


def run_gap_scan(environment_id: int, triggered_by: int | None = None) -> None:
    """BackgroundTasks target. Owns its own DB session (mirrors
    monitoring_scheduler.run_scheduled_scan) — never raises."""
    db = database.SessionLocal()
    run = None
    try:
        env = db.query(models.EbsEnvironment).filter(models.EbsEnvironment.id == environment_id).first()
        if not env:
            return

        targets = db.query(models.PatchTarget).filter(
            models.PatchTarget.environment_id == environment_id
        ).all()
        components = [t.component for t in targets]

        run = models.DiagnosticRun(
            environment_id=env.id, environment_name=env.name, kind="patch_gap",
            trigger="manual", areas=components, status="running",
        )
        db.add(run)
        db.flush()

        creds = patch_exec.resolve_creds(env, db)
        sources_seen = set()

        for target in targets:
            if target.component == "adop":
                patch_numbers = [t.get("patch_number") for t in (target.target_patches or []) if t.get("patch_number")]
                result = _live_ad_bugs_check(env, patch_numbers)
            else:
                result = patch_exec.live_opatch_list_all(creds, target.home_path or "")
            snapshot = models.AppliedPatchSnapshot(
                environment_id=env.id, component=target.component, home_path=target.home_path,
                scanned_by=triggered_by,
            )
            if "error" in result:
                snapshot.scan_status = "error"
                snapshot.scan_error = result["error"][:500]
                snapshot.applied_patches = []
                sources_seen.add("error")
            else:
                snapshot.scan_status = "ok"
                snapshot.applied_patches = result.get("patches", [])
                snapshot.raw_output = (result.get("output") or "")[:8000]
                sources_seen.add("live")
            db.add(snapshot)

            applied = set(snapshot.applied_patches or [])
            extracted = _gap_findings(target.component, target.target_patches or [], applied)
            monitoring_service.upsert_findings(db, run, env.id, env.name, "patch_gap", target.component, extracted)

        monitoring_service.resolve_stale_findings(db, run, env.id, "patch_gap", components)

        run.data_source = "live" if sources_seen == {"live"} else (
            "error" if sources_seen == {"error"} else "mixed")
        run.status = "completed"
        run.completed_at = datetime.now(timezone.utc)
        db.commit()

    except Exception as exc:
        db.rollback()
        try:
            if run is not None:
                run.status = "failed"
                run.error = str(exc)[:2000]
                run.completed_at = datetime.now(timezone.utc)
                db.commit()
        except Exception:
            db.rollback()
    finally:
        db.close()
