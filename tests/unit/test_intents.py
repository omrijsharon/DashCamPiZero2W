from __future__ import annotations

from uuid import UUID

import pytest

from dashcam.storage.intents import (
    ActionKind,
    IntentKind,
    MemberObservation,
    OperationIntent,
    PairMember,
    PairPaths,
    plan_reconciliation,
)


def _move_intent(kind: IntentKind = IntentKind.PROTECT) -> OperationIntent:
    return OperationIntent(
        intent_id=UUID(int=1),
        clip_id=UUID(int=2),
        kind=kind,
        created_monotonic_ns=123,
        paths=PairPaths(
            video_source="clips/clip.mp4",
            sidecar_source="clips/clip.json",
            video_target="protected/clip.mp4",
            sidecar_target="protected/clip.json",
        ),
    )


def test_intent_round_trip_is_strict_and_versioned() -> None:
    intent = _move_intent()

    assert OperationIntent.from_dict(intent.as_dict()) == intent

    raw = intent.as_dict()
    raw["unexpected"] = True
    with pytest.raises(ValueError):
        OperationIntent.from_dict(raw)


@pytest.mark.parametrize(
    "path",
    [
        "../clips/clip.mp4",
        "/srv/dashcam/clips/clip.mp4",
        "clips\\clip.mp4",
        "C:/clips/clip.mp4",
    ],
)
def test_intent_rejects_paths_outside_managed_relative_namespace(path: str) -> None:
    with pytest.raises(ValueError):
        PairPaths(path, "clips/clip.json")


def test_interrupted_move_plans_only_the_unfinished_member() -> None:
    plan = plan_reconciliation(
        _move_intent(),
        video=MemberObservation(source_exists=False, target_exists=True),
        sidecar=MemberObservation(source_exists=True, target_exists=False),
    )

    assert plan.completed_members == (PairMember.VIDEO,)
    assert len(plan.actions) == 1
    assert plan.actions[0].kind is ActionKind.MOVE
    assert plan.actions[0].member is PairMember.SIDECAR
    assert not plan.complete


def test_replayed_completed_move_is_idempotently_complete() -> None:
    plan = plan_reconciliation(
        _move_intent(),
        video=MemberObservation(source_exists=False, target_exists=True),
        sidecar=MemberObservation(source_exists=False, target_exists=True),
    )

    assert plan.complete


def test_move_conflict_and_missing_member_require_explicit_recovery() -> None:
    plan = plan_reconciliation(
        _move_intent(),
        video=MemberObservation(source_exists=True, target_exists=True),
        sidecar=MemberObservation(source_exists=False, target_exists=False),
    )

    assert not plan.actions
    assert [problem.code for problem in plan.problems] == [
        "SOURCE_TARGET_CONFLICT",
        "SOURCE_AND_TARGET_MISSING",
    ]


def test_delete_retries_only_existing_member() -> None:
    intent = OperationIntent(
        intent_id=UUID(int=3),
        clip_id=UUID(int=4),
        kind=IntentKind.DELETE,
        created_monotonic_ns=456,
        paths=PairPaths("clips/clip.mp4", "clips/clip.json"),
    )

    plan = plan_reconciliation(
        intent,
        video=MemberObservation(source_exists=False),
        sidecar=MemberObservation(source_exists=True),
    )

    assert plan.completed_members == (PairMember.VIDEO,)
    assert len(plan.actions) == 1
    assert plan.actions[0].kind is ActionKind.UNLINK
    assert plan.actions[0].member is PairMember.SIDECAR
