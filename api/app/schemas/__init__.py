"""Pydantic request/response schemas, organised by domain.

This package re-exports every schema at the top level so existing call sites
keep using `from app import schemas` / `schemas.X` (and `from app.schemas import
X`) unchanged. Add new schemas to the relevant submodule and list them here.
"""
from app.schemas.auth import (
    UserBase,
    UserCreate,
    UserOut,
    UserLogin,
    AgentOut,
    RoleOut,
    UserWithRolesOut,
    SessionOut,
    UserChangePassword,
    PasswordChangeRequest,
)
from app.schemas.chat import (
    ChatSessionCreate,
    ChatSessionRename,
    ChatMessageOut,
    FeedbackSubmit,
    ChatSessionOut,
    ChatSessionWithMessagesOut,
    MessageCreate,
    AgentRunCreate,
    AgentRunOut,
)
from app.schemas.rag import (
    RagDocumentOut,
)
from app.schemas.deployment import (
    MigrateRequest,
    DeploymentCreate,
    DeploymentStepOut,
    DeploymentRunSummaryOut,
    DeploymentRunOut,
)
from app.schemas.admin import (
    AdminUserCreate,
    AdminUserUpdate,
    AdminUserOut,
    RoleBrief,
    AgentBrief,
    RoleDetailOut,
    RoleCreate,
    RoleUpdate,
    PromptOut,
    PromptUpdate,
)
from app.schemas.infra import (
    SshServerBase,
    SshServerCreate,
    SshServerUpdate,
    SshServerOut,
    EnvironmentBase,
    EnvironmentCreate,
    EnvironmentUpdate,
    EnvironmentOut,
)
from app.schemas.llm import (
    LlmCredentialBase,
    LlmCredentialCreate,
    LlmCredentialUpdate,
    LlmCredentialOut,
    AgentLlmPolicyBase,
    AgentLlmPolicyCreate,
    AgentLlmPolicyUpdate,
    AgentLlmPolicyOut,
    AgentRoutingMeta,
)
from app.schemas.config import (
    ConfigServerOut,
    ConfigEnvironmentOut,
    ConfigLlmOut,
)
from app.schemas.sso import (
    SsoSettingsOut,
    SsoSettingsUpdate,
    SsoStatusOut,
)
from app.schemas.nl_sql_settings import (
    NlSqlChatSettingsOut,
    NlSqlChatSettingsUpdate,
)
from app.schemas.integration import (
    IntegrationBase,
    IntegrationCreate,
    IntegrationUpdate,
    IntegrationOut,
)
from app.schemas.tickets import (
    TicketCreate,
    TicketFromFinding,
    TicketUpdate,
    TicketOut,
)
from app.schemas.patching import (
    PatchTargetCreate,
    PatchTargetUpdate,
    PatchTargetOut,
    PatchFileScanConfigCreate,
    PatchFileScanConfigUpdate,
    PatchFileScanConfigOut,
    PatchFileInventoryOut,
    PatchFileInventoryPage,
)

__all__ = [
    # auth
    "UserBase", "UserCreate", "UserOut", "UserLogin", "AgentOut", "RoleOut",
    "UserWithRolesOut", "SessionOut", "UserChangePassword", "PasswordChangeRequest",
    # chat
    "ChatSessionCreate", "ChatSessionRename", "ChatMessageOut", "FeedbackSubmit",
    "ChatSessionOut", "ChatSessionWithMessagesOut", "MessageCreate",
    "AgentRunCreate", "AgentRunOut",
    # rag
    "RagDocumentOut",
    # deployment
    "MigrateRequest", "DeploymentCreate", "DeploymentStepOut",
    "DeploymentRunSummaryOut", "DeploymentRunOut",
    # admin
    "AdminUserCreate", "AdminUserUpdate", "AdminUserOut",
    "RoleBrief", "AgentBrief", "RoleDetailOut", "RoleCreate", "RoleUpdate",
    "PromptOut", "PromptUpdate",
    # infra (ssh + environments)
    "SshServerBase", "SshServerCreate", "SshServerUpdate", "SshServerOut",
    "EnvironmentBase", "EnvironmentCreate", "EnvironmentUpdate", "EnvironmentOut",
    # llm
    "LlmCredentialBase", "LlmCredentialCreate", "LlmCredentialUpdate", "LlmCredentialOut",
    "AgentLlmPolicyBase", "AgentLlmPolicyCreate", "AgentLlmPolicyUpdate", "AgentLlmPolicyOut",
    "AgentRoutingMeta",
    # config
    "ConfigServerOut", "ConfigEnvironmentOut", "ConfigLlmOut",
    # sso
    "SsoSettingsOut", "SsoSettingsUpdate", "SsoStatusOut",
    # nl-sql chat settings
    "NlSqlChatSettingsOut", "NlSqlChatSettingsUpdate",
    # integration
    "IntegrationBase", "IntegrationCreate", "IntegrationUpdate", "IntegrationOut",
    # tickets
    "TicketCreate", "TicketFromFinding", "TicketUpdate", "TicketOut",
    # patching (patch gap targets)
    "PatchTargetCreate", "PatchTargetUpdate", "PatchTargetOut",
    "PatchFileScanConfigCreate", "PatchFileScanConfigUpdate", "PatchFileScanConfigOut",
    "PatchFileInventoryOut", "PatchFileInventoryPage",
]
