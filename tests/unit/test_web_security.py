from __future__ import annotations

from itertools import count

import pytest

from dashcam_web.security import (
    AuthenticationError,
    CsrfError,
    FixedWindowRateLimiter,
    PasswordRecord,
    RateLimitError,
    SecurityError,
    SessionStore,
    create_password_record,
    verify_password,
)


def test_password_record_verifies_without_retaining_plaintext() -> None:
    password = "correct horse battery staple"
    record = create_password_record(password, random_bytes=lambda size: b"s" * size)

    assert verify_password(password, record)
    assert not verify_password("incorrect password", record)
    assert password not in repr(record)


@pytest.mark.parametrize("password", ["short", "x" * 257])
def test_password_policy_is_bounded(password: str) -> None:
    with pytest.raises(SecurityError):
        create_password_record(password)


def test_password_record_rejects_malformed_or_unknown_records() -> None:
    with pytest.raises(SecurityError):
        PasswordRecord("00", "00")
    with pytest.raises(SecurityError):
        PasswordRecord("00" * 16, "00" * 32, algorithm="future")


def _tokens() -> object:
    sequence = count()
    return lambda _size: f"{next(sequence):032x}"


def test_session_authentication_csrf_reauth_expiry_and_revoke() -> None:
    store = SessionStore(
        idle_timeout_s=10,
        absolute_timeout_s=30,
        reauthentication_window_s=5,
        token_factory=_tokens(),  # type: ignore[arg-type]
    )
    session = store.create(100)
    assert session.token != session.csrf_token
    authenticated = store.authenticate(session.token, 104)
    store.require_csrf(authenticated, session.csrf_token)
    with pytest.raises(CsrfError):
        store.require_csrf(authenticated, "wrong")

    store.require_recent_reauthentication(authenticated, 105)
    with pytest.raises(AuthenticationError):
        store.require_recent_reauthentication(authenticated, 106)
    refreshed = store.reauthenticate(session.token, 106)
    store.require_recent_reauthentication(refreshed, 111)

    with pytest.raises(AuthenticationError):
        store.authenticate(session.token, 117)
    assert store.size == 0

    replacement = store.create(120)
    store.revoke(replacement.token)
    with pytest.raises(AuthenticationError):
        store.authenticate(replacement.token, 120)


def test_session_store_is_bounded_and_evicts_lru() -> None:
    store = SessionStore(max_sessions=2, token_factory=_tokens())  # type: ignore[arg-type]
    first = store.create(0)
    second = store.create(1)
    store.authenticate(first.token, 2)
    third = store.create(3)

    assert store.size == 2
    with pytest.raises(AuthenticationError):
        store.authenticate(second.token, 3)
    assert store.authenticate(first.token, 3).token == first.token
    assert store.authenticate(third.token, 3).token == third.token


@pytest.mark.parametrize("now", [-1, float("inf"), float("nan"), True])
def test_sessions_reject_invalid_monotonic_time(now: float) -> None:
    store = SessionStore(token_factory=_tokens())  # type: ignore[arg-type]
    with pytest.raises(SecurityError):
        store.create(now)


def test_rate_limiter_has_exact_window_boundary_and_bounded_keys() -> None:
    limiter = FixedWindowRateLimiter(limit=2, window_s=10, max_keys=2)
    limiter.check("a", 0)
    limiter.check("a", 1)
    with pytest.raises(RateLimitError):
        limiter.check("a", 9.99)
    limiter.check("a", 10)

    limiter.check("b", 10)
    limiter.check("c", 10)
    assert limiter.key_count == 2


def test_rate_limiter_recovers_from_clock_regression_without_lockout() -> None:
    limiter = FixedWindowRateLimiter(limit=2, window_s=10)
    limiter.check("client", 100)
    limiter.check("client", 90)
    limiter.check("client", 91)
    with pytest.raises(RateLimitError):
        limiter.check("client", 92)
