"""Identity-bound live free-space policy with a durable hysteresis latch.

The monitor never enumerates clips and never mutates clip/catalog lifecycle.
Its directive is an input to the later durable ``DELETING`` orchestrator, not
authority to unlink paths or remove protected evidence.
"""

from __future__ import annotations

import errno
import math
import os
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Final, Protocol, cast

from dashcam.catalog.database import RetentionThresholdLatch
from dashcam.catalog.policy import StorageThresholdController
from dashcam.config import StorageConfig
from dashcam.storage.retention import RetentionMode, StorageThresholds

_MAX_BYTES: Final = 9_223_372_036_854_775_807
_MIB: Final = 1024**2
_GIB: Final = 1024**3


class SpaceObservationFault(StrEnum):
    """Stable fail-closed reasons for live space evidence."""

    OBSERVATION_FAILED = "OBSERVATION_FAILED"
    INVALID_OBSERVATION = "INVALID_OBSERVATION"
    OBSERVATION_STALE = "OBSERVATION_STALE"
    IDENTITY_DRIFT = "IDENTITY_DRIFT"
    CAPACITY_DRIFT = "CAPACITY_DRIFT"
    LATCH_LOAD_FAILED = "LATCH_LOAD_FAILED"
    LATCH_BINDING_MISMATCH = "LATCH_BINDING_MISMATCH"
    LATCH_STORE_FAILED = "LATCH_STORE_FAILED"
    NO_SPACE_WRITE = "NO_SPACE_WRITE"


@dataclass(frozen=True, slots=True)
class FilesystemSpaceObservation:
    """One same-descriptor filesystem identity and space observation."""

    device_id: str
    capacity_bytes: int
    free_bytes: int


class SpaceObserver(Protocol):
    def __call__(self) -> FilesystemSpaceObservation:
        """Return one bounded observation without mutating the filesystem."""


class _StatVfsResult(Protocol):
    f_blocks: int
    f_bavail: int
    f_frsize: int


class RetentionLatchStore(Protocol):
    def retention_threshold_latch(self) -> RetentionThresholdLatch | None: ...

    def store_retention_threshold_latch(self, latch: RetentionThresholdLatch) -> None: ...


@dataclass(frozen=True, slots=True)
class RetentionDirective:
    """Advisory target for a future catalog-only deletion transaction."""

    mode: RetentionMode
    target_free_bytes: int
    requested_reclaim_bytes: int
    emergency: bool
    protected_deletion_allowed: bool = False


@dataclass(frozen=True, slots=True)
class StorageSpaceSnapshot:
    """Truthful bounded status for the current monitor state."""

    sequence: int
    mode: RetentionMode | None
    fault: SpaceObservationFault | None
    trigger: str | None
    stale: bool
    stop_required: bool
    reclaimer_enabled: bool
    consecutive_observation_failures: int
    sample_age_ns: int | None
    volume_uuid: str
    device_id: str
    capacity_bytes: int
    free_bytes: int | None
    free_percent: float | None
    start_deletion_below_bytes: int
    stop_deletion_at_bytes: int
    emergency_below_bytes: int
    directive: RetentionDirective | None

    def as_dict(self) -> dict[str, object]:
        directive = self.directive
        return {
            "sequence": self.sequence,
            "mode": None if self.mode is None else self.mode.value,
            "fault": None if self.fault is None else self.fault.value,
            "trigger": self.trigger,
            "stale": self.stale,
            "stop_required": self.stop_required,
            "reclaimer_enabled": self.reclaimer_enabled,
            "consecutive_observation_failures": self.consecutive_observation_failures,
            "sample_age_ns": self.sample_age_ns,
            "volume_uuid_suffix": self.volume_uuid[-4:],
            "device_id": self.device_id,
            "capacity_bytes": self.capacity_bytes,
            "free_bytes": self.free_bytes,
            "free_percent": self.free_percent,
            "thresholds": {
                "start_deletion_below_bytes": self.start_deletion_below_bytes,
                "stop_deletion_at_bytes": self.stop_deletion_at_bytes,
                "emergency_below_bytes": self.emergency_below_bytes,
            },
            "directive": None
            if directive is None
            else {
                "mode": directive.mode.value,
                "target_free_bytes": directive.target_free_bytes,
                "requested_reclaim_bytes": directive.requested_reclaim_bytes,
                "emergency": directive.emergency,
                "protected_deletion_allowed": directive.protected_deletion_allowed,
            },
        }


class StorageSpaceMonitor:
    """Persist transitions before exposing a mode or reclamation directive."""

    def __init__(
        self,
        *,
        volume_uuid: str,
        expected_device_id: str,
        expected_capacity_bytes: int,
        thresholds: StorageThresholds,
        observer: SpaceObserver,
        latch_store: RetentionLatchStore,
        maximum_observation_failures: int = 3,
        reclaimer_available: bool = False,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        _bounded_identity(volume_uuid, "volume_uuid")
        _validate_device_id(expected_device_id)
        _positive_integer(expected_capacity_bytes, "expected_capacity_bytes")
        if not isinstance(thresholds, StorageThresholds):
            raise TypeError("thresholds must be StorageThresholds")
        if not callable(observer):
            raise TypeError("observer must be callable")
        if not 1 <= maximum_observation_failures <= 60:
            raise ValueError("maximum_observation_failures must be between 1 and 60")
        if not isinstance(reclaimer_available, bool):
            raise TypeError("reclaimer_available must be boolean")
        resolved = thresholds.resolve(expected_capacity_bytes)
        self._volume_uuid = volume_uuid
        self._expected_device_id = expected_device_id
        self._expected_capacity_bytes = expected_capacity_bytes
        self._observer = observer
        self._latch_store = latch_store
        self._maximum_observation_failures = maximum_observation_failures
        self._reclaimer_available = reclaimer_available
        self._monotonic_ns = monotonic_ns
        self._resolved = resolved
        self._lock = RLock()
        self._controller: StorageThresholdController | None = None
        self._sequence = 0
        self._observation_failures = 0
        self._latched_fault: SpaceObservationFault | None = None
        self._last_success_ns: int | None = None
        self._last = self._snapshot(
            mode=None,
            fault=None,
            trigger=None,
            stale=True,
            stop_required=False,
            free_bytes=None,
            directive=None,
        )

    @property
    def snapshot(self) -> StorageSpaceSnapshot:
        with self._lock:
            sampled = self._last_success_ns
            if sampled is None:
                return self._last
            return replace(
                self._last,
                sample_age_ns=max(self._monotonic_ns() - sampled, 0),
            )

    @property
    def maximum_observation_failures(self) -> int:
        return self._maximum_observation_failures

    def observe(self) -> StorageSpaceSnapshot:
        """Contain bounded failures and publish only durable policy state."""

        with self._lock:
            return self._observe_locked()

    def _observe_locked(self) -> StorageSpaceSnapshot:

        if self._latched_fault is not None:
            return self._publish_fault(self._latched_fault, immediate_stop=True)
        try:
            observed = self._observer()
        except Exception:
            return self._observation_failure(SpaceObservationFault.OBSERVATION_FAILED)
        if not isinstance(observed, FilesystemSpaceObservation) or not _valid(observed):
            return self._observation_failure(SpaceObservationFault.INVALID_OBSERVATION)
        if observed.device_id != self._expected_device_id:
            return self._latch_fault(SpaceObservationFault.IDENTITY_DRIFT)
        if observed.capacity_bytes != self._expected_capacity_bytes:
            return self._latch_fault(SpaceObservationFault.CAPACITY_DRIFT)
        self._observation_failures = 0
        self._last_success_ns = self._monotonic_ns()

        controller = self._controller
        if controller is None:
            try:
                latch = self._latch_store.retention_threshold_latch()
            except Exception:
                return self._latch_fault(SpaceObservationFault.LATCH_LOAD_FAILED)
            if latch is None:
                controller = StorageThresholdController(self._resolved)
            else:
                if not self._matches(latch):
                    return self._latch_fault(SpaceObservationFault.LATCH_BINDING_MISMATCH)
                controller = StorageThresholdController(
                    self._resolved,
                    mode=(
                        RetentionMode.RECLAIMING
                        if latch.reclaim_latched
                        else RetentionMode.NORMAL
                    ),
                )
            self._controller = controller

        previous = controller.mode
        mode = controller.evaluate(free_bytes=observed.free_bytes)
        if mode is not previous and not self._store(mode):
            controller.mode = previous
            return self._latch_fault(SpaceObservationFault.LATCH_STORE_FAILED)
        trigger = None if mode is RetentionMode.NORMAL else self._last.trigger
        directive = _directive(mode, observed.free_bytes, self._resolved.stop_deletion_at_bytes)
        stop_required = mode is RetentionMode.EMERGENCY and not self._reclaimer_available
        return self._publish(
            mode=mode,
            fault=None,
            trigger=trigger,
            stale=False,
            stop_required=stop_required,
            free_bytes=observed.free_bytes,
            directive=directive,
        )

    def note_write_error(self, error: BaseException) -> bool:
        """Persist ENOSPC emergency before requesting bounded runtime stop."""

        with self._lock:
            return self._note_write_error_locked(error)

    def _note_write_error_locked(self, error: BaseException) -> bool:

        if not _contains_enospc(error):
            return False
        self._note_no_space_write_locked()
        return True

    def note_no_space_write(self) -> StorageSpaceSnapshot:
        """Explicit adapter seam for a classified equivalent no-space error."""

        with self._lock:
            return self._note_no_space_write_locked()

    def _note_no_space_write_locked(self) -> StorageSpaceSnapshot:

        controller = self._controller
        if controller is None:
            controller = StorageThresholdController(self._resolved)
        previous = controller.mode
        controller.evaluate(
            free_bytes=0 if self._last.free_bytes is None else self._last.free_bytes,
            no_space_write=True,
        )
        if not self._store(controller.mode):
            controller.mode = previous
            self._latch_fault(SpaceObservationFault.LATCH_STORE_FAILED)
            return self._last
        self._controller = controller
        free_bytes = self._last.free_bytes
        self._last = self._publish(
            mode=RetentionMode.EMERGENCY,
            fault=SpaceObservationFault.NO_SPACE_WRITE,
            trigger="NO_SPACE_WRITE",
            stale=free_bytes is None,
            stop_required=True,
            free_bytes=free_bytes,
            directive=_directive(
                RetentionMode.EMERGENCY,
                free_bytes,
                self._resolved.stop_deletion_at_bytes,
            ),
        )
        return self._last

    def _matches(self, latch: RetentionThresholdLatch) -> bool:
        return latch == self._latch(latch.reclaim_latched)

    def _latch(self, reclaim_latched: bool) -> RetentionThresholdLatch:
        return RetentionThresholdLatch(
            volume_uuid=self._volume_uuid,
            capacity_bytes=self._expected_capacity_bytes,
            reclaim_latched=reclaim_latched,
        )

    def _store(self, mode: RetentionMode) -> bool:
        try:
            self._latch_store.store_retention_threshold_latch(
                self._latch(mode is not RetentionMode.NORMAL)
            )
        except Exception:
            return False
        return True

    def _observation_failure(self, fault: SpaceObservationFault) -> StorageSpaceSnapshot:
        self._observation_failures += 1
        expired = self._observation_failures >= self._maximum_observation_failures
        return self._publish_fault(
            SpaceObservationFault.OBSERVATION_STALE if expired else fault,
            immediate_stop=expired,
        )

    def _latch_fault(self, fault: SpaceObservationFault) -> StorageSpaceSnapshot:
        self._latched_fault = fault
        return self._publish_fault(fault, immediate_stop=True)

    def _publish_fault(
        self,
        fault: SpaceObservationFault,
        *,
        immediate_stop: bool,
    ) -> StorageSpaceSnapshot:
        return self._publish(
            mode=None if self._controller is None else self._controller.mode,
            fault=fault,
            trigger=self._last.trigger,
            stale=True,
            stop_required=immediate_stop,
            free_bytes=self._last.free_bytes,
            directive=None,
        )

    def _snapshot(self, **values: object) -> StorageSpaceSnapshot:
        free_bytes = values.get("free_bytes")
        if free_bytes is not None and not isinstance(free_bytes, int):
            raise TypeError("free_bytes must be an integer or null")
        free_percent = (
            None
            if free_bytes is None
            else free_bytes * 100.0 / self._expected_capacity_bytes
        )
        return StorageSpaceSnapshot(
            sequence=self._sequence,
            consecutive_observation_failures=self._observation_failures,
            sample_age_ns=None,
            reclaimer_enabled=self._reclaimer_available,
            volume_uuid=self._volume_uuid,
            device_id=self._expected_device_id,
            capacity_bytes=self._expected_capacity_bytes,
            start_deletion_below_bytes=self._resolved.start_deletion_below_bytes,
            stop_deletion_at_bytes=self._resolved.stop_deletion_at_bytes,
            emergency_below_bytes=self._resolved.emergency_below_bytes,
            free_percent=free_percent,
            **values,  # type: ignore[arg-type]
        )

    def _publish(self, **values: object) -> StorageSpaceSnapshot:
        self._sequence += 1
        self._last = self._snapshot(**values)
        return self._last


def build_storage_space_monitor(
    *,
    storage: StorageConfig,
    volume_uuid: str,
    expected_device_id: str,
    expected_capacity_bytes: int,
    latch_store: RetentionLatchStore,
    observer_factory: Callable[[Path], SpaceObserver] = lambda root: LinuxSpaceObserver(root),
) -> StorageSpaceMonitor:
    """Build a monitor from fresh READY preflight evidence."""

    return StorageSpaceMonitor(
        volume_uuid=volume_uuid,
        expected_device_id=expected_device_id,
        expected_capacity_bytes=expected_capacity_bytes,
        thresholds=StorageThresholds(
            storage.low_watermark_percent,
            storage.high_watermark_percent,
            math.ceil(storage.minimum_free_gib * _GIB),
            storage.emergency_free_mib * _MIB,
        ),
        observer=observer_factory(Path(storage.recording_root)),
        latch_store=latch_store,
    )


class LinuxSpaceObserver:
    """Read identity and statvfs from one no-follow directory descriptor."""

    def __init__(self, root: Path) -> None:
        if not root.as_posix().startswith("/") or ".." in root.parts:
            raise ValueError("recording root must be an absolute normalized path")
        self._root = root

    def __call__(self) -> FilesystemSpaceObservation:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(self._root, flags)
        try:
            root_stat = os.fstat(descriptor)
            if not stat.S_ISDIR(root_stat.st_mode):
                raise OSError("recording root descriptor is not a directory")
            fstatvfs = cast(Callable[[int], _StatVfsResult], vars(os)["fstatvfs"])
            space = fstatvfs(descriptor)
        finally:
            os.close(descriptor)
        major = cast(Callable[[int], int], vars(os)["major"])
        minor = cast(Callable[[int], int], vars(os)["minor"])
        return FilesystemSpaceObservation(
            device_id=f"{major(root_stat.st_dev)}:{minor(root_stat.st_dev)}",
            capacity_bytes=space.f_blocks * space.f_frsize,
            free_bytes=space.f_bavail * space.f_frsize,
        )


def _directive(
    mode: RetentionMode,
    free_bytes: int | None,
    target: int,
) -> RetentionDirective | None:
    if mode is RetentionMode.NORMAL:
        return None
    return RetentionDirective(
        mode=mode,
        target_free_bytes=target,
        requested_reclaim_bytes=0 if free_bytes is None else max(target - free_bytes, 0),
        emergency=mode is RetentionMode.EMERGENCY,
    )


def _valid(value: FilesystemSpaceObservation) -> bool:
    try:
        _validate_device_id(value.device_id)
        _positive_integer(value.capacity_bytes, "capacity_bytes")
        _non_negative_integer(value.free_bytes, "free_bytes")
    except (TypeError, ValueError):
        return False
    return value.free_bytes <= value.capacity_bytes


def _contains_enospc(error: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = error
    for _ in range(8):
        if current is None or id(current) in seen:
            return False
        seen.add(id(current))
        if isinstance(current, OSError) and current.errno in {errno.ENOSPC, errno.EDQUOT}:
            return True
        current = current.__cause__ or current.__context__
    return False


def _bounded_identity(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128 or not value.isprintable():
        raise ValueError(f"{name} must be a bounded printable string")
    return value


def _validate_device_id(value: object) -> str:
    text = _bounded_identity(value, "device_id")
    major, separator, minor = text.partition(":")
    if separator != ":" or not major.isdecimal() or not minor.isdecimal():
        raise ValueError("device_id must be major:minor")
    if int(major) > 2**32 - 1 or int(minor) > 2**32 - 1:
        raise ValueError("device_id is out of range")
    return text


def _positive_integer(value: object, name: str) -> int:
    result = _non_negative_integer(value, name)
    if result == 0:
        raise ValueError(f"{name} must be positive")
    return result


def _non_negative_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if not 0 <= value <= _MAX_BYTES:
        raise ValueError(f"{name} is out of range")
    return value


__all__ = [
    "FilesystemSpaceObservation",
    "LinuxSpaceObserver",
    "RetentionDirective",
    "RetentionLatchStore",
    "SpaceObservationFault",
    "StorageSpaceMonitor",
    "StorageSpaceSnapshot",
    "build_storage_space_monitor",
]
