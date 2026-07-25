"""Bounded, framework-independent authentication and request-security primitives."""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import secrets
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

MAX_SESSIONS: Final = 32
MAX_RATE_LIMIT_KEYS: Final = 128
SESSION_TOKEN_BYTES: Final = 32
CSRF_TOKEN_BYTES: Final = 32
PASSWORD_SALT_BYTES: Final = 16
PASSWORD_MIN_CHARS: Final = 12
PASSWORD_MAX_BYTES: Final = 256
SCRYPT_N: Final = 2**14
SCRYPT_R: Final = 8
SCRYPT_P: Final = 1
SCRYPT_DKLEN: Final = 32
_TOKEN_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{32,256}")


class SecurityError(ValueError):
    """Base class for safe, expected security failures."""


class AuthenticationError(SecurityError):
    """Credentials or a session are absent, invalid, or expired."""


class CsrfError(SecurityError):
    """A state-changing request did not carry its session's CSRF token."""


class RateLimitError(SecurityError):
    """A bounded request rate has been exceeded."""


@dataclass(frozen=True, slots=True)
class PasswordRecord:
    """A versioned password verifier; plaintext is never retained."""

    salt_hex: str
    digest_hex: str
    algorithm: str = "scrypt-v1"

    def __post_init__(self) -> None:
        if self.algorithm != "scrypt-v1":
            raise SecurityError("unsupported password record algorithm")
        try:
            salt = bytes.fromhex(self.salt_hex)
            digest = bytes.fromhex(self.digest_hex)
        except ValueError as error:
            raise SecurityError("password record is not valid hexadecimal") from error
        if len(salt) != PASSWORD_SALT_BYTES or len(digest) != SCRYPT_DKLEN:
            raise SecurityError("password record has invalid lengths")


def _password_bytes(password: str) -> bytes:
    if not isinstance(password, str):
        raise SecurityError("password must be text")
    encoded = password.encode("utf-8")
    if len(password) < PASSWORD_MIN_CHARS or len(encoded) > PASSWORD_MAX_BYTES:
        raise SecurityError(
            f"password must contain at least {PASSWORD_MIN_CHARS} characters "
            f"and at most {PASSWORD_MAX_BYTES} UTF-8 bytes"
        )
    return encoded


def create_password_record(
    password: str, *, random_bytes: Callable[[int], bytes] = secrets.token_bytes
) -> PasswordRecord:
    """Create a salted verifier without retaining or returning the password."""

    encoded = _password_bytes(password)
    salt = random_bytes(PASSWORD_SALT_BYTES)
    if not isinstance(salt, bytes) or len(salt) != PASSWORD_SALT_BYTES:
        raise SecurityError("random source returned an invalid salt")
    digest = hashlib.scrypt(
        encoded,
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )
    return PasswordRecord(salt.hex(), digest.hex())


def verify_password(password: str, record: PasswordRecord) -> bool:
    """Verify in constant time; malformed candidate passwords simply fail."""

    try:
        encoded = _password_bytes(password)
    except (SecurityError, UnicodeError):
        return False
    digest = hashlib.scrypt(
        encoded,
        salt=bytes.fromhex(record.salt_hex),
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )
    return hmac.compare_digest(digest, bytes.fromhex(record.digest_hex))


@dataclass(frozen=True, slots=True)
class Session:
    """Server-side session state; only opaque tokens reach the browser."""

    token: str
    csrf_token: str
    created_at_s: float
    last_seen_s: float
    reauthenticated_at_s: float


def _valid_time(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SecurityError(f"{name} must be a finite non-negative number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise SecurityError(f"{name} must be a finite non-negative number")
    return result


class SessionStore:
    """A bounded in-memory session store using caller-supplied monotonic time."""

    def __init__(
        self,
        *,
        idle_timeout_s: float = 30 * 60,
        absolute_timeout_s: float = 12 * 60 * 60,
        reauthentication_window_s: float = 5 * 60,
        max_sessions: int = MAX_SESSIONS,
        token_factory: Callable[[int], str] = secrets.token_urlsafe,
    ) -> None:
        self._idle_timeout_s = _valid_time(idle_timeout_s, "idle timeout")
        self._absolute_timeout_s = _valid_time(absolute_timeout_s, "absolute timeout")
        self._reauthentication_window_s = _valid_time(
            reauthentication_window_s, "reauthentication window"
        )
        if self._idle_timeout_s <= 0 or self._absolute_timeout_s <= 0:
            raise SecurityError("session timeouts must be positive")
        if isinstance(max_sessions, bool) or not 1 <= max_sessions <= MAX_SESSIONS:
            raise SecurityError(f"max_sessions must be between 1 and {MAX_SESSIONS}")
        self._max_sessions = max_sessions
        self._token_factory = token_factory
        self._sessions: OrderedDict[str, Session] = OrderedDict()

    def _expired(self, session: Session, now_s: float) -> bool:
        return (
            now_s - session.last_seen_s >= self._idle_timeout_s
            or now_s - session.created_at_s >= self._absolute_timeout_s
            or now_s < session.created_at_s
        )

    def prune(self, now_s: float) -> int:
        """Remove expired sessions and return the count removed."""

        now = _valid_time(now_s, "monotonic time")
        expired = [
            token for token, session in self._sessions.items() if self._expired(session, now)
        ]
        for token in expired:
            del self._sessions[token]
        return len(expired)

    def create(self, now_s: float) -> Session:
        """Create a session, evicting the least-recently-used session at the bound."""

        now = _valid_time(now_s, "monotonic time")
        self.prune(now)
        if len(self._sessions) >= self._max_sessions:
            self._sessions.popitem(last=False)
        token = self._unique_token()
        csrf_token = self._unique_token(exclude=frozenset({token}))
        session = Session(token, csrf_token, now, now, now)
        self._sessions[token] = session
        return session

    def _unique_token(self, *, exclude: frozenset[str] = frozenset()) -> str:
        for _ in range(4):
            candidate = self._token_factory(SESSION_TOKEN_BYTES)
            if (
                isinstance(candidate, str)
                and _TOKEN_PATTERN.fullmatch(candidate) is not None
                and candidate not in self._sessions
                and candidate not in exclude
            ):
                return candidate
        raise SecurityError("could not generate a unique bounded session token")

    def authenticate(self, token: str, now_s: float) -> Session:
        """Validate and touch a session without accepting token prefixes."""

        now = _valid_time(now_s, "monotonic time")
        if not isinstance(token, str) or not token or len(token) > 256:
            raise AuthenticationError("invalid session")
        session = self._sessions.get(token)
        if session is None or self._expired(session, now):
            self._sessions.pop(token, None)
            raise AuthenticationError("invalid or expired session")
        touched = Session(
            session.token,
            session.csrf_token,
            session.created_at_s,
            now,
            session.reauthenticated_at_s,
        )
        self._sessions[token] = touched
        self._sessions.move_to_end(token)
        return touched

    def require_csrf(self, session: Session, candidate: str) -> None:
        if (
            not isinstance(candidate, str)
            or len(candidate) > 256
            or not hmac.compare_digest(session.csrf_token, candidate)
        ):
            raise CsrfError("invalid CSRF token")

    def reauthenticate(self, token: str, now_s: float) -> Session:
        session = self.authenticate(token, now_s)
        updated = Session(
            session.token,
            session.csrf_token,
            session.created_at_s,
            session.last_seen_s,
            float(now_s),
        )
        self._sessions[token] = updated
        return updated

    def require_recent_reauthentication(self, session: Session, now_s: float) -> None:
        now = _valid_time(now_s, "monotonic time")
        age = now - session.reauthenticated_at_s
        if age < 0 or age > self._reauthentication_window_s:
            raise AuthenticationError("recent reauthentication required")

    def revoke(self, token: str) -> None:
        self._sessions.pop(token, None)

    @property
    def size(self) -> int:
        return len(self._sessions)


class FixedWindowRateLimiter:
    """Bounded per-key limiter with no background task or unbounded history."""

    def __init__(
        self,
        *,
        limit: int,
        window_s: float,
        max_keys: int = MAX_RATE_LIMIT_KEYS,
    ) -> None:
        if isinstance(limit, bool) or not 1 <= limit <= 10_000:
            raise SecurityError("rate limit must be between 1 and 10000")
        self._window_s = _valid_time(window_s, "rate-limit window")
        if self._window_s <= 0:
            raise SecurityError("rate-limit window must be positive")
        if isinstance(max_keys, bool) or not 1 <= max_keys <= MAX_RATE_LIMIT_KEYS:
            raise SecurityError(f"max_keys must be between 1 and {MAX_RATE_LIMIT_KEYS}")
        self._limit = limit
        self._max_keys = max_keys
        self._events: OrderedDict[str, deque[float]] = OrderedDict()

    def check(self, key: str, now_s: float) -> None:
        now = _valid_time(now_s, "monotonic time")
        if not isinstance(key, str) or not key or len(key) > 128 or not key.isascii():
            raise SecurityError("rate-limit key must be a bounded ASCII identifier")
        events = self._events.get(key)
        if events is None:
            if len(self._events) >= self._max_keys:
                self._events.popitem(last=False)
            events = deque(maxlen=self._limit)
            self._events[key] = events
        else:
            self._events.move_to_end(key)
        cutoff = now - self._window_s
        while events and events[0] <= cutoff:
            events.popleft()
        if events and now < events[-1]:
            events.clear()
        if len(events) >= self._limit:
            raise RateLimitError("request rate limit exceeded")
        events.append(now)

    @property
    def key_count(self) -> int:
        return len(self._events)


__all__ = [
    "CSRF_TOKEN_BYTES",
    "MAX_RATE_LIMIT_KEYS",
    "MAX_SESSIONS",
    "AuthenticationError",
    "CsrfError",
    "FixedWindowRateLimiter",
    "PasswordRecord",
    "RateLimitError",
    "SecurityError",
    "Session",
    "SessionStore",
    "create_password_record",
    "verify_password",
]
