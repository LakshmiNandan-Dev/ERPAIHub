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
    is_admin: bool = False
    role: str = "user"
    auth_provider: str = "local"
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

class PasswordChangeRequest(BaseModel):
    """Authenticated self-service password change (operates on the current user)."""
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


# ══════════════════════════════════════════════════════════════════════════════
# Admin — user management
# ══════════════════════════════════════════════════════════════════════════════

class AdminUserCreate(BaseModel):
    username: str
    email: EmailStr
    password: str
    role: str = "user"          # admin | dba | user
    is_admin: bool = False      # legacy; role wins


class AdminUserUpdate(BaseModel):
    email: Optional[EmailStr] = None
    password: Optional[str] = None
    is_active: Optional[bool] = None
    role: Optional[str] = None
    is_admin: Optional[bool] = None


class AdminUserOut(BaseModel):
    id: int
    username: str
    email: EmailStr
    is_active: bool
    is_admin: bool
    role: str = "user"
    approval_status: str = "approved"
    auth_provider: str
    created_at: datetime

    class Config:
        from_attributes = True


# ══════════════════════════════════════════════════════════════════════════════
# Admin — SSH servers
# ══════════════════════════════════════════════════════════════════════════════

class SshServerBase(BaseModel):
    name: str
    hostname: str
    port: int = 22
    username: Optional[str] = None
    server_type: str = "application"
    app_services: Optional[list[str]] = None
    description: Optional[str] = None
    is_active: bool = True


class SshServerCreate(SshServerBase):
    password: Optional[str] = None


class SshServerUpdate(BaseModel):
    name: Optional[str] = None
    hostname: Optional[str] = None
    port: Optional[int] = None
    username: Optional[str] = None
    password: Optional[str] = None  # set to update; omit to keep existing
    server_type: Optional[str] = None
    app_services: Optional[list[str]] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


class SshServerOut(SshServerBase):
    id: int
    has_password: bool = False
    created_at: datetime

    class Config:
        from_attributes = True


# ══════════════════════════════════════════════════════════════════════════════
# Admin — EBS environments
# ══════════════════════════════════════════════════════════════════════════════

class EnvironmentBase(BaseModel):
    name: str
    tier: str = "nonprod"   # 'prod' | 'nonprod'
    db_host: Optional[str] = None
    db_port: int = 1521
    db_sid: Optional[str] = None
    db_user: Optional[str] = "apps"
    # Extra EBS accounts used by the patching agent's adop tools (non-secret parts).
    system_user: Optional[str] = "system"       # system | ebs_system
    weblogic_user: Optional[str] = "weblogic"
    apps_os_user: Optional[str] = None           # OS owner of the apps tier (e.g. applmgr)
    description: Optional[str] = None
    ssh_server_id: Optional[int] = None
    is_active: bool = True


class EnvironmentCreate(EnvironmentBase):
    db_password: Optional[str] = None          # APPS schema password
    system_password: Optional[str] = None      # SYSTEM / EBS_SYSTEM password
    weblogic_password: Optional[str] = None    # WebLogic AdminServer password


class EnvironmentUpdate(BaseModel):
    name: Optional[str] = None
    tier: Optional[str] = None
    db_host: Optional[str] = None
    db_port: Optional[int] = None
    db_sid: Optional[str] = None
    db_user: Optional[str] = None
    db_password: Optional[str] = None  # set to update; omit to keep existing
    system_user: Optional[str] = None
    system_password: Optional[str] = None
    weblogic_user: Optional[str] = None
    weblogic_password: Optional[str] = None
    apps_os_user: Optional[str] = None
    description: Optional[str] = None
    ssh_server_id: Optional[int] = None
    is_active: Optional[bool] = None


class EnvironmentOut(EnvironmentBase):
    id: int
    has_password: bool = False
    has_system_password: bool = False
    has_weblogic_password: bool = False
    db_id: Optional[str] = None
    global_name: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


# ══════════════════════════════════════════════════════════════════════════════
# Admin — LLM credentials
# ══════════════════════════════════════════════════════════════════════════════

class LlmCredentialBase(BaseModel):
    provider: str  # ollama | openai | anthropic | gemini
    label: Optional[str] = None
    model: Optional[str] = None
    base_url: Optional[str] = None
    is_default: bool = False
    is_active: bool = True


class LlmCredentialCreate(LlmCredentialBase):
    api_key: Optional[str] = None


class LlmCredentialUpdate(BaseModel):
    provider: Optional[str] = None
    label: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None  # set to update; omit to keep existing
    base_url: Optional[str] = None
    is_default: Optional[bool] = None
    is_active: Optional[bool] = None


class LlmCredentialOut(LlmCredentialBase):
    id: int
    has_api_key: bool = False
    created_at: datetime

    class Config:
        from_attributes = True


# ══════════════════════════════════════════════════════════════════════════════
# Consumer (non-secret) config views
# ══════════════════════════════════════════════════════════════════════════════

class ConfigServerOut(BaseModel):
    id: int
    name: str
    hostname: str
    port: int
    username: Optional[str] = None
    server_type: str
    app_services: Optional[list[str]] = None

    class Config:
        from_attributes = True


class ConfigEnvironmentOut(BaseModel):
    id: int
    name: str
    db_host: Optional[str] = None
    db_port: int
    db_sid: Optional[str] = None
    db_user: Optional[str] = None

    class Config:
        from_attributes = True


class ConfigLlmOut(BaseModel):
    id: int
    provider: str
    label: Optional[str] = None
    model: Optional[str] = None
    base_url: Optional[str] = None
    is_default: bool

    class Config:
        from_attributes = True


# ══════════════════════════════════════════════════════════════════════════════
# SSO (OIDC / Microsoft Entra ID)
# ══════════════════════════════════════════════════════════════════════════════

class SsoSettingsOut(BaseModel):
    provider: str = "entra"
    enabled: bool = False
    signup_enabled: bool = False
    tenant_id: Optional[str] = None
    client_id: Optional[str] = None
    redirect_uri: Optional[str] = None
    auto_provision: bool = True
    has_client_secret: bool = False


class SsoSettingsUpdate(BaseModel):
    enabled: Optional[bool] = None
    signup_enabled: Optional[bool] = None
    tenant_id: Optional[str] = None
    client_id: Optional[str] = None
    client_secret: Optional[str] = None  # set to update; omit to keep existing
    redirect_uri: Optional[str] = None
    auto_provision: Optional[bool] = None


class SsoStatusOut(BaseModel):
    """Public — tells the login screen whether to show the SSO button / sign-up."""
    enabled: bool = False
    provider: str = "entra"
    button_label: str = "Sign in with Microsoft"
    signup_enabled: bool = False


# ══════════════════════════════════════════════════════════════════════════════
# Integration credentials (Git / Confluence)
# ══════════════════════════════════════════════════════════════════════════════

class IntegrationBase(BaseModel):
    kind: str  # 'git' | 'confluence'
    name: str
    host: Optional[str] = None
    base_url: Optional[str] = None
    username: Optional[str] = None
    is_default: bool = False
    is_active: bool = True


class IntegrationCreate(IntegrationBase):
    secret: Optional[str] = None  # PAT / API token


class IntegrationUpdate(BaseModel):
    name: Optional[str] = None
    host: Optional[str] = None
    base_url: Optional[str] = None
    username: Optional[str] = None
    secret: Optional[str] = None  # set to update; omit to keep existing
    is_default: Optional[bool] = None
    is_active: Optional[bool] = None


class IntegrationOut(IntegrationBase):
    id: int
    has_secret: bool = False
    created_at: datetime

    class Config:
        from_attributes = True
