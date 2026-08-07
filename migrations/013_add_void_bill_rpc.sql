-- ============================================================
-- 013_add_void_bill_rpc.sql
-- Adds the ability to void a finalized bill and restore stock.
--
-- Changes:
--   1. Add status column to billing.bills (default 'CONFIRMED')
--   2. Replace blanket immutability trigger with one that only
--      permits the CONFIRMED → VOID status transition
--   3. Create public.void_bill RPC
--
-- Run AFTER 012
-- ============================================================


-- -----------------------------------------------------------
-- 1. Add status column to billing.bills
-- -----------------------------------------------------------
ALTER TABLE billing.bills
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'CONFIRMED'
    CHECK (status IN ('CONFIRMED', 'VOID'));


-- -----------------------------------------------------------
-- 2. Replace the blanket no-update trigger with one that
--    permits only the CONFIRMED → VOID status transition.
-- -----------------------------------------------------------
DROP TRIGGER IF EXISTS billing_bills_no_update ON billing.bills;

CREATE OR REPLACE FUNCTION public.billing_bills_allow_void_only()
RETURNS TRIGGER AS $$
BEGIN
    -- Only permitted mutation: CONFIRMED → VOID status change
    IF OLD.status = 'CONFIRMED' AND NEW.status = 'VOID' THEN
        -- Allow only the status field to change, nothing else
        IF NEW.id            = OLD.id
           AND NEW.store_id       = OLD.store_id
           AND NEW.bill_number    = OLD.bill_number
           AND NEW.total_amount   = OLD.total_amount
           AND NEW.payment_mode   = OLD.payment_mode
        THEN
            RETURN NEW;
        END IF;
    END IF;

    RAISE EXCEPTION
        'Records in billing.bills are immutable except for CONFIRMED → VOID status transition.';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER billing_bills_allow_void_only
    BEFORE UPDATE ON billing.bills
    FOR EACH ROW EXECUTE FUNCTION public.billing_bills_allow_void_only();


-- -----------------------------------------------------------
-- 3. public.void_bill
-- Voids a CONFIRMED bill, restores stock, reverses khata.
-- Returns JSONB: { success, message, items_restored }
-- -----------------------------------------------------------
CREATE OR REPLACE FUNCTION public.void_bill(
    p_bill_id UUID
)
RETURNS JSONB AS $$
DECLARE
    v_bill          RECORD;
    v_item          RECORD;
    v_items_count   INTEGER := 0;
BEGIN
    -- Load the bill
    SELECT id, store_id, status, is_credit, customer_id
    INTO v_bill
    FROM billing.bills
    WHERE id = p_bill_id;

    IF v_bill.id IS NULL THEN
        RETURN jsonb_build_object('success', false, 'message', 'Bill not found.');
    END IF;

    IF v_bill.status = 'VOID' THEN
        RETURN jsonb_build_object('success', false, 'message', 'Bill is already voided.');
    END IF;

    -- Mark bill as VOID (trigger allows this transition)
    UPDATE billing.bills
    SET status = 'VOID'
    WHERE id = p_bill_id;

    -- Restore stock for each item
    FOR v_item IN
        SELECT product_id, quantity
        FROM billing.bill_items
        WHERE bill_id = p_bill_id
    LOOP
        UPDATE inventory.inventory
        SET quantity_in_stock = quantity_in_stock + v_item.quantity,
            updated_at        = NOW()
        WHERE store_id   = v_bill.store_id
          AND product_id = v_item.product_id;

        -- Audit trail
        INSERT INTO inventory.stock_movements (
            store_id, product_id, movement_type,
            quantity_delta, reference_id, reference_type, notes
        ) VALUES (
            v_bill.store_id, v_item.product_id, 'STOCK_IN',
            v_item.quantity, p_bill_id, 'BILL_VOID',
            'Stock restored — bill voided'
        );

        v_items_count := v_items_count + 1;
    END LOOP;

    -- Reverse khata credit entry if this was a credit bill
    IF v_bill.is_credit AND v_bill.customer_id IS NOT NULL THEN
        INSERT INTO khata.khata_entries (
            store_id, customer_id, entry_type, amount_delta,
            reference_bill_id, notes
        )
        SELECT
            store_id,
            customer_id,
            'PAYMENT',
            -amount_delta,
            p_bill_id,
            'Khata entry reversed — bill voided'
        FROM khata.khata_entries
        WHERE reference_bill_id = p_bill_id
          AND entry_type = 'CREDIT';
    END IF;

    RETURN jsonb_build_object(
        'success',        true,
        'message',        'Bill voided and stock restored for ' || v_items_count || ' item(s).',
        'items_restored', v_items_count
    );
END;
$$ LANGUAGE plpgsql SECURITY DEFINER
   SET search_path = billing, inventory, khata, public;
