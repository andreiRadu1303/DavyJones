"""Task proxy — routes task submissions to the correct vault's dispatcher pod."""

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cloud.api.auth import get_current_user
from cloud.config import settings
from cloud.db import get_db
from cloud.models.user import User
from cloud.models.vault import Vault

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/vaults/{vault_id}", tags=["tasks"])


def _dispatcher_url(user_id: str, vault_slug: str) -> str:
    """Build the internal URL for a vault's dispatcher pod.

    In K8s, the dispatcher Service is:
      dispatcher.dj-{user_id_prefix}.svc.cluster.local:5555
    """
    namespace = f"{settings.k8s_namespace_prefix}{user_id[:8]}"
    return f"http://dispatcher-{vault_slug}.{namespace}:5555"


async def _get_user_vault(
    vault_id: str, user: User, db: AsyncSession,
) -> Vault:
    result = await db.execute(
        select(Vault).where(Vault.id == vault_id, Vault.user_id == user.id)
    )
    vault = result.scalar_one_or_none()
    if not vault:
        raise HTTPException(status_code=404, detail="Vault not found")
    return vault


class TaskSubmit(BaseModel):
    description: str
    scopeFiles: list[str] = []


@router.post("/tasks")
async def submit_task(
    vault_id: str,
    body: TaskSubmit,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Submit a task to a vault's dispatcher."""
    vault = await _get_user_vault(vault_id, user, db)
    url = _dispatcher_url(user.id, vault.slug)

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{url}/api/task",
                json={"description": body.description, "scopeFiles": body.scopeFiles},
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.ConnectError:
        raise HTTPException(
            status_code=503,
            detail="Dispatcher not running. Vault may need to be activated.",
        )
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)


@router.get("/tasks/active")
async def get_active_tasks(
    vault_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Proxy active tasks from the vault's dispatcher."""
    vault = await _get_user_vault(vault_id, user, db)
    url = _dispatcher_url(user.id, vault.slug)

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{url}/api/tasks/active")
            resp.raise_for_status()
            return resp.json()
    except (httpx.ConnectError, httpx.ReadTimeout):
        return []


@router.get("/tasks/{task_id}")
async def get_task(
    vault_id: str,
    task_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Proxy task details from the vault's dispatcher."""
    vault = await _get_user_vault(vault_id, user, db)
    url = _dispatcher_url(user.id, vault.slug)

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{url}/api/task/{task_id}")
            resp.raise_for_status()
            return resp.json()
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="Dispatcher not running")


@router.get("/reports")
async def get_reports(
    vault_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Proxy reports from the vault's dispatcher."""
    vault = await _get_user_vault(vault_id, user, db)
    url = _dispatcher_url(user.id, vault.slug)

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{url}/api/reports")
            resp.raise_for_status()
            return resp.json()
    except (httpx.ConnectError, httpx.ReadTimeout):
        return []


@router.get("/health")
async def vault_health(
    vault_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Check if a vault's dispatcher is running (replaces file-based heartbeat)."""
    vault = await _get_user_vault(vault_id, user, db)
    url = _dispatcher_url(user.id, vault.slug)

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{url}/api/health")
            resp.raise_for_status()
            return {"active": True, "dispatcher": url, **resp.json()}
    except (httpx.ConnectError, httpx.ReadTimeout):
        return {"active": False, "dispatcher": url}
