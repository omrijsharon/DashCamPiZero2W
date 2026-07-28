from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from dashcam.catalog import CatalogClip, ClipCatalog, RootedFilesystem
from dashcam.metadata.coordinator import (
    ClipMetadataCoordinator,
    MetadataReconciliationRefused,
)
from dashcam.metadata.reconcile import parse_sidecar_bytes, plan_post_anchor_reconciliation
from dashcam.metadata.schema import (
    AudioSummary,
    ClipSidecar,
    GpsSummary,
    TimeAnchor,
    TimeAnchorSource,
    VideoSummary,
)
from dashcam.recorder.finalizer import (
    DurableRootedFinalizationFilesystem,
    RecorderClipFinalizer,
)
from dashcam.state import (
    ClipLifecycle,
    GpsTimeState,
    SystemClockState,
    TimestampQuality,
)
from dashcam.storage.naming import finalized_unsynced_clip_pair

CLIP_ID = UUID("12345678-1234-5678-9234-567812345678")
BOOT_ID = UUID("87654321-4321-6789-a234-678943216789")
INTENT_ID = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")


def _recording_root(tmp_path: Path) -> Path:
    root = tmp_path / "recording"
    root.mkdir()
    for directory in ("pending", "clips", "protected", "quarantine"):
        (root / directory).mkdir()
    return root


def _source_sidecar() -> ClipSidecar:
    pair = finalized_unsynced_clip_pair(boot_id="876543214321", sequence=123)
    return ClipSidecar(
        schema_version=1,
        clip_id=CLIP_ID,
        boot_id=BOOT_ID,
        sequence=123,
        video_file=pair.video_name,
        metadata_file=pair.metadata_name,
        start_utc=None,
        end_utc=None,
        start_monotonic_ns=10_000_000_000,
        end_monotonic_ns=70_000_654_321,
        gps_time_state=GpsTimeState.UNSYNCED,
        system_clock_state=SystemClockState.UNSET,
        timestamp_quality=TimestampQuality.MONOTONIC_ONLY,
        time_anchor=None,
        timezone="Asia/Jerusalem",
        start_local=None,
        video=VideoSummary("h264", 1920, 1080, 30.0, 8_000_000, 7_900_000, 1_800, 0),
        audio=AudioSummary(False, None, None, None, None),
        gps=GpsSummary(False, None),
        protected=False,
        protection_reason=None,
        software_version="test",
    )


def _anchor() -> TimeAnchor:
    return TimeAnchor(
        TimeAnchorSource.GPS,
        10_000_000_000,
        datetime(2026, 7, 28, 16, 0, tzinfo=UTC),
        250_000_000,
        "NMEA:GNRMC:active-valid:complete-utc",
    )


def _registered(root: Path, catalog: ClipCatalog) -> ClipSidecar:
    sidecar = _source_sidecar()
    video_path = f"clips/{sidecar.video_file}"
    sidecar_path = f"clips/{sidecar.metadata_file}"
    (root / video_path).write_bytes(b"video")
    (root / sidecar_path).write_bytes(sidecar.to_canonical_json())
    catalog.register_clip(
        CatalogClip(
            clip_id=CLIP_ID,
            lifecycle=ClipLifecycle.FINALIZED,
            video_path=video_path,
            sidecar_path=sidecar_path,
            start_monotonic_ns=sidecar.start_monotonic_ns,
            end_monotonic_ns=sidecar.end_monotonic_ns,
            retention_order=1,
            size_bytes=5,
            protected=False,
            protection_reason=None,
            pair_reconciled=True,
            managed=True,
        )
    )
    return sidecar


def _coordinator(catalog: ClipCatalog, filesystem: RootedFilesystem) -> ClipMetadataCoordinator:
    return ClipMetadataCoordinator(
        catalog=catalog,
        filesystem=filesystem,
        monotonic_ns=lambda: 80_000_000_000,
        uuid_factory=lambda: INTENT_ID,
    )


def test_coordinator_reconciles_pair_atomically_and_keeps_stable_uuid(tmp_path: Path) -> None:
    root = _recording_root(tmp_path)
    filesystem = RootedFilesystem(root)
    with ClipCatalog(tmp_path / "catalog.sqlite3") as catalog:
        source = _registered(root, catalog)
        api_identity_before = catalog.get_clip(CLIP_ID).clip_id
        coordinator = _coordinator(catalog, filesystem)

        outcome = coordinator.reconcile_clip(
            CLIP_ID,
            anchor=_anchor(),
            expected_boot_id=BOOT_ID,
            gps_time_state=GpsTimeState.GPS_TIME_VALID,
            system_clock_state=SystemClockState.SYNCHRONIZED,
        )

        assert outcome.clip_id == CLIP_ID
        assert outcome.actions_attempted == 2
        assert not (root / "clips" / source.video_file).exists()
        assert not (root / "clips" / source.metadata_file).exists()
        persisted = parse_sidecar_bytes((root / outcome.sidecar_path).read_bytes())
        assert persisted.clip_id == CLIP_ID
        assert catalog.get_clip(api_identity_before).clip_id == api_identity_before
        assert persisted.start_utc == datetime(2026, 7, 28, 16, 0, tzinfo=UTC)
        assert persisted.timestamp_quality is TimestampQuality.GPS_ANCHORED
        assert persisted.video_file == Path(outcome.video_path).name

        replay = coordinator.reconcile_clip(
            CLIP_ID,
            anchor=_anchor(),
            expected_boot_id=BOOT_ID,
            gps_time_state=GpsTimeState.GPS_TIME_VALID,
            system_clock_state=SystemClockState.SYNCHRONIZED,
        )
        assert replay.already_reconciled
        assert replay.intent_id is None
        assert replay.clip_id == CLIP_ID
        assert replay.video_path == outcome.video_path


def test_collision_is_refused_before_catalog_or_pair_mutation(tmp_path: Path) -> None:
    root = _recording_root(tmp_path)
    filesystem = RootedFilesystem(root)
    with ClipCatalog(tmp_path / "catalog.sqlite3") as catalog:
        source = _registered(root, catalog)
        target = plan_post_anchor_reconciliation(
            source,
            anchor=_anchor(),
            intent_id=INTENT_ID,
            created_monotonic_ns=80_000_000_000,
        ).sidecar
        collision = root / "clips" / target.metadata_file.upper()
        collision.write_bytes(b"foreign")
        before = (root / "clips" / source.metadata_file).read_bytes()

        with pytest.raises(MetadataReconciliationRefused, match="collision"):
            _coordinator(catalog, filesystem).reconcile_clip(
                CLIP_ID,
                anchor=_anchor(),
                expected_boot_id=BOOT_ID,
                gps_time_state=GpsTimeState.GPS_TIME_VALID,
                system_clock_state=SystemClockState.SYNCHRONIZED,
            )

        assert (root / "clips" / source.metadata_file).read_bytes() == before
        assert catalog.get_clip(CLIP_ID).pair_reconciled
        assert catalog.list_pending_intents(limit=1) == ()


def test_committed_intent_replays_after_interruption_before_sidecar_replace(
    tmp_path: Path,
) -> None:
    root = _recording_root(tmp_path)
    filesystem = RootedFilesystem(root)
    with ClipCatalog(tmp_path / "catalog.sqlite3") as catalog:
        source = _registered(root, catalog)
        plan = plan_post_anchor_reconciliation(
            source,
            anchor=_anchor(),
            intent_id=INTENT_ID,
            created_monotonic_ns=80_000_000_000,
            existing_names={source.video_file, source.metadata_file},
        )
        catalog.register_name_reconciliation(
            plan,
            source_sidecar=source,
            monotonic_now_ns=80_000_000_000,
        )

        outcome = _coordinator(catalog, filesystem).reconcile_clip(
            CLIP_ID,
            anchor=_anchor(),
            expected_boot_id=BOOT_ID,
            gps_time_state=GpsTimeState.GPS_TIME_VALID,
            system_clock_state=SystemClockState.SYNCHRONIZED,
        )

        assert outcome.actions_attempted == 2
        persisted = (root / outcome.sidecar_path).read_bytes()
        assert persisted == plan.sidecar.to_canonical_json()
        assert parse_sidecar_bytes(persisted).clip_id == plan.sidecar.clip_id


def test_source_drift_after_committed_intent_latches_refusal(tmp_path: Path) -> None:
    root = _recording_root(tmp_path)
    filesystem = RootedFilesystem(root)
    with ClipCatalog(tmp_path / "catalog.sqlite3") as catalog:
        source = _registered(root, catalog)
        plan = plan_post_anchor_reconciliation(
            source,
            anchor=_anchor(),
            intent_id=INTENT_ID,
            created_monotonic_ns=80_000_000_000,
            existing_names={source.video_file, source.metadata_file},
        )
        catalog.register_name_reconciliation(
            plan,
            source_sidecar=source,
            monotonic_now_ns=80_000_000_000,
        )
        (root / "clips" / source.metadata_file).write_bytes(b"tampered")

        with pytest.raises(MetadataReconciliationRefused, match="SOURCE_CHANGED"):
            _coordinator(catalog, filesystem).reconcile_clip(
                CLIP_ID,
                anchor=_anchor(),
                expected_boot_id=BOOT_ID,
                gps_time_state=GpsTimeState.GPS_TIME_VALID,
                system_clock_state=SystemClockState.SYNCHRONIZED,
            )

        assert (root / "clips" / source.video_file).exists()
        assert catalog.list_pending_intents(limit=1)[0].intent_id == INTENT_ID
        assert not catalog.get_clip(CLIP_ID).pair_reconciled


def test_recorder_startup_replays_committed_name_reconciliation(tmp_path: Path) -> None:
    root = _recording_root(tmp_path)
    database = tmp_path / "catalog.sqlite3"
    with ClipCatalog(database) as catalog:
        source = _registered(root, catalog)
        plan = plan_post_anchor_reconciliation(
            source,
            anchor=_anchor(),
            intent_id=INTENT_ID,
            created_monotonic_ns=80_000_000_000,
            existing_names={source.video_file, source.metadata_file},
        )
        catalog.register_name_reconciliation(
            plan,
            source_sidecar=source,
            monotonic_now_ns=80_000_000_000,
        )

    durable_filesystem = DurableRootedFinalizationFilesystem(root)
    with ClipCatalog(database) as catalog:
        finalizer = RecorderClipFinalizer(
            catalog=catalog,
            filesystem=durable_filesystem,
            monotonic_ns=lambda: 81_000_000_000,
        )
        report = finalizer.reconcile_pending()
        clip = catalog.get_clip(CLIP_ID)

    assert report.intents_examined == 1
    assert report.completed == 1
    assert not report.more_work
    assert clip.pair_reconciled
    assert clip.video_path == plan.intent.paths.video_target
    assert clip.sidecar_path == plan.intent.paths.sidecar_target
    persisted = (root / clip.sidecar_path).read_bytes()
    assert persisted == plan.sidecar.to_canonical_json()
    assert parse_sidecar_bytes(persisted).clip_id == plan.sidecar.clip_id
