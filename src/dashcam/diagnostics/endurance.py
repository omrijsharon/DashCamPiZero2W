"""Bounded endurance-sample collection and analysis."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Any, Protocol

MAX_SAMPLES = 43_200
MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_COUNTER_VALUE = 2**63 - 1
MAX_INTERVAL_SECONDS = 3_600.0


class EnduranceOutcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True)
class EnduranceSample:
    monotonic_ns: int
    rss_bytes: int | None
    memory_available_bytes: int | None
    swap_used_bytes: int | None
    cpu_percent: float | None
    temperature_c: float | None
    throttled: bool | None
    undervoltage: bool | None
    dropped_frames: int | None
    bitrate_bps: int | None
    restart_count: int | None

    def __post_init__(self) -> None:
        if (
            isinstance(self.monotonic_ns, bool)
            or not isinstance(self.monotonic_ns, int)
            or not 0 <= self.monotonic_ns <= MAX_COUNTER_VALUE
        ):
            raise ValueError("monotonic_ns must be a bounded non-negative integer")
        for name, integer_value in (
            ("rss_bytes", self.rss_bytes),
            ("memory_available_bytes", self.memory_available_bytes),
            ("swap_used_bytes", self.swap_used_bytes),
            ("dropped_frames", self.dropped_frames),
            ("bitrate_bps", self.bitrate_bps),
            ("restart_count", self.restart_count),
        ):
            if integer_value is not None and (
                isinstance(integer_value, bool)
                or not isinstance(integer_value, int)
                or not 0 <= integer_value <= MAX_COUNTER_VALUE
            ):
                raise ValueError(f"{name} must be a bounded non-negative integer")
        if self.cpu_percent is not None and (
            isinstance(self.cpu_percent, bool)
            or not isinstance(self.cpu_percent, int | float)
            or not math.isfinite(self.cpu_percent)
            or not 0 <= self.cpu_percent <= 1_000
        ):
            raise ValueError("cpu_percent must be finite and in 0..1000")
        if self.temperature_c is not None and (
            isinstance(self.temperature_c, bool)
            or not isinstance(self.temperature_c, int | float)
            or not math.isfinite(self.temperature_c)
            or not -100 <= self.temperature_c <= 250
        ):
            raise ValueError("temperature_c must be finite and plausible")
        for name, boolean_value in (
            ("throttled", self.throttled),
            ("undervoltage", self.undervoltage),
        ):
            if boolean_value is not None and not isinstance(boolean_value, bool):
                raise ValueError(f"{name} must be boolean or null")


class SampleSource(Protocol):
    def sample(self, monotonic_ns: int) -> EnduranceSample: ...


@dataclass(frozen=True)
class EnduranceThresholds:
    maximum_samples: int = MAX_SAMPLES
    maximum_temperature_c: float = 80.0
    maximum_cpu_percent: float = 100.0
    maximum_rss_growth_bytes: int = 32 * 1024 * 1024
    minimum_available_memory_bytes: int = 32 * 1024 * 1024
    maximum_swap_used_bytes: int = 0
    maximum_dropped_frame_increase: int = 0
    maximum_restart_increase: int = 0
    target_bitrate_bps: int = 8_000_000
    bitrate_tolerance_fraction: float = 0.25

    def __post_init__(self) -> None:
        integer_values = (
            self.maximum_samples,
            self.maximum_rss_growth_bytes,
            self.minimum_available_memory_bytes,
            self.maximum_swap_used_bytes,
            self.maximum_dropped_frame_increase,
            self.maximum_restart_increase,
            self.target_bitrate_bps,
        )
        numeric_values = (
            self.maximum_temperature_c,
            self.maximum_cpu_percent,
            self.bitrate_tolerance_fraction,
        )
        if (
            any(isinstance(value, bool) or not isinstance(value, int) for value in integer_values)
            or any(
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                for value in numeric_values
            )
            or not 1 <= self.maximum_samples <= MAX_SAMPLES
            or self.maximum_temperature_c <= 0
            or not 0 < self.maximum_cpu_percent <= 1000
            or self.maximum_rss_growth_bytes < 0
            or self.minimum_available_memory_bytes < 0
            or self.maximum_swap_used_bytes < 0
            or self.maximum_dropped_frame_increase < 0
            or self.maximum_restart_increase < 0
            or self.target_bitrate_bps <= 0
            or not 0 <= self.bitrate_tolerance_fraction < 1
        ):
            raise ValueError("invalid endurance thresholds")


DEFAULT_ENDURANCE_THRESHOLDS = EnduranceThresholds()


@dataclass(frozen=True)
class EnduranceCheck:
    code: str
    outcome: EnduranceOutcome
    observed: int | float | bool | None
    limit: int | float | bool | str
    summary: str


@dataclass(frozen=True)
class EnduranceReport:
    schema_version: int
    outcome: EnduranceOutcome
    sample_count: int
    started_monotonic_ns: int | None
    ended_monotonic_ns: int | None
    checks: tuple[EnduranceCheck, ...]
    samples: tuple[EnduranceSample, ...]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["outcome"] = self.outcome.value
        for check in value["checks"]:
            check["outcome"] = check["outcome"].value
        return value


def collect_samples(
    source: SampleSource,
    *,
    sample_count: int,
    interval_seconds: float,
    monotonic_ns: Callable[[], int],
    sleep: Callable[[float], None],
) -> tuple[EnduranceSample, ...]:
    """Collect a bounded number of samples using injectable time primitives."""

    if (
        isinstance(sample_count, bool)
        or not isinstance(sample_count, int)
        or not 1 <= sample_count <= MAX_SAMPLES
    ):
        raise ValueError(f"sample_count must be between 1 and {MAX_SAMPLES}")
    if (
        isinstance(interval_seconds, bool)
        or not isinstance(interval_seconds, int | float)
        or not math.isfinite(interval_seconds)
        or not 0 <= interval_seconds <= MAX_INTERVAL_SECONDS
    ):
        raise ValueError("interval_seconds must be finite and bounded")
    samples: list[EnduranceSample] = []
    for index in range(sample_count):
        sample = source.sample(monotonic_ns())
        if not isinstance(sample, EnduranceSample):
            raise TypeError("sample source returned an invalid value")
        samples.append(sample)
        if index + 1 < sample_count:
            sleep(interval_seconds)
    return tuple(samples)


def _values(samples: Sequence[EnduranceSample], name: str) -> list[int | float | bool]:
    return [value for sample in samples if (value := getattr(sample, name)) is not None]


def _range_delta(values: Sequence[int | float]) -> int | float | None:
    return values[-1] - values[0] if len(values) >= 2 else None


def analyze_samples(
    samples: Sequence[EnduranceSample],
    thresholds: EnduranceThresholds = DEFAULT_ENDURANCE_THRESHOLDS,
) -> EnduranceReport:
    """Analyze bounded samples without claiming absent metrics passed."""

    if len(samples) > thresholds.maximum_samples:
        raise ValueError("sample history exceeds configured maximum")
    if any(
        following.monotonic_ns <= previous.monotonic_ns for previous, following in pairwise(samples)
    ):
        raise ValueError("sample monotonic times must be strictly increasing")
    checks: list[EnduranceCheck] = []

    def missing(code: str, summary: str, limit: int | float | bool | str) -> None:
        checks.append(EnduranceCheck(code, EnduranceOutcome.INDETERMINATE, None, limit, summary))

    metric_names = (
        "rss_bytes",
        "memory_available_bytes",
        "swap_used_bytes",
        "cpu_percent",
        "temperature_c",
        "throttled",
        "undervoltage",
        "dropped_frames",
        "bitrate_bps",
        "restart_count",
    )
    missing_observations = sum(
        getattr(sample, name) is None for sample in samples for name in metric_names
    )
    checks.append(
        EnduranceCheck(
            "evidence_completeness",
            EnduranceOutcome.PASS
            if samples and missing_observations == 0
            else EnduranceOutcome.INDETERMINATE,
            missing_observations,
            0,
            "every required metric must be present in every sample",
        )
    )

    rss = [int(value) for value in _values(samples, "rss_bytes")]
    rss_growth = _range_delta(rss)
    if rss_growth is None:
        missing(
            "rss_growth",
            "at least two RSS observations are required",
            thresholds.maximum_rss_growth_bytes,
        )
    else:
        checks.append(
            EnduranceCheck(
                "rss_growth",
                EnduranceOutcome.PASS
                if rss_growth <= thresholds.maximum_rss_growth_bytes
                else EnduranceOutcome.FAIL,
                rss_growth,
                thresholds.maximum_rss_growth_bytes,
                "end-to-end RSS growth must remain bounded",
            )
        )

    available = [int(value) for value in _values(samples, "memory_available_bytes")]
    minimum_available = min(available) if available else None
    if minimum_available is None:
        missing(
            "available_memory",
            "available-memory evidence is absent",
            thresholds.minimum_available_memory_bytes,
        )
    else:
        checks.append(
            EnduranceCheck(
                "available_memory",
                EnduranceOutcome.PASS
                if minimum_available >= thresholds.minimum_available_memory_bytes
                else EnduranceOutcome.FAIL,
                minimum_available,
                thresholds.minimum_available_memory_bytes,
                "minimum available memory must remain above the reserve",
            )
        )

    swap = [int(value) for value in _values(samples, "swap_used_bytes")]
    maximum_swap = max(swap) if swap else None
    if maximum_swap is None:
        missing("swap_used", "swap-use evidence is absent", thresholds.maximum_swap_used_bytes)
    else:
        checks.append(
            EnduranceCheck(
                "swap_used",
                EnduranceOutcome.PASS
                if maximum_swap <= thresholds.maximum_swap_used_bytes
                else EnduranceOutcome.FAIL,
                maximum_swap,
                thresholds.maximum_swap_used_bytes,
                "swap use must remain within the configured ceiling",
            )
        )

    cpu = [float(value) for value in _values(samples, "cpu_percent")]
    maximum_cpu = max(cpu) if cpu else None
    if maximum_cpu is None:
        missing("cpu", "CPU evidence is absent", thresholds.maximum_cpu_percent)
    else:
        checks.append(
            EnduranceCheck(
                "cpu",
                EnduranceOutcome.PASS
                if maximum_cpu <= thresholds.maximum_cpu_percent
                else EnduranceOutcome.FAIL,
                maximum_cpu,
                thresholds.maximum_cpu_percent,
                "CPU utilization must remain within the configured ceiling",
            )
        )

    temperatures = [float(value) for value in _values(samples, "temperature_c")]
    maximum_temperature = max(temperatures) if temperatures else None
    if maximum_temperature is None:
        missing("temperature", "temperature evidence is absent", thresholds.maximum_temperature_c)
    else:
        checks.append(
            EnduranceCheck(
                "temperature",
                EnduranceOutcome.PASS
                if maximum_temperature <= thresholds.maximum_temperature_c
                else EnduranceOutcome.FAIL,
                maximum_temperature,
                thresholds.maximum_temperature_c,
                "temperature must remain within the configured ceiling",
            )
        )

    for field, code, summary in (
        ("throttled", "throttling", "no sample may report throttling"),
        ("undervoltage", "undervoltage", "no sample may report undervoltage"),
    ):
        boolean_values = [bool(value) for value in _values(samples, field)]
        if not boolean_values:
            missing(code, f"{code} evidence is absent", False)
        else:
            observed = any(boolean_values)
            checks.append(
                EnduranceCheck(
                    code,
                    EnduranceOutcome.FAIL if observed else EnduranceOutcome.PASS,
                    observed,
                    False,
                    summary,
                )
            )

    for field, code, maximum, summary in (
        (
            "dropped_frames",
            "dropped_frames",
            thresholds.maximum_dropped_frame_increase,
            "dropped-frame counter increase must remain within the limit",
        ),
        (
            "restart_count",
            "service_restarts",
            thresholds.maximum_restart_increase,
            "restart counter increase must remain within the limit",
        ),
    ):
        counter_values = [int(value) for value in _values(samples, field)]
        delta = _range_delta(counter_values)
        if delta is None:
            missing(code, f"at least two {code} observations are required", maximum)
        else:
            checks.append(
                EnduranceCheck(
                    code,
                    EnduranceOutcome.PASS if 0 <= delta <= maximum else EnduranceOutcome.FAIL,
                    delta,
                    maximum,
                    summary,
                )
            )

    bitrate = [int(value) for value in _values(samples, "bitrate_bps")]
    if not bitrate:
        missing("bitrate", "bitrate evidence is absent", thresholds.target_bitrate_bps)
    else:
        average = sum(bitrate) / len(bitrate)
        tolerance = thresholds.bitrate_tolerance_fraction
        minimum = thresholds.target_bitrate_bps * (1 - tolerance)
        bitrate_maximum = thresholds.target_bitrate_bps * (1 + tolerance)
        checks.append(
            EnduranceCheck(
                "bitrate",
                EnduranceOutcome.PASS
                if minimum <= average <= bitrate_maximum
                else EnduranceOutcome.FAIL,
                average,
                f"{minimum:.0f}..{bitrate_maximum:.0f}",
                "average measured bitrate must remain within the documented tolerance",
            )
        )

    if any(check.outcome is EnduranceOutcome.FAIL for check in checks):
        outcome = EnduranceOutcome.FAIL
    elif not checks or any(check.outcome is EnduranceOutcome.INDETERMINATE for check in checks):
        outcome = EnduranceOutcome.INDETERMINATE
    else:
        outcome = EnduranceOutcome.PASS
    return EnduranceReport(
        1,
        outcome,
        len(samples),
        samples[0].monotonic_ns if samples else None,
        samples[-1].monotonic_ns if samples else None,
        tuple(checks),
        tuple(samples),
    )


def _optional_int(item: Mapping[str, Any], index: int, name: str) -> int | None:
    value = item[name]
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"sample {index} field {name} must be an integer or null")
    return value


def _optional_float(item: Mapping[str, Any], index: int, name: str) -> float | None:
    value = item[name]
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"sample {index} field {name} must be numeric or null")
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError(f"sample {index} field {name} must be finite")
    return converted


def _optional_bool(item: Mapping[str, Any], index: int, name: str) -> bool | None:
    value = item[name]
    if value is not None and not isinstance(value, bool):
        raise ValueError(f"sample {index} field {name} must be boolean or null")
    return value


def samples_from_json(raw: bytes) -> tuple[EnduranceSample, ...]:
    """Load bounded fixture/service samples from a versioned JSON document."""

    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError(f"sample input exceeds {MAX_INPUT_BYTES} bytes")
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("sample input is not valid UTF-8 JSON") from error
    schema_version = document.get("schema_version") if isinstance(document, Mapping) else None
    if not isinstance(document, Mapping) or isinstance(schema_version, bool) or schema_version != 1:
        raise ValueError("sample input must be a schema_version 1 object")
    raw_samples = document.get("samples")
    if not isinstance(raw_samples, list) or len(raw_samples) > MAX_SAMPLES:
        raise ValueError(f"samples must be a list with at most {MAX_SAMPLES} items")
    allowed = {field.name for field in EnduranceSample.__dataclass_fields__.values()}
    results: list[EnduranceSample] = []
    for index, item in enumerate(raw_samples):
        if not isinstance(item, Mapping) or set(item) != allowed:
            raise ValueError(f"sample {index} has missing or unknown fields")

        try:
            monotonic_ns = _optional_int(item, index, "monotonic_ns")
            if monotonic_ns is None:
                raise ValueError("monotonic_ns must not be null")
            results.append(
                EnduranceSample(
                    monotonic_ns=monotonic_ns,
                    rss_bytes=_optional_int(item, index, "rss_bytes"),
                    memory_available_bytes=_optional_int(item, index, "memory_available_bytes"),
                    swap_used_bytes=_optional_int(item, index, "swap_used_bytes"),
                    cpu_percent=_optional_float(item, index, "cpu_percent"),
                    temperature_c=_optional_float(item, index, "temperature_c"),
                    throttled=_optional_bool(item, index, "throttled"),
                    undervoltage=_optional_bool(item, index, "undervoltage"),
                    dropped_frames=_optional_int(item, index, "dropped_frames"),
                    bitrate_bps=_optional_int(item, index, "bitrate_bps"),
                    restart_count=_optional_int(item, index, "restart_count"),
                )
            )
        except (TypeError, ValueError) as error:
            raise ValueError(f"sample {index} is invalid") from error
    return tuple(results)


def load_samples(path: Path) -> tuple[EnduranceSample, ...]:
    """Read one explicit, bounded regular JSON file."""

    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError("sample input must be a regular file")
    with resolved.open("rb") as handle:
        raw = handle.read(MAX_INPUT_BYTES + 1)
    return samples_from_json(raw)
