"""TC-ST-01..03 — instant small-talk intent classifier (no LLM call).

Forces the exact-match fallback (``_protos = False``) so the conservative gating
is tested deterministically without loading the embedding model.
"""
import pytest
from app.core import smalltalk


@pytest.fixture(autouse=True)
def _force_fallback(monkeypatch):
    # Skip the embedding model; exercise the exact-match degraded path.
    monkeypatch.setattr(smalltalk, "_protos", False)
    yield
    smalltalk._protos = None  # reset cache for any later real use


def test_greetings_and_smalltalk_match():
    """TC-ST-01: greetings / thanks / capabilities get an instant templated reply."""
    for msg in ["hi", "hello", "thanks", "thank you", "bye", "what can you do"]:
        assert smalltalk.match(msg), msg


def test_real_questions_pass_through():
    """TC-ST-02: genuine (even short) EBS questions are NOT hijacked → None."""
    for msg in ["what is FNDLOAD", "compile xxap_pkg", "adop status", "list invalid objects"]:
        assert smalltalk.match(msg) is None, msg


def test_long_messages_ignored():
    """TC-ST-03: anything beyond a few words is never treated as small-talk."""
    assert smalltalk.match("hi there, can you help me run adop phase apply today please") is None
    assert smalltalk.match("") is None
