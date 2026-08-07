# MCP Module: Documents MCP

**Domain:** Documents  
**Module Path:** `src/mcp/documents/documents_mcp.py`  
**Owned Tables:** None  
**Reads From:** `bills`, `bill_items`, `stores`, analytics data (via Analytics MCP)

---

## Responsibility

The Documents MCP generates physical document artifacts — GST-correct PDF invoices and PPTX business analysis decks — on demand. It does not write to the database. Files are generated in Lambda `/tmp`, sent directly to Telegram, and discarded. No persistent storage.

This module is only active in the `ACTIVE` workflow state.

---

## Tools (PydanticAI Tool Functions)

### 1. `generate_invoice_pdf`

**Description:** Generates a GST-correct PDF invoice for a finalized bill. The bill data is fetched from Supabase, the PDF is rendered in Lambda `/tmp`, sent to the user via Telegram's `sendDocument` API, and then deleted.

**Signature:**
```python
async def generate_invoice_pdf(
    bill_id: str,
    store_id: str,
    telegram_chat_id: int
) -> GenerateDocumentResult
```

**Output (`GenerateDocumentResult`):**
```python
class GenerateDocumentResult(BaseModel):
    success: bool
    file_name: str
    message: str  # 'PDF invoice sent!' or error message
```

**Internal Flow:**
```python
# 1. Fetch bill data
bill = billing_mcp.get_bill(bill_id)
store = identity_mcp.get_store_by_id(store_id)

# 2. Render PDF
pdf_path = f"/tmp/invoice_{bill.bill_number}.pdf"
pdf_renderer = PDFRenderer()  # Abstracted library interface
pdf_renderer.render_invoice(bill, store, pdf_path)

# 3. Send to Telegram
await telegram_client.send_document(
    chat_id=telegram_chat_id,
    file_path=pdf_path,
    caption=f"Invoice {bill.bill_number} — ₹{bill.total_amount}"
)

# 4. Clean up
os.remove(pdf_path)

return GenerateDocumentResult(success=True, file_name=pdf_path)
```

---

### PDF Invoice Layout Specification

The PDF must be a valid GST tax invoice. Layout (top to bottom):

#### Header Section
```
┌──────────────────────────────────────────────────────┐
│  [Shop Name]                                         │
│  [Address]                                           │
│  Phone: [phone]                                      │
│  GSTIN: [gstin] (if present)                         │
│  State: [state_name] ([state_code])                  │
│                                                      │
│  TAX INVOICE                                         │
│  Bill No: BL-2024-001     Date: 15-Jan-2024         │
│  Payment: UPI             Ref: XXXXXXXXXX            │
└──────────────────────────────────────────────────────┘
```

#### Items Table
```
┌────────────────────────────────────────────────────────────────────────────────┐
│ # │ Item             │ HSN  │ Qty │ Unit │ Rate  │ Taxable │ CGST │ SGST │ Total│
├────────────────────────────────────────────────────────────────────────────────┤
│ 1 │ Sugar            │ --   │ 2kg │ KG   │ ₹45   │ ₹90.00  │ 0.00 │ 0.00 │₹90.00│
│ 2 │ Aashirvaad Atta  │ 1101 │  1  │ PKT  │ ₹275  │ ₹275.00 │6.88  │ 6.87 │₹288.75│
│ 3 │ Maggi 70g        │ 1902 │  4  │ PKT  │ ₹14   │ ₹56.00  │3.36  │ 3.36 │₹62.72│
└────────────────────────────────────────────────────────────────────────────────┘
```

#### Totals Section
```
┌──────────────────────────────────────────────────────┐
│                          Subtotal: ₹421.00           │
│                          CGST:      ₹10.24           │
│                          SGST:      ₹10.23           │
│                    ───────────────────────           │
│                    GRAND TOTAL:    ₹441.47           │
└──────────────────────────────────────────────────────┘
```

#### Footer Section
```
┌──────────────────────────────────────────────────────┐
│  Payment Mode: UPI                                   │
│  UPI Ref: XXXXXXXXXX                                 │
│                                                      │
│  This is a computer-generated invoice.               │
│  Thank you for your business!                        │
└──────────────────────────────────────────────────────┘
```

---

### PDF Library Interface (Abstracted)

The PDF library is intentionally abstracted behind a `PDFRenderer` interface so the library can be swapped without changing the Documents MCP:

```python
class PDFRenderer(ABC):
    @abstractmethod
    def render_invoice(self, bill: BillDetailResult, store: StoreResult, output_path: str) -> None:
        """Renders a GST invoice PDF to the given file path."""
        pass

# Concrete implementation (library TBD)
class ReportLabRenderer(PDFRenderer):
    def render_invoice(self, bill, store, output_path):
        ...  # ReportLab implementation

class Fpdf2Renderer(PDFRenderer):
    def render_invoice(self, bill, store, output_path):
        ...  # fpdf2 implementation
```

---

### 2. `generate_analysis_pptx`

**Description:** Generates a PowerPoint analysis deck for a date range, using Analytics MCP data. Renders 5 slides with real charts using `python-pptx`. Sent to Telegram and discarded.

**Signature:**
```python
async def generate_analysis_pptx(
    store_id: str,
    telegram_chat_id: int,
    period: str = 'THIS_WEEK'  # 'TODAY' | 'THIS_WEEK' | 'THIS_MONTH'
) -> GenerateDocumentResult
```

**Internal Flow:**
```python
# 1. Determine date range
start_date, end_date = resolve_period(period)

# 2. Fetch all analytics data
data = AnalyticsDeckData(
    store_name=store.shop_name,
    period_label=f"{start_date} to {end_date}",
    summary=analytics_mcp.get_daily_summary(store_id, end_date),
    sales_trend=analytics_mcp.get_sales_trend(store_id, start_date, end_date),
    top_items=analytics_mcp.get_top_items(store_id, start_date, end_date),
    stock_health=analytics_mcp.get_stock_health(store_id),
    gst_summary=analytics_mcp.get_gst_summary(store_id, start_date, end_date)
)

# 3. Render PPTX
pptx_path = f"/tmp/analysis_{store_id}_{end_date}.pptx"
pptx_renderer = PPTXRenderer()
pptx_renderer.render(data, pptx_path)

# 4. Send to Telegram
await telegram_client.send_document(
    chat_id=telegram_chat_id,
    file_path=pptx_path,
    caption=f"Sales Analysis: {data.period_label}"
)

# 5. Clean up
os.remove(pptx_path)
```

---

### PPTX Deck Structure (5 Slides)

#### Slide 1: Executive Summary
```
[Shop Name] — Weekly Analysis
Period: Jan 13-19, 2024

Total Sales:  ₹52,340    ↑ 12% vs last week
Bills Cut:     147
Tax Collected: ₹3,840

Payment Split:
  Cash: ₹18,000 (34%)
  UPI:  ₹31,000 (59%)
  Card:  ₹3,340 (7%)
```

#### Slide 2: Sales Trend (Line Chart)
- X-axis: 7 days (Mon–Sun)
- Y-axis: Daily sales in ₹
- Single line: `total_sales` per day
- Source: `analytics_mcp.get_sales_trend()`

#### Slide 3: Top Selling Items (Bar Chart)
- X-axis: Product names (top 10)
- Y-axis: Units sold
- Bars colored by GST slab (0%=green, 5%=blue, 12%=orange, 18%=red)
- Source: `analytics_mcp.get_top_items()`

#### Slide 4: Stock Health Table
```
Product          | In Stock | Reorder Level | Status
Sugar            | 8.5 kg   | 5 kg          | ✅ OK
Aashirvaad Atta  | 3 pkts   | 10 pkts       | ⚠️ Low
Maggi 70g        | 0        | 20 pkts       | 🔴 Out
```
- Source: `analytics_mcp.get_stock_health()`

#### Slide 5: GST Summary
```
GST Slab Breakdown (Jan 13-19, 2024)

0%  — ₹8,400 taxable  | ₹0 GST    (loose items)
5%  — ₹22,000 taxable | ₹1,100 GST (packaged staples)
12% — ₹18,000 taxable | ₹2,160 GST (dairy/FMCG)
18% — ₹3,940 taxable  | ₹709 GST  (FMCG)

Total CGST: ₹1,985 | Total SGST: ₹1,985
Total GST Collected: ₹3,970
```
- Donut/pie chart: proportion of sales by GST slab
- Source: `analytics_mcp.get_gst_summary()`

---

### PPTX Implementation Notes (python-pptx)

```python
from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.chart.data import ChartData
from pptx.enum.chart import XL_CHART_TYPE

class PPTXRenderer:
    def render(self, data: AnalyticsDeckData, output_path: str):
        prs = Presentation()
        prs.slide_width = Inches(13.33)
        prs.slide_height = Inches(7.5)
        
        self._add_summary_slide(prs, data)
        self._add_sales_trend_slide(prs, data.sales_trend)
        self._add_top_items_slide(prs, data.top_items)
        self._add_stock_health_slide(prs, data.stock_health)
        self._add_gst_summary_slide(prs, data.gst_summary)
        
        prs.save(output_path)
    
    def _add_sales_trend_slide(self, prs, trend_data):
        slide = prs.slides.add_slide(prs.slide_layouts[5])
        chart_data = ChartData()
        chart_data.categories = [d.date for d in trend_data]
        chart_data.add_series('Daily Sales (₹)', [d.total_sales for d in trend_data])
        
        chart = slide.shapes.add_chart(
            XL_CHART_TYPE.LINE, Inches(1), Inches(1.5),
            Inches(11), Inches(5), chart_data
        ).chart
        # Style the chart...
```

---

## File Handling

| Concern | Design |
|---|---|
| **Storage** | Lambda `/tmp` only (max 512MB, ephemeral) |
| **Lifetime** | Generated → sent to Telegram → `os.remove()` — no persistence |
| **File naming** | `invoice_{bill_number}.pdf`, `analysis_{store_id}_{date}.pptx` |
| **Concurrent generation** | Lambda invocations are isolated — no file name conflicts |
| **Telegram file size limit** | PDF invoices: <1MB. PPTX decks: <2MB. Well within 50MB Telegram limit |
| **Error on generation** | If rendering fails, agent reports error; no file is sent |

---

## Error Handling

| Error | Response |
|---|---|
| Bill not found | "Bill not found. Please provide a valid bill number." |
| No bills in period for PPTX | "No sales data found for that period. The deck cannot be generated." |
| Lambda /tmp full | Extremely unlikely at these file sizes; caught and reported |
| Telegram send failure | Retry once; if still fails, report error to owner |

---

## Phase 2 Extensibility

| Feature | Change |
|---|---|
| Branded invoice template | Add `invoice_template` field to stores — `PDFRenderer` selects template |
| Khata statement PDF | Add `generate_khata_statement_pdf(customer_id)` tool |
| Scheduled PPTX | Triggered by external scheduler calling `generate_analysis_pptx` |
| S3 persistent storage | Replace `/tmp` write + Telegram send with S3 upload + pre-signed URL |
