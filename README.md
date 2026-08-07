# Kirana Agent

A Telegram-based AI assistant for managing Indian kirana (grocery) stores.
Built with PydanticAI, Supabase, and AWS Lambda.

## What It Does

| Feature | Description |
|---|---|
| **Registration** | Register a store via natural conversation |
| **Catalogue** | Add and manage products (loose/branded, GST-aware) |
| **Inventory** | Track stock levels, receive goods, reorder alerts |
| **Billing** | Multi-turn bill creation with GST computation (CGST + SGST) |
| **Khata (Credit)** | Customer credit ledger with balance tracking |
| **Analytics** | Daily sales summaries, trends, GST breakdowns |
| **Documents** | PDF invoices + PPTX analysis decks |

## Architecture

```
Telegram  ──webhook──▶  AWS Lambda (src/handler.py)
                              │
                    ┌─────────┴──────────┐
                    │  PydanticAI Agent   │
                    │  (Groq / Ollama)    │
                    └─────────┬──────────┘
                              │ calls tools
              ┌───────────────┼──────────────────┐
              │               │                  │
        MCP modules     Upstash Redis       Supabase DB
        (src/mcp/)     (conv history)   (all persistent data)
```

**Technology stack:**
- **Database:** Supabase (PostgreSQL) — always running
- **Compute:** AWS Lambda — invoked only on messages (pay-per-use)
- **LLM:** Groq API (prod) / Ollama (local dev)
- **Conversation history:** Upstash Redis (serverless, HTTP)
- **Framework:** PydanticAI for structured tool calling

## Project Structure

```
savysaachi/
├── migrations/          # SQL migrations (already applied to Supabase)
├── docs/                # Detailed architecture documentation
├── src/
│   ├── handler.py       # Lambda entry point
│   ├── agent/           # PydanticAI agent, system prompt, tool registry
│   ├── mcp/             # 7 MCP modules (identity, catalogue, inventory,
│   │                    #   billing, khata, analytics, documents)
│   ├── db/              # Supabase client singleton
│   ├── redis/           # Upstash Redis client
│   ├── telegram/        # Telegram API client + update parser
│   └── utils/           # GST computation, reorder alerts
├── scripts/
│   ├── deploy.sh           # Package and deploy to Lambda
│   ├── register_webhook.py # Register Telegram webhook
│   └── test_agent_local.py # Local conversation test
├── .env.example
└── requirements.txt
```

## Setup

### 1. Prerequisites

```bash
# Python 3.12+
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Environment

```bash
cp .env.example .env
# Fill in: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY,
#          UPSTASH_REDIS_REST_URL, UPSTASH_REDIS_REST_TOKEN,
#          TELEGRAM_BOT_TOKEN, GROQ_API_KEY
```

### 3. Database

The schema is already deployed.  To verify:

```sql
-- Run in Supabase SQL editor
SELECT table_schema, table_name
FROM information_schema.tables
WHERE table_schema IN ('identity','catalogue','inventory','billing','khata','analytics')
ORDER BY table_schema, table_name;
-- Expected: 14 rows
```

### 4. Local testing

```bash
export $(cat .env | xargs)
export LLM_PROVIDER=ollama   # or groq
export TEST_TELEGRAM_USER_ID=99999
python scripts/test_agent_local.py
```

### 5. Deploy to AWS Lambda

```bash
# One-time setup
aws iam create-role --role-name kirana-agent-lambda-role \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
aws iam attach-role-policy --role-name kirana-agent-lambda-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

aws lambda create-function \
  --function-name kirana-agent \
  --runtime python3.12 \
  --handler src.handler.lambda_handler \
  --role arn:aws:iam::YOUR_ACCOUNT_ID:role/kirana-agent-lambda-role \
  --timeout 29 \
  --memory-size 512 \
  --region ap-south-1 \
  --zip-file fileb://kirana-agent.zip

# Set environment variables
aws lambda update-function-configuration \
  --function-name kirana-agent \
  --environment "Variables={TELEGRAM_BOT_TOKEN=...,SUPABASE_URL=...,SUPABASE_SERVICE_ROLE_KEY=...,UPSTASH_REDIS_REST_URL=...,UPSTASH_REDIS_REST_TOKEN=...,GROQ_API_KEY=...,LLM_PROVIDER=groq,LLM_MODEL=llama-3.3-70b-versatile}"

# Create Function URL
aws lambda create-function-url-config --function-name kirana-agent --auth-type NONE
aws lambda add-permission --function-name kirana-agent \
  --action lambda:InvokeFunctionUrl --principal "*" \
  --function-url-auth-type NONE --statement-id AllowPublicInvoke

# Register webhook
export TELEGRAM_BOT_TOKEN=your_token
python scripts/register_webhook.py --url https://YOUR_FUNCTION_URL/
python scripts/register_webhook.py --info   # verify

# Deploy subsequent updates
bash scripts/deploy.sh
```

## Workflow States

| State | Condition | Tools Available |
|---|---|---|
| `UNREGISTERED` | New user | Identity only |
| `PENDING_CATALOGUE` | Store created | Identity + Catalogue |
| `PENDING_INVENTORY` | ≥1 product added | + Inventory |
| `ACTIVE` | ≥1 stock-in done | All 7 MCPs |

## Phase 2 Extensions (scaffolded, not yet enabled)

- Multiple stores per user
- Multiple users per store (cashier, worker roles)
- Scheduled PPTX reports
- Barcode scanning
- Credit limit enforcement
- Payment reminders

## Key Design Decisions

- **Serverless:** Lambda cold starts are acceptable; agent only runs when a message arrives.
- **Stateless compute:** All state (workflow, bills, preferences) is in Supabase. Lambda can scale to N instances.
- **Idempotent operations:** Every MCP write is safe to retry (upserts, ON CONFLICT, workflow_id check).
- **ACID compliance:** Critical paths (finalize_bill, decrement_stock) use Supabase RPCs with row-level locking.
- **No hardcoded intent router:** PydanticAI's LLM reasons over the available tools and calls them naturally.
