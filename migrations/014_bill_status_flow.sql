-- ============================================================
-- 014_bill_status_flow.sql
-- Redesigns billing.bills status lifecycle:
--
--   PENDING_PAYMENT  — bill created, stock decremented, awaiting payment
--   CONFIRMED        — payment received/recorded
--   CANCELLED        — cancelled before payment (stock restored)
--   VOID             — reversed after payment (stock + payment reversed)
--
-- Changes:
--   1. Add status column to billing.bills
--   2. Replace blanket no-update trigger with one that allows
--      only these transitions:
--        PENDING_PAYMENT → CONFIRMED
--        PENDING_PAYMENT → CANCELLED
--        CONFIRMED       → VOID
--   3. Create public.confirm_payment RPC
--   4. Create public.cancel_bill RPC  (pre-payment cancellation)
--   5. Update public.void_bill RPC    (post-payment reversal)
--      (drops and recreates from migration 013 if already applied)
--
-- NOTE: migration 013 must be dropped/superseded by this one.
-- If 013 was already applied, run this file after — it will
-- DROP and recreate the trigger and all affected RPCs.
-- ============================================================


-- -----------------------------------------------------------
-- 1. Drop ALL existing update/delete triggers on billing.bills
--    MUST happen first — before any ALTER TABLE or UPDATE,
--    otherwise the blanket immutability trigger blocks everything.
-- -----------------------------------------------------------
DROP TRIGGER IF EXISTS billing_bills_no_update        ON billing.bills;
DROP TRIGGER IF EXISTS billing_bills_no_delete        ON billing.bills;
DROP TRIGGER IF EXISTS billing_bills_allow_void_only  ON billing.bills;
DROP TRIGGER IF EXISTS billing_bills_status_transition ON billing.bills;


-- -----------------------------------------------------------
-- 2. Add status column to billing.bills
--    DEFAULT 'PENDING_PAYMENT' — new bills start here.
--    Existing rows are backfilled to 'CONFIRMED' (already paid).
-- -----------------------------------------------------------

-- Drop the column added by 013 if it exists (safe re-run)
ALTER TABLE billing.bills DROP COLUMN IF EXISTS status;

ALTER TABLE billing.bills
    ADD COLUMN status TEXT NOT NULL DEFAULT 'PENDING_PAYMENT'
    CHECK (status IN ('PENDING_PAYMENT', 'CONFIRMED', 'CANCELLED', 'VOID'));

-- Backfill existing bills to CONFIRMED (they were already paid before this migration)
UPDATE billing.bills SET status = 'CONFIRMED' WHERE status = 'PENDING_PAYMENT';

-- Now change the default so future inserts start at PENDING_PAYMENT
ALTER TABLE billing.bills ALTER COLUMN status SET DEFAULT 'PENDING_PAYMENT';


-- -----------------------------------------------------------
-- 3. Install new transition-aware trigger
--    (replaces the blanket no-update trigger dropped above)
-- -----------------------------------------------------------

CREATE OR REPLACE FUNCTION public.billing_bills_status_transition()
RETURNS TRIGGER AS $$
BEGIN
    -- Only the status column may change
    IF  NEW.id             = OLD.id
    AND NEW.store_id       = OLD.store_id
    AND NEW.bill_number    = OLD.bill_number
    AND NEW.total_amount   = OLD.total_amount
    AND NEW.payment_mode   = OLD.payment_mode
    THEN
        -- Allowed transitions
        IF (OLD.status = 'PENDING_PAYMENT' AND NEW.status IN ('CONFIRMED', 'CANCELLED'))
        OR (OLD.status = 'CONFIRMED'       AND NEW.status = 'VOID')
        THEN
            RETURN NEW;
        END IF;
    END IF;

    RAISE EXCEPTION
        'Invalid billing.bills mutation. Allowed transitions: '
        'PENDING_PAYMENT→CONFIRMED, PENDING_PAYMENT→CANCELLED, CONFIRMED→VOID. '
        'Current status: %, Attempted: %', OLD.status, NEW.status;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER billing_bills_status_transition
    BEFORE UPDATE ON billing.bills
    FOR EACH ROW EXECUTE FUNCTION public.billing_bills_status_transition();


-- -----------------------------------------------------------
-- 3. public.confirm_payment
-- Moves a PENDING_PAYMENT bill to CONFIRMED.
-- Call after cash is counted, UPI screenshot received, or
-- credit terms accepted.
-- Returns JSONB: { success, message }
-- -----------------------------------------------------------
CREATE OR REPLACE FUNCTION public.confirm_payment(
    p_bill_id UUID
)
RETURNS JSONB AS $$
DECLARE
    v_bill RECORD;
BEGIN
    SELECT id, status, bill_number
    INTO v_bill
    FROM billing.bills
    WHERE id = p_bill_id;

    IF v_bill.id IS NULL THEN
        RETURN jsonb_build_object('success', false, 'message', 'Bill not found.');
    END IF;
    IF v_bill.status != 'PENDING_PAYMENT' THEN
        RETURN jsonb_build_object('success', false,
            'message', 'Bill is already ' || v_bill.status || '. Only PENDING_PAYMENT bills can be confirmed.');
    END IF;

    UPDATE billing.bills SET status = 'CONFIRMED' WHERE id = p_bill_id;

    RETURN jsonb_build_object(
        'success', true,
        'message', 'Payment confirmed for bill ' || v_bill.bill_number || '.'
    );
END;
$$ LANGUAGE plpgsql SECURITY DEFINER
   SET search_path = billing, public;


-- -----------------------------------------------------------
-- 4. public.cancel_bill
-- Cancels a PENDING_PAYMENT bill (before payment is confirmed).
-- Restores stock and reverses any khata credit entry.
-- Returns JSONB: { success, message, items_restored }
-- -----------------------------------------------------------
CREATE OR REPLACE FUNCTION public.cancel_bill(
    p_bill_id UUID
)
RETURNS JSONB AS $$
DECLARE
    v_bill        RECORD;
    v_item        RECORD;
    v_items_count INTEGER := 0;
BEGIN
    SELECT id, store_id, status, bill_number, is_credit, customer_id
    INTO v_bill FROM billing.bills WHERE id = p_bill_id;

    IF v_bill.id IS NULL THEN
        RETURN jsonb_build_object('success', false, 'message', 'Bill not found.');
    END IF;
    IF v_bill.status != 'PENDING_PAYMENT' THEN
        RETURN jsonb_build_object('success', false,
            'message', 'Only PENDING_PAYMENT bills can be cancelled. Status is: ' || v_bill.status
                       || '. Use void_bill to reverse a CONFIRMED bill.');
    END IF;

    UPDATE billing.bills SET status = 'CANCELLED' WHERE id = p_bill_id;

    -- Restore stock
    FOR v_item IN SELECT product_id, quantity FROM billing.bill_items WHERE bill_id = p_bill_id
    LOOP
        UPDATE inventory.inventory
        SET quantity_in_stock = quantity_in_stock + v_item.quantity, updated_at = NOW()
        WHERE store_id = v_bill.store_id AND product_id = v_item.product_id;

        INSERT INTO inventory.stock_movements
            (store_id, product_id, movement_type, quantity_delta, reference_id, reference_type, notes)
        VALUES
            (v_bill.store_id, v_item.product_id, 'STOCK_IN',
             v_item.quantity, p_bill_id, 'BILL_CANCEL', 'Stock restored — bill cancelled before payment');

        v_items_count := v_items_count + 1;
    END LOOP;

    -- Reverse khata credit entry if created (credit bill cancelled before payment confirmed)
    IF v_bill.is_credit AND v_bill.customer_id IS NOT NULL THEN
        INSERT INTO khata.khata_entries
            (store_id, customer_id, entry_type, amount_delta, reference_bill_id, notes)
        SELECT store_id, customer_id, 'PAYMENT', -amount_delta, p_bill_id,
               'Khata entry reversed — bill cancelled before payment'
        FROM khata.khata_entries
        WHERE reference_bill_id = p_bill_id AND entry_type = 'CREDIT';
    END IF;

    RETURN jsonb_build_object(
        'success', true,
        'message', 'Bill ' || v_bill.bill_number || ' cancelled. Stock restored for ' || v_items_count || ' item(s).',
        'items_restored', v_items_count
    );
END;
$$ LANGUAGE plpgsql SECURITY DEFINER
   SET search_path = billing, inventory, khata, public;


-- -----------------------------------------------------------
-- 5. public.void_bill  (supersedes migration 013 version)
-- Voids a CONFIRMED bill — full reversal after payment.
-- Returns JSONB: { success, message, items_restored }
-- -----------------------------------------------------------
CREATE OR REPLACE FUNCTION public.void_bill(
    p_bill_id UUID
)
RETURNS JSONB AS $$
DECLARE
    v_bill        RECORD;
    v_item        RECORD;
    v_items_count INTEGER := 0;
BEGIN
    SELECT id, store_id, status, bill_number, is_credit, customer_id
    INTO v_bill FROM billing.bills WHERE id = p_bill_id;

    IF v_bill.id IS NULL THEN
        RETURN jsonb_build_object('success', false, 'message', 'Bill not found.');
    END IF;
    IF v_bill.status != 'CONFIRMED' THEN
        RETURN jsonb_build_object('success', false,
            'message', 'Only CONFIRMED bills can be voided. Status is: ' || v_bill.status
                       || '. Use cancel_bill for PENDING_PAYMENT bills.');
    END IF;

    UPDATE billing.bills SET status = 'VOID' WHERE id = p_bill_id;

    -- Restore stock
    FOR v_item IN SELECT product_id, quantity FROM billing.bill_items WHERE bill_id = p_bill_id
    LOOP
        UPDATE inventory.inventory
        SET quantity_in_stock = quantity_in_stock + v_item.quantity, updated_at = NOW()
        WHERE store_id = v_bill.store_id AND product_id = v_item.product_id;

        INSERT INTO inventory.stock_movements
            (store_id, product_id, movement_type, quantity_delta, reference_id, reference_type, notes)
        VALUES
            (v_bill.store_id, v_item.product_id, 'STOCK_IN',
             v_item.quantity, p_bill_id, 'BILL_VOID', 'Stock restored — bill voided after payment');

        v_items_count := v_items_count + 1;
    END LOOP;

    -- Reverse khata credit entry
    IF v_bill.is_credit AND v_bill.customer_id IS NOT NULL THEN
        INSERT INTO khata.khata_entries
            (store_id, customer_id, entry_type, amount_delta, reference_bill_id, notes)
        SELECT store_id, customer_id, 'PAYMENT', -amount_delta, p_bill_id,
               'Khata entry reversed — bill voided after payment'
        FROM khata.khata_entries
        WHERE reference_bill_id = p_bill_id AND entry_type = 'CREDIT';
    END IF;

    RETURN jsonb_build_object(
        'success', true,
        'message', 'Bill ' || v_bill.bill_number || ' voided. Stock and payment reversed for ' || v_items_count || ' item(s).',
        'items_restored', v_items_count
    );
END;
$$ LANGUAGE plpgsql SECURITY DEFINER
   SET search_path = billing, inventory, khata, public;
