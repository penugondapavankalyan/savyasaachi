"""
Telegram update parsing utilities.

Extract fields from the raw webhook update dict without raising exceptions —
return None on missing keys.
"""

from __future__ import annotations

from typing import Optional


def get_telegram_user_id(update: dict) -> Optional[int]:
    msg = update.get("message") or update.get("edited_message")
    if msg:
        return msg.get("from", {}).get("id")
    return None


def get_chat_id(update: dict) -> Optional[int]:
    msg = update.get("message") or update.get("edited_message")
    if msg:
        return msg.get("chat", {}).get("id")
    return None


def get_message_text(update: dict) -> Optional[str]:
    msg = update.get("message")
    if msg:
        return msg.get("text")
    return None


def get_username(update: dict) -> Optional[str]:
    msg = update.get("message")
    if msg:
        return msg.get("from", {}).get("username")
    return None


def get_first_name(update: dict) -> Optional[str]:
    msg = update.get("message")
    if msg:
        return msg.get("from", {}).get("first_name")
    return None


def get_last_name(update: dict) -> Optional[str]:
    msg = update.get("message")
    if msg:
        return msg.get("from", {}).get("last_name")
    return None


def is_new_chat_command(update: dict) -> bool:
    text = get_message_text(update) or ""
    return text.strip().lower() in ("/new", "/start", "/restart")


def is_message_update(update: dict) -> bool:
    """Return True only for regular text messages (ignore channels, stickers, etc.)."""
    msg = update.get("message", {})
    return "text" in msg
