"""Bounded, hardware-independent GPS transport supervision.

The service deliberately knows nothing about serial-device discovery or
``pyserial``.  A target adapter supplies a timeout-bounded transport and factory;
transport loss is reflected in a snapshot and retried without terminating the
recorder's supervision tree.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Final, Protocol

from dashcam.gps.nmea import (
    MAX_NMEA_LINE_BYTES,
    NmeaError,
    NmeaSentence,
    SentenceType,
    parse_nmea_line,
)
from dashcam.state import GpsState

_MAX_ERROR_DETAIL_CHARS: Final = 160
_MAX_READ_BYTES: Final = 4096


class GpsTransport(Protocol):
    """One already-open byte transport.

    ``read`` must return within ``timeout_s`` and return at most ``max_bytes``.
    An empty result means that the read timed out without data.  Disconnects
    and read failures are reported by raising ``OSError``.
    """

    async def read(self, max_bytes: int, timeout_s: float) -> bytes:
        """Read a bounded byte chunk, returning ``b""`` on timeout."""

    async def close(self) -> None:
        """Release the transport idempotently."""


class GpsTransportFactory(Protocol):
    """Target-owned factory; implementations may open the configured UART."""

    async def open(self) -> GpsTransport:
        """Return one open transport or raise ``OSError`` when unavailable."""


class ReconnectWaiter(Protocol):
    """Injectable cancellation-aware wait used for deterministic supervision tests."""

    async def wait(self, stop_requested: asyncio.Event, delay_s: float) -> bool:
        """Return true when stop was requested, false when the delay elapsed."""


class GpsServiceError(StrEnum):
    """Stable adapter-level failures; NMEA errors remain in ``NmeaError``."""

    TRANSPORT_UNAVAILABLE = "TRANSPORT_UNAVAILABLE"
    TRANSPORT_FAILURE = "TRANSPORT_FAILURE"
    TRANSPORT_PROTOCOL = "TRANSPORT_PROTOCOL"
    CLOSE_FAILURE = "CLOSE_FAILURE"


@dataclass(frozen=True, slots=True)
class GpsServiceLimits:
    """All memory, wait, reconnect, and shutdown bounds used by the service."""

    read_size_bytes: int = 256
    open_timeout_s: float = 2.0
    read_timeout_s: float = 0.25
    stale_after_s: float = 2.0
    reconnect_min_s: float = 0.25
    reconnect_max_s: float = 30.0
    close_timeout_s: float = 1.0
    max_line_bytes: int = MAX_NMEA_LINE_BYTES

    def __post_init__(self) -> None:
        _bounded_int(self.read_size_bytes, "read_size_bytes", minimum=1, maximum=_MAX_READ_BYTES)
        _bounded_float(self.open_timeout_s, "open_timeout_s", minimum=0.001, maximum=30.0)
        _bounded_float(self.read_timeout_s, "read_timeout_s", minimum=0.001, maximum=30.0)
        _bounded_float(self.stale_after_s, "stale_after_s", minimum=0.001, maximum=3600.0)
        _bounded_float(
            self.reconnect_min_s,
            "reconnect_min_s",
            minimum=0.001,
            maximum=300.0,
        )
        _bounded_float(
            self.reconnect_max_s,
            "reconnect_max_s",
            minimum=0.001,
            maximum=300.0,
        )
        if self.reconnect_min_s > self.reconnect_max_s:
            raise ValueError("reconnect_min_s must not exceed reconnect_max_s")
        _bounded_float(self.close_timeout_s, "close_timeout_s", minimum=0.001, maximum=30.0)
        _bounded_int(
            self.max_line_bytes,
            "max_line_bytes",
            minimum=1,
            maximum=MAX_NMEA_LINE_BYTES,
        )

    @property
    def stale_after_ns(self) -> int:
        return int(self.stale_after_s * 1_000_000_000)


@dataclass(frozen=True, slots=True)
class GpsCounters:
    """Bounded-size cumulative metrics; no sentence or error history is retained."""

    connection_attempts: int = 0
    connections: int = 0
    reconnects: int = 0
    disconnects: int = 0
    read_timeouts: int = 0
    bytes_received: int = 0
    lines_received: int = 0
    valid_sentences: int = 0
    parse_errors: int = 0
    checksum_failures: int = 0
    unsupported_sentences: int = 0
    oversized_lines: int = 0
    transport_errors: int = 0
    valid_fixes: int = 0
    stale_transitions: int = 0


@dataclass(frozen=True, slots=True)
class GpsSnapshot:
    """Current GPS observation and supervision status."""

    state: GpsState = GpsState.UART_UNAVAILABLE
    navigation: NmeaSentence | None = None
    latest_sentence: NmeaSentence | None = None
    counters: GpsCounters = GpsCounters()
    connected: bool = False
    buffered_bytes: int = 0
    discarding_oversized_line: bool = False
    last_error: GpsServiceError | None = None
    last_error_detail: str | None = None
    last_parse_error: NmeaError | None = None


class _AsyncioReconnectWaiter:
    async def wait(self, stop_requested: asyncio.Event, delay_s: float) -> bool:
        try:
            await asyncio.wait_for(stop_requested.wait(), timeout=delay_s)
        except TimeoutError:
            return False
        return True


class GpsService:
    """Supervise one optional GPS source without propagating device failures."""

    def __init__(
        self,
        *,
        transport_factory: GpsTransportFactory,
        limits: GpsServiceLimits | None = None,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        reconnect_waiter: ReconnectWaiter | None = None,
    ) -> None:
        self._factory = transport_factory
        self._limits = limits or GpsServiceLimits()
        self._monotonic_ns = monotonic_ns
        self._waiter = reconnect_waiter or _AsyncioReconnectWaiter()
        self._snapshot = GpsSnapshot()
        self._line_buffer = bytearray()
        self._discarding_oversized_line = False
        self._last_sentence_ns: int | None = None
        self._last_navigation_ns: int | None = None
        self._had_connection = False
        self._started = False

    @property
    def snapshot(self) -> GpsSnapshot:
        """Return the latest immutable status object."""

        return self._snapshot

    async def run(self, stop_requested: asyncio.Event) -> None:
        """Run until cooperative stop or task cancellation.

        Expected open/read/close failures are contained, counted, and retried.
        ``asyncio.CancelledError`` is always re-raised after bounded cleanup so a
        parent supervisor retains normal task-cancellation semantics.
        """

        if self._started:
            raise RuntimeError("GpsService instances are single-use")
        self._started = True
        delay_s = self._limits.reconnect_min_s

        while not stop_requested.is_set():
            transport: GpsTransport | None = None
            received_data = False
            self._increment(connection_attempts=1)
            try:
                transport = await asyncio.wait_for(
                    self._factory.open(),
                    timeout=self._limits.open_timeout_s,
                )
                self._on_connected()
                received_data = await self._read_connected(transport, stop_requested)
            except asyncio.CancelledError:
                raise
            except _TransportProtocolError as error:
                self._on_transport_error(
                    GpsServiceError.TRANSPORT_PROTOCOL,
                    error,
                    disconnected=transport is not None,
                    faulted=True,
                )
            except OSError as error:
                self._on_transport_error(
                    GpsServiceError.TRANSPORT_UNAVAILABLE
                    if transport is None
                    else GpsServiceError.TRANSPORT_FAILURE,
                    error,
                    disconnected=transport is not None,
                )
            except Exception as error:
                self._on_transport_error(
                    GpsServiceError.TRANSPORT_FAILURE,
                    error,
                    disconnected=transport is not None,
                    faulted=True,
                )
            finally:
                if transport is not None:
                    await self._close_transport(transport)

            if stop_requested.is_set():
                break
            if received_data:
                delay_s = self._limits.reconnect_min_s
            stopped = await self._waiter.wait(stop_requested, delay_s)
            if stopped:
                break
            delay_s = min(delay_s * 2.0, self._limits.reconnect_max_s)

    async def _read_connected(
        self,
        transport: GpsTransport,
        stop_requested: asyncio.Event,
    ) -> bool:
        received_data = False
        while not stop_requested.is_set():
            chunk = await asyncio.wait_for(
                transport.read(
                    self._limits.read_size_bytes,
                    self._limits.read_timeout_s,
                ),
                timeout=self._limits.read_timeout_s,
            )
            now_ns = self._now_ns()
            if not isinstance(chunk, bytes) or len(chunk) > self._limits.read_size_bytes:
                raise _TransportProtocolError("read returned an invalid or oversized byte chunk")
            if not chunk:
                self._increment(read_timeouts=1)
                self._refresh_freshness(now_ns)
                continue
            received_data = True
            self._increment(bytes_received=len(chunk))
            self._consume_chunk(chunk, now_ns)
            self._refresh_freshness(now_ns)
        return received_data

    def _consume_chunk(self, chunk: bytes, received_ns: int) -> None:
        for value in chunk:
            if self._discarding_oversized_line:
                if value == 0x0A:
                    self._discarding_oversized_line = False
                    self._publish_buffer_status()
                continue

            self._line_buffer.append(value)
            if len(self._line_buffer) > self._limits.max_line_bytes:
                self._line_buffer.clear()
                self._discarding_oversized_line = value != 0x0A
                self._increment(oversized_lines=1, lines_received=1)
                self._publish_buffer_status()
                continue

            if value == 0x0A:
                raw_line = bytes(self._line_buffer)
                self._line_buffer.clear()
                self._publish_buffer_status()
                self._handle_line(raw_line, received_ns)
        self._publish_buffer_status()

    def _handle_line(self, raw_line: bytes, received_ns: int) -> None:
        self._increment(lines_received=1)
        outcome = parse_nmea_line(raw_line, received_monotonic_ns=received_ns)
        if not outcome.ok:
            assert outcome.error is not None
            increments: dict[str, int] = {"parse_errors": 1}
            if outcome.error is NmeaError.CHECKSUM_MISMATCH:
                increments["checksum_failures"] = 1
            if outcome.error in {NmeaError.UNSUPPORTED_SENTENCE, NmeaError.UNSUPPORTED_TALKER}:
                increments["unsupported_sentences"] = 1
            self._increment(**increments)
            state = (
                self._snapshot.state
                if self._snapshot.navigation is not None
                else GpsState.RECEIVING_INVALID
            )
            self._snapshot = replace(
                self._snapshot,
                state=state,
                last_parse_error=outcome.error,
            )
            return

        sentence = outcome.sentence
        assert sentence is not None
        self._last_sentence_ns = received_ns
        increments = {"valid_sentences": 1}
        navigation = self._snapshot.navigation
        if sentence.navigation_valid:
            navigation = sentence
            self._last_navigation_ns = received_ns
            increments["valid_fixes"] = 1
            state = GpsState.NAVIGATION_VALID
        elif sentence.sentence_type in {SentenceType.RMC, SentenceType.GGA}:
            navigation = None
            self._last_navigation_ns = None
            state = (
                GpsState.TIME_VALID_POSITION_INVALID
                if sentence.time_anchor_candidate
                else GpsState.RECEIVING_INVALID
            )
        elif navigation is not None:
            state = GpsState.NAVIGATION_VALID
        else:
            state = (
                GpsState.TIME_VALID_POSITION_INVALID
                if sentence.time_anchor_candidate
                else GpsState.RECEIVING_INVALID
            )
        self._increment(**increments)
        self._snapshot = replace(
            self._snapshot,
            state=state,
            navigation=navigation,
            latest_sentence=sentence,
            last_error=None,
            last_error_detail=None,
            last_parse_error=None,
        )

    def _refresh_freshness(self, now_ns: int) -> None:
        navigation = self._snapshot.navigation
        if navigation is not None and (
            self._last_navigation_ns is None
            or now_ns < self._last_navigation_ns
            or now_ns - self._last_navigation_ns > self._limits.stale_after_ns
        ):
            navigation = None

        last_sentence = self._last_sentence_ns
        sentence_stale = last_sentence is not None and (
            now_ns < last_sentence or now_ns - last_sentence > self._limits.stale_after_ns
        )
        state = self._snapshot.state
        if sentence_stale and state is not GpsState.STALE:
            state = GpsState.STALE
            self._increment(stale_transitions=1)
        elif navigation is None and self._snapshot.navigation is not None:
            latest = self._snapshot.latest_sentence
            state = (
                GpsState.TIME_VALID_POSITION_INVALID
                if latest is not None
                and latest.received_monotonic_ns == last_sentence
                and latest.time_anchor_candidate
                else GpsState.RECEIVING_INVALID
            )
        self._snapshot = replace(self._snapshot, state=state, navigation=navigation)

    def _on_connected(self) -> None:
        reconnect = self._had_connection
        self._had_connection = True
        self._line_buffer.clear()
        self._discarding_oversized_line = False
        increments = {"connections": 1}
        if reconnect:
            increments["reconnects"] = 1
        self._increment(**increments)
        state = self._snapshot.state
        if state in {GpsState.UART_UNAVAILABLE, GpsState.FAULTED}:
            state = GpsState.RECEIVING_INVALID
        self._snapshot = replace(
            self._snapshot,
            state=state,
            connected=True,
            buffered_bytes=0,
            discarding_oversized_line=False,
            last_error=None,
            last_error_detail=None,
        )

    def _on_transport_error(
        self,
        error_code: GpsServiceError,
        error: BaseException,
        *,
        disconnected: bool,
        faulted: bool = False,
    ) -> None:
        increments = {"transport_errors": 1}
        if disconnected:
            increments["disconnects"] = 1
        self._increment(**increments)
        self._line_buffer.clear()
        self._discarding_oversized_line = False
        if disconnected and self._last_sentence_ns is not None:
            state = GpsState.STALE
            if self._snapshot.state is not GpsState.STALE:
                self._increment(stale_transitions=1)
        else:
            state = GpsState.FAULTED if faulted else GpsState.UART_UNAVAILABLE
        self._snapshot = replace(
            self._snapshot,
            state=state,
            navigation=None,
            connected=False,
            buffered_bytes=0,
            discarding_oversized_line=False,
            last_error=error_code,
            last_error_detail=_error_detail(error),
        )

    async def _close_transport(self, transport: GpsTransport) -> None:
        try:
            await asyncio.wait_for(transport.close(), timeout=self._limits.close_timeout_s)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._increment(transport_errors=1)
            self._snapshot = replace(
                self._snapshot,
                connected=False,
                last_error=GpsServiceError.CLOSE_FAILURE,
                last_error_detail=_error_detail(error),
            )
        else:
            self._snapshot = replace(self._snapshot, connected=False)

    def _publish_buffer_status(self) -> None:
        self._snapshot = replace(
            self._snapshot,
            buffered_bytes=len(self._line_buffer),
            discarding_oversized_line=self._discarding_oversized_line,
        )

    def _increment(self, **increments: int) -> None:
        counters = self._snapshot.counters
        self._snapshot = replace(
            self._snapshot,
            counters=replace(
                counters,
                **{name: getattr(counters, name) + amount for name, amount in increments.items()},
            ),
        )

    def _now_ns(self) -> int:
        value = self._monotonic_ns()
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise _TransportProtocolError("monotonic clock returned an invalid value")
        return value


class _TransportProtocolError(RuntimeError):
    pass


def _error_detail(error: BaseException) -> str:
    raw = f"{type(error).__name__}: {error}".replace("\0", " ")
    detail = " ".join(raw.splitlines()).strip()
    printable = "".join(character if character.isprintable() else " " for character in detail)
    return (printable or type(error).__name__)[:_MAX_ERROR_DETAIL_CHARS]


def _bounded_int(value: object, name: str, *, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")


def _bounded_float(value: object, name: str, *, minimum: float, maximum: float) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
