"""
GST computation utilities.

All kirana-store bills are intra-state, so tax is always split into
equal CGST and SGST.  The sgst absorbs any half-penny rounding delta
to ensure  cgst + sgst == gst_total  exactly.

PRICING MODEL — GST-INCLUSIVE MRP:
Indian retail MRP (Maximum Retail Price) already includes GST.
The customer pays exactly qty × MRP — GST is NOT added on top.
For GST reporting, the taxable value and tax component are back-calculated:
    line_total    = qty × MRP
    taxable_value = line_total / (1 + rate/100)   [GST-exclusive base]
    gst_total     = line_total - taxable_value     [tax embedded in MRP]
    cgst          = gst_total / 2  (rounded up)
    sgst          = gst_total - cgst              (absorbs rounding delta)
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def compute_line_gst(
    quantity: float,
    unit_price: float,
    gst_rate: float,
) -> dict[str, float]:
    """
    Compute GST amounts for a single line item.

    MRP is GST-inclusive — the customer pays exactly qty × MRP.
    Tax components are back-calculated from the inclusive price for
    GST reporting (CGST = SGST = gst_total / 2).

    Returns a dict with keys:
        taxable_value, gst_total, cgst_amount, sgst_amount, line_total
    """
    q = Decimal(str(quantity))
    p = Decimal(str(unit_price))
    r = Decimal(str(gst_rate))

    # Customer pays exactly qty × MRP — no GST added on top
    line_total = (q * p).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    if r == 0:
        # Zero-rated: full amount is taxable, no embedded tax
        taxable = line_total
        gst_total = Decimal("0.00")
    else:
        # Back-calculate from GST-inclusive price:
        #   line_total = taxable × (1 + rate/100)
        #   taxable    = line_total / (1 + rate/100)
        divisor = 1 + r / 100
        taxable = (line_total / divisor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        gst_total = line_total - taxable  # exact: avoids independent rounding gap

    cgst = (gst_total / 2).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sgst = gst_total - cgst  # absorbs rounding delta

    return {
        "taxable_value": float(taxable),
        "gst_total": float(gst_total),
        "cgst_amount": float(cgst),
        "sgst_amount": float(sgst),
        "line_total": float(line_total),
    }


def aggregate_gst(line_items: list[dict[str, float]]) -> dict[str, float]:
    """
    Aggregate GST across multiple line items.

    Each item must have keys: taxable_value, cgst_amount, sgst_amount, line_total.
    Returns: subtotal, total_cgst, total_sgst, total_amount.
    """
    subtotal = sum(Decimal(str(i["taxable_value"])) for i in line_items)
    total_cgst = sum(Decimal(str(i["cgst_amount"])) for i in line_items)
    total_sgst = sum(Decimal(str(i["sgst_amount"])) for i in line_items)
    total_amount = subtotal + total_cgst + total_sgst

    return {
        "subtotal": float(subtotal.quantize(Decimal("0.01"))),
        "total_cgst": float(total_cgst.quantize(Decimal("0.01"))),
        "total_sgst": float(total_sgst.quantize(Decimal("0.01"))),
        "total_amount": float(total_amount.quantize(Decimal("0.01"))),
    }

