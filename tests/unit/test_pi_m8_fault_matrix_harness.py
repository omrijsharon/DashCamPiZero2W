from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

from dashcam.config import default_config
from dashcam.gps.nmea import NmeaError, parse_nmea_line
from dashcam.metadata.reconcile import (
    MetadataReconciliationError,
    plan_post_anchor_reconciliation,
)
from dashcam.metadata.schema import TimeAnchor, TimeAnchorSource

ROOT = Path(__file__).resolve().parents[2]
HARNESS_PATH = ROOT / "deploy/ssh-dev-validation/milestone8-fault-matrix/run.py"
README_PATH = HARNESS_PATH.with_name("README.md")
MANIFEST_PATH = HARNESS_PATH.with_name("SHA256SUMS")


def _load() -> ModuleType:
    name = "pi_m8_fault_matrix_harness"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, HARNESS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_manifest_is_closed_to_readme_and_script(tmp_path: Path) -> None:
    harness = _load()
    (tmp_path / "README.md").write_text("reviewed\n", encoding="utf-8")
    (tmp_path / "run.py").write_text("print('reviewed')\n", encoding="utf-8")
    entries = {
        name: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
        for name in ("README.md", "run.py")
    }
    manifest = "".join(f"{digest}  {name}\n" for name, digest in entries.items()).encode()
    (tmp_path / "SHA256SUMS").write_bytes(manifest)

    assert harness.verify_manifest(hashlib.sha256(manifest).hexdigest(), tmp_path) == entries

    extra = tmp_path / "extra"
    extra.write_text("unreviewed\n", encoding="utf-8")
    changed = manifest + f"{hashlib.sha256(extra.read_bytes()).hexdigest()}  extra\n".encode()
    (tmp_path / "SHA256SUMS").write_bytes(changed)
    with pytest.raises(harness.HarnessError, match="not closed"):
        harness.verify_manifest(hashlib.sha256(changed).hexdigest(), tmp_path)


def test_manifest_rejects_wrong_outer_hash_and_duplicate_member(tmp_path: Path) -> None:
    harness = _load()
    for name in ("README.md", "run.py"):
        (tmp_path / name).write_text(name, encoding="ascii")
    readme_hash = hashlib.sha256((tmp_path / "README.md").read_bytes()).hexdigest()
    run_hash = hashlib.sha256((tmp_path / "run.py").read_bytes()).hexdigest()
    manifest = (
        f"{readme_hash}  README.md\n"
        f"{run_hash}  run.py\n"
        f"{run_hash}  run.py\n"
    ).encode()
    (tmp_path / "SHA256SUMS").write_bytes(manifest)

    with pytest.raises(harness.HarnessError, match="differs"):
        harness.verify_manifest("0" * 64, tmp_path)
    with pytest.raises(harness.HarnessError, match="not closed"):
        harness.verify_manifest(hashlib.sha256(manifest).hexdigest(), tmp_path)


def test_synthetic_nmea_is_checksum_valid_bounded_and_non_private() -> None:
    harness = _load()
    record = harness._rmc(datetime(2026, 7, 28, 12, 34, 56, tzinfo=UTC))

    assert len(record) <= 82
    assert b"0000.0000,N,00000.0000,E" in record
    parsed = parse_nmea_line(record, received_monotonic_ns=123)
    assert parsed.ok
    assert parsed.sentence is not None
    assert parsed.sentence.navigation_valid
    assert parsed.sentence.latitude_deg == 0
    assert parsed.sentence.longitude_deg == 0


def test_bad_checksum_and_malformed_vectors_hit_distinct_rejections() -> None:
    harness = _load()
    valid = harness._rmc(datetime(2026, 7, 28, 12, 34, 56, tzinfo=UTC))

    checksum = parse_nmea_line(
        harness._bad_checksum(valid),
        received_monotonic_ns=123,
    )
    malformed = parse_nmea_line(b"$not-nmea\r\n", received_monotonic_ns=124)

    assert checksum.error is NmeaError.CHECKSUM_MISMATCH
    assert malformed.error is NmeaError.MALFORMED_ENVELOPE


def test_implausible_date_is_parse_valid_for_anchor_policy_rejection() -> None:
    harness = _load()
    record = harness._rmc(datetime(1980, 1, 1, tzinfo=UTC))

    parsed = parse_nmea_line(record, received_monotonic_ns=123)

    assert parsed.ok
    assert parsed.sentence is not None
    assert parsed.sentence.time_anchor_candidate
    assert parsed.sentence.utc_datetime == datetime(1980, 1, 1, tzinfo=UTC)


def test_temporary_config_changes_only_closed_validation_fields() -> None:
    harness = _load()
    base = default_config()

    observed = harness._temporary_config(base)
    expected = replace(
        base,
        video=replace(base.video, clip_duration_s=harness.CLIP_DURATION_S),
        gps=replace(
            base.gps,
            device=str(harness.GPS_LINK),
            stale_after_s=harness.STALE_AFTER_S,
        ),
        service=replace(base.service, watchdog_s=harness.WATCHDOG_S),
    )

    assert observed == expected
    assert not observed.time.discipline_system_clock
    assert observed.time.system_clock_owner == "systemd-timesyncd"
    assert observed.network == base.network
    assert observed.storage == base.storage
    assert observed.audio == base.audio
    assert observed.video.width == 1920
    assert observed.video.height == 1080
    assert observed.video.fps == 30
    assert observed.video.hardware_encoder_required


def test_transient_unit_is_nonrestarting_clock_protected_and_network_closed() -> None:
    harness = _load()
    interpreter = Path(
        "/opt/dashcam/releases/0.1.0.dev0-0123456789abcdef/venv/bin/python"
    )

    unit = harness.render_transient_unit(
        interpreter=interpreter,
        config_path=harness.TEMP_CONFIG_PATH,
    )

    assert "Restart=no" in unit
    assert "ProtectClock=yes" in unit
    assert "RuntimeDirectoryPreserve=yes" in unit
    assert "RestrictAddressFamilies=AF_UNIX" in unit
    assert "CapabilityBoundingSet=\n" in unit
    assert "AmbientCapabilities=\n" in unit
    assert "PrivateDevices=no" in unit
    assert "User=dashcam" in unit
    assert f"--config {harness.TEMP_CONFIG_PATH}" in unit
    assert f"--identity {harness.IDENTITY_PATH}" in unit
    assert "dashcam-network-fallback" not in unit
    assert "NetworkManager" not in unit
    assert "ExecStartPre" not in unit
    assert "ExecStartPost" not in unit


def test_transient_unit_rejects_foreign_interpreter_or_config() -> None:
    harness = _load()
    with pytest.raises(harness.HarnessError, match="interpreter"):
        harness.render_transient_unit(
            interpreter=Path("/usr/bin/python3"),
            config_path=harness.TEMP_CONFIG_PATH,
        )
    with pytest.raises(harness.HarnessError, match="config"):
        harness.render_transient_unit(
            interpreter=Path(
                "/opt/dashcam/releases/0.1.0.dev0-0123456789abcdef/venv/bin/python"
            ),
            config_path=Path("/tmp/config.toml"),
        )


def test_collision_vector_refuses_before_intent_creation() -> None:
    harness = _load()
    sidecar = harness._fixture_sidecar()
    anchor = TimeAnchor(
        TimeAnchorSource.GPS,
        10_000_000_000,
        harness.BASE_UTC,
        250_000_000,
        "NMEA:GNRMC:active-valid:complete-utc",
    )
    first = plan_post_anchor_reconciliation(
        sidecar,
        anchor=anchor,
        intent_id=harness.COLLISION_INTENT_ID,
        created_monotonic_ns=21_000_000_000,
    )

    with pytest.raises(MetadataReconciliationError, match="collision"):
        plan_post_anchor_reconciliation(
            sidecar,
            anchor=anchor,
            intent_id=harness.COLLISION_INTENT_ID,
            created_monotonic_ns=21_000_000_000,
            existing_names={first.sidecar.metadata_file.upper()},
        )


def test_reconciled_sidecar_replay_ignores_later_conflicting_anchor() -> None:
    harness = _load()
    sidecar = harness._fixture_sidecar()
    anchor = TimeAnchor(
        TimeAnchorSource.GPS,
        10_000_000_000,
        harness.BASE_UTC,
        250_000_000,
        "NMEA:GNRMC:active-valid:complete-utc",
    )
    first = plan_post_anchor_reconciliation(
        sidecar,
        anchor=anchor,
        intent_id=harness.COLLISION_INTENT_ID,
        created_monotonic_ns=21_000_000_000,
    )

    evidence = harness._idempotent_plan(first.sidecar)

    assert evidence == {
        "already_reconciled": True,
        "intent_created": False,
        "sidecar_unchanged": True,
        "stable_clip_id": str(sidecar.clip_id),
        "conflicting_later_anchor_ignored": True,
    }


def test_privacy_guard_rejects_coordinates_samples_and_raw_nmea() -> None:
    harness = _load()

    harness._assert_privacy_safe(
        {
            "gps": {
                "navigation_present": False,
                "gps_sample_count": 0,
            },
            "privacy": {"coordinates_retained_in_result": False},
        }
    )
    for document in (
        {"lat_deg": 1.0},
        {"gps": {"samples": []}},
        {"detail": "$GNRMC,private"},
    ):
        with pytest.raises(harness.HarnessError, match=r"privacy|NMEA"):
            harness._assert_privacy_safe(document)


def test_startup_failure_summary_does_not_require_runtime_metrics() -> None:
    harness = _load()

    summary = harness._startup_failure_summary(
        {
            "lifecycle": {
                "state": "FAULTED",
                "reason": "CONFIG_ERROR",
                "detail": "closed diagnostic",
            }
        }
    )

    assert summary["lifecycle_state"] == "FAULTED"
    assert summary["reason"] == "CONFIG_ERROR"
    assert summary["detail"] == "closed diagnostic"
    assert summary["runtime_snapshot_present"] is False


def test_frame_counter_reader_allows_only_initially_unavailable_drop_observation() -> None:
    harness = _load()
    document = {
        "runtime": {
            "frames": {"encoded": 1, "dropped": None},
        }
    }

    assert harness._frame_counts(document) == (1, None)
    document["runtime"]["frames"]["dropped"] = 0
    assert harness._frame_counts(document) == (1, 0)


def test_provisional_discovery_ignores_unrelated_reconciled_backlog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load()
    clips = tmp_path / "clips"
    clips.mkdir()
    sidecar = harness._fixture_sidecar()
    (clips / sidecar.metadata_file).write_bytes(sidecar.to_canonical_json())
    for sequence in range(harness.MAX_NEW_SIDECARS + 1):
        (clips / f"20260728T120000.000Z_{sequence:08d}_unrelated.json").write_text(
            "{}",
            encoding="ascii",
        )
    monkeypatch.setattr(harness, "RECORDING_ROOT", tmp_path)
    monkeypatch.setattr(harness, "CLIPS_ROOT", clips)

    assert harness._new_provisional_sidecars(set()) == [sidecar]


def test_exact_clip_uuid_catalog_lookup_avoids_directory_wide_target_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load()
    clips = tmp_path / "clips"
    clips.mkdir()
    sidecar = harness._fixture_sidecar()
    (clips / sidecar.metadata_file).write_bytes(sidecar.to_canonical_json())
    catalog = tmp_path / "catalog.sqlite3"
    with sqlite3.connect(catalog) as connection:
        connection.execute(
            "CREATE TABLE clips (clip_id TEXT PRIMARY KEY, sidecar_path TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO clips (clip_id, sidecar_path) VALUES (?, ?)",
            (str(sidecar.clip_id), f"clips/{sidecar.metadata_file}"),
        )
    monkeypatch.setattr(harness, "CATALOG_PATH", catalog)
    monkeypatch.setattr(harness, "CLIPS_ROOT", clips)

    assert harness._catalog_sidecar(sidecar.clip_id) == sidecar


def test_exact_clip_uuid_catalog_lookup_retries_rename_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load()
    clips = tmp_path / "clips"
    clips.mkdir()
    sidecar = harness._fixture_sidecar()
    catalog = tmp_path / "catalog.sqlite3"
    with sqlite3.connect(catalog) as connection:
        connection.execute(
            "CREATE TABLE clips (clip_id TEXT PRIMARY KEY, sidecar_path TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO clips (clip_id, sidecar_path) VALUES (?, ?)",
            (str(sidecar.clip_id), f"clips/{sidecar.metadata_file}"),
        )
    monkeypatch.setattr(harness, "CATALOG_PATH", catalog)
    monkeypatch.setattr(harness, "CLIPS_ROOT", clips)

    assert harness._catalog_sidecar(sidecar.clip_id) is None


def test_qualification_lock_refuses_a_concurrent_owner_before_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load()

    class ContendedFcntl:
        LOCK_EX = 2
        LOCK_NB = 4

        @staticmethod
        def flock(_descriptor: int, operation: int) -> None:
            assert operation == ContendedFcntl.LOCK_EX | ContendedFcntl.LOCK_NB
            raise BlockingIOError("held")

    monkeypatch.setitem(sys.modules, "fcntl", ContendedFcntl)
    monkeypatch.setattr(harness, "LOCK_PATH", tmp_path / "qualification.lock")

    with (
        pytest.raises(harness.HarnessError, match="already active"),
        harness._qualification_lock(),
    ):
        pytest.fail("contended qualification lock yielded")


def test_cli_is_closed_and_requires_exact_identity_arguments() -> None:
    harness = _load()
    parser = harness._parser()

    with pytest.raises(SystemExit):
        parser.parse_args([])
    arguments = parser.parse_args(
        [
            "--expected-manifest-sha256",
            "0" * 64,
            "--expected-release",
            "0.1.0.dev0-0123456789abcdef",
            "--expected-board-serial",
            "00000000db28ffe4",
            "--expected-storage-uuid",
            "7EED-3EA7",
            "--output",
            "/var/lib/dashcam/result.json",
        ]
    )

    assert vars(arguments) == {
        "expected_manifest_sha256": "0" * 64,
        "expected_release": "0.1.0.dev0-0123456789abcdef",
        "expected_board_serial": "00000000db28ffe4",
        "expected_storage_uuid": "7EED-3EA7",
        "output": Path("/var/lib/dashcam/result.json"),
    }


def test_global_work_and_input_bounds_are_fixed() -> None:
    harness = _load()

    assert 10 <= harness.CLIP_DURATION_S <= 60
    assert 0.1 <= harness.STALE_AFTER_S <= 5
    assert 60 <= harness.SCENARIO_TIMEOUT_S <= 240
    assert harness.START_TIMEOUT_S <= 45
    assert harness.PHASE_TIMEOUT_S <= 90
    assert harness.STOP_TIMEOUT_S <= 35
    assert harness.MAX_SENTENCES <= 2048
    assert harness.MAX_CLIP_ENTRIES <= 4096
    assert harness.MAX_NEW_SIDECARS <= 16
    assert harness.MAX_STATUS_BYTES <= 64 * 1024
    assert harness.MAX_RESULT_BYTES <= 1024 * 1024


def test_source_has_no_storage_network_package_or_clock_mutators() -> None:
    source = HARNESS_PATH.read_text(encoding="utf-8").casefold()
    forbidden = (
        "nmcli",
        "hostapd",
        "wpa_supplicant",
        "sfdisk",
        "parted",
        "mkfs",
        "wipefs",
        "apt-get",
        "apt ",
        "timedatectl",
        "clock_settime",
        "settimeofday",
        "systemd-run",
        "shutil.rmtree",
    )
    for token in forbidden:
        assert token not in source
    assert "systemctl\", \"start\", \"dashcamd.service" not in source
    assert "systemctl\", \"restart\"" not in source
    assert "restart=no" in source
    assert "protectclock=yes" in source
    assert "status runtime directory contains a foreign entry" in source


def test_readme_states_hardware_scope_privacy_and_local_time_exclusions() -> None:
    readme = README_PATH.read_text(encoding="utf-8")

    assert "production `dashcam.daemon`" in readme
    assert "temporary systemd unit" in readme
    assert "PTY" in readme
    assert "ordinary `dashcamd.service`" in readme
    assert "do not copy raw sidecars to Windows" in readme
    assert "UTC-midnight" in readme
    assert "DST transition logic remain deterministic local tests" in readme
    assert "never runs partition, format, mount, network, AP, package" in readme


def test_release_gate_uses_installer_identity_not_digest_suffixed_package_version() -> None:
    source = HARNESS_PATH.read_text(encoding="utf-8")

    assert "installed.json" in source
    assert "/var/lib/dashcam/app-install-v1.json" in source
    assert 'applied.get("release_id") != expected_release' in source
    assert 'applied.get("manifest_sha256") != marker["manifest_sha256"]' in source
    assert "dashcam.__version__ != expected_release" not in source


def test_anchor_conflict_vector_exceeds_configured_allowance() -> None:
    assert timedelta(seconds=120) > timedelta(
        milliseconds=default_config().gps.anchor_max_conflict_ms
        + 2 * default_config().gps.anchor_uncertainty_ms
    )


def test_checked_bundle_manifest_matches_current_bytes() -> None:
    entries = {}
    for line in MANIFEST_PATH.read_text(encoding="ascii").splitlines():
        digest, name = line.split("  ")
        entries[name] = digest

    assert set(entries) == {"README.md", "run.py"}
    assert entries["README.md"] == hashlib.sha256(README_PATH.read_bytes()).hexdigest()
    assert entries["run.py"] == hashlib.sha256(HARNESS_PATH.read_bytes()).hexdigest()
