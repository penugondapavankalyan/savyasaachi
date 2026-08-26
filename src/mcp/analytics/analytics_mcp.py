"""
Analytics MCP implementation.

Owns (writes): analytics.daily_summary
Reads: billing.bills, billing.bill_items, inventory.inventory,
       inventory.stock_movements, catalogue.products
"""

from __future__ import annotations


def _one(resp):
    """Safe helper: return first row or None. Handles supabase-py 2.x quirks."""
    if resp is None:
        return None
    data = resp.data
    if not data:
        return None
    return data[0] if isinstance(data, list) else data


from datetime import date
from src.utils.ist import date_range_iso, today_ist
from typing import Optional

from src.db.supabase_client import get_client
from src.mcp.analytics.models import (
    AnalyticsDeckData,
    CloseDayResult,
    CustomerCreditSummary,
    DailySummaryResult,
    DailyTrendPoint,
    GSTSlabSummary,
    GSTSummaryResult,
    KhataOverviewData,
    StockHealthItem,
    StockHealthReport,
    TopItemResult,
)


class AnalyticsMCP:
    """Read-heavy aggregation module for sales analytics."""

    def __init__(self) -> None:
        self.db = get_client()

    # ------------------------------------------------------------------
    # Daily summary
    # ------------------------------------------------------------------

    async def get_daily_summary(
        self,
        store_id: str,
        summary_date: Optional[str] = None,
    ) -> DailySummaryResult:
        """
        Return daily sales summary.  Uses cached daily_summary if available,
        otherwise computes live from billing.bills.
        """
        target_date = summary_date or today_ist().isoformat()

        # Try cached
        cached_resp = (
            self.db.schema("analytics")
            .table("daily_summary")
            .select("*")
            .eq("store_id", store_id)
            .eq("summary_date", target_date)
            .limit(1)
            .execute()
        )
        cached = _one(cached_resp)
        if cached:
            return _row_to_summary(cached, is_day_closed=True)

        # Compute live from bills
        return await self._compute_summary_live(store_id, target_date, is_day_closed=False)

    async def close_day(
        self,
        store_id: str,
        summary_date: Optional[str] = None,
    ) -> CloseDayResult:
        """
        Aggregate bills into daily_summary.  Idempotent (ON CONFLICT DO UPDATE).
        """
        target_date = summary_date or today_ist().isoformat()
        live = await self._compute_summary_live(store_id, target_date, is_day_closed=False)

        # Check if previously closed
        existing_resp = (
            self.db.schema("analytics")
            .table("daily_summary")
            .select("id")
            .eq("store_id", store_id)
            .eq("summary_date", target_date)
            .limit(1)
            .execute()
        )
        already_closed = _one(existing_resp) is not None

        # Upsert daily_summary
        top_items_json = [
            {
                "product_id": i.product_id,
                "product_name": i.product_name,
                "brand": i.brand,
                "unit": i.unit,
                "quantity_sold": i.quantity_sold,
                "revenue": i.revenue,
                "gst_collected": i.gst_collected,
            }
            for i in live.top_items
        ]

        self.db.schema("analytics").table("daily_summary").upsert(
            {
                "store_id": store_id,
                "summary_date": target_date,
                "bill_count": live.bill_count,
                "total_sales": live.total_sales,
                "total_cgst": live.total_cgst,
                "total_sgst": live.total_sgst,
                "total_tax": live.total_tax,
                "cash_sales": live.cash_sales,
                "upi_sales": live.upi_sales,
                "card_sales": live.card_sales,
                "credit_sales": live.credit_sales,
                "top_items": top_items_json,
            },
            on_conflict="store_id,summary_date",
        ).execute()

        live.is_day_closed = True
        live.message = f"Day {target_date} closed. {live.bill_count} bills, ₹{live.total_sales:.2f} total."

        return CloseDayResult(
            summary=live,
            already_closed=already_closed,
            message=live.message,
        )

    # ------------------------------------------------------------------
    # Trend / Top items / GST
    # ------------------------------------------------------------------

    async def get_sales_trend(
        self,
        store_id: str,
        start_date: str,
        end_date: str,
    ) -> list[DailyTrendPoint]:
        """Return daily sales totals for a date range."""
        resp = (
            self.db.schema("analytics")
            .table("daily_summary")
            .select("summary_date, total_sales, bill_count, total_tax")
            .eq("store_id", store_id)
            .gte("summary_date", start_date)
            .lte("summary_date", end_date)
            .order("summary_date")
            .execute()
        )
        return [
            DailyTrendPoint(
                date=r["summary_date"],
                total_sales=float(r["total_sales"]),
                bill_count=r["bill_count"],
                total_tax=float(r["total_tax"]),
            )
            for r in (resp.data or [])
        ]

    async def get_top_items(
        self,
        store_id: str,
        start_date: str,
        end_date: str,
        limit: int = 10,
    ) -> list[TopItemResult]:
        """Return best-selling items by quantity for a period."""
        # Query bill_items joined with bills for the date range
        bills_resp = (
            self.db.schema("billing")
            .table("bills")
            .select("id")
            .eq("store_id", store_id)
            .gte("created_at", date_range_iso(start_date, end_date)[0])
            .lte("created_at", date_range_iso(start_date, end_date)[1])
            .execute()
        )
        bill_ids = [r["id"] for r in (bills_resp.data or [])]
        if not bill_ids:
            return []

        items_resp = (
            self.db.schema("billing")
            .table("bill_items")
            .select(
                "product_id, product_name_snapshot, brand_snapshot, unit_snapshot, "
                "quantity, line_total, cgst_amount, sgst_amount"
            )
            .in_("bill_id", bill_ids)
            .execute()
        )

        # Aggregate in Python
        agg: dict[str, dict] = {}
        for row in items_resp.data or []:
            pid = row.get("product_id") or row["product_name_snapshot"]
            if pid not in agg:
                agg[pid] = {
                    "product_id": row.get("product_id"),
                    "product_name": row["product_name_snapshot"],
                    "brand": row.get("brand_snapshot"),
                    "unit": row["unit_snapshot"],
                    "quantity_sold": 0.0,
                    "revenue": 0.0,
                    "gst_collected": 0.0,
                }
            agg[pid]["quantity_sold"] += float(row["quantity"])
            agg[pid]["revenue"] += float(row["line_total"])
            agg[pid]["gst_collected"] += float(row["cgst_amount"]) + float(row["sgst_amount"])

        sorted_by_qty = sorted(agg.values(), key=lambda x: x["quantity_sold"], reverse=True)
        sorted_by_rev = sorted(agg.values(), key=lambda x: x["revenue"], reverse=True)
        rev_rank = {v["product_name"]: i + 1 for i, v in enumerate(sorted_by_rev)}

        return [
            TopItemResult(
                product_id=v["product_id"],
                product_name=v["product_name"],
                brand=v["brand"],
                unit=v["unit"],
                quantity_sold=round(v["quantity_sold"], 3),
                revenue=round(v["revenue"], 2),
                gst_collected=round(v["gst_collected"], 2),
                rank_by_quantity=i + 1,
                rank_by_revenue=rev_rank.get(v["product_name"], 0),
            )
            for i, v in enumerate(sorted_by_qty[:limit])
        ]

    async def get_stock_health(self, store_id: str) -> StockHealthReport:
        """Comprehensive stock health snapshot."""
        # PostgREST does not support cross-schema embedded-resource joins
        # (e.g. 'products:catalogue.products(...)' from schema 'inventory').
        # Fetch inventory rows first, then bulk-fetch product metadata from catalogue.
        resp = (
            self.db.schema("inventory")
            .table("inventory")
            .select("product_id, quantity_in_stock, reorder_level")
            .eq("store_id", store_id)
            .execute()
        )
        inv_rows = resp.data or []

        # Bulk fetch all product metadata in one catalogue query
        product_ids = [r["product_id"] for r in inv_rows]
        prod_map: dict = {}
        if product_ids:
            prod_resp = (
                self.db.schema("catalogue")
                .table("products")
                .select("id, name, brand, unit")
                .in_("id", product_ids)
                .execute()
            )
            prod_map = {r["id"]: r for r in (prod_resp.data or [])}

        items = []
        in_stock = 0
        low_stock = 0
        out_of_stock = 0

        for row in inv_rows:
            qty = float(row["quantity_in_stock"])
            reorder = float(row["reorder_level"])
            prod = prod_map.get(row["product_id"], {})

            if qty == 0:
                status = "OUT_OF_STOCK"
                out_of_stock += 1
            elif qty <= reorder:
                status = "LOW" if reorder == 0 or qty > reorder * 0.5 else "CRITICAL"
                low_stock += 1
            else:
                status = "OK"
                in_stock += 1

            items.append(
                StockHealthItem(
                    product_id=row["product_id"],
                    product_name=prod.get("name", ""),
                    brand=prod.get("brand"),
                    unit=prod.get("unit", ""),
                    quantity_in_stock=qty,
                    reorder_level=reorder,
                    status=status,
                )
            )

        return StockHealthReport(
            total_products=len(items),
            in_stock=in_stock,
            low_stock=low_stock,
            out_of_stock=out_of_stock,
            items=sorted(items, key=lambda x: x.quantity_in_stock),
        )

    async def get_gst_summary(
        self,
        store_id: str,
        start_date: str,
        end_date: str,
    ) -> GSTSummaryResult:
        """GST collected by slab for a period."""
        bills_resp = (
            self.db.schema("billing")
            .table("bills")
            .select("id")
            .eq("store_id", store_id)
            .gte("created_at", date_range_iso(start_date, end_date)[0])
            .lte("created_at", date_range_iso(start_date, end_date)[1])
            .execute()
        )
        bill_ids = [r["id"] for r in (bills_resp.data or [])]
        if not bill_ids:
            return GSTSummaryResult(
                period_start=start_date,
                period_end=end_date,
                total_taxable_value=0,
                total_cgst=0,
                total_sgst=0,
                total_gst=0,
                by_slab=[],
            )

        items_resp = (
            self.db.schema("billing")
            .table("bill_items")
            .select("gst_rate, taxable_value, cgst_amount, sgst_amount")
            .in_("bill_id", bill_ids)
            .execute()
        )

        slab_agg: dict[float, dict] = {}
        total_taxable = 0.0
        total_cgst = 0.0
        total_sgst = 0.0

        for row in items_resp.data or []:
            rate = float(row["gst_rate"])
            tv = float(row["taxable_value"])
            cgst = float(row["cgst_amount"])
            sgst = float(row["sgst_amount"])

            total_taxable += tv
            total_cgst += cgst
            total_sgst += sgst

            if rate not in slab_agg:
                slab_agg[rate] = {"taxable_value": 0.0, "cgst": 0.0, "sgst": 0.0, "item_count": 0}
            slab_agg[rate]["taxable_value"] += tv
            slab_agg[rate]["cgst"] += cgst
            slab_agg[rate]["sgst"] += sgst
            slab_agg[rate]["item_count"] += 1

        by_slab = [
            GSTSlabSummary(
                gst_rate=rate,
                taxable_value=round(v["taxable_value"], 2),
                cgst=round(v["cgst"], 2),
                sgst=round(v["sgst"], 2),
                total_gst=round(v["cgst"] + v["sgst"], 2),
                item_count=v["item_count"],
            )
            for rate, v in sorted(slab_agg.items())
        ]

        return GSTSummaryResult(
            period_start=start_date,
            period_end=end_date,
            total_taxable_value=round(total_taxable, 2),
            total_cgst=round(total_cgst, 2),
            total_sgst=round(total_sgst, 2),
            total_gst=round(total_cgst + total_sgst, 2),
            by_slab=by_slab,
        )

    async def get_khata_overview(
        self,
        store_id: str,
        start_date: str,
        end_date: str,
    ) -> KhataOverviewData:
        """Aggregate khata (credit ledger) data for the PPTX khata overview slide.

        Two bulk queries only:
        1. All khata_entries for the store → compute per-customer balances in Python
        2. All billing.bills in the period with is_credit=True → credit-by-day totals
        3. billing.customers for name/phone lookup (one query)
        """
        # ── 1. All customers ────────────────────────────────────────────
        cust_resp = (
            self.db.schema("billing")
            .table("customers")
            .select("id, name, phone")
            .eq("store_id", store_id)
            .eq("is_active", True)
            .execute()
        )
        customers = {r["id"]: r for r in (cust_resp.data or [])}

        # ── 2. All khata_entries → balance map ──────────────────────────
        khata_resp = (
            self.db.schema("khata")
            .table("khata_entries")
            .select("customer_id, amount_delta")
            .eq("store_id", store_id)
            .execute()
        )
        balance_map: dict[str, float] = {}
        for entry in (khata_resp.data or []):
            cid = entry["customer_id"]
            balance_map[cid] = balance_map.get(cid, 0.0) + float(entry["amount_delta"])

        # ── Compute summary stats ────────────────────────────────────────
        total_credit_given = 0.0
        total_shop_owes    = 0.0
        credit_count       = 0
        shop_owes_count    = 0
        highest_debtor: CustomerCreditSummary | None   = None
        highest_creditor: CustomerCreditSummary | None = None

        for cid, balance in balance_map.items():
            cust = customers.get(cid)
            if not cust:
                continue
            if balance > 0:
                total_credit_given += balance
                credit_count += 1
                if highest_debtor is None or balance > highest_debtor.balance:
                    highest_debtor = CustomerCreditSummary(
                        customer_id=cid, name=cust["name"],
                        phone=cust["phone"], balance=balance,
                    )
            elif balance < 0:
                total_shop_owes += abs(balance)
                shop_owes_count += 1
                if highest_creditor is None or abs(balance) > abs(highest_creditor.balance):
                    highest_creditor = CustomerCreditSummary(
                        customer_id=cid, name=cust["name"],
                        phone=cust["phone"], balance=balance,
                    )

        # ── 3. Credit bills in period → credit-by-day ────────────────────
        credit_bills_resp = (
            self.db.schema("billing")
            .table("bills")
            .select("total_amount, created_at")
            .eq("store_id", store_id)
            .eq("is_credit", True)
            .gte("created_at", date_range_iso(start_date, end_date)[0])
            .lte("created_at", date_range_iso(start_date, end_date)[1])
            .execute()
        )
        from datetime import datetime as _dt
        from src.utils.ist import IST
        day_credit: dict[str, float] = {}
        for b in (credit_bills_resp.data or []):
            try:
                ts_str = b["created_at"]
                if ts_str.endswith("Z"):
                    ts_str = ts_str[:-1] + "+00:00"
                day_key = _dt.fromisoformat(ts_str).astimezone(IST).date().isoformat()
            except Exception:
                day_key = b["created_at"][:10]
            day_credit[day_key] = day_credit.get(day_key, 0.0) + float(b["total_amount"])

        # Fill all days in range (zero if no credit that day)
        from datetime import date as _date, timedelta as _td
        credit_by_day: list[tuple[str, float]] = []
        d = _date.fromisoformat(start_date)
        e = _date.fromisoformat(end_date)
        while d <= e:
            dk = d.isoformat()
            credit_by_day.append((dk, round(day_credit.get(dk, 0.0), 2)))
            d += _td(days=1)

        return KhataOverviewData(
            total_credit_given=round(total_credit_given, 2),
            total_shop_owes=round(total_shop_owes, 2),
            credit_customer_count=credit_count,
            shop_owes_customer_count=shop_owes_count,
            highest_debtor=highest_debtor,
            highest_creditor=highest_creditor,
            credit_by_day=credit_by_day,
        )

    async def get_analytics_deck_data(
        self,
        store_id: str,
        store_name: str,
        start_date: str,
        end_date: str,
    ) -> AnalyticsDeckData:
        """Collect all data needed for the PPTX analytics deck.

        Uses a single bulk bills query to build daily_summaries — avoids
        N per-day DB round-trips that caused timeouts on THIS_WEEK/THIS_MONTH.
        """
        from datetime import date as _date, timedelta as _td

        summary       = await self.get_daily_summary(store_id, end_date)
        sales_trend   = await self.get_sales_trend(store_id, start_date, end_date)
        top_items     = await self.get_top_items(store_id, start_date, end_date)
        stock_health  = await self.get_stock_health(store_id)
        gst_summary   = await self.get_gst_summary(store_id, start_date, end_date)
        khata_overview = await self.get_khata_overview(store_id, start_date, end_date)

        # ── Build per-day summary rows with a SINGLE bulk query ──────────
        # Fetch all bills for the whole period in one shot, then aggregate
        # in Python per calendar day.  No per-day DB calls — avoids N×3
        # sequential round-trips that caused request timeouts.
        bulk_resp = (
            self.db.schema("billing")
            .table("bills")
            .select(
                "total_amount, total_cgst, total_sgst, payment_mode, is_credit, created_at"
            )
            .eq("store_id", store_id)
            .gte("created_at", date_range_iso(start_date, end_date)[0])
            .lte("created_at", date_range_iso(start_date, end_date)[1])
            .execute()
        )
        all_bills = bulk_resp.data or []

        # Group bills by IST calendar date (created_at is ISO timestamp with tz)
        from datetime import timezone as _tz
        from src.utils.ist import IST  # IST tzinfo object

        day_buckets: dict[str, list[dict]] = {}
        for b in all_bills:
            # Parse timestamp and convert to IST date
            try:
                ts_str = b["created_at"]
                # Handle both "+05:30" and "Z" suffixes
                if ts_str.endswith("Z"):
                    ts_str = ts_str[:-1] + "+00:00"
                from datetime import datetime as _dt
                ts = _dt.fromisoformat(ts_str).astimezone(IST)
                day_key = ts.date().isoformat()
            except Exception:
                # Fallback: use raw date prefix
                day_key = b["created_at"][:10]
            day_buckets.setdefault(day_key, []).append(b)

        # Build a DailySummaryResult for each calendar day in the period
        daily_summaries: list[DailySummaryResult] = []
        s = _date.fromisoformat(start_date)
        e = _date.fromisoformat(end_date)
        day = s
        while day <= e:
            dk = day.isoformat()
            day_bills = day_buckets.get(dk, [])
            bill_count   = len(day_bills)
            total_sales  = sum(float(b["total_amount"]) for b in day_bills)
            total_cgst   = sum(float(b["total_cgst"])   for b in day_bills)
            total_sgst   = sum(float(b["total_sgst"])   for b in day_bills)
            cash_sales   = sum(float(b["total_amount"]) for b in day_bills if b.get("payment_mode") == "CASH")
            upi_sales    = sum(float(b["total_amount"]) for b in day_bills if b.get("payment_mode") == "UPI")
            card_sales   = sum(float(b["total_amount"]) for b in day_bills if b.get("payment_mode") == "CARD")
            credit_sales = sum(float(b["total_amount"]) for b in day_bills if b.get("is_credit"))
            daily_summaries.append(
                DailySummaryResult(
                    summary_date=dk,
                    bill_count=bill_count,
                    total_sales=round(total_sales, 2),
                    total_cgst=round(total_cgst, 2),
                    total_sgst=round(total_sgst, 2),
                    total_tax=round(total_cgst + total_sgst, 2),
                    cash_sales=round(cash_sales, 2),
                    upi_sales=round(upi_sales, 2),
                    card_sales=round(card_sales, 2),
                    credit_sales=round(credit_sales, 2),
                    top_items=[],   # not needed for the PPTX daily table
                    is_day_closed=False,
                    message=(
                        f"📊 {dk}: {bill_count} bills, ₹{total_sales:.2f} total."
                        if bill_count else f"No bills on {dk}."
                    ),
                )
            )
            day += _td(days=1)

        period_label = f"{start_date} – {end_date}"
        return AnalyticsDeckData(
            store_name=store_name,
            period_label=period_label,
            summary=summary,
            daily_summaries=daily_summaries,
            sales_trend=sales_trend,
            top_items=top_items,
            stock_health=stock_health,
            gst_summary=gst_summary,
            khata_overview=khata_overview,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _compute_summary_live(
        self, store_id: str, target_date: str, is_day_closed: bool
    ) -> DailySummaryResult:
        """Compute summary live from billing.bills."""
        bills_resp = (
            self.db.schema("billing")
            .table("bills")
            .select("total_amount, total_cgst, total_sgst, payment_mode, is_credit")
            .eq("store_id", store_id)
            .gte("created_at", date_range_iso(target_date, target_date)[0])
            .lte("created_at", date_range_iso(target_date, target_date)[1])
            .execute()
        )
        bills = bills_resp.data or []
        bill_count = len(bills)
        total_sales = sum(float(b["total_amount"]) for b in bills)
        total_cgst = sum(float(b["total_cgst"]) for b in bills)
        total_sgst = sum(float(b["total_sgst"]) for b in bills)
        cash_sales = sum(float(b["total_amount"]) for b in bills if b["payment_mode"] == "CASH")
        upi_sales = sum(float(b["total_amount"]) for b in bills if b["payment_mode"] == "UPI")
        card_sales = sum(float(b["total_amount"]) for b in bills if b["payment_mode"] == "CARD")
        credit_sales = sum(float(b["total_amount"]) for b in bills if b["is_credit"])

        top_items = await self.get_top_items(store_id, target_date, target_date, limit=5)

        return DailySummaryResult(
            summary_date=target_date,
            bill_count=bill_count,
            total_sales=round(total_sales, 2),
            total_cgst=round(total_cgst, 2),
            total_sgst=round(total_sgst, 2),
            total_tax=round(total_cgst + total_sgst, 2),
            cash_sales=round(cash_sales, 2),
            upi_sales=round(upi_sales, 2),
            card_sales=round(card_sales, 2),
            credit_sales=round(credit_sales, 2),
            top_items=top_items,
            is_day_closed=is_day_closed,
            message=(
                f"📊 {target_date}: {bill_count} bills, ₹{total_sales:.2f} total."
                if bill_count
                else f"No bills on {target_date}."
            ),
        )


# ------------------------------------------------------------------
# Helper
# ------------------------------------------------------------------

def _row_to_summary(row: dict, is_day_closed: bool) -> DailySummaryResult:
    top_raw = row.get("top_items") or []
    top_items = [
        TopItemResult(
            product_id=t.get("product_id"),
            product_name=t.get("product_name", ""),
            brand=t.get("brand"),
            unit=t.get("unit", ""),
            quantity_sold=float(t.get("quantity_sold", 0)),
            revenue=float(t.get("revenue", 0)),
            gst_collected=float(t.get("gst_collected", 0)),
            rank_by_quantity=i + 1,
            rank_by_revenue=i + 1,
        )
        for i, t in enumerate(top_raw)
    ]
    return DailySummaryResult(
        summary_date=str(row["summary_date"]),
        bill_count=row["bill_count"],
        total_sales=float(row["total_sales"]),
        total_cgst=float(row["total_cgst"]),
        total_sgst=float(row["total_sgst"]),
        total_tax=float(row["total_tax"]),
        cash_sales=float(row["cash_sales"]),
        upi_sales=float(row["upi_sales"]),
        card_sales=float(row["card_sales"]),
        credit_sales=float(row["credit_sales"]),
        top_items=top_items,
        is_day_closed=is_day_closed,
        message=f"📊 {row['summary_date']}: {row['bill_count']} bills, ₹{float(row['total_sales']):.2f} total.",
    )
