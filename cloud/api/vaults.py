"""Vault management — register, list, activate, configure."""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cloud.api.auth import get_current_user
from cloud.config import settings
from cloud.db import get_db
from cloud.models.user import User
from cloud.models.vault import Vault

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/vaults", tags=["vaults"])


class VaultCreate(BaseModel):
    name: str
    slug: str | None = None
    claude_token: str | None = None


class VaultResponse(BaseModel):
    id: str
    name: str
    slug: str
    git_repo_url: str
    git_push_url: str  # URL with embedded credentials for plugin git push
    config: dict

    class Config:
        from_attributes = True


class VaultConfigUpdate(BaseModel):
    claude_token: str | None = None
    github_token: str | None = None
    gitlab_token: str | None = None
    gitlab_api_url: str | None = None
    slack_bot_token: str | None = None
    slack_app_token: str | None = None
    gws_config: dict | None = None
    vault_rules: dict | None = None


def _slugify(name: str) -> str:
    """Convert vault name to a safe slug."""
    import re
    slug = re.sub(r"[^a-z0-9-]", "-", name.lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:60]


def _git_push_url(internal_url: str) -> str:
    """Build an authenticated git push URL using the external Forgejo address."""
    if not internal_url or not settings.forgejo_admin_token:
        return internal_url or ""
    # Replace internal host with external host in the URL
    external_base = settings.forgejo_external_url or settings.forgejo_url
    # Embed credentials: http://user:token@host/org/repo.git
    from urllib.parse import urlparse, urlunparse
    parsed = urlparse(internal_url)
    # Use external URL as base
    ext_parsed = urlparse(external_base)
    push_url = urlunparse((
        ext_parsed.scheme,
        f"{settings.forgejo_admin_user}:{settings.forgejo_admin_token}@{ext_parsed.netloc}",
        parsed.path,
        "", "", "",
    ))
    return push_url


async def _provision_git_repo(user_id: str, slug: str) -> str:
    """Create a bare git repo on Forgejo for this vault. Returns internal clone URL."""
    import httpx

    if not settings.forgejo_admin_token:
        logger.warning("No forgejo_admin_token — skipping git repo provisioning")
        return ""

    org_name = f"user-{user_id[:8]}"

    async with httpx.AsyncClient() as client:
        # Ensure org exists
        await client.post(
            f"{settings.forgejo_url}/api/v1/orgs",
            json={"username": org_name, "visibility": "private"},
            headers={"Authorization": f"token {settings.forgejo_admin_token}"},
        )

        # Create repo
        resp = await client.post(
            f"{settings.forgejo_url}/api/v1/orgs/{org_name}/repos",
            json={
                "name": slug,
                "private": True,
                "auto_init": True,   # init with empty commit so clone works immediately
                "default_branch": "main",
            },
            headers={"Authorization": f"token {settings.forgejo_admin_token}"},
        )
        if resp.status_code in (201, 409):  # Created or already exists
            return f"{settings.forgejo_url}/{org_name}/{slug}.git"

    return ""


def _build_vault_response(vault: Vault) -> VaultResponse:
    push_url = _git_push_url(vault.git_repo_url)
    return VaultResponse(
        id=vault.id,
        name=vault.name,
        slug=vault.slug,
        git_repo_url=vault.git_repo_url,
        git_push_url=push_url,
        config=vault.config_json,
    )


@router.get("", response_model=list[VaultResponse])
async def list_vaults(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all vaults for the current user."""
    result = await db.execute(select(Vault).where(Vault.user_id == user.id))
    vaults = result.scalars().all()
    return [_build_vault_response(v) for v in vaults]


@router.post("", response_model=VaultResponse)
async def create_vault(
    body: VaultCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Register a new vault, provision git repo, and spin up K8s dispatcher."""
    # Vault-count enforcement disabled (pre-monetization). See cloud/api/limits.py.
    # We still look up the plan so the K8s provisioner can size resource
    # quotas appropriately for actually-paid users.
    from cloud.models.subscription import Subscription
    sub_result = await db.execute(select(Subscription).where(Subscription.user_id == user.id))
    sub = sub_result.scalar_one_or_none()
    plan = sub.plan if (sub and sub.status == "active") else "free"

    slug = _slugify(body.slug or body.name)

    # Idempotent: if this user already has a vault with this slug, return it.
    # Also re-run K8s provisioning — the original create_task may have failed
    # mid-way (e.g. earlier NameError), leaving DB state without K8s
    # resources. provision_vault uses create_from_dict which is idempotent
    # on AlreadyExists, so re-running is safe and self-heals partial state.
    dup = await db.execute(
        select(Vault).where(Vault.user_id == user.id, Vault.slug == slug)
    )
    existing = dup.scalar_one_or_none()
    if existing:
        from cloud.models.subscription import Subscription
        sub_result = await db.execute(select(Subscription).where(Subscription.user_id == user.id))
        sub = sub_result.scalar_one_or_none()
        plan_for_provision = sub.plan if (sub and sub.status == "active") else "free"
        asyncio.create_task(_provision_k8s(
            user_id=user.id,
            vault_id=existing.id,
            vault_slug=existing.slug,
            git_repo_url=existing.git_repo_url,
            claude_token=body.claude_token or "",
            plan=plan_for_provision,
        ))
        return _build_vault_response(existing)

    # Provision git repo on Forgejo
    git_url = await _provision_git_repo(user.id, slug)

    # Store claude_token in config if provided
    initial_config: dict = {}
    if body.claude_token:
        initial_config["claude_token"] = body.claude_token

    vault = Vault(
        user_id=user.id,
        name=body.name,
        slug=slug,
        git_repo_url=git_url,
        config_json=initial_config,
    )
    db.add(vault)
    await db.commit()
    await db.refresh(vault)

    # Provision K8s namespace + dispatcher (fire and forget — don't block the response)
    asyncio.create_task(_provision_k8s(
        user_id=user.id,
        vault_id=vault.id,
        vault_slug=slug,
        git_repo_url=git_url,
        claude_token=body.claude_token or "",
        plan=plan,
    ))

    return _build_vault_response(vault)


async def _provision_k8s(
    user_id: str, vault_id: str, vault_slug: str,
    git_repo_url: str, claude_token: str, plan: str,
) -> None:
    """Provision K8s resources for a vault in the background."""
    try:
        from cloud.k8s_provisioner import provision_vault
        await provision_vault(
            user_id=user_id,
            vault_id=vault_id,
            vault_slug=vault_slug,
            git_repo_url=git_repo_url,
            claude_token=claude_token,
            plan=plan,
        )
        logger.info(f"K8s provisioning complete for vault {vault_slug}")
    except Exception as e:
        logger.error(f"K8s provisioning failed for vault {vault_slug}: {e}", exc_info=True)


@router.get("/{vault_id}", response_model=VaultResponse)
async def get_vault(
    vault_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get vault details."""
    result = await db.execute(
        select(Vault).where(Vault.id == vault_id, Vault.user_id == user.id)
    )
    vault = result.scalar_one_or_none()
    if not vault:
        raise HTTPException(status_code=404, detail="Vault not found")
    return _build_vault_response(vault)


@router.post("/{vault_id}/activate")
async def activate_vault(
    vault_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Scale dispatcher to 1 replica (wake from cold start)."""
    result = await db.execute(
        select(Vault).where(Vault.id == vault_id, Vault.user_id == user.id)
    )
    vault = result.scalar_one_or_none()
    if not vault:
        raise HTTPException(status_code=404, detail="Vault not found")

    try:
        from kubernetes import client as k8s_client, config as k8s_config
        try:
            k8s_config.load_incluster_config()
        except Exception:
            k8s_config.load_kube_config()

        namespace = f"{settings.k8s_namespace_prefix}{user.id[:8]}"
        apps = k8s_client.AppsV1Api()
        # Wake the dispatcher and the always-needed MCPs (obsidian for vault
        # access, davyjones for memory/file ops). Token-dependent MCPs
        # (gitlab/github/slack) stay at 0 until tokens are configured.
        for dep in (
            f"dispatcher-{vault.slug}",
            f"obsidian-mcp-{vault.slug}",
            f"davyjones-mcp-{vault.slug}",
        ):
            try:
                apps.patch_namespaced_deployment_scale(
                    name=dep,
                    namespace=namespace,
                    body={"spec": {"replicas": 1}},
                )
            except Exception as e:
                logger.warning(f"Could not scale {dep}: {e}")
        return {"status": "activating", "vault": vault.slug}
    except Exception as e:
        logger.warning(f"Could not scale dispatcher: {e}")
        return {"status": "ok", "note": "K8s scale not available"}


@router.put("/{vault_id}/config")
async def update_vault_config(
    vault_id: str,
    body: VaultConfigUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update vault configuration (tokens, rules)."""
    result = await db.execute(
        select(Vault).where(Vault.id == vault_id, Vault.user_id == user.id)
    )
    vault = result.scalar_one_or_none()
    if not vault:
        raise HTTPException(status_code=404, detail="Vault not found")

    # Update non-secret config in DB
    config = dict(vault.config_json)
    if body.vault_rules is not None:
        config["vault_rules"] = body.vault_rules
    if body.gws_config is not None:
        config["gws_config"] = body.gws_config
    if body.claude_token is not None:
        config["claude_token"] = body.claude_token
    vault.config_json = config
    await db.commit()

    # Update K8s Secret + ConfigMap, then poke dispatcher to reconcile.
    try:
        from kubernetes import client as k8s_client, config as k8s_config
        try:
            k8s_config.load_incluster_config()
        except Exception:
            k8s_config.load_kube_config()
        import base64
        namespace = f"{settings.k8s_namespace_prefix}{user.id[:8]}"
        core = k8s_client.CoreV1Api()

        # ── Secret: primary tokens ────────────────────────────────────────
        secret_data = {}
        if body.claude_token:
            secret_data["CLAUDE_CODE_OAUTH_TOKEN"] = base64.b64encode(body.claude_token.encode()).decode()
        if body.github_token:
            secret_data["GITHUB_TOKEN"] = base64.b64encode(body.github_token.encode()).decode()
        if body.gitlab_token:
            secret_data["GITLAB_TOKEN"] = base64.b64encode(body.gitlab_token.encode()).decode()
        if body.slack_bot_token:
            secret_data["SLACK_BOT_TOKEN"] = base64.b64encode(body.slack_bot_token.encode()).decode()
        if body.slack_app_token:
            secret_data["SLACK_APP_TOKEN"] = base64.b64encode(body.slack_app_token.encode()).decode()
        if secret_data:
            core.patch_namespaced_secret(
                name=f"vault-credentials-{vault.slug}",
                namespace=namespace,
                body={"data": secret_data},
            )

        # ── ConfigMap: rules JSON + non-secret env overrides ─────────────
        # Mounted into dispatcher (/vault-config/rules.json) and as env source
        # for gitlab-mcp (GITLAB_API_URL). ConfigMap files live-update inside
        # running pods within ~1 minute, no rollout needed.
        import json as _json
        cm_data = {}
        if body.vault_rules is not None:
            cm_data["rules.json"] = _json.dumps(body.vault_rules)
        if body.gitlab_api_url is not None:
            # Default applied at vault provision time; only overwrite when
            # the plugin sent a non-empty value.
            cm_data["GITLAB_API_URL"] = body.gitlab_api_url or "https://gitlab.com"
        if cm_data:
            core.patch_namespaced_config_map(
                name=f"vault-config-{vault.slug}",
                namespace=namespace,
                body={"data": cm_data},
            )

        # ── Token changes: scale up MCP deployments now that a token is set
        #    (they default to 0 replicas to avoid crash-looping unconfigured),
        #    and force a rollout so env-var refs to the Secret pick up the
        #    new value. ConfigMap-only changes do NOT need a restart.
        if secret_data:
            apps = k8s_client.AppsV1Api()
            from datetime import datetime, timezone
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            # Map secret keys → MCP deployments that consume them
            secret_to_deploys = {
                "GITLAB_TOKEN": [f"gitlab-mcp-{vault.slug}"],
                "GITHUB_TOKEN": [f"github-mcp-{vault.slug}"],
                "SLACK_BOT_TOKEN": [f"slack-mcp-{vault.slug}"],
                # CLAUDE_CODE_OAUTH_TOKEN is consumed by the dispatcher (envFrom),
                # restart it so spawned agents see the new token.
                "CLAUDE_CODE_OAUTH_TOKEN": [f"dispatcher-{vault.slug}"],
            }
            restart_targets = set()
            for key in secret_data:
                for dep in secret_to_deploys.get(key, []):
                    restart_targets.add(dep)
            for dep in restart_targets:
                try:
                    # Scale up to 1 if currently 0 (token-dependent MCPs default
                    # to 0). Idempotent for already-running deployments.
                    apps.patch_namespaced_deployment_scale(
                        name=dep,
                        namespace=namespace,
                        body={"spec": {"replicas": 1}},
                    )
                    apps.patch_namespaced_deployment(
                        name=dep,
                        namespace=namespace,
                        body={"spec": {"template": {"metadata": {"annotations": {
                            "davyjones.io/restartedAt": ts,
                        }}}}},
                    )
                except Exception as e:
                    logger.warning(f"Could not roll {dep}: {e}")

        # ── Poke dispatcher to reload rules in-process (no restart) ──────
        if body.vault_rules is not None:
            import httpx
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    await client.post(
                        f"http://dispatcher-{vault.slug}.{namespace}:5555/api/reconcile",
                    )
            except Exception as e:
                logger.info(f"Dispatcher reconcile poke skipped: {e}")
    except Exception as e:
        logger.warning(f"Could not update K8s state: {e}")

    return {"status": "ok"}


# ── Google Workspace BYOC OAuth flow ──────────────────────────────
# The user supplies their own OAuth client credentials (created in their
# own GCP project, kept in test mode with themselves as the only test user).
# This sidesteps Google's verification process — each user is technically
# the developer of their own app, accessing only their own data.

DEFAULT_GWS_SCOPES = [
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents",
]


class GwsSetup(BaseModel):
    client_id: str
    client_secret: str
    scopes: list[str] | None = None


@router.post("/{vault_id}/gws/setup")
async def gws_setup(
    vault_id: str,
    body: GwsSetup,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Step 1 of GWS OAuth: store the user's OAuth client creds and return
    the Google authorize URL (which the plugin opens in a browser)."""
    result = await db.execute(
        select(Vault).where(Vault.id == vault_id, Vault.user_id == user.id)
    )
    vault = result.scalar_one_or_none()
    if not vault:
        raise HTTPException(status_code=404, detail="Vault not found")

    # Persist the user's OAuth client creds in the vault Secret. They're
    # needed at the callback step (to exchange the code for a refresh token)
    # and also at agent-runtime (gws CLI uses them to refresh access tokens).
    try:
        from kubernetes import client as k8s_client, config as k8s_config
        try:
            k8s_config.load_incluster_config()
        except Exception:
            k8s_config.load_kube_config()
        import base64
        namespace = f"{settings.k8s_namespace_prefix}{user.id[:8]}"
        core = k8s_client.CoreV1Api()
        core.patch_namespaced_secret(
            name=f"vault-credentials-{vault.slug}",
            namespace=namespace,
            body={"data": {
                "GWS_CLIENT_ID": base64.b64encode(body.client_id.encode()).decode(),
                "GWS_CLIENT_SECRET": base64.b64encode(body.client_secret.encode()).decode(),
            }},
        )
    except Exception as e:
        logger.warning(f"Could not store GWS client creds in Secret: {e}")
        raise HTTPException(status_code=500, detail="Failed to persist credentials")

    # Build a signed state token so the callback can identify the vault
    # without keeping any server-side state. JWT signed with our existing
    # secret is plenty here — short-lived (10 min), tamper-proof.
    from jose import jwt as _jwt
    import time
    state_payload = {"vault_id": vault.id, "exp": int(time.time()) + 600}
    state = _jwt.encode(state_payload, settings.jwt_secret, algorithm="HS256")

    scopes = body.scopes or DEFAULT_GWS_SCOPES
    redirect_uri = f"{settings.api_url}/api/v1/auth/gws/callback"

    from urllib.parse import quote
    auth_url = (
        f"https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={quote(body.client_id, safe='')}"
        f"&response_type=code"
        f"&scope={'+'.join(quote(s, safe='') for s in scopes)}"
        f"&redirect_uri={quote(redirect_uri, safe='')}"
        f"&state={state}"
        f"&access_type=offline"
        f"&prompt=consent"
    )
    return {"authorize_url": auth_url}


@router.get("/{vault_id}/gws/status")
async def gws_status(
    vault_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Plugin polls this after kicking off the OAuth flow.
    Returns connected=true once the callback has stored a refresh token."""
    result = await db.execute(
        select(Vault).where(Vault.id == vault_id, Vault.user_id == user.id)
    )
    vault = result.scalar_one_or_none()
    if not vault:
        raise HTTPException(status_code=404, detail="Vault not found")

    try:
        from kubernetes import client as k8s_client, config as k8s_config
        try:
            k8s_config.load_incluster_config()
        except Exception:
            k8s_config.load_kube_config()
        namespace = f"{settings.k8s_namespace_prefix}{user.id[:8]}"
        core = k8s_client.CoreV1Api()
        sec = core.read_namespaced_secret(
            name=f"vault-credentials-{vault.slug}",
            namespace=namespace,
        )
        data = sec.data or {}
        connected = bool(data.get("GWS_CREDENTIALS_JSON"))
        # Extract the email from the stored credentials.json if present
        email = None
        if connected:
            import base64, json as _json
            try:
                creds = _json.loads(base64.b64decode(data["GWS_CREDENTIALS_JSON"]).decode())
                email = creds.get("email")
            except Exception:
                pass
        return {"connected": connected, "email": email}
    except Exception as e:
        logger.warning(f"Could not read gws status: {e}")
        return {"connected": False}


@router.post("/{vault_id}/gws/disconnect")
async def gws_disconnect(
    vault_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Revoke locally — clear the stored credentials. Doesn't revoke at Google."""
    result = await db.execute(
        select(Vault).where(Vault.id == vault_id, Vault.user_id == user.id)
    )
    vault = result.scalar_one_or_none()
    if not vault:
        raise HTTPException(status_code=404, detail="Vault not found")

    try:
        from kubernetes import client as k8s_client, config as k8s_config
        try:
            k8s_config.load_incluster_config()
        except Exception:
            k8s_config.load_kube_config()
        import base64
        namespace = f"{settings.k8s_namespace_prefix}{user.id[:8]}"
        core = k8s_client.CoreV1Api()
        core.patch_namespaced_secret(
            name=f"vault-credentials-{vault.slug}",
            namespace=namespace,
            body={"data": {
                "GWS_CLIENT_ID": base64.b64encode(b"").decode(),
                "GWS_CLIENT_SECRET": base64.b64encode(b"").decode(),
                "GWS_CREDENTIALS_JSON": base64.b64encode(b"").decode(),
            }},
        )
    except Exception as e:
        logger.warning(f"Could not clear gws creds: {e}")

    return {"status": "ok"}


@router.delete("/{vault_id}")
async def delete_vault(
    vault_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a vault and its cloud resources."""
    result = await db.execute(
        select(Vault).where(Vault.id == vault_id, Vault.user_id == user.id)
    )
    vault = result.scalar_one_or_none()
    if not vault:
        raise HTTPException(status_code=404, detail="Vault not found")

    slug = vault.slug
    user_id = vault.user_id

    await db.delete(vault)
    await db.commit()

    # Deprovision K8s resources in background
    asyncio.create_task(_deprovision_k8s(user_id, slug))

    return {"status": "deleted"}


async def _deprovision_k8s(user_id: str, vault_slug: str) -> None:
    try:
        from cloud.k8s_provisioner import deprovision_vault
        await deprovision_vault(user_id=user_id, vault_slug=vault_slug)
    except Exception as e:
        logger.error(f"K8s deprovisioning failed for vault {vault_slug}: {e}", exc_info=True)
