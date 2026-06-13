"""
Admin API — central management of users, SSH servers, Oracle environments and
LLM provider credentials. Every route requires an administrator (get_current_admin).

Secrets (SSH/DB passwords, LLM API keys) are encrypted at rest (crypto.encrypt)
and are NEVER returned in responses — only a has_password / has_api_key flag.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional

from app import database, schemas, models, utils, crypto, llm_service, cred_test, llm_guard_service
from app.routers.auth import get_current_admin, require_approver

_ROLES = ("admin", "dba", "user")


def _norm_role(role):
    r = (role or "user").lower()
    return r if r in _ROLES else "user"

router = APIRouter(prefix="/admin", tags=["Admin"])


# ── Serializers (inject has_* flags the ORM rows don't carry) ──────────────────

def _server_out(s: models.SshServer) -> dict:
    return {
        "id": s.id, "name": s.name, "hostname": s.hostname, "port": s.port,
        "username": s.username, "server_type": s.server_type,
        "app_services": s.app_services, "description": s.description,
        "is_active": s.is_active, "created_at": s.created_at,
        "has_password": bool(s.password_enc),
    }


def _env_out(e: models.EbsEnvironment) -> dict:
    return {
        "id": e.id, "name": e.name, "tier": e.tier, "db_host": e.db_host, "db_port": e.db_port,
        "db_sid": e.db_sid, "db_user": e.db_user, "description": e.description,
        "system_user": e.system_user, "weblogic_user": e.weblogic_user, "apps_os_user": e.apps_os_user,
        "ssh_server_id": e.ssh_server_id, "is_active": e.is_active,
        "db_id": e.db_id, "global_name": e.global_name,
        "created_at": e.created_at, "has_password": bool(e.db_password_enc),
        "has_system_password": bool(e.system_password_enc),
        "has_weblogic_password": bool(e.weblogic_password_enc),
    }


def _llm_out(c: models.LlmCredential) -> dict:
    return {
        "id": c.id, "provider": c.provider, "label": c.label, "model": c.model,
        "base_url": c.base_url, "is_default": c.is_default, "is_active": c.is_active,
        "created_at": c.created_at, "has_api_key": bool(c.api_key_enc),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Users
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/users", response_model=List[schemas.AdminUserOut])
def list_users(db: Session = Depends(database.get_db),
               _: models.User = Depends(require_approver)):
    # Admin or DBA may view users (DBA needs this to approve sign-ups).
    return db.query(models.User).order_by(models.User.created_at.asc()).all()


@router.post("/users", response_model=schemas.AdminUserOut, status_code=status.HTTP_201_CREATED)
def create_user(payload: schemas.AdminUserCreate,
                db: Session = Depends(database.get_db),
                _: models.User = Depends(get_current_admin)):
    existing = db.query(models.User).filter(
        (models.User.email == payload.email) | (models.User.username == payload.username)
    ).first()
    if existing:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Username or email already registered")
    role = _norm_role(payload.role if payload.role else ("admin" if payload.is_admin else "user"))
    user = models.User(
        username=payload.username,
        email=payload.email,
        password_hash=utils.hash_password(payload.password),
        role=role,
        is_admin=(role == "admin"),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.patch("/users/{user_id}", response_model=schemas.AdminUserOut)
def update_user(user_id: int, payload: schemas.AdminUserUpdate,
                db: Session = Depends(database.get_db),
                admin: models.User = Depends(get_current_admin)):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if payload.email is not None:
        user.email = payload.email
    if payload.password:
        user.password_hash = utils.hash_password(payload.password)
    if payload.is_active is not None:
        user.is_active = payload.is_active

    new_role = payload.role
    if new_role is None and payload.is_admin is not None:
        new_role = "admin" if payload.is_admin else None  # legacy toggle
    if new_role is not None:
        new_role = _norm_role(new_role)
        # Prevent an admin from demoting themselves (avoid lockout).
        if user.id == admin.id and new_role != "admin":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="You cannot change your own admin role")
        user.role = new_role
        user.is_admin = (new_role == "admin")

    db.commit()
    db.refresh(user)
    return user


@router.post("/users/{user_id}/approve", response_model=schemas.AdminUserOut)
def approve_user(user_id: int,
                 db: Session = Depends(database.get_db),
                 approver: models.User = Depends(require_approver)):
    """Approve a pending self sign-up — activates the account. Admin/DBA; not your own request."""
    from app import audit_service
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if user.id == approver.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="You cannot approve your own request.")
    user.approval_status = "approved"
    user.is_active = True
    db.commit()
    db.refresh(user)
    audit_service.log("user_approved", user_id=user.id, username=user.username,
                      detail={"by": approver.username})
    return user


@router.post("/users/{user_id}/reject", response_model=schemas.AdminUserOut)
def reject_user(user_id: int,
                db: Session = Depends(database.get_db),
                approver: models.User = Depends(require_approver)):
    """Reject a pending self sign-up — keeps the record (inactive) for audit. Admin/DBA."""
    from app import audit_service
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if user.id == approver.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="You cannot reject your own request.")
    user.approval_status = "rejected"
    user.is_active = False
    db.commit()
    db.refresh(user)
    audit_service.log("user_rejected", user_id=user.id, username=user.username,
                      detail={"by": approver.username})
    return user


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(user_id: int,
                db: Session = Depends(database.get_db),
                admin: models.User = Depends(get_current_admin)):
    if user_id == admin.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="You cannot delete your own account")
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    db.delete(user)
    db.commit()


# ══════════════════════════════════════════════════════════════════════════════
# SSH servers
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/servers", response_model=List[schemas.SshServerOut])
def list_servers(db: Session = Depends(database.get_db),
                 _: models.User = Depends(get_current_admin)):
    rows = db.query(models.SshServer).order_by(models.SshServer.name.asc()).all()
    return [_server_out(s) for s in rows]


@router.post("/servers", response_model=schemas.SshServerOut, status_code=status.HTTP_201_CREATED)
def create_server(payload: schemas.SshServerCreate,
                  db: Session = Depends(database.get_db),
                  admin: models.User = Depends(get_current_admin)):
    if db.query(models.SshServer).filter(models.SshServer.name == payload.name).first():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="A server with this name already exists")
    s = models.SshServer(
        name=payload.name, hostname=payload.hostname, port=payload.port,
        username=payload.username, password_enc=crypto.encrypt(payload.password),
        server_type=payload.server_type, app_services=payload.app_services,
        description=payload.description, is_active=payload.is_active,
        created_by=admin.id,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return _server_out(s)


@router.patch("/servers/{server_id}", response_model=schemas.SshServerOut)
def update_server(server_id: int, payload: schemas.SshServerUpdate,
                  db: Session = Depends(database.get_db),
                  _: models.User = Depends(get_current_admin)):
    s = db.query(models.SshServer).filter(models.SshServer.id == server_id).first()
    if not s:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Server not found")
    data = payload.model_dump(exclude_unset=True)
    if "password" in data:
        pw = data.pop("password")
        # Empty string clears the password; None/omitted keeps existing.
        if pw is not None:
            s.password_enc = crypto.encrypt(pw) if pw else None
    for key, val in data.items():
        setattr(s, key, val)
    db.commit()
    db.refresh(s)
    return _server_out(s)


@router.delete("/servers/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_server(server_id: int,
                  db: Session = Depends(database.get_db),
                  _: models.User = Depends(get_current_admin)):
    s = db.query(models.SshServer).filter(models.SshServer.id == server_id).first()
    if not s:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Server not found")
    db.delete(s)
    db.commit()


@router.post("/servers/{server_id}/test")
def test_server(server_id: int,
                db: Session = Depends(database.get_db),
                _: models.User = Depends(get_current_admin)):
    s = db.query(models.SshServer).filter(models.SshServer.id == server_id).first()
    if not s:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Server not found")
    try:
        import paramiko
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=s.hostname, port=s.port or 22, username=s.username,
            password=crypto.decrypt(s.password_enc), timeout=10,
        )
        client.close()
        return {"ok": True, "message": f"Connected to {s.hostname}:{s.port} successfully."}
    except Exception as exc:
        return {"ok": False, "message": f"Connection failed: {exc}"}


# ══════════════════════════════════════════════════════════════════════════════
# Environments
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/environments", response_model=List[schemas.EnvironmentOut])
def list_environments(db: Session = Depends(database.get_db),
                      _: models.User = Depends(get_current_admin)):
    rows = db.query(models.EbsEnvironment).order_by(models.EbsEnvironment.name.asc()).all()
    return [_env_out(e) for e in rows]


@router.post("/environments", response_model=schemas.EnvironmentOut, status_code=status.HTTP_201_CREATED)
def create_environment(payload: schemas.EnvironmentCreate,
                       db: Session = Depends(database.get_db),
                       admin: models.User = Depends(get_current_admin)):
    name = payload.name.strip().upper()
    if db.query(models.EbsEnvironment).filter(models.EbsEnvironment.name == name).first():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="An environment with this name already exists")
    e = models.EbsEnvironment(
        name=name, tier=(payload.tier or "nonprod"),
        db_host=payload.db_host, db_port=payload.db_port,
        db_sid=payload.db_sid, db_user=payload.db_user,
        db_password_enc=crypto.encrypt(payload.db_password),
        system_user=payload.system_user, system_password_enc=crypto.encrypt(payload.system_password),
        weblogic_user=payload.weblogic_user, weblogic_password_enc=crypto.encrypt(payload.weblogic_password),
        apps_os_user=payload.apps_os_user,
        description=payload.description, ssh_server_id=payload.ssh_server_id,
        is_active=payload.is_active, created_by=admin.id,
    )
    db.add(e)
    db.commit()
    db.refresh(e)
    return _env_out(e)


@router.patch("/environments/{env_id}", response_model=schemas.EnvironmentOut)
def update_environment(env_id: int, payload: schemas.EnvironmentUpdate,
                       db: Session = Depends(database.get_db),
                       _: models.User = Depends(get_current_admin)):
    e = db.query(models.EbsEnvironment).filter(models.EbsEnvironment.id == env_id).first()
    if not e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Environment not found")
    data = payload.model_dump(exclude_unset=True)
    # Write-only secrets: set to (re)encrypt, empty string to clear, omit to keep.
    for field, col in (("db_password", "db_password_enc"),
                       ("system_password", "system_password_enc"),
                       ("weblogic_password", "weblogic_password_enc")):
        if field in data:
            pw = data.pop(field)
            if pw is not None:
                setattr(e, col, crypto.encrypt(pw) if pw else None)
    if "name" in data and data["name"]:
        data["name"] = data["name"].strip().upper()
    for key, val in data.items():
        setattr(e, key, val)
    db.commit()
    db.refresh(e)
    return _env_out(e)


@router.delete("/environments/{env_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_environment(env_id: int,
                       db: Session = Depends(database.get_db),
                       _: models.User = Depends(get_current_admin)):
    e = db.query(models.EbsEnvironment).filter(models.EbsEnvironment.id == env_id).first()
    if not e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Environment not found")
    db.delete(e)
    db.commit()


@router.post("/environments/{env_id}/test")
def test_environment(env_id: int,
                     db: Session = Depends(database.get_db),
                     _: models.User = Depends(get_current_admin)):
    e = db.query(models.EbsEnvironment).filter(models.EbsEnvironment.id == env_id).first()
    if not e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Environment not found")
    if not (e.db_host and e.db_sid and e.db_user):
        return {"ok": False, "message": "db_host, db_sid and db_user are required to test."}
    # Connect and capture the intrinsic identity (DBID / global_name) for the prod guard.
    from app import prod_guard
    ident = prod_guard.capture_identity(e)
    if ident.get("reachable"):
        e.db_id = ident.get("db_id") or e.db_id
        e.global_name = ident.get("global_name") or e.global_name
        db.commit()
        return {"ok": True, "message": (f"Connected to {e.db_host}:{e.db_port}/{e.db_sid}. "
                                        f"Captured DBID {e.db_id}, global_name {e.global_name}.")}
    return {"ok": False, "message": f"Connection failed: {ident.get('error', 'unreachable')}"}


# ══════════════════════════════════════════════════════════════════════════════
# LLM credentials
# ══════════════════════════════════════════════════════════════════════════════

def _clear_other_defaults(db: Session, provider: str, keep_id: int | None):
    db.query(models.LlmCredential).filter(
        models.LlmCredential.provider == provider,
        models.LlmCredential.is_default == True,  # noqa: E712
        models.LlmCredential.id != (keep_id or -1),
    ).update({models.LlmCredential.is_default: False})


@router.get("/llm-credentials", response_model=List[schemas.LlmCredentialOut])
def list_llm_credentials(db: Session = Depends(database.get_db),
                         _: models.User = Depends(get_current_admin)):
    rows = db.query(models.LlmCredential).order_by(models.LlmCredential.provider.asc()).all()
    return [_llm_out(c) for c in rows]


@router.post("/llm-credentials", response_model=schemas.LlmCredentialOut, status_code=status.HTTP_201_CREATED)
def create_llm_credential(payload: schemas.LlmCredentialCreate,
                          db: Session = Depends(database.get_db),
                          admin: models.User = Depends(get_current_admin)):
    c = models.LlmCredential(
        provider=payload.provider, label=payload.label, model=payload.model,
        api_key_enc=crypto.encrypt(payload.api_key), base_url=payload.base_url,
        is_default=payload.is_default, is_active=payload.is_active, created_by=admin.id,
    )
    db.add(c)
    db.flush()
    if payload.is_default:
        _clear_other_defaults(db, c.provider, c.id)
    db.commit()
    db.refresh(c)
    return _llm_out(c)


@router.patch("/llm-credentials/{cred_id}", response_model=schemas.LlmCredentialOut)
def update_llm_credential(cred_id: int, payload: schemas.LlmCredentialUpdate,
                          db: Session = Depends(database.get_db),
                          _: models.User = Depends(get_current_admin)):
    c = db.query(models.LlmCredential).filter(models.LlmCredential.id == cred_id).first()
    if not c:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Credential not found")
    data = payload.model_dump(exclude_unset=True)
    if "api_key" in data:
        key = data.pop("api_key")
        if key is not None:
            c.api_key_enc = crypto.encrypt(key) if key else None
    for field, val in data.items():
        setattr(c, field, val)
    if data.get("is_default"):
        _clear_other_defaults(db, c.provider, c.id)
    db.commit()
    db.refresh(c)
    return _llm_out(c)


@router.delete("/llm-credentials/{cred_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_llm_credential(cred_id: int,
                          db: Session = Depends(database.get_db),
                          _: models.User = Depends(get_current_admin)):
    c = db.query(models.LlmCredential).filter(models.LlmCredential.id == cred_id).first()
    if not c:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Credential not found")
    db.delete(c)
    db.commit()


# ══════════════════════════════════════════════════════════════════════════════
# SSO settings (single row)
# ══════════════════════════════════════════════════════════════════════════════

def _sso_out(s: models.SsoSettings) -> dict:
    return {
        "provider": s.provider, "enabled": s.enabled, "signup_enabled": bool(s.signup_enabled),
        "tenant_id": s.tenant_id, "client_id": s.client_id, "redirect_uri": s.redirect_uri,
        "auto_provision": s.auto_provision, "has_client_secret": bool(s.client_secret_enc),
    }


@router.get("/sso", response_model=schemas.SsoSettingsOut)
def get_sso(db: Session = Depends(database.get_db), _: models.User = Depends(get_current_admin)):
    from app.routers.sso import get_or_create_settings
    return _sso_out(get_or_create_settings(db))


@router.put("/sso", response_model=schemas.SsoSettingsOut)
def update_sso(payload: schemas.SsoSettingsUpdate,
               db: Session = Depends(database.get_db),
               _: models.User = Depends(get_current_admin)):
    from app.routers.sso import get_or_create_settings
    s = get_or_create_settings(db)
    data = payload.model_dump(exclude_unset=True)
    if "client_secret" in data:
        secret = data.pop("client_secret")
        if secret is not None:
            s.client_secret_enc = crypto.encrypt(secret) if secret else None
    for field, val in data.items():
        setattr(s, field, val)
    db.commit()
    db.refresh(s)
    return _sso_out(s)


@router.post("/llm-credentials/{cred_id}/test")
def test_llm_credential(cred_id: int,
                        db: Session = Depends(database.get_db),
                        _: models.User = Depends(get_current_admin)):
    c = db.query(models.LlmCredential).filter(models.LlmCredential.id == cred_id).first()
    if not c:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Credential not found")
    try:
        reply = llm_service.complete_sync(
            messages=[{"role": "user", "content": "Reply with the single word: OK"}],
            provider=c.provider,
            model=c.model or None,
            api_key=crypto.decrypt(c.api_key_enc),
            base_url=c.base_url or None,
            use_cache=False,  # connectivity test must hit the provider, never a cache
        )
        return {"ok": True, "message": f"Provider responded: {(reply or '').strip()[:80]}"}
    except Exception as exc:
        return {"ok": False, "message": f"Provider call failed: {exc}"}


# ══════════════════════════════════════════════════════════════════════════════
# Integration credentials (Git / Confluence)
# ══════════════════════════════════════════════════════════════════════════════

def _integration_out(c: models.IntegrationCredential) -> dict:
    return {
        "id": c.id, "kind": c.kind, "name": c.name, "host": c.host,
        "base_url": c.base_url, "username": c.username,
        "is_default": c.is_default, "is_active": c.is_active,
        "created_at": c.created_at, "has_secret": bool(c.secret_enc),
    }


def _clear_other_integration_defaults(db: Session, kind: str, keep_id):
    db.query(models.IntegrationCredential).filter(
        models.IntegrationCredential.kind == kind,
        models.IntegrationCredential.is_default == True,  # noqa: E712
        models.IntegrationCredential.id != (keep_id or -1),
    ).update({models.IntegrationCredential.is_default: False})


@router.get("/integrations", response_model=List[schemas.IntegrationOut])
def list_integrations(kind: Optional[str] = None,
                      db: Session = Depends(database.get_db),
                      _: models.User = Depends(get_current_admin)):
    q = db.query(models.IntegrationCredential)
    if kind:
        q = q.filter(models.IntegrationCredential.kind == kind)
    rows = q.order_by(models.IntegrationCredential.kind.asc(),
                      models.IntegrationCredential.name.asc()).all()
    return [_integration_out(c) for c in rows]


@router.post("/integrations", response_model=schemas.IntegrationOut, status_code=status.HTTP_201_CREATED)
def create_integration(payload: schemas.IntegrationCreate,
                       db: Session = Depends(database.get_db),
                       admin: models.User = Depends(get_current_admin)):
    if payload.kind not in ("git", "confluence"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="kind must be 'git' or 'confluence'")
    c = models.IntegrationCredential(
        kind=payload.kind, name=payload.name, host=payload.host, base_url=payload.base_url,
        username=payload.username, secret_enc=crypto.encrypt(payload.secret),
        is_default=payload.is_default, is_active=payload.is_active, created_by=admin.id,
    )
    db.add(c)
    db.flush()
    if payload.is_default:
        _clear_other_integration_defaults(db, c.kind, c.id)
    db.commit()
    db.refresh(c)
    return _integration_out(c)


@router.patch("/integrations/{cred_id}", response_model=schemas.IntegrationOut)
def update_integration(cred_id: int, payload: schemas.IntegrationUpdate,
                       db: Session = Depends(database.get_db),
                       _: models.User = Depends(get_current_admin)):
    c = db.query(models.IntegrationCredential).filter(models.IntegrationCredential.id == cred_id).first()
    if not c:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Integration not found")
    data = payload.model_dump(exclude_unset=True)
    if "secret" in data:
        secret = data.pop("secret")
        if secret is not None:
            c.secret_enc = crypto.encrypt(secret) if secret else None
    for field, val in data.items():
        setattr(c, field, val)
    if data.get("is_default"):
        _clear_other_integration_defaults(db, c.kind, c.id)
    db.commit()
    db.refresh(c)
    return _integration_out(c)


@router.delete("/integrations/{cred_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_integration(cred_id: int,
                       db: Session = Depends(database.get_db),
                       _: models.User = Depends(get_current_admin)):
    c = db.query(models.IntegrationCredential).filter(models.IntegrationCredential.id == cred_id).first()
    if not c:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Integration not found")
    db.delete(c)
    db.commit()


@router.post("/integrations/{cred_id}/test")
def test_integration(cred_id: int,
                     db: Session = Depends(database.get_db),
                     _: models.User = Depends(get_current_admin)):
    c = db.query(models.IntegrationCredential).filter(models.IntegrationCredential.id == cred_id).first()
    if not c:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Integration not found")
    token = crypto.decrypt(c.secret_enc)
    if c.kind == "git":
        return cred_test.test_git(c.host, c.username, token)
    if c.kind == "confluence":
        return cred_test.test_confluence(c.host, c.username, token)
    return {"ok": False, "message": f"Unknown integration kind '{c.kind}'."}


@router.post("/sso/test")
def test_sso(db: Session = Depends(database.get_db),
             _: models.User = Depends(get_current_admin)):
    """Validate the saved Entra ID settings (tenant discovery + app credentials)."""
    from app.routers.sso import get_or_create_settings
    s = get_or_create_settings(db)
    return cred_test.test_sso(s.tenant_id, s.client_id, crypto.decrypt(s.client_secret_enc))


# ══════════════════════════════════════════════════════════════════════════════
# LLM Guard status
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/llm-guard/status")
def llm_guard_status(_: models.User = Depends(get_current_admin)):
    """Return the current LLM Guard configuration (enabled state, active scanners)."""
    return llm_guard_service.status()
