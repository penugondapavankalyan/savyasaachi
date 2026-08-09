# MCP Module: Catalogue MCP

**Domain:** Catalogue  
**Module Path:** `src/mcp/catalogue/catalogue_mcp.py`  
**Owned Tables:** `products`

---

## Responsibility

The Catalogue MCP owns all operations on the product catalogue. It is the authoritative source for product information: what is sold, at what price, with what GST rate. No product can be inventoried or billed without first being registered in the catalogue via this MCP.

This module is active in `PENDING_CATALOGUE`, `PENDING_INVENTORY`, and `ACTIVE` workflow states.

---

## Tools (PydanticAI Tool Functions)

### 1. `add_product`

**Description:** Adds a new product to the store's catalogue. Triggers workflow state advancement if this is the first product.

**Signature:**
```python
async def add_product(
    store_id: str,
    name: str,
    brand: Optional[str],
    is_loose: bool,
    unit: str,  # 'KG' | 'G' | 'L' | 'ML' | 'PACKET' | 'PIECE' | 'DOZEN' | 'BUNDLE'
    hsn_code: Optional[str],
    gst_rate: float,  # 0.0 for loose, else 5.0 / 12.0 / 18.0 etc.
    cost_price: float,
    mrp: float,
    reorder_level: float
) -> AddProductResult
```

**Output (`AddProductResult`):**
```python
class AddProductResult(BaseModel):
    product_id: str
    name: str
    brand: Optional[str]
    is_loose: bool
    gst_rate: float
    mrp: float
    already_existed: bool
    workflow_advanced: bool  # True if this was the first product and state moved to PENDING_INVENTORY
    message: str
```

**DB Operations:**
```sql
-- Idempotent insert (same name+brand = update prices, not duplicate)
INSERT INTO products (store_id, name, brand, is_loose, unit, hsn_code, gst_rate, cost_price, mrp, reorder_level)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (store_id, LOWER(name), COALESCE(LOWER(brand), ''))
DO UPDATE SET
    cost_price = EXCLUDED.cost_price,
    mrp = EXCLUDED.mrp,
    reorder_level = EXCLUDED.reorder_level,
    updated_at = NOW()
RETURNING id, (xmax = 0) AS inserted;  -- xmax=0 means it was a fresh INSERT
```

**Post-Insert:**
```python
# Count products for this store
product_count = count_active_products(store_id)
if product_count == 1:
    # First product added → advance workflow state
    identity_mcp.advance_workflow_state(telegram_user_id, 'PENDING_INVENTORY')
    workflow_advanced = True
```

**Business Rules:**
- `is_loose = True` forces `gst_rate = 0.0` (DB trigger enforces, MCP also validates)
- `mrp` must be ≥ `cost_price` (don't-sell-below-cost guardrail — agent warns if violated)
- `hsn_code` required for branded items (agent prompts; owner can skip for Phase 1)
- Adding same product again updates prices — does not create duplicate SKU

---

### 2. `get_product`

**Description:** Retrieves a single product by ID.

**Signature:**
```python
async def get_product(store_id: str, product_id: str) -> ProductResult
```

**Output (`ProductResult`):**
```python
class ProductResult(BaseModel):
    product_id: str
    name: str
    brand: Optional[str]
    is_loose: bool
    unit: str
    hsn_code: Optional[str]
    gst_rate: float
    cost_price: float
    mrp: float
    reorder_level: float
    is_active: bool
```

---

### 3. `search_products`

**Description:** Full-text search for products by name or brand. Used by agent to resolve natural language product references like "atta", "maggi", "sugar".

**Signature:**
```python
async def search_products(
    store_id: str,
    query: str,
    active_only: bool = True
) -> List[ProductResult]
```

**DB Operations:**
```sql
SELECT *, ts_rank(to_tsvector('english', name || ' ' || COALESCE(brand, '')),
    plainto_tsquery('english', ?)) AS rank
FROM products
WHERE store_id = ?
  AND is_active = TRUE
  AND to_tsvector('english', name || ' ' || COALESCE(brand, ''))
      @@ plainto_tsquery('english', ?)
ORDER BY rank DESC
LIMIT 10;
```

**Disambiguation Logic:**
- If query returns 1 result → return directly
- If query returns 2+ results → return all, agent asks owner to clarify
  - Example: "atta" → returns Aashirvaad Atta + Pillsbury Atta → agent: "Which atta — Aashirvaad 5kg or Pillsbury Atta 1kg?"
- If `preferred_brands` preference set → auto-select the preferred one without asking

---

### 4. `list_products`

**Description:** Lists all active products in a store's catalogue.

**Signature:**
```python
async def list_products(store_id: str) -> List[ProductResult]
```

**DB Operations:**
```sql
SELECT * FROM products
WHERE store_id = ? AND is_active = TRUE
ORDER BY name, brand;
```

---

### 5. `update_product`

**Description:** Updates pricing, reorder level, or other fields of an existing product.

**Signature:**
```python
async def update_product(
    store_id: str,
    product_id: str,
    cost_price: Optional[float] = None,
    mrp: Optional[float] = None,
    reorder_level: Optional[float] = None,
    hsn_code: Optional[str] = None
) -> ProductResult
```

**Business Rules:**
- Cannot change `is_loose`, `name`, or `brand` — these define the SKU identity
- If `mrp < cost_price` after update → agent warns but allows (owner may have promotional pricing)
- Change is reflected immediately in future bills (draft bills already in progress use snapshotted prices)

---

### 6. `deactivate_product`

**Description:** Soft-deletes a product by setting `is_active = FALSE`. The product disappears from catalogue and cannot be billed.

**Signature:**
```python
async def deactivate_product(store_id: str, product_id: str) -> DeactivateResult
```

**Business Rules:**
- Does not delete the product record — historical bill_items that reference it are preserved
- Agent confirms before deactivating: "Are you sure you want to remove Maggi 70g from your catalogue?"
- If product has open draft_bill_items referencing it → agent warns owner

---

## Agent Conversation Flow for Adding a Product

```
Owner: "add new item: Amul Butter 100g"

Agent calls: search_products(store_id, "Amul Butter 100g")
→ No results found

Agent: "Is Amul Butter 100g a loose item or a branded/packaged product?"
Owner: "branded"

Agent: "What's the GST rate for Amul Butter 100g?
        Common rates: 0% (exempt), 5% (staples), 12% (dairy/FMCG), 18% (FMCG)"
Owner: "12%"

Agent: "What's the HSN code? (Type 'skip' if you don't know)"
Owner: "0405"

Agent: "What unit? (kg / g / L / mL / packet / piece / dozen)"
Owner: "piece"

Agent: "What's your cost price per piece (what you paid)?"
Owner: "₹52"

Agent: "What's the MRP / selling price per piece?"
Owner: "₹62"

Agent: "Set a reorder level — alert me when stock falls below how many pieces?"
Owner: "10"

Agent calls: add_product(store_id, name="Amul Butter 100g", brand="Amul",
                         is_loose=False, unit="PIECE", hsn_code="0405",
                         gst_rate=12.0, cost_price=52.0, mrp=62.0, reorder_level=10)

Agent: "✅ Added: Amul Butter 100g (Amul) — 12% GST (CGST 6% + SGST 6%),
        MRP ₹62/piece, cost ₹52. Reorder alert at 10 pieces."
```

---

## Error Handling

| Error | Response |
|---|---|
| Duplicate product (same name+brand) | Returns existing with updated prices, `already_existed=True` |
| `gst_rate > 0` for loose item | Forces `gst_rate=0`, agent informs owner |
| `mrp < cost_price` | Warning but allowed — agent: "Note: selling price ₹X is below cost ₹Y" |
| Invalid unit | Agent re-prompts with valid unit list |
| Product not found | Agent: "I couldn't find that product. Did you mean [nearest match]?" |

---

## Phase 2 Extensibility

| Feature | Change |
|---|---|
| Barcode support | Add `barcode TEXT` column, add `get_product_by_barcode()` tool |
| Supplier assignment | Add `assign_supplier()` tool |
| Bulk import | Add `bulk_import_products()` tool accepting CSV data |
