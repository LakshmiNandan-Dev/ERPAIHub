"""Execution-accuracy eval -- the real text->SQL metric.

Exact-match is brutal and cosmetic: a correct query that orders columns
differently or aliases differently scores 0. The honest metric is whether the
predicted SQL *returns the same rows as the gold SQL*. We get that without
Oracle/Docker by:

  1. building an in-memory SQLite DB from the synthetic `Schema` (FK-consistent
     rows, with values drawn from the SAME literal pools the sampler filters on
     -- org 101/204/305, years 2023-25, lookup members -- so filters select
     non-empty subsets and GROUP BYs produce several groups);
  2. transpiling Oracle SQL -> SQLite via sqlglot (+ a small EXTRACT(YEAR ..)
     fix sqlglot leaves alone), uniformly for gold and prediction;
  3. running both and comparing result sets (order-sensitive only when the gold
     query has ORDER BY).

This is air-gap-safe (pure-Python stdlib sqlite3 + sqlglot) and the same harness
will later target a customer's Oracle via EXPLAIN/execute.
"""

from __future__ import annotations

import random
import re
import sqlite3

from ..schema_graph.types import ColumnType, SemanticRole, Schema, Table

# sqlglot leaves EXTRACT(YEAR FROM x) untranslated for SQLite -> rewrite it.
_EXTRACT = re.compile(r"EXTRACT\(\s*(YEAR|MONTH|DAY)\s+FROM\s+([^)]+)\)", re.IGNORECASE)
_FMT = {"YEAR": "%Y", "MONTH": "%m", "DAY": "%d"}

_NAMES = [
    "Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf", "Hotel",
    "India", "Juliet", "Kilo", "Lima", "Mike", "November", "Oscar", "Papa",
]
_SEGMENT_VALUES = ["01", "02", "03", "04", "05"]
_ORG_IDS = (101, 204, 305)          # must match QuerySampler._add_filters
_YEARS = (2023, 2024, 2025)


def to_sqlite(oracle_sql: str) -> str | None:
    """Oracle SQL -> executable SQLite, or None if it cannot be transpiled."""
    try:
        import sqlglot
    except ImportError:
        return None
    try:
        out = sqlglot.transpile(oracle_sql, read="oracle", write="sqlite")[0]
    except Exception:
        return None
    return _EXTRACT.sub(
        lambda m: f"CAST(STRFTIME('{_FMT[m.group(1).upper()]}', {m.group(2).strip()}) AS INTEGER)",
        out,
    )


def _sqlite_type(t: ColumnType) -> str:
    return {ColumnType.NUMBER: "INTEGER", ColumnType.VARCHAR2: "TEXT",
            ColumnType.DATE: "TEXT"}[t]


class ExecHarness:
    """An in-memory SQLite DB populated from one synthetic schema. Build once,
    run many queries (gold + each decode mode) against it."""

    def __init__(self, schema: Schema, seed: int = 0, dim_rows: int = 8, fact_rows: int = 40):
        self.schema = schema
        self.rng = random.Random(seed ^ 0x5DEECE66)
        self.dim_rows, self.fact_rows = dim_rows, fact_rows
        # check_same_thread=False so the same harness can back a server thread
        # (the eval uses it single-threaded; this only relaxes the guard).
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._fk_by_col = {(fk.from_table, fk.from_column): fk for fk in schema.foreign_keys}
        self._rowcount: dict[str, int] = {}
        self._build()

    def close(self):
        self.conn.close()

    # -- schema + data ---------------------------------------------------
    def _build(self):
        cur = self.conn.cursor()
        for t in self._topo_order():
            cols = ", ".join(
                f"{c.name} {_sqlite_type(c.type)}" + (" PRIMARY KEY" if c.is_pk else "")
                for c in t.columns
            )
            cur.execute(f"CREATE TABLE {t.name} ({cols})")
            self._insert(cur, t)
        self.conn.commit()

    def _topo_order(self) -> list[Table]:
        """FK targets before the tables that reference them."""
        done: set[str] = set()
        order: list[Table] = []
        tables = list(self.schema.tables)
        while tables:
            progressed = False
            for t in list(tables):
                targets = {fk.to_table for fk in self.schema.foreign_keys if fk.from_table == t.name}
                if targets <= done:
                    order.append(t)
                    done.add(t.name)
                    tables.remove(t)
                    progressed = True
            if not progressed:                 # cycle (shouldn't happen) -> emit rest
                order.extend(tables)
                break
        return order

    def _insert(self, cur, t: Table):
        is_fact = any(fk.from_table == t.name for fk in self.schema.foreign_keys)
        n = self.fact_rows if is_fact else self.dim_rows
        self._rowcount[t.name] = n
        placeholders = ", ".join("?" for _ in t.columns)
        sql = f"INSERT INTO {t.name} VALUES ({placeholders})"
        for pk in range(1, n + 1):
            cur.execute(sql, tuple(self._value(t, c, pk) for c in t.columns))

    def _value(self, t: Table, c, pk: int):
        rng = self.rng
        if c.is_pk:
            return pk
        fk = self._fk_by_col.get((t.name, c.name))
        if fk is not None:
            target_n = self._rowcount.get(fk.to_table, self.dim_rows)
            return rng.randint(1, max(1, target_n))
        role = c.role
        if role == SemanticRole.AMOUNT:
            return rng.randint(10, 100_000)
        if role == SemanticRole.QUANTITY:
            return rng.randint(1, 500)
        if role == SemanticRole.ORG_ID:
            return rng.choice(_ORG_IDS)
        if role == SemanticRole.DATE:
            return f"{rng.choice(_YEARS)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
        if role == SemanticRole.LOOKUP and c.allowed_values:
            return rng.choice(c.allowed_values)
        if role == SemanticRole.FLEXFIELD_SEGMENT:
            return rng.choice(_SEGMENT_VALUES)
        if role == SemanticRole.NAME:
            return _NAMES[pk % len(_NAMES)]            # distinct within small dims
        if role == SemanticRole.CODE:
            return f"{t.name[:3].upper()}-{pk:04d}"
        if c.type == ColumnType.NUMBER:
            return rng.randint(1, 1000)
        if c.type == ColumnType.DATE:
            return f"{rng.choice(_YEARS)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
        return f"{c.name}_{pk}"

    # -- run + compare ---------------------------------------------------
    def rows(self, oracle_sql: str):
        """Execute Oracle SQL against the DB; (rows, None) or (None, error)."""
        sqlite_sql = to_sqlite(oracle_sql)
        if sqlite_sql is None:
            return None, "transpile failed"
        try:
            cur = self.conn.execute(sqlite_sql)
            return cur.fetchall(), None
        except Exception as e:                 # bad columns/SQL -> query is wrong
            return None, str(e).splitlines()[0]


def _norm(rows, ordered: bool):
    tuples = [tuple(r) for r in rows]
    if ordered:
        return tuples
    return sorted(tuples, key=lambda r: tuple(repr(x) for x in r))


def result_match(gold_rows, pred_rows, ordered: bool) -> bool:
    if gold_rows is None or pred_rows is None:
        return False
    return _norm(gold_rows, ordered) == _norm(pred_rows, ordered)


def execution_match(harness: ExecHarness, gold_oracle: str, pred_oracle: str) -> dict:
    """Run gold and prediction; report whether their result sets match."""
    ordered = "ORDER BY" in gold_oracle.upper()
    gold_rows, gold_err = harness.rows(gold_oracle)
    pred_rows, pred_err = harness.rows(pred_oracle)
    return {
        "match": result_match(gold_rows, pred_rows, ordered),
        "gold_ok": gold_rows is not None,
        "pred_ok": pred_rows is not None,
        "pred_error": pred_err,
    }
