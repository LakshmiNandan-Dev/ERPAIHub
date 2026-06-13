from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, TIMESTAMP, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql.expression import text
from sqlalchemy.orm import relationship
from app.core.database import Base


class RagDocument(Base):
    __tablename__ = "rag_documents"

    id = Column(Integer, primary_key=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    filename = Column(String(512), nullable=False)
    file_type = Column(String(50), nullable=False)
    # SHA-256 of the raw uploaded bytes — used to reject duplicate uploads of
    # identical content (regardless of filename). Indexed for fast lookup.
    content_hash = Column(String(64), nullable=True, index=True)
    status = Column(String(50), nullable=False, server_default='indexing')  # indexing | ready | failed
    chunk_count = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))

    user = relationship("User", backref="rag_documents")
