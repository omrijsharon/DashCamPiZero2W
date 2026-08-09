"""Durable SQLite clip catalog and crash-recoverable pair coordination."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, cast
from uuid import UUID, uuid4

from dashcam.catalog.filesystem import CatalogFilesystem, RootedFilesystem
from dashcam.catalog.models import (
    CatalogClip,
    CatalogConflictError,
    ClipNotFoundError,
    EventProtectionResult,
    EventSource,
    EventTargetRole,
    IntentReconciliationResult,
    ReconciliationBounds,
    StartupReconciliationReport,
)
from dashcam.metadata.reconcile import (
    MAX_SIDECAR_BYTES,
    MetadataReconciliationPlan,
    SidecarParseError,
    parse_sidecar_bytes,
)
from dashcam.metadata.schema import ClipSidecar
from dashcam.state import (
    ClipLifecycle,
    DownloadLease,
    DownloadLeaseError,
)
from dashcam.storage.intents import (
    ActionKind,
    IntentKind,
    MemberObservation,
    OperationIntent,
    PairPaths,
    ReconciliationPlan,
    plan_reconciliation,
)
from dashcam.storage.naming import ClipFilePair, ClipNameError, parse_clip_filename
from dashcam.storage.retention import (
    RetentionCandidate,
    RetentionPlan,
    select_oldest_eligible,
)

SCHEMA_VERSION: Final = 5
MAX_PENDING_EVENT_WINDOWS: Final = 64
MAX_QUERY_ROWS: Final = 10_000
_MAX_REASON_CHARS: Final = 256
_MAX_BOOT_ID_CHARS: Final = 128
_BOOT_ID_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_MANAGED_DIRECTORIES: Final = ("pending", "clips", "protected", "quarantine")
_DEFAULT_RECONCILIATION_BOUNDS: Final = ReconciliationBounds()

_MIGRATIONS: Final[tuple[tuple[int, str, tuple[str, ...]], ...]] = (
    (
        1,
        "create_clip_catalog",
        (
            """
            CREATE TABLE clips (
                clip_id TEXT PRIMARY KEY,
                lifecycle TEXT NOT NULL,
                video_path TEXT NOT NULL,
                sidecar_path TEXT NOT NULL,
                start_monotonic_ns INTEGER NOT NULL,
                end_monotonic_ns INTEGER,
                retention_order INTEGER NOT NULL,
                size_bytes INTEGER NOT NULL,
                protected INTEGER NOT NULL CHECK (protected IN (0, 1)),
                protection_reason TEXT,
                pair_reconciled INTEGER NOT NULL CHECK (pair_reconciled IN (0, 1)),
                managed INTEGER NOT NULL CHECK (managed IN (0, 1)),
                lease_holder TEXT,
                lease_issued_ns INTEGER,
                lease_expires_ns INTEGER,
                lease_boot_id TEXT,
                created_catalog_ns INTEGER NOT NULL,
                updated_catalog_ns INTEGER NOT NULL,
                CHECK (size_bytes >= 0),
                CHECK (start_monotonic_ns >= 0),
                CHECK (end_monotonic_ns IS NULL OR end_monotonic_ns > start_monotonic_ns),
                CHECK (
                    (lease_holder IS NULL AND lease_issued_ns IS NULL
                     AND lease_expires_ns IS NULL AND lease_boot_id IS NULL)
                    OR
                    (lease_holder IS NOT NULL AND lease_issued_ns IS NOT NULL
                     AND lease_expires_ns IS NOT NULL AND lease_boot_id IS NOT NULL)
                )
            )
            """,
            """
            CREATE UNIQUE INDEX clips_retention_order_idx
            ON clips(retention_order)
            """,
            """
            CREATE TABLE operation_intents (
                intent_id TEXT PRIMARY KEY,
                clip_id TEXT NOT NULL REFERENCES clips(clip_id),
                kind TEXT NOT NULL,
                created_monotonic_ns INTEGER NOT NULL,
                video_source TEXT NOT NULL,
                sidecar_source TEXT NOT NULL,
                video_target TEXT,
                sidecar_target TEXT,
                status TEXT NOT NULL CHECK (status IN ('PENDING', 'COMPLETE')),
                last_problem TEXT,
                completed_monotonic_ns INTEGER
            )
            """,
            """
            CREATE UNIQUE INDEX one_pending_intent_per_clip_idx
            ON operation_intents(clip_id) WHERE status = 'PENDING'
            """,
            """
            CREATE INDEX pending_intents_order_idx
            ON operation_intents(status, created_monotonic_ns, intent_id)
            """,
        ),
    ),
    (
        2,
        "add_event_protection",
        (
            """
            CREATE TABLE protection_events (
                event_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                triggered_monotonic_ns INTEGER NOT NULL,
                current_clip_id TEXT NOT NULL REFERENCES clips(clip_id),
                requested_previous INTEGER NOT NULL,
                requested_next INTEGER NOT NULL,
                missing_previous INTEGER NOT NULL,
                remaining_next INTEGER NOT NULL,
                CHECK (requested_previous >= 0),
                CHECK (requested_next >= 0),
                CHECK (missing_previous >= 0),
                CHECK (remaining_next >= 0)
            )
            """,
            """
            CREATE TABLE protection_event_targets (
                event_id TEXT NOT NULL REFERENCES protection_events(event_id),
                clip_id TEXT NOT NULL REFERENCES clips(clip_id),
                role TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                PRIMARY KEY (event_id, clip_id)
            )
            """,
            """
            CREATE INDEX pending_event_windows_idx
            ON protection_events(remaining_next, triggered_monotonic_ns, event_id)
            """,
        ),
    ),
    (
        3,
        "add_protection_revisions",
        (
            """
            ALTER TABLE clips
            ADD COLUMN protection_revision INTEGER NOT NULL DEFAULT 0
                CHECK (protection_revision >= 0)
            """,
            """
            ALTER TABLE operation_intents
            ADD COLUMN expected_protection_revision INTEGER
                CHECK (
                    expected_protection_revision IS NULL
                    OR expected_protection_revision >= 0
                )
            """,
        ),
    ),
    (
        4,
        "add_name_reconciliation_payload",
        (
            """
            ALTER TABLE operation_intents
            ADD COLUMN reconciliation_sidecar BLOB
            """,
            """
            ALTER TABLE operation_intents
            ADD COLUMN reconciliation_source_sha256 TEXT
            """,
        ),
    ),
    (
        5,
        "add_retention_threshold_latch",
        (
            """
            CREATE TABLE retention_threshold_latch (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                volume_uuid TEXT NOT NULL,
                capacity_bytes INTEGER NOT NULL CHECK (capacity_bytes > 0),
                reclaim_latched INTEGER NOT NULL
                    CHECK (reclaim_latched IN (0, 1))
            )
            """,
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class RetentionThresholdLatch:
    """Durable hysteresis state bound to one verified filesystem contract."""

    volume_uuid: str
    capacity_bytes: int
    reclaim_latched: bool

    def __post_init__(self) -> None:
        if not self.volume_uuid or len(self.volume_uuid) > 128:
            raise ValueError("volume_uuid must be a bounded non-empty string")
        _non_negative_integer(self.capacity_bytes, "capacity_bytes")
        if self.capacity_bytes == 0:
            raise ValueError("capacity_bytes must be positive")
        if not isinstance(self.reclaim_latched, bool):
            raise TypeError("reclaim_latched must be boolean")


class ClipCatalog:
    """Single-owner catalog API with transactional cross-process coordination."""

    def __init__(self, database_path: Path, *, busy_timeout_ms: int = 5_000) -> None:
        if not isinstance(database_path, Path):
            raise TypeError("database_path must be a pathlib.Path")
        _positive_integer(busy_timeout_ms, "busy_timeout_ms")
        if busy_timeout_ms > 30_000:
            raise ValueError("busy_timeout_ms exceeds 30 second hard bound")
        if not database_path.parent.exists():
            raise ValueError("catalog parent directory does not exist")
        self._path = database_path
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            database_path,
            timeout=busy_timeout_ms / 1_000,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA synchronous = FULL")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def __enter__(self) -> ClipCatalog:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        row = self._connection.execute("PRAGMA user_version").fetchone()
        assert row is not None
        return int(row[0])

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def next_retention_order(self) -> int:
        """Allocate the next single-owner global ordering value."""

        with self._lock:
            row = self._connection.execute(
                "SELECT COALESCE(MAX(retention_order), -1) + 1 FROM clips"
            ).fetchone()
        assert row is not None
        value = int(row[0])
        if not 0 <= value <= 9_223_372_036_854_775_807:
            raise CatalogConflictError("retention ordering space is exhausted")
        return value

    def retention_threshold_latch(self) -> RetentionThresholdLatch | None:
        """Load the singleton hysteresis latch without changing catalog state."""

        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM retention_threshold_latch WHERE singleton = 1"
            ).fetchone()
        if row is None:
            return None
        return RetentionThresholdLatch(
            volume_uuid=str(row["volume_uuid"]),
            capacity_bytes=int(row["capacity_bytes"]),
            reclaim_latched=bool(row["reclaim_latched"]),
        )

    def store_retention_threshold_latch(self, latch: RetentionThresholdLatch) -> None:
        """Persist one transition before a caller publishes or acts on it."""

        if not isinstance(latch, RetentionThresholdLatch):
            raise TypeError("latch must be RetentionThresholdLatch")
        with self._transaction():
            self._connection.execute(
                """
                INSERT OR IGNORE INTO retention_threshold_latch (
                    singleton, volume_uuid, capacity_bytes, reclaim_latched
                ) VALUES (1, ?, ?, ?)
                """,
                (
                    latch.volume_uuid,
                    latch.capacity_bytes,
                    int(latch.reclaim_latched),
                ),
            )
            cursor = self._connection.execute(
                """
                UPDATE retention_threshold_latch
                SET reclaim_latched = ?
                WHERE singleton = 1
                  AND volume_uuid = ? AND capacity_bytes = ?
                """,
                (
                    int(latch.reclaim_latched),
                    latch.volume_uuid,
                    latch.capacity_bytes,
                ),
            )
            if cursor.rowcount != 1:
                raise CatalogConflictError(
                    "retention threshold latch binding differs from durable state"
                )

    def register_clip(self, clip: CatalogClip, *, catalog_now_ns: int = 0) -> None:
        """Insert one new managed clip without overwriting existing durable state."""

        _validate_clip(clip)
        _non_negative_integer(catalog_now_ns, "catalog_now_ns")
        with self._transaction():
            self._insert_clip_locked(clip, catalog_now_ns=catalog_now_ns)

    def register_finalizing_clip(
        self,
        clip: CatalogClip,
        *,
        promotion_paths: PairPaths,
        monotonic_now_ns: int,
    ) -> UUID:
        """Atomically register one durable pending-to-clips pair move.

        The caller must durably create both pending members before this
        transaction. A crash after commit is recovered from the FINALIZE intent;
        a target collision remains a latched reconciliation problem.
        """

        _validate_clip(clip)
        if not isinstance(promotion_paths, PairPaths):
            raise TypeError("promotion_paths must be PairPaths")
        _non_negative_integer(monotonic_now_ns, "monotonic_now_ns")
        source_directory = PurePosixPath(clip.video_path).parent.as_posix()
        target_video = promotion_paths.video_target
        target_sidecar = promotion_paths.sidecar_target
        if (
            clip.lifecycle is not ClipLifecycle.FINALIZING
            or clip.end_monotonic_ns is None
            or source_directory != "pending"
            or clip.pair_reconciled
            or not clip.managed
            or clip.download_lease is not None
            or promotion_paths.video_source != clip.video_path
            or promotion_paths.sidecar_source != clip.sidecar_path
            or target_video is None
            or target_sidecar is None
            or PurePosixPath(target_video).parent.as_posix() != "clips"
            or PurePosixPath(target_sidecar).parent.as_posix() != "clips"
        ):
            raise CatalogConflictError(
                "finalization registration requires one managed unreconciled "
                "pending FINALIZING clip and explicit clips targets"
            )
        source_video_name = PurePosixPath(clip.video_path).name
        source_sidecar_name = PurePosixPath(clip.sidecar_path).name
        target_video_name = PurePosixPath(target_video).name
        target_sidecar_name = PurePosixPath(target_sidecar).name
        try:
            ClipFilePair(source_video_name, source_sidecar_name)
            ClipFilePair(target_video_name, target_sidecar_name)
            source_identity = parse_clip_filename(source_video_name)
            target_identity = parse_clip_filename(target_video_name)
        except ClipNameError as exc:
            raise CatalogConflictError("finalization paths are not a safe clip pair") from exc
        if (
            not source_identity.partial
            or target_identity.partial
            or source_identity.boot_id != target_identity.boot_id
            or source_identity.sequence != target_identity.sequence
        ):
            raise CatalogConflictError(
                "finalization targets must remove the partial suffix without "
                "changing clip identity"
            )
        with self._transaction():
            self._insert_clip_locked(clip, catalog_now_ns=monotonic_now_ns)
            intent = self._insert_intent_locked(
                clip.clip_id,
                kind=IntentKind.FINALIZE,
                paths=promotion_paths,
                monotonic_now_ns=monotonic_now_ns,
            )
        return intent.intent_id

    def register_name_reconciliation(
        self,
        plan: MetadataReconciliationPlan,
        *,
        source_sidecar: ClipSidecar,
        monotonic_now_ns: int,
    ) -> UUID:
        """Persist all recovery data before replacing or renaming either member."""

        if not isinstance(plan, MetadataReconciliationPlan) or plan.intent is None:
            raise TypeError("plan must contain a name-reconciliation intent")
        if not isinstance(source_sidecar, ClipSidecar):
            raise TypeError("source_sidecar must be a ClipSidecar")
        _non_negative_integer(monotonic_now_ns, "monotonic_now_ns")
        intent = plan.intent
        if (
            intent.kind is not IntentKind.RECONCILE_NAME
            or intent.created_monotonic_ns != monotonic_now_ns
            or source_sidecar.clip_id != plan.sidecar.clip_id
            or source_sidecar.clip_id != intent.clip_id
            or source_sidecar.video_file
            != PurePosixPath(intent.paths.video_source).name
            or source_sidecar.metadata_file
            != PurePosixPath(intent.paths.sidecar_source).name
            or plan.sidecar.video_file
            != PurePosixPath(cast(str, intent.paths.video_target)).name
            or plan.sidecar.metadata_file
            != PurePosixPath(cast(str, intent.paths.sidecar_target)).name
            or source_sidecar.start_monotonic_ns != plan.sidecar.start_monotonic_ns
            or source_sidecar.end_monotonic_ns != plan.sidecar.end_monotonic_ns
        ):
            raise CatalogConflictError("name-reconciliation plan has inconsistent identity")
        source_payload = source_sidecar.to_canonical_json()
        target_payload = plan.sidecar.to_canonical_json()
        if len(source_payload) > MAX_SIDECAR_BYTES or len(target_payload) > MAX_SIDECAR_BYTES:
            raise CatalogConflictError("name-reconciliation sidecar exceeds its byte bound")
        try:
            source_name = parse_clip_filename(source_sidecar.video_file)
            target_name = parse_clip_filename(plan.sidecar.video_file)
        except ClipNameError as exc:
            raise CatalogConflictError("name-reconciliation filenames are invalid") from exc
        if (
            source_name.partial
            or not source_name.provisional
            or target_name.provisional
            or target_name.partial
            or source_name.boot_id != target_name.boot_id
            or source_name.sequence != target_name.sequence
        ):
            raise CatalogConflictError("name reconciliation must preserve clip identity")

        with self._transaction():
            row = self._required_clip_row(intent.clip_id)
            if (
                ClipLifecycle(str(row["lifecycle"])) is not ClipLifecycle.FINALIZED
                or not bool(row["pair_reconciled"])
                or not bool(row["managed"])
                or row["lease_holder"] is not None
                or str(row["video_path"]) != intent.paths.video_source
                or str(row["sidecar_path"]) != intent.paths.sidecar_source
                or self._pending_intent_row(intent.clip_id) is not None
            ):
                raise CatalogConflictError(
                    "clip is not an idle reconciled finalized pair at the declared source"
                )
            source_directory = PurePosixPath(intent.paths.video_source).parent
            if (
                source_directory
                != PurePosixPath(intent.paths.sidecar_source).parent
                or source_directory
                != PurePosixPath(cast(str, intent.paths.video_target)).parent
                or source_directory
                != PurePosixPath(cast(str, intent.paths.sidecar_target)).parent
                or source_directory.as_posix() not in {"clips", "protected"}
            ):
                raise CatalogConflictError("name reconciliation must remain in one final directory")
            self._insert_existing_intent_locked(
                intent,
                reconciliation_sidecar=target_payload,
                reconciliation_source_sha256=hashlib.sha256(source_payload).hexdigest(),
            )
            self._connection.execute(
                """
                UPDATE clips SET pair_reconciled = 0, updated_catalog_ns = ?
                WHERE clip_id = ?
                """,
                (monotonic_now_ns, str(intent.clip_id)),
            )
        return intent.intent_id

    def get_clip(self, clip_id: UUID) -> CatalogClip:
        _uuid(clip_id, "clip_id")
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM clips WHERE clip_id = ?", (str(clip_id),)
            ).fetchone()
        if row is None:
            raise ClipNotFoundError(str(clip_id))
        return _clip_from_row(row)

    def list_clips(self, *, limit: int, after_order: int = -1) -> tuple[CatalogClip, ...]:
        """List clips in stable oldest-first order with an explicit row bound."""

        _row_limit(limit, "limit")
        if isinstance(after_order, bool) or not isinstance(after_order, int) or after_order < -1:
            raise ValueError("after_order must be an integer of at least -1")
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM clips
                WHERE retention_order > ?
                ORDER BY retention_order, clip_id
                LIMIT ?
                """,
                (after_order, limit),
            ).fetchall()
        return tuple(_clip_from_row(row) for row in rows)

    def list_metadata_reconciliation_candidates(
        self,
        expected_boot_id: UUID,
        *,
        limit: int,
        after_order: int = -1,
        after_clip_id: UUID | None = None,
    ) -> tuple[CatalogClip, ...]:
        """Return one bounded page of current-boot provisional finalized pairs."""

        boot_id = _uuid(expected_boot_id, "expected_boot_id")
        _row_limit(limit, "limit")
        if (
            isinstance(after_order, bool)
            or not isinstance(after_order, int)
            or after_order < -1
        ):
            raise ValueError("after_order must be an integer of at least -1")
        cursor_id = UUID(int=0) if after_clip_id is None else _uuid(
            after_clip_id, "after_clip_id"
        )
        short_boot_id = boot_id.hex[:12]
        six_digits = "[0-9]" * 6
        video_patterns = (
            f"clips/boot-{short_boot_id}-{six_digits}.mp4",
            f"protected/boot-{short_boot_id}-{six_digits}.mp4",
        )
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT c.* FROM clips AS c
                WHERE c.lifecycle = ?
                  AND c.managed = 1
                  AND c.lease_holder IS NULL
                  AND c.sidecar_path =
                      substr(c.video_path, 1, length(c.video_path) - 4) || '.json'
                  AND (c.video_path GLOB ? OR c.video_path GLOB ?)
                  AND (
                        c.pair_reconciled = 1
                        OR EXISTS (
                            SELECT 1 FROM operation_intents AS active_name
                            WHERE active_name.clip_id = c.clip_id
                              AND active_name.status = 'PENDING'
                              AND active_name.kind = ?
                        )
                  )
                  AND NOT EXISTS (
                        SELECT 1 FROM operation_intents AS other_mutation
                        WHERE other_mutation.clip_id = c.clip_id
                          AND other_mutation.status = 'PENDING'
                          AND other_mutation.kind <> ?
                  )
                  AND (
                        c.retention_order > ?
                        OR (
                            c.retention_order = ?
                            AND c.clip_id > ?
                        )
                  )
                ORDER BY c.retention_order, c.clip_id
                LIMIT ?
                """,
                (
                    ClipLifecycle.FINALIZED.value,
                    video_patterns[0],
                    video_patterns[1],
                    IntentKind.RECONCILE_NAME.value,
                    IntentKind.RECONCILE_NAME.value,
                    after_order,
                    after_order,
                    str(cursor_id),
                    limit,
                ),
            ).fetchall()
        return tuple(_clip_from_row(row) for row in rows)

    def acquire_download_lease(
        self,
        clip_id: UUID,
        *,
        holder: str,
        monotonic_now_ns: int,
        duration_ns: int,
        boot_id: str,
    ) -> DownloadLease:
        """Atomically acquire or replace an expired/previous-boot lease."""

        _uuid(clip_id, "clip_id")
        _boot_id(boot_id)
        lease = DownloadLease.issue(
            holder=holder,
            monotonic_now_ns=monotonic_now_ns,
            duration_ns=duration_ns,
        )
        with self._transaction():
            row = self._required_clip_row(clip_id)
            if ClipLifecycle(str(row["lifecycle"])) is not ClipLifecycle.FINALIZED:
                raise DownloadLeaseError("only finalized clips can be downloaded")
            if not bool(row["pair_reconciled"]):
                raise DownloadLeaseError("clip pair is not reconciled")
            if self._pending_intent_row(clip_id) is not None:
                raise DownloadLeaseError("clip mutation is in progress")
            if _row_has_active_lease(row, monotonic_now_ns=monotonic_now_ns, boot_id=boot_id):
                raise DownloadLeaseError("clip already has an active download lease")
            self._connection.execute(
                """
                UPDATE clips
                SET lease_holder = ?, lease_issued_ns = ?, lease_expires_ns = ?,
                    lease_boot_id = ?, updated_catalog_ns = ?
                WHERE clip_id = ?
                """,
                (
                    lease.holder,
                    lease.issued_at_monotonic_ns,
                    lease.expires_at_monotonic_ns,
                    boot_id,
                    monotonic_now_ns,
                    str(clip_id),
                ),
            )
        return lease

    def release_download_lease(self, clip_id: UUID, *, holder: str) -> None:
        _uuid(clip_id, "clip_id")
        with self._transaction():
            row = self._required_clip_row(clip_id)
            stored_holder = cast(str | None, row["lease_holder"])
            if stored_holder is None:
                return
            if stored_holder != holder:
                raise DownloadLeaseError("lease is owned by a different holder")
            self._clear_lease_locked(clip_id)

    def clear_expired_download_leases(
        self,
        *,
        monotonic_now_ns: int,
        boot_id: str,
        limit: int,
    ) -> tuple[int, bool]:
        """Clear a bounded batch; previous-boot leases expire immediately."""

        _non_negative_integer(monotonic_now_ns, "monotonic_now_ns")
        _boot_id(boot_id)
        _row_limit(limit, "limit")
        with self._transaction():
            rows = self._connection.execute(
                """
                SELECT clip_id FROM clips
                WHERE lease_holder IS NOT NULL
                  AND (lease_boot_id <> ? OR lease_expires_ns <= ?)
                ORDER BY lease_expires_ns, clip_id
                LIMIT ?
                """,
                (boot_id, monotonic_now_ns, limit + 1),
            ).fetchall()
            selected = rows[:limit]
            for row in selected:
                self._clear_lease_locked(UUID(str(row["clip_id"])))
        return len(selected), len(rows) > limit

    def retention_candidates(
        self,
        *,
        monotonic_now_ns: int,
        boot_id: str,
        limit: int,
    ) -> tuple[RetentionCandidate, ...]:
        """Return a bounded, stable candidate view from durable catalog state."""

        _non_negative_integer(monotonic_now_ns, "monotonic_now_ns")
        _boot_id(boot_id)
        _row_limit(limit, "limit")
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT c.*,
                       EXISTS(
                           SELECT 1 FROM operation_intents i
                           WHERE i.clip_id = c.clip_id AND i.status = 'PENDING'
                       ) AS mutation_in_progress
                FROM clips c
                WHERE c.lifecycle <> ?
                ORDER BY c.retention_order, c.clip_id
                LIMIT ?
                """,
                (ClipLifecycle.DELETED.value, limit),
            ).fetchall()
        values: list[RetentionCandidate] = []
        for row in rows:
            active_lease_expiry = (
                int(row["lease_expires_ns"])
                if _row_has_active_lease(row, monotonic_now_ns=monotonic_now_ns, boot_id=boot_id)
                else None
            )
            values.append(
                RetentionCandidate(
                    clip_id=UUID(str(row["clip_id"])),
                    retention_order=int(row["retention_order"]),
                    size_bytes=int(row["size_bytes"]),
                    managed=bool(row["managed"]),
                    finalized=ClipLifecycle(str(row["lifecycle"])) is ClipLifecycle.FINALIZED,
                    pair_reconciled=bool(row["pair_reconciled"]),
                    protected=bool(row["protected"]),
                    mutation_in_progress=bool(row["mutation_in_progress"]),
                    lease_expires_monotonic_ns=active_lease_expiry,
                )
            )
        return tuple(values)

    def plan_retention(
        self,
        *,
        requested_reclaim_bytes: int,
        monotonic_now_ns: int,
        boot_id: str,
        candidate_limit: int,
    ) -> RetentionPlan:
        return select_oldest_eligible(
            self.retention_candidates(
                monotonic_now_ns=monotonic_now_ns,
                boot_id=boot_id,
                limit=candidate_limit,
            ),
            requested_reclaim_bytes=requested_reclaim_bytes,
            monotonic_ns=monotonic_now_ns,
        )

    def prepare_protect(
        self,
        clip_id: UUID,
        *,
        reason: str,
        monotonic_now_ns: int,
    ) -> UUID | None:
        """Durably block retention and queue a protected-directory pair move."""

        _reason(reason)
        _non_negative_integer(monotonic_now_ns, "monotonic_now_ns")
        with self._transaction():
            return self._prepare_protect_locked(
                clip_id, reason=reason, monotonic_now_ns=monotonic_now_ns
            )

    def prepare_unprotect(self, clip_id: UUID, *, monotonic_now_ns: int) -> UUID | None:
        """Queue an unprotect move while keeping protection set until completion."""

        _non_negative_integer(monotonic_now_ns, "monotonic_now_ns")
        with self._transaction():
            row = self._required_clip_row(clip_id)
            if not bool(row["protected"]):
                return None
            pending = self._pending_intent_row(clip_id)
            if pending is not None:
                if IntentKind(str(pending["kind"])) is IntentKind.UNPROTECT:
                    return UUID(str(pending["intent_id"]))
                raise CatalogConflictError("clip mutation is already in progress")
            lifecycle = ClipLifecycle(str(row["lifecycle"]))
            if lifecycle is not ClipLifecycle.FINALIZED:
                raise CatalogConflictError("only finalized clips can be manually unprotected")
            video_source = str(row["video_path"])
            sidecar_source = str(row["sidecar_path"])
            if PurePosixPath(video_source).parent.as_posix() == "clips":
                self._connection.execute(
                    """
                    UPDATE clips
                    SET protected = 0, protection_reason = NULL, updated_catalog_ns = ?
                    WHERE clip_id = ?
                    """,
                    (monotonic_now_ns, str(clip_id)),
                )
                return None
            paths = _move_paths(video_source, sidecar_source, "clips")
            intent = self._insert_intent_locked(
                clip_id,
                kind=IntentKind.UNPROTECT,
                paths=paths,
                monotonic_now_ns=monotonic_now_ns,
                expected_protection_revision=int(row["protection_revision"]),
            )
            self._connection.execute(
                """
                UPDATE clips SET pair_reconciled = 0, updated_catalog_ns = ?
                WHERE clip_id = ?
                """,
                (monotonic_now_ns, str(clip_id)),
            )
            return intent.intent_id

    def prepare_delete(
        self,
        clip_id: UUID,
        *,
        monotonic_now_ns: int,
        boot_id: str,
    ) -> UUID:
        """Atomically transition to DELETING after all eligibility checks."""

        _non_negative_integer(monotonic_now_ns, "monotonic_now_ns")
        _boot_id(boot_id)
        with self._transaction():
            row = self._required_clip_row(clip_id)
            return self._prepare_delete_row_locked(
                row,
                monotonic_now_ns=monotonic_now_ns,
                boot_id=boot_id,
            )

    def prepare_oldest_eligible_delete(
        self,
        *,
        monotonic_now_ns: int,
        boot_id: str,
    ) -> UUID | None:
        """Atomically reserve exactly one oldest eligible reconciled clip."""

        _non_negative_integer(monotonic_now_ns, "monotonic_now_ns")
        _boot_id(boot_id)
        with self._transaction():
            row = self._connection.execute(
                """
                SELECT c.* FROM clips c
                WHERE c.lifecycle = ?
                  AND c.managed = 1
                  AND c.pair_reconciled = 1
                  AND c.protected = 0
                  AND NOT EXISTS (
                      SELECT 1 FROM operation_intents i
                      WHERE i.clip_id = c.clip_id AND i.status = 'PENDING'
                  )
                  AND NOT (
                      c.lease_holder IS NOT NULL
                      AND c.lease_boot_id = ?
                      AND c.lease_expires_ns > ?
                  )
                ORDER BY c.retention_order, c.clip_id
                LIMIT 1
                """,
                (
                    ClipLifecycle.FINALIZED.value,
                    boot_id,
                    monotonic_now_ns,
                ),
            ).fetchone()
            if row is None:
                return None
            return self._prepare_delete_row_locked(
                row,
                monotonic_now_ns=monotonic_now_ns,
                boot_id=boot_id,
            )

    def trigger_event(
        self,
        current_clip_id: UUID,
        *,
        source: EventSource,
        monotonic_now_ns: int,
        previous_count: int = 2,
        next_count: int = 1,
    ) -> EventProtectionResult:
        """Protect previous/current clips and persist a future-clip window."""

        if not isinstance(source, EventSource):
            raise TypeError("source must be an EventSource")
        _non_negative_integer(monotonic_now_ns, "monotonic_now_ns")
        _bounded_count(previous_count, "previous_count")
        _bounded_count(next_count, "next_count")
        event_id = uuid4()
        reason = f"event:{event_id}:{source.value}"
        with self._transaction():
            current = self._required_clip_row(current_clip_id)
            if ClipLifecycle(str(current["lifecycle"])) in {
                ClipLifecycle.DELETING,
                ClipLifecycle.DELETED,
            }:
                raise CatalogConflictError("current event clip no longer exists")
            if next_count:
                pending_window_count = int(
                    self._connection.execute(
                        """
                        SELECT COUNT(*) FROM protection_events
                        WHERE remaining_next > 0
                        """
                    ).fetchone()[0]
                )
                if pending_window_count >= MAX_PENDING_EVENT_WINDOWS:
                    raise CatalogConflictError("pending event-window bound reached")
            previous_rows = self._connection.execute(
                """
                SELECT * FROM clips
                WHERE retention_order < ? AND lifecycle = ?
                ORDER BY retention_order DESC, clip_id DESC
                LIMIT ?
                """,
                (
                    int(current["retention_order"]),
                    ClipLifecycle.FINALIZED.value,
                    previous_count,
                ),
            ).fetchall()
            protected_ids: list[UUID] = []
            intent_ids: list[UUID] = []
            self._connection.execute(
                """
                INSERT INTO protection_events (
                    event_id, source, triggered_monotonic_ns, current_clip_id,
                    requested_previous, requested_next, missing_previous, remaining_next
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(event_id),
                    source.value,
                    monotonic_now_ns,
                    str(current_clip_id),
                    previous_count,
                    next_count,
                    previous_count - len(previous_rows),
                    next_count,
                ),
            )
            targets = [
                (row, EventTargetRole.PREVIOUS, ordinal)
                for ordinal, row in enumerate(reversed(previous_rows), start=1)
            ]
            targets.append((current, EventTargetRole.CURRENT, 0))
            for row, role, ordinal in targets:
                target_id = UUID(str(row["clip_id"]))
                intent_id = self._protect_for_event_locked(
                    target_id,
                    reason=reason,
                    monotonic_now_ns=monotonic_now_ns,
                )
                self._connection.execute(
                    """
                    INSERT INTO protection_event_targets (event_id, clip_id, role, ordinal)
                    VALUES (?, ?, ?, ?)
                    """,
                    (str(event_id), str(target_id), role.value, ordinal),
                )
                protected_ids.append(target_id)
                if intent_id is not None:
                    intent_ids.append(intent_id)
        return EventProtectionResult(
            event_id=event_id,
            protected_clip_ids=tuple(protected_ids),
            missing_previous_count=previous_count - len(previous_rows),
            pending_next_count=next_count,
            queued_intent_ids=tuple(intent_ids),
        )

    def finalize_clip(
        self,
        clip_id: UUID,
        *,
        end_monotonic_ns: int,
        size_bytes: int,
        monotonic_now_ns: int,
    ) -> tuple[UUID, ...]:
        """Finalize a clip and atomically consume all pending event windows."""

        _non_negative_integer(end_monotonic_ns, "end_monotonic_ns")
        _non_negative_integer(size_bytes, "size_bytes")
        _non_negative_integer(monotonic_now_ns, "monotonic_now_ns")
        with self._transaction():
            row = self._required_clip_row(clip_id)
            lifecycle = ClipLifecycle(str(row["lifecycle"]))
            if lifecycle not in {
                ClipLifecycle.CREATING,
                ClipLifecycle.WRITING,
                ClipLifecycle.FINALIZING,
                ClipLifecycle.FINALIZED,
            }:
                raise CatalogConflictError("clip cannot be finalized from its current state")
            if end_monotonic_ns <= int(row["start_monotonic_ns"]):
                raise ValueError("end_monotonic_ns must be after clip start")
            self._connection.execute(
                """
                UPDATE clips
                SET lifecycle = ?, end_monotonic_ns = ?, size_bytes = ?,
                    updated_catalog_ns = ?
                WHERE clip_id = ?
                """,
                (
                    ClipLifecycle.FINALIZED.value,
                    end_monotonic_ns,
                    size_bytes,
                    monotonic_now_ns,
                    str(clip_id),
                ),
            )
            pending_events = self._connection.execute(
                """
                SELECT e.* FROM protection_events e
                JOIN clips current ON current.clip_id = e.current_clip_id
                WHERE e.remaining_next > 0
                  AND e.current_clip_id <> ?
                  AND current.retention_order < ?
                  AND NOT EXISTS (
                      SELECT 1 FROM protection_event_targets t
                      WHERE t.event_id = e.event_id AND t.clip_id = ?
                  )
                ORDER BY e.triggered_monotonic_ns, e.event_id
                LIMIT ?
                """,
                (
                    str(clip_id),
                    int(row["retention_order"]),
                    str(clip_id),
                    MAX_PENDING_EVENT_WINDOWS,
                ),
            ).fetchall()
            event_ids: list[UUID] = []
            for event in pending_events:
                event_id = UUID(str(event["event_id"]))
                event_ids.append(event_id)
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO protection_event_targets
                        (event_id, clip_id, role, ordinal)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        str(event_id),
                        str(clip_id),
                        EventTargetRole.NEXT.value,
                        int(event["requested_next"]) - int(event["remaining_next"]) + 1,
                    ),
                )
                self._connection.execute(
                    """
                    UPDATE protection_events
                    SET remaining_next = remaining_next - 1
                    WHERE event_id = ?
                    """,
                    (str(event_id),),
                )
            refreshed = self._required_clip_row(clip_id)
            should_protect = bool(refreshed["protected"]) or bool(event_ids)
            if not should_protect:
                return ()
            reason = (
                cast(str | None, refreshed["protection_reason"])
                or f"event:{event_ids[0]}:pending-window"
            )
            intent_id = (
                self._protect_for_event_locked(
                    clip_id,
                    reason=reason,
                    monotonic_now_ns=monotonic_now_ns,
                )
                if event_ids
                else self._prepare_protect_locked(
                    clip_id,
                    reason=reason,
                    monotonic_now_ns=monotonic_now_ns,
                    allow_existing_protection=True,
                )
            )
            return () if intent_id is None else (intent_id,)

    def list_pending_intents(self, *, limit: int) -> tuple[OperationIntent, ...]:
        _row_limit(limit, "limit")
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM operation_intents
                WHERE status = 'PENDING'
                ORDER BY created_monotonic_ns, intent_id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return tuple(_intent_from_row(row) for row in rows)

    def list_pending_delete_intents(self, *, limit: int) -> tuple[OperationIntent, ...]:
        """Return a bounded oldest-first view that cannot be masked by other intents."""

        _row_limit(limit, "limit")
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM operation_intents
                WHERE status = 'PENDING' AND kind = ?
                ORDER BY created_monotonic_ns, intent_id
                LIMIT ?
                """,
                (IntentKind.DELETE.value, limit),
            ).fetchall()
        return tuple(_intent_from_row(row) for row in rows)

    def list_pending_intents_by_kind(
        self,
        *,
        kinds: tuple[IntentKind, ...],
        limit: int,
    ) -> tuple[OperationIntent, ...]:
        """Return a bounded stable view containing only the requested kinds."""

        _row_limit(limit, "limit")
        if (
            not isinstance(kinds, tuple)
            or not kinds
            or len(kinds) > len(IntentKind)
            or any(not isinstance(kind, IntentKind) for kind in kinds)
            or len(set(kinds)) != len(kinds)
        ):
            raise ValueError("kinds must be a non-empty unique IntentKind tuple")
        placeholders = ",".join("?" for _ in kinds)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT * FROM operation_intents
                WHERE status = 'PENDING' AND kind IN ({placeholders})
                ORDER BY created_monotonic_ns, intent_id
                LIMIT ?
                """,
                (*tuple(kind.value for kind in kinds), limit),
            ).fetchall()
        return tuple(_intent_from_row(row) for row in rows)

    def reconcile_intent(
        self,
        intent_id: UUID,
        filesystem: CatalogFilesystem,
        *,
        monotonic_now_ns: int,
        max_actions: int = 2,
    ) -> IntentReconciliationResult:
        """Execute a bounded, idempotently re-plannable pair operation."""

        _uuid(intent_id, "intent_id")
        _non_negative_integer(monotonic_now_ns, "monotonic_now_ns")
        _positive_integer(max_actions, "max_actions")
        with self._transaction():
            row = self._connection.execute(
                "SELECT * FROM operation_intents WHERE intent_id = ?",
                (str(intent_id),),
            ).fetchone()
            if row is None:
                raise CatalogConflictError(f"intent not found: {intent_id}")
            intent = _intent_from_row(row)
            if str(row["status"]) == "COMPLETE":
                return IntentReconciliationResult(intent, 0, True, ())
            plan = _observe_plan(intent, filesystem)
            if plan.problems:
                problems = tuple(problem.code for problem in plan.problems)
                self._connection.execute(
                    "UPDATE operation_intents SET last_problem = ? WHERE intent_id = ?",
                    (";".join(problems), str(intent_id)),
                )
                return IntentReconciliationResult(intent, 0, False, problems)
            if intent.kind is IntentKind.RECONCILE_NAME:
                validation_problem = _prepare_name_reconciliation_sidecar(
                    intent,
                    row,
                    filesystem,
                )
                if validation_problem is not None:
                    self._connection.execute(
                        "UPDATE operation_intents SET last_problem = ? WHERE intent_id = ?",
                        (validation_problem, str(intent_id)),
                    )
                    return IntentReconciliationResult(
                        intent,
                        0,
                        False,
                        (validation_problem,),
                    )
            elif intent.kind is IntentKind.FINALIZE:
                sidecar_path = (
                    intent.paths.sidecar_source
                    if filesystem.exists(intent.paths.sidecar_source)
                    else intent.paths.sidecar_target
                )
                assert sidecar_path is not None
                validation_problem = _validate_finalize_sidecar(
                    intent,
                    filesystem,
                    sidecar_path=sidecar_path,
                )
                if validation_problem is not None:
                    self._connection.execute(
                        "UPDATE operation_intents SET last_problem = ? WHERE intent_id = ?",
                        (validation_problem, str(intent_id)),
                    )
                    return IntentReconciliationResult(
                        intent,
                        0,
                        False,
                        (validation_problem,),
                    )
            if intent.kind is IntentKind.DELETE:
                existing_sources = frozenset(action.source for action in plan.actions)
                for source in (
                    intent.paths.video_source,
                    intent.paths.sidecar_source,
                ):
                    if source not in existing_sources:
                        # Absence after an interrupted unlink is not durable
                        # evidence until the verified parent is fsynced.  The
                        # production unlink seam performs that confirmation
                        # idempotently without affecting other intent kinds.
                        filesystem.unlink(source)
            actions_attempted = 0
            for action in plan.actions[:max_actions]:
                if action.kind is ActionKind.MOVE:
                    assert action.target is not None
                    filesystem.move(action.source, action.target)
                else:
                    filesystem.unlink(action.source)
                actions_attempted += 1
            final_plan = _observe_plan(intent, filesystem)
            problems = tuple(problem.code for problem in final_plan.problems)
            if final_plan.complete:
                self._complete_intent_locked(intent, monotonic_now_ns=monotonic_now_ns)
            else:
                self._connection.execute(
                    "UPDATE operation_intents SET last_problem = ? WHERE intent_id = ?",
                    (None if not problems else ";".join(problems), str(intent_id)),
                )
            return IntentReconciliationResult(
                intent=intent,
                actions_attempted=actions_attempted,
                complete=final_plan.complete,
                problems=problems,
            )

    def reconcile_startup(
        self,
        filesystem_or_root: CatalogFilesystem | Path,
        *,
        monotonic_now_ns: int,
        boot_id: str,
        bounds: ReconciliationBounds = _DEFAULT_RECONCILIATION_BOUNDS,
    ) -> StartupReconciliationReport:
        """Run one bounded recovery pass without deleting unindexed files."""

        filesystem: CatalogFilesystem
        if isinstance(filesystem_or_root, Path):
            filesystem = RootedFilesystem(filesystem_or_root)
        else:
            filesystem = filesystem_or_root
        _non_negative_integer(monotonic_now_ns, "monotonic_now_ns")
        _boot_id(boot_id)
        if not isinstance(bounds, ReconciliationBounds):
            raise TypeError("bounds must be ReconciliationBounds")

        issues: list[str] = []
        actions_attempted = 0
        more_work = False
        expired, leases_more = self.clear_expired_download_leases(
            monotonic_now_ns=monotonic_now_ns,
            boot_id=boot_id,
            limit=bounds.max_expired_leases,
        )
        more_work |= leases_more

        intents = self.list_pending_intents(limit=bounds.max_intents + 1)
        if len(intents) > bounds.max_intents:
            more_work = True
            intents = intents[: bounds.max_intents]
        intents_examined = 0
        for intent in intents:
            if actions_attempted >= bounds.max_actions:
                more_work = True
                break
            result = self.reconcile_intent(
                intent.intent_id,
                filesystem,
                monotonic_now_ns=monotonic_now_ns,
                max_actions=min(2, bounds.max_actions - actions_attempted),
            )
            intents_examined += 1
            actions_attempted += result.actions_attempted
            for problem in result.problems:
                _append_issue(
                    issues,
                    f"intent:{intent.intent_id}:{problem}",
                    maximum=bounds.max_issues,
                )
        if self.list_pending_intents(limit=1):
            more_work = True

        rows = self._catalog_rows_for_recovery(bounds.max_catalog_clips + 1)
        if len(rows) > bounds.max_catalog_clips:
            more_work = True
            rows = rows[: bounds.max_catalog_clips]
        for row in rows:
            clip_id = UUID(str(row["clip_id"]))
            if self._pending_intent_row_threadsafe(clip_id) is not None:
                continue
            video_exists = filesystem.exists(str(row["video_path"]))
            sidecar_exists = filesystem.exists(str(row["sidecar_path"]))
            issue = self._record_pair_observation(
                clip_id,
                video_exists=video_exists,
                sidecar_exists=sidecar_exists,
                monotonic_now_ns=monotonic_now_ns,
            )
            if issue is not None:
                _append_issue(issues, issue, maximum=bounds.max_issues)
            elif self._queue_directory_invariant_repair(clip_id, monotonic_now_ns=monotonic_now_ns):
                more_work = True

        entries_examined = 0
        imported = 0
        remaining = bounds.max_directory_entries
        for directory in _MANAGED_DIRECTORIES:
            if remaining <= 0:
                more_work = True
                break
            paths, examined, truncated = filesystem.iter_files(directory, limit=remaining)
            more_work |= truncated
            entries_examined += examined
            remaining -= examined
            for sidecar_path in (path for path in paths if path.endswith(".json")):
                outcome = self._import_sidecar_if_unindexed(
                    sidecar_path,
                    filesystem=filesystem,
                    maximum_bytes=bounds.max_sidecar_bytes,
                    monotonic_now_ns=monotonic_now_ns,
                )
                if outcome == "IMPORTED":
                    imported += 1
                elif outcome is not None:
                    _append_issue(issues, outcome, maximum=bounds.max_issues)

        # Importing a complete pair from pending can atomically create a new
        # FINALIZE intent after this pass's bounded intent phase has finished.
        # Never report convergence while that durable work remains queued.
        if self.list_pending_intents(limit=1):
            more_work = True
        if len(issues) == bounds.max_issues:
            more_work = True
        return StartupReconciliationReport(
            intents_examined=intents_examined,
            actions_attempted=actions_attempted,
            catalog_clips_examined=len(rows),
            directory_entries_examined=entries_examined,
            imported_clips=imported,
            expired_leases_cleared=expired,
            issues=tuple(issues),
            more_work=more_work,
        )

    def _migrate(self) -> None:
        with self._lock:
            current = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
            if current > SCHEMA_VERSION:
                raise RuntimeError(
                    f"catalog schema {current} is newer than supported {SCHEMA_VERSION}"
                )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE
                )
                """
            )
            for version, name, statements in _MIGRATIONS:
                if version <= current:
                    continue
                self._connection.execute("BEGIN IMMEDIATE")
                try:
                    for statement in statements:
                        self._connection.execute(statement)
                    self._connection.execute(
                        "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                        (version, name),
                    )
                    self._connection.execute(f"PRAGMA user_version = {version}")
                    self._connection.execute("COMMIT")
                except BaseException:
                    self._connection.execute("ROLLBACK")
                    raise
                current = version

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")

    def _required_clip_row(self, clip_id: UUID) -> sqlite3.Row:
        _uuid(clip_id, "clip_id")
        row = self._connection.execute(
            "SELECT * FROM clips WHERE clip_id = ?", (str(clip_id),)
        ).fetchone()
        if row is None:
            raise ClipNotFoundError(str(clip_id))
        return cast(sqlite3.Row, row)

    def _pending_intent_row(self, clip_id: UUID) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            self._connection.execute(
                """
            SELECT * FROM operation_intents
            WHERE clip_id = ? AND status = 'PENDING'
            """,
                (str(clip_id),),
            ).fetchone(),
        )

    def _pending_intent_row_threadsafe(self, clip_id: UUID) -> sqlite3.Row | None:
        with self._lock:
            return self._pending_intent_row(clip_id)

    def _clear_lease_locked(self, clip_id: UUID) -> None:
        self._connection.execute(
            """
            UPDATE clips SET lease_holder = NULL, lease_issued_ns = NULL,
                lease_expires_ns = NULL, lease_boot_id = NULL
            WHERE clip_id = ?
            """,
            (str(clip_id),),
        )

    def _insert_clip_locked(self, clip: CatalogClip, *, catalog_now_ns: int) -> None:
        self._connection.execute(
            """
            INSERT INTO clips (
                clip_id, lifecycle, video_path, sidecar_path,
                start_monotonic_ns, end_monotonic_ns, retention_order,
                size_bytes, protected, protection_reason, pair_reconciled,
                managed, lease_holder, lease_issued_ns, lease_expires_ns,
                lease_boot_id, created_catalog_ns, updated_catalog_ns
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(clip.clip_id),
                clip.lifecycle.value,
                clip.video_path,
                clip.sidecar_path,
                clip.start_monotonic_ns,
                clip.end_monotonic_ns,
                clip.retention_order,
                clip.size_bytes,
                int(clip.protected),
                clip.protection_reason,
                int(clip.pair_reconciled),
                int(clip.managed),
                None if clip.download_lease is None else clip.download_lease.holder,
                (
                    None
                    if clip.download_lease is None
                    else clip.download_lease.issued_at_monotonic_ns
                ),
                (
                    None
                    if clip.download_lease is None
                    else clip.download_lease.expires_at_monotonic_ns
                ),
                clip.lease_boot_id,
                catalog_now_ns,
                catalog_now_ns,
            ),
        )

    def _insert_intent_locked(
        self,
        clip_id: UUID,
        *,
        kind: IntentKind,
        paths: PairPaths,
        monotonic_now_ns: int,
        expected_protection_revision: int | None = None,
    ) -> OperationIntent:
        intent = OperationIntent(
            intent_id=uuid4(),
            clip_id=clip_id,
            kind=kind,
            created_monotonic_ns=monotonic_now_ns,
            paths=paths,
        )
        self._connection.execute(
            """
            INSERT INTO operation_intents (
                intent_id, clip_id, kind, created_monotonic_ns,
                video_source, sidecar_source, video_target, sidecar_target,
                status, last_problem, completed_monotonic_ns,
                expected_protection_revision
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', NULL, NULL, ?)
            """,
            (
                str(intent.intent_id),
                str(intent.clip_id),
                intent.kind.value,
                intent.created_monotonic_ns,
                intent.paths.video_source,
                intent.paths.sidecar_source,
                intent.paths.video_target,
                intent.paths.sidecar_target,
                expected_protection_revision,
            ),
        )
        return intent

    def _insert_existing_intent_locked(
        self,
        intent: OperationIntent,
        *,
        reconciliation_sidecar: bytes | None = None,
        reconciliation_source_sha256: str | None = None,
    ) -> None:
        if not isinstance(intent, OperationIntent):
            raise TypeError("intent must be an OperationIntent")
        self._connection.execute(
            """
            INSERT INTO operation_intents (
                intent_id, clip_id, kind, created_monotonic_ns,
                video_source, sidecar_source, video_target, sidecar_target,
                status, last_problem, completed_monotonic_ns,
                expected_protection_revision, reconciliation_sidecar,
                reconciliation_source_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', NULL, NULL, NULL, ?, ?)
            """,
            (
                str(intent.intent_id),
                str(intent.clip_id),
                intent.kind.value,
                intent.created_monotonic_ns,
                intent.paths.video_source,
                intent.paths.sidecar_source,
                intent.paths.video_target,
                intent.paths.sidecar_target,
                reconciliation_sidecar,
                reconciliation_source_sha256,
            ),
        )

    def _prepare_protect_locked(
        self,
        clip_id: UUID,
        *,
        reason: str,
        monotonic_now_ns: int,
        allow_existing_protection: bool = False,
    ) -> UUID | None:
        _uuid(clip_id, "clip_id")
        row = self._required_clip_row(clip_id)
        lifecycle = ClipLifecycle(str(row["lifecycle"]))
        if lifecycle in {ClipLifecycle.DELETING, ClipLifecycle.DELETED}:
            raise CatalogConflictError("clip no longer exists")
        pending = self._pending_intent_row(clip_id)
        if pending is not None:
            pending_kind = IntentKind(str(pending["kind"]))
            if pending_kind is IntentKind.PROTECT and bool(row["protected"]):
                return UUID(str(pending["intent_id"]))
            if (
                allow_existing_protection
                and bool(row["protected"])
                and pending_kind
                in {
                    IntentKind.FINALIZE,
                    IntentKind.RECONCILE_NAME,
                }
            ):
                return None
            raise CatalogConflictError("clip mutation is already in progress")
        self._connection.execute(
            """
            UPDATE clips
            SET protected = 1, protection_reason = ?,
                protection_revision = protection_revision + 1,
                updated_catalog_ns = ?
            WHERE clip_id = ?
            """,
            (reason, monotonic_now_ns, str(clip_id)),
        )
        if lifecycle is not ClipLifecycle.FINALIZED:
            return None
        video_source = str(row["video_path"])
        sidecar_source = str(row["sidecar_path"])
        if PurePosixPath(video_source).parent.as_posix() == "protected":
            return None
        paths = _move_paths(video_source, sidecar_source, "protected")
        intent = self._insert_intent_locked(
            clip_id,
            kind=IntentKind.PROTECT,
            paths=paths,
            monotonic_now_ns=monotonic_now_ns,
        )
        self._connection.execute(
            """
            UPDATE clips SET pair_reconciled = 0, updated_catalog_ns = ?
            WHERE clip_id = ?
            """,
            (monotonic_now_ns, str(clip_id)),
        )
        return intent.intent_id

    def _protect_for_event_locked(
        self,
        clip_id: UUID,
        *,
        reason: str,
        monotonic_now_ns: int,
    ) -> UUID | None:
        """Durably protect one event target without disrupting its pair intent.

        A protection revision records that an event happened after an in-flight
        unprotect was prepared. Completion can then finish the old filesystem
        move and queue the compensating protect move without losing the event.
        Finalize/name-reconciliation intents similarly finish before the pair is
        moved to the protected directory.
        """

        _uuid(clip_id, "clip_id")
        row = self._required_clip_row(clip_id)
        lifecycle = ClipLifecycle(str(row["lifecycle"]))
        if lifecycle in {ClipLifecycle.DELETING, ClipLifecycle.DELETED}:
            return None
        pending = self._pending_intent_row(clip_id)
        if pending is not None and IntentKind(str(pending["kind"])) is IntentKind.DELETE:
            return None
        self._connection.execute(
            """
            UPDATE clips
            SET protected = 1, protection_reason = ?,
                protection_revision = protection_revision + 1,
                updated_catalog_ns = ?
            WHERE clip_id = ?
            """,
            (reason, monotonic_now_ns, str(clip_id)),
        )
        if pending is not None:
            pending_kind = IntentKind(str(pending["kind"]))
            if pending_kind is IntentKind.PROTECT:
                return UUID(str(pending["intent_id"]))
            # UNPROTECT, FINALIZE, and RECONCILE_NAME are allowed to reach their
            # intended pair state. Their completion queues a fresh PROTECT move.
            return None
        if lifecycle is not ClipLifecycle.FINALIZED:
            return None
        video_source = str(row["video_path"])
        sidecar_source = str(row["sidecar_path"])
        if PurePosixPath(video_source).parent.as_posix() == "protected":
            return None
        intent = self._insert_intent_locked(
            clip_id,
            kind=IntentKind.PROTECT,
            paths=_move_paths(video_source, sidecar_source, "protected"),
            monotonic_now_ns=monotonic_now_ns,
        )
        self._connection.execute(
            """
            UPDATE clips SET pair_reconciled = 0, updated_catalog_ns = ?
            WHERE clip_id = ?
            """,
            (monotonic_now_ns, str(clip_id)),
        )
        return intent.intent_id

    def _complete_intent_locked(self, intent: OperationIntent, *, monotonic_now_ns: int) -> None:
        if intent.kind is IntentKind.DELETE:
            self._connection.execute(
                """
                UPDATE clips
                SET lifecycle = ?, pair_reconciled = 1, size_bytes = 0,
                    lease_holder = NULL, lease_issued_ns = NULL,
                    lease_expires_ns = NULL, lease_boot_id = NULL,
                    updated_catalog_ns = ?
                WHERE clip_id = ?
                """,
                (ClipLifecycle.DELETED.value, monotonic_now_ns, str(intent.clip_id)),
            )
            self._mark_intent_complete_locked(intent, monotonic_now_ns)
            return

        assert intent.paths.video_target is not None
        assert intent.paths.sidecar_target is not None
        if intent.kind is IntentKind.UNPROTECT:
            intent_row = self._connection.execute(
                """
                SELECT expected_protection_revision FROM operation_intents
                WHERE intent_id = ?
                """,
                (str(intent.intent_id),),
            ).fetchone()
            assert intent_row is not None
            expected_revision = cast(int | None, intent_row["expected_protection_revision"])
            clip_row = self._required_clip_row(intent.clip_id)
            current_revision = int(clip_row["protection_revision"])
            newer_protection = expected_revision is None or current_revision > expected_revision
            self._connection.execute(
                """
                UPDATE clips
                SET video_path = ?, sidecar_path = ?,
                    protected = CASE WHEN ? THEN 1 ELSE 0 END,
                    protection_reason = CASE WHEN ? THEN protection_reason ELSE NULL END,
                    pair_reconciled = CASE WHEN ? THEN 0 ELSE 1 END,
                    updated_catalog_ns = ?
                WHERE clip_id = ?
                """,
                (
                    intent.paths.video_target,
                    intent.paths.sidecar_target,
                    newer_protection,
                    newer_protection,
                    newer_protection,
                    monotonic_now_ns,
                    str(intent.clip_id),
                ),
            )
            self._mark_intent_complete_locked(intent, monotonic_now_ns)
            if newer_protection:
                self._insert_intent_locked(
                    intent.clip_id,
                    kind=IntentKind.PROTECT,
                    paths=_move_paths(
                        intent.paths.video_target,
                        intent.paths.sidecar_target,
                        "protected",
                    ),
                    monotonic_now_ns=monotonic_now_ns,
                )
            return

        if intent.kind is IntentKind.PROTECT:
            self._connection.execute(
                """
                UPDATE clips
                SET video_path = ?, sidecar_path = ?, protected = 1,
                    pair_reconciled = 1, updated_catalog_ns = ?
                WHERE clip_id = ?
                """,
                (
                    intent.paths.video_target,
                    intent.paths.sidecar_target,
                    monotonic_now_ns,
                    str(intent.clip_id),
                ),
            )
            self._mark_intent_complete_locked(intent, monotonic_now_ns)
            return

        clip_row = self._required_clip_row(intent.clip_id)
        needs_protect_move = bool(clip_row["protected"]) and (
            PurePosixPath(intent.paths.video_target).parent.as_posix() != "protected"
        )
        self._connection.execute(
            """
            UPDATE clips
            SET video_path = ?, sidecar_path = ?, pair_reconciled = ?,
                lifecycle = ?, updated_catalog_ns = ?
            WHERE clip_id = ?
            """,
            (
                intent.paths.video_target,
                intent.paths.sidecar_target,
                int(not needs_protect_move),
                ClipLifecycle.FINALIZED.value,
                monotonic_now_ns,
                str(intent.clip_id),
            ),
        )
        self._mark_intent_complete_locked(intent, monotonic_now_ns)
        if needs_protect_move:
            self._insert_intent_locked(
                intent.clip_id,
                kind=IntentKind.PROTECT,
                paths=_move_paths(
                    intent.paths.video_target,
                    intent.paths.sidecar_target,
                    "protected",
                ),
                monotonic_now_ns=monotonic_now_ns,
            )

    def _prepare_delete_row_locked(
        self,
        row: sqlite3.Row,
        *,
        monotonic_now_ns: int,
        boot_id: str,
    ) -> UUID:
        clip_id = UUID(str(row["clip_id"]))
        pending = self._pending_intent_row(clip_id)
        if pending is not None:
            if IntentKind(str(pending["kind"])) is IntentKind.DELETE:
                return UUID(str(pending["intent_id"]))
            raise CatalogConflictError("clip mutation is already in progress")
        if ClipLifecycle(str(row["lifecycle"])) is not ClipLifecycle.FINALIZED:
            raise CatalogConflictError("only finalized clips can be deleted")
        if bool(row["protected"]):
            raise CatalogConflictError("protected clips cannot be deleted")
        if not bool(row["pair_reconciled"]) or not bool(row["managed"]):
            raise CatalogConflictError("only reconciled managed clips can be deleted")
        if _row_has_active_lease(
            row,
            monotonic_now_ns=monotonic_now_ns,
            boot_id=boot_id,
        ):
            raise CatalogConflictError("download lease is active")
        paths = PairPaths(str(row["video_path"]), str(row["sidecar_path"]))
        if (
            PurePosixPath(paths.video_source).parent.as_posix() != "clips"
            or PurePosixPath(paths.sidecar_source).parent.as_posix() != "clips"
        ):
            raise CatalogConflictError("retention may delete only reconciled clips/ pairs")
        intent = self._insert_intent_locked(
            clip_id,
            kind=IntentKind.DELETE,
            paths=paths,
            monotonic_now_ns=monotonic_now_ns,
        )
        self._connection.execute(
            """
            UPDATE clips
            SET lifecycle = ?, pair_reconciled = 0,
                lease_holder = NULL, lease_issued_ns = NULL,
                lease_expires_ns = NULL, lease_boot_id = NULL,
                updated_catalog_ns = ?
            WHERE clip_id = ?
            """,
            (ClipLifecycle.DELETING.value, monotonic_now_ns, str(clip_id)),
        )
        return intent.intent_id

    def _mark_intent_complete_locked(self, intent: OperationIntent, monotonic_now_ns: int) -> None:
        self._connection.execute(
            """
            UPDATE operation_intents
            SET status = 'COMPLETE', last_problem = NULL, completed_monotonic_ns = ?
            WHERE intent_id = ?
            """,
            (monotonic_now_ns, str(intent.intent_id)),
        )

    def _catalog_rows_for_recovery(self, limit: int) -> list[sqlite3.Row]:
        with self._lock:
            return self._connection.execute(
                """
                SELECT * FROM clips WHERE lifecycle <> ?
                ORDER BY retention_order, clip_id LIMIT ?
                """,
                (ClipLifecycle.DELETED.value, limit),
            ).fetchall()

    def _queue_directory_invariant_repair(self, clip_id: UUID, *, monotonic_now_ns: int) -> bool:
        """Queue a safe move when durable protection and directory disagree."""

        clip = self.get_clip(clip_id)
        if clip.lifecycle is not ClipLifecycle.FINALIZED:
            return False
        directory = PurePosixPath(clip.video_path).parent.as_posix()
        if clip.protected and directory == "clips":
            intent_id = self.prepare_protect(
                clip_id,
                reason=clip.protection_reason or "recovered protection state",
                monotonic_now_ns=monotonic_now_ns,
            )
            return intent_id is not None
        if not clip.protected and directory == "protected":
            intent_id = self.prepare_unprotect(clip_id, monotonic_now_ns=monotonic_now_ns)
            return intent_id is not None
        return False

    def _record_pair_observation(
        self,
        clip_id: UUID,
        *,
        video_exists: bool,
        sidecar_exists: bool,
        monotonic_now_ns: int,
    ) -> str | None:
        if video_exists and sidecar_exists:
            with self._transaction():
                row = self._required_clip_row(clip_id)
                lifecycle = ClipLifecycle(str(row["lifecycle"]))
                repaired = (
                    ClipLifecycle.FINALIZED
                    if lifecycle in {ClipLifecycle.MISSING_SIDECAR, ClipLifecycle.MISSING_VIDEO}
                    else lifecycle
                )
                self._connection.execute(
                    """
                    UPDATE clips SET lifecycle = ?, pair_reconciled = 1,
                        updated_catalog_ns = ? WHERE clip_id = ?
                    """,
                    (repaired.value, monotonic_now_ns, str(clip_id)),
                )
            return None
        if video_exists:
            observed_lifecycle: ClipLifecycle | None = ClipLifecycle.MISSING_SIDECAR
            issue = f"clip:{clip_id}:MISSING_SIDECAR"
        elif sidecar_exists:
            observed_lifecycle = ClipLifecycle.MISSING_VIDEO
            issue = f"clip:{clip_id}:MISSING_VIDEO"
        else:
            observed_lifecycle = None
            issue = f"clip:{clip_id}:PAIR_MISSING"
        with self._transaction():
            if observed_lifecycle is None:
                self._connection.execute(
                    """
                    UPDATE clips SET pair_reconciled = 0, updated_catalog_ns = ?
                    WHERE clip_id = ?
                    """,
                    (monotonic_now_ns, str(clip_id)),
                )
            else:
                self._connection.execute(
                    """
                    UPDATE clips SET lifecycle = ?, pair_reconciled = 0,
                        updated_catalog_ns = ? WHERE clip_id = ?
                    """,
                    (observed_lifecycle.value, monotonic_now_ns, str(clip_id)),
                )
        return issue

    def _import_sidecar_if_unindexed(
        self,
        sidecar_path: str,
        *,
        filesystem: CatalogFilesystem,
        maximum_bytes: int,
        monotonic_now_ns: int,
    ) -> str | None:
        with self._lock:
            indexed = self._connection.execute(
                "SELECT 1 FROM clips WHERE sidecar_path = ? LIMIT 1",
                (sidecar_path,),
            ).fetchone()
        if indexed is not None:
            return None
        try:
            parsed_name = parse_clip_filename(PurePosixPath(sidecar_path).name)
        except ClipNameError:
            # A JSON file is not managed merely because it is located beside
            # clips. Windows/application metadata remains untouched and silent.
            return None
        try:
            if parsed_name.extension != "json":
                return None
            sidecar = parse_sidecar_bytes(
                filesystem.read_bytes(sidecar_path, maximum_bytes=maximum_bytes)
            )
            clip_id = sidecar.clip_id
            video_name = sidecar.video_file
            metadata_name = sidecar.metadata_file
            ClipFilePair(video_name=video_name, metadata_name=metadata_name)
            if metadata_name != PurePosixPath(sidecar_path).name:
                raise ValueError("sidecar filename disagrees with metadata")
            start_ns = sidecar.start_monotonic_ns
            end_ns = sidecar.end_monotonic_ns
            protected_value = sidecar.protected
            directory = PurePosixPath(sidecar_path).parent.as_posix()
            protected = protected_value or directory == "protected"
            video_path = PurePosixPath(directory, video_name).as_posix()
            pair_exists = filesystem.exists(video_path)
            if directory == "pending":
                recovered_lifecycle = (
                    ClipLifecycle.FINALIZING if pair_exists else ClipLifecycle.MISSING_VIDEO
                )
                pair_reconciled = False
            elif directory == "quarantine":
                recovered_lifecycle = ClipLifecycle.QUARANTINED
                pair_reconciled = pair_exists
            else:
                recovered_lifecycle = (
                    ClipLifecycle.FINALIZED if pair_exists else ClipLifecycle.MISSING_VIDEO
                )
                pair_reconciled = pair_exists
            try:
                self.get_clip(clip_id)
                return None
            except ClipNotFoundError:
                pass
            clip = CatalogClip(
                clip_id=clip_id,
                lifecycle=recovered_lifecycle,
                video_path=video_path,
                sidecar_path=sidecar_path,
                start_monotonic_ns=start_ns,
                end_monotonic_ns=end_ns,
                retention_order=start_ns,
                size_bytes=filesystem.file_size(video_path) if pair_exists else 0,
                protected=protected,
                protection_reason=(
                    sidecar.protection_reason
                    if protected_value
                    else ("recovered from protected directory" if protected else None)
                ),
                pair_reconciled=pair_reconciled,
                managed=True,
            )
            self.register_clip(clip, catalog_now_ns=monotonic_now_ns)
            return "IMPORTED"
        except (
            ClipNameError,
            KeyError,
            TypeError,
            ValueError,
            UnicodeDecodeError,
            OSError,
            SidecarParseError,
            sqlite3.IntegrityError,
        ) as exc:
            return f"unindexed:{sidecar_path}:{type(exc).__name__}"


def _clip_from_row(row: sqlite3.Row) -> CatalogClip:
    holder = cast(str | None, row["lease_holder"])
    lease = (
        None
        if holder is None
        else DownloadLease(
            holder=holder,
            issued_at_monotonic_ns=int(row["lease_issued_ns"]),
            expires_at_monotonic_ns=int(row["lease_expires_ns"]),
        )
    )
    return CatalogClip(
        clip_id=UUID(str(row["clip_id"])),
        lifecycle=ClipLifecycle(str(row["lifecycle"])),
        video_path=str(row["video_path"]),
        sidecar_path=str(row["sidecar_path"]),
        start_monotonic_ns=int(row["start_monotonic_ns"]),
        end_monotonic_ns=(
            None if row["end_monotonic_ns"] is None else int(row["end_monotonic_ns"])
        ),
        retention_order=int(row["retention_order"]),
        size_bytes=int(row["size_bytes"]),
        protected=bool(row["protected"]),
        protection_reason=cast(str | None, row["protection_reason"]),
        pair_reconciled=bool(row["pair_reconciled"]),
        managed=bool(row["managed"]),
        download_lease=lease,
        lease_boot_id=cast(str | None, row["lease_boot_id"]),
    )


def _intent_from_row(row: sqlite3.Row) -> OperationIntent:
    return OperationIntent(
        intent_id=UUID(str(row["intent_id"])),
        clip_id=UUID(str(row["clip_id"])),
        kind=IntentKind(str(row["kind"])),
        created_monotonic_ns=int(row["created_monotonic_ns"]),
        paths=PairPaths(
            video_source=str(row["video_source"]),
            sidecar_source=str(row["sidecar_source"]),
            video_target=cast(str | None, row["video_target"]),
            sidecar_target=cast(str | None, row["sidecar_target"]),
        ),
    )


def _observe_plan(intent: OperationIntent, filesystem: CatalogFilesystem) -> ReconciliationPlan:
    video_target = (
        False if intent.paths.video_target is None else filesystem.exists(intent.paths.video_target)
    )
    sidecar_target = (
        False
        if intent.paths.sidecar_target is None
        else filesystem.exists(intent.paths.sidecar_target)
    )
    return plan_reconciliation(
        intent,
        video=MemberObservation(
            source_exists=filesystem.exists(intent.paths.video_source),
            target_exists=video_target,
        ),
        sidecar=MemberObservation(
            source_exists=filesystem.exists(intent.paths.sidecar_source),
            target_exists=sidecar_target,
        ),
    )


def _validate_finalize_sidecar(
    intent: OperationIntent,
    filesystem: CatalogFilesystem,
    *,
    sidecar_path: str,
) -> str | None:
    """Validate the durable logical-pair identity before any FINALIZE move."""

    try:
        sidecar = parse_sidecar_bytes(
            filesystem.read_bytes(sidecar_path, maximum_bytes=MAX_SIDECAR_BYTES)
        )
        assert intent.paths.video_target is not None
        assert intent.paths.sidecar_target is not None
        if (
            sidecar.clip_id != intent.clip_id
            or sidecar.video_file != PurePosixPath(intent.paths.video_target).name
            or sidecar.metadata_file != PurePosixPath(intent.paths.sidecar_target).name
        ):
            return "FINALIZE_SIDECAR_IDENTITY_MISMATCH"
    except (OSError, ValueError, SidecarParseError):
        return "INVALID_FINALIZE_SIDECAR"
    return None


def _prepare_name_reconciliation_sidecar(
    intent: OperationIntent,
    intent_row: sqlite3.Row,
    filesystem: CatalogFilesystem,
) -> str | None:
    """Recoverably install the target metadata before moving the pair names."""

    raw_payload = intent_row["reconciliation_sidecar"]
    raw_source_hash = intent_row["reconciliation_source_sha256"]
    if (
        not isinstance(raw_payload, bytes)
        or not raw_payload
        or len(raw_payload) > MAX_SIDECAR_BYTES
        or not isinstance(raw_source_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", raw_source_hash) is None
    ):
        return "NAME_RECONCILIATION_RECOVERY_DATA_MISSING"
    try:
        target_sidecar = parse_sidecar_bytes(raw_payload)
        video_target = cast(str, intent.paths.video_target)
        sidecar_target = cast(str, intent.paths.sidecar_target)
        if (
            target_sidecar.clip_id != intent.clip_id
            or target_sidecar.video_file != PurePosixPath(video_target).name
            or target_sidecar.metadata_file != PurePosixPath(sidecar_target).name
        ):
            return "NAME_RECONCILIATION_TARGET_IDENTITY_MISMATCH"

        if filesystem.exists(intent.paths.sidecar_source):
            current_payload = filesystem.read_bytes(
                intent.paths.sidecar_source,
                maximum_bytes=MAX_SIDECAR_BYTES,
            )
            if current_payload != raw_payload:
                if hashlib.sha256(current_payload).hexdigest() != raw_source_hash:
                    return "NAME_RECONCILIATION_SOURCE_CHANGED"
                current_sidecar = parse_sidecar_bytes(current_payload)
                if (
                    current_sidecar.clip_id != intent.clip_id
                    or current_sidecar.video_file
                    != PurePosixPath(intent.paths.video_source).name
                    or current_sidecar.metadata_file
                    != PurePosixPath(intent.paths.sidecar_source).name
                ):
                    return "NAME_RECONCILIATION_SOURCE_IDENTITY_MISMATCH"
                filesystem.replace_bytes_atomic(
                    intent.paths.sidecar_source,
                    raw_payload,
                    maximum_bytes=MAX_SIDECAR_BYTES,
                )
                if (
                    filesystem.read_bytes(
                        intent.paths.sidecar_source,
                        maximum_bytes=MAX_SIDECAR_BYTES,
                    )
                    != raw_payload
                ):
                    return "NAME_RECONCILIATION_REPLACE_READBACK_MISMATCH"
        elif filesystem.exists(sidecar_target):
            if (
                filesystem.read_bytes(sidecar_target, maximum_bytes=MAX_SIDECAR_BYTES)
                != raw_payload
            ):
                return "NAME_RECONCILIATION_TARGET_PAYLOAD_MISMATCH"
    except (OSError, ValueError, SidecarParseError):
        return "INVALID_NAME_RECONCILIATION_SIDECAR"
    return None


def _move_paths(video_source: str, sidecar_source: str, target_directory: str) -> PairPaths:
    return PairPaths(
        video_source=video_source,
        sidecar_source=sidecar_source,
        video_target=PurePosixPath(target_directory, PurePosixPath(video_source).name).as_posix(),
        sidecar_target=PurePosixPath(
            target_directory, PurePosixPath(sidecar_source).name
        ).as_posix(),
    )


def _validate_clip(clip: CatalogClip) -> None:
    if not isinstance(clip, CatalogClip):
        raise TypeError("clip must be CatalogClip")
    _uuid(clip.clip_id, "clip_id")
    if not isinstance(clip.lifecycle, ClipLifecycle):
        raise TypeError("lifecycle must be ClipLifecycle")
    PairPaths(clip.video_path, clip.sidecar_path)
    ClipFilePair(
        video_name=PurePosixPath(clip.video_path).name,
        metadata_name=PurePosixPath(clip.sidecar_path).name,
    )
    if PurePosixPath(clip.video_path).parent != PurePosixPath(clip.sidecar_path).parent:
        raise ValueError("clip pair members must share a managed directory")
    if PurePosixPath(clip.video_path).parent.as_posix() not in _MANAGED_DIRECTORIES:
        raise ValueError("clip paths must use a managed directory")
    _non_negative_integer(clip.start_monotonic_ns, "start_monotonic_ns")
    if clip.end_monotonic_ns is not None:
        _non_negative_integer(clip.end_monotonic_ns, "end_monotonic_ns")
        if clip.end_monotonic_ns <= clip.start_monotonic_ns:
            raise ValueError("end_monotonic_ns must be after start")
    _non_negative_integer(clip.retention_order, "retention_order")
    _non_negative_integer(clip.size_bytes, "size_bytes")
    for name, value in (
        ("protected", clip.protected),
        ("pair_reconciled", clip.pair_reconciled),
        ("managed", clip.managed),
    ):
        if not isinstance(value, bool):
            raise TypeError(f"{name} must be boolean")
    if clip.lifecycle is ClipLifecycle.FINALIZED and clip.end_monotonic_ns is None:
        raise ValueError("finalized clip requires end_monotonic_ns")
    if clip.protected:
        if clip.protection_reason is None:
            raise ValueError("protected clip requires a protection reason")
        _reason(clip.protection_reason)
    elif clip.protection_reason is not None:
        raise ValueError("unprotected clip cannot have a protection reason")
    if clip.download_lease is None and clip.lease_boot_id is not None:
        raise ValueError("lease_boot_id requires a lease")
    if clip.download_lease is not None:
        if clip.lifecycle is not ClipLifecycle.FINALIZED:
            raise ValueError("only a finalized clip may have a download lease")
        if clip.lease_boot_id is None:
            raise ValueError("download lease requires lease_boot_id")
        _boot_id(clip.lease_boot_id)


def _row_has_active_lease(row: sqlite3.Row, *, monotonic_now_ns: int, boot_id: str) -> bool:
    return (
        row["lease_holder"] is not None
        and str(row["lease_boot_id"]) == boot_id
        and monotonic_now_ns < int(row["lease_expires_ns"])
    )


def _append_issue(values: list[str], issue: str, *, maximum: int) -> None:
    if len(values) < maximum:
        values.append(issue)


def _reason(reason: str) -> None:
    if not isinstance(reason, str) or not reason or len(reason) > _MAX_REASON_CHARS:
        raise ValueError("protection reason must be a non-empty bounded string")
    if not reason.isascii() or any(ord(character) < 32 for character in reason):
        raise ValueError("protection reason must be printable ASCII")


def _boot_id(boot_id: str) -> None:
    if (
        not isinstance(boot_id, str)
        or len(boot_id) > _MAX_BOOT_ID_CHARS
        or _BOOT_ID_RE.fullmatch(boot_id) is None
    ):
        raise ValueError("boot_id must be a bounded safe identifier")


def _uuid(value: object, name: str) -> UUID:
    if not isinstance(value, UUID):
        raise TypeError(f"{name} must be a UUID")
    return value


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _row_limit(value: object, name: str) -> int:
    result = _positive_integer(value, name)
    if result > MAX_QUERY_ROWS:
        raise ValueError(f"{name} exceeds {MAX_QUERY_ROWS} row bound")
    return result


def _non_negative_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _bounded_count(value: object, name: str) -> int:
    _non_negative_integer(value, name)
    if cast(int, value) > 32:
        raise ValueError(f"{name} exceeds bound")
    return cast(int, value)
