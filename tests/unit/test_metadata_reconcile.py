from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from dashcam.metadata.reconcile import (
    MAX_SIDECAR_BYTES,
    MetadataReconciliationError,
    SidecarParseError,
    parse_sidecar_bytes,
    parse_sidecar_mapping,
    plan_post_anchor_reconciliation,
)
from dashcam.metadata.schema import (
    AudioSummary,
    ClipSidecar,
    GpsSample,
    GpsSummary,
    TimeAnchor,
    TimeAnchorSource,
    VideoSummary,
)
from dashcam.state import GpsTimeState, SystemClockState, TimestampQuality
from dashcam.storage.intents import IntentKind, MemberObservation, plan_reconciliation

CLIP_ID = UUID("12345678-1234-5678-9234-567812345678")
BOOT_ID = UUID("87654321-4321-6789-a234-678943216789")
INTENT_ID = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")


def _unsynced_sidecar(
    *,
    start_monotonic_ns: int = 10_000_000_000,
    end_monotonic_ns: int = 70_000_000_000,
) -> ClipSidecar:
    sample = GpsSample(
        monotonic_ns=start_monotonic_ns + 1_000_000_000,
        utc=None,
        timestamp_quality=TimestampQuality.MONOTONIC_ONLY,
        lat_deg=31.76832,
        lon_deg=35.21371,
        speed_mps=15.0,
        course_deg=91.2,
        altitude_m=782.0,
        fix_quality=1,
        satellites=11,
        hdop=0.9,
    )
    return ClipSidecar(
        schema_version=1,
        clip_id=CLIP_ID,
        boot_id=BOOT_ID,
        sequence=123,
        video_file="boot-876543214321-000123.partial.mp4",
        metadata_file="boot-876543214321-000123.partial.json",
        start_utc=None,
        end_utc=None,
        start_monotonic_ns=start_monotonic_ns,
        end_monotonic_ns=end_monotonic_ns,
        gps_time_state=GpsTimeState.UNSYNCED,
        system_clock_state=SystemClockState.UNSET,
        timestamp_quality=TimestampQuality.MONOTONIC_ONLY,
        time_anchor=None,
        timezone="Asia/Jerusalem",
        start_local=None,
        video=VideoSummary("h264", 1920, 1080, 30.0, 8_000_000, 7_923_000, 1_800, 0),
        audio=AudioSummary(False, None, None, None, None),
        gps=GpsSummary(True, None, (sample,)),
        protected=False,
        protection_reason=None,
        software_version="v0.1.0-test",
        warnings=(),
    )


def _gps_anchor(*, monotonic_ns: int, utc: datetime) -> TimeAnchor:
    return TimeAnchor(
        source=TimeAnchorSource.GPS,
        monotonic_ns=monotonic_ns,
        utc=utc,
        uncertainty_ns=50_000_000,
        provenance="validated NMEA RMC date/time",
    )


def test_strict_parser_round_trips_canonical_mapping_and_bytes() -> None:
    sidecar = _unsynced_sidecar()

    assert parse_sidecar_mapping(sidecar.to_mapping()) == sidecar
    assert parse_sidecar_bytes(sidecar.to_canonical_json()) == sidecar


@pytest.mark.parametrize(
    "mutation",
    [
        lambda raw: raw.pop("clip_id"),
        lambda raw: raw.update(unexpected=True),
        lambda raw: cast_dict(raw["video"]).update(unexpected=True),
        lambda raw: cast_dict(raw["gps"]["samples"][0]).pop("utc"),
    ],
)
def test_strict_parser_rejects_missing_and_unknown_fields(mutation: object) -> None:
    raw = _unsynced_sidecar().to_mapping()
    assert callable(mutation)
    mutation(raw)

    with pytest.raises(SidecarParseError, match="keys differ"):
        parse_sidecar_mapping(raw)


def test_parser_rejects_inconsistent_filenames_and_times() -> None:
    raw = _unsynced_sidecar().to_mapping()
    raw["sequence"] = 124
    with pytest.raises(SidecarParseError, match="sequence"):
        parse_sidecar_mapping(raw)

    anchored = plan_post_anchor_reconciliation(
        _unsynced_sidecar(),
        anchor=_gps_anchor(
            monotonic_ns=10_000_000_000,
            utc=datetime(2026, 7, 23, 18, 27, tzinfo=UTC),
        ),
        intent_id=INTENT_ID,
        created_monotonic_ns=80_000_000_000,
    ).sidecar
    wrong_time = anchored.to_mapping()
    wrong_time["start_utc"] = "2026-07-23T18:27:01.000Z"
    with pytest.raises(SidecarParseError, match="filename UTC"):
        parse_sidecar_mapping(wrong_time)


def test_parser_rejects_malformed_noncanonical_and_oversized_json() -> None:
    with pytest.raises(SidecarParseError, match="malformed"):
        parse_sidecar_bytes(b'{"schema_version":')
    with pytest.raises(SidecarParseError, match="duplicate"):
        parse_sidecar_bytes(b'{"schema_version":1,"schema_version":1}')
    with pytest.raises(SidecarParseError, match="canonical byte"):
        parse_sidecar_bytes(json.dumps(_unsynced_sidecar().to_mapping()).encode())
    with pytest.raises(SidecarParseError, match="byte bound"):
        parse_sidecar_bytes(b"{" + b" " * MAX_SIDECAR_BYTES + b"}")


def test_late_gps_anchor_projects_backward_and_creates_recoverable_pair_intent() -> None:
    sidecar = _unsynced_sidecar()
    anchor = _gps_anchor(
        monotonic_ns=130_000_000_000,
        utc=datetime(2026, 7, 23, 18, 29, tzinfo=UTC),
    )

    result = plan_post_anchor_reconciliation(
        sidecar,
        anchor=anchor,
        intent_id=INTENT_ID,
        created_monotonic_ns=131_000_000_000,
    )

    assert result.sidecar.clip_id == CLIP_ID
    assert result.sidecar.start_utc == datetime(2026, 7, 23, 18, 27, tzinfo=UTC)
    assert result.sidecar.end_utc == datetime(2026, 7, 23, 18, 28, tzinfo=UTC)
    assert result.sidecar.timestamp_quality is TimestampQuality.GPS_ANCHORED
    assert result.sidecar.gps.samples[0].utc == datetime(2026, 7, 23, 18, 27, 1, tzinfo=UTC)
    assert result.sidecar.gps.first_fix_utc == result.sidecar.gps.samples[0].utc
    assert parse_sidecar_bytes(result.sidecar.to_canonical_json()) == result.sidecar
    assert result.intent is not None
    assert result.intent.kind is IntentKind.RECONCILE_NAME
    assert result.intent.clip_id == CLIP_ID
    assert result.intent.paths.video_source.endswith(sidecar.video_file)
    assert result.intent.paths.video_target is not None
    assert result.intent.paths.video_target.endswith(result.sidecar.video_file)

    interrupted = plan_reconciliation(
        result.intent,
        video=MemberObservation(source_exists=False, target_exists=True),
        sidecar=MemberObservation(source_exists=True, target_exists=False),
    )
    assert len(interrupted.actions) == 1
    assert not interrupted.complete


@pytest.mark.parametrize(
    ("start_utc", "expected_local", "expected_offset"),
    [
        (
            datetime(2026, 3, 27, 0, 30, tzinfo=UTC),
            datetime(2026, 3, 27, 3, 30),
            timedelta(hours=3),
        ),
        (
            datetime(2026, 10, 25, 0, 30, tzinfo=UTC),
            datetime(2026, 10, 25, 2, 30),
            timedelta(hours=2),
        ),
        (
            datetime(2026, 7, 23, 23, 59, 30, tzinfo=UTC),
            datetime(2026, 7, 24, 2, 59, 30),
            timedelta(hours=3),
        ),
    ],
)
def test_reconciliation_uses_iana_dst_and_handles_utc_midnight(
    start_utc: datetime, expected_local: datetime, expected_offset: timedelta
) -> None:
    result = plan_post_anchor_reconciliation(
        _unsynced_sidecar(),
        anchor=_gps_anchor(monotonic_ns=10_000_000_000, utc=start_utc),
        intent_id=INTENT_ID,
        created_monotonic_ns=80_000_000_000,
    )

    assert result.sidecar.start_local is not None
    assert result.sidecar.start_local.replace(tzinfo=None) == expected_local
    assert result.sidecar.start_local.utcoffset() == expected_offset
    assert result.sidecar.start_local.tzinfo == ZoneInfo("Asia/Jerusalem")


def test_synchronized_system_anchor_marks_system_derived_time() -> None:
    anchor = TimeAnchor(
        TimeAnchorSource.SYSTEM_CLOCK,
        10_000_000_000,
        datetime(2026, 7, 23, 18, 27, tzinfo=UTC),
        100_000_000,
        "chrony synchronized realtime sample",
    )

    result = plan_post_anchor_reconciliation(
        _unsynced_sidecar(),
        anchor=anchor,
        intent_id=INTENT_ID,
        created_monotonic_ns=80_000_000_000,
    )

    assert result.sidecar.timestamp_quality is TimestampQuality.SYSTEM_DERIVED
    assert result.sidecar.system_clock_state is SystemClockState.SYNCHRONIZED
    assert result.sidecar.gps_time_state is GpsTimeState.UNSYNCED
    assert result.sidecar.gps.samples[0].timestamp_quality is TimestampQuality.SYSTEM_DERIVED


def test_case_insensitive_collision_is_refused_for_either_target_member() -> None:
    sidecar = _unsynced_sidecar()
    anchor = _gps_anchor(
        monotonic_ns=sidecar.start_monotonic_ns,
        utc=datetime(2026, 7, 23, 18, 27, tzinfo=UTC),
    )
    target_name = "20260723T182700.000Z_876543214321_s000123.JSON"

    with pytest.raises(MetadataReconciliationError, match="collision"):
        plan_post_anchor_reconciliation(
            sidecar,
            anchor=anchor,
            intent_id=INTENT_ID,
            created_monotonic_ns=80_000_000_000,
            existing_names={target_name},
        )


def test_reconciliation_refuses_filename_boot_token_that_disagrees_with_uuid() -> None:
    sidecar = replace(
        _unsynced_sidecar(),
        video_file="boot-deadbeefcafe-000123.partial.mp4",
        metadata_file="boot-deadbeefcafe-000123.partial.json",
    )

    with pytest.raises(MetadataReconciliationError, match="boot token"):
        plan_post_anchor_reconciliation(
            sidecar,
            anchor=_gps_anchor(
                monotonic_ns=sidecar.start_monotonic_ns,
                utc=datetime(2026, 7, 23, 18, 27, tzinfo=UTC),
            ),
            intent_id=INTENT_ID,
            created_monotonic_ns=80_000_000_000,
        )


@pytest.mark.parametrize("directory", ["../clips", "pending", "quarantine", "other"])
def test_reconciliation_refuses_nonfinal_managed_directories(directory: str) -> None:
    with pytest.raises(MetadataReconciliationError, match="directory"):
        plan_post_anchor_reconciliation(
            _unsynced_sidecar(),
            anchor=_gps_anchor(
                monotonic_ns=10_000_000_000,
                utc=datetime(2026, 7, 23, 18, 27, tzinfo=UTC),
            ),
            intent_id=INTENT_ID,
            created_monotonic_ns=80_000_000_000,
            directory=directory,
        )


def test_repeated_reconciliation_is_idempotent_and_keeps_uuid_and_name() -> None:
    anchor = _gps_anchor(
        monotonic_ns=10_000_000_000,
        utc=datetime(2026, 7, 23, 18, 27, tzinfo=UTC),
    )
    first = plan_post_anchor_reconciliation(
        _unsynced_sidecar(),
        anchor=anchor,
        intent_id=INTENT_ID,
        created_monotonic_ns=80_000_000_000,
    )
    noisy_later_anchor = replace(
        anchor,
        monotonic_ns=20_000_000_000,
        utc=datetime(2026, 7, 23, 18, 27, 10, 500_000, tzinfo=UTC),
    )

    replay = plan_post_anchor_reconciliation(
        first.sidecar,
        anchor=noisy_later_anchor,
        intent_id=UUID(int=99),
        created_monotonic_ns=90_000_000_000,
        existing_names={first.sidecar.video_file.upper()},
    )

    assert replay.already_reconciled
    assert replay.intent is None
    assert replay.sidecar is first.sidecar
    assert replay.sidecar.clip_id == CLIP_ID
    assert replay.sidecar.video_file == first.sidecar.video_file


def cast_dict(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value
