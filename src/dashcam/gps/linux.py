"""Receive-only Linux UART adapter for the GPS transport contracts.

The adapter intentionally uses only the standard library.  Its file descriptor
is opened read-only and nonblocking; reads are driven by the event loop's file
descriptor readiness notifications, rather than by a thread per poll.
"""

from __future__ import annotations

import asyncio
import errno
import math
import os
import stat
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any, Final, Protocol

_termios_module: ModuleType | None
try:  # Importing this module must remain safe during Windows test collection.
    import termios
except ImportError:  # pragma: no cover - exercised on unsupported platforms.
    _termios_module = None
else:
    _termios_module = termios


_BAUD_RATES: Final = frozenset({4_800, 9_600, 38_400, 57_600, 115_200})
_MAX_READ_BYTES: Final = 4_096
_MAX_TIMEOUT_S: Final = 30.0
_CLOSE_DRAIN_TIMEOUT_S: Final = 1.0
_MAX_ERROR_CHARS: Final = 160
_MAX_CONSECUTIVE_READY_WITHOUT_PROGRESS: Final = 8


class LinuxGpsTransportError(OSError):
    """Fail-closed GPS UART adapter error with a bounded diagnostic."""


class _SerialOperations(Protocol):
    """Small POSIX seam used by the platform-independent unit tests."""

    def open_flags(self) -> int: ...

    def open(self, path: str, flags: int) -> int: ...

    def fstat(self, descriptor: int) -> os.stat_result: ...

    def is_character_device(self, mode: int) -> bool: ...

    def configure_raw_8n1(self, descriptor: int, baud: int) -> object: ...

    def restore(self, descriptor: int, original: object) -> None: ...

    def read(self, descriptor: int, maximum_bytes: int) -> bytes: ...

    def close(self, descriptor: int) -> None: ...


class _PosixSerialOperations:
    """The production POSIX implementation, instantiated only on open."""

    def _require_linux_termios(self) -> Any:
        if sys.platform != "linux" or _termios_module is None:
            raise LinuxGpsTransportError(errno.ENOSYS, "Linux termios support is unavailable")
        return _termios_module

    def open_flags(self) -> int:
        self._require_linux_termios()
        return _open_flags()

    def open(self, path: str, flags: int) -> int:
        self._require_linux_termios()
        return os.open(path, flags)

    def fstat(self, descriptor: int) -> os.stat_result:
        return os.fstat(descriptor)

    def is_character_device(self, mode: int) -> bool:
        return stat.S_ISCHR(mode)

    def configure_raw_8n1(self, descriptor: int, baud: int) -> object:
        termios = self._require_linux_termios()
        speed = getattr(termios, f"B{baud}", None)
        if not isinstance(speed, int):
            raise LinuxGpsTransportError(errno.EINVAL, f"unsupported GPS UART baud: {baud}")

        original = termios.tcgetattr(descriptor)
        configured = list(original)
        configured[0] = 0  # raw input: includes IXON/IXOFF/IXANY disabled
        configured[1] = 0  # raw output processing disabled
        cflag = configured[2] & ~(termios.PARENB | termios.CSTOPB | termios.CSIZE)
        crtscts = getattr(termios, "CRTSCTS", 0)
        if isinstance(crtscts, int):
            cflag &= ~crtscts
        configured[2] = cflag | termios.CS8 | termios.CLOCAL | termios.CREAD
        configured[3] = 0  # noncanonical, no echo, no signal generation
        controls = list(configured[6])
        # O_NONBLOCK bounds the read itself.  VMIN=1 additionally prevents a
        # quiet PL011 from presenting a perpetual readable/zero-byte state.
        controls[termios.VMIN] = 1
        controls[termios.VTIME] = 0
        configured[6] = controls
        configured[4] = speed
        configured[5] = speed
        try:
            termios.tcsetattr(descriptor, termios.TCSANOW, configured)
        except BaseException:
            with suppress(OSError):
                termios.tcsetattr(descriptor, termios.TCSANOW, original)
            raise
        return original

    def restore(self, descriptor: int, original: object) -> None:
        termios = self._require_linux_termios()
        termios.tcsetattr(descriptor, termios.TCSANOW, original)

    def read(self, descriptor: int, maximum_bytes: int) -> bytes:
        return os.read(descriptor, maximum_bytes)

    def close(self, descriptor: int) -> None:
        os.close(descriptor)


class _ReaderLoop(Protocol):
    """Subset of an asyncio selector loop required for nonblocking UART reads."""

    def add_reader(self, descriptor: int, callback: Callable[[], None]) -> Any: ...

    def remove_reader(self, descriptor: int) -> Any: ...

    def create_future(self) -> asyncio.Future[None]: ...


@dataclass(frozen=True, slots=True)
class LinuxGpsTransportFactory:
    """Open one configured Linux character-device UART in raw receive-only mode."""

    device: str | Path
    baud: int
    _operations: _SerialOperations | None = None
    _reader_loop_factory: Callable[[], _ReaderLoop] | None = None
    _device_text: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_device_text", _validate_device(self.device))
        _validate_baud(self.baud)

    async def open(self) -> LinuxGpsTransport:
        """Open/configure the nonblocking UART without the shared executor.

        Every operation here is bounded kernel descriptor/termios work after an
        ``O_NONBLOCK`` open.  Keeping it on the event-loop thread avoids a
        production deadlock where long-lived media workers occupy asyncio's
        shared executor and a cancelled queued UART open can never run its
        cleanup.
        """

        operations = self._operations or _PosixSerialOperations()
        try:
            descriptor, original = _open_configured_uart(
                operations,
                self._device_text,
                self.baud,
            )
        except OSError as error:
            raise _transport_error(error, "GPS UART open/configuration failed") from error
        return LinuxGpsTransport(
            descriptor=descriptor,
            original_termios=original,
            operations=operations,
            reader_loop_factory=self._reader_loop_factory or asyncio.get_running_loop,
        )


class LinuxGpsTransport:
    """A single nonblocking descriptor satisfying ``GpsTransport``."""

    def __init__(
        self,
        *,
        descriptor: int,
        original_termios: object,
        operations: _SerialOperations,
        reader_loop_factory: Callable[[], _ReaderLoop] = asyncio.get_running_loop,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if isinstance(descriptor, bool) or not isinstance(descriptor, int) or descriptor < 0:
            raise ValueError("descriptor must be a non-negative integer")
        if not callable(monotonic):
            raise TypeError("monotonic must be callable")
        self._descriptor: int | None = descriptor
        self._original_termios = original_termios
        self._operations = operations
        self._reader_loop_factory = reader_loop_factory
        self._monotonic = monotonic
        self._read_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._close_requested = asyncio.Event()
        self._closed = False

    async def read(self, max_bytes: int, timeout_s: float) -> bytes:
        """Read one bounded chunk, returning ``b""`` when the deadline expires."""

        _validate_read_request(max_bytes, timeout_s)
        try:
            async with asyncio.timeout(timeout_s):
                async with self._read_lock:
                    descriptor = self._require_open_descriptor()
                    return await self._read_locked(descriptor, max_bytes, timeout_s)
        except TimeoutError:
            return b""

    async def close(self) -> None:
        """Wake a pending read, restore termios, and close exactly once."""

        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._close_requested.set()
            descriptor = self._descriptor
            self._descriptor = None
            assert descriptor is not None

            # A read waiter observes ``_close_requested``.  Taking the same lock
            # proves no direct os.read remains before the descriptor is reused.
            drain_error: LinuxGpsTransportError | None = None
            try:
                async with asyncio.timeout(_CLOSE_DRAIN_TIMEOUT_S):
                    async with self._read_lock:
                        pass
            except TimeoutError:
                drain_error = LinuxGpsTransportError(
                    errno.ETIMEDOUT,
                    "GPS UART read did not stop for close",
                )
            try:
                _restore_and_close(
                    self._operations,
                    descriptor,
                    self._original_termios,
                )
            except OSError as error:
                raise _transport_error(error, "GPS UART close failed") from error
            if drain_error is not None:
                raise drain_error

    def _require_open_descriptor(self) -> int:
        if self._closed or self._descriptor is None:
            raise LinuxGpsTransportError(errno.EBADF, "GPS UART transport is closed")
        return self._descriptor

    async def _read_locked(self, descriptor: int, max_bytes: int, timeout_s: float) -> bytes:
        deadline = self._monotonic() + timeout_s
        readiness_without_progress = 0
        while True:
            if self._closed:
                raise LinuxGpsTransportError(errno.EBADF, "GPS UART transport is closed")
            try:
                chunk = self._operations.read(descriptor, max_bytes)
            except BlockingIOError:
                chunk = None
            except OSError as error:
                if error.errno not in {errno.EAGAIN, errno.EWOULDBLOCK}:
                    raise _transport_error(error, "GPS UART read failed") from error
                chunk = None
            if chunk is not None:
                if not isinstance(chunk, bytes) or len(chunk) > max_bytes:
                    raise LinuxGpsTransportError(
                        errno.EPROTO,
                        "GPS UART adapter returned an invalid read chunk",
                    )
                if not chunk:
                    raise LinuxGpsTransportError(
                        errno.EIO,
                        "GPS UART returned an unexpected zero-byte read",
                    )
                return chunk

            remaining_s = deadline - self._monotonic()
            if remaining_s <= 0:
                return b""
            if not await self._wait_for_readiness(descriptor, remaining_s):
                if self._closed:
                    raise LinuxGpsTransportError(errno.EBADF, "GPS UART transport is closed")
                return b""
            readiness_without_progress += 1
            if readiness_without_progress > _MAX_CONSECUTIVE_READY_WITHOUT_PROGRESS:
                raise LinuxGpsTransportError(
                    errno.EIO,
                    "GPS UART remained readable without data",
                )

    async def _wait_for_readiness(self, descriptor: int, remaining_s: float) -> bool:
        loop = self._reader_loop_factory()
        ready = loop.create_future()

        def mark_ready() -> None:
            if not ready.done():
                ready.set_result(None)

        try:
            loop.add_reader(descriptor, mark_ready)
        except (AttributeError, NotImplementedError, OSError, ValueError) as error:
            raise LinuxGpsTransportError(
                errno.ENOSYS,
                "event loop cannot wait for GPS UART input",
            ) from error
        close_wait = asyncio.create_task(self._wait_for_close())
        try:
            done, _ = await asyncio.wait(
                {ready, close_wait}, timeout=remaining_s, return_when=asyncio.FIRST_COMPLETED
            )
            return ready in done and not self._closed
        finally:
            with suppress(Exception):
                loop.remove_reader(descriptor)
            if not close_wait.done():
                close_wait.cancel()
            with suppress(asyncio.CancelledError):
                await close_wait

    async def _wait_for_close(self) -> None:
        await self._close_requested.wait()


def _open_configured_uart(
    operations: _SerialOperations,
    device: str,
    baud: int,
) -> tuple[int, object]:
    descriptor = -1
    original: object | None = None
    try:
        descriptor = operations.open(device, operations.open_flags())
        if isinstance(descriptor, bool) or not isinstance(descriptor, int) or descriptor < 0:
            raise LinuxGpsTransportError(errno.EIO, "GPS UART open returned an invalid descriptor")
        status = operations.fstat(descriptor)
        if not operations.is_character_device(status.st_mode):
            raise LinuxGpsTransportError(
                errno.ENOTTY,
                "configured GPS path is not a character device",
            )
        original = operations.configure_raw_8n1(descriptor, baud)
        return descriptor, original
    except BaseException:
        if descriptor >= 0:
            if original is not None:
                with suppress(OSError):
                    operations.restore(descriptor, original)
            with suppress(OSError):
                operations.close(descriptor)
        raise


def _restore_and_close(operations: _SerialOperations, descriptor: int, original: object) -> None:
    restore_error: OSError | None = None
    try:
        operations.restore(descriptor, original)
    except OSError as error:
        restore_error = error
    try:
        operations.close(descriptor)
    except OSError:
        if restore_error is None:
            raise
    if restore_error is not None:
        raise restore_error


def _open_flags() -> int:
    required = ("O_RDONLY", "O_NOCTTY", "O_NONBLOCK", "O_CLOEXEC")
    if any(not hasattr(os, name) for name in required):
        raise LinuxGpsTransportError(errno.ENOSYS, "required Linux UART open flags are unavailable")
    flags = 0
    for name in required:
        value = getattr(os, name)
        if isinstance(value, bool) or not isinstance(value, int):
            raise LinuxGpsTransportError(errno.ENOSYS, "required Linux UART open flags are invalid")
        flags |= value
    return flags


def _validate_device(device: object) -> str:
    if not isinstance(device, str | Path):
        raise ValueError("GPS device must be a path string or Path")
    value = str(device).replace("\\", "/")
    if not value or len(value) > 256 or "\0" in value:
        raise ValueError("GPS device must be a bounded non-empty path")
    path = PurePosixPath(value)
    if (
        not value.startswith("/dev/")
        or ".." in path.parts
        or len(path.parts) < 3
        or path.as_posix() != value
    ):
        raise ValueError("GPS device must be an absolute path below /dev")
    return value


def _validate_baud(baud: object) -> None:
    if isinstance(baud, bool) or not isinstance(baud, int) or baud not in _BAUD_RATES:
        supported = ", ".join(str(value) for value in sorted(_BAUD_RATES))
        raise ValueError(f"unsupported GPS baud; supported values are {supported}")


def _validate_read_request(max_bytes: object, timeout_s: object) -> None:
    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or not 1 <= max_bytes <= _MAX_READ_BYTES
    ):
        raise ValueError(f"max_bytes must be an integer between 1 and {_MAX_READ_BYTES}")
    if (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, int | float)
        or not math.isfinite(timeout_s)
        or not 0 < timeout_s <= _MAX_TIMEOUT_S
    ):
        raise ValueError(f"timeout_s must be finite and between 0 and {_MAX_TIMEOUT_S:g}")


def _transport_error(error: OSError, context: str) -> LinuxGpsTransportError:
    number = error.errno if isinstance(error.errno, int) else errno.EIO
    detail = " ".join(str(error).replace("\0", " ").split())
    message = f"{context}: {detail}" if detail else context
    return LinuxGpsTransportError(number, message[:_MAX_ERROR_CHARS])


__all__ = ["LinuxGpsTransport", "LinuxGpsTransportError", "LinuxGpsTransportFactory"]
