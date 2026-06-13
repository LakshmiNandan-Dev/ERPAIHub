from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

import os

from app import models, utils
from app.database import engine, get_db, SessionLocal
from app.middleware import TelemetryMiddleware
from app.llm_service import DEFAULT_MODELS, _OLLAMA_BASE
from app.routers import auth, chat, rag, rl, deployments, deployment_agent, performance_agent, admin, config, sso, monitoring, cloning, audit, training, patching

models.Base.metadata.create_all(bind=engine)


# First-run administrator bootstrap. Credentials are configurable via env so
# production can set a strong password without code changes; defaults make the
# app usable out of the box (username 'admin' / password 'admin').
BOOTSTRAP_ADMIN_USERNAME = os.getenv("BOOTSTRAP_ADMIN_USERNAME", "admin")
BOOTSTRAP_ADMIN_EMAIL = os.getenv("BOOTSTRAP_ADMIN_EMAIL", "admin@oraebs.com")
BOOTSTRAP_ADMIN_PASSWORD = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "admin")
# Recovery switch: when truthy, the bootstrap admin's password is reset to the
# configured value on startup (for locked-out deployments). Off by default so a
# password changed via the UI is never silently clobbered.
BOOTSTRAP_ADMIN_RESET = os.getenv("BOOTSTRAP_ADMIN_RESET", "0").lower() in ("1", "true", "yes")


def seed_initial_admin():
    """
    Ensure a usable administrator account exists so the app works out of the box.

    Fresh database (no such user) → create it with BOOTSTRAP_ADMIN_USERNAME /
    _EMAIL / _PASSWORD (defaults: admin / admin), active + approved + role=admin.
    Existing account → guarantee it is an active, approved administrator, but DO
    NOT change its password unless BOOTSTRAP_ADMIN_RESET is set. Change the
    password after first login and set a strong one in production.
    """
    db = SessionLocal()
    try:
        admin = db.query(models.User).filter(
            models.User.username == BOOTSTRAP_ADMIN_USERNAME
        ).first()

        if not admin:
            db.add(models.User(
                username=BOOTSTRAP_ADMIN_USERNAME,
                email=BOOTSTRAP_ADMIN_EMAIL,
                password_hash=utils.hash_password(BOOTSTRAP_ADMIN_PASSWORD),
                is_admin=True,
                is_active=True,
                role="admin",
                approval_status="approved",
            ))
            db.commit()
            print(f"[seed] Default admin created — username='{BOOTSTRAP_ADMIN_USERNAME}', "
                  f"password='{BOOTSTRAP_ADMIN_PASSWORD}'. Change this after first login.")
            return

        changed = False
        # Always keep the bootstrap account able to actually sign in as an admin.
        if not admin.is_admin or getattr(admin, "role", None) != "admin":
            admin.is_admin = True
            admin.role = "admin"
            changed = True
        if not admin.is_active or admin.approval_status != "approved":
            admin.is_active = True
            admin.approval_status = "approved"
            changed = True
        if BOOTSTRAP_ADMIN_RESET:
            admin.password_hash = utils.hash_password(BOOTSTRAP_ADMIN_PASSWORD)
            changed = True
            print(f"[seed] BOOTSTRAP_ADMIN_RESET set — '{BOOTSTRAP_ADMIN_USERNAME}' "
                  f"password reset to the configured value.")
        if changed:
            db.commit()
            print(f"[seed] Ensured '{BOOTSTRAP_ADMIN_USERNAME}' is an active administrator.")
    except Exception as exc:  # never block startup on seeding
        print(f"[seed] Could not seed default admin: {exc}")
    finally:
        db.close()


def seed_default_llm_provider():
    """
    Make the bundled local Ollama show up under Admin Console → LLM Providers
    so the default provider is visible and manageable out of the box. Ollama
    needs no API key; base_url points at the server-side Ollama service so the
    container can reach it. Only seeds when no provider rows exist at all.
    """
    db = SessionLocal()
    try:
        if db.query(models.LlmCredential.id).first() is not None:
            return  # admin has already configured providers — don't touch them
        db.add(models.LlmCredential(
            provider="ollama",
            label="Local Ollama (bundled)",
            model=DEFAULT_MODELS["ollama"],
            base_url=os.getenv("OLLAMA_URL", _OLLAMA_BASE),
            api_key_enc=None,
            is_default=True,
            is_active=True,
        ))
        db.commit()
        print("[seed] Default Ollama provider registered under LLM Providers.")
    except Exception as exc:  # never block startup on seeding
        print(f"[seed] Could not seed default LLM provider: {exc}")
    finally:
        db.close()


seed_initial_admin()
seed_default_llm_provider()

gateway = FastAPI()

gateway.add_middleware(TelemetryMiddleware)

# CORS — allow the React frontend's origin. By default accept any host serving
# the frontend on port 5173 (covers localhost and remote-server IP/hostname
# deployments) without per-host config. Set CORS_ORIGINS (comma-separated) for a
# fixed allow-list, e.g. behind a domain / reverse proxy.
_cors_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
_cors_kwargs = dict(allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
if _cors_origins:
    _cors_kwargs["allow_origins"] = _cors_origins
else:
    _cors_kwargs["allow_origin_regex"] = r"https?://[^/]+:5173"
gateway.add_middleware(CORSMiddleware, **_cors_kwargs)

gateway.include_router(auth.router)
gateway.include_router(chat.router)
gateway.include_router(rag.router)
gateway.include_router(rl.router)
gateway.include_router(deployments.router)
gateway.include_router(deployment_agent.router)
gateway.include_router(performance_agent.router)
gateway.include_router(admin.router)
gateway.include_router(config.router)
gateway.include_router(sso.router)
gateway.include_router(monitoring.router)
gateway.include_router(cloning.router)
gateway.include_router(audit.router)
gateway.include_router(training.router)
gateway.include_router(patching.router)


@gateway.get("/")
async def root():
    return {"message": "API Service Active"}


@gateway.get("/health")
def health():
    return {"message": "Health OK"}

