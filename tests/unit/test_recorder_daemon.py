from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from dashcam.config import ConfigError, DashcamConfig, default_config
from dashcam.recorder.daemon import (
    DaemonLimits,
    DaemonOutcome,
    RecorderDaemon,
)
from dashcam.recorder.status import RecorderReason
from dashcam.state import RecorderState

AsyncTest = Callable[[], Coroutine[Any, Any, None]]
REPOSITORY_ROOT = Path(__file__).parents[2]
DEFAULT_CONFIG_PATH = REPOSITORY_ROOT / "config" / "default.toml"


def run_async(test: AsyncTest) -> None:
    asyncio.run(test())


@dataclass
class FakeRuntime:
    start_error: BaseException | None = None
    run_error: BaseException | None = None
    stop_error: BaseException | None = None
    start_gate: asyncio.Event | None = None
    run_gate: asyncio.Event | None = None
    stop_gate: asyncio.Event | None = None
    started_with: list[DashcamConfig] = field(default_factory=list)
    run_calls: int = 0
    stop_calls: int = 0
    stop_event_seen: asyncio.Event | None = None

    async def start(self, config: DashcamConfig) -> None:
        self.started_with.append(config)
        if self.start_gate is not None:
            await self.start_gate.wait()
        if self.start_error is not None:
            raise self.start_error

    async def run(self, stop_requested: asyncio.Event) -> None:
        self.run_calls += 1
        self.stop_event_seen = stop_requested
        if self.run_gate is not None:
            await self.run_gate.wait()
        else:
            await stop_requested.wait()
        if self.run_error is not None:
            raise self.run_error

    async def stop(self) -> None:
        self.stop_calls += 1
        if self.stop_gate is not None:
            await self.stop_gate.wait()
        if self.stop_error is not None:
            raise self.stop_error


@dataclass
class RecordingNotifier:
    messages: list[tuple[str, str | None]] = field(default_factory=list)
    fail_operation: str | None = None
    watchdog_seen: asyncio.Event = field(default_factory=asyncio.Event)

    def _record(self, operation: str, status: str | None = None) -> bool:
        self.messages.append((operation, status))
        if operation == "watchdog":
            self.watchdog_seen.set()
        return operation != self.fail_operation

    def ready(self, status: str) -> bool:
        return self._record("ready", status)

    def status(self, status: str) -> bool:
        return self._record("status", status)

    def watchdog(self) -> bool:
        return self._record("watchdog")

    def stopping(self, status: str) -> bool:
        return self._record("stopping", status)


class RaisingNotifier(RecordingNotifier):
    def status(self, status: str) -> bool:
        raise OSError("simulated notification failure")


def fast_limits(
    *,
    startup_timeout_s: float = 0.1,
    shutdown_timeout_s: float = 0.1,
    watchdog_interval_s: float = 0.01,
) -> DaemonLimits:
    return DaemonLimits(
        startup_timeout_s=startup_timeout_s,
        shutdown_timeout_s=shutdown_timeout_s,
        watchdog_interval_s=watchdog_interval_s,
    )


def test_loads_config_reports_ready_watchdog_and_clean_shutdown(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime = FakeRuntime()
        notifier = RecordingNotifier()
        loaded_paths: list[Path] = []
        config = default_config()

        def load(path: str | Path) -> DashcamConfig:
            loaded_paths.append(Path(path))
            return config

        daemon = RecorderDaemon(
            config_path=tmp_path / "dashcam.toml",
            runtime=runtime,
            config_loader=load,
            notifier=notifier,
            limits=fast_limits(),
        )
        daemon_task = asyncio.create_task(daemon.run())
        await asyncio.wait_for(notifier.watchdog_seen.wait(), timeout=0.2)

        assert daemon.status.state is RecorderState.RECORDING
        assert daemon.status.config_schema_version == 1
        assert runtime.started_with == [config]
        assert loaded_paths == [tmp_path / "dashcam.toml"]
        assert ("ready", "state=RECORDING config_schema=1") in notifier.messages

        daemon.request_stop()
        daemon.request_stop()
        result = await asyncio.wait_for(daemon_task, timeout=0.2)

        assert result.outcome is DaemonOutcome.STOPPED
        assert result.clean
        assert result.final_status.state is RecorderState.STOPPING
        assert runtime.stop_calls == 1
        assert runtime.stop_event_seen is not None
        assert runtime.stop_event_seen.is_set()
        assert any(operation == "stopping" for operation, _ in notifier.messages)

    run_async(scenario)


def test_default_loader_integrates_with_checked_in_config() -> None:
    async def scenario() -> None:
        runtime = FakeRuntime()
        daemon = RecorderDaemon(config_path=DEFAULT_CONFIG_PATH, runtime=runtime)
        daemon.request_stop()

        result = await daemon.run()

        assert result.clean
        assert runtime.started_with == [default_config()]
        assert result.final_status.config_schema_version == 1

    run_async(scenario)


def test_config_error_is_reported_without_starting_runtime(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime = FakeRuntime()

        def fail_load(path: str | Path) -> DashcamConfig:
            raise ConfigError(f"invalid configuration at {Path(path).name}")

        daemon = RecorderDaemon(
            config_path=tmp_path / "bad.toml",
            runtime=runtime,
            config_loader=fail_load,
        )

        result = await daemon.run()

        assert result.outcome is DaemonOutcome.CONFIG_ERROR
        assert result.final_status.state is RecorderState.FAULTED
        assert result.final_status.reason is RecorderReason.CONFIG_ERROR
        assert "bad.toml" in (result.final_status.detail or "")
        assert runtime.started_with == []
        assert runtime.run_calls == 0
        assert runtime.stop_calls == 0

    run_async(scenario)


@pytest.mark.parametrize(
    ("runtime", "outcome", "reason"),
    [
        (
            FakeRuntime(start_error=RuntimeError("prepare failed")),
            DaemonOutcome.STARTUP_FAILED,
            RecorderReason.STARTUP_FAILED,
        ),
        (
            FakeRuntime(run_gate=asyncio.Event()),
            DaemonOutcome.RUNTIME_EXITED,
            RecorderReason.RUNTIME_EXITED,
        ),
        (
            FakeRuntime(run_gate=asyncio.Event(), run_error=RuntimeError("pipeline failed")),
            DaemonOutcome.RUNTIME_FAILED,
            RecorderReason.RUNTIME_FAILED,
        ),
    ],
)
def test_runtime_failure_paths_are_visible_and_bounded(
    runtime: FakeRuntime,
    outcome: DaemonOutcome,
    reason: RecorderReason,
) -> None:
    async def scenario() -> None:
        if runtime.run_gate is not None:
            runtime.run_gate.set()
        daemon = RecorderDaemon(
            config_path="config.toml",
            runtime=runtime,
            config_loader=lambda path: default_config(),
            limits=fast_limits(),
        )

        result = await asyncio.wait_for(daemon.run(), timeout=0.3)

        assert result.outcome is outcome
        assert result.final_status.state is RecorderState.FAULTED
        assert result.final_status.reason is reason
        assert runtime.stop_calls == 1

    run_async(scenario)


def test_startup_timeout_cancels_start_and_runs_bounded_cleanup() -> None:
    async def scenario() -> None:
        runtime = FakeRuntime(start_gate=asyncio.Event())
        daemon = RecorderDaemon(
            config_path="config.toml",
            runtime=runtime,
            config_loader=lambda path: default_config(),
            limits=fast_limits(startup_timeout_s=0.01),
        )

        result = await asyncio.wait_for(daemon.run(), timeout=0.2)

        assert result.outcome is DaemonOutcome.STARTUP_TIMEOUT
        assert result.final_status.reason is RecorderReason.STARTUP_TIMEOUT
        assert runtime.stop_calls == 1

    run_async(scenario)


@pytest.mark.parametrize(
    ("runtime", "outcome", "reason"),
    [
        (
            FakeRuntime(stop_error=RuntimeError("finalization failed")),
            DaemonOutcome.SHUTDOWN_FAILED,
            RecorderReason.SHUTDOWN_FAILED,
        ),
        (
            FakeRuntime(stop_gate=asyncio.Event()),
            DaemonOutcome.SHUTDOWN_TIMEOUT,
            RecorderReason.SHUTDOWN_TIMEOUT,
        ),
    ],
)
def test_shutdown_failure_and_timeout_are_reported(
    runtime: FakeRuntime,
    outcome: DaemonOutcome,
    reason: RecorderReason,
) -> None:
    async def scenario() -> None:
        notifier = RecordingNotifier()
        daemon = RecorderDaemon(
            config_path="config.toml",
            runtime=runtime,
            config_loader=lambda path: default_config(),
            notifier=notifier,
            limits=fast_limits(shutdown_timeout_s=0.01),
        )
        task = asyncio.create_task(daemon.run())
        while not any(operation == "ready" for operation, _ in notifier.messages):
            await asyncio.sleep(0)

        daemon.request_stop()
        result = await asyncio.wait_for(task, timeout=0.2)

        assert result.outcome is outcome
        assert result.final_status.state is RecorderState.FAULTED
        assert result.final_status.reason is reason
        assert runtime.stop_calls == 1

    run_async(scenario)


def test_task_cancellation_requests_cleanup_and_is_propagated() -> None:
    async def scenario() -> None:
        runtime = FakeRuntime()
        notifier = RecordingNotifier()
        daemon = RecorderDaemon(
            config_path="config.toml",
            runtime=runtime,
            config_loader=lambda path: default_config(),
            notifier=notifier,
            limits=fast_limits(),
        )
        task = asyncio.create_task(daemon.run())
        while not any(operation == "ready" for operation, _ in notifier.messages):
            await asyncio.sleep(0)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert daemon.stop_requested
        assert daemon.status.state is RecorderState.STOPPING
        assert runtime.stop_calls == 1

    run_async(scenario)


def test_cancellation_during_startup_still_calls_runtime_stop() -> None:
    async def scenario() -> None:
        runtime = FakeRuntime(start_gate=asyncio.Event())
        daemon = RecorderDaemon(
            config_path="config.toml",
            runtime=runtime,
            config_loader=lambda path: default_config(),
            limits=fast_limits(),
        )
        task = asyncio.create_task(daemon.run())
        while not runtime.started_with:
            await asyncio.sleep(0)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert daemon.stop_requested
        assert daemon.status.state is RecorderState.STOPPING
        assert runtime.stop_calls == 1

    run_async(scenario)


def test_notifier_failure_never_terminates_recording() -> None:
    async def scenario() -> None:
        runtime = FakeRuntime()
        notifier = RecordingNotifier(fail_operation="watchdog")
        daemon = RecorderDaemon(
            config_path="config.toml",
            runtime=runtime,
            config_loader=lambda path: default_config(),
            notifier=notifier,
            limits=fast_limits(),
        )
        task = asyncio.create_task(daemon.run())
        await asyncio.wait_for(notifier.watchdog_seen.wait(), timeout=0.2)

        assert daemon.status.state is RecorderState.RECORDING
        assert daemon.status.notification_failures >= 1

        daemon.request_stop()
        result = await task
        assert result.clean

    run_async(scenario)


def test_notifier_exception_is_isolated_from_recording() -> None:
    async def scenario() -> None:
        runtime = FakeRuntime()
        daemon = RecorderDaemon(
            config_path="config.toml",
            runtime=runtime,
            config_loader=lambda path: default_config(),
            notifier=RaisingNotifier(),
            limits=fast_limits(),
        )
        task = asyncio.create_task(daemon.run())
        while daemon.status.state is not RecorderState.RECORDING:
            await asyncio.sleep(0)

        assert daemon.status.notification_failures >= 1
        daemon.request_stop()
        result = await task
        assert result.clean

    run_async(scenario)


def test_stop_requested_before_run_skips_ready_notification() -> None:
    async def scenario() -> None:
        runtime = FakeRuntime()
        notifier = RecordingNotifier()
        daemon = RecorderDaemon(
            config_path="config.toml",
            runtime=runtime,
            config_loader=lambda path: default_config(),
            notifier=notifier,
            limits=fast_limits(),
        )
        daemon.request_stop()

        result = await daemon.run()

        assert result.clean
        assert result.final_status.state is RecorderState.STOPPING
        assert runtime.stop_calls == 1
        assert all(operation != "ready" for operation, _ in notifier.messages)

    run_async(scenario)


def test_daemon_is_single_use() -> None:
    async def scenario() -> None:
        daemon = RecorderDaemon(
            config_path="config.toml",
            runtime=FakeRuntime(),
            config_loader=lambda path: default_config(),
            limits=fast_limits(),
        )
        daemon.request_stop()
        await daemon.run()

        with pytest.raises(RuntimeError, match="single-use"):
            await daemon.run()

    run_async(scenario)


@pytest.mark.parametrize(
    "limits",
    [
        {"startup_timeout_s": 0},
        {"shutdown_timeout_s": 301},
        {"watchdog_interval_s": 0},
        {"startup_timeout_s": True},
    ],
)
def test_daemon_limits_reject_unbounded_values(limits: dict[str, float | bool]) -> None:
    with pytest.raises(ValueError):
        DaemonLimits(**limits)
