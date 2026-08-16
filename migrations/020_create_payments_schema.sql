-- ============================================================
-- 020_create_payments_schema.sql
--
-- Creates the payments domain:
--   1. payments schema
--   2. payments.payment_type enum
--   3. payments.payment_status enum
--   4. payments.payments table (immutable, append-only)
--   5. Indexes + RLS + immutability trigger
--
-- NOTE: After running this migration you MUST manually expose
-- the 'payments' schema in Supabase Dashboard:
--   Dashboard → Project Settings → API → Exposed schemas
--   Add: payments
--
-- Run AFTER 019
-- ============================================================


-- -----------------------------------------------------------
-- 1. Create payments schema
-- -----------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS payments;

GRANT USAGE ON SCHEMA payments TO authenticated, anon, service_role;


-- -----------------------------------------------------------
-- 2. payment_type enum
--    EXACT        — paid_amount == bill_amount (exact match)
--    OVERPAYMENT  — paid_amount > bill_amount  (change returned or put to khata)
--    UNDERPAYMENT — paid_amount < bill_amount  (balance put to khata)
--    KHATA        — credit sale, zero cash exchanged, full amount on customer tab
--    KHATA_SETTLE — customer settling their khata balance (no new bill)
-- -----------------------------------------------------------
CREATE TYPE payments.payment_type AS ENUM (
    'EXACT',
    'OVERPAYMENT',
    'UNDERPAYMENT',
    'KHATA',
    'KHATA_SETTLE'
);


-- -----------------------------------------------------------
-- 3. payment_status enum
--    CONFIRMED  — payment received and recorded
--    PENDING    — reserved for future use (currently unused)
--    CANCELLED  — bill was cancelled before payment
--    REFUNDED   — bill was voided after payment confirmed
-- -----------------------------------------------------------
CREATE TYPE payments.payment_status AS ENUM (
    'CONFIRMED',
    'PENDING',
    'CANCELLED',
    'REFUNDED'
);


-- -----------------------------------------------------------
-- 4. payments.payments table
--
-- Design notes:
--   - payment_id is the primary key (UUID), named payment_id not id
--   - bill_id is NULL for KHATA_SETTLE rows (no new bill, just settlement)
--   - customer_id is optional (only set when customer is identified)
--   - khata_entry_id is set when over/underpayment creates a khata entry
--   - subtotal, total_gst, bill_amount are snapshots from billing.bills
--     (denormalised so payment records are self-contained for reporting)
--   - change_amount: cash returned to customer on overpayment
--   - balance_due:   amount sent to khata on underpayment
--   - Immutable: no updated_at, append-only trigger
-- -----------------------------------------------------------
CREATE TABLE payments.payments (
    payment_id          UUID                        PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Core relationships
    bill_id             UUID                                    -- NULL for KHATA_SETTLE
                                                    REFERENCES billing.bills(id) ON DELETE RESTRICT,
    store_id            UUID                        NOT NULL
                                                    REFERENCES identity.stores(id) ON DELETE RESTRICT,
    customer_id         UUID                                    -- optional
                                                    REFERENCES billing.customers(id) ON DELETE RESTRICT,
    khata_entry_id      UUID                                    -- set when khata involved
                                                    REFERENCES khata.khata_entries(id) ON DELETE SET NULL,

    -- Bill financial snapshot (from billing.bills at time of payment)
    -- NULL for KHATA_SETTLE rows (no bill)
    subtotal            NUMERIC(10,2)               CHECK (subtotal IS NULL OR subtotal >= 0),
    total_gst           NUMERIC(10,2)               CHECK (total_gst IS NULL OR total_gst >= 0),
    bill_amount         NUMERIC(10,2)               CHECK (bill_amount IS NULL OR bill_amount >= 0),

    -- Payment details
    paid_amount         NUMERIC(10,2)               NOT NULL CHECK (paid_amount >= 0),
    payment_mode        billing.payment_mode        NOT NULL,
    payment_reference   TEXT,                       -- UPI transaction ID, etc.

    -- Payment classification
    payment_type        payments.payment_type       NOT NULL,
    payment_status      payments.payment_status     NOT NULL DEFAULT 'CONFIRMED',

    -- Derived amounts
    change_amount       NUMERIC(10,2)               NOT NULL DEFAULT 0.00 CHECK (change_amount >= 0),
    -- Overpayment: cash returned to customer (paid_amount - bill_amount)
    balance_due         NUMERIC(10,2)               NOT NULL DEFAULT 0.00 CHECK (balance_due >= 0),
    -- Underpayment: amount sent to khata (bill_amount - paid_amount)

    -- Audit
    created_at          TIMESTAMPTZ                 NOT NULL DEFAULT NOW()
    -- No updated_at — immutable, append-only
);


-- -----------------------------------------------------------
-- 5. Indexes
-- -----------------------------------------------------------

-- Look up all payments for a bill
CREATE INDEX idx_payments_bill_id
    ON payments.payments (bill_id)
    WHERE bill_id IS NOT NULL;

-- Look up all payments for a store (sorted by date for history)
CREATE INDEX idx_payments_store_date
    ON payments.payments (store_id, created_at DESC);

-- Look up all payments for a customer
CREATE INDEX idx_payments_customer_id
    ON payments.payments (customer_id)
    WHERE customer_id IS NOT NULL;

-- Look up payments by status (for reporting)
CREATE INDEX idx_payments_status
    ON payments.payments (store_id, payment_status, created_at DESC);


-- -----------------------------------------------------------
-- 6. Immutability trigger
--    payments.payments rows are append-only.
--    No UPDATE or DELETE is permitted.
-- -----------------------------------------------------------
CREATE TRIGGER payments_no_update
    BEFORE UPDATE ON payments.payments
    FOR EACH ROW EXECUTE FUNCTION public.prevent_immutable_record_mutation();

CREATE TRIGGER payments_no_delete
    BEFORE DELETE ON payments.payments
    FOR EACH ROW EXECUTE FUNCTION public.prevent_immutable_record_mutation();


-- -----------------------------------------------------------
-- 7. Row Level Security
-- -----------------------------------------------------------
ALTER TABLE payments.payments ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_role_full_access" ON payments.payments
    USING (TRUE) WITH CHECK (TRUE);

GRANT SELECT, INSERT ON payments.payments TO service_role;
