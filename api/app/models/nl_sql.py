from sqlalchemy import Column, Integer, String, Text, ForeignKey, TIMESTAMP
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql.expression import text
from app.core.database import Base


class NlSqlSchemaSnapshot(Base):
    """Result of a TinyLLM catalog extraction (EbsExtractor over a live oracledb
    cursor) for one environment — append-only history, mirrors
    EnvironmentConfigSnapshot/AppliedPatchSnapshot exactly. Both training and the
    QueryService pool read the LATEST scan_status='ok' row."""
    __tablename__ = "nlsql_schema_snapshots"

    id = Column(Integer, primary_key=True, nullable=False)
    environment_id = Column(Integer, ForeignKey("ebs_environments.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    schema_json = Column(JSONB, nullable=True)     # schema_to_dict(Schema); null when scan_status='error'
    table_count = Column(Integer, nullable=True)
    fk_count = Column(Integer, nullable=True)
    scan_status = Column(String(20), nullable=False, server_default='ok')   # ok|error
    scan_error = Column(Text, nullable=True)
    scanned_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    scanned_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)


class NlSqlTrainingRun(Base):
    """A TinyLLM fine-tune run for one environment (subprocess via
    tinyllm.serve.jobs.TrainJob) — one row per attempt, mirrors PatchRun/
    DeploymentRun/CloneRun. checkpoint_path populates on completion; the
    QueryService pool builds from the latest status='done' row per environment."""
    __tablename__ = "nlsql_training_runs"

    id = Column(Integer, primary_key=True, nullable=False)
    environment_id = Column(Integer, ForeignKey("ebs_environments.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    schema_snapshot_id = Column(Integer, ForeignKey("nlsql_schema_snapshots.id", ondelete="SET NULL"),
                                nullable=True)
    steps = Column(Integer, nullable=False, server_default='300')
    n_train = Column(Integer, nullable=False, server_default='800')
    checkpoint_path = Column(String(512), nullable=True)
    log_path = Column(String(512), nullable=True)
    status = Column(String(20), nullable=False, server_default='pending')   # pending|running|done|error
    error = Column(Text, nullable=True)
    started_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    completed_at = Column(TIMESTAMP(timezone=True), nullable=True)
    started_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
