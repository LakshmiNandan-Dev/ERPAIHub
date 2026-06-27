#!/usr/bin/env python3
"""Extract a (mock) Oracle EBS catalog into a Schema, then run the WHOLE TinyLLM
pipeline on it -- proving an extracted real-EBS-shaped schema is a drop-in for
the synthetic ones (graph joins, sampler, retrieval, and the trained model).

    python3 scripts/extract_demo.py
    python3 scripts/extract_demo.py --model        # also run the trained model + exec check
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tinyllm import serialize_schema  # noqa: E402
from tinyllm.extract import EbsExtractor, MockCatalog  # noqa: E402
from tinyllm.nl import render_question  # noqa: E402
from tinyllm.render import render_oracle  # noqa: E402
from tinyllm.retrieve import link_tables  # noqa: E402
from tinyllm.schema_graph import SchemaGraph  # noqa: E402
from tinyllm.sql_sampler import QuerySampler  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=6)
    ap.add_argument("--model", action="store_true")
    ap.add_argument("--beam", type=int, default=5)
    ap.add_argument("--ckpt", default="artifacts/model_best.pt")
    ap.add_argument("--tok", default="artifacts/tokenizer.json")
    args = ap.parse_args()

    schema = EbsExtractor(MockCatalog()).extract()
    graph = SchemaGraph(schema)

    print("=== extracted EBS schema ===")
    for line in serialize_schema(schema).split(" | "):
        print("  " + line)
    print(f"\nforeign keys: {len(schema.foreign_keys)} (the mock catalog declared 0 — all inferred)")
    for fk in schema.foreign_keys:
        print(f"  {fk.from_table}.{fk.from_column} -> {fk.to_table}.{fk.to_column}")
    path = graph.join_path("ap_invoice_lines_all", "gl_code_combinations")
    print(f"\njoin path lines -> code_combinations: "
          f"{[f'{fk.from_table}->{fk.to_table}' for fk in path]}")

    model = tok = None
    if args.model:
        from tinyllm.decode import picard_generate
        from tinyllm.eval import ExecHarness, execution_match
        from tinyllm.model import collate
        from tinyllm.tokenizer import BPETokenizer
        from tinyllm.train import load_model
        tok = BPETokenizer.load(args.tok)
        model = load_model(args.ckpt, device="cpu")

    print("\n=== questions generated over the extracted schema ===")
    exec_ok = 0
    for seed in range(args.n):
        rng = random.Random(seed)
        ast, _ = QuerySampler(graph, rng).sample(1 + seed % 3)
        gold = render_oracle(ast)
        question = render_question(ast)
        picked = link_tables(question, schema)
        print(f"\nQ: {question}")
        print(f"   retrieved: {picked}")
        print(f"   gold: {' '.join(gold.split())}")
        if model is not None:
            sub = serialize_schema(schema, tables=picked)
            batch = collate([(question, sub, gold)], tok, "cpu")
            pred, _ = picard_generate(model, tok, batch["src"], batch["src_keep"],
                                      schema, beam=args.beam, max_len=120)
            harness = ExecHarness(schema, seed=seed)
            ok = execution_match(harness, gold, pred)["match"]
            harness.close()
            exec_ok += int(ok)
            print(f"   pred: {' '.join(pred.split())}")
            print(f"   exec-match: {ok}")

    if model is not None:
        print(f"\nexecution accuracy on extracted EBS schema: {exec_ok}/{args.n}")


if __name__ == "__main__":
    main()
