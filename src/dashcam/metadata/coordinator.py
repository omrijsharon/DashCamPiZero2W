"""Bounded durable coordination of post-anchor sidecar/name reconciliation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol
from uuid import UUID, uuid4

from dashcam.catalog.filesystem import CatalogFilesystem
from dashcam.catalog.models import (
    CatalogClip,
    CatalogConflictError,
    ClipNotFoundError,
    IntentReconciliationResult,
)
from dashcam.metadata.reconcile import (
    MAX_SIDECAR_BYTES,
    MetadataReconciliationError,
    MetadataReconciliationPlan,
    SidecarParseError,
    parse_sidecar_bytes,
    plan_post_anchor_reconciliation,
)
from dashcam.metadata.schema import ClipSidecar, TimeAnchor
from dashcam.state import ClipLifecycle, GpsTimeState, SystemClockState
from dashcam.storage.intents import IntentKind, OperationIntent


class MetadataReconciliationCatalog(Protocol):
    """Small durable catalog seam used by one reconciliation coordinator."""

    def get_clip(self, clip_id: UUID) -> CatalogClip: ...

    def list_pending_intents(self, *, limit: int) -> tuple[OperationIntent, ...]: ...

    def register_name_reconciliation(
        self,
        plan: MetadataReconciliationPlan,
        *,
        source_sidecar: ClipSidecar,
        monotonic_now_ns: int,
    ) -> UUID: ...

    def reconcile_intent(
        self,
        intent_id: UUID,
        filesystem: CatalogFilesystem,
        *,
        monotonic_now_ns: int,
        max_actions: int = 2,
    ) -> IntentReconciliationResult: ...


@dataclass(frozen=True, slots=True)
class MetadataCoordinatorLimits:
    """Hard bounds for collision scans and pending-intent observation."""

    max_directory_entries: int = 4_096
    max_pending_intents: int = 64
    max_sidecar_bytes: int = MAX_SIDECAR_BYTES

    def __post_init__(self) -> None:
        for name, value, maximum in (
            ("max_directory_entries", self.max_directory_entries, 100_000),
            ("max_pending_intents", self.max_pending_intents, 1_024),
            ("max_sidecar_bytes", self.max_sidecar_bytes, MAX_SIDECAR_BYTES),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
                or value > maximum
            ):
                raise ValueError(f"{name} must be between 1 and {maximum}")


@dataclass(frozen=True, slots=True)
class MetadataCoordinatorOutcome:
    """One completed or replayed stable-UUID reconciliation."""

    clip_id: UUID
    intent_id: UUID | None
    already_reconciled: bool
    actions_attempted: int
    video_path: str
    sidecar_path: str


class MetadataReconciliationRefused(RuntimeError):
    """Raised when durable or filesystem state is unsafe or ambiguous."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class ClipMetadataCoordinator:
    """Persist recovery data, replace metadata, and rename a pair idempotently."""

    def __init__(
        self,
        *,
        catalog: MetadataReconciliationCatalog,
        filesystem: CatalogFilesystem,
        monotonic_ns: Callable[[], int],
        uuid_factory: Callable[[], UUID] = uuid4,
        limits: MetadataCoordinatorLimits | None = None,
    ) -> None:
        self._catalog = catalog
        self._filesystem = filesystem
        self._monotonic_ns = monotonic_ns
        self._uuid_factory = uuid_factory
        self._limits = limits or MetadataCoordinatorLimits()

    def reconcile_clip(
        self,
        clip_id: UUID,
        *,
        anchor: TimeAnchor,
        expected_boot_id: UUID,
        gps_time_state: GpsTimeState,
        system_clock_state: SystemClockState,
    ) -> MetadataCoordinatorOutcome:
        """Reconcile one finalized pair or replay its already-durable intent."""

        if not isinstance(clip_id, UUID) or not isinstance(expected_boot_id, UUID):
            raise TypeError("clip_id and expected_boot_id must be UUID values")
        pending = self._matching_pending(clip_id)
        if pending is not None:
            if pending.kind is not IntentKind.RECONCILE_NAME:
                raise MetadataReconciliationRefused(
                    "another clip mutation is already in progress",
                    retryable=True,
                )
            return self._apply_intent(pending, already_reconciled=False)

        try:
            clip = self._catalog.get_clip(clip_id)
        except ClipNotFoundError as exc:
            raise MetadataReconciliationRefused("clip is absent from the catalog") from exc
        if clip.download_lease is not None:
            raise MetadataReconciliationRefused(
                "clip has an active download lease",
                retryable=True,
            )
        if (
            clip.lifecycle is not ClipLifecycle.FINALIZED
            or not clip.pair_reconciled
            or not clip.managed
        ):
            retryable = (
                clip.lifecycle is ClipLifecycle.FINALIZED
                and clip.managed
                and not clip.pair_reconciled
            )
            raise MetadataReconciliationRefused(
                "clip is not a reconciled finalized managed pair",
                retryable=retryable,
            )
        try:
            payload = self._filesystem.read_bytes(
                clip.sidecar_path,
                maximum_bytes=self._limits.max_sidecar_bytes,
            )
            source_sidecar = parse_sidecar_bytes(payload)
        except OSError as exc:
            raise MetadataReconciliationRefused(
                "source sidecar could not be read",
                retryable=True,
            ) from exc
        except (ValueError, SidecarParseError) as exc:
            raise MetadataReconciliationRefused(
                "source sidecar is not canonical bounded metadata"
            ) from exc
        if (
            source_sidecar.clip_id != clip_id
            or source_sidecar.boot_id != expected_boot_id
            or source_sidecar.video_file != PurePosixPath(clip.video_path).name
            or source_sidecar.metadata_file != PurePosixPath(clip.sidecar_path).name
        ):
            raise MetadataReconciliationRefused(
                "catalog, boot epoch, and sidecar identity do not agree"
            )

        directory = PurePosixPath(clip.video_path).parent.as_posix()
        paths, _examined, truncated = self._filesystem.iter_files(
            directory,
            limit=self._limits.max_directory_entries,
        )
        if truncated:
            raise MetadataReconciliationRefused(
                "directory collision scan exceeded its hard bound"
            )
        now_ns = self._now()
        try:
            plan = plan_post_anchor_reconciliation(
                source_sidecar,
                anchor=anchor,
                intent_id=self._new_uuid(),
                created_monotonic_ns=now_ns,
                existing_names={PurePosixPath(path).name for path in paths},
                directory=directory,
                gps_time_state=gps_time_state,
                system_clock_state=system_clock_state,
            )
        except MetadataReconciliationError as exc:
            raise MetadataReconciliationRefused(str(exc)) from exc
        if plan.already_reconciled:
            return MetadataCoordinatorOutcome(
                clip_id,
                None,
                True,
                0,
                clip.video_path,
                clip.sidecar_path,
            )
        assert plan.intent is not None
        try:
            intent_id = self._catalog.register_name_reconciliation(
                plan,
                source_sidecar=source_sidecar,
                monotonic_now_ns=now_ns,
            )
        except CatalogConflictError as exc:
            raise MetadataReconciliationRefused(str(exc), retryable=True) from exc
        if intent_id != plan.intent.intent_id:
            raise MetadataReconciliationRefused("catalog changed reconciliation intent identity")
        return self._apply_intent(plan.intent, already_reconciled=False)

    def _apply_intent(
        self,
        intent: OperationIntent,
        *,
        already_reconciled: bool,
    ) -> MetadataCoordinatorOutcome:
        result = self._catalog.reconcile_intent(
            intent.intent_id,
            self._filesystem,
            monotonic_now_ns=self._now(),
            max_actions=2,
        )
        if result.intent != intent:
            raise MetadataReconciliationRefused("catalog returned another intent")
        if result.problems or not result.complete:
            detail = ",".join(result.problems) if result.problems else "incomplete"
            raise MetadataReconciliationRefused(
                f"name reconciliation did not complete: {detail}",
                retryable=not result.problems,
            )
        clip = self._catalog.get_clip(intent.clip_id)
        if (
            clip.clip_id != intent.clip_id
            or clip.video_path != intent.paths.video_target
            or clip.sidecar_path != intent.paths.sidecar_target
            or not clip.pair_reconciled
        ):
            raise MetadataReconciliationRefused(
                "completed catalog state differs from its intent"
            )
        return MetadataCoordinatorOutcome(
            clip.clip_id,
            intent.intent_id,
            already_reconciled,
            result.actions_attempted,
            clip.video_path,
            clip.sidecar_path,
        )

    def _matching_pending(self, clip_id: UUID) -> OperationIntent | None:
        limit = self._limits.max_pending_intents
        values = self._catalog.list_pending_intents(limit=limit + 1)
        if len(values) > limit:
            raise MetadataReconciliationRefused(
                "pending-intent scan cannot prove unique ownership",
                retryable=True,
            )
        matches = tuple(intent for intent in values if intent.clip_id == clip_id)
        if len(matches) > 1:
            raise MetadataReconciliationRefused("multiple pending intents exist for one clip")
        return None if not matches else matches[0]

    def _now(self) -> int:
        value = self._monotonic_ns()
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= 9_223_372_036_854_775_807
        ):
            raise MetadataReconciliationRefused("monotonic clock returned an invalid value")
        return value

    def _new_uuid(self) -> UUID:
        value = self._uuid_factory()
        if not isinstance(value, UUID):
            raise MetadataReconciliationRefused("UUID factory returned an invalid value")
        return value
