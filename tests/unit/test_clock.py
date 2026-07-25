from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from dashcam.gps.clock import (
    AnchorCandidate,
    AnchorError,
    AnchorPolicy,
    AnchorSource,
    AnchorStatus,
    ConversionError,
    LocalTimeError,
    MonotonicUtcClock,
    to_local_time,
)
from dashcam.state import GpsTimeState, TimestampQuality

_SECOND = 1_000_000_000


@pytest.fixture
def policy() -> AnchorPolicy:
    return AnchorPolicy(
        earliest_utc=datetime(2025, 1, 1, tzinfo=UTC),
        latest_utc=datetime(2028, 1, 1, tzinfo=UTC),
        max_uncertainty_ns=2 * _SECOND,
        max_conflict_ns=100_000_000,
        max_reacquire_disagreement_ns=2 * _SECOND,
        max_anchor_interval_ns=60 * _SECOND,
        max_projection_ns=24 * 60 * 60 * _SECOND,
        gps_stale_after_ns=5 * _SECOND,
        oscillator_uncertainty_ppb=100_000,
    )


def _candidate(
    monotonic_ns: int = 10 * _SECOND,
    utc: datetime = datetime(2026, 7, 23, 12, 0, tzinfo=UTC),
    *,
    uncertainty_ns: int = 20_000_000,
    source: AnchorSource = AnchorSource.GPS_RMC_VALID,
    provenance: str = "GPRMC",
) -> AnchorCandidate:
    return AnchorCandidate(
        monotonic_ns=monotonic_ns,
        utc=utc,
        source=source,
        provenance=provenance,
        uncertainty_ns=uncertainty_ns,
    )


def test_first_anchor_acceptance_and_bidirectional_conversion(policy: AnchorPolicy) -> None:
    outcome = MonotonicUtcClock().consider(_candidate(), policy)

    assert outcome.accepted
    assert outcome.status is AnchorStatus.ACCEPTED
    assert outcome.clock.timestamp_quality is TimestampQuality.GPS_ANCHORED

    later = outcome.clock.convert(12 * _SECOND, policy)
    earlier = outcome.clock.convert(9 * _SECOND, policy)
    assert later.estimate is not None
    assert earlier.estimate is not None
    assert later.estimate.utc == datetime(2026, 7, 23, 12, 0, 2, tzinfo=UTC)
    assert earlier.estimate.utc == datetime(2026, 7, 23, 11, 59, 59, tzinfo=UTC)
    assert later.estimate.quality is TimestampQuality.GPS_ANCHORED
    assert later.estimate.provenance == "GPRMC"
    assert later.estimate.uncertainty_ns == 20_200_000


def test_unsynced_and_projection_bounds_are_explicit(policy: AnchorPolicy) -> None:
    assert MonotonicUtcClock().timestamp_quality is TimestampQuality.MONOTONIC_ONLY
    assert MonotonicUtcClock().convert(0, policy).error is ConversionError.UNSYNCED
    clock = MonotonicUtcClock().consider(_candidate(), policy).clock

    assert clock.convert(-1, policy).error is ConversionError.INVALID_MONOTONIC
    too_far = 10 * _SECOND + policy.max_projection_ns + 1
    assert clock.convert(too_far, policy).error is ConversionError.PROJECTION_TOO_LARGE


@pytest.mark.parametrize(
    ("candidate", "error"),
    [
        (_candidate(monotonic_ns=-1), AnchorError.INVALID_MONOTONIC),
        (_candidate(utc=datetime(2026, 1, 1)), AnchorError.UTC_NOT_AWARE),
        (
            _candidate(utc=datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=2)))),
            AnchorError.UTC_NOT_CANONICAL,
        ),
        (_candidate(utc=datetime(2020, 1, 1, tzinfo=UTC)), AnchorError.IMPLAUSIBLE_UTC),
        (_candidate(uncertainty_ns=3 * _SECOND), AnchorError.INVALID_UNCERTAINTY),
        (_candidate(provenance=""), AnchorError.INVALID_PROVENANCE),
        (_candidate(provenance="x" * 129), AnchorError.INVALID_PROVENANCE),
    ],
)
def test_invalid_candidates_are_rejected(
    policy: AnchorPolicy,
    candidate: AnchorCandidate,
    error: AnchorError,
) -> None:
    outcome = MonotonicUtcClock().consider(candidate, policy)

    assert not outcome.accepted
    assert outcome.status is AnchorStatus.REJECTED
    assert outcome.error is error
    assert outcome.clock.anchor is None


def test_zda_anchor_retains_explicit_caveated_provenance(policy: AnchorPolicy) -> None:
    candidate = _candidate(
        source=AnchorSource.GPS_ZDA_NO_VALIDITY_FLAG,
        provenance="GNZDA:complete:no-validity-flag",
    )
    outcome = MonotonicUtcClock().consider(candidate, policy)

    assert outcome.status is AnchorStatus.ACCEPTED
    assert outcome.clock.anchor is not None
    assert outcome.clock.anchor.source is AnchorSource.GPS_ZDA_NO_VALIDITY_FLAG


def test_exact_candidate_is_idempotent(policy: AnchorPolicy) -> None:
    candidate = _candidate()
    first = MonotonicUtcClock().consider(candidate, policy)
    second = first.clock.consider(candidate, policy)

    assert second.status is AnchorStatus.IDEMPOTENT
    assert second.clock is first.clock
    assert second.disagreement_ns == 0


def test_consistent_observation_refreshes_freshness_without_moving_anchor(
    policy: AnchorPolicy,
) -> None:
    first = MonotonicUtcClock().consider(_candidate(), policy)
    confirmation = _candidate(
        monotonic_ns=14 * _SECOND,
        utc=datetime(2026, 7, 23, 12, 0, 4, 10_000, tzinfo=UTC),
        provenance="GNZDA",
        source=AnchorSource.GPS_ZDA_NO_VALIDITY_FLAG,
    )
    second = first.clock.consider(confirmation, policy)

    assert second.status is AnchorStatus.CONFIRMED
    assert second.disagreement_ns == 10_000_000
    assert second.clock.anchor is first.clock.anchor
    assert second.clock.latest_confirmation is not None
    assert second.clock.latest_confirmation.provenance == "GNZDA"
    assert second.clock.latest_disagreement_ns == 10_000_000
    estimate = second.clock.convert(14 * _SECOND, policy)
    assert estimate.estimate is not None
    assert estimate.estimate.utc == datetime(2026, 7, 23, 12, 0, 4, tzinfo=UTC)


def test_conflicts_and_regressions_do_not_replace_anchor(policy: AnchorPolicy) -> None:
    clock = MonotonicUtcClock().consider(_candidate(), policy).clock
    conflict = clock.consider(
        _candidate(
            monotonic_ns=11 * _SECOND,
            utc=datetime(2026, 7, 23, 12, 0, 3, tzinfo=UTC),
        ),
        policy,
    )
    regression = clock.consider(_candidate(monotonic_ns=9 * _SECOND), policy)

    assert conflict.error is AnchorError.CONFLICT
    assert conflict.disagreement_ns == 2 * _SECOND
    assert regression.error is AnchorError.MONOTONIC_REGRESSION
    assert conflict.clock is clock
    assert regression.clock is clock


def test_plausible_anchor_reacquires_after_continuity_window(policy: AnchorPolicy) -> None:
    original = MonotonicUtcClock().consider(_candidate(), policy).clock
    outcome = original.consider(
        _candidate(
            monotonic_ns=71 * _SECOND,
            utc=datetime(2026, 7, 23, 12, 1, 1, 500_000, tzinfo=UTC),
            provenance="GPRMC:reconnected",
        ),
        policy,
    )

    assert outcome.status is AnchorStatus.REACQUIRED
    assert outcome.disagreement_ns == 500_000_000
    assert outcome.clock.anchor is not original.anchor
    assert outcome.clock.anchor is not None
    assert outcome.clock.anchor.provenance == "GPRMC:reconnected"
    assert outcome.clock.latest_disagreement_ns == 500_000_000


def test_reacquisition_still_rejects_a_large_utc_discontinuity(policy: AnchorPolicy) -> None:
    original = MonotonicUtcClock().consider(_candidate(), policy).clock
    outcome = original.consider(
        _candidate(
            monotonic_ns=71 * _SECOND,
            utc=datetime(2026, 7, 23, 13, 1, 1, tzinfo=UTC),
            provenance="GPRMC:implausible-reconnect",
        ),
        policy,
    )

    assert outcome.status is AnchorStatus.REJECTED
    assert outcome.error is AnchorError.CONFLICT
    assert outcome.clock is original


def test_gps_freshness_uses_latest_consistent_evidence(policy: AnchorPolicy) -> None:
    clock = MonotonicUtcClock()
    assert clock.gps_time_state(0, policy) is GpsTimeState.UNSYNCED
    clock = clock.consider(_candidate(), policy).clock

    assert clock.gps_age_ns(15 * _SECOND) == 5 * _SECOND
    assert clock.gps_age_ns(9 * _SECOND) is None
    assert clock.gps_time_state(15 * _SECOND, policy) is GpsTimeState.GPS_TIME_VALID
    assert clock.gps_time_state(15 * _SECOND + 1, policy) is GpsTimeState.GPS_TIME_STALE
    assert clock.gps_time_state(9 * _SECOND, policy) is GpsTimeState.GPS_TIME_STALE


@pytest.mark.parametrize(
    ("utc", "expected_local", "offset_seconds", "is_dst"),
    [
        (
            datetime(2026, 3, 26, 23, 59, tzinfo=UTC),
            datetime(2026, 3, 27, 1, 59),
            2 * 3600,
            False,
        ),
        (
            datetime(2026, 3, 27, 0, 0, tzinfo=UTC),
            datetime(2026, 3, 27, 3, 0),
            3 * 3600,
            True,
        ),
        (
            datetime(2026, 3, 27, 0, 1, tzinfo=UTC),
            datetime(2026, 3, 27, 3, 1),
            3 * 3600,
            True,
        ),
        (
            datetime(2026, 10, 24, 22, 59, tzinfo=UTC),
            datetime(2026, 10, 25, 1, 59),
            3 * 3600,
            True,
        ),
        (
            datetime(2026, 10, 24, 23, 0, tzinfo=UTC),
            datetime(2026, 10, 25, 1, 0, fold=1),
            2 * 3600,
            False,
        ),
        (
            datetime(2026, 10, 24, 23, 1, tzinfo=UTC),
            datetime(2026, 10, 25, 1, 1, fold=1),
            2 * 3600,
            False,
        ),
    ],
)
def test_asia_jerusalem_iana_dst_transitions(
    utc: datetime,
    expected_local: datetime,
    offset_seconds: int,
    is_dst: bool,
) -> None:
    outcome = to_local_time(utc, "Asia/Jerusalem")

    assert outcome.local is not None
    assert outcome.local.datetime.replace(tzinfo=None) == expected_local
    assert outcome.local.datetime.fold == expected_local.fold
    assert outcome.local.utc_offset_seconds == offset_seconds
    assert outcome.local.is_dst is is_dst
    assert outcome.local.zone_name == "Asia/Jerusalem"


@pytest.mark.parametrize(
    ("utc", "local_date"),
    [
        (datetime(2026, 7, 23, 20, 59, 59, tzinfo=UTC), (2026, 7, 23)),
        (datetime(2026, 7, 23, 21, 0, 0, tzinfo=UTC), (2026, 7, 24)),
        (datetime(2026, 7, 24, 0, 0, 0, tzinfo=UTC), (2026, 7, 24)),
    ],
)
def test_utc_midnight_and_local_date_rollover(
    utc: datetime,
    local_date: tuple[int, int, int],
) -> None:
    outcome = to_local_time(utc, "Asia/Jerusalem")

    assert outcome.local is not None
    assert (
        outcome.local.datetime.year,
        outcome.local.datetime.month,
        outcome.local.datetime.day,
    ) == local_date


def test_timezone_failures_are_explicit() -> None:
    assert to_local_time(datetime(2026, 1, 1), "UTC").error is LocalTimeError.UTC_NOT_AWARE
    non_utc = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=1)))
    assert to_local_time(non_utc, "UTC").error is LocalTimeError.UTC_NOT_CANONICAL
    canonical = datetime(2026, 1, 1, tzinfo=UTC)
    assert to_local_time(canonical, "").error is LocalTimeError.INVALID_ZONE_NAME
    assert (
        to_local_time(canonical, "Etc/Definitely-Not-A-Zone").error is LocalTimeError.UNKNOWN_ZONE
    )
