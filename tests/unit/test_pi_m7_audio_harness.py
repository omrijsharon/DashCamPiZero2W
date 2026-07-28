from __future__ import annotations

import hashlib
import importlib.util
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from uuid import UUID

import pytest

from dashcam.diagnostics.media import CommandResult
from dashcam.metadata.schema import AudioSummary, ClipSidecar, GpsSummary, VideoSummary
from dashcam.state import GpsTimeState, SystemClockState, TimestampQuality
from dashcam.storage.naming import finalized_unsynced_clip_pair

ROOT = Path(__file__).resolve().parents[2]
HARNESS_PATH = ROOT / "deploy/ssh-dev-validation/milestone7-audio/run.py"
BOOT_ID = UUID("601693e3-fa96-427e-906b-1621463a15cd")


def _load() -> ModuleType:
    name = "pi_m7_audio_harness"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, HARNESS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _sidecar(sequence: int) -> ClipSidecar:
    pair = finalized_unsynced_clip_pair(boot_id=BOOT_ID.hex[:12], sequence=sequence)
    start = sequence * 60_000_000_000
    return ClipSidecar(
        schema_version=1,
        clip_id=UUID(int=sequence + 1),
        boot_id=BOOT_ID,
        sequence=sequence,
        video_file=pair.video_name,
        metadata_file=pair.metadata_name,
        start_utc=None,
        end_utc=None,
        start_monotonic_ns=start,
        end_monotonic_ns=start + 60_000_000_000,
        gps_time_state=GpsTimeState.UNSYNCED,
        system_clock_state=SystemClockState.UNSET,
        timestamp_quality=TimestampQuality.MONOTONIC_ONLY,
        time_anchor=None,
        timezone="UTC",
        start_local=None,
        video=VideoSummary("h264", 1920, 1080, 30.0, 8_000_000, 8_000_000, 1800, 0),
        audio=AudioSummary(True, "aac", 48_000, 1, 128_000),
        gps=GpsSummary(False, None),
        protected=False,
        protection_reason=None,
        software_version="test",
    )


def _probe_document(*, skew_seconds: float = 0.02) -> dict[str, object]:
    return {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "profile": "High",
                "width": 1920,
                "height": 1080,
                "r_frame_rate": "30/1",
                "start_time": "0.000000",
                "duration": "60.000000",
                "bit_rate": "8000000",
            },
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "profile": "LC",
                "sample_rate": "48000",
                "channels": 1,
                "r_frame_rate": "0/0",
                "start_time": f"{skew_seconds:.6f}",
                "duration": "60.000000",
                "bit_rate": "127900",
            },
        ],
        "format": {"size": "60000000", "duration": "60.000000"},
    }


def _decode_result(*, timeout: bool = False) -> CommandResult:
    return CommandResult(("/usr/bin/ffmpeg",), 0, b"", b"", timed_out=timeout)


def test_stream_parser_is_closed_and_enforces_av_skew_and_audio_caps() -> None:
    harness = _load()
    passed = harness._validate_streams(_probe_document())

    assert all(passed["checks"].values())
    assert passed["video"]["width"] == 1920
    assert passed["video"]["height"] == 1080
    assert passed["audio"]["frame_rate"] == "0/0"
    assert passed["audio"]["bit_rate"] == 127900
    assert passed["maximum_stream_edge_av_skew_seconds"] == pytest.approx(0.02)

    skewed = harness._validate_streams(_probe_document(skew_seconds=0.101))
    assert skewed["checks"]["maximum_stream_edge_av_skew_100ms"] is False

    bad = _probe_document()
    streams = bad["streams"]
    assert isinstance(streams, list)
    audio = streams[1]
    assert isinstance(audio, dict)
    del audio["profile"]
    with pytest.raises(harness.HarnessError, match="schema differs"):
        harness._validate_streams(bad)

    wrong_dimensions = _probe_document()
    wrong_streams = wrong_dimensions["streams"]
    assert isinstance(wrong_streams, list)
    wrong_video = wrong_streams[0]
    assert isinstance(wrong_video, dict)
    wrong_video["width"] = 1280
    wrong_video["height"] = 720
    # The matching ordinary sidecar is intentionally irrelevant: actual
    # encoded dimensions are independent ffprobe evidence.
    actual = harness._validate_streams(wrong_dimensions)
    assert actual["checks"]["video_h264_high_1080p30"] is False

    wrong_audio_rate = _probe_document()
    wrong_rate_streams = wrong_audio_rate["streams"]
    assert isinstance(wrong_rate_streams, list)
    wrong_audio = wrong_rate_streams[1]
    assert isinstance(wrong_audio, dict)
    wrong_audio["r_frame_rate"] = "30/1"
    observed = harness._validate_streams(wrong_audio_rate)
    assert observed["checks"]["audio_ffprobe_rate_is_not_a_video_rate"] is False


def test_audio_sidecar_refuses_unavailable_or_wrong_contract(tmp_path: Path) -> None:
    harness = _load()
    clips = tmp_path / "clips"
    pending = tmp_path / "pending"
    clips.mkdir()
    pending.mkdir()
    harness.__dict__["CLIPS_ROOT"] = clips
    harness.__dict__["PENDING_ROOT"] = pending
    sidecar = _sidecar(161)
    video = clips / sidecar.video_file
    metadata = clips / sidecar.metadata_file
    video.write_bytes(b"media")
    metadata.write_bytes(sidecar.to_canonical_json())

    loaded, _, _ = harness._load_pair(BOOT_ID, 161, video.stat().st_dev)
    assert loaded.audio.available is True

    unavailable = replace(sidecar, audio=AudioSummary(False, None, None, None, None))
    metadata.write_bytes(unavailable.to_canonical_json())
    with pytest.raises(harness.HarnessError, match="ordinary audio clip"):
        harness._load_pair(BOOT_ID, 161, video.stat().st_dev)


def test_validate_media_reports_decode_timeout_as_failed_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load()
    loaded = [_sidecar(sequence) for sequence in range(161, 164)]
    index = 0

    monkeypatch.setattr(harness, "_checked_clips_root", lambda: 99)

    def load_pair(_: UUID, sequence: int, __: int) -> tuple[ClipSidecar, Path, dict[str, object]]:
        nonlocal index
        assert sequence == 161 + index
        sidecar = loaded[index]
        index += 1
        return sidecar, Path(f"/srv/dashcam/clips/{sidecar.video_file}"), {"sequence": sequence}

    monkeypatch.setattr(harness, "_load_pair", load_pair)
    monkeypatch.setattr(harness, "_compact_probe", lambda _: _probe_document())
    monkeypatch.setattr(harness, "_first_packet_idr", lambda _: None)
    monkeypatch.setattr(harness, "_decode", lambda _: _decode_result(timeout=True))

    result = harness.validate_media(BOOT_ID, 161, 3)

    assert result["passed"] is False
    assert result["checks"]["all_idr_and_independent_av_decodes"] is False


def test_validate_media_rejects_count_outside_bounded_range() -> None:
    harness = _load()
    with pytest.raises(harness.HarnessError, match="range"):
        harness.validate_media(BOOT_ID, 161, 2)
    with pytest.raises(harness.HarnessError, match="range"):
        harness.validate_media(BOOT_ID, 161, 11)


def test_manifest_and_cli_bind_closed_members_and_safe_output(tmp_path: Path) -> None:
    harness = _load()
    source = HARNESS_PATH.read_bytes()
    readme = b"# test\n"
    manifest = tmp_path / "SHA256SUMS"
    (tmp_path / "run.py").write_bytes(source)
    (tmp_path / "README.md").write_bytes(readme)
    manifest.write_text(
        f"{hashlib.sha256(readme).hexdigest()}  README.md\n"
        f"{hashlib.sha256(source).hexdigest()}  run.py\n",
        encoding="ascii",
    )
    manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert harness.verify_manifest(manifest_hash, tmp_path)

    parsed = harness._parser().parse_args(
        [
            "--expected-manifest-sha256",
            "a" * 64,
            "validate-media",
            "--boot-id",
            str(BOOT_ID),
            "--start-sequence",
            "161",
            "--output",
            "/tmp/result.json",
        ]
    )
    assert parsed.count == 3
    with pytest.raises(harness.HarnessError, match="outside /srv/dashcam"):
        harness._write_exclusive_json(Path("/srv/dashcam/result.json"), {"passed": False})


def test_fixed_commands_remain_compact_and_never_request_all_frames_or_packets() -> None:
    source = HARNESS_PATH.read_text(encoding="utf-8")

    assert "codec_name,profile,width,height" in source
    assert '"-read_intervals",' in source
    assert '"%+#1",' in source
    assert '"-show_packets",' in source
    assert '"-show_data",' in source
    assert '"-show_frames"' not in source
    assert (
        '"-show_packets",'
        not in source.split("def _compact_probe", 1)[1].split("def _first_packet_idr", 1)[0]
    )
    assert '"-c:v",' in source
    assert '"h264_v4l2m2m",' in source
    assert '"-map",' in source
    assert '"0:a:0",' in source
