from pydantic import BaseModel, EmailStr
from typing import Optional
from datetime import datetime

class UserBase(BaseModel):
    username: str
    email: EmailStr

class UserCreate(UserBase):
    password: str

class UserOut(UserBase):
    id: int
    is_active: bool
    created_at: datetime
    
    class Config:
        from_attributes = True

class UserLogin(BaseModel):
    username: str
    password: str

class AgentOut(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    
    class Config:
        from_attributes = True

class RoleOut(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    agents: list[AgentOut] = []
    
    class Config:
        from_attributes = True

class UserWithRolesOut(UserOut):
    roles: list[RoleOut] = []

class SessionOut(BaseModel):
    session_token: str
    expires_at: datetime
    token_type: str = "bearer"
    user: Optional[UserWithRolesOut] = None

class UserChangePassword(BaseModel):
    username: str
    old_password: str
    new_password: str

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

class RagDocumentOut(BaseModel):
    id: int
    filename: str
    file_type: str
    status: str
    chunk_count: Optional[int] = None
    error_message: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class MigrateRequest(BaseModel):
    """Payload for migrating a completed deployment to another environment."""
    target_instance: str
    # Target environment DB credentials (must match the target, not the source)
    db_host: Optional[str] = None
    db_port: Optional[int] = 1521
    db_sid: Optional[str] = None
    db_user: Optional[str] = None
    db_password: Optional[str] = None
    # SSH server for the target (leave null to reuse source deployment's server)
    ssh_host: Optional[str] = None
    ssh_port: Optional[int] = 22
    ssh_username: Optional[str] = None
    ssh_password: Optional[str] = None


class DeploymentCreate(BaseModel):
    source_doc_type: str # 'confluence', 'word', 'pdf', 'chat'
    source_doc_name: Optional[str] = "Pasted Instructions"
    source_content: str
    target_instance: str # 'DEV', 'UAT', 'UAT2', 'PROD'
    git_repo_url: Optional[str] = None
    git_branch: Optional[str] = "main"
    git_token: Optional[str] = None
    ssh_host: Optional[str] = None
    ssh_port: Optional[int] = 22
    ssh_username: Optional[str] = None
    ssh_password: Optional[str] = None
    db_host: Optional[str] = None
    db_port: Optional[int] = 1521
    db_sid: Optional[str] = None
    db_user: Optional[str] = None
    db_password: Optional[str] = None

class DeploymentStepOut(BaseModel):
    id: int
    deployment_id: int
    step_number: int
    file_name: str
    execution_type: str
    command: str
    status: str
    log_output: Optional[str] = None
    created_at: datetime
    completed_at: Optional[datetime] = None

    class Config:
        from_attributes = True

class DeploymentRunSummaryOut(BaseModel):
    id: int
    user_id: int
    source_doc_type: str
    source_doc_name: Optional[str] = None
    target_instance: str
    git_repo_url: Optional[str] = None
    ssh_host: Optional[str] = None
    ssh_username: Optional[str] = None
    status: str
    created_at: datetime
    completed_at: Optional[datetime] = None

    class Config:
        from_attributes = True

class DeploymentRunOut(BaseModel):
    id: int
    user_id: int
    source_doc_type: str
    source_doc_name: Optional[str] = None
    source_content: str
    target_instance: str
    git_repo_url: Optional[str] = None
    git_branch: Optional[str] = None
    ssh_host: Optional[str] = None
    ssh_port: Optional[int] = None
    ssh_username: Optional[str] = None
    status: str
    created_at: datetime
    completed_at: Optional[datetime] = None
    steps: list[DeploymentStepOut] = []

    class Config:
        from_attributes = True
