# Events: Session Lifecycle

**Module:** `src/handler.py`, `src/redis/upstash_client.py`

---

## Overview

Session lifecycle events are the discrete state changes in a user's interaction session with the Kirana Agent. Unlike the reorder alert (domain event), session events are infrastructure-level — they manage conversation context, draft bill expiry, and the /new chat reset.

In Phase 1, all session events are handled synchronously in the Lambda handler or MCP modules. The event structures are documented here for Phase 2 async migration.

---

## Event: SESSION_START

**Fired:** At the beginning of every Lambda invocation (every incoming Telegram message).

**Handler:** `handler.py` pre-agent context loader.

**What happens:**
```python
async def on_session_start(telegram_user_id: int) -> SessionContext:
    # 1. Upsert workflow_state (creates UNREGISTERED record for new users)
    await supabase.rpc('upsert_workflow_state', {'p_telegram_user_id': telegram_user_id})
    
    # 2. Read workflow state
    workflow = await identity_mcp.get_workflow_state(telegram_user_id)
    
    # 3. Load conversation history
    history = await upstash_client.get_conversation(telegram_user_id)
    
    # 4. Check draft bill expiry
    if workflow.active_draft_bill_id:
        await check_draft_expiry(workflow.active_draft_bill_id, telegram_user_id)
    
    return SessionContext(workflow=workflow, history=history)
```

---

## Event: NEW_CHAT

**Fired:** When the user sends `/new` command.

**Handler:** `handler.py` — handled before agent invocation, agent is NOT called.

**What happens:**
```python
async def on_new_chat(telegram_user_id: int) -> str:
    # 1. Clear conversation history in Redis
    await upstash_client.clear_conversation(telegram_user_id)
    
    # 2. Cancel any open draft bill
    workflow = await identity_mcp.get_workflow_state(telegram_user_id)
    if workflow.active_draft_bill_id:
        await billing_mcp.cancel_draft_bill(workflow.active_draft_bill_id)
        # This also sets workflow_state.active_draft_bill_id = None
    
    # 3. Return confirmation message (agent NOT invoked)
    return (
        "🆕 Chat cleared!\n\n"
        "Your store data, products, inventory, bills, and preferences are all intact.\n"
        "What would you like to do?"
    )
```

**What is cleared:**
| Data | Cleared? |
|---|---|
| Redis conversation history | ✅ Yes |
| Active draft bill | ✅ Yes (cancelled) |
| Workflow state (ACTIVE etc.) | ❌ No |
| Products catalogue | ❌ No |
| Inventory | ❌ No |
| Bills history | ❌ No |
| Khata entries | ❌ No |
| Store preferences | ❌ No |

---

## Event: DRAFT_BILL_EXPIRED

**Fired:** When an open draft bill's `expires_at` timestamp is in the past.

**Detection:** On `SESSION_START`, the handler checks `draft_bills.expires_at`:

```python
async def check_draft_expiry(draft_bill_id: str, telegram_user_id: int):
    draft = await billing_mcp.get_draft_bill(draft_bill_id)
    
    if draft.status == 'OPEN' and draft.expires_at < datetime.utcnow():
        # Mark expired
        await supabase.table('draft_bills').update(
            {'status': 'EXPIRED'}
        ).eq('id', draft_bill_id).execute()
        
        # Clear from workflow_state
        await identity_mcp.set_active_draft_bill(telegram_user_id, None)
        
        # Notify user on next message
        return DraftExpiredNotification(
            bill_id=draft_bill_id,
            expired_at=draft.expires_at,
            item_count=len(draft.items)
        )
```

**User notification (included in next agent response):**
```
⏰ Your previous bill (3 items, started 4+ hours ago) has expired.
Starting fresh — what would you like to do?
```

**TTL:** 4 hours from draft creation. This is configurable via environment variable `DRAFT_BILL_TTL_HOURS`.

---

## Event: WORKFLOW_STATE_TRANSITION

**Fired:** When workflow state changes (UNREGISTERED→PENDING_CATALOGUE, etc.).

**Handler:** Identity MCP `advance_workflow_state()`.

**Payload structure (for future async dispatch):**
```python
class WorkflowTransitionEvent:
    telegram_user_id: int
    store_id: Optional[str]
    from_state: str
    to_state: str
    transitioned_at: str  # ISO-8601
    triggered_by: str  # 'create_store' | 'add_product' | 'receive_stock'
```

**Phase 1:** The transition is synchronous DB write only. No external notification.

**Phase 2 subscriber ideas:**
- Welcome message sequence (send "now let's add products" on PENDING_CATALOGUE)
- Analytics (track onboarding funnel completion rates)
- CRM integration

---

## Event: REORDER_ALERT

See [`docs/events/reorder_alert.md`](./reorder_alert.md) for full documentation.

---

## Event Summary Table

| Event | When | Handler (Phase 1) | Phase 2 Async? |
|---|---|---|---|
| `SESSION_START` | Every message | Lambda handler sync | No (per-request) |
| `NEW_CHAT` | `/new` command | Lambda handler sync | No (per-request) |
| `DRAFT_BILL_EXPIRED` | Open draft > 4h old | Lambda handler sync | Optional |
| `WORKFLOW_TRANSITION` | State machine advance | Identity MCP sync | Yes — onboarding flow |
| `REORDER_ALERT` | Stock <= reorder_level | Inventory MCP sync | Yes — supplier/dashboard |
