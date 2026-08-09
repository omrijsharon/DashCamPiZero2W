from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import zipfile
from pathlib import Path
from types import ModuleType, SimpleNamespace
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


def _sigkill_cells() -> list[dict[str, object]]:
    recovery_actions = {
        "AFTER_INTENT": 2,
        "AFTER_MEMBER1": 1,
        "AFTER_MEMBER2": 0,
        "AFTER_COMPLETE": 0,
    }
    return [
        {
            "operation": operation,
            "cutpoint": cutpoint,
            "sigkill_observed": True,
            "reopen_reconciled": True,
            "idempotent_reconcile": True,
            "recovery_actions": recovery_actions[cutpoint],
        }
        for operation in harness.CRASH_OPERATIONS
        for cutpoint in harness.CRASH_CUTPOINTS
    ]


def _result() -> dict[str, object]:
    return {
        "schema_version": harness.SCHEMA_VERSION,
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
            "E": {
                "passed": True,
                "operations": 4,
                "cutpoints_per_operation": 4,
                "cell_count": 16,
                "actual_sigkill_cells": 16,
                "fresh_catalogs": 16,
                "cells": _sigkill_cells(),
                "sigkill_cutpoint_matrix_tested": True,
                "physical_power_loss_tested": False,
            },
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

    wrong_schema = _result()
    wrong_schema["schema_version"] = 1
    with pytest.raises(harness.HarnessError, match="schema"):
        harness.validate_result_evidence(wrong_schema)


def test_result_validator_does_not_trust_matrix_pass_boolean() -> None:
    result = _result()
    matrices = cast(dict[str, dict[str, object]], result["matrices"])
    matrices["C"]["active_lease_excluded"] = False
    with pytest.raises(harness.HarnessError, match="semantic"):
        harness.validate_result_evidence(result)


def test_sigkill_matrix_validator_requires_every_exact_unique_cell() -> None:
    matrix = cast(dict[str, object], cast(dict[str, object], _result()["matrices"])["E"])
    harness.validate_sigkill_matrix_evidence(matrix)

    missing = dict(matrix)
    missing["cells"] = cast(list[dict[str, object]], matrix["cells"])[:-1]
    with pytest.raises(harness.HarnessError, match="SIGKILL"):
        harness.validate_sigkill_matrix_evidence(missing)

    forged = dict(matrix)
    forged_cells = [dict(cell) for cell in cast(list[dict[str, object]], matrix["cells"])]
    forged_cells[0]["sigkill_observed"] = False
    forged["cells"] = forged_cells
    with pytest.raises(harness.HarnessError, match="SIGKILL"):
        harness.validate_sigkill_matrix_evidence(forged)


def test_crash_fixture_mount_refuses_active_or_non_owned_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Image:
        @staticmethod
        def stat() -> os.stat_result:
            return cast(
                os.stat_result,
                type(
                    "Metadata",
                    (),
                    {
                        "st_mode": stat.S_IFREG | 0o600,
                        "st_nlink": 1,
                        "st_size": 4096,
                        "st_blocks": 8,
                    },
                )(),
            )

    row = {
        "source": str(Path("/dev/loop7")),
        "target": str(Path("/srv/dashcam")),
        "fstype": "exfat",
        "uuid": "ABCD-1234",
        "label": "M10LOOP",
        "options": "rw,nodev",
    }
    monkeypatch.setattr(harness, "_findmnt", lambda _target: row)
    monkeypatch.setattr(harness, "_require_owned_loop", lambda _loop, _image: None)
    monkeypatch.setattr(
        harness,
        "_blkid",
        lambda _loop: {
            "DEVNAME": "/dev/loop7",
            "UUID": "ABCD-1234",
            "TYPE": "exfat",
            "LABEL": "M10LOOP",
        },
    )
    harness._validate_crash_fixture_mount(
        Path("/srv/dashcam"),
        cast(Any, Image()),
        expected_size=4096,
        filesystem="exfat",
        label="M10LOOP",
    )

    active = dict(row, source=str(Path("/dev/mmcblk0p3")))
    monkeypatch.setattr(harness, "_findmnt", lambda _target: active)
    with pytest.raises(harness.HarnessError, match="loop-backed"):
        harness._validate_crash_fixture_mount(
            Path("/srv/dashcam"),
            cast(Any, Image()),
            expected_size=4096,
            filesystem="exfat",
            label="M10LOOP",
        )


def test_crash_fixture_allocation_refusals_have_four_distinct_private_lines() -> None:
    metadata_cases = (
        SimpleNamespace(
            st_mode=stat.S_IFDIR | 0o700,
            st_nlink=1,
            st_size=4096,
            st_blocks=8,
        ),
        SimpleNamespace(
            st_mode=stat.S_IFREG | 0o600,
            st_nlink=2,
            st_size=4096,
            st_blocks=8,
        ),
        SimpleNamespace(
            st_mode=stat.S_IFREG | 0o600,
            st_nlink=1,
            st_size=4095,
            st_blocks=8,
        ),
        SimpleNamespace(
            st_mode=stat.S_IFREG | 0o600,
            st_nlink=1,
            st_size=4096,
            st_blocks=7,
        ),
    )
    lines: set[int] = set()
    reviewed = harness._reviewed_function_lines()["validate_crash_fixture_mount"]

    for metadata in metadata_cases:
        image = SimpleNamespace(stat=lambda metadata=metadata: metadata)
        try:
            harness._validate_crash_fixture_mount(
                Path("/private/catalog-mount"),
                cast(Any, image),
                expected_size=4096,
                filesystem="ext4",
                label="M10CAT",
            )
        except harness.HarnessError as error:
            payload = harness._worker_refusal_line(error)
        else:
            pytest.fail("invalid backing allocation unexpectedly passed")

        match = harness.WORKER_REFUSAL_RE.fullmatch(payload)
        assert match is not None
        assert match.group(1) == b"HARNESS"
        assert match.group(2) == b"validate_crash_fixture_mount"
        line = int(match.group(3))
        assert line in reviewed
        assert harness._safe_worker_refusal_detail(payload) == payload[:-1].decode("ascii")
        assert b"private" not in payload
        assert b"catalog" not in payload
        assert b"M10CAT" not in payload
        lines.add(line)

    assert len(lines) == 4

    forged = (
        f"REFUSED: H_HARNESS_Fvalidate_crash_fixture_mount_L"
        f"{max(harness._reviewed_function_lines()['validate_crash_fixture_mount']) + 1}\n"
    ).encode("ascii")
    detail = harness._safe_worker_refusal_detail(forged)
    assert detail.startswith("worker-stderr-sha256=")
    assert "validate_crash_fixture_mount" not in detail


def test_crash_cell_parser_is_closed_and_mutually_exclusive() -> None:
    arguments = harness._parser().parse_args(
        [
            "--bundle",
            "/run/reviewed-bundle",
            "--expected-manifest-sha256",
            "a" * 64,
            "--expected-commit",
            "b" * 40,
            "--crash-cell",
            "--work",
            "/var/tmp/dashcam-m10-retention-loop.fixture",
            "--cell-operation",
            "DELETE",
            "--cell-cutpoint",
            "AFTER_MEMBER2",
        ]
    )
    harness._validate_arguments(arguments)
    arguments.worker = True
    with pytest.raises(harness.HarnessError, match="mutually exclusive"):
        harness._validate_arguments(arguments)


def test_crash_subprocess_requires_exact_sigkill_uuid_and_closed_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    intent = b"12345678-1234-4abc-8def-1234567890ab\n"
    observed: dict[str, object] = {}
    sigkill = cast(int, harness.SIGKILL_NUMBER)

    class Process:
        stdout = io.BytesIO(intent)
        stderr = io.BytesIO(b"")

        def wait(self, *, timeout: int) -> int:
            observed["timeout"] = timeout
            return -sigkill

        @staticmethod
        def poll() -> int:
            return -sigkill

        @staticmethod
        def kill() -> None:
            raise AssertionError("completed SIGKILL child must not be killed again")

    def popen(command: tuple[str, ...], **_kwargs: object) -> Process:
        observed["command"] = command
        return Process()

    monkeypatch.setattr(
        harness,
        "_crash_cell_coordinates",
        lambda work, operation, cutpoint: ("cell", 180, work / "catalog.sqlite3"),
    )
    monkeypatch.setattr(harness.subprocess, "Popen", popen)
    value = harness._run_crash_subprocess(
        bundle=tmp_path,
        work=tmp_path,
        expected_manifest_sha256="a" * 64,
        expected_commit="b" * 40,
        operation="FINALIZE",
        cutpoint="AFTER_INTENT",
    )
    command = cast(tuple[str, ...], observed["command"])
    assert str(value) == intent.decode("ascii").strip()
    assert command[:4] == (
        sys.executable,
        "-I",
        str(tmp_path / "run.py"),
        "--crash-cell",
    )
    assert command[command.index("--cell-operation") + 1] == "FINALIZE"
    assert command[command.index("--cell-cutpoint") + 1] == "AFTER_INTENT"


def test_after_complete_sigkill_occurs_before_catalog_context_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from uuid import UUID

    import dashcam.catalog.database as database
    import dashcam.catalog.filesystem as filesystem_module

    events: list[str] = []
    intent_id = UUID("12345678-1234-4abc-8def-1234567890ab")
    clip = SimpleNamespace(clip_id=UUID(int=181))

    class ProcessKilled(RuntimeError):
        pass

    class Catalog:
        def __init__(self, _path: Path) -> None:
            pass

        def __enter__(self) -> Catalog:
            events.append("enter")
            return self

        def __exit__(self, *_args: object) -> None:
            events.append("exit")

        def prepare_delete(self, *_args: object, **_kwargs: object) -> UUID:
            events.append("prepare")
            return intent_id

        def reconcile_intent(self, *_args: object, **_kwargs: object) -> object:
            events.append("reconcile")
            return SimpleNamespace(complete=True, problems=())

    class Filesystem:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

    def killed(_pid: int, number: int) -> None:
        assert number == harness.SIGKILL_NUMBER
        events.append("kill")
        raise ProcessKilled

    monkeypatch.setattr(database, "ClipCatalog", Catalog)
    monkeypatch.setattr(filesystem_module, "RootedFilesystem", Filesystem)
    monkeypatch.setattr(
        harness,
        "_crash_fixture",
        lambda _operation, _order: (clip, None, None),
    )
    monkeypatch.setattr(harness, "_device_id", lambda _root: "1:1")
    monkeypatch.setattr(harness, "_write_all", lambda _descriptor, _payload: None)
    monkeypatch.setattr(harness.os, "kill", killed)

    with pytest.raises(ProcessKilled):
        harness._prepare_crash_intent(
            tmp_path / "cell.sqlite3",
            tmp_path,
            operation="DELETE",
            cutpoint="AFTER_COMPLETE",
            order=180,
        )
    assert events == ["enter", "prepare", "reconcile", "kill", "exit"]


def test_crash_subprocess_timeout_kills_and_joins_exact_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[object] = []
    sigkill = cast(int, harness.SIGKILL_NUMBER)

    class Process:
        stdout = io.BytesIO(b"")
        stderr = io.BytesIO(b"")
        finished = False

        def wait(self, *, timeout: int) -> int:
            calls.append(("wait", timeout))
            if not self.finished:
                raise subprocess.TimeoutExpired("crash-cell", timeout)
            return -sigkill

        def poll(self) -> int | None:
            return -sigkill if self.finished else None

        def kill(self) -> None:
            calls.append("kill")
            self.finished = True

    process = Process()
    monkeypatch.setattr(
        harness,
        "_crash_cell_coordinates",
        lambda work, operation, cutpoint: ("cell", 180, work / "catalog.sqlite3"),
    )
    monkeypatch.setattr(harness.subprocess, "Popen", lambda *_args, **_kwargs: process)
    with pytest.raises(harness.HarnessError, match="timeout"):
        harness._run_crash_subprocess(
            bundle=tmp_path,
            work=tmp_path,
            expected_manifest_sha256="a" * 64,
            expected_commit="b" * 40,
            operation="DELETE",
            cutpoint="AFTER_MEMBER2",
        )
    assert calls == [("wait", harness.CRASH_CELL_TIMEOUT_S), "kill", ("wait", 5)]


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "expected_mask"),
    [
        (0, b"12345678-1234-4abc-8def-1234567890ab\n", b"", "1000"),
        (-9, b"not-a-uuid\n", b"", "0001"),
        (
            -9,
            b"12345678-1234-4abc-8def-1234567890ab\n",
            b"REFUSED: private\n",
            "0010",
        ),
        (-9, b"x" * 514, b"", "0101"),
    ],
)
def test_crash_subprocess_rejects_non_sigkill_malformed_or_stderr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    returncode: int,
    stdout: bytes,
    stderr: bytes,
    expected_mask: str,
) -> None:
    class Process:
        def __init__(self) -> None:
            self.stdout = io.BytesIO(stdout)
            self.stderr = io.BytesIO(stderr)

        def wait(self, *, timeout: int) -> int:
            assert timeout == harness.CRASH_CELL_TIMEOUT_S
            return returncode

        def poll(self) -> int:
            return returncode

        @staticmethod
        def kill() -> None:
            raise AssertionError("completed child must not be killed again")

    monkeypatch.setattr(
        harness,
        "_crash_cell_coordinates",
        lambda work, operation, cutpoint: ("cell", 180, work / "catalog.sqlite3"),
    )
    monkeypatch.setattr(harness.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    with pytest.raises(harness.CrashCellContractError, match="exact SIGKILL") as captured:
        harness._run_crash_subprocess(
            bundle=tmp_path,
            work=tmp_path,
            expected_manifest_sha256="a" * 64,
            expected_commit="b" * 40,
            operation="PROTECT",
            cutpoint="AFTER_COMPLETE",
        )
    error = captured.value
    assert error.operation == "PROTECT"
    assert error.cutpoint == "AFTER_COMPLETE"
    assert error.returncode == returncode
    assert error.failed_mask == expected_mask
    assert len(error.stdout) <= 513
    assert len(error.stderr) <= 513


def test_crash_cell_refusal_is_digest_only_and_parent_revalidates_it() -> None:
    private_stdout = b"12345678-1234-4abc-8def-1234567890ab\n"
    private_stderr = b"/srv/dashcam SSID=private PSK=secret token=hidden\n"
    error = harness.CrashCellContractError(
        operation="DELETE",
        cutpoint="AFTER_MEMBER2",
        returncode=2,
        stdout=private_stdout,
        stderr=private_stderr,
        failed_mask="1010",
    )

    line = harness._crash_cell_refusal_line(error)
    detail = harness._safe_worker_refusal_detail(line)

    assert detail == line[:-1].decode("ascii")
    assert b"/srv" not in line
    assert b"SSID" not in line
    assert b"PSK" not in line
    assert b"token" not in line
    assert str(len(private_stdout)).encode("ascii") in line
    assert hashlib.sha256(private_stdout).hexdigest().encode("ascii") in line
    assert str(len(private_stderr)).encode("ascii") in line
    assert hashlib.sha256(private_stderr).hexdigest().encode("ascii") in line
    assert b"operation=DELETE" in line
    assert b"cutpoint=AFTER_MEMBER2" in line
    assert b"returncode=2" in line
    assert b"failed=1010" in line


@pytest.mark.parametrize(
    "mutated",
    [
        b"REFUSED: H_CRASH_CELL operation=FOREIGN cutpoint=AFTER_MEMBER2 "
        b"returncode=2 stdout_bytes=0 stdout_sha256="
        + hashlib.sha256(b"").hexdigest().encode("ascii")
        + b" stderr_bytes=0 stderr_sha256="
        + hashlib.sha256(b"").hexdigest().encode("ascii")
        + b" failed=1000\n",
        b"REFUSED: H_CRASH_CELL operation=DELETE cutpoint=AFTER_MEMBER2 "
        b"returncode=999 stdout_bytes=0 stdout_sha256="
        + hashlib.sha256(b"").hexdigest().encode("ascii")
        + b" stderr_bytes=0 stderr_sha256="
        + hashlib.sha256(b"").hexdigest().encode("ascii")
        + b" failed=1000\n",
        b"REFUSED: H_CRASH_CELL operation=DELETE cutpoint=AFTER_MEMBER2 "
        b"returncode=2 stdout_bytes=0 stdout_sha256="
        + hashlib.sha256(b"").hexdigest().encode("ascii")
        + b" stderr_bytes=0 stderr_sha256="
        + hashlib.sha256(b"").hexdigest().encode("ascii")
        + b" failed=0000\n",
    ],
)
def test_parent_rejects_forged_crash_cell_diagnostic(mutated: bytes) -> None:
    detail = harness._safe_worker_refusal_detail(mutated)

    assert detail == (
        "worker-stderr-sha256=" + hashlib.sha256(mutated).hexdigest() + f",bytes={len(mutated)}"
    )


@pytest.mark.parametrize(
    ("original", "replacement"),
    [
        (b"failed=1000", b"failed=0001"),
        (b"returncode=2", b"returncode=-0"),
        (b"stdout_bytes=0", b"stdout_bytes=00"),
        (b"stderr_bytes=0", b"stderr_bytes=00"),
    ],
)
def test_parent_recomputes_predicates_and_rejects_noncanonical_numbers(
    original: bytes,
    replacement: bytes,
) -> None:
    valid = harness._crash_cell_refusal_line(
        harness.CrashCellContractError(
            operation="DELETE",
            cutpoint="AFTER_COMPLETE",
            returncode=2,
            stdout=b"",
            stderr=b"",
            failed_mask="1000",
        )
    )
    mutated = valid.replace(original, replacement, 1)
    assert mutated != valid

    detail = harness._safe_worker_refusal_detail(mutated)

    assert detail == (
        "worker-stderr-sha256=" + hashlib.sha256(mutated).hexdigest() + f",bytes={len(mutated)}"
    )


def test_worker_emits_the_closed_crash_cell_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[bytes] = []
    error = harness.CrashCellContractError(
        operation="FINALIZE",
        cutpoint="AFTER_INTENT",
        returncode=1,
        stdout=b"",
        stderr=b"private child detail",
        failed_mask="1011",
    )
    monkeypatch.setattr(
        harness,
        "_write_all",
        lambda _descriptor, payload: captured.append(payload),
    )

    assert harness._emit_worker_refusal(error) == 2
    assert captured == [harness._crash_cell_refusal_line(error)]
    assert b"private child detail" not in captured[0]


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


def test_formatted_image_must_remain_fully_allocated_after_mkfs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[object] = []
    blocks = 8

    def opened(path: Path, flags: int) -> int:
        observed.append((path, flags))
        return 17

    def metadata(_descriptor: int) -> object:
        return SimpleNamespace(
            st_mode=stat.S_IFREG | 0o600,
            st_nlink=1,
            st_size=4096,
            st_blocks=blocks,
        )

    monkeypatch.setattr(harness.os, "open", opened)
    monkeypatch.setattr(harness.os, "fstat", metadata)
    monkeypatch.setattr(harness.os, "close", lambda descriptor: observed.append(descriptor))

    harness._require_fully_allocated_image(Path("fixture.img"), 4096)
    assert observed[-1] == 17
    assert cast(tuple[Path, int], observed[0]) == (
        Path("fixture.img"),
        harness.os.O_RDONLY
        | getattr(harness.os, "O_CLOEXEC", 0)
        | getattr(harness.os, "O_NOFOLLOW", 0),
    )

    blocks = 7
    with pytest.raises(harness.HarnessError, match="not fully allocated"):
        harness._require_fully_allocated_image(Path("fixture.img"), 4096)
    assert observed[-1] == 17


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
    assert "CRASH_CELL_COUNT" in source
    assert '"--crash-cell"' in source
    assert '"--cell-operation"' in source
    assert '"--cell-cutpoint"' in source
    assert "--cell-catalog" not in source
    assert "--cell-root" not in source
    assert "max_active_leases=" not in source
    assert source.count("os.kill(os.getpid(), SIGKILL_NUMBER)") == 3
    assert '"sigkill-all-operation-cutpoint-matrix"' not in source
    for function in (
        '"crash_cell"',
        '"prepare_crash_intent"',
        '"run_crash_subprocess"',
        '"validate_crash_fixture_mount"',
        '"validate_crash_cell_environment"',
    ):
        assert function in source
    assert "os.posix_fallocate" in source
    assert source.count("os.posix_fallocate") == 2
    ext4_format = (
        'MKFS_EXT4,\n                "-F",\n                "-m",\n                "0",\n'
        '                "-E",\n                "nodiscard",\n                "-L",\n'
        '                "M10CAT",'
    )
    assert ext4_format in source
    assert source.index(ext4_format) < source.index(
        "_require_fully_allocated_image(ext4_image, EXT4_IMAGE_BYTES)"
    ) < source.index("_mount_loop(ext4_loop, ext4_image, catalog_mount, \"ext4\")")
    assert "stream.write" not in source
    assert "allocated.st_blocks * 512 < filler_size" in source
    assert "allocated.st_blocks * 512 < size" in source
    assert 'provisional_clip_pair(boot_id="m10loop", sequence=44)' in source
    assert 'finalized_unsynced_clip_pair(boot_id="m10loop", sequence=44)' in source
    assert "tuple(intent.intent_id for intent in pending) != consumed" in source
    assert "pending[0].kind is not IntentKind.PROTECT" in source
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
    assert "`-E nodiscard`" in readme
    assert "production_release_tested=false" in readme
    assert "physical_power_loss_tested=false" in readme
    assert "m10_exit_gate_closed=false" in readme
    assert "sixteen actual process-`SIGKILL` cells" in readme
    assert "not physical-power-loss evidence" in readme
    assert "900-second timeout" in readme
    assert "must not be committed" in readme
