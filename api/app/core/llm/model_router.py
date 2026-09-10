"""
Per-agent LLM routing + complexity-based model selection.

Two independent capabilities, resolved together:

  1. Per-agent provider/model — an admin AgentLlmPolicy row lets each agent
     (chat | deployment | performance | cloning | knowledge_base | patching)
     use its own provider and a small/large model pair.

  2. Complexity routing — a fast, free heuristic reads the task text/intent and
     routes simple lookups to the agent's *small* model and complex reasoning to
     its *large* model. No extra LLM call, so it never adds cost to the request
     it is trying to make cheaper.

Routing only engages when the caller hasn't pinned a concrete model — i.e. the
requested model is empty or the sentinel ``"auto"``. An explicit model always
wins (unless a policy forces a *different* provider, in which case the request
model belongs to the wrong provider and the tier model is used instead).

Everything degrades safely: no policy row, routing disabled, or an unknown
provider all fall back to leaving the model untouched.
"""
import os
import re
from typing import Optional, Tuple

# Global kill-switch. When off, this module is a no-op passthrough.
ROUTING_ENABLED = os.getenv("LLM_ROUTING_ENABLED", "1").lower() not in ("0", "false", "no")
# Complexity score at/above which a task is routed to the large model.
LARGE_THRESHOLD = int(os.getenv("LLM_ROUTING_LARGE_THRESHOLD", "2"))

KNOWN_AGENTS = ["chat", "deployment", "performance", "cloning", "knowledge_base", "patching", "hcm"]


def _tier_default(provider: str, tier: str, fallback: str) -> str:
    """Per-provider tier default, overridable via LLM_TIER_<PROVIDER>_<TIER> env."""
    env = os.getenv(f"LLM_TIER_{provider.upper()}_{tier.upper()}")
    return env or fallback


# Built-in small/large defaults per provider. "small" mirrors each provider's
# historical default (the cheap/fast model); "large" is the more capable tier.
# All overridable via env so a deployment can match its own model availability.
def _default_tiers(provider: str) -> dict:
    table = {
        "ollama": {
            # Ollama large defaults to the same as small so routing is a safe
            # no-op until an admin sets a heavier local model they can actually run.
            "small": _tier_default("ollama", "small", os.getenv("OLLAMA_DEFAULT_MODEL", "llama3.1:8b")),
            "large": _tier_default("ollama", "large", os.getenv("OLLAMA_DEFAULT_MODEL", "llama3.1:8b")),
        },
        "openai": {
            "small": _tier_default("openai", "small", "gpt-4o-mini"),
            "large": _tier_default("openai", "large", "gpt-4o"),
        },
        "anthropic": {
            "small": _tier_default("anthropic", "small", "claude-haiku-4-5-20251001"),
            "large": _tier_default("anthropic", "large", "claude-sonnet-4-6"),
        },
        "gemini": {
            "small": _tier_default("gemini", "small", "gemini-2.0-flash"),
            "large": _tier_default("gemini", "large", "gemini-2.5-pro"),
        },
    }
    return table.get(provider, {})


# ── Heuristic complexity classifier ─────────────────────────────────────────────

# Signals that a task needs deeper reasoning → large model.
_COMPLEX_KEYWORDS = (
    "analyze", "analyse", "root cause", "why ", "optimize", "optimise", "compare",
    "design", "architecture", "plan ", "troubleshoot", "debug", "diagnose",
    "trade-off", "tradeoff", "recommend", "strategy", "review", "refactor",
    "migrate", "performance", "step by step", "step-by-step", "explain in detail",
    "investigate", "in depth", "in-depth", "evaluate", "rewrite",
)
# Signals of a quick lookup/transform → small model.
_SIMPLE_KEYWORDS = (
    "what is", "what's", "define", "definition", "list ", "show ", "status",
    "when is", "who is", "where is", "yes or no", "translate", "format ",
    "spell", "how many", "rename", "summarize briefly",
)
# Code / SQL / EBS tooling signals (multi-step execution → lean large).
_CODE_SIGNALS = (
    "select ", "create ", "alter ", "```", "pl/sql", "procedure", "function ",
    " join ", "fndload", "frmcmp", "wfload", "adop", "sqlplus",
)


def classify_complexity(text: Optional[str]) -> Tuple[str, str]:
    """
    Return (tier, reason) where tier is 'small' or 'large'. Pure heuristic:
    length + intent keywords + code/multi-step signals. Deterministic and cheap.
    """
    t = (text or "").strip()
    if not t:
        return "small", "empty"

    low = t.lower()
    words = len(t.split())
    score = 0
    reasons = []

    if words > 120:
        score += 2
        reasons.append("very-long")
    elif words > 40:
        score += 1
        reasons.append("long")

    complex_hits = [k.strip() for k in _COMPLEX_KEYWORDS if k in low]
    if complex_hits:
        score += len(complex_hits)
        reasons.append("complex:" + ",".join(complex_hits[:3]))

    simple_hits = [k.strip() for k in _SIMPLE_KEYWORDS if k in low]
    if simple_hits and not complex_hits:
        score -= 1
        reasons.append("lookup:" + ",".join(simple_hits[:2]))

    if any(s in low for s in _CODE_SIGNALS):
        score += 1
        reasons.append("code/sql")

    # Multiple questions or an enumerated/sequenced request → multi-part.
    if t.count("?") >= 2 or re.search(r"\b(1\.|2\.|first,|then |finally)\b", low):
        score += 1
        reasons.append("multi-part")

    tier = "large" if score >= LARGE_THRESHOLD else "small"
    return tier, f"score={score};" + (";".join(reasons) or "baseline")


# ── Policy resolution ───────────────────────────────────────────────────────────

def _load_policy(db, agent: Optional[str]):
    """Return the active AgentLlmPolicy row for an agent, or None."""
    if not agent or db is None:
        return None
    try:
        from app import models
        return (
            db.query(models.AgentLlmPolicy)
            .filter(
                models.AgentLlmPolicy.agent == agent,
                models.AgentLlmPolicy.is_active == True,  # noqa: E712
            )
            .first()
        )
    except Exception:
        return None


def resolve(
    db,
    agent: Optional[str],
    provider: str,
    requested_model: Optional[str],
    route_text: Optional[str],
) -> Tuple[str, Optional[str], str]:
    """
    Decide the effective (provider, model) for a request, returning
    (provider, model, reason). ``reason`` is a short human-readable trace for
    logging. Leaves model as ``requested_model`` whenever routing doesn't apply.
    """
    policy = _load_policy(db, agent)

    # 1. Per-agent provider override (independent of complexity routing).
    eff_provider = (policy.provider if policy and policy.provider else provider) or provider
    provider_overridden = bool(policy and policy.provider and policy.provider != provider)

    requested = (requested_model or "").strip()
    is_auto = requested.lower() == "auto"
    has_explicit = bool(requested) and not is_auto

    # 2. Explicit model wins — unless the policy forced a *different* provider,
    #    in which case the request's model is for the wrong provider.
    if has_explicit and not provider_overridden:
        return eff_provider, requested_model, "explicit"

    # 3. Decide whether to route at all. Engage ONLY on explicit opt-in:
    #    model == "auto", or a per-agent policy the admin configured. A bare
    #    route_text must NOT override the caller's model / credential default —
    #    otherwise routing would change the model for every request even when no
    #    routing was set up (and could pick a model that isn't installed).
    if policy is not None:
        routing_on = ROUTING_ENABLED and policy.routing_enabled
        wants_route = True
    else:
        routing_on = ROUTING_ENABLED
        wants_route = is_auto
    if not (routing_on and wants_route):
        # Leave the model untouched (provider override, if any, still returned).
        return eff_provider, requested_model, "no-route"

    tiers = _default_tiers(eff_provider)
    if not tiers:
        return eff_provider, requested_model, "unknown-provider"
    small = (policy.small_model if policy and policy.small_model else None) or tiers["small"]
    large = (policy.large_model if policy and policy.large_model else None) or tiers["large"]

    # 4. Pick the tier: an explicit force wins; else classify the task text.
    if policy and policy.force_tier in ("small", "large"):
        tier, reason = policy.force_tier, "forced"
    else:
        tier, reason = classify_complexity(route_text)

    model = large if tier == "large" else small
    return eff_provider, model, f"{tier}({reason})"
