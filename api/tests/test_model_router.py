"""TC-RT-01 to TC-RT-09 — Per-agent LLM routing + complexity classifier."""
from types import SimpleNamespace
import pytest

from app.core.llm import model_router as mr


def _db_with(policy):
    """A minimal stand-in DB whose query(...).filter(...).first() returns `policy`."""
    class _Q:
        def filter(self, *a, **k): return self
        def first(self): return policy

    class _DB:
        def query(self, *a, **k): return _Q()

    return _DB()


def _policy(**kw):
    base = dict(provider=None, small_model=None, large_model=None,
                routing_enabled=True, force_tier=None, is_active=True)
    base.update(kw)
    return SimpleNamespace(**base)


class TestComplexityClassifier:

    @pytest.mark.parametrize("text,tier", [
        ("what is FNDLOAD?", "small"),
        ("list all environments", "small"),
        ("hi", "small"),
        ("", "small"),
        ("analyze why this AWR report shows high buffer waits and recommend fixes", "large"),
        ("design a migration plan and compare the trade-offs step by step", "large"),
    ])
    def test_classification(self, text, tier):
        got, _reason = mr.classify_complexity(text)
        assert got == tier


class TestRouting:

    def test_auto_complex_routes_large(self):
        # "auto" opts a request into complexity routing without a policy.
        prov, model, reason = mr.resolve(_db_with(None), "chat", "openai", "auto",
                                         "analyze and optimize this query plan")
        assert (prov, model) == ("openai", "gpt-4o")
        assert reason.startswith("large")

    def test_auto_simple_routes_small(self):
        prov, model, reason = mr.resolve(_db_with(None), "chat", "openai", "auto", "what is adop?")
        assert model == "gpt-4o-mini" and reason.startswith("small")

    def test_no_policy_no_auto_is_passthrough(self):
        # A bare route_text must NOT route when there's no policy and no "auto".
        _, model, reason = mr.resolve(_db_with(None), "chat", "openai", None,
                                      "analyze and optimize this query plan")
        assert model is None and reason == "no-route"

    def test_explicit_model_wins(self):
        prov, model, reason = mr.resolve(_db_with(None), "chat", "openai", "gpt-4o", "what is x?")
        assert model == "gpt-4o" and reason == "explicit"

    def test_provider_override_with_force_tier(self):
        pol = _policy(provider="anthropic", force_tier="large")
        prov, model, reason = mr.resolve(_db_with(pol), "performance", "ollama", "llama3.1:8b", "x")
        assert (prov, model) == ("anthropic", "claude-sonnet-4-6")
        assert reason == "large(forced)"

    def test_custom_tier_models(self):
        pol = _policy(small_model="phi3:mini", large_model="llama3.1:70b")
        prov, model, _ = mr.resolve(_db_with(pol), "chat", "ollama", "auto", "show status")
        assert (prov, model) == ("ollama", "phi3:mini")

    def test_routing_disabled_passthrough(self):
        pol = _policy(routing_enabled=False)
        _, model, reason = mr.resolve(_db_with(pol), "chat", "openai", None, "analyze deeply")
        assert model is None and reason == "no-route"

    def test_legacy_passthrough_when_no_route_text(self):
        # Back-compat: no explicit model, no route_text, no policy → untouched.
        _, model, reason = mr.resolve(_db_with(None), "chat", "openai", None, None)
        assert model is None and reason == "no-route"
