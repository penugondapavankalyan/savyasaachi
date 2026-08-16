"""
MCP package — singleton container for all MCP modules.

Usage:
    from src.mcp import get_mcp_instances
    mcps = get_mcp_instances()
    result = await mcps.identity.register_user(...)
"""

from __future__ import annotations

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
    Container for all MCP module singletons.

    Construction order respects dependency injection:
        Identity → Catalogue → Inventory (needs Identity + Catalogue)
        → Khata → Billing (needs Catalogue + Inventory + Khata + Identity)
        → Analytics → Documents (needs Billing + Analytics + Identity)
        → Payments (needs Khata — for get_balance in get_payment_history)
    """

    def __init__(self) -> None:
        self.identity = IdentityMCP()

        self.catalogue = CatalogueMCP(identity_mcp=self.identity)

        self.inventory = InventoryMCP(
            identity_mcp=self.identity,
            catalogue_mcp=self.catalogue,
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

        # Payments last — depends on KhataMCP (for balance reads in history)
        self.payments = PaymentsMCP(khata_mcp=self.khata)

        # Late-bind PaymentsMCP into BillingMCP now that both are constructed.
        # BillingMCP was constructed before PaymentsMCP existed (MCPInstances order),
        # so we inject it here rather than passing at BillingMCP.__init__ time.
        self.billing.set_payments_mcp(self.payments)


# Module-level singleton — reused across warm Lambda invocations
_mcp_instances: MCPInstances | None = None


def get_mcp_instances() -> MCPInstances:
    """Return the module-level MCPInstances singleton."""
    global _mcp_instances
    if _mcp_instances is None:
        _mcp_instances = MCPInstances()
    return _mcp_instances
