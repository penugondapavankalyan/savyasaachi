# AGENTS.md

This file provides guidance to agents when working with code in this repository.

## Project

Kirana Store Agent — a PydanticAI-powered Telegram bot that runs an Indian grocery store from chat. No web UI. The Telegram message is the only trigger. Deployed as a single AWS Lambda (Python 3.12).

## Run / Dev Commands

```bash
# Interactive local development REPL (the primary dev tool — simulates full Lambda pipeline)
python run_local.py
python run_local.py --user-id 12345   # test with a specific fake Telegram user ID

# Scripted test run (canned messages through the full agent pipeline)
python scripts/test_agent_local.py

# Deploy to Lambda (AWS CLI must be configured, region ap-south-1)
bash scripts/deploy.sh

# Register Telegram webhook after deploy
python scripts/register_webhook.py
```

There is **no test framework, no linter config, no CI**. Validation is done by running the agent manually via `run_local.py` or `scripts/test_agent_local.py`.

## Architecture — Critical Non-Obvious Facts

- **All MCP modules are Python classes co-deployed in the same Lambda** — no HTTP between them. Inter-MCP calls are direct Python method calls (`self._inventory.check_availability(...)`).
- **`get_mcp_instances()` / `get_agent()` / `get_client()` are module-level singletons** — they persist across warm Lambda invocations. Never instantiate MCPs, agent, or Supabase client inside a request path.
- **The LLM must NEVER receive or pass `store_id` or `telegram_user_id` to tools.** Tool functions in `src/agent/tool_registry.py` are **context-bound closures** that bake in these IDs at request time. Adding any tool that exposes these IDs will break the security model.
- **Tools are rebuilt from scratch on every request** by `get_tools_for_state()` in `tool_registry.py`. Each tool function is a local async closure, not a method reference. The agent `Agent(...)` object is also re-instantiated per request (not a singleton).
- **ACTIVE state uses intent-based tool sub-groups** (max ~12 tools per call) instead of all tools. `detect_intent()` in `tool_registry.py` classifies the message into BILLING / INVENTORY / KHATA / ANALYTICS / CATALOGUE. Default is BILLING. The "credit" keyword is intentionally excluded from KHATA detection — it routes to BILLING when a draft bill is active.
- **Self-healing workflow state**: `context_loader.py` detects and repairs stale `workflow_state` on every request by comparing against actual DB data. This is intentional — do not remove these checks.
- **All tables live in named PostgreSQL schemas** (`identity`, `catalogue`, `inventory`, `billing`, `khata`, `analytics`), not `public`. Supabase client must use `.schema('billing').table('bills')` syntax. RPCs always live in `public` — no schema selector needed for them.
- **`bills`, `bill_items`, `khata_entries`, `stock_movements` are immutable** — DB triggers prevent UPDATE/DELETE. Never attempt to correct them after insert.
- **Bill finalization is a single Supabase RPC transaction** (`decrement_stock`). The Supabase Python client does not support multi-statement transactions; all atomic operations go through RPCs defined in `migrations/010_create_rpcs.sql`.

## Environment & Config

- All secrets load from `.env` in project root via `src/config.py` (auto-discovers `.env`). **Never call `os.environ` directly — use `from src.config import settings`.**
- `.env.example` contains the real Supabase/Upstash project URLs (non-sensitive publishable keys). `TELEGRAM_BOT_TOKEN` and `GROQ_API_KEY`/`OLLAMA_API_KEY` must be filled in.
- `LLM_PROVIDER=groq` (prod) or `LLM_PROVIDER=ollama` (dev). Ollama cloud uses `https://ollama.com/v1` + `OLLAMA_API_KEY`. Local Ollama uses `http://localhost:11434/v1` — no real key needed.
- Groq fallback chain: `qwen/qwen3.6-27b` → `llama-3.3-70b-versatile` → `openai/gpt-oss-20b` → `openai/gpt-oss-120b`. Triggered on 429, 503, 529, 400-tool-calling errors, and `UnexpectedModelBehavior`.

## Code Style

- `from __future__ import annotations` at top of every file.
- `TYPE_CHECKING` guards for cross-MCP imports to avoid circular imports.
- Pydantic v2 `BaseModel` for all MCP input/output models (in each `src/mcp/<domain>/models.py`).
- All MCP methods are `async`. Supabase Python client calls are **synchronous** (supabase-py v2 is sync) — do not `await` them.
- Error handling: MCP methods raise plain Python exceptions with clear messages. PydanticAI catches and returns them as tool error results to the LLM.
- GST computations use `Decimal` with `ROUND_HALF_UP` in `src/utils/gst.py`. Never use `float` arithmetic for money.
- All LLM input sanitisation goes through `src/utils/guardrails.py` helpers (`clean_phone`, `clean_gstin`, `clean_unit`, `clean_gst_rate`, etc.). Never validate inline — use these helpers.

## Business Rules Baked Into Code

- **Loose items**: `gst_rate` is always forced to `0.0` by `clean_gst_rate(is_loose=True)`. Branded items must be `5 / 12 / 18 / 28` — `0.0` raises `ValueError`.
- **Units**: 8 canonical values (`KG`, `G`, `L`, `ML`, `PACKET`, `PIECE`, `DOZEN`, `BUNDLE`). `KG`/`L` allow float quantities; all others are integer-only. `clean_unit()` resolves ~50 common aliases.
- **Khata entries are append-only** — `prevent_immutable_record_mutation` DB trigger. Balance = `SUM` of all entries for a customer.
- **`finalize_bill` must always be called to complete a sale** — it decrements stock, creates the permanent bill record, and optionally creates the khata entry. Never call `add_credit_entry` directly during billing.
- **Draft bills expire after 4 hours** (`DRAFT_BILL_TTL_HOURS`). `context_loader.py` detects and expires them on each request.
- **Conversation history**: stored in Upstash Redis under key `conv:{telegram_user_id}`. Capped at 200 stored entries; only last `MAX_HISTORY_MESSAGES` (default 10) are passed to the agent.
- **`/new` command**: clears Redis history AND cancels the active draft bill. Does NOT touch Supabase store data.
