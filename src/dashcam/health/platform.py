"""Pure parsers and injected collection for read-only platform health facts.

Nothing here opens a device, invokes a command, or reads the host filesystem.
The target integration supplies a small read-only provider after the Pi
capability gate has established which platform interfaces are safe to use.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Generic, Protocol, TypeVar

MAX_PLATFORM_FACT_BYTES = 4_096
MAX_PLATFORM_VALUE = 2**63 - 1
_TEMPERATURE_RE = re.compile(r"(?:temp=)?([+-]?(?:\d+(?:\.\d*)?|\.\d+))(?:'C)?")
_THROTTLE_RE = re.compile(r"throttled=0x([0-9a-fA-F]{1,8})")
_MEMINFO_RE = re.compile(r"(MemAvailable|SwapTotal|SwapFree):[ \t]+(\d+)[ \t]+kB")

T = TypeVar("T")


class FactState(StrEnum):
    """Whether a platform fact was supplied in a usable form."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    MALFORMED = "malformed"


class PlatformSource(StrEnum):
    """Closed read-only inputs requested from the target-owned adapter."""

    CPU_TEMPERATURE = "cpu_temperature"
    THROTTLE_STATUS = "throttle_status"
    MEMORY_INFO = "memory_info"


class ReadOnlyPlatformFactProvider(Protocol):
    """Return a bounded text payload, or ``None`` when the fact is unavailable."""

    def read(self, source: PlatformSource) -> str | bytes | None:
        """Read one fact without causing platform state changes."""


@dataclass(frozen=True, slots=True)
class PlatformFact(Generic[T]):
    """One parsed value with an explicit absent/malformed distinction."""

    state: FactState
    value: T | None = None

    def __post_init__(self) -> None:
        if self.state is FactState.AVAILABLE and self.value is None:
            raise ValueError("available platform facts require a value")
        if self.state is not FactState.AVAILABLE and self.value is not None:
            raise ValueError("unavailable or malformed platform facts cannot have a value")

    @classmethod
    def available(cls, value: T) -> PlatformFact[T]:
        return cls(FactState.AVAILABLE, value)

    @classmethod
    def unavailable(cls) -> PlatformFact[T]:
        return cls(FactState.UNAVAILABLE)

    @classmethod
    def malformed(cls) -> PlatformFact[T]:
        return cls(FactState.MALFORMED)


@dataclass(frozen=True, slots=True)
class PlatformSnapshot:
    """Current parse-only health inputs, with no retained raw device output."""

    cpu_temperature_c: PlatformFact[float]
    throttled: PlatformFact[bool]
    undervoltage: PlatformFact[bool]
    memory_available_bytes: PlatformFact[int]
    swap_total_bytes: PlatformFact[int]
    swap_free_bytes: PlatformFact[int]
    swap_used_bytes: PlatformFact[int]


def collect_platform_facts(provider: ReadOnlyPlatformFactProvider) -> PlatformSnapshot:
    """Read and parse platform facts through the supplied read-only adapter.

    Provider failures are represented as unavailable rather than escaping into
    the recorder loop.  Invalid or oversized provider output is malformed.
    """

    temperature = parse_cpu_temperature(_read(provider, PlatformSource.CPU_TEMPERATURE))
    throttled, undervoltage = parse_throttle_status(_read(provider, PlatformSource.THROTTLE_STATUS))
    memory = parse_memory_info(_read(provider, PlatformSource.MEMORY_INFO))
    return PlatformSnapshot(
        cpu_temperature_c=temperature,
        throttled=throttled,
        undervoltage=undervoltage,
        memory_available_bytes=memory.available_bytes,
        swap_total_bytes=memory.swap_total_bytes,
        swap_free_bytes=memory.swap_free_bytes,
        swap_used_bytes=memory.swap_used_bytes,
    )


def parse_cpu_temperature(raw: str | bytes | None) -> PlatformFact[float]:
    """Parse standard ``vcgencmd`` or sysfs CPU temperature output."""

    text, state = _normalise(raw)
    if state is not None:
        return PlatformFact(state)
    assert text is not None
    match = _TEMPERATURE_RE.fullmatch(text.strip())
    if match is None:
        return PlatformFact.malformed()
    try:
        value = float(match.group(1))
    except ValueError:
        return PlatformFact.malformed()
    if text.strip().lstrip("+-").isdigit():
        value /= 1_000.0
    if not -100.0 <= value <= 250.0:
        return PlatformFact.malformed()
    return PlatformFact.available(value)


def parse_throttle_status(raw: str | bytes | None) -> tuple[PlatformFact[bool], PlatformFact[bool]]:
    """Parse current Raspberry Pi throttling and undervoltage bits (2 and 0)."""

    text, state = _normalise(raw)
    if state is not None:
        return PlatformFact(state), PlatformFact(state)
    assert text is not None
    match = _THROTTLE_RE.fullmatch(text.strip())
    if match is None:
        return PlatformFact.malformed(), PlatformFact.malformed()
    bits = int(match.group(1), 16)
    return PlatformFact.available(bool(bits & 0x4)), PlatformFact.available(bool(bits & 0x1))


@dataclass(frozen=True, slots=True)
class MemoryFacts:
    """Available RAM and swap values parsed from bounded ``/proc/meminfo`` text."""

    available_bytes: PlatformFact[int]
    swap_total_bytes: PlatformFact[int]
    swap_free_bytes: PlatformFact[int]
    swap_used_bytes: PlatformFact[int]


def parse_memory_info(raw: str | bytes | None) -> MemoryFacts:
    """Parse ``MemAvailable``, ``SwapTotal``, and ``SwapFree`` exactly once."""

    text, state = _normalise(raw)
    if state is not None:
        fact = PlatformFact[int](state)
        return MemoryFacts(fact, fact, fact, fact)
    assert text is not None
    values: dict[str, int] = {}
    for line in text.splitlines():
        match = _MEMINFO_RE.fullmatch(line)
        if match is None:
            continue
        key, value_text = match.groups()
        if key in values:
            return _malformed_memory_facts()
        value = int(value_text)
        if value > MAX_PLATFORM_VALUE // 1_024:
            return _malformed_memory_facts()
        values[key] = value * 1_024
    if set(values) != {"MemAvailable", "SwapTotal", "SwapFree"}:
        return _malformed_memory_facts()
    if values["SwapFree"] > values["SwapTotal"]:
        return _malformed_memory_facts()
    return MemoryFacts(
        available_bytes=PlatformFact.available(values["MemAvailable"]),
        swap_total_bytes=PlatformFact.available(values["SwapTotal"]),
        swap_free_bytes=PlatformFact.available(values["SwapFree"]),
        swap_used_bytes=PlatformFact.available(values["SwapTotal"] - values["SwapFree"]),
    )


def _malformed_memory_facts() -> MemoryFacts:
    fact = PlatformFact[int].malformed()
    return MemoryFacts(fact, fact, fact, fact)


def _read(provider: ReadOnlyPlatformFactProvider, source: PlatformSource) -> str | bytes | None:
    try:
        return provider.read(source)
    except Exception:  # Provider failures must not harm recording supervision.
        return None


def _normalise(raw: str | bytes | None) -> tuple[str | None, FactState | None]:
    if raw is None:
        return None, FactState.UNAVAILABLE
    if isinstance(raw, bytes):
        if len(raw) > MAX_PLATFORM_FACT_BYTES:
            return None, FactState.MALFORMED
        try:
            raw = raw.decode("ascii")
        except UnicodeDecodeError:
            return None, FactState.MALFORMED
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_PLATFORM_FACT_BYTES:
        return None, FactState.MALFORMED
    return raw, None
