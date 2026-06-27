"""Tests for the training loop: it reduces val loss on UNSEEN schemas, returns
the expected metrics, and checkpoints round-trip."""

from __future__ import annotations

import sys
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tinyllm.model import EncoderDecoder, ModelConfig  # noqa: E402
from tinyllm.tokenizer import BPETokenizer  # noqa: E402
from tinyllm.train import (  # noqa: E402
    TrainConfig,
    Trainer,
    corpus_texts,
    load_model,
    make_split,
)


def test_training_reduces_val_loss_and_checkpoints(tmp_path):
    torch.manual_seed(0)
    train_pairs, val_pairs = make_split(n_train=40, n_val=16, paraphrases=1)
    tok = BPETokenizer().train(corpus_texts(train_pairs), vocab_size=512)

    cfg = ModelConfig(vocab_size=tok.vocab_size, d_model=64, n_heads=4,
                      n_enc_layers=2, n_dec_layers=2, pad_id=tok.special("<pad>"))
    model = EncoderDecoder(cfg)
    tcfg = TrainConfig(total_steps=60, batch_size=16, eval_every=60, log_every=10_000,
                       n_decode=8, decode_max_len=48, ckpt_dir=str(tmp_path), device="cpu")
    trainer = Trainer(model, tok, train_pairs, val_pairs, tcfg)

    before = trainer.evaluate()
    model.train()
    trainer.train()
    after = trainer.evaluate()

    assert after["val_loss"] < before["val_loss"]      # generalizes, not just memorizes
    assert set(after) == {"val_loss", "token_acc", "exact_match", "valid_sql"}
    assert 0.0 <= after["valid_sql"] <= 1.0

    ckpt = tmp_path / "model_best.pt"
    assert ckpt.exists()
    reloaded = load_model(ckpt, device="cpu")
    assert reloaded.cfg.vocab_size == cfg.vocab_size
