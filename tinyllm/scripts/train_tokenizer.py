#!/usr/bin/env python3
"""Train the from-scratch BPE tokenizer on the data-engine corpus.

    python scripts/train_tokenizer.py --examples 3000 --vocab 4096

Corpus = serialized schema + canonical question + paraphrases + SQL, sampled
across all complexity levels, so EBS identifiers and SQL keywords get efficient
tokens.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tinyllm import generate_example, serialize_schema  # noqa: E402
from tinyllm.tokenizer import BPETokenizer  # noqa: E402


def build_corpus(examples: int, paraphrases: int):
    texts: list[str] = []
    for seed in range(examples):
        level = 1 + (seed % 5)
        ex = generate_example(seed, level=level, n_paraphrases=paraphrases)
        texts.append(serialize_schema(ex.schema))
        texts.append(ex.question)
        texts.extend(ex.paraphrases)
        texts.append(ex.sql)
    return texts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--examples", type=int, default=3000)
    ap.add_argument("--vocab", type=int, default=4096)
    ap.add_argument("--paraphrases", type=int, default=4)
    ap.add_argument("--out", default="artifacts/tokenizer.json")
    args = ap.parse_args()

    print(f"building corpus from {args.examples} examples ...")
    texts = build_corpus(args.examples, args.paraphrases)
    raw_bytes = sum(len(t.encode("utf-8")) for t in texts)
    print(f"corpus: {len(texts):,} texts, {raw_bytes:,} bytes")

    t0 = time.time()
    tok = BPETokenizer().train(texts, vocab_size=args.vocab)
    print(f"trained vocab_size={tok.vocab_size} ({len(tok.merges)} merges) "
          f"in {time.time() - t0:.1f}s")

    tok.save(args.out)
    print(f"saved -> {args.out}")

    # -- stats -----------------------------------------------------------
    total_tokens = sum(len(tok.encode(t)) for t in texts)
    print(f"\ncompression: {raw_bytes / total_tokens:.2f} bytes/token "
          f"({raw_bytes:,} bytes -> {total_tokens:,} tokens)")

    print("specials:", {k: v for k, v in tok.special_tokens.items()})

    longest = sorted(
        (tok.vocab[i] for i in range(256, 256 + len(tok.merges))),
        key=len, reverse=True,
    )
    pretty = []
    for b in longest:
        s = b.decode("utf-8", errors="replace")
        if s.strip() and len(pretty) < 18:
            pretty.append(repr(s))
    print("longest learned tokens:", ", ".join(pretty))

    ex = generate_example(7, level=3, n_paraphrases=0)
    ids = tok.encode(ex.sql)
    print(f"\nsample SQL ({len(ex.sql)} chars -> {len(ids)} tokens):")
    print(" ", ex.sql.replace("\n", " "))
    print("  tokens:", [tok.decode([i]) for i in ids][:40], "...")
    assert tok.decode(ids) == ex.sql, "round-trip mismatch!"
    print("round-trip: OK")


if __name__ == "__main__":
    main()
