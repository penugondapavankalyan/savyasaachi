# Implementation Guide 7: Build PPTX Analytics Deck

**Order:** Seventh — after the analytics MCP is working.  
**Reference Docs:** `docs/mcp/documents/documents_mcp.md`, `docs/mcp/analytics/analytics_mcp.md`

---

## Prerequisites

- Analytics MCP implemented and tested
- python-pptx installed
- At least a few bills in the DB for test data

---

## Step 1: Install python-pptx

```bash
pip install python-pptx
```

---

## Step 2: PPTX Renderer

```python
# src/mcp/documents/pptx_renderer.py
from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.chart.data import ChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.dml.color import RGBColor
from pptx.util import Inches, Pt
from typing import List

# Colour palette (clean, professional)
COLOUR_PRIMARY = RGBColor(59, 130, 212)    # Blue
COLOUR_ACCENT  = RGBColor(34, 197, 94)     # Green
COLOUR_WARNING = RGBColor(234, 179, 8)     # Amber
COLOUR_DANGER  = RGBColor(239, 68, 68)     # Red
COLOUR_MUTED   = RGBColor(107, 114, 128)   # Grey
COLOUR_BG      = RGBColor(247, 248, 250)   # Light grey bg


class PPTXRenderer:
    
    def render(self, data, output_path: str) -> None:
        """Render full analytics deck to PPTX file."""
        prs = Presentation()
        prs.slide_width  = Inches(13.33)
        prs.slide_height = Inches(7.5)
        
        self._add_summary_slide(prs, data)
        self._add_sales_trend_slide(prs, data)
        self._add_top_items_slide(prs, data)
        self._add_stock_health_slide(prs, data)
        self._add_gst_summary_slide(prs, data)
        
        prs.save(output_path)
    
    def _blank_slide(self, prs: Presentation):
        """Add a blank slide (layout index 6)."""
        return prs.slides.add_slide(prs.slide_layouts[6])
    
    def _add_title(self, slide, title: str, subtitle: str = "") -> None:
        """Add a title text box at the top of the slide."""
        txBox = slide.shapes.add_textbox(Inches(0.5), Inches(0.2), Inches(12), Inches(0.7))
        tf = txBox.text_frame
        p = tf.paragraphs[0]
        p.text = title
        p.font.bold = True
        p.font.size = Pt(24)
        p.font.color.rgb = COLOUR_PRIMARY
        
        if subtitle:
            txBox2 = slide.shapes.add_textbox(Inches(0.5), Inches(0.85), Inches(12), Inches(0.4))
            tf2 = txBox2.text_frame
            p2 = tf2.paragraphs[0]
            p2.text = subtitle
            p2.font.size = Pt(12)
            p2.font.color.rgb = COLOUR_MUTED
    
    def _add_summary_slide(self, prs, data) -> None:
        slide = self._blank_slide(prs)
        s = data.summary
        
        self._add_title(
            slide,
            f"{data.store_name} — Sales Summary",
            f"Period: {data.period_label}"
        )
        
        # 4 KPI boxes in a row
        kpis = [
            ("Total Sales", f"₹{s.total_sales:,.2f}"),
            ("Bills Cut",   str(s.bill_count)),
            ("Tax Collected", f"₹{s.total_tax:,.2f}"),
            ("Credit Sales", f"₹{s.credit_sales:,.2f}"),
        ]
        
        for i, (label, value) in enumerate(kpis):
            x = Inches(0.5 + i * 3.1)
            box = slide.shapes.add_shape(1, x, Inches(1.5), Inches(2.8), Inches(1.8))
            box.fill.solid()
            box.fill.fore_color.rgb = COLOUR_BG
            box.line.color.rgb = COLOUR_PRIMARY
            
            tf = box.text_frame
            tf.word_wrap = True
            p1 = tf.paragraphs[0]
            p1.text = value
            p1.font.size = Pt(22)
            p1.font.bold = True
            p1.font.color.rgb = COLOUR_PRIMARY
            p1.alignment = 2  # center
            
            p2 = tf.add_paragraph()
            p2.text = label
            p2.font.size = Pt(10)
            p2.font.color.rgb = COLOUR_MUTED
            p2.alignment = 2
        
        # Payment split table
        payment_data = [
            ("Cash", f"₹{s.cash_sales:,.0f}"),
            ("UPI",  f"₹{s.upi_sales:,.0f}"),
            ("Card", f"₹{s.card_sales:,.0f}"),
        ]
        
        table = slide.shapes.add_table(
            len(payment_data) + 1, 2,
            Inches(9), Inches(1.5), Inches(3.5), Inches(1.8)
        ).table
        
        table.cell(0, 0).text = "Payment Mode"
        table.cell(0, 1).text = "Amount"
        for i, (mode, amt) in enumerate(payment_data, 1):
            table.cell(i, 0).text = mode
            table.cell(i, 1).text = amt
    
    def _add_sales_trend_slide(self, prs, data) -> None:
        slide = self._blank_slide(prs)
        self._add_title(slide, "Daily Sales Trend", data.period_label)
        
        chart_data = ChartData()
        chart_data.categories = [point.date[-5:] for point in data.sales_trend]  # MM-DD
        chart_data.add_series('Sales (₹)', [point.total_sales for point in data.sales_trend])
        
        chart_frame = slide.shapes.add_chart(
            XL_CHART_TYPE.LINE,
            Inches(0.5), Inches(1.3),
            Inches(12), Inches(5.5),
            chart_data
        )
        
        chart = chart_frame.chart
        chart.has_legend = False
        
        # Style the line
        series = chart.series[0]
        series.format.line.color.rgb = COLOUR_PRIMARY
        series.format.line.width = Pt(2.5)
    
    def _add_top_items_slide(self, prs, data) -> None:
        slide = self._blank_slide(prs)
        self._add_title(slide, "Top Selling Items", f"By quantity — {data.period_label}")
        
        if not data.top_items:
            return
        
        chart_data = ChartData()
        names = [f"{item.product_name[:20]}" for item in data.top_items[:10]]
        chart_data.categories = names
        chart_data.add_series(
            'Units Sold',
            [item.quantity_sold for item in data.top_items[:10]]
        )
        
        chart_frame = slide.shapes.add_chart(
            XL_CHART_TYPE.BAR_CLUSTERED,
            Inches(0.5), Inches(1.3),
            Inches(12), Inches(5.5),
            chart_data
        )
        
        chart = chart_frame.chart
        chart.has_legend = False
        chart.plots[0].series[0].format.fill.solid()
        chart.plots[0].series[0].format.fill.fore_color.rgb = COLOUR_PRIMARY
    
    def _add_stock_health_slide(self, prs, data) -> None:
        slide = self._blank_slide(prs)
        self._add_title(slide, "Stock Health", "Current inventory status")
        
        sh = data.stock_health
        items = sh.items[:15]  # Max 15 rows on slide
        
        table = slide.shapes.add_table(
            len(items) + 1, 4,
            Inches(0.5), Inches(1.3),
            Inches(12), Inches(5.5)
        ).table
        
        # Headers
        for i, h in enumerate(['Product', 'In Stock', 'Reorder Level', 'Status']):
            table.cell(0, i).text = h
        
        # Data rows
        for row_i, item in enumerate(items, 1):
            if item.quantity_in_stock == 0:
                status, colour = "🔴 Out of Stock", COLOUR_DANGER
            elif item.quantity_in_stock <= item.reorder_level:
                status, colour = "⚠️ Low", COLOUR_WARNING
            else:
                status, colour = "✅ OK", COLOUR_ACCENT
            
            table.cell(row_i, 0).text = item.product_name[:30]
            table.cell(row_i, 1).text = f"{item.quantity_in_stock} {item.unit}"
            table.cell(row_i, 2).text = f"{item.reorder_level} {item.unit}"
            table.cell(row_i, 3).text = status
    
    def _add_gst_summary_slide(self, prs, data) -> None:
        slide = self._blank_slide(prs)
        self._add_title(slide, "GST Summary", data.period_label)
        
        g = data.gst_summary
        
        # Summary KPIs
        kpis = [
            ("Total Taxable Value", f"₹{g.total_taxable_value:,.2f}"),
            ("Total CGST",  f"₹{g.total_cgst:,.2f}"),
            ("Total SGST",  f"₹{g.total_sgst:,.2f}"),
            ("Total GST",   f"₹{g.total_gst:,.2f}"),
        ]
        
        for i, (label, value) in enumerate(kpis):
            x = Inches(0.5 + i * 3.1)
            txb = slide.shapes.add_textbox(x, Inches(1.3), Inches(2.8), Inches(1.2))
            tf = txb.text_frame
            p1 = tf.paragraphs[0]
            p1.text = value
            p1.font.size = Pt(18)
            p1.font.bold = True
            p1.font.color.rgb = COLOUR_PRIMARY
            p2 = tf.add_paragraph()
            p2.text = label
            p2.font.size = Pt(9)
            p2.font.color.rgb = COLOUR_MUTED
        
        # GST slab breakdown table
        slabs = g.by_slab
        if slabs:
            table = slide.shapes.add_table(
                len(slabs) + 1, 4,
                Inches(0.5), Inches(3.0),
                Inches(12), Inches(3.8)
            ).table
            
            for i, h in enumerate(['GST Slab', 'Taxable Value', 'CGST', 'SGST']):
                table.cell(0, i).text = h
            
            for row_i, slab in enumerate(slabs, 1):
                table.cell(row_i, 0).text = f"{slab.gst_rate:.0f}%"
                table.cell(row_i, 1).text = f"₹{slab.taxable_value:,.2f}"
                table.cell(row_i, 2).text = f"₹{slab.cgst:,.2f}"
                table.cell(row_i, 3).text = f"₹{slab.sgst:,.2f}"
```

---

## Step 3: Wire into Documents MCP

```python
# In documents_mcp.py
from src.mcp.documents.pptx_renderer import PPTXRenderer

class DocumentsMCP:
    def __init__(self, ...):
        ...
        self.pptx_renderer = PPTXRenderer()
    
    async def generate_analysis_pptx(self, store_id, telegram_chat_id, period='THIS_WEEK'):
        start_date, end_date = resolve_period(period)
        store = await self.identity.get_store_by_id(store_id)
        
        data = AnalyticsDeckData(
            store_name=store.shop_name,
            period_label=f"{start_date} to {end_date}",
            summary=await self.analytics.get_daily_summary(store_id, end_date),
            sales_trend=await self.analytics.get_sales_trend(store_id, start_date, end_date),
            top_items=await self.analytics.get_top_items(store_id, start_date, end_date),
            stock_health=await self.analytics.get_stock_health(store_id),
            gst_summary=await self.analytics.get_gst_summary(store_id, start_date, end_date)
        )
        
        pptx_path = f"/tmp/analysis_{store_id[:8]}_{end_date}.pptx"
        
        try:
            self.pptx_renderer.render(data, pptx_path)
            telegram = get_telegram_client()
            await telegram.send_document(
                chat_id=telegram_chat_id,
                file_path=pptx_path,
                caption=f"📊 Sales Analysis: {data.period_label}"
            )
            return {"success": True, "message": "Analysis deck sent!"}
        finally:
            if os.path.exists(pptx_path):
                os.remove(pptx_path)
```

---

## Validation Checklist

- [ ] PPTX generates with 5 slides
- [ ] Sales trend chart shows correct data (line chart)
- [ ] Top items bar chart renders correctly
- [ ] Stock health table shows correct status icons
- [ ] GST summary shows correct totals
- [ ] File sent via Telegram and deleted from `/tmp`
- [ ] Handles gracefully when there is no data for the period
- [ ] Period resolution works: TODAY, THIS_WEEK, THIS_MONTH
