"""fal.ai AI background generation for premium templates."""

import io
import logging
import time
import requests
from config import FAL_KEY, AI_THEMES
from database import get_client

logger = logging.getLogger(__name__)

FAL_SUBMIT_URL = "https://queue.fal.run/fal-ai/nano-banana-pro"


def generate_ai_background(theme_key: str, accent_hex: str) -> bytes:
    """Generate a cinematic background via fal.ai. Returns image bytes.

    Raises Exception on failure — caller handles fallback.
    """
    theme_words = AI_THEMES.get(theme_key, "abstract modern dark")
    prompt = (
        f"Cinematic dark {theme_words} themed background, "
        f"moody atmospheric lighting, volumetric light, "
        f"accent color hints of {accent_hex}, "
        f"professional photography backdrop, ultra detailed, 8k quality, "
        f"dark moody atmosphere, no text, no words, no letters, no watermark"
    )
    negative_prompt = (
        "text, words, letters, numbers, watermark, logo, blurry, "
        "low quality, cartoon, anime, distorted, bright cheerful colors, "
        "flat lighting, generic stock photo"
    )

    headers = {
        "Authorization": f"Key {FAL_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "image_size": {"width": 1080, "height": 1920},
        "num_images": 1,
        "guidance_scale": 7.5,
        "num_inference_steps": 30,
    }

    # Submit to queue
    resp = requests.post(FAL_SUBMIT_URL, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    request_id = resp.json()["request_id"]
    logger.info(f"fal.ai submitted: {request_id}")

    # Poll for completion
    status_url = f"{FAL_SUBMIT_URL}/requests/{request_id}/status"
    for _ in range(60):
        time.sleep(3)
        status_resp = requests.get(status_url, headers=headers, timeout=15)
        status_data = status_resp.json()
        if status_data.get("status") == "COMPLETED":
            break
    else:
        raise TimeoutError("fal.ai generation timed out")

    # Get result
    result_url = f"{FAL_SUBMIT_URL}/requests/{request_id}"
    result = requests.get(result_url, headers=headers, timeout=30).json()
    image_url = result["images"][0]["url"]

    # Download image
    img_resp = requests.get(image_url, timeout=60)
    img_resp.raise_for_status()
    logger.info(f"AI background downloaded: {len(img_resp.content)} bytes")
    return img_resp.content


def upload_ai_background(group_id: int, image_bytes: bytes) -> str:
    """Upload AI background to Supabase 'backgrounds' bucket."""
    client = get_client()
    path = f"{group_id}/ai_bg.jpg"
    try:
        client.storage.from_("backgrounds").remove([path])
    except Exception:
        pass
    client.storage.from_("backgrounds").upload(
        path, image_bytes, {"content-type": "image/jpeg"}
    )
    return path


def download_ai_background(path: str) -> bytes:
    """Download AI background from Supabase storage."""
    client = get_client()
    return client.storage.from_("backgrounds").download(path)
