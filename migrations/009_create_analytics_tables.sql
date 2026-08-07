-- ============================================================
-- 009_create_analytics_tables.sql
-- Schema:  analytics
-- Tables:  analytics.daily_summary
-- Run AFTER 008
-- ============================================================

CREATE TABLE analytics.daily_summary (
    id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id        UUID            NOT NULL
                                    REFERENCES identity.stores(id) ON DELETE RESTRICT,
    summary_date    DATE            NOT NULL,
    bill_count      INTEGER         NOT NULL DEFAULT 0,
    total_sales     NUMERIC(12,2)   NOT NULL DEFAULT 0.00,
    total_cgst      NUMERIC(10,2)   NOT NULL DEFAULT 0.00,
    total_sgst      NUMERIC(10,2)   NOT NULL DEFAULT 0.00,
    total_tax       NUMERIC(10,2)   NOT NULL DEFAULT 0.00,
    cash_sales      NUMERIC(12,2)   NOT NULL DEFAULT 0.00,
    upi_sales       NUMERIC(12,2)   NOT NULL DEFAULT 0.00,
    card_sales      NUMERIC(12,2)   NOT NULL DEFAULT 0.00,
    credit_sales    NUMERIC(12,2)   NOT NULL DEFAULT 0.00,
    top_items       JSONB           NOT NULL DEFAULT '[]',
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    UNIQUE (store_id, summary_date)
);

CREATE UNIQUE INDEX idx_analytics_daily_summary_store_date
    ON analytics.daily_summary (store_id, summary_date);

CREATE INDEX idx_analytics_daily_summary_range
    ON analytics.daily_summary (store_id, summary_date DESC);

CREATE TRIGGER analytics_daily_summary_updated_at
    BEFORE UPDATE ON analytics.daily_summary
    FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();

ALTER TABLE analytics.daily_summary ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_full_access" ON analytics.daily_summary
    USING (TRUE) WITH CHECK (TRUE);

GRANT SELECT, INSERT, UPDATE ON analytics.daily_summary TO service_role;
