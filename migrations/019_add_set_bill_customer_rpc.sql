-- ============================================================
-- 019_add_set_bill_customer_rpc.sql
--
-- Problem: cash/UPI bills created without a customer_id because
-- the payer is unknown at finalize time. When the owner later
-- identifies the customer (underpayment khata entry, or overpayment
-- surplus added to khata) the bill row has no customer linkage.
--
-- Solution:
--   1. Extend the billing_bills_status_transition trigger to also
--      allow setting customer_id from NULL → a valid UUID (one-way,
--      only when it is currently NULL).
--   2. Add a public.set_bill_customer RPC that performs this update.
--
-- Run AFTER 018
-- ============================================================


-- -----------------------------------------------------------
-- 1. Replace the status-transition trigger function to also
--    permit the customer_id NULL → UUID assignment.
-- -----------------------------------------------------------
CREATE OR REPLACE FUNCTION public.billing_bills_status_transition()
RETURNS TRIGGER AS $$
BEGIN
    -- CASE A: pure status transition (no other columns change)
    IF  NEW.id           = OLD.id
    AND NEW.store_id     = OLD.store_id
    AND NEW.bill_number  = OLD.bill_number
    AND NEW.total_amount = OLD.total_amount
    AND NEW.payment_mode = OLD.payment_mode
    AND NEW.customer_id  IS NOT DISTINCT FROM OLD.customer_id
    THEN
        IF (OLD.status = 'PENDING_PAYMENT' AND NEW.status IN ('CONFIRMED', 'CANCELLED'))
        OR (OLD.status = 'CONFIRMED'       AND NEW.status = 'VOID')
        THEN
            RETURN NEW;
        END IF;
    END IF;

    -- CASE B: customer_id assignment (NULL → UUID, status must stay the same)
    IF  NEW.id           = OLD.id
    AND NEW.store_id     = OLD.store_id
    AND NEW.bill_number  = OLD.bill_number
    AND NEW.total_amount = OLD.total_amount
    AND NEW.payment_mode = OLD.payment_mode
    AND NEW.status       = OLD.status
    AND OLD.customer_id  IS NULL
    AND NEW.customer_id  IS NOT NULL
    THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION
        'Invalid billing.bills mutation. Allowed: status transitions '
        '(PENDING_PAYMENT→CONFIRMED/CANCELLED, CONFIRMED→VOID) or '
        'setting customer_id when currently NULL. '
        'Current status: %, customer_id: %', OLD.status, OLD.customer_id;
END;
$$ LANGUAGE plpgsql;


-- -----------------------------------------------------------
-- 2. RPC: set_bill_customer
--    Sets customer_id on a bill that currently has none.
--    Idempotent: if customer_id is already set, returns success
--    without touching the row.
--    Returns JSONB { success, message }
-- -----------------------------------------------------------
CREATE OR REPLACE FUNCTION public.set_bill_customer(
    p_bill_id     UUID,
    p_customer_id UUID
)
RETURNS JSONB AS $$
DECLARE
    v_bill RECORD;
BEGIN
    SELECT id, bill_number, customer_id
    INTO v_bill
    FROM billing.bills
    WHERE id = p_bill_id;

    IF v_bill.id IS NULL THEN
        RETURN jsonb_build_object('success', false, 'message', 'Bill not found.');
    END IF;

    IF v_bill.customer_id IS NOT NULL THEN
        -- Already linked — idempotent, no-op
        RETURN jsonb_build_object(
            'success', true,
            'message', 'Bill ' || v_bill.bill_number || ' already linked to a customer.'
        );
    END IF;

    UPDATE billing.bills
    SET customer_id = p_customer_id
    WHERE id = p_bill_id;

    RETURN jsonb_build_object(
        'success', true,
        'message', 'Bill ' || v_bill.bill_number || ' linked to customer.'
    );
END;
$$ LANGUAGE plpgsql SECURITY DEFINER
   SET search_path = billing, public;
