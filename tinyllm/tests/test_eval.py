"""Execution-accuracy eval: the synthetic DB runs gold SQL and self-matches,
EXTRACT is translated, and result comparison respects ORDER BY."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tinyllm import generate_example  # noqa: E402
from tinyllm.eval import ExecHarness, execution_match, result_match, to_sqlite  # noqa: E402


def test_gold_executes_and_self_matches():
    for lvl in (1, 2, 3, 4, 5):
        for s in range(1_000_000, 1_000_000 + 12):
            ex = generate_example(s, level=lvl)
            h = ExecHarness(ex.schema, seed=s)
            rows, err = h.rows(ex.sql)
            assert rows is not None, (lvl, ex.sql, err)
            assert execution_match(h, ex.sql, ex.sql)["match"]
            h.close()


def test_to_sqlite_translates_extract():
    # find a date-filter example (uses EXTRACT(YEAR FROM ...))
    ex = next(generate_example(s, level=2) for s in range(200)
              if "EXTRACT" in generate_example(s, 2).sql)
    out = to_sqlite(ex.sql)
    assert out is not None and "EXTRACT" not in out.upper() and "STRFTIME" in out.upper()


def test_wrong_query_does_not_match():
    ex = generate_example(1_000_000, level=2)
    h = ExecHarness(ex.schema, seed=1_000_000)
    t = ex.schema.tables[0]
    other = f"SELECT {t.primary_key.name} FROM {t.name}"   # different shape -> different rows
    assert execution_match(h, ex.sql, other)["match"] is False
    h.close()


def test_result_match_respects_order():
    assert result_match([(1,), (2,)], [(2,), (1,)], ordered=False) is True
    assert result_match([(1,), (2,)], [(2,), (1,)], ordered=True) is False
    assert result_match([(1,), (2,)], [(1,), (2,)], ordered=True) is True
    assert result_match(None, [(1,)], ordered=False) is False
