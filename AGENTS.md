# AGENTS.md

This file provides guidance to agents when working with code in this repository.

## Project Summary
Kirana Store Agent — a PydanticAI-powered Telegram bot for Indian grocery stores. Single AWS Lambda deployment (Python 3.12, `ap-south-1`). Triggered strictly via Telegram webhook (no web UI or server).

## Build, Run & Test Commands
There is no `pytest` or linter config. Validation is performed via local runners:
- **Interactive REPL**: `python run_local.py` (optional: `--user-id 12345`) — simulates full Lambda → Agent → Supabase → Redis pipeline.
  - REPL commands: `/new`, `/status`, `/history`, `/debug`, `/quit`
  - `/debug` shows the full tool call trace from the last agent run.
- **Run Single / Pipeline Test**: `python scripts/test_agent_local.py` — runs canned sequences against real Supabase/Redis.
  - *To run a single test*: Edit the `test_messages` list in `scripts/test_agent_local.py` to contain only that message.
  - Test user ID defaults to `99999`; override with `TEST_TELEGRAM_USER_ID` env var.
- **Deploy to AWS Lambda**: `bash scripts/deploy.sh`
- **Register Telegram Webhook**: `python scripts/register_webhook.py`

## Code Style & Conventions

### Imports, Types & Naming
- **Future Annotations**: Every Python file MUST start with `from __future__ import annotations`.
- **Type Checking Guards**: Use `if TYPE_CHECKING:` for cross-MCP type hints to prevent circular imports. Defer internal `models.py` imports inside method bodies if needed.
- **Models**: All MCP input/output payloads use Pydantic v2 `BaseModel` in `src/mcp/<domain>/models.py`.
- **Config**: Never call `os.environ` directly — always use `from src.config import settings`. Settings keys are lazy `@property` validators that raise `RuntimeError` if required keys are missing.
- **IST dates only**: All date/time must use `src/utils/ist.py` helpers (`today_ist()`, `now_ist()`, `day_start_iso()`, `day_end_iso()`). Never use `datetime.utcnow()`.
- **Heavy optional deps**: `fpdf`, `pptx` imports MUST be deferred inside method bodies (never at module level) — reduces Lambda cold-start time.

### Execution, Async & Error Handling
- **Supabase Calls are Synchronous**: `supabase-py` v2 is synchronous. Do NOT `await` Supabase query execution (`.execute()`).
- **supabase-py query builder mutates in-place**: `.ilike()`, `.eq()` etc. call `self.request.params.add()` which accumulates on the same object. Never reuse a base query across a loop — build a fresh query each iteration (see `catalogue_mcp.py` `search_products` for the correct pattern).
- **MCP Methods are Async**: All MCP domain methods must be `async` and return Pydantic v2 models.
- **`_one()` Helper**: Use the module-local `_one(resp)` helper instead of `resp.data[0]` — handles None/empty Supabase responses safely.
- **Reorder alert failures must be silently swallowed** — `send_reorder_alert()` wraps its logic in bare `except Exception: pass` intentionally.

### Singletons — Never Re-Instantiate in Request Path
- `get_mcp_instances()`, `get_agent()`, `get_client()` are module-level singletons.
- `MCPInstances` construction order: **Identity → Catalogue → Inventory → Khata → Billing → Analytics → Documents → Payments**. Do not reorder.
  - After construction, `billing.set_payments_mcp(self.payments)` late-binds PaymentsMCP into BillingMCP to break the circular construction dependency.
- The PydanticAI `Agent(...)` object is NOT a singleton — rebuilt each request because the tool list changes per workflow state/intent and system prompt is assembled fresh per turn.

### Tool Registry Pattern — Context-Bound Closures
- All tools in `src/agent/tool_registry.py` are local async closures that close over `tuid` (telegram_user_id) and `store_id` from `StoreContext`.
- The LLM NEVER receives or passes `store_id` / `telegram_user_id`. Any new tool must follow this pattern.
- Tool docstrings ARE the LLM-facing API spec — write them as precise instructions to the model.
- **Mutable list cells**: `_draft_id_cell = [value]`, `_bill_id_cell = [value]`, `_last_confirmed_bill_cell = [value]`, `_last_added_product_id_cell = [value]` are lists (not plain variables) to allow in-place mutation visible across sibling closures in the same request.
- **`_last_added_product_id_cell`**: written by `add_product()` immediately after a successful catalogue insert. `receive_stock()` always prefers this cell over whatever UUID the LLM passes — prevents the model from hallucinating a stale product_id from a previous session and sending it to the DB. If the cell is populated and differs from the LLM-passed value, the cell wins. If the cell is empty and the LLM-passed value is not a valid UUID, `receive_stock()` returns a clear error.
- For ACTIVE state, tools are split into 6 intent groups: **BILLING / BILLING_CONFIRM / INVENTORY / KHATA / ANALYTICS / CATALOGUE**, max ~12 tools per request. New tools must go into the correct group(s). Default group is BILLING.
- `detect_intent()` takes `has_active_draft` and `last_assistant_msg` — payment-mode words ("credit", "cash", "upi") during an active billing session route to BILLING, not KHATA.
- `BILLING_CONFIRM` routing is **two-tiered** in `detect_intent()`:
  - **Lookup keywords** (`_BILLING_CONFIRM_LOOKUP_KEYWORDS` frozenset): bill listing, PDF/invoice generation — always route to BILLING_CONFIRM even when `has_active_draft=True` (safe: don't touch active draft).
  - **Payment/cancel keywords**: `paid`, `cancel`, `void` etc. — only route to BILLING_CONFIRM when `has_active_draft=False`.
- `_PAID_PATTERN` regex (`\bpaid\b.*?\d|[\w]+ paid\b`) is a secondary catch — ensures `"naveen paid 40"` routes to BILLING_CONFIRM without explicit keywords.

### Documents MCP — PDF & PPTX
- `generate_invoice_pdf(bill_number_or_id)` is a **shared closure** (defined above all intent `if` blocks) — appears in both ANALYTICS and BILLING_CONFIRM groups. `generate_analysis_pptx(period)` is ANALYTICS-only.
- `generate_invoice_pdf` accepts either a bill UUID or a human bill number (e.g. `BL-003-20260815-013`). When a bill number is passed it does a live DB lookup (`billing.bills` by `bill_number`) to resolve the UUID.
- Both closures call `telegram.send_document(tuid, file_path)` then `os.remove(file_path)` — the MCP itself does NOT call Telegram.
- Both use lazy imports inside method bodies — not at module level. Do not hoist `from fpdf import FPDF` or `from pptx import Presentation` to module level (Lambda cold-start).
- `_STATE_NAMES` dict is defined locally in `documents_mcp.py` (NOT the `STATE_CODE_TO_NAME` in `system_prompt.py`) — two separate copies exist. `_STATE_NAMES` has ~38 entries; `_VALID_STATE_CODES` in `guardrails.py` validates 24 — keep all three in sync if adding state codes.
- **PPTX Slide 2** is a **Daily Summary Table** (Date, Bills, Total, Cash, UPI, Credit, GST per day) — not a line chart. `AnalyticsDeckData` has `daily_summaries: list[DailySummaryResult]` populated by `get_analytics_deck_data()` looping every calendar day.
- **PDF theme system**: `_ACTIVE_PDF_THEME = "default"` in `documents_mcp.py` — change this single line to switch globally. Three built-in themes: `default` (gold), `blue`, `green`.
- **PDF column system**: `_ITEM_COLUMNS` list of `(header, field_key, width_mm, align)` tuples — 9 columns totalling exactly 190mm. Adding/removing a column requires only one entry change here.
- **`send_document()` behaviour**: `LOCAL_MODE=true` → copies file to `LOCAL_DOCS_OUTPUT_DIR` (default `local_output/`) and auto-opens with OS viewer. Production: uploads to Telegram API (60s timeout vs 10s for messages).

### Payments System
- **`payments` schema** is separate — tables live in `payments.payments`, PK is `payment_id`.
- **Payment types**: `EXACT`, `OVERPAYMENT`, `UNDERPAYMENT`, `KHATA`, `KHATA_SETTLE`.
- **Payment statuses**: `CONFIRMED`, `PENDING`, `CANCELLED`, `REFUNDED`.
- **`confirm_payment(paid_amount)` flow** (3-step, spans multiple turns for over/underpayment):
  1. Flip bill PENDING_PAYMENT → CONFIRMED via `confirm_payment` RPC.
  2. Classify: EXACT (insert payment row + done) | OVERPAYMENT (store delta in Redis, ask owner) | UNDERPAYMENT (store delta in Redis, ask owner).
  3. For OVER: owner says "add to khata" → `get_customer` → `add_payment_entry(customer_id, amount=None)` reads from Redis → inserts OVERPAYMENT payment row → clears Redis key.
     For UNDER: owner says "add to khata" → `get_customer` → `add_credit_entry(customer_id, amount=None)` reads from Redis → inserts UNDERPAYMENT payment row → clears Redis key.
- **Redis bridge**: `pending_payment:{telegram_user_id}` key (30-min TTL) stores the delta amount between turns to prevent LLM hallucination. Key is `set_pending_payment` / `get_pending_payment` / `clear_pending_payment` in `src/redis/upstash_client.py`. Key payload must NOT include `customer_id` — customer identity is conversational state, not financial state.
- **CREDIT bills**: At `finalize_bill`, PaymentsMCP immediately inserts a row (`payment_type=KHATA`, `paid_amount=0`, `khata_entry_id` set). `confirm_payment()` must NOT be called after a credit bill.
- **Cancelled bill**: `cancel_bill` inserts a `CANCELLED` audit row. **Voided bill**: `void_bill` inserts a `REFUNDED` audit row.
- **`void_bill_by_number(bill_number_or_id)`**: agent-layer tool (no new MCP method) — accepts a human bill number (e.g. `BL-003-20260815-009`) or UUID and voids any named historical CONFIRMED bill. Resolves bill number → UUID via `billing.bills` DB lookup (scoped to `store_id`), then delegates to `BillingMCP.void_bill()`. Registered in `BILLING_CONFIRM` group. **Use this when the owner explicitly names a bill; use `void_bill()` for same-session "undo".**
- **`khata_entry_id` must be resolved BEFORE inserting the payment row** — the payments table is immutable (no post-insert updates).
- **`PaymentsMCP` is wired via late-binding** — `BillingMCP.__init__` accepts `payments_mcp=None`; `set_payments_mcp()` injects it after `MCPInstances` constructs both.
- **Expose `payments` schema** in Supabase: Dashboard → Project Settings → API → Exposed schemas → add `payments`.
- **`add_payment_entry` with explicit `amount`** always creates a `KHATA_SETTLE` payment row with `bill_id=None` and `payment_mode="CASH"` hardcoded.
- **`add_credit_entry` bypass-confirm self-heal**: detects when `amount is not None AND intent is None AND _bill_id_cell[0] is not None` (model bypassed `confirm_payment`). Auto-confirms the PENDING_PAYMENT bill and synthesises a minimal intent dict before recording the payment row.

### Money, GST, Guardrails & Business Rules
- **GST**: Never compute GST inline. Always use `from src.utils.gst import compute_line_gst, aggregate_gst`. SGST absorbs rounding: `sgst = gst_total - cgst`.
- **Guardrails** (`src/utils/guardrails.py`):
  - `clean_optional_str()` — strips LLM hallucinations: `"none"`, `"null"`, `"n/a"`, `"not provided"`, `"test"`, `"xxx"` → `None`.
  - `clean_gst_rate(value, is_loose)` — raises `ValueError` for branded items with 0% or invalid slabs (5/12/18/28 only). Do NOT swallow this error.
  - `clean_unit()` — resolves ~50 common aliases. Do NOT add inline unit normalisation elsewhere.
  - `clean_quantity_for_unit(qty, unit, is_loose)` — branded items always integer quantities; loose items allow floats only for KG/L.
  - `clean_uuid()` — used inside tool closures to guard against LLM-invented UUIDs before any DB call.
  - `clean_phone()` — strips country code (+91), STD prefix (0), spaces/dashes; must be 10 digits starting with 6/7/8/9.
- **Bill-listing shared closures** (all wired in `tool_registry.py`):
  - `get_bills_by_date(date_str)` — **shared closure** in both ANALYTICS and BILLING_CONFIRM. Lists all bills for a date. Docstring instructs model to convert natural-language dates (e.g. `"13th august"` → `"2026-08-13"`) itself.
  - `list_bills_for_customer(name_or_phone)` — shared closure in BILLING_CONFIRM (and formerly KHATA). Lists all bills linked to a customer with current khata balance.
  - Both query `billing.bills` directly (no new MCP methods needed).
- **Mandatory Tool Calls**:
  - `finalize_bill` or `finalize_and_pay` MUST execute when payment mode is specified. Never output bill summary without calling one of them.
  - `confirm_payment(paid_amount)` MUST execute when owner says "paid <amount>". If no amount is given, ask "How much did the customer pay?" before calling.
- **`finalize_and_pay` vs `finalize_bill`**: Use `finalize_and_pay` for CASH/UPI (handles both one-turn and two-turn flows). Use `finalize_bill` ONLY for CREDIT sales.
- **Optional Customer Linking on Payment**: Both `confirm_payment(paid_amount, customer_name=None)` and `finalize_and_pay(payment_mode, paid_amount=None, customer_name=None)` accept an optional `customer_name`. Pass it when the owner volunteers a name/phone in the same message (e.g. `"ramesh paid 500"`). Never ask for it. Customer resolution happens internally — never blocks payment.

### Database & Mutability
- **Schema Access**: Tables in domain-named schemas: `.schema('billing').table('bills')`. RPCs always in `public`: `db.rpc('decrement_stock', {...}).execute()`.
- **Immutable Tables**: `bills`, `bill_items`, `khata_entries`, `stock_movements`, `payments.payments` have DB-level triggers preventing UPDATE/DELETE. Add adjustment entries instead.
- **Atomic Stock Operations**: Use `increment_stock` / `decrement_stock` RPCs only — never update stock counts in application code.
- **Supabase schemas to expose**: `identity, catalogue, inventory, billing, khata, analytics, payments`.
- **`workflow_state.active_draft_bill_id`** links the current open draft bill across stateless Lambda invocations.
- **NULL brand in upsert**: `ON CONFLICT` on `(store_id, name, brand)` does NOT fire for NULL brand (NULL ≠ NULL in PostgreSQL unique constraints). `CatalogueMCP.add_product` does an explicit SELECT-then-INSERT/UPDATE to handle this.

### Self-Healing — Do Not Remove
- `context_loader._self_heal_workflow()` detects and repairs stale `workflow_state` rows. Required because MCP state transitions can fail mid-flight.

### System Prompt — What NOT to Change
- Rule 12 in `src/agent/system_prompt.py`: "OBSERVE → THINK → ACT: one tool call or one question per turn."
- Rule 3 (GST mandatory for branded) and rule 13 (credit phone mandatory) are safety rails — do not soften them.
- Rule 15 (CATALOGUE DISPLAY RULES): never reclassify LOOSE/BRANDED, never add your own headings.
- Stage 3 GOLDEN RULE: `confirm_payment()` MUST ALWAYS be a real tool call — NEVER write 'Payment confirmed' without calling it.
- `STATE_CODE_TO_NAME` in `system_prompt.py` is the authoritative source for Indian state codes (separate copy also exists in `documents_mcp.py` for PDF generation).

### Model Fallback Cascade
- Primary: `qwen/qwen3.6-27b` (Groq, 8K TPM). Fallback chain: `llama-3.3-70b-versatile` (12K TPM) → `openai/gpt-oss-20b` → `openai/gpt-oss-120b`.
- Recoverable errors (429, 503, 529, 400 "tool calling not supported" or "not found", `UnexpectedModelBehavior`) trigger fallback. `UserError` / 401 raise immediately.
- Max tokens tuned per model in `kirana_agent.py` — `llama-3.3-70b-versatile` gets 3000; others 2048. Do not homogenize.
- Ollama uses `OpenAIChatModel` + `OpenAIProvider` (NOT a native Ollama client) — works for both cloud (`https://ollama.com/v1`) and local (`http://localhost:11434/v1`).
- `kirana.tool_audit` logger (INFO level) logs every tool call and return. `[NO TOOLS CALLED]` warnings indicate the agent responded without using tools — a model compliance issue, not a code bug.

## Deploy to Telegram + AWS Lambda (End-to-End)

### Prerequisites
- AWS CLI configured with IAM user having Lambda + IAM permissions.
- Telegram bot token from [@BotFather](https://t.me/BotFather).
- Filled `.env` file (copy from `.env.example`).

### Step 1 — One-time Lambda setup
```bash
aws iam create-role --role-name kirana-agent-lambda-role \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
aws iam attach-role-policy --role-name kirana-agent-lambda-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

pip install -r requirements.txt -t package/
cp -r src/ package/
cd package && zip -r ../kirana-agent.zip . && cd ..

aws lambda create-function \
  --function-name kirana-agent \
  --runtime python3.12 \
  --handler src.handler.lambda_handler \
  --role arn:aws:iam::YOUR_ACCOUNT_ID:role/kirana-agent-lambda-role \
  --timeout 29 \
  --memory-size 512 \
  --region ap-south-1 \
  --zip-file fileb://kirana-agent.zip

aws lambda update-function-configuration \
  --function-name kirana-agent \
  --environment "Variables={TELEGRAM_BOT_TOKEN=...,SUPABASE_URL=...,SUPABASE_SERVICE_ROLE_KEY=...,UPSTASH_REDIS_REST_URL=...,UPSTASH_REDIS_REST_TOKEN=...,GROQ_API_KEY=...,LLM_PROVIDER=groq,LLM_MODEL=qwen/qwen3.6-27b,LLM_FALLBACK_MODELS=llama-3.3-70b-versatile}"

aws lambda create-function-url-config --function-name kirana-agent --auth-type NONE
aws lambda add-permission --function-name kirana-agent \
  --action lambda:InvokeFunctionUrl --principal "*" \
  --function-url-auth-type NONE --statement-id AllowPublicInvoke
```

### Step 2 — Register Telegram webhook
```bash
export TELEGRAM_BOT_TOKEN=your_token_here
python scripts/register_webhook.py --url https://YOUR_FUNCTION_URL/
python scripts/register_webhook.py --info   # verify
```

### Step 3 — Subsequent deploys
```bash
bash scripts/deploy.sh
# Auto-detects zip size; if >50 MB uploads via S3 (set DEPLOY_S3_BUCKET env var).
# Waits for Lambda to finish updating before exiting.
```

### Key deployment facts
- Lambda function name hard-coded as `kirana-agent` in `scripts/deploy.sh`.
- Region defaults to `ap-south-1`; override: `AWS_REGION=us-east-1 bash scripts/deploy.sh`.
- **Timeout is 29 seconds** — intentionally 1 s under Telegram's 30 s webhook timeout.
- Handler path: `src.handler.lambda_handler`.
- `.env` is NOT packaged — all LLM keys must be Lambda environment variables.
- Check logs: `aws logs tail /aws/lambda/kirana-agent --follow --region ap-south-1`
- `/tmp` in Lambda is ephemeral (512 MB max) — used by Documents MCP for PDF/PPTX generation. Files must be deleted after `send_document`.
