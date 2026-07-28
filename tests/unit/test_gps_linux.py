"""Tests for the stdlib-only Linux UART GPS adapter."""

from __future__ import annotations

import asyncio
import errno
import os
import stat
import sys
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from dashcam.gps import linux
from dashcam.gps.linux import LinuxGpsTransportError, LinuxGpsTransportFactory


@dataclass
class FakeOperations:
    descriptor: int = 41
    character_device: bool = True
    open_error: BaseException | None = None
    fstat_error: BaseException | None = None
    configure_error: BaseException | None = None
    restore_error: BaseException | None = None
    close_error: BaseException | None = None
    reads: deque[object] = field(default_factory=deque)
    opened: list[tuple[str, int]] = field(default_factory=list)
    configured: list[tuple[int, int]] = field(default_factory=list)
    restored: list[tuple[int, object]] = field(default_factory=list)
    closed: list[int] = field(default_factory=list)
    read_calls: list[tuple[int, int]] = field(default_factory=list)
    original: object = field(default_factory=object)

    def open_flags(self) -> int:
        # Deliberately fixed POSIX values: this fake makes Windows collection
        # exercise the adapter without pretending the host has termios flags.
        return 0x0800 | 0x0100 | 0x0004

    def open(self, path: str, flags: int) -> int:
        self.opened.append((path, flags))
        if self.open_error is not None:
            raise self.open_error
        return self.descriptor

    def fstat(self, _descriptor: int) -> os.stat_result:
        if self.fstat_error is not None:
            raise self.fstat_error
        mode = stat.S_IFCHR if self.character_device else stat.S_IFREG
        return SimpleNamespace(st_mode=mode)  # type: ignore[return-value]

    def is_character_device(self, mode: int) -> bool:
        return stat.S_ISCHR(mode)

    def configure_raw_8n1(self, descriptor: int, baud: int) -> object:
        self.configured.append((descriptor, baud))
        if self.configure_error is not None:
            raise self.configure_error
        return self.original

    def restore(self, descriptor: int, original: object) -> None:
        self.restored.append((descriptor, original))
        if self.restore_error is not None:
            raise self.restore_error

    def read(self, descriptor: int, maximum_bytes: int) -> bytes:
        self.read_calls.append((descriptor, maximum_bytes))
        if not self.reads:
            raise BlockingIOError(errno.EAGAIN, "would block")
        value = self.reads.popleft()
        if isinstance(value, BaseException):
            raise value
        return value  # type: ignore[return-value]

    def close(self, descriptor: int) -> None:
        self.closed.append(descriptor)
        if self.close_error is not None:
            raise self.close_error


@dataclass
class FakeReaderLoop:
    """A deterministic selector seam that never touches host file descriptors."""

    trigger_immediately: bool = False
    registered: list[tuple[int, Callable[[], None]]] = field(default_factory=list)
    removed: list[int] = field(default_factory=list)

    def add_reader(self, descriptor: int, callback: Callable[[], None]) -> None:
        self.registered.append((descriptor, callback))
        if self.trigger_immediately:
            asyncio.get_running_loop().call_soon(callback)

    def remove_reader(self, descriptor: int) -> bool:
        self.removed.append(descriptor)
        return True

    def create_future(self) -> asyncio.Future[None]:
        return asyncio.get_running_loop().create_future()


@dataclass
class FakeTermios:
    """Minimal Linux termios surface proving the configured bit contract."""

    PARENB: int = 0x001
    CSTOPB: int = 0x002
    CSIZE: int = 0x00C
    CS8: int = 0x008
    CLOCAL: int = 0x010
    CREAD: int = 0x020
    CRTSCTS: int = 0x040
    VMIN: int = 0
    VTIME: int = 1
    TCSANOW: int = 0
    B115200: int = 115_200
    original: list[object] = field(
        default_factory=lambda: [0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF, 0, 0, [9, 9, 9]]
    )
    set_calls: list[tuple[int, int, list[object]]] = field(default_factory=list)

    def tcgetattr(self, _descriptor: int) -> list[object]:
        return self.original

    def tcsetattr(self, descriptor: int, when: int, attributes: list[object]) -> None:
        self.set_calls.append((descriptor, when, attributes))


def _factory(
    operations: FakeOperations,
    *,
    baud: int = 115_200,
    reader_loop: FakeReaderLoop | None = None,
) -> LinuxGpsTransportFactory:
    loop = reader_loop or FakeReaderLoop()
    return LinuxGpsTransportFactory("/dev/serial0", baud, operations, lambda: loop)


@pytest.mark.parametrize("baud", [4_800, 9_600, 38_400, 57_600, 115_200])
def test_factory_accepts_exact_supported_baud_rates(baud: int) -> None:
    factory = _factory(FakeOperations(), baud=baud)
    assert factory.baud == baud


@pytest.mark.parametrize(
    "device",
    ["serial0", "/tmp/gps", "/dev/../ttyS0", "/dev//ttyS0", "/dev/", ""],
)
def test_factory_refuses_non_dev_paths(device: str) -> None:
    with pytest.raises(ValueError, match="path"):
        LinuxGpsTransportFactory(device, 115_200, FakeOperations())


def test_factory_accepts_path_input_without_changing_the_opened_path() -> None:
    async def scenario() -> None:
        operations = FakeOperations()
        transport = await LinuxGpsTransportFactory(
            Path("/dev/serial0"),
            115_200,
            operations,
            lambda: FakeReaderLoop(),
        ).open()
        assert operations.opened == [("/dev/serial0", operations.open_flags())]
        await transport.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("baud", [1_200, 4_801, True, "115200"])
def test_factory_fails_closed_for_unsupported_baud(baud: object) -> None:
    with pytest.raises(ValueError, match="supported"):
        LinuxGpsTransportFactory("/dev/serial0", baud, FakeOperations())  # type: ignore[arg-type]


def test_open_uses_required_flags_and_raw_configuration() -> None:
    async def scenario() -> None:
        operations = FakeOperations()
        transport = await _factory(operations).open()
        assert operations.opened == [("/dev/serial0", operations.open_flags())]
        assert operations.configured == [(41, 115_200)]
        await transport.close()

    asyncio.run(scenario())


def test_open_and_close_never_depend_on_asyncio_shared_worker_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def forbidden_to_thread(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("GPS descriptor lifecycle must not use the shared executor")

    async def scenario() -> None:
        operations = FakeOperations(reads=deque([b"ok"]))
        monkeypatch.setattr(asyncio, "to_thread", forbidden_to_thread)
        transport = await _factory(operations).open()
        assert await transport.read(2, 0.1) == b"ok"
        await transport.close()
        assert operations.restored == [(41, operations.original)]
        assert operations.closed == [41]

    asyncio.run(scenario())


def test_raw_8n1_configuration_clears_both_flow_control_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_termios = FakeTermios()
    monkeypatch.setattr(linux, "_termios_module", fake_termios)
    monkeypatch.setattr(sys, "platform", "linux")

    original = linux._PosixSerialOperations().configure_raw_8n1(41, 115_200)

    assert original is fake_termios.original
    assert len(fake_termios.set_calls) == 1
    _, _, configured = fake_termios.set_calls[0]
    assert configured[0] == 0
    assert configured[1] == 0
    cflag = configured[2]
    assert isinstance(cflag, int)
    assert cflag & fake_termios.PARENB == 0
    assert cflag & fake_termios.CSTOPB == 0
    assert cflag & fake_termios.CSIZE == fake_termios.CS8
    assert cflag & fake_termios.CRTSCTS == 0
    assert cflag & fake_termios.CLOCAL
    assert cflag & fake_termios.CREAD
    assert configured[3] == 0
    assert configured[4:6] == [fake_termios.B115200, fake_termios.B115200]
    assert configured[6] == [1, 0, 9]


def test_default_factory_fails_closed_when_linux_termios_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        with pytest.raises(LinuxGpsTransportError, match="Linux termios"):
            await LinuxGpsTransportFactory("/dev/serial0", 115_200).open()

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["open", "fstat", "configure"])
def test_open_or_configuration_failure_closes_descriptor(stage: str) -> None:
    async def scenario() -> None:
        operations = FakeOperations()
        if stage == "open":
            operations.open_error = OSError(errno.ENOENT, "missing\nsecret\0")
        elif stage == "fstat":
            operations.fstat_error = OSError(errno.EIO, "bad")
        else:
            operations.configure_error = OSError(errno.EINVAL, "bad")
        with pytest.raises(OSError):
            await _factory(operations).open()
        assert operations.closed == ([] if stage == "open" else [41])

    asyncio.run(scenario())


def test_non_character_device_is_refused_and_closed() -> None:
    async def scenario() -> None:
        operations = FakeOperations(character_device=False)
        with pytest.raises(LinuxGpsTransportError, match="character device"):
            await _factory(operations).open()
        assert operations.configured == []
        assert operations.closed == [41]

    asyncio.run(scenario())


def test_read_returns_data_or_empty_on_deadline_and_is_bounded() -> None:
    async def scenario() -> None:
        operations = FakeOperations(reads=deque([b"abc", BlockingIOError(errno.EAGAIN, "again")]))
        reader_loop = FakeReaderLoop()
        transport = await _factory(operations, reader_loop=reader_loop).open()
        assert await transport.read(3, 0.1) == b"abc"
        assert await transport.read(3, 0.001) == b""
        assert all(maximum == 3 for _, maximum in operations.read_calls)
        assert [descriptor for descriptor, _ in reader_loop.registered] == [41]
        assert reader_loop.removed == [41]
        await transport.close()

    asyncio.run(scenario())


def test_read_retries_eagain_after_selector_readiness() -> None:
    async def scenario() -> None:
        operations = FakeOperations(reads=deque([BlockingIOError(errno.EAGAIN, "again"), b"ok"]))
        reader_loop = FakeReaderLoop(trigger_immediately=True)
        transport = await _factory(operations, reader_loop=reader_loop).open()
        assert await transport.read(2, 0.1) == b"ok"
        assert [descriptor for descriptor, _ in reader_loop.registered] == [41]
        assert reader_loop.removed == [41]
        await transport.close()

    asyncio.run(scenario())


def test_persistent_ready_eagain_is_bounded_and_faults() -> None:
    async def scenario() -> None:
        maximum_ready_wakeups = 8
        operations = FakeOperations(
            reads=deque(
                BlockingIOError(errno.EAGAIN, "again")
                for _ in range(maximum_ready_wakeups + 1)
            )
        )
        reader_loop = FakeReaderLoop(trigger_immediately=True)
        transport = await _factory(operations, reader_loop=reader_loop).open()
        with pytest.raises(LinuxGpsTransportError, match="readable without data") as raised:
            await transport.read(2, 1.0)
        assert raised.value.errno == errno.EIO
        assert operations.read_calls == [(41, 2)] * (maximum_ready_wakeups + 1)
        assert len(reader_loop.registered) == maximum_ready_wakeups + 1
        assert len(reader_loop.removed) == maximum_ready_wakeups + 1
        await transport.close()

    asyncio.run(scenario())


def test_empty_tty_read_is_a_single_bounded_transport_failure() -> None:
    async def scenario() -> None:
        operations = FakeOperations(reads=deque([b""]))
        reader_loop = FakeReaderLoop(trigger_immediately=True)
        transport = await _factory(operations, reader_loop=reader_loop).open()
        with pytest.raises(LinuxGpsTransportError, match="zero-byte read") as raised:
            await transport.read(2, 0.1)
        assert raised.value.errno == errno.EIO
        assert operations.read_calls == [(41, 2)]
        assert reader_loop.registered == []
        assert reader_loop.removed == []
        await transport.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("chunk", [b"toolong", bytearray(b"x"), "not-bytes"])
def test_read_refuses_adapter_protocol_violations(chunk: object) -> None:
    async def scenario() -> None:
        operations = FakeOperations(reads=deque([chunk]))
        transport = await _factory(operations).open()
        with pytest.raises(LinuxGpsTransportError) as raised:
            await transport.read(3, 0.1)
        assert raised.value.errno == errno.EPROTO
        await transport.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("max_bytes, timeout_s", [(0, 1), (4_097, 1), (1, 0), (1, float("inf"))])
def test_read_validates_bounds(max_bytes: int, timeout_s: float) -> None:
    async def scenario() -> None:
        transport = await _factory(FakeOperations()).open()
        with pytest.raises(ValueError):
            await transport.read(max_bytes, timeout_s)
        await transport.close()

    asyncio.run(scenario())


def test_read_cancellation_releases_the_transport_for_close() -> None:
    async def scenario() -> None:
        operations = FakeOperations()
        reader_loop = FakeReaderLoop()
        transport = await _factory(operations, reader_loop=reader_loop).open()
        task = asyncio.create_task(transport.read(1, 10.0))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await transport.close()
        assert operations.closed == [41]
        assert reader_loop.removed == [41]

    asyncio.run(scenario())


def test_close_restores_once_and_is_idempotent_even_with_restore_error() -> None:
    async def scenario() -> None:
        operations = FakeOperations(restore_error=OSError(errno.EIO, "restore failed\n\0"))
        transport = await _factory(operations).open()
        with pytest.raises(OSError):
            await transport.close()
        await transport.close()
        assert operations.restored == [(41, operations.original)]
        assert operations.closed == [41]

    asyncio.run(scenario())


def test_close_timeout_still_restores_and_closes_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        operations = FakeOperations()
        transport = await _factory(operations).open()
        await transport._read_lock.acquire()
        monkeypatch.setattr(linux, "_CLOSE_DRAIN_TIMEOUT_S", 0.001)
        try:
            with pytest.raises(LinuxGpsTransportError) as raised:
                await transport.close()
            assert raised.value.errno == errno.ETIMEDOUT
        finally:
            transport._read_lock.release()
        await transport.close()
        assert operations.restored == [(41, operations.original)]
        assert operations.closed == [41]

    asyncio.run(scenario())


def test_close_wins_a_read_race_without_blocking_the_event_loop() -> None:
    async def scenario() -> None:
        operations = FakeOperations()
        transport = await _factory(operations).open()
        read_task = asyncio.create_task(transport.read(1, 1.0))
        await asyncio.sleep(0.02)
        started = time.monotonic()
        await transport.close()
        assert time.monotonic() - started < 0.5
        with pytest.raises(LinuxGpsTransportError, match="closed"):
            await read_task
        assert operations.closed == [41]

    asyncio.run(scenario())


def test_closed_transport_refuses_new_reads_without_invoking_adapter() -> None:
    async def scenario() -> None:
        operations = FakeOperations()
        transport = await _factory(operations).open()
        await transport.close()
        with pytest.raises(LinuxGpsTransportError, match="closed"):
            await transport.read(1, 0.1)
        assert operations.read_calls == []

    asyncio.run(scenario())
