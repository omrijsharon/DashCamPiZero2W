from __future__ import annotations

import asyncio
import signal
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest

from dashcam.daemon import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_IDENTITY_PATH,
    RecorderDaemonFactory,
    _build_parser,
    _build_production_runtime,
    _install_stop_handlers,
    _remove_stop_handlers,
    run_daemon,
)
from dashcam.recorder.daemon import (
    DaemonOutcome,
    DaemonResult,
    RecorderDaemon,
    RecorderRuntime,
    StorageGate,
)
from dashcam.recorder.metrics import RuntimeSnapshotPublisher
from dashcam.recorder.notifier import ServiceNotifier
from dashcam.recorder.status import RecorderStatus
from dashcam.state import RecorderState, StorageState
from dashcam.storage.preflight import PreflightResult


@dataclass
class FakeRuntime:
    async def check(self, config: object) -> PreflightResult:
        return PreflightResult(StorageState.READY, (), None, True, True)

    async def start(self, config: object) -> None:
        return None

    async def run(self, stop_requested: asyncio.Event) -> None:
        await stop_requested.wait()

    async def stop(self) -> None:
        return None


class FakeNotifier:
    def ready(self, status: str) -> bool:
        return True

    def status(self, status: str) -> bool:
        return True

    def watchdog(self) -> bool:
        return True

    def stopping(self, status: str) -> bool:
        return True


class FakeDaemon:
    def __init__(self, result: DaemonResult) -> None:
        self._result = result
        self.stop_requests = 0
        self.run_started = asyncio.Event()
        self.release_run = asyncio.Event()

    def request_stop(self) -> None:
        self.stop_requests += 1
        self.release_run.set()

    async def run(self) -> DaemonResult:
        self.run_started.set()
        await self.release_run.wait()
        return self._result


class FakeEventLoop:
    def __init__(self) -> None:
        self.handlers: dict[signal.Signals, Callable[[], object]] = {}
        self.removed: list[signal.Signals] = []

    def add_signal_handler(
        self,
        stop_signal: signal.Signals,
        callback: Callable[[], object],
    ) -> None:
        self.handlers[stop_signal] = callback

    def remove_signal_handler(self, stop_signal: signal.Signals) -> bool:
        self.removed.append(stop_signal)
        return True


def _result(outcome: DaemonOutcome) -> DaemonResult:
    return DaemonResult(
        outcome=outcome,
        final_status=RecorderStatus(state=RecorderState.STOPPING, sequence=1),
    )


def _daemon_factory(daemon: FakeDaemon) -> RecorderDaemonFactory:
    def factory(
        *,
        config_path: Path,
        runtime: RecorderRuntime,
        storage_gate: StorageGate,
        notifier: ServiceNotifier,
        snapshot_publisher: RuntimeSnapshotPublisher,
    ) -> RecorderDaemon:
        assert config_path == Path("/tmp/config.toml")
        assert isinstance(runtime, FakeRuntime)
        assert storage_gate is runtime
        assert isinstance(notifier, FakeNotifier)
        assert isinstance(snapshot_publisher, RuntimeSnapshotPublisher)
        return cast(RecorderDaemon, daemon)

    return factory


def test_parser_defaults_and_explicit_paths() -> None:
    parser = _build_parser()

    defaults = parser.parse_args([])
    explicit = parser.parse_args(["--config", "/tmp/config.toml", "--identity", "/tmp/volume.env"])

    assert defaults.config == DEFAULT_CONFIG_PATH
    assert defaults.identity == DEFAULT_IDENTITY_PATH
    assert explicit.config == Path("/tmp/config.toml")
    assert explicit.identity == Path("/tmp/volume.env")


@pytest.mark.parametrize(
    ("outcome", "expected_exit"),
    [(DaemonOutcome.STOPPED, 0), (DaemonOutcome.STARTUP_FAILED, 1)],
)
def test_run_daemon_maps_only_clean_stop_to_zero(
    outcome: DaemonOutcome,
    expected_exit: int,
) -> None:
    async def scenario() -> None:
        daemon = FakeDaemon(_result(outcome))
        factory_calls: list[tuple[Path, Path]] = []

        def runtime_factory(*, config_path: Path, identity_path: Path) -> FakeRuntime:
            factory_calls.append((config_path, identity_path))
            return FakeRuntime()

        task = asyncio.create_task(
            run_daemon(
                config_path=Path("/tmp/config.toml"),
                identity_path=Path("/tmp/identity.env"),
                runtime_factory=runtime_factory,
                notifier_factory=FakeNotifier,
                daemon_factory=_daemon_factory(daemon),
            )
        )
        await daemon.run_started.wait()
        daemon.request_stop()

        assert await task == expected_exit
        assert factory_calls == [(Path("/tmp/config.toml"), Path("/tmp/identity.env"))]

    asyncio.run(scenario())


def test_stop_signal_requests_cooperative_shutdown_and_handlers_are_removed() -> None:
    daemon = FakeDaemon(_result(DaemonOutcome.STOPPED))
    loop = FakeEventLoop()
    event_loop = cast(asyncio.AbstractEventLoop, loop)

    installed = _install_stop_handlers(event_loop, cast(RecorderDaemon, daemon))
    loop.handlers[signal.SIGTERM]()
    _remove_stop_handlers(event_loop, installed)

    assert daemon.stop_requests == 1
    assert set(loop.handlers) == {signal.SIGINT, signal.SIGTERM}
    assert set(loop.removed) == {signal.SIGINT, signal.SIGTERM}


def test_cancelled_entrypoint_requests_stop_and_propagates_cancellation() -> None:
    async def scenario() -> None:
        daemon = FakeDaemon(_result(DaemonOutcome.STOPPED))
        task = asyncio.create_task(
            run_daemon(
                config_path=Path("/tmp/config.toml"),
                identity_path=Path("/tmp/identity.env"),
                runtime_factory=lambda **_: FakeRuntime(),
                notifier_factory=FakeNotifier,
                daemon_factory=_daemon_factory(daemon),
            )
        )
        await daemon.run_started.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
        assert daemon.stop_requests == 1

    asyncio.run(scenario())


def test_production_runtime_factory_is_lazy_and_passes_both_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imported: list[tuple[Path, Path]] = []
    runtime_module = ModuleType("dashcam.recorder.runtime")

    def build_production_runtime(*, config_path: Path, identity_path: Path) -> FakeRuntime:
        imported.append((config_path, identity_path))
        return FakeRuntime()

    runtime_module.build_production_runtime = build_production_runtime  # type: ignore[attr-defined]
    monkeypatch.delitem(sys.modules, "dashcam.recorder.runtime", raising=False)
    assert "dashcam.recorder.runtime" not in sys.modules
    monkeypatch.setitem(sys.modules, "dashcam.recorder.runtime", runtime_module)

    runtime = _build_production_runtime(
        config_path=Path("/tmp/config.toml"), identity_path=Path("/tmp/identity.env")
    )

    assert isinstance(runtime, FakeRuntime)
    assert imported == [(Path("/tmp/config.toml"), Path("/tmp/identity.env"))]
