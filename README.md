# OraEBS Agent

An AI-powered developer and DBA workbench for Oracle E-Business Suite (EBS). It bundles four specialized agents — a conversational assistant, an automated code deployment pipeline, a database performance analyzer, and a document knowledge base — all behind a single React UI.

---

## What It Does

Oracle EBS teams deal with a fragmented toolchain: SQL*Plus for scripts, FNDLOAD for seed data, Forms Compiler for screens, Workflow Loader for approvals, Paramiko/SSH sessions for remote execution, and AWR reports for performance. OraEBS Agent replaces that context-switching with one interface where you describe what you want in plain language and agents handle the mechanics.

---

## Agents

### 1. AI Chat Assistant

A streaming chat interface backed by any LLM you configure. Every conversation is stored in PostgreSQL with full history.

**What makes it EBS-aware:**

- The system prompt constrains the model to Oracle EBS developer tasks: compilation, ADOP patching, form deployment, SQL diagnostics.
- Before answering a substantive question the chat engine searches the RAG knowledge base and injects matching documentation as verified context. A "RAG Knowledge Base Agent Invoked" label appears at the top of the response so you know when reference material was used.
- If the model refuses to answer or returns a canned refusal, the backend detects it and substitutes a hard-coded EBS fallback with real command examples (sqlplus, frmcmp_batch, FNDLOAD, WFLOAD).
- Every non-trivial AI response is audited asynchronously by the RLAIF service (see below) for hallucinations and correctness.

**Session management:**

- Create, rename, and delete named conversations.
- Sessions are auto-titled from the first message.
- The last five messages are used for short-term memory in the deployment interview flow.

**LLM provider switching** is done per conversation from the UI — no server restart needed:

| Provider  | Default model              | What you need     |
|-----------|----------------------------|-------------------|
| Ollama    | `llama3.2:1b`              | Ollama running (Docker handles this) |
| OpenAI    | `gpt-4o-mini`              | OpenAI API key    |
| Anthropic | `claude-haiku-4-5-20251001`| Anthropic API key |

API keys are passed as request headers from the frontend; nothing is stored server-side.

---

### 2. Code Deployment Agent

Converts unstructured deployment instructions (a Confluence page paste, a chat message, a PDF extract) into an ordered sequence of EBS deployment commands and executes them against the target environment.

**How it works:**

1. **Intent detection** — the chat router recognises deployment intent from the message (`deploy`, `patch`, `compile` keywords, combined with file extensions or tool names).
2. **Interview flow** — if the target environment (DEV / UAT / UAT2 / PROD) or file references are missing, the agent asks for them and waits. It tracks context across up to 4 previous turns so you can answer in follow-up messages.
3. **Step extraction** — once all parameters are collected, the source text is sent to Ollama with a prompt that asks it to return a JSON array of deployment steps. If the LLM output cannot be parsed, a regex fallback extracts steps directly from the text.
4. **Execution** — steps run in a background task. Three execution modes are available:

| Mode | When it activates | What happens |
|------|------------------|--------------|
| **Direct DB** | All steps are SQL/PL/SQL AND no SSH host is configured | Connects to Oracle via `python-oracledb` thin mode (no Instant Client needed) and executes statements directly |
| **SSH** | An SSH host is configured | Opens a Paramiko SSH + SFTP session, optionally clones the Git repo on the server, runs each command remotely |
| **Simulator** | No SSH or DB configured | Produces high-fidelity log output matching what each tool would actually print |

**Supported file types and their execution method:**

| File type | EBS tool | Example command |
|-----------|----------|-----------------|
| `.sql` | SQL*Plus | `sqlplus apps/apps @patch.sql` |
| `.pls` / `.plb` | PL/SQL compile | `ALTER PACKAGE xxap_pkg COMPILE BODY` |
| `.ldt` | FNDLOAD | `FNDLOAD apps/apps 0 Y UPLOAD @FND:... my_resp.ldt` |
| `.fmb` | Forms Compiler | `frmcmp_batch Module=custom.fmb ...` |
| `.wft` | WFLOAD | `WFLOAD apps/apps 0 Y UPLOAD custom.wft` |
| `.xml` | XMLImporter (OAF/JRAD) | `java oracle.jrad.tools.xml.importer.XMLImporter ...` |
| `.jar` / `.class` | Host copy | `cp custom.jar $JAVA_TOP/oracle/apps/custom/` |
| `.sh` | Shell script | `sh run_patch.sh` |

**Git source support:** if a Git repository URL is provided the agent clones it (authenticating with a personal access token) on the application server (real SSH) or locally on the API server (direct DB mode) before executing scripts.

**Credential injection:** stored database credentials (host, port, SID, user, password) are injected into every command at runtime, replacing any hardcoded `apps/apps` placeholder.

**Lifecycle states:** `pending → extracting → downloading → deploying → completed / failed / cancelled`

**Operations available after a run:**
- **Cancel** — stops after the current step completes.
- **Retry** — clears steps and re-runs from scratch.
- **Migrate** — clones a completed deployment and re-targets it to a different environment with new DB credentials.

**Self-learning:** every successfully executed step is automatically indexed into the RAG knowledge base as a markdown document containing the command, log output, and environment. Future chat queries can retrieve these real-execution records.

---

### 3. Performance Analysis Agent

Connects to an Oracle DB and runs a set of diagnostic queries, then streams an AI-generated report through Server-Sent Events so the analysis appears word-by-word.

**Diagnostic areas (all seven run by default, individually selectable):**

| Area | What it queries |
|------|----------------|
| Wait events | `V$SYSTEM_EVENT` — top waits by cumulative time, excluding idle |
| Top SQL | `V$SQLAREA` — top 5 statements by elapsed time, with execution count, disk reads, buffer gets |
| Memory | `V$SGA` / `V$PGASTAT` — SGA layout, PGA allocation, buffer cache hit %, shared pool hit % |
| Lock contention | `V$SESSION` — blocking sessions, wait duration, blocking SQL text |
| Tablespace usage | `DBA_TABLESPACE_USAGE_METRICS` — used/free GB and % for every tablespace |
| Concurrent Manager | Queue depth (pending / running / errored), long-running requests, frequently-erroring programs |
| Statistics | `DBA_TAB_STATISTICS` for stale stats, `DBA_OBJECTS` for invalid APPS objects |

When no real Oracle connection is provided, the agent generates deterministic simulated data seeded by environment name — useful for demos or testing the AI analysis on realistic-looking data.

The AI report always follows a fixed structure: executive summary → critical issues (❌) → warnings (⚠️) → healthy areas (✅) → prioritized recommendations (P1 immediate / P2 this week / P3 long-term) with exact Oracle SQL and commands.

**AWR (Automatic Workload Repository) features:**

| Feature | How to use |
|---------|-----------|
| Snapshot browser | Lists 168 hourly snapshots (7-day window) with peak/off-peak labels |
| Period comparison | Select baseline and comparison snap ranges; AI produces a period-over-period regression and improvement analysis |
| Report upload (single) | Upload an AWR text or HTML file; agent parses it and produces an assessment |
| Report upload (compare) | Upload two AWR files; agent compares them baseline vs. comparison |

---

### 4. RAG Knowledge Base

A document store that feeds verified context into every chat query, using two-stage
retrieval (embedding recall → cross-encoder reranking) with a live web fallback.

- **Upload:** PDF, TXT, or Markdown files up to 50 MB. The API responds immediately and indexes in the background using ChromaDB + `sentence-transformers` embeddings.
- **Duplicate guard:** each upload is hashed (SHA-256 of the raw bytes); re-uploading byte-identical content — even under a different filename — is rejected with `409 Conflict` naming the existing document. A previously *failed* index can still be retried.
- **Advanced retrieval:** a wide embedding search pulls candidate chunks (recall), then a cross-encoder reranker (`ms-marco-MiniLM-L-6-v2`) reorders them for precision; only chunks above the relevance floor are prepended to the LLM prompt as `[VERIFIED REFERENCE DOCUMENTATION]`. If the reranker can't load (e.g. air-gapped first run) retrieval falls back to embedding-distance filtering automatically.
- **Web fallback:** when the local knowledge base has no confident match, a live **DuckDuckGo** search grounds the answer instead of returning nothing. (Disabled per-tool for the MCP knowledge-base lookup, which stays local-only.)
- **Auto-indexing:** successful deployment step logs are automatically added as structured markdown documents so the agent can recall what commands were run against which environment.
- **Management:** list all documents (with status: indexing / ready / failed) and delete individual records.

**Retrieval tuning** (environment variables, all optional):

| Variable | Default | Description |
|----------|---------|-------------|
| `RAG_RERANK_ENABLED` | `1` | Enable cross-encoder reranking |
| `RAG_RERANK_MODEL` | `cross-encoder/ms-marco-MiniLM-L-6-v2` | Reranker model |
| `RAG_RERANK_CANDIDATES` | `20` | Candidates pulled before reranking |
| `RAG_RERANK_MIN_SCORE` | `0.0` | Relevance floor; below it a query is a local miss |
| `RAG_WEB_FALLBACK_ENABLED` | `1` | Fall back to DuckDuckGo on a local miss |

---

### 5. RLAIF Quality Auditor

After every non-trivial AI response is saved, a background task sends the query, the retrieved RAG context, and the assistant's reply to a QA prompt asking the LLM to score the response on three axes: faithfulness (no hallucinations beyond the reference docs), correctness (accurate Oracle SQL and EBS commands), and helpfulness. The score (+1 / -1), the reasoning, and a suggested correction are stored on the message record. No action is taken automatically — this data is available for review and future training.

---

## Architecture

```
Browser (React 19 + Vite)
    │   Server-Sent Events (streaming)
    │   REST (sessions, documents, deployments)
    ▼
FastAPI  :8000
    ├── Auth router        — session-based auth (24 h tokens in PostgreSQL)
    ├── Chat router        — SSE streaming, RAG injection, deployment intent detection
    ├── RAG router         — upload / list / delete documents; ChromaDB indexing
    ├── Deployments router — create / cancel / retry / migrate deployment runs
    ├── Deployment agent   — background task: extract steps, execute via DB or SSH
    ├── Performance agent  — diagnostic queries + AWR analysis
    └── RLAIF service      — background QA audit per AI response
    │
    ├── PostgreSQL :5432   — users, sessions, chat history, RAG records, deployment runs + steps
    ├── ChromaDB           — vector embeddings (embedded inside API container)
    ├── Ollama  :11434     — local LLM inference
    └── Redis   :6379      — available for caching / queuing
```

---

## Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (includes Docker Compose)
- [Node.js 20+](https://nodejs.org/) — only needed for local dev without Docker
- [Python 3.11+](https://www.python.org/downloads/) — only needed for local dev without Docker

Verify Docker is running:

```bash
docker info
```

---

## Quick Start — Docker (Recommended)

```bash
docker compose up --build
```

This builds and starts all services. On first run Docker pulls base images; subsequent starts skip the build:

```bash
docker compose up
```

| URL | Service |
|-----|---------|
| http://localhost:5173 | React frontend |
| http://localhost:8000 | FastAPI REST API |
| http://localhost:8000/docs | Swagger / OpenAPI |
| http://localhost:8000/health | Health check |

Stop everything:

```bash
docker compose down
```

Stop and wipe all data (database, model cache, vector store):

```bash
docker compose down -v
```

---

## Pull an LLM Model into Ollama

Ollama starts with no models. Pull the default the app expects:

```bash
docker exec -it ollama ollama pull llama3.2:1b
```

Any model from the [Ollama library](https://ollama.com/library) works. Pull additional models and switch between them from the UI's model manager. Larger models give better deployment step extraction and performance analysis — try `llama3` or `mistral` if you have enough RAM.

---

## First-Time Setup

1. Open http://localhost:5173
2. Register an account (the first user you create is stored in PostgreSQL — no admin seeding needed)
3. Log in
4. (Optional) Upload reference documents to the knowledge base via the **RAG** panel
5. Switch the active agent at the top of the UI to choose between Chat, Deployment, or Performance

---

## Using Cloud LLMs

API keys are entered in the UI's model settings panel and sent as request headers (`X-LLM-Provider`, `X-LLM-Model`, `X-LLM-Api-Key`). Nothing is stored on the server.

```
X-LLM-Provider: openai       # or: anthropic, ollama
X-LLM-Model:    gpt-4o       # optional — overrides the default
X-LLM-Api-Key:  sk-...       # required for openai / anthropic
```

---

## Connecting a Real Oracle Database (optional)

Without a real DB the deployment agent runs in simulator mode and the performance agent uses synthetic data. To connect real environments, supply credentials when creating a deployment or running a performance analysis:

| Field | Description |
|-------|-------------|
| `db_host` | Oracle DB hostname or IP |
| `db_port` | Listener port (default 1521) |
| `db_sid` | SID or service name |
| `db_user` | Schema user (e.g. `apps`) |
| `db_password` | Schema password |

The API uses `python-oracledb` in thin mode — no Oracle Instant Client installation required.

---

## Connecting via SSH (optional)

To run deployment commands on the actual Oracle EBS application server:

| Field | Description |
|-------|-------------|
| `ssh_host` | Application server hostname |
| `ssh_port` | SSH port (default 22) |
| `ssh_username` | OS user on the app server |
| `ssh_password` | OS user password |

With SSH configured the agent SFTPs files and executes commands remotely. Without it, commands run against the DB directly (SQL/PL/SQL only) or simulate execution.

---

## Local Development (without Docker)

Use this path if you want hot-reload on both layers during development.

### Start infrastructure only

```bash
docker compose up postgres ollama redis
```

### Backend

```bash
cd api

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt

# Apply database schema
alembic upgrade head

uvicorn app.gateway:gateway --reload --host 0.0.0.0 --port 8000
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql://aiuser:aipassword@localhost:5432/erpai_hub` | PostgreSQL connection |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama base URL |
| `APP_SECRET_KEY` | `dev-insecure-change-me` | Passphrase used to encrypt admin-managed secrets (SSH/DB passwords, LLM API keys) at rest. **Set a strong random value in production** — changing it later makes previously stored secrets unreadable. |

Create `api/.env` for local dev — the app picks it up automatically via `python-dotenv`:

```env
DATABASE_URL=postgresql://aiuser:aipassword@localhost:5432/erpai_hub
OLLAMA_URL=http://localhost:11434
APP_SECRET_KEY=change-me-to-a-long-random-string
```

### Admin Console

The **first registered user automatically becomes an administrator**. Admins get an
**Admin Console** (user-menu → Admin Console) to centrally manage:

- **Users** — create accounts, grant/revoke admin, enable/disable, reset passwords.
- **SSH Servers** & **Environments** — connection details and credentials, encrypted at rest.
- **LLM Providers** — API keys for OpenAI / Anthropic / Gemini, encrypted at rest and injected
  server-side (keys are never sent from the browser).

Promote an existing user to admin from the `api/` directory:

```bash
python -m scripts.create_admin <username>
```

---

## Database Migrations

```bash
# After editing api/app/models.py:
alembic revision --autogenerate -m "describe your change"
alembic upgrade head

# Roll back one step:
alembic downgrade -1
```

---

## Project Layout

```
oraebsagent/
├── docker-compose.yml
├── api/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── alembic/                    # Migration scripts
│   └── app/
│       ├── gateway.py              # FastAPI app + CORS
│       ├── models.py               # SQLAlchemy ORM models
│       ├── schemas.py              # Pydantic request / response schemas
│       ├── database.py             # DB session factory
│       ├── llm_service.py          # Ollama / OpenAI / Anthropic streaming + sync
│       ├── rag_service.py          # ChromaDB indexing and retrieval
│       ├── rlaif_service.py        # Background QA audit
│       ├── mcp_server.py           # MCP integration
│       └── routers/
│           ├── auth.py             # Register / login / logout / change-password
│           ├── chat.py             # Chat sessions, SSE stream, deployment intent
│           ├── rag.py              # Document upload / list / delete
│           ├── deployments.py      # Deployment runs: CRUD, cancel, retry, migrate
│           ├── deployment_agent.py # Deployment agent background worker
│           └── performance_agent.py# Performance diagnostics + AWR analysis
└── frontend/
    ├── Dockerfile
    ├── package.json
    └── src/
        ├── App.jsx
        ├── api.js                  # Axios client with auth headers
        ├── ModelManager.jsx        # LLM provider / model switcher
        └── components/
            ├── Auth/               # Login / register screens
            ├── Chat/               # Streaming chat UI
            ├── Deployment/         # Deployment panel + step log viewer
            ├── Performance/        # Diagnostics dashboard + AWR UI
            └── Rag/                # Knowledge base manager
```

---

## Troubleshooting

**Port already in use**
Change the host port in `docker-compose.yml` (e.g. `"5433:5432"` for Postgres) or stop the conflicting process.

**API container exits immediately**
The API waits for Postgres to pass its healthcheck. Run `docker compose logs postgres` to see if the DB is starting correctly.

**Ollama returns "model not found"**
Pull the model first: `docker exec -it ollama ollama pull llama3.2:1b`

**Deployment steps all fail in Direct DB mode**
Check that `db_host`, `db_port`, `db_sid`, `db_user`, and `db_password` are all set when triggering the deployment. Without all five the agent logs `[DB] Cannot execute: DB credentials not configured`.

**ChromaDB errors on API start**
The `chroma_db` directory is persisted as a Docker volume (`chroma_data`). If the volume is corrupted, remove it with `docker compose down -v` and restart.

**Frontend cannot reach the API**
The frontend calls `http://localhost:8000`. Confirm the API container is running (`docker compose ps`) and that port 8000 is not blocked by a firewall or another process.

---

## License

This project is open source under the [MIT License](LICENSE).
