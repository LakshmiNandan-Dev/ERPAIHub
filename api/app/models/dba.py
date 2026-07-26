from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, TIMESTAMP, Text, UniqueConstraint, Index
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


class PatchTarget(Base):
    """Admin-defined target patch baseline for one component of one
    environment — what compute_gaps() diffs the live AppliedPatchSnapshot
    against (per the admin-defined-target-list design decision)."""
    __tablename__ = "patch_targets"
    __table_args__ = (
        UniqueConstraint('environment_id', 'component', name='ux_patch_targets_env_component'),
    )

    id = Column(Integer, primary_key=True, nullable=False)
    environment_id = Column(Integer, ForeignKey("ebs_environments.id", ondelete="CASCADE"), nullable=False)
    component = Column(String(50), nullable=False)      # 'db_home' | 'grid' | 'weblogic' | fmw home label | 'adop'
    home_path = Column(String(512), nullable=True)       # OPatch-managed home used for the scan (unused for 'adop' — that's a live ad_bugs SQL check, not an OPatch home)
    target_patches = Column(JSONB, nullable=False, server_default='[]')  # [{"patch_number","label","required_by"}]
    notes = Column(Text, nullable=True)
    updated_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))


class AppliedPatchSnapshot(Base):
    """Result of a live `opatch lspatches` scan for one component/home —
    append-only history; gap computation reads the latest row per component."""
    __tablename__ = "applied_patch_snapshots"

    id = Column(Integer, primary_key=True, nullable=False)
    environment_id = Column(Integer, ForeignKey("ebs_environments.id", ondelete="CASCADE"), nullable=False)
    component = Column(String(50), nullable=False)
    home_path = Column(String(512), nullable=True)
    applied_patches = Column(JSONB, nullable=False, server_default='[]')   # ["36420641", ...]
    raw_output = Column(Text, nullable=True)
    scan_status = Column(String(20), nullable=False, server_default='ok')  # ok|error|unreachable
    scan_error = Column(Text, nullable=True)
    scanned_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    scanned_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)  # null if scheduled


class PatchFileScanConfig(Base):
    """Admin-defined run-edition filesystem root to scan for file-version
    drift — one `find` pass over the WHOLE run filesystem (every product top
    beneath it: ap/, gl/, fnd/, ... in one go), not one config per product.
    The product each file belongs to is derived per-file from its path
    relative to run_fs_path (EBS's $APPL_TOP/<product>/... convention) —
    see patch_file_gap_service.run_file_gap_scan."""
    __tablename__ = "patch_file_scan_configs"
    __table_args__ = (
        UniqueConstraint('environment_id', 'run_fs_path', name='ux_patch_file_scan_env_path'),
    )

    id = Column(Integer, primary_key=True, nullable=False)
    environment_id = Column(Integer, ForeignKey("ebs_environments.id", ondelete="CASCADE"), nullable=False)
    label = Column(String(100), nullable=True)            # e.g. 'Run edition APPL_TOP', 'Node2 run fs'
    run_fs_path = Column(String(512), nullable=False)      # whole run-fs root, e.g. /u01/.../apps_st/appl
    file_globs = Column(JSONB, nullable=False, server_default=
        '["*.pls","*.pkb","*.pks","*.sql","*.fmb","*.rdf","*.java"]')
    notes = Column(Text, nullable=True)
    updated_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))


class PatchFileInventory(Base):
    """Upsert-latest inventory of every file the file-version scan sees —
    filesystem version AND AD_FILES/AD_FILE_VERSIONS-registered version for
    EVERY file (matched, mismatched, and not-registered), not just drift
    (Finding's job). One row per (environment_id, product, filename), updated
    in place every scan — bounded by distinct files ever seen, not
    files x scans (append-only here would add tens of thousands of rows per
    scan of a full run filesystem)."""
    __tablename__ = "patch_file_inventory"
    __table_args__ = (
        UniqueConstraint('environment_id', 'product', 'filename',
                         name='ux_patch_file_inventory_env_product_filename'),
        Index('ix_patch_file_inventory_env_status', 'environment_id', 'match_status'),
    )

    id = Column(Integer, primary_key=True, nullable=False)
    environment_id = Column(Integer, ForeignKey("ebs_environments.id", ondelete="CASCADE"), nullable=False)
    product = Column(String(50), nullable=False)
    filename = Column(String(255), nullable=False)
    path = Column(String(512), nullable=False)          # last-seen full path
    fs_version = Column(String(100), nullable=True)
    db_version = Column(String(100), nullable=True)     # null = not registered in AD_FILES
    match_status = Column(String(20), nullable=False)   # 'match' | 'mismatch' | 'fs_only'
    last_scan_run_id = Column(Integer, ForeignKey("diagnostic_runs.id", ondelete="SET NULL"), nullable=True)
    last_scanned_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
