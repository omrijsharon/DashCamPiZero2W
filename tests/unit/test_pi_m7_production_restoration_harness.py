from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import cast
from uuid import UUID

import pytest

from dashcam.metadata.schema import AudioSummary, ClipSidecar, GpsSummary, VideoSummary
from dashcam.recorder.runtime import RuntimeLifecycleEvent, RuntimeLifecycleEventKind
from dashcam.state import GpsTimeState, SystemClockState, TimestampQuality
from dashcam.storage.naming import finalized_unsynced_clip_pair

ROOT = Path(__file__).resolve().parents[2]
HARNESS_PATH = ROOT / "deploy/ssh-dev-validation/milestone7-production-restoration/run.py"
README_PATH = HARNESS_PATH.with_name("README.md")
MANIFEST_PATH = HARNESS_PATH.with_name("SHA256SUMS")
BOOT_ID = UUID("601693e3-fa96-427e-906b-1621463a15cd")


def _load() -> ModuleType:
    name = "pi_m7_production_restoration_harness"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, HARNESS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _forced(cycle: int = 1, *, preserved: bool = True, edge: int = 99_999_999) -> dict[str, object]:
    request, downstream, idr = 10, 20, 30
    forced, audio_end, downstream_running = 200_000_000, 100_000_001, 100_000_000
    return {
        "request_count": cycle,
        "request_seqnum": 7,
        "downstream_seqnum": 7 if preserved else 8,
        "seqnum_preserved": preserved,
        "all_headers": True,
        "nal5": True,
        "request_monotonic_ns": request,
        "downstream_event_monotonic_ns": downstream,
        "idr_arrival_monotonic_ns": idr,
        "downstream_running_time_ns": downstream_running,
        "forced_idr_running_time_ns": forced,
        "event_to_idr_media_ns": forced - downstream_running,
        "request_to_downstream_ns": downstream - request,
        "downstream_to_idr_ns": idr - downstream,
        "request_to_idr_ns": idr - request,
        "last_audio_end_running_time_ns": audio_end,
        "edge_skew_ns": edge,
        "edge_bound_ns": 100_000_000,
    }


def _loss(cycle: int = 1) -> dict[str, object]:
    forced = _forced(cycle)
    return {
        "retired_generation_id": 1,
        "active_generation_id": 2,
        "boundary_running_time_ns": forced["forced_idr_running_time_ns"],
        "retired_slot_id": 1,
        "active_slot_id": 2,
        "camera_identity_unchanged": True,
        "encoder_identity_unchanged": True,
        "successor_first_buffer_is_idr": True,
        "successor_sticky_events_present": True,
        "successor_observed_video_buffers": 31,
        "successor_state_converged": True,
        "retired_fragment_closed": True,
        "request_pads_constant": True,
        "forced_idr": forced,
    }


def test_forced_idr_proof_accepts_exact_contract_and_seqnum_drift() -> None:
    harness = _load()
    assert harness._handoff_proof(_loss(), 1, restore=False)["forced_idr"] == _forced()
    drift = _loss()
    drift["forced_idr"] = _forced(preserved=False)
    assert harness._handoff_proof(drift, 1, restore=False)["forced_idr"] == drift["forced_idr"]


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda item: item.__setitem__("all_headers", False), "boolean"),
        (
            lambda item: item.update(
                {
                    "edge_skew_ns": 100_000_000,
                    "last_audio_end_running_time_ns": 100_000_000,
                }
            ),
            "edge bound",
        ),
        (lambda item: item.__setitem__("request_count", 2), "request_count"),
        (lambda item: item.__setitem__("request_to_idr_ns", 99), "identity"),
    ],
)
def test_forced_idr_proof_refuses_forged_bool_count_relationship_or_edge(
    mutate: object, match: str
) -> None:
    harness = _load()
    proof = _loss()
    forced = proof["forced_idr"]
    assert isinstance(forced, dict)
    cast_mutate = mutate
    assert callable(cast_mutate)
    cast_mutate(forced)
    with pytest.raises(harness.HarnessError, match=match):
        harness._handoff_proof(proof, 1, restore=False)


def test_forced_idr_edge_just_below_bound_and_two_cycle_order() -> None:
    harness = _load()
    first, second = _loss(1), _loss(2)
    assert harness._handoff_proof(first, 1, restore=False)
    assert harness._handoff_proof(second, 2, restore=False)
    counts = [
        cast(dict[str, object], item["forced_idr"])["request_count"] for item in (first, second)
    ]
    assert counts == [1, 2]
    assert harness._forced_idr(_forced(edge=99_999_999), 1)["edge_skew_ns"] == 99_999_999


def test_avcc_length_prefixed_nal_five_scanner() -> None:
    harness = _load()
    assert harness._contains_idr("00000000: 00000004 65aabbcc") is True
    assert harness._contains_idr("00000000: 00000004 41aabbcc") is False


def _sidecar(sequence: int, *, audio: bool) -> ClipSidecar:
    pair = finalized_unsynced_clip_pair(boot_id=BOOT_ID.hex[:12], sequence=sequence)
    return ClipSidecar(
        schema_version=1,
        clip_id=UUID(int=sequence + 1),
        boot_id=BOOT_ID,
        sequence=sequence,
        video_file=pair.video_name,
        metadata_file=pair.metadata_name,
        start_utc=None,
        end_utc=None,
        start_monotonic_ns=sequence * 5_000_000_000,
        end_monotonic_ns=(sequence + 1) * 5_000_000_000,
        gps_time_state=GpsTimeState.UNSYNCED,
        system_clock_state=SystemClockState.UNSET,
        timestamp_quality=TimestampQuality.MONOTONIC_ONLY,
        time_anchor=None,
        timezone="UTC",
        start_local=None,
        video=VideoSummary("h264", 1920, 1080, 30.0, 8_000_000, 8_000_000, 150, 0),
        audio=AudioSummary(True, "aac", 48_000, 1, 128_000)
        if audio
        else AudioSummary(False, None, None, None, None),
        gps=GpsSummary(False, None),
        protected=False,
        protection_reason=None,
        software_version="test",
    )


def _probe(audio: bool, *, skew_s: float) -> dict[str, object]:
    streams: list[dict[str, object]] = [
        {
            "codec_type": "video",
            "codec_name": "h264",
            "profile": "High",
            "width": 1920,
            "height": 1080,
            "r_frame_rate": "30/1",
            "start_time": "0",
            "duration": "5",
        }
    ]
    if audio:
        streams.append(
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "profile": "LC",
                "sample_rate": "48000",
                "channels": 1,
                "start_time": str(skew_s),
                "duration": "5",
            }
        )
    return {"streams": streams, "format": {"duration": "5", "size": "10"}}


def test_media_returns_measurements_before_skew_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _load()
    recording, clips = tmp_path / "dashcam", tmp_path / "dashcam" / "clips"
    clips.mkdir(parents=True)
    monkeypatch.setattr(harness, "RECORDING_ROOT", recording)
    monkeypatch.setattr(harness, "CLIPS_ROOT", clips)
    names: set[str] = set()
    states = (True, False, True, False, True)
    availability: dict[str, bool] = {}
    for index, audio in enumerate(states, start=100):
        sidecar = _sidecar(index, audio=audio)
        (clips / sidecar.video_file).write_bytes(b"media")
        (clips / sidecar.metadata_file).write_bytes(sidecar.to_canonical_json())
        names.update((sidecar.video_file, sidecar.metadata_file))
        availability[sidecar.video_file] = audio
    monkeypatch.setattr(harness, "_first_idr", lambda _path: None)
    monkeypatch.setattr(harness, "_decode", lambda _path, _audio: None)
    monkeypatch.setattr(
        harness,
        "_probe",
        lambda path: _probe(
            availability[path.name],
            skew_s=0.100 if "000104" in path.name else 0.001,
        ),
    )
    result = harness.collect_media(set(), names)
    assert result["passed"] is False
    assert result["failure"] == "A/V stream-edge skew reached 100 ms bound"
    assert len(result["pairs"]) == 5
    assert result["pairs"][-1]["av_stream_edge_skew_ns"] == 100_000_000


def test_manifest_closed_and_source_uses_ordinary_production_defaults() -> None:
    harness = _load()
    lines = MANIFEST_PATH.read_text(encoding="ascii").splitlines()
    assert [line.split("  ", 1)[1] for line in lines] == ["README.md", "run.py"]
    entries = harness.verify_manifest(
        hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest(), MANIFEST_PATH.parent
    )
    assert tuple(entries) == ("README.md", "run.py")
    source = HARNESS_PATH.read_text(encoding="utf-8")
    factory = source.split("runtime = build_production_runtime(", 1)[1].split(")", 1)[0]
    assert "enable_audio_restoration" not in factory
    assert "enable_unvalidated_audio_loss_isolation" not in factory
    assert "restore_authorized, authorized" in source
    assert "systemctl start" not in source
    assert "force-key" in README_PATH.read_text(encoding="utf-8")


def test_restoration_waits_through_exact_boundary_transition_only() -> None:
    harness = _load()
    state = {
        "state": "restoring",
        "reason": "restoring_at_boundary",
        "topology_observation": "stable",
        "topology_observation_stale": False,
        "last_failure": None,
        "loss_count": 1,
        "restoration_count": 0,
        "stable_confirmations": 2,
    }
    snapshot = {"audio": {"state": "UNAVAILABLE", "restoration": state}}

    assert harness._restoration_when_settled(snapshot, 1, restored=True) is None

    state["last_failure"] = {"phase": "injected"}
    with pytest.raises(harness.HarnessError):
        harness._restoration_when_settled(snapshot, 1, restored=True)


def _topology_snapshot(*, replacement_count: int = 2) -> dict[str, object]:
    return {
        "audio": {
            "restoration": {
                "restoration_enabled": True,
                "slot_count": 3,
                "slot_activations": {"1": 5, "2": 0, "3": 4},
                "request_pad_invariant": "constant_preallocated",
                "request_pad_counts_measured": True,
                "request_pad_peer_ownership_proven": True,
                "request_pad_counts": {
                    "video_tee": 4,
                    "audio_tee": 1,
                    "splitmux_video": 3,
                    "splitmux_audio": 1,
                },
                "tee_pad_routes": {
                    "video_active_linked": True,
                    "video_standby_unlinked": True,
                    "video_continuity_linked": True,
                    "audio_active_linked": True,
                    "audio_standby_unlinked": True,
                },
                "audio_ingress": {
                    "current_count": 1,
                    "current_descendant_count": 1,
                    "stale_descendant_count": 0,
                    "replacement_count": replacement_count,
                },
                "active_slot_id": 1,
                "active_activation_id": 5,
            }
        }
    }


def test_resolves_one_exact_usb_authorization_parent(tmp_path: Path) -> None:
    harness = _load()
    root = tmp_path / "devices"
    device = root / "platform" / "usb1" / "1-1"
    control = device / "1-1.1" / "sound" / "card1" / "controlC1"
    control.mkdir(parents=True)
    for name, value in (
        ("idVendor", "08bb\n"),
        ("idProduct", "2902\n"),
        ("product", "USB PnP Sound Device\n"),
        ("authorized", "1\n"),
    ):
        (device / name).write_text(value, encoding="ascii")
    found = harness.resolve_usb_authorization_path(
        "/devices/platform/usb1/1-1/1-1.1/sound/card1/controlC1",
        harness.AlsaIdentity(
            "08bb",
            "2902",
            physical_path="platform-3f980000.usb-usb-0:1:1.0",
            product="USB_PnP_Sound_Device",
        ),
        sys_devices_root=root,
    )
    assert found == device / "authorized"


def test_authorization_precondition_and_final_restore(tmp_path: Path) -> None:
    harness = _load()
    authorized = tmp_path / "authorized"
    authorized.write_bytes(b"1\n")
    assert harness.write_authorized(authorized, 0, expected=1)["confirmed"] is True
    assert harness.restore_authorized(authorized)["confirmed"] is True
    assert harness.read_authorized(authorized) == 1
    with pytest.raises(harness.HarnessError, match="precondition"):
        harness.write_authorized(authorized, 0, expected=0)


def test_media_requires_exact_two_cycle_audio_pattern_and_skew(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _load()
    recording, clips = tmp_path / "dashcam", tmp_path / "dashcam" / "clips"
    clips.mkdir(parents=True)
    monkeypatch.setattr(harness, "RECORDING_ROOT", recording)
    monkeypatch.setattr(harness, "CLIPS_ROOT", clips)
    states = (True, False, True, False, True)
    names: set[str] = set()
    availability: dict[str, bool] = {}
    for sequence, audio in enumerate(states, 200):
        sidecar = _sidecar(sequence, audio=audio)
        (clips / sidecar.video_file).write_bytes(b"media")
        (clips / sidecar.metadata_file).write_bytes(sidecar.to_canonical_json())
        names.update((sidecar.video_file, sidecar.metadata_file))
        availability[sidecar.video_file] = audio
    monkeypatch.setattr(
        harness, "_probe", lambda path: _probe(availability[path.name], skew_s=0.02)
    )
    monkeypatch.setattr(harness, "_first_idr", lambda _path: None)
    monkeypatch.setattr(harness, "_decode", lambda _path, _audio: None)
    result = harness.validate_new_media(set(), names)
    assert result["audio_states"] == list(states)
    assert max(result["restored_skew_seconds"]) < 0.1


@pytest.mark.parametrize("states", [(True, False, True), (True, False, False, True)])
def test_media_refuses_wrong_pattern(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, states: tuple[bool, ...]
) -> None:
    harness = _load()
    recording, clips = tmp_path / "dashcam", tmp_path / "dashcam" / "clips"
    clips.mkdir(parents=True)
    monkeypatch.setattr(harness, "RECORDING_ROOT", recording)
    monkeypatch.setattr(harness, "CLIPS_ROOT", clips)
    names: set[str] = set()
    availability: dict[str, bool] = {}
    for sequence, audio in enumerate(states, 300):
        sidecar = _sidecar(sequence, audio=audio)
        (clips / sidecar.video_file).write_bytes(b"media")
        (clips / sidecar.metadata_file).write_bytes(sidecar.to_canonical_json())
        names.update((sidecar.video_file, sidecar.metadata_file))
        availability[sidecar.video_file] = audio
    monkeypatch.setattr(
        harness, "_probe", lambda path: _probe(availability[path.name], skew_s=0.001)
    )
    monkeypatch.setattr(harness, "_first_idr", lambda _path: None)
    monkeypatch.setattr(harness, "_decode", lambda _path, _audio: None)
    with pytest.raises(harness.HarnessError, match=r"audio truth|finalize"):
        harness.validate_new_media(set(), names)


@pytest.mark.parametrize("replacement_count", [0, 1, 2])
def test_topology_requires_exact_slot_and_activation_sequences(replacement_count: int) -> None:
    harness = _load()
    slots, activations = harness._topology(_topology_snapshot(replacement_count=replacement_count))
    assert slots == (1, 2, 3)
    assert activations == (5, 0, 4)


@pytest.mark.parametrize(
    "field", ["request_pad_counts_measured", "request_pad_peer_ownership_proven"]
)
def test_topology_refuses_missing_or_false_measured_ownership(field: str) -> None:
    harness = _load()
    snapshot = _topology_snapshot()
    restoration = cast(dict[str, object], cast(dict[str, object], snapshot["audio"])["restoration"])
    restoration[field] = False
    with pytest.raises(harness.HarnessError, match="topology"):
        harness._topology(snapshot)


@pytest.mark.parametrize(
    ("field", "value"),
    [("current_count", 2), ("stale_descendant_count", 1), ("replacement_count", 3)],
)
def test_topology_refuses_inexact_audio_ingress_ownership(field: str, value: int) -> None:
    harness = _load()
    snapshot = _topology_snapshot()
    restoration = cast(dict[str, object], cast(dict[str, object], snapshot["audio"])["restoration"])
    ingress = cast(dict[str, object], restoration["audio_ingress"])
    ingress[field] = value
    with pytest.raises(harness.HarnessError, match="topology"):
        harness._topology(snapshot)


def test_source_uses_no_audio_feature_override_and_finally_precedes_cleanup() -> None:
    source = HARNESS_PATH.read_text(encoding="utf-8")
    qualify = source.split("async def qualify()", 1)[1].split("def _parser", 1)[0]
    factory = qualify.split("runtime = build_production_runtime(", 1)[1].split(")", 1)[0]
    assert "enable_audio_restoration" not in factory
    assert "enable_unvalidated_audio_loss_isolation" not in factory
    finally_body = qualify.split("finally:", 1)[1]
    assert finally_body.index("restore_authorized, authorized") < finally_body.index(
        "runtime.stop()"
    )


def test_lifecycle_observer_is_bound_before_runtime_start() -> None:
    source = HARNESS_PATH.read_text(encoding="utf-8")
    qualify = source.split("async def qualify()", 1)[1].split("def _parser", 1)[0]
    assert qualify.index("runtime.bind_lifecycle_observer(") < qualify.index(
        "await runtime.start(config)"
    )


def test_lifecycle_event_preserves_exact_safe_public_fields() -> None:
    harness = _load()
    event = RuntimeLifecycleEvent(RuntimeLifecycleEventKind.RECOVERING, 1, 2, "camera timed out")
    record = harness._lifecycle_event_record(event)
    assert record == {
        "kind": "RECOVERING",
        "pipeline_restart_count": 1,
        "recovery_attempt": 2,
        "detail": "camera timed out",
        "detail_redacted": False,
        "detail_sha256": hashlib.sha256(b"camera timed out").hexdigest(),
    }


@pytest.mark.parametrize("detail", ["/dev/video11 failure", "token=secret", "C:\\private\\file"])
def test_lifecycle_event_redacts_path_or_secret_like_detail(detail: str) -> None:
    harness = _load()
    event = RuntimeLifecycleEvent(RuntimeLifecycleEventKind.RECOVERING, 1, 2, detail)
    record = harness._lifecycle_event_record(event)
    assert record["detail"] is None
    assert record["detail_redacted"] is True
    assert record["detail_sha256"] == hashlib.sha256(detail.encode()).hexdigest()


def test_lifecycle_event_capture_is_strictly_bounded() -> None:
    harness = _load()
    records: list[dict[str, object]] = []
    event = RuntimeLifecycleEvent(RuntimeLifecycleEventKind.RECOVERED, 1, 1)
    for _ in range(harness.MAX_LIFECYCLE_EVENTS):
        harness._capture_lifecycle_event(records, event)
    with pytest.raises(harness.HarnessError, match="exceeded its bound"):
        harness._capture_lifecycle_event(records, event)
    assert len(records) == harness.MAX_LIFECYCLE_EVENTS


def test_bounds_and_public_three_slot_contract() -> None:
    harness = _load()
    assert harness.INITIAL_TIMEOUT == 20.0
    assert harness.LOSS_TIMEOUT == 20.0
    assert harness.RESTORE_TIMEOUT == 25.0
    assert harness.CLEANUP_TIMEOUT == 30.0
    source = HARNESS_PATH.read_text(encoding="utf-8")
    assert '"video_tee": 4' in source
    assert 'set(slots) != {"1", "2", "3"}' in source


def test_result_output_refuses_recording_storage_missing_or_foreign_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _load()
    recording = tmp_path / "dashcam"
    recording.mkdir()
    monkeypatch.setattr(harness, "RECORDING_ROOT", recording)
    with pytest.raises(harness.HarnessError, match="rootfs"):
        harness._write_result(recording / "result.json", {})
    with pytest.raises(harness.HarnessError, match="parent"):
        harness._write_result(tmp_path / "missing" / "result.json", {})
    parent = tmp_path / "rootfs-parent"
    parent.mkdir()
    monkeypatch.setattr(harness, "_is_rootfs_parent", lambda _parent: True)
    output = parent / "result.json"
    harness._write_result(output, {})
    with pytest.raises(harness.HarnessError, match="new direct file"):
        harness._write_result(output, {})


def test_topology_refuses_changed_constant_request_pad_count() -> None:
    harness = _load()
    snapshot = _topology_snapshot()
    restoration = cast(dict[str, object], cast(dict[str, object], snapshot["audio"])["restoration"])
    counts = cast(dict[str, object], restoration["request_pad_counts"])
    counts["video_tee"] = 3
    with pytest.raises(harness.HarnessError, match="request-pad"):
        harness._topology(snapshot)


def test_loss_snapshot_requires_stable_topology_observation() -> None:
    harness = _load()
    restoration: dict[str, object] = {
        "restoration_enabled": True,
        "state": "video_only",
        "retry_attempts": 0,
        "retry_campaigns": 0,
        "retry_in_flight": False,
        "stable_confirmations": 0,
        "reason": "microphone_loss_isolated",
        "topology_observation": "stable",
        "topology_observation_stale": False,
        "topology_observed_monotonic_ns": 1,
        "active_slot_id": 2,
        "active_activation_id": 2,
        "slot_count": 3,
        "slot_activations": {"1": None, "2": 2, "3": None},
        "request_pad_invariant": "constant_preallocated",
        "request_pad_counts_measured": True,
        "request_pad_peer_ownership_proven": True,
        "request_pad_counts": {
            "video_tee": 4,
            "audio_tee": 1,
            "splitmux_video": 3,
            "splitmux_audio": 1,
        },
        "tee_pad_routes": {},
        "audio_ingress": {},
        "loss_count": 1,
        "restoration_count": 0,
        "matched_endpoint": None,
        "matched_identity": None,
        "last_loss_handoff": _loss(),
        "last_restore_handoff": None,
        "loss_classification": "microphone_loss_isolated",
        "loss_observations": [],
        "last_failure": None,
    }
    snapshot = {
        "audio": {
            "state": "UNAVAILABLE",
            "reason": "microphone_loss_isolated",
            "restoration": restoration,
        }
    }
    assert harness._restoration(snapshot, 1, restored=False) is restoration
    restoration["topology_observation"] = "fresh"
    with pytest.raises(harness.HarnessError, match="topology"):
        harness._restoration(snapshot, 1, restored=False)


def test_initial_av_snapshot_requires_zero_restores_and_no_handoff() -> None:
    harness = _load()
    restoration: dict[str, object] = {
        "restoration_enabled": True,
        "state": "active",
        "retry_attempts": 0,
        "retry_campaigns": 0,
        "retry_in_flight": False,
        "stable_confirmations": 0,
        "reason": "active",
        "topology_observation": "stable",
        "topology_observation_stale": False,
        "topology_observed_monotonic_ns": 1,
        "active_slot_id": 1,
        "active_activation_id": 1,
        "slot_count": 3,
        "slot_activations": {"1": 1, "2": None, "3": None},
        "request_pad_invariant": "constant_preallocated",
        "request_pad_counts_measured": True,
        "request_pad_peer_ownership_proven": True,
        "request_pad_counts": {
            "video_tee": 4,
            "audio_tee": 1,
            "splitmux_video": 3,
            "splitmux_audio": 1,
        },
        "tee_pad_routes": {},
        "audio_ingress": {},
        "loss_count": 0,
        "restoration_count": 0,
        "matched_endpoint": "hw:1,0,0",
        "matched_identity": {},
        "last_loss_handoff": None,
        "last_restore_handoff": None,
        "loss_classification": "not_observed",
        "loss_observations": [],
        "last_failure": None,
    }
    snapshot = {"audio": {"state": "MATCHED", "restoration": restoration}}
    assert harness._restoration(snapshot, 0, restored=True) is restoration
    restoration["restoration_count"] = 1
    with pytest.raises(harness.HarnessError, match="initial A/V"):
        harness._restoration(snapshot, 0, restored=True)


def test_waits_only_through_exact_in_progress_topology_shape() -> None:
    harness = _load()
    state = {
        "topology_observation": "handoff_in_progress",
        "topology_observation_stale": True,
    }
    snapshot = {"audio": {"restoration": state}}
    assert harness._restoration_when_settled(snapshot, 1, restored=False) is None
    state["topology_observation"] = "faulted_handoff"
    with pytest.raises(harness.HarnessError, match="topology"):
        harness._restoration_when_settled(snapshot, 1, restored=False)


def test_restore_wait_accepts_only_exact_bounded_unavailable_transition() -> None:
    harness = _load()
    state = {
        "state": "unavailable",
        "topology_observation": "stable",
        "topology_observation_stale": False,
        "last_failure": None,
        "loss_count": 1,
        "restoration_count": 0,
    }
    snapshot = {"audio": {"state": "UNAVAILABLE", "restoration": state}}

    assert harness._restoration_when_settled(snapshot, 1, restored=True) is None
    state["last_failure"] = "restore failed"
    with pytest.raises(harness.HarnessError, match="topology"):
        harness._restoration_when_settled(snapshot, 1, restored=True)


def test_manifest_failure_result_is_exclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _load()
    output = tmp_path / "result.json"
    monkeypatch.setattr(harness, "verify_manifest", lambda _expected: {})
    monkeypatch.setattr(harness, "_is_rootfs_parent", lambda _parent: True)

    async def failed() -> dict[str, object]:
        raise harness.HarnessError("refused")

    monkeypatch.setattr(harness, "qualify", failed)
    assert (
        harness.main(
            [
                "--expected-manifest-sha256",
                "a" * 64,
                "qualify",
                "--output",
                str(output),
            ]
        )
        == 1
    )
    assert "refused" in output.read_text(encoding="utf-8")


def test_physical_cli_is_bounded_and_defaults_to_thirty_minutes() -> None:
    harness = _load()
    parsed = harness._parser().parse_args(
        [
            "--expected-manifest-sha256",
            "a" * 64,
            "qualify-physical",
            "--output",
            "/var/lib/dashcam/result.json",
        ]
    )
    assert parsed.command == "qualify-physical"
    assert parsed.owner_action_timeout_seconds == 1800.0
    assert harness._owner_action_timeout("30") == 30.0
    assert harness._owner_action_timeout("1800") == 1800.0
    with pytest.raises(harness.argparse.ArgumentTypeError, match="between"):
        harness._owner_action_timeout("29")
    with pytest.raises(harness.argparse.ArgumentTypeError, match="between"):
        harness._owner_action_timeout("1801")


def test_physical_owner_markers_are_exact_and_flushed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    harness = _load()
    action = harness._request_owner_action(harness.OWNER_UNPLUG_MARKER)
    assert capsys.readouterr().out == "OWNER_ACTION_REQUIRED: UNPLUG_MICROPHONE\n"
    assert action["marker"] == harness.OWNER_UNPLUG_MARKER
    assert action["completed"] is False
    assert isinstance(action["required_monotonic_ns"], int)
    with pytest.raises(harness.HarnessError, match="not recognized"):
        harness._request_owner_action("OWNER_ACTION_REQUIRED: UNSAFE")


def test_physical_unplug_wait_requires_old_sysfs_path_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _load()
    authorized = tmp_path / "authorized"
    authorized.write_text("1\n", encoding="ascii")
    observations = iter((True, False))
    monkeypatch.setattr(harness.os.path, "lexists", lambda _path: next(observations))

    async def exercise() -> int:
        task = asyncio.create_task(asyncio.sleep(10))
        try:
            return cast(
                int,
                await harness._wait_for_physical_unplug(
                    authorized,
                    task,
                    timeout=1.0,
                ),
            )
        finally:
            task.cancel()

    assert isinstance(asyncio.run(exercise()), int)


def test_physical_mode_never_writes_usb_authorization_and_uses_production_defaults() -> None:
    source = HARNESS_PATH.read_text(encoding="utf-8")
    physical = source.split("async def qualify_physical(", 1)[1].split("def _parser", 1)[0]
    factory = physical.split("runtime = build_production_runtime(", 1)[1].split(")", 1)[0]
    assert "enable_audio_restoration" not in factory
    assert "enable_unvalidated_audio_loss_isolation" not in factory
    assert "write_authorized(" not in physical
    assert "restore_authorized(" not in physical
    assert physical.index("OWNER_UNPLUG_MARKER") < physical.index("OWNER_RECONNECT_MARKER")
    assert '"usb_authorization_writes": 0' in physical


def test_media_accepts_exact_one_cycle_physical_witness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _load()
    recording, clips = tmp_path / "dashcam", tmp_path / "dashcam" / "clips"
    clips.mkdir(parents=True)
    monkeypatch.setattr(harness, "RECORDING_ROOT", recording)
    monkeypatch.setattr(harness, "CLIPS_ROOT", clips)
    states = (True, False, True)
    names: set[str] = set()
    availability: dict[str, bool] = {}
    for sequence, audio in enumerate(states, 500):
        sidecar = _sidecar(sequence, audio=audio)
        (clips / sidecar.video_file).write_bytes(b"media")
        (clips / sidecar.metadata_file).write_bytes(sidecar.to_canonical_json())
        names.update((sidecar.video_file, sidecar.metadata_file))
        availability[sidecar.video_file] = audio
    monkeypatch.setattr(
        harness, "_probe", lambda path: _probe(availability[path.name], skew_s=0.02)
    )
    monkeypatch.setattr(harness, "_first_idr", lambda _path: None)
    monkeypatch.setattr(harness, "_decode", lambda _path, _audio: None)

    result = harness.collect_media(
        set(),
        names,
        expected_audio_states=states,
        minimum_pairs=3,
    )

    assert result["passed"] is True
    assert result["audio_states"] == list(states)
    assert result["qualifying_subsequence"] == {
        "indexes": [0, 1, 2],
        "sequences": [500, 501, 502],
        "audio_states": list(states),
    }
