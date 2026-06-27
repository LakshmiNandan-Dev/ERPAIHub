"""Dialect parse-validation via sqlglot (optional dependency).

First-line syntactic gate before (later) Oracle XE execution / EXPLAIN PLAN.
If sqlglot isn't installed the result is SKIP, so the pipeline still runs.
"""

from __future__ import annotations

from .result import ValidationResult


def validate_sqlglot(sql: str) -> ValidationResult:
    try:
        import sqlglot
    except ImportError:
        return ValidationResult("sqlglot", ok=None, issues=["sqlglot not installed"])

    try:
        sqlglot.parse_one(sql, dialect="oracle")
        return ValidationResult("sqlglot", ok=True)
    except Exception as exc:  # sqlglot.errors.ParseError and friends
        return ValidationResult("sqlglot", ok=False, issues=[str(exc).splitlines()[0]])
