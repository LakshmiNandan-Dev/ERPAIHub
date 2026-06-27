"""Inference-time schema retrieval (schema linking).

A real EBS instance exposes hundreds of tables; the encoder takes a handful. The
graph -- already our source of join "form" -- doubles as the retriever: match the
question's words to table/column labels (the SEEDS), then keep the FK-connected
module they live in. The result feeds `serialize_schema(schema, tables=...)` (the
socket was always there), reconstructing the small single-module view the model
was trained on.

No model, no embeddings -- a dependency-free, auditable scan + graph walk, so it
runs customer-local and you can see exactly why each table was selected.

    link_tables(question, schema)            -> [table names] for the encoder
    merge_schemas([(prefix, schema), ...])   -> one multi-module catalog (for tests/demos)
"""

from __future__ import annotations

import re

from ..nl.lexicon import entity_label
from ..schema_graph.graph import SchemaGraph
from ..schema_graph.types import ForeignKey, Schema, Table

_WORD = re.compile(r"[a-z0-9]+")

# Words that carry no schema signal: grammar, query verbs/aggregations, filter
# scaffolding, and ubiquitous structural column tokens (every table has them).
_STOP = {
    # grammar
    "the", "of", "by", "per", "for", "each", "with", "and", "or", "is", "are",
    "to", "a", "an", "on", "in", "that", "which", "whose",
    # query verbs / aggregation
    "show", "me", "give", "list", "display", "get", "find", "what", "calculate",
    "breakdown", "total", "sum", "average", "mean", "number", "count", "top",
    "highest", "largest", "most", "rank", "ranking", "above", "below", "over",
    "under", "more", "than", "exceeds", "within", "have", "having", "appear",
    "only", "ones", "where", "group", "broken", "down", "grouped",
    # filter scaffolding
    "operating", "unit", "org", "ou", "fiscal", "year", "during",
    # ubiquitous structural column tokens
    "id", "all", "name", "code", "status", "date",
}


def _toks(s: str) -> set[str]:
    return {w for w in _WORD.findall(s.lower()) if w not in _STOP}


def _table_terms(table: Table) -> set[str]:
    """Distinctive content tokens for a table: cleaned name + column labels."""
    terms = _toks(entity_label(table.name)) | _toks(table.name)
    for c in table.columns:
        terms |= _toks(c.label)
        if c.business_label:
            terms |= _toks(c.business_label)
    return terms


def _phrases(table: Table) -> list[str]:
    """Multi-word labels (e.g. 'cost center', 'paid amount') -- strong evidence."""
    out = []
    for c in table.columns:
        for lbl in (c.label, c.business_label):
            if lbl and " " in lbl:
                out.append(lbl.lower())
    return out


def _score(qtokens: set[str], qstr: str, table: Table) -> int:
    score = len(qtokens & _table_terms(table))
    for phrase in _phrases(table):
        if phrase in qstr:
            score += 2
    return score


def _component(graph: SchemaGraph, start: str) -> set[str]:
    seen, stack = {start}, [start]
    while stack:
        for nb in graph.neighbors(stack.pop()):
            if nb not in seen:
                seen.add(nb)
                stack.append(nb)
    return seen


def link_tables(question: str, schema: Schema, max_component: int = 8) -> list[str]:
    """Pick the relevant tables for `question` out of (a possibly huge) `schema`.

    Seeds = tables whose labels the question mentions; we return the FK-connected
    module around the strongest seed. Small modules are returned whole (they ARE
    the training-shaped view); large modules are narrowed to seeds + connecting
    paths + one FK hop.
    """
    graph = SchemaGraph(schema)
    qstr = question.lower()
    qtokens = _toks(question)
    scored = [(t, _score(qtokens, qstr, t)) for t in schema.tables]
    seeds = [t for t, s in scored if s > 0]
    if not seeds:
        return [t.name for t in schema.tables]      # can't link -> emit all (small schema)

    anchor = max(scored, key=lambda x: x[1])[0]
    comp = _component(graph, anchor.name)
    if len(comp) <= max_component:
        return sorted(comp)                          # one module -> serialize it whole

    # large module: seeds in-component + the FK paths that connect them + 1 hop
    seed_names = {t.name for t in seeds} & comp
    relevant = {anchor.name} | seed_names
    for sn in list(seed_names):
        path = graph.join_path(anchor.name, sn)
        if path:
            for fk in path:
                relevant.update((fk.from_table, fk.to_table))
    for tn in list(relevant):
        relevant.update(nb for nb in graph.neighbors(tn) if nb in comp)
    return sorted(relevant)


def merge_schemas(named: list[tuple[str, Schema]]) -> Schema:
    """Combine schemas into one catalog, prefixing each module's table names
    (columns unchanged) so names stay unique -- simulates a multi-module EBS."""
    tables: list[Table] = []
    fks: list[ForeignKey] = []
    for prefix, sch in named:
        for t in sch.tables:
            tables.append(Table(prefix + t.name, t.columns, t.is_multi_org))
        for fk in sch.foreign_keys:
            fks.append(ForeignKey(prefix + fk.from_table, fk.from_column,
                                  prefix + fk.to_table, fk.to_column))
    return Schema(name="catalog", tables=tables, foreign_keys=fks)
