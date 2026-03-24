"""aiohttp web server for Blockradar payment webhooks."""

import hashlib
import hmac
import json
import logging
from aiohttp import web
from config import BLOCKRADAR_API_KEY
from billing import confirm_payment, get_payment_by_ref

logger = logging.getLogger(__name__)

# Reference to the Telegram bot Application (set at startup)
_bot_app = None


def set_bot_app(app):
    global _bot_app
    _bot_app = app


def _verify_signature(payload: bytes, signature: str) -> bool:
    """Verify HMAC-SHA512 signature from Blockradar."""
    if not signature:
        return False
    expected = hmac.new(
        BLOCKRADAR_API_KEY.encode(),
        payload,
        hashlib.sha512,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


async def handle_blockradar_webhook(request: web.Request) -> web.Response:
    """POST /webhook/blockradar — handle payment confirmations."""
    body = await request.read()
    signature = request.headers.get("x-blockradar-signature", "")

    if not _verify_signature(body, signature):
        logger.warning("Invalid Blockradar webhook signature")
        return web.Response(status=401, text="invalid signature")

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return web.Response(status=400, text="invalid json")

    # Check event type — accept various success indicators
    event = data.get("event", "")
    status = data.get("data", {}).get("status", data.get("status", ""))

    if event not in ("deposit.success", "payment.completed", "") and \
       str(status).lower() not in ("success", "completed"):
        logger.info(f"Ignoring webhook event: {event} status: {status}")
        return web.Response(status=200, text="ignored")

    # Extract reference from metadata or data
    inner = data.get("data", data)
    metadata = inner.get("metadata", {})
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            metadata = {}

    reference = inner.get("reference") or inner.get("id")
    user_id_str = metadata.get("telegram_user_id")

    if not reference:
        logger.warning(f"Webhook missing reference: {data}")
        return web.Response(status=400, text="missing reference")

    # Idempotency check
    existing = get_payment_by_ref(str(reference))
    if existing and existing["status"] == "confirmed":
        return web.Response(status=200, text="already processed")

    # Confirm payment + add credits
    payment = confirm_payment(str(reference))
    if not payment:
        logger.error(f"Payment not found for ref: {reference}")
        return web.Response(status=404, text="payment not found")

    logger.info(f"Payment confirmed: ref={reference} user={payment['user_id']}")

    # Notify user via Telegram
    if _bot_app and payment.get("user_id"):
        try:
            credits = payment.get("credits_added", 0)
            ptype = payment.get("payment_type", "credits")
            if ptype == "subscription":
                msg = "Payment confirmed! Your monthly subscription is now active (15 credits).\nUse /balance to check."
            else:
                msg = f"Payment confirmed! {credits} credit(s) added.\nUse /balance to check."
            await _bot_app.bot.send_message(payment["user_id"], msg)
        except Exception:
            logger.warning(f"Could not notify user {payment['user_id']}")

    return web.Response(status=200, text="ok")


async def health_check(request: web.Request) -> web.Response:
    return web.Response(text="ok")


def create_web_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/webhook/blockradar", handle_blockradar_webhook)
    app.router.add_get("/health", health_check)
    return app
