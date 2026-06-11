"""
Audit report API (admin-only).

Surfaces the clone audit trail, user auth events, and agent invocations.
All list endpoints accept optional query-parameter filters; multiple filters
combine with AND logic.  CSV export endpoints accept the same filters.
"""
import io
import csv
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from .. import database, models, audit_service
from .auth import require_approver

router = APIRouter(prefix="/admin/audit", tags=["Audit"])

_DT_DESC = "ISO 8601 date/time, e.g. 2026-06-01T00:00:00"


# ── helpers ───────────────────────────────────────────────────────────────────

def _event_rows(
    db: Session,
    event_types: list,
    *,
    username: Optional[str] = None,
    agent: Optional[str] = None,
    ip: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
) -> list[dict]:
    q = db.query(models.AuditLog).filter(models.AuditLog.event_type.in_(event_types))
    if username:
        q = q.filter(models.AuditLog.username.ilike(f"%{username}%"))
    if agent:
        q = q.filter(models.AuditLog.agent.ilike(f"%{agent}%"))
    if ip:
        q = q.filter(models.AuditLog.ip_address.ilike(f"%{ip}%"))
    if date_from:
        q = q.filter(models.AuditLog.created_at >= date_from)
    if date_to:
        q = q.filter(models.AuditLog.created_at <= date_to)
    rows = q.order_by(models.AuditLog.created_at.desc()).limit(2000).all()
    return [{
        "id": r.id,
        "event_type": r.event_type,
        "user": r.username,
        "agent": r.agent,
        "ip": r.ip_address,
        "machine": audit_service.platform_of(r.user_agent),
        "user_agent": r.user_agent,
        "method": r.method,
        "path": r.path,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]


def _events_csv(rows: list[dict], cols: list[str]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow([r.get(c) for c in cols])
    return buf.getvalue()


def _clone_rows(
    db: Session,
    *,
    username: Optional[str] = None,
    guard_status: Optional[str] = None,
    status: Optional[str] = None,
    source: Optional[str] = None,
    target: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
) -> list[dict]:
    q = db.query(models.CloneRun)
    if guard_status:
        q = q.filter(models.CloneRun.guard_status == guard_status)
    if status:
        q = q.filter(models.CloneRun.status == status)
    if source:
        q = q.filter(models.CloneRun.source_name.ilike(f"%{source}%"))
    if target:
        q = q.filter(models.CloneRun.target_name.ilike(f"%{target}%"))
    if date_from:
        q = q.filter(models.CloneRun.created_at >= date_from)
    if date_to:
        q = q.filter(models.CloneRun.created_at <= date_to)

    runs = q.order_by(models.CloneRun.created_at.desc()).all()

    if username:
        matched_ids = {
            uid for uid, uname in
            db.query(models.User.id, models.User.username)
            .filter(models.User.username.ilike(f"%{username}%")).all()
        }
        runs = [r for r in runs if r.user_id in matched_ids]

    uids = {r.user_id for r in runs} | {r.overridden_by for r in runs if r.overridden_by}
    umap = dict(db.query(models.User.id, models.User.username)
                .filter(models.User.id.in_(uids)).all()) if uids else {}

    out = []
    for r in runs:
        gr = r.guard_result if isinstance(r.guard_result, dict) else {}
        out.append({
            "id": r.id,
            "requested_by": umap.get(r.user_id),
            "requested_by_id": r.user_id,
            "source": r.source_name,
            "target": r.target_name,
            "target_sid": r.target_sid,
            "target_db_host": r.target_db_host,
            "method": r.db_method,
            "status": r.status,
            "guard_status": r.guard_status,
            "guard_reasons": gr.get("reasons", []),
            "override_reason": r.override_reason,
            "overridden_by": umap.get(r.overridden_by) if r.overridden_by else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        })
    return out


# ── User activity ─────────────────────────────────────────────────────────────

@router.get("/users")
def user_audit(
    username: Optional[str] = Query(None, description="Partial username match"),
    event_type: Optional[List[str]] = Query(None, description="login | logout | login_failed (repeatable)"),
    ip: Optional[str] = Query(None, description="Partial IP address match"),
    date_from: Optional[datetime] = Query(None, description=_DT_DESC),
    date_to: Optional[datetime] = Query(None, description=_DT_DESC),
    db: Session = Depends(database.get_db),
    _: models.User = Depends(require_approver),
):
    types = event_type or ["login", "logout", "login_failed"]
    return _event_rows(db, types, username=username, ip=ip, date_from=date_from, date_to=date_to)


@router.get("/users.csv")
def user_audit_csv(
    username: Optional[str] = Query(None),
    event_type: Optional[List[str]] = Query(None),
    ip: Optional[str] = Query(None),
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    db: Session = Depends(database.get_db),
    _: models.User = Depends(require_approver),
):
    types = event_type or ["login", "logout", "login_failed"]
    rows = _event_rows(db, types, username=username, ip=ip, date_from=date_from, date_to=date_to)
    cols = ["created_at", "event_type", "user", "ip", "machine", "user_agent", "path"]
    return Response(
        content=_events_csv(rows, cols),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="user_audit.csv"'},
    )


# ── Agent invocations ─────────────────────────────────────────────────────────

@router.get("/agents")
def agent_audit(
    username: Optional[str] = Query(None, description="Partial username match"),
    agent: Optional[str] = Query(None, description="Agent name (chat, deployment, performance, …)"),
    ip: Optional[str] = Query(None, description="Partial IP address match"),
    date_from: Optional[datetime] = Query(None, description=_DT_DESC),
    date_to: Optional[datetime] = Query(None, description=_DT_DESC),
    db: Session = Depends(database.get_db),
    _: models.User = Depends(require_approver),
):
    return _event_rows(db, ["agent_invoke"],
                       username=username, agent=agent, ip=ip,
                       date_from=date_from, date_to=date_to)


@router.get("/agents.csv")
def agent_audit_csv(
    username: Optional[str] = Query(None),
    agent: Optional[str] = Query(None),
    ip: Optional[str] = Query(None),
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    db: Session = Depends(database.get_db),
    _: models.User = Depends(require_approver),
):
    rows = _event_rows(db, ["agent_invoke"],
                       username=username, agent=agent, ip=ip,
                       date_from=date_from, date_to=date_to)
    cols = ["created_at", "user", "agent", "ip", "machine", "path", "user_agent"]
    return Response(
        content=_events_csv(rows, cols),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="agent_audit.csv"'},
    )


# ── Clone audit trail ─────────────────────────────────────────────────────────

@router.get("/clones")
def clone_audit(
    username: Optional[str] = Query(None, description="Partial requester username match"),
    guard_status: Optional[str] = Query(None, description="passed | blocked | overridden"),
    status: Optional[str] = Query(None, description="pending | completed | failed | rejected"),
    source: Optional[str] = Query(None, description="Partial source environment name match"),
    target: Optional[str] = Query(None, description="Partial target environment name match"),
    date_from: Optional[datetime] = Query(None, description=_DT_DESC),
    date_to: Optional[datetime] = Query(None, description=_DT_DESC),
    db: Session = Depends(database.get_db),
    _: models.User = Depends(require_approver),
):
    return _clone_rows(db,
                       username=username, guard_status=guard_status, status=status,
                       source=source, target=target, date_from=date_from, date_to=date_to)


@router.get("/clones.csv")
def clone_audit_csv(
    username: Optional[str] = Query(None),
    guard_status: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    target: Optional[str] = Query(None),
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    db: Session = Depends(database.get_db),
    _: models.User = Depends(require_approver),
):
    rows = _clone_rows(db,
                       username=username, guard_status=guard_status, status=status,
                       source=source, target=target, date_from=date_from, date_to=date_to)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "requested_by", "source", "target", "target_sid", "target_db_host",
                "method", "status", "guard_status", "guard_reasons",
                "override_reason", "overridden_by", "created_at", "completed_at"])
    for r in rows:
        w.writerow([
            r["id"], r["requested_by"], r["source"], r["target"], r["target_sid"],
            r["target_db_host"], r["method"], r["status"], r["guard_status"],
            " | ".join(r["guard_reasons"]), r["override_reason"], r["overridden_by"],
            r["created_at"], r["completed_at"],
        ])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="clone_audit.csv"'},
    )
