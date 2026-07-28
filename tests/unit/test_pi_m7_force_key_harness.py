from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
HARNESS_PATH = ROOT / "deploy/ssh-dev-validation/milestone7-force-key/run.py"
README_PATH = HARNESS_PATH.with_name("README.md")
MANIFEST_PATH = HARNESS_PATH.with_name("SHA256SUMS")


def _load() -> ModuleType:
    name = "pi_m7_force_key_harness"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, HARNESS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_force_key_parser_requires_official_downstream_shape() -> None:
    harness = _load()

    class Video:
        @staticmethod
        def video_event_is_force_key_unit(_event: object) -> bool:
            return True

        @staticmethod
        def video_event_parse_downstream_force_key_unit(
            _event: object,
        ) -> tuple[bool, int, int, int, bool, int]:
            return (True, 1, 2, 3, True, 7)

    assert harness._parse_force_key_event(Video(), object()) == (7, True)

    class NonForce(Video):
        @staticmethod
        def video_event_is_force_key_unit(_event: object) -> bool:
            return False

    assert harness._parse_force_key_event(NonForce(), object()) is None

    class Bad(Video):
        @staticmethod
        def video_event_parse_downstream_force_key_unit(_event: object) -> tuple[bool, int]:
            return (True, 1)

    assert harness._parse_force_key_event(Bad(), object()) is None


def test_counter_rejects_non_monotonic_and_greater_than_one_frame_gaps() -> None:
    harness = _load()

    class Buffer:
        def __init__(self, pts: int) -> None:
            self.pts = pts

    counter = harness.PadCounter()
    for pts in (0, harness.FRAME_NS, harness.FRAME_NS * 4, harness.FRAME_NS * 3):
        counter.observe(Buffer(pts))
    observed = counter.snapshot()
    assert observed["count"] == 4
    assert observed["large_gaps"] == 1
    assert observed["non_monotonic"] == 1


def test_gop_offset_gate_is_deterministic_at_boundary() -> None:
    harness = _load()
    assert harness.MAX_FORCE_LATENCY_NS == 100_000_000
    assert harness._clock_wait_for_offset(1_130_000_000, 130_000_000) is True
    assert harness._clock_wait_for_offset(1_129_999_999, 130_000_000) is False
    assert harness._clock_wait_for_offset(1_200_000_000, 130_000_000) is False
    assert harness._clock_wait_for_offset(2_470_000_000, 470_000_000) is True


def test_media_idr_scanner_distinguishes_non_idr_nal() -> None:
    harness = _load()
    assert harness._contains_h264_idr("00000000: 00000001 65aabbcc") is True
    assert harness._contains_h264_idr("00000000: 00000002 65aa") is True
    assert harness._contains_h264_idr("00000000: 00000001 41aabbcc") is False
    assert harness._contains_h264_idr_bytes(b"\x00\x00\x00\x01\x65\xaa") is True
    assert harness._contains_h264_idr_bytes(b"\x00\x00\x00\x02\x65\xaa") is True
    assert harness._contains_h264_idr_bytes(b"\x00\x00\x00\x02\x41\xaa") is False


def test_exclusive_result_writer_refuses_recording_volume_and_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _load()
    output = tmp_path / "result.json"
    harness._write_atomic_exclusive_json(output, {"schema_version": 1})
    assert json.loads(output.read_bytes()) == {"schema_version": 1}
    with pytest.raises(harness.HarnessError, match="new direct"):
        harness._write_atomic_exclusive_json(output, {"schema_version": 1})
    recording = tmp_path / "recording"
    recording.mkdir()
    monkeypatch.setattr(harness, "RECORDING_ROOT", recording)
    with pytest.raises(harness.HarnessError, match="outside"):
        harness._write_atomic_exclusive_json(recording / "evidence.json", {"schema_version": 1})


def test_source_uses_only_the_upstream_helper_and_exact_hardware_contract() -> None:
    source = HARNESS_PATH.read_text(encoding="utf-8")
    assert "if not storage.ready:" in source
    assert 'storage.state.value != "ready"' not in source
    assert 'gi.require_version("GstVideo", "1.0")' in source
    assert "video_event_new_upstream_force_key_unit" in source
    assert "video_event_new_downstream_force_key_unit" not in source
    assert "video_event_parse_downstream_force_key_unit" in source
    assert "v4l2h264enc name=encoder" in source
    assert "repeat_sequence_header=1,video_bitrate=8000000,h264_i_frame_period=30" in source
    assert "format=(string)NV12" in source
    assert "profile=(string)high,level=(string)4.1" in source
    assert "send-keyframe-requests=false" in source
    assert '"h264_v4l2m2m"' in source
    assert "systemctl start" not in source
    assert "systemctl stop" not in source
    assert "systemctl restart" not in source
    assert "alsasrc" not in source
    assert "/sys/" not in source
    assert '"service_operations": 0' in source
    assert '"audio_operations": 0' in source
    assert '"sysfs_operations": 0' in source


def test_experiment_contract_observes_event_then_idr_and_keeps_identity() -> None:
    source = HARNESS_PATH.read_text(encoding="utf-8")
    request = source.split("def request_force_key", 1)[1].split("def stop", 1)[0]
    probes = source.split("def _add_probes", 1)[1].split("def _record", 1)[0]
    assert "timeout_ns: int = 10_000_000" in source
    assert 'encoder.get_static_pad("src")' in request
    assert 'parser.get_static_pad("src")' not in request
    assert "source.send_event(event)" in request
    assert request.index("self.requests[count]") < request.index("source.send_event(event)")
    assert "downstream_event_ns" in probes
    assert "idr_pts_ns" in probes
    assert "request_to_idr_ns" in probes
    assert "request_to_idr_media_ns" in probes
    assert "event_to_idr_ns" in probes
    assert "EVENT_DOWNSTREAM" in probes
    assert "BufferFlags.DELTA_UNIT" in probes
    assert "request_seqnum" in probes
    assert "downstream_seqnum" in probes
    assert "seqnum_preserved" in probes
    assert "downstream force-key event seqnum differs" not in probes
    assert "natural_force_events" in probes
    assert "_contains_h264_idr_bytes" in probes
    assert "first_key_after_event_nal5" in probes
    assert "_assert_identity()" in request
    assert 'request["request_to_idr_media_ns"]' in request
    assert "limit {MAX_FORCE_LATENCY_NS} ns" in request


def test_cli_failure_is_bounded_exclusive_and_not_an_integration_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _load()
    output = tmp_path / "result.json"
    monkeypatch.setattr(harness, "verify_manifest", lambda _expected: {})
    monkeypatch.setattr(
        harness,
        "execute",
        lambda _directory: (_ for _ in ()).throw(harness.HarnessError("refused")),
    )
    status = harness.main(
        [
            "--expected-manifest-sha256",
            "a" * 64,
            "run-experiment",
            "--output-directory",
            "/srv/dashcam/quarantine/m7-forcekey-20260728a",
            "--output",
            str(output),
        ]
    )
    document = json.loads(output.read_bytes())
    assert status == 1
    assert document["passed"] is False
    assert document["safe_to_integrate_production"] is False
    assert document["error"] == "refused"
    assert document["ended_monotonic_ns"] >= document["started_monotonic_ns"]


def test_closed_manifest_and_readme_scope() -> None:
    harness = _load()
    lines = MANIFEST_PATH.read_text(encoding="ascii").splitlines()
    assert [line.split("  ", 1)[1] for line in lines] == ["README.md", "run.py"]
    manifest_hash = hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest()
    assert tuple(harness.verify_manifest(manifest_hash, MANIFEST_PATH.parent)) == (
        "README.md",
        "run.py",
    )
    readme = README_PATH.read_text(encoding="utf-8")
    assert "capability experiment" in readme
    assert "does **not** by itself prove a safe restoration handoff" in " ".join(readme.split())
    assert "GstVideo.video_event_new_upstream_force_key_unit" in readme
    assert "--output /tmp/m7-forcekey-" in readme
    assert "--output /var/lib/dashcam/" not in readme
