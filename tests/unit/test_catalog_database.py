from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from dashcam.catalog import (
    MAX_PENDING_EVENT_WINDOWS,
    MAX_QUERY_ROWS,
    SCHEMA_VERSION,
    CatalogClip,
    CatalogConflictError,
    ClipCatalog,
    EventSource,
    RootedFilesystem,
)
from dashcam.catalog.database import (
    ActiveClipProtectionChanged,
    RetentionThresholdLatch,
)
from dashcam.metadata.schema import (
    AudioSummary,
    ClipSidecar,
    GpsSummary,
    TimeAnchor,
    TimeAnchorSource,
    VideoSummary,
)
from dashcam.state import (
    MAX_DOWNLOAD_LEASE_NS,
    ClipLifecycle,
    DownloadLeaseError,
    GpsTimeState,
    SystemClockState,
    TimestampQuality,
)
from dashcam.storage.intents import IntentKind, PairPaths
from dashcam.storage.naming import (
    finalized_clip_pair,
    finalized_unsynced_clip_pair,
    provisional_clip_pair,
)


def _clip(
    number: int,
    *,
    lifecycle: ClipLifecycle = ClipLifecycle.FINALIZED,
    protected: bool = False,
    directory: str = "clips",
) -> CatalogClip:
    pair = finalized_clip_pair(
        utc_started_at=datetime(2026, 7, 24, 0, 0, number, tzinfo=UTC),
        boot_id="abcde",
        sequence=number,
    )
    return CatalogClip(
        clip_id=UUID(int=number),
        lifecycle=lifecycle,
        video_path=f"{directory}/{pair.video_name}",
        sidecar_path=f"{directory}/{pair.metadata_name}",
        start_monotonic_ns=number * 100,
        end_monotonic_ns=(number * 100 + 50 if lifecycle is ClipLifecycle.FINALIZED else None),
        retention_order=number,
        size_bytes=100,
        protected=protected,
        protection_reason="fixture" if protected else None,
        pair_reconciled=lifecycle is ClipLifecycle.FINALIZED,
        managed=True,
    )


def _unsynced_clip(
    number: int,
    boot_id: UUID,
    *,
    directory: str = "clips",
) -> CatalogClip:
    pair = finalized_unsynced_clip_pair(
        boot_id=boot_id.hex[:12],
        sequence=number,
    )
    return CatalogClip(
        clip_id=UUID(int=10_000 + number),
        lifecycle=ClipLifecycle.FINALIZED,
        video_path=f"{directory}/{pair.video_name}",
        sidecar_path=f"{directory}/{pair.metadata_name}",
        start_monotonic_ns=number * 1_000,
        end_monotonic_ns=number * 1_000 + 500,
        retention_order=number,
        size_bytes=100,
        protected=directory == "protected",
        protection_reason="fixture" if directory == "protected" else None,
        pair_reconciled=True,
        managed=True,
    )


def _writing_clip(number: int) -> CatalogClip:
    pair = provisional_clip_pair(boot_id="abcde", sequence=number)
    return CatalogClip(
        clip_id=UUID(int=number),
        lifecycle=ClipLifecycle.WRITING,
        video_path=f"pending/{pair.video_name}",
        sidecar_path=f"pending/{pair.metadata_name}",
        start_monotonic_ns=number * 100,
        end_monotonic_ns=None,
        retention_order=number,
        size_bytes=0,
        protected=False,
        protection_reason=None,
        pair_reconciled=False,
        managed=True,
    )


def _finalizing_fixture() -> tuple[CatalogClip, PairPaths, bytes]:
    clip_id = UUID(int=1)
    started_at = datetime(2026, 7, 24, 0, 0, 1, tzinfo=UTC)
    source = provisional_clip_pair(boot_id="abcde", sequence=1)
    target = finalized_clip_pair(
        utc_started_at=started_at,
        boot_id="abcde",
        sequence=1,
    )
    clip = CatalogClip(
        clip_id=clip_id,
        lifecycle=ClipLifecycle.FINALIZING,
        video_path=f"pending/{source.video_name}",
        sidecar_path=f"pending/{source.metadata_name}",
        start_monotonic_ns=100,
        end_monotonic_ns=1_000_000_100,
        retention_order=1,
        size_bytes=5,
        protected=False,
        protection_reason=None,
        pair_reconciled=False,
        managed=True,
    )
    paths = PairPaths(
        clip.video_path,
        clip.sidecar_path,
        f"clips/{target.video_name}",
        f"clips/{target.metadata_name}",
    )
    sidecar = ClipSidecar(
        schema_version=1,
        clip_id=clip_id,
        boot_id=UUID("00000000-0000-0000-0000-000000000001"),
        sequence=1,
        video_file=target.video_name,
        metadata_file=target.metadata_name,
        start_utc=started_at,
        end_utc=started_at + timedelta(seconds=1),
        start_monotonic_ns=100,
        end_monotonic_ns=1_000_000_100,
        gps_time_state=GpsTimeState.UNSYNCED,
        system_clock_state=SystemClockState.SYNCHRONIZED,
        timestamp_quality=TimestampQuality.SYSTEM_DERIVED,
        time_anchor=TimeAnchor(
            TimeAnchorSource.SYSTEM_CLOCK,
            100,
            started_at,
            1,
            "catalog test",
        ),
        timezone="UTC",
        start_local=started_at,
        video=VideoSummary("h264", 1920, 1080, 30.0, 8_000_000, 8_000_000, 1, 0),
        audio=AudioSummary(False, None, None, None, None),
        gps=GpsSummary(False, None),
        protected=False,
        protection_reason=None,
        software_version="test",
    )
    return clip, paths, sidecar.to_canonical_json()


def test_oldest_delete_uses_exact_boot_epoch_for_active_lease(tmp_path: Path) -> None:
    full_boot_id = "12345678-1234-5678-9234-567812345678"
    with ClipCatalog(tmp_path / "catalog.sqlite3") as catalog:
        catalog.register_clip(_clip(1), catalog_now_ns=1)
        catalog.register_clip(_clip(2), catalog_now_ns=2)
        catalog.acquire_download_lease(
            UUID(int=1),
            holder="download",
            monotonic_now_ns=10,
            duration_ns=1_000,
            boot_id=full_boot_id,
        )

        intent_id = catalog.prepare_oldest_eligible_delete(
            monotonic_now_ns=11,
            boot_id=full_boot_id,
        )

        assert intent_id is not None
        assert catalog.list_pending_delete_intents(limit=1)[0].clip_id == UUID(int=2)


def test_previous_boot_lease_does_not_mask_oldest_clip(tmp_path: Path) -> None:
    with ClipCatalog(tmp_path / "catalog.sqlite3") as catalog:
        catalog.register_clip(_clip(1), catalog_now_ns=1)
        catalog.register_clip(_clip(2), catalog_now_ns=2)
        catalog.acquire_download_lease(
            UUID(int=1),
            holder="download",
            monotonic_now_ns=10,
            duration_ns=1_000,
            boot_id="previous-boot",
        )

        catalog.prepare_oldest_eligible_delete(
            monotonic_now_ns=11,
            boot_id="current-boot",
        )

        assert catalog.list_pending_delete_intents(limit=1)[0].clip_id == UUID(int=1)


def test_delete_refuses_unprotected_row_whose_pair_is_in_protected_directory(
    tmp_path: Path,
) -> None:
    with ClipCatalog(tmp_path / "catalog.sqlite3") as catalog:
        catalog.register_clip(
            _clip(1, protected=False, directory="protected"),
            catalog_now_ns=1,
        )

        with pytest.raises(CatalogConflictError, match="clips/ pairs"):
            catalog.prepare_delete(
                UUID(int=1),
                monotonic_now_ns=2,
                boot_id="current-boot",
            )


def test_kind_filtered_pending_query_is_stable_and_cannot_be_masked_by_delete(
    tmp_path: Path,
) -> None:
    with ClipCatalog(tmp_path / "catalog.sqlite3") as catalog:
        catalog.register_clip(_clip(1), catalog_now_ns=1)
        catalog.register_clip(_clip(2), catalog_now_ns=2)
        catalog.register_clip(
            _clip(3, protected=True, directory="protected"),
            catalog_now_ns=3,
        )
        catalog.prepare_delete(UUID(int=1), monotonic_now_ns=10, boot_id="boot-a")
        protect_id = catalog.prepare_protect(
            UUID(int=2),
            reason="event",
            monotonic_now_ns=11,
        )
        unprotect_id = catalog.prepare_unprotect(UUID(int=3), monotonic_now_ns=12)
        assert protect_id is not None and unprotect_id is not None

        filtered = catalog.list_pending_intents_by_kind(
            kinds=(IntentKind.PROTECT, IntentKind.UNPROTECT),
            limit=2,
        )

        assert tuple(intent.intent_id for intent in filtered) == (
            protect_id,
            unprotect_id,
        )
        assert all(intent.kind is not IntentKind.DELETE for intent in filtered)
        assert catalog.get_pending_intent(protect_id) == filtered[0]
        assert catalog.get_pending_intent(UUID(int=999)) is None
        assert catalog.list_pending_intents_for_clip(
            UUID(int=3),
            kinds=(IntentKind.PROTECT, IntentKind.UNPROTECT),
            limit=1,
        ) == (filtered[1],)
        with pytest.raises(ValueError, match="unique IntentKind"):
            catalog.list_pending_intents_by_kind(kinds=(), limit=1)
        with pytest.raises(ValueError, match="unique IntentKind"):
            catalog.list_pending_intents_by_kind(
                kinds=(IntentKind.PROTECT, IntentKind.PROTECT),
                limit=1,
            )
        with pytest.raises(ValueError, match="unique IntentKind"):
            catalog.list_pending_intents_for_clip(
                UUID(int=2),
                kinds=(),
                limit=1,
            )


def test_migrations_are_explicit_versioned_and_reopen_cleanly(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"

    with ClipCatalog(database) as catalog:
        assert catalog.schema_version == SCHEMA_VERSION
        catalog.register_clip(_clip(1), catalog_now_ns=1)

    with ClipCatalog(database) as reopened:
        assert reopened.schema_version == SCHEMA_VERSION
        assert reopened.get_clip(UUID(int=1)) == _clip(1)


def test_metadata_candidates_page_all_current_boot_pairs_across_process_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "catalog.sqlite3"
    boot_id = UUID("12345678-1234-5678-9234-567812345678")
    other_boot = UUID("87654321-4321-6789-a234-678943216789")
    with ClipCatalog(database) as catalog:
        for number in range(70):
            catalog.register_clip(
                _unsynced_clip(
                    number,
                    boot_id,
                    directory="protected" if number == 69 else "clips",
                )
            )
        catalog.register_clip(_unsynced_clip(70, other_boot))
        catalog.register_clip(replace(_clip(8), retention_order=80))

        first = catalog.list_metadata_reconciliation_candidates(
            boot_id,
            limit=64,
        )
        assert len(first) == 64
        second = catalog.list_metadata_reconciliation_candidates(
            boot_id,
            limit=64,
            after_order=first[-1].retention_order,
            after_clip_id=first[-1].clip_id,
        )
        assert len(second) == 6
        assert {clip.clip_id for clip in first + second} == {
            UUID(int=10_000 + number) for number in range(70)
        }

    with ClipCatalog(database) as reopened:
        restarted = reopened.list_metadata_reconciliation_candidates(
            boot_id,
            limit=64,
        )
        assert restarted == first
        assert (
            reopened.list_metadata_reconciliation_candidates(
                other_boot,
                limit=64,
            )
            == (_unsynced_clip(70, other_boot),)
        )

    with sqlite3.connect(database) as connection:
        migrations = connection.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
    assert migrations == [
        (1, "create_clip_catalog"),
        (2, "add_event_protection"),
        (3, "add_protection_revisions"),
        (4, "add_name_reconciliation_payload"),
        (5, "add_retention_threshold_latch"),
    ]
    assert journal_mode == ("wal",)


def test_finalizing_registration_atomically_queues_and_reconciles_pair(
    tmp_path: Path,
) -> None:
    root = tmp_path / "recording"
    for directory in ("pending", "clips", "protected", "quarantine"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    clip, paths, sidecar = _finalizing_fixture()
    (root / clip.video_path).write_bytes(b"video")
    (root / clip.sidecar_path).write_bytes(sidecar)
    filesystem = RootedFilesystem(root)

    with ClipCatalog(tmp_path / "catalog.sqlite3") as catalog:
        intent_id = catalog.register_finalizing_clip(
            clip,
            promotion_paths=paths,
            monotonic_now_ns=200,
        )
        intent = catalog.list_pending_intents(limit=1)[0]
        assert intent.intent_id == intent_id
        assert intent.kind is IntentKind.FINALIZE
        assert intent.paths.video_source == clip.video_path
        assert intent.paths.video_target is not None
        assert intent.paths.video_target.startswith("clips/")

        result = catalog.reconcile_intent(
            intent_id,
            filesystem,
            monotonic_now_ns=201,
            max_actions=2,
        )
        finalized = catalog.get_clip(clip.clip_id)

    assert result.complete
    assert finalized.lifecycle is ClipLifecycle.FINALIZED
    assert finalized.pair_reconciled
    assert not (root / clip.video_path).exists()
    assert not (root / clip.sidecar_path).exists()
    assert (root / finalized.video_path).read_bytes() == b"video"
    assert (root / finalized.sidecar_path).read_bytes() == sidecar


def test_finalizing_registration_refuses_non_pending_or_incomplete_state(
    tmp_path: Path,
) -> None:
    base, paths, _ = _finalizing_fixture()
    with (
        ClipCatalog(tmp_path / "catalog.sqlite3") as catalog,
        pytest.raises(CatalogConflictError, match="finalization registration"),
    ):
        catalog.register_finalizing_clip(
            replace(base, end_monotonic_ns=None),
            promotion_paths=paths,
            monotonic_now_ns=200,
        )


def test_finalizing_registration_latches_target_collision_without_mutation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "recording"
    for directory in ("pending", "clips", "protected", "quarantine"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    clip, paths, sidecar = _finalizing_fixture()
    (root / clip.video_path).write_bytes(b"source-video")
    (root / clip.sidecar_path).write_bytes(sidecar)
    assert paths.video_target is not None
    target_video = root / paths.video_target
    target_video.write_bytes(b"existing-video")

    with ClipCatalog(tmp_path / "catalog.sqlite3") as catalog:
        intent_id = catalog.register_finalizing_clip(
            clip,
            promotion_paths=paths,
            monotonic_now_ns=200,
        )
        result = catalog.reconcile_intent(
            intent_id,
            RootedFilesystem(root),
            monotonic_now_ns=201,
            max_actions=2,
        )
        pending = catalog.list_pending_intents(limit=1)

    assert not result.complete
    assert result.actions_attempted == 0
    assert result.problems == ("SOURCE_TARGET_CONFLICT",)
    assert pending[0].intent_id == intent_id
    assert (root / clip.video_path).read_bytes() == b"source-video"
    assert (root / clip.sidecar_path).read_bytes() == sidecar
    assert target_video.read_bytes() == b"existing-video"


def test_catalog_refuses_a_newer_database_schema(tmp_path: Path) -> None:
    database = tmp_path / "future.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")

    with pytest.raises(RuntimeError, match="newer than supported"):
        ClipCatalog(database)


def test_retention_latch_is_transactional_and_refuses_binding_overwrite(
    tmp_path: Path,
) -> None:
    database = tmp_path / "catalog.sqlite3"
    first = RetentionThresholdLatch("7EED-3EA7", 24_000_000_000, False)
    reclaiming = replace(first, reclaim_latched=True)

    with ClipCatalog(database) as catalog:
        assert catalog.retention_threshold_latch() is None
        catalog.store_retention_threshold_latch(first)
        catalog.store_retention_threshold_latch(reclaiming)
        assert catalog.retention_threshold_latch() == reclaiming
        with pytest.raises(CatalogConflictError, match="binding differs"):
            catalog.store_retention_threshold_latch(
                replace(reclaiming, volume_uuid="FOREIGN")
            )
        assert catalog.retention_threshold_latch() == reclaiming

    with ClipCatalog(database) as reopened:
        assert reopened.retention_threshold_latch() == reclaiming


def test_retention_order_is_unique_and_reads_have_a_hard_row_bound(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    with ClipCatalog(database) as catalog:
        catalog.register_clip(_clip(1))
        duplicate_order = replace(_clip(2), retention_order=1)
        with pytest.raises(sqlite3.IntegrityError):
            catalog.register_clip(duplicate_order)
        with pytest.raises(ValueError, match="row bound"):
            catalog.list_clips(limit=MAX_QUERY_ROWS + 1)


def test_download_lease_is_bounded_expires_and_has_a_boot_epoch(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    with ClipCatalog(database) as catalog:
        catalog.register_clip(_clip(1))
        lease = catalog.acquire_download_lease(
            UUID(int=1),
            holder="web:client-1",
            monotonic_now_ns=1_000,
            duration_ns=100,
            boot_id="boot-a",
        )
        assert lease.expires_at_monotonic_ns == 1_100
        with pytest.raises(DownloadLeaseError, match="already has"):
            catalog.acquire_download_lease(
                UUID(int=1),
                holder="web:client-2",
                monotonic_now_ns=1_099,
                duration_ns=100,
                boot_id="boot-a",
            )
        replacement = catalog.acquire_download_lease(
            UUID(int=1),
            holder="web:client-2",
            monotonic_now_ns=1_100,
            duration_ns=100,
            boot_id="boot-a",
        )
        assert replacement.holder == "web:client-2"
        cleared, more = catalog.clear_expired_download_leases(
            monotonic_now_ns=5,
            boot_id="boot-b",
            limit=1,
        )
        assert (cleared, more) == (1, False)
        assert catalog.get_clip(UUID(int=1)).download_lease is None

        with pytest.raises(DownloadLeaseError):
            catalog.acquire_download_lease(
                UUID(int=1),
                holder="client",
                monotonic_now_ns=0,
                duration_ns=MAX_DOWNLOAD_LEASE_NS + 1,
                boot_id="boot-b",
            )


def test_download_lease_global_cap_is_transactional_and_restart_durable(
    tmp_path: Path,
) -> None:
    database = tmp_path / "catalog.sqlite3"
    with ClipCatalog(database) as catalog:
        catalog.register_clip(_clip(1))
        catalog.register_clip(_clip(2))
        catalog.acquire_download_lease(
            UUID(int=1),
            holder="control-opaque-one",
            monotonic_now_ns=100,
            duration_ns=1_000,
            boot_id="boot-a",
            max_active_leases=1,
        )

    with ClipCatalog(database) as restarted:
        with pytest.raises(CatalogConflictError, match="global download lease limit"):
            restarted.acquire_download_lease(
                UUID(int=2),
                holder="control-opaque-two",
                monotonic_now_ns=101,
                duration_ns=1_000,
                boot_id="boot-a",
                max_active_leases=1,
            )
        assert restarted.release_download_lease(
            UUID(int=1),
            holder="control-opaque-one",
            monotonic_now_ns=102,
        )
        assert not restarted.release_download_lease(
            UUID(int=1),
            holder="control-opaque-one",
            monotonic_now_ns=102,
        )
        restarted.acquire_download_lease(
            UUID(int=2),
            holder="control-opaque-two",
            monotonic_now_ns=102,
            duration_ns=1_000,
            boot_id="boot-a",
            max_active_leases=1,
        )
        # A previous-boot row is stale immediately and cannot consume this
        # boot's global lease capacity.
        restarted.acquire_download_lease(
            UUID(int=1),
            holder="control-opaque-three",
            monotonic_now_ns=1,
            duration_ns=1_000,
            boot_id="boot-b",
            max_active_leases=1,
        )


def test_delete_and_download_acquisition_cannot_race_through_catalog_state(
    tmp_path: Path,
) -> None:
    with ClipCatalog(tmp_path / "catalog.sqlite3") as catalog:
        catalog.register_clip(_clip(1))
        catalog.acquire_download_lease(
            UUID(int=1),
            holder="client",
            monotonic_now_ns=100,
            duration_ns=100,
            boot_id="boot-a",
        )
        with pytest.raises(CatalogConflictError, match="lease"):
            catalog.prepare_delete(UUID(int=1), monotonic_now_ns=150, boot_id="boot-a")
        intent_id = catalog.prepare_delete(UUID(int=1), monotonic_now_ns=200, boot_id="boot-a")
        assert catalog.get_clip(UUID(int=1)).lifecycle is ClipLifecycle.DELETING
        assert catalog.list_pending_intents(limit=1)[0].intent_id == intent_id


def test_active_lease_freezes_manual_and_event_pair_moves_until_release_or_expiry(
    tmp_path: Path,
) -> None:
    with ClipCatalog(tmp_path / "catalog.sqlite3") as catalog:
        catalog.register_clip(_clip(1), catalog_now_ns=1)
        catalog.acquire_download_lease(
            UUID(int=1),
            holder="control-event",
            monotonic_now_ns=100,
            duration_ns=1_000,
            boot_id="boot-a",
        )
        with pytest.raises(DownloadLeaseError, match="freezes"):
            catalog.prepare_protect(UUID(int=1), reason="manual", monotonic_now_ns=200)

        event = catalog.trigger_event(
            UUID(int=1),
            source=EventSource.WEB,
            monotonic_now_ns=201,
            previous_count=0,
            next_count=0,
        )
        leased = catalog.get_clip(UUID(int=1))
        assert leased.protected
        assert leased.video_path.startswith("clips/")
        assert leased.pair_reconciled
        assert event.queued_intent_ids == ()

        assert catalog.release_download_lease(
            UUID(int=1),
            holder="control-event",
            monotonic_now_ns=300,
        )
        pending = catalog.list_pending_intents_by_kind(
            kinds=(IntentKind.PROTECT,),
            limit=1,
        )
        assert len(pending) == 1 and pending[0].clip_id == UUID(int=1)
        assert not catalog.get_clip(UUID(int=1)).pair_reconciled

        catalog.register_clip(
            _clip(2, protected=True, directory="protected"),
            catalog_now_ns=2,
        )
        catalog.acquire_download_lease(
            UUID(int=2),
            holder="control-unprotect",
            monotonic_now_ns=400,
            duration_ns=100,
            boot_id="boot-a",
        )
        with pytest.raises(DownloadLeaseError, match="freezes"):
            catalog.prepare_unprotect(UUID(int=2), monotonic_now_ns=450)
        assert catalog.get_clip(UUID(int=2)).video_path.startswith("protected/")

        expired, more = catalog.clear_expired_download_leases(
            monotonic_now_ns=500,
            boot_id="boot-a",
            limit=1,
        )
        assert (expired, more) == (1, False)
        assert catalog.get_clip(UUID(int=2)).download_lease is None


def test_event_window_protects_previous_two_current_and_next_one_durably(
    tmp_path: Path,
) -> None:
    database = tmp_path / "catalog.sqlite3"
    with ClipCatalog(database) as catalog:
        for number in range(1, 4):
            catalog.register_clip(_clip(number))
        catalog.register_clip(_clip(4, lifecycle=ClipLifecycle.WRITING))

        event = catalog.trigger_event(
            UUID(int=3),
            source=EventSource.WEB,
            monotonic_now_ns=1_000,
        )
        assert event.protected_clip_ids == (
            UUID(int=1),
            UUID(int=2),
            UUID(int=3),
        )
        assert event.missing_previous_count == 0
        assert event.pending_next_count == 1
        assert len(event.queued_intent_ids) == 3
        next_intents = catalog.finalize_clip(
            UUID(int=4),
            end_monotonic_ns=450,
            size_bytes=200,
            monotonic_now_ns=1_100,
        )
        assert len(next_intents) == 1
        assert catalog.get_clip(UUID(int=4)).protected

    with ClipCatalog(database) as reopened:
        assert all(reopened.get_clip(UUID(int=number)).protected for number in range(1, 5))
        reopened.register_clip(_clip(5, lifecycle=ClipLifecycle.WRITING))
        assert (
            reopened.finalize_clip(
                UUID(int=5),
                end_monotonic_ns=550,
                size_bytes=200,
                monotonic_now_ns=1_200,
            )
            == ()
        )
        assert not reopened.get_clip(UUID(int=5)).protected


def test_event_reports_previous_clips_that_no_longer_exist(tmp_path: Path) -> None:
    with ClipCatalog(tmp_path / "catalog.sqlite3") as catalog:
        catalog.register_clip(_clip(3))
        result = catalog.trigger_event(
            UUID(int=3),
            source=EventSource.API,
            monotonic_now_ns=100,
        )
        assert result.protected_clip_ids == (UUID(int=3),)
        assert result.missing_previous_count == 2


def test_event_protects_an_active_current_clip_before_acknowledgement(
    tmp_path: Path,
) -> None:
    with ClipCatalog(tmp_path / "catalog.sqlite3") as catalog:
        catalog.register_clip(_clip(1))
        current = _clip(2, lifecycle=ClipLifecycle.WRITING, directory="pending")
        catalog.register_clip(current)

        result = catalog.trigger_event(
            current.clip_id,
            source=EventSource.GPIO,
            monotonic_now_ns=1_000,
            previous_count=1,
            next_count=0,
        )
        durable_current = catalog.get_clip(current.clip_id)
        assert result.protected_clip_ids == (UUID(int=1), current.clip_id)
        assert durable_current.protected
        assert durable_current.lifecycle is ClipLifecycle.WRITING
        assert all(
            intent.clip_id != current.clip_id for intent in catalog.list_pending_intents(limit=10)
        )


def test_writing_registration_next_consumption_and_orphan_demotion_are_durable(
    tmp_path: Path,
) -> None:
    database = tmp_path / "catalog.sqlite3"
    with ClipCatalog(database) as catalog:
        catalog.register_clip(_clip(1), catalog_now_ns=1)
        active = _writing_clip(2)
        catalog.register_writing_clip(active, monotonic_now_ns=2)
        event = catalog.trigger_event(
            UUID(int=1),
            source=EventSource.WEB,
            monotonic_now_ns=3,
            previous_count=0,
            next_count=1,
            event_id=UUID(int=500),
        )

        state = catalog.active_closing_protection(active.clip_id, monotonic_now_ns=4)
        repeated = catalog.active_closing_protection(active.clip_id, monotonic_now_ns=5)

        assert state.protected and state.reason == f"event:{event.event_id}:pending-window"
        assert repeated == state
        assert catalog.get_clip(active.clip_id).protected
        assert catalog.list_writing_clips(limit=2) == (catalog.get_clip(active.clip_id),)
        assert catalog.mark_writing_clip_orphaned(active.clip_id, monotonic_now_ns=6)
        assert not catalog.mark_writing_clip_orphaned(active.clip_id, monotonic_now_ns=7)
        orphan = catalog.get_clip(active.clip_id)
        assert orphan.lifecycle is ClipLifecycle.MISSING_SIDECAR
        assert orphan.protected

    with ClipCatalog(database) as reopened:
        assert reopened.get_clip(active.clip_id) == orphan
        assert reopened.list_writing_clips(limit=1) == ()


def test_event_id_retry_is_idempotent_and_reexposes_only_pending_moves(tmp_path: Path) -> None:
    with ClipCatalog(tmp_path / "catalog.sqlite3") as catalog:
        for number in range(1, 4):
            catalog.register_clip(_clip(number), catalog_now_ns=number)
        event_id = UUID(int=600)
        first = catalog.trigger_event(
            UUID(int=3),
            source=EventSource.WEB,
            monotonic_now_ns=10,
            event_id=event_id,
        )
        pending_before = catalog.list_pending_intents(limit=10)

        retried = catalog.trigger_event(
            None,
            source=EventSource.WEB,
            monotonic_now_ns=999,
            event_id=event_id,
        )

        assert retried == first
        assert catalog.list_pending_intents(limit=10) == pending_before
        assert catalog.trigger_event(
            UUID(int=2),
            source=EventSource.WEB,
            monotonic_now_ns=1_000,
            event_id=event_id,
        ) == first
        with pytest.raises(CatalogConflictError, match="another request"):
            catalog.trigger_event(
                None,
                source=EventSource.API,
                monotonic_now_ns=1_001,
                event_id=event_id,
            )


def test_active_finalize_refuses_stale_protection_revision_without_transition(
    tmp_path: Path,
) -> None:
    with ClipCatalog(tmp_path / "catalog.sqlite3") as catalog:
        active = _writing_clip(1)
        catalog.register_writing_clip(active, monotonic_now_ns=1)
        snapshot = catalog.active_closing_protection(active.clip_id, monotonic_now_ns=2)
        catalog.trigger_event(
            active.clip_id,
            source=EventSource.API,
            monotonic_now_ns=3,
            previous_count=0,
            next_count=0,
            event_id=UUID(int=700),
        )
        target = finalized_unsynced_clip_pair(boot_id="abcde", sequence=1)
        closing = replace(
            active,
            lifecycle=ClipLifecycle.FINALIZING,
            end_monotonic_ns=1_000,
            size_bytes=10,
        )

        with pytest.raises(ActiveClipProtectionChanged):
            catalog.register_finalizing_clip(
                closing,
                promotion_paths=PairPaths(
                    active.video_path,
                    active.sidecar_path,
                    f"clips/{target.video_name}",
                    f"clips/{target.metadata_name}",
                ),
                monotonic_now_ns=4,
                expected_protection_revision=snapshot.revision,
            )

        durable = catalog.get_clip(active.clip_id)
        assert durable.lifecycle is ClipLifecycle.WRITING
        assert durable.protected
        assert catalog.list_pending_intents(limit=1) == ()


def test_catalog_retention_view_excludes_protection_leases_and_mutations(
    tmp_path: Path,
) -> None:
    with ClipCatalog(tmp_path / "catalog.sqlite3") as catalog:
        for number in range(1, 5):
            catalog.register_clip(_clip(number))
        catalog.prepare_protect(UUID(int=1), reason="event", monotonic_now_ns=10)
        catalog.acquire_download_lease(
            UUID(int=2),
            holder="client",
            monotonic_now_ns=10,
            duration_ns=100,
            boot_id="boot-a",
        )
        catalog.prepare_delete(UUID(int=3), monotonic_now_ns=10, boot_id="boot-a")

        plan = catalog.plan_retention(
            requested_reclaim_bytes=100,
            monotonic_now_ns=11,
            boot_id="boot-a",
            candidate_limit=10,
        )
        assert plan.selected_clip_ids == (UUID(int=4),)


def test_prepare_calls_reuse_the_same_pending_intent_id(tmp_path: Path) -> None:
    with ClipCatalog(tmp_path / "catalog.sqlite3") as catalog:
        catalog.register_clip(_clip(1))
        first = catalog.prepare_protect(UUID(int=1), reason="event", monotonic_now_ns=10)
        second = catalog.prepare_protect(UUID(int=1), reason="event", monotonic_now_ns=11)
        assert first is not None
        assert second == first


def test_pending_event_windows_have_a_hard_bound(tmp_path: Path) -> None:
    with ClipCatalog(tmp_path / "catalog.sqlite3") as catalog:
        catalog.register_clip(_clip(1))
        for monotonic_ns in range(MAX_PENDING_EVENT_WINDOWS):
            catalog.trigger_event(
                UUID(int=1),
                source=EventSource.API,
                monotonic_now_ns=monotonic_ns,
                previous_count=0,
                next_count=1,
            )
        with pytest.raises(CatalogConflictError, match="window bound"):
            catalog.trigger_event(
                UUID(int=1),
                source=EventSource.API,
                monotonic_now_ns=MAX_PENDING_EVENT_WINDOWS,
                previous_count=0,
                next_count=1,
            )
