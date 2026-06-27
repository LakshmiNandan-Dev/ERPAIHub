"""Lexicon for rule-based paraphrase -- fully offline / air-gap-safe.

Synonym pools and sentence frames, keyed by query shape. This layer runs
*anywhere*, including customer-local; the optional LLM-augmented paraphrase
(vendor-side, no customer data) stacks on top to add further naturalness.

Frames use named slots so grammar holds regardless of the lexical choices.
"""

from __future__ import annotations

# -- measure phrasing, by aggregate function -------------------------------
MEASURE = {
    "SUM": ["total {m}", "the total {m}", "sum of {m}", "overall {m}", "aggregate {m}"],
    "AVG": ["average {m}", "the average {m}", "mean {m}", "avg {m}"],
    "MAX": ["highest {m}", "maximum {m}", "max {m}", "the highest {m}", "largest {m}"],
    "MIN": ["lowest {m}", "minimum {m}", "min {m}", "the lowest {m}", "smallest {m}"],
}
# COUNT counts rows of a thing, so it phrases on the entity, not a measure column.
# Noun phrases only, so they compose inside the interrogative frames below.
COUNT_MEASURE = ["number of {d}", "count of {d}", "the number of {d}", "total number of {d}"]

# ungrouped totals ("what is the total amount", "number of invoices")
NOGROUP = ["{measure}", "what is the {measure}?", "what's the {measure}?",
           "show me the {measure}", "give me the {measure}", "{measure} overall"]

# -- grouping connectives --------------------------------------------------
GROUP = ["by {g}", "per {g}", "for each {g}", "grouped by {g}", "broken down by {g}", "across {g}"]

# -- aggregate sentence frames ({measure} and {group} are pre-built) -------
AGG_FRAMES = [
    "{measure} {group}",
    "show me {measure} {group}",
    "give me {measure} {group}",
    "what is {measure} {group}?",
    "what's the {measure} {group}?",
    "breakdown of {measure} {group}",
    "calculate {measure} {group}",
    "{measure} broken down {group}",
    "find {measure} {group}",
]

# -- top-N -----------------------------------------------------------------
TOPN = [
    "top {n} {g} by {m}",
    "the {n} highest {g} by {m}",
    "the {n} {g} with the most {m}",
    "{n} largest {g} by {m}",
]

# -- HAVING ----------------------------------------------------------------
HAVING = [
    "with {m2} over {v}",
    "having total {m2} above {v}",
    "where the total {m2} exceeds {v}",
    "with more than {v} in {m2}",
]

# -- filter phrasings ------------------------------------------------------
DATE = ["in {y}", "for {y}", "during {y}", "for fiscal year {y}"]
ORG = ["for operating unit {o}", "for org {o}", "in operating unit {o}", "for ou {o}"]
LOOKUP = ["where {label} is {v}", "that are {v}", "with {label} {v}", "only {v} ones"]

# -- subquery shapes -------------------------------------------------------
ABOVE_AVG = [
    "{d} above the average {m}",
    "which {d} are above average {m}",
    "{d} with {m} higher than average",
    "{d} whose {m} exceeds the average",
]
SEMIJOIN = [
    "list {dim} that have {doc}",
    "which {dim} have {doc}",
    "{dim} that appear in {doc}",
    "find {dim} having {doc}",
]

# -- window ----------------------------------------------------------------
WINDOW = [
    "rank {d} by {m} within each {p}",
    "rank {d} by {m} per {p}",
    "ranking of {d} by {m} for each {p}",
    "rank {d} within {p} by {m}",
]

# -- L1 list ---------------------------------------------------------------
LIST_VERBS = ["list", "show", "show me", "display", "get", "give me", "find"]

# -- entity-name cleanup ---------------------------------------------------
_TABLE_SUFFIXES = ["_headers_all", "_headers", "_lines_all", "_lines", "_all", "_vl", "_tl", "_v"]


def entity_label(table: str) -> str:
    """payment_headers_all -> 'payment'; receipt_lines_all -> 'receipt line'."""
    for suf in _TABLE_SUFFIXES:
        if table.endswith(suf):
            return table[: -len(suf)].replace("_", " ")
    return table.replace("_", " ")


def pluralize(word: str) -> str:
    if word.endswith(("s", "x", "ch", "sh")):
        return word + "es"
    if word.endswith("y") and len(word) > 1 and word[-2] not in "aeiou":
        return word[:-1] + "ies"
    return word + "s"


def group_label(label: str) -> str:
    """'vendor name' -> 'vendor' so 'per vendor' reads naturally."""
    return label[:-5] if label.endswith(" name") else label
