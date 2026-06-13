"""
Audit logging — durable, append-only records of authentication events and agent
invocations, including the origin (IP address + user-agent / machine).

All writes are wrapped so auditing can never break a request.
"""
from app.core import database
from app import models


def platform_of(user_agent: str) -> str:
    """A short, friendly machine/OS label derived from the User-Agent."""
    ua = user_agent or ""
    for needle, label in [
        ("Windows NT", "Windows"), ("Mac OS X", "macOS"), ("iPhone", "iPhone"),
        ("iPad", "iPad"), ("Android", "Android"), ("CrOS", "ChromeOS"), ("Linux", "Linux"),
    ]:
        if needle in ua:
            return label
    return "Unknown"


def log(event_type, user_id=None, username=None, agent=None,
        ip=None, user_agent=None, method=None, path=None, detail=None):
    try:
        db = database.SessionLocal()
        try:
            db.add(models.AuditLog(
                event_type=event_type, user_id=user_id, username=username, agent=agent,
                ip_address=ip, user_agent=(user_agent or None), method=method, path=path, detail=detail,
            ))
            db.commit()
        finally:
            db.close()
    except Exception:
        pass


# ── Agent endpoint mapping (method + path → agent name) ─────────────────────────

def agent_for(method: str, path: str):
    if method != "POST":
        return None
    if path.startswith("/chat/sessions/") and path.endswith("/stream"):
        return "chat"
    if path == "/deployments/" or path == "/deployments/agent/chat":
        return "deployment"
    if path == "/performance/analyze" or path.startswith("/performance/awr"):
        return "performance"
    if path == "/cloning/" or path == "/cloning/agent":
        return "cloning"
    if path.startswith("/rag/") and path not in ("/rag/",):
        return "knowledge_base"
    return None


def client_ip(headers: dict, scope_client) -> str | None:
    """headers: dict of bytes->bytes (ASGI). Prefer X-Forwarded-For when present."""
    try:
        xff = headers.get(b"x-forwarded-for") or headers.get(b"x-real-ip")
        if xff:
            return xff.decode().split(",")[0].strip()
    except Exception:
        pass
    if scope_client:
        return scope_client[0]
    return None


def user_agent_of(headers: dict) -> str | None:
    try:
        ua = headers.get(b"user-agent")
        return ua.decode() if ua else None
    except Exception:
        return None
