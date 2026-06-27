"""Tests for the from-scratch encoder-decoder: shapes, tied weights, and that
it actually learns (overfits a tiny batch). CPU + tiny config to stay fast."""

from __future__ import annotations

import sys
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tinyllm import generate_example, serialize_schema  # noqa: E402
from tinyllm.model import EncoderDecoder, ModelConfig, collate  # noqa: E402
from tinyllm.tokenizer import BPETokenizer  # noqa: E402


def _cfg(vocab, pad=0):
    return ModelConfig(
        vocab_size=vocab, d_model=64, n_heads=4,
        n_enc_layers=2, n_dec_layers=2, max_seq_len=256, pad_id=pad,
    )


def test_forward_shapes_and_finite_loss():
    torch.manual_seed(0)
    cfg = _cfg(300)
    model = EncoderDecoder(cfg)
    B, S, T = 2, 9, 7
    src = torch.randint(1, 300, (B, S))
    tgt = torch.randint(1, 300, (B, T))
    labels = torch.randint(1, 300, (B, T))
    keep_s = torch.ones(B, S, dtype=torch.bool)
    keep_t = torch.ones(B, T, dtype=torch.bool)
    logits, loss = model(src, tgt, keep_s, keep_t, labels)
    assert logits.shape == (B, T, 300)
    assert torch.isfinite(loss)


def test_embeddings_are_tied():
    model = EncoderDecoder(_cfg(300))
    assert model.lm_head.weight is model.embed.weight
    assert model.num_params() > 0


def test_padded_keys_do_not_break_loss():
    torch.manual_seed(0)
    cfg = _cfg(300, pad=0)
    model = EncoderDecoder(cfg)
    src = torch.tensor([[5, 6, 7, 0, 0], [8, 9, 1, 2, 0]])   # 0 == pad
    tgt = torch.tensor([[1, 5, 6, 0], [1, 7, 8, 9]])
    labels = torch.tensor([[5, 6, 0, 0], [7, 8, 9, 2]])
    _, loss = model(src, tgt, src != 0, tgt != 0, labels)
    assert torch.isfinite(loss)


def test_overfits_tiny_batch():
    torch.manual_seed(0)
    examples = [generate_example(s, level=1 + s % 5) for s in range(6)]
    pairs = [(e.question, serialize_schema(e.schema), e.sql) for e in examples]
    corpus = [t for e in examples for t in (e.question, serialize_schema(e.schema), e.sql)]
    tok = BPETokenizer().train(corpus, vocab_size=512)

    model = EncoderDecoder(_cfg(tok.vocab_size, pad=tok.special("<pad>")))
    batch = collate(pairs, tok, "cpu")
    opt = torch.optim.AdamW(model.parameters(), lr=3e-3)

    model.train()
    losses = []
    for _ in range(150):
        _, loss = model(batch["src"], batch["tgt_in"], batch["src_keep"],
                        batch["tgt_keep"], batch["labels"])
        opt.zero_grad()
        loss.backward()
        opt.step()
        losses.append(loss.item())

    assert losses[-1] < losses[0] * 0.3, (losses[0], losses[-1])
    assert losses[-1] < 1.5
