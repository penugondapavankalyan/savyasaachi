# Implementation Guide 2: Build MCP Modules

**Order:** Second — after schema is deployed and validated.  
**Reference Docs:** `docs/mcp/` (all MCP module docs)

---

## Prerequisites

- Schema deployed and validated (Guide 1 complete)
- Python 3.12 installed locally
- Virtual environment set up: `python -m venv .venv && source .venv/bin/activate`
- Supabase Python client: `pip install supabase`

---

## Project Structure

```
src/
├── mcp/
│   ├── __init__.py             # MCPInstances container + get_mcp_instances() singleton
│   ├── identity/
│   │   ├── __init__.py
│   │   ├── identity_mcp.py
│   │   └── models.py           # Pydantic input/output models
│   ├── catalogue/
│   │   ├── __init__.py
│   │   ├── catalogue_mcp.py
│   │   └── models.py
│   ├── inventory/
│   │   ├── __init__.py
│   │   ├── inventory_mcp.py
│   │   └── models.py
│   ├── billing/
│   │   ├── __init__.py
│   │   ├── billing_mcp.py
│   │   └── models.py
│   ├── khata/
│   │   ├── __init__.py
│   │   ├── khata_mcp.py
│   │   └── models.py
│   ├── analytics/
│   │   ├── __init__.py
│   │   ├── analytics_mcp.py
│   │   └── models.py
│   ├── documents/
│   │   ├── __init__.py
│   │   ├── documents_mcp.py
│   │   ├── pdf_renderer.py    # Abstract interface
│   │   └── pptx_renderer.py   # python-pptx implementation
│   └── payments/
│       ├── __init__.py
│       ├── payments_mcp.py    # PaymentsMCP — owns payments.payments
│       └── models.py          # PaymentResult, PaymentHistoryResult, etc.
├── redis/
│   └── upstash_client.py      # Conversation history + pending_payment key
└── db/
    └── supabase_client.py     # Singleton client
```

---

## Step 1: Supabase Client Singleton

```python
# src/db/supabase_client.py
from supabase import create_client, Client
import os

_client: Client = None

def get_client() -> Client:
    global _client
    if _client is None:
        _client = create_client(
            os.environ["SUPABASE_URL"],
            os.environ["SUPABASE_SERVICE_ROLE_KEY"]
        )
    return _client
```

---

## Step 2: Pydantic Models for Each MCP

Each MCP module has a `models.py` with input and output Pydantic models. This ensures type safety and makes PydanticAI tool function signatures clear.

**Pattern for each model file:**

```python
# src/mcp/identity/models.py
from pydantic import BaseModel
from typing import Optional, List

class RegisterUserInput(BaseModel):
    telegram_user_id: int
    telegram_username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None

class RegisterUserResult(BaseModel):
    user_id: str
    already_existed: bool
    message: str

class CreateStoreInput(BaseModel):
    telegram_user_id: int
    shop_name: str
    gstin: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    state_code: str = '29'

class CreateStoreResult(BaseModel):
    store_id: str
    already_existed: bool
    shop_name: str
    message: str

# ... more models
```

---

## Step 3: MCP Module Pattern

Each MCP module follows this pattern:

```python
# src/mcp/identity/identity_mcp.py
from src.db.supabase_client import get_client
from src.mcp.identity.models import (
    RegisterUserResult, CreateStoreResult,
    StoreResult, WorkflowStateResult
)
from typing import Optional

class IdentityMCP:
    def __init__(self):
        self.db = get_client()
    
    async def register_user(
        self,
        telegram_user_id: int,
        telegram_username: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None
    ) -> RegisterUserResult:
        """Creates or retrieves a user record. Idempotent."""
        response = self.db.table('users').upsert({
            'telegram_user_id': telegram_user_id,
            'telegram_username': telegram_username,
            'first_name': first_name,
            'last_name': last_name
        }, on_conflict='telegram_user_id').execute()
        
        user = response.data[0]
        # ... create workflow_state and registration records
        
        return RegisterUserResult(
            user_id=user['id'],
            already_existed=...,
            message="User registered successfully."
        )
    
    # ... other tool methods
```

---

## Step 4: Build Order (Dependencies Between MCPs)

Build MCPs in this order — later modules call earlier ones:

1. **Identity MCP** — no dependencies on other MCPs
2. **Catalogue MCP** — no direct MCP calls (reads stores via DB)
3. **Inventory MCP** — calls Catalogue MCP (`get_product`) and Identity MCP (`advance_workflow_state`)
4. **Khata MCP** — no direct MCP calls
5. **Billing MCP** — calls Catalogue MCP, Inventory MCP, Khata MCP, IdentityMCP; accepts `payments_mcp=None` (injected later)
6. **Analytics MCP** — reads from DB directly
7. **Documents MCP** — calls Billing MCP, Analytics MCP, Identity MCP
8. **Payments MCP** — calls Khata MCP (for `get_balance` in `get_payment_history`)
   - After constructing both, inject: `billing_mcp.set_payments_mcp(payments_mcp)`
   - This late-binding is required because Billing depends on Payments (to record rows) but Payments depends on Khata (not Billing) — so Billing must be constructed before Payments, then Payments injected back in.

---

## Step 5: MCPInstances Container

Create a container class that holds singleton instances of all MCPs:

```python
# src/mcp/__init__.py
from src.mcp.identity.identity_mcp import IdentityMCP
from src.mcp.catalogue.catalogue_mcp import CatalogueMCP
from src.mcp.inventory.inventory_mcp import InventoryMCP
from src.mcp.billing.billing_mcp import BillingMCP
from src.mcp.khata.khata_mcp import KhataMCP
from src.mcp.analytics.analytics_mcp import AnalyticsMCP
from src.mcp.documents.documents_mcp import DocumentsMCP
from src.mcp.payments.payments_mcp import PaymentsMCP

class MCPInstances:
    """
    Construction order respects dependency injection:
        Identity → Catalogue → Inventory → Khata → Billing
        → Analytics → Documents → Payments (last — needs Khata)

    After construction, PaymentsMCP is late-bound into BillingMCP
    via set_payments_mcp() to break the circular dependency:
      - BillingMCP calls PaymentsMCP.record_payment()
      - PaymentsMCP calls KhataMCP.get_balance()
      → Build both, then inject Payments into Billing.
    """
    def __init__(self):
        self.identity = IdentityMCP()
        self.catalogue = CatalogueMCP(identity_mcp=self.identity)
        self.inventory = InventoryMCP(
            catalogue_mcp=self.catalogue,
            identity_mcp=self.identity,
        )
        self.khata = KhataMCP()
        self.billing = BillingMCP(
            catalogue_mcp=self.catalogue,
            inventory_mcp=self.inventory,
            khata_mcp=self.khata,
            identity_mcp=self.identity,
        )
        self.analytics = AnalyticsMCP()
        self.documents = DocumentsMCP(
            billing_mcp=self.billing,
            analytics_mcp=self.analytics,
            identity_mcp=self.identity,
        )
        # Payments last — depends on KhataMCP
        self.payments = PaymentsMCP(khata_mcp=self.khata)
        # Late-bind PaymentsMCP into BillingMCP
        self.billing.set_payments_mcp(self.payments)

# Module-level singleton (reused across warm Lambda invocations)
_mcp_instances: MCPInstances | None = None

def get_mcp_instances() -> MCPInstances:
    global _mcp_instances
    if _mcp_instances is None:
        _mcp_instances = MCPInstances()
    return _mcp_instances
```

---

## Step 6: GST Computation Utility

Create a shared utility for GST calculation — used by both Billing MCP and Documents MCP:

```python
# src/utils/gst.py
from decimal import Decimal, ROUND_HALF_UP

def compute_line_gst(
    quantity: float,
    unit_price: float,
    gst_rate: float
) -> dict:
    """Compute GST amounts for a single line item."""
    taxable = Decimal(str(quantity)) * Decimal(str(unit_price))
    taxable = taxable.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    
    gst_total = (taxable * Decimal(str(gst_rate)) / 100).quantize(
        Decimal('0.01'), rounding=ROUND_HALF_UP
    )
    cgst = (gst_total / 2).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    sgst = gst_total - cgst  # sgst absorbs rounding delta
    
    line_total = taxable + cgst + sgst
    
    return {
        'taxable_value': float(taxable),
        'gst_total': float(gst_total),
        'cgst_amount': float(cgst),
        'sgst_amount': float(sgst),
        'line_total': float(line_total)
    }
```

---

## Step 7: Inter-MCP Call Patterns

MCPs call each other via direct Python function calls (not HTTP). The Billing MCP calling Inventory MCP:

```python
# src/mcp/billing/billing_mcp.py
class BillingMCP:
    def __init__(self, inventory_mcp, catalogue_mcp, khata_mcp, identity_mcp):
        self.db = get_client()
        self.inventory = inventory_mcp  # Injected at construction
        self.catalogue = catalogue_mcp
        self.khata = khata_mcp
        self.identity = identity_mcp
    
    async def add_item_to_draft(self, draft_bill_id, product_id, quantity, ...):
        # Call inventory MCP to check availability
        availability = await self.inventory.check_availability(store_id, product_id, quantity)
        
        if availability.fulfillment_status == 'NONE':
            return AddItemResult(availability_status='NONE', ...)
        # ...
```

---

## Step 8: Error Handling Pattern

Each MCP tool should use a consistent error response pattern:

```python
from pydantic import BaseModel
from typing import Optional

class MCPError(BaseModel):
    error_code: str
    message: str
    details: Optional[dict] = None

# Raise as exceptions
class ProductNotFoundError(Exception):
    def __init__(self, product_name: str):
        self.message = f"Product '{product_name}' not found in catalogue."
        super().__init__(self.message)

class InsufficientStockError(Exception):
    def __init__(self, product_name: str, available: float, requested: float):
        self.message = f"Only {available} units of {product_name} available (requested {requested})."
        super().__init__(self.message)
```

PydanticAI catches exceptions from tools and returns them as error messages to the model.

---

## Step 9: Unit Tests for Each MCP

Before proceeding to Step 3 (Agent), write unit tests:

```
tests/
├── test_identity_mcp.py     # Test register_user, create_store idempotency
├── test_catalogue_mcp.py    # Test add_product, GST enforcement, fuzzy search
├── test_inventory_mcp.py    # Test receive_stock, check_availability, oversell guard
├── test_billing_mcp.py      # Test finalize_bill, idempotency, GST computation
├── test_khata_mcp.py        # Test credit/payment entries, balance computation
└── test_gst_utils.py        # Test GST calculation edge cases
```

Use Supabase test database or mock the Supabase client with `unittest.mock`.

---

## Validation Checklist

- [ ] All 8 MCP modules implemented with all tools from `docs/mcp/`
- [ ] Pydantic models defined for all inputs and outputs
- [ ] GST computation tested with known values
- [ ] Idempotency verified for register_user, create_store, finalize_bill
- [ ] Oversell guard tested (concurrent requests or qty > stock)
- [ ] Loose item GST enforcement tested (gst_rate must be 0)
- [ ] `payments` schema exposed in Supabase Dashboard → API → Exposed schemas
- [ ] PaymentsMCP late-binding verified: `billing._payments` is not None after MCPInstances()
- [ ] confirm_payment tool inserts EXACT payment row; does NOT insert for OVER/UNDER
- [ ] add_payment_entry(amount=None) reads from Redis pending_payment key
- [ ] add_credit_entry(amount=None) reads from Redis pending_payment key
- [ ] Redis pending_payment key cleared after resolution
- [ ] Credit bill finalize inserts KHATA payment row with paid_amount=0
- [ ] cancel_bill inserts CANCELLED audit row; void_bill inserts REFUNDED audit row
- [ ] All unit tests pass
