"""Recorder service boundaries; imports never probe or open hardware."""

from dashcam.recorder.daemon import (
    DaemonLimits,
    DaemonOutcome,
    DaemonResult,
    PipelineNoProgressFault,
    RecorderDaemon,
    RecorderRuntime,
)
from dashcam.recorder.metrics import DEFAULT_STATUS_PATH, RuntimeSnapshotPublisher, SnapshotError
from dashcam.recorder.notifier import (
    NullNotifier,
    ServiceNotifier,
    SystemdNotifier,
)
from dashcam.recorder.runtime import (
    GStreamerRecorderRuntime,
    PipelineRecoveryExhausted,
    RecorderFinalizationFault,
    RecorderStorageFault,
    RuntimeLifecycleEvent,
    RuntimeLifecycleEventKind,
    RuntimeLimits,
    RuntimeObserverFault,
)
from dashcam.recorder.status import (
    RecorderReason,
    RecorderStatus,
    RecorderStatusStore,
    RecorderStatusTransitionError,
)

__all__ = [
    "DEFAULT_STATUS_PATH",
    "DaemonLimits",
    "DaemonOutcome",
    "DaemonResult",
    "GStreamerRecorderRuntime",
    "NullNotifier",
    "PipelineNoProgressFault",
    "PipelineRecoveryExhausted",
    "RecorderDaemon",
    "RecorderFinalizationFault",
    "RecorderReason",
    "RecorderRuntime",
    "RecorderStatus",
    "RecorderStatusStore",
    "RecorderStatusTransitionError",
    "RecorderStorageFault",
    "RuntimeLifecycleEvent",
    "RuntimeLifecycleEventKind",
    "RuntimeLimits",
    "RuntimeObserverFault",
    "RuntimeSnapshotPublisher",
    "ServiceNotifier",
    "SnapshotError",
    "SystemdNotifier",
]
