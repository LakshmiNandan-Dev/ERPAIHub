"""Schema-aware repair of a model's SEMANTIC slips (graph owns form, again).

The graph-constrained decoder guarantees structural validity -- real tables,
columns, FK joins. But a well-trained model can still pick the wrong *valid*
identifier/value: the wrong flexfield segment ("cost center" is segment2, not
segment5) or a value outside a lookup's domain ('PAID' for an INVOICE TYPE
column). Those are deterministically fixable from the schema + the question:

  - flexfield: the question names a business label whose segment we know.
  - lookup:    the value must be one of the column's allowed_values, and the
               question usually says which one.

This is a small, auditable post-decode pass -- the schema correcting the model,
not the model guessing. It only acts on UNAMBIGUOUS cases and otherwise leaves
the SQL untouched.
"""

from __future__ import annotations

import re

from ..schema_graph.types import Schema, SemanticRole


def schema_repair(sql: str, question: str, schema: Schema) -> str:
    sql = _repair_flexfield(sql, question, schema)
    sql = _repair_lookup_values(sql, question, schema)
    return sql


def _repair_flexfield(sql: str, question: str, schema: Schema) -> str:
    """If the question names exactly one flexfield label, force the segment refs
    to that label's segment (e.g. 'by cost center' -> segmentN)."""
    label_to_seg: dict[str, str] = {}
    for t in schema.tables:
        for c in t.columns:
            if c.role == SemanticRole.FLEXFIELD_SEGMENT and c.business_label:
                label_to_seg[c.business_label.lower()] = c.name
    if not label_to_seg:
        return sql

    ql = question.lower()
    wanted = {seg for lbl, seg in label_to_seg.items() if lbl in ql}
    used = set(re.findall(r"\b\w+\.(segment\d+)", sql))
    if len(wanted) == 1 and used and used != wanted:
        seg = next(iter(wanted))
        sql = re.sub(r"(\b\w+\.)segment\d+", lambda m: m.group(1) + seg, sql)
    return sql


def _repair_lookup_values(sql: str, question: str, schema: Schema) -> str:
    """For a lookup-coded column, a literal outside allowed_values is replaced by
    the allowed value the question mentions (if any)."""
    ql = question.lower()
    for t in schema.tables:
        for c in t.columns:
            if c.role != SemanticRole.LOOKUP or not c.allowed_values:
                continue
            allowed = set(c.allowed_values)
            pat = re.compile(r"(\." + re.escape(c.name) + r"\s*=\s*')([^']*)(')", re.IGNORECASE)

            def fix(m, allowed=allowed, c=c):
                if m.group(2) in allowed:
                    return m.group(0)
                cand = [v for v in c.allowed_values if v.lower() in ql]
                return m.group(1) + cand[0] + m.group(3) if cand else m.group(0)

            sql = pat.sub(fix, sql)
    return sql
