from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

import dashcam.config as config_module
from dashcam.config import (
    CONFIG_FILE_MODE,
    AudioConfig,
    ConfigError,
    ConfigMigrationError,
    config_from_mapping,
    config_to_mapping,
    config_to_toml,
    default_config,
    load_config,
    parse_config_toml,
    update_config_atomic,
    write_config_atomic,
)

REPOSITORY_ROOT = Path(__file__).parents[2]
DEFAULT_CONFIG_PATH = REPOSITORY_ROOT / "config" / "default.toml"


@pytest.fixture
def raw_config() -> dict[str, object]:
    return cast(dict[str, object], config_to_mapping(default_config()))


def test_checked_in_defaults_are_valid_and_round_trip() -> None:
    loaded = load_config(DEFAULT_CONFIG_PATH)

    assert loaded == default_config()
    assert parse_config_toml(config_to_toml(loaded)) == loaded
    assert loaded.video.width == 1920
    assert loaded.time.timezone == "Asia/Jerusalem"
    assert loaded.time.discipline_system_clock is False
    assert loaded.time.system_clock_owner == "systemd-timesyncd"
    assert loaded.preview.max_clients == 1
    assert loaded.storage.recording_root == "/srv/dashcam"
    assert loaded.storage.download_lease_timeout_s == 300
    storage_mapping = config_to_mapping(loaded)["storage"]
    assert isinstance(storage_mapping, dict)
    assert storage_mapping["download_lease_timeout_s"] == 300


@pytest.mark.parametrize("timeout_s", [1, 900])
def test_download_lease_timeout_accepts_exact_boundaries(
    raw_config: dict[str, object], timeout_s: int
) -> None:
    storage = raw_config["storage"]
    assert isinstance(storage, dict)
    storage["download_lease_timeout_s"] = timeout_s

    parsed = config_from_mapping(raw_config)

    assert parsed.storage.download_lease_timeout_s == timeout_s
    round_tripped = config_to_mapping(parsed)["storage"]
    assert isinstance(round_tripped, dict)
    assert round_tripped["download_lease_timeout_s"] == timeout_s


def test_enabled_audio_requires_stable_selector_and_exact_production_format() -> None:
    selector_mapping = config_to_mapping(default_config())
    selector_audio = selector_mapping["audio"]
    assert isinstance(selector_audio, dict)
    selector_audio["device_match"] = "hw:1,0"
    with pytest.raises(ConfigError, match="device_match"):
        config_from_mapping(selector_mapping)

    format_mapping = config_to_mapping(default_config())
    format_audio = format_mapping["audio"]
    assert isinstance(format_audio, dict)
    format_audio["sample_rate_hz"] = 44_100
    with pytest.raises(ConfigError, match="fixed production"):
        config_from_mapping(format_mapping)

    assert AudioConfig(enabled=False, device_match="unused").enabled is False


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("video", "fps"), "30"),
        (("video", "width"), 1919),
        (("video", "hardware_encoder_required"), False),
        (("gps", "baud"), 19_200),
        (("gps", "stale_after_s"), 0.0),
        (("gps", "max_sample_hz"), 11),
        (("gps", "anchor_earliest_utc"), "2024-01-01"),
        (("gps", "anchor_latest_utc"), "not-a-date"),
        (("gps", "anchor_uncertainty_ms"), 0),
        (("gps", "anchor_max_interval_s"), 0),
        (("time", "timezone"), "../localtime"),
        (("time", "timezone"), "Area/Definitely_Not_A_Real_Zone"),
        (("time", "discipline_system_clock"), True),
        (("time", "system_clock_owner"), "chrony"),
        (("overlay", "coordinate_decimals"), 9),
        (("preview", "max_clients"), 3),
        (("storage", "recording_root"), "/tmp/dashcam"),
        (("storage", "download_lease_timeout_s"), 0),
        (("storage", "download_lease_timeout_s"), 901),
        (("storage", "download_lease_timeout_s"), True),
        (("network", "address"), "8.8.8.8/24"),
        (("service", "watchdog_s"), 0),
    ],
)
def test_rejects_invalid_types_and_ranges(
    raw_config: dict[str, object], path: tuple[str, str], value: object
) -> None:
    section = raw_config[path[0]]
    assert isinstance(section, dict)
    section[path[1]] = value

    with pytest.raises(ConfigError):
        config_from_mapping(raw_config)


@pytest.mark.parametrize(
    ("section_name", "values"),
    [
        ("video", {"keyframe_interval_frames": 31, "fps": 30}),
        ("preview", {"width": 2048}),
        ("storage", {"low_watermark_percent": 20, "high_watermark_percent": 20}),
        ("storage", {"minimum_free_gib": 0.25, "emergency_free_mib": 256}),
        ("service", {"restart_backoff_min_s": 61, "restart_backoff_max_s": 60}),
    ],
)
def test_rejects_cross_field_failures(
    raw_config: dict[str, object], section_name: str, values: dict[str, object]
) -> None:
    section = raw_config[section_name]
    assert isinstance(section, dict)
    section.update(values)

    with pytest.raises(ConfigError):
        config_from_mapping(raw_config)


def test_rejects_inverted_gps_anchor_plausibility_window(
    raw_config: dict[str, object],
) -> None:
    gps = raw_config["gps"]
    assert isinstance(gps, dict)
    gps["anchor_earliest_utc"] = "2100-01-01T00:00:00Z"
    gps["anchor_latest_utc"] = "2024-01-01T00:00:00Z"

    with pytest.raises(ConfigError, match="must be after"):
        config_from_mapping(raw_config)


def test_rejects_unknown_root_and_section_keys(raw_config: dict[str, object]) -> None:
    raw_config["unexpected"] = True
    with pytest.raises(ConfigError, match="unknown key: unexpected"):
        config_from_mapping(raw_config)

    raw_config.pop("unexpected")
    network = raw_config["network"]
    assert isinstance(network, dict)
    network["passphrase"] = "never-store-this"
    with pytest.raises(ConfigError, match="unknown key: passphrase"):
        config_from_mapping(raw_config)


def test_rejects_missing_and_future_schema_versions(raw_config: dict[str, object]) -> None:
    raw_config.pop("schema_version")
    with pytest.raises(ConfigMigrationError, match="missing required key"):
        config_from_mapping(raw_config)

    raw_config["schema_version"] = 2
    with pytest.raises(ConfigMigrationError, match="newer than supported version 1"):
        config_from_mapping(raw_config)


def test_rejects_older_schema_when_dispatcher_has_no_migration(
    raw_config: dict[str, object],
) -> None:
    raw_config["schema_version"] = 0

    with pytest.raises(ConfigMigrationError, match="schema_version 0 is not supported"):
        config_from_mapping(raw_config)


def test_atomic_write_and_partial_update(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    result = write_config_atomic(path, default_config())

    assert isinstance(result.directory_fsynced, bool)
    assert load_config(path) == default_config()
    updated = update_config_atomic(
        path,
        {
            "video": {"bitrate_bps": 9_000_000},
            "overlay": {"speed_unit": "mph"},
        },
    )
    assert updated.video.bitrate_bps == 9_000_000
    assert updated.overlay.speed_unit == "mph"
    assert load_config(path) == updated
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_write_uses_restricted_permissions_where_supported(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config_atomic(path, default_config())

    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == CONFIG_FILE_MODE


def test_replace_failure_leaves_previous_valid_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    write_config_atomic(path, default_config())
    before = path.read_bytes()

    def fail_replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr("dashcam.config.os.replace", fail_replace)
    with pytest.raises(ConfigError, match="could not atomically write"):
        update_config_atomic(path, {"video": {"bitrate_bps": 9_000_000}})

    assert path.read_bytes() == before
    assert load_config(path) == default_config()
    assert not list(tmp_path.glob("*.tmp"))


def test_write_failure_leaves_previous_valid_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    write_config_atomic(path, default_config())
    before = path.read_bytes()

    def fail_write(descriptor: int, payload: bytes) -> None:
        os.close(descriptor)
        raise OSError("simulated write failure")

    monkeypatch.setattr(config_module, "_write_temp_file", fail_write)
    with pytest.raises(ConfigError, match="could not atomically write"):
        update_config_atomic(path, {"video": {"bitrate_bps": 9_000_000}})

    assert path.read_bytes() == before
    assert load_config(path) == default_config()
    assert not list(tmp_path.glob("*.tmp"))


def test_invalid_update_leaves_previous_valid_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config_atomic(path, default_config())
    before = path.read_bytes()

    with pytest.raises(ConfigError):
        update_config_atomic(path, {"storage": {"recording_root": "/tmp/fallback"}})

    assert path.read_bytes() == before


def _all_strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _all_strings(child)


def test_config_model_and_serialization_cannot_leak_secrets(raw_config: dict[str, object]) -> None:
    passphrase = "unique-device-passphrase"
    network = raw_config["network"]
    assert isinstance(network, dict)
    network["ap_passphrase"] = passphrase

    with pytest.raises(ConfigError) as error:
        config_from_mapping(raw_config)

    serialized = config_to_toml(default_config())
    public_mapping = config_to_mapping(default_config())
    assert passphrase not in str(error.value)
    assert passphrase not in serialized
    assert not any(
        "password" in item.lower() or "passphrase" in item.lower() or "secret" in item.lower()
        for item in _all_strings(public_mapping)
    )
