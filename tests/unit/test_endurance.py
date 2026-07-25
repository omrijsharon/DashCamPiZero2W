from __future__ import annotations

from pathlib import Path

import pytest

from dashcam.diagnostics.endurance import (
    MAX_INPUT_BYTES,
    MAX_INTERVAL_SECONDS,
    MAX_SAMPLES,
    EnduranceOutcome,
    EnduranceSample,
    EnduranceThresholds,
    analyze_samples,
    collect_samples,
    load_samples,
    samples_from_json,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "media"


def _checks(path: str) -> dict[str, EnduranceOutcome]:
    report = analyze_samples(load_samples(FIXTURES / path))
    return {check.code: check.outcome for check in report.checks}


def test_passing_fixture_has_all_required_metrics() -> None:
    report = analyze_samples(load_samples(FIXTURES / "endurance_pass.json"))

    assert report.outcome is EnduranceOutcome.PASS
    assert report.sample_count == 2
    assert set(_checks("endurance_pass.json").values()) == {EnduranceOutcome.PASS}
    assert report.to_dict()["outcome"] == "pass"


def test_threshold_failures_are_reported_independently() -> None:
    checks = _checks("endurance_fail.json")

    assert set(checks) == {
        "evidence_completeness",
        "rss_growth",
        "available_memory",
        "swap_used",
        "cpu",
        "temperature",
        "throttling",
        "undervoltage",
        "dropped_frames",
        "service_restarts",
        "bitrate",
    }
    assert checks["evidence_completeness"] is EnduranceOutcome.PASS
    assert set(checks.values()) == {EnduranceOutcome.PASS, EnduranceOutcome.FAIL}


def test_missing_metrics_are_indeterminate() -> None:
    sample = EnduranceSample(
        monotonic_ns=1,
        rss_bytes=None,
        memory_available_bytes=None,
        swap_used_bytes=None,
        cpu_percent=None,
        temperature_c=None,
        throttled=None,
        undervoltage=None,
        dropped_frames=None,
        bitrate_bps=None,
        restart_count=None,
    )

    report = analyze_samples((sample,))

    assert report.outcome is EnduranceOutcome.INDETERMINATE
    assert set(check.outcome for check in report.checks) == {EnduranceOutcome.INDETERMINATE}


def test_counter_regression_fails_instead_of_hiding_restart() -> None:
    first, second = load_samples(FIXTURES / "endurance_pass.json")
    elevated = EnduranceSample(**{**first.__dict__, "dropped_frames": 2})
    regressed = EnduranceSample(**{**second.__dict__, "dropped_frames": 1})

    report = analyze_samples((elevated, regressed))

    outcomes = {check.code: check.outcome for check in report.checks}
    assert outcomes["dropped_frames"] is EnduranceOutcome.FAIL


def test_non_monotonic_samples_are_rejected() -> None:
    first, second = load_samples(FIXTURES / "endurance_pass.json")
    duplicate_time = EnduranceSample(**{**second.__dict__, "monotonic_ns": first.monotonic_ns})

    with pytest.raises(ValueError, match="strictly increasing"):
        analyze_samples((first, duplicate_time))


def test_collector_uses_injected_source_clock_and_sleep() -> None:
    observed_times: list[int] = []
    sleeps: list[float] = []
    clock_values = iter((10, 20, 30))

    class Source:
        def sample(self, monotonic_ns: int) -> EnduranceSample:
            observed_times.append(monotonic_ns)
            return EnduranceSample(
                monotonic_ns,
                1,
                100_000_000,
                0,
                1.0,
                40.0,
                False,
                False,
                0,
                8_000_000,
                0,
            )

    samples = collect_samples(
        Source(),
        sample_count=3,
        interval_seconds=0.5,
        monotonic_ns=lambda: next(clock_values),
        sleep=sleeps.append,
    )

    assert len(samples) == 3
    assert observed_times == [10, 20, 30]
    assert sleeps == [0.5, 0.5]


@pytest.mark.parametrize("sample_count", [False, 0, MAX_SAMPLES + 1])
def test_collector_rejects_unbounded_sample_counts(sample_count: int) -> None:
    class Unused:
        def sample(self, monotonic_ns: int) -> EnduranceSample:
            raise AssertionError("must not sample")

    with pytest.raises(ValueError, match="sample_count"):
        collect_samples(
            Unused(),
            sample_count=sample_count,
            interval_seconds=1,
            monotonic_ns=lambda: 1,
            sleep=lambda _: None,
        )


def test_collector_rejects_unbounded_intervals() -> None:
    class Unused:
        def sample(self, monotonic_ns: int) -> EnduranceSample:
            raise AssertionError("must not sample")

    with pytest.raises(ValueError, match="finite and bounded"):
        collect_samples(
            Unused(),
            sample_count=1,
            interval_seconds=MAX_INTERVAL_SECONDS + 1,
            monotonic_ns=lambda: 1,
            sleep=lambda _: None,
        )


def test_runtime_models_reject_nonfinite_or_wrong_typed_metrics() -> None:
    with pytest.raises(ValueError, match="rss_bytes"):
        EnduranceSample(1, True, None, None, None, None, None, None, None, None, None)
    with pytest.raises(ValueError, match="cpu_percent"):
        EnduranceSample(1, None, None, None, float("nan"), None, None, None, None, None, None)
    with pytest.raises(ValueError, match="thresholds"):
        EnduranceThresholds(maximum_temperature_c=float("nan"))


@pytest.mark.parametrize(
    "raw",
    [
        b"{",
        b"[]",
        b'{"schema_version": true, "samples": []}',
        b'{"schema_version": 2, "samples": []}',
    ],
)
def test_malformed_sample_documents_are_rejected(raw: bytes) -> None:
    with pytest.raises(ValueError):
        samples_from_json(raw)


def test_oversized_sample_input_is_rejected() -> None:
    with pytest.raises(ValueError, match="exceeds"):
        samples_from_json(b" " * (MAX_INPUT_BYTES + 1))


def test_unknown_or_missing_sample_fields_are_rejected() -> None:
    with pytest.raises(ValueError, match="missing or unknown"):
        samples_from_json(b'{"schema_version":1,"samples":[{"monotonic_ns":1,"unknown":2}]}')


def test_wrong_sample_field_types_are_rejected() -> None:
    raw = (FIXTURES / "endurance_pass.json").read_text(encoding="utf-8")
    malformed = raw.replace('"rss_bytes": 50000000', '"rss_bytes": true', 1).encode()

    with pytest.raises(ValueError, match="sample 0 is invalid"):
        samples_from_json(malformed)
