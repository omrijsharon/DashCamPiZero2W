"""Exact, stateful free-space threshold policy."""

from __future__ import annotations

from dataclasses import dataclass

from dashcam.storage.retention import ResolvedThresholds, RetentionMode, retention_mode


@dataclass(slots=True)
class StorageThresholdController:
    """Apply low/high hysteresis and no-space emergency escalation.

    Equality is deliberate: deletion begins only *below* the low threshold,
    continues until free space is *at least* the high threshold, and emergency
    begins only *below* the emergency threshold unless a write reports ENOSPC.
    """

    thresholds: ResolvedThresholds
    mode: RetentionMode = RetentionMode.NORMAL

    def __post_init__(self) -> None:
        if not isinstance(self.thresholds, ResolvedThresholds):
            raise TypeError("thresholds must be ResolvedThresholds")
        if not isinstance(self.mode, RetentionMode):
            raise TypeError("mode must be RetentionMode")
        emergency = self.thresholds.emergency_below_bytes
        start = self.thresholds.start_deletion_below_bytes
        stop = self.thresholds.stop_deletion_at_bytes
        for name, value in (
            ("emergency_below_bytes", emergency),
            ("start_deletion_below_bytes", start),
            ("stop_deletion_at_bytes", stop),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not emergency < start < stop:
            raise ValueError("thresholds must satisfy emergency < start < stop")

    def evaluate(self, *, free_bytes: int, no_space_write: bool = False) -> RetentionMode:
        if not isinstance(no_space_write, bool):
            raise TypeError("no_space_write must be boolean")
        if no_space_write:
            self.mode = RetentionMode.EMERGENCY
            return self.mode
        self.mode = retention_mode(
            free_bytes=free_bytes,
            thresholds=self.thresholds,
            was_reclaiming=self.mode in {RetentionMode.RECLAIMING, RetentionMode.EMERGENCY},
        )
        return self.mode
