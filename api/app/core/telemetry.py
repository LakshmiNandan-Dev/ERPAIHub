"""
Application telemetry.

Live state (connected users, counters, open-stream gauge, per-user current
usage) lives in Redis; durable per-LLM-call history is appended to the
usage_events table. Every Redis/DB operation is wrapped so telemetry can never
break a request — failures degrade silently.
"""
import os
import time
from datetime import datetime, timezone, timedelta

from sqlalchemy import case, func

from app.core import database
from app import models

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
ACTIVE_WINDOW_S = 300  # a user is "connected" if seen within this many seconds

# Redis keys
K_REQ = "tlm:req:total"
K_BYTES_OUT = "tlm:bytes:out"
K_BYTES_IN = "tlm:bytes:in"
K_TOK_PROMPT = "tlm:tok:prompt"
K_TOK_COMPLETION = "tlm:tok:completion"
K_STREAMS = "tlm:streams"
K_ACTIVE = "tlm:active"            # zset user_id -> last_seen epoch
K_USER = "tlm:user:{}"            # hash per user
K_PROVIDER = "tlm:provider:{}"    # hash per provider
K_TOK2USER = "tlm:tok2user:{}"   # token -> "id|username" cache
# RAG retrieval metrics (live counters)
K_RAG_Q = "tlm:rag:queries"          # total retrieval attempts
K_RAG_HIT = "tlm:rag:hits"           # retrievals that returned KB context
K_RAG_CACHE = "tlm:rag:cache_hits"   # served from the retrieval cache
K_RAG_WEB = "tlm:rag:web"            # grounded via web fallback
K_RAG_CHUNKS = "tlm:rag:chunks_sum"  # Σ chunks returned (for avg over hits)
K_RAG_SCORE_SUM = "tlm:rag:score_sum"  # Σ top rerank score (hits only)
K_RAG_SCORE_CNT = "tlm:rag:score_cnt"
K_RAG_LAT_SUM = "tlm:rag:lat_ms_sum"   # Σ retrieval latency (ms)
K_RAG_LAT_CNT = "tlm:rag:lat_cnt"
# RAG-grounded generation metrics (substantive chat turns only)
K_GEN_CNT = "tlm:gen:count"
K_GEN_GROUNDED = "tlm:gen:grounded"    # answers produced with KB context
K_GEN_LAT_SUM = "tlm:gen:lat_ms_sum"   # Σ generation latency (ms)

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


def est_tokens(text: str) -> int:
    return max(1, len(text or "") // 4)


# ── Capture ────────────────────────────────────────────────────────────────────

def record_http(user_id, username, endpoint, method, status, req_bytes, resp_bytes, duration_ms):
    try:
        r = _r()
        pipe = r.pipeline()
        pipe.incr(K_REQ)
        pipe.incrby(K_BYTES_OUT, int(resp_bytes or 0))
        pipe.incrby(K_BYTES_IN, int(req_bytes or 0))
        if user_id:
            now = time.time()
            pipe.zadd(K_ACTIVE, {str(user_id): now})
            uk = K_USER.format(user_id)
            pipe.hincrby(uk, "requests", 1)
            pipe.hincrby(uk, "bytes_out", int(resp_bytes or 0))
            pipe.hincrby(uk, "bytes_in", int(req_bytes or 0))
            pipe.hset(uk, mapping={
                "username": username or "",
                "last_endpoint": f"{method} {endpoint}",
                "last_seen": now,
            })
            pipe.expire(uk, 86400)
        pipe.execute()
    except Exception:
        pass


def record_llm(user_id, username, endpoint, provider, model,
               prompt_tokens, completion_tokens, context_chars, token_limit=None):
    prompt_tokens = int(prompt_tokens or 0)
    completion_tokens = int(completion_tokens or 0)
    total = prompt_tokens + completion_tokens
    # Redis live counters
    try:
        r = _r()
        pipe = r.pipeline()
        pipe.incrby(K_TOK_PROMPT, prompt_tokens)
        pipe.incrby(K_TOK_COMPLETION, completion_tokens)
        if provider:
            pk = K_PROVIDER.format(provider)
            pipe.hincrby(pk, "prompt_tokens", prompt_tokens)
            pipe.hincrby(pk, "completion_tokens", completion_tokens)
            pipe.hincrby(pk, "calls", 1)
        if user_id:
            now = time.time()
            pipe.zadd(K_ACTIVE, {str(user_id): now})
            uk = K_USER.format(user_id)
            pipe.hincrby(uk, "prompt_tokens", prompt_tokens)
            pipe.hincrby(uk, "completion_tokens", completion_tokens)
            pipe.hset(uk, mapping={
                "username": username or "",
                "context_chars": int(context_chars or 0),
                "est_context_tokens": est_tokens(" " * int(context_chars or 0)),
                "last_seen": now,
            })
            pipe.expire(uk, 86400)
        pipe.execute()
    except Exception:
        pass
    # Durable history
    try:
        db = database.SessionLocal()
        try:
            db.add(models.UsageEvent(
                kind="llm", user_id=user_id, username=username, endpoint=endpoint,
                provider=provider, model=model,
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                total_tokens=total, context_chars=int(context_chars or 0),
                token_limit=int(token_limit) if token_limit else None,
            ))
            db.commit()
        finally:
            db.close()
    except Exception:
        pass


def record_rag(hit, cached=False, from_web=False, num_chunks=0, top_score=None, latency_ms=None):
    """Capture one RAG retrieval for the monitoring RAG panel. ``hit`` = context
    was returned (vs. a confident miss that makes the model abstain)."""
    try:
        r = _r()
        pipe = r.pipeline()
        pipe.incr(K_RAG_Q)
        if hit:
            pipe.incr(K_RAG_HIT)
            pipe.incrby(K_RAG_CHUNKS, int(num_chunks or 0))
            if top_score is not None:
                pipe.incrbyfloat(K_RAG_SCORE_SUM, float(top_score))
                pipe.incr(K_RAG_SCORE_CNT)
        if cached:
            pipe.incr(K_RAG_CACHE)
        if from_web:
            pipe.incr(K_RAG_WEB)
        if latency_ms is not None:
            pipe.incrbyfloat(K_RAG_LAT_SUM, float(latency_ms))
            pipe.incr(K_RAG_LAT_CNT)
        pipe.execute()
    except Exception:
        pass


def record_interaction(*, user_id=None, username=None, session_id=None,
                       message_id=None, query="", response=None, rag_context=None,
                       rag_chunks=0, grounded=False, provider=None, model=None,
                       prompt_key=None, prompt_tokens=0, completion_tokens=0,
                       latency_ms=None):
    """Append one durable interaction-audit row capturing the full request: the
    query, retrieved RAG context, prompt + model version, response, latency and
    token usage. Best-effort — never raises, so it can't break a chat turn."""
    try:
        version = None
        if prompt_key:
            try:
                from app.core import prompts  # lazy: avoid import cycle at load
                version = prompts.prompt_version(prompt_key)
            except Exception:
                version = None
        pt, ct = int(prompt_tokens or 0), int(completion_tokens or 0)
        db = database.SessionLocal()
        try:
            db.add(models.InteractionLog(
                user_id=user_id, username=username, session_id=session_id,
                message_id=message_id, query=query or "", response=response,
                rag_context=(rag_context or None), rag_chunks=int(rag_chunks or 0),
                grounded=bool(grounded), provider=provider, model=model,
                prompt_key=prompt_key, prompt_version=version,
                prompt_tokens=pt, completion_tokens=ct, total_tokens=pt + ct,
                latency_ms=int(latency_ms) if latency_ms is not None else None,
            ))
            db.commit()
        finally:
            db.close()
    except Exception:
        pass


def update_interaction_feedback(message_id, rating):
    """Denormalize a thumbs up/down onto the interaction-audit row for a message,
    so the audit trail stays self-contained. Best-effort."""
    if not message_id:
        return
    try:
        db = database.SessionLocal()
        try:
            db.query(models.InteractionLog).filter(
                models.InteractionLog.message_id == message_id
            ).update({"feedback_rating": rating})
            db.commit()
        finally:
            db.close()
    except Exception:
        pass


def record_generation(grounded=False, latency_ms=None):
    """Capture one substantive (RAG-eligible) chat generation. ``grounded`` =
    the answer was produced with retrieved KB context."""
    try:
        r = _r()
        pipe = r.pipeline()
        pipe.incr(K_GEN_CNT)
        if grounded:
            pipe.incr(K_GEN_GROUNDED)
        if latency_ms is not None:
            pipe.incrbyfloat(K_GEN_LAT_SUM, float(latency_ms))
        pipe.execute()
    except Exception:
        pass


def stream_open(user_id=None):
    try:
        _r().incr(K_STREAMS)
    except Exception:
        pass


def stream_close(user_id=None):
    try:
        # never let the gauge go negative
        if int(_r().get(K_STREAMS) or 0) > 0:
            _r().decr(K_STREAMS)
    except Exception:
        pass


# ── Token → user resolution (cached) ───────────────────────────────────────────

def resolve_user_from_token(token: str):
    if not token:
        return None, None
    try:
        cached = _r().get(K_TOK2USER.format(token))
        if cached:
            uid, _, uname = cached.partition("|")
            return int(uid), uname
    except Exception:
        pass

    user_id = username = None
    try:
        db = database.SessionLocal()
        try:
            sess = db.query(models.UserSession).filter(
                models.UserSession.session_token == token,
                models.UserSession.is_active == True,  # noqa: E712
            ).first()
            if sess and sess.expires_at and sess.expires_at >= datetime.now(timezone.utc):
                user = db.query(models.User).filter(models.User.id == sess.user_id).first()
                if user:
                    user_id, username = user.id, user.username
        finally:
            db.close()
    except Exception:
        return None, None

    if user_id:
        try:
            _r().set(K_TOK2USER.format(token), f"{user_id}|{username}", ex=60)
        except Exception:
            pass
    return user_id, username


# ── Read for the monitoring console ────────────────────────────────────────────

def _int(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _flt(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def get_active_users(window_s: int = ACTIVE_WINDOW_S):
    out = []
    try:
        r = _r()
        cutoff = time.time() - window_s
        ids = r.zrangebyscore(K_ACTIVE, cutoff, "+inf")
        for uid in ids:
            h = r.hgetall(K_USER.format(uid)) or {}
            pt, ct = _int(h.get("prompt_tokens")), _int(h.get("completion_tokens"))
            out.append({
                "user_id": _int(uid),
                "username": h.get("username") or f"user{uid}",
                "requests": _int(h.get("requests")),
                "bytes_out": _int(h.get("bytes_out")),
                "bytes_in": _int(h.get("bytes_in")),
                "prompt_tokens": pt,
                "completion_tokens": ct,
                "total_tokens": pt + ct,
                "context_chars": _int(h.get("context_chars")),
                "est_context_tokens": _int(h.get("est_context_tokens")),
                "last_endpoint": h.get("last_endpoint") or "",
                "last_seen": float(h.get("last_seen") or 0),
            })
        out.sort(key=lambda u: u["last_seen"], reverse=True)
    except Exception:
        pass
    return out


def get_overview(window_s: int = ACTIVE_WINDOW_S):
    live = {
        "connected_users": 0, "open_streams": 0, "total_requests": 0,
        "bytes_in": 0, "bytes_out": 0,
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        "providers": [], "tokens_24h": 0,
    }
    try:
        r = _r()
        cutoff = time.time() - window_s
        live["connected_users"] = r.zcount(K_ACTIVE, cutoff, "+inf")
        live["open_streams"] = max(0, _int(r.get(K_STREAMS)))
        live["total_requests"] = _int(r.get(K_REQ))
        live["bytes_in"] = _int(r.get(K_BYTES_IN))
        live["bytes_out"] = _int(r.get(K_BYTES_OUT))
        pt, ct = _int(r.get(K_TOK_PROMPT)), _int(r.get(K_TOK_COMPLETION))
        live["prompt_tokens"], live["completion_tokens"], live["total_tokens"] = pt, ct, pt + ct
        for key in r.scan_iter(match="tlm:provider:*", count=50):
            h = r.hgetall(key) or {}
            live["providers"].append({
                "provider": key.split(":")[-1],
                "prompt_tokens": _int(h.get("prompt_tokens")),
                "completion_tokens": _int(h.get("completion_tokens")),
                "calls": _int(h.get("calls")),
            })
    except Exception:
        pass
    # Durable totals (survive Redis restarts): 24h tokens + all-time task count.
    # A "task" is one LLM call (usage_events row with kind='llm').
    live["total_tasks"] = 0
    try:
        db = database.SessionLocal()
        try:
            since = datetime.now(timezone.utc) - timedelta(hours=24)
            live["tokens_24h"] = _int(
                db.query(func.coalesce(func.sum(models.UsageEvent.total_tokens), 0))
                .filter(models.UsageEvent.kind == "llm", models.UsageEvent.created_at >= since)
                .scalar()
            )
            live["total_tasks"] = _int(
                db.query(func.count(models.UsageEvent.id))
                .filter(models.UsageEvent.kind == "llm")
                .scalar()
            )
        finally:
            db.close()
    except Exception:
        pass
    return live


def get_tasks(limit: int = 100, offset: int = 0, date_from=None, date_to=None):
    """
    Recent LLM tasks (one per call) for the monitoring drill-down: input/output
    tokens and the output-token limit that applied. Returns {tasks, total, ...}.

    ``date_from`` / ``date_to`` optionally restrict the window (the UI defaults to
    the last 24 hours); ``total`` reflects the count within that window.
    """
    limit = max(1, min(int(limit or 100), 500))
    offset = max(0, int(offset or 0))
    out = {"tasks": [], "total": 0, "limit": limit, "offset": offset}
    try:
        db = database.SessionLocal()
        try:
            U = models.UsageEvent
            base = db.query(U).filter(U.kind == "llm")
            if date_from is not None:
                base = base.filter(U.created_at >= date_from)
            if date_to is not None:
                base = base.filter(U.created_at <= date_to)
            out["total"] = _int(base.with_entities(func.count(U.id)).scalar())
            rows = (
                base.order_by(U.created_at.desc())
                .offset(offset).limit(limit).all()
            )
            out["tasks"] = [
                {
                    "id": r.id,
                    "created_at": r.created_at.timestamp() if r.created_at else 0,
                    "username": r.username or (f"user{r.user_id}" if r.user_id else "—"),
                    "endpoint": r.endpoint or "—",
                    "provider": r.provider or "—",
                    "model": r.model or "—",
                    "prompt_tokens": _int(r.prompt_tokens),
                    "completion_tokens": _int(r.completion_tokens),
                    "total_tokens": _int(r.total_tokens),
                    "token_limit": r.token_limit,
                }
                for r in rows
            ]
        finally:
            db.close()
    except Exception:
        pass
    return out


# ── Response-quality / accuracy aggregation ─────────────────────────────────────
#
# Two accuracy signals already captured per assistant message in chat_messages:
#   • rlaif_rating    — automated faithfulness/correctness audit against the
#                       retrieved RAG context (+1 pass, -1 flagged as
#                       hallucination/incorrect). Proxy for RAG-grounded accuracy.
#   • feedback_rating — human thumbs up/down (+1/-1). Perceived answer quality.

def _rate(positive, total):
    return round(100.0 * positive / total, 1) if total else None


def get_quality(recent_hours: int = 24):
    empty_rl = {"total": 0, "passed": 0, "failed": 0, "pass_rate": None}
    empty_fb = {"total": 0, "positive": 0, "negative": 0, "positive_rate": None}
    out = {
        "rlaif": dict(empty_rl), "feedback": dict(empty_fb),
        "rlaif_recent": dict(empty_rl), "feedback_recent": dict(empty_fb),
        "recent_window_hours": recent_hours, "flags": [],
    }
    try:
        db = database.SessionLocal()
        try:
            M = models.ChatMessage

            def cnt(cond):
                return func.coalesce(func.sum(case((cond, 1), else_=0)), 0)

            def agg(since=None):
                q = db.query(
                    cnt(M.rlaif_rating.isnot(None)), cnt(M.rlaif_rating == 1),
                    cnt(M.rlaif_rating == -1), cnt(M.feedback_rating.isnot(None)),
                    cnt(M.feedback_rating == 1), cnt(M.feedback_rating == -1),
                )
                if since is not None:
                    q = q.filter(M.created_at >= since)
                rl_t, rl_p, rl_f, fb_t, fb_p, fb_n = (_int(v) for v in q.one())
                return (
                    {"total": rl_t, "passed": rl_p, "failed": rl_f, "pass_rate": _rate(rl_p, rl_t)},
                    {"total": fb_t, "positive": fb_p, "negative": fb_n, "positive_rate": _rate(fb_p, fb_t)},
                )

            out["rlaif"], out["feedback"] = agg()
            since = datetime.now(timezone.utc) - timedelta(hours=recent_hours)
            out["rlaif_recent"], out["feedback_recent"] = agg(since)

            flags = (
                db.query(M.id, M.rlaif_critique, M.created_at)
                .filter(M.rlaif_rating == -1, M.rlaif_critique.isnot(None))
                .order_by(M.created_at.desc()).limit(5).all()
            )
            out["flags"] = [
                {
                    "message_id": f.id,
                    "critique": (f.rlaif_critique or "")[:240],
                    "created_at": f.created_at.timestamp() if f.created_at else 0,
                }
                for f in flags
            ]
        finally:
            db.close()
    except Exception:
        pass
    return out


def get_rag():
    """RAG retrieval + grounded-generation metrics for the monitoring panel.

    Retrieval: how often a query found confident KB context (hit) vs. abstained
    (miss), cache efficiency, average relevance/chunks, retrieval latency.
    Generation: of substantive chat turns, how many were grounded in KB context.
    """
    out = {
        "queries": 0, "hits": 0, "misses": 0, "hit_rate": None,
        "cache_hits": 0, "cache_hit_rate": None, "web_fallbacks": 0,
        "avg_chunks": None, "avg_relevance": None, "avg_retrieval_ms": None,
        "generations": 0, "grounded": 0, "grounded_rate": None, "avg_generation_ms": None,
    }
    try:
        r = _r()
        q, hit = _int(r.get(K_RAG_Q)), _int(r.get(K_RAG_HIT))
        out["queries"] = q
        out["hits"] = hit
        out["misses"] = max(0, q - hit)
        out["hit_rate"] = _rate(hit, q)
        out["cache_hits"] = _int(r.get(K_RAG_CACHE))
        out["cache_hit_rate"] = _rate(out["cache_hits"], q)
        out["web_fallbacks"] = _int(r.get(K_RAG_WEB))
        out["avg_chunks"] = round(_int(r.get(K_RAG_CHUNKS)) / hit, 1) if hit else None
        sc_cnt = _int(r.get(K_RAG_SCORE_CNT))
        out["avg_relevance"] = round(_flt(r.get(K_RAG_SCORE_SUM)) / sc_cnt, 2) if sc_cnt else None
        lat_cnt = _int(r.get(K_RAG_LAT_CNT))
        out["avg_retrieval_ms"] = round(_flt(r.get(K_RAG_LAT_SUM)) / lat_cnt) if lat_cnt else None
        gen, grounded = _int(r.get(K_GEN_CNT)), _int(r.get(K_GEN_GROUNDED))
        out["generations"] = gen
        out["grounded"] = grounded
        out["grounded_rate"] = _rate(grounded, gen)
        out["avg_generation_ms"] = round(_flt(r.get(K_GEN_LAT_SUM)) / gen) if gen else None
    except Exception:
        pass
    return out


# ── Interaction audit trail (full per-turn log) ─────────────────────────────────

def _interaction_row(r, truncate=True):
    """Serialize an InteractionLog row. ``truncate`` clips long text for the list
    view; the detail view passes False to return the full query/response/context."""
    def clip(s, n):
        s = s or ""
        return (s[:n] + "…") if (truncate and len(s) > n) else s
    return {
        "id": r.id,
        "created_at": r.created_at.timestamp() if r.created_at else 0,
        "session_id": r.session_id,
        "message_id": r.message_id,
        "username": r.username or (f"user{r.user_id}" if r.user_id else "—"),
        "query": clip(r.query, 160),
        "response": clip(r.response, 200),
        "rag_context": (None if truncate else r.rag_context),
        "rag_chunks": _int(r.rag_chunks),
        "grounded": bool(r.grounded),
        "provider": r.provider or "—",
        "model": r.model or "—",
        "prompt_key": r.prompt_key,
        "prompt_version": r.prompt_version,
        "prompt_tokens": _int(r.prompt_tokens),
        "completion_tokens": _int(r.completion_tokens),
        "total_tokens": _int(r.total_tokens),
        "latency_ms": r.latency_ms,
        "feedback_rating": r.feedback_rating,
    }


def get_interactions(limit=50, offset=0, date_from=None, date_to=None,
                     username=None, grounded=None, rating=None):
    """Paginated, filterable interaction audit trail (most recent first)."""
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))
    out = {"interactions": [], "total": 0, "limit": limit, "offset": offset}
    try:
        db = database.SessionLocal()
        try:
            I = models.InteractionLog
            base = db.query(I)
            if date_from is not None:
                base = base.filter(I.created_at >= date_from)
            if date_to is not None:
                base = base.filter(I.created_at <= date_to)
            if username:
                base = base.filter(I.username.ilike(f"%{username}%"))
            if grounded is not None:
                base = base.filter(I.grounded == bool(grounded))
            if rating is not None:
                base = base.filter(I.feedback_rating == int(rating))
            out["total"] = _int(base.with_entities(func.count(I.id)).scalar())
            rows = base.order_by(I.created_at.desc()).offset(offset).limit(limit).all()
            out["interactions"] = [_interaction_row(r, truncate=True) for r in rows]
        finally:
            db.close()
    except Exception:
        pass
    return out


def get_interaction(interaction_id):
    """Full detail for a single interaction (untruncated), or None if missing."""
    try:
        db = database.SessionLocal()
        try:
            r = db.query(models.InteractionLog).filter(
                models.InteractionLog.id == interaction_id
            ).first()
            return _interaction_row(r, truncate=False) if r else None
        finally:
            db.close()
    except Exception:
        return None
