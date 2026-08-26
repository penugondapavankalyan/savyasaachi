"""
Upstash Redis HTTP client for conversation history and payment intents.

Keys managed:
  conv:{telegram_user_id}
      JSON-encoded list of message dicts, capped at 200 entries.
      TTL: 24 hours (sliding – reset on every write).

  pending_payment:{telegram_user_id}
      JSON-encoded PendingPaymentIntent dict.
      Set when an over/underpayment is detected and awaiting customer resolution.
      TTL: 30 minutes. Deleted immediately once resolved.
      Prevents LLM from hallucinating amounts between turns.

  rate:{telegram_user_id}
      Integer counter of messages sent in the current 60-second window.
      TTL: 60 seconds (set on first hit, not reset on subsequent hits).
      Used by is_rate_limited() to enforce per-user message rate limits.

Uses the Upstash pipeline endpoint for atomic SET + EX in one HTTP call.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from src.config import settings

# ── Injection pattern guard (history poisoning) ───────────────────────────────
# Used in get_conversation() to strip messages that contain prompt-injection
# patterns before they are passed to the agent as conversation history.
_INJECTION_RE = re.compile(
    r"ignore (all |previous |prior |above |your )?(instructions?|rules?|prompt|guidelines?)|"
    r"you are now|disregard|pretend (you are|to be)|act as [a-z]|jailbreak|"
    r"system prompt|forget (everything|all)|new role|override (your|all|the)|"
    r"do not follow|stop being|change your (role|behaviour|behavior)",
    re.IGNORECASE,
)


def _contains_injection(text: str) -> bool:
    """Return True if *text* contains a known prompt-injection pattern."""
    if not text:
        return False
    return bool(_INJECTION_RE.search(text))


class UpstashRedisClient:
    """Thin async wrapper around the Upstash REST API."""

    MAX_STORED_MESSAGES = 200
    TTL_SECONDS = 86_400           # 24 hours — conversation history
    PAYMENT_INTENT_TTL = 1_800     # 30 minutes — pending payment intents
    RATE_WINDOW_SECONDS = 60       # sliding window for rate limiting
    RATE_LIMIT_MAX = 20            # max messages per user per window

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
        """Return the last *max_messages* messages for a user.

        History poisoning guard: any message whose content contains a known
        prompt-injection pattern is stripped before being passed to the agent.
        This prevents a malicious message that slipped into Redis history from
        influencing future turns.
        """
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

        # Guard: filter out non-dict items (corrupted history)
        messages = [m for m in messages if isinstance(m, dict)]
        # Guard: strip any message whose content contains injection patterns
        messages = [m for m in messages if not _contains_injection(m.get("content", ""))]
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

    # ------------------------------------------------------------------
    # Pending payment intent (over/underpayment resolution)
    # ------------------------------------------------------------------

    async def set_pending_payment(
        self,
        telegram_user_id: int,
        intent: dict,
    ) -> None:
        """
        Store a pending payment intent for 30 minutes.

        Called when confirm_payment detects over/underpayment and needs
        to remember the delta amount until the next turn resolves it.
        The intent dict matches PendingPaymentIntent fields.
        """
        key = f"pending_payment:{telegram_user_id}"
        value = json.dumps(intent)
        pipeline_payload = [
            ["SET", key, value, "EX", str(self.PAYMENT_INTENT_TTL)]
        ]
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                f"{self.base_url}/pipeline",
                headers=self.headers,
                json=pipeline_payload,
            )

    async def get_pending_payment(
        self,
        telegram_user_id: int,
    ) -> dict | None:
        """
        Retrieve the pending payment intent for a user.
        Returns None if not set or expired.
        """
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{self.base_url}/get/pending_payment:{telegram_user_id}",
                headers=self.headers,
            )
        result = resp.json().get("result")
        if not result:
            return None
        try:
            return json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return None

    async def clear_pending_payment(self, telegram_user_id: int) -> None:
        """
        Delete the pending payment intent.
        Called once the over/underpayment is fully resolved.
        """
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.get(
                f"{self.base_url}/del/pending_payment:{telegram_user_id}",
                headers=self.headers,
            )

    async def is_rate_limited(self, telegram_user_id: int) -> bool:
        """
        Return True if the user has exceeded RATE_LIMIT_MAX messages in the
        current RATE_WINDOW_SECONDS window.

        Uses a Redis INCR + EXPIRE pattern:
          - INCR atomically increments (and creates) the counter.
          - EXPIRE is only set when count == 1 (first hit in the window)
            so subsequent increments within the window don't reset the TTL.
        Silently returns False on any Redis error so rate-limit failures
        never block legitimate messages.
        """
        key = f"rate:{telegram_user_id}"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                # INCR the counter
                incr_resp = await client.post(
                    f"{self.base_url}/pipeline",
                    headers=self.headers,
                    json=[["INCR", key]],
                )
            results = incr_resp.json()
            # results is a list of per-command results: [{"result": <count>}]
            count = results[0].get("result", 0) if isinstance(results, list) else 0

            # Set TTL only on the first hit so the window is fixed, not sliding
            if count == 1:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    await client.post(
                        f"{self.base_url}/pipeline",
                        headers=self.headers,
                        json=[["EXPIRE", key, str(self.RATE_WINDOW_SECONDS)]],
                    )

            return int(count) > self.RATE_LIMIT_MAX
        except Exception:
            return False  # degrade gracefully — never block on Redis error

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
