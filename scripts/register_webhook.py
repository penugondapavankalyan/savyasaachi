"""
Register or verify the Telegram webhook.

Usage:
  python scripts/register_webhook.py --url https://xxx.lambda-url.ap-south-1.on.aws/
  python scripts/register_webhook.py --info
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import httpx


async def set_webhook(token: str, url: str) -> None:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{token}/setWebhook",
            json={
                "url": url,
                "allowed_updates": ["message", "callback_query"],
                "drop_pending_updates": True,
            },
        )
    result = resp.json()
    print("setWebhook response:", result)
    if result.get("ok"):
        print(f"✅ Webhook set to: {url}")
    else:
        print("❌ Failed:", result.get("description"))
        sys.exit(1)


async def get_info(token: str) -> None:
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"https://api.telegram.org/bot{token}/getWebhookInfo")
    print("Webhook info:", resp.json())


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram webhook manager")
    parser.add_argument("--url", help="Lambda Function URL to register as webhook")
    parser.add_argument("--info", action="store_true", help="Show current webhook info")
    args = parser.parse_args()

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("❌ TELEGRAM_BOT_TOKEN not set in environment.")
        sys.exit(1)

    if args.info:
        asyncio.run(get_info(token))
    elif args.url:
        asyncio.run(set_webhook(token, args.url))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
