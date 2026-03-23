"""Configuration and environment variables."""

import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

# Constants
WATERMARK_POSITIONS = ["center", "bottom-right", "bottom-left", "top-right", "top-left", "banner"]
WATERMARK_TYPES = ["text", "logo", "both"]
ROTATION_OPTIONS = [0, 15, 30, 45, -15, -30, -45]
APPROVAL_TIMEOUT_SECONDS = 3600  # 1 hour
RATE_LIMIT_MAX = 20  # images per minute per group
RATE_LIMIT_WINDOW = 60  # seconds
FONT_PATH = os.path.join(os.path.dirname(__file__), "fonts", "DejaVuSans.ttf")
SAMPLE_IMAGE_SIZE = (800, 600)
SAMPLE_IMAGE_COLOR = (30, 30, 30)
