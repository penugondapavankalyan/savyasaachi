"""
Inventory MCP — Pydantic models.
"""

from __future__ import annotations

from typing import Optional
from pydantic import BaseModel


class StockResult(BaseModel):
    product_id: str
    product_name: str
    brand: Optional[str]
    quantity_in_stock: float
    unit: str
    reorder_level: float
    is_below_reorder: bool
    last_restocked_at: Optional[str]


class ReceiveStockResult(BaseModel):
    inventory_id: Optional[str]      # None in rare race condition before row appears
    product_name: str
    quantity_received: float
    new_total_quantity: float
    unit: str
    cost_price_updated: bool
    workflow_advanced: bool
    message: str


class AvailabilityResult(BaseModel):
    product_id: str
    product_name: str
    requested_quantity: float
    available_quantity: float
    unit: str
    fulfillment_status: str          # FULL | PARTIAL | NONE
    can_partially_fulfill: bool
    message: str


class DecrementResult(BaseModel):
    product_id: str
    quantity_decremented: float
    new_quantity: float
    reorder_alert: bool


class LowStockItem(BaseModel):
    product_id: str
    product_name: str
    brand: Optional[str]
    quantity_in_stock: float
    reorder_level: float
    unit: str
    urgency: str                     # OUT_OF_STOCK | CRITICAL | LOW


class StockMovementRecord(BaseModel):
    movement_id: str
    movement_type: str
    quantity_delta: float
    reference_id: Optional[str]
    reference_type: Optional[str]
    notes: Optional[str]
    created_at: str
