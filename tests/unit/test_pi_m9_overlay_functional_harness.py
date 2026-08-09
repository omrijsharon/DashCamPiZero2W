from __future__ import annotations

import hashlib
import importlib.util
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import cast
from uuid import UUID

import pytest

from dashcam.config import default_config
from dashcam.gps.nmea import parse_nmea_line
from dashcam.overlay import render_luma_bitmap

ROOT = Path(__file__).resolve().parents[2]
HARNESS_PATH = ROOT / "deploy/ssh-dev-validation/milestone9-overlay/run.py"
README_PATH = HARNESS_PATH.with_name("README.md")
MANIFEST_PATH = HARNESS_PATH.with_name("SHA256SUMS")


def _load() -> ModuleType:
    name = "pi_m9_overlay_functional_harness"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, HARNESS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_manifest_is_closed_to_readme_and_script(tmp_path: Path) -> None:
    harness = _load()
    for name, payload in (("README.md", b"reviewed\n"), ("run.py", b"pass\n")):
        (tmp_path / name).write_bytes(payload)
    entries = {
        name: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
        for name in ("README.md", "run.py")
    }
    manifest = "".join(f"{digest}  {name}\n" for name, digest in entries.items()).encode("ascii")
    (tmp_path / "SHA256SUMS").write_bytes(manifest)

    assert harness.verify_manifest(hashlib.sha256(manifest).hexdigest(), tmp_path) == entries

    (tmp_path / "extra").write_text("unreviewed\n", encoding="ascii")
    changed = manifest + f"{'0' * 64}  extra\n".encode("ascii")
    (tmp_path / "SHA256SUMS").write_bytes(changed)
    with pytest.raises(harness.HarnessError, match="not closed"):
        harness.verify_manifest(hashlib.sha256(changed).hexdigest(), tmp_path)


def test_checked_manifest_verifies_current_bundle() -> None:
    harness = _load()
    digest = hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest()

    entries = harness.verify_manifest(digest)

    assert tuple(sorted(entries)) == harness.MANIFEST_MEMBERS


def test_only_accepted_release_is_declared() -> None:
    harness = _load()

    assert harness.ACCEPTED_RELEASE == "0.1.0.dev0-5f95dd806342ac9e"
    assert harness.RELEASE_RE.fullmatch(harness.ACCEPTED_RELEASE)
    assert harness.EXPECTED_INSTALLED_MANIFEST_SHA256 == (
        "619fe30e8123e0ceaec55269de0a6faf6ec88ccb4859a98bbef2d87776dbb655"
    )
    assert harness.EXPECTED_PRODUCTION_CONFIG_SHA256 == (
        "1276363286475bccf85e70332ec893846e3fe3572e8184991843400ac4d6c4b8"
    )


def test_temporary_config_changes_only_closed_timing_and_pty_fields() -> None:
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
    assert observed.overlay == base.overlay
    assert observed.overlay.enabled
    assert observed.video.width == 1920
    assert observed.video.height == 1080
    assert observed.video.fps == 30
    assert observed.video.hardware_encoder_required
    assert observed.audio == base.audio
    assert observed.storage == base.storage
    assert observed.network == base.network
    assert not observed.time.discipline_system_clock


def test_transient_unit_is_nonrestarting_clock_and_network_closed() -> None:
    harness = _load()
    interpreter = Path("/opt/dashcam/releases/0.1.0.dev0-5f95dd806342ac9e/venv/bin/python")

    unit = harness.render_transient_unit(
        interpreter=interpreter,
        config_path=harness.TEMP_CONFIG_PATH,
    )

    assert "Restart=no" in unit
    assert "ProtectClock=yes" in unit
    assert "RestrictAddressFamilies=AF_UNIX" in unit
    assert "CapabilityBoundingSet=\n" in unit
    assert "AmbientCapabilities=\n" in unit
    assert "PrivateDevices=no" in unit
    assert "User=dashcam" in unit
    assert "StateDirectory=dashcam" in unit
    assert "StateDirectoryMode=0750" in unit
    assert f"--config {harness.TEMP_CONFIG_PATH}" in unit
    assert "dashcam-network-fallback" not in unit
    assert "NetworkManager" not in unit
    assert "ExecStartPre" not in unit
    assert "ExecStartPost" not in unit


def test_transient_unit_refuses_foreign_paths() -> None:
    harness = _load()
    with pytest.raises(harness.HarnessError, match="interpreter"):
        harness.render_transient_unit(
            interpreter=Path("/usr/bin/python3"),
            config_path=harness.TEMP_CONFIG_PATH,
        )
    with pytest.raises(harness.HarnessError, match="config"):
        harness.render_transient_unit(
            interpreter=Path("/opt/dashcam/releases/0.1.0.dev0-5f95dd806342ac9e/venv/bin/python"),
            config_path=Path("/tmp/config.toml"),
        )


def test_synthetic_rmc_is_bounded_checksum_valid_and_zero_coordinate() -> None:
    harness = _load()
    record = harness._rmc(harness.BASE_UTC)

    assert len(record) <= 82
    assert b"0000.0000,N,00000.0000,E" in record
    parsed = parse_nmea_line(record, received_monotonic_ns=123)
    assert parsed.ok
    assert parsed.sentence is not None
    assert parsed.sentence.navigation_valid
    assert parsed.sentence.latitude_deg == 0
    assert parsed.sentence.longitude_deg == 0


def test_startup_failure_summary_is_bounded_and_privacy_safe() -> None:
    harness = _load()
    summary = harness._startup_failure_summary(
        {
            "lifecycle": {
                "state": "FAULTED",
                "reason": "CONFIG_ERROR",
                "detail": "closed diagnostic\nwithout telemetry",
            }
        }
    )

    assert summary["lifecycle_state"] == "FAULTED"
    assert summary["reason"] == "CONFIG_ERROR"
    assert summary["detail"] == "closed diagnostic without telemetry"
    assert summary["runtime_snapshot_present"] is False
    harness._assert_privacy_safe(summary)


def test_frozen_bitmap_classifier_tolerates_bounded_codec_noise() -> None:
    harness = _load()
    expected = render_luma_bitmap("TIME UNSYNCED  REC\nGPS INVALID")
    observed = bytearray(expected)
    # Compression-like luma perturbation remains on the same side of the
    # classifier's frozen binary threshold.
    for index, value in enumerate(observed):
        observed[index] = 210 if value >= harness.BINARY_THRESHOLD else 35

    assert harness._bitmap_f1(bytes(observed), expected) == 1.0


def test_classifier_requires_correct_state_and_wrong_template_margin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load()
    correct = render_luma_bitmap("TIME UNSYNCED  REC\nGPS INVALID")
    wrong = render_luma_bitmap("TIME UNSYNCED  REC\nGPS LOST")
    sidecar = SimpleNamespace(
        clip_id=UUID("12345678-1234-5678-9234-567812345678"),
        start_monotonic_ns=1_000_000_000,
        end_monotonic_ns=3_000_000_000,
        video_file="unused.mp4",
    )
    observation = harness.PhaseObservation(
        name="startup_unsynced",
        monotonic_ns=2_000_000_000,
        status={},
    )
    monkeypatch.setattr(
        harness,
        "_decode_overlay_frame",
        lambda *_args, **_kwargs: harness.DecodedOverlayFrame(correct, 0.0, 1.0),
    )
    monkeypatch.setattr(
        harness,
        "_expected_bitmap",
        lambda _sidecar, _config, _when, state: correct if state == "unsynced" else wrong,
    )

    evidence = harness._classify_burn_in(
        sidecar,
        default_config(),
        observation,
        "unsynced",
        "stale",
    )

    assert evidence["passed"] is True
    assert evidence["correct_template_f1"] == 1.0
    assert evidence["wrong_template_margin"] >= harness.MIN_WRONG_TEMPLATE_MARGIN
    assert "text" not in repr(evidence).casefold()


def test_classifier_refuses_ambiguous_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load()
    bitmap = render_luma_bitmap("TIME UNSYNCED  REC\nGPS INVALID")
    sidecar = SimpleNamespace(
        clip_id=UUID("12345678-1234-5678-9234-567812345678"),
        start_monotonic_ns=1_000_000_000,
        end_monotonic_ns=3_000_000_000,
        video_file="unused.mp4",
    )
    observation = harness.PhaseObservation("startup_unsynced", 2_000_000_000, {})
    monkeypatch.setattr(
        harness,
        "_decode_overlay_frame",
        lambda *_args, **_kwargs: harness.DecodedOverlayFrame(bitmap, 0.0, 1.0),
    )
    monkeypatch.setattr(harness, "_expected_bitmap", lambda *_args: bitmap)

    with pytest.raises(harness.HarnessError, match="classifier refused"):
        harness._classify_burn_in(
            sidecar,
            default_config(),
            observation,
            "unsynced",
            "stale",
        )


def test_pts_mapping_refuses_nonzero_first_pts_and_wrong_selected_pts() -> None:
    harness = _load()
    sidecar = SimpleNamespace(
        start_monotonic_ns=1_000_000_000,
        end_monotonic_ns=5_000_000_000,
    )

    assert harness._validate_pts_mapping(
        sidecar,
        2_000_000_000,
        first_pts_s=0.0,
        selected_pts_s=1.0,
    ) == (2_000_000_000, 0)
    with pytest.raises(harness.HarnessError, match="first video PTS"):
        harness._validate_pts_mapping(
            sidecar,
            2_000_000_000,
            first_pts_s=0.050,
            selected_pts_s=1.050,
        )
    with pytest.raises(harness.HarnessError, match="monotonic mapping"):
        harness._validate_pts_mapping(
            sidecar,
            2_000_000_000,
            first_pts_s=0.0,
            selected_pts_s=1.2,
        )
    assert harness._parse_showinfo_pts(b"[Parsed_showinfo_1] pts:30 pts_time:1.000000\n") == 1.0
    with pytest.raises(harness.HarnessError, match="shape differs"):
        harness._parse_showinfo_pts(b"pts_time:1.000000\npts_time:1.033333\n")


def test_complete_clip_uses_actual_packet_fps_and_surfaces_observer_lag() -> None:
    harness = _load()
    packets, duration_s = harness._parse_video_packet_metrics(
        b'{"programs":[],"streams":[{"duration":"59.022300","nb_read_packets":"1771"}]}'
    )

    evidence = harness._complete_clip_metrics(
        sidecar_duration_s=59.0221,
        sidecar_frames_written=1799,
        actual_duration_s=duration_s,
        actual_video_packets=packets,
        dropped_frames=0,
        hardware_profile=True,
    )

    assert evidence["actual_video_packets"] == 1771
    assert evidence["actual_packet_fps"] == pytest.approx(30.005608, abs=0.000001)
    assert evidence["frame_observer_packet_delta"] == 28
    assert evidence["sidecar_frame_counter_matches_actual_packets"] is False
    assert evidence["sidecar_counter_used_for_functional_fps"] is False


@pytest.mark.parametrize("duration_s", [59.0, 61.0])
def test_complete_clip_accepts_closed_normal_duration_bounds(duration_s: float) -> None:
    harness = _load()

    evidence = harness._complete_clip_metrics(
        sidecar_duration_s=duration_s,
        sidecar_frames_written=1800,
        actual_duration_s=duration_s,
        actual_video_packets=round(duration_s * 30),
        dropped_frames=0,
        hardware_profile=True,
    )

    assert evidence["actual_packet_fps"] >= harness.MIN_COMPLETE_FPS


@pytest.mark.parametrize("duration_s", [58.999999, 61.000001])
def test_complete_clip_refuses_duration_outside_normal_bounds(duration_s: float) -> None:
    harness = _load()

    with pytest.raises(harness.HarnessError, match="duration/fps/drop gate"):
        harness._complete_clip_metrics(
            sidecar_duration_s=duration_s,
            sidecar_frames_written=1800,
            actual_duration_s=60.0,
            actual_video_packets=1800,
            dropped_frames=0,
            hardware_profile=True,
        )


def test_complete_clip_refuses_low_actual_packet_fps_despite_sidecar_counter() -> None:
    harness = _load()

    with pytest.raises(harness.HarnessError, match="duration/fps/drop gate"):
        harness._complete_clip_metrics(
            sidecar_duration_s=60.0,
            sidecar_frames_written=1800,
            actual_duration_s=60.0,
            actual_video_packets=1793,
            dropped_frames=0,
            hardware_profile=True,
        )


def test_offline_analysis_requires_verified_clean_transient_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load()
    calls: list[tuple[tuple[str, ...], float]] = []
    clean = {
        "LoadState": "loaded",
        "ActiveState": "inactive",
        "SubState": "dead",
        "MainPID": "0",
        "NRestarts": "0",
        "Result": "success",
        "ExecMainStatus": "0",
    }
    monkeypatch.setattr(
        harness,
        "_systemctl",
        lambda *args, timeout: calls.append((args, timeout)),
    )
    monkeypatch.setattr(harness, "_unit_properties", lambda _unit: clean)

    evidence = harness._stop_transient_for_offline_analysis(timeout=7.5)

    assert calls == [(("stop", harness.UNIT_NAME), 7.5)]
    assert evidence["result_success"] is True
    failed = dict(clean, Result="exit-code")
    monkeypatch.setattr(harness, "_unit_properties", lambda _unit: failed)
    with pytest.raises(harness.HarnessError, match="did not stop cleanly"):
        harness._stop_transient_for_offline_analysis(timeout=7.5)


def test_scenario_orders_clean_stop_before_any_offline_analysis() -> None:
    source = HARNESS_PATH.read_text(encoding="utf-8")
    scenario = source[source.index("def _run_scenario(") : source.index("\n_ACTIVE_ENDPOINT:")]

    stop_at = scenario.index("pre_analysis_stop = _stop_transient_for_offline_analysis")
    classify_at = scenario.index("classifications = (")
    media_at = scenario.index('"first_complete_clip": _media_evidence')
    assert stop_at < classify_at < media_at


def test_phase_sample_ownership_refuses_empty_valid_and_sampled_stale() -> None:
    harness = _load()
    clip_id = UUID("12345678-1234-5678-9234-567812345678")
    anchor = SimpleNamespace(monotonic_ns=1_000_000_000, utc=harness.BASE_UTC)
    sample = SimpleNamespace(
        monotonic_ns=2_000_000_000,
        utc=harness.BASE_UTC + timedelta(seconds=1),
        lat_deg=0.0,
        lon_deg=0.0,
        speed_mps=0.0,
    )
    observation = harness.PhaseObservation(
        "valid_lock",
        2_000_000_000,
        {},
        dwell_start_ns=1_500_000_000,
        dwell_end_ns=2_500_000_000,
    )
    populated = SimpleNamespace(
        clip_id=clip_id,
        time_anchor=anchor,
        gps=SimpleNamespace(samples=(sample,)),
    )
    empty = SimpleNamespace(
        clip_id=clip_id,
        time_anchor=anchor,
        gps=SimpleNamespace(samples=()),
    )

    evidence = harness._phase_sample_evidence(
        populated,
        observation,
        expect_samples=True,
    )
    assert evidence["sample_count"] == 1
    assert evidence["all_samples_zero_location"] is True
    with pytest.raises(harness.HarnessError, match="contains no"):
        harness._phase_sample_evidence(empty, observation, expect_samples=True)
    with pytest.raises(harness.HarnessError, match="unexpectedly contains"):
        harness._phase_sample_evidence(populated, observation, expect_samples=False)


def test_boundary_requires_exact_adjacent_nonoverlapping_sidecars() -> None:
    harness = _load()
    first = SimpleNamespace(
        clip_id=UUID("12345678-1234-5678-9234-567812345678"),
        sequence=10,
        start_monotonic_ns=1_000,
        end_monotonic_ns=2_000,
        gps=SimpleNamespace(samples=(SimpleNamespace(monotonic_ns=1_500),)),
    )
    second = SimpleNamespace(
        clip_id=UUID("22345678-1234-5678-9234-567812345678"),
        sequence=11,
        start_monotonic_ns=2_000,
        end_monotonic_ns=3_000,
        gps=SimpleNamespace(samples=(SimpleNamespace(monotonic_ns=2_500),)),
    )

    assert harness._boundary_evidence(first, second)["exact_half_open_adjacency"] is True
    gapped = SimpleNamespace(
        clip_id=second.clip_id,
        sequence=second.sequence,
        start_monotonic_ns=2_001,
        end_monotonic_ns=second.end_monotonic_ns,
        gps=second.gps,
    )
    with pytest.raises(harness.HarnessError, match="not exactly adjacent"):
        harness._boundary_evidence(first, gapped)
    overlapping = SimpleNamespace(
        clip_id=second.clip_id,
        sequence=second.sequence,
        start_monotonic_ns=second.start_monotonic_ns,
        end_monotonic_ns=second.end_monotonic_ns,
        gps=SimpleNamespace(samples=(SimpleNamespace(monotonic_ns=1_500),)),
    )
    with pytest.raises(harness.HarnessError, match="overlap"):
        harness._boundary_evidence(first, overlapping)


def test_privacy_guard_rejects_coordinates_raw_pixels_media_and_nmea() -> None:
    harness = _load()
    harness._assert_privacy_safe(
        {
            "privacy": {
                "coordinates_retained_in_result": False,
                "decoded_pixels_retained_in_result": False,
            },
            "classification": {"correct_template_f1": 0.99},
        }
    )
    documents = cast(
        tuple[dict[str, object], ...],
        (
            {"lat_deg": 0.0},
            {"samples": []},
            {"crop": "pixels"},
            {"detail": "$GNRMC,private"},
        ),
    )
    for document in documents:
        with pytest.raises(harness.HarnessError, match=r"privacy|NMEA"):
            harness._assert_privacy_safe(document)


def test_qualification_lock_refuses_concurrent_owner(
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
            harness.ACCEPTED_RELEASE,
            "--expected-board-serial",
            "00000000db28ffe4",
            "--expected-storage-uuid",
            "7EED-3EA7",
            "--output",
            "/var/lib/dashcam/result.json",
        ]
    )
    assert arguments.expected_release == harness.ACCEPTED_RELEASE
    assert arguments.output == Path("/var/lib/dashcam/result.json")


def test_global_bounds_and_classifier_contract_are_frozen() -> None:
    harness = _load()

    assert harness.CLIP_DURATION_S == 60
    assert harness.STALE_AFTER_S == 2.0
    assert harness.PHASE_DWELL_S >= 2 * harness.OVERLAY_INTERVAL_S
    assert 120 <= harness.SCENARIO_TIMEOUT_S <= 190
    assert harness.BOUNDARY_TIMEOUT_S <= 90
    assert harness.MAX_SENTENCES <= 2048
    assert harness.MAX_RECONCILIATION_BACKLOG == 64
    assert harness.MAX_SCENARIO_CANONICAL_SIDECARS == 2
    assert harness.MAX_NEW_SIDECARS == 66
    assert harness.OVERLAY_X == harness.OVERLAY_Y == 40
    assert (harness.OVERLAY_WIDTH, harness.OVERLAY_HEIGHT) == (1152, 64)
    assert harness.BINARY_THRESHOLD == 128
    assert harness.MIN_TEMPLATE_F1 == 0.88
    assert harness.MIN_WRONG_TEMPLATE_MARGIN == 0.08
    assert harness.MIN_COMPLETE_FPS == 29.9
    assert harness.MIN_COMPLETE_DURATION_S == 59.0
    assert harness.MAX_COMPLETE_DURATION_S == 61.0
    assert harness.FIRST_PTS_TOLERANCE_S == 0.040
    assert harness.PTS_MAPPING_TOLERANCE_S < 0.040


def test_source_has_no_storage_network_package_clock_or_physical_mutators() -> None:
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
        "timedatectl",
        "clock_settime",
        "settimeofday",
        "systemd-run",
        "shutil.rmtree",
        "/dev/ttyama0",
    )
    for token in forbidden:
        assert token not in source
    assert 'systemctl", "start", "dashcamd.service' not in source
    assert 'systemctl", "restart"' not in source
    assert "restart=no" in source
    assert "protectclock=yes" in source
    assert "ffmpeg" in source
    assert "render_luma_bitmap" in source


def test_readme_states_scope_privacy_classifier_and_remaining_matrix() -> None:
    readme = README_PATH.read_text(encoding="utf-8")

    assert "production `dashcam.daemon`" in readme
    assert "temporary systemd unit" in readme
    assert "PTY" in readme
    assert "ordinary `dashcamd.service`" in readme
    assert "render_luma_bitmap" in readme
    assert "F1 >= 0.88" in readme
    assert "do not copy raw sidecars or media to Windows" in readme
    assert "not a claim" in readme
    assert "Section C1 paired ten-clip" in readme
