from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
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
)
from dashcam.state import MAX_DOWNLOAD_LEASE_NS, ClipLifecycle, DownloadLeaseError
from dashcam.storage.naming import finalized_clip_pair


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


def test_migrations_are_explicit_versioned_and_reopen_cleanly(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"

    with ClipCatalog(database) as catalog:
        assert catalog.schema_version == SCHEMA_VERSION
        catalog.register_clip(_clip(1), catalog_now_ns=1)

    with ClipCatalog(database) as reopened:
        assert reopened.schema_version == SCHEMA_VERSION
        assert reopened.get_clip(UUID(int=1)) == _clip(1)

    with sqlite3.connect(database) as connection:
        migrations = connection.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
    assert migrations == [
        (1, "create_clip_catalog"),
        (2, "add_event_protection"),
        (3, "add_protection_revisions"),
    ]
    assert journal_mode == ("wal",)


def test_catalog_refuses_a_newer_database_schema(tmp_path: Path) -> None:
    database = tmp_path / "future.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")

    with pytest.raises(RuntimeError, match="newer than supported"):
        ClipCatalog(database)


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
