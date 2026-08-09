# Kirana Store Agent — System Architecture

## Overview

The system is a serverless, event-driven conversational agent. A Telegram message is the only trigger that causes any computation. When idle, nothing runs and nothing costs money except Supabase (always-on managed PostgreSQL) and Upstash Redis (serverless, pay-per-request).

---

## Component Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            TELEGRAM USER                                    │
└─────────────────────────────────┬───────────────────────────────────────────┘
                                  │ HTTPS POST (webhook)
                                  ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                     AWS LAMBDA (Python 3.12)                                │
│                     Lambda Function URL                                     │
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  1. handler.py — Lambda entry point                                 │   │
│  │     • Validates Telegram webhook payload                            │   │
│  │     • Extracts telegram_user_id + message text                      │   │
│  │     • Calls workflow state loader                                   │   │
│  └──────────────────────────────┬──────────────────────────────────────┘   │
│                                  │                                          │
│  ┌───────────────────────────────▼──────────────────────────────────────┐  │
│  │  2. Workflow State Loader                                            │  │
│  │     • Reads workflow_state from Supabase                            │  │
│  │     • Loads conversation history from Upstash Redis                 │  │
│  │     • Selects which MCP tool subsets to expose to the agent         │  │
│  │     • Builds system prompt with store context                       │  │
│  └──────────────────────────────┬───────────────────────────────────────┘  │
│                                  │                                          │
│  ┌───────────────────────────────▼───────────────────────────────────────┐ │
│  │  3. PydanticAI Agent (Ollama dev / Groq prod)                       │ │
│  │     Observe → Reason → Act (tool call) → Feed result back → Continue│ │
│  │                                                                     │ │
│  │   Available MCP Tool Modules (co-deployed Python modules):          │ │
│  │   ┌──────────┐ ┌───────────┐ ┌───────────┐ ┌──────────┐           │ │
│  │   │ Identity │ │ Catalogue │ │ Inventory │ │ Billing  │           │ │
│  │   │   MCP    │ │    MCP    │ │    MCP    │ │   MCP    │           │ │
│  │   └──────────┘ └───────────┘ └───────────┘ └──────────┘           │ │
│  │   ┌──────────┐ ┌───────────┐ ┌───────────┐                        │ │
│  │   │  Khata   │ │ Analytics │ │ Documents │                        │ │
│  │   │   MCP    │ │    MCP    │ │    MCP    │                        │ │
│  │   └──────────┘ └───────────┘ └───────────┘                        │ │
│  └──────────────────────────────┬───────────────────────────────────────┘ │
│                                  │                                          │
│  ┌───────────────────────────────▼───────────────────────────────────────┐ │
│  │  4. Response Handler                                                 │ │
│  │     • Saves updated conversation history to Upstash Redis           │ │
│  │     • Sends response text / file to Telegram                        │ │
│  └───────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────┘
         │                              │
         ▼                              ▼
┌─────────────────────┐    ┌────────────────────────┐
│   SUPABASE          │    │   UPSTASH REDIS         │
│   (PostgreSQL)      │    │   (Serverless)          │
│                     │    │                         │
│ • users             │    │ • conv:{user_id}        │
│ • stores            │    │   → JSON list of msgs   │
│ • registrations     │    │   → TTL: 24 hours       │
│ • workflow_state    │    │                         │
│ • products          │    │ Cleared on /new chat    │
│ • inventory         │    │ Does NOT store prefs    │
│ • stock_movements   │    │ (prefs live in Supabase)│
│ • draft_bills       │    └────────────────────────┘
│ • draft_bill_items  │
│ • bills             │
│ • bill_items        │
│ • customers         │
│ • khata_entries     │
│ • daily_summary     │
└─────────────────────┘
```

---

## Request Lifecycle

### Step-by-Step: A Message Arrives

```
1. Telegram sends POST to Lambda Function URL
   Body: { update_id, message: { from: { id: 123 }, text: "2kg sugar, 1 Aashirvaad atta" } }

2. handler.py validates the payload
   - Verify it is a message update (not edited_message, inline_query, etc.)
   - Extract telegram_user_id = 123, text = "2kg sugar, 1 Aashirvaad atta"

3. Workflow State Loader runs (no LLM call yet)
   - Query Supabase: SELECT current_state FROM workflow_state WHERE telegram_user_id = 123
   - Result: ACTIVE
   - Load last 20 messages from Upstash Redis key "conv:123"
   - Load store context: shop_name, gstin, preferences from Supabase stores table
   - Select tool subset: ACTIVE state → all MCP tools available
   - Build system prompt: inject store_name, gstin, default_payment_mode, current date

4. PydanticAI Agent is invoked
   - Input: system_prompt + conversation_history + user_message
   - Model: Groq (prod) or Ollama (dev)
   - Agent reasons: "user wants to add items to a bill"
   - Agent calls tool: billing_mcp.create_draft_bill(store_id, telegram_user_id, workflow_id)
   - Tool result returned to agent
   - Agent calls tool: billing_mcp.add_item_to_draft(draft_bill_id, product_id="sugar", qty=2)
   - Tool internally calls: inventory_mcp.check_availability(store_id, "sugar", 2) → OK
   - Agent calls tool: billing_mcp.add_item_to_draft(draft_bill_id, product_id="aashirvaad_atta_5kg", qty=1)
   - Agent composes response: "Added to bill: 2kg Sugar, 1 Aashirvaad Atta 5kg. Anything else?"

5. Response Handler
   - Append user message + agent response to Upstash Redis "conv:123" (with TTL reset)
   - Send response text to Telegram via sendMessage API

6. Lambda returns 200 OK to Telegram
```

---

## Workflow State Machine

The workflow state determines which MCP tools are exposed to the agent. This is the **pre-agent context loader** — not an intent router. The model still reasons freely within the available tools.

```
┌─────────────────┐
│  UNREGISTERED   │  Tools: identity_mcp only
│                 │  Prompt: ask user to register
└────────┬────────┘
         │ register_user() + create_store() succeed
         ▼
┌─────────────────────┐
│  PENDING_CATALOGUE  │  Tools: identity_mcp + catalogue_mcp
│                     │  Prompt: ask user to add products
└──────────┬──────────┘
           │ add_product() called at least once
           ▼
┌─────────────────────┐
│  PENDING_INVENTORY  │  Tools: identity_mcp + catalogue_mcp + inventory_mcp
│                     │  Prompt: ask user to add stock
└──────────┬──────────┘
           │ receive_stock() called at least once
           ▼
┌──────────────┐
│    ACTIVE    │  Tools: all 7 MCP modules
│              │  Full store operations available
└──────────────┘
```

---

## MCP Module Ownership Map

Each MCP module owns specific tables and is the **only** module that writes to those tables. Other modules may read across boundaries but never write outside their own tables.

| MCP Module | Owns (Write) | Reads From |
|---|---|---|
| **Identity MCP** | users, stores, registrations, workflow_state | — |
| **Catalogue MCP** | products | stores (read) |
| **Inventory MCP** | inventory, stock_movements | products, bills (read) |
| **Billing MCP** | draft_bills, draft_bill_items, bills, bill_items | products, inventory (via inventory_mcp), customers |
| **Khata MCP** | customers, khata_entries | bills (read) |
| **Analytics MCP** | daily_summary | bills, bill_items, inventory, stock_movements (read) |
| **Documents MCP** | none (generates files, no DB writes) | bills, bill_items, products, analytics data |

---

## Multi-Turn Bill Tracking (workflow_id)

The `workflow_id` is a UUID generated at the start of a billing session. It links all messages in a bill-building conversation, even across time gaps.

```
9:00 AM  user: "2kg sugar, 1 Aashirvaad atta"
         → agent creates draft_bill with workflow_id=abc123
         → adds 2 items to draft

9:10 AM  user: "also add 4 Maggi"
         → agent finds existing open draft_bill with workflow_id=abc123 (same session)
         → adds 1 more item

9:12 AM  user: "that's it, pay by UPI"
         → agent finalizes draft_bill abc123
         → computes GST, creates bill record
         → decrements stock for all 3 items atomically
         → returns bill summary + confirms
```

The `workflow_id` is stored in the conversation history (Upstash Redis) and in the `draft_bills` table. The agent retrieves the active `draft_bill_id` from `workflow_state.active_draft_bill_id`.

---

## Idempotency Design

Telegram can redeliver webhook updates. Every state-changing operation is idempotent:

| Operation | Idempotency Mechanism |
|---|---|
| User registration | Unique constraint on `users.telegram_user_id` — second attempt returns existing |
| Store creation | Unique constraint on `stores.owner_user_id` — second attempt returns existing |
| Draft bill creation | `create_draft_bill` is idempotent on `workflow_id` — returns existing open draft |
| Bill finalization | Unique constraint on `bills.workflow_id` — retried finalize returns existing bill |
| Stock decrement | Wrapped in DB transaction with bill creation — atomic, not repeated |

---

## ACID Guarantees

Critical operations use Supabase PostgreSQL transactions:

### Bill Finalization Transaction
```sql
BEGIN;
  INSERT INTO bills (...) VALUES (...);           -- create bill record
  INSERT INTO bill_items (...) VALUES (...);      -- create all line items
  UPDATE inventory SET quantity = quantity - N    -- decrement each product
    WHERE product_id = X AND store_id = Y
    AND quantity >= N;                            -- guard against oversell
  INSERT INTO stock_movements (...) VALUES (...); -- audit trail
  UPDATE draft_bills SET status = 'CONFIRMED';   -- mark draft done
COMMIT;
```

If any step fails (e.g. stock went to 0 between check and decrement), the entire transaction rolls back. No partial state.

### Concurrency Control
- Inventory decrements use `SELECT ... FOR UPDATE` or a Supabase RPC with row-level locking
- Two simultaneous bills cannot both decrement the same product below zero
- The DB-level `CHECK (quantity >= 0)` constraint is the final guard

---

## Data Flow: Reorder Alert

```
finalize_bill()
    ↓
decrement_stock() called for each item
    ↓
After each decrement:
  SELECT quantity, reorder_level FROM inventory WHERE product_id = X
    ↓ if quantity <= reorder_level
  Reorder event triggered (in-process, Phase 1)
    ↓
Telegram message sent to owner:
  "⚠️ Low stock alert: Maggi is at 3 packets (reorder level: 5). Time to restock!"
```

---

## Conversation History Design (Upstash Redis)

```
Key:   conv:{telegram_user_id}
Value: JSON array of message objects
TTL:   86400 seconds (24 hours, reset on each new message)

Message object:
{
  "role": "user" | "assistant",
  "content": "string",
  "timestamp": "ISO-8601"
}

Loading strategy:
  - Load last 20 messages (windowed context)
  - Prevents prompt size from growing unbounded
  - Older history is effectively summarized by the model's responses

/new chat command:
  - DEL conv:{telegram_user_id} in Upstash Redis
  - Does NOT touch Supabase — store data, preferences, bills, khata persist
```

---

## Environment Variables

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram Bot API token |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase service role key (bypasses RLS for server-side ops) |
| `UPSTASH_REDIS_REST_URL` | Upstash Redis REST endpoint |
| `UPSTASH_REDIS_REST_TOKEN` | Upstash Redis auth token |
| `GROQ_API_KEY` | Groq API key (prod LLM) |
| `OLLAMA_BASE_URL` | Ollama endpoint (dev only) |
| `LLM_PROVIDER` | `"groq"` or `"ollama"` |
| `LLM_MODEL` | Model name (e.g. `"llama-3.1-70b-versatile"` for Groq) |
| `LAMBDA_ENV` | `"dev"` or `"prod"` |


## Domain Schema Map

All tables live in dedicated PostgreSQL schemas that mirror the MCP module boundaries. Trigger functions and RPCs live in `public` so they are accessible across all schemas.

```
public                    ← trigger functions, RPCs only (no tables)
├── update_updated_at_column()
├── prevent_immutable_record_mutation()
├── enforce_loose_item_gst()
├── upsert_workflow_state()
├── generate_bill_number()
├── decrement_stock()
├── increment_stock()
└── get_customer_balance()

identity                  ← owned by Identity MCP
├── users
├── stores
├── registrations
└── workflow_state

catalogue                 ← owned by Catalogue MCP
└── products

inventory                 ← owned by Inventory MCP
├── inventory
└── stock_movements

billing                   ← owned by Billing MCP
├── draft_bills
├── draft_bill_items
├── customers             ← customer profiles; also read by Khata MCP
├── bills
└── bill_items

khata                     ← owned by Khata MCP
└── khata_entries

analytics                 ← owned by Analytics MCP
└── daily_summary
```

### Supabase: Exposing Non-public Schemas

Supabase's PostgREST (REST API) only exposes `public` by default. To expose all domain schemas:

1. Go to **Supabase Dashboard → Project Settings → API**
2. Under **"Exposed schemas"**, add each schema name:
   ```
   identity, catalogue, inventory, billing, khata, analytics
   ```
3. Click Save (this restarts PostgREST automatically)

### Python Client Usage

With domain schemas, the Supabase Python client uses the `schema()` selector:

```python
# Reading from identity schema
user = supabase.schema('identity').table('users') \
    .select('*').eq('telegram_user_id', 123).execute()

# Reading from billing schema
bill = supabase.schema('billing').table('bills') \
    .select('*, bill_items(*)').eq('id', bill_id).execute()

# Calling an RPC (always in public — no schema selector needed)
result = supabase.rpc('decrement_stock', {
    'p_store_id': store_id,
    'p_product_id': product_id,
    'p_quantity': 4,
    'p_bill_id': bill_id
}).execute()
```

### Cross-Schema Foreign Keys

PostgreSQL supports FKs across schemas natively. The key cross-schema FKs in this system:

| From | To | Note |
|---|---|---|
| `identity.stores.owner_user_id` | `identity.users.id` | Same schema |
| `identity.workflow_state.active_draft_bill_id` | `billing.draft_bills.id` | Cross-schema |
| `catalogue.products.store_id` | `identity.stores.id` | Cross-schema |
| `inventory.inventory.product_id` | `catalogue.products.id` | Cross-schema |
| `billing.bills.customer_id` | `billing.customers.id` | Same schema |
| `billing.bill_items.product_id` | `catalogue.products.id` | Cross-schema |
| `khata.khata_entries.customer_id` | `billing.customers.id` | Cross-schema |
| `khata.khata_entries.reference_bill_id` | `billing.bills.id` | Cross-schema |
| `analytics.daily_summary.store_id` | `identity.stores.id` | Cross-schema |

---


---

## Scalability Notes

### Phase 1 → Phase 2 Extraction Path
Each MCP Python module has a clean function interface. To extract any module into its own Lambda in Phase 2:
1. Create a new Lambda with the module's code
2. Expose it via an HTTP endpoint (Lambda Function URL or API Gateway)
3. Replace the in-process Python function call with an HTTP client call
4. The agent tool signature does not change — only the transport layer changes

### Database Scaling
- Supabase handles connection pooling via pgBouncer (transaction mode for Lambda)
- RLS (Row Level Security) policies enforce store-level data isolation
- All tables include `store_id` for future multi-tenant sharding

### Agent Scaling
- PydanticAI agent is stateless — each Lambda invocation is independent
- Conversation history is externalized to Upstash Redis
- Lambda auto-scales horizontally; no shared in-process state
