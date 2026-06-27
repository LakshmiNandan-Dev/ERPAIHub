#!/usr/bin/env python3
"""Overfit a tiny batch -- proves the from-scratch encoder-decoder learns.

Generates a handful of (question, schema, SQL) examples, trains the dev-config
model on them, and watches the loss fall to ~0 and greedy decoding reproduce the
gold SQL.

    python scripts/overfit.py --n 24 --steps 400
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tinyllm import generate_example, serialize_schema  # noqa: E402
from tinyllm.model import EncoderDecoder, ModelConfig, collate  # noqa: E402
from tinyllm.tokenizer import BPETokenizer  # noqa: E402


def pick_device():
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=24)
    ap.add_argument("--steps", type=int, default=400)
    ap.add_argument("--lr", type=float, default=3e-3)
    ap.add_argument("--d-model", type=int, default=256)
    ap.add_argument("--device", default=None)
    args = ap.parse_args()

    device = args.device or pick_device()
    torch.manual_seed(0)

    examples = [generate_example(s, level=1 + s % 5) for s in range(args.n)]
    pairs = [(e.question, serialize_schema(e.schema), e.sql) for e in examples]

    corpus = [t for e in examples for t in (e.question, serialize_schema(e.schema), e.sql)]
    tok = BPETokenizer().train(corpus, vocab_size=2048)

    cfg = ModelConfig(
        vocab_size=tok.vocab_size,
        d_model=args.d_model,
        n_heads=8,
        n_enc_layers=4,
        n_dec_layers=4,
        pad_id=tok.special("<pad>"),
    )
    model = EncoderDecoder(cfg).to(device)
    print(f"device={device}  params={model.num_params()/1e6:.1f}M  "
          f"vocab={cfg.vocab_size}  examples={args.n}")

    batch = collate(pairs, tok, device)
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, betas=(0.9, 0.95), weight_decay=0.0)

    model.train()
    t0 = time.time()
    first = None
    for step in range(1, args.steps + 1):
        _, loss = model(batch["src"], batch["tgt_in"], batch["src_keep"],
                        batch["tgt_keep"], batch["labels"])
        opt.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        opt.step()
        first = first if first is not None else loss.item()
        if step % max(1, args.steps // 10) == 0 or step == 1:
            print(f"  step {step:4d}  loss {loss.item():.4f}")
    print(f"loss {first:.3f} -> {loss.item():.4f}  ({time.time()-t0:.1f}s, "
          f"{args.steps/(time.time()-t0):.0f} steps/s)")

    # -- greedy decode a few -> exact-match against gold SQL ------------
    bos, eos = tok.special("<bos>"), tok.special("<eos>")
    exact = 0
    for i in range(min(5, args.n)):
        one = collate([pairs[i]], tok, device)
        out = model.generate(one["src"], one["src_keep"], bos, eos, max_len=160)[0].tolist()
        if eos in out:
            out = out[: out.index(eos)]
        pred = tok.decode(out[1:])  # drop <bos>
        gold = examples[i].sql
        ok = pred.strip() == gold.strip()
        exact += ok
        if i < 2:
            print(f"\n[{i}] Q: {examples[i].question}")
            print(f"    gold: {gold.splitlines()[0]} ...")
            print(f"    pred: {pred.splitlines()[0] if pred.strip() else '(empty)'} ...  match={ok}")
    print(f"\nexact-match (greedy) on {min(5, args.n)} held examples: {exact}/{min(5, args.n)}")


if __name__ == "__main__":
    main()
