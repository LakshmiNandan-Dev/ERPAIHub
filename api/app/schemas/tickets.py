from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class TicketCreate(BaseModel):
    title: str
    description: Optional[str] = None
    priority: Optional[str] = "medium"
    environment_id: Optional[int] = None
    assignee_id: Optional[int] = None


class TicketFromFinding(BaseModel):
    priority: Optional[str] = "medium"
    assignee_id: Optional[int] = None
    description: Optional[str] = None


class TicketUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    assignee_id: Optional[int] = None
    resolution_notes: Optional[str] = None


class TicketOut(BaseModel):
    id: int
    title: str
    description: Optional[str] = None
    status: str
    priority: str
    environment_id: Optional[int] = None
    environment_name: Optional[str] = None
    finding_id: Optional[int] = None
    source_run_id: Optional[int] = None
    created_by: Optional[int] = None
    assignee_id: Optional[int] = None
    resolution_notes: Optional[str] = None
    resolved_by: Optional[int] = None
    resolved_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
