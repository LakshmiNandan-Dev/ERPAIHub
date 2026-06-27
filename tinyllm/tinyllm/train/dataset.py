"""Build train / val splits on DISJOINT schema seeds (Spider-style).

Each seed produces a different synthetic schema, so disjoint seed ranges =>
disjoint schemas. Val therefore measures generalization to schemas never seen
in training -- the whole commercial premise.

Training pairs include paraphrases (more NL variety); val uses the canonical
question only, one per example, for a clean, stable metric.
"""

from __future__ import annotations

import random

from .. import example_from_schema, generate_example, serialize_schema

LEVELS = (1, 2, 3, 4, 5)
VAL_OFFSET = 1_000_000          # keep val seeds far from train -> disjoint schemas


def build_pairs(seeds, paraphrases=0, canonical_only=False, style="default"):
    pairs: list[tuple[str, str, str]] = []
    for i, seed in enumerate(seeds):
        ex = generate_example(seed, level=LEVELS[i % len(LEVELS)],
                              n_paraphrases=paraphrases, style=style)
        schema_str = serialize_schema(ex.schema)
        if canonical_only:
            pairs.append((ex.question, schema_str, ex.sql))
        else:
            for question, sql in ex.training_pairs():
                pairs.append((question, schema_str, sql))
    return pairs


def make_split(n_train: int, n_val: int, paraphrases: int = 2, style: str = "default"):
    train = build_pairs(range(n_train), paraphrases=paraphrases, style=style)
    val = build_pairs(range(VAL_OFFSET, VAL_OFFSET + n_val),
                      canonical_only=True, style=style)
    return train, val


def corpus_texts(pairs):
    """Flatten pairs into texts for tokenizer training (train split only)."""
    texts: list[str] = []
    for question, schema_str, sql in pairs:
        texts.extend((question, schema_str, sql))
    return texts


# -- customer-local: train over the customer's OWN extracted schema(s) ------
def build_pairs_over_schema(schema, n, paraphrases=0, seed_base=0, canonical_only=False):
    """Sample n queries over a FIXED schema (the customer's extracted catalog).
    The split is at the QUERY level here, not the schema level -- the model
    specializes to their tables/columns/flexfield labels."""
    pairs: list[tuple[str, str, str]] = []
    schema_str = serialize_schema(schema)
    for i in range(n):
        s = seed_base + i
        ex = example_from_schema(schema, random.Random(s), level=LEVELS[i % len(LEVELS)],
                                 n_paraphrases=paraphrases, para_rng=random.Random(s ^ 0x9E3779B9))
        if canonical_only:
            pairs.append((ex.question, schema_str, ex.sql))
        else:
            for question, sql in ex.training_pairs():
                pairs.append((question, schema_str, sql))
    return pairs


def make_local_split(schemas, n_train: int, n_val: int, paraphrases: int = 2):
    """Customer-local split: many queries over the extracted schema(s). Train and
    val draw DISJOINT query streams over the SAME schema(s) (held-out queries)."""
    if not isinstance(schemas, (list, tuple)):
        schemas = [schemas]
    train: list = []
    val: list = []
    for schema in schemas:
        train += build_pairs_over_schema(schema, n_train, paraphrases=paraphrases, seed_base=0)
        val += build_pairs_over_schema(schema, n_val, seed_base=VAL_OFFSET, canonical_only=True)
    return train, val
