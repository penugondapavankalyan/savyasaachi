"""
Billing MCP implementation.

Owns: billing.draft_bills, billing.draft_bill_items,
      billing.bills, billing.bill_items
Reads: billing.customers (owned by KhataMCP)

Calls internally: CatalogueMCP, InventoryMCP, KhataMCP, IdentityMCP, PaymentsMCP
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from src.utils.ist import date_range_iso
from typing import TYPE_CHECKING, Optional

from src.db.supabase_client import get_client
from src.utils.guardrails import (
    clean_optional_str, clean_payment_mode, clean_positive_float,
    clean_quantity_for_unit,
)


def _one(resp):
    """Safe helper: return first row or None. Handles supabase-py 2.x quirks."""
    if resp is None:
        return None
    data = resp.data
    if not data:
        return None
    return data[0] if isinstance(data, list) else data

from src.mcp.billing.models import (
    AddItemResult,
    BillDetailResult,
    BillItemDetail,
    BillSummaryResult,
    CancelResult,
    DraftBillDetailResult,
    DraftBillItemDetail,
    DraftBillItemResult,
    DraftBillResult,
    FinalizedBillResult,
    RemoveItemResult,
    UpdateItemResult,
)
from src.utils.gst import aggregate_gst, compute_line_gst

if TYPE_CHECKING:
    from src.mcp.catalogue.catalogue_mcp import CatalogueMCP
    from src.mcp.identity.identity_mcp import IdentityMCP
    from src.mcp.inventory.inventory_mcp import InventoryMCP
    from src.mcp.khata.khata_mcp import KhataMCP
    from src.mcp.payments.payments_mcp import PaymentsMCP


class BillingMCP:
    """Full bill lifecycle — draft through finalization."""

    def __init__(
        self,
        catalogue_mcp: "CatalogueMCP",
        inventory_mcp: "InventoryMCP",
        khata_mcp: "KhataMCP",
        identity_mcp: "IdentityMCP",
        payments_mcp: "PaymentsMCP | None" = None,
    ) -> None:
        self.db = get_client()
        self._catalogue = catalogue_mcp
        self._inventory = inventory_mcp
        self._khata = khata_mcp
        self._identity = identity_mcp
        self._payments = payments_mcp  # injected after PaymentsMCP is constructed

    def set_payments_mcp(self, payments_mcp: "PaymentsMCP") -> None:
        """Late-bind PaymentsMCP after construction (avoids circular dep in MCPInstances)."""
        self._payments = payments_mcp

    # ------------------------------------------------------------------
    # Draft bill management
    # ------------------------------------------------------------------

    async def create_draft_bill(
        self,
        store_id: str,
        telegram_user_id: int,
        workflow_id: Optional[str] = None,
    ) -> DraftBillResult:
        """
        Create a new draft bill or return the existing open one.
        Idempotent: if an open, non-expired draft already exists for
        this user → return it unchanged (multi-turn continuity).
        Never invent store_id or telegram_user_id — use values from context only.
        """
        # ── Guardrails ──────────────────────────────────────────────────
        workflow_id = clean_optional_str(workflow_id)
        # Check for existing open non-expired draft
        existing_resp = (
            self.db.schema("billing")
            .table("draft_bills")
            .select("id, store_id, workflow_id, status, expires_at")
            .eq("telegram_user_id", telegram_user_id)
            .eq("status", "OPEN")
            .gt("expires_at", datetime.now(timezone.utc).isoformat())
            .limit(1)
            .execute()
        )
        existing_draft = _one(existing_resp)
        if existing_draft:
            draft = existing_draft
            items = await self._get_draft_items_basic(draft["id"], draft["store_id"])
            estimated_total = sum(i.line_subtotal for i in items)
            return DraftBillResult(
                draft_bill_id=draft["id"],
                workflow_id=draft["workflow_id"],
                status=draft["status"],
                items=items,
                item_count=len(items),
                estimated_total=estimated_total,
                already_existed=True,
                expires_at=draft["expires_at"],
            )

        # Check for expired open draft → mark EXPIRED
        expired = (
            self.db.schema("billing")
            .table("draft_bills")
            .select("id")
            .eq("telegram_user_id", telegram_user_id)
            .eq("status", "OPEN")
            .lte("expires_at", datetime.now(timezone.utc).isoformat())
            .execute()
        )
        for row in expired.data or []:
            self.db.schema("billing").table("draft_bills").update(
                {"status": "EXPIRED"}
            ).eq("id", row["id"]).execute()

        # Create new draft
        new_workflow_id = workflow_id or str(uuid.uuid4())
        resp = (
            self.db.schema("billing")
            .table("draft_bills")
            .insert(
                {
                    "store_id": store_id,
                    "telegram_user_id": telegram_user_id,
                    "workflow_id": new_workflow_id,
                    "status": "OPEN",
                }
            )
            .execute()
        )
        draft = resp.data[0]
        draft_bill_id = draft["id"]

        # Record in workflow_state
        await self._identity.set_active_draft_bill(telegram_user_id, draft_bill_id)

        return DraftBillResult(
            draft_bill_id=draft_bill_id,
            workflow_id=new_workflow_id,
            status="OPEN",
            items=[],
            item_count=0,
            estimated_total=0.0,
            already_existed=False,
            expires_at=draft["expires_at"],
        )

    async def add_item_to_draft(
        self,
        draft_bill_id: str,
        product_id: str,
        quantity: float,
        is_partial_fulfillment: bool = False,
    ) -> AddItemResult:
        """
        Add a product to the draft bill.
        Returns PARTIAL/NONE if stock is insufficient.
        Stock is NOT decremented here — only on finalize.
        Only pass quantity explicitly stated by the owner — never guess quantities.
        """
        # ── Guardrails — phase 1: basic positivity ───────────────────────
        quantity = clean_positive_float(quantity, "quantity")

        # 1. Get draft to know store_id
        draft = await self._get_draft_header(draft_bill_id)
        store_id = draft["store_id"]

        # 2. Get product details (needed for unit-based quantity check)
        product = await self._catalogue.get_product(store_id, product_id)

        # ── Guardrails — phase 2: unit-based quantity validation ─────────
        quantity = clean_quantity_for_unit(
            quantity, product.unit, "quantity", is_loose=product.is_loose
        )

        # 3. Availability check
        availability = await self._inventory.check_availability(
            store_id, product_id, quantity
        )

        if availability.fulfillment_status == "NONE":
            return AddItemResult(
                draft_item_id=None,
                product_name=product.name,
                quantity=quantity,
                unit=product.unit,
                unit_price=product.mrp,
                gst_rate=product.gst_rate,
                line_subtotal=0.0,
                availability_status="NONE",
                available_quantity=0.0,
                message=availability.message,
            )

        if availability.fulfillment_status == "PARTIAL" and not is_partial_fulfillment:
            return AddItemResult(
                draft_item_id=None,
                product_name=product.name,
                quantity=quantity,
                unit=product.unit,
                unit_price=product.mrp,
                gst_rate=product.gst_rate,
                line_subtotal=0.0,
                availability_status="PARTIAL",
                available_quantity=availability.available_quantity,
                message=availability.message,
            )

        # Use available quantity if partial
        actual_qty = (
            availability.available_quantity
            if availability.fulfillment_status == "PARTIAL"
            else quantity
        )
        line_subtotal = round(actual_qty * product.mrp, 2)

        # 4. Upsert draft_bill_items (one line per product)
        resp = (
            self.db.schema("billing")
            .table("draft_bill_items")
            .upsert(
                {
                    "draft_bill_id": draft_bill_id,
                    "product_id": product_id,
                    "quantity": actual_qty,
                    "unit_price": product.mrp,
                    "gst_rate": product.gst_rate,
                    "is_partial_fulfillment": is_partial_fulfillment
                    or availability.fulfillment_status == "PARTIAL",
                    "available_quantity": availability.available_quantity,
                },
                on_conflict="draft_bill_id,product_id",
            )
            .execute()
        )
        item_id = resp.data[0]["id"]

        return AddItemResult(
            draft_item_id=item_id,
            product_name=product.name,
            quantity=actual_qty,
            unit=product.unit,
            unit_price=product.mrp,
            gst_rate=product.gst_rate,
            line_subtotal=line_subtotal,
            availability_status=availability.fulfillment_status,
            available_quantity=availability.available_quantity,
            message=f"✅ Added {actual_qty} {product.unit.lower()} of {product.name} to the bill.",
        )

    async def remove_item_from_draft(
        self, draft_bill_id: str, product_id: str
    ) -> RemoveItemResult:
        """Remove a product line from the draft."""
        # Check it exists
        existing_resp = (
            self.db.schema("billing")
            .table("draft_bill_items")
            .select("id")
            .eq("draft_bill_id", draft_bill_id)
            .eq("product_id", product_id)
            .limit(1)
            .execute()
        )
        draft = await self._get_draft_header(draft_bill_id)
        product = await self._catalogue.get_product(draft["store_id"], product_id)

        if not _one(existing_resp):
            return RemoveItemResult(
                success=False,
                product_name=product.name,
                message=f"{product.name} is not in this bill.",
            )

        self.db.schema("billing").table("draft_bill_items").delete().eq(
            "draft_bill_id", draft_bill_id
        ).eq("product_id", product_id).execute()

        return RemoveItemResult(
            success=True,
            product_name=product.name,
            message=f"Removed {product.name} from the bill.",
        )

    async def update_item_quantity(
        self,
        draft_bill_id: str,
        product_id: str,
        new_quantity: float,
    ) -> UpdateItemResult:
        """Change the quantity of an existing draft item, re-checking availability."""
        draft = await self._get_draft_header(draft_bill_id)
        store_id = draft["store_id"]
        product = await self._catalogue.get_product(store_id, product_id)

        # Unit-based quantity validation (same rules as add_item_to_draft)
        new_quantity = clean_quantity_for_unit(
            new_quantity, product.unit, "new_quantity", is_loose=product.is_loose
        )

        availability = await self._inventory.check_availability(
            store_id, product_id, new_quantity
        )

        if availability.fulfillment_status == "NONE":
            return UpdateItemResult(
                draft_item_id="",
                product_name=product.name,
                new_quantity=new_quantity,
                availability_status="NONE",
                message=availability.message,
            )

        actual_qty = (
            availability.available_quantity
            if availability.fulfillment_status == "PARTIAL"
            else new_quantity
        )

        resp = (
            self.db.schema("billing")
            .table("draft_bill_items")
            .update({"quantity": actual_qty})
            .eq("draft_bill_id", draft_bill_id)
            .eq("product_id", product_id)
            .execute()
        )
        item_id = resp.data[0]["id"] if resp.data else ""

        return UpdateItemResult(
            draft_item_id=item_id,
            product_name=product.name,
            new_quantity=actual_qty,
            availability_status=availability.fulfillment_status,
            message=f"Updated {product.name} to {actual_qty} {product.unit.lower()}."
            + (
                f" (only {availability.available_quantity} available)"
                if availability.fulfillment_status == "PARTIAL"
                else ""
            ),
        )

    async def get_draft_bill(self, draft_bill_id: str) -> DraftBillDetailResult:
        """Return full draft with GST computations (pre-tax preview)."""
        draft = await self._get_draft_header(draft_bill_id)
        store_id = draft["store_id"]

        # Fetch draft items without cross-schema join (PostgREST does not support
        # embedded resources that span schemas). Bulk-fetch product metadata.
        items_resp = (
            self.db.schema("billing")
            .table("draft_bill_items")
            .select(
                "id, product_id, quantity, unit_price, gst_rate, "
                "is_partial_fulfillment, available_quantity"
            )
            .eq("draft_bill_id", draft_bill_id)
            .execute()
        )
        bill_rows = items_resp.data or []

        # Bulk-fetch product metadata from catalogue (avoid N+1)
        product_ids = [r["product_id"] for r in bill_rows]
        prod_map: dict = {}
        if product_ids:
            try:
                prod_resp = (
                    self.db.schema("catalogue")
                    .table("products")
                    .select("id, name, unit")
                    .in_("id", product_ids)
                    .execute()
                )
                prod_map = {r["id"]: r for r in (prod_resp.data or [])}
            except Exception:
                pass

        detailed_items = []
        for row in bill_rows:
            prod = prod_map.get(row["product_id"], {})
            qty = float(row["quantity"])
            price = float(row["unit_price"])
            rate = float(row["gst_rate"])
            gst = compute_line_gst(qty, price, rate)
            detailed_items.append(
                DraftBillItemDetail(
                    draft_item_id=row["id"],
                    product_id=row["product_id"],
                    product_name=prod.get("name", ""),
                    quantity=qty,
                    unit=prod.get("unit", ""),
                    unit_price=price,
                    gst_rate=rate,
                    line_subtotal=round(qty * price, 2),
                    is_partial_fulfillment=row["is_partial_fulfillment"],
                    taxable_value=gst["taxable_value"],
                    cgst_amount=gst["cgst_amount"],
                    sgst_amount=gst["sgst_amount"],
                    line_total=gst["line_total"],
                )
            )

        totals = aggregate_gst(
            [
                {
                    "taxable_value": i.taxable_value,
                    "cgst_amount": i.cgst_amount,
                    "sgst_amount": i.sgst_amount,
                    "line_total": i.line_total,
                }
                for i in detailed_items
            ]
        ) if detailed_items else {"subtotal": 0, "total_cgst": 0, "total_sgst": 0, "total_amount": 0}

        return DraftBillDetailResult(
            draft_bill_id=draft_bill_id,
            workflow_id=draft["workflow_id"],
            status=draft["status"],
            items=detailed_items,
            subtotal=totals["subtotal"],
            total_cgst=totals["total_cgst"],
            total_sgst=totals["total_sgst"],
            total_amount=totals["total_amount"],
            expires_at=draft["expires_at"],
        )

    # ------------------------------------------------------------------
    # Finalization
    # ------------------------------------------------------------------

    async def finalize_bill(
        self,
        draft_bill_id: str,
        payment_mode: str,
        telegram_user_id: int,
        payment_reference: Optional[str] = None,
        is_credit: bool = False,
        customer_id: Optional[str] = None,
    ) -> FinalizedBillResult:
        """
        The most critical operation. Converts draft → permanent bill.

        Idempotency: checks bills.workflow_id before doing anything.
        Stock: decremented via the decrement_stock RPC (row-level lock per item).
        Khata: credit entry created if is_credit=True.
        Only use payment_mode the owner explicitly stated. Never invent customer_id.
        """
        # ── Guardrails ──────────────────────────────────────────────────
        payment_mode = clean_payment_mode(payment_mode)
        payment_reference = clean_optional_str(payment_reference)
        if is_credit and not customer_id:
            raise ValueError(
                "Credit bill requires a verified customer with a phone number. "
                "Please call get_customer or add_customer (with phone) first, "
                "then pass the customer_id here. "
                "Credit cannot be extended without a valid phone number on file."
            )

        # 1. Load draft
        draft = await self._get_draft_header(draft_bill_id)
        workflow_id = draft["workflow_id"]
        store_id = draft["store_id"]

        # 2. Idempotency check — was this already finalized?
        existing_bill_resp = (
            self.db.schema("billing")
            .table("bills")
            .select("*")
            .eq("workflow_id", workflow_id)
            .limit(1)
            .execute()
        )
        existing_bill = _one(existing_bill_resp)
        if existing_bill:
            bill = existing_bill
            items = await self._get_bill_items(bill["id"])
            return FinalizedBillResult(
                bill_id=bill["id"],
                bill_number=bill["bill_number"],
                workflow_id=bill["workflow_id"],
                items=items,
                subtotal=float(bill["subtotal"]),
                total_cgst=float(bill["total_cgst"]),
                total_sgst=float(bill["total_sgst"]),
                total_amount=float(bill["total_amount"]),
                payment_mode=bill["payment_mode"],
                payment_reference=bill.get("payment_reference"),
                is_credit=bill["is_credit"],
                already_finalized=True,
                message=f"Bill {bill['bill_number']} was already finalized.",
            )

        # 3. Validate draft is still OPEN and not expired
        if draft["status"] != "OPEN":
            raise ValueError(
                f"Draft bill is {draft['status']} — cannot finalize."
            )

        # 4. Load draft items
        draft_detail = await self.get_draft_bill(draft_bill_id)
        if not draft_detail.items:
            raise ValueError("Cannot finalize an empty bill.")

        # 5. Generate bill number
        bill_number_resp = self.db.rpc(
            "generate_bill_number", {"p_store_id": store_id}
        ).execute()
        bill_number = bill_number_resp.data
        if not bill_number:
            raise ValueError(
                f"generate_bill_number RPC returned empty result for store {store_id}. "
                "Check that the RPC exists and the store_id is valid."
            )

        # 6. Insert bill record — status starts as PENDING_PAYMENT
        bill_resp = (
            self.db.schema("billing")
            .table("bills")
            .insert(
                {
                    "store_id": store_id,
                    "bill_number": bill_number,
                    "telegram_user_id": telegram_user_id,
                    "workflow_id": workflow_id,
                    "customer_id": customer_id,
                    "subtotal": draft_detail.subtotal,
                    "total_cgst": draft_detail.total_cgst,
                    "total_sgst": draft_detail.total_sgst,
                    "total_discount": 0.00,
                    "total_amount": draft_detail.total_amount,
                    "payment_mode": payment_mode.upper(),
                    "payment_reference": payment_reference,
                    "is_credit": is_credit,
                    "status": "PENDING_PAYMENT",
                }
            )
            .execute()
        )
        if not bill_resp.data:
            raise ValueError(
                f"Failed to insert bill record for draft {draft_bill_id}. "
                "DB returned empty data — check RLS policies on billing.bills."
            )
        bill_id = bill_resp.data[0]["id"]

        # 7. Insert bill_items and decrement stock
        bill_items: list[BillItemDetail] = []
        for item in draft_detail.items:
            # Get product snapshot data
            product = await self._catalogue.get_product(store_id, item.product_id)
            gst = compute_line_gst(item.quantity, item.unit_price, item.gst_rate)

            bi_resp = (
                self.db.schema("billing")
                .table("bill_items")
                .insert(
                    {
                        "bill_id": bill_id,
                        "product_id": item.product_id,
                        "product_name_snapshot": product.name,
                        "brand_snapshot": product.brand,
                        "unit_snapshot": product.unit,
                        "hsn_code_snapshot": product.hsn_code,
                        "quantity": item.quantity,
                        "unit_price": item.unit_price,
                        "gst_rate": item.gst_rate,
                        "taxable_value": gst["taxable_value"],
                        "cgst_amount": gst["cgst_amount"],
                        "sgst_amount": gst["sgst_amount"],
                        "line_total": gst["line_total"],
                    }
                )
                .execute()
            )
            bi = bi_resp.data[0]
            bill_items.append(
                BillItemDetail(
                    bill_item_id=bi["id"],
                    product_id=item.product_id,
                    product_name=product.name,
                    brand=product.brand,
                    unit=product.unit,
                    quantity=item.quantity,
                    unit_price=item.unit_price,
                    gst_rate=item.gst_rate,
                    taxable_value=gst["taxable_value"],
                    cgst_amount=gst["cgst_amount"],
                    sgst_amount=gst["sgst_amount"],
                    line_total=gst["line_total"],
                )
            )

            # Decrement stock via atomic RPC
            decrement = await self._inventory.decrement_stock(
                store_id=store_id,
                product_id=item.product_id,
                quantity=item.quantity,
                bill_id=bill_id,
            )

            # Reorder alert
            if decrement.reorder_alert:
                # Recorded; Telegram notification is sent by the agent/handler
                pass

        # 8. Mark draft as CONFIRMED
        self.db.schema("billing").table("draft_bills").update(
            {"status": "CONFIRMED"}
        ).eq("id", draft_bill_id).execute()

        # 9. Clear active_draft_bill_id
        await self._identity.set_active_draft_bill(telegram_user_id, None)

        # 10. If credit bill → auto-confirm + create khata entry + record payment
        if is_credit and customer_id:
            # Credit bills are immediately CONFIRMED — the debt is recorded in khata.
            # No cash/UPI payment is expected, so confirm_payment() must NOT be called.
            self.db.rpc("confirm_payment", {"p_bill_id": bill_id}).execute()

            khata_entry = await self._khata.add_credit_entry(
                store_id=store_id,
                customer_id=customer_id,
                amount=draft_detail.total_amount,
                reference_bill_id=bill_id,
                notes=f"Credit sale — bill {bill_number}",
            )

            # Record KHATA payment row in payments.payments
            if self._payments:
                await self._payments.record_payment(
                    store_id=store_id,
                    bill_id=bill_id,
                    bill_number=bill_number,
                    customer_id=customer_id,
                    khata_entry_id=khata_entry.entry_id,
                    paid_amount=0.0,
                    payment_mode=payment_mode.upper(),
                    payment_type="KHATA",
                    payment_status="CONFIRMED",
                    subtotal=draft_detail.subtotal,
                    total_gst=round(draft_detail.total_cgst + draft_detail.total_sgst, 2),
                    bill_amount=draft_detail.total_amount,
                    change_amount=0.0,
                    balance_due=draft_detail.total_amount,
                )

        return FinalizedBillResult(
            bill_id=bill_id,
            bill_number=bill_number,
            workflow_id=workflow_id,
            items=bill_items,
            subtotal=draft_detail.subtotal,
            total_cgst=draft_detail.total_cgst,
            total_sgst=draft_detail.total_sgst,
            total_amount=draft_detail.total_amount,
            payment_mode=payment_mode.upper(),
            payment_reference=payment_reference,
            is_credit=is_credit,
            already_finalized=False,
            message=(
                f"✅ Bill {bill_number} finalized. Total: ₹{draft_detail.total_amount:.2f} | {payment_mode.upper()}"
                + (f" | CREDIT — bill CONFIRMED, ₹{draft_detail.total_amount:.2f} added to customer khata." if is_credit else
                   " | Status: PENDING_PAYMENT — call confirm_payment() once cash/UPI payment is received.")
            ),
        )

    async def confirm_payment(self, bill_id: str) -> CancelResult:
        """
        Flip a PENDING_PAYMENT bill to CONFIRMED via the DB RPC.
        Called by tool_registry confirm_payment tool AFTER PaymentsMCP.record_payment().
        Does NOT insert a payments row — that is done by the tool_registry flow.
        """
        resp = self.db.rpc("confirm_payment", {"p_bill_id": bill_id}).execute()
        result = resp.data
        return CancelResult(
            success=bool(result.get("success", False)),
            message=result.get("message", "Unknown error from confirm_payment RPC."),
        )

    async def get_bill_for_payment(self, bill_id: str) -> dict | None:
        """
        Return raw bill row for payment processing.
        Used by tool_registry to get bill financial details before
        calling PaymentsMCP.record_payment().
        """
        resp = (
            self.db.schema("billing")
            .table("bills")
            .select("id, bill_number, store_id, customer_id, subtotal, total_cgst, total_sgst, total_amount, payment_mode, payment_reference, status, is_credit")
            .eq("id", bill_id)
            .limit(1)
            .execute()
        )
        return _one(resp)

    async def link_bill_customer(self, bill_id: str, customer_id: str) -> CancelResult:
        """
        Set customer_id on a cash/UPI bill that was finalized without one.
        Called after underpayment or overpayment identifies the customer.
        Idempotent — safe to call even if already linked.
        """
        resp = self.db.rpc(
            "set_bill_customer",
            {"p_bill_id": bill_id, "p_customer_id": customer_id},
        ).execute()
        result = resp.data
        return CancelResult(
            success=bool(result.get("success", False)),
            message=result.get("message", "Unknown error from set_bill_customer RPC."),
        )

    async def change_payment_mode(
        self,
        bill_id: str,
        new_payment_mode: str,
        telegram_user_id: int,
    ) -> FinalizedBillResult:
        """
        Change the payment mode of a PENDING_PAYMENT bill.

        Because billing.bills is immutable (only status transitions allowed),
        this is done by:
          1. Cancelling the PENDING_PAYMENT bill (restores stock).
          2. Creating a new draft from the cancelled bill's items.
          3. Finalizing the new draft with the new payment mode.

        Returns the new FinalizedBillResult. The old bill_id is gone — the
        caller should update their bill_id reference to the new bill.
        """
        new_payment_mode = clean_payment_mode(new_payment_mode)

        # 1. Load the bill and its items before cancelling
        bill_resp = (
            self.db.schema("billing")
            .table("bills")
            .select("*")
            .eq("id", bill_id)
            .limit(1)
            .execute()
        )
        bill = _one(bill_resp)
        if not bill:
            raise ValueError(f"Bill {bill_id} not found.")
        if bill["status"] != "PENDING_PAYMENT":
            raise ValueError(
                f"Only PENDING_PAYMENT bills can have their payment mode changed. "
                f"This bill is {bill['status']}."
            )

        store_id = bill["store_id"]
        items = await self._get_bill_items(bill_id)
        if not items:
            raise ValueError("Cannot change payment mode — bill has no items.")

        # 2. Cancel the existing bill (restores stock)
        cancel_resp = self.db.rpc("cancel_bill", {"p_bill_id": bill_id}).execute()
        cancel_result = cancel_resp.data
        if not cancel_result.get("success"):
            raise RuntimeError(
                f"Failed to cancel bill during payment mode change: "
                f"{cancel_result.get('message', 'unknown error')}"
            )

        # 3. Create a new draft
        draft = await self.create_draft_bill(
            store_id=store_id, telegram_user_id=telegram_user_id
        )
        new_draft_id = draft.draft_bill_id

        # 4. Re-add all items to the new draft
        for item in items:
            if not item.product_id:
                continue  # skip if product was deleted from catalogue
            await self.add_item_to_draft(
                draft_bill_id=new_draft_id,
                store_id=store_id,
                product_id=item.product_id,
                quantity=item.quantity,
            )

        # 5. Finalize with the new payment mode
        return await self.finalize_bill(
            draft_bill_id=new_draft_id,
            payment_mode=new_payment_mode,
            telegram_user_id=telegram_user_id,
        )

    async def cancel_bill(self, bill_id: str) -> CancelResult:
        """
        Cancel a PENDING_PAYMENT bill before payment is confirmed.
        Restores stock and reverses any khata credit entry.
        Also inserts a CANCELLED payment row for audit purposes.
        """
        # Get bill details before cancelling (for payment row snapshot)
        bill = await self.get_bill_for_payment(bill_id)

        resp = self.db.rpc("cancel_bill", {"p_bill_id": bill_id}).execute()
        result = resp.data
        success = bool(result.get("success", False))

        # Insert CANCELLED payment row for audit trail
        if success and bill and self._payments:
            await self._payments.record_payment(
                store_id=bill["store_id"],
                bill_id=bill_id,
                bill_number=bill.get("bill_number"),
                customer_id=bill.get("customer_id"),
                paid_amount=0.0,
                payment_mode=bill["payment_mode"],
                payment_type="EXACT",
                payment_status="CANCELLED",
                subtotal=float(bill["subtotal"]) if bill.get("subtotal") else None,
                total_gst=round(
                    float(bill.get("total_cgst", 0)) + float(bill.get("total_sgst", 0)), 2
                ),
                bill_amount=float(bill["total_amount"]) if bill.get("total_amount") else None,
                change_amount=0.0,
                balance_due=0.0,
            )

        return CancelResult(
            success=success,
            message=result.get("message", "Unknown error from cancel_bill RPC."),
        )

    async def void_bill(self, bill_id: str) -> CancelResult:
        """
        Void a CONFIRMED bill — full reversal after payment.
        Restores stock and reverses khata credit entry.
        Also inserts a REFUNDED payment row for audit purposes.

        Refund amount = paid_amount - change_amount from the original payment row.
        This ensures only the cash actually received (minus any change already returned)
        is recorded as refunded — not the full bill amount.

        Examples:
          Underpayment ₹300 on ₹400 bill  → refund ₹300  (300 - 0)
          Overpayment ₹400 on ₹350, ₹50 change returned → refund ₹350  (400 - 50)
          Overpayment ₹500 on ₹400, ₹100 to khata → refund ₹500  (500 - 0)
        """
        # Get bill details before voiding (for payment row snapshot)
        bill = await self.get_bill_for_payment(bill_id)

        resp = self.db.rpc("void_bill", {"p_bill_id": bill_id}).execute()
        result = resp.data
        success = bool(result.get("success", False))

        # Insert REFUNDED payment row for audit trail
        if success and bill and self._payments:
            # Look up the original payment row to get what the customer actually paid
            # and how much change was already returned to them.
            original_payment = await self._payments.get_payment_by_bill(bill_id)
            if original_payment:
                # Refund = what customer handed over minus change already given back
                refund_amount = round(
                    original_payment.paid_amount - original_payment.change_amount, 2
                )
            else:
                # No payment row (e.g. CREDIT bill voided) — fall back to bill total
                refund_amount = float(bill["total_amount"]) if bill.get("total_amount") else 0.0

            await self._payments.record_payment(
                store_id=bill["store_id"],
                bill_id=bill_id,
                bill_number=bill.get("bill_number"),
                customer_id=bill.get("customer_id"),
                paid_amount=refund_amount,
                payment_mode=bill["payment_mode"],
                payment_type="EXACT",
                payment_status="REFUNDED",
                subtotal=float(bill["subtotal"]) if bill.get("subtotal") else None,
                total_gst=round(
                    float(bill.get("total_cgst", 0)) + float(bill.get("total_sgst", 0)), 2
                ),
                bill_amount=float(bill["total_amount"]) if bill.get("total_amount") else None,
                change_amount=0.0,
                balance_due=0.0,
            )

        return CancelResult(
            success=success,
            message=result.get("message", "Unknown error from void_bill RPC."),
        )

    async def cancel_draft_bill(self, draft_bill_id: str) -> CancelResult:
        """Cancel an open draft (no stock impact)."""
        draft_resp = (
            self.db.schema("billing")
            .table("draft_bills")
            .select("status, telegram_user_id")
            .eq("id", draft_bill_id)
            .limit(1)
            .execute()
        )
        draft_row = _one(draft_resp)
        if not draft_row:
            return CancelResult(success=False, message="Draft bill not found.")
        if draft_row["status"] != "OPEN":
            return CancelResult(
                success=False,
                message=f"Draft is already {draft_row['status']}.",
            )
        self.db.schema("billing").table("draft_bills").update(
            {"status": "CANCELLED"}
        ).eq("id", draft_bill_id).execute()

        tuid = draft_row.get("telegram_user_id")
        if tuid:
            await self._identity.set_active_draft_bill(tuid, None)

        return CancelResult(success=True, message="Draft bill cancelled.")

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def get_bill(self, bill_id: str) -> BillDetailResult:
        """Return a finalized bill with all items."""
        bill_resp = (
            self.db.schema("billing")
            .table("bills")
            .select("*")
            .eq("id", bill_id)
            .limit(1)
            .execute()
        )
        bill = _one(bill_resp)
        if not bill:
            raise ValueError(f"Bill {bill_id} not found.")
        items = await self._get_bill_items(bill_id)
        return BillDetailResult(
            bill_id=bill["id"],
            bill_number=bill["bill_number"],
            store_id=bill["store_id"],
            workflow_id=bill["workflow_id"],
            customer_id=bill.get("customer_id"),
            items=items,
            subtotal=float(bill["subtotal"]),
            total_cgst=float(bill["total_cgst"]),
            total_sgst=float(bill["total_sgst"]),
            total_discount=float(bill["total_discount"]),
            total_amount=float(bill["total_amount"]),
            payment_mode=bill["payment_mode"],
            payment_reference=bill.get("payment_reference"),
            is_credit=bill["is_credit"],
            created_at=bill["created_at"],
        )

    async def get_bills_by_date(
        self, store_id: str, date: str
    ) -> list[BillSummaryResult]:
        """Return all finalized bills for a store on a specific date (YYYY-MM-DD)."""
        resp = (
            self.db.schema("billing")
            .table("bills")
            .select("id, bill_number, total_amount, payment_mode, is_credit, created_at")
            .eq("store_id", store_id)
            .gte("created_at", date_range_iso(date, date)[0])
            .lte("created_at", date_range_iso(date, date)[1])
            .order("created_at")
            .execute()
        )
        results = []
        for row in resp.data or []:
            # Count items
            ic = (
                self.db.schema("billing")
                .table("bill_items")
                .select("id", count="exact")
                .eq("bill_id", row["id"])
                .execute()
                .count
                or 0
            )
            results.append(
                BillSummaryResult(
                    bill_id=row["id"],
                    bill_number=row["bill_number"],
                    total_amount=float(row["total_amount"]),
                    payment_mode=row["payment_mode"],
                    is_credit=row["is_credit"],
                    item_count=ic,
                    created_at=row["created_at"],
                )
            )
        return results

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _get_draft_header(self, draft_bill_id: str) -> dict:
        resp = (
            self.db.schema("billing")
            .table("draft_bills")
            .select("id, store_id, telegram_user_id, workflow_id, status, expires_at")
            .eq("id", draft_bill_id)
            .limit(1)
            .execute()
        )
        row = _one(resp)
        if not row:
            raise ValueError(f"Draft bill {draft_bill_id} not found.")
        return row

    async def _get_draft_items_basic(
        self, draft_bill_id: str, store_id: str
    ) -> list[DraftBillItemResult]:
        """
        Fetch draft items for a bill.

        PostgREST does NOT support cross-schema embedded-resource joins
        (e.g. 'products:catalogue.products(name,unit)' fails with PGRST100
        when the querying schema is 'billing' and the target is 'catalogue').
        We therefore fetch the raw billing rows first, then look up product
        name + unit from catalogue via a separate call per item.
        """
        resp = (
            self.db.schema("billing")
            .table("draft_bill_items")
            .select(
                "id, product_id, quantity, unit_price, gst_rate, "
                "is_partial_fulfillment, available_quantity"
            )
            .eq("draft_bill_id", draft_bill_id)
            .execute()
        )
        items = []
        for row in resp.data or []:
            # Fetch product name + unit from catalogue (separate call per item)
            try:
                prod = await self._catalogue.get_product(store_id, row["product_id"])
                prod_name = prod.name
                prod_unit = prod.unit
            except Exception:
                prod_name = ""
                prod_unit = ""
            qty = float(row["quantity"])
            price = float(row["unit_price"])
            items.append(
                DraftBillItemResult(
                    draft_item_id=row["id"],
                    product_id=row["product_id"],
                    product_name=prod_name,
                    quantity=qty,
                    unit=prod_unit,
                    unit_price=price,
                    gst_rate=float(row["gst_rate"]),
                    line_subtotal=round(qty * price, 2),
                    is_partial_fulfillment=row["is_partial_fulfillment"],
                )
            )
        return items

    async def _get_bill_items(self, bill_id: str) -> list[BillItemDetail]:
        resp = (
            self.db.schema("billing")
            .table("bill_items")
            .select("*")
            .eq("bill_id", bill_id)
            .execute()
        )
        return [
            BillItemDetail(
                bill_item_id=r["id"],
                product_id=r.get("product_id"),
                product_name=r["product_name_snapshot"],
                brand=r.get("brand_snapshot"),
                unit=r["unit_snapshot"],
                quantity=float(r["quantity"]),
                unit_price=float(r["unit_price"]),
                gst_rate=float(r["gst_rate"]),
                taxable_value=float(r["taxable_value"]),
                cgst_amount=float(r["cgst_amount"]),
                sgst_amount=float(r["sgst_amount"]),
                line_total=float(r["line_total"]),
            )
            for r in (resp.data or [])
        ]
