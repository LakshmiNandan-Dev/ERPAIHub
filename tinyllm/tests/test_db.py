"""Runtime DB gate: EXPLAIN validates against the live schema and returns no
rows; read-only execute returns rows and refuses writes; QueryService runs the
propose -> EXPLAIN -> confirmed-execute loop."""

from __future__ import annotations

import sys
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tinyllm import generate_example  # noqa: E402
from tinyllm.db import SqliteDb  # noqa: E402
from tinyllm.model import EncoderDecoder, ModelConfig  # noqa: E402
from tinyllm.serve import QueryService  # noqa: E402
from tinyllm.tokenizer import BPETokenizer  # noqa: E402


def test_explain_accepts_gold_rejects_bad():
    ex = generate_example(1_000_000, level=2)
    db = SqliteDb(ex.schema, seed=1)
    assert db.explain(ex.sql).ok is True
    t = ex.schema.tables[0]
    assert db.explain(f"SELECT x.zzz_nope FROM {t.name} x").ok is False
    db.close()


def test_run_readonly_returns_rows_and_blocks_writes():
    ex = generate_example(1_000_001, level=1)
    db = SqliteDb(ex.schema, seed=1)
    rows = db.run_readonly(ex.sql, max_rows=5)
    assert isinstance(rows, list) and len(rows) <= 5
    for stmt in (f"DROP TABLE {ex.schema.tables[0].name}", "DELETE FROM whatever"):
        try:
            db.run_readonly(stmt)
            assert False, "non-SELECT must be refused"
        except ValueError:
            pass
    db.close()


def test_service_explain_and_confirmed_execute():
    torch.manual_seed(0)
    ex = generate_example(1_000_002, level=2)
    tok = BPETokenizer().train([ex.question, ex.sql], vocab_size=320)
    cfg = ModelConfig(vocab_size=tok.vocab_size, d_model=64, n_heads=4,
                      n_enc_layers=2, n_dec_layers=2, pad_id=tok.special("<pad>"))
    db = SqliteDb(ex.schema, seed=2)
    svc = QueryService(EncoderDecoder(cfg), tok, {"s": ex.schema}, dbs={"s": db},
                       beam=2, max_len=20)

    res = svc.query(ex.question, "s")
    assert res.explain_ok in (True, False)            # a DB is wired -> not None
    assert res.executed is False                       # /query never executes

    rows = svc.execute("s", ex.sql, max_rows=3)         # confirmed step runs read-only
    assert isinstance(rows, list) and len(rows) <= 3
    db.close()
