from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any
from uuid import UUID

import pytest

from dashcam.control.api import ErrorCode
from dashcam.control.socket_server import (
    MAX_REQUEST_BYTES,
    ControlCommand,
    ControlOperationError,
    ControlProtocolError,
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
