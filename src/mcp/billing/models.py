"""
Billing MCP — Pydantic models.
"""

from __future__ import annotations

from typing import Optional
from pydantic import BaseModel


class DraftBillItemResult(BaseModel):
    draft_item_id: str
    product_id: str
    product_name: str
    quantity: float
    unit: str
    unit_price: float
    gst_rate: float
    line_subtotal: float
    is_partial_fulfillment: bool


class DraftBillResult(BaseModel):
    draft_bill_id: str
    workflow_id: str
    status: str
    items: list[DraftBillItemResult]
    item_count: int
    estimated_total: float
    already_existed: bool
    expires_at: str


class DraftBillItemDetail(DraftBillItemResult):
    taxable_value: float
    cgst_amount: float
    sgst_amount: float
    line_total: float


class DraftBillDetailResult(BaseModel):
    draft_bill_id: str
    workflow_id: str
    status: str
    items: list[DraftBillItemDetail]
    subtotal: float
    total_cgst: float
    total_sgst: float
    total_amount: float
    expires_at: str


class AddItemResult(BaseModel):
    draft_item_id: Optional[str]
    product_name: str
    quantity: float
    unit: str
    unit_price: float
    gst_rate: float
    line_subtotal: float
    availability_status: str         # FULL | PARTIAL | NONE
    available_quantity: Optional[float]
    message: str


class RemoveItemResult(BaseModel):
    success: bool
    product_name: str
    message: str


class UpdateItemResult(BaseModel):
    draft_item_id: str
    product_name: str
    new_quantity: float
    availability_status: str
    message: str


class BillItemDetail(BaseModel):
    bill_item_id: str
    product_id: Optional[str]
    product_name: str
    brand: Optional[str]
    unit: str
    quantity: float
    unit_price: float
    gst_rate: float
    taxable_value: float
    cgst_amount: float
    sgst_amount: float
    line_total: float


class FinalizedBillResult(BaseModel):
    bill_id: str
    bill_number: str
    workflow_id: str
    items: list[BillItemDetail]
    subtotal: float
    total_cgst: float
    total_sgst: float
    total_amount: float
    payment_mode: str
    payment_reference: Optional[str]
    is_credit: bool
    already_finalized: bool
    message: str


class CancelResult(BaseModel):
    success: bool
    message: str


class BillDetailResult(BaseModel):
    bill_id: str
    bill_number: str
    store_id: str
    workflow_id: str
    customer_id: Optional[str]
    items: list[BillItemDetail]
    subtotal: float
    total_cgst: float
    total_sgst: float
    total_discount: float
    total_amount: float
    payment_mode: str
    payment_reference: Optional[str]
    is_credit: bool
    created_at: str


class BillSummaryResult(BaseModel):
    bill_id: str
    bill_number: str
    total_amount: float
    payment_mode: str
    is_credit: bool
    item_count: int
    created_at: str
