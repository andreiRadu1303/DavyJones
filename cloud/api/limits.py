"""Subscription tier enforcement — checks limits before allowing actions."""

from __future__ import annotations

import logging
import time

from fastapi import Depends, HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from cloud.api.auth import get_current_user
from cloud.db import get_db
from cloud.models.user import User
from cloud.models.vault import Vault
from cloud.models.subscription import Subscription

logger = logging.getLogger(__name__)

# ── Tier definitions ──────────────────────────────────────────────

TIER_LIMITS = {
    "free": {
        "max_vaults": 1,
        "max_tasks_per_month": 50,
        "max_concurrent_agents": 1,
        "warm_pods": False,
        "report_retention_days": 7,
    },
    "pro": {
        "max_vaults": 5,
        "max_tasks_per_month": -1,  # unlimited
        "max_concurrent_agents": 3,
        "warm_pods": True,
        "report_retention_days": 90,
    },
    "team": {
        "max_vaults": 20,
        "max_tasks_per_month": -1,
        "max_concurrent_agents": 5,
        "warm_pods": True,
        "report_retention_days": 365,
    },
}


def get_limits(plan: str) -> dict:
    """Get tier limits for a plan."""
    return TIER_LIMITS.get(plan, TIER_LIMITS["free"])


# ── Redis-based usage counters ────────────────────────────────────

_redis = None


def _get_redis():
    global _redis
    if _redis is None:
        import os
        import redis as redis_lib
        url = os.environ.get("DAVYJONES_REDIS_URL", "redis://localhost:6379/0")
        _redis = redis_lib.from_url(url, decode_responses=True)
    return _redis


def _month_key(user_id: str) -> str:
    """Redis key for monthly task counter, e.g. usage:tasks:{user_id}:2026-03"""
    month = time.strftime("%Y-%m")
    return f"usage:tasks:{user_id}:{month}"


def increment_task_count(user_id: str) -> int:
    """Increment and return the monthly task count for a user."""
    r = _get_redis()
    key = _month_key(user_id)
    count = r.incr(key)
    # Set TTL to 35 days if this is the first increment
    if count == 1:
        r.expire(key, 86400 * 35)
    return count


def get_task_count(user_id: str) -> int:
    """Get the current monthly task count."""
    r = _get_redis()
    val = r.get(_month_key(user_id))
    return int(val) if val else 0


# ── FastAPI dependencies ──────────────────────────────────────────

async def check_task_limit(
    user: User = Depends(get_current_user),
):
    """FastAPI dependency — raises 403 if user has exceeded monthly task limit."""
    plan = "free"
    if user.subscription and user.subscription.status == "active":
        plan = user.subscription.plan

    limits = get_limits(plan)
    max_tasks = limits["max_tasks_per_month"]

    if max_tasks == -1:
        return user  # unlimited

    current = get_task_count(user.id)
    if current >= max_tasks:
        raise HTTPException(
            status_code=403,
            detail=f"Monthly task limit reached ({max_tasks} tasks on {plan} plan). "
                   f"Upgrade to Pro for unlimited tasks.",
        )

    return user


async def check_vault_limit(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """FastAPI dependency — raises 403 if user has reached vault limit."""
    plan = "free"
    if user.subscription and user.subscription.status == "active":
        plan = user.subscription.plan

    limits = get_limits(plan)

    result = await db.execute(
        select(func.count()).where(Vault.user_id == user.id)
    )
    vault_count = result.scalar() or 0

    if vault_count >= limits["max_vaults"]:
        raise HTTPException(
            status_code=403,
            detail=f"Vault limit reached ({limits['max_vaults']} on {plan} plan). "
                   f"Upgrade for more vaults.",
        )

    return user
