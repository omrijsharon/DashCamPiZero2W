"""Pure segmentation and bounded asynchronous finalization contracts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class SegmentContractError(RuntimeError):
    """Base error for invalid segmentation behavior."""


@dataclass(frozen=True, slots=True)
class AccessUnit:
    """Minimal target-independent evidence about one encoded access unit."""

    timestamp_ns: int
    is_idr: bool
    closed_gop: bool
    has_decoder_config: bool

    def __post_init__(self) -> None:
        if (
            isinstance(self.timestamp_ns, bool)
            or not isinstance(self.timestamp_ns, int)
            or self.timestamp_ns < 0
        ):
            raise ValueError("timestamp_ns must be a non-negative monotonic timestamp")
        if not all(
            isinstance(value, bool)
            for value in (self.is_idr, self.closed_gop, self.has_decoder_config)
        ):
            raise ValueError("access-unit evidence flags must be booleans")

    @property
    def independently_decodable(self) -> bool:
        return self.is_idr and self.closed_gop and self.has_decoder_config


class ClipPosition(StrEnum):
    FIRST = "FIRST"
    MIDDLE = "MIDDLE"
    FINAL = "FINAL"


class BoundaryDecision(StrEnum):
    WAIT = "WAIT"
    REQUEST_IDR = "REQUEST_IDR"
    SPLIT = "SPLIT"
    FAULT = "FAULT"


@dataclass(frozen=True, slots=True)
class SegmentBoundaryPolicy:
    """IDR-aware 60-second policy with explicit normal-clip bounds."""

    target_duration_ns: int = 60_000_000_000
    minimum_normal_duration_ns: int = 59_000_000_000
    maximum_normal_duration_ns: int = 61_000_000_000
    request_lead_ns: int = 1_000_000_000

    def __post_init__(self) -> None:
        values = (
            self.target_duration_ns,
            self.minimum_normal_duration_ns,
            self.maximum_normal_duration_ns,
            self.request_lead_ns,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
            raise ValueError("segment duration bounds must be integer nanoseconds")
        if not (
            0
            < self.minimum_normal_duration_ns
            <= self.target_duration_ns
            <= self.maximum_normal_duration_ns
        ):
            raise ValueError("normal segment bounds must contain the target duration")
        if not 0 <= self.request_lead_ns <= self.target_duration_ns:
            raise ValueError("request_lead_ns must be between zero and the target duration")

    def duration_allowed(self, duration_ns: int, position: ClipPosition) -> bool:
        """Validate duration, explicitly allowing short first/final clips."""

        if isinstance(duration_ns, bool) or not isinstance(duration_ns, int) or duration_ns < 0:
            raise ValueError("duration_ns must be a non-negative integer")
        if position is ClipPosition.MIDDLE:
            return (
                self.minimum_normal_duration_ns <= duration_ns <= (self.maximum_normal_duration_ns)
            )
        return duration_ns <= self.maximum_normal_duration_ns

    def decide(self, *, segment_start_ns: int, access_unit: AccessUnit) -> BoundaryDecision:
        if (
            isinstance(segment_start_ns, bool)
            or not isinstance(segment_start_ns, int)
            or segment_start_ns < 0
        ):
            raise ValueError("segment_start_ns must be a non-negative integer")
        if access_unit.timestamp_ns < segment_start_ns:
            raise SegmentContractError("access-unit timestamp regressed before segment start")
        duration_ns = access_unit.timestamp_ns - segment_start_ns
        if duration_ns < self.minimum_normal_duration_ns:
            return BoundaryDecision.WAIT
        if duration_ns > self.maximum_normal_duration_ns:
            return BoundaryDecision.FAULT
        if access_unit.independently_decodable:
            return BoundaryDecision.SPLIT
        if duration_ns >= self.maximum_normal_duration_ns:
            return BoundaryDecision.FAULT
        if duration_ns >= self.target_duration_ns - self.request_lead_ns:
            return BoundaryDecision.REQUEST_IDR
        return BoundaryDecision.WAIT


@dataclass(frozen=True, slots=True)
class SegmentArtifact:
    """Opaque closed-segment handle passed from rotation to finalization."""

    clip_id: str
    start_timestamp_ns: int
    end_timestamp_ns: int
    token: str

    def __post_init__(self) -> None:
        if not self.clip_id or len(self.clip_id) > 128 or not self.clip_id.isprintable():
            raise ValueError("clip_id must be 1 to 128 printable characters")
        if not self.token or len(self.token) > 512 or not self.token.isprintable():
            raise ValueError("token must be 1 to 512 printable characters")
        if (
            isinstance(self.start_timestamp_ns, bool)
            or isinstance(self.end_timestamp_ns, bool)
            or not isinstance(self.start_timestamp_ns, int)
            or not isinstance(self.end_timestamp_ns, int)
            or self.start_timestamp_ns < 0
            or self.end_timestamp_ns <= self.start_timestamp_ns
        ):
            raise ValueError("artifact end timestamp must be after its non-negative start")


class SegmentOutputBackend(Protocol):
    """Rotate only the muxed output of an already-running encoder session."""

    async def request_idr(self) -> None:
        """Request a keyframe without restarting the camera or encoder."""

    async def rotate(self, boundary: AccessUnit) -> SegmentArtifact:
        """Close the prior output immediately before the supplied IDR."""

    async def close_active(self, final_timestamp_ns: int) -> SegmentArtifact:
        """Close the final, possibly short, active clip without a camera restart."""


class SegmentFinalizer(Protocol):
    async def finalize(self, artifact: SegmentArtifact) -> None:
        """Durably finalize one already-closed segment."""


class FinalizationState(StrEnum):
    NEW = "NEW"
    RUNNING = "RUNNING"
    DEGRADED = "DEGRADED"
    CLOSED = "CLOSED"


@dataclass(frozen=True, slots=True)
class FinalizationCloseResult:
    drained: bool
    outstanding: int
    failed_count: int


class FinalizationQueue:
    """Bounded, nonblocking producer queue with one isolated finalizer worker.

    Capacity includes both queued and currently-finalizing artifacts.  A
    reservation is taken before awaiting output rotation, preventing concurrent
    producers from exceeding the bound while a backend closes a segment.
    """

    def __init__(self, finalizer: SegmentFinalizer, *, capacity: int = 2) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or not 1 <= capacity <= 64:
            raise ValueError("capacity must be an integer between 1 and 64")
        self._finalizer = finalizer
        self._capacity = capacity
        self._queue: asyncio.Queue[SegmentArtifact] = asyncio.Queue(maxsize=capacity)
        self._worker: asyncio.Task[None] | None = None
        self._outstanding = 0
        self._failed_count = 0
        self._last_failure: str | None = None
        self._accepting = False
        self._state = FinalizationState.NEW

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def outstanding(self) -> int:
        return self._outstanding

    @property
    def failed_count(self) -> int:
        return self._failed_count

    @property
    def last_failure(self) -> str | None:
        return self._last_failure

    @property
    def state(self) -> FinalizationState:
        return self._state

    def start(self) -> None:
        if self._state is not FinalizationState.NEW:
            raise RuntimeError("finalization queue is single-use")
        self._accepting = True
        self._state = FinalizationState.RUNNING
        self._worker = asyncio.create_task(self._run(), name="segment-finalizer")

    def reserve(self) -> bool:
        """Reserve one bounded slot without blocking the recording path."""

        if not self._accepting or self._outstanding >= self._capacity:
            return False
        self._outstanding += 1
        return True

    def cancel_reservation(self) -> None:
        if self._outstanding <= 0:
            raise SegmentContractError("no finalization reservation exists")
        self._outstanding -= 1

    def submit_reserved(self, artifact: SegmentArtifact) -> None:
        if not self._accepting:
            self.cancel_reservation()
            raise SegmentContractError("finalization queue is not accepting work")
        try:
            self._queue.put_nowait(artifact)
        except asyncio.QueueFull as error:
            self.cancel_reservation()
            raise SegmentContractError("reserved finalization queue unexpectedly filled") from error

    def try_submit(self, artifact: SegmentArtifact) -> bool:
        if not self.reserve():
            return False
        self.submit_reserved(artifact)
        return True

    async def _run(self) -> None:
        while True:
            artifact = await self._queue.get()
            try:
                await self._finalizer.finalize(artifact)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._failed_count += 1
                detail = " ".join(
                    f"{type(error).__name__}: {error}".replace("\0", " ").splitlines()
                )
                self._last_failure = detail[:512] or type(error).__name__
                self._state = FinalizationState.DEGRADED
            finally:
                self._outstanding -= 1
                self._queue.task_done()

    async def close(self, *, drain_timeout_s: float) -> FinalizationCloseResult:
        """Bound draining and cancel any worker still active at the deadline."""

        if (
            isinstance(drain_timeout_s, bool)
            or not isinstance(drain_timeout_s, int | float)
            or not 0 < drain_timeout_s <= 300
        ):
            raise ValueError("drain_timeout_s must be greater than zero and at most 300")
        if self._state is FinalizationState.NEW:
            raise RuntimeError("finalization queue was not started")
        if self._state is FinalizationState.CLOSED:
            raise RuntimeError("finalization queue is already closed")
        self._accepting = False
        drained = True
        try:
            await asyncio.wait_for(self._queue.join(), timeout=drain_timeout_s)
        except TimeoutError:
            drained = False
        worker = self._worker
        if worker is not None and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        self._state = FinalizationState.CLOSED
        return FinalizationCloseResult(drained, self._outstanding, self._failed_count)


class SegmentAction(StrEnum):
    NONE = "NONE"
    IDR_REQUESTED = "IDR_REQUESTED"
    ROTATED = "ROTATED"
    FINALIZED_ACTIVE = "FINALIZED_ACTIVE"
    FINALIZER_OVERLOAD = "FINALIZER_OVERLOAD"
    BOUNDARY_FAULT = "BOUNDARY_FAULT"
    OUTPUT_FAULT = "OUTPUT_FAULT"


@dataclass(frozen=True, slots=True)
class SegmentOutcome:
    action: SegmentAction
    artifact: SegmentArtifact | None = None
    detail: str | None = None


class ContinuousSegmenter:
    """Rotate outputs while leaving camera/encoder ownership untouched."""

    def __init__(
        self,
        *,
        segment_start_ns: int,
        output: SegmentOutputBackend,
        finalizations: FinalizationQueue,
        policy: SegmentBoundaryPolicy | None = None,
    ) -> None:
        if (
            isinstance(segment_start_ns, bool)
            or not isinstance(segment_start_ns, int)
            or segment_start_ns < 0
        ):
            raise ValueError("segment_start_ns must be a non-negative integer")
        self._segment_start_ns = segment_start_ns
        self._output = output
        self._finalizations = finalizations
        self._policy = policy or SegmentBoundaryPolicy()
        self._idr_requested = False
        self._faulted = False
        self._closed = False

    @property
    def segment_start_ns(self) -> int:
        return self._segment_start_ns

    @property
    def faulted(self) -> bool:
        return self._faulted

    async def observe(self, access_unit: AccessUnit) -> SegmentOutcome:
        if self._closed:
            raise SegmentContractError("segmenter is closed")
        if self._faulted:
            raise SegmentContractError("segmenter is faulted")
        decision = self._policy.decide(
            segment_start_ns=self._segment_start_ns, access_unit=access_unit
        )
        if decision is BoundaryDecision.WAIT:
            return SegmentOutcome(SegmentAction.NONE)
        if decision is BoundaryDecision.REQUEST_IDR:
            if self._idr_requested:
                return SegmentOutcome(SegmentAction.NONE)
            try:
                await self._output.request_idr()
            except Exception as error:
                self._faulted = True
                return SegmentOutcome(SegmentAction.OUTPUT_FAULT, detail=str(error)[:512])
            self._idr_requested = True
            return SegmentOutcome(SegmentAction.IDR_REQUESTED)
        if decision is BoundaryDecision.FAULT:
            self._faulted = True
            return SegmentOutcome(
                SegmentAction.BOUNDARY_FAULT,
                detail="no closed-GOP IDR with decoder configuration arrived within the bound",
            )
        if not self._finalizations.reserve():
            self._faulted = True
            return SegmentOutcome(
                SegmentAction.FINALIZER_OVERLOAD,
                detail="bounded finalization capacity is exhausted",
            )
        try:
            artifact = await self._output.rotate(access_unit)
        except Exception as error:
            self._finalizations.cancel_reservation()
            self._faulted = True
            return SegmentOutcome(SegmentAction.OUTPUT_FAULT, detail=str(error)[:512])
        self._finalizations.submit_reserved(artifact)
        self._segment_start_ns = access_unit.timestamp_ns
        self._idr_requested = False
        return SegmentOutcome(SegmentAction.ROTATED, artifact=artifact)

    async def close(self, final_timestamp_ns: int) -> SegmentOutcome:
        """Close and enqueue the final clip, which may be shorter than normal."""

        if self._closed:
            raise SegmentContractError("segmenter is already closed")
        if self._faulted:
            raise SegmentContractError("segmenter is faulted")
        if (
            isinstance(final_timestamp_ns, bool)
            or not isinstance(final_timestamp_ns, int)
            or final_timestamp_ns < self._segment_start_ns
        ):
            raise ValueError("final_timestamp_ns must not precede the active segment")
        duration_ns = final_timestamp_ns - self._segment_start_ns
        if not self._policy.duration_allowed(duration_ns, ClipPosition.FINAL):
            self._faulted = True
            return SegmentOutcome(
                SegmentAction.BOUNDARY_FAULT,
                detail="final clip exceeds the maximum segment-duration bound",
            )
        if not self._finalizations.reserve():
            self._faulted = True
            return SegmentOutcome(
                SegmentAction.FINALIZER_OVERLOAD,
                detail="bounded finalization capacity is exhausted",
            )
        try:
            artifact = await self._output.close_active(final_timestamp_ns)
        except Exception as error:
            self._finalizations.cancel_reservation()
            self._faulted = True
            return SegmentOutcome(SegmentAction.OUTPUT_FAULT, detail=str(error)[:512])
        self._finalizations.submit_reserved(artifact)
        self._closed = True
        return SegmentOutcome(SegmentAction.FINALIZED_ACTIVE, artifact=artifact)
