from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from dashcam.recorder.segmenter import (
    AccessUnit,
    BoundaryDecision,
    ClipPosition,
    ContinuousSegmenter,
    FinalizationQueue,
    FinalizationState,
    SegmentAction,
    SegmentArtifact,
    SegmentBoundaryPolicy,
)

SECOND = 1_000_000_000


def run_async(coroutine: object) -> object:
    return asyncio.run(coroutine)  # type: ignore[arg-type]


def idr(second: int) -> AccessUnit:
    return AccessUnit(second * SECOND, is_idr=True, closed_gop=True, has_decoder_config=True)


def predicted(second: int) -> AccessUnit:
    return AccessUnit(second * SECOND, is_idr=False, closed_gop=False, has_decoder_config=False)


@dataclass
class FakeOutput:
    camera_starts: int = 1
    idr_requests: int = 0
    rotations: list[int] = field(default_factory=list)
    closes: list[int] = field(default_factory=list)
    sequence: int = 0

    async def request_idr(self) -> None:
        self.idr_requests += 1

    async def rotate(self, boundary: AccessUnit) -> SegmentArtifact:
        start = self.rotations[-1] if self.rotations else 0
        self.rotations.append(boundary.timestamp_ns)
        self.sequence += 1
        return SegmentArtifact(
            f"clip-{self.sequence}",
            start,
            boundary.timestamp_ns,
            f"opaque-{self.sequence}",
        )

    async def close_active(self, final_timestamp_ns: int) -> SegmentArtifact:
        start = self.rotations[-1] if self.rotations else 0
        self.closes.append(final_timestamp_ns)
        self.sequence += 1
        return SegmentArtifact(
            f"clip-{self.sequence}",
            start,
            final_timestamp_ns,
            f"opaque-{self.sequence}",
        )


@dataclass
class RecordingFinalizer:
    artifacts: list[SegmentArtifact] = field(default_factory=list)
    gate: asyncio.Event | None = None
    fail_clip: str | None = None

    async def finalize(self, artifact: SegmentArtifact) -> None:
        if self.gate is not None:
            await self.gate.wait()
        self.artifacts.append(artifact)
        if artifact.clip_id == self.fail_clip:
            raise RuntimeError("fake finalize failure")


def test_boundary_policy_splits_only_on_closed_gop_idr_inside_explicit_bounds() -> None:
    policy = SegmentBoundaryPolicy()

    assert policy.decide(segment_start_ns=0, access_unit=idr(58)) is BoundaryDecision.WAIT
    assert (
        policy.decide(segment_start_ns=0, access_unit=predicted(59)) is BoundaryDecision.REQUEST_IDR
    )
    assert policy.decide(segment_start_ns=0, access_unit=idr(60)) is BoundaryDecision.SPLIT
    assert policy.decide(segment_start_ns=0, access_unit=idr(62)) is BoundaryDecision.FAULT
    assert policy.decide(segment_start_ns=0, access_unit=predicted(61)) is BoundaryDecision.FAULT


def test_first_and_final_clips_have_explicit_short_allowance_but_middle_does_not() -> None:
    policy = SegmentBoundaryPolicy()

    assert policy.duration_allowed(2 * SECOND, ClipPosition.FIRST)
    assert not policy.duration_allowed(2 * SECOND, ClipPosition.MIDDLE)
    assert policy.duration_allowed(2 * SECOND, ClipPosition.FINAL)
    assert policy.duration_allowed(59 * SECOND, ClipPosition.MIDDLE)
    assert policy.duration_allowed(61 * SECOND, ClipPosition.MIDDLE)
    assert not policy.duration_allowed(61 * SECOND + 1, ClipPosition.MIDDLE)


def test_normal_rotations_never_restart_camera_or_encoder() -> None:
    async def scenario() -> None:
        output = FakeOutput()
        finalizer = RecordingFinalizer()
        queue = FinalizationQueue(finalizer, capacity=3)
        queue.start()
        segmenter = ContinuousSegmenter(
            segment_start_ns=0,
            output=output,
            finalizations=queue,
        )

        first = await segmenter.observe(idr(60))
        second = await segmenter.observe(idr(120))
        await asyncio.sleep(0)
        close_result = await queue.close(drain_timeout_s=0.1)

        assert first.action is SegmentAction.ROTATED
        assert second.action is SegmentAction.ROTATED
        assert output.rotations == [60 * SECOND, 120 * SECOND]
        assert output.camera_starts == 1
        assert close_result.drained
        assert len(finalizer.artifacts) == 2

    run_async(scenario())


def test_idr_request_is_deduplicated_until_a_valid_boundary_arrives() -> None:
    async def scenario() -> None:
        output = FakeOutput()
        queue = FinalizationQueue(RecordingFinalizer())
        queue.start()
        segmenter = ContinuousSegmenter(
            segment_start_ns=0,
            output=output,
            finalizations=queue,
        )

        first = await segmenter.observe(predicted(59))
        duplicate = await segmenter.observe(predicted(60))
        split = await segmenter.observe(idr(60))
        await queue.close(drain_timeout_s=0.1)

        assert first.action is SegmentAction.IDR_REQUESTED
        assert duplicate.action is SegmentAction.NONE
        assert split.action is SegmentAction.ROTATED
        assert output.idr_requests == 1

    run_async(scenario())


def test_missing_closed_gop_idr_at_maximum_duration_faults_explicitly() -> None:
    async def scenario() -> None:
        output = FakeOutput()
        queue = FinalizationQueue(RecordingFinalizer())
        queue.start()
        segmenter = ContinuousSegmenter(
            segment_start_ns=0,
            output=output,
            finalizations=queue,
        )

        outcome = await segmenter.observe(predicted(61))
        await queue.close(drain_timeout_s=0.1)

        assert outcome.action is SegmentAction.BOUNDARY_FAULT
        assert segmenter.faulted
        assert output.rotations == []

    run_async(scenario())


def test_finalization_capacity_counts_inflight_work_and_never_grows() -> None:
    async def scenario() -> None:
        gate = asyncio.Event()
        finalizer = RecordingFinalizer(gate=gate)
        output = FakeOutput()
        queue = FinalizationQueue(finalizer, capacity=1)
        queue.start()
        segmenter = ContinuousSegmenter(
            segment_start_ns=0,
            output=output,
            finalizations=queue,
        )

        first = await segmenter.observe(idr(60))
        await asyncio.sleep(0)
        overloaded = await segmenter.observe(idr(120))

        assert first.action is SegmentAction.ROTATED
        assert overloaded.action is SegmentAction.FINALIZER_OVERLOAD
        assert queue.outstanding == 1
        assert output.rotations == [60 * SECOND]

        gate.set()
        close_result = await queue.close(drain_timeout_s=0.1)
        assert close_result.drained
        assert queue.outstanding == 0

    run_async(scenario())


def test_finalization_failure_is_recorded_and_worker_continues() -> None:
    async def scenario() -> None:
        finalizer = RecordingFinalizer(fail_clip="clip-1")
        queue = FinalizationQueue(finalizer, capacity=2)
        queue.start()
        first = SegmentArtifact("clip-1", 0, SECOND, "one")
        second = SegmentArtifact("clip-2", SECOND, 2 * SECOND, "two")

        assert queue.try_submit(first)
        assert queue.try_submit(second)
        result = await queue.close(drain_timeout_s=0.1)

        assert result.drained
        assert result.failed_count == 1
        assert [artifact.clip_id for artifact in finalizer.artifacts] == ["clip-1", "clip-2"]
        assert queue.last_failure is not None
        assert "fake finalize failure" in queue.last_failure
        assert queue.state is FinalizationState.CLOSED

    run_async(scenario())


def test_finalizer_close_cancels_blocked_work_at_bounded_deadline() -> None:
    async def scenario() -> None:
        gate = asyncio.Event()
        queue = FinalizationQueue(RecordingFinalizer(gate=gate), capacity=1)
        queue.start()
        assert queue.try_submit(SegmentArtifact("clip-1", 0, SECOND, "one"))
        await asyncio.sleep(0)

        result = await queue.close(drain_timeout_s=0.001)

        assert not result.drained
        assert result.outstanding == 0
        assert queue.state is FinalizationState.CLOSED

    run_async(scenario())


def test_close_allows_a_short_final_clip_and_queues_it() -> None:
    async def scenario() -> None:
        output = FakeOutput()
        finalizer = RecordingFinalizer()
        queue = FinalizationQueue(finalizer)
        queue.start()
        segmenter = ContinuousSegmenter(
            segment_start_ns=0,
            output=output,
            finalizations=queue,
        )

        outcome = await segmenter.close(2 * SECOND)
        result = await queue.close(drain_timeout_s=0.1)

        assert outcome.action is SegmentAction.FINALIZED_ACTIVE
        assert result.drained
        assert finalizer.artifacts[0].end_timestamp_ns == 2 * SECOND
        assert output.camera_starts == 1

    run_async(scenario())


def test_close_faults_instead_of_emitting_an_overlong_final_clip() -> None:
    async def scenario() -> None:
        output = FakeOutput()
        queue = FinalizationQueue(RecordingFinalizer())
        queue.start()
        segmenter = ContinuousSegmenter(
            segment_start_ns=0,
            output=output,
            finalizations=queue,
        )

        outcome = await segmenter.close(62 * SECOND)
        result = await queue.close(drain_timeout_s=0.1)

        assert outcome.action is SegmentAction.BOUNDARY_FAULT
        assert segmenter.faulted
        assert result.drained
        assert output.closes == []

    run_async(scenario())
