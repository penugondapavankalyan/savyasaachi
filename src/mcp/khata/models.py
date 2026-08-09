"""
Khata MCP — Pydantic models.
"""

from __future__ import annotations

from typing import Optional
from pydantic import BaseModel


class CustomerResult(BaseModel):
    customer_id: str
    name: str
    phone: str                       # always present — phone is mandatory for all customers
    notes: Optional[str]
    already_existed: bool
    current_balance: float
    message: str


class CustomerLookupResult(BaseModel):
    found: bool
    customers: list[CustomerResult]
    exact_match: bool


class KhataEntryResult(BaseModel):
    entry_id: str
    customer_name: str
    entry_type: str                  # CREDIT | PAYMENT | ADJUSTMENT
    amount: float
    new_balance: float
    balance_direction: str           # OWES_SHOP | SHOP_OWES | SETTLED
    message: str


class BalanceResult(BaseModel):
    customer_id: str
    customer_name: str
    phone: str                       # always present
    balance: float
    balance_direction: str
    last_transaction_at: Optional[str]
    message: str


class KhataEntryDetail(BaseModel):
    entry_id: str
    entry_type: str
    amount_delta: float
    reference_bill_id: Optional[str]
    notes: Optional[str]
    created_at: str


class KhataHistoryResult(BaseModel):
    customer_name: str
    phone: str                       # always present
    current_balance: float
    balance_direction: str
    entries: list[KhataEntryDetail]


class CustomerBalanceSummary(BaseModel):
    customer_id: str
    name: str
    phone: str                       # always present
    balance: float
    balance_direction: str
