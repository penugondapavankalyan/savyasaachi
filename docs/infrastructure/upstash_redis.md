# Infrastructure: Upstash Redis

**Type:** Serverless Redis (Upstash)  
**Protocol:** HTTP REST API  
**Purpose:** Conversation history storage (ephemeral, TTL-based)  
**Free Tier:** 10,000 requests/day, 256MB storage

---

## Overview

Upstash Redis stores the per-user conversation history that the PydanticAI agent uses as context. It is the only component besides Supabase that the Lambda function communicates with. Unlike AWS ElastiCache, Upstash is serverless and accessed via HTTP — no VPC configuration, no persistent connections, perfect for Lambda.

---

## Why Upstash (Not AWS ElastiCache)

| Concern | Upstash | ElastiCache |
|---|---|---|
| **VPC requirement** | None — HTTP API | Required (adds cold start latency) |
| **Cost** | Free tier + pay per request | Minimum ~$15-20/month |
| **Lambda cold start impact** | None (HTTP, no TCP connection) | Adds 100-500ms for VPC ENI |
| **Persistence** | AOF persistence on free tier | Yes |
| **TTL support** | Native per-key TTL | Yes |

---

## Setup

1. Create account at [upstash.com](https://upstash.com)
2. Create a new Redis database
3. Select region: `ap-south-1` (closest to India)
4. Note the REST URL and REST Token from the console

---

## Key Naming Convention

```
Pattern:  conv:{telegram_user_id}
Type:     String (JSON-encoded array)
TTL:      86400 seconds (24 hours, sliding)

Examples:
  conv:987654321   → Ramesh's conversation history
  conv:112233445   → Another user's history
```

```
Pattern:  pending_payment:{telegram_user_id}
Type:     String (JSON-encoded dict)
TTL:      1800 seconds (30 minutes, NOT sliding — fixed from time of setting)
Purpose:  Stores over/underpayment delta between Lambda invocations (turns)
          Set by confirm_payment tool when over/underpayment detected.
          Read by add_payment_entry / add_credit_entry tools.
          Deleted immediately on resolution.
          NOT deleted by /new command.

Examples:
  pending_payment:987654321   → Ramesh's pending overpayment of ₹70
```

```
Pattern:  rate:{telegram_user_id}
Type:     Integer counter
TTL:      60 seconds (fixed from first hit — NOT sliding)
Purpose:  Per-user rate limiting. Incremented on every message.
          If counter > 20 within the 60s window, the message is rejected
          before reaching the agent.
          TTL is set only on count == 1 so the window is fixed, not sliding.
          Degrades gracefully: Redis error → treated as not rate-limited.

Examples:
  rate:987654321   → Ramesh's message count in current window
```

Phase 2 future keys:
```
pref:{store_id}    → Cached preferences (not needed — already in Supabase)
summary:{telegram_user_id} → Compressed older conversation context
```

---

## Data Format

```json
[
  {"role": "user", "content": "2kg sugar, 1 Aashirvaad atta", "timestamp": "2024-01-15T09:00:00Z"},
  {"role": "assistant", "content": "Added to bill. Anything else?", "timestamp": "2024-01-15T09:00:05Z"}
]
```

Maximum messages stored per key: 200 (older messages trimmed on write).  
Maximum messages loaded per agent call: 20 (windowed context).

---

## REST API Usage

Upstash Redis REST API uses simple HTTP GET/POST with Bearer token auth.

```python
# GET a key
GET  {UPSTASH_REDIS_REST_URL}/get/{key}
Authorization: Bearer {UPSTASH_REDIS_REST_TOKEN}

# DELETE a key
GET  {UPSTASH_REDIS_REST_URL}/del/{key}

# PING (health check)
GET  {UPSTASH_REDIS_REST_URL}/ping

# SET with TTL — use the pipeline endpoint (NOT /set/{key})
# Old approach (WRONG — treats array elements as separate commands):
#   POST /set/key  body: ["{value}", "EX", "86400"]
# Correct approach (atomic SET + EX in one call):
POST {UPSTASH_REDIS_REST_URL}/pipeline
Body: [["SET", "key", "{json_value}", "EX", "86400"]]
```

All responses are JSON: `{"result": "..."}` for success, `{"error": "..."}` for error.

---

## TTL Strategy

| Event | TTL Behavior |
|---|---|
| User sends a message | TTL reset to 86400s (sliding 24-hour window) |
| `/new` command | Key deleted (`DEL` command) |
| No activity for 24h | Key auto-expires — next message starts fresh context |
| Lambda cold start | No impact — history loaded from Redis on each invocation |

**Sliding TTL:** Every time a message is appended, the entire key's TTL is reset to 86400 seconds. This means a user who sends a message every day will never lose their conversation history due to TTL.

---

## Client Implementation

Full client code is documented in [`docs/agent/conversation_history.md`](../agent/conversation_history.md).

---

## Free Tier Usage Estimate

| Metric | Per Message | Per Day (500 messages) |
|---|---|---|
| Redis requests | 2 (1 GET + 1 SET) | 1,000 |
| Data size per message | ~200 bytes | — |
| Total stored per user | ~40KB (200 messages) | — |

Free tier limit: 10,000 req/day → supports **~5,000 messages/day**.  
Free tier storage: 256MB → supports **~6,400 users** at 40KB each.

---

## Free Tier Usage Estimate (Updated)

With rate limiting added, each message now makes up to **3** Redis requests (instead of 2):

| Operation | Redis requests |
|---|---|
| Load conversation history (GET) | 1 |
| Rate limit INCR (POST pipeline) | 1 |
| Rate limit EXPIRE — first hit only (POST pipeline) | 1 |
| Save updated history (POST pipeline) | 1 |

Worst case per message: **4 requests** (first message of a new rate window).
Typical case: **3 requests** (INCR only, no EXPIRE).

Free tier (10,000 req/day) → supports **~2,500–3,300 messages/day**.

---

## Phase 2 Extensibility

| Feature | Change |
|---|---|
| Multi-store per user | Key: `conv:{telegram_user_id}:{store_id}` |
| Conversation summarization | Add `summary:{telegram_user_id}` for compressed older context |
| Distributed locking | Use `SET key value NX EX seconds` for distributed lock (e.g., prevent concurrent finalizations) |
