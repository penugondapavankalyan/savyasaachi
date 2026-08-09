-- ============================================================
-- 002_create_schemas.sql
-- Create all domain schemas
-- Run AFTER 001
-- ============================================================

CREATE SCHEMA IF NOT EXISTS identity;
CREATE SCHEMA IF NOT EXISTS catalogue;
CREATE SCHEMA IF NOT EXISTS inventory;
CREATE SCHEMA IF NOT EXISTS billing;
CREATE SCHEMA IF NOT EXISTS khata;
CREATE SCHEMA IF NOT EXISTS analytics;

-- Grant usage to the authenticated and anon roles
-- (Supabase uses these internally; service_role already has full access)
GRANT USAGE ON SCHEMA identity   TO authenticated, anon, service_role;
GRANT USAGE ON SCHEMA catalogue  TO authenticated, anon, service_role;
GRANT USAGE ON SCHEMA inventory  TO authenticated, anon, service_role;
GRANT USAGE ON SCHEMA billing    TO authenticated, anon, service_role;
GRANT USAGE ON SCHEMA khata      TO authenticated, anon, service_role;
GRANT USAGE ON SCHEMA analytics  TO authenticated, anon, service_role;
