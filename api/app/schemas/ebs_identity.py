"""Schemas for EBS identity mappings — who a chat user becomes inside EBS.

A mapping is the grant that makes the embedded EBSMCP tools usable: without
an open row for a subject, every tool call it makes is denied by name. These
are the shapes the admin console posts and reads.
"""
from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

Environment = Literal["dev", "test", "uat", "prod"]
TargetSystem = Literal["ebs", "ebs_dba", "fusion"]


class IdentityMappingCreate(BaseModel):
    entra_subject: str = Field(..., max_length=320,
                               description="The user's email — matches their OraEBSAgent login")
    environment: Environment
    target_system: TargetSystem
    mapped_role: str = Field(..., max_length=240)
    # Required for every persona EXCEPT ebs_dba, which is all-or-nothing and
    # carries no EBS username or functional domain. Enforced by CheckConstraints
    # on the table; validated here first so the admin gets a readable message
    # rather than a raw constraint violation.
    target_username: Optional[str] = Field(None, max_length=100)
    domain: Optional[str] = Field(None, max_length=40)

    instance_scope_restricted: bool = False
    instance_scope: List[str] = Field(default_factory=list,
                                      description="ebs_environments names this mapping may reach")
    org_scope: List[str] = Field(default_factory=list,
                                 description="EBS Org IDs; not applicable to ebs_dba")

    @field_validator("entra_subject")
    @classmethod
    def _normalise_subject(cls, v: str) -> str:
        return v.strip().lower()

    @field_validator("target_username", "domain", "mapped_role")
    @classmethod
    def _blank_to_none(cls, v):
        if v is None:
            return None
        v = v.strip()
        return v or None


class IdentityMappingClose(BaseModel):
    """Closing is the normal way a grant ends: the row stays, with an end date,
    so the audit trail of who could do what and when survives revocation."""
    reason: Optional[str] = Field(None, max_length=500)


class IdentityMappingOut(BaseModel):
    id: int
    entra_subject: str
    environment: str
    target_system: str
    target_username: Optional[str]
    domain: Optional[str]
    mapped_role: str
    resolution_source: str
    effective_start_date: Optional[datetime]
    effective_end_date: Optional[datetime]
    instance_scope_restricted: bool
    instance_scope: List[str]
    org_scope: List[str]
    created_at: Optional[datetime]
    created_by: str
    updated_by: Optional[str]

    @property
    def is_open(self) -> bool:
        return self.effective_end_date is None


class IdentityMappingOptions(BaseModel):
    """Everything the create form needs to render without hardcoding a copy of
    the server's rules."""
    environments: List[str]
    target_systems: List[str]
    instances: List[str]
    deploy_environment: str = Field(
        ..., description="The stage this deployment serves; a mapping on any "
                         "other environment will never resolve at runtime.")
    personas: dict
