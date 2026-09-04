"""A harness around the public OAuth callbacks, shared by the callback tests.

Lives outside conftest.py so a test can import it as an ordinary module
without loading the conftest a second time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.deps import get_current_auth_optional
from jhin_api.oauth.redirect import CALLBACK_PATH
from jhin_db.models import OAuthAuthorization, OAuthClientRegistration, User
from jhin_oauth.persistence import PendingAuthorizationStore
from jhin_secrets import SecretCrypto

#: The instance the callback harness stands up. Loopback and plain HTTP on
#: purpose: the browser sign-in works there too, and the tests say so.
CALLBACK_APP_URL = "http://localhost:3000"
CALLBACK_ISSUER = "https://auth.example.com"
CALLBACK_TOKEN_ENDPOINT = "https://auth.example.com/token"

#: The one landing every pre-claim refusal produces, byte for byte. Unknown,
#: expired, spent, another user's, another workspace's, another flow's,
#: malformed, over-long — all of them, so a prober learns nothing.
REFUSED = f"{CALLBACK_APP_URL}/apps?oauth_error=expired"


def assert_refused(response: httpx.Response) -> None:
    """The uniform pre-claim refusal: a Jhin page, never a JSON body."""
    assert response.status_code == 303
    assert response.headers["location"] == REFUSED
    assert response.headers["cache-control"] == "no-store"
    assert response.content == b""
    assert "application/json" not in response.headers.get("content-type", "")


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
    #: The dependency ``sign_out`` replaces, so ``sign_in_as`` can put it back.
    auth_override: Any

    async def pending(self, **overrides: Any) -> tuple[OAuthAuthorization, str]:
        store = PendingAuthorizationStore(self.session, self.crypto)
        payload: dict[str, Any] = {
            "workspace_id": self.workspace_id,
            "user_id": self.admin.id,
            "flow": "authorization_code",
            "connector_type": "mcp",
            "ttl_seconds": 1800,
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

    async def settled(
        self,
        *,
        outcome: str,
        connection_id: UUID | None = None,
        retain_seconds: int = 600,
        **overrides: Any,
    ) -> tuple[OAuthAuthorization, str]:
        """A row that has already been spent and left a receipt."""
        row, handle = await self.pending(**overrides)
        now = datetime.now(UTC)
        row.consumed_at = now
        row.verifier_secret_id = None
        row.draft_json = {}
        row.connection_id = None
        row.outcome = outcome
        row.outcome_connection_id = connection_id
        row.retain_until = now + timedelta(seconds=retain_seconds)
        await self.session.commit()
        return row, handle

    async def registration(self, *, client_id: str = "test-client") -> OAuthClientRegistration:
        """A stored client registration the callback can resolve."""
        row = OAuthClientRegistration(
            workspace_id=self.workspace_id,
            issuer=CALLBACK_ISSUER,
            redirect_uri=f"{CALLBACK_APP_URL}{CALLBACK_PATH}",
            client_id=client_id,
            source="manual",
        )
        self.session.add(row)
        await self.session.commit()
        return row

    async def expire_receipt(self, row: OAuthAuthorization) -> None:
        """Push a receipt's retention horizon into the past."""
        row.retain_until = datetime.now(UTC) - timedelta(seconds=1)
        await self.session.commit()

    def sign_out(self) -> None:
        """Make the next callback arrive with no session at all."""
        app = cast(FastAPI, self.client._transport.app)  # type: ignore[attr-defined]
        app.dependency_overrides[get_current_auth_optional] = lambda: None

    def sign_in_as(self, user: User) -> None:
        """Undo :meth:`sign_out`, and make this user the one who is signed in."""
        app = cast(FastAPI, self.client._transport.app)  # type: ignore[attr-defined]
        app.dependency_overrides[get_current_auth_optional] = self.auth_override
        self.actor["user"] = user
