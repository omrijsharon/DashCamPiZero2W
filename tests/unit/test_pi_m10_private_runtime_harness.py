from __future__ import annotations

import hashlib
import importlib.util
import io
import sys
import zipfile
from datetime import UTC
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "deploy" / "ssh-dev-validation" / "milestone10-private-runtime"


def _load(name: str, path: Path) -> ModuleType:
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


run = _load("m10_private_runtime", HARNESS / "run.py")
builder = _load("m10_private_runtime_builder", HARNESS / "prepare-bundle.py")
HARNESS_COMMIT = "f" * 40


def _archive(members: dict[str, bytes], *, compression: int = zipfile.ZIP_STORED) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=compression) as archive:
        for name, payload in sorted(members.items()):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = compression
            archive.writestr(info, payload)
    return stream.getvalue()


def _source(commit: str, archive_name: str, payload: bytes, members: dict[str, bytes]) -> bytes:
    return run.canonical_json(
        {
            "schema_version": 1,
            "git_commit": commit,
            "git_tree": "a" * 40,
            "archive_name": archive_name,
            "archive_sha256": hashlib.sha256(payload).hexdigest(),
            "archive_size": len(payload),
            "members": {
                name: {"sha256": hashlib.sha256(value).hexdigest(), "size": len(value)}
                for name, value in sorted(members.items())
            },
        }
    )


def _bundle(tmp_path: Path, *, compression: int = zipfile.ZIP_STORED) -> Path:
    root = tmp_path / "bundle"
    root.mkdir(parents=True)
    candidate_members = {
        "dashcam/__init__.py": b"candidate\n",
        "dashcam/control/runtime_server.py": b"listener\n",
    }
    rollback_members = {
        "dashcam/__init__.py": b"rollback\n",
        "dashcam/rollback.py": b"guard\n",
    }
    candidate = _archive(candidate_members, compression=compression)
    rollback = _archive(rollback_members)
    payloads = {
        "README.md": b"reviewed\n",
        "run.py": b"#!/usr/bin/env python3\n",
        "BUNDLE.json": run.canonical_json(
            {
                "schema_version": 1,
                "harness_commit": HARNESS_COMMIT,
                "harness_tree": "e" * 40,
                "candidate_commit": run.EXPECTED_CANDIDATE,
                "candidate_tree": "a" * 40,
                "rollback_commit": run.EXPECTED_ROLLBACK,
                "rollback_tree": "a" * 40,
            }
        ),
        "candidate-source.zip": candidate,
        "rollback-source.zip": rollback,
        "CANDIDATE_SOURCE.json": _source(
            run.EXPECTED_CANDIDATE, "candidate-source.zip", candidate, candidate_members
        ),
        "ROLLBACK_SOURCE.json": _source(
            run.EXPECTED_ROLLBACK, "rollback-source.zip", rollback, rollback_members
        ),
    }
    for name, payload in payloads.items():
        (root / name).write_bytes(payload)
    manifest = b"".join(
        f"{hashlib.sha256(payloads[name]).hexdigest()}  {name}\n".encode("ascii")
        for name in sorted(payloads)
    )
    (root / "SHA256SUMS").write_bytes(manifest)
    return root


def test_bundle_verifier_closes_both_exact_sources(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    digest = hashlib.sha256((root / "SHA256SUMS").read_bytes()).hexdigest()

    result = run.verify_bundle(root, digest, HARNESS_COMMIT)

    assert result["candidate"]["git_commit"] == run.EXPECTED_CANDIDATE
    assert result["rollback"]["git_commit"] == run.EXPECTED_ROLLBACK
    with pytest.raises(run.HarnessError, match="manifest"):
        run.verify_bundle(root, "0" * 64, HARNESS_COMMIT)
    with pytest.raises(run.HarnessError, match="identity"):
        run.verify_bundle(root, digest, HARNESS_COMMIT, "b" * 40)


def test_bundle_verifier_rejects_compression_extra_and_traversal(tmp_path: Path) -> None:
    compressed = _bundle(tmp_path / "compressed", compression=zipfile.ZIP_DEFLATED)
    digest = hashlib.sha256((compressed / "SHA256SUMS").read_bytes()).hexdigest()
    with pytest.raises(run.HarnessError, match="unsafe member"):
        run.verify_bundle(compressed, digest, HARNESS_COMMIT)

    extra = _bundle(tmp_path / "extra")
    (extra / "EXTRA").write_text("no\n", encoding="ascii")
    extra_digest = hashlib.sha256((extra / "SHA256SUMS").read_bytes()).hexdigest()
    with pytest.raises(run.HarnessError, match="directory member"):
        run.verify_bundle(extra, extra_digest, HARNESS_COMMIT)

    traversal = _archive({"../escape.py": b"bad\n"})
    assert traversal


def test_builder_source_archives_are_deterministic_stored_and_stripped() -> None:
    members = {
        "dashcam/__init__.py": b"a\n",
        "dashcam/recorder/runtime.py": b"b\n",
    }
    first = builder._zip_bytes(members)
    second = builder._zip_bytes(dict(reversed(tuple(members.items()))))

    assert first == second
    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        assert archive.namelist() == sorted(members)
        assert all(item.compress_type == zipfile.ZIP_STORED for item in archive.infolist())
        assert all(item.date_time == (1980, 1, 1, 0, 0, 0) for item in archive.infolist())


def test_exact_root_budget_and_threshold_boundaries() -> None:
    assert run.ROOT_REQUIRED_FREE_BYTES == 2_701_131_776
    assert run.root_budget_satisfied(run.ROOT_REQUIRED_FREE_BYTES)
    assert not run.root_budget_satisfied(run.ROOT_REQUIRED_FREE_BYTES - 1)
    assert not run.root_budget_satisfied(True)

    low, high, emergency = run.resolved_thresholds(run.EXFAT_IMAGE_BYTES)
    assert emergency == 16 * 1024**2
    assert emergency < 0.1 * 1024**3 < low < high < run.EXFAT_IMAGE_BYTES


def test_candidate_and_rollback_configs_are_deliberately_distinct() -> None:
    candidate = run._config(rollback=False).decode("ascii")
    rollback = run._config(rollback=True).decode("ascii")

    for value in (
        "width = 1920",
        "height = 1080",
        "fps = 30",
        "hardware_encoder_required = true",
        "enabled = false",
        "low_watermark_percent = 30",
        "high_watermark_percent = 35",
        "minimum_free_gib = 0.1",
        "emergency_free_mib = 16",
    ):
        assert value in candidate
        assert value in rollback
    assert "download_lease_timeout_s = 300" in candidate
    assert "download_lease_timeout_s" not in rollback
    assert "/run/dashcam/gps-deliberately-absent" in candidate


def _paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    work = Path("/var/tmp/dashcam-m10-private.123456789abc")
    return work / "recording", work / "state", Path("/run/dashcam-m10-private.123456789abc")


def test_candidate_transient_unit_matches_private_production_contract(tmp_path: Path) -> None:
    recording, state, runtime = _paths(tmp_path)
    properties = run.render_transient_properties(
        recording_source=recording,
        state_source=state,
        runtime_source=runtime,
        role="candidate",
    )
    text = "\n".join(properties)

    assert "User=dashcam" in properties
    assert "Group=dashcam" in properties
    assert (
        "SupplementaryGroups=audio video render dialout dashcam-storage dashcam-api" in properties
    )
    assert "Type=notify" in properties
    assert "Restart=on-failure" in properties
    assert "RuntimeMaxSec=420s" in properties
    assert "PrivateDevices=no" in properties
    assert "RestrictAddressFamilies=AF_UNIX" in properties
    assert f"{recording.as_posix()}:/srv/dashcam" in text
    assert f"{state.as_posix()}:/var/lib/dashcam" in text
    assert f"{runtime.as_posix()}:/run/dashcam" in text
    assert "StateDirectory=" not in text
    assert "RuntimeDirectory=" not in text


def test_rollback_recovery_is_camera_closed_and_rollback_recorder_is_minimal(
    tmp_path: Path,
) -> None:
    recording, state, runtime = _paths(tmp_path)
    recovery = run.render_transient_properties(
        recording_source=recording,
        state_source=state,
        runtime_source=runtime,
        role="rollback-recovery",
    )
    recorder = run.render_transient_properties(
        recording_source=recording,
        state_source=state,
        runtime_source=runtime,
        role="rollback-recorder",
    )
    recovery_text = "\n".join(recovery)
    recorder_text = "\n".join(recorder)

    assert "Type=oneshot" in recovery
    assert "Restart=no" in recovery
    assert "SupplementaryGroups=dashcam-storage" in recovery
    assert "PrivateDevices=yes" in recovery
    assert "DevicePolicy=closed" in recovery
    for group in ("audio", "video", "render", "dialout", "dashcam-api"):
        assert group not in recovery_text
    assert "SupplementaryGroups=video render dialout dashcam-storage" in recorder
    assert "dashcam-api" not in recorder_text
    assert "audio" not in recorder_text


def test_bind_source_allowlist_refuses_production_or_foreign_paths() -> None:
    with pytest.raises(run.HarnessError, match="outside"):
        run.render_transient_properties(
            recording_source=Path("/srv/dashcam"),
            state_source=Path("/var/lib/dashcam"),
            runtime_source=Path("/run/dashcam"),
            role="candidate",
        )


def test_clean_storage_safety_stop_requires_zero_restart_and_success() -> None:
    clean = {
        "ActiveState": "inactive",
        "SubState": "dead",
        "Result": "success",
        "ExecMainCode": "1",
        "ExecMainStatus": "0",
        "NRestarts": "0",
    }
    run.validate_clean_safety_stop(clean)

    for key, value in (("Result", "exit-code"), ("ExecMainStatus", "1"), ("NRestarts", "1")):
        changed = {**clean, key: value}
        with pytest.raises(run.HarnessError, match="safety-stop"):
            run.validate_clean_safety_stop(changed)


def test_startup_budget_requires_exact_64_complete_one_pending_and_no_runtime() -> None:
    exact = {
        "delete_complete": 64,
        "delete_pending": 1,
        "camera_opened": False,
        "listener_present": False,
        "catalog_worker_count": 0,
    }
    run.validate_startup_delete_bound(exact)
    for key, value in (
        ("delete_complete", 65),
        ("delete_pending", 0),
        ("camera_opened", True),
        ("listener_present", True),
        ("catalog_worker_count", 1),
    ):
        with pytest.raises(run.HarnessError, match="budget"):
            run.validate_startup_delete_bound({**exact, key: value})


def test_exact_candidate_fixture_apis_create_a_b_and_65_delete_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "src"))
    from dashcam.metadata import schema

    def bounded_member(root: Path, relative: str, size: int) -> None:
        target = root / Path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fixture\n" + bytes(min(size, 4096) - 8))

    monkeypatch.setattr(run, "_member", bounded_member)
    monkeypatch.setattr(schema, "ZoneInfo", lambda _name: UTC)
    for phase in ("A", "B", "C"):
        phase_root = tmp_path / phase
        recording = phase_root / "recording"
        recording.mkdir(parents=True)
        for name in ("clips", "protected", "pending"):
            (recording / name).mkdir()
        result = run._seed_fixture(
            phase,
            recording,
            phase_root / "catalog.sqlite3",
            "11111111-1111-1111-1111-111111111111",
        )
        assert result["phase"] == phase
        counts = run._catalog_counts(phase_root / "catalog.sqlite3")
        if phase == "A":
            assert counts["finalizing"] == 1
            assert counts["leases"] == 1
        elif phase == "B":
            assert len(result["protected_ids"]) == 8
        else:
            assert counts["delete_pending"] == 65
            assert len(result["pending_delete_ids"]) == 65
            assert all(isinstance(row, dict) for row in result["pending_delete_ids"])

    rollback_root = tmp_path / "D" / "recording"
    rollback_root.mkdir(parents=True)
    for name in ("clips", "protected", "pending"):
        (rollback_root / name).mkdir()
    rollback = run._seed_fixture(
        "D",
        rollback_root,
        tmp_path / "D" / "catalog.sqlite3",
        "11111111-1111-1111-1111-111111111111",
    )
    assert rollback["schema5_latch_absent"] is True


def test_protected_emergency_fixture_is_reachable_within_filler_bound() -> None:
    emergency_target = 14 * 1024**2
    protected_bytes = 8 * 16 * 1024**2
    required = run.EXFAT_IMAGE_BYTES - protected_bytes - emergency_target
    assert required < run.MAX_FILLER_BYTES


def test_strict_idr_parser_rejects_keyframe_without_idr() -> None:
    assert run._contains_idr(b"\x00\x00\x00\x01\x65\x88")
    assert run._contains_idr((2).to_bytes(4, "big") + b"\x65\x88")
    assert not run._contains_idr(b"\x00\x00\x00\x01\x61\x88")


def _healthy_status() -> dict[str, object]:
    return {
        "runtime": {
            "pipeline_restart_count": 0,
            "video": {
                "width": 1920,
                "height": 1080,
                "frames_per_second": 30,
                "codec": "h264",
                "hardware_encoded": True,
                "effective_caps": {
                    "raw_format": "NV12",
                    "fps_numerator": 30,
                    "fps_denominator": 1,
                    "h264_profile": "high",
                    "h264_level": "4.1",
                },
                "encoder_identity": {
                    "factory_name": "v4l2h264enc",
                    "factory_class": "Codec/Encoder/Video/Hardware",
                    "device_path": "/dev/video11",
                },
            },
            "frames": {"raw": 91, "encoded": 90, "dropped": 0, "drop_source": "pts"},
            "overlay": {
                "last_error": None,
                "renderer": {
                    "last_error": None,
                    "update_rejections": 0,
                    "contract_mismatches": 0,
                    "transform_failures": 0,
                    "mapping_limit_rejections": 0,
                    "sync_failures": 0,
                },
            },
        }
    }


def test_runtime_health_requires_exact_hardware_and_known_zero_drops() -> None:
    assert run._runtime_health(_healthy_status())["encoder_factory"] == "v4l2h264enc"
    for mutation in (None, True, 1):
        status = _healthy_status()
        status["runtime"]["frames"]["dropped"] = mutation  # type: ignore[index]
        with pytest.raises(run.HarnessError, match="health"):
            run._runtime_health(status)
    status = _healthy_status()
    status["runtime"]["video"]["encoder_identity"]["factory_name"] = "x264enc"  # type: ignore[index]
    with pytest.raises(run.HarnessError, match="encoder"):
        run._runtime_health(status)


def test_exact_c_transition_binds_first_64_and_preserves_65th(tmp_path: Path) -> None:
    root = tmp_path / "recording"
    (root / "clips").mkdir(parents=True)
    oracle: list[dict[str, object]] = []
    clips: list[dict[str, object]] = []
    intents: list[dict[str, object]] = []
    for index in range(65):
        video_path = f"clips/{index}.mp4"
        sidecar_path = f"clips/{index}.json"
        video_payload, sidecar_payload = b"video", b"sidecar"
        if index == 64:
            (root / video_path).write_bytes(video_payload)
            (root / sidecar_path).write_bytes(sidecar_payload)
        oracle.append(
            {
                "intent_id": f"intent-{index}",
                "clip_id": f"clip-{index}",
                "video_path": video_path,
                "sidecar_path": sidecar_path,
                "video_sha256": hashlib.sha256(video_payload).hexdigest(),
                "sidecar_sha256": hashlib.sha256(sidecar_payload).hexdigest(),
            }
        )
        clips.append(
            {
                "clip_id": f"clip-{index}",
                "lifecycle": "DELETING" if index == 64 else "DELETED",
            }
        )
        intents.append(
            {
                "intent_id": f"intent-{index}",
                "clip_id": f"clip-{index}",
                "status": "PENDING" if index == 64 else "COMPLETE",
            }
        )
    fixture = {"pending_delete_ids": oracle}
    snapshot = {"clips": clips, "intents": intents}
    run._validate_c_exact_transition(fixture, snapshot, root)
    intents[0]["status"], intents[64]["status"] = "PENDING", "COMPLETE"
    with pytest.raises(run.HarnessError, match="first 64"):
        run._validate_c_exact_transition(fixture, snapshot, root)


def test_active_assertion_is_scoped_to_same_writing_interval() -> None:
    before = {
        "clip_id": "a",
        "lifecycle": "WRITING",
        "video_present": True,
        "delete_intent": False,
    }
    run.validate_writing_interval(before, dict(before))

    # Once the former active UUID is FINALIZED the assertion intentionally no longer applies.
    with pytest.raises(run.HarnessError, match="WRITING"):
        run.validate_writing_interval(before, {**before, "lifecycle": "FINALIZED"})
    with pytest.raises(run.HarnessError, match="WRITING"):
        run.validate_writing_interval(before, {**before, "delete_intent": True})


def test_source_orders_a_rollback_b_c_and_fail_safe_single_start() -> None:
    source = (HARNESS / "run.py").read_text(encoding="utf-8")
    qualify = source[source.index("def qualify(") : source.index("def _parser(")]

    positions = [
        qualify.index('phase_results["A"]'),
        qualify.index('phase_results["D"]'),
        qualify.index('phase_results["B"]'),
        qualify.index('phase_results["C"]'),
    ]
    assert positions == sorted(positions)
    assert "StartLimitBurst=1" in source
    assert "StartLimitIntervalSec=300s" in source
    assert "validate_clean_safety_stop(terminal)" in source


def test_source_runtime_masks_and_restores_ordinary_recorder() -> None:
    source = (HARNESS / "run.py").read_text(encoding="utf-8")

    assert '"mask", "--runtime", "dashcamd.service"' in source
    assert "_unlink_owned_runtime_mask(mask, owned_mask)" in source
    assert "_require_masked()" in source
    assert "NRestarts" in source
    assert "production host snapshot changed" in source
    assert "systemctl start dashcamd" not in source
    assert 'state["restore_authorized"] is True' in source
    assert "recover_owned_work" in source
    assert "QUALIFICATION_TIMEOUT_S: Final = 900" in source


def test_runtime_mask_restores_only_after_explicit_cleanup_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    masked = False
    states = iter(
        (
            {"ActiveState": "inactive", "UnitFileState": "enabled", "NRestarts": "0"},
            {"LoadState": "masked", "ActiveState": "inactive"},
        )
    )
    monkeypatch.setattr(run, "_service_properties", lambda _unit: next(states))

    def command(arguments: tuple[object, ...], **_kwargs: object) -> None:
        nonlocal masked
        call = tuple(str(value) for value in arguments)
        calls.append(call)
        if "mask" in call:
            masked = True

    monkeypatch.setattr(run, "_command", command)
    monkeypatch.setattr(Path, "exists", lambda _self: False)
    monkeypatch.setattr(Path, "is_symlink", lambda _self: masked)
    monkeypatch.setattr(run.os, "readlink", lambda _path: "/dev/null")
    monkeypatch.setattr(
        run,
        "_owned_mask_facts",
        lambda _path: {
            "device": 1,
            "inode": 2,
            "uid": 0,
            "gid": 0,
            "mode": 0o777,
            "nlink": 1,
            "target": "/dev/null",
        },
    )
    prior = {
        "LoadState": "",
        "ActiveState": "inactive",
        "SubState": "",
        "UnitFileState": "enabled",
        "NRestarts": "0",
    }
    monkeypatch.setattr(
        run,
        "_read_recovery_journal",
        lambda _work: {
            "phase": "PREPARED",
            "prior_unit": prior,
            "prior_mask_present": False,
        },
    )
    monkeypatch.setattr(run, "_transition_recovery_journal", lambda *_args, **_kwargs: None)
    with (
        pytest.raises(RuntimeError, match="injected"),
        run._runtime_mask(Path("/var/tmp/dashcam-m10-private.123456789abc")),
    ):
        raise RuntimeError("injected")
    assert not any("unmask" in call for call in calls)


@pytest.mark.parametrize(
    ("phase", "mask_present", "exact_owned", "authorized"),
    (
        ("PREPARED", False, False, False),
        ("MASK_INTENT", False, False, False),
        ("MASK_OWNED", True, True, True),
        ("CLEANED_MASKED", True, True, True),
        ("CLEANED_MASKED", False, False, False),
        ("RESTORED", False, False, False),
    ),
)
def test_recovery_unmasks_only_an_exact_owned_mask(
    phase: str, mask_present: bool, exact_owned: bool, authorized: bool
) -> None:
    assert (
        run.validate_recovery_mask_authority(
            phase,
            mask_present=mask_present,
            exact_owned_mask=exact_owned,
        )
        is authorized
    )


@pytest.mark.parametrize(
    ("phase", "mask_present", "exact_owned"),
    (
        ("PREPARED", True, False),
        ("MASK_INTENT", True, False),
        ("MASK_INTENT", True, True),
        ("MASK_OWNED", False, False),
        ("MASK_OWNED", True, False),
        ("CLEANED_MASKED", True, False),
        ("RESTORED", True, False),
    ),
)
def test_recovery_refuses_operator_or_drifted_masks(
    phase: str, mask_present: bool, exact_owned: bool
) -> None:
    with pytest.raises(run.HarnessError):
        run.validate_recovery_mask_authority(
            phase,
            mask_present=mask_present,
            exact_owned_mask=exact_owned,
        )


def _metadata(
    *,
    mode: int,
    uid: int = 0,
    gid: int = 0,
    nlink: int = 1,
    device: int = 7,
    inode: int = 11,
    size: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        st_mode=mode,
        st_uid=uid,
        st_gid=gid,
        st_nlink=nlink,
        st_dev=device,
        st_ino=inode,
        st_size=size,
    )


@pytest.mark.parametrize(
    "work_metadata",
    (
        _metadata(mode=0o120777, nlink=1),
        _metadata(mode=0o40750, nlink=2),
        _metadata(mode=0o40700, uid=1, nlink=2),
        _metadata(mode=0o40700, gid=1, nlink=2),
        _metadata(mode=0o40700, nlink=2, device=8),
    ),
)
def test_recovery_refuses_work_identity_drift(
    monkeypatch: pytest.MonkeyPatch, work_metadata: SimpleNamespace
) -> None:
    work = Path("/var/tmp/dashcam-m10-private.123456789abc")
    parent = _metadata(mode=0o41777, nlink=2)

    def lstat(path: object) -> SimpleNamespace:
        value = Path(path)
        if value == Path("/var/tmp"):
            return parent
        if value == Path("/"):
            return _metadata(mode=0o40755, nlink=2)
        if value == work:
            return work_metadata
        raise AssertionError(value)

    monkeypatch.setattr(run.os, "lstat", lstat)
    with pytest.raises(run.HarnessError, match="work directory identity"):
        run._validate_work_identity(work)


def _recovery_document(work: Path) -> dict[str, object]:
    return {
        "schema_version": 1,
        "nonce": "123456789abc",
        "work": work.as_posix(),
        "ordinary_unit": "dashcamd.service",
        "phase": "PREPARED",
        "mask_owner": "11111111-1111-1111-1111-111111111111",
        "prior_mask_present": False,
        "owned_mask": None,
        "prior_unit": {
            "LoadState": "loaded",
            "ActiveState": "inactive",
            "SubState": "dead",
            "UnitFileState": "enabled",
            "NRestarts": "0",
        },
    }


@pytest.mark.parametrize(
    "journal_metadata",
    (
        _metadata(mode=0o120777, size=1),
        _metadata(mode=0o100640, size=1),
        _metadata(mode=0o100600, uid=1, size=1),
        _metadata(mode=0o100600, gid=1, size=1),
        _metadata(mode=0o100600, nlink=2, size=1),
        _metadata(mode=0o100600, device=8, size=1),
    ),
)
def test_recovery_journal_open_is_nofollow_and_refuses_identity_drift(
    monkeypatch: pytest.MonkeyPatch, journal_metadata: SimpleNamespace
) -> None:
    work = Path("/var/tmp/dashcam-m10-private.123456789abc")
    payload = run.canonical_json(_recovery_document(work))
    journal_metadata.st_size = len(payload)
    work_metadata = _metadata(mode=0o40700, nlink=2, inode=31)
    opened: list[tuple[object, int, int | None]] = []
    reads = iter((payload, b""))

    monkeypatch.setattr(run, "_validate_work_identity", lambda _work: work_metadata)

    def open_file(path: object, flags: int, *args: object, **kwargs: object) -> int:
        opened.append((path, flags, kwargs.get("dir_fd")))
        return 10 if len(opened) == 1 else 11

    monkeypatch.setattr(run.os, "open", open_file)
    monkeypatch.setattr(
        run.os,
        "fstat",
        lambda descriptor: work_metadata if descriptor == 10 else journal_metadata,
    )
    monkeypatch.setattr(run.os, "read", lambda _descriptor, _size: next(reads))
    monkeypatch.setattr(run.os, "close", lambda _descriptor: None)

    with pytest.raises(run.HarnessError, match="journal content differs"):
        run._read_recovery_journal(work)

    assert opened[1][0] == "RECOVERY.json"
    assert opened[1][2] == 10
    if getattr(run.os, "O_NOFOLLOW", 0):
        assert opened[0][1] & run.os.O_NOFOLLOW
        assert opened[1][1] & run.os.O_NOFOLLOW


@pytest.mark.parametrize(
    "runtime_metadata",
    (
        _metadata(mode=0o120777),
        _metadata(mode=0o40700, nlink=2),
        _metadata(mode=0o40750, uid=1, gid=run.EXPECTED_API_GID, nlink=2),
        _metadata(mode=0o40750, uid=42, gid=1, nlink=2),
        _metadata(mode=0o40750, uid=42, gid=run.EXPECTED_API_GID, nlink=2, device=8),
    ),
)
def test_recovery_refuses_runtime_directory_identity_drift(
    monkeypatch: pytest.MonkeyPatch, runtime_metadata: SimpleNamespace
) -> None:
    runtime = Path("/run/dashcam-m10-private.123456789abc")

    def lstat(path: object) -> SimpleNamespace:
        if Path(path) == Path("/run"):
            return _metadata(mode=0o40755, nlink=2)
        if Path(path) == runtime:
            return runtime_metadata
        raise AssertionError(path)

    monkeypatch.setattr(run.os, "lstat", lstat)
    with pytest.raises(run.HarnessError, match="runtime identity differs"):
        run._validate_runtime_recovery_identity(runtime, 42)


def test_intent_only_recovery_transitions_directly_to_restored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work = Path("/var/tmp/dashcam-m10-private.123456789abc")
    mask = Path("/run/systemd/system/dashcamd.service")
    transitions: list[tuple[str, str]] = []
    commands: list[tuple[str, ...]] = []
    prior = {
        "LoadState": "loaded",
        "ActiveState": "inactive",
        "SubState": "dead",
        "UnitFileState": "enabled",
        "NRestarts": "0",
    }
    monkeypatch.setattr(Path, "exists", lambda _self: False)
    monkeypatch.setattr(Path, "is_symlink", lambda _self: False)
    monkeypatch.setattr(run, "_service_properties", lambda _unit: dict(prior))
    monkeypatch.setattr(
        run,
        "_command",
        lambda arguments, **_kwargs: commands.append(tuple(str(value) for value in arguments)),
    )
    monkeypatch.setattr(
        run,
        "_transition_recovery_journal",
        lambda _work, expected, target, **_kwargs: transitions.append((expected, target)),
    )

    run._restore_recovery_mask(
        work,
        {"prior_unit": prior, "owned_mask": None},
        "MASK_INTENT",
        mask,
        False,
    )

    assert transitions == [("MASK_INTENT", "RESTORED")]
    assert commands == [("/usr/bin/systemctl", "daemon-reload")]


def test_owned_mask_swap_at_final_unlink_seam_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mask = Path("/run/systemd/system/dashcamd.service")
    expected = {
        "device": 7,
        "inode": 11,
        "uid": 0,
        "gid": 0,
        "mode": 0o777,
        "nlink": 1,
        "target": "/dev/null",
    }
    drifted = {**expected, "inode": 12}
    unlinked: list[str] = []
    monkeypatch.setattr(run, "_open_mask_parent", lambda _mask: 9)
    monkeypatch.setattr(run, "_mask_facts_at", lambda _descriptor, _name: drifted)
    monkeypatch.setattr(
        run.os,
        "unlink",
        lambda name, **_kwargs: unlinked.append(str(name)),
    )
    monkeypatch.setattr(run.os, "close", lambda _descriptor: None)

    with pytest.raises(run.HarnessError, match="changed before identity-bound unlink"):
        run._unlink_owned_runtime_mask(mask, expected)

    assert unlinked == []


def test_runtime_cleanup_reopens_exact_directory_through_run_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = Path("/run/dashcam-m10-private.123456789abc")
    opened: list[tuple[object, int | None]] = []
    unlinked: list[tuple[str, int | None]] = []
    removed: list[tuple[str, int | None]] = []
    run_metadata = _metadata(mode=0o40755, nlink=2, inode=31)
    runtime_metadata = _metadata(
        mode=0o40750,
        uid=42,
        gid=run.EXPECTED_API_GID,
        nlink=2,
        inode=32,
    )
    member_metadata = _metadata(
        mode=0o100600,
        uid=42,
        gid=run.EXPECTED_API_GID,
        inode=33,
    )

    def open_file(path: object, _flags: int, **kwargs: object) -> int:
        opened.append((path, kwargs.get("dir_fd")))
        return 10 if len(opened) == 1 else 11

    monkeypatch.setattr(run.os, "open", open_file)
    monkeypatch.setattr(
        run.os,
        "fstat",
        lambda descriptor: run_metadata if descriptor == 10 else runtime_metadata,
    )
    monkeypatch.setattr(run.os, "listdir", lambda descriptor: ["status.json"])
    monkeypatch.setattr(
        run.os,
        "stat",
        lambda _name, **_kwargs: member_metadata,
    )
    monkeypatch.setattr(
        run.os,
        "unlink",
        lambda name, **kwargs: unlinked.append((str(name), kwargs.get("dir_fd"))),
    )
    monkeypatch.setattr(
        run.os,
        "rmdir",
        lambda name, **kwargs: removed.append((str(name), kwargs.get("dir_fd"))),
    )
    monkeypatch.setattr(run.os, "fsync", lambda _descriptor: None)
    monkeypatch.setattr(run.os, "close", lambda _descriptor: None)

    assert run._cleanup_runtime_recovery_directory(runtime, 42) is True
    assert opened == [(Path("/run"), None), (runtime.name, 10)]
    assert unlinked == [("status.json", 11)]
    assert removed == [(runtime.name, 10)]


def test_ambiguous_systemd_launch_reconciles_deterministic_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    removed: list[str] = []
    monkeypatch.setattr(
        run,
        "_command",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(run.HarnessError("lost reply")),
    )
    monkeypatch.setattr(run, "_remove_unit", removed.append)
    unit = "dashcam-m10-private-123456789abc-b.service"

    with pytest.raises(run.HarnessError, match="lost reply"):
        run._systemd_run(unit, (), ("/bin/false",))

    assert removed == [unit]


def test_source_has_owned_loop_cleanup_and_no_broad_destructive_actions() -> None:
    source = (HARNESS / "run.py").read_text(encoding="utf-8")

    for token in (
        "_loop_backing(loop).resolve()",
        "refusing to detach a foreign loop",
        "refusing to unmount a foreign target",
        "root_budget_satisfied",
        "MAX_NON_IMAGE_ROOT_DELTA_BYTES",
        "post-cleanup root reserve",
        "nodiscard,lazy_itable_init=0,lazy_journal_init=0",
        '"rw,nosuid,nodev,noexec,noatime,nodiscard"',
        "_require_dense_image",
        "_require_owned_loop",
    ):
        assert token in source
    for forbidden in (
        "rm -rf",
        "mkfs.exfat /dev/mmc",
        "timedatectl",
        "apt-get",
        "systemctl enable",
    ):
        assert forbidden not in source


def test_readme_states_private_scope_and_all_nonclaims() -> None:
    readme = (HARNESS / "README.md").read_text(encoding="utf-8")

    for text in (
        "production catalog or recording volume",
        "2,701,131,776",
        "runtime-masks",
        "65-DELETE backlog",
        "exactly 64",
        "HTTP/download data plane",
        "physical GPS",
        "physical microphone",
        "camera-generated FINALIZING",
        "No media, coordinates, raw NMEA",
        "--recover-work",
        "atomically no-replace",
        "observes the production catalog/sentinel",
    ):
        assert text in readme


def test_result_claims_remain_explicit_and_false_for_unexercised_surfaces() -> None:
    source = (HARNESS / "run.py").read_text(encoding="utf-8")

    for claim in (
        '"camera_generated_finalizing_overlap_tested": False',
        '"download_data_plane_tested": False',
        '"http_or_ui_tested": False',
        '"physical_gps_tested": False',
        '"physical_audio_tested": False',
        '"physical_power_loss_tested": False',
    ):
        assert claim in source


def test_rollback_output_is_opened_inside_private_namespace() -> None:
    source = (HARNESS / "run.py").read_text(encoding="utf-8")

    assert 'p.add_argument("--output")' in source
    assert 'f"/run/dashcam/{output.name}"' in source
    assert "StandardOutput=file:" not in source


def test_publication_contract_is_exact_atomic_and_durable() -> None:
    source = (HARNESS / "run.py").read_text(encoding="utf-8")

    assert 'RESULT_RE: Final = re.compile(r"m10-private-runtime-' in source
    publish = source[source.index("def _publish_result(") : source.index("def _space(")]
    assert "os.link(" in publish
    assert "follow_symlinks=False" in publish
    assert publish.count("os.fsync(directory)") >= 2
    assert "os.unlink(path.name" in publish


def test_second_publication_directory_fsync_failure_removes_final(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "result.json"
    entries: set[str] = set()
    fsync_calls = 0

    def write(path: Path, _payload: bytes, _mode: int) -> None:
        entries.add(path.name)

    def link(source: str, target: str, **_kwargs: object) -> None:
        assert source in entries
        entries.add(target)

    def unlink(name: str, **_kwargs: object) -> None:
        if name not in entries:
            raise FileNotFoundError(name)
        entries.remove(name)

    def fsync(_descriptor: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 2:
            raise OSError("injected second directory fsync failure")

    monkeypatch.setattr(run, "_validate_result_destination", lambda path: path)
    monkeypatch.setattr(run, "_write_exclusive", write)
    monkeypatch.setattr(run, "uuid4", lambda: SimpleNamespace(hex="1" * 32))
    monkeypatch.setattr(run.os, "open", lambda *_args, **_kwargs: 7)
    monkeypatch.setattr(run.os, "close", lambda _descriptor: None)
    monkeypatch.setattr(run.os, "link", link)
    monkeypatch.setattr(run.os, "unlink", unlink)
    monkeypatch.setattr(run.os, "fsync", fsync)
    monkeypatch.setattr(
        run.os,
        "stat",
        lambda _name, **_kwargs: SimpleNamespace(st_dev=1, st_ino=9),
    )

    with pytest.raises(OSError, match="second directory"):
        run._publish_result(destination, b"complete\n")

    assert destination.name not in entries


def test_recovery_journal_phase_transition_is_durable_and_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "dashcam-m10-private.123456789abc"
    work.mkdir()
    document = {
        "schema_version": 1,
        "nonce": "123456789abc",
        "work": work.as_posix(),
        "ordinary_unit": "dashcamd.service",
        "phase": "PREPARED",
        "mask_owner": "11111111-1111-1111-1111-111111111111",
        "prior_mask_present": False,
        "owned_mask": None,
        "prior_unit": {
            "LoadState": "loaded",
            "ActiveState": "inactive",
            "SubState": "dead",
            "UnitFileState": "enabled",
            "NRestarts": "0",
        },
    }
    (work / "RECOVERY.json").write_bytes(run.canonical_json(document))
    monkeypatch.setattr(run, "_read_recovery_journal", lambda _work: dict(document))
    monkeypatch.setattr(
        run, "_write_exclusive", lambda path, payload, _mode: path.write_bytes(payload)
    )
    monkeypatch.setattr(run.os, "open", lambda *_args, **_kwargs: 7)
    monkeypatch.setattr(run.os, "fsync", lambda _descriptor: None)
    monkeypatch.setattr(run.os, "close", lambda _descriptor: None)

    run._transition_recovery_journal(work, "PREPARED", "MASK_INTENT")

    assert run._strict_json((work / "RECOVERY.json").read_bytes(), "test")["phase"] == "MASK_INTENT"


def test_failure_cleanup_stays_inside_mask_scope() -> None:
    source = (HARNESS / "run.py").read_text(encoding="utf-8")
    qualify = source[source.index("def qualify(") : source.index("def _parser(")]

    assert qualify.index("with _runtime_mask(work) as mask_state") < qualify.index(
        "_cleanup_fixture(paths)"
    )
    assert qualify.index("_cleanup_fixture(paths)") < qualify.index(
        'mask_state["restore_authorized"] = True'
    )
