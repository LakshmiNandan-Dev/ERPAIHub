from sqlalchemy import Column, Integer, Float, String, Boolean, ForeignKey, TIMESTAMP, Text
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
    # Output-token cap that applied to this call (max_tokens / num_predict), when
    # one was set. Null = uncapped / provider default. Shown in the task drill-down.
    token_limit = Column(Integer, nullable=True)
    context_chars = Column(Integer, nullable=False, server_default='0')
    duration_ms = Column(Integer, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'), index=True)


class InteractionLog(Base):
    """Durable per-interaction audit trail — one row per chat turn.

    Captures the complete request/response so an issue can be reproduced later:
    the user query, the retrieved RAG context, the prompt + model version that
    produced it, the generated response, latency, and (once submitted) the user's
    thumbs up/down. The user/session/message links are SET NULL on delete, but
    the denormalized fields (username, query, response, feedback) survive so the
    trail stays meaningful even after the source session is gone.
    """
    __tablename__ = "interaction_logs"

    id = Column(Integer, primary_key=True, nullable=False)
    session_id = Column(Integer, ForeignKey("chat_sessions.id", ondelete="SET NULL"), nullable=True, index=True)
    message_id = Column(Integer, ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    username = Column(String(100), nullable=True)
    query = Column(Text, nullable=False)                  # the user's question
    response = Column(Text, nullable=True)                # the generated answer
    rag_context = Column(Text, nullable=True)             # retrieved documents (grounding)
    rag_chunks = Column(Integer, nullable=False, server_default='0')  # chunks returned
    grounded = Column(Boolean, nullable=False, server_default=text('false'))
    # Retrieval signals captured for free from the cross-encoder rerank (see rag_service.query_rag):
    retrieval_top_k = Column(Integer, nullable=True)      # k requested
    retrieval_score = Column(Float, nullable=True)        # top reranked relevance score
    provider = Column(String(50), nullable=True)
    model = Column(String(150), nullable=True)            # model version
    prompt_key = Column(String(100), nullable=True)       # registry key in effect
    prompt_version = Column(String(64), nullable=True)    # hash of the resolved prompt
    prompt_tokens = Column(Integer, nullable=False, server_default='0')
    completion_tokens = Column(Integer, nullable=False, server_default='0')
    total_tokens = Column(Integer, nullable=False, server_default='0')
    latency_ms = Column(Integer, nullable=True)
    feedback_rating = Column(Integer, nullable=True)      # +1/-1, denormalized from the message
    # Per-message quality scores:
    accuracy_score = Column(Integer, nullable=True)       # LLM-judge factual correctness 0-100 (RLAIF audit)
    precision_score = Column(Integer, nullable=True)      # retrieval precision@k %: top-k chunks clearing the relevance floor (null when not grounded)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'), index=True)
