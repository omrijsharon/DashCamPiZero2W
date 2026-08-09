"""Storage-bound production lifecycle for the segmented GStreamer recorder."""

from __future__ import annotations

import asyncio
import os
import re
import stat
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol, TypeAlias, cast
from uuid import UUID, uuid4

from dashcam.audio.alsa import AlsaMatchError, AlsaSelector, parse_alsa_selector
from dashcam.audio.linux import (
    AudioDiscoveryOutcome,
    AudioDiscoveryStatus,
    discover_capture_device,
)
from dashcam.catalog import ClipCatalog, EventProtectionResult, EventSource
from dashcam.catalog.database import RetentionThresholdLatch
from dashcam.config import (
    AudioConfig,
    DashcamConfig,
    GpsConfig,
    OverlayConfig,
    StorageConfig,
    VideoConfig,
)
from dashcam.control.dispatcher import CatalogBackend
from dashcam.gps.anchors import NmeaAnchorTracker
from dashcam.gps.clock import AnchorPolicy, MonotonicUtcClock, to_local_time
from dashcam.gps.service import GpsCounters, GpsService, GpsServiceLimits, GpsSnapshot
from dashcam.gps.telemetry import (
    GpsTelemetryCollector,
    GpsTelemetryWindow,
)
from dashcam.metadata.coordinator import MetadataReconciliationRefused
from dashcam.metadata.reconcile import project_anchored_sidecar
from dashcam.metadata.schema import (
    MAX_GPS_SAMPLES,
    AudioSummary,
    ClipSidecar,
    GpsSample,
    GpsSummary,
    TimeAnchor,
    TimeAnchorSource,
    VideoSummary,
)
from dashcam.overlay import OverlayOptions, OverlayTelemetry, build_overlay
from dashcam.recorder.finalizer import (
    DurableRootedFinalizationFilesystem,
    FinalizationRecoveryReport,
    MetadataReconciliationCandidate,
    RecorderClipFinalizer,
)
from dashcam.recorder.gstreamer import (
    AudioCapturePlan,
    AudioCounters,
    AudioStartupError,
    EffectiveAudioCaps,
    EffectiveCaps,
    EncoderIdentity,
    FinalizedFragment,
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
    RestartPolicy,
    VideoProfile,
)
from dashcam.state import GpsState, GpsTimeState, SystemClockState, TimestampQuality
from dashcam.storage.naming import (
    finalized_unsynced_clip_pair,
    provisional_clip_pair,
)
from dashcam.storage.preflight import PreflightResult, run_live_storage_preflight
from dashcam.storage.reclaimer import ReclamationStep
from dashcam.storage.retention import RetentionMode
from dashcam.storage.space import (
    StorageSpaceMonitor,
    StorageSpaceSnapshot,
    build_storage_space_monitor,
)
from dashcam.version import get_version

_BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
_BOOT_ID_RE = re.compile(rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\n?")
_EXPECTED_VIDEO = VideoConfig()
_PROCESS_CAMERA_OWNERSHIP = CameraOwnership()
_MAX_SEQUENCE_ENTRIES = 4096
_MAX_REORDERED_CLOSURES = 16
_MAX_METADATA_CANDIDATE_PAGE = 64
_MAX_METADATA_RECONCILIATIONS_PER_PASS = 2
_MAX_METADATA_RETRY_ATTEMPTS = 3
_MAX_METADATA_TRACKED = 4_096
_SEQUENCE_DIRECTORIES = ("pending", "clips", "protected")
_CASE_INSENSITIVE_CLIP_IDENTITY_RE = re.compile(
    r"(?:boot-(?P<boot>[a-z0-9]{5,16})-(?P<boot_sequence>\d{6})"
    r"(?:\.partial)?|"
    r"\d{8}t\d{6}\.\d{3}z_(?P<utc_boot>[a-z0-9]{5,16})_"
    r"s(?P<utc_sequence>\d{6}))\.(?:mp4|json)",
    re.IGNORECASE,
)
_METRES_PER_SECOND_PER_KNOT = 1852.0 / 3600.0


class RecorderStorageFault(PipelineContractError):
    """Storage evidence became invalid before the camera could safely open."""


class StorageSafetyStop(RecorderStorageFault):
    """Storage policy requested a deliberate bounded clean recorder stop."""


class RuntimeLifecycleEventKind(StrEnum):
    """Bounded critical-video recovery events consumed by the daemon."""

    RECOVERING = "RECOVERING"
    RESTARTING = "RESTARTING"
    RECOVERED = "RECOVERED"
    EXHAUSTED = "EXHAUSTED"


@dataclass(frozen=True, slots=True)
class RuntimeLifecycleEvent:
    """One synchronous, constant-size recovery notification."""

    kind: RuntimeLifecycleEventKind
    restart_count: int
    recovery_attempt: int
    detail: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, RuntimeLifecycleEventKind):
            raise ValueError("runtime lifecycle event kind is invalid")
        for name, value in (
            ("restart_count", self.restart_count),
            ("recovery_attempt", self.recovery_attempt),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.detail is not None and (
            not self.detail or len(self.detail) > 512 or not self.detail.isprintable()
        ):
            raise ValueError("event detail must be 1 to 512 printable characters")


RuntimeLifecycleObserver: TypeAlias = Callable[[RuntimeLifecycleEvent], None]


class RuntimeObserverFault(PipelineFault):
    """The lifecycle observer refused a critical recovery transition."""

    def __init__(
        self,
        event: RuntimeLifecycleEvent,
        observer_error: BaseException,
    ) -> None:
        self.event = event
        self.observer_error = observer_error
        super().__init__(
            f"runtime lifecycle observer failed for {event.kind.value}: "
            f"{_bounded_exception_detail(observer_error)}"
        )


class PipelineRecoveryExhausted(PipelineFault):
    """Critical video failed after all bounded replacement attempts."""


class RecorderFinalizationFault(PipelineFault):
    """A durable clip finalization/reconciliation contract failed."""


class RuntimeAudioState(StrEnum):
    """Bounded optional-audio state which never masks video lifecycle."""

    DISABLED = "DISABLED"
    MATCHED = "MATCHED"
    UNAVAILABLE = "UNAVAILABLE"
    FAULTED = "FAULTED"


class RuntimeBackend(Protocol):
    def configure_overlay_text(self, text: str | None) -> None: ...

    async def start(self, requested_profile: VideoProfile) -> VideoProfile: ...

    async def set_overlay_text(self, text: str | None) -> None: ...

    async def run(self, stop_requested: asyncio.Event) -> None: ...

    async def stop(self) -> None: ...

    async def wait_for_first_fragment_opened(self) -> OpenedFragment: ...

    async def next_opened_fragment(self) -> OpenedFragment: ...

    async def next_finalized_fragment(self) -> FinalizedFragment: ...

    def mark_finalized_fragment_processed(self) -> None: ...

    async def wait_for_finalized_fragments_processed(self) -> None: ...


class RuntimeFinalizer(Protocol):
    def video_size(self, provisional_video_name: str) -> int: ...

    def next_retention_order(self) -> int: ...

    def retention_threshold_latch(self) -> RetentionThresholdLatch | None: ...

    def store_retention_threshold_latch(self, latch: RetentionThresholdLatch) -> None: ...

    def reclaim_storage_once(self, *, boot_id: str, allow_new: bool) -> ReclamationStep: ...

    def expire_download_leases(self, boot_id: str) -> bool: ...

    def finalize(
        self,
        *,
        provisional_video_name: str,
        sidecar: ClipSidecar,
        retention_order: int,
    ) -> object: ...

    def register_active_clip(
        self,
        *,
        provisional_video_name: str,
        clip_id: UUID,
        start_monotonic_ns: int,
        retention_order: int,
    ) -> None: ...

    def reconcile_orphaned_writing(self, *, limit: int) -> tuple[int, bool]: ...

    def execute_intent(self, intent_id: UUID) -> object: ...

    @property
    def control_catalog(self) -> CatalogBackend: ...

    def trigger_event(
        self,
        current_clip_id: UUID | None,
        *,
        source: EventSource,
        monotonic_now_ns: int,
        previous_count: int,
        next_count: int,
        event_id: UUID,
    ) -> EventProtectionResult: ...

    def reconcile_pending(self) -> FinalizationRecoveryReport: ...

    def metadata_reconciliation_candidates(
        self,
        expected_boot_id: UUID,
        *,
        limit: int,
        after_order: int = -1,
        after_clip_id: UUID | None = None,
    ) -> tuple[MetadataReconciliationCandidate, ...]: ...

    def reconcile_metadata(
        self,
        clip_id: UUID,
        *,
        anchor: TimeAnchor,
        expected_boot_id: UUID,
        gps_time_state: GpsTimeState,
        system_clock_state: SystemClockState,
    ) -> object: ...


class FinalizerFactory(Protocol):
    def __call__(
        self,
        recording_root: Path,
        expected_device_id: str,
    ) -> RuntimeFinalizer: ...


class RuntimeControlEndpoint(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def snapshot(self) -> dict[str, object]: ...


class RuntimeControlEndpointFactory(Protocol):
    def __call__(
        self,
        *,
        runtime: GStreamerRecorderRuntime,
        catalog: CatalogBackend,
        config_path: Path,
        boot_id: str,
        status_provider: Callable[[], Mapping[str, object]],
        fault_callback: Callable[[str], None],
    ) -> RuntimeControlEndpoint: ...


class BackendFactory(Protocol):
    def __call__(self, output: SegmentedOutputConfig) -> RuntimeBackend: ...


class AudioBackendFactory(Protocol):
    def __call__(
        self,
        output: SegmentedOutputConfig,
        audio_plan: AudioCapturePlan,
    ) -> RuntimeBackend: ...


class AudioDiscoverer(Protocol):
    def __call__(self, selector: AlsaSelector) -> AudioDiscoveryOutcome: ...


class RuntimeGpsService(Protocol):
    """Optional GPS supervisor isolated from critical media ownership."""

    @property
    def snapshot(self) -> GpsSnapshot: ...

    def telemetry_window(
        self,
        start_monotonic_ns: int,
        end_monotonic_ns: int,
        *,
        max_samples: int,
    ) -> GpsTelemetryWindow: ...

    async def run(self, stop_requested: asyncio.Event) -> None: ...


class GpsServiceFactory(Protocol):
    def __call__(self, config: GpsConfig) -> RuntimeGpsService: ...


class LivePreflight(Protocol):
    def __call__(
        self,
        config: DashcamConfig,
        *,
        identity_path: str,
    ) -> PreflightResult: ...


class RuntimeBackoffWaiter(Protocol):
    async def __call__(self, delay_s: float, stop_requested: asyncio.Event) -> bool: ...


class StorageSpaceMonitorFactory(Protocol):
    def __call__(
        self,
        *,
        storage: StorageConfig,
        volume_uuid: str,
        expected_device_id: str,
        expected_capacity_bytes: int,
        latch_store: RuntimeFinalizer,
    ) -> StorageSpaceMonitor: ...


async def _wait_for_backoff(delay_s: float, stop_requested: asyncio.Event) -> bool:
    try:
        await asyncio.wait_for(stop_requested.wait(), timeout=delay_s)
    except TimeoutError:
        return False
    return True


def _bounded_exception_detail(error: BaseException) -> str:
    raw = f"{type(error).__name__}: {error}".replace("\0", " ")
    detail = " ".join(raw.splitlines()).strip()
    detail = "".join(character if character.isprintable() else " " for character in detail)
    return (detail or type(error).__name__)[:512]


def _storage_space_detail(status: StorageSpaceSnapshot) -> str:
    fault = "NONE" if status.fault is None else status.fault.value
    mode = "UNOBSERVED" if status.mode is None else status.mode.value
    return (
        f"storage retention safety stop mode={mode} fault={fault} "
        f"free_bytes={status.free_bytes} capacity_bytes={status.capacity_bytes}"
    )[:512]


def _recovery_detail(
    error: BaseException,
    *,
    attempt: int,
    maximum: int,
    delay_s: float | None = None,
) -> str:
    prefix = f"attempt={attempt}/{maximum}"
    if delay_s is not None:
        prefix += f" backoff_s={delay_s:g}"
    return f"{prefix} cause={_bounded_exception_detail(error)}"[:512]


@dataclass(frozen=True, slots=True)
class RuntimeLimits:
    first_fragment_timeout_s: float = 10.0
    task_stop_timeout_s: float = 2.0
    finalizer_timeout_s: float = 6.0
    metadata_reconciliation_interval_s: float = 1.0
    overlay_update_interval_s: float = 0.5
    storage_observation_interval_s: float = 1.0
    max_reclamation_steps_per_pass: int = 8
    max_startup_reclamation_steps: int = 64
    max_startup_reconciliation_passes: int = 4

    def __post_init__(self) -> None:
        for name, value in (
            ("first_fragment_timeout_s", self.first_fragment_timeout_s),
            ("task_stop_timeout_s", self.task_stop_timeout_s),
            ("finalizer_timeout_s", self.finalizer_timeout_s),
            (
                "metadata_reconciliation_interval_s",
                self.metadata_reconciliation_interval_s,
            ),
            ("overlay_update_interval_s", self.overlay_update_interval_s),
            ("storage_observation_interval_s", self.storage_observation_interval_s),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not 0 < value <= 120
            ):
                raise ValueError(f"{name} must be greater than zero and at most 120")
        if (
            isinstance(self.max_reclamation_steps_per_pass, bool)
            or not isinstance(self.max_reclamation_steps_per_pass, int)
            or not 1 <= self.max_reclamation_steps_per_pass <= 64
        ):
            raise ValueError("max_reclamation_steps_per_pass must be between 1 and 64")
        if (
            isinstance(self.max_startup_reclamation_steps, bool)
            or not isinstance(self.max_startup_reclamation_steps, int)
            or not 1 <= self.max_startup_reclamation_steps <= 1_024
        ):
            raise ValueError("max_startup_reclamation_steps must be between 1 and 1024")
        if (
            isinstance(self.max_startup_reconciliation_passes, bool)
            or not isinstance(self.max_startup_reconciliation_passes, int)
            or not 1 <= self.max_startup_reconciliation_passes <= 64
        ):
            raise ValueError("max_startup_reconciliation_passes must be between 1 and 64")


def _absolute_posix(path: Path, description: str) -> str:
    value = path.as_posix()
    if not value.startswith("/") or "\0" in value or ".." in path.parts or len(value) > 4096:
        raise ValueError(f"{description} must be a bounded absolute POSIX path")
    return value


def read_short_boot_id(path: Path = _BOOT_ID_PATH) -> str:
    """Read one canonical kernel boot UUID and derive a filename-safe short ID."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise PipelineContractError("boot ID source is not a regular file")
        payload = os.read(descriptor, 38)
        if os.read(descriptor, 1):
            raise PipelineContractError("boot ID source exceeded its exact bound")
    finally:
        os.close(descriptor)
    if _BOOT_ID_RE.fullmatch(payload) is None:
        raise PipelineContractError("boot ID source is not canonical")
    return payload.rstrip().replace(b"-", b"")[:12].decode("ascii")


def read_boot_uuid(path: Path = _BOOT_ID_PATH) -> UUID:
    payload = path.read_bytes()
    if _BOOT_ID_RE.fullmatch(payload) is None:
        raise PipelineContractError("boot ID source is not canonical")
    return UUID(payload.rstrip().decode("ascii"))


def next_pending_sequence(
    recording_root: Path,
    pending_directory: Path,
    boot_id: str,
    *,
    max_entries: int = _MAX_SEQUENCE_ENTRIES,
) -> int:
    """Select a restart-safe sequence from all clip-bearing directories."""

    if pending_directory != recording_root / "pending":
        raise RecorderStorageFault("pending directory escaped the recording root")
    if not 1 <= max_entries <= _MAX_SEQUENCE_ENTRIES:
        raise ValueError("max_entries is outside the reviewed bound")
    root_info = os.lstat(recording_root)
    if not stat.S_ISDIR(root_info.st_mode):
        raise RecorderStorageFault("recording root is not a directory")

    names: set[str] = set()
    highest = -1
    scanned = 0
    directory_identities: list[tuple[Path, int, int]] = []
    for directory_name in _SEQUENCE_DIRECTORIES:
        directory = recording_root / directory_name
        before = os.lstat(directory)
        if not stat.S_ISDIR(before.st_mode) or before.st_dev != root_info.st_dev:
            raise RecorderStorageFault(
                f"{directory_name} directory is not on the verified recording mount"
            )
        directory_identities.append((directory, before.st_dev, before.st_ino))
        with os.scandir(directory) as entries:
            for entry in entries:
                scanned += 1
                if scanned > max_entries:
                    raise RecorderStorageFault(
                        "clip directories exceeded their sequence scan bound"
                    )
                name = entry.name
                if not name or len(name) > 255 or not name.isascii() or not name.isprintable():
                    raise RecorderStorageFault(
                        f"{directory_name} directory contains an unsafe name"
                    )
                names.add(name.casefold())
                identity = _CASE_INSENSITIVE_CLIP_IDENTITY_RE.fullmatch(name)
                if identity is None:
                    continue
                found_boot = identity.group("boot") or identity.group("utc_boot")
                sequence_text = identity.group("boot_sequence") or identity.group("utc_sequence")
                assert found_boot is not None and sequence_text is not None
                if found_boot.casefold() == boot_id.casefold():
                    highest = max(highest, int(sequence_text))

    root_after = os.lstat(recording_root)
    if (
        not stat.S_ISDIR(root_after.st_mode)
        or root_after.st_dev != root_info.st_dev
        or root_after.st_ino != root_info.st_ino
    ):
        raise RecorderStorageFault("recording root changed during sequence allocation")
    for directory, expected_device, expected_inode in directory_identities:
        after = os.lstat(directory)
        if (
            not stat.S_ISDIR(after.st_mode)
            or after.st_dev != expected_device
            or after.st_ino != expected_inode
        ):
            raise RecorderStorageFault(
                f"{directory.name} directory changed during sequence allocation"
            )
    for sequence in range(highest + 1, 1_000_000):
        pair = provisional_clip_pair(boot_id=boot_id, sequence=sequence)
        if pair.video_name.casefold() not in names and pair.metadata_name.casefold() not in names:
            return sequence
    raise RecorderStorageFault("provisional clip sequence space is exhausted")


def _st_dev_from_device_id(device_id: str) -> int | None:
    """Convert one preflight ``major:minor`` identity to an exact ``st_dev``."""

    match = re.fullmatch(r"([0-9]{1,10}):([0-9]{1,10})", device_id)
    if match is None:
        raise RecorderStorageFault("READY storage device identity is invalid")
    make_device = getattr(os, "makedev", None)
    if make_device is None:
        # Non-POSIX test hosts cannot represent Linux st_dev values.  The
        # production target always supplies os.makedev.
        return None
    make_device = cast(Callable[[int, int], int], make_device)
    try:
        return make_device(int(match.group(1)), int(match.group(2)))
    except (OverflowError, ValueError) as error:
        raise RecorderStorageFault("READY storage device identity is invalid") from error


def _video_profile(config: VideoConfig) -> VideoProfile:
    mismatches = [
        field
        for field in VideoConfig.__dataclass_fields__
        if getattr(config, field) != getattr(_EXPECTED_VIDEO, field)
    ]
    if mismatches:
        raise ProfileValidationError(
            "video configuration differs from the fixed production contract: "
            + ",".join(mismatches)
        )
    return VideoProfile()


def _anchor_policy(config: GpsConfig) -> AnchorPolicy:
    """Build the one GPS/overlay UTC policy from checked configuration."""

    earliest = datetime.fromisoformat(
        config.anchor_earliest_utc.removesuffix("Z") + "+00:00"
    ).astimezone(UTC)
    latest = datetime.fromisoformat(
        config.anchor_latest_utc.removesuffix("Z") + "+00:00"
    ).astimezone(UTC)
    return AnchorPolicy(
        earliest_utc=earliest,
        latest_utc=latest,
        max_uncertainty_ns=config.anchor_uncertainty_ms * 1_000_000,
        max_conflict_ns=config.anchor_max_conflict_ms * 1_000_000,
        max_reacquire_disagreement_ns=(config.anchor_max_reacquire_disagreement_ms * 1_000_000),
        max_anchor_interval_ns=config.anchor_max_interval_s * 1_000_000_000,
        gps_stale_after_ns=int(config.stale_after_s * 1_000_000_000),
    )


def _overlay_options(config: OverlayConfig) -> OverlayOptions:
    """Map checked product settings into the renderer-independent contract."""

    return OverlayOptions(
        show_local_datetime=config.show_local_datetime,
        show_utc_offset=config.show_utc_offset,
        show_rec=config.show_rec,
        show_speed=config.show_speed,
        speed_unit=config.speed_unit,
        show_coordinates=config.show_coordinates,
        coordinate_decimals=config.coordinate_decimals,
        show_altitude=config.show_altitude,
        show_satellites=config.show_satellites,
        show_hdop=config.show_hdop,
    )


class GStreamerRecorderRuntime:
    """Bind fresh READY storage evidence to one continuous backend session."""

    def __init__(
        self,
        *,
        config_path: Path,
        identity_path: Path,
        backend_factory: BackendFactory,
        audio_backend_factory: AudioBackendFactory | None = None,
        audio_discovery: AudioDiscoverer | None = None,
        preflight: LivePreflight = run_live_storage_preflight,
        boot_id_reader: Callable[[], str] = read_short_boot_id,
        sequence_planner: Callable[[Path, Path, str], int] = next_pending_sequence,
        boot_uuid_reader: Callable[[], UUID] = read_boot_uuid,
        finalizer_factory: FinalizerFactory | None = None,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        ownership: CameraOwnership | None = None,
        limits: RuntimeLimits | None = None,
        restart_policy: RestartPolicy | None = None,
        backoff_waiter: RuntimeBackoffWaiter = _wait_for_backoff,
        gps_service_factory: GpsServiceFactory | None = None,
        storage_space_monitor_factory: StorageSpaceMonitorFactory | None = None,
        control_endpoint_factory: RuntimeControlEndpointFactory | None = None,
    ) -> None:
        self._config_path = _absolute_posix(config_path, "config_path")
        self._identity_path = _absolute_posix(identity_path, "identity_path")
        self._backend_factory = backend_factory
        self._audio_backend_factory = audio_backend_factory
        self._audio_discovery = audio_discovery
        self._preflight = preflight
        self._boot_id_reader = boot_id_reader
        self._sequence_planner = sequence_planner
        self._boot_uuid_reader = boot_uuid_reader
        self._finalizer_factory = finalizer_factory
        self._monotonic_ns = monotonic_ns
        self._ownership = ownership or _PROCESS_CAMERA_OWNERSHIP
        self._limits = limits or RuntimeLimits()
        self._restart_policy = restart_policy or RestartPolicy()
        self._backoff_waiter = backoff_waiter
        self._gps_service_factory = gps_service_factory
        self._storage_space_monitor_factory = storage_space_monitor_factory
        self._control_endpoint_factory = control_endpoint_factory
        self._control_endpoint: RuntimeControlEndpoint | None = None
        self._storage_space_monitor: StorageSpaceMonitor | None = None
        self._storage_space_task: asyncio.Task[None] | None = None
        self._storage_space_stop = asyncio.Event()
        self._reconciliation_allowed = storage_space_monitor_factory is None
        self._gps_service: RuntimeGpsService | None = None
        self._gps_stop = asyncio.Event()
        self._gps_task: asyncio.Task[None] | None = None
        self._gps_task_error: str | None = None
        self._overlay_stop = asyncio.Event()
        self._overlay_task: asyncio.Task[None] | None = None
        self._overlay_task_error: str | None = None
        self._overlay_updates = 0
        self._lifecycle_observer: RuntimeLifecycleObserver | None = None
        self._checked_config: DashcamConfig | None = None
        self._preflight_result: PreflightResult | None = None
        self._backend: RuntimeBackend | None = None
        self._backend_stop = asyncio.Event()
        self._backend_run_task: asyncio.Task[None] | None = None
        self._fragment_drain_task: asyncio.Task[None] | None = None
        self._fragment_open_drain_task: asyncio.Task[None] | None = None
        self._stop_task: asyncio.Task[None] | None = None
        self._ownership_claimed = False
        self._start_attempted = False
        self._finalized_count = 0
        self._last_finalized: FinalizedFragment | None = None
        self._finalizer: RuntimeFinalizer | None = None
        self._boot_uuid: UUID | None = None
        self._boot_short_id: str | None = None
        self._pipeline_monotonic_offset_ns: int | None = None
        self._next_fragment_start_running_ns: int | None = None
        self._config: DashcamConfig | None = None
        self._effective_profile: VideoProfile | None = None
        self._last_clip_bitrate_bps: int | None = None
        self._last_clip_duration_ns: int | None = None
        self._last_clip_sequence: int | None = None
        self._last_clip_frames: dict[str, int | None] | None = None
        self._encoder_identity: EncoderIdentity | None = None
        self._effective_caps: EffectiveCaps | None = None
        self._counter_baseline: FrameCounters | None = None
        self._completed_raw_frames = 0
        self._completed_encoded_frames = 0
        self._counter_history_complete = True
        self._completed_dropped_frames = 0
        self._completed_drops_known = True
        self._completed_drop_source: str | None = None
        self._pipeline_restart_count = 0
        self._consecutive_restarts = 0
        self._finalizer_device_id: str | None = None
        self._drain_failure_observed = False
        self._audio_state = RuntimeAudioState.UNAVAILABLE
        self._audio_reason = "not_resolved"
        self._audio_detail: str | None = None
        self._audio_plan: AudioCapturePlan | None = None
        self._audio_selector: AlsaSelector | None = None
        self._matched_audio_plan: AudioCapturePlan | None = None
        self._effective_audio_caps: EffectiveAudioCaps | None = None
        self._audio_counter_baseline: int | None = None
        self._last_clip_audio_units: int | None = None
        self._completed_audio_units = 0
        self._audio_counter_history_complete = True
        self._audio_startup_fallback_used = False
        self._last_audio_restoration_failure: dict[str, object] | None = None
        self._next_allocation_floor = 0
        self._next_finalize_sequence: int | None = None
        self._reordered_finalized: dict[int, FinalizedFragment] = {}
        self._active_clip_lock = threading.Lock()
        self._active_clip_id: UUID | None = None
        self._opened_clip_identities: dict[int, tuple[UUID, str, int]] = {}
        self._opened_identity_changed = asyncio.Event()
        self._durable_mutation_lock = asyncio.Lock()
        self._metadata_reconciliations = 0
        self._metadata_reconciliation_failures = 0
        self._last_metadata_reconciliation_error: str | None = None
        self._metadata_reconciliation_task: asyncio.Task[None] | None = None
        self._metadata_reconciliation_stop = asyncio.Event()
        self._metadata_reconciliation_wakeup = asyncio.Event()
        self._metadata_reconciliation_pass_lock = asyncio.Lock()
        self._metadata_reconciliation_cursor_order = -1
        self._metadata_reconciliation_cursor_id: UUID | None = None
        self._metadata_reconciliation_hints: set[UUID] = set()
        self._metadata_reconciliation_retries: dict[UUID, int] = {}
        self._metadata_reconciliation_parked: dict[UUID, str] = {}
        self._metadata_reconciliation_overflows = 0

    @property
    def finalized_count(self) -> int:
        return self._finalized_count

    @property
    def last_finalized_fragment(self) -> FinalizedFragment | None:
        return self._last_finalized

    def current_clip_id(self) -> UUID | None:
        """Return only an identity whose WRITING row is already durable."""

        with self._active_clip_lock:
            return self._active_clip_id

    async def execute_control_intent(self, intent_id: UUID) -> None:
        """Run one catalog-prepared pair mutation through the recorder worker."""

        if not isinstance(intent_id, UUID):
            raise TypeError("intent_id must be a UUID")
        finalizer = self._finalizer
        if finalizer is None:
            raise RecorderFinalizationFault("recorder finalizer is unavailable")
        await self._durable_worker(
            finalizer.execute_intent,
            intent_id,
            deadline_detail="control pair mutation exceeded its deadline",
        )

    async def start_control_endpoint(
        self,
        status_provider: Callable[[], Mapping[str, object]],
        fault_callback: Callable[[str], None],
    ) -> None:
        """Start control only after startup reconciliation created the owned catalog."""

        if self._control_endpoint is not None:
            raise PipelineFault("control endpoint is already constructed")
        factory = self._control_endpoint_factory
        if factory is None:
            return
        finalizer = self._finalizer
        boot_uuid = self._boot_uuid
        if finalizer is None or boot_uuid is None:
            raise PipelineFault("control endpoint lacks reconciled runtime ownership")
        endpoint = factory(
            runtime=self,
            catalog=finalizer.control_catalog,
            config_path=Path(self._config_path),
            boot_id=str(boot_uuid),
            status_provider=status_provider,
            fault_callback=fault_callback,
        )
        self._control_endpoint = endpoint
        await endpoint.start()

    async def stop_control_endpoint(self) -> None:
        """Stop accepting and drain clients before runtime catalog shutdown."""

        endpoint = self._control_endpoint
        if endpoint is None:
            return
        self._control_endpoint = None
        await endpoint.stop()

    def control_endpoint_snapshot(self) -> dict[str, object] | None:
        endpoint = self._control_endpoint
        return None if endpoint is None else endpoint.snapshot()

    async def trigger_control_event(
        self,
        source: EventSource,
        monotonic_now_ns: int,
        previous_count: int,
        next_count: int,
        event_id: UUID,
    ) -> EventProtectionResult:
        """Linearize event protection with fragment close and pair mutation."""

        finalizer = self._finalizer
        if finalizer is None:
            raise RecorderFinalizationFault("recorder finalizer is unavailable")
        async with self._durable_mutation_lock:
            with self._active_clip_lock:
                current_clip_id = self._active_clip_id
            value = await self._run_durable_worker(
                finalizer.trigger_event,
                current_clip_id,
                source=source,
                monotonic_now_ns=monotonic_now_ns,
                previous_count=previous_count,
                next_count=next_count,
                event_id=event_id,
                deadline_detail="event protection transaction exceeded its deadline",
            )
        if not isinstance(value, EventProtectionResult):
            raise PipelineFault("event protection returned invalid durable evidence")
        return value

    def bind_lifecycle_observer(self, observer: RuntimeLifecycleObserver) -> None:
        """Bind the daemon's O(1) event sink before runtime startup."""

        if self._start_attempted or self._lifecycle_observer is not None:
            raise PipelineContractError("runtime lifecycle observer is already bound")
        if not callable(observer):
            raise TypeError("runtime lifecycle observer must be callable")
        self._lifecycle_observer = observer

    def _emit_lifecycle(
        self,
        event: RuntimeLifecycleEvent,
        *,
        cause: BaseException | None = None,
    ) -> None:
        observer = self._lifecycle_observer
        if observer is None:
            return
        try:
            observer(event)
        except BaseException as error:
            fault = RuntimeObserverFault(event, error)
            if cause is not None:
                raise fault from cause
            raise fault from error

    def runtime_snapshot(self) -> dict[str, object]:
        """Return only measured/bound runtime facts; unavailable metrics are null."""

        self._observe_backend_audio_state()
        preflight = self._preflight_result
        facts = None if preflight is None else preflight.facts
        profile = self._effective_profile
        caps = self._effective_caps
        config = self._config
        counters = self._cumulative_counters()
        audio_counters = self._cumulative_audio_counters()
        audio_plan = self._matched_audio_plan
        effective_audio = self._effective_audio_caps
        backend = self._backend
        restoration = getattr(backend, "audio_restoration_snapshot", None)
        restoration_snapshot = restoration if isinstance(restoration, dict) else None
        overlay_renderer: dict[str, object] | None = None
        overlay_snapshot_error: str | None = None
        inspect_overlay = getattr(backend, "overlay_snapshot", None)
        if callable(inspect_overlay):
            try:
                observed_overlay = inspect_overlay()
                if isinstance(observed_overlay, dict):
                    overlay_renderer = observed_overlay
                else:
                    overlay_snapshot_error = "overlay renderer returned an invalid snapshot"
            except BaseException as error:
                overlay_snapshot_error = _bounded_exception_detail(error)
        renderer_faulted = overlay_snapshot_error is not None or (
            overlay_renderer is not None and overlay_renderer.get("state") == "ISOLATED"
        )
        return {
            "video": None
            if profile is None
            else {
                "width": profile.width,
                "height": profile.height,
                "frames_per_second": profile.frames_per_second,
                "codec": profile.codec,
                "hardware_encoded": profile.hardware_encoded,
                "effective_caps": None
                if caps is None
                else {
                    "raw_format": caps.raw_format,
                    "fps_numerator": caps.frames_per_second_numerator,
                    "fps_denominator": caps.frames_per_second_denominator,
                    "h264_profile": caps.profile,
                    "h264_level": caps.level,
                },
                "configured": None
                if config is None
                else {
                    "target_bitrate_bps": config.video.bitrate_bps,
                    "keyframe_interval_frames": config.video.keyframe_interval_frames,
                },
                "encoder_identity": None
                if self._encoder_identity is None
                else {
                    "factory_name": self._encoder_identity.factory_name,
                    "factory_class": self._encoder_identity.factory_class,
                    "device_path": self._encoder_identity.device_path,
                },
            },
            "frames": None
            if counters is None
            else {
                "raw": counters.raw_frames,
                "encoded": counters.encoded_access_units,
                "dropped": counters.dropped_frames,
                "drop_source": counters.drop_source,
            },
            "audio": {
                "state": self._audio_state.value,
                "reason": self._audio_reason,
                "detail": self._audio_detail,
                "configured": None
                if config is None
                else {
                    "enabled": config.audio.enabled,
                    "sample_rate_hz": config.audio.sample_rate_hz,
                    "channels": config.audio.channels,
                    "codec": config.audio.codec,
                    "target_bitrate_bps": config.audio.bitrate_bps,
                },
                "matched": None
                if audio_plan is None
                else {
                    "vendor_id": audio_plan.identity.vendor_id,
                    "product_id": audio_plan.identity.product_id,
                    "product": audio_plan.identity.product,
                    "physical_path": audio_plan.identity.physical_path,
                    "alsa_card_id": audio_plan.identity.alsa_card_id,
                },
                "effective": None
                if effective_audio is None
                else {
                    "raw_format": effective_audio.raw_format,
                    "sample_rate_hz": effective_audio.sample_rate_hz,
                    "channels": effective_audio.channels,
                    "codec": effective_audio.codec,
                    "mpeg_version": effective_audio.mpeg_version,
                    "stream_format": effective_audio.stream_format,
                    "encoder_factory": effective_audio.encoder_factory,
                    "parser_factory": effective_audio.parser_factory,
                    "bitrate_bps": effective_audio.bitrate_bps,
                },
                "encoded_access_units": None
                if audio_counters is None
                else audio_counters.encoded_access_units,
                "last_clip_encoded_access_units": self._last_clip_audio_units,
                "startup_video_only_fallback_used": self._audio_startup_fallback_used,
                "loss_isolated_without_video_restart": (
                    self._audio_reason == "microphone_loss_isolated"
                ),
                "restoration": restoration_snapshot,
                "last_restoration_failure": self._last_audio_restoration_failure,
            },
            "overlay": {
                "enabled": False if config is None else config.overlay.enabled,
                "state": (
                    "DISABLED"
                    if config is not None and not config.overlay.enabled
                    else "FAULTED"
                    if self._overlay_task_error is not None or renderer_faulted
                    else "ACTIVE"
                    if self._overlay_task is not None
                    else "INACTIVE"
                ),
                "updates": self._overlay_updates,
                "last_error": (
                    self._overlay_task_error
                    or overlay_snapshot_error
                    or (
                        None
                        if overlay_renderer is None
                        else cast(str | None, overlay_renderer.get("last_error"))
                    )
                ),
                "renderer": overlay_renderer,
            },
            "gps": self._gps_runtime_snapshot(),
            "metadata_reconciliation": {
                "completed": self._metadata_reconciliations,
                "failures": self._metadata_reconciliation_failures,
                "last_error": self._last_metadata_reconciliation_error,
                "backlog": len(self._metadata_reconciliation_hints),
                "overflows": self._metadata_reconciliation_overflows,
                "retrying": len(self._metadata_reconciliation_retries),
                "parked": len(self._metadata_reconciliation_parked),
            },
            "pipeline_restart_count": self._pipeline_restart_count,
            "last_clip": {
                "sequence": self._last_clip_sequence,
                "duration_ns": self._last_clip_duration_ns,
                "bitrate_bps": self._last_clip_bitrate_bps,
                "frames": self._last_clip_frames,
            },
            "storage_retention": (
                None
                if self._storage_space_monitor is None
                else self._storage_space_monitor.snapshot.as_dict()
            ),
            "control_endpoint": self.control_endpoint_snapshot(),
            "storage_preflight": None
            if preflight is None
            else {
                "state": preflight.state.value,
                "reasons": [reason.value for reason in preflight.reasons],
                "ready": preflight.ready,
                "mount": None
                if facts is None
                else {
                    "target": facts.mount.target,
                    "mounted": facts.mount.mounted,
                    "filesystem": facts.mount.filesystem,
                    "label": facts.mount.label,
                    "uuid_suffix": None if facts.mount.uuid is None else facts.mount.uuid[-4:],
                    "device_id": facts.mount.device_id,
                    "read_write": (
                        "rw" in facts.mount.mount_options and "ro" not in facts.mount.mount_options
                    ),
                },
                "free_bytes": None if facts is None else facts.space.free_bytes,
                "capacity_bytes": None if facts is None else facts.space.capacity_bytes,
            },
        }

    def _gps_runtime_snapshot(self) -> dict[str, object]:
        """Publish bounded GPS supervision facts without exposing coordinates."""

        config = self._config
        service = self._gps_service
        observed = None if service is None else service.snapshot
        counters = GpsCounters() if observed is None else observed.counters
        latest = None if observed is None else observed.latest_sentence
        supervisor_faulted = self._gps_task_error is not None
        navigation = (
            None
            if (
                observed is None
                or supervisor_faulted
                or observed.state is not GpsState.NAVIGATION_VALID
                or observed.navigation is None
                or not observed.navigation.navigation_valid
            )
            else observed.navigation
        )
        gps_time_state = GpsTimeState.UNSYNCED if observed is None else observed.gps_time_state
        if supervisor_faulted and observed is not None:
            gps_time_state = (
                GpsTimeState.GPS_TIME_STALE
                if observed.time_anchor is not None
                else GpsTimeState.UNSYNCED
            )
        return {
            "configured": None
            if config is None
            else {
                "device": config.gps.device,
                "baud": config.gps.baud,
                "stale_after_s": config.gps.stale_after_s,
                "max_sample_hz": config.gps.max_sample_hz,
                "anchor_earliest_utc": config.gps.anchor_earliest_utc,
                "anchor_latest_utc": config.gps.anchor_latest_utc,
                "anchor_uncertainty_ms": config.gps.anchor_uncertainty_ms,
                "anchor_max_conflict_ms": config.gps.anchor_max_conflict_ms,
                "anchor_max_reacquire_disagreement_ms": (
                    config.gps.anchor_max_reacquire_disagreement_ms
                ),
                "anchor_max_interval_s": config.gps.anchor_max_interval_s,
            },
            "state": (
                "FAULTED"
                if supervisor_faulted
                else "UART_UNAVAILABLE"
                if observed is None
                else observed.state.value
            ),
            "connected": False if observed is None or supervisor_faulted else observed.connected,
            "buffered_bytes": 0 if observed is None else observed.buffered_bytes,
            "discarding_oversized_line": (
                False if observed is None else observed.discarding_oversized_line
            ),
            "last_error": (
                None
                if observed is None or observed.last_error is None
                else observed.last_error.value
            ),
            "last_error_detail": None if observed is None else observed.last_error_detail,
            "supervisor_error": self._gps_task_error,
            "last_parse_error": (
                None
                if observed is None or observed.last_parse_error is None
                else observed.last_parse_error.value
            ),
            "time": None
            if observed is None
            else {
                "state": gps_time_state.value,
                "anchor": None
                if observed.time_anchor is None
                else {
                    "monotonic_ns": observed.time_anchor.monotonic_ns,
                    "utc": observed.time_anchor.utc.isoformat(timespec="milliseconds").replace(
                        "+00:00", "Z"
                    ),
                    "source": observed.time_anchor.source.value,
                    "provenance": observed.time_anchor.provenance,
                    "uncertainty_ns": observed.time_anchor.uncertainty_ns,
                },
                "last_status": (
                    None
                    if observed.last_anchor_status is None
                    else observed.last_anchor_status.value
                ),
                "last_error": observed.last_anchor_error,
                "last_disagreement_ns": observed.last_anchor_disagreement_ns,
            },
            "latest_sentence": None
            if latest is None
            else {
                "talker": latest.talker,
                "type": latest.sentence_type.value,
                "received_monotonic_ns": latest.received_monotonic_ns,
                "time_trust": latest.time_trust.value,
                "time_anchor_candidate": latest.time_anchor_candidate,
                "navigation_valid": latest.navigation_valid,
            },
            "navigation": None
            if navigation is None
            else {
                "received_monotonic_ns": navigation.received_monotonic_ns,
                "sentence_type": navigation.sentence_type.value,
                "fix_quality": navigation.fix_quality,
                "satellites": navigation.satellites,
                "hdop": navigation.hdop,
            },
            "counters": {
                "connection_attempts": counters.connection_attempts,
                "connections": counters.connections,
                "reconnects": counters.reconnects,
                "disconnects": counters.disconnects,
                "read_timeouts": counters.read_timeouts,
                "bytes_received": counters.bytes_received,
                "lines_received": counters.lines_received,
                "valid_sentences": counters.valid_sentences,
                "parse_errors": counters.parse_errors,
                "checksum_failures": counters.checksum_failures,
                "unsupported_sentences": counters.unsupported_sentences,
                "oversized_lines": counters.oversized_lines,
                "transport_errors": counters.transport_errors,
                "valid_fixes": counters.valid_fixes,
                "stale_transitions": counters.stale_transitions,
                "anchor_attempts": counters.anchor_attempts,
                "anchor_acceptances": counters.anchor_acceptances,
                "anchor_confirmations": counters.anchor_confirmations,
                "anchor_reacquisitions": counters.anchor_reacquisitions,
                "anchor_idempotent": counters.anchor_idempotent,
                "anchor_rejections": counters.anchor_rejections,
            },
            "telemetry": {
                "sentences_considered": (
                    0 if observed is None else observed.telemetry_counters.sentences_considered
                ),
                "navigation_observations": (
                    0 if observed is None else observed.telemetry_counters.navigation_observations
                ),
                "invalid_navigation": (
                    0 if observed is None else observed.telemetry_counters.invalid_navigation
                ),
                "ignored_sentences": (
                    0 if observed is None else observed.telemetry_counters.ignored_sentences
                ),
                "samples_emitted": (
                    0 if observed is None else observed.telemetry_counters.samples_emitted
                ),
                "samples_coalesced": (
                    0 if observed is None else observed.telemetry_counters.samples_coalesced
                ),
                "samples_rate_limited": (
                    0 if observed is None else observed.telemetry_counters.samples_rate_limited
                ),
                "samples_evicted": (
                    0 if observed is None else observed.telemetry_counters.samples_evicted
                ),
                "monotonic_regressions": (
                    0 if observed is None else observed.telemetry_counters.monotonic_regressions
                ),
                "source_time_regressions": (
                    0 if observed is None else observed.telemetry_counters.source_time_regressions
                ),
                "omitted_out_of_range_fields": (
                    0
                    if observed is None
                    else observed.telemetry_counters.omitted_out_of_range_fields
                ),
                "retained_samples": (
                    0 if observed is None else observed.telemetry_counters.retained_samples
                ),
            },
        }

    def _gps_summary_for_interval(
        self,
        start_monotonic_ns: int,
        end_monotonic_ns: int,
    ) -> tuple[GpsSummary, tuple[str, ...]]:
        """Snapshot one clip's half-open GPS window without risking finalization."""

        service = self._gps_service
        if service is None:
            return GpsSummary(False, None), ()
        try:
            window = service.telemetry_window(
                start_monotonic_ns,
                end_monotonic_ns,
                max_samples=MAX_GPS_SAMPLES,
            )
        except Exception as error:
            return (
                GpsSummary(False, None),
                (f"GPS telemetry snapshot unavailable: {_bounded_exception_detail(error)}",),
            )
        if not isinstance(window, GpsTelemetryWindow):
            return (
                GpsSummary(False, None),
                ("GPS telemetry snapshot returned an invalid result",),
            )
        samples = tuple(
            GpsSample(
                monotonic_ns=sample.monotonic_ns,
                utc=None,
                timestamp_quality=TimestampQuality.MONOTONIC_ONLY,
                lat_deg=sample.latitude_deg,
                lon_deg=sample.longitude_deg,
                speed_mps=sample.speed_mps,
                course_deg=sample.course_deg,
                altitude_m=sample.altitude_m,
                fix_quality=sample.fix_quality,
                satellites=sample.satellites,
                hdop=sample.hdop,
            )
            for sample in window.samples
        )
        warnings = (
            ()
            if window.complete
            else (
                "GPS telemetry window incomplete: "
                + ",".join(issue.value for issue in window.issues),
            )
        )
        return GpsSummary(bool(samples), None, samples), warnings

    def _project_close_time_anchor(self, sidecar: ClipSidecar) -> ClipSidecar:
        """Best-effort close-time projection through the shared strict model."""

        service = self._gps_service
        if service is None:
            return sidecar
        try:
            snapshot = service.snapshot
            if not isinstance(snapshot, GpsSnapshot) or snapshot.time_anchor is None:
                return sidecar
            accepted = snapshot.time_anchor
            anchor = TimeAnchor(
                source=TimeAnchorSource.GPS,
                monotonic_ns=accepted.monotonic_ns,
                utc=accepted.utc,
                uncertainty_ns=accepted.uncertainty_ns,
                provenance=accepted.provenance,
            )
            return project_anchored_sidecar(
                sidecar,
                anchor=anchor,
                gps_time_state=snapshot.gps_time_state,
                system_clock_state=SystemClockState.UNSET,
            )
        except Exception:
            # GPS metadata is optional to media durability.  Any invalid or
            # unprojectable observation retains the established provisional
            # path so a later accepted anchor can reconcile it recoverably.
            return sidecar

    def _gps_task_finished(self, task: asyncio.Task[None]) -> None:
        """Consume optional-task completion without perturbing media supervision."""

        if task.cancelled():
            if not self._gps_stop.is_set():
                self._gps_task_error = "GPS supervisor was cancelled unexpectedly"
            return
        try:
            error = task.exception()
        except BaseException as error:
            self._gps_task_error = _bounded_exception_detail(error)
            return
        if error is not None:
            self._gps_task_error = _bounded_exception_detail(error)
        elif not self._gps_stop.is_set():
            self._gps_task_error = "GPS supervisor exited unexpectedly"

    def _start_gps(self, config: GpsConfig) -> None:
        """Start optional GPS after recording is active; failures stay observable."""

        factory = self._gps_service_factory
        if factory is None:
            return
        try:
            service = factory(config)
            snapshot = service.snapshot
            if not isinstance(snapshot, GpsSnapshot):
                raise TypeError("GPS service returned an invalid initial snapshot")
            task = asyncio.create_task(
                service.run(self._gps_stop),
                name="recorder-gps-service",
            )
        except BaseException as error:
            self._gps_task_error = _bounded_exception_detail(error)
            return
        self._gps_service = service
        self._gps_task = task
        task.add_done_callback(self._gps_task_finished)

    async def _stop_gps(self) -> None:
        """Stop optional GPS within the media runtime's existing task bound."""

        self._gps_stop.set()
        task = self._gps_task
        if task is None:
            return
        done, _ = await asyncio.wait(
            {task},
            timeout=self._limits.task_stop_timeout_s,
        )
        if task not in done:
            self._gps_task_error = "GPS supervisor did not stop within deadline"
            task.cancel()
            cancelled, _ = await asyncio.wait(
                {task},
                timeout=self._limits.task_stop_timeout_s,
            )
            if task not in cancelled:
                self._gps_task_error = "GPS supervisor ignored cancellation after its stop deadline"
                return
        self._gps_task_finished(task)
        self._gps_task = None

    def _current_overlay_text(self) -> str:
        """Render one coherent immutable GPS/time snapshot for the live frame."""

        config = self._config
        if config is None:
            raise PipelineContractError("overlay renderer lacks its bound configuration")
        service = self._gps_service
        observed = None if service is None else service.snapshot
        if observed is not None and not isinstance(observed, GpsSnapshot):
            raise TypeError("GPS service returned an invalid overlay snapshot")

        supervisor_faulted = self._gps_task_error is not None
        now_ns = self._monotonic_ns()
        gps_state = (
            GpsState.UART_UNAVAILABLE
            if observed is None
            else (
                GpsState.STALE
                if supervisor_faulted and observed.time_anchor is not None
                else GpsState.UART_UNAVAILABLE
                if supervisor_faulted
                else observed.state
            )
        )
        gps_time_state = (
            GpsTimeState.UNSYNCED
            if observed is None
            else (
                GpsTimeState.GPS_TIME_STALE
                if supervisor_faulted and observed.time_anchor is not None
                else GpsTimeState.UNSYNCED
                if supervisor_faulted
                else observed.gps_time_state
            )
        )
        navigation = (
            None
            if (
                observed is None
                or supervisor_faulted
                or observed.state is not GpsState.NAVIGATION_VALID
                or observed.navigation is None
                or not observed.navigation.navigation_valid
            )
            else observed.navigation
        )

        local_time = None
        timestamp_quality = TimestampQuality.MONOTONIC_ONLY
        anchor = None if observed is None else observed.time_anchor
        if anchor is not None:
            conversion = MonotonicUtcClock(
                anchor=anchor,
                latest_confirmation=anchor,
            ).convert(now_ns, _anchor_policy(config.gps))
            if conversion.ok and conversion.estimate is not None:
                local = to_local_time(
                    conversion.estimate.utc,
                    config.time.timezone,
                )
                if local.ok and local.local is not None:
                    local_time = local.local
                    timestamp_quality = conversion.estimate.quality

        speed_mps = (
            None
            if navigation is None or navigation.speed_knots is None
            else navigation.speed_knots * _METRES_PER_SECOND_PER_KNOT
        )
        frame = build_overlay(
            OverlayTelemetry(
                gps_time_state=gps_time_state,
                timestamp_quality=timestamp_quality,
                gps_state=gps_state,
                local_time=local_time,
                latitude_deg=None if navigation is None else navigation.latitude_deg,
                longitude_deg=None if navigation is None else navigation.longitude_deg,
                speed_mps=speed_mps,
                altitude_m=None if navigation is None else navigation.altitude_m,
                satellites=None if navigation is None else navigation.satellites,
                hdop=None if navigation is None else navigation.hdop,
            ),
            _overlay_options(config.overlay),
        )
        return f"{frame.top_line}\n{frame.bottom_line}"

    async def _overlay_loop(self) -> None:
        """Update changed text only; one failed backend waits for replacement."""

        last_backend: RuntimeBackend | None = None
        last_text: str | None = None
        failed_backend: RuntimeBackend | None = None
        while not self._overlay_stop.is_set():
            backend = self._backend
            if backend is not None and backend is not failed_backend:
                update = getattr(backend, "set_overlay_text", None)
                try:
                    if not callable(update):
                        raise PipelineContractError(
                            "active backend lacks the burned-overlay update seam"
                        )
                    text = self._current_overlay_text()
                    if backend is not last_backend or text != last_text:
                        await update(text)
                        last_backend = backend
                        last_text = text
                        self._overlay_updates += 1
                        self._overlay_task_error = None
                except asyncio.CancelledError:
                    raise
                except BaseException as error:
                    self._overlay_task_error = _bounded_exception_detail(error)
                    failed_backend = backend
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    self._overlay_stop.wait(),
                    timeout=self._limits.overlay_update_interval_s,
                )

    def _start_overlay(self, config: OverlayConfig) -> None:
        """Start one optional queue-free updater for the active recording graph."""

        if not config.enabled or self._overlay_task is not None:
            return
        self._overlay_task = asyncio.create_task(
            self._overlay_loop(),
            name="recorder-overlay",
        )

    async def _stop_overlay(self) -> None:
        """Join the optional updater within the common worker deadline."""

        self._overlay_stop.set()
        task = self._overlay_task
        if task is None:
            return
        done, _ = await asyncio.wait(
            {task},
            timeout=self._limits.task_stop_timeout_s,
        )
        if task not in done:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        else:
            try:
                task.result()
            except BaseException as error:
                self._overlay_task_error = _bounded_exception_detail(error)
        self._overlay_task = None

    def recording_progress_token(self) -> int | None:
        """Return cumulative encoded-frame progress for watchdog supervision."""

        counters = self._cumulative_counters()
        return None if counters is None else counters.encoded_access_units

    def _attempt_counters(self) -> FrameCounters | None:
        backend = self._backend
        observed = getattr(backend, "frame_counters", None)
        value = observed() if callable(observed) else None
        return value if isinstance(value, FrameCounters) else None

    def _attempt_audio_counters(self) -> AudioCounters | None:
        backend = self._backend
        observed = getattr(backend, "audio_counters", None)
        value = observed() if callable(observed) else None
        return value if isinstance(value, AudioCounters) else None

    def _cumulative_audio_counters(self) -> AudioCounters | None:
        current = self._attempt_audio_counters()
        if not self._audio_counter_history_complete:
            return None
        if self._backend is not None and self._effective_audio_caps is not None and current is None:
            return None
        if current is None and self._completed_audio_units == 0:
            return None
        return AudioCounters(
            self._completed_audio_units + (0 if current is None else current.encoded_access_units)
        )

    def _cumulative_counters(self) -> FrameCounters | None:
        current = self._attempt_counters()
        if not self._counter_history_complete:
            return None
        if self._backend is not None and current is None:
            return None
        if current is None and self._completed_raw_frames == self._completed_encoded_frames == 0:
            return None
        raw = self._completed_raw_frames
        encoded = self._completed_encoded_frames
        dropped = self._completed_dropped_frames
        drops_known = self._completed_drops_known
        drop_source = self._completed_drop_source
        if current is not None:
            raw += current.raw_frames
            encoded += current.encoded_access_units
            if current.dropped_frames is None:
                drops_known = False
            else:
                dropped += current.dropped_frames
                if drop_source is None:
                    drop_source = current.drop_source
                elif current.drop_source is not None and current.drop_source != drop_source:
                    drop_source = "mixed"
        return FrameCounters(
            raw,
            encoded,
            dropped if drops_known else None,
            drop_source if drops_known else None,
        )

    def _roll_up_attempt_counters(self) -> None:
        counters = self._attempt_counters()
        if counters is None:
            self._counter_history_complete = False
            return
        self._completed_raw_frames += counters.raw_frames
        self._completed_encoded_frames += counters.encoded_access_units
        if counters.dropped_frames is None:
            self._completed_drops_known = False
        else:
            self._completed_dropped_frames += counters.dropped_frames
            if self._completed_drop_source is None:
                self._completed_drop_source = counters.drop_source
            elif (
                counters.drop_source is not None
                and counters.drop_source != self._completed_drop_source
            ):
                self._completed_drop_source = "mixed"
        audio_counters = self._attempt_audio_counters()
        if self._effective_audio_caps is not None:
            if audio_counters is None:
                self._audio_counter_history_complete = False
            else:
                self._completed_audio_units += audio_counters.encoded_access_units

    def _observe_backend_audio_state(self) -> None:
        """Import backend-proven loss/restoration without inferring from closures."""

        backend = self._backend
        if backend is None:
            return
        if getattr(backend, "audio_loss_isolated", False) is True:
            self._audio_state = RuntimeAudioState.UNAVAILABLE
            self._audio_reason = "microphone_loss_isolated"
            self._audio_detail = None
            self._effective_audio_caps = None
            return
        restoration = getattr(backend, "audio_restoration_snapshot", None)
        caps = getattr(backend, "effective_audio_caps", None)
        current_plan = getattr(backend, "audio_capture_plan", None)
        if (
            isinstance(restoration, dict)
            and isinstance(restoration.get("restoration_count"), int)
            and restoration["restoration_count"] > 0
            and isinstance(caps, EffectiveAudioCaps)
            and isinstance(current_plan, AudioCapturePlan)
        ):
            self._audio_state = RuntimeAudioState.MATCHED
            self._audio_reason = "microphone_restored"
            self._audio_detail = None
            self._effective_audio_caps = caps
            self._audio_plan = current_plan
            self._matched_audio_plan = current_plan

    def _capture_audio_restoration_failure(self) -> None:
        """Persist one backend-proven bounded failure across backend replacement."""

        backend = self._backend
        restoration = getattr(backend, "audio_restoration_snapshot", None)
        if not isinstance(restoration, dict):
            return
        failure = restoration.get("last_failure")
        if not isinstance(failure, dict):
            return
        critical = failure.get("critical")
        phase = failure.get("phase")
        detail = failure.get("detail")
        monotonic_ns = failure.get("monotonic_ns")
        if (
            critical is not True
            or not isinstance(phase, str)
            or not 0 < len(phase) <= 64
            or not phase.isprintable()
            or not isinstance(detail, str)
            or not 0 < len(detail) <= 512
            or not detail.isprintable()
            or isinstance(monotonic_ns, bool)
            or not isinstance(monotonic_ns, int)
            or monotonic_ns < 0
        ):
            return
        self._last_audio_restoration_failure = {
            "critical": True,
            "phase": phase,
            "detail": detail,
            "monotonic_ns": monotonic_ns,
        }

    async def _resolve_audio(self, config: AudioConfig) -> AudioCapturePlan | None:
        """Resolve optional audio before camera ownership or graph construction."""

        self._audio_plan = None
        self._audio_selector = None
        self._matched_audio_plan = None
        self._effective_audio_caps = None
        if not config.enabled:
            self._audio_state = RuntimeAudioState.DISABLED
            self._audio_reason = "disabled_by_config"
            self._audio_detail = None
            return None
        expected = AudioConfig()
        if (
            config.sample_rate_hz != expected.sample_rate_hz
            or config.channels != expected.channels
            or config.codec != expected.codec
            or config.bitrate_bps != expected.bitrate_bps
        ):
            self._audio_state = RuntimeAudioState.FAULTED
            self._audio_reason = "invalid_config"
            self._audio_detail = "audio configuration differs from production"
            return None
        try:
            selector = parse_alsa_selector(config.device_match)
        except AlsaMatchError as error:
            self._audio_state = RuntimeAudioState.FAULTED
            self._audio_reason = "invalid_selector"
            self._audio_detail = _bounded_exception_detail(error)
            return None
        self._audio_selector = selector
        discover = self._audio_discovery
        if discover is None:
            self._audio_state = RuntimeAudioState.UNAVAILABLE
            self._audio_reason = "discovery_not_configured"
            self._audio_detail = None
            return None
        try:
            outcome = await asyncio.to_thread(discover, selector)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._audio_state = RuntimeAudioState.FAULTED
            self._audio_reason = "discovery_exception"
            self._audio_detail = _bounded_exception_detail(error)
            return None
        if not isinstance(outcome, AudioDiscoveryOutcome):
            self._audio_state = RuntimeAudioState.FAULTED
            self._audio_reason = "invalid_discovery_result"
            self._audio_detail = None
            return None
        if outcome.status is AudioDiscoveryStatus.NOT_FOUND:
            self._audio_state = RuntimeAudioState.UNAVAILABLE
            self._audio_reason = "not_found"
            self._audio_detail = None
            return None
        if outcome.status is not AudioDiscoveryStatus.MATCHED or outcome.device is None:
            self._audio_state = RuntimeAudioState.FAULTED
            self._audio_reason = outcome.status.value.casefold()
            self._audio_detail = None
            return None
        try:
            plan = AudioCapturePlan.from_match(outcome.device, config)
        except ValueError as error:
            self._audio_state = RuntimeAudioState.FAULTED
            self._audio_reason = "invalid_match"
            self._audio_detail = _bounded_exception_detail(error)
            return None
        if self._audio_backend_factory is None:
            self._audio_state = RuntimeAudioState.FAULTED
            self._audio_reason = "audio_backend_unavailable"
            self._audio_detail = None
            return None
        self._audio_plan = plan
        self._matched_audio_plan = plan
        self._audio_state = RuntimeAudioState.MATCHED
        self._audio_reason = "stable_identity_match"
        self._audio_detail = None
        return plan

    async def check(self, config: DashcamConfig) -> PreflightResult:
        """Run and bind exactly one fresh production storage preflight."""

        if self._checked_config is not None:
            raise PipelineContractError("storage gate is single-use")
        try:
            result = await asyncio.to_thread(
                self._preflight,
                config,
                identity_path=self._identity_path,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise RecorderStorageFault("storage preflight failed") from error
        if not isinstance(result, PreflightResult):
            raise PipelineContractError("storage preflight returned an invalid result")
        self._checked_config = config
        self._preflight_result = result
        return result

    async def _initialize_storage_space_monitor(
        self,
        config: DashcamConfig,
        finalizer: RuntimeFinalizer,
    ) -> None:
        factory = self._storage_space_monitor_factory
        if factory is None:
            return
        result = self._preflight_result
        facts = None if result is None else result.facts
        if facts is None:
            raise RecorderStorageFault("READY storage evidence lacks live space facts")
        device_id = facts.mount.device_id
        volume_uuid = facts.mount.uuid
        capacity_bytes = facts.space.capacity_bytes
        if device_id is None or volume_uuid is None or capacity_bytes is None:
            raise RecorderStorageFault("READY storage evidence lacks retention identity")
        try:
            monitor = factory(
                storage=config.storage,
                volume_uuid=volume_uuid,
                expected_device_id=device_id,
                expected_capacity_bytes=capacity_bytes,
                latch_store=finalizer,
            )
            status = monitor.snapshot
            for _ in range(monitor.maximum_observation_failures):
                status = await asyncio.to_thread(monitor.observe)
                if status.stop_required or not status.stale:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise RecorderStorageFault("storage threshold monitor failed to initialize") from error
        self._storage_space_monitor = monitor
        if status.stop_required or status.stale:
            raise StorageSafetyStop(_storage_space_detail(status))
        self._reconciliation_allowed = True

    async def _fresh_storage_observation(self) -> StorageSpaceSnapshot:
        monitor = self._storage_space_monitor
        if monitor is None:
            raise RecorderStorageFault("storage threshold monitor is unavailable")
        status = monitor.snapshot
        for _ in range(monitor.maximum_observation_failures):
            status = await asyncio.to_thread(monitor.observe)
            if status.stop_required or not status.stale:
                break
        if status.stop_required or status.stale:
            raise StorageSafetyStop(_storage_space_detail(status))
        return status

    async def _run_storage_reclamation(
        self, *, maximum_steps: int
    ) -> tuple[StorageSpaceSnapshot, int, bool]:
        """Complete committed deletes and reclaim one pair per fresh observation."""

        monitor = self._storage_space_monitor
        finalizer = self._finalizer
        boot_uuid = self._boot_uuid
        if monitor is None or finalizer is None or boot_uuid is None:
            raise RecorderStorageFault("storage reclaimer lacks its verified runtime binding")
        status = monitor.snapshot
        steps_used = 0
        pending_delete_remaining = False
        for _ in range(maximum_steps):
            allow_new = status.directive is not None
            try:
                value = await self._durable_worker(
                    finalizer.reclaim_storage_once,
                    boot_id=str(boot_uuid),
                    allow_new=allow_new,
                    deadline_detail="storage reclamation exceeded its deadline",
                )
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                raise StorageSafetyStop(
                    "storage reclamation failed: " + _bounded_exception_detail(error)
                ) from error
            if not isinstance(value, ReclamationStep):
                raise StorageSafetyStop("storage reclaimer returned an invalid result")
            if not value.eligible_found:
                if status.mode is RetentionMode.EMERGENCY:
                    raise StorageSafetyStop(
                        "emergency storage reclamation found no eligible managed clip"
                    )
                return status, steps_used, False
            if not value.deleted:
                raise StorageSafetyStop("storage reclaimer made no durable deletion progress")
            steps_used += 1
            pending_delete_remaining = value.pending_delete_remaining
            status = await self._fresh_storage_observation()
        return status, steps_used, pending_delete_remaining

    async def _storage_space_loop(self) -> None:
        monitor = self._storage_space_monitor
        if monitor is None:
            return
        while not self._storage_space_stop.is_set():
            try:
                await asyncio.wait_for(
                    self._storage_space_stop.wait(),
                    timeout=self._limits.storage_observation_interval_s,
                )
            except TimeoutError:
                status = await self._fresh_storage_observation()
                if status.stop_required:
                    raise StorageSafetyStop(_storage_space_detail(status)) from None
                await self._run_storage_reclamation(
                    maximum_steps=self._limits.max_reclamation_steps_per_pass
                )

    def _start_storage_space_monitor(self) -> None:
        if self._storage_space_monitor is None or self._storage_space_task is not None:
            return
        self._storage_space_stop.clear()
        self._storage_space_task = asyncio.create_task(
            self._storage_space_loop(),
            name="recorder-storage-space-monitor",
        )

    async def _stop_storage_space_monitor(self) -> None:
        self._storage_space_stop.set()
        task = self._storage_space_task
        if task is None:
            return
        if not task.done():
            await asyncio.gather(task, return_exceptions=True)
        elif not task.cancelled():
            task.exception()
        self._storage_space_task = None

    async def _escalate_no_space(self, error: BaseException) -> bool:
        monitor = self._storage_space_monitor
        if monitor is None:
            return False
        if isinstance(error, RecordingStorageNoSpaceError):
            await asyncio.to_thread(monitor.note_no_space_write)
            return True
        return await asyncio.to_thread(monitor.note_write_error, error)

    async def _register_opened_fragment(self, opened: OpenedFragment) -> None:
        finalizer = self._finalizer
        offset = self._pipeline_monotonic_offset_ns
        if finalizer is None or offset is None:
            raise PipelineFault("fragment open lacks its catalog or monotonic binding")
        if len(self._opened_clip_identities) >= _MAX_REORDERED_CLOSURES + 1:
            raise PipelineFault("active fragment identity buffer exceeded its bound")
        if opened.sequence in self._opened_clip_identities:
            raise PipelineFault("fragment-opened reported a duplicate sequence")
        clip_id = uuid4()
        start_monotonic_ns = offset + opened.running_time_ns
        async with self._durable_mutation_lock:
            retention_order = await asyncio.to_thread(finalizer.next_retention_order)
            await self._run_durable_worker(
                finalizer.register_active_clip,
                provisional_video_name=opened.path.name,
                clip_id=clip_id,
                start_monotonic_ns=start_monotonic_ns,
                retention_order=retention_order,
                deadline_detail="active clip registration exceeded its deadline",
            )
            self._opened_clip_identities[opened.sequence] = (
                clip_id,
                opened.path.name,
                retention_order,
            )
            self._opened_identity_changed.set()
            with self._active_clip_lock:
                self._active_clip_id = clip_id

    async def _drain_opened(self, backend: RuntimeBackend) -> None:
        while True:
            opened = await backend.next_opened_fragment()
            await self._register_opened_fragment(opened)

    async def _await_opened_identity(self, sequence: int) -> tuple[UUID, str, int] | None:
        value = self._opened_clip_identities.get(sequence)
        if value is not None:
            return value
        self._opened_identity_changed.clear()
        value = self._opened_clip_identities.get(sequence)
        if value is not None:
            return value
        try:
            await asyncio.wait_for(
                self._opened_identity_changed.wait(),
                timeout=self._limits.finalizer_timeout_s,
            )
        except TimeoutError:
            return None
        return self._opened_clip_identities.get(sequence)

    async def _demote_unclosed_active_clips(self) -> None:
        finalizer = self._finalizer
        if finalizer is None or not self._opened_clip_identities:
            return
        async with self._durable_mutation_lock:
            remaining = len(self._opened_clip_identities)
            while remaining:
                value = await self._run_durable_worker(
                    finalizer.reconcile_orphaned_writing,
                    limit=remaining,
                    deadline_detail="active clip orphan reconciliation exceeded its deadline",
                )
                if not isinstance(value, tuple) or len(value) != 2:
                    raise PipelineFault(
                        "active clip orphan reconciliation returned invalid evidence"
                    )
                examined, more_work = value
                if (
                    isinstance(examined, bool)
                    or not isinstance(examined, int)
                    or examined < 0
                    or not isinstance(more_work, bool)
                ):
                    raise PipelineFault(
                        "active clip orphan reconciliation returned invalid evidence"
                    )
                if more_work and examined == 0:
                    raise PipelineFault("active clip orphan reconciliation made no progress")
                if not more_work:
                    break
                remaining -= examined
            self._opened_clip_identities.clear()
            with self._active_clip_lock:
                self._active_clip_id = None

    async def _drain_finalized(self, backend: RuntimeBackend) -> None:
        while True:
            fragment = await backend.next_finalized_fragment()
            try:
                expected = self._next_finalize_sequence
                if expected is None:
                    raise PipelineFault("fragment finalization order is not initialized")
                if fragment.sequence < expected or fragment.sequence in self._reordered_finalized:
                    raise PipelineFault(
                        "fragment finalization reported a duplicate or stale sequence"
                    )
                if len(self._reordered_finalized) >= _MAX_REORDERED_CLOSURES:
                    raise PipelineFault("fragment finalization reorder buffer exceeded its bound")
                self._reordered_finalized[fragment.sequence] = fragment
                while expected in self._reordered_finalized:
                    ordered = self._reordered_finalized.pop(expected)
                    await self._process_finalized(ordered)
                    expected += 1
                    self._next_finalize_sequence = expected
            finally:
                backend.mark_finalized_fragment_processed()

    async def _durable_worker(
        self,
        callback: Callable[..., object],
        *arguments: object,
        deadline_detail: str,
        **keywords: object,
    ) -> object:
        """Serialize every recorder-owned catalog/filesystem mutation."""

        async with self._durable_mutation_lock:
            return await self._run_durable_worker(
                callback,
                *arguments,
                deadline_detail=deadline_detail,
                **keywords,
            )

    async def _run_durable_worker(
        self,
        callback: Callable[..., object],
        *arguments: object,
        deadline_detail: str,
        **keywords: object,
    ) -> object:
        """Run one durable mutation without ever abandoning its worker thread.

        Cancelling or timing out ``asyncio.to_thread`` only cancels the awaiting
        coroutine; it cannot stop the native thread.  A detached finalizer could
        therefore keep moving a pair after runtime cleanup.  This helper uses a
        non-cancelling deadline observation and always joins the worker before
        propagating timeout or cancellation.
        """

        worker = asyncio.create_task(
            asyncio.to_thread(callback, *arguments, **keywords),
            name="recorder-durable-worker",
        )
        cancellation: asyncio.CancelledError | None = None
        try:
            done, _ = await asyncio.wait(
                {worker},
                timeout=self._limits.finalizer_timeout_s,
            )
        except asyncio.CancelledError as error:
            done = set()
            cancellation = error
        exceeded_deadline = cancellation is None and worker not in done
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError as error:
                cancellation = error
        if cancellation is not None:
            if not worker.cancelled():
                worker.exception()
            raise cancellation
        result = worker.result()
        if exceeded_deadline:
            raise PipelineFault(deadline_detail)
        return result

    async def _process_finalized(self, fragment: FinalizedFragment) -> None:
        finalizer = self._finalizer
        config = self._config
        boot_uuid = self._boot_uuid
        boot_short_id = self._boot_short_id
        offset = self._pipeline_monotonic_offset_ns
        explicit_start_running_ns = fragment.start_running_time_ns
        start_running_ns = (
            explicit_start_running_ns
            if explicit_start_running_ns is not None
            else self._next_fragment_start_running_ns
        )
        if finalizer is not None:
            active_identity = await self._await_opened_identity(fragment.sequence)
            if (
                config is None
                or boot_uuid is None
                or boot_short_id is None
                or offset is None
                or start_running_ns is None
                or fragment.running_time_ns <= start_running_ns
                or active_identity is None
                or active_identity[1] != fragment.path.name
            ):
                raise PipelineFault("fragment finalization timing contract is invalid")
            clip_id = active_identity[0]
            duration_ns = fragment.running_time_ns - start_running_ns
            size_bytes = await asyncio.to_thread(finalizer.video_size, fragment.path.name)
            measured_bitrate = (size_bytes * 8_000_000_000) // duration_ns
            current = self._attempt_counters()
            baseline = self._counter_baseline
            warnings: tuple[str, ...] = ()
            self._last_clip_frames = None
            self._last_clip_audio_units = None
            if current is not None and baseline is not None:
                raw_delta = current.raw_frames - baseline.raw_frames
                encoded_delta = current.encoded_access_units - baseline.encoded_access_units
                dropped_delta = (
                    None
                    if current.dropped_frames is None or baseline.dropped_frames is None
                    else current.dropped_frames - baseline.dropped_frames
                )
                if (
                    raw_delta < 0
                    or encoded_delta < 0
                    or (dropped_delta is not None and dropped_delta < 0)
                ):
                    raise PipelineFault("GStreamer frame counters regressed")
                self._last_clip_frames = {
                    "raw": raw_delta,
                    "encoded": encoded_delta,
                    "dropped": dropped_delta,
                }
                self._counter_baseline = current
            current_audio = self._attempt_audio_counters()
            baseline_audio = self._audio_counter_baseline
            media_contract = fragment.media_contract
            if media_contract is not None:
                units = media_contract.encoded_audio_access_units
                if units is None:
                    raise PipelineFault(
                        "finalized fragment omitted generation-bound audio counters"
                    )
                self._last_clip_audio_units = units
                if media_contract.audio_caps is not None and units == 0:
                    warnings += ("no encoded AAC access units observed; clip marked video-only",)
            elif self._effective_audio_caps is not None:
                if current_audio is None or baseline_audio is None:
                    warnings += (
                        "audio access-unit observation was unavailable; clip marked video-only",
                    )
                else:
                    audio_delta = current_audio.encoded_access_units - baseline_audio
                    if audio_delta < 0:
                        raise PipelineFault("GStreamer audio counter regressed")
                    self._last_clip_audio_units = audio_delta
                    self._audio_counter_baseline = current_audio.encoded_access_units
                    if audio_delta == 0:
                        warnings += (
                            "no encoded AAC access units observed; clip marked video-only",
                        )
            encoded_frames = 0
            dropped_frames = 0
            if self._last_clip_frames is not None:
                encoded = self._last_clip_frames["encoded"]
                dropped = self._last_clip_frames["dropped"]
                encoded_frames = 0 if encoded is None else encoded
                dropped_frames = 0 if dropped is None else dropped
                if dropped is None:
                    warnings += (
                        "dropped-frame observation was unavailable; zero is a "
                        "compatibility sentinel",
                    )
            else:
                warnings += ("frame and drop counters are unavailable in this recorder release",)
            self._last_clip_bitrate_bps = measured_bitrate
            self._last_clip_duration_ns = duration_ns
            self._last_clip_sequence = fragment.sequence
            clip_start_monotonic_ns = offset + start_running_ns
            clip_end_monotonic_ns = offset + fragment.running_time_ns
            gps_summary, gps_warnings = self._gps_summary_for_interval(
                clip_start_monotonic_ns,
                clip_end_monotonic_ns,
            )
            warnings += gps_warnings
            target_pair = finalized_unsynced_clip_pair(
                boot_id=boot_short_id,
                sequence=fragment.sequence,
            )
            effective_audio = (
                self._effective_audio_caps if media_contract is None else media_contract.audio_caps
            )
            audio_available = (
                effective_audio is not None
                and self._last_clip_audio_units is not None
                and self._last_clip_audio_units > 0
            )
            sidecar = ClipSidecar(
                schema_version=1,
                clip_id=clip_id,
                boot_id=boot_uuid,
                sequence=fragment.sequence,
                video_file=target_pair.video_name,
                metadata_file=target_pair.metadata_name,
                start_utc=None,
                end_utc=None,
                start_monotonic_ns=clip_start_monotonic_ns,
                end_monotonic_ns=clip_end_monotonic_ns,
                gps_time_state=GpsTimeState.UNSYNCED,
                system_clock_state=SystemClockState.UNSET,
                timestamp_quality=TimestampQuality.MONOTONIC_ONLY,
                time_anchor=None,
                timezone=config.time.timezone,
                start_local=None,
                video=VideoSummary(
                    "h264",
                    config.video.width,
                    config.video.height,
                    float(config.video.fps),
                    config.video.bitrate_bps,
                    measured_bitrate,
                    encoded_frames,
                    dropped_frames,
                ),
                audio=(
                    AudioSummary(False, None, None, None, None)
                    if not audio_available or effective_audio is None
                    else AudioSummary(
                        True,
                        effective_audio.codec,
                        effective_audio.sample_rate_hz,
                        effective_audio.channels,
                        effective_audio.bitrate_bps,
                    )
                ),
                gps=gps_summary,
                protected=False,
                protection_reason=None,
                software_version=get_version(),
                warnings=warnings,
            )
            sidecar = self._project_close_time_anchor(sidecar)
            retention_order = active_identity[2]
            async with self._durable_mutation_lock:
                await self._run_durable_worker(
                    finalizer.finalize,
                    provisional_video_name=fragment.path.name,
                    sidecar=sidecar,
                    retention_order=retention_order,
                    deadline_detail="clip finalization exceeded its deadline",
                )
                del self._opened_clip_identities[fragment.sequence]
                with self._active_clip_lock:
                    if self._active_clip_id == clip_id:
                        self._active_clip_id = None
            if sidecar.timestamp_quality is TimestampQuality.MONOTONIC_ONLY:
                if len(self._metadata_reconciliation_hints) < _MAX_METADATA_TRACKED:
                    self._metadata_reconciliation_hints.add(sidecar.clip_id)
                else:
                    self._metadata_reconciliation_overflows += 1
                self._metadata_reconciliation_wakeup.set()
            if explicit_start_running_ns is None:
                self._next_fragment_start_running_ns = fragment.running_time_ns
            stable_duration_ns = max(config.video.clip_duration_s - 1, 1) * 1_000_000_000
            if duration_ns >= stable_duration_ns:
                self._consecutive_restarts = 0
        self._last_finalized = fragment
        self._finalized_count += 1

    def _start_metadata_reconciliation(self) -> None:
        if (
            self._finalizer is None
            or self._gps_service is None
            or self._boot_uuid is None
            or self._metadata_reconciliation_task is not None
        ):
            return
        self._metadata_reconciliation_task = asyncio.create_task(
            self._metadata_reconciliation_loop(),
            name="recorder-metadata-reconciliation",
        )

    async def _metadata_reconciliation_loop(self) -> None:
        """Run optional bounded reconciliation independently of fragment draining."""

        while not self._metadata_reconciliation_stop.is_set():
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    self._metadata_reconciliation_wakeup.wait(),
                    timeout=self._limits.metadata_reconciliation_interval_s,
                )
            self._metadata_reconciliation_wakeup.clear()
            if self._metadata_reconciliation_stop.is_set():
                return
            await self._run_metadata_reconciliation_pass()

    async def _run_metadata_reconciliation_pass(self) -> None:
        """Serialize one optional pass and contain every non-cancellation failure."""

        try:
            async with self._metadata_reconciliation_pass_lock:
                await self._metadata_reconciliation_pass()
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            self._metadata_reconciliation_failures += 1
            self._last_metadata_reconciliation_error = _bounded_exception_detail(error)

    async def _metadata_reconciliation_pass(self) -> None:
        finalizer = self._finalizer
        service = self._gps_service
        boot_uuid = self._boot_uuid
        if finalizer is None or service is None or boot_uuid is None:
            return
        snapshot = service.snapshot
        accepted = snapshot.time_anchor
        if accepted is None or snapshot.gps_time_state is GpsTimeState.UNSYNCED:
            return
        anchor = TimeAnchor(
            source=TimeAnchorSource.GPS,
            monotonic_ns=accepted.monotonic_ns,
            utc=accepted.utc,
            uncertainty_ns=accepted.uncertainty_ns,
            provenance=accepted.provenance,
        )
        candidates = cast(
            tuple[MetadataReconciliationCandidate, ...],
            await self._durable_worker(
                finalizer.metadata_reconciliation_candidates,
                boot_uuid,
                limit=_MAX_METADATA_CANDIDATE_PAGE,
                after_order=self._metadata_reconciliation_cursor_order,
                after_clip_id=self._metadata_reconciliation_cursor_id,
                deadline_detail="metadata candidate scan exceeded its deadline",
            ),
        )
        if not candidates:
            self._reset_metadata_cursor()
            return

        attempts = 0
        consumed_page = True
        for candidate in candidates:
            if attempts >= _MAX_METADATA_RECONCILIATIONS_PER_PASS:
                consumed_page = False
                break
            self._metadata_reconciliation_cursor_order = candidate.retention_order
            self._metadata_reconciliation_cursor_id = candidate.clip_id
            if candidate.clip_id in self._metadata_reconciliation_parked:
                continue
            if len(self._metadata_reconciliation_hints) < _MAX_METADATA_TRACKED:
                self._metadata_reconciliation_hints.add(candidate.clip_id)
            attempts += 1
            try:
                await self._durable_worker(
                    finalizer.reconcile_metadata,
                    candidate.clip_id,
                    anchor=anchor,
                    expected_boot_id=boot_uuid,
                    gps_time_state=snapshot.gps_time_state,
                    system_clock_state=SystemClockState.UNSET,
                    deadline_detail="metadata reconciliation exceeded its deadline",
                )
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                self._record_metadata_reconciliation_failure(candidate.clip_id, error)
                continue
            self._metadata_reconciliations += 1
            self._last_metadata_reconciliation_error = None
            self._metadata_reconciliation_hints.discard(candidate.clip_id)
            self._metadata_reconciliation_retries.pop(candidate.clip_id, None)

        if consumed_page and len(candidates) < _MAX_METADATA_CANDIDATE_PAGE:
            self._reset_metadata_cursor()

    def _record_metadata_reconciliation_failure(
        self,
        clip_id: UUID,
        error: BaseException,
    ) -> None:
        self._metadata_reconciliation_failures += 1
        detail = _bounded_exception_detail(error)
        self._last_metadata_reconciliation_error = detail
        retryable = not isinstance(error, MetadataReconciliationRefused) or error.retryable
        retries = self._metadata_reconciliation_retries.get(clip_id, 0) + 1
        if retryable and retries < _MAX_METADATA_RETRY_ATTEMPTS:
            if len(self._metadata_reconciliation_retries) < _MAX_METADATA_TRACKED:
                self._metadata_reconciliation_retries[clip_id] = retries
            return
        self._metadata_reconciliation_retries.pop(clip_id, None)
        self._metadata_reconciliation_hints.discard(clip_id)
        if len(self._metadata_reconciliation_parked) < _MAX_METADATA_TRACKED:
            self._metadata_reconciliation_parked[clip_id] = detail
        else:
            self._metadata_reconciliation_overflows += 1

    def _reset_metadata_cursor(self) -> None:
        self._metadata_reconciliation_cursor_order = -1
        self._metadata_reconciliation_cursor_id = None

    async def _stop_metadata_reconciliation(self) -> None:
        self._metadata_reconciliation_stop.set()
        self._metadata_reconciliation_wakeup.set()
        task = self._metadata_reconciliation_task
        if task is None:
            return
        done, _ = await asyncio.wait({task}, timeout=self._limits.task_stop_timeout_s)
        if task not in done:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        else:
            try:
                task.result()
            except BaseException as error:
                self._metadata_reconciliation_failures += 1
                self._last_metadata_reconciliation_error = _bounded_exception_detail(error)
        self._metadata_reconciliation_task = None

    async def _cancel_background_tasks(self) -> None:
        tasks = tuple(
            task
            for task in (
                self._backend_run_task,
                self._fragment_drain_task,
                self._fragment_open_drain_task,
            )
            if task is not None
        )
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._backend_run_task = None
        self._fragment_drain_task = None
        self._fragment_open_drain_task = None

    def _release_ownership(self) -> None:
        if self._ownership_claimed:
            self._ownership.release("dashcamd")
            self._ownership_claimed = False

    async def _fresh_replacement_preflight(self) -> None:
        config = self._config
        if config is None:
            raise PipelineContractError("recorder runtime lacks its bound configuration")
        try:
            result = await asyncio.to_thread(
                self._preflight,
                config,
                identity_path=self._identity_path,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise RecorderStorageFault("replacement storage preflight failed") from error
        if not isinstance(result, PreflightResult):
            raise PipelineContractError("storage preflight returned an invalid result")
        self._preflight_result = result
        if not result.ready:
            reasons = ",".join(reason.value for reason in result.reasons) or "NOT_READY"
            raise RecorderStorageFault(
                f"replacement storage preflight refused: "
                f"state={result.state.value} reasons={reasons}"[:512]
            )
        if self._finalizer_device_id is not None:
            facts = result.facts
            device_id = None if facts is None else facts.mount.device_id
            if device_id != self._finalizer_device_id:
                raise RecorderStorageFault(
                    "replacement storage identity differs from the bound finalizer"
                )

    async def _allocate_output(self) -> SegmentedOutputConfig:
        config = self._config
        boot_id = self._boot_short_id
        result = self._preflight_result
        if config is None or boot_id is None or result is None or not result.ready:
            raise RecorderStorageFault("runtime lacks matching READY storage evidence")
        recording_root = Path(config.storage.recording_root)
        pending = recording_root / "pending"
        facts = result.facts
        mount_device_id = None if facts is None else facts.mount.device_id
        expected_st_dev = (
            None if mount_device_id is None else _st_dev_from_device_id(mount_device_id)
        )
        try:
            sequence = await asyncio.to_thread(
                self._sequence_planner,
                recording_root,
                pending,
                boot_id,
            )
        except RecorderStorageFault:
            raise
        except (OSError, ValueError, PipelineContractError) as error:
            raise RecorderStorageFault("pending output allocation failed") from error
        sequence = max(sequence, self._next_allocation_floor)
        if sequence > 999_999:
            raise RecorderStorageFault("pending output sequence space is exhausted")
        self._next_allocation_floor = sequence + 1
        return SegmentedOutputConfig(
            pending,
            boot_id,
            start_index=sequence,
            expected_st_dev=expected_st_dev,
        )

    async def _start_attempt(
        self, profile: VideoProfile, audio_plan: AudioCapturePlan | None = None
    ) -> None:
        output = await self._allocate_output()
        if self._reordered_finalized:
            raise PipelineFault("a prior attempt left unresolved fragment closures")
        self._next_finalize_sequence = output.start_index
        self._backend_stop = asyncio.Event()
        self._drain_failure_observed = False
        self._ownership.claim("dashcamd")
        self._ownership_claimed = True
        opened_task: asyncio.Task[OpenedFragment] | None = None
        try:
            backend = (
                self._backend_factory(output)
                if audio_plan is None
                else cast(AudioBackendFactory, self._audio_backend_factory)(
                    output,
                    audio_plan,
                )
            )
            self._backend = backend
            config = self._config
            if config is None:
                raise PipelineContractError("backend startup lacks its bound configuration")
            backend.configure_overlay_text(
                self._current_overlay_text() if config.overlay.enabled else None
            )
            bind_loss_probe = getattr(backend, "bind_audio_loss_probe", None)
            if audio_plan is not None and callable(bind_loss_probe):
                selector = self._audio_selector
                discover = self._audio_discovery
                if selector is None or discover is None:
                    raise PipelineContractError(
                        "audio backend lacks exact loss-discovery collaborators"
                    )
                bind_loss_probe(lambda: discover(selector))
            effective = await backend.start(profile)
            if effective != profile:
                raise ProfileValidationError(
                    "backend effective profile differs from the requested profile"
                )
            self._effective_profile = effective
            identity = getattr(backend, "encoder_identity", None)
            if isinstance(identity, EncoderIdentity):
                self._encoder_identity = identity
            caps = getattr(backend, "effective_caps", None)
            if isinstance(caps, EffectiveCaps):
                self._effective_caps = caps
            audio_caps = getattr(backend, "effective_audio_caps", None)
            if isinstance(audio_caps, EffectiveAudioCaps):
                self._effective_audio_caps = audio_caps
            elif audio_plan is not None:
                raise AudioStartupError(
                    "matched audio backend omitted validated effective audio caps"
                )
            else:
                self._effective_audio_caps = None
            self._backend_run_task = asyncio.create_task(
                backend.run(self._backend_stop),
                name="gstreamer-recorder-backend",
            )
            opened_task = asyncio.create_task(
                backend.wait_for_first_fragment_opened(),
                name="gstreamer-first-fragment-open",
            )
            done, _ = await asyncio.wait(
                {opened_task, self._backend_run_task},
                timeout=self._limits.first_fragment_timeout_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if self._backend_run_task in done:
                try:
                    await self._backend_run_task
                except BaseException as error:
                    if await self._escalate_no_space(error):
                        assert self._storage_space_monitor is not None
                        raise StorageSafetyStop(
                            _storage_space_detail(self._storage_space_monitor.snapshot)
                        ) from error
                    raise
                raise RecoverablePipelineError("backend exited before opening its first fragment")
            if opened_task not in done:
                raise RecoverablePipelineError(
                    "first fragment did not open within the startup deadline"
                )
            opened = await opened_task
            monotonic_now = self._monotonic_ns()
            if monotonic_now < opened.running_time_ns:
                raise PipelineFault("pipeline running time exceeds the host monotonic clock")
            self._pipeline_monotonic_offset_ns = monotonic_now - opened.running_time_ns
            self._next_fragment_start_running_ns = opened.running_time_ns
            if self._finalizer is not None:
                await self._register_opened_fragment(opened)
            self._fragment_drain_task = asyncio.create_task(
                self._drain_finalized(backend),
                name="gstreamer-fragment-events",
            )
            if self._finalizer is not None:
                self._fragment_open_drain_task = asyncio.create_task(
                    self._drain_opened(backend),
                    name="gstreamer-fragment-open-events",
                )
            self._counter_baseline = self._attempt_counters()
            observed_audio = self._attempt_audio_counters()
            self._audio_counter_baseline = (
                None if observed_audio is None else observed_audio.encoded_access_units
            )
        except BaseException as start_error:
            try:
                await self._cleanup_current_attempt()
            except BaseException as cleanup_error:
                raise PipelineFault(
                    "backend cleanup failed after recorder startup failure: "
                    f"{_bounded_exception_detail(cleanup_error)}"
                ) from start_error
            raise
        finally:
            if opened_task is not None and not opened_task.done():
                opened_task.cancel()
                await asyncio.gather(opened_task, return_exceptions=True)

    async def start(self, config: DashcamConfig) -> None:
        """Open the initial media session using the daemon's checked storage."""

        if self._start_attempted:
            raise PipelineContractError("recorder runtime instances are single-use")
        self._start_attempted = True
        profile = _video_profile(config.video)
        checked_result = self._preflight_result
        if (
            self._checked_config != config
            or checked_result is None
            or not (
                checked_result.ready
                or checked_result.recoverable_reserve_exhaustion
            )
        ):
            raise RecorderStorageFault("runtime lacks matching READY storage evidence")
        reclaim_before_probe = checked_result.recoverable_reserve_exhaustion
        recording_root = Path(config.storage.recording_root)
        self._boot_short_id = await asyncio.to_thread(self._boot_id_reader)
        self._config = config
        audio_plan = await self._resolve_audio(config.audio)
        if self._finalizer_factory is not None:
            self._boot_uuid = await asyncio.to_thread(self._boot_uuid_reader)
            facts = checked_result.facts
            device_id = None if facts is None else facts.mount.device_id
            if device_id is None:
                raise RecorderStorageFault("READY storage evidence lacks a device identity")
            self._finalizer_device_id = device_id
            self._finalizer = self._finalizer_factory(recording_root, device_id)
            for _ in range(self._limits.max_startup_reconciliation_passes):
                leases_more = await self._durable_worker(
                    self._finalizer.expire_download_leases,
                    str(self._boot_uuid),
                    deadline_detail="download lease recovery exceeded its deadline",
                )
                if not isinstance(leases_more, bool):
                    raise RecorderFinalizationFault(
                        "download lease recovery returned invalid evidence"
                    )
                if not leases_more:
                    break
            else:
                raise RecorderFinalizationFault(
                    "download lease recovery exceeded its bounded convergence passes"
                )
            for _ in range(self._limits.max_startup_reconciliation_passes):
                orphan_value = await self._durable_worker(
                    self._finalizer.reconcile_orphaned_writing,
                    limit=self._limits.max_startup_reclamation_steps,
                    deadline_detail="active clip orphan recovery exceeded its deadline",
                )
                if (
                    not isinstance(orphan_value, tuple)
                    or len(orphan_value) != 2
                    or isinstance(orphan_value[0], bool)
                    or not isinstance(orphan_value[0], int)
                    or orphan_value[0] < 0
                    or not isinstance(orphan_value[1], bool)
                ):
                    raise RecorderFinalizationFault(
                        "active clip orphan recovery returned invalid evidence"
                    )
                if not orphan_value[1]:
                    break
                if orphan_value[0] == 0:
                    raise RecorderFinalizationFault(
                        "active clip orphan recovery made no bounded progress"
                    )
            else:
                raise RecorderFinalizationFault(
                    "active clip orphan recovery exceeded its bounded convergence passes"
                )
            await self._initialize_storage_space_monitor(config, self._finalizer)
            status = None
            startup_steps_remaining = self._limits.max_startup_reclamation_steps
            if self._storage_space_monitor is not None:
                status, used, pending_delete_remaining = await self._run_storage_reclamation(
                    maximum_steps=startup_steps_remaining
                )
                startup_steps_remaining -= used
            else:
                pending_delete_remaining = False
            if pending_delete_remaining:
                raise StorageSafetyStop(
                    "startup deletion recovery exceeded its bounded convergence budget"
                )
            for _ in range(self._limits.max_startup_reconciliation_passes):
                try:
                    recovery_value = await self._durable_worker(
                        self._finalizer.reconcile_pending,
                        deadline_detail="pending reconciliation exceeded its deadline",
                    )
                except asyncio.CancelledError:
                    raise
                except BaseException as error:
                    raise RecorderFinalizationFault(
                        f"pending reconciliation failed: {_bounded_exception_detail(error)}"
                    ) from error
                if not isinstance(recovery_value, FinalizationRecoveryReport):
                    raise RecorderFinalizationFault(
                        "pending reconciliation returned an invalid report"
                    )
                if not recovery_value.more_work:
                    break
            else:
                raise RecorderFinalizationFault(
                    "pending reconciliation exceeded its bounded convergence passes"
                )
            if self._storage_space_monitor is not None and startup_steps_remaining:
                status, _, pending_delete_remaining = await self._run_storage_reclamation(
                    maximum_steps=startup_steps_remaining
                )
            if pending_delete_remaining:
                raise StorageSafetyStop(
                    "startup deletion recovery exceeded its bounded convergence budget"
                )
            if reclaim_before_probe:
                if status is None or status.mode is not RetentionMode.NORMAL:
                    raise StorageSafetyStop(
                        "startup reserve recovery did not restore the high-water threshold"
                    )
                try:
                    refreshed = await asyncio.to_thread(
                        self._preflight,
                        config,
                        identity_path=self._identity_path,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    raise RecorderStorageFault(
                        "post-reclamation storage preflight failed"
                    ) from error
                if not isinstance(refreshed, PreflightResult) or not refreshed.ready:
                    raise StorageSafetyStop(
                        "post-reclamation full storage preflight did not reach READY"
                    )
                refreshed_facts = refreshed.facts
                monitor = self._storage_space_monitor
                if (
                    refreshed_facts is None
                    or monitor is None
                    or refreshed_facts.mount.device_id != self._finalizer_device_id
                    or refreshed_facts.mount.uuid != monitor.snapshot.volume_uuid
                    or refreshed_facts.space.capacity_bytes
                    != monitor.snapshot.capacity_bytes
                ):
                    raise StorageSafetyStop(
                        "post-reclamation storage identity differs from the bound volume"
                    )
                self._preflight_result = refreshed
        elif reclaim_before_probe:
            raise RecorderStorageFault("startup reserve recovery requires the durable reclaimer")
        try:
            await self._start_attempt(profile, audio_plan)
        except AudioStartupError as error:
            if audio_plan is None or self._audio_startup_fallback_used:
                raise
            self._audio_startup_fallback_used = True
            self._audio_state = RuntimeAudioState.FAULTED
            self._audio_reason = "startup_audio_failure"
            self._audio_detail = _bounded_exception_detail(error)
            self._audio_plan = None
            await self._start_attempt(profile)
        self._start_gps(config.gps)
        self._start_overlay(config.overlay)
        self._start_metadata_reconciliation()
        self._start_storage_space_monitor()

    async def _supervise_attempt(self, stop_requested: asyncio.Event) -> None:
        backend_task = self._backend_run_task
        if backend_task is None:
            raise PipelineContractError("recorder runtime has no active backend")
        external_stop = asyncio.create_task(
            stop_requested.wait(),
            name="recorder-runtime-stop-wait",
        )
        try:
            supervised = {backend_task, external_stop}
            if self._fragment_drain_task is not None:
                supervised.add(self._fragment_drain_task)
            if self._fragment_open_drain_task is not None:
                supervised.add(self._fragment_open_drain_task)
            if self._storage_space_task is not None:
                supervised.add(self._storage_space_task)
            done, _ = await asyncio.wait(
                supervised,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if external_stop in done:
                self._backend_stop.set()
                return
            if self._storage_space_task in done:
                assert self._storage_space_task is not None
                storage_error = self._storage_space_task.exception()
                if isinstance(storage_error, StorageSafetyStop):
                    raise storage_error
                raise RecorderStorageFault(
                    "storage threshold monitor exited unexpectedly"
                ) from storage_error
            if self._fragment_drain_task in done:
                assert self._fragment_drain_task is not None
                try:
                    await self._fragment_drain_task
                except BaseException as error:
                    self._drain_failure_observed = True
                    if await self._escalate_no_space(error):
                        assert self._storage_space_monitor is not None
                        raise StorageSafetyStop(
                            _storage_space_detail(self._storage_space_monitor.snapshot)
                        ) from error
                    raise RecorderFinalizationFault(
                        f"fragment finalization failed: {_bounded_exception_detail(error)}"
                    ) from error
                raise RecorderFinalizationFault("fragment finalizer exited unexpectedly")
            if self._fragment_open_drain_task in done:
                assert self._fragment_open_drain_task is not None
                try:
                    await self._fragment_open_drain_task
                except BaseException as error:
                    raise RecorderFinalizationFault(
                        "fragment identity registration failed: "
                        f"{_bounded_exception_detail(error)}"
                    ) from error
                raise RecorderFinalizationFault(
                    "fragment identity registration exited unexpectedly"
                )
            try:
                await backend_task
            except BaseException as error:
                if await self._escalate_no_space(error):
                    assert self._storage_space_monitor is not None
                    raise StorageSafetyStop(
                        _storage_space_detail(self._storage_space_monitor.snapshot)
                    ) from error
                raise
            if not self._backend_stop.is_set():
                raise RecoverablePipelineError("backend exited without a stop request")
        finally:
            if not external_stop.done():
                external_stop.cancel()
            await asyncio.gather(external_stop, return_exceptions=True)

    async def _stop_once(self) -> None:
        self._backend_stop.set()
        backend = self._backend
        run_task = self._backend_run_task
        drain_task = self._fragment_drain_task
        stop_error: BaseException | None = None
        drain_error: BaseException | None = None
        queue_error: BaseException | None = None
        run_error: BaseException | None = None
        try:
            if backend is not None:
                try:
                    await backend.stop()
                except BaseException as error:
                    stop_error = error
                try:
                    await asyncio.wait_for(
                        backend.wait_for_finalized_fragments_processed(),
                        timeout=self._limits.finalizer_timeout_s,
                    )
                except BaseException as error:
                    queue_error = error
                if queue_error is None and self._reordered_finalized:
                    queue_error = PipelineFault("fragment closure sequence has an unresolved gap")
            if (
                not self._drain_failure_observed
                and drain_task is not None
                and drain_task.done()
                and not drain_task.cancelled()
            ):
                drain_error = drain_task.exception()
                if drain_error is None:
                    drain_error = PipelineFault("fragment finalizer exited unexpectedly")
            if run_task is not None and not run_task.done():
                try:
                    await asyncio.wait_for(
                        asyncio.shield(run_task),
                        timeout=self._limits.task_stop_timeout_s,
                    )
                except BaseException as error:
                    run_error = error
            if (
                run_error is None
                and run_task is not None
                and run_task.done()
                and not run_task.cancelled()
            ):
                observed = run_task.exception()
                if observed is not None and not isinstance(observed, RecoverablePipelineError):
                    run_error = observed
        finally:
            self._roll_up_attempt_counters()
            await self._cancel_background_tasks()
            await self._demote_unclosed_active_clips()
            self._backend = None
            self._counter_baseline = None
            self._drain_failure_observed = False
            self._release_ownership()
        failure = drain_error or stop_error or queue_error or run_error
        if failure is not None:
            raise PipelineFault(
                f"backend cleanup failed: {_bounded_exception_detail(failure)}"
            ) from failure

    async def _cleanup_current_attempt(self) -> None:
        task = self._stop_task
        if task is None:
            if self._backend is None:
                self._release_ownership()
                return
            task = asyncio.create_task(self._stop_once(), name="recorder-runtime-stop")
            self._stop_task = task
        try:
            await asyncio.shield(task)
        finally:
            if task.done() and self._stop_task is task:
                self._stop_task = None

    async def _recover(
        self,
        error: RecoverablePipelineError,
        stop_requested: asyncio.Event,
    ) -> bool:
        """Clean the failed attempt and open one bounded replacement."""

        self._capture_audio_restoration_failure()
        await self._cleanup_current_attempt()
        if stop_requested.is_set():
            return False
        if self._consecutive_restarts >= self._restart_policy.max_restarts:
            detail = _recovery_detail(
                error,
                attempt=self._consecutive_restarts,
                maximum=self._restart_policy.max_restarts,
            )
            event = RuntimeLifecycleEvent(
                RuntimeLifecycleEventKind.EXHAUSTED,
                self._pipeline_restart_count,
                self._consecutive_restarts,
                detail,
            )
            self._emit_lifecycle(event, cause=error)
            raise PipelineRecoveryExhausted(
                "critical pipeline exhausted its bounded restart policy"
            ) from error
        recovery_attempt = self._consecutive_restarts + 1
        delay_s = self._restart_policy.delay_for(recovery_attempt)
        detail = _recovery_detail(
            error,
            attempt=recovery_attempt,
            maximum=self._restart_policy.max_restarts,
            delay_s=delay_s,
        )
        self._emit_lifecycle(
            RuntimeLifecycleEvent(
                RuntimeLifecycleEventKind.RECOVERING,
                self._pipeline_restart_count,
                recovery_attempt,
                detail,
            ),
            cause=error,
        )
        cancelled = await self._backoff_waiter(
            delay_s,
            stop_requested,
        )
        if cancelled or stop_requested.is_set():
            return False
        self._consecutive_restarts = recovery_attempt
        self._pipeline_restart_count += 1
        self._emit_lifecycle(
            RuntimeLifecycleEvent(
                RuntimeLifecycleEventKind.RESTARTING,
                self._pipeline_restart_count,
                recovery_attempt,
                detail,
            ),
            cause=error,
        )
        await self._fresh_replacement_preflight()
        config = self._config
        if config is None:
            raise PipelineContractError("recorder runtime lacks its bound configuration")
        await self._start_attempt(_video_profile(config.video), self._audio_plan)
        try:
            self._emit_lifecycle(
                RuntimeLifecycleEvent(
                    RuntimeLifecycleEventKind.RECOVERED,
                    self._pipeline_restart_count,
                    recovery_attempt,
                )
            )
        except BaseException:
            await self._cleanup_current_attempt()
            raise
        return True

    async def run(self, stop_requested: asyncio.Event) -> None:
        """Supervise video and replace only recoverable camera/encoder attempts."""

        if self._backend_run_task is None:
            raise PipelineContractError("recorder runtime has not started")
        try:
            while not stop_requested.is_set():
                try:
                    await self._supervise_attempt(stop_requested)
                except RecoverablePipelineError as error:
                    current_error = error
                    while True:
                        try:
                            recovered = await self._recover(
                                current_error,
                                stop_requested,
                            )
                        except RecoverablePipelineError as replacement_error:
                            current_error = replacement_error
                            continue
                        if not recovered:
                            return
                        break
                    continue
                return
        except asyncio.CancelledError:
            try:
                await self._stop_storage_space_monitor()
                await self._stop_overlay()
                await self._cleanup_current_attempt()
                await self._run_metadata_reconciliation_pass()
            finally:
                await asyncio.gather(
                    self._stop_gps(),
                    self._stop_metadata_reconciliation(),
                    return_exceptions=True,
                )
            raise

    async def stop(self) -> None:
        """Finalize media, reconcile its metadata, then stop optional workers."""

        media_cleanup: BaseException | None = None
        try:
            await self._stop_storage_space_monitor()
            await self._stop_overlay()
            await self._cleanup_current_attempt()
        except BaseException as error:
            media_cleanup = error
        if media_cleanup is None and self._reconciliation_allowed:
            await self._run_metadata_reconciliation_pass()
        await asyncio.gather(
            self._stop_gps(),
            self._stop_metadata_reconciliation(),
            return_exceptions=True,
        )
        if media_cleanup is not None:
            raise media_cleanup


def build_production_runtime(
    *,
    config_path: Path,
    identity_path: Path,
    enable_unvalidated_audio_loss_isolation: bool = True,
    enable_audio_restoration: bool = True,
) -> GStreamerRecorderRuntime:
    """Build the target runtime without importing or initializing PyGObject.

    The exact-Pi hash-closed production qualifications passed on 2026-07-27
    and 2026-07-28, so ordinary production construction enables both the
    accepted A/V-to-video-only loss handoff and bounded three-slot audio
    restoration.  Explicit switches remain for focused refusal tests.
    """

    if not isinstance(enable_unvalidated_audio_loss_isolation, bool):
        raise ValueError("audio-loss qualification feature gate must be boolean")
    if not isinstance(enable_audio_restoration, bool):
        raise ValueError("audio-restoration feature gate must be boolean")
    if enable_audio_restoration and not enable_unvalidated_audio_loss_isolation:
        raise ValueError("audio restoration requires audio-loss isolation")

    from dashcam.control.runtime_server import (
        CONTROL_CATALOG_BUSY_TIMEOUT_MS,
        CONTROL_DURABLE_WORKER_TIMEOUT_S,
        build_runtime_control_endpoint,
    )

    def build_finalizer(
        recording_root: Path,
        expected_device_id: str,
    ) -> RecorderClipFinalizer:
        filesystem = DurableRootedFinalizationFilesystem(
            recording_root,
            expected_device_id=expected_device_id,
        )
        catalog = ClipCatalog(
            Path("/var/lib/dashcam/catalog.sqlite3"),
            busy_timeout_ms=CONTROL_CATALOG_BUSY_TIMEOUT_MS,
        )
        return RecorderClipFinalizer(
            catalog=catalog,
            filesystem=filesystem,
            monotonic_ns=time.monotonic_ns,
        )

    def build_gps_service(config: GpsConfig) -> GpsService:
        from dashcam.gps.linux import LinuxGpsTransportFactory

        anchor_tracker = NmeaAnchorTracker(
            policy=_anchor_policy(config),
            uncertainty_ns=config.anchor_uncertainty_ms * 1_000_000,
        )
        return GpsService(
            transport_factory=LinuxGpsTransportFactory(
                device=config.device,
                baud=config.baud,
            ),
            limits=GpsServiceLimits(stale_after_s=config.stale_after_s),
            anchor_tracker=anchor_tracker,
            telemetry_collector=GpsTelemetryCollector(
                max_sample_hz=config.max_sample_hz,
                stale_after_ns=int(config.stale_after_s * 1_000_000_000),
                history_capacity=config.max_sample_hz * 3 * 60,
            ),
        )

    return GStreamerRecorderRuntime(
        config_path=config_path,
        identity_path=identity_path,
        backend_factory=lambda output: GStreamerBackend(output=output),
        audio_backend_factory=lambda output, audio_plan: GStreamerBackend(
            output=output,
            audio_plan=audio_plan,
            enable_audio_loss_isolation=enable_unvalidated_audio_loss_isolation,
            enable_audio_restoration=enable_audio_restoration,
        ),
        audio_discovery=discover_capture_device,
        finalizer_factory=build_finalizer,
        gps_service_factory=build_gps_service,
        storage_space_monitor_factory=build_storage_space_monitor,
        control_endpoint_factory=build_runtime_control_endpoint,
        limits=RuntimeLimits(finalizer_timeout_s=CONTROL_DURABLE_WORKER_TIMEOUT_S),
    )


__all__ = [
    "BackendFactory",
    "GStreamerRecorderRuntime",
    "GpsServiceFactory",
    "RecorderStorageFault",
    "RuntimeBackend",
    "RuntimeFinalizer",
    "RuntimeLimits",
    "StorageSafetyStop",
    "build_production_runtime",
    "next_pending_sequence",
    "read_boot_uuid",
    "read_short_boot_id",
]
