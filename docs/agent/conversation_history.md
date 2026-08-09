# Conversation History Design

**File:** `src/redis/upstash_client.py`

---

## Overview

Conversation history is stored in **Upstash Redis** — a serverless, HTTP-based Redis service with a free tier. Each Telegram user has a single Redis key that holds their recent message history. The history is loaded at the start of each Lambda invocation and saved at the end.

Conversation history is **ephemeral context** — it enables the agent to remember what was said in recent messages. It is not permanent data. Preferences, store data, bills, and khata are in Supabase.

---

## Why Redis (Not Supabase) for Conversation History

| Concern | Redis (Upstash) | Supabase |
|---|---|---|
| **Speed** | ~1-3ms via HTTP | ~5-20ms query |
| **TTL (auto-expiry)** | Native TTL per key | Requires cron job |
| **Cost** | Free tier: 10K req/day | Counts against DB connections |
| **Purpose fit** | Short-lived session data | Permanent financial records |
| **Lambda friendliness** | HTTP API, no VPC | Connection pooling needed |

---

## Redis Key Design

```
Key:    conv:{telegram_user_id}
Type:   String (JSON-encoded list)
TTL:    86400 seconds (24 hours)
        Reset on every new message (sliding window TTL)
```

### Key Examples
```
conv:987654321    → Ramesh's conversation history
conv:112233445    → Another user's history
```

---

## Data Format

The value is a JSON-encoded list of message objects:

```json
[
  {
    "role": "user",
    "content": "2kg sugar, 1 Aashirvaad atta",
    "timestamp": "2024-01-15T09:00:00Z"
  },
  {
    "role": "assistant",
    "content": "Added to bill: 2kg Sugar, 1 Aashirvaad Atta 5kg. Anything else?",
    "timestamp": "2024-01-15T09:00:05Z"
  },
  {
    "role": "user",
    "content": "also 4 Maggi",
    "timestamp": "2024-01-15T09:10:00Z"
  },
  {
    "role": "assistant",
    "content": "Added 4 Maggi. Current bill: Sugar 2kg, Atta 1, Maggi 4. Total ~₹385. Done?",
    "timestamp": "2024-01-15T09:10:05Z"
  }
]
```

---

## Windowed Context Loading

To prevent the conversation history from growing unboundedly (which would increase LLM token costs), only the **last N messages** are loaded as context for each invocation:

```python
MAX_HISTORY_MESSAGES = 20  # ~10 turns of back-and-forth

async def get_conversation(telegram_user_id: int, max_messages: int = 20) -> List[Message]:
    raw = await self.redis.get(f"conv:{telegram_user_id}")
    if not raw:
        return []
    messages = json.loads(raw)
    # Return only last N messages
    return messages[-max_messages:]
```

The full history up to TTL expiry is preserved in Redis (not windowed on write), but only a window is passed to the LLM. This means:
- Long conversations don't inflate LLM costs
- Recent context is always available
- Old context naturally expires via TTL

---

## Upstash Client Implementation

```python
import httpx
import json
import os
from typing import List, Optional

class UpstashRedisClient:
    def __init__(self):
        self.base_url = os.environ["UPSTASH_REDIS_REST_URL"]
        self.token = os.environ["UPSTASH_REDIS_REST_TOKEN"]
        self.headers = {"Authorization": f"Bearer {self.token}"}
        self.ttl_seconds = 86400  # 24 hours
    
    async def get_conversation(
        self,
        telegram_user_id: int,
        max_messages: int = 20
    ) -> List[dict]:
        """Load conversation history for a user."""
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.base_url}/get/conv:{telegram_user_id}",
                headers=self.headers
            )
        data = response.json()
        if not data.get("result"):
            return []
        messages = json.loads(data["result"])
        return messages[-max_messages:]
    
    async def save_conversation(
        self,
        telegram_user_id: int,
        messages: List[dict]
    ) -> None:
        """Save full conversation history and reset TTL."""
        async with httpx.AsyncClient() as client:
            # SET with EX (expire in seconds)
            await client.post(
                f"{self.base_url}/set/conv:{telegram_user_id}",
                headers=self.headers,
                json=[json.dumps(messages), "EX", str(self.ttl_seconds)]
            )
    
    async def append_messages(
        self,
        telegram_user_id: int,
        new_messages: List[dict]
    ) -> None:
        """Append new messages to existing history and reset TTL."""
        existing = await self.get_conversation(telegram_user_id, max_messages=9999)
        updated = existing + new_messages
        # Keep max 200 messages total in Redis to cap storage
        if len(updated) > 200:
            updated = updated[-200:]
        await self.save_conversation(telegram_user_id, updated)
    
    async def clear_conversation(self, telegram_user_id: int) -> None:
        """/new chat — delete conversation history."""
        async with httpx.AsyncClient() as client:
            await client.get(
                f"{self.base_url}/del/conv:{telegram_user_id}",
                headers=self.headers
            )
    
    async def ping(self) -> bool:
        """Health check."""
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.base_url}/ping",
                headers=self.headers
            )
        return response.json().get("result") == "PONG"
```

---

## Lambda Lifecycle Integration

```python
# In handler.py — per invocation

async def handle_message(telegram_update: dict) -> None:
    telegram_user_id = telegram_update["message"]["from"]["id"]
    user_message = telegram_update["message"]["text"]
    
    # 1. Load history BEFORE agent
    history = await upstash_client.get_conversation(telegram_user_id)
    
    # 2. Run agent
    result = await agent.run(user_message, history, store_context)
    
    # 3. Save updated history AFTER agent
    new_messages = [
        {"role": "user", "content": user_message, "timestamp": now_iso()},
        {"role": "assistant", "content": result.text, "timestamp": now_iso()}
    ]
    await upstash_client.append_messages(telegram_user_id, new_messages)
    
    # 4. Send response to Telegram
    await telegram_client.send_message(telegram_user_id, result.text)
```

---

## `/new` Chat Behavior

```
User sends: /new

handler.py detects /new command (before agent invocation)
→ upstash_client.clear_conversation(telegram_user_id)
→ Does NOT touch Supabase (bills, inventory, khata, preferences all preserved)
→ Sends: "Chat cleared! Your store data and preferences are all still there. What would you like to do?"
→ Agent is NOT invoked for /new — handled entirely at the handler level
```

**What is cleared:**
- ✅ Conversation history in Upstash Redis

**What is NOT cleared:**
- ❌ User and store records (Supabase `users`, `stores`)
- ❌ Products catalogue (Supabase `products`)
- ❌ Inventory (Supabase `inventory`)
- ❌ Bills (Supabase `bills`)
- ❌ Khata entries (Supabase `khata_entries`)
- ❌ Preferences (Supabase `stores.preferences`)
- ❌ Workflow state (Supabase `workflow_state`) — stays ACTIVE

---

## TTL Strategy

| Event | TTL Behavior |
|---|---|
| New message received | TTL reset to 24h (sliding window) |
| `/new` command | Key deleted immediately |
| No activity for 24h | Key auto-expires, next message starts fresh |
| Lambda cold start | History loaded from Redis — Lambda being cold doesn't affect history |

---

## Free Tier Considerations (Upstash)

Upstash free tier: 10,000 requests/day, 256MB storage.

Per message, the system makes:
- 1 GET (load history)
- 1 SET (save updated history)

= **2 requests per user message**

Free tier supports ~5,000 messages/day — ample for Phase 1 single-store usage.

---

## Phase 2 Extensibility

| Feature | Change |
|---|---|
| Multi-store per user | Key becomes `conv:{telegram_user_id}:{store_id}` |
| Conversation summary | Add periodic summarization: compress old messages into a summary entry |
| Voice note transcripts | Store transcribed text as user message in same format |
