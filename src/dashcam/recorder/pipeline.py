"""Hardware-independent contracts for a sole-owner continuous media pipeline.

This module deliberately contains no device discovery, framework imports, or
encoder/plugin names.  A target adapter must prove its effective profile and
implement :class:`PipelineBackend` after the Phase 0B capability gate.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Protocol


class PipelineContractError(RuntimeError):
    """Base error for a violated media-runtime contract."""


class ProfileValidationError(PipelineContractError, ValueError):
    """Raised when a requested or effective profile violates its declaration."""


class CameraOwnershipError(PipelineContractError):
    """Raised when a second owner attempts to claim the camera boundary."""


class RecoverablePipelineError(PipelineContractError):
    """A backend failure for which the bounded restart policy may be used."""


class PipelineFault(PipelineContractError):
    """A terminal critical-pipeline fault."""


@dataclass(frozen=True, slots=True)
class VideoProfile:
    """Requested or effective video profile.

    Production profiles are intentionally exact.  An adapter must return its
    effective profile from ``start``; the runtime compares every field and
    faults instead of accepting a downgrade.
    """

    width: int = 1920
    height: int = 1080
    frames_per_second: int = 30
    codec: str = "h264"
    hardware_encoded: bool = True
    production: bool = True

    def __post_init__(self) -> None:
        for name, value, maximum in (
            ("width", self.width, 16384),
            ("height", self.height, 16384),
            ("frames_per_second", self.frames_per_second, 240),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ProfileValidationError(f"{name} must be an integer between 1 and {maximum}")
        if not isinstance(self.codec, str) or not self.codec or len(self.codec) > 32:
            raise ProfileValidationError(
                "codec must be a non-empty string of at most 32 characters"
            )
        if not isinstance(self.hardware_encoded, bool) or not isinstance(self.production, bool):
            raise ProfileValidationError("profile flags must be booleans")
        if self.production and (
            self.width != 1920
            or self.height != 1080
            or self.frames_per_second != 30
            or self.codec.casefold() != "h264"
            or not self.hardware_encoded
        ):
            raise ProfileValidationError(
                "production requires exactly 1920x1080 at 30 fps with hardware H.264"
            )


class PipelineBackend(Protocol):
    """Injected target adapter which exclusively opens the camera.

    ``run`` must keep the camera and encoder continuous across normal output
    boundaries.  Segment rotation belongs to the separate segment-output
    contract and must not cause this lifecycle to restart.
    """

    async def start(self, requested_profile: VideoProfile) -> VideoProfile:
        """Open the camera/encoder and return the actual effective profile."""

    async def run(self, stop_requested: asyncio.Event) -> None:
        """Run until stopped, or raise ``RecoverablePipelineError`` on failure."""

    async def stop(self) -> None:
        """Release the camera and encoder idempotently."""


class PipelineBackendFactory(Protocol):
    def __call__(self) -> PipelineBackend:
        """Build one backend for one bounded start attempt."""


class OptionalBranch(Protocol):
    """An isolated noncritical branch such as audio or preview."""

    @property
    def name(self) -> str:
        """Return a stable bounded diagnostic name."""

    async def run(self, stop_requested: asyncio.Event) -> None:
        """Run without owning or reopening the camera."""


class BackoffWaiter(Protocol):
    async def __call__(self, delay_s: float, stop_requested: asyncio.Event) -> bool:
        """Wait for a delay; return true if cancellation was requested."""


@dataclass(frozen=True, slots=True)
class RestartPolicy:
    """Finite exponential restart/backoff and cleanup bounds."""

    max_restarts: int = 3
    initial_backoff_s: float = 1.0
    maximum_backoff_s: float = 60.0
    multiplier: float = 2.0
    stop_timeout_s: float = 20.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_restarts, bool)
            or not isinstance(self.max_restarts, int)
            or not 0 <= self.max_restarts <= 100
        ):
            raise ValueError("max_restarts must be an integer between 0 and 100")
        for name, value, maximum in (
            ("initial_backoff_s", self.initial_backoff_s, 300.0),
            ("maximum_backoff_s", self.maximum_backoff_s, 300.0),
            ("stop_timeout_s", self.stop_timeout_s, 300.0),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not 0 < value <= maximum
            ):
                raise ValueError(f"{name} must be greater than zero and at most {maximum}")
        if self.initial_backoff_s > self.maximum_backoff_s:
            raise ValueError("initial_backoff_s cannot exceed maximum_backoff_s")
        if (
            isinstance(self.multiplier, bool)
            or not isinstance(self.multiplier, int | float)
            or not 1 <= self.multiplier <= 16
        ):
            raise ValueError("multiplier must be between 1 and 16")

    def delay_for(self, restart_number: int) -> float:
        """Return a capped delay for a one-based restart number."""

        if (
            isinstance(restart_number, bool)
            or not isinstance(restart_number, int)
            or not 1 <= restart_number <= self.max_restarts
        ):
            raise ValueError("restart_number is outside the configured restart bound")
        return min(
            self.maximum_backoff_s,
            self.initial_backoff_s * self.multiplier ** (restart_number - 1),
        )


@dataclass(frozen=True, slots=True)
class OptionalBranchFailure:
    name: str
    detail: str


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """Clean pipeline exit, including isolated optional degradation."""

    restart_count: int
    optional_failures: tuple[OptionalBranchFailure, ...]

    @property
    def degraded(self) -> bool:
        return bool(self.optional_failures)


class MonotonicMediaClock:
    """Create media timestamps solely from an injected monotonic source."""

    def __init__(self, source_ns: Callable[[], int] = time.monotonic_ns) -> None:
        self._source_ns = source_ns
        self._last_ns: int | None = None

    def now_ns(self) -> int:
        value = self._source_ns()
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise PipelineContractError("monotonic source returned an invalid timestamp")
        if self._last_ns is not None and value < self._last_ns:
            raise PipelineContractError("monotonic media clock regressed")
        self._last_ns = value
        return value


class CameraOwnership:
    """Process-local fail-closed ownership guard for the camera boundary."""

    def __init__(self) -> None:
        self._owner: str | None = None

    @property
    def owner(self) -> str | None:
        return self._owner

    def claim(self, owner: str) -> None:
        if not isinstance(owner, str) or not owner or len(owner) > 128 or not owner.isprintable():
            raise CameraOwnershipError("owner must be 1 to 128 printable characters")
        if self._owner is not None:
            raise CameraOwnershipError(f"camera is already owned by {self._owner}")
        self._owner = owner

    def release(self, owner: str) -> None:
        if self._owner != owner:
            raise CameraOwnershipError("camera ownership can only be released by its owner")
        self._owner = None


_PROCESS_CAMERA_OWNERSHIP: Final = CameraOwnership()


async def _wait_for_backoff(delay_s: float, stop_requested: asyncio.Event) -> bool:
    try:
        await asyncio.wait_for(stop_requested.wait(), timeout=delay_s)
    except TimeoutError:
        return False
    return True


def _bounded_detail(error: BaseException) -> str:
    detail = " ".join(f"{type(error).__name__}: {error}".replace("\0", " ").splitlines())
    return detail[:512] or type(error).__name__


class ContinuousPipeline:
    """Supervise one continuous critical pipeline with finite recovery.

    This class is single-use.  Optional branches run as isolated tasks: their
    exit or exception is recorded as degradation and never awaited by the
    critical video loop.
    """

    def __init__(
        self,
        *,
        owner: str,
        backend_factory: PipelineBackendFactory,
        profile: VideoProfile | None = None,
        restart_policy: RestartPolicy | None = None,
        optional_branches: tuple[OptionalBranch, ...] = (),
        ownership: CameraOwnership | None = None,
        backoff_waiter: BackoffWaiter = _wait_for_backoff,
    ) -> None:
        if not isinstance(optional_branches, tuple) or len(optional_branches) > 16:
            raise ValueError("optional_branches must be a tuple of at most 16 branches")
        names = tuple(branch.name for branch in optional_branches)
        if any(not name or len(name) > 64 or not name.isprintable() for name in names):
            raise ValueError("optional branch names must be 1 to 64 printable characters")
        if len(set(names)) != len(names):
            raise ValueError("optional branch names must be unique")
        self._owner = owner
        self._backend_factory = backend_factory
        self._profile = profile or VideoProfile()
        self._restart_policy = restart_policy or RestartPolicy()
        self._optional_branches = optional_branches
        self._ownership = ownership or _PROCESS_CAMERA_OWNERSHIP
        self._backoff_waiter = backoff_waiter
        self._used = False
        self._optional_failures: list[OptionalBranchFailure] = []

    async def _run_optional(self, branch: OptionalBranch, stop_requested: asyncio.Event) -> None:
        try:
            await branch.run(stop_requested)
            if not stop_requested.is_set():
                raise RuntimeError("optional branch exited unexpectedly")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._optional_failures.append(
                OptionalBranchFailure(branch.name, _bounded_detail(error))
            )

    async def _stop_backend(self, backend: PipelineBackend) -> BaseException | None:
        try:
            await asyncio.wait_for(backend.stop(), timeout=self._restart_policy.stop_timeout_s)
        except (Exception, asyncio.CancelledError) as error:
            return error
        return None

    @staticmethod
    def _consume_optional_result(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()

    async def run(self, stop_requested: asyncio.Event) -> PipelineResult:
        """Run until cooperative cancellation or a terminal bounded fault."""

        if self._used:
            raise RuntimeError("ContinuousPipeline is single-use")
        self._used = True
        self._ownership.claim(self._owner)
        optional_tasks: tuple[asyncio.Task[None], ...] = ()
        restart_count = 0
        try:
            optional_tasks = tuple(
                asyncio.create_task(
                    self._run_optional(branch, stop_requested),
                    name=f"optional-media-{branch.name}",
                )
                for branch in self._optional_branches
            )
            while not stop_requested.is_set():
                backend = self._backend_factory()
                recoverable_error: RecoverablePipelineError | None = None
                try:
                    effective = await backend.start(self._profile)
                    if effective != self._profile:
                        raise ProfileValidationError(
                            "backend effective profile differs from the requested profile"
                        )
                    await backend.run(stop_requested)
                    if not stop_requested.is_set():
                        recoverable_error = RecoverablePipelineError(
                            "critical pipeline exited unexpectedly"
                        )
                except RecoverablePipelineError as error:
                    recoverable_error = error
                finally:
                    cleanup_error = await self._stop_backend(backend)

                if cleanup_error is not None:
                    raise PipelineFault(
                        f"pipeline cleanup failed: {_bounded_detail(cleanup_error)}"
                    ) from cleanup_error
                if stop_requested.is_set():
                    break
                if recoverable_error is None:
                    raise PipelineFault("pipeline stopped without a declared outcome")
                if restart_count >= self._restart_policy.max_restarts:
                    raise PipelineFault(
                        "critical pipeline exhausted its bounded restart policy"
                    ) from recoverable_error
                restart_count += 1
                cancelled = await self._backoff_waiter(
                    self._restart_policy.delay_for(restart_count), stop_requested
                )
                if cancelled or stop_requested.is_set():
                    break
        finally:
            for task in optional_tasks:
                if not task.done():
                    task.cancel()
            if optional_tasks:
                done, pending = await asyncio.wait(
                    optional_tasks,
                    timeout=self._restart_policy.stop_timeout_s,
                )
                for task in done:
                    self._consume_optional_result(task)
                for index, task in enumerate(optional_tasks):
                    if task in pending:
                        self._optional_failures.append(
                            OptionalBranchFailure(
                                self._optional_branches[index].name,
                                "optional branch exceeded shutdown deadline",
                            )
                        )
                        task.cancel()
                        task.add_done_callback(self._consume_optional_result)
            self._ownership.release(self._owner)

        return PipelineResult(restart_count, tuple(self._optional_failures))
