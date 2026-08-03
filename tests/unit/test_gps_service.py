from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypeAlias

import pytest

from dashcam.gps.anchors import NmeaAnchorTracker
from dashcam.gps.clock import AnchorPolicy, AnchorSource, AnchorStatus
from dashcam.gps.nmea import MAX_NMEA_LINE_BYTES
from dashcam.gps.service import (
    GpsService,
    GpsServiceError,
    GpsServiceLimits,
    GpsSnapshot,
    GpsTransport,
)
from dashcam.gps.telemetry import GpsTelemetryCollector
from dashcam.state import GpsState, GpsTimeState

_SECOND = 1_000_000_000


def _sentence(body: str) -> bytes:
    checksum = 0
    for character in body:
        checksum ^= ord(character)
    return f"${body}*{checksum:02X}\r\n".encode("ascii")


_VALID_RMC = _sentence("GPRMC,123519.25,A,4807.038,N,01131.000,E,22.4,84.4,230726,,,A")
_LATER_RMC = _sentence("GNRMC,123520.25,A,4807.050,N,01131.020,E,10.0,90.0,230726,,,A")
_INVALID_FIX = _sentence("GPRMC,123519.25,V,,,,,0.0,0.0,230726,,,N")
_UNSUPPORTED = _sentence("GPTXT,01,01,02,receiver-message")
_BAD_CHECKSUM = _VALID_RMC[:-4] + b"00\r\n"


@dataclass
class FakeClock:
    now_ns: int = 0

    def __call__(self) -> int:
        return self.now_ns

    def advance(self, amount_ns: int) -> None:
        self.now_ns += amount_ns


@dataclass(frozen=True)
class TimedRead:
    advance_ns: int
    result: bytes | BaseException


ReadAction: TypeAlias = bytes | BaseException | TimedRead


class ScriptedTransport:
    def __init__(
        self,
        actions: list[ReadAction],
        *,
        clock: FakeClock,
        stop: asyncio.Event,
        stop_on_exhaustion: bool = False,
    ) -> None:
        self.actions = deque(actions)
        self.clock = clock
        self.stop = stop
        self.stop_on_exhaustion = stop_on_exhaustion
        self.read_limits: list[tuple[int, float]] = []
        self.close_calls = 0

    async def read(self, max_bytes: int, timeout_s: float) -> bytes:
        self.read_limits.append((max_bytes, timeout_s))
        if not self.actions:
            raise AssertionError("scripted transport was read past exhaustion")
        action = self.actions.popleft()
        if isinstance(action, TimedRead):
            self.clock.advance(action.advance_ns)
            result = action.result
        else:
            result = action
        if self.stop_on_exhaustion and not self.actions:
            self.stop.set()
        if isinstance(result, BaseException):
            raise result
        return result

    async def close(self) -> None:
        self.close_calls += 1


FactoryAction: TypeAlias = GpsTransport | BaseException


class ScriptedFactory:
    def __init__(self, actions: list[FactoryAction]) -> None:
        self.actions = deque(actions)
        self.open_calls = 0

    async def open(self) -> GpsTransport:
        self.open_calls += 1
        if not self.actions:
            raise AssertionError("factory was opened past exhaustion")
        action = self.actions.popleft()
        if isinstance(action, BaseException):
            raise action
        return action


class ScriptedWaiter:
    def __init__(self, *, stop_after: int | None = None) -> None:
        self.delays: list[float] = []
        self.stop_after = stop_after

    async def wait(self, stop_requested: asyncio.Event, delay_s: float) -> bool:
        self.delays.append(delay_s)
        if self.stop_after is not None and len(self.delays) >= self.stop_after:
            stop_requested.set()
            return True
        return stop_requested.is_set()


class RecordingCoalescer:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay_s: float) -> None:
        self.delays.append(delay_s)


class ReferenceBytewiseGpsService(GpsService):
    """Literal pre-optimization consumer used only for differential tests."""

    def _consume_chunk(self, chunk: bytes, received_ns: int) -> None:
        for value in chunk:
            if self._discarding_oversized_line:
                if value == 0x0A:
                    self._discarding_oversized_line = False
                continue

            self._line_buffer.append(value)
            if len(self._line_buffer) > self._limits.max_line_bytes:
                self._line_buffer.clear()
                self._discarding_oversized_line = value != 0x0A
                self._increment(oversized_lines=1, lines_received=1)
                self._record_parse_failure(received_ns)
                continue

            if value == 0x0A:
                raw_line = bytes(self._line_buffer)
                self._line_buffer.clear()
                self._handle_line(raw_line, received_ns)


def _limits(**changes: float | int) -> GpsServiceLimits:
    values: dict[str, float | int] = {
        "read_size_bytes": 128,
        "open_timeout_s": 0.1,
        "read_timeout_s": 0.1,
        "read_coalesce_s": 0.0,
        "stale_after_s": 2.0,
        "reconnect_min_s": 0.1,
        "reconnect_max_s": 0.4,
        "close_timeout_s": 0.1,
        "max_line_bytes": MAX_NMEA_LINE_BYTES,
    }
    values.update(changes)
    return GpsServiceLimits(**values)  # type: ignore[arg-type]


def _anchor_tracker(*, stale_after_ns: int = 2 * _SECOND) -> NmeaAnchorTracker:
    return NmeaAnchorTracker(
        policy=AnchorPolicy(
            earliest_utc=datetime(2020, 1, 1, tzinfo=UTC),
            latest_utc=datetime(2030, 1, 1, tzinfo=UTC),
            gps_stale_after_ns=stale_after_ns,
        ),
        uncertainty_ns=250_000_000,
    )


def test_valid_rmc_anchor_is_accepted_then_confirmed_with_stable_provenance() -> None:
    async def scenario() -> None:
        stop = asyncio.Event()
        clock = FakeClock()
        transport = ScriptedTransport(
            [_VALID_RMC, TimedRead(_SECOND, _LATER_RMC)],
            clock=clock,
            stop=stop,
            stop_on_exhaustion=True,
        )
        service = GpsService(
            transport_factory=ScriptedFactory([transport]),
            limits=_limits(stale_after_s=2.0),
            monotonic_ns=clock,
            anchor_tracker=_anchor_tracker(),
        )

        await service.run(stop)

        snapshot = service.snapshot
        assert snapshot.gps_time_state is GpsTimeState.GPS_TIME_VALID
        assert snapshot.time_anchor is not None
        assert snapshot.time_anchor.source is AnchorSource.GPS_RMC_VALID
        assert snapshot.time_anchor.utc == datetime(
            2026, 7, 23, 12, 35, 19, 250_000, tzinfo=UTC
        )
        assert snapshot.time_anchor.uncertainty_ns == 250_000_000
        assert snapshot.last_anchor_status is AnchorStatus.CONFIRMED
        assert snapshot.last_anchor_error is None
        assert snapshot.last_anchor_disagreement_ns == 0
        assert snapshot.counters.anchor_attempts == 2
        assert snapshot.counters.anchor_acceptances == 1
        assert snapshot.counters.anchor_confirmations == 1
        assert snapshot.counters.anchor_rejections == 0

    asyncio.run(scenario())


def test_invalid_rmc_is_counted_as_anchor_rejection_but_bad_checksum_is_not_attempted() -> None:
    async def scenario() -> None:
        stop = asyncio.Event()
        clock = FakeClock()
        transport = ScriptedTransport(
            [_BAD_CHECKSUM, _INVALID_FIX],
            clock=clock,
            stop=stop,
            stop_on_exhaustion=True,
        )
        service = GpsService(
            transport_factory=ScriptedFactory([transport]),
            limits=_limits(),
            monotonic_ns=clock,
            anchor_tracker=_anchor_tracker(),
        )

        await service.run(stop)

        snapshot = service.snapshot
        assert snapshot.gps_time_state is GpsTimeState.UNSYNCED
        assert snapshot.time_anchor is None
        assert snapshot.last_anchor_status is None
        assert snapshot.last_anchor_error == "NMEA:RMC_NOT_ACTIVE_VALID"
        assert snapshot.counters.checksum_failures == 1
        assert snapshot.counters.anchor_attempts == 1
        assert snapshot.counters.anchor_rejections == 1

    asyncio.run(scenario())


def test_transport_loss_preserves_anchor_but_marks_gps_time_stale() -> None:
    async def scenario() -> None:
        stop = asyncio.Event()
        clock = FakeClock()
        transport = ScriptedTransport(
            [_VALID_RMC, OSError("UART lost")],
            clock=clock,
            stop=stop,
        )
        service = GpsService(
            transport_factory=ScriptedFactory([transport]),
            limits=_limits(),
            monotonic_ns=clock,
            reconnect_waiter=ScriptedWaiter(stop_after=1),
            anchor_tracker=_anchor_tracker(),
        )

        await service.run(stop)

        snapshot = service.snapshot
        assert snapshot.gps_time_state is GpsTimeState.GPS_TIME_STALE
        assert snapshot.time_anchor is not None
        assert snapshot.navigation is None
        assert snapshot.counters.anchor_acceptances == 1
        assert snapshot.counters.disconnects == 1

    asyncio.run(scenario())


def test_no_gps_boot_retries_with_bounded_exponential_backoff() -> None:
    async def scenario() -> None:
        stop = asyncio.Event()
        waiter = ScriptedWaiter(stop_after=4)
        factory = ScriptedFactory(
            [
                OSError("UART absent"),
                OSError("UART absent"),
                OSError("UART absent"),
                OSError("UART absent"),
            ]
        )
        service = GpsService(
            transport_factory=factory,
            limits=_limits(),
            reconnect_waiter=waiter,
        )

        await service.run(stop)

        snapshot = service.snapshot
        assert snapshot.state is GpsState.UART_UNAVAILABLE
        assert snapshot.navigation is None
        assert snapshot.counters.connection_attempts == 4
        assert snapshot.counters.connections == 0
        assert snapshot.counters.transport_errors == 4
        assert waiter.delays == [0.1, 0.2, 0.4, 0.4]
        assert snapshot.last_error is GpsServiceError.TRANSPORT_UNAVAILABLE
        assert len(snapshot.last_error_detail or "") <= 160

    asyncio.run(scenario())


def test_transport_outer_deadline_has_small_bounded_scheduler_margin() -> None:
    assert _limits(read_timeout_s=0.01).transport_read_deadline_s == pytest.approx(0.02)
    assert _limits(read_timeout_s=0.25).transport_read_deadline_s == pytest.approx(0.3125)
    assert _limits(read_timeout_s=30.0).transport_read_deadline_s == pytest.approx(30.25)


def test_short_nonfinal_read_coalesces_once_with_the_configured_delay() -> None:
    async def scenario() -> None:
        stop = asyncio.Event()
        clock = FakeClock()
        coalescer = RecordingCoalescer()
        transport = ScriptedTransport(
            [_VALID_RMC[:7], _VALID_RMC[7:]],
            clock=clock,
            stop=stop,
            stop_on_exhaustion=True,
        )
        service = GpsService(
            transport_factory=ScriptedFactory([transport]),
            limits=_limits(read_coalesce_s=0.02),
            monotonic_ns=clock,
            read_coalescer=coalescer,
        )

        await service.run(stop)

        assert coalescer.delays == [0.02]
        assert service.snapshot.counters.valid_sentences == 1

    asyncio.run(scenario())


def test_full_read_skips_coalescing_so_backlog_drains_immediately() -> None:
    async def scenario() -> None:
        stop = asyncio.Event()
        clock = FakeClock()
        coalescer = RecordingCoalescer()
        transport = ScriptedTransport(
            [b"x" * 128, b"\n"],
            clock=clock,
            stop=stop,
            stop_on_exhaustion=True,
        )
        service = GpsService(
            transport_factory=ScriptedFactory([transport]),
            limits=_limits(read_coalesce_s=0.02),
            monotonic_ns=clock,
            read_coalescer=coalescer,
        )

        await service.run(stop)

        assert coalescer.delays == []
        assert service.snapshot.counters.bytes_received == 129

    asyncio.run(scenario())


def test_stop_after_short_read_prevents_an_extra_coalescing_delay() -> None:
    async def scenario() -> None:
        stop = asyncio.Event()
        clock = FakeClock()
        coalescer = RecordingCoalescer()
        transport = ScriptedTransport(
            [_VALID_RMC],
            clock=clock,
            stop=stop,
            stop_on_exhaustion=True,
        )
        service = GpsService(
            transport_factory=ScriptedFactory([transport]),
            limits=_limits(read_coalesce_s=0.1),
            monotonic_ns=clock,
            read_coalescer=coalescer,
        )

        await service.run(stop)

        assert coalescer.delays == []
        assert service.snapshot.counters.valid_sentences == 1

    asyncio.run(scenario())


def test_transport_read_deadline_is_enforced_even_if_adapter_hangs() -> None:
    class HangingTransport:
        def __init__(self) -> None:
            self.closed = False

        async def read(self, max_bytes: int, timeout_s: float) -> bytes:
            await asyncio.Event().wait()
            return b""

        async def close(self) -> None:
            self.closed = True

    async def scenario() -> None:
        stop = asyncio.Event()
        transport = HangingTransport()
        waiter = ScriptedWaiter(stop_after=1)
        service = GpsService(
            transport_factory=ScriptedFactory([transport]),
            limits=_limits(read_timeout_s=0.01),
            reconnect_waiter=waiter,
        )

        await asyncio.wait_for(service.run(stop), timeout=0.2)

        assert transport.closed
        assert service.snapshot.state is GpsState.UART_UNAVAILABLE
        assert service.snapshot.last_error is GpsServiceError.TRANSPORT_FAILURE

    asyncio.run(scenario())


def test_partial_malformed_unsupported_and_oversized_lines_are_bounded() -> None:
    async def scenario() -> None:
        stop = asyncio.Event()
        clock = FakeClock()
        oversized = b"$" + b"A" * MAX_NMEA_LINE_BYTES + b"\n"
        split_at = len(_VALID_RMC) // 2
        transport = ScriptedTransport(
            [
                _BAD_CHECKSUM,
                _UNSUPPORTED,
                oversized + _VALID_RMC[:split_at],
                _VALID_RMC[split_at:],
            ],
            clock=clock,
            stop=stop,
            stop_on_exhaustion=True,
        )
        service = GpsService(
            transport_factory=ScriptedFactory([transport]),
            limits=_limits(),
            monotonic_ns=clock,
        )

        await service.run(stop)

        snapshot = service.snapshot
        assert snapshot.state is GpsState.NAVIGATION_VALID
        assert snapshot.navigation is not None
        assert snapshot.counters.lines_received == 4
        assert snapshot.counters.valid_sentences == 1
        assert snapshot.counters.parse_errors == 2
        assert snapshot.counters.checksum_failures == 1
        assert snapshot.counters.unsupported_sentences == 1
        assert snapshot.counters.oversized_lines == 1
        assert snapshot.buffered_bytes == 0
        assert not snapshot.discarding_oversized_line
        assert all(max_bytes == 128 for max_bytes, _ in transport.read_limits)

    asyncio.run(scenario())


def test_consecutive_parse_error_limit_forces_bounded_reconnect() -> None:
    async def scenario() -> None:
        stop = asyncio.Event()
        clock = FakeClock()
        malformed = ScriptedTransport(
            [_BAD_CHECKSUM * 8],
            clock=clock,
            stop=stop,
        )
        recovered = ScriptedTransport(
            [_VALID_RMC],
            clock=clock,
            stop=stop,
            stop_on_exhaustion=True,
        )
        waiter = ScriptedWaiter()
        service = GpsService(
            transport_factory=ScriptedFactory([malformed, recovered]),
            limits=_limits(
                read_size_bytes=4096,
                max_consecutive_parse_errors=4,
            ),
            monotonic_ns=clock,
            reconnect_waiter=waiter,
        )

        await service.run(stop)

        snapshot = service.snapshot
        assert snapshot.state is GpsState.NAVIGATION_VALID
        assert snapshot.navigation is not None
        assert snapshot.counters.lines_received == 5
        assert snapshot.counters.parse_errors == 4
        assert snapshot.counters.transport_errors == 1
        assert snapshot.counters.connections == 2
        assert snapshot.counters.reconnects == 1
        assert snapshot.counters.disconnects == 1
        assert malformed.close_calls == 1
        assert recovered.close_calls == 1
        assert waiter.delays == [0.1]

    asyncio.run(scenario())


def test_valid_supported_sentence_resets_consecutive_parse_error_limit() -> None:
    async def scenario() -> None:
        stop = asyncio.Event()
        clock = FakeClock()
        transport = ScriptedTransport(
            [_BAD_CHECKSUM * 3 + _VALID_RMC + _BAD_CHECKSUM * 3],
            clock=clock,
            stop=stop,
            stop_on_exhaustion=True,
        )
        service = GpsService(
            transport_factory=ScriptedFactory([transport]),
            limits=_limits(
                read_size_bytes=4096,
                max_consecutive_parse_errors=4,
            ),
            monotonic_ns=clock,
        )

        await service.run(stop)

        snapshot = service.snapshot
        assert snapshot.state is GpsState.NAVIGATION_VALID
        assert snapshot.navigation is not None
        assert snapshot.counters.lines_received == 7
        assert snapshot.counters.parse_errors == 6
        assert snapshot.counters.transport_errors == 0
        assert snapshot.counters.connections == 1

    asyncio.run(scenario())


def test_parse_error_rate_limit_trips_even_when_valid_sentences_reset_streak() -> None:
    async def scenario() -> None:
        stop = asyncio.Event()
        clock = FakeClock()
        malformed = ScriptedTransport(
            [(_BAD_CHECKSUM + _VALID_RMC) * 5],
            clock=clock,
            stop=stop,
        )
        recovered = ScriptedTransport(
            [_VALID_RMC],
            clock=clock,
            stop=stop,
            stop_on_exhaustion=True,
        )
        service = GpsService(
            transport_factory=ScriptedFactory([malformed, recovered]),
            limits=_limits(
                read_size_bytes=4096,
                max_consecutive_parse_errors=8,
                max_parse_errors_per_second=4,
            ),
            monotonic_ns=clock,
            reconnect_waiter=ScriptedWaiter(),
        )

        await service.run(stop)

        snapshot = service.snapshot
        assert snapshot.state is GpsState.NAVIGATION_VALID
        assert snapshot.counters.lines_received == 10
        assert snapshot.counters.parse_errors == 5
        assert snapshot.counters.valid_sentences == 5
        assert snapshot.counters.transport_errors == 1
        assert snapshot.counters.disconnects == 1
        assert snapshot.counters.reconnects == 1

    asyncio.run(scenario())


def test_parse_error_rate_window_resets_after_one_second() -> None:
    async def scenario() -> None:
        stop = asyncio.Event()
        clock = FakeClock()
        transport = ScriptedTransport(
            [
                _BAD_CHECKSUM * 3 + _VALID_RMC,
                TimedRead(_SECOND, _BAD_CHECKSUM * 3 + _VALID_RMC),
            ],
            clock=clock,
            stop=stop,
            stop_on_exhaustion=True,
        )
        service = GpsService(
            transport_factory=ScriptedFactory([transport]),
            limits=_limits(
                read_size_bytes=4096,
                max_consecutive_parse_errors=4,
                max_parse_errors_per_second=4,
            ),
            monotonic_ns=clock,
        )

        await service.run(stop)

        snapshot = service.snapshot
        assert snapshot.state is GpsState.NAVIGATION_VALID
        assert snapshot.counters.lines_received == 8
        assert snapshot.counters.parse_errors == 6
        assert snapshot.counters.valid_sentences == 2
        assert snapshot.counters.transport_errors == 0

    asyncio.run(scenario())


def test_late_valid_fix_replaces_receiving_invalid_state() -> None:
    async def scenario() -> None:
        stop = asyncio.Event()
        clock = FakeClock()
        transport = ScriptedTransport(
            [
                _INVALID_FIX,
                TimedRead(_SECOND, b""),
                TimedRead(_SECOND, b""),
                _VALID_RMC,
            ],
            clock=clock,
            stop=stop,
            stop_on_exhaustion=True,
        )
        service = GpsService(
            transport_factory=ScriptedFactory([transport]),
            limits=_limits(stale_after_s=5.0),
            monotonic_ns=clock,
            telemetry_collector=GpsTelemetryCollector(
                max_sample_hz=10,
                stale_after_ns=5 * _SECOND,
                history_capacity=20,
            ),
        )

        await service.run(stop)

        snapshot = service.snapshot
        assert snapshot.state is GpsState.NAVIGATION_VALID
        assert snapshot.navigation is not None
        assert snapshot.navigation.latitude_deg == pytest.approx(48.1173)
        assert snapshot.counters.read_timeouts == 2
        assert snapshot.counters.valid_sentences == 2
        assert snapshot.counters.valid_fixes == 1
        assert snapshot.telemetry_counters.sentences_considered == 2
        assert snapshot.telemetry_counters.invalid_navigation == 1
        assert snapshot.telemetry_counters.samples_emitted == 1
        telemetry = service.telemetry_window(0, 3 * _SECOND, max_samples=20)
        assert telemetry.complete
        assert len(telemetry.samples) == 1
        assert telemetry.samples[0].latitude_deg == pytest.approx(48.1173)

    asyncio.run(scenario())


def test_silence_marks_gps_stale_and_clears_current_navigation() -> None:
    async def scenario() -> None:
        stop = asyncio.Event()
        clock = FakeClock()
        transport = ScriptedTransport(
            [_VALID_RMC, TimedRead(2 * _SECOND + 1, b"")],
            clock=clock,
            stop=stop,
            stop_on_exhaustion=True,
        )
        service = GpsService(
            transport_factory=ScriptedFactory([transport]),
            limits=_limits(stale_after_s=2.0),
            monotonic_ns=clock,
        )

        await service.run(stop)

        snapshot = service.snapshot
        assert snapshot.state is GpsState.STALE
        assert snapshot.navigation is None
        assert snapshot.latest_sentence is not None
        assert snapshot.counters.stale_transitions == 1

    asyncio.run(scenario())


def test_disconnect_clears_fix_and_reconnect_accepts_new_fix() -> None:
    async def scenario() -> None:
        stop = asyncio.Event()
        clock = FakeClock()
        first = ScriptedTransport(
            [_VALID_RMC, OSError("cable removed")],
            clock=clock,
            stop=stop,
        )
        second = ScriptedTransport(
            [_LATER_RMC],
            clock=clock,
            stop=stop,
            stop_on_exhaustion=True,
        )
        waiter = ScriptedWaiter()
        service = GpsService(
            transport_factory=ScriptedFactory([first, second]),
            limits=_limits(),
            monotonic_ns=clock,
            reconnect_waiter=waiter,
        )

        await service.run(stop)

        snapshot = service.snapshot
        assert snapshot.state is GpsState.NAVIGATION_VALID
        assert snapshot.navigation is not None
        assert snapshot.navigation.talker == "GN"
        assert snapshot.counters.connections == 2
        assert snapshot.counters.reconnects == 1
        assert snapshot.counters.disconnects == 1
        assert snapshot.counters.valid_fixes == 2
        assert first.close_calls == 1
        assert second.close_calls == 1
        assert waiter.delays == [0.1]

    asyncio.run(scenario())


def test_task_cancellation_closes_transport_and_propagates_cancellation() -> None:
    class BlockingTransport:
        def __init__(self) -> None:
            self.read_started = asyncio.Event()
            self.close_calls = 0

        async def read(self, max_bytes: int, timeout_s: float) -> bytes:
            del max_bytes, timeout_s
            self.read_started.set()
            await asyncio.Future[None]()
            raise AssertionError("unreachable")

        async def close(self) -> None:
            self.close_calls += 1

    async def scenario() -> None:
        stop = asyncio.Event()
        transport = BlockingTransport()
        service = GpsService(
            transport_factory=ScriptedFactory([transport]),
            limits=_limits(),
        )
        task = asyncio.create_task(service.run(stop))
        await transport.read_started.wait()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert transport.close_calls == 1
        assert not service.snapshot.connected

    asyncio.run(scenario())


def test_transport_cannot_exceed_read_bound_or_grow_internal_buffer() -> None:
    async def scenario() -> None:
        stop = asyncio.Event()
        clock = FakeClock()
        transport = ScriptedTransport(
            [b"x" * 33],
            clock=clock,
            stop=stop,
            stop_on_exhaustion=True,
        )
        service = GpsService(
            transport_factory=ScriptedFactory([transport]),
            limits=_limits(read_size_bytes=32),
            monotonic_ns=clock,
        )

        await service.run(stop)

        snapshot = service.snapshot
        assert snapshot.state is GpsState.FAULTED
        assert snapshot.navigation is None
        assert snapshot.buffered_bytes == 0
        assert snapshot.counters.bytes_received == 0
        assert snapshot.counters.transport_errors == 1
        assert snapshot.last_error is GpsServiceError.TRANSPORT_PROTOCOL

    asyncio.run(scenario())


def test_high_volume_chunking_preserves_the_exact_public_snapshot() -> None:
    async def run_with_chunks(chunks: list[ReadAction]) -> GpsSnapshot:
        stop = asyncio.Event()
        clock = FakeClock()
        transport = ScriptedTransport(
            chunks,
            clock=clock,
            stop=stop,
            stop_on_exhaustion=True,
        )
        service = GpsService(
            transport_factory=ScriptedFactory([transport]),
            limits=_limits(
                read_size_bytes=4096,
                max_consecutive_parse_errors=8,
                max_parse_errors_per_second=10_000,
            ),
            monotonic_ns=clock,
        )
        await service.run(stop)
        return service.snapshot

    stream = (_VALID_RMC + _INVALID_FIX + _UNSUPPORTED + _BAD_CHECKSUM) * 1_000
    coarse_chunks: list[ReadAction] = [
        stream[offset : offset + 4096] for offset in range(0, len(stream), 4096)
    ]
    uneven_chunks: list[ReadAction] = [
        stream[offset : offset + 251] for offset in range(0, len(stream), 251)
    ]

    coarse = asyncio.run(run_with_chunks(coarse_chunks))
    uneven = asyncio.run(run_with_chunks(uneven_chunks))

    assert coarse == uneven
    assert coarse.state is GpsState.RECEIVING_INVALID
    assert coarse.counters.lines_received == 4_000
    assert coarse.counters.valid_sentences == 2_000
    assert coarse.counters.parse_errors == 2_000
    assert coarse.counters.valid_fixes == 1_000


def test_observed_snapshots_do_not_follow_later_mutable_state() -> None:
    service = GpsService(
        transport_factory=ScriptedFactory([]),
        limits=_limits(),
        monotonic_ns=FakeClock(),
    )
    initial = service.snapshot

    service._on_connected()
    connected = service.snapshot
    service._consume_chunk(_VALID_RMC[:7], 0)
    partial = service.snapshot
    service._consume_chunk(_VALID_RMC[7:], 0)
    final = service.snapshot

    assert initial.state is GpsState.UART_UNAVAILABLE
    assert not initial.connected
    assert initial.buffered_bytes == 0
    assert initial.counters == initial.counters.__class__()
    assert connected.state is GpsState.RECEIVING_INVALID
    assert connected.connected
    assert connected.buffered_bytes == 0
    assert connected.counters.connections == 1
    assert partial.buffered_bytes == 7
    assert partial.counters.valid_sentences == 0
    assert final.state is GpsState.NAVIGATION_VALID
    assert final.buffered_bytes == 0
    assert final.counters.valid_sentences == 1


def test_freshness_refresh_with_no_transition_preserves_observable_values() -> None:
    service = GpsService(
        transport_factory=ScriptedFactory([]),
        limits=_limits(),
        monotonic_ns=FakeClock(),
    )
    service._on_connected()
    service._consume_chunk(_VALID_RMC, 0)
    before = service.snapshot
    mutable_state_identity = id(service._state)

    for now_ns in (0, 1, 2, 3):
        service._refresh_freshness(now_ns)

    assert id(service._state) == mutable_state_identity
    assert service.snapshot == before
    assert service.snapshot.counters.stale_transitions == 0


def test_line_oriented_consumer_matches_bytewise_reference_across_boundaries() -> None:
    stream = (
        _VALID_RMC
        + _INVALID_FIX
        + _UNSUPPORTED
        + _BAD_CHECKSUM
        + b"$"
        + b"A" * MAX_NMEA_LINE_BYTES
        + b"\n"
        + b"$"
        + b"B" * (MAX_NMEA_LINE_BYTES - 1)
        + b"\n"
        + b"\xff\n"
        + _LATER_RMC
        + b"$G"
    )

    for chunk_size in range(1, 129):
        limits = _limits(
            read_size_bytes=4096,
            max_consecutive_parse_errors=32,
            max_parse_errors_per_second=10_000,
        )
        optimized = GpsService(
            transport_factory=ScriptedFactory([]),
            limits=limits,
        )
        reference = ReferenceBytewiseGpsService(
            transport_factory=ScriptedFactory([]),
            limits=limits,
        )
        optimized._on_connected()
        reference._on_connected()

        for index, offset in enumerate(range(0, len(stream), chunk_size)):
            chunk = stream[offset : offset + chunk_size]
            received_ns = index * 1_000_000
            optimized._consume_chunk(chunk, received_ns)
            reference._consume_chunk(chunk, received_ns)
            optimized._refresh_freshness(received_ns)
            reference._refresh_freshness(received_ns)
            assert optimized.snapshot == reference.snapshot


def test_line_oriented_consumer_matches_parse_guard_failure_boundary() -> None:
    stream = _BAD_CHECKSUM * 5
    for chunk_size in range(1, len(_BAD_CHECKSUM) + 2):
        limits = _limits(
            read_size_bytes=4096,
            max_consecutive_parse_errors=4,
            max_parse_errors_per_second=10_000,
        )
        optimized = GpsService(
            transport_factory=ScriptedFactory([]),
            limits=limits,
        )
        reference = ReferenceBytewiseGpsService(
            transport_factory=ScriptedFactory([]),
            limits=limits,
        )
        optimized._on_connected()
        reference._on_connected()

        optimized_error: RuntimeError | None = None
        reference_error: RuntimeError | None = None
        for offset in range(0, len(stream), chunk_size):
            chunk = stream[offset : offset + chunk_size]
            try:
                optimized._consume_chunk(chunk, 0)
            except RuntimeError as error:
                optimized_error = error
            try:
                reference._consume_chunk(chunk, 0)
            except RuntimeError as error:
                reference_error = error
            assert (type(optimized_error), str(optimized_error)) == (
                type(reference_error),
                str(reference_error),
            )
            assert optimized.snapshot == reference.snapshot
            if optimized_error is not None:
                break

        assert optimized_error is not None


def test_line_oriented_consumer_preserves_exact_oversized_discard_boundaries() -> None:
    service = GpsService(
        transport_factory=ScriptedFactory([]),
        limits=_limits(),
    )
    service._on_connected()

    service._consume_chunk(b"A" * MAX_NMEA_LINE_BYTES, 0)
    at_limit = service.snapshot
    service._consume_chunk(b"X", 0)
    overflowed = service.snapshot
    service._consume_chunk(b"discarded\n", 0)
    resynchronized = service.snapshot
    service._consume_chunk(b"B" * MAX_NMEA_LINE_BYTES + b"\n", 0)
    newline_overflow = service.snapshot

    assert at_limit.buffered_bytes == MAX_NMEA_LINE_BYTES
    assert at_limit.counters.oversized_lines == 0
    assert overflowed.buffered_bytes == 0
    assert overflowed.discarding_oversized_line
    assert overflowed.counters.oversized_lines == 1
    assert resynchronized.buffered_bytes == 0
    assert not resynchronized.discarding_oversized_line
    assert resynchronized.counters.oversized_lines == 1
    assert newline_overflow.buffered_bytes == 0
    assert not newline_overflow.discarding_oversized_line
    assert newline_overflow.counters.oversized_lines == 2
    assert newline_overflow.counters.lines_received == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"read_size_bytes": 0},
        {"read_size_bytes": 4097},
        {"open_timeout_s": 0.0},
        {"read_timeout_s": 0.0},
        {"read_coalesce_s": -0.001},
        {"read_coalesce_s": 0.101},
        {"read_coalesce_s": float("nan")},
        {"read_coalesce_s": float("inf")},
        {"stale_after_s": 0.0},
        {"reconnect_min_s": 2.0, "reconnect_max_s": 1.0},
        {"max_line_bytes": MAX_NMEA_LINE_BYTES + 1},
        {"max_consecutive_parse_errors": 0},
        {"max_consecutive_parse_errors": 4097},
        {"max_parse_errors_per_second": 0},
        {"max_parse_errors_per_second": 10_001},
    ],
)
def test_service_limits_reject_unbounded_or_invalid_values(
    changes: dict[str, float | int],
) -> None:
    with pytest.raises(ValueError):
        _limits(**changes)
