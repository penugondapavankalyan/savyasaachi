# Implementation Guide 4: Build Telegram Handler

**Order:** Fourth — Lambda entry point connecting Telegram to the agent.  
**Reference Docs:** `docs/infrastructure/lambda.md`, `docs/agent/conversation_history.md`

---

## Prerequisites

- Agent built and tested locally (Guide 3 complete)
- Telegram Bot created via @BotFather
- `TELEGRAM_BOT_TOKEN` obtained

---

## Step 1: Create Telegram Bot

1. Message @BotFather on Telegram
2. Send `/newbot`
3. Choose name: "Kirana Store Manager"
4. Choose username: `@YourKiranaBot`
5. Copy the Bot Token

---

## Step 2: Telegram Client

```python
# src/telegram/telegram_client.py
import httpx
import os
from typing import Optional

class TelegramClient:
    def __init__(self):
        self.token = os.environ["TELEGRAM_BOT_TOKEN"]
        self.base_url = f"https://api.telegram.org/bot{self.token}"
    
    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str = "Markdown",
        reply_to_message_id: Optional[int] = None
    ) -> dict:
        """Send a text message to a Telegram chat."""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "reply_to_message_id": reply_to_message_id
                },
                timeout=10.0
            )
        return response.json()
    
    async def send_document(
        self,
        chat_id: int,
        file_path: str,
        caption: Optional[str] = None
    ) -> dict:
        """Send a file (PDF or PPTX) to a Telegram chat."""
        async with httpx.AsyncClient() as client:
            with open(file_path, 'rb') as f:
                response = await client.post(
                    f"{self.base_url}/sendDocument",
                    data={
                        "chat_id": str(chat_id),
                        "caption": caption or ""
                    },
                    files={"document": f},
                    timeout=30.0  # Files can take longer
                )
        return response.json()
    
    async def send_typing_action(self, chat_id: int) -> None:
        """Show typing indicator while agent processes."""
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{self.base_url}/sendChatAction",
                json={"chat_id": chat_id, "action": "typing"}
            )
    
    async def set_webhook(self, webhook_url: str) -> dict:
        """Register Lambda Function URL as Telegram webhook."""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/setWebhook",
                json={
                    "url": webhook_url,
                    "allowed_updates": ["message", "callback_query"],
                    "drop_pending_updates": True
                }
            )
        return response.json()


# Singleton
_telegram: TelegramClient = None

def get_telegram_client() -> TelegramClient:
    global _telegram
    if _telegram is None:
        _telegram = TelegramClient()
    return _telegram
```

---

## Step 3: Update Parsing Utilities

```python
# src/telegram/update_parser.py
from typing import Optional

def get_telegram_user_id(update: dict) -> Optional[int]:
    """Extract telegram user ID from webhook update."""
    msg = update.get('message') or update.get('edited_message')
    if msg:
        return msg.get('from', {}).get('id')
    return None

def get_chat_id(update: dict) -> Optional[int]:
    """Extract chat ID from webhook update."""
    msg = update.get('message') or update.get('edited_message')
    if msg:
        return msg.get('chat', {}).get('id')
    return None

def get_message_text(update: dict) -> Optional[str]:
    """Extract message text from webhook update."""
    msg = update.get('message')
    if msg:
        return msg.get('text')
    return None

def get_username(update: dict) -> Optional[str]:
    msg = update.get('message')
    if msg:
        return msg.get('from', {}).get('username')
    return None

def get_first_name(update: dict) -> Optional[str]:
    msg = update.get('message')
    if msg:
        return msg.get('from', {}).get('first_name')
    return None

def is_new_chat_command(update: dict) -> bool:
    text = get_message_text(update)
    return text is not None and text.strip().lower() in ['/new', '/start /new']

def is_message_update(update: dict) -> bool:
    """Return True if this is a regular message (not channel post, etc.)."""
    return 'message' in update and 'text' in update.get('message', {})
```

---

## Step 4: Main Lambda Handler

```python
# src/handler.py
import json
import asyncio
import os
from datetime import datetime

from src.telegram.telegram_client import get_telegram_client
from src.telegram.update_parser import (
    get_telegram_user_id, get_chat_id, get_message_text,
    get_username, get_first_name, is_new_chat_command, is_message_update
)
from src.agent.kirana_agent import get_agent
from src.agent.context_loader import load_agent_context
from src.redis.upstash_client import UpstashRedisClient
from src.mcp import get_mcp_instances


async def async_handler(event: dict, context) -> dict:
    """Main async Lambda handler."""
    
    # Parse Telegram webhook payload
    try:
        body = json.loads(event.get('body', '{}'))
    except json.JSONDecodeError:
        return {'statusCode': 400, 'body': 'Invalid JSON'}
    
    # Ignore empty or non-message updates
    if not body or not is_message_update(body):
        return {'statusCode': 200, 'body': 'OK'}
    
    telegram_user_id = get_telegram_user_id(body)
    chat_id = get_chat_id(body)
    user_message = get_message_text(body)
    
    if not telegram_user_id or not chat_id or not user_message:
        return {'statusCode': 200, 'body': 'OK'}
    
    telegram = get_telegram_client()
    redis = UpstashRedisClient()
    
    # Handle /new command (no agent invocation)
    if is_new_chat_command(body):
        await redis.clear_conversation(telegram_user_id)
        
        # Cancel any open draft bill
        mcps = get_mcp_instances()
        workflow = await mcps.identity.get_workflow_state(telegram_user_id)
        if workflow and workflow.active_draft_bill_id:
            await mcps.billing.cancel_draft_bill(workflow.active_draft_bill_id)
        
        await telegram.send_message(
            chat_id,
            "🆕 Chat cleared!\n\nYour store data, products, inventory, bills and preferences are all intact.\n\nWhat would you like to do?"
        )
        return {'statusCode': 200, 'body': 'OK'}
    
    # Show typing indicator
    await telegram.send_typing_action(chat_id)
    
    # Load pre-agent context
    try:
        store_context = await load_agent_context(telegram_user_id)
        
        # Ensure user record exists (first-time users)
        mcps = get_mcp_instances()
        await mcps.identity.register_user(
            telegram_user_id=telegram_user_id,
            telegram_username=get_username(body),
            first_name=get_first_name(body)
        )
        
    except Exception as e:
        await telegram.send_message(chat_id, "⚠️ Having trouble connecting. Please try again.")
        return {'statusCode': 200, 'body': 'OK'}
    
    # Load conversation history
    history = await redis.get_conversation(telegram_user_id)
    
    # Run PydanticAI agent
    try:
        agent = get_agent()
        response_text = await agent.run(user_message, history, store_context)
    except Exception as e:
        response_text = "⚠️ I encountered an error. Please try again."
    
    # Save updated conversation history
    timestamp = datetime.utcnow().isoformat()
    await redis.append_messages(telegram_user_id, [
        {'role': 'user', 'content': user_message, 'timestamp': timestamp},
        {'role': 'assistant', 'content': response_text, 'timestamp': timestamp}
    ])
    
    # Send response to Telegram
    await telegram.send_message(chat_id, response_text)
    
    return {'statusCode': 200, 'body': 'OK'}


def lambda_handler(event: dict, context) -> dict:
    """Synchronous Lambda entry point."""
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(async_handler(event, context))
```

---

## Step 5: Webhook Registration Script

```python
# scripts/register_webhook.py
import asyncio
import os
from src.telegram.telegram_client import TelegramClient

async def register():
    client = TelegramClient()
    
    lambda_function_url = os.environ["LAMBDA_FUNCTION_URL"]
    result = await client.set_webhook(lambda_function_url)
    
    print(f"Webhook registration: {result}")

asyncio.run(register())
```

---

## Step 6: Local Development Testing

Use [ngrok](https://ngrok.com) to expose local server for Telegram webhook testing:

```bash
# Run local server
python -m uvicorn src.local_server:app --port 8080

# In another terminal
ngrok http 8080

# Register ngrok URL as webhook
LAMBDA_FUNCTION_URL="https://abc123.ngrok.io" python scripts/register_webhook.py
```

Or test by directly calling `lambda_handler` with a mock event:

```python
# tests/test_handler.py
import json
from src.handler import lambda_handler

mock_event = {
    "body": json.dumps({
        "update_id": 12345,
        "message": {
            "message_id": 1,
            "from": {
                "id": 987654321,
                "first_name": "Test",
                "username": "testuser"
            },
            "chat": {"id": 987654321},
            "text": "hello"
        }
    })
}

result = lambda_handler(mock_event, None)
print(result)
```

---

## Validation Checklist

- [ ] Webhook registered with Telegram successfully
- [ ] `getWebhookInfo` shows no pending updates error
- [ ] First message creates user + prompts registration
- [ ] `/new` command clears history, confirms store data intact
- [ ] Agent response sent to correct Telegram chat
- [ ] Typing indicator shows while agent processes
- [ ] Non-message updates (channel posts, etc.) ignored with 200 OK
- [ ] Invalid JSON body returns 400
- [ ] Handler returns within Telegram's 30-second window
