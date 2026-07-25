from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from dashcam.catalog import (
    CatalogClip,
    ClipCatalog,
    EventSource,
    ReconciliationBounds,
    ReconciliationLimitError,
    RootedFilesystem,
)
from dashcam.metadata.reconcile import parse_sidecar_bytes
from dashcam.metadata.schema import (
    AudioSummary,
    ClipSidecar,
    GpsSummary,
    TimeAnchor,
    TimeAnchorSource,
    VideoSummary,
)
from dashcam.state import (
    ClipLifecycle,
    GpsTimeState,
    SystemClockState,
    TimestampQuality,
)
from dashcam.storage.intents import IntentKind, OperationIntent, PairPaths
from dashcam.storage.naming import ClipFilePair, finalized_clip_pair, parse_clip_filename


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "recording"
    root.mkdir()
    for directory in ("pending", "clips", "protected", "quarantine"):
        (root / directory).mkdir()
    return root


def _clip(number: int, *, directory: str = "clips") -> CatalogClip:
    pair = finalized_clip_pair(
        utc_started_at=datetime(2026, 7, 24, 0, 0, number, tzinfo=UTC),
        boot_id="abcde",
        sequence=number,
    )
    return CatalogClip(
        clip_id=UUID(int=number),
        lifecycle=ClipLifecycle.FINALIZED,
        video_path=f"{directory}/{pair.video_name}",
        sidecar_path=f"{directory}/{pair.metadata_name}",
        start_monotonic_ns=number * 100,
        end_monotonic_ns=number * 100 + 50,
        retention_order=number,
        size_bytes=10,
        protected=directory == "protected",
        protection_reason="fixture" if directory == "protected" else None,
        pair_reconciled=True,
        managed=True,
    )


def _write_pair(root: Path, clip: CatalogClip) -> None:
    (root / Path(clip.video_path)).write_bytes(b"video")
    (root / Path(clip.sidecar_path)).write_text("{}", encoding="utf-8")


def _canonical_sidecar(
    *,
    clip_id: UUID,
    pair: ClipFilePair,
    sequence: int,
    start_monotonic_ns: int,
    protected: bool = False,
) -> bytes:
    parsed = parse_clip_filename(pair.video_name)
    assert parsed.utc_started_at is not None
    end_monotonic_ns = start_monotonic_ns + 1_000_000
    end_utc = parsed.utc_started_at + timedelta(milliseconds=1)
    anchor = TimeAnchor(
        TimeAnchorSource.SYSTEM_CLOCK,
        start_monotonic_ns,
        parsed.utc_started_at,
        1_000,
        "catalog recovery fixture",
    )
    sidecar = ClipSidecar(
        schema_version=1,
        clip_id=clip_id,
        boot_id=UUID("00000000-0000-0000-0000-000000000999"),
        sequence=sequence,
        video_file=pair.video_name,
        metadata_file=pair.metadata_name,
        start_utc=parsed.utc_started_at,
        end_utc=end_utc,
        start_monotonic_ns=start_monotonic_ns,
        end_monotonic_ns=end_monotonic_ns,
        gps_time_state=GpsTimeState.UNSYNCED,
        system_clock_state=SystemClockState.SYNCHRONIZED,
        timestamp_quality=TimestampQuality.SYSTEM_DERIVED,
        time_anchor=anchor,
        timezone="UTC",
        start_local=parsed.utc_started_at,
        video=VideoSummary("h264", 1920, 1080, 30.0, 8_000_000, 8_000_000, 30, 0),
        audio=AudioSummary(False, None, None, None, None),
        gps=GpsSummary(False, None),
        protected=protected,
        protection_reason="fixture" if protected else None,
        software_version="test",
    )
    return sidecar.to_canonical_json()


def _inject_pending_intent(database: Path, intent: OperationIntent) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO operation_intents (
                intent_id, clip_id, kind, created_monotonic_ns,
                video_source, sidecar_source, video_target, sidecar_target,
                status, last_problem, completed_monotonic_ns,
                expected_protection_revision
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', NULL, NULL, NULL)
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
            ),
        )
        connection.execute(
            "UPDATE clips SET pair_reconciled = 0 WHERE clip_id = ?",
            (str(intent.clip_id),),
        )


def test_protect_resumes_after_crash_between_pair_moves(tmp_path: Path) -> None:
    root = _root(tmp_path)
    filesystem = RootedFilesystem(root)
    database = tmp_path / "catalog.sqlite3"
    clip = _clip(1)
    _write_pair(root, clip)

    with ClipCatalog(database) as catalog:
        catalog.register_clip(clip)
        intent_id = catalog.prepare_protect(
            clip.clip_id, reason="manual event", monotonic_now_ns=100
        )
        assert intent_id is not None
        first = catalog.reconcile_intent(intent_id, filesystem, monotonic_now_ns=101, max_actions=1)
        assert first.actions_attempted == 1
        assert not first.complete
        assert catalog.get_clip(clip.clip_id).protected
        assert not catalog.get_clip(clip.clip_id).pair_reconciled

    with ClipCatalog(database) as reopened:
        report = reopened.reconcile_startup(
            filesystem,
            monotonic_now_ns=200,
            boot_id="boot-b",
        )
        recovered = reopened.get_clip(clip.clip_id)
        assert report.actions_attempted == 1
        assert recovered.protected
        assert recovered.pair_reconciled
        assert recovered.video_path.startswith("protected/")
        assert not (root / Path(clip.video_path)).exists()
        assert (root / Path(recovered.video_path)).exists()


def test_unprotect_and_delete_are_replay_safe_after_each_half_operation(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    filesystem = RootedFilesystem(root)
    database = tmp_path / "catalog.sqlite3"
    clip = _clip(1, directory="protected")
    _write_pair(root, clip)

    with ClipCatalog(database) as catalog:
        catalog.register_clip(clip)
        unprotect_id = catalog.prepare_unprotect(clip.clip_id, monotonic_now_ns=10)
        assert unprotect_id is not None
        catalog.reconcile_intent(unprotect_id, filesystem, monotonic_now_ns=11, max_actions=1)

    with ClipCatalog(database) as catalog:
        report = catalog.reconcile_startup(filesystem, monotonic_now_ns=20, boot_id="boot-a")
        assert report.actions_attempted == 1
        unprotected = catalog.get_clip(clip.clip_id)
        assert not unprotected.protected
        assert unprotected.video_path.startswith("clips/")
        delete_id = catalog.prepare_delete(clip.clip_id, monotonic_now_ns=21, boot_id="boot-a")
        catalog.reconcile_intent(delete_id, filesystem, monotonic_now_ns=22, max_actions=1)

    with ClipCatalog(database) as catalog:
        report = catalog.reconcile_startup(filesystem, monotonic_now_ns=30, boot_id="boot-b")
        assert report.actions_attempted == 1
        assert catalog.get_clip(clip.clip_id).lifecycle is ClipLifecycle.DELETED
        assert not (root / Path(unprotected.video_path)).exists()
        assert not (root / Path(unprotected.sidecar_path)).exists()


def test_startup_marks_orphaned_catalog_pair_but_never_deletes_unknown_files(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    filesystem = RootedFilesystem(root)
    unknown = root / "clips" / "System Volume Information.txt"
    unknown.write_text("keep", encoding="utf-8")
    clip = _clip(1)
    (root / Path(clip.video_path)).write_bytes(b"video")

    with ClipCatalog(tmp_path / "catalog.sqlite3") as catalog:
        catalog.register_clip(clip)
        report = catalog.reconcile_startup(
            filesystem,
            monotonic_now_ns=100,
            boot_id="boot-a",
        )
        recovered = catalog.get_clip(clip.clip_id)
        assert recovered.lifecycle is ClipLifecycle.MISSING_SIDECAR
        assert not recovered.pair_reconciled
        assert any("MISSING_SIDECAR" in issue for issue in report.issues)
    assert unknown.read_text(encoding="utf-8") == "keep"


def test_startup_rebuilds_unindexed_pair_from_bounded_sidecar_read(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    filesystem = RootedFilesystem(root)
    clip = _clip(7)
    pair = ClipFilePair(Path(clip.video_path).name, Path(clip.sidecar_path).name)
    (root / Path(clip.video_path)).write_bytes(b"recording")
    (root / Path(clip.sidecar_path)).write_bytes(
        _canonical_sidecar(
            clip_id=clip.clip_id,
            pair=pair,
            sequence=7,
            start_monotonic_ns=700,
        )
    )
    assert parse_sidecar_bytes((root / Path(clip.sidecar_path)).read_bytes())

    with ClipCatalog(tmp_path / "catalog.sqlite3") as catalog:
        report = catalog.reconcile_startup(
            filesystem,
            monotonic_now_ns=1_000,
            boot_id="boot-a",
        )
        imported = catalog.get_clip(clip.clip_id)
        assert report.imported_clips == 1
        assert imported.lifecycle is ClipLifecycle.FINALIZED
        assert imported.size_bytes == len(b"recording")


def test_startup_work_is_explicitly_bounded_and_requests_another_pass(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    for number in range(5):
        (root / "clips" / f"unknown-{number}.bin").write_bytes(b"x")

    with ClipCatalog(tmp_path / "catalog.sqlite3") as catalog:
        report = catalog.reconcile_startup(
            RootedFilesystem(root),
            monotonic_now_ns=0,
            boot_id="boot-a",
            bounds=ReconciliationBounds(max_directory_entries=2),
        )
        assert report.directory_entries_examined == 2
        assert report.more_work


def test_generated_looking_but_noncanonical_pair_is_preserved_as_unknown(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    pair = finalized_clip_pair(
        utc_started_at=datetime(2026, 7, 24, 0, 0, 9, tzinfo=UTC),
        boot_id="abcde",
        sequence=9,
    )
    video = root / "clips" / pair.video_name
    sidecar = root / "clips" / pair.metadata_name
    video.write_bytes(b"user data")
    sidecar.write_text('{"schema_version":1}', encoding="utf-8")

    with ClipCatalog(tmp_path / "catalog.sqlite3") as catalog:
        report = catalog.reconcile_startup(
            RootedFilesystem(root),
            monotonic_now_ns=1_000,
            boot_id="boot-a",
        )

        assert report.imported_clips == 0
        assert any(pair.metadata_name in issue for issue in report.issues)
        assert catalog.list_clips(limit=10) == ()
    assert video.read_bytes() == b"user data"
    assert sidecar.read_text(encoding="utf-8") == '{"schema_version":1}'


def test_startup_rebuilds_pending_pair_as_non_retention_eligible(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    pair = finalized_clip_pair(
        utc_started_at=datetime(2026, 7, 24, 0, 0, 8, tzinfo=UTC),
        boot_id="abcde",
        sequence=8,
    )
    clip_id = UUID(int=8)
    video_path = root / "pending" / pair.video_name
    sidecar_path = root / "pending" / pair.metadata_name
    video_path.write_bytes(b"pending")
    sidecar_path.write_bytes(
        _canonical_sidecar(
            clip_id=clip_id,
            pair=pair,
            sequence=8,
            start_monotonic_ns=800,
        )
    )
    assert parse_sidecar_bytes(sidecar_path.read_bytes())
    unknown = root / "clips" / "settings.json"
    unknown.write_text("{}", encoding="utf-8")

    with ClipCatalog(tmp_path / "catalog.sqlite3") as catalog:
        report = catalog.reconcile_startup(
            RootedFilesystem(root),
            monotonic_now_ns=1_000,
            boot_id="boot-a",
        )
        recovered = catalog.get_clip(clip_id)
        assert recovered.lifecycle is ClipLifecycle.FINALIZING
        assert not recovered.pair_reconciled
        assert report.imported_clips == 1
        assert not any("settings.json" in issue for issue in report.issues)
        assert (
            catalog.plan_retention(
                requested_reclaim_bytes=1,
                monotonic_now_ns=1_000,
                boot_id="boot-a",
                candidate_limit=10,
            ).selected_clip_ids
            == ()
        )
    assert unknown.exists()


def test_startup_queues_repair_when_protection_and_directory_disagree(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    clip = _clip(1)
    protected_in_catalog = CatalogClip(
        clip_id=clip.clip_id,
        lifecycle=clip.lifecycle,
        video_path=clip.video_path,
        sidecar_path=clip.sidecar_path,
        start_monotonic_ns=clip.start_monotonic_ns,
        end_monotonic_ns=clip.end_monotonic_ns,
        retention_order=clip.retention_order,
        size_bytes=clip.size_bytes,
        protected=True,
        protection_reason="event survived restart",
        pair_reconciled=True,
        managed=True,
    )
    _write_pair(root, protected_in_catalog)

    with ClipCatalog(tmp_path / "catalog.sqlite3") as catalog:
        catalog.register_clip(protected_in_catalog)
        report = catalog.reconcile_startup(
            RootedFilesystem(root),
            monotonic_now_ns=100,
            boot_id="boot-a",
        )
        assert report.more_work
        pending = catalog.list_pending_intents(limit=2)
        assert len(pending) == 1
        assert pending[0].clip_id == clip.clip_id


def test_reconciliation_configuration_has_hard_upper_bounds() -> None:
    with pytest.raises(ReconciliationLimitError, match="hard maximum"):
        ReconciliationBounds(max_directory_entries=100_001)


def test_event_does_not_rollback_current_when_previous_is_being_unprotected(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    filesystem = RootedFilesystem(root)
    database = tmp_path / "catalog.sqlite3"
    previous = _clip(1, directory="protected")
    current = _clip(2)
    _write_pair(root, previous)
    _write_pair(root, current)

    with ClipCatalog(database) as catalog:
        catalog.register_clip(previous)
        catalog.register_clip(current)
        unprotect_id = catalog.prepare_unprotect(previous.clip_id, monotonic_now_ns=10)
        assert unprotect_id is not None
        event = catalog.trigger_event(
            current.clip_id,
            source=EventSource.API,
            monotonic_now_ns=20,
            previous_count=1,
            next_count=0,
        )
        assert event.protected_clip_ids == (previous.clip_id, current.clip_id)
        assert catalog.get_clip(previous.clip_id).protected
        assert catalog.get_clip(current.clip_id).protected

    with ClipCatalog(database) as catalog:
        first_pass = catalog.reconcile_startup(
            filesystem,
            monotonic_now_ns=30,
            boot_id="boot-b",
        )
        recovered_previous = catalog.get_clip(previous.clip_id)
        recovered_current = catalog.get_clip(current.clip_id)
        assert first_pass.more_work
        assert recovered_previous.protected
        assert recovered_previous.protection_reason is not None
        assert recovered_previous.video_path.startswith("clips/")
        assert not recovered_previous.pair_reconciled
        assert recovered_current.protected
        assert recovered_current.video_path.startswith("protected/")
        follow_up = catalog.list_pending_intents(limit=10)
        assert len(follow_up) == 1
        assert follow_up[0].kind is IntentKind.PROTECT
        assert follow_up[0].clip_id == previous.clip_id

    with ClipCatalog(database) as catalog:
        second_pass = catalog.reconcile_startup(
            filesystem,
            monotonic_now_ns=40,
            boot_id="boot-b",
        )
        recovered_previous = catalog.get_clip(previous.clip_id)
        assert second_pass.actions_attempted == 2
        assert recovered_previous.protected
        assert recovered_previous.pair_reconciled
        assert recovered_previous.video_path.startswith("protected/")


@pytest.mark.parametrize("kind", [IntentKind.FINALIZE, IntentKind.RECONCILE_NAME])
def test_event_survives_inflight_finalize_or_name_reconciliation(
    tmp_path: Path,
    kind: IntentKind,
) -> None:
    root = _root(tmp_path)
    filesystem = RootedFilesystem(root)
    database = tmp_path / f"{kind.value.lower()}.sqlite3"
    source_directory = "pending" if kind is IntentKind.FINALIZE else "clips"
    source = _clip(1, directory=source_directory)
    lifecycle = ClipLifecycle.FINALIZING if kind is IntentKind.FINALIZE else ClipLifecycle.FINALIZED
    registered = CatalogClip(
        clip_id=source.clip_id,
        lifecycle=lifecycle,
        video_path=source.video_path,
        sidecar_path=source.sidecar_path,
        start_monotonic_ns=source.start_monotonic_ns,
        end_monotonic_ns=source.end_monotonic_ns,
        retention_order=source.retention_order,
        size_bytes=source.size_bytes,
        protected=False,
        protection_reason=None,
        pair_reconciled=False,
        managed=True,
    )
    _write_pair(root, registered)
    target_pair = finalized_clip_pair(
        utc_started_at=datetime(2026, 7, 24, 0, 0, 9, tzinfo=UTC),
        boot_id="abcde",
        sequence=9,
    )
    target_paths = PairPaths(
        video_source=registered.video_path,
        sidecar_source=registered.sidecar_path,
        video_target=f"clips/{target_pair.video_name}",
        sidecar_target=f"clips/{target_pair.metadata_name}",
    )
    intent = OperationIntent(
        intent_id=UUID(int=100 + list(IntentKind).index(kind)),
        clip_id=registered.clip_id,
        kind=kind,
        created_monotonic_ns=10,
        paths=target_paths,
    )

    with ClipCatalog(database) as catalog:
        catalog.register_clip(registered)
    _inject_pending_intent(database, intent)

    with ClipCatalog(database) as catalog:
        event = catalog.trigger_event(
            registered.clip_id,
            source=EventSource.WEB,
            monotonic_now_ns=20,
            previous_count=0,
            next_count=0,
        )
        assert event.protected_clip_ids == (registered.clip_id,)
        assert catalog.get_clip(registered.clip_id).protected

    with ClipCatalog(database) as catalog:
        first_pass = catalog.reconcile_startup(
            filesystem,
            monotonic_now_ns=30,
            boot_id="boot-b",
        )
        intermediate = catalog.get_clip(registered.clip_id)
        assert first_pass.more_work
        assert intermediate.lifecycle is ClipLifecycle.FINALIZED
        assert intermediate.protected
        assert not intermediate.pair_reconciled
        assert intermediate.video_path == target_paths.video_target
        pending = catalog.list_pending_intents(limit=2)
        assert len(pending) == 1
        assert pending[0].kind is IntentKind.PROTECT

    with ClipCatalog(database) as catalog:
        catalog.reconcile_startup(
            filesystem,
            monotonic_now_ns=40,
            boot_id="boot-b",
        )
        recovered = catalog.get_clip(registered.clip_id)
        assert recovered.protected
        assert recovered.pair_reconciled
        assert recovered.video_path.startswith("protected/")
