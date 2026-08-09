"""A bounded stdlib WSGI adapter for the framework-neutral web policy layer.

This module deliberately does not create a socket or start a server.  Its
``bind_address`` validation is a configuration gate for the process manager
that does so.
"""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from http import HTTPStatus
from pathlib import Path
from typing import Final, Protocol, cast
from urllib.parse import parse_qsl

from dashcam_web.application import MAX_HEADER_VALUE_CHARS, MAX_HTTP_BODY_BYTES, Request, Response
from dashcam_web.recorder_client import ApprovedDownload, RecorderCommand

MAX_WSGI_HEADERS: Final = 64
MAX_WSGI_QUERY_PAIRS: Final = 32
MAX_WSGI_METHOD_CHARS: Final = 16
MAX_WSGI_PATH_CHARS: Final = 512
MAX_WSGI_QUERY_CHARS: Final = 2_048
MAX_WSGI_FIELD_CHARS: Final = 128
DOWNLOAD_CHUNK_BYTES: Final = 64 * 1024
_RFC1918_NETWORKS: Final = tuple(
    ipaddress.ip_network(network) for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
_IPV6_UNIQUE_LOCAL: Final = ipaddress.ip_network("fc00::/7")

StartResponse = Callable[[str, list[tuple[str, str]]], object]


class WebHandler(Protocol):
    """The small interface implemented by :class:`WebApplication`."""

    def handle(self, request: Request) -> Response:
        """Return a framework-neutral response."""


class ReleaseClient(Protocol):
    """The recorder operation needed to release a download lease."""

    def call(
        self, command: RecorderCommand, arguments: Mapping[str, object] | None = None
    ) -> dict[str, object]:
        """Call the allow-listed recorder protocol command."""


class WsgiInput(Protocol):
    def read(self, size: int = -1) -> bytes:
        """Read at most the requested number of bytes."""

    def close(self) -> None:
        """Close the request or download stream."""


class WsgiAdapterError(ValueError):
    """Raised for invalid static adapter configuration."""


def validate_bind_address(value: str) -> str:
    """Accept only an explicit loopback or local/AP IP address, never a wildcard."""

    if not isinstance(value, str) or not value or len(value) > 64:
        raise WsgiAdapterError("bind address must be a bounded explicit IP address")
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise WsgiAdapterError("bind address must be an IP address, not a hostname") from error
    if isinstance(address, ipaddress.IPv4Address):
        private_lan = any(address in network for network in _RFC1918_NETWORKS)
    else:
        private_lan = address in _IPV6_UNIQUE_LOCAL
    is_local = address.is_loopback or address.is_link_local or private_lan
    if address.is_unspecified or not is_local:
        raise WsgiAdapterError("bind address must be loopback or a local/AP address")
    return str(address)


class WsgiAdapter:
    """Convert bounded WSGI requests to policy requests and serialize responses."""

    def __init__(
        self,
        application: WebHandler,
        recorder: ReleaseClient,
        *,
        bind_address: str,
        recording_root: Path = Path("/srv/dashcam"),
        stream_opener: Callable[[Path], WsgiInput] | None = None,
    ) -> None:
        if not hasattr(application, "handle") or not hasattr(recorder, "call"):
            raise WsgiAdapterError("application and recorder must provide their bounded interfaces")
        if not isinstance(recording_root, Path) or not recording_root.is_absolute():
            raise WsgiAdapterError("recording_root must be an absolute path")
        self.bind_address = validate_bind_address(bind_address)
        self._application = application
        self._recorder = recorder
        del recording_root, stream_opener

    def __call__(
        self, environ: Mapping[str, object], start_response: StartResponse
    ) -> Iterable[bytes]:
        """Implement the WSGI callable without retaining request bodies or files."""

        try:
            request = _request_from_environ(environ)
        except ValueError:
            return _json_error(start_response, 400, "Invalid request")
        try:
            response = self._application.handle(request)
        except Exception:
            return _json_error(start_response, 500, "Internal server error")
        return self._response_iterable(response, start_response)

    def _response_iterable(
        self, response: Response, start_response: StartResponse
    ) -> Iterable[bytes]:
        if not isinstance(response, Response) or not 100 <= response.status <= 599:
            return _json_error(start_response, 500, "Internal server error")
        if response.download is not None:
            return self._download_iterable(response, start_response)
        try:
            body = b"" if response.body is None else _json_bytes(response.body)
            headers = _response_headers(response.headers, content_length=len(body))
        except (TypeError, ValueError):
            return _json_error(start_response, 500, "Internal server error")
        start_response(_status_line(response.status), headers)
        return (body,)

    def _download_iterable(
        self, response: Response, start_response: StartResponse
    ) -> Iterable[bytes]:
        approval = response.download
        assert approval is not None
        if not isinstance(approval, ApprovedDownload):
            return _json_error(start_response, 500, "Internal server error")
        self._release_download(approval)
        return _json_error(start_response, 501, "Download delivery is not available")

    def _release_download(self, approval: ApprovedDownload) -> None:
        """Best-effort cleanup: release is attempted on every terminal path."""

        with suppress(Exception):
            self._recorder.call(
                RecorderCommand.RELEASE_DOWNLOAD,
                {"clip_id": str(approval.clip_id), "lease_id": approval.lease_id},
            )


class _DownloadIterable:
    """One finite file stream that releases its recorder lease exactly once."""

    def __init__(
        self,
        stream: WsgiInput,
        approval: ApprovedDownload,
        release: Callable[[ApprovedDownload], None],
    ) -> None:
        self._stream = stream
        self._approval = approval
        self._release = release
        self._closed = False

    def __iter__(self) -> _DownloadIterable:
        return self

    def __next__(self) -> bytes:
        if self._closed:
            raise StopIteration
        try:
            chunk = self._stream.read(DOWNLOAD_CHUNK_BYTES)
            if not isinstance(chunk, bytes) or len(chunk) > DOWNLOAD_CHUNK_BYTES:
                raise OSError("download stream returned an invalid chunk")
            if not chunk:
                self.close()
                raise StopIteration
            return chunk
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        """Close file and release the lease; WSGI servers call this on disconnect."""

        if self._closed:
            return
        self._closed = True
        try:
            self._stream.close()
        finally:
            self._release(self._approval)


def _request_from_environ(environ: Mapping[str, object]) -> Request:
    method = _required_str(environ, "REQUEST_METHOD", MAX_WSGI_METHOD_CHARS)
    path = _required_str(environ, "PATH_INFO", MAX_WSGI_PATH_CHARS)
    if not path.startswith("/") or "\x00" in path or not path.isascii():
        raise ValueError("path is invalid")
    query = _parse_query(environ.get("QUERY_STRING", ""))
    headers = _headers_from_environ(environ)
    body = _read_body(environ)
    client_key = environ.get("REMOTE_ADDR", "local")
    if not isinstance(client_key, str) or not client_key or len(client_key) > MAX_WSGI_FIELD_CHARS:
        raise ValueError("client address is invalid")
    return Request(method, path, headers, body, query, client_key)


def _required_str(environ: Mapping[str, object], key: str, limit: int) -> str:
    value = environ.get(key)
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError(f"{key} is invalid")
    return value


def _parse_query(raw_query: object) -> dict[str, str]:
    if not isinstance(raw_query, str) or len(raw_query) > MAX_WSGI_QUERY_CHARS:
        raise ValueError("query is invalid")
    try:
        pairs = parse_qsl(raw_query, keep_blank_values=True, strict_parsing=True, errors="strict")
    except ValueError as error:
        raise ValueError("query is malformed") from error
    if len(pairs) > MAX_WSGI_QUERY_PAIRS:
        raise ValueError("query has too many fields")
    result: dict[str, str] = {}
    for key, value in pairs:
        if (
            not key
            or len(key) > MAX_WSGI_FIELD_CHARS
            or len(value) > MAX_WSGI_FIELD_CHARS
            or key in result
        ):
            raise ValueError("query field is invalid")
        result[key] = value
    return result


def _headers_from_environ(environ: Mapping[str, object]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in environ.items():
        if not isinstance(key, str) or not key.startswith("HTTP_"):
            continue
        header_name = key.removeprefix("HTTP_").replace("_", "-")
        if not header_name or len(header_name) > MAX_WSGI_FIELD_CHARS:
            raise ValueError("header name is invalid")
        if not isinstance(value, str) or len(value) > MAX_HEADER_VALUE_CHARS or "\x00" in value:
            raise ValueError("header value is invalid")
        headers[header_name] = value
    if len(headers) > MAX_WSGI_HEADERS:
        raise ValueError("too many headers")
    return headers


def _read_body(environ: Mapping[str, object]) -> bytes:
    raw_length = environ.get("CONTENT_LENGTH", "")
    if raw_length in {None, ""}:
        return b""
    if not isinstance(raw_length, str) or not raw_length.isascii() or not raw_length.isdecimal():
        raise ValueError("content length is invalid")
    length = int(raw_length)
    if length > MAX_HTTP_BODY_BYTES:
        raise ValueError("request body is too large")
    stream = environ.get("wsgi.input")
    if not hasattr(stream, "read"):
        raise ValueError("WSGI input is missing")
    body = cast(WsgiInput, stream).read(length)
    if not isinstance(body, bytes) or len(body) != length:
        raise ValueError("request body is truncated")
    return body


def _response_headers(
    headers: Mapping[str, str], *, content_length: int | None = None
) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    has_content_length = False
    for key, value in headers.items():
        if (
            not isinstance(key, str)
            or not isinstance(value, str)
            or not key.isascii()
            or not value.isascii()
            or "\r" in key + value
            or "\n" in key + value
        ):
            raise ValueError("response header is invalid")
        has_content_length = has_content_length or key.casefold() == "content-length"
        result.append((key, value))
    if content_length is not None and not has_content_length:
        result.append(("Content-Length", str(content_length)))
    return result


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode(
        "utf-8"
    )


def _json_error(start_response: StartResponse, status: int, message: str) -> Iterable[bytes]:
    body = _json_bytes({"error": {"message": message}})
    start_response(
        _status_line(status),
        [
            ("Content-Type", "application/json"),
            ("Cache-Control", "no-store"),
            ("Content-Length", str(len(body))),
        ],
    )
    return (body,)


def _status_line(status: int) -> str:
    try:
        phrase = HTTPStatus(status).phrase
    except ValueError:
        phrase = "Unknown"
    return f"{status} {phrase}"


__all__ = [
    "DOWNLOAD_CHUNK_BYTES",
    "WsgiAdapter",
    "WsgiAdapterError",
    "validate_bind_address",
]
