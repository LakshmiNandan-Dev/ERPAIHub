from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app import models, schemas
from app.core import database
from app.core.auth.auth import get_current_user, get_current_admin, effective_agents
from app.modules.dba.tickets import tickets_service

router = APIRouter(prefix="/tickets", tags=["Tickets"])

# Which gated agent a Finding's source maps to — reuses the existing
# GATED_AGENTS grants (core/auth/auth.py), no new agent name needed.
_SOURCE_AGENT = {"performance": "performance", "patch_gap": "patching"}


@router.post("/", response_model=schemas.TicketOut, status_code=status.HTTP_201_CREATED)
def create_ticket(payload: schemas.TicketCreate, db: Session = Depends(database.get_db),
                  current_user: models.User = Depends(get_current_user)):
    ticket = models.Ticket(
        title=payload.title, description=payload.description,
        priority=payload.priority or "medium", environment_id=payload.environment_id,
        created_by=current_user.id, assignee_id=payload.assignee_id,
    )
    if payload.environment_id:
        env = db.query(models.EbsEnvironment).filter(models.EbsEnvironment.id == payload.environment_id).first()
        if env:
            ticket.environment_name = env.name
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return ticket


@router.post("/from-finding/{finding_id}", response_model=schemas.TicketOut, status_code=status.HTTP_201_CREATED)
def create_ticket_from_finding(finding_id: int, payload: schemas.TicketFromFinding,
                               db: Session = Depends(database.get_db),
                               current_user: models.User = Depends(get_current_user)):
    finding = db.query(models.Finding).filter(models.Finding.id == finding_id).first()
    if not finding:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Finding not found")

    required_agent = _SOURCE_AGENT.get(finding.source)
    if required_agent and required_agent not in effective_agents(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Your role does not grant access to the {required_agent} agent. Contact an administrator.",
        )

    ticket = tickets_service.create_from_finding(db, finding, payload, current_user)
    db.commit()
    db.refresh(ticket)
    return ticket


@router.get("/", response_model=list[schemas.TicketOut])
def list_tickets(status_filter: Optional[str] = None, assignee_id: Optional[int] = None,
                 environment_id: Optional[int] = None, priority: Optional[str] = None,
                 db: Session = Depends(database.get_db),
                 _: models.User = Depends(get_current_user)):
    q = db.query(models.Ticket)
    if status_filter:
        q = q.filter(models.Ticket.status == status_filter)
    if assignee_id is not None:
        q = q.filter(models.Ticket.assignee_id == assignee_id)
    if environment_id is not None:
        q = q.filter(models.Ticket.environment_id == environment_id)
    if priority:
        q = q.filter(models.Ticket.priority == priority)
    return q.order_by(models.Ticket.created_at.desc(), models.Ticket.id.desc()).limit(500).all()


@router.get("/{ticket_id}", response_model=schemas.TicketOut)
def get_ticket(ticket_id: int, db: Session = Depends(database.get_db),
              _: models.User = Depends(get_current_user)):
    ticket = db.query(models.Ticket).filter(models.Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    return ticket


@router.patch("/{ticket_id}", response_model=schemas.TicketOut)
def update_ticket(ticket_id: int, payload: schemas.TicketUpdate,
                  db: Session = Depends(database.get_db),
                  current_user: models.User = Depends(get_current_user)):
    ticket = db.query(models.Ticket).filter(models.Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")

    data = payload.model_dump(exclude_unset=True)
    new_status = data.pop("status", None)
    resolution_notes = data.pop("resolution_notes", None)

    for key, val in data.items():
        setattr(ticket, key, val)

    if new_status and new_status != ticket.status:
        try:
            tickets_service.transition(db, ticket, new_status, resolution_notes, current_user)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    elif resolution_notes is not None:
        ticket.resolution_notes = resolution_notes

    db.commit()
    db.refresh(ticket)
    return ticket


@router.delete("/{ticket_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_ticket(ticket_id: int, db: Session = Depends(database.get_db),
                  _: models.User = Depends(get_current_admin)):
    ticket = db.query(models.Ticket).filter(models.Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    db.delete(ticket)
    db.commit()
