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
from dashcam.catalog.database import RetentionThresholdLatch
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
