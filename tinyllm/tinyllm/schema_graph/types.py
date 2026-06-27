"""Core schema data model.

This is the *common Schema-Graph Interface* surface: the same dataclasses
represent a synthetic schema (vendor-base training) and a real extracted EBS
schema (customer-side). Everything downstream (sampler, renderer, validator)
depends only on these types -- so the schema *source* is fully swappable.

Semantic annotations (roles, business labels, lookup metadata) are first-class
because EBS NL->SQL is impossible without them: a question about "cost center"
maps to a specific flexfield segment column whose meaning is configured, not
derivable from the column name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ColumnType(str, Enum):
    NUMBER = "NUMBER"
    VARCHAR2 = "VARCHAR2"
    DATE = "DATE"


class SemanticRole(str, Enum):
    """EBS-aware roles that drive BOTH SQL sampling and NL phrasing."""

    ID = "id"                          # surrogate / business identifier (often PK/FK)
    AMOUNT = "amount"                  # numeric measure -> SUM / AVG
    QUANTITY = "quantity"              # numeric count -> SUM / COUNT
    DATE = "date"                      # -> date range / EXTRACT(YEAR ...)
    NAME = "name"                      # descriptive label -> group / select
    CODE = "code"                      # short business code
    ORG_ID = "org_id"                  # multi-org striping column on _ALL tables
    LOOKUP = "lookup"                  # coded column resolved via a lookup type
    FLEXFIELD_SEGMENT = "flexfield_segment"  # KFF segmentN with a configured meaning


@dataclass
class Column:
    name: str
    type: ColumnType
    nullable: bool = True
    is_pk: bool = False
    role: Optional[SemanticRole] = None
    business_label: Optional[str] = None       # e.g. "cost center" for segment4
    lookup_type: Optional[str] = None          # for LOOKUP columns
    allowed_values: tuple[str, ...] = ()        # value-set / lookup members

    @property
    def label(self) -> str:
        """Human-facing term used to phrase questions."""
        return self.business_label or self.name.replace("_", " ")


@dataclass
class ForeignKey:
    from_table: str
    from_column: str
    to_table: str
    to_column: str

    def connects(self, table: str) -> bool:
        return table in (self.from_table, self.to_table)

    def other(self, table: str) -> str:
        return self.to_table if table == self.from_table else self.from_table


@dataclass
class Table:
    name: str
    columns: list[Column] = field(default_factory=list)
    is_multi_org: bool = False                  # _ALL org-striped table

    def column(self, name: str) -> Optional[Column]:
        return next((c for c in self.columns if c.name == name), None)

    @property
    def primary_key(self) -> Optional[Column]:
        return next((c for c in self.columns if c.is_pk), None)

    def by_role(self, role: SemanticRole) -> list[Column]:
        return [c for c in self.columns if c.role == role]

    def has_role(self, role: SemanticRole) -> bool:
        return any(c.role == role for c in self.columns)


@dataclass
class Schema:
    name: str
    tables: list[Table] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)

    def table(self, name: str) -> Optional[Table]:
        return next((t for t in self.tables if t.name == name), None)

    @property
    def table_names(self) -> list[str]:
        return [t.name for t in self.tables]
