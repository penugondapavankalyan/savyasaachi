"""
Analytics MCP — Pydantic models.
"""

from __future__ import annotations

from typing import Optional
from pydantic import BaseModel


class TopItemResult(BaseModel):
    product_id: Optional[str]
    product_name: str
    brand: Optional[str]
    unit: str
    quantity_sold: float
    revenue: float
    gst_collected: float
    rank_by_quantity: int
    rank_by_revenue: int


class DailySummaryResult(BaseModel):
    summary_date: str
    bill_count: int
    total_sales: float
    total_cgst: float
    total_sgst: float
    total_tax: float
    cash_sales: float
    upi_sales: float
    card_sales: float
    credit_sales: float
    top_items: list[TopItemResult]
    is_day_closed: bool
    message: str


class CloseDayResult(BaseModel):
    summary: DailySummaryResult
    already_closed: bool
    message: str


class DailyTrendPoint(BaseModel):
    date: str
    total_sales: float
    bill_count: int
    total_tax: float


class StockHealthItem(BaseModel):
    product_id: str
    product_name: str
    brand: Optional[str]
    unit: str
    quantity_in_stock: float
    reorder_level: float
    status: str                      # OK | LOW | CRITICAL | OUT_OF_STOCK


class StockHealthReport(BaseModel):
    total_products: int
    in_stock: int
    low_stock: int
    out_of_stock: int
    items: list[StockHealthItem]


class GSTSlabSummary(BaseModel):
    gst_rate: float
    taxable_value: float
    cgst: float
    sgst: float
    total_gst: float
    item_count: int


class GSTSummaryResult(BaseModel):
    period_start: str
    period_end: str
    total_taxable_value: float
    total_cgst: float
    total_sgst: float
    total_gst: float
    by_slab: list[GSTSlabSummary]


class AnalyticsDeckData(BaseModel):
    store_name: str
    period_label: str
    summary: DailySummaryResult
    sales_trend: list[DailyTrendPoint]
    top_items: list[TopItemResult]
    stock_health: StockHealthReport
    gst_summary: GSTSummaryResult
