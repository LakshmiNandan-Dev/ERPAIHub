"""Tests for graph-constrained decoding: the graph gate flags bad SQL and
accepts gold SQL; beam search produces well-formed candidates."""

from __future__ import annotations

import sys
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tinyllm import generate_example, serialize_schema  # noqa: E402
from tinyllm.decode import (  # noqa: E402
    SchemaPrefixGate,
    beam_search,
    build_token_strings,
    constrained_generate,
    graph_check_sql,
    hard_generate,
    picard_generate,
)
from tinyllm.model import EncoderDecoder, ModelConfig, collate  # noqa: E402
from tinyllm.schema_graph import SchemaGraph  # noqa: E402
from tinyllm.tokenizer import BPETokenizer  # noqa: E402


def test_graph_check_accepts_gold_sql():
    for seed in range(40):
        ex = generate_example(seed, level=1 + seed % 5)
        res = graph_check_sql(ex.sql, SchemaGraph(ex.schema))
        assert res.ok, (ex.sql, res.issues)


def test_graph_check_flags_unknown_column():
    ex = generate_example(2, level=1)
    table = ex.schema.tables[0].name
    bad = f"SELECT t.zzz_not_a_column FROM {table} t"
    res = graph_check_sql(bad, SchemaGraph(ex.schema))
    assert res.ok is False
    assert any("unknown column" in i for i in res.issues)


def test_graph_check_flags_wrong_join_key():
    # find an example with a foreign key, build a join on the wrong (PK) columns
    ex = next(generate_example(s, level=2) for s in range(20) if generate_example(s, 2).schema.foreign_keys)
    fk = ex.schema.foreign_keys[0]
    a, b = fk.from_table, fk.to_table
    pk_a = ex.schema.table(a).primary_key.name
    pk_b = ex.schema.table(b).primary_key.name
    bad = f"SELECT 1 FROM {a} x JOIN {b} y ON x.{pk_a} = y.{pk_b}"
    res = graph_check_sql(bad, SchemaGraph(ex.schema))
    assert res.ok is False
    assert any("join" in i for i in res.issues)


def _tiny_model_and_tok():
    ex = generate_example(0, level=2)
    tok = BPETokenizer().train([ex.question, ex.sql], vocab_size=320)
    cfg = ModelConfig(vocab_size=tok.vocab_size, d_model=64, n_heads=4,
                      n_enc_layers=2, n_dec_layers=2, pad_id=tok.special("<pad>"))
    return EncoderDecoder(cfg), tok, ex


def test_beam_search_well_formed():
    torch.manual_seed(0)
    model, tok, ex = _tiny_model_and_tok()
    batch = collate([(ex.question, "", ex.sql)], tok, "cpu")
    cands = beam_search(model, batch["src"], batch["src_keep"],
                        tok.special("<bos>"), tok.special("<eos>"), beam=4, max_len=20)
    assert cands and all(toks[0] == tok.special("<bos>") for toks, _ in cands)
    # sorted best-first by length-normalized score
    norm = [s / (len(t) ** 0.7) for t, s in cands]
    assert norm == sorted(norm, reverse=True)


def test_constrained_generate_returns_contract():
    torch.manual_seed(0)
    model, tok, ex = _tiny_model_and_tok()
    batch = collate([(ex.question, "", ex.sql)], tok, "cpu")
    sql, constrained = constrained_generate(model, tok, batch["src"], batch["src_keep"],
                                            ex.schema, beam=3, max_len=20)
    assert isinstance(sql, str) and isinstance(constrained, bool)


# -- incremental (PICARD-style) prefix gate --------------------------------
def test_prefix_gate_never_prunes_gold():
    """Every character prefix of valid gold SQL must survive the gate -- the
    superset of all token-boundary prefixes the decoder can ever produce."""
    for seed in range(60):
        ex = generate_example(seed, level=1 + seed % 5)
        gate = SchemaPrefixGate(ex.schema)
        for k in range(1, len(ex.sql) + 1):
            assert gate.ok(ex.sql[:k]), (ex.sql, ex.sql[:k])


def test_prefix_gate_rejects_hallucinated_table():
    ex = generate_example(3, level=1)
    gate = SchemaPrefixGate(ex.schema)
    # complete fake table, and a partial that prefixes no real table
    assert gate.ok("SELECT x.a FROM zzz_not_a_table x") is False
    assert gate.ok("SELECT 1 FROM zzz") is False
    # a real table (and prefixes of it) must pass
    real = ex.schema.tables[0].name
    assert gate.ok(f"SELECT 1 FROM {real}") is True
    assert gate.ok(f"SELECT 1 FROM {real[:3]}") is True


def test_prefix_gate_rejects_unknown_qualified_column():
    ex = generate_example(2, level=2)
    t = ex.schema.tables[0]
    gate = SchemaPrefixGate(ex.schema)
    bad = f"SELECT x.zzz_nope FROM {t.name} x"
    assert gate.ok(bad) is False
    good_col = t.columns[0].name
    assert gate.ok(f"SELECT x.{good_col} FROM {t.name} x") is True


def test_prefix_gate_rejects_wrong_join_key():
    # an example with an FK; the gate accepts the documented key, rejects a
    # structurally-plausible wrong one (PK=PK) that is not the FK edge
    ex = next(generate_example(s, level=2) for s in range(40)
              if generate_example(s, 2).schema.foreign_keys)
    fk = ex.schema.foreign_keys[0]
    a, b = fk.from_table, fk.to_table
    gate = SchemaPrefixGate(ex.schema)
    good = f"SELECT 1 FROM {a} x JOIN {b} y ON x.{fk.from_column} = y.{fk.to_column}"
    assert gate.ok(good) is True
    pk_a = ex.schema.table(a).primary_key.name
    pk_b = ex.schema.table(b).primary_key.name
    if pk_a != fk.from_column:                 # ensure it's really a different key
        bad = f"SELECT 1 FROM {a} x JOIN {b} y ON x.{pk_a} = y.{pk_b}"
        assert gate.ok(bad) is False
        # ...and it survives until the right column commits the bad key
        assert gate.ok(f"SELECT 1 FROM {a} x JOIN {b} y ON x.{pk_a} = ") is True


def test_prefix_gate_allows_extract_from_column():
    """EXTRACT(YEAR FROM alias.col) must not be mistaken for a table position."""
    ex = next(generate_example(s, level=2) for s in range(50)
              if any(c.role and c.role.value == "date"
                     for tb in generate_example(s, 2).schema.tables for c in tb.columns))
    gate = SchemaPrefixGate(ex.schema)
    t = ex.schema.tables[0]
    dcol = next((c.name for c in t.columns if c.type.value == "DATE"), t.columns[0].name)
    sql = f"SELECT x.{t.columns[0].name} FROM {t.name} x WHERE EXTRACT(YEAR FROM x.{dcol}) = 2024"
    # whole statement and the tricky mid-typing prefix both pass
    assert gate.ok(sql) is True
    cut = sql.index(f"FROM x.{dcol}") + len("FROM x.")  # ...EXTRACT(YEAR FROM x.<partial>
    assert gate.ok(sql[: cut + 2]) is True


def test_picard_generate_returns_contract():
    torch.manual_seed(0)
    model, tok, ex = _tiny_model_and_tok()
    batch = collate([(ex.question, "", ex.sql)], tok, "cpu")
    sql, constrained = picard_generate(model, tok, batch["src"], batch["src_keep"],
                                       ex.schema, beam=3, max_len=20)
    assert isinstance(sql, str) and isinstance(constrained, bool)


# -- hard constraint: logit masking forces real identifiers -----------------
def test_logit_mask_restricts_to_real_table_prefixes():
    from tinyllm.tokenizer import BPETokenizer
    ex = generate_example(3, level=2)
    gate = SchemaPrefixGate(ex.schema)
    tok = BPETokenizer().train([serialize_schema(ex.schema), ex.sql], vocab_size=400)
    ts = build_token_strings(tok)
    tables = {t.name for t in ex.schema.tables}

    # at a table slot, every identifier-token allowed must prefix a real table
    allowed = gate.allowed_next_tokens("SELECT 1 FROM ", ts)
    assert allowed is not None and allowed
    for tid in allowed:
        s = ts[tid]
        if s and (s[0].isalnum() or s[0] == "_"):
            assert any(t.startswith(s) for t in tables), s
    # a free position (right after SELECT) is unconstrained
    assert gate.allowed_next_tokens("SELECT ", ts) is None


def test_hard_generate_returns_contract():
    torch.manual_seed(0)
    model, tok, ex = _tiny_model_and_tok()
    batch = collate([(ex.question, "", ex.sql)], tok, "cpu")
    sql, ok = hard_generate(model, tok, batch["src"], batch["src_keep"],
                            ex.schema, beam=3, max_len=20)
    assert isinstance(sql, str) and isinstance(ok, bool)
