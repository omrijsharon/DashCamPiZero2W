from __future__ import annotations

from collections.abc import Iterable, Mapping
from io import BytesIO
from pathlib import Path, PurePosixPath
from uuid import UUID

import pytest

from dashcam_web.application import MAX_HTTP_BODY_BYTES, Request, Response
from dashcam_web.recorder_client import ApprovedDownload, RecorderCommand
from dashcam_web.wsgi import (
    DOWNLOAD_CHUNK_BYTES,
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


class BrokenStream:
    def __init__(self) -> None:
        self.closed = False

    def read(self, _size: int = -1) -> bytes:
        raise OSError("injected read failure")

    def close(self) -> None:
        self.closed = True


def _approval(path: str) -> ApprovedDownload:
    return ApprovedDownload(CLIP_ID, "bounded_lease_identifier", PurePosixPath(path), 100)


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


def _download_response(path: str, content_type: str = "video/mp4") -> Response:
    return Response(200, None, {"Content-Type": content_type}, download=_approval(path))


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


def test_approved_video_streams_in_bounded_chunks_and_releases_on_normal_completion(
    tmp_path: Path,
) -> None:
    payload = b"x" * (2 * DOWNLOAD_CHUNK_BYTES + 1)
    clips = tmp_path / "clips"
    clips.mkdir()
    (clips / "clip.mp4").write_bytes(payload)
    recorder = FakeRecorder()
    adapter = WsgiAdapter(
        FakeApplication(_download_response("/srv/dashcam/clips/clip.mp4")),
        recorder,
        bind_address="192.168.50.1",
        recording_root=tmp_path,
    )

    status, _, iterable = _invoke(adapter, _environ())
    chunks = list(iterable)

    assert status == "200 OK"
    assert all(len(chunk) <= DOWNLOAD_CHUNK_BYTES for chunk in chunks)
    assert b"".join(chunks) == payload
    assert _release_calls(recorder) == [
        {"clip_id": str(CLIP_ID), "lease_id": "bounded_lease_identifier"}
    ]


def test_download_release_occurs_when_wsgi_server_closes_early(tmp_path: Path) -> None:
    clips = tmp_path / "clips"
    clips.mkdir()
    (clips / "clip.mp4").write_bytes(b"x" * (DOWNLOAD_CHUNK_BYTES + 1))
    recorder = FakeRecorder()
    adapter = WsgiAdapter(
        FakeApplication(_download_response("/srv/dashcam/clips/clip.mp4")),
        recorder,
        bind_address="127.0.0.1",
        recording_root=tmp_path,
    )

    _, _, iterable = _invoke(adapter, _environ())
    iterator = iter(iterable)
    assert next(iterator) == b"x" * DOWNLOAD_CHUNK_BYTES
    assert hasattr(iterable, "close")
    iterable.close()

    assert len(_release_calls(recorder)) == 1


def test_download_read_error_closes_and_releases_lease(tmp_path: Path) -> None:
    clips = tmp_path / "clips"
    clips.mkdir()
    (clips / "clip.mp4").write_bytes(b"valid fixture")
    recorder = FakeRecorder()
    broken = BrokenStream()
    adapter = WsgiAdapter(
        FakeApplication(_download_response("/srv/dashcam/clips/clip.mp4")),
        recorder,
        bind_address="127.0.0.1",
        recording_root=tmp_path,
        stream_opener=lambda _path: broken,
    )

    _, _, iterable = _invoke(adapter, _environ())
    with pytest.raises(OSError, match="injected"):
        next(iter(iterable))

    assert broken.closed
    assert len(_release_calls(recorder)) == 1


@pytest.mark.parametrize(
    "approved_path",
    [
        "/srv/dashcam/clips/../protected/clip.mp4",
        "/srv/dashcam/pending/clip.mp4",
        "/srv/dashcam/clips/clip.json",
        "/srv/dashcam/clips/nested/clip.mp4",
        "/etc/passwd.mp4",
    ],
)
def test_invalid_or_traversal_like_approved_paths_are_rejected_and_released(
    tmp_path: Path, approved_path: str
) -> None:
    recorder = FakeRecorder()
    adapter = WsgiAdapter(
        FakeApplication(_download_response(approved_path)),
        recorder,
        bind_address="127.0.0.1",
        recording_root=tmp_path,
    )

    status, _, iterable = _invoke(adapter, _environ())

    assert status == "404 Not Found"
    assert b"".join(iterable)
    assert len(_release_calls(recorder)) == 1


def test_symlink_marker_and_nonregular_approved_files_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clips = tmp_path / "clips"
    clips.mkdir()
    linked = clips / "linked.mp4"
    linked.write_bytes(b"fixture")
    directory = clips / "directory.mp4"
    directory.mkdir()

    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path.name == "linked.mp4" or original_is_symlink(path),
    )
    linked_adapter = WsgiAdapter(
        FakeApplication(_download_response("/srv/dashcam/clips/linked.mp4")),
        FakeRecorder(),
        bind_address="127.0.0.1",
        recording_root=tmp_path,
    )
    status, _, _ = _invoke(linked_adapter, _environ())
    assert status == "404 Not Found"

    regular_adapter = WsgiAdapter(
        FakeApplication(_download_response("/srv/dashcam/clips/directory.mp4")),
        FakeRecorder(),
        bind_address="127.0.0.1",
        recording_root=tmp_path,
    )
    status, _, _ = _invoke(regular_adapter, _environ())
    assert status == "404 Not Found"


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
