"""
Shared pytest fixtures for OraEBS Agent test suite.

Database strategy:
  - A separate PostgreSQL test database is used (TEST_DATABASE_URL env var).
  - Tables are dropped and recreated once per session for a clean slate.
  - Each test wraps DB writes in a savepoint that is rolled back after the test,
    keeping tests isolated while keeping permanent seed data (admin, SsoSettings).
"""
import os
import sys
import uuid
import json
import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, Session

# ── Env config — MUST be set before app modules are imported ──────────────────
os.environ.setdefault(
    "DATABASE_URL",
    os.getenv("TEST_DATABASE_URL", "postgresql://aiuser:aipassword@localhost:5432/oraebsagent_test"),
)
os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-not-for-production")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from starlette.testclient import TestClient
from app.core.database import Base, get_db
from app.main import app
from app import models
from app.common import utils

# ── Engine bound to the TEST database ─────────────────────────────────────────
TEST_DB_URL = os.environ["DATABASE_URL"]
test_engine = create_engine(TEST_DB_URL)
TestSessionFactory = sessionmaker(bind=test_engine)


class _SharedSession:
    """Proxy that makes ``database.SessionLocal()`` hand back the test's single
    request session instead of opening a new connection. App code that manages its
    own session (the chat streaming save, the RLAIF audit, semantic cache, prompt
    overrides) then reads/writes inside the SAME savepoint-isolated transaction as
    the request — so it sees the request's still-uncommitted rows (no FK errors)
    and its writes are rolled back with the test. ``close()`` is a no-op so those
    callers' ``finally: db.close()`` can't drop the shared session; using ONE
    session keeps a single savepoint (no competing-savepoint corruption)."""

    def __init__(self, session):
        self._s = session

    def __getattr__(self, name):
        return getattr(self._s, name)

    def close(self):
        pass

    def __enter__(self):
        return self._s

    def __exit__(self, *exc):
        return False


# ── Session-scoped: drop/create tables + seed once per test run ───────────────

@pytest.fixture(scope="session", autouse=True)
def setup_test_database():
    """Create a clean schema for the entire test session."""
    Base.metadata.drop_all(bind=test_engine)
    Base.metadata.create_all(bind=test_engine)

    db = TestSessionFactory()
    try:
        # Seed admin user
        db.add(models.User(
            username="admin",
            email="admin@oraebs.com",
            password_hash=utils.hash_password("Admin123!"),
            is_admin=True,
            role="admin",
            is_active=True,
            approval_status="approved",
        ))
        # Seed second admin (for maker-checker tests)
        db.add(models.User(
            username="admin2",
            email="admin2@oraebs.com",
            password_hash=utils.hash_password("Admin123!"),
            is_admin=True,
            role="admin",
            is_active=True,
            approval_status="approved",
        ))
        # Enable public signup so tests can register users
        db.add(models.SsoSettings(id=1, signup_enabled=True))
        db.commit()
    finally:
        db.close()
    yield


# ── Function-scoped: transactional rollback for test isolation ────────────────

@pytest.fixture
def db_session(monkeypatch):
    """Each test gets an isolated DB session — all writes are rolled back after.

    The session uses the classic begin_nested + restart-savepoint pattern. We also
    point the app's ``database.SessionLocal`` at this same session (via
    _SharedSession), so code that opens its OWN session — the chat streaming save,
    the RLAIF audit, semantic cache, prompt-override lookups — runs inside this
    test's transaction and can see rows the request session has written but not yet
    committed (otherwise a fresh SessionLocal connection can't see them and FK
    inserts like the assistant-message save fail)."""
    import app.core.database as database

    connection = test_engine.connect()
    outer_tx = connection.begin()
    session = Session(bind=connection)
    session.begin_nested()  # savepoint

    @event.listens_for(session, "after_transaction_end")
    def restart_savepoint(sess, tx):
        if tx.nested and not tx._parent.nested:
            sess.begin_nested()

    monkeypatch.setattr(database, "SessionLocal", lambda: _SharedSession(session))

    yield session

    session.close()
    outer_tx.rollback()
    connection.close()


@pytest.fixture
def client(db_session):
    """TestClient wired to the test database session."""
    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _login(client, username, password) -> str:
    r = client.post("/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, f"Login failed: {r.text}"
    return r.json()["session_token"]


@pytest.fixture
def admin_headers(client):
    token = _login(client, "admin", "Admin123!")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin2_headers(client):
    token = _login(client, "admin2", "Admin123!")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def user_creds():
    uid = uuid.uuid4().hex[:8]
    return {"username": f"user_{uid}", "email": f"user_{uid}@test.com", "password": "TestPass123!"}


@pytest.fixture
def regular_user_headers(client, admin_headers, user_creds):
    """Register a regular user and approve them, return auth headers."""
    r = client.post("/auth/register", json=user_creds)
    assert r.status_code == 201
    user_id = r.json()["id"]
    # Admin approves and activates the new user (both fields required — see
    # test_roles.py's equivalent inline setup for the same pattern)
    client.patch(f"/admin/users/{user_id}", json={"approval_status": "approved", "is_active": True},
                headers=admin_headers)
    token = _login(client, user_creds["username"], user_creds["password"])
    return {"Authorization": f"Bearer {token}"}


# ── Data helpers ──────────────────────────────────────────────────────────────

@pytest.fixture
def nonprod_env(client, admin_headers):
    """Create and return a registered non-prod environment."""
    r = client.post("/admin/environments", json={
        "name": f"UAT_{uuid.uuid4().hex[:4].upper()}",
        "tier": "nonprod",
        "db_host": "uat-db.example.com",
        "db_sid": "UATDB",
        "db_user": "apps",
    }, headers=admin_headers)
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture
def prod_env(client, admin_headers):
    """Create and return a registered PROD environment."""
    r = client.post("/admin/environments", json={
        "name": f"PROD_{uuid.uuid4().hex[:4].upper()}",
        "tier": "prod",
        "db_host": "prod-db.example.com",
        "db_sid": "PRODDB",
    }, headers=admin_headers)
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture
def ssh_server(client, admin_headers):
    """Create and return a registered SSH server."""
    r = client.post("/admin/servers", json={
        "name": f"srv_{uuid.uuid4().hex[:6]}",
        "hostname": "app-server.example.com",
        "port": 22,
        "username": "applmgr",
        "server_type": "application",
    }, headers=admin_headers)
    assert r.status_code == 201, r.text
    return r.json()


# ── LLM mock helpers ──────────────────────────────────────────────────────────

@pytest.fixture
def mock_llm(monkeypatch):
    """Mock LLM streaming and sync completion — no Ollama required.

    Signatures accept **kwargs so the mocks stay compatible as the real service
    grows optional parameters (e.g. usage / max_tokens / temperature on
    stream_tokens; max_tokens / temperature / use_cache on complete_sync). When a
    mutable ``usage`` dict is passed it is populated, mirroring the real service."""
    from app.core.llm import llm_service

    async def _stream(messages, provider="ollama", model=None, api_key=None,
                      base_url=None, usage=None, **kwargs):
        if usage is not None:
            usage["prompt_tokens"] = 10
            usage["completion_tokens"] = 12
        yield "Oracle EBS mock response. "
        yield "Deployment uses sqlplus apps/apps @script.sql."

    def _complete(messages, provider="ollama", model=None, api_key=None,
                  base_url=None, **kwargs):
        return '{"score": 1, "reasoning": "Response is accurate.", "suggested_correction": ""}'

    monkeypatch.setattr(llm_service, "stream_tokens", _stream)
    monkeypatch.setattr(llm_service, "complete_sync", _complete)


@pytest.fixture
def mock_rag(monkeypatch):
    """Mock RAG service — returns empty context by default. The query_rag mock
    accepts the same optional kwargs as the real service (n_results,
    allow_web_fallback) plus **kwargs for forward-compatibility."""
    from app.core.rag import rag_service
    monkeypatch.setattr(rag_service, "query_rag",
                        lambda q, n_results=4, allow_web_fallback=True, **kw: "")
    monkeypatch.setattr(rag_service, "index_document", lambda *a, **kw: 5)
    monkeypatch.setattr(rag_service, "delete_document_chunks", lambda doc_id: None)


# ── SSE parser ────────────────────────────────────────────────────────────────

def parse_sse(text: str) -> list:
    """Extract the data payload from each SSE line."""
    result = []
    for line in text.strip().split("\n"):
        if line.startswith("data: "):
            raw = line[6:].strip()
            if raw == "[DONE]":
                continue
            try:
                result.append(json.loads(raw))
            except json.JSONDecodeError:
                result.append(raw)
    return result


def chat_session(client, headers) -> int:
    """Helper: create a chat session and return its id."""
    r = client.post("/chat/sessions", json={"title": "Test Session"}, headers=headers)
    assert r.status_code == 201
    return r.json()["id"]
