"""Bounded client for the privileged recorder's versioned Unix-socket protocol."""

from __future__ import annotations

import json
import math
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol, TypeAlias, cast
from uuid import UUID, uuid4

from dashcam.control.api import ErrorCode, parse_clip_id

PROTOCOL_VERSION: Final = 1
DEFAULT_SOCKET_PATH: Final = Path("/run/dashcam/control.sock")
MAX_REQUEST_BYTES: Final = 64 * 1024
MAX_RESPONSE_BYTES: Final = 1024 * 1024
MAX_PROTOCOL_DEPTH: Final = 12
DEFAULT_TIMEOUT_S: Final = 12.0

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class RecorderCommand(StrEnum):
    STATUS = "status"
    GET_CONFIG = "get_config"
    UPDATE_CONFIG = "update_config"
    LIST_CLIPS = "list_clips"
    GET_CLIP = "get_clip"
    ACQUIRE_DOWNLOAD = "acquire_download"
    RELEASE_DOWNLOAD = "release_download"
    PROTECT_CLIP = "protect_clip"
    UNPROTECT_CLIP = "unprotect_clip"
    DELETE_CLIP = "delete_clip"
    EVENT = "event"
    RESTART = "restart"
    PREPARE_REMOVAL = "prepare_removal"
    HEALTH = "health"


class RecorderProtocolError(RuntimeError):
    """The recorder transport or response violated the bounded protocol."""


class RecorderRemoteError(RecorderProtocolError):
    """A structured, expected failure returned by the recorder."""

    def __init__(self, code: ErrorCode, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class RecorderTransport(Protocol):
    def exchange(self, request: bytes, *, timeout_s: float, max_response_bytes: int) -> bytes:
        """Send exactly one request and return exactly one framed response."""


def _bounded_timeout(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RecorderProtocolError("timeout must be numeric")
    timeout = float(value)
    if not math.isfinite(timeout) or not 0.05 <= timeout <= 30:
        raise RecorderProtocolError("timeout must be between 0.05 and 30 seconds")
    return timeout


def _validate_json(value: object, *, depth: int = 0) -> JsonValue:
    if depth > MAX_PROTOCOL_DEPTH:
        raise RecorderProtocolError("protocol value exceeds nesting bound")
    if value is None or isinstance(value, str | bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RecorderProtocolError("protocol numbers must be finite")
        return value
    if isinstance(value, list):
        return [_validate_json(item, depth=depth + 1) for item in value]
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 128:
                raise RecorderProtocolError("protocol keys must be bounded non-empty strings")
            result[key] = _validate_json(item, depth=depth + 1)
        return result
    raise RecorderProtocolError("protocol value is not JSON-compatible")


class UnixSocketTransport:
    """One-request-per-connection transport with byte and time bounds."""

    def __init__(self, path: Path = DEFAULT_SOCKET_PATH) -> None:
        if not path.is_absolute() or "\x00" in str(path):
            raise RecorderProtocolError("recorder socket path must be absolute and bounded")
        if len(str(path).encode()) > 200:
            raise RecorderProtocolError("recorder socket path is too long")
        self._path = path

    def exchange(self, request: bytes, *, timeout_s: float, max_response_bytes: int) -> bytes:
        if not request.endswith(b"\n") or len(request) > MAX_REQUEST_BYTES:
            raise RecorderProtocolError("request frame is invalid")
        if not 1 <= max_response_bytes <= MAX_RESPONSE_BYTES:
            raise RecorderProtocolError("response bound is invalid")
        timeout = _bounded_timeout(timeout_s)
        chunks: list[bytes] = []
        size = 0
        unix_family = getattr(socket, "AF_UNIX", None)
        if unix_family is None:
            raise RecorderProtocolError("Unix-domain sockets are unavailable on this host")
        try:
            with socket.socket(unix_family, socket.SOCK_STREAM) as connection:
                connection.settimeout(timeout)
                connection.connect(str(self._path))
                connection.sendall(request)
                connection.shutdown(socket.SHUT_WR)
                while True:
                    chunk = connection.recv(min(16 * 1024, max_response_bytes + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > max_response_bytes:
                        raise RecorderProtocolError("recorder response exceeds byte limit")
                    if b"\n" in chunk:
                        break
        except (OSError, TimeoutError) as error:
            raise RecorderProtocolError("recorder socket request failed") from error
        response = b"".join(chunks)
        if not response.endswith(b"\n") or response.count(b"\n") != 1:
            raise RecorderProtocolError("recorder returned an invalid frame")
        return response[:-1]


@dataclass(frozen=True, slots=True)
class ApprovedDownload:
    """Recorder-issued bounded lease; byte delivery remains an M11 data plane."""

    clip_id: UUID
    lease_id: str
    member: str
    expires_at_monotonic_ns: int

    def __post_init__(self) -> None:
        if not isinstance(self.clip_id, UUID):
            raise RecorderProtocolError("download clip ID is invalid")
        if (
            not self.lease_id.isascii()
            or not 16 <= len(self.lease_id) <= 128
            or not self.lease_id.replace("-", "").replace("_", "").isalnum()
        ):
            raise RecorderProtocolError("download lease ID is invalid")
        if self.member not in {"video", "metadata"}:
            raise RecorderProtocolError("download member is invalid")
        if (
            isinstance(self.expires_at_monotonic_ns, bool)
            or not isinstance(self.expires_at_monotonic_ns, int)
            or self.expires_at_monotonic_ns < 0
        ):
            raise RecorderProtocolError("download lease expiry is invalid")


class RecorderClient:
    """Strict command client; callers cannot submit arbitrary privileged commands."""

    def __init__(
        self,
        transport: RecorderTransport,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._transport = transport
        self._timeout_s = _bounded_timeout(timeout_s)

    def call(
        self,
        command: RecorderCommand,
        arguments: Mapping[str, object] | None = None,
    ) -> dict[str, JsonValue]:
        if not isinstance(command, RecorderCommand):
            raise RecorderProtocolError("command is not allow-listed")
        request_id = str(uuid4())
        request: dict[str, JsonValue] = {
            "version": PROTOCOL_VERSION,
            "request_id": request_id,
            "command": command.value,
            "arguments": _validate_json(arguments or {}),
        }
        encoded = (
            json.dumps(request, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()
            + b"\n"
        )
        if len(encoded) > MAX_REQUEST_BYTES:
            raise RecorderProtocolError("recorder request exceeds byte limit")
        response_bytes = self._transport.exchange(
            encoded,
            timeout_s=self._timeout_s,
            max_response_bytes=MAX_RESPONSE_BYTES,
        )
        try:
            decoded = json.loads(response_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RecorderProtocolError("recorder returned invalid JSON") from error
        validated = _validate_json(decoded)
        if not isinstance(validated, dict):
            raise RecorderProtocolError("recorder response must be an object")
        if validated.get("version") != PROTOCOL_VERSION:
            raise RecorderProtocolError("recorder protocol version mismatch")
        if validated.get("request_id") != request_id:
            raise RecorderProtocolError("recorder response ID mismatch")
        ok = validated.get("ok")
        if not isinstance(ok, bool):
            raise RecorderProtocolError("recorder response is missing boolean ok")
        if ok:
            if set(validated) != {"version", "request_id", "ok", "result"}:
                raise RecorderProtocolError("successful recorder response has unknown fields")
            result = validated["result"]
            if not isinstance(result, dict):
                raise RecorderProtocolError("recorder result must be an object")
            return result
        if set(validated) != {"version", "request_id", "ok", "error"}:
            raise RecorderProtocolError("failed recorder response has unknown fields")
        decoded_error = validated["error"]
        if not isinstance(decoded_error, dict):
            raise RecorderProtocolError("recorder error must be an object")
        raw_code = decoded_error.get("code")
        if not isinstance(raw_code, str):
            raise RecorderProtocolError("recorder returned an invalid error code")
        try:
            code = ErrorCode(raw_code)
        except ValueError as exception:
            raise RecorderProtocolError("recorder returned an unknown error code") from exception
        message = decoded_error.get("message")
        retryable = decoded_error.get("retryable", False)
        if (
            not isinstance(message, str)
            or not message
            or len(message) > 512
            or not isinstance(retryable, bool)
        ):
            raise RecorderProtocolError("recorder returned an invalid error")
        raise RecorderRemoteError(code, message, retryable=retryable)

    def call_for_clip(
        self,
        command: RecorderCommand,
        clip_id: str,
        *,
        extra: Mapping[str, object] | None = None,
    ) -> dict[str, JsonValue]:
        canonical = str(parse_clip_id(clip_id))
        arguments: dict[str, object] = {"clip_id": canonical}
        if extra:
            arguments.update(extra)
        return self.call(command, arguments)

    def acquire_download(self, clip_id: str, *, member: str, holder: str) -> ApprovedDownload:
        if member not in {"video", "metadata"}:
            raise RecorderProtocolError("download member must be video or metadata")
        result = self.call_for_clip(
            RecorderCommand.ACQUIRE_DOWNLOAD,
            clip_id,
            extra={"member": member, "holder": holder},
        )
        try:
            returned_clip_id = parse_clip_id(cast(str, result["clip_id"]))
            lease_id = cast(str, result["lease_id"])
            returned_member = cast(str, result["member"])
            expiry = cast(int, result["expires_at_monotonic_ns"])
        except (KeyError, TypeError, ValueError) as error:
            raise RecorderProtocolError("recorder returned an invalid download approval") from error
        requested_clip_id = parse_clip_id(clip_id)
        if returned_clip_id != requested_clip_id:
            raise RecorderProtocolError("download approval clip ID mismatch")
        if returned_member != member:
            raise RecorderProtocolError("download approval member mismatch")
        return ApprovedDownload(returned_clip_id, lease_id, returned_member, expiry)


__all__ = [
    "DEFAULT_SOCKET_PATH",
    "MAX_REQUEST_BYTES",
    "MAX_RESPONSE_BYTES",
    "ApprovedDownload",
    "JsonValue",
    "RecorderClient",
    "RecorderCommand",
    "RecorderProtocolError",
    "RecorderRemoteError",
    "RecorderTransport",
    "UnixSocketTransport",
]
