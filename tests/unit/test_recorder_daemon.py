from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest

from dashcam.config import ConfigError, DashcamConfig, default_config
from dashcam.recorder.daemon import (
    DaemonLimits,
    DaemonOutcome,
    RecorderDaemon,
)
from dashcam.recorder.gstreamer import GStreamerLimits
from dashcam.recorder.runtime import (
    PipelineRecoveryExhausted,
    RecorderFinalizationFault,
    RecorderStorageFault,
    RuntimeLifecycleEvent,
    RuntimeLifecycleEventKind,
    RuntimeLifecycleObserver,
    RuntimeLimits,
    StorageSafetyStop,
)
from dashcam.recorder.status import RecorderReason
from dashcam.state import RecorderState, StorageState
from dashcam.storage.preflight import PreflightReason, PreflightResult, RecordingRootFacts

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
    lifecycle_observer: RuntimeLifecycleObserver | None = None

    def bind_lifecycle_observer(self, observer: RuntimeLifecycleObserver) -> None:
        self.lifecycle_observer = observer

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
class ControlRuntime(FakeRuntime):
    control_start_error: Exception | None = None
    lifecycle: list[str] = field(default_factory=list)
    control_fault_callback: Callable[[str], None] | None = None

    async def start(self, config: DashcamConfig) -> None:
        self.lifecycle.append("runtime_start")
        await super().start(config)

    async def start_control_endpoint(
        self,
        status_provider: Callable[[], object],
        fault_callback: Callable[[str], None],
    ) -> None:
        assert status_provider() is not None
        self.control_fault_callback = fault_callback
        self.lifecycle.append("control_start")
        if self.control_start_error is not None:
            raise self.control_start_error

    async def stop_control_endpoint(self) -> None:
        self.lifecycle.append("control_stop")

    async def stop(self) -> None:
        self.lifecycle.append("runtime_stop")
        await super().stop()


@dataclass
class ProgressRuntime(FakeRuntime):
    progress_tokens: list[int | None] = field(default_factory=lambda: [0])
    progress_error: BaseException | None = None
    progress_calls: int = 0

    def recording_progress_token(self) -> int | None:
        self.progress_calls += 1
        if self.progress_error is not None:
            raise self.progress_error
        index = min(self.progress_calls - 1, len(self.progress_tokens) - 1)
        return self.progress_tokens[index]


@dataclass
class FakeStorageGate:
    result: PreflightResult
    error: BaseException | None = None
    gate: asyncio.Event | None = None
    checked_with: list[DashcamConfig] = field(default_factory=list)

    async def check(self, config: DashcamConfig) -> PreflightResult:
        self.checked_with.append(config)
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return self.result


def ready_storage() -> PreflightResult:
    return PreflightResult(StorageState.READY, (), None, True, True)


def faulted_storage() -> PreflightResult:
    return PreflightResult(
        StorageState.FAULTED,
        (PreflightReason.UNMOUNTED,),
        None,
        False,
        False,
    )


def reserve_exhausted_storage() -> PreflightResult:
    return PreflightResult(
        StorageState.EMERGENCY,
        (PreflightReason.RESERVE_EXHAUSTED,),
        cast(RecordingRootFacts, object()),
        False,
        False,
    )


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


def watchdog_count(notifier: RecordingNotifier) -> int:
    return sum(operation == "watchdog" for operation, _ in notifier.messages)


async def wait_for_daemon_state(
    daemon: RecorderDaemon,
    state: RecorderState,
) -> None:
    while daemon.status.state is not state:
        await asyncio.sleep(0)


def fast_limits(
    *,
    startup_timeout_s: float = 0.1,
    storage_timeout_s: float = 0.1,
    shutdown_timeout_s: float = 0.1,
    watchdog_interval_s: float = 0.01,
) -> DaemonLimits:
    return DaemonLimits(
        startup_timeout_s=startup_timeout_s,
        storage_timeout_s=storage_timeout_s,
        shutdown_timeout_s=shutdown_timeout_s,
        watchdog_interval_s=watchdog_interval_s,
    )


def test_production_shutdown_budget_fits_below_systemd_deadline() -> None:
    media = GStreamerLimits()
    runtime = RuntimeLimits()
    internal_budget = (
        media.eos_timeout_s
        + media.null_timeout_s
        + runtime.finalizer_timeout_s
        + runtime.task_stop_timeout_s
    )

    assert internal_budget == 19.0
    assert internal_budget < DaemonLimits().shutdown_timeout_s < 30.0


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


def test_advancing_encoded_progress_keeps_watchdog_healthy() -> None:
    async def scenario() -> None:
        runtime = ProgressRuntime(progress_tokens=[10, 20, 30, 40])
        notifier = RecordingNotifier()
        daemon = RecorderDaemon(
            config_path="config.toml",
            runtime=runtime,
            config_loader=lambda path: default_config(),
            notifier=notifier,
            limits=fast_limits(),
        )
        task = asyncio.create_task(daemon.run())
        while watchdog_count(notifier) < 3:
            await asyncio.sleep(0)

        assert not task.done()
        assert daemon.status.state is RecorderState.RECORDING
        assert runtime.progress_calls >= 3
        daemon.request_stop()
        assert (await task).clean

    run_async(scenario)


def test_stalled_encoded_progress_faults_and_runs_bounded_cleanup() -> None:
    async def scenario() -> None:
        runtime = ProgressRuntime(progress_tokens=[77])
        notifier = RecordingNotifier()
        daemon = RecorderDaemon(
            config_path="config.toml",
            runtime=runtime,
            config_loader=lambda path: default_config(),
            notifier=notifier,
            limits=fast_limits(),
        )

        result = await asyncio.wait_for(daemon.run(), timeout=0.2)

        assert result.outcome is DaemonOutcome.PIPELINE_NO_PROGRESS
        assert result.final_status.state is RecorderState.FAULTED
        assert result.final_status.reason is RecorderReason.PIPELINE_NO_PROGRESS
        assert "did not advance" in (result.final_status.detail or "")
        assert watchdog_count(notifier) == 2
        assert runtime.stop_calls == 1
        assert runtime.stop_event_seen is not None
        assert runtime.stop_event_seen.is_set()

    run_async(scenario)


def test_recovery_lifecycle_resets_stall_baseline() -> None:
    async def scenario() -> None:
        runtime = ProgressRuntime(progress_tokens=[50, 50, 51, 52])
        notifier = RecordingNotifier()
        daemon = RecorderDaemon(
            config_path="config.toml",
            runtime=runtime,
            config_loader=lambda path: default_config(),
            notifier=notifier,
            limits=fast_limits(watchdog_interval_s=0.02),
        )
        task = asyncio.create_task(daemon.run())
        while watchdog_count(notifier) < 1:
            await asyncio.sleep(0)
        assert runtime.lifecycle_observer is not None
        runtime.lifecycle_observer(
            RuntimeLifecycleEvent(
                RuntimeLifecycleEventKind.RECOVERING,
                restart_count=0,
                recovery_attempt=1,
                detail="attempt=1/3 backoff_s=1 cause=test",
            )
        )
        while daemon.status.state.value != "FAULTED":
            await asyncio.sleep(0)
        runtime.lifecycle_observer(
            RuntimeLifecycleEvent(
                RuntimeLifecycleEventKind.RESTARTING,
                restart_count=1,
                recovery_attempt=1,
            )
        )
        await wait_for_daemon_state(daemon, RecorderState.STARTING)
        runtime.lifecycle_observer(
            RuntimeLifecycleEvent(
                RuntimeLifecycleEventKind.RECOVERED,
                restart_count=1,
                recovery_attempt=1,
            )
        )
        await wait_for_daemon_state(daemon, RecorderState.RECORDING)
        while watchdog_count(notifier) < 3:
            await asyncio.sleep(0)

        assert not task.done()
        assert runtime.progress_calls >= 3
        daemon.request_stop()
        assert (await task).clean

    run_async(scenario)


def test_stop_after_progress_baseline_wins_cleanly_over_stall_detection() -> None:
    async def scenario() -> None:
        runtime = ProgressRuntime(progress_tokens=[9])
        notifier = RecordingNotifier()
        daemon = RecorderDaemon(
            config_path="config.toml",
            runtime=runtime,
            config_loader=lambda path: default_config(),
            notifier=notifier,
            limits=fast_limits(),
        )
        task = asyncio.create_task(daemon.run())
        while watchdog_count(notifier) < 1:
            await asyncio.sleep(0)

        daemon.request_stop()
        result = await asyncio.wait_for(task, timeout=0.2)

        assert result.clean
        assert result.final_status.state is RecorderState.STOPPING
        assert 1 <= watchdog_count(notifier) <= 2

    run_async(scenario)


def test_watchdog_progress_exception_is_supervised_and_consumed() -> None:
    async def scenario() -> None:
        contexts: list[dict[str, object]] = []
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: contexts.append(context))
        try:
            runtime = ProgressRuntime(progress_error=RuntimeError("counter read failed"))
            daemon = RecorderDaemon(
                config_path="config.toml",
                runtime=runtime,
                config_loader=lambda path: default_config(),
                limits=fast_limits(),
            )

            result = await asyncio.wait_for(daemon.run(), timeout=0.2)
            await asyncio.sleep(0)

            assert result.outcome is DaemonOutcome.RUNTIME_FAILED
            assert result.final_status.reason is RecorderReason.RUNTIME_FAILED
            assert "counter read failed" in (result.final_status.detail or "")
            assert runtime.stop_calls == 1
            assert contexts == []
        finally:
            loop.set_exception_handler(previous_handler)

    run_async(scenario)


def test_default_loader_integrates_with_checked_in_config() -> None:
    async def scenario() -> None:
        runtime = FakeRuntime()
        daemon = RecorderDaemon(config_path=DEFAULT_CONFIG_PATH, runtime=runtime)
        daemon.request_stop()

        result = await daemon.run()

        assert result.clean
        assert runtime.started_with == []
        assert result.final_status.config_schema_version == 1

    run_async(scenario)


def test_default_daemon_has_no_snapshot_writer_side_effect() -> None:
    daemon = RecorderDaemon(
        config_path="config.toml",
        runtime=FakeRuntime(),
        config_loader=lambda _path: default_config(),
    )

    assert daemon._snapshot_publisher is None


def test_fresh_storage_gate_passes_before_runtime_opens_media() -> None:
    async def scenario() -> None:
        config = default_config()
        storage = FakeStorageGate(ready_storage())
        runtime = FakeRuntime()
        daemon = RecorderDaemon(
            config_path="config.toml",
            runtime=runtime,
            config_loader=lambda path: config,
            storage_gate=storage,
            limits=fast_limits(),
        )
        task = asyncio.create_task(daemon.run())
        while not runtime.started_with:
            await asyncio.sleep(0)
        daemon.request_stop()
        result = await asyncio.wait_for(task, timeout=0.2)

        assert result.clean
        assert storage.checked_with == [config]
        assert runtime.started_with == [config]

    run_async(scenario)


def test_storage_fault_is_ready_and_observable_without_opening_camera() -> None:
    async def scenario() -> None:
        config = default_config()
        storage = FakeStorageGate(faulted_storage())
        runtime = FakeRuntime()
        notifier = RecordingNotifier()
        daemon = RecorderDaemon(
            config_path="config.toml",
            runtime=runtime,
            config_loader=lambda path: config,
            storage_gate=storage,
            notifier=notifier,
            limits=fast_limits(),
        )
        task = asyncio.create_task(daemon.run())
        while not any(operation == "ready" for operation, _ in notifier.messages):
            await asyncio.sleep(0)

        assert daemon.status.state is RecorderState.FAULTED
        assert daemon.status.reason is RecorderReason.STORAGE_FAULT
        assert daemon.status.detail == "storage state=FAULTED reasons=UNMOUNTED"
        assert runtime.started_with == []
        assert runtime.run_calls == 0
        assert runtime.stop_calls == 0

        daemon.request_stop()
        result = await asyncio.wait_for(task, timeout=0.2)

        assert result.clean
        assert result.final_status.state is RecorderState.STOPPING
        assert runtime.stop_calls == 0
        ready_messages = [
            status for operation, status in notifier.messages if operation == "ready"
        ]
        assert ready_messages == [
            "state=FAULTED reason=STORAGE_FAULT "
            "detail=storage state=FAULTED reasons=UNMOUNTED config_schema=1"
        ]

    run_async(scenario)


def test_exact_reserve_exhaustion_is_delegated_to_pre_camera_runtime_recovery() -> None:
    async def scenario() -> None:
        runtime = FakeRuntime()
        storage = FakeStorageGate(reserve_exhausted_storage())
        daemon = RecorderDaemon(
            config_path=DEFAULT_CONFIG_PATH,
            runtime=runtime,
            storage_gate=storage,
            limits=fast_limits(),
        )
        task = asyncio.create_task(daemon.run())
        await wait_for_daemon_state(daemon, RecorderState.RECORDING)

        assert runtime.started_with == [default_config()]
        daemon.request_stop()
        result = await asyncio.wait_for(task, timeout=0.2)
        assert result.clean

    run_async(scenario)


def test_startup_timeout_joins_cancelled_mutation_before_runtime_stop() -> None:
    class JoiningRuntime(FakeRuntime):
        entered = asyncio.Event()
        release = asyncio.Event()
        exited = asyncio.Event()

        async def start(self, config: DashcamConfig) -> None:
            self.started_with.append(config)
            self.entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await self.release.wait()
                self.exited.set()
                raise

        async def stop(self) -> None:
            assert self.exited.is_set()
            await super().stop()

    async def scenario() -> None:
        runtime = JoiningRuntime()
        daemon = RecorderDaemon(
            config_path=DEFAULT_CONFIG_PATH,
            runtime=runtime,
            limits=fast_limits(startup_timeout_s=0.01),
        )
        task = asyncio.create_task(daemon.run())
        await asyncio.wait_for(runtime.entered.wait(), timeout=0.1)
        await asyncio.sleep(0.02)

        assert not task.done()
        assert runtime.stop_calls == 0
        runtime.release.set()
        result = await asyncio.wait_for(task, timeout=0.2)
        assert result.outcome is DaemonOutcome.STARTUP_TIMEOUT
        assert runtime.stop_calls == 1

    run_async(scenario)


@pytest.mark.parametrize(
    ("storage", "detail"),
    [
        (
            FakeStorageGate(
                ready_storage(),
                error=OSError("private device detail must not leak"),
            ),
            "storage preflight failed",
        ),
        (
            FakeStorageGate(
                ready_storage(),
                gate=asyncio.Event(),
            ),
            "storage preflight exceeded deadline",
        ),
    ],
)
def test_storage_gate_exception_and_timeout_fail_closed_without_media(
    storage: FakeStorageGate,
    detail: str,
) -> None:
    async def scenario() -> None:
        runtime = FakeRuntime()
        notifier = RecordingNotifier()
        daemon = RecorderDaemon(
            config_path="config.toml",
            runtime=runtime,
            config_loader=lambda path: default_config(),
            storage_gate=storage,
            notifier=notifier,
            limits=fast_limits(),
        )
        task = asyncio.create_task(daemon.run())
        while not any(operation == "ready" for operation, _ in notifier.messages):
            await asyncio.sleep(0)

        assert daemon.status.reason is RecorderReason.STORAGE_FAULT
        assert daemon.status.detail == detail
        assert runtime.started_with == []

        daemon.request_stop()
        result = await asyncio.wait_for(task, timeout=0.2)
        assert result.clean

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
            FakeRuntime(start_error=RecorderStorageFault("storage preflight refused")),
            DaemonOutcome.STARTUP_FAILED,
            RecorderReason.STORAGE_FAULT,
        ),
        (
            FakeRuntime(start_error=StorageSafetyStop("emergency reserve reached")),
            DaemonOutcome.STORAGE_SAFETY_STOP,
            RecorderReason.STORAGE_FAULT,
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
        assert result.clean is (outcome is DaemonOutcome.STORAGE_SAFETY_STOP)

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


def test_runtime_recovery_events_publish_faulted_starting_recording_never_degraded() -> None:
    async def scenario() -> None:
        runtime = FakeRuntime()
        daemon = RecorderDaemon(
            config_path="config.toml",
            runtime=runtime,
            config_loader=lambda path: default_config(),
            limits=fast_limits(),
        )
        task = asyncio.create_task(daemon.run())
        while daemon.status.state is not RecorderState.RECORDING:
            await asyncio.sleep(0)
        assert runtime.lifecycle_observer is not None

        runtime.lifecycle_observer(
            RuntimeLifecycleEvent(
                RuntimeLifecycleEventKind.RECOVERING,
                restart_count=0,
                recovery_attempt=1,
                detail="RecoverablePipelineError: camera failed",
            )
        )
        while daemon.status.state.value != "FAULTED":
            await asyncio.sleep(0)
        assert daemon.status.reason is RecorderReason.PIPELINE_RECOVERING

        runtime.lifecycle_observer(
            RuntimeLifecycleEvent(
                RuntimeLifecycleEventKind.RESTARTING,
                restart_count=1,
                recovery_attempt=1,
            )
        )
        while daemon.status.state.value != "STARTING":
            await asyncio.sleep(0)
        runtime.lifecycle_observer(
            RuntimeLifecycleEvent(
                RuntimeLifecycleEventKind.RECOVERED,
                restart_count=1,
                recovery_attempt=1,
            )
        )
        while daemon.status.state.value != "RECORDING":
            await asyncio.sleep(0)

        assert daemon.status.reason is None
        daemon.request_stop()
        assert (await task).clean

    run_async(scenario)


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (
            PipelineRecoveryExhausted("restart budget exhausted"),
            RecorderReason.PIPELINE_RECOVERY_EXHAUSTED,
        ),
        (
            RecorderFinalizationFault("durable promotion failed"),
            RecorderReason.FINALIZATION_FAILED,
        ),
        (
            RecorderStorageFault("replacement mount refused"),
            RecorderReason.STORAGE_FAULT,
        ),
    ],
)
def test_terminal_runtime_faults_keep_their_stable_reason(
    error: BaseException,
    reason: RecorderReason,
) -> None:
    async def scenario() -> None:
        run_gate = asyncio.Event()
        runtime = FakeRuntime(run_gate=run_gate, run_error=error)
        daemon = RecorderDaemon(
            config_path="config.toml",
            runtime=runtime,
            config_loader=lambda path: default_config(),
            limits=fast_limits(),
        )
        task = asyncio.create_task(daemon.run())
        while daemon.status.state is not RecorderState.RECORDING:
            await asyncio.sleep(0)
        run_gate.set()

        result = await task

        assert result.outcome is DaemonOutcome.RUNTIME_FAILED
        assert result.final_status.reason is reason

    run_async(scenario)


def test_runtime_storage_safety_stop_is_critical_but_process_clean() -> None:
    async def scenario() -> None:
        run_gate = asyncio.Event()
        runtime = FakeRuntime(
            run_gate=run_gate,
            run_error=StorageSafetyStop("emergency threshold reached"),
        )
        daemon = RecorderDaemon(
            config_path="config.toml",
            runtime=runtime,
            config_loader=lambda path: default_config(),
            limits=fast_limits(),
        )
        task = asyncio.create_task(daemon.run())
        while daemon.status.state is not RecorderState.RECORDING:
            await asyncio.sleep(0)
        run_gate.set()

        result = await task

        assert result.outcome is DaemonOutcome.STORAGE_SAFETY_STOP
        assert result.clean
        assert result.final_status.state is RecorderState.FAULTED
        assert result.final_status.reason is RecorderReason.STORAGE_FAULT
        assert runtime.stop_calls == 1

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
        assert runtime.stop_calls == 0
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


def test_control_starts_after_runtime_and_drains_before_runtime_stop() -> None:
    async def scenario() -> None:
        runtime = ControlRuntime()
        daemon = RecorderDaemon(
            config_path="config.toml",
            runtime=runtime,
            config_loader=lambda path: default_config(),
            limits=fast_limits(),
        )
        task = asyncio.create_task(daemon.run())
        while daemon.status.state is not RecorderState.RECORDING:
            await asyncio.sleep(0)
        daemon.request_stop()

        result = await task

        assert result.clean
        assert runtime.lifecycle == [
            "runtime_start",
            "control_start",
            "control_stop",
            "runtime_stop",
        ]

    run_async(scenario)


def test_control_bind_failure_is_truthful_degraded_and_camera_nonfatal() -> None:
    async def scenario() -> None:
        runtime = ControlRuntime(control_start_error=OSError("injected bind failure"))
        daemon = RecorderDaemon(
            config_path="config.toml",
            runtime=runtime,
            config_loader=lambda path: default_config(),
            limits=fast_limits(),
        )
        task = asyncio.create_task(daemon.run())
        while daemon.status.state is not RecorderState.DEGRADED:
            await asyncio.sleep(0)
        assert daemon.status.reason is RecorderReason.OPTIONAL_SUBSYSTEM
        assert "control endpoint unavailable" in (daemon.status.detail or "")
        while runtime.run_calls == 0:
            await asyncio.sleep(0)
        assert runtime.run_calls == 1
        daemon.request_stop()

        result = await task

        assert result.clean
        assert runtime.lifecycle[-2:] == ["control_stop", "runtime_stop"]

    run_async(scenario)


def test_post_start_control_fault_is_degraded_without_stopping_camera() -> None:
    async def scenario() -> None:
        runtime = ControlRuntime()
        daemon = RecorderDaemon(
            config_path="config.toml",
            runtime=runtime,
            config_loader=lambda path: default_config(),
            limits=fast_limits(),
        )
        task = asyncio.create_task(daemon.run())
        while runtime.control_fault_callback is None or runtime.run_calls == 0:
            await asyncio.sleep(0)

        runtime.control_fault_callback("injected serve failure")
        assert daemon.status.state is RecorderState.DEGRADED
        assert daemon.status.reason is RecorderReason.OPTIONAL_SUBSYSTEM
        assert runtime.run_calls == 1
        assert not task.done()

        daemon.request_stop()
        assert (await task).clean

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
