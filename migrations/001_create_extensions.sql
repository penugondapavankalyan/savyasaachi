-- ============================================================
-- 001_create_extensions.sql
-- Enable required PostgreSQL extensions
-- Run this FIRST
-- ============================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";
