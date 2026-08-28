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

# ── New-product copyable template ─────────────────────────────────────────────
# Appended to search_products "not found" returns, and used by the system prompt
# whenever the model asks for new-product details. A numbered list with a FILLED
# example (not blank fields) so the owner can tap Copy, edit the example values in
# place, and paste back — a single fenced code block renders as monospace/copyable
# on Telegram & WhatsApp (a pipe/markdown table does NOT — see system_prompt.py).
_NEW_PRODUCT_TEMPLATE = (
    "\n\nTo add a new product, I need the following details. Here's an example — "
    "copy this, replace the values with the real ones, and send it back:\n"
    "```\n"
    "1. Name - Bottle\n"
    "2. Type - Branded / Loose\n"
    "3. Unit - PIECE / KG / G / L / ML / PACKET / DOZEN / BUNDLE\n"
    "4. Cost price (Rs.) - 10\n"
    "5. Selling price / MRP (Rs.) - 15\n"
    "6. GST rate - 5  (0 for loose, else 5 / 12 / 18 / 28 for branded)\n"
    "7. Brand - Company Name (skip if loose)\n"
    "8. Reorder level - 20\n"
    "9. Initial stock - 50\n"
    "```"
)


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
        Create the shop for the owner. Call this ONLY after:
        1. The owner has confirmed their name (save_owner_name was called).
        2. ALL 6 shop details have been collected AND the owner confirmed the summary.

        DO NOT call this tool until the owner explicitly says 'yes' or 'save' to the
        summary you showed them. NEVER skip the confirmation step.

        Parameters:
        - shop_name: name of the shop (required)
        - phone: shop phone number — MANDATORY, must be a valid 10-digit Indian mobile number
        - state_code: 2-digit Indian GST state code (required, e.g. '29' for Karnataka, '27' for Maharashtra)
        - gstin: 15-character GST registration number (optional — pass None if owner said 'skip')
        - address: shop address (optional — pass None if owner said 'skip')
        - default_payment_mode: CASH / UPI / CREDIT.
          Ask the owner which mode they prefer. If they don't specify, use 'CASH' and inform them:
          'No payment mode specified — setting Cash as the default. You can change this later.'
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
        return (
            result.message + "\n\n"
            "⚠️ MANDATORY NEXT STEP — output EXACTLY the following to the owner, nothing else:\n"
            "Registration complete! 🎉 Now let's add your first product.\n\n"
            "Copy the template below, fill in the real values, and send it back:\n"
            "```\n"
            "1. Name - \n"
            "2. Type - Branded / Loose\n"
            "3. Unit - PIECE / KG / G / L / ML / PACKET / DOZEN / BUNDLE\n"
            "4. Cost price (Rs.) - \n"
            "5. Selling price / MRP (Rs.) - \n"
            "6. GST rate - 5  (0 for loose, else 5 / 12 / 18 / 28 for branded)\n"
            "7. Brand - Company Name (skip if loose)\n"
            "8. Reorder level - \n"
            "9. Initial stock - \n"
            "```\n"
            "DO NOT offer billing, analytics, or any other options. "
            "DO NOT ask what the owner wants to do next. "
            "Show ONLY the template above and wait for the owner to fill it in."
        )

    async def save_owner_name(first_name: str, last_name: str | None = None) -> str:
        """
        Save the owner's name. Call this ONLY after you have CONFIRMED which part of the name
        is the first name and which (if given) is the last name.

        RULES BEFORE CALLING:
        - Ask: 'What is your first name?'
        - Then ask: 'What is your last name? (type skip if you prefer not to share)'
        - If the owner gives a single word and it is UNCLEAR whether it is first or last name,
          ask: 'Is "{word}" your first name or last name?' — NEVER assume.
        - Only call this tool once you know exactly which field is first_name and which is last_name.

        first_name: required (what the owner confirmed is their first name)
        last_name: optional (what the owner confirmed is their last name, or None if they skipped)
        """
        from src.mcp.identity.models import RegisterUserResult
        result: RegisterUserResult = await mcps.identity.register_user(
            telegram_user_id=tuid,
            first_name=first_name,
            last_name=last_name,
        )
        display = f"{first_name} {last_name}".strip() if last_name else first_name
        return f"Got it! Welcome, {display}. Now let's set up your shop details."

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
        Add a confirmed product to the catalogue. Call this ONLY after:
        1. The owner has explicitly provided ALL 9 fields (name, type, unit, cost price,
           MRP, GST rate, brand, reorder level, initial stock).
        2. You have shown the owner a product summary and they confirmed with 'yes'.

        ⚠️ NEVER assume or guess any field — especially:
           - is_loose: NEVER assume. Ask 'Is it Loose or Branded/Packaged?'
           - gst_rate: Loose → always 0. Branded → MUST be exactly 5/12/18/28.
             If the owner has not stated the GST rate for a branded item → ask.
           - unit: NEVER assume. Ask explicitly.
           - brand: NEVER assume. Ask for the brand name if branded.
        ⚠️ NEVER ask for HSN code — it is not required.

        Parameters:
        - name: product name (e.g. 'Tata Salt', 'Sugar')
        - is_loose: True if sold loose by weight/volume, False if branded/packaged
        - unit: one of KG / G / L / ML / PACKET / PIECE / DOZEN / BUNDLE
        - cost_price: price you paid the supplier (rupees)
        - mrp: selling price / MRP (rupees)
        - reorder_level: minimum stock quantity before reorder alert
        - gst_rate: MANDATORY. Loose items → always pass 0. Branded items → MUST be
          one of 5 / 12 / 18 / 28. NEVER pass 0 for branded — ask the owner first.
        - brand: brand name if branded (e.g. 'Tata'), None if loose

        ⚠️ SAME-TURN RULE: after calling add_product(), you MUST call receive_stock() in
        the SAME turn using the initial stock quantity the owner provided.
        Do NOT skip receive_stock() — without stock, billing will not work.
        The product_id is saved server-side; receive_stock() resolves it automatically.
        """
        from src.mcp.catalogue.models import AddProductResult
        try:
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
            return (
                result.message
                + f"\n[internal product_id={result.product_id} — use for receive_stock(), do NOT show to owner]"
                + "\n⚠️ MANDATORY NEXT STEP (same turn): call receive_stock(product_id, initial_stock_qty) NOW."
            )
        except Exception as e:
            return f"ERROR: {e}"

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
        """Search for a product by name. Returns matching products with their full product IDs.
        Always use the SINGULAR base form of the name — never plural.
        CORRECT: query='pencil'   INCORRECT: query='pencils'
        Never include brand in query — name only.
        CORRECT: query='pencil'   INCORRECT: query='natraj pencil'
        """
        from src.mcp.catalogue.models import ProductResult
        products: list[ProductResult] = await mcps.catalogue.search_products(
            store_id=store_id, query=query
        )
        if not products:
            return f"No products found matching '{query}'." + _NEW_PRODUCT_TEMPLATE
        lines = [
            f"Products matching '{query}' "
            f"[internal — product_ids below are for tool calls only, NEVER show to owner]:"
        ]
        for p in products:
            brand_str = f" ({p.brand})" if p.brand else ""
            loose_str = "LOOSE" if p.is_loose else "BRANDED"
            lines.append(
                f"  [internal product_id={p.product_id}] {p.name}{brand_str} | {loose_str} | {p.unit} | MRP Rs.{p.mrp}"
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
        try:
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
        except Exception as e:
            return f"ERROR: {e}"

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
        try:
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
        except Exception as e:
            return f"ERROR: {e}"

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

    async def receive_stock(product_id: str, quantity: float, notes: str | None = None) -> str:
        """
        Record initial stock for a newly added product.
        MUST be called in the SAME turn as add_product() — do NOT skip.
        - product_id: the product_id returned by add_product() (from [internal product_id=...])
        - quantity: the initial stock quantity the owner specified (REQUIRED)
        The store will transition to ACTIVE once stock is received.
        """
        from src.mcp.inventory.models import ReceiveStockResult
        try:
            result: ReceiveStockResult = await mcps.inventory.receive_stock(
                store_id=store_id,
                product_id=product_id,
                quantity=quantity,
                notes=notes,
                telegram_user_id=tuid,
            )
            return result.message
        except Exception as e:
            return f"ERROR: {e}"

    return [add_product, receive_stock, list_products, search_products, update_product_details, update_store, update_owner_name]


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
        """Search for a product by name. Returns matching products with their full product IDs.
        Always use the SINGULAR base form of the name — never plural.
        CORRECT: query='pencil'   INCORRECT: query='pencils'
        Never include brand in query — name only.
        CORRECT: query='pencil'   INCORRECT: query='natraj pencil'
        """
        from src.mcp.catalogue.models import ProductResult
        products: list[ProductResult] = await mcps.catalogue.search_products(
            store_id=store_id, query=query
        )
        if not products:
            return f"No products found matching '{query}'." + _NEW_PRODUCT_TEMPLATE
        lines = [
            f"Products matching '{query}' "
            f"[internal — product_ids below are for tool calls only, NEVER show to owner]:"
        ]
        for p in products:
            brand_str = f" ({p.brand})" if p.brand else ""
            loose_str = "LOOSE" if p.is_loose else "BRANDED"
            lines.append(
                f"  [internal product_id={p.product_id}] {p.name}{brand_str} | {loose_str} | {p.unit}"
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
        try:
            result: ReceiveStockResult = await mcps.inventory.receive_stock(
                store_id=store_id,
                product_id=product_id,
                quantity=quantity,
                notes=notes,
                telegram_user_id=tuid,
            )
            return result.message
        except Exception as e:
            return f"ERROR: {e}"

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
        Add a new product to the catalogue.

        ⚠️ STOP — before calling this tool you MUST collect ALL 9 fields from the owner
        using the copyable fenced code block template below.
        DO NOT use a plain bullet list or numbered prose — use ONLY the ``` code block.
        Pre-fill the product name the owner mentioned. Example:
        ```
        1. Name - Salt
        2. Type - Branded / Loose
        3. Unit - PIECE / KG / G / L / ML / PACKET / DOZEN / BUNDLE
        4. Cost price (Rs.) - 10
        5. Selling price / MRP (Rs.) - 15
        6. GST rate - 5  (0 for loose, else 5 / 12 / 18 / 28 for branded)
        7. Brand - Company Name (skip if loose)
        8. Reorder level - 20
        9. Initial stock - 50
        ```
        NEVER assume is_loose, gst_rate, brand, or unit.
        NEVER ask for description, category, or HSN code.
        ⚠️ SAME TURN RULE: once you have ALL 9 fields, call add_product() AND receive_stock()
        in the SAME turn — do NOT split across turns.
        The product_id is saved server-side — receive_stock() resolves it automatically.
        """
        from src.mcp.catalogue.models import AddProductResult
        try:
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
            return result.message + f"\n[internal product_id={result.product_id} — use for receive_stock, do NOT show to owner]"
        except Exception as e:
            return f"ERROR: {e}"

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
        try:
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
        except Exception as e:
            return f"ERROR: {e}"

    async def update_store(
        shop_name: str | None = None,
        phone: str | None = None,
        address: str | None = None,
        state_code: str | None = None,
        default_payment_mode: str | None = None,
    ) -> str:
        """Update store details. Only pass fields that should change."""
        try:
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
        except Exception as e:
            return f"ERROR: {e}"

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
    # _last_added_product_id_cell: written by add_product(), read by receive_stock().
    # Prevents the LLM from hallucinating a stale product_id across turns.
    _last_added_product_id_cell: list[str | None] = [None]

    # ── Shared lookup tools (all intents) ───────────────────────────────

    async def search_products(query: str) -> str:
        """
        Search for a product by name. Returns full product_id and details.

        QUERY RULES:
        - Use the SHORTEST single word that identifies the product — never multi-word.
          CORRECT:   search_products(query='wheat')   or   search_products(query='aata')
          INCORRECT: search_products(query='wheat aata')  ← multi-word misses results
          If owner says 'wheat aata', search for 'wheat' (the most distinctive word).
        - ALWAYS use the SINGULAR base form — never plural.
          CORRECT:   search_products(query='pencil')
          INCORRECT: search_products(query='pencils')  ← plural misses results
        - Search by product name ONLY — never use brand as the query.
          CORRECT:   search_products(query='pencil')
          INCORRECT: search_products(query='natraj')  ← brand alone misses results
          If owner says 'natraj pencils', search for 'pencil' — results include brand info.
        - If this tool already returned results earlier this turn or the previous turn,
          do NOT call it again — use the product_id already in your context.
        - If the owner disambiguates from a prior multi-result response (e.g. says 'natraj'
          after you listed Apsara and Natraj pencils), pick the matching product_id from
          that earlier result and proceed directly to add_item_to_draft — do NOT re-search.
        """
        from src.mcp.catalogue.models import ProductResult
        products: list[ProductResult] = await mcps.catalogue.search_products(
            store_id=store_id, query=query
        )
        if not products:
            return (
                f"No products found matching '{query}'.\n"
                f"⚠️ STOP — do NOT paraphrase or reformat what follows. "
                f"Send the owner EXACTLY this message, word for word, code block included:\n"
                f"---\n"
                f"{query} is not in the catalogue. Do you want to add it or skip it?\n"
                + _NEW_PRODUCT_TEMPLATE
                + f"\n---\n"
                f"Wait for the owner's reply before collecting any details. "
                f"If they say yes/add, collect the filled-in fields from their next message."
            )
        lines = [
            f"Products matching '{query}' "
            f"[internal — product_ids below are for tool calls only, NEVER show to owner]:"
        ]
        for p in products:
            brand_str = f" ({p.brand})" if p.brand else ""
            loose_str = "LOOSE" if p.is_loose else "BRANDED"
            lines.append(
                f"  [internal product_id={p.product_id}] {p.name}{brand_str} | {loose_str} | {p.unit} | MRP Rs.{p.mrp} | GST {p.gst_rate}%"
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
        lines = [
            "Catalogue "
            "[internal — product_ids below are for tool calls only, NEVER show to owner]:"
        ]
        for p in products:
            brand_str = f" ({p.brand})" if p.brand else ""
            loose_str = "LOOSE" if p.is_loose else "BRANDED"
            lines.append(
                f"  [internal product_id={p.product_id}] {p.name}{brand_str} | {loose_str} | {p.unit} | MRP Rs.{p.mrp} | GST {p.gst_rate}%"
            )
        return "\n".join(lines)

    async def update_store(
        shop_name: str | None = None, phone: str | None = None,
        address: str | None = None, state_code: str | None = None,
        default_payment_mode: str | None = None,
    ) -> str:
        """Update store-level details. Only pass fields that need to change — leave others as None.

        USE WHEN owner says things like:
          - 'always assume cash' / 'default payment is cash' / 'always use UPI'
            → update_store(default_payment_mode='CASH') or 'UPI' or 'CREDIT'
          - 'change shop name to X' → update_store(shop_name='X')
          - 'update phone to 9999988888' → update_store(phone='9999988888')
          - 'change address to ...' → update_store(address='...')

        ⚠️ When owner says 'always assume cash/upi/credit' or 'default payment is X',
        MANDATORY: call update_store(default_payment_mode='CASH'/'UPI'/'CREDIT') immediately.
        DO NOT just acknowledge in text — always call this tool so the DB is updated.
        valid values for default_payment_mode: 'CASH', 'UPI', 'CREDIT'
        """
        try:
            result = await mcps.identity.update_store(
                store_id=store_id, shop_name=shop_name, phone=phone,
                address=address, state_code=state_code,
                default_payment_mode=default_payment_mode,
            )
            return f"Store updated: {result.shop_name} | Phone: {result.phone or 'not set'} | State: {result.state_code}"
        except Exception as e:
            return f"ERROR: {e}"

    async def update_owner_name(first_name: str | None = None, last_name: str | None = None) -> str:
        """Update the owner's name on their profile."""
        result = await mcps.identity.update_user(
            telegram_user_id=tuid, first_name=first_name, last_name=last_name
        )
        return result.message

    async def get_store_details() -> str:
        """
        Fetch the store's real profile details from the database.

        USE WHEN: owner asks 'store details', 'shop details', 'my store info',
        'what is my shop address', 'store phone number', 'what is my GSTIN', etc.

        ⚠️ CRITICAL: This tool returns ONLY the fields this system actually tracks.
        Fields it does NOT track — email address, bank account/IFSC/UPI details,
        business hours, product category lists — are NOT available. If the owner
        asks for any of those, say EXACTLY: 'I don't have that information.'
        NEVER invent a value for ANY field, known or unknown.
        """
        try:
            store = await mcps.identity.get_store(telegram_user_id=tuid)
        except Exception as e:
            return f"ERROR: {e}"
        if not store:
            return "No store found. Store setup may not be complete."
        return (
            f"Shop Name: {store.shop_name}\n"
            f"Phone: {store.phone or 'not set'}\n"
            f"Address: {store.address or 'not set'}\n"
            f"State code: {store.state_code}\n"
            f"GSTIN: {store.gstin or 'not registered'}\n"
            f"Default payment mode: {store.default_payment_mode}\n"
            f"⚠️ These are the ONLY store fields available. Do NOT state or invent "
            f"email, bank/UPI account details, business hours, or anything not listed above — "
            f"say 'I don't have that information' instead."
        )

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
        for _variant in _possessive_variants(name_or_phone):
            lookup = await mcps.khata.get_customer(
                store_id=store_id, name_or_phone=_variant
            )
            if lookup.found:
                break
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

    async def generate_invoice_pdf(bill_number_or_id: str | None = None) -> str:
        """
        Generate and send a PDF invoice for ANY bill — confirmed, voided, refunded, or
        cancelled. Bill status does NOT matter — always generate when asked.

        USE WHEN: owner asks 'generate invoice', 'send invoice', 'PDF for this bill',
        'share bill as PDF', 'invoice for BL-003-...', or any similar request.
        NEVER refuse to generate because a bill was voided or cancelled — the owner
        may need a record of any transaction regardless of its status.

        - bill_number_or_id: the human bill number (e.g. BL-003-20260815-013) OR bill UUID.
          If the [internal voided_bill_number=...] hint is present in context, use that value.
          If no bill number is known, pass None or omit — the tool fetches the most
          recent bill for this store automatically.

        Sends the PDF directly to the owner's Telegram chat and deletes the temp file.
        Returns a confirmation message.
        """
        import os as _os
        from src.utils.guardrails import clean_uuid as _clean_uuid
        from src.telegram.telegram_client import get_telegram_client

        resolved_bill_id: str | None = None

        if bill_number_or_id:
            if _clean_uuid(bill_number_or_id):
                resolved_bill_id = bill_number_or_id
            else:
                # Lookup by bill_number — no status filter, works for any status
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
            # No bill number given (or lookup failed) — fall back to most recent bill
            # for this store regardless of status.
            try:
                resp = (
                    mcps.billing.db.schema("billing")
                    .table("bills")
                    .select("id")
                    .eq("store_id", store_id)
                    .not_.in_("status", ["OPEN", "CANCELLED_DRAFT"])
                    .order("created_at", desc=True)
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
                "ERROR: No bills found for this store. "
                "Please complete a bill first before generating an invoice."
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
        for _variant in _possessive_variants(name_or_phone):
            lookup = await mcps.khata.get_customer(
                store_id=store_id, name_or_phone=_variant
            )
            if lookup.found:
                break
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

        Returns bill number, total amount, payment mode, and item count for each bill.
        Lines prefixed [internal] contain bill_id for tool use only — NEVER show to owner.
        To generate a PDF for one of these bills, call generate_invoice_pdf with the
        bill_id from the [internal] line that follows each bill row.
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
                f"{b.payment_mode}{credit_tag} | {b.item_count} items"
            )
            lines.append(f"  [internal bill_id={b.bill_id} — for generate_invoice_pdf only, NEVER show to owner]")
        return "\n".join(lines)

    async def list_customers_with_balances() -> str:
        """
        List ALL customers with their current outstanding khata balances.

        USE WHEN: owner asks 'list all balances', 'all khata', 'show all customers',
        'who owes money', 'all outstanding balances', 'list khata'.

        Returns every customer with their signed balance and direction.
        """
        result = await mcps.khata.list_customers_with_balances(store_id=store_id)
        if not result:
            return "No customers found."
        lines = ["Customers with balances "
                  "[internal — customer_ids below are for tool calls only, NEVER show to owner]:"]
        for i, c in enumerate(result, 1):
            bal, owes = _balance_cols(c.balance)
            lines.append(f"  {i}. [internal customer_id={c.customer_id}] {c.name} ({c.phone}) — {bal} ({owes})")
        return "\n".join(lines)

    # ── CATALOGUE intent ─────────────────────────────────────────────────

    if intent == "CATALOGUE":
        async def add_product(
            name: str, is_loose: bool, unit: str, cost_price: float,
            mrp: float, reorder_level: float, brand: str | None = None,
            hsn_code: str | None = None, gst_rate: float = 0.0,
        ) -> str:
            """
            Add a new product to the catalogue after owner confirmation.

            ⚠️ STOP — before calling this tool you MUST collect ALL 9 fields from the owner
            using the copyable fenced code block template (see Rule 24).
            DO NOT use a plain bullet or numbered list — use the ``` code block template ONLY.
            Pre-fill the product name the owner mentioned. Example:
            ```
            1. Name - Salt
            2. Type - Branded / Loose
            3. Unit - PIECE / KG / G / L / ML / PACKET / DOZEN / BUNDLE
            4. Cost price (Rs.) - 10
            5. Selling price / MRP (Rs.) - 15
            6. GST rate - 5  (0 for loose, else 5 / 12 / 18 / 28 for branded)
            7. Brand - Company Name (skip if loose)
            8. Reorder level - 20
            9. Initial stock - 50
            ```
            NEVER assume is_loose, gst_rate, brand, or unit.
            Only call this tool after the owner confirms the summary with 'yes'.
            """
            from src.mcp.catalogue.models import AddProductResult
            try:
                result: AddProductResult = await mcps.catalogue.add_product(
                    store_id=store_id, name=name, is_loose=is_loose, unit=unit,
                    cost_price=cost_price, mrp=mrp, reorder_level=reorder_level,
                    brand=brand, hsn_code=hsn_code, gst_rate=gst_rate, telegram_user_id=tuid,
                )
                return (
                    result.message
                    + f"\n[product_id={result.product_id}]"
                    + "\nMANDATORY NEXT STEPS (same turn):"
                    + "\n  1. If owner provided initial stock quantity → call receive_stock(product_id, qty) NOW."
                    + "\n  2. If this product was requested for the current bill → after receive_stock, call check_availability then add_item_to_draft(product_id, requested_qty)."
                    + "\n  Do NOT skip add_item_to_draft — the product must be added to the bill explicitly."
                )
            except Exception as e:
                return f"ERROR: {e}"

        async def receive_stock(product_id: str, quantity: float, notes: str | None = None) -> str:
            """Record received stock for a product."""
            from src.mcp.inventory.models import ReceiveStockResult
            try:
                result: ReceiveStockResult = await mcps.inventory.receive_stock(
                    store_id=store_id, product_id=product_id,
                    quantity=quantity, notes=notes, telegram_user_id=tuid,
                )
                return result.message
            except Exception as e:
                return f"ERROR: {e}"

        async def update_product_details(
            product_id: str,
            name: str | None = None, brand: str | None = None,
            unit: str | None = None, is_loose: bool | None = None,
            cost_price: float | None = None, mrp: float | None = None,
            reorder_level: float | None = None, gst_rate: float | None = None,
            hsn_code: str | None = None,
        ) -> str:
            """
            Update any field of an existing product.
            product_id MUST be a full UUID from list_products or search_products.
            NEVER pass a placeholder like 'PROD-001' — call list_products() first if you don't have the UUID.
            """
            from src.utils.guardrails import clean_uuid
            if not clean_uuid(product_id):
                return (
                    f"ERROR: '{product_id}' is not a valid product_id UUID. "
                    "Call list_products() or search_products() first to get the real UUID."
                )
            try:
                result = await mcps.catalogue.update_product_details(
                    store_id=store_id, product_id=product_id,
                    name=name, brand=brand, unit=unit, is_loose=is_loose,
                    cost_price=cost_price, mrp=mrp, reorder_level=reorder_level,
                    gst_rate=gst_rate, hsn_code=hsn_code,
                )
                brand_str = f" ({result.brand})" if result.brand else ""
                return f"Updated: {result.name}{brand_str} | {result.unit} | MRP Rs.{result.mrp} | GST {result.gst_rate}%"
            except Exception as e:
                return f"ERROR: {e}"

        async def deactivate_product(product_id: str) -> str:
            """Remove a product from the catalogue (soft delete). Use full product_id."""
            from src.mcp.catalogue.models import DeactivateResult
            result: DeactivateResult = await mcps.catalogue.deactivate_product(
                store_id=store_id, product_id=product_id
            )
            return result.message

        async def check_availability(product_id: str, quantity: float) -> str:
            """
            Check if a product has enough stock for a sale.
            product_id MUST be a full UUID from search_products or list_products.
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

        async def add_item_to_draft(product_id: str, quantity: float) -> str:
            """
            Add a product to the active bill draft.
            - product_id: FULL UUID from search_products or list_products — never invent.
            - quantity: how many units the owner wants to sell.
            Do NOT pass draft_bill_id — resolved automatically from the active draft.
            Only call this after add_product + receive_stock when adding a new product mid-billing.
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

        return [search_products, list_products, add_product, receive_stock, update_product_details,
                deactivate_product, update_store, update_owner_name, get_store_details,
                check_availability, add_item_to_draft]

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
            """
            Get current stock level for a product.
            product_id MUST be a full UUID from search_products or list_products — never invent one.
            """
            from src.utils.guardrails import clean_uuid
            if not clean_uuid(product_id):
                return (
                    f"ERROR: '{product_id}' is not a valid product_id. "
                    "Call search_products(query='<name>') first to get the real UUID."
                )
            result = await mcps.inventory.get_stock(
                store_id=store_id, product_id=product_id
            )
            return str(result)

        async def get_all_stock() -> str:
            """Get stock levels for all products."""
            result = await mcps.inventory.get_all_stock(store_id=store_id)
            return str(result)

        async def check_availability(product_id: str, quantity: float) -> str:
            """
            Check if enough stock is available for a sale.
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

        async def get_low_stock_items() -> str:
            """
            Get all items that are at or below THEIR OWN reorder level (per-product,
            stored in the catalogue — never a generic number).

            ⚠️ MANDATORY: call this tool whenever the owner asks 'what's running out',
            'what's running low', 'what needs reordering', 'low stock', 'out of stock',
            or similar. This is a REAL tool call — NEVER answer from memory or with a
            guessed/invented threshold like '30 units' or 'items below 20'.
            Each product has its own reorder_level set when it was added to the
            catalogue — only this tool knows the correct per-product values.
            """
            result = await mcps.inventory.get_low_stock_items(store_id=store_id)
            return str(result)

        async def get_stock_movements(product_id: str) -> str:
            """Get stock movement history for a product (last 50, newest first)."""
            from src.mcp.inventory.models import StockMovementRecord
            movements: list[StockMovementRecord] = await mcps.inventory.get_stock_movements(
                store_id=store_id, product_id=product_id
            )
            if not movements:
                return "No stock movements found for this product."
            lines = [
                f"{'Date':<19}  {'Type':<10}  {'Qty':>6}  {'Ref':<10}  Notes",
                f"{'-'*19}  {'-'*10}  {'-'*6}  {'-'*10}  -----",
            ]
            for m in movements:
                date = m.created_at[:19].replace("T", " ")
                qty = f"+{m.quantity_delta}" if m.quantity_delta >= 0 else str(m.quantity_delta)
                # Show reference type only — never expose raw UUIDs
                ref = m.reference_type if m.reference_type else "-"
                notes = m.notes or "-"
                lines.append(f"{date:<19}  {m.movement_type:<10}  {qty:>6}  {ref:<10}  {notes}")
            return "```\n" + "\n".join(lines) + "\n```"

        return [search_products, receive_stock, get_stock, get_all_stock,
                check_availability, get_low_stock_items, get_stock_movements]

    # ── KHATA intent ─────────────────────────────────────────────────────

    if intent == "KHATA":
        async def add_customer(name: str, phone: str) -> str:
            """
            Add or find a customer by phone number.
            phone is MANDATORY for all credit customers (10-digit Indian mobile).
            """
            try:
                result = await mcps.khata.add_customer(
                    store_id=store_id, name=name, phone=phone
                )
                # Return only the human message — avoids LLM misreading raw balance float
                return f"[internal customer_id={result.customer_id} — use for finalize_bill/add_credit_entry, do NOT show to owner]\n{result.message}"
            except Exception as e:
                return f"ERROR: {e}"

        async def get_customer(name_or_phone: str) -> str:
            """Look up a customer by name or phone number."""
            for _variant in _possessive_variants(name_or_phone):
                result = await mcps.khata.get_customer(
                    store_id=store_id, name_or_phone=_variant
                )
                if result.found:
                    break
            if not result.found:
                return f"No customer found matching '{name_or_phone}'."
            if len(result.customers) == 1:
                c = result.customers[0]
                bal, owes = _balance_cols(c.current_balance)
                return (
                    f"customer_id={c.customer_id} | {c.name} ({c.phone}) | "
                    f"balance={bal} | owes={owes}"
                )
            lines = [
                f"Multiple customers found for '{name_or_phone}' "
                f"[internal — customer_ids below are for tool calls only, NEVER show to owner]:",
            ]
            for i, c in enumerate(result.customers, 1):
                bal, owes = _balance_cols(c.current_balance)
                lines.append(f"  {i}. [internal customer_id={c.customer_id}] {c.name} ({c.phone}) — {bal} ({owes})")
            return "\n".join(lines)

        async def add_credit_entry(customer_id: str, amount: float, notes: str | None = None) -> str:
            """
            Record that the shop gave goods/money ON CREDIT to a customer — customer now OWES the shop.
            Use this ONLY when the customer has taken something without paying (amount_delta = +positive).
            DO NOT use this when a customer pays more than they owe — use add_payment_entry instead.
            Example: customer takes ₹200 of groceries on credit → add_credit_entry(amount=200)

            ⚠️ IMPORTANT — if there is an active draft bill (items were added this session):
            DO NOT call this tool. Call finalize_bill(payment_mode='CREDIT', is_credit=True,
            customer_id=<uuid>) instead — it creates the bill record AND the khata entry together.
            Calling add_credit_entry on an open draft leaves the draft open and creates a khata entry
            with no bill. (This tool will self-heal internally, but always use finalize_bill.)
            """
            # ── Self-heal: open draft bill exists but model called add_credit_entry directly ──
            # The correct path is finalize_bill(payment_mode='CREDIT', is_credit=True, customer_id=...)
            # which creates the bill, khata entry, and payment row atomically.
            # If the model skipped finalize_bill, do it now so no bill or payment row is lost.
            if _draft_id_cell[0] is not None:
                from src.utils.guardrails import clean_uuid
                if not clean_uuid(customer_id):
                    return "ERROR: customer_id must be a valid UUID. Call get_customer() or add_customer() first."
                draft_id = _draft_id_cell[0]
                finalized = await mcps.billing.finalize_bill(
                    draft_bill_id=draft_id,
                    payment_mode="CREDIT",
                    telegram_user_id=tuid,
                    is_credit=True,
                    customer_id=customer_id,
                )
                _draft_id_cell[0] = None
                _bill_id_cell[0] = finalized.bill_id
                _last_confirmed_bill_cell[0] = finalized.bill_id
                return (
                    f"bill_number={finalized.bill_number}\n"
                    f"status=CONFIRMED\n"
                    f"total=₹{finalized.total_amount:.2f} | payment_mode=CREDIT\n"
                    f"{finalized.message}\n"
                    f"✅ Bill is CONFIRMED. The khata entry has been recorded. "
                    f"Inform the owner and ask if they need anything else."
                )

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
            try:
                result = await mcps.khata.get_balance(
                    store_id=store_id, customer_id=customer_id
                )
                return result.message
            except Exception as e:
                return f"ERROR: {e}"

        async def get_khata_history(customer_id: str) -> str:
            """Get full transaction history for a customer."""
            try:
                result = await mcps.khata.get_khata_history(
                    store_id=store_id, customer_id=customer_id
                )
                return str(result)
            except Exception as e:
                return f"ERROR: {e}"

        return [search_products, add_customer, get_customer, add_credit_entry,
                add_payment_entry, get_balance, get_khata_history, list_customers_with_balances,
                get_payment_history]

    # ── ANALYTICS intent ─────────────────────────────────────────────────

    if intent == "ANALYTICS":
        async def get_daily_summary(date_str: str | None = None) -> str:
            """
            Get daily sales summary for a specific date.
            - date_str: YYYY-MM-DD (default: today)
            Call this for ANY request about sales overview, today's sales, daily report,
            how much was sold, revenue for a day, or any daily summary question.
            ALWAYS report the EXACT numbers returned — NEVER re-compute or re-add GST.
            """
            from src.utils.ist import today_ist as _today_ist
            target = date_str or _today_ist().isoformat()
            result = await mcps.analytics.get_daily_summary(
                store_id=store_id, summary_date=target
            )
            # Build a clear, unambiguous string so the model cannot misread raw Pydantic repr
            top = ""
            if result.top_items:
                top_lines = []
                for i, it in enumerate(result.top_items[:5], 1):
                    top_lines.append(
                        f"  {i}. {it.product_name}"
                        + (f" ({it.brand})" if it.brand else "")
                        + f" — {it.quantity_sold} {it.unit} — ₹{it.revenue:.2f}"
                    )
                top = "\nTop items:\n" + "\n".join(top_lines)
            status = "CLOSED" if result.is_day_closed else "LIVE"
            return (
                f"Daily Summary for {result.summary_date} [{status}]\n"
                f"Bills: {result.bill_count}\n"
                f"Total sales (incl. GST): ₹{result.total_sales:.2f}\n"
                f"  CGST collected: ₹{result.total_cgst:.2f}\n"
                f"  SGST collected: ₹{result.total_sgst:.2f}\n"
                f"  Total tax: ₹{result.total_tax:.2f}\n"
                f"Payment breakdown:\n"
                f"  Cash: ₹{result.cash_sales:.2f}\n"
                f"  UPI: ₹{result.upi_sales:.2f}\n"
                f"  Credit (khata): ₹{result.credit_sales:.2f}\n"
                f"  Card: ₹{result.card_sales:.2f}"
                + top
            )

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
            """
            Get all items that are at or below THEIR OWN reorder level (per-product,
            stored in the catalogue — never a generic number).

            ⚠️ MANDATORY: call this tool whenever the owner asks 'what's running out',
            'what's running low', 'what needs reordering', 'low stock', 'out of stock',
            or similar. NEVER answer from memory or with a guessed/invented threshold
            like '30 units' — each product has its own reorder_level in the catalogue;
            only this tool knows the correct per-product values.
            """
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
        # Guard: if a draft already exists in this session, return it unchanged.
        # Do NOT create a second draft or re-add items — the existing draft is the active bill.
        if _draft_id_cell[0]:
            existing_id = _draft_id_cell[0]
            return (
                f"⚠️ A draft bill is already open (draft_bill_id={existing_id}).\n"
                f"Do NOT call create_draft_bill again — use the existing draft.\n"
                f"To see current items and total, call get_draft_bill().\n"
                f"To proceed to checkout, call get_draft_bill() then finalize_and_pay() or finalize_bill()."
            )

        # Safety net: if a PENDING_PAYMENT bill from this session is still open,
        # auto-cancel it before creating a new draft.  This can happen when the
        # model bypasses amend_pending_bill and tries to manually rebuild the bill
        # instead of using the atomic amend flow.
        if _bill_id_cell[0]:
            stale_bill_id = _bill_id_cell[0]
            try:
                cancel_resp = await mcps.billing.cancel_bill(bill_id=stale_bill_id)
                _bill_id_cell[0] = None
                # Log the auto-cancel but don't surface the bill_id to the model
                import logging as _logging
                _logging.getLogger("kirana.tool_audit").info(
                    "[AUTO-CANCEL] Stale PENDING_PAYMENT bill %s cancelled before new draft "
                    "(reason: bill item changes requested after finalize). "
                    "cancel_result=%s", stale_bill_id, cancel_resp.message
                )
            except Exception as _e:
                import logging as _logging
                _logging.getLogger("kirana.tool_audit").warning(
                    "[AUTO-CANCEL] Failed to cancel stale bill %s: %s", stale_bill_id, _e
                )
                # Continue — allow new draft creation even if cancel failed
                _bill_id_cell[0] = None
        result = await mcps.billing.create_draft_bill(
            store_id=store_id, telegram_user_id=tuid
        )
        # Update the shared cell so add_item_to_draft and finalize_bill see the new ID
        # within this same request, even though they were captured before the draft existed.
        _draft_id_cell[0] = result.draft_bill_id
        return (
            f"✅ Draft bill started.\n"
            f"[internal draft_bill_id={result.draft_bill_id} — DO NOT show to owner]\n"
            f"status={result.status} | items=0 | total=₹0.00\n"
            f"⚠️ NEXT STEP: Call add_item_to_draft() NOW for every item the owner already mentioned.\n"
            f"Do NOT ask the owner what to add — they already told you. Add all items immediately.\n"
            f"Only ask 'Would you like to add anything else?' AFTER all mentioned items are added."
        )

    async def add_item_to_draft(product_id: str, quantity: float) -> str:
        """
        Add a product to the active bill draft.
        - product_id: FULL UUID from search_products or list_products — never invent.
        - quantity: how many units the owner wants to sell.
        Do NOT pass draft_bill_id — resolved automatically from the active draft.

        DISAMBIGUATION RULE:
        If search_products returned multiple results and the owner picks one by brand or
        partial name (e.g. "natraj", "apsara", "the second one"), select the matching
        product_id from that prior result and call this tool immediately.
        Do NOT call search_products again — the product_id is already in your context.

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
        return (
            str(result) + "\n"
            "⚠️ If there are MORE items from the owner's original message that have NOT been added yet, "
            "call add_item_to_draft() for the next item NOW — do NOT pause to ask.\n"
            "Only ask 'Would you like to add anything else?' AFTER every item from the owner's message "
            "has been added. Never ask mid-list."
        )

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

        USE WHEN owner asks ANY of:
          'list bill items', 'show bill', 'show items', 'list items',
          'what is in the bill', 'bill details', 'what did I add',
          'show current bill', 'view bill', 'current bill'
        ALWAYS call this tool — DO NOT answer from memory or show a hallucinated table.

        MANDATORY before Stage 2 (payment mode): call this tool to get the accurate
        total_amount (with GST). NEVER quote a total from memory or conversation history
        — always call this tool so the owner sees the correct amount before paying.
        After showing the total, ALWAYS ask:
        'How would you like to pay — Cash, UPI, or credit (add to khata)?'
        NEVER ask only 'cash or UPI' — credit/khata is ALWAYS the third option.

        ⚠️ DO NOT call this tool on greetings ('hi', 'hello', 'hii', etc.) or messages
        that do not explicitly mention billing or items. On a greeting with a stale draft
        open, ask the owner whether to continue, finalize, or cancel — do NOT inspect
        or act on the draft automatically.
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

        CREDIT FLOW (owner says 'credit', 'khata', 'add to <name> khata'):
          Step 1 — call get_customer(name_or_phone) using the name the owner gave.
          Step 2 — if customer FOUND: show the owner: 'Found <Name> (<phone>). Is this the same customer?'
                   WAIT for owner to confirm YES or NO before calling finalize_bill.
                   If YES → use that customer_id.
                   If NO → ask for the correct 10-digit phone, then add_customer(name, phone).
          Step 3 — if customer NOT FOUND: ask for 10-digit phone → add_customer(name, phone).
          Step 4 — call finalize_bill(payment_mode='CREDIT', is_credit=True, customer_id=<uuid>).
        ⚠️ NEVER call add_credit_entry directly — finalize_bill creates the bill AND the khata entry.
        ⚠️ NEVER skip finalize_bill — calling add_credit_entry leaves the draft OPEN with no bill.
        """
        # Always use the server-authoritative active draft — never trust LLM-passed IDs
        resolved_id = _draft_id_cell[0]
        if not resolved_id:
            return (
                "ERROR: No active draft bill found. Call create_draft_bill() first to start a bill."
            )
        try:
            result = await mcps.billing.finalize_bill(
                draft_bill_id=resolved_id,
                payment_mode=payment_mode,
                telegram_user_id=tuid,
                is_credit=is_credit,
                customer_id=customer_id,
            )
        except Exception as e:
            return f"ERROR: {e}"
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
                f"✅ Bill CONFIRMED. Khata entry recorded. Do NOT call confirm_payment().\n"
                f"⚠️ STOP — do NOT offer to add more items. The bill is CLOSED.\n"
                f"Offer post-bill options ONLY: generate invoice PDF | view today's bills | "
                f"check inventory | check customer balance | start a new bill."
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
            # ── Self-heal: PENDING_PAYMENT bill already exists from a previous turn ──
            # The model called finalize_and_pay again (with paid_amount) instead of
            # confirm_payment. Execute the confirm logic inline — do NOT ask the model
            # to call confirm_payment, because it issues both tools in the same parallel
            # batch and the confirm_payment return gets dropped.
            if paid_amount is not None and _bill_id_cell[0] is not None:
                pending_id = _bill_id_cell[0]
                bill = await mcps.billing.get_bill_for_payment(pending_id)
                if not bill:
                    return "ERROR: Pending bill not found. Cannot confirm payment."
                # Guard: if bill is already CONFIRMED, do not re-confirm
                if bill.get("status") == "CONFIRMED":
                    _bill_num_c = bill.get("bill_number", pending_id)
                    return (
                        f"⚠️ Bill {_bill_num_c} is already CONFIRMED — do NOT call finalize_and_pay or confirm_payment again.\n"
                        f"  • To reverse this bill → call void_bill() immediately.\n"
                        f"  • To start a new bill → call create_draft_bill()."
                    )
                bill_total = float(bill["total_amount"])
                bill_num   = bill["bill_number"]
                bill_mode  = bill["payment_mode"]
                subtotal   = float(bill["subtotal"])
                total_gst  = round(float(bill.get("total_cgst", 0)) + float(bill.get("total_sgst", 0)), 2)
                # Resolve customer from name if provided
                resolved_customer_id: str | None = bill.get("customer_id")
                if customer_name and not resolved_customer_id:
                    try:
                        cust_result = await mcps.khata.get_customer(
                            store_id=store_id, name_or_phone=customer_name.strip()
                        )
                        if cust_result.found and cust_result.customers:
                            resolved_customer_id = cust_result.customers[0].customer_id
                    except Exception:
                        pass
                diff = round(paid_amount - bill_total, 2)
                redis = _get_redis()
                if diff == 0:
                    confirm_result = await mcps.billing.confirm_payment(bill_id=pending_id)
                    if not confirm_result.success:
                        return f"ERROR confirming bill: {confirm_result.message}"
                    _last_confirmed_bill_cell[0] = pending_id
                    _bill_id_cell[0] = None
                    await mcps.payments.record_payment(
                        store_id=store_id, bill_id=pending_id, bill_number=bill_num,
                        customer_id=resolved_customer_id, paid_amount=paid_amount,
                        payment_mode=bill_mode, payment_type="EXACT",
                        payment_status="CONFIRMED", subtotal=subtotal,
                        total_gst=total_gst, bill_amount=bill_total,
                        change_amount=0.0, balance_due=0.0,
                    )
                    if resolved_customer_id:
                        try:
                            await mcps.billing.link_bill_customer(bill_id=pending_id, customer_id=resolved_customer_id)
                        except Exception:
                            pass
                    if redis:
                        await redis.clear_pending_payment(tuid)
                    customer_note = f" | customer={customer_name}" if customer_name and resolved_customer_id else ""
                    return (
                        f"✅ Payment confirmed! Bill {bill_num} paid in full.\n"
                        f"paid=₹{paid_amount:.2f} | total=₹{bill_total:.2f} | EXACT{customer_note}\n"
                        f"⚠️ STOP. Do NOT call any other tool. Do NOT offer to add more items — bill is CLOSED.\n"
                        f"Offer post-bill options ONLY: generate invoice PDF | view today's bills | "
                        f"check inventory | check customer balance | start a new bill."
                    )
                elif diff > 0:
                    change_amount = diff
                    if redis:
                        await redis.set_pending_payment(tuid, {
                            "bill_id": pending_id, "bill_number": bill_num,
                            "bill_amount": bill_total, "paid_amount": paid_amount,
                            "payment_mode": bill_mode, "payment_reference": bill.get("payment_reference"),
                            "intent_type": "OVERPAYMENT", "delta_amount": change_amount,
                            "store_id": store_id, "subtotal": subtotal, "total_gst": total_gst,
                            "customer_id": resolved_customer_id,
                        })
                    return (
                        f"₹{paid_amount:.2f} received for Bill {bill_num} (total ₹{bill_total:.2f}).\n"
                        f"OVERPAYMENT — change = ₹{change_amount:.2f}\n"
                        f"⚠️ STOP HERE. Ask the owner exactly this:\n"
                        f"'₹{change_amount:.2f} extra received. Return change to customer as cash, "
                        f"or add to their khata account?'\n"
                        f"⚠️ Do NOT call any other tool until owner answers.\n"
                        f"  • Return cash → call collect_balance_now().\n"
                        f"  • Add to khata → get_customer(name) then add_payment_entry(customer_id).\n"
                        f"⚠️ CRITICAL: For overpayment-to-khata use add_payment_ENTRY (NOT add_credit_entry)."
                    )
                else:
                    balance_due = round(-diff, 2)
                    if redis:
                        await redis.set_pending_payment(tuid, {
                            "bill_id": pending_id, "bill_number": bill_num,
                            "bill_amount": bill_total, "paid_amount": paid_amount,
                            "payment_mode": bill_mode, "payment_reference": bill.get("payment_reference"),
                            "intent_type": "UNDERPAYMENT", "delta_amount": balance_due,
                            "store_id": store_id, "subtotal": subtotal, "total_gst": total_gst,
                            "customer_id": resolved_customer_id,
                        })
                    return (
                        f"₹{paid_amount:.2f} received for Bill {bill_num} (total ₹{bill_total:.2f}).\n"
                        f"UNDERPAYMENT — remaining balance = ₹{balance_due:.2f}\n"
                        f"⚠️ STOP HERE. Ask the owner exactly this:\n"
                        f"'₹{balance_due:.2f} is still due. Will the customer pay the balance now, "
                        f"or should I add it to their khata?'\n"
                        f"⚠️ Do NOT call any other tool until owner answers.\n"
                        f"  • Collect now → call collect_balance_now().\n"
                        f"  • Add to khata → get_customer(name) then add_credit_entry(customer_id)."
                    )
            # No draft and no paid_amount — check if owner is trying to change payment mode
            # on an existing PENDING_PAYMENT bill (e.g. "change to upi", "use cash instead").
            if _bill_id_cell[0] is not None:
                return (
                    f"ERROR: No active draft bill. There is a PENDING_PAYMENT bill waiting for payment.\n"
                    f"If the owner wants to change the payment mode, call change_payment_mode(new_payment_mode=...).\n"
                    f"If the owner states a paid amount, call confirm_payment(paid_amount=<amount>).\n"
                    f"Do NOT create a new draft — the existing bill is still open."
                )
            return "ERROR: No active draft bill found. Call create_draft_bill() first."

        # Step 1 — finalize (always)
        try:
            result = await mcps.billing.finalize_bill(
                draft_bill_id=resolved_id,
                payment_mode=payment_mode,
                telegram_user_id=tuid,
            )
        except ValueError as _ve:
            # Most common cause: bill is empty (no items added yet)
            return (
                f"ERROR: {_ve}\n"
                f"⚠️ You must call add_item_to_draft() for each item BEFORE calling finalize_and_pay.\n"
                f"The draft bill exists but has NO items — add items first, then finalize."
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
                f"⚠️ STOP. Do NOT call any other tool. Do NOT offer to add more items — bill is CLOSED.\n"
                f"Offer post-bill options ONLY: generate invoice PDF | view today's bills | "
                f"check inventory | check customer balance | start a new bill."
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
                f"⚠️ STOP HERE. Do NOT call any tool this turn.\n"
                f"⚠️ Do NOT reuse any customer from conversation history — this is a new independent bill.\n"
                f"Ask the owner: 'Change is ₹{change_amount:.2f}. Return as cash, or add to customer's khata?'\n"
                f"ONLY in the NEXT turn (after owner answers):\n"
                f"  • If 'return cash' → do nothing. Inform the owner and stop.\n"
                f"  • If 'add to khata' → get_customer(name) → add_payment_entry(customer_id) ← amount=None reads from Redis.\n"
                f"⚠️ CRITICAL: For overpayment-to-khata use add_payment_ENTRY (NOT add_credit_entry).\n"
                f"  add_payment_entry = shop owes customer (balance goes negative = shop favour).\n"
                f"  add_credit_entry  = customer owes shop — WRONG for overpayment change."
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

    async def get_bill(bill_id: str | None = None) -> str:
        """Get details of the current PENDING_PAYMENT bill or any confirmed bill.

        USE WHEN owner asks 'list bill items', 'show bill', 'show items', 'bill details',
        'what is on the bill', 'view bill', 'current bill' — AND no draft is open
        (i.e. finalize_and_pay has already been called).
        ALWAYS call this tool — NEVER show bill items from memory or hallucinate a table.

        bill_id is OPTIONAL — omit it (pass null) to show the current PENDING_PAYMENT bill.
        Only pass bill_id if you have a specific UUID for a different bill.
        """
        resolved = bill_id or _bill_id_cell[0]
        if not resolved:
            return "ERROR: No current bill found. Please finalize a bill first."
        from src.utils.guardrails import clean_uuid
        if not clean_uuid(resolved):
            return f"ERROR: '{resolved}' is not a valid bill_id."
        result = await mcps.billing.get_bill(bill_id=resolved)
        return str(result)

    async def collect_balance_now() -> str:
        """
        Resolve a pending UNDERPAYMENT or OVERPAYMENT by treating the bill as fully settled.

        USE WHEN the owner says the customer will pay the remaining balance RIGHT NOW in cash:
          OVERPAYMENT: 'return change', 'give change', 'return the change', 'return cash'
          UNDERPAYMENT: 'will collect now', 'collect now', 'paying now', 'pay balance now',
            'customer is paying', 'got it', 'collected', 'received full amount'

        ⚠️ DO NOT call this tool when owner says 'add to khata', 'put in khata', 'save to khata',
           or any variation of adding the balance to khata / credit.
           For 'add to khata' on an UNDERPAYMENT: use get_customer(name) → add_credit_entry(customer_id).
           For 'add to khata' on an OVERPAYMENT:  use get_customer(name) → add_payment_entry(customer_id).

        WHAT THIS DOES (NO khata entry is created — physical cash exchange only):
          - UNDERPAYMENT: records the FULL bill amount as EXACT payment, confirms bill as CONFIRMED.
          - OVERPAYMENT:  records the FULL paid_amount as EXACT payment (change returned as cash).

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

        # ── Hard guard: if the owner said "add to khata" this tool is WRONG ──────
        # Check the resolution_hint stored in Redis (set by finalize_and_pay /
        # confirm_payment when the model tells us the owner's choice).
        # Fallback: if resolution_hint is "khata", refuse and redirect.
        if intent.get("resolution_hint") == "khata":
            _delta = intent.get("delta_amount", 0)
            _bill_num = intent.get("bill_number", "")
            if intent_type == "UNDERPAYMENT":
                return (
                    f"⚠️ WRONG TOOL — the owner chose 'add to khata', not 'collect now'.\n"
                    f"Bill {_bill_num} has an UNDERPAYMENT of ₹{_delta:.2f} to add to khata.\n"
                    f"Do NOT call collect_balance_now(). Instead:\n"
                    f"  1. get_customer(name) to resolve the customer_id.\n"
                    f"  2. add_credit_entry(customer_id) — amount=None reads ₹{_delta:.2f} from Redis automatically."
                )
            else:  # OVERPAYMENT
                return (
                    f"⚠️ WRONG TOOL — the owner chose 'add to khata', not 'return change'.\n"
                    f"Bill {_bill_num} has an OVERPAYMENT of ₹{_delta:.2f} to add to khata.\n"
                    f"Do NOT call collect_balance_now(). Instead:\n"
                    f"  1. get_customer(name) to resolve the customer_id.\n"
                    f"  2. add_payment_entry(customer_id) — amount=None reads ₹{_delta:.2f} from Redis automatically."
                )

        try:
            bill_id    = intent["bill_id"]
            bill_num   = intent["bill_number"]
            bill_total = float(intent["bill_amount"])
            paid       = float(intent["paid_amount"])
            mode       = intent["payment_mode"]
        except (KeyError, TypeError, ValueError) as e:
            return f"ERROR: Payment intent data is incomplete or corrupted ({e}). Please try again or send /new."
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
                f"⚠️ STOP. Do NOT call any other tool. Do NOT offer to add more items — bill is CLOSED.\n"
                f"Offer post-bill options ONLY: generate invoice PDF | view today's bills | "
                f"check inventory | check customer balance | start a new bill."
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
                f"⚠️ STOP. Do NOT call any other tool. Do NOT offer to add more items — bill is CLOSED.\n"
                f"Offer post-bill options ONLY: generate invoice PDF | view today's bills | "
                f"check inventory | check customer balance | start a new bill."
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

        BARE NUMBER RULE — CRITICAL:
        If the previous assistant message asked 'How much did the customer pay?'
        or stated the bill total, and the owner replies with ONLY a number
        (e.g. '600', '562', '1000'), that number IS the paid_amount.
        Call this tool IMMEDIATELY: confirm_payment(paid_amount=<that number>).
        DO NOT ask for clarification. DO NOT ask about Sugar or any product.
        DO NOT hallucinate. The bill is already finalized — just confirm payment.

        WHEN TO CALL — any of these patterns:
          - '600'  or  '562.24'   ← bare number after asking for payment amount
          - 'paid 500'
          - 'naveen paid 40'      ← name + amount → confirm_payment(paid_amount=40, customer_name='naveen')
          - 'customer gave 200'
          - '3949 cash'
          - 'full amount'         ← only if a specific number was stated earlier this turn; else ask

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
        # ── Guard: if a Redis OVERPAYMENT/UNDERPAYMENT intent already exists, the model
        # is calling confirm_payment again instead of the resolution tool.
        # Do NOT re-run classification — redirect immediately.
        _redis_guard = _get_redis()
        _existing_intent = None
        if _redis_guard:
            _existing_intent = await _redis_guard.get_pending_payment(tuid)
        if _existing_intent and _existing_intent.get("intent_type") in ("OVERPAYMENT", "UNDERPAYMENT"):
            _itype     = _existing_intent["intent_type"]
            _bill_num  = _existing_intent.get("bill_number", "")
            _delta     = float(_existing_intent.get("delta_amount", 0))
            if _itype == "OVERPAYMENT":
                return (
                    f"⚠️ STOP — bill {_bill_num} is already confirmed as OVERPAYMENT.\n"
                    f"Change = ₹{_delta:.2f}. Do NOT call confirm_payment again.\n"
                    f"  • Return cash → call collect_balance_now() immediately.\n"
                    f"  • Add to khata → get_customer(name) → add_payment_entry(customer_id).\n"
                    f"⚠️ CRITICAL: For overpayment-to-khata use add_payment_ENTRY (NOT add_credit_entry).\n"
                    f"  add_payment_entry = shop owes customer (balance goes negative = shop favour).\n"
                    f"  add_credit_entry  = customer owes shop — WRONG for overpayment change."
                )
            else:
                return (
                    f"⚠️ STOP — bill {_bill_num} is already confirmed as UNDERPAYMENT.\n"
                    f"Balance due = ₹{_delta:.2f}. Do NOT call confirm_payment again.\n"
                    f"  • Collect now → call collect_balance_now() immediately.\n"
                    f"  • Add to khata → get_customer(name) → add_credit_entry(customer_id)."
                )

        resolved_bill_id = _bill_id_cell[0]
        if not resolved_bill_id:
            return "ERROR: No PENDING_PAYMENT bill found. Nothing to confirm."

        # Load bill details (needed for snapshot + payment type detection)
        bill = await mcps.billing.get_bill_for_payment(resolved_bill_id)
        if not bill:
            return "ERROR: Bill not found. Cannot confirm payment."

        # ── Guard: bill is already CONFIRMED — do not re-run confirm logic ──
        # This can happen when the owner says something like "cancel this bill"
        # after an underpayment was already fully resolved (Redis key cleared).
        # The model re-calls confirm_payment, which must be blocked here so it
        # doesn't create a duplicate payment row + khata entry.
        if bill.get("status") == "CONFIRMED":
            bill_num_confirmed = bill.get("bill_number", resolved_bill_id)
            return (
                f"⚠️ Bill {bill_num_confirmed} is already CONFIRMED — do NOT call confirm_payment again.\n"
                f"  • To reverse this bill → call void_bill() immediately.\n"
                f"  • To view the bill → call get_bill(bill_id='{resolved_bill_id}').\n"
                f"  • To start a new bill → call create_draft_bill()."
            )

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
                f"⚠️ STOP. Do NOT call any other tool. Do NOT offer to add more items — bill is CLOSED.\n"
                f"Offer post-bill options ONLY: generate invoice PDF | view today's bills | "
                f"check inventory | check customer balance | start a new bill."
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

    async def change_payment_mode(new_payment_mode: str, customer_id: str | None = None) -> str:
        """
        Change the payment mode of the current PENDING_PAYMENT bill BEFORE any payment has been taken.
        Use ONLY when owner says 'change to cash', 'change to upi', 'change to credit/khata'
        and NO payment has been received yet (i.e. confirm_payment has NOT been called yet).

        ⚠️ DO NOT call this tool if confirm_payment() already ran and detected UNDERPAYMENT.
           In that case 'add to khata' means record the remaining balance as a khata entry —
           use get_customer(name) → add_credit_entry(customer_id) instead.
           This tool would wrongly cancel and re-create the entire bill as CREDIT.

        - new_payment_mode: CASH, UPI, or CREDIT.
        - customer_id: REQUIRED when new_payment_mode is CREDIT — UUID from get_customer/add_customer.
          For CASH/UPI, leave customer_id as None.
        Cancels the current bill, rebuilds all items, and re-finalizes with the new payment mode.
        Do NOT pass bill_id — resolved automatically.

        CREDIT FLOW (owner says 'change to credit', 'credit instead' — NO payment received yet):
          Step 1 — call get_customer(name) using the name the owner gave.
          Step 2 — if FOUND: use that customer_id.
                   If NOT FOUND: ask for 10-digit phone → add_customer(name, phone).
          Step 3 — call change_payment_mode(new_payment_mode='CREDIT', customer_id=<uuid>).
        ⚠️ For CREDIT: do NOT call add_credit_entry separately — change_payment_mode handles everything.
        """
        resolved_bill_id = _bill_id_cell[0]
        if not resolved_bill_id:
            return "ERROR: No PENDING_PAYMENT bill found to change payment mode for."

        mode_upper = new_payment_mode.strip().upper()

        # ── Hard guard: block CREDIT path if an UNDERPAYMENT resolution is pending ──
        # "add to <name> khata" after an underpayment means record the remaining balance
        # as a khata entry — NOT convert the whole bill to CREDIT.
        if mode_upper == "CREDIT":
            redis = _get_redis()
            if redis:
                _existing = await redis.get_pending_payment(tuid)
                if _existing and _existing.get("intent_type") == "UNDERPAYMENT":
                    _delta = _existing.get("delta_amount", 0)
                    _bill_num = _existing.get("bill_number", "")
                    return (
                        f"⚠️ WRONG TOOL — bill {_bill_num} has a confirmed UNDERPAYMENT of ₹{_delta:.2f}.\n"
                        f"The owner wants to add the remaining ₹{_delta:.2f} to khata — NOT convert the bill to CREDIT.\n"
                        f"Do NOT call change_payment_mode. Instead:\n"
                        f"  1. get_customer(name) to resolve the customer_id.\n"
                        f"  2. add_credit_entry(customer_id) — amount=None reads ₹{_delta:.2f} from Redis automatically."
                    )

        # ── CREDIT path: cancel + rebuild + re-finalize as credit ─────────────
        # BillingMCP.change_payment_mode does not support CREDIT (no customer/khata linkage).
        # Mirrors what BillingMCP.change_payment_mode does for CASH/UPI but finalizes with
        # is_credit=True and customer_id so the khata entry and payment row are created.
        if mode_upper == "CREDIT":
            from src.utils.guardrails import clean_uuid
            if not customer_id or not clean_uuid(customer_id):
                return (
                    "To change to CREDIT (khata), a customer_id is required.\n"
                    "Step 1: call get_customer(name_or_phone) to find the customer.\n"
                    "Step 2: if not found, call add_customer(name, phone).\n"
                    "Step 3: call change_payment_mode(new_payment_mode='CREDIT', customer_id=<uuid>)."
                )
            # 1. Load bill items before cancelling (BillingMCP internal helper not accessible here —
            #    query bill_items directly).
            items_resp = (
                mcps.billing.db.schema("billing")
                .table("bill_items")
                .select("product_id, quantity")
                .eq("bill_id", resolved_bill_id)
                .execute()
            )
            bill_items = items_resp.data or []
            if not bill_items:
                return "ERROR: No items found on the existing bill. Cannot change payment mode."

            # 2. Cancel the PENDING_PAYMENT bill (restores stock, records CANCELLED audit row)
            cancel_result = await mcps.billing.cancel_bill(bill_id=resolved_bill_id)
            if not cancel_result.success:
                return f"ERROR: Could not cancel the existing bill: {cancel_result.message}"
            _bill_id_cell[0] = None

            # 3. Create a new draft
            new_draft = await mcps.billing.create_draft_bill(
                store_id=store_id, telegram_user_id=tuid
            )
            new_draft_id = new_draft.draft_bill_id
            _draft_id_cell[0] = new_draft_id

            # 4. Re-add all items to the new draft
            for item in bill_items:
                if not item.get("product_id"):
                    continue
                try:
                    await mcps.billing.add_item_to_draft(
                        draft_bill_id=new_draft_id,
                        product_id=item["product_id"],
                        quantity=float(item["quantity"]),
                    )
                except Exception:
                    pass  # skip deleted products

            # 5. Finalize as CREDIT with customer linkage
            finalized = await mcps.billing.finalize_bill(
                draft_bill_id=new_draft_id,
                payment_mode="CREDIT",
                telegram_user_id=tuid,
                is_credit=True,
                customer_id=customer_id,
            )
            _draft_id_cell[0] = None
            _bill_id_cell[0] = finalized.bill_id
            _last_confirmed_bill_cell[0] = finalized.bill_id
            return (
                f"✅ Payment mode changed to CREDIT.\n"
                f"bill_number={finalized.bill_number}\n"
                f"status=CONFIRMED\n"
                f"total=₹{finalized.total_amount:.2f} | payment_mode=CREDIT\n"
                f"{finalized.message}\n"
                f"✅ Bill CONFIRMED. Khata entry recorded. Do NOT call confirm_payment(). "
                f"Inform the owner and ask if they need anything else."
            )

        # ── CASH/UPI path: delegate to BillingMCP ─────────────────────────────
        result = await mcps.billing.change_payment_mode(
            bill_id=resolved_bill_id,
            new_payment_mode=mode_upper,
            telegram_user_id=tuid,
        )
        # Update cell to the new bill_id
        _bill_id_cell[0] = result.bill_id
        return (
            f"✅ Payment mode changed to {mode_upper}.\n"
            f"bill_number={result.bill_number}\n"
            f"status=PENDING_PAYMENT\n"
            f"total=₹{result.total_amount:.2f} | payment_mode={result.payment_mode}\n"
            f"Bill recreated with all original items. Now ask: 'Bill total is ₹{result.total_amount:.2f}. How much did the customer pay?'\n"
            f"⚠️ STOP — wait for the owner to state the paid amount, then call confirm_payment()."
        )

    async def amend_pending_bill(
        remove_product_names: list[str] | None = None,
        add_items: list[dict] | None = None,
    ) -> str:
        """
        ⚠️ ONE-SHOT ATOMIC AMEND — call this as your FIRST AND ONLY tool when the owner changes
        items on a PENDING_PAYMENT bill (i.e. after finalize_and_pay, before confirm_payment).

        DO NOT call search_products, create_draft_bill, add_item_to_draft, or finalize_and_pay
        before or after this tool. This tool handles EVERYTHING internally:
          1. Loads all existing bill items automatically (you do NOT need to look them up).
          2. Cancels the current PENDING_PAYMENT bill.
          3. Creates a new draft, re-adds all kept items at original quantities.
          4. Adds new items by searching the catalogue by name internally.
          5. Finalizes with the same payment mode → returns a new PENDING_PAYMENT bill total.

        USE WHEN owner says things like:
          - 'drop amul' / 'remove sugar' / 'take off salt' → remove_product_names=["amul"]
          - 'add salt 1 kg' / 'include 2 rice' → add_items=[{"product_name":"salt","quantity":1}]
          - 'drop sugar, add salt' → remove_product_names=["sugar"], add_items=[{"product_name":"salt","quantity":1}]
          - 'drop amul and add sugar' / 'replace aata with rice' → both params combined

        CRITICAL: The items that are NOT changed (e.g. rice, amul butter) are re-added automatically
        from the existing bill — you do NOT need to search for them or pass them again.

        Parameters:
          remove_product_names: list of product name strings to drop (case-insensitive substring match).
            e.g. ["sugar", "amul"] — only the items to REMOVE. Omit or pass [] if nothing removed.
          add_items: list of dicts for NEW items to add, each with:
            - "product_name": str  (tool searches catalogue by this name)
            - "quantity": float
            e.g. [{"product_name": "salt", "quantity": 1}]
            Omit or pass [] if nothing is being added.

        After this tool returns successfully, ask: 'Updated bill total is ₹X.XX. How much did the customer pay?'
        Then call confirm_payment(paid_amount=...) as normal.
        DO NOT call finalize_and_pay or create_draft_bill after this tool.
        DO NOT use this after confirm_payment has already been called.
        """
        resolved_bill_id = _bill_id_cell[0]
        if not resolved_bill_id:
            return (
                "ERROR: No PENDING_PAYMENT bill found to amend. "
                "If a draft is still open, use add_item_to_draft / remove_item_from_draft instead."
            )

        # Normalise inputs
        remove_lower = [n.lower().strip() for n in (remove_product_names or [])]
        adds = add_items or []

        # ── 1. Load current bill details ────────────────────────────────────
        bill_row = await mcps.billing.get_bill_for_payment(resolved_bill_id)
        if not bill_row:
            return f"ERROR: Could not load bill {resolved_bill_id}."
        if bill_row.get("status") != "PENDING_PAYMENT":
            return (
                f"ERROR: Bill is in status '{bill_row.get('status')}' — "
                "amend_pending_bill only works on PENDING_PAYMENT bills. "
                "If already confirmed, use void_bill() to reverse, then create a new bill."
            )
        original_payment_mode = bill_row.get("payment_mode", "CASH")

        # ── 2. Load bill items ───────────────────────────────────────────────
        existing_items = await mcps.billing._get_bill_items(resolved_bill_id)
        if not existing_items:
            return "ERROR: No items found on the existing bill."

        # ── 3. Cancel the PENDING_PAYMENT bill (restores stock) ─────────────
        cancel_result = await mcps.billing.cancel_bill(bill_id=resolved_bill_id)
        if not cancel_result.success:
            return f"ERROR: Could not cancel the existing bill: {cancel_result.message}"
        _bill_id_cell[0] = None

        # ── 4. Create a new draft ────────────────────────────────────────────
        new_draft = await mcps.billing.create_draft_bill(
            store_id=store_id, telegram_user_id=tuid
        )
        new_draft_id = new_draft.draft_bill_id
        _draft_id_cell[0] = new_draft_id

        kept = []
        dropped = []

        # ── 5. Re-add original items, skipping removed ones ──────────────────
        for item in existing_items:
            pname_lower = (item.product_name or "").lower()
            if any(r in pname_lower for r in remove_lower):
                dropped.append(item.product_name or item.product_id)
                continue
            try:
                await mcps.billing.add_item_to_draft(
                    draft_bill_id=new_draft_id,
                    product_id=item.product_id,
                    quantity=item.quantity,
                )
                kept.append(item.product_name)
            except Exception:
                pass  # skip deleted/unavailable products

        # ── 6. Add new items ─────────────────────────────────────────────────
        added = []
        failed_adds = []
        for new_item in adds:
            pname = new_item.get("product_name", "")
            qty = float(new_item.get("quantity", 1))
            # Search catalogue — returns list[ProductResult] directly
            search_result = await mcps.catalogue.search_products(
                store_id=store_id, query=pname
            )
            if not search_result:
                failed_adds.append(pname)
                continue
            product = search_result[0]
            try:
                await mcps.billing.add_item_to_draft(
                    draft_bill_id=new_draft_id,
                    product_id=product.product_id,
                    quantity=qty,
                )
                added.append(f"{product.name} x{qty}")
            except Exception as e:
                failed_adds.append(f"{pname} (error: {e})")

        # ── 7. Finalize with same payment mode ───────────────────────────────
        try:
            finalized = await mcps.billing.finalize_bill(
                draft_bill_id=new_draft_id,
                payment_mode=original_payment_mode,
                telegram_user_id=tuid,
            )
        except Exception as e:
            _draft_id_cell[0] = None
            return f"ERROR: Could not finalize the amended bill: {e}"

        _draft_id_cell[0] = None
        _bill_id_cell[0] = finalized.bill_id

        # Build summary
        parts = []
        if dropped:
            parts.append(f"Removed: {', '.join(dropped)}")
        if added:
            parts.append(f"Added: {', '.join(added)}")
        if failed_adds:
            parts.append(f"Could not add (not in catalogue): {', '.join(failed_adds)}")

        changes = " | ".join(parts) if parts else "No changes made."
        return (
            f"✅ Bill amended. {changes}\n"
            f"bill_number={finalized.bill_number}\n"
            f"status=PENDING_PAYMENT\n"
            f"total=₹{finalized.total_amount:.2f} | payment_mode={original_payment_mode}\n"
            f"Bill recreated. Now ask the owner: 'Updated bill total is ₹{finalized.total_amount:.2f}. "
            f"How much did the customer pay?'\n"
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
                .select("id, bill_number")
                .eq("store_id", store_id)
                .eq("status", "CONFIRMED")
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            _vb_rows = _vb_resp.data or []
            resolved_bill_id = _vb_rows[0]["id"] if _vb_rows else None
            resolved_bill_number = _vb_rows[0].get("bill_number") if _vb_rows else None
        except Exception:
            resolved_bill_id = None
            resolved_bill_number = None
        if not resolved_bill_id:
            return "ERROR: No CONFIRMED bill found to void."
        result = await mcps.billing.void_bill(bill_id=resolved_bill_id)
        bill_ref = f" (bill {resolved_bill_number})" if resolved_bill_number else ""
        return result.message + bill_ref + (
            f"\n[internal voided_bill_number={resolved_bill_number} — "
            f"use this for generate_invoice_pdf if owner asks for invoice]"
            if resolved_bill_number else ""
        )

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
        try:
            result = await mcps.khata.add_customer(
                store_id=store_id, name=name, phone=phone
            )
            return f"[internal customer_id={result.customer_id} — use for finalize_bill/add_credit_entry, do NOT show to owner]\n{result.message}"
        except Exception as e:
            return f"ERROR: {e}"

    async def get_customer(name_or_phone: str) -> str:
        """Look up a customer by name or phone."""
        for _variant in _possessive_variants(name_or_phone):
            result = await mcps.khata.get_customer(
                store_id=store_id, name_or_phone=_variant
            )
            if result.found:
                break
        if not result.found:
            return f"No customer found matching '{name_or_phone}'."
        if len(result.customers) == 1:
            c = result.customers[0]
            bal, owes = _balance_cols(c.current_balance)
            return (
                f"customer_id={c.customer_id} | {c.name} ({c.phone}) | "
                f"balance={bal} | owes={owes}"
            )
        lines = [
            f"Multiple customers found for '{name_or_phone}' "
            f"[internal — customer_ids below are for tool calls only, NEVER show to owner]:",
        ]
        for i, c in enumerate(result.customers, 1):
            bal, owes = _balance_cols(c.current_balance)
            lines.append(f"  {i}. [internal customer_id={c.customer_id}] {c.name} ({c.phone}) — {bal} ({owes})")
        return "\n".join(lines)

    async def add_credit_entry(customer_id: str, amount: float | None = None, notes: str | None = None) -> str:
        """
        Record that a customer owes the shop money (underpayment balance or standalone credit).
        amount_delta = POSITIVE (customer owes shop more).

        USE FOR:
          - Underpayment resolution: after confirm_payment detected underpayment and owner chose 'add to khata'
            → pass amount=None; the balance is read from Redis (prevents LLM hallucination)
          - Standalone credit advance: owner gives goods on credit outside a billing session
            → pass the explicit amount

        ⚠️ DO NOT USE FOR OVERPAYMENTS — that is add_payment_entry (opposite sign).
          Overpayment = shop owes customer change → add_payment_entry (negative delta).
          Underpayment = customer owes shop balance → add_credit_entry (positive delta).
        customer_id MUST be a valid UUID from get_customer/add_customer.

        UNDERPAYMENT FLOW (most common):
          1. confirm_payment(paid_amount=X) or finalize_and_pay detected underpayment → balance stored in Redis
          2. Owner said 'add to khata' + provided customer name
          3. get_customer(name) → got customer_id
          4. add_credit_entry(customer_id=<uuid>) ← amount=None reads from Redis

        ⚠️ IMPORTANT — if there is an active draft bill (items were added this session):
        DO NOT call this tool. Call finalize_bill(payment_mode='CREDIT', is_credit=True,
        customer_id=<uuid>) instead — it creates the bill record AND the khata entry together.
        (This tool will self-heal internally when a draft is open, but always use finalize_bill.)
        """
        from src.utils.guardrails import clean_uuid
        if not clean_uuid(customer_id):
            return "ERROR: customer_id must be a valid UUID. Call get_customer() or add_customer() first."

        # ── Self-heal: open draft bill exists but model called add_credit_entry directly ──
        # The correct path is finalize_bill(payment_mode='CREDIT', is_credit=True, customer_id=...)
        # which creates the bill, khata entry, and payment row atomically.
        # If the model skipped finalize_bill (e.g. routing landed in BILLING_CONFIRM while a draft
        # was still open), do it now so no bill or payment row is lost.
        if _draft_id_cell[0] is not None:
            draft_id = _draft_id_cell[0]
            finalized = await mcps.billing.finalize_bill(
                draft_bill_id=draft_id,
                payment_mode="CREDIT",
                telegram_user_id=tuid,
                is_credit=True,
                customer_id=customer_id,
            )
            _draft_id_cell[0] = None
            _bill_id_cell[0] = finalized.bill_id
            _last_confirmed_bill_cell[0] = finalized.bill_id
            return (
                f"bill_number={finalized.bill_number}\n"
                f"status=CONFIRMED\n"
                f"total=₹{finalized.total_amount:.2f} | payment_mode=CREDIT\n"
                f"{finalized.message}\n"
                f"✅ Bill is CONFIRMED. The khata entry has been recorded. "
                f"Inform the owner and ask if they need anything else."
            )

        redis = _get_redis()
        intent = None
        if redis:
            intent = await redis.get_pending_payment(tuid)

        # ── Hard guard: OVERPAYMENT intent in Redis means the caller picked the wrong tool.
        # add_credit_entry = customer owes shop (+delta). For overpayment, the shop owes the
        # customer — that is add_payment_entry (-delta). Redirect immediately so the correct
        # payment row and khata entry are recorded.
        if intent and intent.get("intent_type") == "OVERPAYMENT":
            _delta = float(intent.get("delta_amount", 0))
            _bill_num = intent.get("bill_number", "")
            return (
                f"⚠️ WRONG TOOL — bill {_bill_num} has an OVERPAYMENT of ₹{_delta:.2f}.\n"
                f"add_credit_entry records 'customer owes shop' — that is WRONG for overpayment.\n"
                f"The shop owes the customer ₹{_delta:.2f} change.\n"
                f"Call add_payment_entry(customer_id='{customer_id}') instead "
                f"(pass amount=None so the correct amount is read from Redis)."
            )

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
            # Self-heal: confirm the PENDING_PAYMENT bill before recording the khata entry.
            # Guard: skip this path if the pending bill is a CREDIT bill — those are already
            # CONFIRMED inside finalize_bill and should never reach here. Firing the
            # bypassed_confirm path on a CREDIT bill would create a spurious UNDERPAYMENT row
            # using the LLM-hallucinated `amount` instead of the real bill total.
            pending_bill_id = _bill_id_cell[0]
            bill_snap = await mcps.billing.get_bill_for_payment(pending_bill_id)
            _is_credit_bill = bill_snap and (
                bill_snap.get("is_credit") or
                (bill_snap.get("payment_mode") or "").upper() == "CREDIT"
            )
            if bill_snap and bill_snap.get("status") == "PENDING_PAYMENT" and not _is_credit_bill:
                confirm_result = await mcps.billing.confirm_payment(bill_id=pending_bill_id)
                if confirm_result.success:
                    _last_confirmed_bill_cell[0] = pending_bill_id
                    _bill_id_cell[0] = None
                    # Synthesise a minimal intent so the payment row is recorded below.
                    # Use the bill's own total_amount as authoritative — never trust the
                    # LLM-passed `amount` for the bill total (it may be hallucinated).
                    bill_total = float(bill_snap.get("total_amount", 0))
                    intent = {
                        "intent_type": "UNDERPAYMENT",
                        "bill_id": pending_bill_id,
                        "bill_number": bill_snap.get("bill_number"),
                        "bill_amount": bill_total,
                        "paid_amount": bill_total - float(amount),
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

        # Guard: negative amount means the model passed paid_amount instead of the balance.
        # Reject immediately — never pass negative values to the khata MCP.
        if amount is not None and float(amount) <= 0:
            return (
                f"ERROR: amount must be a positive number, got {amount}.\n"
                f"For underpayment-to-khata, pass amount=None — the balance is read from Redis automatically.\n"
                f"For overpayment-to-khata, pass amount=None — the change is read from Redis automatically."
            )

        redis = _get_redis()
        intent = None
        if redis:
            intent = await redis.get_pending_payment(tuid)

        # ── Hard guard: UNDERPAYMENT intent in Redis means the caller picked the wrong tool.
        # add_payment_entry = shop owes customer (-delta). For underpayment, the customer
        # owes the shop the remaining balance — that is add_credit_entry (+delta).
        if intent and intent.get("intent_type") == "UNDERPAYMENT":
            _delta = float(intent.get("delta_amount", 0))
            _bill_num = intent.get("bill_number", "")
            return (
                f"⚠️ WRONG TOOL — bill {_bill_num} has an UNDERPAYMENT of ₹{_delta:.2f}.\n"
                f"add_payment_entry records 'shop owes customer' — that is WRONG for underpayment.\n"
                f"The customer owes the shop ₹{_delta:.2f} balance.\n"
                f"Call add_credit_entry(customer_id='{customer_id}') instead "
                f"(pass amount=None so the correct balance is read from Redis)."
            )

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
            # Standalone KHATA_SETTLE — only record a payment row when there is genuinely
            # no bill context. If _bill_id_cell or _last_confirmed_bill_cell has a value,
            # this call is likely a billing-context misfire (model retried after a crash
            # and lost the Redis intent). In that case skip the payment row to avoid
            # a spurious KHATA_SETTLE entry duplicating a billing payment.
            _has_bill_context = bool(_bill_id_cell[0] or _last_confirmed_bill_cell[0])
            if not _has_bill_context:
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

        ⚠️ STOP — before calling this tool you MUST collect ALL missing fields from the owner.
        Use EXACTLY the copyable template below — DO NOT use a plain numbered list or bullet list.
        The template MUST be inside a fenced code block (triple backticks ```) so the owner
        can tap Copy on Telegram, edit the values in-place, and send it back.

        ❌ WRONG (a plain numbered list — owner cannot copy-edit this):
          1. Is it loose or packaged/branded?
          2. Unit – choose one: KG, G, ...
          3. Cost price ...

        ✅ RIGHT — send EXACTLY this, nothing else (pre-fill the product name the owner requested):
        ```
        1. Name - Salt
        2. Type - Branded / Loose
        3. Unit - PIECE / KG / G / L / ML / PACKET / DOZEN / BUNDLE
        4. Cost price (Rs.) - 10
        5. Selling price / MRP (Rs.) - 15
        6. GST rate - 5  (0 for loose, else 5 / 12 / 18 / 28 for branded)
        7. Brand - Company Name (skip if loose)
        8. Reorder level - 20
        9. Initial stock - 50
        ```

        NEVER call this tool with assumed or guessed values for any field.
        NEVER ask for description, category, or HSN code.
        ⚠️ SAME TURN RULE: once you have ALL 9 fields, call add_product() AND receive_stock()
        in the SAME turn — do NOT split across turns.
        The product_id is saved server-side — receive_stock() resolves it automatically.
        """
        from src.mcp.catalogue.models import AddProductResult
        try:
            result: AddProductResult = await mcps.catalogue.add_product(
                store_id=store_id, name=name, is_loose=is_loose, unit=unit,
                cost_price=cost_price, mrp=mrp, reorder_level=reorder_level,
                brand=brand, hsn_code=hsn_code, gst_rate=gst_rate, telegram_user_id=tuid,
            )
            _last_added_product_id_cell[0] = result.product_id
            return result.message + f"\n[internal product_id={result.product_id} — use for receive_stock + add_item_to_draft, do NOT show to owner]"
        except Exception as e:
            return f"ERROR: {e}"

    async def receive_stock(product_id: str, quantity: float, notes: str | None = None) -> str:
        """
        Add initial stock for a newly added product before it can be billed.
        - product_id: pass the product_id returned by add_product().
          The server resolves it automatically — pass any value if unsure.
        - quantity: initial stock quantity (REQUIRED — must ask owner if not stated)
        """
        from src.utils.guardrails import clean_uuid as _clean_uuid
        from src.mcp.inventory.models import ReceiveStockResult
        # Always prefer the server-authoritative product_id written by add_product()
        # this turn or in a recent turn — prevents stale LLM-hallucinated UUIDs reaching DB.
        resolved_pid: str | None = product_id if _clean_uuid(product_id) else None
        if _last_added_product_id_cell[0] and resolved_pid != _last_added_product_id_cell[0]:
            resolved_pid = _last_added_product_id_cell[0]
        if not resolved_pid:
            return (
                "ERROR: No product_id available. Call add_product() first, "
                "then call receive_stock() with the returned product_id."
            )
        try:
            result: ReceiveStockResult = await mcps.inventory.receive_stock(
                store_id=store_id, product_id=resolved_pid,
                quantity=quantity, notes=notes, telegram_user_id=tuid,
            )
            return result.message
        except Exception as e:
            return f"ERROR: {e}"

    # ── BILLING_CONFIRM intent — post-finalize tools (no active draft) ────────
    if intent == "BILLING_CONFIRM":
        return [
            search_products,
            amend_pending_bill,
            collect_balance_now, confirm_payment, cancel_bill, void_bill, void_bill_by_number, change_payment_mode,
            get_bill,
            add_customer, get_customer, add_credit_entry, add_payment_entry,
            get_payment_history, list_bills_for_customer, get_bills_by_date, generate_invoice_pdf,
            list_customers_with_balances,
        ]

    # ── BILLING (default) — draft-building tools ──────────────────────────────
    return [
        search_products, list_products, check_availability,
        create_draft_bill, add_item_to_draft, remove_item_from_draft,
        update_item_quantity, get_draft_bill, finalize_and_pay, finalize_bill,
        cancel_draft_bill, change_payment_mode,
        add_customer, get_customer, add_credit_entry, add_payment_entry,
        add_product, receive_stock,
        list_customers_with_balances,
        update_store,
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
    Used for single-customer prose contexts (payment history, settle khata, etc.).
    """
    if balance > 0:
        return f"{name} owes the shop ₹{balance:.2f}"
    if balance < 0:
        return f"Shop owes {name} ₹{abs(balance):.2f}"
    return f"{name}'s account is settled (₹0)"


def _balance_cols(balance: float) -> tuple[str, str]:
    """
    Return (signed_balance, owes_label) for table rendering.
    signed_balance: ₹+40.80 / ₹-40.80 / ₹0.00
    owes_label:     'Customer owes' / 'Shop owes' / 'Settled'
    """
    if balance > 0:
        return f"₹+{balance:.2f}", "Customer owes"
    if balance < 0:
        return f"₹-{abs(balance):.2f}", "Shop owes"
    return "₹0.00", "Settled"


# ─────────────────────────────────────────────────────────────────────────────
# Intent detection
# ─────────────────────────────────────────────────────────────────────────────

INTENT_KEYWORDS: dict[str, list[str]] = {
    "BILLING_CONFIRM": [
        "confirm payment", "confirm the payment", "payment confirmed", "payment done",
        "void bill", "void the bill", "cancel bill", "cancel the bill", "cancel",
        "undo bill", "reverse bill", "undo", "void", "payment received",
        "paid", "yes paid", "payment done",
        # Bill item amendment on a PENDING_PAYMENT bill
        "drop ", "remove item", "remove from bill", "take off", "delete item",
        "add to bill", "add item to bill", "add more to bill", "include in bill",
        "change item", "replace item", "update bill item", "amend bill",
        "wrong item", "wrong items", "change the bill",
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
        "catalogue", "list catalogue", "show catalogue", "view catalogue",
        "add product", "add item", "new product", "new item",
        "brand", "gst rate", "hsn", "mrp", "cost price", "reorder level",
        "edit product", "update product", "remove product",
        # Store/shop profile settings — update_store, update_owner_name, get_store_details
        "store details", "shop details", "store info", "shop info",
        "store name", "shop name", "store phone", "shop phone",
        "store address", "shop address", "update store", "update shop",
        "change store", "change shop", "store gstin", "shop gstin",
        "payment mode", "default payment", "owner name", "my name",
        "firstname", "first name", "lastname", "last name",
        "update name", "change name", "update profile", "change profile",
        "update owner", "change owner",
        # Default payment mode preference phrases
        "always assume", "always use cash", "always use upi", "always use credit",
        "default to cash", "default to upi", "default to credit",
        "assume cash", "assume upi", "assume credit",
        "set default payment", "change default payment", "set payment mode",
    ],
    "INVENTORY":  [
        "stock", "inventory", "restock", "low stock", "out of stock",
        "stock movement", "stock history", "movement history",
        "track movement", "track movements", "movements",
        "received stock", "add stock", "how much stock",
        "units left", "units available", "in stock",
        "full report", "all stock", "stock report",
        "running out", "running low", "reorder", "re-order",
        "need to reorder", "need reordering", "below reorder",
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
        # Sales overview / today's sales — previously fell through to BILLING
        "sales overview", "overview", "today sales", "today's sales",
        "sales today", "how much did i sell", "how much i sold",
        "how much was sold", "total sales", "total revenue",
        "daily report", "day report", "today report", "summary today",
        "overview today", "today overview", "today summary",
    ],
}

# These billing-mode words must never redirect away from BILLING
# even if they also appear in KHATA keywords by mistake.
_BILLING_PAYMENT_WORDS = frozenset({
    "cash", "upi", "credit", "card", "finalize", "finalise",
    "confirm bill", "confirm the bill", "complete bill", "done",
})

_PAID_PATTERN = _re.compile(r"\bpaid\b.*?\d|[\w]+ paid\b", _re.IGNORECASE)

# Normalises a customer name passed to lookup tools.
# Handles possessive forms so "pavans" and "pavan's" both find "Pavan".
#
# Two-pass strategy used at call sites (see _customer_lookup_with_fallback):
#   Pass 1 — try the name as-is
#   Pass 2 — if not found, try with apostrophe-possessive ("'s") stripped
#   Pass 3 — if still not found, try with bare trailing "s" stripped (min 4-char result)
# This avoids pre-mangling real names like "James" or "Thomas".
#
_POSSESSIVE_APOS_RE = _re.compile(r"'s$", _re.IGNORECASE)


def _possessive_variants(name: str) -> list[str]:
    """
    Return a list of name variants to try in order for customer lookup.
    First entry is always the original (or apostrophe-stripped); subsequent
    entries are fallbacks with trailing 's' removed.
    Phone numbers return a single-element list unchanged.
    """
    stripped = name.strip()
    if stripped.replace(" ", "").isdigit():
        return [stripped]

    variants: list[str] = []

    # Always try apostrophe-possessive stripped first ("pavan's" → "pavan")
    apos_stripped = _POSSESSIVE_APOS_RE.sub("", stripped).strip()
    if apos_stripped != stripped:
        # Had an apostrophe possessive — that's the canonical form; only one try needed
        return [apos_stripped]

    # No apostrophe: try original first, then with trailing 's' removed
    variants.append(stripped)
    if stripped.lower().endswith("s") and len(stripped[:-1]) >= 4:
        variants.append(stripped[:-1].strip())
    return variants

# Matches a bare 10-digit Indian phone number anywhere in the message.
# Used to keep "add to <name> - <phone>" messages in BILLING_CONFIRM when no draft
# is active — these are always overpayment/underpayment customer-resolution replies.
_PHONE_PATTERN = _re.compile(r"\b[6-9]\d{9}\b")

# Matches "add to <name>" with no product/item context — signals a khata/payment
# resolution reply (e.g. "add to Prakash - 7723197822") not a billing add-item.
_ADD_TO_NAME_PATTERN = _re.compile(
    r"\badd\s+to\s+[A-Za-z]",
    _re.IGNORECASE,
)

# Matches "change/switch/modify/update ... cash/upi/mode" or "use/pay ... cash/upi ... instead"
# Used by detect_intent to route payment-mode-change requests to BILLING_CONFIRM.
_CHANGE_MODE_PATTERN = _re.compile(
    r"\b(change|switch|update|modify)\b.{0,30}?(cash|upi|credit|payment mode|mode)\b"
    r"|\b(use|pay with|pay by)\b.{0,20}?(cash|upi)\b.{0,10}?\binstead\b",
    _re.IGNORECASE,
)


# Phrases the agent uses when asking for a customer name/phone to resolve an
# overpayment or underpayment. If the previous assistant turn contained any of
# these, the next reply (which may be just "kiran" or a phone number) must stay
# in BILLING_CONFIRM so add_payment_entry / add_credit_entry with Redis + payment
# recording are available — not the simpler BILLING-group variants.
_BILLING_CONFIRM_FOLLOWUP_PHRASES = (
    "customer's name",
    "customer name",
    "name or phone",
    "phone number",
    "10-digit",
    "10‑digit",
    "provide the customer",
    "khata account",
    "credit the extra",
    "add the extra",
    "add it to",
    "extra received",
    "change to customer",
    "return this change",
    "balance is still due",
    "balance due",
    "remaining balance",
    # Payment-amount prompts from finalize_and_pay / change_payment_mode returns.
    # When the agent has just asked "how much did the customer pay?" or stated the
    # bill total, the owner's numeric reply (e.g. "1100", "paid 500") must stay in
    # BILLING_CONFIRM so confirm_payment() is available — not fall back to BILLING
    # where the model may re-call change_payment_mode or finalize_and_pay instead.
    "how much did the customer pay",
    "how much did they pay",
    "bill total is",
    "how much was paid",
    "did the customer pay",
    "how much via",
    "customer pay via",
    "pay via upi",
    "pay via cash",
    "customer paid via",
)


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

# If the previous assistant turn contained any of these, the next reply (typically a bare
# date like "19th august" or "today") should stay in ANALYTICS so get_bills_by_date,
# get_daily_summary, etc. are available — not fall back to BILLING.
_ANALYTICS_FOLLOWUP_PHRASES = (
    "which date",
    "what date",
    "date would you like",
    "date range",
    "for which date",
    "report for",
    "summary for",
    "sales for",
    "bills for which",
    "specific date",
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
        # Only apply BILLING_CONFIRM followup routing when NO draft is active.
        # With an active draft, "provide customer name" means the credit-bill flow
        # (finalize_bill is in BILLING) — not post-payment over/underpayment resolution.
        if not has_active_draft and any(p in last_lower for p in _BILLING_CONFIRM_FOLLOWUP_PHRASES):
            return "BILLING_CONFIRM"
        if any(p in last_lower for p in _INVENTORY_FOLLOWUP_PHRASES):
            return "INVENTORY"
        if any(p in last_lower for p in _ANALYTICS_FOLLOWUP_PHRASES):
            return "ANALYTICS"

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
    #   (b) Bill lookup/PDF actions — split into two groups:
    #       - DRAFT-SAFE lookups: always route to BILLING_CONFIRM (historical/dated queries,
    #         PDF generation, customer lists — none of these refer to the current draft)
    #       - DRAFT-SENSITIVE lookups: "list bill", "show bill", "bill details", "get bill" —
    #         when a draft is active, these mean "show my current draft" → route to BILLING
    #         so get_draft_bill is available.

    # Keywords that are genuinely safe to route to BILLING_CONFIRM regardless of draft state:
    # they all refer to historical/dated bills, PDFs, or customer balance lists.
    _BILLING_CONFIRM_LOOKUP_SAFE = frozenset({
        # Date-scoped historical bills
        "bills for", "bill history", "purchase history", "purchases for",
        "bought by", "list purchase",
        "bill on", "bills on", "bills today", "bills for today",
        "todays bills", "today's bills", "today bill", "view today",
        "bills yesterday", "yesterdays bills", "yesterday's bills", "yesterday bill", "view yesterday",
        "bills for tomorrow", "list bill on", "show bill on",
        "bills this week", "bills last week",
        "list all bill", "all bill",
        # Invoice / PDF generation for a named/dated bill
        "generate bill", "print bill", "share bill", "bill pdf",
        "send bill", "pdf for", "pdf of", "invoice pdf",
        "send invoice", "generate invoice", "invoice for",
        # All-customer khata / balance listing
        "all khata", "all balances", "all balance", "list all khata",
        "all customers", "list customers", "show all customers",
        "who owes", "list khata", "show khata", "khata list",
        "outstanding balances", "all outstanding",
    })
    # Keywords that are DRAFT-SENSITIVE: with an active draft they mean "show current draft"
    # → must route to BILLING (get_draft_bill lives there); only route to BILLING_CONFIRM
    # when no draft is active (they then refer to the last confirmed bill or a named bill).
    _BILLING_CONFIRM_LOOKUP_DRAFT_SENSITIVE = frozenset({
        "list bill", "show bill",
        "get bill", "fetch bill", "show bill details", "bill details",
        "bill items", "show items", "list items",
        "bill summary", "view bill", "current bill",
        "show current bill", "what is on the bill", "what did i add",
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

    # Always safe to route to BILLING_CONFIRM regardless of draft state
    if any(kw in msg for kw in _BILLING_CONFIRM_LOOKUP_SAFE):
        return "BILLING_CONFIRM"
    # Draft-sensitive: only route to BILLING_CONFIRM when no draft is open
    if not has_active_draft and any(kw in msg for kw in _BILLING_CONFIRM_LOOKUP_DRAFT_SENSITIVE):
        return "BILLING_CONFIRM"
    # Fuzzy match: "list/show/all + balance(s)/khata/khta/udhar" — catches typos
    # like "list ass khta balances" or "all khata" that miss exact keyword matching.
    if _re.search(r"\b(list|show|all|get)\b.{0,20}\b(balance|khata|khta|udhar)\b", msg):
        return "BILLING_CONFIRM"
    if _re.search(r"\b(balance|khata|khta|udhar)\b.{0,20}\b(list|all|show|everyone|customers?)\b", msg):
        return "BILLING_CONFIRM"

    # Resolution keywords (pay now / collect now / add to khata / return change)
    # are ONLY valid when no draft is active.
    # With an active draft these words mean "finalise this draft bill" →
    # must stay in BILLING so finalize_and_pay / finalize_bill are available.
    if not has_active_draft:
        if any(kw in msg for kw in _BILLING_CONFIRM_RESOLUTION_KEYWORDS):
            return "BILLING_CONFIRM"
        # "khata" with any word(s) before it — catches "<name> khata",
        # "add to ramesh's khata", "add it to kalyan khata", etc.
        # Guard: only when no draft active — mid-draft "add to khata" means
        # "finalise as credit" → must stay in BILLING for finalize_bill.
        if _re.search(r"\bkhata\b", msg):
            return "BILLING_CONFIRM"
        # "add to <Name>" with a phone number — always an overpayment/underpayment
        # customer-resolution reply. Route to BILLING_CONFIRM so add_payment_entry /
        # add_credit_entry with Redis payment recording are available.
        if _ADD_TO_NAME_PATTERN.search(user_message) and _PHONE_PATTERN.search(user_message):
            return "BILLING_CONFIRM"
        # Bare 10-digit phone number — always a customer-name/phone resolution follow-up
        # (overpayment or underpayment flow) when no draft is active.
        if _PHONE_PATTERN.search(user_message) and not _re.search(r"\b(bill|item|product|stock|inventory)\b", msg):
            return "BILLING_CONFIRM"

    # Payment/cancellation keywords — only when no active draft
    if not has_active_draft:
        for kw in INTENT_KEYWORDS["BILLING_CONFIRM"]:
            if kw in msg:
                return "BILLING_CONFIRM"
        # "change payment mode" phrases — only valid on a PENDING_PAYMENT bill (no draft active)
        # change_payment_mode lives in the BILLING_CONFIRM tool group.
        if _CHANGE_MODE_PATTERN.search(user_message):
            return "BILLING_CONFIRM"

    for intent, keywords in INTENT_KEYWORDS.items():
        if intent == "BILLING_CONFIRM":
            continue  # already handled above
        # When a draft is active, KHATA keywords mean "finalise as credit" →
        # must stay in BILLING (finalize_bill is there), not route to KHATA.
        if intent == "KHATA" and has_active_draft:
            continue
        if any(kw in msg for kw in keywords):
            return intent
    return "BILLING"
