"""
Tool registry — returns the appropriate tool list for each workflow state.

ARCHITECTURE: Context-bound wrappers
=====================================
ALL tools that touch the database require store_id and/or telegram_user_id.
These IDs must NEVER be passed by the LLM — the LLM cannot be trusted to
use the correct values (it invents random IDs). Instead, we build
context-bound wrapper functions on every request: each wrapper closes over
the real telegram_user_id and store_id from StoreContext, so the LLM
only passes domain-relevant arguments (name, price, quantity, etc.).

Token budget strategy:
- UNREGISTERED: 2 tools   (store setup only — user record already exists server-side)
- PENDING_CATALOGUE: 3 tools (catalogue setup only)
- PENDING_INVENTORY: 4 tools (stock setup only)
- ACTIVE: split into intent groups, max 12 tools per request
  The agent picks the right group based on the user's message intent.
  Default ACTIVE group is BILLING (most common daily use).
"""

from __future__ import annotations

import re as _re
from typing import Callable

from src.agent.config import StoreContext
from src.mcp import MCPInstances


def get_tools_for_state(
    workflow_state: str,
    mcps: MCPInstances,
    context: StoreContext,
    intent: str = "BILLING",
) -> list[Callable]:
    """
    Return context-bound tool functions for the given workflow state.

    Every returned callable is a closure that has already captured
    telegram_user_id and store_id from StoreContext. The LLM NEVER
    sees or passes these IDs — they are baked into the function.

    For ACTIVE state, pass intent to select the right sub-group:
      BILLING   — create/add/finalize bills (default, daily use)
      INVENTORY — stock management and reorder
      KHATA     — credit ledger and customer balances
      ANALYTICS — sales reports, GST summaries, documents
      CATALOGUE — add/update/search products
    """
    tuid = context.telegram_user_id
    store_id = context.store_id or ""

    if workflow_state == "UNREGISTERED":
        return _build_unregistered_tools(mcps, tuid)

    if workflow_state == "PENDING_CATALOGUE":
        return _build_pending_catalogue_tools(mcps, tuid, store_id)

    if workflow_state == "PENDING_INVENTORY":
        return _build_pending_inventory_tools(mcps, tuid, store_id)

    # ── ACTIVE — intent-based sub-groups ────────────────────────────────
    return _build_active_tools(mcps, tuid, store_id, intent, context)


# ─────────────────────────────────────────────────────────────────────────────
# UNREGISTERED — only store creation tools
# The user record is already created server-side (run_local.py / handler.py).
# The LLM's job is only to collect shop_name and call create_store.
# telegram_user_id is NEVER a parameter the LLM sees.
# ─────────────────────────────────────────────────────────────────────────────

def _build_unregistered_tools(mcps: MCPInstances, tuid: int) -> list[Callable]:
    """
    Returns two tools for the UNREGISTERED state.
    Both closures capture tuid — the LLM never passes telegram_user_id.
    """

    async def setup_store(
        shop_name: str,
        phone: str,
        state_code: str,
        gstin: str | None = None,
        address: str | None = None,
        default_payment_mode: str = "CASH",
    ) -> str:
        """
        Create the shop for the owner. Call this once all store details have been collected.
        - shop_name: name of the shop (required)
        - phone: shop phone number — MANDATORY, must be a valid number
        - state_code: 2-digit Indian GST state code (required, e.g. '29' for Karnataka, '27' for Maharashtra)
        - gstin: 15-character GST registration number (optional — pass None if owner said 'skip')
        - address: shop address (optional — pass None if owner said 'skip')
        - default_payment_mode: CASH / UPI / CREDIT (default CASH)
        Returns confirmation with store details.
        """
        from src.utils.guardrails import clean_phone
        # Phone is mandatory — validate before calling create_store
        cleaned = clean_phone(phone)
        if not cleaned:
            return "Phone number is invalid. Please provide a valid phone number for the shop."

        from src.mcp.identity.models import CreateStoreResult
        result: CreateStoreResult = await mcps.identity.create_store(
            telegram_user_id=tuid,
            shop_name=shop_name,
            gstin=gstin,
            address=address,
            phone=cleaned,
            state_code=state_code,
        )
        # Also set default_payment_mode if not CASH
        if default_payment_mode and default_payment_mode.upper() != "CASH" and result.store_id:
            try:
                await mcps.identity.update_store_preferences(
                    store_id=result.store_id,
                    preference_key="default_payment_mode",
                    preference_value=default_payment_mode.upper(),
                )
            except Exception:
                pass
        return result.message

    async def save_owner_name(first_name: str, last_name: str | None = None) -> str:
        """
        Save the owner's name. Call this once the owner tells you their name.
        first_name is required. last_name is optional.
        Returns a confirmation message.
        """
        from src.mcp.identity.models import RegisterUserResult
        result: RegisterUserResult = await mcps.identity.register_user(
            telegram_user_id=tuid,
            first_name=first_name,
            last_name=last_name,
        )
        return f"Got it! Welcome, {first_name}. Now, what is the name of your shop?"

    return [save_owner_name, setup_store]


# ─────────────────────────────────────────────────────────────────────────────
# PENDING_CATALOGUE — only catalogue tools, store_id baked in
# ─────────────────────────────────────────────────────────────────────────────

def _build_pending_catalogue_tools(
    mcps: MCPInstances, tuid: int, store_id: str
) -> list[Callable]:

    async def add_product(
        name: str,
        is_loose: bool,
        unit: str,
        cost_price: float,
        mrp: float,
        reorder_level: float,
        gst_rate: float,
        brand: str | None = None,
        hsn_code: str | None = None,
    ) -> str:
        """
        Add a confirmed product to the catalogue. Only call this AFTER the owner
        has confirmed the product details (shown summary and said yes).
        - name: product name (e.g. 'Tata Salt', 'Sugar')
        - is_loose: True if sold loose by weight/volume, False if branded/packaged
        - unit: one of KG / G / L / ML / PACKET / PIECE / DOZEN / BUNDLE
        - cost_price: price you paid the supplier (rupees)
        - mrp: selling price / MRP (rupees)
        - reorder_level: minimum stock quantity before reorder alert
        - gst_rate: MANDATORY. Loose items → always pass 0. Branded items → MUST be
          one of 5 / 12 / 18 / 28. NEVER pass 0 for branded — ask the owner first.
        - brand: brand name if branded (e.g. 'Tata'), None if loose
        - hsn_code: HSN code if known, None if not
        """
        from src.mcp.catalogue.models import AddProductResult
        result: AddProductResult = await mcps.catalogue.add_product(
            store_id=store_id,
            name=name,
            is_loose=is_loose,
            unit=unit,
            cost_price=cost_price,
            mrp=mrp,
            reorder_level=reorder_level,
            brand=brand,
            hsn_code=hsn_code,
            gst_rate=gst_rate,
            telegram_user_id=tuid,
        )
        return result.message

    async def list_products() -> str:
        """List all products currently in the catalogue with their full product IDs."""
        from src.mcp.catalogue.models import ProductResult
        products: list[ProductResult] = await mcps.catalogue.list_products(
            store_id=store_id
        )
        if not products:
            return "No products in catalogue yet."
        lines = ["Catalogue:"]
        for p in products:
            brand_str = f" ({p.brand})" if p.brand else ""
            lines.append(
                f"  product_id={p.product_id} | {p.name}{brand_str} | {p.unit} | MRP Rs.{p.mrp} | GST {p.gst_rate}%"
            )
        return "\n".join(lines)

    async def search_products(query: str) -> str:
        """Search for a product by name. Returns matching products with their full product IDs."""
        from src.mcp.catalogue.models import ProductResult
        products: list[ProductResult] = await mcps.catalogue.search_products(
            store_id=store_id, query=query
        )
        if not products:
            return f"No products found matching '{query}'."
        lines = [f"Products matching '{query}':"]
        for p in products:
            brand_str = f" ({p.brand})" if p.brand else ""
            lines.append(
                f"  product_id={p.product_id} | {p.name}{brand_str} | {p.unit} | MRP Rs.{p.mrp}"
            )
        return "\n".join(lines)

    async def update_product_details(
        product_id: str,
        name: str | None = None,
        brand: str | None = None,
        unit: str | None = None,
        is_loose: bool | None = None,
        cost_price: float | None = None,
        mrp: float | None = None,
        reorder_level: float | None = None,
        gst_rate: float | None = None,
        hsn_code: str | None = None,
    ) -> str:
        """
        Update any field of an existing catalogue product.
        Use product_id from list_products or search_products (full UUID).
        Only pass fields that should change — others remain untouched.
        Editable: name, brand, unit, is_loose, cost_price, mrp, reorder_level, gst_rate, hsn_code.
        """
        result = await mcps.catalogue.update_product_details(
            store_id=store_id,
            product_id=product_id,
            name=name, brand=brand, unit=unit, is_loose=is_loose,
            cost_price=cost_price, mrp=mrp, reorder_level=reorder_level,
            gst_rate=gst_rate, hsn_code=hsn_code,
        )
        brand_str = f" ({result.brand})" if result.brand else ""
        return (
            f"Updated: {result.name}{brand_str} | {result.unit} | "
            f"Cost Rs.{result.cost_price} | MRP Rs.{result.mrp} | GST {result.gst_rate}%"
        )

    async def update_store(
        shop_name: str | None = None,
        phone: str | None = None,
        address: str | None = None,
        state_code: str | None = None,
        default_payment_mode: str | None = None,
    ) -> str:
        """
        Update store details.
        Editable fields: shop_name, phone, address, state_code, default_payment_mode.
        Only pass fields that should change.
        """
        result = await mcps.identity.update_store(
            store_id=store_id,
            shop_name=shop_name,
            phone=phone,
            address=address,
            state_code=state_code,
            default_payment_mode=default_payment_mode,
        )
        return (
            f"Store updated: {result.shop_name} | Phone: {result.phone or 'not set'} | "
            f"Address: {result.address or 'not set'} | State: {result.state_code} | "
            f"Payment: {result.default_payment_mode}"
        )

    async def update_owner_name(
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> str:
        """
        Update the owner's name on their profile.
        first_name and/or last_name can be changed.
        """
        result = await mcps.identity.update_user(
            telegram_user_id=tuid,
            first_name=first_name,
            last_name=last_name,
        )
        return result.message

    return [add_product, list_products, search_products, update_product_details, update_store, update_owner_name]


# ─────────────────────────────────────────────────────────────────────────────
# PENDING_INVENTORY — catalogue reads + receive_stock, store_id baked in
# ─────────────────────────────────────────────────────────────────────────────

def _build_pending_inventory_tools(
    mcps: MCPInstances, tuid: int, store_id: str
) -> list[Callable]:

    async def list_products() -> str:
        """List all products currently in the catalogue with their full product IDs."""
        from src.mcp.catalogue.models import ProductResult
        products: list[ProductResult] = await mcps.catalogue.list_products(
            store_id=store_id
        )
        if not products:
            return "No products in catalogue yet."
        lines = ["Catalogue (use the full product_id when calling receive_stock):"]
        for p in products:
            brand_str = f" ({p.brand})" if p.brand else ""
            lines.append(
                f"  product_id={p.product_id} | {p.name}{brand_str} | {p.unit} | Reorder at {p.reorder_level}"
            )
        return "\n".join(lines)

    async def search_products(query: str) -> str:
        """Search for a product by name. Returns matching products with their full product IDs."""
        from src.mcp.catalogue.models import ProductResult
        products: list[ProductResult] = await mcps.catalogue.search_products(
            store_id=store_id, query=query
        )
        if not products:
            return f"No products found matching '{query}'."
        lines = [f"Products matching '{query}' (use the full product_id when calling receive_stock):"]
        for p in products:
            brand_str = f" ({p.brand})" if p.brand else ""
            lines.append(
                f"  product_id={p.product_id} | {p.name}{brand_str} | {p.unit}"
            )
        return "\n".join(lines)

    async def receive_stock(product_id: str, quantity: float, notes: str | None = None) -> str:
        """
        Record received stock for a product. Call after finding product_id via list_products or search_products.
        - product_id: the product UUID from list_products / search_products (the [xxxxxxxx] part, full UUID)
        - quantity: how many units received
        - notes: optional note (e.g. supplier name)
        """
        from src.mcp.inventory.models import ReceiveStockResult
        result: ReceiveStockResult = await mcps.inventory.receive_stock(
            store_id=store_id,
            product_id=product_id,
            quantity=quantity,
            notes=notes,
            telegram_user_id=tuid,
        )
        return result.message

    async def add_product(
        name: str,
        is_loose: bool,
        unit: str,
        cost_price: float,
        mrp: float,
        reorder_level: float,
        gst_rate: float,
        brand: str | None = None,
        hsn_code: str | None = None,
    ) -> str:
        """
        Add a new product to catalogue (use only if product is missing from catalogue).
        - gst_rate: MANDATORY. Loose → pass 0. Branded → MUST be 5 / 12 / 18 / 28.
          NEVER pass 0 for branded items — ask the owner for the rate first.
        After adding, ALWAYS ask the owner for quantity and call receive_stock.
        """
        from src.mcp.catalogue.models import AddProductResult
        result: AddProductResult = await mcps.catalogue.add_product(
            store_id=store_id,
            name=name,
            is_loose=is_loose,
            unit=unit,
            cost_price=cost_price,
            mrp=mrp,
            reorder_level=reorder_level,
            brand=brand,
            hsn_code=hsn_code,
            gst_rate=gst_rate,
            telegram_user_id=tuid,
        )
        return result.message + f"\n[product_id={result.product_id}] — Now ask for quantity to receive_stock."

    async def update_product_details(
        product_id: str,
        name: str | None = None,
        brand: str | None = None,
        unit: str | None = None,
        is_loose: bool | None = None,
        cost_price: float | None = None,
        mrp: float | None = None,
        reorder_level: float | None = None,
        gst_rate: float | None = None,
        hsn_code: str | None = None,
    ) -> str:
        """
        Update any field of an existing catalogue product.
        Use product_id from list_products or search_products (full UUID).
        Only pass fields that should change — others remain untouched.
        """
        result = await mcps.catalogue.update_product_details(
            store_id=store_id,
            product_id=product_id,
            name=name, brand=brand, unit=unit, is_loose=is_loose,
            cost_price=cost_price, mrp=mrp, reorder_level=reorder_level,
            gst_rate=gst_rate, hsn_code=hsn_code,
        )
        brand_str = f" ({result.brand})" if result.brand else ""
        return (
            f"Updated: {result.name}{brand_str} | {result.unit} | "
            f"Cost Rs.{result.cost_price} | MRP Rs.{result.mrp} | GST {result.gst_rate}%"
        )

    async def update_store(
        shop_name: str | None = None,
        phone: str | None = None,
        address: str | None = None,
        state_code: str | None = None,
        default_payment_mode: str | None = None,
    ) -> str:
        """Update store details. Only pass fields that should change."""
        result = await mcps.identity.update_store(
            store_id=store_id, shop_name=shop_name, phone=phone,
            address=address, state_code=state_code,
            default_payment_mode=default_payment_mode,
        )
        return (
            f"Store updated: {result.shop_name} | Phone: {result.phone or 'not set'} | "
            f"Address: {result.address or 'not set'} | State: {result.state_code} | "
            f"Payment: {result.default_payment_mode}"
        )

    async def update_owner_name(first_name: str | None = None, last_name: str | None = None) -> str:
        """Update the owner's name on their profile."""
        result = await mcps.identity.update_user(
            telegram_user_id=tuid, first_name=first_name, last_name=last_name
        )
        return result.message

    return [list_products, search_products, receive_stock, add_product,
            update_product_details, update_store, update_owner_name]


# ─────────────────────────────────────────────────────────────────────────────
# ACTIVE — full operational tools, all IDs baked in
# ─────────────────────────────────────────────────────────────────────────────

def _build_active_tools(
    mcps: MCPInstances, tuid: int, store_id: str, intent: str,
    context: "StoreContext | None" = None,
) -> list[Callable]:
    # active_draft_bill_id is shared mutable state across all tool closures in this request.
    # A list is used so that create_draft_bill() can update it in-place and all other
    # closures (add_item_to_draft, finalize_bill, etc.) see the new value immediately.
    # Without this, create_draft_bill() would write to Supabase but the sibling closures
    # would still read None from their captured snapshot, causing "No active draft bill" errors.
    _draft_id_cell: list[str | None] = [context.active_draft_bill_id if context else None]

    # ── Shared lookup tools (all intents) ───────────────────────────────

    async def search_products(query: str) -> str:
        """Search for a product by name. Returns full product_id and details."""
        from src.mcp.catalogue.models import ProductResult
        products: list[ProductResult] = await mcps.catalogue.search_products(
            store_id=store_id, query=query
        )
        if not products:
            return f"No products found matching '{query}'."
        lines = [f"Products matching '{query}':"]
        for p in products:
            brand_str = f" ({p.brand})" if p.brand else ""
            lines.append(
                f"  product_id={p.product_id} | {p.name}{brand_str} | {p.unit} | MRP Rs.{p.mrp} | GST {p.gst_rate}%"
            )
        return "\n".join(lines)

    async def list_products() -> str:
        """List all active products in the catalogue with their full product_ids."""
        from src.mcp.catalogue.models import ProductResult
        products: list[ProductResult] = await mcps.catalogue.list_products(
            store_id=store_id
        )
        if not products:
            return "Catalogue is empty."
        lines = ["Catalogue:"]
        for p in products:
            brand_str = f" ({p.brand})" if p.brand else ""
            lines.append(
                f"  product_id={p.product_id} | {p.name}{brand_str} | {p.unit} | MRP Rs.{p.mrp} | GST {p.gst_rate}%"
            )
        return "\n".join(lines)

    # ── CATALOGUE intent ─────────────────────────────────────────────────

    if intent == "CATALOGUE":
        async def add_product(
            name: str, is_loose: bool, unit: str, cost_price: float,
            mrp: float, reorder_level: float, brand: str | None = None,
            hsn_code: str | None = None, gst_rate: float = 0.0,
        ) -> str:
            """Add a new product to the catalogue after owner confirmation."""
            from src.mcp.catalogue.models import AddProductResult
            result: AddProductResult = await mcps.catalogue.add_product(
                store_id=store_id, name=name, is_loose=is_loose, unit=unit,
                cost_price=cost_price, mrp=mrp, reorder_level=reorder_level,
                brand=brand, hsn_code=hsn_code, gst_rate=gst_rate, telegram_user_id=tuid,
            )
            return result.message

        async def update_product_details(
            product_id: str,
            name: str | None = None, brand: str | None = None,
            unit: str | None = None, is_loose: bool | None = None,
            cost_price: float | None = None, mrp: float | None = None,
            reorder_level: float | None = None, gst_rate: float | None = None,
            hsn_code: str | None = None,
        ) -> str:
            """Update any field of an existing product. Use full product_id from list/search."""
            result = await mcps.catalogue.update_product_details(
                store_id=store_id, product_id=product_id,
                name=name, brand=brand, unit=unit, is_loose=is_loose,
                cost_price=cost_price, mrp=mrp, reorder_level=reorder_level,
                gst_rate=gst_rate, hsn_code=hsn_code,
            )
            brand_str = f" ({result.brand})" if result.brand else ""
            return f"Updated: {result.name}{brand_str} | {result.unit} | MRP Rs.{result.mrp} | GST {result.gst_rate}%"

        async def deactivate_product(product_id: str) -> str:
            """Remove a product from the catalogue (soft delete). Use full product_id."""
            from src.mcp.catalogue.models import DeactivateResult
            result: DeactivateResult = await mcps.catalogue.deactivate_product(
                store_id=store_id, product_id=product_id
            )
            return result.message

        async def update_store(
            shop_name: str | None = None, phone: str | None = None,
            address: str | None = None, state_code: str | None = None,
            default_payment_mode: str | None = None,
        ) -> str:
            """Update store details. Only pass fields that should change."""
            result = await mcps.identity.update_store(
                store_id=store_id, shop_name=shop_name, phone=phone,
                address=address, state_code=state_code,
                default_payment_mode=default_payment_mode,
            )
            return f"Store updated: {result.shop_name} | Phone: {result.phone or 'not set'} | State: {result.state_code}"

        async def update_owner_name(first_name: str | None = None, last_name: str | None = None) -> str:
            """Update the owner's name on their profile."""
            result = await mcps.identity.update_user(
                telegram_user_id=tuid, first_name=first_name, last_name=last_name
            )
            return result.message

        return [search_products, list_products, add_product, update_product_details,
                deactivate_product, update_store, update_owner_name]

    # ── INVENTORY intent ─────────────────────────────────────────────────

    if intent == "INVENTORY":
        async def receive_stock(product_id: str, quantity: float, notes: str | None = None) -> str:
            """Record received stock for a product."""
            from src.mcp.inventory.models import ReceiveStockResult
            result: ReceiveStockResult = await mcps.inventory.receive_stock(
                store_id=store_id, product_id=product_id,
                quantity=quantity, notes=notes, telegram_user_id=tuid,
            )
            return result.message

        async def get_stock(product_id: str) -> str:
            """Get current stock level for a product."""
            result = await mcps.inventory.get_stock(
                store_id=store_id, product_id=product_id
            )
            return str(result)

        async def get_all_stock() -> str:
            """Get stock levels for all products."""
            result = await mcps.inventory.get_all_stock(store_id=store_id)
            return str(result)

        async def check_availability(product_id: str, quantity: float) -> str:
            """Check if enough stock is available for a sale."""
            result = await mcps.inventory.check_availability(
                store_id=store_id, product_id=product_id, requested_quantity=quantity
            )
            return str(result)

        async def get_low_stock_items() -> str:
            """Get all items that are at or below their reorder level."""
            result = await mcps.inventory.get_low_stock_items(store_id=store_id)
            return str(result)

        async def get_stock_movements(product_id: str) -> str:
            """Get stock movement history for a product."""
            result = await mcps.inventory.get_stock_movements(
                store_id=store_id, product_id=product_id
            )
            return str(result)

        return [search_products, receive_stock, get_stock, get_all_stock,
                check_availability, get_low_stock_items, get_stock_movements]

    # ── KHATA intent ─────────────────────────────────────────────────────

    if intent == "KHATA":
        async def add_customer(name: str, phone: str) -> str:
            """
            Add or find a customer by phone number.
            phone is MANDATORY for all credit customers (10-digit Indian mobile).
            """
            result = await mcps.khata.add_customer(
                store_id=store_id, name=name, phone=phone
            )
            # Return only the human message — avoids LLM misreading raw balance float
            return f"customer_id={result.customer_id}\n{result.message}"

        async def get_customer(name_or_phone: str) -> str:
            """Look up a customer by name or phone number."""
            result = await mcps.khata.get_customer(
                store_id=store_id, name_or_phone=name_or_phone
            )
            if not result.found:
                return f"No customer found matching '{name_or_phone}'."
            lines = []
            for c in result.customers:
                lines.append(
                    f"customer_id={c.customer_id} | {c.name} ({c.phone}) | {_balance_summary(c.current_balance, c.name)}"
                )
            return "\n".join(lines)

        async def add_credit_entry(customer_id: str, amount: float, notes: str | None = None) -> str:
            """
            Record that the shop gave goods/money ON CREDIT to a customer — customer now OWES the shop.
            Use this ONLY when the customer has taken something without paying (amount_delta = +positive).
            DO NOT use this when a customer pays more than they owe — use add_payment_entry instead.
            Example: customer takes ₹200 of groceries on credit → add_credit_entry(amount=200)
            """
            result = await mcps.khata.add_credit_entry(
                store_id=store_id, customer_id=customer_id,
                amount=amount, notes=notes
            )
            return result.message

        async def add_payment_entry(customer_id: str, amount: float, notes: str | None = None) -> str:
            """
            Record that a customer paid money to the shop — reduces what they owe, or creates a credit in their favour.
            Use this when:
              - Customer pays off their outstanding balance (amount_delta = -negative, reduces debt)
              - Customer OVERPAYS a cash/UPI bill and the extra amount should be stored for future use
                (e.g. bill was ₹107, customer paid ₹200, extra ₹93 → add_payment_entry(amount=93))
            The balance after this call will be negative if the shop now owes the customer.
            """
            result = await mcps.khata.add_payment_entry(
                store_id=store_id, customer_id=customer_id,
                amount=amount, notes=notes
            )
            return result.message

        async def get_balance(customer_id: str) -> str:
            """Get the current outstanding balance for a customer."""
            result = await mcps.khata.get_balance(
                store_id=store_id, customer_id=customer_id
            )
            return result.message

        async def get_khata_history(customer_id: str) -> str:
            """Get full transaction history for a customer."""
            result = await mcps.khata.get_khata_history(
                store_id=store_id, customer_id=customer_id
            )
            return str(result)

        async def list_customers_with_balances() -> str:
            """List all customers with their current outstanding balances."""
            result = await mcps.khata.list_customers_with_balances(store_id=store_id)
            if not result:
                return "No customers found."
            lines = []
            for c in result:
                lines.append(
                    f"customer_id={c.customer_id} | {c.name} ({c.phone}) | {_balance_summary(c.balance, c.name)}"
                )
            return "\n".join(lines)

        return [search_products, add_customer, get_customer, add_credit_entry,
                add_payment_entry, get_balance, get_khata_history, list_customers_with_balances]

    # ── ANALYTICS intent ─────────────────────────────────────────────────

    if intent == "ANALYTICS":
        async def get_daily_summary(date_str: str | None = None) -> str:
            """
            Get daily sales summary.
            - date_str: YYYY-MM-DD (default: today)
            """
            from datetime import date as _date
            target = date_str or _date.today().isoformat()
            result = await mcps.analytics.get_daily_summary(
                store_id=store_id, summary_date=target
            )
            return str(result)

        async def get_sales_trend(
            start_date: str | None = None,
            end_date: str | None = None,
        ) -> str:
            """
            Get daily sales trend for a date range.
            - start_date: YYYY-MM-DD (default: 7 days ago)
            - end_date: YYYY-MM-DD (default: today)
            If the owner says 'last 7 days' compute the dates yourself.
            """
            from datetime import date as _date, timedelta
            today = _date.today()
            sd = start_date or (today - timedelta(days=6)).isoformat()
            ed = end_date or today.isoformat()
            result = await mcps.analytics.get_sales_trend(
                store_id=store_id, start_date=sd, end_date=ed
            )
            return str(result)

        async def get_top_items(
            start_date: str | None = None,
            end_date: str | None = None,
            limit: int = 10,
        ) -> str:
            """
            Get top-selling items for a date range.
            - start_date: YYYY-MM-DD (default: 30 days ago)
            - end_date: YYYY-MM-DD (default: today)
            - limit: number of items to return (default 10)
            """
            from datetime import date as _date, timedelta
            today = _date.today()
            sd = start_date or (today - timedelta(days=29)).isoformat()
            ed = end_date or today.isoformat()
            result = await mcps.analytics.get_top_items(
                store_id=store_id, start_date=sd, end_date=ed, limit=limit
            )
            return str(result)

        async def get_gst_summary(
            start_date: str | None = None,
            end_date: str | None = None,
        ) -> str:
            """
            Get GST collected summary for a date range.
            - start_date: YYYY-MM-DD (default: first day of current month)
            - end_date: YYYY-MM-DD (default: today)
            If the owner says 'this month' or 'last month' compute the dates yourself.
            """
            from datetime import date as _date
            today = _date.today()
            sd = start_date or today.replace(day=1).isoformat()
            ed = end_date or today.isoformat()
            result = await mcps.analytics.get_gst_summary(
                store_id=store_id, start_date=sd, end_date=ed
            )
            return str(result)

        async def get_low_stock_items() -> str:
            """Get items at or below reorder level."""
            result = await mcps.inventory.get_low_stock_items(store_id=store_id)
            return str(result)

        return [search_products, get_daily_summary, get_sales_trend,
                get_top_items, get_gst_summary, get_low_stock_items]

    # ── BILLING — default ACTIVE group (most common daily use) ───────────

    async def check_availability(product_id: str, quantity: float) -> str:
        """
        Check if a product has enough stock for a sale.
        Returns available quantity and whether partial fulfillment is possible.
        product_id MUST be a full UUID from search_products or list_products — never invent one.
        """
        from src.utils.guardrails import clean_uuid
        if not clean_uuid(product_id):
            return (
                f"ERROR: '{product_id}' is not a valid product_id. "
                "Call search_products(query='<name>') first to get the real UUID."
            )
        result = await mcps.inventory.check_availability(
            store_id=store_id, product_id=product_id, requested_quantity=quantity
        )
        return str(result)

    async def create_draft_bill() -> str:
        """
        Start a new bill session.
        ONLY call this if ACTIVE BILL in the system prompt shows 'None'.
        If a draft is already open (ACTIVE BILL shows a UUID), skip this — use the existing draft.
        """
        result = await mcps.billing.create_draft_bill(
            store_id=store_id, telegram_user_id=tuid
        )
        # Update the shared cell so add_item_to_draft and finalize_bill see the new ID
        # within this same request, even though they were captured before the draft existed.
        _draft_id_cell[0] = result.draft_bill_id
        return (
            f"draft_bill_id={result.draft_bill_id}\n"
            f"status={result.status} | items={result.item_count} | total=₹{result.estimated_total:.2f}"
        )

    async def add_item_to_draft(product_id: str, quantity: float) -> str:
        """
        Add a product to the active bill draft.
        - product_id: FULL UUID from search_products or list_products — never invent.
        - quantity: how many units.
        Do NOT pass draft_bill_id — resolved automatically from the active draft.
        """
        from src.utils.guardrails import clean_uuid
        if not clean_uuid(product_id):
            return (
                f"ERROR: '{product_id}' is not a valid product_id. "
                "Call search_products(query='<name>') first to get the real UUID."
            )
        resolved_id = _draft_id_cell[0]
        if not resolved_id:
            return "ERROR: No active draft bill. Call create_draft_bill() first."
        try:
            result = await mcps.billing.add_item_to_draft(
                draft_bill_id=resolved_id, product_id=product_id,
                quantity=quantity
            )
        except ValueError as e:
            msg = str(e)
            if "not found" in msg.lower():
                return (
                    f"ERROR: product_id '{product_id}' does not exist in this store's catalogue. "
                    "Call search_products(query='<item name>') to get the correct product_id."
                )
            return f"ERROR: {msg}"
        return str(result)

    async def remove_item_from_draft(product_id: str) -> str:
        """
        Remove a product from the active bill draft.
        - product_id: FULL UUID from search_products.
        Do NOT pass draft_bill_id — resolved automatically.
        """
        from src.utils.guardrails import clean_uuid
        if not clean_uuid(product_id):
            return f"ERROR: '{product_id}' is not a valid product_id. Call search_products() first."
        resolved_id = _draft_id_cell[0]
        if not resolved_id:
            return "ERROR: No active draft bill. Call create_draft_bill() first."
        result = await mcps.billing.remove_item_from_draft(
            draft_bill_id=resolved_id, product_id=product_id
        )
        return str(result)

    async def update_item_quantity(product_id: str, quantity: float) -> str:
        """
        Update the quantity of an item already in the active bill draft.
        - product_id: FULL UUID from search_products.
        Do NOT pass draft_bill_id — resolved automatically.
        """
        from src.utils.guardrails import clean_uuid
        if not clean_uuid(product_id):
            return f"ERROR: '{product_id}' is not a valid product_id. Call search_products() first."
        resolved_id = _draft_id_cell[0]
        if not resolved_id:
            return "ERROR: No active draft bill. Call create_draft_bill() first."
        result = await mcps.billing.update_item_quantity(
            draft_bill_id=resolved_id, product_id=product_id, new_quantity=quantity
        )
        return str(result)

    async def get_draft_bill() -> str:
        """Get the current contents and total of the active bill draft.
        Do NOT pass draft_bill_id — resolved automatically.
        """
        resolved_id = _draft_id_cell[0]
        if not resolved_id:
            return "ERROR: No active draft bill. Call create_draft_bill() first."
        result = await mcps.billing.get_draft_bill(draft_bill_id=resolved_id)
        return str(result)

    async def finalize_bill(
        payment_mode: str,
        is_credit: bool = False,
        customer_id: str | None = None,
    ) -> str:
        """
        Finalize and confirm the active bill.
        - payment_mode: REQUIRED — must be exactly one of: CASH / UPI / CREDIT
          NEVER call this without first asking the owner 'Cash, UPI or credit?'
          NEVER default to CASH — always ask the owner explicitly.
        - is_credit: True for credit sales (requires customer_id)
        - customer_id: required only for credit sales
        Do NOT pass draft_bill_id — it is resolved automatically from the server.
        """
        # Always use the server-authoritative active draft — never trust LLM-passed IDs
        resolved_id = _draft_id_cell[0]
        if not resolved_id:
            return (
                "ERROR: No active draft bill found. Call create_draft_bill() first to start a bill."
            )
        result = await mcps.billing.finalize_bill(
            draft_bill_id=resolved_id,
            payment_mode=payment_mode,
            telegram_user_id=tuid,
            is_credit=is_credit,
            customer_id=customer_id,
        )
        # Put bill_id on the FIRST line so the LLM can reliably read it back
        # for confirm_payment / cancel_bill / void_bill in the next turn.
        return (
            f"bill_id={result.bill_id}\n"
            f"bill_number={result.bill_number}\n"
            f"status=PENDING_PAYMENT\n"
            f"total=₹{result.total_amount:.2f} | payment_mode={result.payment_mode}\n"
            f"{result.message}"
        )

    async def cancel_draft_bill() -> str:
        """Cancel the current active bill draft (discard all items).
        Do NOT pass draft_bill_id — it is resolved automatically from the server.
        """
        # Always use the server-authoritative active draft — never trust LLM-passed IDs
        resolved_id = _draft_id_cell[0]
        if not resolved_id:
            return "ERROR: No active draft bill found."
        result = await mcps.billing.cancel_draft_bill(draft_bill_id=resolved_id)
        return str(result)

    async def get_bill(bill_id: str) -> str:
        """Get details of a finalized bill."""
        from src.utils.guardrails import clean_uuid
        if not clean_uuid(bill_id):
            return f"ERROR: '{bill_id}' is not a valid bill_id."
        result = await mcps.billing.get_bill(bill_id=bill_id)
        return str(result)

    async def confirm_payment(bill_id: str) -> str:
        """
        Confirm that payment was received for a bill in PENDING_PAYMENT status.
        Call this after:
          - Cash is handed over and counted
          - UPI transfer is received/confirmed
          - Credit terms are accepted (for CREDIT payment mode)
        This moves the bill from PENDING_PAYMENT → CONFIRMED.
        - bill_id: from the finalize_bill result
        """
        from src.utils.guardrails import clean_uuid
        if not clean_uuid(bill_id):
            return f"ERROR: '{bill_id}' is not a valid bill_id."
        result = await mcps.billing.confirm_payment(bill_id=bill_id)
        return result.message

    async def cancel_bill(bill_id: str) -> str:
        """
        Cancel a bill that is in PENDING_PAYMENT status (before payment is confirmed).
        This restores all stock back to inventory and reverses any khata entry.
        Use this when owner says 'cancel', 'wrong items', 'start over' AFTER finalize_bill
        but BEFORE confirm_payment.
        - bill_id: from the finalize_bill result
        DO NOT use this on CONFIRMED bills — use void_bill instead.
        DO NOT use cancel_draft_bill — that is for OPEN drafts only.
        """
        from src.utils.guardrails import clean_uuid
        if not clean_uuid(bill_id):
            return f"ERROR: '{bill_id}' is not a valid bill_id."
        result = await mcps.billing.cancel_bill(bill_id=bill_id)
        return result.message

    async def void_bill(bill_id: str) -> str:
        """
        Void a CONFIRMED bill — full reversal after payment was already confirmed.
        This restores all stock and reverses the payment/khata entry.
        Use this when owner says 'cancel', 'undo', or 'wrong' AFTER confirm_payment.
        - bill_id: from the finalize_bill or get_bill result
        DO NOT use this on PENDING_PAYMENT bills — use cancel_bill instead.
        """
        from src.utils.guardrails import clean_uuid
        if not clean_uuid(bill_id):
            return f"ERROR: '{bill_id}' is not a valid bill_id."
        result = await mcps.billing.void_bill(bill_id=bill_id)
        return result.message

    async def add_customer(name: str, phone: str) -> str:
        """
        Add or find a customer for a credit sale.
        phone is MANDATORY (10-digit Indian mobile number).
        """
        result = await mcps.khata.add_customer(
            store_id=store_id, name=name, phone=phone
        )
        return f"customer_id={result.customer_id}\n{result.message}"

    async def get_customer(name_or_phone: str) -> str:
        """Look up a customer by name or phone."""
        result = await mcps.khata.get_customer(
            store_id=store_id, name_or_phone=name_or_phone
        )
        if not result.found:
            return f"No customer found matching '{name_or_phone}'."
        lines = []
        for c in result.customers:
            lines.append(
                f"customer_id={c.customer_id} | {c.name} ({c.phone}) | {_balance_summary(c.current_balance, c.name)}"
            )
        return "\n".join(lines)

    async def add_credit_entry(customer_id: str, amount: float, notes: str | None = None) -> str:
        """
        Record that the shop gave goods ON CREDIT — customer OWES the shop (amount_delta = +positive).
        Use ONLY for standalone credit advances, NOT during a billing session (finalize_bill handles that).
        DO NOT use this for overpayments — use add_payment_entry for any money received from the customer.
        """
        result = await mcps.khata.add_credit_entry(
            store_id=store_id, customer_id=customer_id,
            amount=amount, notes=notes
        )
        return str(result)

    async def add_payment_entry(customer_id: str, amount: float, notes: str | None = None) -> str:
        """
        Record that a customer paid money to the shop — reduces what they owe, or stores a credit in their favour.
        Use this when:
          - Customer pays off their outstanding balance
          - Customer OVERPAYS a cash/UPI bill and the extra should be stored for future use
            (e.g. bill was ₹202.85, customer paid ₹220, extra ₹17.15 → add_payment_entry(amount=17.15))
        The balance will be negative if the shop now owes the customer.
        """
        result = await mcps.khata.add_payment_entry(
            store_id=store_id, customer_id=customer_id,
            amount=amount, notes=notes
        )
        return result.message

    async def add_product(
        name: str,
        is_loose: bool,
        unit: str,
        cost_price: float,
        mrp: float,
        reorder_level: float,
        gst_rate: float,
        brand: str | None = None,
        hsn_code: str | None = None,
    ) -> str:
        """
        Add a new product to the catalogue during billing when it is not found by search_products.
        Use this ONLY when search_products returns no results for the product.
        - gst_rate: MANDATORY. Loose → pass 0. Branded → MUST be 5 / 12 / 18 / 28.
          NEVER pass 0 for branded items — ask the owner for the correct rate first.
        After adding, ALWAYS call receive_stock to add initial stock before adding to bill.
        Returns product_id in the result — use it for receive_stock and add_item_to_draft.
        """
        from src.mcp.catalogue.models import AddProductResult
        result: AddProductResult = await mcps.catalogue.add_product(
            store_id=store_id, name=name, is_loose=is_loose, unit=unit,
            cost_price=cost_price, mrp=mrp, reorder_level=reorder_level,
            brand=brand, hsn_code=hsn_code, gst_rate=gst_rate, telegram_user_id=tuid,
        )
        return result.message + f"\n[product_id={result.product_id}] — Ask owner for initial stock quantity, then call receive_stock, then add_item_to_draft."

    async def receive_stock(product_id: str, quantity: float, notes: str | None = None) -> str:
        """
        Add initial stock for a newly added product before it can be billed.
        - product_id: from add_product result
        - quantity: initial stock quantity (REQUIRED — must ask owner)
        """
        from src.mcp.inventory.models import ReceiveStockResult
        result: ReceiveStockResult = await mcps.inventory.receive_stock(
            store_id=store_id, product_id=product_id,
            quantity=quantity, notes=notes, telegram_user_id=tuid,
        )
        return result.message

    # ── BILLING_CONFIRM intent — post-finalize tools (no active draft) ────────
    # Only confirm_payment, cancel_bill, void_bill are needed here.
    # Shown when no draft is open but owner needs to confirm/cancel/void a bill.
    if intent == "BILLING_CONFIRM":
        return [
            search_products,
            confirm_payment, cancel_bill, void_bill,
            get_bill,
        ]

    # ── BILLING (default) — draft-building tools ──────────────────────────────
    # confirm_payment/cancel_bill/void_bill intentionally excluded here to keep
    # the tool count low during the normal add-items flow.
    return [
        search_products, list_products, check_availability,
        create_draft_bill, add_item_to_draft, remove_item_from_draft,
        update_item_quantity, get_draft_bill, finalize_bill,
        cancel_draft_bill,
        add_customer, get_customer, add_credit_entry, add_payment_entry,
        add_product, receive_stock,
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Shared formatting helpers
# ─────────────────────────────────────────────────────────────────────────────

def _balance_summary(balance: float, name: str) -> str:
    """
    Return a single unambiguous sentence about a customer's balance.
    Used by tool closures so the LLM never sees raw floats to misinterpret.
    """
    if balance > 0:
        return f"{name} owes the shop ₹{balance:.2f}"
    if balance < 0:
        return f"Shop owes {name} ₹{abs(balance):.2f}"
    return f"{name}'s account is settled (₹0)"


# ─────────────────────────────────────────────────────────────────────────────
# Intent detection
# ─────────────────────────────────────────────────────────────────────────────

INTENT_KEYWORDS: dict[str, list[str]] = {
    "BILLING_CONFIRM": [
        # Post-finalize actions: confirm payment, cancel or void a bill.
        # These route to a smaller tool set that doesn't include draft-building tools.
        "confirm payment", "confirm the payment", "payment confirmed", "payment done",
        "void bill", "void the bill", "cancel bill", "cancel the bill",
        "undo bill", "reverse bill", "payment received",
    ],
    "CATALOGUE":  [
        "catalogue", "add product", "add item", "new product", "new item",
        "brand", "gst rate", "hsn", "mrp", "cost price", "reorder level",
        "edit product", "update product", "remove product",
    ],
    "INVENTORY":  [
        "stock", "inventory", "restock", "low stock", "out of stock",
        "stock movement", "stock history", "movement history", "received stock",
        "add stock", "how much stock", "units left", "units available", "in stock",
        "full report", "all stock", "stock report",
    ],
    "KHATA":      [
        # NOTE: "credit" is intentionally EXCLUDED here.
        # "credit" during an active billing session means payment mode = CREDIT
        # and must route to BILLING so finalize_bill is available.
        # Only explicit khata-management phrases route here.
        "khata", "udhar", "customer balance",
        "customer payment", "owes", "due", "settle", "pay back",
        "how much does", "customer owes", "add customer",
        "gave money", "returned money",
        "overpayment", "extra amount", "paid extra", "paid more", "save it",
        "save in khata", "credit balance", "keep it",
    ],
    "ANALYTICS":  [
        "report", "daily summary", "sales summary", "sales trend",
        "top items", "top selling", "analytics", "pdf", "pptx",
        "close day", "revenue", "gst report", "gst summary",
    ],
}

# These billing-mode words must never redirect away from BILLING
# even if they also appear in KHATA keywords by mistake.
_BILLING_PAYMENT_WORDS = frozenset({
    "cash", "upi", "credit", "card", "finalize", "finalise",
    "confirm bill", "confirm the bill", "complete bill", "done",
})

_PAID_PATTERN = _re.compile(r"\bpaid\b.*?\d|[\w]+ paid\b", _re.IGNORECASE)


def detect_intent(user_message: str, has_active_draft: bool = False) -> str:
    """
    Lightweight keyword-based intent detector.
    Falls back to BILLING (the most common daily task).

    If has_active_draft=True (an open draft bill exists), payment-mode words
    like 'credit', 'cash', 'upi' always route to BILLING so finalize_bill
    is available — never to KHATA.

    BILLING_CONFIRM is only reachable when no draft is active — it handles
    confirm_payment / cancel_bill / void_bill after finalize_bill is done.
    """
    msg = user_message.lower()

    # If a draft bill is active, payment-mode words mean "pay this bill" →
    # always BILLING, not KHATA or BILLING_CONFIRM.
    if has_active_draft and any(w in msg for w in _BILLING_PAYMENT_WORDS):
        return "BILLING"

    # BILLING_CONFIRM is only meaningful when there is no open draft.
    # If a draft is active, these phrases still belong to BILLING flow.
    if not has_active_draft:
        for kw in INTENT_KEYWORDS["BILLING_CONFIRM"]:
            if kw in msg:
                return "BILLING_CONFIRM"

    # Special pattern: "X paid <number>" or "X paid" → payment in khata
    if _PAID_PATTERN.search(user_message):
        return "KHATA"

    for intent, keywords in INTENT_KEYWORDS.items():
        if intent == "BILLING_CONFIRM":
            continue  # already handled above
        if any(kw in msg for kw in keywords):
            return intent
    return "BILLING"
