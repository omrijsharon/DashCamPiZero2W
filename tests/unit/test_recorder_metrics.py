from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from dashcam.overlay.native_nv12 import NativeNv12OverlayCore
from dashcam.recorder.metrics import RuntimeSnapshotPublisher, SnapshotError
from dashcam.recorder.status import RecorderStatus
from dashcam.state import RecorderState


class Runtime:
    def runtime_snapshot(self) -> dict[str, object]:
        return {
            "video": {"width": 1920, "height": 1080},
            "frames": {"encoded": None, "written": None, "dropped": None},
        }


class NativeOverlayRuntime:
    """Use the production native-renderer snapshot shape at the JSON boundary."""

    def runtime_snapshot(self) -> dict[str, object]:
        renderer = NativeNv12OverlayCore()
        renderer.set_text("REC")
        return {
            "overlay": {
                "enabled": True,
                "state": "UNCONFIGURED",
                "renderer": asdict(renderer.snapshot()),
            }
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


def test_snapshot_publishes_production_native_overlay_renderer_shape(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    publisher = RuntimeSnapshotPublisher(path)

    publisher.publish(RecorderStatus(RecorderState.RECORDING, 1), NativeOverlayRuntime())

    renderer = json.loads(path.read_text(encoding="utf-8"))["runtime"]["overlay"]["renderer"]
    assert renderer["state"] == "UNCONFIGURED"
    assert renderer["render_latency_bucket_bounds_ns"]
    assert renderer["render_latency_bucket_counts"]
