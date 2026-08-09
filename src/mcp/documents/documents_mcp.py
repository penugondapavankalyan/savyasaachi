"""
Documents MCP implementation.

No DB writes.  Generates PDF invoices (fpdf2) and PPTX analytics decks
(python-pptx) in Lambda /tmp, sends to Telegram, then deletes.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from typing import TYPE_CHECKING

from src.mcp.documents.models import AnalysisPPTXResult, InvoicePDFResult

if TYPE_CHECKING:
    from src.mcp.analytics.analytics_mcp import AnalyticsMCP
    from src.mcp.billing.billing_mcp import BillingMCP
    from src.mcp.identity.identity_mcp import IdentityMCP

# State code → state name mapping
_STATE_NAMES: dict[str, str] = {
    "01": "Jammu & Kashmir",
    "02": "Himachal Pradesh",
    "03": "Punjab",
    "04": "Chandigarh",
    "05": "Uttarakhand",
    "06": "Haryana",
    "07": "Delhi",
    "08": "Rajasthan",
    "09": "Uttar Pradesh",
    "10": "Bihar",
    "11": "Sikkim",
    "12": "Arunachal Pradesh",
    "13": "Nagaland",
    "14": "Manipur",
    "15": "Mizoram",
    "16": "Tripura",
    "17": "Meghalaya",
    "18": "Assam",
    "19": "West Bengal",
    "20": "Jharkhand",
    "21": "Odisha",
    "22": "Chhattisgarh",
    "23": "Madhya Pradesh",
    "24": "Gujarat",
    "26": "Dadra & Nagar Haveli and Daman & Diu",
    "27": "Maharashtra",
    "28": "Andhra Pradesh",
    "29": "Karnataka",
    "30": "Goa",
    "31": "Lakshadweep",
    "32": "Kerala",
    "33": "Tamil Nadu",
    "34": "Puducherry",
    "35": "Andaman & Nicobar Islands",
    "36": "Telangana",
    "37": "Andhra Pradesh (New)",
    "38": "Ladakh",
}


class DocumentsMCP:
    """PDF invoice + PPTX analytics deck generation."""

    def __init__(
        self,
        billing_mcp: "BillingMCP",
        analytics_mcp: "AnalyticsMCP",
        identity_mcp: "IdentityMCP",
    ) -> None:
        self._billing = billing_mcp
        self._analytics = analytics_mcp
        self._identity = identity_mcp

    # ------------------------------------------------------------------
    # PDF Invoice
    # ------------------------------------------------------------------

    async def generate_invoice_pdf(
        self,
        bill_id: str,
        store_id: str,
        telegram_user_id: int,
    ) -> InvoicePDFResult:
        """
        Generate a GST-correct PDF invoice for a finalized bill.
        Returns the file path; the handler sends it to Telegram.
        """
        from fpdf import FPDF  # type: ignore

        bill = await self._billing.get_bill(bill_id)
        store = await self._identity.get_store(telegram_user_id)
        if not store:
            raise ValueError("Store not found for this user.")

        state_name = _STATE_NAMES.get(store.state_code, store.state_code)
        bill_date = bill.created_at[:10] if bill.created_at else date.today().isoformat()

        pdf = FPDF()
        pdf.add_page()
        pdf.set_auto_page_break(auto=True, margin=10)

        # ---- Header ----
        pdf.set_font("Helvetica", "B", 16)
        pdf.cell(0, 8, store.shop_name, align="C", new_x="LMARGIN", new_y="NEXT")

        pdf.set_font("Helvetica", "", 9)
        if store.address:
            pdf.cell(0, 5, store.address, align="C", new_x="LMARGIN", new_y="NEXT")
        if store.phone:
            pdf.cell(0, 5, f"Phone: {store.phone}", align="C", new_x="LMARGIN", new_y="NEXT")
        if store.gstin:
            pdf.cell(0, 5, f"GSTIN: {store.gstin}", align="C", new_x="LMARGIN", new_y="NEXT")
        pdf.cell(
            0, 5, f"State: {state_name} ({store.state_code})",
            align="C", new_x="LMARGIN", new_y="NEXT"
        )

        pdf.ln(3)
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 7, "TAX INVOICE", align="C", new_x="LMARGIN", new_y="NEXT")

        pdf.set_font("Helvetica", "", 9)
        pdf.cell(95, 5, f"Bill No: {bill.bill_number}")
        pdf.cell(95, 5, f"Date: {bill_date}", new_x="LMARGIN", new_y="NEXT")
        pdf.cell(95, 5, f"Payment: {bill.payment_mode}")
        if bill.payment_reference:
            pdf.cell(95, 5, f"Ref: {bill.payment_reference}", new_x="LMARGIN", new_y="NEXT")
        else:
            pdf.ln(5)
        pdf.ln(3)

        # ---- Items table header ----
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_fill_color(220, 220, 220)
        cols = [8, 50, 14, 12, 10, 16, 22, 18, 18, 22]
        headers = ["#", "Item", "HSN", "Qty", "Unit", "Rate", "Taxable", "CGST", "SGST", "Total"]
        for w, h in zip(cols, headers):
            pdf.cell(w, 6, h, border=1, fill=True, align="C")
        pdf.ln()

        # ---- Items table rows ----
        pdf.set_font("Helvetica", "", 8)
        for idx, item in enumerate(bill.items, 1):
            row_vals = [
                str(idx),
                (item.product_name + (f"\n({item.brand})" if item.brand else ""))[:40],
                item.unit or "--",
                f"{item.quantity:.2f}",
                item.unit,
                f"{item.unit_price:.2f}",
                f"{item.taxable_value:.2f}",
                f"{item.cgst_amount:.2f}",
                f"{item.sgst_amount:.2f}",
                f"{item.line_total:.2f}",
            ]
            for w, v in zip(cols, row_vals):
                pdf.cell(w, 6, v, border=1, align="C")
            pdf.ln()

        pdf.ln(3)

        # ---- Totals ----
        pdf.set_font("Helvetica", "", 9)
        label_x = 130
        pdf.set_x(label_x)
        pdf.cell(40, 6, "Subtotal:")
        pdf.cell(30, 6, f"Rs. {bill.subtotal:.2f}", new_x="LMARGIN", new_y="NEXT")
        pdf.set_x(label_x)
        pdf.cell(40, 6, "CGST:")
        pdf.cell(30, 6, f"Rs. {bill.total_cgst:.2f}", new_x="LMARGIN", new_y="NEXT")
        pdf.set_x(label_x)
        pdf.cell(40, 6, "SGST:")
        pdf.cell(30, 6, f"Rs. {bill.total_sgst:.2f}", new_x="LMARGIN", new_y="NEXT")
        pdf.set_x(label_x)
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(40, 7, "GRAND TOTAL:")
        pdf.cell(30, 7, f"Rs. {bill.total_amount:.2f}", new_x="LMARGIN", new_y="NEXT")

        pdf.ln(5)
        # ---- Footer ----
        pdf.set_font("Helvetica", "I", 8)
        pdf.cell(0, 5, "This is a computer-generated invoice.", align="C", new_x="LMARGIN", new_y="NEXT")
        pdf.cell(0, 5, "Thank you for your business!", align="C", new_x="LMARGIN", new_y="NEXT")

        file_name = f"invoice_{bill.bill_number}.pdf"
        file_path = f"/tmp/{file_name}"
        pdf.output(file_path)
        file_size = os.path.getsize(file_path)

        return InvoicePDFResult(
            file_path=file_path,
            bill_number=bill.bill_number,
            file_size_bytes=file_size,
            message=f"Invoice {bill.bill_number} generated.",
        )

    # ------------------------------------------------------------------
    # PPTX Analysis Deck
    # ------------------------------------------------------------------

    async def generate_analysis_pptx(
        self,
        store_id: str,
        telegram_user_id: int,
        period: str = "THIS_WEEK",  # TODAY | THIS_WEEK | THIS_MONTH
    ) -> AnalysisPPTXResult:
        """
        Generate a 5-slide PPTX analysis deck.
        Returns the file path; handler sends it to Telegram and deletes it.
        """
        from pptx import Presentation  # type: ignore
        from pptx.chart.data import ChartData  # type: ignore
        from pptx.enum.chart import XL_CHART_TYPE  # type: ignore
        from pptx.util import Inches, Pt  # type: ignore

        store = await self._identity.get_store(telegram_user_id)
        shop_name = store.shop_name if store else "Your Store"

        today = date.today()
        if period == "TODAY":
            start_date = end_date = today.isoformat()
            label = f"Today ({today.strftime('%d %b %Y')})"
        elif period == "THIS_MONTH":
            start_date = today.replace(day=1).isoformat()
            end_date = today.isoformat()
            label = today.strftime("%B %Y")
        else:  # THIS_WEEK (default)
            start_date = (today - timedelta(days=today.weekday())).isoformat()
            end_date = today.isoformat()
            label = f"Week of {start_date}"

        data = await self._analytics.get_analytics_deck_data(
            store_id=store_id,
            store_name=shop_name,
            start_date=start_date,
            end_date=end_date,
        )

        prs = Presentation()
        prs.slide_width = Inches(13.33)
        prs.slide_height = Inches(7.5)

        # Slide 1: Executive Summary
        slide1 = prs.slides.add_slide(prs.slide_layouts[5])
        tf = slide1.shapes.add_textbox(Inches(0.5), Inches(0.3), Inches(12), Inches(1)).text_frame
        tf.text = f"{shop_name} — {label}"
        tf.paragraphs[0].runs[0].font.size = Pt(24)
        tf.paragraphs[0].runs[0].font.bold = True

        summary_text = (
            f"Total Sales: Rs. {data.summary.total_sales:,.2f}\n"
            f"Bills Cut: {data.summary.bill_count}\n"
            f"GST Collected: Rs. {data.summary.total_tax:,.2f}\n\n"
            f"Cash: Rs. {data.summary.cash_sales:,.2f}  "
            f"UPI: Rs. {data.summary.upi_sales:,.2f}  "
            f"Card: Rs. {data.summary.card_sales:,.2f}  "
            f"Credit: Rs. {data.summary.credit_sales:,.2f}"
        )
        tf2 = slide1.shapes.add_textbox(Inches(0.5), Inches(1.5), Inches(12), Inches(5)).text_frame
        tf2.text = summary_text
        tf2.paragraphs[0].runs[0].font.size = Pt(14)

        # Slide 2: Sales Trend
        if data.sales_trend:
            slide2 = prs.slides.add_slide(prs.slide_layouts[5])
            _add_title(slide2, f"Sales Trend — {label}", prs)
            chart_data = ChartData()
            chart_data.categories = [t.date for t in data.sales_trend]
            chart_data.add_series("Daily Sales (Rs.)", [t.total_sales for t in data.sales_trend])
            slide2.shapes.add_chart(
                XL_CHART_TYPE.LINE,
                Inches(1), Inches(1.5), Inches(11), Inches(5.5),
                chart_data,
            )

        # Slide 3: Top Items
        if data.top_items:
            slide3 = prs.slides.add_slide(prs.slide_layouts[5])
            _add_title(slide3, "Top Selling Items", prs)
            chart_data3 = ChartData()
            chart_data3.categories = [i.product_name[:20] for i in data.top_items[:10]]
            chart_data3.add_series("Units Sold", [i.quantity_sold for i in data.top_items[:10]])
            slide3.shapes.add_chart(
                XL_CHART_TYPE.COLUMN_CLUSTERED,
                Inches(1), Inches(1.5), Inches(11), Inches(5.5),
                chart_data3,
            )

        # Slide 4: Stock Health
        slide4 = prs.slides.add_slide(prs.slide_layouts[5])
        _add_title(slide4, "Stock Health", prs)
        health_lines = [
            f"Total Products: {data.stock_health.total_products}  "
            f"In Stock: {data.stock_health.in_stock}  "
            f"Low: {data.stock_health.low_stock}  "
            f"Out: {data.stock_health.out_of_stock}",
            "",
        ]
        for item in data.stock_health.items[:15]:
            status_icon = {"OK": "OK", "LOW": "LOW", "CRITICAL": "CRIT", "OUT_OF_STOCK": "OUT"}.get(item.status, "?")
            health_lines.append(
                f"[{status_icon}] {item.product_name[:25]:25}  "
                f"Stock: {item.quantity_in_stock:.1f} {item.unit}  "
                f"Reorder: {item.reorder_level:.1f}"
            )
        tf4 = slide4.shapes.add_textbox(Inches(0.5), Inches(1.5), Inches(12), Inches(5.5)).text_frame
        tf4.text = "\n".join(health_lines)
        tf4.paragraphs[0].runs[0].font.size = Pt(10)

        # Slide 5: GST Summary
        slide5 = prs.slides.add_slide(prs.slide_layouts[5])
        _add_title(slide5, "GST Summary", prs)
        gst_lines = [
            f"Period: {data.gst_summary.period_start} to {data.gst_summary.period_end}",
            f"Total Taxable: Rs. {data.gst_summary.total_taxable_value:,.2f}",
            f"Total CGST: Rs. {data.gst_summary.total_cgst:,.2f}",
            f"Total SGST: Rs. {data.gst_summary.total_sgst:,.2f}",
            f"Total GST: Rs. {data.gst_summary.total_gst:,.2f}",
            "",
        ]
        for slab in data.gst_summary.by_slab:
            gst_lines.append(
                f"{slab.gst_rate:.0f}%  Taxable: Rs. {slab.taxable_value:,.2f}  "
                f"GST: Rs. {slab.total_gst:,.2f}  Items: {slab.item_count}"
            )
        tf5 = slide5.shapes.add_textbox(Inches(0.5), Inches(1.5), Inches(12), Inches(5.5)).text_frame
        tf5.text = "\n".join(gst_lines)
        tf5.paragraphs[0].runs[0].font.size = Pt(12)

        file_name = f"analysis_{store_id[:8]}_{end_date}.pptx"
        file_path = f"/tmp/{file_name}"
        prs.save(file_path)
        file_size = os.path.getsize(file_path)

        return AnalysisPPTXResult(
            file_path=file_path,
            period_label=label,
            file_size_bytes=file_size,
            message=f"Analysis deck for {label} generated.",
        )


# ------------------------------------------------------------------
# PPTX helper
# ------------------------------------------------------------------

def _add_title(slide, title: str, prs) -> None:
    from pptx.util import Inches, Pt  # type: ignore

    tf = slide.shapes.add_textbox(
        Inches(0.5), Inches(0.2), Inches(12), Inches(0.9)
    ).text_frame
    tf.text = title
    tf.paragraphs[0].runs[0].font.size = Pt(20)
    tf.paragraphs[0].runs[0].font.bold = True
