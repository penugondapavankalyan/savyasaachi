"""
Payments MCP — Pydantic models.
"""

from __future__ import annotations

from typing import Optional
from pydantic import BaseModel


class PaymentResult(BaseModel):
    payment_id: str
    bill_id: Optional[str]           # None for KHATA_SETTLE
    bill_number: Optional[str]       # human-readable bill number, None for KHATA_SETTLE
    store_id: str
    customer_id: Optional[str]
    khata_entry_id: Optional[str]

    # Bill snapshot
    subtotal: Optional[float]
    total_gst: Optional[float]
    bill_amount: Optional[float]

    # Payment details
    paid_amount: float
    payment_mode: str
    payment_reference: Optional[str]
    payment_type: str                # EXACT | OVERPAYMENT | UNDERPAYMENT | KHATA | KHATA_SETTLE
    payment_status: str              # CONFIRMED | PENDING | CANCELLED | REFUNDED

    # Derived
    change_amount: float             # cash returned to customer if overpaid
    balance_due: float               # sent to khata if underpaid

    created_at: str
    message: str


class PaymentHistoryEntry(BaseModel):
    payment_id: str
    bill_id: Optional[str]
    bill_number: Optional[str]
    paid_amount: float
    bill_amount: Optional[float]
    payment_mode: str
    payment_type: str
    payment_status: str
    change_amount: float
    balance_due: float
    khata_entry_id: Optional[str]
    created_at: str


class PaymentHistoryResult(BaseModel):
    customer_id: str
    customer_name: str
    phone: str
    total_paid: float                # sum of all paid_amount rows
    outstanding_balance: float       # current khata balance
    payments: list[PaymentHistoryEntry]
    bills: list[BillHistoryEntry]


class BillHistoryEntry(BaseModel):
    bill_id: str
    bill_number: str
    total_amount: float
    payment_mode: str
    payment_status: str              # bill status: CONFIRMED, VOID, etc.
    is_credit: bool
    created_at: str


class PendingPaymentIntent(BaseModel):
    """
    Stored in Redis under pending_payment:{telegram_user_id}.
    Holds the unresolved over/underpayment state between turns.
    Prevents LLM from hallucinating amounts in the resolution turn.
    """
    bill_id: str
    bill_number: str
    bill_amount: float
    paid_amount: float
    payment_mode: str
    payment_reference: Optional[str]
    intent_type: str                 # OVERPAYMENT | UNDERPAYMENT
    delta_amount: float              # abs(paid_amount - bill_amount)
    store_id: str
