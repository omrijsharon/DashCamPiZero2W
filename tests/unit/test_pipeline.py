from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from dashcam.recorder.pipeline import (
    CameraOwnership,
    CameraOwnershipError,
    ContinuousPipeline,
    MonotonicMediaClock,
    PipelineContractError,
    PipelineFault,
    ProfileValidationError,
    RecoverablePipelineError,
    RestartPolicy,
    VideoProfile,
)


def run_async(coroutine: object) -> object:
    return asyncio.run(coroutine)  # type: ignore[arg-type]


@dataclass
class BackendScript:
    failures_before_success: int = 0
    starts: int = 0
    stops: int = 0
    effective_profile: VideoProfile | None = None

    def factory(self) -> FakeBackend:
        return FakeBackend(self)


class FakeBackend:
    def __init__(self, script: BackendScript) -> None:
        self.script = script

    async def start(self, requested_profile: VideoProfile) -> VideoProfile:
        self.script.starts += 1
        return self.script.effective_profile or requested_profile

    async def run(self, stop_requested: asyncio.Event) -> None:
        if self.script.starts <= self.script.failures_before_success:
            raise RecoverablePipelineError("fake recoverable failure")
        await stop_requested.wait()

    async def stop(self) -> None:
        self.script.stops += 1


@dataclass
class FakeWaiter:
    cancel_on_call: int | None = None
    delays: list[float] = field(default_factory=list)

    async def __call__(self, delay_s: float, stop_requested: asyncio.Event) -> bool:
        self.delays.append(delay_s)
        if self.cancel_on_call == len(self.delays):
            stop_requested.set()
            return True
        return False


class FailingOptionalBranch:
    name = "preview"

    async def run(self, stop_requested: asyncio.Event) -> None:
        raise RuntimeError("preview failed")


def test_production_profile_refuses_any_software_or_downgraded_variant() -> None:
    for changes in (
        {"width": 1280},
        {"height": 720},
        {"frames_per_second": 29},
        {"codec": "h265"},
        {"hardware_encoded": False},
    ):
        with pytest.raises(ProfileValidationError, match="production requires"):
            VideoProfile(**changes)


def test_backend_cannot_silently_downgrade_effective_profile() -> None:
    async def scenario() -> None:
        requested = VideoProfile()
        script = BackendScript(
            effective_profile=VideoProfile(
                width=1280,
                height=720,
                frames_per_second=30,
                hardware_encoded=False,
                production=False,
            )
        )
        pipeline = ContinuousPipeline(
            owner="dashcamd",
            backend_factory=script.factory,
            profile=requested,
            ownership=CameraOwnership(),
        )

        with pytest.raises(ProfileValidationError, match="effective profile"):
            await pipeline.run(asyncio.Event())

        assert script.starts == 1
        assert script.stops == 1

    run_async(scenario())


def test_camera_ownership_refuses_a_second_owner_and_wrong_release() -> None:
    ownership = CameraOwnership()
    ownership.claim("dashcamd")

    with pytest.raises(CameraOwnershipError, match="already owned"):
        ownership.claim("preview")
    with pytest.raises(CameraOwnershipError, match="its owner"):
        ownership.release("web")

    ownership.release("dashcamd")
    assert ownership.owner is None


def test_restart_attempts_and_exponential_backoff_are_strictly_bounded() -> None:
    async def scenario() -> None:
        script = BackendScript(failures_before_success=99)
        waiter = FakeWaiter()
        pipeline = ContinuousPipeline(
            owner="dashcamd",
            backend_factory=script.factory,
            ownership=CameraOwnership(),
            restart_policy=RestartPolicy(
                max_restarts=3,
                initial_backoff_s=1,
                maximum_backoff_s=3,
                multiplier=2,
            ),
            backoff_waiter=waiter,
        )

        with pytest.raises(PipelineFault, match="exhausted"):
            await pipeline.run(asyncio.Event())

        assert script.starts == 4
        assert script.stops == 4
        assert waiter.delays == [1, 2, 3]

    run_async(scenario())


def test_cancellation_during_backoff_prevents_another_camera_start() -> None:
    async def scenario() -> None:
        script = BackendScript(failures_before_success=99)
        waiter = FakeWaiter(cancel_on_call=1)
        stop_requested = asyncio.Event()
        pipeline = ContinuousPipeline(
            owner="dashcamd",
            backend_factory=script.factory,
            ownership=CameraOwnership(),
            restart_policy=RestartPolicy(max_restarts=5),
            backoff_waiter=waiter,
        )

        result = await pipeline.run(stop_requested)

        assert result.restart_count == 1
        assert script.starts == 1
        assert script.stops == 1
        assert waiter.delays == [1.0]

    run_async(scenario())


def test_optional_branch_failure_degrades_without_stopping_video() -> None:
    async def scenario() -> None:
        script = BackendScript()
        stop_requested = asyncio.Event()
        pipeline = ContinuousPipeline(
            owner="dashcamd",
            backend_factory=script.factory,
            ownership=CameraOwnership(),
            optional_branches=(FailingOptionalBranch(),),
        )
        task = asyncio.create_task(pipeline.run(stop_requested))
        while script.starts == 0:
            await asyncio.sleep(0)
        for _ in range(5):
            await asyncio.sleep(0)

        assert not task.done()
        stop_requested.set()
        result = await task

        assert result.degraded
        assert result.optional_failures[0].name == "preview"
        assert script.starts == 1
        assert script.stops == 1

    run_async(scenario())


def test_task_cancellation_cleans_up_and_releases_camera_ownership() -> None:
    async def scenario() -> None:
        ownership = CameraOwnership()
        script = BackendScript()
        pipeline = ContinuousPipeline(
            owner="dashcamd",
            backend_factory=script.factory,
            ownership=ownership,
        )
        task = asyncio.create_task(pipeline.run(asyncio.Event()))
        while script.starts == 0:
            await asyncio.sleep(0)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert script.stops == 1
        assert ownership.owner is None

    run_async(scenario())


def test_media_clock_uses_monotonic_values_and_rejects_regression() -> None:
    values = iter((10, 11, 9))
    clock = MonotonicMediaClock(lambda: next(values))

    assert clock.now_ns() == 10
    assert clock.now_ns() == 11
    with pytest.raises(PipelineContractError, match="regressed"):
        clock.now_ns()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_restarts": -1},
        {"max_restarts": True},
        {"initial_backoff_s": 0},
        {"maximum_backoff_s": 0},
        {"multiplier": 0.5},
        {"stop_timeout_s": 301},
    ],
)
def test_restart_policy_rejects_invalid_or_unbounded_values(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        RestartPolicy(**kwargs)  # type: ignore[arg-type]
