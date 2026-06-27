"""SchemaGraph -- the join-resolution layer.

This is the clean boundary we agreed to keep: the rest of the system asks the
graph "how do these tables connect?" and never invents joins itself. That makes
joins correct-by-construction at generation time and validatable at inference
time -- the same graph, used twice (model = intent, graph = form).

The traversal here is a small, dependency-free BFS so it stays fully auditable.
At scale this class can be swapped for a rustworkx/igraph-backed implementation
behind the identical interface without touching callers.
"""

from __future__ import annotations

from collections import deque
from typing import Optional

from .types import ForeignKey, Schema, Table


class SchemaGraph:
    def __init__(self, schema: Schema):
        self.schema = schema
        # Undirected adjacency for join purposes: table -> list[(neighbor, fk)]
        self._adj: dict[str, list[tuple[str, ForeignKey]]] = {
            t.name: [] for t in schema.tables
        }
        for fk in schema.foreign_keys:
            self._adj.setdefault(fk.from_table, []).append((fk.to_table, fk))
            self._adj.setdefault(fk.to_table, []).append((fk.from_table, fk))

    # -- structure -------------------------------------------------------
    def tables(self) -> list[Table]:
        return self.schema.tables

    def neighbors(self, table: str) -> list[str]:
        return [other for other, _ in self._adj.get(table, [])]

    def fk_between(self, t1: str, t2: str) -> Optional[ForeignKey]:
        for other, fk in self._adj.get(t1, []):
            if other == t2:
                return fk
        return None

    # -- join resolution (graph owns "form") -----------------------------
    def join_path(self, src: str, dst: str) -> Optional[list[ForeignKey]]:
        """Shortest sequence of FKs connecting src..dst, or None if unreachable."""
        if src == dst:
            return []
        prev: dict[str, tuple[str, ForeignKey]] = {}
        seen = {src}
        q = deque([src])
        while q:
            node = q.popleft()
            for other, fk in self._adj.get(node, []):
                if other in seen:
                    continue
                seen.add(other)
                prev[other] = (node, fk)
                if other == dst:
                    return self._rebuild(prev, src, dst)
                q.append(other)
        return None

    def join_tree(self, table_names: list[str]) -> list[ForeignKey]:
        """A connected set of FK edges spanning all given tables.

        Steiner-tree approximation: anchor on the first table and union the
        shortest path from every other table to it. Sufficient for the L1/L2
        query shapes; a proper Steiner solver can drop in here later.
        """
        if len(table_names) <= 1:
            return []
        anchor = table_names[0]
        edges: list[ForeignKey] = []
        seen_edges: set[tuple] = set()
        for target in table_names[1:]:
            path = self.join_path(anchor, target)
            if path is None:
                raise ValueError(f"No join path between {anchor!r} and {target!r}")
            for fk in path:
                key = (fk.from_table, fk.from_column, fk.to_table, fk.to_column)
                if key not in seen_edges:
                    seen_edges.add(key)
                    edges.append(fk)
        return edges

    @staticmethod
    def _rebuild(prev, src, dst) -> list[ForeignKey]:
        path: list[ForeignKey] = []
        node = dst
        while node != src:
            parent, fk = prev[node]
            path.append(fk)
            node = parent
        path.reverse()
        return path
