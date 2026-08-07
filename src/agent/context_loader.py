"""
Pre-agent context loader.

Runs on every Lambda invocation before the agent is invoked.
Builds the StoreContext that drives the system prompt and tool list.

Self-healing: if workflow_state is out of sync with actual DB data
(e.g. UNREGISTERED but store/products/stock already exist), it repairs
the state automatically before returning context. This handles users
whose workflow state got stuck due to crashes or prior bugs.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from src.agent.config import StoreContext
from src.agent.system_prompt import STATE_CODE_TO_NAME
from src.mcp import get_mcp_instances

logger = logging.getLogger(__name__)

# State rank — higher = further along in registration flow
_STATE_ORDER: dict[str, int] = {
    "UNREGISTERED": 0,
    "PENDING_CATALOGUE": 1,
    "PENDING_INVENTORY": 2,
    "ACTIVE": 3,
}


async def load_agent_context(telegram_user_id: int) -> StoreContext:
    """
    1. Read current workflow state.
    2. Self-heal stale state if actual DB data is ahead of recorded state.
    3. Handle expired draft bills.
    4. Load store details.
    5. Load owner name.
    6. Return StoreContext for system prompt + tool selection.
    """
    mcps = get_mcp_instances()
    db = mcps.identity.db

    # ── Step 1: read workflow state ───────────────────────────────────────
    workflow = await mcps.identity.get_workflow_state(telegram_user_id)

    # ── Step 2: self-heal stale workflow_state ────────────────────────────
    # This handles users stuck at UNREGISTERED even though their store/products
    # already exist (caused by crashes, prior bugs, or partial flows).
    workflow = await _self_heal_workflow(telegram_user_id, workflow, db)

    # ── Step 3: handle expired draft bill ────────────────────────────────
    if workflow.active_draft_bill_id:
        try:
            draft_resp = (
                db.schema("billing")
                .table("draft_bills")
                .select("id, status, expires_at")
                .eq("id", workflow.active_draft_bill_id)
                .limit(1)
                .execute()
            )
            draft_rows = draft_resp.data if draft_resp else []
            draft = draft_rows[0] if draft_rows else None
            if draft and draft["status"] == "OPEN":
                expires_at = datetime.fromisoformat(
                    draft["expires_at"].replace("Z", "+00:00")
                )
                if expires_at < datetime.now(timezone.utc):
                    db.schema("billing").table("draft_bills").update(
                        {"status": "EXPIRED"}
                    ).eq("id", workflow.active_draft_bill_id).execute()
                    await mcps.identity.set_active_draft_bill(telegram_user_id, None)
                    workflow.active_draft_bill_id = None
        except Exception as e:
            logger.warning("Draft bill expiry check failed: %s", e)

    # ── Step 4: load store details ────────────────────────────────────────
    store = None
    if workflow.store_id:
        try:
            store = await mcps.identity.get_store(telegram_user_id)
        except Exception as e:
            logger.warning("get_store failed: %s", e)

    state_code = store.state_code if store else "29"
    state_name = STATE_CODE_TO_NAME.get(state_code, state_code)

    # ── Step 5: load owner name ───────────────────────────────────────────
    owner_first_name: str | None = None
    owner_last_name: str | None = None
    try:
        user_resp = (
            db.schema("identity")
            .table("users")
            .select("first_name, last_name")
            .eq("telegram_user_id", telegram_user_id)
            .limit(1)
            .execute()
        )
        if user_resp and user_resp.data:
            u = user_resp.data[0]
            owner_first_name = u.get("first_name") or None
            owner_last_name = u.get("last_name") or None
    except Exception as e:
        logger.warning("Owner name lookup failed: %s", e)

    return StoreContext(
        telegram_user_id=telegram_user_id,
        workflow_state=workflow.current_state,
        shop_name=store.shop_name if store else None,
        store_id=workflow.store_id,
        user_id=workflow.user_id,
        owner_first_name=owner_first_name,
        owner_last_name=owner_last_name,
        gstin=store.gstin if store else None,
        state_code=state_code,
        state_name=state_name,
        default_payment_mode=store.default_payment_mode if store else "CASH",
        preferences=store.preferences if store else {},
        active_draft_bill_id=workflow.active_draft_bill_id,
    )


async def _self_heal_workflow(telegram_user_id: int, workflow, db):
    """
    Detect and repair stale workflow_state.

    Checks actual DB data against the recorded workflow state and advances
    the state to match reality. This is idempotent and cheap (a few extra
    queries per call, only when state is stale).

    Cases handled:
      - workflow_state row has no user_id — fill it from users table
      - workflow_state=UNREGISTERED but store exists → advance to correct state
      - workflow_state row has store_id=NULL but store exists → fill it in
      - PENDING_CATALOGUE but products already exist → advance to PENDING_INVENTORY
      - PENDING_INVENTORY but stock already exists → advance to ACTIVE
    """
    current_rank = _STATE_ORDER.get(workflow.current_state, 0)

    # ── Case 1: No store_id recorded but user has a store ─────────────────
    # This is the most common stale case after the prior workflow_state bug.
    # Also fires when user_id is missing from the workflow row (e.g. register_user
    # created the workflow row before user_id was known).
    if not workflow.store_id or not workflow.user_id or current_rank < 1:
        try:
            # Resolve user_id from users table
            user_resp = (
                db.schema("identity")
                .table("users")
                .select("id")
                .eq("telegram_user_id", telegram_user_id)
                .limit(1)
                .execute()
            )
            user_row = user_resp.data[0] if user_resp.data else None
            if not user_row:
                return workflow   # genuinely new user — nothing to heal

            user_id = user_row["id"]

            # Backfill user_id in workflow row if it was missing (even if no store yet)
            if not workflow.user_id:
                try:
                    db.schema("identity").table("workflow_state").update(
                        {"user_id": user_id}
                    ).eq("telegram_user_id", telegram_user_id).execute()
                    workflow.user_id = user_id
                except Exception:
                    pass

            # Look for a store owned by this user
            store_resp = (
                db.schema("identity")
                .table("stores")
                .select("id")
                .eq("owner_user_id", user_id)
                .limit(1)
                .execute()
            )
            store_row = store_resp.data[0] if store_resp.data else None
            if not store_row:
                return workflow   # genuinely no store yet — nothing more to heal

            store_id = store_row["id"]
            logger.warning(
                "self-heal: user %s has store %s but workflow_state=%s store_id=%s — repairing",
                telegram_user_id, store_id, workflow.current_state, workflow.store_id,
            )

            # Determine correct state from actual data
            correct_state = await _detect_correct_state(store_id, db)

            # Update workflow_state table
            db.schema("identity").table("workflow_state").update(
                {
                    "current_state": correct_state,
                    "store_id": store_id,
                    "user_id": user_id,
                }
            ).eq("telegram_user_id", telegram_user_id).execute()

            # Also repair registrations if stale
            reg_resp = (
                db.schema("identity")
                .table("registrations")
                .select("status, store_id")
                .eq("telegram_user_id", telegram_user_id)
                .limit(1)
                .execute()
            )
            reg = reg_resp.data[0] if reg_resp.data else None
            if reg and (reg["status"] != "COMPLETE" or not reg.get("store_id")):
                db.schema("identity").table("registrations").update(
                    {"store_id": store_id, "status": "COMPLETE"}
                ).eq("telegram_user_id", telegram_user_id).execute()

            # Return updated workflow state
            from src.mcp.identity.models import WorkflowStateResult
            return WorkflowStateResult(
                current_state=correct_state,
                store_id=store_id,
                user_id=user_id,
                active_draft_bill_id=workflow.active_draft_bill_id,
            )

        except Exception as e:
            logger.error("self-heal failed for user %s: %s", telegram_user_id, e)
            return workflow   # return original — don't crash the whole request

    # ── Case 2: store_id present but state may lag behind actual data ──────
    if current_rank >= 1 and workflow.store_id:
        try:
            correct_state = await _detect_correct_state(workflow.store_id, db)
            correct_rank = _STATE_ORDER.get(correct_state, 0)
            if correct_rank > current_rank:
                logger.warning(
                    "self-heal: user %s workflow_state=%s but data says %s — advancing",
                    telegram_user_id, workflow.current_state, correct_state,
                )
                db.schema("identity").table("workflow_state").update(
                    {"current_state": correct_state}
                ).eq("telegram_user_id", telegram_user_id).execute()
                workflow.current_state = correct_state
        except Exception as e:
            logger.warning("self-heal (advance) failed: %s", e)

    return workflow


async def _detect_correct_state(store_id: str, db) -> str:
    """
    Look at catalogue and inventory to determine what the workflow state
    should be for a store that definitely exists.
    """
    # Check for genuine stock receipts (STOCK_IN from a real receive_stock call).
    # Exclude reversals from bill cancellations/voids (reference_type BILL_CANCEL / BILL_VOID).
    inv_resp = (
        db.schema("inventory")
        .table("stock_movements")
        .select("id", count="exact")
        .eq("store_id", store_id)
        .eq("movement_type", "STOCK_IN")
        .in_("reference_type", ["STOCK_IN"])   # only real receive_stock entries
        .execute()
    )
    if (inv_resp.count or 0) > 0:
        return "ACTIVE"

    # Check for catalogue products
    cat_resp = (
        db.schema("catalogue")
        .table("products")
        .select("id", count="exact")
        .eq("store_id", store_id)
        .eq("is_active", True)
        .execute()
    )
    if (cat_resp.count or 0) > 0:
        return "PENDING_INVENTORY"

    return "PENDING_CATALOGUE"
