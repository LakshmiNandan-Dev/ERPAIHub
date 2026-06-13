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
) -> Tuple[str, Optional[str], Optional[str], Optional[str]]:
    """
    Return effective (provider, model, api_key, base_url).

    If the request already supplied an api_key it wins (back-compat). Otherwise
    look up the default active credential for the provider and use its decrypted
    key, plus its model/base_url as fallbacks when the request didn't specify them.

    For the Ollama (local) provider, an agent that has been fine-tuned uses its
    mapped model tag — this is how a trained local model takes effect per agent.
    Cloud providers are never affected.
    """
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
