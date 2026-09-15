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


def link_tables(question: str, schema: Schema, max_component: int = 8,
                max_columns: int = 40) -> list[str]:
    """Pick the relevant tables for `question` out of (a possibly huge) `schema`.

    Seeds = tables whose labels the question mentions; we return the FK-connected
    module around the strongest seed. Small modules are returned whole (they ARE
    the training-shaped view); large modules are narrowed to seeds + connecting
    paths + one FK hop, ranked by score and added under a COLUMN-COUNT budget
    (`max_columns` -- a tokenizer-free proxy for staying inside the encoder's
    context window) so the result never balloons past what the model can take.

    Column count, not table count, bounds the budget: real catalogs have wildly
    uneven table widths (a 6-column lookup table vs. a 194-column transaction
    header), so a pure table-count cap doesn't reliably bound serialized size
    the way a column-count cap does. Verified against a real ~700-table EBS
    module: uncapped, a typical question pulled in over half the catalog
    (~84,000 estimated tokens against a 512-token model); capped, results stay
    in the low tens of tables.

    `max_columns=40` is calibrated against the SHIPPED tokenizer's real output
    (~10 tokens/column measured against `serialize_schema` on real EBS column
    names -- long compound identifiers fragment heavily against the ~2K-token
    dev vocab), targeting comfortably under the 512-token encoder limit with
    room left for the question. A char-count-based guess (~1.3 tok/col) was
    off by ~8x; retune this if the vocab changes. KNOWN LIMITATION even at the
    right calibration: a single very wide real table (`ap_invoices_all` has
    194 columns) can exceed the budget by itself -- the "always keep the first
    table" guard below means such an anchor is never dropped to zero results,
    but its serialization can still overrun 512 tokens alone. Not fixed here;
    would need column-level trimming within a table, which changes what this
    function returns (table names only, today).
    """
    graph = SchemaGraph(schema)
    qstr = question.lower()
    qtokens = _toks(question)
    scored = [(t, _score(qtokens, qstr, t)) for t in schema.tables]
    seeds = [t for t, s in scored if s > 0]
    by_name = {t.name: t for t in schema.tables}

    if not seeds:
        if len(schema.tables) <= max_component:
            return [t.name for t in schema.tables]   # can't link -> emit all (small schema)
        return _budget_cap([t.name for t in schema.tables], by_name, max_columns)

    anchor = max(scored, key=lambda x: x[1])[0]
    comp = _component(graph, anchor.name)
    if len(comp) <= max_component:
        return sorted(comp)                          # one module -> serialize it whole

    # large module: seeds in-component, ranked by score (strongest first),
    # each pulling in its FK path back to the anchor, then light neighbor
    # expansion -- all under the column budget so no single question-shaped
    # slice can exceed what the model was trained to consume
    relevant: list[str] = []
    used_cols = 0

    def try_add(name: str) -> bool:
        nonlocal used_cols
        if name in relevant:
            return True
        cols = len(by_name[name].columns)
        if relevant and used_cols + cols > max_columns:
            return False                              # always allow the very first table
        relevant.append(name)
        used_cols += cols
        return True

    try_add(anchor.name)
    ranked_seeds = [t.name for t, s in sorted(scored, key=lambda x: -x[1])
                    if s > 0 and t.name in comp]
    for sn in ranked_seeds:
        if sn == anchor.name:
            continue
        path = graph.join_path(anchor.name, sn)
        path_tables = [anchor.name, sn]
        if path:
            for fk in path:
                path_tables.extend((fk.from_table, fk.to_table))
        for tn in dict.fromkeys(path_tables):         # de-dup, keep discovery order
            try_add(tn)

    for name in list(relevant):                        # light 1-hop expansion, budget-capped
        for nb in graph.neighbors(name):
            if nb in comp:
                try_add(nb)

    return sorted(relevant)


def _budget_cap(names: list[str], by_name: dict, max_columns: int) -> list[str]:
    """Deterministic fallback for the no-seed / large-schema case: take tables
    in a stable order until the column budget runs out, rather than emitting
    everything (which is fine for a small schema but not a real EBS catalog)."""
    out: list[str] = []
    used = 0
    for name in sorted(names):
        cols = len(by_name[name].columns)
        if out and used + cols > max_columns:
            break
        out.append(name)
        used += cols
    return out


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
