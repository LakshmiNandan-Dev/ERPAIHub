"""Tests for the from-scratch BPE tokenizer: exact round-trip, special tokens,
compression, and save/load fidelity."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tinyllm import generate_example, serialize_schema  # noqa: E402
from tinyllm.tokenizer import BPETokenizer  # noqa: E402


def _texts(seed: int):
    ex = generate_example(seed, level=1 + seed % 5, n_paraphrases=2)
    return [serialize_schema(ex.schema), ex.question, ex.sql, *ex.paraphrases]


@pytest.fixture(scope="module")
def tok() -> BPETokenizer:
    corpus = [t for seed in range(300) for t in _texts(seed)]
    return BPETokenizer().train(corpus, vocab_size=1024)


def test_roundtrip_exact(tok):
    for seed in range(300, 380):  # unseen seeds
        for text in _texts(seed):
            assert tok.decode(tok.encode(text)) == text


def test_special_tokens_roundtrip(tok):
    s = "<question> total amount by vendor <schema> v : vendor_id <sql>"
    ids = tok.encode(s)
    assert tok.special("<question>") in ids
    assert tok.special("<sql>") in ids
    assert tok.decode(ids) == s


def test_identifiers_compress(tok):
    ex = generate_example(9999, level=5)
    ids = tok.encode(ex.sql)
    assert len(ids) < len(ex.sql)          # fewer tokens than characters
    assert tok.decode(ids) == ex.sql


def test_save_load_fidelity(tmp_path, tok):
    path = tmp_path / "tok.json"
    tok.save(path)
    reloaded = BPETokenizer.load(path)
    assert reloaded.vocab_size == tok.vocab_size
    for seed in (123, 456, 789):
        sql = generate_example(seed, level=4).sql
        assert reloaded.encode(sql) == tok.encode(sql)
        assert reloaded.decode(reloaded.encode(sql)) == sql
