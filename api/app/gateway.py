from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from . import models
from .database import engine, get_db
from .routers import auth, chat, rag, rl, deployments, deployment_agent, performance_agent

models.Base.metadata.create_all(bind=engine)

gateway = FastAPI()

gateway.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

gateway.include_router(auth.router)
gateway.include_router(chat.router)
gateway.include_router(rag.router)
gateway.include_router(rl.router)
gateway.include_router(deployments.router)
gateway.include_router(deployment_agent.router)
gateway.include_router(performance_agent.router)


@gateway.get("/")
async def root():
    return {"message": "API Service Active"}


@gateway.get("/health")
def health():
    return {"message": "Health OK"}

