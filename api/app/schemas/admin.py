from pydantic import BaseModel, EmailStr
from typing import Optional
from datetime import datetime


# ══════════════════════════════════════════════════════════════════════════════
# Admin — user management
# ══════════════════════════════════════════════════════════════════════════════

class AdminUserCreate(BaseModel):
    username: str
    email: EmailStr
    password: str
    role: str = "user"          # admin | dba | user
    is_admin: bool = False      # legacy; role wins


class AdminUserUpdate(BaseModel):
    email: Optional[EmailStr] = None
    password: Optional[str] = None
    is_active: Optional[bool] = None
    role: Optional[str] = None
    is_admin: Optional[bool] = None


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

    class Config:
        from_attributes = True
