from __future__ import annotations

import asyncio
import gc
import os
import stat
import threading
import time
import weakref
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import cast

import pytest

from dashcam.audio.alsa import AlsaCaptureDevice, AlsaIdentity
from dashcam.audio.linux import AudioDiscoveryOutcome, AudioDiscoveryStatus
from dashcam.recorder.gstreamer import (
    AUDIO_BRANCH_ELEMENT_NAMES,
    PIPELINE_DESCRIPTION,
    AudioBranchActionKind,
    AudioBranchState,
    AudioCapturePlan,
    AudioCounters,
    AudioHotplugCoordinator,
    AudioLossArmProof,
    AudioLossHandoff,
    AudioReconnectObservation,
    AudioReconnectPolicy,
    AudioRestorationCriticalError,
    AudioRestoreHandoff,
    AudioStartupError,
    BusMessage,
    BusMessageKind,
    EffectiveAudioCaps,
    EffectiveCaps,
    EncoderIdentity,
    FinalizedFragment,
    ForcedIdrProof,
    FragmentMediaContract,
    FragmentMessage,
    GStreamerBackend,
    GStreamerDriverError,
    GStreamerLimits,
    GStreamerShutdownError,
    OpenedFragment,
    PipelineCounters,
    PyGObjectGStreamerDriver,
    SegmentedOutputConfig,
    _AudioEosArbiter,
    _AudioIngressQuarantine,
    _BoundedEventDispatch,
    _ForcedIdrGate,
    _GenerationPipeline,
    _HeldForcedIdr,
    _RecordingGeneration,
    _RestorationParentFailureProvenance,
    _RetirementDispatch,
    build_audio_ingress_description,
    build_audio_pipeline_description,
    build_legacy_audio_pipeline_description,
)
from dashcam.recorder.pipeline import (
    PipelineContractError,
    ProfileValidationError,
    RecoverablePipelineError,
    VideoProfile,
)


def forced_idr_proof(*, edge_skew_ns: int = 50_000_000) -> ForcedIdrProof:
    request_ns = 1_000_000_000
    downstream_ns = request_ns + 1_000_000
    idr_ns = downstream_ns + 2_000_000
    audio_end = 60_000_000_000 - edge_skew_ns
    return ForcedIdrProof(
        request_count=1,
        request_seqnum=100,
        downstream_seqnum=101,
        seqnum_preserved=False,
        all_headers=True,
        nal5=True,
        request_monotonic_ns=request_ns,
        downstream_event_monotonic_ns=downstream_ns,
        idr_arrival_monotonic_ns=idr_ns,
        downstream_running_time_ns=60_000_000_000,
        forced_idr_running_time_ns=60_000_000_000,
        event_to_idr_media_ns=0,
        request_to_downstream_ns=1_000_000,
        downstream_to_idr_ns=2_000_000,
        request_to_idr_ns=3_000_000,
        last_audio_end_running_time_ns=audio_end,
        edge_skew_ns=edge_skew_ns,
    )


def test_pipeline_counters_are_monotonic_and_qos_is_explicitly_unavailable_until_observed() -> None:
    counters = PipelineCounters()
    assert counters.snapshot().dropped_frames is None
    counters.observe_raw_buffer()
    counters.observe_raw_buffer()
    counters.observe_encoded_buffer()
    counters.observe_qos_drop(3)
    assert counters.snapshot().raw_frames == 2
    assert counters.snapshot().encoded_access_units == 1
    assert counters.snapshot().dropped_frames == 3
    assert counters.snapshot().drop_source == "gstreamer-qos"
    with pytest.raises(ValueError, match="non-negative"):
        counters.observe_qos_drop(-1)


def test_fragment_media_contract_is_immutable_and_validates_generation_and_audio() -> None:
    caps = EffectiveAudioCaps(
        "S16LE",
        48_000,
        1,
        "aac",
        4,
        "raw",
        "voaacenc",
        "aacparse",
        128_000,
    )
    contract = FragmentMediaContract(7, caps)
    assert contract.generation_id == 7
    assert contract.audio_caps is caps

    with pytest.raises(ValueError, match="positive 32-bit"):
        FragmentMediaContract(0, None)
    with pytest.raises(ValueError, match="positive 32-bit"):
        FragmentMediaContract(True, None)
    with pytest.raises(ValueError, match="production contract"):
        FragmentMediaContract(
            1,
            EffectiveAudioCaps(
                "S16LE",
                44_100,
                1,
                "aac",
                4,
                "raw",
                "voaacenc",
                "aacparse",
                128_000,
            ),
        )
    with pytest.raises(ValueError, match="EffectiveAudioCaps"):
        FragmentMediaContract(1, object())  # type: ignore[arg-type]


def test_fragment_generation_fields_validate_explicit_start_and_contract_types() -> None:
    path = Path("/srv/dashcam/pending/boot-ba1b2c3d4-000007.partial.mp4")
    contract = FragmentMediaContract(3, None)
    assert FinalizedFragment(path, 7, 20, 10, contract).start_running_time_ns == 10
    assert OpenedFragment(path, 7, 10, 10, contract).media_contract is contract

    with pytest.raises(ValueError, match="precede"):
        FinalizedFragment(path, 7, 20, 20, contract)
    with pytest.raises(ValueError, match="equal"):
        OpenedFragment(path, 7, 10, 9, contract)
    with pytest.raises(ValueError, match="FragmentMediaContract"):
        FinalizedFragment(path, 7, 20, 10, object())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="audio access units"):
        FragmentMediaContract(1, None, 1)
    with pytest.raises(ValueError, match="handoff proof"):
        AudioLossHandoff(
            1,
            2,
            10,
            True,
            False,
            True,
            True,
            31,
            True,
            forced_idr_proof(),
        )


def test_pipeline_counters_measure_30fps_pts_gaps_with_integer_jitter_tolerance() -> None:
    counters = PipelineCounters()
    counters.configure_pts_cadence(30, 1)
    counters.observe_raw_pts(0)
    counters.observe_raw_pts(33_333_333)
    assert counters.snapshot().dropped_frames == 0
    assert counters.snapshot().drop_source == "encoder-input-pts-gap"
    counters.observe_raw_pts(100_000_000)  # exactly two frame periods after the prior PTS
    assert counters.snapshot().dropped_frames == 1


def test_encoder_input_pts_gap_trace_is_exact_and_nonfatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes: list[bytes] = []

    def capture_write(_fd: int, value: bytes) -> int:
        writes.append(value)
        return len(value)

    def broken_write(_fd: int, _value: bytes) -> int:
        raise BrokenPipeError

    monkeypatch.setenv("DASHCAM_HANDOFF_TRACE", "1")
    monkeypatch.setattr(os, "write", capture_write)
    counters = PipelineCounters()
    counters.configure_pts_cadence(30, 1)
    counters.observe_raw_pts(0)
    counters.observe_raw_pts(400_000_000)

    assert counters.snapshot().dropped_frames == 11
    assert len(writes) == 1
    assert b"dashcam-encoder-input-pts-gap" in writes[0]
    assert b"missing_frames=11" in writes[0]
    assert b"previous_pts_ns=0" in writes[0]
    assert b"current_pts_ns=400000000" in writes[0]

    monkeypatch.setattr(os, "write", broken_write)
    counters.observe_raw_pts(800_000_000)
    assert counters.snapshot().dropped_frames == 22


def test_pipeline_counters_accept_realistic_jitter_but_refuse_more_than_a_quarter_frame() -> None:
    jittered = PipelineCounters()
    jittered.configure_pts_cadence(30, 1)
    jittered.observe_raw_pts(0)
    jittered.observe_raw_pts(34_333_333)  # +1 ms around one 30fps period
    assert jittered.snapshot().dropped_frames == 0

    inside = PipelineCounters()
    inside.configure_pts_cadence(30, 1)
    inside.observe_raw_pts(0)
    inside.observe_raw_pts(41_666_666)  # just inside the 1/4-frame bound
    assert inside.snapshot().dropped_frames == 0

    outside = PipelineCounters()
    outside.configure_pts_cadence(30, 1)
    outside.observe_raw_pts(0)
    outside.observe_raw_pts(41_666_668)  # just outside the integer bound
    assert outside.snapshot().dropped_frames is None


def test_pipeline_counters_leave_pts_drops_unavailable_after_invalid_or_regressing_pts() -> None:
    counters = PipelineCounters()
    counters.configure_pts_cadence(30, 1)
    counters.observe_raw_pts(0)
    counters.observe_raw_pts(None)
    assert counters.snapshot().dropped_frames is None

    regressing = PipelineCounters()
    regressing.configure_pts_cadence(30, 1)
    regressing.observe_raw_pts(33_333_334)
    regressing.observe_raw_pts(0)
    assert regressing.snapshot().dropped_frames is None


def test_pipeline_counters_prefer_the_larger_qos_or_pts_gap_estimate_without_double_counting() -> (
    None
):
    counters = PipelineCounters()
    counters.configure_pts_cadence(30, 1)
    counters.observe_raw_pts(0)
    counters.observe_raw_pts(100_000_000)
    counters.observe_qos_drop(5)
    snapshot = counters.snapshot()
    assert snapshot.dropped_frames == 5
    assert snapshot.drop_source == "gstreamer-qos"


def run_async(coroutine: object) -> object:
    return asyncio.run(coroutine)  # type: ignore[arg-type]


def output_config(**changes: object) -> SegmentedOutputConfig:
    values: dict[str, object] = {
        "output_directory": Path("/srv/dashcam/pending"),
        "boot_id": "ba1b2c3d4",
    }
    values.update(changes)
    return SegmentedOutputConfig(**values)  # type: ignore[arg-type]


def fake_stat(mode: int, device: int) -> os.stat_result:
    return os.stat_result((mode, 1, device, 1, 0, 0, 0, 0, 0, 0))


def fragment_message(
    config: SegmentedOutputConfig,
    sequence: int,
    running_time_ns: int = 60_000_000_000,
) -> BusMessage:
    path = config.output_directory / f"boot-{config.boot_id}-{sequence:06d}.partial.mp4"
    return BusMessage(
        BusMessageKind.FRAGMENT_FINALIZED,
        fragment=FragmentMessage(str(path), running_time_ns),
    )


def opened_message(
    config: SegmentedOutputConfig,
    sequence: int,
    running_time_ns: int = 0,
) -> BusMessage:
    path = config.output_directory / f"boot-{config.boot_id}-{sequence:06d}.partial.mp4"
    return BusMessage(
        BusMessageKind.FRAGMENT_OPENED,
        fragment=FragmentMessage(str(path), running_time_ns),
    )


@dataclass
class FakeDriver:
    caps: EffectiveCaps = field(
        default_factory=lambda: EffectiveCaps(1920, 1080, 30, 1, "NV12", "h264", "high", "4.1")
    )
    identity: EncoderIdentity = field(
        default_factory=lambda: EncoderIdentity(
            "v4l2h264enc",
            "Codec/Encoder/Video/Hardware",
            "/dev/video27",
        )
    )
    audio_caps: EffectiveAudioCaps = field(
        default_factory=lambda: EffectiveAudioCaps(
            "S16LE",
            48_000,
            1,
            "aac",
            4,
            "raw",
            "voaacenc",
            "aacparse",
            128_000,
        )
    )
    messages: list[BusMessage] = field(default_factory=list)
    send_eos_result: bool = True
    set_null_error: Exception | None = None
    set_playing_error: Exception | None = None
    pipeline: object = field(default_factory=object)
    descriptions: list[str] = field(default_factory=list)
    output_settings: list[tuple[str, int]] = field(default_factory=list)
    playing_timeouts: list[float] = field(default_factory=list)
    poll_timeouts: list[float] = field(default_factory=list)
    eos_calls: int = 0
    null_timeouts: list[float] = field(default_factory=list)
    installed_audio_counters: PipelineCounters | None = None
    handoff: AudioLossHandoff = field(
        default_factory=lambda: AudioLossHandoff(
            1,
            2,
            60_000_000_000,
            True,
            True,
            True,
            True,
            31,
            True,
            forced_idr_proof(),
        )
    )
    handoff_calls: list[float] = field(default_factory=list)
    handoff_error: Exception | None = None
    arm_loss_calls: list[str] = field(default_factory=list)
    arm_loss_error: Exception | None = None
    idle_poll_delay_s: float = 0.0
    restore_proof: AudioRestoreHandoff = field(
        default_factory=lambda: AudioRestoreHandoff(
            2,
            3,
            90_000_000_000,
            2,
            1,
            True,
            True,
            True,
            True,
            31,
            1,
            True,
            3,
        )
    )
    restore_calls: list[tuple[AudioCapturePlan, float]] = field(default_factory=list)
    restore_error: Exception | None = None
    overlay_texts: list[str | None] = field(default_factory=list)
    overlay_error: Exception | None = None
    overlay_renderer_snapshot: dict[str, object] = field(
        default_factory=lambda: {
            "state": "ACTIVE",
            "caps_accepted": True,
            "enabled": True,
            "updates": 1,
            "update_rejections": 0,
            "frames_seen": 30,
            "frames_rendered": 30,
            "frames_passthrough": 0,
            "bytes_written": 30 * 73_728,
            "buffer_size_mismatches": 0,
            "short_writes": 0,
            "transform_failures": 0,
            "last_error": None,
        }
    )
    topology_snapshot: dict[str, object] = field(
        default_factory=lambda: {
            "topology_observation": "stable",
            "topology_observation_stale": False,
            "topology_observed_monotonic_ns": 123,
            "active_slot_id": 1,
            "active_activation_id": 1,
            "slot_count": 3,
            "slot_activations": {"1": 1, "2": None, "3": None},
            "request_pad_invariant": "constant_preallocated",
            "request_pad_counts_measured": True,
            "request_pad_peer_ownership_proven": True,
            "video_tee_request_pads": 4,
            "audio_tee_request_pads": 1,
            "splitmux_video_request_pads": 3,
            "splitmux_audio_request_pads": 1,
            "tee_pad_routes": {
                "video_active_linked": 1,
                "video_standby_unlinked": 2,
                "video_continuity_linked": 1,
                "audio_active_linked": 1,
                "audio_standby_unlinked": 0,
            },
            "audio_ingress": {
                "current_count": 1,
                "current_descendant_count": 10,
                "stale_descendant_count": 0,
                "replacement_count": 0,
            },
        }
    )

    def create_pipeline(
        self,
        description: str,
        location_pattern: str,
        start_index: int,
        audio_plan: AudioCapturePlan | None = None,
    ) -> object:
        self.descriptions.append(description)
        self.output_settings.append((location_pattern, start_index))
        return self.pipeline

    def set_playing(self, pipeline: object, timeout_s: float) -> None:
        assert pipeline is self.pipeline
        self.playing_timeouts.append(timeout_s)
        if self.set_playing_error is not None:
            raise self.set_playing_error

    def set_overlay_text(self, pipeline: object, text: str | None) -> None:
        assert pipeline is self.pipeline
        if self.overlay_error is not None:
            raise self.overlay_error
        self.overlay_texts.append(text)

    def overlay_snapshot(self, pipeline: object) -> dict[str, object]:
        assert pipeline is self.pipeline
        return dict(self.overlay_renderer_snapshot)

    def effective_caps(self, pipeline: object) -> EffectiveCaps:
        assert pipeline is self.pipeline
        return self.caps

    def encoder_identity(self, pipeline: object) -> EncoderIdentity:
        assert pipeline is self.pipeline
        return self.identity

    def effective_audio_caps(self, pipeline: object) -> EffectiveAudioCaps:
        assert pipeline is self.pipeline
        return self.audio_caps

    def install_audio_metrics(self, pipeline: object, counters: PipelineCounters) -> None:
        assert pipeline is self.pipeline
        self.installed_audio_counters = counters

    def poll_bus(self, pipeline: object, timeout_s: float) -> BusMessage:
        assert pipeline is self.pipeline
        self.poll_timeouts.append(timeout_s)
        if self.messages:
            return self.messages.pop(0)
        if self.idle_poll_delay_s:
            threading.Event().wait(self.idle_poll_delay_s)
        return BusMessage(BusMessageKind.NONE)

    def send_eos(self, pipeline: object) -> bool:
        assert pipeline is self.pipeline
        self.eos_calls += 1
        return self.send_eos_result

    def arm_audio_loss(
        self,
        pipeline: object,
        source_name: str,
    ) -> AudioLossArmProof:
        assert pipeline is self.pipeline
        self.arm_loss_calls.append(source_name)
        if self.arm_loss_error is not None:
            raise self.arm_loss_error
        return AudioLossArmProof(1, 1, source_name)

    def set_null(self, pipeline: object, timeout_s: float) -> None:
        assert pipeline is self.pipeline
        self.null_timeouts.append(timeout_s)
        if self.set_null_error is not None:
            raise self.set_null_error

    def isolate_audio_loss(self, pipeline: object, timeout_s: float) -> AudioLossHandoff:
        assert pipeline is self.pipeline
        self.handoff_calls.append(timeout_s)
        if self.handoff_error is not None:
            raise self.handoff_error
        self.topology_snapshot["active_slot_id"] = self.handoff.active_slot_id
        self.topology_snapshot["active_activation_id"] = self.handoff.active_generation_id
        self.topology_snapshot["slot_activations"] = {
            "1": self.handoff.retired_generation_id,
            str(self.handoff.active_slot_id): self.handoff.active_generation_id,
            str(5 - self.handoff.active_slot_id): None,
        }
        return self.handoff

    def restore_audio(
        self,
        pipeline: object,
        plan: AudioCapturePlan,
        timeout_s: float,
    ) -> AudioRestoreHandoff:
        assert pipeline is self.pipeline
        self.restore_calls.append((plan, timeout_s))
        if self.restore_error is not None:
            raise self.restore_error
        self.topology_snapshot["active_slot_id"] = self.restore_proof.active_slot_id
        self.topology_snapshot["active_activation_id"] = self.restore_proof.active_generation_id
        self.topology_snapshot["slot_activations"] = {
            "1": self.restore_proof.active_generation_id,
            "2": None,
            "3": None,
        }
        return self.restore_proof

    def generation_snapshot(self, pipeline: object) -> dict[str, object]:
        assert pipeline is self.pipeline
        return dict(self.topology_snapshot)


def test_graph_is_exact_and_omits_unsafe_encoder_assignments() -> None:
    assert PIPELINE_DESCRIPTION.startswith("libcamerasrc name=camera !")
    assert (
        "video/x-raw,width=(int)1920,height=(int)1080,format=(string)NV12,framerate=(fraction)30/1"
    ) in PIPELINE_DESCRIPTION
    assert PIPELINE_DESCRIPTION.count("dashcamnv12overlay name=burned_overlay") == 1
    assert PIPELINE_DESCRIPTION.index("framerate=(fraction)30/1") < (
        PIPELINE_DESCRIPTION.index("dashcamnv12overlay name=burned_overlay")
    ) < PIPELINE_DESCRIPTION.index("v4l2h264enc name=encoder")
    assert "dashcamnv12overlay name=burned_overlay" in PIPELINE_DESCRIPTION
    assert "textoverlay" not in PIPELINE_DESCRIPTION
    assert (
        'extra-controls="controls,repeat_sequence_header=1,video_bitrate=8000000,'
        'h264_i_frame_period=30"'
    ) in PIPELINE_DESCRIPTION
    assert "video/x-h264,profile=(string)high,level=(string)4.1" in PIPELINE_DESCRIPTION
    assert "h264parse name=parser config-interval=-1" in PIPELINE_DESCRIPTION
    assert (
        "queue name=record_queue max-size-buffers=60 max-size-bytes=4000000 "
        "max-size-time=2000000000 leaky=no"
    ) in PIPELINE_DESCRIPTION
    assert "splitmuxsink name=output max-size-time=60000000000 max-size-bytes=0" in (
        PIPELINE_DESCRIPTION
    )
    assert "send-keyframe-requests=true async-finalize=true" in PIPELINE_DESCRIPTION
    assert "muxer-factory=mp4mux sink-factory=filesink" in PIPELINE_DESCRIPTION
    assert (
        'muxer-properties="properties,fragment-duration=(uint)1000,fragment-mode=(int)0"'
    ) in PIPELINE_DESCRIPTION
    assert "max-files=" not in PIPELINE_DESCRIPTION
    assert "device=" not in PIPELINE_DESCRIPTION
    assert "bitrate_mode" not in PIPELINE_DESCRIPTION


def test_fixed_generation_graph_uses_only_synchronous_reusable_outputs() -> None:
    driver = PyGObjectGStreamerDriver(object())

    av_description = driver._generation_description(1, True)
    video_description = driver._generation_description(2, False)

    for description in (av_description, video_description):
        assert "async-finalize=false reset-muxer=true" in description
        assert "muxer=mp4mux sink=filesink" in description
        assert "async-finalize=true" not in description
        assert "muxer-factory" not in description
        assert "sink-factory" not in description
        assert "muxer-properties" not in description
    assert "g01_output.audio_0" in av_description
    assert "video_gate_queue" not in av_description
    assert "audio_valve" not in video_description
    assert (
        "g02_video_gate_queue max-size-buffers=2 max-size-bytes=4000000 "
        "max-size-time=100000000 leaky=downstream ! "
        "valve name=g02_video_valve"
    ) in video_description
    assert "async-finalize=true" in PIPELINE_DESCRIPTION


def audio_plan(endpoint: str = "hw:1,0,0") -> AudioCapturePlan:
    identity = AlsaIdentity(
        "08bb",
        "2902",
        physical_path="platform-3f980000.usb-usb-0:1:1.0",
        product="USB_PnP_Sound_Device",
    )
    return AudioCapturePlan(endpoint, identity, 48_000, 1, "aac", 128_000)


def test_matched_audio_graph_is_exact_clocked_bounded_generation_ingress() -> None:
    description = build_audio_pipeline_description(audio_plan())
    ingress = build_audio_ingress_description(audio_plan())

    assert description.startswith(
        PIPELINE_DESCRIPTION.split(" ! splitmuxsink", maxsplit=1)[0]
        + " ! identity name=video_generation_counter silent=true ! "
        "tee name=video_tee allow-not-linked=true "
    )
    assert description.endswith("tee name=audio_tee allow-not-linked=true")
    assert description.count("dashcamnv12overlay name=burned_overlay") == 1
    assert description.index("dashcamnv12overlay name=burned_overlay") < description.index(
        "v4l2h264enc name=encoder"
    )
    assert description.count("name=video_continuity_queue") == 1
    assert (
        "video_tee. ! queue name=video_continuity_queue "
        "max-size-buffers=2 max-size-bytes=0 max-size-time=0 "
        "leaky=downstream ! fakesink name=video_continuity_sink "
        "sync=false async=false enable-last-sample=false qos=false"
    ) in description
    assert "alsasrc" not in description
    assert "capsfilter" not in description
    assert (
        "alsasrc name=audio_source device=hw:1,0,0 provide-clock=false "
        "slave-method=resample use-driver-timestamps=false do-timestamp=true ! "
    ) in ingress
    assert (
        "audio/x-raw,format=(string)S16LE,rate=(int)48000,channels=(int)1,"
        "layout=(string)interleaved"
    ) in ingress
    assert "voaacenc name=audio_encoder bitrate=128000" in ingress
    assert "aacparse name=audio_parser" in ingress
    assert ("identity name=audio_generation_counter silent=true") in ingress
    assert "splitmuxsink" not in description
    assert description.count("max-size-time=2000000000 leaky=no") == 1
    assert ingress.count("max-size-time=2000000000 leaky=downstream") == 2


@pytest.mark.parametrize(
    "endpoint",
    ["hw:1,0", "default", "hw:1,0,0 ! fakesink", 'hw:1,0,0"'],
)
def test_audio_plan_refuses_endpoint_injection(endpoint: str) -> None:
    with pytest.raises(ValueError, match="endpoint"):
        audio_plan(endpoint)


def reconnect_device(
    *,
    card: int = 7,
    path: str = "platform-3f980000.usb-usb-0:1:1.0",
) -> AlsaCaptureDevice:
    return AlsaCaptureDevice(
        AlsaIdentity(
            "08bb",
            "2902",
            physical_path=path,
            product="USB_PnP_Sound_Device",
        ),
        card,
        0,
    )


def test_hotplug_coordinator_is_bounded_boundary_gated_and_supports_repeated_loss() -> None:
    coordinator = AudioHotplugCoordinator(
        audio_plan(),
        policy=AudioReconnectPolicy(interval_ns=1_000_000_000, max_attempts=3),
    )

    loss = coordinator.observe_loss("audio_source")
    assert loss is not None
    assert loss.kind is AudioBranchActionKind.QUIESCE
    assert loss.generation == 1
    assert coordinator.observe_loss("audio_parser") is None
    assert coordinator.active is False
    coordinator.observe_quiesced(100)

    first = coordinator.poll(100)
    assert first is not None
    assert first.kind is AudioBranchActionKind.REDISCOVER
    assert coordinator.poll(100) is None
    coordinator.observe_rediscovery(
        first.generation,
        reconnect_device(path="platform-3f980000.usb-usb-0:2:1.0"),
        now_ns=200,
    )
    assert coordinator.state is AudioBranchState.UNAVAILABLE
    assert coordinator.observe_fragment_boundary() is None
    assert coordinator.poll(1_000_000_199) is None

    second = coordinator.poll(1_000_000_200)
    assert second is not None
    coordinator.observe_rediscovery(
        second.generation,
        reconnect_device(card=9),
        now_ns=1_000_000_300,
    )
    assert coordinator.snapshot()["stable_confirmations"] == 1
    third = coordinator.poll(2_000_000_300)
    assert third is not None
    coordinator.observe_rediscovery(
        third.generation,
        reconnect_device(card=9),
        now_ns=2_000_000_400,
    )
    assert coordinator.snapshot()["state"] == AudioBranchState.RESTORE_PENDING.value
    restore = coordinator.observe_fragment_boundary()
    assert restore is not None
    assert restore.kind is AudioBranchActionKind.RESTORE
    assert restore.plan is not None
    assert restore.plan.endpoint == "hw:9,0,0"
    assert coordinator.observe_fragment_boundary() is None
    coordinator.observe_restored(restore.generation)
    assert coordinator.active

    repeated = coordinator.observe_loss("audio_record_queue")
    assert repeated is not None
    assert repeated.generation == 2
    assert coordinator.snapshot()["rediscovery_attempts"] == 0
    coordinator.observe_quiesced(3_000_000_000)
    for now_ns in (3_000_000_000, 4_000_000_000):
        rediscover = coordinator.poll(now_ns)
        assert rediscover is not None
        coordinator.observe_rediscovery(
            rediscover.generation,
            reconnect_device(card=11),
            now_ns=now_ns,
        )
    repeated_restore = coordinator.observe_fragment_boundary()
    assert repeated_restore is not None
    assert repeated_restore.generation == 2
    assert repeated_restore.plan is not None
    assert repeated_restore.plan.endpoint == "hw:11,0,0"
    coordinator.observe_restored(repeated_restore.generation)
    assert coordinator.active


def test_hotplug_coordinator_refuses_stale_results_and_retries_after_cooldown() -> None:
    coordinator = AudioHotplugCoordinator(
        audio_plan(),
        policy=AudioReconnectPolicy(
            interval_ns=100_000_000,
            max_attempts=2,
            campaign_cooldown_ns=500_000_000,
        ),
    )
    loss = coordinator.observe_loss("audio_encoder")
    assert loss is not None
    coordinator.observe_quiesced(0)
    first = coordinator.poll(0)
    assert first is not None
    with pytest.raises(ValueError, match="stale"):
        coordinator.observe_rediscovery(
            first.generation + 1,
            AudioReconnectObservation.NOT_FOUND,
            now_ns=0,
        )
    coordinator.observe_rediscovery(
        first.generation,
        AudioReconnectObservation.AMBIGUOUS,
        now_ns=0,
    )
    second = coordinator.poll(100_000_000)
    assert second is not None
    coordinator.observe_rediscovery(
        second.generation,
        AudioReconnectObservation.REFUSED,
        now_ns=100_000_000,
    )
    assert coordinator.poll(200_000_000) is None
    assert coordinator.state is AudioBranchState.UNAVAILABLE
    assert coordinator.poll(699_999_999) is None
    retry = coordinator.poll(700_000_000)
    assert retry is not None
    assert retry.kind is AudioBranchActionKind.REDISCOVER
    assert coordinator.snapshot()["rediscovery_campaigns"] == 1


def test_hotplug_coordinator_rejects_wrong_identity_at_the_old_alsa_index() -> None:
    coordinator = AudioHotplugCoordinator(
        audio_plan(),
        policy=AudioReconnectPolicy(interval_ns=100_000_000, max_attempts=3),
    )
    loss = coordinator.observe_loss("audio_source")
    assert loss is not None
    coordinator.observe_quiesced(0)
    action = coordinator.poll(0)
    assert action is not None
    wrong = AlsaCaptureDevice(
        AlsaIdentity(
            "ffff",
            "2902",
            physical_path="platform-3f980000.usb-usb-0:1:1.0",
            product="USB_PnP_Sound_Device",
        ),
        1,
        0,
    )
    coordinator.observe_rediscovery(action.generation, wrong, now_ns=0)
    snapshot = coordinator.snapshot()
    assert snapshot["state"] == AudioBranchState.UNAVAILABLE.value
    assert snapshot["stable_confirmations"] == 0
    assert snapshot["reason"] == "wrong_identity"


@pytest.mark.parametrize("source_name", sorted(AUDIO_BRANCH_ELEMENT_NAMES))
def test_hotplug_coordinator_accepts_only_exact_named_audio_sources(
    source_name: str,
) -> None:
    coordinator = AudioHotplugCoordinator(audio_plan())
    assert coordinator.observe_loss(source_name) is not None

    other = AudioHotplugCoordinator(audio_plan())
    with pytest.raises(ValueError, match="exact named"):
        other.observe_loss("encoder")


def test_matched_backend_validates_effective_aac_and_observes_access_units() -> None:
    async def scenario() -> None:
        driver = FakeDriver()
        plan = audio_plan()
        backend = GStreamerBackend(
            output=output_config(),
            audio_plan=plan,
            driver=driver,
        )

        assert await backend.start(VideoProfile()) == VideoProfile()
        assert driver.descriptions == [build_legacy_audio_pipeline_description(plan)]
        assert backend.effective_audio_caps == driver.audio_caps
        assert driver.installed_audio_counters is not None
        driver.installed_audio_counters.observe_audio_encoded_buffer()
        assert backend.audio_counters() == AudioCounters(1)
        driver.messages.append(BusMessage(BusMessageKind.EOS))
        await backend.stop()

    run_async(scenario())


@pytest.mark.parametrize(
    "caps",
    [
        EffectiveAudioCaps("F32LE", 48_000, 1, "aac", 4, "raw", "voaacenc", "aacparse", 128_000),
        EffectiveAudioCaps("S16LE", 44_100, 1, "aac", 4, "raw", "voaacenc", "aacparse", 128_000),
        EffectiveAudioCaps("S16LE", 48_000, 1, "aac", 2, "raw", "voaacenc", "aacparse", 128_000),
        EffectiveAudioCaps("S16LE", 48_000, 1, "aac", 4, "adts", "voaacenc", "aacparse", 128_000),
        EffectiveAudioCaps("S16LE", 48_000, 1, "aac", 4, "raw", "faac", "aacparse", 128_000),
        EffectiveAudioCaps("S16LE", 48_000, 1, "aac", 4, "raw", "voaacenc", "aacparse", 96_000),
    ],
)
def test_matched_backend_refuses_audio_downgrade(
    caps: EffectiveAudioCaps,
) -> None:
    async def scenario() -> None:
        driver = FakeDriver(audio_caps=caps)
        backend = GStreamerBackend(
            output=output_config(),
            audio_plan=audio_plan(),
            driver=driver,
        )
        with pytest.raises(AudioStartupError, match="audio startup validation"):
            await backend.start(VideoProfile())
        assert driver.null_timeouts == [3.0]

    run_async(scenario())


def test_output_config_generates_only_provisional_pending_locations() -> None:
    config = output_config(start_index=123, event_capacity=4)

    assert (
        config.location_pattern
        == (config.output_directory / "boot-ba1b2c3d4-%06d.partial.mp4").as_posix()
    )
    event = config.finalized_fragment(
        FragmentMessage(
            str(config.output_directory / "boot-ba1b2c3d4-000123.partial.mp4"),
            60_000_000_000,
        )
    )
    assert event == FinalizedFragment(
        config.output_directory / "boot-ba1b2c3d4-000123.partial.mp4",
        123,
        60_000_000_000,
    )


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"output_directory": Path("pending")}, "absolute ASCII path"),
        (
            {"output_directory": Path.cwd().resolve() / "clips"},
            "absolute ASCII path",
        ),
        (
            {"output_directory": Path.cwd().resolve() / "safe" / ".." / "pending"},
            "absolute ASCII path",
        ),
        (
            {"output_directory": Path.cwd().resolve() / "%s" / "pending"},
            "absolute ASCII path",
        ),
        ({"boot_id": "../escape"}, "invalid provisional"),
        ({"start_index": -1}, "start_index"),
        ({"start_index": True}, "start_index"),
        ({"event_capacity": 0}, "event_capacity"),
        ({"event_capacity": 1025}, "event_capacity"),
        ({"expected_st_dev": True}, "expected_st_dev"),
    ],
)
def test_output_config_rejects_unsafe_paths_names_and_bounds(
    changes: dict[str, object],
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        output_config(**changes)


@pytest.mark.parametrize(
    "location",
    [
        "relative/boot-ba1b2c3d4-000000.partial.mp4",
        "../pending/boot-ba1b2c3d4-000000.partial.mp4",
        "boot-ba1b2c3d4-000000.partial.mp4",
    ],
)
def test_output_config_rejects_untrusted_fragment_locations(location: str) -> None:
    config = output_config()
    with pytest.raises(ValueError, match="fragment"):
        config.finalized_fragment(FragmentMessage(location, 0))


def test_start_returns_only_the_verified_effective_profile() -> None:
    async def scenario() -> None:
        driver = FakeDriver()
        config = output_config(start_index=123)
        backend = GStreamerBackend(output=config, driver=driver)

        assert await backend.start(VideoProfile()) == VideoProfile()
        assert driver.descriptions == [PIPELINE_DESCRIPTION]
        assert driver.output_settings == [(config.location_pattern, 123)]
        assert driver.playing_timeouts == [15.0]

        driver.messages.append(BusMessage(BusMessageKind.EOS))
        await backend.stop()

    run_async(scenario())


def test_backend_overlay_updates_are_bounded_serial_and_deduplicated() -> None:
    async def scenario() -> None:
        driver = FakeDriver()
        backend = GStreamerBackend(output=output_config(), driver=driver)

        with pytest.raises(PipelineContractError, match="active pipeline"):
            await backend.set_overlay_text("TIME UNSYNCED\nGPS INVALID")
        with pytest.raises(ValueError, match="two-line ASCII"):
            await backend.set_overlay_text("one\ntwo\nthree")
        with pytest.raises(ValueError, match="two-line ASCII"):
            await backend.set_overlay_text("x" * 97)

        backend.configure_overlay_text("TIME UNSYNCED\nGPS INVALID")
        await backend.start(VideoProfile())
        with pytest.raises(PipelineContractError, match="already bound"):
            backend.configure_overlay_text(None)
        await backend.set_overlay_text("TIME UNSYNCED\nGPS INVALID")
        await backend.set_overlay_text("TIME UNSYNCED\nGPS INVALID")
        await backend.set_overlay_text(None)
        assert driver.overlay_texts == [
            "TIME UNSYNCED\nGPS INVALID",
            None,
        ]
        assert backend.overlay_snapshot() == driver.overlay_renderer_snapshot

        driver.messages.append(BusMessage(BusMessageKind.EOS))
        await backend.stop()

    run_async(scenario())


def test_segment_output_is_bound_to_dashcam_mount_and_reconciles_sequence() -> None:
    with pytest.raises(ValueError, match="application pending"):
        output_config(output_directory=Path("/tmp/pending"))

    config = output_config(start_index=2).after_reconciling(
        (
            "boot-ba1b2c3d4-000002.partial.mp4",
            "boot-ba1b2c3d4-000002.partial.json",
            "boot-other1-000099.partial.mp4",
        )
    )
    assert config.start_index == 3

    with pytest.raises(ValueError, match="unrecognized"):
        config.after_reconciling(("foreign.tmp",))


def test_start_refuses_existing_initial_pair_before_constructing_pipeline() -> None:
    async def scenario() -> None:
        driver = FakeDriver()
        backend = GStreamerBackend(
            output=output_config(start_index=7),
            driver=driver,
            path_exists=lambda path: path.suffix == ".mp4",
        )

        with pytest.raises(RecoverablePipelineError, match="overwrite"):
            await backend.start(VideoProfile())

        assert driver.descriptions == []
        assert driver.playing_timeouts == []
        assert driver.null_timeouts == []

    run_async(scenario())


def test_start_and_each_fragment_open_remain_bound_to_expected_st_dev() -> None:
    async def start_on_replacement_storage() -> None:
        driver = FakeDriver()
        backend = GStreamerBackend(
            output=output_config(expected_st_dev=179),
            driver=driver,
            path_lstat=lambda path: fake_stat(stat.S_IFDIR, 1),
        )

        with pytest.raises(RecoverablePipelineError, match="verified mount device"):
            await backend.start(VideoProfile())
        assert driver.descriptions == []

    async def replacement_on_later_fragment_open() -> None:
        config = output_config(expected_st_dev=179)
        driver = FakeDriver(
            messages=[
                opened_message(config, 0),
                opened_message(config, 1, 60_000_000_000),
            ],
        )

        def lstat_path(path: Path) -> os.stat_result:
            if path == config.output_directory:
                return fake_stat(stat.S_IFDIR, 179)
            device = 1 if "000001" in path.name else 179
            return fake_stat(stat.S_IFREG, device)

        backend = GStreamerBackend(
            output=config,
            driver=driver,
            path_lstat=lstat_path,
        )
        await backend.start(VideoProfile())
        with pytest.raises(RecoverablePipelineError, match="opened fragment left"):
            await backend.run(asyncio.Event())
        driver.messages.extend(
            (
                fragment_message(config, 0),
                BusMessage(BusMessageKind.EOS),
            )
        )
        await backend.stop()

    run_async(start_on_replacement_storage())
    run_async(replacement_on_later_fragment_open())


@pytest.mark.parametrize(
    "caps",
    [
        EffectiveCaps(1280, 1080, 30, 1, "NV12", "h264", "high", "4.1"),
        EffectiveCaps(1920, 1080, 30, 1, "I420", "h264", "high", "4.1"),
        EffectiveCaps(1920, 1080, 30, 1, "NV12", "h264", "baseline", "4.1"),
        EffectiveCaps(1920, 1080, 30, 1, "NV12", "h264", "high", "1"),
    ],
)
def test_start_refuses_effective_caps_mismatch(caps: EffectiveCaps) -> None:
    async def scenario() -> None:
        driver = FakeDriver(caps=caps)
        backend = GStreamerBackend(output=output_config(), driver=driver)

        with pytest.raises(ProfileValidationError, match="effective"):
            await backend.start(VideoProfile())

        assert driver.eos_calls == 0
        assert driver.null_timeouts == [3.0]
        await backend.stop()

    run_async(scenario())


@pytest.mark.parametrize(
    "identity",
    [
        EncoderIdentity("x264enc", "Codec/Encoder/Video", ""),
        EncoderIdentity("v4l2h264enc", "Codec/Encoder/Video", "/dev/video27"),
        EncoderIdentity("v4l2h264enc", "Codec/Encoder/Video/Hardware", "/dev/video"),
    ],
)
def test_start_refuses_nonhardware_or_unidentified_encoder(
    identity: EncoderIdentity,
) -> None:
    async def scenario() -> None:
        driver = FakeDriver(identity=identity)
        backend = GStreamerBackend(output=output_config(), driver=driver)

        with pytest.raises(ProfileValidationError, match="encoder"):
            await backend.start(VideoProfile())

        assert driver.eos_calls == 0
        assert driver.null_timeouts == [3.0]
        await backend.stop()

    run_async(scenario())


def test_run_converts_bus_error_to_recoverable_failure() -> None:
    async def scenario() -> None:
        driver = FakeDriver(messages=[BusMessage(BusMessageKind.ERROR, "STREAMON failed")])
        backend = GStreamerBackend(output=output_config(), driver=driver)
        await backend.start(VideoProfile())

        with pytest.raises(RecoverablePipelineError, match="STREAMON failed"):
            await backend.run(asyncio.Event())

        driver.messages.append(BusMessage(BusMessageKind.EOS))
        await backend.stop()

    run_async(scenario())


def test_cancelled_driver_call_retains_serialization_until_native_worker_exits() -> None:
    async def scenario() -> None:
        backend = GStreamerBackend(output=output_config(), driver=FakeDriver())
        started = threading.Event()
        release = threading.Event()
        order: list[str] = []

        def blocked() -> None:
            started.set()
            release.wait(1.0)
            order.append("blocked_done")

        first = asyncio.create_task(backend._driver_call(blocked))
        assert await asyncio.to_thread(started.wait, 0.2)
        first.cancel()
        second = asyncio.create_task(
            backend._driver_call(lambda: order.append("second_done"))
        )
        await asyncio.sleep(0.02)
        first.cancel()
        await asyncio.sleep(0.01)
        assert second.done() is False
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(first, 0.2)
        await asyncio.wait_for(second, 0.2)
        assert order == ["blocked_done", "second_done"]

    run_async(scenario())


def test_classified_audio_error_remains_fail_closed_until_dynamic_pad_gate_passes() -> None:
    async def scenario() -> None:
        driver = FakeDriver(
            messages=[
                BusMessage(
                    BusMessageKind.AUDIO_ERROR,
                    "microphone disconnected",
                    source_name="audio_source",
                )
            ],
        )
        backend = GStreamerBackend(
            output=output_config(),
            audio_plan=audio_plan(),
            driver=driver,
        )
        await backend.start(VideoProfile())

        with pytest.raises(RecoverablePipelineError, match="not production-enabled"):
            await backend.run(asyncio.Event())

        driver.messages.append(BusMessage(BusMessageKind.EOS))
        await backend.stop()

    run_async(scenario())


def test_enabled_audio_loss_requires_stable_absence_and_generation_bound_events() -> None:
    async def scenario() -> None:
        config = output_config()
        caps = FakeDriver().audio_caps
        old_contract = FragmentMediaContract(1, caps, 93)
        successor_contract = FragmentMediaContract(2, None, 0)
        driver = FakeDriver(
            messages=[
                BusMessage(
                    BusMessageKind.AUDIO_ERROR,
                    "microphone disconnected",
                    source_name="audio_source",
                ),
                BusMessage(
                    BusMessageKind.FRAGMENT_FINALIZED,
                    fragment=FragmentMessage(
                        str(config.output_directory / "boot-ba1b2c3d4-000000.partial.mp4"),
                        60_000_000_000,
                        0,
                        old_contract,
                    ),
                ),
                BusMessage(
                    BusMessageKind.FRAGMENT_OPENED,
                    fragment=FragmentMessage(
                        str(config.output_directory / "boot-ba1b2c3d4-000001.partial.mp4"),
                        60_000_000_000,
                        60_000_000_000,
                        FragmentMediaContract(2, None),
                    ),
                ),
                BusMessage(
                    BusMessageKind.FRAGMENT_FINALIZED,
                    fragment=FragmentMessage(
                        str(config.output_directory / "boot-ba1b2c3d4-000001.partial.mp4"),
                        120_000_000_000,
                        60_000_000_000,
                        successor_contract,
                    ),
                ),
                BusMessage(BusMessageKind.ERROR, "test complete"),
            ]
        )
        observations = iter(
            (
                AudioDiscoveryOutcome(AudioDiscoveryStatus.NOT_FOUND),
                AudioDiscoveryOutcome(AudioDiscoveryStatus.NOT_FOUND),
            )
        )
        slept: list[float] = []

        async def no_wait(delay: float) -> None:
            slept.append(delay)

        backend = GStreamerBackend(
            output=config,
            audio_plan=audio_plan(),
            driver=driver,
            sleep=no_wait,
            enable_audio_loss_isolation=True,
        )

        def discover_after_arm() -> AudioDiscoveryOutcome:
            assert driver.arm_loss_calls == ["audio_source"]
            assert driver.handoff_calls == [20.0]
            return next(observations)

        backend.bind_audio_loss_probe(discover_after_arm)
        await backend.start(VideoProfile())
        assert driver.descriptions == [build_audio_pipeline_description(audio_plan())]
        with pytest.raises(RecoverablePipelineError, match="test complete"):
            await backend.run(asyncio.Event())

        assert backend.audio_loss_isolated is True
        assert backend.effective_audio_caps is None
        assert driver.arm_loss_calls == ["audio_source"]
        assert driver.handoff_calls == [20.0]
        assert slept == [0.5]
        loss_snapshot = cast(
            dict[str, object],
            backend.audio_restoration_snapshot["last_loss_handoff"],
        )
        assert cast(dict[str, object], loss_snapshot["forced_idr"])["edge_skew_ns"] == 50_000_000
        assert (await backend.next_finalized_fragment()).media_contract == old_contract
        assert (await backend.next_finalized_fragment()).media_contract == successor_contract
        driver.messages.extend(
            (
                BusMessage(
                    BusMessageKind.AUDIO_ERROR,
                    "late exact-source disconnect",
                    source_name="audio_source",
                ),
                BusMessage(BusMessageKind.EOS),
            )
        )
        await backend.stop()

    run_async(scenario())


def test_forced_idr_proof_accepts_last_nanosecond_and_rejects_bound() -> None:
    assert forced_idr_proof(edge_skew_ns=99_999_999).edge_skew_ns == 99_999_999
    with pytest.raises(ValueError, match="forced-IDR proof"):
        forced_idr_proof(edge_skew_ns=100_000_000)
    proof = forced_idr_proof()
    for invalid in (
        {"request_count": 2**32},
        {"seqnum_preserved": 0},
        {"all_headers": 1},
        {"nal5": 1},
        {"request_monotonic_ns": 2**64 - 1},
    ):
        with pytest.raises(ValueError, match="forced-IDR proof"):
            replace(proof, **invalid)  # type: ignore[arg-type]


def test_post_cut_discovery_exception_remains_video_only_without_pipeline_restart() -> None:
    async def scenario() -> None:
        driver = FakeDriver(
            messages=[
                BusMessage(
                    BusMessageKind.AUDIO_ERROR,
                    "microphone stream failed",
                    source_name="audio_source",
                ),
                BusMessage(BusMessageKind.ERROR, "test complete"),
            ]
        )
        probe_calls = 0

        def failed_probe() -> AudioDiscoveryOutcome:
            nonlocal probe_calls
            probe_calls += 1
            if probe_calls == 1:
                return AudioDiscoveryOutcome(
                    AudioDiscoveryStatus.MATCHED,
                    reconnect_device(),
                )
            raise OSError("udev temporarily unavailable")

        async def no_wait(_delay: float) -> None:
            return None

        backend = GStreamerBackend(
            output=output_config(),
            audio_plan=audio_plan(),
            driver=driver,
            sleep=no_wait,
            enable_audio_loss_isolation=True,
        )
        backend.bind_audio_loss_probe(failed_probe)
        await backend.start(VideoProfile())
        with pytest.raises(RecoverablePipelineError, match="test complete"):
            await backend.run(asyncio.Event())

        assert probe_calls == 2
        assert backend.audio_loss_isolated is True
        assert driver.handoff_calls == [20.0]
        assert driver.playing_timeouts == [15.0]
        assert driver.null_timeouts == []
        snapshot = backend.audio_restoration_snapshot
        assert snapshot["loss_classification"] == "audio_discovery_degraded"
        observations = cast(list[dict[str, object]], snapshot["loss_observations"])
        assert [item["status"] for item in observations] == ["MATCHED", "probe_error"]
        driver.messages.append(BusMessage(BusMessageKind.EOS))
        await backend.stop()

    run_async(scenario())


@pytest.mark.parametrize(
    "second_status",
    [
        AudioDiscoveryStatus.MATCHED,
        AudioDiscoveryStatus.AMBIGUOUS,
        AudioDiscoveryStatus.REFUSED,
    ],
)
def test_audio_loss_keeps_video_only_for_post_cut_nonabsence_classification(
    second_status: AudioDiscoveryStatus,
) -> None:
    async def scenario() -> None:
        driver = FakeDriver(
            messages=[
                BusMessage(
                    BusMessageKind.AUDIO_ERROR,
                    "microphone disconnected",
                    source_name="audio_source",
                ),
                BusMessage(BusMessageKind.ERROR, "test complete"),
            ]
        )
        outcomes = iter(
            (
                AudioDiscoveryOutcome(AudioDiscoveryStatus.NOT_FOUND),
                (
                    AudioDiscoveryOutcome(second_status)
                    if second_status is not AudioDiscoveryStatus.MATCHED
                    else AudioDiscoveryOutcome(
                        AudioDiscoveryStatus.MATCHED,
                        reconnect_device(),
                    )
                ),
            )
        )

        async def no_wait(_delay: float) -> None:
            return None

        backend = GStreamerBackend(
            output=output_config(),
            audio_plan=audio_plan(),
            driver=driver,
            sleep=no_wait,
            enable_audio_loss_isolation=True,
        )
        backend.bind_audio_loss_probe(lambda: next(outcomes))
        await backend.start(VideoProfile())
        with pytest.raises(RecoverablePipelineError, match="test complete"):
            await backend.run(asyncio.Event())
        assert backend.audio_loss_isolated is True
        assert driver.arm_loss_calls == ["audio_source"]
        assert driver.handoff_calls == [20.0]
        snapshot = backend.audio_restoration_snapshot
        assert snapshot["loss_classification"] == "audio_discovery_degraded"
        driver.messages.append(BusMessage(BusMessageKind.EOS))
        await backend.stop()

    run_async(scenario())


def test_audio_loss_arm_failure_precedes_disappearance_confirmation() -> None:
    async def scenario() -> None:
        driver = FakeDriver(
            messages=[
                BusMessage(
                    BusMessageKind.AUDIO_ERROR,
                    "microphone disconnected",
                    source_name="audio_source",
                )
            ],
            arm_loss_error=GStreamerDriverError("preexisting EOS cannot arm"),
        )
        discovery_calls = 0

        def discovery() -> AudioDiscoveryOutcome:
            nonlocal discovery_calls
            discovery_calls += 1
            return AudioDiscoveryOutcome(AudioDiscoveryStatus.NOT_FOUND)

        backend = GStreamerBackend(
            output=output_config(),
            audio_plan=audio_plan(),
            driver=driver,
            enable_audio_loss_isolation=True,
        )
        backend.bind_audio_loss_probe(discovery)
        await backend.start(VideoProfile())

        with pytest.raises(
            RecoverablePipelineError,
            match="containment arm failed: preexisting EOS cannot arm",
        ):
            await backend.run(asyncio.Event())

        assert driver.arm_loss_calls == ["audio_source"]
        assert discovery_calls == 0
        assert driver.handoff_calls == []
        assert backend.audio_loss_isolated is False
        driver.messages.append(BusMessage(BusMessageKind.EOS))
        await backend.stop()

    run_async(scenario())


def test_audio_handoff_timeout_fails_without_claiming_video_only() -> None:
    async def scenario() -> None:
        driver = FakeDriver(
            messages=[
                BusMessage(
                    BusMessageKind.AUDIO_ERROR,
                    "microphone disconnected",
                    source_name="audio_source",
                )
            ],
            handoff_error=GStreamerDriverError("successor state timeout"),
        )

        async def no_wait(_delay: float) -> None:
            return None

        backend = GStreamerBackend(
            output=output_config(),
            audio_plan=audio_plan(),
            driver=driver,
            sleep=no_wait,
            enable_audio_loss_isolation=True,
        )
        backend.bind_audio_loss_probe(lambda: AudioDiscoveryOutcome(AudioDiscoveryStatus.NOT_FOUND))
        await backend.start(VideoProfile())
        with pytest.raises(RecoverablePipelineError, match="successor state timeout"):
            await backend.run(asyncio.Event())
        assert backend.audio_loss_isolated is False
        driver.messages.append(BusMessage(BusMessageKind.EOS))
        await backend.stop()

    run_async(scenario())


@pytest.mark.parametrize(
    "phase",
    [
        "routed",
        "retiring_eos",
        "media_proof",
        "state_convergence",
        "continuity",
        "retired_closure",
        "recycle",
        "identity",
    ],
)
def test_target_driver_classifies_every_post_route_restore_failure_as_critical(
    phase: str,
) -> None:
    driver = PyGObjectGStreamerDriver(object())
    failure = GStreamerDriverError(f"injected {phase} failure")

    classified = driver._classify_restoration_failure(phase, failure)

    assert isinstance(classified, AudioRestorationCriticalError)
    assert classified.phase == phase
    assert phase in str(classified)
    assert driver._classify_restoration_failure("pre_route", failure) is failure


def test_enabled_restoration_requires_two_stable_matches_and_preserves_resources() -> None:
    async def scenario() -> None:
        driver = FakeDriver(
            messages=[
                BusMessage(
                    BusMessageKind.AUDIO_ERROR,
                    "microphone disconnected",
                    source_name="audio_source",
                )
            ],
            idle_poll_delay_s=0.002,
        )
        matched_at_new_index = AudioDiscoveryOutcome(
            AudioDiscoveryStatus.MATCHED,
            reconnect_device(card=9),
        )
        outcomes = iter(
            (
                AudioDiscoveryOutcome(AudioDiscoveryStatus.NOT_FOUND),
                AudioDiscoveryOutcome(AudioDiscoveryStatus.NOT_FOUND),
                matched_at_new_index,
                matched_at_new_index,
            )
        )

        async def no_wait(_delay: float) -> None:
            return None

        backend = GStreamerBackend(
            output=output_config(),
            audio_plan=audio_plan(),
            driver=driver,
            limits=GStreamerLimits(
                audio_restore_poll_interval_s=0.1,
                audio_restore_campaign_cooldown_s=0.1,
            ),
            sleep=no_wait,
            enable_audio_loss_isolation=True,
            enable_audio_restoration=True,
        )
        backend.bind_audio_loss_probe(lambda: next(outcomes))
        await backend.start(VideoProfile())
        stop = asyncio.Event()
        task = asyncio.create_task(backend.run(stop))
        deadline = asyncio.get_running_loop().time() + 2.0
        while backend.audio_restoration_snapshot["restoration_count"] != 1:
            if asyncio.get_running_loop().time() >= deadline:
                pytest.fail("bounded restoration did not complete")
            await asyncio.sleep(0.01)
        stop.set()
        await task

        assert len(driver.restore_calls) == 1
        restored_plan, timeout_s = driver.restore_calls[0]
        assert restored_plan.endpoint == "hw:9,0,0"
        assert timeout_s == 20.0
        snapshot = backend.audio_restoration_snapshot
        assert snapshot["restoration_enabled"] is True
        assert snapshot["topology_observation"] == "stable"
        assert snapshot["topology_observation_stale"] is False
        assert snapshot["topology_observed_monotonic_ns"] == 123
        assert snapshot["state"] == AudioBranchState.ACTIVE.value
        assert snapshot["active_slot_id"] == 1
        assert snapshot["active_activation_id"] == 3
        assert snapshot["slot_count"] == 3
        assert snapshot["loss_count"] == snapshot["restoration_count"] == 1
        assert snapshot["request_pad_invariant"] == "constant_preallocated"
        assert snapshot["request_pad_counts_measured"] is True
        assert snapshot["request_pad_peer_ownership_proven"] is True
        assert snapshot["request_pad_counts"] == {
            "video_tee": 4,
            "audio_tee": 1,
            "splitmux_video": 3,
            "splitmux_audio": 1,
        }
        assert snapshot["matched_endpoint"] == "hw:9,0,0"
        assert backend.audio_loss_isolated is False
        assert backend.effective_audio_caps == driver.audio_caps
        driver.messages.append(BusMessage(BusMessageKind.EOS))
        await backend.stop()

    run_async(scenario())


def test_public_restoration_snapshot_preserves_topology_transition_marker() -> None:
    async def scenario() -> None:
        driver = FakeDriver()
        backend = GStreamerBackend(
            output=output_config(),
            audio_plan=audio_plan(),
            driver=driver,
            enable_audio_loss_isolation=True,
            enable_audio_restoration=True,
        )
        await backend.start(VideoProfile())
        driver.topology_snapshot.update(
            {
                "topology_observation": "handoff_in_progress",
                "topology_observation_stale": True,
                "topology_observed_monotonic_ns": 123,
            }
        )

        snapshot = backend.audio_restoration_snapshot

        assert snapshot["topology_observation"] == "handoff_in_progress"
        assert snapshot["topology_observation_stale"] is True
        assert snapshot["topology_observed_monotonic_ns"] == 123
        driver.messages.append(BusMessage(BusMessageKind.EOS))
        await backend.stop()

    run_async(scenario())


def test_restore_failure_keeps_video_only_and_retries_later() -> None:
    async def scenario() -> None:
        driver = FakeDriver(
            messages=[
                BusMessage(
                    BusMessageKind.AUDIO_ERROR,
                    "microphone disconnected",
                    source_name="audio_source",
                )
            ],
            restore_error=GStreamerDriverError("slot cleanup not yet quiet"),
            idle_poll_delay_s=0.002,
        )
        match = AudioDiscoveryOutcome(
            AudioDiscoveryStatus.MATCHED,
            reconnect_device(),
        )
        outcomes = iter(
            (
                AudioDiscoveryOutcome(AudioDiscoveryStatus.NOT_FOUND),
                AudioDiscoveryOutcome(AudioDiscoveryStatus.NOT_FOUND),
                match,
                match,
                match,
                match,
            )
        )

        async def no_wait(_delay: float) -> None:
            return None

        backend = GStreamerBackend(
            output=output_config(),
            audio_plan=audio_plan(),
            driver=driver,
            limits=GStreamerLimits(
                audio_restore_poll_interval_s=0.1,
                audio_restore_campaign_cooldown_s=0.1,
            ),
            sleep=no_wait,
            enable_audio_loss_isolation=True,
            enable_audio_restoration=True,
        )
        backend.bind_audio_loss_probe(lambda: next(outcomes))
        await backend.start(VideoProfile())
        stop = asyncio.Event()
        task = asyncio.create_task(backend.run(stop))
        deadline = asyncio.get_running_loop().time() + 2.0
        while len(driver.restore_calls) < 2:
            if asyncio.get_running_loop().time() >= deadline:
                pytest.fail("restoration cleanup failure was not retried")
            if len(driver.restore_calls) == 1:
                driver.restore_error = None
            await asyncio.sleep(0.01)
        while backend.audio_restoration_snapshot["restoration_count"] != 1:
            if asyncio.get_running_loop().time() >= deadline:
                pytest.fail("restoration retry did not complete")
            await asyncio.sleep(0.01)
        stop.set()
        await task
        assert len(driver.restore_calls) == 2
        assert backend.audio_restoration_snapshot["loss_count"] == 1
        assert backend.audio_restoration_snapshot["restoration_count"] == 1
        driver.messages.append(BusMessage(BusMessageKind.EOS))
        await backend.stop()

    run_async(scenario())


def test_post_route_restore_failure_is_terminal_not_optional_audio_retry() -> None:
    async def scenario() -> None:
        driver = FakeDriver(
            messages=[
                BusMessage(
                    BusMessageKind.AUDIO_ERROR,
                    "microphone disconnected",
                    source_name="audio_source",
                )
            ],
            restore_error=AudioRestorationCriticalError(
                "media proof failed",
                phase="media_proof",
            ),
            idle_poll_delay_s=0.002,
        )
        match = AudioDiscoveryOutcome(
            AudioDiscoveryStatus.MATCHED,
            reconnect_device(),
        )
        outcomes = iter(
            (
                AudioDiscoveryOutcome(AudioDiscoveryStatus.NOT_FOUND),
                AudioDiscoveryOutcome(AudioDiscoveryStatus.NOT_FOUND),
                match,
                match,
            )
        )

        async def no_wait(_delay: float) -> None:
            return None

        backend = GStreamerBackend(
            output=output_config(),
            audio_plan=audio_plan(),
            driver=driver,
            limits=GStreamerLimits(
                audio_restore_poll_interval_s=0.1,
                audio_restore_campaign_cooldown_s=0.1,
            ),
            sleep=no_wait,
            enable_audio_loss_isolation=True,
            enable_audio_restoration=True,
        )
        backend.bind_audio_loss_probe(lambda: next(outcomes))
        await backend.start(VideoProfile())
        with pytest.raises(
            RecoverablePipelineError,
            match="without a safe video-only rollback",
        ):
            await asyncio.wait_for(backend.run(asyncio.Event()), timeout=2.0)
        assert len(driver.restore_calls) == 1
        snapshot = backend.audio_restoration_snapshot
        assert snapshot["restoration_count"] == 0
        failure = snapshot["last_failure"]
        assert isinstance(failure, dict)
        assert failure["critical"] is True
        assert failure["phase"] == "media_proof"
        assert failure["detail"] == "media proof failed"
        assert isinstance(failure["monotonic_ns"], int)
        driver.messages.append(BusMessage(BusMessageKind.EOS))
        await backend.stop()

    run_async(scenario())


def test_run_converts_unexpected_eos_to_recoverable_failure() -> None:
    async def scenario() -> None:
        driver = FakeDriver(messages=[BusMessage(BusMessageKind.EOS)])
        backend = GStreamerBackend(output=output_config(), driver=driver)
        await backend.start(VideoProfile())

        with pytest.raises(RecoverablePipelineError, match="unexpected EOS"):
            await backend.run(asyncio.Event())

        driver.messages.append(BusMessage(BusMessageKind.EOS))
        await backend.stop()

    run_async(scenario())


def test_run_emits_validated_finalized_fragment_without_stopping_pipeline() -> None:
    async def scenario() -> None:
        config = output_config(start_index=7)
        driver = FakeDriver(
            messages=[
                fragment_message(config, 7),
                BusMessage(BusMessageKind.ERROR, "stop after observation"),
            ]
        )
        backend = GStreamerBackend(output=config, driver=driver)
        await backend.start(VideoProfile())

        with pytest.raises(RecoverablePipelineError, match="stop after observation"):
            await backend.run(asyncio.Event())
        assert await asyncio.wait_for(backend.next_finalized_fragment(), timeout=0.1) == (
            FinalizedFragment(
                config.output_directory / "boot-ba1b2c3d4-000007.partial.mp4",
                7,
                60_000_000_000,
                media_contract=FragmentMediaContract(1, None, 0),
            )
        )

        driver.messages.append(BusMessage(BusMessageKind.EOS))
        await backend.stop()

    run_async(scenario())


def test_run_exposes_first_validated_fragment_opened_readiness() -> None:
    async def scenario() -> None:
        config = output_config(start_index=7)
        driver = FakeDriver(
            messages=[
                opened_message(config, 7, 123),
                opened_message(config, 8, 60_000_000_123),
                BusMessage(BusMessageKind.ERROR, "stop after opens"),
            ]
        )
        backend = GStreamerBackend(output=config, driver=driver)
        await backend.start(VideoProfile())

        with pytest.raises(RecoverablePipelineError, match="stop after opens"):
            await backend.run(asyncio.Event())
        assert await asyncio.wait_for(
            backend.wait_for_first_fragment_opened(),
            timeout=0.1,
        ) == OpenedFragment(
            config.output_directory / "boot-ba1b2c3d4-000007.partial.mp4",
            7,
            123,
            media_contract=FragmentMediaContract(1, None),
        )

        driver.messages.extend(
            (
                fragment_message(config, 8, 60_000_001_234),
                BusMessage(BusMessageKind.EOS),
            )
        )
        await backend.stop()

    run_async(scenario())


def test_run_fails_closed_for_forged_or_unconsumed_fragment_events() -> None:
    async def forged_location() -> None:
        config = output_config()
        wrong_path = (
            config.output_directory.parent / "clips" / ("boot-ba1b2c3d4-000000.partial.mp4")
        )
        driver = FakeDriver(
            messages=[
                BusMessage(
                    BusMessageKind.FRAGMENT_FINALIZED,
                    fragment=FragmentMessage(str(wrong_path), 1),
                )
            ]
        )
        backend = GStreamerBackend(output=config, driver=driver)
        await backend.start(VideoProfile())
        with pytest.raises(RecoverablePipelineError, match="escaped"):
            await backend.run(asyncio.Event())
        driver.messages.append(BusMessage(BusMessageKind.EOS))
        await backend.stop()

    async def full_queue() -> None:
        config = output_config(event_capacity=1)
        driver = FakeDriver(messages=[fragment_message(config, 0), fragment_message(config, 1)])
        backend = GStreamerBackend(output=config, driver=driver)
        await backend.start(VideoProfile())
        with pytest.raises(RecoverablePipelineError, match="queue exceeded"):
            await backend.run(asyncio.Event())
        driver.messages.append(BusMessage(BusMessageKind.EOS))
        await backend.stop()

    run_async(forged_location())
    run_async(full_queue())


def test_run_honors_an_already_requested_stop_without_polling() -> None:
    async def scenario() -> None:
        driver = FakeDriver()
        backend = GStreamerBackend(output=output_config(), driver=driver)
        await backend.start(VideoProfile())
        stop_requested = asyncio.Event()
        stop_requested.set()

        await backend.run(stop_requested)
        assert driver.poll_timeouts == []

        driver.messages.append(BusMessage(BusMessageKind.EOS))
        await backend.stop()

    run_async(scenario())


def test_stop_times_out_eos_but_still_forces_null() -> None:
    async def scenario() -> None:
        values = iter((0.0, 2.0))
        driver = FakeDriver()
        backend = GStreamerBackend(
            output=output_config(),
            driver=driver,
            limits=GStreamerLimits(eos_timeout_s=1.0),
            monotonic=lambda: next(values),
        )
        await backend.start(VideoProfile())

        with pytest.raises(GStreamerShutdownError, match="deadline"):
            await backend.stop()

        assert driver.eos_calls == 1
        assert driver.null_timeouts == [3.0]

    run_async(scenario())


def test_stop_converts_bus_error_and_is_idempotent() -> None:
    async def scenario() -> None:
        driver = FakeDriver(messages=[BusMessage(BusMessageKind.ERROR, "finalize failed")])
        backend = GStreamerBackend(output=output_config(), driver=driver)
        await backend.start(VideoProfile())

        with pytest.raises(GStreamerShutdownError, match="finalize failed"):
            await backend.stop()
        await backend.stop()

        assert driver.eos_calls == 1
        assert driver.null_timeouts == [3.0]

    run_async(scenario())


def test_stop_reports_null_transition_failure_after_eos() -> None:
    async def scenario() -> None:
        driver = FakeDriver(
            messages=[BusMessage(BusMessageKind.EOS)],
            set_null_error=RuntimeError("NULL failed"),
        )
        backend = GStreamerBackend(output=output_config(), driver=driver)
        await backend.start(VideoProfile())

        with pytest.raises(GStreamerShutdownError, match="NULL failed"):
            await backend.stop()
        assert driver.eos_calls == 1
        driver.set_null_error = None
        await backend.stop()
        assert driver.eos_calls == 1
        assert driver.null_timeouts == [3.0, 3.0]

    run_async(scenario())


def test_failed_start_rolls_directly_to_null_without_consuming_bus_error() -> None:
    async def scenario() -> None:
        driver = FakeDriver(
            set_playing_error=RuntimeError("PLAYING failed"),
            messages=[BusMessage(BusMessageKind.ERROR, "stale startup error")],
        )
        backend = GStreamerBackend(output=output_config(), driver=driver)

        with pytest.raises(RecoverablePipelineError, match="PLAYING failed"):
            await backend.start(VideoProfile())

        assert driver.eos_calls == 0
        assert driver.poll_timeouts == []
        assert driver.null_timeouts == [3.0]
        await backend.stop()

    run_async(scenario())


def test_cancelled_stop_keeps_null_cleanup_reachable() -> None:
    @dataclass
    class BlockingNullDriver(FakeDriver):
        null_entered: threading.Event = field(default_factory=threading.Event)
        release_null: threading.Event = field(default_factory=threading.Event)

        def set_null(self, pipeline: object, timeout_s: float) -> None:
            assert pipeline is self.pipeline
            self.null_timeouts.append(timeout_s)
            self.null_entered.set()
            assert self.release_null.wait(timeout=2.0)

    async def scenario() -> None:
        driver = BlockingNullDriver(messages=[BusMessage(BusMessageKind.EOS)])
        backend = GStreamerBackend(output=output_config(), driver=driver)
        await backend.start(VideoProfile())

        first_stop = asyncio.create_task(backend.stop())
        assert await asyncio.to_thread(driver.null_entered.wait, 1.0)
        first_stop.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_stop

        second_stop = asyncio.create_task(backend.stop())
        await asyncio.sleep(0)
        assert not second_stop.done()
        driver.release_null.set()
        await second_stop

        assert driver.eos_calls == 1
        assert driver.null_timeouts == [3.0]
        await backend.stop()

    run_async(scenario())


def test_stop_observes_final_fragment_before_eos_and_null() -> None:
    async def scenario() -> None:
        config = output_config(start_index=9)
        driver = FakeDriver(
            messages=[
                fragment_message(config, 9, 12_345),
                BusMessage(BusMessageKind.EOS),
            ]
        )
        backend = GStreamerBackend(output=config, driver=driver)
        await backend.start(VideoProfile())

        await backend.stop()

        assert await asyncio.wait_for(backend.next_finalized_fragment(), timeout=0.1) == (
            FinalizedFragment(
                config.output_directory / "boot-ba1b2c3d4-000009.partial.mp4",
                9,
                12_345,
                media_contract=FragmentMediaContract(1, None, 0),
            )
        )
        assert driver.eos_calls == 1
        assert driver.null_timeouts == [3.0]

    run_async(scenario())


def test_stop_does_not_accept_parent_eos_before_exact_active_closure() -> None:
    async def scenario() -> None:
        config = output_config(start_index=9)
        driver = FakeDriver(
            messages=[
                BusMessage(BusMessageKind.EOS),
                fragment_message(config, 9, 12_345),
            ]
        )
        backend = GStreamerBackend(output=config, driver=driver)
        await backend.start(VideoProfile())
        opened = backend._observe_fragment_opened(opened_message(config, 9, 0))
        assert opened.sequence == 9

        await backend.stop()

        finalized = await asyncio.wait_for(
            backend.next_finalized_fragment(),
            timeout=0.1,
        )
        assert finalized.sequence == 9
        assert driver.poll_timeouts == [0.1, 0.1]
        assert driver.null_timeouts == [3.0]

    run_async(scenario())


def test_stop_accepts_exact_active_fragment_closure_when_pipeline_omits_eos() -> None:
    async def scenario() -> None:
        config = output_config(start_index=9)
        driver = FakeDriver(
            messages=[
                opened_message(config, 9, 0),
                fragment_message(config, 9, 12_345),
            ]
        )
        backend = GStreamerBackend(output=config, driver=driver)
        await backend.start(VideoProfile())

        await backend.stop()

        assert await asyncio.wait_for(backend.next_finalized_fragment(), timeout=0.1) == (
            FinalizedFragment(
                config.output_directory / "boot-ba1b2c3d4-000009.partial.mp4",
                9,
                12_345,
                media_contract=FragmentMediaContract(1, None, 0),
            )
        )
        assert driver.eos_calls == 1
        assert driver.null_timeouts == [3.0]

    run_async(scenario())


def test_stop_rejects_stale_fragment_closure_before_active_closure() -> None:
    async def scenario() -> None:
        config = output_config(start_index=9)
        driver = FakeDriver(
            messages=[
                opened_message(config, 10, 60_000_000_000),
                fragment_message(config, 9, 60_000_000_000),
                fragment_message(config, 10, 61_000_000_000),
            ]
        )
        backend = GStreamerBackend(output=config, driver=driver)
        await backend.start(VideoProfile())

        await backend.stop()

        first = await asyncio.wait_for(backend.next_finalized_fragment(), timeout=0.1)
        second = await asyncio.wait_for(backend.next_finalized_fragment(), timeout=0.1)
        assert (first.sequence, second.sequence) == (9, 10)
        assert driver.poll_timeouts == [0.1, 0.1, 0.1]
        assert driver.null_timeouts == [3.0]

    run_async(scenario())


def test_backend_is_single_use_and_run_before_start_is_rejected() -> None:
    async def scenario() -> None:
        driver = FakeDriver()
        backend = GStreamerBackend(output=output_config(), driver=driver)
        with pytest.raises(PipelineContractError, match="not started"):
            await backend.run(asyncio.Event())

        await backend.start(VideoProfile())
        driver.messages.append(BusMessage(BusMessageKind.EOS))
        await backend.stop()
        with pytest.raises(PipelineContractError, match="single-use"):
            await backend.start(VideoProfile())

    run_async(scenario())


@pytest.mark.parametrize(
    "change",
    [
        {"start_timeout_s": 0.0},
        {"bus_poll_s": 5.1},
        {"eos_timeout_s": True},
        {"null_timeout_s": 61.0},
    ],
)
def test_limits_reject_zero_invalid_or_unbounded_values(change: dict[str, object]) -> None:
    factory = cast_limits_factory(GStreamerLimits)
    with pytest.raises(ValueError):
        factory(**change)


def cast_limits_factory(
    factory: type[GStreamerLimits],
) -> Callable[..., GStreamerLimits]:
    return factory


@dataclass
class FakeElement:
    properties: list[tuple[str, object]] = field(default_factory=list)
    overlay_texts: list[str | None] = field(default_factory=list)
    overlay_snapshot_value: dict[str, object] = field(
        default_factory=lambda: {
            "state": "ACTIVE",
            "caps_accepted": True,
            "enabled": True,
            "updates": 1,
            "frames_seen": 1,
            "frames_rendered": 1,
            "frames_passthrough": 0,
            "bytes_written": 73_728,
            "last_error": None,
        }
    )

    def set_property(self, name: str, value: object) -> None:
        self.properties.append((name, value))

    def set_overlay_text(self, text: str | None) -> None:
        self.overlay_texts.append(text)

    def overlay_snapshot(self) -> dict[str, object]:
        return dict(self.overlay_snapshot_value)


class _SyncFactoryIdentity:
    def __init__(self, name: str) -> None:
        self.name = name

    def get_name(self) -> str:
        return self.name


class _SyncConfiguredElement:
    def __init__(self, factory_name: str) -> None:
        self.factory = _SyncFactoryIdentity(factory_name)
        self.properties: dict[str, object] = {}
        if factory_name == "mp4mux":
            self.properties["fragment-duration"] = 0
            self.properties["fragment-mode"] = 0

    def get_factory(self) -> _SyncFactoryIdentity:
        return self.factory

    def set_property(self, name: str, value: object) -> None:
        self.properties[name] = value

    def get_property(self, name: str) -> object:
        return self.properties[name]


def test_sync_generation_output_binds_and_proves_explicit_fragmented_mp4() -> None:
    driver = PyGObjectGStreamerDriver(object())
    output = _SyncConfiguredElement("splitmuxsink")
    muxer = _SyncConfiguredElement("mp4mux")
    sink = _SyncConfiguredElement("filesink")
    output.properties.update(
        {
            "async-finalize": False,
            "reset-muxer": True,
            "muxer": muxer,
            "sink": sink,
        }
    )

    driver._configure_sync_generation_output(output)

    assert output.properties["async-finalize"] is False
    assert output.properties["reset-muxer"] is True
    assert muxer.properties == {
        "fragment-duration": 1000,
        "fragment-mode": 0,
    }


def test_sync_generation_output_refuses_a_foreign_sink_factory() -> None:
    driver = PyGObjectGStreamerDriver(object())
    output = _SyncConfiguredElement("splitmuxsink")
    output.properties.update(
        {
            "async-finalize": False,
            "reset-muxer": True,
            "muxer": _SyncConfiguredElement("mp4mux"),
            "sink": _SyncConfiguredElement("fakesink"),
        }
    )

    with pytest.raises(
        GStreamerDriverError,
        match="synchronous generation output contract differs",
    ):
        driver._configure_sync_generation_output(output)


@dataclass
class FakeStructure:
    name: str
    values: dict[str, object]

    def get_name(self) -> str:
        return self.name

    def get_value(self, name: str) -> object | None:
        return self.values.get(name)


@dataclass
class FakeGstMessage:
    type: int
    structure: FakeStructure | None = None
    qos_stats: tuple[object, object, object] | None = None
    src: object | None = None
    error: tuple[object, object] | None = None
    seqnum: int = 900

    def get_structure(self) -> FakeStructure | None:
        return self.structure

    def parse_qos_stats(self) -> tuple[object, object, object]:
        assert self.qos_stats is not None
        return self.qos_stats

    def parse_error(self) -> tuple[object, object]:
        assert self.error is not None
        return self.error

    def get_seqnum(self) -> int:
        return self.seqnum


@dataclass
class FakeBus:
    messages: list[FakeGstMessage]
    filters: list[tuple[int, int]] = field(default_factory=list)

    def timed_pop_filtered(self, timeout_ns: int, message_filter: int) -> FakeGstMessage | None:
        self.filters.append((timeout_ns, message_filter))
        if self.messages:
            return self.messages.pop(0)
        return None


@dataclass
class FakeGstPipeline:
    output: FakeElement
    bus: FakeBus
    elements: dict[str, FakeElement] = field(default_factory=dict)

    def get_by_name(self, name: str) -> FakeElement | None:
        return self.output if name == "output" else self.elements.get(name)

    def get_bus(self) -> FakeBus:
        return self.bus


class FakeMessageType:
    ERROR = 1
    EOS = 2
    ELEMENT = 4
    QOS = 8


class FakeFormat:
    BUFFERS = "buffers"
    TIME = "time"


@dataclass
class FakeGst:
    pipeline: FakeGstPipeline
    MessageType: type[FakeMessageType] = FakeMessageType
    Format: type[FakeFormat] = FakeFormat
    descriptions: list[str] = field(default_factory=list)

    def parse_launch(self, description: str) -> FakeGstPipeline:
        self.descriptions.append(description)
        return self.pipeline


def test_pygobject_driver_sets_output_properties_outside_parse_launch() -> None:
    output = FakeElement()
    pipeline = FakeGstPipeline(output, FakeBus([]))
    gst = FakeGst(pipeline)
    driver = PyGObjectGStreamerDriver(gst)
    config = output_config(start_index=42)

    assert driver.create_pipeline(PIPELINE_DESCRIPTION, config.location_pattern, 42) is pipeline
    assert gst.descriptions == [PIPELINE_DESCRIPTION]
    assert output.properties == [
        ("location", config.location_pattern),
        ("start-index", 42),
    ]
    assert config.location_pattern not in PIPELINE_DESCRIPTION


def test_pygobject_driver_updates_and_inspects_named_native_overlay() -> None:
    overlay = FakeElement()
    pipeline = FakeGstPipeline(
        FakeElement(),
        FakeBus([]),
        elements={"burned_overlay": overlay},
    )
    driver = PyGObjectGStreamerDriver(FakeGst(pipeline))

    driver.set_overlay_text(pipeline, "TIME UNSYNCED\nGPS INVALID")
    driver.set_overlay_text(pipeline, None)

    assert overlay.overlay_texts == ["TIME UNSYNCED\nGPS INVALID", None]
    assert driver.overlay_snapshot(pipeline) == overlay.overlay_snapshot_value

    missing = FakeGstPipeline(FakeElement(), FakeBus([]))
    with pytest.raises(GStreamerDriverError, match="no burned overlay"):
        driver.set_overlay_text(missing, "REC")


@pytest.mark.parametrize("source_name", sorted(AUDIO_BRANCH_ELEMENT_NAMES))
def test_pygobject_driver_classifies_only_exact_audio_element_error_sources(
    source_name: str,
) -> None:
    source = FakeElement()
    pipeline = FakeGstPipeline(
        FakeElement(),
        FakeBus(
            [
                FakeGstMessage(
                    FakeMessageType.ERROR,
                    src=source,
                    error=("device disconnected", "alsa debug"),
                )
            ]
        ),
        elements={source_name: source},
    )
    driver = PyGObjectGStreamerDriver(FakeGst(pipeline))

    assert driver.poll_bus(pipeline, 0.1) == BusMessage(
        BusMessageKind.AUDIO_ERROR,
        "device disconnected; debug=alsa debug",
        source_name=source_name,
    )


@pytest.mark.parametrize("source_name", ["camera", "encoder", "output", "mp4mux0"])
def test_pygobject_driver_keeps_camera_encoder_mux_and_name_impostors_critical(
    source_name: str,
) -> None:
    source = FakeElement()
    actual_audio_source = FakeElement()
    elements = {"audio_source": actual_audio_source}
    pipeline = FakeGstPipeline(
        FakeElement(),
        FakeBus(
            [
                FakeGstMessage(
                    FakeMessageType.ERROR,
                    src=source,
                    error=("critical failure", source_name),
                )
            ]
        ),
        elements=elements,
    )
    driver = PyGObjectGStreamerDriver(FakeGst(pipeline))

    assert driver.poll_bus(pipeline, 0.1) == BusMessage(
        BusMessageKind.ERROR,
        f"critical failure; debug={source_name}",
    )


def _quarantined_bus_fixture(
    messages: list[FakeGstMessage],
    *,
    current_source: object | None = None,
    quarantine_source: object | None = None,
    extra_elements: dict[str, FakeElement] | None = None,
) -> tuple[
    PyGObjectGStreamerDriver,
    FakeGstPipeline,
    _AudioIngressQuarantine,
]:
    source = FakeElement() if current_source is None else current_source
    quarantined = source if quarantine_source is None else quarantine_source
    ingress = object()
    pipeline = FakeGstPipeline(
        FakeElement(),
        FakeBus(messages),
        elements={"audio_source": cast(FakeElement, source), **(extra_elements or {})},
    )
    generation = _driver_generation(1)
    generation.activation_id = 1
    quarantine = _AudioIngressQuarantine(ingress, quarantined, 1)
    context = _GenerationPipeline(
        pipeline,
        object(),
        "/srv/dashcam/pending",
        "abcdef123456",
        0,
        object(),
        object(),
        object(),
        object(),
        {1: generation},
        threading.Lock(),
        {},
        deque(),
        audio_ingress_bin=ingress,
        audio_ingress_elements=MappingProxyType({"audio_source": source}),
        audio_ingress_quarantine=quarantine,
    )
    driver = PyGObjectGStreamerDriver(FakeGst(pipeline))
    driver._generation_pipelines[id(pipeline)] = context
    return driver, pipeline, quarantine


def _expected_retiring_audio_error(source: object) -> FakeGstMessage:
    return FakeGstMessage(
        FakeMessageType.ERROR,
        src=source,
        error=(
            "gst-stream-error-quark: Internal data stream error. (1)",
            "/GstPipeline:pipeline0/GstBin:audio_ingress/"
            "GstAlsaSrc:audio_source: streaming stopped, reason error (-5)",
        ),
    )


def test_exact_retiring_alsa_error_quarantine_is_bounded() -> None:
    source = FakeElement()
    messages = [_expected_retiring_audio_error(source) for _ in range(5)]
    driver, pipeline, quarantine = _quarantined_bus_fixture(
        messages,
        current_source=source,
    )

    for expected_count in range(1, 5):
        assert driver.poll_bus(pipeline, 0.1).kind is BusMessageKind.NONE
        assert quarantine.error_count == expected_count
    with pytest.raises(GStreamerDriverError, match="error burst exceeded"):
        driver.poll_bus(pipeline, 0.1)


def test_quarantine_keeps_wrong_shape_and_other_audio_element_errors_fatal() -> None:
    source = FakeElement()
    parser = FakeElement()
    messages = [
        FakeGstMessage(
            FakeMessageType.ERROR,
            src=source,
            error=("different source failure", "not the proven ALSA stop shape"),
        ),
        FakeGstMessage(
            FakeMessageType.ERROR,
            src=parser,
            error=("parser failure", "unrelated audio parser"),
        ),
    ]
    driver, pipeline, quarantine = _quarantined_bus_fixture(
        messages,
        current_source=source,
        extra_elements={"audio_parser": parser},
    )

    first = driver.poll_bus(pipeline, 0.1)
    second = driver.poll_bus(pipeline, 0.1)

    assert first.kind is BusMessageKind.ERROR
    assert second.kind is BusMessageKind.ERROR
    assert quarantine.error_count == 0


def test_quarantine_refuses_a_stale_source_after_identity_replacement() -> None:
    current_source = FakeElement()
    stale_source = FakeElement()
    driver, pipeline, _quarantine = _quarantined_bus_fixture(
        [_expected_retiring_audio_error(stale_source)],
        current_source=current_source,
        quarantine_source=stale_source,
    )

    with pytest.raises(GStreamerDriverError, match="ownership drifted"):
        driver.poll_bus(pipeline, 0.1)


@pytest.mark.parametrize("source_name", sorted(AUDIO_BRANCH_ELEMENT_NAMES))
def test_context_classification_uses_retained_wrapper_not_fresh_name_lookup(
    source_name: str,
) -> None:
    retained = FakeElement()
    lookalike = FakeElement()
    driver, pipeline, _quarantine = _quarantined_bus_fixture([])
    context = driver._generation_pipelines[id(pipeline)]
    context.audio_ingress_quarantine = None
    context.audio_ingress_elements = MappingProxyType({source_name: retained})
    pipeline.elements[source_name] = lookalike
    pipeline.bus.messages.extend(
        [
            FakeGstMessage(
                FakeMessageType.ERROR,
                src=retained,
                error=("retained failure", source_name),
            ),
            FakeGstMessage(
                FakeMessageType.ERROR,
                src=lookalike,
                error=("lookalike failure", source_name),
            ),
        ]
    )

    assert driver.poll_bus(pipeline, 0.1) == BusMessage(
        BusMessageKind.AUDIO_ERROR,
        f"retained failure; debug={source_name}",
        source_name=source_name,
    )
    assert driver.poll_bus(pipeline, 0.1) == BusMessage(
        BusMessageKind.ERROR,
        f"lookalike failure; debug={source_name}",
    )


def test_context_retains_audio_source_wrapper_across_garbage_collection() -> None:
    source = FakeElement()
    source_ref = weakref.ref(source)
    driver, pipeline, quarantine = _quarantined_bus_fixture(
        [],
        current_source=source,
    )
    context = driver._generation_pipelines[id(pipeline)]
    context.audio_ingress_quarantine = None
    pipeline.elements["audio_source"] = FakeElement()
    del source
    del quarantine
    gc.collect()

    retained = source_ref()
    assert retained is context.audio_ingress_elements["audio_source"]
    assert retained is not None
    pipeline.bus.messages.append(
        FakeGstMessage(
            FakeMessageType.ERROR,
            src=retained,
            error=("retained failure", "after gc"),
        )
    )
    assert driver.poll_bus(pipeline, 0.1) == BusMessage(
        BusMessageKind.AUDIO_ERROR,
        "retained failure; debug=after gc",
        source_name="audio_source",
    )


def test_audio_error_classification_trace_binds_current_source_and_ingress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes: list[bytes] = []
    source = FakeElement()
    driver, pipeline, _quarantine = _quarantined_bus_fixture(
        [
            FakeGstMessage(
                FakeMessageType.ERROR,
                src=source,
                error=("device disconnected", "alsa debug"),
            )
        ],
        current_source=source,
    )
    context = driver._generation_pipelines[id(pipeline)]
    context.audio_ingress_quarantine = None
    monkeypatch.setenv("DASHCAM_HANDOFF_TRACE", "1")

    def capture_write(_fd: int, value: bytes) -> int:
        writes.append(value)
        return len(value)

    monkeypatch.setattr(os, "write", capture_write)

    assert driver.poll_bus(pipeline, 0.1).kind is BusMessageKind.AUDIO_ERROR

    trace = b"".join(writes)
    assert b"dashcam-handoff-phase=audio_error_source_observed" in trace
    assert b"current_exact=1" in trace
    assert b"fresh_lookup_matches_retained=1" in trace
    assert b"message_source_matches_fresh=1" in trace
    assert b"dashcam-handoff-phase=audio_error_classified" in trace


def test_quarantine_remains_exact_after_retired_av_slot_is_recycled() -> None:
    source = FakeElement()
    driver, pipeline, quarantine = _quarantined_bus_fixture(
        [_expected_retiring_audio_error(source)],
        current_source=source,
    )
    context = driver._generation_pipelines[id(pipeline)]
    generation = context.generations[1]
    generation.activation_id = None
    generation.linked = False
    generation.reusable = True
    context.active_generation_id = 2
    active = _driver_generation(2, linked=True)
    active.activation_id = 2
    context.generations[2] = active
    context.isolated = True

    assert driver.poll_bus(pipeline, 0.1).kind is BusMessageKind.NONE
    assert quarantine.error_count == 1


def test_exact_retiring_source_bus_eos_is_bounded_without_hiding_parent_eos() -> None:
    source = FakeElement()
    driver, pipeline, quarantine = _quarantined_bus_fixture(
        [
            FakeGstMessage(FakeMessageType.EOS, src=source),
            FakeGstMessage(FakeMessageType.EOS, src=source),
        ],
        current_source=source,
    )

    assert driver.poll_bus(pipeline, 0.1).kind is BusMessageKind.NONE
    assert quarantine.eos_count == 1
    with pytest.raises(GStreamerDriverError, match="EOS exceeded"):
        driver.poll_bus(pipeline, 0.1)

    parent_pipeline = FakeGstPipeline(
        FakeElement(),
        FakeBus([FakeGstMessage(FakeMessageType.EOS)]),
    )
    parent_driver = PyGObjectGStreamerDriver(FakeGst(parent_pipeline))
    assert parent_driver.poll_bus(parent_pipeline, 0.1).kind is BusMessageKind.EOS


@pytest.mark.parametrize("source_kind", ["retiring_output", "parent_pipeline"])
def test_unquarantined_eos_stays_fatal_and_traces_exact_generation_ownership(
    source_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes: list[bytes] = []
    pipeline = FakeGstPipeline(FakeElement(), FakeBus([]))
    active = _driver_generation(1, has_audio=True, linked=True)
    active.activation_id = 5
    active.video_units = 31
    active.audio_units = 2
    retiring = _driver_generation(
        3,
        output=object(),
        generation_bin=object(),
    )
    retiring.activation_id = 4
    retiring.reusable = False
    retiring.video_retirement_eos_sent = True
    retiring.opened["retiring.partial.mp4"] = 1
    source = retiring.output if source_kind == "retiring_output" else pipeline
    pipeline.bus.messages.append(FakeGstMessage(FakeMessageType.EOS, src=source))
    context = _GenerationPipeline(
        pipeline,
        object(),
        "/srv/dashcam/pending",
        "abcdef123456",
        0,
        object(),
        object(),
        object(),
        object(),
        {1: active, 3: retiring},
        threading.Lock(),
        {},
        deque(),
        active_generation_id=1,
        next_activation_id=6,
        isolated=True,
        routing_phase="AV_RESTORING",
        audio_ingress_replacement_count=2,
    )
    driver = PyGObjectGStreamerDriver(FakeGst(pipeline))
    driver._generation_pipelines[id(pipeline)] = context
    monkeypatch.setenv("DASHCAM_HANDOFF_TRACE", "1")

    def capture_write(_fd: int, value: bytes) -> int:
        writes.append(value)
        return len(value)

    monkeypatch.setattr(os, "write", capture_write)

    assert driver.poll_bus(pipeline, 0.1).kind is BusMessageKind.EOS

    trace = b"".join(writes)
    assert b"dashcam-handoff-phase=eos_message_source_observed" in trace
    assert b"active_activation_id=5" in trace
    assert b"active_slot_id=1" in trace
    assert b"message_seqnum=900" in trace
    assert b"routing_phase=4" in trace
    if source_kind == "retiring_output":
        assert b"parent_pipeline_exact=0" in trace
        assert b"source_role=2" in trace
        assert b"source_slot_activation_id=4" in trace
        assert b"source_slot_id=3" in trace
        assert b"source_slot_linked=0" in trace
        assert b"source_slot_video_eos_sent=1" in trace
    else:
        assert b"parent_pipeline_exact=1" in trace
        assert b"source_role=0" in trace
        assert b"source_slot_id=-1" in trace


def test_pygobject_driver_parses_only_complete_fragment_closed_messages() -> None:
    config = output_config()
    path = str(config.output_directory / "boot-ba1b2c3d4-000000.partial.mp4")
    bus = FakeBus(
        [
            FakeGstMessage(
                FakeMessageType.ELEMENT,
                FakeStructure(
                    "splitmuxsink-fragment-opened",
                    {"location": path, "running-time": 0},
                ),
            ),
            FakeGstMessage(
                FakeMessageType.ELEMENT,
                FakeStructure(
                    "splitmuxsink-fragment-closed",
                    {"location": path, "running-time": 1234},
                ),
            ),
        ]
    )
    pipeline = FakeGstPipeline(FakeElement(), bus)
    driver = PyGObjectGStreamerDriver(FakeGst(pipeline))

    assert driver.poll_bus(pipeline, 0.25) == opened_message(config, 0, 0)
    assert driver.poll_bus(pipeline, 0.25) == fragment_message(config, 0, 1234)
    assert bus.filters == [(250_000_000, 15), (250_000_000, 15)]


def test_pygobject_driver_rejects_incomplete_fragment_closed_message() -> None:
    bus = FakeBus(
        [
            FakeGstMessage(
                FakeMessageType.ELEMENT,
                FakeStructure(
                    "splitmuxsink-fragment-closed",
                    {"location": "clip.mp4"},
                ),
            )
        ]
    )
    pipeline = FakeGstPipeline(FakeElement(), bus)
    driver = PyGObjectGStreamerDriver(FakeGst(pipeline))

    with pytest.raises(GStreamerDriverError, match="running-time"):
        driver.poll_bus(pipeline, 0.1)


def test_pygobject_driver_qos_counts_cumulative_drops_once_and_rejects_regression() -> None:
    pipeline = FakeGstPipeline(
        FakeElement(),
        FakeBus(
            [
                FakeGstMessage(FakeMessageType.QOS, qos_stats=(FakeFormat.BUFFERS, 10, 2)),
                FakeGstMessage(FakeMessageType.QOS, qos_stats=(FakeFormat.BUFFERS, 20, 5)),
                FakeGstMessage(FakeMessageType.QOS, qos_stats=(FakeFormat.BUFFERS, 30, 4)),
            ]
        ),
    )
    driver = PyGObjectGStreamerDriver(FakeGst(pipeline))
    counters = PipelineCounters()
    driver._metrics[id(pipeline)] = (counters, 0)

    assert driver.poll_bus(pipeline, 0.1).kind is BusMessageKind.NONE
    assert driver.poll_bus(pipeline, 0.1).kind is BusMessageKind.NONE
    assert counters.snapshot().dropped_frames == 5
    with pytest.raises(GStreamerDriverError, match="regressed"):
        driver.poll_bus(pipeline, 0.1)


def test_pygobject_driver_ignores_non_buffer_qos_without_claiming_drop_metrics() -> None:
    pipeline = FakeGstPipeline(
        FakeElement(),
        FakeBus([FakeGstMessage(FakeMessageType.QOS, qos_stats=(FakeFormat.TIME, 1, 9))]),
    )
    driver = PyGObjectGStreamerDriver(FakeGst(pipeline))
    counters = PipelineCounters()
    driver._metrics[id(pipeline)] = (counters, 0)

    assert driver.poll_bus(pipeline, 0.1).kind is BusMessageKind.NONE
    assert counters.snapshot().dropped_frames is None


def _driver_generation(
    generation_id: int,
    *,
    has_audio: bool = False,
    linked: bool = False,
    valve: object | None = None,
    queue: object | None = None,
    output: object | None = None,
    output_pad: object | None = None,
    tee_pad: object | None = None,
    generation_bin: object | None = None,
) -> _RecordingGeneration:
    placeholder = object()
    return _RecordingGeneration(
        generation_id=generation_id,
        has_audio=has_audio,
        bin=placeholder if generation_bin is None else generation_bin,
        output=placeholder if output is None else output,
        video_valve=placeholder if valve is None else valve,
        video_queue=placeholder if queue is None else queue,
        video_ghost=placeholder,
        video_tee_pad=placeholder if tee_pad is None else tee_pad,
        output_video_pad=placeholder if output_pad is None else output_pad,
        linked=linked,
    )


class _TopologyIterator:
    def __init__(self, values: list[object]) -> None:
        self.values = iter(values)

    def next(self) -> tuple[str, object | None]:
        try:
            return ("OK", next(self.values))
        except StopIteration:
            return ("DONE", None)


class _TopologyGst:
    class IteratorResult:
        OK = "OK"
        DONE = "DONE"

    class State:
        PLAYING = "PLAYING"
        VOID_PENDING = "VOID_PENDING"

    class StateChangeReturn:
        SUCCESS = "SUCCESS"
        FAILURE = "FAILURE"


class _TopologyPad:
    def __init__(self, peer: object | None = None) -> None:
        self.peer = peer

    def get_peer(self) -> object | None:
        return self.peer

    def unlink(self, peer: object) -> bool:
        if self.peer is not peer:
            return False
        self.peer = None
        if isinstance(peer, _TopologyPad) and peer.peer is self:
            peer.peer = None
        return True


class _TopologyFactory:
    def __init__(self, name: str) -> None:
        self.name = name

    def get_name(self) -> str:
        return self.name


class _TopologyElement:
    def __init__(
        self,
        name: str,
        parent: object | None = None,
        *,
        factory_name: str | None = None,
    ) -> None:
        self.name = name
        self.parent = parent
        self.factory = _TopologyFactory(
            "capsfilter" if name.startswith("capsfilter") else (factory_name or "identity")
        )
        self.src_pads: list[object] = []
        self.sink_pads: list[object] = []
        self.descendants: list[object] = []
        self.static_pads: dict[str, object] = {}
        self.properties: dict[str, object] = {}
        self.state: tuple[object, object, object] = (
            _TopologyGst.StateChangeReturn.SUCCESS,
            _TopologyGst.State.PLAYING,
            _TopologyGst.State.VOID_PENDING,
        )
        self.retain_removed_descendant = False

    def get_name(self) -> str:
        return self.name

    def get_parent(self) -> object | None:
        return self.parent

    def get_factory(self) -> _TopologyFactory:
        return self.factory

    def iterate_src_pads(self) -> _TopologyIterator:
        return _TopologyIterator(self.src_pads)

    def iterate_sink_pads(self) -> _TopologyIterator:
        return _TopologyIterator(self.sink_pads)

    def iterate_recurse(self) -> _TopologyIterator:
        return _TopologyIterator(self.descendants)

    def get_static_pad(self, name: str) -> object | None:
        return self.static_pads.get(name)

    def get_property(self, name: str) -> object:
        return self.properties[name]

    def get_state(self, _timeout: int) -> tuple[object, object, object]:
        return self.state

    def get_by_name(self, name: str) -> object | None:
        return next(
            (
                candidate
                for candidate in self.descendants
                if isinstance(candidate, _TopologyElement) and candidate.get_name() == name
            ),
            None,
        )

    def remove(self, element: object) -> bool:
        if element not in self.descendants:
            return False
        retired = {id(element)}
        for candidate in self.descendants:
            parent = candidate.get_parent()  # type: ignore[attr-defined]
            if parent is element:
                retired.add(id(candidate))
        retained_identity = None
        if self.retain_removed_descendant:
            retained_identity = next(
                (identity for identity in retired if identity != id(element)),
                None,
            )
        self.descendants = [
            candidate
            for candidate in self.descendants
            if id(candidate) not in retired or id(candidate) == retained_identity
        ]
        if isinstance(element, _TopologyElement):
            element.parent = None
        return True


class _TopologyDriver(PyGObjectGStreamerDriver):
    def _set_and_verify_state(
        self,
        pipeline: object,
        state_name: str,
        timeout_s: float,
    ) -> None:
        assert state_name in {"NULL", "PLAYING"}
        assert timeout_s == 1.0

    def _install_audio_ingress(
        self,
        context: _GenerationPipeline,
        plan: AudioCapturePlan,
        *,
        synchronize: bool,
        bind_metrics: bool,
    ) -> None:
        assert synchronize is True
        assert bind_metrics is True
        assert plan.endpoint == "hw:1,0,0"
        pipeline = cast(_TopologyElement, context.pipeline)
        ingress = _TopologyElement("audio_ingress", pipeline)
        ingress_src = _TopologyPad()
        tee_sink = cast(_TopologyPad, context.audio_tee.get_static_pad("sink"))
        ingress_src.peer = tee_sink
        tee_sink.peer = ingress_src
        ingress.static_pads["src"] = ingress_src
        children = [
            _TopologyElement(name, ingress)
            for name in [
                *sorted(AUDIO_BRANCH_ELEMENT_NAMES),
                "audio_generation_counter",
                "capsfilter-new-0",
                "capsfilter-new-1",
            ]
        ]
        pipeline.descendants.extend([ingress, *children])
        context.audio_ingress_bin = ingress
        context.audio_ingress_elements = MappingProxyType(
            {
                child.get_name(): child
                for child in children
                if child.get_name() in AUDIO_BRANCH_ELEMENT_NAMES
            }
        )
        context.audio_ingress_replacement_count += 1


def _measured_topology_fixture() -> tuple[
    PyGObjectGStreamerDriver,
    _TopologyElement,
    _GenerationPipeline,
]:
    driver = _TopologyDriver(_TopologyGst())
    pipeline = _TopologyElement("pipeline")
    video_tee = _TopologyElement("video_tee", pipeline)
    audio_tee = _TopologyElement("audio_tee", pipeline)
    continuity_queue = _TopologyElement(
        "video_continuity_queue",
        pipeline,
        factory_name="queue",
    )
    continuity_sink = _TopologyElement(
        "video_continuity_sink",
        pipeline,
        factory_name="fakesink",
    )
    continuity_queue.properties.update(
        {
            "max-size-buffers": 2,
            "max-size-bytes": 0,
            "max-size-time": 0,
            "leaky": 2,
        }
    )
    continuity_sink.properties.update(
        {
            "sync": False,
            "async": False,
            "enable-last-sample": False,
            "qos": False,
        }
    )
    continuity_tee_pad = _TopologyPad()
    continuity_queue_sink = _TopologyPad(continuity_tee_pad)
    continuity_tee_pad.peer = continuity_queue_sink
    continuity_queue_src = _TopologyPad()
    continuity_sink_pad = _TopologyPad(continuity_queue_src)
    continuity_queue_src.peer = continuity_sink_pad
    continuity_queue.static_pads.update(
        {"sink": continuity_queue_sink, "src": continuity_queue_src}
    )
    continuity_sink.static_pads["sink"] = continuity_sink_pad
    video_tee.src_pads.append(continuity_tee_pad)
    generations: dict[int, _RecordingGeneration] = {}
    for slot_id in (1, 2, 3):
        video_ghost = object()
        video_tee_pad = _TopologyPad(video_ghost if slot_id == 1 else None)
        video_output_pad = _TopologyPad()
        video_queue_src = _TopologyPad(video_output_pad)
        video_queue = _TopologyElement(f"slot_{slot_id}_video_queue")
        video_queue.static_pads["src"] = video_queue_src
        output = _TopologyElement(f"slot_{slot_id}_output")
        output.sink_pads.append(video_output_pad)
        generation = _RecordingGeneration(
            generation_id=slot_id,
            has_audio=slot_id == 1,
            bin=object(),
            output=output,
            video_valve=object(),
            video_queue=video_queue,
            video_ghost=video_ghost,
            video_tee_pad=video_tee_pad,
            output_video_pad=video_output_pad,
            linked=slot_id == 1,
            activation_id=1 if slot_id == 1 else None,
        )
        if slot_id == 1:
            audio_ghost = object()
            audio_tee_pad = _TopologyPad(audio_ghost)
            audio_output_pad = _TopologyPad()
            audio_queue_src = _TopologyPad(audio_output_pad)
            audio_queue = _TopologyElement("slot_1_audio_queue")
            audio_queue.static_pads["src"] = audio_queue_src
            output.sink_pads.append(audio_output_pad)
            generation.audio_ghost = audio_ghost
            generation.audio_tee_pad = audio_tee_pad
            generation.output_audio_pad = audio_output_pad
            generation.audio_queue = audio_queue
            audio_tee.src_pads.append(audio_tee_pad)
        video_tee.src_pads.append(video_tee_pad)
        generations[slot_id] = generation
    ingress = _TopologyElement("audio_ingress", pipeline)
    ingress_src = _TopologyPad()
    audio_tee_sink = _TopologyPad(ingress_src)
    ingress_src.peer = audio_tee_sink
    ingress.static_pads["src"] = ingress_src
    audio_tee.static_pads["sink"] = audio_tee_sink
    child_names = [
        *sorted(AUDIO_BRANCH_ELEMENT_NAMES),
        "audio_generation_counter",
        "capsfilter0",
        "capsfilter1",
    ]
    children = [_TopologyElement(name, ingress) for name in child_names]
    pipeline.descendants = [ingress, *children]
    context = _GenerationPipeline(
        pipeline,
        _TopologyGst(),
        "/srv/dashcam/pending",
        "abcdef123456",
        0,
        video_tee,
        audio_tee,
        object(),
        object(),
        generations,
        threading.Lock(),
        {},
        deque(),
        video_continuity_queue=continuity_queue,
        video_continuity_sink=continuity_sink,
        video_continuity_tee_pad=continuity_tee_pad,
        audio_ingress_bin=ingress,
        audio_ingress_elements=MappingProxyType(
            {
                child.get_name(): child
                for child in children
                if child.get_name() in AUDIO_BRANCH_ELEMENT_NAMES
            }
        ),
    )
    driver._generation_pipelines[id(pipeline)] = context
    measured = driver._measure_generation_topology(context)
    driver._publish_stable_topology(context, measured)
    return driver, pipeline, context


def test_audio_ingress_element_capture_is_complete_exact_and_immutable() -> None:
    driver = _TopologyDriver(_TopologyGst())
    ingress = _TopologyElement("audio_ingress")
    children = [_TopologyElement(name, ingress) for name in sorted(AUDIO_BRANCH_ELEMENT_NAMES)]
    ingress.descendants.extend(children)

    retained = driver._capture_audio_ingress_elements(ingress)

    assert set(retained) == AUDIO_BRANCH_ELEMENT_NAMES
    assert all(retained[child.get_name()] is child for child in children)
    with pytest.raises(TypeError):
        cast(dict[str, object], retained)["audio_source"] = object()


@pytest.mark.parametrize("drift", ["missing", "duplicate", "foreign"])
def test_audio_ingress_element_capture_refuses_nonexact_ownership(
    drift: str,
) -> None:
    driver = _TopologyDriver(_TopologyGst())
    ingress = _TopologyElement("audio_ingress")
    children = [_TopologyElement(name, ingress) for name in sorted(AUDIO_BRANCH_ELEMENT_NAMES)]
    if drift == "missing":
        children.pop()
    elif drift == "duplicate":
        children.append(_TopologyElement("audio_source", ingress))
    else:
        source = next(child for child in children if child.get_name() == "audio_source")
        source.parent = _TopologyElement("foreign")
    ingress.descendants.extend(children)

    with pytest.raises(
        GStreamerDriverError,
        match=r"duplicate|foreign ancestry|set is incomplete",
    ):
        driver._capture_audio_ingress_elements(ingress)


def test_driver_snapshot_measures_exact_pads_peers_and_owned_ingress() -> None:
    driver, pipeline, context = _measured_topology_fixture()

    for replacement in range(3):
        snapshot = driver.generation_snapshot(pipeline)
        assert snapshot["topology_observation"] == "stable"
        assert snapshot["topology_observation_stale"] is False
        assert isinstance(snapshot["topology_observed_monotonic_ns"], int)
        assert snapshot["request_pad_counts_measured"] is True
        assert snapshot["request_pad_peer_ownership_proven"] is True
        assert (
            snapshot["video_tee_request_pads"],
            snapshot["audio_tee_request_pads"],
            snapshot["splitmux_video_request_pads"],
            snapshot["splitmux_audio_request_pads"],
        ) == (4, 1, 3, 1)
        routes = cast(dict[str, int], snapshot["tee_pad_routes"])
        assert routes["video_continuity_linked"] == 1
        assert snapshot["audio_ingress"] == {
            "current_count": 1,
            "current_descendant_count": 10,
            "stale_descendant_count": 0,
            "replacement_count": replacement,
        }
        if replacement < 2:
            driver._rebuild_audio_ingress(context, audio_plan(), 1.0)
            measured = driver._measure_generation_topology(context)
            driver._publish_stable_topology(context, measured)


def test_verified_playing_publishes_the_initial_strict_topology() -> None:
    driver, pipeline, context = _measured_topology_fixture()
    context.last_stable_topology = None
    context.published_topology = None

    driver.set_playing(pipeline, 1.0)

    snapshot = driver.generation_snapshot(pipeline)
    assert snapshot["topology_observation"] == "stable"
    assert snapshot["topology_observation_stale"] is False
    assert snapshot["request_pad_counts_measured"] is True
    assert snapshot["audio_ingress"] == {
        "current_count": 1,
        "current_descendant_count": 10,
        "stale_descendant_count": 0,
        "replacement_count": 0,
    }


def test_driver_snapshot_uses_published_stale_exact_cache_during_handoff() -> None:
    driver, pipeline, context = _measured_topology_fixture()
    fresh = driver.generation_snapshot(pipeline)
    ingress = context.audio_ingress_bin
    assert ingress is not None
    measured_at = fresh["topology_observed_monotonic_ns"]
    cast(dict[str, int], fresh["slot_activations"])["1"] = 999

    driver._publish_topology_transition(
        context,
        "handoff_in_progress",
        phase="audio_restoration",
    )
    # Model the exact transient that Result 12 observed. The read must not
    # traverse it or expose the caller-mutated copy of the stable publication.
    context.audio_ingress_bin = None
    stale = driver.generation_snapshot(pipeline)
    context.audio_ingress_bin = ingress

    assert stale["topology_observation"] == "handoff_in_progress"
    assert stale["topology_observation_stale"] is True
    assert stale["topology_transition_phase"] == "audio_restoration"
    assert stale["topology_observed_monotonic_ns"] == measured_at
    assert cast(dict[str, int], stale["slot_activations"])["1"] == 1
    assert stale["audio_ingress"] == {
        "current_count": 1,
        "current_descendant_count": 10,
        "stale_descendant_count": 0,
        "replacement_count": 0,
    }


def test_driver_snapshot_reports_unavailable_without_a_published_measurement() -> None:
    driver, pipeline, context = _measured_topology_fixture()
    context.last_stable_topology = None
    context.published_topology = None
    snapshot = driver.generation_snapshot(pipeline)

    assert snapshot == {
        "topology_observation": "unavailable",
        "topology_observation_stale": False,
        "active_slot_id": None,
        "active_activation_id": None,
        "slot_count": 0,
        "slot_activations": {},
        "request_pad_invariant": "unavailable",
        "request_pad_counts_measured": False,
        "request_pad_peer_ownership_proven": False,
    }


def test_strict_topology_measurement_preserves_generic_failure_after_publication() -> None:
    driver, pipeline, context = _measured_topology_fixture()
    before = driver.generation_snapshot(pipeline)
    queue = cast(_TopologyElement, context.video_continuity_queue)
    queue.properties["max-size-buffers"] = 3

    with pytest.raises(GStreamerDriverError, match="bounded properties"):
        driver._measure_generation_topology(context)

    assert context.last_stable_topology is not None
    assert context.last_stable_topology["topology_observation"] == "stable"
    assert driver.generation_snapshot(pipeline) == before


def test_audio_ingress_quarantine_arms_one_exact_owned_activation() -> None:
    driver, pipeline, context = _measured_topology_fixture()

    proof = driver.arm_audio_loss(pipeline, "audio_source")

    assert proof == AudioLossArmProof(1, 1, "audio_source")
    quarantine = context.audio_ingress_quarantine
    assert quarantine is not None
    assert quarantine.ingress is context.audio_ingress_bin
    assert quarantine.source is context.audio_ingress_elements["audio_source"]
    assert quarantine.activation_id == 1
    with pytest.raises(GStreamerDriverError, match="cannot arm"):
        driver.arm_audio_loss(pipeline, "audio_source")


def test_audio_loss_arm_refuses_unarmed_source_but_accepts_one_prearm_natural_eos() -> None:
    driver, pipeline, context = _measured_topology_fixture()

    with pytest.raises(GStreamerDriverError, match="exact source"):
        driver.arm_audio_loss(pipeline, "audio_parser")
    assert context.audio_ingress_quarantine is None
    assert context.generations[1].audio_eos.is_retirement_armed() is False

    assert context.generations[1].audio_eos.observe_eos(99) is True
    proof = driver.arm_audio_loss(pipeline, "audio_source")
    assert proof == AudioLossArmProof(1, 1, "audio_source")
    assert context.audio_ingress_quarantine is not None
    assert context.generations[1].audio_eos.boundary_kind() == "NATURAL"


def test_audio_loss_arm_refuses_stale_activation_before_quarantine() -> None:
    driver, pipeline, context = _measured_topology_fixture()
    context.generations[1].activation_id = None

    with pytest.raises(GStreamerDriverError, match="activation ownership"):
        driver.arm_audio_loss(pipeline, "audio_source")
    assert context.audio_ingress_quarantine is None


def test_audio_loss_arm_accepts_one_natural_eos_during_confirmation() -> None:
    driver, pipeline, context = _measured_topology_fixture()
    generation = context.generations[1]

    driver.arm_audio_loss(pipeline, "audio_source")
    assert generation.audio_eos.is_retirement_armed() is True
    assert generation.audio_eos.has_forwarded_eos() is False
    assert generation.audio_eos.observe_eos(123) is True

    assert generation.audio_eos.boundary_kind() == "NATURAL"
    assert generation.audio_eos.has_forwarded_eos() is True
    assert generation.audio_eos.snapshot() == (
        "NATURAL",
        1,
        123,
        None,
        False,
        False,
    )


def test_audio_loss_arm_trace_preserves_prearm_natural_and_failure_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes: list[bytes] = []
    driver, pipeline, context = _measured_topology_fixture()
    generation = context.generations[1]
    assert generation.audio_eos.observe_eos(321) is True
    monkeypatch.setenv("DASHCAM_HANDOFF_TRACE", "1")

    def capture_write(_fd: int, value: bytes) -> int:
        writes.append(value)
        return len(value)

    monkeypatch.setattr(os, "write", capture_write)

    driver.arm_audio_loss(pipeline, "audio_source")

    trace = b"".join(writes)
    pre = next(line for line in writes if b"dashcam-handoff-phase=audio_loss_arm_eos_pre " in line)
    post = next(
        line for line in writes if b"dashcam-handoff-phase=audio_loss_arm_eos_post " in line
    )
    assert b"eos_count=1" in pre
    assert b"eos_seqnum=321" in pre
    assert b"retirement_armed=0" in pre
    assert b"state=1" in pre
    assert b"boundary_kind=1" in post
    assert b"retirement_armed=1" in post
    assert b"dashcam-handoff-phase=audio_loss_arm_complete" in trace

    writes.clear()
    with pytest.raises(GStreamerDriverError, match="cannot arm"):
        driver.arm_audio_loss(pipeline, "audio_source")
    failure = next(
        line for line in writes if b"dashcam-handoff-phase=audio_loss_arm_failed " in line
    )
    detail_hex = failure.split(b"detail_utf8_hex=", 1)[1].strip()
    assert bytes.fromhex(detail_hex.decode("ascii")).decode("utf-8") == (
        "audio EOS containment cannot arm from its current state"
    )


def test_audio_ingress_quarantine_clears_only_after_exact_ingress_removal() -> None:
    driver, _pipeline, context = _measured_topology_fixture()
    driver._arm_audio_ingress_quarantine(context, context.generations[1])

    driver._rebuild_audio_ingress(context, audio_plan(), 1.0)

    assert context.audio_ingress_quarantine is None
    assert context.audio_ingress_replacement_count == 1
    assert context.audio_ingress_bin is not None


def test_audio_ingress_identity_rotates_exactly_across_two_reconnect_cycles() -> None:
    driver, pipeline, context = _measured_topology_fixture()
    observed_maps = [context.audio_ingress_elements]
    observed_sources = [context.audio_ingress_elements["audio_source"]]

    for expected_generation in (0, 1):
        if expected_generation == 1:
            assert context.generations[1].audio_eos.observe_eos(777) is True
        proof = driver.arm_audio_loss(pipeline, "audio_source")
        quarantine = context.audio_ingress_quarantine
        assert proof.source_name == "audio_source"
        assert quarantine is not None
        assert quarantine.source is observed_sources[-1]
        assert quarantine.ingress_generation == expected_generation
        if expected_generation == 1:
            assert context.generations[1].audio_eos.boundary_kind() == "NATURAL"

        driver._rebuild_audio_ingress(context, audio_plan(), 1.0)
        assert context.audio_ingress_quarantine is None
        assert context.audio_ingress_replacement_count == expected_generation + 1
        assert context.audio_ingress_elements is not observed_maps[-1]
        assert not any(
            current is retired
            for current in context.audio_ingress_elements.values()
            for retired in observed_maps[-1].values()
        )
        assert (
            driver._exact_audio_message_source(
                pipeline,
                FakeGstMessage(
                    FakeMessageType.ERROR,
                    src=observed_sources[-1],
                ),
                context,
            )
            is None
        )
        observed_maps.append(context.audio_ingress_elements)
        observed_sources.append(context.audio_ingress_elements["audio_source"])
        context.generations[1].audio_eos = _AudioEosArbiter()

    with pytest.raises(TypeError):
        cast(dict[str, object], context.audio_ingress_elements)["audio_source"] = object()


def test_driver_snapshot_refuses_extra_or_foreign_request_pad() -> None:
    driver, _pipeline, context = _measured_topology_fixture()
    video_tee = cast(_TopologyElement, context.video_tee)
    video_tee.src_pads.append(_TopologyPad())
    with pytest.raises(GStreamerDriverError, match="registered fixed-slot"):
        driver._measure_generation_topology(context)

    driver, _pipeline, context = _measured_topology_fixture()
    active = context.generations[1]
    cast(_TopologyPad, active.video_tee_pad).peer = object()
    with pytest.raises(GStreamerDriverError, match="foreign or absent"):
        driver._measure_generation_topology(context)


@pytest.mark.parametrize(
    "drift",
    ["ancestry", "factory", "tee_peer", "queue_peer", "queue_bound", "sink_qos", "state"],
)
def test_driver_snapshot_refuses_video_continuity_contract_drift(
    drift: str,
) -> None:
    driver, _pipeline, context = _measured_topology_fixture()
    queue = cast(_TopologyElement, context.video_continuity_queue)
    sink = cast(_TopologyElement, context.video_continuity_sink)
    tee_pad = cast(_TopologyPad, context.video_continuity_tee_pad)
    queue_sink = cast(_TopologyPad, queue.get_static_pad("sink"))
    queue_src = cast(_TopologyPad, queue.get_static_pad("src"))

    if drift == "ancestry":
        queue.parent = object()
    elif drift == "factory":
        queue.factory = _TopologyFactory("identity")
    elif drift == "tee_peer":
        tee_pad.peer = None
    elif drift == "queue_peer":
        queue_src.peer = None
    elif drift == "queue_bound":
        queue.properties["max-size-buffers"] = 3
    elif drift == "sink_qos":
        sink.properties["qos"] = True
    else:
        queue.state = (
            _TopologyGst.StateChangeReturn.SUCCESS,
            "PAUSED",
            _TopologyGst.State.VOID_PENDING,
        )

    with pytest.raises(
        GStreamerDriverError,
        match=r"continuity route (ancestry|peer|bounded|is not stably)",
    ):
        driver._measure_generation_topology(context)

    # Keep the reciprocal reference live so accidental one-sided checks cannot
    # make the fixture itself vacuously pass static analysis.
    assert queue_sink is not None


def test_driver_snapshot_refuses_stale_or_duplicate_named_audio_descendant() -> None:
    driver, pipeline, _context = _measured_topology_fixture()
    pipeline.descendants.append(_TopologyElement("audio_ingress", pipeline))
    with pytest.raises(GStreamerDriverError, match="ingress count/descendant"):
        driver._measure_generation_topology(_context)

    driver, pipeline, _context = _measured_topology_fixture()
    pipeline.descendants.append(_TopologyElement("audio_source", pipeline))
    with pytest.raises(GStreamerDriverError, match="ingress count/descendant"):
        driver._measure_generation_topology(_context)

    driver, pipeline, context = _measured_topology_fixture()
    ingress = context.audio_ingress_bin
    assert ingress is not None
    pipeline.descendants.append(_TopologyElement("audio_parser", ingress))
    with pytest.raises(GStreamerDriverError, match="ingress count/descendant"):
        driver._measure_generation_topology(context)

    driver, pipeline, context = _measured_topology_fixture()
    ingress = context.audio_ingress_bin
    assert ingress is not None
    pipeline.descendants = [
        (
            _TopologyElement("anonymous_wrong_factory", ingress)
            if cast(_TopologyElement, candidate).get_name() == "capsfilter0"
            else candidate
        )
        for candidate in pipeline.descendants
    ]
    with pytest.raises(GStreamerDriverError, match="ingress count/descendant"):
        driver._measure_generation_topology(context)


def test_audio_ingress_rebuild_refuses_a_retired_descendant_still_in_pipeline() -> None:
    driver, pipeline, context = _measured_topology_fixture()
    pipeline.retain_removed_descendant = True

    with pytest.raises(GStreamerDriverError, match="remained in the pipeline"):
        driver._rebuild_audio_ingress(context, audio_plan(), 1.0)

    assert context.audio_ingress_bin is not None
    assert context.audio_ingress_replacement_count == 0


def test_driver_rotates_three_fixed_slots_with_monotonic_activation_ids() -> None:
    pipeline = object()
    driver = PyGObjectGStreamerDriver(object())
    av = _driver_generation(1, has_audio=True, linked=True)
    video_two = _driver_generation(2)
    video_three = _driver_generation(3)
    av.activation_id = 1
    context = _GenerationPipeline(
        pipeline,
        object(),
        "/srv/dashcam/pending",
        "abcdef123456",
        0,
        object(),
        object(),
        object(),
        object(),
        {1: av, 2: video_two, 3: video_three},
        threading.Lock(),
        {},
        deque(),
    )
    driver._generation_pipelines[id(pipeline)] = context
    observed = [(1, 1)]

    first_video = driver._select_video_successor(context)
    assert first_video is video_two
    driver._allocate_slot_activation(context, first_video)
    av.linked = False
    first_video.linked = True
    driver._commit_active_route(context, first_video)
    observed.append((context.active_generation_id, cast(int, first_video.activation_id)))

    first_video.linked = False
    first_video.activation_id = None
    av.activation_id = None
    driver._allocate_slot_activation(context, av)
    av.linked = True
    driver._commit_active_route(context, av)
    observed.append((context.active_generation_id, cast(int, av.activation_id)))

    av.linked = False
    av.activation_id = None
    second_video = driver._select_video_successor(context)
    assert second_video is video_three
    driver._allocate_slot_activation(context, second_video)
    second_video.linked = True
    driver._commit_active_route(context, second_video)
    observed.append((context.active_generation_id, cast(int, second_video.activation_id)))

    second_video.linked = False
    second_video.activation_id = None
    driver._allocate_slot_activation(context, av)
    av.linked = True
    driver._commit_active_route(context, av)
    observed.append((context.active_generation_id, cast(int, av.activation_id)))

    assert observed == [(1, 1), (2, 2), (1, 3), (3, 4), (1, 5)]
    assert context.active_generation_id == 1
    assert context.generations[context.active_generation_id].activation_id == 5


def test_audio_eos_arbiter_accepts_one_natural_boundary_and_refuses_duplicates() -> None:
    unarmed = _AudioEosArbiter()
    assert unarmed.is_retirement_armed() is False
    assert unarmed.has_forwarded_eos() is False
    assert unarmed.observe_eos(9) is True
    assert unarmed.boundary_kind() is None
    unarmed.arm_retirement()
    assert unarmed.boundary_kind() == "NATURAL"
    assert unarmed.has_forwarded_eos() is True

    natural = _AudioEosArbiter()
    natural.arm_retirement()
    assert natural.observe_eos(10) is True
    assert natural.is_retirement_armed() is True
    assert natural.has_forwarded_eos() is True
    assert natural.boundary_kind() == "NATURAL"
    assert natural.snapshot() == ("NATURAL", 1, 10, None, False, False)
    assert natural.observe_eos(30) is False
    assert natural.snapshot()[0] == "REFUSED"
    assert natural.snapshot()[5] is True


def test_audio_eos_arbiter_reserves_generation_eos_and_drops_one_late_natural() -> None:
    arbiter = _AudioEosArbiter()
    arbiter.arm_retirement()
    arbiter.reserve_generation_eos(701)

    assert arbiter.observe_eos(203) is False
    assert arbiter.observe_eos(701) is True
    assert arbiter.boundary_kind() == "GENERATION"
    assert arbiter.has_forwarded_eos() is True
    assert arbiter.generation_snapshot() == (
        "GENERATION",
        1,
        701,
        701,
        1,
        False,
        True,
    )


@pytest.mark.parametrize(
    "drift",
    [
        "duplicate",
        "natural_without_seqnum",
        "natural_with_manual",
    ],
)
def test_audio_eos_arbiter_refuses_every_nonpristine_prearm_shape(
    drift: str,
) -> None:
    arbiter = _AudioEosArbiter()
    if drift == "duplicate":
        assert arbiter.observe_eos(10) is True
        assert arbiter.observe_eos(11) is False
    elif drift == "natural_without_seqnum":
        arbiter.state = "NATURAL"
        arbiter.eos_count = 1
    else:
        assert arbiter.observe_eos(13) is True
        arbiter.manual_seqnum = 14

    with pytest.raises(GStreamerDriverError, match="cannot arm"):
        arbiter.arm_retirement()
    assert arbiter.is_retirement_armed() is False


class _GenerationEosEvent:
    def __init__(self, seqnum: int, kind: str) -> None:
        self.seqnum = seqnum
        self.kind = kind

    def get_seqnum(self) -> int:
        return self.seqnum


class _GenerationEosEventApi:
    @staticmethod
    def new_eos() -> _GenerationEosEvent:
        return _GenerationEosEvent(701, "generation-eos")


class _GenerationEosGst:
    Event = _GenerationEosEventApi


class _GenerationEosOutput:
    def __init__(
        self,
        arbiter: _AudioEosArbiter,
        *,
        outcome: str,
    ) -> None:
        self.arbiter = arbiter
        self.outcome = outcome
        self.calls = 0
        self.release = threading.Event()

    def send_event(self, event: _GenerationEosEvent) -> bool:
        self.calls += 1
        assert event.kind == "generation-eos"
        if self.outcome == "block":
            self.release.wait(1.0)
        if self.outcome == "accepted":
            assert self.arbiter.observe_eos(event.get_seqnum()) is True
            return True
        if self.outcome == "accepted-false":
            assert self.arbiter.observe_eos(event.get_seqnum()) is True
            return False
        if self.outcome == "late-natural":
            assert self.arbiter.observe_eos(203) is False
            assert self.arbiter.observe_eos(event.get_seqnum()) is True
            return True
        if self.outcome == "mismatched":
            assert self.arbiter.observe_eos(event.get_seqnum() + 1) is False
            return True
        if self.outcome == "duplicate":
            assert self.arbiter.observe_eos(event.get_seqnum()) is True
            assert self.arbiter.observe_eos(event.get_seqnum() + 1) is False
            return True
        if self.outcome == "unobserved":
            return True
        assert self.outcome == "refused"
        return False


def _generation_eos_context(generation: _RecordingGeneration) -> _GenerationPipeline:
    generation.activation_id = 1
    generation.linked = True
    return _GenerationPipeline(
        object(),
        _GenerationEosGst(),
        "/srv/dashcam/pending",
        "abcdef123456",
        0,
        object(),
        object(),
        object(),
        object(),
        {generation.generation_id: generation},
        threading.Lock(),
        {},
        deque(),
    )


def test_audio_retirement_uses_natural_eos_fast_path_without_generation_send() -> None:
    driver = PyGObjectGStreamerDriver(object())
    generation = _driver_generation(1, has_audio=True)
    generation.audio_eos.arm_retirement()
    assert generation.audio_eos.observe_eos(600) is True
    output = _GenerationEosOutput(generation.audio_eos, outcome="refused")
    generation.output = output
    context = _generation_eos_context(generation)

    boundary = driver._establish_audio_retirement_boundary(
        context,
        generation,
        object(),
        time.monotonic() + 0.2,
    )

    assert boundary == "NATURAL"
    assert output.calls == 0
    assert generation.video_retirement_eos_sent is False
    assert generation.generation_retirement_eos_seqnum is None


@pytest.mark.parametrize("outcome", ["accepted", "accepted-false", "late-natural"])
def test_audio_retirement_sends_one_generation_eos_and_requires_exact_observation(
    outcome: str,
) -> None:
    driver = PyGObjectGStreamerDriver(_GenerationEosGst())
    generation = _driver_generation(1, has_audio=True)
    generation.audio_eos.arm_retirement()
    output = _GenerationEosOutput(generation.audio_eos, outcome=outcome)
    generation.output = output
    context = _generation_eos_context(generation)

    boundary = driver._establish_audio_retirement_boundary(
        context,
        generation,
        object(),
        time.monotonic() + 0.2,
    )

    assert boundary == "GENERATION"
    assert output.calls == 1
    assert generation.audio_eos.snapshot() == (
        "GENERATION",
        1,
        701,
        None,
        False,
        False,
    )
    assert generation.video_retirement_eos_sent is True
    assert generation.generation_retirement_eos_seqnum == 701
    assert generation.audio_eos.generation_snapshot() in {
        ("GENERATION", 1, 701, 701, 0, False, True),
        ("GENERATION", 1, 701, 701, 1, False, True),
    }


@pytest.mark.parametrize("outcome", ["refused", "unobserved", "mismatched", "duplicate"])
def test_audio_retirement_generation_eos_refuses_nonexact_observation(
    outcome: str,
) -> None:
    driver = PyGObjectGStreamerDriver(_GenerationEosGst())
    generation = _driver_generation(1, has_audio=True)
    generation.audio_eos.arm_retirement()
    output = _GenerationEosOutput(generation.audio_eos, outcome=outcome)
    generation.output = output
    context = _generation_eos_context(generation)

    with pytest.raises(
        GStreamerDriverError,
        match="generation EOS has no exact A/V retirement acceptance",
    ):
        driver._establish_audio_retirement_boundary(
            context,
            generation,
            object(),
            time.monotonic() + 0.2,
        )

    assert output.calls == 1
    assert generation.video_retirement_eos_sent is False
    assert generation.generation_retirement_eos_seqnum is None


def test_audio_retirement_generation_eos_worker_survival_is_bounded_and_fails_closed() -> None:
    driver = PyGObjectGStreamerDriver(_GenerationEosGst())
    generation = _driver_generation(1, has_audio=True)
    generation.audio_eos.arm_retirement()
    output = _GenerationEosOutput(generation.audio_eos, outcome="block")
    generation.output = output
    context = _generation_eos_context(generation)
    driver._generation_pipelines[id(context.pipeline)] = context
    driver._set_and_verify_state = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

    try:
        with pytest.raises(
            GStreamerDriverError,
            match="av-generation-retirement-eos worker survived its deadline",
        ):
            driver._establish_audio_retirement_boundary(
                context,
                generation,
                object(),
                time.monotonic() + 0.03,
            )
        assert len(context.audio_retirement_dispatches) == 1
        survivor = context.audio_retirement_dispatches[-1]
        assert survivor.done.is_set() is False
        assert survivor.thread is not None and survivor.thread.is_alive()
        with pytest.raises(
            GStreamerDriverError,
            match="av-generation-retirement-eos worker survived parent NULL",
        ):
            driver.set_null(context.pipeline, 0.02)
    finally:
        output.release.set()
        survivor = context.audio_retirement_dispatches[-1]
        assert survivor.thread is not None
        survivor.thread.join(0.2)


class _DrainClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, duration: float) -> None:
        self.sleeps.append(duration)
        self.now += duration


class _ScriptedLevelQueue:
    def __init__(self, snapshots: list[tuple[int, int, int]]) -> None:
        self.snapshots = snapshots
        self.sample_index = 0

    def get_property(self, name: str) -> int:
        property_index = {
            "current-level-buffers": 0,
            "current-level-bytes": 1,
            "current-level-time": 2,
        }[name]
        snapshot = self.snapshots[min(self.sample_index, len(self.snapshots) - 1)]
        value = snapshot[property_index]
        if property_index == 2:
            self.sample_index += 1
        return value


def test_audio_queue_drain_converges_after_transients_and_resets_streak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _DrainClock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    queue = _ScriptedLevelQueue(
        [
            (1, 0, 0),
            (0, 0, 0),
            (0, 12, 0),
            (0, 0, 0),
            (0, 0, 0),
        ]
    )

    PyGObjectGStreamerDriver(object())._wait_for_audio_queue_drain(
        queue,
        0.25,
    )

    assert queue.sample_index == 5
    assert clock.sleeps == [0.01, 0.05, 0.01, 0.05]
    assert clock.now == pytest.approx(0.12)


def test_audio_queue_drain_times_out_without_extending_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _DrainClock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    queue = _ScriptedLevelQueue([(1, 2, 3)])

    with pytest.raises(GStreamerDriverError, match="queue drain timed out"):
        PyGObjectGStreamerDriver(object())._wait_for_audio_queue_drain(
            queue,
            0.11,
        )

    assert clock.sleeps == [0.01] * 11
    assert clock.now == pytest.approx(0.11)


def test_audio_queue_drain_runs_first_empty_action_inside_proof_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _DrainClock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    queue = _ScriptedLevelQueue([(1, 2, 3), (0, 0, 0), (0, 0, 0)])
    action_times: list[float] = []

    PyGObjectGStreamerDriver(object())._wait_for_audio_queue_drain(
        queue,
        0.2,
        on_first_empty=lambda: action_times.append(clock.now),
    )

    assert action_times == [0.01]
    assert clock.sleeps == [0.01, 0.05]
    assert clock.now == pytest.approx(0.06)


def test_audio_queue_drain_refuses_refill_after_first_empty_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _DrainClock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    queue = _ScriptedLevelQueue([(0, 0, 0), (1, 2, 3)])
    actions: list[str] = []

    with pytest.raises(GStreamerDriverError, match="refilled after first-empty"):
        PyGObjectGStreamerDriver(object())._wait_for_audio_queue_drain(
            queue,
            0.2,
            on_first_empty=lambda: actions.append("armed"),
        )

    assert actions == ["armed"]
    assert clock.sleeps == [0.05]


def test_fragment_audio_accounting_uses_streaming_boundaries_after_delayed_poll() -> None:
    generation = _driver_generation(1)
    generation.audio_running_times.extend((5, 15, 25, 35))

    assert PyGObjectGStreamerDriver._consume_fragment_audio_units(generation, 0, 20) == 2
    assert tuple(generation.audio_running_times) == (25, 35)
    assert PyGObjectGStreamerDriver._consume_fragment_audio_units(generation, 20, 40) == 2
    assert tuple(generation.audio_running_times) == ()


def test_generation_pending_queue_is_bounded_fail_closed() -> None:
    context = _GenerationPipeline(
        object(),
        object(),
        "/srv/dashcam/pending",
        "abcdef123456",
        0,
        object(),
        object(),
        object(),
        object(),
        {},
        threading.Lock(),
        {},
        deque(),
    )
    for _ in range(64):
        PyGObjectGStreamerDriver._queue_pending(
            context,
            BusMessage(BusMessageKind.NONE),
        )
    with pytest.raises(GStreamerDriverError, match="exceeded its bound"):
        PyGObjectGStreamerDriver._queue_pending(
            context,
            BusMessage(BusMessageKind.NONE),
        )


def test_handoff_bus_refuses_parent_eos_instead_of_deferring_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = PyGObjectGStreamerDriver(object())
    context = _GenerationPipeline(
        object(),
        object(),
        "/srv/dashcam/pending",
        "abcdef123456",
        0,
        object(),
        object(),
        object(),
        object(),
        {},
        threading.Lock(),
        {},
        deque(),
    )
    monkeypatch.setattr(
        driver,
        "_poll_bus_native",
        lambda _pipeline, _timeout: BusMessage(BusMessageKind.EOS),
    )

    with pytest.raises(
        GStreamerDriverError,
        match="parent pipeline reached unexpected EOS during test handoff",
    ):
        driver._preserve_handoff_bus(
            context,
            context.pipeline,
            0.1,
            "test handoff",
        )

    assert not context.pending_messages


class _PrewarmBin:
    def __init__(
        self,
        *,
        sync_result: bool = True,
        after_sync: Callable[[], None] | None = None,
    ) -> None:
        self.locked: list[bool] = []
        self.sync_calls = 0
        self.sync_result = sync_result
        self.after_sync = after_sync

    def set_locked_state(self, locked: bool) -> bool:
        self.locked.append(locked)
        return True

    def sync_state_with_parent(self) -> bool:
        self.sync_calls += 1
        if self.after_sync is not None:
            self.after_sync()
        return self.sync_result


class _PrewarmValve:
    def __init__(self, drop: bool = True) -> None:
        self.drop = drop

    def get_property(self, name: str) -> bool:
        assert name == "drop"
        return self.drop


def _prewarm_fixture() -> tuple[
    PyGObjectGStreamerDriver,
    _GenerationPipeline,
    _RecordingGeneration,
    _PrewarmBin,
]:
    generation_bin = _PrewarmBin()
    generation = _driver_generation(
        2,
        valve=_PrewarmValve(),
        generation_bin=generation_bin,
    )
    generation.activation_id = 2
    context = _GenerationPipeline(
        object(),
        object(),
        "/srv/dashcam/pending",
        "abcdef123456",
        0,
        object(),
        object(),
        object(),
        object(),
        {2: generation},
        threading.Lock(),
        {},
        deque(),
    )
    return PyGObjectGStreamerDriver(object()), context, generation, generation_bin


def test_successor_prewarm_finishes_before_linking_or_media_ownership() -> None:
    driver, context, generation, generation_bin = _prewarm_fixture()

    driver._prewarm_generation(context, generation)

    assert generation_bin.locked == [False]
    assert generation_bin.sync_calls == 1
    assert generation.linked is False
    assert generation.opened == {}
    assert context.location_generation == {}


def test_successor_prewarm_refuses_failed_parent_state_sync() -> None:
    driver, context, generation, _generation_bin = _prewarm_fixture()
    failing_bin = _PrewarmBin(sync_result=False)
    generation.bin = failing_bin

    with pytest.raises(GStreamerDriverError, match="could not synchronize"):
        driver._prewarm_generation(context, generation)

    assert failing_bin.locked == [False]
    assert failing_bin.sync_calls == 1


def test_successor_prewarm_refuses_post_sync_ownership_drift() -> None:
    driver, context, generation, _generation_bin = _prewarm_fixture()

    def reserve_location() -> None:
        context.location_generation["unexpected.partial.mp4"] = (2, 2)

    drifting_bin = _PrewarmBin(after_sync=reserve_location)
    generation.bin = drifting_bin

    with pytest.raises(GStreamerDriverError, match="changed media ownership"):
        driver._prewarm_generation(context, generation)

    assert drifting_bin.locked == [False]
    assert drifting_bin.sync_calls == 1


def test_av_successor_prewarm_keeps_both_valves_closed() -> None:
    driver, context, generation, generation_bin = _prewarm_fixture()
    audio_valve = _PrewarmValve()
    generation.has_audio = True
    generation.audio_valve = audio_valve

    driver._prewarm_generation(context, generation)

    assert cast(_PrewarmValve, generation.video_valve).drop is True
    assert audio_valve.drop is True
    assert generation_bin.locked == [False]
    assert generation_bin.sync_calls == 1


@pytest.mark.parametrize("drift", ["linked", "opened", "reserved", "valve"])
def test_successor_prewarm_refuses_ownership_drift(drift: str) -> None:
    driver, context, generation, generation_bin = _prewarm_fixture()
    if drift == "linked":
        generation.linked = True
    elif drift == "opened":
        generation.opened["unexpected.partial.mp4"] = 1
    elif drift == "reserved":
        context.location_generation["unexpected.partial.mp4"] = (2, 2)
    else:
        cast(_PrewarmValve, generation.video_valve).drop = False

    with pytest.raises(GStreamerDriverError, match="cannot prewarm"):
        driver._prewarm_generation(context, generation)

    assert generation_bin.locked == []
    assert generation_bin.sync_calls == 0


class _ProofPipeline:
    def __init__(
        self,
        source: object,
        camera: object,
        encoder: object,
    ) -> None:
        self.source = source
        self.camera = camera
        self.encoder = encoder

    def get_by_name(self, name: str) -> object | None:
        return {
            "audio_source": self.source,
            "camera": self.camera,
            "encoder": self.encoder,
        }.get(name)


class _ProofValve:
    def __init__(self, drop: bool = False) -> None:
        self.drop = drop

    def get_property(self, name: str) -> bool:
        assert name == "drop"
        return self.drop


class _ProofPad:
    def __init__(self, peer: object | None = None) -> None:
        self.peer = peer

    def get_peer(self) -> object | None:
        return self.peer


class _ProofQueue:
    def __init__(self, src: _ProofPad) -> None:
        self.src = src

    def get_static_pad(self, name: str) -> _ProofPad:
        assert name == "src"
        return self.src


class _ProofOutput:
    def __init__(self, video: object, audio: object) -> None:
        self.video = video
        self.audio = audio

    def get_static_pad(self, name: str) -> object:
        assert name in {"video", "audio_0"}
        return self.video if name == "video" else self.audio


def _retained_audio_elements(source: object) -> MappingProxyType[str, object]:
    return MappingProxyType(
        {
            name: source if name == "audio_source" else object()
            for name in AUDIO_BRANCH_ELEMENT_NAMES
        }
    )


def _restoring_parent_failure_fixture() -> tuple[
    PyGObjectGStreamerDriver,
    _GenerationPipeline,
    _RecordingGeneration,
    _RestorationParentFailureProvenance,
]:
    old_source = object()
    new_source = object()
    camera = object()
    encoder = object()
    pipeline = _ProofPipeline(new_source, camera, encoder)
    video_ghost = object()
    audio_ghost = object()
    video_tee_pad = _ProofPad(video_ghost)
    audio_tee_pad = _ProofPad(audio_ghost)
    output_video_pad = _ProofPad()
    output_audio_pad = _ProofPad()
    video_src = _ProofPad(output_video_pad)
    audio_src = _ProofPad(output_audio_pad)
    output_video_pad.peer = video_src
    output_audio_pad.peer = audio_src
    generation = _RecordingGeneration(
        generation_id=1,
        has_audio=True,
        bin=object(),
        output=_ProofOutput(output_video_pad, output_audio_pad),
        video_valve=_ProofValve(),
        video_queue=_ProofQueue(video_src),
        video_ghost=video_ghost,
        video_tee_pad=video_tee_pad,
        output_video_pad=output_video_pad,
        audio_valve=_ProofValve(),
        audio_queue=_ProofQueue(audio_src),
        audio_ghost=audio_ghost,
        audio_tee_pad=audio_tee_pad,
        output_audio_pad=output_audio_pad,
        linked=True,
        activation_id=3,
        audio_units=1,
        video_units=1,
        first_video_is_idr=True,
        first_video_had_sticky_contract=True,
    )
    generation.first_video_seen.set()
    generation.opened["restored.partial.mp4"] = 1
    original_ingress = object()
    new_ingress = object()
    original_elements = _retained_audio_elements(old_source)
    new_elements = _retained_audio_elements(new_source)
    quarantine = _AudioIngressQuarantine(
        original_ingress,
        old_source,
        1,
        error_count=1,
    )
    retiring = _driver_generation(2, linked=False)
    retiring.activation_id = 2
    retiring.opened["retiring.partial.mp4"] = 1
    context = _GenerationPipeline(
        pipeline,
        object(),
        "/srv/dashcam/pending",
        "abcdef123456",
        0,
        object(),
        object(),
        camera,
        encoder,
        {1: generation, 2: retiring},
        threading.Lock(),
        {
            "restored.partial.mp4": (1, 3),
            "retiring.partial.mp4": (2, 2),
        },
        deque(),
        active_generation_id=1,
        next_activation_id=4,
        isolated=True,
        routing_phase="AV_RESTORING",
        loss_verified=True,
        initial_camera=camera,
        initial_encoder=encoder,
        audio_ingress_bin=new_ingress,
        audio_ingress_elements=new_elements,
        audio_ingress_replacement_count=1,
    )
    provenance = _RestorationParentFailureProvenance(
        context=context,
        pipeline=pipeline,
        camera=camera,
        encoder=encoder,
        retiring=retiring,
        retiring_activation_id=2,
        retiring_location="retiring.partial.mp4",
        original_ingress=original_ingress,
        original_elements=original_elements,
        original_quarantine=quarantine,
        original_source=old_source,
        replacement_count=0,
        successor=generation,
        expected_successor_activation_id=3,
        failure_state="FAILURE",
        playing_state="PLAYING",
        void_pending_state="VOID_PENDING",
    )
    return (
        PyGObjectGStreamerDriver(object()),
        context,
        generation,
        provenance,
    )


def test_restoration_accepts_only_the_latched_audio_parent_failure_shape() -> None:
    driver, context, generation, provenance = _restoring_parent_failure_fixture()

    assert driver._restoration_parent_failure_provenance_matches(
        context,
        generation,
        provenance,
        require_media=True,
    )


@pytest.mark.parametrize(
    "drift",
    [
        "unverified",
        "wrong_phase",
        "not_isolated",
        "wrong_active",
        "unlinked",
        "no_activation",
        "quarantine_present",
        "no_ingress",
        "no_replacement",
        "pending_error",
        "pending_audio_error",
        "pending_eos",
        "consumed",
        "wrong_source",
        "wrong_replacement",
        "not_idr",
        "no_audio",
        "wrong_location",
        "closed_valve",
        "wrong_video_pad",
        "stale_retiring",
        "stale_next_activation",
        "camera_changed",
    ],
)
def test_restoration_refuses_nonexact_latched_parent_failure_shape(
    drift: str,
) -> None:
    driver, context, generation, provenance = _restoring_parent_failure_fixture()
    if drift == "unverified":
        context.loss_verified = False
    elif drift == "wrong_phase":
        context.routing_phase = "AV_ACTIVE"
    elif drift == "not_isolated":
        context.isolated = False
    elif drift == "wrong_active":
        context.active_generation_id = 2
    elif drift == "unlinked":
        generation.linked = False
    elif drift == "no_activation":
        generation.activation_id = None
    elif drift == "quarantine_present":
        context.audio_ingress_quarantine = _AudioIngressQuarantine(
            object(),
            object(),
            1,
        )
    elif drift == "no_ingress":
        context.audio_ingress_bin = None
    elif drift == "no_replacement":
        context.audio_ingress_replacement_count = 0
    elif drift == "consumed":
        provenance.consumed = True
    elif drift == "wrong_source":
        context.audio_ingress_elements = _retained_audio_elements(provenance.original_source)
    elif drift == "wrong_replacement":
        context.audio_ingress_replacement_count = 2
    elif drift == "not_idr":
        generation.first_video_is_idr = False
    elif drift == "no_audio":
        generation.audio_units = 0
    elif drift == "wrong_location":
        context.location_generation["restored.partial.mp4"] = (1, 4)
    elif drift == "closed_valve":
        cast(_ProofValve, generation.audio_valve).drop = True
    elif drift == "wrong_video_pad":
        cast(_ProofQueue, generation.video_queue).src.peer = object()
    elif drift == "stale_retiring":
        provenance.retiring.activation_id = 4
    elif drift == "stale_next_activation":
        context.next_activation_id = 5
    elif drift == "camera_changed":
        cast(_ProofPipeline, context.pipeline).camera = object()
    else:
        kind = {
            "pending_error": BusMessageKind.ERROR,
            "pending_audio_error": BusMessageKind.AUDIO_ERROR,
            "pending_eos": BusMessageKind.EOS,
        }[drift]
        context.pending_messages.append(BusMessage(kind))

    assert not driver._restoration_parent_failure_provenance_matches(
        context,
        generation,
        provenance,
        require_media=True,
    )


class _CapturePipeline(_ProofPipeline):
    def __init__(
        self,
        source: object,
        camera: object,
        encoder: object,
        parent_state: tuple[object, object, object],
    ) -> None:
        super().__init__(source, camera, encoder)
        self.parent_state = parent_state

    def get_state(self, timeout: int) -> tuple[object, object, object]:
        assert timeout == 0
        return self.parent_state


class _CaptureDriver(PyGObjectGStreamerDriver):
    def __init__(self) -> None:
        super().__init__(object())
        self.drains: list[str] = []
        self.measures = 0

    def _state(self, name: str) -> object:
        return name

    def _state_return(self, name: str) -> object:
        return name

    def _drain_handoff_fatal_bus(
        self,
        context: _GenerationPipeline,
        pipeline: object,
        operation: str,
    ) -> None:
        assert pipeline is context.pipeline
        self.drains.append(operation)

    def _measure_audio_ingress(
        self,
        context: _GenerationPipeline,
    ) -> dict[str, int]:
        self.measures += 1
        return {
            "current_count": 1,
            "current_descendant_count": 10,
            "stale_descendant_count": 0,
            "replacement_count": context.audio_ingress_replacement_count,
        }


def test_restoration_failure_provenance_is_captured_before_mutation() -> None:
    driver = _CaptureDriver()
    source = object()
    camera = object()
    encoder = object()
    ingress = object()
    pipeline = _CapturePipeline(
        source,
        camera,
        encoder,
        ("FAILURE", "PLAYING", "VOID_PENDING"),
    )
    successor = _driver_generation(1, has_audio=True)
    retiring = _driver_generation(2, linked=True)
    retiring.activation_id = 2
    retiring.opened["retiring.partial.mp4"] = 1
    quarantine = _AudioIngressQuarantine(
        ingress,
        source,
        1,
        error_count=1,
    )
    context = _GenerationPipeline(
        pipeline,
        object(),
        "/srv/dashcam/pending",
        "abcdef123456",
        0,
        object(),
        object(),
        camera,
        encoder,
        {1: successor, 2: retiring},
        threading.Lock(),
        {"retiring.partial.mp4": (2, 2)},
        deque(),
        active_generation_id=2,
        next_activation_id=3,
        isolated=True,
        routing_phase="VIDEO_ONLY_ACTIVE",
        loss_verified=True,
        initial_camera=camera,
        initial_encoder=encoder,
        audio_ingress_bin=ingress,
        audio_ingress_elements=_retained_audio_elements(source),
        audio_ingress_quarantine=quarantine,
    )

    provenance = driver._capture_restoration_parent_failure_provenance(
        context,
        retiring,
        successor,
    )

    assert provenance is not None
    assert provenance.context is context
    assert provenance.retiring is retiring
    assert provenance.retiring_activation_id == 2
    assert provenance.retiring_location == "retiring.partial.mp4"
    assert provenance.original_ingress is ingress
    assert provenance.original_elements is context.audio_ingress_elements
    assert provenance.original_quarantine is quarantine
    assert provenance.expected_successor_activation_id == 3
    assert successor.activation_id is None
    assert successor.linked is False
    assert driver.drains == [
        "restoration provenance capture",
        "restoration provenance capture",
    ]
    assert driver.measures == 1


def test_restoration_capture_returns_no_token_for_a_clean_parent() -> None:
    driver = _CaptureDriver()
    context, generation, _provenance = _restoring_parent_failure_fixture()[1:]
    pipeline = _CapturePipeline(
        object(),
        context.initial_camera,
        context.initial_encoder,
        ("SUCCESS", "PLAYING", "VOID_PENDING"),
    )
    context.pipeline = pipeline
    context.active_generation_id = 2
    retiring = context.generations[2]
    retiring.linked = True
    successor = generation
    successor.linked = False
    successor.activation_id = None
    successor.opened.clear()
    successor.reusable = True

    assert (
        driver._capture_restoration_parent_failure_provenance(
            context,
            retiring,
            successor,
        )
        is None
    )
    assert driver.drains == [
        "restoration provenance capture",
        "restoration normal-parent capture",
    ]


def test_restoration_bus_drain_rejects_a_new_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver, context, _generation, _provenance = _restoring_parent_failure_fixture()
    monkeypatch.setattr(
        driver,
        "_poll_bus_native",
        lambda _pipeline, _timeout: BusMessage(BusMessageKind.ERROR, "new"),
    )

    with pytest.raises(
        GStreamerDriverError,
        match="observed a new fatal bus message",
    ):
        driver._drain_handoff_fatal_bus(
            context,
            context.pipeline,
            "restoration convergence proof",
        )


def test_video_only_handoff_retains_exact_latched_parent_failure_shape() -> None:
    initial = _driver_generation(1, has_audio=True)
    initial.activation_id = 1
    generation = _driver_generation(2, linked=True)
    generation.activation_id = 2
    ingress = object()
    source = object()
    context = _GenerationPipeline(
        object(),
        object(),
        "/srv/dashcam/pending",
        "abcdef123456",
        0,
        object(),
        object(),
        object(),
        object(),
        {1: initial, 2: generation},
        threading.Lock(),
        {},
        deque(),
        active_generation_id=2,
        routing_phase="VIDEO_ONLY_ACTIVE",
        loss_verified=True,
        audio_ingress_bin=ingress,
        audio_ingress_elements=_retained_audio_elements(source),
        audio_ingress_quarantine=_AudioIngressQuarantine(
            ingress,
            source,
            1,
            error_count=1,
        ),
    )

    assert PyGObjectGStreamerDriver._accept_current_loss_parent_failure(
        context,
        generation,
    )


class _RetirementEvent:
    def get_seqnum(self) -> int:
        return 700


class _RetirementEventApi:
    @staticmethod
    def new_eos() -> _RetirementEvent:
        return _RetirementEvent()


class _RetirementGst:
    Event = _RetirementEventApi


class _CriticalGst(FakeGst):
    Event = _RetirementEventApi


class _RetirementPad:
    def __init__(self, outcome: bool | BaseException = True) -> None:
        self.outcome = outcome
        self.events: list[object] = []

    def send_event(self, event: object) -> bool:
        self.events.append(event)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class _BlockingRetirementPad(_RetirementPad):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def send_event(self, event: object) -> bool:
        self.events.append(event)
        self.entered.set()
        assert self.release.wait(1.0)
        return True


class _RetirementQueue:
    def __init__(
        self,
        parent: object,
        pad: _RetirementPad,
        *,
        stable_pad: bool = True,
    ) -> None:
        self.parent = parent
        self.pad = pad
        self.stable_pad = stable_pad

    def get_parent(self) -> object:
        return self.parent

    def get_static_pad(self, name: str) -> _RetirementPad:
        assert name == "sink"
        return self.pad if self.stable_pad else _RetirementPad()


def _video_retirement_fixture(
    *,
    outcome: bool | BaseException = True,
    linked: bool = False,
    opened_count: int = 1,
    registered: bool = True,
    correct_parent: bool = True,
    stable_pad: bool = True,
) -> tuple[
    PyGObjectGStreamerDriver,
    _GenerationPipeline,
    _RecordingGeneration,
    _RetirementPad,
]:
    generation_bin = object()
    pad = _RetirementPad(outcome)
    queue = _RetirementQueue(
        generation_bin if correct_parent else object(),
        pad,
        stable_pad=stable_pad,
    )
    generation = _driver_generation(
        2,
        queue=queue,
        generation_bin=generation_bin,
        linked=linked,
    )
    generation.activation_id = 2
    generation.opened = {
        f"/srv/dashcam/pending/boot-abcdef123456-{index:06d}.partial.mp4": index
        for index in range(opened_count)
    }
    context = _GenerationPipeline(
        object(),
        object(),
        "/srv/dashcam/pending",
        "abcdef123456",
        0,
        object(),
        object(),
        object(),
        object(),
        ({2: generation} if registered else {3: _driver_generation(3)}),
        threading.Lock(),
        {},
        deque(),
    )
    return (
        PyGObjectGStreamerDriver(_RetirementGst()),
        context,
        generation,
        pad,
    )


def test_retired_video_only_eos_targets_one_exact_inactive_slot_pad_once() -> None:
    driver, context, generation, pad = _video_retirement_fixture()

    dispatch = driver._start_retired_video_eos_dispatch(
        context,
        generation,
        "retiring-video-eos",
    )
    driver._await_retirement_dispatch(
        context,
        dispatch,
        time.monotonic() + 1.0,
    )

    assert len(pad.events) == 1
    assert isinstance(pad.events[0], _RetirementEvent)
    assert dispatch.pad is pad
    assert dispatch.activation_id == 2
    assert dispatch.eos_seqnum == 700
    assert dispatch.branch == "video"
    assert generation.video_retirement_eos_sent is True
    with pytest.raises(GStreamerDriverError, match="generation EOS has no exact A/V closure proof"):
        driver._start_retired_video_eos_dispatch(
            context,
            generation,
            "duplicate-video-eos",
        )


def test_retired_natural_av_eos_uses_the_ordinary_targeted_video_dispatch() -> None:
    driver, context, generation, pad = _video_retirement_fixture()
    generation.has_audio = True
    generation.audio_eos.arm_retirement()
    assert generation.audio_eos.observe_eos(699) is True

    dispatch = driver._start_retired_video_eos_dispatch(
        context,
        generation,
        "retiring-natural-av-video-eos",
    )
    driver._await_retirement_dispatch(
        context,
        dispatch,
        time.monotonic() + 1.0,
    )

    assert len(pad.events) == 1
    assert dispatch.eos_seqnum == 700
    assert generation.video_retirement_eos_sent is True
    assert generation.generation_retirement_eos_seqnum is None


@pytest.mark.parametrize("already_closed", [False, True])
def test_retired_exact_av_generation_eos_reuses_completed_closure_without_video_send(
    already_closed: bool,
) -> None:
    driver, context, generation, pad = _video_retirement_fixture()
    generation.has_audio = True
    generation.audio_eos.arm_retirement()
    generation.audio_eos.reserve_generation_eos(701)
    assert generation.audio_eos.observe_eos(701) is True
    generation.video_retirement_eos_sent = True
    generation.generation_retirement_eos_seqnum = 701
    if already_closed:
        location = next(iter(generation.opened))
        generation.opened.clear()
        generation.last_closed_location = location

    dispatch = driver._start_retired_video_eos_dispatch(
        context,
        generation,
        "retiring-av-generation-eos",
    )
    driver._await_retirement_dispatch(
        context,
        dispatch,
        time.monotonic() + 1.0,
    )

    assert pad.events == []
    assert dispatch.accepted is True
    assert dispatch.done.is_set() is True
    assert dispatch.thread.is_alive() is False
    assert dispatch.eos_seqnum == 701


@pytest.mark.parametrize(
    ("outcome", "message"),
    [
        (False, "video EOS dispatch was refused"),
        (
            RuntimeError("injected branch EOS failure"),
            "video EOS dispatch failed: injected branch EOS failure",
        ),
    ],
)
def test_retired_video_eos_refusal_or_error_fails_closed(
    outcome: bool | BaseException,
    message: str,
) -> None:
    driver, context, generation, _pad = _video_retirement_fixture(outcome=outcome)
    dispatch = driver._start_retired_video_eos_dispatch(
        context,
        generation,
        "retiring-video-eos",
    )

    with pytest.raises(GStreamerDriverError, match=message):
        driver._await_retirement_dispatch(
            context,
            dispatch,
            time.monotonic() + 1.0,
        )


def test_retired_video_eos_timeout_is_bounded() -> None:
    driver, context, generation, pad = _video_retirement_fixture()
    thread = threading.Thread(target=lambda: None)
    thread.start()
    thread.join()
    dispatch = _RetirementDispatch(
        "retiring-video-eos",
        thread,
        threading.Event(),
        generation=generation,
        activation_id=2,
        pad=pad,
        branch="video",
    )

    with pytest.raises(
        GStreamerDriverError,
        match="video EOS dispatch exceeded its deadline",
    ):
        driver._await_retirement_dispatch(
            context,
            dispatch,
            time.monotonic(),
        )


def test_retired_video_eos_refuses_activation_drift_after_dispatch() -> None:
    driver, context, generation, _pad = _video_retirement_fixture()
    dispatch = driver._start_retired_video_eos_dispatch(
        context,
        generation,
        "retiring-video-eos",
    )
    generation.activation_id = 3
    generation.video_retirement_eos_sent = True

    with pytest.raises(
        GStreamerDriverError,
        match="activation/pad ownership drifted",
    ):
        driver._await_retirement_dispatch(
            context,
            dispatch,
            time.monotonic() + 1.0,
        )


@pytest.mark.parametrize("residual_kind", ["opened", "ownership"])
def test_retirement_refuses_an_unexpected_successor_fragment(
    residual_kind: str,
) -> None:
    driver = PyGObjectGStreamerDriver(object())
    generation = _driver_generation(2)
    generation.activation_id = 7
    context = _GenerationPipeline(
        object(),
        object(),
        "/srv/dashcam/pending",
        "abcdef123456",
        0,
        object(),
        object(),
        object(),
        object(),
        {2: generation},
        threading.Lock(),
        {},
        deque(),
    )
    successor_location = "/srv/dashcam/pending/boot-abcdef123456-000008.partial.mp4"
    if residual_kind == "opened":
        generation.opened[successor_location] = 1
    else:
        context.location_generation[successor_location] = (2, 7)

    with pytest.raises(
        GStreamerDriverError,
        match="opened an unexpected successor fragment",
    ):
        driver._prove_retirement_has_no_successor_fragment(
            context,
            generation,
            time.sleep,
            time.monotonic() + 1.0,
        )


def test_retirement_quiet_proof_accepts_no_residual_fragment() -> None:
    generation = _driver_generation(2)
    generation.activation_id = 7
    context = _GenerationPipeline(
        object(),
        object(),
        "/srv/dashcam/pending",
        "abcdef123456",
        0,
        object(),
        object(),
        object(),
        object(),
        {2: generation},
        threading.Lock(),
        {},
        deque(),
    )

    PyGObjectGStreamerDriver._prove_retirement_has_no_successor_fragment(
        context,
        generation,
        time.sleep,
        time.monotonic() + 1.0,
    )


def test_retirement_quiet_proof_refuses_an_exhausted_deadline() -> None:
    generation = _driver_generation(2)
    generation.activation_id = 7
    context = _GenerationPipeline(
        object(),
        object(),
        "/srv/dashcam/pending",
        "abcdef123456",
        0,
        object(),
        object(),
        object(),
        object(),
        {2: generation},
        threading.Lock(),
        {},
        deque(),
    )

    with pytest.raises(
        GStreamerDriverError,
        match="observation exceeded its deadline",
    ):
        PyGObjectGStreamerDriver._prove_retirement_has_no_successor_fragment(
            context,
            generation,
            lambda _timeout: None,
            time.monotonic(),
        )


@pytest.mark.parametrize(
    ("linked", "opened_count", "registered"),
    [
        (True, 1, True),
        (False, 0, True),
        (False, 2, True),
        (False, 1, False),
    ],
)
def test_retired_video_eos_refuses_ambiguous_or_active_slot_ownership(
    linked: bool,
    opened_count: int,
    registered: bool,
) -> None:
    driver, context, generation, _pad = _video_retirement_fixture(
        linked=linked,
        opened_count=opened_count,
        registered=registered,
    )

    with pytest.raises(GStreamerDriverError, match="exact inactive open-slot"):
        driver._start_retired_video_eos_dispatch(
            context,
            generation,
            "retiring-video-eos",
        )


@pytest.mark.parametrize(
    ("correct_parent", "stable_pad", "message"),
    [
        (False, True, "queue ancestry differs"),
        (True, False, "sink pad identity is unstable"),
    ],
)
def test_retired_video_eos_refuses_wrong_queue_or_pad_identity(
    correct_parent: bool,
    stable_pad: bool,
    message: str,
) -> None:
    driver, context, generation, _pad = _video_retirement_fixture(
        correct_parent=correct_parent,
        stable_pad=stable_pad,
    )

    with pytest.raises(GStreamerDriverError, match=message):
        driver._start_retired_video_eos_dispatch(
            context,
            generation,
            "retiring-video-eos",
        )


class _RecycleBin:
    def __init__(self) -> None:
        self.locked = False

    def set_locked_state(self, locked: bool) -> bool:
        self.locked = locked
        return True


class _RecycleDriver(PyGObjectGStreamerDriver):
    fail_null = True

    def _set_and_verify_state(
        self,
        pipeline: object,
        state_name: str,
        timeout_s: float,
    ) -> None:
        assert state_name == "NULL"
        assert timeout_s == 1.0
        if self.fail_null:
            raise GStreamerDriverError("injected synchronous finalization failure")


def test_slot_reuse_remains_latched_until_sync_output_reaches_null() -> None:
    generation_bin = _RecycleBin()
    generation = _driver_generation(1, generation_bin=generation_bin)
    generation.retired = True
    generation.reusable = False
    generation.activation_id = 3
    generation.last_closed_location = "/srv/dashcam/pending/boot-abcdef123456-000002.partial.mp4"
    driver = _RecycleDriver(object())

    with pytest.raises(
        GStreamerDriverError,
        match="injected synchronous finalization failure",
    ):
        driver._recycle_generation(generation, 1.0)

    assert generation_bin.locked is True
    assert generation.retired is True
    assert generation.reusable is False
    assert generation.activation_id == 3
    assert generation.last_closed_location is not None

    driver.fail_null = False
    driver._recycle_generation(generation, 1.0)

    assert generation.retired is False
    assert generation.reusable is True
    assert generation.activation_id is None
    assert generation.last_closed_location is None
    assert generation.video_retirement_eos_sent is False


class _CleanupPad:
    def __init__(self, peer: object) -> None:
        self.peer = peer

    def get_peer(self) -> object | None:
        return self.peer

    def unlink(self, peer: object) -> bool:
        if self.peer is not peer:
            return False
        self.peer = None
        return True


class _CleanupQueue:
    def __init__(
        self,
        pad: _CleanupPad,
        sink_pad: _CleanupPad | None = None,
    ) -> None:
        self.pad = pad
        self.sink_pad = sink_pad

    def get_static_pad(self, name: str) -> _CleanupPad:
        if name == "src":
            return self.pad
        assert name == "sink"
        assert self.sink_pad is not None
        return self.sink_pad


class _CleanupOutput:
    def __init__(self) -> None:
        self.released: list[object] = []

    def release_request_pad(self, pad: object) -> None:
        self.released.append(pad)


class _CleanupTee:
    def __init__(self) -> None:
        self.released: list[object] = []

    def release_request_pad(self, pad: object) -> None:
        self.released.append(pad)


class _CleanupPipeline:
    def __init__(self) -> None:
        self.fail_first_remove = True
        self.removed: list[object] = []

    def remove(self, generation_bin: object) -> bool:
        if self.fail_first_remove:
            self.fail_first_remove = False
            return False
        self.removed.append(generation_bin)
        return True


class _CleanupDriver(PyGObjectGStreamerDriver):
    def _set_and_verify_state(self, pipeline: object, state_name: str, timeout_s: float) -> None:
        assert state_name == "NULL"
        assert timeout_s == 1


def test_generation_cleanup_is_retryable_and_context_is_retained_until_complete() -> None:
    driver = _CleanupDriver(object())
    pipeline = _CleanupPipeline()
    video_tee = _CleanupTee()
    audio_tee = _CleanupTee()
    continuity_tee_pad = _CleanupPad(object())
    continuity_queue_sink = _CleanupPad(continuity_tee_pad)
    continuity_tee_pad.peer = continuity_queue_sink
    continuity_queue = _CleanupQueue(
        _CleanupPad(object()),
        continuity_queue_sink,
    )
    generations: dict[int, _RecordingGeneration] = {}
    for generation_id in (1, 2):
        output_pad = object()
        output = _CleanupOutput()
        generation = _driver_generation(
            generation_id,
            queue=_CleanupQueue(_CleanupPad(output_pad)),
            output=output,
            output_pad=output_pad,
            tee_pad=object(),
            generation_bin=object(),
        )
        generations[generation_id] = generation
    context = _GenerationPipeline(
        pipeline,
        object(),
        "/srv/dashcam/pending",
        "abcdef123456",
        0,
        video_tee,
        audio_tee,
        object(),
        object(),
        generations,
        threading.Lock(),
        {},
        deque(),
        video_continuity_queue=continuity_queue,
        video_continuity_sink=object(),
        video_continuity_tee_pad=continuity_tee_pad,
    )
    driver._generation_pipelines[id(pipeline)] = context

    with pytest.raises(GStreamerDriverError, match="released after parent NULL"):
        driver.set_null(pipeline, 1)
    assert driver._generation_pipelines[id(pipeline)] is context
    assert generations[1].output_video_pad_released is True
    assert generations[1].removed_from_parent is False

    driver.set_null(pipeline, 1)
    assert id(pipeline) not in driver._generation_pipelines
    assert context.cleanup_complete is True
    assert context.video_continuity_tee_pad_released is True
    assert all(item.removed_from_parent for item in generations.values())
    assert len(video_tee.released) == 3
    assert video_tee.released.count(continuity_tee_pad) == 1


class _RoutingValve:
    def __init__(self, drop: bool) -> None:
        self.drop = drop

    def get_property(self, name: str) -> bool:
        assert name == "drop"
        return self.drop


def _critical_loss_shutdown_fixture() -> tuple[
    PyGObjectGStreamerDriver,
    _GenerationPipeline,
    _RecordingGeneration,
    _RecordingGeneration,
    _RetirementPad,
]:
    pipeline = FakeGstPipeline(FakeElement(), FakeBus([]))
    gst = _CriticalGst(pipeline)
    generation_bin = object()
    retirement_pad = _RetirementPad()
    retiring_output = FakeElement()
    retiring = _driver_generation(
        1,
        has_audio=True,
        linked=False,
        valve=_RoutingValve(True),
        queue=_RetirementQueue(generation_bin, retirement_pad),
        output=retiring_output,
        generation_bin=generation_bin,
    )
    retiring.activation_id = 1
    location = "/srv/dashcam/pending/boot-abcdef123456-000001.partial.mp4"
    retiring.opened = {location: 60_000_000_000}
    retiring.audio_eos.arm_retirement()
    assert retiring.audio_eos.observe_eos(701) is True
    successor = _driver_generation(
        2,
        linked=True,
        valve=_RoutingValve(True),
    )
    successor.activation_id = 2
    standby = _driver_generation(3, linked=False, valve=_RoutingValve(True))
    context = _GenerationPipeline(
        pipeline,
        gst,
        "/srv/dashcam/pending",
        "abcdef123456",
        2,
        object(),
        object(),
        object(),
        object(),
        {1: retiring, 2: successor, 3: standby},
        threading.Lock(),
        {location: (1, 1)},
        deque(),
        active_generation_id=1,
        routing_phase="LOSS_CONTAINMENT_CRITICAL",
    )
    driver = PyGObjectGStreamerDriver(gst)
    driver._generation_pipelines[id(pipeline)] = context
    return driver, context, retiring, successor, retirement_pad


def test_critical_loss_shutdown_closes_only_exact_retired_av_fragment() -> None:
    driver, context, retiring, successor, retirement_pad = (
        _critical_loss_shutdown_fixture()
    )

    assert driver.send_eos(context.pipeline) is True

    assert len(retirement_pad.events) == 1
    assert isinstance(retirement_pad.events[0], _RetirementEvent)
    assert retiring.video_retirement_eos_sent is True
    assert retiring.linked is False
    assert successor.linked is True
    assert successor.opened == {}
    assert context.active_generation_id == 1
    assert context.routing_phase == "LOSS_CONTAINMENT_CRITICAL"

    location = next(iter(retiring.opened))
    context.pipeline.bus.messages.append(
        FakeGstMessage(
            FakeMessageType.ELEMENT,
            FakeStructure(
                "splitmuxsink-fragment-closed",
                {"location": location, "running-time": 61_000_000_000},
            ),
            src=retiring.output,
        )
    )
    closed = driver.poll_bus(context.pipeline, 0.1)
    assert closed.kind is BusMessageKind.FRAGMENT_FINALIZED
    assert closed.fragment is not None
    assert closed.fragment.location == location
    assert closed.fragment.media_contract == FragmentMediaContract(
        1,
        EffectiveAudioCaps(
            "S16LE",
            48_000,
            1,
            "aac",
            4,
            "raw",
            "voaacenc",
            "aacparse",
            128_000,
        ),
        0,
    )
    assert retiring.last_closed_location == location
    assert retiring.opened == {}
    assert context.location_generation == {}
    assert successor.opened == {}


def test_critical_loss_shutdown_rejects_stale_successor_closure_identity() -> None:
    driver, context, retiring, successor, _retirement_pad = (
        _critical_loss_shutdown_fixture()
    )
    assert driver.send_eos(context.pipeline) is True
    location = next(iter(retiring.opened))
    context.pipeline.bus.messages.append(
        FakeGstMessage(
            FakeMessageType.ELEMENT,
            FakeStructure(
                "splitmuxsink-fragment-closed",
                {"location": location, "running-time": 61_000_000_000},
            ),
            src=successor.output,
        )
    )

    with pytest.raises(
        GStreamerDriverError,
        match="activation/source identity differs",
    ):
        driver.poll_bus(context.pipeline, 0.1)

    assert retiring.opened == {location: 60_000_000_000}
    assert retiring.last_closed_location is None
    assert successor.opened == {}


def test_critical_loss_retirement_worker_is_bounded_by_parent_null() -> None:
    driver, context, retiring, _successor, _retirement_pad = (
        _critical_loss_shutdown_fixture()
    )
    blocking = _BlockingRetirementPad()
    retiring.video_queue = _RetirementQueue(retiring.bin, blocking)
    assert driver._critical_loss_shutdown_generation(context) is retiring
    dispatch = driver._start_retired_video_eos_dispatch(
        context,
        retiring,
        "critical-loss-retired-av-video-eos",
    )
    assert blocking.entered.wait(0.2)
    driver._set_and_verify_state = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    try:
        with pytest.raises(
            GStreamerDriverError,
            match="retirement worker survived parent NULL",
        ):
            driver.set_null(context.pipeline, 0.02)
    finally:
        blocking.release.set()
        dispatch.thread.join(0.2)
    assert dispatch.done.is_set()


@pytest.mark.parametrize(
    ("drift", "message"),
    [
        ("missing_retired_location", "retired A/V provenance"),
        ("foreign_location_owner", "location ownership differs"),
        ("successor_opened", "successor ownership differs"),
        ("successor_open", "successor ownership differs"),
        ("second_linked_slot", "unique closed successor"),
        ("missing_audio_boundary", "retired A/V provenance"),
    ],
)
def test_critical_loss_shutdown_refuses_ambiguous_provenance(
    drift: str,
    message: str,
) -> None:
    driver, context, retiring, successor, retirement_pad = (
        _critical_loss_shutdown_fixture()
    )
    location = next(iter(retiring.opened))
    if drift == "missing_retired_location":
        retiring.opened.clear()
    elif drift == "foreign_location_owner":
        context.location_generation[location] = (2, 2)
    elif drift == "successor_opened":
        successor.opened["/srv/dashcam/pending/foreign.partial.mp4"] = 2
    elif drift == "successor_open":
        successor.video_valve = _RoutingValve(False)
    elif drift == "second_linked_slot":
        context.generations[3].linked = True
    else:
        retiring.audio_eos = _AudioEosArbiter()

    with pytest.raises(GStreamerDriverError, match=message):
        driver.send_eos(context.pipeline)

    assert retirement_pad.events == []
    assert retiring.video_retirement_eos_sent is False


def test_partial_handoff_shutdown_routes_by_actual_successor_topology() -> None:
    driver = PyGObjectGStreamerDriver(object())
    initial = _driver_generation(1, linked=False, valve=_RoutingValve(True))
    successor = _driver_generation(2, linked=True, valve=_RoutingValve(False))
    context = _GenerationPipeline(
        object(),
        object(),
        "/srv/dashcam/pending",
        "abcdef123456",
        0,
        object(),
        object(),
        object(),
        object(),
        {1: initial, 2: successor},
        threading.Lock(),
        {},
        deque(),
        routing_phase="SWITCHING",
    )

    assert driver._shutdown_generation(context) is successor
    assert context.active_generation_id == 2
    assert context.routing_phase == "VIDEO_ONLY_CLOSING"


class _ForceEvent:
    def __init__(
        self,
        count: int,
        *,
        seqnum: int,
        all_headers: bool = True,
        running_time_ns: int = 60_000_000_000,
    ) -> None:
        self.count = count
        self.seqnum = seqnum
        self.all_headers = all_headers
        self.running_time_ns = running_time_ns

    def get_seqnum(self) -> int:
        return self.seqnum


class _ForceInfo:
    def __init__(self, value: object, *, event: bool) -> None:
        self.value = value
        self.event = event

    def get_event(self) -> object | None:
        return self.value if self.event else None

    def get_buffer(self) -> object | None:
        return None if self.event else self.value


class _ForceBuffer:
    def __init__(
        self,
        *,
        pts: int = 60_000_000_000,
        delta: bool = False,
        payload: bytes = b"\x00\x00\x00\x01\x65\xaa",
    ) -> None:
        self.pts = pts
        self.delta = delta
        self.payload = payload

    def has_flags(self, _flag: object) -> bool:
        return self.delta

    def map(self, _flags: object) -> tuple[bool, object]:
        return True, SimpleNamespace(data=self.payload)

    def unmap(self, _map_info: object) -> None:
        return None


class _ForceSegment:
    def __init__(self, offset_ns: int) -> None:
        self.offset_ns = offset_ns

    def to_running_time(self, _format: object, pts: int) -> int:
        return pts - self.offset_ns


class _ForceSegmentEvent:
    def __init__(self, offset_ns: int) -> None:
        self.segment = _ForceSegment(offset_ns)

    def parse_segment(self) -> _ForceSegment:
        return self.segment


class _ForcePad:
    def __init__(self, parent: object, *, segment_offset_ns: int | None = None) -> None:
        self.parent = parent
        self.peer: object | None = None
        self.segment_offset_ns = segment_offset_ns
        self.probes: dict[int, Callable[[object, object], object]] = {}
        self.probe_types: dict[int, object] = {}
        self.removed: list[int] = []
        self._next_probe = 1

    def get_parent(self) -> object:
        return self.parent

    def get_peer(self) -> object | None:
        return self.peer

    def add_probe(
        self,
        probe_type: object,
        callback: Callable[[object, object], object],
    ) -> int:
        probe_id = self._next_probe
        self._next_probe += 1
        self.probes[probe_id] = callback
        self.probe_types[probe_id] = probe_type
        return probe_id

    def remove_probe(self, probe_id: int) -> None:
        self.removed.append(probe_id)
        self.probes.pop(probe_id, None)
        self.probe_types.pop(probe_id, None)

    def is_blocked(self) -> bool:
        return False

    def is_blocking(self) -> bool:
        return False

    def get_sticky_event(self, _event_type: object, _index: int) -> _ForceSegmentEvent | None:
        return (
            None
            if self.segment_offset_ns is None
            else _ForceSegmentEvent(self.segment_offset_ns)
        )


class _ForceSourcePad(_ForcePad):
    def __init__(self, parent: object, flow: _ForceFlow) -> None:
        super().__init__(parent)
        self.flow = flow

    def send_event(self, event: _ForceEvent) -> bool:
        return self.flow.dispatch(event)


class _ImmediateIdlePad(_ForcePad):
    def __init__(self, parent: object, order: list[str]) -> None:
        super().__init__(parent)
        self.order = order

    def add_probe(
        self,
        probe_type: object,
        callback: Callable[[object, object], object],
    ) -> int:
        probe_id = super().add_probe(probe_type, callback)
        self.order.append("audio_idle")
        callback(self, object())
        return probe_id


class _ForceElement:
    def __init__(self, pipeline: _ForcePipeline | None = None) -> None:
        self.pipeline = pipeline
        self.src: _ForcePad | None = None
        self.sink: _ForcePad | None = None
        self.properties: dict[str, object] = {"drop": True}

    def get_parent(self) -> object | None:
        return self.pipeline

    def get_static_pad(self, name: str) -> _ForcePad | None:
        return self.src if name == "src" else self.sink

    def set_property(self, name: str, value: object) -> None:
        self.properties[name] = value

    def get_property(self, name: str) -> object:
        return self.properties[name]


class _ForcePipeline:
    def __init__(self) -> None:
        self.elements: dict[str, object] = {}

    def get_by_name(self, name: str) -> object | None:
        return self.elements.get(name)


class _ForceVideo:
    def __init__(self, flow: _ForceFlow) -> None:
        self.flow = flow

    def video_event_new_upstream_force_key_unit(
        self,
        _running_time: int,
        all_headers: bool,
        count: int,
    ) -> _ForceEvent:
        assert all_headers is True
        return _ForceEvent(count, seqnum=100)

    @staticmethod
    def video_event_is_force_key_unit(event: object) -> bool:
        return isinstance(event, _ForceEvent)

    @staticmethod
    def video_event_parse_downstream_force_key_unit(
        event: _ForceEvent,
    ) -> tuple[bool, int, int, int, bool, int]:
        return (
            True,
            event.running_time_ns,
            event.running_time_ns,
            event.running_time_ns,
            event.all_headers,
            event.count,
        )


class _ForceFlow:
    def __init__(
        self,
        *,
        response_count: int | None = 1,
        all_headers: bool = True,
        send_accepted: bool = True,
        buffer: _ForceBuffer | None = None,
        reentrant: bool = False,
        response_seqnum: int = 101,
        response_running_time_ns: int = 60_000_000_000,
        send_blocked: bool = False,
        stream_lock_deadlock_on_block_probe: bool = False,
        send_error: BaseException | None = None,
    ) -> None:
        self.response_count = response_count
        self.all_headers = all_headers
        self.send_accepted = send_accepted
        self.buffer = buffer or _ForceBuffer()
        self.reentrant = reentrant
        self.response_seqnum = response_seqnum
        self.response_running_time_ns = response_running_time_ns
        self.send_blocked = send_blocked
        self.stream_lock_deadlock_on_block_probe = stream_lock_deadlock_on_block_probe
        self.send_error = send_error
        self.send_release = threading.Event()
        self.dispatch_thread_ident: int | None = None
        self.block_probe_present_before_downstream_event = False
        self.block_probe_present_after_downstream_event = False
        self.event_pad: _ForcePad | None = None
        self.parser_pad: _ForcePad | None = None
        self.video_pad: _ForcePad | None = None
        self.thread: threading.Thread | None = None

    def dispatch(self, _request: _ForceEvent) -> bool:
        self.dispatch_thread_ident = threading.get_ident()
        if self.send_error is not None:
            raise self.send_error
        if self.send_blocked:
            self.send_release.wait(1.0)
        if self.stream_lock_deadlock_on_block_probe:
            assert self.video_pad is not None
            self.block_probe_present_before_downstream_event = any(
                cast(int, probe_type) & _ForceGst.PadProbeType.BLOCK
                for probe_type in self.video_pad.probe_types.values()
            )
            if self.block_probe_present_before_downstream_event:
                # Model GstPad's stream-lock cycle: a proactive BLOCK probe
                # owns the streaming thread while upstream send_event waits
                # for that same stream lock.
                self.send_release.wait(1.0)
        if not self.send_accepted:
            return False
        if self.response_count is None:
            return True
        assert self.event_pad is not None and self.video_pad is not None
        video_pad = self.video_pad
        event_probe_id, event_callback = next(iter(self.event_pad.probes.items()))
        event_result = event_callback(
            self.event_pad,
            _ForceInfo(
                _ForceEvent(
                    self.response_count,
                    seqnum=self.response_seqnum,
                    all_headers=self.all_headers,
                    running_time_ns=self.response_running_time_ns,
                ),
                event=True,
            ),
        )
        if event_result == _ForceGst.PadProbeReturn.REMOVE:
            self.event_pad.remove_probe(event_probe_id)
        if self.stream_lock_deadlock_on_block_probe:
            self.block_probe_present_after_downstream_event = any(
                cast(int, probe_type) & _ForceGst.PadProbeType.BLOCK
                for probe_type in video_pad.probe_types.values()
            )
            if self.block_probe_present_after_downstream_event:
                # Model the second target deadlock: adding a BLOCK probe from
                # the downstream event callback prevents that callback/event
                # from completing and the corresponding IDR cannot follow.
                self.send_release.wait(1.0)
        buffer_probe = next(iter(video_pad.probes.items()), None)
        if buffer_probe is None:
            return True
        buffer_probe_id, buffer_callback = buffer_probe

        def deliver_buffer() -> None:
            result = buffer_callback(
                video_pad,
                _ForceInfo(self.buffer, event=False),
            )
            if result == _ForceGst.PadProbeReturn.REMOVE:
                video_pad.remove_probe(buffer_probe_id)

        if self.reentrant:
            deliver_buffer()
            return True
        self.thread = threading.Thread(
            target=deliver_buffer,
            daemon=True,
        )
        self.thread.start()
        return True


class _ForceGst:
    CLOCK_TIME_NONE = 2**64 - 1
    PadProbeType = SimpleNamespace(EVENT_DOWNSTREAM=1, BLOCK=2, BUFFER=4, IDLE=8)
    PadProbeReturn = SimpleNamespace(OK=1, PASS=2, REMOVE=3)
    BufferFlags = SimpleNamespace(DELTA_UNIT=1)
    MapFlags = SimpleNamespace(READ=1)
    EventType = SimpleNamespace(SEGMENT=1)
    Format = SimpleNamespace(TIME=1)


def _forced_idr_fixture(
    *,
    edge_skew_ns: int = 99_999_999,
    response_count: int | None = 1,
    all_headers: bool = True,
    send_accepted: bool = True,
    buffer: _ForceBuffer | None = None,
    audio_timing_error: str | None = None,
    reentrant: bool = False,
    audio_end_present: bool = True,
    segment_offset_ns: int = 0,
    response_seqnum: int = 101,
    response_running_time_ns: int = 60_000_000_000,
    send_blocked: bool = False,
    stream_lock_deadlock_on_block_probe: bool = False,
    send_error: BaseException | None = None,
) -> tuple[
    PyGObjectGStreamerDriver,
    _GenerationPipeline,
    _RecordingGeneration,
    _ForceFlow,
]:
    flow = _ForceFlow(
        response_count=response_count,
        all_headers=all_headers,
        send_accepted=send_accepted,
        buffer=buffer,
        reentrant=reentrant,
        response_seqnum=response_seqnum,
        response_running_time_ns=response_running_time_ns,
        send_blocked=send_blocked,
        stream_lock_deadlock_on_block_probe=stream_lock_deadlock_on_block_probe,
        send_error=send_error,
    )
    pipeline = _ForcePipeline()
    encoder = _ForceElement(pipeline)
    parser = _ForceElement(pipeline)
    video_tee = _ForceElement(pipeline)
    flow.buffer.pts += segment_offset_ns
    encoder.src = _ForceSourcePad(encoder, flow)
    parser.src = _ForcePad(parser)
    flow.event_pad = encoder.src
    flow.parser_pad = parser.src
    pipeline.elements = {"encoder": encoder, "parser": parser}
    generation = _driver_generation(1, has_audio=False, linked=True)
    video_valve = _ForceElement()
    video_valve.pipeline = generation.bin
    video_valve.sink = _ForcePad(video_valve)
    video_gate_queue = _ForceElement()
    video_gate_queue.pipeline = generation.bin
    video_gate_queue.src = _ForcePad(
        video_gate_queue,
        segment_offset_ns=segment_offset_ns,
    )
    video_gate_queue.src.peer = video_valve.sink
    video_valve.sink.peer = video_gate_queue.src
    generation.video_valve = video_valve
    generation.video_gate_queue = video_gate_queue
    flow.video_pad = video_gate_queue.src
    generation.activation_id = 1
    generation.last_audio_end_running_time_ns = (
        60_000_000_000 - edge_skew_ns if audio_end_present else None
    )
    generation.streaming_error = audio_timing_error
    context = _GenerationPipeline(
        pipeline,
        _ForceGst(),
        "/srv/dashcam/pending",
        "abcdef123456",
        0,
        video_tee,
        object(),
        object(),
        encoder,
        {1: generation},
        threading.Lock(),
        {},
        deque(),
    )
    driver = PyGObjectGStreamerDriver(_ForceGst(), _ForceVideo(flow))
    driver._generation_pipelines[id(pipeline)] = context
    driver._preserve_handoff_bus = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    return driver, context, generation, flow


def _release_forced_gate(
    driver: PyGObjectGStreamerDriver,
    gate: object,
    flow: _ForceFlow,
) -> None:
    if gate.event_probe_id is not None:  # type: ignore[attr-defined]
        driver._remove_retained_probe(
            gate.event_pad,  # type: ignore[attr-defined]
            gate.event_probe_id,  # type: ignore[attr-defined]
            0.2,
        )
    driver._release_block_probe(
        gate.video_pad,  # type: ignore[attr-defined]
        gate.video_probe_id,  # type: ignore[attr-defined]
        reached=gate.reached,  # type: ignore[attr-defined]
        completed=gate.completed,  # type: ignore[attr-defined]
        release=gate.release,  # type: ignore[attr-defined]
        timeout_s=0.2,
    )
    if flow.thread is not None and flow.thread.ident is not None:
        flow.thread.join(0.2)


def _finalize_forced_gate(
    driver: PyGObjectGStreamerDriver,
    generation: _RecordingGeneration,
    gate: object,
) -> ForcedIdrProof:
    audio_end = generation.last_audio_end_running_time_ns
    assert audio_end is not None
    return driver._finalize_forced_idr_gate(
        generation,
        gate,  # type: ignore[arg-type]
        audio_end,
    )


def test_forced_idr_gate_correlates_count_not_seqnum_and_accepts_strict_edge() -> None:
    driver, context, generation, flow = _forced_idr_fixture()
    gate = driver._arm_forced_idr_gate(
        context,
        generation,
        deadline=time.monotonic() + 0.2,
        timeout_s=0.2,
    )
    try:
        proof = _finalize_forced_gate(driver, generation, gate)
        assert proof.request_count == 1
        assert proof.request_seqnum == 100
        assert proof.downstream_seqnum == 101
        assert proof.seqnum_preserved is False
        assert proof.all_headers is True
        assert proof.nal5 is True
        assert gate.event_probe_id is None
        assert proof.edge_skew_ns == 99_999_999
        assert context.next_force_key_count == 2
        assert gate.dispatch.thread is None
        assert gate.dispatch.caller_thread_ident == threading.get_ident()
        assert flow.dispatch_thread_ident == threading.get_ident()
        assert flow.thread is not None
        assert flow.event_pad is gate.event_pad
        assert flow.parser_pad is not None and flow.parser_pad.probes == {}
    finally:
        _release_forced_gate(driver, gate, flow)


def test_forced_idr_gate_reentrant_send_refuses_after_bounded_hold_timeout() -> None:
    driver, context, generation, flow = _forced_idr_fixture(reentrant=True)
    started = time.monotonic()
    with pytest.raises(GStreamerDriverError, match="held forced-IDR release wait timed out"):
        driver._arm_forced_idr_gate(
            context,
            generation,
            deadline=time.monotonic() + 0.2,
            timeout_s=0.05,
        )
    assert 0.04 <= time.monotonic() - started < 0.2
    assert flow.event_pad is not None and flow.event_pad.probes == {}
    assert flow.parser_pad is not None and flow.parser_pad.probes == {}
    assert flow.video_pad is not None and flow.video_pad.probes == {}
    assert len(context.force_key_dispatches) == 1
    dispatch = context.force_key_dispatches[0]
    assert dispatch.done.is_set()
    assert dispatch.thread is None
    assert dispatch.accepted is True


def test_forced_idr_gate_does_not_preblock_encoder_stream_lock() -> None:
    driver, context, generation, flow = _forced_idr_fixture(
        stream_lock_deadlock_on_block_probe=True,
    )
    gate = driver._arm_forced_idr_gate(
        context,
        generation,
        deadline=time.monotonic() + 0.2,
        timeout_s=0.2,
    )
    try:
        assert flow.video_pad is not None
        assert flow.block_probe_present_before_downstream_event is False
        assert flow.block_probe_present_after_downstream_event is False
        assert flow.video_pad.probe_types[gate.video_probe_id] == _ForceGst.PadProbeType.BUFFER
        assert gate.reached.is_set()
        assert _finalize_forced_gate(driver, generation, gate).nal5 is True
    finally:
        _release_forced_gate(driver, gate, flow)


def test_forced_idr_gate_can_arm_before_old_route_unblocks_response() -> None:
    driver, context, generation, flow = _forced_idr_fixture(response_count=None)

    gate = driver._arm_forced_idr_gate(
        context,
        generation,
        deadline=time.monotonic() + 0.2,
        timeout_s=0.2,
        await_response=False,
    )
    assert gate.held is None
    assert gate.reached.is_set() is False

    flow.response_count = 1
    assert flow.dispatch(_ForceEvent(1, seqnum=100)) is True
    driver._await_forced_idr_gate(
        context,
        generation,
        gate,
        time.monotonic() + 0.2,
    )
    try:
        assert gate.held is not None
        assert gate.held.request_count == 1
    finally:
        _release_forced_gate(driver, gate, flow)


def test_forced_idr_gate_compares_segment_running_time_not_raw_video_pts() -> None:
    driver, context, generation, flow = _forced_idr_fixture(
        segment_offset_ns=7_000_000_000,
    )
    gate = driver._arm_forced_idr_gate(
        context,
        generation,
        deadline=time.monotonic() + 0.2,
        timeout_s=0.2,
    )
    try:
        proof = _finalize_forced_gate(driver, generation, gate)
        assert flow.buffer.pts == 67_000_000_000
        assert proof.forced_idr_running_time_ns == 60_000_000_000
        assert proof.edge_skew_ns == 99_999_999
    finally:
        _release_forced_gate(driver, gate, flow)


def test_forced_idr_after_old_unlink_binds_dispatch_to_closed_linked_successor() -> None:
    driver, context, old, flow = _forced_idr_fixture(edge_skew_ns=50_000_000)
    old.linked = False
    successor = _driver_generation(
        2,
        linked=True,
        valve=old.video_valve,
        generation_bin=old.bin,
    )
    successor.video_gate_queue = old.video_gate_queue
    successor.activation_id = 2
    context.generations[2] = successor

    gate = driver._arm_forced_idr_gate(
        context,
        successor,
        deadline=time.monotonic() + 0.2,
        timeout_s=0.2,
    )
    try:
        proof = _finalize_forced_gate(driver, old, gate)
        assert gate.dispatch.activation_id == 2
        assert gate.dispatch.caller_thread_ident == threading.get_ident()
        assert context.generations[1].linked is False
        assert context.generations[2].linked is True
        assert proof.last_audio_end_running_time_ns == old.last_audio_end_running_time_ns
    finally:
        _release_forced_gate(driver, gate, flow)
    driver._await_force_key_dispatch(
        context,
        successor,
        gate.dispatch,
        time.monotonic() + 0.2,
    )


@pytest.mark.parametrize("force_outcome", ["dispatch_refused", "timeout", "held"])
def test_audio_loss_holds_forced_idr_before_retiring_audio_route(
    force_outcome: str,
) -> None:
    driver, context, generation, _flow = _forced_idr_fixture()
    pipeline = context.pipeline
    generation.audio_eos.arm_retirement()
    order: list[str] = []
    audio_valve = _ForceElement()
    audio_valve.src = _ImmediateIdlePad(audio_valve, order)
    audio_valve.set_property("drop", False)
    generation.audio_valve = audio_valve
    generation.audio_queue = object()
    generation.opened["/srv/dashcam/pending/active.mp4"] = 1
    context.audio_ingress_quarantine = _AudioIngressQuarantine(
        object(),
        object(),
        1,
    )
    context.generations[2] = _driver_generation(2)
    context.generations[3] = _driver_generation(3)

    force_gate = SimpleNamespace(
        held=SimpleNamespace(
            request_count=1,
            forced_idr_running_time_ns=60_000_000_000,
        ),
        request_count=1,
        dispatch=SimpleNamespace(request_count=1),
        event_probe_id=None,
        video_pad=object(),
        video_probe_id=1,
        reached=threading.Event(),
        completed=threading.Event(),
        release=threading.Event(),
    )

    def stop_after_force(*_args: object, **_kwargs: object) -> object:
        order.append("force")
        return force_gate

    def await_force(*_args: object, **_kwargs: object) -> None:
        order.append("await_force")
        if force_outcome == "timeout":
            raise GStreamerDriverError("forced-IDR response/IDR wait timed out")

    def await_dispatch(*_args: object, **_kwargs: object) -> None:
        order.append("await_dispatch")
        if force_outcome == "dispatch_refused":
            raise GStreamerDriverError("encoder source refused forced-IDR request")

    def establish_boundary(*_args: object, **_kwargs: object) -> str:
        order.append("audio_boundary")
        assert generation.audio_eos.observe_eos(700) is True
        return "NATURAL"

    def set_linked(
        _context: _GenerationPipeline,
        target: _RecordingGeneration,
        linked: bool,
    ) -> None:
        order.append(f"link_{target.generation_id}_{str(linked).lower()}")
        target.linked = linked

    def set_open(target: _RecordingGeneration, opened: bool) -> None:
        order.append(f"open_{target.generation_id}_{str(opened).lower()}")
        if target.audio_valve is not None:
            target.audio_valve.set_property("drop", not opened)
        if target.generation_id == 2 and opened:
            raise GStreamerDriverError("successor opened after held IDR")

    def finalize_proof(*_args: object, **_kwargs: object) -> ForcedIdrProof:
        order.append("proof")
        assert generation.last_audio_end_running_time_ns is not None
        return forced_idr_proof(
            edge_skew_ns=(
                60_000_000_000 - generation.last_audio_end_running_time_ns
            )
        )

    def drain_and_arm(
        _queue: object,
        _deadline: float,
        *,
        on_first_empty: Callable[[], None] | None = None,
    ) -> None:
        order.append("audio_drain")
        if on_first_empty is not None:
            on_first_empty()

    driver._prewarm_generation = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: order.append("prewarm")
    )
    driver._wait_for_audio_queue_drain = drain_and_arm  # type: ignore[assignment]
    driver._establish_audio_retirement_boundary = (  # type: ignore[method-assign]
        establish_boundary
    )
    driver._set_generation_linked = set_linked  # type: ignore[assignment]
    driver._set_generation_open = set_open  # type: ignore[assignment]
    driver._arm_forced_idr_gate = stop_after_force  # type: ignore[assignment]
    driver._await_force_key_dispatch = await_dispatch  # type: ignore[method-assign]
    driver._await_forced_idr_gate = await_force  # type: ignore[method-assign]
    driver._finalize_forced_idr_gate = finalize_proof  # type: ignore[method-assign]
    driver._release_block_probe = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    driver._reset_unrouted_generation = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

    expected_error = {
        "dispatch_refused": "encoder source refused forced-IDR request",
        "timeout": "forced-IDR response/IDR wait timed out",
        "held": "successor opened after held IDR",
    }[force_outcome]
    with pytest.raises(GStreamerDriverError, match=expected_error):
        driver.isolate_audio_loss(pipeline, 0.2)

    expected_order = [
        "prewarm",
        "link_2_true",
        "force",
        "await_dispatch",
    ]
    if force_outcome == "dispatch_refused":
        expected_order.append("link_2_false")
    else:
        expected_order.extend(["audio_idle", "audio_drain", "open_1_false"])
    if force_outcome == "held":
        expected_order.extend(
            [
                "await_force",
                "proof",
                "audio_boundary",
                "audio_drain",
                "link_1_false",
                "open_2_true",
            ]
        )
    elif force_outcome == "timeout":
        expected_order.extend(
            ["await_force", "open_1_false", "link_1_false"]
        )
    assert order == expected_order
    assert (context.routing_phase == "LOSS_CONTAINMENT_CRITICAL") is (
        force_outcome != "dispatch_refused"
    )
    assert context.generations[1].linked is (
        force_outcome == "dispatch_refused"
    )
    assert context.generations[2].linked is (
        force_outcome != "dispatch_refused"
    )
    assert ("open_1_false" in order) is (force_outcome != "dispatch_refused")
    assert ("open_2_true" in order) is (force_outcome == "held")
    assert audio_valve.src.probes == {}


@pytest.mark.parametrize(
    (
        "first_edge_ns",
        "retry_edge_ns",
        "expected_requests",
        "expected_final_edge_ns",
        "expected_error",
    ),
    [
        (50_000_000, None, 1, 50_000_000, "stop after forced proof"),
        (115_252_589, None, 1, None, "100 ms production bound"),
        (-1, 50_000_000, 2, 50_000_000, "stop after forced proof"),
        (-1, 115_252_589, 2, None, "100 ms production bound"),
    ],
)
def test_audio_loss_early_force_has_one_bounded_negative_edge_retry(
    first_edge_ns: int,
    retry_edge_ns: int | None,
    expected_requests: int,
    expected_final_edge_ns: int | None,
    expected_error: str,
) -> None:
    driver, context, generation, _flow = _forced_idr_fixture()
    pipeline = context.pipeline
    generation.audio_eos.arm_retirement()
    frozen_audio_end = 195_069_333_333
    generation.last_audio_end_running_time_ns = frozen_audio_end
    audio_valve = _ForceElement()
    audio_valve.src = _ImmediateIdlePad(audio_valve, [])
    audio_valve.set_property("drop", False)
    generation.audio_valve = audio_valve
    generation.audio_queue = object()
    generation.opened["/srv/dashcam/pending/active.mp4"] = 1
    context.audio_ingress_quarantine = _AudioIngressQuarantine(object(), object(), 1)
    context.generations[2] = _driver_generation(2)
    context.generations[3] = _driver_generation(3)
    requested: list[int] = []
    discarded: list[int] = []
    finalized: list[ForcedIdrProof] = []
    edges = [first_edge_ns]
    if retry_edge_ns is not None:
        edges.append(retry_edge_ns)

    def forced_gate(edge_ns: int, count: int) -> _ForcedIdrGate:
        forced_running = frozen_audio_end + edge_ns
        return cast(
            _ForcedIdrGate,
            SimpleNamespace(
                held=_HeldForcedIdr(
                    request_count=count,
                    request_seqnum=100 + count,
                    downstream_seqnum=200 + count,
                    request_monotonic_ns=1_000 + count * 10,
                    downstream_event_monotonic_ns=1_001 + count * 10,
                    idr_arrival_monotonic_ns=1_002 + count * 10,
                    downstream_running_time_ns=forced_running,
                    forced_idr_running_time_ns=forced_running,
                    event_to_idr_media_ns=0,
                ),
                proof=None,
                request_count=count,
                dispatch=SimpleNamespace(request_count=count),
                event_probe_id=None,
                video_probe_id=None,
                release=threading.Event(),
                reached=threading.Event(),
                completed=threading.Event(),
                failed=threading.Event(),
                observed={},
            ),
        )

    def arm(*_args: object, **_kwargs: object) -> _ForcedIdrGate:
        count = len(requested) + 1
        if count > len(edges):
            raise AssertionError("production attempted an unbounded force-key retry")
        requested.append(count)
        return forced_gate(edges[count - 1], count)

    def discard(
        _context: _GenerationPipeline,
        _successor: _RecordingGeneration,
        gate: _ForcedIdrGate,
        _deadline: float,
    ) -> None:
        discarded.append(cast(_HeldForcedIdr, gate.held).request_count)
        gate.release.set()

    original_finalize = driver._finalize_forced_idr_gate

    def finalize(
        old: _RecordingGeneration,
        gate: _ForcedIdrGate,
        audio_end: int,
    ) -> ForcedIdrProof:
        proof = original_finalize(old, gate, audio_end)
        finalized.append(proof)
        return proof

    def drain(
        _queue: object,
        _deadline: float,
        *,
        on_first_empty: Callable[[], None] | None = None,
    ) -> None:
        if on_first_empty is not None:
            on_first_empty()

    def stop_after_proof(*_args: object, **_kwargs: object) -> str:
        raise GStreamerDriverError("stop after forced proof")

    def set_linked(
        _context: _GenerationPipeline,
        target: _RecordingGeneration,
        linked: bool,
    ) -> None:
        target.linked = linked

    def set_open(target: _RecordingGeneration, opened: bool) -> None:
        if target.audio_valve is not None:
            target.audio_valve.set_property("drop", not opened)

    driver._prewarm_generation = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    driver._set_generation_linked = set_linked  # type: ignore[assignment]
    driver._set_generation_open = set_open  # type: ignore[assignment]
    driver._arm_forced_idr_gate = arm  # type: ignore[method-assign]
    driver._await_force_key_dispatch = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    driver._await_forced_idr_gate = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    driver._discard_early_forced_idr_gate = discard  # type: ignore[assignment]
    driver._finalize_forced_idr_gate = finalize  # type: ignore[assignment]
    driver._wait_for_audio_queue_drain = drain  # type: ignore[assignment]
    driver._establish_audio_retirement_boundary = stop_after_proof  # type: ignore[method-assign]
    driver._release_block_probe = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    driver._reset_unrouted_generation = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

    with pytest.raises(GStreamerDriverError, match=expected_error):
        driver.isolate_audio_loss(pipeline, 0.2)

    assert requested == list(range(1, expected_requests + 1))
    assert discarded == ([1] if first_edge_ns < 0 else [])
    assert len(finalized) == int(expected_final_edge_ns is not None)
    if expected_final_edge_ns is not None:
        assert finalized[0].edge_skew_ns == expected_final_edge_ns
    assert len(requested) <= 2


def test_audio_loss_retirement_boundary_refusal_releases_held_force_gate() -> None:
    driver, context, generation, _flow = _forced_idr_fixture()
    pipeline = context.pipeline
    generation.audio_eos.arm_retirement()
    order: list[str] = []
    audio_valve = _ForceElement()
    audio_valve.src = _ImmediateIdlePad(audio_valve, order)
    audio_valve.set_property("drop", False)
    generation.audio_valve = audio_valve
    generation.audio_queue = object()
    generation.opened["/srv/dashcam/pending/active.mp4"] = 1
    context.audio_ingress_quarantine = _AudioIngressQuarantine(object(), object(), 1)
    context.generations[2] = _driver_generation(2)
    context.generations[3] = _driver_generation(3)

    def refuse_boundary(*_args: object, **_kwargs: object) -> str:
        order.append("audio_boundary_refused")
        raise GStreamerDriverError("injected audio boundary refusal")

    force_gate = SimpleNamespace(
        held=SimpleNamespace(
            request_count=1,
            forced_idr_running_time_ns=60_000_000_000,
        ),
        request_count=1,
        dispatch=SimpleNamespace(request_count=1),
        event_probe_id=None,
        event_pad=object(),
        video_pad=object(),
        video_probe_id=1,
        reached=threading.Event(),
        completed=threading.Event(),
        release=threading.Event(),
    )

    def hold_force(*_args: object, **_kwargs: object) -> object:
        order.append("force")
        return force_gate

    def finalize_before_boundary(*_args: object, **_kwargs: object) -> ForcedIdrProof:
        order.append("proof")
        assert generation.last_audio_end_running_time_ns is not None
        return forced_idr_proof(
            edge_skew_ns=(
                60_000_000_000 - generation.last_audio_end_running_time_ns
            )
        )

    def await_before_boundary(*_args: object, **_kwargs: object) -> None:
        order.append("await_force")

    def set_linked(
        _context: _GenerationPipeline,
        target: _RecordingGeneration,
        linked: bool,
    ) -> None:
        order.append(f"link_{target.generation_id}_{str(linked).lower()}")
        target.linked = linked

    def set_open(target: _RecordingGeneration, opened: bool) -> None:
        order.append(f"open_{target.generation_id}_{str(opened).lower()}")
        if target.audio_valve is not None:
            target.audio_valve.set_property("drop", not opened)

    def drain_and_arm(
        _queue: object,
        _deadline: float,
        *,
        on_first_empty: Callable[[], None] | None = None,
    ) -> None:
        if on_first_empty is not None:
            on_first_empty()

    driver._prewarm_generation = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    driver._wait_for_audio_queue_drain = drain_and_arm  # type: ignore[assignment]
    driver._establish_audio_retirement_boundary = refuse_boundary  # type: ignore[method-assign]
    driver._set_generation_linked = set_linked  # type: ignore[assignment]
    driver._set_generation_open = set_open  # type: ignore[assignment]
    driver._arm_forced_idr_gate = hold_force  # type: ignore[assignment]
    driver._await_force_key_dispatch = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: order.append("await_dispatch")
    )
    driver._await_forced_idr_gate = await_before_boundary  # type: ignore[method-assign]
    driver._finalize_forced_idr_gate = finalize_before_boundary  # type: ignore[method-assign]
    driver._release_block_probe = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: force_gate.release.set()
    )
    driver._reset_unrouted_generation = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

    with pytest.raises(GStreamerDriverError, match="injected audio boundary refusal"):
        driver.isolate_audio_loss(pipeline, 0.2)

    assert order == [
        "link_2_true",
        "force",
        "await_dispatch",
        "audio_idle",
        "open_1_false",
        "await_force",
        "proof",
        "audio_boundary_refused",
        "open_1_false",
        "link_1_false",
    ]
    assert context.routing_phase == "LOSS_CONTAINMENT_CRITICAL"
    assert context.generations[1].linked is False
    assert context.generations[2].linked is True
    assert audio_valve.src.probes == {}
    assert force_gate.release.is_set()


@pytest.mark.parametrize(
    ("fixture_kwargs", "message"),
    [
        ({"response_count": None}, "response/IDR wait timed out"),
        ({"response_count": 2}, "response/IDR wait timed out"),
        ({"all_headers": False}, "omitted all headers"),
        ({"all_headers": 1}, "event values are invalid"),
        ({"response_count": True}, "event values are invalid"),
        ({"response_seqnum": -1}, "seqnum is invalid"),
        ({"response_running_time_ns": 2**64 - 1}, "event values are invalid"),
        (
            {"buffer": _ForceBuffer(payload=b"\x00\x00\x00\x01\x41\xaa")},
            "lacks NAL type 5",
        ),
        ({"buffer": _ForceBuffer(delta=True)}, "response/IDR wait timed out"),
        ({"send_accepted": False}, "refused forced-IDR request"),
        (
            {"send_error": RuntimeError("injected synchronous send failure")},
            "dispatch failed: injected synchronous send failure",
        ),
    ],
)
def test_forced_idr_gate_fails_closed_and_removes_both_probes(
    fixture_kwargs: dict[str, object],
    message: str,
) -> None:
    driver, context, generation, flow = _forced_idr_fixture(**fixture_kwargs)  # type: ignore[arg-type]
    assert flow.parser_pad is not None and flow.video_pad is not None
    with pytest.raises(GStreamerDriverError, match=message):
        driver._arm_forced_idr_gate(
            context,
            generation,
            deadline=time.monotonic() + 0.05,
            timeout_s=0.05,
        )
    assert flow.event_pad is not None and flow.event_pad.probes == {}
    assert flow.parser_pad.probes == {}
    assert flow.video_pad.probes == {}
    if flow.thread is not None and flow.thread.ident is not None:
        flow.thread.join(0.2)


@pytest.mark.parametrize("edge_skew_ns", [0, 99_999_999])
def test_forced_idr_post_drain_edge_accepts_strict_bounds(edge_skew_ns: int) -> None:
    driver, context, generation, flow = _forced_idr_fixture(edge_skew_ns=edge_skew_ns)
    gate = driver._arm_forced_idr_gate(
        context,
        generation,
        deadline=time.monotonic() + 0.2,
        timeout_s=0.2,
    )
    try:
        proof = _finalize_forced_gate(driver, generation, gate)
        assert proof.edge_skew_ns == edge_skew_ns
        assert gate.proof is proof
    finally:
        _release_forced_gate(driver, gate, flow)


@pytest.mark.parametrize("edge_skew_ns", [-1, 100_000_000])
def test_forced_idr_post_drain_edge_refuses_outside_strict_bounds(
    edge_skew_ns: int,
) -> None:
    driver, context, generation, flow = _forced_idr_fixture(edge_skew_ns=edge_skew_ns)
    gate = driver._arm_forced_idr_gate(
        context,
        generation,
        deadline=time.monotonic() + 0.2,
        timeout_s=0.2,
    )
    try:
        with pytest.raises(GStreamerDriverError, match="100 ms production bound"):
            _finalize_forced_gate(driver, generation, gate)
        assert gate.proof is None
    finally:
        _release_forced_gate(driver, gate, flow)


@pytest.mark.parametrize(
    "timing_error",
    ["audio buffer duration is invalid", "audio buffer PTS is invalid"],
)
def test_forced_idr_post_drain_proof_refuses_audio_timing_error(
    timing_error: str,
) -> None:
    driver, context, generation, flow = _forced_idr_fixture(
        audio_timing_error=timing_error,
    )
    gate = driver._arm_forced_idr_gate(
        context,
        generation,
        deadline=time.monotonic() + 0.2,
        timeout_s=0.2,
    )
    try:
        with pytest.raises(GStreamerDriverError, match=timing_error):
            _finalize_forced_gate(driver, generation, gate)
    finally:
        _release_forced_gate(driver, gate, flow)


def test_forced_idr_post_drain_proof_refuses_missing_audio_edge() -> None:
    driver, context, generation, flow = _forced_idr_fixture(audio_end_present=False)
    gate = driver._arm_forced_idr_gate(
        context,
        generation,
        deadline=time.monotonic() + 0.2,
        timeout_s=0.2,
    )
    try:
        with pytest.raises(GStreamerDriverError, match="unavailable or changed"):
            driver._finalize_forced_idr_gate(generation, gate, 0)
    finally:
        _release_forced_gate(driver, gate, flow)


def test_forced_idr_post_route_revalidation_refuses_aac_edge_change() -> None:
    driver, context, generation, flow = _forced_idr_fixture(edge_skew_ns=50_000_000)
    gate = driver._arm_forced_idr_gate(
        context,
        generation,
        deadline=time.monotonic() + 0.2,
        timeout_s=0.2,
    )
    try:
        proof = _finalize_forced_gate(driver, generation, gate)
        generation.last_audio_end_running_time_ns = (
            proof.last_audio_end_running_time_ns + 1
        )
        with pytest.raises(GStreamerDriverError, match="AAC access-unit end changed"):
            driver._revalidate_forced_idr_audio_edge(generation, proof)
    finally:
        _release_forced_gate(driver, gate, flow)


def test_parent_null_refuses_a_surviving_force_key_dispatch() -> None:
    pipeline = object()
    context = _GenerationPipeline(
        pipeline,
        object(),
        "/srv/dashcam/pending",
        "abcdef123456",
        0,
        object(),
        object(),
        object(),
        object(),
        {},
        threading.Lock(),
        {},
        deque(),
    )
    release = threading.Event()
    done = threading.Event()

    def blocked_send() -> None:
        release.wait(1.0)
        done.set()

    worker = threading.Thread(target=blocked_send, daemon=True)
    dispatch = _BoundedEventDispatch(
        "forced-idr-request",
        worker,
        done,
        object(),
        1,
    )
    context.force_key_dispatches.append(dispatch)
    driver = PyGObjectGStreamerDriver(object())
    driver._generation_pipelines[id(pipeline)] = context
    driver._set_and_verify_state = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    worker.start()
    try:
        with pytest.raises(GStreamerDriverError, match="survived parent NULL"):
            driver.set_null(pipeline, 0.05)
    finally:
        release.set()
        worker.join(0.2)


def test_synchronous_force_dispatch_refuses_completion_after_deadline_without_worker() -> None:
    driver, context, generation, flow = _forced_idr_fixture(send_blocked=True)
    assert flow.parser_pad is not None and flow.video_pad is not None
    with pytest.raises(GStreamerDriverError, match="dispatch exceeded its deadline"):
        driver._arm_forced_idr_gate(
            context,
            generation,
            deadline=time.monotonic() + 0.05,
            timeout_s=0.05,
        )
    assert len(context.force_key_dispatches) == 1
    dispatch = context.force_key_dispatches[0]
    assert dispatch.done.is_set()
    assert dispatch.thread is None
    assert flow.event_pad is not None and flow.event_pad.probes == {}
    assert flow.parser_pad.probes == {}
    assert flow.video_pad.probes == {}
