-- ============================================================
-- 018_fix_bill_number_store_padding.sql
-- Zero-pad the store_number segment in bill numbers to 3 digits.
--
-- Problem: generate_bill_number emits BL-3-YYYYMMDD-NNN instead of
-- BL-003-YYYYMMDD-NNN because v_store_num::TEXT casts the integer
-- without padding.
--
-- Fix: wrap the store_number cast with LPAD(..., 3, '0') so all three
-- segments use consistent 3-digit zero-padded formatting.
-- ============================================================

CREATE OR REPLACE FUNCTION public.generate_bill_number(
    p_store_id UUID
)
RETURNS TEXT AS $$
DECLARE
    v_store_num  INTEGER;
    v_date       TEXT;
    v_today      DATE;
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

    -- Use IST (Asia/Kolkata) for the date so the bill number date matches
    -- the calendar day visible to the store owner, not the UTC server day.
    v_today := (NOW() AT TIME ZONE 'Asia/Kolkata')::DATE;
    v_date  := TO_CHAR(v_today, 'YYYYMMDD');

    -- Count bills for this store on today's IST date (resets at midnight IST).
    SELECT COUNT(*) + 1
    INTO v_count
    FROM billing.bills
    WHERE store_id = p_store_id
      AND (created_at AT TIME ZONE 'Asia/Kolkata')::DATE = v_today;

    RETURN 'BL-' || LPAD(v_store_num::TEXT, 3, '0') || '-' || v_date || '-' || LPAD(v_count::TEXT, 3, '0');
END;
$$ LANGUAGE plpgsql SECURITY DEFINER
   SET search_path = billing, identity, public;
