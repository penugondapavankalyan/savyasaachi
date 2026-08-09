# Kirana Agent — Documentation Generation Plan

## Top-Level Overview

Generate a complete, detailed `/docs` folder that serves as the authoritative specification
for the Kirana Store Agent system. Every `.md` file will contain enough detail that a
developer can implement the system purely from these docs — schema, contracts, business
rules, tool signatures, event flows, and deployment guides.

**Stack (confirmed):**
- Agent harness: PydanticAI
- LLM: Ollama (dev) / Groq (prod)
- Database: Supabase (PostgreSQL, ACID)
- Deployment: AWS Lambda (Function URL, no API Gateway)
- MCP: Python modules co-deployed in same Lambda (extractable in Phase 2)
- Session/conversation history: Upstash Redis (TTL-based, HTTP API)
- Draft bill state: Supabase `draft_bill` table
- PDF: TBD library (interface documented)
- PPTX: python-pptx
- Generated files: Lambda /tmp → streamed to Telegram
- GST: Intra-state only (CGST + SGST split) in Phase 1
- Phase 1: 1 store per user, 1 user per store (owner only)
- Section 7 stretch goals: excluded from Phase 1, architecture must support future addition

---

## Folder Structure to Generate

```
docs/
├── overview.md
├── architecture.md
├── database/
│   ├── identity/
│   │   ├── users.md
│   │   ├── stores.md
│   │   └── registrations.md
│   ├── catalogue/
│   │   └── products.md
│   ├── inventory/
│   │   ├── inventory.md
│   │   └── stock_movements.md
│   ├── billing/
│   │   ├── draft_bills.md
│   │   ├── draft_bill_items.md
│   │   ├── bills.md
│   │   └── bill_items.md
│   ├── khata/
│   │   ├── customers.md
│   │   └── khata_entries.md
│   ├── analytics/
│   │   └── daily_summary.md
│   └── session/
│       └── workflow_state.md
├── mcp/
│   ├── identity/
│   │   └── identity_mcp.md
│   ├── catalogue/
│   │   └── catalogue_mcp.md
│   ├── inventory/
│   │   └── inventory_mcp.md
│   ├── billing/
│   │   └── billing_mcp.md
│   ├── khata/
│   │   └── khata_mcp.md
│   ├── analytics/
│   │   └── analytics_mcp.md
│   └── documents/
│       └── documents_mcp.md
├── agent/
│   ├── pydantic_ai_agent.md
│   ├── workflow_state_machine.md
│   ├── conversation_history.md
│   └── guardrails.md
├── events/
│   ├── reorder_alert.md
│   └── session_events.md
├── infrastructure/
│   ├── lambda.md
│   ├── supabase.md
│   └── upstash_redis.md
└── implementation/
    ├── 1_build_schema.md
    ├── 2_build_mcp_modules.md
    ├── 3_build_agent.md
    ├── 4_build_telegram_handler.md
    ├── 5_build_lambda_deployer.md
    ├── 6_build_pdf_invoice.md
    ├── 7_build_pptx_analytics.md
    └── 8_testing_and_validation.md
```

---

## Sub-Tasks

### Sub-Task 1 — Top-Level Overview & Architecture Docs
**Status:** [ ] pending

**Intent:**
Establish the bird's-eye view of the entire system — what it is, how the pieces fit together,
data flow from Telegram message to DB and back, deployment topology, and Phase 1 vs Phase 2
boundary. These docs are the entry point for any developer joining the project.

**Expected Outcomes:**
- `docs/overview.md` — project summary, goals, Phase 1 scope, Phase 2 roadmap
- `docs/architecture.md` — full system architecture: Lambda, Supabase, Upstash Redis,
  Telegram webhook, PydanticAI agent, MCP modules, data flow diagram (ASCII), deployment
  topology, scalability notes

**Todo List:**
1. Write `docs/overview.md` with: project purpose, tech stack, Phase 1 scope boundaries,
   Phase 2 extensibility notes, key business rules summary
2. Write `docs/architecture.md` with: component diagram (ASCII), request lifecycle
   (Telegram → Lambda URL → workflow state check → PydanticAI agent → MCP module →
   Supabase → response → Telegram), MCP module ownership map, Redis session design,
   Lambda cold start considerations, ACID guarantees via Supabase

**Relevant Context:**
- PDF §1 (brief), §4 (hard parts), §5 (architecture requirements)
- Phase 1 constraints: 1 user per store, 1 store per user, owner only

---

### Sub-Task 2 — Identity Domain: Database Docs
**Status:** [ ] pending

**Intent:**
Document every table owned by the Identity MCP — users, stores, and registrations.
These are the foundation tables that every other domain references.

**Expected Outcomes:**
- `docs/database/identity/users.md` — full schema, constraints, indexes, RLS policies,
  relations to stores/registrations, Phase 2 extensibility notes
- `docs/database/identity/stores.md` — full schema, GST info, shop preferences, relations
- `docs/database/identity/registrations.md` — registration workflow table, status enum,
  relation to users and stores

**Todo List:**
1. Write `users.md`: columns (id, telegram_user_id, username, created_at, updated_at),
   constraints, indexes, RLS, relation to stores (1:1 Phase 1, 1:N Phase 2 note),
   relation to registrations
2. Write `stores.md`: columns (id, owner_user_id, shop_name, gstin, state_code,
   address, default_payment_mode, preferences JSONB, created_at, updated_at),
   constraints, indexes, RLS, relation to users, products, inventory, bills, khata
3. Write `registrations.md`: columns (id, telegram_user_id, store_id, status enum,
   created_at, completed_at), status flow (INITIATED → STORE_CREATED → COMPLETE),
   relation to users and stores, idempotency notes

**Relevant Context:**
- Phase 1: 1 store per telegram user (enforced by unique constraint on users.telegram_user_id)
- Preferences JSONB on stores: default_payment_mode, preferred_brands, shop_name_on_invoice
- PDF §4 point 9: memory across sessions — preferences live in stores table

---

### Sub-Task 3 — Catalogue Domain: Database Docs
**Status:** [ ] pending

**Intent:**
Document the products/catalogue table — the master list of all SKUs a store sells.
This is the source of truth for item names, GST slabs, HSN codes, units, and pricing.

**Expected Outcomes:**
- `docs/database/catalogue/products.md` — full schema, GST slab enum, HSN codes,
  loose vs branded flag, unit enum, reorder threshold, relations to inventory and bill_items

**Todo List:**
1. Write `products.md`: columns (id, store_id, name, brand, is_loose, unit enum,
   hsn_code, gst_rate, cost_price, mrp, reorder_level, is_active, created_at, updated_at),
   constraints (unique name+brand per store), indexes, GST slab logic (0% loose,
   5% packaged staples, 12-18% FMCG), loose items always 0% GST rule,
   relation to inventory and bill_items, Phase 2 notes (batch/expiry tracking)

**Relevant Context:**
- PDF §2: real SKUs, loose vs packaged, GST rates, HSN codes
- PDF §4 point 1: prices from DB via tools, never invented
- User instruction: loose items have no GST; branded items have GST; bot asks user during
  catalogue addition for: item name, loose or branded, GST if branded
- Two items with same base name but different brands treated separately
  (e.g. Pillsbury Atta and Aashirvaad Atta are separate SKUs)

---

### Sub-Task 4 — Inventory Domain: Database Docs
**Status:** [ ] pending

**Intent:**
Document the inventory and stock movements tables. Inventory tracks current quantity.
Stock movements provide an immutable audit trail of every increment/decrement.

**Expected Outcomes:**
- `docs/database/inventory/inventory.md` — full schema, atomic decrement design,
  reorder level check, relation to products
- `docs/database/inventory/stock_movements.md` — full schema, movement type enum,
  relation to inventory and bills, audit trail design

**Todo List:**
1. Write `inventory.md`: columns (id, store_id, product_id, quantity_in_stock,
   reorder_level, last_restocked_at, updated_at), constraints (non-negative quantity
   enforced at DB level via CHECK constraint), unique (store_id, product_id), indexes,
   atomic decrement pattern (SELECT FOR UPDATE or Supabase RPC), relation to products,
   stock_movements, reorder alert trigger logic
2. Write `stock_movements.md`: columns (id, store_id, product_id, movement_type enum
   [STOCK_IN, SALE, ADJUSTMENT], quantity_delta, reference_id, reference_type, notes,
   created_at), immutable (no updates/deletes), relation to inventory and bills,
   how it supports daily analytics

**Relevant Context:**
- PDF §4 point 2: oversell guard enforced at tool layer (DB), not prompt
- PDF §4 point 6: concurrency — two bills in flight must not corrupt stock
- User instruction: partial fulfillment flow — if stock < requested qty, bot asks
  if partial fulfillment is acceptable

---

### Sub-Task 5 — Billing Domain: Database Docs
**Status:** [ ] pending

**Intent:**
Document all four billing tables — draft_bills, draft_bill_items, bills, bill_items.
Draft tables hold in-progress bills (multi-turn). Final tables are immutable records.

**Expected Outcomes:**
- `docs/database/billing/draft_bills.md` — session-linked draft, status enum, TTL logic
- `docs/database/billing/draft_bill_items.md` — line items of draft, editable
- `docs/database/billing/bills.md` — finalized bill, GST breakup, payment info
- `docs/database/billing/bill_items.md` — immutable finalized line items with GST per item

**Todo List:**
1. Write `draft_bills.md`: columns (id, store_id, telegram_user_id, workflow_id,
   status enum [OPEN, CONFIRMED, CANCELLED], created_at, updated_at, expires_at),
   relation to draft_bill_items, idempotency design (workflow_id uniqueness),
   TTL/expiry handling, relation to bills on finalization
2. Write `draft_bill_items.md`: columns (id, draft_bill_id, product_id, quantity,
   unit_price, gst_rate, is_partial_fulfillment, created_at, updated_at),
   editable until draft_bill is CONFIRMED, relation to products
3. Write `bills.md`: columns (id, store_id, bill_number, telegram_user_id,
   workflow_id, subtotal, total_cgst, total_sgst, total_amount, payment_mode enum,
   payment_reference, is_credit, customer_id, created_at), immutable after creation,
   idempotency via workflow_id unique constraint, relation to bill_items, khata_entries
4. Write `bill_items.md`: columns (id, bill_id, product_id, product_name_snapshot,
   quantity, unit_price, gst_rate, cgst_amount, sgst_amount, line_total, created_at),
   immutable, product name snapshotted at time of billing, GST per line item detail,
   relation to bills

**Relevant Context:**
- PDF §4 point 4: multi-turn bills, edits supported, stock decremented only on finalize
- PDF §4 point 5: idempotency — retried finalize must not double-bill
- PDF §4 point 3: GST correctness — per-item slab, CGST/SGST split, rounding, tax breakup
- User instruction: workflow_id tracks bill session across time gaps (9am + 9:10am = same bill)

---

### Sub-Task 6 — Khata Domain: Database Docs
**Status:** [ ] pending

**Intent:**
Document the customers and khata_entries tables. Khata is the credit ledger —
every credit transaction is an entry; balance is always computed by summing entries.

**Expected Outcomes:**
- `docs/database/khata/customers.md` — customer profile per store
- `docs/database/khata/khata_entries.md` — ledger entries, positive=credit owed,
  negative=payment received, balance query design

**Todo List:**
1. Write `customers.md`: columns (id, store_id, name, phone, created_at, updated_at),
   unique (store_id, phone), unique (store_id, name) soft constraint with note,
   relation to khata_entries and bills, Phase 2 notes (payment reminders)
2. Write `khata_entries.md`: columns (id, store_id, customer_id, amount_delta,
   entry_type enum [CREDIT, PAYMENT, ADJUSTMENT], reference_bill_id nullable,
   notes, created_at), immutable entries (no updates/deletes — append-only ledger),
   balance query (SUM of amount_delta), sign convention (positive = customer owes shop,
   negative = shop owes customer), relation to customers and bills

**Relevant Context:**
- User instruction: credit example — buy 200 on credit → +200 entry; next time buy 100 +
  pay 500 → +100 entry and -500 entry; balance = -200 (shop owes customer)
- PDF §2: khata is a first-class kirana concept
- PDF §4 point 7: don't settle a khata that doesn't exist — confirm or refuse

---

### Sub-Task 7 — Analytics & Session Domain: Database Docs
**Status:** [ ] pending

**Intent:**
Document the daily_summary and workflow_state tables. Daily summary supports the
"close the day" feature. Workflow state drives the pre-agent context loader.

**Expected Outcomes:**
- `docs/database/analytics/daily_summary.md` — daily aggregated sales data
- `docs/database/session/workflow_state.md` — per-user workflow state machine table

**Todo List:**
1. Write `daily_summary.md`: columns (id, store_id, date, total_sales, total_cgst,
   total_sgst, total_tax, cash_sales, upi_sales, card_sales, credit_sales,
   top_items JSONB, bill_count, created_at, updated_at), how it is populated
   (on daily close or computed from bills), relation to bills
2. Write `workflow_state.md`: columns (id, telegram_user_id, store_id nullable,
   current_state enum [UNREGISTERED, PENDING_CATALOGUE, PENDING_INVENTORY, ACTIVE],
   active_draft_bill_id nullable, updated_at), state transition rules,
   how Lambda reads this before agent invocation, relation to users, stores, draft_bills

**Relevant Context:**
- User instruction: state machine — UNREGISTERED → (registration) → PENDING_CATALOGUE
  → (≥1 product added) → PENDING_INVENTORY → (stock added) → ACTIVE
- User instruction: workflow_id tracks bill sessions across time gaps

---

### Sub-Task 8 — Identity MCP Module Docs
**Status:** [ ] pending

**Intent:**
Document the Identity MCP — the Python module that owns all user, store, and
registration operations. This is the first MCP invoked for any new user.

**Expected Outcomes:**
- `docs/mcp/identity/identity_mcp.md` — full tool list, function signatures,
  business rules, DB operations, error handling, Phase 2 extensibility

**Todo List:**
1. Document owned tables: users, stores, registrations, workflow_state
2. Document all tools/functions:
   - `check_user_registration(telegram_user_id)` → registration status
   - `register_user(telegram_user_id, username)` → creates user record
   - `create_store(telegram_user_id, shop_name, gstin, address)` → creates store,
     links to user, updates workflow_state to PENDING_CATALOGUE
   - `get_store(telegram_user_id)` → returns store details
   - `update_store_preferences(store_id, preferences)` → updates JSONB preferences
   - `get_workflow_state(telegram_user_id)` → returns current state enum
   - `advance_workflow_state(telegram_user_id, new_state)` → transitions state
3. Document business rules: 1 store per user (Phase 1), idempotency on registration,
   preferences persistence across sessions
4. Document error cases and guardrails

**Relevant Context:**
- PDF §4 point 9: preferences persist across sessions — stored in stores.preferences JSONB
- User instruction: on first open → check registration → if not registered → prompt registration

---

### Sub-Task 9 — Catalogue MCP Module Docs
**Status:** [ ] pending

**Intent:**
Document the Catalogue MCP — owns all product/SKU management operations.

**Expected Outcomes:**
- `docs/mcp/catalogue/catalogue_mcp.md` — full tool list, GST slab logic,
  loose vs branded rules, search/lookup tools

**Todo List:**
1. Document owned tables: products
2. Document all tools/functions:
   - `add_product(store_id, name, brand, is_loose, unit, hsn_code, gst_rate,
     cost_price, mrp, reorder_level)` → creates product
   - `get_product(store_id, product_id)` → returns product detail
   - `search_products(store_id, query)` → fuzzy search by name/brand
   - `list_products(store_id)` → all active products
   - `update_product(store_id, product_id, fields)` → update pricing/details
   - `deactivate_product(store_id, product_id)` → soft delete
3. Document GST rules: loose items always 0%, branded items carry GST rate set at creation,
   HSN code required for branded items, CGST/SGST split formula
4. Document workflow state advancement: when first product added → advance to PENDING_INVENTORY
5. Document the bot conversation flow for adding a product (name → loose or branded →
   GST if branded → MRP → cost price → reorder level)

**Relevant Context:**
- User instruction: bot asks user — item name, loose or branded, GST if branded
- User instruction: Pillsbury Atta and Aashirvaad Atta are separate SKUs
- PDF §2: real SKUs, units, GST slabs

---

### Sub-Task 10 — Inventory MCP Module Docs
**Status:** [ ] pending

**Intent:**
Document the Inventory MCP — owns stock-in, stock queries, reorder alerts,
and the partial fulfillment check used by billing.

**Expected Outcomes:**
- `docs/mcp/inventory/inventory_mcp.md` — full tool list, atomic decrement design,
  reorder alert trigger, partial fulfillment logic

**Todo List:**
1. Document owned tables: inventory, stock_movements
2. Document all tools/functions:
   - `receive_stock(store_id, product_id, quantity, cost_price)` → stock-in, creates
     stock_movement [STOCK_IN], updates inventory, updates workflow_state if PENDING_INVENTORY
   - `get_stock(store_id, product_id)` → current quantity
   - `check_availability(store_id, product_id, requested_qty)` → returns available qty,
     whether full/partial/none fulfillment possible
   - `decrement_stock(store_id, product_id, quantity, bill_id)` → atomic decrement,
     creates stock_movement [SALE], triggers reorder check
   - `get_low_stock_items(store_id)` → items at or below reorder_level
   - `get_stock_movements(store_id, product_id, date_range)` → audit trail
3. Document oversell guard: enforced at DB level (CHECK quantity >= 0) AND at tool layer
4. Document partial fulfillment flow: check_availability → if partial possible → return
   available_qty + flag → agent prompts user → if yes → bill with available_qty
5. Document reorder alert: triggered after decrement_stock when quantity <= reorder_level
6. Document concurrency: SELECT FOR UPDATE or Supabase RPC for atomic decrements
7. Document workflow state advancement: when first stock_in → advance to ACTIVE

**Relevant Context:**
- PDF §4 point 2: oversell guard at tool layer
- PDF §4 point 6: concurrency safety
- User instruction: partial fulfillment flow

---

### Sub-Task 11 — Billing MCP Module Docs
**Status:** [ ] pending

**Intent:**
Document the Billing MCP — owns the full bill lifecycle from draft creation through
multi-turn editing to finalization with GST computation and stock decrement.

**Expected Outcomes:**
- `docs/mcp/billing/billing_mcp.md` — full tool list, GST computation, multi-turn
  bill design, idempotency, finalization flow

**Todo List:**
1. Document owned tables: draft_bills, draft_bill_items, bills, bill_items
2. Document all tools/functions:
   - `create_draft_bill(store_id, telegram_user_id, workflow_id)` → creates/returns
     existing draft (idempotent on workflow_id)
   - `add_item_to_draft(draft_bill_id, product_id, quantity)` → adds/updates line item,
     calls inventory check_availability
   - `remove_item_from_draft(draft_bill_id, product_id)` → removes line item
   - `update_item_quantity(draft_bill_id, product_id, new_quantity)` → updates qty
   - `get_draft_bill(draft_bill_id)` → returns full draft with computed totals
   - `finalize_bill(draft_bill_id, payment_mode, payment_reference, is_credit,
     customer_id)` → validates, computes GST, creates bill+bill_items, calls
     inventory.decrement_stock for each item, marks draft CONFIRMED (idempotent)
   - `cancel_draft_bill(draft_bill_id)` → marks draft CANCELLED
   - `get_bill(bill_id)` → returns finalized bill detail
   - `get_bills_by_date(store_id, date)` → list bills for a date
3. Document GST computation: per-item (quantity × unit_price × gst_rate / 2 for CGST
   and SGST each), rounding rules, total breakup
4. Document idempotency: workflow_id unique constraint on bills table — retried finalize
   returns existing bill without re-processing
5. Document multi-turn bill: workflow_id links messages across time gaps to same draft
6. Document the don't-sell-below-cost guardrail

**Relevant Context:**
- PDF §4 points 3, 4, 5, 6, 7
- User instruction: workflow_id ties 9am and 9:10am messages to same bill
- User instruction: once items confirmed → push to billing system with bill_id

---

### Sub-Task 12 — Khata MCP Module Docs
**Status:** [ ] pending

**Intent:**
Document the Khata MCP — owns customer profiles and the credit ledger.

**Expected Outcomes:**
- `docs/mcp/khata/khata_mcp.md` — full tool list, ledger entry design,
  balance computation, guardrails

**Todo List:**
1. Document owned tables: customers, khata_entries
2. Document all tools/functions:
   - `add_customer(store_id, name, phone)` → creates customer profile
   - `get_customer(store_id, name_or_phone)` → lookup customer
   - `add_credit_entry(store_id, customer_id, amount, reference_bill_id)` → positive entry
   - `add_payment_entry(store_id, customer_id, amount)` → negative entry
   - `get_balance(store_id, customer_id)` → SUM of all amount_delta entries
   - `get_khata_history(store_id, customer_id)` → all entries with dates
   - `list_customers_with_balances(store_id)` → all customers + current balance
3. Document sign convention: positive = customer owes shop, negative = shop owes customer
4. Document guardrail: cannot add payment entry for non-existent customer
5. Document Phase 2 note: payment reminder hooks (fields already present)

**Relevant Context:**
- User instruction: credit example with 200, 100, 500 scenario → balance -200
- PDF §2: khata is first-class kirana concept
- PDF §4 point 7: don't settle khata that doesn't exist

---

### Sub-Task 13 — Analytics MCP Module Docs
**Status:** [ ] pending

**Intent:**
Document the Analytics MCP — owns daily close, sales queries, stock health reports,
and data aggregation that feeds the PPTX deck generation.

**Expected Outcomes:**
- `docs/mcp/analytics/analytics_mcp.md` — full tool list, daily close logic,
  data aggregation for PPTX

**Todo List:**
1. Document owned tables: daily_summary (read/write), bills (read), bill_items (read),
   inventory (read), stock_movements (read)
2. Document all tools/functions:
   - `get_daily_summary(store_id, date)` → totals, tax, payment breakdown, top items
   - `close_day(store_id, date)` → aggregates bills for date into daily_summary
   - `get_sales_trend(store_id, start_date, end_date)` → time-series sales data
   - `get_top_items(store_id, period)` → best-selling items by qty and revenue
   - `get_stock_health(store_id)` → current inventory status with reorder flags
   - `get_gst_summary(store_id, period)` → CGST/SGST collected per period
3. Document PPTX data contract: what data shapes analytics_mcp returns for the
   documents_mcp to render into charts

**Relevant Context:**
- PDF §3: "today's sales", "close the day", "this week's analysis deck"
- PDF §4 point 8: real PPTX with real charts

---

### Sub-Task 14 — Documents MCP Module Docs
**Status:** [ ] pending

**Intent:**
Document the Documents MCP — owns PDF invoice generation and PPTX deck generation.
Files are generated in Lambda /tmp and streamed to Telegram.

**Expected Outcomes:**
- `docs/mcp/documents/documents_mcp.md` — full tool list, PDF invoice structure,
  PPTX deck structure, file generation flow

**Todo List:**
1. Document all tools/functions:
   - `generate_invoice_pdf(bill_id)` → fetches bill+bill_items from DB, renders
     GST-correct PDF invoice (layout spec: header, items table, GST breakup, totals,
     payment info, shop GSTIN), writes to /tmp, returns file path
   - `generate_analysis_pptx(store_id, period)` → fetches analytics data, renders
     PPTX with charts (sales trend, top items, stock health, GST collected),
     writes to /tmp using python-pptx, returns file path
2. Document PDF invoice layout: shop name + GSTIN, bill number, date, customer info,
   line items (name, qty, unit, MRP, GST%, CGST, SGST, line total), subtotal,
   total CGST, total SGST, grand total, payment mode
3. Document PPTX deck structure: slide 1 (summary), slide 2 (sales trend chart),
   slide 3 (top items bar chart), slide 4 (stock health table), slide 5 (GST summary)
4. Document file handling: /tmp storage, stream to Telegram via sendDocument API,
   no persistent storage, file cleaned up after send
5. Document PDF library interface: abstract interface so library can be swapped

**Relevant Context:**
- PDF §4 point 8: proper GST invoice (PDF) and business analysis deck (PPTX)
- PDF §3: "send me that bill as a PDF", "make this week's analysis deck"
- Decision: Lambda /tmp, streamed to Telegram

---

### Sub-Task 15 — Agent & Workflow Docs
**Status:** [ ] pending

**Intent:**
Document the PydanticAI agent design, the workflow state machine, conversation history
management via Upstash Redis, and all guardrails.

**Expected Outcomes:**
- `docs/agent/pydantic_ai_agent.md` — agent design, tool registration, model config
- `docs/agent/workflow_state_machine.md` — state enum, transitions, pre-agent context loader
- `docs/agent/conversation_history.md` — Upstash Redis design, TTL, /new chat behaviour
- `docs/agent/guardrails.md` — all business rule guardrails

**Todo List:**
1. Write `pydantic_ai_agent.md`: agent initialization, how MCP modules register as tools,
   Ollama vs Groq model config, system prompt design (store context injected), tool
   selection per workflow state, control loop (observe→reason→act→feed back)
2. Write `workflow_state_machine.md`: states (UNREGISTERED, PENDING_CATALOGUE,
   PENDING_INVENTORY, ACTIVE), transition triggers, which MCP tools are exposed per state,
   how workflow_id is generated and used to track bill sessions
3. Write `conversation_history.md`: Redis key design (per telegram_user_id),
   message format stored, TTL (e.g. 24h), windowed context loading (last N messages),
   /new chat clears Redis key, preferences NOT in Redis (in Supabase stores table)
4. Write `guardrails.md`: oversell guard, don't-sell-below-cost, idempotency,
   khata-existence check, clarifying question trigger (ambiguous product names),
   partial fulfillment prompt flow

**Relevant Context:**
- PDF §4 all hard parts
- PDF §5 architecture requirements
- User instruction: agent-first, model orchestrates, no regex intent router

---

### Sub-Task 16 — Events Docs
**Status:** [ ] pending

**Intent:**
Document the event system — primarily the reorder alert and session lifecycle events.
Phase 1 uses in-process events; design supports future async event bus (Phase 2).

**Expected Outcomes:**
- `docs/events/reorder_alert.md` — when triggered, what it does, future extensibility
- `docs/events/session_events.md` — session start, /new chat, draft bill expiry events

**Todo List:**
1. Write `reorder_alert.md`: trigger condition (stock <= reorder_level after decrement),
   Phase 1 behavior (bot sends Telegram message to owner: "⚠️ [Product] is below reorder
   level. Current stock: X. Reorder level: Y."), event payload structure,
   Phase 2 extensibility (async queue, supplier integration)
2. Write `session_events.md`: SESSION_START (new message, load Redis history),
   NEW_CHAT (clear Redis key, keep Supabase data), DRAFT_BILL_EXPIRED (draft older
   than TTL auto-cancelled), workflow state transition events

**Relevant Context:**
- User instruction: reorder alert when stock drops below threshold set during catalogue add
- PDF §3: "what's running out?" — low-stock query (separate from reorder alert)

---

### Sub-Task 17 — Infrastructure Docs
**Status:** [ ] pending

**Intent:**
Document the three infrastructure components: Lambda, Supabase, Upstash Redis.

**Expected Outcomes:**
- `docs/infrastructure/lambda.md` — Lambda function design, Function URL config,
  Telegram webhook setup, cold start mitigation, environment variables
- `docs/infrastructure/supabase.md` — Supabase project setup, RLS policies,
  ACID guarantees, connection pooling (pgBouncer), migrations strategy
- `docs/infrastructure/upstash_redis.md` — Upstash setup, key design, TTL config,
  HTTP client usage in Lambda

**Todo List:**
1. Write `lambda.md`: runtime (Python 3.12), handler structure, Function URL setup,
   Telegram webhook registration, environment variables list, Lambda layers for
   dependencies, memory/timeout config, cold start notes
2. Write `supabase.md`: project setup, database schema deployment via migrations,
   RLS (Row Level Security) policy design per table, pgBouncer connection pooling
   for Lambda (transaction mode), ACID transaction usage for finalize_bill + decrement_stock
3. Write `upstash_redis.md`: free tier setup, REST API usage, key naming convention
   (`conv:{telegram_user_id}`), TTL strategy, data format (JSON list of messages),
   windowed context loading

**Relevant Context:**
- Decision: Lambda Function URL (no API Gateway)
- Decision: Supabase for all persistent data
- Decision: Upstash Redis for conversation history

---

### Sub-Task 18 — Implementation Guide Docs
**Status:** [ ] pending

**Intent:**
Write ordered implementation guide files that a developer follows step-by-step to
build the entire system from scratch. Each file is a numbered, actionable build guide.

**Expected Outcomes:**
- `docs/implementation/1_build_schema.md` — Supabase schema SQL, migration order
- `docs/implementation/2_build_mcp_modules.md` — Python module structure, tool patterns
- `docs/implementation/3_build_agent.md` — PydanticAI agent setup, Ollama/Groq config
- `docs/implementation/4_build_telegram_handler.md` — Lambda handler, webhook, routing
- `docs/implementation/5_build_lambda_deployer.md` — packaging, deploy scripts
- `docs/implementation/6_build_pdf_invoice.md` — PDF generation interface + impl guide
- `docs/implementation/7_build_pptx_analytics.md` — PPTX generation with python-pptx
- `docs/implementation/8_testing_and_validation.md` — test scenarios, validation checklist

**Todo List:**
1. Write each implementation file with: purpose, prerequisites, step-by-step instructions,
   code structure guidance (no full code, just structure + patterns), validation criteria
2. Ensure ordering is correct: schema first → MCP modules → agent → telegram handler →
   deployment → document generation → testing
3. Include environment setup, dependency list, and secrets management in file 5
4. Include all PDF §4 hard-part validation scenarios in file 8

**Relevant Context:**
- All previous sub-tasks feed into these implementation guides
- PDF §4: hard parts to validate in testing
- PDF §6: deliverables checklist

---

## Context for Implementation

After plan approval and doc generation is complete, switch to Agent mode to implement.
Each sub-task above maps to one `start_subtask` call in Agent mode. Sub-tasks 1–18 must
be processed in order as each builds on the previous.
