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
            loose_str = "LOOSE" if p.is_loose else "BRANDED"
            lines.append(
                f"  product_id={p.product_id} | {p.name}{brand_str} | {loose_str} | {p.unit} | MRP Rs.{p.mrp} | GST {p.gst_rate}%"
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
            loose_str = "LOOSE" if p.is_loose else "BRANDED"
            lines.append(
                f"  product_id={p.product_id} | {p.name}{brand_str} | {loose_str} | {p.unit} | MRP Rs.{p.mrp}"
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
            loose_str = "LOOSE" if p.is_loose else "BRANDED"
            lines.append(
                f"  product_id={p.product_id} | {p.name}{brand_str} | {loose_str} | {p.unit} | Reorder at {p.reorder_level}"
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
            loose_str = "LOOSE" if p.is_loose else "BRANDED"
            lines.append(
                f"  product_id={p.product_id} | {p.name}{brand_str} | {loose_str} | {p.unit}"
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

async def _resolve_bill_number(mcps: MCPInstances, bill_id: str) -> str | None:
    """Fetch the human-readable bill number (e.g. BL-003-20260810-010) for a given bill UUID.
    Returns None silently on any error so callers can treat it as optional."""
    try:
        detail = await mcps.billing.get_bill(bill_id=bill_id)
        return detail.bill_number
    except Exception:
        return None


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

    # _bill_id_cell holds the UUID of the most recent PENDING_PAYMENT or recently CONFIRMED bill for this store.
    # confirm_payment / cancel_bill / add_payment_entry read from here — the LLM never passes bill_id.
    # Initialized by querying the DB for the latest PENDING_PAYMENT bill (or most recently confirmed bill
    # in the last 15 mins for post-confirmation overpayment khata entries).
    # _last_confirmed_bill_id stores the bill ID so overpayment entries in subsequent turns still capture reference_bill_id.
    _pending_bill_id: str | None = None
    _last_confirmed_bill_id: str | None = None
    if store_id:
        try:
            _pb_resp = (
                mcps.billing.db.schema("billing")
                .table("bills")
                .select("id, status")
                .eq("store_id", store_id)
                .in_("status", ["PENDING_PAYMENT", "CONFIRMED"])
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            _pb_rows = _pb_resp.data or []
            if _pb_rows:
                _pending_bill_id = _pb_rows[0]["id"]
                if _pb_rows[0]["status"] == "CONFIRMED":
                    _last_confirmed_bill_id = _pb_rows[0]["id"]
        except Exception:
            pass
    _bill_id_cell: list[str | None] = [_pending_bill_id]
    _last_confirmed_bill_cell: list[str | None] = [_last_confirmed_bill_id]

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
            loose_str = "LOOSE" if p.is_loose else "BRANDED"
            lines.append(
                f"  product_id={p.product_id} | {p.name}{brand_str} | {loose_str} | {p.unit} | MRP Rs.{p.mrp} | GST {p.gst_rate}%"
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
            loose_str = "LOOSE" if p.is_loose else "BRANDED"
            lines.append(
                f"  product_id={p.product_id} | {p.name}{brand_str} | {loose_str} | {p.unit} | MRP Rs.{p.mrp} | GST {p.gst_rate}%"
            )
        return "\n".join(lines)

    async def get_payment_history(name_or_phone: str) -> str:
        """
        Get full payment and billing history for a customer.

        USE WHEN:
          - Owner asks: 'show Ramesh's payment history', 'what has Ramesh paid?',
            'all bills for Ramesh', 'payment record for 9876543210'
          - Any request combining payments + bills for a specific customer

        - name_or_phone: customer name (partial match) or 10-digit phone (exact match)

        Returns all payments and bills sorted newest first, plus current outstanding balance.
        """
        lookup = await mcps.khata.get_customer(
            store_id=store_id, name_or_phone=name_or_phone
        )
        if not lookup.found:
            return f"No customer found matching '{name_or_phone}'."
        if len(lookup.customers) > 1:
            lines = [f"Multiple customers found matching '{name_or_phone}'. Please specify:"]
            for c in lookup.customers:
                lines.append(f"  {c.name} ({c.phone}) — customer_id={c.customer_id}")
            return "\n".join(lines)
        customer = lookup.customers[0]
        result = await mcps.payments.get_payment_history(
            store_id=store_id, customer_id=customer.customer_id
        )
        # Format combined output
        lines = [
            f"Payment & Bill History — {result.customer_name} ({result.phone})",
            f"Current Balance: {_balance_summary(result.outstanding_balance, result.customer_name)}",
            f"Total Paid: ₹{result.total_paid:.2f}",
            "",
        ]
        if result.bills:
            lines.append("── BILLS ──────────────────────────")
            for b in result.bills:
                credit_tag = " [CREDIT]" if b.is_credit else ""
                lines.append(
                    f"  {b.bill_number} | ₹{b.total_amount:.2f} | {b.payment_mode}{credit_tag} | "
                    f"{b.payment_status} | {b.created_at[:10]}"
                )
        if result.payments:
            lines.append("")
            lines.append("── PAYMENTS ────────────────────────")
            for p in result.payments:
                bill_ref = f" | Bill {p.bill_number}" if p.bill_number else ""
                lines.append(
                    f"  ₹{p.paid_amount:.2f} | {p.payment_type} | {p.payment_status} | "
                    f"{p.payment_mode}{bill_ref} | {p.created_at[:10]}"
                )
        return "\n".join(lines)

    async def generate_invoice_pdf(bill_number_or_id: str) -> str:
        """
        Generate and send a PDF invoice for a finalized bill to this Telegram chat.

        USE WHEN: owner asks 'send invoice for BL-003-20260815-013', 'PDF for this bill',
        'share bill as PDF', 'generate invoice'.

        - bill_number_or_id: either the human bill number (e.g. BL-003-20260815-013)
          OR the bill UUID from get_bills_by_date / get_payment_history.
          If only a bill number is known, pass it — the tool resolves the bill_id internally.

        Sends the PDF directly to the owner's Telegram chat and deletes the temp file.
        Returns a confirmation message.
        """
        import os as _os
        from src.utils.guardrails import clean_uuid as _clean_uuid
        from src.telegram.telegram_client import get_telegram_client

        resolved_bill_id: str | None = None
        if _clean_uuid(bill_number_or_id):
            resolved_bill_id = bill_number_or_id
        else:
            # Lookup by bill_number
            try:
                resp = (
                    mcps.billing.db.schema("billing")
                    .table("bills")
                    .select("id")
                    .eq("store_id", store_id)
                    .eq("bill_number", bill_number_or_id.strip())
                    .limit(1)
                    .execute()
                )
                rows = resp.data or []
                if rows:
                    resolved_bill_id = rows[0]["id"]
            except Exception:
                pass

        if not resolved_bill_id:
            return (
                f"ERROR: Could not find bill '{bill_number_or_id}'. "
                "Use get_bills_by_date() or get_payment_history() to find the correct bill number."
            )

        try:
            result = await mcps.documents.generate_invoice_pdf(
                bill_id=resolved_bill_id,
                store_id=store_id,
                telegram_user_id=tuid,
            )
            telegram = get_telegram_client()
            await telegram.send_document(
                chat_id=tuid,
                file_path=result.file_path,
                caption=f"Invoice {result.bill_number}",
            )
            _os.remove(result.file_path)
            return f"✅ Invoice {result.bill_number} sent as PDF ({result.file_size_bytes} bytes)."
        except Exception as e:
            return f"ERROR generating invoice: {e}"

    async def list_bills_for_customer(name_or_phone: str) -> str:
        """
        List all finalized bills for a specific customer.

        USE WHEN: owner asks 'show bills for Ramesh', 'all bills for 9876543210',
        'what did Ramesh buy', 'list purchases for Varun'.

        - name_or_phone: customer name (partial match) or 10-digit phone number.

        Returns bill number, date, total, payment mode for each bill, plus current khata balance.
        For full payment rows use get_payment_history instead.
        """
        lookup = await mcps.khata.get_customer(
            store_id=store_id, name_or_phone=name_or_phone
        )
        if not lookup.found:
            return f"No customer found matching '{name_or_phone}'."
        if len(lookup.customers) > 1:
            lines = [f"Multiple customers found for '{name_or_phone}'. Please specify:"]
            for c in lookup.customers:
                lines.append(f"  {c.name} ({c.phone}) — customer_id={c.customer_id}")
            return "\n".join(lines)
        customer = lookup.customers[0]
        # Fetch bills for this customer from billing.bills
        try:
            resp = (
                mcps.billing.db.schema("billing")
                .table("bills")
                .select("id, bill_number, total_amount, payment_mode, is_credit, status, created_at")
                .eq("store_id", store_id)
                .eq("customer_id", customer.customer_id)
                .order("created_at", desc=True)
                .execute()
            )
            bill_rows = resp.data or []
        except Exception as e:
            return f"ERROR fetching bills: {e}"
        if not bill_rows:
            return f"No bills found for {customer.name} ({customer.phone})."
        lines = [
            f"Bills for {customer.name} ({customer.phone}) — "
            f"{_balance_summary(customer.current_balance, customer.name)}",
            f"Total bills: {len(bill_rows)}",
            "",
        ]
        for b in bill_rows:
            credit_tag = " [CREDIT]" if b.get("is_credit") else ""
            lines.append(
                f"  {b['bill_number']} | ₹{float(b['total_amount']):.2f} | "
                f"{b['payment_mode']}{credit_tag} | {b['status']} | "
                f"{b['created_at'][:10]} | bill_id={b['id']}"
            )
        return "\n".join(lines)

    async def get_bills_by_date(date_str: str | None = None) -> str:
        """
        List all finalized bills for a specific date.

        USE WHEN: owner asks 'show bills for today', 'list bills on 15 Aug',
        'what bills were made today', 'bills on 13th august', 'bills for 2026-08-13'.

        - date_str: date in YYYY-MM-DD format (default: today in IST).
          Convert natural language dates like '13th august' → '2026-08-13' yourself.

        Returns bill number, total amount, payment mode, item count, and bill_id for each bill.
        To generate a PDF for one of these bills, call generate_invoice_pdf(bill_id).
        """
        from src.utils.ist import today_ist as _today_ist
        target = date_str or _today_ist().isoformat()
        results = await mcps.billing.get_bills_by_date(store_id=store_id, date=target)
        if not results:
            return f"No bills found for {target}."
        lines = [f"Bills for {target} ({len(results)} total):"]
        for b in results:
            credit_tag = " [CREDIT]" if b.is_credit else ""
            lines.append(
                f"  {b.bill_number} | ₹{b.total_amount:.2f} | "
                f"{b.payment_mode}{credit_tag} | {b.item_count} items | "
                f"bill_id={b.bill_id}"
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
            return result.message + f"\n[product_id={result.product_id}] — MANDATORY: If the owner provided initial stock quantity in their prompt, IMMEDIATELY call receive_stock(product_id, initial_stock_qty) in this same turn!"

        async def receive_stock(product_id: str, quantity: float, notes: str | None = None) -> str:
            """Record received stock for a product."""
            from src.mcp.inventory.models import ReceiveStockResult
            result: ReceiveStockResult = await mcps.inventory.receive_stock(
                store_id=store_id, product_id=product_id,
                quantity=quantity, notes=notes, telegram_user_id=tuid,
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

        return [search_products, list_products, add_product, receive_stock, update_product_details,
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
            # Safety net: if there is a pending Redis UNDERPAYMENT intent (routing landed here
            # instead of BILLING_CONFIRM), run the full resolution — confirm bill, record payment
            # row, link customer — so the DB stays consistent regardless of intent routing.
            redis = _get_redis()
            intent = None
            if redis:
                intent = await redis.get_pending_payment(tuid)

            if intent and intent.get("intent_type") == "UNDERPAYMENT":
                bill_id = intent["bill_id"]
                # Confirm the bill (still PENDING_PAYMENT)
                _cr = await mcps.billing.confirm_payment(bill_id=bill_id)
                if _cr.success:
                    _last_confirmed_bill_cell[0] = bill_id
                    _bill_id_cell[0] = None

                resolved_amount = float(intent["delta_amount"])
                bill_num = intent.get("bill_number", "")
                khata_result = await mcps.khata.add_credit_entry(
                    store_id=store_id, customer_id=customer_id,
                    amount=resolved_amount,
                    reference_bill_id=bill_id,
                    notes=notes or f"Remaining balance for bill {bill_num}",
                )
                # Record UNDERPAYMENT payment row now that we have khata_entry_id
                await mcps.payments.record_payment(
                    store_id=store_id,
                    bill_id=bill_id,
                    bill_number=bill_num or None,
                    customer_id=customer_id,
                    khata_entry_id=khata_result.entry_id,
                    paid_amount=float(intent["paid_amount"]),
                    payment_mode=intent["payment_mode"],
                    payment_type="UNDERPAYMENT",
                    payment_status="CONFIRMED",
                    subtotal=float(intent.get("subtotal") or 0) or None,
                    total_gst=float(intent.get("total_gst") or 0) or None,
                    bill_amount=float(intent["bill_amount"]),
                    change_amount=0.0,
                    balance_due=resolved_amount,
                )
                try:
                    await mcps.billing.link_bill_customer(
                        bill_id=bill_id, customer_id=customer_id
                    )
                except Exception:
                    pass
                if redis:
                    await redis.clear_pending_payment(tuid)
                return khata_result.message

            # Standalone credit (no pending underpayment) — normal path
            result = await mcps.khata.add_credit_entry(
                store_id=store_id, customer_id=customer_id,
                amount=amount, notes=notes,
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
            # Safety net: if there is a pending Redis OVERPAYMENT intent (routing landed here
            # instead of BILLING_CONFIRM), run the full resolution — confirm bill, record payment
            # row, link customer — so the DB stays consistent regardless of intent routing.
            redis = _get_redis()
            intent = None
            if redis:
                intent = await redis.get_pending_payment(tuid)

            if intent and intent.get("intent_type") == "OVERPAYMENT":
                bill_id = intent["bill_id"]
                # Confirm the bill (still PENDING_PAYMENT)
                _cr = await mcps.billing.confirm_payment(bill_id=bill_id)
                if _cr.success:
                    _last_confirmed_bill_cell[0] = bill_id
                    _bill_id_cell[0] = None

                resolved_amount = float(intent["delta_amount"])
                bill_num = intent.get("bill_number", "")
                khata_result = await mcps.khata.add_payment_entry(
                    store_id=store_id, customer_id=customer_id,
                    amount=resolved_amount,
                    reference_bill_id=bill_id,
                    notes=notes or f"Overpayment surplus from bill {bill_num}",
                )
                # Record OVERPAYMENT payment row now that we have khata_entry_id
                await mcps.payments.record_payment(
                    store_id=store_id,
                    bill_id=bill_id,
                    bill_number=bill_num or None,
                    customer_id=customer_id,
                    khata_entry_id=khata_result.entry_id,
                    paid_amount=float(intent["paid_amount"]),
                    payment_mode=intent["payment_mode"],
                    payment_type="OVERPAYMENT",
                    payment_status="CONFIRMED",
                    subtotal=float(intent.get("subtotal") or 0) or None,
                    total_gst=float(intent.get("total_gst") or 0) or None,
                    bill_amount=float(intent["bill_amount"]),
                    change_amount=resolved_amount,
                    balance_due=0.0,
                )
                try:
                    await mcps.billing.link_bill_customer(
                        bill_id=bill_id, customer_id=customer_id
                    )
                except Exception:
                    pass
                if redis:
                    await redis.clear_pending_payment(tuid)
                return khata_result.message

            # Standalone payment / khata settlement — normal path
            resolved_ref_bill_id = _bill_id_cell[0] or _last_confirmed_bill_cell[0]
            result = await mcps.khata.add_payment_entry(
                store_id=store_id, customer_id=customer_id,
                amount=amount, reference_bill_id=resolved_ref_bill_id, notes=notes
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
                add_payment_entry, get_balance, get_khata_history, list_customers_with_balances,
                get_payment_history]

    # ── ANALYTICS intent ─────────────────────────────────────────────────

    if intent == "ANALYTICS":
        async def get_daily_summary(date_str: str | None = None) -> str:
            """
            Get daily sales summary.
            - date_str: YYYY-MM-DD (default: today)
            """
            from src.utils.ist import today_ist as _today_ist
            target = date_str or _today_ist().isoformat()
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
            from datetime import timedelta
            from src.utils.ist import today_ist as _today_ist
            today = _today_ist()
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
            from datetime import timedelta
            from src.utils.ist import today_ist as _today_ist
            today = _today_ist()
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
            from src.utils.ist import today_ist as _today_ist
            today = _today_ist()
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

        # get_bills_by_date is a shared closure defined above (available in BILLING_CONFIRM too)

        async def generate_analysis_pptx(period: str = "THIS_WEEK") -> str:
            """
            Generate and send a PPTX analytics deck for this Telegram chat.

            USE WHEN: owner asks 'send analytics', 'weekly report', 'pptx', 'analysis deck',
            'sales report', 'this week report', 'monthly analysis'.

            - period: TODAY | THIS_WEEK (default) | THIS_MONTH

            Sends the PPTX directly to the owner's Telegram chat and deletes the temp file.
            5 slides: Executive Summary, Daily Summary Table, Top Items, Stock Health, GST Summary.
            Returns a confirmation message.
            """
            import os as _os
            from src.telegram.telegram_client import get_telegram_client

            period_clean = period.strip().upper()
            if period_clean not in ("TODAY", "THIS_WEEK", "THIS_MONTH"):
                period_clean = "THIS_WEEK"

            try:
                result = await mcps.documents.generate_analysis_pptx(
                    store_id=store_id,
                    telegram_user_id=tuid,
                    period=period_clean,
                )
                telegram = get_telegram_client()
                await telegram.send_document(
                    chat_id=tuid,
                    file_path=result.file_path,
                    caption=f"Analytics — {result.period_label}",
                )
                _os.remove(result.file_path)
                return f"✅ Analytics deck ({result.period_label}) sent as PPTX ({result.file_size_bytes} bytes)."
            except Exception as e:
                return f"ERROR generating analytics deck: {e}"

        # generate_invoice_pdf, list_bills_for_customer, get_bills_by_date are shared closures defined above
        return [search_products, get_daily_summary, get_sales_trend,
                get_top_items, get_gst_summary, get_low_stock_items,
                get_bills_by_date, generate_invoice_pdf, generate_analysis_pptx]

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
        - quantity: how many units the owner wants to sell.
        Do NOT pass draft_bill_id — resolved automatically from the active draft.

        QUANTITY RULES — validate BEFORE calling this tool:
          BRANDED items (is_loose=False): quantity MUST be a whole number (1, 2, 3...).
            Fractional quantities (e.g. 1.5) are INVALID — tell owner immediately.
          LOOSE items (is_loose=True):
            - KG, L: fractional quantities allowed (e.g. 0.5 KG, 0.25 L).
            - G, ML, PACKET, PIECE, DOZEN, BUNDLE: whole numbers only.
        If quantity violates these rules, DO NOT call this tool — tell the owner the
        quantity is invalid and ask for a correct whole number.
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
        Remove an entire product line from the active bill draft.
        - product_id: FULL UUID of the product — get it from get_draft_bill() to see
          what is currently in the draft, or from search_products() if not yet retrieved.
          NEVER invent or guess a product_id.
        Do NOT pass draft_bill_id — resolved automatically.

        NOTE: This removes the product completely. To change quantity instead, use
        update_item_quantity(). Only use this tool when the owner wants to remove the
        item entirely from the bill.
        """
        from src.utils.guardrails import clean_uuid
        if not clean_uuid(product_id):
            return f"ERROR: '{product_id}' is not a valid product_id. Call get_draft_bill() to see current items, or search_products() to find the product."
        resolved_id = _draft_id_cell[0]
        if not resolved_id:
            return "ERROR: No active draft bill. Call create_draft_bill() first."
        try:
            result = await mcps.billing.remove_item_from_draft(
                draft_bill_id=resolved_id, product_id=product_id
            )
        except ValueError as e:
            msg = str(e)
            if "not found" in msg.lower():
                return (
                    f"ERROR: product_id '{product_id}' does not exist. "
                    "Call get_draft_bill() to see the correct product_ids in this draft."
                )
            return f"ERROR: {msg}"
        return str(result)

    async def update_item_quantity(product_id: str, quantity: float) -> str:
        """
        Update the quantity of an item already in the active bill draft.
        - product_id: FULL UUID of the product — get it from get_draft_bill() to see
          what is currently in the draft, or from search_products() if not yet retrieved.
          NEVER invent or guess a product_id.
        - quantity: the NEW total quantity (not a delta). E.g. to change 3 KG to 2 KG, pass quantity=2.
        Do NOT pass draft_bill_id — resolved automatically.

        QUANTITY RULES — validate BEFORE calling this tool:
          BRANDED items (is_loose=False): quantity MUST be a whole number (1, 2, 3...).
            Fractional quantities (e.g. 1.5) are INVALID — tell owner immediately.
          LOOSE items (is_loose=True):
            - KG, L: fractional quantities allowed (e.g. 0.5 KG, 0.25 L).
            - G, ML, PACKET, PIECE, DOZEN, BUNDLE: whole numbers only.
        If quantity violates these rules, DO NOT call this tool — tell the owner the
        quantity is invalid and ask for a correct whole number.
        """
        from src.utils.guardrails import clean_uuid
        if not clean_uuid(product_id):
            return f"ERROR: '{product_id}' is not a valid product_id. Call get_draft_bill() to see current items, or search_products() to find the product."
        resolved_id = _draft_id_cell[0]
        if not resolved_id:
            return "ERROR: No active draft bill. Call create_draft_bill() first."
        try:
            result = await mcps.billing.update_item_quantity(
                draft_bill_id=resolved_id, product_id=product_id, new_quantity=quantity
            )
        except ValueError as e:
            msg = str(e)
            if "not found" in msg.lower():
                return (
                    f"ERROR: product_id '{product_id}' does not exist. "
                    "Call get_draft_bill() to see the correct product_ids in this draft."
                )
            return f"ERROR: {msg}"
        return str(result)

    async def get_draft_bill() -> str:
        """Get the current contents and GST-inclusive total of the active bill draft.
        Do NOT pass draft_bill_id — resolved automatically.

        MANDATORY before Stage 2 (payment mode): call this tool to get the accurate
        total_amount (with GST). NEVER quote a total from memory or conversation history
        — always call this tool so the owner sees the correct amount before paying.
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
        Use this tool ONLY for CREDIT sales (payment_mode='CREDIT', is_credit=True, customer_id=<UUID>).
        For CASH or UPI, use finalize_and_pay instead — it handles both one-turn and two-turn flows correctly.
        - payment_mode: CREDIT only when calling this tool directly.
        - is_credit: must be True for credit sales
        - customer_id: required — UUID from get_customer/add_customer
        Do NOT pass draft_bill_id — resolved automatically.
        Do NOT call confirm_payment() after this — credit bills are auto-confirmed.
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
        # Store bill_id so confirm_payment/cancel_bill/void_bill can resolve it
        # automatically — the LLM never needs to pass it.
        _bill_id_cell[0] = result.bill_id

        if is_credit:
            # Credit bills are auto-confirmed inside finalize_bill — no confirm_payment() needed.
            return (
                f"bill_number={result.bill_number}\n"
                f"status=CONFIRMED\n"
                f"total=₹{result.total_amount:.2f} | payment_mode=CREDIT\n"
                f"{result.message}\n"
                f"✅ Bill is CONFIRMED. Do NOT call confirm_payment(). The khata entry has been recorded. "
                f"Inform the owner and ask if they need anything else."
            )
        return (
            f"bill_number={result.bill_number}\n"
            f"status=PENDING_PAYMENT\n"
            f"total=₹{result.total_amount:.2f} | payment_mode={result.payment_mode}\n"
            f"{result.message}\n"
            f"⚠️ STOP — do NOT call confirm_payment, get_customer, or add_payment_entry now.\n"
            f"Show the bill total to the owner and wait for them to confirm payment in their NEXT message."
        )

    async def finalize_and_pay(
        payment_mode: str,
        paid_amount: float | None = None,
        customer_name: str | None = None,
    ) -> str:
        """
        The ONLY tool for finalizing CASH and UPI bills. Always call this immediately
        when the owner states a payment mode of CASH or UPI — never ask questions first.

        - payment_mode: REQUIRED — CASH or UPI only. Use finalize_bill for CREDIT.
        - paid_amount: pass the number if the owner stated it; omit (None) if not stated yet.
        - customer_name: OPTIONAL — pass ONLY if the owner mentioned a customer name or phone
          in the same message (e.g. 'ramesh cash 500', 'upi by priya 200').
          DO NOT ask for a customer name — only use it when the owner volunteers it.

        RULE: Call this tool AS SOON AS the owner says the payment mode — do NOT ask
        for the paid amount first. The tool will handle both cases:
          • Amount given  → 'naveen paid 200 cash', 'cash 500', 'upi 141.96'
              finalize_and_pay(payment_mode='CASH', paid_amount=200)
              Bill CONFIRMED in one shot.
          • Amount NOT given yet → 'cash', 'upi', 'by upi'
              finalize_and_pay(payment_mode='CASH')   ← call immediately, no paid_amount
              Bill created as PENDING_PAYMENT. Tool return message asks owner for amount.
              Then call confirm_payment() after owner states amount next turn.

        CRITICAL: Call this tool immediately — never ask "how much did they pay?" first.
        Do NOT call confirm_payment() in the same turn as finalize_and_pay.
        Do NOT pass draft_bill_id — resolved automatically.
        """
        resolved_id = _draft_id_cell[0]
        if not resolved_id:
            return "ERROR: No active draft bill found. Call create_draft_bill() first."

        # Step 1 — finalize (always)
        result = await mcps.billing.finalize_bill(
            draft_bill_id=resolved_id,
            payment_mode=payment_mode,
            telegram_user_id=tuid,
        )
        bill_id    = result.bill_id
        bill_total = result.total_amount
        bill_num   = result.bill_number
        _bill_id_cell[0] = bill_id

        # Step 2 — if paid_amount is unknown, stop here (two-turn path)
        if paid_amount is None:
            return (
                f"bill_number={bill_num}\n"
                f"status=PENDING_PAYMENT\n"
                f"total=₹{bill_total:.2f} | payment_mode={payment_mode.upper()}\n"
                f"Bill created. Now ask the owner: 'Bill total is ₹{bill_total:.2f}. How much did the customer pay?'\n"
                f"⚠️ STOP — do NOT call any other tool. Wait for the owner to state the amount.\n"
                f"⚠️ CRITICAL: When the owner responds, they MUST give an explicit number (e.g. '500', 'paid 3949').\n"
                f"   If they say only 'paid' with no number → ask again: 'How much did the customer pay?'\n"
                f"   NEVER assume the paid amount equals ₹{bill_total:.2f} — only use what the owner explicitly states.\n"
                f"   Only then call confirm_payment(paid_amount=<the number they gave>)."
            )

        # Step 3 — resolve optional customer (best-effort, never blocks payment)
        resolved_customer_id: str | None = None
        if customer_name:
            try:
                cust_result = await mcps.khata.get_customer(
                    store_id=store_id, name_or_phone=customer_name.strip()
                )
                if cust_result.found and cust_result.customers:
                    resolved_customer_id = cust_result.customers[0].customer_id
            except Exception:
                pass

        # Step 4 — confirm immediately (one-turn path)
        confirm_result = await mcps.billing.confirm_payment(bill_id=bill_id)
        if confirm_result.success:
            _last_confirmed_bill_cell[0] = bill_id
            _bill_id_cell[0] = None

        # Step 5 — classify payment type and record payment row
        diff = round(paid_amount - bill_total, 2)
        subtotal   = result.subtotal
        total_gst  = round(result.total_cgst + result.total_sgst, 2)

        if diff == 0:
            # EXACT — insert payment row immediately, then optionally link customer
            await mcps.payments.record_payment(
                store_id=store_id,
                bill_id=bill_id,
                bill_number=bill_num,
                customer_id=resolved_customer_id,
                paid_amount=paid_amount,
                payment_mode=payment_mode.upper(),
                payment_type="EXACT",
                payment_status="CONFIRMED",
                subtotal=subtotal,
                total_gst=total_gst,
                bill_amount=bill_total,
                change_amount=0.0,
                balance_due=0.0,
            )
            if resolved_customer_id:
                try:
                    await mcps.billing.link_bill_customer(
                        bill_id=bill_id, customer_id=resolved_customer_id
                    )
                except Exception:
                    pass
            # Clear any stale pending intent
            redis = _get_redis()
            if redis:
                await redis.clear_pending_payment(tuid)
            customer_note = f" | customer={customer_name}" if customer_name and resolved_customer_id else ""
            return (
                f"bill_number={bill_num} | total=₹{bill_total:.2f} | paid=₹{paid_amount:.2f}\n"
                f"✅ CONFIRMED. Exact payment received. Bill settled.{customer_note}\n"
                f"⚠️ STOP. Do NOT call any other tool. Inform the owner and ask what's next."
            )
        elif diff > 0:
            # OVERPAYMENT — store delta in Redis, payment row inserted in resolution turn
            change_amount = diff
            redis = _get_redis()
            if redis:
                await redis.set_pending_payment(tuid, {
                    "bill_id": bill_id,
                    "bill_number": bill_num,
                    "bill_amount": bill_total,
                    "paid_amount": paid_amount,
                    "payment_mode": payment_mode.upper(),
                    "payment_reference": None,
                    "intent_type": "OVERPAYMENT",
                    "delta_amount": change_amount,
                    "store_id": store_id,
                    "subtotal": subtotal,
                    "total_gst": total_gst,
                })
            return (
                f"bill_number={bill_num} | total=₹{bill_total:.2f} | paid=₹{paid_amount:.2f}\n"
                f"✅ CONFIRMED. OVERPAYMENT — change = ₹{change_amount:.2f}\n"
                f"⚠️ STOP HERE. Do NOT call get_customer, add_customer, or add_payment_entry this turn.\n"
                f"⚠️ Do NOT reuse any customer from conversation history — this is a new independent bill.\n"
                f"Ask the owner: 'Change is ₹{change_amount:.2f}. Return as cash, or add to customer's khata?'\n"
                f"ONLY in the NEXT turn (after owner answers + gives customer name):\n"
                f"  call get_customer(name) → add_payment_entry(customer_id, amount={change_amount:.2f}, "
                f"notes='Overpayment from bill {bill_num}')."
            )
        else:  # diff < 0
            # UNDERPAYMENT — store balance in Redis, payment row inserted in resolution turn
            balance = round(-diff, 2)
            redis = _get_redis()
            if redis:
                await redis.set_pending_payment(tuid, {
                    "bill_id": bill_id,
                    "bill_number": bill_num,
                    "bill_amount": bill_total,
                    "paid_amount": paid_amount,
                    "payment_mode": payment_mode.upper(),
                    "payment_reference": None,
                    "intent_type": "UNDERPAYMENT",
                    "delta_amount": balance,
                    "store_id": store_id,
                    "subtotal": subtotal,
                    "total_gst": total_gst,
                })
            return (
                f"bill_number={bill_num} | total=₹{bill_total:.2f} | paid=₹{paid_amount:.2f}\n"
                f"✅ CONFIRMED. UNDERPAYMENT — remaining balance = ₹{balance:.2f}\n"
                f"⚠️ STOP HERE. Do NOT call get_customer, add_customer, or add_credit_entry this turn.\n"
                f"⚠️ Do NOT reuse any customer from conversation history — ask the owner explicitly.\n"
                f"Tell the owner: 'Received ₹{paid_amount:.2f}. Remaining ₹{balance:.2f} must go to Khata.\n"
                f"Please provide customer name and 10-digit mobile.'\n"
                f"ONLY in the NEXT turn (after owner gives name + phone):\n"
                f"  call get_customer(phone) or add_customer(name, phone) → "
                f"add_credit_entry(customer_id, amount={balance:.2f}, "
                f"notes='Remaining balance for bill {bill_num}')."
            )

    async def cancel_draft_bill() -> str:
        """Cancel the current active OPEN draft bill and discard all items.

        MANDATORY: Call this tool IMMEDIATELY when the owner says 'cancel', 'stop',
        'discard', 'start over', or 'wrong bill' while a draft is still OPEN
        (i.e. before finalize_and_pay / finalize_bill has been called).

        Do NOT respond with text saying 'bill cancelled' — you MUST call this tool
        or the draft will remain open in the database.
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

    async def collect_balance_now() -> str:
        """
        Resolve a pending UNDERPAYMENT or OVERPAYMENT by treating the bill as fully settled.

        USE WHEN the owner says any of:
          - 'will collect now', 'collect now', 'paying now', 'pay balance now',
            'customer is paying', 'got it', 'collected', 'received full amount'
          After confirm_payment() detected UNDERPAYMENT and the customer will pay the balance immediately.
          After confirm_payment() detected OVERPAYMENT and the owner will return change as cash (no khata).

        WHAT THIS DOES:
          - UNDERPAYMENT: records the FULL bill amount as EXACT payment (paid_amount = bill_total),
            confirms the bill as CONFIRMED, clears Redis. No khata entry.
          - OVERPAYMENT:  records the FULL paid_amount as EXACT payment (change returned as cash),
            confirms the bill as CONFIRMED, clears Redis. No khata entry.

        Do NOT pass any arguments — all values read from Redis intent.
        Do NOT call confirm_payment() before or after this.
        """
        redis = _get_redis()
        intent = None
        if redis:
            intent = await redis.get_pending_payment(tuid)

        if not intent:
            return "ERROR: No pending over/underpayment found. Nothing to resolve."

        intent_type = intent.get("intent_type")
        if intent_type not in ("UNDERPAYMENT", "OVERPAYMENT"):
            return f"ERROR: Pending intent is '{intent_type}', not an over/underpayment. Cannot use collect_balance_now()."

        bill_id    = intent["bill_id"]
        bill_num   = intent["bill_number"]
        bill_total = float(intent["bill_amount"])
        paid       = float(intent["paid_amount"])
        mode       = intent["payment_mode"]
        subtotal   = float(intent.get("subtotal") or 0) or None
        total_gst  = float(intent.get("total_gst") or 0) or None
        resolved_customer_id = intent.get("customer_id")

        # Confirm bill in DB (still PENDING_PAYMENT at this point)
        confirm_result = await mcps.billing.confirm_payment(bill_id=bill_id)
        if not confirm_result.success:
            return f"ERROR confirming bill: {confirm_result.message}"
        _last_confirmed_bill_cell[0] = bill_id
        _bill_id_cell[0] = None

        if intent_type == "UNDERPAYMENT":
            # Treat as EXACT — full bill total collected across two physical payments
            await mcps.payments.record_payment(
                store_id=store_id,
                bill_id=bill_id,
                bill_number=bill_num,
                customer_id=resolved_customer_id,
                paid_amount=bill_total,   # full amount (₹200 + ₹55.60 = ₹255.60)
                payment_mode=mode,
                payment_type="EXACT",
                payment_status="CONFIRMED",
                subtotal=subtotal,
                total_gst=total_gst,
                bill_amount=bill_total,
                change_amount=0.0,
                balance_due=0.0,
            )
            if redis:
                await redis.clear_pending_payment(tuid)
            return (
                f"✅ Balance collected. Bill {bill_num} fully settled.\n"
                f"Total = ₹{bill_total:.2f} | Received in two parts: ₹{paid:.2f} + ₹{bill_total - paid:.2f}\n"
                f"⚠️ STOP. Do NOT call any other tool. Inform the owner and ask what's next."
            )
        else:  # OVERPAYMENT — change returned as cash
            change = float(intent["delta_amount"])
            await mcps.payments.record_payment(
                store_id=store_id,
                bill_id=bill_id,
                bill_number=bill_num,
                customer_id=resolved_customer_id,
                paid_amount=paid,
                payment_mode=mode,
                payment_type="EXACT",
                payment_status="CONFIRMED",
                subtotal=subtotal,
                total_gst=total_gst,
                bill_amount=bill_total,
                change_amount=change,
                balance_due=0.0,
            )
            if redis:
                await redis.clear_pending_payment(tuid)
            return (
                f"✅ Change returned. Bill {bill_num} settled.\n"
                f"Total = ₹{bill_total:.2f} | Paid = ₹{paid:.2f} | Change returned = ₹{change:.2f}\n"
                f"⚠️ STOP. Do NOT call any other tool. Inform the owner and ask what's next."
            )

    async def confirm_payment(paid_amount: float, customer_name: str | None = None) -> str:
        """
        Confirm payment received for the current PENDING_PAYMENT bill.

        ══════════════════════════════════════════════════════════════
        GOLDEN RULE: THIS MUST ALWAYS BE A REAL TOOL CALL.
        When a bill is PENDING_PAYMENT and the owner states an amount
        paid — with OR without a customer name — call this tool.
        NEVER respond with text like "added to khata" or "confirmed"
        without calling confirm_payment first.
        ══════════════════════════════════════════════════════════════

        WHEN TO CALL — any of these patterns:
          - 'paid 500'
          - 'naveen paid 40'       ← name + amount → call confirm_payment(paid_amount=40, customer_name='naveen')
          - 'customer gave 200'
          - '3949 cash'
          - 'full amount'          ← only if a specific number was stated earlier this turn; else ask

        PARAMETERS:
          - paid_amount: REQUIRED — exact rupee amount the customer physically handed over.
            CRITICAL RULES:
              1. NEVER invent or assume paid_amount from bill total or conversation history.
              2. If owner says ONLY 'paid' with NO number → ask 'How much did the customer pay?' and WAIT.
                 Do NOT call this tool until the owner gives an explicit number.
              3. Do NOT default paid_amount to the bill total — use ONLY what the owner stated.
          - customer_name: OPTIONAL — pass ONLY if the owner mentioned a customer name or phone in
            the same message (e.g. 'ramesh paid 500', 'naveen paid 40', 'paid by 9876543210').
            DO NOT ask for a customer name — only use it when the owner volunteers it.
            DO NOT call get_customer separately — pass the name here and it is resolved internally.

        This tool:
          1. Confirms the bill (PENDING_PAYMENT → CONFIRMED)
          2. Records the payment in payments.payments (with customer_id if resolved)
          3. Links the customer to the bill if customer_name was provided
          4. Detects EXACT / OVERPAYMENT / UNDERPAYMENT automatically
          5. For over/underpayment: stores the delta in Redis so you never have to remember it

        Do NOT pass bill_id — resolved automatically.
        Do NOT call any other tool in the same turn after this.
        """
        resolved_bill_id = _bill_id_cell[0]
        if not resolved_bill_id:
            return "ERROR: No PENDING_PAYMENT bill found. Nothing to confirm."

        # Load bill details (needed for snapshot + payment type detection)
        bill = await mcps.billing.get_bill_for_payment(resolved_bill_id)
        if not bill:
            return "ERROR: Bill not found. Cannot confirm payment."

        bill_total  = float(bill["total_amount"])
        bill_num    = bill["bill_number"]
        bill_mode   = bill["payment_mode"]
        subtotal    = float(bill["subtotal"])
        total_gst   = round(float(bill.get("total_cgst", 0)) + float(bill.get("total_sgst", 0)), 2)

        # Resolve customer_id if caller supplied a name/phone — do this BEFORE confirming
        # so the payment row and bill record can be linked in a single operation.
        # Falls back to whatever customer_id is already on the bill (e.g. from a prior
        # add_customer call). Never raises — customer resolution is best-effort.
        resolved_customer_id: str | None = bill.get("customer_id")
        if customer_name and not resolved_customer_id:
            try:
                cust_result = await mcps.khata.get_customer(
                    store_id=store_id, name_or_phone=customer_name.strip()
                )
                if cust_result.found and cust_result.customers:
                    resolved_customer_id = cust_result.customers[0].customer_id
            except Exception:
                pass  # customer resolution is optional — never block payment confirmation

        # Step 2 — classify payment BEFORE confirming the bill in DB.
        # The bill stays PENDING_PAYMENT until we know the full resolution
        # (exact / overpayment-return-change / overpayment-to-khata /
        #  underpayment-collect-now / underpayment-to-khata).
        # Only EXACT payment confirms immediately here; over/under store
        # Redis intent and confirm only when the owner resolves them.
        diff = round(paid_amount - bill_total, 2)

        if diff == 0:
            # EXACT — confirm the bill now, record payment, done.
            confirm_result = await mcps.billing.confirm_payment(bill_id=resolved_bill_id)
            if not confirm_result.success:
                return f"ERROR confirming bill: {confirm_result.message}"
            _last_confirmed_bill_cell[0] = resolved_bill_id
            _bill_id_cell[0] = None

            await mcps.payments.record_payment(
                store_id=store_id,
                bill_id=resolved_bill_id,
                bill_number=bill_num,
                customer_id=resolved_customer_id,
                paid_amount=paid_amount,
                payment_mode=bill_mode,
                payment_type="EXACT",
                payment_status="CONFIRMED",
                subtotal=subtotal,
                total_gst=total_gst,
                bill_amount=bill_total,
                change_amount=0.0,
                balance_due=0.0,
            )
            if resolved_customer_id:
                try:
                    await mcps.billing.link_bill_customer(
                        bill_id=resolved_bill_id, customer_id=resolved_customer_id
                    )
                except Exception:
                    pass
            redis = _get_redis()
            if redis:
                await redis.clear_pending_payment(tuid)
            customer_note = f" | customer={customer_name}" if customer_name and resolved_customer_id else ""
            return (
                f"✅ Payment confirmed! Bill {bill_num} paid in full.\n"
                f"paid=₹{paid_amount:.2f} | total=₹{bill_total:.2f} | EXACT{customer_note}\n"
                f"⚠️ STOP. Do NOT call any other tool. Inform the owner and ask what's next."
            )

        elif diff > 0:
            # OVERPAYMENT — bill stays PENDING_PAYMENT until owner decides what to do with change.
            # confirm + payment row recorded only when owner resolves (return cash → confirm here,
            # add to khata → add_payment_entry confirms + records).
            change_amount = diff
            redis = _get_redis()
            if redis:
                await redis.set_pending_payment(tuid, {
                    "bill_id": resolved_bill_id,
                    "bill_number": bill_num,
                    "bill_amount": bill_total,
                    "paid_amount": paid_amount,
                    "payment_mode": bill_mode,
                    "payment_reference": bill.get("payment_reference"),
                    "intent_type": "OVERPAYMENT",
                    "delta_amount": change_amount,
                    "store_id": store_id,
                    "subtotal": subtotal,
                    "total_gst": total_gst,
                    "customer_id": resolved_customer_id,
                })
            # Bill stays PENDING_PAYMENT — do NOT confirm yet.
            return (
                f"₹{paid_amount:.2f} received for Bill {bill_num} (total ₹{bill_total:.2f}).\n"
                f"OVERPAYMENT — change = ₹{change_amount:.2f}\n"
                f"⚠️ STOP HERE. Ask the owner exactly this:\n"
                f"'₹{change_amount:.2f} extra received. Return change to customer as cash, "
                f"or add to their khata account?'\n"
                f"⚠️ Do NOT call any other tool until owner answers.\n"
                f"  • Return cash → call collect_balance_now() to confirm bill as EXACT.\n"
                f"  • Add to khata → get_customer(name) then add_payment_entry(customer_id)."
            )

        else:
            # UNDERPAYMENT — bill stays PENDING_PAYMENT until owner decides.
            # confirm + payment row recorded only when owner resolves
            # (collect now → collect_balance_now(), to khata → add_credit_entry()).
            balance_due = round(-diff, 2)
            redis = _get_redis()
            if redis:
                await redis.set_pending_payment(tuid, {
                    "bill_id": resolved_bill_id,
                    "bill_number": bill_num,
                    "bill_amount": bill_total,
                    "paid_amount": paid_amount,
                    "payment_mode": bill_mode,
                    "payment_reference": bill.get("payment_reference"),
                    "intent_type": "UNDERPAYMENT",
                    "delta_amount": balance_due,
                    "store_id": store_id,
                    "subtotal": subtotal,
                    "total_gst": total_gst,
                    "customer_id": resolved_customer_id,
                })
            # Bill stays PENDING_PAYMENT — do NOT confirm yet.
            return (
                f"₹{paid_amount:.2f} received for Bill {bill_num} (total ₹{bill_total:.2f}).\n"
                f"UNDERPAYMENT — remaining balance = ₹{balance_due:.2f}\n"
                f"⚠️ STOP HERE. Ask the owner exactly this:\n"
                f"'₹{balance_due:.2f} is still due. Will the customer pay the balance now, "
                f"or should I add it to their khata?'\n"
                f"⚠️ Do NOT call any other tool until owner answers.\n"
                f"  • Collect now → call collect_balance_now() — treats full ₹{bill_total:.2f} as EXACT.\n"
                f"  • Add to khata → get_customer(name) then add_credit_entry(customer_id)."
            )

    async def change_payment_mode(new_payment_mode: str) -> str:
        """
        Change the payment mode of the current PENDING_PAYMENT bill.
        Use when owner says 'change to cash', 'change to upi', 'use cash instead', etc.
        - new_payment_mode: CASH or UPI (CREDIT not supported here — use cancel_bill + new bill for credit).
        Cancels the current bill, rebuilds it with all the same items, and re-finalizes
        with the new payment mode. Returns the new bill number and total.
        Do NOT pass bill_id — resolved automatically.
        """
        resolved_bill_id = _bill_id_cell[0]
        if not resolved_bill_id:
            return "ERROR: No PENDING_PAYMENT bill found to change payment mode for."
        result = await mcps.billing.change_payment_mode(
            bill_id=resolved_bill_id,
            new_payment_mode=new_payment_mode,
            telegram_user_id=tuid,
        )
        # Update cell to the new bill_id
        _bill_id_cell[0] = result.bill_id
        return (
            f"✅ Payment mode changed to {new_payment_mode.upper()}.\n"
            f"bill_number={result.bill_number}\n"
            f"status=PENDING_PAYMENT\n"
            f"total=₹{result.total_amount:.2f} | payment_mode={result.payment_mode}\n"
            f"Bill recreated with all original items. Now ask: 'Bill total is ₹{result.total_amount:.2f}. How much did the customer pay?'\n"
            f"⚠️ STOP — wait for the owner to state the paid amount, then call confirm_payment()."
        )

    async def cancel_bill() -> str:
        """
        Cancel the current PENDING_PAYMENT bill before payment is confirmed.
        MANDATORY: Call this tool immediately when owner says 'cancel', 'wrong items', or 'start over' AFTER finalize_bill.
        Restores all stock back to inventory and reverses any khata entry.
        Do NOT pass bill_id — resolved automatically from the server.
        DO NOT say a bill cannot be cancelled — PENDING_PAYMENT bills CAN be cancelled via cancel_bill().
        """
        resolved_bill_id = _bill_id_cell[0]
        if not resolved_bill_id:
            return "ERROR: No PENDING_PAYMENT bill found. Nothing to cancel."
        result = await mcps.billing.cancel_bill(bill_id=resolved_bill_id)
        if result.success:
            _bill_id_cell[0] = None
        return result.message

    async def void_bill() -> str:
        """
        Void the most recently CONFIRMED bill — full reversal after payment was confirmed.
        Restores all stock and reverses the payment/khata entry.
        Use when owner says 'cancel', 'undo', or 'wrong' AFTER confirm_payment.
        Do NOT pass bill_id — resolved automatically from the server.
        DO NOT use this on PENDING_PAYMENT bills — use cancel_bill instead.
        """
        # void targets the most recent CONFIRMED bill for this store
        try:
            _vb_resp = (
                mcps.billing.db.schema("billing")
                .table("bills")
                .select("id")
                .eq("store_id", store_id)
                .eq("status", "CONFIRMED")
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            _vb_rows = _vb_resp.data or []
            resolved_bill_id = _vb_rows[0]["id"] if _vb_rows else None
        except Exception:
            resolved_bill_id = None
        if not resolved_bill_id:
            return "ERROR: No CONFIRMED bill found to void."
        result = await mcps.billing.void_bill(bill_id=resolved_bill_id)
        return result.message

    async def void_bill_by_number(bill_number_or_id: str) -> str:
        """
        Void a specific CONFIRMED bill by its bill number or UUID — full reversal after payment.
        USE WHEN: owner names a bill explicitly, e.g. 'cancel BL-003-20260815-009',
        'undo bill BL-003-...', 'reverse bill <number>'.
        - bill_number_or_id: the human bill number (e.g. BL-003-20260815-009) OR a bill UUID.
          Resolves to the bill UUID internally — never ask the owner for a UUID.
        DIFFERENT from void_bill() which targets only the most recently confirmed bill.
        Restores stock and reverses payment/khata entries.
        """
        from src.utils.guardrails import clean_uuid as _clean_uuid

        resolved_bill_id: str | None = None
        if _clean_uuid(bill_number_or_id):
            resolved_bill_id = bill_number_or_id
        else:
            try:
                resp = (
                    mcps.billing.db.schema("billing")
                    .table("bills")
                    .select("id")
                    .eq("store_id", store_id)
                    .eq("bill_number", bill_number_or_id.strip())
                    .limit(1)
                    .execute()
                )
                rows = resp.data or []
                if rows:
                    resolved_bill_id = rows[0]["id"]
            except Exception:
                pass

        if not resolved_bill_id:
            return (
                f"ERROR: Could not find bill '{bill_number_or_id}'. "
                "Use get_bills_by_date() to find the correct bill number."
            )

        result = await mcps.billing.void_bill(bill_id=resolved_bill_id)
        return result.message

    async def add_customer(name: str, phone: str) -> str:
        """
        Add a new customer. Call this ONLY when get_customer confirmed the customer does not exist yet.
        ALWAYS call get_customer(name_or_phone) first — only call add_customer if the customer is not found.
        phone is MANDATORY (10-digit Indian mobile number). Never call with a placeholder phone.
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

    async def add_credit_entry(customer_id: str, amount: float | None = None, notes: str | None = None) -> str:
        """
        Record that a customer owes the shop money (underpayment balance or standalone credit).

        USE FOR:
          - Underpayment resolution: after confirm_payment detected underpayment and owner chose 'add to khata'
            → pass amount=None; the balance is read from Redis (prevents LLM hallucination)
          - Standalone credit advance: owner gives goods on credit outside a billing session
            → pass the explicit amount

        DO NOT USE FOR overpayments — use add_payment_entry for those.
        customer_id MUST be a valid UUID from get_customer/add_customer.

        UNDERPAYMENT FLOW (most common):
          1. confirm_payment(paid_amount=X) detected underpayment → balance stored in Redis
          2. Owner said 'add to khata' + provided customer name
          3. get_customer(name) → got customer_id
          4. add_credit_entry(customer_id=<uuid>) ← amount=None reads from Redis
        """
        from src.utils.guardrails import clean_uuid
        if not clean_uuid(customer_id):
            return "ERROR: customer_id must be a valid UUID. Call get_customer() or add_customer() first."

        redis = _get_redis()
        intent = None
        if redis:
            intent = await redis.get_pending_payment(tuid)

        # Detect whether confirm_payment was bypassed:
        # If there is a PENDING_PAYMENT bill in _bill_id_cell but no Redis intent,
        # the model skipped confirm_payment and jumped straight here.
        # In that case we must confirm the bill first, then treat this as an underpayment.
        bypassed_confirm = (
            amount is not None          # explicit amount from LLM (not from Redis)
            and intent is None          # no Redis intent = confirm_payment was not called
            and _bill_id_cell[0] is not None  # but there IS a pending bill
        )

        if bypassed_confirm:
            # Self-heal: confirm the PENDING_PAYMENT bill before recording the khata entry
            pending_bill_id = _bill_id_cell[0]
            bill_snap = await mcps.billing.get_bill_for_payment(pending_bill_id)
            if bill_snap and bill_snap.get("status") == "PENDING_PAYMENT":
                confirm_result = await mcps.billing.confirm_payment(bill_id=pending_bill_id)
                if confirm_result.success:
                    _last_confirmed_bill_cell[0] = pending_bill_id
                    _bill_id_cell[0] = None
                    # Synthesise a minimal intent so the payment row is recorded below
                    intent = {
                        "intent_type": "UNDERPAYMENT",
                        "bill_id": pending_bill_id,
                        "bill_number": bill_snap.get("bill_number"),
                        "bill_amount": float(bill_snap.get("total_amount", 0)),
                        "paid_amount": float(bill_snap.get("total_amount", 0)) - float(amount),
                        "payment_mode": bill_snap.get("payment_mode", "CASH"),
                        "subtotal": float(bill_snap.get("subtotal", 0)),
                        "total_gst": round(
                            float(bill_snap.get("total_cgst", 0)) +
                            float(bill_snap.get("total_sgst", 0)), 2
                        ),
                        "delta_amount": float(amount),
                    }

        is_underpayment = (
            intent is not None
            and intent.get("intent_type") == "UNDERPAYMENT"
        )

        if amount is None:
            if is_underpayment:
                resolved_amount = float(intent["delta_amount"])
            else:
                return "ERROR: amount is required when there is no pending underpayment intent."
        else:
            # Model passed an explicit amount — still treat as underpayment resolution if
            # a valid UNDERPAYMENT intent exists in Redis (model compliance issue: should
            # pass amount=None but passed the explicit value instead).
            # Use Redis delta as authoritative; fall back to model-supplied amount only
            # when no intent exists (standalone credit advance).
            resolved_amount = float(intent["delta_amount"]) if is_underpayment else float(amount)

        resolved_ref_bill_id = (
            intent["bill_id"] if intent and intent.get("bill_id")
            else _last_confirmed_bill_cell[0]
        )

        effective_notes = notes
        if resolved_ref_bill_id:
            bill_num = (intent.get("bill_number") if intent
                        else await _resolve_bill_number(mcps, resolved_ref_bill_id))
            if bill_num and (not effective_notes or bill_num not in effective_notes):
                effective_notes = (
                    f"Remaining balance for bill {bill_num}"
                    + (f" — {effective_notes}" if effective_notes else "")
                )

        khata_result = await mcps.khata.add_credit_entry(
            store_id=store_id, customer_id=customer_id,
            amount=resolved_amount, reference_bill_id=resolved_ref_bill_id,
            notes=effective_notes
        )

        # For UNDERPAYMENT resolution: bill is still PENDING_PAYMENT (we deferred
        # confirmation until now). Confirm it, then record the payment row.
        if intent and intent.get("intent_type") == "UNDERPAYMENT":
            # Confirm the bill in DB now that resolution is decided
            _bill_to_confirm = intent.get("bill_id") or resolved_ref_bill_id
            if _bill_to_confirm:
                _cr = await mcps.billing.confirm_payment(bill_id=_bill_to_confirm)
                if _cr.success:
                    _last_confirmed_bill_cell[0] = _bill_to_confirm
                    _bill_id_cell[0] = None
            await mcps.payments.record_payment(
                store_id=store_id,
                bill_id=resolved_ref_bill_id,
                bill_number=intent.get("bill_number"),
                customer_id=customer_id,
                khata_entry_id=khata_result.entry_id,
                paid_amount=float(intent["paid_amount"]),
                payment_mode=intent["payment_mode"],
                payment_type="UNDERPAYMENT",
                payment_status="CONFIRMED",
                subtotal=float(intent.get("subtotal") or 0) or None,
                total_gst=float(intent.get("total_gst") or 0) or None,
                bill_amount=float(intent["bill_amount"]),
                change_amount=0.0,
                balance_due=resolved_amount,
            )
            if redis:
                await redis.clear_pending_payment(tuid)

        if resolved_ref_bill_id:
            try:
                await mcps.billing.link_bill_customer(
                    bill_id=resolved_ref_bill_id, customer_id=customer_id
                )
            except Exception:
                pass
        return str(khata_result)

    async def add_payment_entry(customer_id: str, amount: float | None = None, notes: str | None = None) -> str:
        """
        Record money received from a customer — reduces what they owe or stores credit in their favour.

        USE FOR:
          - Overpayment resolution: after confirm_payment detected overpayment and owner chose 'add to khata'
            → pass amount=None; the change amount is read from Redis (prevents LLM hallucination)
          - Standalone khata settlement: customer pays off their outstanding balance (no bill)
            → pass the explicit amount

        DO NOT USE FOR underpayment balance — use add_credit_entry for that.
        customer_id MUST be a valid UUID from get_customer/add_customer.

        OVERPAYMENT FLOW (most common):
          1. confirm_payment(paid_amount=X) detected overpayment → change stored in Redis
          2. Owner said 'add to khata' + provided customer name
          3. get_customer(name) → got customer_id
          4. add_payment_entry(customer_id=<uuid>) ← amount=None reads from Redis

        STANDALONE KHATA SETTLE:
          Owner: 'Ramesh paid ₹300 against his khata'
          → get_customer('Ramesh') → add_payment_entry(customer_id=<uuid>, amount=300)
        """
        from src.utils.guardrails import clean_uuid
        if not clean_uuid(customer_id):
            return "ERROR: customer_id must be a valid UUID. Call get_customer() or add_customer() first."

        redis = _get_redis()
        intent = None
        if redis:
            intent = await redis.get_pending_payment(tuid)

        is_overpayment = (
            intent is not None
            and intent.get("intent_type") == "OVERPAYMENT"
        )

        if amount is None:
            if is_overpayment:
                resolved_amount = float(intent["delta_amount"])
            else:
                return "ERROR: amount is required when there is no pending overpayment intent."
        else:
            # Model passed an explicit amount — use it, but still treat as overpayment
            # if a valid OVERPAYMENT intent exists in Redis. This handles the case where
            # the model passes the amount explicitly instead of None (model compliance issue)
            # rather than reading it from Redis. The Redis delta is the authoritative amount;
            # the model-supplied value is accepted as a fallback only when Redis has no intent.
            resolved_amount = float(intent["delta_amount"]) if is_overpayment else float(amount)

        resolved_ref_bill_id = (
            intent["bill_id"] if is_overpayment and intent and intent.get("bill_id")
            else None
        )
        payment_type = "OVERPAYMENT" if is_overpayment else "KHATA_SETTLE"

        effective_notes = notes
        if resolved_ref_bill_id:
            bill_num = (intent.get("bill_number") if intent
                        else await _resolve_bill_number(mcps, resolved_ref_bill_id))
            if bill_num and (not effective_notes or bill_num not in effective_notes):
                effective_notes = (
                    f"Overpayment surplus from bill {bill_num}"
                    + (f" — {effective_notes}" if effective_notes else "")
                )

        khata_result = await mcps.khata.add_payment_entry(
            store_id=store_id, customer_id=customer_id,
            amount=resolved_amount, reference_bill_id=resolved_ref_bill_id,
            notes=effective_notes
        )

        if is_overpayment and intent:
            # Bill is still PENDING_PAYMENT — confirm it now that resolution is decided.
            _bill_to_confirm = intent.get("bill_id") or resolved_ref_bill_id
            if _bill_to_confirm:
                _cr = await mcps.billing.confirm_payment(bill_id=_bill_to_confirm)
                if _cr.success:
                    _last_confirmed_bill_cell[0] = _bill_to_confirm
                    _bill_id_cell[0] = None
            await mcps.payments.record_payment(
                store_id=store_id,
                bill_id=resolved_ref_bill_id,
                bill_number=intent.get("bill_number"),
                customer_id=customer_id,
                khata_entry_id=khata_result.entry_id,
                paid_amount=float(intent["paid_amount"]),
                payment_mode=intent["payment_mode"],
                payment_type="OVERPAYMENT",
                payment_status="CONFIRMED",
                subtotal=float(intent.get("subtotal") or 0) or None,
                total_gst=float(intent.get("total_gst") or 0) or None,
                bill_amount=float(intent["bill_amount"]),
                change_amount=resolved_amount,
                balance_due=0.0,
            )
            if redis:
                await redis.clear_pending_payment(tuid)
        else:
            # Standalone KHATA_SETTLE — no bill
            await mcps.payments.record_payment(
                store_id=store_id,
                bill_id=None,
                customer_id=customer_id,
                khata_entry_id=khata_result.entry_id,
                paid_amount=resolved_amount,
                payment_mode="CASH",
                payment_type="KHATA_SETTLE",
                payment_status="CONFIRMED",
                change_amount=0.0,
                balance_due=0.0,
            )

        if resolved_ref_bill_id:
            try:
                await mcps.billing.link_bill_customer(
                    bill_id=resolved_ref_bill_id, customer_id=customer_id
                )
            except Exception:
                pass
        return khata_result.message

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
    if intent == "BILLING_CONFIRM":
        return [
            search_products,
            collect_balance_now, confirm_payment, cancel_bill, void_bill, void_bill_by_number, change_payment_mode,
            get_bill,
            add_customer, get_customer, add_credit_entry, add_payment_entry,
            get_payment_history, list_bills_for_customer, get_bills_by_date, generate_invoice_pdf,
        ]

    # ── BILLING (default) — draft-building tools ──────────────────────────────
    return [
        search_products, list_products, check_availability,
        create_draft_bill, add_item_to_draft, remove_item_from_draft,
        update_item_quantity, get_draft_bill, finalize_and_pay, finalize_bill,
        cancel_draft_bill, change_payment_mode,
        add_customer, get_customer, add_credit_entry, add_payment_entry,
        add_product, receive_stock,
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Redis helper — lazy singleton for payment intent storage
# ─────────────────────────────────────────────────────────────────────────────

def _get_redis():
    """
    Return an UpstashRedisClient for pending payment intent storage.
    Returns None silently on any failure — payment recording degrades gracefully.
    """
    try:
        from src.redis.upstash_client import UpstashRedisClient
        return UpstashRedisClient()
    except Exception:
        return None


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
        "confirm payment", "confirm the payment", "payment confirmed", "payment done",
        "void bill", "void the bill", "cancel bill", "cancel the bill", "cancel",
        "undo bill", "reverse bill", "undo", "void", "payment received",
        "paid", "yes paid", "payment done",
        # Post-payment underpayment / overpayment resolution phrases.
        # These MUST route to BILLING_CONFIRM (not KHATA) so the Redis-aware
        # add_credit_entry / add_payment_entry with payment recording is used.
        "add to khata", "save to khata", "put in khata", "add in khata",
        "save in khata", "store in khata", "record in khata",
        "add to their khata", "put it in khata", "add balance to khata",
        "return change", "give change", "return the change",
        # Bill lookup — list_bills_for_customer and generate_invoice_pdf are in this group
        "list all bill", "list bill", "show bill", "bills for",
        "all bill", "bill history", "purchase history", "purchases for",
        "bought by", "list purchase",
        # Invoice / PDF generation
        "send invoice", "generate invoice", "invoice for",
        "generate bill", "print bill", "share bill", "bill pdf",
        "send bill", "pdf for", "pdf of", "invoice pdf",
        "get bill", "fetch bill", "show bill details", "bill details",
    ],
    "CATALOGUE":  [
        "catalogue", "add product", "add item", "new product", "new item",
        "brand", "gst rate", "hsn", "mrp", "cost price", "reorder level",
        "edit product", "update product", "remove product",
    ],
    "INVENTORY":  [
        "stock", "inventory", "restock", "low stock", "out of stock",
        "stock movement", "stock history", "movement history",
        "track movement", "track movements", "movements",
        "received stock", "add stock", "how much stock",
        "units left", "units available", "in stock",
        "full report", "all stock", "stock report",
    ],
    "KHATA":      [
        "khata", "udhar", "customer balance",
        "customer payment", "owes", "due", "settle", "pay back",
        "how much does", "customer owes", "add customer",
        "gave money", "returned money",
        "overpayment", "extra amount", "paid extra", "paid more", "save it",
        "save in khata", "credit balance", "keep it",
        "payment history", "payment record", "all bills", "billing history",
        "what has paid", "how much paid", "transaction history",
    ],
    "ANALYTICS":  [
        "report", "daily summary", "sales summary", "sales trend",
        "top items", "top selling", "analytics", "pdf", "pptx",
        "close day", "revenue", "gst report", "gst summary",
        "invoice", "generate invoice", "send invoice", "list bills",
        "bills today", "bills for today", "bills on", "show bills",
        "weekly report", "monthly report", "analysis deck",
    ],
}

# These billing-mode words must never redirect away from BILLING
# even if they also appear in KHATA keywords by mistake.
_BILLING_PAYMENT_WORDS = frozenset({
    "cash", "upi", "credit", "card", "finalize", "finalise",
    "confirm bill", "confirm the bill", "complete bill", "done",
})

_PAID_PATTERN = _re.compile(r"\bpaid\b.*?\d|[\w]+ paid\b", _re.IGNORECASE)


# Phrases the agent uses when asking the owner to name a product for an inventory action.
# If the previous assistant turn contained any of these, a bare product-name follow-up
# should stay in INVENTORY rather than falling back to BILLING.
#
# IMPORTANT: These must be question-style phrases that only appear when the agent is
# actively asking for a product — NOT column headers or table labels that appear in
# formatted inventory output. "product name" and "name of the item" were removed because
# they appear as table column headers (e.g. "| Product Name | Brand | ...") and caused
# false-positive INVENTORY routing after any inventory list response.
_INVENTORY_FOLLOWUP_PHRASES = (
    "stock history",
    "movement history",
    "track movement",
    "which product would you like",
    "which item would you like",
    "product would you like to",
    "item would you like to",
    "see the stock",
    "check stock for",
    "which product do you",
    "which item do you",
    "for which product",
    "for which item",
)


def detect_intent(
    user_message: str,
    has_active_draft: bool = False,
    last_assistant_msg: str | None = None,
) -> str:
    """
    Lightweight keyword-based intent detector.
    Falls back to BILLING (the most common daily task).

    If has_active_draft=True (an open draft bill exists), payment-mode words
    like 'credit', 'cash', 'upi' always route to BILLING so finalize_bill
    is available — never to KHATA.

    BILLING_CONFIRM is only reachable when no draft is active — it handles
    confirm_payment / cancel_bill / void_bill after finalize_bill is done.

    last_assistant_msg: when provided, used to keep follow-up single-word
    replies in the same intent as the previous turn (e.g. product name
    after "which product would you like to check?").
    """
    msg = user_message.lower()

    # Context-aware: if the previous assistant turn was asking for a product name
    # in an INVENTORY context, treat the follow-up as INVENTORY regardless of
    # whether the user's reply contains any INVENTORY keywords.
    if last_assistant_msg:
        last_lower = last_assistant_msg.lower()
        if any(p in last_lower for p in _INVENTORY_FOLLOWUP_PHRASES):
            return "INVENTORY"

    # If a draft bill is active, payment-mode words mean "pay this bill" →
    # always BILLING (finalize_and_pay is there), not KHATA or BILLING_CONFIRM.
    # This also covers "31.5 paid", "paid 50", "cash 200" mid-session.
    if has_active_draft and (
        any(w in msg for w in _BILLING_PAYMENT_WORDS)
        or _PAID_PATTERN.search(user_message)
        or "paid" in msg
    ):
        return "BILLING"

    # BILLING_CONFIRM handles two kinds of requests:
    #   (a) Payment/cancellation actions — only when no draft is active
    #       (so "paid", "cancel", "void" mid-draft stay in BILLING)
    #   (b) Bill lookup/PDF actions — safe to route even with a draft active
    #       (looking up a past bill doesn't touch the current draft)
    _BILLING_CONFIRM_LOOKUP_KEYWORDS = frozenset({
        "list all bill", "list bill", "show bill", "bills for",
        "all bill", "bill history", "purchase history", "purchases for",
        "bought by", "list purchase",
        # Date-based bill listing
        "bill on", "bills on", "bills today", "bills for today",
        "bills for tomorrow", "list bill on", "show bill on",
        "bills this week", "bills last week",
        # Invoice / PDF generation
        "generate bill", "print bill", "share bill", "bill pdf",
        "send bill", "pdf for", "pdf of", "invoice pdf",
        "send invoice", "generate invoice", "invoice for",
        "get bill", "fetch bill", "show bill details", "bill details",
    })
    # Post-payment resolution: underpayment/overpayment answers that must ALWAYS
    # route to BILLING_CONFIRM so the Redis-aware tools with payment recording are used.
    # These are safe to route regardless of draft state — they are always follow-ups
    # to a confirm_payment() call, never mid-draft operations.
    _BILLING_CONFIRM_RESOLUTION_KEYWORDS = frozenset({
        # Underpayment/overpayment → khata (exact substring matches)
        "add to khata", "save to khata", "put in khata", "add in khata",
        "save in khata", "store in khata", "record in khata",
        "add to their khata", "put it in khata", "add balance to khata",
        "his khata", "her khata", "their khata",
        # Overpayment → return change as cash (collect_balance_now)
        "return change", "give change", "return the change",
        # Underpayment → collect balance now (collect_balance_now)
        "will collect", "collect now", "collect balance", "pay now",
        "paying now", "pay balance", "customer will pay", "will pay now",
        "collect it now", "got it", "received", "collected",
        "balance paid", "paid balance", "paid the rest", "paid remaining",
        "full amount paid", "paid full", "paid in full",
    })
    # Lookup + resolution keywords are always allowed (draft or not)
    if any(kw in msg for kw in _BILLING_CONFIRM_LOOKUP_KEYWORDS):
        return "BILLING_CONFIRM"
    if any(kw in msg for kw in _BILLING_CONFIRM_RESOLUTION_KEYWORDS):
        return "BILLING_CONFIRM"
    # "khata" with any word(s) before it — catches "<name> khata", "to <name> khata",
    # "add it to kalyan khata", "add to ramesh's khata", etc.
    # Using regex so an intervening customer name doesn't break the match.
    if _re.search(r"\bkhata\b", msg):
        return "BILLING_CONFIRM"

    # Payment/cancellation keywords — only when no active draft
    if not has_active_draft:
        for kw in INTENT_KEYWORDS["BILLING_CONFIRM"]:
            if kw in msg:
                return "BILLING_CONFIRM"

    for intent, keywords in INTENT_KEYWORDS.items():
        if intent == "BILLING_CONFIRM":
            continue  # already handled above
        if any(kw in msg for kw in keywords):
            return intent
    return "BILLING"
