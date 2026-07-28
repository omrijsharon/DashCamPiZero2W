"""Bounded, monotonic GPS telemetry history for clip and overlay consumers."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, replace
from datetime import time
from enum import StrEnum
from typing import Final

from dashcam.gps.nmea import NmeaSentence, SentenceType

MAX_TELEMETRY_SAMPLE_HZ: Final = 10
MAX_TELEMETRY_HISTORY_SAMPLES: Final = 3 * 60 * MAX_TELEMETRY_SAMPLE_HZ
MAX_CLIP_TELEMETRY_SAMPLES: Final = 60 * MAX_TELEMETRY_SAMPLE_HZ
_NANOSECONDS_PER_SECOND: Final = 1_000_000_000
_KNOTS_TO_METRES_PER_SECOND: Final = 1852.0 / 3600.0
_NANOSECONDS_PER_DAY: Final = 24 * 60 * 60 * _NANOSECONDS_PER_SECOND
_MAX_FORWARD_SOURCE_DELTA_NS: Final = 12 * 60 * 60 * _NANOSECONDS_PER_SECOND


class TelemetryWindowIssue(StrEnum):
    """Why a requested telemetry window cannot be claimed complete."""

    HISTORY_EVICTED = "HISTORY_EVICTED"
    SAMPLE_LIMIT = "SAMPLE_LIMIT"


@dataclass(frozen=True, slots=True)
class GpsTelemetrySample:
    """One coherent navigation snapshot at an actual receive timestamp."""

    monotonic_ns: int
    latitude_deg: float
    longitude_deg: float
    speed_mps: float | None = None
    course_deg: float | None = None
    altitude_m: float | None = None
    fix_quality: int | None = None
    satellites: int | None = None
    hdop: float | None = None

    def __post_init__(self) -> None:
        _integer(self.monotonic_ns, "monotonic_ns", minimum=0)
        _finite(self.latitude_deg, "latitude_deg", minimum=-90.0, maximum=90.0)
        _finite(self.longitude_deg, "longitude_deg", minimum=-180.0, maximum=180.0)
        if self.speed_mps is not None:
            _finite(self.speed_mps, "speed_mps", minimum=0.0, maximum=1_000.0)
        if self.course_deg is not None:
            _finite(
                self.course_deg,
                "course_deg",
                minimum=0.0,
                maximum=360.0,
                maximum_exclusive=True,
            )
        if self.altitude_m is not None:
            _finite(self.altitude_m, "altitude_m", minimum=-2_000.0, maximum=100_000.0)
        if self.fix_quality is not None:
            _integer(self.fix_quality, "fix_quality", minimum=0, maximum=8)
        if self.satellites is not None:
            _integer(self.satellites, "satellites", minimum=0, maximum=255)
        if self.hdop is not None:
            _finite(self.hdop, "hdop", minimum=0.0, maximum=1_000.0)


@dataclass(frozen=True, slots=True)
class GpsTelemetryCounters:
    """Cumulative O(1) telemetry accounting; samples themselves stay private."""

    sentences_considered: int = 0
    navigation_observations: int = 0
    invalid_navigation: int = 0
    ignored_sentences: int = 0
    samples_emitted: int = 0
    samples_coalesced: int = 0
    samples_rate_limited: int = 0
    samples_evicted: int = 0
    monotonic_regressions: int = 0
    source_time_regressions: int = 0
    omitted_out_of_range_fields: int = 0
    retained_samples: int = 0


@dataclass(frozen=True, slots=True)
class GpsTelemetryWindow:
    """A half-open ``[start, end)`` view with explicit completeness evidence."""

    start_monotonic_ns: int
    end_monotonic_ns: int
    samples: tuple[GpsTelemetrySample, ...]
    issues: tuple[TelemetryWindowIssue, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.issues


@dataclass(frozen=True, slots=True)
class _Motion:
    monotonic_ns: int
    speed_mps: float | None
    course_deg: float | None


@dataclass(frozen=True, slots=True)
class _FixDetails:
    monotonic_ns: int
    altitude_m: float | None
    fix_quality: int | None
    satellites: int | None
    hdop: float | None


class GpsTelemetryCollector:
    """Merge RMC/GGA fields into a bounded history at no more than 10 Hz.

    A sentence never receives an invented timestamp. RMC motion and GGA fix
    details may complement the other sentence only while their own observation
    remains inside ``stale_after_ns``. Invalid source sentences clear their
    corresponding complementary fields immediately.
    """

    def __init__(
        self,
        *,
        max_sample_hz: int,
        stale_after_ns: int,
        history_capacity: int = MAX_TELEMETRY_HISTORY_SAMPLES,
    ) -> None:
        _integer(
            max_sample_hz,
            "max_sample_hz",
            minimum=1,
            maximum=MAX_TELEMETRY_SAMPLE_HZ,
        )
        _integer(
            stale_after_ns,
            "stale_after_ns",
            minimum=1,
            maximum=3_600 * _NANOSECONDS_PER_SECOND,
        )
        _integer(
            history_capacity,
            "history_capacity",
            minimum=1,
            maximum=MAX_TELEMETRY_HISTORY_SAMPLES,
        )
        self._minimum_interval_ns = _NANOSECONDS_PER_SECOND // max_sample_hz
        self._stale_after_ns = stale_after_ns
        self._history_capacity = history_capacity
        self._samples: deque[GpsTelemetrySample] = deque()
        self._motion: _Motion | None = None
        self._fix: _FixDetails | None = None
        self._last_observation_ns: int | None = None
        self._last_emitted_source_epoch_ns: int | None = None
        self._evicted_through_ns: int | None = None
        self._counters = GpsTelemetryCounters()

    @property
    def counters(self) -> GpsTelemetryCounters:
        return self._counters

    def observe(self, sentence: NmeaSentence) -> None:
        """Consider one already checksum/parse-valid NMEA sentence."""

        if not isinstance(sentence, NmeaSentence):
            raise TypeError("sentence must be an NmeaSentence")
        self._increment(sentences_considered=1)
        if sentence.sentence_type not in {SentenceType.RMC, SentenceType.GGA}:
            self._increment(ignored_sentences=1)
            return
        received_ns = sentence.received_monotonic_ns
        if (
            received_ns is None
            or isinstance(received_ns, bool)
            or received_ns < 0
            or (
                self._last_observation_ns is not None
                and received_ns < self._last_observation_ns
            )
        ):
            self._increment(monotonic_regressions=1)
            return
        self._last_observation_ns = received_ns

        if sentence.sentence_type is SentenceType.RMC:
            self._observe_rmc(sentence, received_ns)
        else:
            self._observe_gga(sentence, received_ns)

        if (
            not sentence.navigation_valid
            or sentence.latitude_deg is None
            or sentence.longitude_deg is None
        ):
            self._increment(invalid_navigation=1)
            self._refresh_last_sample(sentence, received_ns)
            return

        self._increment(navigation_observations=1)
        source_epoch_ns = _source_epoch_ns(sentence.utc_time)
        sample = self._sample(
            received_ns,
            latitude_deg=sentence.latitude_deg,
            longitude_deg=sentence.longitude_deg,
        )
        previous = self._samples[-1] if self._samples else None
        same_source_epoch = (
            source_epoch_ns is not None
            and source_epoch_ns == self._last_emitted_source_epoch_ns
        )
        if previous is not None and (
            received_ns == previous.monotonic_ns or same_source_epoch
        ):
            self._samples[-1] = replace(sample, monotonic_ns=previous.monotonic_ns)
            self._increment(samples_coalesced=1)
            return

        source_elapsed_ns = self._source_elapsed_ns(source_epoch_ns)
        if source_elapsed_ns is not None:
            rate_limited = source_elapsed_ns < self._minimum_interval_ns
        else:
            rate_limited = (
                previous is not None
                and received_ns - previous.monotonic_ns < self._minimum_interval_ns
            )
        if rate_limited:
            self._increment(samples_rate_limited=1)
            return
        self._append(sample)
        self._last_emitted_source_epoch_ns = source_epoch_ns

    def window(
        self,
        start_monotonic_ns: int,
        end_monotonic_ns: int,
        *,
        max_samples: int = MAX_CLIP_TELEMETRY_SAMPLES,
    ) -> GpsTelemetryWindow:
        """Return an ordered half-open clip view without hiding lost history."""

        _integer(start_monotonic_ns, "start_monotonic_ns", minimum=0)
        _integer(end_monotonic_ns, "end_monotonic_ns", minimum=1)
        if end_monotonic_ns <= start_monotonic_ns:
            raise ValueError("end_monotonic_ns must be after start_monotonic_ns")
        _integer(
            max_samples,
            "max_samples",
            minimum=1,
            maximum=MAX_CLIP_TELEMETRY_SAMPLES,
        )

        issues: list[TelemetryWindowIssue] = []
        if (
            self._evicted_through_ns is not None
            and start_monotonic_ns <= self._evicted_through_ns
        ):
            issues.append(TelemetryWindowIssue.HISTORY_EVICTED)
        selected = tuple(
            sample
            for sample in self._samples
            if start_monotonic_ns <= sample.monotonic_ns < end_monotonic_ns
        )
        if len(selected) > max_samples:
            selected = selected[:max_samples]
            issues.append(TelemetryWindowIssue.SAMPLE_LIMIT)
        return GpsTelemetryWindow(
            start_monotonic_ns=start_monotonic_ns,
            end_monotonic_ns=end_monotonic_ns,
            samples=selected,
            issues=tuple(issues),
        )

    def _observe_rmc(self, sentence: NmeaSentence, received_ns: int) -> None:
        if not sentence.navigation_valid:
            self._motion = None
            return
        omitted = 0
        speed_mps = (
            None
            if sentence.speed_knots is None
            else sentence.speed_knots * _KNOTS_TO_METRES_PER_SECOND
        )
        if speed_mps is not None and speed_mps > 1_000.0:
            speed_mps = None
            omitted += 1
        course = sentence.course_deg
        if course == 360.0:
            course = 0.0
        self._motion = _Motion(received_ns, speed_mps, course)
        if omitted:
            self._increment(omitted_out_of_range_fields=omitted)

    def _observe_gga(self, sentence: NmeaSentence, received_ns: int) -> None:
        if not sentence.navigation_valid:
            self._fix = None
            return
        omitted = 0
        altitude = sentence.altitude_m
        if altitude is not None and not -2_000.0 <= altitude <= 100_000.0:
            altitude = None
            omitted += 1
        self._fix = _FixDetails(
            received_ns,
            altitude,
            sentence.fix_quality,
            sentence.satellites,
            sentence.hdop,
        )
        if omitted:
            self._increment(omitted_out_of_range_fields=omitted)

    def _sample(
        self,
        received_ns: int,
        *,
        latitude_deg: float,
        longitude_deg: float,
    ) -> GpsTelemetrySample:
        motion = self._motion if self._fresh(self._motion, received_ns) else None
        fix = self._fix if self._fresh(self._fix, received_ns) else None
        return GpsTelemetrySample(
            monotonic_ns=received_ns,
            latitude_deg=latitude_deg,
            longitude_deg=longitude_deg,
            speed_mps=None if motion is None else motion.speed_mps,
            course_deg=None if motion is None else motion.course_deg,
            altitude_m=None if fix is None else fix.altitude_m,
            fix_quality=None if fix is None else fix.fix_quality,
            satellites=None if fix is None else fix.satellites,
            hdop=None if fix is None else fix.hdop,
        )

    def _fresh(self, value: _Motion | _FixDetails | None, now_ns: int) -> bool:
        return (
            value is not None
            and value.monotonic_ns <= now_ns
            and now_ns - value.monotonic_ns <= self._stale_after_ns
        )

    def _refresh_last_sample(
        self,
        sentence: NmeaSentence,
        received_ns: int,
    ) -> None:
        if not self._samples:
            return
        previous = self._samples[-1]
        source_epoch_ns = _source_epoch_ns(sentence.utc_time)
        if (
            previous.monotonic_ns != received_ns
            and (
                source_epoch_ns is None
                or source_epoch_ns != self._last_emitted_source_epoch_ns
            )
        ):
            return
        self._samples[-1] = self._sample(
            previous.monotonic_ns,
            latitude_deg=previous.latitude_deg,
            longitude_deg=previous.longitude_deg,
        )
        self._increment(samples_coalesced=1)

    def _source_elapsed_ns(self, source_epoch_ns: int | None) -> int | None:
        previous = self._last_emitted_source_epoch_ns
        if source_epoch_ns is None or previous is None:
            return None
        elapsed_ns = (source_epoch_ns - previous) % _NANOSECONDS_PER_DAY
        if elapsed_ns <= _MAX_FORWARD_SOURCE_DELTA_NS:
            return elapsed_ns
        self._increment(source_time_regressions=1)
        return None

    def _append(self, sample: GpsTelemetrySample) -> None:
        if len(self._samples) == self._history_capacity:
            evicted = self._samples.popleft()
            self._evicted_through_ns = evicted.monotonic_ns
            self._increment(samples_evicted=1)
        self._samples.append(sample)
        self._increment(
            samples_emitted=1,
            retained_samples=len(self._samples) - self._counters.retained_samples,
        )

    def _increment(self, **increments: int) -> None:
        self._counters = replace(
            self._counters,
            **{
                name: getattr(self._counters, name) + amount
                for name, amount in increments.items()
            },
        )


def _integer(
    value: object,
    field: str,
    *,
    minimum: int,
    maximum: int = 9_223_372_036_854_775_807,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{field} is outside its allowed integer range")
    return value


def _finite(
    value: object,
    field: str,
    *,
    minimum: float,
    maximum: float,
    maximum_exclusive: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field} must be numeric")
    number = float(value)
    in_range = minimum <= number < maximum if maximum_exclusive else minimum <= number <= maximum
    if not math.isfinite(number) or not in_range:
        raise ValueError(f"{field} is outside its allowed range")
    return number


def _source_epoch_ns(value: time | None) -> int | None:
    if value is None:
        return None
    return (
        ((value.hour * 60 + value.minute) * 60 + value.second)
        * _NANOSECONDS_PER_SECOND
        + value.microsecond * 1_000
    )


__all__ = [
    "MAX_CLIP_TELEMETRY_SAMPLES",
    "MAX_TELEMETRY_HISTORY_SAMPLES",
    "MAX_TELEMETRY_SAMPLE_HZ",
    "GpsTelemetryCollector",
    "GpsTelemetryCounters",
    "GpsTelemetrySample",
    "GpsTelemetryWindow",
    "TelemetryWindowIssue",
]
