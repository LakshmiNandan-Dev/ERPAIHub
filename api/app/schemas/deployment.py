from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class MigrateRequest(BaseModel):
    """Payload for migrating a completed deployment to another environment."""
    target_instance: str
    # Preferred: reference admin-managed resources (credentials resolved server-side)
    environment_id: Optional[int] = None
    ssh_server_id: Optional[int] = None
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
    # Optional references to admin-managed resources (credentials resolved server-side)
    environment_id: Optional[int] = None
    ssh_server_id: Optional[int] = None
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
