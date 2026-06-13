"""SQLAlchemy ORM models, organised by domain.

All models share the single declarative ``Base`` from app.core.database. This
package imports every submodule so that importing ``app.models`` registers all
tables on ``Base.metadata`` (the schema source of truth — see bootstrap.run_
bootstrap / alembic env). It also re-exports ``Base`` and every model class so
existing call sites keep using ``from app.models import Base``,
``from app import models`` / ``models.X`` unchanged.
"""
from app.core.database import Base

from app.models.iam import User, Role, UserRole, Agent, RoleAgent, UserSession
from app.models.chat import AgentStep, ChatSession, AgentRun, ChatMessage
from app.models.rag import RagDocument
from app.models.infra import SshServer, EbsEnvironment
from app.models.dba import DeploymentRun, DeploymentStep, CloneRun, PatchRun
from app.models.audit import AuditLog, UsageEvent
from app.models.settings import IntegrationCredential, SsoSettings, LlmCredential
from app.models.ml import TrainingExample, TrainingJob, AgentModel

__all__ = [
    "Base",
    # iam
    "User", "Role", "UserRole", "Agent", "RoleAgent", "UserSession",
    # chat / agent-run substrate
    "AgentStep", "ChatSession", "AgentRun", "ChatMessage",
    # rag
    "RagDocument",
    # infra (shared instance/connection registry)
    "SshServer", "EbsEnvironment",
    # apps-DBA run records
    "DeploymentRun", "DeploymentStep", "CloneRun", "PatchRun",
    # audit / telemetry
    "AuditLog", "UsageEvent",
    # admin-managed settings / credentials
    "IntegrationCredential", "SsoSettings", "LlmCredential",
    # local-model fine-tuning
    "TrainingExample", "TrainingJob", "AgentModel",
]
