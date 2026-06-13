from pydantic import BaseModel
from typing import Optional


# ══════════════════════════════════════════════════════════════════════════════
# SSO (OIDC / Microsoft Entra ID)
# ══════════════════════════════════════════════════════════════════════════════

class SsoSettingsOut(BaseModel):
    provider: str = "entra"
    enabled: bool = False
    signup_enabled: bool = False
    tenant_id: Optional[str] = None
    client_id: Optional[str] = None
    redirect_uri: Optional[str] = None
    auto_provision: bool = True
    has_client_secret: bool = False


class SsoSettingsUpdate(BaseModel):
    enabled: Optional[bool] = None
    signup_enabled: Optional[bool] = None
    tenant_id: Optional[str] = None
    client_id: Optional[str] = None
    client_secret: Optional[str] = None  # set to update; omit to keep existing
    redirect_uri: Optional[str] = None
    auto_provision: Optional[bool] = None


class SsoStatusOut(BaseModel):
    """Public — tells the login screen whether to show the SSO button / sign-up."""
    enabled: bool = False
    provider: str = "entra"
    button_label: str = "Sign in with Microsoft"
    signup_enabled: bool = False
