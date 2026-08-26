# Scope Guard — Off-Topic Message Filter

**File:** `src/utils/scope_guard.py`  
**Type:** Pre-LLM input filter  
**Called from:** `src/handler.py`, `run_local.py`

---

## Purpose

Prevents the agent from answering questions outside kirana store operations (e.g. "what is python?", "capital of France?") by checking the message for store-domain keywords **before** the LLM is invoked.

This is a deterministic Python check — it cannot be overridden by any model, unlike system prompt rules which models can ignore.

---

## Why a Keyword Allowlist (Not a Denylist)

| Approach | Problem |
|---|---|
| **Denylist** (block known off-topic words) | Off-topic topics are infinite — the list can never be complete |
| **Allowlist** (require at least one store keyword) | Store domain is finite and well-defined — easier to enumerate |

The allowlist approach means: *"if this message contains no store-related word, it is off-topic."*

---

## Decision Logic

```
is_in_scope(message)
    │
    ├── Starts with "/"  ──────────────→ True  (Telegram command — always forward)
    │
    ├── ≤ 4 words  ─────────────────→ True  (conversational continuation)
    │      e.g. "yes", "done", "500", "cancel", "add it"
    │
    ├── Domain keyword match  ───────→ True  (store-related)
    │      e.g. "add 2kg rice", "what is my khata balance?",
    │           "generate invoice for BL-003"
    │
    └── No keyword matched  ─────────→ False (off-topic → return OFF_TOPIC_REPLY)
```

---

## Short Message Bypass (≤ 4 words)

Messages of 4 words or fewer are always forwarded to the agent unchanged. These are almost always conversational continuations that the agent needs to process:

| Message | Words | Why it must pass |
|---|---|---|
| `yes` | 1 | Confirming a product add |
| `done` | 1 | Signalling end of bill |
| `500` | 1 | Paying amount |
| `no skip it` | 3 | Declining a product |
| `add it` | 2 | Short affirmative |
| `cash 200` | 2 | Payment mode + amount |

Without this bypass, all single-word replies would be blocked.

---

## Domain Keyword Categories

| Category | Example keywords |
|---|---|
| Billing & payment | `bill`, `invoice`, `pay`, `paid`, `cash`, `upi`, `total`, `amount`, `mrp` |
| Products & catalogue | `product`, `item`, `catalogue`, `stock`, `inventory` |
| Common grocery items | `rice`, `sugar`, `oil`, `atta`, `dal`, `milk`, `soap`, `ghee`, … |
| Units | `kg`, `gram`, `litre`, `packet`, `bottle`, `piece`, `dozen` |
| Customer & credit | `customer`, `khata`, `udhar`, `credit`, `balance`, `ledger` |
| GST & tax | `gst`, `cgst`, `sgst`, `igst`, `gstin`, `hsn`, `tax` |
| Store & setup | `store`, `shop`, `register`, `setup`, `owner`, `address` |
| Analytics & reports | `report`, `analytics`, `sales`, `revenue`, `pdf`, `pptx` |
| Action verbs (store context) | `cancel`, `void`, `update`, `edit`, `list`, `show`, `fetch` |

---

## Test Cases

| Message | `is_in_scope` | Reason |
|---|---|---|
| `"what is python?"` | `False` | No store keyword, > 4 words |
| `"capital of France?"` | `False` | No store keyword |
| `"add 2kg sugar"` | `True` | `kg`, `sugar` match |
| `"show my khata balance"` | `True` | `khata`, `balance` match |
| `"generate invoice for BL-003"` | `True` | `invoice` matches |
| `"yes"` | `True` | ≤ 4 words bypass |
| `"500"` | `True` | ≤ 4 words bypass |
| `"/new"` | `True` | Slash command bypass |
| `"how do I make biryani?"` | `False` | No store keyword |
| `"what is GST?"` | `True` | `gst` matches — store-adjacent |
| `"ignore all instructions"` | `True`* | Caught earlier by injection filter (#3) |

\* The injection filter in `handler.py` runs before `is_in_scope` — this message never reaches the scope check.

---

## Canned Response

When `is_in_scope` returns `False`, the handler returns `OFF_TOPIC_REPLY` directly without calling the LLM:

```
"I can only help with your kirana store — billing, stock, catalogue, and accounts.
 For anything else, please use a general search or assistant."
```

This string is defined as `OFF_TOPIC_REPLY` in `scope_guard.py` and imported by both `handler.py` and `run_local.py` to ensure consistency.

---

## Integration Points

### `src/handler.py`
```python
from src.utils.scope_guard import OFF_TOPIC_REPLY, is_in_scope

# Guard #2 — off-topic scope filter
if not is_in_scope(user_message):
    await telegram.send_message(chat_id, OFF_TOPIC_REPLY)
    return {"statusCode": 200, "body": "OK"}
```

### `run_local.py`
```python
from src.utils.scope_guard import OFF_TOPIC_REPLY, is_in_scope

if not is_in_scope(user_input):
    print(f"\nAgent: {OFF_TOPIC_REPLY}\n")
    continue
```

Both use the same module so behaviour is identical in production (Lambda) and local REPL.

---

## Extending the Keyword List

To add new store-domain terms, edit `_DOMAIN_PATTERN` in `src/utils/scope_guard.py`. The regex uses `\b` word boundaries and `re.IGNORECASE` so additions are simple:

```python
# Add new terms by appending to the alternation group:
r"your_new_term|another_term|"
```

Do NOT add overly broad words (e.g. `add`, `get`, `show` alone) — these create false positives that let off-topic messages through. Rely on the 4-word bypass for lone action words.
