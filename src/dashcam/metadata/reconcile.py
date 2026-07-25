"""Strict sidecar parsing and pure post-anchor reconciliation planning.

This module deliberately performs no filesystem access.  The caller persists the
returned sidecar replacement and ``RECONCILE_NAME`` intent before applying the
pair moves through :mod:`dashcam.storage.intents`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Set
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Final, cast
from uuid import UUID

from dashcam.gps.clock import to_local_time
from dashcam.metadata.schema import (
    MAX_GPS_SAMPLES,
    MAX_WARNINGS,
    AudioSummary,
    ClipSidecar,
    GpsSample,
    GpsSummary,
    MetadataValidationError,
    TimeAnchor,
    TimeAnchorSource,
    VideoSummary,
)
from dashcam.state import GpsTimeState, SystemClockState, TimestampQuality
from dashcam.storage.intents import IntentKind, OperationIntent, PairPaths
from dashcam.storage.naming import ClipNameError, finalized_clip_pair, parse_clip_filename

MAX_SIDECAR_BYTES: Final = 512 * 1024
"""Hard input bound comfortably above the bounded v1 sidecar payload."""

_NANOSECONDS_PER_SECOND: Final = 1_000_000_000
_TOP_LEVEL_KEYS: Final = frozenset(
    {
        "schema_version",
        "clip_id",
        "boot_id",
        "sequence",
        "video_file",
        "metadata_file",
        "start_utc",
        "end_utc",
        "start_monotonic_ns",
        "end_monotonic_ns",
        "gps_time_state",
        "system_clock_state",
        "timestamp_quality",
        "time_anchor",
        "timezone",
        "start_local",
        "video",
        "audio",
        "gps",
        "protected",
        "protection_reason",
        "software_version",
        "warnings",
    }
)
_TIME_ANCHOR_KEYS: Final = frozenset(
    {"source", "monotonic_ns", "utc", "uncertainty_ns", "provenance"}
)
_VIDEO_KEYS: Final = frozenset(
    {
        "codec",
        "width",
        "height",
        "fps_nominal",
        "target_bitrate_bps",
        "measured_bitrate_bps",
        "frames_written",
        "dropped_frames",
    }
)
_AUDIO_KEYS: Final = frozenset(
    {"available", "codec", "sample_rate_hz", "channels", "target_bitrate_bps"}
)
_GPS_KEYS: Final = frozenset({"available", "first_fix_utc", "samples"})
_GPS_SAMPLE_KEYS: Final = frozenset(
    {
        "monotonic_ns",
        "utc",
        "timestamp_quality",
        "lat_deg",
        "lon_deg",
        "speed_mps",
        "course_deg",
        "altitude_m",
        "fix_quality",
        "satellites",
        "hdop",
    }
)
_UTC_RE: Final = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
_LOCAL_RE: Final = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2}")


class SidecarParseError(ValueError):
    """Raised when untrusted sidecar input is not canonical bounded v1 data."""


class MetadataReconciliationError(ValueError):
    """Raised when a requested post-anchor reconciliation is unsafe."""


@dataclass(frozen=True, slots=True)
class MetadataReconciliationPlan:
    """New metadata and its recoverable two-member rename intent.

    ``intent`` is ``None`` only when the clip already has civil timestamps and a
    finalized name.  This makes replay a stable no-op rather than allowing later
    noisy anchors to rename the clip again.
    """

    sidecar: ClipSidecar
    intent: OperationIntent | None
    already_reconciled: bool

    def __post_init__(self) -> None:
        if not isinstance(self.sidecar, ClipSidecar):
            raise TypeError("sidecar must be a ClipSidecar")
        if self.already_reconciled != (self.intent is None):
            raise ValueError("only an already-reconciled plan may omit its intent")
        if self.intent is not None:
            if self.intent.kind is not IntentKind.RECONCILE_NAME:
                raise ValueError("metadata reconciliation requires a RECONCILE_NAME intent")
            if self.intent.clip_id != self.sidecar.clip_id:
                raise ValueError("intent and sidecar clip IDs must remain identical")


def parse_sidecar_mapping(raw: Mapping[str, object]) -> ClipSidecar:
    """Parse a closed v1 mapping into the existing immutable typed schema.

    Every object has an exact field set and every scalar is type checked before
    the schema model enforces cross-field filename, timestamp, and sample-window
    consistency.
    """

    try:
        top = _closed_mapping(raw, _TOP_LEVEL_KEYS, "sidecar")
        anchor = _parse_anchor(top["time_anchor"])
        video = _parse_video(top["video"])
        audio = _parse_audio(top["audio"])
        gps = _parse_gps(top["gps"])
        warnings_value = top["warnings"]
        if not isinstance(warnings_value, list):
            raise SidecarParseError("warnings must be an array")
        if len(warnings_value) > MAX_WARNINGS:
            raise SidecarParseError("warnings exceeds its per-clip bound")
        warnings = tuple(_strict_str(value, "warnings[]") for value in warnings_value)

        return ClipSidecar(
            schema_version=_strict_int(top["schema_version"], "schema_version"),
            clip_id=_canonical_uuid(top["clip_id"], "clip_id"),
            boot_id=_canonical_uuid(top["boot_id"], "boot_id"),
            sequence=_strict_int(top["sequence"], "sequence"),
            video_file=_strict_str(top["video_file"], "video_file"),
            metadata_file=_strict_str(top["metadata_file"], "metadata_file"),
            start_utc=_optional_utc(top["start_utc"], "start_utc"),
            end_utc=_optional_utc(top["end_utc"], "end_utc"),
            start_monotonic_ns=_strict_int(top["start_monotonic_ns"], "start_monotonic_ns"),
            end_monotonic_ns=_strict_int(top["end_monotonic_ns"], "end_monotonic_ns"),
            gps_time_state=GpsTimeState(_strict_str(top["gps_time_state"], "gps_time_state")),
            system_clock_state=SystemClockState(
                _strict_str(top["system_clock_state"], "system_clock_state")
            ),
            timestamp_quality=TimestampQuality(
                _strict_str(top["timestamp_quality"], "timestamp_quality")
            ),
            time_anchor=anchor,
            timezone=_strict_str(top["timezone"], "timezone"),
            start_local=_optional_local(top["start_local"], "start_local"),
            video=video,
            audio=audio,
            gps=gps,
            protected=_strict_bool(top["protected"], "protected"),
            protection_reason=_optional_str(top["protection_reason"], "protection_reason"),
            software_version=_strict_str(top["software_version"], "software_version"),
            warnings=warnings,
        )
    except SidecarParseError:
        raise
    except (MetadataValidationError, TypeError, ValueError) as exc:
        raise SidecarParseError(f"invalid sidecar: {exc}") from exc


def parse_sidecar_bytes(payload: bytes) -> ClipSidecar:
    """Parse exact canonical UTF-8 JSON after enforcing a hard byte bound."""

    if not isinstance(payload, bytes):
        raise SidecarParseError("sidecar payload must be bytes")
    if not payload:
        raise SidecarParseError("sidecar payload cannot be empty")
    if len(payload) > MAX_SIDECAR_BYTES:
        raise SidecarParseError("sidecar payload exceeds the byte bound")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SidecarParseError("sidecar payload is not valid UTF-8") from exc
    try:
        decoded = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_non_json_constant,
        )
    except (RecursionError, json.JSONDecodeError, SidecarParseError) as exc:
        raise SidecarParseError(f"malformed sidecar JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise SidecarParseError("sidecar JSON root must be an object")
    sidecar = parse_sidecar_mapping(cast(dict[str, object], decoded))
    if sidecar.to_canonical_json() != payload:
        raise SidecarParseError("sidecar JSON is valid but not in canonical byte form")
    return sidecar


def plan_post_anchor_reconciliation(
    sidecar: ClipSidecar,
    *,
    anchor: TimeAnchor,
    intent_id: UUID,
    created_monotonic_ns: int,
    existing_names: Set[str] = frozenset(),
    directory: str = "clips",
    gps_time_state: GpsTimeState | None = None,
    system_clock_state: SystemClockState | None = None,
) -> MetadataReconciliationPlan:
    """Plan one stable conversion and recoverable pair rename without I/O.

    The accepted anchor may be later than the clip; projection therefore works
    in both monotonic directions.  Existing finalized metadata is returned
    unchanged so repeated reconciliation cannot churn names or UUID identity.
    """

    if not isinstance(sidecar, ClipSidecar):
        raise MetadataReconciliationError("sidecar must be a ClipSidecar")
    if not isinstance(anchor, TimeAnchor):
        raise MetadataReconciliationError("anchor must be a TimeAnchor")
    if not isinstance(intent_id, UUID):
        raise MetadataReconciliationError("intent_id must be a UUID")
    if directory not in {"clips", "protected"}:
        raise MetadataReconciliationError("reconciliation directory must be clips or protected")
    if (
        isinstance(created_monotonic_ns, bool)
        or not isinstance(created_monotonic_ns, int)
        or created_monotonic_ns < 0
    ):
        raise MetadataReconciliationError("created_monotonic_ns must be a non-negative integer")

    parsed_source = parse_clip_filename(sidecar.video_file)
    if not parsed_source.provisional:
        return MetadataReconciliationPlan(sidecar, None, True)

    quality = _quality_for_anchor(anchor)
    resolved_gps_state, resolved_system_state = _resolved_clock_states(
        sidecar,
        anchor=anchor,
        gps_time_state=gps_time_state,
        system_clock_state=system_clock_state,
    )
    start_utc = _project_utc(anchor, sidecar.start_monotonic_ns)
    end_utc = _project_utc(anchor, sidecar.end_monotonic_ns)
    local_outcome = to_local_time(start_utc, sidecar.timezone)
    if not local_outcome.ok or local_outcome.local is None:
        error = local_outcome.error.value if local_outcome.error is not None else "UNKNOWN"
        raise MetadataReconciliationError(f"cannot derive local time: {error}")

    try:
        target_pair = finalized_clip_pair(
            utc_started_at=start_utc,
            boot_id=parsed_source.boot_id,
            sequence=sidecar.sequence,
            existing_names=existing_names,
        )
    except ClipNameError as exc:
        raise MetadataReconciliationError(str(exc)) from exc

    samples = tuple(
        replace(
            sample,
            utc=_project_utc(anchor, sample.monotonic_ns),
            timestamp_quality=quality,
        )
        for sample in sidecar.gps.samples
    )
    first_fix_utc = next(
        (
            sample.utc
            for sample in samples
            if sample.lat_deg is not None and sample.lon_deg is not None
        ),
        None,
    )
    reconciled = replace(
        sidecar,
        video_file=target_pair.video_name,
        metadata_file=target_pair.metadata_name,
        start_utc=start_utc,
        end_utc=end_utc,
        gps_time_state=resolved_gps_state,
        system_clock_state=resolved_system_state,
        timestamp_quality=quality,
        time_anchor=anchor,
        start_local=local_outcome.local.datetime,
        gps=replace(sidecar.gps, first_fix_utc=first_fix_utc, samples=samples),
    )
    if reconciled.clip_id != sidecar.clip_id:
        raise MetadataReconciliationError("reconciliation changed the stable clip UUID")

    try:
        intent = OperationIntent(
            intent_id=intent_id,
            clip_id=sidecar.clip_id,
            kind=IntentKind.RECONCILE_NAME,
            created_monotonic_ns=created_monotonic_ns,
            paths=PairPaths(
                video_source=f"{directory}/{sidecar.video_file}",
                sidecar_source=f"{directory}/{sidecar.metadata_file}",
                video_target=f"{directory}/{target_pair.video_name}",
                sidecar_target=f"{directory}/{target_pair.metadata_name}",
            ),
        )
    except (TypeError, ValueError) as exc:
        raise MetadataReconciliationError(f"invalid reconciliation intent: {exc}") from exc
    return MetadataReconciliationPlan(reconciled, intent, False)


def _parse_anchor(value: object) -> TimeAnchor | None:
    if value is None:
        return None
    raw = _closed_mapping(value, _TIME_ANCHOR_KEYS, "time_anchor")
    return TimeAnchor(
        source=TimeAnchorSource(_strict_str(raw["source"], "time_anchor.source")),
        monotonic_ns=_strict_int(raw["monotonic_ns"], "time_anchor.monotonic_ns"),
        utc=_required_utc(raw["utc"], "time_anchor.utc"),
        uncertainty_ns=_strict_int(raw["uncertainty_ns"], "time_anchor.uncertainty_ns"),
        provenance=_strict_str(raw["provenance"], "time_anchor.provenance"),
    )


def _parse_video(value: object) -> VideoSummary:
    raw = _closed_mapping(value, _VIDEO_KEYS, "video")
    return VideoSummary(
        codec=_strict_str(raw["codec"], "video.codec"),
        width=_strict_int(raw["width"], "video.width"),
        height=_strict_int(raw["height"], "video.height"),
        fps_nominal=_strict_number(raw["fps_nominal"], "video.fps_nominal"),
        target_bitrate_bps=_strict_int(raw["target_bitrate_bps"], "video.target_bitrate_bps"),
        measured_bitrate_bps=_strict_int(raw["measured_bitrate_bps"], "video.measured_bitrate_bps"),
        frames_written=_strict_int(raw["frames_written"], "video.frames_written"),
        dropped_frames=_strict_int(raw["dropped_frames"], "video.dropped_frames"),
    )


def _parse_audio(value: object) -> AudioSummary:
    raw = _closed_mapping(value, _AUDIO_KEYS, "audio")
    return AudioSummary(
        available=_strict_bool(raw["available"], "audio.available"),
        codec=_optional_str(raw["codec"], "audio.codec"),
        sample_rate_hz=_optional_int(raw["sample_rate_hz"], "audio.sample_rate_hz"),
        channels=_optional_int(raw["channels"], "audio.channels"),
        target_bitrate_bps=_optional_int(raw["target_bitrate_bps"], "audio.target_bitrate_bps"),
    )


def _parse_gps(value: object) -> GpsSummary:
    raw = _closed_mapping(value, _GPS_KEYS, "gps")
    raw_samples = raw["samples"]
    if not isinstance(raw_samples, list):
        raise SidecarParseError("gps.samples must be an array")
    if len(raw_samples) > MAX_GPS_SAMPLES:
        raise SidecarParseError("gps.samples exceeds its per-clip bound")
    return GpsSummary(
        available=_strict_bool(raw["available"], "gps.available"),
        first_fix_utc=_optional_utc(raw["first_fix_utc"], "gps.first_fix_utc"),
        samples=tuple(_parse_gps_sample(item) for item in raw_samples),
    )


def _parse_gps_sample(value: object) -> GpsSample:
    raw = _closed_mapping(value, _GPS_SAMPLE_KEYS, "gps.samples[]")
    return GpsSample(
        monotonic_ns=_strict_int(raw["monotonic_ns"], "gps.samples[].monotonic_ns"),
        utc=_optional_utc(raw["utc"], "gps.samples[].utc"),
        timestamp_quality=TimestampQuality(
            _strict_str(raw["timestamp_quality"], "gps.samples[].timestamp_quality")
        ),
        lat_deg=_optional_number(raw["lat_deg"], "gps.samples[].lat_deg"),
        lon_deg=_optional_number(raw["lon_deg"], "gps.samples[].lon_deg"),
        speed_mps=_optional_number(raw["speed_mps"], "gps.samples[].speed_mps"),
        course_deg=_optional_number(raw["course_deg"], "gps.samples[].course_deg"),
        altitude_m=_optional_number(raw["altitude_m"], "gps.samples[].altitude_m"),
        fix_quality=_optional_int(raw["fix_quality"], "gps.samples[].fix_quality"),
        satellites=_optional_int(raw["satellites"], "gps.samples[].satellites"),
        hdop=_optional_number(raw["hdop"], "gps.samples[].hdop"),
    )


def _closed_mapping(value: object, expected_keys: frozenset[str], field: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise SidecarParseError(f"{field} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise SidecarParseError(f"{field} keys must be strings")
    raw = dict(cast(Mapping[str, object], value))
    actual = set(raw)
    if actual != expected_keys:
        raise SidecarParseError(
            f"{field} keys differ; missing={sorted(expected_keys - actual)}, "
            f"unknown={sorted(actual - expected_keys)}"
        )
    return raw


def _strict_str(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise SidecarParseError(f"{field} must be a string")
    return value


def _optional_str(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _strict_str(value, field)


def _strict_bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise SidecarParseError(f"{field} must be boolean")
    return value


def _strict_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SidecarParseError(f"{field} must be an integer")
    return value


def _optional_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    return _strict_int(value, field)


def _strict_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SidecarParseError(f"{field} must be numeric")
    return float(value)


def _optional_number(value: object, field: str) -> float | None:
    if value is None:
        return None
    return _strict_number(value, field)


def _canonical_uuid(value: object, field: str) -> UUID:
    text = _strict_str(value, field)
    try:
        parsed = UUID(text)
    except ValueError as exc:
        raise SidecarParseError(f"{field} must be a canonical UUID") from exc
    if str(parsed) != text:
        raise SidecarParseError(f"{field} must be a canonical UUID")
    return parsed


def _required_utc(value: object, field: str) -> datetime:
    text = _strict_str(value, field)
    if _UTC_RE.fullmatch(text) is None:
        raise SidecarParseError(f"{field} must use canonical UTC milliseconds")
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise SidecarParseError(f"{field} is not a valid UTC timestamp") from exc


def _optional_utc(value: object, field: str) -> datetime | None:
    if value is None:
        return None
    return _required_utc(value, field)


def _optional_local(value: object, field: str) -> datetime | None:
    if value is None:
        return None
    text = _strict_str(value, field)
    if _LOCAL_RE.fullmatch(text) is None:
        raise SidecarParseError(f"{field} must use canonical local milliseconds and offset")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SidecarParseError(f"{field} is not a valid local timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SidecarParseError(f"{field} must include a UTC offset")
    return parsed


def _object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SidecarParseError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_non_json_constant(value: str) -> object:
    raise SidecarParseError(f"non-JSON numeric constant: {value}")


def _quality_for_anchor(anchor: TimeAnchor) -> TimestampQuality:
    if anchor.source is TimeAnchorSource.GPS:
        return TimestampQuality.GPS_ANCHORED
    if anchor.source is TimeAnchorSource.SYSTEM_CLOCK:
        return TimestampQuality.SYSTEM_DERIVED
    raise MetadataReconciliationError("unsupported time-anchor source")


def _resolved_clock_states(
    sidecar: ClipSidecar,
    *,
    anchor: TimeAnchor,
    gps_time_state: GpsTimeState | None,
    system_clock_state: SystemClockState | None,
) -> tuple[GpsTimeState, SystemClockState]:
    if gps_time_state is not None and not isinstance(gps_time_state, GpsTimeState):
        raise MetadataReconciliationError("gps_time_state is invalid")
    if system_clock_state is not None and not isinstance(system_clock_state, SystemClockState):
        raise MetadataReconciliationError("system_clock_state is invalid")
    gps_state = gps_time_state or sidecar.gps_time_state
    system_state = system_clock_state or sidecar.system_clock_state
    if anchor.source is TimeAnchorSource.GPS:
        if gps_time_state is None:
            gps_state = GpsTimeState.GPS_TIME_VALID
        if gps_state is GpsTimeState.UNSYNCED:
            raise MetadataReconciliationError("GPS anchor cannot retain UNSYNCED GPS time")
    else:
        if system_clock_state is None:
            system_state = SystemClockState.SYNCHRONIZED
        if system_state is not SystemClockState.SYNCHRONIZED:
            raise MetadataReconciliationError(
                "system-clock anchor requires SYNCHRONIZED system clock"
            )
    return gps_state, system_state


def _project_utc(anchor: TimeAnchor, monotonic_ns: int) -> datetime:
    delta_ns = monotonic_ns - anchor.monotonic_ns
    seconds, nanoseconds = divmod(delta_ns, _NANOSECONDS_PER_SECOND)
    try:
        return anchor.utc + timedelta(seconds=seconds, microseconds=nanoseconds // 1_000)
    except OverflowError as exc:
        raise MetadataReconciliationError("anchor projection exceeds datetime range") from exc
