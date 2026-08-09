# Database Table: `users`

**Domain:** Identity  
**MCP Owner:** Identity MCP  
**Schema:** `identity`

---

## Purpose

The `users` table stores the identity of every Telegram user who interacts with the Kirana Agent. It is the root anchor for all other entities in the system. Every store, bill, draft bill, khata entry, and workflow state traces back to a record in this table.

In Phase 1, one Telegram user maps to exactly one store. The unique constraint on `telegram_user_id` enforces this.

---

## Schema

```sql
CREATE TABLE public.users (
    id                  UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_user_id    BIGINT          NOT NULL UNIQUE,
    telegram_username   TEXT,
    first_name          TEXT,
    last_name           TEXT,
    is_active           BOOLEAN         NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);
```

### Column Definitions

| Column | Type | Nullable | Default | Description |
|---|---|---|---|---|
| `id` | UUID | No | `gen_random_uuid()` | Internal primary key. Used for all FK relationships. |
| `telegram_user_id` | BIGINT | No | — | Telegram's numeric user ID. Unique. Used as the external identifier for all lookups from the Lambda handler. |
| `telegram_username` | TEXT | Yes | — | Telegram @username (without @). Optional — not all Telegram users have a username. |
| `first_name` | TEXT | Yes | — | Telegram first name. Stored for display purposes in messages. |
| `last_name` | TEXT | Yes | — | Telegram last name. Optional. |
| `is_active` | BOOLEAN | No | `TRUE` | Soft-disable flag. Never hard-delete users. |
| `created_at` | TIMESTAMPTZ | No | `NOW()` | Record creation timestamp. Immutable after insert. |
| `updated_at` | TIMESTAMPTZ | No | `NOW()` | Updated via trigger on any column change. |

---

## Constraints

```sql
-- Primary key
ALTER TABLE public.users ADD CONSTRAINT users_pkey PRIMARY KEY (id);

-- Telegram user ID must be globally unique — one Telegram account maps to one user record
ALTER TABLE public.users ADD CONSTRAINT users_telegram_user_id_unique UNIQUE (telegram_user_id);

-- updated_at auto-update trigger
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER users_updated_at
    BEFORE UPDATE ON public.users
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
```

---

## Indexes

```sql
-- Primary lookup: find user by Telegram user ID (every request starts here)
CREATE INDEX idx_users_telegram_user_id ON public.users (telegram_user_id);

-- Active users filter (for admin queries)
CREATE INDEX idx_users_is_active ON public.users (is_active) WHERE is_active = TRUE;
```

---

## Row Level Security (RLS)

```sql
ALTER TABLE public.users ENABLE ROW LEVEL SECURITY;

-- Service role (Lambda backend) has full access
-- No direct client-side access to this table
-- All access goes through the Identity MCP which uses the service role key
CREATE POLICY "service_role_full_access" ON public.users
    USING (TRUE)
    WITH CHECK (TRUE);
```

> **Note:** The Lambda functions use `SUPABASE_SERVICE_ROLE_KEY` which bypasses RLS. RLS policies here are in place for future direct client access or Supabase Studio access restrictions.

---

## Relations

### Outgoing (this table references)
None — `users` is a root table.

### Incoming (other tables reference `users`)

| Table | Foreign Key Column | Relationship | Note |
|---|---|---|---|
| `stores` | `owner_user_id` | 1:1 (Phase 1), 1:N (Phase 2) | A user owns a store |
| `registrations` | `telegram_user_id` | 1:1 (Phase 1) | Registration record for the user |
| `workflow_state` | `telegram_user_id` | 1:1 | User's current workflow state |
| `draft_bills` | `telegram_user_id` | 1:N | Active draft bills for this user |
| `bills` | `telegram_user_id` | 1:N | All finalized bills created by this user |

---

## Business Rules

1. **One record per Telegram user** — enforced by `UNIQUE (telegram_user_id)`. The Identity MCP's `register_user` tool is idempotent: if a record already exists, it returns the existing record without creating a duplicate.

2. **Never hard-delete** — use `is_active = FALSE` to disable a user. All historical bills, khata entries, and inventory records must remain intact.

3. **telegram_user_id is the external key** — the Lambda handler always has the Telegram user ID from the webhook payload. All lookups start with `WHERE telegram_user_id = ?`.

4. **id (UUID) is the internal key** — all foreign key relationships within the database use the UUID `id`, not `telegram_user_id`. This decouples the internal data model from the Telegram platform.

---

## Phase 2 Extensibility

| Change | How to Support |
|---|---|
| Multiple stores per user | Remove or relax the unique constraint on `stores.owner_user_id`. The `users` table itself needs no change. |
| Multi-user per store | Add a `store_users` join table with `user_id`, `store_id`, and `role` columns. `users` table needs no change. |
| Authentication beyond Telegram | Add `auth_provider` and `external_id` columns. The `telegram_user_id` becomes one of potentially many identity providers. |

---

## Example Record

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "telegram_user_id": 987654321,
  "telegram_username": "rameshkirana",
  "first_name": "Ramesh",
  "last_name": "Kumar",
  "is_active": true,
  "created_at": "2024-01-15T09:30:00Z",
  "updated_at": "2024-01-15T09:30:00Z"
}
```
