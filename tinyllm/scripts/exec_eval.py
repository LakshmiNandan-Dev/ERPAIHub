#!/usr/bin/env python3
"""Execution-accuracy eval on UNSEEN schemas: does the predicted SQL return the
same rows as the gold SQL? (the real text->SQL metric, vs cosmetic exact-match).

    python scripts/exec_eval.py --n 80 --beam 5

Compares greedy decoding vs graph-constrained (picard) decoding. Reports
execution accuracy, exact-match, and the gap between them -- the "equivalent but
not identical" SQL that exact-match unfairly penalizes.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tinyllm import generate_example, serialize_schema  # noqa: E402
from tinyllm.decode import picard_generate  # noqa: E402
from tinyllm.eval import ExecHarness, execution_match  # noqa: E402
from tinyllm.model import collate  # noqa: E402
from tinyllm.tokenizer import BPETokenizer  # noqa: E402
from tinyllm.train import load_model  # noqa: E402
from tinyllm.train.dataset import LEVELS, VAL_OFFSET  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=80)
    ap.add_argument("--beam", type=int, default=5)
    ap.add_argument("--max-len", type=int, default=120)
    ap.add_argument("--ckpt", default="artifacts/model_best.pt")
    ap.add_argument("--tok", default="artifacts/tokenizer.json")
    ap.add_argument("--style", choices=("default", "procedural", "ebs"), default="default",
                    help="match the checkpoint's training name distribution")
    args = ap.parse_args()

    tok = BPETokenizer.load(args.tok)
    model = load_model(args.ckpt, device="cpu")
    bos, eos = tok.special("<bos>"), tok.special("<eos>")

    stats = {m: {"exact": 0, "exec": 0, "exec_not_exact": 0, "pred_fail": 0}
             for m in ("greedy", "picard")}
    gold_ok = 0
    t0 = time.time()

    for i in range(args.n):
        seed = VAL_OFFSET + i
        ex = generate_example(seed, level=LEVELS[i % len(LEVELS)], style=args.style)
        schema_str = serialize_schema(ex.schema)
        harness = ExecHarness(ex.schema, seed=seed)
        gr, gerr = harness.rows(ex.sql)
        gold_ok += int(gr is not None)

        batch = collate([(ex.question, schema_str, ex.sql)], tok, "cpu")
        out = model.generate(batch["src"], batch["src_keep"], bos, eos, max_len=args.max_len)[0].tolist()
        if eos in out:
            out = out[: out.index(eos)]
        preds = {
            "greedy": tok.decode(out[1:]),
            "picard": picard_generate(model, tok, batch["src"], batch["src_keep"],
                                      ex.schema, beam=args.beam, max_len=args.max_len)[0],
        }
        for mode, pred in preds.items():
            exact = pred.strip() == ex.sql.strip()
            m = execution_match(harness, ex.sql, pred)
            stats[mode]["exact"] += int(exact)
            stats[mode]["exec"] += int(m["match"])
            stats[mode]["exec_not_exact"] += int(m["match"] and not exact)
            stats[mode]["pred_fail"] += int(not m["pred_ok"])
        harness.close()

    n = args.n
    print(f"\n{n} unseen-schema examples  (beam={args.beam}, {time.time()-t0:.0f}s)")
    print(f"gold executable: {gold_ok}/{n}\n")
    print(f"{'metric':<22}{'greedy':>10}{'picard':>10}")
    print(f"{'exact_match':<22}{stats['greedy']['exact']/n:>10.3f}{stats['picard']['exact']/n:>10.3f}")
    print(f"{'execution_acc':<22}{stats['greedy']['exec']/n:>10.3f}{stats['picard']['exec']/n:>10.3f}")
    print(f"{'  exec & not exact':<22}{stats['greedy']['exec_not_exact']/n:>10.3f}"
          f"{stats['picard']['exec_not_exact']/n:>10.3f}")
    print(f"{'pred un-runnable':<22}{stats['greedy']['pred_fail']/n:>10.3f}"
          f"{stats['picard']['pred_fail']/n:>10.3f}")
    print("\nexecution_acc is the real metric; 'exec & not exact' = correct SQL "
          "that exact-match wrongly scored 0.")


if __name__ == "__main__":
    main()
