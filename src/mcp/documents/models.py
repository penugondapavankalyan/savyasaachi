"""
Documents MCP — Pydantic models.
"""

from __future__ import annotations

from pydantic import BaseModel


class InvoicePDFResult(BaseModel):
    file_path: str
    bill_number: str
    file_size_bytes: int
    message: str


class AnalysisPPTXResult(BaseModel):
    file_path: str
    period_label: str
    file_size_bytes: int
    message: str
