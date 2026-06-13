"""
ReAct (Reasoning + Acting) Deployment Agent
Each turn: LLM reasons → picks a tool → tool executes → observation fed back → repeat
The loop pauses when it needs user input (request_user_input / present_plan).
"""
import json
import os
import re
import asyncio
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, Any, List

from app.core.auth.auth import get_current_user, require_agent_access
from app import models
from app.core.llm import llm_service
from app.core import database, config_service, telemetry
from app.modules.dba.deployment.deployments import extract_deployment_steps

router = APIRouter(prefix="/deployments/agent", tags=["Deployment Agent"])

# ── Cost controls ──────────────────────────────────────────────────────────────
# Estimated-token budget for one /chat request (all ReAct turns it triggers).
# When exhausted the loop stops calling the LLM and falls back to the
# deterministic next-missing-field question, so the task degrades gracefully
# instead of burning more tokens.
AGENT_TOKEN_BUDGET = int(os.getenv("AGENT_TOKEN_BUDGET", "8000"))
# Max history messages sent to the LLM per turn. The system prompt already
# carries a full snapshot of collected context, so older turns are redundant;
# windowing keeps the prompt from growing quadratically over a session.
AGENT_HISTORY_MAX_MSGS = int(os.getenv("AGENT_HISTORY_MAX_MSGS", "12"))
# Hard cap on retries per LLM call. Only transient failures (connection,
# timeout, 429, 5xx) are retried; auth/4xx errors fail immediately.
AGENT_LLM_MAX_RETRIES = int(os.getenv("AGENT_LLM_MAX_RETRIES", "2"))


# ── Prompt ─────────────────────────────────────────────────────────────────────

_TOOLS_DOC = """\
Available tools (use EXACTLY these names):
  get_servers              — List the SSH server connections saved by the user.
  get_environments         — List the target environments (DEV, UAT, UAT2, PROD) saved by the user.
  fetch_confluence_page    — Fetch deployment instructions from a Confluence page URL using an API token.
  extract_steps            — Parse the raw deployment instructions into an ordered list of steps.
  request_user_input       — Ask the user one question and wait for their reply.
  present_plan             — Show the deployment plan and ask the user to confirm.
  trigger_deployment       — Launch the deployment (only after the user confirms).
  finish                   — End the session with a closing message."""

_SYSTEM_PROMPT = """\
You are an Oracle EBS Deployment Agent. Use the ReAct pattern: think, then act.

{tools}

You must collect these details before deploying:
1. deployment_instructions — what files/packages to deploy. Can come from:
   a. User pasting text directly (field: instructions)
   b. Confluence page — collect confluence_url then confluence_token, then call fetch_confluence_page
2. target_environment — DEV, UAT, UAT2, or PROD
3. ssh_server — which server from the get_servers list
4. source_type — "git" (files on a Git repo) or "local" (files already on the server)
5. git_url and git_branch — only if source_type is "git"
6. git_token — personal access token for private Git repos (ask after git_branch; user may say "none" if public)

ALWAYS respond in exactly this format (three lines, no extra text before or after):

Thought: your reasoning here — one or two plain sentences
Action: one tool name from the list above
Action Input: for request_user_input write {{"field":"<fieldname>","question":"<your question>"}} — for fetch_confluence_page write {{"url":"<url>","token":"<token>"}} — for all other tools write a plain string or leave empty

Valid field names: instructions, confluence_url, confluence_token, environment, server, source_type, git_url, git_branch, git_token, confirmation

Example turn:
Thought: The user has not told me what to deploy. I should ask for instructions first.
Action: request_user_input
Action Input: {{"field":"instructions","question":"What files or packages need to be deployed? Paste your notes, describe the changes, or provide a Confluence page URL."}}

Rules:
- One action per turn only.
- Call get_environments before asking which environment. Call get_servers before asking which server.
- If the user provides a Confluence URL: ask for confluence_token, then call fetch_confluence_page.
- After extract_steps always call present_plan. Never skip straight to trigger_deployment.
- If user says yes/ok/proceed after seeing the plan, call trigger_deployment immediately.
- If user says cancel or no, call finish.
- Do not ask for information already shown in the context below.

Current context (do NOT ask again for non-null fields):
{context}
"""


# ── Pydantic models ────────────────────────────────────────────────────────────

class AgentMessage(BaseModel):
    role: str
    content: str

class AgentContext(BaseModel):
    instructions:     Optional[str]  = None
    confluence_url:   Optional[str]  = None
    confluence_token: Optional[str]  = None
    environment:      Optional[str]  = None
    server_name:      Optional[str]  = None
    server_index:     Optional[int]  = None
    source_type:      Optional[str]  = None
    git_url:          Optional[str]  = None
    git_branch:       Optional[str]  = None
    git_token:        Optional[str]  = None  # None = not asked; "" = public repo
    steps:            Optional[Any]  = None
    confirmed:        Optional[bool] = None

class AgentChatRequest(BaseModel):
    history:               List[AgentMessage] = []
    user_message:          str
    context:               AgentContext
    available_servers:     List[Any] = []
    available_environments: List[Any] = []


# ── Helpers ────────────────────────────────────────────────────────────────────

def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _parse_react(text: str):
    """
    Extract Thought / Action / Action Input from LLM output.
    Handles both the standard ReAct text format AND JSON-formatted responses
    (small models like llama3.2:1b sometimes output JSON instead of ReAct text).
    """
    thought = action = action_input = ""

    # ── Primary: standard ReAct text format ────────────────────────────────────
    t = re.search(r"Thought\s*:\s*(.+?)(?=\nAction\s*:|\Z)", text, re.DOTALL | re.IGNORECASE)
    a = re.search(r"Action\s*:\s*(.+?)(?=\nAction\s*Input\s*:|\Z)", text, re.DOTALL | re.IGNORECASE)
    i = re.search(r"Action\s*Input\s*:\s*(.+?)$", text, re.DOTALL | re.IGNORECASE)

    if t: thought      = t.group(1).strip()
    if a: action       = a.group(1).strip().lower().replace(" ", "_").replace("-", "_")
    if i: action_input = i.group(1).strip()

    # ── Fallback: JSON-formatted response ──────────────────────────────────────
    # Small LLMs often emit {Thought:..., Action:..., "Action Input":...} instead
    if not action:
        cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        try:
            data = json.loads(cleaned)
            thought = str(data.get("Thought", data.get("thought", thought))).strip()
            raw_action = str(data.get("Action", data.get("action", "")))
            action = raw_action.lower().replace(" ", "_").replace("-", "_")
            raw_input = data.get("Action Input",
                         data.get("action_input",
                         data.get("ActionInput", action_input)))
            action_input = json.dumps(raw_input) if isinstance(raw_input, dict) else str(raw_input)
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass

    # ── Last resort: if thought looks like raw JSON, pretty-print it ───────────
    if thought and thought.startswith("{"):
        try:
            parsed = json.loads(thought)
            # Extract a readable string from the JSON object
            for key in ("reasoning", "text", "content", "message"):
                if key in parsed:
                    thought = str(parsed[key])
                    break
            else:
                thought = ", ".join(f"{k}: {v}" for k, v in parsed.items())
        except (json.JSONDecodeError, TypeError):
            pass

    return thought, action, action_input


async def _llm(messages: list, provider: str, model: str, api_key: str, base_url: str) -> str:
    """
    Call the LLM for agent reasoning with a higher token budget than the default
    complete_sync (which uses 300 tokens — too tight for a full ReAct turn).
    """
    import httpx, os

    model = model or llm_service.DEFAULT_MODELS.get(provider, "llama3.2:1b")

    if provider == "openai":
        def _call():
            with httpx.Client(timeout=180.0) as c:
                r = c.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={"model": model, "messages": messages, "temperature": 0.2, "max_tokens": 600},
                )
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"]
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _call)

    elif provider == "anthropic":
        system_msg, filtered = llm_service._split_system(messages)
        body = {"model": model, "max_tokens": 600, "messages": filtered}
        if system_msg:
            body["system"] = system_msg
        def _call():
            with httpx.Client(timeout=180.0) as c:
                r = c.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                             "content-type": "application/json"},
                    json=body,
                )
                r.raise_for_status()
                return r.json()["content"][0]["text"]
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _call)

    elif provider == "gemini":
        def _call():
            return llm_service.complete_sync(
                messages=messages, provider="gemini", model=model, api_key=api_key,
            )
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _call)

    else:  # ollama
        base = (base_url or os.getenv("OLLAMA_URL", "http://localhost:11434")).rstrip("/")
        def _call():
            # Routed through complete_sync for shared Ollama plumbing + keep_alive.
            # use_cache=False: a ReAct turn samples (temp 0.2) and must stay live so
            # the agent can't get stuck replaying a cached reasoning step.
            return llm_service.complete_sync(
                messages=messages, provider="ollama", model=model, base_url=base,
                max_tokens=600, temperature=0.2, use_cache=False,
            )
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _call)


# ── Helpers for stuck-loop detection ──────────────────────────────────────────

async def _fetch_confluence_page(url: str, token: str) -> str:
    """
    Fetch a Confluence page via REST API and return plain text.
    Supports Cloud (email:api_token Basic auth or Bearer PAT) and Server/DC.
    """
    import base64
    import httpx as _httpx

    page_id_match = re.search(r'/pages/(\d+)', url)
    if not page_id_match:
        return "ERROR: Could not extract page ID from URL. Expected format: .../pages/123456789/..."

    page_id     = page_id_match.group(1)
    domain_match = re.match(r'(https?://[^/]+)', url)
    if not domain_match:
        return "ERROR: Invalid Confluence URL — could not extract domain."

    base    = domain_match.group(1)
    api_url = (
        f"{base}/wiki/rest/api/content/{page_id}?expand=body.storage"
        if "/wiki/" in url else
        f"{base}/rest/api/content/{page_id}?expand=body.storage"
    )

    # Determine auth header:
    # - "email:token" → Basic auth (Confluence Cloud with API token)
    # - bare token    → Bearer (Confluence Server/DC personal access token)
    if ":" in token:
        encoded = base64.b64encode(token.encode()).decode()
        auth_header = f"Basic {encoded}"
    else:
        auth_header = f"Bearer {token}"

    headers = {"Accept": "application/json", "Authorization": auth_header}

    try:
        async with _httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(api_url, headers=headers)
            if resp.status_code == 401 and ":" not in token:
                # Retry with Basic using token as password (some Server setups)
                encoded = base64.b64encode(f":{token}".encode()).decode()
                headers["Authorization"] = f"Basic {encoded}"
                resp = await client.get(api_url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except _httpx.HTTPStatusError as exc:
        return f"ERROR: Confluence API returned {exc.response.status_code}. Check the URL and token."
    except Exception as exc:
        return f"ERROR: Could not reach Confluence — {exc}"

    html = data.get("body", {}).get("storage", {}).get("value", "")
    if not html:
        return "ERROR: Page was fetched but the content body is empty."

    # Strip HTML tags and normalise whitespace
    text = re.sub(r'<[^>]+>', ' ', html)
    text = re.sub(r'\s+', ' ', text).strip()
    if len(text) > 4000:
        text = text[:4000] + "\n...[truncated — page too long]"

    return text


def _next_missing_field(ctx: dict, has_git: bool = False, has_confluence: bool = False) -> str:
    """Return the next field that still needs to be collected, in collection order."""
    # If a Confluence URL was given, collect token before anything else —
    # unless an admin-managed Confluence credential is configured (auto-use).
    if ctx.get("confluence_url") and not ctx.get("confluence_token") and not has_confluence:
        return "confluence_token"
    # Instructions must be present (either pasted or fetched from Confluence)
    if not ctx.get("instructions"):
        return "instructions"
    if not ctx.get("environment"):   return "environment"
    if not ctx.get("server_name"):   return "server"
    if not ctx.get("source_type"):   return "source_type"
    if ctx.get("source_type") == "git" and not ctx.get("git_url"):    return "git_url"
    if ctx.get("source_type") == "git" and not ctx.get("git_branch"): return "git_branch"
    # git_token: None = not yet asked; "" = public. Skipped when an admin-managed
    # Git credential is configured (resolved server-side at deploy time).
    if (ctx.get("source_type") == "git" and ctx.get("git_token") is None and not has_git):
        return "git_token"
    return ""

def _fallback_question(field: str, servers: list, envs: list) -> str:
    """Return a direct question for each field when the LLM gets stuck."""
    if field == "instructions":
        return (
            "What files or packages need to be deployed?\n"
            "You can paste your notes directly, describe the changes, "
            "or share a **Confluence page URL** and I'll fetch it for you."
        )
    if field == "confluence_token":
        return (
            "Please provide your Confluence API token so I can fetch the page.\n\n"
            "For **Confluence Cloud**: generate one at https://id.atlassian.com/manage-profile/security/api-tokens — "
            "then enter it as `your-email@company.com:your-token`.\n"
            "For **Confluence Server / Data Center**: enter your Personal Access Token directly."
        )
    if field == "environment":
        names = [e["name"] if isinstance(e, dict) else str(e) for e in envs] or ["DEV", "UAT", "UAT2", "PROD"]
        return "Which environment would you like to deploy to?\nOptions: " + ", ".join(names)
    if field == "server":
        if servers:
            opts = "\n".join(f"{i+1}. {s.get('name')} ({s.get('username')}@{s.get('hostname')}:{s.get('port',22)})"
                             for i, s in enumerate(servers))
            return f"Which SSH server should I use?\n{opts}\n\nEnter the number."
        return "No SSH servers are configured. Go to Settings → SSH Servers to add one."
    if field == "source_type":
        return "Where do the deployment files live?\nReply **git** (I'll clone the repo on the server) or **local** (files are already on the server)."
    if field == "git_url":
        return "What is the Git repository URL? (e.g. https://github.com/org/repo.git)"
    if field == "git_branch":
        return "Which Git branch should I check out? (default: main)"
    if field == "git_token":
        return "Is this a private repository? If yes, paste your Git personal access token (GitHub/GitLab PAT). If public, reply **none**."
    return "What else do you need?"


def _is_transient(exc: Exception) -> bool:
    """
    True when an LLM call failure is worth retrying: connection problems,
    timeouts, rate limits (429) and server errors (5xx). Auth/validation
    errors (other 4xx) are permanent — retrying them only burns time.
    """
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    if isinstance(exc, llm_service.LLMError):
        msg = str(exc)
        m = re.search(r"HTTP (\d{3})", msg)
        if m:
            code = int(m.group(1))
            return code == 429 or code >= 500
        # LLMError without a status is a connectivity failure
        # ("Cannot reach …", "connection failed", "Could not reach …")
        return any(s in msg for s in ("Cannot reach", "connection failed", "Could not reach"))
    return False


def _trim_history(history: list, max_msgs: int = None) -> list:
    """
    Window the conversation sent to the LLM to the most recent messages.
    The full history is still kept (and round-tripped to the client) for the
    audit trail — only the LLM prompt is trimmed. The window must start on a
    user message (Anthropic rejects a leading assistant turn).
    """
    limit = max_msgs if max_msgs is not None else AGENT_HISTORY_MAX_MSGS
    if limit <= 0 or len(history) <= limit:
        return history
    trimmed = history[-limit:]
    while trimmed and trimmed[0].get("role") != "user":
        trimmed = trimmed[1:]
    return trimmed or history[-1:]


def _build_plan_md(context: dict) -> str:
    steps  = context.get("steps") or []
    env    = context.get("environment", "?")
    server = context.get("server_name", "?")
    rows   = [
        f"{i+1}. **[{s.get('execution_type','?')}]** `{s.get('file_name','?')}`\n"
        f"   `{s.get('command','')}`"
        for i, s in enumerate(steps)
    ]
    return (
        f"**Deployment Plan — {env} via {server}**\n\n"
        + "\n".join(rows)
        + "\n\nType **yes** to proceed or **no** to cancel."
    )


# ── ReAct loop ─────────────────────────────────────────────────────────────────

async def react_loop(
    history: list,
    user_message: str,
    context: dict,
    available_servers: list,
    available_environments: list,
    provider: str,
    model: str,
    api_key: str,
    base_url: str,
    has_git_cred: bool = False,
    has_confluence_cred: bool = False,
    resolve_confluence=None,
):
    """
    Async generator — yields SSE chunks until user input is needed or agent finishes.

    Robustness rules for small models:
    - get_environments / get_servers → deterministically ask the user (no second LLM hop)
    - extract_steps → deterministically present the plan (no second LLM hop)
    - Empty or repeated actions → fallback question based on missing context field
    """
    MAX_ITER = 8

    # Only show non-null context fields to avoid confusing small models with nulls
    def _ctx_str():
        collected = {k: v for k, v in context.items()
                     if v is not None and k not in ("steps", "confirmed")}
        if context.get("steps"):
            collected["steps_extracted"] = len(context["steps"])
        return json.dumps(collected, indent=2, default=str) if collected else "Nothing collected yet."

    # Append user message
    if history:
        history.append({"role": "user", "content": f"Observation: User replied: {user_message}"})
    else:
        history.append({"role": "user", "content": user_message})

    consecutive_bad = 0   # turns with no valid action
    last_action     = None
    same_action_cnt = 0
    spent_tokens    = 0   # estimated prompt+completion tokens this request

    cred_note_parts = []
    if has_confluence_cred:
        cred_note_parts.append(
            "- Confluence access is configured centrally. When the user gives a Confluence URL, "
            "call fetch_confluence_page immediately and do NOT ask for confluence_token."
        )
    if has_git_cred:
        cred_note_parts.append(
            "- Git access is configured centrally. Do NOT ask for git_token."
        )
    cred_note = ("\nCentral credentials:\n" + "\n".join(cred_note_parts)) if cred_note_parts else ""

    for _ in range(MAX_ITER):
        if spent_tokens >= AGENT_TOKEN_BUDGET:
            break  # budget exhausted — drop to the deterministic fallback below

        # Build messages with a clean context snapshot each turn
        system   = _SYSTEM_PROMPT.format(tools=_TOOLS_DOC, context=_ctx_str()) + cred_note
        messages = [{"role": "system", "content": system}] + _trim_history(history)

        raw = None
        for attempt in range(AGENT_LLM_MAX_RETRIES + 1):
            try:
                raw = await _llm(messages, provider, model, api_key, base_url)
                break
            except Exception as exc:
                if attempt >= AGENT_LLM_MAX_RETRIES or not _is_transient(exc):
                    yield _sse({"type": "error", "content": f"LLM error: {exc}"})
                    return
                yield _sse({"type": "thought",
                            "content": f"LLM call failed ({exc}) — retrying "
                                       f"({attempt + 1}/{AGENT_LLM_MAX_RETRIES})…"})
                await asyncio.sleep(2 ** attempt)  # 1s, 2s, 4s…

        spent_tokens += sum(telemetry.est_tokens(m.get("content", "")) for m in messages)
        spent_tokens += telemetry.est_tokens(raw)

        thought, action, action_input = _parse_react(raw)
        history.append({"role": "assistant", "content": raw})

        if thought:
            yield _sse({"type": "thought", "content": thought})

        # ── Stuck-loop detection ───────────────────────────────────────────────
        if not action:
            consecutive_bad += 1
            if consecutive_bad >= 2:
                field = _next_missing_field(context, has_git_cred, has_confluence_cred)
                if field:
                    q = _fallback_question(field, available_servers, available_environments)
                    yield _sse({"type": "question", "content": q, "field": field})
                    yield _sse({"type": "needs_input", "history": history, "context": context})
                else:
                    yield _sse({"type": "finish", "content": "All information collected. Ready to deploy."})
                return
            obs = "Please reply with Thought / Action / Action Input."
            history.append({"role": "user", "content": f"Observation: {obs}"})
            continue
        consecutive_bad = 0

        if action == last_action:
            same_action_cnt += 1
        else:
            same_action_cnt = 0
        last_action = action

        if same_action_cnt >= 2:
            # Model is looping on the same tool — force progress
            field = _next_missing_field(context, has_git_cred, has_confluence_cred)
            q     = _fallback_question(field, available_servers, available_environments) if field else "How can I help?"
            yield _sse({"type": "question", "content": q, "field": field})
            yield _sse({"type": "needs_input", "history": history, "context": context})
            return

        # ── Tool execution ─────────────────────────────────────────────────────

        if action == "get_environments":
            env_names = [
                e["name"] if isinstance(e, dict) else str(e)
                for e in (available_environments or [])
            ] or ["DEV", "UAT", "UAT2", "PROD"]
            obs = "Available environments: " + ", ".join(env_names)
            history.append({"role": "user", "content": f"Observation: {obs}"})
            yield _sse({"type": "observation", "content": obs})
            # Deterministic follow-up — don't send back to LLM
            q = "Which environment would you like to deploy to?\nOptions: " + ", ".join(env_names)
            yield _sse({"type": "question", "content": q, "field": "environment"})
            yield _sse({"type": "needs_input", "history": history, "context": context})
            return

        elif action == "get_servers":
            if available_servers:
                lines = [
                    f"{i+1}. {s.get('name','?')} "
                    f"({s.get('username','?')}@{s.get('hostname','?')}:{s.get('port',22)})"
                    for i, s in enumerate(available_servers)
                ]
                obs = "Available SSH servers:\n" + "\n".join(lines)
                q   = "Which SSH server should I use?\n" + "\n".join(lines) + "\n\nEnter the number."
            else:
                obs = "No SSH servers configured."
                q   = "No SSH servers are configured. Go to Settings → SSH Servers to add one first."
            history.append({"role": "user", "content": f"Observation: {obs}"})
            yield _sse({"type": "observation", "content": obs})
            # Deterministic follow-up
            yield _sse({"type": "question", "content": q, "field": "server"})
            yield _sse({"type": "needs_input", "history": history, "context": context})
            return

        elif action == "fetch_confluence_page":
            # Parse action_input for url + token (JSON or fallback to context)
            cf_url   = context.get("confluence_url", "")
            cf_token = context.get("confluence_token", "")
            try:
                parsed   = json.loads(action_input)
                cf_url   = parsed.get("url",   cf_url)
                cf_token = parsed.get("token", cf_token)
            except (json.JSONDecodeError, TypeError):
                pass

            # Auto-use the admin-managed Confluence token when none was supplied.
            if cf_url and not cf_token and resolve_confluence:
                cf_token = resolve_confluence(cf_url) or ""

            if not cf_url or not cf_token:
                # Missing — ask for whichever is absent
                if not cf_url:
                    yield _sse({"type": "question", "content": "Please share the Confluence page URL.", "field": "confluence_url"})
                else:
                    yield _sse({"type": "question",
                                "content": _fallback_question("confluence_token", available_servers, available_environments),
                                "field": "confluence_token"})
                yield _sse({"type": "needs_input", "history": history, "context": context})
                return

            yield _sse({"type": "thought", "content": f"Fetching Confluence page: {cf_url}"})
            page_text = await _fetch_confluence_page(cf_url, cf_token)

            if page_text.startswith("ERROR:"):
                obs = page_text
                history.append({"role": "user", "content": f"Observation: {obs}"})
                yield _sse({"type": "observation", "content": obs})
                # Ask user to correct the token
                yield _sse({"type": "question",
                            "content": f"{obs}\n\nPlease check your token and try again.",
                            "field": "confluence_token"})
                context["confluence_token"] = None  # reset so user can re-enter
                yield _sse({"type": "needs_input", "history": history, "context": context})
                return

            context["instructions"] = page_text
            obs = f"Confluence page fetched successfully ({len(page_text)} chars)."
            history.append({"role": "user", "content": f"Observation: {obs}"})
            yield _sse({"type": "observation", "content": obs})
            # Deterministic follow-up: proceed to environment
            env_names = [e["name"] if isinstance(e, dict) else str(e)
                         for e in (available_environments or [])] or ["DEV", "UAT", "UAT2", "PROD"]
            q = "Instructions loaded from Confluence ✓\n\nWhich environment would you like to deploy to?\nOptions: " + ", ".join(env_names)
            yield _sse({"type": "question", "content": q, "field": "environment"})
            yield _sse({"type": "needs_input", "history": history, "context": context})
            return

        elif action == "extract_steps":
            instructions = context.get("instructions") or action_input or user_message
            try:
                loop = asyncio.get_event_loop()
                steps = await loop.run_in_executor(None, extract_deployment_steps, instructions)
                context["steps"] = steps
                obs = f"Extracted {len(steps)} step(s)."
            except Exception as exc:
                steps = []
                obs = f"Step extraction failed: {exc}"
            history.append({"role": "user", "content": f"Observation: {obs}"})
            yield _sse({"type": "observation", "content": obs, "steps": steps})
            # Deterministic follow-up — present plan immediately
            yield _sse({"type": "plan", "content": _build_plan_md(context),
                        "steps": steps, "field": "confirmation"})
            yield _sse({"type": "needs_input", "history": history, "context": context})
            return

        elif action == "request_user_input":
            field    = ""
            question = action_input
            try:
                parsed   = json.loads(action_input)
                field    = parsed.get("field", "")
                question = parsed.get("question", action_input)
            except (json.JSONDecodeError, TypeError):
                pass
            yield _sse({"type": "question", "content": question, "field": field})
            yield _sse({"type": "needs_input", "history": history, "context": context})
            return

        elif action == "present_plan":
            steps = context.get("steps") or []
            yield _sse({"type": "plan", "content": _build_plan_md(context),
                        "steps": steps, "field": "confirmation"})
            yield _sse({"type": "needs_input", "history": history, "context": context})
            return

        elif action == "trigger_deployment":
            yield _sse({"type": "trigger_deployment", "context": context, "history": history})
            return

        elif action == "finish":
            yield _sse({"type": "finish", "content": action_input or "Session ended."})
            return

        else:
            # Unrecognised tool name — nudge and continue
            obs = f"Unknown tool '{action}'. Use one of: get_environments, get_servers, extract_steps, request_user_input, present_plan, trigger_deployment, finish."
            history.append({"role": "user", "content": f"Observation: {obs}"})
            yield _sse({"type": "observation", "content": obs})

    # Fallback if loop exhausted
    field = _next_missing_field(context, has_git_cred, has_confluence_cred)
    q     = _fallback_question(field, available_servers, available_environments) if field else "Please try again."
    yield _sse({"type": "question", "content": q, "field": field})
    yield _sse({"type": "needs_input", "history": history, "context": context})


# ── Endpoint ───────────────────────────────────────────────────────────────────

@router.post("/chat")
async def agent_chat(
    payload: AgentChatRequest,
    request: Request,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(require_agent_access),
):
    provider = request.headers.get("X-LLM-Provider", "ollama")
    model    = request.headers.get("X-LLM-Model", "")
    api_key  = request.headers.get("X-LLM-Api-Key", "")
    base_url = request.headers.get("X-LLM-Base-Url", "")
    provider, model, api_key, base_url = config_service.resolve_llm(
        provider, model or None, api_key or None, base_url or None, db, agent="deployment"
    )
    model = model or ""
    api_key = api_key or ""
    base_url = base_url or ""

    context = payload.context.model_dump()
    history = [m.model_dump() for m in payload.history]

    # Strip server passwords before they enter the agent history
    safe_servers = [
        {k: v for k, v in s.items() if k != "password"}
        for s in payload.available_servers
    ]

    # Admin-managed Git/Confluence credentials → auto-use (agent stops asking)
    has_git_cred = config_service.has_integration(db, "git")
    has_confluence_cred = config_service.has_integration(db, "confluence")

    def _resolve_confluence(url: str):
        cdb = database.SessionLocal()
        try:
            return config_service.resolve_confluence_auth(url, cdb)
        finally:
            cdb.close()

    async def stream():
        async for chunk in react_loop(
            history=history,
            user_message=payload.user_message,
            context=context,
            available_servers=safe_servers,
            available_environments=payload.available_environments,
            provider=provider,
            model=model or llm_service.DEFAULT_MODELS.get(provider, "llama3.2:1b"),
            api_key=api_key,
            base_url=base_url,
            has_git_cred=has_git_cred,
            has_confluence_cred=has_confluence_cred,
            resolve_confluence=_resolve_confluence,
        ):
            yield chunk
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")
