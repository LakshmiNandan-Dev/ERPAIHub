from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, TIMESTAMP, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql.expression import text
from sqlalchemy.orm import relationship
from .database import Base

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, nullable=False)
    username = Column(String(50), unique=True, nullable=False)
    email = Column(String(255), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=True)  # null for SSO-provisioned users
    is_active = Column(Boolean, server_default='TRUE', nullable=False)
    is_admin = Column(Boolean, server_default='FALSE', nullable=False)
    role = Column(String(20), nullable=False, server_default='user')  # admin | dba | user
    approval_status = Column(String(20), nullable=False, server_default='approved')  # approved | pending | rejected
    auth_provider = Column(String(50), nullable=False, server_default='local')  # 'local' | 'entra'
    external_id = Column(String(255), unique=True, nullable=True)  # IdP subject for SSO users
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))

    roles = relationship("Role", secondary="user_roles", back_populates="users")
    sessions = relationship("UserSession", back_populates="user", cascade="all, delete-orphan")


class Role(Base):
    __tablename__ = "roles"

    id = Column(Integer, primary_key=True, nullable=False)
    name = Column(String(50), unique=True, nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))

    users = relationship("User", secondary="user_roles", back_populates="roles")
    agents = relationship("Agent", secondary="role_agents", back_populates="roles")


class UserRole(Base):
    __tablename__ = "user_roles"

    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, nullable=False)
    role_id = Column(Integer, ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True, nullable=False)


class Agent(Base):
    __tablename__ = "agents"

    id = Column(Integer, primary_key=True, nullable=False)
    name = Column(String(100), unique=True, nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))

    roles = relationship("Role", secondary="role_agents", back_populates="agents")
    steps = relationship("AgentStep", back_populates="agent", cascade="all, delete-orphan")


class RoleAgent(Base):
    __tablename__ = "role_agents"

    role_id = Column(Integer, ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True, nullable=False)
    agent_id = Column(Integer, ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True, nullable=False)


class UserSession(Base):
    __tablename__ = "user_sessions"

    id = Column(Integer, primary_key=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    session_token = Column(String(255), unique=True, nullable=False)
    expires_at = Column(TIMESTAMP(timezone=True), nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    last_active_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    is_active = Column(Boolean, server_default='TRUE', nullable=False)

    user = relationship("User", back_populates="sessions")


class AgentStep(Base):
    __tablename__ = "agent_steps"

    id = Column(Integer, primary_key=True, nullable=False)
    agent_id = Column(Integer, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    step_order = Column(Integer, nullable=False)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    config = Column(JSONB, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))

    agent = relationship("Agent", back_populates="steps")

class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id = Column(Integer, primary_key=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    title = Column(String(255), nullable=True)
    context_summary = Column(Text, nullable=True)
    summarized_through_id = Column(Integer, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))

    user = relationship("User", backref="chat_sessions")
    messages = relationship("ChatMessage", back_populates="session", cascade="all, delete-orphan")
    agent_runs = relationship("AgentRun", back_populates="session", cascade="all, delete-orphan")

class AgentRun(Base):
    __tablename__ = "agent_runs"

    id = Column(Integer, primary_key=True, nullable=False)
    session_id = Column(Integer, ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False)
    agent_id = Column(Integer, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    status = Column(String(50), nullable=False, server_default='started')
    started_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    completed_at = Column(TIMESTAMP(timezone=True), nullable=True)

    session = relationship("ChatSession", back_populates="agent_runs")
    agent = relationship("Agent")
    messages = relationship("ChatMessage", back_populates="agent_run")

class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, nullable=False)
    session_id = Column(Integer, ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False)
    role = Column(String(50), nullable=False) # 'user' or 'assistant'
    content = Column(Text, nullable=False)
    agent_run_id = Column(Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True)
    agent_step_id = Column(Integer, ForeignKey("agent_steps.id", ondelete="SET NULL"), nullable=True)
    feedback_rating = Column(Integer, nullable=True) # +1 for good, -1 for bad
    feedback_correction = Column(Text, nullable=True) # Optional user corrected answer
    feedback_notes = Column(Text, nullable=True) # Optional user notes on the rating
    rlaif_rating = Column(Integer, nullable=True) # +1 if passes automated critique, -1 if fails
    rlaif_critique = Column(Text, nullable=True) # Automated AI critique/hallucination report
    rlaif_correction = Column(Text, nullable=True) # Automated AI-generated correction
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))

    session = relationship("ChatSession", back_populates="messages")
    agent_run = relationship("AgentRun", back_populates="messages")
    agent_step = relationship("AgentStep")

class RagDocument(Base):
    __tablename__ = "rag_documents"

    id = Column(Integer, primary_key=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    filename = Column(String(512), nullable=False)
    file_type = Column(String(50), nullable=False)
    status = Column(String(50), nullable=False, server_default='indexing')  # indexing | ready | failed
    chunk_count = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))

    user = relationship("User", backref="rag_documents")


class DeploymentRun(Base):
    __tablename__ = "deployment_runs"

    id = Column(Integer, primary_key=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    source_doc_type = Column(String(50), nullable=False) # 'confluence', 'word', 'pdf', 'chat'
    source_doc_name = Column(String(255), nullable=True)
    source_content = Column(Text, nullable=False)
    target_instance = Column(String(50), nullable=False) # 'DEV', 'UAT', 'UAT2', 'PROD'
    # Optional references to admin-managed resources; credentials are resolved &
    # decrypted in-memory at execution time rather than stored plaintext here.
    ssh_server_id = Column(Integer, ForeignKey("ssh_servers.id", ondelete="SET NULL"), nullable=True)
    environment_id = Column(Integer, ForeignKey("ebs_environments.id", ondelete="SET NULL"), nullable=True)
    git_repo_url = Column(String(512), nullable=True)
    git_branch = Column(String(100), nullable=True, server_default='main')
    ssh_host = Column(String(255), nullable=True)
    ssh_port = Column(Integer, nullable=True, server_default='22')
    ssh_username = Column(String(100), nullable=True)
    ssh_password = Column(String(255), nullable=True)
    git_token    = Column(String(255), nullable=True)
    db_host      = Column(String(255), nullable=True)
    db_port      = Column(Integer,     nullable=True, server_default='1521')
    db_sid       = Column(String(100), nullable=True)
    db_user      = Column(String(100), nullable=True)
    db_password  = Column(String(255), nullable=True)
    status = Column(String(50), nullable=False, server_default='pending') # 'pending', 'extracting', 'downloading', 'deploying', 'completed', 'failed'
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    completed_at = Column(TIMESTAMP(timezone=True), nullable=True)

    user = relationship("User", backref="deployment_runs")
    steps = relationship("DeploymentStep", back_populates="deployment", cascade="all, delete-orphan")


class DeploymentStep(Base):
    __tablename__ = "deployment_steps"

    id = Column(Integer, primary_key=True, nullable=False)
    deployment_id = Column(Integer, ForeignKey("deployment_runs.id", ondelete="CASCADE"), nullable=False)
    step_number = Column(Integer, nullable=False)
    file_name = Column(String(255), nullable=False)
    execution_type = Column(String(50), nullable=False) # 'sql_script', 'plsql_package', 'shell_script', 'host_copy'
    command = Column(String(512), nullable=False)
    status = Column(String(50), nullable=False, server_default='pending') # 'pending', 'running', 'success', 'failed'
    log_output = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    completed_at = Column(TIMESTAMP(timezone=True), nullable=True)

    deployment = relationship("DeploymentRun", back_populates="steps")


# ── Admin-managed resources (secrets stored as Fernet ciphertext) ──────────────

class SshServer(Base):
    __tablename__ = "ssh_servers"

    id = Column(Integer, primary_key=True, nullable=False)
    name = Column(String(100), unique=True, nullable=False)
    hostname = Column(String(255), nullable=False)
    port = Column(Integer, nullable=False, server_default='22')
    username = Column(String(100), nullable=True)
    password_enc = Column(Text, nullable=True)  # encrypted
    server_type = Column(String(50), nullable=False, server_default='application')  # application | database
    app_services = Column(JSONB, nullable=True)  # e.g. ["web", "forms", "concurrent"]
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, server_default='TRUE', nullable=False)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))


class EbsEnvironment(Base):
    __tablename__ = "ebs_environments"

    id = Column(Integer, primary_key=True, nullable=False)
    name = Column(String(50), unique=True, nullable=False)  # DEV | UAT | UAT2 | PROD | ...
    tier = Column(String(20), nullable=False, server_default='nonprod')  # 'prod' | 'nonprod'
    db_host = Column(String(255), nullable=True)
    db_port = Column(Integer, nullable=False, server_default='1521')
    db_sid = Column(String(100), nullable=True)
    db_user = Column(String(100), nullable=True, server_default='apps')
    db_password_enc = Column(Text, nullable=True)  # encrypted
    # Intrinsic database identity (captured by probing) — used to detect a mis-labelled PROD.
    db_id = Column(String(40), nullable=True)        # v$database.dbid
    global_name = Column(String(255), nullable=True) # global_name
    # Extra EBS credentials used by the patching agent's adop tools (encrypted at rest).
    # APPS is db_user/db_password_enc above. SYSTEM (or EBS_SYSTEM on 19c+) and the
    # WebLogic AdminServer account are prompted for by adop phases.
    # 'system_user' is a reserved word in PostgreSQL 16+, so force quoting of the
    # identifier; otherwise CREATE TABLE fails with a syntax error.
    system_user = Column(String(100), nullable=True, server_default='system', quote=True)   # system | ebs_system
    system_password_enc = Column(Text, nullable=True)
    weblogic_user = Column(String(100), nullable=True, server_default='weblogic')
    weblogic_password_enc = Column(Text, nullable=True)
    apps_os_user = Column(String(100), nullable=True)   # OS owner of the apps tier (e.g. applmgr)
    description = Column(Text, nullable=True)
    ssh_server_id = Column(Integer, ForeignKey("ssh_servers.id", ondelete="SET NULL"), nullable=True)
    is_active = Column(Boolean, server_default='TRUE', nullable=False)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))

    ssh_server = relationship("SshServer")


class CloneRun(Base):
    """An EBS Rapid Clone run (RMAN duplicate DB + Rapid Clone apps tier).

    Simulator-first: steps and logs are generated and stored as JSONB; the
    downloadable runbook parameterises all passwords as shell variables.
    """
    __tablename__ = "clone_runs"

    id = Column(Integer, primary_key=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    source_name = Column(String(50), nullable=True)
    target_name = Column(String(50), nullable=True)
    source_sid = Column(String(100), nullable=True)
    target_sid = Column(String(100), nullable=True)
    source_db_host = Column(String(255), nullable=True)
    target_db_host = Column(String(255), nullable=True)
    source_apps_host = Column(String(255), nullable=True)
    target_apps_host = Column(String(255), nullable=True)
    db_method = Column(String(50), nullable=False, server_default='rman_duplicate')
    target_environment_id = Column(Integer, ForeignKey("ebs_environments.id", ondelete="SET NULL"), nullable=True)
    params = Column(JSONB, nullable=True)   # extra collected fields (ports, homes, etc.)
    steps = Column(JSONB, nullable=True)    # [{phase, node, command, status, log}]
    # Production-guard outcome
    guard_status = Column(String(20), nullable=True)   # 'passed' | 'blocked' | 'overridden'
    guard_result = Column(JSONB, nullable=True)        # {production, reasons[], identity{...}}
    override_reason = Column(Text, nullable=True)
    overridden_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    status = Column(String(50), nullable=False, server_default='pending')
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    completed_at = Column(TIMESTAMP(timezone=True), nullable=True)

    user = relationship("User", foreign_keys=[user_id], backref="clone_runs")


class PatchRun(Base):
    """An EBS patching run — DB Oracle Home / Grid / RAC (OPatch, opatchauto,
    datapatch) and the application tier (adop online patching, WebLogic and FMW
    component homes: oracle_common, Forms, OHS).

    Simulator-first: phases and logs are generated and stored as JSONB; the
    downloadable runbook (patch.sh) parameterises every password as a shell
    variable. Patching a PRODUCTION target is allowed but gated by the production
    guard + maker-checker approval (a different Admin/DBA must approve).
    """
    __tablename__ = "patch_runs"

    id = Column(Integer, primary_key=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    environment_id = Column(Integer, ForeignKey("ebs_environments.id", ondelete="SET NULL"), nullable=True)
    environment_name = Column(String(50), nullable=True)   # snapshot of the target env name
    patch_label = Column(String(255), nullable=True)       # human label (e.g. "Oct-2025 CPU", "23ai RU")
    patch_number = Column(String(255), nullable=True)      # patch / bundle id(s)
    components = Column(JSONB, nullable=True)               # ["db_home","grid","adop","weblogic","fmw_homes"]
    params = Column(JSONB, nullable=True)                   # topology, homes, adop mode, stage dir, etc.
    steps = Column(JSONB, nullable=True)                    # [{step, phase, node, command, status, log}]
    # Production-guard outcome (prod target => approval_required, gated by maker-checker)
    guard_status = Column(String(20), nullable=True)        # 'passed' | 'approval_required' | 'overridden' | 'rejected'
    guard_result = Column(JSONB, nullable=True)             # {production, reasons[], signals{...}}
    override_reason = Column(Text, nullable=True)
    overridden_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    status = Column(String(50), nullable=False, server_default='pending')  # pending|running|completed|awaiting_approval|rejected
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    completed_at = Column(TIMESTAMP(timezone=True), nullable=True)

    user = relationship("User", foreign_keys=[user_id], backref="patch_runs")


class AuditLog(Base):
    """Append-only audit trail — auth events and agent invocations, with origin (IP / machine)."""
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, nullable=False)
    event_type = Column(String(30), nullable=False)  # login | logout | login_failed | agent_invoke
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    username = Column(String(100), nullable=True)
    agent = Column(String(50), nullable=True)         # for agent_invoke: chat | deployment | ...
    ip_address = Column(String(64), nullable=True)
    user_agent = Column(Text, nullable=True)          # browser / OS (machine)
    method = Column(String(10), nullable=True)
    path = Column(String(255), nullable=True)
    detail = Column(JSONB, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'), index=True)


class UsageEvent(Base):
    """Durable telemetry log — one row per HTTP request (kind='http') or LLM call (kind='llm')."""
    __tablename__ = "usage_events"

    id = Column(Integer, primary_key=True, nullable=False)
    kind = Column(String(10), nullable=False)  # 'http' | 'llm'
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    username = Column(String(100), nullable=True)
    endpoint = Column(String(255), nullable=True)
    method = Column(String(10), nullable=True)
    provider = Column(String(50), nullable=True)
    model = Column(String(150), nullable=True)
    status_code = Column(Integer, nullable=True)
    request_bytes = Column(Integer, nullable=False, server_default='0')
    response_bytes = Column(Integer, nullable=False, server_default='0')
    prompt_tokens = Column(Integer, nullable=False, server_default='0')
    completion_tokens = Column(Integer, nullable=False, server_default='0')
    total_tokens = Column(Integer, nullable=False, server_default='0')
    context_chars = Column(Integer, nullable=False, server_default='0')
    duration_ms = Column(Integer, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'), index=True)


class IntegrationCredential(Base):
    """
    Admin-managed Git / Confluence credentials, encrypted at rest, resolved
    server-side at use-time. `host` is matched against the repo/page URL host;
    `is_default` is the fallback when no host matches.
    """
    __tablename__ = "integration_credentials"

    id = Column(Integer, primary_key=True, nullable=False)
    kind = Column(String(20), nullable=False)         # 'git' | 'confluence'
    name = Column(String(100), nullable=False)
    host = Column(String(255), nullable=True)         # e.g. github.com, gitlab.corp.local, mysite.atlassian.net
    base_url = Column(String(512), nullable=True)     # optional full base (confluence site)
    username = Column(String(255), nullable=True)     # git user, or Confluence Cloud email
    secret_enc = Column(Text, nullable=True)          # encrypted PAT / API token
    is_default = Column(Boolean, server_default='FALSE', nullable=False)
    is_active = Column(Boolean, server_default='TRUE', nullable=False)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))


class SsoSettings(Base):
    """Single-row store (id=1) for OIDC SSO configuration. Secret stored encrypted."""
    __tablename__ = "sso_settings"

    id = Column(Integer, primary_key=True, nullable=False)
    provider = Column(String(50), nullable=False, server_default='entra')  # microsoft entra id
    enabled = Column(Boolean, server_default='FALSE', nullable=False)
    signup_enabled = Column(Boolean, server_default='FALSE', nullable=False)  # public self sign-up
    tenant_id = Column(String(255), nullable=True)
    client_id = Column(String(255), nullable=True)
    client_secret_enc = Column(Text, nullable=True)  # encrypted
    redirect_uri = Column(String(512), nullable=True)
    auto_provision = Column(Boolean, server_default='TRUE', nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))


class LlmCredential(Base):
    __tablename__ = "llm_credentials"

    id = Column(Integer, primary_key=True, nullable=False)
    provider = Column(String(50), nullable=False)  # ollama | openai | anthropic | gemini
    label = Column(String(100), nullable=True)
    model = Column(String(150), nullable=True)
    api_key_enc = Column(Text, nullable=True)  # encrypted
    base_url = Column(String(512), nullable=True)
    is_default = Column(Boolean, server_default='FALSE', nullable=False)
    is_active = Column(Boolean, server_default='TRUE', nullable=False)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))


# ── Local-model fine-tuning (Ollama only) ──────────────────────────────────────

class TrainingExample(Base):
    """A single curated training row, per agent. kind='sft' uses prompt+chosen;
    kind='dpo' adds 'rejected' for preference tuning. Assembled from feedback,
    uploads, RAG documents or accepted agent runs."""
    __tablename__ = "training_examples"

    id = Column(Integer, primary_key=True, nullable=False)
    agent = Column(String(40), nullable=False)            # chat | deployment | performance | cloning | knowledge_base
    kind = Column(String(10), nullable=False, server_default='sft')   # sft | dpo
    source = Column(String(20), nullable=False, server_default='upload')  # feedback | upload | rag | agent_run
    system = Column(Text, nullable=True)
    prompt = Column(Text, nullable=False)
    chosen = Column(Text, nullable=False)
    rejected = Column(Text, nullable=True)                 # required for dpo
    is_active = Column(Boolean, server_default='TRUE', nullable=False)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))


class TrainingJob(Base):
    """A fine-tuning job for one agent: assembles a dataset, emits a runnable LoRA
    SFT→DPO bundle (run on a GPU host), then registers the resulting GGUF in Ollama."""
    __tablename__ = "training_jobs"

    id = Column(Integer, primary_key=True, nullable=False)
    agent = Column(String(40), nullable=False)
    base_model = Column(String(150), nullable=False)      # an Ollama base, e.g. llama3.2:1b
    method = Column(String(10), nullable=False, server_default='both')  # sft | dpo | both
    target_model_tag = Column(String(150), nullable=False)  # resulting Ollama tag, e.g. oraebs-chat:v1
    status = Column(String(20), nullable=False, server_default='draft')  # draft | bundled | registered | failed
    sft_count = Column(Integer, nullable=False, server_default='0')
    dpo_count = Column(Integer, nullable=False, server_default='0')
    params = Column(JSONB, nullable=True)                  # epochs, lr, lora_r, sources...
    notes = Column(Text, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    bundled_at = Column(TIMESTAMP(timezone=True), nullable=True)
    registered_at = Column(TIMESTAMP(timezone=True), nullable=True)


class AgentModel(Base):
    """Per-agent active local model. When an agent runs on the Ollama provider and
    has a mapping here, the gateway uses this (fine-tuned) model tag for that agent."""
    __tablename__ = "agent_models"

    id = Column(Integer, primary_key=True, nullable=False)
    agent = Column(String(40), unique=True, nullable=False)
    provider = Column(String(20), nullable=False, server_default='ollama')
    model = Column(String(150), nullable=False)
    training_job_id = Column(Integer, ForeignKey("training_jobs.id", ondelete="SET NULL"), nullable=True)
    is_active = Column(Boolean, server_default='TRUE', nullable=False)
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
