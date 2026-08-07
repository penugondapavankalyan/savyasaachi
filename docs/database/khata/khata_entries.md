# Database Table: `khata_entries`

**Domain:** Khata  
**MCP Owner:** Khata MCP  
**Schema:** `khata`

---

## Purpose

The `khata_entries` table is the **append-only credit ledger** for each customer at a store. Every credit transaction (customer buys on credit) and every payment (customer pays back) is recorded as a separate entry. The customer's current balance is always computed by summing all entries — it is never stored as a running total to avoid inconsistency.

This design (event-sourcing style ledger) ensures that:
- Every transaction is traceable
- Balance is always computable from the source entries
- No entry can be silently modified to hide a transaction
- Disputes can be resolved by reviewing the full history

---

## Schema

```sql
CREATE TYPE khata_entry_type AS ENUM (
    'CREDIT',       -- Customer bought on credit (shop is owed money)
    'PAYMENT',      -- Customer paid money to the shop
    'ADJUSTMENT'    -- Manual correction (Phase 2)
);

CREATE TABLE public.khata_entries (
    id                  UUID                PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id            UUID                NOT NULL REFERENCES public.stores(id) ON DELETE RESTRICT,
    customer_id         UUID                NOT NULL REFERENCES public.customers(id) ON DELETE RESTRICT,
    entry_type          khata_entry_type    NOT NULL,
    amount_delta        NUMERIC(10,2)       NOT NULL,
    reference_bill_id   UUID                REFERENCES public.bills(id) ON DELETE SET NULL,
    notes               TEXT,
    created_at          TIMESTAMPTZ         NOT NULL DEFAULT NOW()
);
```

### Column Definitions

| Column | Type | Nullable | Default | Description |
|---|---|---|---|---|
| `id` | UUID | No | `gen_random_uuid()` | Internal primary key. Immutable. |
| `store_id` | UUID | No | — | FK to `stores.id`. Scopes entries to a store. |
| `customer_id` | UUID | No | — | FK to `customers.id`. The customer this entry belongs to. |
| `entry_type` | khata_entry_type | No | — | Type of transaction: `CREDIT`, `PAYMENT`, or `ADJUSTMENT`. |
| `amount_delta` | NUMERIC(10,2) | No | — | The signed amount for this entry. **Positive** = customer owes more to the shop. **Negative** = shop owes the customer (payment received / credit note). |
| `reference_bill_id` | UUID | Yes | — | FK to `bills.id`. For `CREDIT` entries, this links to the bill that created the credit. For `PAYMENT`, NULL (payment is not against a specific bill). SET NULL on delete — if a bill is somehow removed, the khata entry is preserved. |
| `notes` | TEXT | Yes | — | Optional notes ("Paid in installment", "Advance payment"). |
| `created_at` | TIMESTAMPTZ | No | `NOW()` | Immutable timestamp. **No `updated_at`** — this table is append-only. |

---

## Sign Convention

| Scenario | entry_type | amount_delta | Meaning |
|---|---|---|---|
| Customer buys ₹200 on credit | `CREDIT` | `+200.00` | Shop is owed ₹200 |
| Customer buys ₹100 on credit | `CREDIT` | `+100.00` | Shop is owed ₹100 more |
| Customer pays ₹500 | `PAYMENT` | `-500.00` | Shop received ₹500 |
| **Balance** | — | **-200.00** | Shop owes customer ₹200 (overpayment) |

**Balance formula:**
```sql
SELECT SUM(amount_delta) as balance
FROM khata_entries
WHERE store_id = ? AND customer_id = ?;
```

Result interpretation:
- `balance > 0` → Customer owes the shop
- `balance < 0` → Shop owes the customer (customer has paid more than they owe)
- `balance = 0` → Account settled

---

## Immutability

```sql
-- Prevent updates and deletes on khata_entries
CREATE OR REPLACE FUNCTION prevent_khata_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'khata_entries are immutable. Records cannot be updated or deleted.';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER no_update_khata_entries
    BEFORE UPDATE ON public.khata_entries
    FOR EACH ROW EXECUTE FUNCTION prevent_khata_mutation();

CREATE TRIGGER no_delete_khata_entries
    BEFORE DELETE ON public.khata_entries
    FOR EACH ROW EXECUTE FUNCTION prevent_khata_mutation();
```

If a mistake is made (e.g., wrong amount entered), the correction is made by creating an `ADJUSTMENT` entry, not by modifying the original. This preserves the full audit trail.

---

## Worked Example

```
Jan 15: Ramesh buys groceries worth ₹200 on credit (bill BL-2024-001)
  → INSERT khata_entry: entry_type=CREDIT, amount_delta=+200.00, reference_bill_id=BL-2024-001

Jan 20: Ramesh buys ₹100 on credit (bill BL-2024-012)
  → INSERT khata_entry: entry_type=CREDIT, amount_delta=+100.00, reference_bill_id=BL-2024-012

Jan 20: Ramesh pays ₹500 cash
  → INSERT khata_entry: entry_type=PAYMENT, amount_delta=-500.00, reference_bill_id=NULL

Balance query: SUM(amount_delta) = 200 + 100 - 500 = -200
Agent response: "Ramesh's balance is -₹200. The shop owes Ramesh ₹200."
```

---

## Constraints

```sql
ALTER TABLE public.khata_entries ADD CONSTRAINT khata_entries_pkey PRIMARY KEY (id);
ALTER TABLE public.khata_entries ADD CONSTRAINT khata_entries_store_id_fkey
    FOREIGN KEY (store_id) REFERENCES public.stores(id) ON DELETE RESTRICT;
ALTER TABLE public.khata_entries ADD CONSTRAINT khata_entries_customer_id_fkey
    FOREIGN KEY (customer_id) REFERENCES public.customers(id) ON DELETE RESTRICT;
ALTER TABLE public.khata_entries ADD CONSTRAINT khata_entries_reference_bill_fkey
    FOREIGN KEY (reference_bill_id) REFERENCES public.bills(id) ON DELETE SET NULL;
```

---

## Indexes

```sql
-- Balance computation and history (most common query)
CREATE INDEX idx_khata_entries_customer ON public.khata_entries (store_id, customer_id, created_at DESC);

-- Link back to bills
CREATE INDEX idx_khata_entries_bill ON public.khata_entries (reference_bill_id)
    WHERE reference_bill_id IS NOT NULL;
```

---

## Row Level Security (RLS)

```sql
ALTER TABLE public.khata_entries ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_full_access" ON public.khata_entries USING (TRUE) WITH CHECK (TRUE);
```

---

## Relations

### Outgoing
| Table | Column | Note |
|---|---|---|
| `stores` | `store_id` | Store this entry belongs to |
| `customers` | `customer_id` | Customer this entry belongs to |
| `bills` | `reference_bill_id` | Bill that caused this entry (nullable) |

---

## Business Rules

1. **Customer must exist before entry:** Khata MCP verifies `customer_id` exists and is active before inserting any entry.
2. **Cannot settle a khata that doesn't exist:** Agent confirms customer existence before processing payment. Returns error if customer not found.
3. **No direct edits:** Mistakes are corrected via `ADJUSTMENT` entries, not record modifications.
4. **CREDIT entries linked to bills:** When a credit bill is finalized, the Billing MCP calls the Khata MCP to create the CREDIT entry with `reference_bill_id`. This ensures the khata entry and the bill are always in sync.
