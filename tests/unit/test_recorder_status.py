from __future__ import annotations

import pytest

from dashcam.recorder.status import (
    MAX_STATUS_DETAIL_CHARS,
    RecorderReason,
    RecorderStatus,
    RecorderStatusStore,
    RecorderStatusTransitionError,
)
from dashcam.state import RecorderState


def test_status_store_reports_immutable_sequenced_transitions() -> None:
    store = RecorderStatusStore()
    initial = store.snapshot()

    configured = store.transition(RecorderState.STARTING, config_schema_version=1)
    recording = store.transition(RecorderState.RECORDING)
    degraded = store.transition(
        RecorderState.DEGRADED,
        reason=RecorderReason.OPTIONAL_SUBSYSTEM,
        detail="GPS unavailable",
    )
    recovered = store.transition(RecorderState.RECORDING)

    assert initial == RecorderStatus(state=RecorderState.STARTING, sequence=0)
    assert configured.sequence == 1
    assert recording.sequence == 2
    assert degraded.sequence == 3
    assert recovered.sequence == 4
    assert recovered.config_schema_version == 1
    assert initial is not store.snapshot()
    assert degraded.as_dict() == {
        "state": "DEGRADED",
        "reason": "OPTIONAL_SUBSYSTEM",
        "detail": "GPS unavailable",
        "sequence": 3,
        "config_schema_version": 1,
        "notification_failures": 0,
    }


def test_status_store_refuses_invalid_or_misleading_transitions() -> None:
    store = RecorderStatusStore()
    store.transition(RecorderState.STOPPING)

    with pytest.raises(RecorderStatusTransitionError, match="cannot transition"):
        store.transition(RecorderState.RECORDING)
    with pytest.raises(RecorderStatusTransitionError, match="requires a reason"):
        RecorderStatus(state=RecorderState.FAULTED, sequence=1)
    with pytest.raises(RecorderStatusTransitionError, match="cannot carry"):
        RecorderStatus(
            state=RecorderState.RECORDING,
            sequence=1,
            reason=RecorderReason.RUNTIME_FAILED,
        )


@pytest.mark.parametrize(
    "detail",
    ["", "line one\nline two", "x" * (MAX_STATUS_DETAIL_CHARS + 1)],
)
def test_status_detail_is_bounded_and_single_line(detail: str) -> None:
    with pytest.raises(RecorderStatusTransitionError, match="detail"):
        RecorderStatus(
            state=RecorderState.FAULTED,
            sequence=1,
            reason=RecorderReason.RUNTIME_FAILED,
            detail=detail,
        )


def test_notification_failures_are_orthogonal_to_lifecycle() -> None:
    store = RecorderStatusStore()
    before = store.transition(RecorderState.RECORDING, config_schema_version=1)

    after = store.record_notification_failure()

    assert after.state is RecorderState.RECORDING
    assert after.reason is None
    assert after.sequence == before.sequence + 1
    assert after.notification_failures == 1


def test_status_runtime_validation_rejects_invalid_typed_values() -> None:
    with pytest.raises(RecorderStatusTransitionError, match="state"):
        RecorderStatus(state="STARTING", sequence=0)  # type: ignore[arg-type]
    with pytest.raises(RecorderStatusTransitionError, match="sequence"):
        RecorderStatus(state=RecorderState.STARTING, sequence=True)
    with pytest.raises(RecorderStatusTransitionError, match="config_schema_version"):
        RecorderStatus(
            state=RecorderState.STARTING,
            sequence=0,
            config_schema_version=0,
        )
