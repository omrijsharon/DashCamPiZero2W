"""Pure monotonic-to-UTC anchoring and IANA timezone conversion."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dashcam.state import GpsTimeState, TimestampQuality

_NANOSECONDS_PER_SECOND: Final = 1_000_000_000
_MAX_PROVENANCE_CHARS: Final = 128


class AnchorSource(StrEnum):
    """Provenance of a UTC anchor candidate."""

    GPS_RMC_VALID = "GPS_RMC_VALID"
    GPS_ZDA_NO_VALIDITY_FLAG = "GPS_ZDA_NO_VALIDITY_FLAG"


class AnchorStatus(StrEnum):
    """Result of considering an anchor candidate."""

    ACCEPTED = "ACCEPTED"
    CONFIRMED = "CONFIRMED"
    REACQUIRED = "REACQUIRED"
    IDEMPOTENT = "IDEMPOTENT"
    REJECTED = "REJECTED"


class AnchorError(StrEnum):
    """Stable anchor rejection reasons."""

    INVALID_MONOTONIC = "INVALID_MONOTONIC"
    UTC_NOT_AWARE = "UTC_NOT_AWARE"
    UTC_NOT_CANONICAL = "UTC_NOT_CANONICAL"
    IMPLAUSIBLE_UTC = "IMPLAUSIBLE_UTC"
    INVALID_UNCERTAINTY = "INVALID_UNCERTAINTY"
    INVALID_PROVENANCE = "INVALID_PROVENANCE"
    MONOTONIC_REGRESSION = "MONOTONIC_REGRESSION"
    CONFLICT = "CONFLICT"


class ConversionError(StrEnum):
    """Stable conversion failures."""

    UNSYNCED = "UNSYNCED"
    INVALID_MONOTONIC = "INVALID_MONOTONIC"
    PROJECTION_TOO_LARGE = "PROJECTION_TOO_LARGE"
    DATETIME_RANGE = "DATETIME_RANGE"


class LocalTimeError(StrEnum):
    """Stable local-time conversion failures."""

    UTC_NOT_AWARE = "UTC_NOT_AWARE"
    UTC_NOT_CANONICAL = "UTC_NOT_CANONICAL"
    INVALID_ZONE_NAME = "INVALID_ZONE_NAME"
    UNKNOWN_ZONE = "UNKNOWN_ZONE"


@dataclass(frozen=True, slots=True)
class AnchorCandidate:
    """One UTC observation paired with a monotonic timestamp."""

    monotonic_ns: int
    utc: datetime
    source: AnchorSource
    provenance: str
    uncertainty_ns: int


@dataclass(frozen=True, slots=True)
class AnchorPolicy:
    """Configured bounds; no wall-clock value is consulted implicitly."""

    earliest_utc: datetime
    latest_utc: datetime
    max_uncertainty_ns: int = 5 * _NANOSECONDS_PER_SECOND
    max_conflict_ns: int = 2 * _NANOSECONDS_PER_SECOND
    max_reacquire_disagreement_ns: int = 5 * _NANOSECONDS_PER_SECOND
    max_anchor_interval_ns: int = 24 * 60 * 60 * _NANOSECONDS_PER_SECOND
    max_projection_ns: int = 14 * 24 * 60 * 60 * _NANOSECONDS_PER_SECOND
    gps_stale_after_ns: int = 5 * _NANOSECONDS_PER_SECOND
    oscillator_uncertainty_ppb: int = 100_000


@dataclass(frozen=True, slots=True)
class UtcAnchor:
    """A validated anchor retained as stable canonical provenance."""

    monotonic_ns: int
    utc: datetime
    source: AnchorSource
    provenance: str
    uncertainty_ns: int


@dataclass(frozen=True, slots=True)
class MonotonicUtcClock:
    """Immutable anchor state.

    Consistent nearby observations refresh ``latest_confirmation`` while the
    original anchor stays stable, avoiding repeated timestamp/filename shifts.
    After the configured continuity window, a plausible trusted candidate
    explicitly reacquires the clock so a GPS disconnect is recoverable.
    """

    anchor: UtcAnchor | None = None
    latest_confirmation: UtcAnchor | None = None
    latest_disagreement_ns: int | None = None

    @property
    def timestamp_quality(self) -> TimestampQuality:
        if self.anchor is None:
            return TimestampQuality.MONOTONIC_ONLY
        return TimestampQuality.GPS_ANCHORED

    def consider(
        self,
        candidate: AnchorCandidate,
        policy: AnchorPolicy,
    ) -> AnchorOutcome:
        """Return new immutable state plus an explicit accept/reject result."""

        policy_error = _validate_policy(policy)
        if policy_error is not None:
            return AnchorOutcome(self, AnchorStatus.REJECTED, policy_error)
        normalized, error = _validate_candidate(candidate, policy)
        if normalized is None:
            return AnchorOutcome(self, AnchorStatus.REJECTED, error)

        if self.anchor is None:
            updated = MonotonicUtcClock(
                anchor=normalized,
                latest_confirmation=normalized,
                latest_disagreement_ns=0,
            )
            return AnchorOutcome(updated, AnchorStatus.ACCEPTED)

        latest = self.latest_confirmation or self.anchor
        if normalized == latest:
            return AnchorOutcome(self, AnchorStatus.IDEMPOTENT, disagreement_ns=0)
        if normalized.monotonic_ns < latest.monotonic_ns:
            return AnchorOutcome(
                self,
                AnchorStatus.REJECTED,
                AnchorError.MONOTONIC_REGRESSION,
            )
        interval_ns = normalized.monotonic_ns - latest.monotonic_ns
        predicted = _project(self.anchor.utc, normalized.monotonic_ns - self.anchor.monotonic_ns)
        if predicted is None:
            return AnchorOutcome(self, AnchorStatus.REJECTED, AnchorError.IMPLAUSIBLE_UTC)
        disagreement_ns = _timedelta_to_ns(abs(normalized.utc - predicted))
        if (
            interval_ns > policy.max_anchor_interval_ns
            and disagreement_ns > policy.max_reacquire_disagreement_ns
        ):
            return AnchorOutcome(
                self,
                AnchorStatus.REJECTED,
                AnchorError.CONFLICT,
                disagreement_ns,
            )
        if interval_ns > policy.max_anchor_interval_ns:
            reacquired = MonotonicUtcClock(
                anchor=normalized,
                latest_confirmation=normalized,
                latest_disagreement_ns=disagreement_ns,
            )
            return AnchorOutcome(
                reacquired,
                AnchorStatus.REACQUIRED,
                disagreement_ns=disagreement_ns,
            )

        drift_allowance_ns = (
            abs(normalized.monotonic_ns - self.anchor.monotonic_ns)
            * policy.oscillator_uncertainty_ppb
            // _NANOSECONDS_PER_SECOND
        )
        allowed_ns = (
            policy.max_conflict_ns
            + self.anchor.uncertainty_ns
            + normalized.uncertainty_ns
            + drift_allowance_ns
        )
        if disagreement_ns > allowed_ns:
            return AnchorOutcome(
                self,
                AnchorStatus.REJECTED,
                AnchorError.CONFLICT,
                disagreement_ns,
            )
        if normalized.monotonic_ns == latest.monotonic_ns:
            return AnchorOutcome(self, AnchorStatus.IDEMPOTENT, disagreement_ns=disagreement_ns)

        updated = replace(
            self,
            latest_confirmation=normalized,
            latest_disagreement_ns=disagreement_ns,
        )
        return AnchorOutcome(updated, AnchorStatus.CONFIRMED, disagreement_ns=disagreement_ns)

    def convert(self, monotonic_ns: int, policy: AnchorPolicy) -> UtcConversionOutcome:
        """Convert a monotonic value using the stable anchor."""

        if self.anchor is None:
            return UtcConversionOutcome(error=ConversionError.UNSYNCED)
        if isinstance(monotonic_ns, bool) or monotonic_ns < 0:
            return UtcConversionOutcome(error=ConversionError.INVALID_MONOTONIC)
        delta_ns = monotonic_ns - self.anchor.monotonic_ns
        if abs(delta_ns) > policy.max_projection_ns:
            return UtcConversionOutcome(error=ConversionError.PROJECTION_TOO_LARGE)
        utc = _project(self.anchor.utc, delta_ns)
        if utc is None:
            return UtcConversionOutcome(error=ConversionError.DATETIME_RANGE)
        drift_uncertainty_ns = (
            abs(delta_ns) * policy.oscillator_uncertainty_ppb // _NANOSECONDS_PER_SECOND
        )
        return UtcConversionOutcome(
            estimate=TimestampEstimate(
                utc=utc,
                quality=TimestampQuality.GPS_ANCHORED,
                source=self.anchor.source,
                provenance=self.anchor.provenance,
                uncertainty_ns=self.anchor.uncertainty_ns + drift_uncertainty_ns,
            )
        )

    def gps_age_ns(self, now_monotonic_ns: int) -> int | None:
        """Return age of the latest accepted GPS evidence, or ``None`` if unavailable."""

        latest = self.latest_confirmation
        if (
            latest is None
            or isinstance(now_monotonic_ns, bool)
            or now_monotonic_ns < latest.monotonic_ns
        ):
            return None
        return now_monotonic_ns - latest.monotonic_ns

    def gps_time_state(self, now_monotonic_ns: int, policy: AnchorPolicy) -> GpsTimeState:
        """Return GPS time freshness without reusing stale observations indefinitely."""

        if self.latest_confirmation is None:
            return GpsTimeState.UNSYNCED
        age_ns = self.gps_age_ns(now_monotonic_ns)
        if age_ns is None or policy.gps_stale_after_ns < 0:
            return GpsTimeState.GPS_TIME_STALE
        if age_ns > policy.gps_stale_after_ns:
            return GpsTimeState.GPS_TIME_STALE
        return GpsTimeState.GPS_TIME_VALID


@dataclass(frozen=True, slots=True)
class AnchorOutcome:
    """Result of considering one candidate."""

    clock: MonotonicUtcClock
    status: AnchorStatus
    error: AnchorError | None = None
    disagreement_ns: int | None = None

    @property
    def accepted(self) -> bool:
        return self.status is not AnchorStatus.REJECTED


@dataclass(frozen=True, slots=True)
class TimestampEstimate:
    """UTC derived from a monotonic timestamp, with its provenance."""

    utc: datetime
    quality: TimestampQuality
    source: AnchorSource
    provenance: str
    uncertainty_ns: int


@dataclass(frozen=True, slots=True)
class UtcConversionOutcome:
    """Explicit monotonic-to-UTC conversion result."""

    estimate: TimestampEstimate | None = None
    error: ConversionError | None = None

    @property
    def ok(self) -> bool:
        return self.estimate is not None and self.error is None


@dataclass(frozen=True, slots=True)
class LocalTimeView:
    """IANA-derived local display fields for one canonical UTC instant."""

    datetime: datetime
    zone_name: str
    utc_offset_seconds: int
    abbreviation: str
    is_dst: bool


@dataclass(frozen=True, slots=True)
class LocalTimeOutcome:
    """Explicit timezone conversion result."""

    local: LocalTimeView | None = None
    error: LocalTimeError | None = None

    @property
    def ok(self) -> bool:
        return self.local is not None and self.error is None


def to_local_time(utc: datetime, zone_name: str) -> LocalTimeOutcome:
    """Convert canonical UTC with the installed IANA database and ``zoneinfo``."""

    if not _is_aware(utc):
        return LocalTimeOutcome(error=LocalTimeError.UTC_NOT_AWARE)
    if utc.utcoffset() != timedelta(0):
        return LocalTimeOutcome(error=LocalTimeError.UTC_NOT_CANONICAL)
    if not zone_name or len(zone_name) > 128 or not zone_name.isascii():
        return LocalTimeOutcome(error=LocalTimeError.INVALID_ZONE_NAME)
    try:
        zone = ZoneInfo(zone_name)
    except (ValueError, ZoneInfoNotFoundError):
        return LocalTimeOutcome(error=LocalTimeError.UNKNOWN_ZONE)
    local = utc.astimezone(zone)
    offset = local.utcoffset()
    dst = local.dst()
    if offset is None:
        return LocalTimeOutcome(error=LocalTimeError.UNKNOWN_ZONE)
    return LocalTimeOutcome(
        local=LocalTimeView(
            datetime=local,
            zone_name=zone.key,
            utc_offset_seconds=int(offset.total_seconds()),
            abbreviation=local.tzname() or "",
            is_dst=dst is not None and dst != timedelta(0),
        )
    )


def _validate_policy(policy: AnchorPolicy) -> AnchorError | None:
    if not _is_aware(policy.earliest_utc) or not _is_aware(policy.latest_utc):
        return AnchorError.UTC_NOT_AWARE
    if policy.earliest_utc.utcoffset() != timedelta(
        0
    ) or policy.latest_utc.utcoffset() != timedelta(0):
        return AnchorError.UTC_NOT_CANONICAL
    if policy.earliest_utc > policy.latest_utc:
        return AnchorError.IMPLAUSIBLE_UTC
    numeric_bounds = (
        policy.max_uncertainty_ns,
        policy.max_conflict_ns,
        policy.max_reacquire_disagreement_ns,
        policy.max_anchor_interval_ns,
        policy.max_projection_ns,
        policy.gps_stale_after_ns,
        policy.oscillator_uncertainty_ppb,
    )
    if any(isinstance(value, bool) or value < 0 for value in numeric_bounds):
        return AnchorError.INVALID_UNCERTAINTY
    return None


def _validate_candidate(
    candidate: AnchorCandidate,
    policy: AnchorPolicy,
) -> tuple[UtcAnchor | None, AnchorError | None]:
    if isinstance(candidate.monotonic_ns, bool) or candidate.monotonic_ns < 0:
        return None, AnchorError.INVALID_MONOTONIC
    if not _is_aware(candidate.utc):
        return None, AnchorError.UTC_NOT_AWARE
    if candidate.utc.utcoffset() != timedelta(0):
        return None, AnchorError.UTC_NOT_CANONICAL
    normalized_utc = candidate.utc.astimezone(UTC)
    if not policy.earliest_utc <= normalized_utc <= policy.latest_utc:
        return None, AnchorError.IMPLAUSIBLE_UTC
    if (
        isinstance(candidate.uncertainty_ns, bool)
        or candidate.uncertainty_ns < 0
        or candidate.uncertainty_ns > policy.max_uncertainty_ns
    ):
        return None, AnchorError.INVALID_UNCERTAINTY
    if (
        not candidate.provenance
        or len(candidate.provenance) > _MAX_PROVENANCE_CHARS
        or not candidate.provenance.isascii()
        or not candidate.provenance.isprintable()
    ):
        return None, AnchorError.INVALID_PROVENANCE
    return (
        UtcAnchor(
            monotonic_ns=candidate.monotonic_ns,
            utc=normalized_utc,
            source=candidate.source,
            provenance=candidate.provenance,
            uncertainty_ns=candidate.uncertainty_ns,
        ),
        None,
    )


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _project(anchor_utc: datetime, delta_ns: int) -> datetime | None:
    try:
        seconds, nanoseconds = divmod(delta_ns, _NANOSECONDS_PER_SECOND)
        return anchor_utc + timedelta(seconds=seconds, microseconds=nanoseconds // 1_000)
    except OverflowError:
        return None


def _timedelta_to_ns(value: timedelta) -> int:
    return (
        value.days * 86_400 * _NANOSECONDS_PER_SECOND
        + value.seconds * _NANOSECONDS_PER_SECOND
        + value.microseconds * 1_000
    )
