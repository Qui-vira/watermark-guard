"""Pillow-based watermark engine."""

import io
import logging
import math
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from config import FONT_PATH, SAMPLE_IMAGE_SIZE, SAMPLE_IMAGE_COLOR
from database import download_logo

logger = logging.getLogger(__name__)


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except OSError:
        logger.warning("DejaVuSans.ttf not found, falling back to default font")
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
    padding: int = 20,
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

    pad = 10
    layer = Image.new("RGBA", (tw + pad * 2, th + pad * 2), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    # Dark shadow/outline for contrast
    for dx in (-2, -1, 0, 1, 2):
        for dy in (-2, -1, 0, 1, 2):
            if dx == 0 and dy == 0:
                continue
            draw.text((pad + dx, pad + dy), text, font=font, fill=(0, 0, 0, 160))

    # Semi-transparent white text
    draw.text((pad, pad), text, font=font, fill=(255, 255, 255, 200))

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
    alpha = alpha.point(lambda p: min(p, 128))
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

    # Build text layer
    if wm_type in ("text", "both"):
        text = _build_text(config)
        if text:
            font_size = max(16, int(w * 0.03))
            text_layer = _render_text_layer(text, font_size, angle)

    # Build logo layer
    if wm_type in ("logo", "both"):
        logo_path = config.get("logo_path")
        if logo_path:
            try:
                logo_bytes_data = download_logo(logo_path)
                target_logo_w = max(40, int(w * 0.15))
                logo_layer = _render_logo_layer(logo_bytes_data, target_logo_w, angle)
            except Exception:
                logger.exception("Failed to download/process logo")

    # Compose layers
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))

    if position == "banner":
        # Draw semi-transparent banner strip at bottom
        banner_h = max(50, int(h * 0.08))
        banner = Image.new("RGBA", (w, banner_h), (0, 0, 0, 120))
        overlay.paste(banner, (0, h - banner_h))

        # Place elements inside banner
        if wm_type == "both" and logo_layer and text_layer:
            total_w = logo_layer.width + 10 + text_layer.width
            total_h = max(logo_layer.height, text_layer.height)
            start_x = (w - total_w) // 2
            start_y = h - banner_h + (banner_h - total_h) // 2
            overlay.paste(logo_layer, (start_x, start_y), logo_layer)
            text_y = h - banner_h + (banner_h - text_layer.height) // 2
            overlay.paste(text_layer, (start_x + logo_layer.width + 10, text_y), text_layer)
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
            # Logo on the left, text to the right
            combined_w = logo_layer.width + 10 + text_layer.width
            combined_h = max(logo_layer.height, text_layer.height)
            x, y = _get_position(w, h, combined_w, combined_h, position)
            logo_y = y + (combined_h - logo_layer.height) // 2
            text_y = y + (combined_h - text_layer.height) // 2
            overlay.paste(logo_layer, (x, logo_y), logo_layer)
            overlay.paste(text_layer, (x + logo_layer.width + 10, text_y), text_layer)
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
    img = Image.new("RGB", SAMPLE_IMAGE_SIZE, SAMPLE_IMAGE_COLOR)
    draw = ImageDraw.Draw(img)
    # Draw a simple grid pattern for visual reference
    for x in range(0, SAMPLE_IMAGE_SIZE[0], 80):
        draw.line([(x, 0), (x, SAMPLE_IMAGE_SIZE[1])], fill=(50, 50, 50), width=1)
    for y in range(0, SAMPLE_IMAGE_SIZE[1], 80):
        draw.line([(0, y), (SAMPLE_IMAGE_SIZE[0], y)], fill=(50, 50, 50), width=1)
    font = _load_font(24)
    draw.text(
        (SAMPLE_IMAGE_SIZE[0] // 2 - 80, SAMPLE_IMAGE_SIZE[1] // 2 - 12),
        "Sample Image",
        font=font,
        fill=(100, 100, 100),
    )
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()
