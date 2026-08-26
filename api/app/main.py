import os
from dotenv import load_dotenv
load_dotenv()   # api/.env, if present — must run before any app.* import below,
                # several of which read os.getenv(...) at module import time.

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.bootstrap import run_bootstrap
from app.core.middleware import TelemetryMiddleware
from app.core.auth import auth, sso
from app.core import scheduler as scheduler_module
from app.platform_api import chat, rag, admin, config, monitoring, audit
from app.ml import rl, training
from app.modules.dba.deployment import deployments, deployment_agent
from app.modules.dba.performance import performance_agent
from app.modules.dba.performance import monitoring as diagnostics_monitoring
from app.modules.dba.cloning import cloning
from app.modules.dba.patching import patching
from app.modules.dba.patching import patch_gap
from app.modules.dba.patching import patch_file_gap
from app.modules.dba.tickets import tickets
from app.modules.dba.compare import compare
from app.modules.dba.nl_sql import nl_sql
from app.modules.dba.rca import rca_agent
from app.modules.functional.hcm import hcm_agent

# Provision the schema and seed first-run data (admin, default LLM provider).
run_bootstrap()


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler_module.start()
    yield
    scheduler_module.shutdown()


app = FastAPI(lifespan=lifespan)

app.add_middleware(TelemetryMiddleware)

# CORS — allow the React frontend's origin. By default accept any host serving
# the frontend on port 5173 (covers localhost and remote-server IP/hostname
# deployments) without per-host config. Set CORS_ORIGINS (comma-separated) for a
# fixed allow-list, e.g. behind a domain / reverse proxy.
_cors_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
_cors_kwargs = dict(allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
if _cors_origins:
    _cors_kwargs["allow_origins"] = _cors_origins
else:
    _cors_kwargs["allow_origin_regex"] = r"https?://[^/]+:5173"
app.add_middleware(CORSMiddleware, **_cors_kwargs)

app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(rag.router)
app.include_router(rl.router)
app.include_router(deployments.router)
app.include_router(deployment_agent.router)
app.include_router(performance_agent.router)
app.include_router(admin.router)
app.include_router(config.router)
app.include_router(sso.router)
app.include_router(monitoring.router)
app.include_router(cloning.router)
app.include_router(audit.router)
app.include_router(training.router)
app.include_router(patching.router)
app.include_router(hcm_agent.router)
app.include_router(diagnostics_monitoring.router)
app.include_router(tickets.router)
app.include_router(patch_gap.router)
app.include_router(patch_file_gap.router)
app.include_router(compare.router)
app.include_router(nl_sql.router)
app.include_router(rca_agent.router)


@app.get("/")
async def root():
    return {"message": "API Service Active"}


@app.get("/health")
def health():
    return {"message": "Health OK"}
