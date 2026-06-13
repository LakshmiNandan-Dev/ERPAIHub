"""
LLM Guard — input and output content-safety scanning.

Scanners are loaded lazily on first use (HuggingFace transformer models are
cached locally after the first download — no internet required on subsequent
runs, fully compatible with an air-gapped Ollama deployment).

All scanner failures are NON-FATAL: if llm-guard is not installed, a model
download fails, or any scanner raises, the request continues normally and the
error is logged.  This ensures LLM Guard never takes down the chat service.

Configuration (environment variables):
  LLM_GUARD_ENABLED           true / false  (default: false)
  LLM_GUARD_INPUT_SCANNERS    comma-separated list  (default below)
  LLM_GUARD_OUTPUT_SCANNERS   comma-separated list  (default below)
  LLM_GUARD_MAX_TOKENS        max input token budget (default: 2048)

Available scanner names:
  Input:  prompt_injection, secrets, token_limit, invisible_text, anonymize,
          ban_topics, gibberish, language
  Output: sensitive, no_refusal, relevance, toxicity, bias, language_same
"""

import os
import logging
import threading
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

_ENABLED = os.getenv("LLM_GUARD_ENABLED", "false").lower() in ("1", "true", "yes")

_INPUT_NAMES: list[str] = [
    s.strip()
    for s in os.getenv(
        "LLM_GUARD_INPUT_SCANNERS",
        "prompt_injection,secrets,token_limit,invisible_text",
    ).split(",")
    if s.strip()
]

_OUTPUT_NAMES: list[str] = [
    s.strip()
    for s in os.getenv(
        "LLM_GUARD_OUTPUT_SCANNERS",
        "sensitive,no_refusal",
    ).split(",")
    if s.strip()
]

_MAX_TOKENS = int(os.getenv("LLM_GUARD_MAX_TOKENS", "2048"))

_lock = threading.Lock()
_input_scanners:  Optional[list] = None
_output_scanners: Optional[list] = None


# ── Scanner factory ────────────────────────────────────────────────────────────

def _build_input_scanners() -> list:
    scanners = []
    for name in _INPUT_NAMES:
        try:
            if name == "prompt_injection":
                from llm_guard.input_scanners import PromptInjection
                from llm_guard.input_scanners.prompt_injection import MatchType
                scanners.append(PromptInjection(match_type=MatchType.FULL))

            elif name == "secrets":
                from llm_guard.input_scanners import Secrets
                scanners.append(Secrets())

            elif name == "token_limit":
                from llm_guard.input_scanners import TokenLimit
                scanners.append(TokenLimit(limit=_MAX_TOKENS))

            elif name == "invisible_text":
                from llm_guard.input_scanners import InvisibleText
                scanners.append(InvisibleText())

            elif name == "anonymize":
                from llm_guard.input_scanners import Anonymize
                scanners.append(Anonymize(recognizer_conf={"DEFAULT": ["en"]}))

            elif name == "ban_topics":
                topics = [t.strip() for t in os.getenv("LLM_GUARD_BAN_TOPICS", "").split(",") if t.strip()]
                if topics:
                    from llm_guard.input_scanners import BanTopics
                    scanners.append(BanTopics(topics=topics, threshold=0.5))
                else:
                    log.warning("[LLMGuard] 'ban_topics' scanner enabled but LLM_GUARD_BAN_TOPICS is empty — skipped")

            elif name == "gibberish":
                from llm_guard.input_scanners import Gibberish
                scanners.append(Gibberish())

            elif name == "language":
                from llm_guard.input_scanners import Language
                scanners.append(Language(valid_languages=["en"]))

            else:
                log.warning("[LLMGuard] Unknown input scanner name: '%s' — skipped", name)

            log.info("[LLMGuard] Input scanner loaded: %s", name)

        except ImportError:
            log.warning("[LLMGuard] llm-guard not installed — scanner '%s' skipped. "
                        "Install with: pip install llm-guard", name)
        except Exception as exc:
            log.warning("[LLMGuard] Failed to load input scanner '%s': %s", name, exc)

    return scanners


def _build_output_scanners() -> list:
    scanners = []
    for name in _OUTPUT_NAMES:
        try:
            if name == "sensitive":
                from llm_guard.output_scanners import Sensitive
                scanners.append(Sensitive())

            elif name == "no_refusal":
                from llm_guard.output_scanners import NoRefusal
                scanners.append(NoRefusal())

            elif name == "relevance":
                from llm_guard.output_scanners import Relevance
                scanners.append(Relevance())

            elif name == "toxicity":
                from llm_guard.output_scanners import Toxicity
                scanners.append(Toxicity())

            elif name == "bias":
                from llm_guard.output_scanners import Bias
                scanners.append(Bias())

            elif name == "language_same":
                from llm_guard.output_scanners import LanguageSame
                scanners.append(LanguageSame())

            else:
                log.warning("[LLMGuard] Unknown output scanner name: '%s' — skipped", name)

            log.info("[LLMGuard] Output scanner loaded: %s", name)

        except ImportError:
            log.warning("[LLMGuard] llm-guard not installed — scanner '%s' skipped.", name)
        except Exception as exc:
            log.warning("[LLMGuard] Failed to load output scanner '%s': %s", name, exc)

    return scanners


def _get_input_scanners() -> list:
    global _input_scanners
    if _input_scanners is None:
        with _lock:
            if _input_scanners is None:   # double-checked locking
                log.info("[LLMGuard] Initialising input scanners: %s", _INPUT_NAMES)
                _input_scanners = _build_input_scanners()
                log.info("[LLMGuard] %d input scanner(s) ready", len(_input_scanners))
    return _input_scanners


def _get_output_scanners() -> list:
    global _output_scanners
    if _output_scanners is None:
        with _lock:
            if _output_scanners is None:
                log.info("[LLMGuard] Initialising output scanners: %s", _OUTPUT_NAMES)
                _output_scanners = _build_output_scanners()
                log.info("[LLMGuard] %d output scanner(s) ready", len(_output_scanners))
    return _output_scanners


# ── Result type ────────────────────────────────────────────────────────────────

@dataclass
class GuardResult:
    """Returned by scan_input / scan_output."""
    text:       str                        # sanitized / original text
    is_valid:   bool        = True         # False → request should be blocked
    scanner:    str         = ""           # first scanner that tripped
    risk_score: float       = 0.0          # 0.0 (clean) → 1.0 (certain threat)
    scores:     dict        = field(default_factory=dict)   # per-scanner scores

    @property
    def blocked(self) -> bool:
        return not self.is_valid


# ── Public API ─────────────────────────────────────────────────────────────────

def scan_input(prompt: str) -> GuardResult:
    """
    Run all enabled input scanners against the user's message.

    Returns a GuardResult whose .blocked property is True when any scanner
    trips (prompt injection, secret leak, token overflow, invisible text, …).
    The .text field holds the sanitized version (relevant for Anonymize which
    replaces PII with placeholders instead of blocking).
    """
    if not _ENABLED:
        return GuardResult(text=prompt)

    scanners = _get_input_scanners()
    if not scanners:
        return GuardResult(text=prompt)

    try:
        from llm_guard import scan_prompt
        sanitized, results_valid, results_score = scan_prompt(scanners, prompt)

        if not all(results_valid.values()):
            failing = [k for k, v in results_valid.items() if not v]
            top_score = max((results_score.get(k, 1.0) for k in failing), default=1.0)
            log.warning("[LLMGuard] Input BLOCKED by %s (score=%.2f)", failing[0], top_score)
            return GuardResult(
                text=sanitized,
                is_valid=False,
                scanner=failing[0],
                risk_score=float(top_score),
                scores=dict(results_score),
            )

        return GuardResult(
            text=sanitized,
            is_valid=True,
            scores=dict(results_score),
        )

    except Exception as exc:
        log.error("[LLMGuard] scan_input raised (non-fatal, continuing): %s", exc)
        return GuardResult(text=prompt)


def scan_output(prompt: str, response: str) -> GuardResult:
    """
    Run all enabled output scanners against the assembled LLM response.

    Returns a GuardResult whose .blocked property is True when any output
    scanner trips (sensitive PII in response, unexpected refusal, etc.).
    """
    if not _ENABLED:
        return GuardResult(text=response)

    scanners = _get_output_scanners()
    if not scanners:
        return GuardResult(text=response)

    try:
        from llm_guard import scan_output as _llmg_scan_output
        sanitized, results_valid, results_score = _llmg_scan_output(scanners, prompt, response)

        if not all(results_valid.values()):
            failing = [k for k, v in results_valid.items() if not v]
            top_score = max((results_score.get(k, 1.0) for k in failing), default=1.0)
            log.warning("[LLMGuard] Output FILTERED by %s (score=%.2f)", failing[0], top_score)
            return GuardResult(
                text=sanitized,
                is_valid=False,
                scanner=failing[0],
                risk_score=float(top_score),
                scores=dict(results_score),
            )

        return GuardResult(
            text=sanitized,
            is_valid=True,
            scores=dict(results_score),
        )

    except Exception as exc:
        log.error("[LLMGuard] scan_output raised (non-fatal, continuing): %s", exc)
        return GuardResult(text=response)


def status() -> dict:
    """Return a dict describing the current guard configuration (for /admin/llm-guard)."""
    return {
        "enabled": _ENABLED,
        "input_scanners": _INPUT_NAMES,
        "output_scanners": _OUTPUT_NAMES,
        "max_tokens": _MAX_TOKENS,
        "scanners_initialised": _input_scanners is not None or _output_scanners is not None,
    }
