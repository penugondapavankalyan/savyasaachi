# AGENTS.md

This file provides guidance to agents when working with code in this repository.

## Project Summary
Kirana Store Agent — a PydanticAI-powered Telegram bot for Indian grocery stores. Single AWS Lambda deployment (Python 3.12, `ap-south-1`). No web UI or standard web server; triggered strictly via Telegram webhook.

## Run, Test & Deployment Commands
There is no framework test runner (e.g. `pytest`) or linter config. Validation is done via local pipeline runners:
- **Interactive Local REPL**: `python run_local.py` (optional: `--user-id 12345`) — simulates the complete Lambda → Agent → Supabase → Redis pipeline.
- **Single / Pipeline Test Script**: `python scripts/test_agent_local.py` — runs a scripted sequence of messages against real Supabase/Redis. To test a single query or message flow, edit `test_messages` in `scripts/test_agent_local.py`.
- **Deploy to AWS Lambda**: `bash scripts/deploy.sh`
- **Register Webhook**: `python scripts/register_webhook.py`

## Code Style & Conventions

### Imports & Config
- **Future Imports**: Every Python file MUST start with `from __future__ import annotations`.
- **Circular Import Prevention**: Use `if TYPE_CHECKING:` guards for cross-MCP type hints. Defer `models.py` imports inside method bodies if needed.
- **Settings Access**: Never call `os.environ` directly in application code — always use `from src.config import settings`.

### Execution & Async Rules
- **Supabase Calls are Synchronous**: `supabase-py` v2 is synchronous. Do NOT `await` Supabase query execution (`.execute()`).
- **MCP Methods are Async**: All MCP methods (`src/mcp/<domain>/...`) must be `async` and return Pydantic v2 models.
- **Error Handling**: MCP methods raise standard Python exceptions (`ValueError`, `RuntimeError`). Do not swallow errors; PydanticAI catches and returns them as tool errors to the LLM.

### Money, GST & Validation
- **GST / Money Arithmetic**: Never compute GST inline or with `float`. Always use `from src.utils.gst import compute_line_gst, aggregate_gst` (`Decimal` with `ROUND_HALF_UP`). SGST absorbs rounding deltas (`sgst = gst_total - cgst`).
- **Input Guardrails**: All sanitization/validation MUST use `src/utils/guardrails.py` (`clean_phone`, `clean_gstin`, `clean_unit`, `clean_gst_rate`).
  - Branded items require `5/12/18/28%` (`clean_gst_rate` raises `ValueError` on 0% for branded items).
  - Loose items force `0.0%` GST.

### Database & Mutability
- **Schema Access**: Tables reside in domain-named PostgreSQL schemas (e.g., `.schema('billing').table('bills')`). RPCs reside in `public` (call via `db.rpc('rpc_name', {...}).execute()`).
- **Immutable Tables**: `bills`, `bill_items`, `khata_entries`, `stock_movements` have DB-level triggers preventing UPDATE/DELETE. Never attempt mutations on these tables; add new/adjustment entries instead.
