from pydantic import BaseModel
from typing import Optional
from datetime import datetime


# ══════════════════════════════════════════════════════════════════════════════
# Admin — SSH servers
# ══════════════════════════════════════════════════════════════════════════════

class SshServerBase(BaseModel):
    name: str
    hostname: str
    port: int = 22
    username: Optional[str] = None
    server_type: str = "application"
    app_services: Optional[list[str]] = None
    description: Optional[str] = None
    is_active: bool = True


class SshServerCreate(SshServerBase):
    password: Optional[str] = None


class SshServerUpdate(BaseModel):
    name: Optional[str] = None
    hostname: Optional[str] = None
    port: Optional[int] = None
    username: Optional[str] = None
    password: Optional[str] = None  # set to update; omit to keep existing
    server_type: Optional[str] = None
    app_services: Optional[list[str]] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


class SshServerOut(SshServerBase):
    id: int
    has_password: bool = False
    created_at: datetime

    class Config:
        from_attributes = True


# ══════════════════════════════════════════════════════════════════════════════
# Admin — EBS environments
# ══════════════════════════════════════════════════════════════════════════════

class EnvironmentBase(BaseModel):
    name: str
    tier: str = "nonprod"   # 'prod' | 'nonprod'
    db_host: Optional[str] = None
    db_port: int = 1521
    db_sid: Optional[str] = None
    db_user: Optional[str] = "apps"
    # Extra EBS accounts used by the patching agent's adop tools (non-secret parts).
    system_user: Optional[str] = "system"       # system | ebs_system
    weblogic_user: Optional[str] = "weblogic"
    apps_os_user: Optional[str] = None           # OS owner of the apps tier (e.g. applmgr)
    description: Optional[str] = None
    ssh_server_id: Optional[int] = None
    is_active: bool = True


class EnvironmentCreate(EnvironmentBase):
    db_password: Optional[str] = None          # APPS schema password
    system_password: Optional[str] = None      # SYSTEM / EBS_SYSTEM password
    weblogic_password: Optional[str] = None    # WebLogic AdminServer password


class EnvironmentUpdate(BaseModel):
    name: Optional[str] = None
    tier: Optional[str] = None
    db_host: Optional[str] = None
    db_port: Optional[int] = None
    db_sid: Optional[str] = None
    db_user: Optional[str] = None
    db_password: Optional[str] = None  # set to update; omit to keep existing
    system_user: Optional[str] = None
    system_password: Optional[str] = None
    weblogic_user: Optional[str] = None
    weblogic_password: Optional[str] = None
    apps_os_user: Optional[str] = None
    description: Optional[str] = None
    ssh_server_id: Optional[int] = None
    is_active: Optional[bool] = None


class EnvironmentOut(EnvironmentBase):
    id: int
    has_password: bool = False
    has_system_password: bool = False
    has_weblogic_password: bool = False
    db_id: Optional[str] = None
    global_name: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True
