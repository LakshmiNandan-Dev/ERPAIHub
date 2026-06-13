from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, TIMESTAMP, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql.expression import text
from sqlalchemy.orm import relationship
from app.core.database import Base


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
