"""NL/SQL diversity: aggregates beyond SUM (COUNT/AVG/MAX/MIN) and ungrouped
totals appear and are valid; the vendor-side LLM paraphrase hook stacks on top."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tinyllm import generate_example  # noqa: E402
from tinyllm.nl import set_llm_paraphraser  # noqa: E402


def test_aggregate_functions_and_ungrouped_appear():
    feats = set()
    for s in range(400):
        feats |= set(generate_example(s, level=2, style="ebs").ebs_features)
    assert {"count", "avg", "max", "min"} <= feats, feats      # beyond just SUM
    assert "no_group" in feats                                  # ungrouped totals


def test_count_and_nogroup_sql_valid():
    seen_count = seen_nogroup = False
    for s in range(400):
        ex = generate_example(s, level=2, style="ebs")
        assert ex.valid, ex.sql
        if "count" in ex.ebs_features:
            assert "COUNT(" in ex.sql
            seen_count = True
        if "no_group" in ex.ebs_features:
            assert "GROUP BY" not in ex.sql
            seen_nogroup = True
    assert seen_count and seen_nogroup


def test_llm_paraphraser_hook_stacks():
    try:
        set_llm_paraphraser(lambda q, k: [f"LLMVARIANT::{q}"])
        ex = generate_example(0, level=2, style="ebs", n_paraphrases=2)
        assert any(p.startswith("LLMVARIANT::") for p in ex.paraphrases)
    finally:
        set_llm_paraphraser(None)                              # reset global state

    # default (no hook) -> rule-based only, no leakage
    ex = generate_example(0, level=2, style="ebs", n_paraphrases=2)
    assert not any(p.startswith("LLMVARIANT::") for p in ex.paraphrases)
