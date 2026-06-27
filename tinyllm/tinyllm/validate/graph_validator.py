"""Structural validation against the schema graph -- dependency-free.

This is the same check that runs at INFERENCE time as the first gate: it proves
the SQL is well-formed *against the schema* -- tables/columns exist and every
join condition corresponds to a real FK edge. It does NOT judge whether the
query answers the question (that's semantics, not structure).

Validation is recursive: each (sub)query is checked against its OWN table scope,
so non-correlated subqueries are validated independently.
"""

from __future__ import annotations

from ..schema_graph.graph import SchemaGraph
from ..sql_sampler.ast import (
    Aggregate,
    ColumnRef,
    OrderItem,
    SelectQuery,
    Subquery,
    WindowFunc,
)
from .result import ValidationResult


def validate_graph(query: SelectQuery, graph: SchemaGraph) -> ValidationResult:
    issues: list[str] = []
    _validate_scope(query, graph, issues)
    return ValidationResult("graph", ok=(len(issues) == 0), issues=issues)


def _validate_scope(query: SelectQuery, graph: SchemaGraph, issues: list[str]):
    schema = graph.schema
    present = set(query.tables)

    for tname in query.tables:
        if schema.table(tname) is None:
            issues.append(f"unknown table: {tname}")

    for ref in _scope_column_refs(query):
        t = schema.table(ref.table)
        if t is None:
            issues.append(f"column {ref.column.name} references unknown table {ref.table}")
        elif t.column(ref.column.name) is None:
            issues.append(f"unknown column: {ref.table}.{ref.column.name}")
        elif ref.table not in present:
            issues.append(f"column {ref.table}.{ref.column.name} not in FROM/JOIN scope")

    for j in query.joins:
        for left, right in j.on:
            fk = graph.fk_between(left.table, right.table)
            if fk is None:
                issues.append(f"join {left.table}<->{right.table} has no FK edge in the graph")
            elif not _matches_fk(fk, left, right):
                issues.append(
                    f"join {left.table}.{left.column.name}={right.table}.{right.column.name} "
                    f"is not the documented FK key"
                )

    for sub in _subqueries(query):
        _validate_scope(sub.query, graph, issues)


def _matches_fk(fk, left: ColumnRef, right: ColumnRef) -> bool:
    pairs = {
        (fk.from_table, fk.from_column, fk.to_table, fk.to_column),
        (fk.to_table, fk.to_column, fk.from_table, fk.from_column),
    }
    return (left.table, left.column.name, right.table, right.column.name) in pairs


def _order_refs(o: OrderItem):
    yield o.expr.column if isinstance(o.expr, Aggregate) else o.expr


def _scope_column_refs(query: SelectQuery):
    """Column refs belonging to THIS scope (subquery internals validated separately)."""
    for item in query.select:
        if isinstance(item, Aggregate):
            yield item.column
        elif isinstance(item, WindowFunc):
            yield from item.partition_by
            for o in item.order_by:
                yield from _order_refs(o)
        else:
            yield item
    for j in query.joins:
        for left, right in j.on:
            yield left
            yield right
    for p in query.where:
        yield p.column                       # left side is always a column in this scope
    for c in query.group_by:
        yield c
    for h in query.having:
        yield h.agg.column
    for o in query.order_by:
        yield from _order_refs(o)


def _subqueries(query: SelectQuery):
    for p in query.where:
        if isinstance(p.value, Subquery):
            yield p.value
