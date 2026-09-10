"""Answer an EBS question by picking a reviewed tool — never by writing SQL.

This is the anti-hallucination path. The old NL->SQL flow asks a model to
author Oracle SQL against a schema it has to be taught; this one gives the
model a catalog of tools whose SQL was written and reviewed by hand, and
asks it for one thing only: a tool name plus typed arguments. Everything
the model returns is validated against the tool's own JSON schema before
execution, so the worst a confused model can do is pick the wrong tool or
drop an argument — it cannot invent a table, a column, or a predicate.

Rendering follows the same rule. The numbers a user sees are formatted in
Python straight from the tool's structured payload; the model only writes
a short lead-in sentence over data that is already on screen. A drifting
summary is then visibly contradicted by the table beneath it rather than
silently believed.

The catalog is also the routing vocabulary: `shortlist()` scores a question
against the tool names and descriptions themselves rather than a hardcoded
keyword list, so a toolset added to EBSMCP later becomes reachable from chat
with no change here.
"""

from __future__ import annotations

import asyncio
import functools
import json
import re
from typing import Any, Optional

from app.core.ebs_tools_service import call_ebs_tool, list_ebs_tools

# Words that carry no routing signal. Deliberately small: this is a stopword
# list for scoring, not an attempt at real NLP.
_STOP = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being", "am",
    "do", "does", "did", "doing", "have", "has", "had", "of", "in", "on", "at",
    "to", "for", "with", "by", "from", "out", "if", "or", "and", "but",
    "as", "it", "its", "this", "that", "these", "those", "there", "here",
    "what", "which", "who", "whom", "when", "where", "why", "how", "all",
    "any", "both", "each", "more", "most", "some", "such", "no", "not", "only",
    "own", "same", "so", "than", "too", "very", "can", "will", "just", "now",
    "me", "my", "we", "our", "you", "your", "i", "please", "show", "tell",
    "give", "get", "list", "check", "look", "see", "find", "want", "need",
    "about", "into", "over", "under", "then", "them", "they", "he", "she",
}

# Paraphrase coverage. The catalog's own vocabulary does the primary matching;
# these map the words operators actually type onto words the tool text uses
# ("stuck" never appears in a description, "hung"/"running" do). Add to this
# only when a real question fails to route — it is a bridge, not a taxonomy.
_SYNONYMS = {
    "stuck": ("running", "pending", "hung", "blocked", "wait"),
    "hung": ("running", "blocked", "wait"),
    "slow": ("load", "elapsed", "performance", "wait", "sql"),
    "hang": ("blocked", "lock", "wait"),
    "down": ("status", "instance", "running"),
    "up": ("status", "instance", "running"),
    "database": ("database", "instance"),
    "db": ("database", "instance"),
    "uptime": ("startup", "instance", "status"),
    "restarted": ("startup", "instance"),
    "space": ("tablespace", "usage", "free", "temp", "undo"),
    "disk": ("tablespace", "usage", "segment"),
    "full": ("usage", "tablespace", "free"),
    "memory": ("sga", "pga", "cache", "workarea"),
    "cpu": ("load", "session", "sql"),
    "user": ("fnd_user", "accounts", "login", "responsibility"),
    "users": ("fnd_user", "accounts", "login", "responsibility"),
    "locked": ("lock", "blocking", "accounts"),
    "job": ("request", "concurrent", "program"),
    "jobs": ("request", "concurrent", "program"),
    "queue": ("pending", "backlog", "manager"),
    "error": ("errors", "failed", "invalid"),
    "errors": ("failed", "invalid", "alert"),
    "failing": ("failed", "errors"),
    "invalid": ("errors", "objects"),
    "patch": ("patch", "adop", "history"),
    "backup": ("backup", "rman", "archive"),
    "print": ("printer", "output"),
    "printing": ("printer", "output"),
    "email": ("mailer", "notification"),
    "mail": ("mailer", "notification"),
    "workflow": ("workflow", "notification", "activities"),
    "security": ("privilege", "grants", "sod", "responsibility"),
    "access": ("privilege", "grants", "responsibility"),
    "instance": ("instance", "database", "status"),
    "instances": ("instance", "database", "status"),
    "environment": ("instance",),
    "environments": ("instance",),
}


def _tokens(text: str) -> set[str]:
    """Lowercase word tokens, stopwords removed, expanded with synonyms."""
    raw = [w for w in re.findall(r"[a-z_][a-z0-9_]+", (text or "").lower()) if w not in _STOP]
    out: set[str] = set()
    for w in raw:
        out.add(w)
        # Cheap singularisation so "tablespaces" matches "tablespace".
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            out.add(w[:-1])
        out.update(_SYNONYMS.get(w, ()))
    return out


# A description can run to 200 words, so incidental overlap piles up: a tool
# whose text merely mentions "space", "usage" and "free" would otherwise
# outrank one actually named `tablespace_health`. Capping the description's
# contribution keeps it as the tie-breaker it should be, and lets a clean
# name match win.
_DESC_CAP = 3.0

# Menu candidates must score at least this fraction of the leader.
RELATIVE_CUTOFF = 0.5


def _score(q_tokens: set[str], tool: dict) -> float:
    """Overlap of the question against one tool's name and description.

    A name hit counts far more than a description hit: names are curated
    and short ("blocking_locks"), while descriptions run long enough that
    incidental word overlap is common and nearly meaningless.
    """
    name_tokens = set(tool["name"].lower().split("_"))
    desc_tokens = _tokens(tool.get("description", ""))
    return (3.0 * len(q_tokens & name_tokens)
            + min(float(len(q_tokens & desc_tokens)), _DESC_CAP))


def shortlist(question: str, catalog: list[dict], limit: int = 14,
              min_score: float = 3.0) -> list[dict]:
    """The plausible tools for this question, best first.

    Two jobs at once. It is the cheap intent gate — an empty result means
    "not an EBS tool question", and the caller falls through to its normal
    behaviour without ever paying for an LLM call. It is also the context
    trimmer: selection accuracy falls off well before 65 choices, so the
    model is shown a short, relevant menu rather than the whole catalog.

    min_score of 3.0 is exactly one tool-name word ("tablespace", "sga"),
    or three description words. Below that the overlap is incidental.

    Candidates far behind the leader are then dropped entirely rather than
    padded onto the menu. A model asked for "failed concurrent requests" will
    reach for `failed_login_attempts` if it is offered — and the lexical
    evidence never supported offering it. Keeping the menu to genuine
    contenders removes the temptation instead of arguing with it in a prompt.
    """
    q = _tokens(question)
    if not q:
        return []
    scored = [(_score(q, t), t) for t in catalog]
    scored = [(sc, t) for sc, t in scored if sc >= min_score]
    if not scored:
        return []
    scored.sort(key=lambda pair: (-pair[0], pair[1]["name"]))
    cutoff = max(min_score, RELATIVE_CUTOFF * scored[0][0])
    return [t for sc, t in scored[:limit] if sc >= cutoff]


def _brief(description: str, limit: int = 320) -> str:
    """Collapse a tool docstring to a single-line menu entry."""
    flat = " ".join((description or "").split())
    if len(flat) <= limit:
        return flat
    cut = flat[:limit]
    stop = cut.rfind(". ")
    return (cut[:stop + 1] if stop > 80 else cut.rstrip() + "…")


def _param_hint(schema: dict) -> str:
    """One-line parameter summary: names, enum choices, and requiredness."""
    props = (schema or {}).get("properties") or {}
    if not props:
        return "no arguments"
    required = set((schema or {}).get("required") or [])
    parts = []
    for name, spec in props.items():
        choices = spec.get("enum")
        if choices:
            parts.append(f"{name}={'|'.join(str(c) for c in choices)}")
        else:
            parts.append(name if name in required else f"{name}?")
    return ", ".join(parts)


_SELECT_SYSTEM = (
    "You route Oracle E-Business Suite questions to a diagnostic tool.\n"
    "You are given a numbered menu of tools, already ordered by how well each one\n"
    "matches the question — 1 is the strongest candidate. Choose the single best\n"
    "one, or none. Prefer a lower number unless a later one is clearly better.\n\n"
    "Reply with ONE line of JSON and nothing else:\n"
    '  {"tool": "<exact name from the menu>", "arguments": {...}}\n'
    '  {"tool": null}   if no tool on the menu answers the question\n\n'
    "Rules:\n"
    "- The tool name must be copied exactly from the menu. Never invent one.\n"
    "- Include an argument only if the question states its value. Omit the rest;\n"
    "  every argument has a safe default.\n"
    "- Arguments marked name=a|b|c accept only one of those exact values.\n"
    '- Never write SQL, table names, or column names. Only a tool name and arguments.\n'
    "- These tools report the CURRENT STATE of a live system. If the question asks\n"
    "  for an explanation, a definition, a recommendation, or how to do something,\n"
    '  reply {"tool": null} — that is not a state question.\n'
    "- Prefer null over a poor fit. Something else handles the question then."
)


def _selection_messages(question: str, menu: list[dict]) -> list[dict]:
    lines = []
    for i, tool in enumerate(menu, 1):
        lines.append(
            f"{i}. {tool['name']}\n"
            f"   what it does: {_brief(tool.get('description', ''))}\n"
            f"   arguments: {_param_hint(tool.get('input_schema') or {})}"
        )
    return [
        {"role": "system", "content": _SELECT_SYSTEM},
        {"role": "user", "content": f"TOOL MENU\n\n" + "\n".join(lines)
                                    + f"\n\nQUESTION\n{question}\n\nJSON:"},
    ]


def _parse_selection(raw: str) -> Optional[dict]:
    """Pull the JSON object out of a model reply, tolerating fences and prose."""
    if not raw:
        return None
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    if start == -1:
        return None
    # Scan for the matching brace rather than taking the last one in the
    # string — trailing commentary can contain braces of its own.
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[start:i + 1])
                except ValueError:
                    return None
                return obj if isinstance(obj, dict) else None
    return None


def _prop_types(spec: dict) -> set[str]:
    """The JSON types a property accepts, flattening the anyOf/null wrapper
    pydantic emits for `x: str | None = None`."""
    types = set()
    if "type" in spec:
        types.add(spec["type"])
    for branch in spec.get("anyOf", []) or []:
        if "type" in branch:
            types.add(branch["type"])
    return types


# Values a model emits when it has nothing real to put in a slot. None of these
# is ever a legitimate filter, and passing one through turns a good tool choice
# into a failed call ("Unknown EBS instance '?'").
_PLACEHOLDERS = {
    "?", "??", "n/a", "na", "none", "null", "nil", "-", "--", "todo",
    "all", "any", "every", "unknown", "unspecified", "not specified",
    "string", "value", "name", "example", "default", "tbd", "xxx",
}


def _is_placeholder(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    v = value.strip().lower()
    if not v or v in _PLACEHOLDERS:
        return True
    # <instance>, {name}, [value] — the shapes a model copies out of a schema.
    return bool(re.fullmatch(r"[<{\[].*[>}\]]", v))


def sanitize_arguments(arguments: Any, schema: dict,
                       known_instances: Optional[set[str]] = None) -> dict:
    """Keep only what the tool's own schema accepts.

    This is the hard boundary. Anything the model made up — an unknown
    argument, an enum value outside the allowed set, a string where a number
    belongs — is dropped here, and the tool runs on its documented default
    instead. Dropping beats passing through: every argument on these tools is
    a filter with a safe default, so a discarded one widens the answer rather
    than corrupting it.
    """
    if not isinstance(arguments, dict):
        return {}
    props = (schema or {}).get("properties") or {}
    clean: dict[str, Any] = {}
    for key, value in arguments.items():
        spec = props.get(key)
        if spec is None or value is None or _is_placeholder(value):
            continue
        # `instance` names a configured EBS connector. Checked against the real
        # list rather than trusted: a wrong name is a hard tool failure, while
        # dropping it lets EBSMCP apply its own auto-selection.
        if key == "instance" and known_instances is not None:
            if not isinstance(value, str) or value.upper() not in known_instances:
                continue
            clean[key] = value.upper()
            continue
        choices = spec.get("enum")
        if choices is not None:
            if value in choices:
                clean[key] = value
            continue
        types = _prop_types(spec)
        if "integer" in types or "number" in types:
            try:
                clean[key] = int(value) if "integer" in types else float(value)
            except (TypeError, ValueError):
                continue
        elif "boolean" in types:
            if isinstance(value, bool):
                clean[key] = value
            elif isinstance(value, str) and value.lower() in ("true", "false"):
                clean[key] = value.lower() == "true"
        elif "string" in types:
            if isinstance(value, (str, int, float)):
                clean[key] = str(value)
        elif "array" in types:
            if isinstance(value, list):
                clean[key] = value
        elif not types:
            clean[key] = value
    return clean


def _resolve_choice(raw_choice: Any, menu: list[dict]) -> Optional[dict]:
    """Map whatever the model put in "tool" onto a menu entry, or None.

    Small models answer a numbered menu with its number as often as with the
    name it was asked for — llama3.2:1b returns {"tool": "1"} while getting the
    arguments exactly right. An index resolves against the same menu the model
    was shown, so honouring it admits nothing a name wouldn't; refusing it just
    discards a correct answer. Anything that doesn't land on a menu entry —
    an invented name above all — still returns None.
    """
    if raw_choice is None or isinstance(raw_choice, bool):
        return None
    if isinstance(raw_choice, int):
        return menu[raw_choice - 1] if 1 <= raw_choice <= len(menu) else None
    if not isinstance(raw_choice, str):
        return None
    text = raw_choice.strip().strip("`\"'").strip()
    if not text or text.lower() in ("null", "none"):
        return None
    for tool in menu:                                   # exact name
        if tool["name"] == text:
            return tool
    # "3", or "3. blocking_locks" — take the leading index.
    lead = re.match(r"^(\d+)\s*[.):]?\s*(.*)$", text)
    if lead:
        idx = int(lead.group(1))
        if 1 <= idx <= len(menu):
            rest = lead.group(2).strip()
            # A number paired with a name must agree, or we trust neither: a
            # model that contradicts itself in one field has told us nothing,
            # and falling through costs only a turn of NL->SQL.
            if not rest or rest == menu[idx - 1]["name"]:
                return menu[idx - 1]
        return None
    lowered = text.lower()
    return next((t for t in menu if t["name"].lower() == lowered), None)


def select_tool(question: str, menu: list[dict], *, llm, provider: str,
                model: Optional[str], api_key: Optional[str],
                base_url: Optional[str],
                known_instances: Optional[set[str]] = None) -> Optional[dict]:
    """Ask the model to pick one tool from `menu`. Returns
    {"tool": name, "arguments": {...}} or None when nothing fits.

    A name the model returns that is not on the menu is treated as None, not
    as an error to surface: an invented tool name is exactly the failure this
    whole path exists to prevent, and falling through is the safe response.
    """
    if not menu:
        return None
    raw = llm.complete_sync(
        _selection_messages(question, menu),
        provider=provider, model=model, api_key=api_key, base_url=base_url,
        max_tokens=200, temperature=0.0,
    )
    parsed = _parse_selection(raw)
    if not parsed:
        return None
    chosen = _resolve_choice(parsed.get("tool"), menu)
    if chosen is None:
        return None
    return {
        "tool": chosen["name"],
        "arguments": sanitize_arguments(parsed.get("arguments"),
                                        chosen.get("input_schema") or {},
                                        known_instances),
    }


# ── Deterministic rendering ───────────────────────────────────────────────────
# The tool payloads are shallow JSON: a dict of scalars plus, usually, one list
# of uniform row dicts. Formatting that in Python keeps every figure the user
# reads identical to what the database returned.

_MAX_ROWS = 25
_MAX_COLS = 8


def _cell(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    text = str(value)
    return text if len(text) <= 60 else text[:57] + "…"


def _table(rows: list[dict]) -> str:
    cols: list[str] = []
    for row in rows:
        for key in row:
            if key not in cols:
                cols.append(key)
    truncated_cols = cols[:_MAX_COLS]
    head = "| " + " | ".join(truncated_cols) + " |"
    rule = "| " + " | ".join("---" for _ in truncated_cols) + " |"
    body = [
        "| " + " | ".join(_cell(row.get(c)) for c in truncated_cols) + " |"
        for row in rows[:_MAX_ROWS]
    ]
    out = "\n".join([head, rule, *body])
    notes = []
    if len(rows) > _MAX_ROWS:
        notes.append(f"showing {_MAX_ROWS} of {len(rows)} rows")
    if len(cols) > _MAX_COLS:
        notes.append(f"{len(cols) - _MAX_COLS} more column(s) not shown")
    return out + (f"\n\n_{'; '.join(notes)}._" if notes else "")


def format_payload(payload: Any) -> str:
    """Render a tool result as markdown, exactly as returned."""
    if payload is None:
        return "_No data returned._"
    if not isinstance(payload, dict):
        return f"```\n{payload}\n```"

    scalars, tables = [], []
    for key, value in payload.items():
        if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
            tables.append((key, value))
        elif isinstance(value, list):
            scalars.append((key, ", ".join(_cell(v) for v in value) if value else "—"))
        elif isinstance(value, dict):
            tables.append((key, [value]))
        else:
            scalars.append((key, _cell(value)))

    parts = []
    if scalars:
        parts.append("\n".join(f"- **{k}**: {v}" for k, v in scalars))
    for key, rows in tables:
        parts.append(f"**{key}** ({len(rows)} row(s))\n\n{_table(rows)}")
    return "\n\n".join(parts) if parts else "_No data returned._"


_NARRATE_SYSTEM = (
    "You are an Oracle EBS DBA assistant. You are shown a user's question and "
    "the exact data a diagnostic tool returned for it.\n\n"
    "Write 1-3 plain sentences answering the question from that data, then stop.\n"
    "- Use only values present in the data. Never estimate, extrapolate, or add "
    "figures, names, or dates that are not shown.\n"
    "- If the data does not answer the question, say so in one sentence.\n"
    "- The full data is already displayed to the user below your sentences, so "
    "do not repeat it as a list or table.\n"
    "- No preamble, no headings, no bullet points."
)


def _has_rows(payload: Any) -> bool:
    """Whether the payload actually carries data, as opposed to a well-formed
    empty answer ({"results": [], "username": null})."""
    if not isinstance(payload, dict):
        return payload is not None
    for key, value in payload.items():
        if key in ("environment", "mapped_role", "instance", "source"):
            continue  # governance echo, present on every result
        if isinstance(value, (list, dict)):
            if value:
                return True
        elif value is not None:
            return True
    return False


# Tokens a summary may use without them appearing in the data: ordinary prose,
# plus the vocabulary of a truthful "nothing found" answer.
_NARRATE_ALLOWED = {
    "no", "none", "not", "nothing", "any", "there", "are", "is", "was", "were",
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "and", "or", "but",
    "it", "its", "this", "that", "these", "those", "data", "tool", "returned",
    "shows", "show", "found", "currently", "now", "all", "only", "one", "row",
    "rows", "record", "records", "result", "results", "does", "do", "did",
    "have", "has", "had", "be", "been", "which", "with", "from", "by", "as",
    "reports", "report", "indicates", "appears", "question", "answer", "up",
    "down", "open", "active", "running", "healthy", "ok", "environment",
}


def _ungrounded_terms(lead: str, payload: Any, question: str) -> list[str]:
    """Terms in the summary that appear neither in the data nor in the question.

    The check that makes the narration safe. A model handed an empty result
    set will cheerfully invent a plausible one — a username, a second
    tablespace, a count — and those inventions are exactly the tokens that
    have no source text behind them. Numbers and identifiers are checked;
    ordinary prose is not.
    """
    haystack = (json.dumps(payload, default=str) + " " + question + " "
                + " ".join(_NARRATE_ALLOWED)).lower()
    suspect = []
    for term in re.findall(r"[A-Za-z_][A-Za-z0-9_$#]{2,}|\d[\d,.]*", lead):
        t = term.lower().rstrip(".,;:")
        if t in _NARRATE_ALLOWED or len(t) < 3:
            continue
        if t.replace(",", "") in haystack.replace(",", ""):
            continue
        # Plain lowercase prose is not an identifier — only flag terms that
        # look like data: digits, underscores, or non-initial capitals.
        if term[0].islower() and term.isalpha() and "_" not in term:
            continue
        suspect.append(term)
    return suspect


_MAX_LEAD_CHARS = 500


def _clean_lead(text: str) -> str:
    """Trim a summary to the few sentences it was asked for, or reject it.

    Asked to describe a JSON payload, a small model will sometimes just print
    the payload back. That is not a summary, and it renders as a wall of raw
    JSON above the table that already says the same thing — so anything
    carrying the shape of data rather than prose is dropped outright.
    """
    lead = re.sub(r"```.*?```", "", text, flags=re.DOTALL).strip()
    if not lead:
        return ""
    # Structural markers mean it echoed the payload or built its own table.
    if any(marker in lead for marker in ("{", "}", '":', "| ---", "\n|")):
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", lead)
    lead = " ".join(sentences[:3]).strip()
    if len(lead) > _MAX_LEAD_CHARS:
        lead = lead[:_MAX_LEAD_CHARS].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return lead


def narrate(question: str, tool_name: str, payload: Any, *, llm, provider: str,
            model: Optional[str], api_key: Optional[str],
            base_url: Optional[str]) -> str:
    """A short lead-in over the rendered data. Returns "" whenever it cannot be
    trusted — the table below it is the actual answer, so no sentence is
    strictly better than a wrong one.

    Two refusals, both learned from watching a small local model:
    an empty result set is summarised deterministically rather than by an LLM
    (given nothing to describe, it describes something it made up), and any
    summary naming data that isn't in the payload is discarded.
    """
    if not _has_rows(payload):
        return "That tool ran and came back with nothing matching."

    blob = json.dumps(payload, indent=1, default=str)
    if len(blob) > 6000:
        blob = blob[:6000] + "\n… (truncated)"
    try:
        text = llm.complete_sync(
            [
                {"role": "system", "content": _NARRATE_SYSTEM},
                {"role": "user", "content": f"QUESTION\n{question}\n\n"
                                            f"TOOL `{tool_name}` RETURNED\n{blob}"},
            ],
            provider=provider, model=model, api_key=api_key, base_url=base_url,
            max_tokens=220, temperature=0.0,
        )
    except Exception:
        return ""
    lead = _clean_lead(text or "")
    if not lead:
        return ""
    invented = _ungrounded_terms(lead, payload, question)
    if invented:
        print(f"[EBSTools] dropped ungrounded summary for {tool_name}: {invented[:5]}")
        return ""
    return lead


async def answer_question(
    ctx, subject: str, question: str, *, llm, provider: str,
    model: Optional[str] = None, api_key: Optional[str] = None,
    base_url: Optional[str] = None, toolsets=None,
) -> Optional[dict]:
    """Route `question` to an EBS tool and run it.

    Returns None when no tool applies — not a data question, or nothing on
    the catalog fits — so the caller falls through to its existing behaviour
    (NL->SQL, then normal chat) unchanged.

    Otherwise returns {"tool", "arguments", "ok", "payload"|"error",
    "markdown"}: `markdown` is the ready-to-send reply, already carrying the
    deterministic rendering of whatever the tool returned.
    """
    # No connector means no EBS instance has usable read-only credentials
    # configured, so every tool would fail identically at the last step. Bail
    # before the catalog and the LLM call rather than spend both to say so: a
    # deployment that hasn't finished onboarding behaves exactly as it did
    # before these tools existed.
    known_instances = {name.upper() for name in (getattr(ctx, "connectors", None) or {})}
    if not known_instances:
        return None

    catalog = await list_ebs_tools(ctx, toolsets)
    menu = shortlist(question, catalog)
    if not menu:
        return None

    # complete_sync blocks, and on a local Ollama it blocks for seconds — long
    # enough to stall every other request sharing this event loop. The tool
    # call itself stays inline: these are indexed data-dictionary reads, not
    # the kind of query worth a thread hop.
    loop = asyncio.get_running_loop()
    selection = await loop.run_in_executor(None, functools.partial(
        select_tool, question, menu, llm=llm, provider=provider,
        model=model, api_key=api_key, base_url=base_url,
        known_instances=known_instances))
    if selection is None:
        return None

    result = await call_ebs_tool(ctx, subject, selection["tool"],
                                 selection["arguments"], toolsets)

    if not result.get("ok"):
        # Surface the governed pipeline's own reason — an unmapped identity,
        # an entitlement denial, an unreachable instance. Stating it beats
        # falling through to a model that would answer from imagination.
        return {
            "tool": selection["tool"], "arguments": selection["arguments"],
            "ok": False, "error": result.get("error", "unknown error"),
            "markdown": (
                f"🧰 **EBS Tools** — `{selection['tool']}` could not run.\n\n"
                f"> {result.get('error', 'unknown error')}"
            ),
        }

    payload = result.get("result")
    lead = await loop.run_in_executor(None, functools.partial(
        narrate, question, selection["tool"], payload, llm=llm,
        provider=provider, model=model, api_key=api_key, base_url=base_url))
    arg_note = ""
    if selection["arguments"]:
        arg_note = " · " + ", ".join(f"{k}=`{v}`" for k, v in selection["arguments"].items())
    markdown = (
        (lead + "\n\n" if lead else "")
        + f"🧰 **EBS Tools** — `{selection['tool']}`{arg_note}\n\n"
        + format_payload(payload)
    )
    return {
        "tool": selection["tool"], "arguments": selection["arguments"],
        "ok": True, "payload": payload, "markdown": markdown,
    }
