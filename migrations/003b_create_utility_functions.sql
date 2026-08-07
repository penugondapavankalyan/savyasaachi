-- ============================================================
-- 003b_create_utility_functions.sql
-- Shared trigger functions (all in public schema so they are
-- accessible from every domain schema)
--
-- MUST be run BEFORE 004_create_enums.sql and all table files.
-- Safe to re-run: uses CREATE OR REPLACE.
-- ============================================================

-- Auto-update updated_at whenever a row is modified.
-- Referenced by triggers on every mutable table.
CREATE OR REPLACE FUNCTION public.update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Prevent UPDATE or DELETE on immutable tables.
-- Applied to: billing.bills, billing.bill_items,
--             khata.khata_entries, inventory.stock_movements
CREATE OR REPLACE FUNCTION public.prevent_immutable_record_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        'Records in %.% are immutable. Updates and deletes are not allowed.',
        TG_TABLE_SCHEMA, TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

-- Enforce that loose catalogue items always have 0% GST.
-- Applied to: catalogue.products (BEFORE INSERT OR UPDATE)
CREATE OR REPLACE FUNCTION public.enforce_loose_item_gst()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.is_loose = TRUE AND NEW.gst_rate != 0 THEN
        RAISE EXCEPTION
            'Loose items must have 0%% GST. Received gst_rate = %',
            NEW.gst_rate;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
