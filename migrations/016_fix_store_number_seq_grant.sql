-- ============================================================
-- 016_fix_store_number_seq_grant.sql
-- Grants USAGE and SELECT on the store_number sequence to
-- service_role so that INSERT INTO identity.stores can call
-- nextval() for the SERIAL store_number column.
--
-- Root cause: 012_add_store_number.sql added the SERIAL column
-- (which creates identity.stores_store_number_seq) but only
-- granted SELECT on the column, not on the sequence itself.
-- PostgreSQL requires explicit sequence grants for INSERT to work.
--
-- Run AFTER 012
-- Safe to run on a live database.
-- ============================================================

GRANT USAGE, SELECT ON SEQUENCE identity.stores_store_number_seq TO service_role, anon, authenticated;

-- Ensure default privileges on future sequences in the identity schema
ALTER DEFAULT PRIVILEGES IN SCHEMA identity GRANT USAGE, SELECT ON SEQUENCES TO service_role, anon, authenticated;
