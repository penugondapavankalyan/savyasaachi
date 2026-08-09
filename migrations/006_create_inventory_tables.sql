-- ============================================================
-- 006_create_inventory_tables.sql
-- Schema:  inventory
-- Tables:  inventory.inventory
--          inventory.stock_movements
-- Run AFTER 005
-- ============================================================


-- -----------------------------------------------------------
-- inventory.inventory
-- Live stock levels per product per store.
-- quantity_in_stock can never go below 0 (CHECK constraint).
-- Atomic decrements done via the decrement_stock() RPC.
-- -----------------------------------------------------------
CREATE TABLE inventory.inventory (
    id                  UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id            UUID            NOT NULL
                                        REFERENCES identity.stores(id) ON DELETE RESTRICT,
    product_id          UUID            NOT NULL
                                        REFERENCES catalogue.products(id) ON DELETE RESTRICT,
    quantity_in_stock   NUMERIC(10,3)   NOT NULL DEFAULT 0
                                        CHECK (quantity_in_stock >= 0),
    reorder_level       NUMERIC(10,3)   NOT NULL DEFAULT 0
                                        CHECK (reorder_level >= 0),
    last_restocked_at   TIMESTAMPTZ,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    UNIQUE (store_id, product_id)
);

CREATE INDEX idx_inventory_low_stock
    ON inventory.inventory (store_id, quantity_in_stock, reorder_level);

CREATE TRIGGER inventory_inventory_updated_at
    BEFORE UPDATE ON inventory.inventory
    FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();

ALTER TABLE inventory.inventory ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_full_access" ON inventory.inventory
    USING (TRUE) WITH CHECK (TRUE);

GRANT SELECT, INSERT, UPDATE ON inventory.inventory TO service_role;


-- -----------------------------------------------------------
-- inventory.stock_movements
-- Immutable audit trail of every stock change.
-- Never updated or deleted after INSERT.
-- quantity_delta: positive = stock in, negative = sale.
-- -----------------------------------------------------------
CREATE TABLE inventory.stock_movements (
    id              UUID                        PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id        UUID                        NOT NULL
                                                REFERENCES identity.stores(id) ON DELETE RESTRICT,
    product_id      UUID                        NOT NULL
                                                REFERENCES catalogue.products(id) ON DELETE RESTRICT,
    movement_type   inventory.movement_type     NOT NULL,
    quantity_delta  NUMERIC(10,3)               NOT NULL,
    reference_id    UUID,
    reference_type  TEXT
                                                CHECK (reference_type IN ('BILL', 'STOCK_IN', 'ADJUSTMENT')),
    notes           TEXT,
    created_at      TIMESTAMPTZ                 NOT NULL DEFAULT NOW()
    -- No updated_at — immutable
);

CREATE INDEX idx_inventory_movements_store_product
    ON inventory.stock_movements (store_id, product_id);

CREATE INDEX idx_inventory_movements_created_at
    ON inventory.stock_movements (store_id, created_at DESC);

CREATE INDEX idx_inventory_movements_reference
    ON inventory.stock_movements (reference_id) WHERE reference_id IS NOT NULL;

CREATE INDEX idx_inventory_movements_type
    ON inventory.stock_movements (store_id, movement_type, created_at DESC);

-- Immutability enforcement
CREATE TRIGGER inventory_stock_movements_no_update
    BEFORE UPDATE ON inventory.stock_movements
    FOR EACH ROW EXECUTE FUNCTION public.prevent_immutable_record_mutation();

CREATE TRIGGER inventory_stock_movements_no_delete
    BEFORE DELETE ON inventory.stock_movements
    FOR EACH ROW EXECUTE FUNCTION public.prevent_immutable_record_mutation();

ALTER TABLE inventory.stock_movements ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_full_access" ON inventory.stock_movements
    USING (TRUE) WITH CHECK (TRUE);

GRANT SELECT, INSERT ON inventory.stock_movements TO service_role;
