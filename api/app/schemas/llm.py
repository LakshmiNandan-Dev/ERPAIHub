from pydantic import BaseModel
from typing import Optional
from datetime import datetime


# ══════════════════════════════════════════════════════════════════════════════
# Admin — LLM credentials
# ══════════════════════════════════════════════════════════════════════════════

class LlmCredentialBase(BaseModel):
    provider: str  # ollama | openai | anthropic | gemini
    label: Optional[str] = None
    model: Optional[str] = None
    base_url: Optional[str] = None
    is_default: bool = False
    is_active: bool = True


class LlmCredentialCreate(LlmCredentialBase):
    api_key: Optional[str] = None


class LlmCredentialUpdate(BaseModel):
    provider: Optional[str] = None
    label: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None  # set to update; omit to keep existing
    base_url: Optional[str] = None
    is_default: Optional[bool] = None
    is_active: Optional[bool] = None


class LlmCredentialOut(LlmCredentialBase):
    id: int
    has_api_key: bool = False
    created_at: datetime

    class Config:
        from_attributes = True
