"""
Identity MCP implementation.

Owns: identity.users, identity.stores, identity.registrations,
      identity.workflow_state
"""

from __future__ import annotations

import re
from typing import Any, Optional

from src.utils.guardrails import (
    clean_gstin, clean_name, clean_optional_str,
    clean_phone, clean_state_code,
)

from src.db.supabase_client import get_client
from src.mcp.identity.models import (
    CreateStoreResult,
    RegisterUserResult,
    RegistrationStatusResult,
    StoreResult,
    UpdatePreferencesResult,
    WorkflowStateResult,
)

_GSTIN_RE = re.compile(
    r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$"
)


def _one(resp) -> Optional[dict]:
    """
    Safe helper: return resp.data[0] if present, else None.
    Works for both .limit(1).execute() and regular list responses.
    Handles supabase-py 2.x where maybe_single() can return None.
    """
    if resp is None:
        return None
    data = resp.data
    if not data:
        return None
    if isinstance(data, list):
        return data[0] if data else None
    return data  # dict (single row)


class IdentityMCP:
    """All DB operations for the identity domain."""

    def __init__(self) -> None:
        self.db = get_client()

    # ------------------------------------------------------------------
    # User registration
    # ------------------------------------------------------------------

    async def check_user_registration(
        self, telegram_user_id: int
    ) -> RegistrationStatusResult:
        """
        Check registration status.  Returns UNREGISTERED if no record found.
        Called by the Lambda handler (pre-agent context loader).
        """
        resp = (
            self.db.schema("identity")
            .table("registrations")
            .select("status, user_id, store_id")
            .eq("telegram_user_id", telegram_user_id)
            .limit(1)
            .execute()
        )
        reg = _one(resp)
        if not reg:
            # Also check workflow_state in case user row exists but no registration
            ws = await self.get_workflow_state(telegram_user_id)
            return RegistrationStatusResult(
                is_registered=False,
                registration_status="UNREGISTERED",
                user_id=ws.user_id,
                store_id=ws.store_id,
                workflow_state=ws.current_state,
            )

        is_registered = reg["status"] == "COMPLETE"
        ws = await self.get_workflow_state(telegram_user_id)
        return RegistrationStatusResult(
            is_registered=is_registered,
            registration_status=reg["status"],
            user_id=reg["user_id"],
            store_id=reg["store_id"],
            workflow_state=ws.current_state,
        )

    async def register_user(
        self,
        telegram_user_id: int,
        telegram_username: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
    ) -> RegisterUserResult:
        """
        Upsert a user record and ensure workflow_state + registration rows exist.
        Fully idempotent — safe to call on every inbound message.

        Name sourcing priority:
          1. Production (Telegram): first_name/last_name/username come automatically
             from the Telegram webhook payload — handler.py passes them here.
          2. Local dev (run_local.py): user enters their name at REPL startup.
          3. Agent call during UNREGISTERED flow: agent passes the name the owner
             typed in chat (e.g. 'My name is Pavan' → first_name='Pavan').

        Only pass values explicitly provided — pass None for anything unknown.
        """
        # ── Guardrails ──────────────────────────────────────────────────
        telegram_username = clean_optional_str(telegram_username)
        first_name = clean_optional_str(first_name)
        first_name = first_name.title() if first_name else first_name
        last_name = clean_optional_str(last_name)
        last_name  = last_name.title() if last_name else last_name

        # 1. Upsert user — only update name/username fields if they are non-None
        # (prevents overwriting a known name with None on subsequent calls)
        existing_user_resp = (
            self.db.schema("identity")
            .table("users")
            .select("id, first_name, last_name, telegram_username")
            .eq("telegram_user_id", telegram_user_id)
            .limit(1)
            .execute()
        )
        existing_user = _one(existing_user_resp)

        if existing_user:
            # Merge: only update fields that are explicitly provided (non-None)
            update_fields: dict = {}
            if telegram_username is not None:
                update_fields["telegram_username"] = telegram_username
            if first_name is not None:
                update_fields["first_name"] = first_name
            if last_name is not None:
                update_fields["last_name"] = last_name
            if update_fields:
                self.db.schema("identity").table("users").update(
                    update_fields
                ).eq("telegram_user_id", telegram_user_id).execute()
            user_id = existing_user["id"]
        else:
            # New user — insert with whatever we have
            user_resp = (
                self.db.schema("identity")
                .table("users")
                .insert(
                    {
                        "telegram_user_id": telegram_user_id,
                        "telegram_username": telegram_username,
                        "first_name": first_name,
                        "last_name": last_name,
                    }
                )
                .execute()
            )
            user_id = user_resp.data[0]["id"]

        # 2. Ensure workflow_state row — INSERT only if missing, never overwrite current_state
        existing_ws_resp = (
            self.db.schema("identity")
            .table("workflow_state")
            .select("id, user_id")
            .eq("telegram_user_id", telegram_user_id)
            .limit(1)
            .execute()
        )
        existing_ws = _one(existing_ws_resp)
        if not existing_ws:
            # New row — include user_id so self-heal doesn't need to back-fill it
            self.db.schema("identity").table("workflow_state").insert(
                {
                    "telegram_user_id": telegram_user_id,
                    "current_state": "UNREGISTERED",
                    "user_id": user_id,
                }
            ).execute()
        elif not existing_ws.get("user_id"):
            # Row exists but user_id is missing — back-fill it now
            self.db.schema("identity").table("workflow_state").update(
                {"user_id": user_id}
            ).eq("telegram_user_id", telegram_user_id).execute()

        # 3. Ensure registration row — use limit(1) instead of maybe_single()
        existing_reg_resp = (
            self.db.schema("identity")
            .table("registrations")
            .select("id")
            .eq("telegram_user_id", telegram_user_id)
            .limit(1)
            .execute()
        )
        existing_reg = _one(existing_reg_resp)
        already_existed = existing_reg is not None
        if not already_existed:
            self.db.schema("identity").table("registrations").insert(
                {
                    "telegram_user_id": telegram_user_id,
                    "user_id": user_id,
                    "status": "INITIATED",
                }
            ).execute()

        return RegisterUserResult(
            user_id=user_id,
            already_existed=already_existed,
            message=(
                "Welcome back!"
                if already_existed
                else "User record created. Let's set up your store!"
            ),
        )

    # ------------------------------------------------------------------
    # Store
    # ------------------------------------------------------------------

    async def create_store(
        self,
        telegram_user_id: int,
        shop_name: str,
        gstin: Optional[str] = None,
        address: Optional[str] = None,
        phone: Optional[str] = None,
        state_code: str = "29",
    ) -> CreateStoreResult:
        """
        Create a store for the user. Phase 1: one store per user.
        Automatically ensures the user record exists first (idempotent).
        Call this after collecting shop_name from the owner.
        GSTIN is optional — pass None if the owner did not explicitly provide one.
        Only pass a real 15-character GSTIN string, never invent or guess one.
        """
        # ── Guardrails ──────────────────────────────────────────────────
        shop_name = clean_name(shop_name, "shop_name")
        gstin = clean_gstin(gstin)           # sanitises placeholders → None, validates format
        address = clean_optional_str(address)
        phone = clean_phone(phone)
        state_code = clean_state_code(state_code)

        # 0. Auto-ensure user record exists (idempotent — safe if already registered)
        await self.register_user(telegram_user_id=telegram_user_id)

        # 1. Resolve user_id
        user_resp = (
            self.db.schema("identity")
            .table("users")
            .select("id")
            .eq("telegram_user_id", telegram_user_id)
            .limit(1)
            .execute()
        )
        user_row = _one(user_resp)
        if not user_row:
            raise ValueError("User record could not be created. Please try again.")
        user_id = user_row["id"]

        # 2. Phase-1 guard: one store per user — use limit(1) instead of maybe_single()
        existing_resp = (
            self.db.schema("identity")
            .table("stores")
            .select("id, shop_name")
            .eq("owner_user_id", user_id)
            .limit(1)
            .execute()
        )
        existing = _one(existing_resp)
        if existing:
            existing_store_id = existing["id"]

            # ── Self-heal stale workflow_state ─────────────────────────────
            # The store exists but workflow_state may still be UNREGISTERED
            # if a previous session crashed before updating it. Fix it now.
            ws_resp = (
                self.db.schema("identity")
                .table("workflow_state")
                .select("current_state, store_id, user_id")
                .eq("telegram_user_id", telegram_user_id)
                .limit(1)
                .execute()
            )
            ws = _one(ws_resp)
            ws_state = ws["current_state"] if ws else "UNREGISTERED"

            # If workflow is still stuck at UNREGISTERED (or store_id missing), repair it.
            # Advance to at least PENDING_CATALOGUE so billing tools become available.
            _STATE_ORDER = {
                "UNREGISTERED": 0, "PENDING_CATALOGUE": 1,
                "PENDING_INVENTORY": 2, "ACTIVE": 3,
            }
            if _STATE_ORDER.get(ws_state, 0) < 1 or not ws or not ws.get("store_id"):
                # Determine correct state: check catalogue and inventory
                cat_resp = (
                    self.db.schema("catalogue")
                    .table("products")
                    .select("id", count="exact")
                    .eq("store_id", existing_store_id)
                    .eq("is_active", True)
                    .execute()
                )
                product_count = cat_resp.count or 0

                inv_resp = (
                    self.db.schema("inventory")
                    .table("stock_movements")
                    .select("id", count="exact")
                    .eq("store_id", existing_store_id)
                    .eq("movement_type", "STOCK_IN")
                    .execute()
                )
                stock_in_count = inv_resp.count or 0

                if stock_in_count > 0:
                    correct_state = "ACTIVE"
                elif product_count > 0:
                    correct_state = "PENDING_INVENTORY"
                else:
                    correct_state = "PENDING_CATALOGUE"

                self.db.schema("identity").table("workflow_state").update(
                    {
                        "current_state": correct_state,
                        "store_id": existing_store_id,
                        "user_id": user_id,
                    }
                ).eq("telegram_user_id", telegram_user_id).execute()

            # ── Self-heal stale registrations ──────────────────────────────
            reg_resp = (
                self.db.schema("identity")
                .table("registrations")
                .select("status, store_id")
                .eq("telegram_user_id", telegram_user_id)
                .limit(1)
                .execute()
            )
            reg = _one(reg_resp)
            if reg and (reg["status"] != "COMPLETE" or not reg.get("store_id")):
                self.db.schema("identity").table("registrations").update(
                    {"store_id": existing_store_id, "status": "COMPLETE"}
                ).eq("telegram_user_id", telegram_user_id).execute()

            return CreateStoreResult(
                store_id=existing_store_id,
                already_existed=True,
                shop_name=existing["shop_name"],
                message=f"Your store '{existing['shop_name']}' is already set up.",
            )

        # 3. Create store
        store_resp = (
            self.db.schema("identity")
            .table("stores")
            .insert(
                {
                    "owner_user_id": user_id,
                    "shop_name": shop_name,
                    "gstin": gstin,
                    "address": address,
                    "phone": phone,
                    "state_code": state_code,
                }
            )
            .execute()
        )
        store_id = store_resp.data[0]["id"]

        # 4. Advance registration → STORE_CREATED
        self.db.schema("identity").table("registrations").update(
            {"store_id": store_id, "status": "STORE_CREATED"}
        ).eq("telegram_user_id", telegram_user_id).execute()

        # 5. Advance registration → COMPLETE
        self.db.schema("identity").table("registrations").update(
            {"status": "COMPLETE"}
        ).eq("telegram_user_id", telegram_user_id).execute()

        # 6. Advance workflow_state → PENDING_CATALOGUE
        self.db.schema("identity").table("workflow_state").update(
            {
                "current_state": "PENDING_CATALOGUE",
                "store_id": store_id,
                "user_id": user_id,
            }
        ).eq("telegram_user_id", telegram_user_id).execute()

        return CreateStoreResult(
            store_id=store_id,
            already_existed=False,
            shop_name=shop_name,
            message=(
                f"✅ Store '{shop_name}' created! "
                "Now let's add your first product to the catalogue."
            ),
        )

    async def get_store(self, telegram_user_id: int) -> Optional[StoreResult]:
        """Return store details for a user, or None if not yet created."""
        # Step 1: resolve user_id — return None if user row doesn't exist yet
        user_resp = (
            self.db.schema("identity")
            .table("users")
            .select("id")
            .eq("telegram_user_id", telegram_user_id)
            .limit(1)
            .execute()
        )
        user_row = _one(user_resp)
        if not user_row:
            return None
        user_id = user_row["id"]

        # Step 2: look up the store for this user
        resp = (
            self.db.schema("identity")
            .table("stores")
            .select("id, shop_name, gstin, address, phone, state_code, default_payment_mode, preferences")
            .eq("owner_user_id", user_id)
            .limit(1)
            .execute()
        )
        store_row = _one(resp)
        if not store_row:
            return None
        s = store_row
        return StoreResult(
            store_id=s["id"],
            shop_name=s["shop_name"],
            gstin=s.get("gstin"),
            address=s.get("address"),
            phone=s.get("phone"),
            state_code=s["state_code"],
            default_payment_mode=s["default_payment_mode"],
            preferences=s.get("preferences") or {},
        )

    async def update_store(
        self,
        store_id: str,
        shop_name: Optional[str] = None,
        phone: Optional[str] = None,
        address: Optional[str] = None,
        state_code: Optional[str] = None,
        default_payment_mode: Optional[str] = None,
    ) -> StoreResult:
        """
        Update editable fields of the store.
        Only pass fields that should change — others are left untouched.
        Allowed fields: shop_name, phone, address, state_code, default_payment_mode.
        GSTIN cannot be changed after registration (contact support).
        """
        from src.utils.guardrails import clean_name, clean_phone, clean_optional_str, clean_state_code, clean_payment_mode

        updates: dict = {}
        if shop_name is not None:
            updates["shop_name"] = clean_name(shop_name, "shop_name")
        if phone is not None:
            cleaned_phone = clean_phone(phone)
            if not cleaned_phone:
                raise ValueError("Phone number is invalid. Please provide a valid phone number.")
            updates["phone"] = cleaned_phone
        if address is not None:
            updates["address"] = clean_optional_str(address)
        if state_code is not None:
            updates["state_code"] = clean_state_code(state_code)
        if default_payment_mode is not None:
            updates["default_payment_mode"] = clean_payment_mode(default_payment_mode)

        if not updates:
            raise ValueError("No fields provided to update.")

        self.db.schema("identity").table("stores").update(updates).eq("id", store_id).execute()

        # Re-fetch and return full store result
        resp = (
            self.db.schema("identity")
            .table("stores")
            .select("id, shop_name, gstin, address, phone, state_code, default_payment_mode, preferences")
            .eq("id", store_id)
            .limit(1)
            .execute()
        )
        row = _one(resp)
        if not row:
            raise ValueError("Store not found after update.")
        return StoreResult(
            store_id=row["id"],
            shop_name=row["shop_name"],
            gstin=row.get("gstin"),
            address=row.get("address"),
            phone=row.get("phone"),
            state_code=row["state_code"],
            default_payment_mode=row["default_payment_mode"],
            preferences=row.get("preferences") or {},
        )

    async def update_user(
        self,
        telegram_user_id: int,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
    ) -> RegisterUserResult:
        """
        Update editable fields of the owner profile.
        Only first_name and last_name can be changed.
        """
        from src.utils.guardrails import clean_name, clean_optional_str

        updates: dict = {}
        if first_name is not None:
            updates["first_name"] = clean_name(first_name, "first_name")
        if last_name is not None:
            last_cleaned = clean_optional_str(last_name)
            updates["last_name"] = last_cleaned

        if not updates:
            raise ValueError("No fields provided to update.")

        self.db.schema("identity").table("users").update(updates).eq(
            "telegram_user_id", telegram_user_id
        ).execute()

        # Re-fetch user_id for the result
        resp = (
            self.db.schema("identity")
            .table("users")
            .select("id")
            .eq("telegram_user_id", telegram_user_id)
            .limit(1)
            .execute()
        )
        row = _one(resp)
        user_id = row["id"] if row else "unknown"

        name_str = updates.get("first_name", "") or ""
        return RegisterUserResult(
            user_id=user_id,
            already_existed=True,
            message=f"Profile updated: {name_str}.",
        )

    async def update_store_preferences(
        self,
        store_id: str,
        preference_key: str,
        preference_value: Any,
    ) -> UpdatePreferencesResult:
        """
        Update a preference value.  Top-level column updates (default_payment_mode)
        are applied directly; all other keys go into the JSONB preferences field.
        """
        TOP_LEVEL_COLS = {"default_payment_mode", "address", "phone"}

        if preference_key in TOP_LEVEL_COLS:
            self.db.schema("identity").table("stores").update(
                {preference_key: preference_value}
            ).eq("id", store_id).execute()
        else:
            # Read-modify-write the JSONB preferences column
            current_resp = (
                self.db.schema("identity")
                .table("stores")
                .select("preferences")
                .eq("id", store_id)
                .limit(1)
                .execute()
            )
            current_row = _one(current_resp)
            prefs: dict = (current_row.get("preferences") if current_row else None) or {}
            # Navigate to the right level and set the value
            parts = preference_key.split(".")
            target = prefs
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = preference_value
            self.db.schema("identity").table("stores").update(
                {"preferences": prefs}
            ).eq("id", store_id).execute()

        return UpdatePreferencesResult(
            store_id=store_id,
            updated_key=preference_key,
            message=f"Preference '{preference_key}' updated to '{preference_value}'.",
        )

    # ------------------------------------------------------------------
    # Workflow state
    # ------------------------------------------------------------------

    async def get_workflow_state(
        self, telegram_user_id: int
    ) -> WorkflowStateResult:
        """
        Return workflow state for a user.
        Creates an UNREGISTERED row only if one does not already exist.
        NEVER overwrites an existing state — that would reset user progress.
        """
        # 1. Try to read existing row
        resp = (
            self.db.schema("identity")
            .table("workflow_state")
            .select("current_state, store_id, user_id, active_draft_bill_id")
            .eq("telegram_user_id", telegram_user_id)
            .limit(1)
            .execute()
        )
        ws = _one(resp)
        if ws:
            return WorkflowStateResult(
                current_state=ws["current_state"],
                store_id=ws.get("store_id"),
                user_id=ws.get("user_id"),
                active_draft_bill_id=ws.get("active_draft_bill_id"),
            )

        # 2. Row missing — insert a fresh UNREGISTERED row (new user only)
        self.db.schema("identity").table("workflow_state").insert(
            {"telegram_user_id": telegram_user_id, "current_state": "UNREGISTERED"}
        ).execute()
        return WorkflowStateResult(
            current_state="UNREGISTERED",
            store_id=None,
            user_id=None,
            active_draft_bill_id=None,
        )

    async def advance_workflow_state(
        self, telegram_user_id: int, new_state: str
    ) -> bool:
        """
        Move workflow state forward.  Never downgrades.
        Also ensures store_id and user_id are filled in the workflow_state row
        if they are currently missing (common after a fresh store creation).
        Returns True if state was changed, False if already at or beyond new_state.
        """
        ORDER = {
            "UNREGISTERED": 0,
            "PENDING_CATALOGUE": 1,
            "PENDING_INVENTORY": 2,
            "ACTIVE": 3,
        }
        ws = await self.get_workflow_state(telegram_user_id)

        # Build update payload — always try to fill in store_id/user_id if missing
        update_payload: dict = {}

        if ORDER.get(ws.current_state, 0) < ORDER.get(new_state, 0):
            update_payload["current_state"] = new_state

        # Backfill store_id / user_id in the workflow row if they are missing.
        # This handles the case where create_store ran but the update didn't
        # propagate before advance_workflow_state was called.
        if not ws.store_id or not ws.user_id:
            try:
                user_resp = (
                    self.db.schema("identity")
                    .table("users")
                    .select("id")
                    .eq("telegram_user_id", telegram_user_id)
                    .limit(1)
                    .execute()
                )
                user_row = _one(user_resp)
                if user_row:
                    user_id = user_row["id"]
                    if not ws.user_id:
                        update_payload["user_id"] = user_id
                    if not ws.store_id:
                        store_resp = (
                            self.db.schema("identity")
                            .table("stores")
                            .select("id")
                            .eq("owner_user_id", user_id)
                            .limit(1)
                            .execute()
                        )
                        store_row = _one(store_resp)
                        if store_row:
                            update_payload["store_id"] = store_row["id"]
            except Exception:
                pass  # best-effort — don't crash if this lookup fails

        if not update_payload:
            return False

        self.db.schema("identity").table("workflow_state").update(
            update_payload
        ).eq("telegram_user_id", telegram_user_id).execute()

        return "current_state" in update_payload

    async def set_active_draft_bill(
        self, telegram_user_id: int, draft_bill_id: Optional[str]
    ) -> bool:
        """Set or clear active_draft_bill_id in workflow_state."""
        self.db.schema("identity").table("workflow_state").update(
            {"active_draft_bill_id": draft_bill_id}
        ).eq("telegram_user_id", telegram_user_id).execute()
        return True
