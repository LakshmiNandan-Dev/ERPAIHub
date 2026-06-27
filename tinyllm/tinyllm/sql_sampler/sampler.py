"""QuerySampler -- walk the graph to build join-correct query ASTs.

Complexity ladder (start with L1/L2; L3+ added later):
  L1  single-table select with optional filter
  L2  aggregate + GROUP BY, optionally joined to a dimension for the group label

The sampler chooses *intent* (which measure, which grouping, which filters) and
asks the SchemaGraph for the *join path* -- it never fabricates a join. EBS
shapes (multi-org, flexfield, lookup) are exercised whenever the schema has them
and the chosen features are recorded for curriculum/coverage tracking.
"""

from __future__ import annotations

import random

from ..schema_graph.graph import SchemaGraph
from ..schema_graph.types import SemanticRole, Table
from .ast import (
    Aggregate,
    ColumnRef,
    HavingPredicate,
    JoinClause,
    Op,
    OrderItem,
    Predicate,
    SelectQuery,
    Subquery,
    WindowFunc,
)


class QuerySampler:
    def __init__(self, graph: SchemaGraph, rng: random.Random | None = None):
        self.graph = graph
        self.schema = graph.schema
        self.rng = rng or random.Random()

    def sample(self, level: int = 2) -> tuple[SelectQuery, list[str]]:
        facts = [t for t in self.schema.tables if t.has_role(SemanticRole.AMOUNT)]
        if level < 2 or not facts:
            return self._sample_l1()
        fact = self.rng.choice(facts)
        if level == 2:
            return self._sample_l2(fact)
        if level == 3:
            return self._sample_l3(fact)
        if level == 4:
            return self._sample_l4(fact)
        return self._sample_l5(fact)

    # -- L2: aggregate (+ group by) -------------------------------------
    def _pick_aggregate(self, fact: Table) -> Aggregate:
        """A varied measure: SUM/AVG/MAX/MIN over an amount, or COUNT of rows."""
        func = self.rng.choices(["SUM", "AVG", "COUNT", "MAX", "MIN"],
                                weights=[5, 2, 2, 1, 1])[0]
        if func == "COUNT":
            col = fact.primary_key or fact.columns[0]
        else:
            col = self.rng.choice(fact.by_role(SemanticRole.AMOUNT))
        return Aggregate(func, ColumnRef(fact.name, col))

    def _sample_l2(self, fact: Table, allow_no_group: bool = True) -> tuple[SelectQuery, list[str]]:
        rng = self.rng
        features: list[str] = []
        agg = self._pick_aggregate(fact)
        features.append(agg.func.lower())

        # ungrouped total ("how many invoices", "total invoice amount", "average amount")
        if allow_no_group and rng.random() < 0.3:
            query = SelectQuery(from_table=fact.name, aliases=self._aliases([fact.name]),
                                select=[agg])
            features.append("no_group")
            self._add_filters(query, fact, features)
            return query, features

        group_table, group_col = self._pick_group(fact)
        join_targets = [fact.name] if group_table == fact.name else [fact.name, group_table]
        joins = self._build_joins(fact.name, join_targets)
        # alias EVERY table in the query, including intermediates added by 2-hop joins
        all_tables = [fact.name] + [j.new_table for j in joins]
        aliases = self._aliases(all_tables)
        if joins:
            features.append("join")
        if group_col.role == SemanticRole.FLEXFIELD_SEGMENT:
            features.append("flexfield")

        query = SelectQuery(
            from_table=fact.name,
            aliases=aliases,
            select=[ColumnRef(group_table, group_col), agg],
            joins=joins,
            group_by=[ColumnRef(group_table, group_col)],
        )
        self._add_filters(query, fact, features)
        return query, features

    # -- L3: HAVING or top-N (extends L2) -------------------------------
    def _sample_l3(self, fact: Table) -> tuple[SelectQuery, list[str]]:
        query, features = self._sample_l2(fact, allow_no_group=False)
        agg = next((it for it in query.select if isinstance(it, Aggregate)), None)
        if agg is None:
            return query, features
        if self.rng.random() < 0.5:
            threshold = self.rng.choice([1_000, 10_000, 50_000, 100_000])
            query.having.append(HavingPredicate(agg, Op.GT, threshold))
            features.append("having")
        else:
            query.order_by.append(OrderItem(agg, descending=True))
            query.limit = self.rng.choice([3, 5, 10])
            features.append("top_n")
        features.append("L3")
        return query, features

    # -- L4: nested subquery --------------------------------------------
    def _sample_l4(self, fact: Table) -> tuple[SelectQuery, list[str]]:
        name_fks = [
            fk
            for fk in self.schema.foreign_keys
            if fk.from_table == fact.name
            and self.schema.table(fk.to_table).has_role(SemanticRole.NAME)
        ]
        if name_fks and self.rng.random() < 0.5:
            return self._sample_semijoin(fact, self.rng.choice(name_fks))
        return self._sample_above_average(fact)

    def _sample_above_average(self, fact: Table) -> tuple[SelectQuery, list[str]]:
        amount = self.rng.choice(fact.by_role(SemanticRole.AMOUNT))
        inner = SelectQuery(
            from_table=fact.name,
            aliases=self._aliases([fact.name]),
            select=[Aggregate("AVG", ColumnRef(fact.name, amount))],
        )
        pk = fact.primary_key or fact.columns[0]
        query = SelectQuery(
            from_table=fact.name,
            aliases=self._aliases([fact.name]),
            select=[ColumnRef(fact.name, pk), ColumnRef(fact.name, amount)],
            where=[Predicate(ColumnRef(fact.name, amount), Op.GT, Subquery(inner))],
        )
        return query, ["above_average", "subquery", "L4"]

    def _sample_semijoin(self, fact: Table, fk) -> tuple[SelectQuery, list[str]]:
        dim = self.schema.table(fk.to_table)
        name_col = self.rng.choice(dim.by_role(SemanticRole.NAME))
        inner = SelectQuery(
            from_table=fact.name,
            aliases=self._aliases([fact.name]),
            select=[ColumnRef(fact.name, fact.column(fk.from_column))],
        )
        features = ["semijoin", "subquery", "L4"]
        if fact.has_role(SemanticRole.DATE) and self.rng.random() < 0.7:
            dcol = self.rng.choice(fact.by_role(SemanticRole.DATE))
            inner.where.append(
                Predicate(ColumnRef(fact.name, dcol), Op.YEAR_EQ,
                          self.rng.choice([2023, 2024, 2025]))
            )
            features.append("date_filter")
        query = SelectQuery(
            from_table=dim.name,
            aliases=self._aliases([dim.name]),
            select=[ColumnRef(dim.name, name_col)],
            where=[Predicate(ColumnRef(dim.name, dim.column(fk.to_column)),
                             Op.IN, Subquery(inner))],
        )
        return query, features

    # -- L5: window ranking ---------------------------------------------
    def _sample_l5(self, fact: Table) -> tuple[SelectQuery, list[str]]:
        amount = self.rng.choice(fact.by_role(SemanticRole.AMOUNT))
        pk = fact.primary_key or fact.columns[0]
        part_col = None
        if fact.has_role(SemanticRole.ORG_ID):
            part_col = fact.by_role(SemanticRole.ORG_ID)[0]
        else:
            id_cols = [c for c in fact.columns if c.role == SemanticRole.ID and not c.is_pk]
            part_col = id_cols[0] if id_cols else None
        window = WindowFunc(
            "RANK",
            partition_by=[ColumnRef(fact.name, part_col)] if part_col else [],
            order_by=[OrderItem(ColumnRef(fact.name, amount), descending=True)],
        )
        select = [ColumnRef(fact.name, pk)]
        if part_col:
            select.append(ColumnRef(fact.name, part_col))
        select += [ColumnRef(fact.name, amount), window]
        query = SelectQuery(
            from_table=fact.name, aliases=self._aliases([fact.name]), select=select
        )
        return query, ["window", "L5"]

    def _pick_group(self, fact: Table):
        """Choose a label column to group by: prefer a joined dimension's NAME or
        a flexfield segment; fall back to a lookup/code column on the fact."""
        candidates = []
        for tname in [fact.name] + self._reachable(fact.name, max_hops=2):
            t = self.schema.table(tname)
            for role in (SemanticRole.NAME, SemanticRole.FLEXFIELD_SEGMENT):
                for col in t.by_role(role):
                    # avoid grouping a fact by its own surrogate id; names/segments only
                    candidates.append((tname, col))
        if candidates:
            return self.rng.choice(candidates)
        # fallback: group by a lookup/code column on the fact itself
        for role in (SemanticRole.LOOKUP, SemanticRole.CODE):
            cols = fact.by_role(role)
            if cols:
                return fact.name, self.rng.choice(cols)
        # last resort: the primary key
        return fact.name, fact.primary_key

    def _add_filters(self, query: SelectQuery, fact: Table, features: list[str]):
        rng = self.rng
        # date filter
        if fact.has_role(SemanticRole.DATE) and rng.random() < 0.6:
            col = rng.choice(fact.by_role(SemanticRole.DATE))
            query.where.append(
                Predicate(ColumnRef(fact.name, col), Op.YEAR_EQ, rng.choice([2023, 2024, 2025]))
            )
            features.append("date_filter")
        # multi-org filter
        if fact.has_role(SemanticRole.ORG_ID) and rng.random() < 0.7:
            col = fact.by_role(SemanticRole.ORG_ID)[0]
            query.where.append(
                Predicate(ColumnRef(fact.name, col), Op.EQ, rng.choice([101, 204, 305]))
            )
            features.append("multi_org")
        # lookup filter
        if fact.has_role(SemanticRole.LOOKUP) and rng.random() < 0.6:
            col = rng.choice(fact.by_role(SemanticRole.LOOKUP))
            if col.allowed_values:
                query.where.append(
                    Predicate(ColumnRef(fact.name, col), Op.EQ, rng.choice(col.allowed_values))
                )
                features.append("lookup_filter")

    # -- L1: single table -----------------------------------------------
    def _sample_l1(self) -> tuple[SelectQuery, list[str]]:
        rng = self.rng
        t = rng.choice(self.schema.tables)
        cols = [t.primary_key] if t.primary_key else []
        for role in (SemanticRole.NAME, SemanticRole.CODE, SemanticRole.AMOUNT, SemanticRole.DATE):
            cols.extend(t.by_role(role))
        cols = [c for c in cols if c][:4] or [t.columns[0]]
        query = SelectQuery(
            from_table=t.name,
            aliases=self._aliases([t.name]),
            select=[ColumnRef(t.name, c) for c in cols],
        )
        features: list[str] = []
        self._add_filters(query, t, features)
        return query, features

    # -- helpers ---------------------------------------------------------
    def _reachable(self, src: str, max_hops: int) -> list[str]:
        out, frontier, seen = [], [src], {src}
        for _ in range(max_hops):
            nxt = []
            for node in frontier:
                for nb in self.graph.neighbors(node):
                    if nb not in seen:
                        seen.add(nb)
                        out.append(nb)
                        nxt.append(nb)
            frontier = nxt
        return out

    def _build_joins(self, anchor: str, tables: list[str]) -> list[JoinClause]:
        fks = self.graph.join_tree(tables)
        # order edges so each adds a table connected to the already-present set
        present = {anchor}
        remaining = list(fks)
        joins: list[JoinClause] = []
        while remaining:
            for fk in list(remaining):
                a, b = fk.from_table, fk.to_table
                if a in present and b in present:
                    remaining.remove(fk)
                    break
                if a in present or b in present:
                    new = b if a in present else a
                    ta, tb = self.schema.table(fk.from_table), self.schema.table(fk.to_table)
                    joins.append(
                        JoinClause(
                            new_table=new,
                            on=[(
                                ColumnRef(fk.from_table, ta.column(fk.from_column)),
                                ColumnRef(fk.to_table, tb.column(fk.to_column)),
                            )],
                        )
                    )
                    present.add(new)
                    remaining.remove(fk)
                    break
            else:
                break  # disconnected (should not happen for join_tree output)
        return joins

    # short SQL keywords that must never be used as a bare table alias
    _RESERVED_ALIAS = {
        "as", "is", "in", "on", "or", "by", "to", "of", "at", "if", "no",
        "all", "and", "any", "asc", "end", "for", "not", "set", "sum", "min", "max",
    }

    @classmethod
    def _aliases(cls, tables: list[str]) -> dict[str, str]:
        aliases, used = {}, set()
        for name in tables:
            base = "".join(part[0] for part in name.split("_") if part)[:3] or name[:2]
            alias, i = base, 1
            while alias in used or alias in cls._RESERVED_ALIAS:
                i += 1
                alias = f"{base}{i}"
            used.add(alias)
            aliases[name] = alias
        return aliases
