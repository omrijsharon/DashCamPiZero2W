from __future__ import annotations

import os
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from dashcam import rollback as rollback_module
from dashcam.catalog import CatalogClip, CatalogConflictError, ClipCatalog, RootedFilesystem
from dashcam.catalog.database import RetentionThresholdLatch
from dashcam.catalog.models import ReconciliationBounds
from dashcam.config import ConfigError, DashcamConfig, StorageConfig, load_config
from dashcam.rollback import (
    DEFAULT_IDENTITY_PATH,
    RollbackSafetyError,
    quiesce_for_rollback,
    run_pre_camera_guard,
)
from dashcam.state import ClipLifecycle, StorageState
from dashcam.storage.naming import finalized_clip_pair, provisional_clip_pair
from dashcam.storage.preflight import (
    MountFacts,
    PreflightReason,
    PreflightResult,
    RecordingRootFacts,
    SpaceFacts,
)

BOOT_ID = "12345678-1234-5678-9234-567812345678"
CAPACITY = 24 * 1024**3
HIGH = (CAPACITY * 20 + 99) // 100
FREE = 6 * 1024**3
UUID_TEXT = "7EED-3EA7"


def test_quiesce_uses_the_installed_storage_identity_by_default() -> None:
    assert Path("/etc/dashcam/storage-volume.env") == DEFAULT_IDENTITY_PATH


def _space() -> tuple[int, int]:
    return CAPACITY, FREE


def _device_id(path: Path) -> str:
    device = path.stat().st_dev
    return (
        f"{os.major(device)}:{os.minor(device)}"
        if hasattr(os, "major") and hasattr(os, "minor")
        else str(device)
    )


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, DashcamConfig, PreflightResult]:
    root = tmp_path / "recording"
    root.mkdir()
    for directory in ("pending", "clips", "protected", "quarantine"):
        (root / directory).mkdir()
    catalog_path = tmp_path / "catalog.sqlite3"
    socket_path = tmp_path / "control.sock"
    config = replace(
        DashcamConfig(),
        storage=replace(StorageConfig(), recording_root=str(root)),
    )
    device_id = _device_id(root)
    facts = RecordingRootFacts(
        mount=MountFacts(
            target=str(root),
            mounted=True,
            source="/dev/test",
            filesystem="exfat",
            label="DASHCAM",
            uuid=UUID_TEXT,
            mount_options=("rw",),
            device_id=device_id,
            os_root_device_id="foreign",
        ),
        space=SpaceFacts(capacity_bytes=CAPACITY, free_bytes=FREE),
        sentinel=None,
    )
    return (
        root,
        catalog_path,
        socket_path,
        config,
        PreflightResult(StorageState.READY, (), facts, True, True),
    )


def _downgrade_fixture_to_schema4(catalog_path: Path) -> None:
    with ClipCatalog(catalog_path):
        pass
    with sqlite3.connect(catalog_path) as connection:
        connection.execute("DROP TABLE retention_threshold_latch")
        connection.execute("DELETE FROM schema_migrations WHERE version = 5")
        connection.execute("PRAGMA user_version = 4")


def _clip(number: int, *, lifecycle: ClipLifecycle = ClipLifecycle.FINALIZED) -> CatalogClip:
    pair = finalized_clip_pair(
        utc_started_at=datetime(2026, 8, 10, 0, 0, number, tzinfo=UTC),
        boot_id="abcde",
        sequence=number,
    )
    return CatalogClip(
        clip_id=UUID(int=number),
        lifecycle=lifecycle,
        video_path=f"clips/{pair.video_name}",
        sidecar_path=f"clips/{pair.metadata_name}",
        start_monotonic_ns=number * 100,
        end_monotonic_ns=(number * 100 + 50 if lifecycle is ClipLifecycle.FINALIZED else None),
        retention_order=number,
        size_bytes=10 if lifecycle is ClipLifecycle.FINALIZED else 0,
        protected=False,
        protection_reason=None,
        pair_reconciled=lifecycle is ClipLifecycle.FINALIZED,
        managed=True,
    )


def _write_pair(root: Path, clip: CatalogClip) -> None:
    (root / clip.video_path).write_bytes(b"video")
    (root / clip.sidecar_path).write_bytes(b"{}")


def _initialize_safe_latch(root: Path, catalog_path: Path) -> None:
    filesystem = RootedFilesystem(root)
    with ClipCatalog(catalog_path) as catalog:
        catalog.initialize_rollback_latch_if_quiescent(
            RetentionThresholdLatch(UUID_TEXT, CAPACITY, False),
            filesystem,
            minimum_free_bytes=HIGH,
            space_observer=_space,
        )


def test_schema4_quiesce_migrates_initializes_and_is_idempotent(tmp_path: Path) -> None:
    root, catalog_path, socket_path, config, preflight = _fixture(tmp_path)
    del root
    _downgrade_fixture_to_schema4(catalog_path)

    first = quiesce_for_rollback(
        config,
        preflight,
        catalog_path=catalog_path,
        control_socket_path=socket_path,
        boot_id=BOOT_ID,
        monotonic_ns=iter(range(100, 200)).__next__,
        preflight_refresh=lambda: preflight,
        space_observer=_space,
    )
    assert first.schema_before == 4
    assert first.schema_after == 5
    assert first.latch_initialized
    assert first.guard.free_bytes == FREE

    second = quiesce_for_rollback(
        config,
        preflight,
        catalog_path=catalog_path,
        control_socket_path=socket_path,
        boot_id=BOOT_ID,
        monotonic_ns=iter(range(200, 300)).__next__,
        space_observer=_space,
    )
    assert second.schema_before == 5
    assert not second.latch_initialized
    assert second.actions_attempted == 0


def test_missing_latch_cannot_initialize_without_post_recovery_preflight(
    tmp_path: Path,
) -> None:
    _, catalog_path, socket_path, config, preflight = _fixture(tmp_path)
    _downgrade_fixture_to_schema4(catalog_path)

    with pytest.raises(RollbackSafetyError, match="fresh post-recovery storage preflight"):
        quiesce_for_rollback(
            config,
            preflight,
            catalog_path=catalog_path,
            control_socket_path=socket_path,
            boot_id=BOOT_ID,
            monotonic_ns=iter(range(100, 200)).__next__,
            space_observer=_space,
        )
    with ClipCatalog(catalog_path) as catalog:
        assert catalog.retention_threshold_latch() is None


def test_quiesce_refuses_future_schema_without_rewriting_it(tmp_path: Path) -> None:
    _, catalog_path, socket_path, config, preflight = _fixture(tmp_path)
    with ClipCatalog(catalog_path):
        pass
    with sqlite3.connect(catalog_path) as connection:
        connection.execute("PRAGMA user_version = 6")

    with pytest.raises(RollbackSafetyError, match="only catalog schema 4 or exact schema 5"):
        quiesce_for_rollback(
            config,
            preflight,
            catalog_path=catalog_path,
            control_socket_path=socket_path,
            boot_id=BOOT_ID,
            space_observer=_space,
        )
    with sqlite3.connect(catalog_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (6,)


@pytest.mark.parametrize(
    "lease_boot_id",
    [BOOT_ID, "87654321-4321-6789-a234-567812345678"],
)
def test_quiesce_clears_expired_or_previous_boot_lease_before_guard(
    tmp_path: Path,
    lease_boot_id: str,
) -> None:
    root, catalog_path, socket_path, config, preflight = _fixture(tmp_path)
    _downgrade_fixture_to_schema4(catalog_path)
    clip = _clip(1)
    _write_pair(root, clip)
    with ClipCatalog(catalog_path) as catalog:
        catalog.register_clip(clip)
        catalog.acquire_download_lease(
            clip.clip_id,
            holder="abandoned-holder",
            monotonic_now_ns=10,
            duration_ns=50,
            boot_id=lease_boot_id,
        )

    report = quiesce_for_rollback(
        config,
        preflight,
        catalog_path=catalog_path,
        control_socket_path=socket_path,
        boot_id=BOOT_ID,
        monotonic_ns=iter(range(100, 300)).__next__,
        preflight_refresh=lambda: preflight,
        space_observer=_space,
    )
    assert report.expired_leases_cleared == 1
    with ClipCatalog(catalog_path) as catalog:
        assert catalog.get_clip(clip.clip_id).download_lease is None


@pytest.mark.parametrize(
    "observed,match",
    [
        ((CAPACITY + 1, FREE), "capacity changed"),
        ((CAPACITY, HIGH - 1), "below high watermark"),
    ],
)
def test_latch_initialization_rechecks_capacity_and_free_inside_transaction(
    tmp_path: Path,
    observed: tuple[int, int],
    match: str,
) -> None:
    root, catalog_path, _, _, _ = _fixture(tmp_path)
    filesystem = RootedFilesystem(root)
    with ClipCatalog(catalog_path) as catalog:
        with pytest.raises(CatalogConflictError, match=match):
            catalog.initialize_rollback_latch_if_quiescent(
                RetentionThresholdLatch(UUID_TEXT, CAPACITY, False),
                filesystem,
                minimum_free_bytes=HIGH,
                space_observer=lambda: observed,
            )
        assert catalog.retention_threshold_latch() is None


def test_latch_initialization_rechecks_full_layout_inside_transaction(
    tmp_path: Path,
) -> None:
    root, catalog_path, _, _, _ = _fixture(tmp_path)
    filesystem = RootedFilesystem(root)
    with ClipCatalog(catalog_path) as catalog:

        def drift_layout() -> tuple[int, int]:
            connection = catalog._connection
            connection.execute("DROP INDEX clips_retention_order_idx")
            return _space()

        with pytest.raises(CatalogConflictError, match="catalog is not quiescent"):
            catalog.initialize_rollback_latch_if_quiescent(
                RetentionThresholdLatch(UUID_TEXT, CAPACITY, False),
                filesystem,
                minimum_free_bytes=HIGH,
                space_observer=drift_layout,
            )
        assert catalog.retention_threshold_latch() is None
        assert catalog.inspect_rollback_state(filesystem).schema_layout_valid


def test_latch_initialization_rechecks_schema_version_inside_transaction(
    tmp_path: Path,
) -> None:
    root, catalog_path, _, _, _ = _fixture(tmp_path)
    filesystem = RootedFilesystem(root)
    with ClipCatalog(catalog_path) as catalog:

        def drift_version() -> tuple[int, int]:
            catalog._connection.execute("PRAGMA user_version = 6")
            return _space()

        with pytest.raises(CatalogConflictError, match="catalog is not quiescent"):
            catalog.initialize_rollback_latch_if_quiescent(
                RetentionThresholdLatch(UUID_TEXT, CAPACITY, False),
                filesystem,
                minimum_free_bytes=HIGH,
                space_observer=drift_version,
            )
        assert catalog.retention_threshold_latch() is None
        assert catalog.schema_version == 5


def test_latch_initialization_refuses_a_forged_off_key_row(tmp_path: Path) -> None:
    root, catalog_path, _, _, _ = _fixture(tmp_path)
    filesystem = RootedFilesystem(root)
    with ClipCatalog(catalog_path) as catalog:
        catalog._connection.execute("PRAGMA ignore_check_constraints = ON")
        catalog._connection.execute(
            """
            INSERT INTO retention_threshold_latch (
                singleton, volume_uuid, capacity_bytes, reclaim_latched
            ) VALUES (2, 'EXTRA', 1, 0)
            """
        )
        with pytest.raises(CatalogConflictError, match="catalog is not quiescent"):
            catalog.initialize_rollback_latch_if_quiescent(
                RetentionThresholdLatch(UUID_TEXT, CAPACITY, False),
                filesystem,
                minimum_free_bytes=HIGH,
                space_observer=_space,
            )
        rows = catalog._connection.execute(
            "SELECT singleton FROM retention_threshold_latch ORDER BY singleton"
        ).fetchall()
        assert [int(row["singleton"]) for row in rows] == [2]


def test_quiesce_admits_only_exact_reserve_exhaustion_then_requires_fresh_ready(
    tmp_path: Path,
) -> None:
    _, catalog_path, socket_path, config, ready = _fixture(tmp_path)
    _downgrade_fixture_to_schema4(catalog_path)
    facts = ready.facts
    assert facts is not None
    reserve = PreflightResult(
        StorageState.EMERGENCY,
        (PreflightReason.RESERVE_EXHAUSTED,),
        replace(facts, space=replace(facts.space, free_bytes=1024**3)),
        False,
        False,
    )
    report = quiesce_for_rollback(
        config,
        reserve,
        catalog_path=catalog_path,
        control_socket_path=socket_path,
        boot_id=BOOT_ID,
        monotonic_ns=iter(range(100, 200)).__next__,
        preflight_refresh=lambda: ready,
        space_observer=_space,
    )
    assert report.guard.free_bytes == FREE
    refused = replace(
        reserve,
        reasons=(PreflightReason.READ_ONLY, PreflightReason.RESERVE_EXHAUSTED),
    )
    with pytest.raises(RollbackSafetyError, match="fresh READY"):
        quiesce_for_rollback(
            config,
            refused,
            catalog_path=catalog_path,
            control_socket_path=socket_path,
            boot_id=BOOT_ID,
            space_observer=_space,
        )
    with pytest.raises(RollbackSafetyError, match="fresh READY"):
        quiesce_for_rollback(
            config,
            replace(reserve, state=StorageState.READY),
            catalog_path=catalog_path,
            control_socket_path=socket_path,
            boot_id=BOOT_ID,
            space_observer=_space,
        )


def test_quiesce_refuses_forged_schema4_history_before_migration_or_recovery(
    tmp_path: Path,
) -> None:
    _, catalog_path, socket_path, config, preflight = _fixture(tmp_path)
    _downgrade_fixture_to_schema4(catalog_path)
    with sqlite3.connect(catalog_path) as connection:
        connection.execute(
            "UPDATE schema_migrations SET name='forged' WHERE version=4"
        )
    with pytest.raises(RollbackSafetyError, match="migration history is not exact"):
        quiesce_for_rollback(
            config,
            preflight,
            catalog_path=catalog_path,
            control_socket_path=socket_path,
            boot_id=BOOT_ID,
            space_observer=_space,
        )
    with sqlite3.connect(catalog_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (4,)
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='retention_threshold_latch'"
        ).fetchone() is None


def test_quiesce_refuses_damaged_schema4_layout_without_migrating_or_rewriting(
    tmp_path: Path,
) -> None:
    _, catalog_path, socket_path, config, preflight = _fixture(tmp_path)
    _downgrade_fixture_to_schema4(catalog_path)
    with sqlite3.connect(catalog_path) as connection:
        connection.execute("ALTER TABLE clips RENAME COLUMN managed TO damaged_managed")
    catalog_bytes = catalog_path.read_bytes()

    with pytest.raises(RollbackSafetyError, match="schema layout is not exact"):
        quiesce_for_rollback(
            config,
            preflight,
            catalog_path=catalog_path,
            control_socket_path=socket_path,
            boot_id=BOOT_ID,
            space_observer=_space,
        )

    assert catalog_path.read_bytes() == catalog_bytes
    with sqlite3.connect(catalog_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (4,)
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='retention_threshold_latch'"
        ).fetchone() is None


def test_quiesce_requires_fresh_ready_same_identity_before_latch_insert(tmp_path: Path) -> None:
    _, catalog_path, socket_path, config, ready = _fixture(tmp_path)
    _downgrade_fixture_to_schema4(catalog_path)
    facts = ready.facts
    assert facts is not None
    reserve = PreflightResult(
        StorageState.EMERGENCY,
        (PreflightReason.RESERVE_EXHAUSTED,),
        replace(facts, space=replace(facts.space, free_bytes=1024**3)),
        False,
        False,
    )
    for refreshed, match in (
        (reserve, "fresh READY"),
        (
            replace(ready, facts=replace(facts, mount=replace(facts.mount, uuid="OTHER"))),
            "identity changed",
        ),
    ):
        with pytest.raises(RollbackSafetyError, match=match):
            quiesce_for_rollback(
                config,
                reserve,
                catalog_path=catalog_path,
                control_socket_path=socket_path,
                boot_id=BOOT_ID,
                preflight_refresh=lambda value=refreshed: value,
                space_observer=_space,
            )
        with ClipCatalog(catalog_path) as catalog:
            assert catalog.retention_threshold_latch() is None


@pytest.mark.parametrize(
    "mutation,match",
    [
        ("missing_latch", "latch is missing"),
        ("latched", "latch remains set"),
        ("latch_uuid", "latch UUID differs"),
        ("latch_capacity", "latch capacity differs"),
        ("latch_extra_row", "retention latch rows"),
        ("pending", "pending intents"),
        ("lease", "download leases"),
        ("partial_lease", "download leases"),
        ("next", "NEXT event windows"),
        ("negative_next", "NEXT event windows"),
        ("unknown_lifecycle", "unknown lifecycle"),
        ("active_CREATING", "CREATING lifecycle"),
        ("active_WRITING", "WRITING lifecycle"),
        ("active_FINALIZING", "FINALIZING lifecycle"),
        ("active_DELETING", "DELETING lifecycle"),
        ("video_missing", "VIDEO_MISSING"),
        ("sidecar_missing", "SIDECAR_MISSING"),
        ("pair_unreconciled", "PAIR_NOT_RECONCILED"),
        ("pair_directory", "VIDEO_DIRECTORY_MISMATCH"),
        ("history", "migration history"),
        ("layout", "schema layout"),
    ],
)
def test_pre_camera_guard_refuses_each_catalog_hazard(
    tmp_path: Path,
    mutation: str,
    match: str,
) -> None:
    root, catalog_path, socket_path, config, preflight = _fixture(tmp_path)
    clip = _clip(1)
    _write_pair(root, clip)
    with ClipCatalog(catalog_path) as catalog:
        catalog.register_clip(clip)
    _initialize_safe_latch(root, catalog_path)
    with sqlite3.connect(catalog_path) as connection:
        if mutation == "missing_latch":
            connection.execute("DELETE FROM retention_threshold_latch")
        elif mutation == "latched":
            connection.execute(
                "UPDATE retention_threshold_latch SET reclaim_latched = 1"
            )
        elif mutation == "latch_uuid":
            connection.execute(
                "UPDATE retention_threshold_latch SET volume_uuid = 'OTHER'"
            )
        elif mutation == "latch_capacity":
            connection.execute(
                "UPDATE retention_threshold_latch SET capacity_bytes = capacity_bytes + 1"
            )
        elif mutation == "latch_extra_row":
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                """
                INSERT INTO retention_threshold_latch (
                    singleton, volume_uuid, capacity_bytes, reclaim_latched
                ) VALUES (2, 'EXTRA', 1, 0)
                """
            )
        elif mutation == "pending":
            with ClipCatalog(catalog_path) as catalog:
                catalog.prepare_protect(clip.clip_id, reason="test", monotonic_now_ns=10)
        elif mutation == "lease":
            connection.execute(
                """
                UPDATE clips SET lease_holder='holder', lease_issued_ns=1,
                    lease_expires_ns=100, lease_boot_id=? WHERE clip_id=?
                """,
                (BOOT_ID, str(clip.clip_id)),
            )
        elif mutation == "partial_lease":
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                "UPDATE clips SET lease_issued_ns=1 WHERE clip_id=?",
                (str(clip.clip_id),),
            )
        elif mutation == "next":
            connection.execute(
                """
                INSERT INTO protection_events (
                    event_id, source, triggered_monotonic_ns, current_clip_id,
                    requested_previous, requested_next, missing_previous, remaining_next
                ) VALUES (?, 'api', 1, ?, 0, 1, 0, 1)
                """,
                (str(UUID(int=99)), str(clip.clip_id)),
            )
        elif mutation == "negative_next":
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                """
                INSERT INTO protection_events (
                    event_id, source, triggered_monotonic_ns, current_clip_id,
                    requested_previous, requested_next, missing_previous, remaining_next
                ) VALUES (?, 'api', 1, ?, 0, 1, 0, -1)
                """,
                (str(UUID(int=99)), str(clip.clip_id)),
            )
        elif mutation == "unknown_lifecycle":
            connection.execute(
                "UPDATE clips SET lifecycle='UNKNOWN' WHERE clip_id=?",
                (str(clip.clip_id),),
            )
        elif mutation.startswith("active_"):
            active = _clip(2, lifecycle=ClipLifecycle(mutation.removeprefix("active_")))
            with ClipCatalog(catalog_path) as catalog:
                catalog.register_clip(active)
        elif mutation == "video_missing":
            (root / clip.video_path).unlink()
        elif mutation == "sidecar_missing":
            (root / clip.sidecar_path).unlink()
        elif mutation == "pair_unreconciled":
            connection.execute(
                "UPDATE clips SET pair_reconciled=0 WHERE clip_id=?",
                (str(clip.clip_id),),
            )
        elif mutation == "pair_directory":
            connection.execute(
                "UPDATE clips SET video_path=? WHERE clip_id=?",
                (clip.video_path.replace("clips/", "protected/"), str(clip.clip_id)),
            )
        elif mutation == "history":
            connection.execute(
                "UPDATE schema_migrations SET name='forged' WHERE version=5"
            )
        elif mutation == "layout":
            connection.execute(
                "ALTER TABLE retention_threshold_latch RENAME TO old_latch"
            )
            connection.execute(
                """
                CREATE TABLE retention_threshold_latch (
                    singleton INTEGER PRIMARY KEY, volume_uuid TEXT,
                    capacity_bytes INTEGER, reclaim_latched INTEGER
                )
                """
            )
            connection.execute(
                """
                INSERT INTO retention_threshold_latch
                SELECT * FROM old_latch
                """
            )
            connection.execute("DROP TABLE old_latch")
    with pytest.raises(RollbackSafetyError, match=match):
        run_pre_camera_guard(
            config,
            preflight,
            catalog_path=catalog_path,
            control_socket_path=socket_path,
            space_observer=_space,
        )


def test_guard_refuses_capacity_uuid_free_and_existing_socket(tmp_path: Path) -> None:
    root, catalog_path, socket_path, config, preflight = _fixture(tmp_path)
    _initialize_safe_latch(root, catalog_path)
    assert run_pre_camera_guard(
        config,
        preflight,
        catalog_path=catalog_path,
        control_socket_path=socket_path,
        space_observer=_space,
    )
    for observed, match in (
        ((CAPACITY + 1, FREE), "capacity changed"),
        ((CAPACITY, HIGH - 1), "high watermark"),
    ):
        with pytest.raises(RollbackSafetyError, match=match):
            run_pre_camera_guard(
                config,
                preflight,
                catalog_path=catalog_path,
                control_socket_path=socket_path,
                space_observer=lambda value=observed: value,
            )
    facts = preflight.facts
    assert facts is not None
    for changed, match in (
        (replace(facts, mount=replace(facts.mount, uuid="OTHER")), "UUID differs"),
        (
            replace(facts, mount=replace(facts.mount, device_id="999:999")),
            "device identity differs",
        ),
        (
            replace(facts, space=replace(facts.space, capacity_bytes=CAPACITY + 1)),
            "capacity changed",
        ),
        (replace(facts, space=replace(facts.space, free_bytes=HIGH - 1)), "high watermark"),
    ):
        with pytest.raises(RollbackSafetyError, match=match):
            run_pre_camera_guard(
                config,
                replace(preflight, facts=changed),
                catalog_path=catalog_path,
                control_socket_path=socket_path,
                space_observer=_space,
            )
    assert run_pre_camera_guard(
        config,
        replace(
            preflight,
            facts=replace(facts, space=replace(facts.space, free_bytes=HIGH)),
        ),
        catalog_path=catalog_path,
        control_socket_path=socket_path,
        space_observer=_space,
    )
    socket_path.write_text("foreign", encoding="ascii")
    with pytest.raises(RollbackSafetyError, match="socket path still exists"):
        run_pre_camera_guard(
            config,
            preflight,
            catalog_path=catalog_path,
            control_socket_path=socket_path,
            space_observer=_space,
        )


def test_guard_refuses_when_finalized_pair_audit_exceeds_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, catalog_path, socket_path, config, preflight = _fixture(tmp_path)
    with ClipCatalog(catalog_path) as catalog:
        for number in (1, 2):
            clip = _clip(number)
            _write_pair(root, clip)
            catalog.register_clip(clip)
    _initialize_safe_latch(root, catalog_path)
    monkeypatch.setattr(rollback_module, "MAX_FINALIZED_CLIPS", 1)

    with pytest.raises(RollbackSafetyError, match="finalized clip audit bound"):
        run_pre_camera_guard(
            config,
            preflight,
            catalog_path=catalog_path,
            control_socket_path=socket_path,
            space_observer=_space,
        )


def test_guard_keeps_valid_delete_journal_catalog_byte_exact(tmp_path: Path) -> None:
    root, catalog_path, socket_path, config, preflight = _fixture(tmp_path)
    _initialize_safe_latch(root, catalog_path)
    with sqlite3.connect(catalog_path) as connection:
        assert connection.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
    catalog_bytes = catalog_path.read_bytes()

    assert run_pre_camera_guard(
        config,
        preflight,
        catalog_path=catalog_path,
        control_socket_path=socket_path,
        space_observer=_space,
    )

    assert catalog_path.read_bytes() == catalog_bytes
    with sqlite3.connect(catalog_path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)


def test_guard_rechecks_full_layout_after_space_observation_seam(tmp_path: Path) -> None:
    root, catalog_path, socket_path, config, preflight = _fixture(tmp_path)
    _initialize_safe_latch(root, catalog_path)

    def drift_layout() -> tuple[int, int]:
        with sqlite3.connect(catalog_path) as connection:
            connection.execute("DROP INDEX clips_retention_order_idx")
        return _space()

    with pytest.raises(RollbackSafetyError, match="catalog is not rollback-quiescent"):
        run_pre_camera_guard(
            config,
            preflight,
            catalog_path=catalog_path,
            control_socket_path=socket_path,
            space_observer=drift_layout,
        )


def test_guard_rechecks_schema_version_after_space_observation_seam(tmp_path: Path) -> None:
    root, catalog_path, socket_path, config, preflight = _fixture(tmp_path)
    _initialize_safe_latch(root, catalog_path)

    def drift_version() -> tuple[int, int]:
        with sqlite3.connect(catalog_path) as connection:
            connection.execute("PRAGMA user_version = 6")
        return _space()

    with pytest.raises(RollbackSafetyError, match="schema version"):
        run_pre_camera_guard(
            config,
            preflight,
            catalog_path=catalog_path,
            control_socket_path=socket_path,
            space_observer=drift_version,
        )
    with sqlite3.connect(catalog_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (6,)


def test_quiesce_replays_protect_and_demotes_orphan_writing(tmp_path: Path) -> None:
    root, catalog_path, socket_path, config, preflight = _fixture(tmp_path)
    _downgrade_fixture_to_schema4(catalog_path)
    finalized = _clip(1)
    _write_pair(root, finalized)
    partial = provisional_clip_pair(boot_id="abcdef123456", sequence=2)
    writing = CatalogClip(
        clip_id=UUID(int=2),
        lifecycle=ClipLifecycle.WRITING,
        video_path=f"pending/{partial.video_name}",
        sidecar_path=f"pending/{partial.metadata_name}",
        start_monotonic_ns=200,
        end_monotonic_ns=None,
        retention_order=2,
        size_bytes=0,
        protected=False,
        protection_reason=None,
        pair_reconciled=False,
        managed=True,
    )
    (root / writing.video_path).write_bytes(b"partial")
    with ClipCatalog(catalog_path) as catalog:
        catalog.register_clip(finalized)
        catalog.register_clip(writing)
        catalog.prepare_protect(finalized.clip_id, reason="event", monotonic_now_ns=10)

    report = quiesce_for_rollback(
        config,
        preflight,
        catalog_path=catalog_path,
        control_socket_path=socket_path,
        boot_id=BOOT_ID,
        monotonic_ns=iter(range(100, 300)).__next__,
        preflight_refresh=lambda: preflight,
        space_observer=_space,
    )
    assert report.orphaned_writing_demoted == 1
    assert report.actions_attempted == 2
    with ClipCatalog(catalog_path) as catalog:
        assert catalog.get_clip(finalized.clip_id).video_path.startswith("protected/")
        assert catalog.get_clip(writing.clip_id).lifecycle is ClipLifecycle.MISSING_SIDECAR


def test_quiesce_replays_delete_and_unprotect_without_resetting_rows(tmp_path: Path) -> None:
    root, catalog_path, socket_path, config, preflight = _fixture(tmp_path)
    _downgrade_fixture_to_schema4(catalog_path)
    deleted = _clip(1)
    protected_base = _clip(2)
    protected = replace(
        protected_base,
        video_path=protected_base.video_path.replace("clips/", "protected/"),
        sidecar_path=protected_base.sidecar_path.replace("clips/", "protected/"),
        protected=True,
        protection_reason="event",
    )
    _write_pair(root, deleted)
    _write_pair(root, protected)
    with ClipCatalog(catalog_path) as catalog:
        catalog.register_clip(deleted)
        catalog.register_clip(protected)
        catalog.prepare_delete(deleted.clip_id, monotonic_now_ns=10, boot_id=BOOT_ID)
        catalog.prepare_unprotect(protected.clip_id, monotonic_now_ns=11)

    report = quiesce_for_rollback(
        config,
        preflight,
        catalog_path=catalog_path,
        control_socket_path=socket_path,
        boot_id=BOOT_ID,
        monotonic_ns=iter(range(100, 300)).__next__,
        preflight_refresh=lambda: preflight,
        space_observer=_space,
    )
    assert report.actions_attempted == 4
    with ClipCatalog(catalog_path) as catalog:
        deleted_after = catalog.get_clip(deleted.clip_id)
        protected_after = catalog.get_clip(protected.clip_id)
        assert deleted_after.lifecycle is ClipLifecycle.DELETED
        assert protected_after.lifecycle is ClipLifecycle.FINALIZED
        assert not protected_after.protected
        assert protected_after.video_path.startswith("clips/")


def test_quiesce_refuses_when_recovery_exceeds_total_pass_bound(tmp_path: Path) -> None:
    root, catalog_path, socket_path, config, preflight = _fixture(tmp_path)
    _downgrade_fixture_to_schema4(catalog_path)
    with ClipCatalog(catalog_path) as catalog:
        for number in (1, 2):
            clip = _clip(number)
            _write_pair(root, clip)
            catalog.register_clip(clip)
            catalog.prepare_protect(clip.clip_id, reason="event", monotonic_now_ns=number)
    with pytest.raises(RollbackSafetyError, match="pass budget"):
        quiesce_for_rollback(
            config,
            preflight,
            catalog_path=catalog_path,
            control_socket_path=socket_path,
            boot_id=BOOT_ID,
            monotonic_ns=iter(range(100, 300)).__next__,
            max_passes=1,
            bounds=ReconciliationBounds(max_intents=1, max_actions=2),
            space_observer=_space,
        )


def test_rollback_config_rejects_candidate_only_key(tmp_path: Path) -> None:
    source = Path("config/default.toml").read_text(encoding="utf-8")
    candidate = source.replace(
        "emergency_free_mib = 256",
        "emergency_free_mib = 256\ndownload_lease_timeout_s = 300",
    )
    path = tmp_path / "candidate.toml"
    path.write_text(candidate, encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(path)


def test_socket_activation_is_retired_and_web_remains_dormant() -> None:
    assert not Path("systemd/dashcamd.socket").exists()
    installer = Path("deploy/ssh-dev-app/install.py").read_text(encoding="utf-8")
    dormant = installer.split("DORMANT_UNITS: Final = (", 1)[1].split(")", 1)[0]
    assert '"dashcamd.socket"' in dormant
    assert '"dashcam-web.service"' in dormant
    web = Path("systemd/dashcam-web.service").read_text(encoding="utf-8")
    assert "dashcamd.socket" not in web
    assert "SupplementaryGroups=dashcam-api" not in web
