-- ============================================================
-- 011_verify.sql
-- Run this LAST to confirm every object was created correctly.
-- Each query should return the expected number of rows.
-- If any row count is wrong, re-run the failed migration file.
-- ============================================================


-- 1. Verify all domain schemas exist (expect 6 rows)
SELECT schema_name
FROM information_schema.schemata
WHERE schema_name IN (
    'identity', 'catalogue', 'inventory',
    'billing', 'khata', 'analytics'
)
ORDER BY schema_name;


-- 2. Verify all 14 tables exist across schemas (expect 14 rows)
SELECT table_schema, table_name
FROM information_schema.tables
WHERE table_schema IN (
    'identity', 'catalogue', 'inventory',
    'billing', 'khata', 'analytics'
)
ORDER BY table_schema, table_name;
-- Expected:
--   analytics  | daily_summary
--   billing    | bill_items
--   billing    | bills
--   billing    | customers
--   billing    | draft_bill_items
--   billing    | draft_bills
--   catalogue  | products
--   identity   | registrations
--   identity   | stores
--   identity   | users
--   identity   | workflow_state
--   inventory  | inventory
--   inventory  | stock_movements
--   khata      | khata_entries


-- 3. Verify all 7 ENUMs exist (expect 7 rows)
SELECT n.nspname AS schema, t.typname AS enum_name
FROM pg_type t
JOIN pg_namespace n ON n.oid = t.typnamespace
WHERE t.typtype = 'e'
  AND n.nspname IN ('identity', 'catalogue', 'inventory', 'billing', 'khata')
ORDER BY n.nspname, t.typname;
-- Expected:
--   billing   | draft_bill_status
--   billing   | payment_mode
--   catalogue | product_unit
--   identity  | registration_status
--   identity  | user_workflow_state
--   inventory | movement_type
--   khata     | entry_type


-- 4. Verify all 5 RPCs exist (expect 5 rows)
SELECT proname
FROM pg_proc
WHERE proname IN (
    'upsert_workflow_state',
    'generate_bill_number',
    'decrement_stock',
    'increment_stock',
    'get_customer_balance'
)
ORDER BY proname;


-- 5. Verify the deferred FK from workflow_state → draft_bills was added
SELECT
    tc.table_schema,
    tc.table_name,
    kcu.column_name,
    ccu.table_schema AS ref_schema,
    ccu.table_name   AS ref_table
FROM information_schema.table_constraints AS tc
JOIN information_schema.key_column_usage AS kcu
    ON tc.constraint_name = kcu.constraint_name
    AND tc.table_schema   = kcu.table_schema
JOIN information_schema.constraint_column_usage AS ccu
    ON ccu.constraint_name = tc.constraint_name
WHERE tc.constraint_type = 'FOREIGN KEY'
  AND tc.table_schema    = 'identity'
  AND tc.table_name      = 'workflow_state'
  AND kcu.column_name    = 'active_draft_bill_id';
-- Expected: 1 row  →  identity.workflow_state → billing.draft_bills


-- 6. Quick smoke test: loose-item GST trigger
--    Uncomment and run — should RAISE EXCEPTION (that means trigger works)
--
-- DO $$
-- DECLARE v_store_id UUID := gen_random_uuid();
--         v_user_id  UUID := gen_random_uuid();
-- BEGIN
--     -- Create minimal user + store for the test
--     INSERT INTO identity.users (id, telegram_user_id) VALUES (v_user_id, 9999999999);
--     INSERT INTO identity.stores (id, owner_user_id, shop_name) VALUES (v_store_id, v_user_id, 'Test Store');
--
--     -- This should raise: "Loose items must have 0% GST"
--     INSERT INTO catalogue.products (
--         store_id, name, is_loose, unit, gst_rate, cost_price, mrp
--     ) VALUES (
--         v_store_id, 'Sugar', TRUE, 'KG', 5.00, 30, 40
--     );
-- END $$;


-- 7. Quick smoke test: immutability trigger on billing.bills
--    Uncomment AFTER inserting a real test bill
--
-- UPDATE billing.bills SET total_amount = 0 WHERE id = '<some-bill-id>';
-- ^ Should raise: "Records in billing.bills are immutable"
