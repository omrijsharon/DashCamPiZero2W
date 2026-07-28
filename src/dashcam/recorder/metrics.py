"""Bounded atomic recorder runtime snapshot publication."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Final

from dashcam.recorder.status import RecorderStatus

RUNTIME_SNAPSHOT_SCHEMA_VERSION: Final = 2
DEFAULT_STATUS_PATH: Final = Path("/run/dashcam/status.json")
MAX_SNAPSHOT_BYTES: Final = 32 * 1024
MAX_SNAPSHOT_DEPTH: Final = 8


class SnapshotError(ValueError):
    """A runtime snapshot is not bounded JSON-compatible data."""


def _validate(value: object, depth: int = 0) -> None:
    if depth > MAX_SNAPSHOT_DEPTH:
        raise SnapshotError("snapshot nesting exceeds its bound")
    if value is None or isinstance(value, str | int | float):
        if isinstance(value, float) and (value != value or value in {float("inf"), float("-inf")}):
            raise SnapshotError("snapshot float is non-finite")
        return
    if isinstance(value, list):
        if len(value) > 128:
            raise SnapshotError("snapshot list exceeds its bound")
        for item in value:
            _validate(item, depth + 1)
        return
    if isinstance(value, dict) and len(value) <= 64 and all(isinstance(key, str) for key in value):
        for item in value.values():
            _validate(item, depth + 1)
        return
    raise SnapshotError("snapshot contains a non-JSON-compatible value")


class RuntimeSnapshotPublisher:
    """Best-effort publisher: callers must isolate its failures from recording."""

    def __init__(self, path: Path = DEFAULT_STATUS_PATH) -> None:
        absolute = path.is_absolute() or path.as_posix().startswith("/")
        if not absolute or path.name != "status.json":
            raise SnapshotError("snapshot path must be an absolute status.json path")
        self._path = path

    def publish(self, status: RecorderStatus, runtime: object) -> None:
        runtime_snapshot = getattr(runtime, "runtime_snapshot", None)
        observed = runtime_snapshot() if callable(runtime_snapshot) else None
        if observed is not None and not isinstance(observed, dict):
            raise SnapshotError("runtime snapshot must be one object or null")
        payload: dict[str, object] = {
            "schema_version": RUNTIME_SNAPSHOT_SCHEMA_VERSION,
            "lifecycle": status.as_dict(),
            "runtime": observed,
        }
        _validate(payload)
        encoded = (
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        if len(encoded) > MAX_SNAPSHOT_BYTES:
            raise SnapshotError("snapshot exceeds its byte bound")
        self._path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".status-", dir=self._path.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o640)
            os.replace(temporary, self._path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            Path(temporary).unlink(missing_ok=True)
