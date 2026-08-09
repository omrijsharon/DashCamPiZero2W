from __future__ import annotations

from collections.abc import Iterable, Mapping
from io import BytesIO
from pathlib import Path
from uuid import UUID

import pytest

from dashcam_web.application import MAX_HTTP_BODY_BYTES, Request, Response
from dashcam_web.recorder_client import ApprovedDownload, RecorderCommand
from dashcam_web.wsgi import (
    WsgiAdapter,
    WsgiAdapterError,
    validate_bind_address,
)

CLIP_ID = UUID("00000000-0000-0000-0000-000000000123")


class FakeApplication:
    def __init__(self, response: Response) -> None:
        self.response = response
        self.requests: list[Request] = []

    def handle(self, request: Request) -> Response:
        self.requests.append(request)
        return self.response


class FakeRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[RecorderCommand, dict[str, object]]] = []

    def call(
        self, command: RecorderCommand, arguments: Mapping[str, object] | None = None
    ) -> dict[str, object]:
        self.calls.append((command, dict(arguments or {})))
        return {}


def _approval() -> ApprovedDownload:
    return ApprovedDownload(CLIP_ID, "bounded_lease_identifier", "video", 100)


def _environ(
    *, path: str = "/api/v1/status", query: str = "", body: bytes = b""
) -> dict[str, object]:
    return {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "REMOTE_ADDR": "192.168.50.22",
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": BytesIO(body),
        "HTTP_AUTHORIZATION": "Bearer token",
    }


def _invoke(
    adapter: WsgiAdapter, environ: dict[str, object]
) -> tuple[str, list[tuple[str, str]], Iterable[bytes]]:
    status = ""
    headers: list[tuple[str, str]] = []

    def start_response(response_status: str, response_headers: list[tuple[str, str]]) -> None:
        nonlocal status, headers
        status = response_status
        headers = response_headers

    iterable = adapter(environ, start_response)
    assert status
    return status, headers, iterable


def _download_response(content_type: str = "video/mp4") -> Response:
    return Response(200, None, {"Content-Type": content_type}, download=_approval())


def _release_calls(recorder: FakeRecorder) -> list[dict[str, object]]:
    return [
        arguments
        for command, arguments in recorder.calls
        if command is RecorderCommand.RELEASE_DOWNLOAD
    ]


def test_wsgi_request_is_bounded_converted_and_json_response_is_serialized(tmp_path: Path) -> None:
    application = FakeApplication(Response(200, {"status": "ok"}, {"Cache-Control": "no-store"}))
    recorder = FakeRecorder()
    adapter = WsgiAdapter(
        application,
        recorder,
        bind_address="192.168.50.1",
        recording_root=tmp_path,
    )

    status, headers, iterable = _invoke(adapter, _environ(query="limit=20&offset=4"))

    assert b"".join(iterable) == b'{"status":"ok"}'
    assert status == "200 OK"
    assert ("Content-Length", "15") in headers
    request = application.requests[-1]
    assert request.query == {"limit": "20", "offset": "4"}
    assert request.headers == {"AUTHORIZATION": "Bearer token"}
    assert request.client_key == "192.168.50.22"


def test_wsgi_input_limits_and_duplicate_query_are_rejected_before_application(
    tmp_path: Path,
) -> None:
    application = FakeApplication(Response(200, {}, {}))
    adapter = WsgiAdapter(
        application,
        FakeRecorder(),
        bind_address="127.0.0.1",
        recording_root=tmp_path,
    )

    too_large = _environ()
    too_large["CONTENT_LENGTH"] = str(MAX_HTTP_BODY_BYTES + 1)
    status, _, iterable = _invoke(adapter, too_large)
    assert status == "400 Bad Request"
    assert b"".join(iterable)

    status, _, _ = _invoke(adapter, _environ(query="limit=1&limit=2"))
    assert status == "400 Bad Request"
    assert application.requests == []


def test_wsgi_download_fallback_is_stably_unavailable_without_opening_files(
    tmp_path: Path,
) -> None:
    recorder = FakeRecorder()
    opened: list[Path] = []
    adapter = WsgiAdapter(
        FakeApplication(_download_response()),
        recorder,
        bind_address="127.0.0.1",
        recording_root=tmp_path,
        stream_opener=lambda path: opened.append(path),  # type: ignore[arg-type,return-value]
    )

    status, _, iterable = _invoke(adapter, _environ())

    assert status == "501 Not Implemented"
    assert b"Download delivery is not available" in b"".join(iterable)
    assert opened == []
    assert _release_calls(recorder) == [
        {"clip_id": str(CLIP_ID), "lease_id": "bounded_lease_identifier"}
    ]


@pytest.mark.parametrize("address", ["127.0.0.1", "192.168.50.1", "::1", "fe80::1"])
def test_explicit_local_bind_addresses_are_accepted(address: str) -> None:
    assert validate_bind_address(address)


@pytest.mark.parametrize(
    "address",
    ["", "0.0.0.0", "::", "8.8.8.8", "192.0.2.1", "dashcam.local"],
)
def test_wildcard_public_and_hostname_binds_are_rejected(address: str) -> None:
    with pytest.raises(WsgiAdapterError):
        validate_bind_address(address)
