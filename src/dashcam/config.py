"""Typed, versioned dashcam configuration with atomic persistence."""

from __future__ import annotations

import copy
import errno
import ipaddress
import json
import math
import os
import re
import tempfile
import tomllib
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, TypeAlias, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dashcam.audio.alsa import AlsaMatchError, parse_alsa_selector

CURRENT_SCHEMA_VERSION: Final = 1
MAX_CONFIG_BYTES: Final = 64 * 1024
CONFIG_FILE_MODE: Final = 0o640
SUPPORTED_GPS_BAUD_RATES: Final = frozenset({4_800, 9_600, 38_400, 57_600, 115_200})

ConfigValue: TypeAlias = str | int | float | bool
ConfigTable: TypeAlias = dict[str, "ConfigValue | ConfigTable"]
Migration: TypeAlias = Callable[[ConfigTable], ConfigTable]

_SECTION_NAMES: Final = (
    "video",
    "audio",
    "gps",
    "time",
    "overlay",
    "preview",
    "storage",
    "network",
    "service",
)
_IANA_NAME = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9_.+-]*/)+[A-Za-z0-9][A-Za-z0-9_.+-]*$")


class ConfigError(ValueError):
    """Raised when configuration cannot be parsed, validated, migrated, or saved."""


class ConfigMigrationError(ConfigError):
    """Raised when no safe migration path exists for a schema version."""


@dataclass(frozen=True, slots=True)
class VideoConfig:
    width: int = 1920
    height: int = 1080
    fps: int = 30
    codec: str = "h264"
    hardware_encoder_required: bool = True
    bitrate_bps: int = 8_000_000
    keyframe_interval_frames: int = 30
    clip_duration_s: int = 60
    container: str = "mp4"


@dataclass(frozen=True, slots=True)
class AudioConfig:
    enabled: bool = True
    device_match: str = (
        "usb:vid=08bb,pid=2902,product=USB_PnP_Sound_Device,"
        "path=platform-3f980000.usb-usb-0:1:1.0"
    )
    sample_rate_hz: int = 48_000
    channels: int = 1
    codec: str = "aac"
    bitrate_bps: int = 128_000


@dataclass(frozen=True, slots=True)
class GpsConfig:
    device: str = "/dev/serial0"
    baud: int = 115_200
    stale_after_s: float = 2.0
    max_sample_hz: int = 10
    anchor_earliest_utc: str = "2024-01-01T00:00:00Z"
    anchor_latest_utc: str = "2100-01-01T00:00:00Z"
    anchor_uncertainty_ms: int = 250
    anchor_max_conflict_ms: int = 2_000
    anchor_max_reacquire_disagreement_ms: int = 5_000
    anchor_max_interval_s: int = 86_400


@dataclass(frozen=True, slots=True)
class TimeConfig:
    timezone: str = "Asia/Jerusalem"
    filename_timezone: str = "UTC"
    discipline_system_clock: bool = False
    system_clock_owner: str = "systemd-timesyncd"


@dataclass(frozen=True, slots=True)
class OverlayConfig:
    enabled: bool = True
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


@dataclass(frozen=True, slots=True)
class PreviewConfig:
    enabled: bool = True
    width: int = 640
    height: int = 360
    fps: int = 15
    max_clients: int = 1
    latency_target_ms: int = 500


@dataclass(frozen=True, slots=True)
class StorageConfig:
    recording_root: str = "/srv/dashcam"
    required_filesystem: str = "exfat"
    required_volume_label: str = "DASHCAM"
    require_distinct_mount: bool = True
    low_watermark_percent: int = 15
    high_watermark_percent: int = 20
    minimum_free_gib: float = 2.0
    emergency_free_mib: int = 256
    download_lease_timeout_s: int = 300
    protect_previous_clips: int = 2
    protect_next_clips: int = 1


@dataclass(frozen=True, slots=True)
class NetworkConfig:
    ap_enabled: bool = True
    ssid_prefix: str = "Dashcam"
    address: str = "192.168.50.1/24"


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    watchdog_s: int = 20
    restart_backoff_min_s: int = 1
    restart_backoff_max_s: int = 60


@dataclass(frozen=True, slots=True)
class DashcamConfig:
    schema_version: int = CURRENT_SCHEMA_VERSION
    device_name: str = "Dashcam"
    video: VideoConfig = VideoConfig()
    audio: AudioConfig = AudioConfig()
    gps: GpsConfig = GpsConfig()
    time: TimeConfig = TimeConfig()
    overlay: OverlayConfig = OverlayConfig()
    preview: PreviewConfig = PreviewConfig()
    storage: StorageConfig = StorageConfig()
    network: NetworkConfig = NetworkConfig()
    service: ServiceConfig = ServiceConfig()


@dataclass(frozen=True, slots=True)
class AtomicWriteResult:
    """Durability details for an atomic write.

    ``directory_fsynced`` is false only when the host does not support opening or
    flushing directories. The file itself is always flushed before replacement.
    """

    directory_fsynced: bool


def default_config() -> DashcamConfig:
    """Return the version-1 default configuration."""

    return DashcamConfig()


def _mapping(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{path} must be a TOML table")
    if not all(isinstance(key, str) for key in value):
        raise ConfigError(f"{path} contains a non-string key")
    return cast(Mapping[str, object], value)


def _allowed(table: Mapping[str, object], allowed: set[str], path: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(f"{path} contains unknown key: {unknown[0]}")
    missing = sorted(allowed - set(table))
    if missing:
        raise ConfigError(f"{path} is missing required key: {missing[0]}")


def _integer(table: Mapping[str, object], key: str, path: str) -> int:
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{path}.{key} must be an integer")
    return value


def _number(table: Mapping[str, object], key: str, path: str) -> float:
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{path}.{key} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ConfigError(f"{path}.{key} must be finite")
    return result


def _boolean(table: Mapping[str, object], key: str, path: str) -> bool:
    value = table[key]
    if not isinstance(value, bool):
        raise ConfigError(f"{path}.{key} must be a boolean")
    return value


def _string(table: Mapping[str, object], key: str, path: str) -> str:
    value = table[key]
    if not isinstance(value, str):
        raise ConfigError(f"{path}.{key} must be a string")
    return value


def _bounded_int(value: int, low: int, high: int, path: str) -> int:
    if not low <= value <= high:
        raise ConfigError(f"{path} must be between {low} and {high}")
    return value


def _bounded_number(value: float, low: float, high: float, path: str) -> float:
    if not low <= value <= high:
        raise ConfigError(f"{path} must be between {low:g} and {high:g}")
    return value


def _nonempty(value: str, max_length: int, path: str) -> str:
    if (
        not value
        or not value.strip()
        or len(value) > max_length
        or not all(character.isprintable() for character in value)
    ):
        raise ConfigError(f"{path} must contain 1 to {max_length} printable characters")
    return value


def _choice(value: str, choices: set[str], path: str) -> str:
    if value not in choices:
        raise ConfigError(f"{path} must be one of: {', '.join(sorted(choices))}")
    return value


def _section(root: Mapping[str, object], name: str, fields: set[str]) -> Mapping[str, object]:
    section = _mapping(root[name], name)
    _allowed(section, fields, name)
    return section


def _video(root: Mapping[str, object]) -> VideoConfig:
    fields = set(VideoConfig.__dataclass_fields__)
    table = _section(root, "video", fields)
    config = VideoConfig(
        width=_bounded_int(_integer(table, "width", "video"), 160, 4096, "video.width"),
        height=_bounded_int(_integer(table, "height", "video"), 120, 2160, "video.height"),
        fps=_bounded_int(_integer(table, "fps", "video"), 1, 60, "video.fps"),
        codec=_choice(_string(table, "codec", "video"), {"h264"}, "video.codec"),
        hardware_encoder_required=_boolean(table, "hardware_encoder_required", "video"),
        bitrate_bps=_bounded_int(
            _integer(table, "bitrate_bps", "video"),
            500_000,
            50_000_000,
            "video.bitrate_bps",
        ),
        keyframe_interval_frames=_bounded_int(
            _integer(table, "keyframe_interval_frames", "video"),
            1,
            600,
            "video.keyframe_interval_frames",
        ),
        clip_duration_s=_bounded_int(
            _integer(table, "clip_duration_s", "video"), 10, 600, "video.clip_duration_s"
        ),
        container=_choice(_string(table, "container", "video"), {"mp4"}, "video.container"),
    )
    if not config.hardware_encoder_required:
        raise ConfigError("video.hardware_encoder_required must be true for version 1")
    if config.width % 2 or config.height % 2:
        raise ConfigError("video.width and video.height must both be even")
    if config.keyframe_interval_frames > config.fps:
        raise ConfigError("video.keyframe_interval_frames must not exceed video.fps")
    return config


def _audio(root: Mapping[str, object]) -> AudioConfig:
    fields = set(AudioConfig.__dataclass_fields__)
    table = _section(root, "audio", fields)
    config = AudioConfig(
        enabled=_boolean(table, "enabled", "audio"),
        device_match=_nonempty(_string(table, "device_match", "audio"), 256, "audio.device_match"),
        sample_rate_hz=_bounded_int(
            _integer(table, "sample_rate_hz", "audio"),
            8_000,
            192_000,
            "audio.sample_rate_hz",
        ),
        channels=_bounded_int(_integer(table, "channels", "audio"), 1, 2, "audio.channels"),
        codec=_choice(_string(table, "codec", "audio"), {"aac"}, "audio.codec"),
        bitrate_bps=_bounded_int(
            _integer(table, "bitrate_bps", "audio"),
            16_000,
            512_000,
            "audio.bitrate_bps",
        ),
    )
    if (
        config.sample_rate_hz,
        config.channels,
        config.codec,
        config.bitrate_bps,
    ) != (48_000, 1, "aac", 128_000):
        raise ConfigError(
            "enabled audio must use the fixed production contract: "
            "S16LE/48000/mono AAC at 128000 bit/s"
        )
    if config.enabled:
        try:
            parse_alsa_selector(config.device_match)
        except AlsaMatchError as error:
            raise ConfigError(f"audio.device_match is unsafe: {error}") from error
    return config


def _gps(root: Mapping[str, object]) -> GpsConfig:
    fields = set(GpsConfig.__dataclass_fields__)
    table = _section(root, "gps", fields)
    device = _nonempty(_string(table, "device", "gps"), 256, "gps.device")
    if not device.startswith("/dev/") or ".." in Path(device).parts:
        raise ConfigError("gps.device must be an absolute path below /dev")
    baud = _bounded_int(_integer(table, "baud", "gps"), 1_200, 921_600, "gps.baud")
    if baud not in SUPPORTED_GPS_BAUD_RATES:
        supported = ",".join(str(value) for value in sorted(SUPPORTED_GPS_BAUD_RATES))
        raise ConfigError(f"gps.baud must be one of the supported rates: {supported}")
    earliest = _canonical_utc_text(
        _string(table, "anchor_earliest_utc", "gps"),
        "gps.anchor_earliest_utc",
    )
    latest = _canonical_utc_text(
        _string(table, "anchor_latest_utc", "gps"),
        "gps.anchor_latest_utc",
    )
    if _parse_canonical_utc(latest) <= _parse_canonical_utc(earliest):
        raise ConfigError("gps.anchor_latest_utc must be after gps.anchor_earliest_utc")
    return GpsConfig(
        device=device,
        baud=baud,
        stale_after_s=_bounded_number(
            _number(table, "stale_after_s", "gps"), 0.1, 60.0, "gps.stale_after_s"
        ),
        max_sample_hz=_bounded_int(
            _integer(table, "max_sample_hz", "gps"), 1, 10, "gps.max_sample_hz"
        ),
        anchor_earliest_utc=earliest,
        anchor_latest_utc=latest,
        anchor_uncertainty_ms=_bounded_int(
            _integer(table, "anchor_uncertainty_ms", "gps"),
            1,
            5_000,
            "gps.anchor_uncertainty_ms",
        ),
        anchor_max_conflict_ms=_bounded_int(
            _integer(table, "anchor_max_conflict_ms", "gps"),
            0,
            60_000,
            "gps.anchor_max_conflict_ms",
        ),
        anchor_max_reacquire_disagreement_ms=_bounded_int(
            _integer(table, "anchor_max_reacquire_disagreement_ms", "gps"),
            0,
            60_000,
            "gps.anchor_max_reacquire_disagreement_ms",
        ),
        anchor_max_interval_s=_bounded_int(
            _integer(table, "anchor_max_interval_s", "gps"),
            1,
            604_800,
            "gps.anchor_max_interval_s",
        ),
    )


def _canonical_utc_text(value: str, path: str) -> str:
    if len(value) != 20 or not value.endswith("Z"):
        raise ConfigError(f"{path} must use canonical YYYY-MM-DDTHH:MM:SSZ UTC")
    try:
        parsed = _parse_canonical_utc(value)
    except ValueError as exc:
        raise ConfigError(f"{path} must use canonical YYYY-MM-DDTHH:MM:SSZ UTC") from exc
    if parsed.isoformat(timespec="seconds").replace("+00:00", "Z") != value:
        raise ConfigError(f"{path} must use canonical YYYY-MM-DDTHH:MM:SSZ UTC")
    return value


def _parse_canonical_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError("not canonical UTC")
    return parsed.astimezone(UTC)


def _time(root: Mapping[str, object]) -> TimeConfig:
    fields = set(TimeConfig.__dataclass_fields__)
    table = _section(root, "time", fields)
    timezone = _string(table, "timezone", "time")
    if (
        len(timezone) > 128
        or (timezone != "UTC" and not _IANA_NAME.fullmatch(timezone))
        or ".." in timezone
    ):
        raise ConfigError("time.timezone must be an IANA area/location name")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError("time.timezone must resolve through installed timezone data") from exc
    discipline_system_clock = _boolean(table, "discipline_system_clock", "time")
    if discipline_system_clock:
        raise ConfigError(
            "time.discipline_system_clock is unsupported in v1; "
            "GPS anchors do not set Linux wall time"
        )
    return TimeConfig(
        timezone=timezone,
        filename_timezone=_choice(
            _string(table, "filename_timezone", "time"), {"UTC"}, "time.filename_timezone"
        ),
        discipline_system_clock=discipline_system_clock,
        system_clock_owner=_choice(
            _string(table, "system_clock_owner", "time"),
            {"systemd-timesyncd"},
            "time.system_clock_owner",
        ),
    )


def _overlay(root: Mapping[str, object]) -> OverlayConfig:
    fields = set(OverlayConfig.__dataclass_fields__)
    table = _section(root, "overlay", fields)
    return OverlayConfig(
        enabled=_boolean(table, "enabled", "overlay"),
        show_local_datetime=_boolean(table, "show_local_datetime", "overlay"),
        show_utc_offset=_boolean(table, "show_utc_offset", "overlay"),
        show_rec=_boolean(table, "show_rec", "overlay"),
        show_speed=_boolean(table, "show_speed", "overlay"),
        speed_unit=_choice(
            _string(table, "speed_unit", "overlay"), {"kmh", "mph"}, "overlay.speed_unit"
        ),
        show_coordinates=_boolean(table, "show_coordinates", "overlay"),
        coordinate_decimals=_bounded_int(
            _integer(table, "coordinate_decimals", "overlay"),
            0,
            8,
            "overlay.coordinate_decimals",
        ),
        show_altitude=_boolean(table, "show_altitude", "overlay"),
        show_satellites=_boolean(table, "show_satellites", "overlay"),
        show_hdop=_boolean(table, "show_hdop", "overlay"),
    )


def _preview(root: Mapping[str, object], video: VideoConfig) -> PreviewConfig:
    fields = set(PreviewConfig.__dataclass_fields__)
    table = _section(root, "preview", fields)
    config = PreviewConfig(
        enabled=_boolean(table, "enabled", "preview"),
        width=_bounded_int(_integer(table, "width", "preview"), 160, 1920, "preview.width"),
        height=_bounded_int(_integer(table, "height", "preview"), 90, 1080, "preview.height"),
        fps=_bounded_int(_integer(table, "fps", "preview"), 1, 30, "preview.fps"),
        max_clients=_bounded_int(
            _integer(table, "max_clients", "preview"), 1, 2, "preview.max_clients"
        ),
        latency_target_ms=_bounded_int(
            _integer(table, "latency_target_ms", "preview"),
            50,
            1_000,
            "preview.latency_target_ms",
        ),
    )
    if config.width % 2 or config.height % 2:
        raise ConfigError("preview.width and preview.height must both be even")
    if config.width > video.width or config.height > video.height or config.fps > video.fps:
        raise ConfigError("preview dimensions and fps must not exceed the video profile")
    return config


def _storage(root: Mapping[str, object]) -> StorageConfig:
    fields = set(StorageConfig.__dataclass_fields__)
    table = _section(root, "storage", fields)
    config = StorageConfig(
        recording_root=_choice(
            _string(table, "recording_root", "storage"),
            {"/srv/dashcam"},
            "storage.recording_root",
        ),
        required_filesystem=_choice(
            _string(table, "required_filesystem", "storage"),
            {"exfat"},
            "storage.required_filesystem",
        ),
        required_volume_label=_choice(
            _string(table, "required_volume_label", "storage"),
            {"DASHCAM"},
            "storage.required_volume_label",
        ),
        require_distinct_mount=_boolean(table, "require_distinct_mount", "storage"),
        low_watermark_percent=_bounded_int(
            _integer(table, "low_watermark_percent", "storage"),
            1,
            99,
            "storage.low_watermark_percent",
        ),
        high_watermark_percent=_bounded_int(
            _integer(table, "high_watermark_percent", "storage"),
            1,
            99,
            "storage.high_watermark_percent",
        ),
        minimum_free_gib=_bounded_number(
            _number(table, "minimum_free_gib", "storage"),
            0.1,
            1_024.0,
            "storage.minimum_free_gib",
        ),
        emergency_free_mib=_bounded_int(
            _integer(table, "emergency_free_mib", "storage"),
            1,
            1_048_576,
            "storage.emergency_free_mib",
        ),
        download_lease_timeout_s=_bounded_int(
            _integer(table, "download_lease_timeout_s", "storage"),
            1,
            900,
            "storage.download_lease_timeout_s",
        ),
        protect_previous_clips=_bounded_int(
            _integer(table, "protect_previous_clips", "storage"),
            0,
            60,
            "storage.protect_previous_clips",
        ),
        protect_next_clips=_bounded_int(
            _integer(table, "protect_next_clips", "storage"),
            0,
            60,
            "storage.protect_next_clips",
        ),
    )
    if not config.require_distinct_mount:
        raise ConfigError("storage.require_distinct_mount must be true")
    if config.low_watermark_percent >= config.high_watermark_percent:
        raise ConfigError("storage.low_watermark_percent must be below high_watermark_percent")
    if config.emergency_free_mib >= config.minimum_free_gib * 1024:
        raise ConfigError("storage.emergency_free_mib must be below minimum_free_gib")
    return config


def _network(root: Mapping[str, object]) -> NetworkConfig:
    fields = set(NetworkConfig.__dataclass_fields__)
    table = _section(root, "network", fields)
    prefix = _nonempty(_string(table, "ssid_prefix", "network"), 23, "network.ssid_prefix")
    address = _string(table, "address", "network")
    try:
        interface = ipaddress.ip_interface(address)
    except ValueError as error:
        raise ConfigError("network.address must be a valid IPv4 CIDR interface") from error
    if not isinstance(interface, ipaddress.IPv4Interface):
        raise ConfigError("network.address must be IPv4")
    if interface.ip in {interface.network.network_address, interface.network.broadcast_address}:
        raise ConfigError("network.address must identify a usable host address")
    if not interface.ip.is_private:
        raise ConfigError("network.address must use a private address")
    return NetworkConfig(
        ap_enabled=_boolean(table, "ap_enabled", "network"),
        ssid_prefix=prefix,
        address=address,
    )


def _service(root: Mapping[str, object]) -> ServiceConfig:
    fields = set(ServiceConfig.__dataclass_fields__)
    table = _section(root, "service", fields)
    config = ServiceConfig(
        watchdog_s=_bounded_int(
            _integer(table, "watchdog_s", "service"), 1, 300, "service.watchdog_s"
        ),
        restart_backoff_min_s=_bounded_int(
            _integer(table, "restart_backoff_min_s", "service"),
            1,
            300,
            "service.restart_backoff_min_s",
        ),
        restart_backoff_max_s=_bounded_int(
            _integer(table, "restart_backoff_max_s", "service"),
            1,
            3_600,
            "service.restart_backoff_max_s",
        ),
    )
    if config.restart_backoff_min_s > config.restart_backoff_max_s:
        raise ConfigError("service.restart_backoff_min_s must not exceed restart_backoff_max_s")
    return config


# Add a migration from N to N+1 here when CURRENT_SCHEMA_VERSION advances.
_MIGRATIONS: dict[int, Migration] = {}


def migrate_config_mapping(raw: Mapping[str, object]) -> ConfigTable:
    """Copy and migrate a raw mapping to the current schema version."""

    source = _mapping(raw, "configuration")
    if "schema_version" not in source:
        raise ConfigMigrationError("configuration is missing required key: schema_version")
    version_value = source["schema_version"]
    if isinstance(version_value, bool) or not isinstance(version_value, int):
        raise ConfigMigrationError("schema_version must be an integer")
    if version_value > CURRENT_SCHEMA_VERSION:
        raise ConfigMigrationError(
            f"schema_version {version_value} is newer than supported version "
            f"{CURRENT_SCHEMA_VERSION}"
        )
    if version_value < 1:
        raise ConfigMigrationError(f"schema_version {version_value} is not supported")

    migrated = cast(ConfigTable, copy.deepcopy(dict(source)))
    version = version_value
    while version < CURRENT_SCHEMA_VERSION:
        migration = _MIGRATIONS.get(version)
        if migration is None:
            raise ConfigMigrationError(
                f"no migration from schema_version {version} to {version + 1}"
            )
        migrated = migration(migrated)
        version += 1
        migrated["schema_version"] = version
    return migrated


def config_from_mapping(raw: Mapping[str, object]) -> DashcamConfig:
    """Migrate and strictly validate a mapping."""

    root = migrate_config_mapping(raw)
    root_fields = {"schema_version", "device_name", *_SECTION_NAMES}
    _allowed(root, root_fields, "configuration")
    version = _integer(root, "schema_version", "configuration")
    if version != CURRENT_SCHEMA_VERSION:
        raise ConfigMigrationError(f"schema_version {version} was not migrated")
    video = _video(root)
    return DashcamConfig(
        schema_version=version,
        device_name=_nonempty(_string(root, "device_name", "configuration"), 64, "device_name"),
        video=video,
        audio=_audio(root),
        gps=_gps(root),
        time=_time(root),
        overlay=_overlay(root),
        preview=_preview(root, video),
        storage=_storage(root),
        network=_network(root),
        service=_service(root),
    )


def parse_config_toml(text: str) -> DashcamConfig:
    """Parse and validate TOML text without including its values in errors."""

    if len(text.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise ConfigError(f"configuration exceeds {MAX_CONFIG_BYTES} bytes")
    try:
        raw = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, UnicodeError) as error:
        raise ConfigError("configuration is not valid TOML") from error
    return config_from_mapping(raw)


def load_config(path: str | os.PathLike[str]) -> DashcamConfig:
    """Read at most ``MAX_CONFIG_BYTES`` and validate a TOML configuration."""

    config_path = Path(path)
    try:
        with config_path.open("rb") as stream:
            payload = stream.read(MAX_CONFIG_BYTES + 1)
    except OSError as error:
        raise ConfigError(f"could not read configuration file: {config_path}") from error
    if len(payload) > MAX_CONFIG_BYTES:
        raise ConfigError(f"configuration exceeds {MAX_CONFIG_BYTES} bytes")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ConfigError("configuration must be UTF-8") from error
    return parse_config_toml(text)


def config_to_mapping(config: DashcamConfig) -> ConfigTable:
    """Return the complete allow-listed mapping; it can never contain secrets."""

    validated = config_from_mapping(cast(Mapping[str, object], asdict(config)))
    return cast(ConfigTable, asdict(validated))


def _toml_value(value: ConfigValue) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return repr(value)
    raise ConfigError("configuration contains an unsupported value")


def config_to_toml(config: DashcamConfig) -> str:
    """Serialize an already typed configuration in a deterministic field order."""

    raw = config_to_mapping(config)
    lines = [
        f"schema_version = {_toml_value(cast(ConfigValue, raw['schema_version']))}",
        f"device_name = {_toml_value(cast(ConfigValue, raw['device_name']))}",
    ]
    for section_name in _SECTION_NAMES:
        lines.extend(("", f"[{section_name}]"))
        section = cast(ConfigTable, raw[section_name])
        lines.extend(
            f"{key} = {_toml_value(cast(ConfigValue, value))}" for key, value in section.items()
        )
    return "\n".join(lines) + "\n"


_UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS: Final = {
    errno.EACCES,
    errno.EBADF,
    errno.EINVAL,
    errno.EISDIR,
    errno.ENOTSUP,
    errno.EPERM,
}


def _fsync_parent_directory(parent: Path) -> bool:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(parent, flags)
    except OSError as error:
        if error.errno in _UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS:
            return False
        raise
    try:
        os.fsync(descriptor)
    except OSError as error:
        if error.errno in _UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS:
            return False
        raise
    finally:
        os.close(descriptor)
    return True


def _write_temp_file(descriptor: int, payload: bytes) -> None:
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def write_config_atomic(path: str | os.PathLike[str], config: DashcamConfig) -> AtomicWriteResult:
    """Validate, flush, and atomically replace ``path`` with mode ``0640``.

    Failures before ``os.replace`` leave an existing destination untouched and
    remove the temporary file.
    """

    config_path = Path(path)
    payload = config_to_toml(config).encode("utf-8")
    if len(payload) > MAX_CONFIG_BYTES:
        raise ConfigError(f"configuration exceeds {MAX_CONFIG_BYTES} bytes")

    descriptor = -1
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{config_path.name}.", suffix=".tmp", dir=config_path.parent
        )
        os.chmod(temporary_name, CONFIG_FILE_MODE)
        # _write_temp_file takes ownership of the descriptor, including on failure.
        owned_descriptor = descriptor
        descriptor = -1
        _write_temp_file(owned_descriptor, payload)
        os.replace(temporary_name, config_path)
        temporary_name = None
        directory_fsynced = _fsync_parent_directory(config_path.parent)
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        raise ConfigError(
            f"could not atomically write configuration file: {config_path}"
        ) from error
    finally:
        if temporary_name is not None:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name)
    return AtomicWriteResult(directory_fsynced=directory_fsynced)


def _merge_update(current: ConfigTable, updates: Mapping[str, object]) -> ConfigTable:
    merged = copy.deepcopy(current)
    for key, value in updates.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, Mapping):
            merged[key] = _merge_update(existing, cast(Mapping[str, object], value))
        else:
            merged[key] = cast(ConfigValue | ConfigTable, copy.deepcopy(value))
    return merged


def update_config_atomic(
    path: str | os.PathLike[str], updates: Mapping[str, object]
) -> DashcamConfig:
    """Apply a validated partial mapping and atomically preserve-or-replace the file."""

    current = load_config(path)
    candidate_mapping = _merge_update(config_to_mapping(current), _mapping(updates, "update"))
    candidate = config_from_mapping(candidate_mapping)
    write_config_atomic(path, candidate)
    return candidate


__all__ = [
    "CONFIG_FILE_MODE",
    "CURRENT_SCHEMA_VERSION",
    "MAX_CONFIG_BYTES",
    "AtomicWriteResult",
    "AudioConfig",
    "ConfigError",
    "ConfigMigrationError",
    "DashcamConfig",
    "GpsConfig",
    "NetworkConfig",
    "OverlayConfig",
    "PreviewConfig",
    "ServiceConfig",
    "StorageConfig",
    "TimeConfig",
    "VideoConfig",
    "config_from_mapping",
    "config_to_mapping",
    "config_to_toml",
    "default_config",
    "load_config",
    "migrate_config_mapping",
    "parse_config_toml",
    "update_config_atomic",
    "write_config_atomic",
]
