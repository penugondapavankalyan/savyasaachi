-- ============================================================
-- 003_create_enums.sql
-- All ENUM types, placed in their owning domain schema
-- Run AFTER 002
--
-- Safe to re-run: uses CREATE TYPE IF NOT EXISTS
-- ============================================================

-- identity schema enums
CREATE TYPE IF NOT EXISTS identity.registration_status AS ENUM (
    'INITIATED',
    'STORE_CREATED',
    'COMPLETE'
);

CREATE TYPE IF NOT EXISTS identity.user_workflow_state AS ENUM (
    'UNREGISTERED',
    'PENDING_CATALOGUE',
    'PENDING_INVENTORY',
    'ACTIVE'
);

-- catalogue schema enums
CREATE TYPE IF NOT EXISTS catalogue.product_unit AS ENUM (
    'KG',
    'G',
    'L',
    'ML',
    'PACKET',
    'PIECE',
    'DOZEN',
    'BUNDLE'
);

-- inventory schema enums
CREATE TYPE IF NOT EXISTS inventory.movement_type AS ENUM (
    'STOCK_IN',
    'SALE',
    'ADJUSTMENT'
);

-- billing schema enums
CREATE TYPE IF NOT EXISTS billing.draft_bill_status AS ENUM (
    'OPEN',
    'CONFIRMED',
    'CANCELLED',
    'EXPIRED'
);

CREATE TYPE IF NOT EXISTS billing.payment_mode AS ENUM (
    'CASH',
    'UPI',
    'CARD',
    'CREDIT'
);

-- khata schema enums
CREATE TYPE IF NOT EXISTS khata.entry_type AS ENUM (
    'CREDIT',
    'PAYMENT',
    'ADJUSTMENT'
);
