"""
Connectivity / credential tests for admin-managed integrations.

Each function returns {"ok": bool, "message": str} — the same shape the SSH,
environment and LLM provider tests use, so the Admin Console renders them
identically. Every network call is wrapped so a test can never raise.
"""
import base64

import httpx

TIMEOUT = 15.0


# ── Git ─────────────────────────────────────────────────────────────────────────

def test_git(host: str, username: str, token: str) -> dict:
    host = (host or "").strip().lower().rstrip("/")
    if not token:
        return {"ok": False, "message": "No token stored — add a Personal Access Token first."}
    if not host:
        return {"ok": False, "message": "No host set — add the Git host (e.g. github.com) to enable testing."}
    try:
        if "github" in host:
            # api.github.com for cloud; /api/v3 for GitHub Enterprise Server
            api = ("https://api.github.com/user"
                   if host in ("github.com", "api.github.com")
                   else f"https://{host}/api/v3/user")
            r = httpx.get(api, headers={"Authorization": f"Bearer {token}",
                                        "Accept": "application/vnd.github+json"},
                          timeout=TIMEOUT, follow_redirects=True)
            if r.status_code == 200:
                return {"ok": True, "message": f"GitHub token valid — authenticated as {r.json().get('login')}."}
            return {"ok": False, "message": f"GitHub rejected the token (HTTP {r.status_code})."}

        if "gitlab" in host:
            r = httpx.get(f"https://{host}/api/v4/user", headers={"PRIVATE-TOKEN": token},
                          timeout=TIMEOUT, follow_redirects=True)
            if r.status_code == 200:
                return {"ok": True, "message": f"GitLab token valid — authenticated as {r.json().get('username')}."}
            return {"ok": False, "message": f"GitLab rejected the token (HTTP {r.status_code})."}

        if "bitbucket" in host:
            if username:
                auth = base64.b64encode(f"{username}:{token}".encode()).decode()
                headers = {"Authorization": f"Basic {auth}"}
            else:
                headers = {"Authorization": f"Bearer {token}"}
            r = httpx.get("https://api.bitbucket.org/2.0/user", headers=headers,
                          timeout=TIMEOUT, follow_redirects=True)
            if r.status_code == 200:
                return {"ok": True, "message": f"Bitbucket token valid — {r.json().get('username', 'authenticated')}."}
            return {"ok": False, "message": f"Bitbucket rejected the token (HTTP {r.status_code})."}

        # Unknown / self-hosted host: we can't deep-verify the token without a repo URL.
        # Confirm the host is reachable over HTTPS and report honestly.
        r = httpx.get(f"https://{host}", timeout=TIMEOUT, follow_redirects=True)
        return {"ok": True, "message": (f"Host {host} reachable (HTTP {r.status_code}). Token stored — "
                                        "it will be validated against the repository at clone time.")}
    except Exception as exc:
        return {"ok": False, "message": f"Could not reach {host} — {exc}"}


# ── Confluence ──────────────────────────────────────────────────────────────────

def test_confluence(host: str, username: str, token: str) -> dict:
    host = (host or "").strip().lower().rstrip("/")
    if not token:
        return {"ok": False, "message": "No API token / PAT stored — add one first."}
    if not host:
        return {"ok": False, "message": "No site host set (e.g. mysite.atlassian.net)."}

    is_cloud = host.endswith("atlassian.net")
    api = f"https://{host}/wiki/rest/api/space?limit=1" if is_cloud else f"https://{host}/rest/api/space?limit=1"

    def _call(headers):
        return httpx.get(api, headers={**headers, "Accept": "application/json"},
                         timeout=TIMEOUT, follow_redirects=True)

    try:
        if username:  # Cloud: email + API token → Basic
            auth = base64.b64encode(f"{username}:{token}".encode()).decode()
            r = _call({"Authorization": f"Basic {auth}"})
        else:         # Server/DC: bare PAT → Bearer
            r = _call({"Authorization": f"Bearer {token}"})
            if r.status_code == 401:  # some Server setups want Basic with ":token"
                auth = base64.b64encode(f":{token}".encode()).decode()
                r = _call({"Authorization": f"Basic {auth}"})

        if r.status_code == 200:
            return {"ok": True, "message": f"Confluence reachable and credentials accepted ({host})."}
        if r.status_code in (401, 403):
            return {"ok": False, "message": f"Confluence rejected the credentials (HTTP {r.status_code}). "
                                            "For Cloud set the account email as Username; for Server/DC use a bare PAT."}
        return {"ok": False, "message": f"Confluence returned HTTP {r.status_code} for {api}."}
    except Exception as exc:
        return {"ok": False, "message": f"Could not reach Confluence — {exc}"}


# ── SSO (Microsoft Entra ID / OIDC) ─────────────────────────────────────────────

# AADSTS codes that mean the credential itself is bad (vs. just missing Graph consent)
_BAD_CREDENTIAL_CODES = ("AADSTS7000215",  # invalid client secret
                         "AADSTS700016",   # application not found in tenant
                         "AADSTS90002",    # tenant not found
                         "AADSTS700027",   # client assertion failed
                         "AADSTS7000222")  # expired client secret


def test_sso(tenant_id: str, client_id: str, client_secret: str) -> dict:
    tenant_id = (tenant_id or "").strip()
    if not tenant_id:
        return {"ok": False, "message": "Tenant ID is required."}

    # 1. Validate the tenant via the OpenID discovery document.
    disc = f"https://login.microsoftonline.com/{tenant_id}/v2.0/.well-known/openid-configuration"
    try:
        r = httpx.get(disc, timeout=TIMEOUT)
        if r.status_code != 200:
            return {"ok": False, "message": f"Tenant discovery failed (HTTP {r.status_code}) — check the Tenant ID."}
    except Exception as exc:
        return {"ok": False, "message": f"Could not reach Microsoft login endpoint — {exc}"}

    if not (client_id and client_secret):
        return {"ok": True, "message": "Tenant discovery OK. Add Client ID + secret and save to verify the app registration."}

    # 2. Authenticate the app (client_credentials) to validate client_id + secret.
    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = {"grant_type": "client_credentials", "client_id": client_id,
            "client_secret": client_secret, "scope": "https://graph.microsoft.com/.default"}
    try:
        r = httpx.post(token_url, data=data, timeout=TIMEOUT)
        if r.status_code == 200 and r.json().get("access_token"):
            return {"ok": True, "message": "Entra ID app verified — tenant, Client ID and secret are valid."}
        body = {}
        try:
            body = r.json()
        except Exception:
            pass
        desc = str(body.get("error_description") or body.get("error") or f"HTTP {r.status_code}")
        first = desc.splitlines()[0][:180]
        if any(code in desc for code in _BAD_CREDENTIAL_CODES) or body.get("error") in ("invalid_client", "unauthorized_client"):
            return {"ok": False, "message": f"App credentials rejected: {first}"}
        # Authenticated, but Graph .default not consented — fine for OIDC sign-in, which doesn't need it.
        return {"ok": True, "message": f"Client ID + secret authenticated. ({first}) "
                                       "This is sufficient for SSO sign-in."}
    except Exception as exc:
        return {"ok": False, "message": f"Token request failed — {exc}"}
