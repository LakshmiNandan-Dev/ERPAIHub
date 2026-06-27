"""Vendor-side LLM paraphrase hook (optional; air-gap default is OFF).

The rule-based realizer (`realize.py`) is the air-gap-safe default and runs
anywhere, including customer-local. VENDOR-side -- where there is NO customer
data, so a frontier model's ToS is satisfiable -- you can register an LLM
paraphraser to stack richer, more natural phrasings on top (e.g. ask Claude to
rewrite the canonical question 10 ways). It augments the from-scratch base's
training data only; it never runs customer-side and never sees customer data.

    from tinyllm.nl import set_llm_paraphraser
    set_llm_paraphraser(lambda q, k: my_claude_call(q, k))   # vendor-side only
"""

from __future__ import annotations

from typing import Callable, Optional

_FN: Optional[Callable[[str, int], list[str]]] = None


def set_llm_paraphraser(fn: Optional[Callable[[str, int], list[str]]]) -> None:
    """Register (or clear, with None) a vendor-side paraphraser fn(question, k)->phrases."""
    global _FN
    _FN = fn


def llm_paraphrases(question: str, k: int) -> list[str]:
    """k extra phrasings from the registered LLM paraphraser; [] if none/air-gap."""
    if _FN is None or k <= 0:
        return []
    try:
        return [p for p in _FN(question, k) if p and p.strip()][:k]
    except Exception:                          # a flaky vendor call must never break gen
        return []
