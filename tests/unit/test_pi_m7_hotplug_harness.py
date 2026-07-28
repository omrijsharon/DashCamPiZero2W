from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from dashcam.audio.alsa import AlsaCaptureDevice, AlsaIdentity
from dashcam.audio.linux import AudioDiscoveryOutcome, AudioDiscoveryStatus
from dashcam.config import default_config

ROOT = Path(__file__).resolve().parents[2]
HARNESS_PATH = (
    ROOT / "deploy/ssh-dev-validation/milestone7-hotplug/run.py"
)
README_PATH = HARNESS_PATH.with_name("README.md")
MANIFEST_PATH = HARNESS_PATH.with_name("SHA256SUMS")


def _load() -> ModuleType:
    name = "pi_m7_hotplug_harness"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, HARNESS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _matched() -> AudioDiscoveryOutcome:
    return AudioDiscoveryOutcome(
        AudioDiscoveryStatus.MATCHED,
        AlsaCaptureDevice(
            AlsaIdentity(
                "08bb",
                "2902",
                physical_path="platform-3f980000.usb-usb-0:1:1.0",
                product="USB_PnP_Sound_Device",
            ),
            1,
            0,
        ),
    )


def test_proposed_quarantine_target_is_observed_but_never_created(tmp_path: Path) -> None:
    harness = _load()
    recording = tmp_path / "recording"
    recording.mkdir()
    quarantine = recording / "quarantine"
    selected = quarantine / "m7-hotplug-20260727a"

    result = harness.validate_absent_quarantine_target(
        selected,
        recording_root=recording,
        quarantine_root=quarantine,
    )

    assert result["proposed_directory_absent"] is True
    assert result["created"] is False
    assert result["quarantine_state"] == "absent"
    assert not quarantine.exists()
    assert not selected.exists()

    quarantine.mkdir()
    result = harness.validate_absent_quarantine_target(
        selected,
        recording_root=recording,
        quarantine_root=quarantine,
    )
    assert result["quarantine_state"] == "existing_exact_directory"
    assert not selected.exists()

    selected.mkdir()
    with pytest.raises(harness.HarnessError, match="already exists"):
        harness.validate_absent_quarantine_target(
            selected,
            recording_root=recording,
            quarantine_root=quarantine,
        )


@pytest.mark.parametrize(
    "name",
    [
        "hotplug-20260727a",
        "m7-hotplug-short",
        "m7-hotplug-UPPERCASE1",
        "m7-hotplug-../../escape",
    ],
)
def test_proposed_quarantine_target_refuses_unsafe_names(
    tmp_path: Path, name: str
) -> None:
    harness = _load()
    recording = tmp_path / "recording"
    recording.mkdir()
    quarantine = recording / "quarantine"
    with pytest.raises(harness.HarnessError, match="safe quarantine"):
        harness.validate_absent_quarantine_target(
            quarantine / name,
            recording_root=recording,
            quarantine_root=quarantine,
        )


def test_gst_inspect_analysis_requires_expected_api_and_records_absent_barriers() -> None:
    harness = _load()
    payload = (
        b"Pad Templates:\n audio_%u\n"
        b"Element Actions:\n split-now\n split-after\n split-at-running-time\n"
        b"Properties:\n async-finalize\n"
    )

    result = harness.analyze_gst_inspect(payload)

    assert result["sha256"] == hashlib.sha256(payload).hexdigest()
    assert result["candidate_barrier_present"] is False
    assert all(result["expected_tokens"].values())
    assert not any(result["candidate_barrier_tokens"].values())

    with pytest.raises(harness.HarnessError, match="public API differs"):
        harness.analyze_gst_inspect(b"audio_%u split-now")


def test_refusal_evidence_is_bound_to_graph_match_and_has_zero_mutations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load()
    proposed = Path("/srv/dashcam/quarantine/m7-hotplug-20260727a")
    monkeypatch.setattr(
        harness,
        "_release_identity",
        lambda: {"release": "test", "venv": "/opt/test", "package": "/opt/test/dashcam"},
    )
    monkeypatch.setattr(
        harness,
        "_read_only_unit_inactive",
        lambda: {"active_state": "inactive", "sub_state": "dead", "main_pid": 0},
    )
    monkeypatch.setattr(
        harness,
        "validate_absent_quarantine_target",
        lambda _path: {
            "recording_root": "/srv/dashcam",
            "proposed_directory": str(proposed),
            "proposed_directory_absent": True,
            "quarantine_state": "absent",
            "created": False,
        },
    )
    monkeypatch.setattr(harness, "load_config", lambda _path: default_config())
    monkeypatch.setattr(harness, "discover_capture_device", lambda _selector: _matched())
    monkeypatch.setattr(
        harness,
        "probe_splitmux_public_api",
        lambda: {
            "gstreamer_version": "GStreamer 1.26.2",
            "public_atomic_track_switch_barrier_present": False,
        },
    )

    result = harness.execute_refusal(proposed)

    assert result["passed"] is False
    assert result["outcome"] == "refused"
    assert result["safe_to_enable_production_hotplug"] is False
    assert result["blockers"] == list(harness.BLOCKERS)
    assert result["production_graph"]["camera_opened"] is False
    assert result["production_graph"]["pipeline_constructed"] is False
    assert result["media"] == []
    assert set(result["mutations"].values()) == {0}
    assert "block+IDLE" in result["required_future_gate"]["transaction"]


def test_exclusive_evidence_writer_refuses_recording_volume_and_existing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load()
    output = tmp_path / "evidence.json"
    harness._write_atomic_exclusive_json(output, {"schema_version": 1})
    assert json.loads(output.read_bytes()) == {"schema_version": 1}
    with pytest.raises(harness.HarnessError, match="new direct"):
        harness._write_atomic_exclusive_json(output, {"schema_version": 1})
    recording_root = tmp_path / "recording"
    recording_root.mkdir()
    monkeypatch.setattr(harness, "RECORDING_ROOT", recording_root)
    with pytest.raises(harness.HarnessError, match="outside"):
        harness._write_atomic_exclusive_json(
            recording_root / "quarantine" / "evidence.json",
            {"schema_version": 1},
        )


def test_cli_always_returns_refusal_and_writes_bounded_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load()
    output = tmp_path / "refusal.json"
    monkeypatch.setattr(harness, "verify_manifest", lambda _expected: {})
    monkeypatch.setattr(
        harness,
        "execute_refusal",
        lambda _directory: {
            "schema_version": 1,
            "passed": False,
            "outcome": "refused",
            "safe_to_enable_production_hotplug": False,
            "media": [],
        },
    )

    result = harness.main(
        [
            "--expected-manifest-sha256",
            "a" * 64,
            "probe-refusal",
            "--output-directory",
            "/srv/dashcam/quarantine/m7-hotplug-20260727a",
            "--output",
            str(output),
        ]
    )

    document = json.loads(output.read_bytes())
    assert result == 1
    assert document["passed"] is False
    assert document["outcome"] == "refused"
    assert document["media"] == []
    assert document["ended_monotonic_ns"] >= document["started_monotonic_ns"]


def test_source_contains_no_media_or_pad_mutation_path() -> None:
    source = HARNESS_PATH.read_text(encoding="utf-8")
    forbidden = (
        "parse_launch(",
        "set_state(",
        "request_pad_simple(",
        "release_request_pad(",
        "add_probe(",
        "remove_probe(",
        "send_eos(",
        "systemctl start",
        "systemctl stop",
        "systemctl restart",
        "/srv/dashcam/pending",
        "/srv/dashcam/clips",
        "h264_v4l2m2m",
        "/usr/bin/ffmpeg",
        "/usr/bin/ffprobe",
    )
    assert all(token not in source for token in forbidden)
    assert '"recording_volume_writes": 0' in source
    assert '"camera_opens": 0' in source
    assert '"request_pad_operations": 0' in source
    readme = README_PATH.read_text(encoding="utf-8")
    assert "deliberately non-mutating" in readme
    assert "always returns nonzero" in readme
    assert "not Milestone 7 completion" in readme


def test_checked_manifest_is_closed_and_matches_members() -> None:
    harness = _load()
    lines = MANIFEST_PATH.read_text(encoding="ascii").splitlines()
    assert [line.split("  ", 1)[1] for line in lines] == ["README.md", "run.py"]
    manifest_hash = harness._sha256_file(MANIFEST_PATH, maximum=4096)
    entries = harness.verify_manifest(manifest_hash, MANIFEST_PATH.parent)
    assert tuple(entries) == ("README.md", "run.py")
