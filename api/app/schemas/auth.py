from pydantic import BaseModel, EmailStr
from typing import Optional
from datetime import datetime


class UserBase(BaseModel):
    username: str
    email: EmailStr

class UserCreate(UserBase):
    password: str

class UserOut(UserBase):
    id: int
    is_active: bool
    is_admin: bool = False
    role: str = "user"
    auth_provider: str = "local"
    created_at: datetime

    class Config:
        from_attributes = True

class UserLogin(BaseModel):
    username: str
    password: str

class AgentOut(BaseModel):
    id: int
    name: str
    description: Optional[str] = None

    class Config:
        from_attributes = True

class RoleOut(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    agents: list[AgentOut] = []

    class Config:
        from_attributes = True

class UserWithRolesOut(UserOut):
    roles: list[RoleOut] = []

class SessionOut(BaseModel):
    session_token: str
    expires_at: datetime
    token_type: str = "bearer"
    user: Optional[UserWithRolesOut] = None

class UserChangePassword(BaseModel):
    username: str
    old_password: str
    new_password: str

class PasswordChangeRequest(BaseModel):
    """Authenticated self-service password change (operates on the current user)."""
    old_password: str
    new_password: str
