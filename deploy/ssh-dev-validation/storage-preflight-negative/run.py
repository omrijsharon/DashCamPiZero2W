#!/usr/bin/env python3
"""Safely exercise production storage preflight refusal paths on the Pi."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from dashcam.config import load_config
from dashcam.storage.preflight import (
    PosixFactsCollector,
    load_storage_identity,
    policy_from_identity,
    recording_root_facts_from_mapping,
)

SCHEMA_VERSION: Final = 1
RECORDING_ROOT: Final = Path("/srv/dashcam")
CONFIG_PATH: Final = Path("/etc/dashcam/config.toml")
IDENTITY_PATH: Final = Path("/etc/dashcam/storage-volume.env")
SENTINEL_NAME: Final = ".dashcam-volume"
PROBE_NAME: Final = ".dashcam-preflight-v1.tmp"
TEMP_PARENT: Final = Path("/var/tmp")
CONTROL_PARENT: Final = Path("/run")
IMAGE_MARGIN_BYTES: Final = 512 * 1024**2
IDENTITY_MARGIN_BYTES: Final = 128 * 1024**2
MAX_OUTPUT_BYTES: Final = 64 * 1024
COMMAND_TIMEOUT_SECONDS: Final = 120
WORKER_TIMEOUT_SECONDS: Final = 180
SSH_TIMEOUT_SECONDS: Final = 3.0
LOOP_RE: Final = re.compile(r"/dev/loop[0-9]{1,4}")
FINGERPRINT: Final = hashlib.sha256(b"dashcam-negative-harness-v1").hexdigest()
EXPECTED_HOST_CID: Final = "fe34325344000000200000031a0192d1"
EXPECTED_HOST_UUID: Final = "7EED-3EA7"
EXPECTED_HOST_SOURCE: Final = "/dev/mmcblk0p3"

FINDMNT: Final = "/usr/bin/findmnt"
MOUNT: Final = "/usr/bin/mount"
UMOUNT: Final = "/usr/bin/umount"
UNSHARE: Final = "/usr/bin/unshare"
SYSTEMCTL: Final = "/usr/bin/systemctl"
NMCLI: Final = "/usr/bin/nmcli"
SYNC: Final = "/usr/bin/sync"
LOSETUP: Final = "/usr/sbin/losetup"
BLKID: Final = "/usr/sbin/blkid"
MKFS_EXFAT: Final = "/usr/sbin/mkfs.exfat"
MKFS_EXT4: Final = "/usr/sbin/mkfs.ext4"
REQUIRED_EXECUTABLES: Final = (
    FINDMNT,
    MOUNT,
    UMOUNT,
    UNSHARE,
    SYSTEMCTL,
    NMCLI,
    SYNC,
    LOSETUP,
    BLKID,
    MKFS_EXFAT,
    MKFS_EXT4,
)


class HarnessError(RuntimeError):
    """A fail-closed validation or safety refusal."""


@dataclass(frozen=True, slots=True)
class Case:
    name: str
    filesystem: str | None
    label: str | None
    identity_mode: str
    sentinel_mode: str
    read_only: bool
    expected_state: str
    expected_reasons: frozenset[str]


CASES: Final = (
    Case(
        "unmounted_rootfs_fallback",
        None,
        None,
        "normal",
        "normal",
        False,
        "FAULTED",
        frozenset(
            {
                "UNMOUNTED",
                "MISSING_MOUNT_IDENTITY",
                "READ_ONLY",
                "MISSING_SENTINEL",
                "INVALID_SPACE",
            }
        ),
    ),
    Case(
        "wrong_filesystem",
        "ext4",
        "DASHCAM",
        "normal",
        "normal",
        False,
        "FAULTED",
        frozenset({"WRONG_FILESYSTEM"}),
    ),
    Case(
        "wrong_label",
        "exfat",
        "NOTDASHCAM",
        "normal",
        "normal",
        False,
        "FAULTED",
        frozenset({"WRONG_LABEL"}),
    ),
    Case(
        "wrong_uuid",
        "exfat",
        "DASHCAM",
        "wrong_uuid",
        "normal",
        False,
        "FAULTED",
        frozenset({"WRONG_UUID"}),
    ),
    Case(
        "wrong_sentinel",
        "exfat",
        "DASHCAM",
        "normal",
        "wrong_identity",
        False,
        "FAULTED",
        frozenset({"WRONG_SENTINEL_IDENTITY"}),
    ),
    Case(
        "read_only",
        "exfat",
        "DASHCAM",
        "normal",
        "normal",
        True,
        "READ_ONLY",
        frozenset({"READ_ONLY"}),
    ),
)
CASE_BY_NAME: Final = {case.name: case for case in CASES}


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _run(
    command: Sequence[str],
    *,
    accepted: frozenset[int] = frozenset({0}),
    timeout: int = COMMAND_TIMEOUT_SECONDS,
    extra_allowed: frozenset[str] = frozenset(),
) -> subprocess.CompletedProcess[bytes]:
    allowed = frozenset(REQUIRED_EXECUTABLES) | extra_allowed
    if (
        not command
        or command[0] not in allowed
        or timeout < 1
        or timeout > WORKER_TIMEOUT_SECONDS
        or any(not item or "\x00" in item or len(item) > 4096 for item in command)
    ):
        raise HarnessError("command differs from the closed harness allowlist")
    try:
        completed = subprocess.run(
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
    if len(completed.stdout) > MAX_OUTPUT_BYTES or len(completed.stderr) > MAX_OUTPUT_BYTES:
        raise HarnessError(f"command output exceeded bound: {command[0]}")
    if completed.returncode not in accepted:
        diagnostic = (completed.stderr or completed.stdout)[:1024].decode(
            "utf-8", errors="backslashreplace"
        )
        diagnostic = " | ".join(diagnostic.splitlines())
        if not diagnostic:
            diagnostic = "no output"
        raise HarnessError(f"command exited {completed.returncode}: {command[0]}: {diagnostic}")
    return completed


def _require_prerequisites() -> None:
    geteuid = cast(Callable[[], int], getattr(os, "geteuid", lambda: -1))
    if (
        os.name != "posix"
        or not Path("/proc/self/ns/mnt").is_symlink()
        or not Path("/proc/self/ns/net").is_symlink()
        or geteuid() != 0
    ):
        raise HarnessError("harness requires root in a normal Linux process")
    for executable in REQUIRED_EXECUTABLES:
        path = Path(executable)
        if not path.is_file() or not os.access(path, os.X_OK):
            raise HarnessError(f"required executable is unavailable: {executable}")
    if (
        not TEMP_PARENT.is_dir()
        or not CONTROL_PARENT.is_dir()
        or RECORDING_ROOT.is_symlink()
        or not RECORDING_ROOT.is_dir()
    ):
        raise HarnessError("fixed harness paths are unavailable or unsafe")
    module_path = Path(cast(str, sys.modules["dashcam.storage.preflight"].__file__)).resolve()
    if "/opt/dashcam/releases/" not in module_path.as_posix():
        raise HarnessError("production preflight is not loaded from an installed release")


def _safe_read(path: Path, limit: int = 64 * 1024) -> bytes:
    listed = path.lstat()
    if stat.S_ISLNK(listed.st_mode) or not stat.S_ISREG(listed.st_mode) or listed.st_nlink != 1:
        raise HarnessError(f"unsafe file identity: {path}")
    if listed.st_size > limit:
        raise HarnessError(f"file exceeds bound: {path}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        current = os.fstat(descriptor)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or (current.st_dev, current.st_ino) != (listed.st_dev, listed.st_ino)
        ):
            raise HarnessError(f"file changed while opening: {path}")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(4096, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(descriptor)
    payload = b"".join(chunks)
    if len(payload) > limit or len(payload) != listed.st_size:
        raise HarnessError(f"file read was incomplete or excessive: {path}")
    return payload


def _read_virtual(path: Path, limit: int = 4096) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(4096, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(descriptor)
    payload = b"".join(chunks)
    if len(payload) > limit:
        raise HarnessError(f"virtual file exceeded bound: {path}")
    return payload


def _write_root_file(
    path: Path,
    payload: bytes,
    mode: int,
    gid: int,
    *,
    set_metadata: bool = True,
) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        fchmod = cast(Callable[[int, int], None] | None, getattr(os, "fchmod", None))
        fchown = cast(
            Callable[[int, int, int], None] | None,
            getattr(os, "fchown", None),
        )
        if set_metadata:
            if fchmod is None or fchown is None:
                raise HarnessError("POSIX ownership functions are unavailable")
            fchmod(descriptor, mode)
            fchown(descriptor, 0, gid)
        written = os.write(descriptor, payload)
        if written != len(payload):
            raise HarnessError("short write creating disposable harness file")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _mount_row() -> dict[str, object]:
    completed = _run(
        (
            FINDMNT,
            "--json",
            "--mountpoint",
            str(RECORDING_ROOT),
            "--output",
            "TARGET,SOURCE,FSTYPE,LABEL,UUID,OPTIONS,MAJ:MIN",
        ),
        accepted=frozenset({0}),
    )
    try:
        value = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HarnessError("findmnt returned malformed JSON") from error
    if not isinstance(value, dict) or set(value) != {"filesystems"}:
        raise HarnessError("findmnt root differs")
    rows = value["filesystems"]
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise HarnessError("findmnt did not return exactly one mount")
    row = cast(dict[str, object], rows[0])
    expected = {"target", "source", "fstype", "label", "uuid", "options", "maj:min"}
    if set(row) != expected or any(
        value is not None and not isinstance(value, str) for value in row.values()
    ):
        raise HarnessError("findmnt row differs from its closed shape")
    return row


def _device_id(path: Path) -> str:
    device = path.stat().st_dev
    major = cast(Callable[[int], int] | None, getattr(os, "major", None))
    minor = cast(Callable[[int], int] | None, getattr(os, "minor", None))
    if major is None or minor is None:
        raise HarnessError("POSIX device-number functions are unavailable")
    return f"{major(device)}:{minor(device)}"


def _host_mount_snapshot() -> dict[str, object]:
    identity = load_storage_identity(IDENTITY_PATH)
    row = _mount_row()
    sentinel_payload = _safe_read(RECORDING_ROOT / SENTINEL_NAME, 4096)
    try:
        sentinel = json.loads(sentinel_payload.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HarnessError("host sentinel is malformed") from error
    canonical = _canonical_json(sentinel)
    if canonical != sentinel_payload or not isinstance(sentinel, dict):
        raise HarnessError("host sentinel is not canonical")
    options = str(row["options"]).split(",")
    expected_sentinel_keys = {
        "layout_version",
        "serial",
        "dashcam_uuid",
        "source_table_fingerprint",
        "root_end_sector",
        "data_start_sector",
        "data_end_sector",
    }
    if (
        row["target"] != str(RECORDING_ROOT)
        or row["source"] != EXPECTED_HOST_SOURCE
        or row["fstype"] != "exfat"
        or row["label"] != "DASHCAM"
        or row["uuid"] != identity.uuid
        or identity.cid != EXPECTED_HOST_CID
        or identity.uuid != EXPECTED_HOST_UUID
        or row["maj:min"] != _device_id(RECORDING_ROOT)
        or row["maj:min"] == _device_id(Path("/"))
        or "rw" not in options
        or "ro" in options
        or set(sentinel) != expected_sentinel_keys
        or sentinel.get("layout_version") != identity.layout_version
        or sentinel.get("serial") != identity.cid
        or sentinel.get("dashcam_uuid") != identity.uuid
        or sentinel.get("source_table_fingerprint") != identity.source_mbr_sha256
        or sentinel.get("root_end_sector") != identity.root_end_sector
        or sentinel.get("data_start_sector") != identity.data_start_sector
        or sentinel.get("data_end_sector") != identity.data_end_sector
    ):
        raise HarnessError("host recording mount is not the exact healthy prerequisite")
    return {
        "row": row,
        "recording_root_device_id": _device_id(RECORDING_ROOT),
        "root_device_id": _device_id(Path("/")),
        "sentinel_sha256": hashlib.sha256(sentinel_payload).hexdigest(),
        "identity_sha256": hashlib.sha256(_safe_read(IDENTITY_PATH, 8192)).hexdigest(),
    }


def _service_active(name: str) -> None:
    result = _run((SYSTEMCTL, "is-active", name), accepted=frozenset({0, 3}))
    if result.returncode != 0 or result.stdout.strip() != b"active":
        raise HarnessError(f"required service is not active: {name}")


def _ssh_banner() -> str:
    try:
        with socket.create_connection(("127.0.0.1", 22), timeout=SSH_TIMEOUT_SECONDS) as stream:
            stream.settimeout(SSH_TIMEOUT_SECONDS)
            banner = stream.recv(256)
    except OSError as error:
        raise HarnessError("SSH loopback listener is not usable") from error
    if len(banner) > 255 or not banner.startswith(b"SSH-2.0-") or b"\n" not in banner:
        raise HarnessError("SSH loopback listener returned an invalid banner")
    return banner.splitlines()[0].decode("ascii", errors="strict")


def _network_snapshot() -> dict[str, object]:
    _service_active("NetworkManager.service")
    _service_active("ssh.service")
    state = _run((NMCLI, "--terse", "--fields", "STATE", "general", "status")).stdout
    try:
        state_text = state.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise HarnessError("NetworkManager returned non-ASCII state") from error
    if not state_text or len(state_text) > 128:
        raise HarnessError("NetworkManager state is missing or excessive")
    return {
        "networkmanager_active": True,
        "networkmanager_state": state_text,
        "ssh_active": True,
        "ssh_banner": _ssh_banner(),
        "network_namespace": os.readlink("/proc/self/ns/net"),
    }


def _validate_namespace_isolation(
    current_mount_namespace: str,
    parent_mount_namespace: str,
    current_network_namespace: str,
    parent_network_namespace: str,
) -> None:
    if current_mount_namespace == parent_mount_namespace:
        raise HarnessError("worker did not enter a private mount namespace")
    if current_network_namespace != parent_network_namespace:
        raise HarnessError("worker unexpectedly entered a different network namespace")


def _unshare_command(
    script: Path,
    case: Case,
    parent_mount_namespace: str,
    parent_network_namespace: str,
    host_snapshot_sha256: str,
) -> tuple[str, ...]:
    return (
        UNSHARE,
        "--mount",
        "--fork",
        "--kill-child",
        sys.executable,
        str(script),
        "--worker-case",
        case.name,
        "--parent-mount-namespace",
        parent_mount_namespace,
        "--parent-network-namespace",
        parent_network_namespace,
        "--host-snapshot-sha256",
        host_snapshot_sha256,
    )


def _loop_backing_file(loop: Path) -> Path:
    metadata = loop.stat()
    if not stat.S_ISBLK(metadata.st_mode):
        raise HarnessError("loop target is not a block device")
    major = cast(Callable[[int], int] | None, getattr(os, "major", None))
    minor = cast(Callable[[int], int] | None, getattr(os, "minor", None))
    raw_device = cast(int, getattr(metadata, "st_rdev", -1))
    if major is None or minor is None or raw_device < 0:
        raise HarnessError("POSIX block-device identity is unavailable")
    device = f"{major(raw_device)}:{minor(raw_device)}"
    backing = Path("/sys/dev/block") / device / "loop/backing_file"
    value = _read_virtual(backing, 4096).decode("utf-8").strip()
    if not value.startswith("/"):
        raise HarnessError("loop backing file is not absolute")
    return Path(value).resolve(strict=True)


def _attach_disposable_loop(image: Path) -> Path:
    result = _run((LOSETUP, "--find", "--show", "--nooverlap", str(image)))
    try:
        loop = Path(result.stdout.decode("ascii").strip())
    except UnicodeDecodeError as error:
        raise HarnessError("losetup returned non-ASCII output") from error
    if LOOP_RE.fullmatch(loop.as_posix()) is None or not loop.exists():
        raise HarnessError("losetup returned an unsafe device")
    if _loop_backing_file(loop) != image.resolve(strict=True):
        raise HarnessError("loop backing file differs from the disposable image")
    return loop


def _detach_disposable_loop(loop: Path) -> None:
    _run((LOSETUP, "--detach", loop.as_posix()))


def _loop_snapshot() -> tuple[tuple[str, str, bool], ...]:
    completed = _run((LOSETUP, "--json", "--list", "--output", "NAME,BACK-FILE,AUTOCLEAR"))
    try:
        value = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HarnessError("losetup returned malformed JSON") from error
    if not isinstance(value, dict) or set(value) != {"loopdevices"}:
        raise HarnessError("losetup JSON root differs")
    rows = value["loopdevices"]
    if not isinstance(rows, list) or len(rows) > 64:
        raise HarnessError("losetup returned an invalid device count")
    snapshot: list[tuple[str, str, bool]] = []
    for raw_row in rows:
        if not isinstance(raw_row, dict) or set(raw_row) != {
            "name",
            "back-file",
            "autoclear",
        }:
            raise HarnessError("losetup returned a malformed row")
        name = raw_row["name"]
        backing = raw_row["back-file"]
        autoclear = raw_row["autoclear"]
        if (
            not isinstance(name, str)
            or LOOP_RE.fullmatch(name) is None
            or not isinstance(backing, str)
            or not backing
            or len(backing) > 4096
            or not isinstance(autoclear, bool)
        ):
            raise HarnessError("losetup row values are unsafe")
        snapshot.append((name, backing, autoclear))
    return tuple(sorted(snapshot))


def _wait_for_loop_snapshot(expected: tuple[tuple[str, str, bool], ...]) -> None:
    for _ in range(50):
        if _loop_snapshot() == expected:
            return
        time.sleep(0.1)
    raise HarnessError("loop inventory did not return to its exact baseline")


def _mkfs_command(case: Case, loop: Path) -> tuple[str, ...]:
    if LOOP_RE.fullmatch(loop.as_posix()) is None:
        raise HarnessError("mkfs target is not a numbered loop device")
    if case.filesystem == "exfat" and case.label is not None:
        return (MKFS_EXFAT, "-n", case.label, loop.as_posix())
    if case.filesystem == "ext4" and case.label is not None:
        return (MKFS_EXT4, "-F", "-m", "0", "-L", case.label, loop.as_posix())
    raise HarnessError("case has no disposable filesystem contract")


def _blkid(loop: Path) -> dict[str, str]:
    result = _run((BLKID, "-o", "export", str(loop)))
    values: dict[str, str] = {}
    for line in result.stdout.decode("ascii").splitlines():
        key, separator, value = line.partition("=")
        if not separator or key in values or not key or not value:
            raise HarnessError("blkid output is malformed")
        values[key] = value
    if not {"DEVNAME", "UUID", "LABEL", "TYPE"} <= set(values):
        raise HarnessError("blkid output lacks required identity")
    if values["DEVNAME"] != str(loop):
        raise HarnessError("blkid inspected a different device")
    return values


def _different_uuid(actual: str) -> str:
    if len(actual) < 5:
        raise HarnessError("disposable filesystem UUID is too short")
    replacement = "0" if actual[0] != "0" else "1"
    return replacement + actual[1:]


def _sentinel(identity_uuid: str, *, wrong_identity: bool) -> dict[str, object]:
    return {
        "layout_version": 1,
        "serial": "wrong-sentinel-card" if wrong_identity else "negative-harness-card",
        "dashcam_uuid": identity_uuid,
        "source_table_fingerprint": FINGERPRINT,
        "root_end_sector": 12_647_871,
        "data_start_sector": 12_648_448,
        "data_end_sector": 18_000_000,
    }


def _identity_payload(uuid: str, minimum_capacity_bytes: int) -> bytes:
    return (
        "DASHCAM_STORAGE_SCHEMA_VERSION=1\n"
        "DASHCAM_STORAGE_LAYOUT_VERSION=1\n"
        "DASHCAM_STORAGE_MOUNT=/srv/dashcam\n"
        f"DASHCAM_STORAGE_UUID={uuid}\n"
        "DASHCAM_STORAGE_CID=negative-harness-card\n"
        f"DASHCAM_STORAGE_SOURCE_MBR_SHA256={FINGERPRINT}\n"
        "DASHCAM_STORAGE_ROOT_END_SECTOR=12647871\n"
        "DASHCAM_STORAGE_DATA_START_SECTOR=12648448\n"
        "DASHCAM_STORAGE_DATA_END_SECTOR=18000000\n"
        f"DASHCAM_STORAGE_MINIMUM_CAPACITY_BYTES={minimum_capacity_bytes}\n"
    ).encode("ascii")


def _mount_options(filesystem: str, *, read_only: bool) -> str:
    prefix = "ro" if read_only else "rw"
    common = f"{prefix},nosuid,nodev,noexec,noatime"
    if filesystem == "exfat":
        return f"{common},uid=0,gid=0,fmask=0137,dmask=0027"
    return common


def _mount(loop: Path, target: Path, filesystem: str, *, read_only: bool) -> None:
    _run(
        (
            MOUNT,
            "-t",
            filesystem,
            "-o",
            _mount_options(filesystem, read_only=read_only),
            str(loop),
            str(target),
        )
    )


def _unmount_exact_loop(target: Path, loop: Path) -> None:
    row = _mount_row() if target == RECORDING_ROOT else None
    if row is not None and row["source"] != str(loop):
        raise HarnessError("refusing to unmount a non-disposable recording-root source")
    _run((UMOUNT, "--", str(target)))


def _prepare_case_files(
    temporary: Path,
    case: Case,
    actual_uuid: str,
    minimum_capacity_bytes: int,
    gid: int,
) -> tuple[Path, Path, str]:
    expected_uuid = (
        _different_uuid(actual_uuid) if case.identity_mode == "wrong_uuid" else actual_uuid
    )
    identity_path = temporary / "storage-volume.env"
    config_path = temporary / "config.toml"
    _write_root_file(
        identity_path, _identity_payload(expected_uuid, minimum_capacity_bytes), 0o640, gid
    )
    _write_root_file(config_path, _safe_read(CONFIG_PATH), 0o640, gid)
    return config_path, identity_path, expected_uuid


def _write_disposable_sentinel(
    mountpoint: Path,
    expected_uuid: str,
    case: Case,
    gid: int,
) -> None:
    _write_root_file(
        mountpoint / SENTINEL_NAME,
        _canonical_json(
            _sentinel(expected_uuid, wrong_identity=case.sentinel_mode == "wrong_identity")
        ),
        0o640,
        gid,
        set_metadata=False,
    )
    _run((SYNC, "-f", str(mountpoint)), timeout=30)


def _validate_case_status(
    case: Case,
    returncode: int,
    status: Mapping[str, object],
    *,
    probe_absent: bool,
) -> None:
    reasons = status.get("reasons")
    if (
        returncode != 2
        or status.get("schema_version") != 1
        or status.get("ready") is not False
        or status.get("state") != case.expected_state
        or not isinstance(reasons, list)
        or any(not isinstance(reason, str) for reason in reasons)
        or frozenset(cast(list[str], reasons)) != case.expected_reasons
        or status.get("probe_attempted") is not False
        or status.get("probe_succeeded") is not False
        or not probe_absent
    ):
        raise HarnessError(f"production preflight result differed for {case.name}")


def _run_production_preflight(case: Case, config: Path, identity: Path) -> dict[str, object]:
    try:
        loaded_config = load_config(config)
        loaded_identity = load_storage_identity(identity)
        policy = policy_from_identity(loaded_config, loaded_identity)
        recording_root_facts_from_mapping(PosixFactsCollector().collect(policy.recording_root))
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise HarnessError(f"installed fact collection failed: {error}") from error
    executable = str(Path(sys.executable).absolute())
    result = _run(
        (
            executable,
            "-m",
            "dashcam.storage.preflight",
            "--config",
            str(config),
            "--identity",
            str(identity),
        ),
        accepted=frozenset({2}),
        timeout=30,
        extra_allowed=frozenset({executable}),
    )
    try:
        status = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HarnessError("production preflight emitted malformed JSON") from error
    if not isinstance(status, dict):
        raise HarnessError("production preflight status is not one object")
    probe_absent = not (RECORDING_ROOT / PROBE_NAME).exists()
    _validate_case_status(case, result.returncode, status, probe_absent=probe_absent)
    return cast(dict[str, object], status)


def _unmount_private_recording_root() -> None:
    _run((UMOUNT, "--", str(RECORDING_ROOT)))
    absent = _run(
        (
            FINDMNT,
            "--json",
            "--mountpoint",
            str(RECORDING_ROOT),
            "--output",
            "TARGET",
        ),
        accepted=frozenset({1}),
    )
    if absent.stdout or absent.stderr:
        raise HarnessError("private recording root still appears as a mountpoint")


def _worker_unmounted(
    case: Case,
    temporary: Path,
    minimum_capacity_bytes: int,
    gid: int,
) -> dict[str, object]:
    identity_path = temporary / "storage-volume.env"
    config_path = temporary / "config.toml"
    _write_root_file(
        identity_path,
        _identity_payload("0000-0001", minimum_capacity_bytes),
        0o640,
        gid,
    )
    _write_root_file(config_path, _safe_read(CONFIG_PATH), 0o640, gid)
    if (RECORDING_ROOT / PROBE_NAME).exists():
        raise HarnessError("underlying rootfs probe path preexists")
    _unmount_private_recording_root()
    return _run_production_preflight(case, config_path, identity_path)


def _worker_loop_case(
    case: Case,
    temporary: Path,
    image_bytes: int,
    minimum_capacity_bytes: int,
    gid: int,
) -> dict[str, object]:
    if case.filesystem is None:
        raise HarnessError("loop case lacks a filesystem")
    image = temporary / f"{case.name}.img"
    descriptor = os.open(
        image,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.ftruncate(descriptor, image_bytes)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    loop = _attach_disposable_loop(image)
    prep = temporary / "prepare"
    prep.mkdir(mode=0o700)
    prep_mounted = False
    target_mounted = False
    try:
        if _loop_backing_file(loop) != image.resolve(strict=True):
            raise HarnessError("mkfs target lost its exact backing-file identity")
        _run(_mkfs_command(case, loop), timeout=60)
        facts = _blkid(loop)
        if facts["TYPE"] != case.filesystem or facts["LABEL"] != case.label:
            raise HarnessError("formatted disposable filesystem identity differs")
        config, identity, expected_uuid = _prepare_case_files(
            temporary,
            case,
            facts["UUID"],
            minimum_capacity_bytes,
            gid,
        )
        _mount(loop, prep, case.filesystem, read_only=False)
        prep_mounted = True
        _write_disposable_sentinel(prep, expected_uuid, case, gid)
        _run((UMOUNT, "--", str(prep)))
        prep_mounted = False

        _unmount_private_recording_root()
        _mount(loop, RECORDING_ROOT, case.filesystem, read_only=case.read_only)
        target_mounted = True
        if (RECORDING_ROOT / PROBE_NAME).exists():
            raise HarnessError("disposable probe path preexists")
        status = _run_production_preflight(case, config, identity)
        _unmount_exact_loop(RECORDING_ROOT, loop)
        target_mounted = False
        return status
    finally:
        if target_mounted:
            with contextlib.suppress(HarnessError):
                _unmount_exact_loop(RECORDING_ROOT, loop)
        if prep_mounted:
            with contextlib.suppress(HarnessError):
                _run((UMOUNT, "--", str(prep)))
        _detach_disposable_loop(loop)


def _storage_gid() -> int:
    payload = _safe_read(Path("/etc/group"), 1024 * 1024)
    matches: list[int] = []
    for line in payload.decode("ascii").splitlines():
        fields = line.split(":")
        if len(fields) == 4 and fields[0] == "dashcam-storage" and fields[2].isdecimal():
            matches.append(int(fields[2]))
    if len(matches) != 1 or not 0 < matches[0] <= 2**31 - 1:
        raise HarnessError("dashcam-storage group identity is missing or ambiguous")
    return matches[0]


def _worker(
    case: Case,
    *,
    parent_mount_namespace: str,
    parent_network_namespace: str,
    host_snapshot_sha256: str,
) -> dict[str, object]:
    current_mount_namespace = os.readlink("/proc/self/ns/mnt")
    current_network_namespace = os.readlink("/proc/self/ns/net")
    _validate_namespace_isolation(
        current_mount_namespace,
        parent_mount_namespace,
        current_network_namespace,
        parent_network_namespace,
    )
    initial_snapshot = _host_mount_snapshot()
    if _digest(initial_snapshot) != host_snapshot_sha256:
        raise HarnessError("worker did not inherit the exact host mount snapshot")
    _run((MOUNT, "--make-rprivate", "/"))
    storage_gid = _storage_gid()
    config = load_config(CONFIG_PATH)
    reserve = math.ceil(config.storage.minimum_free_gib * 1024**3)
    minimum_capacity = reserve + IDENTITY_MARGIN_BYTES
    image_bytes = minimum_capacity + IMAGE_MARGIN_BYTES
    if image_bytes > 4 * 1024**3:
        raise HarnessError("disposable sparse image exceeds its hard limit")

    with tempfile.TemporaryDirectory(
        prefix=f"dashcam-preflight-negative-{case.name}-",
        dir=TEMP_PARENT,
    ) as temporary_name:
        temporary = Path(temporary_name)
        temporary.chmod(0o700)
        if case.filesystem is None:
            status = _worker_unmounted(case, temporary, minimum_capacity, storage_gid)
        else:
            status = _worker_loop_case(
                case,
                temporary,
                image_bytes,
                minimum_capacity,
                storage_gid,
            )
    return {
        "case": case.name,
        "namespace": {
            "mount_private": True,
            "network_unchanged": True,
            "mount": current_mount_namespace,
            "network": current_network_namespace,
        },
        "preflight": status,
    }


def _outer() -> dict[str, object]:
    host_snapshot = _host_mount_snapshot()
    host_digest = _digest(host_snapshot)
    parent_mount_namespace = os.readlink("/proc/self/ns/mnt")
    parent_network_namespace = os.readlink("/proc/self/ns/net")
    network_before_all = _network_snapshot()
    case_results: list[dict[str, object]] = []
    source_script = Path(__file__).resolve(strict=True)
    with tempfile.TemporaryDirectory(
        prefix="dashcam-preflight-negative-control-",
        dir=CONTROL_PARENT,
    ) as control_name:
        control = Path(control_name)
        control.chmod(0o700)
        script = control / "run.py"
        _write_root_file(script, _safe_read(source_script, 1024 * 1024), 0o500, 0)

        for case in CASES:
            before_mount = _host_mount_snapshot()
            before_network = _network_snapshot()
            before_loops = _loop_snapshot()
            command = _unshare_command(
                script,
                case,
                parent_mount_namespace,
                parent_network_namespace,
                host_digest,
            )
            executable = str(Path(sys.executable).absolute())
            completed = _run(
                command,
                accepted=frozenset({0, 2}),
                timeout=WORKER_TIMEOUT_SECONDS,
                extra_allowed=frozenset({executable}),
            )
            _wait_for_loop_snapshot(before_loops)
            try:
                worker_result = json.loads(completed.stdout.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise HarnessError(f"worker emitted malformed JSON: {case.name}") from error
            if (
                not isinstance(worker_result, dict)
                or worker_result.get("ready") is not True
                or worker_result.get("case") != case.name
            ):
                reason = (
                    worker_result.get("reason")
                    if isinstance(worker_result, dict)
                    else "non-object result"
                )
                if not isinstance(reason, str) or not reason or len(reason) > 1024:
                    reason = "missing or invalid refusal reason"
                raise HarnessError(f"worker refused for {case.name}: {reason}")
            after_mount = _host_mount_snapshot()
            after_network = _network_snapshot()
            if before_mount != host_snapshot or after_mount != host_snapshot:
                raise HarnessError(f"host recording mount changed during case: {case.name}")
            case_results.append(
                {
                    **cast(dict[str, object], worker_result),
                    "host_mount_unchanged": True,
                    "networkmanager_usable_before": bool(before_network["networkmanager_active"]),
                    "networkmanager_usable_after": bool(after_network["networkmanager_active"]),
                    "ssh_usable_before": bool(before_network["ssh_active"]),
                    "ssh_usable_after": bool(after_network["ssh_active"]),
                    "ssh_banner_after": after_network["ssh_banner"],
                }
            )

    final_snapshot = _host_mount_snapshot()
    network_after_all = _network_snapshot()
    if final_snapshot != host_snapshot:
        raise HarnessError("host recording mount changed across the validation matrix")
    return {
        "schema_version": SCHEMA_VERSION,
        "ready": True,
        "host_mount_sha256": host_digest,
        "host_mount_unchanged": True,
        "real_recording_device_formatted": False,
        "real_recording_mount_mutated": False,
        "network_namespace_unshared": False,
        "network_before": network_before_all,
        "network_after": network_after_all,
        "cases": case_results,
    }


def _emit(value: Mapping[str, object]) -> None:
    payload = _canonical_json(value)
    if len(payload) > MAX_OUTPUT_BYTES:
        raise HarnessError("result exceeded its output bound")
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-case", choices=tuple(CASE_BY_NAME))
    parser.add_argument("--parent-mount-namespace")
    parser.add_argument("--parent-network-namespace")
    parser.add_argument("--host-snapshot-sha256")
    arguments = parser.parse_args(argv)
    try:
        _require_prerequisites()
        if arguments.worker_case is None:
            if any(
                value is not None
                for value in (
                    arguments.parent_mount_namespace,
                    arguments.parent_network_namespace,
                    arguments.host_snapshot_sha256,
                )
            ):
                raise HarnessError("worker-only arguments were supplied to the parent")
            _emit(_outer())
        else:
            if (
                arguments.parent_mount_namespace is None
                or arguments.parent_network_namespace is None
                or arguments.host_snapshot_sha256 is None
                or not re.fullmatch(r"[0-9a-f]{64}", arguments.host_snapshot_sha256)
            ):
                raise HarnessError("worker safety arguments are incomplete")
            result = _worker(
                CASE_BY_NAME[arguments.worker_case],
                parent_mount_namespace=arguments.parent_mount_namespace,
                parent_network_namespace=arguments.parent_network_namespace,
                host_snapshot_sha256=arguments.host_snapshot_sha256,
            )
            _emit({"schema_version": SCHEMA_VERSION, "ready": True, **result})
    except (
        HarnessError,
        KeyError,
        OSError,
        ValueError,
        subprocess.SubprocessError,
    ) as error:
        _emit(
            {
                "schema_version": SCHEMA_VERSION,
                "ready": False,
                "outcome": "refused",
                "reason": str(error),
            }
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
