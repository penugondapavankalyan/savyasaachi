-- ============================================================
-- 015_fix_bill_number_date.sql
-- Fix generate_bill_number date computation.
--
-- Problem: TO_CHAR(NOW(), 'YYYYMMDD') and TO_CHAR(created_at, 'YYYYMMDD')
-- both use the DB session timezone.  Supabase projects default to UTC.
-- For stores in India (IST = UTC+5:30), a bill created at 00:30 IST is
-- still the previous day in UTC — so the count query matches the wrong day
-- and the date in the bill number never advances at midnight IST.
--
-- Fix: use (NOW() AT TIME ZONE 'Asia/Kolkata')::DATE for both sides of
-- the comparison.  This makes the bill number date match the India
-- calendar day regardless of DB server timezone.
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
    -- Cast created_at to IST date before comparing so the count is consistent
    -- with the date we just computed above.
    SELECT COUNT(*) + 1
    INTO v_count
    FROM billing.bills
    WHERE store_id = p_store_id
      AND (created_at AT TIME ZONE 'Asia/Kolkata')::DATE = v_today;

    RETURN 'BL-' || v_store_num::TEXT || '-' || v_date || '-' || LPAD(v_count::TEXT, 3, '0');
END;
$$ LANGUAGE plpgsql SECURITY DEFINER
   SET search_path = billing, identity, public;
