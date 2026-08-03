from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from dashcam.gps.clock import LocalTimeView
from dashcam.overlay.formatting import (
    OVERLAY_1080P_LAYOUT,
    OverlayFormatError,
    OverlayLayout,
    OverlayOptions,
    OverlayTelemetry,
    build_overlay,
    format_utc_offset,
)
from dashcam.state import GpsState, GpsTimeState, TimestampQuality


def _local(offset_seconds: int = 10_800) -> LocalTimeView:
    value = datetime(2026, 7, 23, 21, 27, 4, tzinfo=UTC).astimezone(
        timezone(timedelta(seconds=offset_seconds))
    )
    return LocalTimeView(value, "Asia/Jerusalem", offset_seconds, "IDT", True)


def _valid_telemetry(**changes: object) -> OverlayTelemetry:
    values: dict[str, object] = {
        "gps_time_state": GpsTimeState.GPS_TIME_VALID,
        "timestamp_quality": TimestampQuality.GPS_ANCHORED,
        "gps_state": GpsState.NAVIGATION_VALID,
        "local_time": _local(),
        "latitude_deg": 31.76832,
        "longitude_deg": 35.21371,
        "speed_mps": 15.0,
        "altitude_m": 782.0,
        "satellites": 11,
        "hdop": 0.9,
    }
    values.update(changes)
    return OverlayTelemetry(**values)  # type: ignore[arg-type]


def test_default_format_matches_contract_with_numeric_utc_offset_and_units() -> None:
    frame = build_overlay(_valid_telemetry())

    assert frame.top_line == "2026-07-24 00:27:04  +03:00  REC"
    assert frame.bottom_line == "31.76832, 35.21371   54 km/h   ALT 782 m   SAT 11"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (-64_800, "-18:00"),
        (-19_800, "-05:30"),
        (0, "+00:00"),
        (20_700, "+05:45"),
        (64_800, "+18:00"),
    ],
)
def test_numeric_utc_offsets_are_signed_and_zero_padded(seconds: int, expected: str) -> None:
    assert format_utc_offset(seconds) == expected


@pytest.mark.parametrize("seconds", [-64_801, 61, 64_801, True])
def test_invalid_utc_offsets_are_refused(seconds: int) -> None:
    with pytest.raises(OverlayFormatError):
        format_utc_offset(seconds)


@pytest.mark.parametrize(
    ("changes", "expected_top", "expected_bottom"),
    [
        (
            {
                "gps_time_state": GpsTimeState.UNSYNCED,
                "timestamp_quality": TimestampQuality.MONOTONIC_ONLY,
            },
            "TIME UNSYNCED  REC",
            "31.76832, 35.21371   54 km/h   ALT 782 m   SAT 11",
        ),
        (
            {"gps_state": GpsState.STALE},
            "2026-07-24 00:27:04  +03:00  REC",
            "GPS LOST",
        ),
        (
            {"gps_time_state": GpsTimeState.GPS_TIME_STALE},
            "2026-07-24 00:27:04  +03:00  REC",
            "GPS LOST",
        ),
        (
            {"gps_state": GpsState.RECEIVING_INVALID},
            "2026-07-24 00:27:04  +03:00  REC",
            "GPS INVALID",
        ),
        (
            {"gps_state": GpsState.TIME_VALID_POSITION_INVALID},
            "2026-07-24 00:27:04  +03:00  REC",
            "GPS INVALID",
        ),
    ],
)
def test_time_and_navigation_fault_states_never_replay_stale_values(
    changes: dict[str, object], expected_top: str, expected_bottom: str
) -> None:
    frame = build_overlay(_valid_telemetry(**changes))

    assert frame.top_line == expected_top
    assert frame.bottom_line == expected_bottom


@pytest.mark.parametrize(("unit", "expected"), [("kmh", "54 km/h"), ("mph", "34 mph")])
def test_configurable_speed_units(unit: str, expected: str) -> None:
    frame = build_overlay(_valid_telemetry(), OverlayOptions(speed_unit=unit))
    assert expected in frame.bottom_line


@pytest.mark.parametrize("decimals", range(0, 9))
def test_coordinate_precision_is_bounded_and_exact(decimals: int) -> None:
    frame = build_overlay(_valid_telemetry(), OverlayOptions(coordinate_decimals=decimals))
    coordinate = frame.bottom_line.split("   ")[0]
    latitude, longitude = coordinate.split(", ")
    assert len(latitude.rsplit(".", 1)[1]) == decimals if decimals else "." not in latitude
    assert len(longitude.rsplit(".", 1)[1]) == decimals if decimals else "." not in longitude


@pytest.mark.parametrize(
    ("latitude", "longitude", "speed"),
    [(91.0, 0.0, 0.0), (0.0, -181.0, 0.0), (0.0, 0.0, float("nan"))],
)
def test_invalid_navigation_values_are_marked_invalid(
    latitude: float, longitude: float, speed: float
) -> None:
    frame = build_overlay(
        _valid_telemetry(latitude_deg=latitude, longitude_deg=longitude, speed_mps=speed)
    )
    assert frame.bottom_line == "GPS INVALID"


def test_1080p_layout_is_strict_and_all_generated_default_values_fit() -> None:
    assert (
        OVERLAY_1080P_LAYOUT.max_line_chars * OVERLAY_1080P_LAYOUT.glyph_width_px
        <= OVERLAY_1080P_LAYOUT.region_width_px
    )
    for latitude in (-90.0, 0.0, 90.0):
        for longitude in (-180.0, 0.0, 180.0):
            telemetry = _valid_telemetry(
                latitude_deg=latitude,
                longitude_deg=longitude,
                speed_mps=2_000.0,
                altitude_m=20_000.0,
                satellites=99,
                hdop=100.0,
            )
            frame = build_overlay(
                telemetry,
                OverlayOptions(show_hdop=True, coordinate_decimals=8),
            )
            assert len(frame.top_line) <= OVERLAY_1080P_LAYOUT.max_line_chars
            assert len(frame.bottom_line) <= OVERLAY_1080P_LAYOUT.max_line_chars


def test_layout_bounds_refuse_unrenderable_text_region() -> None:
    with pytest.raises(OverlayFormatError, match="character bound"):
        OverlayLayout(region_width_px=1151)
