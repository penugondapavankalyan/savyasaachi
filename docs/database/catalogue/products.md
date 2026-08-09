# Database Table: `products`

**Domain:** Catalogue  
**MCP Owner:** Catalogue MCP  
**Schema:** `catalogue`

---

## Purpose

The `products` table is the **master catalogue** of every SKU (Stock Keeping Unit) that a store sells. It is the source of truth for item names, brand, unit of measure, GST classification, HSN codes, pricing (cost and MRP), and the reorder threshold. No product can be inventoried or billed without first existing in this table.

Each store has its own independent product catalogue — products are scoped to `store_id`. The same product name (e.g., "Aashirvaad Atta 5kg") in two different stores are two separate records.

---

## Schema

```sql
CREATE TYPE product_unit AS ENUM (
    'KG',       -- kilograms (loose items: rice, sugar, dal)
    'G',        -- grams
    'L',        -- litres
    'ML',       -- millilitres
    'PACKET',   -- packet/pouch (Maggi, chips)
    'PIECE',    -- individual piece (soap, biscuit pack)
    'DOZEN',    -- dozen (eggs)
    'BUNDLE'    -- bundle (bananas, spinach)
);

CREATE TABLE public.products (
    id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id        UUID            NOT NULL REFERENCES public.stores(id) ON DELETE RESTRICT,
    name            TEXT            NOT NULL,
    brand           TEXT,
    is_loose        BOOLEAN         NOT NULL DEFAULT FALSE,
    unit            product_unit    NOT NULL,
    hsn_code        TEXT,
    gst_rate        NUMERIC(5,2)    NOT NULL DEFAULT 0.00
                                    CHECK (gst_rate >= 0 AND gst_rate <= 28),
    cost_price      NUMERIC(10,2)   NOT NULL CHECK (cost_price >= 0),
    mrp             NUMERIC(10,2)   NOT NULL CHECK (mrp >= 0),
    reorder_level   NUMERIC(10,3)   NOT NULL DEFAULT 0 CHECK (reorder_level >= 0),
    is_active       BOOLEAN         NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);
```

### Column Definitions

| Column | Type | Nullable | Default | Description |
|---|---|---|---|---|
| `id` | UUID | No | `gen_random_uuid()` | Internal primary key. Referenced by inventory, bill_items, stock_movements. |
| `store_id` | UUID | No | — | FK to `stores.id`. All products are scoped to a store. |
| `name` | TEXT | No | — | Product display name (e.g., "Aashirvaad Atta 5kg", "Sugar", "Tata Salt 1kg"). |
| `brand` | TEXT | Yes | — | Brand name (e.g., "Aashirvaad", "Amul", "Tata"). NULL for loose/unbranded items. |
| `is_loose` | BOOLEAN | No | `FALSE` | TRUE for loose items sold by weight (sugar, rice, dal). Loose items always have `gst_rate = 0`. |
| `unit` | product_unit | No | — | Unit of measure for this product. |
| `hsn_code` | TEXT | Yes | — | Harmonized System of Nomenclature code. Required for branded/packaged items for GST compliance. NULL for loose items (optional). |
| `gst_rate` | NUMERIC(5,2) | No | `0.00` | GST rate as a percentage (e.g., 5.00, 12.00, 18.00). Always 0 for loose items (enforced by trigger). |
| `cost_price` | NUMERIC(10,2) | No | — | The price the shop paid per unit. Used for the don't-sell-below-cost guardrail. |
| `mrp` | NUMERIC(10,2) | No | — | Maximum Retail Price / selling price per unit. Used in bill calculation. |
| `reorder_level` | NUMERIC(10,3) | No | `0` | When inventory quantity falls at or below this level, a reorder alert is triggered. |
| `is_active` | BOOLEAN | No | `TRUE` | Soft-delete. Inactive products are not shown in catalogue or available for billing. |
| `created_at` | TIMESTAMPTZ | No | `NOW()` | Immutable creation timestamp. |
| `updated_at` | TIMESTAMPTZ | No | `NOW()` | Auto-updated via trigger. |

---

## GST Classification Rules

### Rule 1: Loose Items Are Always 0% GST
Loose items (sugar, rice, dal, flour sold by weight without packaging) are exempt from GST under Indian tax law.

```sql
-- Enforced by trigger — loose items cannot have non-zero GST
CREATE OR REPLACE FUNCTION enforce_loose_item_gst()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.is_loose = TRUE AND NEW.gst_rate != 0 THEN
        RAISE EXCEPTION 'Loose items must have 0%% GST. Set gst_rate = 0 for loose items.';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER check_loose_item_gst
    BEFORE INSERT OR UPDATE ON public.products
    FOR EACH ROW EXECUTE FUNCTION enforce_loose_item_gst();
```

### Rule 2: GST Rate Reference (Indian Grocery Context)

| Category | Examples | GST Rate | Notes |
|---|---|---|---|
| Loose staples | Sugar, rice, dal, loose atta | 0% | is_loose = TRUE |
| Packaged staples | Aashirvaad Atta, Tata Salt, Fortune Oil | 5% | is_loose = FALSE |
| Branded milk products | Amul Butter, Amul Cheese | 12% | is_loose = FALSE |
| FMCG / packaged foods | Maggi, Parle-G, chocolates | 12–18% | is_loose = FALSE |
| Soaps, detergents | Surf Excel, Lux | 18% | is_loose = FALSE |

### Rule 3: HSN Codes (Common Examples)

| Product | HSN Code |
|---|---|
| Atta / flour (packaged) | 1101 |
| Rice (packaged) | 1006 |
| Sugar (packaged) | 1701 |
| Edible oil | 1512 |
| Instant noodles (Maggi) | 1902 |
| Biscuits | 1905 |
| Butter | 0405 |
| Salt | 2501 |
| Soap | 3401 |
| Detergent | 3402 |

---

## Unique SKU Design

Each product is uniquely identified within a store by the combination of `name + brand`. Two products with the same base name but different brands are **separate SKUs**:

```
"Aashirvaad Atta 5kg" (brand: Aashirvaad) → separate SKU
"Pillsbury Atta 5kg"  (brand: Pillsbury)  → separate SKU
"Sugar"               (brand: NULL)        → loose item, separate SKU
```

```sql
-- Unique constraint: same product name + brand cannot appear twice in one store
CREATE UNIQUE INDEX idx_products_store_name_brand
    ON public.products (store_id, LOWER(name), COALESCE(LOWER(brand), ''));
```

The `COALESCE(LOWER(brand), '')` ensures NULL brand is treated as empty string for uniqueness — so "Sugar" (no brand) can only appear once per store.

---

## Bot Conversation Flow for Adding a Product

When the owner says "add new item" or any similar intent, the agent (via Catalogue MCP) guides the conversation:

```
Agent:  "What's the item name?"
Owner:  "Aashirvaad Atta"

Agent:  "Is this a loose item or a branded/packaged product?"
Owner:  "branded"

Agent:  "What's the GST rate? (Common rates: 0%, 5%, 12%, 18%)"
Owner:  "5%"

Agent:  "What's the HSN code? (Press skip if you don't know)"
Owner:  "1101"

Agent:  "What unit? (kg / g / L / mL / packet / piece / dozen)"
Owner:  "kg"

Agent:  "What's the cost price per kg (what you paid)?"
Owner:  "₹22"

Agent:  "What's the MRP / selling price per kg?"
Owner:  "₹28"

Agent:  "What's the reorder level? (Alert me when stock falls below this quantity)"
Owner:  "10"

Agent:  "✅ Added: Aashirvaad Atta — 5% GST, HSN 1101, MRP ₹28/kg, reorder at 10kg"
```

---

## Constraints

```sql
-- Primary key
ALTER TABLE public.products ADD CONSTRAINT products_pkey PRIMARY KEY (id);

-- FK to stores
ALTER TABLE public.products ADD CONSTRAINT products_store_id_fkey
    FOREIGN KEY (store_id) REFERENCES public.stores(id) ON DELETE RESTRICT;

-- GST rate bounds
ALTER TABLE public.products ADD CONSTRAINT products_gst_rate_check
    CHECK (gst_rate >= 0 AND gst_rate <= 28);

-- Cost price non-negative
ALTER TABLE public.products ADD CONSTRAINT products_cost_price_check
    CHECK (cost_price >= 0);

-- MRP non-negative
ALTER TABLE public.products ADD CONSTRAINT products_mrp_check
    CHECK (mrp >= 0);

-- Reorder level non-negative
ALTER TABLE public.products ADD CONSTRAINT products_reorder_level_check
    CHECK (reorder_level >= 0);

-- updated_at trigger
CREATE TRIGGER products_updated_at
    BEFORE UPDATE ON public.products
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
```

---

## Indexes

```sql
-- Lookup by store (list all products for a store)
CREATE INDEX idx_products_store_id ON public.products (store_id);

-- Unique: name + brand per store (case-insensitive)
CREATE UNIQUE INDEX idx_products_store_name_brand
    ON public.products (store_id, LOWER(name), COALESCE(LOWER(brand), ''));

-- Full-text search on name + brand for agent product lookup
CREATE INDEX idx_products_search ON public.products
    USING GIN (to_tsvector('english', name || ' ' || COALESCE(brand, '')));

-- Active products filter
CREATE INDEX idx_products_is_active ON public.products (store_id, is_active)
    WHERE is_active = TRUE;
```

---

## Row Level Security (RLS)

```sql
ALTER TABLE public.products ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_role_full_access" ON public.products
    USING (TRUE)
    WITH CHECK (TRUE);
```

---

## Relations

### Outgoing (this table references)

| Table | Column | Type | Note |
|---|---|---|---|
| `stores` | `store_id` | Many-to-One | The store this product belongs to |

### Incoming (other tables reference `products`)

| Table | FK Column | Note |
|---|---|---|
| `inventory` | `product_id` | Stock levels for this product |
| `stock_movements` | `product_id` | Audit trail of stock changes |
| `draft_bill_items` | `product_id` | Product in an in-progress bill |
| `bill_items` | `product_id` | Product in a finalized bill |

---

## Business Rules

1. **Loose items always have 0% GST:** Enforced at DB level by trigger. The agent never needs to ask for GST when `is_loose = TRUE`.

2. **cost_price is the guardrail floor:** The billing system refuses to create a bill where `mrp < cost_price`. Selling below cost requires explicit owner override (Phase 2).

3. **Soft-delete only:** Setting `is_active = FALSE` hides the product from the catalogue and prevents new billing. Historical bill_items that reference this product are unaffected (they snapshot the product name at billing time).

4. **Product name snapshotting in bills:** When a bill is finalized, `bill_items.product_name_snapshot` stores the product name at that moment. This means renaming a product later does not alter historical invoices.

5. **Fuzzy search for agent tool calls:** The agent uses `search_products()` which leverages the GIN full-text index to match natural language like "maggi" → "Maggi 70g", "atta" → shows both "Aashirvaad Atta 5kg" and "Pillsbury Atta 5kg" → agent asks clarifying question.

---

## Phase 2 Extensibility

| Feature | Migration |
|---|---|
| Batch/expiry tracking | Add `batch_tracking_enabled BOOLEAN` column to products |
| Barcode scanning | Add `barcode TEXT` column with unique index |
| Supplier tracking | Add `supplier_id UUID` FK to a future `suppliers` table |
| Multiple price tiers | Add a `product_prices` child table |

---

## Example Records

```json
[
  {
    "id": "prod-001",
    "store_id": "store-001",
    "name": "Aashirvaad Atta 5kg",
    "brand": "Aashirvaad",
    "is_loose": false,
    "unit": "PACKET",
    "hsn_code": "1101",
    "gst_rate": 5.00,
    "cost_price": 220.00,
    "mrp": 275.00,
    "reorder_level": 10,
    "is_active": true
  },
  {
    "id": "prod-002",
    "store_id": "store-001",
    "name": "Sugar",
    "brand": null,
    "is_loose": true,
    "unit": "KG",
    "hsn_code": null,
    "gst_rate": 0.00,
    "cost_price": 38.00,
    "mrp": 45.00,
    "reorder_level": 5,
    "is_active": true
  },
  {
    "id": "prod-003",
    "store_id": "store-001",
    "name": "Maggi 70g",
    "brand": "Nestle",
    "is_loose": false,
    "unit": "PACKET",
    "hsn_code": "1902",
    "gst_rate": 12.00,
    "cost_price": 12.00,
    "mrp": 14.00,
    "reorder_level": 20,
    "is_active": true
  }
]
```
