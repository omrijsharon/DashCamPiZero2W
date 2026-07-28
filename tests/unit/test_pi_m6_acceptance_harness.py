from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from dashcam.diagnostics.media import Check, CommandResult, MediaValidation, Outcome
from dashcam.metadata.schema import AudioSummary, ClipSidecar, GpsSummary, VideoSummary
from dashcam.state import GpsTimeState, SystemClockState, TimestampQuality
from dashcam.storage.naming import finalized_unsynced_clip_pair

ROOT = Path(__file__).resolve().parents[2]
HARNESS_PATH = (
    ROOT / "deploy/ssh-dev-validation/milestone6-acceptance/run.py"
)
README_PATH = HARNESS_PATH.with_name("README.md")
MANIFEST_PATH = HARNESS_PATH.with_name("SHA256SUMS")
BOOT_ID = UUID("601693e3-fa96-427e-906b-1621463a15cd")


def _load() -> ModuleType:
    name = "pi_m6_acceptance_harness"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, HARNESS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _status_document(*, dropped: int = 0, state: str = "RECORDING") -> dict[str, object]:
    return {
        "schema_version": 1,
        "lifecycle": {
            "state": state,
            "reason": None,
            "detail": None,
            "sequence": 4,
            "config_schema_version": 1,
            "notification_failures": 0,
        },
        "runtime": {
            "video": {
                "width": 1920,
                "height": 1080,
                "frames_per_second": 30,
                "codec": "h264",
                "hardware_encoded": True,
                "effective_caps": {
                    "raw_format": "NV12",
                    "fps_numerator": 30,
                    "fps_denominator": 1,
                    "h264_profile": "high",
                    "h264_level": "4.1",
                },
                "configured": {
                    "target_bitrate_bps": 8_000_000,
                    "keyframe_interval_frames": 30,
                },
                "encoder_identity": {
                    "factory_name": "v4l2h264enc",
                    "factory_class": "Codec/Encoder/Video/Hardware",
                    "device_path": "/dev/video11",
                },
            },
            "frames": {
                "raw": 3_600,
                "encoded": 3_600,
                "dropped": dropped,
                "drop_source": "pts_discontinuity",
            },
            "pipeline_restart_count": 0,
            "last_clip": {
                "sequence": 22,
                "duration_ns": 59_988_667_000,
                "bitrate_bps": 8_004_656,
                "frames": {"raw": 1_800, "encoded": 1_800, "dropped": 0},
            },
            "storage_preflight": {
                "state": "READY",
                "reasons": [],
                "ready": True,
                "mount": {
                    "target": "/srv/dashcam",
                    "mounted": True,
                    "filesystem": "exfat",
                    "label": "DASHCAM",
                    "uuid_suffix": "3EA7",
                    "device_id": "179:3",
                    "read_write": True,
                },
                "free_bytes": 20_000_000_000,
                "capacity_bytes": 24_000_000_000,
            },
        },
    }


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
        video=VideoSummary(
            "h264",
            1920,
            1080,
            30.0,
            8_000_000,
            8_000_000,
            1_800,
            0,
        ),
        audio=AudioSummary(False, None, None, None, None),
        gps=GpsSummary(False, None),
        protected=False,
        protection_reason=None,
        software_version="test",
    )


def _install_pairs(tmp_path: Path, harness: ModuleType, *, gap_ns: int = 0) -> None:
    clips = tmp_path / "clips"
    pending = tmp_path / "pending"
    clips.mkdir()
    pending.mkdir()
    harness.__dict__["RECORDING_ROOT"] = tmp_path
    harness.__dict__["CLIPS_ROOT"] = clips
    harness.__dict__["PENDING_ROOT"] = pending
    for sequence in range(20, 30):
        sidecar = _sidecar(sequence)
        if sequence >= 25 and gap_ns:
            sidecar = replace(
                sidecar,
                start_monotonic_ns=sidecar.start_monotonic_ns + gap_ns,
                end_monotonic_ns=sidecar.end_monotonic_ns + gap_ns,
            )
        (clips / sidecar.video_file).write_bytes(b"synthetic-media-for-injected-probe")
        (clips / sidecar.metadata_file).write_bytes(sidecar.to_canonical_json())


def _passing_probe(
    media_path: Path,
    *,
    thresholds: Any,
    timeline: Any,
) -> MediaValidation:
    del thresholds
    return MediaValidation(
        1,
        str(media_path),
        Outcome.PASS,
        (
            Check("video_codec_h264", Outcome.PASS, "H.264", "h264", "h264"),
            Check("decoder_run", Outcome.PASS, "decoded", 0, 0),
            Check("first_video_packet_keyframe", Outcome.PASS, "key", True, True),
            Check("first_video_frame_keyframe", Outcome.PASS, "key", True, True),
            Check("first_video_packet_idr", Outcome.PASS, "IDR", True, True),
            Check("duration", Outcome.PASS, "duration", 60.0, "59..61"),
            Check(
                "video_bitrate",
                Outcome.PASS,
                "bitrate",
                8_000_000,
                "80000..15920000",
            ),
            Check("av_skew", Outcome.NOT_APPLICABLE, "video-only"),
        ),
        timeline,
    )


def _sample(harness: ModuleType, index: int) -> Any:
    status = _status_document()
    runtime = status["runtime"]
    assert isinstance(runtime, dict)
    last_clip = runtime["last_clip"]
    frames = runtime["frames"]
    assert isinstance(last_clip, dict)
    assert isinstance(frames, dict)
    last_clip["sequence"] = 20 + index
    last_clip["bitrate_bps"] = 8_000_000
    frames["raw"] = 1_000 + index * 300
    frames["encoded"] = 1_000 + index * 300
    return harness.AcceptanceSample(
        monotonic_ns=(index + 1) * 10_000_000_000,
        recorder_status=status,
        rss_bytes=50_000_000 + index * 1024,
        system_used_memory_bytes=100_000_000,
        memory_available_bytes=300_000_000,
        swap_used_bytes=0,
        cpu_percent=40.0,
        temperature_c=55.0,
        throttled=False,
        undervoltage=False,
        filesystem_free_bytes=20_000_000_000 - index * 10_000_000,
        raw_frames=1_000 + index * 300,
        encoded_frames=1_000 + index * 300,
        dropped_frames=0,
        clip_sequence=20 + index,
        bitrate_bps=8_000_000,
        restart_count=0,
    )


def _zram_policy(harness: ModuleType) -> Any:
    return harness.SwapPolicy(
        "/proc/swaps",
        "a" * 64,
        "/dev/zram0",
        "partition",
        434_172 * 1024,
        10_000_000,
        100,
    )


def test_status_parser_requires_exact_effective_hardware_and_non_null_counters() -> None:
    harness = _load()
    document = _status_document()
    payload = json.dumps(document, separators=(",", ":")).encode()
    parsed, counters = harness.parse_status_snapshot(payload)

    assert parsed["schema_version"] == 1
    assert counters.lifecycle_state == "RECORDING"
    assert counters.raw_frames == 3_600
    assert counters.encoded_frames == 3_600
    assert counters.dropped_frames == 0
    assert counters.pipeline_restart_count == 0
    assert counters.storage_free_bytes == 20_000_000_000

    runtime = document["runtime"]
    assert isinstance(runtime, dict)
    runtime_frames = runtime["frames"]
    assert isinstance(runtime_frames, dict)
    bad_frames = dict(runtime_frames)
    bad_frames["dropped"] = None
    runtime["frames"] = bad_frames
    with pytest.raises(harness.HarnessError, match="dropped"):
        harness.parse_status_snapshot(json.dumps(document).encode())


def test_status_parser_rejects_duplicate_keys_and_non_hardware_encoder() -> None:
    harness = _load()
    with pytest.raises(harness.HarnessError, match="duplicate"):
        harness.parse_status_snapshot(
            b'{"schema_version":1,"schema_version":1,"lifecycle":{},"runtime":{}}'
        )

    document = _status_document()
    runtime = document["runtime"]
    assert isinstance(runtime, dict)
    video = runtime["video"]
    assert isinstance(video, dict)
    identity = video["encoder_identity"]
    assert isinstance(identity, dict)
    identity["factory_name"] = "x264enc"
    with pytest.raises(harness.HarnessError, match="hardware encoder"):
        harness.parse_status_snapshot(json.dumps(document).encode())


def test_ten_clip_media_acceptance_uses_canonical_pairs_and_normalized_timeline(
    tmp_path: Path,
) -> None:
    harness = _load()
    _install_pairs(tmp_path, harness)

    result = harness.validate_media_acceptance(
        BOOT_ID,
        20,
        probe=_passing_probe,
        bitrate_probe=lambda _path: 8_000_000,
    )

    assert result["passed"] is True
    assert result["clip_count"] == 10
    assert result["average_bitrate_bps"] == 8_000_000
    assert len(result["pairs"]) == 10
    assert len(result["boundaries"]) == 9
    assert all(item["outcome"] == "pass" for item in result["boundaries"])


def test_ten_clip_media_rejects_more_than_one_frame_monotonic_gap(tmp_path: Path) -> None:
    harness = _load()
    _install_pairs(tmp_path, harness, gap_ns=40_000_000)

    result = harness.validate_media_acceptance(
        BOOT_ID,
        20,
        probe=_passing_probe,
        bitrate_probe=lambda _path: 8_000_000,
    )

    assert result["passed"] is False
    assert result["checks"]["all_boundaries_within_one_frame"] is False
    assert any(item["code"] == "gap_exceeds_one_frame" for item in result["boundaries"])


def test_media_aggregate_uses_compact_ffprobe_when_general_check_is_absent(
    tmp_path: Path,
) -> None:
    harness = _load()
    _install_pairs(tmp_path, harness)
    compact_payload = json.dumps(
        {
            "streams": [{"codec_type": "video", "bit_rate": "8002431"}],
            "format": {
                "bit_rate": "8006274",
                "size": "59065000",
                "duration": "59.022059877",
            },
            "programs": [],
            "stream_groups": [],
        },
        separators=(",", ":"),
    ).encode()
    measured = harness.parse_compact_bitrate_probe(compact_payload)

    def probe_without_bitrate(
        media_path: Path,
        *,
        thresholds: Any,
        timeline: Any,
    ) -> MediaValidation:
        validation = _passing_probe(
            media_path,
            thresholds=thresholds,
            timeline=timeline,
        )
        return replace(
            validation,
            checks=tuple(
                check for check in validation.checks if check.code != "video_bitrate"
            ),
        )

    result = harness.validate_media_acceptance(
        BOOT_ID,
        20,
        probe=probe_without_bitrate,
        bitrate_probe=lambda _path: measured,
    )

    assert measured == 8_002_431
    assert result["passed"] is True
    assert result["average_bitrate_bps"] == 8_002_431
    assert all(
        pair["compact_probe_bitrate_bps"] == 8_002_431
        for pair in result["pairs"]
    )

    def failed_decode(
        media_path: Path,
        *,
        thresholds: Any,
        timeline: Any,
    ) -> MediaValidation:
        validation = probe_without_bitrate(
            media_path,
            thresholds=thresholds,
            timeline=timeline,
        )
        return replace(
            validation,
            overall=Outcome.FAIL,
            checks=tuple(
                replace(check, outcome=Outcome.FAIL)
                if check.code == "decoder_run"
                else check
                for check in validation.checks
            ),
        )

    decode_refused = harness.validate_media_acceptance(
        BOOT_ID,
        20,
        probe=failed_decode,
        bitrate_probe=lambda _path: measured,
    )
    assert decode_refused["passed"] is False
    assert decode_refused["checks"]["all_h264_decode_idr_duration"] is False

    with pytest.raises(harness.HarnessError, match="no measured"):
        harness.parse_compact_bitrate_probe(
            b'{"streams":[{"codec_type":"video"}],"format":{}}'
        )


def test_compact_bitrate_probe_refuses_nonempty_or_unknown_top_level_shapes() -> None:
    harness = _load()
    payload: dict[str, object] = {
        "streams": [{"codec_type": "video", "bit_rate": "8002431"}],
        "format": {},
    }
    rejected_documents = (
        payload | {"programs": [{}]},
        payload | {"stream_groups": [{}]},
        payload | {"chapters": []},
    )

    for document in rejected_documents:
        with pytest.raises(harness.HarnessError, match=r"schema is not closed|not empty"):
            harness.parse_compact_bitrate_probe(
                json.dumps(document, separators=(",", ":")).encode()
            )


def test_production_probe_uses_compact_items_and_explicit_hardware_decode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load()
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"synthetic-command-fixture")
    calls: list[tuple[tuple[str, ...], float, int]] = []
    compact = json.dumps(
        {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "start_time": "0",
                    "duration": "59.022333",
                    "bit_rate": "8002431",
                }
            ],
            "format": {
                "duration": "59.022333",
                "bit_rate": "8006274",
                "size": "59065000",
            },
            "packets": [{"codec_type": "video", "flags": "K_"}],
            "frames": [{"media_type": "video", "key_frame": 1}],
        },
        separators=(",", ":"),
    ).encode()
    idr = json.dumps(
        {
            "packets": [
                {
                    "codec_type": "video",
                    "flags": "K_",
                    "data": "00000000: 00 00 00 01 65 " + "00 " * 140_000,
                }
            ]
        },
        separators=(",", ":"),
    ).encode()

    def runner(
        argv: Any,
        *,
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> CommandResult:
        command = tuple(argv)
        calls.append((command, timeout_seconds, max_output_bytes))
        if command[0] == harness.FFMPEG:
            return CommandResult(command, 0, b"", b"")
        payload = idr if "-show_data" in command else compact
        return CommandResult(command, 0, payload, b"")

    monkeypatch.setattr(harness, "run_fixed_argv", runner)
    validation = harness._production_probe(
        media,
        thresholds=harness.MediaThresholds(),
        timeline=None,
    )

    checks = {check.code: check.outcome for check in validation.checks}
    assert checks["decoder_run"] is Outcome.PASS
    assert checks["first_video_packet_idr"] is Outcome.PASS
    assert checks["first_video_packet_keyframe"] is Outcome.PASS
    assert checks["first_video_frame_keyframe"] is Outcome.PASS
    assert any("%+#1" in command for command, _, _ in calls)
    assert harness.MAX_STATUS_BYTES < len(idr) <= harness.MAX_JSON_BYTES
    idr_call = next(call for call in calls if "-show_data" in call[0])
    assert idr_call[2] == harness.MAX_JSON_BYTES
    decode = next(command for command, _, _ in calls if command[0] == harness.FFMPEG)
    assert decode[decode.index("-c:v") + 1] == "h264_v4l2m2m"
    assert all(timeout <= 120 for _, timeout, _ in calls)


def test_media_refuses_pending_member_or_sidecar_without_real_frames(tmp_path: Path) -> None:
    harness = _load()
    _install_pairs(tmp_path, harness)
    pending = tmp_path / "pending" / f"boot-{BOOT_ID.hex[:12]}-000020.partial.mp4"
    pending.write_bytes(b"still-pending")
    with pytest.raises(harness.HarnessError, match="pending member"):
        harness.validate_media_acceptance(
            BOOT_ID,
            20,
            probe=_passing_probe,
            bitrate_probe=lambda _path: 8_000_000,
        )
    pending.unlink()

    sidecar_path = tmp_path / "clips" / f"boot-{BOOT_ID.hex[:12]}-000020.json"
    sidecar = replace(
        _sidecar(20),
        video=replace(_sidecar(20).video, frames_written=0),
    )
    sidecar_path.write_bytes(sidecar.to_canonical_json())
    with pytest.raises(harness.HarnessError, match="ordinary M6 clip"):
        harness.validate_media_acceptance(
            BOOT_ID,
            20,
            probe=_passing_probe,
            bitrate_probe=lambda _path: 8_000_000,
        )


def test_endurance_analysis_passes_complete_bounded_samples_and_refuses_missing() -> None:
    harness = _load()
    samples = tuple(_sample(harness, index) for index in range(3))

    result = harness.analyze_endurance_acceptance(
        0,
        30_000_000_000,
        samples,
        swap_policy=_zram_policy(harness),
        required_duration_s=30.0,
        required_samples=3,
    )

    assert result["passed"] is True
    assert result["checks"]["counter_ordered"] is True
    assert result["checks"]["frame_shapes_valid"] is True
    assert result["checks"]["frame_counter_alignment_valid"] is True
    assert result["checks"]["maximum_frame_counter_delta"] == 0
    assert result["diagnostic_analysis"]["outcome"] == "pass"

    with pytest.raises(harness.HarnessError, match="sample count"):
        harness.analyze_endurance_acceptance(
            0,
            30_000_000_000,
            samples[:2],
            swap_policy=_zram_policy(harness),
            required_duration_s=30.0,
            required_samples=3,
        )


def test_endurance_analysis_accepts_one_buffer_encoded_lead() -> None:
    harness = _load()
    samples = tuple(
        replace(
            _sample(harness, index),
            encoded_frames=_sample(harness, index).raw_frames + 1,
        )
        for index in range(3)
    )

    result = harness.analyze_endurance_acceptance(
        0,
        30_000_000_000,
        samples,
        swap_policy=_zram_policy(harness),
        required_duration_s=30.0,
        required_samples=3,
    )

    assert result["passed"] is True
    assert result["checks"]["counter_ordered"] is True
    assert result["checks"]["frame_counter_alignment_valid"] is True
    assert result["checks"]["maximum_frame_counter_delta"] == 1
    assert result["checks"]["maximum_accepted_frame_counter_delta"] == 1
    assert result["diagnostic_analysis"]["outcome"] == "pass"


def test_endurance_analysis_rejects_frame_counter_divergence_over_one() -> None:
    harness = _load()
    samples = tuple(
        replace(
            _sample(harness, index),
            encoded_frames=_sample(harness, index).raw_frames + 2,
        )
        for index in range(3)
    )

    result = harness.analyze_endurance_acceptance(
        0,
        30_000_000_000,
        samples,
        swap_policy=_zram_policy(harness),
        required_duration_s=30.0,
        required_samples=3,
    )

    assert result["passed"] is False
    assert result["checks"]["counter_ordered"] is True
    assert result["checks"]["frame_shapes_valid"] is False
    assert result["checks"]["frame_counter_alignment_valid"] is False
    assert result["checks"]["maximum_frame_counter_delta"] == 2
    assert result["diagnostic_analysis"]["outcome"] == "pass"


def test_endurance_analysis_fails_drop_restart_throttle_and_counter_regression() -> None:
    harness = _load()
    samples = [_sample(harness, index) for index in range(3)]
    samples[1] = replace(
        samples[1],
        dropped_frames=1,
        restart_count=1,
        throttled=True,
        encoded_frames=samples[0].encoded_frames - 1,
    )
    samples[2] = replace(samples[2], dropped_frames=1, restart_count=1)

    result = harness.analyze_endurance_acceptance(
        0,
        30_000_000_000,
        samples,
        swap_policy=_zram_policy(harness),
        required_duration_s=30.0,
        required_samples=3,
    )

    assert result["passed"] is False
    assert result["checks"]["counter_ordered"] is False
    assert result["diagnostic_analysis"]["outcome"] == "fail"


def test_endurance_accepts_declining_zram_use_and_rejects_growth() -> None:
    harness = _load()
    declining = tuple(
        replace(_sample(harness, index), swap_used_bytes=10_000_000 - index * 1_000_000)
        for index in range(3)
    )

    accepted = harness.analyze_endurance_acceptance(
        0,
        30_000_000_000,
        declining,
        swap_policy=_zram_policy(harness),
        required_duration_s=30.0,
        required_samples=3,
    )

    assert accepted["passed"] is True
    assert accepted["checks"]["swap_no_growth"] is True
    assert accepted["checks"]["swap_baseline_bytes"] == 10_000_000
    assert accepted["checks"]["maximum_swap_used_bytes"] == 10_000_000
    assert accepted["diagnostic_analysis"]["outcome"] == "pass"

    growing = (
        declining[0],
        replace(declining[1], swap_used_bytes=10_000_001),
        declining[2],
    )
    refused = harness.analyze_endurance_acceptance(
        0,
        30_000_000_000,
        growing,
        swap_policy=_zram_policy(harness),
        required_duration_s=30.0,
        required_samples=3,
    )
    assert refused["passed"] is False
    assert refused["checks"]["swap_no_growth"] is False
    assert refused["checks"]["swap_growth_above_baseline_bytes"] == 1
    assert refused["diagnostic_analysis"]["outcome"] == "fail"


def test_swap_policy_requires_exactly_one_zram0_partition() -> None:
    harness = _load()
    payload = (
        b"Filename Type Size Used Priority\n"
        b"/dev/zram0 partition 434172 10884 100\n"
    )

    policy = harness.parse_swap_policy(payload)

    assert policy.device == "/dev/zram0"
    assert policy.kind == "partition"
    assert policy.size_bytes == 434_172 * 1024
    assert policy.used_bytes == 10_884 * 1024

    with pytest.raises(harness.HarnessError, match="zram0"):
        harness.parse_swap_policy(
            b"Filename Type Size Used Priority\n"
            b"/swapfile file 1024 0 -2\n"
        )
    with pytest.raises(harness.HarnessError, match="schema"):
        harness.parse_swap_policy(
            payload + b"/dev/mmcblk0p2 partition 1024 0 -3\n"
        )


def test_reanalysis_is_hash_bound_compact_and_recomputes_without_source_mutation(
    tmp_path: Path,
) -> None:
    harness = _load()
    samples = tuple(
        replace(_sample(harness, index), swap_used_bytes=10_000_000 - index * 1_000_000)
        for index in range(3)
    )
    source_document = harness.analyze_endurance_acceptance(
        0,
        30_000_000_000,
        samples,
        swap_policy=_zram_policy(harness),
        required_duration_s=30.0,
        required_samples=3,
    )
    source_payload = (
        json.dumps(source_document, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    source = tmp_path / "retained.json"
    source.write_bytes(source_payload)
    source_hash = hashlib.sha256(source_payload).hexdigest()

    result = harness.reanalyze_endurance(
        source,
        source_hash,
        swap_policy=_zram_policy(harness),
        required_duration_s=30.0,
        required_samples=3,
    )

    assert result["passed"] is True
    assert result["source"]["sha256"] == source_hash
    assert result["swap_observation"]["baseline_bytes"] == 10_000_000
    assert result["swap_observation"]["maximum_bytes"] == 10_000_000
    assert result["swap_observation"]["growth_above_baseline_bytes"] == 0
    assert "samples" not in result["corrected_analysis"]["diagnostic_analysis"]
    assert source.read_bytes() == source_payload

    with pytest.raises(harness.HarnessError, match="hash differs"):
        harness.reanalyze_endurance(
            source,
            "0" * 64,
            swap_policy=_zram_policy(harness),
            required_duration_s=30.0,
            required_samples=3,
        )


def test_reanalysis_rejects_malformed_source_and_original_non_swap_failure(
    tmp_path: Path,
) -> None:
    harness = _load()
    samples = tuple(_sample(harness, index) for index in range(3))
    source_document = harness.analyze_endurance_acceptance(
        0,
        30_000_000_000,
        samples,
        swap_policy=_zram_policy(harness),
        required_duration_s=30.0,
        required_samples=3,
    )
    checks = source_document["checks"]
    assert isinstance(checks, dict)
    checks["counter_ordered"] = False
    payload = (
        json.dumps(source_document, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    source = tmp_path / "failed-structural.json"
    source.write_bytes(payload)

    with pytest.raises(harness.HarnessError, match="non-swap structural"):
        harness.reanalyze_endurance(
            source,
            hashlib.sha256(payload).hexdigest(),
            swap_policy=_zram_policy(harness),
            required_duration_s=30.0,
            required_samples=3,
        )

    malformed = dict(source_document)
    malformed.pop("samples")
    malformed_payload = json.dumps(malformed, sort_keys=True).encode()
    source.write_bytes(malformed_payload)
    with pytest.raises(harness.HarnessError, match="schema"):
        harness.reanalyze_endurance(
            source,
            hashlib.sha256(malformed_payload).hexdigest(),
            swap_policy=_zram_policy(harness),
            required_duration_s=30.0,
            required_samples=3,
        )


def test_proc_parsers_are_bounded_and_process_stat_handles_spaces(tmp_path: Path) -> None:
    harness = _load()
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        "MemTotal: 500000 kB\n"
        "MemAvailable: 300000 kB\n"
        "SwapTotal: 1000 kB\n"
        "SwapFree: 1000 kB\n",
        encoding="ascii",
    )
    memory = harness.read_memory(meminfo)
    assert memory.used_bytes == 200_000 * 1024
    assert memory.swap_used_bytes == 0

    process = tmp_path / "123"
    process.mkdir()
    # Fields after the command begin at Linux proc field 3. The positions used
    # by the harness are utime=14, stime=15, and starttime=22.
    post_comm = ["S", *["0"] * 10, "100", "20", *["0"] * 6, "777", "0", "0"]
    (process / "stat").write_text(
        f"123 (dash cam worker) {' '.join(post_comm)}\n",
        encoding="ascii",
    )
    (process / "status").write_text("VmRSS: 1234 kB\n", encoding="ascii")
    reading = harness.read_process(123, tmp_path)
    assert reading.ticks == 120
    assert reading.start_ticks == 777
    assert reading.rss_bytes == 1234 * 1024


def test_throttle_reader_retains_latched_events(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _load()

    def result(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(
            returncode=0,
            timed_out=False,
            output_truncated=False,
            stdout=b"throttled=0x50000\n",
        )

    monkeypatch.setattr(harness, "run_fixed_argv", result)
    assert harness.read_throttle() == (True, True)


def test_evidence_writer_is_exclusive_and_refuses_recording_volume(tmp_path: Path) -> None:
    harness = _load()
    recording = tmp_path / "recording"
    recording.mkdir()
    harness.__dict__["RECORDING_ROOT"] = recording
    outside = tmp_path / "evidence.json"

    harness._write_exclusive_json(outside, {"schema_version": 1})
    assert json.loads(outside.read_bytes()) == {"schema_version": 1}
    with pytest.raises(harness.HarnessError, match="new file"):
        harness._write_exclusive_json(outside, {"schema_version": 1})
    with pytest.raises(harness.HarnessError, match="recording volume"):
        harness._write_exclusive_json(
            recording / "forbidden.json",
            {"schema_version": 1},
        )


def test_cli_and_source_are_read_only_fixed_duration_and_hash_closed() -> None:
    harness = _load()
    parser = harness._parser()
    manifest = "a" * 64
    media = parser.parse_args(
        [
            "--expected-manifest-sha256",
            manifest,
            "validate-media",
            "--boot-id",
            str(BOOT_ID),
            "--start-sequence",
            "20",
            "--output",
            "/tmp/media.json",
        ]
    )
    endurance = parser.parse_args(
        [
            "--expected-manifest-sha256",
            manifest,
            "collect-endurance",
            "--pid",
            "123",
            "--output",
            "/tmp/endurance.json",
        ]
    )
    reanalysis = parser.parse_args(
        [
            "--expected-manifest-sha256",
            manifest,
            "reanalyze-endurance",
            "--source",
            "/tmp/endurance.json",
            "--expected-source-sha256",
            "b" * 64,
            "--output",
            "/tmp/reanalysis.json",
        ]
    )
    assert media.phase == "validate-media"
    assert endurance.phase == "collect-endurance"
    assert reanalysis.phase == "reanalyze-endurance"
    assert harness.ENDURANCE_DURATION_S == 7200.0
    assert harness.ENDURANCE_INTERVAL_S == 10.0
    assert harness.ENDURANCE_SAMPLE_COUNT == 720

    source = HARNESS_PATH.read_text(encoding="utf-8")
    forbidden = (
        "systemctl start",
        "systemctl stop",
        "systemctl restart",
        "nmcli",
        "NetworkManager",
        "mkfs",
        "sfdisk",
    )
    assert all(token not in source for token in forbidden)
    assert 'Path("/srv/dashcam")' in source
    assert 'Path("/run/dashcam/status.json")' in source
    assert 'Path("/proc/swaps")' in source
    assert '"h264_v4l2m2m"' in source
    assert '"%+#1"' in source

    readme = README_PATH.read_text(encoding="utf-8")
    assert "never starts, stops, restarts" in readme
    assert "720 samples" in readme
    assert "7,200 seconds" in readme


def test_checked_manifest_is_closed_and_matches_members() -> None:
    harness = _load()
    lines = MANIFEST_PATH.read_text(encoding="ascii").splitlines()
    assert [line.split("  ", 1)[1] for line in lines] == ["README.md", "run.py"]
    manifest_hash = harness._sha256_file(MANIFEST_PATH, maximum=4096)
    entries = harness.verify_manifest(manifest_hash, MANIFEST_PATH.parent)
    assert tuple(entries) == ("README.md", "run.py")
