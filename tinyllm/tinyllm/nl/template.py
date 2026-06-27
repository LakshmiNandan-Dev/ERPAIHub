"""AST -> canonical natural-language question.

This is the *first* (template) layer only -- deliberately literal. Rule-based
paraphrase and (vendor-side, no customer data) LLM paraphrase stack on top later
to add naturalness and variety. Business labels (flexfield meanings, etc.) are
used so questions read in the user's vocabulary, not raw column names.
"""

from __future__ import annotations

from ..sql_sampler.ast import (
    Aggregate,
    ColumnRef,
    Op,
    Predicate,
    SelectQuery,
    Subquery,
    WindowFunc,
)
from .lexicon import entity_label, pluralize

_AGG_WORD = {"SUM": "total", "AVG": "average", "MAX": "highest", "MIN": "lowest"}


def _measure(agg: Aggregate, from_table: str) -> str:
    if agg.func == "COUNT":
        return "number of " + pluralize(entity_label(from_table))
    return f"{_AGG_WORD.get(agg.func, 'total')} {agg.column.column.label}"


def render_question(q: SelectQuery) -> str:
    window = next((it for it in q.select if isinstance(it, WindowFunc)), None)
    if window is not None:
        return _window_question(q, window)

    sub_pred = next((p for p in q.where if isinstance(p.value, Subquery)), None)
    if sub_pred is not None:
        return _subquery_question(q, sub_pred)

    agg = next((it for it in q.select if isinstance(it, Aggregate)), None)
    if agg is not None:
        return _aggregate_question(q, agg)
    return _list_question(q)


def _aggregate_question(q: SelectQuery, agg: Aggregate) -> str:
    mphrase = _measure(agg, q.from_table)
    group = " and ".join(c.column.label for c in q.group_by) if q.group_by else None

    if q.limit and q.order_by:
        text = f"top {q.limit} {group} by {mphrase}" if group else f"top {q.limit} by {mphrase}"
    elif group:
        text = f"{mphrase} by {group}"
    else:
        text = mphrase

    if q.having:
        h = q.having[0]
        text += f" with {_measure(h.agg, q.from_table)} over {h.value}"
    return text + _filters_phrase(q.where)


def _window_question(q: SelectQuery, win: WindowFunc) -> str:
    measure = win.order_by[0].expr.column.label if win.order_by else "value"
    text = f"rank {_humanize(q.from_table)} by {measure}"
    if win.partition_by:
        text += f" within each {win.partition_by[0].column.label}"
    return text


def _subquery_question(q: SelectQuery, pred: Predicate) -> str:
    if pred.op == Op.IN:  # semijoin
        doc = _humanize(pred.value.query.from_table)
        text = f"list {_humanize(q.from_table)} that have {doc}"
        return text + _filters_phrase(pred.value.query.where)
    # scalar "above average"
    return f"list {_humanize(q.from_table)} above the average {pred.column.column.label}"


def _list_question(q: SelectQuery) -> str:
    cols = ", ".join(c.column.label for c in q.select if isinstance(c, ColumnRef))
    return f"list {cols} from {_humanize(q.from_table)}" + _filters_phrase(q.where)


def _filters_phrase(where: list[Predicate]) -> str:
    out = ""
    for p in where:
        if isinstance(p.value, Subquery):
            continue
        label = p.column.column.label
        if p.op == Op.YEAR_EQ:
            out += f" in {p.value}"
        elif label == "org id":
            out += f" for operating unit {p.value}"
        elif isinstance(p.value, str):
            out += f" where {label} is {p.value.lower()}"
        else:
            out += f" where {label} {p.op.value} {p.value}"
    return out


def _humanize(name: str) -> str:
    return name.replace("_", " ")
