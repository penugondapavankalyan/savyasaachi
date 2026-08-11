-- ============================================================
-- 017_fix_stock_movements_reference_type_check.sql
--
-- The cancel_bill and void_bill RPCs (migration 014) insert
-- reference_type values 'BILL_CANCEL' and 'BILL_VOID' but the
-- original CHECK constraint on inventory.stock_movements
-- (migration 006) only allows: 'BILL', 'STOCK_IN', 'ADJUSTMENT'.
--
-- This migration widens the constraint to include the two new
-- values. No data changes — constraint only.
-- Run AFTER 016
-- ============================================================

ALTER TABLE inventory.stock_movements
    DROP CONSTRAINT IF EXISTS stock_movements_reference_type_check;

ALTER TABLE inventory.stock_movements
    ADD CONSTRAINT stock_movements_reference_type_check
    CHECK (reference_type IN (
        'BILL',        -- sale recorded via decrement_stock / finalize_bill
        'STOCK_IN',    -- stock received via increment_stock / receive_stock
        'ADJUSTMENT',  -- manual stock adjustment
        'BILL_CANCEL', -- stock restored via cancel_bill (pre-payment cancellation)
        'BILL_VOID'    -- stock restored via void_bill (post-payment reversal)
    ));
