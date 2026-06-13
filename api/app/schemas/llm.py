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


# ══════════════════════════════════════════════════════════════════════════════
# Admin — per-agent LLM routing policy
# ══════════════════════════════════════════════════════════════════════════════

class AgentLlmPolicyBase(BaseModel):
    agent: str                                    # chat | deployment | performance | cloning | knowledge_base | patching
    provider: Optional[str] = None                # null = keep request/default provider
    small_model: Optional[str] = None             # null = provider's built-in small default
    large_model: Optional[str] = None             # null = provider's built-in large default
    routing_enabled: bool = True
    force_tier: Optional[str] = None              # 'small' | 'large' | null (classify)
    is_active: bool = True


class AgentLlmPolicyCreate(AgentLlmPolicyBase):
    pass


class AgentLlmPolicyUpdate(BaseModel):
    provider: Optional[str] = None
    small_model: Optional[str] = None
    large_model: Optional[str] = None
    routing_enabled: Optional[bool] = None
    force_tier: Optional[str] = None
    is_active: Optional[bool] = None


class AgentLlmPolicyOut(AgentLlmPolicyBase):
    id: int
    created_at: datetime

    class Config:
        from_attributes = True


class AgentRoutingMeta(BaseModel):
    """Reference data for the Admin UI: known agents + per-provider tier defaults."""
    agents: list[str]
    tier_defaults: dict[str, dict[str, str]]
    routing_enabled: bool
