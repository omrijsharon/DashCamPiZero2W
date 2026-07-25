"""Hardware-independent recorder service boundaries.

The media runtime remains an injected protocol until the Raspberry Pi capability
gate selects and validates a camera pipeline. Importing this package never probes
or opens hardware.
"""

from dashcam.recorder.daemon import (
    DaemonLimits,
    DaemonOutcome,
    DaemonResult,
    RecorderDaemon,
    RecorderRuntime,
)
from dashcam.recorder.notifier import (
    NullNotifier,
    ServiceNotifier,
    SystemdNotifier,
)
from dashcam.recorder.status import (
    RecorderReason,
    RecorderStatus,
    RecorderStatusStore,
    RecorderStatusTransitionError,
)

__all__ = [
    "DaemonLimits",
    "DaemonOutcome",
    "DaemonResult",
    "NullNotifier",
    "RecorderDaemon",
    "RecorderReason",
    "RecorderRuntime",
    "RecorderStatus",
    "RecorderStatusStore",
    "RecorderStatusTransitionError",
    "ServiceNotifier",
    "SystemdNotifier",
]
