"""
run_local.py — Interactive local development REPL for the Kirana Agent.

This is the ONE file you run to develop and test locally.
It simulates the full Lambda → Agent → Supabase → Redis pipeline
in a terminal chat loop — no Telegram, no deployment needed.

Usage:
  # From project root, after filling in .env:
  python run_local.py

  # Use a specific fake user ID (so you can test multiple users):
  python run_local.py --user-id 12345

Commands inside the chat:
  /new      — Clear conversation history (same as Telegram /new)
  /status   — Show current workflow state and store context
  /history  — Print the current Redis conversation history
  /debug    — Show full tool call trace from the last agent run
  /quit     — Exit the REPL

How it mirrors production:
  - .env is loaded via src/config.py (same as Lambda)
  - Every message goes through the full handler pipeline
  - Supabase state persists between runs (real DB)
  - Redis history persists between runs (real Redis)
  - Same agent, same tools, same system prompt
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import traceback
from datetime import datetime, timezone

# Make sure project root is importable regardless of where you run from
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import re as _re
from src.utils.scope_guard import (
    CATALOGUE_GATE_REPLY,
    OFF_TOPIC_REPLY,
    REGISTRATION_GATE_REPLY,
    STALE_DRAFT_REPLY,
    is_in_scope,
    is_pending_catalogue_block,
    is_stale_draft_greeting,
    is_unregistered_block,
)

_INJECTION_RE_LOCAL = _re.compile(
    r"ignore (all |previous |prior |above |your )?(instructions?|rules?|prompt|guidelines?)|"
    r"you are now|disregard|pretend (you are|to be)|act as [a-z]|jailbreak|"
    r"system prompt|forget (everything|all)|new role|override (your|all|the)|"
    r"do not follow|stop being|change your (role|behaviour|behavior)",
    _re.IGNORECASE,
)

# ── Logging setup ─────────────────────────────────────────────────────────────
# In run_local, we show INFO+ so tool audit logs are visible in the terminal.
logging.basicConfig(
    level=logging.WARNING,           # suppress noisy library logs
    format="%(levelname)s  %(name)s  %(message)s",
    stream=sys.stderr,
)
# Always show our audit logger at INFO regardless of root level
_audit_logger = logging.getLogger("kirana.tool_audit")
_audit_logger.setLevel(logging.INFO)
_audit_logger.propagate = False
_audit_handler = logging.StreamHandler(sys.stderr)
_audit_handler.setFormatter(logging.Formatter("  AUDIT  %(message)s"))
_audit_logger.addHandler(_audit_handler)


async def repl(telegram_user_id: int) -> None:
    # src/config.py loads .env automatically
    from src.config import settings
    from src.agent.context_loader import load_agent_context
    from src.agent.kirana_agent import get_agent
    from src.mcp import get_mcp_instances
    from src.redis.upstash_client import UpstashRedisClient
    from src.telegram.telegram_client import get_telegram_client

    print()
    print("=" * 60)
    print("  Kirana Agent — Local REPL")
    print(f"  User ID : {telegram_user_id}")
    print(f"  LLM     : {settings.LLM_PROVIDER} / {settings.LLM_MODEL if settings.LLM_PROVIDER=="groq" else settings.OLLAMA_MODEL}")
    print(f"  Env     : {settings.LAMBDA_ENV}")
    print("=" * 60)
    print("  Type your message and press Enter.")
    print("  Commands: /new  /status  /history  /debug  /sendtg  /quit")
    print("  Tool calls are logged to stderr automatically.")
    print("  /sendtg sends the last agent reply to your real Telegram chat")
    print("  (requires TELEGRAM_TEST_CHAT_ID in .env) to validate MarkdownV2 rendering.")
    print("=" * 60)
    print()
    print("  (Local mode: Telegram profile is not available)")
    print("  Enter your name for this session (simulates Telegram first_name):")
    try:
        local_first_name = input("  Your first name: ").strip() or "LocalDev"
        local_last_name_raw = input("  Your last name (optional, press Enter to skip): ").strip()
        local_last_name = local_last_name_raw if local_last_name_raw else None
    except (KeyboardInterrupt, EOFError):
        print("\nBye!")
        return
    print()

    redis = UpstashRedisClient()
    mcps = get_mcp_instances()
    agent = get_agent()

    # ── Step 1: Ensure user record exists BEFORE any connectivity checks.
    #
    # This mirrors what handler.py does in production: the Telegram webhook
    # always provides first_name/last_name/username so we register the user
    # (idempotent) before touching any other system.
    #
    # Doing this ONCE at startup (not every message loop iteration) is correct
    # because:
    #   a) It is idempotent — safe to call on every app start.
    #   b) It does NOT interfere with the LLM's own register_user calls during
    #      the UNREGISTERED flow — those are for store setup, not user creation.
    #   c) The context loader (called inside the message loop) will then find
    #      the user record and the self-heal logic can work correctly.
    #
    # IMPORTANT: Do NOT call register_user again inside the message loop.
    # Calling it every turn previously caused a second user row to be created
    # when the LLM also called register_user with a different ID, resulting in
    # duplicate users and a broken workflow_state.
    try:
        reg_result = await mcps.identity.register_user(
            telegram_user_id=telegram_user_id,
            first_name=local_first_name,
            last_name=local_last_name,
        )
        if reg_result.already_existed:
            print(f"  Welcome back! (user {telegram_user_id} already registered)")
        else:
            print(f"  New user record created for {local_first_name} (ID: {telegram_user_id})")
    except Exception as e:
        print(f"  WARNING: Could not ensure user record: {e}")
    print()

    # ── connectivity checks ──────────────────────────────────────────
    print("Checking connections...")
    try:
        if not await redis.ping():
            print("  Redis unreachable — check UPSTASH_REDIS_REST_URL / TOKEN in .env")
            return
        print("  ✅  Upstash Redis")
    except Exception as e:
        print(f"  ❌  Upstash Redis: {e}")
        return

    try:
        # Light Supabase check — just read workflow state (read-only, no upsert)
        ws = await mcps.identity.get_workflow_state(telegram_user_id)
        print(f"  ✅  Supabase  (workflow_state={ws.current_state})")
    except Exception as e:
        print(f"  ❌  Supabase: {e}")
        return

    print()

    # Stores the all_messages() list from the most recent agent run (for /debug)
    _last_run_messages: list = []
    # Stores the most recent agent reply text (for /sendtg)
    _last_agent_reply: str | None = None

    # ── main loop ────────────────────────────────────────────────────
    while True:
        try:
            user_input = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nBye!")
            break

        if not user_input:
            continue

        # ── built-in commands ────────────────────────────────────────

        if user_input.lower() == "/quit":
            print("Bye!")
            break

        if user_input.lower() == "/new":
            await redis.clear_conversation(telegram_user_id)
            try:
                workflow = await mcps.identity.get_workflow_state(telegram_user_id)
                if workflow.active_draft_bill_id:
                    await mcps.billing.cancel_draft_bill(workflow.active_draft_bill_id)
            except Exception:
                pass
            print("Agent: Chat cleared! Your store data and preferences are intact.")
            continue

        if user_input.lower() == "/status":
            try:
                context = await load_agent_context(telegram_user_id)
                print()
                print("── Workflow State ──────────────────────────────────────")
                print(f"  State     : {context.workflow_state}")
                print(f"  Store     : {context.shop_name or '(not set)'}")
                print(f"  Store ID  : {context.store_id or '(none)'}")
                print(f"  User ID   : {context.user_id or '(none)'}")
                print(f"  GSTIN     : {context.gstin or '(none)'}")
                print(f"  State code: {context.state_code}  ({context.state_name})")
                print(f"  Payment   : {context.default_payment_mode}")
                print(f"  Draft bill: {context.active_draft_bill_id or '(none)'}")
                print(f"  Prefs     : {context.preferences or '{}'}")
                print("────────────────────────────────────────────────────────")
            except Exception as e:
                print(f"  ERROR: Could not load status: {e}")
            print()
            continue

        if user_input.lower() == "/history":
            history = await redis.get_conversation(telegram_user_id, max_messages=999)
            if not history:
                print("  (no conversation history)")
            else:
                print()
                print(f"── Conversation history ({len(history)} messages) ──────────────")
                for msg in history:
                    role = msg.get("role", "?").upper()
                    content = msg.get("content", "")
                    ts = msg.get("timestamp", "")[:19]
                    print(f"  [{ts}] {role}: {content[:120]}")
                print("────────────────────────────────────────────────────────")
            print()
            continue

        if user_input.lower() == "/sendtg":
            # Send the last agent reply to a real Telegram chat to validate
            # actual MarkdownV2 rendering. Dev-only: reads TELEGRAM_TEST_CHAT_ID
            # from .env. This code path lives ONLY in run_local.py (never
            # deployed to Lambda) and is never read by production handler.py.
            if not _last_agent_reply:
                print("  (no agent reply yet — send a message first)")
            elif not settings.TELEGRAM_TEST_CHAT_ID:
                print("  ERROR: TELEGRAM_TEST_CHAT_ID is not set in .env.")
                print("  Add TELEGRAM_TEST_CHAT_ID=<your real Telegram chat id> to .env and restart.")
            else:
                try:
                    test_chat_id = int(settings.TELEGRAM_TEST_CHAT_ID)
                    telegram = get_telegram_client()
                    result = await telegram.send_message(test_chat_id, _last_agent_reply)
                    if result.get("ok"):
                        print(f"  ✅  Sent to Telegram chat {test_chat_id}. Check your app.")
                    else:
                        print(f"  ❌  Telegram API returned an error: {result.get('description', result)}")
                except ValueError:
                    print("  ERROR: TELEGRAM_TEST_CHAT_ID must be a numeric chat id.")
                except Exception as e:
                    print(f"  ERROR: Failed to send to Telegram: {e}")
            print()
            continue

        if user_input.lower() == "/debug":
            # Show full message trace from the last agent run
            if not _last_run_messages:
                print("  (no run yet — send a message first)")
            else:
                from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
                print()
                print("── Last run message trace ──────────────────────────────")
                for i, msg in enumerate(_last_run_messages):
                    print(f"  [{i}] {type(msg).__name__}")
                    for part in getattr(msg, "parts", []):
                        ptype = type(part).__name__
                        if isinstance(part, ToolCallPart):
                            print(f"       TOOL_CALL  {part.tool_name}  args={str(part.args)[:200]}")
                        elif isinstance(part, ToolReturnPart):
                            result_str = str(part.content)
                            print(f"       TOOL_RETURN {part.tool_name}  len={len(result_str)}  {result_str[:200]}")
                        else:
                            content = getattr(part, "content", "")
                            print(f"       {ptype}: {str(content)[:150]}")
                print("────────────────────────────────────────────────────────")
            print()
            continue

        # ── normal message → full agent pipeline ─────────────────────
        try:
            # Pre-LLM guards (mirror handler.py so local testing catches them too)
            if len(user_input) > 500:
                print(f"\nAgent: Message too long (max 500 characters). Please keep it concise.\n")
                continue
            if _INJECTION_RE_LOCAL.search(user_input):
                print(f"\nAgent: {OFF_TOPIC_REPLY}\n")
                continue
            if not is_in_scope(user_input):
                print(f"\nAgent: {OFF_TOPIC_REPLY}\n")
                continue

            # Load context fresh from DB before each turn.
            # This ensures workflow state changes (e.g. UNREGISTERED→PENDING_CATALOGUE)
            # made by tools in the previous turn are reflected immediately.
            # The self-heal logic in context_loader will repair any stale state.
            context = await load_agent_context(telegram_user_id)
            history = await redis.get_conversation(
                telegram_user_id,
                max_messages=settings.MAX_HISTORY_MESSAGES,
            )

            # Stale-draft greeting interceptor — same logic as handler.py
            if is_stale_draft_greeting(user_input, bool(context.active_draft_bill_id)):
                now = datetime.now(timezone.utc).isoformat()
                await redis.append_messages(
                    telegram_user_id,
                    [
                        {"role": "user", "content": user_input, "timestamp": now},
                        {"role": "assistant", "content": STALE_DRAFT_REPLY, "timestamp": now},
                    ],
                )
                print(f"\nAgent: {STALE_DRAFT_REPLY}\n")
                continue

            # Workflow gate interceptors — same logic as handler.py
            if context.workflow_state == "UNREGISTERED" and is_unregistered_block(user_input):
                now = datetime.now(timezone.utc).isoformat()
                await redis.append_messages(
                    telegram_user_id,
                    [
                        {"role": "user", "content": user_input, "timestamp": now},
                        {"role": "assistant", "content": REGISTRATION_GATE_REPLY, "timestamp": now},
                    ],
                )
                print(f"\nAgent: {REGISTRATION_GATE_REPLY}\n")
                continue

            if context.workflow_state == "PENDING_CATALOGUE" and is_pending_catalogue_block(user_input):
                now = datetime.now(timezone.utc).isoformat()
                await redis.append_messages(
                    telegram_user_id,
                    [
                        {"role": "user", "content": user_input, "timestamp": now},
                        {"role": "assistant", "content": CATALOGUE_GATE_REPLY, "timestamp": now},
                    ],
                )
                print(f"\nAgent: {CATALOGUE_GATE_REPLY}\n")
                continue

            # Run agent — tool audit logs emit to stderr automatically
            response, run_messages = await agent.run_with_trace(user_input, history, context)
            _last_run_messages = run_messages
            _last_agent_reply = response

            # Persist to Redis (same as handler.py)
            now = datetime.now(timezone.utc).isoformat()
            await redis.append_messages(
                telegram_user_id,
                [
                    {"role": "user", "content": user_input, "timestamp": now},
                    {"role": "assistant", "content": response, "timestamp": now},
                ],
            )

            print(f"\nAgent: {response}\n")

        except KeyboardInterrupt:
            print("\nBye!")
            break
        except Exception as e:
            print(f"\n  ERROR: {e}")
            traceback.print_exc()
            print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Kirana Agent — interactive local REPL",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run_local.py                # use default test user ID 99999
  python run_local.py --user-id 42  # use a specific test user ID
""",
    )
    parser.add_argument(
        "--user-id",
        type=int,
        default=int(os.environ.get("TEST_TELEGRAM_USER_ID", "99999")),
        help="Fake Telegram user ID to use (default: 99999 or TEST_TELEGRAM_USER_ID from .env)",
    )
    args = parser.parse_args()
    asyncio.run(repl(args.user_id))


if __name__ == "__main__":
    main()
