from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
HARNESS_PATH = ROOT / "deploy/ssh-dev-validation/milestone7-production-force-ab/run.py"
README_PATH = HARNESS_PATH.with_name("README.md")
MANIFEST_PATH = HARNESS_PATH.with_name("SHA256SUMS")


def _load() -> ModuleType:
    name = "pi_m7_production_force_ab_harness"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, HARNESS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_idr_scanner_accepts_annex_b_and_avcc_only_for_nal_five() -> None:
    harness = _load()
    assert harness._contains_h264_idr_bytes(b"\x00\x00\x00\x01\x65\xaa")
    assert harness._contains_h264_idr_bytes(b"\x00\x00\x00\x02\x65\xaa")
    assert not harness._contains_h264_idr_bytes(b"\x00\x00\x00\x01\x41\xaa")


def test_force_parser_accepts_both_official_tuple_shapes() -> None:
    harness = _load()

    class Video:
        @staticmethod
        def video_event_is_force_key_unit(_event: object) -> bool:
            return True

        @staticmethod
        def video_event_parse_upstream_force_key_unit(
            _event: object,
        ) -> tuple[bool, int, bool, int]:
            return (True, 1, True, 9)

        @staticmethod
        def video_event_parse_downstream_force_key_unit(
            _event: object,
        ) -> tuple[bool, int, int, int, bool, int]:
            return (True, 1, 2, 3, True, 10)

    assert harness._parse_force_key_event(Video(), object(), "upstream") == (9, True)
    assert harness._parse_force_key_event(Video(), object(), "downstream") == (10, True)


def test_exclusive_result_writer_refuses_recording_and_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _load()
    recording = tmp_path / "recording"
    rootfs = tmp_path / "rootfs"
    recording.mkdir()
    rootfs.mkdir()
    monkeypatch.setattr(harness, "RECORDING_ROOT", recording)
    monkeypatch.setattr(harness, "_is_rootfs_parent", lambda _parent: True)
    output = rootfs / "result.json"
    harness._write_result(output, {"schema_version": 1})
    assert json.loads(output.read_bytes()) == {"schema_version": 1}
    with pytest.raises(harness.HarnessError, match="new direct"):
        harness._write_result(output, {"schema_version": 1})
    with pytest.raises(harness.HarnessError, match="rootfs"):
        harness._write_result(recording / "result.json", {"schema_version": 1})


def test_source_uses_production_graph_and_never_invokes_handoffs_or_service_mutations() -> None:
    source = HARNESS_PATH.read_text(encoding="utf-8")
    assert "build_audio_pipeline_description(plan)" in source
    assert "PyGObjectGStreamerDriver.load()" in source
    assert "driver.create_pipeline(" in source
    assert "video_event_new_upstream_force_key_unit" in source
    assert "driver.arm_audio_loss" not in source
    assert "driver.isolate_audio_loss" not in source
    assert "driver.restore_audio" not in source
    assert "systemctl start" not in source
    assert "systemctl stop" not in source
    assert "systemctl restart" not in source
    assert '"production_isolation_invoked": False' in source
    assert '"production_restoration_invoked": False' in source


def test_a_precedes_deauthorization_error_and_b_in_exact_order() -> None:
    source = HARNESS_PATH.read_text(encoding="utf-8")
    body = source.split("def run_diagnostic()", 1)[1].split("def _parser()", 1)[0]
    assert body.index('diagnostic.force(REQUEST_A') < body.index("write_authorized(authorized, 0")
    assert body.index("write_authorized(authorized, 0") < body.index(
        "diagnostic.wait_audio_error()"
    )
    assert body.index("diagnostic.wait_audio_error()") < body.index('diagnostic.force(REQUEST_B')
    assert body.index("restore_authorized(authorized)") < body.index("diagnostic.stop()")


def test_probe_contract_covers_edges_flows_threads_and_foreign_counts() -> None:
    source = HARNESS_PATH.read_text(encoding="utf-8")
    for label in (
        "encoder_src",
        "parser_src",
        "video_tee_sink",
        "continuity_src",
        "g01_valve_src",
        "g02_valve_src",
        "g03_valve_src",
    ):
        assert label in source
    assert "EVENT_UPSTREAM" in source
    assert "EVENT_DOWNSTREAM" in source
    assert "threading.get_ident()" in source
    assert '"foreign_force_events"' in source
    assert '"clock_identity"' in source
    assert '"base_time_ns"' in source
    assert "generation_snapshot" in source


def test_v2_invokes_current_gate_then_encoder_edge_and_restores_successor() -> None:
    source = HARNESS_PATH.read_text(encoding="utf-8")
    body = source.split("def _production_gate_v2", 1)[1].split(
        "def _make_media_namespace", 1
    )[0]
    assert "ThreadPoolExecutor" in body
    assert "max_workers=1" in body
    assert "context.next_force_key_count = 1" in body
    assert "._prewarm_generation(context, successor)" in body
    assert "._set_generation_linked(context, successor, True)" in body
    assert "._arm_forced_idr_gate(" in body
    assert '"forced-IDR response/IDR wait timed out"' in body
    assert "_diagnostic_encoder_edge_gate(" in body
    assert "._set_generation_linked(context, successor, False)" in body
    assert "._reset_unrouted_generation(" in body
    assert "context.active_generation_id != 1" in body
    assert "isolate_audio_loss" not in body
    assert "restore_audio" not in body


def test_diagnostic_edge_gate_observes_encoder_and_holds_tee_nal5() -> None:
    source = HARNESS_PATH.read_text(encoding="utf-8")
    body = source.split("def _diagnostic_encoder_edge_gate", 1)[1].split(
        "def _production_gate_v2", 1
    )[0]
    assert 'diagnostic.element("encoder")' in body
    assert 'diagnostic.element("video_tee")' in body
    assert "EVENT_DOWNSTREAM" in body
    assert "video_event_parse_downstream_force_key_unit" in body
    assert "video_event_new_upstream_force_key_unit" in body
    assert "_contains_h264_idr_bytes" in body
    assert "release.wait(timeout_s)" in body
    assert "_release_block_probe" in body
    assert '"dispatch_synchronous"' in body


def test_parser_blind_spot_shape_requires_exact_two_edges_and_no_parser_source() -> None:
    harness = _load()
    exact = [
        {"count": 1, "direction": "downstream", "pad": "encoder.src"},
        {"count": 1, "direction": "downstream", "pad": "parser.sink"},
        {"count": 0, "direction": "upstream", "pad": "parser.src"},
    ]
    assert harness._parser_blind_spot_shape(exact, 1) == {
        "encoder.src": 1,
        "parser.sink": 1,
        "parser.src": 0,
    }
    with pytest.raises(harness.HarnessError, match="blind-spot"):
        harness._parser_blind_spot_shape(
            [*exact, {"count": 1, "direction": "downstream", "pad": "parser.src"}],
            1,
        )
    with pytest.raises(harness.HarnessError, match="blind-spot"):
        harness._parser_blind_spot_shape(exact[:1], 1)


def test_main_failure_is_fresh_bounded_and_not_an_integration_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _load()
    output = tmp_path / "result.json"
    monkeypatch.setattr(harness, "verify_manifest", lambda _expected: {})
    monkeypatch.setattr(harness, "_is_rootfs_parent", lambda _parent: True)
    monkeypatch.setattr(
        harness,
        "run_diagnostic",
        lambda: (_ for _ in ()).throw(harness.HarnessError("refused")),
    )
    status = harness.main(
        [
            "--expected-manifest-sha256",
            "a" * 64,
            "run-diagnostic",
            "--output",
            str(output),
        ]
    )
    document = json.loads(output.read_bytes())
    assert status == 1
    assert document["passed"] is False
    assert document["safe_to_integrate_production"] is False
    assert document["authorization_restored"] is False
    assert "refused" in document["failures"][0]
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
    readme = " ".join(README_PATH.read_text(encoding="utf-8").split())
    assert "diagnostic evidence only" in readme
    assert "does not prove physical unplug, restoration" in readme
    assert "--output /tmp/m7-production-force-ab-" in readme
