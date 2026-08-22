"""
Lambda handler — entry point for all Telegram webhook requests.

Flow per invocation:
  1. Parse Telegram update
  2. Handle /new, /start commands directly (no agent)
  3. Show typing indicator
  4. Ensure user record exists (idempotent)
  5. Load pre-agent context (workflow state, store details)
  6. Load conversation history from Upstash Redis
  7. Run PydanticAI agent
  8. Save updated history to Redis
  9. Send response to Telegram
  10. (If reorder alerts triggered) send proactive notifications
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone

from src.agent.context_loader import load_agent_context
from src.agent.kirana_agent import get_agent
from src.config import settings
from src.mcp import get_mcp_instances
from src.redis.upstash_client import UpstashRedisClient
from src.telegram.telegram_client import get_telegram_client
from src.telegram.update_parser import (
    get_chat_id,
    get_first_name,
    get_last_name,
    get_message_text,
    get_telegram_user_id,
    get_username,
    is_message_update,
    is_new_chat_command,
)
from src.utils.scope_guard import (
    OFF_TOPIC_REPLY,
    STALE_DRAFT_REPLY,
    is_in_scope,
    is_stale_draft_greeting,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ── Pre-LLM input guards ──────────────────────────────────────────────────────

# Guard #1 — message length cap
_MAX_MESSAGE_LENGTH = 500

# Guard #3 — prompt injection pattern filter (pre-LLM, on raw user input)
# Kept intentionally tight to avoid false positives on legitimate store messages.
# History-stored injection patterns are handled separately in upstash_client.py.
_INJECTION_RE = re.compile(
    r"ignore (all |previous |prior |above |your )?(instructions?|rules?|prompt|guidelines?)|"
    r"you are now|disregard|pretend (you are|to be)|act as [a-z]|jailbreak|"
    r"system prompt|forget (everything|all)|new role|override (your|all|the)|"
    r"do not follow|stop being|change your (role|behaviour|behavior)",
    re.IGNORECASE,
)

# ──────────────────────────────────────────────────────────────────────────────
# Async handler
# ──────────────────────────────────────────────────────────────────────────────

async def async_handler(event: dict, context) -> dict:
    """Main async Lambda handler."""

    # 1. Parse webhook payload
    try:
        body = json.loads(event.get("body") or "{}")
    except (json.JSONDecodeError, TypeError):
        return {"statusCode": 400, "body": "Invalid JSON"}

    if not body or not is_message_update(body):
        return {"statusCode": 200, "body": "OK"}

    telegram_user_id = get_telegram_user_id(body)
    chat_id = get_chat_id(body)
    user_message = get_message_text(body)

    if not telegram_user_id or not chat_id or not user_message:
        return {"statusCode": 200, "body": "OK"}

    telegram = get_telegram_client()
    redis = UpstashRedisClient()
    mcps = get_mcp_instances()

    # ── Pre-LLM input guards (before any agent / DB work) ─────────────────────

    # Guard #1 — message length cap
    if len(user_message) > _MAX_MESSAGE_LENGTH:
        await telegram.send_message(
            chat_id,
            f"Message too long (max {_MAX_MESSAGE_LENGTH} characters). Please keep it concise.",
        )
        return {"statusCode": 200, "body": "OK"}

    # Guard #3 — prompt injection filter
    if _INJECTION_RE.search(user_message):
        await telegram.send_message(chat_id, OFF_TOPIC_REPLY)
        return {"statusCode": 200, "body": "OK"}

    # Guard #2 — off-topic scope filter (allowlist-based, skips LLM entirely)
    if not is_in_scope(user_message):
        await telegram.send_message(chat_id, OFF_TOPIC_REPLY)
        return {"statusCode": 200, "body": "OK"}

    # Guard #4 — per-user rate limit (20 messages / 60 seconds)
    if await redis.is_rate_limited(telegram_user_id):
        await telegram.send_message(
            chat_id,
            "Too many messages. Please wait a moment before trying again.",
        )
        return {"statusCode": 200, "body": "OK"}

    # 2. /new  /start  /restart — clear history, no agent invocation
    if is_new_chat_command(body):
        await redis.clear_conversation(telegram_user_id)

        # Cancel any open draft bill
        try:
            workflow = await mcps.identity.get_workflow_state(telegram_user_id)
            if workflow and workflow.active_draft_bill_id:
                await mcps.billing.cancel_draft_bill(workflow.active_draft_bill_id)
        except Exception:
            pass

        await telegram.send_message(
            chat_id,
            (
                "🆕 *Chat cleared!*\n\n"
                "Your store data, products, inventory, bills and preferences are all intact.\n\n"
                "What would you like to do?"
            ),
        )
        return {"statusCode": 200, "body": "OK"}

    # 3. Show typing indicator (fire-and-forget)
    try:
        await telegram.send_typing_action(chat_id)
    except Exception:
        pass

    # 4. Ensure user record exists (idempotent — safe on every message)
    try:
        await mcps.identity.register_user(
            telegram_user_id=telegram_user_id,
            telegram_username=get_username(body),
            first_name=get_first_name(body),
            last_name=get_last_name(body),
        )
    except Exception as exc:
        logger.warning("register_user failed: %s", exc)

    # 5. Load pre-agent context
    try:
        store_context = await load_agent_context(telegram_user_id)
    except Exception as exc:
        logger.error("load_agent_context failed: %s", exc)
        await telegram.send_message(
            chat_id, "⚠️ Having trouble connecting. Please try again."
        )
        return {"statusCode": 200, "body": "OK"}

    # 6. Load conversation history
    history = await redis.get_conversation(
        telegram_user_id,
        max_messages=settings.MAX_HISTORY_MESSAGES,
    )

    # 6b. Stale-draft greeting interceptor — bypass LLM entirely
    # If there is an active draft AND the message is a bare greeting, send the
    # fixed keyword-menu reply and save it to history so the next turn has
    # correct context (the model will see its own reply and act accordingly).
    if is_stale_draft_greeting(user_message, bool(store_context.active_draft_bill_id)):
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            await redis.append_messages(
                telegram_user_id,
                [
                    {"role": "user", "content": user_message, "timestamp": now_iso},
                    {"role": "assistant", "content": STALE_DRAFT_REPLY, "timestamp": now_iso},
                ],
            )
        except Exception as exc:
            logger.warning("Redis append (stale-draft intercept) failed: %s", exc)
        await telegram.send_message(chat_id, STALE_DRAFT_REPLY)
        return {"statusCode": 200, "body": "OK"}

    # 7. Run agent
    try:
        agent = get_agent()
        response_text = await agent.run(user_message, history, store_context)
    except Exception as exc:
        logger.error("Agent error: %s", exc, exc_info=True)
        response_text = (
            "⚠️ I ran into an error. Please try again or send /new to reset the chat."
        )

    # 8. Save updated history
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        await redis.append_messages(
            telegram_user_id,
            [
                {"role": "user", "content": user_message, "timestamp": now_iso},
                {"role": "assistant", "content": response_text, "timestamp": now_iso},
            ],
        )
    except Exception as exc:
        logger.warning("Redis append failed: %s", exc)

    # 9. Send response
    try:
        await telegram.send_message(chat_id, response_text)
    except Exception as exc:
        logger.error("Failed to send Telegram message: %s", exc)

    return {"statusCode": 200, "body": "OK"}


# ──────────────────────────────────────────────────────────────────────────────
# Synchronous Lambda entry point
# ──────────────────────────────────────────────────────────────────────────────

def lambda_handler(event: dict, context) -> dict:
    """
    Synchronous entry point required by AWS Lambda.
    Delegates to the async handler via the running event loop.
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(async_handler(event, context))
    except Exception as exc:
        logger.error("Unhandled Lambda error: %s", exc, exc_info=True)
        return {"statusCode": 500, "body": "Internal Server Error"}
