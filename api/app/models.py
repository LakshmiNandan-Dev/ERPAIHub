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
    password_hash = Column(String(255), nullable=False)
    is_active = Column(Boolean, server_default='TRUE', nullable=False)
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
