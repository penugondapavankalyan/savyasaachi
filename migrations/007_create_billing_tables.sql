-- ============================================================
-- 007_create_billing_tables.sql
-- Schema:  billing
-- Tables:  billing.draft_bills
--          billing.draft_bill_items
--          billing.customers   ← in billing schema; khata entries reference it
--          billing.bills
--          billing.bill_items
--
-- Also adds: identity.workflow_state.active_draft_bill_id FK
--            (deferred until billing.draft_bills exists)
--
-- Run AFTER 006
-- ============================================================


-- -----------------------------------------------------------
-- billing.draft_bills
-- In-progress bills being built across multiple messages.
-- workflow_id links messages in the same billing session.
-- -----------------------------------------------------------
CREATE TABLE billing.draft_bills (
    id                  UUID                        PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id            UUID                        NOT NULL
                                                    REFERENCES identity.stores(id) ON DELETE RESTRICT,
    telegram_user_id    BIGINT                      NOT NULL,
    workflow_id         UUID                        NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    status              billing.draft_bill_status   NOT NULL DEFAULT 'OPEN',
    expires_at          TIMESTAMPTZ                 NOT NULL DEFAULT (NOW() + INTERVAL '4 hours'),
    created_at          TIMESTAMPTZ                 NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ                 NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX idx_billing_draft_bills_workflow_id
    ON billing.draft_bills (workflow_id);

CREATE INDEX idx_billing_draft_bills_user_open
    ON billing.draft_bills (telegram_user_id, status) WHERE status = 'OPEN';

CREATE INDEX idx_billing_draft_bills_store_id
    ON billing.draft_bills (store_id, created_at DESC);

CREATE INDEX idx_billing_draft_bills_expires_at
    ON billing.draft_bills (expires_at) WHERE status = 'OPEN';

CREATE TRIGGER billing_draft_bills_updated_at
    BEFORE UPDATE ON billing.draft_bills
    FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();

ALTER TABLE billing.draft_bills ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_full_access" ON billing.draft_bills
    USING (TRUE) WITH CHECK (TRUE);

GRANT SELECT, INSERT, UPDATE ON billing.draft_bills TO service_role;


-- -----------------------------------------------------------
-- Now that billing.draft_bills exists, add the FK from
-- identity.workflow_state.active_draft_bill_id
-- -----------------------------------------------------------
ALTER TABLE identity.workflow_state
    ADD CONSTRAINT workflow_state_active_draft_bill_fkey
    FOREIGN KEY (active_draft_bill_id)
    REFERENCES billing.draft_bills(id) ON DELETE SET NULL;


-- -----------------------------------------------------------
-- billing.draft_bill_items
-- Editable line items attached to an open draft bill.
-- Deleted automatically when parent draft is cancelled (CASCADE).
-- -----------------------------------------------------------
CREATE TABLE billing.draft_bill_items (
    id                      UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    draft_bill_id           UUID            NOT NULL
                                            REFERENCES billing.draft_bills(id) ON DELETE CASCADE,
    product_id              UUID            NOT NULL
                                            REFERENCES catalogue.products(id) ON DELETE RESTRICT,
    quantity                NUMERIC(10,3)   NOT NULL CHECK (quantity > 0),
    unit_price              NUMERIC(10,2)   NOT NULL CHECK (unit_price >= 0),
    gst_rate                NUMERIC(5,2)    NOT NULL DEFAULT 0.00
                                            CHECK (gst_rate >= 0 AND gst_rate <= 28),
    is_partial_fulfillment  BOOLEAN         NOT NULL DEFAULT FALSE,
    available_quantity      NUMERIC(10,3),
    created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    UNIQUE (draft_bill_id, product_id)
);

CREATE INDEX idx_billing_draft_bill_items_draft_id
    ON billing.draft_bill_items (draft_bill_id);

CREATE TRIGGER billing_draft_bill_items_updated_at
    BEFORE UPDATE ON billing.draft_bill_items
    FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();

ALTER TABLE billing.draft_bill_items ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_full_access" ON billing.draft_bill_items
    USING (TRUE) WITH CHECK (TRUE);

GRANT SELECT, INSERT, UPDATE, DELETE ON billing.draft_bill_items TO service_role;


-- -----------------------------------------------------------
-- billing.customers
-- Customer profiles for khata (credit) accounts.
-- Placed in billing schema — referenced by billing.bills.
-- Khata entries (separate schema) also reference this table.
-- -----------------------------------------------------------
CREATE TABLE billing.customers (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id    UUID        NOT NULL
                            REFERENCES identity.stores(id) ON DELETE RESTRICT,
    name        TEXT        NOT NULL,
    phone       TEXT        NOT NULL,
    notes       TEXT,
    is_active   BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (store_id, phone)
);

CREATE INDEX idx_billing_customers_store_name
    ON billing.customers (store_id, LOWER(name));

CREATE INDEX idx_billing_customers_store_id
    ON billing.customers (store_id);

CREATE TRIGGER billing_customers_updated_at
    BEFORE UPDATE ON billing.customers
    FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();

ALTER TABLE billing.customers ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_full_access" ON billing.customers
    USING (TRUE) WITH CHECK (TRUE);

GRANT SELECT, INSERT, UPDATE ON billing.customers TO service_role;


-- -----------------------------------------------------------
-- billing.bills
-- Finalized, immutable billing records.
-- workflow_id is the idempotency key — unique constraint prevents
-- double-billing on Telegram webhook redelivery.
-- -----------------------------------------------------------
CREATE TABLE billing.bills (
    id                  UUID                    PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id            UUID                    NOT NULL
                                                REFERENCES identity.stores(id) ON DELETE RESTRICT,
    bill_number         TEXT                    NOT NULL,
    telegram_user_id    BIGINT                  NOT NULL,
    workflow_id         UUID                    NOT NULL UNIQUE,
    customer_id         UUID
                                                REFERENCES billing.customers(id) ON DELETE RESTRICT,
    subtotal            NUMERIC(10,2)           NOT NULL CHECK (subtotal >= 0),
    total_cgst          NUMERIC(10,2)           NOT NULL DEFAULT 0.00 CHECK (total_cgst >= 0),
    total_sgst          NUMERIC(10,2)           NOT NULL DEFAULT 0.00 CHECK (total_sgst >= 0),
    total_discount      NUMERIC(10,2)           NOT NULL DEFAULT 0.00 CHECK (total_discount >= 0),
    total_amount        NUMERIC(10,2)           NOT NULL CHECK (total_amount >= 0),
    payment_mode        billing.payment_mode    NOT NULL,
    payment_reference   TEXT,
    is_credit           BOOLEAN                 NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ             NOT NULL DEFAULT NOW(),
    -- No updated_at — bills are immutable after creation
    CONSTRAINT billing_bills_credit_requires_customer
        CHECK (is_credit = FALSE OR (is_credit = TRUE AND customer_id IS NOT NULL))
);

CREATE UNIQUE INDEX idx_billing_bills_workflow_id
    ON billing.bills (workflow_id);

CREATE INDEX idx_billing_bills_store_date
    ON billing.bills (store_id, created_at DESC);

CREATE INDEX idx_billing_bills_customer
    ON billing.bills (customer_id) WHERE customer_id IS NOT NULL;

CREATE INDEX idx_billing_bills_store_number
    ON billing.bills (store_id, bill_number);

-- Immutability enforcement
CREATE TRIGGER billing_bills_no_update
    BEFORE UPDATE ON billing.bills
    FOR EACH ROW EXECUTE FUNCTION public.prevent_immutable_record_mutation();

CREATE TRIGGER billing_bills_no_delete
    BEFORE DELETE ON billing.bills
    FOR EACH ROW EXECUTE FUNCTION public.prevent_immutable_record_mutation();

ALTER TABLE billing.bills ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_full_access" ON billing.bills
    USING (TRUE) WITH CHECK (TRUE);

GRANT SELECT, INSERT ON billing.bills TO service_role;


-- -----------------------------------------------------------
-- billing.bill_items
-- Immutable line items of a finalized bill.
-- Product details snapshotted at time of billing.
-- -----------------------------------------------------------
CREATE TABLE billing.bill_items (
    id                      UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    bill_id                 UUID            NOT NULL
                                            REFERENCES billing.bills(id) ON DELETE RESTRICT,
    product_id              UUID
                                            REFERENCES catalogue.products(id) ON DELETE SET NULL,
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
    -- No updated_at — immutable
);

CREATE INDEX idx_billing_bill_items_bill_id
    ON billing.bill_items (bill_id);

CREATE INDEX idx_billing_bill_items_product_id
    ON billing.bill_items (product_id) WHERE product_id IS NOT NULL;

-- Immutability enforcement
CREATE TRIGGER billing_bill_items_no_update
    BEFORE UPDATE ON billing.bill_items
    FOR EACH ROW EXECUTE FUNCTION public.prevent_immutable_record_mutation();

CREATE TRIGGER billing_bill_items_no_delete
    BEFORE DELETE ON billing.bill_items
    FOR EACH ROW EXECUTE FUNCTION public.prevent_immutable_record_mutation();

ALTER TABLE billing.bill_items ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_full_access" ON billing.bill_items
    USING (TRUE) WITH CHECK (TRUE);

GRANT SELECT, INSERT ON billing.bill_items TO service_role;
