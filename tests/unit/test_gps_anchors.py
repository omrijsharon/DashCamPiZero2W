from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from dashcam.gps.anchors import NmeaAnchorError, NmeaAnchorTracker
from dashcam.gps.clock import AnchorError, AnchorPolicy, AnchorSource, AnchorStatus
from dashcam.gps.nmea import NmeaSentence, SentenceType, TimeTrust, parse_nmea_line
from dashcam.state import GpsTimeState

_SECOND = 1_000_000_000


@pytest.fixture
def policy() -> AnchorPolicy:
    return AnchorPolicy(
        earliest_utc=datetime(2025, 1, 1, tzinfo=UTC),
        latest_utc=datetime(2028, 1, 1, tzinfo=UTC),
        max_uncertainty_ns=20_000_000,
        max_conflict_ns=100_000_000,
        max_reacquire_disagreement_ns=2 * _SECOND,
        max_anchor_interval_ns=60 * _SECOND,
        max_projection_ns=24 * 60 * 60 * _SECOND,
        gps_stale_after_ns=5 * _SECOND,
        oscillator_uncertainty_ppb=100_000,
    )


def _line(body: str) -> str:
    checksum = 0
    for character in body:
        checksum ^= ord(character)
    return f"${body}*{checksum:02X}"


def _parsed(body: str, monotonic_ns: int) -> NmeaSentence:
    outcome = parse_nmea_line(_line(body), received_monotonic_ns=monotonic_ns)
    assert outcome.ok
    assert outcome.sentence is not None
    return outcome.sentence


def _tracker(policy: AnchorPolicy, **changes: object) -> NmeaAnchorTracker:
    values: dict[str, object] = {"policy": policy, "uncertainty_ns": 10_000_000}
    values.update(changes)
    return NmeaAnchorTracker(**values)  # type: ignore[arg-type]


def test_active_rmc_becomes_a_deterministic_bounded_candidate(policy: AnchorPolicy) -> None:
    tracker = _tracker(policy)
    sentence = _parsed("GPRMC,123519.250,A,,,,,0.0,0.0,230726", 42)

    outcome = tracker.candidate_from(sentence)

    assert outcome.ok
    assert outcome.candidate is not None
    assert outcome.candidate.monotonic_ns == 42
    assert outcome.candidate.utc == datetime(2026, 7, 23, 12, 35, 19, 250_000, tzinfo=UTC)
    assert outcome.candidate.source is AnchorSource.GPS_RMC_VALID
    assert outcome.candidate.provenance == "NMEA:GPRMC:active-valid:complete-utc"
    assert outcome.candidate.uncertainty_ns == 10_000_000
    assert len(outcome.candidate.provenance) <= 128


def test_complete_zda_has_its_caveated_source_and_deterministic_provenance(
    policy: AnchorPolicy,
) -> None:
    tracker = _tracker(policy)
    sentence = _parsed("GNZDA,123520.00,23,07,2026,00,00", 43)

    outcome = tracker.candidate_from(sentence)

    assert outcome.ok
    assert outcome.candidate is not None
    assert outcome.candidate.source is AnchorSource.GPS_ZDA_NO_VALIDITY_FLAG
    assert outcome.candidate.provenance == "NMEA:GNZDA:complete:no-validity-flag"


@pytest.mark.parametrize(
    ("sentence", "error"),
    [
        (
            _parsed("GPRMC,123519,V,,,,,0.0,0.0,230726", 10),
            NmeaAnchorError.RMC_NOT_ACTIVE_VALID,
        ),
        (
            _parsed("GPGGA,123519,,,,,0,,,,M,,,", 10),
            NmeaAnchorError.UNSUPPORTED_SENTENCE,
        ),
        (
            NmeaSentence(
                sentence_type=SentenceType.RMC,
                talker="GP",
                received_monotonic_ns=10,
                time_trust=TimeTrust.RMC_STATUS_VALID,
            ),
            NmeaAnchorError.INCOMPLETE_UTC,
        ),
        (
            NmeaSentence(
                sentence_type=SentenceType.ZDA,
                talker="GP",
                received_monotonic_ns=10,
                time_trust=TimeTrust.UNAVAILABLE,
            ),
            NmeaAnchorError.ZDA_NOT_COMPLETE,
        ),
    ],
)
def test_void_incomplete_or_unsupported_sentences_refuse_explicitly(
    policy: AnchorPolicy,
    sentence: NmeaSentence,
    error: NmeaAnchorError,
) -> None:
    outcome = _tracker(policy).candidate_from(sentence)

    assert not outcome.ok
    assert outcome.candidate is None
    assert outcome.error is error


@pytest.mark.parametrize("received_monotonic_ns", [None, -1, True])
def test_missing_or_invalid_received_monotonic_time_refuses(
    policy: AnchorPolicy,
    received_monotonic_ns: int | None,
) -> None:
    sentence = replace(
        _parsed("GPRMC,123519,A,,,,,0.0,0.0,230726", 10),
        received_monotonic_ns=received_monotonic_ns,
    )

    assert _tracker(policy).candidate_from(sentence).error is NmeaAnchorError.INVALID_MONOTONIC


@pytest.mark.parametrize("uncertainty_ns", [0, -1, True, 20_000_001])
def test_uncertainty_must_be_positive_and_within_clock_policy_bound(
    policy: AnchorPolicy,
    uncertainty_ns: int,
) -> None:
    sentence = _parsed("GPRMC,123519,A,,,,,0.0,0.0,230726", 10)

    assert (
        _tracker(policy, uncertainty_ns=uncertainty_ns).candidate_from(sentence).error
        is NmeaAnchorError.INVALID_UNCERTAINTY
    )


def test_candidate_uses_the_exact_policy_bound(policy: AnchorPolicy) -> None:
    sentence = _parsed("GPRMC,123519,A,,,,,0.0,0.0,230726", 10)
    outcome = _tracker(policy, uncertainty_ns=policy.max_uncertainty_ns).candidate_from(sentence)

    assert outcome.ok


def test_consider_preserves_clock_conflict_and_reacquisition_semantics(
    policy: AnchorPolicy,
) -> None:
    first = _tracker(policy).consider(_parsed("GPRMC,120000,A,,,,,0.0,0.0,230726", 10 * _SECOND))
    assert first.clock_outcome is not None
    assert first.clock_outcome.status is AnchorStatus.ACCEPTED

    conflict = first.tracker.consider(
        _parsed("GPRMC,120003,A,,,,,0.0,0.0,230726", 11 * _SECOND)
    )
    assert conflict.clock_outcome is not None
    assert conflict.clock_outcome.status is AnchorStatus.REJECTED
    assert conflict.clock_outcome.error is AnchorError.CONFLICT
    assert conflict.tracker.clock is first.tracker.clock

    reacquired = first.tracker.consider(
        _parsed("GPRMC,120101.500,A,,,,,0.0,0.0,230726", 71 * _SECOND)
    )
    assert reacquired.clock_outcome is not None
    assert reacquired.clock_outcome.status is AnchorStatus.REACQUIRED
    assert reacquired.accepted
    assert reacquired.tracker.clock.anchor is not first.tracker.clock.anchor


def test_freshness_delegates_to_the_clock(policy: AnchorPolicy) -> None:
    accepted = _tracker(policy).consider(_parsed("GPRMC,120000,A,,,,,0.0,0.0,230726", 10))

    assert accepted.tracker.gps_time_state(10 + 5 * _SECOND) is GpsTimeState.GPS_TIME_VALID
    assert accepted.tracker.gps_time_state(11 + 5 * _SECOND) is GpsTimeState.GPS_TIME_STALE
