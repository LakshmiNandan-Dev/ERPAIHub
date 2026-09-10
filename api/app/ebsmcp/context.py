"""The seam between OraEBSAgent's authentication and EBSMCP's identity.

EBSMCP's whole governance chain (resolve identity -> entitlement -> audit)
keys off a single subject string; how that subject is authenticated is not
EBSMCP's concern. In the standalone server it comes from a validated Entra
bearer token. Embedded here as a library, OraEBSAgent has already
authenticated the caller (local session / SSO) — so it simply asserts the
subject through this contextvar, per request, and no token machinery runs.

An OraEBSAgent dependency sets this from get_current_user (see the design
note, section 3):

    from app.ebsmcp.context import set_current_subject
    def bind_ebs_subject(user = Depends(get_current_user)):
        set_current_subject(user.email)

current_subject() reads it first, before any MCP token or dev fallback.
"""

from __future__ import annotations

from contextvars import ContextVar

_current_subject: ContextVar[str | None] = ContextVar("ebs_subject", default=None)


def set_current_subject(subject: str) -> None:
    _current_subject.set(subject)


def get_injected_subject() -> str | None:
    return _current_subject.get()
