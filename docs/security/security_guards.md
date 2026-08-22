# Security Guards — Overview

**Introduced:** Post v3.1  
**Files:** `src/handler.py`, `src/utils/scope_guard.py`, `src/utils/guardrails.py`, `src/redis/upstash_client.py`

---

## Overview

Six layered security guards protect the agent from abuse, off-topic misuse, and prompt injection attacks. They are applied in order — earlier guards are cheaper and block more obvious attacks before reaching more expensive operations (LLM calls, DB queries).

```
Telegram Message
       │
       ▼
[1] Length cap (> 500 chars → reject)          handler.py
       │
       ▼
[3] Injection pattern filter (regex → reject)  handler.py
       │
       ▼
[2] Scope guard (no store keyword → reject)    handler.py  ← scope_guard.py
       │
       ▼
[4] Rate limit (> 20 msg/60s → reject)         handler.py  ← upstash_client.py
       │
       ▼
  Agent / LLM
       │
       ▼
[6] History poisoning strip (on history load)  upstash_client.py
       │
       ▼
  Tool calls → MCP layer
       │
       ▼
[5] Unicode NFKC normalisation (all string inputs) guardrails.py
       │
       ▼
  Supabase DB
```

---

## Guards at a Glance

| # | Guard | Layer | File | Mechanism |
|---|---|---|---|---|
| 1 | **Message length cap** | Handler (pre-LLM) | `handler.py` | Reject messages > 500 chars |
| 2 | **Off-topic scope filter** | Handler (pre-LLM) | `scope_guard.py` | Keyword allowlist — no store term → reject |
| 3 | **Injection pattern filter** | Handler (pre-LLM) | `handler.py` | Regex on known injection phrases |
| 4 | **Per-user rate limit** | Handler (pre-LLM) | `upstash_client.py` | Redis INCR/EXPIRE, 20 msg/60 s |
| 5 | **Unicode NFKC normalisation** | Tool input | `guardrails.py` | Collapse lookalike chars before validation |
| 6 | **History poisoning strip** | Redis load | `upstash_client.py` | Strip injected messages from stored history |

---

## Guard Details

### Guard 1 — Message Length Cap

**File:** [`src/handler.py`](../../src/handler.py)  
**Threshold:** 500 characters  

No legitimate kirana store message (billing, stock queries, khata entries) requires more than ~200 characters. Long messages are a common prompt injection vector — attackers paste essay-length instruction overrides.

```python
if len(user_message) > 500:
    # Reject before any DB or LLM call
    await telegram.send_message(chat_id, "Message too long...")
    return
```

---

### Guard 2 — Off-Topic Scope Filter

**File:** [`src/utils/scope_guard.py`](../../src/utils/scope_guard.py)  
**See also:** [`docs/security/scope_guard.md`](scope_guard.md)

Uses a keyword allowlist of store-domain terms. If no domain keyword is found in a message longer than 4 words, the message is rejected before the LLM is called.

```
"what is python?"       → no store keyword → rejected  ✓
"add 2 kg sugar"        → "kg", "sugar" match → forwarded  ✓
"yes"                   → ≤ 4 words bypass → forwarded  ✓
"/new"                  → slash command bypass → forwarded  ✓
```

---

### Guard 3 — Injection Pattern Filter

**File:** [`src/handler.py`](../../src/handler.py)  
**Applied:** Before scope guard — known injection phrases are blocked regardless of domain keywords.

Regex matches well-known prompt injection phrases:

```
"ignore all previous instructions"  → blocked
"you are now a general assistant"   → blocked  
"pretend to be ChatGPT"             → blocked
"jailbreak"                         → blocked
"forget everything"                 → blocked
```

The pattern is intentionally narrow to avoid false positives on legitimate messages like "forget that item, remove it".

---

### Guard 4 — Per-User Rate Limit

**File:** [`src/redis/upstash_client.py`](../../src/redis/upstash_client.py)  
**Limit:** 20 messages per 60-second window per `telegram_user_id`

Protects Groq API token budget from flooding. Uses the Redis INCR + EXPIRE pattern:
- `INCR rate:{telegram_user_id}` — atomically increments counter (creates on first use)
- `EXPIRE rate:{telegram_user_id} 60` — set only on count == 1 so window is fixed, not sliding

Degrades gracefully: any Redis error returns `False` (not rate-limited) so Redis downtime never blocks legitimate users.

---

### Guard 5 — Unicode NFKC Normalisation

**File:** [`src/utils/guardrails.py`](../../src/utils/guardrails.py)  
**Applied:** `clean_optional_str()` — called by all tool input sanitisers

Attackers use Unicode lookalike characters to bypass keyword/placeholder detection:
- Cyrillic `а` (U+0430) looks identical to Latin `a` (U+0061)
- Zero-width spaces (U+200B) break string matching
- Bidirectional override characters (U+202E) flip text rendering

NFKC normalisation collapses all of these to their canonical ASCII equivalents before any comparison.

```python
v = unicodedata.normalize("NFKC", value).strip()
```

---

### Guard 6 — History Poisoning Strip

**File:** [`src/redis/upstash_client.py`](../../src/redis/upstash_client.py)  
**Applied:** `get_conversation()` — every time history is loaded before an agent run

If a malicious message somehow passed earlier guards and was stored in Redis history, it would re-inject itself into every future agent call in that session. The history load now filters out any stored message whose content matches the injection pattern regex.

```python
messages = [m for m in messages if not _contains_injection(m.get("content", ""))]
```

---

## Defence-in-Depth Summary

| Attack | Guard(s) that stop it |
|---|---|
| Prompt injection via long pasted text | #1 (length), #3 (pattern) |
| "what is python?" / general knowledge | #2 (scope) |
| "ignore all previous instructions" | #3 (pattern), system prompt rule 23 |
| Unicode bypass of null-string detection | #5 (NFKC) |
| Stored injection in Redis history | #6 (history strip) |
| Token flooding / API cost abuse | #4 (rate limit) |
| Role-override attempt (soft) | System prompt rule 23 |

---

## What is NOT Covered

- **Allowlisted user IDs (guard #7):** Not implemented — this is a public bot. Any Telegram user can interact.
- **LLM-level jailbreaks:** System prompt rule 23 provides a soft instruction but cannot guarantee model compliance. Guards #1–#3 catch the common patterns before the LLM sees them.
- **Image/sticker/voice inputs:** `update_parser.is_message_update()` only passes text messages — non-text updates are silently dropped at the handler entry point.
