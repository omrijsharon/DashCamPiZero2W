from __future__ import annotations

import json
from pathlib import Path

import pytest

from dashcam.recorder.metrics import RuntimeSnapshotPublisher, SnapshotError
from dashcam.recorder.status import RecorderStatus
from dashcam.state import RecorderState


class Runtime:
    def runtime_snapshot(self) -> dict[str, object]:
        return {
            "video": {"width": 1920, "height": 1080},
            "frames": {"encoded": None, "written": None, "dropped": None},
        }


def test_snapshot_is_canonical_bounded_and_atomically_replaced(tmp_path: Path) -> None:
    path = tmp_path / "run/status.json"
    publisher = RuntimeSnapshotPublisher(path)
    publisher.publish(RecorderStatus(RecorderState.STARTING, 0), Runtime())

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "lifecycle": {
            "config_schema_version": None,
            "detail": None,
            "notification_failures": 0,
            "reason": None,
            "sequence": 0,
            "state": "STARTING",
        },
        "runtime": Runtime().runtime_snapshot(),
        "schema_version": 2,
    }


def test_snapshot_refuses_unsafe_path_and_non_json_runtime(tmp_path: Path) -> None:
    with pytest.raises(SnapshotError, match="absolute"):
        RuntimeSnapshotPublisher(Path("status.json"))

    class BadRuntime:
        def runtime_snapshot(self) -> object:
            return {"bad": object()}

    publisher = RuntimeSnapshotPublisher(tmp_path / "status.json")
    with pytest.raises(SnapshotError, match="JSON-compatible"):
        publisher.publish(RecorderStatus(RecorderState.STARTING, 0), BadRuntime())
