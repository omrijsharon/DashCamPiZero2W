from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from uuid import UUID

import pytest

from dashcam.audio.alsa import AlsaIdentity
from dashcam.metadata.schema import (
    AudioSummary,
    ClipSidecar,
    GpsSummary,
    VideoSummary,
)
from dashcam.state import GpsTimeState, SystemClockState, TimestampQuality
from dashcam.storage.naming import finalized_unsynced_clip_pair

ROOT = Path(__file__).resolve().parents[2]
HARNESS_PATH = ROOT / "deploy/ssh-dev-validation/milestone7-production-loss/run.py"
README_PATH = HARNESS_PATH.with_name("README.md")
MANIFEST_PATH = HARNESS_PATH.with_name("SHA256SUMS")
BOOT_ID = UUID("601693e3-fa96-427e-906b-1621463a15cd")


def _load() -> ModuleType:
    name = "pi_m7_production_loss_harness"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, HARNESS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _identity() -> AlsaIdentity:
    return AlsaIdentity(
        "08bb",
        "2902",
        physical_path="platform-3f980000.usb-usb-0:1:1.0",
        product="USB_PnP_Sound_Device",
    )


def _sidecar(sequence: int, *, audio: bool) -> ClipSidecar:
    pair = finalized_unsynced_clip_pair(
        boot_id=BOOT_ID.hex[:12],
        sequence=sequence,
    )
    start = sequence * 5_000_000_000
    return ClipSidecar(
        schema_version=1,
        clip_id=UUID(int=sequence + 1),
        boot_id=BOOT_ID,
        sequence=sequence,
        video_file=pair.video_name,
        metadata_file=pair.metadata_name,
        start_utc=None,
        end_utc=None,
        start_monotonic_ns=start,
        end_monotonic_ns=start + 5_000_000_000,
        gps_time_state=GpsTimeState.UNSYNCED,
        system_clock_state=SystemClockState.UNSET,
        timestamp_quality=TimestampQuality.MONOTONIC_ONLY,
        time_anchor=None,
        timezone="UTC",
        start_local=None,
        video=VideoSummary("h264", 1920, 1080, 30.0, 8_000_000, 8_000_000, 150, 0),
        audio=(
            AudioSummary(True, "aac", 48_000, 1, 128_000)
            if audio
            else AudioSummary(False, None, None, None, None)
        ),
        gps=GpsSummary(False, None),
        protected=False,
        protection_reason=None,
        software_version="test",
    )


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
            "duration": "5.000000",
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
                "duration": "5.000000",
                "bit_rate": "128000",
            }
        )
    return {"streams": streams, "format": {"duration": "5.0", "size": "3000000"}}


def test_resolve_authorization_uses_exact_matching_usb_parent(tmp_path: Path) -> None:
    harness = _load()
    sys_root = tmp_path / "devices"
    device = sys_root / "platform" / "usb1" / "1-1"
    control = device / "1-1.1" / "sound" / "card1" / "controlC1"
    control.mkdir(parents=True)
    (device / "idVendor").write_text("08bb\n", encoding="ascii")
    (device / "idProduct").write_text("2902\n", encoding="ascii")
    (device / "product").write_text("USB PnP Sound Device\n", encoding="ascii")
    (device / "authorized").write_text("1\n", encoding="ascii")

    found = harness.resolve_usb_authorization_path(
        "/devices/platform/usb1/1-1/1-1.1/sound/card1/controlC1",
        _identity(),
        sys_devices_root=sys_root,
    )

    assert found == device / "authorized"
    wrong = AlsaIdentity(
        "08bb",
        "2902",
        physical_path="platform-3f980000.usb-usb-0:1:1.0",
        product="Other_Device",
    )
    with pytest.raises(harness.HarnessError, match="exactly one"):
        harness.resolve_usb_authorization_path(
            "/devices/platform/usb1/1-1/1-1.1/sound/card1/controlC1",
            wrong,
            sys_devices_root=sys_root,
        )


def test_authorization_transition_is_preconditioned_and_reversible(tmp_path: Path) -> None:
    harness = _load()
    authorized = tmp_path / "authorized"
    authorized.write_bytes(b"1\n")

    down = harness.write_authorized(authorized, 0, expected=1)
    assert down["confirmed"] is True
    assert harness.read_authorized(authorized) == 0
    up = harness.write_authorized(authorized, 1, expected=0)
    assert up["confirmed"] is True
    assert harness.read_authorized(authorized) == 1

    authorized.write_bytes(b"0\n")
    with pytest.raises(harness.HarnessError, match="precondition"):
        harness.write_authorized(authorized, 0, expected=1)
    restored = harness.restore_authorized(authorized)
    assert restored["confirmed"] is True
    assert restored["write_required"] is True
    assert harness.read_authorized(authorized) == 1


def test_media_validation_requires_truthful_av_then_video_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _load()
    recording = tmp_path / "dashcam"
    clips = recording / "clips"
    clips.mkdir(parents=True)
    monkeypatch.setattr(harness, "RECORDING_ROOT", recording)
    monkeypatch.setattr(harness, "CLIPS_ROOT", clips)
    sidecars = (_sidecar(200, audio=True), _sidecar(201, audio=False))
    names: set[str] = set()
    for sidecar in sidecars:
        (clips / sidecar.video_file).write_bytes(b"media")
        (clips / sidecar.metadata_file).write_bytes(sidecar.to_canonical_json())
        names.update((sidecar.video_file, sidecar.metadata_file))
    monkeypatch.setattr(
        harness,
        "_probe",
        lambda path: _probe_document("000200" in path.name),
    )
    monkeypatch.setattr(harness, "_first_packet_idr", lambda _path: None)
    monkeypatch.setattr(harness, "_decode", lambda _path, _audio: None)

    result = harness.validate_new_media(set(), names)

    assert result["pair_count"] == 2
    assert result["audio_states"] == [True, False]


def test_media_validation_refuses_audio_return_after_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _load()
    recording = tmp_path / "dashcam"
    clips = recording / "clips"
    clips.mkdir(parents=True)
    monkeypatch.setattr(harness, "RECORDING_ROOT", recording)
    monkeypatch.setattr(harness, "CLIPS_ROOT", clips)
    sidecars = (
        _sidecar(200, audio=True),
        _sidecar(201, audio=False),
        _sidecar(202, audio=True),
    )
    names: set[str] = set()
    for sidecar in sidecars:
        (clips / sidecar.video_file).write_bytes(b"media")
        (clips / sidecar.metadata_file).write_bytes(sidecar.to_canonical_json())
        names.update((sidecar.video_file, sidecar.metadata_file))
    by_sequence = {sidecar.video_file: sidecar.audio.available for sidecar in sidecars}
    monkeypatch.setattr(
        harness,
        "_probe",
        lambda path: _probe_document(by_sequence[path.name]),
    )
    monkeypatch.setattr(harness, "_first_packet_idr", lambda _path: None)
    monkeypatch.setattr(harness, "_decode", lambda _path, _audio: None)
    with pytest.raises(harness.HarnessError, match="unexpectedly returned"):
        harness.validate_new_media(set(), names)


def test_source_uses_real_default_runtime_and_restores_sysfs_in_finally() -> None:
    source = HARNESS_PATH.read_text(encoding="utf-8")
    qualify = source.split("async def qualify()", 1)[1].split("def _parser", 1)[0]

    assert "build_production_runtime(" in qualify
    factory_arguments = qualify.split("runtime = build_production_runtime(", 1)[1].split(")", 1)[0]
    assert "config_path=CONFIG_PATH" in factory_arguments
    assert "identity_path=IDENTITY_PATH" in factory_arguments
    assert "enable_unvalidated_audio_loss_isolation" not in factory_arguments
    assert '"ordinary_defaults": True' in qualify
    assert '"audio_loss_override_supplied": False' in qualify
    assert "await runtime.check(config)" in qualify
    assert "await runtime.start(config)" in qualify
    assert "runtime.run(stop_requested)" in qualify
    assert "finally:" in qualify
    finally_body = qualify.split("finally:", 1)[1]
    assert "restore_authorized, authorized_path" in finally_body
    assert finally_body.index("restore_authorized, authorized_path") < finally_body.index(
        "runtime.stop()"
    )
    assert "systemctl start" not in source
    assert "systemctl stop" not in source
    assert "systemctl restart" not in source


def test_idr_parser_requires_nal_type_five() -> None:
    harness = _load()
    assert harness._contains_h264_idr("00000000: 00000001 65aabbcc") is True
    assert harness._contains_h264_idr("00000000: 00000001 41aabbcc") is False


def test_cli_failure_writes_bounded_exclusive_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _load()
    output = tmp_path / "result.json"
    monkeypatch.setattr(harness, "verify_manifest", lambda _expected: {})

    async def failed() -> dict[str, object]:
        raise harness.HarnessError("refused")

    monkeypatch.setattr(harness, "qualify", failed)
    status = harness.main(
        [
            "--expected-manifest-sha256",
            "a" * 64,
            "qualify",
            "--output",
            str(output),
        ]
    )
    document = json.loads(output.read_bytes())
    assert status == 1
    assert document["passed"] is False
    assert document["authorization_restored"] is False
    assert "refused" in document["failures"][0]


def test_manifest_is_closed_and_readme_states_ordinary_default_scope() -> None:
    harness = _load()
    lines = MANIFEST_PATH.read_text(encoding="ascii").splitlines()
    assert [line.split("  ", 1)[1] for line in lines] == ["README.md", "run.py"]
    assert all(len(line.split("  ", 1)[0]) == 64 for line in lines)
    manifest_hash = hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest()
    entries = harness.verify_manifest(manifest_hash, MANIFEST_PATH.parent)
    assert tuple(entries) == ("README.md", "run.py")
    readme = README_PATH.read_text(encoding="utf-8")
    assert "ordinary production defaults" in readme
    assert "supplies no audio-loss feature override" in readme
    assert "authorized=1" in readme
    assert "microphone restoration remains disabled" in readme
