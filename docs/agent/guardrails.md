# Agent Guardrails

**Reference:** PDF §4 (Hard Parts)

---

## Overview

Guardrails are business rules enforced at the **tool layer** (MCP modules and database), not in the system prompt. A guardrail lives where the data changes — not where the language model runs. This ensures correctness even if the model's reasoning is imperfect.

---

## Guardrail 1: Oversell Guard

**Rule:** Stock cannot go negative. Selling 10 units when only 6 are in stock must be refused.

**Where enforced (defense in depth):**

| Layer | Mechanism |
|---|---|
| **Tool layer** | `inventory_mcp.check_availability()` before item is added to draft |
| **RPC layer** | `decrement_stock` Supabase RPC raises exception if quantity insufficient |
| **DB layer** | `CHECK (quantity_in_stock >= 0)` constraint on inventory table |

**Flow:**
```
add_item_to_draft(product_id, qty=10)
  → check_availability(product_id, requested=10)
  → available=6, status='PARTIAL'
  → Returns PARTIAL status (not added yet)

Agent responds: "⚠️ Only 6 Maggi in stock (you asked for 10).
                Add 6 instead, or skip?"

Owner: "add 6"
→ add_item_to_draft(product_id, qty=6, is_partial=True)
→ check_availability → FULL for qty=6 → added

Owner: "no"
→ Item not added to bill
```

---

## Guardrail 2: Don't Sell Below Cost

**Rule:** The selling price must not be below the cost price. Protect the owner from accidental loss.

**Where enforced:** Billing MCP `finalize_bill()` validation.

```python
# In finalize_bill(), before creating bill_items
for item in draft_items:
    if item.unit_price < product.cost_price:
        raise BelowCostError(
            f"Cannot sell {product.name} at ₹{item.unit_price} "
            f"(cost is ₹{product.cost_price})"
        )
```

**Agent behavior:**
```
Scenario: MRP was accidentally set lower than cost price
→ finalize_bill() raises BelowCostError
→ Agent: "⚠️ Aashirvaad Atta is priced at ₹22 but your cost price is ₹25.
         Update the price before billing, or confirm if this is intentional?"
```

**Phase 2:** Allow override with explicit confirmation.

---

## Guardrail 3: GST Correctness

**Rule:** Per-item GST computation must be accurate. CGST and SGST are equal halves. Rounding must not accumulate.

**Where enforced:** Billing MCP `finalize_bill()` computation.

```python
# Anti-double-rounding pattern
def compute_gst(taxable_value: float, gst_rate: float) -> tuple[float, float]:
    gst_total = round(taxable_value * gst_rate / 100, 2)
    cgst = round(gst_total / 2, 2)
    sgst = gst_total - cgst  # ← sgst absorbs any rounding delta
    return cgst, sgst
```

**Loose items (is_loose=True):** gst_rate is always 0.0 — no GST applied regardless of any other data.

**DB-level enforced:** `enforce_loose_item_gst()` trigger on products table.

---

## Guardrail 4: Idempotency (No Double-Bill)

**Rule:** Telegram redelivers webhook updates. A retried "finalize" must not create a second bill or double-decrement stock.

**Where enforced:** Billing MCP `finalize_bill()` + `bills` table `UNIQUE (workflow_id)`.

```python
# In finalize_bill()
existing = await get_bill_by_workflow_id(draft.workflow_id)
if existing:
    return FinalizedBillResult(..., already_finalized=True)
    # No DB writes, no stock changes
```

```sql
-- DB-level safety net
INSERT INTO bills (..., workflow_id)
VALUES (..., ?)
ON CONFLICT (workflow_id) DO NOTHING;
```

Same pattern applies to user registration: `ON CONFLICT (telegram_user_id) DO NOTHING` on users table.

---

## Guardrail 5: Khata Existence Check

**Rule:** Cannot record a payment for a customer that doesn't exist.

**Where enforced:** Khata MCP `add_payment_entry()`.

```python
async def add_payment_entry(store_id, customer_id, amount):
    customer = await get_customer_by_id(store_id, customer_id)
    if not customer:
        raise CustomerNotFoundError(f"Customer {customer_id} not found in this store")
    if not customer.is_active:
        raise CustomerInactiveError(f"Customer account is deactivated")
    # ... proceed
```

**Agent behavior:**
```
Owner: "Suresh paid ₹500"
→ get_customer(store_id, "Suresh") → not found
→ Agent: "I don't have a customer named Suresh in your khata.
         Would you like to add them first?"
```

---

## Guardrail 6: Concurrency Safety

**Rule:** Two bills in flight simultaneously must not corrupt stock.

**Where enforced:** Inventory MCP `decrement_stock()` via Supabase RPC with `SELECT ... FOR UPDATE`.

```
Bill A finalizes (Maggi, qty=5, stock=6)
Bill B finalizes (Maggi, qty=4, stock=6) — concurrent

DB execution:
  Bill A: acquires FOR UPDATE lock on inventory row for Maggi
  Bill B: waits for lock
  Bill A: decrements 6→1, releases lock
  Bill B: acquires lock, checks: 1 < 4 → RAISES EXCEPTION
  Bill B: entire transaction rolls back
  Bill B: Billing MCP returns error → agent notifies owner
```

The `CHECK (quantity_in_stock >= 0)` constraint is the absolute fallback if locking somehow fails.

---

## Guardrail 7: Draft Bill Expiry

**Rule:** Draft bills older than 4 hours must not be finalizable.

**Where enforced:** Billing MCP `finalize_bill()` validation.

```python
if draft.expires_at < datetime.utcnow():
    await mark_draft_expired(draft.id)
    raise DraftExpiredError("This bill has expired. Start a new bill.")
```

```sql
-- Also enforced in the finalization transaction
SELECT * FROM draft_bills WHERE id = ? AND status = 'OPEN' AND expires_at > NOW()
FOR UPDATE;
-- If 0 rows returned → draft is expired or already confirmed
```

---

## Guardrail 8: Product Disambiguation

**Rule:** When a request is ambiguous (multiple products match a name), the agent must ask — not guess.

**Where enforced:** Agent reasoning, triggered by `search_products()` returning multiple results.

```python
# In catalogue_mcp.search_products()
results = full_text_search(store_id, "atta")
# Returns: [Aashirvaad Atta 5kg, Pillsbury Atta 1kg]

# Agent receives 2 results — must ask
```

**Exception:** If `stores.preferences.preferred_brands` contains a mapping for the ambiguous term, auto-select.

```python
if preferred_brands.get("atta") == "Aashirvaad Atta 5kg":
    # Auto-select, no question needed
    return results[0]
```

---

## Guardrail 9: Workflow State Gate

**Rule:** Operations not available in the current workflow state must be unavailable to the agent.

**Where enforced:** Pre-agent context loader — tool list is state-scoped.

```
PENDING_CATALOGUE user tries to bill:
→ Billing tools NOT in tool_list for this state
→ LLM only has identity + catalogue tools
→ LLM naturally guides user to add products first
(No hardcoded check — the model simply doesn't have the tool available)
```

---

## Guardrail 10: Immutable Financial Records

**Rule:** Finalized bills and khata entries must never be modified or deleted.

**Where enforced:** Database triggers.

```sql
-- On bills table
CREATE TRIGGER no_update_bills
    BEFORE UPDATE ON public.bills
    FOR EACH ROW EXECUTE FUNCTION prevent_bill_mutation();

-- On khata_entries table  
CREATE TRIGGER no_update_khata_entries
    BEFORE UPDATE ON public.khata_entries
    FOR EACH ROW EXECUTE FUNCTION prevent_khata_mutation();

-- On stock_movements table
CREATE TRIGGER no_update_stock_movements
    BEFORE UPDATE ON public.stock_movements
    FOR EACH ROW EXECUTE FUNCTION prevent_stock_movement_mutation();
```

Corrections are made via new entries (adjustment khata entry, not edit), preserving full audit trail.
