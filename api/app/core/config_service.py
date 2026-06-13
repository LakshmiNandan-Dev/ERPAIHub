"""
Server-side LLM credential resolution.

Browser requests carry the chosen provider/model (X-LLM-* headers) but no longer
carry the API key. This resolver fills in the key (and optionally model/base_url)
from the admin-managed, encrypted LlmCredential store at use-time.
"""
from typing import Optional, Tuple
from urllib.parse import urlparse

from app import models
from app.core import crypto
from app.core.llm import model_router


def agent_model(db, agent: Optional[str]) -> Optional[str]:
    """The fine-tuned local model mapped to an agent (Ollama only), or None."""
    if not agent:
        return None
    try:
        am = (db.query(models.AgentModel)
              .filter(models.AgentModel.agent == agent,
                      models.AgentModel.is_active == True)  # noqa: E712
              .first())
        return am.model if am else None
    except Exception:
        return None


def resolve_llm(
    provider: str,
    model: Optional[str],
    api_key: Optional[str],
    base_url: Optional[str],
    db,
    agent: Optional[str] = None,
    route_text: Optional[str] = None,
) -> Tuple[str, Optional[str], Optional[str], Optional[str]]:
    """
    Return effective (provider, model, api_key, base_url).

    Resolution order:
      1. Per-agent routing (model_router): a different provider per agent and a
         complexity-based small/large model choice driven by ``route_text``.
         Only engages when the request didn't pin a concrete model (empty or
         "auto"); an explicit model wins. See app.core.llm.model_router.
      2. Ollama fine-tuned model: an agent with a trained local model uses its
         mapped tag (cloud providers unaffected).
      3. Credential fill: if the request had no api_key, the default active
         credential for the provider supplies the key and model/base_url fallbacks.
    """
    # 1. Per-agent provider + complexity routing (no-op unless opted in).
    provider, model, route_reason = model_router.resolve(db, agent, provider, model, route_text)
    if route_reason not in ("explicit", "no-route"):
        print(f"[LLMRouter] agent={agent} provider={provider} model={model} via {route_reason}")

    # 2. Ollama fine-tuned per-agent model takes precedence for local inference.
    if provider == "ollama":
        tuned = agent_model(db, agent)
        if tuned:
            model = tuned

    if api_key:
        return provider, model, api_key, base_url

    cred = (
        db.query(models.LlmCredential)
        .filter(
            models.LlmCredential.provider == provider,
            models.LlmCredential.is_active == True,  # noqa: E712
        )
        .order_by(models.LlmCredential.is_default.desc(), models.LlmCredential.id.asc())
        .first()
    )
    if not cred:
        return provider, model, api_key, base_url

    resolved_key = crypto.decrypt(cred.api_key_enc) or api_key
    return (
        provider,
        model or cred.model,
        resolved_key,
        base_url or cred.base_url,
    )


# ── Git / Confluence credential resolution (admin-managed, encrypted) ──────────

def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _find_integration(db, kind: str, url: str):
    """Pick the integration credential for a URL: host match → default → any."""
    rows = (
        db.query(models.IntegrationCredential)
        .filter(
            models.IntegrationCredential.kind == kind,
            models.IntegrationCredential.is_active == True,  # noqa: E712
        )
        .all()
    )
    host = _host_of(url)
    if host:
        for r in rows:
            if r.host and r.host.lower() in host:
                return r
    for r in rows:
        if r.is_default:
            return r
    return rows[0] if rows else None


def has_integration(db, kind: str) -> bool:
    return (
        db.query(models.IntegrationCredential.id)
        .filter(
            models.IntegrationCredential.kind == kind,
            models.IntegrationCredential.is_active == True,  # noqa: E712
        )
        .first()
        is not None
    )


def resolve_git_token(repo_url: str, db) -> Tuple[Optional[str], Optional[str]]:
    """Return (username, token) for a Git repo URL, or (None, None)."""
    cred = _find_integration(db, "git", repo_url or "")
    if not cred:
        return None, None
    return cred.username, crypto.decrypt(cred.secret_enc)


def resolve_confluence_auth(page_url: str, db) -> Optional[str]:
    """
    Return the auth string accepted by the Confluence fetcher:
    'email:token' for Cloud (Basic) or a bare PAT for Server/DC.
    """
    cred = _find_integration(db, "confluence", page_url or "")
    if not cred:
        return None
    token = crypto.decrypt(cred.secret_enc) or ""
    if not token:
        return None
    if cred.username and ":" not in token:
        return f"{cred.username}:{token}"
    return token
