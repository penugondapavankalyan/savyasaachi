# Database Table: `customers`

**Domain:** Khata  
**MCP Owner:** Khata MCP  
**Schema:** `billing` (customer profiles owned by billing domain)

---

## Purpose

The `customers` table stores the profiles of customers who have a credit account (khata) at a store. Customers are unique per store (not global) — the same person (Ramesh) could exist as a customer at two different stores without any relationship between those records.

A customer must exist in this table before any khata (credit) entry can be made. The Billing MCP requires a `customer_id` to create a credit bill.

---

## Schema

```sql
CREATE TABLE public.customers (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id    UUID        NOT NULL REFERENCES public.stores(id) ON DELETE RESTRICT,
    name        TEXT        NOT NULL,
    phone       TEXT        NOT NULL,
    notes       TEXT,
    is_active   BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### Column Definitions

| Column | Type | Nullable | Default | Description |
|---|---|---|---|---|
| `id` | UUID | No | `gen_random_uuid()` | Internal primary key. |
| `store_id` | UUID | No | — | FK to `stores.id`. Customers are scoped to a store. |
| `name` | TEXT | No | — | Customer's display name. Used by owner in natural language ("Ramesh's balance"). |
| `phone` | TEXT | No | — | Customer's mobile number. Unique per store. Used as the primary deduplication key. |
| `notes` | TEXT | Yes | — | Optional notes about the customer (address, alternate contact, etc.). |
| `is_active` | BOOLEAN | No | `TRUE` | Soft-delete. Inactive customers cannot have new khata entries. |
| `created_at` | TIMESTAMPTZ | No | `NOW()` | Immutable creation timestamp. |
| `updated_at` | TIMESTAMPTZ | No | `NOW()` | Auto-updated via trigger. |

---

## Uniqueness

```sql
-- One customer profile per phone per store
CREATE UNIQUE INDEX idx_customers_store_phone ON public.customers (store_id, phone);

-- Soft uniqueness on name per store (enforced at application layer with warning)
-- Two customers with the same name but different phone numbers are allowed
-- The agent warns: "There's already a Ramesh. Adding as Ramesh (9876543210)"
CREATE INDEX idx_customers_store_name ON public.customers (store_id, LOWER(name));
```

---

## Constraints

```sql
ALTER TABLE public.customers ADD CONSTRAINT customers_pkey PRIMARY KEY (id);
ALTER TABLE public.customers ADD CONSTRAINT customers_store_id_fkey
    FOREIGN KEY (store_id) REFERENCES public.stores(id) ON DELETE RESTRICT;

CREATE TRIGGER customers_updated_at
    BEFORE UPDATE ON public.customers
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
```

---

## Indexes

```sql
CREATE UNIQUE INDEX idx_customers_store_phone ON public.customers (store_id, phone);
CREATE INDEX idx_customers_store_name ON public.customers (store_id, LOWER(name));
CREATE INDEX idx_customers_store_id ON public.customers (store_id);
```

---

## Row Level Security (RLS)

```sql
ALTER TABLE public.customers ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_full_access" ON public.customers USING (TRUE) WITH CHECK (TRUE);
```

---

## Relations

### Outgoing
| Table | Column | Note |
|---|---|---|
| `stores` | `store_id` | Store this customer belongs to |

### Incoming
| Table | FK Column | Note |
|---|---|---|
| `khata_entries` | `customer_id` | All ledger entries for this customer |
| `bills` | `customer_id` | Credit bills for this customer |

---

## Business Rules

1. **Phone as primary dedup key:** If the owner says "add customer Ramesh, 9876543210", the system checks if a customer with phone 9876543210 already exists in this store. If yes, returns the existing record.

2. **Name-only lookup with disambiguation:** The owner often says "Ramesh's balance" without a phone number. The Khata MCP searches by name. If multiple customers have similar names, the agent asks: "Did you mean Ramesh (9876543210) or Ramesh Kumar (9988776655)?"

3. **Guardrail — customer must exist before khata entry:** The Khata MCP refuses to create a `khata_entry` for a non-existent customer. The agent asks the owner to add the customer first.

4. **Soft-delete only:** Setting `is_active = FALSE` prevents new entries but preserves historical balance. The owner can still view the balance of an inactive customer.

---

## Phase 2 Extensibility

| Feature | Migration |
|---|---|
| Payment reminders | `phone` already present — add reminder scheduler |
| Customer email | Add `email TEXT` column |
| Customer credit limit | Add `credit_limit NUMERIC` column |

---

## Example Record

```json
{
  "id": "cust-001",
  "store_id": "store-001",
  "name": "Ramesh Kumar",
  "phone": "9876543210",
  "notes": "Near the temple",
  "is_active": true,
  "created_at": "2024-01-10T10:00:00Z",
  "updated_at": "2024-01-10T10:00:00Z"
}
```
