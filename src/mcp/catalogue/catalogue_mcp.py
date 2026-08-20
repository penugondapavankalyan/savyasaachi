"""
Catalogue MCP implementation.

Owns: catalogue.products
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from src.utils.guardrails import (
    clean_brand, clean_gst_rate, clean_hsn_code, clean_name,
    clean_non_negative_float, clean_optional_str, clean_positive_float, clean_unit,
    clean_quantity_for_unit,
)


def _one(resp):
    """Safe helper: return first row or None. Handles supabase-py 2.x quirks."""
    if resp is None:
        return None
    data = resp.data
    if not data:
        return None
    return data[0] if isinstance(data, list) else data

from src.db.supabase_client import get_client
from src.mcp.catalogue.models import (
    AddProductResult,
    DeactivateResult,
    ProductResult,
)

if TYPE_CHECKING:
    from src.mcp.identity.identity_mcp import IdentityMCP

# _VALID_UNITS moved to src/utils/guardrails.py


class CatalogueMCP:
    """All DB operations for the product catalogue domain."""

    def __init__(self, identity_mcp: "IdentityMCP | None" = None) -> None:
        self.db = get_client()
        self._identity = identity_mcp

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _sync_reorder_to_inventory(
        self,
        product_id: str,
        store_id: str,
        reorder_level: float,
    ) -> bool:
        """
        Sync the catalogue reorder_level to the inventory row (if it exists).

        Catalogue is the single source of truth for reorder_level.
        Whenever it changes (add_product / update_product / update_product_details),
        we immediately propagate the value to inventory.inventory so that the
        decrement_stock RPC always compares against the correct threshold.

        Returns True if an inventory row was updated, False if no row existed yet
        (first stock-in hasn't happened — nothing to sync).
        """
        resp = (
            self.db.schema("inventory")
            .table("inventory")
            .select("id")
            .eq("store_id", store_id)
            .eq("product_id", product_id)
            .limit(1)
            .execute()
        )
        inv_row = resp.data[0] if (resp.data) else None
        if not inv_row:
            # No inventory row yet — sync will happen when receive_stock is called
            # (it reads reorder_level fresh from catalogue via the increment_stock RPC)
            return False

        self.db.schema("inventory").table("inventory").update(
            {"reorder_level": reorder_level}
        ).eq("id", inv_row["id"]).execute()
        return True

    # ------------------------------------------------------------------
    # Add / Update
    # ------------------------------------------------------------------

    async def add_product(
        self,
        store_id: str,
        name: str,
        is_loose: bool,
        unit: str,
        cost_price: float,
        mrp: float,
        reorder_level: float,
        gst_rate: float,
        brand: Optional[str] = None,
        hsn_code: Optional[str] = None,
        telegram_user_id: Optional[int] = None,
    ) -> AddProductResult:
        """
        Upsert a product.
        - Loose items: gst_rate is forced to 0 regardless of input.
        - Branded items: gst_rate MUST be 5 / 12 / 18 / 28 — passing 0 raises ValueError.
        Advances workflow state to PENDING_INVENTORY on first product add.
        Only pass values the owner explicitly stated — never invent brand, HSN, or GST.
        """
        # ── Guardrails ──────────────────────────────────────────────────
        name = clean_name(name, "product name")
        unit = clean_unit(unit)
        cost_price = clean_positive_float(cost_price, "cost_price")
        mrp = clean_positive_float(mrp, "mrp")
        reorder_level = clean_non_negative_float(reorder_level, "reorder_level")
        brand = clean_brand(brand)
        hsn_code = clean_hsn_code(hsn_code)
        gst_rate = clean_gst_rate(gst_rate, is_loose)

        # Reorder level must also obey unit-based integer rules
        # (e.g. reorder at 0.5 PACKET makes no sense)
        if reorder_level > 0:
            reorder_level = clean_quantity_for_unit(reorder_level, unit, "reorder_level")

        if mrp < cost_price:
            # Allow but warn (agent handles the warning in the response)
            pass

        # ── Check for existing product with same store_id + name + brand ──────
        # We cannot use upsert ON CONFLICT (store_id,name,brand) when brand is NULL
        # because PostgreSQL NULL != NULL in unique constraints — the conflict is
        # never triggered for NULL brand rows, causing a duplicate insert or a
        # "no unique constraint matching ON CONFLICT" error.
        # Solution: explicit SELECT first, then INSERT or UPDATE.

        existing_q = (
            self.db.schema("catalogue")
            .table("products")
            .select("id")
            .eq("store_id", store_id)
            .eq("name", name)
        )
        if brand is not None:
            existing_q = existing_q.eq("brand", brand)
        else:
            existing_q = existing_q.is_("brand", "null")
        existing_resp = existing_q.limit(1).execute()
        existing_row = _one(existing_resp)

        product_data = {
            "store_id": store_id,
            "name": name,
            "brand": brand,
            "is_loose": is_loose,
            "unit": unit,
            "hsn_code": hsn_code,
            "gst_rate": gst_rate,
            "cost_price": cost_price,
            "mrp": mrp,
            "reorder_level": reorder_level,
            "is_active": True,
        }

        if existing_row:
            # Product already exists — UPDATE it
            resp = (
                self.db.schema("catalogue")
                .table("products")
                .update(product_data)
                .eq("id", existing_row["id"])
                .execute()
            )
        else:
            # New product — INSERT it
            resp = (
                self.db.schema("catalogue")
                .table("products")
                .insert(product_data)
                .execute()
            )

        product = resp.data[0]
        product_id = product["id"]

        # Sync reorder_level to inventory (no-op if inventory row doesn't exist yet)
        await self._sync_reorder_to_inventory(product_id, store_id, reorder_level)

        # Check if this is the first product for the store
        count_resp = (
            self.db.schema("catalogue")
            .table("products")
            .select("id", count="exact")
            .eq("store_id", store_id)
            .eq("is_active", True)
            .execute()
        )
        product_count = count_resp.count or 0
        workflow_advanced = False

        if product_count == 1 and telegram_user_id and self._identity:
            advanced = await self._identity.advance_workflow_state(
                telegram_user_id, "PENDING_INVENTORY"
            )
            workflow_advanced = advanced

        price_warning = ""
        if mrp < cost_price:
            price_warning = f" ⚠️ Note: selling price ₹{mrp} is below cost ₹{cost_price}."

        return AddProductResult(
            product_id=product_id,
            name=name,
            brand=brand,
            is_loose=is_loose,
            gst_rate=gst_rate,
            mrp=mrp,
            already_existed=False,   # upsert — always returns fresh row data
            workflow_advanced=workflow_advanced,
            message=(
                f"✅ Added: {name}"
                + (f" ({brand})" if brand else "")
                + f" — {'0%' if is_loose else str(gst_rate) + '%'} GST, MRP ₹{mrp}/{unit.lower()}."
                + price_warning
            ),
        )

    async def update_product(
        self,
        store_id: str,
        product_id: str,
        cost_price: Optional[float] = None,
        mrp: Optional[float] = None,
        reorder_level: Optional[float] = None,
        hsn_code: Optional[str] = None,
    ) -> ProductResult:
        """Update mutable fields of an existing product (price/reorder/hsn only)."""
        updates: dict = {}
        if cost_price is not None:
            updates["cost_price"] = cost_price
        if mrp is not None:
            updates["mrp"] = mrp
        if reorder_level is not None:
            updates["reorder_level"] = reorder_level
        if hsn_code is not None:
            updates["hsn_code"] = hsn_code

        if not updates:
            raise ValueError("No fields provided to update.")

        self.db.schema("catalogue").table("products").update(updates).eq(
            "id", product_id
        ).eq("store_id", store_id).execute()

        # Sync reorder_level to inventory if it was updated
        if reorder_level is not None:
            await self._sync_reorder_to_inventory(product_id, store_id, updates["reorder_level"])

        return await self.get_product(store_id, product_id)

    async def update_product_details(
        self,
        store_id: str,
        product_id: str,
        name: Optional[str] = None,
        brand: Optional[str] = None,
        unit: Optional[str] = None,
        is_loose: Optional[bool] = None,
        cost_price: Optional[float] = None,
        mrp: Optional[float] = None,
        reorder_level: Optional[float] = None,
        gst_rate: Optional[float] = None,
        hsn_code: Optional[str] = None,
    ) -> ProductResult:
        """
        Update any field of an existing catalogue product.
        Only pass fields that should change — others are left untouched.
        All catalogue fields are editable: name, brand, unit, is_loose,
        cost_price, mrp, reorder_level, gst_rate, hsn_code.
        """
        # First fetch current product to get unit (needed for reorder_level validation)
        current = await self.get_product(store_id, product_id)

        updates: dict = {}
        if name is not None:
            updates["name"] = clean_name(name, "name")
        if brand is not None:
            updates["brand"] = clean_brand(brand)
        if unit is not None:
            updates["unit"] = clean_unit(unit)
        if is_loose is not None:
            updates["is_loose"] = is_loose
        if cost_price is not None:
            updates["cost_price"] = clean_positive_float(cost_price, "cost_price")
        if mrp is not None:
            updates["mrp"] = clean_positive_float(mrp, "mrp")
        if reorder_level is not None:
            effective_unit = updates.get("unit", current.unit)
            rl = clean_non_negative_float(reorder_level, "reorder_level")
            if rl > 0:
                rl = clean_quantity_for_unit(rl, effective_unit, "reorder_level")
            updates["reorder_level"] = rl
        if gst_rate is not None:
            effective_loose = updates.get("is_loose", current.is_loose)
            updates["gst_rate"] = clean_gst_rate(gst_rate, effective_loose)
        if hsn_code is not None:
            updates["hsn_code"] = clean_hsn_code(hsn_code)

        if not updates:
            raise ValueError("No fields provided to update.")

        self.db.schema("catalogue").table("products").update(updates).eq(
            "id", product_id
        ).eq("store_id", store_id).execute()

        # Sync reorder_level to inventory if it was changed
        if "reorder_level" in updates:
            await self._sync_reorder_to_inventory(product_id, store_id, updates["reorder_level"])

        return await self.get_product(store_id, product_id)

    async def deactivate_product(
        self, store_id: str, product_id: str
    ) -> DeactivateResult:
        """Soft-delete a product (is_active = FALSE)."""
        resp = (
            self.db.schema("catalogue")
            .table("products")
            .select("name")
            .eq("id", product_id)
            .eq("store_id", store_id)
            .limit(1)
            .execute()
        )
        row = _one(resp)
        if not row:
            raise ValueError(f"Product {product_id} not found.")
        name = row["name"]
        self.db.schema("catalogue").table("products").update(
            {"is_active": False}
        ).eq("id", product_id).eq("store_id", store_id).execute()
        return DeactivateResult(
            product_id=product_id,
            product_name=name,
            success=True,
            message=f"'{name}' has been removed from your catalogue.",
        )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def get_product(self, store_id: str, product_id: str) -> ProductResult:
        """Retrieve a single product by ID."""
        resp = (
            self.db.schema("catalogue")
            .table("products")
            .select("*")
            .eq("id", product_id)
            .eq("store_id", store_id)
            .limit(1)
            .execute()
        )
        row = _one(resp)
        if not row:
            raise ValueError(f"Product {product_id} not found.")
        return _row_to_product(row)

    async def search_products(
        self,
        store_id: str,
        query: str,
        active_only: bool = True,
    ) -> list[ProductResult]:
        """
        Substring search on name and brand.
        Also tries stemmed variants of the query so that plural/suffix forms
        (e.g. 'pencils', 'sugars', 'biscuits') match the stored singular name.
        For multi-word queries (e.g. 'apsara pencil'), also tries a cross-field
        pass: each word is matched against name while the remaining words are
        checked against brand in-memory, so 'apsara pencil' finds a product
        named 'Pencil' with brand 'Apsara'.
        Returns up to 10 results ordered by name.
        """
        # Build a set of search terms: original query + stemmed variants.
        # Strip common English plural/suffix endings so "pencils" → "pencil",
        # "sugars" → "sugar", "biscuits" → "biscuit", etc.
        terms: list[str] = [query.strip()]
        q_lower = query.strip().lower()
        for suffix in ("ies", "es", "s"):
            if q_lower.endswith(suffix) and len(q_lower) > len(suffix) + 2:
                stem = q_lower[: -len(suffix)]
                if stem not in [t.lower() for t in terms]:
                    terms.append(stem)
                break  # only strip one suffix

        seen_ids: set[str] = set()
        results: list[dict] = []

        def _base_q() -> object:
            """Return a fresh base query (never reuse — builder mutates in-place)."""
            q = (
                self.db.schema("catalogue")
                .table("products")
                .select("*")
                .eq("store_id", store_id)
            )
            if active_only:
                q = q.eq("is_active", True)
            return q

        for term in terms:
            # Build a FRESH query each iteration — the supabase-py query builder
            # mutates its internal params object in-place via .add().  Reusing the
            # same base_q across iterations would accumulate ilike filters as AND
            # conditions (e.g. name=ilike.%pencils% AND name=ilike.%pencil%) which
            # can never both be satisfied simultaneously, always returning 0 rows.
            search_term = f"%{term}%"
            resp = _base_q().ilike("name", search_term).limit(10).execute()
            for row in (resp.data or []):
                if row["id"] not in seen_ids:
                    seen_ids.add(row["id"])
                    results.append(row)
            if results:
                break  # name hit found — no need to try more stems

        # Fall back to brand search if still no hits
        if not results:
            for term in terms:
                search_term = f"%{term}%"
                resp2 = (
                    _base_q()
                    .ilike("brand", search_term)
                    .limit(10)
                    .execute()
                )
                for row in (resp2.data or []):
                    if row["id"] not in seen_ids:
                        seen_ids.add(row["id"])
                        results.append(row)
                if results:
                    break

        # Cross-field pass for multi-word queries (e.g. "apsara pencil"):
        # Try each individual word as a name match, then verify in-memory that
        # at least one of the remaining words appears in the brand field.
        # This catches products like name="Pencil", brand="Apsara" which the
        # whole-phrase passes above would miss entirely.
        if not results:
            words = q_lower.split()
            if len(words) > 1:
                for name_word in words:
                    other_words = [w for w in words if w != name_word]
                    resp3 = (
                        _base_q()
                        .ilike("name", f"%{name_word}%")
                        .limit(50)
                        .execute()
                    )
                    for row in (resp3.data or []):
                        if row["id"] in seen_ids:
                            continue
                        brand_val = (row.get("brand") or "").lower()
                        if any(w in brand_val for w in other_words):
                            seen_ids.add(row["id"])
                            results.append(row)
                    if results:
                        break

        return [_row_to_product(r) for r in results]

    async def list_products(self, store_id: str) -> list[ProductResult]:
        """List all active products for a store."""
        resp = (
            self.db.schema("catalogue")
            .table("products")
            .select("*")
            .eq("store_id", store_id)
            .eq("is_active", True)
            .order("name")
            .execute()
        )
        return [_row_to_product(r) for r in (resp.data or [])]


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _row_to_product(row: dict) -> ProductResult:
    return ProductResult(
        product_id=row["id"],
        name=row["name"],
        brand=row.get("brand"),
        is_loose=row["is_loose"],
        unit=row["unit"],
        hsn_code=row.get("hsn_code"),
        gst_rate=float(row["gst_rate"]),
        cost_price=float(row["cost_price"]),
        mrp=float(row["mrp"]),
        reorder_level=float(row["reorder_level"]),
        is_active=row["is_active"],
    )
