from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks
from sqlalchemy.orm import Session

from app import models
from app.core import database
from app.core.auth.auth import get_current_user, require_agent
from app.modules.dba.patching import patch_gap_service

router = APIRouter(prefix="/patching/gap", tags=["Patch Gap Analysis"])


@router.post("/scan/{environment_id}", status_code=status.HTTP_202_ACCEPTED)
def trigger_gap_scan(environment_id: int, background_tasks: BackgroundTasks,
                     db: Session = Depends(database.get_db),
                     current_user: models.User = Depends(require_agent("patching"))):
    env = db.query(models.EbsEnvironment).filter(models.EbsEnvironment.id == environment_id).first()
    if not env:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Environment not found")
    targets = db.query(models.PatchTarget).filter(models.PatchTarget.environment_id == environment_id).count()
    if not targets:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="No patch targets defined for this environment. Add one in Admin Console → Environments.")
    background_tasks.add_task(patch_gap_service.run_gap_scan, environment_id, current_user.id)
    return {"status": "scan_started", "environment_id": environment_id}


@router.get("/{environment_id}")
def get_gaps(environment_id: int, db: Session = Depends(database.get_db),
            _: models.User = Depends(get_current_user)):
    env = db.query(models.EbsEnvironment).filter(models.EbsEnvironment.id == environment_id).first()
    if not env:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Environment not found")
    return patch_gap_service.compute_gaps(db, environment_id)


@router.get("/{environment_id}/history")
def get_scan_history(environment_id: int, component: str | None = None,
                     db: Session = Depends(database.get_db),
                     _: models.User = Depends(get_current_user)):
    q = db.query(models.AppliedPatchSnapshot).filter(
        models.AppliedPatchSnapshot.environment_id == environment_id
    )
    if component:
        q = q.filter(models.AppliedPatchSnapshot.component == component)
    rows = q.order_by(models.AppliedPatchSnapshot.scanned_at.desc()).limit(100).all()
    return [{
        "id": r.id, "component": r.component, "home_path": r.home_path,
        "applied_patches": r.applied_patches, "scan_status": r.scan_status,
        "scan_error": r.scan_error, "scanned_at": r.scanned_at,
    } for r in rows]
