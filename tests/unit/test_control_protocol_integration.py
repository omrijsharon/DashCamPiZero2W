from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

from dashcam.control.socket_server import ControlCommand, decode_request, execute_request
from dashcam_web.recorder_client import (
    RecorderClient,
    RecorderCommand,
    RecorderTransport,
)


class Dispatcher:
    async def dispatch(
        self, command: ControlCommand, arguments: Mapping[str, Any]
    ) -> dict[str, object]:
        return {"command": command.value, "arguments": dict(arguments)}


class InProcessTransport(RecorderTransport):
    def exchange(self, request: bytes, *, timeout_s: float, max_response_bytes: int) -> bytes:
        assert request.endswith(b"\n")
        decoded = decode_request(request[:-1])
        response = asyncio.run(execute_request(decoded, Dispatcher()))
        assert len(response) <= max_response_bytes
        return response[:-1]


def test_web_and_recorder_command_sets_cannot_drift() -> None:
    assert {command.value for command in RecorderCommand} == {
        command.value for command in ControlCommand
    }


def test_client_and_server_round_trip_correlates_request_and_arguments() -> None:
    result = RecorderClient(InProcessTransport()).call(
        RecorderCommand.LIST_CLIPS,
        {"limit": 10, "offset": 20, "protected": "false"},
    )

    assert result == {
        "command": "list_clips",
        "arguments": {"limit": 10, "offset": 20, "protected": "false"},
    }
    json.dumps(result, allow_nan=False)
