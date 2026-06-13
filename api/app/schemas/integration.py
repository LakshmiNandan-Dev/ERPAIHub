from pydantic import BaseModel
from typing import Optional
from datetime import datetime


# ══════════════════════════════════════════════════════════════════════════════
# Integration credentials (Git / Confluence)
# ══════════════════════════════════════════════════════════════════════════════

class IntegrationBase(BaseModel):
    kind: str  # 'git' | 'confluence'
    name: str
    host: Optional[str] = None
    base_url: Optional[str] = None
    username: Optional[str] = None
    is_default: bool = False
    is_active: bool = True


class IntegrationCreate(IntegrationBase):
    secret: Optional[str] = None  # PAT / API token


class IntegrationUpdate(BaseModel):
    name: Optional[str] = None
    host: Optional[str] = None
    base_url: Optional[str] = None
    username: Optional[str] = None
    secret: Optional[str] = None  # set to update; omit to keep existing
    is_default: Optional[bool] = None
    is_active: Optional[bool] = None


class IntegrationOut(IntegrationBase):
    id: int
    has_secret: bool = False
    created_at: datetime

    class Config:
        from_attributes = True
