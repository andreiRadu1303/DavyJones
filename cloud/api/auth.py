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
