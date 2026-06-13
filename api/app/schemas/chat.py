from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class ChatSessionCreate(BaseModel):
    title: Optional[str] = "New Chat"

class ChatSessionRename(BaseModel):
    title: str

class ChatMessageOut(BaseModel):
    id: int
    role: str
    content: str
    agent_run_id: Optional[int] = None
    agent_step_id: Optional[int] = None
    feedback_rating: Optional[int] = None
    feedback_correction: Optional[str] = None
    feedback_notes: Optional[str] = None
    rlaif_rating: Optional[int] = None
    rlaif_critique: Optional[str] = None
    rlaif_correction: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True

class FeedbackSubmit(BaseModel):
    rating: int # +1 (thumbs up) or -1 (thumbs down)
    correction: Optional[str] = None
    notes: Optional[str] = None


class ChatSessionOut(BaseModel):
    id: int
    title: Optional[str]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

class ChatSessionWithMessagesOut(ChatSessionOut):
    messages: list[ChatMessageOut] = []

class MessageCreate(BaseModel):
    content: str
    agent_run_id: Optional[int] = None
    agent_step_id: Optional[int] = None

class AgentRunCreate(BaseModel):
    agent_id: int

class AgentRunOut(BaseModel):
    id: int
    session_id: int
    agent_id: int
    status: str
    started_at: datetime
    completed_at: Optional[datetime]

    class Config:
        from_attributes = True
