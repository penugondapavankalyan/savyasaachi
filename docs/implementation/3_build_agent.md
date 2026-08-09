# Implementation Guide 3: Build PydanticAI Agent

**Order:** Third — after MCP modules are built and tested.  
**Reference Docs:** `docs/agent/pydantic_ai_agent.md`, `docs/agent/workflow_state_machine.md`, `docs/agent/guardrails.md`

---

## Prerequisites

- MCP modules built and tested (Guide 2 complete)
- PydanticAI installed: `pip install pydantic-ai`
- Groq API key OR Ollama running locally
- Upstash Redis account and credentials

---

## Step 1: Install Dependencies

```bash
pip install pydantic-ai
pip install groq          # For Groq provider
pip install httpx         # For Upstash Redis HTTP client
```

For local dev with Ollama:
```bash
# Install Ollama: https://ollama.ai
ollama pull llama3.1  # or whichever model you want
```

---

## Step 2: Upstash Redis Client

Build the conversation history client first — the agent depends on it.

```python
# src/redis/upstash_client.py
# Full implementation documented in docs/agent/conversation_history.md
```

Key methods to implement:
- `get_conversation(telegram_user_id, max_messages=20) -> List[dict]`
- `append_messages(telegram_user_id, new_messages) -> None`
- `clear_conversation(telegram_user_id) -> None`
- `ping() -> bool`

Test the Redis client standalone before integrating with the agent.

---

## Step 3: Agent Configuration Models

```python
# src/agent/config.py
from pydantic import BaseModel
from typing import Optional

class AgentConfig(BaseModel):
    llm_provider: str      # 'groq' | 'ollama'
    llm_model: str         # e.g., 'llama-3.1-70b-versatile'
    groq_api_key: Optional[str] = None
    ollama_base_url: Optional[str] = "http://localhost:11434"
    max_history_messages: int = 20
    draft_bill_ttl_hours: int = 4

class StoreContext(BaseModel):
    shop_name: str
    gstin: Optional[str]
    state_code: str
    state_name: str         # Derived from state_code
    default_payment_mode: str
    preferences: dict
    workflow_state: str     # UNREGISTERED | PENDING_CATALOGUE | etc.
    active_draft_bill_id: Optional[str]
    store_id: Optional[str]
    user_id: Optional[str]
    telegram_user_id: int
```

---

## Step 4: System Prompt Builder

```python
# src/agent/system_prompt.py
from src.agent.config import StoreContext
from datetime import date

STATE_CODE_TO_NAME = {
    '29': 'Karnataka', '27': 'Maharashtra', '07': 'Delhi',
    '33': 'Tamil Nadu', '36': 'Telangana', '32': 'Kerala',
    # Add all state codes
}

def build_system_prompt(context: StoreContext) -> str:
    state_name = STATE_CODE_TO_NAME.get(context.state_code, context.state_code)
    today = date.today().strftime("%A, %d %B %Y")
    
    # Build preferences summary
    prefs = []
    if context.preferences.get('preferred_brands'):
        for item, brand in context.preferences['preferred_brands'].items():
            prefs.append(f"  - Default {item}: {brand}")
    pref_text = '\n'.join(prefs) if prefs else "  None set"
    
    # State-specific system prompt
    workflow_guidance = {
        'UNREGISTERED': "Guide the user through registering their store.",
        'PENDING_CATALOGUE': "Prompt the user to add their first product to the catalogue.",
        'PENDING_INVENTORY': "Prompt the user to add initial stock for their products.",
        'ACTIVE': "Help the user manage their store — billing, inventory, khata, analytics."
    }
    
    return f"""You are a helpful store assistant for {context.shop_name or 'a kirana store'}.
You help the owner manage their Indian kirana/grocery store through natural conversation.

TODAY: {today}
STORE: {context.shop_name or 'Not set up yet'}
GSTIN: {context.gstin or 'Not registered'}
STATE: {state_name} (Code: {context.state_code}) — Intra-state (CGST + SGST split)
DEFAULT PAYMENT: {context.default_payment_mode}

OWNER PREFERENCES:
{pref_text}

CURRENT STATE: {context.workflow_state}
ACTIVE BILL: {'Bill in progress (ID: ' + context.active_draft_bill_id[:8] + '...)' if context.active_draft_bill_id else 'None'}

YOUR TASK: {workflow_guidance.get(context.workflow_state, 'Help the owner.')}

RULES:
- All amounts in ₹ (Indian Rupees). Never invent prices — always use tool data.
- Loose items (sugar, rice, dal): always 0% GST, no HSN needed.
- Packaged/branded items: apply GST per their slab (CGST + SGST equally split).
- Never sell more stock than is available — check availability before adding to bill.
- Never sell below cost price without explicit owner confirmation.
- If product reference is ambiguous (multiple matches), always ask which one — never guess.
- Owner preferences are permanent. Confirm and apply them immediately.
- Be concise. Real shopkeepers are busy. Short, clear responses.
- Format currency as ₹X.XX (e.g., ₹14.00, ₹275.50)."""
```

---

## Step 5: Tool Registration Function

```python
# src/agent/tool_registry.py
from src.mcp import MCPInstances
from typing import List, Callable

def get_tools_for_state(
    workflow_state: str,
    mcps: MCPInstances
) -> List[Callable]:
    # Full implementation documented in docs/agent/pydantic_ai_agent.md
    # Returns the correct tool subset for the current workflow state
    ...
```

---

## Step 6: Build the Agent

```python
# src/agent/kirana_agent.py
from pydantic_ai import Agent
from pydantic_ai.models.groq import GroqModel
from pydantic_ai.models.ollama import OllamaModel
import os

from src.agent.config import AgentConfig, StoreContext
from src.agent.system_prompt import build_system_prompt
from src.agent.tool_registry import get_tools_for_state
from src.mcp import get_mcp_instances

class KiranaAgent:
    def __init__(self):
        self.config = AgentConfig(
            llm_provider=os.environ.get("LLM_PROVIDER", "groq"),
            llm_model=os.environ.get("LLM_MODEL", "llama-3.1-70b-versatile"),
            groq_api_key=os.environ.get("GROQ_API_KEY"),
            ollama_base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        )
        self.mcps = get_mcp_instances()
        self._model = self._build_model()
    
    def _build_model(self):
        if self.config.llm_provider == 'groq':
            return GroqModel(
                model_name=self.config.llm_model,
                api_key=self.config.groq_api_key
            )
        else:
            return OllamaModel(
                model_name=self.config.llm_model,
                base_url=self.config.ollama_base_url
            )
    
    async def run(
        self,
        user_message: str,
        conversation_history: list,
        store_context: StoreContext
    ) -> str:
        tools = get_tools_for_state(store_context.workflow_state, self.mcps)
        system_prompt = build_system_prompt(store_context)
        
        agent = Agent(
            model=self._model,
            system_prompt=system_prompt,
            tools=tools
        )
        
        result = await agent.run(
            user_message,
            message_history=conversation_history
        )
        
        return result.data


# Module-level singleton
_agent: KiranaAgent = None

def get_agent() -> KiranaAgent:
    global _agent
    if _agent is None:
        _agent = KiranaAgent()
    return _agent
```

---

## Step 7: Pre-Agent Context Loader

```python
# src/agent/context_loader.py
from src.mcp import get_mcp_instances
from src.redis.upstash_client import UpstashRedisClient
from src.agent.config import StoreContext

redis = UpstashRedisClient()

async def load_agent_context(telegram_user_id: int) -> StoreContext:
    mcps = get_mcp_instances()
    
    # 1. Ensure workflow_state record exists (idempotent upsert)
    await mcps.identity.ensure_workflow_state(telegram_user_id)
    
    # 2. Load current workflow state
    workflow = await mcps.identity.get_workflow_state(telegram_user_id)
    
    # 3. Check and handle draft bill expiry
    if workflow.active_draft_bill_id:
        await handle_draft_expiry(workflow.active_draft_bill_id, telegram_user_id)
        # Re-read state after potential expiry handling
        workflow = await mcps.identity.get_workflow_state(telegram_user_id)
    
    # 4. Load store context
    store = None
    if workflow.store_id:
        store = await mcps.identity.get_store(telegram_user_id)
    
    return StoreContext(
        telegram_user_id=telegram_user_id,
        shop_name=store.shop_name if store else None,
        gstin=store.gstin if store else None,
        state_code=store.state_code if store else '29',
        state_name=STATE_CODE_MAP.get(store.state_code if store else '29'),
        default_payment_mode=store.default_payment_mode if store else 'CASH',
        preferences=store.preferences if store else {},
        workflow_state=workflow.current_state,
        active_draft_bill_id=workflow.active_draft_bill_id,
        store_id=workflow.store_id,
        user_id=workflow.user_id
    )
```

---

## Step 8: Local Dev Testing

Test the agent locally before Lambda deployment:

```python
# scripts/test_agent_local.py
import asyncio
import os
from src.agent.kirana_agent import get_agent
from src.agent.context_loader import load_agent_context
from src.redis.upstash_client import UpstashRedisClient

# Set environment variables
os.environ["LLM_PROVIDER"] = "ollama"
os.environ["LLM_MODEL"] = "llama3.1"
os.environ["SUPABASE_URL"] = "..."
os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "..."
os.environ["UPSTASH_REDIS_REST_URL"] = "..."
os.environ["UPSTASH_REDIS_REST_TOKEN"] = "..."

async def test():
    telegram_user_id = 99999  # Test user ID
    redis = UpstashRedisClient()
    agent = get_agent()
    
    test_messages = [
        "hello",
        "Ramesh General Store, GSTIN 29AABCU9603R1ZX",
        "add new item: Maggi 70g, branded, 12% GST, MRP 14, cost 12, reorder 20",
        "50 packets of Maggi came in",
        "make a bill: 4 Maggi, UPI"
    ]
    
    for msg in test_messages:
        print(f"\nUser: {msg}")
        context = await load_agent_context(telegram_user_id)
        history = await redis.get_conversation(telegram_user_id)
        response = await agent.run(msg, history, context)
        print(f"Agent: {response}")
        await redis.append_messages(telegram_user_id, [
            {'role': 'user', 'content': msg},
            {'role': 'assistant', 'content': response}
        ])

asyncio.run(test())
```

---

## Validation Checklist

- [ ] Agent initializes with both Groq and Ollama models
- [ ] System prompt includes correct store context
- [ ] Tool list changes based on workflow state
- [ ] Multi-turn bill test: add items across 2 messages → finalize
- [ ] Disambiguation test: "atta" with 2 products → agent asks which one
- [ ] Oversell test: request more than available → agent reports partial/none
- [ ] Preference test: set "always UPI" → next bill uses UPI by default
- [ ] /new chat test: history cleared, store data intact
- [ ] GST test: 4 Maggi at ₹14 → taxable ₹56, CGST ₹3.36, SGST ₹3.36, total ₹62.72
