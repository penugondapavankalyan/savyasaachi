# Database Table: `draft_bill_items`

**Domain:** Billing  
**MCP Owner:** Billing MCP  
**Schema:** `billing`

---

## Purpose

The `draft_bill_items` table holds the line items for a bill that is currently being built (its parent `draft_bills` record has `status = 'OPEN'`). These records are **mutable** — items can be added, removed, or updated while the draft is open. They become immutable once the draft is confirmed and the corresponding `bill_items` records are created.

---

## Schema

```sql
CREATE TABLE public.draft_bill_items (
    id                      UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    draft_bill_id           UUID            NOT NULL REFERENCES public.draft_bills(id) ON DELETE CASCADE,
    product_id              UUID            NOT NULL REFERENCES public.products(id) ON DELETE RESTRICT,
    quantity                NUMERIC(10,3)   NOT NULL CHECK (quantity > 0),
    unit_price              NUMERIC(10,2)   NOT NULL CHECK (unit_price >= 0),
    gst_rate                NUMERIC(5,2)    NOT NULL DEFAULT 0.00 CHECK (gst_rate >= 0 AND gst_rate <= 28),
    is_partial_fulfillment  BOOLEAN         NOT NULL DEFAULT FALSE,
    available_quantity      NUMERIC(10,3),
    created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);
```

### Column Definitions

| Column | Type | Nullable | Default | Description |
|---|---|---|---|---|
| `id` | UUID | No | `gen_random_uuid()` | Internal primary key. |
| `draft_bill_id` | UUID | No | — | FK to `draft_bills.id`. Cascade delete — when draft is cancelled, items are deleted. |
| `product_id` | UUID | No | — | FK to `products.id`. The product being billed. |
| `quantity` | NUMERIC(10,3) | No | — | Quantity to bill. Must be positive. Uses 3 decimal places for fractional units. |
| `unit_price` | NUMERIC(10,2) | No | — | Price per unit at the time of adding to draft (copied from `products.mrp`). Snapshot to handle price changes before finalization. |
| `gst_rate` | NUMERIC(5,2) | No | `0.00` | GST rate at the time of adding to draft (copied from `products.gst_rate`). Snapshotted to preserve accuracy. |
| `is_partial_fulfillment` | BOOLEAN | No | `FALSE` | TRUE if the owner confirmed partial fulfillment (less than requested due to low stock). |
| `available_quantity` | NUMERIC(10,3) | Yes | — | If `is_partial_fulfillment = TRUE`, stores the originally requested quantity for context. |
| `created_at` | TIMESTAMPTZ | No | `NOW()` | When this line item was added to the draft. |
| `updated_at` | TIMESTAMPTZ | No | `NOW()` | Auto-updated when quantity is changed. |

---

## Constraints

```sql
ALTER TABLE public.draft_bill_items ADD CONSTRAINT draft_bill_items_pkey PRIMARY KEY (id);

ALTER TABLE public.draft_bill_items ADD CONSTRAINT draft_bill_items_draft_bill_id_fkey
    FOREIGN KEY (draft_bill_id) REFERENCES public.draft_bills(id) ON DELETE CASCADE;

ALTER TABLE public.draft_bill_items ADD CONSTRAINT draft_bill_items_product_id_fkey
    FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE RESTRICT;

-- One line item per product per draft (upsert on add)
CREATE UNIQUE INDEX idx_draft_bill_items_draft_product
    ON public.draft_bill_items (draft_bill_id, product_id);

ALTER TABLE public.draft_bill_items ADD CONSTRAINT draft_bill_items_quantity_positive
    CHECK (quantity > 0);

CREATE TRIGGER draft_bill_items_updated_at
    BEFORE UPDATE ON public.draft_bill_items
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
```

---

## Indexes

```sql
-- Fetch all items for a draft (used on every draft view)
CREATE INDEX idx_draft_bill_items_draft_bill_id ON public.draft_bill_items (draft_bill_id);

-- Unique: one line per product per draft
CREATE UNIQUE INDEX idx_draft_bill_items_draft_product ON public.draft_bill_items (draft_bill_id, product_id);
```

---

## Row Level Security (RLS)

```sql
ALTER TABLE public.draft_bill_items ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_full_access" ON public.draft_bill_items USING (TRUE) WITH CHECK (TRUE);
```

---

## Business Rules

1. **One item per product per draft:** Enforced by unique index on `(draft_bill_id, product_id)`. Adding the same product again updates the quantity rather than creating a duplicate line.

2. **Prices are snapshotted:** `unit_price` and `gst_rate` are copied from `products` when the item is added. If the owner updates the product price between adding to draft and finalizing, the draft preserves the original price. The owner is shown the prices in the draft summary before finalizing.

3. **CASCADE DELETE:** When a `draft_bills` record is cancelled or deleted, all its `draft_bill_items` are automatically deleted via CASCADE. No orphaned items.

4. **Partial fulfillment tracking:** When an owner accepts partial fulfillment, `is_partial_fulfillment = TRUE` and `quantity` holds the actual (reduced) quantity. The `available_quantity` column stores what was originally requested, for display in the bill summary.
