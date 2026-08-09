"""Framework-neutral HTTP policy layer for the local dashcam web service."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, cast
from uuid import UUID, uuid4

from dashcam.control.api import (
    API_PREFIX,
    ENDPOINT_BY_METHOD_PATH,
    AuthPolicy,
    EndpointContract,
    ErrorCode,
    ErrorResponse,
    HttpMethod,
    PageRequest,
)
from dashcam_web.recorder_client import (
    ApprovedDownload,
    JsonValue,
    RecorderClient,
    RecorderCommand,
    RecorderProtocolError,
    RecorderRemoteError,
)
from dashcam_web.security import (
    AuthenticationError,
    CsrfError,
    FixedWindowRateLimiter,
    PasswordRecord,
    RateLimitError,
    SecurityError,
    Session,
    SessionStore,
    verify_password,
)

MAX_HTTP_BODY_BYTES: Final = 64 * 1024
MAX_HEADER_VALUE_CHARS: Final = 512
SESSION_PATH: Final = f"{API_PREFIX}/session"
REAUTHENTICATE_PATH: Final = f"{SESSION_PATH}/reauthenticate"
_SECRET_KEYS: Final = frozenset(
    {"password", "passphrase", "ap_passphrase", "session_secret", "csrf_secret"}
)


@dataclass(frozen=True, slots=True)
class Request:
    method: str
    path: str
    headers: Mapping[str, str]
    body: bytes = b""
    query: Mapping[str, str] | None = None
    client_key: str = "local"


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: JsonValue | None
    headers: Mapping[str, str]
    download: ApprovedDownload | None = None


def _json_response(status: int, body: JsonValue) -> Response:
    return Response(
        status,
        body,
        {"Content-Type": "application/json", "Cache-Control": "no-store"},
    )


def _error(status: int, code: ErrorCode, message: str, *, retryable: bool = False) -> Response:
    payload = ErrorResponse(code, message, retryable=retryable).as_dict()
    return _json_response(status, cast(JsonValue, payload))


def _parse_object(body: bytes) -> dict[str, object]:
    if len(body) > MAX_HTTP_BODY_BYTES:
        raise ValueError("request body is too large")
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("request body must be a JSON object") from error
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("request body must be a JSON object")
    return value


def _header(request: Request, name: str) -> str:
    target = name.casefold()
    matches = [value for key, value in request.headers.items() if key.casefold() == target]
    if len(matches) != 1 or len(matches[0]) > MAX_HEADER_VALUE_CHARS:
        return ""
    return matches[0]


def _redact_secrets(value: JsonValue) -> JsonValue:
    if isinstance(value, list):
        return [_redact_secrets(item) for item in value]
    if isinstance(value, dict):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if key.casefold() in _SECRET_KEYS:
                is_set = bool(item.get("is_set", False)) if isinstance(item, dict) else bool(item)
                result[key] = {"is_set": is_set}
            else:
                result[key] = _redact_secrets(item)
        return result
    return value


def _match_endpoint(method: HttpMethod, path: str) -> tuple[EndpointContract, str | None] | None:
    exact = ENDPOINT_BY_METHOD_PATH.get((method, path))
    if exact is not None:
        return exact, None
    prefix = f"{API_PREFIX}/clips/"
    if not path.startswith(prefix):
        return None
    tail = path.removeprefix(prefix).split("/")
    if len(tail) not in {1, 2}:
        return None
    suffix = "" if len(tail) == 1 else f"/{tail[1]}"
    template = f"{API_PREFIX}/clips/{{clip_id}}{suffix}"
    endpoint = ENDPOINT_BY_METHOD_PATH.get((method, template))
    return (endpoint, tail[0]) if endpoint is not None else None


class WebApplication:
    """Authentication, validation, and dispatch independent of an HTTP framework."""

    def __init__(
        self,
        *,
        recorder: RecorderClient,
        sessions: SessionStore,
        password_record: PasswordRecord,
        clock: Callable[[], float] = time.monotonic,
        rate_limiter: FixedWindowRateLimiter | None = None,
        login_rate_limiter: FixedWindowRateLimiter | None = None,
    ) -> None:
        self._recorder = recorder
        self._sessions = sessions
        self._password_record = password_record
        self._clock = clock
        self._rate_limiter = rate_limiter or FixedWindowRateLimiter(limit=120, window_s=60)
        self._login_rate_limiter = login_rate_limiter or FixedWindowRateLimiter(
            limit=5, window_s=60
        )

    def handle(self, request: Request) -> Response:
        """Handle one already-bounded HTTP request without exposing exceptions."""

        if (
            not isinstance(request.method, str)
            or not isinstance(request.path, str)
            or len(request.path) > 512
            or len(request.body) > MAX_HTTP_BODY_BYTES
        ):
            return _error(400, ErrorCode.INVALID_REQUEST, "Invalid request")
        try:
            self._rate_limiter.check(request.client_key, self._clock())
        except RateLimitError:
            return _error(429, ErrorCode.INVALID_REQUEST, "Request rate limit exceeded")
        except SecurityError:
            return _error(400, ErrorCode.INVALID_REQUEST, "Invalid client identity")

        try:
            method = HttpMethod(request.method.upper())
        except ValueError:
            return _error(405, ErrorCode.INVALID_REQUEST, "Method not allowed")
        if request.path == SESSION_PATH and method is HttpMethod.POST:
            return self._login(request)

        try:
            session = self._authenticate_request(request)
        except AuthenticationError:
            return _error(401, ErrorCode.AUTHENTICATION_REQUIRED, "Authentication required")

        if request.path == SESSION_PATH and method is HttpMethod.DELETE:
            return self._logout(request, session)
        if request.path == REAUTHENTICATE_PATH and method is HttpMethod.POST:
            return self._reauthenticate(request, session)

        matched = _match_endpoint(method, request.path)
        if matched is None:
            return _error(404, ErrorCode.NOT_FOUND, "Endpoint not found")
        endpoint, clip_id = matched
        if endpoint.csrf_required:
            try:
                self._sessions.require_csrf(session, _header(request, "X-CSRF-Token"))
            except CsrfError:
                return _error(403, ErrorCode.CSRF_FAILED, "CSRF validation failed")
        if endpoint.auth is AuthPolicy.REAUTHENTICATE:
            try:
                self._sessions.require_recent_reauthentication(session, self._clock())
            except AuthenticationError:
                return _error(
                    401,
                    ErrorCode.REAUTHENTICATION_REQUIRED,
                    "Recent reauthentication required",
                )
        try:
            return self._dispatch(method, request, clip_id, session)
        except ValueError:
            return _error(400, ErrorCode.INVALID_REQUEST, "Invalid request")
        except RecorderRemoteError as error:
            status = {
                ErrorCode.NOT_FOUND: 404,
                ErrorCode.AUTHENTICATION_REQUIRED: 401,
                ErrorCode.REAUTHENTICATION_REQUIRED: 401,
                ErrorCode.CSRF_FAILED: 403,
                ErrorCode.CLIP_BUSY: 409,
                ErrorCode.CONFLICT: 409,
                ErrorCode.INVALID_REQUEST: 400,
                ErrorCode.OPERATION_TIMEOUT: 504,
                ErrorCode.RECORDER_FAULT: 503,
                ErrorCode.STORAGE_FAULT: 503,
                ErrorCode.UNSUPPORTED_CONFIGURATION: 422,
                ErrorCode.INTERNAL_ERROR: 500,
            }[error.code]
            return _error(status, error.code, str(error), retryable=error.retryable)
        except RecorderProtocolError:
            return _error(
                503, ErrorCode.RECORDER_FAULT, "Recorder service unavailable", retryable=True
            )

    def _authenticate_request(self, request: Request) -> Session:
        authorization = _header(request, "Authorization")
        scheme, separator, token = authorization.partition(" ")
        if separator != " " or scheme.casefold() != "bearer":
            raise AuthenticationError("missing session")
        return self._sessions.authenticate(token, self._clock())

    def _login(self, request: Request) -> Response:
        try:
            self._login_rate_limiter.check(f"login:{request.client_key}", self._clock())
        except (RateLimitError, SecurityError):
            return _error(429, ErrorCode.INVALID_REQUEST, "Login rate limit exceeded")
        try:
            payload = _parse_object(request.body)
        except ValueError:
            return _error(400, ErrorCode.INVALID_REQUEST, "Invalid credentials request")
        if set(payload) != {"password"} or not isinstance(payload["password"], str):
            return _error(400, ErrorCode.INVALID_REQUEST, "Invalid credentials request")
        if not verify_password(payload["password"], self._password_record):
            return _error(401, ErrorCode.AUTHENTICATION_REQUIRED, "Invalid credentials")
        session = self._sessions.create(self._clock())
        return _json_response(
            201,
            {"session_token": session.token, "csrf_token": session.csrf_token},
        )

    def _logout(self, request: Request, session: Session) -> Response:
        try:
            self._sessions.require_csrf(session, _header(request, "X-CSRF-Token"))
        except CsrfError:
            return _error(403, ErrorCode.CSRF_FAILED, "CSRF validation failed")
        self._sessions.revoke(session.token)
        return Response(204, None, {"Cache-Control": "no-store"})

    def _reauthenticate(self, request: Request, session: Session) -> Response:
        try:
            self._sessions.require_csrf(session, _header(request, "X-CSRF-Token"))
            payload = _parse_object(request.body)
        except CsrfError:
            return _error(403, ErrorCode.CSRF_FAILED, "CSRF validation failed")
        except ValueError:
            return _error(400, ErrorCode.INVALID_REQUEST, "Invalid credentials request")
        if (
            set(payload) != {"password"}
            or not isinstance(payload["password"], str)
            or not verify_password(payload["password"], self._password_record)
        ):
            return _error(401, ErrorCode.REAUTHENTICATION_REQUIRED, "Invalid credentials")
        self._sessions.reauthenticate(session.token, self._clock())
        return Response(204, None, {"Cache-Control": "no-store"})

    def _dispatch(
        self, method: HttpMethod, request: Request, clip_id: str | None, session: Session
    ) -> Response:
        path = request.path
        result: JsonValue
        if method is HttpMethod.GET and path == f"{API_PREFIX}/status":
            result = self._recorder.call(RecorderCommand.STATUS)
        elif method is HttpMethod.GET and path == f"{API_PREFIX}/health":
            result = self._recorder.call(RecorderCommand.HEALTH)
        elif method is HttpMethod.GET and path == f"{API_PREFIX}/config":
            result = self._recorder.call(RecorderCommand.GET_CONFIG)
            result = _redact_secrets(result)  # defense in depth
        elif method is HttpMethod.PUT and path == f"{API_PREFIX}/config":
            result = self._recorder.call(RecorderCommand.UPDATE_CONFIG, _parse_object(request.body))
            result = _redact_secrets(result)
        elif method is HttpMethod.GET and path == f"{API_PREFIX}/clips":
            query = request.query or {}
            allowed = {"limit", "offset", "protected"}
            if set(query) - allowed:
                raise ValueError("unknown query")
            page = PageRequest(
                limit=int(query.get("limit", "50")),
                offset=int(query.get("offset", "0")),
            )
            protected = query.get("protected", "all")
            if protected not in {"all", "true", "false"}:
                raise ValueError("invalid protected filter")
            result = self._recorder.call(
                RecorderCommand.LIST_CLIPS,
                {"limit": page.limit, "offset": page.offset, "protected": protected},
            )
        elif clip_id is not None and method is HttpMethod.GET and path.endswith(
            ("/video", "/metadata")
        ):
            return _error(
                501,
                ErrorCode.UNSUPPORTED_CONFIGURATION,
                "Download delivery is not available in this release",
            )
        elif clip_id is not None:
            command = {
                (HttpMethod.GET, ""): RecorderCommand.GET_CLIP,
                (HttpMethod.POST, "protect"): RecorderCommand.PROTECT_CLIP,
                (HttpMethod.POST, "unprotect"): RecorderCommand.UNPROTECT_CLIP,
                (HttpMethod.DELETE, ""): RecorderCommand.DELETE_CLIP,
            }.get((method, path.rsplit("/", 1)[-1] if path.count("/") > 4 else ""))
            if command is None:
                raise ValueError("unsupported clip operation")
            result = self._recorder.call_for_clip(command, clip_id)
        elif method is HttpMethod.POST and path == f"{API_PREFIX}/event":
            payload = _parse_object(request.body)
            if set(payload) - {"source", "event_id"} or payload.get("source", "web") != "web":
                raise ValueError("invalid event")
            raw_event_id = payload.get("event_id")
            if raw_event_id is None:
                event_id = uuid4()
            elif isinstance(raw_event_id, str):
                event_id = UUID(raw_event_id)
                if str(event_id) != raw_event_id:
                    raise ValueError("invalid event ID")
            else:
                raise ValueError("invalid event ID")
            result = self._recorder.call(
                RecorderCommand.EVENT,
                {"source": "web", "event_id": str(event_id)},
            )
        elif method is HttpMethod.POST and path == f"{API_PREFIX}/recorder/restart":
            result = self._recorder.call(RecorderCommand.RESTART)
        elif method is HttpMethod.POST and path == f"{API_PREFIX}/system/prepare-sd-removal":
            payload = _parse_object(request.body)
            if payload != {"confirmation": "PREPARE SD CARD"}:
                raise ValueError("explicit confirmation required")
            result = self._recorder.call(RecorderCommand.PREPARE_REMOVAL)
        else:
            raise ValueError("unsupported operation")
        return _json_response(200, result)


__all__ = [
    "MAX_HTTP_BODY_BYTES",
    "REAUTHENTICATE_PATH",
    "SESSION_PATH",
    "Request",
    "Response",
    "WebApplication",
]
