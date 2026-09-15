"""Best-effort join-predicate mining from view/PL-SQL source text.

EBS's real join semantics often live in the CODE -- a view, a package body, a
trigger -- not in declared constraints or column-name conventions (the
`terms_id` -> `ap_terms.term_id` case: no FK, and the column name doesn't match
the target's PK name, so naming-convention inference in `extractor.py` misses
it -- but any view or procedure that actually joins them says so explicitly).

This scans that source text for equality predicates between two qualified
column references (`a.col = b.col`), resolves each side's alias back to a real
table via the object's own FROM/JOIN clauses, and keeps only predicates where
BOTH sides land on a table already in the extracted catalog scope -- a
predicate that can't be resolved to two real tables is dropped, never guessed
into an edge.

Views are single, well-formed SELECT statements, so sqlglot (Oracle dialect)
parses them properly and joins are read straight off the AST. Package bodies /
procedures / functions / triggers mix embedded SQL into procedural PL/SQL that
sqlglot cannot parse as one statement, so those fall back to a regex scan for
the same shape of alias-qualified equality predicate -- inherently heuristic,
but the "both sides must resolve to a known table" filter keeps false
positives (record-field access, package-qualified calls, ...) out.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass
from typing import Iterable

logging.getLogger("sqlglot").setLevel(logging.ERROR)   # e.g. legacy (+) outer-join warnings


@dataclass(frozen=True)
class RawJoinHint:
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    source_name: str
    source_kind: str          # "view" | "package_body" | "procedure" | "function" | "trigger"


@dataclass(frozen=True)
class RawLabelHint:
    table: str
    column: str
    label: str                # human-readable, e.g. "cost center" (not "cost_center")
    source_name: str
    source_kind: str


_SQL_KEYWORDS = {
    "select", "from", "where", "and", "or", "on", "join", "inner", "outer",
    "left", "right", "full", "cross", "group", "order", "by", "having",
    "union", "all", "distinct", "as", "into", "values", "set", "when",
    "then", "else", "end", "case", "exists", "not", "in", "is", "null",
    "begin", "loop", "for", "if", "elsif", "return", "declare", "cursor",
}
_ITEM = r"[A-Za-z_][\w$#]*(?:\s+(?:AS\s+)?[A-Za-z_][\w$#]*)?"
# FROM/UPDATE take a comma-separated table list (old-style "FROM a x, b y");
# JOIN never does (each JOIN keyword introduces exactly one table)
_FROM_LIST_RE = re.compile(rf"\b(?:FROM|UPDATE)\s+({_ITEM}(?:\s*,\s*{_ITEM})*)", re.IGNORECASE)
_JOIN_ITEM_RE = re.compile(
    r"\bJOIN\s+([A-Za-z_][\w$#]*)(?:\s+(?:AS\s+)?([A-Za-z_][\w$#]*))?", re.IGNORECASE
)
_ITEM_RE = re.compile(r"([A-Za-z_][\w$#]*)(?:\s+(?:AS\s+)?([A-Za-z_][\w$#]*))?")
_EQ_PRED_RE = re.compile(
    r"\b([A-Za-z_][\w$#]*)\.([A-Za-z_][\w$#]*)\s*=\s*([A-Za-z_][\w$#]*)\.([A-Za-z_][\w$#]*)\b"
)


def _regex_alias_map(text: str) -> dict[str, str]:
    """alias/bare-name (lowercased) -> table name (lowercased), from FROM/UPDATE
    (comma-separated old-style lists included) and JOIN refs."""
    amap: dict[str, str] = {}

    def add(table: str, alias: str | None) -> None:
        if table.lower() in _SQL_KEYWORDS:
            return
        amap[table.lower()] = table.lower()
        if alias and alias.lower() not in _SQL_KEYWORDS:
            amap[alias.lower()] = table.lower()

    for item_list in _FROM_LIST_RE.findall(text):
        for table, alias in _ITEM_RE.findall(item_list):
            add(table, alias)
    for table, alias in _JOIN_ITEM_RE.findall(text):
        add(table, alias)
    return amap


def _mine_regex(text: str, name: str, kind: str, known: set[str]) -> list[RawJoinHint]:
    amap = _regex_alias_map(text)
    out: list[RawJoinHint] = []
    for lt, lc, rt, rc in _EQ_PRED_RE.findall(text):
        ft = amap.get(lt.lower(), lt.lower())
        tt = amap.get(rt.lower(), rt.lower())
        if ft == tt or ft not in known or tt not in known:
            continue
        out.append(RawJoinHint(ft, lc.lower(), tt, rc.lower(), name, kind))
    return out


def _parse_sql(text: str):
    """sqlglot.parse_one wrapped so both miners share one failure mode: None
    means "couldn't parse at all" (caller decides what to fall back to,
    or nothing for the label miner, which has no regex path)."""
    try:
        import sqlglot
    except ImportError:
        return None
    try:
        return sqlglot.parse_one(text, dialect="oracle")
    except Exception:
        return None


def _sqlglot_alias_map(tree, known: set[str]) -> dict[str, str]:
    """alias/bare-name (lowercased) -> real table name (lowercased), read off
    the parsed AST's own FROM/JOIN table references. Prefers the OWNER-
    QUALIFIED form ("ap.ap_invoices_all") when that's what's actually in
    `known` -- most real EBS tables have no APPS-owned synonym (verified: only
    ~9% did on a real instance), so their canonical extracted name is owner-
    qualified, but sqlglot's `Table.name` drops the owner/db prefix entirely
    (`FROM ap.ap_invoices_all` parses to name="ap_invoices_all", db="ap") --
    without this, every owner-qualified reference silently resolves to a bare
    name that isn't actually in the schema, and nothing ever matches. Falls
    back to the bare name when that's what's in `known` instead (a real APPS
    synonym exists for that table)."""
    from sqlglot import expressions as exp

    amap: dict[str, str] = {}
    for t in tree.find_all(exp.Table):
        bare = (t.name or "").lower()
        if not bare:
            continue
        qualified = f"{t.db.lower()}.{bare}" if t.db else None
        resolved = qualified if qualified in known else bare
        amap[bare] = resolved
        if qualified:
            amap[qualified] = resolved
        if t.alias:
            amap[t.alias.lower()] = resolved
    return amap


def _mine_sqlglot(text: str, name: str, kind: str, known: set[str]) -> list[RawJoinHint] | None:
    """Returns None (not []) when sqlglot can't parse `text` at all, so the
    caller knows to fall back to the regex scan instead of accepting "no
    joins found" from a parse that never happened."""
    from sqlglot import expressions as exp

    tree = _parse_sql(text)
    if tree is None:
        return None
    amap = _sqlglot_alias_map(tree, known)

    out: list[RawJoinHint] = []
    for eq in tree.find_all(exp.EQ):
        left, right = eq.this, eq.expression
        if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
            continue
        ft = amap.get((left.table or left.name).lower())
        tt = amap.get((right.table or right.name).lower())
        if not ft or not tt or ft == tt or ft not in known or tt not in known:
            continue
        out.append(RawJoinHint(ft, left.name.lower(), tt, right.name.lower(), name, kind))
    return out


def _mine_sqlglot_labels(text: str, name: str, kind: str, known: set[str]) -> list[RawLabelHint]:
    """SELECT-list aliases over a bare column (`segment3 cost_center`, or
    `AS`) reveal that column's business meaning -- the same kind of thing FND
    flexfield/lookup metadata provides, but discoverable straight from how the
    customer's own views already label it. Only a DIRECT column alias counts
    (not `UPPER(x) y` or any other expression) -- that's the confident case,
    not a guess. Aliases identical to the column's own name teach nothing."""
    from sqlglot import expressions as exp

    tree = _parse_sql(text)
    if tree is None or not isinstance(tree, exp.Select):
        return []
    amap = _sqlglot_alias_map(tree, known)
    sole_table = next(iter(set(amap.values()))) if len(set(amap.values())) == 1 else None

    out: list[RawLabelHint] = []
    for item in tree.expressions:
        if not isinstance(item, exp.Alias) or not isinstance(item.this, exp.Column):
            continue
        col = item.this
        # unqualified column ("SELECT segment3 cost_center FROM t", no
        # alias) belongs to the sole FROM table when there's only one
        table = amap.get(col.table.lower()) if col.table else sole_table
        if not table or table not in known:
            continue
        label = item.alias.strip().lower()
        if not label or label == col.name.lower():
            continue
        out.append(RawLabelHint(table, col.name.lower(), label.replace("_", " "), name, kind))
    return out


def mine_join_hints(
    objects: Iterable[tuple[str, str, str]], known_tables: Iterable[str],
    on_progress=None,
) -> list[RawJoinHint]:
    """objects: iterable of (name, kind, text) -- kind is 'view',
    'materialized_view', 'package_body', 'procedure', 'function', 'type_body',
    or 'trigger'. Returns deduped join hints whose both ends resolve to a
    table already in `known_tables`; text sqlglot can't parse (virtually all
    non-view/mview PL/SQL) falls back to the regex scan.

    `on_progress(done, total)`, if given, is called every 500 objects -- this
    loop is CPU-bound (sqlglot parsing) and can be the slowest single step
    when mining a real instance's full view/PL-SQL catalog, with nothing else
    to signal it's still working."""
    objects = list(objects)
    total = len(objects)
    known = {t.lower() for t in known_tables}
    seen: set[tuple] = set()
    out: list[RawJoinHint] = []
    for i, (name, kind, text) in enumerate(objects, 1):
        if on_progress and i % 500 == 0:
            on_progress(i, total)
        if not text:
            continue
        mined = _mine_sqlglot(text, name, kind, known)
        if mined is None:
            mined = _mine_regex(text, name, kind, known)
        for h in mined:
            key = (h.from_table, h.from_column, h.to_table, h.to_column)
            if key not in seen:
                seen.add(key)
                out.append(h)
    return out


def mine_column_labels(
    objects: Iterable[tuple[str, str, str]], known_tables: Iterable[str],
    on_progress=None,
) -> dict[str, dict[str, str]]:
    """(table -> {column: business_label}) mined from view/materialized-view
    SELECT-list aliases. Unlike join mining, there's no regex fallback here --
    a direct-column-alias is a precise AST shape (`Alias(this=Column(...))`)
    that's only reliably recognizable with a real parse, and procedural PL/SQL
    (package bodies, procedures, functions, triggers) isn't SELECT-list shaped
    in the first place, so it's skipped rather than guessed at.

    The SAME column is often aliased differently across views (one calls
    `segment3` "cost_center", another calls it "department" for a report-
    specific reason) -- the most FREQUENT label wins, ties broken
    alphabetically for determinism. This is a real limitation: a label used
    by one important, official view can lose to two casual/incidental
    aliases elsewhere that happen to agree with each other by coincidence.
    There's no reliable signal here to weight "official" over "incidental"."""
    objects = list(objects)
    total = len(objects)
    known = {t.lower() for t in known_tables}
    votes: dict[tuple[str, str], Counter] = {}
    for i, (name, kind, text) in enumerate(objects, 1):
        if on_progress and i % 500 == 0:
            on_progress(i, total)
        if not text or kind not in ("view", "materialized_view"):
            continue
        for h in _mine_sqlglot_labels(text, name, kind, known):
            votes.setdefault((h.table, h.column), Counter())[h.label] += 1

    out: dict[str, dict[str, str]] = {}
    for (table, column), counter in votes.items():
        best = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        out.setdefault(table, {})[column] = best
    return out
