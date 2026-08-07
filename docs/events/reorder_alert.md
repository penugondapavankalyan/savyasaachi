# Event: Reorder Alert

**Module:** Triggered by `src/mcp/inventory/inventory_mcp.py`  
**Type:** In-process (Phase 1) / Async queue (Phase 2)

---

## Overview

A reorder alert fires whenever a product's inventory quantity drops to or below its reorder level after a stock decrement from a finalized bill. The alert notifies the store owner via Telegram so they can place a restock order.

In Phase 1, the alert is triggered **in-process** — synchronously after the `decrement_stock` RPC returns. The system is designed so this can be replaced with an async event queue (SNS/SQS, EventBridge) in Phase 2 without changing the decrement logic.

---

## Trigger Condition

```python
# In inventory_mcp.decrement_stock()
result = await supabase.rpc('decrement_stock', {
    'p_store_id': store_id,
    'p_product_id': product_id,
    'p_quantity': quantity,
    'p_bill_id': bill_id
}).execute()

new_quantity = result.data['new_quantity']
reorder_alert = result.data['reorder_alert']  # True if new_quantity <= reorder_level

if reorder_alert:
    await fire_reorder_alert(store_id, product_id, new_quantity)
```

**Condition:** `new_quantity <= reorder_level` (not strictly less than — triggers at exactly the reorder level too).

---

## Alert Payload

```python
class ReorderAlertPayload:
    store_id: str
    product_id: str
    product_name: str
    brand: Optional[str]
    unit: str
    current_quantity: float
    reorder_level: float
    triggered_by_bill_id: str
    triggered_at: str  # ISO-8601 timestamp
```

---

## Phase 1: In-Process Alert (Telegram Message)

```python
async def fire_reorder_alert(store_id: str, product_id: str, new_quantity: float):
    # Fetch product details
    product = await catalogue_mcp.get_product(store_id, product_id)
    inventory = await get_stock(store_id, product_id)
    
    # Fetch store owner's Telegram ID
    store = await identity_mcp.get_store_by_id(store_id)
    owner_user = await identity_mcp.get_user_by_store(store_id)
    
    # Compose message
    urgency = "🔴 OUT OF STOCK" if new_quantity == 0 else "⚠️ LOW STOCK ALERT"
    
    message = (
        f"{urgency}\n\n"
        f"*{product.name}*"
        f"{' (' + product.brand + ')' if product.brand else ''}\n"
        f"Current stock: *{new_quantity} {product.unit}*\n"
        f"Reorder level: {inventory.reorder_level} {product.unit}\n\n"
        f"Time to restock!"
    )
    
    await telegram_client.send_message(
        chat_id=owner_user.telegram_user_id,
        text=message,
        parse_mode='Markdown'
    )
```

**Example Telegram message sent to owner:**
```
⚠️ LOW STOCK ALERT

*Maggi 70g* (Nestle)
Current stock: *3 PACKET*
Reorder level: 20 PACKET

Time to restock!
```

---

## Behavior Check: Preference Gate

Before firing the alert, check if the owner has disabled low stock alerts:

```python
store = await identity_mcp.get_store_by_id(store_id)
if not store.preferences.get('low_stock_alert_enabled', True):
    return  # Owner has disabled alerts
```

---

## Separation from "What's Running Out?" Query

The reorder alert fires **automatically** after a sale. It is distinct from:
- `inventory_mcp.get_low_stock_items()` — a **manual query** by the owner ("what's running out?")

Both use the same threshold (`reorder_level`), but:
- Alert: automatic, push notification, fires once after each decrement that crosses threshold
- Query: on-demand, pull query, shows all items currently at or below threshold

---

## Phase 2: Async Event Queue Design

In Phase 2, the in-process call is replaced with an event publish:

```python
# Phase 1 (current)
if reorder_alert:
    await fire_reorder_alert(store_id, product_id, new_quantity)

# Phase 2 (async queue)
if reorder_alert:
    await event_bus.publish('reorder.alert', ReorderAlertPayload(
        store_id=store_id,
        product_id=product_id,
        current_quantity=new_quantity,
        triggered_by_bill_id=bill_id,
        triggered_at=now_iso()
    ))
    # A separate Lambda subscriber handles the Telegram notification
    # Additional subscribers can add: supplier email, dashboard notification, etc.
```

**The payload structure is identical** — the Phase 2 migration only changes how the payload is dispatched, not what it contains.

---

## Future Subscribers (Phase 2)

| Subscriber | Action |
|---|---|
| Telegram notifier | Sends message to owner (current Phase 1 behavior) |
| Supplier emailer | Sends reorder email to supplier if configured |
| Dashboard notifier | Pushes alert to a future web dashboard |
| Reorder suggestions | Uses sales velocity from `stock_movements` to suggest order quantity |
