from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest

from dashcam.audio.alsa import AlsaCaptureDevice, AlsaIdentity, AlsaSelector
from dashcam.audio.linux import AudioDiscoveryOutcome, AudioDiscoveryStatus

ROOT = Path(__file__).resolve().parents[2]
HARNESS_PATH = ROOT / "deploy/ssh-dev-validation/milestone7-physical-loss-handoff/run.py"
README_PATH = HARNESS_PATH.with_name("README.md")
MANIFEST_PATH = HARNESS_PATH.with_name("SHA256SUMS")


def _load() -> ModuleType:
    name = "pi_m7_physical_loss_handoff_harness"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, HARNESS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _probe_document(audio: bool) -> dict[str, object]:
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
                "start_time": "0.020000",
                "duration": "3.000000",
                "bit_rate": "128000",
            }
        )
    return {
        "streams": streams,
        "format": {"duration": "3.000000", "size": "3000000"},
    }


def _audio_device(card: int = 1, *, path: str = "platform-xhci-usb-0:1.1:1.0") -> AlsaCaptureDevice:
    return AlsaCaptureDevice(
        AlsaIdentity(
            vendor_id="08bb",
            product_id="2902",
            product="USB PnP Sound Device",
            physical_path=path,
            alsa_card_id=str(card),
        ),
        card_index=card,
        pcm_device_index=0,
    )


def _selector(device: AlsaCaptureDevice) -> AlsaSelector:
    identity = device.identity
    return AlsaSelector(
        vendor_id=identity.vendor_id,
        product_id=identity.product_id,
        product=identity.product,
        physical_path=identity.physical_path,
    )


def _prime_discovery_fields(experiment: Any) -> None:
    device = _audio_device()
    experiment.selector = _selector(device)
    experiment.initial_device = device
    experiment.loss_discovery_observations = []
    experiment.confirmed_physical_loss = None
    experiment.discovery_window_open = False
    experiment.eos_dispatch_evidence = []
    experiment.audio_eos_fallback_evidence = []
    experiment.audio_eos_branch_decision_evidence = []
    experiment.natural_eos_final_absence_checks = []
    experiment.video_path_diagnostics = []
    experiment.successor_state_convergence = []
    experiment.terminal_shutdown_phase = "INACTIVE"
    experiment.terminal_shutdown_context = None
    experiment.terminal_parent_eos_observations = []
    experiment._output_audio_eos_lock = threading.Lock()
    experiment._output_audio_eos_observations = []
    experiment._output_audio_eos_arbiter_states = {}
    experiment._retained_audio_idle_probes = []


def _monitor_experiment(harness: ModuleType) -> Any:
    experiment = object.__new__(harness.PhysicalLossExperiment)
    device = _audio_device()
    experiment.selector = _selector(device)
    experiment.initial_device = device
    experiment.loss_wait_armed = True
    experiment.loss_wait_armed_ns = 1
    experiment.loss_discovery_observations = []
    experiment.confirmed_physical_loss = None
    experiment.discovery_window_open = True
    experiment.events = []
    return experiment


def test_source_preconstructs_only_complete_av_and_video_only_generations() -> None:
    source = HARNESS_PATH.read_text(encoding="utf-8")
    run = source.split("def run(self)", 1)[1].split("def _stream_contract", 1)[0]

    assert "first = self.create_generation(1, True)" in run
    assert "second = self.create_generation(2, False)" in run
    assert run.index("second = self.create_generation(2, False)") < run.index("self.start(first)")
    assert run.count("create_generation(") == 2
    assert "create_generation(3" not in run
    assert "SHARED_MANIFEST_SHA256" in source
    assert "module.verify_manifest(SHARED_MANIFEST_SHA256, directory)" in source
    assert "drop-mode=forward-sticky-events" not in source


def test_shared_members_are_verified_before_exact_verified_bytes_execute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load()
    shared = ROOT / "deploy/ssh-dev-validation/milestone7-generation-handoff"
    for name in ("README.md", "run.py"):
        (tmp_path / name).write_bytes((shared / name).read_bytes())
    entries = {
        name: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
        for name in ("README.md", "run.py")
    }
    manifest = "".join(f"{entries[name]}  {name}\n" for name in ("README.md", "run.py"))
    (tmp_path / "SHA256SUMS").write_text(manifest, encoding="ascii", newline="")
    monkeypatch.setattr(
        harness,
        "SHARED_MANIFEST_SHA256",
        hashlib.sha256((tmp_path / "SHA256SUMS").read_bytes()).hexdigest(),
    )

    verified = harness._verified_shared_members(tmp_path)
    assert verified["run.py"] == (tmp_path / "run.py").read_bytes()
    (tmp_path / "run.py").write_bytes(b"raise RuntimeError('must never execute')\n")
    with pytest.raises(RuntimeError, match=r"member run[.]py failed verification"):
        harness._verified_shared_members(tmp_path)

    source = HARNESS_PATH.read_text(encoding="utf-8")
    loader = source.split("def _load_hash_closed_shared_harness", 1)[1].split(
        "_shared = _load_hash_closed_shared_harness()", 1
    )[0]
    assert loader.index("_verified_shared_members(directory)") < loader.index(
        'compile(members["run.py"]'
    )
    assert "exec_module" not in loader


def test_expected_loss_is_exact_registered_audio_source_and_bounded() -> None:
    source = HARNESS_PATH.read_text(encoding="utf-8")
    bus = source.split("def _drain_bus_once(", 1)[1].split("def _assert_parent_identity", 1)[0]
    run = source.split("def run(self)", 1)[1].split("def _stream_contract", 1)[0]

    assert "message.src is self.audio_source" in bus
    assert 'self.pipeline.get_by_name("audio_source") is self.audio_source' in bus
    assert "message.src.get_path_string() == self.registered_audio_source_path" in bus
    assert "not self.loss_wait_armed" in bus
    assert "MAX_AUDIO_LOSS_ERRORS" in bus
    assert "MAX_LOSS_LATENCY_REFUSALS" in bus
    assert "delta_from_loss_window_armed_ns" in bus
    assert "accepted_loss_burst" in bus
    assert "error_domain" in bus
    assert "error_code" in bus
    assert '"debug"' in bus
    assert "unexpected GStreamer error" in bus
    assert "self._wait_for_confirmed_physical_loss(first)" in run
    assert "MIN_LOSS_TIMEOUT_SECONDS <= loss_timeout_seconds <= MAX_LOSS_TIMEOUT_SECONDS" in source
    assert "MAX_PRELOSS_FRAGMENTS" in source
    assert "MAX_MEDIA_COUNT: Final = 40" in source
    assert "LOSS_DISCOVERY_POLL_SECONDS: Final = 0.5" in source
    assert "STABLE_NOT_FOUND_SEPARATION_NS: Final = 500_000_000" in source
    assert "diagnostic media count left its 5-40 bound" in source
    assert "5-10 bound" not in source
    assert "OWNER_ACTION_REQUIRED" in source
    execute = source.split("def execute(", 1)[1].split("def _parser", 1)[0]
    assert execute.count("discover_capture_device(selector)") == 2
    assert "AudioDiscoveryStatus.NOT_FOUND" in execute
    assert '"attempts": 1' in execute
    assert '"post_loss_microphone_discovery": post_loss_evidence' in execute


def test_exact_source_loss_burst_is_ordered_bounded_and_late_error_refuses() -> None:
    harness = _load()
    experiment = object.__new__(harness.PhysicalLossExperiment)

    class MessageType:
        ERROR = 1
        WARNING = 2
        EOS = 4
        ELEMENT = 8
        LATENCY = 16
        NEW_CLOCK = 32
        CLOCK_LOST = 64
        QOS = 128

    message_type_class = MessageType

    class Gst:
        MessageType = message_type_class

    class Error:
        domain = 77
        code = 1
        message = "Internal data stream error."

        def __str__(self) -> str:
            return "gst-stream-error-quark: Internal data stream error. (1)"

    class Source:
        def get_name(self) -> str:
            return "audio_source"

        def get_path_string(self) -> str:
            return "/GstPipeline:pipeline0/GstAlsaSrc:audio_source"

    source = Source()

    class Pipeline:
        def get_by_name(self, name: str) -> object | None:
            return source if name == "audio_source" else None

    class Message:
        type = MessageType.ERROR
        src = source

        def parse_error(self) -> tuple[Error, str]:
            return Error(), "../libs/gst/base/gstbasesrc.c: gst_base_src_loop"

    class Bus:
        def __init__(self) -> None:
            self.messages = [Message(), Message(), Message()]

        def timed_pop_filtered(self, _timeout: int, _types: int) -> Message | None:
            return self.messages.pop(0) if self.messages else None

    experiment.gst = Gst()
    experiment.pipeline = Pipeline()
    experiment.bus = Bus()
    experiment.audio_source = source
    experiment.registered_audio_source_path = source.get_path_string()
    experiment.loss_wait_armed = True
    experiment.loss_wait_armed_ns = time.monotonic_ns()
    experiment.expected_audio_error = None
    experiment.audio_loss_errors = []
    experiment.unexpected_bus_errors = []
    experiment.loss_latency_refusals = []
    experiment.audio_loss_burst_first_ns = None
    experiment.audio_loss_burst_closed = False
    experiment.errors = []
    experiment.warnings = []
    experiment.events = []
    experiment.clock = None
    experiment.initial_new_clock_seen = False
    experiment.transitions = [{}]
    _prime_discovery_fields(experiment)

    assert experiment._drain_bus_once() is True
    assert experiment._drain_bus_once() is True
    experiment._close_audio_corroboration_window()

    burst = experiment._audio_loss_evidence()
    assert harness._loss_burst_contract(burst) is True
    assert burst["accepted_count"] == 2
    assert [message["sequence"] for message in burst["messages"]] == [1, 2]
    assert all(message["error_domain"] == 77 for message in burst["messages"])
    assert all(message["error_code"] == 1 for message in burst["messages"])
    assert all("gst_base_src_loop" in message["debug"] for message in burst["messages"])

    with pytest.raises(harness.HarnessError, match="unexpected GStreamer error"):
        experiment._drain_bus_once()
    diagnostic = experiment._failure_diagnostic()
    assert diagnostic["audio_loss_error_burst"]["accepted_count"] == 2
    assert diagnostic["unexpected_bus_errors"][0]["rejection"] == "burst_closed"


def test_zero_gstreamer_errors_is_valid_optional_corroboration() -> None:
    harness = _load()
    experiment = object.__new__(harness.PhysicalLossExperiment)
    experiment.audio_loss_errors = []
    experiment.audio_loss_burst_closed = True

    evidence = experiment._audio_loss_evidence()

    assert evidence["corroborated"] is False
    assert evidence["accepted_count"] == 0
    assert harness._loss_burst_contract(evidence) is True


def test_two_separated_consecutive_not_found_samples_trigger_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load()
    experiment = _monitor_experiment(harness)
    outcomes = iter(
        (
            AudioDiscoveryOutcome(AudioDiscoveryStatus.MATCHED, experiment.initial_device),
            AudioDiscoveryOutcome(AudioDiscoveryStatus.NOT_FOUND),
            AudioDiscoveryOutcome(AudioDiscoveryStatus.NOT_FOUND),
        )
    )
    monkeypatch.setattr(harness, "discover_capture_device", lambda _selector: next(outcomes))
    tick = iter(
        (
            10,
            11,
            1_000_000_010,
            1_000_000_011,
            1_500_000_010,
            1_500_000_011,
            1_500_000_012,
            1_500_000_013,
        )
    )
    monkeypatch.setattr(harness.time, "monotonic_ns", lambda: next(tick))

    experiment._observe_loss_identity(require_initial_match=True)
    experiment._observe_loss_identity()
    assert experiment.confirmed_physical_loss is None
    experiment._observe_loss_identity()
    confirmed = experiment.confirmed_physical_loss
    assert confirmed is not None
    assert confirmed["trigger"] == "stable_identity_not_found"
    assert confirmed["separation_ns"] == 500_000_000
    experiment._close_discovery_trigger_window()
    assert harness._physical_loss_discovery_contract(experiment._loss_discovery_evidence())


def test_single_not_found_or_intervening_exact_match_does_not_trigger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load()
    experiment = _monitor_experiment(harness)
    outcomes = iter(
        (
            AudioDiscoveryOutcome(AudioDiscoveryStatus.MATCHED, experiment.initial_device),
            AudioDiscoveryOutcome(AudioDiscoveryStatus.NOT_FOUND),
            AudioDiscoveryOutcome(AudioDiscoveryStatus.MATCHED, experiment.initial_device),
            AudioDiscoveryOutcome(AudioDiscoveryStatus.NOT_FOUND),
        )
    )
    monkeypatch.setattr(harness, "discover_capture_device", lambda _selector: next(outcomes))
    current = 0

    def clock() -> int:
        nonlocal current
        current += 500_000_000
        return current

    monkeypatch.setattr(harness.time, "monotonic_ns", clock)
    experiment._observe_loss_identity(require_initial_match=True)
    experiment._observe_loss_identity()
    experiment._observe_loss_identity()
    experiment._observe_loss_identity()

    assert experiment.confirmed_physical_loss is None
    assert [item["status"] for item in experiment.loss_discovery_observations] == [
        "MATCHED",
        "NOT_FOUND",
        "MATCHED",
        "NOT_FOUND",
    ]


@pytest.mark.parametrize("mode", ["changed", "ambiguous", "refused"])
def test_changed_ambiguous_or_refused_discovery_fails_closed(
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load()
    experiment = _monitor_experiment(harness)
    if mode == "changed":
        second = AudioDiscoveryOutcome(
            AudioDiscoveryStatus.MATCHED,
            _audio_device(card=2, path="platform-xhci-usb-0:1.2:1.0"),
        )
        reason = "changed identity or endpoint"
    elif mode == "ambiguous":
        second = AudioDiscoveryOutcome(AudioDiscoveryStatus.AMBIGUOUS)
        reason = "became AMBIGUOUS"
    else:
        second = AudioDiscoveryOutcome(AudioDiscoveryStatus.REFUSED)
        reason = "became REFUSED"
    outcomes = iter(
        (
            AudioDiscoveryOutcome(AudioDiscoveryStatus.MATCHED, experiment.initial_device),
            second,
        )
    )
    monkeypatch.setattr(harness, "discover_capture_device", lambda _selector: next(outcomes))
    current = 0

    def clock() -> int:
        nonlocal current
        current += 500_000_000
        return current

    monkeypatch.setattr(harness.time, "monotonic_ns", clock)
    experiment._observe_loss_identity(require_initial_match=True)
    with pytest.raises(harness.HarnessError, match=reason):
        experiment._observe_loss_identity()
    assert experiment.loss_discovery_observations[-1]["status"] == second.status.value


def test_loss_window_latency_refusals_are_bounded_evidenced_and_not_a_trigger() -> None:
    harness = _load()
    experiment = object.__new__(harness.PhysicalLossExperiment)

    class MessageType:
        ERROR = 1
        WARNING = 2
        EOS = 4
        ELEMENT = 8
        LATENCY = 16
        NEW_CLOCK = 32
        CLOCK_LOST = 64
        QOS = 128

    message_type_class = MessageType

    class Gst:
        MessageType = message_type_class

    class Source:
        def get_name(self) -> str:
            return "sink_9"

        def get_path_string(self) -> str:
            return "/GstPipeline:pipeline0/GstFileSink:sink_9"

    source = Source()

    class Pipeline:
        @staticmethod
        def recalculate_latency() -> bool:
            return False

    class Message:
        type = MessageType.LATENCY
        src = source

    class Bus:
        def __init__(self) -> None:
            self.messages = [Message() for _ in range(5)]

        def timed_pop_filtered(self, _timeout: int, _types: int) -> Message | None:
            return self.messages.pop(0) if self.messages else None

    experiment.gst = Gst()
    experiment.pipeline = Pipeline()
    experiment.bus = Bus()
    experiment.loss_wait_armed = True
    experiment.loss_wait_armed_ns = time.monotonic_ns() - 1_000
    experiment.audio_loss_burst_closed = False
    experiment.loss_latency_refusals = []
    experiment.expected_audio_error = None
    experiment.audio_loss_errors = []
    experiment.unexpected_bus_errors = []
    experiment.registered_audio_source_path = "/pipeline/audio_source"
    experiment.errors = []
    experiment.warnings = []
    experiment.events = []
    _prime_discovery_fields(experiment)

    for _index in range(4):
        assert experiment._drain_bus_once() is True
    evidence = experiment._latency_refusal_evidence()
    assert harness._loss_latency_refusal_contract(evidence) is True
    assert experiment.expected_audio_error is None
    assert [message["sequence"] for message in evidence["messages"]] == [1, 2, 3, 4]
    assert all(message["source"] == "sink_9" for message in evidence["messages"])

    with pytest.raises(harness.HarnessError, match="latency recalculation failed"):
        experiment._drain_bus_once()
    diagnostic = experiment._failure_diagnostic()
    refusals = diagnostic["latency_recalculation_refusals"]
    assert refusals["count"] == 5
    assert refusals["messages"][-1]["accepted_loss_window"] is False
    assert refusals["messages"][-1]["rejection"] == "refusal_count_exceeded"


def test_loss_switch_blocks_only_next_video_idr_and_never_waits_for_audio() -> None:
    source = HARNESS_PATH.read_text(encoding="utf-8")
    block = source.split("def _block_next_video_idr(", 1)[1].split(
        "def _verify_final_post_eos_absence_before_topology", 1
    )[0]
    switch = source.split("def switch_after_physical_loss(", 1)[1].split("def stop_video_only", 1)[
        0
    ]

    assert "PadProbeType.BLOCK | self.gst.PadProbeType.BUFFER" in block
    assert "BufferFlags.DELTA_UNIT" in block
    assert "PadProbeReturn.PASS" in block
    assert "audio" not in block.lower()
    assert "_block_handoff_inputs" not in switch
    assert "self._block_next_video_idr()" in switch
    assert "release.wait(VIDEO_IDR_RELEASE_TIMEOUT_SECONDS)" in block
    assert "PadProbeReturn.REMOVE" in block
    assert "sink.is_blocked()" in block
    assert "sink.is_blocking()" in block
    assert '"no_post_loss_audio_buffer_wait": True' in switch
    assert "self._set_generation_open(old, False)" in switch
    assert "self._unlink_external(old)" in switch
    assert "self._set_generation_open(successor, True)" in switch
    assert "send_event(" not in switch
    assert switch.count("_start_downstream_eos(") == 2
    assert "_await_eos_dispatches" in switch
    assert 'allow_refused_labels=("loss-retired-audio",)' in switch
    assert "self._resolve_retired_audio_eos(" in switch
    assert "old.video_queue.get_static_pad" in switch
    assert "old.audio_queue.get_static_pad" in switch
    assert "audio_source.set_state" not in switch
    assert "self._close_discovery_trigger_window()" in switch
    assert '"trigger": "stable_identity_not_found"' in switch
    assert "finally:" in switch
    assert "self._release_blocked_video_idr(handoff)" in switch
    assert switch.index("self._converge_successor_state_after_preroll(") < switch.index(
        "successor.video_counter.count >= first_successor_count + 30"
    )
    assert "successor.video_counter.count >= first_successor_count + 30" in switch
    assert '"successor_video_stall"' in switch
    assert '"post_loss_fragment_stall"' in switch
    stop = source.split("def stop_video_only(", 1)[1].split("def _bounded_failure_cleanup", 1)[0]
    assert stop.index("self._transition_parent_null_bounded()") < stop.index(
        "self._close_audio_corroboration_window()"
    )


def test_no_live_request_pad_mutation_and_release_only_after_parent_null() -> None:
    source = HARNESS_PATH.read_text(encoding="utf-8")
    switch = source.split("def switch_after_physical_loss(", 1)[1].split("def stop_video_only", 1)[
        0
    ]
    stop = source.split("def stop_video_only(", 1)[1].split("def _bounded_failure_cleanup", 1)[0]
    cleanup = source.split("def _bounded_failure_cleanup(", 1)[1].split("def run(self)", 1)[0]

    assert "request_pad_simple" not in switch
    assert "release_request_pad" not in switch
    assert "old.output_audio_pad" in switch
    assert "self._serialize_audio_eos_branch(" in switch
    assert switch.index("self._verify_final_post_eos_absence_before_topology(old)") < switch.index(
        "self._serialize_audio_eos_branch("
    )
    assert switch.index("self._serialize_audio_eos_branch(") < switch.index(
        "successor.bin.set_locked_state(False)"
    )
    assert switch.index("self._serialize_audio_eos_branch(") < switch.index(
        "self._link_external(successor)"
    )
    assert switch.index("self._serialize_audio_eos_branch(") < switch.index(
        "self._set_generation_open(old, False)"
    )
    assert switch.index("eos_deadline =") < switch.index("self._serialize_audio_eos_branch(")
    assert switch.index("eos_deadline =") < switch.index("self._start_downstream_eos(")
    assert "loss-retired-audio-serialization-barrier" not in switch
    assert stop.index("self._transition_parent_null_bounded()") < stop.index(
        "self.release_after_parent_null(generation)"
    )
    assert stop.index("self._transition_parent_null_bounded()") < stop.index(
        "self._release_audio_idle_blocks_after_parent_null()"
    )
    assert cleanup.index("self._transition_parent_null_bounded()") < cleanup.index(
        "self.release_after_parent_null(generation)"
    )
    assert cleanup.index("self._transition_parent_null_bounded()") < cleanup.index(
        "self._release_audio_idle_blocks_after_parent_null()"
    )
    assert cleanup.index("self._release_audio_idle_blocks_before_failure_null()") < cleanup.index(
        "self._transition_parent_null_bounded()"
    )
    assert stop.index("self._release_blocked_video_idr(handoff)") < stop.index(
        "self._transition_parent_null_bounded()"
    )
    assert "release_request_pad" not in stop.split("self._transition_parent_null_bounded()", 1)[0]
    convergence = source.split("def _converge_successor_state_after_preroll(", 1)[1].split(
        "def _video_pad_state", 1
    )[0]
    assert convergence.count("successor.bin.set_state(self.gst.State.PLAYING)") == 1
    assert "sync_state_with_parent" not in convergence


def test_video_path_diagnostic_contract_requires_proven_probe_release_and_progress() -> None:
    harness = _load()
    stages = (
        "handoff_before_idr_release",
        "handoff_idr_released",
        "successor_first_video_buffer",
        "successor_continuous_video_proven",
        "post_loss_fragment_wait_started",
        "post_loss_fragment_wait_completed",
    )
    counts = (0, 0, 1, 31, 31, 100)
    diagnostics: list[dict[str, object]] = []
    for sequence, (stage, count) in enumerate(zip(stages, counts, strict=True), start=1):
        item: dict[str, object] = {
            "sequence": sequence,
            "stage": stage,
            "observed_monotonic_ns": sequence,
            "camera_raw": {"count": count + 100},
            "parent_encoded": {"count": count + 50},
            "successor_generation": {
                "video": {"count": count},
                "external_linked": True,
                "video_valve_drop": False,
                "tee_peer_is_exact_ghost": True,
                "opened_locations": ["g02-00.mp4"] if sequence >= 3 else [],
                "closed_locations": (
                    ["g02-00.mp4", "g02-01.mp4", "g02-02.mp4"] if sequence == 6 else []
                ),
            },
            "video_tee_sink": {
                "blocked": sequence == 1,
                "blocking": sequence == 1,
            },
            "successor_video_tee_pad": {"linked": True},
            "pipeline_state": {"current": 4},
            "retained_audio_idle_probe_count": 0,
            "files": [],
        }
        if sequence <= 2:
            item["idr_release"] = {
                "release_requested": sequence == 2,
                "callback_returning": sequence == 2,
                "release_requested_monotonic_ns": 10 if sequence == 2 else None,
                "callback_returning_monotonic_ns": 11 if sequence == 2 else None,
            }
        diagnostics.append(item)

    assert harness._video_path_diagnostic_contract(diagnostics) is True
    cast(dict[str, object], diagnostics[1]["video_tee_sink"])["blocking"] = True
    assert harness._video_path_diagnostic_contract(diagnostics) is False


def _successor_state_fixture(
    harness: ModuleType,
    *,
    successor_state: int,
    successor_pending: int = 0,
    successor_change_return: int = 1,
    parent_state: int = 4,
    parent_pending: int = 0,
    parent_change_return: int = 1,
    child_state: int | None = None,
    set_return: int = 1,
    converge_on_set: bool = True,
    queue_buffers: int = 0,
) -> tuple[Any, Any, Any]:
    class State:
        VOID_PENDING = 0
        PAUSED = 3
        PLAYING = 4

    class StateChangeReturn:
        FAILURE = 0
        SUCCESS = 1
        ASYNC = 2

    class IteratorResult:
        OK = 1
        DONE = 2
        RESYNC = 3

    gst = SimpleNamespace(
        SECOND=1_000_000_000,
        State=State,
        StateChangeReturn=StateChangeReturn,
        IteratorResult=IteratorResult,
    )

    class Factory:
        def __init__(self, name: str) -> None:
            self.name = name

        def get_name(self) -> str:
            return self.name

    class Counter:
        def __init__(self, count: int) -> None:
            self.count = count

        def snapshot(self) -> dict[str, int]:
            return {"count": self.count}

    class Element:
        def __init__(
            self,
            name: str,
            current: int,
            pending: int = 0,
            change_return: int = 1,
            factory: str | None = None,
        ) -> None:
            self.name = name
            self.current = current
            self.pending = pending
            self.change_return = change_return
            self.factory = factory
            self.children: list[Element] = []
            self.set_calls = 0
            self.on_set: Any | None = None

        def get_state(self, _timeout_ns: int) -> tuple[int, int, int]:
            return self.change_return, self.current, self.pending

        def set_state(self, requested: int) -> int:
            self.set_calls += 1
            if self.on_set is not None:
                self.on_set(requested)
            return set_return

        def is_locked_state(self) -> bool:
            return False

        def get_factory(self) -> Factory | None:
            return Factory(self.factory) if self.factory is not None else None

        def get_path_string(self) -> str:
            return f"/pipeline/{self.name}"

        def iterate_recurse(self) -> Any:
            children = list(self.children)

            class Iterator:
                index = 0

                def next(self) -> tuple[int, Element | None]:
                    if self.index >= len(children):
                        return IteratorResult.DONE, None
                    child = children[self.index]
                    self.index += 1
                    return IteratorResult.OK, child

            return Iterator()

        def get_property(self, name: str) -> int | bool:
            values: dict[str, int | bool] = {
                "drop": False,
                "current-level-buffers": queue_buffers,
                "current-level-bytes": 0,
                "current-level-time": 0,
                "max-size-buffers": 60,
                "max-size-bytes": 4_000_000,
                "max-size-time": 2_000_000_000,
            }
            return values[name]

    parent = Element(
        "parent",
        parent_state,
        parent_pending,
        parent_change_return,
    )
    bin_element = Element(
        "successor",
        successor_state,
        successor_pending,
        successor_change_return,
    )
    output = Element(
        "splitmux",
        successor_state,
        successor_pending,
        successor_change_return,
    )
    valve = Element(
        "valve",
        successor_state,
        successor_pending,
        successor_change_return,
    )
    queue = Element(
        "queue",
        successor_state,
        successor_pending,
        successor_change_return,
    )
    effective_child_state = successor_state if child_state is None else child_state
    muxer = Element(
        "muxer",
        effective_child_state,
        successor_pending,
        successor_change_return,
        "mp4mux",
    )
    sink = Element(
        "sink",
        effective_child_state,
        successor_pending,
        successor_change_return,
        "filesink",
    )
    output.children = [muxer, sink]
    state_elements = [bin_element, output, valve, queue, muxer, sink]

    def on_set(requested: int) -> None:
        if converge_on_set:
            for element in state_elements:
                element.current = requested
                element.pending = State.VOID_PENDING
                element.change_return = StateChangeReturn.SUCCESS

    bin_element.on_set = on_set
    ghost = object()
    camera_counter = Counter(10)
    parent_counter = Counter(10)
    successor_counter = Counter(1)
    successor = SimpleNamespace(
        bin=bin_element,
        output=output,
        video_valve=valve,
        video_queue=queue,
        external_linked=True,
        video_ghost=ghost,
        video_tee_pad=SimpleNamespace(get_peer=lambda: ghost),
        video_counter=successor_counter,
    )
    experiment = object.__new__(harness.PhysicalLossExperiment)
    experiment.gst = gst
    experiment.pipeline = parent
    experiment.successor_state_convergence = []
    experiment.camera_source_counter = camera_counter
    experiment.video_source_counter = parent_counter
    experiment.confirmed_physical_loss = None
    experiment.discovery_window_open = False
    experiment.loss_discovery_observations = []
    experiment.audio_loss_errors = []
    experiment.unexpected_bus_errors = []
    experiment.warnings = []
    experiment.errors = []
    experiment.clock = object()
    experiment.base_time_ns = 1
    experiment.initial_new_clock_seen = True
    experiment.registered_audio_source_path = "/pipeline/audio_source"
    experiment._assert_parent_identity = lambda: None

    def advance_video(_timeout_ns: int = 0) -> bool:
        camera_counter.count += 1
        parent_counter.count += 1
        successor_counter.count += 1
        return False

    experiment._drain_bus_once = advance_video
    return experiment, successor, bin_element


def _prime_exact_degraded_parent_loss(experiment: Any) -> None:
    experiment.confirmed_physical_loss = {
        "trigger": "stable_identity_not_found",
        "first_not_found_sequence": 2,
        "second_not_found_sequence": 3,
        "first_not_found_monotonic_ns": 100,
        "second_not_found_monotonic_ns": 500_000_100,
        "separation_ns": 500_000_000,
    }
    experiment.loss_discovery_observations = [
        {
            "sequence": 2,
            "observed_monotonic_ns": 100,
            "status": "NOT_FOUND",
            "device_exposed": False,
        },
        {
            "sequence": 3,
            "observed_monotonic_ns": 500_000_100,
            "status": "NOT_FOUND",
            "device_exposed": False,
        },
    ]
    experiment.audio_loss_errors = [
        {
            "accepted_loss_burst": True,
            "exact_registered_audio_source": True,
            "source_path": "/pipeline/audio_source",
            "observed_monotonic_ns": 50,
            "error_domain": "gst-resource-error-quark",
            "error_code": 9,
            "error_message": (
                "Error recording from audio device. The device has been disconnected."
            ),
            "debug": "gst_alsasrc_read exact disconnect",
        }
    ]


def test_successor_state_gate_accepts_already_playing_without_correction() -> None:
    harness = _load()
    experiment, successor, bin_element = _successor_state_fixture(
        harness,
        successor_state=4,
    )

    record = experiment._converge_successor_state_after_preroll(
        successor,
        initial_sync={
            "count": 1,
            "return": True,
            "started_monotonic_ns": 1,
            "ended_monotonic_ns": 2,
            "duration_ns": 1,
        },
    )

    assert record["converged"] is True
    assert record["correction_required"] is False
    assert record["correction_count"] == 0
    assert bin_element.set_calls == 0
    assert harness._successor_state_convergence_contract([record]) is True


def test_successor_state_gate_recovers_exact_paused_void_once() -> None:
    harness = _load()
    experiment, successor, bin_element = _successor_state_fixture(
        harness,
        successor_state=3,
        set_return=2,
    )

    record = experiment._converge_successor_state_after_preroll(
        successor,
        initial_sync={
            "count": 1,
            "return": True,
            "started_monotonic_ns": 1,
            "ended_monotonic_ns": 2,
            "duration_ns": 1,
        },
    )

    assert record["converged"] is True
    assert record["correction_required"] is True
    assert record["correction_count"] == 1
    assert record["set_playing_return"] == 2
    assert bin_element.set_calls == 1
    assert harness._successor_state_convergence_contract([record]) is True


def test_successor_state_gate_allows_exact_known_degraded_parent_query() -> None:
    harness = _load()
    experiment, successor, bin_element = _successor_state_fixture(
        harness,
        successor_state=3,
        parent_change_return=0,
        set_return=2,
    )
    _prime_exact_degraded_parent_loss(experiment)

    record = experiment._converge_successor_state_after_preroll(
        successor,
        initial_sync={
            "count": 1,
            "return": True,
            "started_monotonic_ns": 1,
            "ended_monotonic_ns": 2,
            "duration_ns": 1,
        },
    )

    assert record["parent_state_query_known_degraded_after_audio_source_failure"] is True
    assert record["correction_count"] == 1
    assert cast(dict[str, object], record["video_progress"])["verified"] is True
    assert bin_element.set_calls == 1
    assert harness._successor_state_convergence_contract([record]) is True


@pytest.mark.parametrize(
    "parent_options",
    [
        {"parent_state": 3, "parent_change_return": 0},
        {"parent_state": 4, "parent_pending": 3, "parent_change_return": 0},
    ],
)
def test_known_degraded_parent_query_requires_playing_void(
    parent_options: dict[str, int],
) -> None:
    harness = _load()
    experiment, successor, _bin_element = _successor_state_fixture(
        harness,
        successor_state=3,
        **cast(Any, parent_options),
    )
    _prime_exact_degraded_parent_loss(experiment)

    with pytest.raises(harness.HarnessError, match="topology/parent state"):
        experiment._converge_successor_state_after_preroll(
            successor,
            initial_sync={"count": 1, "return": True},
        )


def test_known_degraded_parent_query_requires_exact_confirmed_loss() -> None:
    harness = _load()
    experiment, successor, _bin_element = _successor_state_fixture(
        harness,
        successor_state=3,
        parent_change_return=0,
    )

    with pytest.raises(harness.HarnessError, match="topology/parent state"):
        experiment._converge_successor_state_after_preroll(
            successor,
            initial_sync={"count": 1, "return": True},
        )


def test_known_degraded_parent_query_requires_continued_video_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load()
    monkeypatch.setattr(harness, "SUCCESSOR_STATE_CONVERGENCE_TIMEOUT_SECONDS", 0.01)
    experiment, successor, _bin_element = _successor_state_fixture(
        harness,
        successor_state=3,
        parent_change_return=0,
    )
    _prime_exact_degraded_parent_loss(experiment)
    experiment._drain_bus_once = lambda _timeout_ns=0: False

    with pytest.raises(harness.HarnessError, match="video progress proof timed out"):
        experiment._converge_successor_state_after_preroll(
            successor,
            initial_sync={"count": 1, "return": True},
        )


def test_successor_state_gate_allows_bounded_async_grace_without_correction() -> None:
    harness = _load()
    experiment, successor, bin_element = _successor_state_fixture(
        harness,
        successor_state=3,
        successor_pending=4,
        successor_change_return=2,
    )

    def finish_natural_transition(_timeout_ns: int = 0) -> bool:
        elements = [
            successor.bin,
            successor.output,
            successor.video_valve,
            successor.video_queue,
            *successor.output.children,
        ]
        for element in elements:
            element.current = 4
            element.pending = 0
            element.change_return = 1
        experiment.camera_source_counter.count += 1
        experiment.video_source_counter.count += 1
        successor.video_counter.count += 1
        return False

    experiment._drain_bus_once = finish_natural_transition
    record = experiment._converge_successor_state_after_preroll(
        successor,
        initial_sync={
            "count": 1,
            "return": True,
            "started_monotonic_ns": 1,
            "ended_monotonic_ns": 2,
            "duration_ns": 1,
        },
    )

    assert record["grace_attempted"] is True
    assert record["correction_required"] is False
    assert bin_element.set_calls == 0
    assert harness._successor_state_convergence_contract([record]) is True


@pytest.mark.parametrize(
    ("fixture_options", "message"),
    [
        ({"successor_state": 3, "successor_pending": 2}, "not PLAYING or exact"),
        ({"successor_state": 4, "child_state": 3}, "not PLAYING or exact"),
        ({"successor_state": 3, "set_return": 0}, "correction was refused"),
        (
            {
                "successor_state": 3,
                "set_return": 2,
                "converge_on_set": False,
            },
            "convergence to PLAYING timed out",
        ),
        ({"successor_state": 3, "queue_buffers": 60}, "queue reached"),
        ({"successor_state": 3, "parent_state": 3}, "topology/parent state"),
    ],
)
def test_successor_state_gate_refuses_wrong_or_stalled_state(
    fixture_options: dict[str, object],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load()
    monkeypatch.setattr(harness, "SUCCESSOR_STATE_CONVERGENCE_TIMEOUT_SECONDS", 0.01)
    experiment, successor, bin_element = _successor_state_fixture(
        harness,
        **cast(Any, fixture_options),
    )

    with pytest.raises(harness.HarnessError, match=message):
        experiment._converge_successor_state_after_preroll(
            successor,
            initial_sync={"count": 1, "return": True},
        )
    assert bin_element.set_calls <= 1


def test_video_only_shutdown_does_not_require_dead_audio_source_eos() -> None:
    source = HARNESS_PATH.read_text(encoding="utf-8")
    stop = source.split("def stop_video_only(", 1)[1].split("def _bounded_failure_cleanup", 1)[0]

    assert 'final.video_queue.get_static_pad("sink")' in stop
    assert "self.pipeline.send_event" not in stop
    assert "audio_queue" not in stop
    assert "final.audio_eos_seen" not in stop
    assert "parent_eos_required=False" in stop
    assert "self._drain_safety_bus_quiet()" in stop
    assert "self._unlink_external(final)" not in stop
    assert "_start_downstream_eos" in stop
    assert "_transition_parent_null_bounded" in stop
    assert stop.index("self._drain_safety_bus_quiet()") < stop.index(
        "self._block_next_video_idr()"
    )
    assert stop.index("common_tee_sink.add_probe(") < stop.index(
        "self._block_next_video_idr()"
    )
    assert "deque(" in stop
    assert "maxlen=MAX_FINAL_COMMON_TEE_RING_BUFFERS" in stop
    assert "common-tee buffer evidence exceeded its bound" not in stop
    assert 'final.video_valve.get_static_pad("sink")' in stop
    assert 'self._element("video_counter").get_static_pad("src")' in stop
    assert "observe_parent_post_block" in stop
    assert "terminal_drop_counter.observe(buffer)" in stop
    assert "_final_shutdown_tail_contract(self.final_shutdown_tail_evidence)" in stop
    assert "MAX_FINAL_UNROUTED_VIDEO_FRAMES" not in source


def _valid_final_shutdown_tail(harness: ModuleType) -> dict[str, object]:
    period = harness.FRAME_PERIOD_NS
    blocked = 1_000_000_000
    fragment_closed = blocked + period
    release = blocked + 6 * period
    unblocked = release + 2_000_000
    null_started = unblocked + 1_000_000
    parent_observations = (
        release + 100_000,
        null_started + 2_000_000,
    )
    closed_observations = (
        null_started + 1_000_000,
        null_started + 2_000_000,
    )
    first_drop = closed_observations[0]
    last_drop = closed_observations[-1]
    null_ended = last_drop + 2_000_000
    media_window = last_drop - release
    wall_budget = 1 + (media_window + period - 1) // period
    held_pts = 12_000_000_000
    parent_pts = [held_pts + offset * period for offset in range(1, 3)]
    media_pts_span = parent_pts[-1] - held_pts
    media_budget = 1 + (media_pts_span + period - 1) // period
    parent_records = [
        {
            "pts_ns": pts_ns,
            "observed_monotonic_ns": observed_ns,
            "delta_unit": True,
        }
        for pts_ns, observed_ns in zip(parent_pts, parent_observations, strict=True)
    ]
    closed_records = [
        {
            "pts_ns": pts_ns,
            "observed_monotonic_ns": observed_ns,
            "delta_unit": index != 0,
        }
        for index, (pts_ns, observed_ns) in enumerate(
            zip([held_pts, parent_pts[0]], closed_observations, strict=True)
        )
    ]
    held_sequence = 28
    common_tee_records = [
        {
            **record,
            "sequence": held_sequence + index,
            "observed_monotonic_ns": (
                blocked - 1_000
                if index == 0
                else cast(int, parent_records[index - 1]["observed_monotonic_ns"])
                + 100_000
            ),
        }
        for index, record in enumerate(closed_records)
    ]
    frozen_media = {
        "name": "g02-03.mp4",
        "device": 42,
        "inode": 84,
        "size_bytes": 1_000_000,
        "sha256": "a" * 64,
    }
    return {
        "frame_period_ns": period,
        "maximum_media_periods": harness.MAX_FINAL_SHUTDOWN_MEDIA_PERIODS,
        "maximum_media_window_ns": harness.MAX_FINAL_SHUTDOWN_MEDIA_PERIODS * period,
        "blocked_monotonic_ns": blocked,
        "held_idr_pts_ns": held_pts,
        "release_requested_monotonic_ns": release,
        "pad_unblocked_monotonic_ns": unblocked,
        "null_started_monotonic_ns": null_started,
        "null_ended_monotonic_ns": null_ended,
        "null_request_after_unblock_ns": null_started - unblocked,
        "first_closed_valve_buffer_monotonic_ns": first_drop,
        "last_closed_valve_buffer_monotonic_ns": last_drop,
        "media_window_ns": media_window,
        "media_window_frame_budget": wall_budget,
        "media_pts_span_ns": media_pts_span,
        "media_pts_frame_budget": media_budget,
        "held_idr_closure_wait_ns": release - blocked,
        "shutdown_control_window_ns": null_started - release,
        "fragment_closed_phase_monotonic_ns": fragment_closed,
        "closed_valve_counter": {
            "count": 2,
            "first_pts_ns": held_pts,
            "last_pts_ns": held_pts + period,
            "non_monotonic": 0,
            "large_gaps": 0,
            "first_delta": False,
        },
        "closed_valve_buffers": closed_records,
        "common_tee_buffers": common_tee_records,
        "common_tee_baseline": {
            "ring_capacity": harness.MAX_FINAL_COMMON_TEE_RING_BUFFERS,
            "retained_count_at_block": held_sequence,
            "evicted_count_at_block": 0,
            "total_count_at_block": held_sequence,
            "held_ring_index": held_sequence - 1,
            "held_sequence": held_sequence,
            "held_is_exact_last_record": True,
            "total_count_at_release": held_sequence,
            "final_total_count": held_sequence + 1,
            "terminal_suffix_count": 2,
            "terminal_suffix_retained": True,
        },
        "parent_post_block_buffers": parent_records,
        "post_null_parent_only_buffers": [parent_records[-1]],
        "terminal_counter_probe_errors": [],
        "frozen_media_before_null": dict(frozen_media),
        "frozen_media_after_null": dict(frozen_media),
        "final_valve_drop_after_null": True,
        "final_generation_linked_after_null": True,
        "all_final_fragments_closed_after_null": True,
        "parent_count_at_block": 100,
        "parent_last_pts_at_block": held_pts,
        "routed_count_at_block": 99,
        "initial_unrouted_frames": 1,
        "final_parent_count": 102,
        "final_parent_last_pts_ns": held_pts + 2 * period,
        "final_routed_count": 99,
        "routed_count_stable": True,
        "additional_parent_frames": 2,
        "allowed_final_unrouted_video_frames": media_budget,
        "measured_final_unrouted_video_frames": 3,
        "tail_identity_verified": True,
        "within_time_frame_contract": True,
    }


def test_final_shutdown_tail_contract_accepts_exact_run_r_categories() -> None:
    harness = _load()
    evidence = _valid_final_shutdown_tail(harness)

    assert harness._final_shutdown_tail_contract(evidence)


def test_final_shutdown_tail_contract_accepts_bounded_preblock_ring_eviction() -> None:
    harness = _load()
    evidence = _valid_final_shutdown_tail(harness)
    baseline = cast(dict[str, object], evidence["common_tee_baseline"])
    baseline.update(
        {
            "retained_count_at_block": harness.MAX_FINAL_COMMON_TEE_RING_BUFFERS,
            "evicted_count_at_block": 36,
            "total_count_at_block": 100,
            "held_ring_index": harness.MAX_FINAL_COMMON_TEE_RING_BUFFERS - 1,
            "held_sequence": 100,
            "total_count_at_release": 100,
            "final_total_count": 101,
        }
    )
    common = cast(list[dict[str, object]], evidence["common_tee_buffers"])
    common[0]["sequence"] = 100
    common[1]["sequence"] = 101

    assert harness._final_shutdown_tail_contract(evidence)


@pytest.mark.parametrize(
    ("mutation", "value"),
    (
        ("initial_unrouted_frames", 2),
        ("routed_count_stable", False),
        ("final_routed_count", 100),
        ("measured_final_unrouted_video_frames", 6),
        ("tail_identity_verified", False),
        ("within_time_frame_contract", False),
        ("null_request_after_unblock_ns", 40_000_000),
    ),
)
def test_final_shutdown_tail_contract_refuses_identity_and_timing_drift(
    mutation: str,
    value: object,
) -> None:
    harness = _load()
    evidence = _valid_final_shutdown_tail(harness)
    evidence[mutation] = value

    assert not harness._final_shutdown_tail_contract(evidence)


def test_final_shutdown_tail_contract_refuses_unattributed_or_overlong_tail() -> None:
    harness = _load()
    evidence = _valid_final_shutdown_tail(harness)
    counter = cast(dict[str, object], evidence["closed_valve_counter"])
    counter["count"] = 3
    assert not harness._final_shutdown_tail_contract(evidence)

    evidence = _valid_final_shutdown_tail(harness)
    maximum = harness.MAX_FINAL_SHUTDOWN_MEDIA_PERIODS * harness.FRAME_PERIOD_NS
    overlong_last = cast(int, evidence["release_requested_monotonic_ns"]) + maximum + 1
    evidence["last_closed_valve_buffer_monotonic_ns"] = overlong_last
    evidence["null_ended_monotonic_ns"] = overlong_last + 1
    closed = cast(list[dict[str, object]], evidence["closed_valve_buffers"])
    closed[-1]["observed_monotonic_ns"] = overlong_last
    evidence["media_window_ns"] = maximum + 1
    evidence["media_window_frame_budget"] = (
        1 + (maximum + harness.FRAME_PERIOD_NS) // harness.FRAME_PERIOD_NS
    )
    assert not harness._final_shutdown_tail_contract(evidence)


def test_final_shutdown_tail_contract_refuses_pre_release_drift_or_baseline_mismatch() -> None:
    harness = _load()
    evidence = _valid_final_shutdown_tail(harness)
    baseline = cast(dict[str, object], evidence["common_tee_baseline"])
    baseline["total_count_at_release"] = cast(int, baseline["total_count_at_release"]) + 1
    assert not harness._final_shutdown_tail_contract(evidence)

    evidence = _valid_final_shutdown_tail(harness)
    baseline = cast(dict[str, object], evidence["common_tee_baseline"])
    baseline["held_is_exact_last_record"] = False
    assert not harness._final_shutdown_tail_contract(evidence)


def test_final_shutdown_tail_contract_refuses_overlong_media_pts_span() -> None:
    harness = _load()
    evidence = _valid_final_shutdown_tail(harness)
    maximum = harness.MAX_FINAL_SHUTDOWN_MEDIA_PERIODS * harness.FRAME_PERIOD_NS
    evidence["media_pts_span_ns"] = maximum + 1
    assert not harness._final_shutdown_tail_contract(evidence)


def test_final_shutdown_tail_contract_refuses_pre_null_or_multiple_parent_only_buffers() -> None:
    harness = _load()
    evidence = _valid_final_shutdown_tail(harness)
    parent_only = cast(list[dict[str, object]], evidence["post_null_parent_only_buffers"])
    parent_only[0]["observed_monotonic_ns"] = (
        cast(int, evidence["null_started_monotonic_ns"]) - 1
    )
    assert not harness._final_shutdown_tail_contract(evidence)

    evidence = _valid_final_shutdown_tail(harness)
    parent_only = cast(list[dict[str, object]], evidence["post_null_parent_only_buffers"])
    parent_only.append(dict(parent_only[0]))
    assert not harness._final_shutdown_tail_contract(evidence)


def test_final_shutdown_tail_contract_refuses_tee_or_frozen_media_drift() -> None:
    harness = _load()
    evidence = _valid_final_shutdown_tail(harness)
    common = cast(list[dict[str, object]], evidence["common_tee_buffers"])
    common[-1]["pts_ns"] = cast(int, common[-1]["pts_ns"]) + 1
    assert not harness._final_shutdown_tail_contract(evidence)

    evidence = _valid_final_shutdown_tail(harness)
    frozen = cast(dict[str, object], evidence["frozen_media_after_null"])
    frozen["sha256"] = "b" * 64
    assert not harness._final_shutdown_tail_contract(evidence)


def _fallback_experiment(
    harness: ModuleType,
    *,
    exact_error_count: int,
) -> Any:
    experiment = object.__new__(harness.PhysicalLossExperiment)

    class Event:
        next_seqnum = 1000

        def __init__(self) -> None:
            type(self).next_seqnum += 1
            self.seqnum = type(self).next_seqnum

        def get_seqnum(self) -> int:
            return self.seqnum

    class EventFactory:
        @staticmethod
        def new_eos() -> Event:
            return Event()

    class Gst:
        Event = EventFactory

    experiment.gst = Gst()
    experiment._eos_dispatches = []
    experiment.eos_dispatch_evidence = []
    experiment.audio_eos_fallback_evidence = []
    experiment.audio_eos_branch_decision_evidence = []
    experiment.natural_eos_final_absence_checks = []
    experiment._output_audio_eos_lock = threading.Lock()
    experiment._output_audio_eos_observations = []
    experiment._output_audio_eos_arbiter_states = {}
    experiment._retained_audio_idle_probes = []
    experiment.confirmed_physical_loss = {"trigger": "stable_identity_not_found"}
    experiment.audio_loss_errors = [
        {"sequence": sequence} for sequence in range(1, exact_error_count + 1)
    ]
    experiment.events = []
    return experiment


def _fallback_old(
    *,
    send_result: bool,
    levels: tuple[int, int, int] = (0, 0, 0),
    counter: Any | None = None,
    on_send: Any | None = None,
) -> tuple[Any, Any]:
    class Peer:
        pad: Any = None

        def get_peer(self) -> Any:
            return self.pad

        def get_path_string(self) -> str:
            return "/pipeline/audio_queue/src"

    peer = Peer()

    class Output:
        pad: Any = None

        def get_static_pad(self, name: str) -> Any:
            return self.pad if name == "audio_0" else None

        def get_path_string(self) -> str:
            return "/pipeline/output"

    output = Output()

    class Pad:
        calls = 0

        def send_event(self, event: object) -> bool:
            self.calls += 1
            if send_result and on_send is not None:
                on_send(self, event)
            return send_result

        def get_path_string(self) -> str:
            return "/pipeline/output/audio_0"

        def get_name(self) -> str:
            return "audio_0"

        def get_parent_element(self) -> Output:
            return output

        def get_peer(self) -> Peer:
            return peer

    pad = Pad()
    peer.pad = pad
    output.pad = pad

    class Queue:
        def get_static_pad(self, name: str) -> Peer | None:
            return peer if name == "src" else None

        def get_property(self, name: str) -> int:
            values = {
                "current-level-buffers": levels[0],
                "current-level-bytes": levels[1],
                "current-level-time": levels[2],
            }
            return values[name]

    old = SimpleNamespace(
        number=1,
        output_audio_pad=pad,
        output=output,
        audio_queue=Queue(),
        audio_valve=SimpleNamespace(get_property=lambda name: name == "drop"),
        audio_counter=counter if counter is not None else SimpleNamespace(count=7),
        external_linked=False,
        audio_eos_seen=False,
    )
    return old, pad


def _primary_eos_evidence(
    *,
    accepted: bool,
    seqnum: int = 401,
) -> dict[str, object]:
    started_ns = time.monotonic_ns()
    return {
        "label": "loss-retired-audio",
        "completed": True,
        "accepted": accepted,
        "timed_out": False,
        "error": None,
        "event_seqnum": seqnum,
        "started_monotonic_ns": started_ns,
        "ended_monotonic_ns": started_ns + 1_000_000,
        "duration_ns": 1_000_000,
    }


def _observe_test_eos(
    harness: ModuleType,
    experiment: Any,
    pad: Any,
    event: Any,
    *,
    observed_monotonic_ns: int | None = None,
) -> None:
    experiment._output_audio_eos_observations.append(
        harness._PadEosObservation(
            pad=pad,
            pad_path=pad.get_path_string(),
            pad_name="audio_0",
            parent_path="/pipeline/output",
            peer_path="/pipeline/audio_queue/src",
            generation_number=1,
            generation_external_linked=True,
            generation_valve_drop=False,
            generation_retired=False,
            active_identity_verified=True,
            forwarded_to_splitmux=True,
            duplicate_refused=False,
            seqnum=event.get_seqnum(),
            observed_monotonic_ns=(
                time.monotonic_ns() if observed_monotonic_ns is None else observed_monotonic_ns
            ),
        )
    )


def _natural_eos_experiment(
    harness: ModuleType,
    *,
    levels: tuple[int, int, int] = (0, 0, 0),
    counter: Any | None = None,
) -> tuple[Any, Any, Any, dict[str, int]]:
    experiment = _fallback_experiment(harness, exact_error_count=0)
    experiment.selector = _selector(_audio_device())
    old, pad = _fallback_old(send_result=True, levels=levels, counter=counter)
    old.audio_eos_seen = True
    times = {
        "armed": 100,
        "error": 200,
        "eos": 300,
        "not_found_1": 400,
        "not_found_2": 500_000_400,
        "video_dispatch": 600_000_000,
    }
    source_path = "/pipeline/audio_source"
    experiment.registered_audio_source_path = source_path
    experiment.loss_wait_armed = True
    experiment.loss_wait_armed_ns = times["armed"]
    experiment.audio_loss_burst_closed = False
    experiment.audio_loss_errors = [
        {
            "sequence": 1,
            "accepted_loss_burst": True,
            "exact_registered_audio_source": True,
            "source_path": source_path,
            "error_domain": "gst-resource-error-quark",
            "error_code": 9,
            "error_message": (
                "Error recording from audio device. The device has been disconnected."
            ),
            "debug": "gst_alsasrc_read (): /pipeline/audio_source",
            "observed_monotonic_ns": times["error"],
        }
    ]
    experiment.confirmed_physical_loss = {
        "trigger": "stable_identity_not_found",
        "first_not_found_sequence": 2,
        "second_not_found_sequence": 3,
        "first_not_found_monotonic_ns": times["not_found_1"],
        "second_not_found_monotonic_ns": times["not_found_2"],
    }
    experiment.discovery_window_open = False
    experiment.loss_discovery_observations = [
        {
            "sequence": 1,
            "observed_monotonic_ns": 150,
            "status": "MATCHED",
            "device_exposed": True,
        },
        {
            "sequence": 2,
            "observed_monotonic_ns": times["not_found_1"],
            "status": "NOT_FOUND",
            "device_exposed": False,
        },
        {
            "sequence": 3,
            "observed_monotonic_ns": times["not_found_2"],
            "status": "NOT_FOUND",
            "device_exposed": False,
        },
    ]
    experiment.eos_dispatch_evidence = [
        {
            "label": "loss-retired-video",
            "completed": True,
            "accepted": True,
            "timed_out": False,
            "error": None,
            "started_monotonic_ns": times["video_dispatch"],
        }
    ]
    _observe_test_eos(
        harness,
        experiment,
        pad,
        SimpleNamespace(get_seqnum=lambda: 179),
        observed_monotonic_ns=times["eos"],
    )
    return experiment, old, pad, times


def test_loss_attributed_natural_audio_eos_uses_zero_audio_dispatches() -> None:
    harness = _load()
    experiment, old, pad, _times = _natural_eos_experiment(harness)

    resolution = experiment._resolve_natural_retired_audio_eos(
        old,
        deadline=time.monotonic() + 2,
    )

    assert resolution["delivery_mode"] == "loss_attributed_natural_upstream_eos"
    assert resolution["audio_dispatch_attempted"] is False
    assert resolution["audio_dispatch_return"] is None
    assert resolution["audio_dispatch_attempt_count"] == 0
    assert resolution["fallback_used"] is False
    assert resolution["natural_exact_eos_observation"]["seqnum"] == 179
    assert resolution["loss_attribution_timing_verified"] is True
    assert resolution["no_rematch_after_natural_eos"] is True
    assert resolution["effective_delivery_observed"] is True
    assert pad.calls == 0


def test_first_eos_arbiter_drops_late_primary_after_natural_race() -> None:
    harness = _load()
    experiment = _fallback_experiment(harness, exact_error_count=1)

    class PadProbeReturn:
        OK = "OK"
        DROP = "DROP"

    experiment.gst.PadProbeReturn = PadProbeReturn
    old, pad = _fallback_old(send_result=True)
    generation = SimpleNamespace(
        number=1,
        audio=True,
        output_audio_pad=pad,
        output=old.output,
        audio_queue=old.audio_queue,
        audio_valve=SimpleNamespace(get_property=lambda _name: False),
        external_linked=True,
        retired=False,
        ingress_event_error=None,
    )
    experiment._output_audio_eos_arbiter_states[id(pad)] = {
        "mode": "OPEN",
        "barrier_seqnum": None,
        "barrier_observed": False,
        "barrier_observed_monotonic_ns": None,
        "barrier_event": threading.Event(),
        "manual_eos_seqnum": None,
    }

    assert experiment._output_audio_eos_snapshot(pad) == ()
    natural_return = experiment._arbitrate_output_audio_eos(
        generation,
        pad,
        SimpleNamespace(get_seqnum=lambda: 179),
    )
    primary_return = experiment._arbitrate_output_audio_eos(
        generation,
        pad,
        SimpleNamespace(get_seqnum=lambda: 180),
    )

    observations = experiment._output_audio_eos_snapshot(pad)
    assert natural_return == PadProbeReturn.OK
    assert primary_return == PadProbeReturn.DROP
    assert [item.forwarded_to_splitmux for item in observations] == [True, False]
    assert [item.duplicate_refused for item in observations] == [False, True]
    assert "duplicate exact output audio EOS" in generation.ingress_event_error
    assert not experiment.audio_eos_fallback_evidence


def test_barrier_orders_natural_manual_and_late_eos_fail_closed() -> None:
    harness = _load()

    class PadProbeReturn:
        OK = "OK"
        DROP = "DROP"

    def fixture() -> tuple[Any, Any, Any]:
        experiment = _fallback_experiment(harness, exact_error_count=1)
        experiment.gst.PadProbeReturn = PadProbeReturn
        old, pad = _fallback_old(send_result=True)
        generation = SimpleNamespace(
            number=1,
            audio=True,
            output_audio_pad=pad,
            output=old.output,
            audio_queue=old.audio_queue,
            audio_valve=SimpleNamespace(get_property=lambda _name: False),
            external_linked=True,
            retired=False,
            ingress_event_error=None,
        )
        experiment._output_audio_eos_arbiter_states[id(pad)] = {
            "mode": "OPEN",
            "barrier_seqnum": 500,
            "barrier_observed": False,
            "barrier_observed_monotonic_ns": None,
            "barrier_event": threading.Event(),
            "manual_eos_seqnum": None,
        }
        return experiment, generation, pad

    experiment, generation, pad = fixture()
    assert (
        experiment._arbitrate_output_audio_eos(
            generation,
            pad,
            SimpleNamespace(get_seqnum=lambda: 179),
        )
        == PadProbeReturn.OK
    )
    assert (
        experiment._arbitrate_output_audio_barrier(
            generation,
            pad,
            SimpleNamespace(get_seqnum=lambda: 500),
        )
        == PadProbeReturn.DROP
    )
    assert experiment._output_audio_eos_arbiter_states[id(pad)]["mode"] == "NATURAL"

    experiment, generation, pad = fixture()
    assert (
        experiment._arbitrate_output_audio_barrier(
            generation,
            pad,
            SimpleNamespace(get_seqnum=lambda: 500),
        )
        == PadProbeReturn.DROP
    )
    state = experiment._output_audio_eos_arbiter_states[id(pad)]
    state["manual_eos_seqnum"] = 501
    state["mode"] = "MANUAL_RESERVED"
    assert (
        experiment._arbitrate_output_audio_eos(
            generation,
            pad,
            SimpleNamespace(get_seqnum=lambda: 501),
        )
        == PadProbeReturn.OK
    )
    assert state["mode"] == "MANUAL_DELIVERED"

    experiment, generation, pad = fixture()
    experiment._arbitrate_output_audio_barrier(
        generation,
        pad,
        SimpleNamespace(get_seqnum=lambda: 500),
    )
    assert (
        experiment._arbitrate_output_audio_eos(
            generation,
            pad,
            SimpleNamespace(get_seqnum=lambda: 179),
        )
        == PadProbeReturn.DROP
    )
    assert experiment._output_audio_eos_arbiter_states[id(pad)]["mode"] == "FATAL"
    assert experiment._output_audio_eos_snapshot(pad)[0].forwarded_to_splitmux is False


def test_idle_block_timeout_refuses_before_audio_event_injection() -> None:
    harness = _load()
    experiment = _fallback_experiment(harness, exact_error_count=1)

    class PadProbeType:
        IDLE = 1

    class PadProbeReturn:
        OK = 1

    pad_probe_type_class = PadProbeType
    pad_probe_return_class = PadProbeReturn

    class Gst:
        PadProbeType = pad_probe_type_class
        PadProbeReturn = pad_probe_return_class

    class IdlePad:
        def add_probe(self, _probe_type: int, _callback: Any) -> int:
            return 7

        def get_path_string(self) -> str:
            return "/pipeline/audio_valve/src"

    experiment.gst = Gst()
    experiment._retained_audio_idle_probes = []
    old = SimpleNamespace(
        audio_valve=SimpleNamespace(get_static_pad=lambda _name: IdlePad()),
    )

    with pytest.raises(harness.HarnessError, match=r"IDLE block probe timed out"):
        experiment._install_audio_idle_block(
            old,
            deadline=time.monotonic() + 0.01,
        )
    assert all(item["label"] != "loss-retired-audio" for item in experiment.eos_dispatch_evidence)


def test_already_natural_eos_requires_no_audio_idle_block() -> None:
    harness = _load()
    experiment = _fallback_experiment(harness, exact_error_count=1)
    old, pad = _fallback_old(send_result=True)

    def refuse_idle_access(_name: str) -> Any:
        raise AssertionError("natural terminal arbiter must not install an IDLE block")

    old.audio_valve = SimpleNamespace(
        get_static_pad=refuse_idle_access,
        get_property=lambda name: name == "drop",
    )
    experiment._output_audio_eos_arbiter_states[id(pad)] = {
        "mode": "NATURAL",
        "barrier_seqnum": None,
        "barrier_observed": False,
        "barrier_observed_monotonic_ns": None,
        "barrier_event": threading.Event(),
        "manual_eos_seqnum": None,
    }

    decision, natural, manual = experiment._serialize_audio_eos_branch(
        old,
        deadline=time.monotonic() + 1,
    )

    assert natural is True
    assert manual is None
    assert decision["idle_block"] == {
        "required": False,
        "reason": "natural_eos_already_admitted_before_topology",
        "permanent_output_arbiter_remains": True,
    }
    assert experiment._retained_audio_idle_probes == []


def test_audio_idle_block_releases_only_after_exact_old_closure() -> None:
    harness = _load()
    experiment = _fallback_experiment(harness, exact_error_count=1)
    removed: list[int] = []

    class IdlePad:
        def remove_probe(self, probe: int) -> None:
            removed.append(probe)

    pad = IdlePad()
    old = SimpleNamespace(
        number=1,
        audio_valve=SimpleNamespace(get_static_pad=lambda _name: pad),
        external_linked=False,
        opened_locations=["g01-00.mp4"],
        closed_locations=["g01-00.mp4"],
        video_eos_seen=True,
        audio_eos_seen=True,
    )
    experiment._retained_audio_idle_probes = [(pad, 7)]
    decision: dict[str, object] = {
        "idle_block": {
            "required": True,
            "probe": 7,
            "exact_pad_identity": True,
            "retained_until_terminal_decision_and_exact_old_fragment_closure": True,
            "released_after_exact_unlink_and_old_fragment_closure": False,
        }
    }

    experiment._release_audio_idle_block_after_old_closure(old, decision)

    idle = cast(dict[str, object], decision["idle_block"])
    assert removed == [7]
    assert experiment._retained_audio_idle_probes == []
    assert idle["released_after_exact_unlink_and_old_fragment_closure"] is True
    assert idle["permanent_output_arbiter_remains"] is True


def test_barrier_timeout_refuses_before_manual_eos_reservation() -> None:
    harness = _load()
    experiment = _fallback_experiment(harness, exact_error_count=1)
    old, pad = _fallback_old(send_result=True)
    manual_eos_creations = 0

    class PadProbeType:
        IDLE = 1

    class PadProbeReturn:
        OK = 1
        DROP = 2

    class EventType:
        CUSTOM_DOWNSTREAM = 3

    class Structure:
        @staticmethod
        def new_empty(name: str) -> str:
            return name

    class Event:
        @staticmethod
        def new_custom(_event_type: int, _structure: str) -> Any:
            return SimpleNamespace(get_seqnum=lambda: 500)

        @staticmethod
        def new_eos() -> Any:
            nonlocal manual_eos_creations
            manual_eos_creations += 1
            return SimpleNamespace(get_seqnum=lambda: 501)

    pad_probe_type_class = PadProbeType
    pad_probe_return_class = PadProbeReturn
    event_type_class = EventType
    structure_class = Structure
    event_class = Event

    class Gst:
        PadProbeType = pad_probe_type_class
        PadProbeReturn = pad_probe_return_class
        EventType = event_type_class
        Structure = structure_class
        Event = event_class

    class IdlePad:
        def add_probe(self, _probe_type: int, callback: Any) -> int:
            callback(self, None)
            return 7

        def get_path_string(self) -> str:
            return "/pipeline/audio_valve/src"

    class BarrierSink:
        def send_event(self, _event: Any) -> bool:
            return False

    original_queue = old.audio_queue

    class Queue:
        def get_static_pad(self, name: str) -> Any:
            if name == "sink":
                return BarrierSink()
            return original_queue.get_static_pad(name)

        def get_property(self, name: str) -> int:
            return cast(int, original_queue.get_property(name))

    old.audio_queue = Queue()
    old.audio_valve = SimpleNamespace(
        get_static_pad=lambda _name: IdlePad(),
        get_property=lambda name: name == "drop",
    )
    experiment.gst = Gst()
    experiment._retained_audio_idle_probes = []
    experiment._output_audio_eos_arbiter_states[id(pad)] = {
        "mode": "OPEN",
        "barrier_seqnum": None,
        "barrier_observed": False,
        "barrier_observed_monotonic_ns": None,
        "barrier_event": threading.Event(),
        "manual_eos_seqnum": None,
    }

    with pytest.raises(harness.HarnessError, match=r"barrier observation timed out"):
        experiment._serialize_audio_eos_branch(
            old,
            deadline=time.monotonic() + 0.03,
        )
    assert manual_eos_creations == 0
    assert not experiment._output_audio_eos_snapshot(pad)
    assert [item["label"] for item in experiment.eos_dispatch_evidence] == [
        "loss-retired-audio-serialization-barrier"
    ]


@pytest.mark.parametrize("mode", ["before_arm", "before_error"])
def test_natural_audio_eos_before_armed_exact_error_is_fatal(mode: str) -> None:
    harness = _load()
    experiment, old, pad, times = _natural_eos_experiment(harness)
    observation = experiment._output_audio_eos_observations[0]
    observed_ns = times["armed"] - 1 if mode == "before_arm" else times["error"] - 1
    experiment._output_audio_eos_observations[0] = replace(
        observation,
        observed_monotonic_ns=observed_ns,
    )

    with pytest.raises(harness.HarnessError, match="loss attribution differs"):
        experiment._resolve_natural_retired_audio_eos(
            old,
            deadline=time.monotonic() + 2,
        )
    assert pad.calls == 0


@pytest.mark.parametrize("mode", ["foreign", "unrecognized"])
def test_natural_audio_eos_requires_recognized_exact_loss_error(mode: str) -> None:
    harness = _load()
    experiment, old, pad, _times = _natural_eos_experiment(harness)
    if mode == "foreign":
        experiment.audio_loss_errors[0]["exact_registered_audio_source"] = False
    else:
        experiment.audio_loss_errors[0]["error_domain"] = "foreign-error-quark"

    with pytest.raises(harness.HarnessError, match="loss attribution differs"):
        experiment._resolve_natural_retired_audio_eos(
            old,
            deadline=time.monotonic() + 2,
        )
    assert pad.calls == 0


def test_natural_audio_eos_followed_by_rematch_is_fatal() -> None:
    harness = _load()
    experiment, old, pad, times = _natural_eos_experiment(harness)
    experiment.loss_discovery_observations.insert(
        -2,
        {
            "sequence": 2,
            "observed_monotonic_ns": times["eos"] + 1,
            "status": "MATCHED",
            "device_exposed": True,
        },
    )

    with pytest.raises(harness.HarnessError, match="loss attribution differs"):
        experiment._resolve_natural_retired_audio_eos(
            old,
            deadline=time.monotonic() + 2,
        )
    assert pad.calls == 0


@pytest.mark.parametrize(
    ("observed_ns", "expected_class"),
    [
        (450, "between_stable_not_found_pair"),
        (550_000_000, "after_stable_not_found_pair_before_handoff"),
    ],
)
def test_natural_audio_eos_accepts_between_or_after_stable_pair(
    observed_ns: int,
    expected_class: str,
) -> None:
    harness = _load()
    experiment, old, _pad, times = _natural_eos_experiment(harness)
    experiment._output_audio_eos_observations[0] = replace(
        experiment._output_audio_eos_observations[0],
        observed_monotonic_ns=observed_ns,
    )
    if expected_class == "after_stable_not_found_pair_before_handoff":
        experiment.natural_eos_final_absence_checks = [
            {
                "attempts": 1,
                "required": True,
                "status": "NOT_FOUND",
                "device_exposed": False,
                "before_topology_mutation": True,
                "observed_monotonic_ns": observed_ns + 1,
            }
        ]

    resolution = experiment._resolve_natural_retired_audio_eos(
        old,
        deadline=time.monotonic() + 2,
    )

    assert resolution["natural_eos_timing_class"] == expected_class
    assert resolution["delta_from_first_not_found_ns"] == (observed_ns - times["not_found_1"])
    assert resolution["delta_from_second_not_found_ns"] == (observed_ns - times["not_found_2"])
    assert resolution["stable_not_found_pair_verified"] is True
    assert resolution["no_rematch_after_natural_eos"] is True


def test_stable_pair_is_bound_by_sequence_not_exact_suffix_length() -> None:
    harness = _load()
    experiment, old, _pad, _times = _natural_eos_experiment(harness)
    experiment.loss_discovery_observations.insert(
        1,
        {
            "sequence": 2,
            "observed_monotonic_ns": 250,
            "status": "NOT_FOUND",
            "device_exposed": False,
        },
    )
    experiment.loss_discovery_observations[2]["sequence"] = 3
    experiment.loss_discovery_observations[3]["sequence"] = 4
    experiment.confirmed_physical_loss["first_not_found_sequence"] = 3
    experiment.confirmed_physical_loss["second_not_found_sequence"] = 4

    resolution = experiment._resolve_natural_retired_audio_eos(
        old,
        deadline=time.monotonic() + 2,
    )

    assert resolution["stable_not_found_pair_verified"] is True
    assert resolution["no_invalid_discovery_status_after_first_error"] is True


def test_post_pair_natural_eos_requires_final_read_only_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load()
    experiment, old, _pad, _times = _natural_eos_experiment(harness)
    experiment._output_audio_eos_observations[0] = replace(
        experiment._output_audio_eos_observations[0],
        observed_monotonic_ns=550_000_000,
    )
    monkeypatch.setattr(
        harness,
        "discover_capture_device",
        lambda _selector: AudioDiscoveryOutcome(AudioDiscoveryStatus.NOT_FOUND),
    )

    experiment._verify_final_post_eos_absence_before_topology(old)

    assert experiment.natural_eos_final_absence_checks == [
        {
            "attempts": 1,
            "required": True,
            "reason": "no_recorded_discovery_observation_after_natural_eos",
            "observed_monotonic_ns": experiment.natural_eos_final_absence_checks[0][
                "observed_monotonic_ns"
            ],
            "natural_eos_observed_monotonic_ns": 550_000_000,
            "status": "NOT_FOUND",
            "device_exposed": False,
            "before_topology_mutation": True,
        }
    ]
    assert all(item["label"] != "loss-retired-audio" for item in experiment.eos_dispatch_evidence)


@pytest.mark.parametrize(
    "outcome",
    [
        AudioDiscoveryOutcome(AudioDiscoveryStatus.MATCHED, _audio_device()),
        AudioDiscoveryOutcome(AudioDiscoveryStatus.AMBIGUOUS),
        AudioDiscoveryOutcome(AudioDiscoveryStatus.REFUSED),
    ],
)
def test_post_pair_final_absence_refuses_later_non_absence(
    outcome: AudioDiscoveryOutcome,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load()
    experiment, old, _pad, _times = _natural_eos_experiment(harness)
    experiment._output_audio_eos_observations[0] = replace(
        experiment._output_audio_eos_observations[0],
        observed_monotonic_ns=550_000_000,
    )
    monkeypatch.setattr(harness, "discover_capture_device", lambda _selector: outcome)

    with pytest.raises(harness.HarnessError, match=r"did not prove NOT_FOUND"):
        experiment._verify_final_post_eos_absence_before_topology(old)
    assert all(item["label"] != "loss-retired-audio" for item in experiment.eos_dispatch_evidence)


def test_post_pair_final_absence_records_and_refuses_discovery_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load()
    experiment, old, _pad, _times = _natural_eos_experiment(harness)
    experiment._output_audio_eos_observations[0] = replace(
        experiment._output_audio_eos_observations[0],
        observed_monotonic_ns=550_000_000,
    )

    def fail_discovery(_selector: AlsaSelector) -> AudioDiscoveryOutcome:
        raise RuntimeError("malformed discovery result")

    monkeypatch.setattr(harness, "discover_capture_device", fail_discovery)

    with pytest.raises(harness.HarnessError, match=r"exact-device discovery failed"):
        experiment._verify_final_post_eos_absence_before_topology(old)
    assert experiment.natural_eos_final_absence_checks[0]["status"] == "MALFORMED"
    assert experiment.natural_eos_final_absence_checks[0]["error"] == "malformed discovery result"
    assert all(item["label"] != "loss-retired-audio" for item in experiment.eos_dispatch_evidence)


def test_post_pair_final_absence_refuses_malformed_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load()
    experiment, old, _pad, _times = _natural_eos_experiment(harness)
    experiment._output_audio_eos_observations[0] = replace(
        experiment._output_audio_eos_observations[0],
        observed_monotonic_ns=550_000_000,
    )
    monkeypatch.setattr(
        harness,
        "discover_capture_device",
        lambda _selector: SimpleNamespace(status="NOT_FOUND"),
    )

    with pytest.raises(harness.HarnessError, match=r"did not prove NOT_FOUND"):
        experiment._verify_final_post_eos_absence_before_topology(old)
    assert experiment.natural_eos_final_absence_checks[0]["status"] == "MALFORMED"
    assert experiment.natural_eos_final_absence_checks[0]["status_detail"] == "NOT_FOUND"
    assert experiment.natural_eos_final_absence_checks[0]["device_exposed"] is True
    assert all(item["label"] != "loss-retired-audio" for item in experiment.eos_dispatch_evidence)


@pytest.mark.parametrize("status", ["MATCHED", "AMBIGUOUS", "REFUSED"])
def test_natural_audio_eos_refuses_rematch_or_invalid_status_after_error(
    status: str,
) -> None:
    harness = _load()
    experiment, old, _pad, _times = _natural_eos_experiment(harness)
    experiment.loss_discovery_observations[1]["status"] = status
    experiment.loss_discovery_observations[1]["device_exposed"] = status == "MATCHED"

    with pytest.raises(harness.HarnessError, match="loss attribution differs"):
        experiment._resolve_natural_retired_audio_eos(
            old,
            deadline=time.monotonic() + 2,
        )


def test_natural_audio_eos_requires_every_exact_error_to_precede_it() -> None:
    harness = _load()
    experiment, old, _pad, _times = _natural_eos_experiment(harness)
    experiment.audio_loss_errors.append(
        {
            **experiment.audio_loss_errors[0],
            "sequence": 2,
            "observed_monotonic_ns": 301,
            "error_domain": "gst-stream-error-quark",
            "error_code": 1,
            "error_message": "Internal data stream error.",
            "debug": "gst_base_src_loop (): reason error (-5)",
        }
    )

    with pytest.raises(harness.HarnessError, match="loss attribution differs"):
        experiment._resolve_natural_retired_audio_eos(
            old,
            deadline=time.monotonic() + 2,
        )


def test_duplicate_natural_audio_eos_is_fatal_without_fallback() -> None:
    harness = _load()
    experiment, old, pad, _times = _natural_eos_experiment(harness)
    experiment._output_audio_eos_observations.append(
        replace(
            experiment._output_audio_eos_observations[0],
            seqnum=180,
            observed_monotonic_ns=301,
        )
    )

    with pytest.raises(harness.HarnessError, match="observation count differs"):
        experiment._resolve_natural_retired_audio_eos(
            old,
            deadline=time.monotonic() + 2,
        )
    assert pad.calls == 0
    assert experiment.audio_eos_fallback_evidence[-1]["fallback_used"] is False


@pytest.mark.parametrize("mode", ["nonzero", "unstable"])
def test_natural_audio_eos_queue_proof_fails_closed(mode: str) -> None:
    harness = _load()

    class UnstableCounter:
        reads = 0

        @property
        def count(self) -> int:
            self.reads += 1
            return self.reads

    experiment, old, pad, _times = _natural_eos_experiment(
        harness,
        levels=(1, 0, 0) if mode == "nonzero" else (0, 0, 0),
        counter=UnstableCounter() if mode == "unstable" else None,
    )

    with pytest.raises(
        harness.HarnessError,
        match=r"queue was nonempty|queue/counter stability proof failed",
    ):
        experiment._resolve_natural_retired_audio_eos(
            old,
            deadline=time.monotonic() + 2,
        )
    assert pad.calls == 0


def test_natural_audio_eos_inactive_identity_or_missing_video_dispatch_is_fatal() -> None:
    harness = _load()
    experiment, old, pad, _times = _natural_eos_experiment(harness)
    experiment._output_audio_eos_observations[0] = replace(
        experiment._output_audio_eos_observations[0],
        active_identity_verified=False,
    )

    with pytest.raises(harness.HarnessError, match="loss attribution differs"):
        experiment._resolve_natural_retired_audio_eos(
            old,
            deadline=time.monotonic() + 2,
        )
    assert pad.calls == 0

    experiment, old, pad, _times = _natural_eos_experiment(harness)
    experiment.eos_dispatch_evidence.clear()
    with pytest.raises(harness.HarnessError, match="loss attribution differs"):
        experiment._resolve_natural_retired_audio_eos(
            old,
            deadline=time.monotonic() + 2,
        )
    assert pad.calls == 0


def test_natural_audio_eos_contract_rejects_closure_pad_or_video_failure() -> None:
    harness = _load()
    experiment, old, _pad, _times = _natural_eos_experiment(harness)
    resolution = experiment._resolve_natural_retired_audio_eos(
        old,
        deadline=time.monotonic() + 2,
    )
    resolution["post_closure_pad_identity"] = resolution["initial_pad_identity"]
    resolution["post_closure_pad_identity_verified"] = True
    resolution["post_closure_output_audio_eos_observation_count"] = 1
    resolution["post_closure_forwarded_audio_eos_count"] = 1
    resolution["post_closure_duplicate_audio_eos_refusal_count"] = 0
    resolution["serialization_decision"] = {
        "idle_block": {
            "required": False,
            "reason": "natural_eos_already_admitted_before_topology",
            "permanent_output_arbiter_remains": True,
        },
        "audio_barrier": {
            "required": False,
            "reason": "natural_eos_already_admitted_before_barrier",
        },
        "post_barrier_queue_proof": {
            "queue_snapshots": resolution["queue_snapshots"],
            "audio_counter_stable": True,
        },
        "selected_natural_audio_eos": True,
        "manual_eos_reserved_seqnum": None,
    }
    transition = {
        "video_eos_return": True,
        "audio_eos_primary_return": None,
        "audio_eos_dispatch_attempted": False,
        "audio_eos_dispatch_count": 0,
        "audio_eos_effective_return": True,
        "old_audio_eos_observed": True,
        "audio_eos_fallback": resolution,
        "eos_dispatches": experiment.eos_dispatch_evidence,
    }

    assert harness._audio_eos_fallback_contract(transition) is True
    resolution["post_closure_pad_identity_verified"] = False
    assert harness._audio_eos_fallback_contract(transition) is False
    resolution["post_closure_pad_identity_verified"] = True
    experiment.eos_dispatch_evidence[0]["accepted"] = False
    assert harness._audio_eos_fallback_contract(transition) is False


def test_retired_audio_primary_eos_acceptance_uses_no_fallback() -> None:
    harness = _load()
    experiment = _fallback_experiment(harness, exact_error_count=0)
    old, pad = _fallback_old(send_result=True)
    primary = _primary_eos_evidence(accepted=True)
    _observe_test_eos(
        harness,
        experiment,
        pad,
        SimpleNamespace(get_seqnum=lambda: primary["event_seqnum"]),
        observed_monotonic_ns=cast(int, primary["started_monotonic_ns"]) + 1,
    )

    resolution = experiment._resolve_retired_audio_eos(
        old,
        primary,
        deadline=time.monotonic() + 2,
    )

    assert resolution["fallback_used"] is False
    assert resolution["fallback"] is None
    assert pad.calls == 0


def test_retired_audio_refusal_without_exact_arbiter_observation_has_no_fallback() -> None:
    harness = _load()
    experiment = _fallback_experiment(harness, exact_error_count=2)

    old, pad = _fallback_old(send_result=True)
    primary = _primary_eos_evidence(accepted=False)

    with pytest.raises(harness.HarnessError, match=r"lacked exact reserved arbiter"):
        experiment._resolve_retired_audio_eos(
            old,
            primary,
            deadline=time.monotonic() + 2,
        )
    assert experiment.audio_eos_fallback_evidence[-1]["fallback_used"] is False
    assert not experiment.eos_dispatch_evidence
    assert pad.calls == 0


def test_retired_audio_false_return_with_exact_output_eos_observed_uses_no_duplicate() -> None:
    harness = _load()
    experiment = _fallback_experiment(harness, exact_error_count=2)
    old, pad = _fallback_old(send_result=True)
    primary = _primary_eos_evidence(accepted=False)
    _observe_test_eos(
        harness,
        experiment,
        pad,
        SimpleNamespace(get_seqnum=lambda: primary["event_seqnum"]),
        observed_monotonic_ns=cast(int, primary["ended_monotonic_ns"]) + 1,
    )

    resolution = experiment._resolve_retired_audio_eos(
        old,
        primary,
        deadline=time.monotonic() + 2,
    )

    assert resolution["delivery_mode"] == ("exact_output_pad_eos_observed_after_primary_refusal")
    assert resolution["output_audio_eos_observation_count_before_primary"] == 0
    assert resolution["primary_exact_eos_observation"]["seqnum"] == primary["event_seqnum"]
    assert resolution["direct_fallback_suppressed_to_avoid_duplicate_eos"] is True
    assert resolution["fallback_used"] is False
    assert resolution["fallback"] is None
    assert resolution["attempt_count"] == 0
    assert pad.calls == 0


def test_retired_audio_eos_observed_before_primary_is_stale_and_fatal() -> None:
    harness = _load()
    experiment = _fallback_experiment(harness, exact_error_count=2)
    old, pad = _fallback_old(send_result=True)
    primary = _primary_eos_evidence(accepted=False)
    _observe_test_eos(
        harness,
        experiment,
        pad,
        SimpleNamespace(get_seqnum=lambda: primary["event_seqnum"]),
        observed_monotonic_ns=cast(int, primary["started_monotonic_ns"]) - 1,
    )

    with pytest.raises(harness.HarnessError, match=r"pre-primary.*stale"):
        experiment._resolve_retired_audio_eos(
            old,
            primary,
            deadline=time.monotonic() + 2,
            observation_count_before_primary=1,
        )
    assert pad.calls == 0


def test_retired_audio_mismatched_dispatch_seqnum_is_fatal() -> None:
    harness = _load()
    experiment = _fallback_experiment(harness, exact_error_count=2)
    old, pad = _fallback_old(send_result=True)
    primary = _primary_eos_evidence(accepted=False)
    _observe_test_eos(
        harness,
        experiment,
        pad,
        SimpleNamespace(get_seqnum=lambda: cast(int, primary["event_seqnum"]) + 1),
        observed_monotonic_ns=cast(int, primary["started_monotonic_ns"]) + 1,
    )

    with pytest.raises(harness.HarnessError, match=r"stale/mismatched.*observation"):
        experiment._resolve_retired_audio_eos(
            old,
            primary,
            deadline=time.monotonic() + 2,
        )
    assert pad.calls == 0


def test_retired_audio_refusal_without_exact_error_corroboration_is_fatal() -> None:
    harness = _load()
    experiment = _fallback_experiment(harness, exact_error_count=0)
    old = SimpleNamespace(output_audio_pad=None, output=None)
    primary = _primary_eos_evidence(accepted=False)

    with pytest.raises(harness.HarnessError, match="lacks stable-loss exact-error"):
        experiment._resolve_retired_audio_eos(
            old,
            primary,
            deadline=time.monotonic() + 2,
        )
    assert experiment.audio_eos_fallback_evidence[0]["eligible"] is False


def test_retired_audio_false_without_observation_never_attempts_direct_pad() -> None:
    harness = _load()
    experiment = _fallback_experiment(harness, exact_error_count=1)

    old, _pad = _fallback_old(send_result=False)
    primary = _primary_eos_evidence(accepted=False)

    with pytest.raises(harness.HarnessError, match=r"lacked exact reserved arbiter"):
        experiment._resolve_retired_audio_eos(
            old,
            primary,
            deadline=time.monotonic() + 2,
        )
    assert not experiment.eos_dispatch_evidence


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("nonzero", "queue was nonempty"),
        ("unstable", "queue/counter stability proof failed"),
        ("identity", "request-pad identity differs"),
        ("timeout", "lacked stability-deadline time"),
    ],
)
def test_retired_audio_fallback_preconditions_fail_closed(
    mode: str,
    reason: str,
) -> None:
    harness = _load()
    experiment = _fallback_experiment(harness, exact_error_count=1)

    class UnstableCounter:
        reads = 0

        @property
        def count(self) -> int:
            self.reads += 1
            return self.reads

    old, _pad = _fallback_old(
        send_result=True,
        levels=(1, 0, 0) if mode == "nonzero" else (0, 0, 0),
        counter=UnstableCounter() if mode == "unstable" else None,
    )
    if mode == "identity":
        old.output.pad = object()
    deadline = time.monotonic() + 0.01 if mode == "timeout" else time.monotonic() + 2
    primary = _primary_eos_evidence(accepted=False)

    with pytest.raises(harness.HarnessError, match=reason):
        experiment._resolve_retired_audio_eos(
            old,
            primary,
            deadline=deadline,
        )
    assert not experiment.eos_dispatch_evidence


def test_eos_dispatch_timeout_is_bounded_and_worker_is_joined_after_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load()
    experiment = object.__new__(harness.PhysicalLossExperiment)
    experiment._eos_dispatches = []
    experiment.eos_dispatch_evidence = []

    class EventFactory:
        @staticmethod
        def new_eos() -> object:
            return SimpleNamespace(get_seqnum=lambda: 999)

    class Gst:
        Event = EventFactory

    release = threading.Event()

    class BlockingPad:
        def send_event(self, _event: object) -> bool:
            assert release.wait(timeout=1)
            return True

    experiment.gst = Gst()
    monkeypatch.setattr(harness, "EOS_DISPATCH_TIMEOUT_SECONDS", 0.01)
    dispatch = experiment._start_downstream_eos(BlockingPad(), "blocked-test")
    with pytest.raises(harness.HarnessError, match="timed out"):
        experiment._await_eos_dispatches((dispatch,))
    release.set()
    experiment._join_eos_workers_after_null()
    assert experiment._eos_dispatches == []


def _terminal_parent_eos_fixture(harness: ModuleType) -> tuple[Any, Any, Any]:
    experiment = object.__new__(harness.PhysicalLossExperiment)

    class Pipeline:
        def get_name(self) -> str:
            return "pipeline0"

        def get_path_string(self) -> str:
            return "/GstPipeline:pipeline0"

    pipeline = Pipeline()
    active_location = "g02-03.mp4"
    generation = SimpleNamespace(
        number=2,
        audio=False,
        retired=False,
        external_linked=True,
        video_valve=SimpleNamespace(get_property=lambda name: name == "drop"),
        opened_locations=[active_location],
        closed_locations=[active_location],
        video_eos_seen=True,
    )
    experiment.pipeline = pipeline
    experiment.generations = {2: generation}
    experiment.transitions = [
        {
            "new_generation": 2,
            "within_one_frame": True,
            "new_first_video_is_idr": True,
        }
    ]
    experiment.video_path_diagnostics = [{"stage": "post_loss_fragment_wait_completed"}]
    experiment.successor_state_convergence = [{"converged": True}]
    experiment.terminal_shutdown_phase = "FINAL_FRAGMENT_CLOSED"
    experiment.terminal_shutdown_context = {
        "final_generation": 2,
        "active_location": active_location,
        "prepared_monotonic_ns": 1,
        "final_video_eos_dispatch": {
            "label": "final-video-only",
            "completed": True,
            "accepted": True,
            "timed_out": False,
            "error": None,
            "event_seqnum": 622,
            "ended_monotonic_ns": 10,
        },
        "fragment_closed_phase_monotonic_ns": 11,
    }
    experiment.terminal_parent_eos_observations = []
    experiment.events = []
    message = SimpleNamespace(src=pipeline)
    return experiment, generation, message


def test_parent_eos_is_accepted_only_after_exact_terminal_fragment_closure() -> None:
    harness = _load()
    experiment, _generation, message = _terminal_parent_eos_fixture(harness)

    assert experiment._accept_terminal_parent_eos(message, "pipeline0") is True
    assert experiment.terminal_shutdown_phase == "TERMINAL_PARENT_EOS_ACCEPTED"
    assert len(experiment.terminal_parent_eos_observations) == 1
    assert experiment.terminal_parent_eos_observations[0]["phase"] == ("FINAL_FRAGMENT_CLOSED")
    assert experiment._accept_terminal_parent_eos(message, "pipeline0") is False
    experiment.terminal_shutdown_phase = "COMPLETE"
    assert (
        harness._terminal_shutdown_contract(
            {
                "phase": experiment.terminal_shutdown_phase,
                "context": experiment.terminal_shutdown_context,
                "parent_eos_observations": experiment.terminal_parent_eos_observations,
            }
        )
        is True
    )


@pytest.mark.parametrize(
    "mutation",
    ["inactive", "before_closure", "foreign_source", "dispatch_refused", "proof_missing"],
)
def test_parent_eos_refuses_outside_exact_terminal_window_or_order(
    mutation: str,
) -> None:
    harness = _load()
    experiment, generation, message = _terminal_parent_eos_fixture(harness)
    if mutation == "inactive":
        experiment.terminal_shutdown_phase = "INACTIVE"
    elif mutation == "before_closure":
        experiment.terminal_shutdown_phase = "FINAL_BRANCH_EOS_DISPATCHED"
        generation.closed_locations = []
    elif mutation == "foreign_source":
        message = SimpleNamespace(src=SimpleNamespace(get_path_string=lambda: "/foreign"))
    elif mutation == "dispatch_refused":
        cast(
            dict[str, object],
            experiment.terminal_shutdown_context["final_video_eos_dispatch"],
        )["accepted"] = False
    else:
        experiment.video_path_diagnostics = []

    assert experiment._accept_terminal_parent_eos(message, "pipeline0") is False
    assert experiment.terminal_parent_eos_observations == []


def test_parent_eos_bus_path_keeps_fail_closed_fallback() -> None:
    source = HARNESS_PATH.read_text(encoding="utf-8")
    bus = source.split("def _drain_bus_once(", 1)[1].split("def _assert_parent_identity", 1)[0]
    assert "self._accept_terminal_parent_eos(message, source)" in bus
    assert '"unexpected_pipeline_eos"' in bus
    assert "unexpected parent pipeline EOS" in bus


def test_media_validation_enforces_exact_generation_stream_sets_and_decode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load()
    names = [
        "g01-00.mp4",
        "g01-01.mp4",
        "g02-00.mp4",
        "g02-01.mp4",
        "g02-02.mp4",
    ]
    for name in names:
        (tmp_path / name).write_bytes(b"media")
    monkeypatch.setattr(
        harness._shared,
        "_probe_media",
        lambda path: _probe_document(path.name.startswith("g01")),
    )
    monkeypatch.setattr(harness._shared, "_first_packet_is_idr", lambda _path: True)
    decoded: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        harness._shared,
        "_decode_media",
        lambda path, audio: decoded.append((path.name, audio)),
    )

    result = harness.validate_media(tmp_path)

    assert result["generation_counts"] == {1: 2, 2: 3}
    assert decoded == [
        ("g01-00.mp4", True),
        ("g01-01.mp4", True),
        ("g02-00.mp4", False),
        ("g02-01.mp4", False),
        ("g02-02.mp4", False),
    ]

    monkeypatch.setattr(harness._shared, "_probe_media", lambda _path: _probe_document(True))
    with pytest.raises(harness.HarnessError, match="stream set"):
        harness.validate_media(tmp_path)
    monkeypatch.setattr(
        harness._shared,
        "_probe_media",
        lambda path: _probe_document(path.name.startswith("g01")),
    )
    foreign = tmp_path / "foreign.txt"
    foreign.write_text("must refuse", encoding="utf-8")
    with pytest.raises(harness.HarnessError, match="foreign member"):
        harness.validate_media(tmp_path)


def test_runtime_fragment_lifecycle_is_exactly_bound_to_validated_media() -> None:
    harness = _load()
    runtime = {
        "generations": {
            "1": {
                "opened_locations": ["g01-00.mp4", "g01-01.mp4"],
                "closed_locations": ["g01-00.mp4", "g01-01.mp4"],
            },
            "2": {
                "opened_locations": ["g02-00.mp4", "g02-01.mp4", "g02-02.mp4"],
                "closed_locations": ["g02-00.mp4", "g02-01.mp4", "g02-02.mp4"],
            },
        }
    }
    media = {
        "members": [
            {"file": name}
            for name in (
                "g01-00.mp4",
                "g01-01.mp4",
                "g02-00.mp4",
                "g02-01.mp4",
                "g02-02.mp4",
            )
        ]
    }

    harness._validate_runtime_media_binding(runtime, media)

    runtime["generations"]["2"]["closed_locations"].pop()
    with pytest.raises(harness.HarnessError, match="lifecycle set"):
        harness._validate_runtime_media_binding(runtime, media)

    runtime["generations"]["2"]["closed_locations"].append("g02-02.mp4")
    media["members"].pop()
    with pytest.raises(harness.HarnessError, match="differ from validated media"):
        harness._validate_runtime_media_binding(runtime, media)


def test_cli_failure_is_exclusive_and_never_claims_production_safety(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load()
    output = tmp_path / "m7-physical-loss-20260727a.json"
    monkeypatch.setattr(harness, "RESULT_ROOT", tmp_path)
    monkeypatch.setattr(harness, "verify_manifest", lambda _expected: {})
    monkeypatch.setattr(
        harness,
        "execute",
        lambda _directory, _timeout: (_ for _ in ()).throw(
            harness.PhysicalExperimentFailure(
                "refused",
                {"audio_loss_error_burst": {"accepted_count": 2}},
            )
        ),
    )

    status = harness.main(
        [
            "--expected-manifest-sha256",
            "a" * 64,
            "run-experiment",
            "--output-directory",
            "/srv/dashcam/quarantine/m7-physical-loss-20260727a",
            "--output",
            str(output),
            "--loss-timeout-seconds",
            "30",
        ]
    )

    document = json.loads(output.read_bytes())
    assert status == 1
    assert document["passed"] is False
    assert document["safe_to_integrate_production"] is False
    assert document["scope"] == "owner_assisted_physical_loss_capability_only"
    assert document["error"] == "refused"
    assert document["diagnostic"]["audio_loss_error_burst"]["accepted_count"] == 2


def test_result_path_is_exact_existing_state_child_and_bound_to_media_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load()
    monkeypatch.setattr(harness, "RESULT_ROOT", tmp_path)
    media = Path("/srv/dashcam/quarantine/m7-physical-loss-20260727a")
    expected = tmp_path / "m7-physical-loss-20260727a.json"
    assert harness._validated_result_path(expected, media) == expected
    expected.write_bytes(b"occupied")
    with pytest.raises(harness.HarnessError, match="fresh"):
        harness._validated_result_path(expected, media)
    with pytest.raises(harness.HarnessError, match="match the media run identity"):
        harness._validated_result_path(tmp_path / "other.json", media)


def test_manifest_is_closed_and_readme_scopes_owner_action_and_paths() -> None:
    harness = _load()
    lines = MANIFEST_PATH.read_text(encoding="ascii").splitlines()
    assert [line.split("  ", 1)[1] for line in lines] == ["README.md", "run.py"]
    assert all(len(line.split("  ", 1)[0]) == 64 for line in lines)
    manifest_hash = hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest()
    entries = harness.verify_manifest(manifest_hash, MANIFEST_PATH.parent)
    assert tuple(entries) == ("README.md", "run.py")
    readme = README_PATH.read_text(encoding="utf-8")
    assert "OWNER_ACTION_REQUIRED" in readme
    assert "safe_to_integrate_production" in readme
    assert "/srv/dashcam/quarantine" in readme
    assert "/var/lib/dashcam/" in readme
    assert "pending, clips, sidecars, catalogs" in readme


def test_source_never_touches_production_or_service_network_state() -> None:
    source = HARNESS_PATH.read_text(encoding="utf-8")
    assert "/srv/dashcam/pending" not in source
    assert "/srv/dashcam/clips" not in source
    assert "systemctl start" not in source
    assert "systemctl stop" not in source
    assert "systemctl restart" not in source
    assert "dashcam-network-fallback" not in source
