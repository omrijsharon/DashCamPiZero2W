"""Typed values returned by the durable clip catalog."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from dashcam.state import ClipLifecycle, DownloadLease
from dashcam.storage.intents import OperationIntent


class CatalogError(RuntimeError):
    """Base error for catalog operations."""


class ClipNotFoundError(CatalogError):
    """Raised when a stable clip identifier is not in the catalog."""


class CatalogConflictError(CatalogError):
    """Raised when a requested operation conflicts with durable clip state."""


class ReconciliationLimitError(CatalogError):
    """Raised when a caller supplies an invalid recovery bound."""


class EventSource(StrEnum):
    """Allowed event origins recorded in the durable catalog."""

    WEB = "web"
    GPIO = "gpio"
    API = "api"


class EventTargetRole(StrEnum):
    """Position of a protected clip relative to an event."""

    PREVIOUS = "PREVIOUS"
    CURRENT = "CURRENT"
    NEXT = "NEXT"


@dataclass(frozen=True, slots=True)
class CatalogClip:
    """One durable clip row.

    Paths are relative to the verified recording root. ``download_lease`` is
    exposed only when its boot epoch matches the caller's epoch.
    """

    clip_id: UUID
    lifecycle: ClipLifecycle
    video_path: str
    sidecar_path: str
    start_monotonic_ns: int
    end_monotonic_ns: int | None
    retention_order: int
    size_bytes: int
    protected: bool
    protection_reason: str | None
    pair_reconciled: bool
    managed: bool
    download_lease: DownloadLease | None = None
    lease_boot_id: str | None = None


@dataclass(frozen=True, slots=True)
class EventProtectionResult:
    """Durable result acknowledged for one event request."""

    event_id: UUID
    protected_clip_ids: tuple[UUID, ...]
    missing_previous_count: int
    pending_next_count: int
    queued_intent_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class IntentReconciliationResult:
    """Outcome of one bounded intent-reconciliation attempt."""

    intent: OperationIntent
    actions_attempted: int
    complete: bool
    problems: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StartupReconciliationReport:
    """Bounded startup pass summary.

    ``more_work`` tells the caller to schedule another bounded pass rather than
    delaying recording startup indefinitely.
    """

    intents_examined: int
    actions_attempted: int
    catalog_clips_examined: int
    directory_entries_examined: int
    imported_clips: int
    expired_leases_cleared: int
    issues: tuple[str, ...]
    more_work: bool


@dataclass(frozen=True, slots=True)
class ReconciliationBounds:
    """Hard work and input-size limits for one startup recovery pass."""

    max_intents: int = 64
    max_actions: int = 128
    max_catalog_clips: int = 2_048
    max_directory_entries: int = 4_096
    max_sidecar_bytes: int = 1_048_576
    max_issues: int = 128
    max_expired_leases: int = 512

    def __post_init__(self) -> None:
        for name, value, maximum in (
            ("max_intents", self.max_intents, 1_024),
            ("max_actions", self.max_actions, 2_048),
            ("max_catalog_clips", self.max_catalog_clips, 100_000),
            ("max_directory_entries", self.max_directory_entries, 100_000),
            ("max_sidecar_bytes", self.max_sidecar_bytes, 16 * 1_048_576),
            ("max_issues", self.max_issues, 4_096),
            ("max_expired_leases", self.max_expired_leases, 10_000),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ReconciliationLimitError(f"{name} must be a positive integer")
            if value > maximum:
                raise ReconciliationLimitError(f"{name} exceeds its hard maximum")
