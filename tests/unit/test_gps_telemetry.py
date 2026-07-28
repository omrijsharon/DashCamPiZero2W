from __future__ import annotations

from datetime import UTC, time

import pytest

from dashcam.gps.nmea import NmeaSentence, SentenceType, TimeTrust
from dashcam.gps.telemetry import (
    MAX_CLIP_TELEMETRY_SAMPLES,
    GpsTelemetryCollector,
    GpsTelemetrySample,
    TelemetryWindowIssue,
)

_SECOND = 1_000_000_000


def _rmc(
    monotonic_ns: int,
    *,
    valid: bool = True,
    speed_knots: float | None = 10.0,
    course_deg: float | None = 90.0,
    source_epoch_ns: int | None = None,
) -> NmeaSentence:
    return NmeaSentence(
        sentence_type=SentenceType.RMC,
        talker="GN",
        received_monotonic_ns=monotonic_ns,
        utc_time=_utc_time(monotonic_ns if source_epoch_ns is None else source_epoch_ns),
        time_trust=(
            TimeTrust.RMC_STATUS_VALID if valid else TimeTrust.RMC_STATUS_INVALID
        ),
        latitude_deg=32.1 if valid else None,
        longitude_deg=34.8 if valid else None,
        speed_knots=speed_knots,
        course_deg=course_deg,
        navigation_valid=valid,
    )


def _gga(
    monotonic_ns: int,
    *,
    valid: bool = True,
    altitude_m: float | None = 25.0,
    source_epoch_ns: int | None = None,
) -> NmeaSentence:
    return NmeaSentence(
        sentence_type=SentenceType.GGA,
        talker="GN",
        received_monotonic_ns=monotonic_ns,
        utc_time=_utc_time(monotonic_ns if source_epoch_ns is None else source_epoch_ns),
        latitude_deg=32.1001 if valid else None,
        longitude_deg=34.8001 if valid else None,
        altitude_m=altitude_m,
        fix_quality=2 if valid else 0,
        satellites=9,
        hdop=0.8,
        navigation_valid=valid,
    )


def _utc_time(epoch_ns: int) -> time:
    seconds, nanoseconds = divmod(epoch_ns, _SECOND)
    seconds %= 24 * 60 * 60
    hour, seconds = divmod(seconds, 60 * 60)
    minute, second = divmod(seconds, 60)
    return time(
        int(hour),
        int(minute),
        int(second),
        int(nanoseconds // 1_000),
        tzinfo=UTC,
    )


def _collector(
    *,
    capacity: int = 20,
    stale_after_ns: int = 2 * _SECOND,
    max_sample_hz: int = 10,
) -> GpsTelemetryCollector:
    return GpsTelemetryCollector(
        max_sample_hz=max_sample_hz,
        stale_after_ns=stale_after_ns,
        history_capacity=capacity,
    )


def test_same_timestamp_rmc_and_gga_coalesce_into_one_coherent_sample() -> None:
    collector = _collector()

    collector.observe(_rmc(_SECOND, course_deg=360.0))
    collector.observe(_gga(_SECOND))

    window = collector.window(_SECOND, 2 * _SECOND)
    assert window.complete
    assert len(window.samples) == 1
    sample = window.samples[0]
    assert sample.monotonic_ns == _SECOND
    assert sample.latitude_deg == pytest.approx(32.1001)
    assert sample.longitude_deg == pytest.approx(34.8001)
    assert sample.speed_mps == pytest.approx(5.1444444444)
    assert sample.course_deg == 0.0
    assert sample.altitude_m == 25.0
    assert sample.fix_quality == 2
    assert sample.satellites == 9
    assert sample.hdop == 0.8
    assert collector.counters.samples_emitted == 1
    assert collector.counters.samples_coalesced == 1
    assert collector.counters.retained_samples == 1


def test_source_epoch_coalescing_retains_native_cadence_despite_host_jitter() -> None:
    collector = _collector(stale_after_ns=_SECOND)
    collector.observe(_rmc(0, source_epoch_ns=0))
    collector.observe(_gga(50_000_000, source_epoch_ns=0))
    collector.observe(_gga(99_000_000, source_epoch_ns=100_000_000))
    collector.observe(_gga(1_100_000_001, source_epoch_ns=1_100_000_000))

    samples = collector.window(0, 2 * _SECOND).samples
    assert [sample.monotonic_ns for sample in samples] == [0, 99_000_000, 1_100_000_001]
    assert samples[1].speed_mps == pytest.approx(5.1444444444)
    assert samples[2].speed_mps is None
    assert samples[2].course_deg is None
    assert collector.counters.samples_coalesced == 1
    assert collector.counters.samples_rate_limited == 0


def test_configured_source_epoch_rate_limit_is_explicit() -> None:
    collector = _collector(max_sample_hz=5)
    collector.observe(_rmc(0, source_epoch_ns=0))
    collector.observe(_rmc(100_000_000, source_epoch_ns=100_000_000))
    collector.observe(_rmc(200_000_000, source_epoch_ns=200_000_000))

    assert [sample.monotonic_ns for sample in collector.window(0, _SECOND).samples] == [
        0,
        200_000_000,
    ]
    assert collector.counters.samples_rate_limited == 1


def test_invalid_source_clears_complementary_fields_at_same_timestamp() -> None:
    collector = _collector()
    collector.observe(_rmc(_SECOND, source_epoch_ns=_SECOND))
    collector.observe(_gga(2 * _SECOND, source_epoch_ns=2 * _SECOND))
    assert collector.window(2 * _SECOND, 3 * _SECOND).samples[0].speed_mps is not None

    collector.observe(
        _rmc(
            2 * _SECOND + 50_000_000,
            valid=False,
            source_epoch_ns=2 * _SECOND,
        )
    )

    sample = collector.window(2 * _SECOND, 3 * _SECOND).samples[0]
    assert sample.speed_mps is None
    assert sample.course_deg is None
    assert sample.fix_quality == 2
    assert sample.monotonic_ns == 2 * _SECOND
    assert collector.counters.invalid_navigation == 1


def test_history_eviction_sample_limit_and_half_open_boundaries_are_explicit() -> None:
    collector = _collector(capacity=2)
    collector.observe(_rmc(0))
    collector.observe(_rmc(100_000_000))
    collector.observe(_rmc(200_000_000))

    evicted = collector.window(0, 300_000_000)
    assert evicted.issues == (TelemetryWindowIssue.HISTORY_EVICTED,)
    assert [sample.monotonic_ns for sample in evicted.samples] == [
        100_000_000,
        200_000_000,
    ]
    assert collector.counters.samples_evicted == 1
    assert collector.counters.retained_samples == 2

    half_open = collector.window(100_000_000, 200_000_000)
    assert [sample.monotonic_ns for sample in half_open.samples] == [100_000_000]

    limited = collector.window(100_000_000, 300_000_000, max_samples=1)
    assert limited.issues == (TelemetryWindowIssue.SAMPLE_LIMIT,)
    assert [sample.monotonic_ns for sample in limited.samples] == [100_000_000]


def test_monotonic_regression_cannot_corrupt_history_or_complementary_state() -> None:
    collector = _collector()
    collector.observe(_rmc(_SECOND))
    collector.observe(_gga(_SECOND - 1))
    collector.observe(_gga(2 * _SECOND))

    samples = collector.window(0, 3 * _SECOND).samples
    assert len(samples) == 2
    assert samples[-1].altitude_m == 25.0
    assert collector.counters.monotonic_regressions == 1


def test_out_of_sidecar_range_fields_are_omitted_and_counted_not_clamped() -> None:
    collector = _collector()
    collector.observe(_rmc(0, speed_knots=2_000.0))
    collector.observe(_gga(0, altitude_m=-3_000.0))

    sample = collector.window(0, _SECOND).samples[0]
    assert sample.speed_mps is None
    assert sample.altitude_m is None
    assert collector.counters.omitted_out_of_range_fields == 2


def test_constructor_samples_and_windows_enforce_product_bounds() -> None:
    with pytest.raises(ValueError, match="max_sample_hz"):
        GpsTelemetryCollector(max_sample_hz=11, stale_after_ns=_SECOND)
    with pytest.raises(ValueError, match="history_capacity"):
        GpsTelemetryCollector(max_sample_hz=10, stale_after_ns=_SECOND, history_capacity=0)
    with pytest.raises(ValueError, match="end_monotonic_ns"):
        _collector().window(10, 10)
    with pytest.raises(ValueError, match="max_samples"):
        _collector().window(0, 10, max_samples=MAX_CLIP_TELEMETRY_SAMPLES + 1)
    with pytest.raises(ValueError, match="course_deg"):
        GpsTelemetrySample(0, 0.0, 0.0, course_deg=360.0)
