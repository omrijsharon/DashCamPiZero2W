"""Bounded, evidence-conscious media validation.

The validator separates container/packet inspection (``ffprobe``) from a real
decoder run (``ffmpeg``).  A successful probe is never reported as proof that
the media decodes.
"""

from __future__ import annotations

import json
import math
import subprocess
import threading
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Any, Protocol

MAX_PROBE_JSON_BYTES = 8 * 1024 * 1024
MAX_PROBE_ITEMS = 20_000
MAX_COMMAND_OUTPUT_BYTES = 8 * 1024 * 1024
DEFAULT_COMMAND_TIMEOUT_SECONDS = 120.0


class Outcome(StrEnum):
    """Stable validation outcome values."""

    PASS = "pass"
    FAIL = "fail"
    INDETERMINATE = "indeterminate"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class CommandResult:
    """Bounded subprocess result returned by an injectable runner."""

    argv: tuple[str, ...]
    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    output_truncated: bool = False


class FixedArgvRunner(Protocol):
    """Run an already-tokenized command without a shell."""

    def __call__(
        self, argv: Sequence[str], *, timeout_seconds: float, max_output_bytes: int
    ) -> CommandResult: ...


def run_fixed_argv(
    argv: Sequence[str], *, timeout_seconds: float, max_output_bytes: int
) -> CommandResult:
    """Execute fixed argv with bounded retained output and no shell.

    Reader threads continuously drain both pipes, but retain at most the configured
    byte budget.  This prevents a noisy child from filling a pipe or consuming
    unbounded Python memory.
    """

    if (
        not argv
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= DEFAULT_COMMAND_TIMEOUT_SECONDS
        or not 0 < max_output_bytes <= MAX_COMMAND_OUTPUT_BYTES
    ):
        raise ValueError("command timeout/output bounds exceed the validated limits")
    command = tuple(str(part) for part in argv)
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        close_fds=True,
    )
    captured = [bytearray(), bytearray()]
    truncated = [False, False]

    def drain(stream: Any, index: int) -> None:
        while chunk := stream.read(65_536):
            remaining = max_output_bytes - len(captured[index])
            if remaining > 0:
                captured[index].extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated[index] = True

    assert process.stdout is not None
    assert process.stderr is not None
    readers = [
        threading.Thread(target=drain, args=(process.stdout, 0), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, 1), daemon=True),
    ]
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        returncode: int | None = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        returncode = process.wait()
    for reader in readers:
        reader.join(timeout=2.0)
    return CommandResult(
        argv=command,
        returncode=returncode,
        stdout=bytes(captured[0]),
        stderr=bytes(captured[1]),
        timed_out=timed_out,
        output_truncated=any(truncated),
    )


@dataclass(frozen=True)
class Check:
    """One stable validation check."""

    code: str
    outcome: Outcome
    summary: str
    observed: str | float | int | bool | None = None
    expected: str | float | int | bool | None = None


@dataclass(frozen=True)
class TimelineEvidence:
    """Clip placement on the recorder's monotonic capture timeline."""

    start_monotonic_ns: int
    end_monotonic_ns: int

    def __post_init__(self) -> None:
        if self.start_monotonic_ns < 0 or self.end_monotonic_ns < self.start_monotonic_ns:
            raise ValueError("invalid monotonic timeline interval")


@dataclass(frozen=True)
class MediaThresholds:
    nominal_duration_seconds: float = 60.0
    duration_tolerance_seconds: float = 1.0
    target_video_bitrate_bps: int = 8_000_000
    bitrate_tolerance_fraction: float = 0.25
    maximum_av_skew_seconds: float = 0.100
    frame_rate: float = 30.0

    def __post_init__(self) -> None:
        if (
            self.nominal_duration_seconds <= 0
            or self.duration_tolerance_seconds < 0
            or self.target_video_bitrate_bps <= 0
            or not 0 <= self.bitrate_tolerance_fraction < 1
            or self.maximum_av_skew_seconds < 0
            or self.frame_rate <= 0
        ):
            raise ValueError("invalid media thresholds")


DEFAULT_MEDIA_THRESHOLDS = MediaThresholds()


@dataclass(frozen=True)
class MediaValidation:
    """Serializable result for one media file."""

    schema_version: int
    media_path: str
    overall: Outcome
    checks: tuple[Check, ...]
    timeline: TimelineEvidence | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["overall"] = self.overall.value
        for check in value["checks"]:
            check["outcome"] = check["outcome"].value
        return value


@dataclass(frozen=True)
class BoundaryValidation:
    """Normalized continuity result for two adjacent clips."""

    previous_path: str
    next_path: str
    delta_seconds: float
    frame_period_seconds: float
    outcome: Outcome
    code: str

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["outcome"] = self.outcome.value
        return value


def _number(value: object) -> float | None:
    try:
        result = float(str(value))
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _stream(document: Mapping[str, Any], codec_type: str) -> Mapping[str, Any] | None:
    streams = document.get("streams")
    if not isinstance(streams, list) or len(streams) > MAX_PROBE_ITEMS:
        return None
    return next(
        (
            item
            for item in streams
            if isinstance(item, Mapping) and item.get("codec_type") == codec_type
        ),
        None,
    )


def _items(document: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    raw = document.get(key)
    if not isinstance(raw, list):
        return []
    if len(raw) > MAX_PROBE_ITEMS:
        raise ValueError(f"{key} exceeds {MAX_PROBE_ITEMS} items")
    return [item for item in raw if isinstance(item, Mapping)]


def parse_probe_json(raw: bytes) -> Mapping[str, Any]:
    """Parse one bounded ffprobe JSON document."""

    if len(raw) > MAX_PROBE_JSON_BYTES:
        raise ValueError(f"probe JSON exceeds {MAX_PROBE_JSON_BYTES} bytes")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("probe output is not valid UTF-8 JSON") from error
    if not isinstance(value, Mapping):
        raise ValueError("probe JSON root must be an object")
    for key in ("streams", "packets", "frames", "packets_and_frames"):
        raw_items = value.get(key)
        if isinstance(raw_items, list) and len(raw_items) > MAX_PROBE_ITEMS:
            raise ValueError(f"{key} exceeds {MAX_PROBE_ITEMS} items")
    return value


def _hex_payload(data: object) -> bytes | None:
    if not isinstance(data, str):
        return None
    pieces: list[str] = []
    for line in data.splitlines():
        payload = line.split(":", 1)[1] if ":" in line else line
        payload = payload.split("  ", 1)[0]
        pieces.extend(
            part
            for part in payload.split()
            if all(char in "0123456789abcdefABCDEF" for char in part)
        )
    encoded = "".join(pieces)
    if not encoded or len(encoded) % 2:
        return None
    try:
        return bytes.fromhex(encoded)
    except ValueError:
        return None


def _contains_h264_idr(payload: bytes) -> bool:
    # Annex-B byte stream.
    for marker in (b"\x00\x00\x01", b"\x00\x00\x00\x01"):
        position = 0
        while (index := payload.find(marker, position)) >= 0:
            nal_index = index + len(marker)
            if nal_index < len(payload) and payload[nal_index] & 0x1F == 5:
                return True
            position = nal_index
    # AVC length-prefixed sample (the common MP4 representation).
    offset = 0
    while offset + 4 <= len(payload):
        length = int.from_bytes(payload[offset : offset + 4], "big")
        offset += 4
        if length <= 0 or offset + length > len(payload):
            break
        if payload[offset] & 0x1F == 5:
            return True
        offset += length
    return False


def _first_video_item(document: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    items = _items(document, key)
    if not isinstance(document.get(key), list):
        item_type = {"packets": "packet", "frames": "frame"}.get(key)
        items = [
            item for item in _items(document, "packets_and_frames") if item.get("type") == item_type
        ]
    return next(
        (
            item
            for item in items
            if item.get("media_type") == "video" or item.get("codec_type") == "video"
        ),
        None,
    )


def _duration(document: Mapping[str, Any], video: Mapping[str, Any] | None) -> float | None:
    format_value = document.get("format")
    if isinstance(format_value, Mapping):
        value = _number(format_value.get("duration"))
        if value is not None:
            return value
    return _number(video.get("duration")) if video else None


def _bitrate(
    document: Mapping[str, Any], video: Mapping[str, Any] | None, duration: float | None
) -> float | None:
    if video:
        value = _number(video.get("bit_rate"))
        if value is not None:
            return value
    format_value = document.get("format")
    if isinstance(format_value, Mapping):
        value = _number(format_value.get("bit_rate"))
        if value is not None:
            return value
        size = _number(format_value.get("size"))
        if size is not None and duration and duration > 0:
            return size * 8 / duration
    return None


def _stream_interval(stream: Mapping[str, Any]) -> tuple[float, float] | None:
    start = _number(stream.get("start_time"))
    duration = _number(stream.get("duration"))
    if start is None or duration is None or duration < 0:
        return None
    return start, start + duration


def analyze_probe_document(
    document: Mapping[str, Any],
    *,
    media_path: str,
    decoder_result: CommandResult | None,
    thresholds: MediaThresholds = DEFAULT_MEDIA_THRESHOLDS,
    timeline: TimelineEvidence | None = None,
    idr_document: Mapping[str, Any] | None = None,
) -> MediaValidation:
    """Analyze bounded ffprobe evidence and an independent decoder result."""

    checks: list[Check] = []
    video = _stream(document, "video")
    audio = _stream(document, "audio")
    checks.append(
        Check(
            "video_codec_h264",
            Outcome.PASS if video and video.get("codec_name") == "h264" else Outcome.FAIL,
            "video stream must use H.264",
            video.get("codec_name") if video else None,
            "h264",
        )
    )
    if audio is None:
        checks.append(
            Check("audio_codec_aac", Outcome.NOT_APPLICABLE, "no audio stream was declared")
        )
    else:
        checks.append(
            Check(
                "audio_codec_aac",
                Outcome.PASS if audio.get("codec_name") == "aac" else Outcome.FAIL,
                "present audio stream must use AAC",
                audio.get("codec_name"),
                "aac",
            )
        )

    if decoder_result is None:
        checks.append(
            Check(
                "decoder_run",
                Outcome.INDETERMINATE,
                "no independent decoder evidence was supplied",
            )
        )
    else:
        decoder_ok = (
            decoder_result.returncode == 0
            and not decoder_result.timed_out
            and not decoder_result.output_truncated
        )
        checks.append(
            Check(
                "decoder_run",
                Outcome.PASS if decoder_ok else Outcome.FAIL,
                "independent decoder must consume the selected video stream without errors",
                decoder_result.returncode,
                0,
            )
        )

    first_packet = _first_video_item(document, "packets")
    packet_key = bool(first_packet and "K" in str(first_packet.get("flags", "")))
    checks.append(
        Check(
            "first_video_packet_keyframe",
            Outcome.PASS if packet_key else Outcome.FAIL,
            "first video packet must be marked as a keyframe",
            packet_key,
            True,
        )
    )
    first_frame = _first_video_item(document, "frames")
    frame_key_value = _number(first_frame.get("key_frame")) if first_frame else None
    frame_key = frame_key_value == 1
    checks.append(
        Check(
            "first_video_frame_keyframe",
            Outcome.PASS if frame_key else Outcome.FAIL,
            "first probed video frame must be marked as a keyframe",
            frame_key,
            True,
        )
    )
    idr_packet = (
        _first_video_item(idr_document, "packets") if idr_document is not None else first_packet
    )
    payload = _hex_payload(idr_packet.get("data")) if idr_packet else None
    if payload is None:
        checks.append(
            Check(
                "first_video_packet_idr",
                Outcome.INDETERMINATE,
                "first-packet NAL data was absent; keyframe evidence does not prove IDR",
            )
        )
    else:
        is_idr = _contains_h264_idr(payload)
        checks.append(
            Check(
                "first_video_packet_idr",
                Outcome.PASS if is_idr else Outcome.FAIL,
                "first video packet must contain an H.264 IDR NAL unit",
                is_idr,
                True,
            )
        )

    duration = _duration(document, video)
    duration_ok = (
        duration is not None
        and abs(duration - thresholds.nominal_duration_seconds)
        <= thresholds.duration_tolerance_seconds
    )
    checks.append(
        Check(
            "duration",
            Outcome.PASS if duration_ok else Outcome.FAIL,
            "clip duration must be within the configured tolerance",
            duration,
            (
                f"{thresholds.nominal_duration_seconds - thresholds.duration_tolerance_seconds}"
                f"..{thresholds.nominal_duration_seconds + thresholds.duration_tolerance_seconds}"
            ),
        )
    )
    bitrate = _bitrate(document, video, duration)
    minimum = thresholds.target_video_bitrate_bps * (1 - thresholds.bitrate_tolerance_fraction)
    maximum = thresholds.target_video_bitrate_bps * (1 + thresholds.bitrate_tolerance_fraction)
    bitrate_ok = bitrate is not None and minimum <= bitrate <= maximum
    checks.append(
        Check(
            "video_bitrate",
            Outcome.PASS if bitrate_ok else Outcome.FAIL,
            "measured video bitrate must be within the documented tolerance",
            round(bitrate) if bitrate is not None else None,
            f"{minimum:.0f}..{maximum:.0f}",
        )
    )

    if audio is None:
        checks.append(Check("av_skew", Outcome.NOT_APPLICABLE, "video-only clip"))
    else:
        video_interval = _stream_interval(video) if video else None
        audio_interval = _stream_interval(audio)
        if video_interval is None or audio_interval is None:
            checks.append(
                Check(
                    "av_skew",
                    Outcome.INDETERMINATE,
                    "stream start/duration data is insufficient to calculate A/V skew",
                )
            )
        else:
            skew = max(
                abs(video_interval[0] - audio_interval[0]),
                abs(video_interval[1] - audio_interval[1]),
            )
            checks.append(
                Check(
                    "av_skew",
                    Outcome.PASS if skew <= thresholds.maximum_av_skew_seconds else Outcome.FAIL,
                    "maximum stream-edge A/V skew must stay within the configured limit",
                    skew,
                    thresholds.maximum_av_skew_seconds,
                )
            )

    decisive = [check for check in checks if check.outcome is not Outcome.NOT_APPLICABLE]
    if any(check.outcome is Outcome.FAIL for check in decisive):
        overall = Outcome.FAIL
    elif any(check.outcome is Outcome.INDETERMINATE for check in decisive):
        overall = Outcome.INDETERMINATE
    else:
        overall = Outcome.PASS
    return MediaValidation(1, media_path, overall, tuple(checks), timeline)


def validate_boundaries(
    validations: Sequence[MediaValidation], *, frame_rate: float = 30.0
) -> tuple[BoundaryValidation, ...]:
    """Compare adjacent clips on their shared monotonic timeline, never raw MP4 PTS."""

    if frame_rate <= 0:
        raise ValueError("frame_rate must be positive")
    frame_period = 1.0 / frame_rate
    results: list[BoundaryValidation] = []
    for previous, following in pairwise(validations):
        if previous.timeline is None or following.timeline is None:
            results.append(
                BoundaryValidation(
                    previous.media_path,
                    following.media_path,
                    0.0,
                    frame_period,
                    Outcome.INDETERMINATE,
                    "missing_monotonic_timeline",
                )
            )
            continue
        delta = (
            following.timeline.start_monotonic_ns - previous.timeline.end_monotonic_ns
        ) / 1_000_000_000
        results.append(
            BoundaryValidation(
                previous.media_path,
                following.media_path,
                delta,
                frame_period,
                Outcome.PASS if abs(delta) <= frame_period else Outcome.FAIL,
                "continuous"
                if abs(delta) <= frame_period
                else ("gap_exceeds_one_frame" if delta > 0 else "overlap_exceeds_one_frame"),
            )
        )
    return tuple(results)


def probe_media_file(
    media_path: Path,
    *,
    runner: FixedArgvRunner = run_fixed_argv,
    thresholds: MediaThresholds = DEFAULT_MEDIA_THRESHOLDS,
    timeline: TimelineEvidence | None = None,
    timeout_seconds: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    max_output_bytes: int = MAX_COMMAND_OUTPUT_BYTES,
) -> MediaValidation:
    """Run fixed ffprobe/ffmpeg argv and validate one explicit regular file."""

    if (
        not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= DEFAULT_COMMAND_TIMEOUT_SECONDS
        or not 0 < max_output_bytes <= MAX_COMMAND_OUTPUT_BYTES
    ):
        raise ValueError("media command bounds exceed validated maxima")
    path = media_path.resolve(strict=True)
    if not path.is_file():
        raise ValueError("media input must be a regular file")
    probe_argv = (
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        "-show_packets",
        "-show_frames",
        "-show_entries",
        (
            "format=duration,bit_rate,size:"
            "stream=codec_type,codec_name,start_time,duration,bit_rate:"
            "packet=codec_type,flags:"
            "frame=media_type,key_frame"
        ),
        str(path),
    )
    probe = runner(probe_argv, timeout_seconds=timeout_seconds, max_output_bytes=max_output_bytes)
    if probe.returncode != 0 or probe.timed_out or probe.output_truncated:
        check = Check(
            "probe_run",
            Outcome.FAIL,
            "ffprobe must finish successfully with complete bounded output",
            probe.returncode,
            0,
        )
        return MediaValidation(1, str(path), Outcome.FAIL, (check,), timeline)
    try:
        document = parse_probe_json(probe.stdout)
    except ValueError as error:
        check = Check("probe_json", Outcome.FAIL, str(error))
        return MediaValidation(1, str(path), Outcome.FAIL, (check,), timeline)
    idr_argv = (
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-select_streams",
        "v:0",
        "-read_intervals",
        "%+#1",
        "-show_packets",
        "-show_data",
        "-show_entries",
        "packet=codec_type,flags,data",
        str(path),
    )
    idr_probe = runner(
        idr_argv,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
    )
    idr_document: Mapping[str, Any] | None = None
    if idr_probe.returncode == 0 and not idr_probe.timed_out and not idr_probe.output_truncated:
        try:
            idr_document = parse_probe_json(idr_probe.stdout)
        except ValueError:
            idr_document = None
    decode_argv = (
        "ffmpeg",
        "-v",
        "error",
        "-xerror",
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-f",
        "null",
        "-",
    )
    decoder = runner(
        decode_argv, timeout_seconds=timeout_seconds, max_output_bytes=max_output_bytes
    )
    return analyze_probe_document(
        document,
        media_path=str(path),
        decoder_result=decoder,
        thresholds=thresholds,
        timeline=timeline,
        idr_document=idr_document,
    )
