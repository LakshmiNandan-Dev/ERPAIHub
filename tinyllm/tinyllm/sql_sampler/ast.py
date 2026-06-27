"""SQL AST / intermediate representation.

We sample a typed AST, never a string. The AST is the single source of truth we
render two ways -- to Oracle SQL and to a natural-language question -- and that
we validate against the graph. Keeping joins/predicates structured is what lets
the graph guarantee correctness and what will later let the model emit an IR
instead of raw SQL if we choose option (3b).

Complexity ladder supported here:
  L1 single-table   L2 aggregate+group-by   L3 HAVING / top-N
  L4 nested subquery (scalar "above average", IN-semijoin)   L5 window ranking
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from ..schema_graph.types import Column


@dataclass
class ColumnRef:
    table: str          # table NAME; alias resolved at render time via the query map
    column: Column


@dataclass
class Aggregate:
    func: str           # SUM | AVG | COUNT | MAX | MIN
    column: ColumnRef

    @property
    def output_name(self) -> str:
        return f"{self.func.lower()}_{self.column.column.name}"


class Op(str, Enum):
    EQ = "="
    GT = ">"
    GE = ">="
    LE = "<="
    LIKE = "LIKE"
    IN = "IN"
    YEAR_EQ = "YEAR_EQ"   # rendered as EXTRACT(YEAR FROM col) = value


@dataclass
class Predicate:
    column: ColumnRef
    op: Op
    value: object        # int | str | Subquery


@dataclass
class HavingPredicate:
    agg: Aggregate
    op: Op
    value: object        # numeric threshold


@dataclass
class OrderItem:
    expr: object         # ColumnRef | Aggregate
    descending: bool = False


@dataclass
class WindowFunc:
    func: str            # RANK | ROW_NUMBER | DENSE_RANK
    partition_by: list[ColumnRef] = field(default_factory=list)
    order_by: list[OrderItem] = field(default_factory=list)
    output_name: str = "rnk"


@dataclass
class JoinClause:
    new_table: str                          # table introduced by this join
    on: list[tuple[ColumnRef, ColumnRef]]   # equality join conditions


@dataclass
class SelectQuery:
    from_table: str
    aliases: dict[str, str]                 # table name -> alias
    select: list                            # ColumnRef | Aggregate | WindowFunc
    joins: list[JoinClause] = field(default_factory=list)
    where: list[Predicate] = field(default_factory=list)
    group_by: list[ColumnRef] = field(default_factory=list)
    having: list[HavingPredicate] = field(default_factory=list)
    order_by: list[OrderItem] = field(default_factory=list)
    limit: Optional[int] = None

    @property
    def tables(self) -> list[str]:
        return [self.from_table] + [j.new_table for j in self.joins]


@dataclass
class Subquery:
    """A nested, non-correlated SELECT used as a predicate value."""

    query: SelectQuery
