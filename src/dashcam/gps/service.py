"""Bounded, hardware-independent GPS transport supervision.

The service deliberately knows nothing about serial-device discovery or
``pyserial``.  A target adapter supplies a timeout-bounded transport and factory;
transport loss is reflected in a snapshot and retried without terminating the
recorder's supervision tree.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, Protocol

from dashcam.gps.anchors import NmeaAnchorTracker
from dashcam.gps.clock import AnchorStatus, UtcAnchor
from dashcam.gps.nmea import (
    MAX_NMEA_LINE_BYTES,
    NmeaError,
    NmeaSentence,
    SentenceType,
    parse_nmea_line,
)
from dashcam.gps.telemetry import (
    GpsTelemetryCollector,
    GpsTelemetryCounters,
    GpsTelemetryWindow,
)
from dashcam.state import GpsState, GpsTimeState

_MAX_ERROR_DETAIL_CHARS: Final = 160
_MAX_READ_BYTES: Final = 4096
_PARSE_RATE_WINDOW_NS: Final = 1_000_000_000


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
    read_coalesce_s: float = 0.100
    stale_after_s: float = 2.0
    reconnect_min_s: float = 0.25
    reconnect_max_s: float = 30.0
    close_timeout_s: float = 1.0
    max_line_bytes: int = MAX_NMEA_LINE_BYTES
    max_consecutive_parse_errors: int = 32
    max_parse_errors_per_second: int = 384

    def __post_init__(self) -> None:
        _bounded_int(self.read_size_bytes, "read_size_bytes", minimum=1, maximum=_MAX_READ_BYTES)
        _bounded_float(self.open_timeout_s, "open_timeout_s", minimum=0.001, maximum=30.0)
        _bounded_float(self.read_timeout_s, "read_timeout_s", minimum=0.001, maximum=30.0)
        _bounded_float(
            self.read_coalesce_s,
            "read_coalesce_s",
            minimum=0.0,
            maximum=0.100,
        )
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
        _bounded_int(
            self.max_consecutive_parse_errors,
            "max_consecutive_parse_errors",
            minimum=1,
            maximum=4096,
        )
        _bounded_int(
            self.max_parse_errors_per_second,
            "max_parse_errors_per_second",
            minimum=1,
            maximum=10_000,
        )

    @property
    def stale_after_ns(self) -> int:
        return int(self.stale_after_s * 1_000_000_000)

    @property
    def transport_read_deadline_s(self) -> float:
        """Outer protocol deadline with bounded scheduler margin.

        The transport receives ``read_timeout_s`` as its ordinary no-data
        deadline.  The supervisor retains a slightly later outer deadline so a
        correct transport returning at that boundary is not misclassified as a
        disconnect merely because the event loop resumed a few milliseconds
        later.
        """

        margin_s = min(max(self.read_timeout_s * 0.25, 0.01), 0.25)
        return self.read_timeout_s + margin_s


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
    anchor_attempts: int = 0
    anchor_acceptances: int = 0
    anchor_confirmations: int = 0
    anchor_reacquisitions: int = 0
    anchor_idempotent: int = 0
    anchor_rejections: int = 0


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
    gps_time_state: GpsTimeState = GpsTimeState.UNSYNCED
    time_anchor: UtcAnchor | None = None
    last_anchor_status: AnchorStatus | None = None
    last_anchor_error: str | None = None
    last_anchor_disagreement_ns: int | None = None
    telemetry_counters: GpsTelemetryCounters = field(
        default_factory=GpsTelemetryCounters
    )


@dataclass(slots=True)
class _MutableGpsCounters:
    """Private hot-path accounting, materialized immutably only for observers."""

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
    anchor_attempts: int = 0
    anchor_acceptances: int = 0
    anchor_confirmations: int = 0
    anchor_reacquisitions: int = 0
    anchor_idempotent: int = 0
    anchor_rejections: int = 0

    def snapshot(self) -> GpsCounters:
        return GpsCounters(
            connection_attempts=self.connection_attempts,
            connections=self.connections,
            reconnects=self.reconnects,
            disconnects=self.disconnects,
            read_timeouts=self.read_timeouts,
            bytes_received=self.bytes_received,
            lines_received=self.lines_received,
            valid_sentences=self.valid_sentences,
            parse_errors=self.parse_errors,
            checksum_failures=self.checksum_failures,
            unsupported_sentences=self.unsupported_sentences,
            oversized_lines=self.oversized_lines,
            transport_errors=self.transport_errors,
            valid_fixes=self.valid_fixes,
            stale_transitions=self.stale_transitions,
            anchor_attempts=self.anchor_attempts,
            anchor_acceptances=self.anchor_acceptances,
            anchor_confirmations=self.anchor_confirmations,
            anchor_reacquisitions=self.anchor_reacquisitions,
            anchor_idempotent=self.anchor_idempotent,
            anchor_rejections=self.anchor_rejections,
        )


@dataclass(slots=True)
class _MutableGpsState:
    """Private current state; public observations remain frozen value objects."""

    state: GpsState = GpsState.UART_UNAVAILABLE
    navigation: NmeaSentence | None = None
    latest_sentence: NmeaSentence | None = None
    connected: bool = False
    last_error: GpsServiceError | None = None
    last_error_detail: str | None = None
    last_parse_error: NmeaError | None = None
    gps_time_state: GpsTimeState = GpsTimeState.UNSYNCED
    time_anchor: UtcAnchor | None = None
    last_anchor_status: AnchorStatus | None = None
    last_anchor_error: str | None = None
    last_anchor_disagreement_ns: int | None = None


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
        read_coalescer: Callable[[float], Awaitable[None]] | None = None,
        anchor_tracker: NmeaAnchorTracker | None = None,
        telemetry_collector: GpsTelemetryCollector | None = None,
    ) -> None:
        self._factory = transport_factory
        self._limits = limits or GpsServiceLimits()
        self._monotonic_ns = monotonic_ns
        self._waiter = reconnect_waiter or _AsyncioReconnectWaiter()
        self._read_coalescer = read_coalescer or asyncio.sleep
        self._anchor_tracker = anchor_tracker
        self._telemetry_collector = telemetry_collector
        self._state = _MutableGpsState()
        self._counters = _MutableGpsCounters()
        self._line_buffer = bytearray()
        self._discarding_oversized_line = False
        self._last_sentence_ns: int | None = None
        self._last_navigation_ns: int | None = None
        self._consecutive_parse_errors = 0
        self._parse_error_window_started_ns: int | None = None
        self._parse_errors_in_window = 0
        self._had_connection = False
        self._started = False

    @property
    def snapshot(self) -> GpsSnapshot:
        """Return the latest immutable status object."""

        state = self._state
        collector = self._telemetry_collector
        return GpsSnapshot(
            state=state.state,
            navigation=state.navigation,
            latest_sentence=state.latest_sentence,
            counters=self._counters.snapshot(),
            connected=state.connected,
            buffered_bytes=len(self._line_buffer),
            discarding_oversized_line=self._discarding_oversized_line,
            last_error=state.last_error,
            last_error_detail=state.last_error_detail,
            last_parse_error=state.last_parse_error,
            gps_time_state=state.gps_time_state,
            time_anchor=state.time_anchor,
            last_anchor_status=state.last_anchor_status,
            last_anchor_error=state.last_anchor_error,
            last_anchor_disagreement_ns=state.last_anchor_disagreement_ns,
            telemetry_counters=(
                GpsTelemetryCounters() if collector is None else collector.counters
            ),
        )

    def telemetry_window(
        self,
        start_monotonic_ns: int,
        end_monotonic_ns: int,
        *,
        max_samples: int,
    ) -> GpsTelemetryWindow:
        """Return a bounded half-open history view when telemetry is configured."""

        collector = self._telemetry_collector
        if collector is None:
            raise RuntimeError("GPS telemetry collection is not configured")
        return collector.window(
            start_monotonic_ns,
            end_monotonic_ns,
            max_samples=max_samples,
        )

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
            self._counters.connection_attempts += 1
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
                timeout=self._limits.transport_read_deadline_s,
            )
            now_ns = self._now_ns()
            if not isinstance(chunk, bytes) or len(chunk) > self._limits.read_size_bytes:
                raise _TransportProtocolError("read returned an invalid or oversized byte chunk")
            if not chunk:
                self._counters.read_timeouts += 1
                self._refresh_freshness(now_ns)
                continue
            received_data = True
            self._counters.bytes_received += len(chunk)
            self._consume_chunk(chunk, now_ns)
            self._refresh_freshness(now_ns)
            if (
                len(chunk) < self._limits.read_size_bytes
                and not stop_requested.is_set()
                and self._limits.read_coalesce_s > 0.0
            ):
                await self._read_coalescer(self._limits.read_coalesce_s)
        return received_data

    def _consume_chunk(self, chunk: bytes, received_ns: int) -> None:
        offset = 0
        chunk_size = len(chunk)
        max_line_bytes = self._limits.max_line_bytes
        while offset < chunk_size:
            if self._discarding_oversized_line:
                newline = chunk.find(b"\n", offset)
                if newline < 0:
                    return
                self._discarding_oversized_line = False
                offset = newline + 1
                continue

            newline = chunk.find(b"\n", offset)
            end = chunk_size if newline < 0 else newline + 1
            segment_size = end - offset
            buffered_size = len(self._line_buffer)
            if buffered_size + segment_size > max_line_bytes:
                overflow_offset = offset + max_line_bytes - buffered_size
                self._line_buffer.clear()
                self._discarding_oversized_line = chunk[overflow_offset] != 0x0A
                self._counters.oversized_lines += 1
                self._counters.lines_received += 1
                self._record_parse_failure(received_ns)
                if newline < 0:
                    return
                self._discarding_oversized_line = False
                offset = end
                continue

            if newline < 0:
                self._line_buffer.extend(chunk[offset:end])
                return

            if buffered_size:
                self._line_buffer.extend(chunk[offset:end])
                raw_line = bytes(self._line_buffer)
                self._line_buffer.clear()
            else:
                raw_line = chunk[offset:end]
            self._handle_line(raw_line, received_ns)
            offset = end

    def _handle_line(self, raw_line: bytes, received_ns: int) -> None:
        self._counters.lines_received += 1
        outcome = parse_nmea_line(raw_line, received_monotonic_ns=received_ns)
        if not outcome.ok:
            assert outcome.error is not None
            self._counters.parse_errors += 1
            if outcome.error is NmeaError.CHECKSUM_MISMATCH:
                self._counters.checksum_failures += 1
            if outcome.error in {NmeaError.UNSUPPORTED_SENTENCE, NmeaError.UNSUPPORTED_TALKER}:
                self._counters.unsupported_sentences += 1
            state = (
                self._state.state
                if self._state.navigation is not None
                else GpsState.RECEIVING_INVALID
            )
            self._state.state = state
            self._state.last_parse_error = outcome.error
            self._record_parse_failure(received_ns)
            return

        sentence = outcome.sentence
        assert sentence is not None
        self._consecutive_parse_errors = 0
        self._consider_anchor(sentence)
        collector = self._telemetry_collector
        if collector is not None:
            collector.observe(sentence)
        self._last_sentence_ns = received_ns
        self._counters.valid_sentences += 1
        navigation = self._state.navigation
        if sentence.navigation_valid:
            navigation = sentence
            self._last_navigation_ns = received_ns
            self._counters.valid_fixes += 1
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
        self._state.state = state
        self._state.navigation = navigation
        self._state.latest_sentence = sentence
        self._state.last_error = None
        self._state.last_error_detail = None
        self._state.last_parse_error = None

    def _refresh_freshness(self, now_ns: int) -> None:
        navigation = self._state.navigation
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
        state = self._state.state
        if sentence_stale and state is not GpsState.STALE:
            state = GpsState.STALE
            self._counters.stale_transitions += 1
        elif navigation is None and self._state.navigation is not None:
            latest = self._state.latest_sentence
            state = (
                GpsState.TIME_VALID_POSITION_INVALID
                if latest is not None
                and latest.received_monotonic_ns == last_sentence
                and latest.time_anchor_candidate
                else GpsState.RECEIVING_INVALID
            )
        tracker = self._anchor_tracker
        gps_time_state = (
            GpsTimeState.UNSYNCED
            if tracker is None
            else tracker.gps_time_state(now_ns)
        )
        if state is not self._state.state:
            self._state.state = state
        if navigation is not self._state.navigation:
            self._state.navigation = navigation
        if gps_time_state is not self._state.gps_time_state:
            self._state.gps_time_state = gps_time_state

    def _consider_anchor(self, sentence: NmeaSentence) -> None:
        tracker = self._anchor_tracker
        if tracker is None or sentence.sentence_type not in {
            SentenceType.RMC,
            SentenceType.ZDA,
        }:
            return

        self._counters.anchor_attempts += 1
        outcome = tracker.consider(sentence)
        self._anchor_tracker = outcome.tracker
        clock_outcome = outcome.clock_outcome
        if clock_outcome is None:
            self._counters.anchor_rejections += 1
            self._state.gps_time_state = _tracker_time_state(
                outcome.tracker,
                sentence.received_monotonic_ns,
            )
            self._state.time_anchor = outcome.tracker.clock.anchor
            self._state.last_anchor_status = None
            self._state.last_anchor_error = (
                None if outcome.error is None else f"NMEA:{outcome.error.value}"
            )
            self._state.last_anchor_disagreement_ns = None
            return

        if clock_outcome.status is AnchorStatus.ACCEPTED:
            self._counters.anchor_acceptances += 1
        elif clock_outcome.status is AnchorStatus.CONFIRMED:
            self._counters.anchor_confirmations += 1
        elif clock_outcome.status is AnchorStatus.REACQUIRED:
            self._counters.anchor_reacquisitions += 1
        elif clock_outcome.status is AnchorStatus.IDEMPOTENT:
            self._counters.anchor_idempotent += 1
        else:
            self._counters.anchor_rejections += 1
        self._state.gps_time_state = _tracker_time_state(
            outcome.tracker,
            sentence.received_monotonic_ns,
        )
        self._state.time_anchor = outcome.tracker.clock.anchor
        self._state.last_anchor_status = clock_outcome.status
        self._state.last_anchor_error = (
            None
            if clock_outcome.error is None
            else f"CLOCK:{clock_outcome.error.value}"
        )
        self._state.last_anchor_disagreement_ns = clock_outcome.disagreement_ns

    def _on_connected(self) -> None:
        reconnect = self._had_connection
        self._had_connection = True
        self._line_buffer.clear()
        self._discarding_oversized_line = False
        self._consecutive_parse_errors = 0
        self._parse_error_window_started_ns = None
        self._parse_errors_in_window = 0
        self._counters.connections += 1
        if reconnect:
            self._counters.reconnects += 1
        state = self._state.state
        if state in {GpsState.UART_UNAVAILABLE, GpsState.FAULTED}:
            state = GpsState.RECEIVING_INVALID
        self._state.state = state
        self._state.connected = True
        self._state.last_error = None
        self._state.last_error_detail = None

    def _on_transport_error(
        self,
        error_code: GpsServiceError,
        error: BaseException,
        *,
        disconnected: bool,
        faulted: bool = False,
    ) -> None:
        self._counters.transport_errors += 1
        if disconnected:
            self._counters.disconnects += 1
        self._line_buffer.clear()
        self._discarding_oversized_line = False
        if disconnected and self._last_sentence_ns is not None:
            state = GpsState.STALE
            if self._state.state is not GpsState.STALE:
                self._counters.stale_transitions += 1
        else:
            state = GpsState.FAULTED if faulted else GpsState.UART_UNAVAILABLE
        self._state.state = state
        self._state.navigation = None
        self._state.connected = False
        self._state.last_error = error_code
        self._state.last_error_detail = _error_detail(error)
        self._state.gps_time_state = (
            GpsTimeState.GPS_TIME_STALE
            if self._state.time_anchor is not None
            else GpsTimeState.UNSYNCED
        )

    async def _close_transport(self, transport: GpsTransport) -> None:
        try:
            await asyncio.wait_for(transport.close(), timeout=self._limits.close_timeout_s)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._counters.transport_errors += 1
            self._state.connected = False
            self._state.last_error = GpsServiceError.CLOSE_FAILURE
            self._state.last_error_detail = _error_detail(error)
        else:
            self._state.connected = False

    def _record_parse_failure(self, received_ns: int) -> None:
        """Trip bounded reconnect/backoff before malformed input can starve media."""

        self._consecutive_parse_errors += 1
        window_started = self._parse_error_window_started_ns
        if (
            window_started is None
            or received_ns < window_started
            or received_ns - window_started >= _PARSE_RATE_WINDOW_NS
        ):
            self._parse_error_window_started_ns = received_ns
            self._parse_errors_in_window = 0
        self._parse_errors_in_window += 1
        if (
            self._consecutive_parse_errors
            >= self._limits.max_consecutive_parse_errors
        ):
            raise _TransportProtocolError(
                "consecutive GPS parse-error limit reached"
            )
        if (
            self._parse_errors_in_window
            > self._limits.max_parse_errors_per_second
        ):
            raise _TransportProtocolError(
                "GPS parse-error rate limit reached"
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


def _tracker_time_state(
    tracker: NmeaAnchorTracker,
    monotonic_ns: int | None,
) -> GpsTimeState:
    if (
        monotonic_ns is None
        or isinstance(monotonic_ns, bool)
        or monotonic_ns < 0
    ):
        return (
            GpsTimeState.GPS_TIME_STALE
            if tracker.clock.anchor is not None
            else GpsTimeState.UNSYNCED
        )
    return tracker.gps_time_state(monotonic_ns)


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
