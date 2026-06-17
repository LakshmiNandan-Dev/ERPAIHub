from pydantic import BaseModel, EmailStr
from typing import Optional, List
from datetime import datetime


# ══════════════════════════════════════════════════════════════════════════════
# Admin — agent prompts (editable instruction overrides)
# ══════════════════════════════════════════════════════════════════════════════

class PromptUpdate(BaseModel):
    content: str


class PromptOut(BaseModel):
    key: str
    agent: str
    label: str
    description: Optional[str] = None
    placeholders: List[str] = []
    default: str
    content: str            # current effective text (override or default)
    is_overridden: bool = False


# ══════════════════════════════════════════════════════════════════════════════
# Admin — roles & agent permissions (RBAC)
# ══════════════════════════════════════════════════════════════════════════════

class RoleBrief(BaseModel):
    """Compact role reference embedded in a user row."""
    id: int
    name: str

    class Config:
        from_attributes = True


class AgentBrief(BaseModel):
    """A gated agent that can be granted to a role."""
    name: str
    description: Optional[str] = None


class RoleDetailOut(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    agents: List[AgentBrief] = []     # gated agents this role grants
    user_count: int = 0               # how many users hold this role


class RoleCreate(BaseModel):
    name: str
    description: Optional[str] = None
    agent_names: List[str] = []       # gated-agent names to grant


class RoleUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    agent_names: Optional[List[str]] = None   # None = leave grants unchanged


# ══════════════════════════════════════════════════════════════════════════════
# Admin — user management
# ══════════════════════════════════════════════════════════════════════════════

class AdminUserCreate(BaseModel):
    username: str
    email: EmailStr
    password: str
    role: str = "user"               # admin | dba | user (coarse tier)
    is_admin: bool = False           # legacy; role wins
    role_ids: Optional[List[int]] = None   # RBAC roles granting agent access


class AdminUserUpdate(BaseModel):
    email: Optional[EmailStr] = None
    password: Optional[str] = None
    is_active: Optional[bool] = None
    role: Optional[str] = None
    is_admin: Optional[bool] = None
    role_ids: Optional[List[int]] = None   # None = leave role assignments unchanged


class AdminUserOut(BaseModel):
    id: int
    username: str
    email: EmailStr
    is_active: bool
    is_admin: bool
    role: str = "user"
    approval_status: str = "approved"
    auth_provider: str
    created_at: datetime
    roles: List[RoleBrief] = []      # assigned RBAC roles

    class Config:
        from_attributes = True
