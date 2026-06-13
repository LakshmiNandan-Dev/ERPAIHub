from pydantic import BaseModel
from typing import Optional


# ══════════════════════════════════════════════════════════════════════════════
# Consumer (non-secret) config views
# ══════════════════════════════════════════════════════════════════════════════

class ConfigServerOut(BaseModel):
    id: int
    name: str
    hostname: str
    port: int
    username: Optional[str] = None
    server_type: str
    app_services: Optional[list[str]] = None

    class Config:
        from_attributes = True


class ConfigEnvironmentOut(BaseModel):
    id: int
    name: str
    db_host: Optional[str] = None
    db_port: int
    db_sid: Optional[str] = None
    db_user: Optional[str] = None

    class Config:
        from_attributes = True


class ConfigLlmOut(BaseModel):
    id: int
    provider: str
    label: Optional[str] = None
    model: Optional[str] = None
    base_url: Optional[str] = None
    is_default: bool

    class Config:
        from_attributes = True
