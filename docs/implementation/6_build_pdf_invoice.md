# Implementation Guide 6: Build PDF Invoice Generator

**Order:** Sixth — implement PDF generation and wire to Documents MCP.  
**Reference Docs:** `docs/mcp/documents/documents_mcp.md`

---

## Prerequisites

- Billing MCP implemented and tested (Guide 2 complete)
- Lambda deployed and working (Guide 5 complete)
- Choose a PDF library: **fpdf2** (recommended — lightweight, pure Python, Lambda-friendly)

---

## Step 1: Install PDF Library

```bash
pip install fpdf2
```

Add to `requirements.txt`:
```
fpdf2>=2.7.0
```

---

## Step 2: Abstract PDF Renderer Interface

```python
# src/mcp/documents/pdf_renderer.py
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.mcp.billing.models import BillDetailResult
    from src.mcp.identity.models import StoreResult

class PDFRenderer(ABC):
    """Abstract interface for PDF invoice rendering.
    Swap implementations without changing Documents MCP."""
    
    @abstractmethod
    def render_invoice(
        self,
        bill: 'BillDetailResult',
        store: 'StoreResult',
        output_path: str
    ) -> None:
        """Render a GST invoice PDF to the given file path."""
        pass
```

---

## Step 3: fpdf2 Implementation

```python
# src/mcp/documents/fpdf2_renderer.py
from fpdf import FPDF
from src.mcp.documents.pdf_renderer import PDFRenderer

class Fpdf2Renderer(PDFRenderer):
    
    def render_invoice(self, bill, store, output_path: str) -> None:
        pdf = FPDF(orientation='P', unit='mm', format='A4')
        pdf.add_page()
        pdf.set_auto_page_break(auto=True, margin=15)
        
        self._add_header(pdf, store, bill)
        self._add_items_table(pdf, bill.items)
        self._add_totals(pdf, bill)
        self._add_footer(pdf, bill)
        
        pdf.output(output_path)
    
    def _add_header(self, pdf: FPDF, store, bill) -> None:
        # Shop name (large, bold)
        pdf.set_font('Helvetica', 'B', 16)
        pdf.cell(0, 10, store.shop_name, align='C', new_x='LMARGIN', new_y='NEXT')
        
        # Address and contact (small, regular)
        pdf.set_font('Helvetica', '', 9)
        if store.address:
            pdf.cell(0, 5, store.address, align='C', new_x='LMARGIN', new_y='NEXT')
        if store.phone:
            pdf.cell(0, 5, f"Phone: {store.phone}", align='C', new_x='LMARGIN', new_y='NEXT')
        if store.gstin:
            pdf.cell(0, 5, f"GSTIN: {store.gstin}", align='C', new_x='LMARGIN', new_y='NEXT')
        
        pdf.ln(3)
        
        # "TAX INVOICE" title
        pdf.set_font('Helvetica', 'B', 12)
        pdf.cell(0, 8, 'TAX INVOICE', align='C', new_x='LMARGIN', new_y='NEXT')
        
        # Horizontal rule
        pdf.set_draw_color(0, 0, 0)
        pdf.line(10, pdf.get_y(), 200, pdf.get_y())
        pdf.ln(3)
        
        # Bill details (two columns)
        pdf.set_font('Helvetica', '', 9)
        pdf.cell(95, 5, f"Bill No: {bill.bill_number}")
        pdf.cell(95, 5, f"Date: {bill.created_at[:10]}", new_x='LMARGIN', new_y='NEXT')
        pdf.cell(95, 5, f"Payment: {bill.payment_mode}")
        if bill.payment_reference:
            pdf.cell(95, 5, f"Ref: {bill.payment_reference}", new_x='LMARGIN', new_y='NEXT')
        else:
            pdf.cell(95, 5, "", new_x='LMARGIN', new_y='NEXT')
        pdf.ln(3)
    
    def _add_items_table(self, pdf: FPDF, items) -> None:
        # Table header
        pdf.set_font('Helvetica', 'B', 8)
        pdf.set_fill_color(240, 240, 240)
        
        col_widths = [8, 55, 15, 15, 12, 18, 18, 18, 22]
        headers = ['#', 'Item', 'HSN', 'Qty', 'Unit', 'Rate ₹', 'CGST ₹', 'SGST ₹', 'Total ₹']
        
        for i, (header, width) in enumerate(zip(headers, col_widths)):
            pdf.cell(width, 7, header, border=1, fill=True, align='C')
        pdf.ln()
        
        # Table rows
        pdf.set_font('Helvetica', '', 8)
        for i, item in enumerate(items, 1):
            row = [
                str(i),
                item.product_name_snapshot[:30],  # Truncate long names
                item.hsn_code_snapshot or '--',
                f"{item.quantity:.2f}",
                item.unit_snapshot,
                f"{item.unit_price:.2f}",
                f"{item.cgst_amount:.2f}",
                f"{item.sgst_amount:.2f}",
                f"{item.line_total:.2f}"
            ]
            for j, (value, width) in enumerate(zip(row, col_widths)):
                align = 'R' if j >= 5 else 'L'
                pdf.cell(width, 6, value, border=1, align=align)
            pdf.ln()
        
        pdf.ln(3)
    
    def _add_totals(self, pdf: FPDF, bill) -> None:
        # Right-aligned totals box
        pdf.set_font('Helvetica', '', 9)
        label_x = 130
        value_x = 160
        col_w = 30
        
        rows = [
            ('Subtotal:', f"₹{bill.subtotal:.2f}"),
            ('CGST:', f"₹{bill.total_cgst:.2f}"),
            ('SGST:', f"₹{bill.total_sgst:.2f}"),
        ]
        
        for label, value in rows:
            pdf.set_x(label_x)
            pdf.cell(col_w, 6, label, align='R')
            pdf.cell(col_w, 6, value, align='R', new_x='LMARGIN', new_y='NEXT')
        
        # Horizontal rule before grand total
        y = pdf.get_y()
        pdf.line(label_x, y, 200, y)
        
        # Grand total (bold, larger)
        pdf.set_font('Helvetica', 'B', 11)
        pdf.set_x(label_x)
        pdf.cell(col_w, 8, 'TOTAL:', align='R')
        pdf.cell(col_w, 8, f"₹{bill.total_amount:.2f}", align='R', new_x='LMARGIN', new_y='NEXT')
        
        pdf.ln(5)
    
    def _add_footer(self, pdf: FPDF, bill) -> None:
        pdf.set_font('Helvetica', 'I', 8)
        pdf.set_text_color(100, 100, 100)
        pdf.cell(0, 5, "This is a computer-generated invoice.", align='C', new_x='LMARGIN', new_y='NEXT')
        pdf.cell(0, 5, "Thank you for your business!", align='C', new_x='LMARGIN', new_y='NEXT')
```

---

## Step 4: Wire into Documents MCP

```python
# src/mcp/documents/documents_mcp.py
import os
from src.mcp.documents.fpdf2_renderer import Fpdf2Renderer

class DocumentsMCP:
    def __init__(self, billing_mcp, analytics_mcp, identity_mcp):
        self.billing = billing_mcp
        self.analytics = analytics_mcp
        self.identity = identity_mcp
        self.pdf_renderer = Fpdf2Renderer()  # Swap here to change library
    
    async def generate_invoice_pdf(
        self,
        bill_id: str,
        store_id: str,
        telegram_chat_id: int
    ):
        from src.telegram.telegram_client import get_telegram_client
        
        bill = await self.billing.get_bill(bill_id)
        store = await self.identity.get_store_by_id(store_id)
        
        pdf_path = f"/tmp/invoice_{bill.bill_number}.pdf"
        
        try:
            self.pdf_renderer.render_invoice(bill, store, pdf_path)
            
            telegram = get_telegram_client()
            await telegram.send_document(
                chat_id=telegram_chat_id,
                file_path=pdf_path,
                caption=f"🧾 Invoice {bill.bill_number} — ₹{bill.total_amount:.2f}"
            )
            return {"success": True, "message": "Invoice PDF sent!"}
        finally:
            # Always clean up /tmp
            if os.path.exists(pdf_path):
                os.remove(pdf_path)
```

---

## Step 5: Test PDF Generation

```python
# tests/test_pdf_generation.py
# Use a real bill from test DB or mock the bill data

bill_mock = BillDetailResult(
    bill_id="test-001",
    bill_number="BL-2024-001",
    items=[
        BillItemDetail(
            product_name_snapshot="Sugar",
            hsn_code_snapshot=None,
            quantity=2.0, unit_snapshot="KG",
            unit_price=45.0, gst_rate=0.0,
            taxable_value=90.0, cgst_amount=0.0,
            sgst_amount=0.0, line_total=90.0
        ),
        BillItemDetail(
            product_name_snapshot="Maggi 70g",
            hsn_code_snapshot="1902",
            quantity=4.0, unit_snapshot="PACKET",
            unit_price=14.0, gst_rate=12.0,
            taxable_value=56.0, cgst_amount=3.36,
            sgst_amount=3.36, line_total=62.72
        )
    ],
    subtotal=146.0, total_cgst=3.36, total_sgst=3.36, total_amount=152.72,
    payment_mode="UPI", created_at="2024-01-15T10:30:00Z"
)

renderer = Fpdf2Renderer()
renderer.render_invoice(bill_mock, store_mock, "/tmp/test_invoice.pdf")
# Open /tmp/test_invoice.pdf to visually verify layout
```

---

## Validation Checklist

- [ ] PDF generates without errors for a 1-item bill
- [ ] PDF generates without errors for a 10-item bill (page overflow handled)
- [ ] GST amounts are correct (verify manually with test data)
- [ ] Loose items show 0.00 for CGST and SGST
- [ ] Bill number, date, and payment mode appear correctly
- [ ] GSTIN shown only if store has GSTIN set
- [ ] File is deleted from `/tmp` after sending
- [ ] File sends successfully via Telegram `sendDocument`
