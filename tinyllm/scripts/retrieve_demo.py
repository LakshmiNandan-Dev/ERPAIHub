#!/usr/bin/env python3
"""Schema retrieval on a big multi-module catalog (what a real EBS looks like).

Builds, per question, a catalog of one relevant module + many distractor modules,
then shows the graph retriever recovers the relevant tables and shrinks the
encoder input from "won't fit" to "training-shaped".

    python3 scripts/retrieve_demo.py --n 40 --distractors 40
    python3 scripts/retrieve_demo.py --n 16 --model      # also run the model on the retrieved view
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tinyllm import generate_example, serialize_schema  # noqa: E402
from tinyllm.retrieve import link_tables, merge_schemas  # noqa: E402
from tinyllm.train.dataset import LEVELS, VAL_OFFSET  # noqa: E402


def gold_tables(ex):
    names = set(ex.ast.tables)
    for p in ex.ast.where:
        sub = getattr(p.value, "query", None)
        if sub is not None:
            names.update(sub.tables)
    return names


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--distractors", type=int, default=40)
    ap.add_argument("--model", action="store_true", help="also run the model on retrieved views")
    ap.add_argument("--max-len", type=int, default=120)
    ap.add_argument("--beam", type=int, default=5)
    args = ap.parse_args()

    distractor_seeds = list(range(2_000_000, 2_000_000 + args.distractors))

    tok = model = None
    if args.model:
        from tinyllm.decode import picard_generate
        from tinyllm.eval import ExecHarness, execution_match
        from tinyllm.model import collate
        from tinyllm.tokenizer import BPETokenizer
        from tinyllm.train import load_model
        tok = BPETokenizer.load("artifacts/tokenizer.json")
        model = load_model("artifacts/model_best.pt", device="cpu")

    recall = exec_ok = 0
    cat_tables = cat_tokens = ret_tables = ret_tokens = 0
    shown = False
    for i in range(args.n):
        seed = VAL_OFFSET + i
        ex = generate_example(seed, level=LEVELS[i % len(LEVELS)])
        named = [("", ex.schema)] + [
            (f"m{j}_", generate_example(s, level=2).schema) for j, s in enumerate(distractor_seeds)
        ]
        catalog = merge_schemas(named)

        picked = link_tables(ex.question, catalog)
        recall += int(gold_tables(ex) <= set(picked))

        full_ser = serialize_schema(catalog)
        ret_ser = serialize_schema(catalog, tables=picked)
        cat_tables += len(catalog.tables)
        ret_tables += len(picked)
        if tok is not None:
            cat_tokens += len(tok.encode(full_ser, allow_special=False))
            ret_tokens += len(tok.encode(ret_ser, allow_special=False))

        if not shown:
            shown = True
            print(f"\nexample: {ex.question}")
            print(f"  catalog has {len(catalog.tables)} tables across {1+args.distractors} modules")
            print(f"  retrieved: {picked}")
            print(f"  gold needs: {sorted(gold_tables(ex))}\n")

        if model is not None:
            batch = collate([(ex.question, ret_ser, ex.sql)], tok, "cpu")
            pred, _ = picard_generate(model, tok, batch["src"], batch["src_keep"],
                                      ex.schema, beam=args.beam, max_len=args.max_len)
            harness = ExecHarness(ex.schema, seed=seed)
            exec_ok += int(execution_match(harness, ex.sql, pred)["match"])
            harness.close()

    n = args.n
    print(f"--- {n} questions, {args.distractors} distractor modules ---")
    print(f"retrieval recall (all gold tables found) : {recall/n:.3f}")
    print(f"avg tables: catalog {cat_tables/n:.0f} -> retrieved {ret_tables/n:.1f}")
    if tok is not None:
        print(f"avg encoder tokens: full {cat_tokens/n:.0f} -> retrieved {ret_tokens/n:.0f} "
              f"(model max_seq_len=512)")
    if model is not None:
        print(f"execution accuracy on retrieved views (picard): {exec_ok/n:.3f}")


if __name__ == "__main__":
    main()
