from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from dashcam.diagnostics.media import (
    MAX_PROBE_JSON_BYTES,
    BoundaryValidation,
    CommandResult,
    MediaValidation,
    Outcome,
    TimelineEvidence,
    analyze_probe_document,
    parse_probe_json,
    probe_media_file,
    validate_boundaries,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "media"


def _fixture(name: str) -> dict[str, object]:
    value = json.loads((FIXTURES / name).read_bytes())
    assert isinstance(value, dict)
    return value


def _decoder(returncode: int = 0, *, truncated: bool = False) -> CommandResult:
    return CommandResult(("ffmpeg",), returncode, b"", b"", output_truncated=truncated)


def _checks(validation: MediaValidation) -> dict[str, Outcome]:
    return {check.code: check.outcome for check in validation.checks}


def test_valid_probe_and_decoder_evidence_pass() -> None:
    validation = analyze_probe_document(
        _fixture("valid_probe.json"),
        media_path="clip.mp4",
        decoder_result=_decoder(),
    )

    assert validation.overall is Outcome.PASS
    assert set(_checks(validation).values()) <= {Outcome.PASS}
    assert validation.to_dict()["overall"] == "pass"


def test_ffprobe_combined_packets_and_frames_evidence_passes() -> None:
    validation = analyze_probe_document(
        _fixture("valid_combined_probe.json"),
        media_path="clip.mp4",
        decoder_result=_decoder(),
        idr_document=_fixture("valid_probe.json"),
    )

    assert validation.overall is Outcome.PASS
    assert _checks(validation)["first_video_packet_keyframe"] is Outcome.PASS
    assert _checks(validation)["first_video_frame_keyframe"] is Outcome.PASS
    assert _checks(validation)["first_video_packet_idr"] is Outcome.PASS


def test_probe_without_decoder_never_claims_decodability() -> None:
    validation = analyze_probe_document(
        _fixture("valid_probe.json"),
        media_path="clip.mp4",
        decoder_result=None,
    )

    assert _checks(validation)["decoder_run"] is Outcome.INDETERMINATE
    assert validation.overall is Outcome.INDETERMINATE


def test_decoder_error_or_truncation_fails_decodability() -> None:
    document = _fixture("valid_probe.json")

    assert (
        _checks(
            analyze_probe_document(document, media_path="clip.mp4", decoder_result=_decoder(1))
        )["decoder_run"]
        is Outcome.FAIL
    )
    assert (
        _checks(
            analyze_probe_document(
                document,
                media_path="clip.mp4",
                decoder_result=_decoder(0, truncated=True),
            )
        )["decoder_run"]
        is Outcome.FAIL
    )


def test_threshold_and_codec_failures_are_stable() -> None:
    validation = analyze_probe_document(
        _fixture("failing_probe.json"),
        media_path="bad.mp4",
        decoder_result=_decoder(),
    )

    assert validation.overall is Outcome.FAIL
    assert _checks(validation) == {
        "video_codec_h264": Outcome.FAIL,
        "audio_codec_aac": Outcome.FAIL,
        "decoder_run": Outcome.PASS,
        "first_video_packet_keyframe": Outcome.FAIL,
        "first_video_frame_keyframe": Outcome.FAIL,
        "first_video_packet_idr": Outcome.FAIL,
        "duration": Outcome.FAIL,
        "video_bitrate": Outcome.FAIL,
        "av_skew": Outcome.FAIL,
    }


def test_missing_probe_fields_are_failures_or_indeterminate_not_exceptions() -> None:
    validation = analyze_probe_document(
        _fixture("missing_evidence_probe.json"),
        media_path="incomplete.mp4",
        decoder_result=_decoder(),
    )

    assert validation.overall is Outcome.FAIL
    assert _checks(validation)["first_video_packet_idr"] is Outcome.INDETERMINATE
    assert _checks(validation)["av_skew"] is Outcome.INDETERMINATE


@pytest.mark.parametrize("raw", [b"{", b"[]", b"\xff"])
def test_malformed_probe_json_is_rejected(raw: bytes) -> None:
    with pytest.raises(ValueError):
        parse_probe_json(raw)


def test_oversized_probe_json_is_rejected_before_parsing() -> None:
    with pytest.raises(ValueError, match="exceeds"):
        parse_probe_json(b" " * (MAX_PROBE_JSON_BYTES + 1))


def test_oversized_packet_list_is_rejected() -> None:
    raw = json.dumps({"packets": [{}] * 250_001}).encode()
    with pytest.raises(ValueError, match="packets"):
        parse_probe_json(raw)


def test_oversized_combined_packet_and_frame_list_is_rejected() -> None:
    raw = json.dumps({"packets_and_frames": [{}] * 20_001}).encode()
    with pytest.raises(ValueError, match="packets_and_frames"):
        parse_probe_json(raw)


def test_boundary_validation_uses_monotonic_timeline_not_raw_pts() -> None:
    base = analyze_probe_document(
        _fixture("valid_probe.json"),
        media_path="one.mp4",
        decoder_result=_decoder(),
        timeline=TimelineEvidence(1_000_000_000, 61_000_000_000),
    )
    following = analyze_probe_document(
        _fixture("valid_probe.json"),
        media_path="two.mp4",
        decoder_result=_decoder(),
        timeline=TimelineEvidence(61_020_000_000, 121_020_000_000),
    )

    boundary = validate_boundaries((base, following))[0]

    assert isinstance(boundary, BoundaryValidation)
    assert boundary.outcome is Outcome.PASS
    assert boundary.delta_seconds == pytest.approx(0.020)


@pytest.mark.parametrize(
    ("following_start", "code"),
    [
        (61_040_000_000, "gap_exceeds_one_frame"),
        (60_960_000_000, "overlap_exceeds_one_frame"),
    ],
)
def test_boundary_gap_or_overlap_larger_than_one_frame_fails(
    following_start: int, code: str
) -> None:
    previous = MediaValidation(
        1,
        "one.mp4",
        Outcome.PASS,
        (),
        TimelineEvidence(1_000_000_000, 61_000_000_000),
    )
    following = MediaValidation(
        1,
        "two.mp4",
        Outcome.PASS,
        (),
        TimelineEvidence(following_start, following_start + 60_000_000_000),
    )

    result = validate_boundaries((previous, following))[0]

    assert result.outcome is Outcome.FAIL
    assert result.code == code


def test_missing_timeline_is_indeterminate() -> None:
    result = validate_boundaries(
        (
            MediaValidation(1, "one.mp4", Outcome.PASS, ()),
            MediaValidation(1, "two.mp4", Outcome.PASS, ()),
        )
    )[0]
    assert result.outcome is Outcome.INDETERMINATE
    assert result.code == "missing_monotonic_timeline"


def test_probe_media_uses_fixed_argv_and_separate_decoder(tmp_path: Path) -> None:
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"fixture only")
    calls: list[tuple[str, ...]] = []

    def runner(
        argv: Sequence[str], *, timeout_seconds: float, max_output_bytes: int
    ) -> CommandResult:
        command = tuple(argv)
        calls.append(command)
        assert timeout_seconds == 2.0
        assert max_output_bytes == 10_000
        if command[0] == "ffprobe":
            fixture = (
                "valid_combined_probe.json" if "-show_frames" in command else "valid_probe.json"
            )
            return CommandResult(command, 0, (FIXTURES / fixture).read_bytes(), b"")
        return CommandResult(command, 0, b"", b"")

    result = probe_media_file(media, runner=runner, timeout_seconds=2.0, max_output_bytes=10_000)

    assert result.overall is Outcome.PASS
    assert [call[0] for call in calls] == ["ffprobe", "ffprobe", "ffmpeg"]
    assert all(str(media.resolve()) in call for call in calls)


def test_probe_media_rejects_unbounded_command_settings(tmp_path: Path) -> None:
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"fixture only")

    with pytest.raises(ValueError, match="validated maxima"):
        probe_media_file(media, timeout_seconds=121.0)
    with pytest.raises(ValueError, match="validated maxima"):
        probe_media_file(media, max_output_bytes=8 * 1024 * 1024 + 1)


def test_malformed_keyframe_value_is_a_failure_not_an_exception() -> None:
    document = _fixture("valid_probe.json")
    frames = document["frames"]
    assert isinstance(frames, list)
    first = frames[0]
    assert isinstance(first, dict)
    first["key_frame"] = "not-a-number"

    validation = analyze_probe_document(
        document,
        media_path="clip.mp4",
        decoder_result=_decoder(),
    )

    assert _checks(validation)["first_video_frame_keyframe"] is Outcome.FAIL


def test_failed_probe_does_not_invoke_decoder(tmp_path: Path) -> None:
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"fixture only")
    calls = 0

    def runner(
        argv: Sequence[str], *, timeout_seconds: float, max_output_bytes: int
    ) -> CommandResult:
        nonlocal calls
        calls += 1
        return CommandResult(("ffprobe",), 1, b"", b"error")

    result = probe_media_file(media, runner=runner)

    assert result.overall is Outcome.FAIL
    assert calls == 1
