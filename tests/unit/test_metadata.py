from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from dashcam.metadata.schema import (
    MAX_GPS_SAMPLES,
    MAX_WARNING_LENGTH,
    MAX_WARNINGS,
    AudioSummary,
    ClipSidecar,
    GpsSample,
    GpsSummary,
    MetadataValidationError,
    TimeAnchor,
    TimeAnchorSource,
    VideoSummary,
    write_sidecar_atomic,
)
from dashcam.state import GpsTimeState, SystemClockState, TimestampQuality

START = datetime(2026, 7, 23, 18, 27, tzinfo=UTC)
REPOSITORY_ROOT = Path(__file__).parents[2]
SIDECAR_SCHEMA_PATH = REPOSITORY_ROOT / "schemas" / "clip-sidecar-v1.schema.json"


def video() -> VideoSummary:
    return VideoSummary("h264", 1920, 1080, 30.0, 8_000_000, 7_923_000, 1_800, 0)


def audio() -> AudioSummary:
    return AudioSummary(True, "aac", 48_000, 1, 128_000)


def gps_sample(
    *,
    utc: datetime | None = START + timedelta(milliseconds=200),
    quality: TimestampQuality = TimestampQuality.GPS_ANCHORED,
) -> GpsSample:
    return GpsSample(
        monotonic_ns=1_200_000_000,
        utc=utc,
        timestamp_quality=quality,
        lat_deg=31.76832,
        lon_deg=35.21371,
        speed_mps=15.0,
        course_deg=91.2,
        altitude_m=782.0,
        fix_quality=1,
        satellites=11,
        hdop=0.9,
    )


def anchored_sidecar() -> ClipSidecar:
    return ClipSidecar(
        schema_version=1,
        clip_id=UUID("12345678-1234-5678-9234-567812345678"),
        boot_id=UUID("87654321-4321-6789-a234-678943216789"),
        sequence=123,
        video_file="20260723T182700.000Z_ba1b2c3d4_s000123.mp4",
        metadata_file="20260723T182700.000Z_ba1b2c3d4_s000123.json",
        start_utc=START,
        end_utc=START + timedelta(minutes=1),
        start_monotonic_ns=1_000_000_000,
        end_monotonic_ns=61_000_000_000,
        gps_time_state=GpsTimeState.GPS_TIME_VALID,
        system_clock_state=SystemClockState.SYNCHRONIZED,
        timestamp_quality=TimestampQuality.GPS_ANCHORED,
        time_anchor=TimeAnchor(
            TimeAnchorSource.GPS,
            1_100_000_000,
            START + timedelta(milliseconds=100),
            50_000_000,
            "validated NMEA RMC date/time",
        ),
        timezone="Asia/Jerusalem",
        start_local=START.astimezone(ZoneInfo("Asia/Jerusalem")),
        video=video(),
        audio=audio(),
        gps=GpsSummary(True, START + timedelta(milliseconds=200), (gps_sample(),)),
        protected=False,
        protection_reason=None,
        software_version="v0.1.0-test",
        warnings=(),
    )


def unsynced_sidecar() -> ClipSidecar:
    base = anchored_sidecar()
    return replace(
        base,
        video_file="boot-ba1b2c3d4-000123.partial.mp4",
        metadata_file="boot-ba1b2c3d4-000123.partial.json",
        start_utc=None,
        end_utc=None,
        gps_time_state=GpsTimeState.UNSYNCED,
        system_clock_state=SystemClockState.UNSET,
        timestamp_quality=TimestampQuality.MONOTONIC_ONLY,
        time_anchor=None,
        start_local=None,
        gps=GpsSummary(
            True,
            None,
            (gps_sample(utc=None, quality=TimestampQuality.MONOTONIC_ONLY),),
        ),
    )


def test_canonical_json_is_deterministic_and_has_explicit_identity() -> None:
    sidecar = anchored_sidecar()

    first = sidecar.to_canonical_json()
    second = sidecar.to_canonical_json()
    document = json.loads(first)

    assert first == second
    assert first is second
    assert first.startswith(b'{"audio":')
    assert document["clip_id"] == "12345678-1234-5678-9234-567812345678"
    assert document["metadata_file"].endswith(".json")
    assert document["start_utc"] == "2026-07-23T18:27:00.000Z"
    assert document["start_local"] == "2026-07-23T21:27:00.000+03:00"
    assert document["time_anchor"]["source"] == "GPS"
    assert document["gps"]["samples"][0]["timestamp_quality"] == "GPS_ANCHORED"


def test_replaced_sidecar_does_not_reuse_stale_canonical_bytes() -> None:
    sidecar = anchored_sidecar()
    original = sidecar.to_canonical_json()

    updated = replace(sidecar, software_version="v0.1.1-test")
    updated_bytes = updated.to_canonical_json()

    assert updated_bytes is updated.to_canonical_json()
    assert updated_bytes != original
    assert json.loads(updated_bytes)["software_version"] == "v0.1.1-test"


def test_canonical_document_validates_against_the_public_json_schema() -> None:
    schema = json.loads(SIDECAR_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())

    assert list(validator.iter_errors(anchored_sidecar().to_mapping())) == []
    assert list(validator.iter_errors(unsynced_sidecar().to_mapping())) == []


def test_schema_is_immutable_and_uses_immutable_collections() -> None:
    sidecar = anchored_sidecar()

    with pytest.raises(FrozenInstanceError):
        sidecar.sequence = 124  # type: ignore[misc]
    with pytest.raises(MetadataValidationError, match="immutable tuple"):
        replace(sidecar, warnings=[])  # type: ignore[arg-type]


def test_unsynced_sidecar_preserves_monotonic_time_and_nulls_civil_time() -> None:
    document = json.loads(unsynced_sidecar().to_canonical_json())

    assert document["start_monotonic_ns"] == 1_000_000_000
    assert document["end_monotonic_ns"] == 61_000_000_000
    assert document["start_utc"] is None
    assert document["end_utc"] is None
    assert document["start_local"] is None
    assert document["gps"]["samples"][0]["utc"] is None


@pytest.mark.parametrize("field", ["start_utc", "end_utc", "start_local"])
def test_unsynced_quality_rejects_civil_timestamp(field: str) -> None:
    with pytest.raises(MetadataValidationError, match="null civil time"):
        replace(unsynced_sidecar(), **{field: START})  # type: ignore[arg-type]


def test_derived_quality_requires_matching_anchor_source() -> None:
    sidecar = anchored_sidecar()
    anchor = sidecar.time_anchor
    assert anchor is not None
    system_anchor = replace(anchor, source=TimeAnchorSource.SYSTEM_CLOCK)

    with pytest.raises(MetadataValidationError, match="GPS time anchor"):
        replace(sidecar, time_anchor=system_anchor)

    derived = replace(
        sidecar,
        timestamp_quality=TimestampQuality.SYSTEM_DERIVED,
        time_anchor=system_anchor,
    )
    assert derived.timestamp_quality is TimestampQuality.SYSTEM_DERIVED


def test_utc_and_local_validation_rejects_wrong_offsets_and_zone() -> None:
    sidecar = anchored_sidecar()
    local = START.astimezone(ZoneInfo("Asia/Jerusalem"))

    with pytest.raises(MetadataValidationError, match="UTC offset"):
        replace(sidecar, start_utc=local)
    with pytest.raises(MetadataValidationError, match="does not match"):
        replace(sidecar, start_local=START.astimezone(ZoneInfo("Europe/London")))
    with pytest.raises(MetadataValidationError, match="valid IANA"):
        replace(sidecar, timezone="Mars/Olympus_Mons")
    with pytest.raises(MetadataValidationError, match="durations do not agree"):
        replace(sidecar, end_utc=START + timedelta(seconds=61))


def test_gps_samples_are_ordered_and_bounded_by_the_clip_interval() -> None:
    sidecar = anchored_sidecar()
    sample = gps_sample()

    before_clip = replace(sample, monotonic_ns=sidecar.start_monotonic_ns - 1)
    with pytest.raises(MetadataValidationError, match="outside the clip interval"):
        replace(sidecar, gps=GpsSummary(True, START, (before_clip,)))

    at_exclusive_end = replace(sample, monotonic_ns=sidecar.end_monotonic_ns)
    with pytest.raises(MetadataValidationError, match="outside the clip interval"):
        replace(sidecar, gps=GpsSummary(True, START, (at_exclusive_end,)))

    later = replace(sample, monotonic_ns=sample.monotonic_ns + 1)
    with pytest.raises(MetadataValidationError, match="ordered"):
        replace(sidecar, gps=GpsSummary(True, START, (later, sample)))

    wrong_utc = replace(sample, utc=START + timedelta(minutes=2))
    with pytest.raises(MetadataValidationError, match="sample UTC"):
        replace(sidecar, gps=GpsSummary(True, START, (wrong_utc,)))


def test_mp4_json_identity_and_sequence_are_strict() -> None:
    sidecar = anchored_sidecar()

    with pytest.raises(MetadataValidationError, match="safe pair"):
        replace(sidecar, metadata_file="different.json")
    with pytest.raises(MetadataValidationError, match="sequence"):
        replace(sidecar, sequence=True)
    with pytest.raises(MetadataValidationError, match="sequence does not match"):
        replace(sidecar, sequence=124)
    with pytest.raises(MetadataValidationError, match="safe pair"):
        replace(sidecar, video_file="ordinary.mp4", metadata_file="ordinary.json")
    with pytest.raises(MetadataValidationError, match="filename UTC"):
        replace(
            sidecar,
            video_file="20260723T182701.000Z_ba1b2c3d4_s000123.mp4",
            metadata_file="20260723T182701.000Z_ba1b2c3d4_s000123.json",
        )


def test_audio_availability_is_consistent() -> None:
    assert AudioSummary(False, None, None, None, None).available is False
    with pytest.raises(MetadataValidationError, match="must not claim"):
        AudioSummary(False, "aac", None, None, None)
    with pytest.raises(MetadataValidationError, match="requires a codec"):
        AudioSummary(True, None, 48_000, 1, 128_000)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"lat_deg": float("nan")}, "lat_deg"),
        ({"lon_deg": 181.0}, "lon_deg"),
        ({"speed_mps": -0.1}, "speed_mps"),
        ({"course_deg": 360.0}, "course_deg"),
        ({"lat_deg": None}, "latitude and longitude"),
        ({"utc": None}, "requires UTC"),
    ],
)
def test_gps_sample_rejects_nonfinite_inconsistent_or_out_of_range_values(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(MetadataValidationError, match=message):
        replace(gps_sample(), **changes)  # type: ignore[arg-type]


def test_sample_and_warning_bounds_accept_limit_and_reject_limit_plus_one() -> None:
    sample = gps_sample()
    summary = GpsSummary(True, START, (sample,) * MAX_GPS_SAMPLES)
    assert len(summary.samples) == MAX_GPS_SAMPLES
    with pytest.raises(MetadataValidationError, match="per-clip bound"):
        GpsSummary(True, START, (sample,) * (MAX_GPS_SAMPLES + 1))

    sidecar = anchored_sidecar()
    warnings = ("x" * MAX_WARNING_LENGTH,) * MAX_WARNINGS
    assert len(replace(sidecar, warnings=warnings).warnings) == MAX_WARNINGS
    with pytest.raises(MetadataValidationError, match="per-clip bound"):
        replace(sidecar, warnings=("x",) * (MAX_WARNINGS + 1))
    with pytest.raises(MetadataValidationError, match="bounded string"):
        replace(sidecar, warnings=("x" * (MAX_WARNING_LENGTH + 1),))


def test_protection_fields_must_agree() -> None:
    sidecar = anchored_sidecar()

    with pytest.raises(MetadataValidationError, match="require"):
        replace(sidecar, protected=True)
    protected = replace(sidecar, protected=True, protection_reason="manual event")
    assert protected.protection_reason == "manual event"
    with pytest.raises(MetadataValidationError, match="cannot have"):
        replace(sidecar, protection_reason="manual event")


def test_atomic_writer_creates_exact_canonical_bytes_and_no_temp(tmp_path: Path) -> None:
    sidecar = anchored_sidecar()
    destination = tmp_path / sidecar.metadata_file

    write_sidecar_atomic(sidecar, destination)

    assert destination.read_bytes() == sidecar.to_canonical_json()
    assert list(tmp_path.iterdir()) == [destination]


def test_atomic_writer_refuses_overwrite_by_default_and_reconciliation_is_explicit(
    tmp_path: Path,
) -> None:
    sidecar = anchored_sidecar()
    destination = tmp_path / sidecar.metadata_file
    destination.write_bytes(b"old")

    with pytest.raises(FileExistsError):
        write_sidecar_atomic(sidecar, destination)
    assert destination.read_bytes() == b"old"

    write_sidecar_atomic(sidecar, destination, replace_existing=True)
    assert destination.read_bytes() == sidecar.to_canonical_json()


def test_atomic_writer_failure_preserves_existing_file_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sidecar = anchored_sidecar()
    destination = tmp_path / sidecar.metadata_file
    destination.write_bytes(b"old")

    def fail_replace(_source: object, _destination: object) -> None:
        raise OSError("injected replacement failure")

    monkeypatch.setattr("dashcam.metadata.schema.os.replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        write_sidecar_atomic(sidecar, destination, replace_existing=True)

    assert destination.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [destination]


def test_atomic_writer_rejects_wrong_destination_and_missing_parent(tmp_path: Path) -> None:
    sidecar = anchored_sidecar()

    with pytest.raises(OSError, match="does not match"):
        write_sidecar_atomic(sidecar, tmp_path / "other.json")
    with pytest.raises(OSError, match="does not exist"):
        write_sidecar_atomic(sidecar, tmp_path / "missing" / sidecar.metadata_file)
