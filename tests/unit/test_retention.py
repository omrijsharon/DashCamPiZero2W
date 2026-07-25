from __future__ import annotations

from uuid import UUID

import pytest

from dashcam.storage.retention import (
    RetentionCandidate,
    RetentionMode,
    StorageThresholds,
    retention_mode,
    select_oldest_eligible,
)

_GIB = 1024**3


def _clip(number: int, **overrides: object) -> RetentionCandidate:
    values: dict[str, object] = {
        "clip_id": UUID(int=number),
        "retention_order": number,
        "size_bytes": 100,
    }
    values.update(overrides)
    return RetentionCandidate(**values)  # type: ignore[arg-type]


def test_thresholds_use_stricter_percentage_and_absolute_reserve() -> None:
    thresholds = StorageThresholds(15, 20, 2 * _GIB, 256 * 1024**2)

    resolved = thresholds.resolve(25 * 1000**3)

    assert resolved.start_deletion_below_bytes == 3_750_000_000
    assert resolved.stop_deletion_at_bytes == 5_000_000_000
    assert resolved.emergency_below_bytes == 256 * 1024**2


def test_hysteresis_starts_below_low_and_stops_at_high() -> None:
    resolved = StorageThresholds(15, 20, 2 * _GIB, 256 * 1024**2).resolve(25 * 1000**3)

    assert (
        retention_mode(
            free_bytes=resolved.start_deletion_below_bytes,
            thresholds=resolved,
            was_reclaiming=False,
        )
        is RetentionMode.NORMAL
    )
    assert (
        retention_mode(
            free_bytes=resolved.start_deletion_below_bytes - 1,
            thresholds=resolved,
            was_reclaiming=False,
        )
        is RetentionMode.RECLAIMING
    )
    assert (
        retention_mode(
            free_bytes=resolved.stop_deletion_at_bytes - 1,
            thresholds=resolved,
            was_reclaiming=True,
        )
        is RetentionMode.RECLAIMING
    )
    assert (
        retention_mode(
            free_bytes=resolved.stop_deletion_at_bytes,
            thresholds=resolved,
            was_reclaiming=True,
        )
        is RetentionMode.NORMAL
    )
    assert (
        retention_mode(
            free_bytes=resolved.emergency_below_bytes - 1,
            thresholds=resolved,
            was_reclaiming=True,
        )
        is RetentionMode.EMERGENCY
    )


def test_oldest_eligible_selection_excludes_every_protected_category() -> None:
    candidates = (
        _clip(1, managed=False),
        _clip(2, finalized=False),
        _clip(3, pair_reconciled=False),
        _clip(4, protected=True),
        _clip(5, mutation_in_progress=True),
        _clip(6, lease_expires_monotonic_ns=101),
        _clip(7),
        _clip(8),
    )

    plan = select_oldest_eligible(candidates, requested_reclaim_bytes=150, monotonic_ns=100)

    assert plan.selected_clip_ids == (UUID(int=7), UUID(int=8))
    assert plan.planned_reclaim_bytes == 200
    assert plan.target_reached


def test_expired_lease_becomes_eligible_and_insufficient_plan_is_explicit() -> None:
    plan = select_oldest_eligible(
        (_clip(1, size_bytes=50, lease_expires_monotonic_ns=100),),
        requested_reclaim_bytes=100,
        monotonic_ns=100,
    )

    assert plan.selected_clip_ids == (UUID(int=1),)
    assert plan.planned_reclaim_bytes == 50
    assert not plan.target_reached


@pytest.mark.parametrize(
    "thresholds",
    [
        StorageThresholds(20, 20 + 1, 100, 10),
        StorageThresholds(0, 99, 100, 10),
    ],
)
def test_thresholds_reject_capacity_that_cannot_preserve_hysteresis(
    thresholds: StorageThresholds,
) -> None:
    with pytest.raises(ValueError):
        thresholds.resolve(100)
