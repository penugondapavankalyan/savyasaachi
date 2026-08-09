# Infrastructure: Supabase

**Type:** Managed PostgreSQL (Supabase)  
**Always On:** Yes — Supabase runs continuously regardless of Lambda activity  
**ACID:** Full PostgreSQL ACID guarantees

---

## Overview

Supabase is the primary persistent data store. It holds all financial records (bills, khata), inventory state, product catalogue, user identity, and workflow state. All data in Supabase survives Lambda restarts, `/new` chat commands, and Redis TTL expiry.

The Lambda functions connect to Supabase via the official Python client using the **service role key** (bypasses Row Level Security for server-side operations).

---

## Project Setup

1. Create a new Supabase project at [supabase.com](https://supabase.com)
2. Choose region: `ap-south-1` (Singapore or Mumbai — closest to India)
3. Note the project URL and service role key from Project Settings → API

---

## Connection Configuration

### Connection Pooling (pgBouncer)

Lambda functions create a new DB connection on each cold start. Without connection pooling, this can exhaust PostgreSQL's connection limit.

**Supabase provides pgBouncer in Transaction Mode** — use the pooler URL:

```python
# src/db/supabase_client.py
import os
from supabase import create_client, Client

def get_supabase_client() -> Client:
    return create_client(
        supabase_url=os.environ["SUPABASE_URL"],
        supabase_key=os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    )

# The Supabase Python client uses the REST API (PostgREST), not a raw TCP connection
# PostgREST handles connection pooling internally — no pgBouncer config needed for REST API
# Direct SQL (via supabase.rpc()) also uses REST
```

> **Note:** The Supabase Python client uses the **REST API** (PostgREST), not a raw `psycopg2` TCP connection. This means connection limits are not a concern for Lambda — each HTTP request to Supabase is stateless. pgBouncer is only relevant if using `psycopg2`/`asyncpg` directly.

---

## Row Level Security (RLS)

RLS is enabled on all tables. Since the Lambda uses the `service_role_key`, it bypasses RLS. RLS policies are defined for future use when direct client access is added (e.g., a Phase 2 web dashboard).

```sql
-- Pattern for all tables
ALTER TABLE public.{table_name} ENABLE ROW LEVEL SECURITY;

-- Service role full access (Lambda backend)
CREATE POLICY "service_role_full_access" ON public.{table_name}
    USING (TRUE)
    WITH CHECK (TRUE);
```

### Future RLS Policies (Phase 2)

When a web dashboard is added, store-scoped policies will be added:
```sql
-- Example: store owners can only see their own store's data
CREATE POLICY "owner_own_store" ON public.bills
    USING (store_id = (
        SELECT id FROM stores WHERE owner_user_id = auth.uid()
    ));
```

---

## Database Schema Deployment (Migrations)

All schema changes are applied via SQL migration files in the `migrations/` directory. Apply using Supabase CLI or Supabase Dashboard SQL editor.

### Migration Files (ordered)

```
migrations/
├── 001_create_extensions.sql        -- Enable pgcrypto, uuid-ossp
├── 002_create_enums.sql             -- All ENUM types
├── 003_create_utility_functions.sql -- update_updated_at_column() trigger function
├── 004_create_identity_tables.sql   -- users, stores, registrations, workflow_state
├── 005_create_catalogue_tables.sql  -- products
├── 006_create_inventory_tables.sql  -- inventory, stock_movements
├── 007_create_billing_tables.sql    -- draft_bills, draft_bill_items, bills, bill_items
├── 008_create_khata_tables.sql      -- customers, khata_entries
├── 009_create_analytics_tables.sql  -- daily_summary
├── 010_create_indexes.sql           -- All non-unique indexes
├── 011_create_rls_policies.sql      -- All RLS policies
├── 012_create_triggers.sql          -- All immutability triggers, GST trigger
├── 013_create_rpcs.sql              -- decrement_stock, generate_bill_number, upsert_workflow_state
└── 014_seed_data.sql                -- (Optional) sample products for testing
```

Apply via Supabase CLI:
```bash
supabase db push --db-url "postgresql://postgres:[password]@db.[project-ref].supabase.co:5432/postgres"
```

---

## Critical Supabase RPCs

These are PostgreSQL functions called via `supabase.rpc()` from Python. They run inside Supabase and handle atomic operations.

### `decrement_stock`
```sql
-- Defined in docs/database/inventory/inventory.md
-- Atomically decrements stock with row-level locking
-- Returns new_quantity and reorder_alert flag
SELECT public.decrement_stock(
    p_store_id => ?,
    p_product_id => ?,
    p_quantity => ?,
    p_bill_id => ?
);
```

### `generate_bill_number`
```sql
-- Defined in docs/database/billing/bills.md
-- Generates sequential bill number like BL-2024-001
SELECT public.generate_bill_number(p_store_id => ?);
```

### `upsert_workflow_state`
```sql
-- Creates UNREGISTERED record for new users (idempotent)
CREATE OR REPLACE FUNCTION upsert_workflow_state(p_telegram_user_id BIGINT)
RETURNS VOID AS $$
BEGIN
    INSERT INTO public.workflow_state (telegram_user_id, current_state)
    VALUES (p_telegram_user_id, 'UNREGISTERED')
    ON CONFLICT (telegram_user_id) DO NOTHING;
END;
$$ LANGUAGE plpgsql;
```

---

## ACID Transaction Usage

The most critical transactions in the system:

### 1. Bill Finalization (Billing MCP)
```sql
BEGIN;
  SELECT ... FROM draft_bills WHERE id = ? FOR UPDATE;
  INSERT INTO bills (...) ON CONFLICT (workflow_id) DO NOTHING;
  INSERT INTO bill_items (...);
  -- decrement_stock RPC called per item (each internally atomic)
  UPDATE draft_bills SET status = 'CONFIRMED';
  UPDATE workflow_state SET active_draft_bill_id = NULL;
COMMIT;
```

### 2. Registration (Identity MCP)
```sql
BEGIN;
  INSERT INTO users (...) ON CONFLICT DO UPDATE;
  INSERT INTO stores (...);
  UPDATE registrations SET status = 'COMPLETE', store_id = ?, completed_at = NOW();
  UPDATE workflow_state SET current_state = 'PENDING_CATALOGUE', store_id = ?;
COMMIT;
```

### 3. Stock Receipt (Inventory MCP)
```sql
BEGIN;
  INSERT INTO inventory (...) ON CONFLICT DO UPDATE SET quantity = quantity + ?;
  INSERT INTO stock_movements (...);
COMMIT;
```

---

## Supabase Free Tier

| Resource | Free Tier | Notes |
|---|---|---|
| Database size | 500MB | Ample for Phase 1 single store |
| REST API requests | Unlimited | Used by Python client |
| Realtime connections | 200 concurrent | Not used in Phase 1 |
| Edge Functions | 500K invocations/month | Not used in Phase 1 |
| Storage | 1GB | Not used (files go via Telegram) |

---

## Backup Strategy

Supabase Pro plan includes daily backups. On the free plan:
- Use Supabase Dashboard → Database → Backups for manual backups
- Or export via `pg_dump` periodically

---

## Phase 2 Extensibility

| Feature | Migration |
|---|---|
| Multi-store per user | `ALTER TABLE stores DROP CONSTRAINT stores_owner_user_id_unique` |
| Multi-user per store | Add `store_users` table with role enum |
| Supabase Auth | Enable Supabase Auth for web dashboard users |
| Realtime | Enable Realtime on `workflow_state` for live dashboard updates |
| Read replicas | Available on Supabase Pro plan for analytics queries |
