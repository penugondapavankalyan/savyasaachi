"""
Reorder alert helper.

Called by the billing flow after decrement_stock returns reorder_alert=True.
Sends a proactive Telegram notification to the store owner.
"""

from __future__ import annotations

import os

from src.mcp import get_mcp_instances


async def send_reorder_alert(
    store_id: str,
    product_id: str,
    current_quantity: float,
    telegram_user_id: int,
) -> None:
    """
    Send a low-stock / out-of-stock alert to the store owner via Telegram.
    Imported lazily to avoid circular imports.
    """
    from src.telegram.telegram_client import get_telegram_client

    try:
        mcps = get_mcp_instances()
        stock = await mcps.inventory.get_stock(store_id, product_id)

        if current_quantity == 0:
            icon = "🔴"
            status = "OUT OF STOCK"
        elif current_quantity <= stock.reorder_level * 0.5:
            icon = "🟠"
            status = "CRITICALLY LOW"
        else:
            icon = "⚠️"
            status = "LOW STOCK"

        message = (
            f"{icon} *{status} ALERT*\n\n"
            f"*{stock.product_name}*"
            + (f" ({stock.brand})" if stock.brand else "")
            + "\n"
            f"Current stock: {current_quantity} {stock.unit.lower()}\n"
            f"Reorder level: {stock.reorder_level} {stock.unit.lower()}\n\n"
            "Time to restock! 🛒"
        )
        telegram = get_telegram_client()
        await telegram.send_message(telegram_user_id, message)

    except Exception:
        # Alert failure must never interrupt the billing flow
        pass
