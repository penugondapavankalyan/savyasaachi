"""
Catalogue MCP — Pydantic models.
"""

from __future__ import annotations

from typing import Optional
from pydantic import BaseModel


class ProductResult(BaseModel):
    product_id: str
    name: str
    brand: Optional[str]
    is_loose: bool
    unit: str
    hsn_code: Optional[str]
    gst_rate: float
    cost_price: float
    mrp: float
    reorder_level: float
    is_active: bool


class AddProductResult(BaseModel):
    product_id: str
    name: str
    brand: Optional[str]
    is_loose: bool
    gst_rate: float
    mrp: float
    already_existed: bool
    workflow_advanced: bool
    message: str


class DeactivateResult(BaseModel):
    product_id: str
    product_name: str
    success: bool
    message: str
