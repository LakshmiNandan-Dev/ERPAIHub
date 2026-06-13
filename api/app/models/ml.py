from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, TIMESTAMP, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql.expression import text
from sqlalchemy.orm import relationship
from app.core.database import Base


# ── Local-model fine-tuning (Ollama only) ──────────────────────────────────────

class TrainingExample(Base):
    """A single curated training row, per agent. kind='sft' uses prompt+chosen;
    kind='dpo' adds 'rejected' for preference tuning. Assembled from feedback,
    uploads, RAG documents or accepted agent runs."""
    __tablename__ = "training_examples"

    id = Column(Integer, primary_key=True, nullable=False)
    agent = Column(String(40), nullable=False)            # chat | deployment | performance | cloning | knowledge_base
    kind = Column(String(10), nullable=False, server_default='sft')   # sft | dpo
    source = Column(String(20), nullable=False, server_default='upload')  # feedback | upload | rag | agent_run
    system = Column(Text, nullable=True)
    prompt = Column(Text, nullable=False)
    chosen = Column(Text, nullable=False)
    rejected = Column(Text, nullable=True)                 # required for dpo
    is_active = Column(Boolean, server_default='TRUE', nullable=False)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))


class TrainingJob(Base):
    """A fine-tuning job for one agent: assembles a dataset, emits a runnable LoRA
    SFT→DPO bundle (run on a GPU host), then registers the resulting GGUF in Ollama."""
    __tablename__ = "training_jobs"

    id = Column(Integer, primary_key=True, nullable=False)
    agent = Column(String(40), nullable=False)
    base_model = Column(String(150), nullable=False)      # an Ollama base, e.g. llama3.2:1b
    method = Column(String(10), nullable=False, server_default='both')  # sft | dpo | both
    target_model_tag = Column(String(150), nullable=False)  # resulting Ollama tag, e.g. oraebs-chat:v1
    status = Column(String(20), nullable=False, server_default='draft')  # draft | bundled | registered | failed
    sft_count = Column(Integer, nullable=False, server_default='0')
    dpo_count = Column(Integer, nullable=False, server_default='0')
    params = Column(JSONB, nullable=True)                  # epochs, lr, lora_r, sources...
    notes = Column(Text, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    bundled_at = Column(TIMESTAMP(timezone=True), nullable=True)
    registered_at = Column(TIMESTAMP(timezone=True), nullable=True)


class AgentModel(Base):
    """Per-agent active local model. When an agent runs on the Ollama provider and
    has a mapping here, the gateway uses this (fine-tuned) model tag for that agent."""
    __tablename__ = "agent_models"

    id = Column(Integer, primary_key=True, nullable=False)
    agent = Column(String(40), unique=True, nullable=False)
    provider = Column(String(20), nullable=False, server_default='ollama')
    model = Column(String(150), nullable=False)
    training_job_id = Column(Integer, ForeignKey("training_jobs.id", ondelete="SET NULL"), nullable=True)
    is_active = Column(Boolean, server_default='TRUE', nullable=False)
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
