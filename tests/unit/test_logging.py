from __future__ import annotations

import io
import json
import logging

from dashcam.logging import create_json_handler


def _logger_with_stream(stream: io.StringIO) -> logging.Logger:
    logger = logging.getLogger("dashcam.test.structured")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(create_json_handler(stream))
    return logger


def test_structured_log_is_valid_json_and_redacts_secrets() -> None:
    stream = io.StringIO()
    logger = _logger_with_stream(stream)

    logger.info(
        "login password=hunter2",
        extra={
            "component": "web",
            "event_data": {
                "username": "driver",
                "nested": {"api_token": "secret-token", "safe": "visible"},
            },
        },
    )

    payload = json.loads(stream.getvalue())
    assert payload["timestamp"].endswith("Z")
    assert payload["component"] == "web"
    assert payload["message"] == "login password=[REDACTED]"
    assert payload["event_data"]["nested"]["api_token"] == "[REDACTED]"
    assert payload["event_data"]["nested"]["safe"] == "visible"
    assert "hunter2" not in stream.getvalue()
    assert "secret-token" not in stream.getvalue()


def test_structured_log_bounds_sequences_and_unknown_objects() -> None:
    stream = io.StringIO()
    logger = _logger_with_stream(stream)

    logger.info(
        "bounded",
        extra={
            "event_data": {
                "values": list(range(100)),
                "object": object(),
                "not_finite": float("nan"),
            }
        },
    )

    payload = json.loads(stream.getvalue())
    assert len(payload["event_data"]["values"]) == 33
    assert payload["event_data"]["values"][-1] == "<truncated:68>"
    assert payload["event_data"]["object"] == "<object>"
    assert payload["event_data"]["not_finite"] is None
