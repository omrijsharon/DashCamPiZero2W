#!/usr/bin/env python3
"""Hash-closed, disposable-loop Milestone 10 component validation harness."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Final, cast
from uuid import UUID

SCHEMA_VERSION: Final = 2
EXPECTED_RELEASE: Final = "0.1.0.dev0-5f95dd806342ac9e"
EXPECTED_RELEASE_MANIFEST: Final = (
    "619fe30e8123e0ceaec55269de0a6faf6ec88ccb4859a98bbef2d87776dbb655"
)
EXPECTED_CONFIG_SHA256: Final = "1276363286475bccf85e70332ec893846e3fe3572e8184991843400ac4d6c4b8"
EXPECTED_BOARD_SERIAL: Final = "00000000db28ffe4"
EXPECTED_STORAGE_SOURCE: Final = "/dev/mmcblk0p3"
EXPECTED_STORAGE_UUID: Final = "7EED-3EA7"
EXPECTED_STORAGE_LABEL: Final = "DASHCAM"
RECORDING_ROOT: Final = Path("/srv/dashcam")
CONFIG_PATH: Final = Path("/etc/dashcam/config.toml")
SENTINEL_PATH: Final = RECORDING_ROOT / ".dashcam-volume"
PRODUCTION_CATALOG_MEMBERS: Final = (
    Path("/var/lib/dashcam/catalog.sqlite3"),
    Path("/var/lib/dashcam/catalog.sqlite3-wal"),
    Path("/var/lib/dashcam/catalog.sqlite3-shm"),
)
RESULT_FALSE_CLAIMS: Final = {
    "production_release_tested": False,
    "physical_power_loss_tested": False,
    "m10_exit_gate_closed": False,
    "production_daemon_tested": False,
    "production_camera_tested": False,
    "production_gstreamer_no_space_tested": False,
    "production_control_listener_service_tested": False,
    "download_data_plane_tested": False,
}
MATRIX_NAMES: Final = tuple("ABCDEFGH")
COMMIT_RE: Final = re.compile(r"[0-9a-f]{40}")
SHA256_RE: Final = re.compile(r"[0-9a-f]{64}")
LOOP_RE: Final = re.compile(r"/dev/loop[0-9]{1,4}")
DEVICE_RE: Final = re.compile(r"[0-9]{1,5}:[0-9]{1,10}")
WORKER_REFUSAL_RE: Final = re.compile(
    rb"REFUSED: H_(HARNESS|OS|UNICODE|ZIP|VALUE|ASSERT|ATTRIBUTE|KEY|RUNTIME|TYPE|"
    rb"EXCEPTION)_F([a-z][a-z0-9_]*)_L([1-9][0-9]{0,3})\n"
)
CRASH_CELL_REFUSAL_RE: Final = re.compile(
    rb"REFUSED: H_CRASH_CELL operation=(FINALIZE|PROTECT|UNPROTECT|DELETE) "
    rb"cutpoint=(AFTER_INTENT|AFTER_MEMBER1|AFTER_MEMBER2|AFTER_COMPLETE) "
    rb"returncode=(-?[0-9]{1,3}) stdout_bytes=([0-9]{1,3}) "
    rb"stdout_sha256=([0-9a-f]{64}) stderr_bytes=([0-9]{1,3}) "
    rb"stderr_sha256=([0-9a-f]{64}) failed=([01]{4})\n"
)
MAX_REVIEWED_RUN_LINE: Final = 8192
WORKER_DIAGNOSTIC_FUNCTIONS: Final = (
    "worker",
    "matrix_a",
    "matrix_b_c",
    "matrix_d",
    "matrix_e",
    "crash_cell",
    "prepare_crash_intent",
    "run_crash_subprocess",
    "validate_crash_fixture_mount",
    "validate_crash_cell_environment",
    "matrix_control_component",
    "validate_control_client_environment",
    "run_abandoned_lease_client",
    "raw_control_request",
    "validate_control_response",
    "cleanup_control_runtime_directory",
    "arm_parent_death_sigkill",
    "matrix_g",
    "write_result",
    "load_commit_source",
    "materialize_clip",
    "write_member",
    "stat_space",
    "device_id",
    "mount_loop",
    "unmount_owned",
    "detach_owned",
    "attach_loop",
    "require_owned_loop",
    "loop_backing_file",
    "blkid",
    "findmnt",
    "findmnt_backing",
    "observe_root_backing",
    "safe_worker_refusal_detail",
    "worker_refusal_line",
    "emit_worker_refusal",
)
MAX_BUNDLE_FILE_BYTES: Final = 16 * 1024 * 1024
MAX_SOURCE_MEMBERS: Final = 512
MAX_OUTPUT_BYTES: Final = 64 * 1024
COMMAND_TIMEOUT_S: Final = 120
WORKER_TIMEOUT_S: Final = 900
EXFAT_IMAGE_BYTES: Final = 480 * 1024**2
EXT4_IMAGE_BYTES: Final = 64 * 1024**2
MAX_FILLER_BYTES: Final = 256 * 1024**2
MAX_FILLER_ALLOCATION_CHUNK_BYTES: Final = 16 * 1024**2
MAX_FILLER_ALLOCATION_STEPS: Final = 64
MIN_FILLER_EMERGENCY_GUARD_BYTES: Final = 2 * 1024**2
ROOT_PRESERVED_FREE_BYTES: Final = 2 * 1024**3
ROOT_BOUNDED_OVERHEAD_BYTES: Final = 32 * 1024**2
EXPECTED_ROOT_SOURCE: Final = "/dev/mmcblk0p2"
MIN_ROOT_CAPACITY_BYTES: Final = 5 * 1024**3
MAX_ROOT_CAPACITY_BYTES: Final = 7 * 1024**3
MAX_SIGNED_BYTES: Final = 2**63 - 1
MAX_RECLAIM_STEPS: Final = 64
MAX_FIXTURE_FILES: Final = 256
CRASH_OPERATIONS: Final = ("FINALIZE", "PROTECT", "UNPROTECT", "DELETE")
CRASH_CUTPOINTS: Final = (
    "AFTER_INTENT",
    "AFTER_MEMBER1",
    "AFTER_MEMBER2",
    "AFTER_COMPLETE",
)
CRASH_CELL_COUNT: Final = len(CRASH_OPERATIONS) * len(CRASH_CUTPOINTS)
CRASH_CELL_TIMEOUT_S: Final = 15
CRASH_CELL_BASE_ORDER: Final = 180
SIGKILL_NUMBER: Final = 9
CRASH_INTENT_LINE_RE: Final = re.compile(
    rb"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    rb"[89ab][0-9a-f]{3}-[0-9a-f]{12}\n"
)
CONTROL_RUNTIME_PREFIX: Final = "dashcam-m10-control."
CONTROL_SOCKET_NAME: Final = "control.sock"
CONTROL_BOOT_ID: Final = "m10-control-boot-a"
CONTROL_NEXT_BOOT_ID: Final = "m10-control-boot-b"
CONTROL_LEASE_DURATION_NS: Final = 1_000_000_000
CONTROL_DURABLE_TIMEOUT_S: Final = 3.0
CONTROL_DISPATCHER_TIMEOUT_S: Final = 5.0
CONTROL_HANDLER_TIMEOUT_S: Final = 7.0
CONTROL_PROTOCOL_CLIENT_TIMEOUT_S: Final = 9.0
CONTROL_CLIENT_TIMEOUT_S: Final = 12
CONTROL_CLIENT_CONFIRMATION: Final = b"LEASE_ACQUIRED\n"
CONTROL_FIXTURE_BASE_ORDER: Final = 300
CONTROL_FIXTURE_CLIP_COUNT: Final = 38
CONTROL_MAX_ACTIVE_LEASES: Final = 32
PR_SET_PDEATHSIG: Final = 1
CONTROL_EVIDENCE_FIELDS: Final = frozenset(
    {
        "socket_is_unix",
        "socket_mode_0660",
        "socket_gid_dashcam_api",
        "socket_owner_root",
        "hard_admission_refused",
        "bounded_drain_completed",
        "raw_protocol_used",
        "lease_authority_opaque",
        "response_paths_absent",
        "abandoned_client_sigkill_observed",
        "lease_survived_client_loss",
        "listener_dispatcher_restart_preserved_lease",
        "restart_release_authority_succeeded",
        "wrong_release_authority_refused",
        "idempotent_second_release",
        "active_lease_cap",
        "listener_admission_cap",
        "configured_lease_timeout_s",
        "global_cap_refused",
        "same_boot_preexpiry_excluded",
        "same_boot_exact_expiry_cleared",
        "postexpiry_retention_eligible",
        "previous_boot_lease_cleared",
        "manual_lease_path_frozen",
        "manual_post_release_protect_converged",
        "manual_protect_pair_converged",
        "manual_unprotect_pair_converged",
        "event_lease_path_frozen",
        "event_expiry_repair_converged",
        "event_previous_count",
        "event_current_count",
        "event_next_count",
        "event_runtime_callback_seam_used",
        "event_retry_without_active_idempotent",
        "event_pair_intents_converged",
        "component_scope",
        "production_listener_service_tested",
        "download_data_plane_tested",
        "production_runtime_tested",
        "production_camera_tested",
    }
)

FINDMNT: Final = "/usr/bin/findmnt"
MOUNT: Final = "/usr/bin/mount"
UMOUNT: Final = "/usr/bin/umount"
UNSHARE: Final = "/usr/bin/unshare"
SYSTEMCTL: Final = "/usr/bin/systemctl"
NMCLI: Final = "/usr/bin/nmcli"
SYNC: Final = "/usr/bin/sync"
LOSSETUP: Final = "/usr/sbin/losetup"
BLKID: Final = "/usr/sbin/blkid"
MKFS_EXFAT: Final = "/usr/sbin/mkfs.exfat"
MKFS_EXT4: Final = "/usr/sbin/mkfs.ext4"
FSCK_EXFAT: Final = "/usr/sbin/fsck.exfat"
E2FSCK: Final = "/usr/sbin/e2fsck"
REQUIRED_EXECUTABLES: Final = (
    FINDMNT,
    MOUNT,
    UMOUNT,
    UNSHARE,
    SYSTEMCTL,
    NMCLI,
    SYNC,
    LOSSETUP,
    BLKID,
    MKFS_EXFAT,
    MKFS_EXT4,
    FSCK_EXFAT,
    E2FSCK,
)


class HarnessError(RuntimeError):
    """A safety, identity, evidence, or bound check refused the run."""


class CrashCellContractError(HarnessError):
    """One crash child returned bounded evidence that missed its closed contract."""

    def __init__(
        self,
        *,
        operation: str,
        cutpoint: str,
        returncode: int,
        stdout: bytes,
        stderr: bytes,
        failed_mask: str,
    ) -> None:
        super().__init__("crash-cell did not terminate at the exact SIGKILL cutpoint")
        self.operation = operation
        self.cutpoint = cutpoint
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.failed_mask = failed_mask


@dataclass(frozen=True, slots=True)
class RootBackingObservation:
    device_id: str
    source: str
    target: str
    filesystem: str
    capacity_bytes: int
    free_bytes: int


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    written = 0
    while written < len(view):
        count = os.write(descriptor, view[written:])
        if count <= 0:
            raise HarnessError("bounded file write made no progress")
        written += count


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _bounded_regular_bytes(path: Path, maximum: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise HarnessError(f"no-follow input open refused: {path.name}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= maximum:
            raise HarnessError(f"bounded regular input refused: {path.name}")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) != metadata.st_size or len(payload) > maximum:
            raise HarnessError(f"input size changed or exceeded its bound: {path.name}")
        return payload
    finally:
        os.close(descriptor)


def _bounded_virtual_bytes(path: Path, maximum: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if not payload or len(payload) > maximum:
            raise HarnessError(f"virtual input is empty or excessive: {path.name}")
        return payload
    finally:
        os.close(descriptor)


def _strict_json(payload: bytes, label: str) -> dict[str, object]:
    try:
        value = json.loads(payload.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HarnessError(f"{label} is not canonical ASCII JSON") from error
    if not isinstance(value, dict) or canonical_json(value) != payload:
        raise HarnessError(f"{label} differs from canonical JSON")
    return cast(dict[str, object], value)


def parse_manifest(payload: bytes) -> dict[str, str]:
    if len(payload) > 8192 or not payload.endswith(b"\n"):
        raise HarnessError("manifest is missing or excessive")
    result: dict[str, str] = {}
    try:
        lines = payload.decode("ascii").splitlines()
    except UnicodeDecodeError as error:
        raise HarnessError("manifest is not ASCII") from error
    for line in lines:
        digest, separator, name = line.partition("  ")
        if (
            not separator
            or SHA256_RE.fullmatch(digest) is None
            or name in result
            or name not in {"README.md", "run.py", "SOURCE.json", "dashcam-source.zip"}
        ):
            raise HarnessError("manifest contains an unsafe row")
        result[name] = digest
    if set(result) != {"README.md", "run.py", "SOURCE.json", "dashcam-source.zip"}:
        raise HarnessError("manifest member set differs")
    return result


def validate_source_metadata(value: Mapping[str, object], expected_commit: str) -> None:
    if set(value) != {
        "schema_version",
        "git_commit",
        "git_tree",
        "archive_name",
        "archive_sha256",
        "archive_size",
        "members",
    }:
        raise HarnessError("source metadata keys differ")
    members = value["members"]
    if (
        value["schema_version"] != 1
        or value["git_commit"] != expected_commit
        or COMMIT_RE.fullmatch(cast(str, value["git_tree"])) is None
        or value["archive_name"] != "dashcam-source.zip"
        or SHA256_RE.fullmatch(cast(str, value["archive_sha256"])) is None
        or isinstance(value["archive_size"], bool)
        or not isinstance(value["archive_size"], int)
        or not 0 < value["archive_size"] <= MAX_BUNDLE_FILE_BYTES
        or not isinstance(members, dict)
        or not 1 <= len(members) <= MAX_SOURCE_MEMBERS
    ):
        raise HarnessError("source metadata values differ")
    for name, raw_facts in cast(dict[str, object], members).items():
        path = PurePosixPath(name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or not name.startswith("dashcam/")
            or not (name.endswith(".py") or name.endswith("/py.typed"))
            or not isinstance(raw_facts, dict)
        ):
            raise HarnessError("source member path is unsafe")
        facts = cast(dict[str, object], raw_facts)
        if (
            set(facts) != {"sha256", "size"}
            or not isinstance(facts["sha256"], str)
            or SHA256_RE.fullmatch(facts["sha256"]) is None
            or isinstance(facts["size"], bool)
            or not isinstance(facts["size"], int)
            or not 0 <= facts["size"] <= MAX_BUNDLE_FILE_BYTES
        ):
            raise HarnessError("source member facts are unsafe")
    if "dashcam/storage/reclaimer.py" not in members:
        raise HarnessError("reclaimer source is absent")


def verify_bundle(
    root: Path, expected_manifest_sha256: str, expected_commit: str
) -> dict[str, object]:
    if SHA256_RE.fullmatch(expected_manifest_sha256) is None:
        raise HarnessError("expected manifest SHA-256 is malformed")
    if COMMIT_RE.fullmatch(expected_commit) is None:
        raise HarnessError("expected source commit is malformed")
    root = root.resolve(strict=True)
    manifest_payload = _bounded_regular_bytes(root / "SHA256SUMS", 8192)
    if _sha256(manifest_payload) != expected_manifest_sha256:
        raise HarnessError("manifest differs from the reviewed hash")
    manifest = parse_manifest(manifest_payload)
    allowed = set(manifest) | {"SHA256SUMS"}
    actual = {entry.name for entry in root.iterdir()}
    if actual != allowed:
        raise HarnessError("bundle directory member set differs")
    payloads: dict[str, bytes] = {}
    for name, digest in manifest.items():
        payload = _bounded_regular_bytes(root / name, MAX_BUNDLE_FILE_BYTES)
        if _sha256(payload) != digest:
            raise HarnessError(f"bundle member hash differs: {name}")
        payloads[name] = payload
    metadata = _strict_json(payloads["SOURCE.json"], "SOURCE.json")
    validate_source_metadata(metadata, expected_commit)
    archive_payload = payloads["dashcam-source.zip"]
    if (
        len(archive_payload) != metadata["archive_size"]
        or _sha256(archive_payload) != metadata["archive_sha256"]
    ):
        raise HarnessError("source archive identity differs")
    expected_members = cast(dict[str, dict[str, object]], metadata["members"])
    with zipfile.ZipFile(root / "dashcam-source.zip") as archive:
        if archive.comment:
            raise HarnessError("source archive comment differs")
        infos = archive.infolist()
        names = [info.filename for info in infos]
        total_size = sum(info.file_size for info in infos)
        if (
            len(infos) > MAX_SOURCE_MEMBERS
            or total_size > MAX_BUNDLE_FILE_BYTES
            or len(names) != len(set(names))
            or set(names) != set(expected_members)
        ):
            raise HarnessError("source archive member set differs")
        for info in infos:
            if (
                info.is_dir()
                or info.compress_type != zipfile.ZIP_STORED
                or info.file_size > MAX_BUNDLE_FILE_BYTES
                or info.compress_size != info.file_size
                or info.date_time != (1980, 1, 1, 0, 0, 0)
                or info.create_system != 3
                or info.external_attr >> 16 != 0o100644
                or info.extra
                or info.comment
            ):
                raise HarnessError("source archive member is unsafe")
            payload = archive.read(info, pwd=None)
            facts = expected_members[info.filename]
            if len(payload) != facts["size"] or _sha256(payload) != facts["sha256"]:
                raise HarnessError("source archive member identity differs")
    return metadata


def validate_mount_identity(
    row: Mapping[str, object],
    *,
    source: str,
    target: str,
    filesystem: str,
    uuid: str,
    label: str,
) -> None:
    required = {"source", "target", "fstype", "uuid", "label", "options"}
    if set(row) != required:
        raise HarnessError("mount identity fields differ")
    options = row["options"]
    if (
        row["source"] != source
        or row["target"] != target
        or row["fstype"] != filesystem
        or row["uuid"] != uuid
        or row["label"] != label
        or not isinstance(options, str)
        or "rw" not in options.split(",")
    ):
        raise HarnessError("mount identity differs")


def validate_loop_identity(
    loop: Path, image: Path, *, stat_result: os.stat_result, backing_file: str
) -> None:
    if LOOP_RE.fullmatch(loop.as_posix()) is None or not stat.S_ISBLK(stat_result.st_mode):
        raise HarnessError("loop target is not a numbered block device")
    if not Path(backing_file).is_absolute() or Path(backing_file).resolve() != image.resolve():
        raise HarnessError("loop backing-file identity differs")


def validate_threshold_evidence(cases: Sequence[Mapping[str, object]]) -> None:
    expected = (
        ("start_equal", "NORMAL", False),
        ("start_minus_one", "RECLAIMING", True),
        ("high_minus_one", "RECLAIMING", True),
        ("high_equal", "NORMAL", False),
        ("emergency_equal", "RECLAIMING", True),
        ("emergency_minus_one", "EMERGENCY", True),
        ("no_space_write", "EMERGENCY", True),
        ("restart_below_high", "RECLAIMING", True),
        ("restart_high_equal", "NORMAL", False),
    )
    observed = tuple(
        (case.get("name"), case.get("mode"), case.get("reclaim_latched")) for case in cases
    )
    if observed != expected:
        raise HarnessError("threshold equality or durable restart evidence differs")


def validate_cleanup_identity(
    *, expected_loop: str, expected_image: Path, mount_source: str | None, backing_file: str
) -> None:
    if (
        LOOP_RE.fullmatch(expected_loop) is None
        or mount_source not in {None, expected_loop}
        or not Path(backing_file).is_absolute()
        or Path(backing_file).resolve() != expected_image.resolve()
    ):
        raise HarnessError("cleanup target no longer has exact disposable identity")


def validate_privacy(value: object) -> None:
    forbidden_keys = {
        "latitude",
        "longitude",
        "coordinates",
        "nmea",
        "ssid",
        "psk",
        "lease_id",
        "lease_holder",
        "holder",
        "approved_path",
        "video_path",
        "sidecar_path",
    }
    forbidden_fragments = ("$GPGGA", "$GPRMC", "$GNGGA", "$GNRMC")
    stack = [value]
    visited = 0
    while stack:
        item = stack.pop()
        visited += 1
        if visited > 10_000:
            raise HarnessError("privacy scan exceeded its bound")
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or key.casefold() in forbidden_keys:
                    raise HarnessError("result contains a forbidden privacy key")
                stack.append(child)
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
        elif isinstance(item, str) and (
            len(item) > 4096 or any(part in item for part in forbidden_fragments)
        ):
            raise HarnessError("result contains raw or excessive private text")


def validate_sigkill_matrix_evidence(matrix: Mapping[str, object]) -> None:
    raw_cells = matrix.get("cells")
    if (
        matrix.get("operations") != len(CRASH_OPERATIONS)
        or matrix.get("cutpoints_per_operation") != len(CRASH_CUTPOINTS)
        or matrix.get("cell_count") != CRASH_CELL_COUNT
        or matrix.get("actual_sigkill_cells") != CRASH_CELL_COUNT
        or matrix.get("fresh_catalogs") != CRASH_CELL_COUNT
        or matrix.get("sigkill_cutpoint_matrix_tested") is not True
        or matrix.get("physical_power_loss_tested") is not False
        or not isinstance(raw_cells, list)
        or len(raw_cells) != CRASH_CELL_COUNT
    ):
        raise HarnessError("SIGKILL matrix summary differs")
    expected = {
        (operation, cutpoint)
        for operation in CRASH_OPERATIONS
        for cutpoint in CRASH_CUTPOINTS
    }
    observed: set[tuple[str, str]] = set()
    recovery_actions = {
        "AFTER_INTENT": 2,
        "AFTER_MEMBER1": 1,
        "AFTER_MEMBER2": 0,
        "AFTER_COMPLETE": 0,
    }
    for raw_cell in raw_cells:
        if not isinstance(raw_cell, dict):
            raise HarnessError("SIGKILL matrix cell is not an object")
        cell = cast(dict[str, object], raw_cell)
        if set(cell) != {
            "operation",
            "cutpoint",
            "sigkill_observed",
            "reopen_reconciled",
            "idempotent_reconcile",
            "recovery_actions",
        }:
            raise HarnessError("SIGKILL matrix cell fields differ")
        operation = cell["operation"]
        cutpoint = cell["cutpoint"]
        if not isinstance(operation, str) or not isinstance(cutpoint, str):
            raise HarnessError("SIGKILL matrix cell identity differs")
        identity = (operation, cutpoint)
        if (
            identity not in expected
            or identity in observed
            or cell["sigkill_observed"] is not True
            or cell["reopen_reconciled"] is not True
            or cell["idempotent_reconcile"] is not True
            or cell["recovery_actions"] != recovery_actions[cutpoint]
        ):
            raise HarnessError("SIGKILL matrix cell evidence differs")
        observed.add(identity)
    if observed != expected:
        raise HarnessError("SIGKILL matrix coverage differs")


def validate_result_evidence(result: Mapping[str, object]) -> None:
    if result.get("schema_version") != SCHEMA_VERSION:
        raise HarnessError("result schema version differs")
    raw_matrices = result.get("matrices")
    if not isinstance(raw_matrices, dict) or set(raw_matrices) != set(MATRIX_NAMES):
        raise HarnessError("matrix result set is incomplete")
    for name, matrix in cast(dict[str, object], result["matrices"]).items():
        if not isinstance(matrix, dict) or matrix.get("passed") is not True:
            raise HarnessError(f"matrix {name} did not pass its declared component scope")
    matrices = cast(dict[str, dict[str, object]], result["matrices"])
    validate_control_component_evidence(matrices["H"].get("control_component"))
    validate_threshold_evidence(cast(Sequence[Mapping[str, object]], matrices["A"].get("cases")))
    validate_sigkill_matrix_evidence(matrices["E"])
    semantic_checks = (
        matrices["A"].get("identity_drift_refused") is True,
        matrices["A"].get("capacity_drift_refused") is True,
        matrices["A"].get("invalid_observation_bounded") is True,
        matrices["A"].get("observation_failure_bounded") is True,
        cast(int, matrices["B"].get("oldest_first_pair_count", 0)) > 0,
        matrices["B"].get("one_pair_per_observation") is True,
        matrices["B"].get("repeated_cycle_count") == 3,
        matrices["B"].get("high_water_stop_bounded") is True,
        0
        < cast(int, matrices["B"].get("filler_allocation_steps", 0))
        <= MAX_FILLER_ALLOCATION_STEPS,
        0 < cast(int, matrices["B"].get("filler_bytes", 0)) <= MAX_FILLER_BYTES,
        matrices["C"].get("protected_excluded") is True,
        matrices["C"].get("active_lease_excluded") is True,
        matrices["C"].get("pending_mutation_excluded") is True,
        matrices["C"].get("finalizing_pair_excluded") is True,
        matrices["C"].get("unknown_files_unchanged") is True,
        matrices["D"].get("previous_count") == 2,
        matrices["D"].get("current_count") == 1,
        matrices["D"].get("next_count") == 1,
        matrices["F"].get("exfat_read_only_fsck_status") == 0,
        matrices["F"].get("ext4_read_only_fsck_status") == 0,
        matrices["G"].get("no_eligible_candidate") is True,
        matrices["G"].get("protected_pair_unchanged") is True,
        matrices["H"].get("private_mount_namespace") is True,
        matrices["H"].get("network_namespace_unchanged") is True,
    )
    if not all(semantic_checks):
        raise HarnessError("one or more matrix semantic evidence fields differ")
    for name, expected in RESULT_FALSE_CLAIMS.items():
        if result.get(name) is not expected:
            raise HarnessError(f"unsafe acceptance claim: {name}")
    validate_privacy(result)


def validate_control_component_evidence(raw: object) -> None:
    """Require semantic control/lease evidence without retaining authorities or paths."""

    if not isinstance(raw, dict):
        raise HarnessError("control component evidence is absent")
    evidence = cast(dict[str, object], raw)
    if set(evidence) != CONTROL_EVIDENCE_FIELDS:
        raise HarnessError("control component evidence fields differ")
    expected_true = (
        "socket_is_unix",
        "socket_mode_0660",
        "socket_gid_dashcam_api",
        "socket_owner_root",
        "hard_admission_refused",
        "bounded_drain_completed",
        "raw_protocol_used",
        "lease_authority_opaque",
        "response_paths_absent",
        "abandoned_client_sigkill_observed",
        "lease_survived_client_loss",
        "listener_dispatcher_restart_preserved_lease",
        "restart_release_authority_succeeded",
        "wrong_release_authority_refused",
        "idempotent_second_release",
        "global_cap_refused",
        "same_boot_preexpiry_excluded",
        "same_boot_exact_expiry_cleared",
        "postexpiry_retention_eligible",
        "previous_boot_lease_cleared",
        "manual_lease_path_frozen",
        "manual_post_release_protect_converged",
        "manual_protect_pair_converged",
        "manual_unprotect_pair_converged",
        "event_lease_path_frozen",
        "event_expiry_repair_converged",
        "event_runtime_callback_seam_used",
        "event_retry_without_active_idempotent",
        "event_pair_intents_converged",
    )
    if any(evidence.get(name) is not True for name in expected_true):
        raise HarnessError("control component semantic evidence differs")
    if (
        evidence.get("active_lease_cap") != CONTROL_MAX_ACTIVE_LEASES
        or evidence.get("listener_admission_cap") != 8
        or evidence.get("configured_lease_timeout_s") != 1
        or evidence.get("event_previous_count") != 2
        or evidence.get("event_current_count") != 1
        or evidence.get("event_next_count") != 1
        or evidence.get("component_scope") != "commit-source-private-loop"
        or evidence.get("production_listener_service_tested") is not False
        or evidence.get("download_data_plane_tested") is not False
        or evidence.get("production_runtime_tested") is not False
        or evidence.get("production_camera_tested") is not False
    ):
        raise HarnessError("control component scope evidence differs")


def _run(
    command: Sequence[str],
    *,
    accepted: frozenset[int] = frozenset({0}),
    timeout: int = COMMAND_TIMEOUT_S,
    safe_worker_refusal: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    if (
        not command
        or command[0] not in REQUIRED_EXECUTABLES
        or not 1 <= timeout <= WORKER_TIMEOUT_S
        or any(not item or "\x00" in item or len(item) > 4096 for item in command)
    ):
        raise HarnessError("command differs from the closed allowlist")
    try:
        result = subprocess.run(
            list(command),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=timeout,
            start_new_session=True,
            env={
                "LC_ALL": "C",
                "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                "PYTHONNOUSERSITE": "1",
            },
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HarnessError(f"bounded command failed: {command[0]}") from error
    if len(result.stdout) > MAX_OUTPUT_BYTES or len(result.stderr) > MAX_OUTPUT_BYTES:
        raise HarnessError(f"command output exceeded its bound: {command[0]}")
    if result.returncode not in accepted:
        message = f"command failed with status {result.returncode}: {command[0]}"
        if safe_worker_refusal:
            message += ": " + _safe_worker_refusal_detail(result.stderr)
        raise HarnessError(message)
    return result


def _safe_worker_refusal_detail(stderr: bytes) -> str:
    if not isinstance(stderr, bytes):
        return "worker-stderr-unavailable"
    match = WORKER_REFUSAL_RE.fullmatch(stderr)
    if match is not None:
        function = match.group(2).decode("ascii")
        line = int(match.group(3))
        if line in _reviewed_function_lines().get(function, frozenset()):
            return stderr[:-1].decode("ascii")
    crash_match = CRASH_CELL_REFUSAL_RE.fullmatch(stderr)
    if crash_match is not None:
        returncode_token = crash_match.group(3).decode("ascii")
        stdout_bytes_token = crash_match.group(4).decode("ascii")
        stderr_bytes_token = crash_match.group(6).decode("ascii")
        returncode = int(returncode_token)
        stdout_bytes = int(stdout_bytes_token)
        stdout_sha256 = crash_match.group(5).decode("ascii")
        stderr_bytes = int(stderr_bytes_token)
        stderr_sha256 = crash_match.group(7).decode("ascii")
        failed_mask = crash_match.group(8)
        empty_sha256 = _sha256(b"")
        expected_mask_prefix = bytes(
            (
                ord("1") if returncode != -SIGKILL_NUMBER else ord("0"),
                ord("1") if stdout_bytes > 512 else ord("0"),
                ord("1") if stderr_bytes != 0 else ord("0"),
            )
        )
        if (
            -255 <= returncode <= 255
            and stdout_bytes <= 513
            and stderr_bytes <= 513
            and returncode_token == str(returncode)
            and stdout_bytes_token == str(stdout_bytes)
            and stderr_bytes_token == str(stderr_bytes)
            and (stdout_bytes != 0 or stdout_sha256 == empty_sha256)
            and (stderr_bytes != 0 or stderr_sha256 == empty_sha256)
            and failed_mask[:3] == expected_mask_prefix
            and failed_mask != b"0000"
        ):
            return stderr[:-1].decode("ascii")
    return f"worker-stderr-sha256={_sha256(stderr)},bytes={len(stderr)}"


def _crash_cell_refusal_line(error: CrashCellContractError) -> bytes:
    if (
        error.operation not in CRASH_OPERATIONS
        or error.cutpoint not in CRASH_CUTPOINTS
        or isinstance(error.returncode, bool)
        or not isinstance(error.returncode, int)
        or not -255 <= error.returncode <= 255
        or len(error.stdout) > 513
        or len(error.stderr) > 513
        or re.fullmatch(r"[01]{4}", error.failed_mask) is None
        or error.failed_mask == "0000"
    ):
        raise HarnessError("crash-cell diagnostic fields differ from their bounds")
    return (
        "REFUSED: H_CRASH_CELL "
        f"operation={error.operation} cutpoint={error.cutpoint} "
        f"returncode={error.returncode} "
        f"stdout_bytes={len(error.stdout)} stdout_sha256={_sha256(error.stdout)} "
        f"stderr_bytes={len(error.stderr)} stderr_sha256={_sha256(error.stderr)} "
        f"failed={error.failed_mask}\n"
    ).encode("ascii")


def _worker_exception_category(error: Exception) -> str:
    for expected, category in (
        (HarnessError, "HARNESS"),
        (UnicodeError, "UNICODE"),
        (zipfile.BadZipFile, "ZIP"),
        (OSError, "OS"),
        (AssertionError, "ASSERT"),
        (AttributeError, "ATTRIBUTE"),
        (KeyError, "KEY"),
        (RuntimeError, "RUNTIME"),
        (TypeError, "TYPE"),
        (ValueError, "VALUE"),
    ):
        if isinstance(error, expected):
            return category
    return "EXCEPTION"


def _reviewed_function_lines() -> dict[str, frozenset[int]]:
    result: dict[str, frozenset[int]] = {}
    reviewed_path = Path(__file__).resolve(strict=True)
    for function_code in WORKER_DIAGNOSTIC_FUNCTIONS:
        function = globals().get("_" + function_code)
        code = getattr(function, "__code__", None)
        if code is None:
            continue
        try:
            code_path = Path(code.co_filename).resolve(strict=True)
        except OSError:
            continue
        if code_path != reviewed_path:
            continue
        lines = frozenset(
            line
            for _start, _end, line in code.co_lines()
            if line is not None and 1 <= line <= MAX_REVIEWED_RUN_LINE
        )
        if lines:
            result[function_code] = lines
    return result


def _reviewed_exception_location(error: Exception) -> tuple[str, int] | None:
    reviewed_path = Path(__file__).resolve(strict=True)
    line_map = _reviewed_function_lines()
    selected: tuple[str, int] | None = None
    traceback = error.__traceback__
    while traceback is not None:
        try:
            frame_path = Path(traceback.tb_frame.f_code.co_filename).resolve(strict=True)
        except OSError:
            frame_path = Path()
        function_name = traceback.tb_frame.f_code.co_name.removeprefix("_")
        if frame_path == reviewed_path and traceback.tb_lineno in line_map.get(
            function_name, frozenset()
        ):
            selected = (function_name, traceback.tb_lineno)
        traceback = traceback.tb_next
    return selected


def _worker_refusal_line(error: Exception) -> bytes:
    category = _worker_exception_category(error)
    location = _reviewed_exception_location(error)
    if location is None:
        return b"REFUSED: H_UNLOCATED\n"
    function, line = location
    payload = f"REFUSED: H_{category}_F{function}_L{line}\n".encode("ascii")
    match = WORKER_REFUSAL_RE.fullmatch(payload)
    if match is None or line not in _reviewed_function_lines().get(function, frozenset()):
        return b"REFUSED: H_UNLOCATED\n"
    return payload


def _findmnt(target: Path) -> dict[str, str] | None:
    result = _run(
        (
            FINDMNT,
            "--json",
            "--mountpoint",
            str(target),
            "--output",
            "SOURCE,TARGET,FSTYPE,UUID,LABEL,OPTIONS",
        ),
        accepted=frozenset({0, 1}),
    )
    if result.returncode == 1:
        return None
    try:
        value = json.loads(result.stdout.decode("utf-8"))
        rows = value["filesystems"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise HarnessError("findmnt returned malformed JSON") from error
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise HarnessError("findmnt returned an ambiguous mount")
    raw = cast(dict[str, object], rows[0])
    translated = {
        "source": raw.get("source"),
        "target": raw.get("target"),
        "fstype": raw.get("fstype"),
        "uuid": raw.get("uuid"),
        "label": raw.get("label"),
        "options": raw.get("options"),
    }
    if any(not isinstance(item, str) or len(item) > 4096 for item in translated.values()):
        raise HarnessError("findmnt returned unsafe values")
    return cast(dict[str, str], translated)


def _findmnt_backing(target: Path) -> dict[str, str]:
    result = _run(
        (
            FINDMNT,
            "--json",
            "--target",
            str(target),
            "--output",
            "SOURCE,TARGET,FSTYPE",
        )
    )
    try:
        rows = json.loads(result.stdout.decode("utf-8"))["filesystems"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise HarnessError("findmnt backing-filesystem output is malformed") from error
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise HarnessError("findmnt backing-filesystem identity is ambiguous")
    row = cast(dict[str, object], rows[0])
    values = {
        "source": row.get("source"),
        "target": row.get("target"),
        "fstype": row.get("fstype"),
    }
    if any(not isinstance(value, str) or not value for value in values.values()):
        raise HarnessError("findmnt backing-filesystem values are unsafe")
    return cast(dict[str, str], values)


def required_root_free_bytes(
    *,
    exfat_image_bytes: int = EXFAT_IMAGE_BYTES,
    ext4_image_bytes: int = EXT4_IMAGE_BYTES,
    overhead_bytes: int = ROOT_BOUNDED_OVERHEAD_BYTES,
    preserved_free_bytes: int = ROOT_PRESERVED_FREE_BYTES,
) -> int:
    values = (
        exfat_image_bytes,
        ext4_image_bytes,
        overhead_bytes,
        preserved_free_bytes,
    )
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
        raise HarnessError("root-space budget contains an invalid byte count")
    total = 0
    for value in values:
        if value > MAX_SIGNED_BYTES - total:
            raise HarnessError("root-space budget overflows its checked integer range")
        total += value
    return total


def _validate_root_backing_identity(observation: RootBackingObservation) -> None:
    if (
        not isinstance(observation, RootBackingObservation)
        or DEVICE_RE.fullmatch(observation.device_id) is None
        or observation.source != EXPECTED_ROOT_SOURCE
        or observation.target != "/"
        or observation.filesystem != "ext4"
        or not MIN_ROOT_CAPACITY_BYTES <= observation.capacity_bytes <= MAX_ROOT_CAPACITY_BYTES
        or not 0 <= observation.free_bytes <= observation.capacity_bytes
    ):
        raise HarnessError("/var/tmp backing-filesystem identity or capacity differs")


def validate_root_backing_preflight(observation: RootBackingObservation) -> None:
    _validate_root_backing_identity(observation)
    if observation.free_bytes < required_root_free_bytes():
        raise HarnessError("/var/tmp lacks the bounded fixture budget plus 2 GiB reserve")


def validate_root_backing_poststate(
    before: RootBackingObservation, after: RootBackingObservation
) -> None:
    _validate_root_backing_identity(after)
    if (
        after.device_id != before.device_id
        or after.source != before.source
        or after.target != before.target
        or after.filesystem != before.filesystem
        or after.capacity_bytes != before.capacity_bytes
    ):
        raise HarnessError("/var/tmp backing-filesystem identity drifted")
    if after.free_bytes < ROOT_PRESERVED_FREE_BYTES:
        raise HarnessError("root free space did not restore the preserved 2 GiB reserve")


def validate_root_remaining_budget(
    observation: RootBackingObservation, *, remaining_allocation_bytes: int
) -> None:
    _validate_root_backing_identity(observation)
    required = required_root_free_bytes(
        exfat_image_bytes=remaining_allocation_bytes,
        ext4_image_bytes=0,
    )
    if observation.free_bytes < required:
        raise HarnessError("root reserve fell below the remaining allocation budget")


def _observe_root_backing(*, require_fixture_budget: bool = True) -> RootBackingObservation:
    temporary = Path("/var/tmp")
    root = Path("/")
    temporary_metadata = temporary.stat()
    root_metadata = root.stat()
    if temporary_metadata.st_dev != root_metadata.st_dev:
        raise HarnessError("/var/tmp is not on the expected root backing device")
    row = _findmnt_backing(temporary)
    values = os.statvfs(temporary)  # type: ignore[attr-defined]
    if (
        values.f_blocks <= 0
        or values.f_frsize <= 0
        or values.f_bavail < 0
        or values.f_bavail > values.f_blocks
    ):
        raise HarnessError("/var/tmp returned invalid statvfs values")
    capacity = values.f_blocks * values.f_frsize
    free = values.f_bavail * values.f_frsize
    if capacity > MAX_SIGNED_BYTES or free > MAX_SIGNED_BYTES:
        raise HarnessError("/var/tmp space observation exceeds its integer bound")
    observation = RootBackingObservation(
        device_id=f"{os.major(temporary_metadata.st_dev)}:{os.minor(temporary_metadata.st_dev)}",  # type: ignore[attr-defined]
        source=row["source"],
        target=row["target"],
        filesystem=row["fstype"],
        capacity_bytes=capacity,
        free_bytes=free,
    )
    if require_fixture_budget:
        validate_root_backing_preflight(observation)
    else:
        _validate_root_backing_identity(observation)
    return observation


def _file_fact(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"exists": False}
    payload = _bounded_regular_bytes(path, MAX_BUNDLE_FILE_BYTES)
    return {"exists": True, "size": len(payload), "sha256": _sha256(payload)}


def _board_serial() -> str:
    payload = _bounded_virtual_bytes(Path("/proc/cpuinfo"), 256 * 1024).decode(
        "ascii", errors="strict"
    )
    serials = [
        line.partition(":")[2].strip() for line in payload.splitlines() if line.startswith("Serial")
    ]
    if len(serials) != 1 or re.fullmatch(r"[0-9a-f]{16}", serials[0]) is None:
        raise HarnessError("board serial is absent or malformed")
    return serials[0]


def _unit_properties(name: str) -> dict[str, str]:
    result = _run(
        (
            SYSTEMCTL,
            "show",
            name,
            "--property=LoadState,ActiveState,SubState,NRestarts",
            "--no-pager",
        )
    )
    values: dict[str, str] = {}
    for line in result.stdout.decode("ascii").splitlines():
        key, separator, value = line.partition("=")
        if not separator or key in values:
            raise HarnessError("systemd property output is malformed")
        values[key] = value
    if set(values) != {"LoadState", "ActiveState", "SubState", "NRestarts"}:
        raise HarnessError("systemd property set differs")
    return values


def _network_snapshot() -> dict[str, object]:
    nm = _unit_properties("NetworkManager.service")
    ssh = _unit_properties("ssh.service")
    if nm["ActiveState"] != "active" or ssh["ActiveState"] != "active":
        raise HarnessError("network or SSH service is not active")
    state = _run((NMCLI, "--terse", "--fields", "STATE", "general", "status")).stdout
    state_text = state.decode("ascii").strip()
    if not state_text or len(state_text) > 128:
        raise HarnessError("NetworkManager state is unsafe")
    try:
        with socket.create_connection(("127.0.0.1", 22), timeout=3) as stream:
            stream.settimeout(3)
            banner = stream.recv(256)
    except OSError as error:
        raise HarnessError("SSH loopback listener is unavailable") from error
    if not banner.startswith(b"SSH-2.0-") or b"\n" not in banner:
        raise HarnessError("SSH banner differs")
    return {
        "networkmanager": nm,
        "ssh": ssh,
        "state": state_text,
        "network_namespace": os.readlink("/proc/self/ns/net"),
    }


def _release_snapshot() -> dict[str, object]:
    current = Path("/opt/dashcam/current")
    if not current.is_symlink():
        raise HarnessError("current release link is absent")
    target = os.readlink(current)
    if target != f"releases/{EXPECTED_RELEASE}":
        raise HarnessError("installed release differs from the accepted dormant release")
    marker = _strict_json(
        _bounded_regular_bytes(current.resolve(strict=True) / "installed.json", 8192),
        "installed release marker",
    )
    if marker != {
        "manifest_sha256": EXPECTED_RELEASE_MANIFEST,
        "release_id": EXPECTED_RELEASE,
        "schema_version": 1,
    }:
        raise HarnessError("installed release marker differs")
    expected_python = current.resolve(strict=True) / "venv/bin/python"
    if Path(sys.executable).resolve(strict=True) != expected_python.resolve(strict=True):
        raise HarnessError("harness interpreter is not the accepted release interpreter")
    if _sha256(_bounded_regular_bytes(CONFIG_PATH, 1024 * 1024)) != EXPECTED_CONFIG_SHA256:
        raise HarnessError("managed production config differs")
    daemon = _unit_properties("dashcamd.service")
    if daemon["LoadState"] != "loaded" or daemon["ActiveState"] != "inactive":
        raise HarnessError("dashcamd must remain loaded and inactive")
    return {
        "release_id": EXPECTED_RELEASE,
        "manifest_sha256": EXPECTED_RELEASE_MANIFEST,
        "daemon": daemon,
    }


def _throttle_snapshot() -> str:
    executable = "/usr/bin/vcgencmd"
    if not Path(executable).is_file() or not os.access(executable, os.X_OK):
        raise HarnessError("vcgencmd is unavailable")
    # Deliberately local: vcgencmd is read-only but not part of worker mutation allowlist.
    try:
        result = subprocess.run(
            [executable, "get_throttled"],
            capture_output=True,
            check=False,
            timeout=5,
            env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HarnessError("bounded throttle query failed") from error
    if result.returncode != 0 or result.stdout.strip() != b"throttled=0x0":
        raise HarnessError("Pi is throttled or throttle evidence differs")
    return "0x0"


def _host_snapshot(expected_board_serial: str) -> dict[str, object]:
    if os.geteuid() != 0 or _board_serial() != expected_board_serial:  # type: ignore[attr-defined]
        raise HarnessError("exact Pi/root identity gate failed")
    row = _findmnt(RECORDING_ROOT)
    if row is None:
        raise HarnessError("production recording root is not mounted")
    validate_mount_identity(
        row,
        source=EXPECTED_STORAGE_SOURCE,
        target=str(RECORDING_ROOT),
        filesystem="exfat",
        uuid=EXPECTED_STORAGE_UUID,
        label=EXPECTED_STORAGE_LABEL,
    )
    sentinel = _strict_json(_bounded_regular_bytes(SENTINEL_PATH, 8192), "storage sentinel")
    if sentinel.get("dashcam_uuid") != EXPECTED_STORAGE_UUID:
        raise HarnessError("storage sentinel UUID differs")
    return {
        "board_serial": expected_board_serial,
        "release": _release_snapshot(),
        "mount": row,
        "sentinel_sha256": _sha256(_bounded_regular_bytes(SENTINEL_PATH, 8192)),
        "catalog": {path.name: _file_fact(path) for path in PRODUCTION_CATALOG_MEMBERS},
        "network": _network_snapshot(),
        "throttle": _throttle_snapshot(),
        "mount_namespace": os.readlink("/proc/self/ns/mnt"),
        "loop_inventory": _loop_snapshot(),
    }


def _loop_snapshot() -> tuple[tuple[str, str, bool], ...]:
    result = _run((LOSSETUP, "--json", "--list", "--output", "NAME,BACK-FILE,AUTOCLEAR"))
    try:
        rows = json.loads(result.stdout.decode("utf-8"))["loopdevices"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise HarnessError("losetup inventory is malformed") from error
    if not isinstance(rows, list) or len(rows) > 64:
        raise HarnessError("loop inventory count is unsafe")
    normalized: list[tuple[str, str, bool]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            raise HarnessError("loop inventory row is malformed")
        name, backing, autoclear = raw.get("name"), raw.get("back-file"), raw.get("autoclear")
        if (
            not isinstance(name, str)
            or LOOP_RE.fullmatch(name) is None
            or not isinstance(backing, str)
            or not backing
            or not isinstance(autoclear, bool)
        ):
            raise HarnessError("loop inventory identity is unsafe")
        normalized.append((name, backing, autoclear))
    return tuple(sorted(normalized))


def _loop_backing_file(loop: Path) -> Path:
    metadata = loop.stat()
    if not stat.S_ISBLK(metadata.st_mode):
        raise HarnessError("loop target is not a block device")
    device = f"{os.major(metadata.st_rdev)}:{os.minor(metadata.st_rdev)}"  # type: ignore[attr-defined]
    if DEVICE_RE.fullmatch(device) is None:
        raise HarnessError("loop kernel device identity is malformed")
    path = Path("/sys/dev/block") / device / "loop/backing_file"
    payload = _bounded_virtual_bytes(path, 4096)
    value = payload.decode("utf-8").strip()
    if not value.startswith("/"):
        raise HarnessError("loop backing-file value is not absolute")
    return Path(value).resolve(strict=True)


def _attach_loop(image: Path) -> Path:
    result = _run((LOSSETUP, "--find", "--show", "--nooverlap", str(image)))
    loop = Path(result.stdout.decode("ascii").strip())
    validate_loop_identity(
        loop,
        image,
        stat_result=loop.stat(),
        backing_file=str(_loop_backing_file(loop)),
    )
    return loop


def _require_owned_loop(loop: Path, image: Path) -> None:
    image_metadata = image.stat()
    if not stat.S_ISREG(image_metadata.st_mode) or image_metadata.st_nlink != 1:
        raise HarnessError("disposable image identity is no longer a single regular file")
    validate_loop_identity(
        loop,
        image,
        stat_result=loop.stat(),
        backing_file=str(_loop_backing_file(loop)),
    )


def _blkid(loop: Path) -> dict[str, str]:
    result = _run((BLKID, "-o", "export", str(loop)))
    facts: dict[str, str] = {}
    for line in result.stdout.decode("ascii").splitlines():
        key, separator, value = line.partition("=")
        if not separator or key in facts or not key or not value:
            raise HarnessError("blkid output is malformed")
        facts[key] = value
    if facts.get("DEVNAME") != str(loop) or not {"UUID", "TYPE", "LABEL"} <= set(facts):
        raise HarnessError("blkid identity is incomplete")
    return facts


def _mount_loop(loop: Path, image: Path, target: Path, filesystem: str) -> dict[str, str]:
    _require_owned_loop(loop, image)
    options = "rw,nosuid,nodev,noexec,noatime"
    if filesystem == "exfat":
        options += ",uid=0,gid=0,fmask=0137,dmask=0027"
    elif filesystem == "ext4":
        options += ",nodiscard"
    else:
        raise HarnessError("disposable filesystem type differs")
    _run((MOUNT, "-t", filesystem, "-o", options, str(loop), str(target)))
    facts = _blkid(loop)
    row = _findmnt(target)
    if row is None:
        raise HarnessError("disposable mount is absent after mount")
    validate_mount_identity(
        row,
        source=str(loop),
        target=str(target),
        filesystem=filesystem,
        uuid=facts["UUID"],
        label=facts["LABEL"],
    )
    return facts


def _unmount_owned(target: Path, loop: Path, image: Path) -> None:
    row = _findmnt(target)
    backing = str(_loop_backing_file(loop))
    validate_cleanup_identity(
        expected_loop=str(loop),
        expected_image=image,
        mount_source=None if row is None else row["source"],
        backing_file=backing,
    )
    if row is not None:
        _run((UMOUNT, "--", str(target)))


def _detach_owned(loop: Path, image: Path, baseline: tuple[tuple[str, str, bool], ...]) -> None:
    if any(row[0] == str(loop) for row in baseline):
        raise HarnessError("refusing to detach a baseline loop device")
    validate_cleanup_identity(
        expected_loop=str(loop),
        expected_image=image,
        mount_source=None,
        backing_file=str(_loop_backing_file(loop)),
    )
    _run((LOSSETUP, "--detach", str(loop)))


def _device_id(path: Path) -> str:
    device = path.stat().st_dev
    return f"{os.major(device)}:{os.minor(device)}"  # type: ignore[attr-defined]


def _stat_space(path: Path) -> tuple[int, int]:
    value = os.statvfs(path)  # type: ignore[attr-defined]
    if value.f_blocks <= 0 or value.f_bavail < 0 or value.f_bavail > value.f_blocks:
        raise HarnessError("filesystem returned invalid space values")
    return value.f_blocks * value.f_frsize, value.f_bavail * value.f_frsize


def _filler_allocation_increment(
    *,
    free_bytes: int,
    start_bytes: int,
    emergency_bytes: int,
    allocation_unit_bytes: int,
    filler_size_bytes: int,
) -> int:
    values = (
        free_bytes,
        start_bytes,
        emergency_bytes,
        allocation_unit_bytes,
        filler_size_bytes,
    )
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
        raise HarnessError("filler allocation inputs are invalid")
    if (
        not emergency_bytes < start_bytes <= free_bytes
        or allocation_unit_bytes <= 0
        or filler_size_bytes > MAX_FILLER_BYTES
    ):
        raise HarnessError("filler allocation thresholds or bounds differ")
    guard = max(MIN_FILLER_EMERGENCY_GUARD_BYTES, allocation_unit_bytes * 2)
    target_free = ((start_bytes - 1) // allocation_unit_bytes) * allocation_unit_bytes
    if target_free <= emergency_bytes + guard:
        raise HarnessError("filler threshold band cannot preserve the emergency guard")
    required = free_bytes - target_free
    remaining = MAX_FILLER_BYTES - filler_size_bytes
    increment = min(required, remaining, MAX_FILLER_ALLOCATION_CHUNK_BYTES)
    increment -= increment % allocation_unit_bytes
    if increment <= 0 or free_bytes - increment <= emergency_bytes + guard:
        raise HarnessError("filler allocation cannot make safe bounded progress")
    return increment


def _validate_filler_allocation_observation(
    *,
    previous_free_bytes: int,
    free_bytes: int,
    requested_increment_bytes: int,
    allocation_unit_bytes: int,
    emergency_bytes: int,
) -> None:
    guard = max(MIN_FILLER_EMERGENCY_GUARD_BYTES, allocation_unit_bytes * 2)
    observed_drop = previous_free_bytes - free_bytes
    if (
        observed_drop <= 0
        or observed_drop > requested_increment_bytes + allocation_unit_bytes
        or free_bytes <= emergency_bytes + guard
    ):
        raise HarnessError("filler allocation made unsafe free-space progress")


def _load_commit_source(archive: Path, expected_members: Mapping[str, object]) -> dict[str, str]:
    for name in tuple(sys.modules):
        if name == "dashcam" or name.startswith("dashcam."):
            raise HarnessError("dashcam module was loaded before source verification")
    sys.path.insert(0, str(archive))
    import dashcam.catalog.database as database
    import dashcam.catalog.filesystem as filesystem
    import dashcam.catalog.models as models
    import dashcam.catalog.policy as policy
    import dashcam.config as config
    import dashcam.control.dispatcher as dispatcher
    import dashcam.control.socket_server as socket_server
    import dashcam.recorder.finalizer as finalizer
    import dashcam.state as state
    import dashcam.storage.reclaimer as reclaimer
    import dashcam.storage.retention as retention
    import dashcam.storage.space as space

    required = (
        database,
        filesystem,
        models,
        policy,
        config,
        dispatcher,
        socket_server,
        finalizer,
        reclaimer,
        retention,
        space,
        state,
    )
    if any(module.__name__ not in sys.modules for module in required):
        raise HarnessError("required commit-source module import is incomplete")
    provenance: dict[str, str] = {}
    expected_root = str(archive.resolve(strict=True)) + os.sep
    loaded = tuple(
        module
        for name, module in sys.modules.items()
        if (name == "dashcam" or name.startswith("dashcam.")) and module is not None
    )
    for module in loaded:
        origin = cast(str | None, getattr(module, "__file__", None))
        if origin is None or not origin.startswith(expected_root):
            raise HarnessError(f"module import escaped the verified archive: {module.__name__}")
        member = origin[len(expected_root) :].replace("\\", "/")
        if member not in expected_members:
            raise HarnessError("imported module lacks verified member provenance")
        provenance[module.__name__] = cast(dict[str, str], expected_members[member])["sha256"]
    return provenance


def _write_member(root: Path, relative: str, payload: bytes) -> None:
    path = root / PurePosixPath(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink() or len(payload) > 16 * 1024**2:
        raise HarnessError("fixture member differs from bounded fresh-file contract")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    try:
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _require_fully_allocated_image(image: Path, expected_size: int) -> None:
    descriptor = os.open(
        image,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        allocated = os.fstat(descriptor)
        if (
            not stat.S_ISREG(allocated.st_mode)
            or allocated.st_nlink != 1
            or allocated.st_size != expected_size
            or allocated.st_blocks * 512 < expected_size  # type: ignore[attr-defined]
        ):
            raise HarnessError("formatted loop backing image is not fully allocated")
    finally:
        os.close(descriptor)


def _fixture_clip(order: int, *, protected: bool = False, managed: bool = True) -> Any:
    from uuid import UUID

    from dashcam.catalog.models import CatalogClip
    from dashcam.state import ClipLifecycle

    clip_id = UUID(int=order + 1)
    directory = "protected" if protected else "clips"
    return CatalogClip(
        clip_id=clip_id,
        lifecycle=ClipLifecycle.FINALIZED,
        video_path=f"{directory}/clip-{order:04d}.mp4",
        sidecar_path=f"{directory}/clip-{order:04d}.json",
        start_monotonic_ns=order * 1_000_000_000,
        end_monotonic_ns=(order + 1) * 1_000_000_000,
        retention_order=order,
        size_bytes=1024 * 1024 + 3,
        protected=protected,
        protection_reason="fixture:event" if protected else None,
        pair_reconciled=True,
        managed=managed,
    )


def _materialize_clip(root: Path, clip: Any, *, video_bytes: int = 1024 * 1024) -> None:
    _write_member(root, cast(str, clip.video_path), b"M10\0" + b"v" * (video_bytes - 4))
    _write_member(root, cast(str, clip.sidecar_path), b'{"schema_version":1}\n')


def _crash_cell_coordinates(
    work: Path, operation: str, cutpoint: str
) -> tuple[str, int, Path]:
    if operation not in CRASH_OPERATIONS or cutpoint not in CRASH_CUTPOINTS:
        raise HarnessError("crash-cell operation or cutpoint differs")
    resolved_work = work.resolve(strict=True)
    if (
        resolved_work.parent != Path("/var/tmp")
        or re.fullmatch(r"dashcam-m10-retention-loop\.[A-Za-z0-9_-]{6,32}", resolved_work.name)
        is None
    ):
        raise HarnessError("crash-cell work identity differs")
    operation_index = CRASH_OPERATIONS.index(operation)
    cutpoint_index = CRASH_CUTPOINTS.index(cutpoint)
    order = CRASH_CELL_BASE_ORDER + operation_index * len(CRASH_CUTPOINTS) + cutpoint_index
    cell_id = f"{operation.lower()}-{cutpoint.lower().replace('_', '-')}"
    catalog = resolved_work / "catalog" / f"crash-{cell_id}.sqlite3"
    return cell_id, order, catalog


def _validate_crash_fixture_mount(
    target: Path,
    image: Path,
    *,
    expected_size: int,
    filesystem: str,
    label: str,
) -> None:
    image_metadata = image.stat()
    if not stat.S_ISREG(image_metadata.st_mode):
        raise HarnessError("crash-cell backing image is not regular")
    if image_metadata.st_nlink != 1:
        raise HarnessError("crash-cell backing image link count differs")
    if image_metadata.st_size != expected_size:
        raise HarnessError("crash-cell backing image size differs")
    if image_metadata.st_blocks * 512 < expected_size:  # type: ignore[attr-defined]
        raise HarnessError("crash-cell backing image is not fully allocated")
    row = _findmnt(target)
    if row is None or not isinstance(row.get("source"), str):
        raise HarnessError("crash-cell fixture mount is absent")
    loop = Path(row["source"])
    if LOOP_RE.fullmatch(loop.as_posix()) is None:
        raise HarnessError("crash-cell fixture is not loop-backed")
    _require_owned_loop(loop, image)
    facts = _blkid(loop)
    if facts["TYPE"] != filesystem or facts["LABEL"] != label:
        raise HarnessError("crash-cell filesystem identity differs")
    validate_mount_identity(
        row,
        source=str(loop),
        target=str(target),
        filesystem=filesystem,
        uuid=facts["UUID"],
        label=label,
    )


def _validate_crash_cell_environment(
    work: Path, operation: str, cutpoint: str
) -> tuple[Path, int, Path]:
    _cell_id, order, catalog = _crash_cell_coordinates(work, operation, cutpoint)
    resolved_work = work.resolve(strict=True)
    expected_python = (
        Path("/opt/dashcam/releases") / EXPECTED_RELEASE / "venv/bin/python"
    ).resolve(strict=True)
    work_metadata = resolved_work.stat()
    if (
        sys.platform != "linux"
        or os.geteuid() != 0
        or Path(sys.executable).resolve(strict=True) != expected_python
        or not stat.S_ISDIR(work_metadata.st_mode)
        or work_metadata.st_uid != 0
        or stat.S_IMODE(work_metadata.st_mode) != 0o700
        or work_metadata.st_dev != Path("/var/tmp").stat().st_dev
    ):
        raise HarnessError("crash-cell process or work ownership differs")
    catalog_mount = (resolved_work / "catalog").resolve(strict=True)
    _validate_crash_fixture_mount(
        RECORDING_ROOT.resolve(strict=True),
        resolved_work / "recording.exfat.img",
        expected_size=EXFAT_IMAGE_BYTES,
        filesystem="exfat",
        label="M10LOOP",
    )
    _validate_crash_fixture_mount(
        catalog_mount,
        resolved_work / "catalog.ext4.img",
        expected_size=EXT4_IMAGE_BYTES,
        filesystem="ext4",
        label="M10CAT",
    )
    if (
        catalog.parent != catalog_mount
        or not catalog.is_file()
        or catalog.is_symlink()
        or catalog.stat().st_dev != catalog_mount.stat().st_dev
    ):
        raise HarnessError("crash-cell catalog identity differs")
    return resolved_work, order, catalog


def _crash_finalizing_fixture(order: int) -> tuple[Any, Any, bytes]:
    from uuid import UUID

    from dashcam.catalog.models import CatalogClip
    from dashcam.metadata.schema import AudioSummary, ClipSidecar, GpsSummary, VideoSummary
    from dashcam.state import (
        ClipLifecycle,
        GpsTimeState,
        SystemClockState,
        TimestampQuality,
    )
    from dashcam.storage.intents import PairPaths
    from dashcam.storage.naming import finalized_unsynced_clip_pair, provisional_clip_pair

    source = provisional_clip_pair(boot_id="m10kill", sequence=order)
    target = finalized_unsynced_clip_pair(boot_id="m10kill", sequence=order)
    clip_id = UUID(int=order + 1)
    start = order * 1_000_000_000
    end = start + 1_000_000_000
    clip = CatalogClip(
        clip_id=clip_id,
        lifecycle=ClipLifecycle.FINALIZING,
        video_path=f"pending/{source.video_name}",
        sidecar_path=f"pending/{source.metadata_name}",
        start_monotonic_ns=start,
        end_monotonic_ns=end,
        retention_order=order,
        size_bytes=64 * 1024,
        protected=False,
        protection_reason=None,
        pair_reconciled=False,
        managed=True,
    )
    paths = PairPaths(
        clip.video_path,
        clip.sidecar_path,
        f"clips/{target.video_name}",
        f"clips/{target.metadata_name}",
    )
    sidecar = ClipSidecar(
        schema_version=1,
        clip_id=clip_id,
        boot_id=UUID(int=1),
        sequence=order,
        video_file=target.video_name,
        metadata_file=target.metadata_name,
        start_utc=None,
        end_utc=None,
        start_monotonic_ns=start,
        end_monotonic_ns=end,
        gps_time_state=GpsTimeState.UNSYNCED,
        system_clock_state=SystemClockState.UNSET,
        timestamp_quality=TimestampQuality.MONOTONIC_ONLY,
        time_anchor=None,
        timezone="UTC",
        start_local=None,
        video=VideoSummary("h264", 1920, 1080, 30.0, 8_000_000, 8_000_000, 30, 0),
        audio=AudioSummary(False, None, None, None, None),
        gps=GpsSummary(False, None),
        protected=False,
        protection_reason=None,
        software_version="m10-loop",
    )
    return clip, paths, sidecar.to_canonical_json()


def _crash_fixture(operation: str, order: int) -> tuple[Any, Any | None, bytes | None]:
    if operation == "FINALIZE":
        return _crash_finalizing_fixture(order)
    return _fixture_clip(order, protected=operation == "UNPROTECT"), None, None


class _CrashAfterActionFilesystem:
    def __init__(self, filesystem: Any, *, kill_after: int) -> None:
        if kill_after not in {1, 2}:
            raise HarnessError("crash member cutpoint differs")
        self._filesystem = filesystem
        self._kill_after = kill_after
        self._actions = 0

    def exists(self, relative_path: str) -> bool:
        return cast(bool, self._filesystem.exists(relative_path))

    def read_bytes(self, relative_path: str, *, maximum_bytes: int) -> bytes:
        return cast(
            bytes,
            self._filesystem.read_bytes(relative_path, maximum_bytes=maximum_bytes),
        )

    def move(self, source: str, target: str) -> None:
        self._filesystem.move(source, target)
        self._after_durable_action()

    def unlink(self, relative_path: str) -> None:
        self._filesystem.unlink(relative_path)
        self._after_durable_action()

    def _after_durable_action(self) -> None:
        self._actions += 1
        if self._actions == self._kill_after:
            os.kill(os.getpid(), SIGKILL_NUMBER)
            os._exit(125)


def _prepare_crash_intent(
    catalog_path: Path,
    root: Path,
    *,
    operation: str,
    cutpoint: str,
    order: int,
) -> None:
    from dashcam.catalog.database import ClipCatalog
    from dashcam.catalog.filesystem import RootedFilesystem

    clip, paths, _sidecar = _crash_fixture(operation, order)
    with ClipCatalog(catalog_path) as catalog:
        intent_id: Any
        if operation == "FINALIZE":
            assert paths is not None
            intent_id = catalog.register_finalizing_clip(
                clip,
                promotion_paths=paths,
                monotonic_now_ns=80_000 + order,
            )
        elif operation == "PROTECT":
            intent_id = catalog.prepare_protect(
                clip.clip_id,
                reason="m10:sigkill",
                monotonic_now_ns=80_000 + order,
            )
        elif operation == "UNPROTECT":
            intent_id = catalog.prepare_unprotect(
                clip.clip_id,
                monotonic_now_ns=80_000 + order,
            )
        elif operation == "DELETE":
            intent_id = catalog.prepare_delete(
                clip.clip_id,
                monotonic_now_ns=80_000 + order,
                boot_id="m10-sigkill",
            )
        else:
            raise HarnessError("crash-cell operation differs")
        if intent_id is None:
            raise HarnessError("crash-cell intent was not durably prepared")
        _write_all(sys.stdout.fileno(), f"{intent_id}\n".encode("ascii"))
        if cutpoint == "AFTER_INTENT":
            os.kill(os.getpid(), SIGKILL_NUMBER)
            os._exit(125)
        filesystem: Any = RootedFilesystem(root, expected_device_id=_device_id(root))
        if cutpoint in {"AFTER_MEMBER1", "AFTER_MEMBER2"}:
            filesystem = _CrashAfterActionFilesystem(
                filesystem,
                kill_after=1 if cutpoint == "AFTER_MEMBER1" else 2,
            )
        result = catalog.reconcile_intent(
            intent_id,
            filesystem,
            monotonic_now_ns=90_000 + order,
            max_actions=2,
        )
        if not result.complete or result.problems:
            raise HarnessError("crash-cell reconciliation did not complete")
        if cutpoint != "AFTER_COMPLETE":
            raise HarnessError("crash-cell member cutpoint did not terminate")
        os.kill(os.getpid(), SIGKILL_NUMBER)
        os._exit(125)
    raise HarnessError("crash-cell unexpectedly survived its completion cutpoint")


def _crash_cell(arguments: argparse.Namespace) -> int:
    work, order, catalog = _validate_crash_cell_environment(
        Path(cast(str, arguments.work)),
        cast(str, arguments.cell_operation),
        cast(str, arguments.cell_cutpoint),
    )
    metadata = verify_bundle(
        Path(arguments.bundle).resolve(strict=True),
        arguments.expected_manifest_sha256,
        arguments.expected_commit,
    )
    _load_commit_source(
        Path(arguments.bundle).resolve(strict=True) / "dashcam-source.zip",
        cast(dict[str, object], metadata["members"]),
    )
    _prepare_crash_intent(
        catalog,
        RECORDING_ROOT.resolve(strict=True),
        operation=arguments.cell_operation,
        cutpoint=arguments.cell_cutpoint,
        order=order,
    )
    raise HarnessError(f"crash-cell unexpectedly returned: {work.name}")


def _run_crash_subprocess(
    *,
    bundle: Path,
    work: Path,
    expected_manifest_sha256: str,
    expected_commit: str,
    operation: str,
    cutpoint: str,
) -> Any:
    from uuid import UUID

    _crash_cell_coordinates(work, operation, cutpoint)
    command = (
        sys.executable,
        "-I",
        str(bundle / "run.py"),
        "--crash-cell",
        "--bundle",
        str(bundle),
        "--work",
        str(work),
        "--expected-manifest-sha256",
        expected_manifest_sha256,
        "--expected-commit",
        expected_commit,
        "--cell-operation",
        operation,
        "--cell-cutpoint",
        cutpoint,
    )
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
    )
    try:
        try:
            returncode = process.wait(timeout=CRASH_CELL_TIMEOUT_S)
        except subprocess.TimeoutExpired as error:
            process.kill()
            process.wait(timeout=5)
            raise HarnessError("crash-cell exceeded its bounded timeout") from error
        assert process.stdout is not None
        assert process.stderr is not None
        stdout = process.stdout.read(513)
        stderr = process.stderr.read(513)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
    failed_mask = "".join(
        "1" if failed else "0"
        for failed in (
            returncode != -SIGKILL_NUMBER,
            len(stdout) > 512,
            bool(stderr),
            CRASH_INTENT_LINE_RE.fullmatch(stdout) is None,
        )
    )
    if failed_mask != "0000":
        raise CrashCellContractError(
            operation=operation,
            cutpoint=cutpoint,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            failed_mask=failed_mask,
        )
    return UUID(stdout.decode("ascii").strip())


def _matrix_a(
    catalog_path: Path, volume_uuid: str, capacity: int, device_id: str
) -> dict[str, object]:
    from dashcam.catalog.database import ClipCatalog
    from dashcam.catalog.policy import StorageThresholdController
    from dashcam.storage.retention import ResolvedThresholds, RetentionMode, StorageThresholds
    from dashcam.storage.space import FilesystemSpaceObservation, StorageSpaceMonitor

    configured = StorageThresholds(15, 20, 64 * 1024**2, 32 * 1024**2)
    resolved: ResolvedThresholds = configured.resolve(capacity)
    emergency = resolved.emergency_below_bytes
    start = resolved.start_deletion_below_bytes
    high = resolved.stop_deletion_at_bytes
    controller = StorageThresholdController(resolved)
    rows: list[dict[str, object]] = []
    for name, free, no_space in (
        ("start_equal", start, False),
        ("start_minus_one", start - 1, False),
        ("high_minus_one", high - 1, False),
        ("high_equal", high, False),
        ("emergency_equal", emergency, False),
        ("emergency_minus_one", emergency - 1, False),
        ("no_space_write", high, True),
    ):
        mode = controller.evaluate(free_bytes=free, no_space_write=no_space)
        rows.append(
            {"name": name, "mode": mode.value, "reclaim_latched": mode is not RetentionMode.NORMAL}
        )
        if name == "high_equal":
            controller.mode = RetentionMode.RECLAIMING
    with ClipCatalog(catalog_path) as catalog:
        from dashcam.catalog.database import RetentionThresholdLatch

        catalog.store_retention_threshold_latch(
            RetentionThresholdLatch(volume_uuid, capacity, True)
        )
    with ClipCatalog(catalog_path) as reopened:
        observations = iter(
            (
                FilesystemSpaceObservation(device_id, capacity, high - 1),
                FilesystemSpaceObservation(device_id, capacity, high),
                FilesystemSpaceObservation("999:999", capacity, high),
            )
        )
        monitor = StorageSpaceMonitor(
            volume_uuid=volume_uuid,
            expected_device_id=device_id,
            expected_capacity_bytes=capacity,
            thresholds=configured,
            observer=lambda: next(observations),
            latch_store=reopened,
            reclaimer_available=True,
        )
        restart_below = monitor.observe()
        if restart_below.mode is None:
            raise HarnessError("restart monitor did not publish a mode")
        below_latch = reopened.retention_threshold_latch()
        if below_latch is None:
            raise HarnessError("durable restart latch disappeared")
        rows.append(
            {
                "name": "restart_below_high",
                "mode": restart_below.mode.value,
                "reclaim_latched": below_latch.reclaim_latched,
            }
        )
        restart_high = monitor.observe()
        if restart_high.mode is None:
            raise HarnessError("restart monitor did not publish a high-water mode")
        high_latch = reopened.retention_threshold_latch()
        if high_latch is None:
            raise HarnessError("durable restart latch disappeared at high water")
        rows.append(
            {
                "name": "restart_high_equal",
                "mode": restart_high.mode.value,
                "reclaim_latched": high_latch.reclaim_latched,
            }
        )
        drift = monitor.observe()
    capacity_catalog_path = catalog_path.with_name("capacity.sqlite3")
    with ClipCatalog(capacity_catalog_path) as capacity_catalog:
        capacity_monitor = StorageSpaceMonitor(
            volume_uuid=volume_uuid,
            expected_device_id=device_id,
            expected_capacity_bytes=capacity,
            thresholds=configured,
            observer=lambda: FilesystemSpaceObservation(device_id, capacity + 4096, high),
            latch_store=capacity_catalog,
            reclaimer_available=True,
        )
        capacity_drift = capacity_monitor.observe()
    invalid_catalog_path = catalog_path.with_name("invalid.sqlite3")
    with ClipCatalog(invalid_catalog_path) as invalid_catalog:
        invalid_monitor = StorageSpaceMonitor(
            volume_uuid=volume_uuid,
            expected_device_id=device_id,
            expected_capacity_bytes=capacity,
            thresholds=configured,
            observer=lambda: cast(FilesystemSpaceObservation, None),
            latch_store=invalid_catalog,
            maximum_observation_failures=3,
            reclaimer_available=True,
        )
        invalid = [invalid_monitor.observe() for _ in range(3)]
    failure_catalog_path = catalog_path.with_name("failure.sqlite3")

    def failing_observer() -> FilesystemSpaceObservation:
        raise OSError("bounded fixture observation failure")

    with ClipCatalog(failure_catalog_path) as failure_catalog:
        failure_monitor = StorageSpaceMonitor(
            volume_uuid=volume_uuid,
            expected_device_id=device_id,
            expected_capacity_bytes=capacity,
            thresholds=configured,
            observer=failing_observer,
            latch_store=failure_catalog,
            maximum_observation_failures=3,
            reclaimer_available=True,
        )
        failures = [failure_monitor.observe() for _ in range(3)]
    validate_threshold_evidence(rows)
    if (
        restart_below.stale
        or drift.fault is None
        or drift.fault.value != "IDENTITY_DRIFT"
        or capacity_drift.fault is None
        or capacity_drift.fault.value != "CAPACITY_DRIFT"
        or invalid[0].fault is None
        or invalid[0].fault.value != "INVALID_OBSERVATION"
        or invalid[-1].fault is None
        or invalid[-1].fault.value != "OBSERVATION_STALE"
        or not invalid[-1].stop_required
        or failures[0].fault is None
        or failures[0].fault.value != "OBSERVATION_FAILED"
        or failures[-1].fault is None
        or failures[-1].fault.value != "OBSERVATION_STALE"
        or not failures[-1].stop_required
    ):
        raise HarnessError("fresh, drift, or invalid-observation containment differs")
    return {
        "passed": True,
        "cases": rows,
        "identity_drift_refused": True,
        "capacity_drift_refused": True,
        "invalid_observation_bounded": True,
        "observation_failure_bounded": True,
    }


def _matrix_b_c(root: Path, catalog_path: Path) -> tuple[dict[str, object], dict[str, object]]:
    from uuid import UUID

    from dashcam.catalog.database import ClipCatalog
    from dashcam.catalog.filesystem import RootedFilesystem
    from dashcam.catalog.models import CatalogClip
    from dashcam.state import ClipLifecycle
    from dashcam.storage.intents import PairPaths
    from dashcam.storage.naming import finalized_unsynced_clip_pair, provisional_clip_pair
    from dashcam.storage.reclaimer import StorageReclaimer
    from dashcam.storage.retention import StorageThresholds

    filesystem = RootedFilesystem(root)
    unknown = {
        "System Volume Information/IndexerVolumeGuid": b"windows-metadata\n",
        "$RECYCLE.BIN/desktop.ini": b"[.ShellClassInfo]\r\n",
        "clips/UNTRACKED.TXT": b"unknown\n",
    }
    for path, payload in unknown.items():
        _write_member(root, path, payload)
    unknown_before = {name: _sha256(payload) for name, payload in unknown.items()}
    eligible: list[Any] = []
    with ClipCatalog(catalog_path) as catalog:
        for order in range(36):
            clip = _fixture_clip(order)
            _materialize_clip(root, clip, video_bytes=8 * 1024**2)
            catalog.register_clip(clip, catalog_now_ns=order)
            eligible.append(clip)
        protected = _fixture_clip(40, protected=True)
        _materialize_clip(root, protected)
        catalog.register_clip(protected, catalog_now_ns=40)
        leased = _fixture_clip(41)
        _materialize_clip(root, leased)
        catalog.register_clip(leased, catalog_now_ns=41)
        catalog.acquire_download_lease(
            leased.clip_id,
            holder="m10-loop-download",
            monotonic_now_ns=100,
            duration_ns=300 * 1_000_000_000,
            boot_id="m10-loop-boot",
        )
        unmanaged = _fixture_clip(42, managed=False)
        _materialize_clip(root, unmanaged)
        catalog.register_clip(unmanaged, catalog_now_ns=42)
        mutating = _fixture_clip(43)
        _materialize_clip(root, mutating)
        catalog.register_clip(mutating, catalog_now_ns=43)
        if (
            catalog.prepare_protect(
                mutating.clip_id, reason="fixture:pending-protect", monotonic_now_ns=44
            )
            is None
        ):
            raise HarnessError("pending mutation fixture was not created")
        pending_pair = provisional_clip_pair(boot_id="m10loop", sequence=44)
        final_pair = finalized_unsynced_clip_pair(boot_id="m10loop", sequence=44)
        finalizing = CatalogClip(
            clip_id=UUID(int=45),
            lifecycle=ClipLifecycle.FINALIZING,
            video_path=f"pending/{pending_pair.video_name}",
            sidecar_path=f"pending/{pending_pair.metadata_name}",
            start_monotonic_ns=44 * 1_000_000_000,
            end_monotonic_ns=45 * 1_000_000_000,
            retention_order=44,
            size_bytes=1024 * 1024,
            protected=False,
            protection_reason=None,
            pair_reconciled=False,
            managed=True,
        )
        _materialize_clip(root, finalizing)
        catalog.register_finalizing_clip(
            finalizing,
            promotion_paths=PairPaths(
                finalizing.video_path,
                finalizing.sidecar_path,
                f"clips/{final_pair.video_name}",
                f"clips/{final_pair.metadata_name}",
            ),
            monotonic_now_ns=45,
        )

        reclaimer = StorageReclaimer(
            catalog=catalog,
            filesystem=filesystem,
            monotonic_ns=lambda: 1_000,
        )
        deleted: list[str] = []
        observations = 0
        capacity, free = _stat_space(root)
        thresholds = StorageThresholds(15, 20, 64 * 1024**2, 32 * 1024**2).resolve(capacity)
        start = thresholds.start_deletion_below_bytes
        high = thresholds.stop_deletion_at_bytes
        emergency = thresholds.emergency_below_bytes
        space_facts = os.statvfs(root)  # type: ignore[attr-defined]
        allocation_unit = max(
            space_facts.f_bsize,
            space_facts.f_frsize,
            root.stat().st_blksize,  # type: ignore[attr-defined]
        )
        filler = root / "windows-camera-roll.bin"
        filler_size = 0
        filler_steps = 0
        cycle_counts: list[int] = []
        filler_descriptor = os.open(
            filler,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o640,
        )
        try:
            for _cycle in range(3):
                while free >= start:
                    if filler_steps >= MAX_FILLER_ALLOCATION_STEPS:
                        raise HarnessError("filler allocation step bound reached")
                    increment = _filler_allocation_increment(
                        free_bytes=free,
                        start_bytes=start,
                        emergency_bytes=emergency,
                        allocation_unit_bytes=allocation_unit,
                        filler_size_bytes=filler_size,
                    )
                    previous_free = free
                    os.posix_fallocate(  # type: ignore[attr-defined]
                        filler_descriptor, filler_size, increment
                    )
                    filler_size += increment
                    filler_steps += 1
                    allocated = os.fstat(filler_descriptor)
                    if (
                        allocated.st_size != filler_size
                        or allocated.st_nlink != 1
                        or allocated.st_blocks * 512 < filler_size  # type: ignore[attr-defined]
                    ):
                        raise HarnessError("filler extent is sparse or has invalid identity")
                    capacity_now, free = _stat_space(root)
                    if capacity_now != capacity:
                        raise HarnessError("fixture capacity drifted while filling")
                    _validate_filler_allocation_observation(
                        previous_free_bytes=previous_free,
                        free_bytes=free,
                        requested_increment_bytes=increment,
                        allocation_unit_bytes=allocation_unit,
                        emergency_bytes=emergency,
                    )
                os.fsync(filler_descriptor)
                cycle_deleted = 0
                while True:
                    capacity_now, free = _stat_space(root)
                    observations += 1
                    if capacity_now != capacity:
                        raise HarnessError("fixture capacity drifted during reclamation")
                    if free >= high:
                        break
                    if cycle_deleted >= MAX_RECLAIM_STEPS or len(deleted) >= MAX_RECLAIM_STEPS:
                        raise HarnessError("reclamation step bound reached")
                    step = reclaimer.run_one(boot_id="m10-loop-boot", allow_new=True)
                    if not step.deleted or step.clip_id is None or step.actions_attempted > 2:
                        raise HarnessError("one-pair reclamation step differs")
                    deleted.append(str(step.clip_id))
                    cycle_deleted += 1
                if cycle_deleted == 0:
                    raise HarnessError("repeated low/high cycle made no bounded progress")
                cycle_counts.append(cycle_deleted)
        finally:
            os.close(filler_descriptor)
        expected = [str(clip.clip_id) for clip in eligible[: len(deleted)]]
        if deleted != expected:
            raise HarnessError("reclamation was not exact oldest-first")
        for clip in eligible[: len(deleted)]:
            if (root / clip.video_path).exists() or (root / clip.sidecar_path).exists():
                raise HarnessError("deleted pair still exists")
        for clip in eligible[len(deleted) :]:
            if not (root / clip.video_path).is_file() or not (root / clip.sidecar_path).is_file():
                raise HarnessError("reclamation passed the high-water stop")
        for clip in (protected, leased, unmanaged, mutating):
            if not (root / clip.video_path).is_file() or not (root / clip.sidecar_path).is_file():
                raise HarnessError("excluded pair was mutated")
        if (
            not (root / finalizing.video_path).is_file()
            or not (root / finalizing.sidecar_path).is_file()
        ):
            raise HarnessError("active finalization pair was mutated")
    unknown_after = {
        name: _sha256(_bounded_regular_bytes(root / PurePosixPath(name), 8192)) for name in unknown
    }
    if unknown_before != unknown_after:
        raise HarnessError("unknown or Windows metadata was mutated")
    return (
        {
            "passed": True,
            "oldest_first_pair_count": len(deleted),
            "fresh_observations": observations,
            "one_pair_per_observation": observations == len(deleted) + len(cycle_counts),
            "cycle_delete_counts": cycle_counts,
            "repeated_cycle_count": len(cycle_counts),
            "high_water_stop_bounded": free >= high,
            "filler_bytes": filler_size,
            "filler_allocation_steps": filler_steps,
        },
        {
            "passed": True,
            "protected_excluded": True,
            "active_lease_excluded": True,
            "unmanaged_excluded": True,
            "pending_mutation_excluded": True,
            "finalizing_pair_excluded": True,
            "unknown_files_unchanged": unknown_before == unknown_after,
        },
    )


def _matrix_d(root: Path, catalog_path: Path) -> dict[str, object]:
    from dashcam.catalog.database import ClipCatalog
    from dashcam.catalog.filesystem import RootedFilesystem
    from dashcam.catalog.models import EventSource
    from dashcam.storage.intents import IntentKind

    filesystem = RootedFilesystem(root)
    with ClipCatalog(catalog_path) as catalog:
        clips = []
        for order in range(100, 104):
            clip = _fixture_clip(order)
            _materialize_clip(root, clip, video_bytes=64 * 1024)
            catalog.register_clip(clip, catalog_now_ns=order)
            clips.append(clip)
        event = catalog.trigger_event(
            clips[2].clip_id,
            source=EventSource.API,
            monotonic_now_ns=50_000,
            previous_count=2,
            next_count=1,
        )
        for intent_id in event.queued_intent_ids:
            result = catalog.reconcile_intent(
                intent_id, filesystem, monotonic_now_ns=50_001, max_actions=2
            )
            if not result.complete or result.problems:
                raise HarnessError("event protection intent did not converge")
        consumed = catalog.finalize_clip(
            clips[3].clip_id,
            end_monotonic_ns=105 * 1_000_000_000,
            size_bytes=64 * 1024,
            monotonic_now_ns=50_002,
        )
        pending = catalog.list_pending_intents(limit=16)
        if (
            len(consumed) != 1
            or tuple(intent.intent_id for intent in pending) != consumed
            or pending[0].clip_id != clips[3].clip_id
            or pending[0].kind is not IntentKind.PROTECT
        ):
            raise HarnessError("next-clip event intent selection differs")
        for intent in pending:
            result = catalog.reconcile_intent(
                intent.intent_id, filesystem, monotonic_now_ns=50_003, max_actions=2
            )
            if not result.complete or result.problems:
                raise HarnessError("next-clip protection intent did not converge")
        protected_ids = tuple(str(value) for value in event.protected_clip_ids)
        expected = tuple(str(clip.clip_id) for clip in clips[:3])
        if protected_ids != expected:
            raise HarnessError("event previous2/current/next1 selection differs")
        for clip in clips:
            stored = catalog.get_clip(clip.clip_id)
            if not stored.protected or not stored.video_path.startswith("protected/"):
                raise HarnessError("event target was not durably protected")
    return {
        "passed": True,
        "previous_count": 2,
        "current_count": 1,
        "next_count": 1,
        "catalog_and_pair_intents_converged": True,
        "production_active_clip_callback_tested": False,
    }


def _crash_expected_targets(operation: str, clip: Any, paths: Any | None) -> tuple[str, str] | None:
    if operation == "DELETE":
        return None
    if operation == "FINALIZE":
        assert paths is not None
        return cast(str, paths.video_target), cast(str, paths.sidecar_target)
    directory = "protected" if operation == "PROTECT" else "clips"
    return (
        f"{directory}/{PurePosixPath(clip.video_path).name}",
        f"{directory}/{PurePosixPath(clip.sidecar_path).name}",
    )


def _assert_crash_cutpoint_files(
    root: Path,
    *,
    operation: str,
    cutpoint: str,
    clip: Any,
    paths: Any | None,
) -> None:
    targets = _crash_expected_targets(operation, clip, paths)
    expected_sources = {
        "AFTER_INTENT": (True, True),
        "AFTER_MEMBER1": (False, True),
        "AFTER_MEMBER2": (False, False),
        "AFTER_COMPLETE": (False, False),
    }[cutpoint]
    observed_sources = (
        (root / clip.video_path).is_file(),
        (root / clip.sidecar_path).is_file(),
    )
    if observed_sources != expected_sources:
        raise HarnessError("SIGKILL source-member cutpoint state differs")
    if targets is None:
        return
    expected_targets = {
        "AFTER_INTENT": (False, False),
        "AFTER_MEMBER1": (True, False),
        "AFTER_MEMBER2": (True, True),
        "AFTER_COMPLETE": (True, True),
    }[cutpoint]
    observed_targets = ((root / targets[0]).is_file(), (root / targets[1]).is_file())
    if observed_targets != expected_targets:
        raise HarnessError("SIGKILL target-member cutpoint state differs")


def _matrix_e(
    root: Path,
    catalog_mount: Path,
    *,
    bundle: Path,
    work: Path,
    expected_manifest_sha256: str,
    expected_commit: str,
) -> dict[str, object]:
    from dashcam.catalog.database import ClipCatalog
    from dashcam.catalog.filesystem import RootedFilesystem
    from dashcam.state import ClipLifecycle

    filesystem = RootedFilesystem(root, expected_device_id=_device_id(root))
    cells: list[dict[str, object]] = []
    for operation in CRASH_OPERATIONS:
        for cutpoint in CRASH_CUTPOINTS:
            _cell_id, order, catalog_path = _crash_cell_coordinates(work, operation, cutpoint)
            if catalog_path.parent != catalog_mount.resolve(strict=True) or catalog_path.exists():
                raise HarnessError("crash-cell catalog is not a fresh exact target")
            clip, paths, sidecar = _crash_fixture(operation, order)
            if operation == "FINALIZE":
                assert paths is not None and sidecar is not None
                _write_member(root, clip.video_path, b"M10-SIGKILL-VIDEO\n")
                _write_member(root, clip.sidecar_path, sidecar)
                with ClipCatalog(catalog_path):
                    pass
            else:
                _materialize_clip(root, clip, video_bytes=64 * 1024)
                with ClipCatalog(catalog_path) as catalog:
                    catalog.register_clip(clip, catalog_now_ns=70_000 + order)
            _run((SYNC, "-f", str(root)), timeout=30)
            _run((SYNC, "-f", str(catalog_mount)), timeout=30)
            intent_id = _run_crash_subprocess(
                bundle=bundle,
                work=work,
                expected_manifest_sha256=expected_manifest_sha256,
                expected_commit=expected_commit,
                operation=operation,
                cutpoint=cutpoint,
            )
            _assert_crash_cutpoint_files(
                root,
                operation=operation,
                cutpoint=cutpoint,
                clip=clip,
                paths=paths,
            )
            expected_actions = {
                "AFTER_INTENT": 2,
                "AFTER_MEMBER1": 1,
                "AFTER_MEMBER2": 0,
                "AFTER_COMPLETE": 0,
            }[cutpoint]
            with ClipCatalog(catalog_path) as reopened:
                pending_before = reopened.list_pending_intents(limit=2)
                if cutpoint == "AFTER_COMPLETE":
                    if pending_before:
                        raise HarnessError("completed SIGKILL cell retained a pending intent")
                elif (
                    len(pending_before) != 1
                    or pending_before[0].intent_id != intent_id
                    or pending_before[0].clip_id != clip.clip_id
                    or pending_before[0].kind.value != operation
                ):
                    raise HarnessError("SIGKILL cell did not retain its exact durable intent")
                first = reopened.reconcile_intent(
                    intent_id,
                    filesystem,
                    monotonic_now_ns=100_000 + order,
                    max_actions=2,
                )
                second = reopened.reconcile_intent(
                    intent_id,
                    filesystem,
                    monotonic_now_ns=110_000 + order,
                    max_actions=2,
                )
                stored = reopened.get_clip(clip.clip_id)
                pending = reopened.list_pending_intents(limit=2)
            if (
                first.intent.intent_id != intent_id
                or first.intent.clip_id != clip.clip_id
                or first.intent.kind.value != operation
                or not first.complete
                or first.problems
                or first.actions_attempted != expected_actions
                or second.intent.intent_id != intent_id
                or not second.complete
                or second.problems
                or second.actions_attempted != 0
                or pending
            ):
                raise HarnessError("SIGKILL cell did not replay and converge idempotently")
            if operation == "DELETE":
                if (
                    stored.lifecycle is not ClipLifecycle.DELETED
                    or not stored.pair_reconciled
                    or (root / clip.video_path).exists()
                    or (root / clip.sidecar_path).exists()
                ):
                    raise HarnessError("SIGKILL DELETE final state differs")
            else:
                assert paths is not None or operation in {"PROTECT", "UNPROTECT"}
                expected_directory = "protected" if operation == "PROTECT" else "clips"
                if operation == "FINALIZE":
                    assert paths is not None
                    expected_video = cast(str, paths.video_target)
                    expected_sidecar = cast(str, paths.sidecar_target)
                    expected_protected = False
                else:
                    expected_video = f"{expected_directory}/{PurePosixPath(clip.video_path).name}"
                    expected_sidecar = (
                        f"{expected_directory}/{PurePosixPath(clip.sidecar_path).name}"
                    )
                    expected_protected = operation == "PROTECT"
                if (
                    stored.lifecycle is not ClipLifecycle.FINALIZED
                    or stored.protected is not expected_protected
                    or not stored.pair_reconciled
                    or stored.video_path != expected_video
                    or stored.sidecar_path != expected_sidecar
                    or not (root / expected_video).is_file()
                    or not (root / expected_sidecar).is_file()
                    or (expected_video != clip.video_path and (root / clip.video_path).exists())
                    or (
                        expected_sidecar != clip.sidecar_path
                        and (root / clip.sidecar_path).exists()
                    )
                ):
                    raise HarnessError(f"SIGKILL {operation} final state differs")
            cells.append(
                {
                    "operation": operation,
                    "cutpoint": cutpoint,
                    "sigkill_observed": True,
                    "reopen_reconciled": True,
                    "idempotent_reconcile": True,
                    "recovery_actions": expected_actions,
                }
            )
    if len(cells) != CRASH_CELL_COUNT:
        raise HarnessError("SIGKILL matrix cell count differs")
    return {
        "passed": True,
        "operations": len(CRASH_OPERATIONS),
        "cutpoints_per_operation": len(CRASH_CUTPOINTS),
        "cell_count": len(cells),
        "actual_sigkill_cells": len(cells),
        "fresh_catalogs": len(cells),
        "cells": cells,
        "sigkill_cutpoint_matrix_tested": True,
        "physical_power_loss_tested": False,
    }


def _control_runtime_directory(work: Path) -> Path:
    resolved = work.resolve(strict=True)
    match = re.fullmatch(
        r"dashcam-m10-retention-loop\.([A-Za-z0-9_-]{6,32})", resolved.name
    )
    if resolved.parent != Path("/var/tmp") or match is None:
        raise HarnessError("control fixture work identity differs")
    return Path("/run") / f"{CONTROL_RUNTIME_PREFIX}{match.group(1)}"


def _dashcam_api_group_id() -> int:
    import grp

    try:
        getgrnam = cast(Any, grp.getgrnam)  # type: ignore[attr-defined]
        group_id = int(getgrnam("dashcam-api").gr_gid)
    except (KeyError, OSError) as error:
        raise HarnessError("dashcam-api group is unavailable") from error
    if group_id < 0:
        raise HarnessError("dashcam-api group identity differs")
    return group_id


def _cleanup_control_runtime_directory(work: Path, *, group_id: int) -> None:
    directory = _control_runtime_directory(work)
    try:
        directory_info = os.lstat(directory)
    except FileNotFoundError:
        return
    if (
        not stat.S_ISDIR(directory_info.st_mode)
        or stat.S_ISLNK(directory_info.st_mode)
        or directory_info.st_uid != 0
        or (
            (directory_info.st_gid, stat.S_IMODE(directory_info.st_mode))
            not in {(0, 0o700), (group_id, 0o700), (group_id, 0o750)}
        )
    ):
        raise HarnessError("control runtime cleanup directory identity differs")
    entries = tuple(directory.iterdir())
    if len(entries) > 1:
        raise HarnessError("control runtime cleanup contains unexpected members")
    if entries:
        leaf = entries[0]
        leaf_info = os.lstat(leaf)
        if (
            leaf.name != CONTROL_SOCKET_NAME
            or not stat.S_ISSOCK(leaf_info.st_mode)
            or leaf_info.st_uid != 0
            or leaf_info.st_nlink != 1
            or leaf_info.st_dev != directory_info.st_dev
        ):
            raise HarnessError("control runtime cleanup socket identity differs")
        os.unlink(leaf)
    os.rmdir(directory)


def _writing_fixture(order: int) -> Any:
    from dashcam.catalog.models import CatalogClip
    from dashcam.state import ClipLifecycle
    from dashcam.storage.naming import provisional_clip_pair

    pair = provisional_clip_pair(boot_id="m10control", sequence=order)
    return CatalogClip(
        clip_id=UUID(int=order + 1),
        lifecycle=ClipLifecycle.WRITING,
        video_path=f"pending/{pair.video_name}",
        sidecar_path=f"pending/{pair.metadata_name}",
        start_monotonic_ns=order * 1_000_000_000,
        end_monotonic_ns=None,
        retention_order=order,
        size_bytes=0,
        protected=False,
        protection_reason=None,
        pair_reconciled=False,
        managed=True,
    )


def _canonical_control_sidecar(
    clip: Any, *, target_video_name: str, target_sidecar_name: str
) -> bytes:
    from dashcam.metadata.schema import AudioSummary, ClipSidecar, GpsSummary, VideoSummary
    from dashcam.state import GpsTimeState, SystemClockState, TimestampQuality

    if not clip.protected or clip.protection_reason is None:
        raise HarnessError("control finalization sidecar lacks durable protection")
    return ClipSidecar(
        schema_version=1,
        clip_id=clip.clip_id,
        boot_id=UUID(int=2),
        sequence=clip.retention_order,
        video_file=target_video_name,
        metadata_file=target_sidecar_name,
        start_utc=None,
        end_utc=None,
        start_monotonic_ns=clip.start_monotonic_ns,
        end_monotonic_ns=clip.end_monotonic_ns,
        gps_time_state=GpsTimeState.UNSYNCED,
        system_clock_state=SystemClockState.UNSET,
        timestamp_quality=TimestampQuality.MONOTONIC_ONLY,
        time_anchor=None,
        timezone="UTC",
        start_local=None,
        video=VideoSummary("h264", 1920, 1080, 30.0, 8_000_000, 8_000_000, 30, 0),
        audio=AudioSummary(False, None, None, None, None),
        gps=GpsSummary(False, None),
        protected=True,
        protection_reason=clip.protection_reason,
        software_version="m10-control-loop",
    ).to_canonical_json()


def _validate_control_socket(path: Path, *, group_id: int) -> None:
    info = os.lstat(path)
    if (
        not stat.S_ISSOCK(info.st_mode)
        or info.st_uid != 0
        or info.st_gid != group_id
        or stat.S_IMODE(info.st_mode) != 0o660
    ):
        raise HarnessError("control fixture socket identity differs")


def _control_request_payload(request_id: UUID, command: str, arguments: object) -> bytes:
    payload = canonical_json(
        {
            "version": 1,
            "request_id": str(request_id),
            "command": command,
            "arguments": arguments,
        }
    )
    if len(payload) > 4096 or not payload.endswith(b"\n"):
        raise HarnessError("control fixture request exceeds its closed bound")
    return payload


def _validate_control_response(
    payload: bytes, *, request_id: UUID, expect_ok: bool
) -> dict[str, object]:
    if not payload.endswith(b"\n") or len(payload) > 16 * 1024 or b"path" in payload.lower():
        raise HarnessError("control fixture response framing or privacy differs")
    value = _strict_json(payload, "control response")
    if (
        value.get("version") != 1
        or value.get("request_id") != str(request_id)
        or value.get("ok") is not expect_ok
    ):
        raise HarnessError("control fixture response identity differs")
    field = "result" if expect_ok else "error"
    result = value.get(field)
    if not isinstance(result, dict):
        raise HarnessError("control fixture response body differs")
    return cast(dict[str, object], result)


async def _raw_control_request(
    path: Path,
    *,
    request_id: UUID,
    command: str,
    arguments: object,
    expect_ok: bool = True,
) -> dict[str, object]:
    open_unix_connection = cast(
        Any, asyncio.open_unix_connection  # type: ignore[attr-defined]
    )
    reader, writer = await asyncio.wait_for(
        open_unix_connection(str(path)), timeout=CONTROL_PROTOCOL_CLIENT_TIMEOUT_S
    )
    try:
        writer.write(_control_request_payload(request_id, command, arguments))
        await asyncio.wait_for(
            writer.drain(), timeout=CONTROL_PROTOCOL_CLIENT_TIMEOUT_S
        )
        payload = await asyncio.wait_for(
            reader.readline(), timeout=CONTROL_PROTOCOL_CLIENT_TIMEOUT_S
        )
        return _validate_control_response(payload, request_id=request_id, expect_ok=expect_ok)
    finally:
        writer.close()
        await asyncio.wait_for(
            writer.wait_closed(), timeout=CONTROL_PROTOCOL_CLIENT_TIMEOUT_S
        )


async def _joined_durable_worker(
    callback: Any, /, *arguments: object, **keywords: object
) -> Any:
    """Observe an inner deadline but never detach a native mutation worker."""

    worker = asyncio.create_task(asyncio.to_thread(callback, *arguments, **keywords))
    cancellation: asyncio.CancelledError | None = None
    try:
        done, _ = await asyncio.wait({worker}, timeout=CONTROL_DURABLE_TIMEOUT_S)
    except asyncio.CancelledError as error:
        done = set()
        cancellation = error
    exceeded = cancellation is None and worker not in done
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError as error:
            cancellation = error
    if cancellation is not None:
        if not worker.cancelled():
            worker.exception()
        raise cancellation
    result = worker.result()
    if exceeded:
        raise HarnessError("control durable worker exceeded its inner deadline")
    return result


def _validate_control_client_environment(work: Path) -> Path:
    resolved_work = work.resolve(strict=True)
    runtime = _control_runtime_directory(resolved_work)
    expected_python = (
        Path("/opt/dashcam/releases") / EXPECTED_RELEASE / "venv/bin/python"
    ).resolve(strict=True)
    if (
        sys.platform != "linux"
        or os.geteuid() != 0
        or Path(sys.executable).resolve() != expected_python
    ):
        raise HarnessError("control client process identity differs")
    _validate_crash_fixture_mount(
        RECORDING_ROOT.resolve(strict=True),
        resolved_work / "recording.exfat.img",
        expected_size=EXFAT_IMAGE_BYTES,
        filesystem="exfat",
        label="M10LOOP",
    )
    _validate_crash_fixture_mount(
        (resolved_work / "catalog").resolve(strict=True),
        resolved_work / "catalog.ext4.img",
        expected_size=EXT4_IMAGE_BYTES,
        filesystem="ext4",
        label="M10CAT",
    )
    socket_path = runtime / CONTROL_SOCKET_NAME
    _validate_control_socket(socket_path, group_id=_dashcam_api_group_id())
    return socket_path


def _arm_parent_death_sigkill() -> None:
    from ctypes import CDLL, c_int, get_errno

    if sys.platform != "linux" or os.geteuid() != 0:
        raise HarnessError("lease client parent-death guard requires root Linux")
    parent_pid = os.getppid()
    if parent_pid <= 1:
        raise HarnessError("lease client lacks its exact worker parent")
    library = CDLL(None, use_errno=True)
    prctl = library.prctl
    prctl.argtypes = (c_int, c_int, c_int, c_int, c_int)
    prctl.restype = c_int
    if prctl(PR_SET_PDEATHSIG, SIGKILL_NUMBER, 0, 0, 0) != 0:
        raise HarnessError(f"lease client parent-death guard failed with errno {get_errno()}")
    if os.getppid() != parent_pid:
        os.kill(os.getpid(), SIGKILL_NUMBER)
        os._exit(125)


def _lease_client(arguments: argparse.Namespace) -> int:
    _arm_parent_death_sigkill()
    clip_id = UUID(cast(str, arguments.lease_clip_id))
    if clip_id != UUID(int=CONTROL_FIXTURE_BASE_ORDER + 1):
        raise HarnessError("lease client clip identity differs")
    bundle = Path(arguments.bundle).resolve(strict=True)
    verify_bundle(bundle, arguments.expected_manifest_sha256, arguments.expected_commit)
    socket_path = _validate_control_client_environment(Path(cast(str, arguments.work)))
    request_id = UUID(int=8_001)
    client = socket.socket(
        cast(int, socket.AF_UNIX),  # type: ignore[attr-defined]
        socket.SOCK_STREAM,
    )
    client.settimeout(CONTROL_PROTOCOL_CLIENT_TIMEOUT_S)
    try:
        client.connect(str(socket_path))
        client.sendall(
            _control_request_payload(
                request_id,
                "acquire_download",
                {"clip_id": str(clip_id), "member": "video", "holder": "abandoned"},
            )
        )
        response = bytearray()
        while not response.endswith(b"\n") and len(response) <= 16 * 1024:
            chunk = client.recv(4096)
            if not chunk:
                break
            response.extend(chunk)
        result = _validate_control_response(
            bytes(response), request_id=request_id, expect_ok=True
        )
        lease_id = result.get("lease_id")
        if not isinstance(lease_id, str) or re.fullmatch(r"[0-9a-f]{32}", lease_id) is None:
            raise HarnessError("lease client authority is not opaque and bounded")
        _write_all(sys.stdout.fileno(), CONTROL_CLIENT_CONFIRMATION)
        cast(Any, signal.alarm)(CONTROL_CLIENT_TIMEOUT_S + 5)  # type: ignore[attr-defined]
        while True:
            cast(Any, signal.pause)()  # type: ignore[attr-defined]
    finally:
        client.close()


def _run_abandoned_lease_client(
    *, bundle: Path, work: Path, expected_manifest_sha256: str, expected_commit: str
) -> None:
    command = (
        sys.executable,
        "-I",
        str(bundle / "run.py"),
        "--lease-client",
        "--bundle",
        str(bundle),
        "--work",
        str(work),
        "--expected-manifest-sha256",
        expected_manifest_sha256,
        "--expected-commit",
        expected_commit,
        "--lease-clip-id",
        str(UUID(int=CONTROL_FIXTURE_BASE_ORDER + 1)),
    )
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"LC_ALL": "C", "PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1"},
    )
    stdout = b""
    stderr = b""
    try:
        assert process.stdout is not None and process.stderr is not None
        ready, _, _ = select.select((process.stdout,), (), (), CONTROL_CLIENT_TIMEOUT_S)
        if ready:
            stdout = os.read(process.stdout.fileno(), len(CONTROL_CLIENT_CONFIRMATION) + 1)
        if stdout != CONTROL_CLIENT_CONFIRMATION:
            process.kill()
            process.wait(timeout=5)
            stderr = process.stderr.read(513)
            raise HarnessError(
                "lease client did not confirm acquisition: "
                f"stdout={len(stdout)}:{_sha256(stdout)} stderr={len(stderr)}:{_sha256(stderr)}"
            )
        process.kill()
        returncode = process.wait(timeout=5)
        stderr = process.stderr.read(513)
        if returncode != -SIGKILL_NUMBER or stderr:
            raise HarnessError(
                "lease client loss contract differed: "
                f"rc={returncode} stderr={len(stderr)}:{_sha256(stderr)}"
            )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


def _matrix_control_component(
    root: Path,
    catalog_path: Path,
    *,
    bundle: Path,
    work: Path,
    expected_manifest_sha256: str,
    expected_commit: str,
) -> dict[str, object]:
    from dashcam.catalog.database import ClipCatalog
    from dashcam.config import default_config
    from dashcam.control.dispatcher import (
        MAX_ACTIVE_DOWNLOAD_LEASES,
        RecorderControlDispatcher,
    )
    from dashcam.control.socket_server import (
        MAX_CONCURRENT_CLIENTS,
        BoundedConnectionHandler,
        RecorderUnixServer,
    )
    from dashcam.recorder.finalizer import (
        DurableRootedFinalizationFilesystem,
        RecorderClipFinalizer,
    )
    from dashcam.state import ClipLifecycle
    from dashcam.storage.intents import PairPaths
    from dashcam.storage.naming import finalized_unsynced_clip_pair

    async def scenario() -> dict[str, object]:
        group_id = _dashcam_api_group_id()
        runtime_dir = _control_runtime_directory(work)
        if runtime_dir.exists() or runtime_dir.is_symlink():
            raise HarnessError("control runtime directory is not fresh")
        runtime_dir.mkdir(mode=0o700)
        socket_path = runtime_dir / CONTROL_SOCKET_NAME
        filesystem = DurableRootedFinalizationFilesystem(
            root, expected_device_id=_device_id(root)
        )
        clock = [2_000_000_000]
        config = default_config()
        config = replace(
            config,
            storage=replace(
                config.storage,
                download_lease_timeout_s=1,
                protect_previous_clips=2,
                protect_next_clips=1,
            ),
        )
        request_number = 9_000
        active_clip_id: list[UUID | None] = [None]
        server: Any | None = None
        if (
            MAX_ACTIVE_DOWNLOAD_LEASES != CONTROL_MAX_ACTIVE_LEASES
            or MAX_CONCURRENT_CLIENTS != 8
        ):
            raise HarnessError("control production bounds differ from reviewed values")
        if not (
            0
            < CONTROL_DURABLE_TIMEOUT_S
            < CONTROL_DISPATCHER_TIMEOUT_S
            < CONTROL_HANDLER_TIMEOUT_S
            < CONTROL_PROTOCOL_CLIENT_TIMEOUT_S
            < CONTROL_CLIENT_TIMEOUT_S
        ):
            raise HarnessError("control deadline nesting differs")

        with ClipCatalog(catalog_path) as catalog:
            finalizer = RecorderClipFinalizer(
                catalog=catalog,
                filesystem=filesystem,
                monotonic_ns=lambda: clock[0],
            )
            clips = []
            for order in range(
                CONTROL_FIXTURE_BASE_ORDER,
                CONTROL_FIXTURE_BASE_ORDER + CONTROL_FIXTURE_CLIP_COUNT,
            ):
                clip = _fixture_clip(order)
                _materialize_clip(root, clip, video_bytes=32 * 1024)
                catalog.register_clip(clip, catalog_now_ns=order)
                clips.append(clip)

            async def execute_intent(intent_id: UUID) -> None:
                outcome = await _joined_durable_worker(finalizer.execute_intent, intent_id)
                if not outcome.complete:
                    raise HarnessError("control intent did not converge")

            class ComponentRuntimeControlSeam:
                async def trigger_control_event(
                    self,
                    source: Any,
                    monotonic_now_ns: int,
                    previous_count: int,
                    next_count: int,
                    event_id: UUID,
                ) -> Any:
                    return await _joined_durable_worker(
                        finalizer.trigger_event,
                        active_clip_id[0],
                        source=source,
                        monotonic_now_ns=monotonic_now_ns,
                        previous_count=previous_count,
                        next_count=next_count,
                        event_id=event_id,
                    )

            runtime_seam = ComponentRuntimeControlSeam()

            async def unavailable() -> None:
                raise HarnessError("unsupported control callback was invoked")

            def build_server(
                *, max_clients: int = MAX_CONCURRENT_CLIENTS, boot_id: str = CONTROL_BOOT_ID
            ) -> Any:
                dispatcher = RecorderControlDispatcher(
                    catalog=catalog,
                    config_provider=lambda: config,
                    config_writer=lambda _value: None,
                    status_provider=lambda: {"state": "COMPONENT"},
                    health_provider=lambda: {"state": "COMPONENT"},
                    intent_executor=execute_intent,
                    event_executor=runtime_seam.trigger_control_event,
                    restart_callback=unavailable,
                    prepare_removal_callback=unavailable,
                    monotonic_ns=lambda: clock[0],
                    boot_id=boot_id,
                    max_active_download_leases=CONTROL_MAX_ACTIVE_LEASES,
                    operation_timeout_s=CONTROL_DISPATCHER_TIMEOUT_S,
                )
                return RecorderUnixServer(
                    BoundedConnectionHandler(
                        dispatcher,
                        request_timeout_s=CONTROL_HANDLER_TIMEOUT_S,
                        max_concurrent_clients=max_clients,
                    ),
                    path=socket_path,
                    socket_group_id=group_id,
                    owner_uid=0,
                    drain_timeout_s=0.1,
                )

            async def request(
                command: str, arguments: object, *, expect_ok: bool = True
            ) -> dict[str, object]:
                nonlocal request_number
                request_number += 1
                return await _raw_control_request(
                    socket_path,
                    request_id=UUID(int=request_number),
                    command=command,
                    arguments=arguments,
                    expect_ok=expect_ok,
                )

            def approval(clip: Any, holder: str) -> dict[str, object]:
                return {
                    "clip_id": str(clip.clip_id),
                    "member": "video",
                    "holder": holder,
                }

            try:
                server = build_server()
                await server.start()
                _validate_control_socket(socket_path, group_id=group_id)
                await asyncio.to_thread(
                    _run_abandoned_lease_client,
                    bundle=bundle,
                    work=work,
                    expected_manifest_sha256=expected_manifest_sha256,
                    expected_commit=expected_commit,
                )
                abandoned = catalog.get_clip(clips[0].clip_id).download_lease
                if abandoned is None or abandoned.holder == "abandoned":
                    raise HarnessError("abandoned control lease was not durably opaque")
                lease_results: list[tuple[Any, str]] = []
                for index, clip in enumerate(clips[1:CONTROL_MAX_ACTIVE_LEASES], start=1):
                    acquired = await request("acquire_download", approval(clip, f"holder-{index}"))
                    lease_id = acquired.get("lease_id")
                    if (
                        not isinstance(lease_id, str)
                        or re.fullmatch(r"[0-9a-f]{32}", lease_id) is None
                    ):
                        raise HarnessError("control lease authority differs")
                    lease_results.append((clip, lease_id))
                capped = await request(
                    "acquire_download",
                    approval(clips[CONTROL_MAX_ACTIVE_LEASES], "over-cap"),
                    expect_ok=False,
                )
                if capped.get("code") != "CONFLICT":
                    raise HarnessError("control lease global cap did not refuse")

                await server.stop()
                server = build_server()
                await server.start()
                capped_after_restart = await request(
                    "acquire_download",
                    approval(clips[CONTROL_MAX_ACTIVE_LEASES], "restart-over-cap"),
                    expect_ok=False,
                )
                if capped_after_restart.get("code") != "CONFLICT":
                    raise HarnessError("restarted dispatcher lost the global lease cap")
                release_clip, release_id = lease_results[0]
                released = await request(
                    "release_download",
                    {"clip_id": str(release_clip.clip_id), "lease_id": release_id},
                )
                if released.get("released") is not True:
                    raise HarnessError("restarted dispatcher lost release authority")
                replacement = await request(
                    "acquire_download",
                    approval(clips[CONTROL_MAX_ACTIVE_LEASES], "replacement"),
                )
                if not isinstance(replacement.get("lease_id"), str):
                    raise HarnessError("global lease capacity did not reopen")

                clock[0] = abandoned.expires_at_monotonic_ns - 1
                before_expiry = catalog.retention_candidates(
                    monotonic_now_ns=clock[0], boot_id=CONTROL_BOOT_ID, limit=64
                )
                abandoned_candidate = next(
                    item for item in before_expiry if item.clip_id == clips[0].clip_id
                )
                if abandoned is None or abandoned_candidate.eligible_at(clock[0]):
                    raise HarnessError("active lease entered the retention view")
                cleared_before, _ = catalog.clear_expired_download_leases(
                    monotonic_now_ns=clock[0], boot_id=CONTROL_BOOT_ID, limit=64
                )
                if cleared_before != 0:
                    raise HarnessError("same-boot lease expired before equality")
                clock[0] = abandoned.expires_at_monotonic_ns
                cleared_at, more = catalog.clear_expired_download_leases(
                    monotonic_now_ns=clock[0], boot_id=CONTROL_BOOT_ID, limit=64
                )
                after_expiry = catalog.retention_candidates(
                    monotonic_now_ns=clock[0], boot_id=CONTROL_BOOT_ID, limit=64
                )
                expired_candidate = next(
                    item for item in after_expiry if item.clip_id == clips[0].clip_id
                )
                if (
                    cleared_at != CONTROL_MAX_ACTIVE_LEASES
                    or more
                    or not expired_candidate.eligible_at(clock[0])
                ):
                    raise HarnessError("same-boot lease equality expiry differs")

                clock[0] += 1
                previous_boot = await request(
                    "acquire_download", approval(clips[33], "previous-boot")
                )
                if not isinstance(previous_boot.get("lease_id"), str):
                    raise HarnessError("previous-boot lease was not acquired")
                more_boot = await _joined_durable_worker(
                    finalizer.expire_download_leases, CONTROL_NEXT_BOOT_ID
                )
                if more_boot or catalog.get_clip(clips[33].clip_id).download_lease is not None:
                    raise HarnessError("previous-boot lease was not immediately cleared")

                manual = clips[34]
                manual_approval = await request(
                    "acquire_download", approval(manual, "manual-freeze")
                )
                manual_lease_id = manual_approval.get("lease_id")
                if not isinstance(manual_lease_id, str):
                    raise HarnessError("manual freeze lease authority differs")
                wrong_lease_id = (
                    ("0" if manual_lease_id[0] != "0" else "1") + manual_lease_id[1:]
                )
                wrong_release = await request(
                    "release_download",
                    {"clip_id": str(manual.clip_id), "lease_id": wrong_lease_id},
                    expect_ok=False,
                )
                if wrong_release.get("code") != "CLIP_BUSY":
                    raise HarnessError("wrong lease authority was not refused")
                leased_protect = await request(
                    "protect_clip",
                    {"clip_id": str(manual.clip_id)},
                    expect_ok=False,
                )
                protected_video = f"protected/{PurePosixPath(manual.video_path).name}"
                protected_sidecar = f"protected/{PurePosixPath(manual.sidecar_path).name}"
                frozen_stored = catalog.get_clip(manual.clip_id)
                if (
                    leased_protect.get("code") != "CLIP_BUSY"
                    or frozen_stored.protected
                    or frozen_stored.video_path != manual.video_path
                    or frozen_stored.sidecar_path != manual.sidecar_path
                    or not (root / manual.video_path).is_file()
                    or not (root / manual.sidecar_path).is_file()
                    or (root / protected_video).exists()
                    or (root / protected_sidecar).exists()
                ):
                    raise HarnessError("manual lease did not freeze the approved pair")
                manual_release = await request(
                    "release_download",
                    {"clip_id": str(manual.clip_id), "lease_id": manual_lease_id},
                )
                repeated_release = await request(
                    "release_download",
                    {"clip_id": str(manual.clip_id), "lease_id": manual_lease_id},
                )
                protected = await request(
                    "protect_clip", {"clip_id": str(manual.clip_id)}
                )
                protected_stored = catalog.get_clip(manual.clip_id)
                if (
                    manual_release.get("released") is not True
                    or repeated_release.get("released") is not False
                    or protected.get("protected") is not True
                    or protected_stored.video_path != protected_video
                    or protected_stored.sidecar_path != protected_sidecar
                    or not (root / protected_video).is_file()
                    or not (root / protected_sidecar).is_file()
                    or (root / manual.video_path).exists()
                    or (root / manual.sidecar_path).exists()
                ):
                    raise HarnessError("manual protect did not converge its pair")
                unprotected = await request(
                    "unprotect_clip", {"clip_id": str(manual.clip_id)}
                )
                unprotected_stored = catalog.get_clip(manual.clip_id)
                if (
                    unprotected.get("protected") is not False
                    or unprotected_stored.video_path != manual.video_path
                    or unprotected_stored.sidecar_path != manual.sidecar_path
                    or not (root / manual.video_path).is_file()
                    or not (root / manual.sidecar_path).is_file()
                    or (root / protected_video).exists()
                    or (root / protected_sidecar).exists()
                ):
                    raise HarnessError("manual unprotect did not converge its pair")

                event_clips = [_fixture_clip(order) for order in (340, 341)]
                for clip in event_clips:
                    _materialize_clip(root, clip, video_bytes=32 * 1024)
                    catalog.register_clip(clip, catalog_now_ns=clock[0])
                event_approval = await request(
                    "acquire_download", approval(event_clips[0], "event-freeze")
                )
                if not isinstance(event_approval.get("lease_id"), str):
                    raise HarnessError("event freeze lease authority differs")
                event_lease = catalog.get_clip(event_clips[0].clip_id).download_lease
                if event_lease is None:
                    raise HarnessError("event freeze lease is not durable")
                current = _writing_fixture(342)
                _materialize_clip(root, current, video_bytes=32 * 1024)
                catalog.register_writing_clip(current, monotonic_now_ns=clock[0])
                active_clip_id[0] = current.clip_id
                event_id = UUID(int=9_999)
                event = await request(
                    "event", {"source": "web", "event_id": str(event_id)}
                )
                if (
                    event.get("protected_clip_ids")
                    != [str(clip.clip_id) for clip in (*event_clips, current)]
                    or event.get("pending_next_count") != 1
                ):
                    raise HarnessError("control event window selection differs")
                event_frozen = catalog.get_clip(event_clips[0].clip_id)
                if (
                    not event_frozen.protected
                    or event_frozen.video_path != event_clips[0].video_path
                    or event_frozen.sidecar_path != event_clips[0].sidecar_path
                    or not (root / event_clips[0].video_path).is_file()
                    or not (root / event_clips[0].sidecar_path).is_file()
                ):
                    raise HarnessError("event lease did not freeze the approved pair")
                active_clip_id[0] = None
                retried = await request(
                    "event", {"source": "web", "event_id": str(event_id)}
                )
                if (
                    retried.get("event_id") != event.get("event_id")
                    or retried.get("protected_clip_ids") != event.get("protected_clip_ids")
                    or retried.get("missing_previous_count")
                    != event.get("missing_previous_count")
                    or retried.get("pending_next_count") != event.get("pending_next_count")
                    or retried.get("queued_intent_ids") != []
                ):
                    raise HarnessError("control event retry without active clip differed")
                clock[0] = event_lease.expires_at_monotonic_ns
                event_expiry_more = await _joined_durable_worker(
                    finalizer.expire_download_leases, CONTROL_BOOT_ID
                )
                event_repaired = catalog.get_clip(event_clips[0].clip_id)
                if (
                    event_expiry_more
                    or event_repaired.download_lease is not None
                    or not event_repaired.video_path.startswith("protected/")
                    or not event_repaired.sidecar_path.startswith("protected/")
                    or not (root / event_repaired.video_path).is_file()
                    or not (root / event_repaired.sidecar_path).is_file()
                    or (root / event_clips[0].video_path).exists()
                    or (root / event_clips[0].sidecar_path).exists()
                ):
                    raise HarnessError("event lease expiry repair did not converge")

                async def finalize_writing(clip: Any) -> None:
                    protection = catalog.active_closing_protection(
                        clip.clip_id, monotonic_now_ns=clock[0]
                    )
                    target = finalized_unsynced_clip_pair(
                        boot_id="m10control", sequence=clip.retention_order
                    )
                    closing = replace(
                        clip,
                        lifecycle=ClipLifecycle.FINALIZING,
                        end_monotonic_ns=clip.start_monotonic_ns + 1_000_000_000,
                        size_bytes=32 * 1024,
                        protected=protection.protected,
                        protection_reason=protection.reason,
                    )
                    filesystem.replace_bytes_atomic(
                        clip.sidecar_path,
                        _canonical_control_sidecar(
                            closing,
                            target_video_name=target.video_name,
                            target_sidecar_name=target.metadata_name,
                        ),
                        maximum_bytes=512 * 1024,
                    )
                    intent_id = catalog.register_finalizing_clip(
                        closing,
                        promotion_paths=PairPaths(
                            clip.video_path,
                            clip.sidecar_path,
                            f"clips/{target.video_name}",
                            f"clips/{target.metadata_name}",
                        ),
                        monotonic_now_ns=clock[0],
                        expected_protection_revision=protection.revision,
                    )
                    pending_finalize = catalog.get_pending_intent(intent_id)
                    if (
                        pending_finalize is None
                        or pending_finalize.clip_id != clip.clip_id
                        or pending_finalize.paths.video_target
                        != f"clips/{target.video_name}"
                        or pending_finalize.paths.sidecar_target
                        != f"clips/{target.metadata_name}"
                    ):
                        raise HarnessError("control FINALIZE intent target binding differs")
                    await execute_intent(intent_id)

                await finalize_writing(current)
                next_clip = _writing_fixture(343)
                _materialize_clip(root, next_clip, video_bytes=32 * 1024)
                catalog.register_writing_clip(next_clip, monotonic_now_ns=clock[0])
                await finalize_writing(next_clip)
                protected_ids = (*event_clips, current, next_clip)
                if any(
                    not catalog.get_clip(clip.clip_id).protected
                    or not catalog.get_clip(clip.clip_id).pair_reconciled
                    or not catalog.get_clip(clip.clip_id).video_path.startswith("protected/")
                    or not catalog.get_clip(clip.clip_id).sidecar_path.startswith("protected/")
                    or not (root / catalog.get_clip(clip.clip_id).video_path).is_file()
                    or not (root / catalog.get_clip(clip.clip_id).sidecar_path).is_file()
                    for clip in protected_ids
                ) or any(
                    (root / clip.video_path).exists() or (root / clip.sidecar_path).exists()
                    for clip in protected_ids
                ):
                    raise HarnessError("control event pair convergence differs")

                await server.stop()
                server = build_server()
                await server.start()
                open_unix_connection = cast(
                    Any, asyncio.open_unix_connection  # type: ignore[attr-defined]
                )
                idle_connections = [
                    await open_unix_connection(str(socket_path))
                    for _ in range(MAX_CONCURRENT_CLIENTS)
                ]
                for _ in range(100):
                    if server.snapshot()["active_connections"] == MAX_CONCURRENT_CLIENTS:
                        break
                    await asyncio.sleep(0.001)
                if server.snapshot()["active_connections"] != MAX_CONCURRENT_CLIENTS:
                    raise HarnessError("control listener did not reach its admission cap")
                refused_reader, refused_writer = await open_unix_connection(str(socket_path))
                refused = await asyncio.wait_for(refused_reader.read(1), timeout=2.0)
                refused_writer.close()
                await refused_writer.wait_closed()
                if refused != b"" or server.snapshot()["connections_refused"] != 1:
                    raise HarnessError("control listener admission cap differed")
                await server.stop()
                server = None
                for _reader, writer in idle_connections:
                    writer.close()
                    with suppress(ConnectionError, OSError):
                        await writer.wait_closed()
                if socket_path.exists():
                    raise HarnessError("control listener drain retained its socket")
            finally:
                if server is not None:
                    await server.stop()
                _cleanup_control_runtime_directory(work, group_id=group_id)

        return {
            "socket_is_unix": True,
            "socket_mode_0660": True,
            "socket_gid_dashcam_api": True,
            "socket_owner_root": True,
            "hard_admission_refused": True,
            "bounded_drain_completed": True,
            "raw_protocol_used": True,
            "lease_authority_opaque": True,
            "response_paths_absent": True,
            "abandoned_client_sigkill_observed": True,
            "lease_survived_client_loss": True,
            "listener_dispatcher_restart_preserved_lease": True,
            "restart_release_authority_succeeded": True,
            "wrong_release_authority_refused": True,
            "idempotent_second_release": True,
            "active_lease_cap": CONTROL_MAX_ACTIVE_LEASES,
            "listener_admission_cap": 8,
            "configured_lease_timeout_s": 1,
            "global_cap_refused": True,
            "same_boot_preexpiry_excluded": True,
            "same_boot_exact_expiry_cleared": True,
            "postexpiry_retention_eligible": True,
            "previous_boot_lease_cleared": True,
            "manual_lease_path_frozen": True,
            "manual_post_release_protect_converged": True,
            "manual_protect_pair_converged": True,
            "manual_unprotect_pair_converged": True,
            "event_lease_path_frozen": True,
            "event_expiry_repair_converged": True,
            "event_previous_count": 2,
            "event_current_count": 1,
            "event_next_count": 1,
            "event_runtime_callback_seam_used": True,
            "event_retry_without_active_idempotent": True,
            "event_pair_intents_converged": True,
            "component_scope": "commit-source-private-loop",
            "production_listener_service_tested": False,
            "download_data_plane_tested": False,
            "production_runtime_tested": False,
            "production_camera_tested": False,
        }

    return asyncio.run(scenario())


def _matrix_g(
    root: Path, catalog_path: Path, capacity: int, device_id: str, uuid: str
) -> dict[str, object]:
    from dashcam.catalog.database import ClipCatalog
    from dashcam.catalog.filesystem import RootedFilesystem
    from dashcam.storage.reclaimer import StorageReclaimer
    from dashcam.storage.retention import StorageThresholds
    from dashcam.storage.space import FilesystemSpaceObservation, StorageSpaceMonitor

    with ClipCatalog(catalog_path) as catalog:
        clip = _fixture_clip(160, protected=True)
        _materialize_clip(root, clip, video_bytes=64 * 1024)
        before = (
            _sha256(_bounded_regular_bytes(root / clip.video_path, 128 * 1024)),
            _sha256(_bounded_regular_bytes(root / clip.sidecar_path, 8192)),
        )
        catalog.register_clip(clip, catalog_now_ns=60)
        reclaimer = StorageReclaimer(
            catalog=catalog, filesystem=RootedFilesystem(root), monotonic_ns=lambda: 70_000
        )
        no_candidate = reclaimer.run_one(boot_id="m10-loop-emergency", allow_new=True)
        monitor = StorageSpaceMonitor(
            volume_uuid=uuid,
            expected_device_id=device_id,
            expected_capacity_bytes=capacity,
            thresholds=StorageThresholds(15, 20, 64 * 1024**2, 32 * 1024**2),
            observer=lambda: FilesystemSpaceObservation(device_id, capacity, 32 * 1024**2 - 1),
            latch_store=catalog,
            reclaimer_available=True,
        )
        emergency = monitor.observe()
        after = (
            _sha256(_bounded_regular_bytes(root / clip.video_path, 128 * 1024)),
            _sha256(_bounded_regular_bytes(root / clip.sidecar_path, 8192)),
        )
    if (
        no_candidate.eligible_found
        or emergency.mode is None
        or emergency.mode.value != "EMERGENCY"
        or emergency.stop_required
        or before != after
    ):
        raise HarnessError("protected-full emergency did not fail closed")
    return {
        "passed": True,
        "no_eligible_candidate": True,
        "protected_pair_unchanged": True,
        "reclaimer_enabled_emergency_observed": True,
        "runtime_no-candidate_safety_stop_tested": False,
        "protected_deletion_allowed": False,
    }


def _write_result(path: Path, value: object) -> None:
    payload = canonical_json(value)
    if len(payload) > MAX_OUTPUT_BYTES or path.exists() or path.is_symlink():
        raise HarnessError("result target is not a fresh bounded file")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _worker(arguments: argparse.Namespace) -> int:
    bundle = Path(arguments.bundle).resolve(strict=True)
    metadata = verify_bundle(bundle, arguments.expected_manifest_sha256, arguments.expected_commit)
    _dashcam_api_group_id()
    current_mount_namespace = os.readlink("/proc/self/ns/mnt")
    current_network_namespace = os.readlink("/proc/self/ns/net")
    if (
        current_mount_namespace == arguments.parent_mount_namespace
        or current_network_namespace != arguments.parent_network_namespace
    ):
        raise HarnessError("worker namespace isolation differs")
    _run((MOUNT, "--make-rprivate", "/"))
    host_row = _findmnt(RECORDING_ROOT)
    if host_row is None:
        raise HarnessError("cloned production mount disappeared before isolation")
    validate_mount_identity(
        host_row,
        source=EXPECTED_STORAGE_SOURCE,
        target=str(RECORDING_ROOT),
        filesystem="exfat",
        uuid=EXPECTED_STORAGE_UUID,
        label=EXPECTED_STORAGE_LABEL,
    )
    _run((UMOUNT, "--", str(RECORDING_ROOT)))
    if _findmnt(RECORDING_ROOT) is not None:
        raise HarnessError("private namespace retained the production recording mount")

    work = Path(arguments.work).resolve(strict=True)
    if not str(work).startswith("/var/tmp/dashcam-m10-retention-loop."):
        raise HarnessError("worker directory differs from the disposable prefix")
    exfat_image = work / "recording.exfat.img"
    ext4_image = work / "catalog.ext4.img"
    catalog_mount = work / "catalog"
    catalog_mount.mkdir(mode=0o700)
    image_contracts = (
        (exfat_image, EXFAT_IMAGE_BYTES),
        (ext4_image, EXT4_IMAGE_BYTES),
    )
    remaining_allocation = sum(size for _image, size in image_contracts)
    for image, size in image_contracts:
        validate_root_remaining_budget(
            _observe_root_backing(require_fixture_budget=False),
            remaining_allocation_bytes=remaining_allocation,
        )
        descriptor = os.open(image, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.ftruncate(descriptor, size)
            os.posix_fallocate(descriptor, 0, size)  # type: ignore[attr-defined]
            os.fsync(descriptor)
            allocated = os.fstat(descriptor)
            if (
                allocated.st_size != size
                or allocated.st_nlink != 1
                or allocated.st_blocks * 512 < size  # type: ignore[attr-defined]
            ):
                raise HarnessError("loop backing image is not fully allocated")
        finally:
            os.close(descriptor)
        remaining_allocation -= size
        validate_root_remaining_budget(
            _observe_root_backing(require_fixture_budget=False),
            remaining_allocation_bytes=remaining_allocation,
        )
    baseline = _loop_snapshot()
    exfat_loop: Path | None = None
    ext4_loop: Path | None = None
    mounted_exfat = False
    mounted_ext4 = False
    try:
        exfat_loop = _attach_loop(exfat_image)
        ext4_loop = _attach_loop(ext4_image)
        _require_owned_loop(exfat_loop, exfat_image)
        _run((MKFS_EXFAT, "-n", "M10LOOP", str(exfat_loop)), timeout=60)
        _require_owned_loop(exfat_loop, exfat_image)
        _require_fully_allocated_image(exfat_image, EXFAT_IMAGE_BYTES)
        _require_owned_loop(ext4_loop, ext4_image)
        _run(
            (
                MKFS_EXT4,
                "-F",
                "-m",
                "0",
                "-E",
                "nodiscard,lazy_itable_init=0,lazy_journal_init=0",
                "-L",
                "M10CAT",
                str(ext4_loop),
            ),
            timeout=60,
        )
        _require_owned_loop(ext4_loop, ext4_image)
        _require_fully_allocated_image(ext4_image, EXT4_IMAGE_BYTES)
        exfat_facts = _mount_loop(exfat_loop, exfat_image, RECORDING_ROOT, "exfat")
        mounted_exfat = True
        ext4_facts = _mount_loop(ext4_loop, ext4_image, catalog_mount, "ext4")
        mounted_ext4 = True
        _require_owned_loop(ext4_loop, ext4_image)
        _require_fully_allocated_image(ext4_image, EXT4_IMAGE_BYTES)
        for directory in ("pending", "clips", "protected", "quarantine"):
            (RECORDING_ROOT / directory).mkdir(mode=0o750)
        provenance = _load_commit_source(
            bundle / "dashcam-source.zip", cast(dict[str, object], metadata["members"])
        )
        capacity, _free = _stat_space(RECORDING_ROOT)
        device_id = _device_id(RECORDING_ROOT)
        matrices: dict[str, object] = {}
        matrices["A"] = _matrix_a(
            catalog_mount / "matrix-a.sqlite3", exfat_facts["UUID"], capacity, device_id
        )
        matrices["B"], matrices["C"] = _matrix_b_c(
            RECORDING_ROOT, catalog_mount / "matrix-bc.sqlite3"
        )
        matrices["D"] = _matrix_d(RECORDING_ROOT, catalog_mount / "matrix-d.sqlite3")
        matrices["E"] = _matrix_e(
            RECORDING_ROOT,
            catalog_mount,
            bundle=bundle,
            work=work,
            expected_manifest_sha256=arguments.expected_manifest_sha256,
            expected_commit=arguments.expected_commit,
        )
        _run((SYNC, "-f", str(RECORDING_ROOT)), timeout=30)
        _run((SYNC, "-f", str(catalog_mount)), timeout=30)
        _unmount_owned(RECORDING_ROOT, exfat_loop, exfat_image)
        mounted_exfat = False
        _unmount_owned(catalog_mount, ext4_loop, ext4_image)
        mounted_ext4 = False
        _require_owned_loop(exfat_loop, exfat_image)
        exfat_check = _run(
            (FSCK_EXFAT, "-n", str(exfat_loop)), accepted=frozenset({0}), timeout=120
        )
        _require_owned_loop(ext4_loop, ext4_image)
        ext4_check = _run((E2FSCK, "-fn", str(ext4_loop)), accepted=frozenset({0}), timeout=120)
        exfat_facts_after = _mount_loop(exfat_loop, exfat_image, RECORDING_ROOT, "exfat")
        mounted_exfat = True
        ext4_facts_after = _mount_loop(ext4_loop, ext4_image, catalog_mount, "ext4")
        mounted_ext4 = True
        _require_owned_loop(ext4_loop, ext4_image)
        _require_fully_allocated_image(ext4_image, EXT4_IMAGE_BYTES)
        if exfat_facts_after != exfat_facts or ext4_facts_after != ext4_facts:
            raise HarnessError("filesystem identity drifted across fsck/remount")
        matrices["F"] = {
            "passed": True,
            "exfat_read_only_fsck_status": exfat_check.returncode,
            "ext4_read_only_fsck_status": ext4_check.returncode,
            "directory_fsync_paths_exercised": True,
            "unmount_remount_identity_stable": True,
        }
        matrices["G"] = _matrix_g(
            RECORDING_ROOT,
            catalog_mount / "matrix-g.sqlite3",
            capacity,
            device_id,
            exfat_facts["UUID"],
        )
        control_component = _matrix_control_component(
            RECORDING_ROOT,
            catalog_mount / "matrix-control.sqlite3",
            bundle=bundle,
            work=work,
            expected_manifest_sha256=arguments.expected_manifest_sha256,
            expected_commit=arguments.expected_commit,
        )
        fixture_files = sum(1 for path in RECORDING_ROOT.rglob("*") if path.is_file())
        if fixture_files > MAX_FIXTURE_FILES:
            raise HarnessError("fixture file count exceeded its hard bound")
        matrices["H"] = {
            "passed": True,
            "private_mount_namespace": True,
            "network_namespace_unchanged": True,
            "production_mount_unmounted_only_in_worker": True,
            "loop_identity_bound": True,
            "source_import_provenance_count": len(provenance),
            "fixture_file_count": fixture_files,
            "control_component": control_component,
        }
        result: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "source": {
                "git_commit": metadata["git_commit"],
                "git_tree": metadata["git_tree"],
                "archive_sha256": metadata["archive_sha256"],
                "imported_module_hashes": provenance,
            },
            "fixture": {
                "recording_filesystem": "loop-backed exfat",
                "catalog_filesystem": "loop-backed ext4",
                "recording_image_bytes": EXFAT_IMAGE_BYTES,
                "catalog_image_bytes": EXT4_IMAGE_BYTES,
            },
            "matrices": matrices,
            **RESULT_FALSE_CLAIMS,
            "deferred_gates": [
                "real-production-daemon-and-camera-integration",
                "structured-gstreamer-no-space-on-physical-recording-path",
                "physical-power-interruption-and-remount",
                "installed-deployable-m10-release",
            ],
        }
        validate_result_evidence(result)
        _write_result(Path(arguments.result), result)
    finally:
        cleanup_errors: list[str] = []
        for mounted, target, loop, image in (
            (mounted_exfat, RECORDING_ROOT, exfat_loop, exfat_image),
            (mounted_ext4, catalog_mount, ext4_loop, ext4_image),
        ):
            if mounted and loop is not None:
                try:
                    _unmount_owned(target, loop, image)
                except Exception as error:
                    cleanup_errors.append(f"unmount:{target.name}:{type(error).__name__}")
        for loop, image in ((exfat_loop, exfat_image), (ext4_loop, ext4_image)):
            if loop is not None and loop.exists():
                try:
                    _detach_owned(loop, image, baseline)
                except Exception as error:
                    cleanup_errors.append(f"detach:{loop.name}:{type(error).__name__}")
        try:
            if _loop_snapshot() != baseline:
                cleanup_errors.append("loop-baseline-differs")
        except Exception as error:
            cleanup_errors.append(f"loop-snapshot:{type(error).__name__}")
        if cleanup_errors:
            raise HarnessError("worker cleanup failed: " + ",".join(cleanup_errors))
    return 0


def _freeze_bundle(
    source: Path,
    expected_manifest: str,
    expected_commit: str,
    *,
    temporary_parent: Path = Path("/run"),
) -> Path:
    metadata = verify_bundle(source, expected_manifest, expected_commit)
    destination = Path(tempfile.mkdtemp(prefix="dashcam-m10-bundle.", dir=str(temporary_parent)))
    try:
        for name in (
            "README.md",
            "run.py",
            "SOURCE.json",
            "dashcam-source.zip",
            "SHA256SUMS",
        ):
            payload = _bounded_regular_bytes(source / name, MAX_BUNDLE_FILE_BYTES)
            path = destination / name
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o500 if name == "run.py" else 0o400,
            )
            try:
                _write_all(descriptor, payload)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        os.chmod(destination, 0o500)
        frozen = verify_bundle(destination, expected_manifest, expected_commit)
        if frozen != metadata:
            raise HarnessError("frozen bundle metadata differs")
        return destination
    except BaseException:
        os.chmod(destination, 0o700)
        for member in destination.iterdir():
            os.chmod(member, 0o600)
        _remove_exact_temporary(destination, parent=temporary_parent, prefix="dashcam-m10-bundle.")
        raise


def _remove_exact_temporary(path: Path, *, parent: Path, prefix: str) -> None:
    resolved = path.resolve(strict=True)
    if resolved.parent != parent.resolve(strict=True) or not resolved.name.startswith(prefix):
        raise HarnessError("temporary cleanup target identity differs")
    shutil.rmtree(resolved)


def _cleanup_worker_loops(work: Path, baseline: tuple[tuple[str, str, bool], ...]) -> None:
    expected_images = {
        (work / "recording.exfat.img").resolve(strict=False),
        (work / "catalog.ext4.img").resolve(strict=False),
    }
    baseline_names = {row[0] for row in baseline}
    errors: list[str] = []
    for name, _backing, _autoclear in _loop_snapshot():
        loop = Path(name)
        if name in baseline_names:
            continue
        try:
            actual = _loop_backing_file(loop)
            if actual not in expected_images:
                errors.append(f"foreign:{name}")
                continue
            validate_cleanup_identity(
                expected_loop=name,
                expected_image=actual,
                mount_source=None,
                backing_file=str(actual),
            )
            _run((LOSSETUP, "--detach", name))
        except Exception as error:
            errors.append(f"cleanup:{name}:{type(error).__name__}")
    try:
        if _loop_snapshot() != baseline:
            errors.append("inventory-differs")
    except Exception as error:
        errors.append(f"inventory:{type(error).__name__}")
    if errors:
        raise HarnessError("parent loop cleanup failed: " + ",".join(errors))


def _owned_loop_backing_present(work: Path) -> bool:
    expected = {
        (work / "recording.exfat.img").resolve(strict=False),
        (work / "catalog.ext4.img").resolve(strict=False),
    }
    for name, _backing, _autoclear in _loop_snapshot():
        try:
            if _loop_backing_file(Path(name)) in expected:
                return True
        except Exception:
            return True
    return False


def _validate_output_path(output: Path) -> Path:
    resolved = output.resolve(strict=False)
    if (
        resolved.parent != Path("/var/tmp").resolve(strict=False)
        or re.fullmatch(r"m10-retention-result(?:-[A-Za-z0-9]{1,32})?\.json", resolved.name) is None
    ):
        raise HarnessError("result output is not a direct bounded /var/tmp result path")
    return resolved


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise HarnessError("result parent is no longer a directory")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_result(output: Path, result: Mapping[str, object]) -> None:
    try:
        _write_result(output, result)
        _fsync_directory(output.parent)
    except BaseException:
        if output.is_file() and not output.is_symlink():
            output.unlink()
            _fsync_directory(output.parent)
        raise
    print(canonical_json(result).decode("ascii"), end="")


def _parent(arguments: argparse.Namespace) -> int:
    import fcntl

    bundle = Path(arguments.bundle).resolve(strict=True)
    output = _validate_output_path(Path(arguments.output))
    if output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise HarnessError("result output must be a fresh file in an existing directory")
    for executable in REQUIRED_EXECUTABLES:
        if not Path(executable).is_file() or not os.access(executable, os.X_OK):
            raise HarnessError(f"required executable is unavailable: {executable}")
    root_backing_before = _observe_root_backing()
    expected_api_group_id = _dashcam_api_group_id()
    lock_path = Path("/run/dashcam-m10-retention-loop.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o600)
    frozen: Path | None = None
    work: Path | None = None
    before: dict[str, object] | None = None
    completed_result: dict[str, object] | None = None
    try:
        try:
            fcntl.flock(  # type: ignore[attr-defined]
                descriptor,
                getattr(fcntl, "LOCK_EX", 2) | getattr(fcntl, "LOCK_NB", 4),
            )
        except BlockingIOError as error:
            raise HarnessError("another M10 retention harness holds the global lock") from error
        locked_root_backing = _observe_root_backing()
        validate_root_backing_poststate(root_backing_before, locked_root_backing)
        root_backing_before = locked_root_backing
        before = _host_snapshot(arguments.expected_board_serial)
        before_digest = _sha256(canonical_json(before))
        frozen = _freeze_bundle(
            bundle, arguments.expected_manifest_sha256, arguments.expected_commit
        )
        work = Path(tempfile.mkdtemp(prefix="dashcam-m10-retention-loop.", dir="/var/tmp"))
        result_path = work / "worker-result.json"
        command = (
            UNSHARE,
            "--mount",
            "--fork",
            "--kill-child",
            "--",
            sys.executable,
            "-I",
            str(frozen / "run.py"),
            "--worker",
            "--bundle",
            str(frozen),
            "--work",
            str(work),
            "--result",
            str(result_path),
            "--expected-manifest-sha256",
            arguments.expected_manifest_sha256,
            "--expected-commit",
            arguments.expected_commit,
            "--parent-mount-namespace",
            cast(str, before["mount_namespace"]),
            "--parent-network-namespace",
            cast(dict[str, str], before["network"])["network_namespace"],
        )
        baseline = cast(tuple[tuple[str, str, bool], ...], before["loop_inventory"])
        try:
            _run(command, timeout=WORKER_TIMEOUT_S, safe_worker_refusal=True)
        finally:
            _cleanup_worker_loops(work, baseline)
        worker_result = _strict_json(
            _bounded_regular_bytes(result_path, MAX_OUTPUT_BYTES), "worker result"
        )
        validate_result_evidence(worker_result)
        after = _host_snapshot(arguments.expected_board_serial)
        after_digest = _sha256(canonical_json(after))
        if after != before or after_digest != before_digest:
            raise HarnessError("production host poststate differs from exact prestate")
        matrices = cast(dict[str, dict[str, object]], worker_result["matrices"])
        matrices["H"]["production_poststate_unchanged"] = True
        matrices["H"]["loop_inventory_restored"] = True
        worker_result["host_prepost_sha256"] = before_digest
        validate_result_evidence(worker_result)
        completed_result = worker_result
    finally:
        cleanup_errors: list[str] = []
        if work is not None and work.exists():
            try:
                _cleanup_control_runtime_directory(
                    work, group_id=expected_api_group_id
                )
            except Exception as error:
                cleanup_errors.append(f"control-runtime:{type(error).__name__}")
            try:
                if _owned_loop_backing_present(work):
                    cleanup_errors.append("work-retained-owned-loop-attached")
                else:
                    _remove_exact_temporary(
                        work,
                        parent=Path("/var/tmp"),
                        prefix="dashcam-m10-retention-loop.",
                    )
            except Exception as error:
                cleanup_errors.append(f"work:{type(error).__name__}")
        if frozen is not None and frozen.exists():
            try:
                os.chmod(frozen, 0o700)
                for member in frozen.iterdir():
                    os.chmod(member, 0o600)
                _remove_exact_temporary(frozen, parent=Path("/run"), prefix="dashcam-m10-bundle.")
            except Exception as error:
                cleanup_errors.append(f"bundle:{type(error).__name__}")
        if before is not None:
            try:
                failure_poststate = _host_snapshot(arguments.expected_board_serial)
                if failure_poststate != before:
                    cleanup_errors.append("production-poststate-differs")
            except Exception as error:
                cleanup_errors.append(f"poststate:{type(error).__name__}")
        try:
            root_backing_after = _observe_root_backing(require_fixture_budget=False)
            validate_root_backing_poststate(root_backing_before, root_backing_after)
            if completed_result is not None:
                completed_result["root_space"] = {
                    "capacity_bytes": root_backing_after.capacity_bytes,
                    "preflight_free_bytes": root_backing_before.free_bytes,
                    "postcleanup_free_bytes": root_backing_after.free_bytes,
                    "required_preflight_bytes": required_root_free_bytes(),
                    "preserved_free_bytes": ROOT_PRESERVED_FREE_BYTES,
                }
        except Exception as error:
            cleanup_errors.append(f"root-reserve:{type(error).__name__}")
        os.close(descriptor)
        if cleanup_errors:
            raise HarnessError("parent final cleanup failed: " + ",".join(cleanup_errors))
    if completed_result is None:
        raise HarnessError("parent completed without bounded result evidence")
    _publish_result(output, completed_result)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--expected-board-serial", default=EXPECTED_BOARD_SERIAL)
    parser.add_argument("--output")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--crash-cell", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--lease-client", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--work", help=argparse.SUPPRESS)
    parser.add_argument("--result", help=argparse.SUPPRESS)
    parser.add_argument("--parent-mount-namespace", help=argparse.SUPPRESS)
    parser.add_argument("--parent-network-namespace", help=argparse.SUPPRESS)
    parser.add_argument("--cell-operation", choices=CRASH_OPERATIONS, help=argparse.SUPPRESS)
    parser.add_argument("--cell-cutpoint", choices=CRASH_CUTPOINTS, help=argparse.SUPPRESS)
    parser.add_argument("--lease-clip-id", help=argparse.SUPPRESS)
    return parser


def _validate_arguments(arguments: argparse.Namespace) -> None:
    if SHA256_RE.fullmatch(arguments.expected_manifest_sha256 or "") is None:
        raise HarnessError("expected manifest SHA-256 is malformed")
    if COMMIT_RE.fullmatch(arguments.expected_commit or "") is None:
        raise HarnessError("expected commit is malformed")
    if re.fullmatch(r"[0-9a-f]{16}", arguments.expected_board_serial or "") is None:
        raise HarnessError("expected board serial is malformed")
    if arguments.expected_board_serial != EXPECTED_BOARD_SERIAL:
        raise HarnessError("expected board serial differs from the accepted exact Pi")
    modes = (arguments.worker, arguments.crash_cell, arguments.lease_client)
    if sum(bool(value) for value in modes) > 1:
        raise HarnessError("worker modes are mutually exclusive")
    if arguments.lease_client:
        if (
            not isinstance(arguments.work, str)
            or not arguments.work
            or arguments.lease_clip_id != str(UUID(int=CONTROL_FIXTURE_BASE_ORDER + 1))
            or any(
                value is not None
                for value in (
                    arguments.output,
                    arguments.result,
                    arguments.parent_mount_namespace,
                    arguments.parent_network_namespace,
                    arguments.cell_operation,
                    arguments.cell_cutpoint,
                )
            )
        ):
            raise HarnessError("lease-client arguments are incomplete or excessive")
    elif arguments.crash_cell:
        if (
            not isinstance(arguments.work, str)
            or not arguments.work
            or arguments.cell_operation not in CRASH_OPERATIONS
            or arguments.cell_cutpoint not in CRASH_CUTPOINTS
            or any(
                value is not None
                for value in (
                    arguments.output,
                    arguments.result,
                    arguments.parent_mount_namespace,
                    arguments.parent_network_namespace,
                    arguments.lease_clip_id,
                )
            )
        ):
            raise HarnessError("crash-cell arguments are incomplete or excessive")
    elif arguments.worker:
        if not all(
            isinstance(value, str) and value
            for value in (
                arguments.work,
                arguments.result,
                arguments.parent_mount_namespace,
                arguments.parent_network_namespace,
            )
        ):
            raise HarnessError("worker arguments are incomplete")
        if arguments.lease_clip_id is not None:
            raise HarnessError("worker arguments are excessive")
    elif not isinstance(arguments.output, str) or not arguments.output:
        raise HarnessError("parent output path is required")


def _emit_worker_refusal(error: Exception) -> int:
    line = (
        _crash_cell_refusal_line(error)
        if isinstance(error, CrashCellContractError)
        else _worker_refusal_line(error)
    )
    _write_all(sys.stderr.fileno(), line)
    return 2


def main() -> int:
    arguments = _parser().parse_args()
    try:
        _validate_arguments(arguments)
        if arguments.lease_client:
            return _lease_client(arguments)
        if arguments.crash_cell:
            return _crash_cell(arguments)
        return _worker(arguments) if arguments.worker else _parent(arguments)
    except (HarnessError, OSError, ValueError, UnicodeError, zipfile.BadZipFile) as error:
        if arguments.worker or arguments.crash_cell or arguments.lease_client:
            return _emit_worker_refusal(error)
        print(f"REFUSED: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        if arguments.worker or arguments.crash_cell or arguments.lease_client:
            return _emit_worker_refusal(error)
        print("REFUSED: unexpected parent exception", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
