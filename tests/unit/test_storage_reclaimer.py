from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

import pytest

from dashcam.catalog.database import ClipCatalog
from dashcam.catalog.models import CatalogClip
from dashcam.recorder.finalizer import DurableRootedFinalizationFilesystem
from dashcam.state import ClipLifecycle
from dashcam.storage.intents import IntentKind
from dashcam.storage.reclaimer import StorageReclaimer


def clip(number: int, *, protected: bool = False) -> CatalogClip:
    return CatalogClip(
        clip_id=UUID(int=number),
        lifecycle=ClipLifecycle.FINALIZED,
        video_path=f"clips/clip-{number}.mp4",
        sidecar_path=f"clips/clip-{number}.json",
        start_monotonic_ns=number * 100,
        end_monotonic_ns=number * 100 + 50,
        retention_order=number,
        size_bytes=100,
        protected=protected,
        protection_reason="fixture" if protected else None,
        pair_reconciled=True,
        managed=True,
    )


class Filesystem:
    def __init__(self, catalog: ClipCatalog, paths: set[str]) -> None:
        self.catalog = catalog
        self.paths = paths
        self.unlinked: list[str] = []
        self.fail_after_unlinks: int | None = None

    def exists(self, relative_path: str) -> bool:
        return relative_path in self.paths

    def unlink(self, relative_path: str) -> None:
        clip_id = UUID(int=int(relative_path.split("-")[1].split(".")[0]))
        assert self.catalog.get_clip(clip_id).lifecycle is ClipLifecycle.DELETING
        pending = self.catalog.list_pending_delete_intents(limit=1)
        assert pending and pending[0].kind is IntentKind.DELETE
        if self.fail_after_unlinks is not None and len(self.unlinked) == self.fail_after_unlinks:
            raise OSError("injected unlink interruption")
        self.paths.discard(relative_path)
        self.unlinked.append(relative_path)

    def move(self, source: str, target: str) -> None:
        raise AssertionError((source, target))

    def iter_files(self, directory: str, *, limit: int) -> tuple[tuple[str, ...], int, bool]:
        raise AssertionError((directory, limit))

    def read_bytes(self, relative_path: str, *, maximum_bytes: int) -> bytes:
        raise AssertionError((relative_path, maximum_bytes))

    def file_size(self, relative_path: str) -> int:
        raise AssertionError(relative_path)

    def replace_bytes_atomic(
        self, relative_path: str, payload: bytes, *, maximum_bytes: int
    ) -> None:
        raise AssertionError((relative_path, payload, maximum_bytes))


def test_one_step_commits_deleting_before_unlink_and_selects_oldest_eligible(
    tmp_path: Path,
) -> None:
    with ClipCatalog(tmp_path / "catalog.sqlite3") as catalog:
        catalog.register_clip(clip(1, protected=True), catalog_now_ns=1)
        catalog.register_clip(clip(2), catalog_now_ns=2)
        catalog.register_clip(clip(3), catalog_now_ns=3)
        paths = {
            "clips/clip-1.mp4",
            "clips/clip-1.json",
            "clips/clip-2.mp4",
            "clips/clip-2.json",
            "clips/clip-3.mp4",
            "clips/clip-3.json",
            "System Volume Information/unknown.bin",
        }
        filesystem = Filesystem(catalog, paths)
        reclaimer = StorageReclaimer(
            catalog=catalog,
            filesystem=filesystem,
            monotonic_ns=lambda: 10,
        )

        outcome = reclaimer.run_one(boot_id="boot-a", allow_new=True)

        assert outcome.clip_id == UUID(int=2)
        assert outcome.deleted and not outcome.recovered
        assert catalog.get_clip(UUID(int=2)).lifecycle is ClipLifecycle.DELETED
        assert catalog.get_clip(UUID(int=1)).lifecycle is ClipLifecycle.FINALIZED
        assert "clips/clip-3.mp4" in paths
        assert "System Volume Information/unknown.bin" in paths


def test_interrupted_half_delete_replays_before_any_new_selection(tmp_path: Path) -> None:
    with ClipCatalog(tmp_path / "catalog.sqlite3") as catalog:
        catalog.register_clip(clip(1), catalog_now_ns=1)
        catalog.register_clip(clip(2), catalog_now_ns=2)
        paths = {
            "clips/clip-1.mp4",
            "clips/clip-1.json",
            "clips/clip-2.mp4",
            "clips/clip-2.json",
        }
        filesystem = Filesystem(catalog, paths)
        filesystem.fail_after_unlinks = 1
        reclaimer = StorageReclaimer(
            catalog=catalog,
            filesystem=filesystem,
            monotonic_ns=lambda: 10,
        )

        with pytest.raises(OSError, match="injected unlink"):
            reclaimer.run_one(boot_id="boot-a", allow_new=True)
        assert catalog.get_clip(UUID(int=1)).lifecycle is ClipLifecycle.DELETING
        assert catalog.get_clip(UUID(int=2)).lifecycle is ClipLifecycle.FINALIZED

        filesystem.fail_after_unlinks = None
        recovered = reclaimer.run_one(boot_id="boot-a", allow_new=False)
        assert recovered.recovered and recovered.clip_id == UUID(int=1)
        assert catalog.get_clip(UUID(int=1)).lifecycle is ClipLifecycle.DELETED
        assert catalog.get_clip(UUID(int=2)).lifecycle is ClipLifecycle.FINALIZED


def test_normal_mode_recovers_prior_intent_but_never_stages_a_new_delete(tmp_path: Path) -> None:
    with ClipCatalog(tmp_path / "catalog.sqlite3") as catalog:
        catalog.register_clip(clip(1), catalog_now_ns=1)
        filesystem = Filesystem(catalog, {"clips/clip-1.mp4", "clips/clip-1.json"})
        reclaimer = StorageReclaimer(
            catalog=catalog,
            filesystem=filesystem,
            monotonic_ns=lambda: 10,
        )

        idle = reclaimer.run_one(boot_id="boot-a", allow_new=False)

        assert not idle.eligible_found and not idle.progress
        assert catalog.get_clip(UUID(int=1)).lifecycle is ClipLifecycle.FINALIZED


def test_multiple_pending_delete_intents_are_replayed_one_pair_per_call(tmp_path: Path) -> None:
    with ClipCatalog(tmp_path / "catalog.sqlite3") as catalog:
        catalog.register_clip(clip(1), catalog_now_ns=1)
        catalog.register_clip(clip(2), catalog_now_ns=2)
        catalog.prepare_delete(UUID(int=1), monotonic_now_ns=3, boot_id="boot-a")
        catalog.prepare_delete(UUID(int=2), monotonic_now_ns=4, boot_id="boot-a")
        filesystem = Filesystem(
            catalog,
            {
                "clips/clip-1.mp4",
                "clips/clip-1.json",
                "clips/clip-2.mp4",
                "clips/clip-2.json",
            },
        )
        reclaimer = StorageReclaimer(
            catalog=catalog,
            filesystem=filesystem,
            monotonic_ns=lambda: 10,
        )

        first = reclaimer.run_one(boot_id="boot-a", allow_new=False)

        assert first.clip_id == UUID(int=1)
        assert first.pending_delete_remaining
        assert catalog.get_clip(UUID(int=1)).lifecycle is ClipLifecycle.DELETED
        assert catalog.get_clip(UUID(int=2)).lifecycle is ClipLifecycle.DELETING
        assert len(catalog.list_pending_delete_intents(limit=10)) == 1


def test_preexisting_half_pair_is_idempotently_completed(tmp_path: Path) -> None:
    with ClipCatalog(tmp_path / "catalog.sqlite3") as catalog:
        catalog.register_clip(clip(1), catalog_now_ns=1)
        # An already missing sidecar makes the first observation an ambiguous
        # half-pair before this process has performed either unlink.
        filesystem = Filesystem(catalog, {"clips/clip-1.mp4"})
        reclaimer = StorageReclaimer(
            catalog=catalog,
            filesystem=filesystem,
            monotonic_ns=lambda: 10,
        )

        # DELETE reconciliation defines member absence as idempotent success;
        # this is the required crash-recovery behavior, not an ambiguity.
        result = reclaimer.run_one(boot_id="boot-a", allow_new=True)
        assert result.deleted
        assert catalog.get_clip(UUID(int=1)).lifecycle is ClipLifecycle.DELETED


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX directory fsync semantics")
def test_retry_fsyncs_observed_absence_before_completing_catalog_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "recording"
    for name in ("pending", "clips", "protected", "quarantine"):
        (root / name).mkdir(parents=True, exist_ok=True)
    video = root / "clips" / "clip-1.mp4"
    sidecar = root / "clips" / "clip-1.json"
    video.write_bytes(b"video")
    sidecar.write_bytes(b"sidecar")
    real_fsync = os.fsync
    fail_once = True

    def injected_fsync(descriptor: int) -> None:
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise OSError("injected directory fsync failure")
        real_fsync(descriptor)

    with ClipCatalog(tmp_path / "catalog.sqlite3") as catalog:
        catalog.register_clip(clip(1), catalog_now_ns=1)
        filesystem = DurableRootedFinalizationFilesystem(root)
        reclaimer = StorageReclaimer(
            catalog=catalog,
            filesystem=filesystem,
            monotonic_ns=lambda: 10,
        )
        monkeypatch.setattr(os, "fsync", injected_fsync)

        with pytest.raises(OSError, match="directory fsync"):
            reclaimer.run_one(boot_id="boot-a", allow_new=True)
        assert not video.exists()
        assert sidecar.exists()
        assert catalog.get_clip(UUID(int=1)).lifecycle is ClipLifecycle.DELETING

        recovered = reclaimer.run_one(boot_id="boot-a", allow_new=False)
        assert recovered.deleted and recovered.recovered
        assert not sidecar.exists()
        assert catalog.get_clip(UUID(int=1)).lifecycle is ClipLifecycle.DELETED
