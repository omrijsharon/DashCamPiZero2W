from __future__ import annotations

import threading

import pytest

from dashcam.health.metrics import (
    MAX_COUNTER_VALUE,
    REQUIRED_COUNTERS,
    REQUIRED_GAUGES,
    CounterName,
    GaugeName,
    Metrics,
)


def test_snapshot_includes_the_closed_required_metric_names() -> None:
    metrics = Metrics()

    snapshot = metrics.snapshot()

    assert tuple(snapshot.counters) == REQUIRED_COUNTERS
    assert tuple(snapshot.gauges) == REQUIRED_GAUGES
    assert snapshot.to_dict()["counters"] == {name.value: 0 for name in REQUIRED_COUNTERS}


def test_counters_saturate_and_reject_unknown_or_invalid_updates() -> None:
    metrics = Metrics()

    assert metrics.increment(CounterName.CLIPS_FINALIZED, MAX_COUNTER_VALUE) == MAX_COUNTER_VALUE
    assert metrics.increment(CounterName.CLIPS_FINALIZED, 1) == MAX_COUNTER_VALUE
    with pytest.raises(TypeError):
        metrics.increment("clips_finalized_total")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        metrics.increment(CounterName.CLIPS_FINALIZED, -1)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -101, 251, True])
def test_gauges_require_finite_values_within_their_declared_bounds(
    value: float | int | bool,
) -> None:
    metrics = Metrics()

    with pytest.raises(ValueError):
        metrics.set_gauge(GaugeName.CPU_TEMPERATURE_C, value)


def test_snapshot_is_immutable_and_does_not_change_after_later_updates() -> None:
    metrics = Metrics()
    metrics.increment(CounterName.GPS_VALID_FIXES, 2)
    first = metrics.snapshot()
    metrics.increment(CounterName.GPS_VALID_FIXES)

    assert first.counters[CounterName.GPS_VALID_FIXES] == 2
    with pytest.raises(TypeError):
        first.counters[CounterName.GPS_VALID_FIXES] = 7  # type: ignore[index]


def test_concurrent_updates_are_atomic_and_constant_registry_size() -> None:
    metrics = Metrics()
    workers = 12
    increments_per_worker = 2_000

    def increment_captured_frames() -> None:
        for _ in range(increments_per_worker):
            metrics.increment(CounterName.VIDEO_FRAMES_CAPTURED)

    threads = [threading.Thread(target=increment_captured_frames) for _ in range(workers)]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    snapshot = metrics.snapshot()
    assert snapshot.counters[CounterName.VIDEO_FRAMES_CAPTURED] == workers * increments_per_worker
    assert len(snapshot.counters) == len(REQUIRED_COUNTERS)
    assert len(snapshot.gauges) == len(REQUIRED_GAUGES)
