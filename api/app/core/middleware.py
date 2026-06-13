"""
Pure-ASGI telemetry middleware.

Implemented at the ASGI layer (not Starlette BaseHTTPMiddleware, which buffers
the response body and would break SSE streaming). It counts response body bytes
as they pass through, captures status + request size, resolves the bearer token
to a user, and records an HTTP telemetry event. The blocking Redis/DB work is
offloaded to a thread so it never delays the response.
"""
import time
import asyncio

from app.core import telemetry
from app.core.audit import audit_service


class TelemetryMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        start = time.time()
        headers = dict(scope.get("headers") or [])

        req_bytes = 0
        try:
            cl = headers.get(b"content-length")
            if cl:
                req_bytes = int(cl.decode())
        except Exception:
            req_bytes = 0

        token = ""
        try:
            auth = headers.get(b"authorization", b"").decode()
            if auth.lower().startswith("bearer "):
                token = auth[7:].strip()
        except Exception:
            token = ""

        ip = audit_service.client_ip(headers, scope.get("client"))
        ua = audit_service.user_agent_of(headers)
        agent = audit_service.agent_for(scope.get("method", ""), scope.get("path", ""))

        state = {"status": 0, "bytes": 0}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
            elif message["type"] == "http.response.body":
                state["bytes"] += len(message.get("body") or b"")
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = int((time.time() - start) * 1000)
            path = scope.get("path", "")
            method = scope.get("method", "")

            def _record():
                uid, uname = telemetry.resolve_user_from_token(token)
                telemetry.record_http(
                    uid, uname, path, method, state["status"],
                    req_bytes, state["bytes"], duration_ms,
                )
                # Durable audit for agent invocations (who invoked which agent, from where).
                if agent and uid and 200 <= state["status"] < 400:
                    audit_service.log("agent_invoke", user_id=uid, username=uname, agent=agent,
                                      ip=ip, user_agent=ua, method=method, path=path)

            try:
                asyncio.get_event_loop().run_in_executor(None, _record)
            except Exception:
                pass
