"""Pillow-based watermark engine."""

import io
import logging
import os
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from config import FONT_PATH, SAMPLE_IMAGE_SIZE, SAMPLE_IMAGE_COLOR
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


def apply_watermark(image_bytes: bytes, config: dict) -> bytes:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    w, h = img.size

    wm_type = config.get("watermark_type", "text")
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
