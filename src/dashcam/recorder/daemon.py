"""Hardware-independent recorder daemon lifecycle orchestration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from dashcam.config import ConfigError, DashcamConfig, load_config
from dashcam.recorder.notifier import NullNotifier, ServiceNotifier
from dashcam.recorder.status import RecorderReason, RecorderStatus, RecorderStatusStore
from dashcam.state import RecorderState


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


@dataclass(frozen=True, slots=True)
class DaemonLimits:
    """Explicit bounds corresponding to service-manager startup/shutdown limits."""

    startup_timeout_s: float = 40.0
    shutdown_timeout_s: float = 25.0
    watchdog_interval_s: float | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.startup_timeout_s, bool)
            or not isinstance(self.startup_timeout_s, int | float)
            or not 0 < self.startup_timeout_s <= 300
        ):
            raise ValueError("startup_timeout_s must be between 0 and 300")
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
    CONFIG_ERROR = "CONFIG_ERROR"
    STARTUP_FAILED = "STARTUP_FAILED"
    STARTUP_TIMEOUT = "STARTUP_TIMEOUT"
    RUNTIME_EXITED = "RUNTIME_EXITED"
    RUNTIME_FAILED = "RUNTIME_FAILED"
    SHUTDOWN_FAILED = "SHUTDOWN_FAILED"
    SHUTDOWN_TIMEOUT = "SHUTDOWN_TIMEOUT"


@dataclass(frozen=True, slots=True)
class DaemonResult:
    outcome: DaemonOutcome
    final_status: RecorderStatus

    @property
    def clean(self) -> bool:
        return self.outcome is DaemonOutcome.STOPPED


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
        notifier: ServiceNotifier | None = None,
        status_store: RecorderStatusStore | None = None,
        limits: DaemonLimits | None = None,
    ) -> None:
        self._config_path = Path(config_path)
        self._runtime = runtime
        self._config_loader = config_loader
        self._notifier = notifier or NullNotifier()
        self._status_store = status_store or RecorderStatusStore()
        self._limits = limits or DaemonLimits()
        self._stop_requested = asyncio.Event()
        self._started = False

    @property
    def status(self) -> RecorderStatus:
        return self._status_store.snapshot()

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested.is_set()

    def request_stop(self) -> None:
        """Request idempotent cooperative shutdown."""

        self._stop_requested.set()

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

    def _publish(
        self,
        state: RecorderState,
        *,
        reason: RecorderReason | None = None,
        detail: str | None = None,
        config_schema_version: int | None = None,
    ) -> RecorderStatus:
        status = self._status_store.transition(
            state,
            reason=reason,
            detail=detail,
            config_schema_version=config_schema_version,
        )
        self._notify(lambda: self._notifier.status(self._format_status(status)))
        return self.status

    async def _watchdog_loop(self, interval_s: float) -> None:
        while not self._stop_requested.is_set():
            try:
                await asyncio.wait_for(self._stop_requested.wait(), timeout=interval_s)
            except TimeoutError:
                self._notify(self._notifier.watchdog)

    async def _cancel_task(self, task: asyncio.Task[object] | None) -> None:
        if task is None or task.done():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    @staticmethod
    def _task_error(task: asyncio.Task[None]) -> BaseException | None:
        if task.cancelled():
            return asyncio.CancelledError()
        return task.exception()

    @staticmethod
    def _consume_task_result(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()

    def _cancel_without_waiting(self, task: asyncio.Task[None]) -> None:
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
            self._cancel_without_waiting(start_task)
            self.request_stop()
            self._publish(RecorderState.STOPPING)
            self._shutdown_notification()
            await self._stop_runtime(None)
            raise
        if not done:
            self._cancel_without_waiting(start_task)
            self._publish(
                RecorderState.FAULTED,
                reason=RecorderReason.STARTUP_TIMEOUT,
                detail="runtime start exceeded deadline",
            )
            await self._cleanup_after_failure(None)
            return DaemonResult(DaemonOutcome.STARTUP_TIMEOUT, self.status)
        startup_error = self._task_error(start_task)
        if startup_error is not None:
            self._publish(
                RecorderState.FAULTED,
                reason=RecorderReason.STARTUP_FAILED,
                detail=_exception_detail(startup_error),
            )
            await self._cleanup_after_failure(None)
            return DaemonResult(DaemonOutcome.STARTUP_FAILED, self.status)

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
            self._stop_requested.wait(),
            name="recorder-stop-wait",
        )
        watchdog_task = asyncio.create_task(
            self._watchdog_loop(watchdog_interval),
            name="recorder-watchdog",
        )

        try:
            await asyncio.wait(
                {run_task, stop_wait_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            await self._cancel_task(watchdog_task)
            if self._stop_requested.is_set():
                return await self._external_shutdown(run_task)

            runtime_error = self._task_error(run_task)
            if runtime_error is None:
                reason = RecorderReason.RUNTIME_EXITED
                outcome = DaemonOutcome.RUNTIME_EXITED
                detail = "runtime exited without a stop request"
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
