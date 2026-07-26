from sqlalchemy import Column, Integer, String, ForeignKey, TIMESTAMP, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql.expression import text
from app.core.database import Base


class EnvironmentConfigSnapshot(Base):
    """Result of a live EBS config scan (one category) for one environment —
    append-only history, mirrors AppliedPatchSnapshot (models/dba.py) exactly.
    Diff computation (compare_service.compute_config_diff) reads the latest
    row per (environment_id, category)."""
    __tablename__ = "environment_config_snapshots"

    id = Column(Integer, primary_key=True, nullable=False)
    environment_id = Column(Integer, ForeignKey("ebs_environments.id", ondelete="CASCADE"), nullable=False)
    category = Column(String(30), nullable=False)   # 'db_parameters' | 'profile_options' | 'responsibilities'
    payload = Column(JSONB, nullable=False, server_default='[]')   # [{name, value, ...}] — shape varies by category
    scan_status = Column(String(20), nullable=False, server_default='ok')   # ok|error
    scan_error = Column(Text, nullable=True)
    scanned_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    scanned_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
