"""Bounded recorder-side implementation of the local JSON socket protocol."""

from __future__ import annotations

import asyncio
import json
import math
import os
import stat
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol, TypeAlias, cast
from uuid import UUID

from dashcam.control.api import ErrorCode

PROTOCOL_VERSION: Final = 1
MAX_REQUEST_BYTES: Final = 64 * 1024
MAX_RESPONSE_BYTES: Final = 1024 * 1024
MAX_PROTOCOL_DEPTH: Final = 12
MAX_CONCURRENT_CLIENTS: Final = 8
DEFAULT_CONTROL_SOCKET_PATH: Final = Path("/run/dashcam/control.sock")
DEFAULT_DRAIN_TIMEOUT_S: Final = 5.0

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


def _chown_no_follow(path: Path, uid: int, gid: int) -> None:
    chown = cast(Callable[..., None], getattr(os, "chown"))  # noqa: B009
    chown(path, uid, gid, follow_symlinks=False)


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
        self._max_concurrent_clients = max_concurrent_clients
        self._slots = asyncio.Semaphore(max_concurrent_clients)

    @property
    def max_concurrent_clients(self) -> int:
        """Expose the hard listener admission cap paired with this handler."""

        return self._max_concurrent_clients

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


class RecorderUnixServer:
    """Recorder-owned bounded AF_UNIX listener with safe path lifecycle."""

    def __init__(
        self,
        handler: BoundedConnectionHandler,
        *,
        path: Path = DEFAULT_CONTROL_SOCKET_PATH,
        socket_group_id: int | None = None,
        owner_uid: int | None = None,
        drain_timeout_s: float = DEFAULT_DRAIN_TIMEOUT_S,
        fault_callback: Callable[[str], None] | None = None,
    ) -> None:
        if not callable(handler):
            raise TypeError("handler must be callable")
        path_text = str(path)
        if not path.is_absolute() or "\0" in path_text or len(os.fsencode(path_text)) > 100:
            raise ValueError("control socket path must be a bounded absolute path")
        if socket_group_id is not None and (
            isinstance(socket_group_id, bool)
            or not isinstance(socket_group_id, int)
            or socket_group_id < 0
        ):
            raise ValueError("socket_group_id must be a non-negative integer")
        if owner_uid is not None and (
            isinstance(owner_uid, bool) or not isinstance(owner_uid, int) or owner_uid < 0
        ):
            raise ValueError("owner_uid must be a non-negative integer")
        if (
            isinstance(drain_timeout_s, bool)
            or not isinstance(drain_timeout_s, int | float)
            or not math.isfinite(drain_timeout_s)
            or not 0.05 <= drain_timeout_s <= 30
        ):
            raise ValueError("drain timeout must be between 0.05 and 30 seconds")
        self._handler = handler
        self._path = path
        self._socket_group_id = socket_group_id
        self._owner_uid = owner_uid
        self._drain_timeout_s = float(drain_timeout_s)
        if fault_callback is not None and not callable(fault_callback):
            raise TypeError("fault_callback must be callable")
        self._fault_callback = fault_callback
        self._server: asyncio.AbstractServer | None = None
        self._serve_task: asyncio.Task[None] | None = None
        self._connections: set[asyncio.Task[None]] = set()
        self._socket_identity: tuple[int, int] | None = None
        self._accepting = False
        self._connections_started = 0
        self._connections_completed = 0
        self._connections_refused = 0
        self._connection_faults = 0
        self._last_error: str | None = None

    def snapshot(self) -> dict[str, object]:
        """Return bounded listener evidence without exposing client data."""

        return {
            "state": "LISTENING" if self._accepting else "INACTIVE",
            "path": str(self._path),
            "active_connections": len(self._connections),
            "connections_started": self._connections_started,
            "connections_completed": self._connections_completed,
            "connections_refused": self._connections_refused,
            "faults": self._connection_faults,
            "last_error": self._last_error,
        }

    async def start(self) -> None:
        """Bind only after validating the runtime directory and any stale leaf."""

        if self._server is not None:
            raise RuntimeError("control listener is already started")
        try:
            self._prepare_parent()
            self._remove_stale_owned_socket()
            start_unix_server = cast(
                Callable[..., Awaitable[asyncio.AbstractServer]],
                getattr(asyncio, "start_unix_server"),  # noqa: B009
            )
            server = await start_unix_server(
                self._accept_connection,
                path=str(self._path),
                limit=MAX_REQUEST_BYTES + 1,
                start_serving=False,
            )
            info = os.lstat(self._path)
            self._require_owned_socket(info)
            self._socket_identity = (info.st_dev, info.st_ino)
            if self._socket_group_id is not None:
                _chown_no_follow(self._path, -1, self._socket_group_id)
            os.chmod(self._path, 0o660, follow_symlinks=False)
            self._accepting = True
            await server.start_serving()
        except BaseException as error:
            self._accepting = False
            if "server" in locals():
                server.close()
                with suppress(Exception):
                    await server.wait_closed()
            self._record_fault(error)
            if self._socket_identity is not None:
                self._remove_current_socket_if_owned()
            raise
        self._server = server
        self._serve_task = asyncio.create_task(
            self._serve(server),
            name="dashcam-control-listener",
        )

    async def stop(self) -> None:
        """Stop accepting, boundedly drain handlers, then remove only our socket."""

        self._accepting = False
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            with suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), timeout=self._drain_timeout_s)
        serve_task = self._serve_task
        self._serve_task = None
        if serve_task is not None and not serve_task.done():
            serve_task.cancel()
        if serve_task is not None:
            with suppress(asyncio.CancelledError, OSError):
                await serve_task
        pending = tuple(self._connections)
        if pending:
            _done, remaining = await asyncio.wait(pending, timeout=self._drain_timeout_s)
            for task in remaining:
                task.cancel()
            if remaining:
                await asyncio.gather(*remaining, return_exceptions=True)
        self._remove_current_socket_if_owned()

    async def _serve(self, server: asyncio.AbstractServer) -> None:
        try:
            await server.serve_forever()
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            self._accepting = False
            server.close()
            with suppress(Exception):
                await asyncio.wait_for(
                    server.wait_closed(),
                    timeout=self._drain_timeout_s,
                )
            if self._server is server:
                self._server = None
            self._remove_current_socket_if_owned()
            self._record_fault(error)
            callback = self._fault_callback
            if callback is not None:
                try:
                    callback(self._last_error or "control listener failed")
                except BaseException as callback_error:
                    self._record_fault(callback_error)

    def _accept_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Synchronously cap and register an accepted stream before yielding."""

        if (
            not self._accepting
            or len(self._connections) >= self._handler.max_concurrent_clients
        ):
            self._connections_refused += 1
            writer.close()
            return
        task = asyncio.create_task(
            self._handle_connection(reader, writer),
            name="dashcam-control-client",
        )
        self._connections.add(task)
        self._connections_started += 1
        task.add_done_callback(self._connection_finished)

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            await self._handler(reader, writer)
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            self._record_fault(error)

    def _connection_finished(self, task: asyncio.Task[None]) -> None:
        self._connections.discard(task)
        self._connections_completed += 1
        if task.cancelled():
            return
        try:
            error = task.exception()
        except BaseException as error:
            self._record_fault(error)
            return
        if error is not None:
            self._record_fault(error)

    def _prepare_parent(self) -> None:
        parent = self._path.parent
        info = os.lstat(parent)
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise OSError("control socket parent is not a real directory")
        if self._owner_uid is not None and info.st_uid != self._owner_uid:
            raise OSError("control socket parent has a foreign owner")
        if self._socket_group_id is not None:
            _chown_no_follow(parent, -1, self._socket_group_id)
        os.chmod(parent, 0o750, follow_symlinks=False)

    def _remove_stale_owned_socket(self) -> None:
        try:
            info = os.lstat(self._path)
        except FileNotFoundError:
            return
        self._require_owned_socket(info)
        os.unlink(self._path)

    def _remove_current_socket_if_owned(self) -> None:
        try:
            info = os.lstat(self._path)
        except FileNotFoundError:
            return
        try:
            self._require_owned_socket(info)
        except OSError as error:
            self._record_fault(error)
            return
        identity = self._socket_identity
        if identity is not None and (info.st_dev, info.st_ino) != identity:
            self._record_fault(OSError("control socket path identity changed"))
            return
        os.unlink(self._path)
        self._socket_identity = None

    def _require_owned_socket(self, info: os.stat_result) -> None:
        if not stat.S_ISSOCK(info.st_mode):
            raise OSError("control socket path is not a socket")
        if self._owner_uid is not None and info.st_uid != self._owner_uid:
            raise OSError("control socket path has a foreign owner")

    def _record_fault(self, error: BaseException) -> None:
        self._connection_faults += 1
        detail = " ".join(f"{type(error).__name__}: {error}".splitlines()).strip()
        self._last_error = detail[:256] if detail else type(error).__name__


__all__ = [
    "DEFAULT_CONTROL_SOCKET_PATH",
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
    "RecorderUnixServer",
    "decode_request",
    "encode_error",
    "encode_success",
    "execute_request",
]
