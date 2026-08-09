"""
Upstash Redis HTTP client for conversation history.

Each Telegram user has one key:  conv:{telegram_user_id}
Value: JSON-encoded list of message dicts, capped at 200 entries.
TTL: 24 hours (sliding – reset on every write).

Uses the Upstash pipeline endpoint for atomic SET + EX in one HTTP call.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from src.config import settings


class UpstashRedisClient:
    """Thin async wrapper around the Upstash REST API."""

    MAX_STORED_MESSAGES = 200
    TTL_SECONDS = 86_400  # 24 hours

    def __init__(self) -> None:
        self.base_url = settings.UPSTASH_REDIS_REST_URL.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {settings.UPSTASH_REDIS_REST_TOKEN}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_conversation(
        self,
        telegram_user_id: int,
        max_messages: int = 20,
    ) -> list[dict[str, Any]]:
        """Return the last *max_messages* messages for a user."""
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{self.base_url}/get/conv:{telegram_user_id}",
                headers=self.headers,
            )
        result = resp.json().get("result")
        if not result:
            return []
        try:
            messages = json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return []

        # Guard: filter out any non-dict items (from corrupted history)
        messages = [m for m in messages if isinstance(m, dict)]
        return messages[-max_messages:]

    async def append_messages(
        self,
        telegram_user_id: int,
        new_messages: list[dict[str, Any]],
    ) -> None:
        """Append *new_messages* to the stored history and reset TTL."""
        existing = await self._get_all(telegram_user_id)
        updated = existing + new_messages
        if len(updated) > self.MAX_STORED_MESSAGES:
            updated = updated[-self.MAX_STORED_MESSAGES:]
        await self._save(telegram_user_id, updated)

    async def clear_conversation(self, telegram_user_id: int) -> None:
        """/new chat — delete the conversation key."""
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.get(
                f"{self.base_url}/del/conv:{telegram_user_id}",
                headers=self.headers,
            )

    async def ping(self) -> bool:
        """Health-check the Redis endpoint."""
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{self.base_url}/ping",
                headers=self.headers,
            )
        return resp.json().get("result") == "PONG"

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _get_all(self, telegram_user_id: int) -> list[dict[str, Any]]:
        """Retrieve full stored history (no window) for internal use."""
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{self.base_url}/get/conv:{telegram_user_id}",
                headers=self.headers,
            )
        result = resp.json().get("result")
        if not result:
            return []
        try:
            messages = json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return []
        # Guard: filter out any non-dict items
        return [m for m in messages if isinstance(m, dict)]

    async def _save(
        self,
        telegram_user_id: int,
        messages: list[dict[str, Any]],
    ) -> None:
        """
        Persist messages with a sliding TTL using the Upstash pipeline endpoint.

        Upstash REST pipeline format:
            POST /pipeline
            body: [["SET", "key", "value", "EX", "seconds"], ...]

        This is the correct way to do SET + EX atomically.
        The old approach of posting ["<json>", "EX", "86400"] to /set/key
        was wrong — Upstash was treating each array element as a separate command.
        """
        key = f"conv:{telegram_user_id}"
        value = json.dumps(messages)
        pipeline_payload = [
            ["SET", key, value, "EX", str(self.TTL_SECONDS)]
        ]
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                f"{self.base_url}/pipeline",
                headers=self.headers,
                json=pipeline_payload,
            )
