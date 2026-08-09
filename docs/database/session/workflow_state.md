# Database Table: `workflow_state`

**Domain:** Session  
**MCP Owner:** Identity MCP  
**Schema:** `identity`

---

## Purpose

The `workflow_state` table tracks the **current onboarding and operational state** of each Telegram user. It is the first table read on every Lambda invocation — before the PydanticAI agent is called. The state determines which MCP tools are made available to the agent.

This table is the pre-agent context loader's source of truth. It replaces a complex regex/intent router with a simple state enum read.

---

## Schema

```sql
CREATE TYPE user_workflow_state AS ENUM (
    'UNREGISTERED',         -- User has no registration record yet
    'PENDING_CATALOGUE',    -- Registration complete, no products yet
    'PENDING_INVENTORY',    -- Has products, no stock added yet
    'ACTIVE'                -- Fully operational — all tools available
);

CREATE TABLE public.workflow_state (
    id                      UUID                    PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_user_id        BIGINT                  NOT NULL UNIQUE,
    user_id                 UUID                    REFERENCES public.users(id) ON DELETE RESTRICT,
    store_id                UUID                    REFERENCES public.stores(id) ON DELETE RESTRICT,
    current_state           user_workflow_state     NOT NULL DEFAULT 'UNREGISTERED',
    active_draft_bill_id    UUID                    REFERENCES public.draft_bills(id) ON DELETE SET NULL,
    updated_at              TIMESTAMPTZ             NOT NULL DEFAULT NOW()
);
```

### Column Definitions

| Column | Type | Nullable | Default | Description |
|---|---|---|---|---|
| `id` | UUID | No | `gen_random_uuid()` | Internal primary key. |
| `telegram_user_id` | BIGINT | No | — | Telegram user ID. **Unique** — one workflow state record per user. |
| `user_id` | UUID | Yes | — | FK to `users.id`. NULL while user is UNREGISTERED. Populated when user record is created. |
| `store_id` | UUID | Yes | — | FK to `stores.id`. NULL until store is created. Populated when workflow transitions to PENDING_CATALOGUE. |
| `current_state` | user_workflow_state | No | `'UNREGISTERED'` | Current state in the onboarding/operational flow. |
| `active_draft_bill_id` | UUID | Yes | — | FK to `draft_bills.id`. Non-null when user has an open bill being built. SET NULL when draft is confirmed/cancelled/expired. |
| `updated_at` | TIMESTAMPTZ | No | `NOW()` | Auto-updated on every state transition. |

---

## State Machine

```
First message from user
        ↓
  UNREGISTERED
  (MCP tools: identity only)
  Agent: guides through registration
        ↓ create_store() succeeds
  PENDING_CATALOGUE
  (MCP tools: identity + catalogue)
  Agent: prompts to add first product
        ↓ add_product() called at least once
  PENDING_INVENTORY
  (MCP tools: identity + catalogue + inventory)
  Agent: prompts to add first stock
        ↓ receive_stock() called at least once
  ACTIVE
  (MCP tools: all 7 modules)
  Agent: full store operations available
```

### Tool Exposure Per State

| State | Tools Available |
|---|---|
| `UNREGISTERED` | Identity MCP only |
| `PENDING_CATALOGUE` | Identity MCP + Catalogue MCP |
| `PENDING_INVENTORY` | Identity MCP + Catalogue MCP + Inventory MCP |
| `ACTIVE` | All 7 MCP modules |

---

## Upsert Pattern

The `workflow_state` record is created on first contact and updated on each transition. The Lambda handler uses an upsert:

```sql
INSERT INTO public.workflow_state (telegram_user_id, current_state)
VALUES (?, 'UNREGISTERED')
ON CONFLICT (telegram_user_id) DO NOTHING;

-- Then read the current state
SELECT * FROM public.workflow_state WHERE telegram_user_id = ?;
```

---

## Constraints

```sql
ALTER TABLE public.workflow_state ADD CONSTRAINT workflow_state_pkey PRIMARY KEY (id);
ALTER TABLE public.workflow_state ADD CONSTRAINT workflow_state_telegram_user_id_unique UNIQUE (telegram_user_id);
ALTER TABLE public.workflow_state ADD CONSTRAINT workflow_state_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE RESTRICT;
ALTER TABLE public.workflow_state ADD CONSTRAINT workflow_state_store_id_fkey
    FOREIGN KEY (store_id) REFERENCES public.stores(id) ON DELETE RESTRICT;
ALTER TABLE public.workflow_state ADD CONSTRAINT workflow_state_active_draft_bill_fkey
    FOREIGN KEY (active_draft_bill_id) REFERENCES public.draft_bills(id) ON DELETE SET NULL;

CREATE TRIGGER workflow_state_updated_at
    BEFORE UPDATE ON public.workflow_state
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
```

---

## Indexes

```sql
-- Primary lookup by Telegram user ID (every request)
CREATE UNIQUE INDEX idx_workflow_state_telegram_user_id ON public.workflow_state (telegram_user_id);
```

---

## Row Level Security (RLS)

```sql
ALTER TABLE public.workflow_state ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_full_access" ON public.workflow_state USING (TRUE) WITH CHECK (TRUE);
```

---

## Relations

### Outgoing
| Table | Column | Note |
|---|---|---|
| `users` | `user_id` | User this state belongs to |
| `stores` | `store_id` | Store context |
| `draft_bills` | `active_draft_bill_id` | Currently open bill (nullable) |

---

## Business Rules

1. **First record created with UNREGISTERED:** When a new Telegram user sends their first message, the Lambda handler does an upsert to create the workflow_state record before any other operation.

2. **active_draft_bill_id is the bill session pointer:** The agent uses this to find the current open draft without needing to search. When a bill is finalized or cancelled, this is set to NULL.

3. **state transitions are one-way in Phase 1:** Once ACTIVE, the state never goes back. There is no "downgrade" path.

4. **No conversation history here:** Conversation history lives in Upstash Redis. This table only stores durable operational state that must survive Lambda restarts and Redis TTL expiry.
