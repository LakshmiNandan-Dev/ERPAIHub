from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class RagDocumentOut(BaseModel):
    id: int
    filename: str
    file_type: str
    status: str
    chunk_count: Optional[int] = None
    error_message: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True
