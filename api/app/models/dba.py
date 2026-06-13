from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, TIMESTAMP, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql.expression import text
from sqlalchemy.orm import relationship
from app.core.database import Base


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
