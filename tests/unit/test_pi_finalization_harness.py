from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID

import pytest

ROOT = Path(__file__).resolve().parents[2]
HARNESS_PATH = ROOT / "deploy/ssh-dev-validation/finalization/run.py"
README_PATH = HARNESS_PATH.with_name("README.md")
MANIFEST_PATH = HARNESS_PATH.with_name("SHA256SUMS")
TEST_UUID = UUID("4e32659b-0f28-45af-91ca-7d6fd61a102c")


def _load() -> ModuleType:
    name = "pi_finalization_harness"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, HARNESS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _roots(tmp_path: Path, harness: ModuleType) -> Any:
    recording_root = tmp_path / "recording"
    for name in harness.MANAGED_DIRECTORIES:
        (recording_root / name).mkdir(parents=True, exist_ok=True)
    catalog_parent = tmp_path / "catalog"
    catalog_parent.mkdir()
    catalog_path = catalog_parent / "catalog.sqlite3"
    with harness.ClipCatalog(catalog_path):
        pass
    return harness.HarnessPaths(
        recording_root=recording_root,
        catalog_path=catalog_path,
    )


def test_identity_is_unique_bounded_and_never_uses_retained_diagnostic_sequences() -> None:
    harness = _load()
    identity = harness.derive_test_identity(TEST_UUID)

    assert identity.clip_id == TEST_UUID
    assert 900_000 <= identity.sequence <= 999_999
    assert identity.sequence > 10
    assert identity.source_video.startswith("pending/boot-")
    assert identity.source_video.endswith(".partial.mp4")
    assert identity.target_video.startswith("clips/boot-")
    assert identity.target_video.endswith(".mp4")
    assert ".partial" not in identity.target_video
    assert harness.derive_test_identity(TEST_UUID) == identity


def test_sidecar_and_synthetic_mp4_are_deterministic_canonical_and_nonempty() -> None:
    harness = _load()
    identity = harness.derive_test_identity(TEST_UUID)
    first = harness.synthetic_mp4(identity)
    second = harness.synthetic_mp4(identity)
    sidecar = harness.build_sidecar(identity)
    payload = sidecar.to_canonical_json()

    assert first == second
    assert len(first) > 24
    assert first[4:8] == b"ftyp"
    assert payload == harness.json.dumps(
        sidecar.to_mapping(),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert harness.parse_sidecar_bytes(payload) == sidecar
    assert sidecar.video_file == Path(identity.target_video).name
    assert sidecar.metadata_file == Path(identity.target_sidecar).name


def test_production_first_move_crash_shape_and_production_recovery(tmp_path: Path) -> None:
    harness = _load()
    paths = _roots(tmp_path, harness)
    identity = harness.derive_test_identity(TEST_UUID)
    pre = harness.prepare(identity, paths=paths)
    harness.validate_observation("pre_crash", identity, pre)

    class InjectedCrash(RuntimeError):
        pass

    def crash() -> None:
        raise InjectedCrash("after durable first move")

    with pytest.raises(InjectedCrash, match="durable first move"):
        harness.crash_worker(
            identity,
            paths=paths,
            expected_device_id=None,
            crash=crash,
        )

    post = harness.inspect_identity(identity, paths=paths)
    harness.validate_observation("post_crash", identity, post)
    assert post["files"]["target_video"]["exists"]
    assert post["files"]["source_sidecar"]["exists"]
    assert post["clip"]["lifecycle"] == "FINALIZING"
    assert post["clip"]["pair_reconciled"] is False
    assert [intent["kind"] for intent in post["intents"]] == ["FINALIZE"]

    harness.recover_locally_for_test(identity, paths=paths)
    recovered = harness.inspect_identity(identity, paths=paths)
    harness.validate_observation("recovered", identity, recovered)
    assert recovered["clip"]["lifecycle"] == "FINALIZED"
    assert recovered["clip"]["pair_reconciled"] is True
    assert recovered["intents"] == []

    recorded_identity = harness.derive_recorded_identity(
        identity.short_boot_id,
        identity.sequence,
    )
    verified = harness.verify_recorded_pair(recorded_identity, paths=paths)
    assert verified["video"]["sha256"] == recovered["files"]["target_video"]["sha256"]
    assert verified["sidecar"]["sha256"] == recovered["files"]["target_sidecar"]["sha256"]
    assert verified["pending_members_absent"] is True
    assert verified["related_pending_intents"] == []
    assert verified["ffprobe_or_decode_performed"] is False


def test_existing_identity_refuses_without_overwrite_or_cleanup(tmp_path: Path) -> None:
    harness = _load()
    paths = _roots(tmp_path, harness)
    identity = harness.derive_test_identity(TEST_UUID)
    first = harness.prepare(identity, paths=paths)
    original = (paths.recording_root / identity.source_video).read_bytes()

    with pytest.raises(harness.HarnessError, match="already exists"):
        harness.prepare(identity, paths=paths)

    assert (paths.recording_root / identity.source_video).read_bytes() == original
    harness.validate_observation("pre_crash", identity, first)


def test_shape_validator_rejects_wrong_half_pair_and_missing_intent(tmp_path: Path) -> None:
    harness = _load()
    paths = _roots(tmp_path, harness)
    identity = harness.derive_test_identity(TEST_UUID)
    pre = harness.prepare(identity, paths=paths)

    wrong = {
        **pre,
        "files": {
            **pre["files"],
            "target_video": {
                "exists": True,
                "size_bytes": 1,
                "sha256": "0" * 64,
            },
        },
    }
    with pytest.raises(harness.HarnessError, match="file existence differs"):
        harness.validate_observation("pre_crash", identity, wrong)


def test_collision_helper_arms_case_only_sentinel_and_refuses_related_catalog(
    tmp_path: Path,
) -> None:
    harness = _load()
    paths = _roots(tmp_path, harness)
    boot_id = UUID("601693e3-fa96-427e-906b-1621463a15cd")
    identity = harness.derive_collision_identity(TEST_UUID, boot_id, 42)
    source = paths.recording_root / identity.source_video
    source.write_bytes(b"open-production-fragment")

    armed = harness.prepare_collision(identity, paths=paths)
    harness.validate_collision_observation("sentinel_armed", identity, armed)
    sentinel = paths.recording_root / identity.sentinel_path
    assert sentinel.name == Path(identity.target_sidecar).name.upper()
    assert sentinel.read_bytes() == harness.collision_sentinel(identity)
    assert source.read_bytes() == b"open-production-fragment"
    assert not (paths.recording_root / identity.source_sidecar).exists()
    assert armed["clips"] == []
    assert armed["intents"] == []

    refused = harness.inspect_collision(identity, paths=paths)
    harness.validate_collision_observation("collision_refused", identity, refused)

    cleaned = harness.cleanup_collision_sentinel(identity, paths=paths)
    assert cleaned["sentinel_absent"] is True
    assert cleaned["partial_pair_cleanup_performed"] is False
    assert source.read_bytes() == b"open-production-fragment"
    assert not sentinel.exists()


def test_collision_sequence_never_overlaps_retained_diagnostics() -> None:
    harness = _load()
    boot_id = UUID("601693e3-fa96-427e-906b-1621463a15cd")

    for sequence in range(0, 11):
        with pytest.raises(ValueError, match="reserved diagnostics"):
            harness.derive_collision_identity(TEST_UUID, boot_id, sequence)


def test_cli_is_phase_separated_and_requires_reviewed_manifest_hash() -> None:
    harness = _load()
    parser = harness._parser()
    manifest = "a" * 64

    for phase in ("prepare", "inject-crash", "inspect-post-crash", "verify-recovered"):
        parsed = parser.parse_args(
            [
                "--expected-manifest-sha256",
                manifest,
                phase,
                "--identity",
                str(TEST_UUID),
            ]
        )
        assert parsed.phase == phase
        assert parsed.identity == TEST_UUID
    for phase in ("prepare-collision", "inspect-collision", "cleanup-collision-sentinel"):
        parsed = parser.parse_args(
            [
                "--expected-manifest-sha256",
                manifest,
                phase,
                "--identity",
                str(TEST_UUID),
                "--sequence",
                "42",
            ]
        )
        assert parsed.phase == phase
        assert parsed.sequence == 42
    recorded = parser.parse_args(
        [
            "--expected-manifest-sha256",
            manifest,
            "verify-recorded",
            "--boot-id",
            "601693e3fa96",
            "--sequence",
            "42",
        ]
    )
    assert recorded.phase == "verify-recorded"
    assert recorded.boot_id == "601693e3fa96"


def test_service_state_parses_named_fields_independently_of_systemd_output_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load()
    output = b"NRestarts=0\nMainPID=0\nActiveState=inactive\nSubState=dead\n"
    monkeypatch.setattr(
        harness,
        "_run",
        lambda *_args, **_kwargs: harness.subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=output,
            stderr=b"",
        ),
    )

    assert harness._service_state(expected_active=False) == {
        "active_state": "inactive",
        "sub_state": "dead",
        "main_pid": 0,
        "n_restarts": 0,
    }


def test_source_has_closed_live_gates_sigkill_and_no_cleanup_or_service_mutation() -> None:
    source = HARNESS_PATH.read_text(encoding="utf-8")

    assert 'EXPECTED_CARD_CID: Final = "fe34325344000000200000031a0192d1"' in source
    assert 'EXPECTED_VOLUME_UUID: Final = "7EED-3EA7"' in source
    assert 'EXPECTED_SOURCE: Final = "/dev/mmcblk0p3"' in source
    assert 'EXPECTED_LABEL: Final = "DASHCAM"' in source
    assert 'EXPECTED_FILESYSTEM: Final = "exfat"' in source
    assert "live harness must run directly as dashcam, never root" in source
    assert "production modules are not loaded from the installed release" in source
    assert "os.kill(os.getpid(), SIGKILL_NUMBER)" in source
    assert "completed.returncode != -SIGKILL_NUMBER" in source
    assert "super().move(source, target)" in source
    assert source.index("super().move(source, target)") < source.index("self._crash()")
    assert '"systemctl", "start"' not in source
    assert '"systemctl", "stop"' not in source
    assert "shutil.rmtree" not in source
    assert source.count("os.unlink(") == 1
    assert "DELETE FROM" not in source
    assert "UPDATE clips" not in source
    assert "import sqlite3" not in source
    assert ".execute(" not in source


def test_readme_requires_owner_visible_recovery_and_durable_delete_retention() -> None:
    text = README_PATH.read_text(encoding="utf-8")

    assert "sudo systemctl start dashcamd.service" in text
    assert "inspect-post-crash" in text
    assert "verify-recovered" in text
    assert "`FINALIZING`" in text
    assert "`FINALIZE` intent" in text
    assert "`FINALIZED` with `pair_reconciled=true`" in text
    assert "or provide a cleanup command" in text
    assert "catalog API path that first commits a durable" in text
    assert "`DELETE` intent" in text
    assert "Do not use `rm`, manual moves, SQL, or catalog" in text
    assert "replacement to remove it" in text
    assert "sequences `000000` through" in text
    assert "`000010`" in text
    assert "prepare-collision" in text
    assert "inspect-collision" in text
    assert "cleanup-collision-sentinel" in text
    assert "Placing it before service start would be invalid evidence" in text
    assert "never performs partial-pair cleanup" in text
    assert "verify-recorded" in text
    assert "stream-hashes the bounded MP4" in text
    assert "does not claim `ffprobe`, decode, IDR, duration" in text


def test_hash_manifest_is_closed_and_verifiable() -> None:
    harness = _load()
    result = harness.verify_bundle(manifest_path=MANIFEST_PATH)

    assert set(result["members"]) == {"README.md", "run.py"}
    assert len(result["manifest_sha256"]) == 64
    with pytest.raises(harness.HarnessError, match="reviewed value"):
        harness.verify_bundle(
            manifest_path=MANIFEST_PATH,
            expected_manifest_sha256="0" * 64,
        )
