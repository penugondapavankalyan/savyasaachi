# Database Table: `stock_movements`

**Domain:** Inventory  
**MCP Owner:** Inventory MCP  
**Schema:** `inventory`

---

## Purpose

The `stock_movements` table is an **immutable audit trail** of every quantity change to inventory. Every time stock increases (receive stock) or decreases (sale, adjustment), a record is written here. The `inventory` table holds the current state; `stock_movements` holds the full history.

This table is append-only — records are never updated or deleted. It enables:
- Full audit trail for any inventory dispute
- Daily and weekly analytics (sales velocity, top items)
- Reorder suggestion computations (Phase 2)
- Stock reconciliation

---

## Schema

```sql
CREATE TYPE movement_type AS ENUM (
    'STOCK_IN',     -- Stock received from supplier
    'SALE',         -- Stock decremented due to a finalized bill
    'ADJUSTMENT'    -- Manual correction by owner (Phase 2 feature, schema ready)
);

CREATE TABLE public.stock_movements (
    id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id        UUID            NOT NULL REFERENCES public.stores(id) ON DELETE RESTRICT,
    product_id      UUID            NOT NULL REFERENCES public.products(id) ON DELETE RESTRICT,
    movement_type   movement_type   NOT NULL,
    quantity_delta  NUMERIC(10,3)   NOT NULL,
    reference_id    UUID,
    reference_type  TEXT            CHECK (reference_type IN ('BILL', 'STOCK_IN', 'ADJUSTMENT')),
    notes           TEXT,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);
```

### Column Definitions

| Column | Type | Nullable | Default | Description |
|---|---|---|---|---|
| `id` | UUID | No | `gen_random_uuid()` | Internal primary key. Immutable. |
| `store_id` | UUID | No | — | FK to `stores.id`. Scopes movements to a store. |
| `product_id` | UUID | No | — | FK to `products.id`. The product whose stock changed. |
| `movement_type` | movement_type | No | — | Type of movement: `STOCK_IN` (positive), `SALE` (negative), `ADJUSTMENT` (positive or negative). |
| `quantity_delta` | NUMERIC(10,3) | No | — | The signed quantity change. **Positive** for stock-in (e.g., +50). **Negative** for sales (e.g., -4). The sign always reflects the direction of change. |
| `reference_id` | UUID | Yes | — | ID of the entity that caused this movement. For `SALE`: the `bills.id`. For `STOCK_IN`: a stock-in batch UUID. For `ADJUSTMENT`: an adjustment record UUID. |
| `reference_type` | TEXT | Yes | — | Discriminator for `reference_id`: `'BILL'`, `'STOCK_IN'`, or `'ADJUSTMENT'`. |
| `notes` | TEXT | Yes | — | Optional human-readable note. For `ADJUSTMENT`, required description of why adjustment was made. |
| `created_at` | TIMESTAMPTZ | No | `NOW()` | Immutable timestamp of when this movement occurred. **No `updated_at`** — this table is append-only. |

---

## Immutability

This table has **no `updated_at` column** intentionally — records are never modified after creation. To enforce immutability at the DB level:

```sql
-- Revoke UPDATE and DELETE from the application role
-- The service role (used by Lambda) can INSERT but should never UPDATE or DELETE
CREATE OR REPLACE FUNCTION prevent_stock_movement_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'stock_movements records are immutable. No updates or deletes allowed.';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER no_update_stock_movements
    BEFORE UPDATE ON public.stock_movements
    FOR EACH ROW EXECUTE FUNCTION prevent_stock_movement_mutation();

CREATE TRIGGER no_delete_stock_movements
    BEFORE DELETE ON public.stock_movements
    FOR EACH ROW EXECUTE FUNCTION prevent_stock_movement_mutation();
```

---

## Sign Convention

| Movement Type | quantity_delta Sign | Example |
|---|---|---|
| `STOCK_IN` | **Positive (+)** | Received 50 packets of Maggi → `+50` |
| `SALE` | **Negative (-)** | Sold 4 Maggi in a bill → `-4` |
| `ADJUSTMENT` | **Either** | Manual correction: `-2` (found damaged stock), `+3` (found extra stock) |

---

## Relation to `inventory` Table

`stock_movements` and `inventory` work together:

```
inventory.quantity_in_stock = SUM(stock_movements.quantity_delta)
                              WHERE store_id = X AND product_id = Y
```

This invariant is maintained by always writing to both tables in the same transaction. The `inventory` table is the denormalized current state (fast to read). The `stock_movements` table is the normalized history (accurate, auditable).

---

## Analytics Queries Supported

### Daily Sales for a Product
```sql
SELECT SUM(ABS(quantity_delta)) as units_sold
FROM stock_movements
WHERE store_id = ? AND product_id = ? AND movement_type = 'SALE'
  AND created_at::DATE = '2024-01-15';
```

### Top Selling Products (This Week)
```sql
SELECT product_id, SUM(ABS(quantity_delta)) as total_sold
FROM stock_movements
WHERE store_id = ? AND movement_type = 'SALE'
  AND created_at >= NOW() - INTERVAL '7 days'
GROUP BY product_id
ORDER BY total_sold DESC
LIMIT 10;
```

### Stock Velocity (Average Daily Sales for Reorder Suggestions — Phase 2)
```sql
SELECT product_id,
       SUM(ABS(quantity_delta)) / 30.0 as avg_daily_sales
FROM stock_movements
WHERE store_id = ? AND movement_type = 'SALE'
  AND created_at >= NOW() - INTERVAL '30 days'
GROUP BY product_id;
```

---

## Constraints

```sql
-- Primary key
ALTER TABLE public.stock_movements ADD CONSTRAINT stock_movements_pkey PRIMARY KEY (id);

-- FK to stores
ALTER TABLE public.stock_movements ADD CONSTRAINT stock_movements_store_id_fkey
    FOREIGN KEY (store_id) REFERENCES public.stores(id) ON DELETE RESTRICT;

-- FK to products
ALTER TABLE public.stock_movements ADD CONSTRAINT stock_movements_product_id_fkey
    FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE RESTRICT;

-- reference_type must be valid if provided
ALTER TABLE public.stock_movements ADD CONSTRAINT stock_movements_reference_type_check
    CHECK (reference_type IN ('BILL', 'STOCK_IN', 'ADJUSTMENT'));
```

---

## Indexes

```sql
-- Lookup all movements for a product in a store (most common query)
CREATE INDEX idx_stock_movements_store_product ON public.stock_movements (store_id, product_id);

-- Time-range queries for analytics
CREATE INDEX idx_stock_movements_created_at ON public.stock_movements (store_id, created_at DESC);

-- Lookup movements by reference (e.g., all items decremented by a specific bill)
CREATE INDEX idx_stock_movements_reference ON public.stock_movements (reference_id)
    WHERE reference_id IS NOT NULL;

-- Filter by movement type
CREATE INDEX idx_stock_movements_type ON public.stock_movements (store_id, movement_type, created_at DESC);
```

---

## Row Level Security (RLS)

```sql
ALTER TABLE public.stock_movements ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_role_full_access" ON public.stock_movements
    USING (TRUE)
    WITH CHECK (TRUE);
```

---

## Relations

### Outgoing (this table references)

| Table | Column | Type | Note |
|---|---|---|---|
| `stores` | `store_id` | Many-to-One | Store this movement belongs to |
| `products` | `product_id` | Many-to-One | Product whose stock changed |

### Incoming (other tables reference `stock_movements`)
None — this is a terminal audit table.

---

## Business Rules

1. **Always created inside a transaction:** A `SALE` movement is always created in the same DB transaction as the `decrement_stock` operation and the `bills` insert. They either all succeed or all roll back.

2. **Never orphaned:** If a bill is voided or cancelled (only allowed before finalization — drafts can be cancelled), no `SALE` movement is created since stock is only decremented on finalization.

3. **quantity_delta is always the actual quantity moved:** For `SALE` of 4 Maggi, `quantity_delta = -4`. For `STOCK_IN` of 50 Maggi, `quantity_delta = +50`. The Inventory MCP always writes the signed value.

4. **reference_id links back to the cause:** Every `SALE` movement has `reference_id = bill_id`. This allows: "show me all stock decrements from bill #BL-2024-001" or "verify this bill actually decremented stock".

5. **ADJUSTMENT type reserved for Phase 2:** The `ADJUSTMENT` movement type is in the schema but the Inventory MCP does not expose an adjustment tool in Phase 1. The schema is ready so Phase 2 can add manual stock correction without a migration.

---

## Phase 2 Extensibility

| Feature | Migration |
|---|---|
| Batch tracking | Add `batch_id UUID` column; movements linked to specific batches |
| Supplier tracking | Add `supplier_id UUID` column to STOCK_IN movements |
| Sales velocity for reorder | Already queryable from this table, no schema change needed |
| Void/reversal | Add `reversed_by UUID` FK to another stock_movement (the reversal entry) |

---

## Example Records

```json
[
  {
    "id": "sm-001",
    "store_id": "store-001",
    "product_id": "prod-003",
    "movement_type": "STOCK_IN",
    "quantity_delta": 50,
    "reference_id": "stockin-001",
    "reference_type": "STOCK_IN",
    "notes": "Received from distributor",
    "created_at": "2024-01-15T08:00:00Z"
  },
  {
    "id": "sm-002",
    "store_id": "store-001",
    "product_id": "prod-003",
    "movement_type": "SALE",
    "quantity_delta": -4,
    "reference_id": "bill-001",
    "reference_type": "BILL",
    "notes": null,
    "created_at": "2024-01-15T10:30:00Z"
  }
]
```
