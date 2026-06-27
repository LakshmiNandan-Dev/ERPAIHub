"""Schema retrieval: from a big multi-module catalog, link_tables must recall
every table the gold query needs while dropping the irrelevant modules."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tinyllm import generate_example  # noqa: E402
from tinyllm.retrieve import link_tables, merge_schemas  # noqa: E402


def _gold_tables(ex):
    """Table names the gold AST actually references (incl. subqueries)."""
    names = set(ex.ast.tables)
    for p in ex.ast.where:
        sub = getattr(p.value, "query", None)
        if sub is not None:
            names.update(sub.tables)
    return names


def _catalog_around(target_ex, distractor_seeds):
    """target module (unprefixed) + distractor modules (prefixed, so no collision)."""
    named = [("", target_ex.schema)]
    for i, s in enumerate(distractor_seeds):
        named.append((f"m{i}_", generate_example(s, level=2).schema))
    return merge_schemas(named)


def test_retrieval_recall_and_compression():
    distractors = list(range(2_000_000, 2_000_040))     # 40 unrelated modules
    total_recall = total = 0
    sizes = []
    for i in range(40):
        ex = generate_example(1_000_000 + i, level=1 + i % 5)
        catalog = _catalog_around(ex, distractors)
        picked = set(link_tables(ex.question, catalog))
        gold = _gold_tables(ex)
        total_recall += int(gold <= picked)            # all gold tables retrieved?
        total += 1
        sizes.append(len(picked))
    recall = total_recall / total
    avg_size = sum(sizes) / len(sizes)
    # strong recall, and we serialize a handful of tables out of ~200
    assert recall >= 0.9, recall
    assert avg_size <= 12, avg_size


def test_retrieval_drops_irrelevant_modules():
    ex = generate_example(1_000_003, level=2)
    catalog = _catalog_around(ex, range(2_000_100, 2_000_130))
    picked = set(link_tables(ex.question, catalog))
    assert _gold_tables(ex) <= picked
    assert len(picked) < len(catalog.tables) / 4        # massive compression
    # nothing from a distractor module (all prefixed m*) should sneak in
    assert not any(t.startswith("m") and "_" in t for t in picked)


def test_no_seed_fallback_returns_all():
    ex = generate_example(5, level=1)
    picked = link_tables("xyzzy qux", ex.schema)        # no schema words -> emit all
    assert set(picked) == set(ex.schema.table_names)


def test_merge_schemas_unique_and_valid():
    a = generate_example(1, level=2).schema
    b = generate_example(2, level=2).schema
    cat = merge_schemas([("a_", a), ("b_", b)])
    assert len(cat.tables) == len(a.tables) + len(b.tables)
    assert len(cat.table_names) == len(set(cat.table_names))   # no collisions
    for fk in cat.foreign_keys:                                # FK refs resolve
        assert cat.table(fk.from_table) and cat.table(fk.to_table)
