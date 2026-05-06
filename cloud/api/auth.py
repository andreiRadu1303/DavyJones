"""Authentication — OAuth2 (GitHub/Google) + JWT token issuance."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from jose import jwt
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cloud.config import settings
from cloud.db import get_db
from cloud.models.user import User
from cloud.models.subscription import Subscription

router = APIRouter(prefix="/auth", tags=["auth"])


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    email: str
    plan: str


def create_jwt(user_id: str, email: str) -> str:
    """Create a signed JWT for the given user."""
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    payload = {
        "sub": user_id,
        "email": email,
        "exp": expire,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_jwt(token: str) -> dict:
    """Decode and validate a JWT. Raises HTTPException on failure."""
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


async def get_current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    """FastAPI dependency — extract and validate the current user from JWT."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    token = auth_header[7:]
    payload = decode_jwt(token)
    user_id = payload.get("sub")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


@router.get("/login/{provider}")
async def login_redirect(provider: str, request: Request, redirect_to: str = ""):
    """Redirect to OAuth provider login page.

    Optional redirect_to: after auth, redirect here with ?token=...&email=...
    Used by the Obsidian plugin's local callback server.
    """
    import urllib.parse
    state = urllib.parse.quote(redirect_to) if redirect_to else ""

    if provider == "github":
        return RedirectResponse(
            f"https://github.com/login/oauth/authorize"
            f"?client_id={settings.github_client_id}"
            f"&scope=user:email"
            f"&redirect_uri={settings.api_url}/api/v1/auth/callback/github"
            f"&state={state}"
        )
    elif provider == "google":
        return RedirectResponse(
            f"https://accounts.google.com/o/oauth2/v2/auth"
            f"?client_id={settings.google_client_id}"
            f"&response_type=code"
            f"&scope=openid+email+profile"
            f"&redirect_uri={settings.api_url}/api/v1/auth/callback/google"
            f"&state={state}"
        )
    raise HTTPException(status_code=400, detail=f"Unknown provider: {provider}")


@router.get("/callback/{provider}")
async def oauth_callback(provider: str, code: str, state: str = "", db: AsyncSession = Depends(get_db)):
    """Handle OAuth callback — exchange code for token, create/find user, return JWT."""
    import httpx

    if provider == "github":
        # Exchange code for access token
        async with httpx.AsyncClient() as client:
            token_resp = await client.post(
                "https://github.com/login/oauth/access_token",
                json={
                    "client_id": settings.github_client_id,
                    "client_secret": settings.github_client_secret,
                    "code": code,
                },
                headers={"Accept": "application/json"},
            )
            access_token = token_resp.json().get("access_token")
            if not access_token:
                raise HTTPException(status_code=400, detail="Failed to get GitHub token")

            # Get user info
            user_resp = await client.get(
                "https://api.github.com/user",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            gh_user = user_resp.json()

            # Get primary email
            emails_resp = await client.get(
                "https://api.github.com/user/emails",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            primary_email = next(
                (e["email"] for e in emails_resp.json() if e.get("primary")),
                gh_user.get("email", ""),
            )

        oauth_id = str(gh_user["id"])
        name = gh_user.get("name") or gh_user.get("login", "")
        email = primary_email

    elif provider == "google":
        async with httpx.AsyncClient() as client:
            token_resp = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": settings.google_client_id,
                    "client_secret": settings.google_client_secret,
                    "redirect_uri": f"{settings.api_url}/api/v1/auth/callback/google",
                    "grant_type": "authorization_code",
                },
            )
            tokens = token_resp.json()
            access_token = tokens.get("access_token")
            if not access_token:
                raise HTTPException(status_code=400, detail="Failed to get Google token")

            user_resp = await client.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            g_user = user_resp.json()

        oauth_id = g_user["id"]
        name = g_user.get("name", "")
        email = g_user.get("email", "")
    else:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider}")

    # Find or create user
    result = await db.execute(
        select(User).where(User.oauth_provider == provider, User.oauth_id == oauth_id)
    )
    user = result.scalar_one_or_none()

    plan = "free"
    if not user:
        user = User(
            email=email,
            name=name,
            oauth_provider=provider,
            oauth_id=oauth_id,
        )
        db.add(user)
        sub = Subscription(user_id=user.id, plan="free", status="active")
        db.add(sub)
        await db.commit()
        await db.refresh(user)
    else:
        # Load subscription without lazy-loading
        from sqlalchemy import select as sa_select
        from cloud.models.subscription import Subscription as Sub
        sub_result = await db.execute(sa_select(Sub).where(Sub.user_id == user.id))
        sub = sub_result.scalar_one_or_none()
        if sub:
            plan = sub.plan

    # Issue JWT
    token = create_jwt(user.id, user.email)

    # If plugin passed a redirect_to (local callback server), redirect there with token
    if state:
        import urllib.parse
        redirect_to = urllib.parse.unquote(state)
        params = urllib.parse.urlencode({
            "token": token,
            "email": user.email,
            "user_id": user.id,
            "plan": plan,
        })
        return RedirectResponse(f"{redirect_to}?{params}")

    # Return token as JSON (browser flow)
    return TokenResponse(
        access_token=token,
        user_id=user.id,
        email=user.email,
        plan=plan,
    )


# ── Google Workspace BYOC OAuth callback ──────────────────────────
# This is hit by Google after the user authorizes via *their own* OAuth
# client. We use the user's stored client_secret (set via /vaults/{id}/gws/setup)
# to exchange the code for a refresh token, then store the credentials in
# the vault's K8s Secret in the format the agent's gws CLI expects.

@router.get("/gws/callback")
async def gws_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    if error:
        return _gws_html_response(False, f"Google returned an error: {error}")
    if not code or not state:
        return _gws_html_response(False, "Missing code or state from Google.")

    # Decode signed state to find the vault
    import jwt as _jwt
    try:
        payload = _jwt.decode(state, settings.jwt_secret, algorithms=["HS256"])
        vault_id = payload["vault_id"]
    except Exception:
        return _gws_html_response(False, "Invalid or expired state token.")

    from cloud.models.vault import Vault as _Vault
    result = await db.execute(select(_Vault).where(_Vault.id == vault_id))
    vault = result.scalar_one_or_none()
    if not vault:
        return _gws_html_response(False, "Vault not found.")

    # Read the user's OAuth client creds back from the K8s Secret to do
    # the code-for-token exchange.
    try:
        from kubernetes import client as k8s_client, config as k8s_config
        try:
            k8s_config.load_incluster_config()
        except Exception:
            k8s_config.load_kube_config()
        import base64
        namespace = f"{settings.k8s_namespace_prefix}{vault.user_id[:8]}"
        core = k8s_client.CoreV1Api()
        sec = core.read_namespaced_secret(
            name=f"vault-credentials-{vault.slug}",
            namespace=namespace,
        )
        data = sec.data or {}
        client_id = base64.b64decode(data.get("GWS_CLIENT_ID", "")).decode()
        client_secret = base64.b64decode(data.get("GWS_CLIENT_SECRET", "")).decode()
        if not client_id or not client_secret:
            return _gws_html_response(False, "OAuth client credentials missing — re-run setup.")
    except Exception as e:
        logger.exception("Failed to read GWS client creds")
        return _gws_html_response(False, f"Failed to load credentials: {e}")

    # Exchange code → refresh_token using the user's own client_secret
    redirect_uri = f"{settings.api_url}/api/v1/auth/gws/callback"
    try:
        async with httpx.AsyncClient() as client:
            tok_resp = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            tokens = tok_resp.json()
        if "refresh_token" not in tokens:
            return _gws_html_response(
                False,
                "Google didn't return a refresh token. Try revoking access at "
                "myaccount.google.com/permissions and retry.",
            )

        # Fetch the user's email so we can show "Connected as foo@bar.com"
        email = ""
        try:
            async with httpx.AsyncClient() as client:
                ui = await client.get(
                    "https://www.googleapis.com/oauth2/v2/userinfo",
                    headers={"Authorization": f"Bearer {tokens.get('access_token', '')}"},
                )
                email = ui.json().get("email", "")
        except Exception:
            pass

        # Stored format: a single JSON blob the gws CLI can consume.
        # See https://github.com/sigsep/gws-cli for the expected shape.
        creds_json = {
            "type": "authorized_user",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": tokens["refresh_token"],
            "email": email,
        }
        import json as _json
        creds_b64 = base64.b64encode(_json.dumps(creds_json).encode()).decode()
        core.patch_namespaced_secret(
            name=f"vault-credentials-{vault.slug}",
            namespace=namespace,
            body={"data": {"GWS_CREDENTIALS_JSON": creds_b64}},
        )
    except Exception as e:
        logger.exception("GWS token exchange failed")
        return _gws_html_response(False, f"Token exchange failed: {e}")

    return _gws_html_response(True, f"Connected{' as ' + email if email else ''}.")


def _gws_html_response(ok: bool, message: str):
    """Render a tiny success/failure page the user sees in their browser
    after the Google authorize redirect lands here. The plugin polls
    /vaults/{id}/gws/status separately to detect completion — it doesn't
    rely on this page."""
    from fastapi.responses import HTMLResponse
    color = "#22c55e" if ok else "#ef4444"
    title = "DavyJones · Google Workspace connected" if ok else "DavyJones · Connection failed"
    icon = "✓" if ok else "✗"
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>body{{font-family:system-ui;background:#0b0d10;color:#e8eaed;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}}
.card{{max-width:480px;padding:32px;background:#16191e;border-radius:12px;text-align:center}}
.icon{{font-size:48px;color:{color};margin-bottom:16px}}
h1{{font-size:20px;margin:0 0 12px}}
p{{color:#9aa4b2;margin:0;line-height:1.5}}</style></head>
<body><div class="card"><div class="icon">{icon}</div>
<h1>{title.split(' · ')[1]}</h1><p>{message}</p>
<p style="margin-top:16px;font-size:13px">You can close this tab and return to Obsidian.</p>
</div></body></html>"""
    return HTMLResponse(content=html, status_code=200 if ok else 400)
