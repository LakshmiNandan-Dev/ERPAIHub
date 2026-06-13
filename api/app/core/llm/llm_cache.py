"""
Exact-match LLM response cache (Redis).

Caches deterministic completions — same (provider, model, messages) → same
output — so repeated background calls (history summaries, RLAIF audits, doc
extraction) skip re-inference entirely. Safe only for temperature-0 calls;
``llm_service.complete_sync`` runs every provider at temperature 0.

Like telemetry, every Redis operation is wrapped so the cache can never break a
request: on any failure it simply behaves as a miss. Disable with
LLM_CACHE_ENABLED=0.
"""
import os
import json
import hashlib
from typing import Optional

ENABLED = os.getenv("LLM_CACHE_ENABLED", "1").lower() not in ("0", "false", "no")
TTL_S = int(os.getenv("LLM_CACHE_TTL", "86400"))  # 24h
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

_KEY = "llmcache:resp:{}"
_redis = None


def _r():
    global _redis
    if _redis is None:
        import redis
        _redis = redis.Redis.from_url(
            REDIS_URL, decode_responses=True,
            socket_connect_timeout=1, socket_timeout=1,
        )
    return _redis


def _key(provider: str, model: Optional[str], messages: list,
         max_tokens=None, temperature=None) -> str:
    payload = json.dumps(
        {"p": provider, "m": model or "", "mt": max_tokens, "t": temperature, "msgs": messages},
        sort_keys=True, ensure_ascii=False,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return _KEY.format(digest)


def get(provider: str, model: Optional[str], messages: list,
        max_tokens=None, temperature=None) -> Optional[str]:
    """Return a cached completion for these exact inputs, or None."""
    if not ENABLED:
        return None
    try:
        return _r().get(_key(provider, model, messages, max_tokens, temperature))
    except Exception:
        return None


def set(provider: str, model: Optional[str], messages: list, response: str,
        max_tokens=None, temperature=None) -> None:
    """Store a completion under its exact inputs (no-op on empty response)."""
    if not ENABLED or not response:
        return
    try:
        _r().set(_key(provider, model, messages, max_tokens, temperature), response, ex=TTL_S)
    except Exception:
        pass
