#!/usr/bin/env python3
"""Greedy vs graph-constrained decoding on UNSEEN schemas.

Loads the trained checkpoint and compares plain greedy decoding against
beam-search + graph verification, reporting valid-SQL / graph-valid / exact-match
for each. The lift is the "structure compensates for a tiny model" payoff.

    python scripts/constrained_demo.py --n 48 --beam 5
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tinyllm import generate_example, serialize_schema  # noqa: E402
from tinyllm.decode import constrained_generate, graph_check_sql, picard_generate  # noqa: E402
from tinyllm.model import collate  # noqa: E402
from tinyllm.schema_graph import SchemaGraph  # noqa: E402
from tinyllm.tokenizer import BPETokenizer  # noqa: E402
from tinyllm.train import load_model  # noqa: E402
from tinyllm.train.dataset import LEVELS, VAL_OFFSET  # noqa: E402
from tinyllm.validate import validate_sqlglot  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=48)
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

    # unseen-schema val examples (with Schema objects for graph checks)
    val = []
    for i in range(args.n):
        ex = generate_example(VAL_OFFSET + i, level=LEVELS[i % len(LEVELS)], style=args.style)
        val.append((ex.question, serialize_schema(ex.schema), ex.sql, ex.schema))

    g = {"valid": 0, "graph": 0, "exact": 0}
    c = {"valid": 0, "graph": 0, "exact": 0, "fallback": 0}
    p = {"valid": 0, "graph": 0, "exact": 0, "fallback": 0}
    t0 = time.time()
    for q, schema_str, gold, schema in val:
        batch = collate([(q, schema_str, gold)], tok, "cpu")
        graph = SchemaGraph(schema)

        # greedy
        out = model.generate(batch["src"], batch["src_keep"], bos, eos, max_len=args.max_len)[0].tolist()
        if eos in out:
            out = out[: out.index(eos)]
        pred_g = tok.decode(out[1:])
        g["valid"] += int(validate_sqlglot(pred_g).ok is True)
        g["graph"] += int(graph_check_sql(pred_g, graph).ok is True)
        g["exact"] += int(pred_g.strip() == gold.strip())

        # constrained (generate-then-verify)
        pred_c, used = constrained_generate(model, tok, batch["src"], batch["src_keep"],
                                            schema, beam=args.beam, max_len=args.max_len)
        c["valid"] += int(validate_sqlglot(pred_c).ok is True)
        c["graph"] += int(graph_check_sql(pred_c, graph).ok is True)
        c["exact"] += int(pred_c.strip() == gold.strip())
        c["fallback"] += int(not used)

        # picard (incremental gating during the search)
        pred_p, used_p = picard_generate(model, tok, batch["src"], batch["src_keep"],
                                         schema, beam=args.beam, max_len=args.max_len)
        p["valid"] += int(validate_sqlglot(pred_p).ok is True)
        p["graph"] += int(graph_check_sql(pred_p, graph).ok is True)
        p["exact"] += int(pred_p.strip() == gold.strip())
        p["fallback"] += int(not used_p)

    n = len(val)
    print(f"\n{n} unseen-schema examples  (beam={args.beam}, {time.time()-t0:.0f}s)\n")
    print(f"{'metric':<12}{'greedy':>10}{'verify':>10}{'picard':>10}")
    for k in ("valid", "graph", "exact"):
        print(f"{k:<12}{g[k]/n:>10.3f}{c[k]/n:>10.3f}{p[k]/n:>10.3f}")
    print(f"\nfallback (no fully graph-valid candidate):  "
          f"verify {c['fallback']}/{n} ({c['fallback']/n:.0%})   "
          f"picard {p['fallback']}/{n} ({p['fallback']/n:.0%})")


if __name__ == "__main__":
    main()
