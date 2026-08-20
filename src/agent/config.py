"""
Agent configuration models.
"""

from __future__ import annotations

from typing import Optional
from pydantic import BaseModel


class AgentConfig(BaseModel):
    llm_provider: str = "ollama"         # 'ollama' only (groq removed)
    llm_model: str = "llama-3.3-70b-versatile"
    # groq_api_key: Optional[str] = None  # unused — Groq removed
    ollama_base_url: str = "http://localhost:11434"
    max_history_messages: int = 20
    draft_bill_ttl_hours: int = 4


class StoreContext(BaseModel):
    telegram_user_id: int
    workflow_state: str               # UNREGISTERED | PENDING_CATALOGUE | PENDING_INVENTORY | ACTIVE
    shop_name: Optional[str] = None
    store_id: Optional[str] = None
    user_id: Optional[str] = None
    # Owner's name — from Telegram webhook (first_name) or asked during registration
    owner_first_name: Optional[str] = None
    owner_last_name: Optional[str] = None
    gstin: Optional[str] = None
    state_code: str = "29"
    state_name: str = "Karnataka"
    default_payment_mode: str = "CASH"
    preferences: dict = {}
    active_draft_bill_id: Optional[str] = None
