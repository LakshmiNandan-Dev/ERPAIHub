"""
Consumer config API — read-only, secret-free views of admin-managed resources.

These power the frontend dropdowns (SSH servers, environments, LLM providers)
that used to live in browser localStorage. Available to any authenticated user;
NO passwords or API keys are ever exposed here.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from typing import List

from app import database, schemas, models
from app.routers.auth import get_current_user

router = APIRouter(prefix="/config", tags=["Config"])


@router.get("/servers", response_model=List[schemas.ConfigServerOut])
def list_servers(db: Session = Depends(database.get_db),
                 _: models.User = Depends(get_current_user)):
    return db.query(models.SshServer).filter(
        models.SshServer.is_active == True  # noqa: E712
    ).order_by(models.SshServer.name.asc()).all()


@router.get("/environments", response_model=List[schemas.ConfigEnvironmentOut])
def list_environments(db: Session = Depends(database.get_db),
                      _: models.User = Depends(get_current_user)):
    return db.query(models.EbsEnvironment).filter(
        models.EbsEnvironment.is_active == True  # noqa: E712
    ).order_by(models.EbsEnvironment.name.asc()).all()


@router.get("/llm", response_model=List[schemas.ConfigLlmOut])
def list_llm(db: Session = Depends(database.get_db),
             _: models.User = Depends(get_current_user)):
    return db.query(models.LlmCredential).filter(
        models.LlmCredential.is_active == True  # noqa: E712
    ).order_by(models.LlmCredential.is_default.desc(), models.LlmCredential.provider.asc()).all()
