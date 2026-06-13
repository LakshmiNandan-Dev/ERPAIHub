"""
Admin monitoring API — live telemetry for the Admin Console Monitoring tab.
"""
from fastapi import APIRouter, Depends

from app import models, telemetry
from app.routers.auth import get_current_admin

router = APIRouter(prefix="/admin/monitoring", tags=["Monitoring"])


@router.get("/overview")
def overview(_: models.User = Depends(get_current_admin)):
    return telemetry.get_overview()


@router.get("/users")
def users(_: models.User = Depends(get_current_admin)):
    return {"users": telemetry.get_active_users()}


@router.get("/quality")
def quality(_: models.User = Depends(get_current_admin)):
    """RAG/LLM accuracy signals: automated RLAIF audit + human feedback."""
    return telemetry.get_quality()
