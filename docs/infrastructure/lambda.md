# Infrastructure: AWS Lambda

**Runtime:** Python 3.12  
**Trigger:** Lambda Function URL (HTTPS, no API Gateway)  
**Deployment:** Single Lambda function, all MCP modules co-deployed

---

## Overview

The entire Kirana Agent backend runs as a single AWS Lambda function. It is invoked only when a Telegram user sends a message — the function is cold when idle. Supabase and Upstash Redis handle persistence and are always available.

---

## Lambda Function Configuration

| Setting | Value | Notes |
|---|---|---|
| **Runtime** | Python 3.12 | Latest stable Python on Lambda |
| **Handler** | `handler.lambda_handler` | Entry point in `src/handler.py` |
| **Memory** | 512 MB | Sufficient for PydanticAI + dependencies |
| **Timeout** | 29 seconds | Telegram webhook expects response within 30s |
| **Ephemeral storage** | 512 MB (`/tmp`) | For PDF/PPTX generation |
| **Architecture** | x86_64 | ARM64 (Graviton) also works and is cheaper |

---

## Lambda Function URL

No API Gateway is used. Lambda Function URL provides a direct HTTPS endpoint:

```
URL format: https://<url-id>.lambda-url.<region>.on.aws/
Auth type:  NONE (Telegram sends requests, public endpoint)
CORS:       Disabled (not needed for webhooks)
```

### Setting Up Function URL

```bash
# Create Lambda with Function URL
aws lambda create-function-url-config \
  --function-name kirana-agent \
  --auth-type NONE

# Response:
# {
#   "FunctionUrl": "https://abc123.lambda-url.ap-south-1.on.aws/"
# }
```

---

## Telegram Webhook Registration

After deploying the Lambda and obtaining the Function URL, register it as the Telegram webhook:

```bash
curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  --data "url=https://abc123.lambda-url.ap-south-1.on.aws/" \
  --data "allowed_updates=[\"message\",\"callback_query\"]" \
  --data "drop_pending_updates=true"
```

Verify webhook:
```bash
curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo"
```

---

## Handler Entry Point

```python
# src/handler.py

import json
import asyncio

async def async_handler(event: dict, context) -> dict:
    """Main async Lambda handler."""
    
    body = json.loads(event.get('body', '{}'))
    
    # Handle /new command without invoking agent
    if is_new_chat_command(body):
        response_text = await on_new_chat(get_telegram_user_id(body))
        await telegram_client.send_message(get_chat_id(body), response_text)
        return {'statusCode': 200, 'body': 'OK'}
    
    # Ignore non-message updates
    if not is_message_update(body):
        return {'statusCode': 200, 'body': 'OK'}
    
    telegram_user_id = get_telegram_user_id(body)
    user_message = get_message_text(body)
    chat_id = get_chat_id(body)
    
    # Pre-agent: load context
    context = await load_context(telegram_user_id)
    
    # Run agent
    result = await agent.run(
        user_message,
        context.conversation_history,
        context.store_context,
        context.tools
    )
    
    # Post-agent: save history
    await upstash_client.append_messages(telegram_user_id, [
        {'role': 'user', 'content': user_message},
        {'role': 'assistant', 'content': result.text}
    ])
    
    # Send response
    await telegram_client.send_message(chat_id, result.text)
    
    return {'statusCode': 200, 'body': 'OK'}


def lambda_handler(event: dict, context) -> dict:
    """Sync Lambda entry point — runs async handler."""
    return asyncio.get_event_loop().run_until_complete(
        async_handler(event, context)
    )
```

---

## Environment Variables

All secrets and configuration are stored as Lambda environment variables (or AWS Secrets Manager in production):

| Variable | Description | Required |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram Bot API token from @BotFather | ✅ |
| `SUPABASE_URL` | Supabase project URL | ✅ |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase service role key (bypasses RLS) | ✅ |
| `UPSTASH_REDIS_REST_URL` | Upstash Redis REST endpoint | ✅ |
| `UPSTASH_REDIS_REST_TOKEN` | Upstash Redis auth token | ✅ |
| `GROQ_API_KEY` | Groq API key (prod LLM) | ✅ prod |
| `OLLAMA_BASE_URL` | Ollama server URL (dev LLM) | ✅ dev |
| `LLM_PROVIDER` | `"groq"` or `"ollama"` | ✅ |
| `LLM_MODEL` | Model name (e.g. `"llama-3.1-70b-versatile"`) | ✅ |
| `LAMBDA_ENV` | `"dev"` or `"prod"` | ✅ |
| `DRAFT_BILL_TTL_HOURS` | Draft bill expiry (default: `"4"`) | ❌ |
| `MAX_HISTORY_MESSAGES` | Redis history window (default: `"20"`) | ❌ |

---

## Dependency Packaging

Lambda requires dependencies to be packaged with the function. Use a Lambda layer or zip deployment:

```bash
# Install dependencies to a local directory
pip install -r requirements.txt -t ./package/

# Copy source code
cp -r src/ ./package/

# Zip for deployment
cd package && zip -r ../kirana-agent.zip . && cd ..
```

### Key Dependencies

```
pydantic-ai          # Agent harness
pydantic             # Data validation
supabase             # Supabase Python client
httpx                # Async HTTP (Upstash Redis calls, Telegram API)
python-telegram-bot  # OR httpx calls to Telegram API directly
python-pptx          # PPTX generation
# PDF library: TBD (reportlab or fpdf2)
```

---

## Cold Start Mitigation

Lambda cold starts add ~500ms-2s latency on first invocation. For a Telegram bot, this is acceptable (Telegram has a 30s webhook timeout). Mitigation strategies:

| Strategy | Implementation |
|---|---|
| **Keep dependencies lean** | Only import what's needed; avoid heavy unused packages |
| **Provisioned concurrency** | Optional for Phase 2 if cold starts become a problem |
| **Lazy initialization** | Initialize Supabase/Redis clients once at module level (reused across warm invocations) |
| **ARM64 (Graviton)** | Faster cold starts, cheaper per invocation |

```python
# Module-level initialization (reused across warm Lambda invocations)
# src/db/supabase_client.py
from supabase import create_client, Client
import os

_client: Client = None

def get_supabase_client() -> Client:
    global _client
    if _client is None:
        _client = create_client(
            os.environ["SUPABASE_URL"],
            os.environ["SUPABASE_SERVICE_ROLE_KEY"]
        )
    return _client
```

---

## AWS Free Tier

| Resource | Free Tier | Estimated Usage |
|---|---|---|
| Lambda invocations | 1M/month | ~50K messages/month = well within free |
| Lambda compute | 400K GB-seconds/month | 512MB × 5s avg = ~50K GB-sec for 100K messages |
| Lambda Function URL | No additional cost | Free |

---

## Region Recommendation

Deploy to `ap-south-1` (Mumbai) for lowest latency to Indian users.

---

## Deployment Script

```bash
# deploy.sh
FUNCTION_NAME="kirana-agent"
REGION="ap-south-1"

# Package
pip install -r requirements.txt -t ./package/
cp -r src/ ./package/
cd package && zip -r ../kirana-agent.zip . && cd ..

# Deploy
aws lambda update-function-code \
  --function-name $FUNCTION_NAME \
  --zip-file fileb://kirana-agent.zip \
  --region $REGION

# Update env vars
aws lambda update-function-configuration \
  --function-name $FUNCTION_NAME \
  --environment "Variables={LLM_PROVIDER=groq,LLM_MODEL=llama-3.1-70b-versatile,...}" \
  --region $REGION
```

---

## Phase 2 Scalability

| Change | What to Do |
|---|---|
| MCP extraction to separate Lambda | Create new Lambda per MCP, update import to HTTP call |
| API Gateway | Add if custom domain, rate limiting, or auth headers needed |
| Provisioned concurrency | Add if cold start SLA becomes an issue |
| Container image | Use if dependencies exceed 50MB zip limit (python-pptx + PDF lib + pydantic-ai) |
