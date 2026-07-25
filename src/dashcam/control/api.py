"""Version-1 API contracts that are independent of the web framework."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from dashcam.storage.naming import parse_clip_id as parse_clip_id

API_PREFIX: Final = "/api/v1"
_MAX_ERROR_MESSAGE_CHARS: Final = 512
_MAX_PAGE_LIMIT: Final = 200
_MAX_PAGE_OFFSET: Final = 1_000_000


class HttpMethod(StrEnum):
    """Methods used by the version-1 public API."""

    GET = "GET"
    PUT = "PUT"
    POST = "POST"
    DELETE = "DELETE"


class AuthPolicy(StrEnum):
    """Minimum authentication policy for an endpoint."""

    SESSION = "SESSION"
    REAUTHENTICATE = "REAUTHENTICATE"


class ErrorCode(StrEnum):
    """Stable public error codes; messages may become more specific."""

    INVALID_REQUEST = "INVALID_REQUEST"
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    REAUTHENTICATION_REQUIRED = "REAUTHENTICATION_REQUIRED"
    CSRF_FAILED = "CSRF_FAILED"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    CLIP_BUSY = "CLIP_BUSY"
    STORAGE_FAULT = "STORAGE_FAULT"
    RECORDER_FAULT = "RECORDER_FAULT"
    OPERATION_TIMEOUT = "OPERATION_TIMEOUT"
    UNSUPPORTED_CONFIGURATION = "UNSUPPORTED_CONFIGURATION"
    INTERNAL_ERROR = "INTERNAL_ERROR"


@dataclass(frozen=True, slots=True)
class EndpointContract:
    """Security and mutation contract for one public endpoint."""

    method: HttpMethod
    path: str
    auth: AuthPolicy = AuthPolicy.SESSION
    state_changing: bool = False
    csrf_required: bool = False

    def __post_init__(self) -> None:
        if not self.path.startswith(f"{API_PREFIX}/") and self.path != API_PREFIX:
            raise ValueError("endpoint must remain below the versioned API prefix")
        if self.state_changing and self.method is HttpMethod.GET:
            raise ValueError("GET endpoint cannot be state-changing")
        if self.state_changing and not self.csrf_required:
            raise ValueError("browser state-changing endpoint must require CSRF protection")
        if not self.state_changing and self.csrf_required:
            raise ValueError("read-only endpoint does not require a CSRF token")


def _endpoint(
    method: HttpMethod,
    suffix: str,
    *,
    state_changing: bool = False,
    reauthenticate: bool = False,
) -> EndpointContract:
    return EndpointContract(
        method=method,
        path=f"{API_PREFIX}{suffix}",
        auth=(AuthPolicy.REAUTHENTICATE if reauthenticate else AuthPolicy.SESSION),
        state_changing=state_changing,
        csrf_required=state_changing,
    )


PUBLIC_ENDPOINTS: Final = (
    _endpoint(HttpMethod.GET, "/status"),
    _endpoint(HttpMethod.GET, "/config"),
    _endpoint(HttpMethod.PUT, "/config", state_changing=True),
    _endpoint(HttpMethod.GET, "/clips"),
    _endpoint(HttpMethod.GET, "/clips/{clip_id}"),
    _endpoint(HttpMethod.GET, "/clips/{clip_id}/video"),
    _endpoint(HttpMethod.GET, "/clips/{clip_id}/metadata"),
    _endpoint(HttpMethod.POST, "/clips/{clip_id}/protect", state_changing=True),
    _endpoint(HttpMethod.POST, "/clips/{clip_id}/unprotect", state_changing=True),
    _endpoint(HttpMethod.DELETE, "/clips/{clip_id}", state_changing=True),
    _endpoint(HttpMethod.POST, "/event", state_changing=True),
    _endpoint(HttpMethod.POST, "/recorder/restart", state_changing=True),
    _endpoint(
        HttpMethod.POST,
        "/system/prepare-sd-removal",
        state_changing=True,
        reauthenticate=True,
    ),
    _endpoint(HttpMethod.GET, "/health"),
)

ENDPOINT_BY_METHOD_PATH: Final = MappingProxyType(
    {(endpoint.method, endpoint.path): endpoint for endpoint in PUBLIC_ENDPOINTS}
)


@dataclass(frozen=True, slots=True)
class ErrorResponse:
    """Closed structured error response safe for a client."""

    code: ErrorCode
    message: str
    retryable: bool = False
    field: str | None = None

    def __post_init__(self) -> None:
        if not self.message or len(self.message) > _MAX_ERROR_MESSAGE_CHARS:
            raise ValueError("error message has invalid length")
        if self.field is not None and (
            not self.field
            or len(self.field) > 128
            or not self.field.replace("_", "").replace(".", "").isalnum()
        ):
            raise ValueError("error field must be a bounded dotted identifier")

    def as_dict(self) -> dict[str, object]:
        """Return the stable public representation."""

        payload: dict[str, object] = {
            "error": {
                "code": self.code.value,
                "message": self.message,
                "retryable": self.retryable,
            }
        }
        if self.field is not None:
            error = payload["error"]
            assert isinstance(error, dict)
            error["field"] = self.field
        return payload


@dataclass(frozen=True, slots=True)
class RedactedSecret:
    """The only public representation of a configured secret."""

    is_set: bool

    def as_dict(self) -> dict[str, bool]:
        """Return an indicator without the value or a reversible fingerprint."""

        return {"is_set": self.is_set}


@dataclass(frozen=True, slots=True)
class PageRequest:
    """Bounded clip-list pagination."""

    limit: int = 50
    offset: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.limit, bool) or not 1 <= self.limit <= _MAX_PAGE_LIMIT:
            raise ValueError(f"limit must be between 1 and {_MAX_PAGE_LIMIT}")
        if isinstance(self.offset, bool) or self.offset < 0 or self.offset > _MAX_PAGE_OFFSET:
            raise ValueError(f"offset must be between 0 and {_MAX_PAGE_OFFSET}")
