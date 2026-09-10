"""EBS tool selection — the path that replaced generated SQL with reviewed tools.

These tests pin the guarantees the design rests on, all of which hold no
matter how badly the model behaves: an invented tool name never runs, an
invented argument never reaches the database, a summary that names data the
tool didn't return is thrown away, and a question no tool covers falls
through untouched so NL->SQL still sees it.

No database, no EBS, no LLM: the catalog is a fixture and the model is a
stub that returns whatever a test wants to simulate.
"""
import pytest

from app.core import ebs_tool_selector as sel


CATALOG = [
    {
        "name": "concurrent_requests",
        "description": "List concurrent requests matching status (running, pending, "
                       "on_hold, completed, or failed), optionally narrowed to one "
                       "requestor's EBS username or program.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "default": "running",
                           "enum": ["running", "pending", "on_hold", "completed", "failed"]},
                "requested_by": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
                "instance": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
                "limit": {"type": "integer", "default": 50},
            },
        },
    },
    {
        "name": "tablespace_health",
        "description": "Tablespace free space, used percentage and autoextend headroom.",
        "input_schema": {"type": "object", "properties": {
            "instance": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}}},
    },
    {
        "name": "sga_status",
        "description": "SGA memory pool sizes and advisories.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


class StubLLM:
    """Stands in for llm_service.complete_sync, returning canned replies."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def complete_sync(self, messages, **kwargs):
        self.calls.append(messages)
        return self.replies.pop(0) if self.replies else ""


def _select(reply, question="show me failed concurrent requests", menu=None):
    return sel.select_tool(question, menu if menu is not None else CATALOG,
                           llm=StubLLM(reply), provider="ollama", model="m",
                           api_key=None, base_url=None)


# ── Routing ──────────────────────────────────────────────────────────────────

def test_shortlist_ranks_the_matching_tool_first():
    names = [t["name"] for t in sel.shortlist("which concurrent requests are failed", CATALOG)]
    assert names[0] == "concurrent_requests"


def test_shortlist_is_empty_for_an_unrelated_question():
    # The cheap intent gate: an off-topic turn must never reach an LLM call.
    assert sel.shortlist("how do I write a python decorator", CATALOG) == []
    assert sel.shortlist("thanks!", CATALOG) == []


def test_shortlist_drops_candidates_far_behind_the_leader():
    menu = sel.shortlist("tablespace free space", CATALOG)
    assert menu and menu[0]["name"] == "tablespace_health"
    top = sel._score(sel._tokens("tablespace free space"), menu[0])
    assert all(sel._score(sel._tokens("tablespace free space"), t) >= 0.5 * top for t in menu)


# ── The model's choice is validated, never trusted ───────────────────────────

def test_invented_tool_name_is_refused():
    assert _select('{"tool": "ebs_query_database", "arguments": {}}') is None


def test_explicit_null_falls_through():
    assert _select('{"tool": null}') is None


def test_unparseable_reply_falls_through():
    assert _select("I think you should look at the concurrent manager.") is None


def test_menu_index_resolves_to_that_tool():
    # llama3.2:1b answers a numbered menu with its number; the index maps onto
    # the same menu the model was shown, so it admits nothing a name wouldn't.
    got = _select('{"tool": "1", "arguments": {"status": "failed"}}')
    assert got == {"tool": "concurrent_requests", "arguments": {"status": "failed"}}


def test_out_of_range_index_is_refused():
    assert _select('{"tool": "99", "arguments": {}}') is None


def test_index_and_name_that_disagree_are_refused():
    assert _select('{"tool": "1. sga_status"}') is None


def test_json_in_a_code_fence_is_accepted():
    got = _select('```json\n{"tool": "sga_status"}\n```')
    assert got["tool"] == "sga_status"


# ── Arguments are filtered against the tool's own schema ─────────────────────

@pytest.mark.parametrize("bad", [
    {"instance": "?"}, {"instance": "all"}, {"instance": "<instance>"},
    {"requested_by": "n/a"}, {"requested_by": ""},
])
def test_placeholder_arguments_are_dropped(bad):
    assert sel.sanitize_arguments(bad, CATALOG[0]["input_schema"]) == {}


def test_value_outside_an_enum_is_dropped():
    assert sel.sanitize_arguments({"status": "maybe"}, CATALOG[0]["input_schema"]) == {}


def test_argument_not_in_the_schema_is_dropped():
    assert sel.sanitize_arguments({"table_name": "fnd_user"}, CATALOG[0]["input_schema"]) == {}


def test_valid_arguments_survive_and_coerce():
    got = sel.sanitize_arguments(
        {"status": "failed", "requested_by": "jdoe", "limit": "20"},
        CATALOG[0]["input_schema"])
    assert got == {"status": "failed", "requested_by": "jdoe", "limit": 20}


def test_unconfigured_instance_is_dropped_and_known_one_normalised():
    schema = CATALOG[0]["input_schema"]
    assert sel.sanitize_arguments({"instance": "PROD"}, schema, {"DEV"}) == {}
    assert sel.sanitize_arguments({"instance": "dev"}, schema, {"DEV"}) == {"instance": "DEV"}


# ── The summary is checked against the data it describes ─────────────────────

def test_empty_result_is_summarised_without_the_model():
    # Given nothing to describe, a small model describes something it invented.
    llm = StubLLM("Failed login attempts include username 'admin'.")
    lead = sel.narrate("any failed logins?", "failed_login_attempts",
                       {"environment": "prod", "mapped_role": "Senior DBA",
                        "username": None, "results": []},
                       llm=llm, provider="ollama", model="m", api_key=None, base_url=None)
    assert "admin" not in lead
    assert llm.calls == []


def test_summary_naming_absent_data_is_discarded():
    payload = {"temp_tablespaces": [{"tablespace_name": "TEMP", "free_mb": 0}]}
    lead = sel.narrate("which tablespaces are full?", "temp_usage", payload,
                       llm=StubLLM("TEMP and TEMP2 are both full."),
                       provider="ollama", model="m", api_key=None, base_url=None)
    assert lead == ""


def test_grounded_summary_is_kept():
    payload = {"temp_tablespaces": [{"tablespace_name": "TEMP", "free_mb": 0}]}
    lead = sel.narrate("which tablespaces are full?", "temp_usage", payload,
                       llm=StubLLM("TEMP has no free space left."),
                       provider="ollama", model="m", api_key=None, base_url=None)
    assert lead == "TEMP has no free space left."


def test_echoed_payload_is_not_used_as_a_summary():
    payload = {"results": [{"name": "Fixed Size", "value_mb": 4.7}]}
    lead = sel.narrate("sga?", "sga_status", payload,
                       llm=StubLLM('{"results": [{"name": "Fixed Size", "value_mb": 4.7}]}'),
                       provider="ollama", model="m", api_key=None, base_url=None)
    assert lead == ""


# ── Rendering comes from the payload, not the model ──────────────────────────

def test_rows_render_as_a_table_of_exactly_what_was_returned():
    md = sel.format_payload({
        "summary": "2 failed request(s)",
        "requests": [{"request_id": 1, "program_name": "Autoinvoice"},
                     {"request_id": 2, "program_name": "Gather Stats"}],
    })
    assert "**summary**: 2 failed request(s)" in md
    assert "| request_id | program_name |" in md
    assert "| 1 | Autoinvoice |" in md
    assert "| 2 | Gather Stats |" in md


def test_long_result_sets_are_truncated_and_say_so():
    md = sel.format_payload({"rows": [{"n": i} for i in range(60)]})
    assert "showing 25 of 60 rows" in md
