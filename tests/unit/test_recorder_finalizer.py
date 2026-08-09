from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from dashcam.catalog.database import (
    ActiveClipProtectionState,
    ClipCatalog,
    RetentionThresholdLatch,
)
from dashcam.catalog.filesystem import CatalogFilesystem
from dashcam.catalog.models import (
    CatalogClip,
    ClipNotFoundError,
    EventProtectionResult,
    EventSource,
    IntentReconciliationResult,
)
from dashcam.metadata.reconcile import MetadataReconciliationPlan, parse_sidecar_bytes
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
    FinalizationOutcome,
    FinalizationRefused,
    FinalizerLimits,
    RecorderClipFinalizer,
)
from dashcam.state import (
    ClipLifecycle,
    GpsTimeState,
    SystemClockState,
    TimestampQuality,
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

SOURCE_NAME = "boot-abcdef123456-000007.partial.mp4"
TARGET_VIDEO = "20260726T120000.000Z_abcdef123456_s000007.mp4"
TARGET_SIDECAR = "20260726T120000.000Z_abcdef123456_s000007.json"
STAGED_SIDECAR = "boot-abcdef123456-000007.partial.json"
CLIP_ID = UUID("12345678-1234-5678-9234-567812345678")
BOOT_ID = UUID("abcdef12-3456-4789-a234-678943216789")
START_UTC = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


class InjectedCrash(RuntimeError):
    pass


class FakePromotionCatalog:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.clips: dict[UUID, CatalogClip] = {}
        self.intents: dict[UUID, OperationIntent] = {}
        self.complete: set[UUID] = set()
        self.next_intent = 1
        self.fail_registration_once = False
        self.fail_before_actions_once = False
        self.fail_before_completion_once = False
        self.retention_latch: RetentionThresholdLatch | None = None

    def retention_threshold_latch(self) -> RetentionThresholdLatch | None:
        return self.retention_latch

    def store_retention_threshold_latch(self, latch: RetentionThresholdLatch) -> None:
        self.retention_latch = latch

    def next_retention_order(self) -> int:
        return 0

    def list_metadata_reconciliation_candidates(
        self,
        expected_boot_id: UUID,
        *,
        limit: int,
        after_order: int = -1,
        after_clip_id: UUID | None = None,
    ) -> tuple[CatalogClip, ...]:
        del expected_boot_id, limit, after_order, after_clip_id
        return ()

    def register_name_reconciliation(
        self,
        plan: MetadataReconciliationPlan,
        *,
        source_sidecar: ClipSidecar,
        monotonic_now_ns: int,
    ) -> UUID:
        del plan, source_sidecar, monotonic_now_ns
        raise AssertionError("name reconciliation is outside this finalizer fixture")

    def register_finalizing_clip(
        self,
        clip: CatalogClip,
        *,
        promotion_paths: PairPaths,
        monotonic_now_ns: int,
        expected_protection_revision: int | None = None,
    ) -> UUID:
        del expected_protection_revision
        self.events.append("catalog-register")
        if self.fail_registration_once:
            self.fail_registration_once = False
            raise InjectedCrash("catalog commit failed")
        if clip.clip_id in self.clips:
            raise RuntimeError("duplicate clip")
        intent = OperationIntent(
            intent_id=UUID(int=self.next_intent),
            clip_id=clip.clip_id,
            kind=IntentKind.FINALIZE,
            created_monotonic_ns=monotonic_now_ns,
            paths=promotion_paths,
        )
        self.next_intent += 1
        # This assignment models the required single durable transaction.
        self.clips[clip.clip_id] = clip
        self.intents[intent.intent_id] = intent
        return intent.intent_id

    def register_writing_clip(self, clip: CatalogClip, *, monotonic_now_ns: int) -> None:
        del monotonic_now_ns
        if clip.clip_id in self.clips:
            raise RuntimeError("duplicate clip")
        self.clips[clip.clip_id] = clip

    def active_closing_protection(
        self,
        clip_id: UUID,
        *,
        monotonic_now_ns: int,
    ) -> ActiveClipProtectionState:
        del monotonic_now_ns
        clip = self.clips[clip_id]
        return ActiveClipProtectionState(clip.protected, clip.protection_reason, 0)

    def trigger_event(
        self,
        current_clip_id: UUID | None,
        *,
        source: EventSource,
        monotonic_now_ns: int,
        previous_count: int = 2,
        next_count: int = 1,
        event_id: UUID | None = None,
    ) -> EventProtectionResult:
        del source, monotonic_now_ns, previous_count
        if current_clip_id is None:
            raise RuntimeError("new fake event requires a current clip")
        return EventProtectionResult(
            event_id=UUID(int=999) if event_id is None else event_id,
            protected_clip_ids=(current_clip_id,),
            missing_previous_count=0,
            pending_next_count=next_count,
            queued_intent_ids=(),
        )

    def list_writing_clips(self, *, limit: int) -> tuple[CatalogClip, ...]:
        return tuple(
            clip for clip in self.clips.values() if clip.lifecycle is ClipLifecycle.WRITING
        )[:limit]

    def mark_writing_clip_orphaned(self, clip_id: UUID, *, monotonic_now_ns: int) -> bool:
        del monotonic_now_ns
        clip = self.clips[clip_id]
        if clip.lifecycle is not ClipLifecycle.WRITING:
            return False
        self.clips[clip_id] = replace(clip, lifecycle=ClipLifecycle.MISSING_SIDECAR)
        return True

    def get_clip(self, clip_id: UUID) -> CatalogClip:
        try:
            return self.clips[clip_id]
        except KeyError as exc:
            raise ClipNotFoundError(str(clip_id)) from exc

    def list_pending_intents(self, *, limit: int) -> tuple[OperationIntent, ...]:
        values = sorted(
            (
                intent
                for intent_id, intent in self.intents.items()
                if intent_id not in self.complete
            ),
            key=lambda intent: (intent.created_monotonic_ns, str(intent.intent_id)),
        )
        return tuple(values[:limit])

    def list_pending_delete_intents(self, *, limit: int) -> tuple[OperationIntent, ...]:
        return tuple(
            intent
            for intent in self.list_pending_intents(limit=limit)
            if intent.kind is IntentKind.DELETE
        )

    def list_pending_intents_by_kind(
        self,
        *,
        kinds: tuple[IntentKind, ...],
        limit: int,
    ) -> tuple[OperationIntent, ...]:
        values = sorted(
            (
                intent
                for intent_id, intent in self.intents.items()
                if intent_id not in self.complete and intent.kind in kinds
            ),
            key=lambda intent: (intent.created_monotonic_ns, str(intent.intent_id)),
        )
        return tuple(values[:limit])

    def get_pending_intent(self, intent_id: UUID) -> OperationIntent | None:
        intent = self.intents.get(intent_id)
        return None if intent_id in self.complete else intent

    def list_pending_intents_for_clip(
        self,
        clip_id: UUID,
        *,
        kinds: tuple[IntentKind, ...],
        limit: int,
    ) -> tuple[OperationIntent, ...]:
        values = sorted(
            (
                intent
                for intent_id, intent in self.intents.items()
                if intent_id not in self.complete
                and intent.clip_id == clip_id
                and intent.kind in kinds
            ),
            key=lambda intent: (intent.created_monotonic_ns, str(intent.intent_id)),
        )
        return tuple(values[:limit])

    def prepare_oldest_eligible_delete(
        self,
        *,
        monotonic_now_ns: int,
        boot_id: str,
    ) -> UUID | None:
        del monotonic_now_ns, boot_id
        return None

    def reconcile_intent(
        self,
        intent_id: UUID,
        filesystem: CatalogFilesystem,
        *,
        monotonic_now_ns: int,
        max_actions: int = 2,
    ) -> IntentReconciliationResult:
        del monotonic_now_ns
        intent = self.intents[intent_id]
        if intent_id in self.complete:
            return IntentReconciliationResult(intent, 0, True, ())
        if self.fail_before_actions_once:
            self.fail_before_actions_once = False
            raise InjectedCrash("power loss after intent commit")

        plan = _plan(intent, filesystem)
        if plan.problems:
            return IntentReconciliationResult(
                intent,
                0,
                False,
                tuple(problem.code for problem in plan.problems),
            )
        attempted = 0
        for action in plan.actions[:max_actions]:
            if action.kind is ActionKind.MOVE:
                assert action.target is not None
                filesystem.move(action.source, action.target)
            else:
                filesystem.unlink(action.source)
            attempted += 1
        final_plan = _plan(intent, filesystem)
        if final_plan.complete:
            if self.fail_before_completion_once:
                self.fail_before_completion_once = False
                raise InjectedCrash("power loss after both renames")
            clip = self.clips[intent.clip_id]
            assert intent.paths.video_target is not None
            assert intent.paths.sidecar_target is not None
            self.clips[intent.clip_id] = replace(
                clip,
                lifecycle=ClipLifecycle.FINALIZED,
                video_path=intent.paths.video_target,
                sidecar_path=intent.paths.sidecar_target,
                pair_reconciled=True,
            )
            self.complete.add(intent_id)
        return IntentReconciliationResult(
            intent,
            attempted,
            final_plan.complete,
            tuple(problem.code for problem in final_plan.problems),
        )


class InstrumentedFilesystem(DurableRootedFinalizationFilesystem):
    def __init__(self, root: Path, events: list[str]) -> None:
        super().__init__(root)
        self.events = events
        self.moves = 0
        self.fail_after_move: int | None = None

    def write_staged_sidecar(self, relative_path: str, sidecar: ClipSidecar) -> None:
        super().write_staged_sidecar(relative_path, sidecar)
        self.events.append("sidecar-durable")

    def move(self, source: str, target: str) -> None:
        super().move(source, target)
        self.moves += 1
        self.events.append(f"move:{source}")
        if self.fail_after_move == self.moves:
            self.fail_after_move = None
            raise InjectedCrash("power loss after rename")


def _plan(intent: OperationIntent, filesystem: CatalogFilesystem) -> ReconciliationPlan:
    assert intent.paths.video_target is not None
    assert intent.paths.sidecar_target is not None
    return plan_reconciliation(
        intent,
        video=MemberObservation(
            filesystem.exists(intent.paths.video_source),
            filesystem.exists(intent.paths.video_target),
        ),
        sidecar=MemberObservation(
            filesystem.exists(intent.paths.sidecar_source),
            filesystem.exists(intent.paths.sidecar_target),
        ),
    )


def _sidecar() -> ClipSidecar:
    return ClipSidecar(
        schema_version=1,
        clip_id=CLIP_ID,
        boot_id=BOOT_ID,
        sequence=7,
        video_file=TARGET_VIDEO,
        metadata_file=TARGET_SIDECAR,
        start_utc=START_UTC,
        end_utc=START_UTC + timedelta(seconds=1),
        start_monotonic_ns=10_000,
        end_monotonic_ns=1_000_010_000,
        gps_time_state=GpsTimeState.UNSYNCED,
        system_clock_state=SystemClockState.SYNCHRONIZED,
        timestamp_quality=TimestampQuality.SYSTEM_DERIVED,
        time_anchor=TimeAnchor(
            source=TimeAnchorSource.SYSTEM_CLOCK,
            monotonic_ns=10_000,
            utc=START_UTC,
            uncertainty_ns=10_000_000,
            provenance="injected synchronized system-clock snapshot",
        ),
        timezone="Asia/Jerusalem",
        start_local=START_UTC.astimezone(ZoneInfo("Asia/Jerusalem")),
        video=VideoSummary("h264", 1920, 1080, 30.0, 8_000_000, 7_900_000, 30, 0),
        audio=AudioSummary(False, None, None, None, None),
        gps=GpsSummary(False, None),
        protected=False,
        protection_reason=None,
        software_version="test-release",
    )


def _unsynced_sidecar() -> ClipSidecar:
    return replace(
        _sidecar(),
        video_file="boot-abcdef123456-000007.mp4",
        metadata_file="boot-abcdef123456-000007.json",
        start_utc=None,
        end_utc=None,
        start_local=None,
        gps_time_state=GpsTimeState.UNSYNCED,
        system_clock_state=SystemClockState.UNSET,
        timestamp_quality=TimestampQuality.MONOTONIC_ONLY,
        time_anchor=None,
    )


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "recording"
    for directory in ("pending", "clips", "protected", "quarantine"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    (root / "pending" / SOURCE_NAME).write_bytes(b"closed fragmented MP4")
    return root


def _components(
    tmp_path: Path,
) -> tuple[RecorderClipFinalizer, FakePromotionCatalog, InstrumentedFilesystem, list[str]]:
    events: list[str] = []
    filesystem = InstrumentedFilesystem(_root(tmp_path), events)
    catalog = FakePromotionCatalog(events)
    ticks = iter(range(100, 1_000))
    finalizer = RecorderClipFinalizer(
        catalog=catalog,
        filesystem=filesystem,
        monotonic_ns=lambda: next(ticks),
    )
    return finalizer, catalog, filesystem, events


def _finalize(finalizer: RecorderClipFinalizer) -> FinalizationOutcome:
    return finalizer.finalize(
        provisional_video_name=SOURCE_NAME,
        sidecar=_sidecar(),
        retention_order=10_000,
    )


def test_finalizer_persists_canonical_sidecar_and_intent_before_pair_moves(
    tmp_path: Path,
) -> None:
    finalizer, catalog, filesystem, events = _components(tmp_path)

    outcome = _finalize(finalizer)

    assert outcome.complete
    assert outcome.actions_attempted == 2
    assert events.index("sidecar-durable") < events.index("catalog-register")
    assert events.index("catalog-register") < events.index(f"move:pending/{SOURCE_NAME}")
    assert not filesystem.exists(f"pending/{SOURCE_NAME}")
    assert filesystem.exists(f"clips/{TARGET_VIDEO}")
    payload = filesystem.read_bytes(f"clips/{TARGET_SIDECAR}", maximum_bytes=1_048_576)
    assert parse_sidecar_bytes(payload) == _sidecar()
    assert ".partial" not in catalog.get_clip(CLIP_ID).video_path


def test_active_event_clip_keeps_uuid_and_protected_sidecar_through_finalize(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    filesystem = InstrumentedFilesystem(root, [])
    with ClipCatalog(tmp_path / "catalog.sqlite3") as catalog:
        finalizer = _real_finalizer(root, catalog, filesystem)
        finalizer.register_active_clip(
            provisional_video_name=SOURCE_NAME,
            clip_id=CLIP_ID,
            start_monotonic_ns=10_000,
            retention_order=10_000,
        )
        event = finalizer.trigger_event(
            CLIP_ID,
            source=EventSource.WEB,
            monotonic_now_ns=2_000,
            previous_count=0,
            next_count=0,
            event_id=UUID(int=900),
        )

        outcome = _finalize(finalizer)

        durable = catalog.get_clip(CLIP_ID)
        assert outcome.clip_id == CLIP_ID == durable.clip_id
        assert durable.lifecycle is ClipLifecycle.FINALIZED
        assert durable.protected and durable.pair_reconciled
        assert durable.video_path.startswith("protected/")
        payload = filesystem.read_bytes(durable.sidecar_path, maximum_bytes=1_048_576)
        sidecar = parse_sidecar_bytes(payload)
        assert sidecar.clip_id == CLIP_ID
        assert sidecar.protected
        assert sidecar.protection_reason == f"event:{event.event_id}:web"
        assert catalog.list_pending_intents(limit=1) == ()


def test_real_catalog_promotes_unsynced_clip_without_partial_target(tmp_path: Path) -> None:
    root = _root(tmp_path)
    filesystem = DurableRootedFinalizationFilesystem(root)
    with ClipCatalog(tmp_path / "catalog.sqlite3") as catalog:
        finalizer = RecorderClipFinalizer(
            catalog=catalog,
            filesystem=filesystem,
            monotonic_ns=iter(range(100, 200)).__next__,
        )

        outcome = finalizer.finalize(
            provisional_video_name=SOURCE_NAME,
            sidecar=_unsynced_sidecar(),
            retention_order=10_000,
        )
        durable = catalog.get_clip(CLIP_ID)

    assert outcome.complete
    assert durable.lifecycle is ClipLifecycle.FINALIZED
    assert durable.video_path == "clips/boot-abcdef123456-000007.mp4"
    assert durable.sidecar_path == "clips/boot-abcdef123456-000007.json"
    assert (
        parse_sidecar_bytes(filesystem.read_bytes(durable.sidecar_path, maximum_bytes=1_048_576))
        == _unsynced_sidecar()
    )
    assert not any(path.name.endswith(".partial.mp4") for path in (root / "clips").iterdir())


def test_partial_target_is_refused_without_catalog_or_sidecar_mutation(tmp_path: Path) -> None:
    finalizer, catalog, filesystem, _events = _components(tmp_path)
    source_sidecar = replace(
        _sidecar(),
        video_file=SOURCE_NAME,
        metadata_file=SOURCE_NAME.removesuffix(".mp4") + ".json",
        start_utc=None,
        end_utc=None,
        start_local=None,
        timestamp_quality=TimestampQuality.MONOTONIC_ONLY,
        time_anchor=None,
        system_clock_state=SystemClockState.UNSET,
    )

    with pytest.raises(FinalizationRefused, match=r"\.partial basename"):
        finalizer.finalize(
            provisional_video_name=SOURCE_NAME,
            sidecar=source_sidecar,
            retention_order=1,
        )

    assert catalog.clips == {}
    assert not filesystem.exists(f"pending/{STAGED_SIDECAR}")


def test_finalization_refuses_filename_boot_token_mismatch_before_mutation(
    tmp_path: Path,
) -> None:
    finalizer, catalog, filesystem, _events = _components(tmp_path)
    sidecar = replace(
        _unsynced_sidecar(),
        boot_id=UUID("deadbeef-cafe-4789-a234-678943216789"),
    )

    with pytest.raises(FinalizationRefused, match="boot token"):
        finalizer.finalize(
            provisional_video_name=SOURCE_NAME,
            sidecar=sidecar,
            retention_order=7,
        )

    assert filesystem.exists(f"pending/{SOURCE_NAME}")
    assert catalog.clips == {}
    assert catalog.intents == {}


def test_case_insensitive_target_collision_refuses_before_intent(tmp_path: Path) -> None:
    finalizer, catalog, filesystem, _events = _components(tmp_path)
    (filesystem.root / "protected" / TARGET_VIDEO.upper()).write_bytes(b"existing evidence")

    with pytest.raises(FinalizationRefused, match="case-insensitive"):
        _finalize(finalizer)

    assert catalog.clips == {}
    assert filesystem.exists(f"pending/{SOURCE_NAME}")
    assert not filesystem.exists(f"pending/{STAGED_SIDECAR}")


def test_catalog_commit_failure_leaves_durable_pending_pair_and_retry_converges(
    tmp_path: Path,
) -> None:
    finalizer, catalog, filesystem, _events = _components(tmp_path)
    catalog.fail_registration_once = True

    with pytest.raises(InjectedCrash, match="catalog commit"):
        _finalize(finalizer)

    assert filesystem.exists(f"pending/{SOURCE_NAME}")
    assert filesystem.exists(f"pending/{STAGED_SIDECAR}")
    assert not filesystem.exists(f"clips/{TARGET_VIDEO}")
    assert _finalize(finalizer).complete


def test_crash_after_intent_before_moves_resumes_same_intent(tmp_path: Path) -> None:
    finalizer, catalog, filesystem, _events = _components(tmp_path)
    catalog.fail_before_actions_once = True

    with pytest.raises(InjectedCrash, match="after intent"):
        _finalize(finalizer)
    intent_id = catalog.list_pending_intents(limit=1)[0].intent_id

    outcome = _finalize(finalizer)

    assert outcome.complete
    assert outcome.resumed
    assert outcome.intent_id == intent_id
    assert filesystem.exists(f"clips/{TARGET_VIDEO}")


def test_crash_after_video_move_resumes_only_sidecar_move(tmp_path: Path) -> None:
    finalizer, _catalog, filesystem, _events = _components(tmp_path)
    filesystem.fail_after_move = 1

    with pytest.raises(InjectedCrash, match="after rename"):
        _finalize(finalizer)
    assert filesystem.exists(f"clips/{TARGET_VIDEO}")
    assert filesystem.exists(f"pending/{STAGED_SIDECAR}")

    outcome = _finalize(finalizer)

    assert outcome.complete
    assert outcome.actions_attempted == 1
    assert filesystem.exists(f"clips/{TARGET_SIDECAR}")


def test_crash_after_both_moves_completes_catalog_without_another_move(tmp_path: Path) -> None:
    finalizer, catalog, filesystem, _events = _components(tmp_path)
    catalog.fail_before_completion_once = True

    with pytest.raises(InjectedCrash, match="after both renames"):
        _finalize(finalizer)
    moves_before_retry = filesystem.moves

    outcome = _finalize(finalizer)

    assert outcome.complete
    assert outcome.actions_attempted == 0
    assert filesystem.moves == moves_before_retry
    assert catalog.get_clip(CLIP_ID).pair_reconciled


def test_source_target_ambiguity_is_latched_without_overwrite(tmp_path: Path) -> None:
    finalizer, catalog, filesystem, _events = _components(tmp_path)
    catalog.fail_before_actions_once = True
    with pytest.raises(InjectedCrash):
        _finalize(finalizer)
    target = filesystem.root / "clips" / TARGET_VIDEO
    target.write_bytes(b"foreign collision")

    with pytest.raises(FinalizationRefused, match="SOURCE_TARGET_CONFLICT"):
        _finalize(finalizer)

    assert (filesystem.root / "pending" / SOURCE_NAME).read_bytes() == b"closed fragmented MP4"
    assert target.read_bytes() == b"foreign collision"
    assert catalog.list_pending_intents(limit=1)


def test_recovery_refuses_corrupt_sidecar_before_any_move(tmp_path: Path) -> None:
    finalizer, catalog, filesystem, _events = _components(tmp_path)
    catalog.fail_before_actions_once = True
    with pytest.raises(InjectedCrash):
        _finalize(finalizer)
    (filesystem.root / "pending" / STAGED_SIDECAR).write_bytes(b'{"not":"canonical"}')

    with pytest.raises(FinalizationRefused, match="canonical bounded JSON"):
        finalizer.reconcile_pending()

    assert filesystem.exists(f"pending/{SOURCE_NAME}")
    assert not filesystem.exists(f"clips/{TARGET_VIDEO}")


def test_mp4_only_diagnostics_are_preserved_and_never_imported(tmp_path: Path) -> None:
    finalizer, catalog, filesystem, _events = _components(tmp_path)
    diagnostic = filesystem.root / "pending" / "boot-abcdef123456-000006.partial.mp4"
    diagnostic.write_bytes(b"retained live-test diagnostic")

    assert _finalize(finalizer).complete

    assert diagnostic.read_bytes() == b"retained live-test diagnostic"
    assert set(catalog.clips) == {CLIP_ID}


def test_completed_replay_is_an_identity_checked_no_op(tmp_path: Path) -> None:
    finalizer, _catalog, filesystem, _events = _components(tmp_path)
    assert _finalize(finalizer).complete
    moves = filesystem.moves

    replay = _finalize(finalizer)

    assert replay.complete
    assert replay.resumed
    assert replay.intent_id is None
    assert replay.actions_attempted == 0
    assert filesystem.moves == moves


def test_collision_scan_bound_refuses_without_registration(tmp_path: Path) -> None:
    root = _root(tmp_path)
    (root / "pending" / "unknown.txt").write_text("preserve", encoding="utf-8")
    events: list[str] = []
    filesystem = InstrumentedFilesystem(root, events)
    catalog = FakePromotionCatalog(events)
    finalizer = RecorderClipFinalizer(
        catalog=catalog,
        filesystem=filesystem,
        monotonic_ns=lambda: 1,
        limits=FinalizerLimits(max_directory_entries=1),
    )

    with pytest.raises(FinalizationRefused, match="scan bound"):
        _finalize(finalizer)

    assert catalog.clips == {}


def _protection_clip(number: int, *, protected: bool = False) -> CatalogClip:
    directory = "protected" if protected else "clips"
    return CatalogClip(
        clip_id=UUID(int=number),
        lifecycle=ClipLifecycle.FINALIZED,
        video_path=f"{directory}/protection-{number}.mp4",
        sidecar_path=f"{directory}/protection-{number}.json",
        start_monotonic_ns=number * 100,
        end_monotonic_ns=number * 100 + 50,
        retention_order=number,
        size_bytes=100,
        protected=protected,
        protection_reason="fixture" if protected else None,
        pair_reconciled=True,
        managed=True,
    )


def _write_protection_pair(root: Path, clip: CatalogClip) -> None:
    (root / clip.video_path).write_bytes(b"video")
    (root / clip.sidecar_path).write_bytes(b"sidecar")


def _real_finalizer(
    root: Path,
    catalog: ClipCatalog,
    filesystem: InstrumentedFilesystem,
    *,
    limits: FinalizerLimits | None = None,
) -> RecorderClipFinalizer:
    ticks = iter(range(1_000, 10_000))
    return RecorderClipFinalizer(
        catalog=catalog,
        filesystem=filesystem,
        monotonic_ns=lambda: next(ticks),
        limits=limits,
    )


def test_protect_intent_replays_before_moves_and_across_one_member_crash(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    events: list[str] = []
    filesystem = InstrumentedFilesystem(root, events)
    clip = _protection_clip(1)
    _write_protection_pair(root, clip)
    with ClipCatalog(tmp_path / "catalog.sqlite3") as catalog:
        catalog.register_clip(clip, catalog_now_ns=1)
        intent_id = catalog.prepare_protect(
            clip.clip_id,
            reason="event",
            monotonic_now_ns=2,
        )
        assert intent_id is not None
        assert filesystem.moves == 0
        filesystem.fail_after_move = 1
        finalizer = _real_finalizer(root, catalog, filesystem)

        with pytest.raises(InjectedCrash, match="after rename"):
            finalizer.reconcile_pending()
        assert (
            catalog.list_pending_intents_by_kind(kinds=(IntentKind.PROTECT,), limit=1)[0].intent_id
            == intent_id
        )
        assert filesystem.moves == 1

        report = finalizer.reconcile_pending()
        recovered = catalog.get_clip(clip.clip_id)
        assert report.completed == 1 and not report.more_work
        assert recovered.protected and recovered.pair_reconciled
        assert recovered.video_path.startswith("protected/")


def test_protect_replay_completes_catalog_when_both_members_already_moved(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    filesystem = InstrumentedFilesystem(root, [])
    clip = _protection_clip(1)
    _write_protection_pair(root, clip)
    with ClipCatalog(tmp_path / "catalog.sqlite3") as catalog:
        catalog.register_clip(clip, catalog_now_ns=1)
        intent_id = catalog.prepare_protect(
            clip.clip_id,
            reason="event",
            monotonic_now_ns=2,
        )
        assert intent_id is not None
        intent = catalog.list_pending_intents_by_kind(kinds=(IntentKind.PROTECT,), limit=1)[0]
        assert intent.paths.video_target is not None
        assert intent.paths.sidecar_target is not None
        filesystem.move(intent.paths.video_source, intent.paths.video_target)
        filesystem.move(intent.paths.sidecar_source, intent.paths.sidecar_target)
        moves = filesystem.moves

        report = _real_finalizer(root, catalog, filesystem).reconcile_pending()

        assert report.actions_attempted == 0 and report.completed == 1
        assert filesystem.moves == moves
        assert catalog.get_clip(clip.clip_id).pair_reconciled


@pytest.mark.parametrize("initially_protected", [False, True])
def test_protection_recovery_moves_opaque_sidecar_without_semantic_validation(
    tmp_path: Path,
    *,
    initially_protected: bool,
) -> None:
    root = _root(tmp_path)
    filesystem = InstrumentedFilesystem(root, [])
    clip = _protection_clip(1, protected=initially_protected)
    opaque_sidecar = b"\x00opaque catalog-owned sidecar bytes\xff"
    (root / clip.video_path).write_bytes(b"video")
    (root / clip.sidecar_path).write_bytes(opaque_sidecar)
    with ClipCatalog(tmp_path / "catalog.sqlite3") as catalog:
        catalog.register_clip(clip, catalog_now_ns=1)
        if initially_protected:
            intent_id = catalog.prepare_unprotect(clip.clip_id, monotonic_now_ns=2)
        else:
            intent_id = catalog.prepare_protect(
                clip.clip_id,
                reason="event",
                monotonic_now_ns=2,
            )
        assert intent_id is not None

        report = _real_finalizer(root, catalog, filesystem).reconcile_pending()

        recovered = catalog.get_clip(clip.clip_id)
        assert report.completed == 1 and not report.more_work
        assert recovered.protected is (not initially_protected)
        assert (root / recovered.sidecar_path).read_bytes() == opaque_sidecar


def test_protect_recovery_refuses_target_collision_without_mutation(tmp_path: Path) -> None:
    root = _root(tmp_path)
    filesystem = InstrumentedFilesystem(root, [])
    clip = _protection_clip(1)
    _write_protection_pair(root, clip)
    collision = root / "protected" / "protection-1.mp4"
    collision.write_bytes(b"foreign collision")
    with ClipCatalog(tmp_path / "catalog.sqlite3") as catalog:
        catalog.register_clip(clip, catalog_now_ns=1)
        intent_id = catalog.prepare_protect(
            clip.clip_id,
            reason="event",
            monotonic_now_ns=2,
        )
        assert intent_id is not None

        with pytest.raises(FinalizationRefused, match="SOURCE_TARGET_CONFLICT"):
            _real_finalizer(root, catalog, filesystem).reconcile_pending()

        assert filesystem.moves == 0
        assert (root / clip.video_path).read_bytes() == b"video"
        assert collision.read_bytes() == b"foreign collision"
        assert not catalog.get_clip(clip.clip_id).pair_reconciled
        assert catalog.list_pending_intents_by_kind(
            kinds=(IntentKind.PROTECT,),
            limit=1,
        )


def test_overlapping_event_unprotect_compensation_requires_follow_up_pass(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    filesystem = InstrumentedFilesystem(root, [])
    previous = _protection_clip(1, protected=True)
    current = _protection_clip(2)
    _write_protection_pair(root, previous)
    _write_protection_pair(root, current)
    with ClipCatalog(tmp_path / "catalog.sqlite3") as catalog:
        catalog.register_clip(previous, catalog_now_ns=1)
        catalog.register_clip(current, catalog_now_ns=2)
        assert catalog.prepare_unprotect(previous.clip_id, monotonic_now_ns=10) is not None
        catalog.trigger_event(
            current.clip_id,
            source=EventSource.API,
            monotonic_now_ns=20,
            previous_count=1,
            next_count=0,
        )
        finalizer = _real_finalizer(root, catalog, filesystem)

        first = finalizer.reconcile_pending()
        intermediate = catalog.get_clip(previous.clip_id)
        assert first.more_work
        assert intermediate.protected and not intermediate.pair_reconciled
        assert intermediate.video_path.startswith("clips/")
        remaining = catalog.list_pending_intents_by_kind(kinds=(IntentKind.PROTECT,), limit=2)
        assert tuple(intent.clip_id for intent in remaining) == (previous.clip_id,)

        second = finalizer.reconcile_pending()
        recovered = catalog.get_clip(previous.clip_id)
        assert second.completed == 1 and not second.more_work
        assert recovered.protected and recovered.pair_reconciled
        assert recovered.video_path.startswith("protected/")


def test_filtered_recovery_is_not_masked_by_delete_and_reports_truncation(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    filesystem = InstrumentedFilesystem(root, [])
    delete_clip = _protection_clip(1)
    first = _protection_clip(2)
    second = _protection_clip(3)
    for clip in (delete_clip, first, second):
        _write_protection_pair(root, clip)
    with ClipCatalog(tmp_path / "catalog.sqlite3") as catalog:
        for clip in (delete_clip, first, second):
            catalog.register_clip(clip, catalog_now_ns=clip.retention_order)
        catalog.prepare_delete(delete_clip.clip_id, monotonic_now_ns=10, boot_id="boot-a")
        catalog.prepare_protect(first.clip_id, reason="event", monotonic_now_ns=11)
        catalog.prepare_protect(second.clip_id, reason="event", monotonic_now_ns=12)
        finalizer = _real_finalizer(
            root,
            catalog,
            filesystem,
            limits=FinalizerLimits(max_pending_intents=1),
        )

        first_pass = finalizer.reconcile_pending()
        assert first_pass.intents_examined == 1 and first_pass.more_work
        assert catalog.get_clip(first.clip_id).pair_reconciled
        assert catalog.get_clip(second.clip_id).video_path.startswith("clips/")
        assert catalog.list_pending_delete_intents(limit=1)

        second_pass = finalizer.reconcile_pending()
        assert second_pass.intents_examined == 1 and not second_pass.more_work
        assert catalog.get_clip(second.clip_id).pair_reconciled
        assert catalog.list_pending_delete_intents(limit=1)
