"""QueryService contract: retrieve -> decode -> preview, framework-free. Uses a
tiny in-memory model (no checkpoint) so it stays fast and server-free."""

from __future__ import annotations

import sys
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from tinyllm import generate_example  # noqa: E402
from tinyllm.db import SqliteDb  # noqa: E402
from tinyllm.model import EncoderDecoder, ModelConfig  # noqa: E402
from tinyllm.serve import QueryResult, QueryService  # noqa: E402
from tinyllm.tokenizer import BPETokenizer  # noqa: E402


def _service():
    torch.manual_seed(0)
    ex = generate_example(0, level=2)
    tok = BPETokenizer().train([ex.question, ex.sql], vocab_size=320)
    cfg = ModelConfig(vocab_size=tok.vocab_size, d_model=64, n_heads=4,
                      n_enc_layers=2, n_dec_layers=2, pad_id=tok.special("<pad>"))
    model = EncoderDecoder(cfg)
    svc = QueryService(model, tok, {"demo": ex.schema},
                       dbs={"demo": SqliteDb(ex.schema, seed=3)}, beam=2, max_len=20)
    return svc, ex


def test_query_result_contract():
    service, ex = _service()
    assert service.schema_ids() == ["demo"]
    res = service.query(ex.question, "demo")
    assert isinstance(res, QueryResult)
    assert res.schema_id == "demo" and res.question == ex.question
    assert isinstance(res.sql, str)
    # retrieval only ever returns real tables from the schema
    assert set(res.tables_used) <= set(ex.schema.table_names)
    # the service PREVIEWS; it must never execute
    assert res.requires_confirmation is True and res.executed is False
    assert isinstance(res.to_dict(), dict)


def test_unknown_schema_raises():
    service, ex = _service()
    try:
        service.query("anything", "nope")
        assert False, "expected KeyError"
    except KeyError:
        pass


def test_fastapi_app_smoke():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from tinyllm.serve import build_app
    service, ex = _service()
    client = TestClient(build_app(service))

    assert "TinyLLM" in client.get("/").text                      # UI served at /
    assert client.get("/schemas").json()["schemas"] == ["demo"]
    assert "sql" in client.post("/query",
                                json={"question": ex.question, "schema_id": "demo"}).json()
    ok = client.post("/execute", json={"schema_id": "demo", "sql": ex.sql, "max_rows": 3})
    assert ok.status_code == 200 and "rows" in ok.json()
    blocked = client.post("/execute", json={"schema_id": "demo", "sql": "DROP TABLE x"})
    assert blocked.status_code == 400                              # read-only refuses writes


def test_admin_extract_and_status(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from tinyllm.serve import build_app
    service, _ = _service()
    admin = {"schemas_dir": str(tmp_path / "sch"), "models_dir": str(tmp_path / "mod"),
             "base_ckpt": "base.pt", "base_tok": "base.json"}
    client = TestClient(build_app(service, admin=admin))

    assert "Setup" in client.get("/setup").text
    r = client.post("/admin/extract", json={"schema_id": "ebs", "mock": True})
    assert r.status_code == 200
    assert any(t["name"] == "ap_invoices_all" for t in r.json()["tables"])
    assert "ebs" in service.schema_ids()                           # registered at runtime
    assert client.get("/admin/train/status").json()["state"] == "idle"
