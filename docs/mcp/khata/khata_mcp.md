# MCP Module: Khata MCP

**Domain:** Khata (Credit Ledger)  
**Module Path:** `src/mcp/khata/khata_mcp.py`  
**Owned Tables:** `customers`, `khata_entries`

---

## Responsibility

The Khata MCP owns the customer credit ledger system. It manages customer profiles and maintains the append-only ledger of credit transactions and payments. Balance is always computed by summing all entries — it is never stored as a running total.

This module is only active in the `ACTIVE` workflow state.

---

## Tools (PydanticAI Tool Functions)

### 1. `add_customer`

**Description:** Creates a new customer profile for a store. Idempotent on phone number.

**Signature:**
```python
async def add_customer(
    store_id: str,
    name: str,
    phone: str,
    notes: Optional[str] = None
) -> CustomerResult
```

**Output (`CustomerResult`):**
```python
class CustomerResult(BaseModel):
    customer_id: str
    name: str
    phone: str
    notes: Optional[str]
    already_existed: bool
    current_balance: float  # SUM of all khata_entries (0 for new customer)
    message: str
```

**DB Operations:**
```sql
INSERT INTO customers (store_id, name, phone, notes)
VALUES (?, ?, ?, ?)
ON CONFLICT (store_id, phone)
DO UPDATE SET name = EXCLUDED.name, updated_at = NOW()
RETURNING id, (xmax != 0) AS already_existed;
```

**Business Rules:**
- Phone is the primary deduplication key — same phone = same customer
- If owner adds "Ramesh, 9876543210" again → returns existing record, updates name if changed
- Agent confirmation: "Customer Ramesh (9876543210) added to your khata."

---

### 2. `get_customer`

**Description:** Finds a customer by name or phone number. Supports fuzzy name matching.

**Signature:**
```python
async def get_customer(
    store_id: str,
    name_or_phone: str
) -> CustomerLookupResult
```

**Output (`CustomerLookupResult`):**
```python
class CustomerLookupResult(BaseModel):
    found: bool
    customers: List[CustomerResult]  # Multiple if ambiguous name match
    exact_match: bool
```

**Lookup Logic:**
```python
# Try exact phone match first
if name_or_phone.isdigit():
    result = query_by_phone(store_id, name_or_phone)
else:
    # Name search (case-insensitive, partial match)
    results = query_by_name(store_id, name_or_phone)
    
    if len(results) == 1:
        return CustomerLookupResult(found=True, customers=results, exact_match=True)
    elif len(results) > 1:
        # Ambiguous — return all, let agent ask
        return CustomerLookupResult(found=True, customers=results, exact_match=False)
    else:
        return CustomerLookupResult(found=False, customers=[], exact_match=False)
```

**Disambiguation Example:**
```
Owner: "Ramesh paid ₹300"
Agent calls: get_customer(store_id, "Ramesh")
→ Returns 2 matches: Ramesh Kumar (9876543210), Ramesh Sharma (9988776655)

Agent: "Which Ramesh — Ramesh Kumar (9876) or Ramesh Sharma (9988)?"
Owner: "Kumar"
→ Agent calls: add_payment_entry(store_id, customer_id=ramesh_kumar_id, amount=300)
```

---

### 3. `add_credit_entry`

**Description:** Records a credit transaction — the customer has bought goods on credit. Creates a CREDIT entry with a positive `amount_delta`. Called by Billing MCP when a credit bill is finalized.

**Signature:**
```python
async def add_credit_entry(
    store_id: str,
    customer_id: str,
    amount: float,
    reference_bill_id: Optional[str] = None,
    notes: Optional[str] = None
) -> KhataEntryResult
```

**Output (`KhataEntryResult`):**
```python
class KhataEntryResult(BaseModel):
    entry_id: str
    customer_name: str
    entry_type: str  # 'CREDIT'
    amount: float
    new_balance: float
    balance_direction: str  # 'OWES_SHOP' | 'SHOP_OWES' | 'SETTLED'
    message: str
```

**DB Operations:**
```sql
INSERT INTO khata_entries (store_id, customer_id, entry_type, amount_delta, reference_bill_id, notes)
VALUES (?, ?, 'CREDIT', ?, ?, ?);

-- Compute new balance
SELECT SUM(amount_delta) FROM khata_entries WHERE store_id = ? AND customer_id = ?;
```

**Business Rules:**
- `amount` must be positive
- `reference_bill_id` links to the bill that created this credit
- Called automatically by Billing MCP on credit bill finalization — agent does not call this directly for normal billing flow

---

### 4. `add_payment_entry`

**Description:** Records a payment received from a customer. Creates a PAYMENT entry with a negative `amount_delta`.

**Signature:**
```python
async def add_payment_entry(
    store_id: str,
    customer_id: str,
    amount: float,
    notes: Optional[str] = None
) -> KhataEntryResult
```

**Business Rules:**
- `amount` must be positive (the sign is applied internally: `amount_delta = -amount`)
- Agent guardrail: verifies customer exists and is active before creating entry
- Agent guardrail: if customer has balance = 0 or negative, warns owner: "Ramesh's balance is already ₹0. Are you recording an advance payment?"
- Agent response example: "Payment of ₹300 recorded for Ramesh. New balance: ₹200 (owes shop)"

---

### 5. `get_balance`

**Description:** Returns the current balance for a customer. Balance = SUM of all amount_delta entries.

**Signature:**
```python
async def get_balance(store_id: str, customer_id: str) -> BalanceResult
```

**Output (`BalanceResult`):**
```python
class BalanceResult(BaseModel):
    customer_id: str
    customer_name: str
    phone: str
    balance: float
    balance_direction: str  # 'OWES_SHOP' | 'SHOP_OWES' | 'SETTLED'
    last_transaction_at: Optional[str]
    message: str
```

**Balance Direction:**
```python
if balance > 0:
    direction = 'OWES_SHOP'    # Customer owes the shop
    message = f"Ramesh owes ₹{balance} to the shop"
elif balance < 0:
    direction = 'SHOP_OWES'    # Shop owes the customer
    message = f"Shop owes Ramesh ₹{abs(balance)}"
else:
    direction = 'SETTLED'
    message = "Ramesh's account is settled (₹0 balance)"
```

**DB Operations:**
```sql
SELECT SUM(ke.amount_delta) as balance,
       MAX(ke.created_at) as last_transaction_at,
       c.name, c.phone
FROM khata_entries ke
JOIN customers c ON c.id = ke.customer_id
WHERE ke.store_id = ? AND ke.customer_id = ?;
```

---

### 6. `get_khata_history`

**Description:** Returns the full transaction history for a customer with running balance.

**Signature:**
```python
async def get_khata_history(
    store_id: str,
    customer_id: str,
    limit: int = 20
) -> KhataHistoryResult
```

**Output:**
```python
class KhataHistoryResult(BaseModel):
    customer_name: str
    current_balance: float
    entries: List[KhataEntryDetail]
```

**Example Response:**
```
Ramesh Kumar (9876543210)

Jan 15: Credit +₹200.00 (Bill BL-2024-001)   Running: ₹200.00
Jan 20: Credit +₹100.00 (Bill BL-2024-012)   Running: ₹300.00
Jan 20: Payment -₹500.00                      Running: -₹200.00

Current balance: -₹200.00 (shop owes Ramesh ₹200)
```

---

### 7. `list_customers_with_balances`

**Description:** Returns all customers for a store with their current balance. Useful for "who owes me money?" queries.

**Signature:**
```python
async def list_customers_with_balances(
    store_id: str,
    filter: str = 'ALL'  # 'ALL' | 'OWES_SHOP' | 'SHOP_OWES' | 'SETTLED'
) -> List[CustomerBalanceSummary]
```

**DB Operations:**
```sql
SELECT c.id, c.name, c.phone, COALESCE(SUM(ke.amount_delta), 0) as balance
FROM customers c
LEFT JOIN khata_entries ke ON ke.customer_id = c.id AND ke.store_id = c.store_id
WHERE c.store_id = ? AND c.is_active = TRUE
GROUP BY c.id, c.name, c.phone
ORDER BY ABS(COALESCE(SUM(ke.amount_delta), 0)) DESC;
```

---

## Guardrails

| Situation | Guardrail |
|---|---|
| Payment for non-existent customer | Error: "Customer not found. Add them first." |
| Payment > current outstanding balance | Warning: "Ramesh currently owes ₹200. You're recording ₹500. This will mean the shop owes Ramesh ₹300. Confirm?" |
| Negative amount passed | Validation: `amount` must be positive |
| Adding entry for inactive customer | Error: "This customer's account is deactivated." |

---

## Worked Example (from user instructions)

```
Jan 15: Customer buys ₹200 on credit
  → finalize_bill(is_credit=True, customer_id=ramesh)
  → Billing MCP → khata_mcp.add_credit_entry(amount=200, reference_bill_id=bill-001)
  → Entry: CREDIT +200

Jan 20: Customer buys ₹100 on credit AND pays ₹500 cash
  → Bill 1 (credit): finalize_bill(is_credit=True, customer_id=ramesh, amount=100)
  → Entry: CREDIT +100
  
  → Owner: "Ramesh paid ₹500"
  → Agent: "Ramesh currently owes ₹300. Recording ₹500 payment means shop will owe
           Ramesh ₹200. Confirm?"
  → Owner: "yes"
  → khata_mcp.add_payment_entry(customer_id=ramesh, amount=500)
  → Entry: PAYMENT -500

Owner: "Ramesh's balance?"
→ khata_mcp.get_balance(customer_id=ramesh)
→ SUM(200 + 100 - 500) = -200
→ Agent: "Shop owes Ramesh ₹200 (he has a credit of ₹200 with your shop)"
```

---

## Phase 2 Extensibility

| Feature | Change |
|---|---|
| Payment reminders | Add `schedule_reminder(customer_id, due_date, amount)` tool |
| Credit limit | Add `credit_limit` to customers, validate in `add_credit_entry` |
| Partial payment tracking | Already supported — each payment is a separate entry |
| Statement PDF | Add to Documents MCP — data already available from `get_khata_history` |
