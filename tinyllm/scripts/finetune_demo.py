#!/usr/bin/env python3
"""The full deployed-accuracy stack on a real (extracted) EBS schema:

    vendor base  ->  + schema repair  ->  + customer-local fine-tune

Execution accuracy is measured on HELD-OUT queries over the customer's schema
(disjoint query seeds), so the fine-tune number is honest specialization, not
memorization.

    python3 scripts/finetune_demo.py --steps 200
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tinyllm.decode import picard_generate, schema_repair  # noqa: E402
from tinyllm.eval import ExecHarness, execution_match  # noqa: E402
from tinyllm.extract import extract_schema  # noqa: E402
from tinyllm.model import collate  # noqa: E402
from tinyllm.tokenizer import BPETokenizer  # noqa: E402
from tinyllm.train import TrainConfig, Trainer, load_model, make_local_split  # noqa: E402


def evaluate(model, tok, schema, val_pairs, beam, max_len=120):
    harness = ExecHarness(schema, seed=7)
    raw = rep = 0
    for q, s, gold in val_pairs:
        b = collate([(q, s, gold)], tok, "cpu")
        pred, _ = picard_generate(model, tok, b["src"], b["src_keep"], schema,
                                  beam=beam, max_len=max_len)
        raw += execution_match(harness, gold, pred)["match"]
        rep += execution_match(harness, gold, schema_repair(pred, q, schema))["match"]
    harness.close()
    n = len(val_pairs)
    return raw / n, rep / n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", default="artifacts/model_best.pt")
    ap.add_argument("--tok", default="artifacts/tokenizer.json")
    ap.add_argument("--train", type=int, default=400)
    ap.add_argument("--val", type=int, default=40)
    ap.add_argument("--steps", type=int, default=200)
    ap.add_argument("--beam", type=int, default=5)
    args = ap.parse_args()

    schema = extract_schema(mock=True)
    print(f"extracted EBS schema: {len(schema.tables)} tables")
    train_pairs, val_pairs = make_local_split(schema, args.train, args.val, paraphrases=2)
    print(f"  {len(train_pairs)} local train / {len(val_pairs)} held-out val queries\n")
    tok = BPETokenizer.load(args.tok)

    b, br = evaluate(load_model(args.ckpt), tok, schema, val_pairs, args.beam)
    print(f"vendor base          : exec {b:.2f}   + repair {br:.2f}")

    model = load_model(args.ckpt)
    with tempfile.TemporaryDirectory() as d:
        Trainer(model, tok, train_pairs, val_pairs,
                TrainConfig(total_steps=args.steps, eval_every=args.steps,
                            warmup=20, device="cpu", ckpt_dir=d)).train()
    f, fr = evaluate(model, tok, schema, val_pairs, args.beam)
    print(f"\n+ customer fine-tune : exec {f:.2f}   + repair {fr:.2f}")


if __name__ == "__main__":
    main()
