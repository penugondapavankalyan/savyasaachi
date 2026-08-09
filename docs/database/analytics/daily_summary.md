# Database Table: `daily_summary`

**Domain:** Analytics  
**MCP Owner:** Analytics MCP  
**Schema:** `analytics`

---

## Purpose

The `daily_summary` table stores pre-aggregated daily sales data for a store. It is populated when the owner runs "close the day" and serves as the primary data source for the weekly analysis deck (PPTX) and quick daily close queries.

Pre-aggregating into this table ensures that analytics queries are fast even as the `bills` table grows over months/years.

---

## Schema

```sql
CREATE TABLE public.daily_summary (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id        UUID        NOT NULL REFERENCES public.stores(id) ON DELETE RESTRICT,
    summary_date    DATE        NOT NULL,
    bill_count      INTEGER     NOT NULL DEFAULT 0,
    total_sales     NUMERIC(12,2) NOT NULL DEFAULT 0.00,
    total_cgst      NUMERIC(10,2) NOT NULL DEFAULT 0.00,
    total_sgst      NUMERIC(10,2) NOT NULL DEFAULT 0.00,
    total_tax       NUMERIC(10,2) NOT NULL DEFAULT 0.00,
    cash_sales      NUMERIC(12,2) NOT NULL DEFAULT 0.00,
    upi_sales       NUMERIC(12,2) NOT NULL DEFAULT 0.00,
    card_sales      NUMERIC(12,2) NOT NULL DEFAULT 0.00,
    credit_sales    NUMERIC(12,2) NOT NULL DEFAULT 0.00,
    top_items       JSONB        NOT NULL DEFAULT '[]',
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
```

### Column Definitions

| Column | Type | Description |
|---|---|---|
| `id` | UUID | Primary key |
| `store_id` | UUID | FK to stores |
| `summary_date` | DATE | The calendar date this summary covers |
| `bill_count` | INTEGER | Number of finalized bills on this date |
| `total_sales` | NUMERIC(12,2) | Total revenue (subtotal + tax) |
| `total_cgst` | NUMERIC(10,2) | Total CGST collected |
| `total_sgst` | NUMERIC(10,2) | Total SGST collected |
| `total_tax` | NUMERIC(10,2) | `total_cgst + total_sgst` |
| `cash_sales` | NUMERIC(12,2) | Revenue from CASH payment bills |
| `upi_sales` | NUMERIC(12,2) | Revenue from UPI payment bills |
| `card_sales` | NUMERIC(12,2) | Revenue from CARD payment bills |
| `credit_sales` | NUMERIC(12,2) | Revenue from CREDIT (khata) bills |
| `top_items` | JSONB | Array of top 10 items by quantity sold. See schema below. |
| `created_at` | TIMESTAMPTZ | When this summary was first created |
| `updated_at` | TIMESTAMPTZ | Auto-updated (summary can be re-run to update) |

### `top_items` JSONB Schema

```json
[
  {
    "product_id": "prod-003",
    "product_name": "Maggi 70g",
    "quantity_sold": 42,
    "revenue": 588.00
  },
  ...
]
```

---

## Constraints

```sql
ALTER TABLE public.daily_summary ADD CONSTRAINT daily_summary_pkey PRIMARY KEY (id);

-- One summary per store per date
ALTER TABLE public.daily_summary ADD CONSTRAINT daily_summary_store_date_unique
    UNIQUE (store_id, summary_date);

ALTER TABLE public.daily_summary ADD CONSTRAINT daily_summary_store_id_fkey
    FOREIGN KEY (store_id) REFERENCES public.stores(id) ON DELETE RESTRICT;

CREATE TRIGGER daily_summary_updated_at
    BEFORE UPDATE ON public.daily_summary
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
```

---

## Indexes

```sql
CREATE UNIQUE INDEX idx_daily_summary_store_date ON public.daily_summary (store_id, summary_date);
CREATE INDEX idx_daily_summary_store_range ON public.daily_summary (store_id, summary_date DESC);
```

---

## Row Level Security (RLS)

```sql
ALTER TABLE public.daily_summary ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_full_access" ON public.daily_summary USING (TRUE) WITH CHECK (TRUE);
```
