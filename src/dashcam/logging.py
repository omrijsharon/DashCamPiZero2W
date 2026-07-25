"""Bounded structured logging with conservative secret redaction."""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from itertools import islice
from typing import Final, TextIO, TypeAlias

_REDACTED: Final = "[REDACTED]"
_MAX_DEPTH: Final = 4
_MAX_MAPPING_ITEMS: Final = 64
_MAX_SEQUENCE_ITEMS: Final = 32
_MAX_STRING_CHARS: Final = 1024
_MAX_RECORD_CHARS: Final = 8192
_SENSITIVE_MARKERS: Final = (
    "authorization",
    "cookie",
    "credential",
    "password",
    "passphrase",
    "secret",
    "session",
    "token",
)
_SECRET_ASSIGNMENT: Final = re.compile(
    r"(?i)\b(password|passphrase|secret|token|authorization|cookie)"
    r"(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)

JsonValue: TypeAlias = bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"] | None


def _bounded_string(value: str) -> str:
    if len(value) <= _MAX_STRING_CHARS:
        return value
    return f"{value[:_MAX_STRING_CHARS]}…[truncated:{len(value) - _MAX_STRING_CHARS}]"


def _redact_message(message: str) -> str:
    redacted = _SECRET_ASSIGNMENT.sub(rf"\1\2{_REDACTED}", message)
    return _bounded_string(redacted)


def _is_sensitive_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    return any(marker in normalized for marker in _SENSITIVE_MARKERS)


def _sanitize(value: object, *, key: str | None = None, depth: int = 0) -> JsonValue:
    if key is not None and _is_sensitive_key(key):
        return _REDACTED
    if depth >= _MAX_DEPTH:
        return f"<{type(value).__name__}:max-depth>"
    if value is None or isinstance(value, (bool, int, str)):
        return _bounded_string(value) if isinstance(value, str) else value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    if isinstance(value, Mapping):
        sanitized: dict[str, JsonValue] = {}
        items = list(islice(value.items(), _MAX_MAPPING_ITEMS))
        for raw_key, item in items[:_MAX_MAPPING_ITEMS]:
            item_key = _bounded_string(str(raw_key))
            sanitized[item_key] = _sanitize(item, key=item_key, depth=depth + 1)
        if len(value) > _MAX_MAPPING_ITEMS:
            sanitized["_truncated_items"] = len(value) - _MAX_MAPPING_ITEMS
        return sanitized
    if isinstance(value, Sequence):
        items = list(islice(value, _MAX_SEQUENCE_ITEMS))
        sanitized_items = [_sanitize(item, depth=depth + 1) for item in items]
        if len(value) > _MAX_SEQUENCE_ITEMS:
            sanitized_items.append(f"<truncated:{len(value) - _MAX_SEQUENCE_ITEMS}>")
        return sanitized_items
    return f"<{type(value).__name__}>"


class BoundedJsonFormatter(logging.Formatter):
    """Emit one bounded JSON object per log record.

    Callers place structured fields in ``extra={"event_data": {...}}``. Arbitrary
    ``LogRecord`` extras are intentionally ignored so secrets cannot leak implicitly.
    """

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=UTC).isoformat(
            timespec="milliseconds"
        )
        payload: dict[str, JsonValue] = {
            "timestamp": timestamp.replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": _bounded_string(record.name),
            "message": _redact_message(record.getMessage()),
        }

        for field in ("component", "boot_id", "clip_id"):
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = _sanitize(value, key=field)

        event_data = getattr(record, "event_data", None)
        if event_data is not None:
            payload["event_data"] = _sanitize(event_data)

        if record.exc_info is not None and record.exc_info[0] is not None:
            payload["exception_type"] = record.exc_info[0].__name__

        encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        if len(encoded) <= _MAX_RECORD_CHARS:
            return encoded

        payload["event_data"] = {"_truncated": True}
        payload["message"] = _bounded_string(str(payload["message"]))
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def create_json_handler(stream: TextIO | None = None) -> logging.StreamHandler[TextIO]:
    """Create a handler without mutating global logging configuration."""

    handler = logging.StreamHandler(stream)
    handler.setFormatter(BoundedJsonFormatter())
    return handler
