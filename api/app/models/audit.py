from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, TIMESTAMP, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql.expression import text
from sqlalchemy.orm import relationship
from app.core.database import Base


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
