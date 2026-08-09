# AGENTS.md

This file provides guidance to agents when working with code in this repository.

## Project Summary
Kirana Store Agent — a PydanticAI-powered Telegram bot for Indian grocery stores. Single AWS Lambda deployment (Python 3.12, `ap-south-1`). Triggered strictly via Telegram webhook (no web UI or server).

## Build, Run & Test Commands
There is no framework test runner (e.g. `pytest`) or linter config in this repository. Validation is performed via local runners:
- **Interactive REPL**: `python run_local.py` (optional: `--user-id 12345`) — simulates the full Lambda → Agent → Supabase → Redis pipeline.
- **Run Single / Pipeline Test**: `python scripts/test_agent_local.py` — runs canned test sequences against real Supabase/Redis.
  - *To run a single test or query*: Edit the `test_messages` list in `scripts/test_agent_local.py` to contain only the specific message(s) under test.
- **Deploy to AWS Lambda**: `bash scripts/deploy.sh`
- **Register Telegram Webhook**: `python scripts/register_webhook.py`

## Code Style & Conventions

### Imports, Types & Naming
- **Future Annotations**: Every Python file MUST start with `from __future__ import annotations`.
- **Type Checking Guards**: Use `if TYPE_CHECKING:` guards for cross-MCP type hints to prevent circular imports. Defer internal `models.py` imports inside method bodies if required.
- **Models**: All MCP input/output payloads use Pydantic v2 `BaseModel` (located in `src/mcp/<domain>/models.py`).
- **Naming Conventions**: Standard Python naming — `snake_case` for functions/variables/files, `PascalCase` for classes/models, `UPPER_SNAKE_CASE` for constants.
- **Config & Settings**: Never call `os.environ` directly in application code — always use `from src.config import settings`.

### Execution, Async & Error Handling
- **Supabase Calls are Synchronous**: `supabase-py` v2 is synchronous. Do NOT `await` Supabase query execution (`.execute()`).
- **MCP Methods are Async**: All MCP domain methods (`src/mcp/<domain>/...`) must be `async` and return Pydantic v2 models.
- **`_one()` Helper**: Use the module-local `_one(resp)` helper instead of `resp.data[0]` — it handles None/empty Supabase responses safely.
- **Error Handling**: MCP methods raise standard Python exceptions (`ValueError`, `RuntimeError`). Do not swallow or hide business errors — PydanticAI catches them and returns clear tool error messages to the LLM.

### Singletons — Never Re-Instantiate in Request Path
- `get_mcp_instances()`, `get_agent()`, `get_client()` are module-level singletons.
- `MCPInstances` construction order matters: **Identity → Catalogue → Inventory → Khata → Billing → Analytics → Documents**. Do not reorder — each depends on the prior ones.
- The PydanticAI `Agent(...)` object is NOT a singleton — it is rebuilt each request in `_execute()` because the tool list changes per workflow state and intent, and the system prompt is assembled fresh per turn.

### Tool Registry Pattern — Context-Bound Closures
- All tools in `src/agent/tool_registry.py` are local async closures that close over `tuid` (telegram_user_id) and `store_id` from `StoreContext`.
- The LLM NEVER receives or passes `store_id` / `telegram_user_id`. Any new tool must follow this pattern.
- Tool docstrings ARE the LLM-facing API spec — write them as precise instructions to the model, not as developer documentation.
- **Mutable list cells for cross-closure coordination**: `_draft_id_cell = [context.active_draft_bill_id]` is a list, not a plain value. When `create_draft_bill()` writes a new UUID via `_draft_id_cell[0] = draft_id`, sibling tools (`add_item_to_draft`, `finalize_bill`) see it in the same turn. Python closures can read but not rebind outer variables, so the list is used for in-place mutation.
- For ACTIVE state, tools are split into 5 intent groups (BILLING / INVENTORY / KHATA / ANALYTICS / CATALOGUE). New tools must be placed in the correct group(s). Default group is BILLING.

### Money, GST, Guardrails & Business Rules
- **Bill Finalization & Payment Tool Calls**:
  - `finalize_bill`: MUST be called as a tool call immediately when payment mode (`cash`, `upi`, or `credit`) is specified. The agent MUST NOT output bill text without executing `finalize_bill`.
  - `confirm_payment`: MUST be called as a tool call immediately when payment is received (`paid <amount>`), whether exact, overpaid, or underpaid, so the bill transitions to `CONFIRMED` in `billing.bills`.
- **Underpayment Constraint**: In underpayment scenarios, remaining balance MUST be added to Khata credit (`add_credit_entry`). Split/multi-method payment for remaining balance is NOT supported.
- **Unit & Quantity Guardrails**:
  - Branded items (`is_loose = False`): Reject fractional quantities under ANY unit (integer quantities only).
  - Loose items (`is_loose = True`): Allow fractional quantities for `KG` and `L` only; integer-only for all other units.
- **GST & Money Arithmetic**: Never compute GST inline or with `float`. Always use `from src.utils.gst import compute_line_gst, aggregate_gst` (`Decimal` with `ROUND_HALF_UP`). SGST absorbs rounding deltas (`sgst = gst_total - cgst`).
- **Input Guardrails**: All sanitization/validation MUST use `src/utils/guardrails.py` (`clean_phone`, `clean_gstin`, `clean_unit`, `clean_gst_rate`, `clean_quantity_for_unit`).
- **Placeholder Detection**: `clean_optional_str()` in guardrails strips LLM hallucinations like `"none"`, `"null"`, `"n/a"`, `"not provided"`, `"test"`, `"xxx"`, `"some_gstin"` → returns `None`. Use it for all optional string fields.
- `clean_gst_rate(value, is_loose)` raises `ValueError` for branded items with `0%` or invalid slabs. This is intentional — the agent must ask the owner. Do not swallow this error.
- `clean_unit()` resolves ~50 common aliases before validation. Do not add inline unit normalisation elsewhere.
- `clean_quantity_for_unit(qty, unit, is_loose)` enforces that branded items cannot be sold in fractional quantities under any unit, while loose items allow fractions for `KG`/`L` only.

### Database & Mutability
- **Schema Access**: Tables reside in domain-named PostgreSQL schemas (e.g., `.schema('billing').table('bills')`). RPCs reside in `public` (call via `db.rpc('rpc_name', {...}).execute()`).
- **Immutable Tables**: `bills`, `bill_items`, `khata_entries`, `stock_movements` have DB-level triggers preventing `UPDATE`/`DELETE`. Never attempt mutations on these tables; add adjustment entries instead.
- **Atomic Stock Operations**: Use `increment_stock` and `decrement_stock` RPCs — never do stock math in application code. The RPCs record movements and lock rows atomically.

### Self-Healing — Do Not Remove
- `context_loader._self_heal_workflow()` detects and repairs stale `workflow_state` rows. This is required because MCP state transitions can fail mid-flight. Do not remove or skip this logic.

### System Prompt — What NOT to Change
- The system prompt in `src/agent/system_prompt.py` includes critical rule 12: "OBSERVE → THINK → ACT: one tool call or one question per turn."
- Rule 3 (GST mandatory for branded) and rule 13 (credit phone mandatory) are safety rails — do not soften them.
- `STATE_CODE_TO_NAME` dict in `system_prompt.py` is the authoritative source for Indian state codes — used in both prompt and `context_loader.py`.

### Model Fallback Cascade
- Primary model is `qwen/qwen3.6-27b` (Groq). Fallback chain: `llama-3.3-70b-versatile` → `openai/gpt-oss-20b` → `openai/gpt-oss-120b`.
- Recoverable errors (429, 503, 529, 400 "tool calling not supported", `UnexpectedModelBehavior`) trigger fallback. `UserError` / 401 raise immediately.
- Max tokens per model are tuned individually in `kirana_agent.py` — do not homogenize them.
