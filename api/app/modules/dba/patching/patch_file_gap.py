from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks
from sqlalchemy.orm import Session

from app import models, schemas
from app.core import database
from app.core.auth.auth import get_current_user, require_agent
from app.modules.dba.patching import patch_file_gap_service

router = APIRouter(prefix="/patching/gap/files", tags=["Patch Gap Analysis"])


@router.post("/scan/{config_id}", status_code=status.HTTP_202_ACCEPTED)
def trigger_file_gap_scan(config_id: int, background_tasks: BackgroundTasks,
                          db: Session = Depends(database.get_db),
                          current_user: models.User = Depends(require_agent("patching"))):
    config = db.query(models.PatchFileScanConfig).filter(models.PatchFileScanConfig.id == config_id).first()
    if not config:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File scan config not found")
    background_tasks.add_task(patch_file_gap_service.run_file_gap_scan, config_id, current_user.id)
    return {"status": "scan_started", "config_id": config_id}


@router.get("/inventory/{environment_id}", response_model=schemas.PatchFileInventoryPage)
def list_file_inventory(environment_id: int, product: Optional[str] = None, filename: Optional[str] = None,
                        match_status: Optional[str] = None, limit: int = 100, offset: int = 0,
                        db: Session = Depends(database.get_db),
                        _: models.User = Depends(get_current_user)):
    env = db.query(models.EbsEnvironment).filter(models.EbsEnvironment.id == environment_id).first()
    if not env:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Environment not found")
    q = db.query(models.PatchFileInventory).filter(models.PatchFileInventory.environment_id == environment_id)
    if product:
        q = q.filter(models.PatchFileInventory.product == product)
    if filename:
        q = q.filter(models.PatchFileInventory.filename.ilike(f"%{filename}%"))
    if match_status:
        q = q.filter(models.PatchFileInventory.match_status == match_status)
    total = q.count()
    limit = max(1, min(limit, 500))
    rows = q.order_by(models.PatchFileInventory.product.asc(), models.PatchFileInventory.filename.asc()) \
            .limit(limit).offset(max(0, offset)).all()
    return {"total": total, "items": rows}
