#!/usr/bin/env python3
"""Hash-closed, bounded Milestone 6 media and endurance acceptance harness."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any, Final, Protocol, cast
from uuid import UUID

from dashcam.diagnostics.endurance import (
    EnduranceOutcome,
    EnduranceSample,
    EnduranceThresholds,
    analyze_samples,
)
from dashcam.diagnostics.media import (
    CommandResult,
    MediaThresholds,
    MediaValidation,
    Outcome,
    TimelineEvidence,
    analyze_probe_document,
    parse_probe_json,
    run_fixed_argv,
    validate_boundaries,
)
from dashcam.health.platform import FactState, parse_throttle_status
from dashcam.metadata.reconcile import parse_sidecar_bytes
from dashcam.metadata.schema import ClipSidecar

RECORDING_ROOT: Final = Path("/srv/dashcam")
CLIPS_ROOT: Final = RECORDING_ROOT / "clips"
PENDING_ROOT: Final = RECORDING_ROOT / "pending"
STATUS_PATH: Final = Path("/run/dashcam/status.json")
TEMPERATURE_PATH: Final = Path("/sys/class/thermal/thermal_zone0/temp")
MEMINFO_PATH: Final = Path("/proc/meminfo")
SWAPS_PATH: Final = Path("/proc/swaps")
VCGENCMD: Final = "/usr/bin/vcgencmd"
FFPROBE: Final = "/usr/bin/ffprobe"
FFMPEG: Final = "/usr/bin/ffmpeg"
MEDIA_COUNT: Final = 10
FRAME_RATE: Final = 30.0
FRAME_PERIOD_NS: Final = round(1_000_000_000 / FRAME_RATE)
TARGET_BITRATE_BPS: Final = 8_000_000
BITRATE_TOLERANCE: Final = 0.25
ENDURANCE_DURATION_S: Final = 7_200.0
ENDURANCE_INTERVAL_S: Final = 10.0
ENDURANCE_SAMPLE_COUNT: Final = 720
MAX_JSON_BYTES: Final = 8 * 1024 * 1024
MAX_STATUS_BYTES: Final = 64 * 1024
MAX_SIDECAR_BYTES: Final = 1024 * 1024
MAX_TEXT_BYTES: Final = 64 * 1024
MAX_INTEGER: Final = 2**63 - 1
SHA256_RE: Final = re.compile(r"[0-9a-f]{64}")
SHORT_BOOT_RE: Final = re.compile(r"[0-9a-f]{12}")
MANIFEST_MEMBERS: Final = ("README.md", "run.py")
COMPACT_BITRATE_REQUIRED_TOP_LEVEL_KEYS: Final = frozenset({"streams", "format"})
COMPACT_BITRATE_OPTIONAL_EMPTY_LIST_KEYS: Final = frozenset(
    {"programs", "stream_groups"}
)
LIFECYCLE_KEYS: Final = frozenset(
    {
        "state",
        "reason",
        "detail",
        "sequence",
        "config_schema_version",
        "notification_failures",
    }
)
RUNTIME_KEYS: Final = frozenset(
    {
        "video",
        "frames",
        "pipeline_restart_count",
        "last_clip",
        "storage_preflight",
    }
)
VIDEO_KEYS: Final = frozenset(
    {
        "width",
        "height",
        "frames_per_second",
        "codec",
        "hardware_encoded",
        "effective_caps",
        "configured",
        "encoder_identity",
    }
)
EFFECTIVE_CAPS_KEYS: Final = frozenset(
    {"raw_format", "fps_numerator", "fps_denominator", "h264_profile", "h264_level"}
)
CONFIGURED_VIDEO_KEYS: Final = frozenset(
    {"target_bitrate_bps", "keyframe_interval_frames"}
)
ENCODER_IDENTITY_KEYS: Final = frozenset(
    {"factory_name", "factory_class", "device_path"}
)
FRAME_KEYS: Final = frozenset({"raw", "encoded", "dropped", "drop_source"})
LAST_CLIP_REQUIRED_KEYS: Final = frozenset(
    {"sequence", "duration_ns", "bitrate_bps", "frames"}
)
LAST_CLIP_FRAME_KEYS: Final = frozenset({"raw", "encoded", "dropped"})
STORAGE_KEYS: Final = frozenset(
    {"state", "reasons", "ready", "mount", "free_bytes", "capacity_bytes"}
)
MOUNT_KEYS: Final = frozenset(
    {
        "target",
        "mounted",
        "filesystem",
        "label",
        "uuid_suffix",
        "device_id",
        "read_write",
    }
)
ACCEPTANCE_SAMPLE_KEYS: Final = frozenset(
    {
        "monotonic_ns",
        "recorder_status",
        "rss_bytes",
        "system_used_memory_bytes",
        "memory_available_bytes",
        "swap_used_bytes",
        "cpu_percent",
        "temperature_c",
        "throttled",
        "undervoltage",
        "filesystem_free_bytes",
        "raw_frames",
        "encoded_frames",
        "dropped_frames",
        "clip_sequence",
        "bitrate_bps",
        "restart_count",
    }
)
ENDURANCE_RESULT_KEYS: Final = frozenset(
    {
        "schema_version",
        "phase",
        "passed",
        "started_monotonic_ns",
        "ended_monotonic_ns",
        "elapsed_seconds",
        "sample_count",
        "required_sample_count",
        "required_duration_seconds",
        "samples",
        "diagnostic_analysis",
        "checks",
    }
)
ENDURANCE_RESULT_KEYS_WITH_SWAP_POLICY: Final = ENDURANCE_RESULT_KEYS | {"swap_policy"}
LEGACY_ENDURANCE_CHECK_KEYS: Final = frozenset(
    {
        "all_required_samples_present",
        "counter_ordered",
        "frame_shapes_valid",
        "frame_counter_alignment_valid",
        "maximum_frame_counter_delta",
        "maximum_accepted_frame_counter_delta",
        "minimum_clip_advance",
        "observed_clip_advance",
        "clip_progress",
        "filesystem_free_bytes_positive",
    }
)
ENDURANCE_CHECK_KEYS: Final = LEGACY_ENDURANCE_CHECK_KEYS | frozenset(
    {
        "swap_no_growth",
        "swap_baseline_bytes",
        "maximum_swap_used_bytes",
        "swap_growth_above_baseline_bytes",
    }
)
DIAGNOSTIC_RESULT_KEYS: Final = frozenset(
    {
        "schema_version",
        "outcome",
        "sample_count",
        "started_monotonic_ns",
        "ended_monotonic_ns",
        "checks",
        "samples",
    }
)
DIAGNOSTIC_CHECK_KEYS: Final = frozenset(
    {"code", "outcome", "observed", "limit", "summary"}
)
DIAGNOSTIC_CHECK_CODES: Final = frozenset(
    {
        "evidence_completeness",
        "rss_growth",
        "available_memory",
        "swap_used",
        "cpu",
        "temperature",
        "throttling",
        "undervoltage",
        "dropped_frames",
        "service_restarts",
        "bitrate",
    }
)


class HarnessError(RuntimeError):
    """A closed acceptance contract was not met."""


class MediaProbe(Protocol):
    def __call__(
        self,
        media_path: Path,
        *,
        thresholds: MediaThresholds,
        timeline: TimelineEvidence | None,
    ) -> MediaValidation: ...


class BitrateProbe(Protocol):
    def __call__(self, media_path: Path) -> int: ...


@dataclass(frozen=True, slots=True)
class StatusCounters:
    lifecycle_state: str
    lifecycle_reason: str | None
    lifecycle_sequence: int
    raw_frames: int
    encoded_frames: int
    dropped_frames: int
    pipeline_restart_count: int
    clip_sequence: int
    clip_duration_ns: int
    bitrate_bps: int
    storage_free_bytes: int
    storage_capacity_bytes: int
    storage_device_id: str


@dataclass(frozen=True, slots=True)
class ProcessReading:
    ticks: int
    start_ticks: int
    rss_bytes: int


@dataclass(frozen=True, slots=True)
class MemoryReading:
    total_bytes: int
    available_bytes: int
    used_bytes: int
    swap_used_bytes: int


@dataclass(frozen=True, slots=True)
class SwapPolicy:
    """One exact zram-only swap policy observation."""

    source: str
    source_sha256: str
    device: str
    kind: str
    size_bytes: int
    used_bytes: int
    priority: int

    def __post_init__(self) -> None:
        if (
            self.source != SWAPS_PATH.as_posix()
            or SHA256_RE.fullmatch(self.source_sha256) is None
            or self.device != "/dev/zram0"
            or self.kind != "partition"
            or isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or not 0 < self.size_bytes <= MAX_INTEGER
            or isinstance(self.used_bytes, bool)
            or not isinstance(self.used_bytes, int)
            or not 0 <= self.used_bytes <= self.size_bytes
            or isinstance(self.priority, bool)
            or not isinstance(self.priority, int)
            or not -32_768 <= self.priority <= 32_767
        ):
            raise ValueError("swap policy is not exactly one bounded zram0 partition")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AcceptanceSample:
    monotonic_ns: int
    recorder_status: Mapping[str, object]
    rss_bytes: int
    system_used_memory_bytes: int
    memory_available_bytes: int
    swap_used_bytes: int
    cpu_percent: float
    temperature_c: float
    throttled: bool
    undervoltage: bool
    filesystem_free_bytes: int
    raw_frames: int
    encoded_frames: int
    dropped_frames: int
    clip_sequence: int
    bitrate_bps: int
    restart_count: int

    def __post_init__(self) -> None:
        integer_fields = (
            self.monotonic_ns,
            self.rss_bytes,
            self.system_used_memory_bytes,
            self.memory_available_bytes,
            self.swap_used_bytes,
            self.filesystem_free_bytes,
            self.raw_frames,
            self.encoded_frames,
            self.dropped_frames,
            self.clip_sequence,
            self.bitrate_bps,
            self.restart_count,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= MAX_INTEGER
            for value in integer_fields
        ):
            raise ValueError("acceptance sample integer fields must be bounded and non-negative")
        if (
            isinstance(self.cpu_percent, bool)
            or not isinstance(self.cpu_percent, int | float)
            or not math.isfinite(self.cpu_percent)
            or not 0 <= self.cpu_percent <= 1_000
        ):
            raise ValueError("acceptance sample CPU percentage is invalid")
        if (
            isinstance(self.temperature_c, bool)
            or not isinstance(self.temperature_c, int | float)
            or not math.isfinite(self.temperature_c)
            or not -100 <= self.temperature_c <= 250
        ):
            raise ValueError("acceptance sample temperature is invalid")
        if not isinstance(self.throttled, bool) or not isinstance(self.undervoltage, bool):
            raise ValueError("acceptance sample throttle fields must be boolean")

    def endurance_sample(self) -> EnduranceSample:
        return EnduranceSample(
            monotonic_ns=self.monotonic_ns,
            rss_bytes=self.rss_bytes,
            memory_available_bytes=self.memory_available_bytes,
            swap_used_bytes=self.swap_used_bytes,
            cpu_percent=self.cpu_percent,
            temperature_c=self.temperature_c,
            throttled=self.throttled,
            undervoltage=self.undervoltage,
            dropped_frames=self.dropped_frames,
            bitrate_bps=self.bitrate_bps,
            restart_count=self.restart_count,
        )


def _bounded_regular_bytes(path: Path, maximum: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise HarnessError(f"{path} is not a bounded regular file")
        retained = bytearray()
        while len(retained) <= maximum:
            chunk = os.read(descriptor, min(65_536, maximum + 1 - len(retained)))
            if not chunk:
                break
            retained.extend(chunk)
        if len(retained) > maximum:
            raise HarnessError(f"{path} exceeded its read bound")
        return bytes(retained)
    finally:
        os.close(descriptor)


def _sha256_file(path: Path, *, maximum: int | None = None) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    total = 0
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise HarnessError(f"{path} is not a regular file")
        while chunk := os.read(descriptor, 1024 * 1024):
            total += len(chunk)
            if maximum is not None and total > maximum:
                raise HarnessError(f"{path} exceeded its hash bound")
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def verify_manifest(expected_sha256: str, directory: Path | None = None) -> dict[str, str]:
    if SHA256_RE.fullmatch(expected_sha256) is None:
        raise HarnessError("expected manifest SHA-256 is not canonical")
    root = (directory or Path(__file__).resolve().parent).resolve(strict=True)
    manifest_path = root / "SHA256SUMS"
    if _sha256_file(manifest_path, maximum=4096) != expected_sha256:
        raise HarnessError("reviewed manifest hash differs from the supplied hash")
    payload = _bounded_regular_bytes(manifest_path, 4096)
    entries: dict[str, str] = {}
    for line in payload.decode("ascii").splitlines():
        pieces = line.split("  ")
        if len(pieces) != 2 or SHA256_RE.fullmatch(pieces[0]) is None:
            raise HarnessError("manifest contains an invalid entry")
        digest, name = pieces
        if name in entries or name not in MANIFEST_MEMBERS:
            raise HarnessError("manifest member set is not closed")
        entries[name] = digest
    if tuple(sorted(entries)) != tuple(sorted(MANIFEST_MEMBERS)):
        raise HarnessError("manifest omits a required member")
    for name, expected in entries.items():
        member = root / name
        if member.parent != root or _sha256_file(member, maximum=2 * 1024 * 1024) != expected:
            raise HarnessError(f"manifest member {name} failed verification")
    return entries


def _exact_int(value: object, name: str, *, minimum: int = 0) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= MAX_INTEGER
    ):
        raise HarnessError(f"{name} must be a bounded integer")
    return value


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or not value.isprintable()
    ):
        raise HarnessError(f"{name} must be null or bounded printable text")
    return value


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise HarnessError(f"{name} must be an object")
    return cast(Mapping[str, object], value)


def _strict_json_object(payload: bytes, name: str) -> Mapping[str, object]:
    def closed_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise HarnessError(f"{name} contains a duplicate key")
            result[key] = value
        return result

    try:
        decoded = json.loads(
            payload,
            object_pairs_hook=closed_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                HarnessError(f"{name} contains non-finite {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HarnessError(f"{name} is invalid JSON") from error
    return _mapping(decoded, name)


def _parse_frames(value: object) -> tuple[Mapping[str, object], tuple[int, int, int]]:
    frames = _mapping(value, "runtime.frames")
    if frozenset(frames) != FRAME_KEYS:
        raise HarnessError("runtime.frames schema is not closed")
    if _optional_text(frames["drop_source"], "runtime.frames.drop_source") is None:
        raise HarnessError("runtime frame drop source is unavailable")
    values = tuple(
        _exact_int(frames[name], f"runtime.frames.{name}")
        for name in ("raw", "encoded", "dropped")
    )
    return frames, cast(tuple[int, int, int], values)


def _parse_effective_video(value: object) -> None:
    video = _mapping(value, "runtime.video")
    if frozenset(video) != VIDEO_KEYS:
        raise HarnessError("runtime.video schema is not closed")
    if (
        _exact_int(video["width"], "runtime.video.width", minimum=1) != 1920
        or _exact_int(video["height"], "runtime.video.height", minimum=1) != 1080
        or _exact_int(
            video["frames_per_second"],
            "runtime.video.frames_per_second",
            minimum=1,
        )
        != 30
        or video["codec"] != "h264"
        or video["hardware_encoded"] is not True
    ):
        raise HarnessError("runtime effective video profile is not production")
    caps = _mapping(video["effective_caps"], "runtime.video.effective_caps")
    if frozenset(caps) != EFFECTIVE_CAPS_KEYS:
        raise HarnessError("runtime effective caps schema is not closed")
    if (
        caps["raw_format"] != "NV12"
        or _exact_int(caps["fps_numerator"], "effective fps numerator", minimum=1) != 30
        or _exact_int(caps["fps_denominator"], "effective fps denominator", minimum=1) != 1
        or caps["h264_profile"] != "high"
        or caps["h264_level"] != "4.1"
    ):
        raise HarnessError("runtime negotiated caps are not production")
    configured = _mapping(video["configured"], "runtime.video.configured")
    if frozenset(configured) != CONFIGURED_VIDEO_KEYS:
        raise HarnessError("runtime configured video schema is not closed")
    if (
        _exact_int(configured["target_bitrate_bps"], "configured bitrate", minimum=1)
        != TARGET_BITRATE_BPS
        or _exact_int(
            configured["keyframe_interval_frames"],
            "configured keyframe interval",
            minimum=1,
        )
        != 30
    ):
        raise HarnessError("runtime configured video controls are not production")
    identity = _mapping(video["encoder_identity"], "runtime.video.encoder_identity")
    if frozenset(identity) != ENCODER_IDENTITY_KEYS:
        raise HarnessError("runtime encoder identity schema is not closed")
    factory_name = _optional_text(identity["factory_name"], "encoder factory name")
    factory_class = _optional_text(identity["factory_class"], "encoder factory class")
    device_path = _optional_text(identity["device_path"], "encoder device path")
    if (
        factory_name != "v4l2h264enc"
        or factory_class is None
        or "hardware" not in {part.casefold() for part in factory_class.split("/")}
        or device_path is None
        or re.fullmatch(r"/dev/video[0-9]+", device_path) is None
    ):
        raise HarnessError("runtime encoder identity is not the selected hardware encoder")


def parse_status_snapshot(payload: bytes) -> tuple[Mapping[str, object], StatusCounters]:
    if not payload or len(payload) > MAX_STATUS_BYTES:
        raise HarnessError("recorder status snapshot is empty or oversized")
    document = _strict_json_object(payload, "recorder status snapshot")
    if frozenset(document) != {"schema_version", "lifecycle", "runtime"}:
        raise HarnessError("status snapshot top-level schema is not closed")
    if _exact_int(document["schema_version"], "schema_version", minimum=1) != 1:
        raise HarnessError("unsupported status snapshot schema")

    lifecycle = _mapping(document["lifecycle"], "lifecycle")
    if frozenset(lifecycle) != LIFECYCLE_KEYS:
        raise HarnessError("lifecycle status schema is not closed")
    state = _optional_text(lifecycle["state"], "lifecycle.state")
    if state is None:
        raise HarnessError("lifecycle state is absent")
    reason = _optional_text(lifecycle["reason"], "lifecycle.reason")
    _optional_text(lifecycle["detail"], "lifecycle.detail")
    lifecycle_sequence = _exact_int(lifecycle["sequence"], "lifecycle.sequence")
    _exact_int(
        lifecycle["config_schema_version"],
        "lifecycle.config_schema_version",
        minimum=1,
    )
    _exact_int(lifecycle["notification_failures"], "lifecycle.notification_failures")

    runtime = _mapping(document["runtime"], "runtime")
    if frozenset(runtime) != RUNTIME_KEYS:
        raise HarnessError("runtime status schema is not closed")
    _parse_effective_video(runtime["video"])
    _, frame_values = _parse_frames(runtime["frames"])
    restart_count = _exact_int(
        runtime["pipeline_restart_count"],
        "runtime.pipeline_restart_count",
    )

    last_clip = _mapping(runtime["last_clip"], "runtime.last_clip")
    last_keys = frozenset(last_clip)
    if (
        last_keys != LAST_CLIP_REQUIRED_KEYS
    ):
        raise HarnessError("runtime.last_clip has missing or unknown fields")
    clip_sequence = _exact_int(last_clip["sequence"], "runtime.last_clip.sequence")
    duration_ns = _exact_int(
        last_clip["duration_ns"],
        "runtime.last_clip.duration_ns",
        minimum=1,
    )
    bitrate_bps = _exact_int(
        last_clip["bitrate_bps"],
        "runtime.last_clip.bitrate_bps",
        minimum=1,
    )
    clip_frames = _mapping(last_clip["frames"], "runtime.last_clip.frames")
    if frozenset(clip_frames) != LAST_CLIP_FRAME_KEYS:
        raise HarnessError("runtime.last_clip.frames schema is not closed")
    for name in LAST_CLIP_FRAME_KEYS:
        _exact_int(clip_frames[name], f"runtime.last_clip.frames.{name}")

    storage = _mapping(runtime["storage_preflight"], "runtime.storage_preflight")
    if frozenset(storage) != STORAGE_KEYS:
        raise HarnessError("runtime.storage_preflight schema is not closed")
    storage_state = _optional_text(storage["state"], "runtime.storage_preflight.state")
    reasons = storage["reasons"]
    if (
        not isinstance(reasons, list)
        or len(reasons) > 32
        or any(_optional_text(item, "storage reason") is None for item in reasons)
    ):
        raise HarnessError("runtime.storage_preflight.reasons is invalid")
    if storage["ready"] is not True or storage_state != "READY" or reasons:
        raise HarnessError("runtime storage preflight is not READY")
    mount = _mapping(storage["mount"], "runtime.storage_preflight.mount")
    if frozenset(mount) != MOUNT_KEYS:
        raise HarnessError("runtime storage mount schema is not closed")
    device_id = _optional_text(mount["device_id"], "storage mount device identity")
    if (
        mount["target"] != RECORDING_ROOT.as_posix()
        or mount["mounted"] is not True
        or mount["filesystem"] != "exfat"
        or mount["label"] != "DASHCAM"
        or mount["uuid_suffix"] != "3EA7"
        or mount["read_write"] is not True
        or device_id is None
        or re.fullmatch(r"[0-9]{1,10}:[0-9]{1,10}", device_id) is None
    ):
        raise HarnessError("runtime storage mount is not the exact writable DASHCAM volume")
    free_bytes = _exact_int(storage["free_bytes"], "storage.free_bytes", minimum=1)
    capacity_bytes = _exact_int(
        storage["capacity_bytes"],
        "storage.capacity_bytes",
        minimum=1,
    )
    if free_bytes > capacity_bytes:
        raise HarnessError("storage free bytes exceed capacity")

    counters = StatusCounters(
        state,
        reason,
        lifecycle_sequence,
        frame_values[0],
        frame_values[1],
        frame_values[2],
        restart_count,
        clip_sequence,
        duration_ns,
        bitrate_bps,
        free_bytes,
        capacity_bytes,
        device_id,
    )
    return document, counters


def _short_boot_id(boot_id: UUID) -> str:
    result = boot_id.hex[:12]
    if SHORT_BOOT_RE.fullmatch(result) is None:
        raise AssertionError("UUID-derived short boot ID is invalid")
    return result


def _clip_paths(boot_id: UUID, sequence: int) -> tuple[Path, Path, Path, Path]:
    short = _short_boot_id(boot_id)
    stem = f"boot-{short}-{sequence:06d}"
    return (
        CLIPS_ROOT / f"{stem}.mp4",
        CLIPS_ROOT / f"{stem}.json",
        PENDING_ROOT / f"{stem}.partial.mp4",
        PENDING_ROOT / f"{stem}.partial.json",
    )


def _checked_recording_root() -> int:
    resolved = RECORDING_ROOT.resolve(strict=True)
    if resolved != RECORDING_ROOT or not CLIPS_ROOT.is_dir() or not PENDING_ROOT.is_dir():
        raise HarnessError("the exact recording root layout is unavailable")
    info = os.lstat(RECORDING_ROOT)
    if not stat.S_ISDIR(info.st_mode):
        raise HarnessError("recording root is not a directory")
    for directory in (CLIPS_ROOT, PENDING_ROOT):
        child = os.lstat(directory)
        if not stat.S_ISDIR(child.st_mode) or child.st_dev != info.st_dev:
            raise HarnessError("managed directory left the exact recording device")
    return info.st_dev


def _load_pair(
    boot_id: UUID,
    sequence: int,
    expected_st_dev: int,
) -> tuple[ClipSidecar, Path, dict[str, object]]:
    video_path, sidecar_path, pending_video, pending_sidecar = _clip_paths(boot_id, sequence)
    if pending_video.exists() or pending_sidecar.exists():
        raise HarnessError(f"sequence {sequence:06d} still has a pending member")
    for path in (video_path, sidecar_path):
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_dev != expected_st_dev:
            raise HarnessError(f"{path} is not a regular file on the recording device")
    payload = _bounded_regular_bytes(sidecar_path, MAX_SIDECAR_BYTES)
    sidecar = parse_sidecar_bytes(payload)
    if payload != sidecar.to_canonical_json():
        raise HarnessError(f"{sidecar_path} is not canonical JSON")
    if (
        sidecar.boot_id != boot_id
        or sidecar.sequence != sequence
        or sidecar.video_file != video_path.name
        or sidecar.metadata_file != sidecar_path.name
        or sidecar.protected
        or sidecar.video.codec.casefold() != "h264"
        or sidecar.video.width != 1920
        or sidecar.video.height != 1080
        or sidecar.video.fps_nominal != FRAME_RATE
        or sidecar.video.frames_written <= 0
    ):
        raise HarnessError(f"sequence {sequence:06d} sidecar is not an ordinary M6 clip")
    evidence = {
        "sequence": sequence,
        "video_path": str(video_path),
        "video_sha256": _sha256_file(video_path),
        "video_size_bytes": os.lstat(video_path).st_size,
        "sidecar_path": str(sidecar_path),
        "sidecar_sha256": hashlib.sha256(payload).hexdigest(),
        "clip_id": str(sidecar.clip_id),
        "start_monotonic_ns": sidecar.start_monotonic_ns,
        "end_monotonic_ns": sidecar.end_monotonic_ns,
        "frames_written": sidecar.video.frames_written,
        "dropped_frames": sidecar.video.dropped_frames,
        "sidecar_bitrate_bps": sidecar.video.measured_bitrate_bps,
    }
    return sidecar, video_path, evidence


def _production_probe(
    media_path: Path,
    *,
    thresholds: MediaThresholds,
    timeline: TimelineEvidence | None,
) -> MediaValidation:
    path = media_path.resolve(strict=True)
    compact = run_fixed_argv(
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
        ),
        timeout_seconds=30.0,
        max_output_bytes=MAX_STATUS_BYTES,
    )
    if compact.returncode != 0 or compact.timed_out or compact.output_truncated:
        raise HarnessError("compact first-item media probe did not complete")
    try:
        document = parse_probe_json(compact.stdout)
    except ValueError as error:
        raise HarnessError("compact first-item media probe JSON is invalid") from error

    idr = run_fixed_argv(
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
            str(path),
        ),
        timeout_seconds=30.0,
        max_output_bytes=MAX_JSON_BYTES,
    )
    idr_document: Mapping[str, Any] | None = None
    if idr.returncode == 0 and not idr.timed_out and not idr.output_truncated:
        try:
            idr_document = parse_probe_json(idr.stdout)
        except ValueError:
            idr_document = None

    decoder: CommandResult = run_fixed_argv(
        (
            FFMPEG,
            "-v",
            "error",
            "-xerror",
            "-c:v",
            "h264_v4l2m2m",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-f",
            "null",
            "-",
        ),
        timeout_seconds=120.0,
        max_output_bytes=MAX_STATUS_BYTES,
    )
    return analyze_probe_document(
        document,
        media_path=str(path),
        decoder_result=decoder,
        thresholds=thresholds,
        timeline=timeline,
        idr_document=idr_document,
    )


def parse_compact_bitrate_probe(payload: bytes) -> int:
    """Parse one bounded ffprobe bitrate result with explicit fallbacks."""

    if not payload or len(payload) > MAX_STATUS_BYTES:
        raise HarnessError("compact bitrate probe output is empty or oversized")
    document = _strict_json_object(payload, "compact bitrate probe")
    top_level_keys = frozenset(document)
    if not (
        top_level_keys >= COMPACT_BITRATE_REQUIRED_TOP_LEVEL_KEYS
        and top_level_keys
        <= COMPACT_BITRATE_REQUIRED_TOP_LEVEL_KEYS
        | COMPACT_BITRATE_OPTIONAL_EMPTY_LIST_KEYS
    ):
        raise HarnessError("compact bitrate probe schema is not closed")
    for name in COMPACT_BITRATE_OPTIONAL_EMPTY_LIST_KEYS:
        if name in document and document[name] != []:
            raise HarnessError("compact bitrate probe optional list is not empty")
    streams = document["streams"]
    if not isinstance(streams, list) or not 1 <= len(streams) <= 8:
        raise HarnessError("compact bitrate probe stream list is invalid")
    stream_bitrate: float | None = None
    for index, value in enumerate(streams):
        stream = _mapping(value, f"compact bitrate stream {index}")
        if frozenset(stream) - {"codec_type", "bit_rate"}:
            raise HarnessError("compact bitrate stream schema is not closed")
        if stream.get("codec_type") != "video":
            continue
        raw_bitrate = stream.get("bit_rate")
        try:
            candidate = float(cast(str | int | float, raw_bitrate))
        except (TypeError, ValueError):
            candidate = math.nan
        if math.isfinite(candidate) and candidate > 0:
            stream_bitrate = candidate
            break
    format_value = _mapping(document["format"], "compact bitrate format")
    if frozenset(format_value) - {"bit_rate", "size", "duration"}:
        raise HarnessError("compact bitrate format schema is not closed")

    measured = stream_bitrate
    if measured is None:
        try:
            candidate = float(
                cast(str | int | float, format_value.get("bit_rate"))
            )
        except (TypeError, ValueError):
            candidate = math.nan
        if math.isfinite(candidate) and candidate > 0:
            measured = candidate
    if measured is None:
        try:
            size = float(cast(str | int | float, format_value.get("size")))
            duration = float(cast(str | int | float, format_value.get("duration")))
        except (TypeError, ValueError):
            size = math.nan
            duration = math.nan
        if (
            math.isfinite(size)
            and math.isfinite(duration)
            and size > 0
            and duration > 0
        ):
            measured = size * 8 / duration
    if measured is None or not math.isfinite(measured):
        raise HarnessError("compact ffprobe result has no measured video bitrate")
    rounded = round(measured)
    if not 1 <= rounded <= 1_000_000_000:
        raise HarnessError("compact ffprobe video bitrate is outside its bound")
    return rounded


def _production_bitrate_probe(media_path: Path) -> int:
    result = run_fixed_argv(
        (
            FFPROBE,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_entries",
            "stream=codec_type,bit_rate:format=bit_rate,size,duration",
            str(media_path),
        ),
        timeout_seconds=10.0,
        max_output_bytes=MAX_STATUS_BYTES,
    )
    if result.returncode != 0 or result.timed_out or result.output_truncated:
        raise HarnessError("compact ffprobe bitrate command did not complete")
    return parse_compact_bitrate_probe(result.stdout)


def _check_without_bitrate(validation: MediaValidation) -> bool:
    decisive = (
        check
        for check in validation.checks
        if check.code != "video_bitrate" and check.outcome is not Outcome.NOT_APPLICABLE
    )
    return all(check.outcome is Outcome.PASS for check in decisive)


def validate_media_acceptance(
    boot_id: UUID,
    start_sequence: int,
    *,
    probe: MediaProbe = _production_probe,
    bitrate_probe: BitrateProbe = _production_bitrate_probe,
) -> dict[str, object]:
    if not 0 <= start_sequence <= 999_999 - (MEDIA_COUNT - 1):
        raise HarnessError("ten-clip sequence range is invalid")
    expected_st_dev = _checked_recording_root()
    thresholds = MediaThresholds(
        nominal_duration_seconds=60.0,
        duration_tolerance_seconds=1.0,
        target_video_bitrate_bps=TARGET_BITRATE_BPS,
        # The product gate is aggregate bitrate. Individual results remain
        # visible, but do not silently redefine that gate as per-clip.
        bitrate_tolerance_fraction=0.99,
        frame_rate=FRAME_RATE,
    )
    validations: list[MediaValidation] = []
    pair_evidence: list[dict[str, object]] = []
    measured_bitrates: list[int] = []
    for sequence in range(start_sequence, start_sequence + MEDIA_COUNT):
        sidecar, video_path, evidence = _load_pair(boot_id, sequence, expected_st_dev)
        timeline = TimelineEvidence(
            sidecar.start_monotonic_ns,
            sidecar.end_monotonic_ns,
        )
        validation = probe(video_path, thresholds=thresholds, timeline=timeline)
        measured_bitrate = bitrate_probe(video_path)
        if (
            isinstance(measured_bitrate, bool)
            or not isinstance(measured_bitrate, int)
            or not 1 <= measured_bitrate <= 1_000_000_000
        ):
            raise HarnessError(
                f"sequence {sequence:06d} has no bounded compact-probe bitrate"
            )
        evidence["compact_probe_bitrate_bps"] = measured_bitrate
        measured_bitrates.append(measured_bitrate)
        validations.append(validation)
        pair_evidence.append(evidence)

    boundaries = validate_boundaries(validations, frame_rate=FRAME_RATE)
    average_bitrate = round(sum(measured_bitrates) / len(measured_bitrates))
    minimum_bitrate = round(TARGET_BITRATE_BPS * (1 - BITRATE_TOLERANCE))
    maximum_bitrate = round(TARGET_BITRATE_BPS * (1 + BITRATE_TOLERANCE))
    media_pass = all(_check_without_bitrate(item) for item in validations)
    boundaries_pass = all(
        item.outcome is Outcome.PASS
        and abs(round(item.delta_seconds * 1_000_000_000)) <= FRAME_PERIOD_NS
        for item in boundaries
    )
    bitrate_pass = minimum_bitrate <= average_bitrate <= maximum_bitrate
    passed = media_pass and boundaries_pass and bitrate_pass
    return {
        "schema_version": 1,
        "phase": "ten_clip_media",
        "passed": passed,
        "recording_root": RECORDING_ROOT.as_posix(),
        "boot_id": str(boot_id),
        "start_sequence": start_sequence,
        "clip_count": MEDIA_COUNT,
        "pairs": pair_evidence,
        "media": [item.to_dict() for item in validations],
        "boundaries": [item.to_dict() for item in boundaries],
        "average_bitrate_bps": average_bitrate,
        "accepted_bitrate_range_bps": [minimum_bitrate, maximum_bitrate],
        "checks": {
            "all_h264_decode_idr_duration": media_pass,
            "all_boundaries_within_one_frame": boundaries_pass,
            "aggregate_bitrate_within_tolerance": bitrate_pass,
        },
    }


def _parse_kib_lines(payload: bytes, required: set[str]) -> dict[str, int]:
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as error:
        raise HarnessError("proc text is not ASCII") from error
    values: dict[str, int] = {}
    for line in text.splitlines():
        pieces = line.split()
        if len(pieces) != 3 or not pieces[0].endswith(":") or pieces[2] != "kB":
            continue
        key = pieces[0][:-1]
        if key not in required:
            continue
        if key in values or not pieces[1].isdecimal():
            raise HarnessError(f"proc field {key} is invalid")
        value = int(pieces[1]) * 1024
        if value > MAX_INTEGER:
            raise HarnessError(f"proc field {key} is oversized")
        values[key] = value
    if set(values) != required:
        raise HarnessError("proc text omits required fields")
    return values


def read_memory(path: Path = MEMINFO_PATH) -> MemoryReading:
    values = _parse_kib_lines(
        _bounded_regular_bytes(path, MAX_TEXT_BYTES),
        {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"},
    )
    if values["MemAvailable"] > values["MemTotal"] or values["SwapFree"] > values["SwapTotal"]:
        raise HarnessError("memory facts are inconsistent")
    return MemoryReading(
        values["MemTotal"],
        values["MemAvailable"],
        values["MemTotal"] - values["MemAvailable"],
        values["SwapTotal"] - values["SwapFree"],
    )


def parse_swap_policy(payload: bytes, *, source: Path = SWAPS_PATH) -> SwapPolicy:
    """Require the exact Pi policy: one zram0 partition and no other swap."""

    if not payload or len(payload) > MAX_TEXT_BYTES:
        raise HarnessError("swap policy observation is empty or oversized")
    try:
        lines = payload.decode("ascii").splitlines()
    except UnicodeDecodeError as error:
        raise HarnessError("swap policy observation is not ASCII") from error
    if len(lines) != 2 or lines[0].split() != [
        "Filename",
        "Type",
        "Size",
        "Used",
        "Priority",
    ]:
        raise HarnessError("swap policy schema is not exact")
    fields = lines[1].split()
    if (
        len(fields) != 5
        or fields[0] != "/dev/zram0"
        or fields[1] != "partition"
        or not fields[2].isdecimal()
        or not fields[3].isdecimal()
        or re.fullmatch(r"-?[0-9]+", fields[4]) is None
    ):
        raise HarnessError("swap policy must contain only the zram0 partition")
    size_kib = int(fields[2])
    used_kib = int(fields[3])
    priority = int(fields[4])
    if (
        size_kib < 1
        or used_kib > size_kib
        or size_kib > MAX_INTEGER // 1024
        or not -32_768 <= priority <= 32_767
    ):
        raise HarnessError("zram0 swap policy values are outside their bounds")
    return SwapPolicy(
        source.as_posix(),
        hashlib.sha256(payload).hexdigest(),
        fields[0],
        fields[1],
        size_kib * 1024,
        used_kib * 1024,
        priority,
    )


def read_swap_policy(path: Path = SWAPS_PATH) -> SwapPolicy:
    return parse_swap_policy(
        _bounded_regular_bytes(path, MAX_TEXT_BYTES),
        source=path,
    )


def _same_swap_policy_shape(first: SwapPolicy, second: SwapPolicy) -> bool:
    return (
        first.source == second.source
        and first.device == second.device
        and first.kind == second.kind
        and first.size_bytes == second.size_bytes
        and first.priority == second.priority
    )


def read_process(pid: int, proc_root: Path = Path("/proc")) -> ProcessReading:
    if isinstance(pid, bool) or not isinstance(pid, int) or not 1 <= pid <= 2**31 - 1:
        raise HarnessError("PID is invalid")
    stat_payload = _bounded_regular_bytes(proc_root / str(pid) / "stat", 4096)
    try:
        stat_text = stat_payload.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise HarnessError("process stat is not ASCII") from error
    close = stat_text.rfind(")")
    if close < 2:
        raise HarnessError("process stat shape is invalid")
    fields = stat_text[close + 2 :].split()
    if len(fields) < 22:
        raise HarnessError("process stat omits required fields")
    try:
        ticks = int(fields[11]) + int(fields[12])
        start_ticks = int(fields[19])
    except ValueError as error:
        raise HarnessError("process stat counters are invalid") from error
    if not 0 <= ticks <= MAX_INTEGER or not 1 <= start_ticks <= MAX_INTEGER:
        raise HarnessError("process stat counters are outside their accepted range")
    status = _parse_kib_lines(
        _bounded_regular_bytes(proc_root / str(pid) / "status", MAX_TEXT_BYTES),
        {"VmRSS"},
    )
    return ProcessReading(ticks, start_ticks, status["VmRSS"])


def read_temperature(path: Path = TEMPERATURE_PATH) -> float:
    payload = _bounded_regular_bytes(path, 64).decode("ascii").strip()
    try:
        value = float(payload)
    except ValueError as error:
        raise HarnessError("CPU temperature is invalid") from error
    if abs(value) > 250:
        value /= 1000.0
    if not math.isfinite(value) or not -100 <= value <= 250:
        raise HarnessError("CPU temperature is outside its accepted range")
    return value


def read_throttle() -> tuple[bool, bool]:
    result = run_fixed_argv(
        (VCGENCMD, "get_throttled"),
        timeout_seconds=2.0,
        max_output_bytes=4096,
    )
    if result.returncode != 0 or result.timed_out or result.output_truncated:
        raise HarnessError("vcgencmd throttle observation failed")
    throttled, undervoltage = parse_throttle_status(result.stdout)
    if (
        throttled.state is not FactState.AVAILABLE
        or undervoltage.state is not FactState.AVAILABLE
        or throttled.value is None
        or undervoltage.value is None
    ):
        raise HarnessError("vcgencmd throttle observation is malformed")
    match = re.fullmatch(rb"throttled=0x([0-9a-fA-F]{1,8})\s*", result.stdout)
    if match is None:
        raise HarnessError("vcgencmd throttle observation is malformed")
    bits = int(match.group(1), 16)
    # Include both current and latched-since-boot bits. A transient event
    # between ten-second samples must not disappear from endurance evidence.
    return bool(bits & (0x4 | 0x40000)), bool(bits & (0x1 | 0x10000))


class LiveSampleSource:
    """Read-only exact-Pi source with PID-reuse and mount-drift refusal."""

    def __init__(
        self,
        pid: int,
        *,
        clock_ticks: int | None = None,
        proc_root: Path = Path("/proc"),
        status_path: Path = STATUS_PATH,
        recording_root: Path = RECORDING_ROOT,
        process_reader: Callable[[int, Path], ProcessReading] = read_process,
        memory_reader: Callable[[Path], MemoryReading] = read_memory,
        temperature_reader: Callable[[Path], float] = read_temperature,
        throttle_reader: Callable[[], tuple[bool, bool]] = read_throttle,
    ) -> None:
        sysconf = cast(Callable[[str], int], getattr(os, "sysconf", None))
        if clock_ticks is None and not callable(sysconf):
            raise HarnessError("system clock tick observation is unavailable")
        ticks = sysconf("SC_CLK_TCK") if clock_ticks is None else clock_ticks
        if isinstance(ticks, bool) or not isinstance(ticks, int) or not 1 <= ticks <= 1_000_000:
            raise HarnessError("system clock tick rate is invalid")
        self._pid = pid
        self._clock_ticks = ticks
        self._proc_root = proc_root
        self._status_path = status_path
        self._recording_root = recording_root
        self._process_reader = process_reader
        self._memory_reader = memory_reader
        self._temperature_reader = temperature_reader
        self._throttle_reader = throttle_reader
        self._recording_device = os.lstat(recording_root).st_dev
        self._previous_process: ProcessReading | None = None
        self._previous_monotonic_ns: int | None = None

    def prime(self, monotonic_ns: int) -> None:
        self._previous_process = self._process_reader(self._pid, self._proc_root)
        self._previous_monotonic_ns = monotonic_ns

    def sample(self, monotonic_ns: int) -> AcceptanceSample:
        previous = self._previous_process
        previous_time = self._previous_monotonic_ns
        if previous is None or previous_time is None or monotonic_ns <= previous_time:
            raise HarnessError("sample source was not primed with an earlier monotonic time")
        process = self._process_reader(self._pid, self._proc_root)
        if process.start_ticks != previous.start_ticks or process.ticks < previous.ticks:
            raise HarnessError("recorder PID changed identity or CPU counters regressed")
        elapsed_ns = monotonic_ns - previous_time
        cpu_percent = (
            (process.ticks - previous.ticks)
            * 100.0
            * 1_000_000_000
            / (self._clock_ticks * elapsed_ns)
        )
        memory = self._memory_reader(MEMINFO_PATH)
        temperature = self._temperature_reader(TEMPERATURE_PATH)
        throttled, undervoltage = self._throttle_reader()
        status_document, counters = parse_status_snapshot(
            _bounded_regular_bytes(self._status_path, MAX_STATUS_BYTES)
        )
        if counters.lifecycle_state != "RECORDING" or counters.lifecycle_reason is not None:
            raise HarnessError("recorder status is not healthy RECORDING")
        root_info = os.lstat(self._recording_root)
        if not stat.S_ISDIR(root_info.st_mode) or root_info.st_dev != self._recording_device:
            raise HarnessError("recording root device changed during collection")
        device_major = cast(Callable[[int], int], getattr(os, "major", None))
        device_minor = cast(Callable[[int], int], getattr(os, "minor", None))
        if not callable(device_major) or not callable(device_minor):
            raise HarnessError("filesystem device identity observation is unavailable")
        observed_device_id = (
            f"{device_major(root_info.st_dev)}:{device_minor(root_info.st_dev)}"
        )
        if observed_device_id != counters.storage_device_id:
            raise HarnessError("recording root device disagrees with recorder status")
        statvfs = cast(Callable[[Path], Any], getattr(os, "statvfs", None))
        if not callable(statvfs):
            raise HarnessError("filesystem space observation is unavailable")
        space = statvfs(self._recording_root)
        free_bytes = space.f_bavail * space.f_frsize
        if not 0 < free_bytes <= MAX_INTEGER:
            raise HarnessError("filesystem free-byte observation is invalid")
        self._previous_process = process
        self._previous_monotonic_ns = monotonic_ns
        return AcceptanceSample(
            monotonic_ns,
            status_document,
            process.rss_bytes,
            memory.used_bytes,
            memory.available_bytes,
            memory.swap_used_bytes,
            cpu_percent,
            temperature,
            throttled,
            undervoltage,
            free_bytes,
            counters.raw_frames,
            counters.encoded_frames,
            counters.dropped_frames,
            counters.clip_sequence,
            counters.bitrate_bps,
            counters.pipeline_restart_count,
        )


def collect_endurance(
    source: LiveSampleSource,
    *,
    monotonic_ns: Callable[[], int] = time.monotonic_ns,
    sleep: Callable[[float], None] = time.sleep,
    sample_count: int = ENDURANCE_SAMPLE_COUNT,
    interval_s: float = ENDURANCE_INTERVAL_S,
) -> tuple[int, int, tuple[AcceptanceSample, ...]]:
    if (
        isinstance(sample_count, bool)
        or not isinstance(sample_count, int)
        or not 2 <= sample_count <= ENDURANCE_SAMPLE_COUNT
        or isinstance(interval_s, bool)
        or not isinstance(interval_s, int | float)
        or not math.isfinite(interval_s)
        or not 0 < interval_s <= ENDURANCE_INTERVAL_S
    ):
        raise HarnessError("endurance collection bounds are invalid")
    started = monotonic_ns()
    source.prime(started)
    samples: list[AcceptanceSample] = []
    for index in range(1, sample_count + 1):
        target = started + round(index * interval_s * 1_000_000_000)
        remaining = (target - monotonic_ns()) / 1_000_000_000
        if remaining > 0:
            sleep(remaining)
        samples.append(source.sample(monotonic_ns()))
    ended = monotonic_ns()
    return started, ended, tuple(samples)


def analyze_endurance_acceptance(
    started_ns: int,
    ended_ns: int,
    samples: Sequence[AcceptanceSample],
    *,
    swap_policy: SwapPolicy,
    required_duration_s: float = ENDURANCE_DURATION_S,
    required_samples: int = ENDURANCE_SAMPLE_COUNT,
) -> dict[str, object]:
    if not isinstance(swap_policy, SwapPolicy):
        raise HarnessError("endurance acceptance requires exact zram-only policy evidence")
    if (
        isinstance(started_ns, bool)
        or isinstance(ended_ns, bool)
        or not isinstance(started_ns, int)
        or not isinstance(ended_ns, int)
        or ended_ns <= started_ns
    ):
        raise HarnessError("endurance collection interval is invalid")
    if len(samples) != required_samples:
        raise HarnessError("endurance sample count is incomplete")
    elapsed_s = (ended_ns - started_ns) / 1_000_000_000
    if elapsed_s < required_duration_s:
        raise HarnessError("endurance collection did not span its required duration")
    swap_baseline = samples[0].swap_used_bytes
    maximum_swap_used = max(sample.swap_used_bytes for sample in samples)
    swap_growth = maximum_swap_used - swap_baseline
    swap_no_growth = swap_growth <= 0
    report = analyze_samples(
        tuple(sample.endurance_sample() for sample in samples),
        EnduranceThresholds(
            maximum_samples=required_samples,
            maximum_temperature_c=80.0,
            maximum_cpu_percent=100.0,
            maximum_rss_growth_bytes=32 * 1024 * 1024,
            minimum_available_memory_bytes=32 * 1024 * 1024,
            maximum_swap_used_bytes=swap_baseline,
            maximum_dropped_frame_increase=0,
            maximum_restart_increase=0,
            target_bitrate_bps=TARGET_BITRATE_BPS,
            bitrate_tolerance_fraction=BITRATE_TOLERANCE,
        ),
    )
    counter_ordered = all(
        following.raw_frames >= previous.raw_frames
        and following.encoded_frames >= previous.encoded_frames
        and following.clip_sequence >= previous.clip_sequence
        for previous, following in pairwise(samples)
    )
    frame_counter_deltas = tuple(
        abs(sample.raw_frames - sample.encoded_frames) for sample in samples
    )
    maximum_frame_counter_delta = max(frame_counter_deltas)
    # The probes are attached independently to the encoder sink and source.
    # Their initial attachment order and one in-flight encoder buffer can put
    # either counter ahead by one without a drop. Larger divergence is not an
    # accepted observation.
    frame_counter_alignment_valid = maximum_frame_counter_delta <= 1
    minimum_clip_advance = max(1, math.floor(required_duration_s / 60) - 2)
    clip_advance = samples[-1].clip_sequence - samples[0].clip_sequence
    clip_progress = clip_advance >= minimum_clip_advance
    passed = (
        report.outcome is EnduranceOutcome.PASS
        and swap_no_growth
        and counter_ordered
        and frame_counter_alignment_valid
        and clip_progress
        and all(sample.filesystem_free_bytes > 0 for sample in samples)
    )
    return {
        "schema_version": 1,
        "phase": "two_hour_endurance",
        "passed": passed,
        "started_monotonic_ns": started_ns,
        "ended_monotonic_ns": ended_ns,
        "elapsed_seconds": elapsed_s,
        "sample_count": len(samples),
        "required_sample_count": required_samples,
        "required_duration_seconds": required_duration_s,
        "swap_policy": swap_policy.to_dict(),
        "samples": [asdict(sample) for sample in samples],
        "diagnostic_analysis": report.to_dict(),
        "checks": {
            "all_required_samples_present": True,
            "counter_ordered": counter_ordered,
            "frame_shapes_valid": frame_counter_alignment_valid,
            "frame_counter_alignment_valid": frame_counter_alignment_valid,
            "maximum_frame_counter_delta": maximum_frame_counter_delta,
            "maximum_accepted_frame_counter_delta": 1,
            "minimum_clip_advance": minimum_clip_advance,
            "observed_clip_advance": clip_advance,
            "clip_progress": clip_progress,
            "filesystem_free_bytes_positive": all(
                sample.filesystem_free_bytes > 0 for sample in samples
            ),
            "swap_no_growth": swap_no_growth,
            "swap_baseline_bytes": swap_baseline,
            "maximum_swap_used_bytes": maximum_swap_used,
            "swap_growth_above_baseline_bytes": max(swap_growth, 0),
        },
    }


def _exact_number(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
    ):
        raise HarnessError(f"{name} must be a finite number")
    return float(value)


def _parse_retained_sample(value: object, index: int) -> AcceptanceSample:
    item = _mapping(value, f"source sample {index}")
    if frozenset(item) != ACCEPTANCE_SAMPLE_KEYS:
        raise HarnessError(f"source sample {index} schema is not closed")
    recorder_status = _mapping(
        item["recorder_status"],
        f"source sample {index} recorder_status",
    )
    status_payload = json.dumps(
        recorder_status,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    _, counters = parse_status_snapshot(status_payload)
    if counters.lifecycle_state != "RECORDING" or counters.lifecycle_reason is not None:
        raise HarnessError(f"source sample {index} is not healthy RECORDING")
    throttled = item["throttled"]
    undervoltage = item["undervoltage"]
    if not isinstance(throttled, bool) or not isinstance(undervoltage, bool):
        raise HarnessError(f"source sample {index} throttle fields are not boolean")
    try:
        sample = AcceptanceSample(
            monotonic_ns=_exact_int(
                item["monotonic_ns"],
                f"source sample {index} monotonic_ns",
            ),
            recorder_status=recorder_status,
            rss_bytes=_exact_int(item["rss_bytes"], f"source sample {index} rss_bytes"),
            system_used_memory_bytes=_exact_int(
                item["system_used_memory_bytes"],
                f"source sample {index} system_used_memory_bytes",
            ),
            memory_available_bytes=_exact_int(
                item["memory_available_bytes"],
                f"source sample {index} memory_available_bytes",
            ),
            swap_used_bytes=_exact_int(
                item["swap_used_bytes"],
                f"source sample {index} swap_used_bytes",
            ),
            cpu_percent=_exact_number(
                item["cpu_percent"],
                f"source sample {index} cpu_percent",
            ),
            temperature_c=_exact_number(
                item["temperature_c"],
                f"source sample {index} temperature_c",
            ),
            throttled=throttled,
            undervoltage=undervoltage,
            filesystem_free_bytes=_exact_int(
                item["filesystem_free_bytes"],
                f"source sample {index} filesystem_free_bytes",
                minimum=1,
            ),
            raw_frames=_exact_int(
                item["raw_frames"],
                f"source sample {index} raw_frames",
            ),
            encoded_frames=_exact_int(
                item["encoded_frames"],
                f"source sample {index} encoded_frames",
            ),
            dropped_frames=_exact_int(
                item["dropped_frames"],
                f"source sample {index} dropped_frames",
            ),
            clip_sequence=_exact_int(
                item["clip_sequence"],
                f"source sample {index} clip_sequence",
            ),
            bitrate_bps=_exact_int(
                item["bitrate_bps"],
                f"source sample {index} bitrate_bps",
                minimum=1,
            ),
            restart_count=_exact_int(
                item["restart_count"],
                f"source sample {index} restart_count",
            ),
        )
    except ValueError as error:
        raise HarnessError(f"source sample {index} values are invalid") from error
    if (
        sample.raw_frames != counters.raw_frames
        or sample.encoded_frames != counters.encoded_frames
        or sample.dropped_frames != counters.dropped_frames
        or sample.restart_count != counters.pipeline_restart_count
        or sample.clip_sequence != counters.clip_sequence
        or sample.bitrate_bps != counters.bitrate_bps
    ):
        raise HarnessError(f"source sample {index} disagrees with its recorder snapshot")
    return sample


def _validate_retained_diagnostic(
    value: object,
    samples: Sequence[AcceptanceSample],
) -> dict[str, object]:
    diagnostic = _mapping(value, "source diagnostic_analysis")
    if frozenset(diagnostic) != DIAGNOSTIC_RESULT_KEYS:
        raise HarnessError("source diagnostic_analysis schema is not closed")
    if (
        diagnostic["schema_version"] != 1
        or diagnostic["sample_count"] != len(samples)
        or diagnostic["started_monotonic_ns"] != samples[0].monotonic_ns
        or diagnostic["ended_monotonic_ns"] != samples[-1].monotonic_ns
        or diagnostic["outcome"] not in {"pass", "fail", "indeterminate"}
    ):
        raise HarnessError("source diagnostic_analysis identity is inconsistent")
    raw_checks = diagnostic["checks"]
    if not isinstance(raw_checks, list) or len(raw_checks) != len(
        DIAGNOSTIC_CHECK_CODES
    ):
        raise HarnessError("source diagnostic checks are incomplete")
    outcomes: dict[str, str] = {}
    for index, raw_check in enumerate(raw_checks):
        check = _mapping(raw_check, f"source diagnostic check {index}")
        if frozenset(check) != DIAGNOSTIC_CHECK_KEYS:
            raise HarnessError("source diagnostic check schema is not closed")
        code = check["code"]
        outcome = check["outcome"]
        if (
            not isinstance(code, str)
            or code not in DIAGNOSTIC_CHECK_CODES
            or code in outcomes
            or outcome not in {"pass", "fail", "indeterminate"}
            or not isinstance(check["summary"], str)
            or not check["summary"]
            or len(check["summary"]) > 512
        ):
            raise HarnessError("source diagnostic check is invalid")
        outcomes[code] = outcome
    if frozenset(outcomes) != DIAGNOSTIC_CHECK_CODES:
        raise HarnessError("source diagnostic check set is not closed")
    if any(
        outcome != "pass"
        for code, outcome in outcomes.items()
        if code != "swap_used"
    ):
        raise HarnessError("source failed a non-swap diagnostic gate")
    raw_diagnostic_samples = diagnostic["samples"]
    expected = [asdict(sample.endurance_sample()) for sample in samples]
    if raw_diagnostic_samples != expected:
        raise HarnessError("source diagnostic samples differ from primary samples")
    return {"outcome": diagnostic["outcome"], "check_outcomes": outcomes}


def _validate_original_checks(value: object) -> dict[str, object]:
    checks = _mapping(value, "source checks")
    keys = frozenset(checks)
    if keys not in {LEGACY_ENDURANCE_CHECK_KEYS, ENDURANCE_CHECK_KEYS}:
        raise HarnessError("source endurance check schema is not recognized")
    required_true = (
        "all_required_samples_present",
        "counter_ordered",
        "frame_shapes_valid",
        "frame_counter_alignment_valid",
        "clip_progress",
        "filesystem_free_bytes_positive",
    )
    if any(checks[name] is not True for name in required_true):
        raise HarnessError("source failed an original non-swap structural gate")
    if (
        _exact_int(
            checks["maximum_frame_counter_delta"],
            "source maximum frame-counter delta",
        )
        > 1
        or checks["maximum_accepted_frame_counter_delta"] != 1
    ):
        raise HarnessError("source frame-counter alignment gate is invalid")
    return dict(checks)


def _swap_policy_from_mapping(value: object, name: str) -> SwapPolicy:
    mapping = _mapping(value, name)
    if frozenset(mapping) != {
        "source",
        "source_sha256",
        "device",
        "kind",
        "size_bytes",
        "used_bytes",
        "priority",
    }:
        raise HarnessError(f"{name} schema is not closed")
    try:
        return SwapPolicy(
            source=cast(str, mapping["source"]),
            source_sha256=cast(str, mapping["source_sha256"]),
            device=cast(str, mapping["device"]),
            kind=cast(str, mapping["kind"]),
            size_bytes=_exact_int(mapping["size_bytes"], f"{name} size_bytes", minimum=1),
            used_bytes=_exact_int(mapping["used_bytes"], f"{name} used_bytes"),
            priority=cast(int, mapping["priority"]),
        )
    except (TypeError, ValueError) as error:
        raise HarnessError(f"{name} values are invalid") from error


def parse_retained_endurance(
    payload: bytes,
    *,
    required_samples: int = ENDURANCE_SAMPLE_COUNT,
    required_duration_s: float = ENDURANCE_DURATION_S,
) -> tuple[int, int, tuple[AcceptanceSample, ...], dict[str, object]]:
    """Strictly parse one prior full acceptance result for read-only reanalysis."""

    if not payload or len(payload) > MAX_JSON_BYTES:
        raise HarnessError("retained endurance result is empty or oversized")
    document = _strict_json_object(payload, "retained endurance result")
    keys = frozenset(document)
    if keys not in {ENDURANCE_RESULT_KEYS, ENDURANCE_RESULT_KEYS_WITH_SWAP_POLICY}:
        raise HarnessError("retained endurance result schema is not closed")
    if (
        document["schema_version"] != 1
        or document["phase"] != "two_hour_endurance"
        or not isinstance(document["passed"], bool)
        or document["sample_count"] != required_samples
        or document["required_sample_count"] != required_samples
        or _exact_number(
            document["required_duration_seconds"],
            "source required duration",
        )
        != required_duration_s
    ):
        raise HarnessError("retained endurance result identity is invalid")
    started_ns = _exact_int(
        document["started_monotonic_ns"],
        "source started_monotonic_ns",
    )
    ended_ns = _exact_int(
        document["ended_monotonic_ns"],
        "source ended_monotonic_ns",
        minimum=1,
    )
    if ended_ns <= started_ns:
        raise HarnessError("retained endurance interval is invalid")
    elapsed = _exact_number(document["elapsed_seconds"], "source elapsed_seconds")
    computed_elapsed = (ended_ns - started_ns) / 1_000_000_000
    if not math.isclose(elapsed, computed_elapsed, rel_tol=0.0, abs_tol=1e-9):
        raise HarnessError("retained endurance elapsed time is inconsistent")
    raw_samples = document["samples"]
    if not isinstance(raw_samples, list) or len(raw_samples) != required_samples:
        raise HarnessError("retained endurance samples are incomplete")
    samples = tuple(
        _parse_retained_sample(value, index)
        for index, value in enumerate(raw_samples)
    )
    original_checks = _validate_original_checks(document["checks"])
    diagnostic = _validate_retained_diagnostic(
        document["diagnostic_analysis"],
        samples,
    )
    if "swap_policy" in document:
        _swap_policy_from_mapping(document["swap_policy"], "source swap_policy")
    return (
        started_ns,
        ended_ns,
        samples,
        {
            "original_passed": document["passed"],
            "original_checks": original_checks,
            "original_diagnostic": diagnostic,
        },
    )


def reanalyze_endurance(
    source: Path,
    expected_source_sha256: str,
    *,
    swap_policy: SwapPolicy | None = None,
    required_samples: int = ENDURANCE_SAMPLE_COUNT,
    required_duration_s: float = ENDURANCE_DURATION_S,
) -> dict[str, object]:
    """Recompute corrected endurance acceptance without mutating source evidence."""

    if SHA256_RE.fullmatch(expected_source_sha256) is None:
        raise HarnessError("expected source SHA-256 is not canonical")
    resolved = source.resolve(strict=True)
    payload = _bounded_regular_bytes(resolved, MAX_JSON_BYTES)
    observed_hash = hashlib.sha256(payload).hexdigest()
    if observed_hash != expected_source_sha256:
        raise HarnessError("retained endurance source hash differs from the supplied hash")
    policy = read_swap_policy() if swap_policy is None else swap_policy
    if not isinstance(policy, SwapPolicy):
        raise HarnessError("reanalysis requires exact current zram-only swap policy")
    started_ns, ended_ns, samples, original = parse_retained_endurance(
        payload,
        required_samples=required_samples,
        required_duration_s=required_duration_s,
    )
    corrected = analyze_endurance_acceptance(
        started_ns,
        ended_ns,
        samples,
        swap_policy=policy,
        required_samples=required_samples,
        required_duration_s=required_duration_s,
    )
    diagnostic = _mapping(
        corrected["diagnostic_analysis"],
        "corrected diagnostic analysis",
    )
    compact_diagnostic = {
        key: value for key, value in diagnostic.items() if key != "samples"
    }
    swap_values = [sample.swap_used_bytes for sample in samples]
    return {
        "schema_version": 1,
        "phase": "reanalyze_endurance",
        "passed": corrected["passed"],
        "source": {
            "path": resolved.as_posix(),
            "sha256": observed_hash,
            "size_bytes": len(payload),
            "original_passed": original["original_passed"],
            "original_diagnostic_outcome": _mapping(
                original["original_diagnostic"],
                "original diagnostic summary",
            )["outcome"],
        },
        "swap_policy": policy.to_dict(),
        "swap_observation": {
            "baseline_bytes": swap_values[0],
            "last_bytes": swap_values[-1],
            "minimum_bytes": min(swap_values),
            "maximum_bytes": max(swap_values),
            "distinct_values": len(set(swap_values)),
            "growth_above_baseline_bytes": max(max(swap_values) - swap_values[0], 0),
        },
        "corrected_analysis": {
            "sample_count": corrected["sample_count"],
            "elapsed_seconds": corrected["elapsed_seconds"],
            "checks": corrected["checks"],
            "diagnostic_analysis": compact_diagnostic,
        },
    }


def _write_exclusive_json(path: Path, document: Mapping[str, object]) -> None:
    parent = path.parent.resolve(strict=True)
    recording_root = RECORDING_ROOT.resolve(strict=True)
    if parent == recording_root or recording_root in parent.parents:
        raise HarnessError("acceptance evidence must not be written to the recording volume")
    destination = parent / path.name
    if not path.name or path.name in {".", ".."} or destination.exists():
        raise HarnessError("output must be a new file in an existing directory")
    payload = (
        json.dumps(
            document,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    if len(payload) > MAX_JSON_BYTES:
        raise HarnessError("result exceeds its machine-readable output bound")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError as error:
            raise HarnessError("refusing to replace an existing output") from error
        if os.name != "nt":
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="milestone6-acceptance")
    parser.add_argument("--expected-manifest-sha256", required=True)
    subparsers = parser.add_subparsers(dest="phase", required=True)
    media = subparsers.add_parser("validate-media")
    media.add_argument("--boot-id", type=UUID, required=True)
    media.add_argument("--start-sequence", type=int, required=True)
    media.add_argument("--output", type=Path, required=True)
    endurance = subparsers.add_parser("collect-endurance")
    endurance.add_argument("--pid", type=int, required=True)
    endurance.add_argument("--output", type=Path, required=True)
    reanalyze = subparsers.add_parser("reanalyze-endurance")
    reanalyze.add_argument("--source", type=Path, required=True)
    reanalyze.add_argument("--expected-source-sha256", required=True)
    reanalyze.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        verify_manifest(arguments.expected_manifest_sha256)
        if arguments.phase == "validate-media":
            result = validate_media_acceptance(
                arguments.boot_id,
                arguments.start_sequence,
            )
        elif arguments.phase == "collect-endurance":
            initial_swap_policy = read_swap_policy()
            source = LiveSampleSource(arguments.pid)
            started, ended, samples = collect_endurance(source)
            final_swap_policy = read_swap_policy()
            if not _same_swap_policy_shape(
                initial_swap_policy,
                final_swap_policy,
            ):
                raise HarnessError("zram-only swap policy changed during collection")
            result = analyze_endurance_acceptance(
                started,
                ended,
                samples,
                swap_policy=final_swap_policy,
            )
        elif arguments.phase == "reanalyze-endurance":
            result = reanalyze_endurance(
                arguments.source,
                arguments.expected_source_sha256,
            )
        else:
            raise HarnessError("unknown phase")
        _write_exclusive_json(arguments.output, result)
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "phase": arguments.phase,
                    "passed": result["passed"],
                    "output": str(arguments.output),
                    "output_sha256": _sha256_file(arguments.output, maximum=MAX_JSON_BYTES),
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
