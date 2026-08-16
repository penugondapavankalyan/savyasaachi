# MCP Module: Billing MCP

**Domain:** Billing
**Module Path:** `src/mcp/billing/billing_mcp.py`
**Owned Tables:** `billing.draft_bills`, `billing.draft_bill_items`, `billing.bills`, `billing.bill_items`
**Calls Into:** `CatalogueMCP`, `InventoryMCP`, `KhataMCP`, `IdentityMCP`, `PaymentsMCP` (late-bound)

---

## Responsibility

The Billing MCP is the most complex module. It manages the full lifecycle of a bill — from the first mention of items (creating a draft) through multi-turn editing to final confirmation. On finalization, it computes GST, creates the permanent bill record, and triggers inventory decrements. It enforces idempotency so retried finalizations never double-bill.

This module is only active in the `ACTIVE` workflow state.

---

## Tools (PydanticAI Tool Functions)

### 1. `create_draft_bill`

**Description:** Creates a new draft bill for a billing session, or returns the existing open draft for the current session. Idempotent on `workflow_id`.

**Signature:**
```python
async def create_draft_bill(
    store_id: str,
    telegram_user_id: int,
    workflow_id: Optional[str] = None  # If None, generates new UUID
) -> DraftBillResult
```

**Output (`DraftBillResult`):**
```python
class DraftBillResult(BaseModel):
    draft_bill_id: str
    workflow_id: str
    status: str  # 'OPEN'
    items: List[DraftBillItemResult]
    item_count: int
    estimated_total: float
    already_existed: bool
    expires_at: str
```

**DB Operations:**
```sql
-- Check for existing open draft (idempotent)
SELECT id, workflow_id FROM draft_bills
WHERE telegram_user_id = ? AND status = 'OPEN'
  AND expires_at > NOW()
LIMIT 1;

-- If none found, create new
INSERT INTO draft_bills (store_id, telegram_user_id, workflow_id)
VALUES (?, ?, COALESCE(?, gen_random_uuid()))
RETURNING *;

-- Update workflow_state
UPDATE workflow_state SET active_draft_bill_id = ? WHERE telegram_user_id = ?;
```

**Business Rules:**
- If an open non-expired draft exists → return it (multi-turn continuity)
- If open draft is expired → mark it EXPIRED, create fresh draft
- `workflow_id` is stored and used as the idempotency key for `finalize_bill`

---

### 2. `add_item_to_draft`

**Description:** Adds a product to the current draft bill. If the product already exists in the draft, updates the quantity. Checks availability before adding.

**Signature:**
```python
async def add_item_to_draft(
    draft_bill_id: str,
    product_id: str,
    quantity: float,
    is_partial_fulfillment: bool = False
) -> AddItemResult
```

**Output (`AddItemResult`):**
```python
class AddItemResult(BaseModel):
    draft_item_id: str
    product_name: str
    quantity: float
    unit: str
    unit_price: float
    gst_rate: float
    line_subtotal: float  # quantity × unit_price (pre-tax)
    availability_status: str  # 'FULL' | 'PARTIAL' | 'NONE'
    message: str
```

**Internal Flow:**
```python
# 1. Get product details
product = catalogue_mcp.get_product(store_id, product_id)

# 2. Check availability (tool-layer oversell guard)
availability = inventory_mcp.check_availability(store_id, product_id, quantity)

if availability.fulfillment_status == 'NONE':
    return AddItemResult(availability_status='NONE',
                         message=f"❌ {product.name} is out of stock.")

if availability.fulfillment_status == 'PARTIAL' and not is_partial_fulfillment:
    # Return PARTIAL status — agent will prompt owner for decision
    return AddItemResult(availability_status='PARTIAL',
                         available_quantity=availability.available_quantity,
                         message=f"⚠️ Only {availability.available_quantity} available.")

# 3. Upsert into draft_bill_items
INSERT INTO draft_bill_items (draft_bill_id, product_id, quantity, unit_price, gst_rate, is_partial_fulfillment)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT (draft_bill_id, product_id)
DO UPDATE SET quantity = EXCLUDED.quantity, updated_at = NOW();
```

**Business Rules:**
- Stock is NOT decremented at this point — only when bill is finalized
- Price is snapshotted from `products.mrp` at the time of adding
- GST rate is snapshotted from `products.gst_rate` at the time of adding
- One line item per product per draft (upsert on conflict)

---

### 3. `remove_item_from_draft`

**Description:** Removes a product from the current draft bill.

**Signature:**
```python
async def remove_item_from_draft(draft_bill_id: str, product_id: str) -> RemoveItemResult
```

**DB Operations:**
```sql
DELETE FROM draft_bill_items WHERE draft_bill_id = ? AND product_id = ?;
```

**Business Rules:**
- No stock impact — stock has not been decremented yet
- If product not in draft → returns error, agent informs owner

---

### 4. `update_item_quantity`

**Description:** Changes the quantity of an existing item in the draft.

**Signature:**
```python
async def update_item_quantity(
    draft_bill_id: str,
    product_id: str,
    new_quantity: float
) -> UpdateItemResult
```

**Internal Flow:**
```python
# Re-check availability for new quantity
availability = inventory_mcp.check_availability(store_id, product_id, new_quantity)
# Same partial fulfillment logic as add_item_to_draft
```

---

### 5. `get_draft_bill`

**Description:** Returns the current state of the draft bill with all items and computed totals. Used to show the owner a bill summary before confirmation.

**Signature:**
```python
async def get_draft_bill(draft_bill_id: str) -> DraftBillDetailResult
```

**Output:**
```python
class DraftBillDetailResult(BaseModel):
    draft_bill_id: str
    workflow_id: str
    status: str
    items: List[DraftBillItemDetail]
    subtotal: float
    total_cgst: float
    total_sgst: float
    total_amount: float
    expires_at: str
```

**Item computation (done in Python, not SQL):**
```python
for item in items:
    item.taxable_value = item.quantity * item.unit_price
    item.gst_amount = round(item.taxable_value * item.gst_rate / 100, 2)
    item.cgst = round(item.gst_amount / 2, 2)
    item.sgst = item.gst_amount - item.cgst  # avoids double-rounding
    item.line_total = item.taxable_value + item.cgst + item.sgst
```

---

### 6. `finalize_bill`

**Description:** The most critical tool. Converts a confirmed draft bill into a permanent, immutable bill. Computes final GST, creates bill + bill_items records, decrements stock for all items atomically, and marks the draft as CONFIRMED. Fully idempotent.

**Signature:**
```python
async def finalize_bill(
    draft_bill_id: str,
    payment_mode: str,  # 'CASH' | 'UPI' | 'CARD' | 'CREDIT'
    payment_reference: Optional[str],
    is_credit: bool = False,
    customer_id: Optional[str] = None  # Required if is_credit=True
) -> FinalizedBillResult
```

**Output (`FinalizedBillResult`):**
```python
class FinalizedBillResult(BaseModel):
    bill_id: str
    bill_number: str
    workflow_id: str
    items: List[BillItemDetail]
    subtotal: float
    total_cgst: float
    total_sgst: float
    total_amount: float
    payment_mode: str
    payment_reference: Optional[str]
    is_credit: bool
    already_finalized: bool  # True if idempotent return
    message: str
```

**Idempotency Check:**
```python
# Check if this workflow_id already has a finalized bill
existing_bill = get_bill_by_workflow_id(draft.workflow_id)
if existing_bill:
    return FinalizedBillResult(..., already_finalized=True)
```

**Finalization Transaction (all-or-nothing):**
```sql
BEGIN;

  -- 1. Validate draft is OPEN and not expired
  SELECT * FROM draft_bills WHERE id = ? AND status = 'OPEN' AND expires_at > NOW()
  FOR UPDATE;

  -- 2. Generate bill number
  SELECT generate_bill_number(?) AS bill_number;

  -- 3. Insert bill record (ON CONFLICT = idempotency safety net)
  INSERT INTO bills (store_id, bill_number, telegram_user_id, workflow_id,
                     customer_id, subtotal, total_cgst, total_sgst, total_amount,
                     payment_mode, payment_reference, is_credit)
  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
  ON CONFLICT (workflow_id) DO NOTHING
  RETURNING id;

  -- 4. Insert bill_items for each draft_bill_item
  INSERT INTO bill_items (bill_id, product_id, product_name_snapshot, brand_snapshot,
                          unit_snapshot, hsn_code_snapshot, quantity, unit_price,
                          gst_rate, taxable_value, cgst_amount, sgst_amount, line_total)
  SELECT ?, p.id, p.name, p.brand, p.unit::TEXT, p.hsn_code,
         dbi.quantity, dbi.unit_price, dbi.gst_rate,
         dbi.quantity * dbi.unit_price,
         ROUND((dbi.quantity * dbi.unit_price * dbi.gst_rate / 100) / 2, 2),
         ... -- full GST calculation
  FROM draft_bill_items dbi
  JOIN products p ON p.id = dbi.product_id
  WHERE dbi.draft_bill_id = ?;

  -- 5. Decrement stock for each item (via RPC, with row-level locking per item)
  -- Called in a loop for each item in the draft

  -- 6. Mark draft as CONFIRMED
  UPDATE draft_bills SET status = 'CONFIRMED', updated_at = NOW()
  WHERE id = ?;

  -- 7. Clear active_draft_bill_id in workflow_state
  UPDATE workflow_state SET active_draft_bill_id = NULL WHERE telegram_user_id = ?;

COMMIT;
```

**Post-Finalization (Credit Bills Only):**
```python
# Step 10 — credit bill: auto-confirm + khata entry + payment row (all in one turn)
if is_credit and customer_id:
    # Flip bill to CONFIRMED immediately (no payment collected)
    db.rpc("confirm_payment", {"p_bill_id": bill_id}).execute()

    # Create khata entry (customer now owes the shop)
    khata_entry = await khata_mcp.add_credit_entry(
        store_id=store_id,
        customer_id=customer_id,
        amount=total_amount,
        reference_bill_id=bill_id,
        notes=f"Credit sale — bill {bill_number}",
    )

    # Insert KHATA payment row (paid_amount=0, khata_entry_id links the debt)
    if self._payments:
        await self._payments.record_payment(
            store_id=store_id,
            bill_id=bill_id,
            bill_number=bill_number,
            customer_id=customer_id,
            khata_entry_id=khata_entry.entry_id,
            paid_amount=0.0,
            payment_mode=payment_mode.upper(),
            payment_type="KHATA",
            payment_status="CONFIRMED",
            subtotal=subtotal,
            total_gst=total_cgst + total_sgst,
            bill_amount=total_amount,
            change_amount=0.0,
            balance_due=total_amount,
        )
# For CASH/UPI bills: bill is left in PENDING_PAYMENT status.
# The agent tool confirm_payment(paid_amount) MUST be called after the owner
# reports the cash/UPI amount received. Do NOT call it for credit bills.
```

**Business Rules:**
1. **Idempotent:** `ON CONFLICT (workflow_id) DO NOTHING` on `bills` insert — Telegram redelivery safe
2. **All-or-nothing:** Single DB transaction — partial bill creation is impossible
3. **Stock only decremented on finalize:** Not on draft item add
4. **Don't-sell-below-cost guard:** Before inserting bill_items, validate no `unit_price < cost_price`
5. **Credit bills are auto-CONFIRMED at finalize time** — no separate `confirm_payment()` call
6. **CASH/UPI bills end in PENDING_PAYMENT** — require `confirm_payment(paid_amount)` tool call

---

### 7. `cancel_draft_bill`

**Description:** Cancels an open draft bill. No stock impact.

**Signature:**
```python
async def cancel_draft_bill(draft_bill_id: str) -> CancelResult
```

**DB Operations:**
```sql
UPDATE draft_bills SET status = 'CANCELLED', updated_at = NOW()
WHERE id = ? AND status = 'OPEN';
-- draft_bill_items are cascade-deleted
UPDATE workflow_state SET active_draft_bill_id = NULL WHERE telegram_user_id = ?;
```

---

### 8. `confirm_payment` (MCP method, not the tool)

**Description:** Flips a `PENDING_PAYMENT` bill to `CONFIRMED` via the DB RPC. Called by the `confirm_payment` tool in `tool_registry.py` after the payment amount is received. Does NOT insert a payments row — that is done by the tool.

**Signature:**
```python
async def confirm_payment(self, bill_id: str) -> CancelResult
```

**Important:** This method only transitions the bill status. The tool in `tool_registry.py` wraps it with payment classification and `PaymentsMCP.record_payment()`.

---

### 9. `cancel_bill`

**Description:** Cancels a `PENDING_PAYMENT` or `CONFIRMED` bill. Inserts a `CANCELLED` audit row in `payments.payments`.

**Signature:**
```python
async def cancel_bill(self, bill_id: str) -> CancelResult
```

**Side effects:**
- DB RPC `cancel_bill()` transitions bill status to `CANCELLED`
- `PaymentsMCP.record_payment(payment_type='CANCELLED', payment_status='CANCELLED')` creates audit row

---

### 10. `void_bill`

**Description:** Voids a `CONFIRMED` bill (reversal/refund). Inserts a `REFUNDED` audit row in `payments.payments`.

**Signature:**
```python
async def void_bill(self, bill_id: str) -> CancelResult
```

**Side effects:**
- DB RPC `void_bill()` transitions bill status to `VOID`
- `PaymentsMCP.record_payment(payment_type='REFUNDED', payment_status='REFUNDED')` creates audit row

---

### 10a. `void_bill_by_number` *(agent tool only — no new MCP method)*

**Description:** Agent-layer tool that lets the owner void **any named historical bill** by its human bill number (e.g. `BL-003-20260815-009`) or UUID. Resolves the bill number → UUID via a DB lookup on `billing.bills` scoped to `store_id`, then calls `BillingMCP.void_bill()` with the resolved UUID.

**Why a separate tool:** The existing `void_bill()` tool is zero-argument and hardcodes "fetch the most recent CONFIRMED bill" — correct for the in-session case (owner says "undo" immediately after payment). When the owner specifies a bill by name from a previous session, `void_bill_by_number(bill_number_or_id)` is the correct tool.

**Signature (tool closure in `tool_registry.py`):**
```python
async def void_bill_by_number(bill_number_or_id: str) -> str
```

**Resolution logic:**
```python
if is_valid_uuid(bill_number_or_id):
    resolved_id = bill_number_or_id
else:
    # DB lookup: billing.bills WHERE store_id=? AND bill_number=?
    resolved_id = lookup_bill_id_by_number(bill_number_or_id, store_id)
```

**Registered in:** `BILLING_CONFIRM` intent group.

---

### 11. `get_bill_for_payment`

**Description:** Fetches minimal bill details needed by the `confirm_payment` tool (total, payment mode, bill number, subtotal, GST). Used internally — not exposed as an LLM tool.

**Signature:**
```python
async def get_bill_for_payment(self, bill_id: str) -> dict | None
```

Returns dict with keys: `id`, `bill_number`, `total_amount`, `payment_mode`, `payment_reference`, `subtotal`, `total_cgst`, `total_sgst`, `customer_id`, `is_credit`.

---

### 12. `get_bill`

**Description:** Returns a finalized bill with all its items. Used for PDF generation and review.

**Signature:**
```python
async def get_bill(bill_id: str) -> BillDetailResult
```

---

### 13. `get_bills_by_date`

**Description:** Returns all finalized bills for a store on a specific date. Used by Analytics MCP.

**Signature:**
```python
async def get_bills_by_date(store_id: str, date: str) -> List[BillSummaryResult]
```

---

## GST Computation Reference

```
For each line item:
  taxable_value = quantity × unit_price
  gst_total     = ROUND(taxable_value × gst_rate / 100, 2)
  cgst_amount   = ROUND(gst_total / 2, 2)
  sgst_amount   = gst_total - cgst_amount   ← avoids cumulative rounding error
  line_total    = taxable_value + cgst_amount + sgst_amount

Bill totals:
  subtotal      = SUM(taxable_value)
  total_cgst    = SUM(cgst_amount)
  total_sgst    = SUM(sgst_amount)
  total_amount  = subtotal + total_cgst + total_sgst
```

---

## Multi-Turn Bill Example

```
9:00 AM  "2kg sugar, 1 Aashirvaad atta"
  → create_draft_bill() → draft abc-123, stored in workflow_state.active_draft_bill_id
  → add_item_to_draft(sugar, 2kg) → check_availability → FULL → added
  → add_item_to_draft(aashirvaad_atta, 1) → check_availability → FULL → added
  → get_draft_bill() → shows 2 items, estimated ₹323

9:10 AM  "also 4 Maggi"
  → workflow_state.active_draft_bill_id → draft abc-123 (retrieved from Supabase)
  → add_item_to_draft(maggi, 4) → check_availability → FULL → added
  → get_draft_bill() → shows 3 items, estimated ₹385.72

9:12 AM  "drop the butter, make it 6 Maggi"
  → remove_item_from_draft(butter) → butter not in draft → no-op with note
  → update_item_quantity(maggi, 6) → check_availability → FULL → updated
  → get_draft_bill() → 3 items, estimated ₹419.58

9:13 AM  "UPI, done"
  → finalize_bill(payment_mode='UPI') → creates bill BL-2024-001
  → decrements stock for all 3 items
  → workflow_state.active_draft_bill_id → NULL
  → bill status = PENDING_PAYMENT
  → returns bill summary: "Bill BL-2024-001 finalised. Total ₹419.58. Call confirm_payment() once paid."

9:14 AM  Owner: "paid 500"
  → confirm_payment(paid_amount=500)
  → bill flipped PENDING_PAYMENT → CONFIRMED
  → OVERPAYMENT detected (₹500 - ₹419.58 = ₹80.42 extra)
  → Redis: pending_payment:{tuid} = {intent_type: OVERPAYMENT, delta_amount: 80.42, ...}
  → Agent: "₹80.42 overpaid. Return change or add to customer khata?"

9:14 AM  Owner: "add to khata, Ramesh"
  → get_customer("Ramesh") → customer_id
  → add_payment_entry(customer_id, amount=None)   ← amount=None reads from Redis
  → khata entry inserted, khata_entry_id obtained
  → payments row inserted (type=OVERPAYMENT, paid=500, change=80.42)
  → Redis key cleared
```

---

## Payment Status Transitions

```
PENDING_PAYMENT  ──(confirm_payment RPC)──►  CONFIRMED
                 ──(cancel_bill RPC)────────►  CANCELLED

CONFIRMED        ──(void_bill RPC)──────────►  VOID
```

Transitions are enforced by a DB trigger (`bill_status_flow` in migration 014). Any other transition raises an exception.

---

## Error Handling

| Error | Response |
|---|---|
| Draft expired | "Your previous bill expired. Starting a fresh bill." |
| Product out of stock during finalize | Transaction rolls back, agent reports which item(s) failed |
| Insufficient stock during finalize (concurrent) | Same — rolls back, retry with updated availability |
| Credit bill without customer | Validation error: "Please specify the customer for a credit bill" |

---

## Phase 2 Extensibility

| Feature | Change |
|---|---|
| Discount support | `total_discount` already in bills schema — add `apply_discount()` tool |
| IGST | Add `gst_type` field to bills, update computation logic |
| Split payments | Add second payment row per bill; update `payments.payments` schema |
| Payment dashboard | `PaymentsMCP.get_payment_history()` already supports this |
