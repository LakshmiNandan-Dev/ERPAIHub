"""
Unified LLM service — supports Ollama (local), OpenAI, and Anthropic.
Async streaming for chat endpoints; sync completion for background tasks (RLAIF).
"""
import os
import httpx
import json
from typing import AsyncGenerator, Optional

_OLLAMA_BASE = os.getenv("OLLAMA_URL", "http://localhost:11434")

DEFAULT_MODELS = {
    "ollama":    "llama3.2:1b",
    "openai":    "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5-20251001",
}


# ── Async streaming ────────────────────────────────────────────────────────────

async def stream_tokens(
    messages: list,
    provider: str = "ollama",
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    """Yield raw text tokens from the chosen provider."""
    model = model or DEFAULT_MODELS.get(provider, "llama3.2:1b")

    if provider == "openai":
        async for t in _stream_openai(messages, model, api_key):
            yield t
    elif provider == "anthropic":
        async for t in _stream_anthropic(messages, model, api_key):
            yield t
    else:
        async for t in _stream_ollama(messages, model, base_url or _OLLAMA_BASE):
            yield t


async def _stream_ollama(messages, model, base_url):
    url = f"{base_url.rstrip('/')}/api/chat"
    async with httpx.AsyncClient(timeout=180.0) as client:
        async with client.stream(
            "POST", url,
            json={"model": model, "messages": messages, "stream": True}
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                token = chunk.get("message", {}).get("content", "")
                if token:
                    yield token
                if chunk.get("done", False):
                    break


async def _stream_openai(messages, model, api_key):
    if not api_key:
        raise ValueError("OpenAI API key is required")
    async with httpx.AsyncClient(timeout=180.0) as client:
        async with client.stream(
            "POST",
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "messages": messages, "stream": True},
        ) as resp:
            resp.raise_for_status()
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
                token = (chunk.get("choices") or [{}])[0].get("delta", {}).get("content", "")
                if token:
                    yield token


async def _stream_anthropic(messages, model, api_key):
    if not api_key:
        raise ValueError("Anthropic API key is required")
    system_msg, filtered = _split_system(messages)
    body: dict = {"model": model, "max_tokens": 4096, "messages": filtered, "stream": True}
    if system_msg:
        body["system"] = system_msg
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
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    chunk = json.loads(line[6:].strip())
                except json.JSONDecodeError:
                    continue
                if chunk.get("type") == "content_block_delta":
                    token = chunk.get("delta", {}).get("text", "")
                    if token:
                        yield token
                elif chunk.get("type") == "message_stop":
                    break


# ── Sync completion (for background tasks) ────────────────────────────────────

def complete_sync(
    messages: list,
    provider: str = "ollama",
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
) -> str:
    """Blocking completion — use only in background/thread-pool tasks."""
    model = model or DEFAULT_MODELS.get(provider, "llama3.2:1b")

    if provider == "openai":
        return _complete_openai(messages, model, api_key)
    elif provider == "anthropic":
        return _complete_anthropic(messages, model, api_key)
    else:
        return _complete_ollama(messages, model, base_url or _OLLAMA_BASE)


def _complete_ollama(messages, model, base_url):
    url = f"{base_url.rstrip('/')}/api/chat"
    with httpx.Client(timeout=180.0) as client:
        resp = client.post(url, json={
            "model": model, "messages": messages, "stream": False,
            "options": {"temperature": 0.0, "num_predict": 300},
        })
        resp.raise_for_status()
        return resp.json().get("message", {}).get("content", "")


def _complete_openai(messages, model, api_key):
    with httpx.Client(timeout=180.0) as client:
        resp = client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "messages": messages, "temperature": 0.0, "max_tokens": 300},
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


def _complete_anthropic(messages, model, api_key):
    system_msg, filtered = _split_system(messages)
    body: dict = {"model": model, "max_tokens": 300, "messages": filtered}
    if system_msg:
        body["system"] = system_msg
    with httpx.Client(timeout=180.0) as client:
        resp = client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=body,
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"]


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
