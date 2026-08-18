"""Test doubles for connector development (plan 32.2)."""

from jhin_connectors.testing.fake_supabase import FakeSupabaseServer, FakeSupabaseState
from jhin_connectors.testing.fake_vercel import FakeVercelServer, FakeVercelState

__all__ = [
    "FakeSupabaseServer",
    "FakeSupabaseState",
    "FakeVercelServer",
    "FakeVercelState",
]
