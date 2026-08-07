"""
Local development test script.

Simulates a full Telegram conversation against a real Supabase + Upstash Redis.
Credentials are loaded from the .env file in the project root by src/config.py
automatically — no manual export needed.

Usage:
  cp .env.example .env          # fill in your real credentials
  python scripts/test_agent_local.py

Set TEST_TELEGRAM_USER_ID in .env (or the environment) to use a specific test ID.
Defaults to 99999 so it never collides with a real user.
"""

from __future__ import annotations

import asyncio
import os
import sys

# Ensure project root is on the path when running as a script
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


async def main() -> None:
    # src/config.py loads .env automatically on first import
    from src.config import settings
    from src.agent.context_loader import load_agent_context
    from src.agent.kirana_agent import get_agent
    from src.redis.upstash_client import UpstashRedisClient
    from datetime import datetime, timezone

    telegram_user_id = int(os.environ.get("TEST_TELEGRAM_USER_ID", "99999"))

    print(f"Settings: {settings}")
    print(f"Testing with telegram_user_id={telegram_user_id}")
    print("=" * 60)

    redis = UpstashRedisClient()

    # Quick connectivity check
    if not await redis.ping():
        print("❌ Upstash Redis is not reachable. Check UPSTASH_REDIS_REST_URL and TOKEN in .env")
        return

    agent = get_agent()

    test_messages = [
        "hello",
        "My shop is Ramesh General Store, GSTIN 29AABCU9603R1ZX, Karnataka",
        "add new item: Maggi 70g, branded, 12% GST, MRP 14, cost 12, reorder 20",
        "50 packets of Maggi came in",
        "make a bill: 4 Maggi, UPI",
        "confirm",
    ]

    for msg in test_messages:
        print(f"\nUser: {msg}")
        context = await load_agent_context(telegram_user_id)
        history = await redis.get_conversation(
            telegram_user_id,
            max_messages=settings.MAX_HISTORY_MESSAGES,
        )
        response = await agent.run(msg, history, context)
        print(f"Agent: {response}")

        now = datetime.now(timezone.utc).isoformat()
        await redis.append_messages(
            telegram_user_id,
            [
                {"role": "user", "content": msg, "timestamp": now},
                {"role": "assistant", "content": response, "timestamp": now},
            ],
        )


if __name__ == "__main__":
    asyncio.run(main())
