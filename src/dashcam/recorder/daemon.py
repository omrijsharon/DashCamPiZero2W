"""Hardware-independent recorder daemon lifecycle orchestration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, TypeVar

from dashcam.config import ConfigError, DashcamConfig, load_config
from dashcam.recorder.metrics import RuntimeSnapshotPublisher
from dashcam.recorder.notifier import NullNotifier, ServiceNotifier
from dashcam.recorder.runtime import (
    PipelineRecoveryExhausted,
    RecorderFinalizationFault,
    RecorderStorageFault,
    RuntimeLifecycleEvent,
    RuntimeLifecycleEventKind,
    StorageSafetyStop,
)
from dashcam.recorder.status import RecorderReason, RecorderStatus, RecorderStatusStore
from dashcam.state import RecorderState
from dashcam.storage.preflight import PreflightResult

_TaskResult = TypeVar("_TaskResult")


class RecorderRuntime(Protocol):
    """Future media-runtime boundary; implementations exclusively own the camera."""

    async def start(self, config: DashcamConfig) -> None:
        """Prepare resources without returning until recording is active."""

    async def run(self, stop_requested: asyncio.Event) -> None:
        """Run until ``stop_requested`` is set and active work is finalized."""

    async def stop(self) -> None:
        """Prompt active work to stop and release resources idempotently."""


class ConfigLoader(Protocol):
    def __call__(self, path: str | Path) -> DashcamConfig:
        """Load and validate one configuration file."""


class StorageGate(Protocol):
    """Fresh in-process recording-volume verification before camera ownership."""

    async def check(self, config: DashcamConfig) -> PreflightResult:
        """Return bounded storage evidence without opening media hardware."""


@dataclass(frozen=True, slots=True)
class DaemonLimits:
    """Explicit bounds corresponding to service-manager startup/shutdown limits."""

    startup_timeout_s: float = 40.0
    storage_timeout_s: float = 20.0
    # Production runtime phases are 8s EOS + 3s NULL + 6s finalizer drain +
    # 2s run-task join = 19s.  Keep a five-second orchestration margin and a
    # further six-second manager margin below TimeoutStopSec=30s.
    shutdown_timeout_s: float = 24.0
    watchdog_interval_s: float | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.startup_timeout_s, bool)
            or not isinstance(self.startup_timeout_s, int | float)
            or not 0 < self.startup_timeout_s <= 300
        ):
            raise ValueError("startup_timeout_s must be between 0 and 300")
        if (
            isinstance(self.storage_timeout_s, bool)
            or not isinstance(self.storage_timeout_s, int | float)
            or not 0 < self.storage_timeout_s <= 300
        ):
            raise ValueError("storage_timeout_s must be between 0 and 300")
        if (
            isinstance(self.shutdown_timeout_s, bool)
            or not isinstance(self.shutdown_timeout_s, int | float)
            or not 0 < self.shutdown_timeout_s <= 300
        ):
            raise ValueError("shutdown_timeout_s must be between 0 and 300")
        if self.watchdog_interval_s is not None and (
            isinstance(self.watchdog_interval_s, bool)
            or not isinstance(self.watchdog_interval_s, int | float)
            or not 0 < self.watchdog_interval_s <= 150
        ):
            raise ValueError("watchdog_interval_s must be between 0 and 150")


class DaemonOutcome(StrEnum):
    STOPPED = "STOPPED"
    STORAGE_SAFETY_STOP = "STORAGE_SAFETY_STOP"
    CONFIG_ERROR = "CONFIG_ERROR"
    STARTUP_FAILED = "STARTUP_FAILED"
    STARTUP_TIMEOUT = "STARTUP_TIMEOUT"
    RUNTIME_EXITED = "RUNTIME_EXITED"
    RUNTIME_FAILED = "RUNTIME_FAILED"
    PIPELINE_NO_PROGRESS = "PIPELINE_NO_PROGRESS"
    SHUTDOWN_FAILED = "SHUTDOWN_FAILED"
    SHUTDOWN_TIMEOUT = "SHUTDOWN_TIMEOUT"


@dataclass(frozen=True, slots=True)
class DaemonResult:
    outcome: DaemonOutcome
    final_status: RecorderStatus

    @property
    def clean(self) -> bool:
        return self.outcome in {
            DaemonOutcome.STOPPED,
            DaemonOutcome.STORAGE_SAFETY_STOP,
        }


class PipelineNoProgressFault(RuntimeError):
    """Encoded-frame progress stalled across two recording watchdog samples."""


def _exception_detail(error: BaseException) -> str:
    raw_detail = f"{type(error).__name__}: {error}".replace("\0", " ")
    detail = " ".join(raw_detail.splitlines()).strip()
    detail = "".join(character if character.isprintable() else " " for character in detail)
    return detail[:512] if detail else type(error).__name__


class RecorderDaemon:
    """Coordinate config, lifecycle, cancellation, and supervisor notification.

    The object is single-use. It never creates a media implementation and cannot
    access hardware unless an explicitly injected ``RecorderRuntime`` does so.
    """

    def __init__(
        self,
        *,
        config_path: str | Path,
        runtime: RecorderRuntime,
        config_loader: ConfigLoader = load_config,
        storage_gate: StorageGate | None = None,
        notifier: ServiceNotifier | None = None,
        status_store: RecorderStatusStore | None = None,
        snapshot_publisher: RuntimeSnapshotPublisher | None = None,
        limits: DaemonLimits | None = None,
    ) -> None:
        self._config_path = Path(config_path)
        self._runtime = runtime
        self._config_loader = config_loader
        self._storage_gate = storage_gate
        self._notifier = notifier or NullNotifier()
        self._status_store = status_store or RecorderStatusStore()
        self._snapshot_publisher = snapshot_publisher
        self._limits = limits or DaemonLimits()
        self._stop_requested = asyncio.Event()
        self._runtime_events: asyncio.Queue[RuntimeLifecycleEvent] = asyncio.Queue(
            maxsize=16
        )
        self._recording_progress_epoch = 0
        self._started = False
        bind_observer = getattr(runtime, "bind_lifecycle_observer", None)
        if bind_observer is not None:
            if not callable(bind_observer):
                raise TypeError("runtime lifecycle observer binder must be callable")
            bind_observer(self._enqueue_runtime_event)

    @property
    def status(self) -> RecorderStatus:
        return self._status_store.snapshot()

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested.is_set()

    def request_stop(self) -> None:
        """Request idempotent cooperative shutdown."""

        self._stop_requested.set()

    def _enqueue_runtime_event(self, event: RuntimeLifecycleEvent) -> None:
        """O(1) observer callback; a full queue fails the runtime closed."""

        if not isinstance(event, RuntimeLifecycleEvent):
            raise TypeError("runtime lifecycle observer received an invalid event")
        self._recording_progress_epoch += 1
        self._runtime_events.put_nowait(event)

    def _format_status(self, status: RecorderStatus) -> str:
        parts = [f"state={status.state.value}"]
        if status.reason is not None:
            parts.append(f"reason={status.reason.value}")
        if status.detail is not None:
            parts.append(f"detail={status.detail}")
        if status.config_schema_version is not None:
            parts.append(f"config_schema={status.config_schema_version}")
        return " ".join(parts)

    def _notify(self, operation: Callable[[], bool]) -> None:
        try:
            delivered = operation()
        except Exception:
            delivered = False
        if not delivered:
            self._status_store.record_notification_failure()

    def _snapshot(self) -> None:
        if self._snapshot_publisher is None:
            return
        try:
            self._snapshot_publisher.publish(self.status, self._runtime)
        except Exception:
            # Observability is optional and must never affect recording.
            self._status_store.record_notification_failure()

    def _publish(
        self,
        state: RecorderState,
        *,
        reason: RecorderReason | None = None,
        detail: str | None = None,
        config_schema_version: int | None = None,
    ) -> RecorderStatus:
        previous_state = self._status_store.snapshot().state
        status = self._status_store.transition(
            state,
            reason=reason,
            detail=detail,
            config_schema_version=config_schema_version,
        )
        if state is not RecorderState.RECORDING or previous_state is not RecorderState.RECORDING:
            self._recording_progress_epoch += 1
        self._notify(lambda: self._notifier.status(self._format_status(status)))
        self._snapshot()
        return self.status

    async def _watchdog_loop(self, interval_s: float) -> None:
        baseline_token: int | None = None
        baseline_epoch: int | None = None
        while not self._stop_requested.is_set():
            try:
                await asyncio.wait_for(self._stop_requested.wait(), timeout=interval_s)
            except TimeoutError:
                self._notify(self._notifier.watchdog)
                self._snapshot()
                if self.status.state is not RecorderState.RECORDING:
                    baseline_token = None
                    baseline_epoch = None
                    continue
                progress_reader = getattr(self._runtime, "recording_progress_token", None)
                if not callable(progress_reader):
                    baseline_token = None
                    baseline_epoch = None
                    continue
                token = progress_reader()
                if token is None:
                    baseline_token = None
                    baseline_epoch = None
                    continue
                if isinstance(token, bool) or not isinstance(token, int) or token < 0:
                    raise RuntimeError(
                        "runtime returned an invalid recording progress token"
                    ) from None
                epoch = self._recording_progress_epoch
                if baseline_epoch != epoch or baseline_token is None:
                    baseline_token = token
                    baseline_epoch = epoch
                    continue
                if token <= baseline_token:
                    raise PipelineNoProgressFault(
                        f"encoded-frame progress did not advance beyond {baseline_token}"
                    ) from None
                baseline_token = token

    async def _wait_for_stop(self) -> None:
        await self._stop_requested.wait()

    async def _runtime_event_loop(self) -> None:
        while True:
            event = await self._runtime_events.get()
            try:
                if event.kind is RuntimeLifecycleEventKind.RECOVERING:
                    self._publish(
                        RecorderState.FAULTED,
                        reason=RecorderReason.PIPELINE_RECOVERING,
                        detail=event.detail,
                    )
                elif event.kind is RuntimeLifecycleEventKind.RESTARTING:
                    self._publish(RecorderState.STARTING)
                elif event.kind is RuntimeLifecycleEventKind.RECOVERED:
                    self._publish(RecorderState.RECORDING)
                elif event.kind is RuntimeLifecycleEventKind.EXHAUSTED:
                    self._publish(
                        RecorderState.FAULTED,
                        reason=RecorderReason.PIPELINE_RECOVERY_EXHAUSTED,
                        detail=event.detail,
                    )
                else:
                    raise RuntimeError("runtime lifecycle observer received an unknown event")
            finally:
                self._runtime_events.task_done()

    async def _cancel_task(self, task: asyncio.Task[object] | None) -> None:
        if task is None:
            return
        if task.done():
            if not task.cancelled():
                task.exception()
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    @staticmethod
    def _task_error(task: asyncio.Task[_TaskResult]) -> BaseException | None:
        if task.cancelled():
            return asyncio.CancelledError()
        return task.exception()

    @staticmethod
    def _consume_task_result(task: asyncio.Task[_TaskResult]) -> None:
        if not task.cancelled():
            task.exception()

    def _cancel_without_waiting(self, task: asyncio.Task[_TaskResult]) -> None:
        task.cancel()
        task.add_done_callback(self._consume_task_result)

    async def _stop_runtime(
        self,
        run_task: asyncio.Task[None] | None,
    ) -> tuple[RecorderReason | None, str | None]:
        stop_task = asyncio.create_task(self._runtime.stop(), name="recorder-runtime-stop")
        tasks: set[asyncio.Task[None]] = {stop_task}
        if run_task is not None:
            tasks.add(run_task)
        done, pending = await asyncio.wait(tasks, timeout=self._limits.shutdown_timeout_s)
        if pending:
            for task in pending:
                self._cancel_without_waiting(task)
            return RecorderReason.SHUTDOWN_TIMEOUT, "runtime did not stop within deadline"

        errors = [error for task in done if (error := self._task_error(task)) is not None]
        if errors:
            return RecorderReason.SHUTDOWN_FAILED, _exception_detail(errors[0])
        return None, None

    def _shutdown_notification(self) -> None:
        status = self.status
        self._notify(lambda: self._notifier.stopping(self._format_status(status)))

    async def _storage_fault(
        self,
        *,
        config: DashcamConfig,
        detail: str,
    ) -> DaemonResult:
        """Remain observable without opening the camera until orderly shutdown."""

        self._publish(
            RecorderState.FAULTED,
            reason=RecorderReason.STORAGE_FAULT,
            detail=detail,
            config_schema_version=config.schema_version,
        )
        self._notify(lambda: self._notifier.ready(self._format_status(self.status)))
        watchdog_interval = (
            self._limits.watchdog_interval_s
            if self._limits.watchdog_interval_s is not None
            else config.service.watchdog_s / 2
        )
        watchdog_task = asyncio.create_task(
            self._watchdog_loop(watchdog_interval),
            name="recorder-storage-fault-watchdog",
        )
        try:
            await self._stop_requested.wait()
        finally:
            await self._cancel_task(watchdog_task)
        self._publish(RecorderState.STOPPING)
        self._shutdown_notification()
        return DaemonResult(DaemonOutcome.STOPPED, self.status)

    async def _check_storage(self, config: DashcamConfig) -> DaemonResult | None:
        gate = self._storage_gate
        if gate is None:
            return None
        check_task = asyncio.create_task(
            gate.check(config),
            name="recorder-storage-preflight",
        )
        try:
            done, _ = await asyncio.wait(
                {check_task},
                timeout=self._limits.storage_timeout_s,
            )
        except asyncio.CancelledError:
            self._cancel_without_waiting(check_task)
            self.request_stop()
            raise
        if not done:
            self._cancel_without_waiting(check_task)
            return await self._storage_fault(
                config=config,
                detail="storage preflight exceeded deadline",
            )
        error = self._task_error(check_task)
        if error is not None:
            return await self._storage_fault(
                config=config,
                detail="storage preflight failed",
            )
        result = check_task.result()
        if not result.ready and not result.recoverable_reserve_exhaustion:
            reasons = ",".join(reason.value for reason in result.reasons) or "NOT_READY"
            return await self._storage_fault(
                config=config,
                detail=f"storage state={result.state.value} reasons={reasons}"[:512],
            )
        return None

    async def _external_shutdown(
        self,
        run_task: asyncio.Task[None],
    ) -> DaemonResult:
        self._publish(RecorderState.STOPPING)
        self._shutdown_notification()
        failure, detail = await self._stop_runtime(run_task)
        if failure is RecorderReason.SHUTDOWN_TIMEOUT:
            self._publish(RecorderState.FAULTED, reason=failure, detail=detail)
            return DaemonResult(DaemonOutcome.SHUTDOWN_TIMEOUT, self.status)
        if failure is RecorderReason.SHUTDOWN_FAILED:
            self._publish(RecorderState.FAULTED, reason=failure, detail=detail)
            return DaemonResult(DaemonOutcome.SHUTDOWN_FAILED, self.status)
        return DaemonResult(DaemonOutcome.STOPPED, self.status)

    async def _cleanup_after_failure(self, run_task: asyncio.Task[None] | None) -> None:
        self._stop_requested.set()
        self._shutdown_notification()
        await self._stop_runtime(run_task)

    async def run(self) -> DaemonResult:
        """Run once until requested shutdown or a bounded failure outcome."""

        if self._started:
            raise RuntimeError("RecorderDaemon instances are single-use")
        self._started = True
        self._notify(lambda: self._notifier.status(self._format_status(self.status)))
        self._snapshot()

        try:
            config = self._config_loader(self._config_path)
        except (ConfigError, OSError) as error:
            self._publish(
                RecorderState.FAULTED,
                reason=RecorderReason.CONFIG_ERROR,
                detail=_exception_detail(error),
            )
            return DaemonResult(DaemonOutcome.CONFIG_ERROR, self.status)

        self._publish(
            RecorderState.STARTING,
            config_schema_version=config.schema_version,
        )
        storage_result = await self._check_storage(config)
        if storage_result is not None:
            return storage_result
        if self._stop_requested.is_set():
            self._publish(RecorderState.STOPPING)
            self._shutdown_notification()
            return DaemonResult(DaemonOutcome.STOPPED, self.status)
        start_task = asyncio.create_task(
            self._runtime.start(config),
            name="recorder-runtime-start",
        )
        try:
            done, _ = await asyncio.wait(
                {start_task},
                timeout=self._limits.startup_timeout_s,
            )
        except asyncio.CancelledError:
            await self._cancel_task(start_task)
            self.request_stop()
            self._publish(RecorderState.STOPPING)
            self._shutdown_notification()
            await self._stop_runtime(None)
            raise
        if not done:
            await self._cancel_task(start_task)
            self._publish(
                RecorderState.FAULTED,
                reason=RecorderReason.STARTUP_TIMEOUT,
                detail="runtime start exceeded deadline",
            )
            await self._cleanup_after_failure(None)
            return DaemonResult(DaemonOutcome.STARTUP_TIMEOUT, self.status)
        startup_error = self._task_error(start_task)
        if startup_error is not None:
            if isinstance(startup_error, StorageSafetyStop):
                reason = RecorderReason.STORAGE_FAULT
                outcome = DaemonOutcome.STORAGE_SAFETY_STOP
            elif isinstance(startup_error, RecorderStorageFault):
                reason = RecorderReason.STORAGE_FAULT
                outcome = DaemonOutcome.STARTUP_FAILED
            elif isinstance(startup_error, RecorderFinalizationFault):
                reason = RecorderReason.FINALIZATION_FAILED
                outcome = DaemonOutcome.STARTUP_FAILED
            else:
                reason = RecorderReason.STARTUP_FAILED
                outcome = DaemonOutcome.STARTUP_FAILED
            self._publish(
                RecorderState.FAULTED,
                reason=reason,
                detail=_exception_detail(startup_error),
            )
            await self._cleanup_after_failure(None)
            return DaemonResult(outcome, self.status)

        if self._stop_requested.is_set():
            run_task = asyncio.create_task(
                self._runtime.run(self._stop_requested),
                name="recorder-runtime",
            )
            return await self._external_shutdown(run_task)

        self._publish(RecorderState.RECORDING)
        self._notify(lambda: self._notifier.ready(self._format_status(self.status)))

        watchdog_interval = (
            self._limits.watchdog_interval_s
            if self._limits.watchdog_interval_s is not None
            else config.service.watchdog_s / 2
        )
        run_task = asyncio.create_task(
            self._runtime.run(self._stop_requested),
            name="recorder-runtime",
        )
        stop_wait_task = asyncio.create_task(
            self._wait_for_stop(),
            name="recorder-stop-wait",
        )
        watchdog_task = asyncio.create_task(
            self._watchdog_loop(watchdog_interval),
            name="recorder-watchdog",
        )
        runtime_event_task = asyncio.create_task(
            self._runtime_event_loop(),
            name="recorder-runtime-events",
        )

        try:
            done, _ = await asyncio.wait(
                {run_task, stop_wait_task, watchdog_task, runtime_event_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if self._stop_requested.is_set():
                await self._cancel_task(watchdog_task)
                return await self._external_shutdown(run_task)
            if watchdog_task in done:
                watchdog_error = self._task_error(watchdog_task)
                if isinstance(watchdog_error, PipelineNoProgressFault):
                    reason = RecorderReason.PIPELINE_NO_PROGRESS
                    outcome = DaemonOutcome.PIPELINE_NO_PROGRESS
                    detail = _exception_detail(watchdog_error)
                else:
                    reason = RecorderReason.RUNTIME_FAILED
                    outcome = DaemonOutcome.RUNTIME_FAILED
                    detail = (
                        "watchdog task exited unexpectedly"
                        if watchdog_error is None
                        else _exception_detail(watchdog_error)
                    )
                self._publish(RecorderState.FAULTED, reason=reason, detail=detail)
                await self._cleanup_after_failure(run_task)
                return DaemonResult(outcome, self.status)
            await self._cancel_task(watchdog_task)
            if runtime_event_task in done:
                event_error = self._task_error(runtime_event_task)
                detail = (
                    "runtime lifecycle event loop exited unexpectedly"
                    if event_error is None
                    else _exception_detail(event_error)
                )
                self._publish(
                    RecorderState.FAULTED,
                    reason=RecorderReason.RUNTIME_FAILED,
                    detail=detail,
                )
                await self._cleanup_after_failure(run_task)
                return DaemonResult(DaemonOutcome.RUNTIME_FAILED, self.status)

            runtime_error = self._task_error(run_task)
            if runtime_error is None:
                reason = RecorderReason.RUNTIME_EXITED
                outcome = DaemonOutcome.RUNTIME_EXITED
                detail = "runtime exited without a stop request"
            else:
                if isinstance(runtime_error, StorageSafetyStop):
                    reason = RecorderReason.STORAGE_FAULT
                    outcome = DaemonOutcome.STORAGE_SAFETY_STOP
                elif isinstance(runtime_error, RecorderStorageFault):
                    reason = RecorderReason.STORAGE_FAULT
                    outcome = DaemonOutcome.RUNTIME_FAILED
                elif isinstance(runtime_error, PipelineRecoveryExhausted):
                    reason = RecorderReason.PIPELINE_RECOVERY_EXHAUSTED
                    outcome = DaemonOutcome.RUNTIME_FAILED
                elif isinstance(runtime_error, RecorderFinalizationFault):
                    reason = RecorderReason.FINALIZATION_FAILED
                    outcome = DaemonOutcome.RUNTIME_FAILED
                else:
                    reason = RecorderReason.RUNTIME_FAILED
                    outcome = DaemonOutcome.RUNTIME_FAILED
                detail = _exception_detail(runtime_error)
            self._publish(RecorderState.FAULTED, reason=reason, detail=detail)
            await self._cleanup_after_failure(None)
            return DaemonResult(outcome, self.status)
        except asyncio.CancelledError:
            self.request_stop()
            await self._cancel_task(watchdog_task)
            await self._external_shutdown(run_task)
            raise
        finally:
            await self._cancel_task(stop_wait_task)
            await self._cancel_task(watchdog_task)
            await self._cancel_task(runtime_event_task)
