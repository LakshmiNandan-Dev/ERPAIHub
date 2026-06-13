"""
Unified LLM service — supports Ollama (local), OpenAI, and Anthropic.
Async streaming for chat endpoints; sync completion for background tasks (RLAIF).
"""
import os
import httpx
import json
from typing import AsyncGenerator, Optional

from app import llm_cache

_OLLAMA_BASE = os.getenv("OLLAMA_URL", "http://localhost:11434")
# Keep the model resident between requests so each call skips a cold reload and
# can reuse the previous prompt's KV cache. Mirrors the OLLAMA_KEEP_ALIVE server
# default; set per-request as belt-and-suspenders. "30m", "-1" (forever), etc.
_OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "30m")

# Single source of truth for each provider's default model. The local Ollama
# default is env-driven so a deployment can pick a model that fits its hardware
# (e.g. OLLAMA_DEFAULT_MODEL=qwen2.5:7b) without code changes.
DEFAULT_MODELS = {
    "ollama":    os.getenv("OLLAMA_DEFAULT_MODEL", "llama3.1:8b"),
    "openai":    "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5-20251001",
    "gemini":    "gemini-2.0-flash",
}

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"


def _to_gemini(messages: list) -> tuple[Optional[dict], list]:
    """Convert OpenAI-style messages to Gemini (system_instruction, contents)."""
    system_text = ""
    contents = []
    for m in messages:
        role = m.get("role")
        text = m.get("content", "")
        if role == "system":
            system_text += (("\n" if system_text else "") + text)
        else:
            gem_role = "model" if role == "assistant" else "user"
            contents.append({"role": gem_role, "parts": [{"text": text}]})
    system = {"parts": [{"text": system_text}]} if system_text else None
    return system, contents


# ── Async streaming ────────────────────────────────────────────────────────────

async def stream_tokens(
    messages: list,
    provider: str = "ollama",
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    usage: Optional[dict] = None,
) -> AsyncGenerator[str, None]:
    """
    Yield raw text tokens from the chosen provider.

    If a mutable `usage` dict is supplied it is populated with provider-reported
    token counts ("prompt_tokens" / "completion_tokens") as they arrive.
    """
    model = model or DEFAULT_MODELS.get(provider, "llama3.2:1b")

    if provider == "openai":
        async for t in _stream_openai(messages, model, api_key, usage):
            yield t
    elif provider == "anthropic":
        async for t in _stream_anthropic(messages, model, api_key, usage):
            yield t
    elif provider == "gemini":
        async for t in _stream_gemini(messages, model, api_key, usage):
            yield t
    else:
        async for t in _stream_ollama(messages, model, base_url or _OLLAMA_BASE, usage):
            yield t


# ── Error surfacing helpers ───────────────────────────────────────────────────

class LLMError(RuntimeError):
    """Raised when an LLM provider is unreachable, unauthorized, or the model is unavailable."""


def _extract_error_message(body: str) -> str:
    """Pull a human-readable message out of a provider error body."""
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return body.strip()[:300]
    err = data.get("error", data)
    if isinstance(err, dict):
        return err.get("message") or err.get("type") or err.get("status") or json.dumps(err)[:300]
    if isinstance(err, str):
        return err
    return data.get("message", "")[:300]


async def _raise_if_http_error(provider: str, resp):
    """For a streamed response, raise a descriptive LLMError on a 4xx/5xx status."""
    if resp.status_code >= 400:
        try:
            body = (await resp.aread()).decode("utf-8", "replace")
        except Exception:
            body = ""
        detail = _extract_error_message(body) or "no details returned"
        raise LLMError(f"{provider} API error (HTTP {resp.status_code}): {detail}")


async def _stream_ollama(messages, model, base_url, usage=None):
    url = f"{base_url.rstrip('/')}/api/chat"
    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            async with client.stream(
                "POST", url,
                json={"model": model, "messages": messages, "stream": True,
                      "keep_alive": _OLLAMA_KEEP_ALIVE}
            ) as resp:
                await _raise_if_http_error("Ollama", resp)
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    # Ollama reports a missing model as HTTP 200 + an inline error line.
                    if chunk.get("error"):
                        raise LLMError(f"Ollama: {chunk['error']}")
                    token = chunk.get("message", {}).get("content", "")
                    if token:
                        yield token
                    if chunk.get("done", False):
                        if usage is not None:
                            if chunk.get("prompt_eval_count") is not None:
                                usage["prompt_tokens"] = chunk["prompt_eval_count"]
                            if chunk.get("eval_count") is not None:
                                usage["completion_tokens"] = chunk["eval_count"]
                        break
    except httpx.ConnectError:
        raise LLMError(
            f"Cannot reach Ollama at {base_url}. Is the Ollama server running and the model pulled?"
        )
    except httpx.HTTPError as exc:
        raise LLMError(f"Ollama connection failed: {exc}")


async def _stream_openai(messages, model, api_key, usage=None):
    if not api_key:
        raise LLMError("No OpenAI API key configured. Ask an administrator to add an OpenAI key in the Admin Console.")
    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            async with client.stream(
                "POST",
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": model, "messages": messages, "stream": True,
                      "stream_options": {"include_usage": True}},
            ) as resp:
                await _raise_if_http_error("OpenAI", resp)
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    raw = line[6:].strip()
                    if raw == "[DONE]":
                        break
                    try:
                        chunk = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("error"):
                        raise LLMError(f"OpenAI: {_extract_error_message(raw)}")
                    u = chunk.get("usage")
                    if u and usage is not None:
                        usage["prompt_tokens"] = u.get("prompt_tokens")
                        usage["completion_tokens"] = u.get("completion_tokens")
                    choices = chunk.get("choices") or []
                    if choices:
                        token = choices[0].get("delta", {}).get("content", "")
                        if token:
                            yield token
    except httpx.HTTPError as exc:
        raise LLMError(f"Could not reach OpenAI: {exc}")


async def _stream_anthropic(messages, model, api_key, usage=None):
    if not api_key:
        raise LLMError("No Anthropic API key configured. Ask an administrator to add a Claude key in the Admin Console.")
    system_msg, filtered = _split_system(messages)
    body: dict = {"model": model, "max_tokens": 4096, "messages": filtered, "stream": True}
    if system_msg:
        body["system"] = system_msg
    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            async with client.stream(
                "POST",
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=body,
            ) as resp:
                await _raise_if_http_error("Anthropic", resp)
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        chunk = json.loads(line[6:].strip())
                    except json.JSONDecodeError:
                        continue
                    ctype = chunk.get("type")
                    if ctype == "error":
                        raise LLMError(f"Anthropic: {_extract_error_message(json.dumps(chunk))}")
                    if ctype == "message_start" and usage is not None:
                        inp = chunk.get("message", {}).get("usage", {}).get("input_tokens")
                        if inp is not None:
                            usage["prompt_tokens"] = inp
                    elif ctype == "content_block_delta":
                        token = chunk.get("delta", {}).get("text", "")
                        if token:
                            yield token
                    elif ctype == "message_delta" and usage is not None:
                        out = chunk.get("usage", {}).get("output_tokens")
                        if out is not None:
                            usage["completion_tokens"] = out
                    elif ctype == "message_stop":
                        break
    except httpx.HTTPError as exc:
        raise LLMError(f"Could not reach Anthropic: {exc}")


async def _stream_gemini(messages, model, api_key, usage=None):
    if not api_key:
        raise LLMError("No Gemini API key configured. Ask an administrator to add a Gemini key in the Admin Console.")
    system, contents = _to_gemini(messages)
    body: dict = {"contents": contents}
    if system:
        body["system_instruction"] = system
    url = f"{_GEMINI_BASE}/models/{model}:streamGenerateContent?alt=sse&key={api_key}"
    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            async with client.stream("POST", url, json=body) as resp:
                await _raise_if_http_error("Gemini", resp)
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        chunk = json.loads(line[6:].strip())
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("error"):
                        raise LLMError(f"Gemini: {_extract_error_message(json.dumps(chunk))}")
                    um = chunk.get("usageMetadata")
                    if um and usage is not None:
                        if um.get("promptTokenCount") is not None:
                            usage["prompt_tokens"] = um["promptTokenCount"]
                        if um.get("candidatesTokenCount") is not None:
                            usage["completion_tokens"] = um["candidatesTokenCount"]
                    for cand in chunk.get("candidates", []):
                        for part in cand.get("content", {}).get("parts", []):
                            token = part.get("text", "")
                            if token:
                                yield token
    except httpx.HTTPError as exc:
        raise LLMError(f"Could not reach Gemini: {exc}")


# ── Sync completion (for background tasks) ────────────────────────────────────

def complete_sync(
    messages: list,
    provider: str = "ollama",
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    timeout: float = 180.0,
    use_cache: bool = True,
) -> str:
    """
    Blocking completion — use only in background/thread-pool tasks.

    max_tokens / temperature default to None, meaning "use each provider's
    historical default" (so existing callers are unaffected); pass explicit
    values for a bigger budget or sampling.

    When the call is deterministic (temperature 0/None for the temp-0 providers),
    an exact-match Redis cache short-circuits the repeat call. The cache key folds
    in max_tokens and temperature so differently-configured calls never collide.
    Pass use_cache=False for anything whose result must reflect a *live* call
    (e.g. a credential connectivity test) or that intentionally samples.
    """
    model = model or DEFAULT_MODELS.get(provider, "llama3.2:1b")

    if use_cache:
        hit = llm_cache.get(provider, model, messages, max_tokens, temperature)
        if hit is not None:
            return hit

    if provider == "openai":
        result = _complete_openai(messages, model, api_key, max_tokens, temperature, timeout)
    elif provider == "anthropic":
        result = _complete_anthropic(messages, model, api_key, max_tokens, temperature, timeout)
    elif provider == "gemini":
        result = _complete_gemini(messages, model, api_key, max_tokens, temperature, timeout)
    else:
        result = _complete_ollama(messages, model, base_url or _OLLAMA_BASE, max_tokens, temperature, timeout)

    if use_cache and result:
        llm_cache.set(provider, model, messages, result, max_tokens, temperature)
    return result


def _check_sync(provider: str, resp) -> None:
    """Raise a descriptive LLMError for a non-2xx sync response."""
    if resp.status_code >= 400:
        detail = _extract_error_message(resp.text) or "no details returned"
        raise LLMError(f"{provider} API error (HTTP {resp.status_code}): {detail}")


def _complete_ollama(messages, model, base_url, max_tokens=None, temperature=None, timeout=180.0):
    url = f"{base_url.rstrip('/')}/api/chat"
    num_predict = max_tokens if max_tokens is not None else 300
    temp = temperature if temperature is not None else 0.0
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json={
                "model": model, "messages": messages, "stream": False,
                "keep_alive": _OLLAMA_KEEP_ALIVE,
                "options": {"temperature": temp, "num_predict": num_predict},
            })
            _check_sync("Ollama", resp)
            data = resp.json()
            if data.get("error"):
                raise LLMError(f"Ollama: {data['error']}")
            return data.get("message", {}).get("content", "")
    except httpx.ConnectError:
        raise LLMError(f"Cannot reach Ollama at {base_url}. Is the Ollama server running?")
    except httpx.HTTPError as exc:
        raise LLMError(f"Ollama connection failed: {exc}")


def _complete_openai(messages, model, api_key, max_tokens=None, temperature=None, timeout=180.0):
    if not api_key:
        raise LLMError("No OpenAI API key configured.")
    mt = max_tokens if max_tokens is not None else 300
    temp = temperature if temperature is not None else 0.0
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": model, "messages": messages, "temperature": temp, "max_tokens": mt},
            )
            _check_sync("OpenAI", resp)
            return resp.json()["choices"][0]["message"]["content"]
    except httpx.HTTPError as exc:
        raise LLMError(f"Could not reach OpenAI: {exc}")


def _complete_anthropic(messages, model, api_key, max_tokens=None, temperature=None, timeout=180.0):
    if not api_key:
        raise LLMError("No Anthropic API key configured.")
    system_msg, filtered = _split_system(messages)
    body: dict = {"model": model, "max_tokens": max_tokens if max_tokens is not None else 300, "messages": filtered}
    if temperature is not None:
        body["temperature"] = temperature
    if system_msg:
        body["system"] = system_msg
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=body,
            )
            _check_sync("Anthropic", resp)
            return resp.json()["content"][0]["text"]
    except httpx.HTTPError as exc:
        raise LLMError(f"Could not reach Anthropic: {exc}")


def _complete_gemini(messages, model, api_key, max_tokens=None, temperature=None, timeout=180.0):
    if not api_key:
        raise LLMError("No Gemini API key configured.")
    system, contents = _to_gemini(messages)
    gen_cfg: dict = {"maxOutputTokens": max_tokens if max_tokens is not None else 800}
    if temperature is not None:
        gen_cfg["temperature"] = temperature
    body: dict = {"contents": contents, "generationConfig": gen_cfg}
    if system:
        body["system_instruction"] = system
    url = f"{_GEMINI_BASE}/models/{model}:generateContent?key={api_key}"
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json=body)
            _check_sync("Gemini", resp)
            data = resp.json()
            parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
            return "".join(p.get("text", "") for p in parts)
    except httpx.HTTPError as exc:
        raise LLMError(f"Could not reach Gemini: {exc}")


def _split_system(messages: list) -> tuple[str, list]:
    """Extract system message from the list (Anthropic requires it separately)."""
    system = ""
    rest = []
    for m in messages:
        if m.get("role") == "system":
            system = m["content"]
        else:
            rest.append(m)
    return system, rest
