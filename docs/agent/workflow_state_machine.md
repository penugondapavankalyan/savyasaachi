# Workflow State Machine

**File:** `src/agent/workflow_state_machine.py`

---

## Overview

The workflow state machine determines the **onboarding progress** of a store owner and controls which MCP tools are available to the PydanticAI agent on each Lambda invocation. It is read from Supabase before the agent is called — the pre-agent context loader.

This is **not** an intent router. The model still reasons freely within the available tools. The state machine only controls which tools are in scope.

---

## States

| State | Description | Condition to Enter |
|---|---|---|
| `UNREGISTERED` | New user, no account | Default on first contact |
| `PENDING_CATALOGUE` | Registered, no products yet | Registration `COMPLETE` |
| `PENDING_INVENTORY` | Has products, no stock added | At least 1 product in catalogue |
| `ACTIVE` | Fully operational | At least 1 stock-in done |

---

## State Transition Diagram

```
                     First message
                          │
                          ▼
                   ┌──────────────┐
                   │ UNREGISTERED │
                   │              │
                   │ Tools:       │
                   │ • identity   │
                   └──────┬───────┘
                          │
                          │ register_user() + create_store() succeed
                          │ (registration.status = COMPLETE)
                          ▼
                   ┌────────────────────┐
                   │ PENDING_CATALOGUE  │
                   │                    │
                   │ Tools:             │
                   │ • identity         │
                   │ • catalogue        │
                   └──────────┬─────────┘
                              │
                              │ add_product() called at least once
                              │ (products count for store >= 1)
                              ▼
                   ┌────────────────────┐
                   │ PENDING_INVENTORY  │
                   │                    │
                   │ Tools:             │
                   │ • identity         │
                   │ • catalogue        │
                   │ • inventory        │
                   └──────────┬─────────┘
                              │
                              │ receive_stock() called at least once
                              │ (stock_movements count for store >= 1)
                              ▼
                   ┌────────────────────┐
                   │      ACTIVE        │
                   │                    │
                   │ Tools: all 7 MCPs  │
                   │                    │
                   │ (terminal state    │
                   │  in Phase 1)       │
                   └────────────────────┘
```

---

## Transition Triggers

### UNREGISTERED → PENDING_CATALOGUE

**Trigger:** `identity_mcp.create_store()` completes successfully.

**Side effects:**
```python
# In identity_mcp.create_store()
UPDATE workflow_state
SET current_state = 'PENDING_CATALOGUE',
    user_id = ?,
    store_id = ?,
    updated_at = NOW()
WHERE telegram_user_id = ?;

UPDATE registrations
SET status = 'COMPLETE', completed_at = NOW()
WHERE telegram_user_id = ?;
```

**Agent prompt on next message:** "Your store [name] is set up! Let's add your first product to the catalogue. What would you like to sell? (e.g., 'add Aashirvaad Atta 5kg')"

---

### PENDING_CATALOGUE → PENDING_INVENTORY

**Trigger:** `catalogue_mcp.add_product()` is called and it is the first product (product count for store goes from 0 to 1).

**Side effects:**
```python
# In catalogue_mcp.add_product(), after successful insert
product_count = await count_active_products(store_id)
if product_count == 1:
    await identity_mcp.advance_workflow_state(telegram_user_id, 'PENDING_INVENTORY')
```

**DB operation:**
```sql
UPDATE workflow_state
SET current_state = 'PENDING_INVENTORY', updated_at = NOW()
WHERE telegram_user_id = ?
  AND current_state = 'PENDING_CATALOGUE';
```

**Agent prompt on next message:** "Great, [product] is in your catalogue! Now let's add your initial stock. How many [units] of [product] do you have on hand? (e.g., '50 packets of Maggi came in')"

---

### PENDING_INVENTORY → ACTIVE

**Trigger:** `inventory_mcp.receive_stock()` is called and it is the first stock-in for this store.

**Side effects:**
```python
# In inventory_mcp.receive_stock(), after successful upsert
stock_in_count = await count_stock_ins_for_store(store_id)
if stock_in_count == 1:
    await identity_mcp.advance_workflow_state(telegram_user_id, 'ACTIVE')
```

**DB operation:**
```sql
UPDATE workflow_state
SET current_state = 'ACTIVE', updated_at = NOW()
WHERE telegram_user_id = ?
  AND current_state = 'PENDING_INVENTORY';
```

**Agent prompt on next message:** "🎉 Your store is ready! You can now:
- Cut bills ("make a bill: 2kg sugar, 1 Aashirvaad atta, UPI")
- Manage inventory ("50 more Maggi came in")
- Track credit ("put ₹500 on Ramesh's tab")
- View daily sales ("today's sales?")
What would you like to do?"

---

## workflow_id: Bill Session Identifier

The `workflow_id` is a separate concept from the workflow state — it is a **per-bill session identifier**, not an onboarding state.

### How workflow_id Is Generated

```python
# In billing_mcp.create_draft_bill()
import uuid

# Check for existing open draft first
existing = await get_open_draft_for_user(telegram_user_id)
if existing:
    return existing  # Reuse existing draft's workflow_id

# Create new draft with new workflow_id
workflow_id = str(uuid.uuid4())
draft = await create_new_draft(store_id, telegram_user_id, workflow_id)
await identity_mcp.set_active_draft_bill(telegram_user_id, draft.id)
```

### How workflow_id Persists Through Time Gaps

```
9:00 AM — user: "2kg sugar"
  → workflow_state.active_draft_bill_id = None
  → create_draft_bill() → new draft, id=draft-001, workflow_id=abc-123
  → workflow_state.active_draft_bill_id = draft-001
  → [stored in Supabase]

9:10 AM — user: "also 4 Maggi"
  → Lambda invoked fresh (stateless)
  → reads workflow_state.active_draft_bill_id = draft-001 from Supabase
  → add_item_to_draft(draft-001, maggi, 4)
  → same bill, same workflow_id=abc-123

9:15 AM — user: "UPI done"
  → finalize_bill(draft-001, payment_mode='UPI')
  → bills record created with workflow_id=abc-123 (idempotency key)
  → workflow_state.active_draft_bill_id = None
```

### workflow_id and Idempotency

If Telegram redelivers the "done" message:
```
Retry: finalize_bill(draft-001, ...)
  → draft_bills.status = 'CONFIRMED' (already)
  → OR: bills table has workflow_id=abc-123 (already)
  → Return existing bill: FinalizedBillResult(already_finalized=True)
  → No double-decrement, no double-bill
```

---

## Pre-Agent Context Loader (Lambda Handler)

This runs on every Lambda invocation, before the agent:

```python
async def load_context(telegram_user_id: int) -> AgentContext:
    # 1. Upsert workflow_state for new users
    await supabase.rpc('upsert_workflow_state', {'p_telegram_user_id': telegram_user_id})
    
    # 2. Read current workflow state
    workflow = await identity_mcp.get_workflow_state(telegram_user_id)
    
    # 3. Load store context (if registered)
    store = None
    if workflow.store_id:
        store = await identity_mcp.get_store(telegram_user_id)
    
    # 4. Load conversation history from Upstash Redis
    history = await upstash_client.get_conversation(telegram_user_id, max_messages=20)
    
    # 5. Select tool subset for current state
    tools = get_tools_for_state(workflow.current_state, mcp_instances)
    
    # 6. Build system prompt with store context
    system_prompt = build_system_prompt(store, workflow)
    
    return AgentContext(
        workflow_state=workflow.current_state,
        store=store,
        conversation_history=history,
        tools=tools,
        system_prompt=system_prompt,
        active_draft_bill_id=workflow.active_draft_bill_id
    )
```

---

## State Guards

### Attempting billing before ACTIVE
```
User (PENDING_INVENTORY): "make a bill: 2 Maggi"
→ Billing tools not in tool list
→ LLM only has identity + catalogue + inventory tools
→ LLM responds: "You need to add stock to your inventory before billing.
   How many Maggi do you have? Let's add the stock first."
(The model naturally guides the user — no hardcoded check needed)
```

### Attempting catalogue add before PENDING_CATALOGUE
```
User (UNREGISTERED): "add Maggi to catalogue"
→ Only identity tools available
→ LLM responds: "Let's set up your store first! What's your shop name?"
```

---

## Phase 2 State Extensions

| New State | When | What Unlocks |
|---|---|---|
| `STORE_SELECTION` | Phase 2 multi-store | User picks which store to operate |
| `SUSPENDED` | Phase 2 subscription | Store suspended for payment |
| `PENDING_KYC` | Phase 2 compliance | KYC verification required |

Adding new states requires:
1. Adding to `user_workflow_state` enum in Supabase migration
2. Adding tool subset in `get_tools_for_state()`
3. Adding transition trigger in the relevant MCP
