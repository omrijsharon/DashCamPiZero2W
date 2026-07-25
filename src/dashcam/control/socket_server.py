"""Bounded recorder-side implementation of the local JSON socket protocol."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Awaitable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol, TypeAlias
from uuid import UUID

from dashcam.control.api import ErrorCode

PROTOCOL_VERSION: Final = 1
MAX_REQUEST_BYTES: Final = 64 * 1024
MAX_RESPONSE_BYTES: Final = 1024 * 1024
MAX_PROTOCOL_DEPTH: Final = 12
MAX_CONCURRENT_CLIENTS: Final = 8

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class ControlCommand(StrEnum):
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


class ControlProtocolError(ValueError):
    """A request or response violates the closed local protocol."""


class ControlOperationError(RuntimeError):
    """An expected command failure safe to return to the local client."""

    def __init__(self, code: ErrorCode, message: str, *, retryable: bool = False) -> None:
        if not isinstance(code, ErrorCode):
            raise TypeError("code must be an ErrorCode")
        if not isinstance(message, str) or not message or len(message) > 512:
            raise ValueError("operation error message must be bounded")
        if not isinstance(retryable, bool):
            raise TypeError("retryable must be boolean")
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class ControlRequest:
    request_id: UUID
    command: ControlCommand
    arguments: dict[str, JsonValue]


class ControlDispatcher(Protocol):
    def dispatch(
        self, command: ControlCommand, arguments: Mapping[str, JsonValue]
    ) -> Awaitable[Mapping[str, object]]:
        """Execute one allow-listed command without blocking the event loop."""


def _json_value(value: object, *, depth: int = 0) -> JsonValue:
    if depth > MAX_PROTOCOL_DEPTH:
        raise ControlProtocolError("protocol value exceeds nesting bound")
    if value is None or isinstance(value, str | bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ControlProtocolError("protocol numbers must be finite")
        return value
    if isinstance(value, list):
        return [_json_value(item, depth=depth + 1) for item in value]
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 128:
                raise ControlProtocolError("protocol keys must be bounded strings")
            result[key] = _json_value(item, depth=depth + 1)
        return result
    raise ControlProtocolError("protocol value is not JSON-compatible")


def decode_request(frame: bytes) -> ControlRequest:
    """Decode one newline-free request with exact top-level fields."""

    if not isinstance(frame, bytes) or not frame or len(frame) > MAX_REQUEST_BYTES:
        raise ControlProtocolError("request frame has invalid length")
    if b"\n" in frame or b"\0" in frame:
        raise ControlProtocolError("request frame contains a forbidden delimiter")
    try:
        raw = json.loads(frame)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ControlProtocolError("request is not valid JSON") from error
    validated = _json_value(raw)
    if not isinstance(validated, dict):
        raise ControlProtocolError("request must be an object")
    if set(validated) != {"version", "request_id", "command", "arguments"}:
        raise ControlProtocolError("request has missing or unknown fields")
    if validated["version"] != PROTOCOL_VERSION:
        raise ControlProtocolError("protocol version mismatch")
    raw_id = validated["request_id"]
    raw_command = validated["command"]
    arguments = validated["arguments"]
    if not isinstance(raw_id, str) or not isinstance(raw_command, str):
        raise ControlProtocolError("request identity and command must be strings")
    try:
        request_id = UUID(raw_id)
    except ValueError as error:
        raise ControlProtocolError("request ID must be a UUID") from error
    if str(request_id) != raw_id:
        raise ControlProtocolError("request ID must be canonical")
    try:
        command = ControlCommand(raw_command)
    except ValueError as error:
        raise ControlProtocolError("command is not allow-listed") from error
    if not isinstance(arguments, dict):
        raise ControlProtocolError("arguments must be an object")
    return ControlRequest(request_id, command, arguments)


def _encode(payload: Mapping[str, object]) -> bytes:
    validated = _json_value(payload)
    encoded = json.dumps(
        validated, ensure_ascii=True, allow_nan=False, separators=(",", ":")
    ).encode()
    if len(encoded) + 1 > MAX_RESPONSE_BYTES:
        raise ControlProtocolError("response exceeds byte limit")
    return encoded + b"\n"


def encode_success(request_id: UUID, result: Mapping[str, object]) -> bytes:
    return _encode(
        {
            "version": PROTOCOL_VERSION,
            "request_id": str(request_id),
            "ok": True,
            "result": result,
        }
    )


def encode_error(
    request_id: UUID,
    code: ErrorCode,
    message: str,
    *,
    retryable: bool = False,
) -> bytes:
    error = ControlOperationError(code, message, retryable=retryable)
    return _encode(
        {
            "version": PROTOCOL_VERSION,
            "request_id": str(request_id),
            "ok": False,
            "error": {
                "code": error.code.value,
                "message": str(error),
                "retryable": error.retryable,
            },
        }
    )


async def execute_request(request: ControlRequest, dispatcher: ControlDispatcher) -> bytes:
    """Dispatch one request and turn all failures into closed responses."""

    try:
        result = await dispatcher.dispatch(request.command, request.arguments)
        return encode_success(request.request_id, result)
    except ControlOperationError as error:
        return encode_error(
            request.request_id,
            error.code,
            str(error),
            retryable=error.retryable,
        )
    except TimeoutError:
        return encode_error(
            request.request_id,
            ErrorCode.OPERATION_TIMEOUT,
            "Recorder operation timed out",
            retryable=True,
        )
    except Exception:
        return encode_error(
            request.request_id,
            ErrorCode.INTERNAL_ERROR,
            "Recorder operation failed",
        )


class BoundedConnectionHandler:
    """Serve one request per stream with concurrency, byte, and time bounds."""

    def __init__(
        self,
        dispatcher: ControlDispatcher,
        *,
        request_timeout_s: float = 5.0,
        max_concurrent_clients: int = MAX_CONCURRENT_CLIENTS,
    ) -> None:
        if (
            isinstance(request_timeout_s, bool)
            or not isinstance(request_timeout_s, int | float)
            or not math.isfinite(request_timeout_s)
            or not 0.05 <= request_timeout_s <= 30
        ):
            raise ValueError("request timeout must be between 0.05 and 30 seconds")
        if (
            isinstance(max_concurrent_clients, bool)
            or not isinstance(max_concurrent_clients, int)
            or not 1 <= max_concurrent_clients <= MAX_CONCURRENT_CLIENTS
        ):
            raise ValueError(
                f"max_concurrent_clients must be between 1 and {MAX_CONCURRENT_CLIENTS}"
            )
        self._dispatcher = dispatcher
        self._timeout_s = float(request_timeout_s)
        self._slots = asyncio.Semaphore(max_concurrent_clients)

    async def __call__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Read, dispatch, drain, and close one connection."""

        async with self._slots:
            try:
                framed = await asyncio.wait_for(reader.readline(), timeout=self._timeout_s)
                if not framed.endswith(b"\n") or len(framed) > MAX_REQUEST_BYTES:
                    return
                request = decode_request(framed[:-1])
                response = await asyncio.wait_for(
                    execute_request(request, self._dispatcher),
                    timeout=self._timeout_s,
                )
                writer.write(response)
                await asyncio.wait_for(writer.drain(), timeout=self._timeout_s)
            except (
                asyncio.IncompleteReadError,
                asyncio.LimitOverrunError,
                ConnectionError,
                ControlProtocolError,
                OSError,
                TimeoutError,
            ):
                return
            finally:
                writer.close()
                with suppress(ConnectionError, OSError, TimeoutError):
                    await asyncio.wait_for(writer.wait_closed(), timeout=1.0)


__all__ = [
    "MAX_CONCURRENT_CLIENTS",
    "MAX_REQUEST_BYTES",
    "MAX_RESPONSE_BYTES",
    "PROTOCOL_VERSION",
    "BoundedConnectionHandler",
    "ControlCommand",
    "ControlDispatcher",
    "ControlOperationError",
    "ControlProtocolError",
    "ControlRequest",
    "JsonValue",
    "decode_request",
    "encode_error",
    "encode_success",
    "execute_request",
]
