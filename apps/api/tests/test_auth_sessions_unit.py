"""Session lifecycle: rotation, idle expiry, client binding, revocation.

These exercise the real dependency (`get_current_auth`) against a real session
row, because every one of them is a property an attacker holding a stolen
cookie would try to violate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import Headers
from starlette.requests import Request

from jhin_api.auth import service as auth_service
from jhin_api.deps import get_current_auth
from jhin_api.security.passwords import hash_password, verify_password
from jhin_api.security.rate_limit import LoginRateLimiter
from jhin_api.security.tokens import hash_token
from jhin_api.settings import Settings
from jhin_db.models import User, UserSession
from jhin_domain import UserStatus, new_uuid7

PASSWORD = "orbital-lemon-parade-77"
UA = "Mozilla/5.0 (test browser)"


def make_request(
    *, cookies: dict[str, str], user_agent: str | None = UA, client_host: str = "203.0.113.9"
) -> Request:
    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
    raw: list[tuple[bytes, bytes]] = [(b"cookie", cookie_header.encode())]
    if user_agent is not None:
        raw.append((b"user-agent", user_agent.encode()))
    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/auth/me",
        "headers": raw,
        "client": (client_host, 51234),
        "query_string": b"",
        "state": {"request_id": new_uuid7()},
    }
    request = Request(scope)
    request.scope["headers"] = Headers(raw=raw).raw
    return request


async def make_user(session: AsyncSession, *, email: str = "dana@example.com") -> User:
    user = User(
        email=email,
        display_name="Dana",
        password_hash=hash_password(PASSWORD),
        status=UserStatus.ACTIVE.value,
    )
    session.add(user)
    await session.flush()
    return user


async def seat(
    session: AsyncSession, user: User, *, ttl_hours: int = 168, user_agent: str | None = UA
) -> str:
    token = await auth_service.start_session(
        session, user.id, ttl_hours=ttl_hours, ip_hash="hash", user_agent=user_agent
    )
    await session.commit()
    return token


async def resolve(session: AsyncSession, token: str, **request_kwargs: Any) -> Any:
    settings = Settings()
    request = make_request(cookies={settings.session_cookie_name: token}, **request_kwargs)
    return await get_current_auth(request, session, settings)


# --- rotation / fixation ----------------------------------------------------


async def test_every_login_mints_a_new_session_token(session: AsyncSession) -> None:
    """Session fixation: a token planted before login is never the token the
    victim ends up authenticated with."""
    user = await make_user(session)
    await session.commit()
    limiter = LoginRateLimiter()
    first = await auth_service.login(
        session,
        limiter,
        email=user.email,
        password=PASSWORD,
        session_ttl_hours=168,
        request_id=new_uuid7(),
        ip="203.0.113.9",
        ip_hash="hash",
        user_agent=UA,
    )
    second = await auth_service.login(
        session,
        limiter,
        email=user.email,
        password=PASSWORD,
        session_ttl_hours=168,
        request_id=new_uuid7(),
        ip="203.0.113.9",
        ip_hash="hash",
        user_agent=UA,
    )
    assert first.session_token != second.session_token
    rows = list(await session.scalars(select(UserSession).where(UserSession.user_id == user.id)))
    assert {row.token_hash for row in rows} == {
        hash_token(first.session_token),
        hash_token(second.session_token),
    }


async def test_plaintext_session_token_is_never_stored(session: AsyncSession) -> None:
    user = await make_user(session)
    token = await seat(session, user)
    row = await session.scalar(select(UserSession).where(UserSession.user_id == user.id))
    assert row is not None
    assert token not in row.token_hash
    assert row.token_hash == hash_token(token)


# --- expiry -----------------------------------------------------------------


async def test_valid_session_resolves(session: AsyncSession) -> None:
    user = await make_user(session)
    token = await seat(session, user)
    auth = await resolve(session, token)
    assert auth.user.id == user.id


async def test_absolutely_expired_session_is_rejected(session: AsyncSession) -> None:
    user = await make_user(session)
    token = await seat(session, user)
    row = await session.scalar(select(UserSession).where(UserSession.user_id == user.id))
    assert row is not None
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await session.commit()
    with pytest.raises(HTTPException) as caught:
        await resolve(session, token)
    assert caught.value.status_code == 401


async def test_idle_session_is_revoked_and_rejected(session: AsyncSession) -> None:
    """An abandoned tab stops being a credential long before the absolute TTL."""
    settings = Settings()
    user = await make_user(session)
    token = await seat(session, user)
    row = await session.scalar(select(UserSession).where(UserSession.user_id == user.id))
    assert row is not None
    stale = datetime.now(UTC) - timedelta(hours=settings.session_idle_timeout_hours + 1)
    row.last_used_at = stale
    await session.commit()

    with pytest.raises(HTTPException) as caught:
        await resolve(session, token)
    assert caught.value.status_code == 401
    await session.refresh(row)
    assert row.revoked_at is not None  # revoked, not merely refused


async def test_active_use_keeps_a_session_alive(session: AsyncSession) -> None:
    user = await make_user(session)
    token = await seat(session, user)
    await resolve(session, token)
    row = await session.scalar(select(UserSession).where(UserSession.user_id == user.id))
    assert row is not None and row.last_used_at is not None
    assert (datetime.now(UTC) - row.last_used_at.replace(tzinfo=UTC)) < timedelta(minutes=1)


# --- client binding ---------------------------------------------------------


async def test_cookie_replayed_from_another_client_kills_the_session(
    session: AsyncSession,
) -> None:
    user = await make_user(session)
    token = await seat(session, user)
    with pytest.raises(HTTPException) as caught:
        await resolve(session, token, user_agent="curl/8.5.0")
    assert caught.value.status_code == 401
    row = await session.scalar(select(UserSession).where(UserSession.user_id == user.id))
    assert row is not None and row.revoked_at is not None


async def test_roaming_to_a_different_address_does_not_log_you_out(
    session: AsyncSession,
) -> None:
    """Mobile clients change address constantly; only the client identity is
    part of the binding."""
    user = await make_user(session)
    token = await seat(session, user)
    auth = await resolve(session, token, client_host="198.51.100.7")
    assert auth.user.id == user.id


async def test_sessions_without_a_recorded_user_agent_are_not_bound(
    session: AsyncSession,
) -> None:
    user = await make_user(session)
    token = await seat(session, user, user_agent=None)
    assert (await resolve(session, token, user_agent="anything/1.0")).user.id == user.id


# --- revocation -------------------------------------------------------------


async def test_logout_revokes_only_the_calling_session(session: AsyncSession) -> None:
    user = await make_user(session)
    keep = await seat(session, user)
    drop = await seat(session, user)
    record = await session.scalar(
        select(UserSession).where(UserSession.token_hash == hash_token(drop))
    )
    assert record is not None
    await auth_service.logout(
        session, session_record=record, user=user, request_id=new_uuid7(), ip_hash="hash"
    )
    with pytest.raises(HTTPException):
        await resolve(session, drop)
    assert (await resolve(session, keep)).user.id == user.id


async def test_logout_everywhere_revokes_every_session(session: AsyncSession) -> None:
    user = await make_user(session)
    tokens = [await seat(session, user) for _ in range(3)]
    revoked = await auth_service.logout_everywhere(
        session, user=user, request_id=new_uuid7(), ip_hash="hash"
    )
    assert revoked == 3
    for token in tokens:
        with pytest.raises(HTTPException):
            await resolve(session, token)


async def test_logout_everywhere_does_not_touch_other_users(session: AsyncSession) -> None:
    user = await make_user(session)
    other = await make_user(session, email="other@example.com")
    other_token = await seat(session, other)
    await seat(session, user)
    await auth_service.logout_everywhere(session, user=user, request_id=new_uuid7(), ip_hash="hash")
    assert (await resolve(session, other_token)).user.id == other.id


# --- password change --------------------------------------------------------


async def test_password_change_revokes_every_old_session_and_reseats_the_caller(
    session: AsyncSession,
) -> None:
    """The whole point of changing a password after a suspected compromise."""
    user = await make_user(session)
    old_tokens = [await seat(session, user) for _ in range(2)]
    new_token = await auth_service.change_password(
        session,
        user=user,
        current_password=PASSWORD,
        new_password="another-decent-passphrase-9",
        session_ttl_hours=168,
        request_id=new_uuid7(),
        ip_hash="hash",
        user_agent=UA,
    )
    for token in old_tokens:
        with pytest.raises(HTTPException):
            await resolve(session, token)
    assert (await resolve(session, new_token)).user.id == user.id
    assert verify_password(user.password_hash, "another-decent-passphrase-9")


async def test_password_change_requires_the_current_password(session: AsyncSession) -> None:
    user = await make_user(session)
    token = await seat(session, user)
    with pytest.raises(HTTPException) as caught:
        await auth_service.change_password(
            session,
            user=user,
            current_password="not-the-right-one",
            new_password="another-decent-passphrase-9",
            session_ttl_hours=168,
            request_id=new_uuid7(),
            ip_hash="hash",
            user_agent=UA,
        )
    assert caught.value.status_code == 403
    # Nothing changed: the old session still works.
    assert (await resolve(session, token)).user.id == user.id


async def test_password_change_enforces_the_password_policy(session: AsyncSession) -> None:
    user = await make_user(session)
    with pytest.raises(HTTPException) as caught:
        await auth_service.change_password(
            session,
            user=user,
            current_password=PASSWORD,
            new_password="password1234",
            session_ttl_hours=168,
            request_id=new_uuid7(),
            ip_hash="hash",
            user_agent=UA,
        )
    assert caught.value.status_code == 422
    assert "commonly guessed" in str(caught.value.detail)


async def test_password_change_rejects_reusing_the_same_password(
    session: AsyncSession,
) -> None:
    user = await make_user(session)
    with pytest.raises(HTTPException) as caught:
        await auth_service.change_password(
            session,
            user=user,
            current_password=PASSWORD,
            new_password=PASSWORD,
            session_ttl_hours=168,
            request_id=new_uuid7(),
            ip_hash="hash",
            user_agent=UA,
        )
    assert caught.value.status_code == 422


# --- login hardening --------------------------------------------------------


async def test_unknown_email_and_wrong_password_are_indistinguishable(
    session: AsyncSession,
) -> None:
    user = await make_user(session)
    await session.commit()
    limiter = LoginRateLimiter()
    errors = []
    for email, password in (
        (user.email, "wrong-password-entirely"),
        ("nobody@example.com", PASSWORD),
    ):
        with pytest.raises(HTTPException) as caught:
            await auth_service.login(
                session,
                limiter,
                email=email,
                password=password,
                session_ttl_hours=168,
                request_id=new_uuid7(),
                ip="203.0.113.9",
                ip_hash="hash",
                user_agent=UA,
            )
        errors.append((caught.value.status_code, caught.value.detail))
    assert errors[0] == errors[1]


async def test_repeated_failures_lock_the_account_with_a_retry_after(
    session: AsyncSession,
) -> None:
    user = await make_user(session)
    await session.commit()
    limiter = LoginRateLimiter(account_max_attempts=3)
    for _ in range(3):
        with pytest.raises(HTTPException):
            await auth_service.login(
                session,
                limiter,
                email=user.email,
                password="wrong",
                session_ttl_hours=168,
                request_id=new_uuid7(),
                ip="203.0.113.9",
                ip_hash="hash",
                user_agent=UA,
            )
    with pytest.raises(HTTPException) as caught:
        await auth_service.login(
            session,
            limiter,
            email=user.email,
            password=PASSWORD,  # even the *correct* password is refused
            session_ttl_hours=168,
            request_id=new_uuid7(),
            ip="203.0.113.9",
            ip_hash="hash",
            user_agent=UA,
        )
    assert caught.value.status_code == 429
    assert caught.value.headers is not None
    assert int(caught.value.headers["Retry-After"]) > 0
    assert "clears itself" in str(caught.value.detail)


async def test_inactive_users_cannot_sign_in(session: AsyncSession) -> None:
    user = await make_user(session)
    user.status = UserStatus.DISABLED.value
    await session.commit()
    with pytest.raises(HTTPException) as caught:
        await auth_service.login(
            session,
            LoginRateLimiter(),
            email=user.email,
            password=PASSWORD,
            session_ttl_hours=168,
            request_id=new_uuid7(),
            ip="203.0.113.9",
            ip_hash="hash",
            user_agent=UA,
        )
    assert caught.value.status_code == 401


async def test_bootstrap_rejects_a_weak_owner_password(session: AsyncSession) -> None:
    with pytest.raises(HTTPException) as caught:
        await auth_service.bootstrap_owner(
            session,
            email="owner@example.com",
            password="password1234",
            display_name="Owner",
            workspace_name="Acme",
            session_ttl_hours=168,
            request_id=new_uuid7(),
            ip_hash="hash",
            user_agent=UA,
        )
    assert caught.value.status_code == 422
