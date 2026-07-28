#!/usr/bin/env python3
"""One-shot, fail-closed live validation of interrupted pair finalization."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Final, cast
from uuid import UUID, uuid5

from dashcam.catalog.database import ClipCatalog
from dashcam.catalog.models import CatalogClip, ClipNotFoundError
from dashcam.metadata.reconcile import parse_sidecar_bytes
from dashcam.metadata.schema import (
    SCHEMA_VERSION as SIDECAR_SCHEMA_VERSION,
)
from dashcam.metadata.schema import (
    AudioSummary,
    ClipSidecar,
    GpsSummary,
    VideoSummary,
)
from dashcam.recorder.finalizer import (
    DurableRootedFinalizationFilesystem,
    FinalizationRefused,
    RecorderClipFinalizer,
)
from dashcam.state import (
    ClipLifecycle,
    GpsTimeState,
    SystemClockState,
    TimestampQuality,
)
from dashcam.storage.intents import IntentKind, OperationIntent

HARNESS_SCHEMA_VERSION: Final = 1
HARNESS_NAME: Final = "dashcam-finalization-live-v1"
RECORDING_ROOT: Final = Path("/srv/dashcam")
CATALOG_PATH: Final = Path("/var/lib/dashcam/catalog.sqlite3")
IDENTITY_PATH: Final = Path("/etc/dashcam/storage-volume.env")
SENTINEL_PATH: Final = RECORDING_ROOT / ".dashcam-volume"
MANIFEST_PATH: Final = Path(__file__).with_name("SHA256SUMS")
EXPECTED_CARD_CID: Final = "fe34325344000000200000031a0192d1"
EXPECTED_VOLUME_UUID: Final = "7EED-3EA7"
EXPECTED_SOURCE: Final = "/dev/mmcblk0p3"
EXPECTED_LABEL: Final = "DASHCAM"
EXPECTED_FILESYSTEM: Final = "exfat"
EXPECTED_USER: Final = "dashcam"
MAX_MANAGED_ENTRIES: Final = 4_096
MAX_PENDING_INTENTS: Final = 1_024
MAX_FILE_BYTES: Final = 1_048_576
MAX_RECORDED_VIDEO_BYTES: Final = 2 * 1_024**3
COMMAND_TIMEOUT_SECONDS: Final = 15
WORKER_TIMEOUT_SECONDS: Final = 30
RECOVERY_TIMEOUT_SECONDS: Final = 30
BOOT_ID_PATH: Final = Path("/proc/sys/kernel/random/boot_id")
FINDMNT: Final = "/usr/bin/findmnt"
SYSTEMCTL: Final = "/usr/bin/systemctl"
REQUIRED_MANIFEST_NAMES: Final = frozenset({"README.md", "run.py"})
MANAGED_DIRECTORIES: Final = ("pending", "clips", "protected", "quarantine")
IDENTITY_NAMESPACE: Final = UUID("8508cd9d-489e-48f3-9b16-13f5c5ead31b")
SIGKILL_NUMBER: Final = cast(int, getattr(signal, "SIGKILL", 9))


class HarnessError(RuntimeError):
    """The live state is unsafe, ambiguous, or different from the contract."""


@dataclass(frozen=True, slots=True)
class TestIdentity:
    """All deterministic identities and paths derived from one caller UUID."""

    clip_id: UUID
    boot_id: UUID
    short_boot_id: str
    sequence: int
    source_video: str
    source_sidecar: str
    target_video: str
    target_sidecar: str
    start_monotonic_ns: int
    end_monotonic_ns: int

    def __post_init__(self) -> None:
        if not 900_000 <= self.sequence <= 999_999:
            raise ValueError("validation sequence is outside its reserved bounded range")
        if self.sequence <= 10:
            raise ValueError("validation identity overlaps retained diagnostics")

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["clip_id"] = str(self.clip_id)
        value["boot_id"] = str(self.boot_id)
        return value


@dataclass(frozen=True, slots=True)
class HarnessPaths:
    recording_root: Path = RECORDING_ROOT
    catalog_path: Path = CATALOG_PATH


@dataclass(frozen=True, slots=True)
class CollisionIdentity:
    """Case-only target collision bound to this kernel boot and sequence."""

    token: UUID
    boot_id: UUID
    short_boot_id: str
    sequence: int
    source_video: str
    source_sidecar: str
    target_video: str
    target_sidecar: str
    sentinel_path: str

    def __post_init__(self) -> None:
        if not 11 <= self.sequence <= 999_999:
            raise ValueError("collision sequence overlaps reserved diagnostics or is invalid")
        if (
            PurePosixPath(self.sentinel_path).name.casefold()
            != PurePosixPath(self.target_sidecar).name.casefold()
        ):
            raise ValueError("collision sentinel is not a case-only target")
        if self.sentinel_path == self.target_sidecar:
            raise ValueError("collision sentinel must differ by case")

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["token"] = str(self.token)
        value["boot_id"] = str(self.boot_id)
        return value


@dataclass(frozen=True, slots=True)
class RecordedIdentity:
    short_boot_id: str
    sequence: int
    source_video: str
    source_sidecar: str
    target_video: str
    target_sidecar: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")


def derive_test_identity(clip_id: UUID) -> TestIdentity:
    """Derive a collision-resistant, reproducible namespace from one UUIDv4."""

    if (
        not isinstance(clip_id, UUID)
        or clip_id.version != 4
        or clip_id.variant != UUID("00000000-0000-4000-8000-000000000000").variant
    ):
        raise HarnessError("identity must be one canonical random UUIDv4")
    boot_id = uuid5(IDENTITY_NAMESPACE, str(clip_id))
    short_boot_id = boot_id.hex[:12]
    sequence = 900_000 + clip_id.int % 100_000
    source_stem = f"boot-{short_boot_id}-{sequence:06d}.partial"
    target_stem = f"boot-{short_boot_id}-{sequence:06d}"
    start_monotonic_ns = 10_000_000_000 + clip_id.int % 1_000_000_000
    return TestIdentity(
        clip_id=clip_id,
        boot_id=boot_id,
        short_boot_id=short_boot_id,
        sequence=sequence,
        source_video=f"pending/{source_stem}.mp4",
        source_sidecar=f"pending/{source_stem}.json",
        target_video=f"clips/{target_stem}.mp4",
        target_sidecar=f"clips/{target_stem}.json",
        start_monotonic_ns=start_monotonic_ns,
        end_monotonic_ns=start_monotonic_ns + 1_000_000_000,
    )


def derive_collision_identity(token: UUID, boot_id: UUID, sequence: int) -> CollisionIdentity:
    if token.version != 4:
        raise HarnessError("collision token must be one canonical random UUIDv4")
    if not isinstance(boot_id, UUID):
        raise HarnessError("kernel boot identity is invalid")
    if isinstance(sequence, bool) or not isinstance(sequence, int):
        raise HarnessError("collision sequence must be an integer")
    short_boot_id = boot_id.hex[:12]
    source_stem = f"boot-{short_boot_id}-{sequence:06d}.partial"
    target_stem = f"boot-{short_boot_id}-{sequence:06d}"
    target_sidecar = f"clips/{target_stem}.json"
    return CollisionIdentity(
        token=token,
        boot_id=boot_id,
        short_boot_id=short_boot_id,
        sequence=sequence,
        source_video=f"pending/{source_stem}.mp4",
        source_sidecar=f"pending/{source_stem}.json",
        target_video=f"clips/{target_stem}.mp4",
        target_sidecar=target_sidecar,
        sentinel_path=f"clips/{PurePosixPath(target_sidecar).name.upper()}",
    )


def collision_sentinel(identity: CollisionIdentity) -> bytes:
    return canonical_json(
        {
            "schema_version": 1,
            "purpose": "dashcam-finalization-casefold-collision-v1",
            "token": str(identity.token),
            "boot_id": str(identity.boot_id),
            "sequence": identity.sequence,
            "casefold_target": PurePosixPath(identity.target_sidecar).name,
        }
    )


def derive_recorded_identity(short_boot_id: str, sequence: int) -> RecordedIdentity:
    if (
        not isinstance(short_boot_id, str)
        or len(short_boot_id) != 12
        or any(character not in "0123456789abcdef" for character in short_boot_id)
    ):
        raise HarnessError("recorded boot ID must be exactly 12 lowercase hexadecimal characters")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or not 0 <= sequence <= 999_999:
        raise HarnessError("recorded sequence must be between 0 and 999999")
    source_stem = f"boot-{short_boot_id}-{sequence:06d}.partial"
    target_stem = f"boot-{short_boot_id}-{sequence:06d}"
    return RecordedIdentity(
        short_boot_id=short_boot_id,
        sequence=sequence,
        source_video=f"pending/{source_stem}.mp4",
        source_sidecar=f"pending/{source_stem}.json",
        target_video=f"clips/{target_stem}.mp4",
        target_sidecar=f"clips/{target_stem}.json",
    )


def synthetic_mp4(identity: TestIdentity) -> bytes:
    """Return deterministic nonempty ISO-BMFF test bytes, not a codec fixture."""

    identity_bytes = identity.clip_id.bytes + identity.boot_id.bytes
    # A normal ftyp box followed by a private free box carrying deterministic identity.
    return (
        b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"
        + (len(identity_bytes) + 8).to_bytes(4, "big")
        + b"free"
        + identity_bytes
    )


def build_sidecar(identity: TestIdentity) -> ClipSidecar:
    return ClipSidecar(
        schema_version=SIDECAR_SCHEMA_VERSION,
        clip_id=identity.clip_id,
        boot_id=identity.boot_id,
        sequence=identity.sequence,
        video_file=PurePosixPath(identity.target_video).name,
        metadata_file=PurePosixPath(identity.target_sidecar).name,
        start_utc=None,
        end_utc=None,
        start_monotonic_ns=identity.start_monotonic_ns,
        end_monotonic_ns=identity.end_monotonic_ns,
        gps_time_state=GpsTimeState.UNSYNCED,
        system_clock_state=SystemClockState.UNSET,
        timestamp_quality=TimestampQuality.MONOTONIC_ONLY,
        time_anchor=None,
        timezone="Etc/UTC",
        start_local=None,
        video=VideoSummary(
            codec="h264",
            width=16,
            height=16,
            fps_nominal=1.0,
            target_bitrate_bps=1,
            measured_bitrate_bps=0,
            frames_written=0,
            dropped_frames=0,
        ),
        audio=AudioSummary(
            available=False,
            codec=None,
            sample_rate_hz=None,
            channels=None,
            target_bitrate_bps=None,
        ),
        gps=GpsSummary(available=False, first_fix_utc=None),
        protected=False,
        protection_reason=None,
        software_version=HARNESS_NAME,
        warnings=("deterministic synthetic MP4; finalization recovery validation only",),
    )


class CrashAfterFirstMoveFilesystem(DurableRootedFinalizationFilesystem):
    """Production filesystem whose first fully durable move triggers a hard stop."""

    def __init__(
        self,
        root: Path,
        *,
        expected_device_id: str | None,
        crash: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(root, expected_device_id=expected_device_id)
        self._move_count = 0
        self._crash = crash or _sigkill_self

    @property
    def move_count(self) -> int:
        return self._move_count

    def move(self, source: str, target: str) -> None:
        super().move(source, target)
        self._move_count += 1
        if self._move_count == 1:
            self._crash()
            raise AssertionError("SIGKILL callback unexpectedly returned")


def _sigkill_self() -> None:
    os.kill(os.getpid(), SIGKILL_NUMBER)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def verify_bundle(
    *,
    manifest_path: Path = MANIFEST_PATH,
    expected_manifest_sha256: str | None = None,
) -> dict[str, object]:
    payload = _safe_read_file(manifest_path, maximum_bytes=16_384)
    manifest_sha256 = _sha256(payload)
    if expected_manifest_sha256 is not None and manifest_sha256 != expected_manifest_sha256.lower():
        raise HarnessError("bundle manifest SHA-256 differs from the reviewed value")
    try:
        lines = payload.decode("ascii").splitlines()
    except UnicodeDecodeError as error:
        raise HarnessError("bundle manifest is not ASCII") from error
    entries: dict[str, str] = {}
    for line in lines:
        parts = line.split("  ")
        if (
            len(parts) != 2
            or len(parts[0]) != 64
            or any(character not in "0123456789abcdef" for character in parts[0])
            or "/" in parts[1]
            or "\\" in parts[1]
            or parts[1] in {"", ".", ".."}
            or parts[1] in entries
        ):
            raise HarnessError("bundle manifest has an invalid closed entry")
        entries[parts[1]] = parts[0]
    if frozenset(entries) != REQUIRED_MANIFEST_NAMES:
        raise HarnessError("bundle manifest file set differs")
    observed: dict[str, str] = {}
    for name, expected in sorted(entries.items()):
        candidate = manifest_path.parent / name
        digest = _sha256(_safe_read_file(candidate, maximum_bytes=MAX_FILE_BYTES))
        if digest != expected:
            raise HarnessError(f"bundle member hash differs: {name}")
        observed[name] = digest
    return {
        "manifest_sha256": manifest_sha256,
        "members": observed,
    }


def _safe_read_file(path: Path, *, maximum_bytes: int) -> bytes:
    try:
        listed = path.lstat()
    except OSError as error:
        raise HarnessError(f"required file is unavailable: {path}") from error
    if (
        stat.S_ISLNK(listed.st_mode)
        or not stat.S_ISREG(listed.st_mode)
        or listed.st_nlink != 1
        or listed.st_size > maximum_bytes
    ):
        raise HarnessError(f"file identity or size is unsafe: {path}")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags)
    try:
        current = os.fstat(descriptor)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or (current.st_dev, current.st_ino) != (listed.st_dev, listed.st_ino)
        ):
            raise HarnessError(f"file changed while opening: {path}")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(descriptor)
    payload = b"".join(chunks)
    if len(payload) != listed.st_size or len(payload) > maximum_bytes:
        raise HarnessError(f"bounded file read differs: {path}")
    return payload


def _safe_read_virtual(path: Path, *, maximum_bytes: int = 4_096) -> bytes:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(4_096, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(descriptor)
    payload = b"".join(chunks)
    if len(payload) > maximum_bytes:
        raise HarnessError(f"virtual file exceeds its read bound: {path}")
    return payload


def _run(
    command: Sequence[str],
    *,
    accepted: frozenset[int] = frozenset({0}),
    timeout: int = COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[bytes]:
    if (
        not command
        or command[0] not in {FINDMNT, SYSTEMCTL}
        or not 1 <= timeout <= WORKER_TIMEOUT_SECONDS
        or any(not argument or "\x00" in argument or len(argument) > 4_096 for argument in command)
    ):
        raise HarnessError("command differs from the closed allowlist")
    try:
        completed = subprocess.run(
            list(command),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=timeout,
            env={
                "LC_ALL": "C",
                "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                "PYTHONNOUSERSITE": "1",
            },
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HarnessError(f"bounded command failed: {command[0]}") from error
    if len(completed.stdout) > 65_536 or len(completed.stderr) > 65_536:
        raise HarnessError(f"command output exceeded bound: {command[0]}")
    if completed.returncode not in accepted:
        raise HarnessError(f"command exited unexpectedly: {command[0]}")
    return completed


def _parse_identity_file(payload: bytes) -> dict[str, str]:
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as error:
        raise HarnessError("storage identity is not ASCII") from error
    result: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line.startswith("#") or line.count("=") != 1:
            raise HarnessError("storage identity line differs")
        key, value = line.split("=", 1)
        if not key or not value or key in result:
            raise HarnessError("storage identity is malformed")
        result[key] = value
    required = {
        "DASHCAM_STORAGE_SCHEMA_VERSION",
        "DASHCAM_STORAGE_LAYOUT_VERSION",
        "DASHCAM_STORAGE_MOUNT",
        "DASHCAM_STORAGE_UUID",
        "DASHCAM_STORAGE_CID",
        "DASHCAM_STORAGE_SOURCE_MBR_SHA256",
        "DASHCAM_STORAGE_ROOT_END_SECTOR",
        "DASHCAM_STORAGE_DATA_START_SECTOR",
        "DASHCAM_STORAGE_DATA_END_SECTOR",
        "DASHCAM_STORAGE_MINIMUM_CAPACITY_BYTES",
    }
    if set(result) != required:
        raise HarnessError("storage identity keys differ")
    return result


def _device_id(path: Path) -> str:
    device = path.stat().st_dev
    major = cast(Callable[[int], int], getattr(os, "major"))  # noqa: B009
    minor = cast(Callable[[int], int], getattr(os, "minor"))  # noqa: B009
    return f"{major(device)}:{minor(device)}"


def _mount_row() -> dict[str, object]:
    completed = _run(
        (
            FINDMNT,
            "--json",
            "--mountpoint",
            str(RECORDING_ROOT),
            "--output",
            "TARGET,SOURCE,FSTYPE,LABEL,UUID,OPTIONS,MAJ:MIN",
        )
    )
    try:
        value = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HarnessError("findmnt returned malformed JSON") from error
    if not isinstance(value, dict) or set(value) != {"filesystems"}:
        raise HarnessError("findmnt result shape differs")
    rows = value["filesystems"]
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise HarnessError("findmnt did not return exactly one mount")
    row = cast(dict[str, object], rows[0])
    if set(row) != {"target", "source", "fstype", "label", "uuid", "options", "maj:min"}:
        raise HarnessError("findmnt row keys differ")
    if any(item is not None and not isinstance(item, str) for item in row.values()):
        raise HarnessError("findmnt row types differ")
    return row


def _service_state(*, expected_active: bool) -> dict[str, object]:
    completed = _run(
        (
            SYSTEMCTL,
            "show",
            "dashcamd.service",
            "--property=ActiveState,SubState,MainPID,NRestarts",
        ),
        accepted=frozenset({0}),
    )
    try:
        lines = completed.stdout.decode("ascii").splitlines()
    except UnicodeDecodeError as error:
        raise HarnessError("systemd returned non-ASCII service state") from error
    values: dict[str, str] = {}
    for line in lines:
        if line.count("=") != 1:
            raise HarnessError("systemd service state line differs")
        key, value = line.split("=", 1)
        if not key or not value or key in values:
            raise HarnessError("systemd service state shape differs")
        values[key] = value
    if set(values) != {"ActiveState", "SubState", "MainPID", "NRestarts"}:
        raise HarnessError("systemd service state shape differs")
    active = values["ActiveState"]
    substate = values["SubState"]
    main_pid_text = values["MainPID"]
    restarts_text = values["NRestarts"]
    try:
        main_pid = int(main_pid_text)
        restarts = int(restarts_text)
    except ValueError as error:
        raise HarnessError("systemd service counters are malformed") from error
    if restarts < 0 or main_pid < 0:
        raise HarnessError("systemd service counters are invalid")
    if expected_active:
        if active != "active" or substate != "running" or main_pid <= 0:
            raise HarnessError("dashcamd is not active and running")
    elif active != "inactive" or substate not in {"dead", "exited"} or main_pid != 0:
        raise HarnessError("dashcamd must be fully inactive")
    return {
        "active_state": active,
        "sub_state": substate,
        "main_pid": main_pid,
        "n_restarts": restarts,
    }


def require_live_prerequisites(*, expected_active: bool) -> dict[str, object]:
    if os.name != "posix" or not hasattr(os, "geteuid"):
        raise HarnessError("live harness requires Linux")
    pwd_module = importlib.import_module("pwd")
    getpwnam = cast(Callable[[str], object], getattr(pwd_module, "getpwnam"))  # noqa: B009
    getpwuid = cast(Callable[[int], object], getattr(pwd_module, "getpwuid"))  # noqa: B009
    account = getpwnam(EXPECTED_USER)
    account_uid = cast(int, getattr(account, "pw_uid"))  # noqa: B009
    current_name = cast(str, getattr(getpwuid(os.geteuid()), "pw_name"))  # noqa: B009
    if os.geteuid() != account_uid or current_name != EXPECTED_USER:
        raise HarnessError("live harness must run directly as dashcam, never root")
    for executable in (FINDMNT, SYSTEMCTL):
        if not Path(executable).is_file() or not os.access(executable, os.X_OK):
            raise HarnessError(f"required executable is unavailable: {executable}")
    for path in (RECORDING_ROOT, *tuple(RECORDING_ROOT / name for name in MANAGED_DIRECTORIES)):
        if path.is_symlink() or not path.is_dir():
            raise HarnessError(f"managed directory is unavailable or unsafe: {path}")
    if CATALOG_PATH.is_symlink() or not CATALOG_PATH.is_file():
        raise HarnessError("production catalog file is unavailable or unsafe")

    identity_payload = _safe_read_file(IDENTITY_PATH, maximum_bytes=8_192)
    identity = _parse_identity_file(identity_payload)
    sentinel_payload = _safe_read_file(SENTINEL_PATH, maximum_bytes=4_096)
    try:
        sentinel = json.loads(sentinel_payload.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HarnessError("recording sentinel is malformed") from error
    if canonical_json(sentinel) != sentinel_payload or not isinstance(sentinel, dict):
        raise HarnessError("recording sentinel is not canonical")
    row = _mount_row()
    options = str(row["options"]).split(",")
    cid = _safe_read_virtual(Path("/sys/class/block/mmcblk0/device/cid")).decode("ascii").strip()
    expected_sentinel = {
        "layout_version": int(identity["DASHCAM_STORAGE_LAYOUT_VERSION"]),
        "serial": identity["DASHCAM_STORAGE_CID"],
        "dashcam_uuid": identity["DASHCAM_STORAGE_UUID"],
        "source_table_fingerprint": identity["DASHCAM_STORAGE_SOURCE_MBR_SHA256"],
        "root_end_sector": int(identity["DASHCAM_STORAGE_ROOT_END_SECTOR"]),
        "data_start_sector": int(identity["DASHCAM_STORAGE_DATA_START_SECTOR"]),
        "data_end_sector": int(identity["DASHCAM_STORAGE_DATA_END_SECTOR"]),
    }
    if (
        identity["DASHCAM_STORAGE_SCHEMA_VERSION"] != "1"
        or identity["DASHCAM_STORAGE_MOUNT"] != str(RECORDING_ROOT)
        or identity["DASHCAM_STORAGE_CID"] != EXPECTED_CARD_CID
        or identity["DASHCAM_STORAGE_UUID"] != EXPECTED_VOLUME_UUID
        or cid != EXPECTED_CARD_CID
        or row["target"] != str(RECORDING_ROOT)
        or row["source"] != EXPECTED_SOURCE
        or row["fstype"] != EXPECTED_FILESYSTEM
        or row["label"] != EXPECTED_LABEL
        or row["uuid"] != EXPECTED_VOLUME_UUID
        or row["maj:min"] != _device_id(RECORDING_ROOT)
        or row["maj:min"] == _device_id(Path("/"))
        or "rw" not in options
        or "ro" in options
        or sentinel != expected_sentinel
    ):
        raise HarnessError("exact card, mount, UUID, label, CID, or sentinel gate differs")

    module_paths = (
        Path(cast(str, sys.modules["dashcam.catalog.database"].__file__)).resolve(),
        Path(cast(str, sys.modules["dashcam.recorder.finalizer"].__file__)).resolve(),
        Path(cast(str, sys.modules["dashcam.metadata.schema"].__file__)).resolve(),
    )
    if any("/opt/dashcam/releases/" not in module.as_posix() for module in module_paths):
        raise HarnessError("production modules are not loaded from the installed release")
    return {
        "cid": cid,
        "volume_uuid": EXPECTED_VOLUME_UUID,
        "mount_source": EXPECTED_SOURCE,
        "mount_device_id": _device_id(RECORDING_ROOT),
        "root_device_id": _device_id(Path("/")),
        "sentinel_sha256": _sha256(sentinel_payload),
        "identity_sha256": _sha256(identity_payload),
        "production_modules": [module.as_posix() for module in module_paths],
        "dashcamd": _service_state(expected_active=expected_active),
    }


def _managed_names(root: Path, directory: str) -> tuple[str, ...]:
    path = root / directory
    names: list[str] = []
    with os.scandir(path) as entries:
        for index, entry in enumerate(entries):
            if index == MAX_MANAGED_ENTRIES:
                raise HarnessError(f"{directory} exceeds the collision scan bound")
            names.append(entry.name)
    return tuple(sorted(names, key=str.casefold))


def read_kernel_boot_id(path: Path = BOOT_ID_PATH) -> UUID:
    payload = _safe_read_virtual(path, maximum_bytes=38)
    if len(payload) != 37 or not payload.endswith(b"\n"):
        raise HarnessError("kernel boot ID has an invalid exact shape")
    try:
        value = payload[:-1].decode("ascii")
        boot_id = UUID(value)
    except (UnicodeDecodeError, ValueError) as error:
        raise HarnessError("kernel boot ID is malformed") from error
    if str(boot_id) != value:
        raise HarnessError("kernel boot ID is not canonical")
    return boot_id


def verify_identity_absent(identity: TestIdentity, *, paths: HarnessPaths) -> None:
    expected_names = {
        PurePosixPath(value).name.casefold()
        for value in (
            identity.source_video,
            identity.source_sidecar,
            identity.target_video,
            identity.target_sidecar,
        )
    }
    for directory in MANAGED_DIRECTORIES:
        if any(
            name.casefold() in expected_names
            for name in _managed_names(paths.recording_root, directory)
        ):
            raise HarnessError("validation filename identity already exists")
    with ClipCatalog(paths.catalog_path) as catalog:
        try:
            catalog.get_clip(identity.clip_id)
        except ClipNotFoundError:
            pass
        else:
            raise HarnessError("validation clip identity already exists in the catalog")
        matching = _matching_intents(catalog, identity.clip_id)
        if matching:
            raise HarnessError("validation clip identity already has a durable intent")


def _matching_intents(catalog: ClipCatalog, clip_id: UUID) -> tuple[OperationIntent, ...]:
    intents = catalog.list_pending_intents(limit=MAX_PENDING_INTENTS + 1)
    if len(intents) > MAX_PENDING_INTENTS:
        raise HarnessError("pending intent scan exceeds its hard validation bound")
    return tuple(intent for intent in intents if intent.clip_id == clip_id)


def _collision_catalog_matches(
    catalog: ClipCatalog,
    identity: CollisionIdentity,
) -> tuple[tuple[CatalogClip, ...], tuple[OperationIntent, ...]]:
    related = {
        path.casefold()
        for path in (
            identity.source_video,
            identity.source_sidecar,
            identity.target_video,
            identity.target_sidecar,
            identity.sentinel_path,
        )
    }
    clips = catalog.list_clips(limit=MAX_MANAGED_ENTRIES + 1)
    if len(clips) > MAX_MANAGED_ENTRIES:
        raise HarnessError("catalog clip scan exceeds its hard validation bound")
    matching_clips = tuple(
        clip
        for clip in clips
        if clip.clip_id == identity.token
        or clip.video_path.casefold() in related
        or clip.sidecar_path.casefold() in related
    )
    intents = catalog.list_pending_intents(limit=MAX_PENDING_INTENTS + 1)
    if len(intents) > MAX_PENDING_INTENTS:
        raise HarnessError("pending intent scan exceeds its hard validation bound")

    def related_intent(intent: OperationIntent) -> bool:
        paths = intent.paths
        values = (
            paths.video_source,
            paths.sidecar_source,
            paths.video_target,
            paths.sidecar_target,
        )
        return intent.clip_id == identity.token or any(
            value is not None and value.casefold() in related for value in values
        )

    return matching_clips, tuple(intent for intent in intents if related_intent(intent))


def _collision_source_identity(path: Path, *, expected_device: int) -> dict[str, object]:
    with os.scandir(path.parent) as entries:
        matching: list[str] = []
        for index, entry in enumerate(entries):
            if index == MAX_MANAGED_ENTRIES:
                raise HarnessError(f"{path.parent.name} exceeds the collision scan bound")
            if entry.name.casefold() == path.name.casefold():
                matching.append(entry.name)
    if matching != [path.name]:
        raise HarnessError("expected pending MP4 is absent or case-ambiguous")
    try:
        before = path.lstat()
    except FileNotFoundError as error:
        raise HarnessError("expected current-boot pending MP4 is absent") from error
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_dev != expected_device
        or before.st_size <= 0
        or before.st_size > 2**40
    ):
        raise HarnessError("expected current-boot pending MP4 identity is unsafe")
    return {
        "device": before.st_dev,
        "inode": before.st_ino,
        "size_bytes": before.st_size,
    }


def _observed_exact_entry(
    path: Path,
    *,
    allowed_case_variants: frozenset[str] = frozenset(),
) -> dict[str, object]:
    with os.scandir(path.parent) as entries:
        matching: list[str] = []
        for index, entry in enumerate(entries):
            if index == MAX_MANAGED_ENTRIES:
                raise HarnessError(f"{path.parent.name} exceeds the collision scan bound")
            if entry.name.casefold() == path.name.casefold():
                matching.append(entry.name)
    if not matching:
        return {"exists": False, "size_bytes": None, "sha256": None}
    if matching != [path.name]:
        if frozenset(matching).issubset(allowed_case_variants):
            return {"exists": False, "size_bytes": None, "sha256": None}
        raise HarnessError(f"managed validation name is case-ambiguous: {path.name}")
    return _observed_file(path)


def inspect_collision(
    identity: CollisionIdentity,
    *,
    paths: HarnessPaths,
) -> dict[str, object]:
    source = _collision_source_identity(
        paths.recording_root / identity.source_video,
        expected_device=paths.recording_root.stat().st_dev,
    )
    files = {
        "source_sidecar": _observed_exact_entry(paths.recording_root / identity.source_sidecar),
        "target_video": _observed_exact_entry(paths.recording_root / identity.target_video),
        "target_sidecar": _observed_exact_entry(
            paths.recording_root / identity.target_sidecar,
            allowed_case_variants=frozenset({PurePosixPath(identity.sentinel_path).name}),
        ),
        "sentinel": _observed_exact_entry(
            paths.recording_root / identity.sentinel_path,
            allowed_case_variants=frozenset({PurePosixPath(identity.target_sidecar).name}),
        ),
    }
    with ClipCatalog(paths.catalog_path) as catalog:
        clips, intents = _collision_catalog_matches(catalog, identity)
    return {
        "source_video": source,
        "files": files,
        "clips": [_clip_mapping(clip) for clip in clips],
        "intents": [intent.as_dict() for intent in intents],
    }


def validate_collision_observation(
    expected_phase: str,
    identity: CollisionIdentity,
    observation: Mapping[str, object],
) -> None:
    if expected_phase not in {"before_sentinel", "sentinel_armed", "collision_refused"}:
        raise HarnessError("unknown collision observation phase")
    source = observation.get("source_video")
    files = observation.get("files")
    clips = observation.get("clips")
    intents = observation.get("intents")
    if (
        not isinstance(source, dict)
        or not isinstance(files, dict)
        or not isinstance(clips, list)
        or not isinstance(intents, list)
    ):
        raise HarnessError("collision observation shape differs")
    if (
        isinstance(source.get("device"), bool)
        or not isinstance(source.get("device"), int)
        or isinstance(source.get("inode"), bool)
        or not isinstance(source.get("inode"), int)
        or isinstance(source.get("size_bytes"), bool)
        or not isinstance(source.get("size_bytes"), int)
        or cast(int, source["size_bytes"]) <= 0
    ):
        raise HarnessError("pending MP4 observation is invalid")
    expected_payload = collision_sentinel(identity)
    sentinel_expected = expected_phase != "before_sentinel"
    expected_files = {
        "source_sidecar": (False, None),
        "target_video": (False, None),
        "target_sidecar": (False, None),
        "sentinel": (sentinel_expected, expected_payload if sentinel_expected else None),
    }
    for name, (exists, payload) in expected_files.items():
        item = files.get(name)
        if not isinstance(item, dict) or item.get("exists") is not exists:
            raise HarnessError(f"collision file existence differs: {name}")
        expected_size = None if payload is None else len(payload)
        expected_hash = None if payload is None else _sha256(payload)
        if item.get("size_bytes") != expected_size or item.get("sha256") != expected_hash:
            raise HarnessError(f"collision file identity differs: {name}")
    if clips or intents:
        raise HarnessError("collision namespace unexpectedly has a catalog row or intent")


def _write_collision_sentinel(
    identity: CollisionIdentity,
    *,
    paths: HarnessPaths,
) -> None:
    target = paths.recording_root / identity.sentinel_path
    payload = collision_sentinel(identity)
    descriptor = os.open(
        target,
        os.O_WRONLY
        | getattr(os, "O_BINARY", 0)
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o660,
    )
    try:
        written = os.write(descriptor, payload)
        if written != len(payload):
            raise HarnessError("short write creating collision sentinel")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_managed_directory(target.parent)


def _fsync_managed_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(
        directory,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def prepare_collision(
    identity: CollisionIdentity,
    *,
    paths: HarnessPaths,
) -> dict[str, object]:
    before = inspect_collision(identity, paths=paths)
    validate_collision_observation("before_sentinel", identity, before)
    before_source = cast(dict[str, object], before["source_video"])
    _write_collision_sentinel(identity, paths=paths)
    after = inspect_collision(identity, paths=paths)
    validate_collision_observation("sentinel_armed", identity, after)
    after_source = cast(dict[str, object], after["source_video"])
    if (
        before_source["device"] != after_source["device"]
        or before_source["inode"] != after_source["inode"]
        or cast(int, after_source["size_bytes"]) < cast(int, before_source["size_bytes"])
    ):
        raise HarnessError("active pending MP4 changed identity while arming collision")
    return after


def cleanup_collision_sentinel(
    identity: CollisionIdentity,
    *,
    paths: HarnessPaths,
) -> dict[str, object]:
    before = inspect_collision(identity, paths=paths)
    validate_collision_observation("collision_refused", identity, before)
    target = paths.recording_root / identity.sentinel_path
    expected = collision_sentinel(identity)
    listed = target.lstat()
    observed = _safe_read_file(target, maximum_bytes=MAX_FILE_BYTES)
    if (
        stat.S_ISLNK(listed.st_mode)
        or not stat.S_ISREG(listed.st_mode)
        or listed.st_nlink != 1
        or observed != expected
    ):
        raise HarnessError("collision sentinel changed before explicit cleanup")
    os.unlink(target)
    _fsync_managed_directory(target.parent)
    if target.exists() or target.is_symlink():
        raise HarnessError("collision sentinel removal did not persist")
    after = inspect_collision(identity, paths=paths)
    validate_collision_observation("before_sentinel", identity, after)
    return {
        "before_sha256": _sha256(observed),
        "removed_path": identity.sentinel_path,
        "source_video": after["source_video"],
        "sentinel_absent": True,
        "partial_pair_cleanup_performed": False,
    }


def _observed_file(path: Path) -> dict[str, object]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"exists": False, "size_bytes": None, "sha256": None}
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_size > MAX_FILE_BYTES
    ):
        raise HarnessError(f"managed validation path is unsafe: {path}")
    payload = _safe_read_file(path, maximum_bytes=MAX_FILE_BYTES)
    return {"exists": True, "size_bytes": len(payload), "sha256": _sha256(payload)}


def _stream_hash_regular_file(path: Path, *, maximum_bytes: int) -> dict[str, object]:
    listed = path.lstat()
    if (
        stat.S_ISLNK(listed.st_mode)
        or not stat.S_ISREG(listed.st_mode)
        or listed.st_nlink != 1
        or listed.st_size <= 0
        or listed.st_size > maximum_bytes
    ):
        raise HarnessError(f"recorded file identity or size is unsafe: {path}")
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    digest = hashlib.sha256()
    total = 0
    try:
        current = os.fstat(descriptor)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or (current.st_dev, current.st_ino, current.st_size)
            != (listed.st_dev, listed.st_ino, listed.st_size)
        ):
            raise HarnessError(f"recorded file changed while opening: {path}")
        while total <= maximum_bytes:
            chunk = os.read(descriptor, min(1_048_576, maximum_bytes + 1 - total))
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
    finally:
        os.close(descriptor)
    after = path.lstat()
    if (
        total != listed.st_size
        or total > maximum_bytes
        or (after.st_dev, after.st_ino, after.st_size)
        != (listed.st_dev, listed.st_ino, listed.st_size)
    ):
        raise HarnessError(f"recorded file changed during bounded hash: {path}")
    return {"exists": True, "size_bytes": total, "sha256": digest.hexdigest()}


def verify_recorded_pair(
    identity: RecordedIdentity,
    *,
    paths: HarnessPaths,
) -> dict[str, object]:
    for relative in (identity.source_video, identity.source_sidecar):
        observed = _observed_exact_entry(paths.recording_root / relative)
        if observed["exists"]:
            raise HarnessError("recorded pair still has a pending member")
    for relative in (identity.target_video, identity.target_sidecar):
        names = _managed_names(paths.recording_root, "clips")
        expected = PurePosixPath(relative).name
        matches = [name for name in names if name.casefold() == expected.casefold()]
        if matches != [expected]:
            raise HarnessError("recorded target is absent or case-ambiguous")

    video = _stream_hash_regular_file(
        paths.recording_root / identity.target_video,
        maximum_bytes=MAX_RECORDED_VIDEO_BYTES,
    )
    sidecar_payload = _safe_read_file(
        paths.recording_root / identity.target_sidecar,
        maximum_bytes=MAX_FILE_BYTES,
    )
    try:
        sidecar = parse_sidecar_bytes(sidecar_payload)
    except (ValueError, OSError) as error:
        raise HarnessError("recorded sidecar is not canonical bounded JSON") from error
    if (
        sidecar_payload != sidecar.to_canonical_json()
        or sidecar.video_file != PurePosixPath(identity.target_video).name
        or sidecar.metadata_file != PurePosixPath(identity.target_sidecar).name
        or sidecar.sequence != identity.sequence
        or sidecar.boot_id.hex[:12] != identity.short_boot_id
    ):
        raise HarnessError("recorded sidecar identity differs from its pair")

    with ClipCatalog(paths.catalog_path) as catalog:
        try:
            clip = catalog.get_clip(sidecar.clip_id)
        except ClipNotFoundError as error:
            raise HarnessError("recorded pair has no catalog row") from error
        intents = catalog.list_pending_intents(limit=MAX_PENDING_INTENTS + 1)
        if len(intents) > MAX_PENDING_INTENTS:
            raise HarnessError("pending intent scan exceeds its hard validation bound")
    related = {
        identity.source_video,
        identity.source_sidecar,
        identity.target_video,
        identity.target_sidecar,
    }
    matching_intents = [
        intent
        for intent in intents
        if intent.clip_id == sidecar.clip_id
        or any(
            value in related
            for value in (
                intent.paths.video_source,
                intent.paths.sidecar_source,
                intent.paths.video_target,
                intent.paths.sidecar_target,
            )
            if value is not None
        )
    ]
    if matching_intents:
        raise HarnessError("recorded pair still has a related pending intent")
    if (
        clip.lifecycle is not ClipLifecycle.FINALIZED
        or not clip.pair_reconciled
        or not clip.managed
        or clip.video_path != identity.target_video
        or clip.sidecar_path != identity.target_sidecar
        or clip.size_bytes != video["size_bytes"]
        or clip.start_monotonic_ns != sidecar.start_monotonic_ns
        or clip.end_monotonic_ns != sidecar.end_monotonic_ns
        or clip.protected != sidecar.protected
        or clip.protection_reason != sidecar.protection_reason
    ):
        raise HarnessError("recorded catalog row differs from the canonical pair")
    return {
        "video": video,
        "sidecar": {
            "exists": True,
            "size_bytes": len(sidecar_payload),
            "sha256": _sha256(sidecar_payload),
        },
        "clip": _clip_mapping(clip),
        "related_pending_intents": [],
        "pending_members_absent": True,
        "ffprobe_or_decode_performed": False,
    }


def _clip_mapping(clip: CatalogClip) -> dict[str, object]:
    return {
        "clip_id": str(clip.clip_id),
        "lifecycle": clip.lifecycle.value,
        "video_path": clip.video_path,
        "sidecar_path": clip.sidecar_path,
        "start_monotonic_ns": clip.start_monotonic_ns,
        "end_monotonic_ns": clip.end_monotonic_ns,
        "retention_order": clip.retention_order,
        "size_bytes": clip.size_bytes,
        "protected": clip.protected,
        "protection_reason": clip.protection_reason,
        "pair_reconciled": clip.pair_reconciled,
        "managed": clip.managed,
        "download_lease": clip.download_lease is not None,
    }


def inspect_identity(identity: TestIdentity, *, paths: HarnessPaths) -> dict[str, object]:
    files = {
        name: _observed_file(paths.recording_root / relative)
        for name, relative in (
            ("source_video", identity.source_video),
            ("source_sidecar", identity.source_sidecar),
            ("target_video", identity.target_video),
            ("target_sidecar", identity.target_sidecar),
        )
    }
    with ClipCatalog(paths.catalog_path) as catalog:
        try:
            clip_value: dict[str, object] | None = _clip_mapping(catalog.get_clip(identity.clip_id))
        except ClipNotFoundError:
            clip_value = None
        matching = _matching_intents(catalog, identity.clip_id)
    return {
        "files": files,
        "clip": clip_value,
        "intents": [intent.as_dict() for intent in matching],
    }


def validate_observation(
    expected_phase: str,
    identity: TestIdentity,
    observation: Mapping[str, object],
) -> None:
    if expected_phase not in {"pre_crash", "post_crash", "recovered"}:
        raise HarnessError("unknown observation phase")
    files = observation.get("files")
    clip = observation.get("clip")
    intents = observation.get("intents")
    if not isinstance(files, dict) or not isinstance(intents, list):
        raise HarnessError("observation has an invalid shape")

    mp4_payload = synthetic_mp4(identity)
    sidecar_payload = build_sidecar(identity).to_canonical_json()
    expected_file_states = {
        "pre_crash": {
            "source_video": (True, mp4_payload),
            "source_sidecar": (False, None),
            "target_video": (False, None),
            "target_sidecar": (False, None),
        },
        "post_crash": {
            "source_video": (False, None),
            "source_sidecar": (True, sidecar_payload),
            "target_video": (True, mp4_payload),
            "target_sidecar": (False, None),
        },
        "recovered": {
            "source_video": (False, None),
            "source_sidecar": (False, None),
            "target_video": (True, mp4_payload),
            "target_sidecar": (True, sidecar_payload),
        },
    }[expected_phase]
    for name, (exists, payload) in expected_file_states.items():
        item = files.get(name)
        if not isinstance(item, dict) or item.get("exists") is not exists:
            raise HarnessError(f"{expected_phase} file existence differs: {name}")
        expected_size = None if payload is None else len(payload)
        expected_hash = None if payload is None else _sha256(payload)
        if item.get("size_bytes") != expected_size or item.get("sha256") != expected_hash:
            raise HarnessError(f"{expected_phase} file identity differs: {name}")

    if expected_phase == "pre_crash":
        if clip is not None or intents:
            raise HarnessError("pre-crash catalog identity is not absent")
        return
    if not isinstance(clip, dict):
        raise HarnessError(f"{expected_phase} clip row is absent")
    expected_paths = (
        (identity.source_video, identity.source_sidecar)
        if expected_phase == "post_crash"
        else (identity.target_video, identity.target_sidecar)
    )
    expected_lifecycle = (
        ClipLifecycle.FINALIZING.value
        if expected_phase == "post_crash"
        else ClipLifecycle.FINALIZED.value
    )
    exact_clip = {
        "clip_id": str(identity.clip_id),
        "lifecycle": expected_lifecycle,
        "video_path": expected_paths[0],
        "sidecar_path": expected_paths[1],
        "start_monotonic_ns": identity.start_monotonic_ns,
        "end_monotonic_ns": identity.end_monotonic_ns,
        "size_bytes": len(mp4_payload),
        "protected": False,
        "protection_reason": None,
        "pair_reconciled": expected_phase == "recovered",
        "managed": True,
        "download_lease": False,
    }
    for key, expected in exact_clip.items():
        if clip.get(key) != expected:
            raise HarnessError(f"{expected_phase} catalog field differs: {key}")
    retention_order = clip.get("retention_order")
    if (
        isinstance(retention_order, bool)
        or not isinstance(retention_order, int)
        or retention_order < 0
    ):
        raise HarnessError("catalog retention order is invalid")

    if expected_phase == "recovered":
        if intents:
            raise HarnessError("recovered pair still has a durable intent")
        return
    if len(intents) != 1 or not isinstance(intents[0], dict):
        raise HarnessError("post-crash state requires exactly one durable intent")
    intent = cast(dict[str, object], intents[0])
    if (
        intent.get("clip_id") != str(identity.clip_id)
        or intent.get("kind") != IntentKind.FINALIZE.value
        or intent.get("paths")
        != {
            "video_source": identity.source_video,
            "sidecar_source": identity.source_sidecar,
            "video_target": identity.target_video,
            "sidecar_target": identity.target_sidecar,
        }
    ):
        raise HarnessError("post-crash FINALIZE intent differs")


def _write_source(identity: TestIdentity, *, paths: HarnessPaths) -> None:
    target = paths.recording_root / identity.source_video
    payload = synthetic_mp4(identity)
    flags = (
        os.O_WRONLY
        | getattr(os, "O_BINARY", 0)
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(target, flags, 0o660)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise HarnessError("short write creating synthetic MP4")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if os.name != "nt":
        directory_descriptor = os.open(
            target.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)


def prepare(identity: TestIdentity, *, paths: HarnessPaths) -> dict[str, object]:
    verify_identity_absent(identity, paths=paths)
    _write_source(identity, paths=paths)
    observation = inspect_identity(identity, paths=paths)
    validate_observation("pre_crash", identity, observation)
    return observation


def crash_worker(
    identity: TestIdentity,
    *,
    paths: HarnessPaths,
    expected_device_id: str,
    crash: Callable[[], None] | None = None,
) -> None:
    observation = inspect_identity(identity, paths=paths)
    validate_observation("pre_crash", identity, observation)
    filesystem = CrashAfterFirstMoveFilesystem(
        paths.recording_root,
        expected_device_id=expected_device_id,
        crash=crash,
    )
    with ClipCatalog(paths.catalog_path) as catalog:
        finalizer = RecorderClipFinalizer(
            catalog=catalog,
            filesystem=filesystem,
            monotonic_ns=time.monotonic_ns,
        )
        finalizer.finalize(
            provisional_video_name=PurePosixPath(identity.source_video).name,
            sidecar=build_sidecar(identity),
            retention_order=finalizer.next_retention_order(),
        )
    raise HarnessError("crash worker returned without SIGKILL")


def recover_locally_for_test(identity: TestIdentity, *, paths: HarnessPaths) -> None:
    """Use the production recovery path in local tests; never called by live CLI."""

    filesystem = DurableRootedFinalizationFilesystem(paths.recording_root)
    with ClipCatalog(paths.catalog_path) as catalog:
        report = RecorderClipFinalizer(
            catalog=catalog,
            filesystem=filesystem,
            monotonic_ns=time.monotonic_ns,
        ).reconcile_pending()
    if report.completed != 1 or report.more_work:
        raise HarnessError("local production recovery did not complete exactly one intent")


def _output_document(
    phase: str,
    identity: TestIdentity | CollisionIdentity | RecordedIdentity,
    *,
    bundle: Mapping[str, object],
    prerequisites: Mapping[str, object],
    observation: Mapping[str, object],
) -> dict[str, object]:
    body = {
        "schema_version": HARNESS_SCHEMA_VERSION,
        "harness": HARNESS_NAME,
        "phase": phase,
        "identity": identity.as_dict(),
        "bundle": dict(bundle),
        "prerequisites": dict(prerequisites),
        "observation": dict(observation),
        "automatic_cleanup_performed": False,
        "catalog_replaced": False,
        "manual_pair_move_or_delete_performed": False,
    }
    return {**body, "evidence_sha256": _sha256(canonical_json(body))}


def _parse_uuid(value: str) -> UUID:
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("identity must be a canonical UUIDv4") from error
    if str(parsed) != value or parsed.version != 4:
        raise argparse.ArgumentTypeError("identity must be a canonical lowercase UUIDv4")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--expected-manifest-sha256",
        required=True,
        help="reviewed SHA-256 of this payload's SHA256SUMS",
    )
    subparsers = parser.add_subparsers(dest="phase", required=True)
    for phase in ("prepare", "inject-crash", "inspect-post-crash", "verify-recovered"):
        child = subparsers.add_parser(phase)
        child.add_argument("--identity", required=True, type=_parse_uuid)
    for phase in ("prepare-collision", "inspect-collision", "cleanup-collision-sentinel"):
        child = subparsers.add_parser(phase)
        child.add_argument("--identity", required=True, type=_parse_uuid)
        child.add_argument("--sequence", required=True, type=int)
    recorded = subparsers.add_parser("verify-recorded")
    recorded.add_argument("--boot-id", required=True)
    recorded.add_argument("--sequence", required=True, type=int)
    worker = subparsers.add_parser("_crash-worker", help=argparse.SUPPRESS)
    worker.add_argument("--identity", required=True, type=_parse_uuid)
    return parser


def _spawn_crash_worker(
    identity: TestIdentity,
    *,
    expected_manifest_sha256: str,
) -> None:
    command = (
        sys.executable,
        str(Path(__file__).resolve()),
        "--expected-manifest-sha256",
        expected_manifest_sha256,
        "_crash-worker",
        "--identity",
        str(identity.clip_id),
    )
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=WORKER_TIMEOUT_SECONDS,
            env={
                "LC_ALL": "C",
                "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                "PYTHONNOUSERSITE": "1",
            },
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HarnessError("bounded crash worker failed") from error
    if len(completed.stdout) > 4_096 or len(completed.stderr) > 4_096:
        raise HarnessError("crash worker emitted excessive output")
    if completed.returncode != -SIGKILL_NUMBER:
        diagnostic = (completed.stderr or completed.stdout)[:512].decode(
            "utf-8", errors="backslashreplace"
        )
        raise HarnessError(
            f"worker did not terminate by SIGKILL ({completed.returncode}): "
            f"{' | '.join(diagnostic.splitlines())}"
        )


def _validate_manifest_argument(value: str) -> str:
    lowered = value.lower()
    if len(lowered) != 64 or any(character not in "0123456789abcdef" for character in lowered):
        raise HarnessError("expected manifest SHA-256 is malformed")
    return lowered


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        manifest_hash = _validate_manifest_argument(arguments.expected_manifest_sha256)
        bundle = verify_bundle(expected_manifest_sha256=manifest_hash)
        if arguments.phase == "_crash-worker":
            identity = derive_test_identity(arguments.identity)
            prerequisites = require_live_prerequisites(expected_active=False)
            crash_worker(
                identity,
                paths=HarnessPaths(),
                expected_device_id=cast(str, prerequisites["mount_device_id"]),
            )
            raise HarnessError("unreachable crash worker return")

        collision_phase = arguments.phase in {
            "prepare-collision",
            "inspect-collision",
            "cleanup-collision-sentinel",
        }
        recorded_phase = arguments.phase == "verify-recorded"
        expected_active = arguments.phase in {"verify-recovered", "prepare-collision"}
        prerequisites = require_live_prerequisites(expected_active=expected_active)
        paths = HarnessPaths()
        if recorded_phase:
            recorded_identity = derive_recorded_identity(
                arguments.boot_id,
                arguments.sequence,
            )
            observation = verify_recorded_pair(recorded_identity, paths=paths)
            output_phase = "recorded_pair_verified"
            output_identity: TestIdentity | CollisionIdentity | RecordedIdentity = recorded_identity
        elif collision_phase:
            collision_identity = derive_collision_identity(
                arguments.identity,
                read_kernel_boot_id(),
                arguments.sequence,
            )
            if arguments.phase == "prepare-collision":
                observation = prepare_collision(collision_identity, paths=paths)
                output_phase = "collision_sentinel_armed"
            elif arguments.phase == "inspect-collision":
                observation = inspect_collision(collision_identity, paths=paths)
                validate_collision_observation(
                    "collision_refused",
                    collision_identity,
                    observation,
                )
                output_phase = "collision_refused"
            elif arguments.phase == "cleanup-collision-sentinel":
                observation = cleanup_collision_sentinel(collision_identity, paths=paths)
                output_phase = "collision_sentinel_removed"
            else:
                raise HarnessError("unknown collision phase")
            output_identity = collision_identity
        else:
            identity = derive_test_identity(arguments.identity)
            output_identity = identity
        if arguments.phase == "prepare":
            observation = prepare(identity, paths=paths)
            output_phase = "pre_crash"
        elif arguments.phase == "inject-crash":
            existing = inspect_identity(identity, paths=paths)
            validate_observation("pre_crash", identity, existing)
            _spawn_crash_worker(identity, expected_manifest_sha256=manifest_hash)
            observation = inspect_identity(identity, paths=paths)
            validate_observation("post_crash", identity, observation)
            output_phase = "post_crash"
        elif arguments.phase == "inspect-post-crash":
            observation = inspect_identity(identity, paths=paths)
            validate_observation("post_crash", identity, observation)
            output_phase = "post_crash"
        elif arguments.phase == "verify-recovered":
            deadline = time.monotonic() + RECOVERY_TIMEOUT_SECONDS
            while True:
                observation = inspect_identity(identity, paths=paths)
                try:
                    validate_observation("recovered", identity, observation)
                    break
                except HarnessError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.25)
            output_phase = "recovered"
        elif not collision_phase and not recorded_phase:
            raise HarnessError("unknown phase")
        document = _output_document(
            output_phase,
            output_identity,
            bundle=bundle,
            prerequisites=prerequisites,
            observation=observation,
        )
        sys.stdout.buffer.write(canonical_json(document))
        return 0
    except (HarnessError, FinalizationRefused, OSError, ValueError) as error:
        failure = {
            "schema_version": HARNESS_SCHEMA_VERSION,
            "harness": HARNESS_NAME,
            "status": "refused",
            "error_type": type(error).__name__,
            "detail": str(error)[:1_024],
        }
        sys.stderr.buffer.write(canonical_json(failure))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
