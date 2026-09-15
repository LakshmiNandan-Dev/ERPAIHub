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
from ..retrieve import link_tables
from ..sql_sampler.ast import Aggregate, ColumnRef, WindowFunc

LEVELS = (1, 2, 3, 4, 5)
VAL_OFFSET = 1_000_000          # keep val seeds far from train -> disjoint schemas
_CONTEXT_COLUMNS = 12            # cap for tables pulled in as context only (not gold)


def _gold_tables(ast) -> set[str]:
    """Tables the gold AST actually references, including WHERE subqueries."""
    names = set(ast.tables)
    for p in ast.where:
        sub = getattr(p.value, "query", None)
        if sub is not None:
            names.update(sub.tables)
    return names


def _visit_expr(expr, out: set[tuple[str, str]]) -> None:
    if isinstance(expr, ColumnRef):
        out.add((expr.table, expr.column.name))
    elif isinstance(expr, Aggregate):
        _visit_expr(expr.column, out)
    elif isinstance(expr, WindowFunc):
        for c in expr.partition_by:
            _visit_expr(c, out)
        for oi in expr.order_by:
            _visit_expr(oi.expr, out)


def _referenced_columns(ast) -> dict[str, set[str]]:
    """(table -> {column names}) the AST actually touches anywhere -- SELECT,
    JOIN ON, WHERE (incl. subqueries), GROUP BY, HAVING, ORDER BY. Real (wide)
    tables can't be serialized in full within the encoder's context window, so
    gold tables get trimmed down to just this instead of every column."""
    pairs: set[tuple[str, str]] = set()

    def visit(q):
        for e in q.select:
            _visit_expr(e, pairs)
        for j in q.joins:
            for left, right in j.on:
                _visit_expr(left, pairs)
                _visit_expr(right, pairs)
        for p in q.where:
            _visit_expr(p.column, pairs)
            sub = getattr(p.value, "query", None)
            if sub is not None:
                visit(sub)
        for c in q.group_by:
            _visit_expr(c, pairs)
        for h in q.having:
            _visit_expr(h.agg, pairs)
        for oi in q.order_by:
            _visit_expr(oi.expr, pairs)

    visit(ast)
    by_table: dict[str, set[str]] = {}
    for table, col in pairs:
        by_table.setdefault(table, set()).add(col)
    return by_table


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
    specializes to their tables/columns/flexfield labels.

    Serializes only the RETRIEVED subset per question (`link_tables`), not the
    whole schema -- dumping a real extracted catalog's full table set into every
    example is fine at demo scale but breaks down on a real customer instance
    (verified: a single example ran ~10MB against a ~22K-table extraction, and
    even a single ~700-table module produced tens of thousands of tokens against
    a 512-token model). The AST's own gold tables are always unioned in on top
    of whatever `link_tables` retrieves, so training data stays correct even
    where retrieval's heuristic ranking picks a less-central table alongside
    (or instead of) the right one -- the model still always sees what it needs
    to answer the query, plus realistic surrounding noise matching what it'll
    actually see from retrieval at inference time.

    Each table is ALSO trimmed to a relevant column subset, not serialized in
    full: a gold table keeps just the columns the AST actually references
    (`_referenced_columns`, plus PK/FK always, via `serialize_schema`), and a
    context-only table (retrieved but not gold) is capped at the first
    `_CONTEXT_COLUMNS`. Column count, not table count, is what real (wide) EBS
    tables blow past -- a table-level cap alone still leaves e.g. a 194-column
    transaction header far over the model's context window on its own."""
    pairs: list[tuple[str, str, str]] = []
    for i in range(n):
        s = seed_base + i
        ex = example_from_schema(schema, random.Random(s), level=LEVELS[i % len(LEVELS)],
                                 n_paraphrases=paraphrases, para_rng=random.Random(s ^ 0x9E3779B9))
        gold = _gold_tables(ex.ast)
        gold_cols = _referenced_columns(ex.ast)
        by_name = {t.name: t for t in schema.tables}

        def schema_for(question: str) -> str:
            relevant = set(link_tables(question, schema)) | gold
            columns = {
                name: gold_cols[name] if name in gold_cols else
                {c.name for c in by_name[name].columns[:_CONTEXT_COLUMNS]}
                for name in relevant
            }
            return serialize_schema(schema, tables=sorted(relevant), columns=columns)

        if canonical_only:
            pairs.append((ex.question, schema_for(ex.question), ex.sql))
        else:
            for question, sql in ex.training_pairs():
                pairs.append((question, schema_for(question), sql))
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
