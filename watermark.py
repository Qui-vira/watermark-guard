"""Pillow-based watermark engine."""

import io
import logging
import os
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from config import (
    FONT_PATH,
    FONT_BOLD_PATH,
    SAMPLE_IMAGE_SIZE,
    SAMPLE_IMAGE_COLOR,
    TEMPLATE_CANVAS_WIDTH,
    TEMPLATE_PADDING,
    TEMPLATE_HEADER_HEIGHT,
    TEMPLATE_FOOTER_HEIGHT,
    TEMPLATE_IMAGE_BORDER,
    TEMPLATE_BG_TOP,
    TEMPLATE_BG_BOTTOM,
    TEMPLATE_DEFAULT_ACCENT,
)
from database import download_logo

logger = logging.getLogger(__name__)

# Resolve font path at import time — check multiple locations
_RESOLVED_FONT = None
_FONT_SEARCH_PATHS = [
    FONT_PATH,
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "DejaVuSans.ttf"),
    "/app/fonts/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
for _p in _FONT_SEARCH_PATHS:
    if os.path.isfile(_p):
        _RESOLVED_FONT = _p
        break

if _RESOLVED_FONT:
    logger.info(f"Font found at: {_RESOLVED_FONT}")
else:
    logger.warning("No font file found in any search path")

# Resolve bold font
_RESOLVED_BOLD_FONT = None
_BOLD_FONT_SEARCH_PATHS = [
    FONT_BOLD_PATH,
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "DejaVuSans-Bold.ttf"),
    "/app/fonts/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
for _p in _BOLD_FONT_SEARCH_PATHS:
    if os.path.isfile(_p):
        _RESOLVED_BOLD_FONT = _p
        break


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    if _RESOLVED_FONT:
        try:
            return ImageFont.truetype(_RESOLVED_FONT, size)
        except OSError:
            pass
    logger.warning("Using Pillow default font")
    return ImageFont.load_default()


def _build_text(config: dict) -> str:
    parts = []
    if config.get("watermark_text"):
        parts.append(config["watermark_text"])
    if config.get("use_channel_name") and config.get("title"):
        parts.append(config["title"])
    if config.get("watermark_url"):
        parts.append(config["watermark_url"])
    return " | ".join(parts) if parts else ""


def _get_position(
    canvas_w: int,
    canvas_h: int,
    element_w: int,
    element_h: int,
    position: str,
    padding: int = 30,
) -> tuple[int, int]:
    if position == "center":
        return (canvas_w - element_w) // 2, (canvas_h - element_h) // 2
    elif position == "bottom-right":
        return canvas_w - element_w - padding, canvas_h - element_h - padding
    elif position == "bottom-left":
        return padding, canvas_h - element_h - padding
    elif position == "top-right":
        return canvas_w - element_w - padding, padding
    elif position == "top-left":
        return padding, padding
    elif position == "banner":
        return (canvas_w - element_w) // 2, canvas_h - element_h - padding
    return canvas_w - element_w - padding, canvas_h - element_h - padding


def _rotate_element(img: Image.Image, angle: int) -> Image.Image:
    if angle == 0:
        return img
    return img.rotate(angle, resample=Image.BICUBIC, expand=True)


def _render_text_layer(
    text: str, font_size: int, angle: int
) -> Image.Image:
    font = _load_font(font_size)
    # Measure text
    dummy = Image.new("RGBA", (1, 1))
    draw = ImageDraw.Draw(dummy)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]

    pad = 16
    layer = Image.new("RGBA", (tw + pad * 2, th + pad * 2), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    # Strong dark outline for bold look and contrast on any background
    outline_range = max(3, font_size // 10)
    for dx in range(-outline_range, outline_range + 1):
        for dy in range(-outline_range, outline_range + 1):
            if dx == 0 and dy == 0:
                continue
            draw.text(
                (pad + dx, pad + dy), text, font=font,
                fill=(0, 0, 0, 220),
            )

    # Bright white text — fully opaque for bold visibility
    draw.text((pad, pad), text, font=font, fill=(255, 255, 255, 255))

    return _rotate_element(layer, angle)


def _render_logo_layer(
    logo_bytes: bytes, target_width: int, angle: int
) -> Image.Image:
    logo = Image.open(io.BytesIO(logo_bytes)).convert("RGBA")
    ratio = target_width / logo.width
    new_h = int(logo.height * ratio)
    logo = logo.resize((target_width, new_h), Image.LANCZOS)

    # Apply semi-transparency
    alpha = logo.split()[3]
    alpha = alpha.point(lambda p: min(p, 160))
    logo.putalpha(alpha)

    return _rotate_element(logo, angle)


def _load_bold_font(size: int) -> ImageFont.FreeTypeFont:
    if _RESOLVED_BOLD_FONT:
        try:
            return ImageFont.truetype(_RESOLVED_BOLD_FONT, size)
        except OSError:
            pass
    return _load_font(size)


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    hex_color = hex_color.lstrip("#")
    if len(hex_color) != 6:
        hex_color = "00CCFF"
    return tuple(int(hex_color[i : i + 2], 16) for i in (0, 2, 4))


def _draw_gradient(draw: ImageDraw.Draw, width: int, height: int,
                   top_color: tuple, bottom_color: tuple) -> None:
    for y in range(height):
        ratio = y / max(height - 1, 1)
        r = int(top_color[0] + (bottom_color[0] - top_color[0]) * ratio)
        g = int(top_color[1] + (bottom_color[1] - top_color[1]) * ratio)
        b = int(top_color[2] + (bottom_color[2] - top_color[2]) * ratio)
        draw.line([(0, y), (width, y)], fill=(r, g, b))


def _fit_cover(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Resize image to cover target dimensions, cropping excess."""
    img_ratio = img.width / img.height
    target_ratio = target_w / target_h
    if img_ratio > target_ratio:
        new_h = target_h
        new_w = int(target_h * img_ratio)
    else:
        new_w = target_w
        new_h = int(target_w / img_ratio)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return img.crop((left, top, left + target_w, top + target_h))


def _apply_vignette(canvas: Image.Image, strength: int = 80) -> Image.Image:
    """Darken edges with radial vignette effect."""
    w, h = canvas.size
    vig = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    vd = ImageDraw.Draw(vig)
    for i in range(strength):
        alpha = int((strength - i) * 2.5)
        margin = i * max(w, h) // (strength * 2)
        vd.rectangle([0, 0, w, margin], fill=(0, 0, 0, alpha))
        vd.rectangle([0, h - margin, w, h], fill=(0, 0, 0, alpha))
        vd.rectangle([0, 0, margin, h], fill=(0, 0, 0, alpha))
        vd.rectangle([w - margin, 0, w, h], fill=(0, 0, 0, alpha))
    return Image.alpha_composite(canvas.convert("RGBA"), vig).convert("RGB")


def _apply_gradient_orbs(canvas: Image.Image, accent: tuple) -> Image.Image:
    """Add soft accent-colored orbs for ambient depth."""
    w, h = canvas.size
    orb_layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    orb_draw = ImageDraw.Draw(orb_layer)
    positions = [(int(w * 0.2), int(h * 0.25)), (int(w * 0.8), int(h * 0.75))]
    radius = min(w, h) // 4
    for ox, oy in positions:
        for r in range(radius, 0, -2):
            alpha = int(20 * (r / radius))
            orb_draw.ellipse(
                [ox - r, oy - r, ox + r, oy + r],
                fill=(*accent, alpha),
            )
    orb_layer = orb_layer.filter(ImageFilter.GaussianBlur(radius // 3))
    return Image.alpha_composite(canvas.convert("RGBA"), orb_layer).convert("RGB")


def _draw_glow_text(canvas: Image.Image, text: str, x: int, y: int,
                    font, color: tuple, blur: int = 8, alpha: int = 120) -> Image.Image:
    """Draw text with a soft glow halo behind it."""
    glow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.text((x, y), text, fill=(*color[:3], alpha), font=font)
    glow = glow.filter(ImageFilter.GaussianBlur(blur))
    canvas = Image.alpha_composite(canvas.convert("RGBA"), glow).convert("RGB")
    draw = ImageDraw.Draw(canvas)
    draw.text((x, y), text, fill=(255, 255, 255), font=font)
    return canvas


def _draw_glow_line(canvas: Image.Image, start: tuple, end: tuple,
                    color: tuple, width: int = 2) -> Image.Image:
    """Draw accent line with soft glow behind it."""
    glow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.line([start, end], fill=(*color[:3], 80), width=width + 4)
    glow = glow.filter(ImageFilter.GaussianBlur(6))
    canvas = Image.alpha_composite(canvas.convert("RGBA"), glow).convert("RGB")
    draw = ImageDraw.Draw(canvas)
    draw.line([start, end], fill=color, width=width)
    return canvas


def _create_ai_canvas(bg_path: str, width: int, height: int, accent: tuple) -> Image.Image:
    """Build premium canvas: AI bg + dark overlay + vignette + gradient orbs."""
    from ai_background import download_ai_background
    bg_bytes = download_ai_background(bg_path)
    bg = Image.open(io.BytesIO(bg_bytes)).convert("RGB")
    bg = _fit_cover(bg, width, height)
    # Dark overlay ~60%
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 155))
    canvas = Image.alpha_composite(bg.convert("RGBA"), overlay).convert("RGB")
    canvas = _apply_vignette(canvas)
    canvas = _apply_gradient_orbs(canvas, accent)
    return canvas


def _draw_glass_panel(canvas: Image.Image, x: int, y: int, w: int, h: int,
                      accent: tuple) -> Image.Image:
    """Draw a glass-morphism panel with drop shadow."""
    canvas_rgba = canvas.convert("RGBA")
    margin = 8
    # Drop shadow
    shadow = Image.new("RGBA", (w + margin * 2 + 10, h + margin * 2 + 10), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rectangle([5, 5, shadow.width - 5, shadow.height - 5], fill=(0, 0, 0, 50))
    shadow = shadow.filter(ImageFilter.GaussianBlur(8))
    canvas_rgba.paste(shadow, (x - margin - 5, y - margin - 5), shadow)
    # Glass panel
    panel = Image.new("RGBA", (w + margin * 2, h + margin * 2), (255, 255, 255, 15))
    panel_draw = ImageDraw.Draw(panel)
    panel_draw.rectangle([0, 0, panel.width - 1, panel.height - 1],
                         outline=(*accent, 60), width=2)
    canvas_rgba.paste(panel, (x - margin, y - margin), panel)
    return canvas_rgba.convert("RGB")


def apply_template(image_bytes: bytes, config: dict) -> bytes:
    """Wrap an image in a branded template frame."""
    accent_hex = config.get("accent_color") or TEMPLATE_DEFAULT_ACCENT
    accent = _hex_to_rgb(accent_hex)
    has_ai_bg = bool(config.get("template_bg_path"))
    brand_name = config.get("brand_name") or config.get("title") or "BRAND"
    tagline = config.get("template_tagline") or ""
    stars = config.get("star_rating", 5)
    if stars is None:
        stars = 5

    # Contact info
    contacts = []
    if config.get("contact_whatsapp"):
        contacts.append(f"WA: {config['contact_whatsapp']}")
    if config.get("contact_telegram"):
        contacts.append(f"TG: {config['contact_telegram']}")
    if config.get("contact_instagram"):
        contacts.append(f"IG: {config['contact_instagram']}")
    if config.get("contact_linkedin"):
        contacts.append(f"LI: {config['contact_linkedin']}")
    contact_line = "  |  ".join(contacts)

    pad = TEMPLATE_PADDING
    cw = TEMPLATE_CANVAS_WIDTH
    inner_w = cw - pad * 2  # area for image

    # Load and scale the posted image to fit the inner width
    posted = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    scale = inner_w / posted.width
    posted_w = inner_w
    posted_h = int(posted.height * scale)
    posted = posted.resize((posted_w, posted_h), Image.LANCZOS)

    # Calculate dynamic canvas height
    header_h = TEMPLATE_HEADER_HEIGHT
    border = TEMPLATE_IMAGE_BORDER
    tagline_h = 50 if tagline else 0
    footer_h = TEMPLATE_FOOTER_HEIGHT if contact_line else 0
    line_gap = 20  # spacing for accent lines

    canvas_h = (
        pad +           # top padding
        header_h +      # header with brand + stars
        line_gap +      # accent line + gap
        border * 2 +    # image border
        posted_h +      # image
        line_gap +      # gap
        tagline_h +     # tagline
        line_gap +      # accent line + gap
        footer_h +      # contacts
        pad             # bottom padding
    )

    # Create canvas — AI background or gradient fallback
    if has_ai_bg:
        try:
            canvas = _create_ai_canvas(config["template_bg_path"], cw, canvas_h, accent)
        except Exception:
            logger.warning("Failed to load AI background, falling back to gradient")
            canvas = Image.new("RGB", (cw, canvas_h))
            draw = ImageDraw.Draw(canvas)
            _draw_gradient(draw, cw, canvas_h, TEMPLATE_BG_TOP, TEMPLATE_BG_BOTTOM)
            has_ai_bg = False
    else:
        canvas = Image.new("RGB", (cw, canvas_h))
        draw = ImageDraw.Draw(canvas)
        _draw_gradient(draw, cw, canvas_h, TEMPLATE_BG_TOP, TEMPLATE_BG_BOTTOM)

    # Fonts
    brand_font = _load_bold_font(32)
    star_font = _load_font(28)
    tagline_font = _load_font(20)
    contact_font = _load_font(16)

    y_cursor = pad

    # ── Header: Logo + Brand Name + Stars ──
    logo_offset_x = pad
    # Try to draw small logo in header
    logo_path = config.get("logo_path")
    if logo_path:
        try:
            logo_bytes = download_logo(logo_path)
            logo_img = Image.open(io.BytesIO(logo_bytes)).convert("RGBA")
            logo_size = header_h - 16
            logo_ratio = logo_size / max(logo_img.width, logo_img.height)
            lw = int(logo_img.width * logo_ratio)
            lh = int(logo_img.height * logo_ratio)
            logo_img = logo_img.resize((lw, lh), Image.LANCZOS)
            logo_y = y_cursor + (header_h - lh) // 2
            canvas.paste(logo_img, (pad, logo_y), logo_img)
            logo_offset_x = pad + lw + 12
        except Exception:
            logger.warning("Failed to load logo for template header")

    # Brand name — with glow on AI backgrounds
    draw = ImageDraw.Draw(canvas)
    brand_bbox = draw.textbbox((0, 0), brand_name.upper(), font=brand_font)
    brand_text_h = brand_bbox[3] - brand_bbox[1]
    brand_y = y_cursor + (header_h - brand_text_h) // 2
    if has_ai_bg:
        canvas = _draw_glow_text(canvas, brand_name.upper(), logo_offset_x, brand_y,
                                 brand_font, accent, blur=10, alpha=140)
        draw = ImageDraw.Draw(canvas)
    else:
        draw.text((logo_offset_x, brand_y), brand_name.upper(), font=brand_font, fill=(255, 255, 255))

    # Stars on the right — with glow on AI backgrounds
    star_text = "★" * stars + "☆" * (5 - stars)
    star_bbox = draw.textbbox((0, 0), star_text, font=star_font)
    star_w = star_bbox[2] - star_bbox[0]
    star_y = y_cursor + (header_h - (star_bbox[3] - star_bbox[1])) // 2
    if has_ai_bg:
        canvas = _draw_glow_text(canvas, star_text, cw - pad - star_w, star_y,
                                 star_font, accent, blur=6, alpha=100)
        draw = ImageDraw.Draw(canvas)
    else:
        draw.text((cw - pad - star_w, star_y), star_text, font=star_font, fill=accent)

    y_cursor += header_h

    # ── Accent line under header — with glow on AI backgrounds ──
    if has_ai_bg:
        canvas = _draw_glow_line(canvas, (pad, y_cursor), (cw - pad, y_cursor), accent)
        draw = ImageDraw.Draw(canvas)
    else:
        draw.line([(pad, y_cursor), (cw - pad, y_cursor)], fill=accent, width=2)
    y_cursor += line_gap

    # ── Framed image — glass panel on AI bg, simple border otherwise ──
    if has_ai_bg:
        canvas = _draw_glass_panel(canvas, pad, y_cursor, posted_w, posted_h, accent)
        draw = ImageDraw.Draw(canvas)
    else:
        img_x = pad - border
        img_y = y_cursor - border
        draw.rectangle(
            [img_x, img_y, img_x + posted_w + border * 2, img_y + posted_h + border * 2],
            outline=accent, width=border,
        )
    canvas.paste(posted.convert("RGB"), (pad, y_cursor))

    # Corner L-bracket accents
    img_x = pad - border
    img_y = y_cursor - border
    bracket_len = 30
    bw = 3
    corners = [
        (img_x, img_y),
        (img_x + posted_w + border * 2, img_y),
        (img_x, img_y + posted_h + border * 2),
        (img_x + posted_w + border * 2, img_y + posted_h + border * 2),
    ]
    for i, (cx, cy) in enumerate(corners):
        dx = 1 if i % 2 == 0 else -1
        dy = 1 if i < 2 else -1
        draw.line([(cx, cy), (cx + dx * bracket_len, cy)], fill=accent, width=bw)
        draw.line([(cx, cy), (cx, cy + dy * bracket_len)], fill=accent, width=bw)

    y_cursor += posted_h + border * 2 + line_gap

    # ── Tagline ──
    if tagline:
        tl_bbox = draw.textbbox((0, 0), f'"{tagline}"', font=tagline_font)
        tl_w = tl_bbox[2] - tl_bbox[0]
        draw.text(
            ((cw - tl_w) // 2, y_cursor),
            f'"{tagline}"',
            font=tagline_font,
            fill=(220, 220, 220),
        )
        y_cursor += tagline_h

    # ── Accent line above footer ──
    if contact_line:
        if has_ai_bg:
            canvas = _draw_glow_line(canvas, (pad, y_cursor), (cw - pad, y_cursor), accent)
            draw = ImageDraw.Draw(canvas)
        else:
            draw.line([(pad, y_cursor), (cw - pad, y_cursor)], fill=accent, width=2)
        y_cursor += line_gap

        # ── Contact footer ──
        ct_bbox = draw.textbbox((0, 0), contact_line, font=contact_font)
        ct_w = ct_bbox[2] - ct_bbox[0]
        draw.text(
            ((cw - ct_w) // 2, y_cursor),
            contact_line,
            font=contact_font,
            fill=accent,
        )

    output = io.BytesIO()
    canvas.save(output, format="JPEG", quality=95)
    return output.getvalue()


def apply_watermark(image_bytes: bytes, config: dict) -> bytes:
    wm_type = config.get("watermark_type", "text")

    # Route to template engine if type is template
    if wm_type == "template":
        return apply_template(image_bytes, config)

    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    w, h = img.size

    position = config.get("watermark_position", "bottom-right")
    angle = config.get("watermark_rotation", 0)

    text_layer = None
    logo_layer = None

    # Build text layer — bigger and bolder (5% of image width, min 28px)
    if wm_type in ("text", "both"):
        text = _build_text(config)
        if text:
            font_size = max(28, int(w * 0.05))
            text_layer = _render_text_layer(text, font_size, angle)

    # Build logo layer — 18% of image width for more presence
    if wm_type in ("logo", "both"):
        logo_path = config.get("logo_path")
        if logo_path:
            try:
                logo_bytes_data = download_logo(logo_path)
                target_logo_w = max(60, int(w * 0.18))
                logo_layer = _render_logo_layer(logo_bytes_data, target_logo_w, angle)
            except Exception:
                logger.exception("Failed to download/process logo")

    # Compose layers
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))

    if position == "banner":
        # Semi-transparent banner strip at bottom
        banner_h = max(60, int(h * 0.10))
        banner = Image.new("RGBA", (w, banner_h), (0, 0, 0, 140))
        overlay.paste(banner, (0, h - banner_h))

        # Place elements inside banner
        if wm_type == "both" and logo_layer and text_layer:
            gap = 16
            total_w = logo_layer.width + gap + text_layer.width
            total_h = max(logo_layer.height, text_layer.height)
            start_x = (w - total_w) // 2
            start_y = h - banner_h + (banner_h - total_h) // 2
            overlay.paste(logo_layer, (start_x, start_y), logo_layer)
            text_y = h - banner_h + (banner_h - text_layer.height) // 2
            overlay.paste(text_layer, (start_x + logo_layer.width + gap, text_y), text_layer)
        elif text_layer:
            tx = (w - text_layer.width) // 2
            ty = h - banner_h + (banner_h - text_layer.height) // 2
            overlay.paste(text_layer, (tx, ty), text_layer)
        elif logo_layer:
            lx = (w - logo_layer.width) // 2
            ly = h - banner_h + (banner_h - logo_layer.height) // 2
            overlay.paste(logo_layer, (lx, ly), logo_layer)
    else:
        if wm_type == "both" and logo_layer and text_layer:
            gap = 16
            combined_w = logo_layer.width + gap + text_layer.width
            combined_h = max(logo_layer.height, text_layer.height)
            x, y = _get_position(w, h, combined_w, combined_h, position)
            logo_y = y + (combined_h - logo_layer.height) // 2
            text_y = y + (combined_h - text_layer.height) // 2
            overlay.paste(logo_layer, (x, logo_y), logo_layer)
            overlay.paste(text_layer, (x + logo_layer.width + gap, text_y), text_layer)
        elif text_layer:
            x, y = _get_position(w, h, text_layer.width, text_layer.height, position)
            overlay.paste(text_layer, (x, y), text_layer)
        elif logo_layer:
            x, y = _get_position(w, h, logo_layer.width, logo_layer.height, position)
            overlay.paste(logo_layer, (x, y), logo_layer)

    result = Image.alpha_composite(img, overlay)
    result = result.convert("RGB")

    output = io.BytesIO()
    result.save(output, format="JPEG", quality=95)
    return output.getvalue()


def generate_sample_image() -> bytes:
    """Generate a visually appealing sample image for watermark preview."""
    w, h = 1200, 800
    img = Image.new("RGB", (w, h))
    draw = ImageDraw.Draw(img)

    # Gradient background — dark blue to teal
    for y in range(h):
        r = int(15 + (25 - 15) * y / h)
        g = int(25 + (60 - 25) * y / h)
        b = int(50 + (80 - 50) * y / h)
        draw.line([(0, y), (w, y)], fill=(r, g, b))

    # Subtle geometric shapes for visual interest
    for i in range(5):
        cx = 200 + i * 200
        cy = 300 + (i % 2) * 100
        radius = 60 + i * 15
        draw.ellipse(
            [cx - radius, cy - radius, cx + radius, cy + radius],
            outline=(255, 255, 255, 30),
            width=2,
        )

    # Horizontal accent lines
    for y_pos in [200, 500]:
        draw.line([(100, y_pos), (w - 100, y_pos)], fill=(255, 255, 255, 20), width=1)

    # Center text
    font = _load_font(36)
    text = "WATERMARK PREVIEW"
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    draw.text(
        ((w - tw) // 2, h // 2 - 20),
        text,
        font=font,
        fill=(255, 255, 255, 80),
    )

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()
