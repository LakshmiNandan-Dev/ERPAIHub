from sqlalchemy import Column, Integer, String, ForeignKey, TIMESTAMP, Text
from sqlalchemy.sql.expression import text
from app.core.database import Base


class Ticket(Base):
    """A tracked work item, created manually or converted from a Finding
    (see models/monitoring.py). Status/priority follow the plain-string
    convention used throughout models/dba.py — no Python Enum, no DB CHECK."""
    __tablename__ = "tickets"

    id = Column(Integer, primary_key=True, nullable=False)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, server_default='open')      # open|in_progress|resolved|dismissed
    priority = Column(String(20), nullable=False, server_default='medium')  # low|medium|high|critical
    environment_id = Column(Integer, ForeignKey("ebs_environments.id", ondelete="SET NULL"), nullable=True)
    environment_name = Column(String(50), nullable=True)   # snapshot
    finding_id = Column(Integer, ForeignKey("findings.id", ondelete="SET NULL"), nullable=True)
    source_run_id = Column(Integer, ForeignKey("diagnostic_runs.id", ondelete="SET NULL"), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    assignee_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    resolution_notes = Column(Text, nullable=True)
    resolved_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    resolved_at = Column(TIMESTAMP(timezone=True), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
