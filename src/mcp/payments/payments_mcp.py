"""
Payments MCP implementation.

Owns: payments.payments
Reads: billing.bills, billing.customers, khata.khata_entries, identity.stores

Responsibilities:
  - Record payment rows for every payment event
  - Retrieve payment history for a customer (payments + bills combined)
  - Handle KHATA_SETTLE payments (no bill_id, just khata settlement)

Does NOT:
  - Modify billing.bills status (BillingMCP does that via confirm_payment RPC)
  - Create khata entries (KhataMCP does that)
  - Call confirm_payment RPC (BillingMCP does that)

Call order in BillingMCP.confirm_payment():
  1. Validate paid_amount
  2. Call confirm_payment RPC (bills.status → CONFIRMED)
  3. Call PaymentsMCP.record_payment() to INSERT payments row
  4. If over/underpayment → caller handles khata, then calls
     PaymentsMCP.update_khata_link() to set khata_entry_id on the payment row
     — BUT payments is immutable, so khata_entry_id is set at INSERT time.
     The flow must resolve khata BEFORE inserting the payment row.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from src.db.supabase_client import get_client
from src.mcp.payments.models import (
    BillHistoryEntry,
    PaymentHistoryEntry,
    PaymentHistoryResult,
    PaymentResult,
)
from src.utils.ist import now_ist

if TYPE_CHECKING:
    from src.mcp.khata.khata_mcp import KhataMCP


def _one(resp):
    """Safe helper: return first row or None."""
    if resp is None:
        return None
    data = resp.data
    if not data:
        return None
    return data[0] if isinstance(data, list) else data


class PaymentsMCP:
    """
    Records and retrieves payment events.

    Every payment — cash, UPI, credit, over, under, khata settlement —
    gets exactly one row in payments.payments per event.
    The table is immutable (append-only).
    """

    def __init__(self, khata_mcp: "KhataMCP") -> None:
        self.db = get_client()
        self._khata = khata_mcp

    # ------------------------------------------------------------------
    # Core write operations
    # ------------------------------------------------------------------

    async def record_payment(
        self,
        store_id: str,
        bill_id: Optional[str],
        paid_amount: float,
        payment_mode: str,
        payment_type: str,
        payment_status: str = "CONFIRMED",
        bill_number: Optional[str] = None,
        customer_id: Optional[str] = None,
        khata_entry_id: Optional[str] = None,
        payment_reference: Optional[str] = None,
        subtotal: Optional[float] = None,
        total_gst: Optional[float] = None,
        bill_amount: Optional[float] = None,
        change_amount: float = 0.0,
        balance_due: float = 0.0,
    ) -> PaymentResult:
        """
        Insert one row into payments.payments.

        Called by BillingMCP after:
          - confirm_payment RPC flips bill to CONFIRMED (CASH/UPI)
          - finalize_bill completes for CREDIT bills
          - cancel_bill RPC runs (payment_status=CANCELLED)
          - void_bill RPC runs   (payment_status=REFUNDED)
          - KhataMCP.add_payment_entry for KHATA_SETTLE (bill_id=None)

        All financial amounts must be resolved BEFORE calling this method.
        khata_entry_id must be resolved BEFORE calling this method
        (payments table is immutable — no post-insert updates).
        """
        row = {
            "store_id": store_id,
            "paid_amount": paid_amount,
            "payment_mode": payment_mode.upper(),
            "payment_type": payment_type,
            "payment_status": payment_status,
            "change_amount": change_amount,
            "balance_due": balance_due,
        }
        # Optional fields — only set if provided
        if bill_id:
            row["bill_id"] = bill_id
        if customer_id:
            row["customer_id"] = customer_id
        if khata_entry_id:
            row["khata_entry_id"] = khata_entry_id
        if payment_reference:
            row["payment_reference"] = payment_reference
        if subtotal is not None:
            row["subtotal"] = subtotal
        if total_gst is not None:
            row["total_gst"] = total_gst
        if bill_amount is not None:
            row["bill_amount"] = bill_amount

        resp = (
            self.db.schema("payments")
            .table("payments")
            .insert(row)
            .execute()
        )
        inserted = resp.data[0]
        payment_id = inserted["payment_id"]

        return PaymentResult(
            payment_id=payment_id,
            bill_id=bill_id,
            bill_number=bill_number,
            store_id=store_id,
            customer_id=customer_id,
            khata_entry_id=khata_entry_id,
            subtotal=subtotal,
            total_gst=total_gst,
            bill_amount=bill_amount,
            paid_amount=paid_amount,
            payment_mode=payment_mode.upper(),
            payment_reference=payment_reference,
            payment_type=payment_type,
            payment_status=payment_status,
            change_amount=change_amount,
            balance_due=balance_due,
            created_at=inserted["created_at"],
            message=self._build_message(
                payment_type=payment_type,
                payment_status=payment_status,
                paid_amount=paid_amount,
                bill_amount=bill_amount,
                change_amount=change_amount,
                balance_due=balance_due,
                bill_number=bill_number,
            ),
        )

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    async def get_payment_history(
        self,
        store_id: str,
        customer_id: str,
    ) -> PaymentHistoryResult:
        """
        Return combined payment + bill history for a customer,
        sorted newest first.

        Fetches:
          1. All payments.payments rows for this customer
          2. All billing.bills rows for this customer
          3. Merges and sorts by created_at DESC
        """
        # 1. Get customer details
        cust_resp = (
            self.db.schema("billing")
            .table("customers")
            .select("id, name, phone")
            .eq("id", customer_id)
            .limit(1)
            .execute()
        )
        customer = _one(cust_resp)
        if not customer:
            raise ValueError(f"Customer {customer_id} not found.")

        # 2. Get all payment rows for this customer
        pay_resp = (
            self.db.schema("payments")
            .table("payments")
            .select("*")
            .eq("store_id", store_id)
            .eq("customer_id", customer_id)
            .order("created_at", desc=True)
            .execute()
        )
        payment_rows = pay_resp.data or []

        # 3. Get all bills for this customer
        bill_resp = (
            self.db.schema("billing")
            .table("bills")
            .select("id, bill_number, total_amount, payment_mode, status, is_credit, created_at")
            .eq("store_id", store_id)
            .eq("customer_id", customer_id)
            .order("created_at", desc=True)
            .execute()
        )
        bill_rows = bill_resp.data or []

        # 4. Fetch bill numbers for payment rows that have a bill_id
        bill_id_to_number: dict[str, str] = {
            r["id"]: r["bill_number"] for r in bill_rows
        }
        # Also fetch any bill_ids in payments not already in bill_rows
        extra_bill_ids = [
            r["bill_id"] for r in payment_rows
            if r.get("bill_id") and r["bill_id"] not in bill_id_to_number
        ]
        if extra_bill_ids:
            extra_resp = (
                self.db.schema("billing")
                .table("bills")
                .select("id, bill_number")
                .in_("id", extra_bill_ids)
                .execute()
            )
            for r in (extra_resp.data or []):
                bill_id_to_number[r["id"]] = r["bill_number"]

        # 5. Build payment entries
        payment_entries = [
            PaymentHistoryEntry(
                payment_id=r["payment_id"],
                bill_id=r.get("bill_id"),
                bill_number=bill_id_to_number.get(r["bill_id"], "") if r.get("bill_id") else None,
                paid_amount=float(r["paid_amount"]),
                bill_amount=float(r["bill_amount"]) if r.get("bill_amount") else None,
                payment_mode=r["payment_mode"],
                payment_type=r["payment_type"],
                payment_status=r["payment_status"],
                change_amount=float(r["change_amount"]),
                balance_due=float(r["balance_due"]),
                khata_entry_id=r.get("khata_entry_id"),
                created_at=r["created_at"],
            )
            for r in payment_rows
        ]

        # 6. Build bill entries
        bill_entries = [
            BillHistoryEntry(
                bill_id=r["id"],
                bill_number=r["bill_number"],
                total_amount=float(r["total_amount"]),
                payment_mode=r["payment_mode"],
                payment_status=r["status"],
                is_credit=r["is_credit"],
                created_at=r["created_at"],
            )
            for r in bill_rows
        ]

        # 7. Compute totals
        total_paid = sum(
            float(r["paid_amount"]) for r in payment_rows
            if r["payment_status"] == "CONFIRMED"
        )
        outstanding = await self._khata.get_balance(store_id, customer_id)

        return PaymentHistoryResult(
            customer_id=customer_id,
            customer_name=customer["name"],
            phone=customer["phone"],
            total_paid=total_paid,
            outstanding_balance=outstanding.balance,
            payments=payment_entries,
            bills=bill_entries,
        )

    async def get_payment_by_bill(
        self,
        bill_id: str,
    ) -> Optional[PaymentResult]:
        """
        Return the most recent payment row for a given bill_id.
        Returns None if no payment row exists yet.
        """
        resp = (
            self.db.schema("payments")
            .table("payments")
            .select("*")
            .eq("bill_id", bill_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        row = _one(resp)
        if not row:
            return None
        return PaymentResult(
            payment_id=row["payment_id"],
            bill_id=row.get("bill_id"),
            bill_number=None,
            store_id=row["store_id"],
            customer_id=row.get("customer_id"),
            khata_entry_id=row.get("khata_entry_id"),
            subtotal=float(row["subtotal"]) if row.get("subtotal") else None,
            total_gst=float(row["total_gst"]) if row.get("total_gst") else None,
            bill_amount=float(row["bill_amount"]) if row.get("bill_amount") else None,
            paid_amount=float(row["paid_amount"]),
            payment_mode=row["payment_mode"],
            payment_reference=row.get("payment_reference"),
            payment_type=row["payment_type"],
            payment_status=row["payment_status"],
            change_amount=float(row["change_amount"]),
            balance_due=float(row["balance_due"]),
            created_at=row["created_at"],
            message="",
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_message(
        self,
        payment_type: str,
        payment_status: str,
        paid_amount: float,
        bill_amount: Optional[float],
        change_amount: float,
        balance_due: float,
        bill_number: Optional[str],
    ) -> str:
        bill_ref = f" for {bill_number}" if bill_number else ""
        if payment_status == "CANCELLED":
            return f"❌ Payment cancelled{bill_ref}."
        if payment_status == "REFUNDED":
            return f"↩️ Payment refunded{bill_ref}."
        if payment_type == "EXACT":
            return f"✅ Payment of ₹{paid_amount:.2f} confirmed{bill_ref}."
        if payment_type == "OVERPAYMENT":
            return (
                f"✅ Payment of ₹{paid_amount:.2f} confirmed{bill_ref}. "
                f"₹{change_amount:.2f} overpaid — recorded in khata."
            )
        if payment_type == "UNDERPAYMENT":
            return (
                f"✅ Partial payment of ₹{paid_amount:.2f} confirmed{bill_ref}. "
                f"₹{balance_due:.2f} balance added to customer khata."
            )
        if payment_type == "KHATA":
            return f"✅ Credit sale recorded{bill_ref}. ₹{bill_amount:.2f} added to customer khata."
        if payment_type == "KHATA_SETTLE":
            return f"✅ Khata settlement of ₹{paid_amount:.2f} recorded."
        return f"✅ Payment of ₹{paid_amount:.2f} recorded."
