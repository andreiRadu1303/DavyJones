"""Account info — user profile, plan, usage stats."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from cloud.api.auth import get_current_user
from cloud.api.limits import get_limits, get_task_count
from cloud.db import get_db
from cloud.models.user import User
from cloud.models.vault import Vault

router = APIRouter(prefix="/account", tags=["account"])


class AccountInfo(BaseModel):
    user_id: str
    email: str
    name: str
    plan: str
    plan_status: str
    limits: dict
    usage: dict


@router.get("", response_model=AccountInfo)
async def get_account(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get current user account info, plan, and usage."""
    plan = "free"
    plan_status = "active"
    if user.subscription:
        plan = user.subscription.plan
        plan_status = user.subscription.status

    limits = get_limits(plan)

    # Count vaults
    result = await db.execute(
        select(func.count()).where(Vault.user_id == user.id)
    )
    vault_count = result.scalar() or 0

    # Get monthly task usage
    task_count = get_task_count(user.id)

    return AccountInfo(
        user_id=user.id,
        email=user.email,
        name=user.name,
        plan=plan,
        plan_status=plan_status,
        limits=limits,
        usage={
            "vaults": vault_count,
            "tasks_this_month": task_count,
        },
    )
