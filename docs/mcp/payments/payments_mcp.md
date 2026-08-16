# MCP Module: Payments MCP

**Domain:** Payments  
**Module Path:** `src/mcp/payments/payments_mcp.py`  
**Owned Tables:** `payments.payments`  
**Reads From:** `billing.bills`, `billing.customers`, `khata.khata_entries` (for balance via KhataMCP)

---

## Responsibility

The Payments MCP owns the complete payment audit trail. Every financial event — cash received, UPI collected, credit recorded, overpayment stored in khata, underpayment balance added to khata, bill cancellation, bill void, standalone khata settlement — results in exactly one immutable row in `payments.payments`.

This module does **not**:
- Modify `billing.bills` status (BillingMCP calls the `confirm_payment` RPC for that)
- Create khata entries (KhataMCP does that)
- Call the `confirm_payment` DB RPC directly

The table is **append-only** — no UPDATE or DELETE is permitted. This is enforced by a DB trigger.

---

## Construction and Late-Binding

```python
# PaymentsMCP is constructed LAST in MCPInstances.
# It depends on KhataMCP (for get_balance in get_payment_history).
self.payments = PaymentsMCP(khata_mcp=self.khata)

# BillingMCP was already constructed before PaymentsMCP existed.
# It must be injected after:
self.billing.set_payments_mcp(self.payments)
```

This late-binding pattern exists because:
- `BillingMCP` calls `PaymentsMCP.record_payment()` → Billing depends on Payments
- `PaymentsMCP` calls `KhataMCP.get_balance()` → Payments depends on Khata
- At the time `BillingMCP` is constructed, `PaymentsMCP` does not yet exist

---

## Payment Schema

```sql
-- Schema: payments
-- Table:  payments.payments

CREATE TABLE payments.payments (
    payment_id         UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id           UUID         NOT NULL REFERENCES identity.stores(id),
    bill_id            UUID         REFERENCES billing.bills(id),       -- NULL for KHATA_SETTLE
    customer_id        UUID         REFERENCES billing.customers(id),
    khata_entry_id     UUID         REFERENCES khata.khata_entries(id), -- NULL for EXACT/CANCELLED/REFUNDED
    payment_mode       TEXT         NOT NULL,  -- CASH | UPI | CARD | CREDIT
    payment_type       payment_type NOT NULL,  -- see enum below
    payment_status     pay_status   NOT NULL DEFAULT 'CONFIRMED',
    paid_amount        NUMERIC(10,2) NOT NULL,
    bill_amount        NUMERIC(10,2),          -- snapshot of bill total
    subtotal           NUMERIC(10,2),
    total_gst          NUMERIC(10,2),
    change_amount      NUMERIC(10,2) NOT NULL DEFAULT 0,  -- for OVERPAYMENT
    balance_due        NUMERIC(10,2) NOT NULL DEFAULT 0,  -- for UNDERPAYMENT
    payment_reference  TEXT,                  -- UPI transaction ID etc.
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW()
    -- No updated_at — immutable
);

-- Enums
CREATE TYPE payment_type AS ENUM (
    'EXACT', 'OVERPAYMENT', 'UNDERPAYMENT', 'KHATA', 'KHATA_SETTLE'
);
CREATE TYPE pay_status AS ENUM (
    'CONFIRMED', 'PENDING', 'CANCELLED', 'REFUNDED'
);
```

**Key constraint:** `khata_entry_id` must be set at INSERT time (no post-insert update). The tool layer resolves khata entries before calling `record_payment()`.

---

## Payment Types Reference

| Type | When | `bill_id` | `khata_entry_id` | `paid_amount` |
|---|---|---|---|---|
| `EXACT` | CASH/UPI, exact amount | ✅ | null | = bill total |
| `OVERPAYMENT` | CASH/UPI, paid > bill | ✅ | ✅ | > bill total |
| `UNDERPAYMENT` | CASH/UPI, paid < bill | ✅ | ✅ | < bill total |
| `KHATA` | Credit sale finalized | ✅ | ✅ | 0.00 |
| `KHATA_SETTLE` | Standalone khata repayment | null | ✅ | > 0 |
| (CANCELLED) | Bill cancelled | ✅ | null | 0.00 |
| (REFUNDED) | Bill voided | ✅ | null | 0.00 |

*CANCELLED/REFUNDED use `payment_status` field, not `payment_type`, to distinguish them as audit entries.*

---

## Tools (MCP Methods)

### 1. `record_payment`

**Description:** Inserts one row into `payments.payments`. Called by BillingMCP and the tool layer — never called directly by the LLM.

**Signature:**
```python
async def record_payment(
    self,
    store_id: str,
    bill_id: Optional[str],
    paid_amount: float,
    payment_mode: str,
    payment_type: str,              # EXACT | OVERPAYMENT | UNDERPAYMENT | KHATA | KHATA_SETTLE
    payment_status: str = "CONFIRMED",
    bill_number: Optional[str] = None,
    customer_id: Optional[str] = None,
    khata_entry_id: Optional[str] = None,   # MUST be resolved before calling
    payment_reference: Optional[str] = None,
    subtotal: Optional[float] = None,
    total_gst: Optional[float] = None,
    bill_amount: Optional[float] = None,
    change_amount: float = 0.0,
    balance_due: float = 0.0,
) -> PaymentResult
```

**Critical invariant:** `khata_entry_id` must be resolved by the caller before calling this method. The payments table is immutable — there is no way to set it after the row is created.

---

### 2. `get_payment_history`

**Description:** Returns combined payment + bill history for a customer, sorted newest first. Exposed as the `get_payment_history` LLM tool in the KHATA intent group.

**Signature:**
```python
async def get_payment_history(
    self,
    store_id: str,
    customer_id: str,
) -> PaymentHistoryResult
```

**Output (`PaymentHistoryResult`):**
```python
class PaymentHistoryResult(BaseModel):
    customer_id: str
    customer_name: str
    phone: str
    total_paid: float            # SUM of all CONFIRMED payment rows
    outstanding_balance: float   # from KhataMCP.get_balance()
    payments: List[PaymentHistoryEntry]
    bills: List[BillHistoryEntry]
```

**Internal flow:**
1. Fetch customer from `billing.customers`
2. Fetch all `payments.payments` rows for this customer
3. Fetch all `billing.bills` rows for this customer
4. Resolve bill numbers for payment rows with `bill_id` set
5. Compute `total_paid` = SUM of confirmed payment `paid_amount`
6. Call `KhataMCP.get_balance()` for current outstanding
7. Return merged result sorted newest first

---

### 3. `get_payment_by_bill`

**Description:** Returns the most recent payment row for a given `bill_id`. Used internally for lookups.

**Signature:**
```python
async def get_payment_by_bill(self, bill_id: str) -> Optional[PaymentResult]
```

---

## Multi-Turn Payment Resolution

Over and underpayment resolution spans two Lambda invocations. The `pending_payment:{telegram_user_id}` Redis key bridges them:

```
Turn 1 — confirm_payment(paid_amount=X) [in tool_registry.py]:
  1. BillingMCP.confirm_payment(bill_id) → bill status: PENDING_PAYMENT → CONFIRMED
  2. If EXACT:
       PaymentsMCP.record_payment(type=EXACT) → done
  3. If OVERPAYMENT (paid > bill total):
       Redis.set_pending_payment(tuid, {intent_type: OVERPAYMENT, delta_amount: X-total, ...})
       → payment row NOT inserted yet
       → agent asks "return change or add to khata?"
  4. If UNDERPAYMENT (paid < bill total):
       Redis.set_pending_payment(tuid, {intent_type: UNDERPAYMENT, delta_amount: total-X, ...})
       → payment row NOT inserted yet
       → agent asks "pay now or add to khata?"

Turn 2 — resolution (in tool_registry.py):
  OVERPAYMENT resolution (owner chose "add to khata"):
    get_customer() → customer_id
    add_payment_entry(customer_id, amount=None):
      1. Read Redis: delta = ₹70
      2. KhataMCP.add_payment_entry(amount=70) → khata_entry_id
      3. PaymentsMCP.record_payment(type=OVERPAYMENT,
                                    paid=paid_amount,  ← from Redis
                                    change=70,
                                    khata_entry_id=...)
      4. Redis.clear_pending_payment(tuid)

  UNDERPAYMENT resolution (owner chose "add to khata"):
    get_customer() → customer_id
    add_credit_entry(customer_id, amount=None):
      1. Read Redis: delta = ₹50
      2. KhataMCP.add_credit_entry(amount=50) → khata_entry_id
      3. PaymentsMCP.record_payment(type=UNDERPAYMENT,
                                    paid=paid_amount,  ← from Redis
                                    balance_due=50,
                                    khata_entry_id=...)
      4. Redis.clear_pending_payment(tuid)
```

**Redis key format (`pending_payment:{tuid}`):**
```json
{
  "intent_type": "OVERPAYMENT",      // or UNDERPAYMENT
  "delta_amount": 70.00,             // change (OVER) or balance due (UNDER)
  "bill_id": "uuid",
  "bill_number": "BL-001-20260101-001",
  "bill_amount": 430.00,
  "paid_amount": 500.00,
  "payment_mode": "CASH",
  "payment_reference": null,
  "store_id": "uuid",
  "subtotal": 400.00,
  "total_gst": 30.00
}
```

TTL: 30 minutes. Automatically expires if the owner never resolves the overpayment.

---

## Error Handling

| Situation | Behaviour |
|---|---|
| `_get_redis()` returns None (Redis unavailable) | `record_payment` proceeds without Redis; over/underpayment resolution falls back to LLM-supplied amounts |
| Redis key expired before resolution | `add_credit_entry(amount=None)` returns `"ERROR: amount is required when there is no pending underpayment intent."` — agent must ask owner for amount again |
| `khata_entry_id` not yet available | Caller must resolve khata first; never call `record_payment` without it for OVER/UNDER rows |

---

## Phase 2 Extensibility

| Feature | Change |
|---|---|
| Payment reports | Extend `get_payment_history()` with date filters |
| UPI reconciliation | Add `payment_reference` queries; already stored |
| Refund tracking | REFUNDED rows already capture original `bill_amount` |
| Multi-store analytics | `store_id` on all rows supports cross-store aggregation |
