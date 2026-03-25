"""Billing — Stripe subscription management."""

import logging

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cloud.api.auth import get_current_user
from cloud.config import settings
from cloud.db import get_db
from cloud.models.user import User
from cloud.models.subscription import Subscription

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])

stripe.api_key = settings.stripe_secret_key


@router.post("/checkout")
async def create_checkout(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a Stripe Checkout session for upgrading to Pro."""
    if not settings.stripe_secret_key:
        raise HTTPException(status_code=503, detail="Billing not configured")

    # Find or create Stripe customer
    sub = user.subscription
    if not sub:
        sub = Subscription(user_id=user.id, plan="free", status="active")
        db.add(sub)
        await db.commit()
        await db.refresh(sub)

    if not sub.stripe_customer_id:
        customer = stripe.Customer.create(
            email=user.email,
            metadata={"user_id": user.id},
        )
        sub.stripe_customer_id = customer.id
        await db.commit()

    session = stripe.checkout.Session.create(
        customer=sub.stripe_customer_id,
        mode="subscription",
        line_items=[{"price": settings.stripe_price_pro, "quantity": 1}],
        success_url=f"{settings.api_url}/billing/success",
        cancel_url=f"{settings.api_url}/billing/cancel",
    )
    return {"checkout_url": session.url}


@router.get("/portal")
async def customer_portal(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get a Stripe Customer Portal link for managing subscription."""
    sub = user.subscription
    if not sub or not sub.stripe_customer_id:
        raise HTTPException(status_code=400, detail="No active subscription")

    portal = stripe.billing_portal.Session.create(
        customer=sub.stripe_customer_id,
        return_url=settings.api_url,
    )
    return {"portal_url": portal.url}


@router.post("/webhook")
async def stripe_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """Handle Stripe webhook events."""
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig, settings.stripe_webhook_secret,
        )
    except (ValueError, stripe.error.SignatureVerificationError):
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    event_type = event["type"]
    data = event["data"]["object"]

    if event_type == "customer.subscription.created":
        await _update_subscription(db, data, "active")
    elif event_type == "customer.subscription.updated":
        status = "active" if data["status"] == "active" else data["status"]
        await _update_subscription(db, data, status)
    elif event_type == "customer.subscription.deleted":
        await _update_subscription(db, data, "canceled")
    elif event_type == "invoice.payment_failed":
        customer_id = data.get("customer")
        if customer_id:
            result = await db.execute(
                select(Subscription).where(
                    Subscription.stripe_customer_id == customer_id
                )
            )
            sub = result.scalar_one_or_none()
            if sub:
                sub.status = "past_due"
                await db.commit()

    return {"received": True}


async def _update_subscription(db: AsyncSession, data: dict, status: str):
    """Update subscription from Stripe webhook data."""
    customer_id = data.get("customer")
    if not customer_id:
        return

    result = await db.execute(
        select(Subscription).where(Subscription.stripe_customer_id == customer_id)
    )
    sub = result.scalar_one_or_none()
    if not sub:
        return

    # Determine plan from price ID
    items = data.get("items", {}).get("data", [])
    if items:
        price_id = items[0].get("price", {}).get("id", "")
        if price_id == settings.stripe_price_pro:
            sub.plan = "pro"
        elif price_id == settings.stripe_price_team:
            sub.plan = "team"

    sub.status = status
    if data.get("current_period_end"):
        from datetime import datetime, timezone
        sub.current_period_end = datetime.fromtimestamp(
            data["current_period_end"], tz=timezone.utc
        )
    await db.commit()
