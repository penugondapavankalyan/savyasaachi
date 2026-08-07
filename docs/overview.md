# Kirana Store Agent — Project Overview

## What Is This?

The Kirana Store Agent is a conversational AI agent that runs an entire Indian kirana (grocery) store from a Telegram chat window. There is no web app, no admin panel, no forms. The chat is the product.

The store owner operates the shop entirely through plain language messages on Telegram — receiving stock, cutting bills, checking inventory, managing customer credit (khata), closing the day, and generating GST-correct PDF invoices and business analysis decks on demand.

This is **not** a CRUD app with a chatbot in front of it. It is a PydanticAI-powered agent that reasons over messy, terse, real-shopkeeper phrasing and keeps the store's books consistent.

---

## Tech Stack

| Component | Technology |
|---|---|
| **Interface** | Telegram Bot API (webhook) |
| **Agent Harness** | PydanticAI |
| **LLM (dev)** | Ollama (local) |
| **LLM (prod)** | Groq |
| **Database** | Supabase (PostgreSQL, ACID) |
| **Conversation History** | Upstash Redis (serverless, TTL-based) |
| **Deployment** | AWS Lambda (Python 3.12) + Lambda Function URL |
| **MCP Modules** | Python modules co-deployed in same Lambda |
| **PDF Generation** | TBD library (interface abstracted) |
| **PPTX Generation** | python-pptx |
| **Generated File Delivery** | Lambda /tmp → streamed directly to Telegram |
| **Currency** | ₹ INR |
| **GST Regime** | Intra-state only (CGST + SGST split) in Phase 1 |

---

## Phase 1 Scope

Phase 1 delivers a fully functional single-store, single-owner kirana agent.

### Constraints
- **One store per Telegram user** — each Telegram account can register and manage exactly one store
- **One owner per store** — only the registering Telegram user can manage the store
- **Owner-only access** — no cashier, worker, or staff roles in Phase 1
- **Intra-state GST only** — CGST + SGST split; no IGST in Phase 1

### User Journey (Phase 1)
```
User opens Telegram chat
        ↓
Agent checks if user is registered
        ↓ (not registered)
Registration flow — collect shop name, GSTIN, address
        ↓ (registered)
Prompt to add items to catalogue
        ↓ (at least 1 product in catalogue)
Prompt to add stock to inventory
        ↓ (stock added)
ACTIVE state — full store operations available:
  • Cut bills
  • Manage inventory
  • Manage khata (credit ledger)
  • View daily/weekly analytics
  • Generate PDF invoices
  • Generate PPTX analysis decks
  • Set preferences (default payment mode, preferred brands)
```

### What the Owner Can Do (Phase 1)

| Capability | Example |
|---|---|
| Receive stock | "50 packets of Maggi came in, cost ₹12, MRP ₹14" |
| Add product to catalogue | "new item: Amul Butter 100g, GST 12%, MRP ₹62" |
| Cut a bill | "make a bill: 2kg sugar, 1 Aashirvaad atta 5kg, 4 Maggi, UPI" |
| Edit a bill mid-build | "drop the butter, make it 6 Maggi" |
| Stock query | "how much sugar is left?" |
| Low-stock / reorder check | "what's running out?" |
| Credit (khata) | "put ₹500 on Ramesh's credit" / "Ramesh paid ₹300" / "Ramesh's balance?" |
| Daily close | "today's sales?" / "close the day" |
| Generate PDF invoice | "send me that bill as a PDF" |
| Generate analysis deck | "make this week's sales analysis deck" |
| Set preferences | "always assume UPI unless I say cash" / "default atta = Aashirvaad 5kg" |

---

## Key Business Rules (Phase 1)

### GST
- Loose items (sugar, rice, dal by the kg) → always **0% GST**
- Packaged staples → **5% GST**
- FMCG items (chocolates, soaps, etc.) → **12–18% GST**
- All GST is intra-state: split equally into **CGST** and **SGST**
- Every bill shows a complete per-item GST breakup
- Each branded product carries an **HSN code**

### Inventory
- Every SKU has cost price, MRP/sell price, quantity, and a reorder level
- Selling decrements stock **atomically** — stock cannot go negative
- When stock falls to or below reorder level → owner receives a Telegram alert
- **Partial fulfillment**: if requested quantity exceeds stock, agent asks if partial is acceptable

### Billing
- Bills are built **across multiple messages** (multi-turn) using a `workflow_id`
- Items are only confirmed and stock decremented when the owner **finalizes** the bill
- Telegram may redeliver updates — retried finalize must **not** double-bill or double-decrement
- Cannot sell below cost price

### Khata (Credit Ledger)
- Append-only ledger — entries are never modified or deleted
- Positive entry = customer owes the shop
- Negative entry = shop owes the customer (overpayment)
- Balance = SUM of all entries for that customer
- Cannot settle a khata that does not exist

### Memory & Preferences
- **Conversation history** is stored in Upstash Redis with a TTL; cleared on `/new` chat
- **Preferences** (default payment mode, preferred brands, GSTIN, shop name) are stored permanently in Supabase — they survive a `/new` chat and Lambda restarts

---

## Phase 2 Roadmap (Not in Scope for Phase 1)

The Phase 1 architecture is intentionally designed to support these extensions without breaking changes:

| Feature | Phase 2 Design Note |
|---|---|
| **Multiple stores per user** | `users` ↔ `stores` relation is 1:N (unique constraint lifted) |
| **Multiple users per store** | `store_users` join table with roles (owner, cashier, worker) |
| **IGST (inter-state)** | `gst_type` column on `bills`, already-modeled state code on stores |
| **MCP extraction to separate Lambdas** | Each MCP module has a clean interface contract |
| **Async reorder notifications** | Event payload structure designed; swap in-process call for queue |
| **Batch/expiry tracking (FEFO)** | `batch_id` and `expiry_date` columns reserved in inventory |
| **Payment reminders** | `phone` field on customers already collected |
| **Barcode/photo product ID** | Documents MCP interface supports file input |
| **Multi-language** | System prompt and agent config abstracted |
| **Scheduled analysis decks** | Analytics MCP data contract already defined |
| **Reorder suggestions from velocity** | `stock_movements` table provides full history |

---

## Section 7 Stretch Goals

The following stretch goals from the original brief are **excluded from Phase 1** but the system architecture supports adding them:

- Branded/templated invoice PDFs
- Scheduled weekly analysis deck (auto-sent)
- Reorder suggestions from sales velocity
- Expiry/batch tracking with FEFO
- Voice-note orders (transcribe → bill)
- Multi-language support (Hindi/Tamil)
- Barcode/product photo → identify item
- Khata payment reminders

---

## Repository Structure (Target)

```
savysaachi/
├── docs/                          ← All specification docs (this folder)
│   ├── overview.md                ← This file
│   ├── architecture.md
│   ├── database/
│   ├── mcp/
│   ├── agent/
│   ├── events/
│   ├── infrastructure/
│   └── implementation/
├── src/
│   ├── handler.py                 ← Lambda entry point
│   ├── agent/
│   │   └── kirana_agent.py        ← PydanticAI agent
│   ├── mcp/
│   │   ├── identity/
│   │   ├── catalogue/
│   │   ├── inventory/
│   │   ├── billing/
│   │   ├── khata/
│   │   ├── analytics/
│   │   └── documents/
│   ├── db/
│   │   └── supabase_client.py
│   ├── redis/
│   │   └── upstash_client.py
│   └── telegram/
│       └── telegram_client.py
├── migrations/                    ← Supabase SQL migrations
├── requirements.txt
└── README.md
```
