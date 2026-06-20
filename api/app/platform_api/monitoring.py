"""
Admin monitoring API — live telemetry for the Admin Console Monitoring tab.
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

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


@router.get("/rag")
def rag(_: models.User = Depends(get_current_admin)):
    """RAG retrieval (hit/miss/cache/relevance/latency) + grounded-generation metrics."""
    return telemetry.get_rag()


@router.get("/interactions")
def interactions(limit: int = 50, offset: int = 0,
                 date_from: Optional[datetime] = Query(None, description=_DT_DESC),
                 date_to: Optional[datetime] = Query(None, description=_DT_DESC),
                 username: Optional[str] = None,
                 grounded: Optional[bool] = None,
                 rating: Optional[int] = None,
                 _: models.User = Depends(get_current_admin)):
    """Interaction audit trail: one row per chat turn with its query, retrieved
    RAG context, prompt + model version, response, latency and feedback.
    Paginated and filterable by window / user / grounded / feedback rating."""
    return telemetry.get_interactions(limit=limit, offset=offset,
                                      date_from=date_from, date_to=date_to,
                                      username=username, grounded=grounded, rating=rating)


@router.get("/interactions/{interaction_id}")
def interaction_detail(interaction_id: int,
                       _: models.User = Depends(get_current_admin)):
    """Full, untruncated detail for one interaction (query + RAG context + response)."""
    row = telemetry.get_interaction(interaction_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Interaction not found")
    return row
