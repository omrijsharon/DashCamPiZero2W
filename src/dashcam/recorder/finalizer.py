"""Durable, bounded promotion of one closed pending MP4 and its JSON sidecar.

The recorder runtime uses the catalog protocol below to atomically register a
closed provisional pair and its explicit pending-to-clips ``FINALIZE`` intent
in the ext4 catalog before either exFAT member moves.
"""

from __future__ import annotations

import ctypes
import errno
import os
import stat
import tempfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Final, Protocol
from uuid import UUID

from dashcam.catalog.database import (
    ActiveClipProtectionChanged,
    ActiveClipProtectionState,
    RetentionThresholdLatch,
)
from dashcam.catalog.filesystem import CatalogFilesystem, RootedFilesystem
from dashcam.catalog.models import (
    CatalogClip,
    ClipNotFoundError,
    EventProtectionResult,
    EventSource,
    IntentReconciliationResult,
)
from dashcam.metadata.coordinator import (
    ClipMetadataCoordinator,
    MetadataCoordinatorOutcome,
)
from dashcam.metadata.reconcile import (
    MetadataReconciliationPlan,
    SidecarParseError,
    parse_sidecar_bytes,
)
from dashcam.metadata.schema import ClipSidecar, TimeAnchor
from dashcam.state import ClipLifecycle, GpsTimeState, SystemClockState
from dashcam.storage.intents import IntentKind, OperationIntent, PairPaths
from dashcam.storage.naming import ClipNameError, parse_clip_filename
from dashcam.storage.reclaimer import ReclamationStep, StorageReclaimer

MAX_SIDECAR_BYTES: Final = 1_048_576
_MAX_DIRECTORY_ENTRIES: Final = 100_000
_MAX_PENDING_INTENTS: Final = 1_024
_MAX_MONOTONIC_NS: Final = 9_223_372_036_854_775_807
_AT_FDCWD: Final = -100
_RENAME_NOREPLACE: Final = 1
_RECOVERY_INTENT_KINDS: Final = (
    IntentKind.FINALIZE,
    IntentKind.RECONCILE_NAME,
    IntentKind.PROTECT,
    IntentKind.UNPROTECT,
)


class FinalizationError(RuntimeError):
    """Base error for a refused or failed clip finalization."""


class FinalizationRefused(FinalizationError):
    """The observed state is unsafe or ambiguous and was left untouched."""


class PairPromotionCatalog(Protocol):
    """Minimal durable catalog seam required by the finalizer.

    ``register_finalizing_clip`` must commit ``clip`` and a ``FINALIZE`` intent
    containing exactly ``promotion_paths`` in one synchronous ext4 transaction.
    It must never begin an exFAT move itself.
    """

    def register_finalizing_clip(
        self,
        clip: CatalogClip,
        *,
        promotion_paths: PairPaths,
        monotonic_now_ns: int,
        expected_protection_revision: int | None = None,
    ) -> UUID: ...

    def register_writing_clip(
        self,
        clip: CatalogClip,
        *,
        monotonic_now_ns: int,
    ) -> None: ...

    def active_closing_protection(
        self,
        clip_id: UUID,
        *,
        monotonic_now_ns: int,
    ) -> ActiveClipProtectionState: ...

    def trigger_event(
        self,
        current_clip_id: UUID | None,
        *,
        source: EventSource,
        monotonic_now_ns: int,
        previous_count: int = 2,
        next_count: int = 1,
        event_id: UUID | None = None,
        require_current_writing: bool = False,
    ) -> EventProtectionResult: ...

    def list_writing_clips(self, *, limit: int) -> tuple[CatalogClip, ...]: ...

    def mark_writing_clip_orphaned(
        self,
        clip_id: UUID,
        *,
        monotonic_now_ns: int,
    ) -> bool: ...

    def get_clip(self, clip_id: UUID) -> CatalogClip: ...

    def next_retention_order(self) -> int: ...

    def retention_threshold_latch(self) -> RetentionThresholdLatch | None: ...

    def store_retention_threshold_latch(self, latch: RetentionThresholdLatch) -> None: ...

    def list_metadata_reconciliation_candidates(
        self,
        expected_boot_id: UUID,
        *,
        limit: int,
        after_order: int = -1,
        after_clip_id: UUID | None = None,
    ) -> tuple[CatalogClip, ...]: ...

    def list_pending_intents(self, *, limit: int) -> tuple[OperationIntent, ...]: ...

    def list_pending_delete_intents(self, *, limit: int) -> tuple[OperationIntent, ...]: ...

    def list_pending_intents_by_kind(
        self,
        *,
        kinds: tuple[IntentKind, ...],
        limit: int,
    ) -> tuple[OperationIntent, ...]: ...

    def get_pending_intent(self, intent_id: UUID) -> OperationIntent | None: ...

    def list_pending_intents_for_clip(
        self,
        clip_id: UUID,
        *,
        kinds: tuple[IntentKind, ...],
        limit: int,
    ) -> tuple[OperationIntent, ...]: ...

    def prepare_oldest_eligible_delete(
        self,
        *,
        monotonic_now_ns: int,
        boot_id: str,
    ) -> UUID | None: ...

    def reconcile_intent(
        self,
        intent_id: UUID,
        filesystem: CatalogFilesystem,
        *,
        monotonic_now_ns: int,
        max_actions: int = 2,
    ) -> IntentReconciliationResult: ...

    def register_name_reconciliation(
        self,
        plan: MetadataReconciliationPlan,
        *,
        source_sidecar: ClipSidecar,
        monotonic_now_ns: int,
    ) -> UUID: ...


class FinalizationFilesystem(CatalogFilesystem, Protocol):
    """Filesystem operations needed before and during pair promotion."""

    def sync_file(self, relative_path: str) -> None: ...

    def write_staged_sidecar(self, relative_path: str, sidecar: ClipSidecar) -> None: ...


@dataclass(frozen=True, slots=True)
class FinalizerLimits:
    """Hard work bounds for one finalization or recovery pass."""

    max_directory_entries: int = 4_096
    max_pending_intents: int = 64
    max_actions_per_intent: int = 2
    max_sidecar_bytes: int = MAX_SIDECAR_BYTES
    max_protection_retries: int = 3

    def __post_init__(self) -> None:
        for name, value, maximum in (
            ("max_directory_entries", self.max_directory_entries, _MAX_DIRECTORY_ENTRIES),
            ("max_pending_intents", self.max_pending_intents, _MAX_PENDING_INTENTS),
            ("max_actions_per_intent", self.max_actions_per_intent, 2),
            ("max_sidecar_bytes", self.max_sidecar_bytes, MAX_SIDECAR_BYTES),
            ("max_protection_retries", self.max_protection_retries, 8),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
                or value > maximum
            ):
                raise ValueError(f"{name} must be between 1 and {maximum}")


@dataclass(frozen=True, slots=True)
class FinalizationOutcome:
    """Result of one bounded finalization attempt."""

    clip_id: UUID
    intent_id: UUID | None
    actions_attempted: int
    complete: bool
    resumed: bool


@dataclass(frozen=True, slots=True)
class FinalizationRecoveryReport:
    """Summary of one bounded pass over recorder-managed durable intents."""

    intents_examined: int
    actions_attempted: int
    completed: int
    more_work: bool


@dataclass(frozen=True, slots=True)
class MetadataReconciliationCandidate:
    """Stable catalog cursor data for one provisional finalized clip."""

    clip_id: UUID
    retention_order: int

    def __post_init__(self) -> None:
        if not isinstance(self.clip_id, UUID):
            raise TypeError("clip_id must be a UUID")
        _non_negative(self.retention_order, "retention_order")


class DurableRootedFinalizationFilesystem(RootedFilesystem):
    """Root-confined filesystem with durable sidecars and no-replace moves."""

    def __init__(self, root: Path, *, expected_device_id: str | None = None) -> None:
        super().__init__(root, expected_device_id=expected_device_id)

    def exists(self, relative_path: str) -> bool:
        path = self._safe_path(relative_path)
        try:
            information = os.lstat(path)
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(information.st_mode):
            raise FinalizationRefused("managed file is a symbolic link")
        if not stat.S_ISREG(information.st_mode):
            raise FinalizationRefused("managed path is not a regular file")
        return True

    def move(self, source: str, target: str) -> None:
        source_path = self._safe_path(source)
        target_path = self._safe_path(target)
        if not self.exists(source):
            raise FileNotFoundError(source_path)
        if target_path.exists():
            raise FileExistsError(target_path)
        _rename_noreplace(source_path, target_path)
        _fsync_directory(source_path.parent)
        if target_path.parent != source_path.parent:
            _fsync_directory(target_path.parent)

    def unlink(self, relative_path: str) -> None:
        path = self._safe_path(relative_path)
        if os.name == "nt":
            try:
                path.unlink()
            except FileNotFoundError:
                return
            _fsync_directory(path.parent)
            return
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        directory_descriptor = os.open(path.parent, flags)
        try:
            root_device = os.lstat(self.root).st_dev
            directory_information = os.fstat(directory_descriptor)
            if (
                not stat.S_ISDIR(directory_information.st_mode)
                or directory_information.st_dev != root_device
            ):
                raise FinalizationRefused("managed directory identity changed before unlink")
            try:
                member = os.stat(
                    path.name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                os.fsync(directory_descriptor)
                return
            if not stat.S_ISREG(member.st_mode) or member.st_dev != root_device:
                raise FinalizationRefused("managed unlink target is not a bound regular file")
            os.unlink(path.name, dir_fd=directory_descriptor)
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)

    def iter_files(self, directory: str, *, limit: int) -> tuple[tuple[str, ...], int, bool]:
        if directory not in {"pending", "clips", "protected", "quarantine"}:
            raise ValueError("directory is outside the managed namespace")
        _positive_bound(limit, "limit")
        directory_path = self._safe_directory(directory)
        paths: list[str] = []
        examined = 0
        truncated = False
        with os.scandir(directory_path) as entries:
            for entry in entries:
                if examined == limit:
                    truncated = True
                    break
                examined += 1
                if entry.is_file(follow_symlinks=False):
                    paths.append(PurePosixPath(directory, entry.name).as_posix())
        paths.sort(key=str.casefold)
        return tuple(paths), examined, truncated

    def read_bytes(self, relative_path: str, *, maximum_bytes: int) -> bytes:
        _positive_bound(maximum_bytes, "maximum_bytes")
        path = self._safe_path(relative_path)
        if not self.exists(relative_path):
            raise FileNotFoundError(path)
        if path.stat().st_size > maximum_bytes:
            raise ValueError("file exceeds recovery size bound")
        with path.open("rb") as stream:
            payload = stream.read(maximum_bytes + 1)
        if len(payload) > maximum_bytes:
            raise ValueError("file exceeds recovery size bound")
        return payload

    def file_size(self, relative_path: str) -> int:
        path = self._safe_path(relative_path)
        if not self.exists(relative_path):
            raise FileNotFoundError(path)
        return path.stat().st_size

    def sync_file(self, relative_path: str) -> None:
        path = self._safe_path(relative_path)
        if os.name == "nt":
            # Windows' CRT rejects fsync on a read-only descriptor.
            with path.open("rb+") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise FinalizationRefused("closed MP4 is not a regular file")
                os.fsync(stream.fileno())
            return
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise FinalizationRefused("closed MP4 is not a regular file")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def write_staged_sidecar(self, relative_path: str, sidecar: ClipSidecar) -> None:
        """Atomically stage canonical bytes under the provisional pair name."""

        path = self._safe_path(relative_path)
        if path.exists():
            raise FileExistsError(path)
        descriptor = -1
        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.",
                suffix=".tmp",
                dir=path.parent,
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(sidecar.to_canonical_json())
                stream.flush()
                os.fsync(stream.fileno())
            _rename_noreplace(temporary_path, path)
            temporary_path = None
            _fsync_directory(path.parent)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_path is not None:
                with suppress(FileNotFoundError):
                    temporary_path.unlink()

    def _safe_path(self, relative_path: str) -> Path:
        # PairPaths remains the repository's closed portable-path validator.
        PairPaths(relative_path, "__validation__.json")
        parts = PurePosixPath(relative_path).parts
        if len(parts) != 2 or parts[0] not in {
            "pending",
            "clips",
            "protected",
            "quarantine",
        }:
            raise FinalizationRefused("path is outside a managed leaf directory")
        directory = self._safe_directory(parts[0])
        path = directory / parts[1]
        try:
            information = os.lstat(path)
            if stat.S_ISLNK(information.st_mode):
                raise FinalizationRefused("managed file is a symbolic link")
            if information.st_dev != os.lstat(self.root).st_dev:
                raise FinalizationRefused("managed file is on a different device")
        except FileNotFoundError:
            pass
        return path

    def _safe_directory(self, name: str) -> Path:
        self._assert_bound()
        directory = self.root / name
        directory_information = os.lstat(directory)
        if stat.S_ISLNK(directory_information.st_mode) or not stat.S_ISDIR(
            directory_information.st_mode
        ):
            raise FinalizationRefused("managed directory is not a real directory")
        if directory_information.st_dev != os.lstat(self.root).st_dev:
            raise FinalizationRefused("managed directory is on a different device")
        if directory.resolve(strict=True).parent != self.root:
            raise FinalizationRefused("managed directory escapes recording root")
        return directory


class RecorderClipFinalizer:
    """Create one canonical sidecar and promote its logical pair recoverably."""

    def __init__(
        self,
        *,
        catalog: PairPromotionCatalog,
        filesystem: FinalizationFilesystem,
        monotonic_ns: Callable[[], int],
        limits: FinalizerLimits | None = None,
    ) -> None:
        self._catalog = catalog
        self._filesystem = filesystem
        self._monotonic_ns = monotonic_ns
        self._limits = limits or FinalizerLimits()
        self._metadata_coordinator = ClipMetadataCoordinator(
            catalog=catalog,
            filesystem=filesystem,
            monotonic_ns=monotonic_ns,
        )
        self._storage_reclaimer = StorageReclaimer(
            catalog=catalog,
            filesystem=filesystem,
            monotonic_ns=monotonic_ns,
        )

    def register_active_clip(
        self,
        *,
        provisional_video_name: str,
        clip_id: UUID,
        start_monotonic_ns: int,
        retention_order: int,
    ) -> None:
        """Persist an opened fragment's stable identity before it is exposed."""

        source = _promotion_source(provisional_video_name)
        parsed = parse_clip_filename(PurePosixPath(source).name)
        if not parsed.partial:
            raise FinalizationRefused("active fragment must use a provisional filename")
        _non_negative(start_monotonic_ns, "start_monotonic_ns")
        _non_negative(retention_order, "retention_order")
        sidecar_source = source.removesuffix(".mp4") + ".json"
        clip = CatalogClip(
            clip_id=clip_id,
            lifecycle=ClipLifecycle.WRITING,
            video_path=source,
            sidecar_path=sidecar_source,
            start_monotonic_ns=start_monotonic_ns,
            end_monotonic_ns=None,
            retention_order=retention_order,
            size_bytes=0,
            protected=False,
            protection_reason=None,
            pair_reconciled=False,
            managed=True,
        )
        self._catalog.register_writing_clip(clip, monotonic_now_ns=self._now())

    def reconcile_orphaned_writing(self, *, limit: int) -> tuple[int, bool]:
        """Boundedly demote prior-process WRITING rows without deleting partials."""

        _positive_bound(limit, "limit")
        values = self._catalog.list_writing_clips(limit=limit + 1)
        for clip in values[:limit]:
            self._catalog.mark_writing_clip_orphaned(
                clip.clip_id,
                monotonic_now_ns=self._now(),
            )
        return min(len(values), limit), len(values) > limit

    def finalize(
        self,
        *,
        provisional_video_name: str,
        sidecar: ClipSidecar,
        retention_order: int,
    ) -> FinalizationOutcome:
        """Finalize one explicitly identified closed MP4 without scanning imports."""

        paths = _promotion_paths(provisional_video_name, sidecar)
        _non_negative(retention_order, "retention_order")
        expected_payload = sidecar.to_canonical_json()

        pending, truncated = self._pending_intents()
        matching = tuple(intent for intent in pending if intent.clip_id == sidecar.clip_id)
        if len(matching) > 1:
            raise FinalizationRefused("multiple pending intents exist for one clip")
        if matching:
            intent = matching[0]
            self._validate_intent(intent, paths)
            self._validate_durable_sidecar(intent, expected_payload=expected_payload)
            return self._reconcile(intent, resumed=True)
        if truncated:
            raise FinalizationRefused("pending intent scan bound prevents unique registration")

        existing = self._get_clip_if_present(sidecar.clip_id)
        active = existing is not None and existing.lifecycle is ClipLifecycle.WRITING
        if existing is not None and not active:
            return self._completed_replay(existing, paths, expected_payload)

        pending_names = self._directory_names("pending")
        self._require_unique_exact(pending_names, paths.video_source)
        self._require_absent_casefold(
            pending_names,
            paths.sidecar_source,
            allow_exact=True,
        )
        for directory in ("clips", "protected"):
            names = self._directory_names(directory)
            self._require_absent_casefold(names, paths.video_target)
            self._require_absent_casefold(names, paths.sidecar_target)

        if not self._filesystem.exists(paths.video_source):
            raise FinalizationRefused("closed provisional MP4 is missing")
        size_bytes = self._filesystem.file_size(paths.video_source)
        if size_bytes <= 0:
            raise FinalizationRefused("closed provisional MP4 is empty")
        self._filesystem.sync_file(paths.video_source)
        attempts = self._limits.max_protection_retries if active else 1
        intent_id: UUID | None = None
        effective_sidecar = sidecar
        for _ in range(attempts):
            protection: ActiveClipProtectionState | None = None
            if active:
                protection = self._catalog.active_closing_protection(
                    sidecar.clip_id,
                    monotonic_now_ns=self._now(),
                )
                effective_sidecar = replace(
                    sidecar,
                    protected=protection.protected,
                    protection_reason=protection.reason,
                )
            expected_payload = effective_sidecar.to_canonical_json()
            self._ensure_pending_sidecar(
                paths.sidecar_source,
                effective_sidecar,
                expected_payload,
                replace_existing=active,
            )
            clip = CatalogClip(
                clip_id=effective_sidecar.clip_id,
                lifecycle=ClipLifecycle.FINALIZING,
                video_path=paths.video_source,
                sidecar_path=paths.sidecar_source,
                start_monotonic_ns=effective_sidecar.start_monotonic_ns,
                end_monotonic_ns=effective_sidecar.end_monotonic_ns,
                retention_order=retention_order,
                size_bytes=size_bytes,
                protected=effective_sidecar.protected,
                protection_reason=effective_sidecar.protection_reason,
                pair_reconciled=False,
                managed=True,
            )
            try:
                intent_id = self._catalog.register_finalizing_clip(
                    clip,
                    promotion_paths=paths,
                    monotonic_now_ns=self._now(),
                    expected_protection_revision=(
                        None if protection is None else protection.revision
                    ),
                )
            except ActiveClipProtectionChanged:
                continue
            break
        if intent_id is None:
            raise FinalizationRefused(
                "active clip protection did not stabilize within its retry bound"
            )
        intent = self._find_registered_intent(intent_id, sidecar.clip_id, paths)
        outcome = self._reconcile(intent, resumed=False)
        if outcome.complete and effective_sidecar.protected:
            return self._reconcile_clip_followups(outcome)
        return outcome

    def execute_intent(self, intent_id: UUID) -> FinalizationOutcome:
        """Execute one recorder-authorized durable pair intent and its compensation."""

        intent = self._catalog.get_pending_intent(intent_id)
        if intent is None:
            raise FinalizationRefused("control intent is not pending")
        outcome = self._reconcile(intent, resumed=True)
        return self._reconcile_clip_followups(outcome) if outcome.complete else outcome

    def trigger_event(
        self,
        current_clip_id: UUID | None,
        *,
        source: EventSource,
        monotonic_now_ns: int,
        previous_count: int,
        next_count: int,
        event_id: UUID,
    ) -> EventProtectionResult:
        """Commit one idempotent event and converge its immediately queued moves."""

        event = self._catalog.trigger_event(
            current_clip_id,
            source=source,
            monotonic_now_ns=monotonic_now_ns,
            previous_count=previous_count,
            next_count=next_count,
            event_id=event_id,
            require_current_writing=True,
        )
        for intent_id in event.queued_intent_ids:
            outcome = self.execute_intent(intent_id)
            if not outcome.complete:
                raise FinalizationRefused("event protection intent did not converge")
        return event

    def _reconcile_clip_followups(
        self,
        initial: FinalizationOutcome,
    ) -> FinalizationOutcome:
        actions = initial.actions_attempted
        for _ in range(2):
            values = self._catalog.list_pending_intents_for_clip(
                initial.clip_id,
                kinds=(IntentKind.PROTECT, IntentKind.UNPROTECT),
                limit=2,
            )
            if not values:
                return replace(initial, actions_attempted=actions)
            if len(values) != 1:
                raise FinalizationRefused("clip has ambiguous protection follow-up intents")
            followup = self._reconcile(values[0], resumed=True)
            actions += followup.actions_attempted
            if not followup.complete:
                return replace(initial, actions_attempted=actions, complete=False)
        raise FinalizationRefused("clip protection compensation exceeded its bound")

    def video_size(self, provisional_video_name: str) -> int:
        """Return the bounded regular-file size used to build honest metadata."""

        source = _promotion_source(provisional_video_name)
        if not self._filesystem.exists(source):
            raise FinalizationRefused("closed provisional MP4 is missing")
        size = self._filesystem.file_size(source)
        if size <= 0:
            raise FinalizationRefused("closed provisional MP4 is empty")
        return size

    def next_retention_order(self) -> int:
        return self._catalog.next_retention_order()

    def retention_threshold_latch(self) -> RetentionThresholdLatch | None:
        """Load the volume-bound threshold latch without touching clip rows."""

        return self._catalog.retention_threshold_latch()

    def store_retention_threshold_latch(self, latch: RetentionThresholdLatch) -> None:
        """Persist threshold hysteresis without touching clip rows."""

        self._catalog.store_retention_threshold_latch(latch)

    def reclaim_storage_once(self, *, boot_id: str, allow_new: bool) -> ReclamationStep:
        """Replay or delete one pair; the caller must reobserve before repeating."""

        return self._storage_reclaimer.run_one(boot_id=boot_id, allow_new=allow_new)

    def metadata_reconciliation_candidates(
        self,
        expected_boot_id: UUID,
        *,
        limit: int,
        after_order: int = -1,
        after_clip_id: UUID | None = None,
    ) -> tuple[MetadataReconciliationCandidate, ...]:
        """Read one bounded, stable page from the durable catalog."""

        clips = self._catalog.list_metadata_reconciliation_candidates(
            expected_boot_id,
            limit=limit,
            after_order=after_order,
            after_clip_id=after_clip_id,
        )
        return tuple(
            MetadataReconciliationCandidate(clip.clip_id, clip.retention_order)
            for clip in clips
        )

    def reconcile_metadata(
        self,
        clip_id: UUID,
        *,
        anchor: TimeAnchor,
        expected_boot_id: UUID,
        gps_time_state: GpsTimeState,
        system_clock_state: SystemClockState,
    ) -> MetadataCoordinatorOutcome:
        """Project one trusted anchor through the durable pair state machine."""

        return self._metadata_coordinator.reconcile_clip(
            clip_id,
            anchor=anchor,
            expected_boot_id=expected_boot_id,
            gps_time_state=gps_time_state,
            system_clock_state=system_clock_state,
        )

    def reconcile_pending(self) -> FinalizationRecoveryReport:
        """Reconcile durable media/protection pair intents within hard bounds."""

        intents, truncated = self._pending_intents()
        examined = 0
        actions = 0
        completed = 0
        incomplete = False
        for intent in intents:
            examined += 1
            if intent.kind is IntentKind.FINALIZE:
                self._validate_durable_sidecar(intent)
            outcome = self._reconcile(intent, resumed=True)
            if intent.kind is IntentKind.RECONCILE_NAME:
                self._validate_durable_sidecar(intent)
            actions += outcome.actions_attempted
            completed += int(outcome.complete)
            incomplete = incomplete or not outcome.complete
        return FinalizationRecoveryReport(
            intents_examined=examined,
            actions_attempted=actions,
            completed=completed,
            more_work=truncated or incomplete or self._has_pending_recovery_intent(),
        )

    def _reconcile(self, intent: OperationIntent, *, resumed: bool) -> FinalizationOutcome:
        if intent.kind not in _RECOVERY_INTENT_KINDS:
            raise FinalizationRefused(
                "finalizer cannot execute this pair-intent kind"
            )
        result = self._catalog.reconcile_intent(
            intent.intent_id,
            self._filesystem,
            monotonic_now_ns=self._now(),
            max_actions=self._limits.max_actions_per_intent,
        )
        if result.intent != intent:
            raise FinalizationRefused("catalog returned a different reconciliation intent")
        if result.problems:
            raise FinalizationRefused("pair promotion is ambiguous: " + ",".join(result.problems))
        return FinalizationOutcome(
            clip_id=intent.clip_id,
            intent_id=intent.intent_id,
            actions_attempted=result.actions_attempted,
            complete=result.complete,
            resumed=resumed,
        )

    def _pending_intents(self) -> tuple[tuple[OperationIntent, ...], bool]:
        limit = self._limits.max_pending_intents
        values = self._catalog.list_pending_intents_by_kind(
            kinds=_RECOVERY_INTENT_KINDS,
            limit=limit + 1,
        )
        return values[:limit], len(values) > limit

    def _has_pending_recovery_intent(self) -> bool:
        return bool(
            self._catalog.list_pending_intents_by_kind(
                kinds=_RECOVERY_INTENT_KINDS,
                limit=1,
            )
        )

    def _find_registered_intent(
        self, intent_id: UUID, clip_id: UUID, paths: PairPaths
    ) -> OperationIntent:
        intent = self._catalog.get_pending_intent(intent_id)
        if intent is None:
            raise FinalizationRefused("catalog did not expose the committed intent")
        if intent.clip_id != clip_id:
            raise FinalizationRefused("registered intent changed clip identity")
        self._validate_intent(intent, paths)
        return intent

    def _validate_intent(self, intent: OperationIntent, paths: PairPaths) -> None:
        if intent.kind is not IntentKind.FINALIZE or intent.paths != paths:
            raise FinalizationRefused("existing intent does not match requested promotion")

    def _validate_durable_sidecar(
        self,
        intent: OperationIntent,
        *,
        expected_payload: bytes | None = None,
    ) -> None:
        target = _required_target(intent.paths.sidecar_target)
        locations = tuple(
            path for path in (intent.paths.sidecar_source, target) if self._filesystem.exists(path)
        )
        if not locations:
            return
        for location in locations:
            try:
                payload = self._filesystem.read_bytes(
                    location,
                    maximum_bytes=self._limits.max_sidecar_bytes,
                )
                sidecar = parse_sidecar_bytes(payload)
            except (OSError, SidecarParseError, ValueError) as exc:
                raise FinalizationRefused("durable sidecar is not canonical bounded JSON") from exc
            if (
                sidecar.clip_id != intent.clip_id
                or sidecar.video_file
                != PurePosixPath(_required_target(intent.paths.video_target)).name
                or sidecar.metadata_file != PurePosixPath(target).name
            ):
                raise FinalizationRefused("durable sidecar identity differs from its intent")
            if expected_payload is not None and payload != expected_payload:
                raise FinalizationRefused("durable sidecar differs from requested metadata")

    def _ensure_pending_sidecar(
        self,
        relative_path: str,
        sidecar: ClipSidecar,
        expected_payload: bytes,
        *,
        replace_existing: bool = False,
    ) -> None:
        if self._filesystem.exists(relative_path):
            try:
                existing = self._filesystem.read_bytes(
                    relative_path,
                    maximum_bytes=self._limits.max_sidecar_bytes,
                )
                parsed = parse_sidecar_bytes(existing)
            except (OSError, SidecarParseError, ValueError) as exc:
                raise FinalizationRefused("existing pending sidecar is not canonical") from exc
            if existing != expected_payload or parsed != sidecar:
                if replace_existing:
                    self._filesystem.replace_bytes_atomic(
                        relative_path,
                        expected_payload,
                        maximum_bytes=self._limits.max_sidecar_bytes,
                    )
                    persisted = self._filesystem.read_bytes(
                        relative_path,
                        maximum_bytes=self._limits.max_sidecar_bytes,
                    )
                    if persisted != expected_payload:
                        raise FinalizationRefused(
                            "replacement pending sidecar failed canonical readback"
                        )
                    return
                raise FinalizationRefused("existing pending sidecar has different identity")
            return
        self._filesystem.write_staged_sidecar(relative_path, sidecar)
        persisted = self._filesystem.read_bytes(
            relative_path,
            maximum_bytes=self._limits.max_sidecar_bytes,
        )
        try:
            parsed = parse_sidecar_bytes(persisted)
        except SidecarParseError as exc:
            raise FinalizationRefused("new sidecar failed canonical readback") from exc
        if persisted != expected_payload or parsed != sidecar:
            raise FinalizationRefused("new sidecar failed identity readback")

    def _directory_names(self, directory: str) -> tuple[str, ...]:
        paths, _examined, truncated = self._filesystem.iter_files(
            directory,
            limit=self._limits.max_directory_entries,
        )
        if truncated:
            raise FinalizationRefused(f"{directory} directory exceeds collision scan bound")
        return tuple(PurePosixPath(path).name for path in paths)

    def _require_unique_exact(self, names: tuple[str, ...], path: str) -> None:
        expected = PurePosixPath(path).name
        matches = tuple(name for name in names if name.casefold() == expected.casefold())
        if matches != (expected,):
            raise FinalizationRefused("provisional MP4 identity is missing or case-ambiguous")

    def _require_absent_casefold(
        self,
        names: tuple[str, ...],
        path: str | None,
        *,
        allow_exact: bool = False,
    ) -> None:
        expected = PurePosixPath(_required_target(path)).name
        matches = tuple(name for name in names if name.casefold() == expected.casefold())
        if not matches or (allow_exact and matches == (expected,)):
            return
        raise FinalizationRefused("refusing case-insensitive filename collision")

    def _get_clip_if_present(self, clip_id: UUID) -> CatalogClip | None:
        try:
            return self._catalog.get_clip(clip_id)
        except ClipNotFoundError:
            return None

    def _completed_replay(
        self, clip: CatalogClip, paths: PairPaths, expected_payload: bytes
    ) -> FinalizationOutcome:
        if (
            clip.lifecycle is not ClipLifecycle.FINALIZED
            or not clip.pair_reconciled
            or clip.video_path != paths.video_target
            or clip.sidecar_path != paths.sidecar_target
            or not self._filesystem.exists(clip.video_path)
            or not self._filesystem.exists(clip.sidecar_path)
        ):
            raise FinalizationRefused("catalog already contains conflicting clip state")
        self._validate_durable_sidecar(
            OperationIntent(
                intent_id=UUID(int=0),
                clip_id=clip.clip_id,
                kind=IntentKind.FINALIZE,
                created_monotonic_ns=0,
                paths=paths,
            ),
            expected_payload=expected_payload,
        )
        return FinalizationOutcome(clip.clip_id, None, 0, True, True)

    def _now(self) -> int:
        value = self._monotonic_ns()
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= _MAX_MONOTONIC_NS
        ):
            raise FinalizationRefused("monotonic clock returned an invalid value")
        return value


def _promotion_paths(provisional_video_name: str, sidecar: ClipSidecar) -> PairPaths:
    if not isinstance(sidecar, ClipSidecar):
        raise TypeError("sidecar must be a ClipSidecar")
    try:
        source = parse_clip_filename(provisional_video_name)
        target = parse_clip_filename(sidecar.video_file)
    except ClipNameError as exc:
        raise FinalizationRefused("clip filename identity is invalid") from exc
    if not source.partial or source.extension != "mp4":
        raise FinalizationRefused("source must be one provisional .partial.mp4")
    if target.partial:
        raise FinalizationRefused("refusing to promote a .partial basename into clips")
    if source.boot_id != target.boot_id or source.sequence != target.sequence:
        raise FinalizationRefused("source and finalized target identities differ")
    if target.boot_id != sidecar.boot_id.hex[:12]:
        raise FinalizationRefused(
            "filename boot token does not match the sidecar boot UUID"
        )
    if sidecar.metadata_file != sidecar.video_file.removesuffix(".mp4") + ".json":
        raise FinalizationRefused("sidecar pair identity is inconsistent")
    return PairPaths(
        video_source=_promotion_source(provisional_video_name),
        sidecar_source=(f"pending/{provisional_video_name.removesuffix('.mp4')}.json"),
        video_target=f"clips/{sidecar.video_file}",
        sidecar_target=f"clips/{sidecar.metadata_file}",
    )


def _promotion_source(provisional_video_name: str) -> str:
    try:
        source = parse_clip_filename(provisional_video_name)
    except ClipNameError as exc:
        raise FinalizationRefused("clip filename identity is invalid") from exc
    if not source.partial or source.extension != "mp4":
        raise FinalizationRefused("source must be one provisional .partial.mp4")
    return f"pending/{provisional_video_name}"


def _required_target(value: str | None) -> str:
    if value is None:
        raise FinalizationRefused("pair promotion is missing a target")
    return value


def _non_negative(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _positive_bound(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _rename_noreplace(source: Path, target: Path) -> None:
    if os.name != "posix":
        os.rename(source, target)
        return
    library = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(library, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOTSUP, "renameat2(RENAME_NOREPLACE) is unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        _AT_FDCWD,
        os.fsencode(source),
        _AT_FDCWD,
        os.fsencode(target),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), target)


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
