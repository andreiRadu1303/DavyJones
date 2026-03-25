"""Vault management — register, list, activate, configure."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cloud.api.auth import get_current_user
from cloud.config import settings
from cloud.db import get_db
from cloud.models.user import User
from cloud.models.vault import Vault

router = APIRouter(prefix="/vaults", tags=["vaults"])


class VaultCreate(BaseModel):
    name: str


class VaultResponse(BaseModel):
    id: str
    name: str
    slug: str
    git_repo_url: str
    config: dict

    class Config:
        from_attributes = True


class VaultConfigUpdate(BaseModel):
    claude_token: str | None = None
    github_token: str | None = None
    gitlab_token: str | None = None
    slack_bot_token: str | None = None
    slack_app_token: str | None = None
    gws_config: dict | None = None
    vault_rules: dict | None = None


def _slugify(name: str) -> str:
    """Convert vault name to a safe slug."""
    return name.lower().replace(" ", "-").strip("-")[:60]


async def _provision_git_repo(user_id: str, slug: str) -> str:
    """Create a bare git repo on Forgejo for this vault. Returns clone URL."""
    import httpx

    if not settings.forgejo_admin_token:
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
                "auto_init": False,
            },
            headers={"Authorization": f"token {settings.forgejo_admin_token}"},
        )
        if resp.status_code in (201, 409):  # Created or already exists
            return f"{settings.forgejo_url}/{org_name}/{slug}.git"

    return ""


@router.get("", response_model=list[VaultResponse])
async def list_vaults(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all vaults for the current user."""
    result = await db.execute(select(Vault).where(Vault.user_id == user.id))
    vaults = result.scalars().all()
    return [
        VaultResponse(
            id=v.id, name=v.name, slug=v.slug,
            git_repo_url=v.git_repo_url, config=v.config_json,
        )
        for v in vaults
    ]


@router.post("", response_model=VaultResponse)
async def create_vault(
    body: VaultCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Register a new vault."""
    # Check vault limit based on subscription
    result = await db.execute(select(Vault).where(Vault.user_id == user.id))
    existing = result.scalars().all()
    plan = user.subscription.plan if user.subscription else "free"
    limits = {"free": 1, "pro": 5, "team": 20}
    if len(existing) >= limits.get(plan, 1):
        raise HTTPException(status_code=403, detail=f"Vault limit reached for {plan} plan")

    slug = _slugify(body.name)

    # Check slug uniqueness for this user
    dup = await db.execute(
        select(Vault).where(Vault.user_id == user.id, Vault.slug == slug)
    )
    if dup.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"Vault '{slug}' already exists")

    # Provision git repo
    git_url = await _provision_git_repo(user.id, slug)

    vault = Vault(
        user_id=user.id,
        name=body.name,
        slug=slug,
        git_repo_url=git_url,
    )
    db.add(vault)
    await db.commit()
    await db.refresh(vault)

    return VaultResponse(
        id=vault.id, name=vault.name, slug=vault.slug,
        git_repo_url=vault.git_repo_url, config=vault.config_json,
    )


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
    return VaultResponse(
        id=vault.id, name=vault.name, slug=vault.slug,
        git_repo_url=vault.git_repo_url, config=vault.config_json,
    )


@router.put("/{vault_id}/config")
async def update_vault_config(
    vault_id: str,
    body: VaultConfigUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update vault configuration (tokens, rules). Secrets go to K8s Secrets."""
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
    vault.config_json = config
    await db.commit()

    # TODO: Update K8s Secrets for sensitive tokens (claude_token, github_token, etc.)
    # This will be implemented when K8s provisioning is wired up.

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

    # TODO: Tear down K8s namespace, delete git repo, delete PVCs

    await db.delete(vault)
    await db.commit()
    return {"status": "deleted"}
