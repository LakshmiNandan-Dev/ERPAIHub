import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.bootstrap import run_bootstrap
from app.core.middleware import TelemetryMiddleware
from app.routers import auth, chat, rag, rl, deployments, deployment_agent, performance_agent, admin, config, sso, monitoring, cloning, audit, training, patching

# Provision the schema and seed first-run data (admin, default LLM provider).
run_bootstrap()

app = FastAPI()

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


@app.get("/")
async def root():
    return {"message": "API Service Active"}


@app.get("/health")
def health():
    return {"message": "Health OK"}
