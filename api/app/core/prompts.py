"""
Agent prompt registry.

Each LLM-driven agent prompt has a canonical default defined here (single source
of truth) and an optional admin override stored in the ``agent_prompts`` table.
``get_prompt(key, **subs)`` returns the override if present, else the default,
then substitutes ``{name}`` tokens via str.replace — NOT str.format — because
prompt bodies contain literal ``{ }`` (e.g. JSON examples in the deployment
ReAct prompt). Every DB access is wrapped so a failure always degrades to the
built-in default.

Agents that don't drive an LLM (cloning, patching — deterministic interview /
simulation flows) intentionally have no entry here.
"""
import hashlib

from app.core import database
from app import models


# ── Default prompt bodies ───────────────────────────────────────────────────────

_CHAT_SYSTEM = (
    "You are an expert Oracle E-Business Suite (EBS) developer assistant. "
    "Help developers with PL/SQL package compilation, SQL script execution, ADOP patching, FNDLOAD data uploads, "
    "Forms compilation, Workflow uploads, OAF page imports, SSH-based deployments, and general EBS configuration tasks. "
    "Provide complete, accurate command examples and scripts tailored to Oracle EBS 12.x environments. "
    "When a question is ambiguous, ask for clarification rather than guessing.\n\n"
    "GROUNDING RULES — follow these strictly:\n"
    "- When a message includes a [KNOWLEDGE BASE] block, treat it as the authoritative source and base your "
    "answer on it. Cite the source filename for facts you draw from it.\n"
    "- Do NOT invent table names, column names, profile options, API/package signatures, patch numbers, or file "
    "paths. If a specific detail is not in the knowledge base or established Oracle EBS fundamentals, say so plainly.\n"
    "- If the knowledge base does not contain the answer, reply that it isn't in the knowledge base and give only "
    "the general guidance you are confident about — do not fabricate specifics to fill the gap.\n"
    "- Prefer saying \"I'm not certain\" over presenting a guess as fact."
)

_KB_GROUNDING = (
    "[KNOWLEDGE BASE]\n"
    "{rag_context}\n"
    "[END KNOWLEDGE BASE]\n\n"
    "Answer the QUESTION using the knowledge base above as the authoritative source, and cite the source "
    "for the specifics you use. If the knowledge base does not contain the answer, say it isn't in the "
    "knowledge base and give only the general guidance you are confident about — do not invent table names, "
    "profile options, API signatures, patch numbers, or file paths to fill the gap.\n\n"
    "QUESTION: {question}"
)

_KB_NO_CONTEXT = (
    "No knowledge-base entry was found for this question. Answer only from well-established, standard "
    "Oracle EBS knowledge. Do NOT invent or guess specific command syntax, parameter names, file paths, "
    "table/column names, profile options, or patch numbers. If you are not certain of the exact command, "
    "say so and tell the user to verify against the official Oracle documentation rather than giving an "
    "unverified command.\n\n"
    "QUESTION: {question}"
)

# NOTE: single braces around the JSON examples — substitution is str.replace, so
# only the {tools} and {context} tokens are replaced; everything else is literal.
_DEPLOYMENT_SYSTEM = """\
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
Action Input: for request_user_input write {"field":"<fieldname>","question":"<your question>"} — for fetch_confluence_page write {"url":"<url>","token":"<token>"} — for all other tools write a plain string or leave empty

Valid field names: instructions, confluence_url, confluence_token, environment, server, source_type, git_url, git_branch, git_token, confirmation

Example turn:
Thought: The user has not told me what to deploy. I should ask for instructions first.
Action: request_user_input
Action Input: {"field":"instructions","question":"What files or packages need to be deployed? Paste your notes, describe the changes, or provide a Confluence page URL."}

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

_PERF_DIAGNOSTIC = (
    "You are a senior Oracle DBA and Oracle E-Business Suite (EBS) performance expert with 15+ years of experience. "
    "Analyze the provided diagnostic data and return a structured, prioritized report.\n\n"
    "Use this exact structure:\n\n"
    "## 📊 Executive Summary\n"
    "2-3 sentence health overview.\n\n"
    "## 🚨 Critical Issues\n"
    "List critical problems (prefix each with ❌).\n\n"
    "## ⚠️ Warnings\n"
    "Moderate issues to address soon (prefix each with ⚠️).\n\n"
    "## ✅ Healthy Areas\n"
    "Brief note on areas performing well.\n\n"
    "## 🔧 Recommendations\n\n"
    "### Priority 1 — Immediate Actions\n"
    "Numbered. Include exact Oracle SQL/commands.\n\n"
    "### Priority 2 — Short-Term (This Week)\n"
    "Numbered. Specific.\n\n"
    "### Priority 3 — Long-Term Optimizations\n"
    "Numbered. Architectural improvements.\n\n"
    "Reference specific metric values from the data. Be concise but precise."
)

_PERF_AWR_COMPARE = (
    "You are a senior Oracle DBA specialising in Oracle E-Business Suite performance tuning. "
    "Compare two AWR periods and produce a structured report.\n\n"
    "## 📊 Period Comparison Summary\n"
    "2-3 sentences. Include whether baseline or comparison is peak/off-peak.\n\n"
    "## 📉 Regressions (Comparison Period Worse)\n"
    "Prefix each with ❌. Show exact metric values and Δ%.\n\n"
    "## 📈 Improvements (Comparison Period Better)\n"
    "Prefix each with ✅.\n\n"
    "## 🔍 Root Cause Analysis\n"
    "Explain the most likely cause for each regression.\n\n"
    "## 🔧 Remediation\n"
    "### Priority 1 — Immediate Actions\nExact Oracle SQL / commands.\n\n"
    "### Priority 2 — Short-Term (This Week)\nSpecific tasks.\n\n"
    "Be concise and precise. Reference actual numbers."
)

_PERF_AWR_SINGLE = (
    "You are a senior Oracle DBA specialising in Oracle EBS performance tuning. "
    "Analyse this AWR report and produce a structured assessment.\n\n"
    "## 📊 AWR Report Summary\n"
    "## 🚨 Critical Findings  (❌ prefix)\n"
    "## ⚠️ Warnings  (⚠️ prefix)\n"
    "## ✅ Healthy Aspects\n"
    "## 🔧 Recommendations\n"
    "### Priority 1 — Immediate\n### Priority 2 — This Week\n### Priority 3 — Long-Term\n\n"
    "Include exact Oracle SQL / commands where relevant."
)

_PERF_AWR_UPLOAD_COMPARE = (
    "You are a senior Oracle DBA specialising in Oracle EBS performance tuning. "
    "Compare two AWR reports (Baseline vs Comparison).\n\n"
    "## 📊 Comparison Summary\n"
    "## 📉 Regressions (❌ prefix — with exact Δ values)\n"
    "## 📈 Improvements (✅ prefix)\n"
    "## 🔍 Root Cause Analysis\n"
    "## 🔧 Remediation\n"
    "### Priority 1 — Immediate\n### Priority 2 — This Week\n\n"
    "Reference specific numbers from both reports."
)


_HCM_SYSTEM = (
    "You are an expert Oracle E-Business Suite (EBS) R12 HCM & Payroll functional consultant. "
    "Help functional analysts and HR/payroll users with Core HR, Payroll, Self-Service HR (SSHR), "
    "absence and elements: setup steps, transaction flows (new hire, assignment changes, the payroll "
    "run → prepayments → costing → transfer-to-GL cycle), and troubleshooting (e.g. why a payroll "
    "action errored, a suspended assignment, missing element entries).\n\n"
    "You are READ-ONLY: explain, diagnose, and recommend read-only checks or navigation paths. Never "
    "instruct the user to run DML or describe an action as if you performed it.\n\n"
    "GROUNDING RULES — follow strictly:\n"
    "- When a message includes a [KNOWLEDGE BASE] block, treat it as authoritative and cite the source "
    "filename for specifics you draw from it.\n"
    "- Do NOT invent table/column names, profile options, API/package signatures, navigation paths, or "
    "form/responsibility names. If a specific detail is not in the knowledge base or well-established "
    "EBS HCM fundamentals, say so plainly and suggest the official Oracle documentation.\n"
    "- Prefer \"I'm not certain\" over presenting a guess as fact. HCM data is sensitive — never ask for "
    "or expose national identifiers, bank details, or salary amounts."
)

_HCM_INQUIRY_SUMMARIZE = (
    "You are an Oracle EBS R12 HCM & Payroll functional consultant interpreting the results of a "
    "READ-ONLY inquiry for a colleague. Given the inquiry description and its returned rows (JSON):\n"
    "- Summarise what the data shows in plain functional language.\n"
    "- Flag anything that warrants attention (errored payroll assignment actions, suspended/terminated "
    "assignments, missing element entries, closed/late periods).\n"
    "- Suggest read-only next checks or standard navigation (responsibility → form/report) to dig deeper.\n"
    "Base your answer ONLY on the rows provided — do not invent employees, amounts, or rows. If the "
    "result set is empty, say so and suggest why (e.g. wrong effective date, no matching record). "
    "Keep it concise and structured."
)


# ── Registry ─────────────────────────────────────────────────────────────────────
# Order here is the display order in the admin UI.
_DEFINITIONS = [
    {"key": "chat.system", "agent": "chat", "label": "Chat — System Prompt",
     "description": "Base system prompt for the general chat assistant (and the Knowledge Base agent).",
     "placeholders": [], "default": _CHAT_SYSTEM},
    {"key": "knowledge_base.grounding", "agent": "knowledge_base", "label": "Knowledge Base — Grounding",
     "description": "Wraps the question when retrieval finds KB context: use it, cite it, abstain rather than fabricate.",
     "placeholders": ["rag_context", "question"], "default": _KB_GROUNDING},
    {"key": "knowledge_base.no_context", "agent": "knowledge_base", "label": "Knowledge Base — No-Context Guard",
     "description": "Used when a substantive query finds no KB match: answer from standard knowledge only, don't invent specifics.",
     "placeholders": ["question"], "default": _KB_NO_CONTEXT},
    {"key": "deployment.system", "agent": "deployment", "label": "Deployment — ReAct System Prompt",
     "description": "ReAct loop instructions for the Code Deployment Agent. {tools} = tool list, {context} = collected fields.",
     "placeholders": ["tools", "context"], "default": _DEPLOYMENT_SYSTEM},
    {"key": "performance.diagnostic", "agent": "performance", "label": "Performance — Live Diagnostic Report",
     "description": "System prompt for the structured live-diagnostics performance report.",
     "placeholders": [], "default": _PERF_DIAGNOSTIC},
    {"key": "performance.awr_compare", "agent": "performance", "label": "Performance — AWR Period Comparison",
     "description": "System prompt for comparing two captured AWR periods.",
     "placeholders": [], "default": _PERF_AWR_COMPARE},
    {"key": "performance.awr_single", "agent": "performance", "label": "Performance — Single AWR Analysis",
     "description": "System prompt for analysing a single uploaded AWR report.",
     "placeholders": [], "default": _PERF_AWR_SINGLE},
    {"key": "performance.awr_upload_compare", "agent": "performance", "label": "Performance — AWR Upload Comparison",
     "description": "System prompt for comparing two uploaded AWR reports.",
     "placeholders": [], "default": _PERF_AWR_UPLOAD_COMPARE},
    {"key": "hcm.system", "agent": "hcm", "label": "HCM — Functional Advisor System Prompt",
     "description": "System prompt for the read-only HCM/Payroll functional advisor (free-text Q&A, RAG-grounded).",
     "placeholders": [], "default": _HCM_SYSTEM},
    {"key": "hcm.inquiry_summarize", "agent": "hcm", "label": "HCM — Inquiry Result Interpretation",
     "description": "System prompt that interprets read-only HCM/Payroll inquiry rows for a functional consultant.",
     "placeholders": [], "default": _HCM_INQUIRY_SUMMARIZE},
]

_BY_KEY = {d["key"]: d for d in _DEFINITIONS}


def definition(key):
    return _BY_KEY.get(key)


def all_definitions():
    return _DEFINITIONS


def build_out(key, override_content):
    """Pure serializer for the admin API: combine the registry default with an
    optional override (callers fetch the override via their own request session
    so reads/writes stay consistent under the test transaction)."""
    d = _BY_KEY[key]
    overridden = bool(override_content and override_content.strip())
    return {
        "key": d["key"], "agent": d["agent"], "label": d["label"],
        "description": d["description"], "placeholders": d["placeholders"],
        "default": d["default"],
        "content": override_content if overridden else d["default"],
        "is_overridden": overridden,
    }


def _apply(content, subs):
    for name, val in subs.items():
        content = content.replace("{" + name + "}", str(val))
    return content


def _overrides():
    """Map of prompt_key -> override content from the DB ({} on any failure)."""
    try:
        db = database.SessionLocal()
        try:
            return {p.prompt_key: p.content for p in db.query(models.AgentPrompt).all()}
        finally:
            db.close()
    except Exception:
        return {}


def get_prompt(key, **subs):
    """Resolved prompt text: admin override if set, else the built-in default,
    with {name} tokens replaced from ``subs``. Always falls back to the default."""
    d = _BY_KEY.get(key)
    default = d["default"] if d else ""
    content = default
    try:
        db = database.SessionLocal()
        try:
            row = db.query(models.AgentPrompt).filter(
                models.AgentPrompt.prompt_key == key
            ).first()
            if row and (row.content or "").strip():
                content = row.content
        finally:
            db.close()
    except Exception:
        content = default
    return _apply(content, subs)


def prompt_version(key) -> str:
    """Short stable id for the *resolved* prompt currently in effect for ``key``
    (admin override if set, else the built-in default). The hash changes whenever
    the prompt text does, so an interaction can be tied to the exact prompt that
    produced it — admin edits become visible as a new version. "" if unknown."""
    d = _BY_KEY.get(key)
    if not d:
        return ""
    content = d["default"]
    try:
        db = database.SessionLocal()
        try:
            row = db.query(models.AgentPrompt).filter(
                models.AgentPrompt.prompt_key == key
            ).first()
            if row and (row.content or "").strip():
                content = row.content
        finally:
            db.close()
    except Exception:
        content = d["default"]
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()[:12]


def missing_placeholders(key, content):
    """Required placeholder tokens absent from ``content`` (for save validation)."""
    d = _BY_KEY.get(key)
    if not d:
        return []
    return [p for p in d["placeholders"] if ("{" + p + "}") not in (content or "")]


def list_prompts():
    """All registry entries with their default + current (override or default)
    content and an is_overridden flag — for the admin UI."""
    ov = _overrides()
    out = []
    for d in _DEFINITIONS:
        override = ov.get(d["key"])
        overridden = bool(override and override.strip())
        out.append({
            "key": d["key"], "agent": d["agent"], "label": d["label"],
            "description": d["description"], "placeholders": d["placeholders"],
            "default": d["default"],
            "content": override if overridden else d["default"],
            "is_overridden": overridden,
        })
    return out
