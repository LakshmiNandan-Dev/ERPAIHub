from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

import os

from . import models, utils
from .database import engine, get_db, SessionLocal
from .middleware import TelemetryMiddleware
from .llm_service import DEFAULT_MODELS, _OLLAMA_BASE
from .routers import auth, chat, rag, rl, deployments, deployment_agent, performance_agent, admin, config, sso, monitoring, cloning, audit, training, patching

models.Base.metadata.create_all(bind=engine)


def seed_initial_admin():
    """
    Ensure a default administrator exists so the app is usable out of the box.
    Creates username 'admin' / password 'Admin' (role=admin) if absent.
    The user is expected to change this password after first login.
    """
    db = SessionLocal()
    try:
        admin = db.query(models.User).filter(models.User.username == "admin").first()
        if not admin:
            db.add(models.User(
                username="admin",
                email="admin@oraebs.com",
                password_hash=utils.hash_password("Admin"),
                is_admin=True,
                role="admin",
            ))
            db.commit()
            print("[seed] Default admin created — username='admin', password='Admin'. "
                  "Change this password after first login.")
        elif not admin.is_admin or getattr(admin, "role", None) != "admin":
            # Ensure the bootstrap account always has administrator rights.
            admin.is_admin = True
            admin.role = "admin"
            db.commit()
            print("[seed] Promoted existing 'admin' account to administrator.")
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
            model=DEFAULT_MODELS.get("ollama", "llama3.2:1b"),
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

gateway.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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

