from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from dashcam.control.api import ErrorCode
from dashcam.control.socket_server import (
    MAX_REQUEST_BYTES,
    BoundedConnectionHandler,
    ControlCommand,
    ControlOperationError,
    ControlProtocolError,
    RecorderUnixServer,
    decode_request,
    encode_error,
    encode_success,
    execute_request,
)

REQUEST_ID = UUID("00000000-0000-0000-0000-000000000123")


def _frame(
    *,
    command: object = "status",
    arguments: object = None,
    request_id: object = str(REQUEST_ID),
    version: object = 1,
) -> bytes:
    return json.dumps(
        {
            "version": version,
            "request_id": request_id,
            "command": command,
            "arguments": {} if arguments is None else arguments,
        }
    ).encode()


def test_decode_accepts_only_exact_versioned_allow_listed_request() -> None:
    request = decode_request(_frame(arguments={"limit": 20}))

    assert request.request_id == REQUEST_ID
    assert request.command is ControlCommand.STATUS
    assert request.arguments == {"limit": 20}


@pytest.mark.parametrize(
    "frame",
    [
        b"",
        b"not-json",
        _frame(version=2),
        _frame(command="shell"),
        _frame(request_id="../path"),
        _frame(arguments=[]),
        _frame(arguments={"number": float("nan")}),
        _frame() + b"\n",
        b"x" * (MAX_REQUEST_BYTES + 1),
    ],
    ids=[
        "empty",
        "invalid-json",
        "wrong-version",
        "unknown-command",
        "invalid-request-id",
        "non-object-arguments",
        "non-finite-number",
        "embedded-newline",
        "oversized",
    ],
)
def test_decode_rejects_malformed_or_dangerous_requests(frame: bytes) -> None:
    with pytest.raises(ControlProtocolError):
        decode_request(frame)


def test_response_encoding_is_closed_and_correlated() -> None:
    success = json.loads(encode_success(REQUEST_ID, {"state": "RECORDING"}))
    failure = json.loads(encode_error(REQUEST_ID, ErrorCode.CLIP_BUSY, "Clip busy", retryable=True))

    assert success == {
        "version": 1,
        "request_id": str(REQUEST_ID),
        "ok": True,
        "result": {"state": "RECORDING"},
    }
    assert failure == {
        "version": 1,
        "request_id": str(REQUEST_ID),
        "ok": False,
        "error": {"code": "CLIP_BUSY", "message": "Clip busy", "retryable": True},
    }


class Dispatcher:
    def __init__(self, outcome: object) -> None:
        self.outcome = outcome
        self.calls: list[tuple[ControlCommand, dict[str, object]]] = []

    async def dispatch(
        self, command: ControlCommand, arguments: Mapping[str, Any]
    ) -> dict[str, object]:
        self.calls.append((command, dict(arguments)))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        assert isinstance(self.outcome, dict)
        return self.outcome


class MemoryWriter:
    def __init__(self) -> None:
        self.value = bytearray()
        self.closed = False

    def write(self, value: bytes) -> None:
        self.value.extend(value)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def test_execute_dispatches_success() -> None:
    request = decode_request(_frame(command="health"))
    dispatcher = Dispatcher({"ok": True})

    response = json.loads(asyncio.run(execute_request(request, dispatcher)))

    assert response["ok"] is True
    assert dispatcher.calls == [(ControlCommand.HEALTH, {})]


@pytest.mark.parametrize(
    ("error", "code", "retryable"),
    [
        (ControlOperationError(ErrorCode.STORAGE_FAULT, "Unavailable"), "STORAGE_FAULT", False),
        (TimeoutError(), "OPERATION_TIMEOUT", True),
        (RuntimeError("secret traceback detail"), "INTERNAL_ERROR", False),
    ],
)
def test_execute_returns_closed_errors_without_internal_details(
    error: BaseException, code: str, retryable: bool
) -> None:
    request = decode_request(_frame())
    response = json.loads(asyncio.run(execute_request(request, Dispatcher(error))))

    assert response["error"]["code"] == code
    assert response["error"]["retryable"] is retryable
    assert "secret traceback detail" not in response["error"]["message"]


def test_response_size_and_depth_are_bounded() -> None:
    with pytest.raises(ControlProtocolError):
        encode_success(REQUEST_ID, {"data": "x" * (1024 * 1024)})

    nested: dict[str, object] = {}
    cursor = nested
    for _ in range(20):
        child: dict[str, object] = {}
        cursor["child"] = child
        cursor = child
    with pytest.raises(ControlProtocolError):
        encode_success(REQUEST_ID, nested)


def test_listener_synchronously_caps_and_tracks_handlers_before_they_run() -> None:
    class BlockingDispatcher:
        def __init__(self) -> None:
            self.release = asyncio.Event()

        async def dispatch(
            self,
            command: ControlCommand,
            arguments: Mapping[str, Any],
        ) -> dict[str, object]:
            del command, arguments
            await self.release.wait()
            return {}

    async def scenario() -> None:
        dispatcher = BlockingDispatcher()
        server = RecorderUnixServer(
            BoundedConnectionHandler(dispatcher, max_concurrent_clients=2),
            path=Path.cwd() / "control.sock",
            drain_timeout_s=0.05,
        )
        server._accepting = True
        writers: list[MemoryWriter] = []
        for _ in range(8):
            reader = asyncio.StreamReader()
            reader.feed_data(_frame() + b"\n")
            reader.feed_eof()
            writer = MemoryWriter()
            writers.append(writer)
            server._accept_connection(reader, writer)

        snapshot = server.snapshot()
        assert snapshot["active_connections"] == 2
        assert snapshot["connections_started"] == 2
        assert snapshot["connections_refused"] == 6
        assert all(writer.closed for writer in writers[2:])

        await server.stop()
        assert server.snapshot()["active_connections"] == 0
        assert server.snapshot()["connections_completed"] == 2

        reader = asyncio.StreamReader()
        writer = MemoryWriter()
        server._accept_connection(reader, writer)
        await server.stop()
        assert writer.closed
        assert server.snapshot()["active_connections"] == 0
        assert server.snapshot()["connections_completed"] == 2
        assert server.snapshot()["connections_refused"] == 7

    asyncio.run(scenario())


def test_post_start_serve_fault_closes_listener_and_notifies_degradation() -> None:
    class FailingServer:
        def __init__(self) -> None:
            self.closed = False

        async def serve_forever(self) -> None:
            raise OSError("injected serve failure")

        def close(self) -> None:
            self.closed = True

        async def wait_closed(self) -> None:
            return None

    async def scenario() -> None:
        faults: list[str] = []
        listener = RecorderUnixServer(
            BoundedConnectionHandler(Dispatcher({})),
            path=Path.cwd() / "control.sock",
            fault_callback=faults.append,
        )
        server = FailingServer()
        listener._server = server  # type: ignore[assignment]
        listener._accepting = True

        await listener._serve(server)  # type: ignore[arg-type]

        assert server.closed
        assert listener.snapshot()["state"] == "INACTIVE"
        assert listener.snapshot()["faults"] == 1
        assert faults and "injected serve failure" in faults[0]
        assert listener._server is None

    asyncio.run(scenario())


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX AF_UNIX ownership semantics")
def test_recorder_owned_listener_serves_and_drains_before_unlink(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "control.sock"
        server = RecorderUnixServer(
            BoundedConnectionHandler(Dispatcher({"state": "RECORDING"})),
            path=path,
            owner_uid=os.geteuid(),
        )
        await server.start()
        reader, writer = await asyncio.open_unix_connection(path)
        writer.write(_frame() + b"\n")
        await writer.drain()
        response = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
        await server.stop()

        assert response["result"] == {"state": "RECORDING"}
        assert not path.exists()
        snapshot = server.snapshot()
        assert snapshot["connections_started"] == 1
        assert snapshot["connections_completed"] == 1
        assert snapshot["active_connections"] == 0

    asyncio.run(scenario())


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX AF_UNIX ownership semantics")
def test_listener_refuses_foreign_leaf_and_never_unlinks_replacement(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "control.sock"
        path.write_text("foreign", encoding="ascii")
        refused = RecorderUnixServer(
            BoundedConnectionHandler(Dispatcher({})),
            path=path,
            owner_uid=os.geteuid(),
        )
        with pytest.raises(OSError, match="not a socket"):
            await refused.start()
        assert path.read_text(encoding="ascii") == "foreign"

        path.unlink()
        server = RecorderUnixServer(
            BoundedConnectionHandler(Dispatcher({})),
            path=path,
            owner_uid=os.geteuid(),
        )
        await server.start()
        path.unlink()
        path.write_text("replacement", encoding="ascii")
        await server.stop()
        assert path.read_text(encoding="ascii") == "replacement"
        assert server.snapshot()["faults"] == 1

    asyncio.run(scenario())
