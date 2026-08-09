-- ============================================================
-- 005_create_catalogue_tables.sql
-- Schema:  catalogue
-- Tables:  catalogue.products
-- Run AFTER 004
-- ============================================================

CREATE TABLE catalogue.products (
    id              UUID                    PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id        UUID                    NOT NULL
                                            REFERENCES identity.stores(id) ON DELETE RESTRICT,
    name            TEXT                    NOT NULL,
    brand           TEXT,
    is_loose        BOOLEAN                 NOT NULL DEFAULT FALSE,
    unit            catalogue.product_unit  NOT NULL,
    hsn_code        TEXT,
    gst_rate        NUMERIC(5,2)            NOT NULL DEFAULT 0.00
                                            CHECK (gst_rate >= 0 AND gst_rate <= 28),
    cost_price      NUMERIC(10,2)           NOT NULL CHECK (cost_price >= 0),
    mrp             NUMERIC(10,2)           NOT NULL CHECK (mrp >= 0),
    reorder_level   NUMERIC(10,3)           NOT NULL DEFAULT 0 CHECK (reorder_level >= 0),
    is_active       BOOLEAN                 NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ             NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ             NOT NULL DEFAULT NOW()
);

-- Unique SKU per store: same name+brand cannot appear twice (case-insensitive)
CREATE UNIQUE INDEX idx_catalogue_products_store_name_brand
    ON catalogue.products (store_id, LOWER(name), COALESCE(LOWER(brand), ''));

-- Full-text search for agent product lookup ("atta", "maggi", etc.)
CREATE INDEX idx_catalogue_products_search
    ON catalogue.products
    USING GIN (to_tsvector('english', name || ' ' || COALESCE(brand, '')));

CREATE INDEX idx_catalogue_products_store_id
    ON catalogue.products (store_id);

CREATE INDEX idx_catalogue_products_active
    ON catalogue.products (store_id, is_active) WHERE is_active = TRUE;

CREATE TRIGGER catalogue_products_updated_at
    BEFORE UPDATE ON catalogue.products
    FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();

-- Loose items must always have 0% GST
CREATE TRIGGER catalogue_products_loose_gst
    BEFORE INSERT OR UPDATE ON catalogue.products
    FOR EACH ROW EXECUTE FUNCTION public.enforce_loose_item_gst();

ALTER TABLE catalogue.products ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_full_access" ON catalogue.products
    USING (TRUE) WITH CHECK (TRUE);

GRANT SELECT, INSERT, UPDATE ON catalogue.products TO service_role;
