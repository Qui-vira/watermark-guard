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
from config import WATERMARK_POSITIONS, ROTATION_OPTIONS, ACCENT_PRESETS, AI_THEMES

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
    # Template-specific states
    TPL_BRAND_NAME,
    TPL_UPLOAD_LOGO,
    TPL_ACCENT_COLOR,
    TPL_ACCENT_CUSTOM,
    TPL_STARS,
    TPL_TAGLINE,
    TPL_CONTACT_WA,
    TPL_CONTACT_TG,
    TPL_CONTACT_IG,
    TPL_CONTACT_LI,
    TPL_CONFIRM_PREVIEW,
    # AI background states
    TPL_THEME_SELECT,
    TPL_AI_CONFIRM,
) = range(23)

# Temporary config storage key
SETUP_KEY = "setup_config"


def _get_setup(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data.setdefault(SETUP_KEY, {})


def _set_setup(context: ContextTypes.DEFAULT_TYPE, data: dict) -> None:
    context.user_data[SETUP_KEY] = data


async def _get_admin_groups(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> list[dict]:
    """Return only groups where the user is an admin or owner."""
    from telegram import ChatMember
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


async def setup_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_chat.type != "private":
        await update.message.reply_text("Please use /setup in a DM with me.")
        return ConversationHandler.END

    # Find groups where this user is an admin
    all_groups = await _get_admin_groups(update.effective_user.id, context)
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
        ],
        [InlineKeyboardButton("Branded Template", callback_data="wm_type:template")],
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

    if wm_type == "template":
        await query.edit_message_text("What's your brand name?")
        return TPL_BRAND_NAME
    elif wm_type in ("text", "both"):
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
                ],
                [InlineKeyboardButton("Branded Template", callback_data="wm_type:template")],
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


# ── Template setup handlers ──


async def tpl_brand_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    setup = _get_setup(context)
    setup["brand_name"] = update.message.text.strip()
    buttons = [
        [
            InlineKeyboardButton("Upload Logo", callback_data="tpl_logo:yes"),
            InlineKeyboardButton("Skip", callback_data="tpl_logo:skip"),
        ]
    ]
    await update.message.reply_text(
        "Upload a logo for the header?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return TPL_UPLOAD_LOGO


async def tpl_logo_choice_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":")[1]
    if choice == "yes":
        await query.edit_message_text("Send me the logo image:")
        return TPL_UPLOAD_LOGO
    return await _ask_accent_color(update, context, via_query=True)


async def tpl_upload_logo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    setup = _get_setup(context)
    if update.message.photo:
        file = await update.message.photo[-1].get_file()
    elif update.message.document:
        file = await update.message.document.get_file()
    else:
        await update.message.reply_text("Please send an image file as the logo.")
        return TPL_UPLOAD_LOGO

    file_bytes = await file.download_as_bytearray()
    logo_path = upload_logo(setup["group_id"], bytes(file_bytes), "logo.png")
    setup["logo_path"] = logo_path
    return await _ask_accent_color(update, context, via_query=False)


async def _ask_accent_color(update: Update, context: ContextTypes.DEFAULT_TYPE,
                            via_query: bool = False) -> int:
    buttons = []
    row = []
    for name, hex_val in ACCENT_PRESETS.items():
        row.append(InlineKeyboardButton(name, callback_data=f"accent:{hex_val}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("Custom Hex", callback_data="accent:custom")])

    text = "Pick your accent color:"
    if via_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup(buttons)
        )
    else:
        await update.message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(buttons)
        )
    return TPL_ACCENT_COLOR


async def tpl_accent_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    value = query.data.split(":")[1]
    setup = _get_setup(context)

    if value == "custom":
        await query.edit_message_text("Send me a hex color code (e.g. #FF5500):")
        return TPL_ACCENT_CUSTOM

    setup["accent_color"] = value
    return await _ask_theme(update, context)


async def tpl_accent_custom(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    setup = _get_setup(context)
    text = update.message.text.strip()
    # Validate hex
    clean = text.lstrip("#")
    if len(clean) != 6 or not all(c in "0123456789abcdefABCDEF" for c in clean):
        await update.message.reply_text("Invalid hex code. Send a 6-digit hex like #FF5500:")
        return TPL_ACCENT_CUSTOM
    setup["accent_color"] = f"#{clean.upper()}"
    return await _ask_theme(update, context)


# ── AI Background theme selection ──


async def _ask_theme(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show AI background theme options or free gradient."""
    buttons = [[InlineKeyboardButton("Free (Gradient)", callback_data="theme:free")]]
    row = []
    for theme_name in AI_THEMES:
        row.append(InlineKeyboardButton(theme_name.title(), callback_data=f"theme:{theme_name}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    text = (
        "Choose a background style:\n\n"
        "Free — Classic gradient background\n"
        "AI Themes — Premium cinematic AI-generated backgrounds"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup(buttons)
        )
    else:
        await update.message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(buttons)
        )
    return TPL_THEME_SELECT


async def tpl_theme_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    theme = query.data.split(":")[1]
    setup = _get_setup(context)

    if theme == "free":
        setup["template_bg_path"] = None
        return await _ask_stars(update, context)

    setup["ai_theme"] = theme

    # Check credits/trial
    from billing import get_or_create_user, can_generate_ai_bg, is_trial_active

    user = get_or_create_user(update.effective_user.id)

    if not user.get("trial_start"):
        # First time — offer trial
        buttons = [
            [InlineKeyboardButton("Start Free Trial (3 days)", callback_data="ai_gen:trial")],
            [InlineKeyboardButton("Skip (Free Gradient)", callback_data="ai_gen:skip")],
        ]
        text = (
            "AI backgrounds are a premium feature.\n\n"
            "Start a 3-day free trial to try them out!"
        )
    elif can_generate_ai_bg(user)[0]:
        allowed, reason = can_generate_ai_bg(user)
        buttons = [
            [InlineKeyboardButton("Generate AI Background", callback_data="ai_gen:go")],
            [InlineKeyboardButton("Skip (Free Gradient)", callback_data="ai_gen:skip")],
        ]
        text = f"Generate AI background?\n{reason}"
    else:
        _, reason = can_generate_ai_bg(user)
        buttons = [
            [InlineKeyboardButton("Buy Credits (/buy)", callback_data="ai_gen:buy")],
            [InlineKeyboardButton("Skip (Free Gradient)", callback_data="ai_gen:skip")],
        ]
        text = f"{reason}\n\nUse /buy to purchase credits."

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
    return TPL_AI_CONFIRM


async def tpl_ai_confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":")[1]
    setup = _get_setup(context)

    if choice == "skip":
        setup["template_bg_path"] = None
        return await _ask_stars(update, context)

    if choice == "buy":
        await query.edit_message_text("Use /buy to purchase credits, then run /setup again.")
        _set_setup(context, {})
        return ConversationHandler.END

    # Start trial or deduct credit
    from billing import (
        get_or_create_user, start_trial, is_trial_active,
        has_active_subscription, deduct_credit, can_generate_ai_bg,
    )

    user = get_or_create_user(update.effective_user.id)
    credit_deducted = False

    if choice == "trial":
        start_trial(update.effective_user.id)
    elif choice == "go":
        allowed, _ = can_generate_ai_bg(user)
        if not allowed:
            await query.edit_message_text("No credits available. Use /buy to purchase.")
            _set_setup(context, {})
            return ConversationHandler.END
        # Deduct only if not on trial and not subscription
        if not is_trial_active(user):
            credit_deducted = deduct_credit(update.effective_user.id)

    # Generate AI background
    await query.edit_message_text("Generating AI background... (15-30 seconds)")
    try:
        from ai_background import generate_ai_background, upload_ai_background

        accent = setup.get("accent_color", "#00CCFF")
        theme = setup.get("ai_theme", "abstract")
        bg_bytes = generate_ai_background(theme, accent)
        bg_path = upload_ai_background(setup["group_id"], bg_bytes)
        setup["template_bg_path"] = bg_path

        await query.from_user.send_photo(
            photo=bg_bytes, caption="AI background generated!"
        )
    except Exception:
        logger.exception("AI background generation failed")
        setup["template_bg_path"] = None
        # Refund credit if we deducted one
        if credit_deducted:
            from billing import add_credits
            add_credits(update.effective_user.id, 1)
        await query.from_user.send_message(
            "AI generation failed. Using free gradient instead.\n"
            "Your credit was not deducted."
        )

    return await _ask_stars(update, context)


async def _ask_stars(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    buttons = [
        [InlineKeyboardButton(f"{'★' * i}{'☆' * (5 - i)}", callback_data=f"stars:{i}")]
        for i in range(6)
    ]
    text = "Star rating to display (0 = none):"
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup(buttons)
        )
    else:
        await update.message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(buttons)
        )
    return TPL_STARS


async def tpl_stars_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    setup = _get_setup(context)
    setup["star_rating"] = int(query.data.split(":")[1])
    await query.edit_message_text("Enter a tagline (or send 'skip'):")
    return TPL_TAGLINE


async def tpl_tagline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    setup = _get_setup(context)
    text = update.message.text.strip()
    if text.lower() != "skip":
        setup["template_tagline"] = text
    await update.message.reply_text("WhatsApp contact? (or send 'skip'):")
    return TPL_CONTACT_WA


async def tpl_contact_wa(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    setup = _get_setup(context)
    text = update.message.text.strip()
    if text.lower() != "skip":
        setup["contact_whatsapp"] = text
    await update.message.reply_text("Telegram handle? (or send 'skip'):")
    return TPL_CONTACT_TG


async def tpl_contact_tg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    setup = _get_setup(context)
    text = update.message.text.strip()
    if text.lower() != "skip":
        setup["contact_telegram"] = text
    await update.message.reply_text("Instagram handle? (or send 'skip'):")
    return TPL_CONTACT_IG


async def tpl_contact_ig(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    setup = _get_setup(context)
    text = update.message.text.strip()
    if text.lower() != "skip":
        setup["contact_instagram"] = text
    await update.message.reply_text("LinkedIn handle? (or send 'skip'):")
    return TPL_CONTACT_LI


async def tpl_contact_li(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    setup = _get_setup(context)
    text = update.message.text.strip()
    if text.lower() != "skip":
        setup["contact_linkedin"] = text

    # Generate template preview
    sample = generate_sample_image()
    preview_config = {
        "watermark_type": "template",
        "brand_name": setup.get("brand_name"),
        "logo_path": setup.get("logo_path"),
        "accent_color": setup.get("accent_color", "#00CCFF"),
        "star_rating": setup.get("star_rating", 5),
        "template_tagline": setup.get("template_tagline"),
        "contact_whatsapp": setup.get("contact_whatsapp"),
        "contact_telegram": setup.get("contact_telegram"),
        "contact_instagram": setup.get("contact_instagram"),
        "contact_linkedin": setup.get("contact_linkedin"),
        "template_bg_path": setup.get("template_bg_path"),
        "title": setup.get("group_title", ""),
    }
    watermarked = apply_watermark(sample, preview_config)

    buttons = [
        [
            InlineKeyboardButton("Confirm", callback_data="tpl_confirm:yes"),
            InlineKeyboardButton("Redo", callback_data="tpl_confirm:redo"),
        ]
    ]
    await update.message.reply_photo(
        photo=watermarked,
        caption="Template preview — does this look good?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return TPL_CONFIRM_PREVIEW


async def tpl_confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":")[1]

    if choice == "redo":
        await query.edit_message_caption(caption="Let's redo. Starting over...")
        setup = _get_setup(context)
        group_id = setup.get("group_id")
        group_title = setup.get("group_title")
        _set_setup(context, {"group_id": group_id, "group_title": group_title})
        return await _ask_watermark_type(update, context)

    # Save template config to Supabase
    setup = _get_setup(context)
    updates = {
        "watermark_type": "template",
        "brand_name": setup.get("brand_name"),
        "accent_color": setup.get("accent_color", "#00CCFF"),
        "star_rating": setup.get("star_rating", 5),
        "template_tagline": setup.get("template_tagline"),
        "contact_whatsapp": setup.get("contact_whatsapp"),
        "contact_telegram": setup.get("contact_telegram"),
        "contact_instagram": setup.get("contact_instagram"),
        "contact_linkedin": setup.get("contact_linkedin"),
        "template_bg_path": setup.get("template_bg_path"),
    }
    if "logo_path" in setup:
        updates["logo_path"] = setup["logo_path"]

    update_group(setup["group_id"], updates)
    await query.edit_message_caption(
        caption=f"Branded template configured for *{setup['group_title']}*! Images will now be wrapped in your branded frame.",
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
            # Template states
            TPL_BRAND_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, tpl_brand_name)],
            TPL_UPLOAD_LOGO: [
                CallbackQueryHandler(tpl_logo_choice_cb, pattern=r"^tpl_logo:"),
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, tpl_upload_logo),
            ],
            TPL_ACCENT_COLOR: [CallbackQueryHandler(tpl_accent_cb, pattern=r"^accent:")],
            TPL_ACCENT_CUSTOM: [MessageHandler(filters.TEXT & ~filters.COMMAND, tpl_accent_custom)],
            TPL_STARS: [CallbackQueryHandler(tpl_stars_cb, pattern=r"^stars:")],
            TPL_TAGLINE: [MessageHandler(filters.TEXT & ~filters.COMMAND, tpl_tagline)],
            TPL_CONTACT_WA: [MessageHandler(filters.TEXT & ~filters.COMMAND, tpl_contact_wa)],
            TPL_CONTACT_TG: [MessageHandler(filters.TEXT & ~filters.COMMAND, tpl_contact_tg)],
            TPL_CONTACT_IG: [MessageHandler(filters.TEXT & ~filters.COMMAND, tpl_contact_ig)],
            TPL_CONTACT_LI: [MessageHandler(filters.TEXT & ~filters.COMMAND, tpl_contact_li)],
            TPL_CONFIRM_PREVIEW: [CallbackQueryHandler(tpl_confirm_cb, pattern=r"^tpl_confirm:")],
            # AI background states
            TPL_THEME_SELECT: [CallbackQueryHandler(tpl_theme_cb, pattern=r"^theme:")],
            TPL_AI_CONFIRM: [CallbackQueryHandler(tpl_ai_confirm_cb, pattern=r"^ai_gen:")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )
