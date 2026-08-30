# Kirana Agent

A Telegram-based AI assistant for managing Indian kirana (grocery) stores.
Built with PydanticAI, Supabase, and AWS Lambda.

## Telegram Bot

| | |
|---|---|
| **Bot Link** | [Telegram Bot Link](https://t.me/SavyasaachiBot) |
| **Telegram User Name** | @SavyasaachiBot |

## What It Does

| Feature | Description |
|---|---|
| **Registration** | Register a store via natural conversation |
| **Catalogue** | Add and manage products (loose/branded, GST-aware) |
| **Inventory** | Track stock levels, receive goods, reorder level status |
| **Billing** | Multi-turn bill creation with GST computation (CGST + SGST) |
| **Payments** | Cash/UPI/credit (Khata) payment tracking with over/underpayment handling |
| **Khata (Credit)** | Customer credit ledger (Khata Account) with balance tracking |
| **Analytics** | Weekly sales summaries, trends, GST breakdowns |
| **Documents** | PDF invoices + PPTX analysis decks |

## Architecture

```
Telegram  ──webhook──▶  AWS Lambda (src/handler.py)
                              │
                    ┌─────────┴──────────┐
                    │  PydanticAI Agent  │
                    │     (Ollama)       │
                    └─────────┬──────────┘
                              │ calls tools
              ┌───────────────┼──────────────────┐
              │               │                  │
        MCP modules     Upstash Redis       Supabase DB
        (src/mcp/)     (conv history)   (all persistent data)
```

**Technology stack:**
- **Database:** Supabase (PostgreSQL) — always running
- **Compute:** AWS Lambda — invoked only on messages (pay-per-use)
- **LLM:** Ollama (cloud via `https://ollama.com/v1` or local `http://localhost:11434/v1`)
- **Conversation history:** Upstash Redis (serverless, HTTP)
- **Framework:** PydanticAI for structured tool calling

## Pre-LLM Input Guards

Every message passes through code-level filters in [`handler.py`](src/handler.py) **before** the agent is invoked:

| Guard | Description |
|---|---|
| **Message length cap** | Rejects messages over 500 characters |
| **Injection filter** | Blocks prompt-injection patterns (`ignore instructions`, `jailbreak`, `act as`, etc.) |
| **Scope filter** | Blocks provably off-topic WH-questions (e.g. `what is python?`) — see [`scope_guard.py`](src/utils/scope_guard.py) |
| **Rate limit** | 20 messages / 60 seconds per user (enforced via Upstash Redis `rate:{user_id}` key) |
| **Workflow gate** | Blocks billing/khata messages when store is `UNREGISTERED` or `PENDING_CATALOGUE` |
| **Stale draft interceptor** | Intercepts bare greetings when a draft bill is open — returns a fixed keyword-menu reply without hitting the LLM |

History retrieved from Redis is also scanned for injection patterns before being passed to the agent as conversation context.

## Project Structure

```
savyasaachi/
├── kirana-agent-plan.md # Original project plan / design document
├── migrations/          # SQL migrations (already applied to Supabase)
├── docs/                # Detailed architecture documentation
├── src/
│   ├── handler.py       # Lambda entry point
│   ├── config.py        # Settings & environment variable validation
│   ├── agent/           # PydanticAI agent, system prompt, tool registry
│   │   ├── config.py        # AgentConfig + StoreContext models
│   │   ├── context_loader.py# Pre-agent context builder (self-healing workflow state)
│   │   ├── kirana_agent.py  # KiranaAgent wrapper with model fallback
│   │   ├── system_prompt.py # Dynamic system prompt assembly per request
│   │   └── tool_registry.py # Context-bound tool closures + intent routing
│   ├── mcp/             # 8 MCP modules (identity, catalogue, inventory,
│   │                    #   billing, khata, analytics, documents, payments)
│   ├── db/              # Supabase client singleton
│   ├── redis/           # Upstash Redis client (conversations, payments, rate limits)
│   ├── telegram/        # Telegram API client + update parser
│   └── utils/           # gst.py, ist.py, guardrails.py, reorder_alert.py, scope_guard.py
├── scripts/
│   ├── deploy.sh              # Package and deploy to Lambda
│   ├── deploy.md              # Manual Windows deploy steps
│   ├── register_webhook.py    # Register Telegram webhook
│   ├── test_agent_local.py    # Local conversation pipeline test
│   ├── inspect_pptx.py        # Inspect generated PPTX files locally
│   ├── test_markdownv2_escaper.py # Test MarkdownV2 escaping
│   └── test_scope_guard.py    # Test scope guard filter
├── .gitignore
└── requirements.txt
```

## MCP Modules

| Module | Schema | Responsibility |
|---|---|---|
| `identity` | `identity` | User + store registration, preferences, workflow state |
| `catalogue` | `catalogue` | Products — add, search, update (loose/branded, GST rates) |
| `inventory` | `inventory` | Stock levels, receive goods, reorder alerts |
| `billing` | `billing` | Draft bills, line items, finalize/cancel/void, GST computation |
| `payments` | `payments` | Payment rows (EXACT/OVERPAYMENT/UNDERPAYMENT/KHATA/KHATA_SETTLE) |
| `khata` | `khata` | Customer credit ledger — add credit entries, query balances |
| `analytics` | `analytics` | Daily/weekly sales summaries, GST breakdowns |
| `documents` | `billing` + `analytics` | PDF invoice generation, PPTX analysis deck generation |

## Workflow States

| State | Condition | Tools Available |
|---|---|---|
| `UNREGISTERED` | New user | 2 tools — `save_owner_name`, `setup_store` |
| `PENDING_CATALOGUE` | Store created | 5 tools — add/search/update product, update store/owner |
| `PENDING_INVENTORY` | ≥1 product added | + `receive_stock` |
| `ACTIVE` | ≥1 stock-in done | All 8 MCPs — split into intent groups (max ~12 tools per request) |

### ACTIVE Intent Groups

In `ACTIVE` state, tools are split into 6 intent groups to stay within the LLM token budget. [`detect_intent()`](src/agent/tool_registry.py) selects the group automatically from the user's message:

| Intent Group | Triggered by | Tools |
|---|---|---|
| `BILLING` | Default / bill creation | Add items, create bill, finalize, cancel |
| `BILLING_CONFIRM` | Payment words, bill lookup, void/cancel by name | Confirm payment, void bill, list bills, PDF invoice |
| `INVENTORY` | Stock / reorder keywords | Receive stock, check levels, reorder status |
| `KHATA` | Credit / balance keywords | Add credit, view balance, list customers |
| `ANALYTICS` | Report / sales keywords | Daily/weekly summaries, GST report, PPTX deck |
| `CATALOGUE` | Product add/edit keywords | Add, search, update product details |

## Redis Key Layout

| Key | TTL | Purpose |
|---|---|---|
| `conv:{telegram_user_id}` | 24 h (sliding) | Conversation history (capped at 200 entries) |
| `pending_payment:{telegram_user_id}` | 30 min | Over/underpayment delta between turns |
| `rate:{telegram_user_id}` | 60 s | Per-user rate limit counter (20 msg/min) |

## Setup

### 1. Prerequisites

```bash
# Python 3.12+
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Environment

```bash
cp .env.example .env
# Required: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY,
#           UPSTASH_REDIS_REST_URL, UPSTASH_REDIS_REST_TOKEN,
#           TELEGRAM_BOT_TOKEN,
#           OLLAMA_MODEL (e.g. gpt-oss:120b-cloud),
#           OLLAMA_BASE_URL (https://ollama.com/v1 or http://localhost:11434/v1),
#           OLLAMA_API_KEY (required for ollama.com cloud; any value for local)
# Optional: LOCAL_MODE=true, LOCAL_DOCS_OUTPUT_DIR, LLM_TEMPERATURE (default 0.1)
```

### 3. Database

The schema is already deployed. To verify:

```sql
-- Run in Supabase SQL editor
SELECT table_schema, table_name
FROM information_schema.tables
WHERE table_schema IN ('identity','catalogue','inventory','billing','khata','analytics','payments')
ORDER BY table_schema, table_name;
```

### 4. Local testing

```
python scripts/test_agent_local.py
```
Note: Make sure you have all the required API keys/settings in .env file before you run in order to work


## Key Design Decisions

- **Serverless:** Lambda cold starts are acceptable; agent only runs when a message arrives.
- **Stateless compute:** All state (workflow, bills, preferences) is in Supabase. Lambda can scale to N instances.
- **Idempotent operations:** Every MCP write is safe to retry (upserts, ON CONFLICT, workflow_id check).
- **ACID compliance:** Critical paths (`finalize_bill`, `decrement_stock`) use Supabase RPCs with row-level locking.
- **No hardcoded intent router:** `detect_intent()` uses keyword matching to select a tool group; the LLM reasons over those tools naturally.
- **Context-bound tools:** `store_id` and `telegram_user_id` are baked into every tool closure — the LLM never sees or passes them.
- **IST dates everywhere:** All timestamps and day-boundary queries use [`ist.py`](src/utils/ist.py) helpers. `datetime.utcnow()` is never used.
- **Immutable audit tables:** `bills`, `bill_items`, `khata_entries`, `stock_movements`, `payments.payments` have DB-level triggers preventing UPDATE/DELETE. Corrections are recorded as adjustment entries.
- **Self-healing workflow state:** [`context_loader.py`](src/agent/context_loader.py) detects and repairs stale `workflow_state` rows on every invocation.
- **Payments schema separation:** The `payments` schema is separate from `billing` — each schema is independently exposed in Supabase API settings.

## Phase 2 Extensions (scaffolded, not yet enabled)

- Multiple stores per user
- Multiple users per store (cashier, worker roles)
- Scheduled PPTX reports
- Payment reminders
- Multiple Language support

---

## Technical Deep-Dive

### Framework Harness and Why

The agent is built on **[PydanticAI](https://ai.pydantic.dev/)** as the agent framework. PydanticAI was chosen for three concrete reasons:

1. **Structured tool calling with type-safe I/O.** Every tool is a typed Python async callable. PydanticAI marshals the LLM's JSON arguments into typed Python values and validates them before your code ever runs. This means the oversell guard, GST validator, and UUID cleaner in [`src/utils/guardrails.py`](src/utils/guardrails.py) receive proper Python `float` and `str` values — not raw JSON strings that need re-parsing.

2. **Provider-agnostic model abstraction.** The `Agent(model=..., tools=...)` API works identically for Groq, Ollama, and any OpenAI-compatible endpoint. Swapping the LLM is a one-line config change — no tool or prompt logic changes. The fallback cascade in [`kirana_agent.py`](src/agent/kirana_agent.py) exploits this: if the primary model rate-limits or returns a 400 "tool calling not supported" error, the next model in `_model_chain` is tried transparently.

3. **Minimal surface area.** PydanticAI does not force a framework-specific "memory" layer, a router, or a chain abstraction. The control loop stays in plain Python (`async_handler → load_agent_context → agent.run → save history`), which makes every step debuggable without framework internals.

The current deployment uses **Ollama** (cloud at `https://ollama.com/v1`) through PydanticAI's `OpenAIChatModel` + `OpenAIProvider` wrapper — Ollama's cloud API is OpenAI-compatible, so no special integration was needed.

---

### Control Loop

The control loop runs entirely inside [`src/handler.py`](src/handler.py) and executes on every Telegram webhook delivery:

```
Telegram webhook
  └─▶ lambda_handler (sync)
        └─▶ async_handler
              1. Parse update (telegram_user_id, message_text)
              2. Pre-LLM guards (length cap, injection filter, scope guard, rate limit)
              3. /new → clear Redis history, cancel open drafts, return early
              4. Ensure user record exists (idempotent upsert via IdentityMCP)
              5. load_agent_context → build StoreContext (workflow state, store, preferences)
              6. Stale-draft greeting interceptor — bypass LLM, return canned reply
              7. Workflow gate interceptors (UNREGISTERED / PENDING_CATALOGUE)
              8. redis.get_conversation → load history
              9. agent.run(message, history, context)
                   └─▶ detect_intent → select tool group (max ~12 tools)
                   └─▶ build_system_prompt(StoreContext)
                   └─▶ PydanticAI Agent.run → LLM ↔ tool calls ↔ results
             10. redis.append_messages → save updated history
             11. telegram.send_message → reply to user
```

The `Agent(...)` object is **not** a singleton — it is rebuilt on every request because the tool list changes per workflow state and intent, and the system prompt is assembled fresh from the current `StoreContext`. The MCP instances (`MCPInstances`), the Supabase client, and the Telegram client are module-level singletons that survive across warm Lambda invocations.

---

### Skill / Tool Design

Tools are **context-bound closures** defined in [`src/agent/tool_registry.py`](src/agent/tool_registry.py). On every request, `get_tools_for_state()` builds a fresh list of `async def` functions that close over `tuid` (Telegram user ID) and `store_id` from `StoreContext`. The LLM never receives or passes these IDs — they are baked into each function's closure at construction time.

This eliminates an entire class of hallucination: the LLM cannot invent a `store_id` or route data to the wrong store, because those values are never in the LLM's context at all.

Tool docstrings serve as the LLM-facing API specification. Rules like "call receive_stock() in the SAME turn as add_product()" and "NEVER pass 0% GST for a branded item" are written directly into the docstring and enforced at the tool layer independently.

For the `ACTIVE` workflow state, tools are split into six intent groups — `BILLING`, `BILLING_CONFIRM`, `INVENTORY`, `KHATA`, `ANALYTICS`, `CATALOGUE` — each containing at most ~12 tools. [`detect_intent()`](src/agent/tool_registry.py) selects the group from the user's message using keyword matching before the LLM is invoked. This keeps the token budget predictable and prevents the LLM from being overwhelmed by irrelevant tools.

Mutable state that must be visible across sibling closures within a single request (the active draft bill ID, the last confirmed bill ID, the most recently added product ID) is stored in single-element list cells — `_draft_id_cell = [value]` — so that one closure can update the value in-place and all other closures in the same request immediately see the new value. A plain variable capture would freeze the initial snapshot.

---

### How Each Hard Part Is Solved

#### 1. Grounding — prices, GST slabs, and stock from the DB

The LLM never invents product data. Every billing operation flows through `search_products()` or `list_products()`, which query the `catalogue.products` table and return the real `mrp`, `gst_rate`, and `unit` from Supabase. The `add_item_to_draft()` method in [`billing_mcp.py`](src/mcp/billing/billing_mcp.py) calls `CatalogueMCP.get_product()` to fetch the live price and GST rate — the LLM-passed quantity is the only input that comes from the model. `product_id` values are marked `[internal — do NOT show to owner]` in tool return strings so the LLM uses them as opaque handles, not values to guess.

#### 2. Oversell Guard — stock cannot go negative

The guard operates at two layers:

- **Tool layer (pre-draft):** `add_item_to_draft()` in [`billing_mcp.py`](src/mcp/billing/billing_mcp.py) calls `InventoryMCP.check_availability()` (see [`inventory_mcp.py`](src/mcp/inventory/inventory_mcp.py)) before writing anything. If stock is `NONE`, it returns an error immediately and nothing is written to `draft_bill_items`. If stock is `PARTIAL`, it returns the available quantity and a prompt asking the owner to confirm.

- **DB layer (at finalization):** The `decrement_stock` PostgreSQL RPC in [`migrations/010_create_rpcs.sql`](migrations/010_create_rpcs.sql) executes a `SELECT ... FOR UPDATE` row lock on the inventory row, re-checks `quantity_in_stock >= requested`, and raises a PostgreSQL exception if the check fails — all inside a single transaction. This is the final line of defence if two concurrent requests somehow both pass the tool-layer check.

The LLM is never in this path. It cannot approve an oversell by responding "yes, sell it anyway" — the tool simply returns an error string.

#### 3. GST Correctness

GST computation is centralized in [`src/utils/gst.py`](src/utils/gst.py) and never done inline. Indian MRP is GST-inclusive, so the implementation back-calculates the tax component:

```
line_total    = qty × MRP
taxable_value = line_total / (1 + rate/100)
gst_total     = line_total − taxable_value
cgst          = round(gst_total / 2, up)
sgst          = gst_total − cgst           # absorbs rounding delta
```

`Decimal` arithmetic with `ROUND_HALF_UP` is used throughout to avoid floating-point drift. SGST absorbs the rounding delta so `cgst + sgst == gst_total` exactly. `aggregate_gst()` sums across all line items using `Decimal` accumulators before the final `float()` cast.

GST rates are validated at input time by `clean_gst_rate()` in [`src/utils/guardrails.py`](src/utils/guardrails.py): loose items are forced to 0%, branded items must be exactly one of 5 / 12 / 18 / 28 % — any other value raises a `ValueError` that surfaces as a tool error, forcing the agent to ask the owner.

The `get_draft_bill()` and `finalize_bill()` methods compute `taxable_value`, `cgst_amount`, and `sgst_amount` per line item and store them as snapshots in `billing.bill_items` — the PDF invoice reads these stored values directly without re-computing.

#### 4. Multi-Turn Bills

A draft bill is a live database record in `billing.draft_bills` with status `OPEN` and a 4-hour `expires_at`. Items accumulate in `billing.draft_bill_items` via upsert on `(draft_bill_id, product_id)` — adding the same product twice updates its quantity rather than creating a duplicate row.

`_draft_id_cell` in the tool closure captures `context.active_draft_bill_id` at request start and is updated in-place by `create_draft_bill()` when a new draft is opened. Every subsequent tool call (`add_item_to_draft`, `remove_item_from_draft`, `update_item_quantity`, `get_draft_bill`) reads from this cell — the LLM never passes the draft ID.

Between sessions, the draft ID is persisted in `identity.workflow_state.active_draft_bill_id`. `load_agent_context()` in [`context_loader.py`](src/agent/context_loader.py) reads it back on every Lambda invocation, so a bill started in one session resumes seamlessly in the next.

`get_draft_bill()` computes live GST totals at any point, so the owner can inspect the running total mid-session before committing.

#### 5. Idempotency — retried finalize must not double-bill or double-decrement

`finalize_bill()` in [`billing_mcp.py`](src/mcp/billing/billing_mcp.py) checks for an existing bill with the same `workflow_id` **before** inserting anything:

```python
existing_bill_resp = (
    self.db.schema("billing").table("bills")
    .select("*").eq("workflow_id", workflow_id).limit(1).execute()
)
if existing_bill := _one(existing_bill_resp):
    return FinalizedBillResult(..., already_finalized=True, ...)
```

Each draft bill carries a unique `workflow_id` (UUID). If Telegram redelivers the finalize message and the agent calls `finalize_bill()` a second time, the idempotency check finds the existing bill and returns it immediately — no second insert, no second `decrement_stock` call.

The `increment_stock` RPC similarly uses `ON CONFLICT (store_id, product_id) DO UPDATE` — a retry adds the delta again, but `receive_stock` is only called with an explicit quantity, so a true duplicate call (same quantity, same product) would double the stock-in. This is acceptable because `receive_stock` is driven by the owner explicitly saying "received 50 units" — a human-in-the-loop action that does not get automatically retried.

`register_user()` in [`identity_mcp.py`](src/mcp/identity/identity_mcp.py) is idempotent by design — it merges only non-None fields and inserts `workflow_state` / `registration` rows with `ON CONFLICT DO NOTHING`. It is called on **every** Lambda invocation.

#### 6. Concurrency — two bills or a sale plus a stock-in must not corrupt stock

The `decrement_stock` PostgreSQL RPC (in [`migrations/010_create_rpcs.sql`](migrations/010_create_rpcs.sql)) uses a row-level lock:

```sql
SELECT id, quantity_in_stock, reorder_level
INTO v_inv_id, v_current_qty, v_reorder_lvl
FROM inventory.inventory
WHERE store_id = p_store_id AND product_id = p_product_id
FOR UPDATE;
```

Two concurrent `finalize_bill` calls for the same product will serialize at this `FOR UPDATE` — the second one reads the already-decremented quantity and raises an exception if it is now insufficient. Stock can never go negative from two concurrent sales.

`increment_stock` uses `ON CONFLICT DO UPDATE SET quantity_in_stock = inventory.inventory.quantity_in_stock + EXCLUDED.quantity_in_stock` — a concurrent stock-in and sale on the same product serialize at the Postgres row lock, so no quantity is lost.

#### 7. Guardrails

Multiple independent layers prevent invalid operations:

- **`src/utils/guardrails.py`** contains `clean_gst_rate()`, `clean_unit()`, `clean_quantity_for_unit()`, `clean_phone()`, `clean_uuid()`, and `clean_optional_str()`. These run inside every MCP method before any DB call, stripping LLM hallucinations (e.g. `"none"`, `"null"`, `"n/a"`, `"test"`, `"xxx"`) and rejecting invalid values with a `ValueError` that becomes a tool error string visible to the LLM.

- **Cost guard:** The system prompt instructs the agent never to set MRP below cost price, and `update_product_details()` in `CatalogueMCP` validates this at the application layer.

- **Khata settle guard:** `get_customer()` must return a found customer before any `add_payment_entry()` or `add_credit_entry()` call. Tool docstrings mandate this flow. If the customer does not exist, the tool returns an error and no DB write happens.

- **Credit bill phone guard:** `finalize_bill()` in [`billing_mcp.py`](src/mcp/billing/billing_mcp.py) raises a `ValueError` if `is_credit=True` and no `customer_id` is provided — credit cannot be extended without a verified customer on file.

- **Pre-LLM input guards:** [`handler.py`](src/handler.py) rejects messages over 500 characters, matches a prompt-injection regex, runs the scope guard from [`scope_guard.py`](src/utils/scope_guard.py), and enforces a rate limit of 20 messages per 60 seconds — all before the agent is invoked.

- **Immutable audit tables:** DB-level triggers on `billing.bills`, `billing.bill_items`, `inventory.stock_movements`, `khata.khata_entries`, and `payments.payments` prevent any `UPDATE` or `DELETE`. Corrections must be new entries (e.g. a reversal khata entry) — there is no code path that can silently overwrite historical records.

#### 8. Real Artifacts — PDF invoice and PPTX business-analysis deck

Both documents are generated by [`src/mcp/documents/documents_mcp.py`](src/mcp/documents/documents_mcp.py) using `fpdf2` (PDF) and `python-pptx` (PPTX), with imports deferred inside method bodies to reduce Lambda cold-start time.

**PDF invoice:** `generate_invoice_pdf()` loads the finalized bill and its `bill_items` from Supabase (including the stored `taxable_value`, `cgst_amount`, `sgst_amount` per line). It renders a full GST invoice: shop header with GSTIN and address, a 9-column items table (description, HSN, qty, unit, MRP, taxable value, CGST, SGST, total), and a tax summary footer with CGST/SGST subtotals and grand total. A theme system (`_ACTIVE_PDF_THEME` / `_PDF_THEMES` dict) allows global colour changes with a single-line edit. The file is written to Lambda `/tmp`, sent to Telegram via `send_document()`, then immediately deleted.

**PPTX analytics deck:** `generate_analysis_pptx()` pulls data from `AnalyticsMCP` — daily summaries (bills, total sales, cash/UPI/credit breakdown, GST) for a rolling period. Slide 2 is a Daily Summary Table rendered as a proper `python-pptx` table object, not a screenshot. Slide 3 has a bar chart of daily revenue. The file lifecycle is the same: `/tmp` → Telegram → delete.

The agent tool `generate_invoice_pdf` (in [`tool_registry.py`](src/agent/tool_registry.py)) accepts either a human bill number (e.g. `BL-003-20260815-013`) or a UUID — if a bill number is passed, it does a live DB lookup to resolve the UUID before delegating to `DocumentsMCP`.

#### 9. Memory Across Sessions

No preference lives only in the conversation window. Every standing preference is persisted in Supabase and reloaded on every Lambda invocation.

**Store-level preferences** (default payment mode, shop name, GSTIN, address, state code) are stored in `identity.stores` and loaded by `load_agent_context()` in [`context_loader.py`](src/agent/context_loader.py) into `StoreContext`. The system prompt in [`system_prompt.py`](src/agent/system_prompt.py) reads `context.default_payment_mode` and states it explicitly — so a new `/new` chat immediately knows the owner's default without being told again.

**Owner name** is stored in `identity.users.first_name` / `last_name` and loaded into `StoreContext.owner_first_name`. The system prompt addresses the owner by name on every turn.

**Arbitrary key/value preferences** are stored in the `identity.stores.preferences` JSONB column. `update_store_preferences()` in `IdentityMCP` writes to this column. The system prompt renders all preferences in a block so the LLM sees them on every request — e.g. `default_payment_mode: UPI`.

**Active draft bill ID** is stored in `identity.workflow_state.active_draft_bill_id`. If the owner closes Telegram mid-bill, reopens it hours later, and sends a new message, `load_agent_context()` reads the draft ID and the agent resumes adding items to the existing draft — no re-entry needed.

**Workflow state** (`UNREGISTERED` → `PENDING_CATALOGUE` → `PENDING_INVENTORY` → `ACTIVE`) is stored in `identity.workflow_state.current_state`. The self-healing logic in `_self_heal_workflow()` ([`context_loader.py`](src/agent/context_loader.py)) detects and repairs stale states by checking actual catalogue and inventory data — so even if a state transition failed mid-flight, the next request picks up from the correct state.

The only thing stored in Redis is the rolling conversation window (last 20 messages, 24-hour TTL) and the pending payment intent (30-minute TTL). All durable business data — products, stock levels, bills, khata balances, preferences — lives in Supabase and is immune to Redis expiry.
