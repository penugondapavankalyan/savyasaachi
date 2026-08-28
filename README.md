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
