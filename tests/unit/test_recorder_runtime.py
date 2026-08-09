from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from dashcam.audio.alsa import AlsaCaptureDevice, AlsaIdentity
from dashcam.audio.linux import AudioDiscoveryOutcome, AudioDiscoveryStatus
from dashcam.catalog.database import RetentionThresholdLatch
from dashcam.config import (
    AudioConfig,
    DashcamConfig,
    GpsConfig,
    StorageConfig,
    VideoConfig,
    default_config,
)
from dashcam.gps.clock import AnchorSource, UtcAnchor
from dashcam.gps.nmea import NmeaSentence, SentenceType
from dashcam.gps.service import GpsCounters, GpsService, GpsSnapshot
from dashcam.gps.telemetry import (
    GpsTelemetrySample,
    GpsTelemetryWindow,
    TelemetryWindowIssue,
)
from dashcam.metadata.coordinator import MetadataReconciliationRefused
from dashcam.metadata.reconcile import parse_sidecar_bytes
from dashcam.metadata.schema import ClipSidecar, TimeAnchor, TimeAnchorSource
from dashcam.recorder.finalizer import (
    FinalizationRecoveryReport,
    MetadataReconciliationCandidate,
)
from dashcam.recorder.gstreamer import (
    AudioCapturePlan,
    AudioCounters,
    AudioStartupError,
    EffectiveAudioCaps,
    EffectiveCaps,
    FinalizedFragment,
    FragmentMediaContract,
    FrameCounters,
    GStreamerBackend,
    OpenedFragment,
    RecordingStorageNoSpaceError,
    SegmentedOutputConfig,
)
from dashcam.recorder.pipeline import (
    CameraOwnership,
    PipelineContractError,
    PipelineFault,
    ProfileValidationError,
    RecoverablePipelineError,
    VideoProfile,
)
from dashcam.recorder.runtime import (
    AudioBackendFactory,
    AudioDiscoverer,
    GStreamerRecorderRuntime,
    PipelineRecoveryExhausted,
    RecorderFinalizationFault,
    RecorderStorageFault,
    RuntimeAudioState,
    RuntimeFinalizer,
    RuntimeLifecycleEvent,
    RuntimeLifecycleEventKind,
    RuntimeLimits,
    RuntimeObserverFault,
    StorageSafetyStop,
    build_production_runtime,
    next_pending_sequence,
    read_short_boot_id,
)
from dashcam.state import GpsState, GpsTimeState, StorageState, SystemClockState
from dashcam.storage.preflight import (
    MountFacts,
    PreflightReason,
    PreflightResult,
    RecordingRootFacts,
    SpaceFacts,
)
from dashcam.storage.retention import StorageThresholds
from dashcam.storage.space import FilesystemSpaceObservation, StorageSpaceMonitor

AsyncTest = Callable[[], Coroutine[Any, Any, None]]


def run_async(test: AsyncTest) -> None:
    asyncio.run(test())


def ready_storage() -> PreflightResult:
    return PreflightResult(StorageState.READY, (), None, True, True)


def ready_storage_with_device() -> PreflightResult:
    return PreflightResult(
        StorageState.READY,
        (),
        RecordingRootFacts(
            MountFacts(
                "/srv/dashcam",
                True,
                "/dev/mmcblk0p3",
                "exfat",
                "DASHCAM",
                "7EED-3EA7",
                ("rw",),
                "179:3",
                "179:2",
            ),
            SpaceFacts(24_000_000_000, 20_000_000_000),
            None,
        ),
        True,
        True,
    )


@dataclass
class FakePreflight:
    result: PreflightResult = field(default_factory=ready_storage)
    calls: list[tuple[DashcamConfig, str]] = field(default_factory=list)

    def __call__(
        self,
        config: DashcamConfig,
        *,
        identity_path: str,
    ) -> PreflightResult:
        self.calls.append((config, identity_path))
        return self.result


@dataclass
class FakeBackend:
    effective: VideoProfile = field(default_factory=VideoProfile)
    start_error: BaseException | None = None
    run_error: BaseException | None = None
    stop_error: BaseException | None = None
    open_gate: asyncio.Event | None = None
    run_error_gate: asyncio.Event | None = None
    opened_sequence: int = 0
    started_with: list[VideoProfile] = field(default_factory=list)
    stop_calls: int = 0
    run_started: asyncio.Event = field(default_factory=asyncio.Event)
    finalized: asyncio.Queue[FinalizedFragment] = field(default_factory=asyncio.Queue)
    configured_overlay_texts: list[str | None] = field(default_factory=list)
    overlay_texts: list[str | None] = field(default_factory=list)
    overlay_error: BaseException | None = None

    def configure_overlay_text(self, text: str | None) -> None:
        self.configured_overlay_texts.append(text)

    async def start(self, requested_profile: VideoProfile) -> VideoProfile:
        self.started_with.append(requested_profile)
        if self.start_error is not None:
            raise self.start_error
        return self.effective

    async def set_overlay_text(self, text: str | None) -> None:
        if self.overlay_error is not None:
            raise self.overlay_error
        self.overlay_texts.append(text)

    async def run(self, stop_requested: asyncio.Event) -> None:
        self.run_started.set()
        if self.run_error is not None:
            if self.run_error_gate is not None:
                await self.run_error_gate.wait()
            raise self.run_error
        await stop_requested.wait()

    async def stop(self) -> None:
        self.stop_calls += 1
        if self.stop_error is not None:
            raise self.stop_error

    async def wait_for_first_fragment_opened(self) -> OpenedFragment:
        if self.open_gate is not None:
            await self.open_gate.wait()
        return OpenedFragment(
            Path(f"/srv/dashcam/pending/boot-abcdef123456-{self.opened_sequence:06d}.partial.mp4"),
            self.opened_sequence,
            0,
        )

    async def next_finalized_fragment(self) -> FinalizedFragment:
        return await self.finalized.get()

    def mark_finalized_fragment_processed(self) -> None:
        self.finalized.task_done()

    async def wait_for_finalized_fragments_processed(self) -> None:
        await self.finalized.join()


@dataclass
class RecordingFactory:
    backend: FakeBackend
    outputs: list[SegmentedOutputConfig] = field(default_factory=list)

    def __call__(self, output: SegmentedOutputConfig) -> FakeBackend:
        self.outputs.append(output)
        return self.backend


@dataclass
class AudioFakeBackend(FakeBackend):
    effective_audio_caps: EffectiveAudioCaps = field(
        default_factory=lambda: EffectiveAudioCaps(
            "S16LE",
            48_000,
            1,
            "aac",
            4,
            "raw",
            "voaacenc",
            "aacparse",
            128_000,
        )
    )
    audio_units: int = 0
    audio_capture_plan: AudioCapturePlan | None = None
    audio_restoration_snapshot: dict[str, object] | None = None

    def audio_counters(self) -> AudioCounters:
        return AudioCounters(self.audio_units)


@dataclass
class RecordingAudioFactory:
    backend: FakeBackend
    calls: list[tuple[SegmentedOutputConfig, AudioCapturePlan]] = field(default_factory=list)

    def __call__(
        self,
        output: SegmentedOutputConfig,
        plan: AudioCapturePlan,
    ) -> FakeBackend:
        self.calls.append((output, plan))
        self.backend.opened_sequence = output.start_index
        return self.backend


def matched_audio_outcome() -> AudioDiscoveryOutcome:
    return AudioDiscoveryOutcome(
        AudioDiscoveryStatus.MATCHED,
        AlsaCaptureDevice(
            AlsaIdentity(
                "08bb",
                "2902",
                physical_path="platform-3f980000.usb-usb-0:1:1.0",
                product="USB_PnP_Sound_Device",
            ),
            1,
            0,
        ),
    )


@dataclass
class ScriptedFactory:
    backends: list[FakeBackend]
    outputs: list[SegmentedOutputConfig] = field(default_factory=list)

    def __call__(self, output: SegmentedOutputConfig) -> FakeBackend:
        backend = self.backends[len(self.outputs)]
        backend.opened_sequence = output.start_index
        self.outputs.append(output)
        return backend


@dataclass
class ScriptedPreflight:
    results: list[PreflightResult]
    calls: list[tuple[DashcamConfig, str]] = field(default_factory=list)

    def __call__(
        self,
        config: DashcamConfig,
        *,
        identity_path: str,
    ) -> PreflightResult:
        self.calls.append((config, identity_path))
        return self.results[len(self.calls) - 1]


@dataclass
class ImmediateBackoff:
    stop_on_call: int | None = None
    delays: list[float] = field(default_factory=list)

    async def __call__(self, delay_s: float, stop_requested: asyncio.Event) -> bool:
        self.delays.append(delay_s)
        if self.stop_on_call == len(self.delays):
            stop_requested.set()
            return True
        return False


@dataclass
class CounterBackend(FakeBackend):
    counters: FrameCounters = field(default_factory=lambda: FrameCounters(0, 0, None, None))

    def frame_counters(self) -> FrameCounters:
        return self.counters


@dataclass
class FakeFinalizer:
    error: BaseException | None = None
    metadata_error: BaseException | None = None
    reconciled: int = 0
    calls: list[tuple[str, object, int]] = field(default_factory=list)
    completed: asyncio.Event = field(default_factory=asyncio.Event)
    metadata_reconciliations: list[tuple[UUID, TimeAnchor, UUID]] = field(default_factory=list)
    metadata_attempts: int = 0
    metadata_candidate_scans: int = 0
    retention_latch: RetentionThresholdLatch | None = None

    def reconcile_pending(self) -> FinalizationRecoveryReport:
        self.reconciled += 1
        return FinalizationRecoveryReport(0, 0, 0, False)

    def video_size(self, provisional_video_name: str) -> int:
        assert provisional_video_name.endswith(".partial.mp4")
        return 1_000_000

    def next_retention_order(self) -> int:
        return 7

    def retention_threshold_latch(self) -> RetentionThresholdLatch | None:
        return self.retention_latch

    def store_retention_threshold_latch(self, latch: RetentionThresholdLatch) -> None:
        self.retention_latch = latch

    def metadata_reconciliation_candidates(
        self,
        expected_boot_id: UUID,
        *,
        limit: int,
        after_order: int = -1,
        after_clip_id: UUID | None = None,
    ) -> tuple[MetadataReconciliationCandidate, ...]:
        reconciled = {item[0] for item in self.metadata_reconciliations}
        cursor_id = UUID(int=0) if after_clip_id is None else after_clip_id
        candidates = sorted(
            (
                MetadataReconciliationCandidate(sidecar.clip_id, retention_order)
                for _name, value, retention_order in self.calls
                if isinstance(value, ClipSidecar)
                and (sidecar := value).boot_id == expected_boot_id
                and sidecar.clip_id not in reconciled
            ),
            key=lambda candidate: (candidate.retention_order, str(candidate.clip_id)),
        )
        return tuple(
            candidate
            for candidate in candidates
            if (
                candidate.retention_order > after_order
                or (
                    candidate.retention_order == after_order
                    and str(candidate.clip_id) > str(cursor_id)
                )
            )
        )[:limit]

    def finalize(
        self,
        *,
        provisional_video_name: str,
        sidecar: ClipSidecar,
        retention_order: int,
    ) -> object:
        if self.error is not None:
            raise self.error
        self.calls.append((provisional_video_name, sidecar, retention_order))
        self.completed.set()
        return object()

    def reconcile_metadata(
        self,
        clip_id: UUID,
        *,
        anchor: TimeAnchor,
        expected_boot_id: UUID,
        gps_time_state: GpsTimeState,
        system_clock_state: SystemClockState,
    ) -> object:
        assert gps_time_state is GpsTimeState.GPS_TIME_VALID
        assert system_clock_state is SystemClockState.UNSET
        self.metadata_attempts += 1
        if self.metadata_error is not None:
            raise self.metadata_error
        self.metadata_reconciliations.append((clip_id, anchor, expected_boot_id))
        return object()


@dataclass
class SpaceMonitorFactory:
    observations: list[object]
    calls: int = 0

    def __call__(
        self,
        *,
        storage: StorageConfig,
        volume_uuid: str,
        expected_device_id: str,
        expected_capacity_bytes: int,
        latch_store: RuntimeFinalizer,
    ) -> StorageSpaceMonitor:
        del storage
        self.calls += 1

        def observe() -> FilesystemSpaceObservation:
            value = self.observations.pop(0)
            if isinstance(value, BaseException):
                raise value
            assert isinstance(value, FilesystemSpaceObservation)
            return value

        return StorageSpaceMonitor(
            volume_uuid=volume_uuid,
            expected_device_id=expected_device_id,
            expected_capacity_bytes=expected_capacity_bytes,
            thresholds=StorageThresholds(15, 20, 2 * 1024**3, 256 * 1024**2),
            observer=observe,
            latch_store=latch_store,
        )


@dataclass
class FakeGpsService:
    current_snapshot: GpsSnapshot = field(
        default_factory=lambda: GpsSnapshot(
            state=GpsState.RECEIVING_INVALID,
            counters=GpsCounters(
                connection_attempts=1,
                connections=1,
                bytes_received=123,
                lines_received=4,
                valid_sentences=3,
            ),
            connected=True,
        )
    )
    run_started: asyncio.Event = field(default_factory=asyncio.Event)
    stopped: bool = False
    run_error: BaseException | None = None
    telemetry_samples: tuple[GpsTelemetrySample, ...] = ()
    telemetry_issues: tuple[TelemetryWindowIssue, ...] = ()
    telemetry_error: BaseException | None = None
    telemetry_requests: list[tuple[int, int, int]] = field(default_factory=list)

    @property
    def snapshot(self) -> GpsSnapshot:
        return self.current_snapshot

    def telemetry_window(
        self,
        start_monotonic_ns: int,
        end_monotonic_ns: int,
        *,
        max_samples: int,
    ) -> GpsTelemetryWindow:
        self.telemetry_requests.append((start_monotonic_ns, end_monotonic_ns, max_samples))
        if self.telemetry_error is not None:
            raise self.telemetry_error
        return GpsTelemetryWindow(
            start_monotonic_ns,
            end_monotonic_ns,
            tuple(
                sample
                for sample in self.telemetry_samples
                if start_monotonic_ns <= sample.monotonic_ns < end_monotonic_ns
            )[:max_samples],
            self.telemetry_issues,
        )

    async def run(self, stop_requested: asyncio.Event) -> None:
        self.run_started.set()
        if self.run_error is not None:
            raise self.run_error
        await stop_requested.wait()
        self.stopped = True


@dataclass
class RecordingGpsFactory:
    service: FakeGpsService
    configs: list[GpsConfig] = field(default_factory=list)
    error: BaseException | None = None

    def __call__(self, config: GpsConfig) -> FakeGpsService:
        self.configs.append(config)
        if self.error is not None:
            raise self.error
        return self.service


def runtime_for(
    backend: FakeBackend,
    *,
    preflight: FakePreflight | None = None,
    ownership: CameraOwnership | None = None,
    limits: RuntimeLimits | None = None,
    gps_factory: RecordingGpsFactory | None = None,
    monotonic_ns: Callable[[], int] | None = None,
) -> tuple[GStreamerRecorderRuntime, RecordingFactory]:
    factory = RecordingFactory(backend)
    runtime = GStreamerRecorderRuntime(
        config_path=Path("/etc/dashcam/config.toml"),
        identity_path=Path("/etc/dashcam/storage-volume.env"),
        backend_factory=factory,
        preflight=preflight or FakePreflight(),
        boot_id_reader=lambda: "abcdef123456",
        sequence_planner=lambda root, pending, boot: 0,
        ownership=ownership or CameraOwnership(),
        limits=limits,
        gps_service_factory=gps_factory,
        monotonic_ns=monotonic_ns or time.monotonic_ns,
    )
    return runtime, factory


def scripted_runtime(
    backends: list[FakeBackend],
    preflight: ScriptedPreflight,
    waiter: ImmediateBackoff,
    *,
    sequence_values: list[int] | None = None,
    finalizer: FakeFinalizer | None = None,
) -> tuple[GStreamerRecorderRuntime, ScriptedFactory]:
    factory = ScriptedFactory(backends)
    sequences = iter(sequence_values or list(range(len(backends))))
    runtime = GStreamerRecorderRuntime(
        config_path=Path("/etc/dashcam/config.toml"),
        identity_path=Path("/etc/dashcam/storage-volume.env"),
        backend_factory=factory,
        preflight=preflight,
        boot_id_reader=lambda: "abcdef123456",
        boot_uuid_reader=lambda: UUID("12345678-1234-5678-9234-567812345678"),
        sequence_planner=lambda root, pending, boot: next(sequences),
        finalizer_factory=(None if finalizer is None else lambda root, device: finalizer),
        monotonic_ns=lambda: 1_000,
        ownership=CameraOwnership(),
        backoff_waiter=waiter,
    )
    return runtime, factory


def test_matching_ready_storage_is_bound_before_camera_and_first_fragment() -> None:
    async def scenario() -> None:
        ownership = CameraOwnership()
        backend = FakeBackend()
        preflight = FakePreflight()
        runtime, factory = runtime_for(
            backend,
            preflight=preflight,
            ownership=ownership,
        )
        config = default_config()

        assert await runtime.check(config) == ready_storage()
        await runtime.start(config)

        assert preflight.calls == [(config, "/etc/dashcam/storage-volume.env")]
        assert backend.started_with == [VideoProfile()]
        assert ownership.owner == "dashcamd"
        assert factory.outputs == [
            SegmentedOutputConfig(
                Path("/srv/dashcam/pending"),
                "abcdef123456",
                start_index=0,
            )
        ]
        await runtime.stop()
        assert ownership.owner is None

    run_async(scenario)


def test_storage_monitor_retries_fresh_sample_before_reconcile_and_camera() -> None:
    async def scenario() -> None:
        backend = FakeBackend()
        backend_factory = RecordingFactory(backend)
        finalizer = FakeFinalizer()
        space_factory = SpaceMonitorFactory(
            [
                OSError("temporary stat failure"),
                OSError("temporary stat failure"),
                FilesystemSpaceObservation("179:3", 24_000_000_000, 20_000_000_000),
            ]
        )
        runtime = GStreamerRecorderRuntime(
            config_path=Path("/etc/dashcam/config.toml"),
            identity_path=Path("/etc/dashcam/storage-volume.env"),
            backend_factory=backend_factory,
            preflight=FakePreflight(ready_storage_with_device()),
            boot_id_reader=lambda: "abcdef123456",
            boot_uuid_reader=lambda: UUID("12345678-1234-5678-9234-567812345678"),
            sequence_planner=lambda root, pending, boot: 0,
            finalizer_factory=lambda root, device: finalizer,
            ownership=CameraOwnership(),
            storage_space_monitor_factory=space_factory,
        )
        config = default_config()

        await runtime.check(config)
        await runtime.start(config)

        assert finalizer.reconciled == 1
        assert backend.started_with == [VideoProfile()]
        storage = runtime.runtime_snapshot()["storage_retention"]
        assert isinstance(storage, dict)
        assert storage["mode"] == "NORMAL"
        assert storage["volume_uuid_suffix"] == "3EA7"
        assert "7EED-3EA7" not in repr(storage)
        await runtime.stop()

    run_async(scenario)


def test_startup_emergency_stops_before_reconcile_or_camera_and_stop_is_read_only() -> None:
    async def scenario() -> None:
        backend = FakeBackend()
        finalizer = FakeFinalizer()
        runtime = GStreamerRecorderRuntime(
            config_path=Path("/etc/dashcam/config.toml"),
            identity_path=Path("/etc/dashcam/storage-volume.env"),
            backend_factory=RecordingFactory(backend),
            preflight=FakePreflight(ready_storage_with_device()),
            boot_id_reader=lambda: "abcdef123456",
            boot_uuid_reader=lambda: UUID("12345678-1234-5678-9234-567812345678"),
            sequence_planner=lambda root, pending, boot: 0,
            finalizer_factory=lambda root, device: finalizer,
            ownership=CameraOwnership(),
            storage_space_monitor_factory=SpaceMonitorFactory(
                [FilesystemSpaceObservation("179:3", 24_000_000_000, 100_000_000)]
            ),
        )
        config = default_config()

        await runtime.check(config)
        with pytest.raises(StorageSafetyStop, match="EMERGENCY"):
            await runtime.start(config)
        await runtime.stop()

        assert finalizer.reconciled == 0
        assert finalizer.metadata_candidate_scans == 0
        assert backend.started_with == []

    run_async(scenario)


def test_periodic_emergency_is_supervised_as_storage_safety_stop() -> None:
    async def scenario() -> None:
        backend = FakeBackend()
        finalizer = FakeFinalizer()
        runtime = GStreamerRecorderRuntime(
            config_path=Path("/etc/dashcam/config.toml"),
            identity_path=Path("/etc/dashcam/storage-volume.env"),
            backend_factory=RecordingFactory(backend),
            preflight=FakePreflight(ready_storage_with_device()),
            boot_id_reader=lambda: "abcdef123456",
            boot_uuid_reader=lambda: UUID("12345678-1234-5678-9234-567812345678"),
            sequence_planner=lambda root, pending, boot: 0,
            finalizer_factory=lambda root, device: finalizer,
            ownership=CameraOwnership(),
            storage_space_monitor_factory=SpaceMonitorFactory(
                [
                    FilesystemSpaceObservation("179:3", 24_000_000_000, 20_000_000_000),
                    FilesystemSpaceObservation("179:3", 24_000_000_000, 100_000_000),
                ]
            ),
            limits=RuntimeLimits(storage_observation_interval_s=0.001),
        )
        config = default_config()
        await runtime.check(config)
        await runtime.start(config)

        with pytest.raises(StorageSafetyStop, match="EMERGENCY"):
            await asyncio.wait_for(runtime.run(asyncio.Event()), timeout=0.2)
        await runtime.stop()

        storage = runtime.runtime_snapshot()["storage_retention"]
        assert isinstance(storage, dict)
        assert storage["stop_required"] is True
        assert storage["reclaimer_enabled"] is False

    run_async(scenario)


def test_typed_gstreamer_no_space_is_persisted_and_stops_without_recovery() -> None:
    async def scenario() -> None:
        no_space = asyncio.Event()
        backend = FakeBackend(
            run_error=RecordingStorageNoSpaceError("recording sink exhausted"),
            run_error_gate=no_space,
        )
        finalizer = FakeFinalizer()
        runtime = GStreamerRecorderRuntime(
            config_path=Path("/etc/dashcam/config.toml"),
            identity_path=Path("/etc/dashcam/storage-volume.env"),
            backend_factory=RecordingFactory(backend),
            preflight=FakePreflight(ready_storage_with_device()),
            boot_id_reader=lambda: "abcdef123456",
            boot_uuid_reader=lambda: UUID("12345678-1234-5678-9234-567812345678"),
            sequence_planner=lambda root, pending, boot: 0,
            finalizer_factory=lambda root, device: finalizer,
            ownership=CameraOwnership(),
            storage_space_monitor_factory=SpaceMonitorFactory(
                [FilesystemSpaceObservation("179:3", 24_000_000_000, 20_000_000_000)]
            ),
        )
        config = default_config()
        await runtime.check(config)
        await runtime.start(config)
        no_space.set()

        with pytest.raises(StorageSafetyStop, match="NO_SPACE_WRITE"):
            await runtime.run(asyncio.Event())
        await runtime.stop()

        assert finalizer.retention_latch == RetentionThresholdLatch(
            "7EED-3EA7", 24_000_000_000, True
        )
        assert runtime.runtime_snapshot()["pipeline_restart_count"] == 0

    run_async(scenario)


def test_typed_no_space_before_first_fragment_uses_storage_safety_stop() -> None:
    async def scenario() -> None:
        backend = FakeBackend(run_error=RecordingStorageNoSpaceError("recording sink exhausted"))
        finalizer = FakeFinalizer()
        runtime = GStreamerRecorderRuntime(
            config_path=Path("/etc/dashcam/config.toml"),
            identity_path=Path("/etc/dashcam/storage-volume.env"),
            backend_factory=RecordingFactory(backend),
            preflight=FakePreflight(ready_storage_with_device()),
            boot_id_reader=lambda: "abcdef123456",
            boot_uuid_reader=lambda: UUID("12345678-1234-5678-9234-567812345678"),
            sequence_planner=lambda root, pending, boot: 0,
            finalizer_factory=lambda root, device: finalizer,
            ownership=CameraOwnership(),
            storage_space_monitor_factory=SpaceMonitorFactory(
                [FilesystemSpaceObservation("179:3", 24_000_000_000, 20_000_000_000)]
            ),
        )
        config = default_config()
        await runtime.check(config)

        with pytest.raises(StorageSafetyStop, match="NO_SPACE_WRITE"):
            await runtime.start(config)
        await runtime.stop()

        assert finalizer.retention_latch == RetentionThresholdLatch(
            "7EED-3EA7", 24_000_000_000, True
        )
        assert runtime.runtime_snapshot()["pipeline_restart_count"] == 0
        assert backend.stop_calls == 1

    run_async(scenario)


def test_optional_gps_is_supervised_with_media_and_published_without_coordinates() -> None:
    async def scenario() -> None:
        gps = FakeGpsService()
        gps_factory = RecordingGpsFactory(gps)
        runtime, _ = runtime_for(FakeBackend(), gps_factory=gps_factory)
        config = default_config()

        await runtime.check(config)
        await runtime.start(config)
        await asyncio.wait_for(gps.run_started.wait(), timeout=1)

        observed = runtime.runtime_snapshot()["gps"]
        assert isinstance(observed, dict)
        assert gps_factory.configs == [config.gps]
        assert observed["state"] == GpsState.RECEIVING_INVALID.value
        assert observed["connected"] is True
        counters = observed["counters"]
        assert isinstance(counters, dict)
        assert counters["bytes_received"] == 123
        assert counters["valid_sentences"] == 3
        assert counters["anchor_attempts"] == 0
        time_status = observed["time"]
        assert isinstance(time_status, dict)
        assert time_status["state"] == "UNSYNCED"
        assert time_status["anchor"] is None
        assert "latitude" not in repr(observed).casefold()
        assert "longitude" not in repr(observed).casefold()

        await runtime.stop()
        assert gps.stopped

    run_async(scenario)


def test_optional_gps_construction_or_task_failure_never_blocks_recording() -> None:
    async def scenario() -> None:
        construction = RecordingGpsFactory(
            FakeGpsService(),
            error=OSError("UART unavailable"),
        )
        runtime, _ = runtime_for(FakeBackend(), gps_factory=construction)
        config = default_config()
        await runtime.check(config)
        await runtime.start(config)
        snapshot = runtime.runtime_snapshot()
        gps_snapshot = snapshot["gps"]
        assert isinstance(gps_snapshot, dict)
        assert gps_snapshot["state"] == GpsState.FAULTED.value
        assert "UART unavailable" in str(gps_snapshot["supervisor_error"])
        assert runtime.recording_progress_token() is None
        await runtime.stop()

        failed_service = FakeGpsService(run_error=RuntimeError("GPS task fault"))
        task_failure = RecordingGpsFactory(failed_service)
        second, _ = runtime_for(FakeBackend(), gps_factory=task_failure)
        await second.check(config)
        await second.start(config)
        await asyncio.wait_for(failed_service.run_started.wait(), timeout=1)
        await asyncio.sleep(0)
        second_snapshot = second.runtime_snapshot()["gps"]
        assert isinstance(second_snapshot, dict)
        assert second_snapshot["state"] == GpsState.FAULTED.value
        assert "GPS task fault" in str(second_snapshot["supervisor_error"])
        await second.stop()

    run_async(scenario)


def test_unexpected_gps_cancellation_marks_time_stale_and_hides_navigation() -> None:
    async def scenario() -> None:
        navigation = NmeaSentence(
            sentence_type=SentenceType.GGA,
            talker="GN",
            received_monotonic_ns=1_000,
            latitude_deg=32.1,
            longitude_deg=34.8,
            speed_knots=10.0,
            navigation_valid=True,
        )
        gps = FakeGpsService(
            current_snapshot=GpsSnapshot(
                state=GpsState.NAVIGATION_VALID,
                navigation=navigation,
                latest_sentence=navigation,
                connected=True,
                gps_time_state=GpsTimeState.GPS_TIME_VALID,
                time_anchor=UtcAnchor(
                    monotonic_ns=1_000,
                    utc=datetime(2026, 7, 28, tzinfo=UTC),
                    uncertainty_ns=250_000_000,
                    source=AnchorSource.GPS_RMC_VALID,
                    provenance="NMEA:GNRMC:active-valid:complete-utc",
                ),
            )
        )
        runtime, _ = runtime_for(
            FakeBackend(),
            gps_factory=RecordingGpsFactory(gps),
        )
        config = default_config()
        await runtime.check(config)
        await runtime.start(config)
        await asyncio.wait_for(gps.run_started.wait(), timeout=1)

        assert runtime._gps_task is not None
        runtime._gps_task.cancel()
        await asyncio.gather(runtime._gps_task, return_exceptions=True)
        await asyncio.sleep(0)

        snapshot = runtime.runtime_snapshot()
        gps_status = cast(dict[str, object], snapshot["gps"])
        assert gps_status["state"] == GpsState.FAULTED.value
        assert gps_status["connected"] is False
        assert gps_status["navigation"] is None
        assert "cancelled unexpectedly" in str(gps_status["supervisor_error"])
        time_status = cast(dict[str, object], gps_status["time"])
        assert time_status["state"] == GpsTimeState.GPS_TIME_STALE.value
        assert time_status["anchor"] is not None
        assert snapshot["pipeline_restart_count"] == 0

        await runtime.stop()

    run_async(scenario)


def test_overlay_uses_one_gps_anchor_model_and_hides_stale_navigation() -> None:
    async def scenario() -> None:
        navigation = NmeaSentence(
            sentence_type=SentenceType.GGA,
            talker="GN",
            received_monotonic_ns=1_000_000_000,
            latitude_deg=32.12345,
            longitude_deg=34.98765,
            speed_knots=10.0,
            altitude_m=12.0,
            fix_quality=1,
            satellites=8,
            hdop=0.8,
            navigation_valid=True,
        )
        anchor = UtcAnchor(
            monotonic_ns=1_000_000_000,
            utc=datetime(2026, 7, 28, 16, 0, tzinfo=UTC),
            uncertainty_ns=250_000_000,
            source=AnchorSource.GPS_RMC_VALID,
            provenance="NMEA:GNRMC:active-valid:complete-utc",
        )
        gps = FakeGpsService(
            current_snapshot=GpsSnapshot(
                state=GpsState.NAVIGATION_VALID,
                navigation=navigation,
                latest_sentence=navigation,
                connected=True,
                gps_time_state=GpsTimeState.GPS_TIME_VALID,
                time_anchor=anchor,
            )
        )
        backend = FakeBackend()
        runtime, _ = runtime_for(
            backend,
            gps_factory=RecordingGpsFactory(gps),
            monotonic_ns=lambda: 2_000_000_000,
            limits=RuntimeLimits(overlay_update_interval_s=0.001),
        )
        config = default_config()
        await runtime.check(config)
        await runtime.start(config)

        async def wait_for_updates(count: int) -> None:
            while len(backend.overlay_texts) < count:
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_updates(1), timeout=1)
        assert backend.overlay_texts[-1] == (
            "2026-07-28 19:00:01  +03:00  REC\n32.12345, 34.98765   19 km/h   ALT 12 m   SAT 8"
        )

        gps.current_snapshot = replace(
            gps.current_snapshot,
            state=GpsState.STALE,
            gps_time_state=GpsTimeState.GPS_TIME_STALE,
        )
        await asyncio.wait_for(wait_for_updates(2), timeout=1)
        assert backend.overlay_texts[-1] == ("2026-07-28 19:00:01  +03:00  REC\nGPS LOST")
        assert "32.12345" not in str(backend.overlay_texts[-1])
        assert runtime.runtime_snapshot()["overlay"] == {
            "enabled": True,
            "state": "ACTIVE",
            "updates": 2,
            "last_error": None,
            "renderer": None,
        }
        await runtime.stop()

    run_async(scenario)


def test_overlay_unsynced_disabled_and_failure_paths_do_not_restart_video() -> None:
    async def scenario() -> None:
        unsynced_backend = FakeBackend()
        unsynced, _ = runtime_for(
            unsynced_backend,
            limits=RuntimeLimits(overlay_update_interval_s=0.001),
        )
        config = default_config()
        await unsynced.check(config)
        await unsynced.start(config)
        while not unsynced_backend.overlay_texts:
            await asyncio.sleep(0)
        assert unsynced_backend.overlay_texts == ["TIME UNSYNCED  REC\nGPS INVALID"]
        await unsynced.stop()

        disabled_backend = FakeBackend()
        disabled, _ = runtime_for(disabled_backend)
        disabled_config = replace(
            config,
            overlay=replace(config.overlay, enabled=False),
        )
        await disabled.check(disabled_config)
        await disabled.start(disabled_config)
        await asyncio.sleep(0)
        assert disabled_backend.overlay_texts == []
        assert disabled.runtime_snapshot()["overlay"] == {
            "enabled": False,
            "state": "DISABLED",
            "updates": 0,
            "last_error": None,
            "renderer": None,
        }
        await disabled.stop()

        failed_backend = FakeBackend(overlay_error=OSError("overlay setter failed"))
        failed, _ = runtime_for(
            failed_backend,
            limits=RuntimeLimits(overlay_update_interval_s=0.001),
        )
        await failed.check(config)
        await failed.start(config)
        while cast(dict[str, object], failed.runtime_snapshot()["overlay"])["state"] != "FAULTED":
            await asyncio.sleep(0)
        overlay_status = cast(dict[str, object], failed.runtime_snapshot()["overlay"])
        assert "overlay setter failed" in str(overlay_status["last_error"])
        assert failed.runtime_snapshot()["pipeline_restart_count"] == 0
        assert failed._backend_run_task is not None
        assert not failed._backend_run_task.done()
        await failed.stop()

    run_async(scenario)


def test_start_refuses_absent_mismatched_or_nonready_storage_evidence() -> None:
    async def scenario() -> None:
        config = default_config()
        backend = FakeBackend()
        runtime, factory = runtime_for(backend)
        with pytest.raises(RecorderStorageFault, match="READY"):
            await runtime.start(config)
        assert factory.outputs == []

        refused = PreflightResult(
            StorageState.FAULTED,
            (PreflightReason.UNMOUNTED,),
            None,
            False,
            False,
        )
        second, second_factory = runtime_for(
            FakeBackend(),
            preflight=FakePreflight(refused),
        )
        await second.check(config)
        with pytest.raises(RecorderStorageFault, match="READY"):
            await second.start(config)
        assert second_factory.outputs == []

        third, _ = runtime_for(FakeBackend())
        await third.check(config)
        changed = replace(config, device_name="Other")
        with pytest.raises(RecorderStorageFault, match="matching"):
            await third.start(changed)

    run_async(scenario)


def test_first_fragment_readiness_is_raced_against_early_backend_failure() -> None:
    async def scenario() -> None:
        open_gate = asyncio.Event()
        backend = FakeBackend(
            run_error=RecoverablePipelineError("early bus failure"),
            open_gate=open_gate,
        )
        runtime, _ = runtime_for(backend)
        await runtime.check(default_config())

        with pytest.raises(RecoverablePipelineError, match="early bus"):
            await runtime.start(default_config())

        assert backend.stop_calls == 1

    run_async(scenario)


def test_first_fragment_timeout_is_bounded_and_cleans_up() -> None:
    async def scenario() -> None:
        backend = FakeBackend(open_gate=asyncio.Event())
        runtime, _ = runtime_for(
            backend,
            limits=RuntimeLimits(first_fragment_timeout_s=0.01),
        )
        await runtime.check(default_config())

        with pytest.raises(RecoverablePipelineError, match="startup deadline"):
            await runtime.start(default_config())

        assert backend.stop_calls == 1

    run_async(scenario)


def test_finalized_events_are_continuously_drained_into_bounded_counters() -> None:
    async def scenario() -> None:
        backend = FakeBackend()
        runtime, _ = runtime_for(backend)
        await runtime.check(default_config())
        await runtime.start(default_config())
        fragment = FinalizedFragment(
            Path("/srv/dashcam/pending/boot-abcdef123456-000000.partial.mp4"),
            0,
            60_000_000_000,
        )
        await backend.finalized.put(fragment)
        while runtime.finalized_count == 0:
            await asyncio.sleep(0)

        assert runtime.finalized_count == 1
        assert runtime.last_finalized_fragment == fragment
        await runtime.stop()

    run_async(scenario)


def test_finalized_fragment_is_promoted_with_canonical_unsynced_metadata() -> None:
    async def scenario() -> None:
        backend = FakeBackend()
        finalizer = FakeFinalizer()
        runtime = GStreamerRecorderRuntime(
            config_path=Path("/etc/dashcam/config.toml"),
            identity_path=Path("/etc/dashcam/storage-volume.env"),
            backend_factory=RecordingFactory(backend),
            preflight=FakePreflight(ready_storage_with_device()),
            boot_id_reader=lambda: "abcdef123456",
            boot_uuid_reader=lambda: UUID("12345678-1234-5678-9234-567812345678"),
            sequence_planner=lambda root, pending, boot: 0,
            finalizer_factory=lambda root, device: finalizer,
            monotonic_ns=lambda: 1_000,
        )
        config = default_config()
        assert (await runtime.check(config)).ready
        await runtime.start(config)

        await backend.finalized.put(
            FinalizedFragment(
                Path("/srv/dashcam/pending/boot-abcdef123456-000000.partial.mp4"),
                0,
                1_000_000_000,
            )
        )
        await asyncio.wait_for(finalizer.completed.wait(), timeout=1)
        await runtime.stop()

        name, sidecar_value, retention_order = finalizer.calls[0]
        assert isinstance(sidecar_value, ClipSidecar)
        assert name.endswith(".partial.mp4")
        assert sidecar_value.video_file == "boot-abcdef123456-000000.mp4"
        assert sidecar_value.metadata_file == "boot-abcdef123456-000000.json"
        assert sidecar_value.start_monotonic_ns == 1_000
        assert sidecar_value.end_monotonic_ns == 1_000_001_000
        assert sidecar_value.video.fps_nominal == 30.0
        assert isinstance(sidecar_value.video.fps_nominal, float)
        assert sidecar_value.video.measured_bitrate_bps == 8_000_000
        assert parse_sidecar_bytes(sidecar_value.to_canonical_json()) == sidecar_value
        assert retention_order == 7
        assert finalizer.reconciled == 1

    run_async(scenario)


def test_finalized_sidecar_captures_one_bounded_half_open_gps_window() -> None:
    async def scenario() -> None:
        backend = FakeBackend()
        finalizer = FakeFinalizer()
        gps = FakeGpsService(
            telemetry_samples=(
                GpsTelemetrySample(
                    500_001_000,
                    32.1,
                    34.8,
                    speed_mps=5.0,
                    course_deg=90.0,
                    altitude_m=25.0,
                    fix_quality=2,
                    satellites=9,
                    hdop=0.8,
                ),
                GpsTelemetrySample(1_000_001_000, 32.2, 34.9),
            )
        )
        runtime = GStreamerRecorderRuntime(
            config_path=Path("/etc/dashcam/config.toml"),
            identity_path=Path("/etc/dashcam/storage-volume.env"),
            backend_factory=RecordingFactory(backend),
            preflight=FakePreflight(ready_storage_with_device()),
            boot_id_reader=lambda: "abcdef123456",
            boot_uuid_reader=lambda: UUID("12345678-1234-5678-9234-567812345678"),
            sequence_planner=lambda root, pending, boot: 0,
            finalizer_factory=lambda root, device: finalizer,
            monotonic_ns=lambda: 1_000,
            gps_service_factory=RecordingGpsFactory(gps),
            limits=RuntimeLimits(metadata_reconciliation_interval_s=0.001),
        )
        config = default_config()
        assert (await runtime.check(config)).ready
        await runtime.start(config)

        await backend.finalized.put(
            FinalizedFragment(
                Path("/srv/dashcam/pending/boot-abcdef123456-000000.partial.mp4"),
                0,
                1_000_000_000,
            )
        )
        await asyncio.wait_for(finalizer.completed.wait(), timeout=1)
        await runtime.stop()

        sidecar = cast(ClipSidecar, finalizer.calls[0][1])
        assert gps.telemetry_requests == [(1_000, 1_000_001_000, 600)]
        assert sidecar.gps.available
        assert sidecar.gps.first_fix_utc is None
        assert len(sidecar.gps.samples) == 1
        sample = sidecar.gps.samples[0]
        assert sample.monotonic_ns == 500_001_000
        assert sample.utc is None
        assert sample.timestamp_quality.value == "MONOTONIC_ONLY"
        assert sample.lat_deg == pytest.approx(32.1)
        assert sample.lon_deg == pytest.approx(34.8)
        assert sidecar.timestamp_quality.value == "MONOTONIC_ONLY"
        assert parse_sidecar_bytes(sidecar.to_canonical_json()) == sidecar

    run_async(scenario)


def test_trusted_gps_anchor_triggers_stable_uuid_metadata_reconciliation() -> None:
    async def scenario() -> None:
        backend = FakeBackend()
        finalizer = FakeFinalizer()
        gps = FakeGpsService(
            current_snapshot=GpsSnapshot(
                state=GpsState.NAVIGATION_VALID,
                connected=True,
                gps_time_state=GpsTimeState.GPS_TIME_VALID,
                time_anchor=UtcAnchor(
                    monotonic_ns=500_000_000,
                    utc=datetime(2026, 7, 28, 16, 0, tzinfo=UTC),
                    source=AnchorSource.GPS_RMC_VALID,
                    uncertainty_ns=250_000_000,
                    provenance="NMEA:GNRMC:active-valid:complete-utc",
                ),
            )
        )
        boot_uuid = UUID("12345678-1234-5678-9234-567812345678")
        runtime = GStreamerRecorderRuntime(
            config_path=Path("/etc/dashcam/config.toml"),
            identity_path=Path("/etc/dashcam/storage-volume.env"),
            backend_factory=RecordingFactory(backend),
            preflight=FakePreflight(ready_storage_with_device()),
            boot_id_reader=lambda: "abcdef123456",
            boot_uuid_reader=lambda: boot_uuid,
            sequence_planner=lambda root, pending, boot: 0,
            finalizer_factory=lambda root, device: finalizer,
            monotonic_ns=lambda: 1_000,
            gps_service_factory=RecordingGpsFactory(gps),
        )
        config = default_config()
        await runtime.check(config)
        await runtime.start(config)
        await backend.finalized.put(
            FinalizedFragment(
                Path("/srv/dashcam/pending/boot-abcdef123456-000000.partial.mp4"),
                0,
                1_000_000_000,
            )
        )
        while (
            cast(
                int,
                cast(
                    dict[str, object],
                    runtime.runtime_snapshot()["metadata_reconciliation"],
                )["completed"],
            )
            == 0
        ):
            await asyncio.sleep(0)

        clip_id, anchor, observed_boot = finalizer.metadata_reconciliations[0]
        sidecar = cast(ClipSidecar, finalizer.calls[0][1])
        assert clip_id == sidecar.clip_id
        assert observed_boot == boot_uuid
        assert anchor.source is TimeAnchorSource.GPS
        assert anchor.monotonic_ns == 500_000_000
        assert anchor.utc == datetime(2026, 7, 28, 16, 0, tzinfo=UTC)
        assert runtime.runtime_snapshot()["metadata_reconciliation"] == {
            "completed": 1,
            "failures": 0,
            "last_error": None,
            "backlog": 0,
            "overflows": 0,
            "retrying": 0,
            "parked": 0,
        }
        await runtime.stop()

    run_async(scenario)


def test_late_gps_lock_drains_bounded_same_boot_provisional_backlog() -> None:
    async def scenario() -> None:
        backend = FakeBackend()
        finalizer = FakeFinalizer()
        gps = FakeGpsService()
        boot_uuid = UUID("12345678-1234-5678-9234-567812345678")
        runtime = GStreamerRecorderRuntime(
            config_path=Path("/etc/dashcam/config.toml"),
            identity_path=Path("/etc/dashcam/storage-volume.env"),
            backend_factory=RecordingFactory(backend),
            preflight=FakePreflight(ready_storage_with_device()),
            boot_id_reader=lambda: "abcdef123456",
            boot_uuid_reader=lambda: boot_uuid,
            sequence_planner=lambda root, pending, boot: 0,
            finalizer_factory=lambda root, device: finalizer,
            monotonic_ns=lambda: 1_000,
            gps_service_factory=RecordingGpsFactory(gps),
        )
        config = default_config()
        await runtime.check(config)
        await runtime.start(config)
        await backend.finalized.put(
            FinalizedFragment(
                Path("/srv/dashcam/pending/boot-abcdef123456-000000.partial.mp4"),
                0,
                1_000_000_000,
            )
        )
        while runtime.finalized_count < 1:
            await asyncio.sleep(0)
        first_sidecar = cast(ClipSidecar, finalizer.calls[0][1])
        assert finalizer.metadata_reconciliations == []
        assert runtime.runtime_snapshot()["metadata_reconciliation"] == {
            "completed": 0,
            "failures": 0,
            "last_error": None,
            "backlog": 1,
            "overflows": 0,
            "retrying": 0,
            "parked": 0,
        }

        gps.current_snapshot = GpsSnapshot(
            state=GpsState.NAVIGATION_VALID,
            connected=True,
            gps_time_state=GpsTimeState.GPS_TIME_VALID,
            time_anchor=UtcAnchor(
                monotonic_ns=500_000_000,
                utc=datetime(2026, 7, 28, 16, 0, tzinfo=UTC),
                source=AnchorSource.GPS_RMC_VALID,
                uncertainty_ns=250_000_000,
                provenance="NMEA:GNRMC:active-valid:complete-utc",
            ),
        )
        while (
            cast(
                int,
                cast(
                    dict[str, object],
                    runtime.runtime_snapshot()["metadata_reconciliation"],
                )["completed"],
            )
            < 1
        ):
            await asyncio.sleep(0)
        assert [item[0] for item in finalizer.metadata_reconciliations] == [first_sidecar.clip_id]

        await backend.finalized.put(
            FinalizedFragment(
                Path("/srv/dashcam/pending/boot-abcdef123456-000001.partial.mp4"),
                1,
                2_000_000_000,
            )
        )
        while (
            cast(
                int,
                cast(
                    dict[str, object],
                    runtime.runtime_snapshot()["metadata_reconciliation"],
                )["completed"],
            )
            < 2
        ):
            await asyncio.sleep(0)

        second_sidecar = cast(ClipSidecar, finalizer.calls[1][1])
        assert [item[0] for item in finalizer.metadata_reconciliations] == [
            first_sidecar.clip_id,
            second_sidecar.clip_id,
        ]
        assert all(item[2] == boot_uuid for item in finalizer.metadata_reconciliations)
        assert runtime.runtime_snapshot()["metadata_reconciliation"] == {
            "completed": 2,
            "failures": 0,
            "last_error": None,
            "backlog": 0,
            "overflows": 0,
            "retrying": 0,
            "parked": 0,
        }
        await runtime.stop()

    run_async(scenario)


def test_shutdown_flushes_a_late_anchor_without_waiting_for_another_fragment() -> None:
    async def scenario() -> None:
        backend = FakeBackend()
        finalizer = FakeFinalizer()
        gps = FakeGpsService()
        boot_uuid = UUID("12345678-1234-5678-9234-567812345678")
        runtime = GStreamerRecorderRuntime(
            config_path=Path("/etc/dashcam/config.toml"),
            identity_path=Path("/etc/dashcam/storage-volume.env"),
            backend_factory=RecordingFactory(backend),
            preflight=FakePreflight(ready_storage_with_device()),
            boot_id_reader=lambda: "abcdef123456",
            boot_uuid_reader=lambda: boot_uuid,
            sequence_planner=lambda root, pending, boot: 0,
            finalizer_factory=lambda root, device: finalizer,
            monotonic_ns=lambda: 1_000,
            gps_service_factory=RecordingGpsFactory(gps),
            limits=RuntimeLimits(metadata_reconciliation_interval_s=120),
        )
        config = default_config()
        await runtime.check(config)
        await runtime.start(config)
        await backend.finalized.put(
            FinalizedFragment(
                Path("/srv/dashcam/pending/boot-abcdef123456-000000.partial.mp4"),
                0,
                1_000_000_000,
            )
        )
        while runtime.finalized_count < 1:
            await asyncio.sleep(0)
        sidecar = cast(ClipSidecar, finalizer.calls[0][1])
        assert finalizer.metadata_reconciliations == []

        gps.current_snapshot = GpsSnapshot(
            state=GpsState.NAVIGATION_VALID,
            connected=True,
            gps_time_state=GpsTimeState.GPS_TIME_VALID,
            time_anchor=UtcAnchor(
                monotonic_ns=500_000_000,
                utc=datetime(2026, 7, 28, 16, 0, tzinfo=UTC),
                source=AnchorSource.GPS_RMC_VALID,
                uncertainty_ns=250_000_000,
                provenance="NMEA:GNRMC:active-valid:complete-utc",
            ),
        )
        await runtime.stop()

        assert [item[0] for item in finalizer.metadata_reconciliations] == [sidecar.clip_id]
        assert runtime.runtime_snapshot()["metadata_reconciliation"] == {
            "completed": 1,
            "failures": 0,
            "last_error": None,
            "backlog": 0,
            "overflows": 0,
            "retrying": 0,
            "parked": 0,
        }

    run_async(scenario)


def test_metadata_reconciliation_failure_is_reported_but_video_remains_running() -> None:
    async def scenario() -> None:
        backend = FakeBackend()
        finalizer = FakeFinalizer(metadata_error=RuntimeError("optional metadata collision"))
        gps = FakeGpsService(
            current_snapshot=GpsSnapshot(
                state=GpsState.NAVIGATION_VALID,
                connected=True,
                gps_time_state=GpsTimeState.GPS_TIME_VALID,
                time_anchor=UtcAnchor(
                    monotonic_ns=500_000_000,
                    utc=datetime(2026, 7, 28, 16, 0, tzinfo=UTC),
                    source=AnchorSource.GPS_RMC_VALID,
                    uncertainty_ns=250_000_000,
                    provenance="NMEA:GNRMC:active-valid:complete-utc",
                ),
            )
        )
        runtime = GStreamerRecorderRuntime(
            config_path=Path("/etc/dashcam/config.toml"),
            identity_path=Path("/etc/dashcam/storage-volume.env"),
            backend_factory=RecordingFactory(backend),
            preflight=FakePreflight(ready_storage_with_device()),
            boot_id_reader=lambda: "abcdef123456",
            boot_uuid_reader=lambda: UUID("12345678-1234-5678-9234-567812345678"),
            sequence_planner=lambda root, pending, boot: 0,
            finalizer_factory=lambda root, device: finalizer,
            monotonic_ns=lambda: 1_000,
            gps_service_factory=RecordingGpsFactory(gps),
        )
        config = default_config()
        await runtime.check(config)
        await runtime.start(config)
        await backend.finalized.put(
            FinalizedFragment(
                Path("/srv/dashcam/pending/boot-abcdef123456-000000.partial.mp4"),
                0,
                1_000_000_000,
            )
        )
        while (
            cast(
                dict[str, object],
                runtime.runtime_snapshot()["metadata_reconciliation"],
            )["failures"]
            == 0
        ):
            await asyncio.sleep(0)

        snapshot = runtime.runtime_snapshot()["metadata_reconciliation"]
        assert snapshot == {
            "completed": 0,
            "failures": 1,
            "last_error": "RuntimeError: optional metadata collision",
            "backlog": 1,
            "overflows": 0,
            "retrying": 1,
            "parked": 0,
        }
        assert runtime.runtime_snapshot()["pipeline_restart_count"] == 0
        await runtime.stop()

    run_async(scenario)


def test_terminal_metadata_refusal_is_parked_without_repeated_media_work() -> None:
    async def scenario() -> None:
        backend = FakeBackend()
        finalizer = FakeFinalizer(metadata_error=MetadataReconciliationRefused("target collision"))
        gps = FakeGpsService(
            current_snapshot=GpsSnapshot(
                state=GpsState.NAVIGATION_VALID,
                connected=True,
                gps_time_state=GpsTimeState.GPS_TIME_VALID,
                time_anchor=UtcAnchor(
                    monotonic_ns=500_000_000,
                    utc=datetime(2026, 7, 28, 16, 0, tzinfo=UTC),
                    source=AnchorSource.GPS_RMC_VALID,
                    uncertainty_ns=250_000_000,
                    provenance="NMEA:GNRMC:active-valid:complete-utc",
                ),
            )
        )
        runtime = GStreamerRecorderRuntime(
            config_path=Path("/etc/dashcam/config.toml"),
            identity_path=Path("/etc/dashcam/storage-volume.env"),
            backend_factory=RecordingFactory(backend),
            preflight=FakePreflight(ready_storage_with_device()),
            boot_id_reader=lambda: "abcdef123456",
            boot_uuid_reader=lambda: UUID("12345678-1234-5678-9234-567812345678"),
            sequence_planner=lambda root, pending, boot: 0,
            finalizer_factory=lambda root, device: finalizer,
            monotonic_ns=lambda: 1_000,
            gps_service_factory=RecordingGpsFactory(gps),
            limits=RuntimeLimits(metadata_reconciliation_interval_s=0.01),
        )
        await runtime.check(default_config())
        await runtime.start(default_config())
        await backend.finalized.put(
            FinalizedFragment(
                Path("/srv/dashcam/pending/boot-abcdef123456-000000.partial.mp4"),
                0,
                1_000_000_000,
            )
        )
        while (
            cast(
                dict[str, object],
                runtime.runtime_snapshot()["metadata_reconciliation"],
            )["parked"]
            == 0
        ):
            await asyncio.sleep(0)
        await asyncio.sleep(0.04)

        assert finalizer.metadata_attempts == 1
        assert runtime.runtime_snapshot()["pipeline_restart_count"] == 0
        await runtime.stop()

    run_async(scenario)


def test_nonretryable_metadata_refusal_is_parked_without_media_restart() -> None:
    async def scenario() -> None:
        backend = FakeBackend()
        finalizer = FakeFinalizer(
            metadata_error=MetadataReconciliationRefused("optional collision")
        )
        gps = FakeGpsService(
            current_snapshot=GpsSnapshot(
                state=GpsState.NAVIGATION_VALID,
                connected=True,
                gps_time_state=GpsTimeState.GPS_TIME_VALID,
                time_anchor=UtcAnchor(
                    monotonic_ns=500_000_000,
                    utc=datetime(2026, 7, 28, 16, 0, tzinfo=UTC),
                    source=AnchorSource.GPS_RMC_VALID,
                    uncertainty_ns=250_000_000,
                    provenance="NMEA:GNRMC:active-valid:complete-utc",
                ),
            )
        )
        runtime = GStreamerRecorderRuntime(
            config_path=Path("/etc/dashcam/config.toml"),
            identity_path=Path("/etc/dashcam/storage-volume.env"),
            backend_factory=RecordingFactory(backend),
            preflight=FakePreflight(ready_storage_with_device()),
            boot_id_reader=lambda: "abcdef123456",
            boot_uuid_reader=lambda: UUID("12345678-1234-5678-9234-567812345678"),
            sequence_planner=lambda root, pending, boot: 0,
            finalizer_factory=lambda root, device: finalizer,
            monotonic_ns=lambda: 1_000,
            gps_service_factory=RecordingGpsFactory(gps),
            limits=RuntimeLimits(metadata_reconciliation_interval_s=0.001),
        )
        config = default_config()
        await runtime.check(config)
        await runtime.start(config)
        await backend.finalized.put(
            FinalizedFragment(
                Path("/srv/dashcam/pending/boot-abcdef123456-000000.partial.mp4"),
                0,
                1_000_000_000,
            )
        )
        while (
            cast(
                dict[str, object],
                runtime.runtime_snapshot()["metadata_reconciliation"],
            )["parked"]
            == 0
        ):
            await asyncio.sleep(0)

        assert runtime.runtime_snapshot()["metadata_reconciliation"] == {
            "completed": 0,
            "failures": 1,
            "last_error": "MetadataReconciliationRefused: optional collision",
            "backlog": 0,
            "overflows": 0,
            "retrying": 0,
            "parked": 1,
        }
        assert runtime.runtime_snapshot()["pipeline_restart_count"] == 0
        await runtime.stop()

    run_async(scenario)


def test_gps_telemetry_snapshot_faults_and_truncation_remain_noncritical() -> None:
    async def scenario() -> None:
        failed_gps = FakeGpsService(telemetry_error=OSError("optional GPS history fault"))
        failed_runtime, _ = runtime_for(
            FakeBackend(),
            gps_factory=RecordingGpsFactory(failed_gps),
        )
        config = default_config()
        await failed_runtime.check(config)
        await failed_runtime.start(config)
        summary, warnings = failed_runtime._gps_summary_for_interval(0, 1_000)
        assert not summary.available
        assert "optional GPS history fault" in warnings[0]
        await failed_runtime.stop()

        incomplete_gps = FakeGpsService(
            telemetry_samples=(GpsTelemetrySample(500, 32.1, 34.8),),
            telemetry_issues=(TelemetryWindowIssue.HISTORY_EVICTED,),
        )
        incomplete_runtime, _ = runtime_for(
            FakeBackend(),
            gps_factory=RecordingGpsFactory(incomplete_gps),
        )
        await incomplete_runtime.check(config)
        await incomplete_runtime.start(config)
        summary, warnings = incomplete_runtime._gps_summary_for_interval(0, 1_000)
        assert summary.available
        assert len(summary.samples) == 1
        assert warnings == ("GPS telemetry window incomplete: HISTORY_EVICTED",)
        await incomplete_runtime.stop()

    run_async(scenario)


def test_finalizer_failure_is_supervised_as_a_recorder_failure() -> None:
    async def scenario() -> None:
        backend = FakeBackend()
        finalizer = FakeFinalizer(error=RuntimeError("durable promotion failed"))
        runtime = GStreamerRecorderRuntime(
            config_path=Path("/etc/dashcam/config.toml"),
            identity_path=Path("/etc/dashcam/storage-volume.env"),
            backend_factory=RecordingFactory(backend),
            preflight=FakePreflight(ready_storage_with_device()),
            boot_id_reader=lambda: "abcdef123456",
            boot_uuid_reader=lambda: UUID("12345678-1234-5678-9234-567812345678"),
            sequence_planner=lambda root, pending, boot: 0,
            finalizer_factory=lambda root, device: finalizer,
            monotonic_ns=lambda: 1_000,
        )
        config = default_config()
        await runtime.check(config)
        await runtime.start(config)
        await backend.finalized.put(
            FinalizedFragment(
                Path("/srv/dashcam/pending/boot-abcdef123456-000000.partial.mp4"),
                0,
                1_000_000_000,
            )
        )

        with pytest.raises(RuntimeError, match="durable promotion failed"):
            await runtime.run(asyncio.Event())
        await runtime.stop()

    run_async(scenario)


def test_run_and_idempotent_stop_share_one_backend_session() -> None:
    async def scenario() -> None:
        backend = FakeBackend()
        runtime, _ = runtime_for(backend)
        await runtime.check(default_config())
        await runtime.start(default_config())
        stop_requested = asyncio.Event()
        run_task = asyncio.create_task(runtime.run(stop_requested))
        await backend.run_started.wait()
        stop_requested.set()
        await run_task
        await runtime.stop()
        await runtime.stop()
        assert backend.stop_calls == 1

    run_async(scenario)


@pytest.mark.parametrize(
    ("field_name", "change"),
    [
        ("width", lambda video: replace(video, width=1280)),
        ("height", lambda video: replace(video, height=720)),
        ("fps", lambda video: replace(video, fps=25)),
        ("codec", lambda video: replace(video, codec="h265")),
        (
            "hardware_encoder_required",
            lambda video: replace(video, hardware_encoder_required=False),
        ),
        ("bitrate_bps", lambda video: replace(video, bitrate_bps=4_000_000)),
        (
            "keyframe_interval_frames",
            lambda video: replace(video, keyframe_interval_frames=60),
        ),
        ("clip_duration_s", lambda video: replace(video, clip_duration_s=30)),
        ("container", lambda video: replace(video, container="mkv")),
    ],
)
def test_every_fixed_video_setting_fails_closed(
    field_name: str,
    change: Callable[[VideoConfig], VideoConfig],
) -> None:
    async def scenario() -> None:
        runtime, factory = runtime_for(FakeBackend())
        config = default_config()
        changed_video = change(config.video)
        changed_config = replace(config, video=changed_video)
        await runtime.check(changed_config)

        with pytest.raises(ProfileValidationError, match=field_name):
            await runtime.start(changed_config)

        assert factory.outputs == []

    run_async(scenario)


def test_short_boot_id_reader_requires_exact_canonical_uuid(tmp_path: Path) -> None:
    valid = tmp_path / "boot-id"
    valid.write_bytes(b"601693e3-fa96-427e-906b-1621463a15cd\n")
    assert read_short_boot_id(valid) == "601693e3fa96"

    valid.write_bytes(b"not-a-boot-id\n")
    with pytest.raises(PipelineContractError, match="canonical"):
        read_short_boot_id(valid)


def test_pending_sequence_scan_is_bounded_and_pair_collision_safe(tmp_path: Path) -> None:
    recording_root = tmp_path / "DASHCAM"
    pending = recording_root / "pending"
    clips = recording_root / "clips"
    protected = recording_root / "protected"
    for directory in (pending, clips, protected):
        directory.mkdir(parents=True, exist_ok=True)
    (pending / "boot-abcdef123456-000000.partial.mp4").write_bytes(b"")
    (pending / "boot-abcdef123456-000001.partial.json").write_bytes(b"")
    (clips / "20260726T120000.000Z_ABCDEF123456_s000005.MP4").write_bytes(b"")
    (protected / "BOOT-ABCDEF123456-000007.JSON").write_bytes(b"")

    assert next_pending_sequence(recording_root, pending, "abcdef123456") == 8

    (pending / "unknown.txt").write_bytes(b"")
    with pytest.raises(RecorderStorageFault, match="sequence scan bound"):
        next_pending_sequence(
            recording_root,
            pending,
            "abcdef123456",
            max_entries=4,
        )


def test_finalizer_deadline_never_abandons_a_mutating_to_thread_worker() -> None:
    @dataclass
    class BlockingFinalizer(FakeFinalizer):
        entered: threading.Event = field(default_factory=threading.Event)
        release: threading.Event = field(default_factory=threading.Event)

        def finalize(
            self,
            *,
            provisional_video_name: str,
            sidecar: ClipSidecar,
            retention_order: int,
        ) -> object:
            self.entered.set()
            assert self.release.wait(timeout=2)
            return super().finalize(
                provisional_video_name=provisional_video_name,
                sidecar=sidecar,
                retention_order=retention_order,
            )

    async def scenario() -> None:
        backend = FakeBackend()
        finalizer = BlockingFinalizer()
        runtime = GStreamerRecorderRuntime(
            config_path=Path("/etc/dashcam/config.toml"),
            identity_path=Path("/etc/dashcam/storage-volume.env"),
            backend_factory=RecordingFactory(backend),
            preflight=FakePreflight(ready_storage_with_device()),
            boot_id_reader=lambda: "abcdef123456",
            boot_uuid_reader=lambda: UUID("12345678-1234-5678-9234-567812345678"),
            sequence_planner=lambda root, pending, boot: 0,
            finalizer_factory=lambda root, device: finalizer,
            monotonic_ns=lambda: 1_000,
            limits=RuntimeLimits(finalizer_timeout_s=0.1),
        )
        config = default_config()
        await runtime.check(config)
        await runtime.start(config)
        await backend.finalized.put(
            FinalizedFragment(
                Path("/srv/dashcam/pending/boot-abcdef123456-000000.partial.mp4"),
                0,
                1_000_000_000,
            )
        )
        assert await asyncio.to_thread(finalizer.entered.wait, 1)

        run_task = asyncio.create_task(runtime.run(asyncio.Event()))
        await asyncio.sleep(0.12)
        assert not run_task.done()
        finalizer.release.set()
        with pytest.raises(PipelineFault, match="finalization exceeded"):
            await asyncio.wait_for(run_task, timeout=1)
        assert len(finalizer.calls) == 1
        await runtime.stop()

    run_async(scenario)


def test_cancelled_runtime_stop_cannot_cancel_active_finalization() -> None:
    @dataclass
    class BlockingFinalizer(FakeFinalizer):
        entered: threading.Event = field(default_factory=threading.Event)
        release: threading.Event = field(default_factory=threading.Event)

        def finalize(
            self,
            *,
            provisional_video_name: str,
            sidecar: ClipSidecar,
            retention_order: int,
        ) -> object:
            self.entered.set()
            assert self.release.wait(timeout=2)
            return super().finalize(
                provisional_video_name=provisional_video_name,
                sidecar=sidecar,
                retention_order=retention_order,
            )

    async def scenario() -> None:
        backend = FakeBackend()
        finalizer = BlockingFinalizer()
        runtime = GStreamerRecorderRuntime(
            config_path=Path("/etc/dashcam/config.toml"),
            identity_path=Path("/etc/dashcam/storage-volume.env"),
            backend_factory=RecordingFactory(backend),
            preflight=FakePreflight(ready_storage_with_device()),
            boot_id_reader=lambda: "abcdef123456",
            boot_uuid_reader=lambda: UUID("12345678-1234-5678-9234-567812345678"),
            sequence_planner=lambda root, pending, boot: 0,
            finalizer_factory=lambda root, device: finalizer,
            monotonic_ns=lambda: 1_000,
            limits=RuntimeLimits(finalizer_timeout_s=1),
        )
        config = default_config()
        await runtime.check(config)
        await runtime.start(config)
        await backend.finalized.put(
            FinalizedFragment(
                Path("/srv/dashcam/pending/boot-abcdef123456-000000.partial.mp4"),
                0,
                1_000_000_000,
            )
        )
        assert await asyncio.to_thread(finalizer.entered.wait, 1)

        first_stop = asyncio.create_task(runtime.stop())
        await asyncio.sleep(0)
        first_stop.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_stop

        second_stop = asyncio.create_task(runtime.stop())
        await asyncio.sleep(0)
        assert not second_stop.done()
        assert finalizer.calls == []
        finalizer.release.set()
        await asyncio.wait_for(second_stop, timeout=1)
        assert len(finalizer.calls) == 1

    run_async(scenario)


def test_recoverable_runtime_error_gets_fresh_storage_sequence_and_backend() -> None:
    async def scenario() -> None:
        failure_gate = asyncio.Event()
        first = CounterBackend(
            run_error=RecoverablePipelineError("camera stream failed"),
            run_error_gate=failure_gate,
            counters=FrameCounters(90, 88, 2, "encoder_input_pts"),
        )
        second = CounterBackend(counters=FrameCounters(30, 30, 0, "encoder_input_pts"))
        preflight = ScriptedPreflight([ready_storage_with_device(), ready_storage_with_device()])
        waiter = ImmediateBackoff()
        runtime, factory = scripted_runtime(
            [first, second],
            preflight,
            waiter,
            sequence_values=[4, 5],
            finalizer=FakeFinalizer(),
        )
        events: list[RuntimeLifecycleEvent] = []
        runtime.bind_lifecycle_observer(events.append)
        config = default_config()
        await runtime.check(config)
        await runtime.start(config)
        stop_requested = asyncio.Event()
        run_task = asyncio.create_task(runtime.run(stop_requested))

        failure_gate.set()
        await asyncio.wait_for(second.run_started.wait(), timeout=1)
        while len(events) < 3:
            await asyncio.sleep(0)

        assert [output.start_index for output in factory.outputs] == [4, 5]
        assert len(preflight.calls) == 2
        assert first.stop_calls == 1
        assert waiter.delays == [1.0]
        assert [event.kind for event in events] == [
            RuntimeLifecycleEventKind.RECOVERING,
            RuntimeLifecycleEventKind.RESTARTING,
            RuntimeLifecycleEventKind.RECOVERED,
        ]
        snapshot = runtime.runtime_snapshot()
        assert snapshot["pipeline_restart_count"] == 1
        assert runtime.recording_progress_token() == 118
        assert snapshot["frames"] == {
            "raw": 120,
            "encoded": 118,
            "dropped": 2,
            "drop_source": "encoder_input_pts",
        }

        stop_requested.set()
        await run_task
        await runtime.stop()
        assert second.stop_calls == 1

    run_async(scenario)


def test_recovery_is_bounded_to_exact_one_two_four_delays_then_exhausts() -> None:
    async def scenario() -> None:
        failure_gate = asyncio.Event()
        backends = [
            FakeBackend(
                run_error=RecoverablePipelineError("runtime failure"),
                run_error_gate=failure_gate,
            ),
            *[
                FakeBackend(start_error=RecoverablePipelineError(f"start failure {index}"))
                for index in range(1, 4)
            ],
        ]
        preflight = ScriptedPreflight([ready_storage()] * 4)
        waiter = ImmediateBackoff()
        runtime, factory = scripted_runtime(backends, preflight, waiter)
        events: list[RuntimeLifecycleEvent] = []
        runtime.bind_lifecycle_observer(events.append)
        config = default_config()
        await runtime.check(config)
        await runtime.start(config)
        failure_gate.set()

        with pytest.raises(PipelineRecoveryExhausted, match="exhausted"):
            await runtime.run(asyncio.Event())

        assert waiter.delays == [1.0, 2.0, 4.0]
        assert len(factory.outputs) == 4
        assert len(preflight.calls) == 4
        assert runtime.runtime_snapshot()["pipeline_restart_count"] == 3
        assert [event.kind for event in events].count(RuntimeLifecycleEventKind.RESTARTING) == 3
        assert events[-1].kind is RuntimeLifecycleEventKind.EXHAUSTED
        assert all(backend.stop_calls == 1 for backend in backends)

    run_async(scenario)


def test_stop_during_backoff_cancels_replacement_without_counting_restart() -> None:
    async def scenario() -> None:
        failure_gate = asyncio.Event()
        first = FakeBackend(
            run_error=RecoverablePipelineError("camera disappeared"),
            run_error_gate=failure_gate,
        )
        waiter = ImmediateBackoff(stop_on_call=1)
        preflight = ScriptedPreflight([ready_storage()])
        runtime, factory = scripted_runtime([first], preflight, waiter)
        config = default_config()
        await runtime.check(config)
        await runtime.start(config)
        failure_gate.set()
        stop_requested = asyncio.Event()

        await runtime.run(stop_requested)

        assert stop_requested.is_set()
        assert waiter.delays == [1.0]
        assert len(factory.outputs) == 1
        assert len(preflight.calls) == 1
        assert runtime.runtime_snapshot()["pipeline_restart_count"] == 0
        assert first.stop_calls == 1

    run_async(scenario)


def test_replacement_storage_fault_is_terminal_before_new_camera_open() -> None:
    async def scenario() -> None:
        failure_gate = asyncio.Event()
        first = FakeBackend(
            run_error=RecoverablePipelineError("encoder failed"),
            run_error_gate=failure_gate,
        )
        refused = PreflightResult(
            StorageState.FAULTED,
            (PreflightReason.UNMOUNTED,),
            None,
            False,
            False,
        )
        preflight = ScriptedPreflight([ready_storage(), refused])
        runtime, factory = scripted_runtime([first], preflight, ImmediateBackoff())
        config = default_config()
        await runtime.check(config)
        await runtime.start(config)
        failure_gate.set()

        with pytest.raises(RecorderStorageFault, match="replacement"):
            await runtime.run(asyncio.Event())

        assert len(preflight.calls) == 2
        assert len(factory.outputs) == 1
        assert first.stop_calls == 1

    run_async(scenario)


def test_cleanup_failure_precedes_recovery_and_opens_no_replacement() -> None:
    async def scenario() -> None:
        failure_gate = asyncio.Event()
        first = FakeBackend(
            run_error=RecoverablePipelineError("camera failed"),
            run_error_gate=failure_gate,
            stop_error=RuntimeError("NULL transition failed"),
        )
        waiter = ImmediateBackoff()
        runtime, factory = scripted_runtime(
            [first],
            ScriptedPreflight([ready_storage()]),
            waiter,
        )
        config = default_config()
        await runtime.check(config)
        await runtime.start(config)
        failure_gate.set()

        with pytest.raises(PipelineFault, match="NULL transition failed"):
            await runtime.run(asyncio.Event())

        assert waiter.delays == []
        assert len(factory.outputs) == 1

    run_async(scenario)


def test_observer_failure_is_terminal_after_cleanup_and_keeps_media_cause() -> None:
    async def scenario() -> None:
        failure_gate = asyncio.Event()
        media_error = RecoverablePipelineError("critical camera failure")
        first = FakeBackend(run_error=media_error, run_error_gate=failure_gate)
        runtime, factory = scripted_runtime(
            [first],
            ScriptedPreflight([ready_storage()]),
            ImmediateBackoff(),
        )

        def reject(event: RuntimeLifecycleEvent) -> None:
            raise RuntimeError(f"observer rejected {event.kind.value}")

        runtime.bind_lifecycle_observer(reject)
        config = default_config()
        await runtime.check(config)
        await runtime.start(config)
        failure_gate.set()

        with pytest.raises(RuntimeObserverFault) as caught:
            await runtime.run(asyncio.Event())

        assert caught.value.event.kind is RuntimeLifecycleEventKind.RECOVERING
        assert caught.value.__cause__ is media_error
        assert first.stop_calls == 1
        assert len(factory.outputs) == 1

    run_async(scenario)


@pytest.mark.parametrize(
    ("rejected_kind", "expected_outputs", "expected_replacement_stops"),
    [
        (RuntimeLifecycleEventKind.RESTARTING, 1, 0),
        (RuntimeLifecycleEventKind.RECOVERED, 2, 1),
    ],
)
def test_observer_failure_before_or_after_replacement_never_leaks_camera(
    rejected_kind: RuntimeLifecycleEventKind,
    expected_outputs: int,
    expected_replacement_stops: int,
) -> None:
    async def scenario() -> None:
        failure_gate = asyncio.Event()
        first = FakeBackend(
            run_error=RecoverablePipelineError("critical camera failure"),
            run_error_gate=failure_gate,
        )
        second = FakeBackend()
        runtime, factory = scripted_runtime(
            [first, second],
            ScriptedPreflight([ready_storage(), ready_storage()]),
            ImmediateBackoff(),
        )

        def observer(event: RuntimeLifecycleEvent) -> None:
            if event.kind is rejected_kind:
                raise RuntimeError("observer transition failed")

        runtime.bind_lifecycle_observer(observer)
        config = default_config()
        await runtime.check(config)
        await runtime.start(config)
        failure_gate.set()

        with pytest.raises(RuntimeObserverFault) as caught:
            await runtime.run(asyncio.Event())

        assert caught.value.event.kind is rejected_kind
        assert len(factory.outputs) == expected_outputs
        assert first.stop_calls == 1
        assert second.stop_calls == expected_replacement_stops

    run_async(scenario)


def test_one_stable_ordinary_clip_resets_only_consecutive_recovery_budget() -> None:
    async def scenario() -> None:
        first_failure = asyncio.Event()
        second_failure = asyncio.Event()
        first = FakeBackend(
            run_error=RecoverablePipelineError("first outage"),
            run_error_gate=first_failure,
        )
        second = FakeBackend(
            run_error=RecoverablePipelineError("second outage"),
            run_error_gate=second_failure,
        )
        third = FakeBackend()
        finalizer = FakeFinalizer()
        preflight = ScriptedPreflight([ready_storage_with_device()] * 3)
        runtime, _ = scripted_runtime(
            [first, second, third],
            preflight,
            ImmediateBackoff(),
            sequence_values=[0, 1, 2],
            finalizer=finalizer,
        )
        events: list[RuntimeLifecycleEvent] = []
        runtime.bind_lifecycle_observer(events.append)
        config = default_config()
        await runtime.check(config)
        await runtime.start(config)
        stop_requested = asyncio.Event()
        run_task = asyncio.create_task(runtime.run(stop_requested))
        first_failure.set()
        await asyncio.wait_for(second.run_started.wait(), timeout=1)
        await second.finalized.put(
            FinalizedFragment(
                Path("/srv/dashcam/pending/boot-abcdef123456-000001.partial.mp4"),
                1,
                60_000_000_000,
            )
        )
        while len(finalizer.calls) < 1:
            await asyncio.sleep(0)
        second_failure.set()
        await asyncio.wait_for(third.run_started.wait(), timeout=1)

        recovering_attempts = [
            event.recovery_attempt
            for event in events
            if event.kind is RuntimeLifecycleEventKind.RECOVERING
        ]
        assert recovering_attempts == [1, 1]
        assert runtime.runtime_snapshot()["pipeline_restart_count"] == 2

        stop_requested.set()
        await run_task
        await runtime.stop()

    run_async(scenario)


def test_factory_is_lazy_and_builds_without_loading_gi() -> None:
    runtime = build_production_runtime(
        config_path=Path("/etc/dashcam/config.toml"),
        identity_path=Path("/etc/dashcam/storage-volume.env"),
    )
    assert isinstance(runtime, GStreamerRecorderRuntime)
    gps_factory = runtime._gps_service_factory
    assert gps_factory is not None
    gps_service = cast(GpsService, gps_factory(default_config().gps))
    tracker = gps_service._anchor_tracker
    assert tracker is not None
    assert tracker.uncertainty_ns == 250_000_000
    assert tracker.policy.earliest_utc.isoformat() == "2024-01-01T00:00:00+00:00"
    assert tracker.policy.latest_utc.isoformat() == "2100-01-01T00:00:00+00:00"
    assert tracker.policy.gps_stale_after_ns == 2_000_000_000
    telemetry = gps_service._telemetry_collector
    assert telemetry is not None
    assert telemetry.counters.retained_samples == 0


def test_production_factory_enables_accepted_audio_recovery_with_refusal_overrides() -> None:
    output = SegmentedOutputConfig(
        Path("/srv/dashcam/pending"),
        "abcdef123456",
    )
    matched = matched_audio_outcome()
    assert matched.device is not None
    plan = AudioCapturePlan.from_match(matched.device, default_config().audio)

    ordinary = build_production_runtime(
        config_path=Path("/etc/dashcam/config.toml"),
        identity_path=Path("/etc/dashcam/storage-volume.env"),
    )
    ordinary_factory = ordinary._audio_backend_factory
    assert ordinary_factory is not None
    ordinary_backend = ordinary_factory(output, plan)
    assert isinstance(ordinary_backend, GStreamerBackend)
    assert ordinary_backend._enable_audio_loss_isolation is True
    assert ordinary_backend._enable_audio_restoration is True

    restoration_refusal = build_production_runtime(
        config_path=Path("/etc/dashcam/config.toml"),
        identity_path=Path("/etc/dashcam/storage-volume.env"),
        enable_audio_restoration=False,
    )
    restoration_refusal_factory = restoration_refusal._audio_backend_factory
    assert restoration_refusal_factory is not None
    restoration_refusal_backend = restoration_refusal_factory(output, plan)
    assert isinstance(restoration_refusal_backend, GStreamerBackend)
    assert restoration_refusal_backend._enable_audio_loss_isolation is True
    assert restoration_refusal_backend._enable_audio_restoration is False

    refusal = build_production_runtime(
        config_path=Path("/etc/dashcam/config.toml"),
        identity_path=Path("/etc/dashcam/storage-volume.env"),
        enable_unvalidated_audio_loss_isolation=False,
        enable_audio_restoration=False,
    )
    refusal_factory = refusal._audio_backend_factory
    assert refusal_factory is not None
    refusal_backend = refusal_factory(output, plan)
    assert isinstance(refusal_backend, GStreamerBackend)
    assert refusal_backend._enable_audio_loss_isolation is False
    assert refusal_backend._enable_audio_restoration is False

    with pytest.raises(ValueError, match="requires audio-loss"):
        build_production_runtime(
            config_path=Path("/etc/dashcam/config.toml"),
            identity_path=Path("/etc/dashcam/storage-volume.env"),
            enable_unvalidated_audio_loss_isolation=False,
            enable_audio_restoration=True,
        )


def test_runtime_limits_are_strictly_bounded() -> None:
    with pytest.raises(ValueError):
        RuntimeLimits(first_fragment_timeout_s=0)
    with pytest.raises(ValueError):
        RuntimeLimits(task_stop_timeout_s=121)


def test_runtime_snapshot_keeps_negotiated_caps_distinct_from_configured_intent() -> None:
    runtime, _ = runtime_for(FakeBackend())
    runtime._config = default_config()
    runtime._effective_profile = VideoProfile()
    runtime._effective_caps = EffectiveCaps(1920, 1080, 30, 1, "NV12", "h264", "high", "4.1")

    video = runtime.runtime_snapshot()["video"]

    assert video == {
        "width": 1920,
        "height": 1080,
        "frames_per_second": 30,
        "codec": "h264",
        "hardware_encoded": True,
        "effective_caps": {
            "raw_format": "NV12",
            "fps_numerator": 30,
            "fps_denominator": 1,
            "h264_profile": "high",
            "h264_level": "4.1",
        },
        "configured": {"target_bitrate_bps": 8_000_000, "keyframe_interval_frames": 30},
        "encoder_identity": None,
    }


def test_runtime_snapshot_exposes_closed_truthful_storage_mount_shape() -> None:
    runtime, _ = runtime_for(FakeBackend())
    runtime._preflight_result = ready_storage_with_device()

    storage = runtime.runtime_snapshot()["storage_preflight"]

    assert storage == {
        "state": "READY",
        "reasons": [],
        "ready": True,
        "mount": {
            "target": "/srv/dashcam",
            "mounted": True,
            "filesystem": "exfat",
            "label": "DASHCAM",
            "uuid_suffix": "3EA7",
            "device_id": "179:3",
            "read_write": True,
        },
        "free_bytes": 20_000_000_000,
        "capacity_bytes": 24_000_000_000,
    }


def test_video_fixture_covers_every_declared_fixed_field() -> None:
    assert set(VideoConfig.__dataclass_fields__) == {
        "width",
        "height",
        "fps",
        "codec",
        "hardware_encoder_required",
        "bitrate_bps",
        "keyframe_interval_frames",
        "clip_duration_s",
        "container",
    }


@pytest.mark.parametrize(
    ("enabled", "status", "expected_state", "expected_reason"),
    [
        (True, AudioDiscoveryStatus.NOT_FOUND, "UNAVAILABLE", "not_found"),
        (True, AudioDiscoveryStatus.AMBIGUOUS, "FAULTED", "ambiguous"),
        (True, AudioDiscoveryStatus.REFUSED, "FAULTED", "refused"),
        (False, AudioDiscoveryStatus.MATCHED, "DISABLED", "disabled_by_config"),
    ],
)
def test_audio_nonmatches_preserve_video_only_startup(
    enabled: bool,
    status: AudioDiscoveryStatus,
    expected_state: str,
    expected_reason: str,
) -> None:
    async def scenario() -> None:
        video = FakeBackend()
        video_factory = RecordingFactory(video)
        audio_factory = RecordingAudioFactory(AudioFakeBackend())
        discovery_calls = 0

        def discover(_selector: object) -> AudioDiscoveryOutcome:
            nonlocal discovery_calls
            discovery_calls += 1
            if status is AudioDiscoveryStatus.MATCHED:
                return matched_audio_outcome()
            return AudioDiscoveryOutcome(status)

        runtime = GStreamerRecorderRuntime(
            config_path=Path("/etc/dashcam/config.toml"),
            identity_path=Path("/etc/dashcam/storage-volume.env"),
            backend_factory=video_factory,
            audio_backend_factory=cast(AudioBackendFactory, audio_factory),
            audio_discovery=cast(AudioDiscoverer, discover),
            preflight=FakePreflight(),
            boot_id_reader=lambda: "abcdef123456",
            sequence_planner=lambda root, pending, boot: 0,
            ownership=CameraOwnership(),
        )
        config = default_config()
        if not enabled:
            config = replace(config, audio=replace(config.audio, enabled=False))
        await runtime.check(config)
        await runtime.start(config)

        assert len(video_factory.outputs) == 1
        assert audio_factory.calls == []
        assert discovery_calls == (1 if enabled else 0)
        audio = runtime.runtime_snapshot()["audio"]
        assert isinstance(audio, dict)
        assert audio["state"] == expected_state
        assert audio["reason"] == expected_reason
        await runtime.stop()

    run_async(scenario)


def test_audio_discovery_precedes_camera_ownership_and_builds_one_immutable_plan() -> None:
    async def scenario() -> None:
        ownership = CameraOwnership()
        video_factory = RecordingFactory(FakeBackend())
        audio_backend = AudioFakeBackend()
        audio_factory = RecordingAudioFactory(audio_backend)

        def discover(_selector: object) -> AudioDiscoveryOutcome:
            assert ownership.owner is None
            return matched_audio_outcome()

        runtime = GStreamerRecorderRuntime(
            config_path=Path("/etc/dashcam/config.toml"),
            identity_path=Path("/etc/dashcam/storage-volume.env"),
            backend_factory=video_factory,
            audio_backend_factory=cast(AudioBackendFactory, audio_factory),
            audio_discovery=cast(AudioDiscoverer, discover),
            preflight=FakePreflight(),
            boot_id_reader=lambda: "abcdef123456",
            sequence_planner=lambda root, pending, boot: 7,
            ownership=ownership,
        )
        config = default_config()
        await runtime.check(config)
        await runtime.start(config)

        assert video_factory.outputs == []
        assert len(audio_factory.calls) == 1
        output, plan = audio_factory.calls[0]
        assert output.start_index == 7
        assert plan.endpoint == "hw:1,0,0"
        assert plan.sample_rate_hz == 48_000
        assert ownership.owner == "dashcamd"
        audio = runtime.runtime_snapshot()["audio"]
        assert isinstance(audio, dict)
        assert audio["state"] == RuntimeAudioState.MATCHED.value
        assert audio["matched"] == {
            "vendor_id": "08bb",
            "product_id": "2902",
            "product": "USB_PnP_Sound_Device",
            "physical_path": "platform-3f980000.usb-usb-0:1:1.0",
            "alsa_card_id": None,
        }
        await runtime.stop()

    run_async(scenario)


def test_invalid_selector_is_bounded_fault_and_never_reaches_discovery() -> None:
    async def scenario() -> None:
        discovery_calls = 0

        def discover(_selector: object) -> AudioDiscoveryOutcome:
            nonlocal discovery_calls
            discovery_calls += 1
            return matched_audio_outcome()

        runtime, _ = runtime_for(FakeBackend())
        runtime._audio_discovery = cast(AudioDiscoverer, discover)
        config = replace(
            default_config(),
            audio=AudioConfig(device_match="hw:1,0"),
        )
        await runtime.check(config)
        await runtime.start(config)
        audio = runtime.runtime_snapshot()["audio"]
        assert isinstance(audio, dict)
        assert audio["state"] == "FAULTED"
        assert audio["reason"] == "invalid_selector"
        assert isinstance(audio["detail"], str)
        assert len(audio["detail"]) <= 512
        assert discovery_calls == 0
        await runtime.stop()

    run_async(scenario)


def test_typed_audio_startup_failure_falls_back_once_with_fresh_sequence() -> None:
    async def scenario() -> None:
        audio_backend = AudioFakeBackend(start_error=AudioStartupError("AAC negotiation failed"))
        video_backend = FakeBackend()
        video_factory = RecordingFactory(video_backend)
        audio_factory = RecordingAudioFactory(audio_backend)
        planner_calls = 0

        def plan_sequence(_root: Path, _pending: Path, _boot: str) -> int:
            nonlocal planner_calls
            planner_calls += 1
            return 4

        runtime = GStreamerRecorderRuntime(
            config_path=Path("/etc/dashcam/config.toml"),
            identity_path=Path("/etc/dashcam/storage-volume.env"),
            backend_factory=video_factory,
            audio_backend_factory=cast(AudioBackendFactory, audio_factory),
            audio_discovery=cast(AudioDiscoverer, lambda _selector: matched_audio_outcome()),
            preflight=FakePreflight(),
            boot_id_reader=lambda: "abcdef123456",
            sequence_planner=plan_sequence,
            ownership=CameraOwnership(),
        )
        config = default_config()
        await runtime.check(config)
        await runtime.start(config)

        assert planner_calls == 2
        assert audio_factory.calls[0][0].start_index == 4
        assert video_factory.outputs[0].start_index == 5
        assert audio_backend.stop_calls == 1
        snapshot = runtime.runtime_snapshot()
        assert snapshot["pipeline_restart_count"] == 0
        audio = snapshot["audio"]
        assert isinstance(audio, dict)
        assert audio["state"] == "FAULTED"
        assert audio["reason"] == "startup_audio_failure"
        assert audio["startup_video_only_fallback_used"] is True
        assert audio["matched"] is not None
        await runtime.stop()

    run_async(scenario)


def test_unclassified_matched_graph_failure_does_not_weaken_camera_fault() -> None:
    async def scenario() -> None:
        audio_backend = AudioFakeBackend(
            start_error=RecoverablePipelineError("PLAYING failed for unknown branch")
        )
        video_factory = RecordingFactory(FakeBackend())
        runtime = GStreamerRecorderRuntime(
            config_path=Path("/etc/dashcam/config.toml"),
            identity_path=Path("/etc/dashcam/storage-volume.env"),
            backend_factory=video_factory,
            audio_backend_factory=cast(AudioBackendFactory, RecordingAudioFactory(audio_backend)),
            audio_discovery=cast(AudioDiscoverer, lambda _selector: matched_audio_outcome()),
            preflight=FakePreflight(),
            boot_id_reader=lambda: "abcdef123456",
            sequence_planner=lambda root, pending, boot: 4,
            ownership=CameraOwnership(),
        )
        config = default_config()
        await runtime.check(config)
        with pytest.raises(RecoverablePipelineError, match="unknown branch"):
            await runtime.start(config)

        assert video_factory.outputs == []
        assert audio_backend.stop_calls == 1
        assert runtime.runtime_snapshot()["pipeline_restart_count"] == 0

    run_async(scenario)


@pytest.mark.parametrize(("encoded_units", "available"), [(100, True), (0, False)])
def test_sidecar_audio_truth_requires_effective_caps_and_fragment_access_units(
    encoded_units: int,
    available: bool,
) -> None:
    async def scenario() -> None:
        backend = AudioFakeBackend()
        audio_factory = RecordingAudioFactory(backend)
        finalizer = FakeFinalizer()
        runtime = GStreamerRecorderRuntime(
            config_path=Path("/etc/dashcam/config.toml"),
            identity_path=Path("/etc/dashcam/storage-volume.env"),
            backend_factory=RecordingFactory(FakeBackend()),
            audio_backend_factory=cast(AudioBackendFactory, audio_factory),
            audio_discovery=cast(AudioDiscoverer, lambda _selector: matched_audio_outcome()),
            preflight=FakePreflight(ready_storage_with_device()),
            boot_id_reader=lambda: "abcdef123456",
            boot_uuid_reader=lambda: UUID("12345678-1234-5678-9234-567812345678"),
            sequence_planner=lambda root, pending, boot: 0,
            finalizer_factory=lambda root, device: finalizer,
            monotonic_ns=lambda: 1_000,
            ownership=CameraOwnership(),
        )
        config = default_config()
        await runtime.check(config)
        await runtime.start(config)
        backend.audio_units = encoded_units
        await backend.finalized.put(
            FinalizedFragment(
                Path("/srv/dashcam/pending/boot-abcdef123456-000000.partial.mp4"),
                0,
                1_000_000_000,
            )
        )
        await asyncio.wait_for(finalizer.completed.wait(), timeout=1)
        await runtime.stop()

        sidecar = finalizer.calls[0][1]
        assert isinstance(sidecar, ClipSidecar)
        assert sidecar.audio.available is available
        if available:
            assert sidecar.audio.codec == "aac"
            assert sidecar.audio.sample_rate_hz == 48_000
            assert sidecar.audio.channels == 1
            assert sidecar.audio.target_bitrate_bps == 128_000
        else:
            assert sidecar.audio.codec is None
            assert any("no encoded AAC" in warning for warning in sidecar.warnings)
        audio = runtime.runtime_snapshot()["audio"]
        assert isinstance(audio, dict)
        assert audio["last_clip_encoded_access_units"] == encoded_units

    run_async(scenario)


def test_generation_contract_keeps_audio_truth_and_timing_on_late_closures() -> None:
    async def scenario() -> None:
        backend = AudioFakeBackend()
        finalizer = FakeFinalizer()
        runtime = GStreamerRecorderRuntime(
            config_path=Path("/etc/dashcam/config.toml"),
            identity_path=Path("/etc/dashcam/storage-volume.env"),
            backend_factory=RecordingFactory(FakeBackend()),
            audio_backend_factory=cast(AudioBackendFactory, RecordingAudioFactory(backend)),
            audio_discovery=cast(AudioDiscoverer, lambda _selector: matched_audio_outcome()),
            preflight=FakePreflight(ready_storage_with_device()),
            boot_id_reader=lambda: "abcdef123456",
            boot_uuid_reader=lambda: UUID("12345678-1234-5678-9234-567812345678"),
            sequence_planner=lambda root, pending, boot: 0,
            finalizer_factory=lambda root, device: finalizer,
            monotonic_ns=lambda: 1_000,
            ownership=CameraOwnership(),
        )
        config = default_config()
        await runtime.check(config)
        await runtime.start(config)
        caps = backend.effective_audio_caps
        backend.audio_units = 0

        fragments = {
            0: FinalizedFragment(
                Path("/srv/dashcam/pending/boot-abcdef123456-000000.partial.mp4"),
                0,
                60_000_000_000,
                0,
                FragmentMediaContract(1, caps, 100),
            ),
            1: FinalizedFragment(
                Path("/srv/dashcam/pending/boot-abcdef123456-000001.partial.mp4"),
                1,
                120_000_000_000,
                60_000_000_000,
                FragmentMediaContract(2, None, 0),
            ),
            2: FinalizedFragment(
                Path("/srv/dashcam/pending/boot-abcdef123456-000002.partial.mp4"),
                2,
                180_000_000_000,
                120_000_000_000,
                FragmentMediaContract(3, caps, 100),
            ),
        }

        # The successor closes first. The runtime must hold it until the late
        # retiring A/V closure arrives, then finalize in global sequence order.
        await backend.finalized.put(fragments[1])
        await asyncio.sleep(0)
        assert finalizer.calls == []
        runtime._effective_audio_caps = None
        await backend.finalized.put(fragments[0])
        await backend.finalized.put(fragments[2])
        await asyncio.wait_for(backend.finalized.join(), timeout=1)
        await runtime.stop()

        sidecars = {
            sidecar.sequence: sidecar
            for _, sidecar, _ in finalizer.calls
            if isinstance(sidecar, ClipSidecar)
        }
        assert [
            sidecar.sequence
            for _, sidecar, _ in finalizer.calls
            if isinstance(sidecar, ClipSidecar)
        ] == [0, 1, 2]
        assert sidecars[0].audio.available is True
        assert sidecars[1].audio.available is False
        assert sidecars[2].audio.available is True
        assert sidecars[0].audio.codec == sidecars[2].audio.codec == "aac"
        assert sidecars[1].audio.codec is None
        assert sidecars[0].start_monotonic_ns == 1_000
        assert sidecars[0].end_monotonic_ns == 60_000_001_000
        assert sidecars[1].start_monotonic_ns == 60_000_001_000
        assert sidecars[1].end_monotonic_ns == 120_000_001_000
        assert sidecars[2].start_monotonic_ns == 120_000_001_000
        assert sidecars[2].end_monotonic_ns == 180_000_001_000
        assert all(
            not any("no encoded AAC" in warning for warning in sidecar.warnings)
            for sidecar in sidecars.values()
        )

    run_async(scenario)


def test_runtime_imports_the_current_restored_endpoint_and_alsa_identity() -> None:
    async def scenario() -> None:
        backend = AudioFakeBackend()
        runtime = GStreamerRecorderRuntime(
            config_path=Path("/etc/dashcam/config.toml"),
            identity_path=Path("/etc/dashcam/storage-volume.env"),
            backend_factory=RecordingFactory(FakeBackend()),
            audio_backend_factory=cast(AudioBackendFactory, RecordingAudioFactory(backend)),
            audio_discovery=cast(AudioDiscoverer, lambda _selector: matched_audio_outcome()),
            preflight=FakePreflight(),
            boot_id_reader=lambda: "abcdef123456",
            sequence_planner=lambda root, pending, boot: 0,
            ownership=CameraOwnership(),
        )
        config = default_config()
        await runtime.check(config)
        await runtime.start(config)
        restored_identity = AlsaIdentity(
            "08bb",
            "2902",
            physical_path="platform-3f980000.usb-usb-0:1:1.0",
            alsa_card_id="USB_PnP_Sound_Device",
            product="USB_PnP_Sound_Device",
        )
        backend.audio_capture_plan = AudioCapturePlan(
            "hw:9,0,0",
            restored_identity,
            48_000,
            1,
            "aac",
            128_000,
        )
        backend.audio_restoration_snapshot = {
            "restoration_count": 1,
            "matched_endpoint": "hw:9,0,0",
        }

        audio = runtime.runtime_snapshot()["audio"]

        assert isinstance(audio, dict)
        assert audio["state"] == RuntimeAudioState.MATCHED.value
        assert audio["reason"] == "microphone_restored"
        assert audio["matched"] == {
            "vendor_id": "08bb",
            "product_id": "2902",
            "product": "USB_PnP_Sound_Device",
            "physical_path": "platform-3f980000.usb-usb-0:1:1.0",
            "alsa_card_id": "USB_PnP_Sound_Device",
        }
        assert audio["restoration"] == {
            "restoration_count": 1,
            "matched_endpoint": "hw:9,0,0",
        }
        assert audio["last_restoration_failure"] is None
        await runtime.stop()

    run_async(scenario)


def test_runtime_preserves_a_bounded_restoration_failure_across_replacement() -> None:
    backend = AudioFakeBackend()
    runtime = GStreamerRecorderRuntime(
        config_path=Path("/etc/dashcam/config.toml"),
        identity_path=Path("/etc/dashcam/storage-volume.env"),
        backend_factory=RecordingFactory(FakeBackend()),
        ownership=CameraOwnership(),
    )
    backend.audio_restoration_snapshot = {
        "last_failure": {
            "critical": True,
            "phase": "media_proof",
            "detail": "GStreamerDriverError: restored slot did not open",
            "monotonic_ns": 123_456,
        }
    }
    runtime._backend = backend

    runtime._capture_audio_restoration_failure()
    runtime._backend = AudioFakeBackend(audio_restoration_snapshot={})

    audio = runtime.runtime_snapshot()["audio"]
    assert isinstance(audio, dict)
    assert audio["last_restoration_failure"] == {
        "critical": True,
        "phase": "media_proof",
        "detail": "GStreamerDriverError: restored slot did not open",
        "monotonic_ns": 123_456,
    }


def test_shutdown_refuses_an_unresolved_generation_closure_gap() -> None:
    async def scenario() -> None:
        backend = FakeBackend()
        runtime, _ = runtime_for(backend)
        config = default_config()
        await runtime.check(config)
        await runtime.start(config)
        await backend.finalized.put(
            FinalizedFragment(
                Path("/srv/dashcam/pending/boot-abcdef123456-000001.partial.mp4"),
                1,
                120_000_000_000,
                60_000_000_000,
                FragmentMediaContract(2, None, 0),
            )
        )
        await asyncio.wait_for(backend.finalized.join(), timeout=1)

        with pytest.raises(PipelineFault, match="unresolved gap"):
            await runtime.stop()
        assert runtime.runtime_snapshot()["pipeline_restart_count"] == 0

    run_async(scenario)


def test_duplicate_generation_closure_is_terminal_and_never_regresses_last_clip() -> None:
    async def scenario() -> None:
        backend = FakeBackend()
        finalizer = FakeFinalizer()
        runtime = GStreamerRecorderRuntime(
            config_path=Path("/etc/dashcam/config.toml"),
            identity_path=Path("/etc/dashcam/storage-volume.env"),
            backend_factory=RecordingFactory(backend),
            preflight=FakePreflight(ready_storage_with_device()),
            boot_id_reader=lambda: "abcdef123456",
            boot_uuid_reader=lambda: UUID("12345678-1234-5678-9234-567812345678"),
            sequence_planner=lambda root, pending, boot: 0,
            finalizer_factory=lambda root, device: finalizer,
            monotonic_ns=lambda: 1_000,
            ownership=CameraOwnership(),
        )
        config = replace(default_config(), audio=replace(default_config().audio, enabled=False))
        await runtime.check(config)
        await runtime.start(config)
        fragment = FinalizedFragment(
            Path("/srv/dashcam/pending/boot-abcdef123456-000000.partial.mp4"),
            0,
            60_000_000_000,
            0,
            FragmentMediaContract(1, None, 0),
        )
        await backend.finalized.put(fragment)
        await asyncio.wait_for(backend.finalized.join(), timeout=1)
        assert runtime.runtime_snapshot()["last_clip"]["sequence"] == 0  # type: ignore[index]

        await backend.finalized.put(fragment)
        await asyncio.sleep(0)
        with pytest.raises(RecorderFinalizationFault, match="duplicate or stale"):
            await runtime.run(asyncio.Event())
        assert runtime.runtime_snapshot()["last_clip"]["sequence"] == 0  # type: ignore[index]
        await runtime.stop()

    run_async(scenario)
