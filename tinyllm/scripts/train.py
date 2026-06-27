#!/usr/bin/env python3
"""Train the encoder-decoder on synthetic NL->SQL with a cross-schema split.

    python scripts/train.py --train 1500 --val 200 --steps 600

Trains the BPE tokenizer on the train split, then the model, evaluating on
UNSEEN schemas (disjoint seeds) -- exact-match there is the generalization
signal, not memorization.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tinyllm.model import EncoderDecoder, ModelConfig  # noqa: E402
from tinyllm.tokenizer import BPETokenizer  # noqa: E402
from tinyllm.train import TrainConfig, Trainer, corpus_texts, make_split  # noqa: E402


def pick_device():
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", type=int, default=1500, help="# training schemas")
    ap.add_argument("--val", type=int, default=200, help="# unseen validation schemas")
    ap.add_argument("--steps", type=int, default=600)
    ap.add_argument("--batch-size", type=int, default=48)
    ap.add_argument("--paraphrases", type=int, default=2)
    ap.add_argument("--style", choices=("default", "procedural", "ebs"), default="default",
                    help="naming: default | procedural (forces linking) | ebs (real EBS names)")
    ap.add_argument("--vocab", type=int, default=2048)
    ap.add_argument("--d-model", type=int, default=256)
    ap.add_argument("--dropout", type=float, default=0.0)
    ap.add_argument("--lr", type=float, default=5e-4)
    ap.add_argument("--eval-every", type=int, default=150)
    ap.add_argument("--warmup", type=int, default=50)
    ap.add_argument("--ckpt-dir", default="artifacts")
    ap.add_argument("--device", default=None)
    args = ap.parse_args()

    device = args.device or pick_device()
    torch.manual_seed(0)

    print(f"building split: {args.train} train schemas, {args.val} unseen val schemas "
          f"(style={args.style}) ...")
    train_pairs, val_pairs = make_split(args.train, args.val, paraphrases=args.paraphrases,
                                        style=args.style)
    print(f"  {len(train_pairs):,} train pairs (with paraphrases), {len(val_pairs)} val pairs")

    tok = BPETokenizer().train(corpus_texts(train_pairs), vocab_size=args.vocab)
    print(f"  tokenizer vocab_size={tok.vocab_size}")

    cfg = ModelConfig(
        vocab_size=tok.vocab_size, d_model=args.d_model, n_heads=8,
        n_enc_layers=4, n_dec_layers=4, dropout=args.dropout, pad_id=tok.special("<pad>"),
    )
    model = EncoderDecoder(cfg)
    print(f"  model params={model.num_params()/1e6:.1f}M  dropout={args.dropout}  device={device}")

    tcfg = TrainConfig(total_steps=args.steps, batch_size=args.batch_size, lr=args.lr,
                       eval_every=args.eval_every, warmup=args.warmup, device=device,
                       ckpt_dir=args.ckpt_dir)
    Trainer(model, tok, train_pairs, val_pairs, tcfg).train()
    print(f"best checkpoint -> {args.ckpt_dir}/model_best.pt")


if __name__ == "__main__":
    main()
