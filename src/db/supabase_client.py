"""
Supabase client singleton.

The client is initialised once per Lambda container lifecycle and reused
across warm invocations (connection pooling via the supabase-py library).
"""

from __future__ import annotations

from supabase import create_client, Client

from src.config import settings

_client: Client | None = None


def get_client() -> Client:
    """Return the module-level Supabase client, creating it on first call."""
    global _client
    if _client is None:
        _client = create_client(
            settings.SUPABASE_URL,
            settings.SUPABASE_SERVICE_ROLE_KEY,
        )
    return _client
