"""Bounded, thread-safe recorder observability metrics.

The recorder updates these values from several supervision threads.  This
module intentionally retains only the current values: it does not retain
per-frame events, samples, or error history.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from types import MappingProxyType
from typing import Final

MAX_COUNTER_VALUE: Final = 2**63 - 1


class CounterName(StrEnum):
    """The complete set of cumulative counters required by the product contract."""

    CLIPS_FINALIZED = "clips_finalized_total"
    CLIPS_FAILED = "clips_failed_total"
    CLIPS_REPAIRED = "clips_repaired_total"
    CLIPS_QUARANTINED = "clips_quarantined_total"
    CLIPS_DELETED = "clips_deleted_total"
    VIDEO_FRAMES_CAPTURED = "video_frames_captured_total"
    VIDEO_FRAMES_ENCODED = "video_frames_encoded_total"
    VIDEO_FRAMES_WRITTEN = "video_frames_written_total"
    VIDEO_FRAMES_DROPPED = "video_frames_dropped_total"
    AUDIO_DISCONTINUITIES = "audio_discontinuities_total"
    GPS_SENTENCES_RECEIVED = "gps_sentences_received_total"
    GPS_CHECKSUM_FAILURES = "gps_checksum_failures_total"
    GPS_VALID_FIXES = "gps_valid_fixes_total"
    GPS_TIME_ANCHORS = "gps_time_anchors_total"
    GPS_RECONNECTS = "gps_reconnects_total"
    PREVIEW_FRAME_DROPS = "preview_frame_drops_total"
    PREVIEW_QUEUE_OVERRUNS = "preview_queue_overruns_total"
    FILESYSTEM_DELETION_CYCLES = "filesystem_deletion_cycles_total"
    CAMERA_ENCODER_PIPELINE_RESTARTS = "camera_encoder_pipeline_restarts_total"
    SERVICE_CRASHES = "service_crashes_total"
    WATCHDOG_RESETS = "watchdog_resets_total"


class GaugeName(StrEnum):
    """The complete set of instantaneous gauges required by the product contract."""

    RECORDING_UPTIME_NS = "recording_uptime_ns"
    ESTIMATED_AV_SKEW_MS = "estimated_av_skew_ms"
    PREVIEW_CLIENTS = "preview_clients"
    FILESYSTEM_FREE_BYTES = "filesystem_free_bytes"
    CPU_TEMPERATURE_C = "cpu_temperature_c"
    CPU_THROTTLED = "cpu_throttled"
    CPU_UNDERVOLTAGE = "cpu_undervoltage"


REQUIRED_COUNTERS: Final[tuple[CounterName, ...]] = tuple(CounterName)
REQUIRED_GAUGES: Final[tuple[GaugeName, ...]] = tuple(GaugeName)

_GAUGE_BOUNDS: Final[dict[GaugeName, tuple[float, float]]] = {
    GaugeName.RECORDING_UPTIME_NS: (0.0, float(MAX_COUNTER_VALUE)),
    GaugeName.ESTIMATED_AV_SKEW_MS: (-600_000.0, 600_000.0),
    GaugeName.PREVIEW_CLIENTS: (0.0, 10_000.0),
    GaugeName.FILESYSTEM_FREE_BYTES: (0.0, float(MAX_COUNTER_VALUE)),
    GaugeName.CPU_TEMPERATURE_C: (-100.0, 250.0),
    GaugeName.CPU_THROTTLED: (0.0, 1.0),
    GaugeName.CPU_UNDERVOLTAGE: (0.0, 1.0),
}


@dataclass(frozen=True, slots=True)
class MetricsSnapshot:
    """An immutable point-in-time view suitable for health/status serialization."""

    counters: Mapping[CounterName, int]
    gauges: Mapping[GaugeName, float]

    def to_dict(self) -> dict[str, dict[str, int | float]]:
        """Return a plain JSON-ready copy with the stable metric names as keys."""

        return {
            "counters": {name.value: value for name, value in self.counters.items()},
            "gauges": {name.value: value for name, value in self.gauges.items()},
        }


class Metrics:
    """Constant-memory metric registry with atomic updates and snapshots."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._counters = dict.fromkeys(REQUIRED_COUNTERS, 0)
        self._gauges = dict.fromkeys(REQUIRED_GAUGES, 0.0)

    def increment(self, name: CounterName, amount: int = 1) -> int:
        """Increase one closed-set counter, saturating at ``MAX_COUNTER_VALUE``."""

        _require_counter_name(name)
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise ValueError("counter increment must be a non-negative integer")
        with self._lock:
            value = min(MAX_COUNTER_VALUE, self._counters[name] + amount)
            self._counters[name] = value
            return value

    def set_gauge(self, name: GaugeName, value: float) -> float:
        """Set one finite, product-bounded gauge and return its stored value."""

        _require_gauge_name(name)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError("gauge value must be a finite number")
        numeric_value = float(value)
        minimum, maximum = _GAUGE_BOUNDS[name]
        if not math.isfinite(numeric_value) or not minimum <= numeric_value <= maximum:
            raise ValueError(f"gauge {name.value} must be finite and in {minimum}..{maximum}")
        with self._lock:
            self._gauges[name] = numeric_value
            return numeric_value

    def snapshot(self) -> MetricsSnapshot:
        """Copy the small registry while holding its lock; no history is retained."""

        with self._lock:
            counters = MappingProxyType(self._counters.copy())
            gauges = MappingProxyType(self._gauges.copy())
        return MetricsSnapshot(counters=counters, gauges=gauges)


def _require_counter_name(name: CounterName) -> None:
    if not isinstance(name, CounterName):
        raise TypeError("counter name must be a CounterName")


def _require_gauge_name(name: GaugeName) -> None:
    if not isinstance(name, GaugeName):
        raise TypeError("gauge name must be a GaugeName")
