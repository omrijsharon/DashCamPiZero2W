from __future__ import annotations

import json
from collections.abc import Mapping
from itertools import count
from typing import Any

import pytest

from dashcam.control.api import ErrorCode
from dashcam_web.application import REAUTHENTICATE_PATH, SESSION_PATH, Request, WebApplication
from dashcam_web.recorder_client import RecorderClient, RecorderCommand, RecorderRemoteError
from dashcam_web.security import SessionStore, create_password_record

PASSWORD = "a unique local password"
CLIP_ID = "00000000-0000-0000-0000-000000000123"


class BackendTransport:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.error: RecorderRemoteError | None = None

    def exchange(self, request: bytes, *, timeout_s: float, max_response_bytes: int) -> bytes:
        decoded = json.loads(request)
        self.requests.append(decoded)
        if self.error is not None:
            error = self.error
            response = {
                "version": 1,
                "request_id": decoded["request_id"],
                "ok": False,
                "error": {
                    "code": error.code.value,
                    "message": str(error),
                    "retryable": error.retryable,
                },
            }
        else:
            command = decoded["command"]
            result: dict[str, object] = {"command": command}
            if command == "get_config":
                result["password"] = "must-not-leak"
                result["ap_passphrase"] = {"is_set": True}
            if command == "acquire_download":
                result = {
                    "clip_id": decoded["arguments"]["clip_id"],
                    "lease_id": "bounded_lease_identifier",
                    "member": decoded["arguments"]["member"],
                    "expires_at_monotonic_ns": 100,
                }
            response = {
                "version": 1,
                "request_id": decoded["request_id"],
                "ok": True,
                "result": result,
            }
        return json.dumps(response).encode()


@pytest.fixture
def web() -> tuple[WebApplication, BackendTransport, list[float]]:
    transport = BackendTransport()
    now = [10.0]
    tokens = count()
    application = WebApplication(
        recorder=RecorderClient(transport),
        sessions=SessionStore(token_factory=lambda _size: f"{next(tokens):043d}"),
        password_record=create_password_record(PASSWORD, random_bytes=lambda size: b"s" * size),
        clock=lambda: now[0],
    )
    return application, transport, now


def _request(
    method: str,
    path: str,
    *,
    token: str = "",
    csrf: str = "",
    body: Mapping[str, object] | None = None,
    query: Mapping[str, str] | None = None,
) -> Request:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if csrf:
        headers["X-CSRF-Token"] = csrf
    return Request(
        method,
        path,
        headers,
        json.dumps(body).encode() if body is not None else b"",
        query,
    )


def _login(web: WebApplication) -> tuple[str, str]:
    response = web.handle(_request("POST", SESSION_PATH, body={"password": PASSWORD}))
    assert response.status == 201
    assert isinstance(response.body, dict)
    return str(response.body["session_token"]), str(response.body["csrf_token"])


def test_login_required_and_bad_credentials_are_generic(
    web: tuple[WebApplication, BackendTransport, list[float]],
) -> None:
    application, _, _ = web
    assert application.handle(_request("GET", "/api/v1/status")).status == 401
    failure = application.handle(
        _request("POST", SESSION_PATH, body={"password": "wrong password value"})
    )
    assert failure.status == 401
    assert "wrong password value" not in json.dumps(failure.body)


def test_password_verification_has_a_separate_low_rate_limit(
    web: tuple[WebApplication, BackendTransport, list[float]],
) -> None:
    application, _, _ = web
    for _ in range(5):
        response = application.handle(
            _request("POST", SESSION_PATH, body={"password": "wrong password value"})
        )
        assert response.status == 401
    limited = application.handle(
        _request("POST", SESSION_PATH, body={"password": "wrong password value"})
    )
    assert limited.status == 429


def test_read_dispatch_and_defense_in_depth_secret_redaction(
    web: tuple[WebApplication, BackendTransport, list[float]],
) -> None:
    application, transport, _ = web
    token, _ = _login(application)
    response = application.handle(_request("GET", "/api/v1/config", token=token))

    assert response.status == 200
    assert response.body == {
        "command": "get_config",
        "password": {"is_set": True},
        "ap_passphrase": {"is_set": True},
    }
    assert transport.requests[-1]["command"] == RecorderCommand.GET_CONFIG.value


def test_mutation_requires_csrf_and_prepare_removal_requires_reauth_and_phrase(
    web: tuple[WebApplication, BackendTransport, list[float]],
) -> None:
    application, transport, now = web
    token, csrf = _login(application)
    now[0] = 400
    path = "/api/v1/system/prepare-sd-removal"
    assert application.handle(_request("POST", path, token=token)).status == 403
    assert application.handle(_request("POST", path, token=token, csrf=csrf)).status == 401

    reauth = application.handle(
        _request(
            "POST",
            REAUTHENTICATE_PATH,
            token=token,
            csrf=csrf,
            body={"password": PASSWORD},
        )
    )
    assert reauth.status == 204
    assert (
        application.handle(
            _request("POST", path, token=token, csrf=csrf, body={"confirmation": "yes"})
        ).status
        == 400
    )
    success = application.handle(
        _request(
            "POST",
            path,
            token=token,
            csrf=csrf,
            body={"confirmation": "PREPARE SD CARD"},
        )
    )
    assert success.status == 200
    assert transport.requests[-1]["command"] == RecorderCommand.PREPARE_REMOVAL.value


def test_download_route_is_stably_unavailable_without_lease_or_filesystem_access(
    web: tuple[WebApplication, BackendTransport, list[float]],
) -> None:
    application, transport, _ = web
    token, _ = _login(application)
    bad = application.handle(_request("GET", "/api/v1/clips/../../etc/passwd", token=token))
    assert bad.status == 404
    requests_before = len(transport.requests)
    response = application.handle(_request("GET", f"/api/v1/clips/{CLIP_ID}/video", token=token))
    assert response.status == 501
    assert response.download is None
    assert response.body == {
        "error": {
            "code": "UNSUPPORTED_CONFIGURATION",
            "message": "Download delivery is not available in this release",
            "retryable": False,
        }
    }
    assert len(transport.requests) == requests_before


def test_event_passes_one_canonical_id_and_preserves_caller_retry_identity(
    web: tuple[WebApplication, BackendTransport, list[float]],
) -> None:
    application, transport, _ = web
    token, csrf = _login(application)
    event_id = "00000000-0000-0000-0000-000000000777"

    first = application.handle(
        _request(
            "POST",
            "/api/v1/event",
            token=token,
            csrf=csrf,
            body={"source": "web", "event_id": event_id},
        )
    )
    second = application.handle(
        _request(
            "POST",
            "/api/v1/event",
            token=token,
            csrf=csrf,
            body={"source": "web", "event_id": event_id},
        )
    )

    assert first.status == second.status == 200
    assert [request["arguments"] for request in transport.requests[-2:]] == [
        {"source": "web", "event_id": event_id},
        {"source": "web", "event_id": event_id},
    ]


def test_clip_list_query_is_strict_and_bounded(
    web: tuple[WebApplication, BackendTransport, list[float]],
) -> None:
    application, transport, _ = web
    token, _ = _login(application)
    response = application.handle(
        _request(
            "GET",
            "/api/v1/clips",
            token=token,
            query={"limit": "20", "offset": "4", "protected": "true"},
        )
    )
    assert response.status == 200
    assert transport.requests[-1]["arguments"] == {
        "limit": 20,
        "offset": 4,
        "protected": "true",
    }
    assert (
        application.handle(
            _request("GET", "/api/v1/clips", token=token, query={"limit": "201"})
        ).status
        == 400
    )


def test_remote_error_is_structured_without_traceback(
    web: tuple[WebApplication, BackendTransport, list[float]],
) -> None:
    application, transport, _ = web
    token, _ = _login(application)
    transport.error = RecorderRemoteError(ErrorCode.CLIP_BUSY, "Clip busy", retryable=True)
    response = application.handle(_request("GET", f"/api/v1/clips/{CLIP_ID}", token=token))

    assert response.status == 409
    assert response.body == {
        "error": {"code": "CLIP_BUSY", "message": "Clip busy", "retryable": True}
    }


def test_logout_requires_csrf_and_revokes_session(
    web: tuple[WebApplication, BackendTransport, list[float]],
) -> None:
    application, _, _ = web
    token, csrf = _login(application)
    assert application.handle(_request("DELETE", SESSION_PATH, token=token)).status == 403
    assert (
        application.handle(_request("DELETE", SESSION_PATH, token=token, csrf=csrf)).status == 204
    )
    assert application.handle(_request("GET", "/api/v1/status", token=token)).status == 401
