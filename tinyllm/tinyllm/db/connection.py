"""Runtime database connection -- the "execution owns truth" component.

After the model proposes SQL and the graph validates its *form*, the live
database is the final authority on *correctness against the real instance*. Two
operations, both read-only and safe on a sensitive production DB:

  - explain(sql)        -- validate the SQL against the LIVE schema, returning NO
                           rows (Oracle: `EXPLAIN PLAN FOR ...`). Catches things
                           the structural gate can't: real type mismatches,
                           privileges, columns that exist in our graph but not in
                           this instance.
  - run_readonly(sql)   -- execute a SELECT against a read-only, least-privilege
                           account, capped to a row limit (the user-confirmed
                           step).

`SqliteDb` is a working stand-in (synthetic data) for demos/tests; `OracleDb`
is the real adapter (documented; needs a live instance + `oracledb`). Same
interface, so the serving layer is agnostic to which one is wired in.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..eval.execution import ExecHarness, to_sqlite
from ..schema_graph.types import Schema


@dataclass
class GateResult:
    ok: bool
    error: str = ""


def _is_select(sql: str) -> bool:
    head = sql.lstrip().lstrip("(").lstrip().upper()
    return head.startswith("SELECT") or head.startswith("WITH")


class DbConnection:
    """Interface: an EXPLAIN gate + a read-only execute."""

    def explain(self, sql: str) -> GateResult: raise NotImplementedError
    def run_readonly(self, sql: str, max_rows: int = 50) -> list: raise NotImplementedError


class SqliteDb(DbConnection):
    """Stand-in DB: an in-memory SQLite instance populated from a `Schema` (the
    same harness the eval uses). Lets the full propose->confirm->execute loop run
    end-to-end without Oracle."""

    def __init__(self, schema: Schema, seed: int = 0):
        self.harness = ExecHarness(schema, seed=seed)

    def explain(self, sql: str) -> GateResult:
        sqlite_sql = to_sqlite(sql)
        if sqlite_sql is None:
            return GateResult(False, "could not transpile")
        try:
            self.harness.conn.execute("EXPLAIN " + sqlite_sql)  # compiles, runs no rows
            return GateResult(True)
        except Exception as e:
            return GateResult(False, str(e).splitlines()[0])

    def run_readonly(self, sql: str, max_rows: int = 50) -> list:
        if not _is_select(sql):
            raise ValueError("read-only: only SELECT statements may be executed")
        sqlite_sql = to_sqlite(sql)
        if sqlite_sql is None:
            raise ValueError("could not transpile SQL")
        cur = self.harness.conn.execute(sqlite_sql)
        return cur.fetchmany(max_rows)

    def close(self):
        self.harness.close()


class OracleDb(DbConnection):
    """Real adapter (untested here -- needs a live instance). The cursor MUST come
    from a read-only, least-privilege account; EXPLAIN PLAN returns no rows so it
    is safe even on sensitive production data."""

    def __init__(self, cursor):
        self.cur = cursor

    def explain(self, sql: str) -> GateResult:
        try:
            self.cur.execute("EXPLAIN PLAN FOR " + sql)   # validates vs live schema, no rows
            return GateResult(True)
        except Exception as e:                            # ORA-xxxxx -> the real reason
            return GateResult(False, str(e).splitlines()[0])

    def run_readonly(self, sql: str, max_rows: int = 50) -> list:
        if not _is_select(sql):
            raise ValueError("read-only: only SELECT statements may be executed")
        self.cur.execute(sql)
        return self.cur.fetchmany(max_rows)
