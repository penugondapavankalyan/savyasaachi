"""
Inventory MCP implementation.

Owns: inventory.inventory, inventory.stock_movements
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from src.db.supabase_client import get_client
from src.utils.guardrails import clean_optional_str, clean_positive_float, clean_quantity_for_unit


def _one(resp) -> Optional[dict]:
    """Safe helper: return first row or None. Handles supabase-py 2.x quirks."""
    if resp is None:
        return None
    data = resp.data
    if not data:
        return None
    return data[0] if isinstance(data, list) else data

from src.mcp.inventory.models import (
    AvailabilityResult,
    DecrementResult,
    LowStockItem,
    ReceiveStockResult,
    StockMovementRecord,
    StockResult,
)

if TYPE_CHECKING:
    from src.mcp.identity.identity_mcp import IdentityMCP
    from src.mcp.catalogue.catalogue_mcp import CatalogueMCP


class InventoryMCP:
    """All DB operations for the inventory domain."""

    def __init__(
        self,
        identity_mcp: "IdentityMCP | None" = None,
        catalogue_mcp: "CatalogueMCP | None" = None,
    ) -> None:
        self.db = get_client()
        self._identity = identity_mcp
        self._catalogue = catalogue_mcp

    # ------------------------------------------------------------------
    # Stock-in
    # ------------------------------------------------------------------

    async def receive_stock(
        self,
        store_id: str,
        product_id: str,
        quantity: float,
        telegram_user_id: Optional[int] = None,
        cost_price: Optional[float] = None,
        notes: Optional[str] = None,
    ) -> ReceiveStockResult:
        """
        Record receipt of new stock via the increment_stock RPC.
        The RPC atomically upserts inventory and writes a STOCK_IN movement.
        Advances workflow state to ACTIVE on the first ever stock-in.
        Only pass quantity and cost_price the owner explicitly stated.
        """
        # ── Guardrails — phase 1: basic positivity ───────────────────────
        quantity = clean_positive_float(quantity, "quantity")
        if cost_price is not None:
            cost_price = clean_positive_float(cost_price, "cost_price")
        notes = clean_optional_str(notes)

        # Get product metadata (needed before unit-based quantity check)
        prod_resp = (
            self.db.schema("catalogue")
            .table("products")
            .select("name, unit, cost_price, reorder_level")
            .eq("id", product_id)
            .limit(1)
            .execute()
        )
        prod = _one(prod_resp)
        if not prod:
            raise ValueError(f"Product {product_id} not found in catalogue.")
        product_name = prod["name"]
        unit = prod["unit"]

        # ── Guardrails — phase 2: unit-based quantity validation ─────────
        quantity = clean_quantity_for_unit(
            quantity, unit, "quantity", is_loose=prod.get("is_loose", True)
        )

        # Call the increment_stock RPC (atomic add + movement record)
        rpc_resp = self.db.rpc(
            "increment_stock",
            {
                "p_store_id": store_id,
                "p_product_id": product_id,
                "p_quantity": quantity,
                "p_reorder_level": prod["reorder_level"],
            },
        ).execute()
        new_qty = float(rpc_resp.data["new_quantity"])

        # Optionally update cost price in catalogue
        cost_price_updated = False
        if cost_price is not None and cost_price != float(prod["cost_price"]):
            self.db.schema("catalogue").table("products").update(
                {"cost_price": cost_price}
            ).eq("id", product_id).execute()
            cost_price_updated = True

        # Get the inventory record id for the result
        inv_resp = (
            self.db.schema("inventory")
            .table("inventory")
            .select("id")
            .eq("store_id", store_id)
            .eq("product_id", product_id)
            .limit(1)
            .execute()
        )
        inv_row = _one(inv_resp)
        inventory_id = inv_row["id"] if inv_row else None

        # Count STOCK_IN movements to detect first-ever stock-in for this store
        count_resp = (
            self.db.schema("inventory")
            .table("stock_movements")
            .select("id", count="exact")
            .eq("store_id", store_id)
            .eq("movement_type", "STOCK_IN")
            .execute()
        )
        stock_in_count = count_resp.count or 0
        workflow_advanced = False

        if stock_in_count == 1 and telegram_user_id and self._identity:
            advanced = await self._identity.advance_workflow_state(
                telegram_user_id, "ACTIVE"
            )
            workflow_advanced = advanced

        return ReceiveStockResult(
            inventory_id=inventory_id,
            product_name=product_name,
            quantity_received=quantity,
            new_total_quantity=new_qty,
            unit=unit,
            cost_price_updated=cost_price_updated,
            workflow_advanced=workflow_advanced,
            message=(
                f"Received {quantity} {unit.lower()} of {product_name}. "
                f"New stock: {new_qty} {unit.lower()}."
                + (
                    " Store is now fully set up! You can start billing."
                    if workflow_advanced
                    else ""
                )
            ),
        )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def get_stock(self, store_id: str, product_id: str) -> StockResult:
        """Return current stock level for a product."""
        inv_resp = (
            self.db.schema("inventory")
            .table("inventory")
            .select("product_id, quantity_in_stock, reorder_level, last_restocked_at")
            .eq("store_id", store_id)
            .eq("product_id", product_id)
            .limit(1)
            .execute()
        )
        # Product meta
        prod_resp = (
            self.db.schema("catalogue")
            .table("products")
            .select("name, brand, unit, reorder_level")
            .eq("id", product_id)
            .limit(1)
            .execute()
        )
        prod = _one(prod_resp) or {}

        resp_row = _one(inv_resp)
        if not resp_row:
            return StockResult(
                product_id=product_id,
                product_name=prod["name"],
                brand=prod.get("brand"),
                quantity_in_stock=0.0,
                unit=prod["unit"],
                reorder_level=float(prod["reorder_level"]),
                is_below_reorder=True,
                last_restocked_at=None,
            )
        row = resp_row
        qty = float(row["quantity_in_stock"])
        reorder = float(row["reorder_level"])
        return StockResult(
            product_id=product_id,
            product_name=prod["name"],
            brand=prod.get("brand"),
            quantity_in_stock=qty,
            unit=prod["unit"],
            reorder_level=reorder,
            is_below_reorder=qty <= reorder,
            last_restocked_at=row.get("last_restocked_at"),
        )

    async def check_availability(
        self,
        store_id: str,
        product_id: str,
        requested_quantity: float,
    ) -> AvailabilityResult:
        """
        Tool-layer oversell guard.  Returns FULL / PARTIAL / NONE.
        The DB-level atomic lock fires inside decrement_stock RPC.
        """
        stock = await self.get_stock(store_id, product_id)
        available = stock.quantity_in_stock

        if available >= requested_quantity:
            status = "FULL"
            can_partial = True
            msg = f"✅ {stock.product_name}: {requested_quantity} {stock.unit.lower()} available."
        elif available > 0:
            status = "PARTIAL"
            can_partial = True
            msg = (
                f"⚠️ Only {available} {stock.unit.lower()} of {stock.product_name} "
                f"available (you asked for {requested_quantity}). "
                "Add partial quantity or skip?"
            )
        else:
            status = "NONE"
            can_partial = False
            msg = f"❌ {stock.product_name} is out of stock."

        return AvailabilityResult(
            product_id=product_id,
            product_name=stock.product_name,
            requested_quantity=requested_quantity,
            available_quantity=available,
            unit=stock.unit,
            fulfillment_status=status,
            can_partially_fulfill=can_partial,
            message=msg,
        )

    async def decrement_stock(
        self,
        store_id: str,
        product_id: str,
        quantity: float,
        bill_id: str,
    ) -> DecrementResult:
        """
        Atomically decrement stock via the decrement_stock RPC.
        Called only by BillingMCP.finalize_bill — never directly by the agent.
        The RPC acquires a FOR UPDATE row lock, checks sufficiency, updates
        quantity, and inserts a SALE stock_movement — all in one transaction.
        """
        resp = self.db.rpc(
            "decrement_stock",
            {
                "p_store_id": store_id,
                "p_product_id": product_id,
                "p_quantity": quantity,
                "p_bill_id": bill_id,
            },
        ).execute()
        result = resp.data
        new_qty = float(result["new_quantity"])
        reorder_alert = bool(result["reorder_alert"])

        return DecrementResult(
            product_id=product_id,
            quantity_decremented=quantity,
            new_quantity=new_qty,
            reorder_alert=reorder_alert,
        )

    async def get_low_stock_items(self, store_id: str) -> list[LowStockItem]:
        """Return all items at or below their reorder level."""
        # Fetch all rows for this store — column-to-column comparison
        # (.lte("quantity_in_stock", "reorder_level") passes "reorder_level" as a
        # literal string value, causing a DB type error). Filter in Python instead.
        resp = (
            self.db.schema("inventory")
            .table("inventory")
            .select("product_id, quantity_in_stock, reorder_level")
            .eq("store_id", store_id)
            .order("quantity_in_stock")
            .execute()
        )
        inv_rows = [
            r for r in (resp.data or [])
            if float(r["quantity_in_stock"]) <= float(r["reorder_level"])
        ]

        # Batch-fetch all product metadata in one query (avoid N+1)
        product_ids = [r["product_id"] for r in inv_rows]
        prod_map: dict = {}
        if product_ids:
            prod_resp = (
                self.db.schema("catalogue")
                .table("products")
                .select("id, name, brand, unit")
                .in_("id", product_ids)
                .execute()
            )
            prod_map = {r["id"]: r for r in (prod_resp.data or [])}

        items = []
        for row in inv_rows:
            qty = float(row["quantity_in_stock"])
            reorder = float(row["reorder_level"])
            prod = prod_map.get(row["product_id"], {})
            if qty == 0:
                urgency = "OUT_OF_STOCK"
            elif reorder > 0 and qty <= reorder * 0.5:
                urgency = "CRITICAL"
            else:
                urgency = "LOW"
            items.append(
                LowStockItem(
                    product_id=row["product_id"],
                    product_name=prod.get("name", ""),
                    brand=prod.get("brand"),
                    quantity_in_stock=qty,
                    reorder_level=reorder,
                    unit=prod.get("unit", ""),
                    urgency=urgency,
                )
            )
        return items

    async def get_stock_movements(
        self,
        store_id: str,
        product_id: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 50,
    ) -> list[StockMovementRecord]:
        """Return stock movement audit trail for a product."""
        q = (
            self.db.schema("inventory")
            .table("stock_movements")
            .select("id, movement_type, quantity_delta, reference_id, reference_type, notes, created_at")
            .eq("store_id", store_id)
            .eq("product_id", product_id)
            .order("created_at", desc=True)
            .limit(limit)
        )
        if start_date:
            q = q.gte("created_at", start_date)
        if end_date:
            q = q.lte("created_at", end_date)
        resp = q.execute()
        return [
            StockMovementRecord(
                movement_id=r["id"],
                movement_type=r["movement_type"],
                quantity_delta=float(r["quantity_delta"]),
                reference_id=r.get("reference_id"),
                reference_type=r.get("reference_type"),
                notes=r.get("notes"),
                created_at=r["created_at"],
            )
            for r in (resp.data or [])
        ]

    async def get_all_stock(self, store_id: str) -> list[StockResult]:
        """Return current stock for all products in a store."""
        resp = (
            self.db.schema("inventory")
            .table("inventory")
            .select("product_id, quantity_in_stock, reorder_level, last_restocked_at")
            .eq("store_id", store_id)
            .order("quantity_in_stock")
            .execute()
        )
        inv_rows = resp.data or []

        # Batch-fetch product metadata in one query (avoids N+1)
        product_ids = [r["product_id"] for r in inv_rows]
        prod_map: dict = {}
        if product_ids:
            prod_resp = (
                self.db.schema("catalogue")
                .table("products")
                .select("id, name, brand, unit")
                .in_("id", product_ids)
                .execute()
            )
            prod_map = {r["id"]: r for r in (prod_resp.data or [])}

        results = []
        for row in inv_rows:
            prod = prod_map.get(row["product_id"], {})
            qty = float(row["quantity_in_stock"])
            reorder = float(row["reorder_level"])
            results.append(
                StockResult(
                    product_id=row["product_id"],
                    product_name=prod.get("name", ""),
                    brand=prod.get("brand"),
                    quantity_in_stock=qty,
                    unit=prod.get("unit", ""),
                    reorder_level=reorder,
                    is_below_reorder=qty <= reorder,
                    last_restocked_at=row.get("last_restocked_at"),
                )
            )
        return results
