"""WatermarkGuard — Telegram bot for auto-watermarking group images."""

import asyncio
import io
import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatMemberUpdated,
    ChatMember,
)
from telegram.ext import (
    Application,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    filters,
)
from telegram.error import BadRequest, Forbidden

from config import (
    TELEGRAM_BOT_TOKEN,
    APPROVAL_TIMEOUT_SECONDS,
    RATE_LIMIT_MAX,
    RATE_LIMIT_WINDOW,
    PORT,
    CREDIT_PRICE_USD,
    SUBSCRIPTION_PRICE_USD,
    SUBSCRIPTION_CREDITS,
)
from database import (
    upsert_group,
    get_group,
    update_group,
    create_pending_image,
    get_pending_image,
    update_pending_image,
    get_expired_pending_images,
)
from watermark import apply_watermark, generate_sample_image
from setup_flow import build_setup_handler
from billing import (
    get_or_create_user,
    is_trial_active,
    has_active_subscription,
    can_generate_ai_bg,
    trial_expires_at,
    create_payment,
    get_expiring_trials,
    get_low_credit_users,
    get_expiring_subscriptions,
)
from payments import create_payment_link
from webhook_server import create_web_app, set_bot_app

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Rate limiter: {chat_id: [timestamps]}
_rate_buckets: dict[int, list[float]] = defaultdict(list)

# Local queue for images when Supabase is down: list of dicts
_local_queue: list[dict] = []

# Track media groups to batch-process
# {media_group_id: {"items": [...], "timer_task": asyncio.Task}}
_media_groups: dict[str, dict] = {}


# ── Helpers ──


def _check_rate_limit(chat_id: int) -> bool:
    now = time.time()
    bucket = _rate_buckets[chat_id]
    # Prune old entries
    _rate_buckets[chat_id] = [t for t in bucket if now - t < RATE_LIMIT_WINDOW]
    if len(_rate_buckets[chat_id]) >= RATE_LIMIT_MAX:
        return False
    _rate_buckets[chat_id].append(now)
    return True


async def _is_admin(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in (ChatMember.ADMINISTRATOR, ChatMember.OWNER)
    except Exception:
        return False


async def _can_delete(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        me = await context.bot.get_chat_member(chat_id, context.bot.id)
        return me.can_delete_messages or me.status == ChatMember.ADMINISTRATOR
    except Exception:
        return False


def _format_config(group: dict) -> str:
    wm_type = group.get("watermark_type", "not set")
    lines = [
        f"*Group:* {group.get('title', 'Unknown')}",
        f"*Type:* {wm_type}",
    ]
    if wm_type == "template":
        lines += [
            f"*Brand:* {group.get('brand_name') or '—'}",
            f"*Accent:* {group.get('accent_color') or '#00CCFF'}",
            f"*Stars:* {'★' * (group.get('star_rating') or 0)}",
            f"*Tagline:* {group.get('template_tagline') or '—'}",
            f"*WhatsApp:* {group.get('contact_whatsapp') or '—'}",
            f"*Telegram:* {group.get('contact_telegram') or '—'}",
            f"*Instagram:* {group.get('contact_instagram') or '—'}",
            f"*LinkedIn:* {group.get('contact_linkedin') or '—'}",
            f"*Logo:* {'Uploaded' if group.get('logo_path') else '—'}",
        ]
    else:
        lines += [
            f"*Text:* {group.get('watermark_text') or '—'}",
            f"*URL:* {group.get('watermark_url') or '—'}",
            f"*Use channel name:* {'Yes' if group.get('use_channel_name') else 'No'}",
            f"*Position:* {group.get('watermark_position', 'bottom-right')}",
            f"*Rotation:* {group.get('watermark_rotation', 0)}°",
            f"*Logo:* {'Uploaded' if group.get('logo_path') else '—'}",
        ]
    lines.append(f"*Active:* {'Yes' if group.get('is_active', True) else 'No'}")
    return "\n".join(lines)


# ── Bot added to group ──


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    result: ChatMemberUpdated = update.my_chat_member
    if result is None:
        return

    new = result.new_chat_member
    chat = result.chat

    # Bot was added or promoted
    if new.status in (ChatMember.MEMBER, ChatMember.ADMINISTRATOR):
        upsert_group(chat.id, chat.title or "Untitled")
        try:
            await context.bot.send_message(
                chat.id,
                "WatermarkGuard is active! Admin, DM me and send /setup to configure your watermark.",
            )
        except Exception:
            logger.warning(f"Could not send welcome to {chat.id}")

    # Bot was removed
    elif new.status in (ChatMember.LEFT, ChatMember.BANNED):
        logger.info(f"Removed from group {chat.id}")


# ── DM commands ──


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private":
        return
    await update.message.reply_text(
        "Welcome to *WatermarkGuard*!\n\n"
        "I automatically watermark images posted in groups where I'm an admin.\n\n"
        "*How to get started:*\n"
        "1. Add me to your group/channel\n"
        "2. Make me an admin with 'Delete Messages' permission\n"
        "3. DM me /setup to configure your watermark\n\n"
        "*Commands:*\n"
        "/setup — Configure watermark\n"
        "/settings — View/edit current settings\n"
        "/preview — Preview watermark on a sample image\n"
        "/buy — Purchase AI credits\n"
        "/balance — Check credits & subscription\n"
        "/subscribe — Monthly subscription\n"
        "/help — Show help",
        parse_mode="Markdown",
    )


async def _get_admin_groups(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> list[dict]:
    """Return only groups where the user is an admin or owner."""
    from database import get_all_groups
    all_groups = get_all_groups()
    admin_groups = []
    for g in all_groups:
        try:
            member = await context.bot.get_chat_member(g["id"], user_id)
            if member.status in (ChatMember.ADMINISTRATOR, ChatMember.OWNER):
                admin_groups.append(g)
        except Exception:
            pass
    return admin_groups


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private":
        return

    groups = await _get_admin_groups(update.effective_user.id, context)
    if not groups:
        await update.message.reply_text("No groups configured yet. Add me to a group first.")
        return

    for g in groups:
        buttons = [
            [
                InlineKeyboardButton("Edit", callback_data=f"edit_group:{g['id']}"),
                InlineKeyboardButton(
                    "Toggle Active" if g.get("is_active", True) else "Enable",
                    callback_data=f"toggle_group:{g['id']}",
                ),
            ]
        ]
        await update.message.reply_text(
            _format_config(g),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons),
        )


async def cmd_preview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private":
        return

    groups = await _get_admin_groups(update.effective_user.id, context)
    if not groups:
        await update.message.reply_text("No groups configured yet.")
        return

    for g in groups:
        if not g.get("watermark_type"):
            await update.message.reply_text(f"No watermark configured for {g['title']}.")
            continue
        sample = generate_sample_image()
        watermarked = apply_watermark(sample, g)
        await update.message.reply_photo(
            photo=watermarked, caption=f"Preview for *{g['title']}*", parse_mode="Markdown"
        )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "*WatermarkGuard Commands*\n\n"
        "*DM Commands:*\n"
        "/start — Welcome & instructions\n"
        "/setup — Configure watermark for a group\n"
        "/settings — View/edit settings\n"
        "/preview — Preview watermark\n"
        "/buy — Purchase AI background credits\n"
        "/balance — Check credits & subscription\n"
        "/subscribe — Monthly subscription ($7/mo)\n"
        "/help — This message\n\n"
        "*Group Commands (admin only):*\n"
        "/wm\\_status — Show watermark config\n"
        "/wm\\_toggle — Enable/disable watermarking",
        parse_mode="Markdown",
    )


# ── Payment / credits commands ──


async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private":
        return
    buttons = [
        [InlineKeyboardButton(
            f"1 Credit — ${CREDIT_PRICE_USD:.2f}",
            callback_data="buy:credit_1",
        )],
        [InlineKeyboardButton(
            f"5 Credits — ${CREDIT_PRICE_USD * 5:.2f}",
            callback_data="buy:credit_5",
        )],
        [InlineKeyboardButton(
            f"Monthly Sub ({SUBSCRIPTION_CREDITS} credits) — ${SUBSCRIPTION_PRICE_USD:.2f}",
            callback_data="buy:subscription",
        )],
    ]
    await update.message.reply_text(
        "*Purchase AI Background Credits*\n\n"
        "Each credit = 1 AI background generation.\n"
        "Payments via crypto (USDC on BNB BEP20).",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private":
        return
    user = get_or_create_user(update.effective_user.id)
    lines = ["*Your Balance*\n"]

    credits = user.get("credits", 0)
    lines.append(f"Credits: *{credits}*")

    if is_trial_active(user):
        exp = trial_expires_at(user)
        lines.append(f"Trial: *Active* (expires {exp.strftime('%b %d, %Y')})")
    elif user.get("trial_start"):
        lines.append("Trial: *Expired*")

    if has_active_subscription(user):
        sub_exp = user.get("subscription_expires", "")
        used = user.get("monthly_credits_used", 0)
        remaining = SUBSCRIPTION_CREDITS - used
        lines.append(f"Subscription: *Active* ({remaining}/{SUBSCRIPTION_CREDITS} left)")
    else:
        lines.append("Subscription: *None*")

    lines.append(f"\nUse /buy to purchase credits.")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private":
        return
    try:
        link_data = create_payment_link(
            SUBSCRIPTION_PRICE_USD, update.effective_user.id, "subscription"
        )
        create_payment(
            user_id=update.effective_user.id,
            amount=SUBSCRIPTION_PRICE_USD,
            credits=SUBSCRIPTION_CREDITS,
            payment_type="subscription",
            blockradar_ref=link_data["reference"],
        )
        await update.message.reply_text(
            f"*Monthly Subscription — ${SUBSCRIPTION_PRICE_USD:.2f}*\n\n"
            f"{SUBSCRIPTION_CREDITS} AI background credits per month.\n\n"
            f"Pay here: {link_data['url']}\n\n"
            f"You'll be notified once payment is confirmed.",
            parse_mode="Markdown",
        )
    except Exception:
        logger.exception("Failed to create subscription link")
        await update.message.reply_text("Error creating payment link. Please try again later.")


async def buy_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":")[1]

    user_id = update.effective_user.id

    if choice == "subscription":
        amount = SUBSCRIPTION_PRICE_USD
        credits = SUBSCRIPTION_CREDITS
        payment_type = "subscription"
    elif choice == "credit_5":
        amount = CREDIT_PRICE_USD * 5
        credits = 5
        payment_type = "credits"
    else:
        amount = CREDIT_PRICE_USD
        credits = 1
        payment_type = "credits"

    try:
        link_data = create_payment_link(amount, user_id, payment_type)
        create_payment(
            user_id=user_id,
            amount=amount,
            credits=credits,
            payment_type=payment_type,
            blockradar_ref=link_data["reference"],
        )
        await query.edit_message_text(
            f"*Payment — ${amount:.2f}*\n\n"
            f"{'Monthly subscription' if payment_type == 'subscription' else f'{credits} credit(s)'}\n\n"
            f"Pay here: {link_data['url']}\n\n"
            f"You'll be notified once payment is confirmed.",
            parse_mode="Markdown",
        )
    except Exception:
        logger.exception("Failed to create payment link")
        await query.edit_message_text("Error creating payment link. Please try again with /buy.")


# ── Notification jobs ──


async def check_trial_expiry(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Notify users whose trial expires within 24 hours."""
    try:
        users = get_expiring_trials()
    except Exception:
        logger.exception("Failed to check expiring trials")
        return
    for u in users:
        try:
            await context.bot.send_message(
                u["user_id"],
                "Your WatermarkGuard free trial expires soon!\n"
                "Use /buy to purchase credits or /subscribe for monthly access.",
            )
        except Exception:
            pass


async def check_low_credits(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Notify users with low credits."""
    try:
        users = get_low_credit_users()
    except Exception:
        logger.exception("Failed to check low credit users")
        return
    for u in users:
        try:
            credits = u.get("credits", 0)
            await context.bot.send_message(
                u["user_id"],
                f"You have {credits} AI credit(s) remaining.\n"
                f"Use /buy to top up or /subscribe for {SUBSCRIPTION_CREDITS} credits/month.",
            )
        except Exception:
            pass


async def check_subscription_expiry(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Notify users whose subscription expires within 3 days."""
    try:
        users = get_expiring_subscriptions()
    except Exception:
        logger.exception("Failed to check expiring subscriptions")
        return
    for u in users:
        try:
            await context.bot.send_message(
                u["user_id"],
                "Your WatermarkGuard subscription expires in 3 days.\n"
                "Use /subscribe to renew.",
            )
        except Exception:
            pass


# ── Group commands ──


async def cmd_wm_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if chat.type == "private":
        return
    if not await _is_admin(chat.id, user.id, context):
        await update.message.reply_text("Only admins can use this command.")
        return
    group = get_group(chat.id)
    if not group:
        await update.message.reply_text("No config found. Ask an admin to DM me /setup.")
        return
    await update.message.reply_text(_format_config(group), parse_mode="Markdown")


async def cmd_wm_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if chat.type == "private":
        return
    if not await _is_admin(chat.id, user.id, context):
        await update.message.reply_text("Only admins can use this command.")
        return
    group = get_group(chat.id)
    if not group:
        await update.message.reply_text("No config found. Ask an admin to DM me /setup.")
        return
    new_state = not group.get("is_active", True)
    update_group(chat.id, {"is_active": new_state})
    status = "enabled" if new_state else "disabled"
    await update.message.reply_text(f"Watermarking is now *{status}* for this group.", parse_mode="Markdown")


# ── Settings callbacks ──


async def toggle_group_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    group_id = int(query.data.split(":")[1])
    group = get_group(group_id)
    if not group:
        await query.edit_message_text("Group not found.")
        return
    new_state = not group.get("is_active", True)
    update_group(group_id, {"is_active": new_state})
    group["is_active"] = new_state
    await query.edit_message_text(_format_config(group), parse_mode="Markdown")


async def edit_group_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Use /setup to reconfigure this group's watermark.")


# ── Image processing ──


async def _process_single_image(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    file_id: str,
    caption: str | None,
    chat_id: int,
    user_id: int,
    username: str | None,
    message_id: int,
) -> None:
    group = get_group(chat_id)

    # No config yet
    if not group or not group.get("watermark_type"):
        try:
            await context.bot.send_message(
                user_id,
                f"No watermark configured for *{group['title'] if group else 'this group'}*. "
                f"Use /setup to configure.",
                parse_mode="Markdown",
            )
        except Forbidden:
            mention = f"@{username}" if username else "Admin"
            tmp = await context.bot.send_message(
                chat_id,
                f"{mention} I need to DM you but you haven't started a chat with me. "
                f"Please DM me first.",
            )
            await asyncio.sleep(30)
            try:
                await tmp.delete()
            except Exception:
                pass
        return

    if not group.get("is_active", True):
        return

    # Delete original message
    try:
        await context.bot.delete_message(chat_id, message_id)
    except BadRequest as e:
        if "can't be deleted" in str(e).lower() or "not enough rights" in str(e).lower():
            # DM admin about permissions
            try:
                await context.bot.send_message(
                    user_id,
                    f"I need 'Delete Messages' permission in *{group['title']}*.",
                    parse_mode="Markdown",
                )
            except Forbidden:
                pass
            return
        raise

    # Download image
    file = await context.bot.get_file(file_id)
    file_bytes = await file.download_as_bytearray()

    # Apply watermark
    watermarked_bytes = apply_watermark(bytes(file_bytes), group)

    # Save to pending
    try:
        pending = create_pending_image(
            group_id=chat_id,
            admin_id=user_id,
            original_file_id=file_id,
            original_caption=caption,
        )
        pending_id = pending["id"]
    except Exception:
        logger.exception("Supabase error creating pending image, queueing locally")
        _local_queue.append(
            {
                "group_id": chat_id,
                "admin_id": user_id,
                "file_id": file_id,
                "caption": caption,
                "watermarked_bytes": watermarked_bytes,
            }
        )
        return

    # Send preview to sender via DM
    buttons = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Approve", callback_data=f"approve:{pending_id}"),
                InlineKeyboardButton("Reject", callback_data=f"reject:{pending_id}"),
            ]
        ]
    )

    try:
        dm_msg = await context.bot.send_photo(
            chat_id=user_id,
            photo=watermarked_bytes,
            caption=f"Watermarked preview for *{group['title']}*.\n"
            f"Original caption: {caption or '(none)'}",
            parse_mode="Markdown",
            reply_markup=buttons,
        )
        # Store watermarked bytes in bot_data for later retrieval
        context.bot_data[f"wm_bytes:{pending_id}"] = watermarked_bytes
    except Forbidden:
        # User hasn't started DM
        mention = f"@{username}" if username else "the sender"
        tmp = await context.bot.send_message(
            chat_id,
            f"{mention} I need to DM you for image approval. Please start a chat with me first.",
        )
        await asyncio.sleep(30)
        try:
            await tmp.delete()
        except Exception:
            pass
        update_pending_image(pending_id, {"status": "rejected"})


async def on_channel_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle images posted in channels — auto-watermark without approval since only admins post."""
    message = update.channel_post or update.effective_message
    if not message:
        return

    chat = message.chat
    if chat.type != "channel":
        return

    if not _check_rate_limit(chat.id):
        logger.warning(f"Rate limit hit for channel {chat.id}")
        return

    group = get_group(chat.id)
    if not group or not group.get("watermark_type") or not group.get("is_active", True):
        return

    # Extract file_id and caption
    caption = message.caption
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document:
        file_id = message.document.file_id
    else:
        return

    # Delete original
    try:
        await context.bot.delete_message(chat.id, message.message_id)
    except BadRequest:
        logger.warning(f"Cannot delete message in channel {chat.id}")
        return

    # Download and watermark
    file = await context.bot.get_file(file_id)
    file_bytes = await file.download_as_bytearray()
    watermarked_bytes = apply_watermark(bytes(file_bytes), group)

    # Post watermarked image directly (no approval needed for channels)
    await context.bot.send_photo(
        chat_id=chat.id,
        photo=watermarked_bytes,
        caption=caption,
    )


async def on_group_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private" or chat.type == "channel":
        return

    # Rate limit
    if not _check_rate_limit(chat.id):
        logger.warning(f"Rate limit hit for group {chat.id}")
        return

    # Check delete permission
    if not await _can_delete(chat.id, context):
        if user:
            try:
                await context.bot.send_message(
                    user.id,
                    f"I need 'Delete Messages' permission in *{chat.title}*.",
                    parse_mode="Markdown",
                )
            except Exception:
                pass
        return

    # Extract file_id and caption
    caption = message.caption
    username = user.username if user else None
    user_id = user.id if user else 0

    if message.media_group_id:
        # Media group handling
        mgid = message.media_group_id
        if message.photo:
            file_id = message.photo[-1].file_id
        elif message.document:
            file_id = message.document.file_id
        else:
            return

        if mgid not in _media_groups:
            _media_groups[mgid] = {"items": [], "timer_task": None}

        _media_groups[mgid]["items"].append(
            {
                "file_id": file_id,
                "caption": caption,
                "user_id": user_id,
                "username": username,
                "message_id": message.message_id,
                "chat_id": chat.id,
            }
        )

        # Cancel existing timer and set a new one
        if _media_groups[mgid]["timer_task"]:
            _media_groups[mgid]["timer_task"].cancel()

        async def process_media_group(mg_id: str):
            await asyncio.sleep(2)  # Wait for all photos in group
            items = _media_groups.pop(mg_id, {}).get("items", [])
            for item in items:
                await _process_single_image(
                    update,
                    context,
                    item["file_id"],
                    item["caption"],
                    item["chat_id"],
                    item["user_id"],
                    item["username"],
                    item["message_id"],
                )

        _media_groups[mgid]["timer_task"] = asyncio.create_task(
            process_media_group(mgid)
        )
        return

    # Single image
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document:
        file_id = message.document.file_id
    else:
        return

    await _process_single_image(
        update, context, file_id, caption, chat.id, user_id, username, message.message_id
    )


# ── Approval callbacks ──


async def approve_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    pending_id = query.data.split(":")[1]

    pending = get_pending_image(pending_id)
    if not pending:
        await query.edit_message_caption(caption="Image record not found.")
        return

    if pending["status"] != "pending":
        await query.edit_message_caption(caption=f"Image already {pending['status']}.")
        return

    group = get_group(pending["group_id"])
    if not group:
        await query.edit_message_caption(caption="Group no longer exists.")
        return

    # Get watermarked bytes
    wm_key = f"wm_bytes:{pending_id}"
    watermarked_bytes = context.bot_data.get(wm_key)

    if not watermarked_bytes:
        # Re-process if bytes not in memory
        try:
            file = await context.bot.get_file(pending["original_file_id"])
            file_bytes = await file.download_as_bytearray()
            watermarked_bytes = apply_watermark(bytes(file_bytes), group)
        except Exception:
            await query.edit_message_caption(caption="Failed to process image. Please repost.")
            update_pending_image(pending_id, {"status": "rejected"})
            return

    # Get sender info for credit
    try:
        sender = await context.bot.get_chat_member(pending["group_id"], pending["admin_id"])
        sender_name = sender.user.first_name or "Someone"
        sender_mention = f'<a href="tg://user?id={pending["admin_id"]}">{sender_name}</a>'
    except Exception:
        sender_mention = "A member"

    caption_parts = []
    if pending.get("original_caption"):
        caption_parts.append(pending["original_caption"])
    caption_parts.append(f"Posted by {sender_mention}")
    final_caption = "\n".join(caption_parts)

    # Post to group
    try:
        sent = await context.bot.send_photo(
            chat_id=pending["group_id"],
            photo=watermarked_bytes,
            caption=final_caption,
            parse_mode="HTML",
        )
        update_pending_image(
            pending_id,
            {"status": "approved", "watermarked_file_id": sent.photo[-1].file_id},
        )
    except Exception:
        logger.exception("Failed to post watermarked image to group")
        await query.edit_message_caption(caption="Failed to post to group. Please try again.")
        return

    await query.edit_message_caption(caption="Image approved and posted!")

    # Cleanup
    context.bot_data.pop(wm_key, None)


async def reject_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    pending_id = query.data.split(":")[1]

    pending = get_pending_image(pending_id)
    if not pending:
        await query.edit_message_caption(caption="Image record not found.")
        return

    if pending["status"] != "pending":
        await query.edit_message_caption(caption=f"Image already {pending['status']}.")
        return

    update_pending_image(pending_id, {"status": "rejected"})
    await query.edit_message_caption(caption="Image rejected and discarded.")

    # Cleanup
    context.bot_data.pop(f"wm_bytes:{pending_id}", None)


# ── Expiration job ──


async def expire_pending_images(context: ContextTypes.DEFAULT_TYPE) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=APPROVAL_TIMEOUT_SECONDS)
    cutoff_iso = cutoff.isoformat()

    try:
        expired = get_expired_pending_images(cutoff_iso)
    except Exception:
        logger.exception("Failed to check expired images")
        return

    for img in expired:
        update_pending_image(img["id"], {"status": "rejected"})
        try:
            await context.bot.send_message(
                img["admin_id"],
                "An image you posted has expired (no response within 1 hour). Repost it to try again.",
            )
        except Exception:
            pass

        # Cleanup stored bytes
        context.bot_data.pop(f"wm_bytes:{img['id']}", None)


# ── Retry local queue job ──


async def retry_local_queue(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _local_queue:
        return

    remaining = []
    for item in _local_queue:
        try:
            pending = create_pending_image(
                group_id=item["group_id"],
                admin_id=item["admin_id"],
                original_file_id=item["file_id"],
                original_caption=item["caption"],
            )
            pending_id = pending["id"]
            buttons = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Approve", callback_data=f"approve:{pending_id}"),
                        InlineKeyboardButton("Reject", callback_data=f"reject:{pending_id}"),
                    ]
                ]
            )
            context.bot_data[f"wm_bytes:{pending_id}"] = item["watermarked_bytes"]
            await context.bot.send_photo(
                chat_id=item["admin_id"],
                photo=item["watermarked_bytes"],
                caption="Watermarked preview (delayed due to temporary issue).",
                reply_markup=buttons,
            )
        except Exception:
            remaining.append(item)

    _local_queue.clear()
    _local_queue.extend(remaining)


# ── Main ──


async def main() -> None:
    from aiohttp import web

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Setup conversation handler (must be added before generic handlers)
    app.add_handler(build_setup_handler())

    # Chat member updates (bot added/removed)
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

    # DM commands
    app.add_handler(CommandHandler("start", cmd_start, filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("settings", cmd_settings, filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("preview", cmd_preview, filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("buy", cmd_buy, filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("balance", cmd_balance, filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("subscribe", cmd_subscribe, filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("help", cmd_help))

    # Group commands
    app.add_handler(CommandHandler("wm_status", cmd_wm_status))
    app.add_handler(CommandHandler("wm_toggle", cmd_wm_toggle))

    # Settings callbacks
    app.add_handler(CallbackQueryHandler(toggle_group_cb, pattern=r"^toggle_group:"))
    app.add_handler(CallbackQueryHandler(edit_group_cb, pattern=r"^edit_group:"))

    # Purchase callbacks
    app.add_handler(CallbackQueryHandler(buy_cb, pattern=r"^buy:"))

    # Approval callbacks
    app.add_handler(CallbackQueryHandler(approve_cb, pattern=r"^approve:"))
    app.add_handler(CallbackQueryHandler(reject_cb, pattern=r"^reject:"))

    # Image handler for groups
    app.add_handler(
        MessageHandler(
            (filters.PHOTO | filters.Document.IMAGE)
            & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
            on_group_image,
        )
    )

    # Channel post handler — auto-watermark without approval
    app.add_handler(
        MessageHandler(
            (filters.PHOTO | filters.Document.IMAGE)
            & filters.UpdateType.CHANNEL_POST,
            on_channel_image,
        )
    )

    # Scheduled jobs
    job_queue = app.job_queue
    job_queue.run_repeating(expire_pending_images, interval=300, first=60)
    job_queue.run_repeating(retry_local_queue, interval=120, first=30)
    job_queue.run_repeating(check_trial_expiry, interval=3600, first=300)
    job_queue.run_repeating(check_low_credits, interval=3600, first=600)
    job_queue.run_repeating(check_subscription_expiry, interval=3600, first=900)

    # Initialize bot application
    await app.initialize()
    await app.start()

    # Give webhook server access to the bot for notifications
    set_bot_app(app)

    # Start Telegram polling
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    logger.info("WatermarkGuard bot polling started")

    # Start aiohttp webhook server
    web_app = create_web_app()
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Webhook server listening on port {PORT}")

    # Block forever
    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
