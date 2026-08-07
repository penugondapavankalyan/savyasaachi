-- ============================================================
-- 008_create_khata_tables.sql
-- Schema:  khata
-- Tables:  khata.khata_entries
--
-- NOTE: billing.customers is the customer profile table.
--       It lives in the billing schema because billing.bills
--       has a FK to it. khata_entries references it cross-schema.
--
-- Run AFTER 007
-- ============================================================

CREATE TABLE khata.khata_entries (
    id                  UUID                    PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id            UUID                    NOT NULL
                                                REFERENCES identity.stores(id) ON DELETE RESTRICT,
    customer_id         UUID                    NOT NULL
                                                REFERENCES billing.customers(id) ON DELETE RESTRICT,
    entry_type          khata.entry_type        NOT NULL,
    amount_delta        NUMERIC(10,2)           NOT NULL,
    -- Positive = customer owes shop (credit)
    -- Negative = shop owes customer (payment received)
    reference_bill_id   UUID
                                                REFERENCES billing.bills(id) ON DELETE SET NULL,
    notes               TEXT,
    created_at          TIMESTAMPTZ             NOT NULL DEFAULT NOW()
    -- No updated_at — append-only, immutable
);

CREATE INDEX idx_khata_entries_customer
    ON khata.khata_entries (store_id, customer_id, created_at DESC);

CREATE INDEX idx_khata_entries_bill
    ON khata.khata_entries (reference_bill_id) WHERE reference_bill_id IS NOT NULL;

-- Immutability enforcement
CREATE TRIGGER khata_entries_no_update
    BEFORE UPDATE ON khata.khata_entries
    FOR EACH ROW EXECUTE FUNCTION public.prevent_immutable_record_mutation();

CREATE TRIGGER khata_entries_no_delete
    BEFORE DELETE ON khata.khata_entries
    FOR EACH ROW EXECUTE FUNCTION public.prevent_immutable_record_mutation();

ALTER TABLE khata.khata_entries ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_full_access" ON khata.khata_entries
    USING (TRUE) WITH CHECK (TRUE);

GRANT SELECT, INSERT ON khata.khata_entries TO service_role;
