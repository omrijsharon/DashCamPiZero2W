#!/usr/bin/env python3
"""Hash-closed, read-only Milestone 7 A/V clip acceptance harness."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Final, cast
from uuid import UUID

from dashcam.diagnostics.media import CommandResult, run_fixed_argv
from dashcam.metadata.reconcile import parse_sidecar_bytes
from dashcam.metadata.schema import ClipSidecar

RECORDING_ROOT: Final = Path("/srv/dashcam")
CLIPS_ROOT: Final = RECORDING_ROOT / "clips"
PENDING_ROOT: Final = RECORDING_ROOT / "pending"
FFPROBE: Final = "/usr/bin/ffprobe"
FFMPEG: Final = "/usr/bin/ffmpeg"
FRAME_RATE: Final = 30.0
FRAME_PERIOD_NS: Final = round(1_000_000_000 / FRAME_RATE)
MINIMUM_COUNT: Final = 3
MAXIMUM_COUNT: Final = 10
MAX_SIDECAR_BYTES: Final = 1024 * 1024
MAX_MANIFEST_BYTES: Final = 4096
MAX_COMMAND_OUTPUT_BYTES: Final = 128 * 1024
MAX_IDR_OUTPUT_BYTES: Final = 8 * 1024 * 1024
MAX_RESULT_BYTES: Final = 2 * 1024 * 1024
MAX_FILE_HASH_BYTES: Final = 512 * 1024 * 1024
SHA256_RE: Final = re.compile(r"[0-9a-f]{64}")
SHORT_BOOT_RE: Final = re.compile(r"[0-9a-f]{12}")
MANIFEST_MEMBERS: Final = ("README.md", "run.py")
VIDEO_BITRATE_MIN_BPS: Final = 6_000_000
VIDEO_BITRATE_MAX_BPS: Final = 10_000_000
AUDIO_BITRATE_BPS: Final = 128_000
AUDIO_BITRATE_TOLERANCE_BPS: Final = 6_400
MAX_AV_SKEW_SECONDS: Final = 0.100


class HarnessError(RuntimeError):
    """The reviewed acceptance contract could not be proved."""


@dataclass(frozen=True)
class StreamEvidence:
    codec_name: str
    profile: str
    width: int | None
    height: int | None
    sample_rate: int | None
    channels: int | None
    start_time: float
    duration: float
    bit_rate: int
    frame_rate: str | None


def _bounded_regular_bytes(path: Path, maximum: int) -> bytes:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise HarnessError(f"{path} is not a bounded regular file")
        payload = bytearray()
        while chunk := os.read(descriptor, min(65536, maximum + 1 - len(payload))):
            payload.extend(chunk)
            if len(payload) > maximum:
                raise HarnessError(f"{path} exceeded its read bound")
        return bytes(payload)
    finally:
        os.close(descriptor)


def _sha256_file(path: Path, *, maximum: int) -> str:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    total = 0
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise HarnessError(f"{path} is not a regular file")
        while chunk := os.read(descriptor, 1024 * 1024):
            total += len(chunk)
            if total > maximum:
                raise HarnessError(f"{path} exceeded its hash bound")
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def verify_manifest(expected_sha256: str, directory: Path | None = None) -> dict[str, str]:
    """Verify the closed two-file harness before any media is read."""

    if SHA256_RE.fullmatch(expected_sha256) is None:
        raise HarnessError("expected manifest SHA-256 is not canonical")
    root = (directory or Path(__file__).resolve().parent).resolve(strict=True)
    manifest = root / "SHA256SUMS"
    if _sha256_file(manifest, maximum=MAX_MANIFEST_BYTES) != expected_sha256:
        raise HarnessError("reviewed manifest hash differs from the supplied hash")
    entries: dict[str, str] = {}
    for line in _bounded_regular_bytes(manifest, MAX_MANIFEST_BYTES).decode("ascii").splitlines():
        digest, separator, name = line.partition("  ")
        if not separator or SHA256_RE.fullmatch(digest) is None or name in entries:
            raise HarnessError("manifest contains an invalid entry")
        if name not in MANIFEST_MEMBERS or Path(name).name != name:
            raise HarnessError("manifest member set is not closed")
        entries[name] = digest
    if tuple(sorted(entries)) != MANIFEST_MEMBERS:
        raise HarnessError("manifest omits a required member")
    for name, digest in entries.items():
        if _sha256_file(root / name, maximum=2 * 1024 * 1024) != digest:
            raise HarnessError(f"manifest member {name} failed verification")
    return entries


def _strict_json_object(payload: bytes, name: str) -> Mapping[str, object]:
    def no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise HarnessError(f"{name} contains a duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(payload, object_pairs_hook=no_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HarnessError(f"{name} is invalid JSON") from error
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise HarnessError(f"{name} is not an object")
    return cast(Mapping[str, object], value)


def _number(value: object, name: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool):
        raise HarnessError(f"{name} is not numeric")
    try:
        number = float(cast(str | int | float, value))
    except (TypeError, ValueError) as error:
        raise HarnessError(f"{name} is not numeric") from error
    if not math.isfinite(number) or number < minimum:
        raise HarnessError(f"{name} is outside its bound")
    return number


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    number = _number(value, name, minimum=float(minimum))
    if not number.is_integer() or number > 1_000_000_000:
        raise HarnessError(f"{name} is not a bounded integer")
    return int(number)


def _short_boot_id(boot_id: UUID) -> str:
    short = boot_id.hex[:12]
    if SHORT_BOOT_RE.fullmatch(short) is None:
        raise AssertionError("UUID-derived short boot ID is invalid")
    return short


def _clip_paths(boot_id: UUID, sequence: int) -> tuple[Path, Path, Path, Path]:
    stem = f"boot-{_short_boot_id(boot_id)}-{sequence:06d}"
    return (
        CLIPS_ROOT / f"{stem}.mp4",
        CLIPS_ROOT / f"{stem}.json",
        PENDING_ROOT / f"{stem}.partial.mp4",
        PENDING_ROOT / f"{stem}.partial.json",
    )


def _checked_clips_root() -> int:
    if Path("/srv/dashcam") != RECORDING_ROOT or Path("/srv/dashcam/clips") != CLIPS_ROOT:
        raise HarnessError("recording root contract differs")
    root = RECORDING_ROOT.resolve(strict=True)
    clips = CLIPS_ROOT.resolve(strict=True)
    pending = PENDING_ROOT.resolve(strict=True)
    if root != RECORDING_ROOT or clips != CLIPS_ROOT or pending != PENDING_ROOT:
        raise HarnessError("recording path resolves outside the exact DASHCAM layout")
    root_info = os.lstat(root)
    if not stat.S_ISDIR(root_info.st_mode):
        raise HarnessError("recording root is not a directory")
    for path in (clips, pending):
        info = os.lstat(path)
        if not stat.S_ISDIR(info.st_mode) or info.st_dev != root_info.st_dev:
            raise HarnessError("managed media directory left the exact recording device")
    return root_info.st_dev


def _load_pair(
    boot_id: UUID, sequence: int, expected_st_dev: int
) -> tuple[ClipSidecar, Path, dict[str, object]]:
    video, sidecar_path, pending_video, pending_sidecar = _clip_paths(boot_id, sequence)
    if pending_video.exists() or pending_sidecar.exists():
        raise HarnessError(f"sequence {sequence:06d} still has a pending member")
    for path in (video, sidecar_path):
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_dev != expected_st_dev:
            raise HarnessError(f"{path} is not a regular file on the recording device")
    sidecar_payload = _bounded_regular_bytes(sidecar_path, MAX_SIDECAR_BYTES)
    sidecar = parse_sidecar_bytes(sidecar_payload)
    if sidecar_payload != sidecar.to_canonical_json():
        raise HarnessError(f"{sidecar_path} is not canonical JSON")
    audio = sidecar.audio
    if (
        sidecar.boot_id != boot_id
        or sidecar.sequence != sequence
        or sidecar.video_file != video.name
        or sidecar.metadata_file != sidecar_path.name
        or sidecar.protected
        or sidecar.video.codec.casefold() != "h264"
        or sidecar.video.width != 1920
        or sidecar.video.height != 1080
        or sidecar.video.fps_nominal != FRAME_RATE
        or sidecar.video.frames_written <= 0
        or audio.available is not True
        or (audio.codec or "").casefold() != "aac"
        or audio.sample_rate_hz != 48_000
        or audio.channels != 1
        or audio.target_bitrate_bps != AUDIO_BITRATE_BPS
    ):
        raise HarnessError(f"sequence {sequence:06d} sidecar is not an ordinary audio clip")
    return (
        sidecar,
        video,
        {
            "sequence": sequence,
            "clip_id": str(sidecar.clip_id),
            "video_path": str(video),
            "video_sha256": _sha256_file(video, maximum=MAX_FILE_HASH_BYTES),
            "video_size_bytes": os.lstat(video).st_size,
            "sidecar_sha256": hashlib.sha256(sidecar_payload).hexdigest(),
            "start_monotonic_ns": sidecar.start_monotonic_ns,
            "end_monotonic_ns": sidecar.end_monotonic_ns,
        },
    )


def _compact_probe(path: Path) -> Mapping[str, object]:
    result = run_fixed_argv(
        (
            FFPROBE,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_entries",
            "stream=codec_type,codec_name,profile,width,height,sample_rate,channels,r_frame_rate,start_time,duration,bit_rate:format=size,duration",
            str(path.resolve(strict=True)),
        ),
        timeout_seconds=30.0,
        max_output_bytes=MAX_COMMAND_OUTPUT_BYTES,
    )
    if result.returncode != 0 or result.timed_out or result.output_truncated:
        raise HarnessError("compact A/V ffprobe command did not complete")
    document = _strict_json_object(result.stdout, "compact A/V ffprobe")
    allowed = {"streams", "format", "programs", "stream_groups"}
    if not {"streams", "format"} <= set(document) or not set(document) <= allowed:
        raise HarnessError("compact A/V ffprobe schema is not closed")
    for name in ("programs", "stream_groups"):
        if name in document and document[name] != []:
            raise HarnessError("compact A/V ffprobe optional list is not empty")
    return document


def _stream(document: Mapping[str, object], codec_type: str) -> Mapping[str, object]:
    streams = document.get("streams")
    if not isinstance(streams, list) or len(streams) != 2:
        raise HarnessError("compact A/V ffprobe must report exactly two streams")
    candidates = [
        item
        for item in streams
        if isinstance(item, Mapping) and item.get("codec_type") == codec_type
    ]
    if len(candidates) != 1:
        raise HarnessError(f"compact A/V ffprobe has no unique {codec_type} stream")
    value = cast(Mapping[str, object], candidates[0])
    expected = {
        "codec_type",
        "codec_name",
        "profile",
        "r_frame_rate",
        "start_time",
        "duration",
        "bit_rate",
    }
    if codec_type == "video":
        expected |= {"width", "height"}
    else:
        expected |= {"sample_rate", "channels"}
    if set(value) != expected:
        raise HarnessError(f"compact {codec_type} stream schema differs")
    return value


def _stream_evidence(document: Mapping[str, object], codec_type: str) -> StreamEvidence:
    value = _stream(document, codec_type)
    codec = value.get("codec_name")
    profile = value.get("profile")
    if not isinstance(codec, str) or not isinstance(profile, str) or not codec or not profile:
        raise HarnessError(f"compact {codec_type} codec/profile is invalid")
    width = _integer(value["width"], "video width", minimum=1) if codec_type == "video" else None
    height = _integer(value["height"], "video height", minimum=1) if codec_type == "video" else None
    sample_rate = (
        _integer(value["sample_rate"], "audio sample rate", minimum=1)
        if codec_type == "audio"
        else None
    )
    channels = (
        _integer(value["channels"], "audio channels", minimum=1) if codec_type == "audio" else None
    )
    frame_rate = value["r_frame_rate"]
    if not isinstance(frame_rate, str):
        raise HarnessError(f"{codec_type} frame rate is invalid")
    return StreamEvidence(
        codec,
        profile,
        width,
        height,
        sample_rate,
        channels,
        _number(value["start_time"], f"{codec_type} start time"),
        _number(value["duration"], f"{codec_type} duration", minimum=0.001),
        _integer(value["bit_rate"], f"{codec_type} bit rate", minimum=1),
        frame_rate,
    )


def _format_evidence(document: Mapping[str, object]) -> tuple[int, float]:
    value = document.get("format")
    if not isinstance(value, Mapping) or set(value) != {"size", "duration"}:
        raise HarnessError("compact A/V format schema differs")
    return _integer(value["size"], "format size", minimum=1), _number(
        value["duration"], "format duration", minimum=0.001
    )


def _first_packet_idr(path: Path) -> None:
    result = run_fixed_argv(
        (
            FFPROBE,
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
            str(path.resolve(strict=True)),
        ),
        timeout_seconds=30.0,
        max_output_bytes=MAX_IDR_OUTPUT_BYTES,
    )
    if result.returncode != 0 or result.timed_out or result.output_truncated:
        raise HarnessError("first-video-packet IDR probe did not complete")
    document = _strict_json_object(result.stdout, "first-video-packet IDR probe")
    packets = document.get("packets")
    if set(document) != {"packets"} or not isinstance(packets, list) or len(packets) != 1:
        raise HarnessError("first-video-packet IDR probe schema differs")
    packet = packets[0]
    if not isinstance(packet, Mapping) or set(packet) != {"codec_type", "flags", "data"}:
        raise HarnessError("first-video-packet IDR packet schema differs")
    if (
        packet.get("codec_type") != "video"
        or not isinstance(packet.get("flags"), str)
        or "K" not in cast(str, packet["flags"])
    ):
        raise HarnessError("first video packet is not a key packet")
    data = packet.get("data")
    if not isinstance(data, str) or not _contains_h264_idr(data):
        raise HarnessError("first video packet does not contain an H.264 IDR")


def _contains_h264_idr(data: str) -> bool:
    words: list[str] = []
    for line in data.splitlines():
        payload = line.split(":", 1)[1] if ":" in line else line
        words.extend(word for word in payload.split() if re.fullmatch(r"[0-9A-Fa-f]{2,8}", word))
    try:
        raw = bytes.fromhex("".join(words))
    except ValueError:
        return False
    for marker in (b"\x00\x00\x01", b"\x00\x00\x00\x01"):
        offset = 0
        while (index := raw.find(marker, offset)) >= 0:
            position = index + len(marker)
            if position < len(raw) and raw[position] & 0x1F == 5:
                return True
            offset = position
    offset = 0
    while offset + 4 <= len(raw):
        size = int.from_bytes(raw[offset : offset + 4], "big")
        offset += 4
        if size <= 0 or offset + size > len(raw):
            return False
        if raw[offset] & 0x1F == 5:
            return True
        offset += size
    return False


def _decode(path: Path) -> CommandResult:
    return run_fixed_argv(
        (
            FFMPEG,
            "-v",
            "error",
            "-xerror",
            "-c:v",
            "h264_v4l2m2m",
            "-i",
            str(path.resolve(strict=True)),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-f",
            "null",
            "-",
        ),
        timeout_seconds=120.0,
        max_output_bytes=MAX_COMMAND_OUTPUT_BYTES,
    )


def _validate_streams(document: Mapping[str, object]) -> dict[str, object]:
    video = _stream_evidence(document, "video")
    audio = _stream_evidence(document, "audio")
    size, duration = _format_evidence(document)
    video_end = video.start_time + video.duration
    audio_end = audio.start_time + audio.duration
    skew = max(abs(video.start_time - audio.start_time), abs(video_end - audio_end))
    checks = {
        "video_h264_high_1080p30": video.codec_name == "h264"
        and video.profile.casefold() == "high"
        and video.width == 1920
        and video.height == 1080
        and video.frame_rate == "30/1",
        "audio_aac_lc_48k_mono": audio.codec_name == "aac"
        and audio.profile.casefold() == "lc"
        and audio.sample_rate == 48_000
        and audio.channels == 1,
        "audio_ffprobe_rate_is_not_a_video_rate": audio.frame_rate == "0/0",
        "duration_59_to_61_seconds": 59.0 <= duration <= 61.0
        and 59.0 <= video.duration <= 61.0
        and 59.0 <= audio.duration <= 61.0,
        "video_bitrate_6_to_10_mbps": VIDEO_BITRATE_MIN_BPS
        <= video.bit_rate
        <= VIDEO_BITRATE_MAX_BPS,
        "aac_bitrate_128kbps_source_tolerance": abs(audio.bit_rate - AUDIO_BITRATE_BPS)
        <= AUDIO_BITRATE_TOLERANCE_BPS,
        "maximum_stream_edge_av_skew_100ms": skew <= MAX_AV_SKEW_SECONDS,
    }
    return {
        "video": {
            "codec": video.codec_name,
            "profile": video.profile,
            "width": video.width,
            "height": video.height,
            "frame_rate": video.frame_rate,
            "start_time": video.start_time,
            "duration": video.duration,
            "bit_rate": video.bit_rate,
        },
        "audio": {
            "codec": audio.codec_name,
            "profile": audio.profile,
            "sample_rate": audio.sample_rate,
            "channels": audio.channels,
            "frame_rate": audio.frame_rate,
            "start_time": audio.start_time,
            "duration": audio.duration,
            "bit_rate": audio.bit_rate,
        },
        "format": {"size": size, "duration": duration},
        "maximum_stream_edge_av_skew_seconds": skew,
        "checks": checks,
    }


def validate_media(
    boot_id: UUID, start_sequence: int, count: int = MINIMUM_COUNT
) -> dict[str, object]:
    if not 0 <= start_sequence <= 999_999 or not MINIMUM_COUNT <= count <= MAXIMUM_COUNT:
        raise HarnessError("clip sequence/count range is invalid")
    if start_sequence + count - 1 > 999_999:
        raise HarnessError("clip range exceeds supported sequence space")
    device = _checked_clips_root()
    pairs: list[dict[str, object]] = []
    sidecars: list[ClipSidecar] = []
    stream_passes: list[bool] = []
    for sequence in range(start_sequence, start_sequence + count):
        sidecar, video, evidence = _load_pair(boot_id, sequence, device)
        stream_evidence = _validate_streams(_compact_probe(video))
        checks = cast(Mapping[str, bool], stream_evidence["checks"])
        _first_packet_idr(video)
        decoded = _decode(video)
        decode_pass = (
            decoded.returncode == 0 and not decoded.timed_out and not decoded.output_truncated
        )
        evidence["streams"] = stream_evidence
        evidence["first_video_packet_idr"] = True
        evidence["independent_h264_v4l2m2m_av_decode"] = decode_pass
        pairs.append(evidence)
        sidecars.append(sidecar)
        stream_passes.append(all(checks.values()))
    boundaries = [
        {
            "previous_sequence": previous.sequence,
            "next_sequence": current.sequence,
            "delta_ns": current.start_monotonic_ns - previous.end_monotonic_ns,
            "within_one_frame": abs(current.start_monotonic_ns - previous.end_monotonic_ns)
            <= FRAME_PERIOD_NS,
        }
        for previous, current in pairwise(sidecars)
    ]
    stream_pass = all(stream_passes)
    decode_pass = all(pair["independent_h264_v4l2m2m_av_decode"] is True for pair in pairs)
    boundaries_pass = all(item["within_one_frame"] is True for item in boundaries)
    passed = stream_pass and decode_pass and boundaries_pass
    return {
        "schema_version": 1,
        "phase": "audio_video_media",
        "passed": passed,
        "recording_root": RECORDING_ROOT.as_posix(),
        "boot_id": str(boot_id),
        "start_sequence": start_sequence,
        "clip_count": count,
        "pairs": pairs,
        "boundaries": boundaries,
        "checks": {
            "all_stream_contracts": stream_pass,
            "all_idr_and_independent_av_decodes": decode_pass,
            "all_sidecar_boundaries_within_one_frame": boundaries_pass,
        },
    }


def _write_exclusive_json(path: Path, value: Mapping[str, object]) -> None:
    if not path.is_absolute() or path == RECORDING_ROOT or RECORDING_ROOT in path.parents:
        raise HarnessError("output must be an absolute path outside /srv/dashcam")
    if path.exists() or path.is_symlink() or not path.parent.is_dir():
        raise HarnessError("output path must be a new file below an existing directory")
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(payload) > MAX_RESULT_BYTES:
        raise HarnessError("result exceeds its bounded output size")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        os.unlink(temporary)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="milestone7-audio")
    parser.add_argument("--expected-manifest-sha256", required=True)
    subparsers = parser.add_subparsers(dest="phase", required=True)
    media = subparsers.add_parser("validate-media")
    media.add_argument("--boot-id", type=UUID, required=True)
    media.add_argument("--start-sequence", type=int, required=True)
    media.add_argument("--count", type=int, default=MINIMUM_COUNT)
    media.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        verify_manifest(arguments.expected_manifest_sha256)
        result = validate_media(arguments.boot_id, arguments.start_sequence, arguments.count)
        _write_exclusive_json(arguments.output, result)
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "phase": arguments.phase,
                    "passed": result["passed"],
                    "output": str(arguments.output),
                    "output_sha256": _sha256_file(arguments.output, maximum=MAX_RESULT_BYTES),
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            flush=True,
        )
        return 0 if result["passed"] is True else 1
    except (HarnessError, OSError, ValueError) as error:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "phase": getattr(arguments, "phase", None),
                    "passed": False,
                    "error": f"{type(error).__name__}: {error}"[:512],
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            flush=True,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
