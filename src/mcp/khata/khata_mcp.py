"""
Khata MCP implementation.

Owns: billing.customers, khata.khata_entries
(customers are in billing schema, khata_entries in khata schema)
"""

from __future__ import annotations

from src.utils.guardrails import clean_name, clean_optional_str, clean_phone, clean_positive_float


def _one(resp):
    """Safe helper: return first row or None. Handles supabase-py 2.x quirks."""
    if resp is None:
        return None
    data = resp.data
    if not data:
        return None
    return data[0] if isinstance(data, list) else data


from typing import Optional

from src.db.supabase_client import get_client
from src.mcp.khata.models import (
    BalanceResult,
    CustomerBalanceSummary,
    CustomerLookupResult,
    CustomerResult,
    KhataEntryDetail,
    KhataEntryResult,
    KhataHistoryResult,
)


class KhataMCP:
    """Credit ledger operations."""

    def __init__(self) -> None:
        self.db = get_client()

    # ------------------------------------------------------------------
    # Customers
    # ------------------------------------------------------------------

    async def add_customer(
        self,
        store_id: str,
        name: str,
        phone: str,
        notes: Optional[str] = None,
    ) -> CustomerResult:
        """
        Register a new credit customer. Phone number is MANDATORY — it is the unique
        identifier for the customer and is required for credit/khata security.

        Rules:
        - phone must be a valid 10-digit Indian mobile number.
        - Uniqueness is enforced on (store_id, phone) — no two customers share a phone.
        - If the owner cannot provide a phone number, DO NOT add the customer and
          DO NOT allow credit for this sale. Tell the owner:
          'Credit cannot be given without a customer phone number for security.'
        - Never invent or guess a phone number.
        """
        # ── Guardrails ──────────────────────────────────────────────────
        name = clean_name(name, "customer name")
        notes = clean_optional_str(notes)

        # Phone is mandatory — clean and validate strictly
        phone_clean = clean_phone(phone)
        if not phone_clean:
            raise ValueError(
                "Credit cannot be given without a valid customer phone number. "
                "Phone is mandatory for all credit customers for security and identification. "
                "Please ask the customer for their 10-digit mobile number. "
                "If they cannot provide one, credit cannot be extended for this sale."
            )

        # Upsert on (store_id, phone) — guarantees uniqueness per store
        resp = (
            self.db.schema("billing")
            .table("customers")
            .upsert(
                {
                    "store_id": store_id,
                    "name": name,
                    "phone": phone_clean,
                    "notes": notes,
                },
                on_conflict="store_id,phone",
            )
            .execute()
        )
        customer = resp.data[0]
        customer_id = customer["id"]
        already_existed = customer.get("updated_at") is not None  # upsert sets updated_at on conflict

        balance = await self._compute_balance(store_id, customer_id)

        return CustomerResult(
            customer_id=customer_id,
            name=customer["name"],
            phone=customer["phone"],
            notes=customer.get("notes"),
            already_existed=already_existed,
            current_balance=balance,
            message=(
                f"✅ Customer {name} ({phone_clean}) saved. "
                + (_balance_msg(balance, name) if balance != 0 else "Account is fresh (₹0 balance).")
            ),
        )

    async def get_customer(
        self, store_id: str, name_or_phone: str
    ) -> CustomerLookupResult:
        """Find a customer by phone (exact) or name (partial match)."""
        # Exact phone match
        if name_or_phone.replace(" ", "").isdigit():
            resp = (
                self.db.schema("billing")
                .table("customers")
                .select("id, name, phone, notes")
                .eq("store_id", store_id)
                .eq("phone", name_or_phone)
                .eq("is_active", True)
                .execute()
            )
        else:
            resp = (
                self.db.schema("billing")
                .table("customers")
                .select("id, name, phone, notes")
                .eq("store_id", store_id)
                .ilike("name", f"%{name_or_phone}%")
                .eq("is_active", True)
                .limit(10)
                .execute()
            )

        rows = resp.data or []
        if not rows:
            return CustomerLookupResult(found=False, customers=[], exact_match=False)

        customers = []
        for r in rows:
            balance = await self._compute_balance(store_id, r["id"])
            customers.append(
                CustomerResult(
                    customer_id=r["id"],
                    name=r["name"],
                    phone=r["phone"],
                    notes=r.get("notes"),
                    already_existed=True,
                    current_balance=balance,
                    message="",
                )
            )

        return CustomerLookupResult(
            found=True,
            customers=customers,
            exact_match=len(customers) == 1,
        )

    # ------------------------------------------------------------------
    # Khata entries
    # ------------------------------------------------------------------

    async def add_credit_entry(
        self,
        store_id: str,
        customer_id: str,
        amount: float,
        reference_bill_id: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> KhataEntryResult:
        """
        Record a credit (customer owes shop). amount_delta = +amount.
        Only pass the exact amount the owner stated — never invent amounts.
        """
        # ── Guardrails ──────────────────────────────────────────────────
        amount = clean_positive_float(amount, "credit amount")
        notes = clean_optional_str(notes)

        resp = (
            self.db.schema("khata")
            .table("khata_entries")
            .insert(
                {
                    "store_id": store_id,
                    "customer_id": customer_id,
                    "entry_type": "CREDIT",
                    "amount_delta": amount,
                    "reference_bill_id": reference_bill_id,
                    "notes": notes,
                }
            )
            .execute()
        )
        entry_id = resp.data[0]["id"]
        customer_name = await self._get_customer_name(customer_id)
        new_balance = await self._compute_balance(store_id, customer_id)
        direction = _balance_direction(new_balance)

        return KhataEntryResult(
            entry_id=entry_id,
            customer_name=customer_name,
            entry_type="CREDIT",
            amount=amount,
            new_balance=new_balance,
            balance_direction=direction,
            message=f"Credit of ₹{amount:.2f} added for {customer_name}. Balance: {_balance_msg(new_balance, customer_name)}",
        )

    async def add_payment_entry(
        self,
        store_id: str,
        customer_id: str,
        amount: float,
        reference_bill_id: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> KhataEntryResult:
        """
        Record a payment (customer pays shop). amount_delta = -amount.
        Only pass the exact amount the owner stated — never invent amounts.
        """
        # ── Guardrails ──────────────────────────────────────────────────
        amount = clean_positive_float(amount, "payment amount")
        notes = clean_optional_str(notes)

        resp = (
            self.db.schema("khata")
            .table("khata_entries")
            .insert(
                {
                    "store_id": store_id,
                    "customer_id": customer_id,
                    "entry_type": "PAYMENT",
                    "amount_delta": -amount,
                    "reference_bill_id": reference_bill_id,
                    "notes": notes,
                }
            )
            .execute()
        )
        entry_id = resp.data[0]["id"]
        customer_name = await self._get_customer_name(customer_id)
        new_balance = await self._compute_balance(store_id, customer_id)
        direction = _balance_direction(new_balance)

        return KhataEntryResult(
            entry_id=entry_id,
            customer_name=customer_name,
            entry_type="PAYMENT",
            amount=amount,
            new_balance=new_balance,
            balance_direction=direction,
            message=f"Payment of ₹{amount:.2f} recorded for {customer_name}. {_balance_msg(new_balance, customer_name)}",
        )

    async def get_balance(
        self, store_id: str, customer_id: str
    ) -> BalanceResult:
        """Return the current balance for a customer."""
        resp = (
            self.db.schema("billing")
            .table("customers")
            .select("name, phone")
            .eq("id", customer_id)
            .limit(1)
            .execute()
        )
        customer = _one(resp)
        if not customer:
            raise ValueError(f"Customer {customer_id} not found.")
        balance = await self._compute_balance(store_id, customer_id)

        # Last transaction date
        last_resp = (
            self.db.schema("khata")
            .table("khata_entries")
            .select("created_at")
            .eq("store_id", store_id)
            .eq("customer_id", customer_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        last_at = last_resp.data[0]["created_at"] if last_resp.data else None

        return BalanceResult(
            customer_id=customer_id,
            customer_name=customer["name"],
            phone=customer["phone"],
            balance=balance,
            balance_direction=_balance_direction(balance),
            last_transaction_at=last_at,
            message=_balance_msg(balance, customer["name"]),
        )

    async def get_khata_history(
        self,
        store_id: str,
        customer_id: str,
        limit: int = 20,
    ) -> KhataHistoryResult:
        """Return transaction history with running balance."""
        resp = (
            self.db.schema("billing")
            .table("customers")
            .select("name, phone")
            .eq("id", customer_id)
            .limit(1)
            .execute()
        )
        customer = _one(resp)
        if not customer:
            raise ValueError(f"Customer {customer_id} not found.")

        entries_resp = (
            self.db.schema("khata")
            .table("khata_entries")
            .select("id, entry_type, amount_delta, reference_bill_id, notes, created_at")
            .eq("store_id", store_id)
            .eq("customer_id", customer_id)
            .order("created_at", desc=False)
            .limit(limit)
            .execute()
        )

        entries = [
            KhataEntryDetail(
                entry_id=r["id"],
                entry_type=r["entry_type"],
                amount_delta=float(r["amount_delta"]),
                reference_bill_id=r.get("reference_bill_id"),
                notes=r.get("notes"),
                created_at=r["created_at"],
            )
            for r in (entries_resp.data or [])
        ]

        current_balance = await self._compute_balance(store_id, customer_id)

        return KhataHistoryResult(
            customer_name=customer["name"],
            phone=customer["phone"],
            current_balance=current_balance,
            balance_direction=_balance_direction(current_balance),
            entries=entries,
        )

    async def list_customers_with_balances(
        self,
        store_id: str,
        filter: str = "ALL",  # ALL | OWES_SHOP | SHOP_OWES | SETTLED
    ) -> list[CustomerBalanceSummary]:
        """Return all customers with their current balance."""
        # Batch-fetch all customers (avoid N+1)
        cust_resp = (
            self.db.schema("billing")
            .table("customers")
            .select("id, name, phone")
            .eq("store_id", store_id)
            .eq("is_active", True)
            .execute()
        )
        customers = cust_resp.data or []

        # Batch-fetch all khata entries for this store (then compute balances in Python)
        khata_resp = (
            self.db.schema("khata")
            .table("khata_entries")
            .select("customer_id, amount_delta")
            .eq("store_id", store_id)
            .execute()
        )
        khata_entries = khata_resp.data or []

        # Build balance map: customer_id → balance
        balance_map: dict[str, float] = {}
        for entry in khata_entries:
            cid = entry["customer_id"]
            balance_map[cid] = balance_map.get(cid, 0.0) + float(entry["amount_delta"])

        results = []
        for row in customers:
            balance = balance_map.get(row["id"], 0.0)
            direction = _balance_direction(balance)
            if filter == "OWES_SHOP" and direction != "OWES_SHOP":
                continue
            if filter == "SHOP_OWES" and direction != "SHOP_OWES":
                continue
            if filter == "SETTLED" and direction != "SETTLED":
                continue
            results.append(
                CustomerBalanceSummary(
                    customer_id=row["id"],
                    name=row["name"],
                    phone=row["phone"],
                    balance=balance,
                    balance_direction=direction,
                )
            )
        results.sort(key=lambda x: abs(x.balance), reverse=True)
        return results

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _compute_balance(self, store_id: str, customer_id: str) -> float:
        resp = (
            self.db.schema("khata")
            .table("khata_entries")
            .select("amount_delta")
            .eq("store_id", store_id)
            .eq("customer_id", customer_id)
            .execute()
        )
        return sum(float(r["amount_delta"]) for r in (resp.data or []))

    async def _get_customer_name(self, customer_id: str) -> str:
        resp = (
            self.db.schema("billing")
            .table("customers")
            .select("name")
            .eq("id", customer_id)
            .limit(1)
            .execute()
        )
        row = _one(resp)
        return row["name"] if row else "Unknown"


# ------------------------------------------------------------------
# Pure helpers (no DB)
# ------------------------------------------------------------------

def _balance_direction(balance: float) -> str:
    if balance > 0:
        return "OWES_SHOP"
    if balance < 0:
        return "SHOP_OWES"
    return "SETTLED"


def _balance_msg(balance: float, name: str) -> str:
    if balance > 0:
        return f"{name} owes the shop ₹{balance:.2f}."
    if balance < 0:
        return f"Shop owes {name} ₹{abs(balance):.2f}."
    return f"{name}'s account is settled (₹0 balance)."
