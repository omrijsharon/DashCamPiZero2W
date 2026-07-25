"""Bounded, renderer-independent overlay formatting for the 1080p profile."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from dashcam.gps.clock import LocalTimeView
from dashcam.state import GpsState, GpsTimeState, TimestampQuality

_MAX_LINE_CHARS: Final = 96
_MAX_COORDINATE_DECIMALS: Final = 8
_KILOMETRES_PER_HOUR_PER_MPS: Final = 3.6
_MILES_PER_HOUR_PER_MPS: Final = 2.2369362920544


class OverlayFormatError(ValueError):
    """Raised when a layout or formatter input violates the fixed contract."""


@dataclass(frozen=True, slots=True)
class OverlayLayout:
    """A fixed, pre-renderable two-line region for the 1920x1080 production mode."""

    frame_width_px: int = 1920
    frame_height_px: int = 1080
    origin_x_px: int = 40
    origin_y_px: int = 40
    region_width_px: int = 1536
    region_height_px: int = 64
    glyph_width_px: int = 16
    line_height_px: int = 32
    max_line_chars: int = _MAX_LINE_CHARS

    def __post_init__(self) -> None:
        values = (
            self.frame_width_px,
            self.frame_height_px,
            self.origin_x_px,
            self.origin_y_px,
            self.region_width_px,
            self.region_height_px,
            self.glyph_width_px,
            self.line_height_px,
            self.max_line_chars,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
            raise OverlayFormatError("layout dimensions must be integers")
        if self.frame_width_px != 1920 or self.frame_height_px != 1080:
            raise OverlayFormatError("layout is restricted to the 1920x1080 profile")
        if min(values) <= 0 or self.origin_x_px < 0 or self.origin_y_px < 0:
            raise OverlayFormatError("layout dimensions must be positive")
        if self.max_line_chars * self.glyph_width_px > self.region_width_px:
            raise OverlayFormatError("line character bound exceeds the overlay region")
        if 2 * self.line_height_px > self.region_height_px:
            raise OverlayFormatError("two text lines exceed the overlay region")
        if (
            self.origin_x_px + self.region_width_px > self.frame_width_px
            or self.origin_y_px + self.region_height_px > self.frame_height_px
        ):
            raise OverlayFormatError("overlay region exceeds the 1080p frame")


OVERLAY_1080P_LAYOUT: Final = OverlayLayout()


@dataclass(frozen=True, slots=True)
class OverlayOptions:
    """The text-affecting subset of the version-1 overlay configuration."""

    show_local_datetime: bool = True
    show_utc_offset: bool = True
    show_rec: bool = True
    show_speed: bool = True
    speed_unit: str = "kmh"
    show_coordinates: bool = True
    coordinate_decimals: int = 5
    show_altitude: bool = True
    show_satellites: bool = True
    show_hdop: bool = False

    def __post_init__(self) -> None:
        if self.speed_unit not in {"kmh", "mph"}:
            raise OverlayFormatError("speed_unit must be kmh or mph")
        if (
            isinstance(self.coordinate_decimals, bool)
            or not isinstance(self.coordinate_decimals, int)
            or not 0 <= self.coordinate_decimals <= _MAX_COORDINATE_DECIMALS
        ):
            raise OverlayFormatError("coordinate_decimals must be between 0 and 8")
        if not all(
            isinstance(value, bool)
            for value in (
                self.show_local_datetime,
                self.show_utc_offset,
                self.show_rec,
                self.show_speed,
                self.show_coordinates,
                self.show_altitude,
                self.show_satellites,
                self.show_hdop,
            )
        ):
            raise OverlayFormatError("overlay visibility options must be booleans")


DEFAULT_OVERLAY_OPTIONS: Final = OverlayOptions()


@dataclass(frozen=True, slots=True)
class OverlayTelemetry:
    """A single already-coherent telemetry snapshot supplied by ``dashcamd``."""

    gps_time_state: GpsTimeState
    timestamp_quality: TimestampQuality
    gps_state: GpsState
    local_time: LocalTimeView | None = None
    latitude_deg: float | None = None
    longitude_deg: float | None = None
    speed_mps: float | None = None
    altitude_m: float | None = None
    satellites: int | None = None
    hdop: float | None = None


@dataclass(frozen=True, slots=True)
class OverlayFrame:
    """Validated text for the two fixed overlay rows; rendering remains separate."""

    top_line: str
    bottom_line: str
    layout: OverlayLayout = OVERLAY_1080P_LAYOUT

    def __post_init__(self) -> None:
        _validate_line(self.top_line, self.layout)
        _validate_line(self.bottom_line, self.layout)


def format_utc_offset(offset_seconds: int) -> str:
    """Format a numeric ISO-8601-style offset, never a timezone abbreviation."""

    if (
        isinstance(offset_seconds, bool)
        or not isinstance(offset_seconds, int)
        or abs(offset_seconds) > 18 * 60 * 60
        or offset_seconds % 60 != 0
    ):
        raise OverlayFormatError("UTC offset must be a whole minute within +/-18 hours")
    sign = "+" if offset_seconds >= 0 else "-"
    hours, seconds = divmod(abs(offset_seconds), 60 * 60)
    minutes = seconds // 60
    return f"{sign}{hours:02d}:{minutes:02d}"


def build_overlay(
    telemetry: OverlayTelemetry,
    options: OverlayOptions = DEFAULT_OVERLAY_OPTIONS,
    layout: OverlayLayout = OVERLAY_1080P_LAYOUT,
) -> OverlayFrame:
    """Build safe display text without retaining or rendering any frame data.

    Invalid or stale navigation measurements intentionally collapse to a status
    marker.  This prevents a previous coordinate or speed from being presented
    as a current fix while keeping recording independent of GPS health.
    """

    if not isinstance(telemetry, OverlayTelemetry):
        raise OverlayFormatError("telemetry must be an OverlayTelemetry")
    if not isinstance(options, OverlayOptions) or not isinstance(layout, OverlayLayout):
        raise OverlayFormatError("options and layout must use overlay contract types")

    top_parts = _time_parts(telemetry, options)
    if options.show_rec:
        top_parts.append("REC")
    bottom_parts = _navigation_parts(telemetry, options)
    return OverlayFrame("  ".join(top_parts), "   ".join(bottom_parts), layout)


def _time_parts(telemetry: OverlayTelemetry, options: OverlayOptions) -> list[str]:
    if not options.show_local_datetime:
        return []
    if (
        telemetry.gps_time_state is GpsTimeState.UNSYNCED
        or telemetry.timestamp_quality is TimestampQuality.MONOTONIC_ONLY
        or telemetry.local_time is None
    ):
        return ["TIME UNSYNCED"]
    try:
        local = telemetry.local_time.datetime
        if not isinstance(local, datetime) or local.utcoffset() is None:
            raise OverlayFormatError("local time is invalid")
        parts = [local.strftime("%Y-%m-%d %H:%M:%S")]
        if options.show_utc_offset:
            parts.append(format_utc_offset(telemetry.local_time.utc_offset_seconds))
        return parts
    except (OverlayFormatError, ValueError):
        return ["TIME UNSYNCED"]


def _navigation_parts(telemetry: OverlayTelemetry, options: OverlayOptions) -> list[str]:
    if (
        telemetry.gps_time_state is GpsTimeState.GPS_TIME_STALE
        or telemetry.gps_state is GpsState.STALE
    ):
        return ["GPS LOST"]
    if telemetry.gps_state is not GpsState.NAVIGATION_VALID:
        return ["GPS INVALID"]
    try:
        return _valid_navigation_parts(telemetry, options)
    except OverlayFormatError:
        return ["GPS INVALID"]


def _valid_navigation_parts(telemetry: OverlayTelemetry, options: OverlayOptions) -> list[str]:
    parts: list[str] = []
    if options.show_coordinates:
        parts.append(
            _format_coordinates(
                telemetry.latitude_deg,
                telemetry.longitude_deg,
                options.coordinate_decimals,
            )
        )
    if options.show_speed:
        parts.append(_format_speed(telemetry.speed_mps, options.speed_unit))
    if options.show_altitude and telemetry.altitude_m is not None:
        altitude_m = _finite(telemetry.altitude_m, "altitude_m")
        parts.append(f"ALT {altitude_m:.0f} m")
    if options.show_satellites and telemetry.satellites is not None:
        if isinstance(telemetry.satellites, bool) or not 0 <= telemetry.satellites <= 99:
            raise OverlayFormatError("satellites must be between 0 and 99")
        parts.append(f"SAT {telemetry.satellites}")
    if options.show_hdop and telemetry.hdop is not None:
        hdop = _finite(telemetry.hdop, "hdop")
        if not 0 <= hdop <= 100:
            raise OverlayFormatError("hdop must be between 0 and 100")
        parts.append(f"HDOP {hdop:.1f}")
    return parts or ["GPS VALID"]


def _format_coordinates(
    latitude_deg: float | None, longitude_deg: float | None, decimals: int
) -> str:
    latitude = _finite(latitude_deg, "latitude_deg")
    longitude = _finite(longitude_deg, "longitude_deg")
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise OverlayFormatError("coordinates are outside legal ranges")
    return f"{latitude:.{decimals}f}, {longitude:.{decimals}f}"


def _format_speed(speed_mps: float | None, unit: str) -> str:
    speed = _finite(speed_mps, "speed_mps")
    if speed < 0 or speed > 2_000:
        raise OverlayFormatError("speed_mps is outside the supported range")
    if unit == "kmh":
        return f"{speed * _KILOMETRES_PER_HOUR_PER_MPS:.0f} km/h"
    return f"{speed * _MILES_PER_HOUR_PER_MPS:.0f} mph"


def _finite(value: float | None, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise OverlayFormatError(f"{name} must be finite")
    return float(value)


def _validate_line(line: str, layout: OverlayLayout) -> None:
    if (
        not isinstance(line, str)
        or len(line) > layout.max_line_chars
        or not line.isascii()
        or any(not character.isprintable() for character in line)
    ):
        raise OverlayFormatError("overlay text exceeds the fixed printable ASCII bounds")
