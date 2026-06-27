"""AST -> Oracle SQL.

Targets the conventions we settled on: unqualified object names (resolved via
APPS synonyms in a real EBS instance), ANSI joins, Oracle date handling via
EXTRACT, analytic functions via OVER(), and top-N via FETCH FIRST. Subqueries
are rendered inline so the surrounding clause stays readable.
"""

from __future__ import annotations

from ..schema_graph.types import ColumnType
from ..sql_sampler.ast import (
    Aggregate,
    ColumnRef,
    HavingPredicate,
    Op,
    OrderItem,
    Predicate,
    SelectQuery,
    Subquery,
    WindowFunc,
)


def render_oracle(q: SelectQuery) -> str:
    return _render(q, inline=False)


def _render(q: SelectQuery, inline: bool) -> str:
    a = q.aliases
    parts = [
        "SELECT " + ", ".join(_select_item(it, a) for it in q.select),
        f"FROM {q.from_table} {a[q.from_table]}",
    ]
    for j in q.joins:
        conds = " AND ".join(f"{_ref(l, a)} = {_ref(r, a)}" for l, r in j.on)
        parts.append(f"JOIN {j.new_table} {a[j.new_table]} ON {conds}")
    if q.where:
        joiner = " AND " if inline else "\n  AND "
        parts.append("WHERE " + joiner.join(_pred(p, a) for p in q.where))
    if q.group_by:
        parts.append("GROUP BY " + ", ".join(_ref(c, a) for c in q.group_by))
    if q.having:
        parts.append("HAVING " + " AND ".join(_having(h, a) for h in q.having))
    if q.order_by:
        parts.append("ORDER BY " + ", ".join(_order(o, a) for o in q.order_by))
    if q.limit is not None:
        parts.append(f"FETCH FIRST {q.limit} ROWS ONLY")
    return (" " if inline else "\n").join(parts)


def _select_item(item, a) -> str:
    if isinstance(item, Aggregate):
        return f"{item.func}({_ref(item.column, a)}) AS {item.output_name}"
    if isinstance(item, WindowFunc):
        clauses = []
        if item.partition_by:
            clauses.append("PARTITION BY " + ", ".join(_ref(c, a) for c in item.partition_by))
        if item.order_by:
            clauses.append("ORDER BY " + ", ".join(_order(o, a) for o in item.order_by))
        return f"{item.func}() OVER ({' '.join(clauses)}) AS {item.output_name}"
    return _ref(item, a)


def _ref(ref: ColumnRef, a) -> str:
    return f"{a[ref.table]}.{ref.column.name}"


def _order(o: OrderItem, a) -> str:
    if isinstance(o.expr, Aggregate):
        s = f"{o.expr.func}({_ref(o.expr.column, a)})"
    else:
        s = _ref(o.expr, a)
    return s + (" DESC" if o.descending else "")


def _having(h: HavingPredicate, a) -> str:
    return f"{h.agg.func}({_ref(h.agg.column, a)}) {h.op.value} {h.value}"


def _pred(p: Predicate, a) -> str:
    col = _ref(p.column, a)
    if isinstance(p.value, Subquery):
        sub = "(" + _render(p.value.query, inline=True) + ")"
        return f"{col} IN {sub}" if p.op == Op.IN else f"{col} {p.op.value} {sub}"
    val = _literal(p.value, p.column.column.type)
    if p.op == Op.YEAR_EQ:
        return f"EXTRACT(YEAR FROM {col}) = {val}"
    return f"{col} {p.op.value} {val}"


def _literal(value, col_type: ColumnType) -> str:
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return str(value)
