"""
Semantic response cache.

Returns a previously generated answer when a new question is *semantically*
near-identical to one already answered — instant replies for repeated/FAQ-style
queries without re-running the model. It reuses the RAG embedding model and the
shared ChromaDB client (a dedicated ``semantic_cache`` collection).

Correctness guardrails:
  * Callers must only consult this for standalone, context-free questions (the
    first turn of a session, no conversation history the answer depends on).
  * Matching is scoped by provider+model, so different models never share an
    answer.
  * A high cosine-similarity threshold avoids loose matches.
  * Entries carry a TTL so answers can't go stale indefinitely.

Every operation degrades silently on error (treated as a miss). Disable with
SEMANTIC_CACHE_ENABLED=0.
"""
import os
import time
import uuid
from typing import Optional

from app.core.rag import rag_service

ENABLED = os.getenv("SEMANTIC_CACHE_ENABLED", "1").lower() not in ("0", "false", "no")
# Minimum cosine similarity for a hit. ChromaDB cosine distance = 1 - similarity.
THRESHOLD = float(os.getenv("SEMANTIC_CACHE_THRESHOLD", "0.95"))
TTL_S = int(os.getenv("SEMANTIC_CACHE_TTL", "21600"))  # 6h
COLLECTION = "semantic_cache"

_MAX_DISTANCE = 1.0 - THRESHOLD


def _norm(text: str) -> str:
    """Whitespace/case-normalise so trivial formatting differences still match."""
    return " ".join((text or "").split()).lower()


def lookup(query: str, provider: str, model: Optional[str]) -> Optional[str]:
    """Return a cached answer for a near-identical question, or None."""
    if not ENABLED or not query:
        return None
    try:
        col = rag_service.get_named_collection(COLLECTION)
        if col.count() == 0:
            return None

        emb = [rag_service.embed_query(_norm(query))]
        res = col.query(
            query_embeddings=emb,
            n_results=1,
            include=["metadatas", "distances"],
        )
        dists = (res.get("distances") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        if not dists:
            return None

        dist, meta = dists[0], metas[0]
        if dist > _MAX_DISTANCE:
            return None
        # Different model/provider would have produced a different answer.
        if meta.get("provider") != provider or meta.get("model") != (model or ""):
            return None
        # Expire stale entries (Chroma has no native TTL — enforce on read).
        if TTL_S > 0 and (time.time() - float(meta.get("created_at", 0))) > TTL_S:
            try:
                col.delete(ids=[meta.get("id")])
            except Exception:
                pass
            return None

        return meta.get("response") or None
    except Exception:
        return None


def store(query: str, response: str, provider: str, model: Optional[str]) -> None:
    """Record a fresh standalone answer for future semantic matches."""
    if not ENABLED or not query or not response:
        return
    try:
        col = rag_service.get_named_collection(COLLECTION)
        emb = [rag_service.embed_query(_norm(query))]
        cid = uuid.uuid4().hex
        col.upsert(
            ids=[cid],
            embeddings=emb,
            documents=[query],
            metadatas=[{
                "id": cid,
                "response": response,
                "provider": provider,
                "model": model or "",
                "created_at": time.time(),
            }],
        )
    except Exception:
        pass
