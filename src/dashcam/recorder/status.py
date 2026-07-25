"""Thread-safe, hardware-independent recorder status reporting."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from threading import Lock
from types import MappingProxyType
from typing import Final

from dashcam.state import RecorderState

MAX_STATUS_DETAIL_CHARS: Final = 512


class RecorderReason(StrEnum):
    """Stable reason codes for recorder lifecycle status."""

    CONFIG_ERROR = "CONFIG_ERROR"
    STARTUP_FAILED = "STARTUP_FAILED"
    STARTUP_TIMEOUT = "STARTUP_TIMEOUT"
    RUNTIME_EXITED = "RUNTIME_EXITED"
    RUNTIME_FAILED = "RUNTIME_FAILED"
    STORAGE_FAULT = "STORAGE_FAULT"
    OPTIONAL_SUBSYSTEM = "OPTIONAL_SUBSYSTEM"
    SHUTDOWN_FAILED = "SHUTDOWN_FAILED"
    SHUTDOWN_TIMEOUT = "SHUTDOWN_TIMEOUT"


class RecorderStatusTransitionError(ValueError):
    """Raised when a recorder status transition would be misleading."""


_TRANSITIONS: Final = MappingProxyType(
    {
        RecorderState.STARTING: frozenset(
            {
                RecorderState.STARTING,
                RecorderState.RECORDING,
                RecorderState.DEGRADED,
                RecorderState.STOPPING,
                RecorderState.FAULTED,
            }
        ),
        RecorderState.RECORDING: frozenset(
            {
                RecorderState.RECORDING,
                RecorderState.DEGRADED,
                RecorderState.STOPPING,
                RecorderState.FAULTED,
            }
        ),
        RecorderState.DEGRADED: frozenset(
            {
                RecorderState.RECORDING,
                RecorderState.DEGRADED,
                RecorderState.STOPPING,
                RecorderState.FAULTED,
            }
        ),
        RecorderState.FAULTED: frozenset(
            {
                RecorderState.FAULTED,
                RecorderState.STARTING,
                RecorderState.STOPPING,
            }
        ),
        RecorderState.STOPPING: frozenset(
            {
                RecorderState.STOPPING,
                RecorderState.FAULTED,
            }
        ),
    }
)


@dataclass(frozen=True, slots=True)
class RecorderStatus:
    """Immutable status snapshot safe to share with control-plane readers."""

    state: RecorderState
    sequence: int
    reason: RecorderReason | None = None
    detail: str | None = None
    config_schema_version: int | None = None
    notification_failures: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.state, RecorderState):
            raise RecorderStatusTransitionError("state must be a RecorderState")
        if (
            isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence < 0
        ):
            raise RecorderStatusTransitionError("sequence must be a non-negative integer")
        if self.reason is not None and not isinstance(self.reason, RecorderReason):
            raise RecorderStatusTransitionError("reason must be a RecorderReason")
        if self.state in {RecorderState.DEGRADED, RecorderState.FAULTED} and self.reason is None:
            raise RecorderStatusTransitionError(f"{self.state.value} status requires a reason")
        if self.state is RecorderState.RECORDING and self.reason is not None:
            raise RecorderStatusTransitionError("RECORDING status cannot carry a fault reason")
        if self.detail is not None and (
            not self.detail
            or len(self.detail) > MAX_STATUS_DETAIL_CHARS
            or not self.detail.isprintable()
        ):
            raise RecorderStatusTransitionError(
                f"detail must contain 1 to {MAX_STATUS_DETAIL_CHARS} printable characters"
            )
        if self.config_schema_version is not None and (
            isinstance(self.config_schema_version, bool)
            or not isinstance(self.config_schema_version, int)
            or self.config_schema_version < 1
        ):
            raise RecorderStatusTransitionError("config_schema_version must be a positive integer")
        if (
            isinstance(self.notification_failures, bool)
            or not isinstance(self.notification_failures, int)
            or self.notification_failures < 0
        ):
            raise RecorderStatusTransitionError(
                "notification_failures must be a non-negative integer"
            )

    def as_dict(self) -> dict[str, object]:
        """Return a stable JSON-compatible control-plane representation."""

        return {
            "state": self.state.value,
            "reason": self.reason.value if self.reason is not None else None,
            "detail": self.detail,
            "sequence": self.sequence,
            "config_schema_version": self.config_schema_version,
            "notification_failures": self.notification_failures,
        }


class RecorderStatusStore:
    """Serialize transitions and expose immutable snapshots to other threads."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._status = RecorderStatus(state=RecorderState.STARTING, sequence=0)

    def snapshot(self) -> RecorderStatus:
        """Return the current immutable status snapshot."""

        with self._lock:
            return self._status

    def transition(
        self,
        state: RecorderState,
        *,
        reason: RecorderReason | None = None,
        detail: str | None = None,
        config_schema_version: int | None = None,
    ) -> RecorderStatus:
        """Publish an allowed lifecycle transition with a new sequence number."""

        if not isinstance(state, RecorderState):
            raise RecorderStatusTransitionError("transition target must be a RecorderState")
        with self._lock:
            current = self._status
            if state not in _TRANSITIONS[current.state]:
                raise RecorderStatusTransitionError(
                    f"cannot transition {current.state.value} to {state.value}"
                )
            next_status = RecorderStatus(
                state=state,
                reason=reason,
                detail=detail,
                sequence=current.sequence + 1,
                config_schema_version=(
                    current.config_schema_version
                    if config_schema_version is None
                    else config_schema_version
                ),
                notification_failures=current.notification_failures,
            )
            self._status = next_status
            return next_status

    def record_notification_failure(self) -> RecorderStatus:
        """Count a failed supervisor notification without changing lifecycle."""

        with self._lock:
            self._status = replace(
                self._status,
                sequence=self._status.sequence + 1,
                notification_failures=self._status.notification_failures + 1,
            )
            return self._status
