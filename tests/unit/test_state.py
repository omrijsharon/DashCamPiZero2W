from __future__ import annotations

from uuid import UUID

import pytest

from dashcam.state import (
    MAX_DOWNLOAD_LEASE_NS,
    NANOSECONDS_PER_SECOND,
    ClipLifecycle,
    ClipRecord,
    DownloadLeaseError,
    StateTransitionError,
)


def _finalized_clip() -> ClipRecord:
    clip = ClipRecord.create(UUID("12345678-1234-5678-1234-567812345678"))
    writing = clip.transition_to(ClipLifecycle.WRITING)
    finalizing = writing.transition_to(ClipLifecycle.FINALIZING)
    return finalizing.transition_to(ClipLifecycle.FINALIZED)


def test_valid_lifecycle_transition_preserves_uuid() -> None:
    clip = ClipRecord.create(UUID("12345678-1234-5678-1234-567812345678"))

    writing = clip.transition_to(ClipLifecycle.WRITING)

    assert writing.lifecycle is ClipLifecycle.WRITING
    assert writing.clip_id == clip.clip_id
    assert writing is not clip


def test_invalid_lifecycle_transition_is_refused() -> None:
    with pytest.raises(StateTransitionError, match="cannot transition"):
        ClipRecord.create().transition_to(ClipLifecycle.FINALIZED)


def test_protection_is_orthogonal_to_lifecycle() -> None:
    clip = ClipRecord.create().set_protected(True)

    assert clip.lifecycle is ClipLifecycle.CREATING
    assert clip.protected is True
    assert clip.set_protected(False).protected is False


def test_download_lease_is_bounded_and_expires_by_monotonic_time() -> None:
    clip = _finalized_clip().acquire_download_lease(
        holder="web-request-1",
        monotonic_now_ns=100 * NANOSECONDS_PER_SECOND,
        duration_ns=10 * NANOSECONDS_PER_SECOND,
    )

    assert clip.has_active_download_lease(110 * NANOSECONDS_PER_SECOND - 1)
    assert not clip.has_active_download_lease(110 * NANOSECONDS_PER_SECOND)
    assert clip.clear_expired_download_lease(110 * NANOSECONDS_PER_SECOND).download_lease is None
    with pytest.raises(DownloadLeaseError, match="bounded"):
        _finalized_clip().acquire_download_lease(
            holder="web-request-1",
            monotonic_now_ns=0,
            duration_ns=MAX_DOWNLOAD_LEASE_NS + 1,
        )


def test_active_lease_blocks_replacement_and_retention() -> None:
    clip = _finalized_clip().acquire_download_lease(
        holder="client", monotonic_now_ns=0, duration_ns=NANOSECONDS_PER_SECOND
    )

    assert not clip.is_retention_eligible(NANOSECONDS_PER_SECOND - 1)
    assert clip.is_retention_eligible(NANOSECONDS_PER_SECOND)
    with pytest.raises(DownloadLeaseError, match="active"):
        clip.acquire_download_lease(
            holder="other",
            monotonic_now_ns=NANOSECONDS_PER_SECOND - 1,
            duration_ns=NANOSECONDS_PER_SECOND,
        )


def test_only_finalized_clips_may_be_leased() -> None:
    with pytest.raises(DownloadLeaseError, match="only finalized"):
        ClipRecord.create().acquire_download_lease(
            holder="client", monotonic_now_ns=0, duration_ns=NANOSECONDS_PER_SECOND
        )


def test_runtime_model_rejects_invalid_typed_values() -> None:
    with pytest.raises(StateTransitionError, match="UUID"):
        ClipRecord(clip_id="not-a-uuid")  # type: ignore[arg-type]
    with pytest.raises(StateTransitionError, match="boolean"):
        ClipRecord.create().set_protected(1)  # type: ignore[arg-type]
    with pytest.raises(StateTransitionError, match="target"):
        ClipRecord.create().transition_to("FINALIZED")  # type: ignore[arg-type]
    with pytest.raises(DownloadLeaseError, match="integer"):
        _finalized_clip().acquire_download_lease(
            holder="client",
            monotonic_now_ns=1.5,  # type: ignore[arg-type]
            duration_ns=NANOSECONDS_PER_SECOND,
        )
