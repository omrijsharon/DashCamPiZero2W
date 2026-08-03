from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from dashcam.diagnostics.media import CommandResult

ROOT = Path(__file__).resolve().parents[2]
HARNESS_PATH = (
    ROOT / "deploy/ssh-dev-validation/milestone9-gdkpixbuf-candidate/run.py"
)


def _load() -> ModuleType:
    name = "pi_m9_gdkpixbufoverlay_harness"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, HARNESS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _result(argv: tuple[str, ...], stdout: bytes = b"", *, returncode: int = 0) -> CommandResult:
    return CommandResult(argv, returncode, stdout, b"")


def test_candidate_graph_is_pre_rendered_rgba_before_hardware_encoder(tmp_path: Path) -> None:
    harness = _load()
    description = harness._pipeline_description(
        tmp_path / "candidate-%02d.mp4", tmp_path / "overlay.png"
    )

    assert "gdkpixbufoverlay name=overlay" in description
    assert description.index("gdkpixbufoverlay") < description.index("v4l2h264enc")
    assert "textoverlay" not in description
    assert "format=(string)NV12" in description
    assert "overlay-width=1536 overlay-height=64" in description
    assert "offset-x=40 offset-y=40" in description
    assert "video_bitrate_mode" not in description
    assert "repeat_sequence_header=1" in description


def test_pre_rendered_rgba_png_has_exact_small_region_dimensions(tmp_path: Path) -> None:
    harness = _load()
    path = tmp_path / "overlay.png"
    harness._write_rgba_png(path, accent=(1, 2, 3))
    payload = path.read_bytes()

    assert payload.startswith(b"\x89PNG\r\n\x1a\n")
    length = int.from_bytes(payload[8:12], "big")
    assert payload[12:16] == b"IHDR"
    assert length == 13
    width = int.from_bytes(payload[16:20], "big")
    height = int.from_bytes(payload[20:24], "big")
    assert (width, height, payload[24], payload[25]) == (1536, 64, 8, 6)
    assert len(payload) < 64 * 1024


def test_inactive_gate_refuses_camera_service_competition(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _load()
    monkeypatch.setattr(
        harness,
        "_unit_properties",
        lambda _: {
            "LoadState": "loaded",
            "ActiveState": "active",
            "SubState": "running",
            "MainPID": "42",
            "NRestarts": "0",
            "Result": "success",
        },
    )

    with pytest.raises(harness.HarnessError, match="will not compete"):
        harness._require_inactive("dashcamd.service")


def test_privacy_guard_rejects_text_coordinates_and_raw_nmea() -> None:
    harness = _load()
    harness._assert_privacy_safe({"hash": "a" * 64, "counter": 1})

    for document in (
        {"coordinates": False},
        {"overlay_text": "REC"},
        {"nested": {"longitude": 0}},
        {"detail": "$GNRMC,private"},
    ):
        with pytest.raises(harness.HarnessError, match=r"privacy|raw NMEA"):
            harness._assert_privacy_safe(document)


def test_result_writer_refuses_exfat_output_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _load()
    exfat = tmp_path / "exfat"
    exfat.mkdir()
    monkeypatch.setattr(harness, "RECORDING_ROOT", exfat)
    with pytest.raises(harness.HarnessError, match="outside exFAT"):
        harness._write_result(exfat / "forbidden.json", {"passed": True})


def test_media_validator_requires_high_1080p30_and_hardware_decoder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _load()
    media = tmp_path / "candidate-00.mp4"
    media.write_bytes(b"synthetic-media")
    probe = {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "profile": "High",
                "width": 1920,
                "height": 1080,
                "r_frame_rate": "30/1",
            }
        ],
        "format": {},
    }
    calls: list[tuple[str, ...]] = []

    def fake_command(argv: tuple[str, ...], **_: object) -> CommandResult:
        calls.append(argv)
        return _result(argv, json.dumps(probe).encode() if argv[0] == harness.FFPROBE else b"")

    monkeypatch.setattr(harness, "_command", fake_command)
    evidence = harness._validate_media(media)

    assert evidence["ffprobe_high_1080p30_h264"] is True
    assert evidence["independent_hardware_h264_decode"] is True
    decode = next(command for command in calls if command[0] == harness.FFMPEG)
    assert decode[decode.index("-c:v") + 1] == "h264_v4l2m2m"

    probe["streams"][0]["r_frame_rate"] = "15/1"
    with pytest.raises(harness.HarnessError, match="High 1080p30"):
        harness._validate_media(media)


def test_qualification_does_not_start_or_stop_production_service_on_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load()
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(harness.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(harness, "_script_sha256", lambda: "a" * 64)
    monkeypatch.setattr(harness, "_board_identity", lambda _: {"model": "Pi", "serial": "0" * 16})
    monkeypatch.setattr(harness, "_release_identity", lambda _: {"release": "x", "path": "/x"})
    monkeypatch.setattr(harness, "_storage_identity", lambda _: {"uuid": "7EED-3EA7"})
    monkeypatch.setattr(harness, "_require_inactive", lambda _: {"NRestarts": "0"})
    monkeypatch.setattr(harness, "_throttle", lambda: "throttled=0x0")
    monkeypatch.setattr(
        harness, "_systemctl", lambda *argv, **_: commands.append(argv) or _result(argv)
    )
    monkeypatch.setattr(harness, "QUARANTINE_ROOT", Path("/not-present"))
    arguments = SimpleNamespace(
        expected_script_sha256="a" * 64,
        expected_release="0.1.0.dev0-0123456789abcdef",
        expected_board_serial="0" * 16,
        expected_storage_uuid="7EED-3EA7",
        duration_s=20.0,
        minimum_fps=29.5,
        dynamic_pixbuf_updates=False,
    )

    evidence = harness.qualify(arguments)

    assert evidence["passed"] is False
    assert evidence["service_starts"] == 0
    assert commands == []
    assert any("quarantine" in item for item in evidence["failures"])
