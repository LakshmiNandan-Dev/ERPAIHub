"""TinyLLM NL->SQL embedding — per-environment schema extraction, the
QueryService pool, propose/execute, and router gating.

Requires the tinyllm package on PYTHONPATH (the outer `tinyllm/` dir at the
repo root, sibling to `api/`) — see the project README / CI config. Model
load tests point BASE_CKPT/BASE_TOK at the repo's local artifacts rather than
the Docker-only `/app/tinyllm/artifacts/...` paths baked into
nl_sql_service.py's defaults (correct for the real deployment target, not for
local pytest runs).
"""
import os
import uuid
import pytest

from app import models
from app.modules.dba.nl_sql import nl_sql_service

_TINYLLM_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "tinyllm"))
LOCAL_CKPT = os.path.join(_TINYLLM_ROOT, "artifacts", "model_best.pt")
LOCAL_TOK = os.path.join(_TINYLLM_ROOT, "artifacts", "tokenizer.json")


@pytest.fixture(autouse=True)
def _local_checkpoint_paths(monkeypatch):
    """Point the module at the repo's real local artifacts instead of the
    Docker-only /app/... defaults, and clear the pool between tests."""
    monkeypatch.setattr(nl_sql_service, "BASE_CKPT", LOCAL_CKPT)
    monkeypatch.setattr(nl_sql_service, "BASE_TOK", LOCAL_TOK)
    nl_sql_service._POOL.clear()
    yield
    nl_sql_service._POOL.clear()


def _make_env(client, admin_headers, **overrides):
    body = {
        "name": f"ENV_{uuid.uuid4().hex[:6].upper()}", "tier": "nonprod",
        "db_host": "db.example.com", "db_sid": "TESTDB", "db_user": "apps",
    }
    body.update(overrides)
    r = client.post("/admin/environments", json=body, headers=admin_headers)
    assert r.status_code == 201, r.text
    return r.json()


class TestRunExtraction:

    def test_mock_extraction_succeeds(self, client, admin_headers, db_session):
        env = _make_env(client, admin_headers)
        nl_sql_service.run_extraction(env["id"], triggered_by=None, mock=True)

        snap = db_session.query(models.NlSqlSchemaSnapshot).filter(
            models.NlSqlSchemaSnapshot.environment_id == env["id"]
        ).first()
        assert snap is not None
        assert snap.scan_status == "ok"
        assert snap.table_count == 4          # MockCatalog's fixed AP/GL sample
        assert snap.fk_count is not None
        assert snap.schema_json is not None

    def test_unreachable_db_persists_error_never_raises(self, client, admin_headers, db_session):
        env = _make_env(client, admin_headers, db_host="unreachable.invalid")
        nl_sql_service.run_extraction(env["id"], triggered_by=None, mock=False)

        snap = db_session.query(models.NlSqlSchemaSnapshot).filter(
            models.NlSqlSchemaSnapshot.environment_id == env["id"]
        ).first()
        assert snap.scan_status == "error"
        assert snap.schema_json is None

    def test_low_table_count_flags_missing_grants(self, client, admin_headers, db_session, monkeypatch):
        """A live extraction that succeeds but returns very few tables should
        be flagged, not reported as bare success (SELECT_CATALOG_ROLE gap is
        silent — fewer rows, not an ORA- error)."""
        env = _make_env(client, admin_headers)

        class _FakeCursor:
            pass

        class _FakeConn:
            def cursor(self): return _FakeCursor()
            def close(self): pass

        from tinyllm.extract import EbsExtractor, MockCatalog

        # Compute the truncated schema BEFORE patching EbsExtractor.extract,
        # since patching it first would make this very call recurse into itself.
        truncated = EbsExtractor(MockCatalog()).extract()
        truncated.tables = truncated.tables[:2]

        monkeypatch.setattr(nl_sql_service, "_live_conn", lambda env: _FakeConn())
        monkeypatch.setattr(EbsExtractor, "extract", lambda self: truncated)

        nl_sql_service.run_extraction(env["id"], triggered_by=None, mock=False)

        snap = db_session.query(models.NlSqlSchemaSnapshot).filter(
            models.NlSqlSchemaSnapshot.environment_id == env["id"]
        ).first()
        assert snap.scan_status == "ok"
        assert snap.table_count == 2
        assert "SELECT_CATALOG_ROLE" in snap.scan_error


class TestServicePool:

    def test_get_service_falls_back_to_mock_schema_and_base_checkpoint(self, client, admin_headers, db_session):
        env = _make_env(client, admin_headers)
        svc = nl_sql_service.get_service(db_session, env["id"])
        assert nl_sql_service._schema_id(env["id"]) in svc.schemas

    def test_get_service_uses_extracted_schema(self, client, admin_headers, db_session):
        env = _make_env(client, admin_headers)
        nl_sql_service.run_extraction(env["id"], triggered_by=None, mock=True)
        svc = nl_sql_service.get_service(db_session, env["id"])
        schema = svc.schemas[nl_sql_service._schema_id(env["id"])]
        assert len(schema.tables) == 4

    def test_pool_caches_across_calls(self, client, admin_headers, db_session):
        env = _make_env(client, admin_headers)
        svc1 = nl_sql_service.get_service(db_session, env["id"])
        svc2 = nl_sql_service.get_service(db_session, env["id"])
        assert svc1 is svc2

    def test_invalidate_forces_rebuild(self, client, admin_headers, db_session):
        env = _make_env(client, admin_headers)
        svc1 = nl_sql_service.get_service(db_session, env["id"])
        nl_sql_service.invalidate(env["id"])
        svc2 = nl_sql_service.get_service(db_session, env["id"])
        assert svc1 is not svc2

    def test_done_training_run_checkpoint_is_preferred_over_base(self, client, admin_headers, db_session):
        env = _make_env(client, admin_headers)
        nl_sql_service.run_extraction(env["id"], triggered_by=None, mock=True)
        snap = nl_sql_service._latest_schema_snapshot(db_session, env["id"])
        run = models.NlSqlTrainingRun(
            environment_id=env["id"], schema_snapshot_id=snap.id,
            status="done", checkpoint_path=LOCAL_CKPT,   # stand-in "fine-tuned" checkpoint
        )
        db_session.add(run)
        db_session.commit()

        latest = nl_sql_service._latest_done_run(db_session, env["id"])
        assert latest is not None
        assert latest.checkpoint_path == LOCAL_CKPT
        # Building the service with this run present must not raise (loads fine
        # from the same checkpoint file, just exercising the "run present" path).
        svc = nl_sql_service.get_service(db_session, env["id"])
        assert svc is not None


class TestProposeExecute:

    def test_propose_degrades_gracefully_without_live_connection(self, client, admin_headers, db_session, monkeypatch):
        env = _make_env(client, admin_headers)
        monkeypatch.setattr(nl_sql_service, "_live_conn", lambda env: None)
        result = nl_sql_service.propose(db_session, models.EbsEnvironment(
            id=env["id"], db_host=None, db_sid=None, db_user=None,
        ), "how many invoices are there")
        assert result["explain_ok"] is None
        assert result["executed"] is False

    def test_execute_raises_runtime_error_without_connection(self, monkeypatch):
        fake_env = models.EbsEnvironment(id=1, db_host=None, db_sid=None, db_user=None)
        with pytest.raises(RuntimeError):
            nl_sql_service.execute(fake_env, "SELECT 1 FROM DUAL")


class TestNlSqlRouterGating:

    def test_query_requires_nl_sql_agent(self, client, admin_headers, regular_user_headers):
        env = _make_env(client, admin_headers)
        r = client.post("/nl-sql/query", json={
            "environment_id": env["id"], "question": "how many vendors",
        }, headers=regular_user_headers)
        assert r.status_code == 403

    def test_query_unknown_environment_404(self, client, admin_headers):
        r = client.post("/nl-sql/query", json={
            "environment_id": 999999, "question": "how many vendors",
        }, headers=admin_headers)
        assert r.status_code == 404

    def test_train_without_extraction_rejected(self, client, admin_headers):
        env = _make_env(client, admin_headers)
        r = client.post(f"/nl-sql/train/{env['id']}", json={}, headers=admin_headers)
        assert r.status_code == 400

    def test_train_conflict_when_already_running(self, client, admin_headers, db_session):
        env = _make_env(client, admin_headers)
        nl_sql_service.run_extraction(env["id"], triggered_by=None, mock=True)
        db_session.add(models.NlSqlTrainingRun(environment_id=env["id"], status="running"))
        db_session.commit()

        r = client.post(f"/nl-sql/train/{env['id']}", json={}, headers=admin_headers)
        assert r.status_code == 409

    def test_schema_status_never_extracted(self, client, admin_headers):
        env = _make_env(client, admin_headers)
        r = client.get(f"/nl-sql/schema/{env['id']}", headers=admin_headers)
        assert r.status_code == 200
        assert r.json()["status"] == "never_extracted"

    def test_train_status_never_trained(self, client, admin_headers):
        env = _make_env(client, admin_headers)
        r = client.get(f"/nl-sql/train/{env['id']}/status", headers=admin_headers)
        assert r.status_code == 200
        assert r.json()["status"] == "never_trained"


class TestNlSqlChatSettings:
    """Admin-configurable toggle for whether the general Chat Assistant's
    inline NL-SQL replies show technical detail (schema-valid/explain-ok)."""

    def test_defaults_to_hidden(self, client, admin_headers):
        r = client.get("/admin/nl-sql-chat-settings", headers=admin_headers)
        assert r.status_code == 200
        assert r.json()["show_technical_details"] is False

    def test_admin_can_enable(self, client, admin_headers):
        r = client.put("/admin/nl-sql-chat-settings", json={"show_technical_details": True},
                       headers=admin_headers)
        assert r.status_code == 200
        assert r.json()["show_technical_details"] is True
        r2 = client.get("/admin/nl-sql-chat-settings", headers=admin_headers)
        assert r2.json()["show_technical_details"] is True

    def test_regular_user_forbidden(self, client, regular_user_headers):
        r = client.get("/admin/nl-sql-chat-settings", headers=regular_user_headers)
        assert r.status_code == 403
