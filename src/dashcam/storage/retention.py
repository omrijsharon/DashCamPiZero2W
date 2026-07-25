"""Pure retention threshold and oldest-eligible selection logic."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class RetentionMode(StrEnum):
    """Storage policy state derived without performing filesystem work."""

    NORMAL = "NORMAL"
    RECLAIMING = "RECLAIMING"
    EMERGENCY = "EMERGENCY"


@dataclass(frozen=True, slots=True)
class ResolvedThresholds:
    """Byte thresholds resolved for one filesystem capacity."""

    start_deletion_below_bytes: int
    stop_deletion_at_bytes: int
    emergency_below_bytes: int


@dataclass(frozen=True, slots=True)
class StorageThresholds:
    """Configured free-space thresholds.

    Percentages are integers to keep the byte calculation deterministic.
    """

    low_watermark_percent: int
    high_watermark_percent: int
    minimum_free_bytes: int
    emergency_free_bytes: int

    def __post_init__(self) -> None:
        for name, value in (
            ("low_watermark_percent", self.low_watermark_percent),
            ("high_watermark_percent", self.high_watermark_percent),
            ("minimum_free_bytes", self.minimum_free_bytes),
            ("emergency_free_bytes", self.emergency_free_bytes),
        ):
            _require_integer(value, name)
        if not 0 <= self.low_watermark_percent < self.high_watermark_percent < 100:
            raise ValueError("watermarks must satisfy 0 <= low < high < 100")
        if self.minimum_free_bytes < 0 or self.emergency_free_bytes < 0:
            raise ValueError("free-space thresholds cannot be negative")
        if self.emergency_free_bytes >= self.minimum_free_bytes:
            raise ValueError("emergency reserve must be below the minimum free reserve")

    def resolve(self, capacity_bytes: int) -> ResolvedThresholds:
        """Resolve percentage/absolute thresholds for a concrete capacity."""

        _require_integer(capacity_bytes, "capacity_bytes")
        if capacity_bytes <= 0:
            raise ValueError("capacity_bytes must be positive")

        start = max(
            _percent_bytes(capacity_bytes, self.low_watermark_percent),
            self.minimum_free_bytes,
        )
        stop = max(
            _percent_bytes(capacity_bytes, self.high_watermark_percent),
            self.minimum_free_bytes,
        )
        if start >= stop:
            raise ValueError("resolved stop threshold must be greater than start threshold")
        if stop >= capacity_bytes:
            raise ValueError("resolved stop threshold must be below total capacity")
        if self.emergency_free_bytes >= start:
            raise ValueError("emergency threshold must be below deletion start threshold")

        return ResolvedThresholds(
            start_deletion_below_bytes=start,
            stop_deletion_at_bytes=stop,
            emergency_below_bytes=self.emergency_free_bytes,
        )


def _percent_bytes(capacity_bytes: int, percent: int) -> int:
    """Return a ceiling percentage using integer arithmetic."""

    return (capacity_bytes * percent + 99) // 100


def retention_mode(
    *,
    free_bytes: int,
    thresholds: ResolvedThresholds,
    was_reclaiming: bool,
) -> RetentionMode:
    """Apply hysteresis and return the next policy mode."""

    if free_bytes < 0:
        raise ValueError("free_bytes cannot be negative")
    if free_bytes < thresholds.emergency_below_bytes:
        return RetentionMode.EMERGENCY
    if was_reclaiming:
        return (
            RetentionMode.RECLAIMING
            if free_bytes < thresholds.stop_deletion_at_bytes
            else RetentionMode.NORMAL
        )
    return (
        RetentionMode.RECLAIMING
        if free_bytes < thresholds.start_deletion_below_bytes
        else RetentionMode.NORMAL
    )


@dataclass(frozen=True, slots=True)
class RetentionCandidate:
    """Catalog facts used to determine whether one managed clip is eligible."""

    clip_id: UUID
    retention_order: int
    size_bytes: int
    managed: bool = True
    finalized: bool = True
    pair_reconciled: bool = True
    protected: bool = False
    mutation_in_progress: bool = False
    lease_expires_monotonic_ns: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.clip_id, UUID):
            raise TypeError("clip_id must be a UUID")
        _require_integer(self.retention_order, "retention_order")
        _require_integer(self.size_bytes, "size_bytes")
        for name, value in (
            ("managed", self.managed),
            ("finalized", self.finalized),
            ("pair_reconciled", self.pair_reconciled),
            ("protected", self.protected),
            ("mutation_in_progress", self.mutation_in_progress),
        ):
            if not isinstance(value, bool):
                raise TypeError(f"{name} must be boolean")
        if self.lease_expires_monotonic_ns is not None:
            _require_integer(
                self.lease_expires_monotonic_ns,
                "lease_expires_monotonic_ns",
            )
        if self.retention_order < 0:
            raise ValueError("retention_order cannot be negative")
        if self.size_bytes < 0:
            raise ValueError("size_bytes cannot be negative")
        if self.lease_expires_monotonic_ns is not None and self.lease_expires_monotonic_ns < 0:
            raise ValueError("lease expiry cannot be negative")

    def eligible_at(self, monotonic_ns: int) -> bool:
        """Return whether retention may select this candidate now."""

        _require_integer(monotonic_ns, "monotonic_ns")
        if monotonic_ns < 0:
            raise ValueError("monotonic_ns cannot be negative")
        has_active_lease = (
            self.lease_expires_monotonic_ns is not None
            and monotonic_ns < self.lease_expires_monotonic_ns
        )
        return (
            self.managed
            and self.finalized
            and self.pair_reconciled
            and not self.protected
            and not self.mutation_in_progress
            and not has_active_lease
        )


@dataclass(frozen=True, slots=True)
class RetentionPlan:
    """Oldest-first selection result."""

    selected_clip_ids: tuple[UUID, ...]
    planned_reclaim_bytes: int
    requested_reclaim_bytes: int

    @property
    def target_reached(self) -> bool:
        """Return whether selected managed clips satisfy the byte request."""

        return self.planned_reclaim_bytes >= self.requested_reclaim_bytes


def select_oldest_eligible(
    candidates: tuple[RetentionCandidate, ...],
    *,
    requested_reclaim_bytes: int,
    monotonic_ns: int,
) -> RetentionPlan:
    """Select eligible candidates in stable durable-catalog order."""

    _require_integer(requested_reclaim_bytes, "requested_reclaim_bytes")
    _require_integer(monotonic_ns, "monotonic_ns")
    if requested_reclaim_bytes < 0:
        raise ValueError("requested_reclaim_bytes cannot be negative")
    if monotonic_ns < 0:
        raise ValueError("monotonic_ns cannot be negative")
    if requested_reclaim_bytes == 0:
        return RetentionPlan((), 0, 0)

    eligible = sorted(
        (candidate for candidate in candidates if candidate.eligible_at(monotonic_ns)),
        key=lambda candidate: (candidate.retention_order, str(candidate.clip_id)),
    )

    selected: list[UUID] = []
    planned_bytes = 0
    for candidate in eligible:
        selected.append(candidate.clip_id)
        planned_bytes += candidate.size_bytes
        if planned_bytes >= requested_reclaim_bytes:
            break

    return RetentionPlan(
        selected_clip_ids=tuple(selected),
        planned_reclaim_bytes=planned_bytes,
        requested_reclaim_bytes=requested_reclaim_bytes,
    )


def _require_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    return value
