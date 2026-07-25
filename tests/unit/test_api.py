from __future__ import annotations

from uuid import UUID

import pytest

from dashcam.control.api import (
    API_PREFIX,
    ENDPOINT_BY_METHOD_PATH,
    PUBLIC_ENDPOINTS,
    AuthPolicy,
    ErrorCode,
    ErrorResponse,
    HttpMethod,
    PageRequest,
    RedactedSecret,
    parse_clip_id,
)


def test_public_contract_contains_every_required_endpoint_once() -> None:
    required = {
        ("GET", "/status"),
        ("GET", "/config"),
        ("PUT", "/config"),
        ("GET", "/clips"),
        ("GET", "/clips/{clip_id}"),
        ("GET", "/clips/{clip_id}/video"),
        ("GET", "/clips/{clip_id}/metadata"),
        ("POST", "/clips/{clip_id}/protect"),
        ("POST", "/clips/{clip_id}/unprotect"),
        ("DELETE", "/clips/{clip_id}"),
        ("POST", "/event"),
        ("POST", "/recorder/restart"),
        ("POST", "/system/prepare-sd-removal"),
        ("GET", "/health"),
    }

    actual = {
        (endpoint.method.value, endpoint.path.removeprefix(API_PREFIX))
        for endpoint in PUBLIC_ENDPOINTS
    }
    assert actual == required
    assert len(ENDPOINT_BY_METHOD_PATH) == len(PUBLIC_ENDPOINTS)


def test_every_state_change_requires_session_and_csrf() -> None:
    mutations = [endpoint for endpoint in PUBLIC_ENDPOINTS if endpoint.state_changing]

    assert mutations
    assert all(endpoint.method is not HttpMethod.GET for endpoint in mutations)
    assert all(endpoint.csrf_required for endpoint in mutations)
    assert all(
        endpoint.auth in {AuthPolicy.SESSION, AuthPolicy.REAUTHENTICATE} for endpoint in mutations
    )


def test_prepare_removal_requires_reauthentication() -> None:
    endpoint = ENDPOINT_BY_METHOD_PATH[(HttpMethod.POST, f"{API_PREFIX}/system/prepare-sd-removal")]

    assert endpoint.auth is AuthPolicy.REAUTHENTICATE


def test_error_response_is_closed_and_bounded() -> None:
    response = ErrorResponse(
        ErrorCode.INVALID_REQUEST,
        "Invalid bitrate",
        field="video.bitrate_bps",
    )

    assert response.as_dict() == {
        "error": {
            "code": "INVALID_REQUEST",
            "message": "Invalid bitrate",
            "retryable": False,
            "field": "video.bitrate_bps",
        }
    }

    with pytest.raises(ValueError):
        ErrorResponse(ErrorCode.INTERNAL_ERROR, "x" * 513)


def test_secret_representation_never_contains_secret_material() -> None:
    representation = RedactedSecret(is_set=True).as_dict()

    assert representation == {"is_set": True}
    assert set(representation) == {"is_set"}


@pytest.mark.parametrize("raw", ["../clip", "not-a-uuid", "A" * 36, "uuid/child"])
def test_clip_id_parser_rejects_user_paths_and_noncanonical_values(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_clip_id(raw)


def test_clip_id_parser_accepts_canonical_uuid_only() -> None:
    raw = "00000000-0000-0000-0000-000000000123"

    assert parse_clip_id(raw) == UUID(raw)


@pytest.mark.parametrize(
    ("limit", "offset"),
    [(0, 0), (201, 0), (True, 0), (50, -1), (50, 1_000_001)],
)
def test_pagination_is_bounded(limit: int, offset: int) -> None:
    with pytest.raises(ValueError):
        PageRequest(limit=limit, offset=offset)
