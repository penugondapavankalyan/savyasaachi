# PydanticAI Agent Design

**File:** `src/agent/kirana_agent.py`

---

## Overview

The Kirana Agent is a PydanticAI agent. It receives a message context (system prompt + conversation history + user message), reasons over it using an LLM (Ollama dev / Groq prod), selects and calls MCP tools, feeds results back, and produces a final natural language response. The model orchestrates all tool calls — there is no hardcoded intent router or regex dispatch.

---

## Agent Initialization

```python
from pydantic_ai import Agent
from pydantic_ai.models.groq import GroqModel
from pydantic_ai.models.ollama import OllamaModel

class KiranaAgent:
    def __init__(self, config: AgentConfig):
        self.config = config
        self.model = self._init_model(config)
        self.agent = Agent(
            model=self.model,
            system_prompt=self._build_system_prompt,  # Dynamic, injected per request
            tools=[]  # Tools are registered per workflow state
        )
        self._register_tools()
    
    def _init_model(self, config: AgentConfig):
        if config.llm_provider == 'groq':
            return GroqModel(
                model_name=config.llm_model,  # e.g., 'llama-3.1-70b-versatile'
                api_key=config.groq_api_key
            )
        elif config.llm_provider == 'ollama':
            return OllamaModel(
                model_name=config.llm_model,  # e.g., 'llama3.1'
                base_url=config.ollama_base_url
            )
```

---

## System Prompt Design

The system prompt is **dynamically built per request** — it injects the store's context so the model never needs to ask for basic information the system already knows.

```python
def _build_system_prompt(self, store_context: StoreContext) -> str:
    return f"""You are a helpful assistant for {store_context.shop_name}, an Indian kirana store.

STORE CONTEXT:
- Shop: {store_context.shop_name}
- GSTIN: {store_context.gstin or 'Not registered'}
- State: {store_context.state_name} (Code: {store_context.state_code})
- Default Payment: {store_context.default_payment_mode}
- Today's Date: {today_date}

PREFERENCES:
{store_context.preferences_summary}

RULES YOU MUST FOLLOW:
1. All prices are in ₹ (INR). Never invent prices — always fetch from the database via tools.
2. Loose items (sugar, rice, dal, loose atta/flour) have 0% GST. Never apply GST to them.
3. Branded/packaged items have GST per their HSN code slab. Always compute CGST + SGST (equal split).
4. Never sell stock that isn't available — check availability before adding to a bill.
5. Never sell below cost price without explicit owner confirmation.
6. If a product reference is ambiguous (e.g., "atta" matches multiple SKUs), ask a clarifying question.
7. Preferences (default payment mode, preferred brands) are remembered permanently.
8. When a customer pays more than they owe, confirm with the owner before recording.
9. All currency values are rounded to 2 decimal places.

CURRENT WORKFLOW STATE: {store_context.workflow_state}
ACTIVE DRAFT BILL: {store_context.active_draft_bill_id or 'None'}

You have access to tools. Use them to fetch data, perform operations, and verify before acting.
Think step by step. If unsure, ask — do not guess."""
```

---

## Tool Registration by Workflow State

The agent's tool set is determined by the user's workflow state **before** the LLM is invoked. This keeps the tool list lean and the prompt smaller.

```python
def get_tools_for_state(
    workflow_state: str,
    mcp_instances: MCPInstances
) -> List[Callable]:
    
    # Base tools always available
    base_tools = [
        mcp_instances.identity.check_user_registration,
        mcp_instances.identity.get_store,
        mcp_instances.identity.update_store_preferences,
        mcp_instances.identity.get_workflow_state,
    ]
    
    if workflow_state == 'UNREGISTERED':
        return base_tools + [
            mcp_instances.identity.register_user,
            mcp_instances.identity.create_store,
        ]
    
    elif workflow_state == 'PENDING_CATALOGUE':
        return base_tools + [
            mcp_instances.catalogue.add_product,
            mcp_instances.catalogue.search_products,
            mcp_instances.catalogue.list_products,
        ]
    
    elif workflow_state == 'PENDING_INVENTORY':
        return base_tools + [
            mcp_instances.catalogue.add_product,
            mcp_instances.catalogue.search_products,
            mcp_instances.catalogue.list_products,
            mcp_instances.catalogue.update_product,
            mcp_instances.inventory.receive_stock,
            mcp_instances.inventory.get_stock,
        ]
    
    elif workflow_state == 'ACTIVE':
        return base_tools + [
            # Catalogue tools
            mcp_instances.catalogue.add_product,
            mcp_instances.catalogue.search_products,
            mcp_instances.catalogue.list_products,
            mcp_instances.catalogue.update_product,
            mcp_instances.catalogue.deactivate_product,
            # Inventory tools
            mcp_instances.inventory.receive_stock,
            mcp_instances.inventory.get_stock,
            mcp_instances.inventory.get_all_stock,
            mcp_instances.inventory.check_availability,
            mcp_instances.inventory.get_low_stock_items,
            mcp_instances.inventory.get_stock_movements,
            # Billing tools
            mcp_instances.billing.create_draft_bill,
            mcp_instances.billing.add_item_to_draft,
            mcp_instances.billing.remove_item_from_draft,
            mcp_instances.billing.update_item_quantity,
            mcp_instances.billing.get_draft_bill,
            mcp_instances.billing.finalize_bill,
            mcp_instances.billing.cancel_draft_bill,
            mcp_instances.billing.get_bill,
            mcp_instances.billing.get_bills_by_date,
            # Khata tools
            mcp_instances.khata.add_customer,
            mcp_instances.khata.get_customer,
            mcp_instances.khata.add_credit_entry,
            mcp_instances.khata.add_payment_entry,
            mcp_instances.khata.get_balance,
            mcp_instances.khata.get_khata_history,
            mcp_instances.khata.list_customers_with_balances,
            # Analytics tools
            mcp_instances.analytics.get_daily_summary,
            mcp_instances.analytics.close_day,
            mcp_instances.analytics.get_sales_trend,
            mcp_instances.analytics.get_top_items,
            mcp_instances.analytics.get_stock_health,
            mcp_instances.analytics.get_gst_summary,
            # Document tools
            mcp_instances.documents.generate_invoice_pdf,
            mcp_instances.documents.generate_analysis_pptx,
        ]
```

---

## Control Loop (Observe → Reason → Act → Feed Back → Continue)

PydanticAI's agent loop handles this natively. The agent:

1. **Observes:** Receives `[system_prompt, conversation_history, user_message]`
2. **Reasons:** LLM decides what tool(s) to call (or to respond directly)
3. **Acts:** Calls tool(s) — may chain multiple tool calls in one turn
4. **Feeds back:** Tool results are returned to the model as tool-use responses
5. **Continues:** Model either calls more tools or produces a final text response

```python
async def run(
    self,
    user_message: str,
    conversation_history: List[Message],
    store_context: StoreContext
) -> AgentResponse:
    
    # Build messages list: history + new user message
    messages = conversation_history + [{"role": "user", "content": user_message}]
    
    # Run agent (PydanticAI handles the tool-calling loop)
    result = await self.agent.run(
        user_message,
        message_history=messages,
        deps=store_context  # Store context available to all tool functions
    )
    
    return AgentResponse(
        text=result.data,
        messages=result.all_messages(),  # For saving to Redis
        tool_calls_made=result.usage()
    )
```

---

## Multi-Tool Chaining Example

For the message "make a bill: 2kg sugar, 1 Aashirvaad atta, UPI":

```
LLM Turn 1:
  → Calls: search_products(query="sugar")
  → Calls: search_products(query="Aashirvaad atta")
  
Tool Results:
  → sugar: {product_id: "p1", name: "Sugar", is_loose: true, gst_rate: 0}
  → atta: {product_id: "p2", name: "Aashirvaad Atta 5kg", gst_rate: 5}

LLM Turn 2 (still reasoning):
  → Calls: create_draft_bill(store_id, telegram_user_id)
  → Calls: check_availability(store_id, "p1", 2)
  → Calls: check_availability(store_id, "p2", 1)
  
Tool Results:
  → sugar: FULL (8.5 kg available)
  → atta: FULL (5 packets available)
  → draft_bill: {draft_bill_id: "d1", workflow_id: "abc-123"}

LLM Turn 3:
  → Calls: add_item_to_draft("d1", "p1", 2)
  → Calls: add_item_to_draft("d1", "p2", 1)
  
Tool Results:
  → both added successfully

LLM Turn 4:
  → Calls: get_draft_bill("d1")
  
Tool Result:
  → {items: [sugar 2kg ₹90, atta 1 ₹288.75], subtotal ₹378, total ₹378 (sugar 0% GST, atta 5%)}

LLM Final Response:
  "Bill ready:
   • Sugar — 2kg × ₹45 = ₹90.00 (0% GST)
   • Aashirvaad Atta 5kg — 1 × ₹275 = ₹275.00 + CGST ₹6.88 + SGST ₹6.87 = ₹288.75
   
   Subtotal: ₹365.00 | GST: ₹13.75 | Total: ₹378.75
   Payment: UPI
   
   Confirm to finalize or say 'add more items'?"
```

---

## LLM Configuration

### Groq (Production)
```python
# Recommended model for production: speed + quality balance
model = GroqModel(
    model_name="llama-3.1-70b-versatile",  # or "llama-3.3-70b-versatile"
    api_key=os.environ["GROQ_API_KEY"]
)
```

### Ollama (Development)
```python
# Recommended model for dev: llama3.1 or mistral
model = OllamaModel(
    model_name="llama3.1",
    base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
)
```

### Switching Models
```python
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "groq")
LLM_MODEL = os.environ.get("LLM_MODEL", "llama-3.1-70b-versatile")

if LLM_PROVIDER == "groq":
    model = GroqModel(model_name=LLM_MODEL, api_key=GROQ_API_KEY)
else:
    model = OllamaModel(model_name=LLM_MODEL, base_url=OLLAMA_BASE_URL)
```

---

## Clarifying Questions (Agent-Driven, Not Hardcoded)

Per PDF §3: when a request is ambiguous, **the model must ask** — not a hardcoded branch.

```
Owner: "add atta"
→ search_products("atta") returns 2 results
→ LLM sees two products, no preference set
→ LLM Response: "Which atta do you want?
   1. Aashirvaad Atta 5kg (₹275, 5% GST)
   2. Pillsbury Atta 1kg (₹55, 5% GST)"
```

If a `preferred_brands.atta` preference is set:
```
→ LLM sees preference: "atta = Aashirvaad Atta 5kg"
→ LLM auto-selects Aashirvaad, no question needed
→ LLM Response: "Added Aashirvaad Atta 5kg (using your default). Anything else?"
```

---

## `/new` Chat Handling

When owner sends `/new`:
1. Lambda handler detects `/new` command (before agent)
2. Calls `upstash_client.delete_conversation(telegram_user_id)`
3. Does NOT clear Supabase data (bills, preferences, inventory — all preserved)
4. Returns: "Chat cleared! Your store data and preferences are intact. What would you like to do?"
5. Agent is NOT invoked for `/new` — it is handled at the handler layer
