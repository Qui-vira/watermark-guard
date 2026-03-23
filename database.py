"""Supabase database operations and storage."""

import logging
from datetime import datetime, timezone
from typing import Optional
from supabase import create_client, Client
from config import SUPABASE_URL, SUPABASE_KEY

logger = logging.getLogger(__name__)

_client: Optional[Client] = None


def get_client() -> Client:
    global _client
    if _client is None:
        _client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _client


# ── Group operations ──


def upsert_group(chat_id: int, title: str) -> dict:
    client = get_client()
    data = {
        "id": chat_id,
        "title": title,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    result = client.table("wg_groups").upsert(data, on_conflict="id").execute()
    return result.data[0] if result.data else {}


def get_group(chat_id: int) -> Optional[dict]:
    client = get_client()
    result = client.table("wg_groups").select("*").eq("id", chat_id).execute()
    return result.data[0] if result.data else None


def update_group(chat_id: int, updates: dict) -> dict:
    client = get_client()
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    result = client.table("wg_groups").update(updates).eq("id", chat_id).execute()
    return result.data[0] if result.data else {}


def get_groups_for_admin(admin_id: int) -> list[dict]:
    """Get all groups where this user is tracked as having used the bot.

    We track admin association via pending_images entries or explicit group creation.
    For simplicity, we return all active groups — admin verification happens at the
    Telegram API level when commands are issued.
    """
    client = get_client()
    # Get groups where this admin has submitted images or configured the bot
    result = (
        client.table("wg_pending_images")
        .select("group_id")
        .eq("admin_id", admin_id)
        .execute()
    )
    group_ids = list({row["group_id"] for row in result.data}) if result.data else []

    if not group_ids:
        return []

    groups_result = (
        client.table("wg_groups").select("*").in_("id", group_ids).execute()
    )
    return groups_result.data if groups_result.data else []


def get_all_groups() -> list[dict]:
    client = get_client()
    result = client.table("wg_groups").select("*").execute()
    return result.data if result.data else []


# ── Pending images operations ──


def create_pending_image(
    group_id: int,
    admin_id: int,
    original_file_id: str,
    original_caption: Optional[str] = None,
) -> dict:
    client = get_client()
    data = {
        "group_id": group_id,
        "admin_id": admin_id,
        "original_file_id": original_file_id,
        "original_caption": original_caption,
        "status": "pending",
    }
    result = client.table("wg_pending_images").insert(data).execute()
    return result.data[0] if result.data else {}


def get_pending_image(image_id: str) -> Optional[dict]:
    client = get_client()
    result = client.table("wg_pending_images").select("*").eq("id", image_id).execute()
    return result.data[0] if result.data else None


def update_pending_image(image_id: str, updates: dict) -> dict:
    client = get_client()
    result = (
        client.table("wg_pending_images").update(updates).eq("id", image_id).execute()
    )
    return result.data[0] if result.data else {}


def get_expired_pending_images(cutoff_iso: str) -> list[dict]:
    client = get_client()
    result = (
        client.table("wg_pending_images")
        .select("*")
        .eq("status", "pending")
        .lt("created_at", cutoff_iso)
        .execute()
    )
    return result.data if result.data else []


# ── Logo storage operations ──


def upload_logo(group_id: int, file_bytes: bytes, filename: str) -> str:
    client = get_client()
    path = f"{group_id}/{filename}"
    # Remove old logo if exists
    try:
        client.storage.from_("logos").remove([path])
    except Exception:
        pass
    client.storage.from_("logos").upload(path, file_bytes, {"content-type": "image/png"})
    return path


def download_logo(path: str) -> bytes:
    client = get_client()
    return client.storage.from_("logos").download(path)


def delete_logo(path: str) -> None:
    client = get_client()
    try:
        client.storage.from_("logos").remove([path])
    except Exception:
        logger.warning(f"Failed to delete logo at {path}")
