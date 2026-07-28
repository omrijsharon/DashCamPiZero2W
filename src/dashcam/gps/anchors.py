"""Pure adaptation of verified NMEA UTC observations to the anchor clock."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum

from dashcam.gps.clock import (
    AnchorCandidate,
    AnchorOutcome,
    AnchorPolicy,
    AnchorSource,
    MonotonicUtcClock,
)
from dashcam.gps.nmea import NmeaSentence, SentenceType, TimeTrust
from dashcam.state import GpsTimeState

_MAX_PROVENANCE_CHARS = 128


class NmeaAnchorError(StrEnum):
    """Stable refusal reasons before the clock considers an observation."""

    INVALID_UNCERTAINTY = "INVALID_UNCERTAINTY"
    INVALID_MONOTONIC = "INVALID_MONOTONIC"
    INVALID_SENTENCE = "INVALID_SENTENCE"
    UNSUPPORTED_SENTENCE = "UNSUPPORTED_SENTENCE"
    RMC_NOT_ACTIVE_VALID = "RMC_NOT_ACTIVE_VALID"
    ZDA_NOT_COMPLETE = "ZDA_NOT_COMPLETE"
    INCOMPLETE_UTC = "INCOMPLETE_UTC"


@dataclass(frozen=True, slots=True)
class NmeaCandidateOutcome:
    """The explicit result of adapting one checksum-verified NMEA sentence."""

    candidate: AnchorCandidate | None = None
    error: NmeaAnchorError | None = None

    @property
    def ok(self) -> bool:
        return self.candidate is not None and self.error is None


@dataclass(frozen=True, slots=True)
class NmeaAnchorOutcome:
    """Candidate adaptation followed by the unmodified clock-policy outcome."""

    tracker: NmeaAnchorTracker
    candidate_outcome: NmeaCandidateOutcome
    clock_outcome: AnchorOutcome | None = None

    @property
    def candidate(self) -> AnchorCandidate | None:
        return self.candidate_outcome.candidate

    @property
    def error(self) -> NmeaAnchorError | None:
        return self.candidate_outcome.error

    @property
    def accepted(self) -> bool:
        return self.clock_outcome is not None and self.clock_outcome.accepted


@dataclass(frozen=True, slots=True)
class NmeaAnchorTracker:
    """Immutable NMEA-to-clock coordinator with no transport or wall-clock I/O.

    ``uncertainty_ns`` is deliberately fixed at construction instead of inferred
    from a NMEA sentence. The adapter only accepts a strictly positive value
    that is within the supplied clock policy; all UTC plausibility, continuity,
    conflict, reacquisition, and freshness policy remains in
    :class:`MonotonicUtcClock`.
    """

    policy: AnchorPolicy
    uncertainty_ns: int
    clock: MonotonicUtcClock = field(default_factory=MonotonicUtcClock)

    def candidate_from(self, sentence: NmeaSentence) -> NmeaCandidateOutcome:
        """Adapt exactly the trusted complete RMC and ZDA forms to a candidate."""

        if not _valid_uncertainty(self.uncertainty_ns, self.policy):
            return NmeaCandidateOutcome(error=NmeaAnchorError.INVALID_UNCERTAINTY)

        received_ns = sentence.received_monotonic_ns
        if (
            isinstance(received_ns, bool)
            or not isinstance(received_ns, int)
            or received_ns < 0
        ):
            return NmeaCandidateOutcome(error=NmeaAnchorError.INVALID_MONOTONIC)

        if not _valid_talker(sentence.talker):
            return NmeaCandidateOutcome(error=NmeaAnchorError.INVALID_SENTENCE)

        source: AnchorSource
        provenance: str
        if sentence.sentence_type is SentenceType.RMC:
            if sentence.time_trust is not TimeTrust.RMC_STATUS_VALID:
                return NmeaCandidateOutcome(error=NmeaAnchorError.RMC_NOT_ACTIVE_VALID)
            if sentence.utc_datetime is None:
                return NmeaCandidateOutcome(error=NmeaAnchorError.INCOMPLETE_UTC)
            source = AnchorSource.GPS_RMC_VALID
            provenance = f"NMEA:{sentence.talker}RMC:active-valid:complete-utc"
        elif sentence.sentence_type is SentenceType.ZDA:
            if sentence.time_trust is not TimeTrust.ZDA_REQUIRES_PLAUSIBILITY:
                return NmeaCandidateOutcome(error=NmeaAnchorError.ZDA_NOT_COMPLETE)
            if sentence.utc_datetime is None:
                return NmeaCandidateOutcome(error=NmeaAnchorError.INCOMPLETE_UTC)
            source = AnchorSource.GPS_ZDA_NO_VALIDITY_FLAG
            provenance = f"NMEA:{sentence.talker}ZDA:complete:no-validity-flag"
        elif sentence.sentence_type is SentenceType.GGA:
            return NmeaCandidateOutcome(error=NmeaAnchorError.UNSUPPORTED_SENTENCE)
        else:
            return NmeaCandidateOutcome(error=NmeaAnchorError.INVALID_SENTENCE)

        if not sentence.time_anchor_candidate or len(provenance) > _MAX_PROVENANCE_CHARS:
            return NmeaCandidateOutcome(error=NmeaAnchorError.INCOMPLETE_UTC)
        return NmeaCandidateOutcome(
            candidate=AnchorCandidate(
                monotonic_ns=received_ns,
                utc=sentence.utc_datetime,
                source=source,
                provenance=provenance,
                uncertainty_ns=self.uncertainty_ns,
            )
        )

    def consider(self, sentence: NmeaSentence) -> NmeaAnchorOutcome:
        """Adapt then delegate completely to ``MonotonicUtcClock.consider``."""

        candidate_outcome = self.candidate_from(sentence)
        candidate = candidate_outcome.candidate
        if candidate is None:
            return NmeaAnchorOutcome(self, candidate_outcome)

        clock_outcome = self.clock.consider(candidate, self.policy)
        return NmeaAnchorOutcome(
            tracker=replace(self, clock=clock_outcome.clock),
            candidate_outcome=candidate_outcome,
            clock_outcome=clock_outcome,
        )

    def gps_time_state(self, now_monotonic_ns: int) -> GpsTimeState:
        """Delegate GPS freshness to the retained clock policy."""

        return self.clock.gps_time_state(now_monotonic_ns, self.policy)


def _valid_uncertainty(uncertainty_ns: int, policy: AnchorPolicy) -> bool:
    maximum = policy.max_uncertainty_ns
    return (
        not isinstance(uncertainty_ns, bool)
        and isinstance(uncertainty_ns, int)
        and uncertainty_ns > 0
        and not isinstance(maximum, bool)
        and isinstance(maximum, int)
        and uncertainty_ns <= maximum
    )


def _valid_talker(talker: str) -> bool:
    return (
        len(talker) == 2
        and talker.isascii()
        and talker.isalnum()
        and talker.upper() == talker
    )
