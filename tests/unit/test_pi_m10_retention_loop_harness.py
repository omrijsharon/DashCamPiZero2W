from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

ROOT = Path(__file__).resolve().parents[2]
HARNESS_ROOT = ROOT / "deploy" / "ssh-dev-validation" / "milestone10-retention-loop"


def _load(name: str, path: Path) -> ModuleType:
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


harness = _load("m10_retention_loop", HARNESS_ROOT / "run.py")
builder = _load("m10_retention_loop_builder", HARNESS_ROOT / "prepare-bundle.py")

COMMIT = "a" * 40
TREE = "b" * 40


def _zip_payload(members: dict[str, bytes], *, compression: int = zipfile.ZIP_STORED) -> bytes:
    import io

    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=compression) as archive:
        for name, payload in sorted(members.items()):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = compression
            archive.writestr(info, payload)
    return stream.getvalue()


def _bundle(tmp_path: Path, *, compression: int = zipfile.ZIP_STORED) -> Path:
    root = tmp_path / "bundle"
    root.mkdir()
    members = {
        "dashcam/__init__.py": b'"""fixture"""\n',
        "dashcam/storage/reclaimer.py": b'"""fixture"""\n',
    }
    archive = _zip_payload(members, compression=compression)
    source = {
        "schema_version": 1,
        "git_commit": COMMIT,
        "git_tree": TREE,
        "archive_name": "dashcam-source.zip",
        "archive_sha256": hashlib.sha256(archive).hexdigest(),
        "archive_size": len(archive),
        "members": {
            name: {"sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}
            for name, payload in sorted(members.items())
        },
    }
    payloads = {
        "README.md": b"reviewed\n",
        "run.py": b"#!/usr/bin/env python3\n",
        "SOURCE.json": harness.canonical_json(source),
        "dashcam-source.zip": archive,
    }
    for name, payload in payloads.items():
        (root / name).write_bytes(payload)
    manifest = b"".join(
        f"{hashlib.sha256(payloads[name]).hexdigest()}  {name}\n".encode()
        for name in sorted(payloads)
    )
    (root / "SHA256SUMS").write_bytes(manifest)
    return root


def _threshold_cases() -> list[dict[str, object]]:
    return [
        {"name": "start_equal", "mode": "NORMAL", "reclaim_latched": False},
        {"name": "start_minus_one", "mode": "RECLAIMING", "reclaim_latched": True},
        {"name": "high_minus_one", "mode": "RECLAIMING", "reclaim_latched": True},
        {"name": "high_equal", "mode": "NORMAL", "reclaim_latched": False},
        {"name": "emergency_equal", "mode": "RECLAIMING", "reclaim_latched": True},
        {"name": "emergency_minus_one", "mode": "EMERGENCY", "reclaim_latched": True},
        {"name": "no_space_write", "mode": "EMERGENCY", "reclaim_latched": True},
        {"name": "restart_below_high", "mode": "RECLAIMING", "reclaim_latched": True},
        {"name": "restart_high_equal", "mode": "NORMAL", "reclaim_latched": False},
    ]


def _result() -> dict[str, object]:
    return {
        "matrices": {
            "A": {
                "passed": True,
                "cases": _threshold_cases(),
                "identity_drift_refused": True,
                "capacity_drift_refused": True,
                "invalid_observation_bounded": True,
                "observation_failure_bounded": True,
            },
            "B": {
                "passed": True,
                "oldest_first_pair_count": 12,
                "one_pair_per_observation": True,
                "repeated_cycle_count": 3,
                "high_water_stop_bounded": True,
                "filler_allocation_steps": 12,
                "filler_bytes": 160 * 1024**2,
            },
            "C": {
                "passed": True,
                "protected_excluded": True,
                "active_lease_excluded": True,
                "pending_mutation_excluded": True,
                "finalizing_pair_excluded": True,
                "unknown_files_unchanged": True,
            },
            "D": {
                "passed": True,
                "previous_count": 2,
                "current_count": 1,
                "next_count": 1,
            },
            "E": {"passed": True, "delete_one_member_preseeded_replay": True},
            "F": {
                "passed": True,
                "exfat_read_only_fsck_status": 0,
                "ext4_read_only_fsck_status": 0,
            },
            "G": {
                "passed": True,
                "no_eligible_candidate": True,
                "protected_pair_unchanged": True,
            },
            "H": {
                "passed": True,
                "private_mount_namespace": True,
                "network_namespace_unchanged": True,
            },
        },
        "production_release_tested": False,
        "physical_power_loss_tested": False,
        "m10_exit_gate_closed": False,
    }


def test_bundle_verification_binds_manifest_commit_tree_and_members(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    manifest_hash = hashlib.sha256((root / "SHA256SUMS").read_bytes()).hexdigest()

    metadata = harness.verify_bundle(root, manifest_hash, COMMIT)

    assert metadata["git_commit"] == COMMIT
    assert metadata["git_tree"] == TREE
    with pytest.raises(harness.HarnessError, match="manifest"):
        harness.verify_bundle(root, "0" * 64, COMMIT)
    with pytest.raises(harness.HarnessError, match="metadata"):
        harness.verify_bundle(root, manifest_hash, "c" * 40)


def test_bundle_verification_rejects_compression_and_extra_members(tmp_path: Path) -> None:
    compressed = _bundle(tmp_path, compression=zipfile.ZIP_DEFLATED)
    digest = hashlib.sha256((compressed / "SHA256SUMS").read_bytes()).hexdigest()
    with pytest.raises(harness.HarnessError, match="unsafe"):
        harness.verify_bundle(compressed, digest, COMMIT)

    extra = tmp_path / "extra"
    extra.mkdir()
    for child in compressed.iterdir():
        (extra / child.name).write_bytes(child.read_bytes())
    (extra / "unreviewed.txt").write_text("no", encoding="ascii")
    with pytest.raises(harness.HarnessError, match="member set"):
        harness.verify_bundle(extra, digest, COMMIT)


def test_manifest_and_source_parsers_reject_noncanonical_or_unsafe_values() -> None:
    with pytest.raises(harness.HarnessError, match="manifest"):
        harness.parse_manifest(b"bad\n")
    with pytest.raises(harness.HarnessError, match="keys"):
        harness.validate_source_metadata({"schema_version": 1}, COMMIT)
    with pytest.raises(harness.HarnessError, match="metadata"):
        harness.validate_source_metadata(
            cast(dict[str, object], json.loads(harness.canonical_json({}).decode())),
            COMMIT,
        )


def test_argv_validation_is_closed_to_exact_pi_and_parent_worker_shapes() -> None:
    parser = harness._parser()
    parent = parser.parse_args(
        [
            "--bundle",
            "bundle",
            "--expected-manifest-sha256",
            "d" * 64,
            "--expected-commit",
            COMMIT,
            "--output",
            "result.json",
        ]
    )
    harness._validate_arguments(parent)
    parent.expected_board_serial = "0" * 16
    with pytest.raises(harness.HarnessError, match="exact Pi"):
        harness._validate_arguments(parent)

    worker = parser.parse_args(
        [
            "--bundle",
            "bundle",
            "--expected-manifest-sha256",
            "d" * 64,
            "--expected-commit",
            COMMIT,
            "--worker",
        ]
    )
    with pytest.raises(harness.HarnessError, match="incomplete"):
        harness._validate_arguments(worker)


def test_mount_loop_and_cleanup_identity_validators_fail_closed(tmp_path: Path) -> None:
    row = {
        "source": "/dev/loop7",
        "target": "/srv/dashcam",
        "fstype": "exfat",
        "uuid": "ABCD-1234",
        "label": "M10LOOP",
        "options": "rw,nodev,noexec",
    }
    harness.validate_mount_identity(
        row,
        source="/dev/loop7",
        target="/srv/dashcam",
        filesystem="exfat",
        uuid="ABCD-1234",
        label="M10LOOP",
    )
    wrong = dict(row, source="/dev/mmcblk0p3")
    with pytest.raises(harness.HarnessError, match="mount identity"):
        harness.validate_mount_identity(
            wrong,
            source="/dev/loop7",
            target="/srv/dashcam",
            filesystem="exfat",
            uuid="ABCD-1234",
            label="M10LOOP",
        )

    image = tmp_path / "fixture.img"
    image.write_bytes(b"x")
    block = os.stat_result((stat.S_IFBLK | 0o600, 0, 0, 1, 0, 0, 0, 0, 0, 0))
    harness.validate_loop_identity(
        Path("/dev/loop7"), image, stat_result=block, backing_file=str(image.resolve())
    )
    harness.validate_cleanup_identity(
        expected_loop="/dev/loop7",
        expected_image=image,
        mount_source="/dev/loop7",
        backing_file=str(image.resolve()),
    )
    with pytest.raises(harness.HarnessError, match="cleanup"):
        harness.validate_cleanup_identity(
            expected_loop="/dev/loop7",
            expected_image=image,
            mount_source="/dev/mmcblk0p3",
            backing_file=str(image.resolve()),
        )


def test_threshold_and_matrix_evidence_enforce_boundaries_and_false_claims() -> None:
    cases = _threshold_cases()
    harness.validate_threshold_evidence(cases)
    wrong = [dict(item) for item in cases]
    wrong[0]["mode"] = "RECLAIMING"
    with pytest.raises(harness.HarnessError, match="threshold"):
        harness.validate_threshold_evidence(wrong)

    result = _result()
    harness.validate_result_evidence(result)
    result["m10_exit_gate_closed"] = True
    with pytest.raises(harness.HarnessError, match="unsafe acceptance"):
        harness.validate_result_evidence(result)


def test_result_validator_does_not_trust_matrix_pass_boolean() -> None:
    result = _result()
    matrices = cast(dict[str, dict[str, object]], result["matrices"])
    matrices["C"]["active_lease_excluded"] = False
    with pytest.raises(harness.HarnessError, match="semantic"):
        harness.validate_result_evidence(result)


@pytest.mark.parametrize(
    "value",
    [
        {"latitude": 32.0},
        {"nested": [{"longitude": 34.0}]},
        {"sentence": "$GPRMC,raw-private-data"},
        {"ssid": "home"},
    ],
)
def test_privacy_validator_rejects_coordinates_raw_nmea_and_network_secrets(
    value: object,
) -> None:
    with pytest.raises(harness.HarnessError, match=r"privacy|private"):
        harness.validate_privacy(value)


def test_builder_refuses_repository_or_production_output(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    with pytest.raises(builder.BundleError, match="outside"):
        builder._require_outside(repository, repository / "bundle")


def test_result_output_is_constrained_to_direct_bounded_var_tmp_name() -> None:
    accepted = Path("/var/tmp/m10-retention-result-A1.json")
    assert harness._validate_output_path(accepted) == accepted.resolve(strict=False)
    for refused in (
        Path("/etc/m10-retention-result.json"),
        Path("/var/tmp/nested/m10-retention-result.json"),
        Path("/var/tmp/foreign.json"),
        Path("/var/tmp") / ("m10-retention-result-" + "x" * 40 + ".json"),
    ):
        with pytest.raises(harness.HarnessError, match="/var/tmp"):
            harness._validate_output_path(refused)


def _root_observation(free_bytes: int) -> Any:
    return harness.RootBackingObservation(
        device_id="179:2",
        source="/dev/mmcblk0p2",
        target="/",
        filesystem="ext4",
        capacity_bytes=6 * 1024**3,
        free_bytes=free_bytes,
    )


def test_root_backing_preflight_budget_accepts_equality_and_refuses_one_byte_below() -> None:
    required = harness.required_root_free_bytes()
    harness.validate_root_backing_preflight(_root_observation(required))
    with pytest.raises(harness.HarnessError, match="2 GiB reserve"):
        harness.validate_root_backing_preflight(_root_observation(required - 1))
    assert required == 2624 * 1024**2


def test_root_backing_budget_checks_identity_overflow_and_remaining_allocation() -> None:
    required = harness.required_root_free_bytes()
    wrong_source = harness.RootBackingObservation(
        device_id="179:2",
        source="/dev/loop7",
        target="/",
        filesystem="ext4",
        capacity_bytes=6 * 1024**3,
        free_bytes=required,
    )
    with pytest.raises(harness.HarnessError, match="identity"):
        harness.validate_root_backing_preflight(wrong_source)
    wrong_capacity = harness.RootBackingObservation(
        device_id="179:2",
        source="/dev/mmcblk0p2",
        target="/",
        filesystem="ext4",
        capacity_bytes=8 * 1024**3,
        free_bytes=required,
    )
    with pytest.raises(harness.HarnessError, match="capacity"):
        harness.validate_root_backing_preflight(wrong_capacity)
    with pytest.raises(harness.HarnessError, match="overflows"):
        harness.required_root_free_bytes(
            exfat_image_bytes=harness.MAX_SIGNED_BYTES,
            preserved_free_bytes=1,
        )

    remaining_required = harness.required_root_free_bytes(
        exfat_image_bytes=harness.EXT4_IMAGE_BYTES,
        ext4_image_bytes=0,
    )
    harness.validate_root_remaining_budget(
        _root_observation(remaining_required),
        remaining_allocation_bytes=harness.EXT4_IMAGE_BYTES,
    )
    with pytest.raises(harness.HarnessError, match="remaining"):
        harness.validate_root_remaining_budget(
            _root_observation(remaining_required - 1),
            remaining_allocation_bytes=harness.EXT4_IMAGE_BYTES,
        )


def test_root_postcleanup_requires_same_identity_and_two_gibibyte_reserve() -> None:
    before = _root_observation(harness.required_root_free_bytes())
    at_reserve = _root_observation(2 * 1024**3)
    harness.validate_root_backing_poststate(before, at_reserve)
    with pytest.raises(harness.HarnessError, match="2 GiB"):
        harness.validate_root_backing_poststate(before, _root_observation(2 * 1024**3 - 1))
    drifted = harness.RootBackingObservation(
        device_id="179:3",
        source="/dev/mmcblk0p2",
        target="/",
        filesystem="ext4",
        capacity_bytes=6 * 1024**3,
        free_bytes=2 * 1024**3,
    )
    with pytest.raises(harness.HarnessError, match="drifted"):
        harness.validate_root_backing_poststate(before, drifted)


def test_filler_allocation_increment_is_aligned_bounded_and_emergency_safe() -> None:
    mib = 1024**2
    increment = harness._filler_allocation_increment(
        free_bytes=150 * mib,
        start_bytes=72 * mib,
        emergency_bytes=64 * mib,
        allocation_unit_bytes=4096,
        filler_size_bytes=0,
    )
    assert increment == 16 * mib
    assert increment % 4096 == 0
    assert 150 * mib - increment > 64 * mib

    boundary = harness._filler_allocation_increment(
        free_bytes=72 * mib,
        start_bytes=72 * mib,
        emergency_bytes=64 * mib,
        allocation_unit_bytes=4096,
        filler_size_bytes=16 * mib,
    )
    assert boundary == 4096
    assert 72 * mib - boundary > 64 * mib


def test_filler_allocation_increment_refuses_exhaustion_and_unsafe_threshold_band() -> None:
    mib = 1024**2
    with pytest.raises(harness.HarnessError, match="progress"):
        harness._filler_allocation_increment(
            free_bytes=72 * mib,
            start_bytes=72 * mib,
            emergency_bytes=64 * mib,
            allocation_unit_bytes=4096,
            filler_size_bytes=harness.MAX_FILLER_BYTES,
        )
    with pytest.raises(harness.HarnessError, match="emergency guard"):
        harness._filler_allocation_increment(
            free_bytes=72 * mib,
            start_bytes=72 * mib,
            emergency_bytes=71 * mib,
            allocation_unit_bytes=mib,
            filler_size_bytes=0,
        )


def test_filler_observation_allows_one_unit_rounding_and_rejects_drift_or_emergency() -> None:
    mib = 1024**2
    harness._validate_filler_allocation_observation(
        previous_free_bytes=100 * mib,
        free_bytes=83 * mib,
        requested_increment_bytes=16 * mib,
        allocation_unit_bytes=mib,
        emergency_bytes=32 * mib,
    )
    with pytest.raises(harness.HarnessError, match="unsafe"):
        harness._validate_filler_allocation_observation(
            previous_free_bytes=100 * mib,
            free_bytes=83 * mib - 1,
            requested_increment_bytes=16 * mib,
            allocation_unit_bytes=mib,
            emergency_bytes=32 * mib,
        )
    with pytest.raises(harness.HarnessError, match="unsafe"):
        harness._validate_filler_allocation_observation(
            previous_free_bytes=35 * mib,
            free_bytes=34 * mib,
            requested_increment_bytes=mib,
            allocation_unit_bytes=mib,
            emergency_bytes=32 * mib,
        )


def test_freeze_bundle_removes_exact_partial_directory_on_verify_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_parent = tmp_path / "source"
    source_parent.mkdir()
    bundle = _bundle(source_parent)
    manifest_hash = hashlib.sha256((bundle / "SHA256SUMS").read_bytes()).hexdigest()
    temporary_parent = tmp_path / "run"
    temporary_parent.mkdir()
    real_verify = harness.verify_bundle
    calls = 0

    def failing_second_verify(root: Path, manifest: str, commit: str) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise harness.HarnessError("injected frozen verification failure")
        return cast(dict[str, object], real_verify(root, manifest, commit))

    monkeypatch.setattr(harness, "verify_bundle", failing_second_verify)
    with pytest.raises(harness.HarnessError, match="injected"):
        harness._freeze_bundle(
            bundle,
            manifest_hash,
            COMMIT,
            temporary_parent=temporary_parent,
        )
    assert calls == 2
    assert list(temporary_parent.iterdir()) == []


def test_write_all_handles_partial_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[bytes] = []

    def partial_write(_descriptor: int, payload: memoryview) -> int:
        value = bytes(payload[:2])
        observed.append(value)
        return len(value)

    monkeypatch.setattr(harness.os, "write", partial_write)
    harness._write_all(9, b"abcdef")
    assert b"".join(observed) == b"abcdef"


def test_publication_occurs_after_cleanup_barrier_and_removes_failed_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "result.json"

    def failing_fsync(_path: Path) -> None:
        raise harness.HarnessError("injected directory fsync failure")

    monkeypatch.setattr(harness, "_fsync_directory", failing_fsync)
    with pytest.raises(harness.HarnessError, match="injected"):
        harness._publish_result(output, {"passed": True})
    assert not output.exists()
    assert capsys.readouterr().out == ""

    source = (HARNESS_ROOT / "run.py").read_text(encoding="utf-8")
    assert source.index("_publish_result(output, completed_result)") > source.index(
        "if cleanup_errors:"
    )


def test_safe_worker_refusal_accepts_only_exact_category_line_and_digests_everything_else() -> None:
    lines = harness._reviewed_function_lines()
    valid_line = min(lines["worker_refusal_line"])
    accepted = f"REFUSED: H_TYPE_Fworker_refusal_line_L{valid_line}\n".encode()
    assert harness._safe_worker_refusal_detail(accepted) == accepted.decode().rstrip()

    for private in (
        b"REFUSED: SSID MyHome PSK hunter2 coordinates 32.1,34.8\n",
        b"REFUSED: bearer token abcdef\n",
        f"REFUSED: H_TYPE_Fworker_refusal_line_L{valid_line}".encode(),
        accepted + b"second line\n",
        b"REFUSED: H_TYPE_Fworker_refusal_line_L4097\n",
        f"REFUSED: H_TYPE_Fworker_refusal_line_L{max(lines['worker_refusal_line']) + 1}\n".encode(),
        f"REFUSED: H_TYPE_Fnot_reviewed_L{valid_line}\n".encode(),
        f"REFUSED: H_UNKNOWN_Fworker_refusal_line_L{valid_line}\n".encode(),
        b"x" * 600,
    ):
        detail = harness._safe_worker_refusal_detail(private)
        assert detail == (
            "worker-stderr-sha256=" + hashlib.sha256(private).hexdigest() + f",bytes={len(private)}"
        )
        for fragment in ("MyHome", "hunter2", "32.1", "34.8", "abcdef"):
            assert fragment not in detail


def test_worker_exception_line_never_contains_exception_message_or_traceback_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for error in (
        ValueError("SSID MyHome PSK hunter2"),
        OSError("coordinates 32.1,34.8 token abcdef"),
        TypeError("/private/path"),
    ):
        payload = harness._worker_refusal_line(error)
        assert payload == b"REFUSED: H_UNLOCATED\n"
        assert harness.WORKER_REFUSAL_RE.fullmatch(payload) is None
        for private in (b"MyHome", b"hunter2", b"32.1", b"abcdef", b"/private"):
            assert private not in payload

    try:
        harness._fsync_directory(tmp_path / "secret-token-path")
    except OSError as error:
        payload = harness._worker_refusal_line(error)
    else:
        pytest.fail("missing directory unexpectedly opened")
    assert payload == b"REFUSED: H_UNLOCATED\n"
    assert b"secret-token-path" not in payload

    def failing_statvfs(_path: Path) -> object:
        raise OSError("private stat path and token")

    monkeypatch.setattr(harness.os, "statvfs", failing_statvfs, raising=False)
    try:
        harness._stat_space(tmp_path)
    except OSError as error:
        exact_frame = harness._worker_refusal_line(error)
    else:
        pytest.fail("statvfs unexpectedly succeeded")
    assert exact_frame.startswith(b"REFUSED: H_OS_Fstat_space_L")
    assert harness.WORKER_REFUSAL_RE.fullmatch(exact_frame) is not None

    try:
        raise AttributeError("external filename frame with private token")
    except AttributeError as error:
        external = harness._worker_refusal_line(error)
    assert external == b"REFUSED: H_UNLOCATED\n"
    assert b"private token" not in external


def test_parent_rejects_allowlisted_function_name_when_code_filename_is_external(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_lines = harness._reviewed_function_lines()["stat_space"]

    def external_stat_space(_path: Path) -> tuple[int, int]:
        return (1, 1)

    monkeypatch.setattr(harness, "_stat_space", external_stat_space)
    assert "stat_space" not in harness._reviewed_function_lines()
    forged = f"REFUSED: H_OS_Fstat_space_L{min(original_lines)}\n".encode()
    detail = harness._safe_worker_refusal_detail(forged)
    assert detail.startswith("worker-stderr-sha256=")
    assert "H_OS" not in detail


def test_only_opted_in_worker_command_surfaces_safe_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = subprocess.CompletedProcess(
        [harness.UNSHARE, "--"],
        2,
        stdout=b"",
        stderr=(
            f"REFUSED: H_ATTRIBUTE_Fworker_refusal_line_L"
            f"{min(harness._reviewed_function_lines()['worker_refusal_line'])}\n"
        ).encode("ascii"),
    )
    monkeypatch.setattr(harness.subprocess, "run", lambda *_args, **_kwargs: completed)

    with pytest.raises(harness.HarnessError, match="H_ATTRIBUTE_Fworker_refusal_line"):
        harness._run((harness.UNSHARE, "--"), safe_worker_refusal=True)
    with pytest.raises(harness.HarnessError) as ordinary:
        harness._run((harness.UNSHARE, "--"))
    assert "H_ATTRIBUTE_Fworker_refusal_line" not in str(ordinary.value)


@pytest.mark.parametrize(
    "error",
    [
        ValueError("SSID MyHome PSK hunter2"),
        OSError("coordinates 32.1,34.8 token abcdef"),
        AttributeError("unexpected private path /secret"),
    ],
)
def test_worker_top_level_emits_only_category_function_and_reviewed_line(
    error: Exception,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    arguments = harness.argparse.Namespace(worker=True)
    parser = type("Parser", (), {"parse_args": lambda self: arguments})()
    monkeypatch.setattr(harness, "_parser", lambda: parser)
    monkeypatch.setattr(harness, "_validate_arguments", lambda _arguments: None)

    def refuse(_arguments: object) -> int:
        raise error

    monkeypatch.setattr(harness, "_worker", refuse)
    assert harness.main() == 2
    captured = capfd.readouterr()
    assert captured.out == ""
    payload = captured.err.encode("ascii")
    assert payload == b"REFUSED: H_UNLOCATED\n"
    assert harness.WORKER_REFUSAL_RE.fullmatch(payload) is None
    detail = harness._safe_worker_refusal_detail(payload)
    assert detail.startswith("worker-stderr-sha256=")
    for private in ("MyHome", "hunter2", "32.1", "abcdef", "/secret"):
        assert private not in captured.err


def test_checked_harness_declares_hard_bounds_and_honest_deferred_gates() -> None:
    source = (HARNESS_ROOT / "run.py").read_text(encoding="utf-8")
    readme = (HARNESS_ROOT / "README.md").read_text(encoding="utf-8")
    assert '"--make-rprivate", "/"' in source
    assert (
        '"--mount",\n            "--fork",\n            "--kill-child",\n            "--",'
        "\n            sys.executable"
    ) in source
    assert "MAX_FILLER_BYTES" in source
    assert "MAX_RECLAIM_STEPS" in source
    assert "os.posix_fallocate" in source
    assert source.count("os.posix_fallocate") == 2
    assert "stream.write" not in source
    assert "allocated.st_blocks * 512 < filler_size" in source
    assert "allocated.st_blocks * 512 < size" in source
    assert 'provisional_clip_pair(boot_id="m10loop", sequence=44)' in source
    assert 'finalized_unsynced_clip_pair(boot_id="m10loop", sequence=44)' in source
    assert 'invalid[0].fault.value != "INVALID_OBSERVATION"' in source
    assert 'failures[0].fault.value != "OBSERVATION_FAILED"' in source
    assert source.count('[-1].fault.value != "OBSERVATION_STALE"') == 2
    parent_source = source[source.index("def _parent(") :]
    assert parent_source.index(
        "root_backing_before = _observe_root_backing()"
    ) < parent_source.index("lock_path =")
    assert parent_source.index('cleanup_errors.append(f"root-reserve:') < parent_source.index(
        "_publish_result(output, completed_result)"
    )
    assert "480 MiB loop-backed exFAT" in readme
    assert "64 MiB loop-backed ext4" in readme
    assert "at least 2 GiB" in readme
    assert "production_release_tested=false" in readme
    assert "physical_power_loss_tested=false" in readme
    assert "m10_exit_gate_closed=false" in readme
    assert "must not be committed" in readme
