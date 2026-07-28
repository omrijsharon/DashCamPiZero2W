"""Typed, bounded, and deterministically serialized per-clip metadata."""

from __future__ import annotations

import json
import math
import os
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Final
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dashcam.state import GpsTimeState, SystemClockState, TimestampQuality
from dashcam.storage.naming import ClipFilePair, ClipNameError, parse_clip_filename

SCHEMA_VERSION: Final = 1
MAX_GPS_SAMPLES: Final = 600
MAX_WARNINGS: Final = 32
MAX_WARNING_LENGTH: Final = 256
MAX_PROVENANCE_LENGTH: Final = 256
MAX_SOFTWARE_VERSION_LENGTH: Final = 128
MAX_PROTECTION_REASON_LENGTH: Final = 128
MAX_UNCERTAINTY_NS: Final = 86_400_000_000_000
MAX_MONOTONIC_NS: Final = 9_223_372_036_854_775_807
# Canonical v1 JSON serializes civil timestamps to milliseconds. Independently
# truncating the two endpoints can change their observed duration by less than
# one millisecond even though the in-memory projection is microsecond precise.
MAX_DURATION_ROUNDING_ERROR_NS: Final = 1_000_000


class MetadataValidationError(ValueError):
    """Raised when sidecar metadata violates the versioned contract."""


class SidecarWriteError(OSError):
    """Raised when a sidecar cannot be written under the requested policy."""


class TimeAnchorSource(StrEnum):
    GPS = "GPS"
    SYSTEM_CLOCK = "SYSTEM_CLOCK"


def _integer(
    value: object, *, field: str, minimum: int = 0, maximum: int = MAX_MONOTONIC_NS
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MetadataValidationError(f"{field} must be an integer")
    if not minimum <= value <= maximum:
        raise MetadataValidationError(f"{field} is outside its allowed range")
    return value


def _finite(
    value: object,
    *,
    field: str,
    minimum: float,
    maximum: float,
    maximum_exclusive: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MetadataValidationError(f"{field} must be numeric")
    number = float(value)
    in_range = minimum <= number < maximum if maximum_exclusive else minimum <= number <= maximum
    if not math.isfinite(number) or not in_range:
        raise MetadataValidationError(f"{field} is outside its allowed range")
    return number


def _bounded_text(value: object, *, field: str, maximum: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not allow_empty) or len(value) > maximum:
        raise MetadataValidationError(f"{field} must be a bounded string")
    if any(ord(character) < 0x20 for character in value):
        raise MetadataValidationError(f"{field} cannot contain control characters")
    return value


def _utc(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise MetadataValidationError(f"{field} must be timezone-aware UTC")
    if value.utcoffset() != UTC.utcoffset(value):
        raise MetadataValidationError(f"{field} must use a UTC offset")
    return value.astimezone(UTC)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _iso_local(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds")


@dataclass(frozen=True, slots=True)
class TimeAnchor:
    source: TimeAnchorSource
    monotonic_ns: int
    utc: datetime
    uncertainty_ns: int
    provenance: str

    def __post_init__(self) -> None:
        if not isinstance(self.source, TimeAnchorSource):
            raise MetadataValidationError("time_anchor.source is invalid")
        _integer(self.monotonic_ns, field="time_anchor.monotonic_ns")
        _utc(self.utc, field="time_anchor.utc")
        _integer(
            self.uncertainty_ns,
            field="time_anchor.uncertainty_ns",
            maximum=MAX_UNCERTAINTY_NS,
        )
        _bounded_text(
            self.provenance,
            field="time_anchor.provenance",
            maximum=MAX_PROVENANCE_LENGTH,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "source": self.source.value,
            "monotonic_ns": self.monotonic_ns,
            "utc": _iso_utc(self.utc),
            "uncertainty_ns": self.uncertainty_ns,
            "provenance": self.provenance,
        }


@dataclass(frozen=True, slots=True)
class VideoSummary:
    codec: str
    width: int
    height: int
    fps_nominal: float
    target_bitrate_bps: int
    measured_bitrate_bps: int
    frames_written: int
    dropped_frames: int

    def __post_init__(self) -> None:
        _bounded_text(self.codec, field="video.codec", maximum=32)
        _integer(self.width, field="video.width", minimum=1, maximum=16_384)
        _integer(self.height, field="video.height", minimum=1, maximum=16_384)
        _finite(self.fps_nominal, field="video.fps_nominal", minimum=0.1, maximum=1_000.0)
        _integer(
            self.target_bitrate_bps,
            field="video.target_bitrate_bps",
            minimum=1,
            maximum=1_000_000_000,
        )
        _integer(
            self.measured_bitrate_bps,
            field="video.measured_bitrate_bps",
            maximum=1_000_000_000,
        )
        _integer(self.frames_written, field="video.frames_written")
        _integer(self.dropped_frames, field="video.dropped_frames")

    def to_mapping(self) -> dict[str, object]:
        return {
            "codec": self.codec,
            "width": self.width,
            "height": self.height,
            "fps_nominal": self.fps_nominal,
            "target_bitrate_bps": self.target_bitrate_bps,
            "measured_bitrate_bps": self.measured_bitrate_bps,
            "frames_written": self.frames_written,
            "dropped_frames": self.dropped_frames,
        }


@dataclass(frozen=True, slots=True)
class AudioSummary:
    available: bool
    codec: str | None
    sample_rate_hz: int | None
    channels: int | None
    target_bitrate_bps: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.available, bool):
            raise MetadataValidationError("audio.available must be boolean")
        values = (self.codec, self.sample_rate_hz, self.channels, self.target_bitrate_bps)
        if not self.available:
            if any(value is not None for value in values):
                raise MetadataValidationError("unavailable audio must not claim stream settings")
            return
        if self.codec is None:
            raise MetadataValidationError("available audio requires a codec")
        _bounded_text(self.codec, field="audio.codec", maximum=32)
        _integer(self.sample_rate_hz, field="audio.sample_rate_hz", minimum=1, maximum=768_000)
        _integer(self.channels, field="audio.channels", minimum=1, maximum=32)
        _integer(
            self.target_bitrate_bps,
            field="audio.target_bitrate_bps",
            minimum=1,
            maximum=100_000_000,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "available": self.available,
            "codec": self.codec,
            "sample_rate_hz": self.sample_rate_hz,
            "channels": self.channels,
            "target_bitrate_bps": self.target_bitrate_bps,
        }


@dataclass(frozen=True, slots=True)
class GpsSample:
    monotonic_ns: int
    utc: datetime | None
    timestamp_quality: TimestampQuality
    lat_deg: float | None = None
    lon_deg: float | None = None
    speed_mps: float | None = None
    course_deg: float | None = None
    altitude_m: float | None = None
    fix_quality: int | None = None
    satellites: int | None = None
    hdop: float | None = None

    def __post_init__(self) -> None:
        _integer(self.monotonic_ns, field="gps.samples[].monotonic_ns")
        if not isinstance(self.timestamp_quality, TimestampQuality):
            raise MetadataValidationError("gps.samples[].timestamp_quality is invalid")
        if self.timestamp_quality is TimestampQuality.MONOTONIC_ONLY:
            if self.utc is not None:
                raise MetadataValidationError("monotonic-only GPS sample UTC must be null")
        elif self.utc is None:
            raise MetadataValidationError("derived GPS sample requires UTC")
        else:
            _utc(self.utc, field="gps.samples[].utc")
        optional_floats = (
            ("lat_deg", self.lat_deg, -90.0, 90.0, False),
            ("lon_deg", self.lon_deg, -180.0, 180.0, False),
            ("speed_mps", self.speed_mps, 0.0, 1_000.0, False),
            ("course_deg", self.course_deg, 0.0, 360.0, True),
            ("altitude_m", self.altitude_m, -2_000.0, 100_000.0, False),
            ("hdop", self.hdop, 0.0, 1_000.0, False),
        )
        for name, value, minimum, maximum, exclusive in optional_floats:
            if value is not None:
                _finite(
                    value,
                    field=f"gps.samples[].{name}",
                    minimum=minimum,
                    maximum=maximum,
                    maximum_exclusive=exclusive,
                )
        if self.fix_quality is not None:
            _integer(self.fix_quality, field="gps.samples[].fix_quality", maximum=8)
        if self.satellites is not None:
            _integer(self.satellites, field="gps.samples[].satellites", maximum=255)
        if (self.lat_deg is None) is not (self.lon_deg is None):
            raise MetadataValidationError("GPS latitude and longitude must be present together")

    def to_mapping(self) -> dict[str, object]:
        return {
            "monotonic_ns": self.monotonic_ns,
            "utc": None if self.utc is None else _iso_utc(self.utc),
            "timestamp_quality": self.timestamp_quality.value,
            "lat_deg": self.lat_deg,
            "lon_deg": self.lon_deg,
            "speed_mps": self.speed_mps,
            "course_deg": self.course_deg,
            "altitude_m": self.altitude_m,
            "fix_quality": self.fix_quality,
            "satellites": self.satellites,
            "hdop": self.hdop,
        }


@dataclass(frozen=True, slots=True)
class GpsSummary:
    available: bool
    first_fix_utc: datetime | None
    samples: tuple[GpsSample, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.available, bool):
            raise MetadataValidationError("gps.available must be boolean")
        if self.first_fix_utc is not None:
            _utc(self.first_fix_utc, field="gps.first_fix_utc")
        if not isinstance(self.samples, tuple):
            raise MetadataValidationError("gps.samples must be an immutable tuple")
        if len(self.samples) > MAX_GPS_SAMPLES:
            raise MetadataValidationError("gps.samples exceeds its per-clip bound")
        if any(not isinstance(sample, GpsSample) for sample in self.samples):
            raise MetadataValidationError("gps.samples contains an invalid item")
        if not self.available and (self.first_fix_utc is not None or self.samples):
            raise MetadataValidationError("unavailable GPS must not contain fixes or samples")

    def to_mapping(self) -> dict[str, object]:
        return {
            "available": self.available,
            "first_fix_utc": (None if self.first_fix_utc is None else _iso_utc(self.first_fix_utc)),
            "samples": [sample.to_mapping() for sample in self.samples],
        }


@dataclass(frozen=True, slots=True)
class ClipSidecar:
    schema_version: int
    clip_id: UUID
    boot_id: UUID
    sequence: int
    video_file: str
    metadata_file: str
    start_utc: datetime | None
    end_utc: datetime | None
    start_monotonic_ns: int
    end_monotonic_ns: int
    gps_time_state: GpsTimeState
    system_clock_state: SystemClockState
    timestamp_quality: TimestampQuality
    time_anchor: TimeAnchor | None
    timezone: str
    start_local: datetime | None
    video: VideoSummary
    audio: AudioSummary
    gps: GpsSummary
    protected: bool
    protection_reason: str | None
    software_version: str
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != SCHEMA_VERSION
        ):
            raise MetadataValidationError(f"schema_version must be {SCHEMA_VERSION}")
        if not isinstance(self.clip_id, UUID) or not isinstance(self.boot_id, UUID):
            raise MetadataValidationError("clip_id and boot_id must be UUID values")
        _integer(self.sequence, field="sequence", maximum=999_999)
        try:
            ClipFilePair(video_name=self.video_file, metadata_name=self.metadata_file)
            parsed_name = parse_clip_filename(self.video_file)
        except ClipNameError as exc:
            raise MetadataValidationError(
                "video_file and metadata_file must be a safe pair"
            ) from exc
        if parsed_name.sequence != self.sequence:
            raise MetadataValidationError("filename sequence does not match sidecar sequence")
        _integer(self.start_monotonic_ns, field="start_monotonic_ns")
        _integer(self.end_monotonic_ns, field="end_monotonic_ns")
        if self.end_monotonic_ns <= self.start_monotonic_ns:
            raise MetadataValidationError("end_monotonic_ns must be after start_monotonic_ns")
        if not isinstance(self.gps_time_state, GpsTimeState):
            raise MetadataValidationError("gps_time_state is invalid")
        if not isinstance(self.system_clock_state, SystemClockState):
            raise MetadataValidationError("system_clock_state is invalid")
        if not isinstance(self.timestamp_quality, TimestampQuality):
            raise MetadataValidationError("timestamp_quality is invalid")
        if (
            self.timestamp_quality is TimestampQuality.MONOTONIC_ONLY
            and not parsed_name.provisional
        ):
            raise MetadataValidationError("unsynced sidecar requires a provisional filename")
        if (
            self.timestamp_quality is not TimestampQuality.MONOTONIC_ONLY
            and parsed_name.provisional
        ):
            raise MetadataValidationError("derived sidecar requires a finalized UTC filename")
        if self.start_utc is not None and parsed_name.utc_started_at is not None:
            canonical_start_utc = _utc(self.start_utc, field="start_utc")
            expected_name_utc = canonical_start_utc.replace(
                microsecond=(self.start_utc.microsecond // 1_000) * 1_000
            )
            if parsed_name.utc_started_at != expected_name_utc:
                raise MetadataValidationError("filename UTC does not match sidecar start_utc")
        if not isinstance(self.video, VideoSummary):
            raise MetadataValidationError("video summary is invalid")
        if not isinstance(self.audio, AudioSummary):
            raise MetadataValidationError("audio summary is invalid")
        if not isinstance(self.gps, GpsSummary):
            raise MetadataValidationError("GPS summary is invalid")
        _bounded_text(self.timezone, field="timezone", maximum=128)
        try:
            timezone = ZoneInfo(self.timezone)
        except (TypeError, ZoneInfoNotFoundError) as exc:
            raise MetadataValidationError("timezone must be a valid IANA zone") from exc
        self._validate_time_fields(timezone)
        self._validate_sample_window()
        if not isinstance(self.protected, bool):
            raise MetadataValidationError("protected must be boolean")
        if self.protected:
            if self.protection_reason is None:
                raise MetadataValidationError("protected clips require a protection_reason")
            _bounded_text(
                self.protection_reason,
                field="protection_reason",
                maximum=MAX_PROTECTION_REASON_LENGTH,
            )
        elif self.protection_reason is not None:
            raise MetadataValidationError("unprotected clips cannot have a protection_reason")
        _bounded_text(
            self.software_version,
            field="software_version",
            maximum=MAX_SOFTWARE_VERSION_LENGTH,
        )
        if not isinstance(self.warnings, tuple):
            raise MetadataValidationError("warnings must be an immutable tuple")
        if len(self.warnings) > MAX_WARNINGS:
            raise MetadataValidationError("warnings exceeds its per-clip bound")
        for warning in self.warnings:
            _bounded_text(warning, field="warnings[]", maximum=MAX_WARNING_LENGTH)

    def _validate_time_fields(self, timezone: ZoneInfo) -> None:
        civil_fields = (self.start_utc, self.end_utc, self.start_local)
        if self.timestamp_quality is TimestampQuality.MONOTONIC_ONLY:
            if any(value is not None for value in civil_fields) or self.time_anchor is not None:
                raise MetadataValidationError("monotonic-only metadata must have null civil time")
            if self.gps_time_state is not GpsTimeState.UNSYNCED:
                raise MetadataValidationError("monotonic-only metadata requires UNSYNCED GPS time")
            if self.gps.first_fix_utc is not None:
                raise MetadataValidationError("monotonic-only metadata cannot claim first_fix_utc")
            return
        if any(value is None for value in civil_fields) or self.time_anchor is None:
            raise MetadataValidationError("derived timestamps require civil time and a time anchor")
        assert self.start_utc is not None
        assert self.end_utc is not None
        assert self.start_local is not None
        start_utc = _utc(self.start_utc, field="start_utc")
        end_utc = _utc(self.end_utc, field="end_utc")
        if end_utc <= start_utc:
            raise MetadataValidationError("end_utc must be after start_utc")
        monotonic_duration_ns = self.end_monotonic_ns - self.start_monotonic_ns
        civil_duration_ns = _timedelta_ns(end_utc - start_utc)
        if abs(civil_duration_ns - monotonic_duration_ns) > MAX_DURATION_ROUNDING_ERROR_NS:
            raise MetadataValidationError("civil and monotonic clip durations do not agree")
        if self.start_local.tzinfo is None or self.start_local.utcoffset() is None:
            raise MetadataValidationError("start_local must be timezone-aware")
        expected_local = start_utc.astimezone(timezone)
        if (
            self.start_local != expected_local
            or self.start_local.utcoffset() != expected_local.utcoffset()
        ):
            raise MetadataValidationError("start_local does not match start_utc and timezone")
        if self.timestamp_quality is TimestampQuality.GPS_ANCHORED:
            if self.time_anchor.source is not TimeAnchorSource.GPS:
                raise MetadataValidationError("GPS_ANCHORED requires a GPS time anchor")
            if self.gps_time_state is GpsTimeState.UNSYNCED:
                raise MetadataValidationError("GPS_ANCHORED cannot use UNSYNCED GPS time")
        elif self.timestamp_quality is TimestampQuality.SYSTEM_DERIVED:
            if self.time_anchor.source is not TimeAnchorSource.SYSTEM_CLOCK:
                raise MetadataValidationError("SYSTEM_DERIVED requires a system-clock anchor")
            if self.system_clock_state is not SystemClockState.SYNCHRONIZED:
                raise MetadataValidationError("SYSTEM_DERIVED requires a synchronized system clock")

    def _validate_sample_window(self) -> None:
        previous_monotonic_ns = self.start_monotonic_ns
        for sample in self.gps.samples:
            if not self.start_monotonic_ns <= sample.monotonic_ns < self.end_monotonic_ns:
                raise MetadataValidationError("GPS sample falls outside the clip interval")
            if sample.monotonic_ns < previous_monotonic_ns:
                raise MetadataValidationError("GPS samples must be ordered by monotonic time")
            previous_monotonic_ns = sample.monotonic_ns
            if sample.utc is not None and self.start_utc is not None and self.end_utc is not None:
                sample_utc = _utc(sample.utc, field="gps.samples[].utc")
                if not self.start_utc <= sample_utc <= self.end_utc:
                    raise MetadataValidationError("GPS sample UTC falls outside the clip interval")
        if (
            self.gps.first_fix_utc is not None
            and self.start_utc is not None
            and self.end_utc is not None
            and not self.start_utc <= self.gps.first_fix_utc <= self.end_utc
        ):
            raise MetadataValidationError("GPS first fix falls outside the clip interval")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "clip_id": str(self.clip_id),
            "boot_id": str(self.boot_id),
            "sequence": self.sequence,
            "video_file": self.video_file,
            "metadata_file": self.metadata_file,
            "start_utc": None if self.start_utc is None else _iso_utc(self.start_utc),
            "end_utc": None if self.end_utc is None else _iso_utc(self.end_utc),
            "start_monotonic_ns": self.start_monotonic_ns,
            "end_monotonic_ns": self.end_monotonic_ns,
            "gps_time_state": self.gps_time_state.value,
            "system_clock_state": self.system_clock_state.value,
            "timestamp_quality": self.timestamp_quality.value,
            "time_anchor": None if self.time_anchor is None else self.time_anchor.to_mapping(),
            "timezone": self.timezone,
            "start_local": None if self.start_local is None else _iso_local(self.start_local),
            "video": self.video.to_mapping(),
            "audio": self.audio.to_mapping(),
            "gps": self.gps.to_mapping(),
            "protected": self.protected,
            "protection_reason": self.protection_reason,
            "software_version": self.software_version,
            "warnings": list(self.warnings),
        }

    def to_canonical_json(self) -> bytes:
        """Return deterministic UTF-8 JSON with no insignificant whitespace."""

        return json.dumps(
            self.to_mapping(),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")


def write_sidecar_atomic(
    sidecar: ClipSidecar, destination: Path, *, replace_existing: bool = False
) -> None:
    """Durably write one sidecar using an explicit single-writer overwrite policy.

    Normal finalization refuses an existing destination. Reconciliation may opt in
    to atomic replacement. The caller must serialize writers for one clip.
    """

    destination = Path(destination)
    if destination.name != sidecar.metadata_file:
        raise SidecarWriteError("destination filename does not match metadata identity")
    if not destination.parent.is_dir():
        raise SidecarWriteError("destination directory does not exist")
    if destination.exists() and not replace_existing:
        raise FileExistsError(destination)

    descriptor = -1
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(sidecar.to_canonical_json())
            stream.flush()
            os.fsync(stream.fileno())
        if destination.exists() and not replace_existing:
            raise FileExistsError(destination)
        os.replace(temporary_path, destination)
        temporary_path = None
        _fsync_directory(destination.parent)
    except OSError:
        raise
    except Exception as exc:
        raise SidecarWriteError("failed to write sidecar") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path is not None:
            with suppress(FileNotFoundError):
                temporary_path.unlink()


def _fsync_directory(directory: Path) -> None:
    """Flush the containing directory where the host supports directory handles."""

    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _timedelta_ns(value: timedelta) -> int:
    return (
        value.days * 86_400_000_000_000 + value.seconds * 1_000_000_000 + value.microseconds * 1_000
    )
