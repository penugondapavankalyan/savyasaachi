-- ============================================================
-- 012_add_store_number.sql
-- Adds an auto-incrementing store_number to identity.stores.
-- Used in bill number format: BL-<store_number>-YYYYMMDD-NNN
--
-- Run AFTER 011
-- Safe to run on a live database with existing stores:
--   SERIAL back-fills numbers for all existing rows automatically.
-- ============================================================

-- Add the sequence-backed column.
-- SERIAL is shorthand for:
--   CREATE SEQUENCE identity.stores_store_number_seq;
--   ADD COLUMN store_number INTEGER NOT NULL DEFAULT nextval(...)
-- Numbers are assigned in insertion order (existing rows get 1, 2, 3…).
ALTER TABLE identity.stores
    ADD COLUMN store_number SERIAL NOT NULL;

-- Enforce uniqueness — two stores must never share a number.
ALTER TABLE identity.stores
    ADD CONSTRAINT stores_store_number_unique UNIQUE (store_number);

-- Grant read access so the generate_bill_number RPC (SECURITY DEFINER)
-- can cross-schema JOIN into identity.stores.
GRANT SELECT (store_number) ON identity.stores TO service_role;
