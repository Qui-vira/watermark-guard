"""User billing, credits, and trial management."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from database import get_client
from config import TRIAL_DAYS, SUBSCRIPTION_CREDITS

logger = logging.getLogger(__name__)


def get_or_create_user(user_id: int) -> dict:
    client = get_client()
    result = client.table("wg_users").upsert(
        {"user_id": user_id}, on_conflict="user_id"
    ).execute()
    return result.data[0] if result.data else {"user_id": user_id, "credits": 0}


def get_user(user_id: int) -> Optional[dict]:
    client = get_client()
    result = client.table("wg_users").select("*").eq("user_id", user_id).execute()
    return result.data[0] if result.data else None


def update_user(user_id: int, updates: dict) -> dict:
    client = get_client()
    result = client.table("wg_users").update(updates).eq("user_id", user_id).execute()
    return result.data[0] if result.data else {}


def start_trial(user_id: int) -> dict:
    return update_user(user_id, {
        "trial_start": datetime.now(timezone.utc).isoformat(),
    })


def is_trial_active(user: dict) -> bool:
    trial_start = user.get("trial_start")
    if not trial_start:
        return False
    if isinstance(trial_start, str):
        trial_start = datetime.fromisoformat(trial_start.replace("Z", "+00:00"))
    expiry = trial_start + timedelta(days=TRIAL_DAYS)
    return datetime.now(timezone.utc) < expiry


def trial_expires_at(user: dict) -> Optional[datetime]:
    trial_start = user.get("trial_start")
    if not trial_start:
        return None
    if isinstance(trial_start, str):
        trial_start = datetime.fromisoformat(trial_start.replace("Z", "+00:00"))
    return trial_start + timedelta(days=TRIAL_DAYS)


def has_active_subscription(user: dict) -> bool:
    sub_exp = user.get("subscription_expires")
    if not sub_exp:
        return False
    if isinstance(sub_exp, str):
        sub_exp = datetime.fromisoformat(sub_exp.replace("Z", "+00:00"))
    return datetime.now(timezone.utc) < sub_exp


def can_generate_ai_bg(user: dict) -> tuple[bool, str]:
    """Check if user can generate an AI background. Returns (allowed, reason)."""
    if is_trial_active(user):
        exp = trial_expires_at(user)
        return True, f"Trial active (expires {exp.strftime('%b %d')})"

    if has_active_subscription(user):
        used = user.get("monthly_credits_used", 0)
        if used < SUBSCRIPTION_CREDITS:
            remaining = SUBSCRIPTION_CREDITS - used
            return True, f"Subscription: {remaining}/{SUBSCRIPTION_CREDITS} credits left"
        return False, "Monthly credits used up. Buy extra credits or wait for renewal."

    credits = user.get("credits", 0)
    if credits > 0:
        return True, f"{credits} credit(s) available"

    return False, "No credits. Use /buy to purchase."


def deduct_credit(user_id: int) -> bool:
    """Deduct 1 credit. For subscribers, increments monthly_credits_used instead."""
    user = get_user(user_id)
    if not user:
        return False

    if has_active_subscription(user):
        used = user.get("monthly_credits_used", 0)
        if used < SUBSCRIPTION_CREDITS:
            update_user(user_id, {"monthly_credits_used": used + 1})
            return True
        return False

    credits = user.get("credits", 0)
    if credits <= 0:
        return False
    update_user(user_id, {"credits": credits - 1})
    return True


def add_credits(user_id: int, amount: int) -> dict:
    user = get_or_create_user(user_id)
    new_credits = user.get("credits", 0) + amount
    return update_user(user_id, {"credits": new_credits})


def activate_subscription(user_id: int) -> dict:
    """Activate or renew monthly subscription."""
    expires = datetime.now(timezone.utc) + timedelta(days=30)
    return update_user(user_id, {
        "subscription_expires": expires.isoformat(),
        "monthly_credits_used": 0,
    })


def create_payment(user_id: int, amount: float, credits: int,
                   payment_type: str, blockradar_ref: str) -> dict:
    # Ensure user exists (FK constraint)
    get_or_create_user(user_id)
    client = get_client()
    data = {
        "user_id": user_id,
        "amount": amount,
        "credits_added": credits,
        "payment_type": payment_type,
        "blockradar_ref": blockradar_ref,
        "status": "pending",
    }
    result = client.table("wg_payments").insert(data).execute()
    return result.data[0] if result.data else {}


def get_payment_by_ref(blockradar_ref: str) -> Optional[dict]:
    client = get_client()
    result = (
        client.table("wg_payments")
        .select("*")
        .eq("blockradar_ref", blockradar_ref)
        .execute()
    )
    return result.data[0] if result.data else None


def confirm_payment(blockradar_ref: str) -> Optional[dict]:
    """Mark payment confirmed and add credits/subscription to user."""
    payment = get_payment_by_ref(blockradar_ref)
    if not payment or payment["status"] == "confirmed":
        return payment

    client = get_client()
    client.table("wg_payments").update(
        {"status": "confirmed"}
    ).eq("blockradar_ref", blockradar_ref).execute()

    user_id = payment["user_id"]
    if payment["payment_type"] == "subscription":
        activate_subscription(user_id)
    else:
        add_credits(user_id, payment["credits_added"])

    payment["status"] = "confirmed"
    return payment


def get_expiring_trials() -> list[dict]:
    """Get users whose trial expires within 24 hours."""
    client = get_client()
    now = datetime.now(timezone.utc)
    cutoff_start = now
    cutoff_end = now + timedelta(hours=24)
    # trial_start + TRIAL_DAYS between now and now+24h
    trial_expiry_start = (cutoff_start - timedelta(days=TRIAL_DAYS)).isoformat()
    trial_expiry_end = (cutoff_end - timedelta(days=TRIAL_DAYS)).isoformat()
    result = (
        client.table("wg_users")
        .select("*")
        .gte("trial_start", trial_expiry_start)
        .lte("trial_start", trial_expiry_end)
        .execute()
    )
    return result.data if result.data else []


def get_low_credit_users() -> list[dict]:
    """Get users with 1-2 credits remaining (no active trial/sub)."""
    client = get_client()
    now = datetime.now(timezone.utc).isoformat()
    result = (
        client.table("wg_users")
        .select("*")
        .lte("credits", 2)
        .gt("credits", 0)
        .execute()
    )
    # Filter out those with active trial or subscription
    users = result.data if result.data else []
    return [u for u in users if not is_trial_active(u) and not has_active_subscription(u)]


def get_expiring_subscriptions() -> list[dict]:
    """Get users whose subscription expires within 3 days."""
    client = get_client()
    now = datetime.now(timezone.utc)
    cutoff = (now + timedelta(days=3)).isoformat()
    result = (
        client.table("wg_users")
        .select("*")
        .gte("subscription_expires", now.isoformat())
        .lte("subscription_expires", cutoff)
        .execute()
    )
    return result.data if result.data else []
