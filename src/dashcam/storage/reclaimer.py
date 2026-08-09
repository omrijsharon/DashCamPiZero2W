"""One-pair-at-a-time durable retention reclamation.

The catalog transaction reserves exactly one oldest eligible clip in
``DELETING`` before this component permits either exFAT member to be unlinked.
Every call performs at most one logical pair operation so its caller can obtain
a fresh same-volume free-space observation before authorizing another delete.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from dashcam.catalog.filesystem import CatalogFilesystem
from dashcam.catalog.models import IntentReconciliationResult
from dashcam.storage.intents import IntentKind, OperationIntent


class ReclamationError(RuntimeError):
    """A durable deletion was ambiguous or could not complete safely."""


class ReclamationCatalog(Protocol):
    def list_pending_delete_intents(self, *, limit: int) -> tuple[OperationIntent, ...]: ...

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


@dataclass(frozen=True, slots=True)
class ReclamationStep:
    """Bounded result that always requires a fresh observation before reuse."""

    clip_id: UUID | None
    intent_id: UUID | None
    recovered: bool
    deleted: bool
    eligible_found: bool
    actions_attempted: int
    pending_delete_remaining: bool = False

    @property
    def progress(self) -> bool:
        return self.deleted or self.actions_attempted > 0


class StorageReclaimer:
    """Serialize crash replay and new oldest-first deletion one pair at a time."""

    def __init__(
        self,
        *,
        catalog: ReclamationCatalog,
        filesystem: CatalogFilesystem,
        monotonic_ns: Callable[[], int],
    ) -> None:
        if not callable(monotonic_ns):
            raise TypeError("monotonic_ns must be callable")
        self._catalog = catalog
        self._filesystem = filesystem
        self._monotonic_ns = monotonic_ns

    def run_one(self, *, boot_id: str, allow_new: bool) -> ReclamationStep:
        """Replay one prior delete, or reserve and complete one new oldest clip."""

        if not isinstance(boot_id, str) or not boot_id or len(boot_id) > 128:
            raise ValueError("boot_id must be a bounded non-empty string")
        if not isinstance(allow_new, bool):
            raise TypeError("allow_new must be boolean")
        pending = self._catalog.list_pending_delete_intents(limit=1)
        recovered = bool(pending)
        intent_id: UUID | None
        if recovered:
            intent = pending[0]
            if intent.kind is not IntentKind.DELETE:
                raise ReclamationError("delete-only catalog scan returned another intent kind")
            intent_id = intent.intent_id
        else:
            if not allow_new:
                return ReclamationStep(None, None, False, False, False, 0)
            intent_id = self._catalog.prepare_oldest_eligible_delete(
                monotonic_now_ns=self._now(),
                boot_id=boot_id,
            )
            if intent_id is None:
                return ReclamationStep(None, None, False, False, False, 0)

        result = self._catalog.reconcile_intent(
            intent_id,
            self._filesystem,
            monotonic_now_ns=self._now(),
            max_actions=2,
        )
        if result.intent.kind is not IntentKind.DELETE or result.intent.intent_id != intent_id:
            raise ReclamationError("catalog returned a different deletion intent")
        if result.problems:
            raise ReclamationError("deletion pair is ambiguous: " + ",".join(result.problems))
        if not result.complete:
            raise ReclamationError("bounded two-member deletion did not complete")
        return ReclamationStep(
            clip_id=result.intent.clip_id,
            intent_id=intent_id,
            recovered=recovered,
            deleted=True,
            eligible_found=True,
            actions_attempted=result.actions_attempted,
            pending_delete_remaining=bool(
                self._catalog.list_pending_delete_intents(limit=1)
            ),
        )

    def _now(self) -> int:
        value = self._monotonic_ns()
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ReclamationError("monotonic clock returned an invalid value")
        return value


__all__ = [
    "ReclamationError",
    "ReclamationStep",
    "StorageReclaimer",
]
