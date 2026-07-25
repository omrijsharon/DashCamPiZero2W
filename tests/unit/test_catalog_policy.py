from __future__ import annotations

import pytest

from dashcam.catalog import StorageThresholdController
from dashcam.storage.retention import ResolvedThresholds, RetentionMode


def test_threshold_controller_uses_exact_boundaries_and_hysteresis() -> None:
    controller = StorageThresholdController(
        ResolvedThresholds(
            start_deletion_below_bytes=150,
            stop_deletion_at_bytes=200,
            emergency_below_bytes=25,
        )
    )

    assert controller.evaluate(free_bytes=150) is RetentionMode.NORMAL
    assert controller.evaluate(free_bytes=149) is RetentionMode.RECLAIMING
    assert controller.evaluate(free_bytes=199) is RetentionMode.RECLAIMING
    assert controller.evaluate(free_bytes=200) is RetentionMode.NORMAL
    assert controller.evaluate(free_bytes=25) is RetentionMode.RECLAIMING
    assert controller.evaluate(free_bytes=24) is RetentionMode.EMERGENCY
    assert controller.evaluate(free_bytes=175) is RetentionMode.RECLAIMING
    assert controller.evaluate(free_bytes=200) is RetentionMode.NORMAL


def test_no_space_write_forces_emergency_even_above_threshold() -> None:
    controller = StorageThresholdController(ResolvedThresholds(150, 200, 25))

    assert controller.evaluate(free_bytes=500, no_space_write=True) is RetentionMode.EMERGENCY


def test_threshold_controller_rejects_ambiguous_ordering() -> None:
    with pytest.raises(ValueError, match="emergency < start < stop"):
        StorageThresholdController(ResolvedThresholds(100, 100, 10))
