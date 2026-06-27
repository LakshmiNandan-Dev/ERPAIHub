#!/usr/bin/env python3
"""Generate and inspect synthetic NL->Oracle-SQL examples.

    python scripts/generate_data.py --n 5 --level 2
    python scripts/generate_data.py --n 2000 --quiet      # throughput + valid-rate
    python scripts/generate_data.py --demo-repair         # show graph catching a bad join
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tinyllm import generate_example, serialize_schema  # noqa: E402
from tinyllm.schema_graph import SchemaGraph  # noqa: E402
from tinyllm.validate import validate_graph  # noqa: E402


def _print_example(ex, idx):
    used = ex.schema.table_names
    print(f"\n=== example {idx}  (schema={ex.schema.name}, level=L{ex.level}) ===")
    print("schema   :", serialize_schema(ex.schema, ex.ast.tables))
    print("features :", ", ".join(ex.ebs_features) or "(none)")
    print("question :", ex.question)
    for i, p in enumerate(ex.paraphrases):
        print(f"  para {i} :", p)
    print("sql      :")
    for line in ex.sql.splitlines():
        print("           " + line)
    print("validate :", "  ".join(f"{v.name}={v.status}" for v in ex.validations))
    for v in ex.validations:
        for issue in v.issues:
            print(f"           ! {v.name}: {issue}")


def demo_repair(seed: int = 7):
    """Show the graph validator catching a fabricated join (the inference gate)."""
    ex = generate_example(seed, level=2)
    graph = SchemaGraph(ex.schema)
    print("Valid query:")
    print(ex.sql)
    print("graph validation:", validate_graph(ex.ast, graph).status)

    if ex.ast.joins:
        bad = ex.ast.joins[0]
        good = bad.on[0]
        # corrupt the join: keep the table but point at the wrong (PK) column
        wrong_col = ex.schema.table(good[0].table).primary_key
        from tinyllm.sql_sampler.ast import ColumnRef

        bad.on[0] = (ColumnRef(good[0].table, wrong_col), good[1])
        res = validate_graph(ex.ast, graph)
        print("\nAfter corrupting the join key:")
        print("graph validation:", res.status)
        for issue in res.issues:
            print("  !", issue)
    else:
        print("(seed produced no joins; try another --seed)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--level", type=int, default=2)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--paraphrases", type=int, default=0, help="natural-language variants per example")
    ap.add_argument("--quiet", action="store_true", help="only print throughput + valid rate")
    ap.add_argument("--demo-repair", action="store_true")
    args = ap.parse_args()

    if args.demo_repair:
        demo_repair(args.seed)
        return

    t0 = time.time()
    valid = 0
    feature_counts: dict[str, int] = {}
    for i in range(args.n):
        ex = generate_example(args.seed + i, level=args.level, n_paraphrases=args.paraphrases)
        valid += int(ex.valid)
        for f in ex.ebs_features:
            feature_counts[f] = feature_counts.get(f, 0) + 1
        if not args.quiet and i < 50:
            _print_example(ex, i)

    dt = time.time() - t0
    print(f"\n--- {args.n} examples | valid {valid}/{args.n} "
          f"({100 * valid / args.n:.1f}%) | {args.n / dt:,.0f} ex/s ---")
    if feature_counts:
        print("feature coverage:", ", ".join(
            f"{k}={v}" for k, v in sorted(feature_counts.items())))


if __name__ == "__main__":
    main()
