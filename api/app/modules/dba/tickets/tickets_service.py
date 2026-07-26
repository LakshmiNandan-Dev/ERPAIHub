"""Pure ticket-workflow logic — no FastAPI coupling, mirrors cloning_service.py."""
from datetime import datetime, timezone

from app import models

TERMINAL_STATUSES = {"resolved", "dismissed"}


def create_from_finding(db, finding: "models.Finding", payload, current_user) -> "models.Ticket":
    ticket = models.Ticket(
        title=finding.title,
        description=payload.description,
        priority=payload.priority or "medium",
        environment_id=finding.environment_id,
        environment_name=finding.environment_name,
        finding_id=finding.id,
        source_run_id=finding.last_seen_run_id,
        created_by=current_user.id,
        assignee_id=payload.assignee_id,
    )
    db.add(ticket)
    return ticket


def transition(db, ticket: "models.Ticket", new_status: str, resolution_notes, actor) -> "models.Ticket":
    """Apply a status change, requiring resolution_notes when closing the
    ticket (resolved/dismissed) and stamping resolved_by/resolved_at then."""
    if new_status in TERMINAL_STATUSES and not (resolution_notes or ticket.resolution_notes):
        raise ValueError(f"resolution_notes is required to transition a ticket to '{new_status}'.")
    ticket.status = new_status
    if resolution_notes is not None:
        ticket.resolution_notes = resolution_notes
    if new_status in TERMINAL_STATUSES:
        ticket.resolved_by = actor.id
        ticket.resolved_at = datetime.now(timezone.utc)
    else:
        ticket.resolved_by = None
        ticket.resolved_at = None
    return ticket
