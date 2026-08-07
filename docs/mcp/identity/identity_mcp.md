# MCP Module: Identity MCP

**Domain:** Identity  
**Module Path:** `src/mcp/identity/identity_mcp.py`  
**Owned Tables:** `users`, `stores`, `registrations`, `workflow_state`

---

## Responsibility

The Identity MCP is the **first module invoked for any new user**. It owns the complete lifecycle of user registration, store creation, and workflow state management. No other MCP module writes to the tables owned by Identity MCP.

This module is also responsible for persisting and retrieving owner preferences that survive across chat sessions.

---

## Tools (PydanticAI Tool Functions)

### 1. `check_user_registration`

**Description:** Checks the registration status of a Telegram user. Called by the Lambda handler before agent invocation to determine the workflow state.

**Signature:**
```python
async def check_user_registration(telegram_user_id: int) -> RegistrationStatusResult
```

**Input:**
| Parameter | Type | Description |
|---|---|---|
| `telegram_user_id` | `int` | Telegram's numeric user ID from the webhook payload |

**Output (`RegistrationStatusResult`):**
```python
class RegistrationStatusResult(BaseModel):
    is_registered: bool
    status: str  # 'UNREGISTERED' | 'INITIATED' | 'STORE_CREATED' | 'COMPLETE'
    user_id: Optional[str]
    store_id: Optional[str]
    workflow_state: str  # 'UNREGISTERED' | 'PENDING_CATALOGUE' | 'PENDING_INVENTORY' | 'ACTIVE'
```

**DB Operations:**
```sql
SELECT r.status, r.user_id, r.store_id, ws.current_state
FROM registrations r
JOIN workflow_state ws ON ws.telegram_user_id = r.telegram_user_id
WHERE r.telegram_user_id = ?
```

**Business Rules:**
- If no record found → returns `is_registered=False, workflow_state='UNREGISTERED'`
- If `registration.status = 'COMPLETE'` → returns `is_registered=True`
- Otherwise → returns `is_registered=False` with current progress status

---

### 2. `register_user`

**Description:** Creates a new user record. Called when a new Telegram user sends their first message.

**Signature:**
```python
async def register_user(
    telegram_user_id: int,
    telegram_username: Optional[str],
    first_name: Optional[str],
    last_name: Optional[str]
) -> RegisterUserResult
```

**Output (`RegisterUserResult`):**
```python
class RegisterUserResult(BaseModel):
    user_id: str
    already_existed: bool  # True if user was already registered (idempotent)
    message: str
```

**DB Operations (Idempotent):**
```sql
INSERT INTO users (telegram_user_id, telegram_username, first_name, last_name)
VALUES (?, ?, ?, ?)
ON CONFLICT (telegram_user_id) DO UPDATE
SET telegram_username = EXCLUDED.telegram_username,
    first_name = EXCLUDED.first_name,
    updated_at = NOW()
RETURNING id;

-- Ensure workflow_state record exists
INSERT INTO workflow_state (telegram_user_id, current_state)
VALUES (?, 'UNREGISTERED')
ON CONFLICT (telegram_user_id) DO NOTHING;

-- Upsert registration record
INSERT INTO registrations (telegram_user_id, user_id, status)
VALUES (?, ?, 'INITIATED')
ON CONFLICT (telegram_user_id) DO NOTHING;
```

**Business Rules:**
- Fully idempotent — safe to call multiple times
- If user already exists, updates username/name in case they changed in Telegram
- Never downgrades registration status

---

### 3. `create_store`

**Description:** Creates a store record and links it to the user. Advances registration to `STORE_CREATED` and workflow state to `PENDING_CATALOGUE`.

**Signature:**
```python
async def create_store(
    telegram_user_id: int,
    shop_name: str,
    gstin: Optional[str],
    address: Optional[str],
    phone: Optional[str],
    state_code: str = '29'
) -> CreateStoreResult
```

**Output (`CreateStoreResult`):**
```python
class CreateStoreResult(BaseModel):
    store_id: str
    already_existed: bool
    shop_name: str
    message: str
```

**DB Operations:**
```sql
-- Phase 1: check user doesn't already have a store
SELECT id FROM stores WHERE owner_user_id = (
    SELECT id FROM users WHERE telegram_user_id = ?
);
-- If found → return existing store (idempotent)

-- Create store
INSERT INTO stores (owner_user_id, shop_name, gstin, address, phone, state_code)
VALUES (?, ?, ?, ?, ?, ?)
RETURNING id;

-- Update registration
UPDATE registrations
SET store_id = ?, status = 'STORE_CREATED', store_created_at = NOW()
WHERE telegram_user_id = ?;

-- Mark registration COMPLETE
UPDATE registrations
SET status = 'COMPLETE', completed_at = NOW()
WHERE telegram_user_id = ?;

-- Advance workflow state
UPDATE workflow_state
SET current_state = 'PENDING_CATALOGUE', store_id = ?, user_id = (
    SELECT id FROM users WHERE telegram_user_id = ?
), updated_at = NOW()
WHERE telegram_user_id = ?;
```

**Business Rules:**
- Phase 1: enforces one store per user — returns existing store if already created
- GSTIN validated against regex `^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$` if provided
- All four DB writes are in a single transaction — partial registration never occurs
- After success, agent prompts: "Store created! Now let's add your first product to the catalogue."

---

### 4. `get_store`

**Description:** Retrieves the store details for a Telegram user.

**Signature:**
```python
async def get_store(telegram_user_id: int) -> StoreResult
```

**Output (`StoreResult`):**
```python
class StoreResult(BaseModel):
    store_id: str
    shop_name: str
    gstin: Optional[str]
    address: Optional[str]
    phone: Optional[str]
    state_code: str
    default_payment_mode: str
    preferences: dict
```

**DB Operations:**
```sql
SELECT s.* FROM stores s
JOIN users u ON u.id = s.owner_user_id
WHERE u.telegram_user_id = ?;
```

---

### 5. `update_store_preferences`

**Description:** Updates the owner's persistent preferences stored in `stores.preferences`. Called when the owner says things like "always assume UPI", "default atta = Aashirvaad 5kg".

**Signature:**
```python
async def update_store_preferences(
    store_id: str,
    preference_key: str,
    preference_value: Any
) -> UpdatePreferencesResult
```

**Input examples:**
- `preference_key="default_payment_mode"`, `preference_value="UPI"` → updates `stores.default_payment_mode`
- `preference_key="preferred_brands.atta"`, `preference_value="Aashirvaad Atta 5kg"` → merges into `stores.preferences` JSONB
- `preference_key="low_stock_alert_enabled"`, `preference_value=False` → updates in JSONB

**DB Operations:**
```sql
-- For JSONB preferences (dot-notation keys)
UPDATE stores
SET preferences = jsonb_set(preferences, '{preferred_brands, atta}', '"Aashirvaad Atta 5kg"'),
    updated_at = NOW()
WHERE id = ?;

-- For top-level columns like default_payment_mode
UPDATE stores
SET default_payment_mode = 'UPI', updated_at = NOW()
WHERE id = ?;
```

**Business Rules:**
- Preferences persist forever — not cleared on `/new` chat
- Agent confirms the change: "Got it! I'll always assume UPI payment unless you say otherwise."

---

### 6. `get_workflow_state`

**Description:** Returns the current workflow state for a Telegram user. Used by the Lambda handler (pre-agent context loader).

**Signature:**
```python
async def get_workflow_state(telegram_user_id: int) -> WorkflowStateResult
```

**Output (`WorkflowStateResult`):**
```python
class WorkflowStateResult(BaseModel):
    current_state: str  # UNREGISTERED | PENDING_CATALOGUE | PENDING_INVENTORY | ACTIVE
    store_id: Optional[str]
    user_id: Optional[str]
    active_draft_bill_id: Optional[str]
```

---

### 7. `advance_workflow_state`

**Description:** Transitions the workflow state to the next stage. Called by Catalogue MCP (after first product) and Inventory MCP (after first stock-in).

**Signature:**
```python
async def advance_workflow_state(
    telegram_user_id: int,
    new_state: str  # 'PENDING_INVENTORY' | 'ACTIVE'
) -> bool
```

**DB Operations:**
```sql
UPDATE workflow_state
SET current_state = ?, updated_at = NOW()
WHERE telegram_user_id = ?
  AND current_state != 'ACTIVE';  -- Never downgrade
```

**Business Rules:**
- State only moves forward — cannot transition backwards
- Calling with same state as current is a no-op (idempotent)

---

### 8. `set_active_draft_bill`

**Description:** Sets or clears the `active_draft_bill_id` in `workflow_state`. Called by Billing MCP when a draft is created, confirmed, or cancelled.

**Signature:**
```python
async def set_active_draft_bill(
    telegram_user_id: int,
    draft_bill_id: Optional[str]  # None to clear
) -> bool
```

---

## Error Handling

| Error | Response |
|---|---|
| User not found | Creates user record (register_user is idempotent) |
| Store already exists (Phase 1) | Returns existing store with `already_existed=True` |
| Invalid GSTIN format | Returns validation error, agent asks owner to re-enter or skip |
| DB transaction failure | Rolls back entirely, returns error, agent retries or informs user |

---

## Phase 2 Extensibility

| Feature | Change Required |
|---|---|
| Multiple stores per user | `create_store` no longer checks for existing store per user |
| Multi-user per store | Add `invite_user_to_store(store_id, telegram_user_id, role)` tool |
| Store switching | Add `select_active_store(telegram_user_id, store_id)` tool |
