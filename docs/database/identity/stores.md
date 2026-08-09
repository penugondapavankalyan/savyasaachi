# Database Table: `stores`

**Domain:** Identity  
**MCP Owner:** Identity MCP  
**Schema:** `identity`

---

## Purpose

The `stores` table holds the core profile of each kirana shop registered in the system. It stores shop identification details (name, GSTIN, address), the owner's preferences that persist across sessions, and the default payment mode. Every domain-specific table (products, inventory, bills, khata) is scoped to a `store_id`, making this the multi-tenancy anchor for the entire system.

In Phase 1, one user owns exactly one store.

---

## Schema

```sql
CREATE TABLE public.stores (
    id                      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id           UUID        NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
    shop_name               TEXT        NOT NULL,
    gstin                   TEXT,
    state_code              TEXT        NOT NULL DEFAULT '29',
    address                 TEXT,
    phone                   TEXT,
    default_payment_mode    TEXT        NOT NULL DEFAULT 'CASH'
                                        CHECK (default_payment_mode IN ('CASH', 'UPI', 'CARD')),
    preferences             JSONB       NOT NULL DEFAULT '{}',
    is_active               BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### Column Definitions

| Column | Type | Nullable | Default | Description |
|---|---|---|---|---|
| `id` | UUID | No | `gen_random_uuid()` | Internal primary key. All domain tables use this as `store_id` FK. |
| `owner_user_id` | UUID | No | — | FK to `users.id`. The Telegram user who registered and owns this store. |
| `shop_name` | TEXT | No | — | Display name of the shop. Printed on PDF invoices. |
| `gstin` | TEXT | Yes | — | GST Identification Number (15-character alphanumeric). Optional — not all small kirana stores are GST-registered. |
| `state_code` | TEXT | No | `'29'` | 2-digit state code used for GST computation. Default is 29 (Karnataka). Determines intra-state CGST/SGST split. Phase 2: used for IGST when inter-state. |
| `address` | TEXT | Yes | — | Shop address. Printed on PDF invoices. |
| `phone` | TEXT | Yes | — | Shop contact number. Printed on PDF invoices. |
| `default_payment_mode` | TEXT | No | `'CASH'` | Default assumed payment mode when user does not specify. Owner can change via agent ("always assume UPI unless I say cash"). |
| `preferences` | JSONB | No | `'{}'` | Flexible key-value store for owner preferences that persist across sessions. See preferences schema below. |
| `is_active` | BOOLEAN | No | `TRUE` | Soft-disable flag. |
| `created_at` | TIMESTAMPTZ | No | `NOW()` | Immutable creation timestamp. |
| `updated_at` | TIMESTAMPTZ | No | `NOW()` | Auto-updated via trigger. |

---

## Preferences JSONB Schema

The `preferences` column stores owner preferences that the agent remembers across sessions. These are NOT in Redis (which is cleared on `/new` chat) — they are permanent in Supabase.

```json
{
  "preferred_brands": {
    "atta": "Aashirvaad 5kg",
    "oil": "Fortune Sunflower 1L"
  },
  "invoice_header": "Ramesh General Stores",
  "show_gstin_on_invoice": true,
  "low_stock_alert_enabled": true,
  "language": "en"
}
```

| Key | Type | Description |
|---|---|---|
| `preferred_brands` | Object | Maps generic item names to preferred SKU names. Agent uses these to resolve ambiguous requests ("add atta" → resolves to "Aashirvaad 5kg"). |
| `invoice_header` | String | Custom header for PDF invoices. Defaults to `shop_name` if not set. |
| `show_gstin_on_invoice` | Boolean | Whether to print GSTIN on PDF invoices. |
| `low_stock_alert_enabled` | Boolean | Whether to send Telegram alerts when stock hits reorder level. Default true. |
| `language` | String | Language preference for bot responses. Phase 1: `"en"` only. Phase 2: `"hi"`, `"ta"` etc. |

---

## Constraints

```sql
-- Primary key
ALTER TABLE public.stores ADD CONSTRAINT stores_pkey PRIMARY KEY (id);

-- Phase 1: one store per user
-- IMPORTANT: In Phase 2, this constraint is DROPPED to allow 1:N
ALTER TABLE public.stores ADD CONSTRAINT stores_owner_user_id_unique UNIQUE (owner_user_id);

-- Payment mode must be valid
ALTER TABLE public.stores ADD CONSTRAINT stores_payment_mode_check
    CHECK (default_payment_mode IN ('CASH', 'UPI', 'CARD'));

-- GSTIN format validation (15 alphanumeric characters) — nullable but if provided must be valid
ALTER TABLE public.stores ADD CONSTRAINT stores_gstin_format_check
    CHECK (gstin IS NULL OR gstin ~ '^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$');

-- FK to users
ALTER TABLE public.stores ADD CONSTRAINT stores_owner_user_id_fkey
    FOREIGN KEY (owner_user_id) REFERENCES public.users(id) ON DELETE RESTRICT;

-- updated_at trigger
CREATE TRIGGER stores_updated_at
    BEFORE UPDATE ON public.stores
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
```

---

## Indexes

```sql
-- Primary lookup: find store by owner user ID
CREATE INDEX idx_stores_owner_user_id ON public.stores (owner_user_id);

-- Active stores filter
CREATE INDEX idx_stores_is_active ON public.stores (is_active) WHERE is_active = TRUE;

-- GIN index on preferences JSONB for fast key lookups
CREATE INDEX idx_stores_preferences ON public.stores USING GIN (preferences);
```

---

## Row Level Security (RLS)

```sql
ALTER TABLE public.stores ENABLE ROW LEVEL SECURITY;

-- Full access via service role key (used by Lambda)
CREATE POLICY "service_role_full_access" ON public.stores
    USING (TRUE)
    WITH CHECK (TRUE);
```

---

## Relations

### Outgoing (this table references)

| Table | Column | Type | Note |
|---|---|---|---|
| `users` | `owner_user_id` | Many-to-One | The owner of this store |

### Incoming (other tables reference `stores`)

| Table | FK Column | Cardinality | Note |
|---|---|---|---|
| `registrations` | `store_id` | 1:1 | Registration record that created this store |
| `workflow_state` | `store_id` | 1:1 | User's workflow state for this store |
| `products` | `store_id` | 1:N | All products in this store's catalogue |
| `inventory` | `store_id` | 1:N | All inventory records for this store |
| `stock_movements` | `store_id` | 1:N | All stock movement audit records |
| `draft_bills` | `store_id` | 1:N | All draft bills for this store |
| `bills` | `store_id` | 1:N | All finalized bills for this store |
| `customers` | `store_id` | 1:N | All khata customers for this store |
| `khata_entries` | `store_id` | 1:N | All khata entries for this store |
| `daily_summary` | `store_id` | 1:N | Daily close summaries for this store |

---

## Business Rules

1. **Phase 1 — one store per user:** Enforced by `UNIQUE (owner_user_id)`. The Identity MCP's `create_store` tool checks this constraint and returns a clear error if the user already has a store.

2. **GSTIN is optional:** Many small kirana stores in India are not GST-registered (turnover below ₹20L threshold). If GSTIN is NULL, the PDF invoice omits the GSTIN field but still shows itemized amounts. GST computation on the bill still works regardless.

3. **Preferences persist across sessions:** The `preferences` JSONB is the owner's long-term memory. It is NOT cleared when the user sends `/new` to start a fresh chat. The Redis conversation history is cleared, but Supabase data is never cleared.

4. **default_payment_mode is the agent's default assumption:** When the owner says "cut a bill" without specifying payment method, the agent uses this value. Owner can change it at any time via natural language ("always assume UPI").

5. **state_code for GST:** Phase 1 uses intra-state CGST + SGST. The state_code is stored now so Phase 2 can add IGST support without a schema change.

6. **ON DELETE RESTRICT on owner_user_id:** Cannot delete a user who owns a store. Data integrity is paramount — stores contain financial records.

---

## Phase 2 Extensibility

| Change | Migration Required |
|---|---|
| Multiple stores per user | `DROP CONSTRAINT stores_owner_user_id_unique` — no column change needed |
| Multi-user per store | Add `store_users` join table — `stores` table unchanged |
| IGST support | `state_code` already present — add `gst_type` to `bills` table |
| Custom invoice templates | Add `invoice_template` column or extend `preferences` JSONB |

---

## Example Record

```json
{
  "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "owner_user_id": "550e8400-e29b-41d4-a716-446655440000",
  "shop_name": "Ramesh General Stores",
  "gstin": "29AABCU9603R1ZX",
  "state_code": "29",
  "address": "12, MG Road, Bangalore - 560001",
  "phone": "9876543210",
  "default_payment_mode": "UPI",
  "preferences": {
    "preferred_brands": {
      "atta": "Aashirvaad Atta 5kg",
      "oil": "Fortune Sunflower Oil 1L"
    },
    "invoice_header": "Ramesh General Stores",
    "show_gstin_on_invoice": true,
    "low_stock_alert_enabled": true,
    "language": "en"
  },
  "is_active": true,
  "created_at": "2024-01-15T09:30:00Z",
  "updated_at": "2024-01-15T10:00:00Z"
}
```
