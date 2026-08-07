# Database Table: `inventory`

**Domain:** Inventory  
**MCP Owner:** Inventory MCP  
**Schema:** `inventory`

---

## Purpose

The `inventory` table tracks the **current stock quantity** for every product in a store. It is the live, always-current record of how much of each product is on the shelf. Every stock-in (receiving stock) increments this table. Every sale (bill finalization) decrements it atomically.

This table does not record history — that is the job of `stock_movements`. The `inventory` table only holds the current state.

---

## Schema

```sql
CREATE TABLE public.inventory (
    id                  UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id            UUID            NOT NULL REFERENCES public.stores(id) ON DELETE RESTRICT,
    product_id          UUID            NOT NULL REFERENCES public.products(id) ON DELETE RESTRICT,
    quantity_in_stock   NUMERIC(10,3)   NOT NULL DEFAULT 0
                                        CHECK (quantity_in_stock >= 0),
    reorder_level       NUMERIC(10,3)   NOT NULL DEFAULT 0
                                        CHECK (reorder_level >= 0),
    last_restocked_at   TIMESTAMPTZ,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);
```

### Column Definitions

| Column | Type | Nullable | Default | Description |
|---|---|---|---|---|
| `id` | UUID | No | `gen_random_uuid()` | Internal primary key. |
| `store_id` | UUID | No | — | FK to `stores.id`. Scopes inventory to a store. |
| `product_id` | UUID | No | — | FK to `products.id`. The product whose stock is tracked. |
| `quantity_in_stock` | NUMERIC(10,3) | No | `0` | Current quantity available. Uses 3 decimal places to support fractional units (e.g., 2.5 kg). **Cannot go below 0** (DB-level CHECK constraint). |
| `reorder_level` | NUMERIC(10,3) | No | `0` | When `quantity_in_stock` drops to or below this value, a reorder alert is triggered. Copied from `products.reorder_level` at stock-in time but can be updated independently. |
| `last_restocked_at` | TIMESTAMPTZ | Yes | — | Timestamp of the most recent stock-in operation. Used for analytics (days since last restock). |
| `created_at` | TIMESTAMPTZ | No | `NOW()` | Immutable creation timestamp. |
| `updated_at` | TIMESTAMPTZ | No | `NOW()` | Auto-updated via trigger. |

---

## Atomic Decrement Pattern

Stock decrements **must be atomic** to prevent overselling when two bills are being built simultaneously. The system uses a PostgreSQL row-level lock via `SELECT ... FOR UPDATE`.

### Supabase RPC for Atomic Decrement

```sql
CREATE OR REPLACE FUNCTION decrement_stock(
    p_store_id      UUID,
    p_product_id    UUID,
    p_quantity      NUMERIC,
    p_bill_id       UUID
)
RETURNS JSONB AS $$
DECLARE
    v_current_qty   NUMERIC;
    v_reorder_lvl   NUMERIC;
    v_inv_id        UUID;
BEGIN
    -- Lock the inventory row for this product (prevents concurrent decrements)
    SELECT id, quantity_in_stock, reorder_level
    INTO v_inv_id, v_current_qty, v_reorder_lvl
    FROM public.inventory
    WHERE store_id = p_store_id AND product_id = p_product_id
    FOR UPDATE;

    -- Oversell guard
    IF v_current_qty < p_quantity THEN
        RAISE EXCEPTION 'Insufficient stock. Available: %, Requested: %',
            v_current_qty, p_quantity;
    END IF;

    -- Decrement
    UPDATE public.inventory
    SET quantity_in_stock = quantity_in_stock - p_quantity,
        updated_at = NOW()
    WHERE id = v_inv_id;

    -- Create stock movement audit record
    INSERT INTO public.stock_movements (store_id, product_id, movement_type, quantity_delta, reference_id, reference_type)
    VALUES (p_store_id, p_product_id, 'SALE', -p_quantity, p_bill_id, 'BILL');

    -- Return new quantity and whether reorder alert should fire
    RETURN jsonb_build_object(
        'new_quantity', v_current_qty - p_quantity,
        'reorder_alert', (v_current_qty - p_quantity) <= v_reorder_lvl
    );
END;
$$ LANGUAGE plpgsql;
```

This RPC is called within the `finalize_bill` transaction so that:
1. The row lock is held only for the duration of the decrement
2. The stock movement record is created in the same transaction
3. The reorder alert flag is returned to the calling code

---

## Oversell Guard (Defense in Depth)

Stock cannot go negative. This is enforced at **three levels**:

| Level | Mechanism | Description |
|---|---|---|
| **Tool layer** | `check_availability()` | Before adding to bill, checks if qty is available. Returns partial/none if insufficient. |
| **RPC layer** | `decrement_stock()` raises exception | If concurrent request already decremented stock, RPC raises exception and rolls back transaction. |
| **DB constraint** | `CHECK (quantity_in_stock >= 0)` | Final line of defense — DB rejects any UPDATE that would make quantity negative. |

---

## Partial Fulfillment Flow

When a customer requests a quantity that exceeds available stock:

```
Owner: "2kg Aashirvaad Atta, 3kg Sugar, 1 Maggi"

Agent calls: check_availability(store_id, sugar_product_id, 3)
Result: { available: 1.5, status: "PARTIAL", can_partially_fulfill: true }

Agent responds: "⚠️ Only 1.5kg of Sugar is available (requested 3kg).
Would you like to add 1.5kg instead, or skip Sugar from this bill?"

Owner: "add 1.5kg"
Agent calls: add_item_to_draft(draft_bill_id, sugar_product_id, 1.5, is_partial=true)
```

If `available = 0`:
```
Agent responds: "❌ Sugar is out of stock. It will be skipped from the bill.
Would you like to continue with the remaining items?"
```

---

## Reorder Alert Trigger

After every `decrement_stock` call, the Inventory MCP checks if `new_quantity <= reorder_level`. If so, it fires the reorder alert:

```python
# In inventory_mcp.py, after calling the decrement_stock RPC
result = supabase.rpc('decrement_stock', {...}).execute()
if result.data['reorder_alert']:
    send_reorder_alert(store_id, product_id, result.data['new_quantity'])
```

The `send_reorder_alert` function sends a Telegram message to the store owner.

---

## Constraints

```sql
-- Primary key
ALTER TABLE public.inventory ADD CONSTRAINT inventory_pkey PRIMARY KEY (id);

-- One inventory record per product per store
ALTER TABLE public.inventory ADD CONSTRAINT inventory_store_product_unique
    UNIQUE (store_id, product_id);

-- Stock cannot go negative
ALTER TABLE public.inventory ADD CONSTRAINT inventory_quantity_non_negative
    CHECK (quantity_in_stock >= 0);

-- Reorder level non-negative
ALTER TABLE public.inventory ADD CONSTRAINT inventory_reorder_level_check
    CHECK (reorder_level >= 0);

-- FK to stores
ALTER TABLE public.inventory ADD CONSTRAINT inventory_store_id_fkey
    FOREIGN KEY (store_id) REFERENCES public.stores(id) ON DELETE RESTRICT;

-- FK to products
ALTER TABLE public.inventory ADD CONSTRAINT inventory_product_id_fkey
    FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE RESTRICT;

-- updated_at trigger
CREATE TRIGGER inventory_updated_at
    BEFORE UPDATE ON public.inventory
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
```

---

## Indexes

```sql
-- Primary lookup: stock for a specific product in a store
CREATE UNIQUE INDEX idx_inventory_store_product ON public.inventory (store_id, product_id);

-- Low stock query: find all products at or below reorder level
CREATE INDEX idx_inventory_low_stock ON public.inventory (store_id, quantity_in_stock, reorder_level);
```

---

## Row Level Security (RLS)

```sql
ALTER TABLE public.inventory ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_role_full_access" ON public.inventory
    USING (TRUE)
    WITH CHECK (TRUE);
```

---

## Relations

### Outgoing (this table references)

| Table | Column | Type | Note |
|---|---|---|---|
| `stores` | `store_id` | Many-to-One | Store this inventory belongs to |
| `products` | `product_id` | Many-to-One | Product being tracked |

### Incoming (other tables reference `inventory`)
None directly — `stock_movements` records the history but references `product_id` + `store_id`, not `inventory.id`.

---

## Business Rules

1. **Zero negative stock — ever:** The `CHECK (quantity_in_stock >= 0)` constraint is the absolute floor. Combined with row-level locking in the RPC, this is impossible to violate under any concurrency scenario.

2. **Inventory record is created on first stock-in:** When `receive_stock()` is called for a product that has no inventory record yet, the Inventory MCP creates the record with the incoming quantity. Subsequent calls UPDATE the existing record.

3. **reorder_level is copied from products at stock-in but is independently editable:** The owner can say "set reorder level for Maggi to 30" and only the inventory record changes, not the products table default.

4. **NUMERIC(10,3) for fractional quantities:** Loose items (sugar, rice) are often sold in fractions (500g = 0.5kg, 250ml = 0.25L). The 3 decimal places support this. Bill quantities and inventory quantities must use the same unit basis.

5. **last_restocked_at for analytics:** Used in `get_stock_health()` to identify products that haven't been restocked in a long time (potential dead stock).

---

## Phase 2 Extensibility

| Feature | Migration |
|---|---|
| Batch/expiry tracking | Add `batch_id UUID` FK to a future `batches` table |
| Multiple locations/shelves | Add `location_id UUID` FK to a future `store_locations` table |
| Reserved stock (pending orders) | Add `quantity_reserved NUMERIC` column |

---

## Example Record

```json
{
  "id": "inv-001",
  "store_id": "store-001",
  "product_id": "prod-003",
  "quantity_in_stock": 47,
  "reorder_level": 20,
  "last_restocked_at": "2024-01-15T08:00:00Z",
  "created_at": "2024-01-10T10:00:00Z",
  "updated_at": "2024-01-15T14:30:00Z"
}
```
