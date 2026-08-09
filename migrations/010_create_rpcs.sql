-- ============================================================
-- 010_create_rpcs.sql
-- PostgreSQL functions (RPCs) called via supabase.rpc()
-- All placed in public schema so they are callable from any schema
-- Run AFTER 009
-- ============================================================


-- -----------------------------------------------------------
-- public.upsert_workflow_state
-- Creates an UNREGISTERED workflow_state record for a new
-- Telegram user. Safe to call on every Lambda invocation —
-- does nothing if the record already exists.
-- -----------------------------------------------------------
CREATE OR REPLACE FUNCTION public.upsert_workflow_state(
    p_telegram_user_id BIGINT
)
RETURNS VOID AS $$
BEGIN
    INSERT INTO identity.workflow_state (telegram_user_id, current_state)
    VALUES (p_telegram_user_id, 'UNREGISTERED')
    ON CONFLICT (telegram_user_id) DO NOTHING;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER
   SET search_path = identity, public;


-- -----------------------------------------------------------
-- public.generate_bill_number
-- Generates a sequential bill number per store per day.
-- Format: BL-<store_number>-YYYYMMDD-NNN
--   e.g., BL-1-20260615-001  (store 1, 15 Jun 2026, bill #1 of the day)
--        BL-12-20260615-003  (store 12, 15 Jun 2026, bill #3 of the day)
-- store_number: auto-assigned integer from identity.stores.store_number
-- NNN resets to 001 each calendar day per store.
-- -----------------------------------------------------------
CREATE OR REPLACE FUNCTION public.generate_bill_number(
    p_store_id UUID
)
RETURNS TEXT AS $$
DECLARE
    v_store_num  INTEGER;
    v_date       TEXT;
    v_count      INTEGER;
BEGIN
    -- Fetch the short store number from identity.stores
    SELECT store_number
    INTO v_store_num
    FROM identity.stores
    WHERE id = p_store_id;

    IF v_store_num IS NULL THEN
        RAISE EXCEPTION 'Store not found or store_number missing for store_id %', p_store_id;
    END IF;

    v_date := TO_CHAR(NOW(), 'YYYYMMDD');

    -- Count bills for this store on today's date (resets daily)
    SELECT COUNT(*) + 1
    INTO v_count
    FROM billing.bills
    WHERE store_id = p_store_id
      AND TO_CHAR(created_at, 'YYYYMMDD') = v_date;

    RETURN 'BL-' || v_store_num::TEXT || '-' || v_date || '-' || LPAD(v_count::TEXT, 3, '0');
END;
$$ LANGUAGE plpgsql SECURITY DEFINER
   SET search_path = billing, identity, public;


-- -----------------------------------------------------------
-- public.decrement_stock
-- Atomically decrements inventory for one product.
-- Uses SELECT ... FOR UPDATE to block concurrent decrements
-- on the same inventory row.
-- Also inserts an inventory.stock_movements SALE record.
-- Returns JSONB: { new_quantity, reorder_alert }
-- -----------------------------------------------------------
CREATE OR REPLACE FUNCTION public.decrement_stock(
    p_store_id      UUID,
    p_product_id    UUID,
    p_quantity      NUMERIC,
    p_bill_id       UUID
)
RETURNS JSONB AS $$
DECLARE
    v_inv_id        UUID;
    v_current_qty   NUMERIC;
    v_reorder_lvl   NUMERIC;
    v_new_qty       NUMERIC;
BEGIN
    -- Lock the inventory row for this product to prevent concurrent oversell
    SELECT id, quantity_in_stock, reorder_level
    INTO v_inv_id, v_current_qty, v_reorder_lvl
    FROM inventory.inventory
    WHERE store_id  = p_store_id
      AND product_id = p_product_id
    FOR UPDATE;

    IF v_inv_id IS NULL THEN
        RAISE EXCEPTION
            'No inventory record found for product % in store %',
            p_product_id, p_store_id;
    END IF;

    IF v_current_qty < p_quantity THEN
        RAISE EXCEPTION
            'Insufficient stock. Available: %, Requested: %',
            v_current_qty, p_quantity;
    END IF;

    v_new_qty := v_current_qty - p_quantity;

    UPDATE inventory.inventory
    SET quantity_in_stock = v_new_qty,
        updated_at        = NOW()
    WHERE id = v_inv_id;

    -- Immutable audit record
    INSERT INTO inventory.stock_movements (
        store_id, product_id, movement_type,
        quantity_delta, reference_id, reference_type
    ) VALUES (
        p_store_id, p_product_id, 'SALE',
        -p_quantity, p_bill_id, 'BILL'
    );

    RETURN jsonb_build_object(
        'new_quantity',  v_new_qty,
        'reorder_alert', v_new_qty <= v_reorder_lvl
    );
END;
$$ LANGUAGE plpgsql SECURITY DEFINER
   SET search_path = inventory, billing, public;


-- -----------------------------------------------------------
-- public.increment_stock
-- Upserts inventory for a product (receive_stock operation).
-- Also inserts an inventory.stock_movements STOCK_IN record.
-- Returns JSONB: { new_quantity }
-- -----------------------------------------------------------
CREATE OR REPLACE FUNCTION public.increment_stock(
    p_store_id      UUID,
    p_product_id    UUID,
    p_quantity      NUMERIC,
    p_reorder_level NUMERIC DEFAULT NULL
)
RETURNS JSONB AS $$
DECLARE
    v_new_qty       NUMERIC;
    v_reorder_lvl   NUMERIC;
BEGIN
    -- Use provided reorder level or fall back to the products table default
    IF p_reorder_level IS NOT NULL THEN
        v_reorder_lvl := p_reorder_level;
    ELSE
        SELECT reorder_level INTO v_reorder_lvl
        FROM catalogue.products
        WHERE id = p_product_id;
    END IF;

    INSERT INTO inventory.inventory (
        store_id, product_id, quantity_in_stock,
        reorder_level, last_restocked_at
    ) VALUES (
        p_store_id, p_product_id, p_quantity,
        COALESCE(v_reorder_lvl, 0), NOW()
    )
    ON CONFLICT (store_id, product_id)
    DO UPDATE SET
        quantity_in_stock = inventory.inventory.quantity_in_stock + EXCLUDED.quantity_in_stock,
        last_restocked_at = NOW(),
        updated_at        = NOW()
    RETURNING quantity_in_stock INTO v_new_qty;

    -- Immutable audit record
    INSERT INTO inventory.stock_movements (
        store_id, product_id, movement_type,
        quantity_delta, reference_type
    ) VALUES (
        p_store_id, p_product_id, 'STOCK_IN',
        p_quantity, 'STOCK_IN'
    );

    RETURN jsonb_build_object('new_quantity', v_new_qty);
END;
$$ LANGUAGE plpgsql SECURITY DEFINER
   SET search_path = inventory, catalogue, public;


-- -----------------------------------------------------------
-- public.get_customer_balance
-- Returns the current khata balance for a customer.
-- Positive  = customer owes the shop.
-- Negative  = shop owes the customer (overpayment).
-- Zero      = account settled.
-- -----------------------------------------------------------
CREATE OR REPLACE FUNCTION public.get_customer_balance(
    p_store_id      UUID,
    p_customer_id   UUID
)
RETURNS NUMERIC AS $$
DECLARE
    v_balance NUMERIC;
BEGIN
    SELECT COALESCE(SUM(amount_delta), 0)
    INTO v_balance
    FROM khata.khata_entries
    WHERE store_id    = p_store_id
      AND customer_id = p_customer_id;

    RETURN v_balance;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER
   SET search_path = khata, public;
