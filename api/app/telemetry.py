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

from app import database, models

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
               prompt_tokens, completion_tokens, context_chars):
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
            ))
            db.commit()
        finally:
            db.close()
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
    # Durable 24h token total (survives Redis restarts)
    try:
        db = database.SessionLocal()
        try:
            since = datetime.now(timezone.utc) - timedelta(hours=24)
            live["tokens_24h"] = _int(
                db.query(func.coalesce(func.sum(models.UsageEvent.total_tokens), 0))
                .filter(models.UsageEvent.kind == "llm", models.UsageEvent.created_at >= since)
                .scalar()
            )
        finally:
            db.close()
    except Exception:
        pass
    return live


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
