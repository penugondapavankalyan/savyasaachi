# Database Table: `draft_bills`

**Domain:** Billing  
**MCP Owner:** Billing MCP  
**Schema:** `billing`

---

## Purpose

The `draft_bills` table stores bills that are currently being built across one or more Telegram messages. It represents the **in-progress, mutable state** of a billing session. A draft bill is created when the owner starts mentioning items to bill and is either confirmed (finalized into a `bills` record) or cancelled.

Draft bills are the mechanism that enables **multi-turn billing** — the owner can add items at 9:00 AM, edit them at 9:10 AM, and finalize at 9:15 AM, all within the same draft bill linked by `workflow_id`.

---

## Schema

```sql
CREATE TYPE draft_bill_status AS ENUM (
    'OPEN',         -- Bill is actively being built
    'CONFIRMED',    -- Bill has been finalized and a bills record was created
    'CANCELLED',    -- Bill was cancelled before finalization
    'EXPIRED'       -- Bill was not acted on within the TTL window
);

CREATE TABLE public.draft_bills (
    id                  UUID                PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id            UUID                NOT NULL REFERENCES public.stores(id) ON DELETE RESTRICT,
    telegram_user_id    BIGINT              NOT NULL,
    workflow_id         UUID                NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    status              draft_bill_status   NOT NULL DEFAULT 'OPEN',
    expires_at          TIMESTAMPTZ         NOT NULL DEFAULT (NOW() + INTERVAL '4 hours'),
    created_at          TIMESTAMPTZ         NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ         NOT NULL DEFAULT NOW()
);
```

### Column Definitions

| Column | Type | Nullable | Default | Description |
|---|---|---|---|---|
| `id` | UUID | No | `gen_random_uuid()` | Internal primary key. |
| `store_id` | UUID | No | — | FK to `stores.id`. The store this bill belongs to. |
| `telegram_user_id` | BIGINT | No | — | Telegram user ID of the owner building this bill. Denormalized for fast lookup. |
| `workflow_id` | UUID | No | `gen_random_uuid()` | Business-level session identifier. Links multiple messages to the same bill. Also used for idempotency — the final `bills` record carries the same `workflow_id`. **Unique** across all draft bills. |
| `status` | draft_bill_status | No | `'OPEN'` | Current status of the draft. Only `OPEN` drafts can have items added. |
| `expires_at` | TIMESTAMPTZ | No | `NOW() + 4 hours` | TTL for the draft. An expired draft cannot be finalized. The agent detects expiry and prompts to start a new bill. |
| `created_at` | TIMESTAMPTZ | No | `NOW()` | Immutable creation timestamp. |
| `updated_at` | TIMESTAMPTZ | No | `NOW()` | Auto-updated via trigger. |

---

## Lifecycle

```
Owner mentions items to add to bill
        ↓
[OPEN] — draft_bill created, draft_bill_items accumulated
        ↓ (owner says "done", "pay by UPI", "finalize", etc.)
[CONFIRMED] — finalize_bill() called:
  • bills record created with same workflow_id
  • bill_items records created
  • inventory decremented atomically
  • draft_bills.status → CONFIRMED

        OR

[CANCELLED] — owner says "cancel the bill" / "start over"
  • draft_bill_items deleted
  • draft_bills.status → CANCELLED
  • No stock change

        OR

[EXPIRED] — draft not touched for 4 hours
  • Detected when agent checks active_draft_bill_id
  • draft_bills.status → EXPIRED
  • Agent informs owner: "Your previous bill expired. Starting fresh."
```

---

## workflow_id: The Multi-Turn Bill Key

The `workflow_id` is the mechanism that ties a billing conversation across time gaps:

```
9:00 AM  message: "2kg sugar, 1 Aashirvaad atta"
         → workflow_state.active_draft_bill_id is NULL
         → create_draft_bill() → new draft, workflow_id = abc-123
         → workflow_state.active_draft_bill_id = draft-bill-uuid
         → add items to draft

9:10 AM  message: "also 4 Maggi"
         → workflow_state.active_draft_bill_id = draft-bill-uuid (from Supabase)
         → get existing draft_bill with status=OPEN
         → add_item_to_draft()

9:12 AM  message: "UPI, that's it"
         → workflow_state.active_draft_bill_id = draft-bill-uuid
         → finalize_bill()
         → draft_bills.status → CONFIRMED
         → workflow_state.active_draft_bill_id → NULL
```

---

## Idempotency

The `workflow_id` unique constraint on **both** `draft_bills` and `bills` ensures finalization is idempotent:

```python
# Billing MCP finalize_bill() pseudocode
def finalize_bill(draft_bill_id, payment_mode, ...):
    draft = get_draft_bill(draft_bill_id)
    
    # Idempotency check: has this draft already been finalized?
    existing_bill = get_bill_by_workflow_id(draft.workflow_id)
    if existing_bill:
        return existing_bill  # Return existing bill, no re-processing
    
    # Otherwise, finalize in a DB transaction
    with transaction():
        create_bill(workflow_id=draft.workflow_id, ...)
        create_bill_items(...)
        decrement_stock_for_all_items(...)
        update draft_bills.status = CONFIRMED
```

---

## Constraints

```sql
ALTER TABLE public.draft_bills ADD CONSTRAINT draft_bills_pkey PRIMARY KEY (id);

ALTER TABLE public.draft_bills ADD CONSTRAINT draft_bills_workflow_id_unique UNIQUE (workflow_id);

ALTER TABLE public.draft_bills ADD CONSTRAINT draft_bills_store_id_fkey
    FOREIGN KEY (store_id) REFERENCES public.stores(id) ON DELETE RESTRICT;

CREATE TRIGGER draft_bills_updated_at
    BEFORE UPDATE ON public.draft_bills
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
```

---

## Indexes

```sql
-- Find active draft for a user (most common query)
CREATE INDEX idx_draft_bills_user_open ON public.draft_bills (telegram_user_id, status)
    WHERE status = 'OPEN';

-- Lookup by workflow_id (for idempotency checks)
CREATE UNIQUE INDEX idx_draft_bills_workflow_id ON public.draft_bills (workflow_id);

-- Store-level queries (admin/analytics)
CREATE INDEX idx_draft_bills_store_id ON public.draft_bills (store_id, created_at DESC);

-- Expiry cleanup (find expired drafts)
CREATE INDEX idx_draft_bills_expires_at ON public.draft_bills (expires_at)
    WHERE status = 'OPEN';
```

---

## Row Level Security (RLS)

```sql
ALTER TABLE public.draft_bills ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_full_access" ON public.draft_bills USING (TRUE) WITH CHECK (TRUE);
```

---

## Relations

### Outgoing
| Table | Column | Type | Note |
|---|---|---|---|
| `stores` | `store_id` | Many-to-One | Store this draft belongs to |

### Incoming
| Table | FK Column | Note |
|---|---|---|
| `draft_bill_items` | `draft_bill_id` | Line items for this draft |
| `workflow_state` | `active_draft_bill_id` | Current user's active draft |
