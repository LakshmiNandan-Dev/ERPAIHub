"""
Admin monitoring API — live telemetry for the Admin Console Monitoring tab.
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query

from app import models
from app.core import telemetry
from app.core.auth.auth import get_current_admin

router = APIRouter(prefix="/admin/monitoring", tags=["Monitoring"])

_DT_DESC = "ISO 8601 datetime, e.g. 2026-06-14T00:00"


@router.get("/overview")
def overview(_: models.User = Depends(get_current_admin)):
    return telemetry.get_overview()


@router.get("/tasks")
def tasks(limit: int = 100, offset: int = 0,
          date_from: Optional[datetime] = Query(None, description=_DT_DESC),
          date_to: Optional[datetime] = Query(None, description=_DT_DESC),
          _: models.User = Depends(get_current_admin)):
    """Drill-down for 'Tasks Executed': each LLM call with its in/out tokens and
    limit, optionally restricted to a [date_from, date_to] window."""
    return telemetry.get_tasks(limit=limit, offset=offset,
                               date_from=date_from, date_to=date_to)


@router.get("/users")
def users(_: models.User = Depends(get_current_admin)):
    return {"users": telemetry.get_active_users()}


@router.get("/quality")
def quality(_: models.User = Depends(get_current_admin)):
    """RAG/LLM accuracy signals: automated RLAIF audit + human feedback."""
    return telemetry.get_quality()
