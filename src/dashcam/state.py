"""Hardware-independent recorder and clip state models.

These values are deliberately small and immutable: durable storage is responsible
for persisting each returned value before carrying out filesystem work.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Final
from uuid import UUID, uuid4


class RecorderState(StrEnum):
    STARTING = "STARTING"
    RECORDING = "RECORDING"
    DEGRADED = "DEGRADED"
    STOPPING = "STOPPING"
    FAULTED = "FAULTED"


class StorageState(StrEnum):
    UNKNOWN = "UNKNOWN"
    CHECKING = "CHECKING"
    READY = "READY"
    LOW_SPACE = "LOW_SPACE"
    EMERGENCY = "EMERGENCY"
    READ_ONLY = "READ_ONLY"
    FAULTED = "FAULTED"


class GpsState(StrEnum):
    UART_UNAVAILABLE = "UART_UNAVAILABLE"
    RECEIVING_INVALID = "RECEIVING_INVALID"
    TIME_VALID_POSITION_INVALID = "TIME_VALID_POSITION_INVALID"
    NAVIGATION_VALID = "NAVIGATION_VALID"
    STALE = "STALE"
    FAULTED = "FAULTED"


class AudioState(StrEnum):
    DISABLED = "DISABLED"
    UNAVAILABLE = "UNAVAILABLE"
    AVAILABLE = "AVAILABLE"
    FAULTED = "FAULTED"


class GpsTimeState(StrEnum):
    UNSYNCED = "UNSYNCED"
    GPS_TIME_VALID = "GPS_TIME_VALID"
    GPS_TIME_STALE = "GPS_TIME_STALE"


class SystemClockState(StrEnum):
    UNSET = "UNSET"
    SYNCING = "SYNCING"
    SYNCHRONIZED = "SYNCHRONIZED"
    ERROR = "ERROR"


class TimestampQuality(StrEnum):
    MONOTONIC_ONLY = "MONOTONIC_ONLY"
    GPS_ANCHORED = "GPS_ANCHORED"
    SYSTEM_DERIVED = "SYSTEM_DERIVED"


class DeviceOperationState(StrEnum):
    IDLE = "IDLE"
    PREPARING_REMOVAL = "PREPARING_REMOVAL"
    SHUTTING_DOWN = "SHUTTING_DOWN"


class ClipLifecycle(StrEnum):
    CREATING = "CREATING"
    WRITING = "WRITING"
    FINALIZING = "FINALIZING"
    FINALIZED = "FINALIZED"
    DELETING = "DELETING"
    DELETED = "DELETED"
    CORRUPT = "CORRUPT"
    QUARANTINED = "QUARANTINED"
    MISSING_SIDECAR = "MISSING_SIDECAR"
    MISSING_VIDEO = "MISSING_VIDEO"


class StateTransitionError(ValueError):
    """Raised when a lifecycle or attribute operation is unsafe."""


class DownloadLeaseError(ValueError):
    """Raised when a requested download lease is invalid or unavailable."""


NANOSECONDS_PER_SECOND: Final = 1_000_000_000
MAX_DOWNLOAD_LEASE_NS: Final = 15 * 60 * NANOSECONDS_PER_SECOND
MAX_LEASE_HOLDER_LENGTH: Final = 128
_LEASE_HOLDER_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")

_TRANSITIONS: dict[ClipLifecycle, frozenset[ClipLifecycle]] = {
    ClipLifecycle.CREATING: frozenset(
        {ClipLifecycle.WRITING, ClipLifecycle.CORRUPT, ClipLifecycle.QUARANTINED}
    ),
    ClipLifecycle.WRITING: frozenset(
        {ClipLifecycle.FINALIZING, ClipLifecycle.CORRUPT, ClipLifecycle.QUARANTINED}
    ),
    ClipLifecycle.FINALIZING: frozenset(
        {
            ClipLifecycle.FINALIZED,
            ClipLifecycle.CORRUPT,
            ClipLifecycle.QUARANTINED,
            ClipLifecycle.MISSING_SIDECAR,
            ClipLifecycle.MISSING_VIDEO,
        }
    ),
    ClipLifecycle.FINALIZED: frozenset(
        {
            ClipLifecycle.DELETING,
            ClipLifecycle.CORRUPT,
            ClipLifecycle.QUARANTINED,
            ClipLifecycle.MISSING_SIDECAR,
            ClipLifecycle.MISSING_VIDEO,
        }
    ),
    ClipLifecycle.DELETING: frozenset(
        {
            ClipLifecycle.DELETED,
            ClipLifecycle.FINALIZED,
            ClipLifecycle.CORRUPT,
            ClipLifecycle.QUARANTINED,
            ClipLifecycle.MISSING_SIDECAR,
            ClipLifecycle.MISSING_VIDEO,
        }
    ),
    ClipLifecycle.DELETED: frozenset(),
    ClipLifecycle.CORRUPT: frozenset({ClipLifecycle.QUARANTINED}),
    ClipLifecycle.QUARANTINED: frozenset(),
    ClipLifecycle.MISSING_SIDECAR: frozenset({ClipLifecycle.FINALIZED, ClipLifecycle.QUARANTINED}),
    ClipLifecycle.MISSING_VIDEO: frozenset({ClipLifecycle.FINALIZED, ClipLifecycle.QUARANTINED}),
}


@dataclass(frozen=True, slots=True)
class DownloadLease:
    """A short, monotonic-clock lease owned by ``dashcamd``."""

    holder: str
    issued_at_monotonic_ns: int
    expires_at_monotonic_ns: int

    def __post_init__(self) -> None:
        if (
            not self.holder.isascii()
            or len(self.holder) > MAX_LEASE_HOLDER_LENGTH
            or _LEASE_HOLDER_PATTERN.fullmatch(self.holder) is None
        ):
            raise DownloadLeaseError("lease holder must be a bounded safe identifier")
        if (
            isinstance(self.issued_at_monotonic_ns, bool)
            or isinstance(self.expires_at_monotonic_ns, bool)
            or not isinstance(self.issued_at_monotonic_ns, int)
            or not isinstance(self.expires_at_monotonic_ns, int)
        ):
            raise DownloadLeaseError("lease timestamps must be integer nanoseconds")
        if self.issued_at_monotonic_ns < 0:
            raise DownloadLeaseError("lease issue time cannot be negative")
        if self.expires_at_monotonic_ns <= self.issued_at_monotonic_ns:
            raise DownloadLeaseError("lease expiry must be after issue time")
        if self.duration_ns > MAX_DOWNLOAD_LEASE_NS:
            raise DownloadLeaseError("lease duration exceeds configured bound")

    @property
    def duration_ns(self) -> int:
        return self.expires_at_monotonic_ns - self.issued_at_monotonic_ns

    def is_active(self, monotonic_now_ns: int) -> bool:
        """Return whether the lease is active at ``monotonic_now``.

        Expiry is exclusive so it is safe to acquire a replacement exactly at the
        recorded deadline.
        """

        if isinstance(monotonic_now_ns, bool) or not isinstance(monotonic_now_ns, int):
            raise DownloadLeaseError("monotonic time must be integer nanoseconds")
        if monotonic_now_ns < 0:
            raise DownloadLeaseError("monotonic time cannot be negative")
        return monotonic_now_ns < self.expires_at_monotonic_ns

    @classmethod
    def issue(cls, *, holder: str, monotonic_now_ns: int, duration_ns: int) -> DownloadLease:
        if (
            isinstance(duration_ns, bool)
            or not isinstance(duration_ns, int)
            or duration_ns <= 0
            or duration_ns > MAX_DOWNLOAD_LEASE_NS
        ):
            raise DownloadLeaseError("lease duration must be positive and bounded")
        return cls(
            holder=holder,
            issued_at_monotonic_ns=monotonic_now_ns,
            expires_at_monotonic_ns=monotonic_now_ns + duration_ns,
        )


@dataclass(frozen=True, slots=True)
class ClipRecord:
    """Catalog state for one stable clip UUID.

    Protection and the lease intentionally are attributes rather than lifecycle
    states: an event may protect an active clip, while a finalized protected clip
    can still be downloaded.
    """

    clip_id: UUID
    lifecycle: ClipLifecycle = ClipLifecycle.CREATING
    protected: bool = False
    download_lease: DownloadLease | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.clip_id, UUID):
            raise StateTransitionError("clip_id must be a UUID")
        if not isinstance(self.lifecycle, ClipLifecycle):
            raise StateTransitionError("lifecycle must be a ClipLifecycle")
        if not isinstance(self.protected, bool):
            raise StateTransitionError("protected must be boolean")
        if self.download_lease is not None and not isinstance(self.download_lease, DownloadLease):
            raise StateTransitionError("download_lease must be a DownloadLease")

    @classmethod
    def create(cls, clip_id: UUID | None = None) -> ClipRecord:
        return cls(clip_id=clip_id or uuid4())

    def transition_to(self, target: ClipLifecycle) -> ClipRecord:
        if not isinstance(target, ClipLifecycle):
            raise StateTransitionError("transition target must be a ClipLifecycle")
        if target not in _TRANSITIONS[self.lifecycle]:
            message = f"cannot transition {self.lifecycle.value} to {target.value}"
            raise StateTransitionError(message)
        if target is ClipLifecycle.DELETED and self.download_lease is not None:
            raise StateTransitionError("cannot delete a clip with a lease record")
        return replace(self, lifecycle=target)

    def set_protected(self, protected: bool) -> ClipRecord:
        if not isinstance(protected, bool):
            raise StateTransitionError("protected must be boolean")
        if self.lifecycle is ClipLifecycle.DELETED:
            raise StateTransitionError("cannot change protection on a deleted clip")
        return replace(self, protected=protected)

    def has_active_download_lease(self, monotonic_now_ns: int) -> bool:
        return self.download_lease is not None and self.download_lease.is_active(monotonic_now_ns)

    def acquire_download_lease(
        self, *, holder: str, monotonic_now_ns: int, duration_ns: int
    ) -> ClipRecord:
        if (
            isinstance(monotonic_now_ns, bool)
            or not isinstance(monotonic_now_ns, int)
            or monotonic_now_ns < 0
        ):
            raise DownloadLeaseError("monotonic time must be non-negative integer nanoseconds")
        if self.lifecycle is not ClipLifecycle.FINALIZED:
            raise DownloadLeaseError("only finalized clips can be downloaded")
        if self.has_active_download_lease(monotonic_now_ns):
            raise DownloadLeaseError("clip already has an active download lease")
        return replace(
            self,
            download_lease=DownloadLease.issue(
                holder=holder,
                monotonic_now_ns=monotonic_now_ns,
                duration_ns=duration_ns,
            ),
        )

    def clear_expired_download_lease(self, monotonic_now_ns: int) -> ClipRecord:
        if self.download_lease is None or self.download_lease.is_active(monotonic_now_ns):
            return self
        return replace(self, download_lease=None)

    def release_download_lease(self, holder: str) -> ClipRecord:
        if self.download_lease is None:
            return self
        if self.download_lease.holder != holder:
            raise DownloadLeaseError("lease is owned by a different holder")
        return replace(self, download_lease=None)

    def is_retention_eligible(self, monotonic_now_ns: int) -> bool:
        return (
            self.lifecycle is ClipLifecycle.FINALIZED
            and not self.protected
            and not self.has_active_download_lease(monotonic_now_ns)
        )
