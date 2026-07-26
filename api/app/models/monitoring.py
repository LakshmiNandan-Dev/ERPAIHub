from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, TIMESTAMP, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql.expression import text
from app.core.database import Base


class MonitoringSchedule(Base):
    """Per-environment scheduled diagnostic scan config, reconciled into the
    in-process APScheduler on admin edit and app startup."""
    __tablename__ = "monitoring_schedules"

    id = Column(Integer, primary_key=True, nullable=False)
    environment_id = Column(Integer, ForeignKey("ebs_environments.id", ondelete="CASCADE"),
                            nullable=False, unique=True)
    enabled = Column(Boolean, server_default='FALSE', nullable=False)
    interval_minutes = Column(Integer, nullable=False, server_default='60')
    areas = Column(JSONB, nullable=True)   # subset of ALL_AREAS; null/empty = all
    last_run_at = Column(TIMESTAMP(timezone=True), nullable=True)
    last_run_status = Column(String(20), nullable=True)   # 'ok' | 'error'
    last_error = Column(Text, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))


class DiagnosticRun(Base):
    """One scan execution — performance areas or a patch-gap scan. Shared
    ledger so Findings from both sources diff/ticket the same way."""
    __tablename__ = "diagnostic_runs"

    id = Column(Integer, primary_key=True, nullable=False)
    environment_id = Column(Integer, ForeignKey("ebs_environments.id", ondelete="SET NULL"), nullable=True)
    environment_name = Column(String(50), nullable=True)   # snapshot
    kind = Column(String(20), nullable=False)   # 'performance' | 'patch_gap' | 'patch_file_gap'
    trigger = Column(String(20), nullable=False, server_default='manual')   # 'manual' | 'scheduled'
    areas = Column(JSONB, nullable=True)         # areas/components actually scanned
    data_source = Column(String(20), nullable=False, server_default='simulated')  # live|simulated|mixed|error
    status = Column(String(20), nullable=False, server_default='running')   # running|completed|failed
    new_findings_count = Column(Integer, nullable=False, server_default='0')
    changed_findings_count = Column(Integer, nullable=False, server_default='0')
    resolved_findings_count = Column(Integer, nullable=False, server_default='0')
    error = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    completed_at = Column(TIMESTAMP(timezone=True), nullable=True)


class Finding(Base):
    """Current state of a distinct issue, one row per (environment, source,
    area, finding_key) — upserted every run, not one row per occurrence."""
    __tablename__ = "findings"
    __table_args__ = (
        UniqueConstraint('environment_id', 'source', 'area', 'finding_key',
                         name='ux_findings_env_source_area_key'),
    )

    id = Column(Integer, primary_key=True, nullable=False)
    environment_id = Column(Integer, ForeignKey("ebs_environments.id", ondelete="SET NULL"), nullable=True)
    environment_name = Column(String(50), nullable=True)
    source = Column(String(20), nullable=False, server_default='performance')  # 'performance' | 'patch_gap' | 'patch_file_gap'
    area = Column(String(50), nullable=False)     # diagnostic area, component name for patch_gap, or product top for patch_file_gap
    finding_key = Column(String(255), nullable=False)
    severity = Column(String(20), nullable=False, server_default='info')   # info|warning|critical
    title = Column(String(255), nullable=False)
    detail = Column(JSONB, nullable=True)          # offending row / metric snapshot / target-vs-applied
    value_hash = Column(String(64), nullable=True)  # sha256 of the fields that drive "changed" detection
    status = Column(String(20), nullable=False, server_default='open')     # open|resolved
    first_seen_run_id = Column(Integer, ForeignKey("diagnostic_runs.id", ondelete="SET NULL"), nullable=True)
    first_seen_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    last_seen_run_id = Column(Integer, ForeignKey("diagnostic_runs.id", ondelete="SET NULL"), nullable=True)
    last_seen_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    last_changed_at = Column(TIMESTAMP(timezone=True), nullable=True)
    resolved_run_id = Column(Integer, ForeignKey("diagnostic_runs.id", ondelete="SET NULL"), nullable=True)
    resolved_at = Column(TIMESTAMP(timezone=True), nullable=True)
    occurrence_count = Column(Integer, nullable=False, server_default='1')
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
