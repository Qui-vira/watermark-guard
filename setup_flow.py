"""Conversation handler for /setup command (DM-based admin configuration)."""

import io
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)
from database import get_group, update_group, upload_logo, get_all_groups
from watermark import apply_watermark, generate_sample_image
from config import WATERMARK_POSITIONS, ROTATION_OPTIONS

logger = logging.getLogger(__name__)

# Conversation states
(
    SELECT_GROUP,
    SELECT_TYPE,
    ENTER_TEXT,
    UPLOAD_LOGO,
    ASK_URL,
    ENTER_URL,
    ASK_CHANNEL_NAME,
    SELECT_POSITION,
    SELECT_ROTATION,
    CONFIRM_PREVIEW,
) = range(10)

# Temporary config storage key
SETUP_KEY = "setup_config"


def _get_setup(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data.setdefault(SETUP_KEY, {})


def _set_setup(context: ContextTypes.DEFAULT_TYPE, data: dict) -> None:
    context.user_data[SETUP_KEY] = data


async def setup_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_chat.type != "private":
        await update.message.reply_text("Please use /setup in a DM with me.")
        return ConversationHandler.END

    # Find groups where this bot is present
    all_groups = get_all_groups()
    if not all_groups:
        await update.message.reply_text(
            "I'm not added to any groups yet. Add me to a group first, then come back and run /setup."
        )
        return ConversationHandler.END

    if len(all_groups) == 1:
        group = all_groups[0]
        _set_setup(context, {"group_id": group["id"], "group_title": group["title"]})
        return await _ask_watermark_type(update, context)

    # Multiple groups — let admin pick
    buttons = [
        [InlineKeyboardButton(g["title"], callback_data=f"setup_group:{g['id']}")]
        for g in all_groups
    ]
    await update.message.reply_text(
        "Which group do you want to configure?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return SELECT_GROUP


async def select_group_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    group_id = int(query.data.split(":")[1])
    group = get_group(group_id)
    if not group:
        await query.edit_message_text("Group not found. Try /setup again.")
        return ConversationHandler.END
    _set_setup(context, {"group_id": group_id, "group_title": group["title"]})
    return await _ask_watermark_type(update, context)


async def _ask_watermark_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    setup = _get_setup(context)
    buttons = [
        [
            InlineKeyboardButton("Text Only", callback_data="wm_type:text"),
            InlineKeyboardButton("Logo Only", callback_data="wm_type:logo"),
            InlineKeyboardButton("Both", callback_data="wm_type:both"),
        ]
    ]
    text = f"Setting up watermark for *{setup['group_title']}*.\n\nWhat should the watermark contain?"
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown"
        )
    return SELECT_TYPE


async def select_type_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    wm_type = query.data.split(":")[1]
    setup = _get_setup(context)
    setup["watermark_type"] = wm_type

    if wm_type in ("text", "both"):
        await query.edit_message_text("Send me the text you want as watermark:")
        return ENTER_TEXT
    else:
        await query.edit_message_text("Send me the logo image:")
        return UPLOAD_LOGO


async def enter_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    setup = _get_setup(context)
    setup["watermark_text"] = update.message.text.strip()

    if setup["watermark_type"] == "both":
        await update.message.reply_text("Now send me the logo image:")
        return UPLOAD_LOGO

    return await _ask_url(update, context)


async def upload_logo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    setup = _get_setup(context)

    if update.message.photo:
        file = await update.message.photo[-1].get_file()
    elif update.message.document:
        file = await update.message.document.get_file()
    else:
        await update.message.reply_text("Please send an image file as the logo.")
        return UPLOAD_LOGO

    file_bytes = await file.download_as_bytearray()
    logo_path = upload_logo(setup["group_id"], bytes(file_bytes), "logo.png")
    setup["logo_path"] = logo_path

    return await _ask_url(update, context)


async def _ask_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    buttons = [
        [
            InlineKeyboardButton("Yes", callback_data="ask_url:yes"),
            InlineKeyboardButton("Skip", callback_data="ask_url:skip"),
        ]
    ]
    await update.message.reply_text(
        "Add a URL or handle to the watermark?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return ASK_URL


async def ask_url_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":")[1]

    if choice == "yes":
        await query.edit_message_text("Send me the URL or handle:")
        return ENTER_URL

    return await _ask_channel_name(update, context, via_query=True)


async def enter_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    setup = _get_setup(context)
    setup["watermark_url"] = update.message.text.strip()
    return await _ask_channel_name(update, context, via_query=False)


async def _ask_channel_name(
    update: Update, context: ContextTypes.DEFAULT_TYPE, via_query: bool = False
) -> int:
    buttons = [
        [
            InlineKeyboardButton("Yes", callback_data="chan_name:yes"),
            InlineKeyboardButton("No", callback_data="chan_name:no"),
        ]
    ]
    text = "Use channel/group name in the watermark?"
    if via_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup(buttons)
        )
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))
    return ASK_CHANNEL_NAME


async def channel_name_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    setup = _get_setup(context)
    setup["use_channel_name"] = query.data.split(":")[1] == "yes"

    # Position selection
    buttons = []
    row = []
    for i, pos in enumerate(WATERMARK_POSITIONS):
        row.append(InlineKeyboardButton(pos.replace("-", " ").title(), callback_data=f"pos:{pos}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    await query.edit_message_text(
        "Where to place the watermark?", reply_markup=InlineKeyboardMarkup(buttons)
    )
    return SELECT_POSITION


async def select_position_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    setup = _get_setup(context)
    setup["watermark_position"] = query.data.split(":")[1]

    buttons = []
    row = []
    for angle in ROTATION_OPTIONS:
        label = f"{angle}°"
        row.append(InlineKeyboardButton(label, callback_data=f"rot:{angle}"))
        if len(row) == 4:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    await query.edit_message_text(
        "Rotation angle?", reply_markup=InlineKeyboardMarkup(buttons)
    )
    return SELECT_ROTATION


async def select_rotation_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    setup = _get_setup(context)
    setup["watermark_rotation"] = int(query.data.split(":")[1])

    # Generate preview
    sample = generate_sample_image()
    preview_config = {
        "watermark_type": setup.get("watermark_type", "text"),
        "watermark_text": setup.get("watermark_text"),
        "watermark_url": setup.get("watermark_url"),
        "use_channel_name": setup.get("use_channel_name", False),
        "title": setup.get("group_title", ""),
        "logo_path": setup.get("logo_path"),
        "watermark_position": setup.get("watermark_position", "bottom-right"),
        "watermark_rotation": setup.get("watermark_rotation", 0),
    }
    watermarked = apply_watermark(sample, preview_config)

    buttons = [
        [
            InlineKeyboardButton("Confirm", callback_data="confirm:yes"),
            InlineKeyboardButton("Redo", callback_data="confirm:redo"),
        ]
    ]
    await query.delete_message()
    await query.from_user.send_photo(
        photo=watermarked,
        caption="Preview — does this look good?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return CONFIRM_PREVIEW


async def confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":")[1]

    if choice == "redo":
        await query.edit_message_caption(caption="Let's redo. Starting over...")
        # Reset and restart
        _set_setup(context, {})
        all_groups = get_all_groups()
        if len(all_groups) == 1:
            group = all_groups[0]
            _set_setup(context, {"group_id": group["id"], "group_title": group["title"]})
            buttons = [
                [
                    InlineKeyboardButton("Text Only", callback_data="wm_type:text"),
                    InlineKeyboardButton("Logo Only", callback_data="wm_type:logo"),
                    InlineKeyboardButton("Both", callback_data="wm_type:both"),
                ]
            ]
            await query.from_user.send_message(
                f"Setting up watermark for *{group['title']}*.\n\nWhat should the watermark contain?",
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode="Markdown",
            )
            return SELECT_TYPE

        buttons = [
            [InlineKeyboardButton(g["title"], callback_data=f"setup_group:{g['id']}")]
            for g in all_groups
        ]
        await query.from_user.send_message(
            "Which group do you want to configure?",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return SELECT_GROUP

    # Save config to Supabase
    setup = _get_setup(context)
    updates = {
        "watermark_type": setup.get("watermark_type", "text"),
        "watermark_text": setup.get("watermark_text"),
        "watermark_url": setup.get("watermark_url"),
        "use_channel_name": setup.get("use_channel_name", False),
        "watermark_position": setup.get("watermark_position", "bottom-right"),
        "watermark_rotation": setup.get("watermark_rotation", 0),
    }
    if "logo_path" in setup:
        updates["logo_path"] = setup["logo_path"]

    update_group(setup["group_id"], updates)
    await query.edit_message_caption(
        caption=f"Watermark configured for *{setup['group_title']}*! Images in that group will now be watermarked.",
        parse_mode="Markdown",
    )
    _set_setup(context, {})
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _set_setup(context, {})
    await update.message.reply_text("Setup cancelled.")
    return ConversationHandler.END


def build_setup_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("setup", setup_start)],
        states={
            SELECT_GROUP: [CallbackQueryHandler(select_group_cb, pattern=r"^setup_group:")],
            SELECT_TYPE: [CallbackQueryHandler(select_type_cb, pattern=r"^wm_type:")],
            ENTER_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_text)],
            UPLOAD_LOGO: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, upload_logo_handler)
            ],
            ASK_URL: [CallbackQueryHandler(ask_url_cb, pattern=r"^ask_url:")],
            ENTER_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_url)],
            ASK_CHANNEL_NAME: [CallbackQueryHandler(channel_name_cb, pattern=r"^chan_name:")],
            SELECT_POSITION: [CallbackQueryHandler(select_position_cb, pattern=r"^pos:")],
            SELECT_ROTATION: [CallbackQueryHandler(select_rotation_cb, pattern=r"^rot:")],
            CONFIRM_PREVIEW: [CallbackQueryHandler(confirm_cb, pattern=r"^confirm:")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )
