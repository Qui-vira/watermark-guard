"""Configuration and environment variables."""

import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

# AI & Payment keys
FAL_KEY = os.environ.get("FAL_KEY", "")
BLOCKRADAR_API_KEY = os.environ.get("BLOCKRADAR_API_KEY", "")
BLOCKRADAR_WALLET_ID = os.environ.get("BLOCKRADAR_WALLET_ID", "")
PORT = int(os.environ.get("PORT", 8080))

# Constants
WATERMARK_POSITIONS = ["center", "bottom-right", "bottom-left", "top-right", "top-left", "banner"]
WATERMARK_TYPES = ["text", "logo", "both", "template"]
ROTATION_OPTIONS = [0, 15, 30, 45, -15, -30, -45]
APPROVAL_TIMEOUT_SECONDS = 3600  # 1 hour
RATE_LIMIT_MAX = 20  # images per minute per group
RATE_LIMIT_WINDOW = 60  # seconds
FONT_PATH = os.path.join(os.path.dirname(__file__), "fonts", "DejaVuSans.ttf")
FONT_BOLD_PATH = os.path.join(os.path.dirname(__file__), "fonts", "DejaVuSans-Bold.ttf")
SAMPLE_IMAGE_SIZE = (800, 600)
SAMPLE_IMAGE_COLOR = (30, 30, 30)

# Template constants
TEMPLATE_CANVAS_WIDTH = 1080
TEMPLATE_PADDING = 40
TEMPLATE_HEADER_HEIGHT = 80
TEMPLATE_FOOTER_HEIGHT = 60
TEMPLATE_IMAGE_BORDER = 4
TEMPLATE_BG_TOP = (12, 20, 40)       # Dark navy
TEMPLATE_BG_BOTTOM = (10, 40, 50)    # Dark teal
TEMPLATE_DEFAULT_ACCENT = "#00CCFF"
ACCENT_PRESETS = {
    "Electric Blue": "#00CCFF",
    "Gold": "#FFD700",
    "Lime": "#39FF14",
    "Red": "#FF3B3B",
    "Purple": "#A855F7",
}

# Pricing
CREDIT_PRICE_USD = 0.60
SUBSCRIPTION_PRICE_USD = 7.00
SUBSCRIPTION_CREDITS = 15
TRIAL_DAYS = 3

# AI Background themes
AI_THEMES = {
    "crypto": "cryptocurrency blockchain digital finance futuristic neon circuits",
    "luxury": "luxury premium elegant gold marble high-end fashion",
    "tech": "technology circuits digital modern minimalist silicon",
    "nature": "nature landscape mountains forest serene peaceful",
    "abstract": "abstract geometric shapes colorful modern art fluid",
    "dark": "dark moody cinematic noir shadows mysterious",
    "neon": "neon lights cyberpunk city night glow electric",
    "minimal": "minimalist clean simple elegant monochrome zen",
}
