# MCP Module: Inventory MCP

**Domain:** Inventory  
**Module Path:** `src/mcp/inventory/inventory_mcp.py`  
**Owned Tables:** `inventory`, `stock_movements`

---

## Responsibility

The Inventory MCP owns all stock quantity operations — receiving stock from suppliers, querying current stock levels, atomically decrementing stock during billing, and triggering reorder alerts. It also provides the partial fulfillment check used by the Billing MCP before adding items to a draft bill.

This module is active in `PENDING_INVENTORY` and `ACTIVE` workflow states.

---

## Tools (PydanticAI Tool Functions)

### 1. `receive_stock`

**Description:** Records the receipt of new stock for a product. Creates or updates the inventory record and writes a `STOCK_IN` movement. Advances workflow state to `ACTIVE` if this is the first stock receipt.

**Signature:**
```python
async def receive_stock(
    store_id: str,
    product_id: str,
    quantity: float,
    cost_price: Optional[float] = None,  # Optionally update cost price at stock-in
    notes: Optional[str] = None
) -> ReceiveStockResult
```

**Output (`ReceiveStockResult`):**
```python
class ReceiveStockResult(BaseModel):
    inventory_id: str
    product_name: str
    quantity_received: float
    new_total_quantity: float
    unit: str
    cost_price_updated: bool
    workflow_advanced: bool  # True if this was first stock-in and state → ACTIVE
    message: str
```

**DB Operations (inside a transaction):**
```sql
-- Upsert inventory record
INSERT INTO inventory (store_id, product_id, quantity_in_stock, reorder_level, last_restocked_at)
VALUES (?, ?, ?, (SELECT reorder_level FROM products WHERE id = ?), NOW())
ON CONFLICT (store_id, product_id)
DO UPDATE SET
    quantity_in_stock = inventory.quantity_in_stock + EXCLUDED.quantity_in_stock,
    last_restocked_at = NOW(),
    updated_at = NOW()
RETURNING quantity_in_stock;

-- Write stock movement
INSERT INTO stock_movements (store_id, product_id, movement_type, quantity_delta,
    reference_id, reference_type, notes)
VALUES (?, ?, 'STOCK_IN', ?, gen_random_uuid(), 'STOCK_IN', ?);

-- Optionally update cost price in products
UPDATE products SET cost_price = ?, updated_at = NOW()
WHERE id = ? AND ? IS NOT NULL;
```

**Post-Insert:**
```python
# Check if this is the first stock-in for the store
total_stock_ins = count_stock_ins_for_store(store_id)
if total_stock_ins == 1:
    identity_mcp.advance_workflow_state(telegram_user_id, 'ACTIVE')
    workflow_advanced = True
```

**Business Rules:**
- `quantity` must be positive
- Updates `products.cost_price` if new cost price differs (owner bought at a different price this time)
- Agent example: "50 packets of Maggi came in, cost ₹12, MRP ₹14" → `receive_stock(product_id=maggi, qty=50, cost_price=12)` + `update_product(mrp=14)`

---

### 2. `get_stock`

**Description:** Returns current stock quantity for a specific product.

**Signature:**
```python
async def get_stock(store_id: str, product_id: str) -> StockResult
```

**Output (`StockResult`):**
```python
class StockResult(BaseModel):
    product_id: str
    product_name: str
    brand: Optional[str]
    quantity_in_stock: float
    unit: str
    reorder_level: float
    is_below_reorder: bool
    last_restocked_at: Optional[str]
```

**DB Operations:**
```sql
SELECT i.quantity_in_stock, i.reorder_level, i.last_restocked_at,
       p.name, p.brand, p.unit
FROM inventory i
JOIN products p ON p.id = i.product_id
WHERE i.store_id = ? AND i.product_id = ?;
```

---

### 3. `check_availability`

**Description:** Checks whether a requested quantity of a product is available. Used by Billing MCP before adding items to a draft bill. This is the **tool-layer oversell guard**.

**Signature:**
```python
async def check_availability(
    store_id: str,
    product_id: str,
    requested_quantity: float
) -> AvailabilityResult
```

**Output (`AvailabilityResult`):**
```python
class AvailabilityResult(BaseModel):
    product_id: str
    product_name: str
    requested_quantity: float
    available_quantity: float
    unit: str
    fulfillment_status: str  # 'FULL' | 'PARTIAL' | 'NONE'
    can_partially_fulfill: bool
    message: str
```

**Fulfillment Status Logic:**
```python
if available >= requested:
    fulfillment_status = 'FULL'
elif available > 0:
    fulfillment_status = 'PARTIAL'
    can_partially_fulfill = True
else:
    fulfillment_status = 'NONE'
    can_partially_fulfill = False
```

**Partial Fulfillment Flow (orchestrated by agent):**
```
check_availability → PARTIAL
Agent: "⚠️ Only 1.5kg of Sugar is available (you asked for 3kg).
        Add 1.5kg instead, or skip Sugar?"
Owner: "add 1.5kg"
Agent calls: add_item_to_draft(draft_bill_id, sugar_id, 1.5, is_partial=True)
```

---

### 4. `decrement_stock`

**Description:** Atomically decrements stock for a product. Called by Billing MCP during `finalize_bill`. Uses a Supabase RPC to ensure atomic row-level locking. NOT called directly by the agent — only by Billing MCP.

**Signature:**
```python
async def decrement_stock(
    store_id: str,
    product_id: str,
    quantity: float,
    bill_id: str
) -> DecrementResult
```

**Output (`DecrementResult`):**
```python
class DecrementResult(BaseModel):
    product_id: str
    quantity_decremented: float
    new_quantity: float
    reorder_alert: bool  # True if new_quantity <= reorder_level
```

**DB Operations:** Calls the `decrement_stock` Supabase RPC (defined in `docs/database/inventory/inventory.md`). The RPC:
1. Acquires row-level lock (`SELECT ... FOR UPDATE`)
2. Validates sufficient stock
3. Updates inventory quantity
4. Inserts stock_movement record
5. Returns new quantity and reorder flag

**Post-Decrement:**
```python
if result.reorder_alert:
    await send_reorder_alert(store_id, product_id, result.new_quantity)
```

---

### 5. `get_low_stock_items`

**Description:** Returns all products at or below their reorder level. Used for the "what's running out?" query.

**Signature:**
```python
async def get_low_stock_items(store_id: str) -> List[LowStockItem]
```

**Output:**
```python
class LowStockItem(BaseModel):
    product_id: str
    product_name: str
    brand: Optional[str]
    quantity_in_stock: float
    reorder_level: float
    unit: str
    urgency: str  # 'OUT_OF_STOCK' | 'CRITICAL' | 'LOW'
```

**Urgency Levels:**
- `OUT_OF_STOCK`: `quantity = 0`
- `CRITICAL`: `0 < quantity <= reorder_level * 0.5`
- `LOW`: `reorder_level * 0.5 < quantity <= reorder_level`

**DB Operations:**
```sql
SELECT i.quantity_in_stock, i.reorder_level, p.name, p.brand, p.unit
FROM inventory i
JOIN products p ON p.id = i.product_id
WHERE i.store_id = ? AND i.quantity_in_stock <= i.reorder_level
ORDER BY (i.quantity_in_stock / NULLIF(i.reorder_level, 0)) ASC;
```

---

### 6. `get_stock_movements`

**Description:** Returns the audit trail of stock changes for a product in a date range.

**Signature:**
```python
async def get_stock_movements(
    store_id: str,
    product_id: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 50
) -> List[StockMovementRecord]
```

---

### 7. `get_all_stock`

**Description:** Returns current stock for all products in a store. Used for analytics and "what's in stock?" queries.

**Signature:**
```python
async def get_all_stock(store_id: str) -> List[StockResult]
```

---

## Reorder Alert

When `decrement_stock` returns `reorder_alert=True`, the Inventory MCP triggers a Telegram message to the store owner:

```python
async def send_reorder_alert(store_id: str, product_id: str, current_quantity: float):
    product = await catalogue_mcp.get_product(store_id, product_id)
    inventory = await get_stock(store_id, product_id)
    message = (
        f"⚠️ *Low Stock Alert*\n\n"
        f"*{product.name}* is running low.\n"
        f"Current stock: {current_quantity} {product.unit}\n"
        f"Reorder level: {inventory.reorder_level} {product.unit}\n\n"
        f"Time to restock!"
    )
    await telegram_client.send_message(store_owner_telegram_id, message)
```

---

## Concurrency Safety

Two bills being finalized simultaneously for the same product:

```
Bill A: requests 5 units of Maggi (stock: 6)
Bill B: requests 4 units of Maggi (stock: 6) — concurrent

Timeline:
  Bill A → calls decrement_stock RPC → acquires FOR UPDATE lock on inventory row
  Bill B → calls decrement_stock RPC → waits (blocked on the lock)
  Bill A → decrements: 6 - 5 = 1 → releases lock
  Bill B → acquires lock → checks: 1 < 4 → RAISES EXCEPTION "Insufficient stock"
  Bill B → transaction rolls back → Billing MCP returns error
  Agent → informs owner: "Only 1 Maggi is available. Would you like to adjust the bill?"
```

---

## Error Handling

| Error | Response |
|---|---|
| Product not in inventory yet | "No stock record for [product]. Please receive stock first." |
| Insufficient stock (during decrement) | Transaction rolls back, Billing MCP retries check_availability |
| Quantity must be positive | Validation error returned to agent |

---

## Phase 2 Extensibility

| Feature | Change |
|---|---|
| Batch tracking | Add `batch_id` to `receive_stock`, `decrement_stock` |
| Manual stock adjustment | Add `adjust_stock(product_id, delta, reason)` tool |
| Stock transfer between stores | Add `transfer_stock(from_store, to_store, product_id, qty)` tool |
| Sales velocity for reorder | Already queryable from `stock_movements` — add `get_sales_velocity()` tool |
