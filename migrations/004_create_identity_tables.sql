-- ============================================================
-- 005_create_identity_tables.sql
-- Schema:  identity
-- Tables:  identity.users
--          identity.stores
--          identity.registrations
--          identity.workflow_state
--
-- NOTE: identity.workflow_state.active_draft_bill_id FK
--       is added in 008 after billing.draft_bills exists.
-- Run AFTER 004
-- ============================================================


-- -----------------------------------------------------------
-- identity.users
-- Root identity table. One record per Telegram account.
-- -----------------------------------------------------------
CREATE TABLE identity.users (
    id                  UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_user_id    BIGINT          NOT NULL UNIQUE,
    telegram_username   TEXT,
    first_name          TEXT,
    last_name           TEXT,
    is_active           BOOLEAN         NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_identity_users_telegram_id
    ON identity.users (telegram_user_id);

CREATE INDEX idx_identity_users_active
    ON identity.users (is_active) WHERE is_active = TRUE;

CREATE TRIGGER identity_users_updated_at
    BEFORE UPDATE ON identity.users
    FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();

ALTER TABLE identity.users ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_full_access" ON identity.users
    USING (TRUE) WITH CHECK (TRUE);

GRANT SELECT, INSERT, UPDATE ON identity.users TO service_role;


-- -----------------------------------------------------------
-- identity.stores
-- Shop profile, preferences, default payment mode.
-- Phase 1: one store per user (enforced by UNIQUE on owner_user_id).
-- -----------------------------------------------------------
CREATE TABLE identity.stores (
    id                      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id           UUID        NOT NULL UNIQUE
                                        REFERENCES identity.users(id) ON DELETE RESTRICT,
    shop_name               TEXT        NOT NULL,
    gstin                   TEXT
                                        CHECK (
                                            gstin IS NULL
                                            OR gstin ~ '^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$'
                                        ),
    state_code              TEXT        NOT NULL DEFAULT '29',
    address                 TEXT,
    phone                   TEXT,
    default_payment_mode    TEXT        NOT NULL DEFAULT 'CASH'
                                        CHECK (default_payment_mode IN ('CASH', 'UPI', 'CARD')),
    preferences             JSONB       NOT NULL DEFAULT '{}',
    is_active               BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_identity_stores_owner
    ON identity.stores (owner_user_id);

CREATE INDEX idx_identity_stores_active
    ON identity.stores (is_active) WHERE is_active = TRUE;

CREATE INDEX idx_identity_stores_preferences
    ON identity.stores USING GIN (preferences);

CREATE TRIGGER identity_stores_updated_at
    BEFORE UPDATE ON identity.stores
    FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();

ALTER TABLE identity.stores ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_full_access" ON identity.stores
    USING (TRUE) WITH CHECK (TRUE);

GRANT SELECT, INSERT, UPDATE ON identity.stores TO service_role;


-- -----------------------------------------------------------
-- identity.registrations
-- Tracks onboarding progress for each Telegram user.
-- Resumable: if user drops off, picks up from current status.
-- -----------------------------------------------------------
CREATE TABLE identity.registrations (
    id                  UUID                        PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_user_id    BIGINT                      NOT NULL UNIQUE,
    user_id             UUID                        NOT NULL
                                                    REFERENCES identity.users(id) ON DELETE RESTRICT,
    store_id            UUID
                                                    REFERENCES identity.stores(id) ON DELETE RESTRICT,
    status              identity.registration_status NOT NULL DEFAULT 'INITIATED',
    initiated_at        TIMESTAMPTZ                 NOT NULL DEFAULT NOW(),
    store_created_at    TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    failure_reason      TEXT,
    created_at          TIMESTAMPTZ                 NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ                 NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_identity_registrations_telegram_id
    ON identity.registrations (telegram_user_id);

CREATE INDEX idx_identity_registrations_user_id
    ON identity.registrations (user_id);

CREATE INDEX idx_identity_registrations_status
    ON identity.registrations (status);

CREATE TRIGGER identity_registrations_updated_at
    BEFORE UPDATE ON identity.registrations
    FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();

ALTER TABLE identity.registrations ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_full_access" ON identity.registrations
    USING (TRUE) WITH CHECK (TRUE);

GRANT SELECT, INSERT, UPDATE ON identity.registrations TO service_role;


-- -----------------------------------------------------------
-- identity.workflow_state
-- Tracks the onboarding/operational state per Telegram user.
-- Read by Lambda handler BEFORE the agent is invoked.
-- active_draft_bill_id FK added later in 008.
-- -----------------------------------------------------------
CREATE TABLE identity.workflow_state (
    id                      UUID                        PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_user_id        BIGINT                      NOT NULL UNIQUE,
    user_id                 UUID
                                                        REFERENCES identity.users(id) ON DELETE RESTRICT,
    store_id                UUID
                                                        REFERENCES identity.stores(id) ON DELETE RESTRICT,
    current_state           identity.user_workflow_state NOT NULL DEFAULT 'UNREGISTERED',
    active_draft_bill_id    UUID,   -- FK to billing.draft_bills added in migration 008
    updated_at              TIMESTAMPTZ                 NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX idx_identity_workflow_state_telegram_id
    ON identity.workflow_state (telegram_user_id);

CREATE TRIGGER identity_workflow_state_updated_at
    BEFORE UPDATE ON identity.workflow_state
    FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();

ALTER TABLE identity.workflow_state ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_full_access" ON identity.workflow_state
    USING (TRUE) WITH CHECK (TRUE);

GRANT SELECT, INSERT, UPDATE ON identity.workflow_state TO service_role;
