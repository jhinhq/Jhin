"""A harness around the public OAuth callbacks, shared by the callback tests.

Lives outside conftest.py so a test can import it as an ordinary module
without loading the conftest a second time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.deps import get_current_auth_optional
from jhin_api.oauth.redirect import CALLBACK_PATH
from jhin_db.models import OAuthAuthorization, User
from jhin_oauth.persistence import PendingAuthorizationStore
from jhin_secrets import SecretCrypto

#: The instance the callback harness stands up. Loopback and plain HTTP on
#: purpose: the browser sign-in works there too, and the tests say so.
CALLBACK_APP_URL = "http://localhost:3000"
CALLBACK_ISSUER = "https://auth.example.com"
CALLBACK_TOKEN_ENDPOINT = "https://auth.example.com/token"


@dataclass
class CallbackHarness:
    """The public OAuth routes mounted on a bare app, with a swappable session.

    ``actor["user"]`` is who the session dependency answers with; a test that
    wants "somebody else's browser" swaps it. ``pending`` mints a row exactly
    as the service would, so a callback can be walked with a real handle.
    """

    client: httpx.AsyncClient
    session: AsyncSession
    crypto: SecretCrypto
    workspace_id: UUID
    actor: dict[str, User]
    admin: User
    other: User

    async def pending(self, **overrides: Any) -> tuple[OAuthAuthorization, str]:
        store = PendingAuthorizationStore(self.session, self.crypto)
        payload: dict[str, Any] = {
            "workspace_id": self.workspace_id,
            "user_id": self.admin.id,
            "flow": "authorization_code",
            "connector_type": "mcp",
            "ttl_seconds": 600,
            "issuer": CALLBACK_ISSUER,
            "authorization_endpoint": f"{CALLBACK_ISSUER}/authorize",
            "token_endpoint": CALLBACK_TOKEN_ENDPOINT,
            "resource": "https://mcp.example.com",
            "scope": "read",
            "redirect_uri": f"{CALLBACK_APP_URL}{CALLBACK_PATH}",
            "verifier": "v" * 43,
            "draft": {"name": "Example", "config": {}},
        }
        payload.update(overrides)
        row, handle = await store.create(**payload)
        await self.session.commit()
        return row, handle

    def sign_out(self) -> None:
        """Make the next callback arrive with no session at all."""
        app = cast(FastAPI, self.client._transport.app)  # type: ignore[attr-defined]
        app.dependency_overrides[get_current_auth_optional] = lambda: None
