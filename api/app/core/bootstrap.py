"""Application bootstrap: schema provisioning + first-run seeding.

Invoked once at startup by app.main. The schema source of truth is the models'
metadata (create_all), not the Alembic migration chain. Seeding is best-effort
and never blocks startup.
"""
import os

from sqlalchemy import text

from app import models
from app.common import utils
from app.core.database import engine, SessionLocal
from app.core.llm.llm_service import DEFAULT_MODELS, _OLLAMA_BASE


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


def seed_rbac_defaults():
    """
    Activate the role/agent permission model out of the box.

    1. Ensure an `agents` row exists for every gated agent (deployment,
       performance, cloning, patching).
    2. Ensure two default roles: 'DBA' (grants all gated agents) and 'User'
       (grants none) so the console has something to assign immediately.
    3. One-time backfill: any non-admin user that has no role yet inherits a
       role matching their legacy tier (dba → DBA, otherwise → User). This keeps
       existing accounts working exactly as before the per-agent gate landed.

    Idempotent and best-effort — never blocks startup.
    """
    from app.core.auth.auth import GATED_AGENTS

    db = SessionLocal()
    try:
        # 1. Gated agents
        agents = {}
        for name, desc in GATED_AGENTS.items():
            agent = db.query(models.Agent).filter(models.Agent.name == name).first()
            if not agent:
                agent = models.Agent(name=name, description=desc)
                db.add(agent)
            agents[name] = agent
        db.flush()

        # 2. Default roles
        dba_role = db.query(models.Role).filter(models.Role.name == "DBA").first()
        if not dba_role:
            dba_role = models.Role(name="DBA", description="Full agent access (all EBS agents).")
            db.add(dba_role)
        dba_role.agents = list(agents.values())  # always keep DBA granting every agent

        user_role = db.query(models.Role).filter(models.Role.name == "User").first()
        if not user_role:
            user_role = models.Role(name="User", description="Chat only — no agent access.")
            db.add(user_role)
        db.flush()

        # 3. Backfill users that have never been assigned a role
        for u in db.query(models.User).filter(models.User.role != "admin").all():
            if u.roles:
                continue
            u.roles = [dba_role] if u.role == "dba" else [user_role]

        db.commit()
        print("[seed] RBAC defaults ensured (agents + DBA/User roles, users backfilled).")
    except Exception as exc:  # never block startup on seeding
        db.rollback()
        print(f"[seed] Could not seed RBAC defaults: {exc}")
    finally:
        db.close()


def ensure_schema_upgrades():
    """
    Apply additive column upgrades that create_all can't make to *existing*
    tables (create_all only creates missing tables, never alters present ones).
    Each statement is idempotent (ADD COLUMN IF NOT EXISTS) and best-effort so a
    failure here never blocks startup.
    """
    statements = [
        # Dedup support: hash of the uploaded bytes (added 2026-06).
        "ALTER TABLE rag_documents ADD COLUMN IF NOT EXISTS content_hash VARCHAR(64)",
        "CREATE INDEX IF NOT EXISTS ix_rag_documents_content_hash "
        "ON rag_documents (content_hash)",
        # Per-task output-token cap for the monitoring drill-down (added 2026-06).
        "ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS token_limit INTEGER",
    ]
    for stmt in statements:
        try:
            with engine.begin() as conn:
                conn.execute(text(stmt))
        except Exception as exc:  # never block startup on a schema tweak
            print(f"[schema] Skipped '{stmt[:48]}...': {exc}")


def run_bootstrap():
    """Provision the schema then run first-run seeding. Called once at startup."""
    models.Base.metadata.create_all(bind=engine)
    ensure_schema_upgrades()
    seed_initial_admin()
    seed_default_llm_provider()
    seed_rbac_defaults()
