"""Admin API for EBS identity mappings — the grant behind every EBS tool call.

A chat user asking "are there any blocking locks?" only gets an answer if an
open mapping exists for their email. Without one, EBSMCP denies the call by
name; with one, the mapped persona decides which tools resolve and which EBS
instances they reach. Until now the only way to create that grant was a hand
written INSERT with created_by set by hand, which put onboarding out of reach
of the people who actually administer this system.

Two rules shape the endpoints:

  * Grants are CLOSED, not deleted. effective_end_date exists so the record of
    who could do what, and when, survives revocation. Delete stays available
    for genuine mistakes and says so.
  * The table's CheckConstraints are re-stated here as validation. They are the
    real enforcement, but a raw constraint violation is not something an admin
    can act on, so every one of them gets a readable message first.
"""
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import models, schemas
from app.core import database
from app.core.audit import audit_service
from app.core.auth.auth import get_current_admin
from app.models.ebs_identity import (
    IdentityMapping,
    IdentityMappingInstanceScope,
    IdentityMappingOrgScope,
)

router = APIRouter(prefix="/admin/identity-mappings", tags=["EBS Identity"])

# What each persona means, shown in the console so an admin picks the right one
# without reading the architecture doc. Kept beside the validation that enforces
# the same distinctions.
PERSONAS = {
    "ebs_dba": {
        "label": "EBS DBA",
        "summary": "Database and concurrent-processing diagnostics across the whole instance.",
        "needs_username": False,
        "needs_domain": False,
        "supports_org_scope": False,
        "note": "All-or-nothing: an ebs_dba grant is not narrowed by Org ID. "
                "Restrict which databases it reaches with instance scope instead.",
    },
    "ebs": {
        "label": "EBS Functional",
        "summary": "Functional inquiry as a specific EBS user, limited to their Org IDs.",
        "needs_username": True,
        "needs_domain": True,
        "supports_org_scope": True,
        "note": "Requires the person's FND_USER name and a functional domain. "
                "Org scope narrows what they can see and can never widen it.",
    },
    "fusion": {
        "label": "Fusion",
        "summary": "Oracle Fusion Cloud applications.",
        "needs_username": True,
        "needs_domain": True,
        "supports_org_scope": True,
        "note": "No Fusion toolsets are mounted in this deployment yet.",
    },
}


def _out(m: IdentityMapping) -> schemas.IdentityMappingOut:
    return schemas.IdentityMappingOut(
        id=m.id,
        entra_subject=m.entra_subject,
        environment=m.environment,
        target_system=m.target_system,
        target_username=m.target_username,
        domain=m.domain,
        mapped_role=m.mapped_role,
        resolution_source=m.resolution_source,
        effective_start_date=m.effective_start_date,
        effective_end_date=m.effective_end_date,
        instance_scope_restricted=m.instance_scope_restricted,
        instance_scope=sorted(s.instance_name for s in (m.instance_scope or [])),
        org_scope=sorted(s.org_id for s in (m.org_scope or [])),
        created_at=m.created_at,
        created_by=m.created_by,
        updated_by=m.updated_by,
    )


def _validate(payload: schemas.IdentityMappingCreate, known_instances: set[str]) -> None:
    """Re-state the table's CheckConstraints as messages an admin can act on."""
    persona = PERSONAS[payload.target_system]

    if persona["needs_username"] and not payload.target_username:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"{persona['label']} needs an EBS username (FND_USER) — it acts as that user.")
    if persona["needs_domain"] and not payload.domain:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"{persona['label']} needs a functional domain (e.g. finance, scm).")

    if not persona["needs_username"] and payload.target_username:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"{persona['label']} does not act as a specific EBS user — leave the username blank.")
    if not persona["needs_domain"] and payload.domain:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"{persona['label']} is not scoped by domain — leave the domain blank.")

    if payload.org_scope and not persona["supports_org_scope"]:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"{persona['label']} is all-or-nothing and ignores Org ID scope. "
                   "Use instance scope to limit which databases it reaches.")

    if payload.instance_scope_restricted and not payload.instance_scope:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Instance scope is restricted but no instances were selected — "
                   "this mapping could not reach anything.")

    unknown = [i for i in payload.instance_scope if i.strip().upper() not in known_instances]
    if unknown:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown instance(s): {', '.join(unknown)}. "
                   f"Configured: {', '.join(sorted(known_instances)) or 'none'}.")


def _known_instances(db: Session) -> set[str]:
    return {e.name.upper() for e in db.query(models.EbsEnvironment).all() if e.name}


@router.get("/options", response_model=schemas.IdentityMappingOptions)
def mapping_options(db: Session = Depends(database.get_db),
                    _: models.User = Depends(get_current_admin)):
    """Everything the create form needs, so the UI never keeps its own copy of
    the server's rules."""
    from app.core.ebs_bridge import EBS_DEPLOY_ENVIRONMENT
    return schemas.IdentityMappingOptions(
        environments=["dev", "test", "uat", "prod"],
        target_systems=list(PERSONAS),
        instances=sorted(_known_instances(db)),
        deploy_environment=EBS_DEPLOY_ENVIRONMENT,
        personas=PERSONAS,
    )


@router.get("", response_model=List[schemas.IdentityMappingOut])
def list_mappings(
    subject: Optional[str] = Query(None, description="Filter by email (substring, case-insensitive)"),
    target_system: Optional[str] = Query(None),
    open_only: bool = Query(False, description="Only mappings that currently grant access"),
    db: Session = Depends(database.get_db),
    _: models.User = Depends(get_current_admin),
):
    q = db.query(IdentityMapping)
    if subject:
        q = q.filter(func.lower(IdentityMapping.entra_subject).contains(subject.strip().lower()))
    if target_system:
        q = q.filter(IdentityMapping.target_system == target_system)
    if open_only:
        q = q.filter(IdentityMapping.effective_end_date.is_(None))
    rows = q.order_by(IdentityMapping.entra_subject.asc(),
                      IdentityMapping.effective_start_date.desc()).all()
    return [_out(m) for m in rows]


@router.post("", response_model=schemas.IdentityMappingOut, status_code=status.HTTP_201_CREATED)
def create_mapping(payload: schemas.IdentityMappingCreate,
                   db: Session = Depends(database.get_db),
                   admin: models.User = Depends(get_current_admin)):
    known = _known_instances(db)
    _validate(payload, known)

    # The partial unique index allows one OPEN mapping per
    # (subject, environment, target_system, domain). Check first so a duplicate
    # is a 409 naming the existing row, not an opaque IntegrityError.
    clash = db.query(IdentityMapping).filter(
        IdentityMapping.entra_subject == payload.entra_subject,
        IdentityMapping.environment == payload.environment,
        IdentityMapping.target_system == payload.target_system,
        func.coalesce(IdentityMapping.domain, "") == (payload.domain or ""),
        IdentityMapping.effective_end_date.is_(None),
    ).first()
    if clash:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"{payload.entra_subject} already has an open {payload.target_system} mapping "
                   f"for {payload.environment} (id {clash.id}). Close it before creating a new one.")

    m = IdentityMapping(
        entra_subject=payload.entra_subject,
        environment=payload.environment,
        target_system=payload.target_system,
        target_username=payload.target_username,
        domain=payload.domain,
        mapped_role=payload.mapped_role,
        instance_scope_restricted=payload.instance_scope_restricted,
        created_by=admin.email,
    )
    for name in {i.strip().upper() for i in payload.instance_scope}:
        m.instance_scope.append(IdentityMappingInstanceScope(instance_name=name))
    for org_id in {o.strip() for o in payload.org_scope if o.strip()}:
        m.org_scope.append(IdentityMappingOrgScope(org_id=org_id, resolved_from_source=False))

    db.add(m)
    db.commit()
    db.refresh(m)

    audit_service.log("ebs_mapping_create", user_id=admin.id, username=admin.username,
                      detail={"id": m.id, "subject": m.entra_subject,
                              "target_system": m.target_system, "role": m.mapped_role,
                              "environment": m.environment,
                              "instance_scope": [s.instance_name for s in m.instance_scope]})
    return _out(m)


@router.post("/{mapping_id}/close", response_model=schemas.IdentityMappingOut)
def close_mapping(mapping_id: int, payload: schemas.IdentityMappingClose,
                  db: Session = Depends(database.get_db),
                  admin: models.User = Depends(get_current_admin)):
    """Revoke a grant while keeping the record of it."""
    m = db.query(IdentityMapping).filter(IdentityMapping.id == mapping_id).first()
    if not m:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Mapping not found")
    if m.effective_end_date is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            detail="This mapping is already closed.")

    # ck_identity_mappings_date_order requires end > start. A mapping created
    # moments ago would otherwise fail that check on a same-instant timestamp.
    now = datetime.now(timezone.utc)
    start = m.effective_start_date
    if start is not None and now <= start:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="This mapping starts in the future — delete it instead of closing it.")

    m.effective_end_date = now
    m.updated_by = admin.email
    db.commit()
    db.refresh(m)

    audit_service.log("ebs_mapping_close", user_id=admin.id, username=admin.username,
                      detail={"id": m.id, "subject": m.entra_subject,
                              "reason": payload.reason})
    return _out(m)


@router.delete("/{mapping_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_mapping(mapping_id: int,
                   db: Session = Depends(database.get_db),
                   admin: models.User = Depends(get_current_admin)):
    """Erase a mapping entirely — for rows created in error.

    Closing is the right action for a grant that was genuine and is ending;
    this removes the history too, so the console asks before offering it.
    """
    m = db.query(IdentityMapping).filter(IdentityMapping.id == mapping_id).first()
    if not m:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Mapping not found")
    snapshot = {"id": m.id, "subject": m.entra_subject, "target_system": m.target_system,
                "environment": m.environment, "role": m.mapped_role}
    db.delete(m)
    db.commit()
    audit_service.log("ebs_mapping_delete", user_id=admin.id, username=admin.username,
                      detail=snapshot)
