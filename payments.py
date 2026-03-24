"""Blockradar payment integration."""

import json
import logging
import requests
from config import BLOCKRADAR_API_KEY, BLOCKRADAR_WALLET_ID

logger = logging.getLogger(__name__)

BLOCKRADAR_BASE = "https://api.blockradar.co/v1"


def create_payment_link(amount_usd: float, user_id: int, payment_type: str) -> dict:
    """Create a Blockradar payment link.

    Returns {"url": str, "reference": str} or raises Exception.
    """
    if payment_type == "subscription":
        name = "WatermarkGuard Pro — Monthly"
        desc = "15 AI background credits per month"
    else:
        credits = max(1, round(amount_usd / 0.60))
        name = f"WatermarkGuard — {credits} Credit(s)"
        desc = f"{credits} AI background generation credit(s)"

    headers = {"x-api-key": BLOCKRADAR_API_KEY}

    # Blockradar uses multipart/form-data
    form_data = {
        "name": name,
        "amount": str(amount_usd),
        "currency": "USDC",
        "description": desc,
        "metadata": json.dumps({
            "telegram_user_id": str(user_id),
            "payment_type": payment_type,
        }),
    }

    resp = requests.post(
        f"{BLOCKRADAR_BASE}/payment_links",
        data=form_data,
        headers=headers,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    # Handle multiple possible response structures
    link_data = data.get("data", data)
    if isinstance(link_data, dict) and "data" in link_data:
        link_data = link_data["data"]

    url = (
        link_data.get("url")
        or link_data.get("payment_link")
        or link_data.get("paymentLink")
    )
    reference = (
        link_data.get("id")
        or link_data.get("reference")
        or link_data.get("slug")
    )

    if not url:
        logger.error(f"Blockradar response missing URL: {data}")
        raise ValueError("Payment link URL not found in response")

    logger.info(f"Payment link created: {reference}")
    return {"url": url, "reference": str(reference)}
