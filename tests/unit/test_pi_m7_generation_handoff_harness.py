from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
HARNESS_PATH = ROOT / "deploy/ssh-dev-validation/milestone7-generation-handoff/run.py"
README_PATH = HARNESS_PATH.with_name("README.md")
MANIFEST_PATH = HARNESS_PATH.with_name("SHA256SUMS")


def _load() -> ModuleType:
    name = "pi_m7_generation_handoff_harness"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, HARNESS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _probe_document(audio: bool, *, skew: float = 0.02) -> dict[str, object]:
    streams: list[dict[str, object]] = [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": "h264",
            "profile": "High",
            "width": 1920,
            "height": 1080,
            "r_frame_rate": "30/1",
            "start_time": "0.000000",
            "duration": "3.000000",
            "bit_rate": "8000000",
        }
    ]
    if audio:
        streams.append(
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "aac",
                "profile": "LC",
                "sample_rate": "48000",
                "channels": 1,
                "r_frame_rate": "0/0",
                "start_time": f"{skew:.6f}",
                "duration": "3.000000",
                "bit_rate": "128000",
            }
        )
    return {"streams": streams, "format": {"duration": "3.0", "size": "3000000"}}


def test_source_preconstructs_exact_generations_and_keeps_topology_fixed() -> None:
    source = HARNESS_PATH.read_text(encoding="utf-8")
    run_body = source.split("def run(self)", 1)[1].split("def _strict_json", 1)[0]

    assert "first = self.create_generation(1, True)" in run_body
    assert "second = self.create_generation(2, False)" in run_body
    assert "third = self.create_generation(3, True)" in run_body
    assert run_body.index("third = self.create_generation(3, True)") < run_body.index(
        "self.start(first)"
    )
    assert run_body.count("create_generation(") == 3
    assert "drop-mode=forward-sticky-events" in source
    assert '"pipeline_state": "pre-data"' not in source
    assert 'pipeline_state="pre-data"' in source
    create = source.split("def create_generation(", 1)[1].split("def _link_external(", 1)[0]
    assert ".link(" not in create
    assert "set_locked_state(True)" in create
    assert "if not self.pipeline.add(" not in create
    assert "generation_bin.get_parent() is not self.pipeline" in create
    start = source.split("def start(", 1)[1].split("def _assert_parent_identity", 1)[0]
    assert start.index("self._link_external(initial)") < start.index("set_locked_state(False)")


def test_splitmux_request_pads_are_only_released_after_whole_parent_null() -> None:
    source = HARNESS_PATH.read_text(encoding="utf-8")
    release = source.split("def release_after_parent_null(", 1)[1].split("def stop(", 1)[0]
    stop = source.split("def stop(", 1)[1].split("def run(", 1)[0]

    assert stop.index("self.pipeline.set_state(self.gst.State.NULL)") < stop.index(
        "self.release_after_parent_null(generation)"
    )
    assert release.index("generation.bin.get_state") < release.index("release_request_pad")
    assert "generation.output.release_request_pad(generation.output_video_pad)" in release
    assert "generation.output.release_request_pad(generation.output_audio_pad)" in release
    assert "if not self.pipeline.remove(" not in release
    assert "generation.bin.get_parent() is not None" in release

    switch = source.split("def switch(", 1)[1].split("def release_after_parent_null(", 1)[0]
    assert "release_request_pad" not in switch
    assert "request_pad_simple" not in switch
    assert "send_event(" in switch


def test_handoff_blocks_encoded_idr_and_never_mutates_live_audio_request_pad() -> None:
    source = HARNESS_PATH.read_text(encoding="utf-8")
    idr = source.split("def _block_handoff_inputs", 1)[1].split("def switch(", 1)[0]
    switch = source.split("def switch(", 1)[1].split("def release_after_parent_null(", 1)[0]

    assert "PadProbeType.BLOCK | self.gst.PadProbeType.BUFFER" in idr
    assert "BLOCK_DOWNSTREAM" not in idr
    assert "BufferFlags.DELTA_UNIT" in idr
    assert "PadProbeReturn.PASS" in idr
    assert idr.index("audio_sink.add_probe(") < idr.index("video_sink.add_probe(")
    assert "target_ready.is_set()" in idr
    assert 'audio_running_ns < held["video_running_time_ns"]' in idr
    assert "segment.to_running_time(self.gst.Format.TIME, pts_ns)" in idr
    assert 'held["video_blocked_monotonic_ns"] = time.monotonic_ns()' in idr
    assert '"blocked_input_alignment_ns": input_alignment_ns' in switch
    assert (
        "probes_removed_monotonic_ns - handoff.video_blocked_monotonic_ns"
        in switch
    )
    assert switch.index(
        "if not 0 <= input_alignment_ns < MAX_INPUT_ALIGNMENT_NS"
    ) < switch.index(
        "self._drain_safety_bus_quiet()"
    )
    assert "self._set_generation_open(old, False)" in switch
    assert "self._set_generation_open(successor, True)" in switch
    assert switch.index("self._set_generation_open(old, False)") < switch.index("send_event(")
    assert switch.index("self._unlink_external(old)") < switch.index("send_event(")
    assert 'get_static_pad("audio_0")' not in switch
    assert "output.audio_0" in source
    eos_probe = source.split("def _add_eos_probe", 1)[1].split(
        "def _add_generation_probe", 1
    )[0]
    assert "def observe_event(" in eos_probe
    assert "def observe_buffer(" in eos_probe
    assert "EVENT_DOWNSTREAM,\n            observe_event," in eos_probe
    assert "PadProbeType.BUFFER, observe_buffer" in eos_probe
    assert "EVENT_DOWNSTREAM | self.gst.PadProbeType.BUFFER" not in eos_probe


def test_probe_bus_and_parent_lifecycle_cleanup_are_fail_closed() -> None:
    source = HARNESS_PATH.read_text(encoding="utf-8")
    input_block = source.split("def _block_handoff_inputs", 1)[1].split(
        "def switch(", 1
    )[0]
    switch = source.split("def switch(", 1)[1].split(
        "def release_after_parent_null(", 1
    )[0]
    stop = source.split("def stop(", 1)[1].split("def run(", 1)[0]
    run = source.split("def run(self)", 1)[1].split("def _strict_json", 1)[0]

    assert "except BaseException:" in input_block
    assert "video_sink.remove_probe(video_probe)" in input_block
    assert "audio_sink.remove_probe(audio_probe)" in input_block
    assert switch.count("finally:") >= 2
    assert switch.index("self._drain_safety_bus_quiet()") < switch.index(
        "active_locations ="
    )
    assert stop.count("self._drain_safety_bus_quiet()") == 2
    assert stop.index("self._drain_safety_bus_quiet()") < stop.index(
        "self.pipeline.set_state(self.gst.State.NULL)"
    )
    assert run.index("try:") < run.index("self.start(first)")
    assert "state != self.gst.State.NULL" in run


def test_media_validation_enforces_generation_stream_sets_and_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _load()
    names = [
        "g01-00.mp4",
        "g01-01.mp4",
        "g01-02.mp4",
        "g02-00.mp4",
        "g02-01.mp4",
        "g02-02.mp4",
        "g03-00.mp4",
        "g03-01.mp4",
        "g03-02.mp4",
    ]
    for name in names:
        (tmp_path / name).write_bytes(b"media")
    monkeypatch.setattr(
        harness,
        "_probe_media",
        lambda path: _probe_document(
            not path.name.startswith("g02"),
            skew=0.099 if path.name == "g03-00.mp4" else 0.02,
        ),
    )
    monkeypatch.setattr(harness, "_first_packet_is_idr", lambda _path: True)
    monkeypatch.setattr(harness, "_decode_media", lambda _path, _audio: None)

    result = harness.validate_media(tmp_path)

    assert result["count"] == 9
    assert result["generation_counts"] == {1: 3, 2: 3, 3: 3}
    assert result["restored_skew_seconds"] == pytest.approx([0.099, 0.02, 0.02])
    assert result["stabilized_skew_spread_seconds"] == pytest.approx(0.0)

    monkeypatch.setattr(harness, "_probe_media", lambda _path: _probe_document(True))
    with pytest.raises(harness.HarnessError, match="stream set"):
        harness.validate_media(tmp_path)


def test_media_validation_rejects_restored_skew_or_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _load()
    names = [
        "g01-00.mp4",
        "g01-01.mp4",
        "g02-00.mp4",
        "g02-01.mp4",
        "g03-00.mp4",
        "g03-01.mp4",
        "g03-02.mp4",
        "g03-03.mp4",
    ]
    for name in names:
        (tmp_path / name).write_bytes(b"media")

    def probe(path: Path) -> dict[str, object]:
        if path.name.startswith("g02"):
            return _probe_document(False)
        skew = 0.101 if path.name == "g03-03.mp4" else 0.01
        return _probe_document(True, skew=skew)

    monkeypatch.setattr(harness, "_probe_media", probe)
    monkeypatch.setattr(harness, "_first_packet_is_idr", lambda _path: True)
    monkeypatch.setattr(harness, "_decode_media", lambda _path, _audio: None)
    with pytest.raises(harness.HarnessError, match="skew"):
        harness.validate_media(tmp_path)


def test_pad_counter_reads_typed_buffer_flags_without_integer_has_flags() -> None:
    harness = _load()

    class Buffer:
        pts = 123

        @staticmethod
        def get_flags() -> int:
            return 1 << 13

        @staticmethod
        def has_flags(_flags: object) -> bool:
            raise AssertionError("raw integer has_flags must not be used")

    counter = harness.PadCounter()
    counter.observe(Buffer())

    assert counter.count == 1
    assert counter.first_delta is True


def test_cli_failure_is_exclusive_bounded_and_never_claims_integration(
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
            "/srv/dashcam/quarantine/m7-generation-20260727a",
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


def test_manifest_is_closed_and_readme_states_capability_scope() -> None:
    harness = _load()
    lines = MANIFEST_PATH.read_text(encoding="ascii").splitlines()
    assert [line.split("  ", 1)[1] for line in lines] == ["README.md", "run.py"]
    assert all(len(line.split("  ", 1)[0]) == 64 for line in lines)
    manifest_hash = hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest()
    entries = harness.verify_manifest(manifest_hash, MANIFEST_PATH.parent)
    assert tuple(entries) == ("README.md", "run.py")
    readme = README_PATH.read_text(encoding="utf-8")
    assert "capability" in readme
    assert "safe_to_integrate_production" in readme
    assert "No live splitmux generation ever gains" in " ".join(readme.split())


def test_fixed_media_commands_use_compact_probe_idr_and_hardware_decode() -> None:
    harness = _load()
    source = HARNESS_PATH.read_text(encoding="utf-8")
    assert "codec_name,profile,width,height" in source
    assert '"-read_intervals",' in source
    assert '"%+#1",' in source
    assert '"h264_v4l2m2m",' in source
    assert '"0:a:0"' in source
    assert "/srv/dashcam/pending" not in source
    assert "/srv/dashcam/clips" not in source
    assert "systemctl start" not in source
    assert "systemctl stop" not in source
    assert "systemctl restart" not in source
    assert harness._contains_h264_idr("00000000: 00000001 65aabbcc") is True
    assert harness._contains_h264_idr("00000000: 00000001 41aabbcc") is False
    multiline = (
        "00000000: 0000 0002 27aa  dead\n"
        "00000004: 0000 0001 65  e\n"
    )
    assert harness._contains_h264_idr(multiline) is True
