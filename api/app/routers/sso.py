"""
Microsoft Entra ID (Azure AD) OIDC SSO — authentication only, with JIT
auto-provisioning. Implemented over httpx (no extra deps); the Authorization
Code + PKCE flow receives the ID token directly from the token endpoint over
TLS (back channel), so the token is trusted without separate JWKS verification.

Configuration lives in the sso_settings table (admin-managed); the client secret
is stored encrypted at rest.
"""
import os
import re
import json
import time
import base64
import hashlib
import secrets
import urllib.parse

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import database, models, schemas, crypto, utils

router = APIRouter(prefix="/auth/sso", tags=["SSO"])

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")

# Short-lived store for PKCE verifiers keyed by the OAuth `state` value.
_PENDING: dict[str, dict] = {}
_PENDING_TTL = 600  # seconds


def get_or_create_settings(db: Session) -> models.SsoSettings:
    row = db.query(models.SsoSettings).filter(models.SsoSettings.id == 1).first()
    if not row:
        row = models.SsoSettings(id=1)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _prune_pending():
    now = time.time()
    for s in [k for k, v in _PENDING.items() if now - v["ts"] > _PENDING_TTL]:
        _PENDING.pop(s, None)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _decode_jwt_claims(id_token: str) -> dict:
    """Decode (not verify) the JWT payload — safe for back-channel auth-code tokens."""
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _front_redirect(**params) -> RedirectResponse:
    qs = urllib.parse.urlencode(params)
    return RedirectResponse(url=f"{FRONTEND_URL}/?{qs}", status_code=302)


def _unique_username(db: Session, base: str) -> str:
    base = re.sub(r"[^a-zA-Z0-9_.-]", "", (base or "user").split("@")[0]) or "user"
    candidate, i = base, 1
    while db.query(models.User.id).filter(models.User.username == candidate).first():
        candidate = f"{base}{i}"
        i += 1
    return candidate


# ── Public endpoints ───────────────────────────────────────────────────────────

@router.get("/status", response_model=schemas.SsoStatusOut)
def sso_status(db: Session = Depends(database.get_db)):
    s = get_or_create_settings(db)
    # Only advertise as enabled when fully configured.
    ready = bool(s.enabled and s.tenant_id and s.client_id and s.client_secret_enc and s.redirect_uri)
    return schemas.SsoStatusOut(enabled=ready, provider=s.provider, signup_enabled=bool(s.signup_enabled))


@router.get("/login")
def sso_login(db: Session = Depends(database.get_db)):
    s = get_or_create_settings(db)
    if not (s.enabled and s.tenant_id and s.client_id and s.redirect_uri):
        return _front_redirect(sso_error="SSO is not fully configured.")

    _prune_pending()
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    _PENDING[state] = {"verifier": verifier, "ts": time.time()}

    params = {
        "client_id": s.client_id,
        "response_type": "code",
        "redirect_uri": s.redirect_uri,
        "response_mode": "query",
        "scope": "openid email profile",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    authorize = (
        f"https://login.microsoftonline.com/{s.tenant_id}/oauth2/v2.0/authorize?"
        + urllib.parse.urlencode(params)
    )
    return RedirectResponse(url=authorize, status_code=302)


@router.get("/callback")
def sso_callback(request: Request, db: Session = Depends(database.get_db)):
    params = request.query_params
    if params.get("error"):
        return _front_redirect(sso_error=params.get("error_description") or params.get("error"))

    code = params.get("code")
    state = params.get("state")
    pending = _PENDING.pop(state, None) if state else None
    if not code or not pending:
        return _front_redirect(sso_error="Invalid or expired SSO state. Please try again.")

    s = get_or_create_settings(db)
    client_secret = crypto.decrypt(s.client_secret_enc) or ""

    token_url = f"https://login.microsoftonline.com/{s.tenant_id}/oauth2/v2.0/token"
    data = {
        "client_id": s.client_id,
        "client_secret": client_secret,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": s.redirect_uri,
        "code_verifier": pending["verifier"],
        "scope": "openid email profile",
    }
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(token_url, data=data)
        if resp.status_code >= 400:
            detail = ""
            try:
                detail = resp.json().get("error_description", "")
            except Exception:
                detail = resp.text[:200]
            return _front_redirect(sso_error=f"Token exchange failed: {detail or resp.status_code}")
        tokens = resp.json()
    except Exception as exc:
        return _front_redirect(sso_error=f"Could not reach the identity provider: {exc}")

    claims = _decode_jwt_claims(tokens.get("id_token", ""))
    subject = claims.get("oid") or claims.get("sub")
    email = claims.get("email") or claims.get("preferred_username") or claims.get("upn")
    name = claims.get("name")
    if not subject:
        return _front_redirect(sso_error="Identity provider did not return a user identifier.")

    # Find by external_id, then by email; auto-provision if allowed.
    user = db.query(models.User).filter(models.User.external_id == subject).first()
    if not user and email:
        user = db.query(models.User).filter(models.User.email == email).first()
        if user:  # link the existing local account to the IdP identity
            user.external_id = subject
            user.auth_provider = "entra"

    if not user:
        if not s.auto_provision:
            return _front_redirect(sso_error="No account exists for this user and auto-provisioning is disabled.")
        user = models.User(
            username=_unique_username(db, email or name or subject),
            email=email or f"{subject}@sso.local",
            password_hash=None,
            is_admin=False,
            auth_provider="entra",
            external_id=subject,
        )
        db.add(user)

    db.commit()
    db.refresh(user)

    if not user.is_active:
        return _front_redirect(sso_error="This account is disabled.")

    # Issue the standard session token used by the rest of the app.
    session = models.UserSession(
        user_id=user.id,
        session_token=utils.create_session_token(),
        expires_at=utils.get_session_expiration(hours=24),
    )
    db.add(session)
    db.commit()

    return _front_redirect(sso_token=session.session_token)
