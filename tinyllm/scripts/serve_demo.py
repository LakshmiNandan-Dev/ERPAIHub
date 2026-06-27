#!/usr/bin/env python3
"""End-to-end product loop in one command (no HTTP server):

    question -> retrieve -> constrained-decode -> graph gate -> EXPLAIN
             -> [confirm] -> read-only execute -> rows

Uses a SQLite stand-in DB (synthetic data) so the confirmed-execute step runs
without Oracle. Against a real instance, swap SqliteDb -> tinyllm.db.OracleDb.

    python3 scripts/serve_demo.py --schema synthetic_demo
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tinyllm import generate_example  # noqa: E402
from tinyllm.db import SqliteDb  # noqa: E402
from tinyllm.extract import EbsExtractor, MockCatalog  # noqa: E402
from tinyllm.serve import QueryService  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", default="artifacts/model_best.pt")
    ap.add_argument("--tok", default="artifacts/tokenizer.json")
    ap.add_argument("--schema", default="synthetic_demo")
    ap.add_argument("--question", default=None)
    ap.add_argument("--max-rows", type=int, default=10)
    args = ap.parse_args()

    ex = generate_example(1_000_000, level=2)
    schemas = {
        "synthetic_demo": ex.schema,
        "ebs_ap_gl": EbsExtractor(MockCatalog()).extract(),
    }
    dbs = {sid: SqliteDb(s, seed=7) for sid, s in schemas.items()}
    service = QueryService.from_files(args.ckpt, args.tok, schemas, dbs=dbs)

    question = args.question or ex.question
    print(f"schema   : {args.schema}")
    print(f"question : {question}\n")

    res = service.query(question, args.schema)             # propose (no execution)
    print(f"proposed SQL : {' '.join(res.sql.split())}")
    print(f"tables_used  : {res.tables_used}")
    print(f"graph_valid  : {res.graph_valid}   explain_ok: {res.explain_ok}   "
          f"constrained: {res.constrained}")
    if res.note:
        print(f"note         : {res.note}")

    if res.graph_valid and res.explain_ok:
        print("\n[user confirms] -> executing read-only ...")
        rows = service.execute(args.schema, res.sql, max_rows=args.max_rows)
        print(f"returned {len(rows)} row(s):")
        for r in rows[:args.max_rows]:
            print("  ", r)
    else:
        print("\nnot auto-executed: proposal did not pass both gates (preview only).")


if __name__ == "__main__":
    main()
