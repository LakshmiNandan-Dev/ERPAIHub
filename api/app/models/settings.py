from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, TIMESTAMP, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql.expression import text
from sqlalchemy.orm import relationship
from app.core.database import Base


class IntegrationCredential(Base):
    """
    Admin-managed Git / Confluence credentials, encrypted at rest, resolved
    server-side at use-time. `host` is matched against the repo/page URL host;
    `is_default` is the fallback when no host matches.
    """
    __tablename__ = "integration_credentials"

    id = Column(Integer, primary_key=True, nullable=False)
    kind = Column(String(20), nullable=False)         # 'git' | 'confluence'
    name = Column(String(100), nullable=False)
    host = Column(String(255), nullable=True)         # e.g. github.com, gitlab.corp.local, mysite.atlassian.net
    base_url = Column(String(512), nullable=True)     # optional full base (confluence site)
    username = Column(String(255), nullable=True)     # git user, or Confluence Cloud email
    secret_enc = Column(Text, nullable=True)          # encrypted PAT / API token
    is_default = Column(Boolean, server_default='FALSE', nullable=False)
    is_active = Column(Boolean, server_default='TRUE', nullable=False)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))


class SsoSettings(Base):
    """Single-row store (id=1) for OIDC SSO configuration. Secret stored encrypted."""
    __tablename__ = "sso_settings"

    id = Column(Integer, primary_key=True, nullable=False)
    provider = Column(String(50), nullable=False, server_default='entra')  # microsoft entra id
    enabled = Column(Boolean, server_default='FALSE', nullable=False)
    signup_enabled = Column(Boolean, server_default='FALSE', nullable=False)  # public self sign-up
    tenant_id = Column(String(255), nullable=True)
    client_id = Column(String(255), nullable=True)
    client_secret_enc = Column(Text, nullable=True)  # encrypted
    redirect_uri = Column(String(512), nullable=True)
    auto_provision = Column(Boolean, server_default='TRUE', nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))


class LlmCredential(Base):
    __tablename__ = "llm_credentials"

    id = Column(Integer, primary_key=True, nullable=False)
    provider = Column(String(50), nullable=False)  # ollama | openai | anthropic | gemini
    label = Column(String(100), nullable=True)
    model = Column(String(150), nullable=True)
    api_key_enc = Column(Text, nullable=True)  # encrypted
    base_url = Column(String(512), nullable=True)
    is_default = Column(Boolean, server_default='FALSE', nullable=False)
    is_active = Column(Boolean, server_default='TRUE', nullable=False)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))


class AgentLlmPolicy(Base):
    """
    Per-agent LLM routing policy (admin-managed). Lets each agent
    (chat | deployment | performance | cloning | knowledge_base | patching)
    pick its own provider and a small/large model pair. At request time a
    heuristic complexity classifier routes simple tasks to ``small_model`` and
    complex ones to ``large_model``. ``force_tier`` pins a tier (skip the
    classifier); ``provider`` (null = use the request/default provider) enables
    a different provider per agent. See app.core.llm.model_router.
    """
    __tablename__ = "agent_llm_policies"

    id = Column(Integer, primary_key=True, nullable=False)
    agent = Column(String(40), unique=True, nullable=False)
    provider = Column(String(20), nullable=True)        # null = keep request/default provider
    small_model = Column(String(150), nullable=True)    # null = provider's built-in small default
    large_model = Column(String(150), nullable=True)    # null = provider's built-in large default
    routing_enabled = Column(Boolean, server_default='TRUE', nullable=False)
    force_tier = Column(String(10), nullable=True)      # 'small' | 'large' | null (classify)
    is_active = Column(Boolean, server_default='TRUE', nullable=False)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))


class AgentPrompt(Base):
    """
    Admin override for an agent prompt. The canonical default for each prompt
    lives in code (app.core.prompts); a row here overrides it by ``prompt_key``.
    Absence of a row = use the built-in default (a 'reset' deletes the row).
    """
    __tablename__ = "agent_prompts"

    id = Column(Integer, primary_key=True, nullable=False)
    prompt_key = Column(String(80), unique=True, nullable=False, index=True)
    content = Column(Text, nullable=False)
    updated_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
