# Implementation Guide 1: Build Database Schema

**Order:** First — all other steps depend on the database being ready.  
**Reference Docs:** `docs/database/` (all table docs), `docs/infrastructure/supabase.md`

---

## Prerequisites

- Supabase project created
- `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` noted
- Supabase CLI installed (`npm install -g supabase`) or use Dashboard SQL editor

---

## Step 1: Enable Required PostgreSQL Extensions

```sql
-- migrations/001_create_extensions.sql
CREATE EXTENSION IF NOT EXISTS "pgcrypto";    -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";   -- uuid_generate_v4() (fallback)
CREATE EXTENSION IF NOT EXISTS "pg_trgm";     -- Trigram similarity for fuzzy search
```

---

## Step 2: Create All ENUM Types

```sql
-- migrations/002_create_enums.sql

CREATE TYPE product_unit AS ENUM (
    'KG', 'G', 'L', 'ML', 'PACKET', 'PIECE', 'DOZEN', 'BUNDLE'
);

CREATE TYPE movement_type AS ENUM (
    'STOCK_IN', 'SALE', 'ADJUSTMENT'
);

CREATE TYPE registration_status AS ENUM (
    'INITIATED', 'STORE_CREATED', 'COMPLETE'
);

CREATE TYPE draft_bill_status AS ENUM (
    'OPEN', 'CONFIRMED', 'CANCELLED', 'EXPIRED'
);

CREATE TYPE payment_mode AS ENUM (
    'CASH', 'UPI', 'CARD', 'CREDIT'
);

CREATE TYPE khata_entry_type AS ENUM (
    'CREDIT', 'PAYMENT', 'ADJUSTMENT'
);

CREATE TYPE user_workflow_state AS ENUM (
    'UNREGISTERED', 'PENDING_CATALOGUE', 'PENDING_INVENTORY', 'ACTIVE'
);
```

---

## Step 3: Create Utility Functions

```sql
-- migrations/003_create_utility_functions.sql

-- Auto-update updated_at on row change
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Prevent mutation of immutable tables
CREATE OR REPLACE FUNCTION prevent_immutable_record_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'Records in % are immutable and cannot be updated or deleted.', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

-- Enforce loose items have 0% GST
CREATE OR REPLACE FUNCTION enforce_loose_item_gst()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.is_loose = TRUE AND NEW.gst_rate != 0 THEN
        RAISE EXCEPTION 'Loose items must have 0%% GST.';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
```

---

## Step 4: Create Identity Domain Tables

Create in this order (respects FK dependencies):
1. `users`
2. `stores` (references users)
3. `registrations` (references users, stores)
4. `workflow_state` (references users, stores, draft_bills — add draft_bills FK after billing tables)

```sql
-- migrations/004_create_identity_tables.sql

CREATE TABLE public.users (
    id                  UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_user_id    BIGINT          NOT NULL UNIQUE,
    telegram_username   TEXT,
    first_name          TEXT,
    last_name           TEXT,
    is_active           BOOLEAN         NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE TABLE public.stores (
    id                      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id           UUID        NOT NULL UNIQUE REFERENCES public.users(id) ON DELETE RESTRICT,
    shop_name               TEXT        NOT NULL,
    gstin                   TEXT        CHECK (gstin IS NULL OR gstin ~ '^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$'),
    state_code              TEXT        NOT NULL DEFAULT '29',
    address                 TEXT,
    phone                   TEXT,
    default_payment_mode    TEXT        NOT NULL DEFAULT 'CASH' CHECK (default_payment_mode IN ('CASH', 'UPI', 'CARD')),
    preferences             JSONB       NOT NULL DEFAULT '{}',
    is_active               BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE public.registrations (
    id                  UUID                PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_user_id    BIGINT              NOT NULL UNIQUE,
    user_id             UUID                NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
    store_id            UUID                REFERENCES public.stores(id) ON DELETE RESTRICT,
    status              registration_status NOT NULL DEFAULT 'INITIATED',
    initiated_at        TIMESTAMPTZ         NOT NULL DEFAULT NOW(),
    store_created_at    TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    failure_reason      TEXT,
    created_at          TIMESTAMPTZ         NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ         NOT NULL DEFAULT NOW()
);

-- workflow_state created after billing tables (FK to draft_bills added via ALTER TABLE)
CREATE TABLE public.workflow_state (
    id                      UUID                    PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_user_id        BIGINT                  NOT NULL UNIQUE,
    user_id                 UUID                    REFERENCES public.users(id) ON DELETE RESTRICT,
    store_id                UUID                    REFERENCES public.stores(id) ON DELETE RESTRICT,
    current_state           user_workflow_state     NOT NULL DEFAULT 'UNREGISTERED',
    active_draft_bill_id    UUID,  -- FK added after draft_bills table creation
    updated_at              TIMESTAMPTZ             NOT NULL DEFAULT NOW()
);
```

---

## Step 5: Create Catalogue Tables

```sql
-- migrations/005_create_catalogue_tables.sql

CREATE TABLE public.products (
    id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id        UUID            NOT NULL REFERENCES public.stores(id) ON DELETE RESTRICT,
    name            TEXT            NOT NULL,
    brand           TEXT,
    is_loose        BOOLEAN         NOT NULL DEFAULT FALSE,
    unit            product_unit    NOT NULL,
    hsn_code        TEXT,
    gst_rate        NUMERIC(5,2)    NOT NULL DEFAULT 0.00 CHECK (gst_rate >= 0 AND gst_rate <= 28),
    cost_price      NUMERIC(10,2)   NOT NULL CHECK (cost_price >= 0),
    mrp             NUMERIC(10,2)   NOT NULL CHECK (mrp >= 0),
    reorder_level   NUMERIC(10,3)   NOT NULL DEFAULT 0 CHECK (reorder_level >= 0),
    is_active       BOOLEAN         NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

-- Unique: same name+brand per store (case-insensitive)
CREATE UNIQUE INDEX idx_products_store_name_brand
    ON public.products (store_id, LOWER(name), COALESCE(LOWER(brand), ''));
```

---

## Step 6: Create Inventory Tables

```sql
-- migrations/006_create_inventory_tables.sql

CREATE TABLE public.inventory (
    id                  UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id            UUID            NOT NULL REFERENCES public.stores(id) ON DELETE RESTRICT,
    product_id          UUID            NOT NULL REFERENCES public.products(id) ON DELETE RESTRICT,
    quantity_in_stock   NUMERIC(10,3)   NOT NULL DEFAULT 0 CHECK (quantity_in_stock >= 0),
    reorder_level       NUMERIC(10,3)   NOT NULL DEFAULT 0 CHECK (reorder_level >= 0),
    last_restocked_at   TIMESTAMPTZ,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    UNIQUE (store_id, product_id)
);

CREATE TABLE public.stock_movements (
    id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id        UUID            NOT NULL REFERENCES public.stores(id) ON DELETE RESTRICT,
    product_id      UUID            NOT NULL REFERENCES public.products(id) ON DELETE RESTRICT,
    movement_type   movement_type   NOT NULL,
    quantity_delta  NUMERIC(10,3)   NOT NULL,
    reference_id    UUID,
    reference_type  TEXT            CHECK (reference_type IN ('BILL', 'STOCK_IN', 'ADJUSTMENT')),
    notes           TEXT,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);
```

---

## Step 7: Create Billing Tables

```sql
-- migrations/007_create_billing_tables.sql

CREATE TABLE public.draft_bills (
    id                  UUID                PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id            UUID                NOT NULL REFERENCES public.stores(id) ON DELETE RESTRICT,
    telegram_user_id    BIGINT              NOT NULL,
    workflow_id         UUID                NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    status              draft_bill_status   NOT NULL DEFAULT 'OPEN',
    expires_at          TIMESTAMPTZ         NOT NULL DEFAULT (NOW() + INTERVAL '4 hours'),
    created_at          TIMESTAMPTZ         NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ         NOT NULL DEFAULT NOW()
);

CREATE TABLE public.draft_bill_items (
    id                      UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    draft_bill_id           UUID            NOT NULL REFERENCES public.draft_bills(id) ON DELETE CASCADE,
    product_id              UUID            NOT NULL REFERENCES public.products(id) ON DELETE RESTRICT,
    quantity                NUMERIC(10,3)   NOT NULL CHECK (quantity > 0),
    unit_price              NUMERIC(10,2)   NOT NULL CHECK (unit_price >= 0),
    gst_rate                NUMERIC(5,2)    NOT NULL DEFAULT 0.00,
    is_partial_fulfillment  BOOLEAN         NOT NULL DEFAULT FALSE,
    available_quantity      NUMERIC(10,3),
    created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    UNIQUE (draft_bill_id, product_id)
);

CREATE TABLE public.customers (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id    UUID        NOT NULL REFERENCES public.stores(id) ON DELETE RESTRICT,
    name        TEXT        NOT NULL,
    phone       TEXT        NOT NULL,
    notes       TEXT,
    is_active   BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (store_id, phone)
);

CREATE TABLE public.bills (
    id                  UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id            UUID            NOT NULL REFERENCES public.stores(id) ON DELETE RESTRICT,
    bill_number         TEXT            NOT NULL,
    telegram_user_id    BIGINT          NOT NULL,
    workflow_id         UUID            NOT NULL UNIQUE,
    customer_id         UUID            REFERENCES public.customers(id) ON DELETE RESTRICT,
    subtotal            NUMERIC(10,2)   NOT NULL CHECK (subtotal >= 0),
    total_cgst          NUMERIC(10,2)   NOT NULL DEFAULT 0.00 CHECK (total_cgst >= 0),
    total_sgst          NUMERIC(10,2)   NOT NULL DEFAULT 0.00 CHECK (total_sgst >= 0),
    total_discount      NUMERIC(10,2)   NOT NULL DEFAULT 0.00 CHECK (total_discount >= 0),
    total_amount        NUMERIC(10,2)   NOT NULL CHECK (total_amount >= 0),
    payment_mode        payment_mode    NOT NULL,
    payment_reference   TEXT,
    is_credit           BOOLEAN         NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    CONSTRAINT bills_credit_requires_customer CHECK (is_credit = FALSE OR (is_credit = TRUE AND customer_id IS NOT NULL))
);

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

-- Add FK from workflow_state to draft_bills (now that draft_bills exists)
ALTER TABLE public.workflow_state
    ADD CONSTRAINT workflow_state_active_draft_bill_fkey
    FOREIGN KEY (active_draft_bill_id) REFERENCES public.draft_bills(id) ON DELETE SET NULL;
```

---

## Step 8: Create Khata and Analytics Tables

```sql
-- migrations/008_create_khata_tables.sql

CREATE TABLE public.khata_entries (
    id                  UUID                PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id            UUID                NOT NULL REFERENCES public.stores(id) ON DELETE RESTRICT,
    customer_id         UUID                NOT NULL REFERENCES public.customers(id) ON DELETE RESTRICT,
    entry_type          khata_entry_type    NOT NULL,
    amount_delta        NUMERIC(10,2)       NOT NULL,
    reference_bill_id   UUID                REFERENCES public.bills(id) ON DELETE SET NULL,
    notes               TEXT,
    created_at          TIMESTAMPTZ         NOT NULL DEFAULT NOW()
);

-- migrations/009_create_analytics_tables.sql

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
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (store_id, summary_date)
);
```

---

## Step 9: Create All Indexes

See each table's `.md` file for the full index list. Apply via `migrations/010_create_indexes.sql`.

---

## Step 10: Create All Triggers and RLS Policies

Apply `migrations/011_create_rls_policies.sql` and `migrations/012_create_triggers.sql`.

Key triggers to verify:
- `update_updated_at_column` on all mutable tables
- `enforce_loose_item_gst` on products
- `prevent_immutable_record_mutation` on `bills`, `bill_items`, `khata_entries`, `stock_movements`

---

## Step 11: Create Supabase RPCs

Apply `migrations/013_create_rpcs.sql` containing:
- `decrement_stock` — atomic inventory decrement
- `generate_bill_number` — sequential bill number
- `upsert_workflow_state` — idempotent workflow state init

---

## Step 12: Validation Checklist

After applying all migrations, verify:

```sql
-- Check all tables exist
SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename;
-- Expected: 13 tables

-- Check all enums exist
SELECT typname FROM pg_type WHERE typtype = 'e' ORDER BY typname;
-- Expected: 7 enums

-- Test the decrement_stock RPC exists
SELECT proname FROM pg_proc WHERE proname = 'decrement_stock';

-- Test loose item GST trigger
INSERT INTO products (..., is_loose=TRUE, gst_rate=5) ...;
-- Should RAISE EXCEPTION

-- Test immutability trigger on bills
INSERT INTO bills (...); -- succeeds
UPDATE bills SET total_amount = 0 WHERE id = ...; -- should RAISE EXCEPTION
```
