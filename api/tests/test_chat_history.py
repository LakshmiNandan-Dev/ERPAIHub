"""TC-CHAT-HIST — LLM history assembly (orphaned-turn handling after a failed reply)."""
from types import SimpleNamespace
from app.platform_api.chat import _build_ollama_messages


def _m(role, content):
    return SimpleNamespace(role=role, content=content)


def _user_texts(out):
    return [m["content"] for m in out if m["role"] == "user"]


def test_orphaned_user_turn_dropped_after_failure():
    """A previous turn whose generation failed leaves a user message with no
    assistant reply. It must NOT be sent on the next turn (else the model answers
    the old question)."""
    msgs = [_m("user", "OLD question one"), _m("user", "NEW question two")]
    out = _build_ollama_messages(msgs, "NEW question two", rag_context="", context_block="")
    joined = " ".join(_user_texts(out))
    assert "OLD question one" not in joined
    assert "NEW question two" in joined


def test_multiple_orphans_collapse_to_current():
    msgs = [_m("user", "fail A"), _m("user", "fail B"), _m("user", "current C")]
    out = _build_ollama_messages(msgs, "current C", rag_context="", context_block="")
    joined = " ".join(_user_texts(out))
    assert "fail A" not in joined and "fail B" not in joined
    assert "current C" in joined


def test_healthy_history_preserved():
    """Normal alternating history is left intact."""
    msgs = [_m("user", "Q1"), _m("assistant", "A1"), _m("user", "Q2")]
    out = _build_ollama_messages(msgs, "Q2", rag_context="", context_block="")
    contents = " ".join(m["content"] for m in out)
    assert "Q1" in contents and "A1" in contents and "Q2" in contents


def test_rag_context_attaches_to_current_question():
    """RAG context grounds the current (last) message, not an orphan."""
    msgs = [_m("user", "orphan old"), _m("user", "real question")]
    out = _build_ollama_messages(msgs, "real question", rag_context="KB SNIPPET", context_block="")
    # The grounded block must wrap the current question.
    grounded = [m["content"] for m in out if "KB SNIPPET" in m["content"]]
    assert len(grounded) == 1
    assert "real question" in grounded[0]
    assert "orphan old" not in grounded[0]


def test_no_context_guard_on_substantive_miss():
    """Substantive query with no KB hit → anti-fabrication guard on the question."""
    msgs = [_m("user", "How do I compile a package?")]
    out = _build_ollama_messages(msgs, "How do I compile a package?",
                                 rag_context="", context_block="", no_context_guard=True)
    last = out[-1]["content"]
    assert "No knowledge-base entry was found" in last
    assert "do NOT invent" in last.lower() or "do not invent" in last.lower()
    assert "How do I compile a package?" in last


def test_simple_query_left_bare():
    """A simple query (no guard) is sent verbatim — no grounding noise on 'hi'."""
    msgs = [_m("user", "hi")]
    out = _build_ollama_messages(msgs, "hi", rag_context="", context_block="", no_context_guard=False)
    assert out[-1]["content"] == "hi"
