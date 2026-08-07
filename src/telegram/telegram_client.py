"""
Telegram HTTP client.

Thin async wrapper around the Telegram Bot API (sendMessage, sendDocument,
sendChatAction, setWebhook).
"""

from __future__ import annotations

from typing import Optional

import httpx

from src.config import settings


class TelegramClient:
    def __init__(self) -> None:
        self.token = settings.TELEGRAM_BOT_TOKEN
        self.base_url = f"https://api.telegram.org/bot{self.token}"

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str = "Markdown",
        reply_to_message_id: Optional[int] = None,
    ) -> dict:
        """Send a text message."""
        payload: dict = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
        }
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(f"{self.base_url}/sendMessage", json=payload)
        return resp.json()

    async def send_document(
        self,
        chat_id: int,
        file_path: str,
        caption: Optional[str] = None,
    ) -> dict:
        """Send a file (PDF or PPTX)."""
        async with httpx.AsyncClient(timeout=60.0) as client:
            with open(file_path, "rb") as f:
                resp = await client.post(
                    f"{self.base_url}/sendDocument",
                    data={"chat_id": str(chat_id), "caption": caption or ""},
                    files={"document": f},
                )
        return resp.json()

    async def send_typing_action(self, chat_id: int) -> None:
        """Show the typing indicator."""
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                f"{self.base_url}/sendChatAction",
                json={"chat_id": chat_id, "action": "typing"},
            )

    async def set_webhook(self, webhook_url: str) -> dict:
        """Register a URL as the Telegram webhook."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{self.base_url}/setWebhook",
                json={
                    "url": webhook_url,
                    "allowed_updates": ["message", "callback_query"],
                    "drop_pending_updates": True,
                },
            )
        return resp.json()

    async def get_webhook_info(self) -> dict:
        """Return current webhook configuration."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{self.base_url}/getWebhookInfo")
        return resp.json()


# ------------------------------------------------------------------
# Module-level singleton
# ------------------------------------------------------------------

_telegram: TelegramClient | None = None


def get_telegram_client() -> TelegramClient:
    global _telegram
    if _telegram is None:
        _telegram = TelegramClient()
    return _telegram
