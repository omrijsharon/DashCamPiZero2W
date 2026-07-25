from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any

import pytest

from dashcam.control.api import ErrorCode
from dashcam_web.recorder_client import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    RecorderClient,
    RecorderCommand,
    RecorderProtocolError,
    RecorderRemoteError,
)

CLIP_ID = "00000000-0000-0000-0000-000000000123"


class FakeTransport:
    def __init__(self, responder: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        self.responder = responder
        self.requests: list[dict[str, Any]] = []

    def exchange(self, request: bytes, *, timeout_s: float, max_response_bytes: int) -> bytes:
        assert len(request) <= MAX_REQUEST_BYTES
        assert timeout_s == 5
        assert max_response_bytes == MAX_RESPONSE_BYTES
        decoded = json.loads(request)
        self.requests.append(decoded)
        return json.dumps(self.responder(decoded)).encode()


def _success(request: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": 1,
        "request_id": request["request_id"],
        "ok": True,
        "result": result,
    }


def test_client_sends_only_versioned_allow_listed_commands() -> None:
    transport = FakeTransport(lambda request: _success(request, {"state": "RECORDING"}))
    client = RecorderClient(transport)

    assert client.call(RecorderCommand.STATUS) == {"state": "RECORDING"}
    assert transport.requests[0]["command"] == "status"
    assert transport.requests[0]["arguments"] == {}

    with pytest.raises(RecorderProtocolError):
        client.call("shell", {})  # type: ignore[arg-type]


@pytest.mark.parametrize("clip_id", ["../etc/passwd", "not-a-uuid", f"{CLIP_ID}/x"])
def test_clip_commands_reject_paths_before_transport(clip_id: str) -> None:
    transport = FakeTransport(lambda request: _success(request, {}))
    client = RecorderClient(transport)

    with pytest.raises(ValueError):
        client.call_for_clip(RecorderCommand.GET_CLIP, clip_id)
    assert transport.requests == []


def test_download_approval_is_tied_to_requested_clip() -> None:
    def response(request: dict[str, Any]) -> dict[str, Any]:
        return _success(
            request,
            {
                "clip_id": CLIP_ID,
                "lease_id": "safe_lease_identifier",
                "approved_path": "/srv/dashcam/clips/owned.mp4",
                "expires_at_monotonic_ns": 123,
            },
        )

    transport = FakeTransport(response)
    approval = RecorderClient(transport).acquire_download(
        CLIP_ID, member="video", holder="web-session"
    )

    assert approval.clip_id.hex.endswith("0123")
    assert approval.approved_path == PurePosixPath("/srv/dashcam/clips/owned.mp4")
    assert transport.requests[0]["arguments"]["clip_id"] == CLIP_ID
    assert "path" not in transport.requests[0]["arguments"]


def test_download_rejects_member_and_mismatched_identity() -> None:
    transport = FakeTransport(
        lambda request: _success(
            request,
            {
                "clip_id": "00000000-0000-0000-0000-000000000124",
                "lease_id": "safe_lease_identifier",
                "approved_path": "/srv/dashcam/clips/owned.mp4",
                "expires_at_monotonic_ns": 123,
            },
        )
    )
    client = RecorderClient(transport)

    with pytest.raises(RecorderProtocolError):
        client.acquire_download(CLIP_ID, member="other", holder="web-session")
    with pytest.raises(RecorderProtocolError):
        client.acquire_download(CLIP_ID, member="video", holder="web-session")


def test_remote_errors_are_closed_and_typed() -> None:
    transport = FakeTransport(
        lambda request: {
            "version": 1,
            "request_id": request["request_id"],
            "ok": False,
            "error": {
                "code": "CLIP_BUSY",
                "message": "Clip is leased",
                "retryable": True,
            },
        }
    )

    with pytest.raises(RecorderRemoteError) as captured:
        RecorderClient(transport).call(RecorderCommand.DELETE_CLIP)
    assert captured.value.code is ErrorCode.CLIP_BUSY
    assert captured.value.retryable


@pytest.mark.parametrize(
    "mutation",
    [
        lambda response: response.update(version=2),
        lambda response: response.update(request_id="wrong"),
        lambda response: response.update(extra=True),
        lambda response: response.update(result=[]),
    ],
)
def test_client_rejects_malformed_or_noncorrelated_responses(
    mutation: Callable[[dict[str, Any]], None],
) -> None:
    def responder(request: dict[str, Any]) -> dict[str, Any]:
        response = _success(request, {})
        mutation(response)
        return response

    with pytest.raises(RecorderProtocolError):
        RecorderClient(FakeTransport(responder)).call(RecorderCommand.STATUS)


def test_client_rejects_oversized_or_deep_requests_before_transport() -> None:
    transport = FakeTransport(lambda request: _success(request, {}))
    client = RecorderClient(transport)
    with pytest.raises(RecorderProtocolError):
        client.call(RecorderCommand.UPDATE_CONFIG, {"text": "x" * MAX_REQUEST_BYTES})

    nested: dict[str, object] = {}
    cursor = nested
    for _ in range(20):
        child: dict[str, object] = {}
        cursor["child"] = child
        cursor = child
    with pytest.raises(RecorderProtocolError):
        client.call(RecorderCommand.UPDATE_CONFIG, nested)
