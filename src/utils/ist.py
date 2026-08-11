"""
ist.py — IST (Asia/Kolkata, UTC+05:30) timezone helpers.

All kirana stores operate in India. Dates and day boundaries must always
be computed in IST so that e.g. a bill created at 23:30 UTC (= 05:00 IST
next day) is bucketed into the correct IST calendar day.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

# IST = UTC + 5h30m (India Standard Time, no DST)
IST = timezone(timedelta(hours=5, minutes=30))


def today_ist() -> date:
    """Return the current calendar date in IST."""
    return datetime.now(IST).date()


def now_ist() -> datetime:
    """Return the current datetime in IST (timezone-aware)."""
    return datetime.now(IST)


def day_start_iso(d: date) -> str:
    """Return ISO-8601 timestamp for the start of the given IST date (00:00:00+05:30)."""
    return datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=IST).isoformat()


def day_end_iso(d: date) -> str:
    """Return ISO-8601 timestamp for the end of the given IST date (23:59:59+05:30)."""
    return datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=IST).isoformat()


def date_range_iso(start: str, end: str) -> tuple[str, str]:
    """
    Convert YYYY-MM-DD start/end strings to IST-bounded ISO timestamps
    suitable for Supabase gte/lte filters on timestamptz columns.

    Returns (start_iso, end_iso) where:
      start_iso = YYYY-MM-DDT00:00:00+05:30
      end_iso   = YYYY-MM-DDT23:59:59+05:30
    """
    sd = date.fromisoformat(start)
    ed = date.fromisoformat(end)
    return day_start_iso(sd), day_end_iso(ed)
