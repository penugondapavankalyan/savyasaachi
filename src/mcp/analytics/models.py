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


class CustomerCreditSummary(BaseModel):
    customer_id: str
    name: str
    phone: str
    balance: float          # positive = owes shop; negative = shop owes customer


class KhataOverviewData(BaseModel):
    total_credit_given: float           # sum of positive balances (shop is owed)
    total_shop_owes: float              # sum of abs(negative balances) (shop owes customers)
    credit_customer_count: int          # number of customers with positive balance
    shop_owes_customer_count: int       # number of customers with negative balance
    highest_debtor: Optional[CustomerCreditSummary]     # customer who owes shop the most
    highest_creditor: Optional[CustomerCreditSummary]   # customer shop owes the most
    credit_by_day: list[tuple[str, float]]              # [(date_str, credit_amount), ...]


class AnalyticsDeckData(BaseModel):
    store_name: str
    period_label: str
    summary: DailySummaryResult
    daily_summaries: list[DailySummaryResult]   # per-day rows for the period (for PPTX table)
    sales_trend: list[DailyTrendPoint]
    top_items: list[TopItemResult]
    stock_health: StockHealthReport
    gst_summary: GSTSummaryResult
    khata_overview: KhataOverviewData
