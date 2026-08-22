"""Test doubles for connector development (plan 32.2)."""

from jhin_connectors.testing.fake_supabase import FakeSupabaseServer
from jhin_connectors.testing.fake_vercel import FakeVercelServer

__all__ = [
    "FakeSupabaseServer",
    "FakeVercelServer",
]
