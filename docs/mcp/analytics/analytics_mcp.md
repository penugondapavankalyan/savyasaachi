# MCP Module: Analytics MCP

**Domain:** Analytics  
**Module Path:** `src/mcp/analytics/analytics_mcp.py`  
**Owned Tables:** `daily_summary` (read/write)  
**Reads From:** `bills`, `bill_items`, `inventory`, `stock_movements`, `products`

---

## Responsibility

The Analytics MCP aggregates sales data, produces daily close summaries, and provides structured data for the PPTX analysis deck. It is the read-heavy module — it never modifies bills, inventory, or khata data. It only writes to `daily_summary`.

This module is only active in the `ACTIVE` workflow state.

---

## Tools (PydanticAI Tool Functions)

### 1. `get_daily_summary`

**Description:** Returns the sales summary for a specific date. If a `daily_summary` record exists for that date, returns it directly. If not, computes it live from bills.

**Signature:**
```python
async def get_daily_summary(
    store_id: str,
    date: Optional[str] = None  # ISO date string 'YYYY-MM-DD', defaults to today
) -> DailySummaryResult
```

**Output (`DailySummaryResult`):**
```python
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
    top_items: List[TopItemResult]
    is_day_closed: bool  # True if close_day() has been run
    message: str
```

**DB Operations:**
```sql
-- Try cached summary first
SELECT * FROM daily_summary WHERE store_id = ? AND summary_date = ?;

-- If not found, compute live
SELECT
    COUNT(*) as bill_count,
    SUM(total_amount) as total_sales,
    SUM(total_cgst) as total_cgst,
    SUM(total_sgst) as total_sgst,
    SUM(total_cgst + total_sgst) as total_tax,
    SUM(CASE WHEN payment_mode = 'CASH' THEN total_amount ELSE 0 END) as cash_sales,
    SUM(CASE WHEN payment_mode = 'UPI' THEN total_amount ELSE 0 END) as upi_sales,
    SUM(CASE WHEN payment_mode = 'CARD' THEN total_amount ELSE 0 END) as card_sales,
    SUM(CASE WHEN is_credit THEN total_amount ELSE 0 END) as credit_sales
FROM bills
WHERE store_id = ? AND created_at::DATE = ?;
```

**Agent Response Example:**
```
📊 Today's Sales (Jan 15, 2024)

Bills: 23
Total Sales: ₹8,450
  └ Cash: ₹3,200 | UPI: ₹4,750 | Card: ₹500

GST Collected: ₹650
  └ CGST: ₹325 | SGST: ₹325

Top items:
  1. Maggi 70g — 42 packets
  2. Aashirvaad Atta 5kg — 8 packets
  3. Sugar — 12kg
```

---

### 2. `close_day`

**Description:** Aggregates all bills for a date into a `daily_summary` record. This is the "close the day" operation. Can be run multiple times — idempotent via `ON CONFLICT ... DO UPDATE`.

**Signature:**
```python
async def close_day(
    store_id: str,
    date: Optional[str] = None  # Defaults to today
) -> CloseDayResult
```

**Output (`CloseDayResult`):**
```python
class CloseDayResult(BaseModel):
    summary: DailySummaryResult
    already_closed: bool
    message: str
```

**DB Operations:**
```sql
-- Compute aggregates from bills
WITH daily_stats AS (
    SELECT ... -- same as get_daily_summary live query
    FROM bills WHERE store_id = ? AND created_at::DATE = ?
),
top_items AS (
    SELECT bi.product_id, p.name as product_name,
           SUM(bi.quantity) as quantity_sold,
           SUM(bi.line_total) as revenue
    FROM bill_items bi
    JOIN bills b ON b.id = bi.bill_id
    JOIN products p ON p.id = bi.product_id
    WHERE b.store_id = ? AND b.created_at::DATE = ?
    GROUP BY bi.product_id, p.name
    ORDER BY quantity_sold DESC
    LIMIT 10
)
INSERT INTO daily_summary (store_id, summary_date, bill_count, total_sales,
    total_cgst, total_sgst, total_tax, cash_sales, upi_sales, card_sales,
    credit_sales, top_items)
SELECT ?, ?, ds.bill_count, ds.total_sales, ds.total_cgst, ds.total_sgst,
    ds.total_tax, ds.cash_sales, ds.upi_sales, ds.card_sales, ds.credit_sales,
    (SELECT json_agg(ti) FROM top_items ti)
FROM daily_stats ds
ON CONFLICT (store_id, summary_date)
DO UPDATE SET
    bill_count = EXCLUDED.bill_count,
    total_sales = EXCLUDED.total_sales,
    total_cgst = EXCLUDED.total_cgst,
    total_sgst = EXCLUDED.total_sgst,
    total_tax = EXCLUDED.total_tax,
    cash_sales = EXCLUDED.cash_sales,
    upi_sales = EXCLUDED.upi_sales,
    card_sales = EXCLUDED.card_sales,
    credit_sales = EXCLUDED.credit_sales,
    top_items = EXCLUDED.top_items,
    updated_at = NOW();
```

---

### 3. `get_sales_trend`

**Description:** Returns daily sales totals for a date range. Primary data source for the PPTX sales trend chart.

**Signature:**
```python
async def get_sales_trend(
    store_id: str,
    start_date: str,
    end_date: str
) -> List[DailyTrendPoint]
```

**Output:**
```python
class DailyTrendPoint(BaseModel):
    date: str
    total_sales: float
    bill_count: int
    total_tax: float
```

**DB Operations:**
```sql
-- Use daily_summary if available, otherwise compute from bills
SELECT summary_date as date, total_sales, bill_count, total_tax
FROM daily_summary
WHERE store_id = ? AND summary_date BETWEEN ? AND ?
ORDER BY summary_date;
```

---

### 4. `get_top_items`

**Description:** Returns the best-selling items by quantity and revenue for a period. Used in PPTX deck and for "what's selling well?" queries.

**Signature:**
```python
async def get_top_items(
    store_id: str,
    start_date: str,
    end_date: str,
    limit: int = 10
) -> List[TopItemResult]
```

**Output:**
```python
class TopItemResult(BaseModel):
    product_id: str
    product_name: str
    brand: Optional[str]
    unit: str
    quantity_sold: float
    revenue: float
    gst_collected: float
    rank_by_quantity: int
    rank_by_revenue: int
```

**DB Operations:**
```sql
SELECT bi.product_id,
       bi.product_name_snapshot as product_name,
       bi.brand_snapshot as brand,
       bi.unit_snapshot as unit,
       SUM(bi.quantity) as quantity_sold,
       SUM(bi.line_total) as revenue,
       SUM(bi.cgst_amount + bi.sgst_amount) as gst_collected
FROM bill_items bi
JOIN bills b ON b.id = bi.bill_id
WHERE b.store_id = ? AND b.created_at::DATE BETWEEN ? AND ?
GROUP BY bi.product_id, bi.product_name_snapshot, bi.brand_snapshot, bi.unit_snapshot
ORDER BY quantity_sold DESC
LIMIT ?;
```

---

### 5. `get_stock_health`

**Description:** Returns a comprehensive stock health report — current levels, reorder status, and days since last restock. Used in PPTX deck slide 4.

**Signature:**
```python
async def get_stock_health(store_id: str) -> StockHealthReport
```

**Output:**
```python
class StockHealthReport(BaseModel):
    total_products: int
    in_stock: int
    low_stock: int       # quantity <= reorder_level
    out_of_stock: int    # quantity = 0
    items: List[StockHealthItem]
```

---

### 6. `get_gst_summary`

**Description:** Returns GST collected for a period, broken down by GST slab. Used in PPTX deck slide 5.

**Signature:**
```python
async def get_gst_summary(
    store_id: str,
    start_date: str,
    end_date: str
) -> GSTSummaryResult
```

**Output:**
```python
class GSTSummaryResult(BaseModel):
    period_start: str
    period_end: str
    total_taxable_value: float
    total_cgst: float
    total_sgst: float
    total_gst: float
    by_slab: List[GSTSlabSummary]  # Grouped by gst_rate

class GSTSlabSummary(BaseModel):
    gst_rate: float
    taxable_value: float
    cgst: float
    sgst: float
    total_gst: float
    item_count: int
```

---

## PPTX Data Contract

The Documents MCP calls Analytics MCP to get data for the PPTX analysis deck. The required data shape:

```python
class AnalyticsDeckData(BaseModel):
    store_name: str
    period_label: str              # e.g., "Jan 13-19, 2024"
    
    # Slide 1: Summary
    summary: DailySummaryResult    # Or weekly aggregate
    
    # Slide 2: Sales Trend
    sales_trend: List[DailyTrendPoint]   # 7 data points for weekly deck
    
    # Slide 3: Top Items
    top_items: List[TopItemResult]       # Top 10
    
    # Slide 4: Stock Health
    stock_health: StockHealthReport
    
    # Slide 5: GST Summary
    gst_summary: GSTSummaryResult
```

The Documents MCP calls:
```python
data = AnalyticsDeckData(
    store_name = store.shop_name,
    period_label = "Jan 13-19, 2024",
    summary = analytics_mcp.get_daily_summary(store_id, today),
    sales_trend = analytics_mcp.get_sales_trend(store_id, start, end),
    top_items = analytics_mcp.get_top_items(store_id, start, end),
    stock_health = analytics_mcp.get_stock_health(store_id),
    gst_summary = analytics_mcp.get_gst_summary(store_id, start, end)
)
documents_mcp.generate_analysis_pptx(data)
```

---

## Phase 2 Extensibility

| Feature | Change |
|---|---|
| Scheduled weekly deck | Add cron-triggered Lambda that calls `get_analytics_deck_data()` + `generate_analysis_pptx()` |
| Reorder suggestions from velocity | Add `get_sales_velocity(store_id)` tool querying `stock_movements` |
| Monthly/quarterly reports | `get_sales_trend()` already supports arbitrary date ranges |
| Profit margin analysis | Add `cost_price_snapshot` to `bill_items` in Phase 2 |
