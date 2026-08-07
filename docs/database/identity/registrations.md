# Database Table: `registrations`

**Domain:** Identity  
**MCP Owner:** Identity MCP  
**Schema:** `identity`

---

## Purpose

The `registrations` table tracks the registration workflow for each Telegram user. It records when a user initiated registration, what stage they are in, and when registration completed. It acts as both a status tracker and an audit log for the onboarding flow.

This table ensures the registration process is idempotent — if a user starts registration and drops off midway, re-opening the chat resumes from where they left off rather than starting over.

---

## Schema

```sql
CREATE TYPE registration_status AS ENUM (
    'INITIATED',        -- User opened chat, user record created
    'STORE_CREATED',    -- Store details collected and stored
    'COMPLETE'          -- Registration fully complete, workflow_state → PENDING_CATALOGUE
);

CREATE TABLE public.registrations (
    id                  UUID                PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_user_id    BIGINT              NOT NULL UNIQUE,
    user_id             UUID                NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
    store_id            UUID                REFERENCES public.stores(id) ON DELETE RESTRICT,
    status              registration_status NOT NULL DEFAULT 'INITIATED',
    initiated_at        TIMESTAMPTZ         NOT NULL DEFAULT NOW(),
    store_created_at    TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    failure_reason      TEXT,
    created_at          TIMESTAMPTZ         NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ         NOT NULL DEFAULT NOW()
);
```

### Column Definitions

| Column | Type | Nullable | Default | Description |
|---|---|---|---|---|
| `id` | UUID | No | `gen_random_uuid()` | Internal primary key. |
| `telegram_user_id` | BIGINT | No | — | Telegram user ID. Unique — one registration record per Telegram account. Denormalized from `users` for fast lookup without a JOIN. |
| `user_id` | UUID | No | — | FK to `users.id`. The user this registration belongs to. |
| `store_id` | UUID | Yes | — | FK to `stores.id`. NULL until store is created. Populated when status transitions to `STORE_CREATED`. |
| `status` | registration_status | No | `'INITIATED'` | Current stage in the registration workflow. See state machine below. |
| `initiated_at` | TIMESTAMPTZ | No | `NOW()` | When the user first opened the chat and triggered registration. |
| `store_created_at` | TIMESTAMPTZ | Yes | — | Populated when store record is created. |
| `completed_at` | TIMESTAMPTZ | Yes | — | Populated when registration reaches `COMPLETE`. |
| `failure_reason` | TEXT | Yes | — | If registration was interrupted or failed, stores the reason for debugging. |
| `created_at` | TIMESTAMPTZ | No | `NOW()` | Immutable record creation timestamp. |
| `updated_at` | TIMESTAMPTZ | No | `NOW()` | Auto-updated via trigger. |

---

## Registration Status State Machine

```
User opens chat for first time
            ↓
      [INITIATED]
      user record created
      registration record created
            ↓
      Agent collects: shop_name, gstin (optional), address, phone
            ↓
      [STORE_CREATED]
      store record created
      store_id populated in registration
      store_created_at populated
            ↓
      Internal validation complete
            ↓
      [COMPLETE]
      completed_at populated
      workflow_state → PENDING_CATALOGUE
      Agent: "Registration complete! Now let's add your first product to the catalogue."
```

### Transition Rules

| From | To | Trigger | Side Effects |
|---|---|---|---|
| *(none)* | `INITIATED` | User sends first message | `users` record created, `registrations` record created |
| `INITIATED` | `STORE_CREATED` | `create_store()` succeeds | `stores` record created, `store_id` populated |
| `STORE_CREATED` | `COMPLETE` | Post-creation validation passes | `workflow_state` record created/updated to `PENDING_CATALOGUE` |

---

## Constraints

```sql
-- Primary key
ALTER TABLE public.registrations ADD CONSTRAINT registrations_pkey PRIMARY KEY (id);

-- One registration record per Telegram user
ALTER TABLE public.registrations ADD CONSTRAINT registrations_telegram_user_id_unique
    UNIQUE (telegram_user_id);

-- FK to users
ALTER TABLE public.registrations ADD CONSTRAINT registrations_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE RESTRICT;

-- FK to stores (nullable)
ALTER TABLE public.registrations ADD CONSTRAINT registrations_store_id_fkey
    FOREIGN KEY (store_id) REFERENCES public.stores(id) ON DELETE RESTRICT;

-- updated_at trigger
CREATE TRIGGER registrations_updated_at
    BEFORE UPDATE ON public.registrations
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
```

---

## Indexes

```sql
-- Primary lookup by Telegram user ID
CREATE INDEX idx_registrations_telegram_user_id ON public.registrations (telegram_user_id);

-- Lookup by user UUID (for internal joins)
CREATE INDEX idx_registrations_user_id ON public.registrations (user_id);

-- Lookup by status (for monitoring incomplete registrations)
CREATE INDEX idx_registrations_status ON public.registrations (status);
```

---

## Row Level Security (RLS)

```sql
ALTER TABLE public.registrations ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_role_full_access" ON public.registrations
    USING (TRUE)
    WITH CHECK (TRUE);
```

---

## Relations

### Outgoing (this table references)

| Table | Column | Type | Note |
|---|---|---|---|
| `users` | `user_id` | Many-to-One | The user being registered |
| `stores` | `store_id` | Many-to-One (nullable) | The store created during registration |

### Incoming (other tables reference `registrations`)
None — this is a terminal audit/tracking table.

---

## Idempotency Design

The registration flow must be idempotent because:
1. Telegram may redeliver the first message
2. The user may abandon registration midway and return later
3. The Lambda may time out mid-registration

**How it works:**

```
check_user_registration(telegram_user_id):
  SELECT status FROM registrations WHERE telegram_user_id = ?

  CASE status:
    NULL          → no record yet → INSERT new user + registration (INITIATED)
    'INITIATED'   → user record exists, no store yet → prompt for store details
    'STORE_CREATED' → store exists, not yet COMPLETE → complete the registration
    'COMPLETE'    → registration done → return existing store_id
```

Each step is a separate DB write, and the agent can always query the current status to know where to resume.

---

## Business Rules

1. **One registration per Telegram user:** Enforced by `UNIQUE (telegram_user_id)`. The Identity MCP checks this before creating a new registration record.

2. **Resumable:** If a user abandons registration at `INITIATED` (no store yet) and returns days later, the agent detects `status = INITIATED` and prompts for store details without re-creating the user record.

3. **store_id is NULL until store is created:** The FK is nullable for this reason. Phase 1 requires store creation to complete registration.

4. **completed_at is the signal for workflow activation:** When `completed_at` is populated and `status = COMPLETE`, the workflow_state table is updated to `PENDING_CATALOGUE`, unlocking catalogue tools.

5. **failure_reason for debugging:** If any step in registration fails (e.g., invalid GSTIN format), the reason is stored here. The agent reads this to give the user a helpful retry message.

---

## Phase 2 Extensibility

| Change | Migration Required |
|---|---|
| Multi-user per store | Add `invited_by_user_id` column for store staff invitation flow |
| Email/OTP verification step | Add `VERIFICATION_PENDING` to the status enum |
| KYC or document upload step | Add `kyc_document_url` column |

---

## Example Record

```json
{
  "id": "f1e2d3c4-b5a6-7890-1234-567890abcdef",
  "telegram_user_id": 987654321,
  "user_id": "550e8400-e29b-41d4-a716-446655440000",
  "store_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "status": "COMPLETE",
  "initiated_at": "2024-01-15T09:30:00Z",
  "store_created_at": "2024-01-15T09:32:00Z",
  "completed_at": "2024-01-15T09:32:30Z",
  "failure_reason": null,
  "created_at": "2024-01-15T09:30:00Z",
  "updated_at": "2024-01-15T09:32:30Z"
}
```
