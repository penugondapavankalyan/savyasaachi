"""
Identity MCP — Pydantic models.

Covers: users, stores, registrations, workflow_state.
"""

from __future__ import annotations

from typing import Optional
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# register_user
# ---------------------------------------------------------------------------

class RegisterUserResult(BaseModel):
    user_id: str
    already_existed: bool
    message: str


# ---------------------------------------------------------------------------
# create_store
# ---------------------------------------------------------------------------

class CreateStoreResult(BaseModel):
    store_id: str
    already_existed: bool
    shop_name: str
    message: str


# ---------------------------------------------------------------------------
# get_store
# ---------------------------------------------------------------------------

class StoreResult(BaseModel):
    store_id: str
    shop_name: str
    gstin: Optional[str]
    address: Optional[str]
    phone: Optional[str]
    state_code: str
    default_payment_mode: str
    preferences: dict


# ---------------------------------------------------------------------------
# check_user_registration
# ---------------------------------------------------------------------------

class RegistrationStatusResult(BaseModel):
    is_registered: bool
    registration_status: str          # INITIATED | STORE_CREATED | COMPLETE | UNREGISTERED
    user_id: Optional[str]
    store_id: Optional[str]
    workflow_state: str               # UNREGISTERED | PENDING_CATALOGUE | PENDING_INVENTORY | ACTIVE


# ---------------------------------------------------------------------------
# get_workflow_state
# ---------------------------------------------------------------------------

class WorkflowStateResult(BaseModel):
    current_state: str
    store_id: Optional[str]
    user_id: Optional[str]
    active_draft_bill_id: Optional[str]


# ---------------------------------------------------------------------------
# update_store_preferences
# ---------------------------------------------------------------------------

class UpdatePreferencesResult(BaseModel):
    store_id: str
    updated_key: str
    message: str
