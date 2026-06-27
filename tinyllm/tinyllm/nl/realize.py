"""Structure-aware NL realizer: AST -> diverse natural-language questions.

Generates *from the AST* (not by rewriting the canonical string), sampling a
frame + lexical choices per query shape. Because we only vary surface form,
every paraphrase is meaning-preserving -- the gold SQL never changes.

    paraphrase(ast, rng)     -> one natural question
    paraphrases(ast, k, rng) -> up to k distinct questions
"""

from __future__ import annotations

import random

from ..sql_sampler.ast import (
    Aggregate,
    ColumnRef,
    Op,
    Predicate,
    SelectQuery,
    Subquery,
    WindowFunc,
)
from . import lexicon as lx


def paraphrase(ast: SelectQuery, rng: random.Random) -> str:
    window = next((it for it in ast.select if isinstance(it, WindowFunc)), None)
    if window is not None:
        return _window(ast, window, rng)
    sub_pred = next((p for p in ast.where if isinstance(p.value, Subquery)), None)
    if sub_pred is not None:
        return _subquery(ast, sub_pred, rng)
    agg = next((it for it in ast.select if isinstance(it, Aggregate)), None)
    if agg is not None:
        return _aggregate(ast, agg, rng)
    return _list(ast, rng)


def paraphrases(ast: SelectQuery, k: int, rng: random.Random) -> list[str]:
    seen: list[str] = []
    attempts = 0
    while len(seen) < k and attempts < k * 6:
        cand = paraphrase(ast, rng)
        if cand not in seen:
            seen.append(cand)
        attempts += 1
    return seen


# -- per-shape realizers ---------------------------------------------------
def _aggregate(q: SelectQuery, agg: Aggregate, rng: random.Random) -> str:
    if q.limit and q.order_by and q.group_by:
        g = " and ".join(lx.group_label(c.column.label) for c in q.group_by)
        base = rng.choice(lx.TOPN).format(n=q.limit, g=lx.pluralize(g), m=_topn_measure(agg, q))
    elif q.group_by:
        base = rng.choice(lx.AGG_FRAMES).format(
            measure=_measure(agg, q, rng), group=_group(q, rng)
        )
    else:
        base = rng.choice(lx.NOGROUP).format(measure=_measure(agg, q, rng))

    if q.having:
        h = q.having[0]
        base += " " + rng.choice(lx.HAVING).format(m2=h.agg.column.column.label, v=h.value)
    return _clean(base + _filters(q.where, rng))


def _window(q: SelectQuery, win: WindowFunc, rng: random.Random) -> str:
    m = win.order_by[0].expr.column.label if win.order_by else "value"
    d = lx.pluralize(lx.entity_label(q.from_table))
    if win.partition_by:
        p = win.partition_by[0].column.label
        return _clean(rng.choice(lx.WINDOW).format(d=d, m=m, p=p))
    return _clean(f"rank {d} by {m}")


def _subquery(q: SelectQuery, pred: Predicate, rng: random.Random) -> str:
    if pred.op == Op.IN:  # semijoin
        dim = lx.pluralize(lx.entity_label(q.from_table))
        doc = lx.pluralize(lx.entity_label(pred.value.query.from_table))
        base = rng.choice(lx.SEMIJOIN).format(dim=dim, doc=doc)
        return _clean(base + _filters(pred.value.query.where, rng))
    d = lx.pluralize(lx.entity_label(q.from_table))     # above average
    return _clean(rng.choice(lx.ABOVE_AVG).format(d=d, m=pred.column.column.label))


def _list(q: SelectQuery, rng: random.Random) -> str:
    cols = ", ".join(c.column.label for c in q.select if isinstance(c, ColumnRef))
    t = lx.pluralize(lx.entity_label(q.from_table))
    base = f"{rng.choice(lx.LIST_VERBS)} {cols} for {t}"
    return _clean(base + _filters(q.where, rng))


# -- shared pieces ---------------------------------------------------------
def _measure(agg: Aggregate, q: SelectQuery, rng: random.Random) -> str:
    if agg.func == "COUNT":
        return rng.choice(lx.COUNT_MEASURE).format(d=lx.pluralize(lx.entity_label(q.from_table)))
    frames = lx.MEASURE.get(agg.func, ["{m}"])
    return rng.choice(frames).format(m=agg.column.column.label)


def _topn_measure(agg: Aggregate, q: SelectQuery) -> str:
    if agg.func == "COUNT":
        return lx.pluralize(lx.entity_label(q.from_table))
    return agg.column.column.label


def _group(q: SelectQuery, rng: random.Random) -> str:
    g = " and ".join(lx.group_label(c.column.label) for c in q.group_by)
    return rng.choice(lx.GROUP).format(g=g)


def _filters(where: list[Predicate], rng: random.Random) -> str:
    out = ""
    for p in where:
        if isinstance(p.value, Subquery):
            continue
        label = p.column.column.label
        if p.op == Op.YEAR_EQ:
            out += " " + rng.choice(lx.DATE).format(y=p.value)
        elif label == "org id":
            out += " " + rng.choice(lx.ORG).format(o=p.value)
        elif isinstance(p.value, str):
            out += " " + rng.choice(lx.LOOKUP).format(label=label, v=p.value.lower())
        else:
            out += f" where {label} {p.op.value} {p.value}"
    return out


def _clean(s: str) -> str:
    s = " ".join(s.split())
    if "?" in s:  # an interrogative frame may have a filter appended after its '?'
        s = s.replace("?", "").rstrip() + "?"
    return s
