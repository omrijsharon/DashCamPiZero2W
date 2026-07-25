from __future__ import annotations

import pytest

from dashcam.health.platform import (
    MAX_PLATFORM_FACT_BYTES,
    FactState,
    PlatformSource,
    collect_platform_facts,
    parse_cpu_temperature,
    parse_memory_info,
    parse_throttle_status,
)


class _Provider:
    def __init__(self, facts: dict[PlatformSource, str | bytes | None]) -> None:
        self._facts = facts
        self.requests: list[PlatformSource] = []

    def read(self, source: PlatformSource) -> str | bytes | None:
        self.requests.append(source)
        return self._facts.get(source)


def test_platform_parsers_accept_standard_bounded_target_text() -> None:
    temperature = parse_cpu_temperature("temp=42.8'C")
    throttled, undervoltage = parse_throttle_status("throttled=0x5")
    memory = parse_memory_info("MemAvailable:       1024 kB\nSwapTotal: 8 kB\nSwapFree: 3 kB\n")

    assert temperature.value == 42.8
    assert throttled.value is True
    assert undervoltage.value is True
    assert memory.available_bytes.value == 1_048_576
    assert memory.swap_used_bytes.value == 5_120


@pytest.mark.parametrize(
    ("raw", "state"),
    [
        (None, FactState.UNAVAILABLE),
        ("not a temperature", FactState.MALFORMED),
        (b"\xff", FactState.MALFORMED),
        ("x" * (MAX_PLATFORM_FACT_BYTES + 1), FactState.MALFORMED),
        ("temp=999'C", FactState.MALFORMED),
    ],
)
def test_temperature_has_explicit_unavailable_and_malformed_states(
    raw: str | bytes | None, state: FactState
) -> None:
    result = parse_cpu_temperature(raw)

    assert result.state is state
    assert result.value is None


def test_throttle_and_memory_reject_bad_or_inconsistent_input() -> None:
    throttled, undervoltage = parse_throttle_status("throttled=0x100000000")
    memory = parse_memory_info("MemAvailable: 1 kB\nSwapTotal: 2 kB\nSwapFree: 3 kB\n")

    assert throttled.state is FactState.MALFORMED
    assert undervoltage.state is FactState.MALFORMED
    assert memory.available_bytes.state is FactState.MALFORMED
    assert memory.swap_used_bytes.state is FactState.MALFORMED


def test_collection_converts_provider_failure_to_unavailable() -> None:
    class FailingProvider:
        def read(self, source: PlatformSource) -> str | bytes | None:
            raise OSError(source.value)

    snapshot = collect_platform_facts(FailingProvider())

    assert snapshot.cpu_temperature_c.state is FactState.UNAVAILABLE
    assert snapshot.throttled.state is FactState.UNAVAILABLE
    assert snapshot.memory_available_bytes.state is FactState.UNAVAILABLE


def test_collection_collects_all_closed_read_only_sources() -> None:
    provider = _Provider(
        {
            PlatformSource.CPU_TEMPERATURE: "42800",
            PlatformSource.THROTTLE_STATUS: "throttled=0x0",
            PlatformSource.MEMORY_INFO: "MemAvailable: 1 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n",
        }
    )

    snapshot = collect_platform_facts(provider)

    assert provider.requests == list(PlatformSource)
    assert snapshot.cpu_temperature_c.value == 42.8
    assert snapshot.throttled.value is False
    assert snapshot.undervoltage.value is False
    assert snapshot.memory_available_bytes.value == 1_024
