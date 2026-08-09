# Database Table: `bill_items`

**Domain:** Billing  
**MCP Owner:** Billing MCP  
**Schema:** `billing`

---

## Purpose

The `bill_items` table stores the individual line items of a finalized bill. Each record represents one product on one bill — quantity, price, GST breakup, and line total. These records are **immutable** once created.

Product names and prices are snapshotted at the time of billing so that historical bills remain accurate even if products are later renamed or repriced.

---

## Schema

```sql
CREATE TABLE public.bill_items (
    id                      UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    bill_id                 UUID            NOT NULL REFERENCES public.bills(id) ON DELETE RESTRICT,
    product_id              UUID            REFERENCES public.products(id) ON DELETE SET NULL,
    product_name_snapshot   TEXT            NOT NULL,
    brand_snapshot          TEXT,
    unit_snapshot           TEXT            NOT NULL,
    hsn_code_snapshot       TEXT,
    quantity                NUMERIC(10,3)   NOT NULL CHECK (quantity > 0),
    unit_price              NUMERIC(10,2)   NOT NULL CHECK (unit_price >= 0),
    gst_rate                NUMERIC(5,2)    NOT NULL DEFAULT 0.00,
    taxable_value           NUMERIC(10,2)   NOT NULL CHECK (taxable_value >= 0),
    cgst_amount             NUMERIC(10,2)   NOT NULL DEFAULT 0.00 CHECK (cgst_amount >= 0),
    sgst_amount             NUMERIC(10,2)   NOT NULL DEFAULT 0.00 CHECK (sgst_amount >= 0),
    line_total              NUMERIC(10,2)   NOT NULL CHECK (line_total >= 0),
    created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);
```

### Column Definitions

| Column | Type | Nullable | Default | Description |
|---|---|---|---|---|
| `id` | UUID | No | `gen_random_uuid()` | Internal primary key. |
| `bill_id` | UUID | No | — | FK to `bills.id`. RESTRICT delete — bills with items cannot be deleted. |
| `product_id` | UUID | Yes | — | FK to `products.id`. SET NULL on delete — if a product is hard-deleted (should not happen), the bill item is preserved. |
| `product_name_snapshot` | TEXT | No | — | Product name at the time of billing. Immutable historical record. |
| `brand_snapshot` | TEXT | Yes | — | Brand at time of billing. |
| `unit_snapshot` | TEXT | No | — | Unit of measure at time of billing (e.g., "KG", "PACKET"). |
| `hsn_code_snapshot` | TEXT | Yes | — | HSN code at time of billing. Required for GST-compliant invoice PDFs. |
| `quantity` | NUMERIC(10,3) | No | — | Quantity billed. |
| `unit_price` | NUMERIC(10,2) | No | — | Price per unit at time of billing (snapshotted from `products.mrp`). |
| `gst_rate` | NUMERIC(5,2) | No | `0.00` | GST rate at time of billing (snapshotted from `products.gst_rate`). |
| `taxable_value` | NUMERIC(10,2) | No | — | `quantity × unit_price`. Pre-tax value. |
| `cgst_amount` | NUMERIC(10,2) | No | `0.00` | `ROUND(taxable_value × gst_rate / 100 / 2, 2)`. |
| `sgst_amount` | NUMERIC(10,2) | No | `0.00` | `gst_total - cgst_amount` (avoids double-rounding). |
| `line_total` | NUMERIC(10,2) | No | — | `taxable_value + cgst_amount + sgst_amount`. |
| `created_at` | TIMESTAMPTZ | No | `NOW()` | Immutable timestamp. **No `updated_at`** — this table is immutable. |

---

## Constraints

```sql
ALTER TABLE public.bill_items ADD CONSTRAINT bill_items_pkey PRIMARY KEY (id);
ALTER TABLE public.bill_items ADD CONSTRAINT bill_items_bill_id_fkey
    FOREIGN KEY (bill_id) REFERENCES public.bills(id) ON DELETE RESTRICT;
ALTER TABLE public.bill_items ADD CONSTRAINT bill_items_product_id_fkey
    FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE SET NULL;
ALTER TABLE public.bill_items ADD CONSTRAINT bill_items_quantity_positive CHECK (quantity > 0);
```

---

## Indexes

```sql
-- All items for a bill (primary query for invoice PDF)
CREATE INDEX idx_bill_items_bill_id ON public.bill_items (bill_id);

-- Lookup by product (for analytics: how many units of X sold)
CREATE INDEX idx_bill_items_product_id ON public.bill_items (product_id) WHERE product_id IS NOT NULL;
```

---

## Row Level Security (RLS)

```sql
ALTER TABLE public.bill_items ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_full_access" ON public.bill_items USING (TRUE) WITH CHECK (TRUE);
```

---

## GST Calculation Example

```
Product: Maggi 70g
Quantity: 4
Unit Price: ₹14.00
GST Rate: 12%

taxable_value = 4 × 14.00 = ₹56.00
gst_total     = 56.00 × 12 / 100 = ₹6.72
cgst_amount   = ROUND(6.72 / 2, 2) = ₹3.36
sgst_amount   = 6.72 - 3.36 = ₹3.36
line_total    = 56.00 + 3.36 + 3.36 = ₹62.72
```

---

## Relations

### Outgoing
| Table | Column | Note |
|---|---|---|
| `bills` | `bill_id` | Parent bill |
| `products` | `product_id` | Source product (nullable after product deletion) |
