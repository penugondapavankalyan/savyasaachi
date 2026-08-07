# Implementation Guide 8: Testing and Validation

**Order:** Eighth — full end-to-end validation of the deployed system.  
**Reference Docs:** PDF §4 (Hard Parts), PDF §3 (What the Owner Must Be Able to Do)

---

## Overview

This guide covers all test scenarios required to validate the Kirana Agent against the original brief. Every "hard part" from §4 of the spec must be explicitly tested and verified.

---

## Test Environment Setup

```bash
# Set environment for local testing
export LLM_PROVIDER=ollama
export LLM_MODEL=llama3.1
export SUPABASE_URL=<your_url>
export SUPABASE_SERVICE_ROLE_KEY=<your_key>
export UPSTASH_REDIS_REST_URL=<your_url>
export UPSTASH_REDIS_REST_TOKEN=<your_token>

# Use a dedicated test Telegram user ID (not real user)
TEST_USER_ID=99999999
```

Create test data seeding script:
```python
# scripts/seed_test_data.py
# Creates a test store, products, and initial inventory
# for running all test scenarios
```

---

## Test Suite 1: Registration Flow

### T1.1 — New User Registration
```
Input: First message from new telegram_user_id
Expected:
  ✅ workflow_state = UNREGISTERED → PENDING_CATALOGUE after store created
  ✅ users record created
  ✅ stores record created with provided details
  ✅ registrations record shows COMPLETE
  ✅ Agent prompts to add first product
```

### T1.2 — Registration Idempotency
```
Input: Repeat the registration message (simulating Telegram redeliver)
Expected:
  ✅ No duplicate user or store records
  ✅ Agent returns existing store info
  ✅ No exception or error
```

### T1.3 — Invalid GSTIN
```
Input: "My shop, GSTIN: INVALID123"
Expected:
  ✅ Agent asks for correct GSTIN format or offers to skip
  ✅ No store created with invalid GSTIN
```

---

## Test Suite 2: Catalogue Management

### T2.1 — Add Loose Item
```
Input: "add Sugar, loose, KG, cost 38, MRP 45, reorder 5"
Expected:
  ✅ Product created with is_loose=TRUE, gst_rate=0.00
  ✅ DB trigger: no GST applied
  ✅ workflow_state → PENDING_INVENTORY (first product)
```

### T2.2 — Add Branded Item with GST
```
Input: "add Maggi 70g, branded, 12% GST, MRP 14, cost 12, reorder 20"
Expected:
  ✅ Product created with gst_rate=12.00, hsn_code captured
  ✅ Correct CGST/SGST rates implied (6% each)
```

### T2.3 — Duplicate Product (Same Name+Brand)
```
Input: Add "Maggi 70g" twice with different MRP
Expected:
  ✅ Second add UPDATES price, not duplicate record
  ✅ Count of products remains same
```

### T2.4 — Disambiguation (Two Products Match)
```
Setup: Add "Aashirvaad Atta 5kg" and "Pillsbury Atta 1kg"
Input: "add atta to bill"
Expected:
  ✅ Agent asks: "Which atta — Aashirvaad 5kg or Pillsbury 1kg?"
  ✅ Model does NOT guess
```

---

## Test Suite 3: Inventory Management

### T3.1 — Receive Stock
```
Input: "50 packets of Maggi came in, cost ₹12"
Expected:
  ✅ inventory record: quantity_in_stock = 50
  ✅ stock_movements: STOCK_IN +50
  ✅ workflow_state → ACTIVE (first stock-in)
  ✅ products.cost_price updated to 12
```

### T3.2 — Stock Query
```
Input: "how much sugar is left?"
Expected:
  ✅ Returns current quantity from inventory table
  ✅ Shows unit and reorder level
```

### T3.3 — Low Stock Query
```
Setup: Sell enough Maggi to go below reorder level (20)
Input: "what's running out?"
Expected:
  ✅ Returns list of items at or below reorder level
  ✅ Sorted by urgency (out of stock first)
```

---

## Test Suite 4: Billing (The Core Hard Parts)

### T4.1 — Simple Bill
```
Input: "make a bill: 2kg sugar, 4 Maggi, UPI"
Expected:
  ✅ Sugar: 2kg × ₹45 = ₹90.00, 0% GST
  ✅ Maggi: 4 × ₹14 = ₹56.00, 12% GST
     taxable=56, cgst=3.36, sgst=3.36, line=62.72
  ✅ Total = ₹90 + ₹62.72 = ₹152.72
  ✅ bills record created
  ✅ inventory decremented: sugar -2, maggi -4
  ✅ stock_movements: 2 SALE entries
```

### T4.2 — Multi-Turn Bill (PDF §4 Point 4)
```
Input msg 1: "2kg sugar, 1 Aashirvaad atta"
Input msg 2: (10 minutes later) "also 4 Maggi"
Input msg 3: "UPI, done"
Expected:
  ✅ All 3 messages go to same draft bill (same workflow_id)
  ✅ Final bill has all 3 items
  ✅ Stock decremented only on finalize (not on each add)
```

### T4.3 — Edit Mid-Build (PDF §3)
```
Input msg 1: "2 butter, 4 Maggi"
Input msg 2: "drop the butter, make it 6 Maggi"
Expected:
  ✅ Butter removed from draft
  ✅ Maggi quantity updated to 6
  ✅ Final bill has only 6 Maggi
```

### T4.4 — Oversell Guard (PDF §4 Point 2) — THE CRITICAL TEST
```
Setup: Maggi stock = 6
Input: "10 Maggi, UPI"
Expected:
  ✅ Agent informs: "Only 6 Maggi available (you asked for 10)"
  ✅ Agent asks: partial fulfillment or skip?
  ✅ If owner says yes → bill created with 6 Maggi
  ✅ If owner says no → Maggi not added to bill
  ✅ Stock never goes negative (DB CHECK constraint verified)
```

### T4.5 — Idempotency (PDF §4 Point 5) — THE CRITICAL TEST
```
Setup: Finalize a bill successfully
Action: Simulate Telegram redeliver — call finalize_bill again with same workflow_id
Expected:
  ✅ Second call returns existing bill (already_finalized=True)
  ✅ bills table has exactly 1 record with this workflow_id
  ✅ stock_movements has exactly 1 set of SALE entries for this bill
  ✅ No double-decrement
```

### T4.6 — Concurrency Safety (PDF §4 Point 6)
```
Setup: Maggi stock = 5
Action: Simultaneously finalize two bills both requesting 4 Maggi
Method: Call finalize_bill twice concurrently (asyncio.gather)
Expected:
  ✅ One bill succeeds (stock: 5→1)
  ✅ Other bill fails with InsufficientStockError
  ✅ Stock never goes negative
  ✅ total stock_movements for SALE = 4 (not 8)
```

### T4.7 — GST Correctness (PDF §4 Point 3)
```
Test case: 3 items of Surf Excel 1kg (18% GST), MRP ₹120
  taxable = 3 × 120 = ₹360.00
  gst = 360 × 18/100 = ₹64.80
  cgst = ROUND(64.80/2, 2) = ₹32.40
  sgst = 64.80 - 32.40 = ₹32.40
  line = 360 + 32.40 + 32.40 = ₹424.80

Expected:
  ✅ bill_items.cgst_amount = 32.40
  ✅ bill_items.sgst_amount = 32.40
  ✅ bill_items.line_total = 424.80
  ✅ PDF invoice shows correct values
```

### T4.8 — Don't Sell Below Cost
```
Setup: Create product with cost=₹30, MRP=₹25 (below cost)
Input: Try to bill this product
Expected:
  ✅ Agent warns owner
  ✅ finalize_bill raises BelowCostError or returns error
```

---

## Test Suite 5: Khata (Credit Ledger)

### T5.1 — Full Khata Cycle (from user instructions)
```
Step 1: Customer buys ₹200 on credit
  → finalize_bill(is_credit=True, customer_id=ramesh)
  → khata_entry: CREDIT +200

Step 2: Customer buys ₹100 on credit
  → khata_entry: CREDIT +100

Step 3: Customer pays ₹500
  → Input: "Ramesh paid ₹500"
  → Agent warns: "Balance is ₹300, paying ₹500 will mean shop owes ₹200. Confirm?"
  → Owner confirms
  → khata_entry: PAYMENT -500

Step 4: Balance query
  → Input: "Ramesh's balance?"
  → SUM(200 + 100 - 500) = -200
  ✅ Agent: "Shop owes Ramesh ₹200"
```

### T5.2 — Non-existent Customer (PDF §4 Point 7)
```
Input: "Suresh paid ₹500" (Suresh not in customers)
Expected:
  ✅ Agent: "I don't have a customer named Suresh. Would you like to add them first?"
  ✅ No khata_entry created
```

### T5.3 — Ambiguous Customer Name
```
Setup: Two customers named "Ramesh" with different phone numbers
Input: "Ramesh's balance?"
Expected:
  ✅ Agent: "Which Ramesh — Ramesh Kumar (9876) or Ramesh Sharma (9988)?"
```

---

## Test Suite 6: Analytics

### T6.1 — Daily Summary
```
Input: "today's sales?"
Expected:
  ✅ Shows total revenue, bill count, CGST+SGST breakdown
  ✅ Payment split (cash/UPI/card/credit)
  ✅ Top items list
```

### T6.2 — Close Day
```
Input: "close the day"
Expected:
  ✅ daily_summary record created for today
  ✅ Re-running is idempotent (updates, not duplicates)
```

---

## Test Suite 7: Documents

### T7.1 — PDF Invoice (PDF §4 Point 8)
```
Input: "send me that bill as a PDF"
Expected:
  ✅ PDF file sent via Telegram
  ✅ Contains correct bill number, date, items, GST breakup
  ✅ /tmp file deleted after send
```

### T7.2 — PPTX Analysis Deck (PDF §4 Point 8)
```
Input: "make this week's sales analysis deck"
Expected:
  ✅ PPTX file sent via Telegram
  ✅ Has 5 slides (summary, trend, top items, stock, GST)
  ✅ Charts contain real data (not empty)
  ✅ /tmp file deleted after send
```

---

## Test Suite 8: Memory and Preferences (PDF §4 Point 9)

### T8.1 — Preference Persistence Across /new
```
Step 1: "always assume UPI unless I say cash"
Step 2: /new (clears chat)
Step 3: "make a bill: 4 Maggi"
Expected:
  ✅ Agent assumes UPI payment without being told
  ✅ stores.default_payment_mode = 'UPI'
  ✅ Preference survived /new
```

### T8.2 — Preferred Brand
```
Step 1: "default atta = Aashirvaad 5kg"
Step 2: /new
Step 3: "make a bill: 1 atta"
Expected:
  ✅ Agent uses Aashirvaad Atta 5kg without asking
  ✅ Preference from stores.preferences JSONB
```

---

## Test Suite 9: Reorder Alert

### T9.1 — Alert Fires After Sale
```
Setup: Maggi reorder_level = 20, stock = 22
Action: Sell 5 Maggi (stock → 17, below reorder level)
Expected:
  ✅ Owner receives Telegram message: "⚠️ Low Stock Alert: Maggi..."
  ✅ Alert message shows current quantity and reorder level
```

### T9.2 — No Alert Above Reorder Level
```
Setup: Maggi reorder_level = 20, stock = 50
Action: Sell 5 Maggi (stock → 45, above reorder level)
Expected:
  ✅ No alert message sent
```

---

## Deployment Validation (Production Check)

```bash
# 1. Verify webhook is active
curl "https://api.telegram.org/bot${TOKEN}/getWebhookInfo"
# Should show: "has_custom_certificate": false, "pending_update_count": 0

# 2. Send test message via Telegram UI
# Message: "hello"
# Expected: Agent responds with registration prompt

# 3. Run full E2E scenario manually:
#    Register → Add product → Add stock → Cut bill → Check khata → Get PDF → Get PPTX
```

---

## Checklist: PDF §4 Hard Parts

| Hard Part | Test | Status |
|---|---|---|
| 1. Grounding (no invented data) | T2.4, T4.1 | |
| 2. Oversell guard | T4.4 | |
| 3. GST correctness | T4.7 | |
| 4. Multi-turn bills | T4.2, T4.3 | |
| 5. Idempotency | T4.5 | |
| 6. Concurrency | T4.6 | |
| 7. Guardrails | T4.8, T5.2 | |
| 8. Real artifacts (PDF + PPTX) | T7.1, T7.2 | |
| 9. Memory across sessions | T8.1, T8.2 | |

---

## Automated Test Runner

```bash
# Run all unit tests
python -m pytest tests/ -v

# Run integration tests (requires real Supabase + Redis)
python -m pytest tests/integration/ -v --timeout=30

# Run specific hard-part tests
python -m pytest tests/test_oversell_guard.py tests/test_idempotency.py tests/test_gst.py -v
```
