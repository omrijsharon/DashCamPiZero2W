#!/usr/bin/env python3
"""Hash-closed exact-Pi M10 private-runtime qualification.

The root parent owns disposable host-visible loop mounts. Every recorder or
rollback process runs in a transient systemd mount namespace in which those
mounts cover /srv/dashcam, /var/lib/dashcam and /run/dashcam.  The ordinary
recorder is excluded by an owned runtime drop-in for the complete qualification and its production
catalog/media paths are observed read-only only for before/after identity.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import math
import os
import re
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Final, cast
from uuid import UUID, uuid4

SCHEMA_VERSION: Final = 1
EXPECTED_CANDIDATE: Final = "efc16c651511f7d64428c26dc874cb32d663ac42"
EXPECTED_ROLLBACK: Final = "051f98a70039a448ce0b3475617b399429d5a023"
EXPECTED_API_GID: Final = 983
EXPECTED_RELEASE: Final = "0.1.0.dev0-5f95dd806342ac9e"
EXPECTED_INTERPRETER: Final = Path(f"/opt/dashcam/releases/{EXPECTED_RELEASE}/venv/bin/python")
EXPECTED_ROOT_SOURCE: Final = "/dev/mmcblk0p2"
EXPECTED_BOARD_MODEL: Final = "Raspberry Pi Zero 2 W Rev 1.0"

EXFAT_IMAGE_BYTES: Final = 416 * 1024**2
EXT4_IMAGE_BYTES: Final = 48 * 1024**2
ROOT_BOUNDED_OVERHEAD_BYTES: Final = 64 * 1024**2
ROOT_PRESERVED_FREE_BYTES: Final = 2 * 1024**3
ROOT_REQUIRED_FREE_BYTES: Final = (
    EXFAT_IMAGE_BYTES + EXT4_IMAGE_BYTES + ROOT_BOUNDED_OVERHEAD_BYTES + ROOT_PRESERVED_FREE_BYTES
)
MAX_NON_IMAGE_ROOT_DELTA_BYTES: Final = ROOT_BOUNDED_OVERHEAD_BYTES
MAX_BUNDLE_FILE_BYTES: Final = 24 * 1024 * 1024
MAX_RESULT_BYTES: Final = 128 * 1024
MAX_COMMAND_OUTPUT: Final = 1024 * 1024
MAX_SOURCE_MEMBERS: Final = 768
MAX_FIXTURE_ROWS: Final = 512
MAX_FILLER_BYTES: Final = 320 * 1024**2
FILLER_CHUNK_BYTES: Final = 8 * 1024**2
LIVE_FILLER_CHUNK_BYTES: Final = 1 * 1024**2
MAX_FILLER_STEPS: Final = MAX_FILLER_BYTES // LIVE_FILLER_CHUNK_BYTES + 1
PHASE_TIMEOUT_S: Final = 420
QUALIFICATION_TIMEOUT_S: Final = 900
UNIT_START_TIMEOUT_S: Final = 48
UNIT_STOP_TIMEOUT_S: Final = 35
ROLLBACK_TIMEOUT_S: Final = 60
SYSTEMD_RUN_CLIENT_TIMEOUT_S: Final = 20.0
SYSTEMD_RUN_NOTIFY_TIMEOUT_S: Final = 50.0
OBSERVATION_INTERVAL_S: Final = 0.1
CONTROL_RESPONSE_BYTES: Final = 16 * 1024
CONTROL_TIMEOUT_S: Final = 9.0
RUNTIME_STATUS_BYTES: Final = 32 * 1024
RECORDER_STATES: Final = ("STARTING", "RECORDING", "DEGRADED", "STOPPING", "FAULTED")
RECORDER_REASONS: Final = (
    "CONFIG_ERROR",
    "STARTUP_FAILED",
    "STARTUP_TIMEOUT",
    "RUNTIME_EXITED",
    "RUNTIME_FAILED",
    "PIPELINE_RECOVERING",
    "PIPELINE_RECOVERY_EXHAUSTED",
    "PIPELINE_NO_PROGRESS",
    "FINALIZATION_FAILED",
    "STORAGE_FAULT",
    "OPTIONAL_SUBSYSTEM",
    "SHUTDOWN_FAILED",
    "SHUTDOWN_TIMEOUT",
)
PREFLIGHT_STATES: Final = (
    "UNKNOWN",
    "CHECKING",
    "READY",
    "LOW_SPACE",
    "EMERGENCY",
    "READ_ONLY",
    "FAULTED",
)
PREFLIGHT_REASONS: Final = (
    "MALFORMED_FACTS",
    "WRONG_TARGET",
    "UNMOUNTED",
    "MISSING_MOUNT_IDENTITY",
    "ROOTFS_ALIAS",
    "WRONG_FILESYSTEM",
    "WRONG_LABEL",
    "WRONG_UUID",
    "WRONG_UUID_SUFFIX",
    "READ_ONLY",
    "CONFLICTING_MOUNT_OPTIONS",
    "MISSING_SENTINEL",
    "WRONG_SENTINEL_VERSION",
    "WRONG_SENTINEL_IDENTITY",
    "WRONG_SENTINEL_UUID",
    "INVALID_SENTINEL_GEOMETRY",
    "INVALID_SPACE",
    "INSUFFICIENT_CAPACITY",
    "RESERVE_EXHAUSTED",
    "WRITE_PROBE_FAILED",
)
SPACE_OBSERVATION_FAULTS: Final = (
    "OBSERVATION_FAILED",
    "INVALID_OBSERVATION",
    "OBSERVATION_STALE",
    "IDENTITY_DRIFT",
    "CAPACITY_DRIFT",
    "LATCH_LOAD_FAILED",
    "LATCH_BINDING_MISMATCH",
    "LATCH_STORE_FAILED",
    "NO_SPACE_WRITE",
)
RETENTION_MODES: Final = ("NORMAL", "RECLAIMING", "EMERGENCY")
PREFLIGHT_STATUS_KEYS: Final = frozenset(
    {"state", "reasons", "ready", "mount", "free_bytes", "capacity_bytes"}
)
RETENTION_STATUS_KEYS: Final = frozenset(
    {
        "sequence",
        "mode",
        "fault",
        "trigger",
        "stale",
        "stop_required",
        "reclaimer_enabled",
        "consecutive_observation_failures",
        "sample_age_ns",
        "volume_uuid_suffix",
        "device_id",
        "capacity_bytes",
        "free_bytes",
        "free_percent",
        "thresholds",
        "directive",
    }
)
FIXTURE_FINALIZING_SIDECAR: Final = PurePosixPath(
    "pending/boot-m10private-000022.partial.json"
)

LOW_PERCENT: Final = 30
HIGH_PERCENT: Final = 35
MINIMUM_FREE_GIB: Final = 0.1
EMERGENCY_FREE_MIB: Final = 16
STARTUP_DELETE_BUDGET: Final = 64
PRIVATE_MINIMUM_CAPACITY_BYTES: Final = 128 * 1024 * 1024
PREFLIGHT_DIAGNOSTIC_STAGES: Final = frozenset(
    {
        "IMPORT",
        "CONFIG",
        "IDENTITY",
        "POLICY",
        "COLLECT_ROOT",
        "FINDMNT_EXEC",
        "FINDMNT_BOUND",
        "FINDMNT_RETURN",
        "FINDMNT_JSON",
        "FINDMNT_ROOT",
        "FINDMNT_ROWS",
        "FINDMNT_ROW",
        "FINDMNT_DIFFERING",
        "COLLECT_TARGET",
        "COLLECT_SOURCE",
        "COLLECT_FIELDS",
        "COLLECT_DIRECTORY",
        "COLLECT_DEVICE",
        "COLLECT_RELATION",
        "COLLECT_SPACE",
        "COLLECT_SENTINEL",
        "PARSE",
        "FILESYSTEM",
        "RUN",
        "RESULT",
    }
)

RECORDING_LABEL: Final = "DASHCAM"
CATALOG_LABEL: Final = "M10STATE"
CONTROL_SOCKET: Final = Path("/run/dashcam/control.sock")
STATUS_PATH: Final = Path("/run/dashcam/status.json")
EXCLUSION_DIRECTORY: Final = Path("/run/systemd/system/dashcamd.service.d")
EXCLUSION_FILE_NAME: Final = "99-m10-exclusion.conf"
CATALOG_PATH: Final = Path("/var/lib/dashcam/catalog.sqlite3")
CONFIG_PATH: Final = Path("/var/lib/dashcam/config.toml")
IDENTITY_PATH: Final = Path("/var/lib/dashcam/storage-volume.env")
LAUNCHER_PATH: Final = Path("/var/lib/dashcam/source-launcher.py")
CANDIDATE_ARCHIVE_PATH: Final = Path("/var/lib/dashcam/candidate-source.zip")
ROLLBACK_ARCHIVE_PATH: Final = Path("/var/lib/dashcam/rollback-source.zip")
REAL_FINDMNT_PATH: Final = Path("/usr/libexec/dashcam-m10-findmnt-real")
LIVE_LOCKS: Final = (
    Path("/run/lock/dashcam-live-qualification.lock"),
    Path("/run/dashcam-m10-retention-loop.lock"),
)

COMMIT_RE: Final = re.compile(r"[0-9a-f]{40}")
SHA256_RE: Final = re.compile(r"[0-9a-f]{64}")
LOOP_RE: Final = re.compile(r"/dev/loop[0-9]{1,4}")
REFUSAL_TOKEN_PATTERN: Final = (
    rb"H_(HARNESS|SQLITE|OS|UNICODE|ZIP|VALUE|ASSERT|ATTRIBUTE|KEY|RUNTIME|"
    rb"TYPE|EXCEPTION)_F([a-z][a-z0-9_]*)_L([1-9][0-9]{0,3})"
)
REFUSAL_LOCATION_RE: Final = re.compile(rb"REFUSED: " + REFUSAL_TOKEN_PATTERN + rb"\n")
FIXTURE_CHILD_REFUSAL_RE: Final = re.compile(
    rb"REFUSED: H_FIXTURE_CHILD child=(" + REFUSAL_TOKEN_PATTERN + rb")\n"
)
MAX_REVIEWED_RUN_LINE: Final = 8192
DIAGNOSTIC_FUNCTIONS: Final = (
    "main",
    "qualify",
    "validate_pi",
    "host_snapshot",
    "qualification_locks",
    "runtime_exclusion",
    "require_excluded",
    "freeze_bundle",
    "require_root_identity",
    "findmnt",
    "service_properties",
    "create_runtime_exclusion",
    "remove_owned_runtime_exclusion",
    "write_recovery_journal",
    "transition_recovery_journal",
    "mount_fixture",
    "cleanup_fixture",
    "discard_fixture",
    "install_private_state",
    "wait_unit_terminal",
    "wait_recording",
    "remove_unit",
    "run_bind_probe",
    "source_environment",
    "fixture_subprocess",
    "seed_fixture",
    "query_catalog",
    "managed_manifest",
    "allocate_filler",
    "remove_filler",
    "listener_identity",
    "phase_a_launch_failure_status",
    "run_preflight_diagnostic",
    "candidate_unit",
    "stop_clean",
    "wait_writing",
    "wait_delete_progress",
    "wait_reclaim_quiescent",
    "canonical_media_row",
    "wait_event_media",
    "media_evidence",
    "runtime_health",
    "phase_a",
    "rollback_phase",
    "protected_emergency_phase",
    "startup_bound_phase",
    "fresh_phase",
    "remove_frozen",
    "same_production",
    "publish_result",
    "require_dense_image",
    "require_owned_loop",
    "require_mount",
    "attach",
    "detach",
    "unmount",
    "blkid",
)
UNIT_RE: Final = re.compile(
    r"dashcam-m10-private-[a-z0-9]{12}-(?:bind|preflight|a|rollback[0123]|b|c)\.service"
)
NONCE_RE: Final = re.compile(r"dashcam-m10-private\.[a-z0-9]{12}")
RESULT_RE: Final = re.compile(r"m10-private-runtime-[0-9a-f]{12}\.json")
MANIFEST_MEMBERS: Final = frozenset(
    {
        "README.md",
        "run.py",
        "BUNDLE.json",
        "CANDIDATE_SOURCE.json",
        "ROLLBACK_SOURCE.json",
        "candidate-source.zip",
        "rollback-source.zip",
    }
)


class HarnessError(RuntimeError):
    """The qualification cannot safely establish one required fact."""


class FixtureChildRefusal(HarnessError):
    """One isolated fixture child emitted an exact reviewed refusal token."""

    def __init__(self, child_refusal: bytes) -> None:
        super().__init__("isolated fixture child safely refused")
        self.child_refusal = child_refusal


_qualification_deadline_ns: int | None = None


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")


def _bounded_read(path: Path, maximum: int) -> bytes:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size > maximum
        ):
            raise HarnessError(f"unsafe or oversized file: {path.name}")
        chunks = bytearray()
        while len(chunks) <= maximum:
            chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        payload = bytes(chunks)
        if len(payload) != metadata.st_size:
            raise HarnessError(f"short bounded read: {path.name}")
        return payload
    finally:
        os.close(descriptor)


def _bounded_virtual_read(path: Path, maximum: int) -> bytes:
    """Read one declared procfs/sysfs regular file whose st_size is not authoritative."""

    if maximum <= 0:
        raise HarnessError("virtual file read bound is invalid")
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise HarnessError(f"virtual file type differs: {path.name}")
        chunks = bytearray()
        while len(chunks) <= maximum:
            chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        if not chunks:
            raise HarnessError(f"virtual file made no progress: {path.name}")
        if len(chunks) > maximum:
            raise HarnessError(f"virtual file exceeded its bound: {path.name}")
        return bytes(chunks)
    finally:
        os.close(descriptor)


def _read_boot_id() -> str:
    payload = _bounded_virtual_read(Path("/proc/sys/kernel/random/boot_id"), 37)
    try:
        value = payload.decode("ascii")
    except UnicodeDecodeError as error:
        raise HarnessError("boot ID is not ASCII") from error
    if not value.endswith("\n") or value.count("\n") != 1:
        raise HarnessError("boot ID virtual-file shape differs")
    value = value.removesuffix("\n")
    try:
        canonical = str(UUID(value))
    except ValueError as error:
        raise HarnessError("boot ID is not canonical UUID text") from error
    if canonical != value:
        raise HarnessError("boot ID is not canonical UUID text")
    return value


def _read_board_model() -> str:
    payload = _bounded_virtual_read(Path("/proc/device-tree/model"), 256)
    if not payload.endswith(b"\0") or b"\0" in payload[:-1]:
        raise HarnessError("board model virtual-file shape differs")
    try:
        return payload[:-1].decode("ascii")
    except UnicodeDecodeError as error:
        raise HarnessError("board model is not ASCII") from error


def _read_cpu_serial() -> str:
    payload = _bounded_virtual_read(Path("/proc/cpuinfo"), 256 * 1024)
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as error:
        raise HarnessError("CPU information is not ASCII") from error
    if "\0" in text or not text.endswith("\n"):
        raise HarnessError("CPU information virtual-file shape differs")
    serials: list[str] = []
    for line in text.splitlines():
        match = re.fullmatch(r"Serial[ \t]*:[ \t]*([0-9a-f]{16})", line)
        if match is not None:
            serials.append(match.group(1))
    if len(serials) != 1:
        raise HarnessError("CPU serial record count differs")
    return serials[0]


def _strict_json(payload: bytes, label: str) -> dict[str, object]:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise HarnessError(f"{label} contains a duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(payload, object_pairs_hook=unique)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise HarnessError(f"{label} is not strict JSON") from error
    if not isinstance(value, dict):
        raise HarnessError(f"{label} must be a JSON object")
    return cast(dict[str, object], value)


def _parse_manifest(payload: bytes) -> dict[str, str]:
    rows: dict[str, str] = {}
    for line in payload.decode("ascii").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_.-]{1,64})", line)
        if match is None or match.group(2) in rows:
            raise HarnessError("SHA256SUMS is not canonical")
        rows[match.group(2)] = match.group(1)
    if set(rows) != MANIFEST_MEMBERS:
        raise HarnessError("bundle manifest member set differs")
    return rows


def _verify_source(
    bundle: Path,
    metadata_name: str,
    archive_name: str,
    expected_commit: str,
) -> dict[str, object]:
    metadata = _strict_json(
        _bounded_read(bundle / metadata_name, MAX_BUNDLE_FILE_BYTES), metadata_name
    )
    if set(metadata) != {
        "schema_version",
        "git_commit",
        "git_tree",
        "archive_name",
        "archive_sha256",
        "archive_size",
        "members",
    }:
        raise HarnessError(f"{metadata_name} fields differ")
    if (
        metadata["schema_version"] != 1
        or metadata["git_commit"] != expected_commit
        or metadata["archive_name"] != archive_name
        or not isinstance(metadata["git_tree"], str)
        or COMMIT_RE.fullmatch(metadata["git_tree"]) is None
    ):
        raise HarnessError(f"{metadata_name} identity differs")
    archive = _bounded_read(bundle / archive_name, MAX_BUNDLE_FILE_BYTES)
    if metadata["archive_sha256"] != _sha256(archive) or metadata["archive_size"] != len(archive):
        raise HarnessError(f"{archive_name} digest or size differs")
    members = metadata["members"]
    if not isinstance(members, dict) or not 1 <= len(members) <= MAX_SOURCE_MEMBERS:
        raise HarnessError(f"{archive_name} member facts differ")
    with zipfile.ZipFile(io.BytesIO(archive), "r") as source:
        if source.comment or len(source.infolist()) != len(members):
            raise HarnessError(f"{archive_name} ZIP shape differs")
        observed: set[str] = set()
        for item in source.infolist():
            path = PurePosixPath(item.filename)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not item.filename.startswith("dashcam/")
                or item.compress_type != zipfile.ZIP_STORED
                or item.is_dir()
                or item.filename in observed
            ):
                raise HarnessError(f"{archive_name} contains an unsafe member")
            observed.add(item.filename)
            fact = members.get(item.filename)
            if not isinstance(fact, dict) or set(fact) != {"sha256", "size"}:
                raise HarnessError(f"{archive_name} member metadata differs")
            payload = source.read(item)
            if fact["sha256"] != _sha256(payload) or fact["size"] != len(payload):
                raise HarnessError(f"{archive_name} member digest differs")
        if observed != set(members):
            raise HarnessError(f"{archive_name} member set differs")
    return metadata


def verify_bundle(
    bundle: Path,
    expected_manifest_sha256: str,
    expected_harness: str,
    expected_candidate: str = EXPECTED_CANDIDATE,
    expected_rollback: str = EXPECTED_ROLLBACK,
) -> dict[str, object]:
    bundle = bundle.resolve(strict=True)
    if (
        not bundle.is_dir()
        or bundle.is_symlink()
        or SHA256_RE.fullmatch(expected_manifest_sha256) is None
    ):
        raise HarnessError("bundle path or expected manifest digest is invalid")
    manifest_payload = _bounded_read(bundle / "SHA256SUMS", 4096)
    if _sha256(manifest_payload) != expected_manifest_sha256:
        raise HarnessError("bundle manifest digest differs")
    manifest = _parse_manifest(manifest_payload)
    entries = {entry.name for entry in bundle.iterdir()}
    if entries != MANIFEST_MEMBERS | {"SHA256SUMS"}:
        raise HarnessError("bundle directory member set differs")
    for name, digest in manifest.items():
        if _sha256(_bounded_read(bundle / name, MAX_BUNDLE_FILE_BYTES)) != digest:
            raise HarnessError(f"bundle member digest differs: {name}")
    bundle_metadata = _strict_json(_bounded_read(bundle / "BUNDLE.json", 4096), "BUNDLE.json")
    if set(bundle_metadata) != {
        "schema_version",
        "harness_commit",
        "harness_tree",
        "candidate_commit",
        "candidate_tree",
        "rollback_commit",
        "rollback_tree",
    }:
        raise HarnessError("bundle provenance fields differ")
    if (
        bundle_metadata["schema_version"] != 1
        or bundle_metadata["harness_commit"] != expected_harness
        or bundle_metadata["candidate_commit"] != expected_candidate
        or bundle_metadata["rollback_commit"] != expected_rollback
        or any(
            not isinstance(bundle_metadata[key], str)
            or COMMIT_RE.fullmatch(cast(str, bundle_metadata[key])) is None
            for key in ("harness_tree", "candidate_tree", "rollback_tree")
        )
    ):
        raise HarnessError("bundle provenance identity differs")
    candidate = _verify_source(
        bundle, "CANDIDATE_SOURCE.json", "candidate-source.zip", expected_candidate
    )
    rollback = _verify_source(
        bundle, "ROLLBACK_SOURCE.json", "rollback-source.zip", expected_rollback
    )
    return {
        "bundle": bundle_metadata,
        "candidate": candidate,
        "rollback": rollback,
        "manifest": manifest,
    }


def root_budget_satisfied(free_bytes: int) -> bool:
    return (
        isinstance(free_bytes, int)
        and not isinstance(free_bytes, bool)
        and free_bytes >= ROOT_REQUIRED_FREE_BYTES
    )


def resolved_thresholds(capacity_bytes: int) -> tuple[int, int, int]:
    if (
        isinstance(capacity_bytes, bool)
        or not isinstance(capacity_bytes, int)
        or capacity_bytes <= 0
    ):
        raise ValueError("capacity must be positive")
    minimum = math.ceil(MINIMUM_FREE_GIB * 1024**3)
    low = max((capacity_bytes * LOW_PERCENT + 99) // 100, minimum)
    high = max((capacity_bytes * HIGH_PERCENT + 99) // 100, minimum)
    emergency = EMERGENCY_FREE_MIB * 1024**2
    if not emergency < minimum <= low < high < capacity_bytes:
        raise HarnessError("private fixture thresholds do not resolve safely")
    return low, high, emergency


def _config(*, rollback: bool) -> bytes:
    lease = "" if rollback else "download_lease_timeout_s = 300\n"
    device_match = (
        "usb:vid=08bb,pid=2902,product=USB_PnP_Sound_Device,path=platform-3f980000.usb-usb-0:1:1.0"
    )
    return f"""schema_version = 1
device_name = "Dashcam-M10-Private"

[video]
width = 1920
height = 1080
fps = 30
codec = "h264"
hardware_encoder_required = true
bitrate_bps = 8000000
keyframe_interval_frames = 30
clip_duration_s = 60
container = "mp4"

[audio]
enabled = false
device_match = "{device_match}"
sample_rate_hz = 48000
channels = 1
codec = "aac"
bitrate_bps = 128000

[gps]
device = "/dev/dashcam-gps-deliberately-absent"
baud = 115200
stale_after_s = 2.0
max_sample_hz = 10
anchor_earliest_utc = "2024-01-01T00:00:00Z"
anchor_latest_utc = "2100-01-01T00:00:00Z"
anchor_uncertainty_ms = 250
anchor_max_conflict_ms = 2000
anchor_max_reacquire_disagreement_ms = 5000
anchor_max_interval_s = 86400

[time]
timezone = "Asia/Jerusalem"
filename_timezone = "UTC"
discipline_system_clock = false
system_clock_owner = "systemd-timesyncd"

[overlay]
enabled = true
show_local_datetime = true
show_utc_offset = true
show_rec = true
show_speed = true
speed_unit = "kmh"
show_coordinates = true
coordinate_decimals = 5
show_altitude = true
show_satellites = true
show_hdop = false

[preview]
enabled = true
width = 640
height = 360
fps = 15
max_clients = 1
latency_target_ms = 500

[storage]
recording_root = "/srv/dashcam"
required_filesystem = "exfat"
required_volume_label = "DASHCAM"
require_distinct_mount = true
low_watermark_percent = {LOW_PERCENT}
high_watermark_percent = {HIGH_PERCENT}
minimum_free_gib = {MINIMUM_FREE_GIB}
emergency_free_mib = {EMERGENCY_FREE_MIB}
{lease}protect_previous_clips = 2
protect_next_clips = 1

[network]
ap_enabled = true
ssid_prefix = "Dashcam"
address = "192.168.50.1/24"

[service]
watchdog_s = 20
restart_backoff_min_s = 1
restart_backoff_max_s = 60
""".encode("ascii")


LAUNCHER = b"""#!/usr/bin/env python3
import argparse, os, runpy, sys
p=argparse.ArgumentParser()
p.add_argument("--archive", required=True)
p.add_argument("--module", required=True)
p.add_argument("--output")
a,arguments=p.parse_known_args()
if a.output:
 f=os.open(a.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
 sys.stdout=os.fdopen(f,"w",encoding="ascii",closefd=True)
sys.path.insert(0,a.archive)
sys.argv=[a.module,*arguments]
runpy.run_module(a.module,run_name="__main__",alter_sys=True)
"""

PREFLIGHT_DIAGNOSTIC = b"""#!/usr/bin/env python3
import json,os,subprocess,sys
out="/run/dashcam/preflight-diagnostic.json"
def emit(stage):
 payload=(json.dumps({"schema_version":1,"stage":stage},sort_keys=True,separators=(",",":"))+"\\n").encode("ascii")
 fd=os.open(out,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
 try:
  view=memoryview(payload)
  while view:
   count=os.write(fd,view)
   if count<=0: raise RuntimeError("diagnostic write made no progress")
   view=view[count:]
  os.fsync(fd)
 finally: os.close(fd)
 directory=os.open("/run/dashcam",os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
 try: os.fsync(directory)
 finally: os.close(directory)
def attempt(stage,operation):
 try: return operation()
 except Exception:
  emit(stage);raise SystemExit(0)
sys.path.insert(0,"/var/lib/dashcam/candidate-source.zip")
def imports():
 from dashcam.config import load_config
 from dashcam.storage import preflight
 return load_config,preflight
load_config,p=attempt("IMPORT",imports)
config=attempt("CONFIG",lambda:load_config("/var/lib/dashcam/config.toml"))
identity=attempt("IDENTITY",lambda:p.load_storage_identity("/var/lib/dashcam/storage-volume.env"))
policy=attempt("POLICY",lambda:p.policy_from_identity(config,identity))
collector=p.PosixFactsCollector()
root_id=attempt("COLLECT_ROOT",lambda:p._directory_device_id("/"))
command=("/usr/bin/findmnt","--json","--mountpoint",policy.recording_root,"--output","TARGET,SOURCE,FSTYPE,LABEL,UUID,OPTIONS,MAJ:MIN")
result=attempt(
 "FINDMNT_EXEC",
 lambda:subprocess.run(command,check=False,stdin=subprocess.DEVNULL,capture_output=True,timeout=p.COMMAND_TIMEOUT_SECONDS,env={"LC_ALL":"C","PATH":"/usr/sbin:/usr/bin:/sbin:/bin"}),
)
if len(result.stdout)>p.MAX_FINDMNT_BYTES or len(result.stderr)>p.MAX_FINDMNT_BYTES:
 emit("FINDMNT_BOUND");raise SystemExit(0)
if result.returncode==1 and not result.stdout and not result.stderr:
 row=None
else:
 if result.returncode!=0:
  emit("FINDMNT_RETURN");raise SystemExit(0)
 raw_findmnt=attempt(
  "FINDMNT_JSON",
  lambda:json.loads(result.stdout.decode("utf-8"),object_pairs_hook=p._unique_json_object),
 )
 if not isinstance(raw_findmnt,dict) or set(raw_findmnt)!={"filesystems"}:
  emit("FINDMNT_ROOT");raise SystemExit(0)
 rows=raw_findmnt["filesystems"]
 if not isinstance(rows,list) or not rows or len(rows)>p.MAX_FINDMNT_ROWS:
  emit("FINDMNT_ROWS");raise SystemExit(0)
 parsed_rows=attempt(
  "FINDMNT_ROW",
  lambda:tuple(p._parse_findmnt_row(value,target=policy.recording_root) for value in rows),
 )
 row=parsed_rows[0]
 if any(value!=row for value in parsed_rows[1:]):
  emit("FINDMNT_DIFFERING");raise SystemExit(0)
if row is None:
 raw={"mount":{"target":policy.recording_root,"mounted":False,"source":None,"filesystem":None,"label":None,"uuid":None,"mount_options":[],"device_id":None,"os_root_device_id":root_id},"space":{"capacity_bytes":None,"free_bytes":None},"sentinel":None}
else:
 target=attempt("COLLECT_TARGET",lambda:p._required_findmnt_string(row,"target"))
 source=attempt(
  "COLLECT_SOURCE",
  lambda:p._canonical_block_source(p._required_findmnt_string(row,"source")),
 )
 def fields():
  filesystem=p._optional_findmnt_string(row,"fstype")
  label=p._optional_findmnt_string(row,"label")
  uuid=p._optional_findmnt_string(row,"uuid")
  options_text=p._required_findmnt_string(row,"options")
  options=options_text.split(",") if options_text else []
  if len(options)>p.MAX_MOUNT_OPTIONS: raise RuntimeError("options bound")
  return filesystem,label,uuid,options
 filesystem_name,label,uuid,options=attempt("COLLECT_FIELDS",fields)
 device_id=attempt(
  "COLLECT_DIRECTORY",
  lambda:p._directory_device_id(policy.recording_root),
 )
 observed_device=attempt(
  "COLLECT_DEVICE",
  lambda:p._required_findmnt_string(row,"maj:min"),
 )
 if target!=policy.recording_root or observed_device!=device_id:
  emit("COLLECT_RELATION");raise SystemExit(0)
 def space():
  observed=os.statvfs(policy.recording_root)
  return (
   p._checked_product(observed.f_blocks,observed.f_frsize),
   p._checked_product(observed.f_bavail,observed.f_frsize),
  )
 capacity,free=attempt("COLLECT_SPACE",space)
 sentinel=attempt(
  "COLLECT_SENTINEL",
  lambda:p._read_canonical_sentinel(policy.recording_root,device_id),
 )
 raw={"mount":{"target":target,"mounted":True,"source":source,"filesystem":filesystem_name,"label":label,"uuid":uuid,"mount_options":options,"device_id":device_id,"os_root_device_id":root_id},"space":{"capacity_bytes":capacity,"free_bytes":free},"sentinel":sentinel}
parsed=attempt("PARSE",lambda:p.recording_root_facts_from_mapping(raw))
filesystem=attempt("FILESYSTEM",lambda:p.PosixPreflightFilesystem(
 recording_root=policy.recording_root,
 expected_device_id=parsed.mount.device_id or "0:0",
))
attempt("RUN",lambda:p.run_storage_preflight(raw,policy=policy,filesystem=filesystem))
emit("RESULT")
"""


def render_transient_properties(
    *,
    recording_source: Path,
    state_source: Path,
    runtime_source: Path,
    role: str,
) -> tuple[str, ...]:
    if role not in {
        "candidate",
        "rollback-recovery",
        "rollback-recorder",
        "bind",
        "preflight-diagnostic",
    }:
        raise HarnessError("transient service role differs")
    sources = tuple(path.as_posix() for path in (recording_source, state_source, runtime_source))
    if any(
        not value.startswith("/var/tmp/dashcam-m10-private.")
        and not value.startswith("/run/dashcam-m10-private.")
        for value in sources
    ):
        raise HarnessError("transient bind source is outside the nonce roots")
    if role in {"rollback-recovery", "bind", "preflight-diagnostic"}:
        groups = "dashcam-storage"
    elif role == "rollback-recorder":
        groups = "video render dialout dashcam-storage"
    else:
        groups = "audio video render dialout dashcam-storage dashcam-api"
    camera = role in {"candidate", "rollback-recorder"}
    needs_devices = camera or role == "preflight-diagnostic"
    bind_paths = (
        f"BindPaths={sources[0]}:/srv/dashcam "
        f"{sources[1]}:/var/lib/dashcam {sources[2]}:/run/dashcam"
    )
    adapter_source = recording_source.parent / "bundle" / "run.py"
    findmnt_adapter = (
        f"BindReadOnlyPaths=/usr/bin/findmnt:{REAL_FINDMNT_PATH.as_posix()} "
        f"{adapter_source.as_posix()}:/usr/bin/findmnt"
    )
    properties = [
        "User=dashcam",
        "Group=dashcam",
        f"SupplementaryGroups={groups}",
        "Type=notify" if camera else "Type=oneshot",
        "Restart=on-failure" if camera else "Restart=no",
        "TimeoutStartSec=45s",
        "TimeoutStopSec=30s",
        "RuntimeMaxSec=420s" if camera else "RuntimeMaxSec=90s",
        "UMask=0027",
        "NoNewPrivileges=yes",
        "CapabilityBoundingSet=",
        "AmbientCapabilities=",
        "PrivateTmp=yes",
        "PrivateDevices=no" if needs_devices else "PrivateDevices=yes",
        "DevicePolicy=auto" if needs_devices else "DevicePolicy=closed",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "ProtectKernelLogs=yes",
        "ProtectControlGroups=yes",
        "ProtectClock=yes",
        "RestrictSUIDSGID=yes",
        "RestrictNamespaces=yes",
        "LockPersonality=yes",
        "MemoryDenyWriteExecute=yes",
        "RestrictAddressFamilies=AF_UNIX",
        bind_paths,
        findmnt_adapter,
        "WorkingDirectory=/var/lib/dashcam",
    ]
    if camera:
        properties.extend(("NotifyAccess=main", "WatchdogSec=20s"))
    else:
        properties.append("ProtectProc=invisible")
    if any(
        value.startswith("StateDirectory=") or value.startswith("RuntimeDirectory=")
        for value in properties
    ):
        raise AssertionError("private harness must not request production directories")
    return tuple(properties)


def validate_clean_safety_stop(properties: Mapping[str, str]) -> None:
    expected = {
        "ActiveState": "inactive",
        "SubState": "dead",
        "Result": "success",
        "ExecMainCode": "1",
        "ExecMainStatus": "0",
        "NRestarts": "0",
    }
    if any(properties.get(key) != value for key, value in expected.items()):
        raise HarnessError("clean pre-camera storage safety-stop unit outcome differs")


def validate_writing_interval(before: Mapping[str, object], after: Mapping[str, object]) -> None:
    clip_id = before.get("clip_id")
    if (
        not isinstance(clip_id, str)
        or before.get("lifecycle") != "WRITING"
        or after.get("clip_id") != clip_id
        or after.get("lifecycle") != "WRITING"
        or before.get("video_present") is not True
        or after.get("video_present") is not True
        or after.get("delete_intent") is not False
    ):
        raise HarnessError("active clip was not preserved for its WRITING interval")


def validate_startup_delete_bound(snapshot: Mapping[str, object]) -> None:
    if (
        snapshot.get("delete_complete") != STARTUP_DELETE_BUDGET
        or snapshot.get("delete_pending") != 1
        or snapshot.get("camera_opened") is not False
        or snapshot.get("listener_present") is not False
        or snapshot.get("catalog_worker_count") != 0
    ):
        raise HarnessError("startup deletion budget evidence differs")


def _validate_c_exact_transition(
    fixture: Mapping[str, object], snapshot: Mapping[str, object], root: Path
) -> None:
    oracle = fixture.get("pending_delete_ids")
    if not isinstance(oracle, list) or len(oracle) != STARTUP_DELETE_BUDGET + 1:
        raise HarnessError("startup DELETE fixture oracle differs")
    clips = {row["clip_id"]: row for row in cast(list[dict[str, object]], snapshot["clips"])}
    intents = {row["intent_id"]: row for row in cast(list[dict[str, object]], snapshot["intents"])}
    if len(clips) != len(oracle) or len(intents) != len(oracle):
        raise HarnessError("startup DELETE transition introduced an extra row")
    for index, raw in enumerate(oracle):
        if not isinstance(raw, dict):
            raise HarnessError("startup DELETE oracle row differs")
        clip_id, intent_id = raw.get("clip_id"), raw.get("intent_id")
        if not isinstance(clip_id, str) or not isinstance(intent_id, str):
            raise HarnessError("startup DELETE oracle identity differs")
        clip, intent = clips.get(clip_id), intents.get(intent_id)
        if clip is None or intent is None or intent.get("clip_id") != clip_id:
            raise HarnessError("startup DELETE transition identity differs")
        video_path, sidecar_path = raw.get("video_path"), raw.get("sidecar_path")
        if not isinstance(video_path, str) or not isinstance(sidecar_path, str):
            raise HarnessError("startup DELETE member oracle differs")
        video, sidecar = root / PurePosixPath(video_path), root / PurePosixPath(sidecar_path)
        if index < STARTUP_DELETE_BUDGET:
            if (
                intent.get("status") != "COMPLETE"
                or clip.get("lifecycle") != "DELETED"
                or video.exists()
                or video.is_symlink()
                or sidecar.exists()
                or sidecar.is_symlink()
            ):
                raise HarnessError("one of the first 64 DELETE intents did not complete exactly")
        elif (
            intent.get("status") != "PENDING"
            or clip.get("lifecycle") != "DELETING"
            or not video.is_file()
            or video.is_symlink()
            or not sidecar.is_file()
            or sidecar.is_symlink()
            or _sha256(_bounded_read(video, 8192)) != raw.get("video_sha256")
            or _sha256(_bounded_read(sidecar, 8192)) != raw.get("sidecar_sha256")
        ):
            raise HarnessError("the 65th DELETE intent/member state differs")


def _command(
    arguments: Sequence[str | Path],
    *,
    timeout: float = 30,
    allowed: frozenset[int] = frozenset({0}),
    safe_fixture_refusal: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    if _qualification_deadline_ns is not None:
        remaining = (_qualification_deadline_ns - time.monotonic_ns()) / 1_000_000_000
        if remaining <= 0:
            raise HarnessError("qualification exceeded its global deadline")
        timeout = min(timeout, max(0.1, remaining))
    command = [str(value) for value in arguments]
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=timeout,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HarnessError(f"bounded command failed: {Path(command[0]).name}") from error
    if len(result.stdout) > MAX_COMMAND_OUTPUT or len(result.stderr) > MAX_COMMAND_OUTPUT:
        raise HarnessError("command output exceeded its bound")
    if result.returncode not in allowed:
        if (
            safe_fixture_refusal
            and result.returncode == 2
            and result.stdout == b""
            and _validated_refusal_location(result.stderr) == result.stderr
        ):
            raise FixtureChildRefusal(result.stderr)
        raise HarnessError(f"command refused: {Path(command[0]).name}")
    return result


def _write_exclusive(path: Path, payload: bytes, mode: int, *, uid: int = 0, gid: int = 0) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise HarnessError("exclusive write made no progress")
            view = view[written:]
        fchown = getattr(os, "fchown", None)
        if callable(fchown):
            fchown(descriptor, uid, gid)
        elif os.name == "posix":
            raise HarnessError("descriptor ownership control is unavailable")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_exfat_sentinel_exclusive(
    path: Path,
    payload: bytes,
    *,
    dashcam_uid: int,
    storage_gid: int,
) -> None:
    """Durably create the fixed-ownership exFAT sentinel without chown/chmod."""

    if path.name != ".dashcam-volume":
        raise HarnessError("exFAT sentinel path differs")
    parent_descriptor = os.open(
        path.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        parent_metadata = os.fstat(parent_descriptor)
        if (
            not stat.S_ISDIR(parent_metadata.st_mode)
            or parent_metadata.st_uid != dashcam_uid
            or parent_metadata.st_gid != storage_gid
            or stat.S_IMODE(parent_metadata.st_mode) != 0o750
        ):
            raise HarnessError("exFAT sentinel parent identity differs")
        descriptor = os.open(
            path.name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o640,
            dir_fd=parent_descriptor,
        )
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise HarnessError("exFAT sentinel write made no progress")
                view = view[written:]
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != dashcam_uid
                or metadata.st_gid != storage_gid
                or stat.S_IMODE(metadata.st_mode) != 0o640
                or metadata.st_nlink != 1
                or metadata.st_dev != parent_metadata.st_dev
                or metadata.st_size != len(payload)
            ):
                raise HarnessError("exFAT sentinel effective identity differs")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)


def _fixture_storage_identity() -> tuple[int, int]:
    """Resolve the fixed exFAT mount identity independently in the fixture child."""

    import grp
    import pwd

    if sys.platform != "linux" or os.geteuid() != 0:
        raise HarnessError("fixture storage identity requires Linux root")
    dashcam = pwd.getpwnam("dashcam")
    storage = grp.getgrnam("dashcam-storage")
    if (
        dashcam.pw_name != "dashcam"
        or storage.gr_name != "dashcam-storage"
        or isinstance(dashcam.pw_uid, bool)
        or isinstance(storage.gr_gid, bool)
        or dashcam.pw_uid <= 0
        or storage.gr_gid <= 0
    ):
        raise HarnessError("fixture storage account identity differs")
    return dashcam.pw_uid, storage.gr_gid


def _write_exfat_pending_finalizing_sidecar(
    root: Path,
    relative: str,
    payload: bytes,
    *,
    dashcam_uid: int,
    storage_gid: int,
) -> None:
    """Durably create the one generated pending sidecar without chown/chmod."""

    requested = PurePosixPath(relative)
    root_path = root.as_posix()
    if (
        relative != FIXTURE_FINALIZING_SIDECAR.as_posix()
        or requested != FIXTURE_FINALIZING_SIDECAR
        or re.fullmatch(
            r"/var/tmp/dashcam-m10-private\.[a-z0-9]{12}/recording",
            root_path,
        )
        is None
    ):
        raise HarnessError("fixture FINALIZING sidecar path differs")
    root_descriptor = os.open(
        root,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        root_metadata = os.fstat(root_descriptor)
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or root_metadata.st_uid != dashcam_uid
            or root_metadata.st_gid != storage_gid
            or stat.S_IMODE(root_metadata.st_mode) != 0o750
        ):
            raise HarnessError("fixture recording root identity differs")
        pending_descriptor = os.open(
            "pending",
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_descriptor,
        )
        try:
            pending_metadata = os.fstat(pending_descriptor)
            if (
                not stat.S_ISDIR(pending_metadata.st_mode)
                or pending_metadata.st_uid != dashcam_uid
                or pending_metadata.st_gid != storage_gid
                or stat.S_IMODE(pending_metadata.st_mode) != 0o750
                or pending_metadata.st_dev != root_metadata.st_dev
            ):
                raise HarnessError("fixture pending directory identity differs")
            descriptor = os.open(
                requested.name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o640,
                dir_fd=pending_descriptor,
            )
            try:
                view = memoryview(payload)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise HarnessError("fixture FINALIZING sidecar write made no progress")
                    view = view[written:]
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != dashcam_uid
                    or metadata.st_gid != storage_gid
                    or stat.S_IMODE(metadata.st_mode) != 0o640
                    or metadata.st_nlink != 1
                    or metadata.st_dev != root_metadata.st_dev
                    or metadata.st_size != len(payload)
                ):
                    raise HarnessError("fixture FINALIZING sidecar identity differs")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.fsync(pending_descriptor)
        finally:
            os.close(pending_descriptor)
    finally:
        os.close(root_descriptor)


def _validate_result_destination(path: Path) -> Path:
    parent = path.parent
    parent_metadata = os.lstat(parent)
    root_device = os.lstat("/").st_dev
    if (
        parent != Path("/var/tmp")
        or RESULT_RE.fullmatch(path.name) is None
        or not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_uid != 0
        or parent_metadata.st_dev != root_device
        or stat.S_IMODE(parent_metadata.st_mode) != 0o1777
        or path.exists()
        or path.is_symlink()
    ):
        raise HarnessError("result destination identity differs")
    return path


def _publish_result(path: Path, payload: bytes) -> None:
    path = _validate_result_destination(path)
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        _write_exclusive(temporary, payload, 0o600)
    except BaseException:
        with contextlib.suppress(Exception):
            metadata = os.lstat(temporary)
            if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                temporary.unlink()
        raise
    try:
        directory = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except BaseException:
        temporary.unlink()
        raise
    linked = False
    target_identity: tuple[int, int] | None = None
    try:
        source = os.stat(temporary.name, dir_fd=directory, follow_symlinks=False)
        target_identity = (source.st_dev, source.st_ino)
        os.link(
            temporary.name,
            path.name,
            src_dir_fd=directory,
            dst_dir_fd=directory,
            follow_symlinks=False,
        )
        linked = True
        os.fsync(directory)
        os.unlink(temporary.name, dir_fd=directory)
        os.fsync(directory)
    except BaseException:
        if linked:
            with contextlib.suppress(Exception):
                target = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                if (target.st_dev, target.st_ino) == target_identity:
                    os.unlink(path.name, dir_fd=directory)
        with contextlib.suppress(Exception):
            os.unlink(temporary.name, dir_fd=directory)
        with contextlib.suppress(Exception):
            os.fsync(directory)
        raise
    finally:
        os.close(directory)


def _space(path: Path) -> tuple[int, int]:
    facts = os.statvfs(path)  # type: ignore[attr-defined]
    return facts.f_blocks * facts.f_frsize, facts.f_bavail * facts.f_frsize


class _RootSpaceSampler:
    def __init__(self, initial_free: int) -> None:
        self.minimum_free = initial_free
        self._stop = threading.Event()
        self._failure: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="m10-root-space-sampler",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> int:
        self._stop.set()
        self._thread.join(timeout=2)
        if self._thread.is_alive():
            raise HarnessError("root-space sampler did not join")
        if self._failure is not None:
            raise HarnessError("root-space sampler observation failed") from self._failure
        return self.minimum_free

    def _run(self) -> None:
        try:
            while not self._stop.wait(0.1):
                self.minimum_free = min(self.minimum_free, _require_root_identity())
        except BaseException as error:
            self._failure = error
            self._stop.set()


class _CameraAbsenceObserver:
    def __init__(self, catalog: Path, root: Path, runtime: Path) -> None:
        self._catalog = catalog
        self._root = root
        self._runtime = runtime
        self._stop = threading.Event()
        self._failure: BaseException | None = None
        self._thread = threading.Thread(target=self._run, name="m10-no-camera-observer")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3)
        if self._thread.is_alive():
            raise HarnessError("camera-absence observer did not join")
        if self._failure is not None:
            raise HarnessError("camera-absence observation failed") from self._failure

    def _run(self) -> None:
        try:
            while not self._stop.wait(0.02):
                snapshot = _query_catalog(self._catalog, self._root)
                if any(
                    row.get("lifecycle") == "WRITING"
                    for row in cast(list[dict[str, object]], snapshot["clips"])
                ):
                    raise HarnessError("camera opened a durable WRITING clip")
                socket_path = self._runtime / "control.sock"
                if socket_path.exists() or socket_path.is_symlink():
                    raise HarnessError("listener appeared during pre-camera refusal")
                status_path = self._runtime / "status.json"
                if status_path.is_file() and not status_path.is_symlink():
                    status = _strict_json(_bounded_read(status_path, 128 * 1024), "status")
                    lifecycle = status.get("lifecycle")
                    if isinstance(lifecycle, dict) and lifecycle.get("state") in {
                        "RECORDING",
                        "DEGRADED",
                    }:
                        raise HarnessError("recorder published a camera-active state")
        except BaseException as error:
            self._failure = error
            self._stop.set()


def _device_id(path: Path) -> str:
    device = path.stat().st_dev
    return f"{os.major(device)}:{os.minor(device)}"  # type: ignore[attr-defined]


def _block_device_id(path: Path) -> str:
    metadata = path.stat()
    if not stat.S_ISBLK(metadata.st_mode):
        raise HarnessError("loop path is not a block device")
    return f"{os.major(metadata.st_rdev)}:{os.minor(metadata.st_rdev)}"  # type: ignore[attr-defined]


def _require_root_identity(*, minimum_free: int = ROOT_PRESERVED_FREE_BYTES) -> int:
    row = _findmnt(Path("/"))
    root_metadata = os.lstat("/")
    temporary_metadata = os.lstat("/var/tmp")
    if (
        row.get("source") != EXPECTED_ROOT_SOURCE
        or row.get("fstype") != "ext4"
        or row.get("target") != "/"
        or not stat.S_ISDIR(root_metadata.st_mode)
        or not stat.S_ISDIR(temporary_metadata.st_mode)
        or root_metadata.st_dev != temporary_metadata.st_dev
        or temporary_metadata.st_uid != 0
    ):
        raise HarnessError("root or /var/tmp identity differs")
    _capacity, free = _space(Path("/"))
    if free < minimum_free:
        raise HarnessError("root reserve gate refused a mutation")
    return free


def _findmnt_adapter(argv: Sequence[str]) -> int:
    expected = (
        "--json",
        "--mountpoint",
        "/srv/dashcam",
        "--output",
        "TARGET,SOURCE,FSTYPE,LABEL,UUID,OPTIONS,MAJ:MIN",
    )
    if tuple(argv) != expected:
        return 2
    try:
        result = subprocess.run(
            (REAL_FINDMNT_PATH, *expected),
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=3,
            env={"LC_ALL": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
        )
        if (
            result.returncode != 0
            or result.stderr
            or len(result.stdout) > 16 * 1024
        ):
            return 2
        duplicates = False

        def pairs(pairs_value: list[tuple[str, object]]) -> dict[str, object]:
            nonlocal duplicates
            value: dict[str, object] = {}
            for key, item in pairs_value:
                if key in value:
                    duplicates = True
                value[key] = item
            return value

        document = json.loads(result.stdout.decode("utf-8"), object_pairs_hook=pairs)
        rows = (
            document.get("filesystems")
            if isinstance(document, dict) and set(document) == {"filesystems"}
            else None
        )
        if duplicates or not isinstance(rows, list) or not 1 <= len(rows) <= 2:
            return 2
        expected_keys = {"target", "source", "fstype", "label", "uuid", "options", "maj:min"}
        if any(not isinstance(row, dict) or set(row) != expected_keys for row in rows):
            return 2
        metadata = os.stat("/srv/dashcam")
        major = cast(Callable[[int], int], getattr(os, "major"))  # noqa: B009
        minor = cast(Callable[[int], int], getattr(os, "minor"))  # noqa: B009
        device_id = f"{major(metadata.st_dev)}:{minor(metadata.st_dev)}"
        matches = [
            row
            for row in rows
            if row.get("target") == "/srv/dashcam" and row.get("maj:min") == device_id
        ]
        if len(matches) != 1:
            return 2
        sys.stdout.buffer.write(canonical_json({"filesystems": matches}))
        sys.stdout.buffer.flush()
        return 0
    except Exception:
        return 2


def _findmnt(path: Path) -> dict[str, object]:
    result = _command(
        (
            "/usr/bin/findmnt",
            "--json",
            "--output",
            "TARGET,SOURCE,FSTYPE,LABEL,UUID,OPTIONS,MAJ:MIN",
            "--target",
            path,
        ),
    )
    value = _strict_json(result.stdout, "findmnt")
    rows = value.get("filesystems")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise HarnessError("findmnt returned an unexpected shape")
    return cast(dict[str, object], rows[0])


def _service_properties(unit: str) -> dict[str, str]:
    result = _command(
        (
            "/usr/bin/systemctl",
            "show",
            unit,
            "--property=LoadState,ActiveState,SubState,Result,ExecMainCode,"
            "ExecMainStatus,NRestarts,MainPID,UnitFileState,ControlGroup,"
            "FragmentPath,DropInPaths,RefuseManualStart,Conditions",
        )
    )
    values: dict[str, str] = {}
    for line in result.stdout.decode("ascii").splitlines():
        if line.count("=") != 1:
            raise HarnessError("systemd property output differs")
        key, value = line.split("=", 1)
        values[key] = value
    return values


def _host_snapshot() -> dict[str, object]:
    catalog = Path("/var/lib/dashcam/catalog.sqlite3")
    sentinel = Path("/srv/dashcam/.dashcam-volume")
    return {
        "root": _findmnt(Path("/")),
        "recording": _findmnt(Path("/srv/dashcam")),
        "dashcamd": _service_properties("dashcamd.service"),
        "catalog_sha256": None
        if not catalog.is_file()
        else _sha256(_bounded_read(catalog, 64 * 1024**2)),
        "sentinel_sha256": None
        if not sentinel.is_file()
        else _sha256(_bounded_read(sentinel, 4096)),
        "throttled": _command(("/usr/bin/vcgencmd", "get_throttled"))
        .stdout.decode("ascii")
        .strip(),
    }


@contextlib.contextmanager
def _qualification_locks() -> Iterator[None]:
    try:
        import fcntl
    except ImportError as error:
        raise HarnessError("live qualification locks require POSIX flock") from error
    descriptors: list[int] = []
    try:
        for path in LIVE_LOCKS:
            parent_metadata = os.lstat(path.parent)
            if (
                not stat.S_ISDIR(parent_metadata.st_mode)
                or parent_metadata.st_uid != 0
                or path.parent.is_symlink()
            ):
                raise HarnessError("live qualification lock directory identity differs")
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or metadata.st_uid != 0
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                    or metadata.st_dev != parent_metadata.st_dev
                ):
                    raise HarnessError("live qualification lock identity differs")
                fcntl.flock(  # type: ignore[attr-defined]
                    descriptor,
                    fcntl.LOCK_EX | fcntl.LOCK_NB,  # type: ignore[attr-defined]
                )
            except BlockingIOError as error:
                os.close(descriptor)
                raise HarnessError("another live qualification holds a required lock") from error
            descriptors.append(descriptor)
        yield
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _exclusion_loaded(values: Mapping[str, str], nonce: str, before: Mapping[str, str]) -> bool:
    drop_in = (EXCLUSION_DIRECTORY / EXCLUSION_FILE_NAME).as_posix()
    conditions = values.get("Conditions", "")
    return (
        values.get("LoadState") == before.get("LoadState") == "loaded"
        and values.get("ActiveState") == "inactive"
        and values.get("UnitFileState") == before.get("UnitFileState") == "enabled"
        and values.get("DropInPaths") == drop_in
        and values.get("RefuseManualStart") == "yes"
        and conditions == "[unprintable]"
        and not (_exclusion_condition_path(nonce).exists())
        and not (_exclusion_condition_path(nonce).is_symlink())
    )


@contextlib.contextmanager
def _runtime_exclusion(work: Path) -> Iterator[dict[str, object]]:
    before = _service_properties("dashcamd.service")
    journal = _read_recovery_journal(work)
    nonce = work.name.removeprefix("dashcam-m10-private.")
    if (
        before.get("ActiveState") != "inactive"
        or before.get("UnitFileState") != "enabled"
        or journal.get("phase") != "PREPARED"
        or journal.get("prior_unit") != _unit_restore_facts(before)
        or journal.get("prior_exclusion_present") is not False
    ):
        raise HarnessError("ordinary dashcamd must be enabled and inactive")
    condition = _exclusion_condition_path(nonce)
    if (
        EXCLUSION_DIRECTORY.exists()
        or EXCLUSION_DIRECTORY.is_symlink()
        or condition.exists()
        or condition.is_symlink()
    ):
        raise HarnessError("ordinary dashcamd already has a runtime exclusion override")
    _transition_recovery_journal(work, "PREPARED", "EXCLUSION_INTENT")
    owned_exclusion = _create_runtime_exclusion(nonce)
    _transition_recovery_journal(
        work,
        "EXCLUSION_INTENT",
        "EXCLUSION_OWNED",
        owned_exclusion=owned_exclusion,
    )
    state: dict[str, object] = {"before": before, "restore_authorized": False}
    try:
        _command(("/usr/bin/systemctl", "daemon-reload"))
        excluded = _service_properties("dashcamd.service")
        if not _exclusion_loaded(excluded, nonce, before):
            raise HarnessError("ordinary dashcamd runtime exclusion did not close admission")
        yield state
    finally:
        if state["restore_authorized"] is True:
            if set(entry.name for entry in work.iterdir()) != {"RECOVERY.json"}:
                raise HarnessError("owned work was not cleaned before exclusion restoration")
            if _owned_exclusion_facts(nonce) != owned_exclusion:
                raise HarnessError("owned runtime exclusion changed before restoration")
            _transition_recovery_journal(work, "EXCLUSION_OWNED", "CLEANED_EXCLUSION")
            if _owned_exclusion_facts(nonce) != owned_exclusion:
                raise HarnessError("owned runtime exclusion changed after cleanup commit")
            _remove_owned_runtime_exclusion(nonce, owned_exclusion)
            _command(("/usr/bin/systemctl", "daemon-reload"))
            after = _service_properties("dashcamd.service")
            if (
                EXCLUSION_DIRECTORY.exists()
                or EXCLUSION_DIRECTORY.is_symlink()
                or _unit_restore_facts(after) != cast(dict[str, str], journal["prior_unit"])
            ):
                raise HarnessError("ordinary dashcamd state was not restored exactly")
            _transition_recovery_journal(work, "CLEANED_EXCLUSION", "RESTORED")
            _remove_recovery_authority(work)


def _require_excluded(nonce: str, before: Mapping[str, str]) -> None:
    state = _service_properties("dashcamd.service")
    if not _exclusion_loaded(state, nonce, before):
        raise HarnessError("ordinary dashcamd exclusion changed during qualification")


def _freeze_bundle(bundle: Path, work: Path) -> Path:
    _require_root_identity()
    frozen = work / "bundle"
    frozen.mkdir(mode=0o700)
    for name in (*sorted(MANIFEST_MEMBERS), "SHA256SUMS"):
        _require_root_identity()
        payload = _bounded_read(bundle / name, MAX_BUNDLE_FILE_BYTES)
        _write_exclusive(frozen / name, payload, 0o755 if name == "run.py" else 0o644)
    return frozen


def _unit_restore_facts(values: Mapping[str, str]) -> dict[str, str]:
    return {
        key: values.get(key, "")
        for key in (
            "LoadState",
            "ActiveState",
            "SubState",
            "UnitFileState",
            "NRestarts",
            "FragmentPath",
            "DropInPaths",
            "RefuseManualStart",
            "Conditions",
        )
    }


def _validate_work_identity(work: Path) -> os.stat_result:
    parent = Path("/var/tmp")
    parent_metadata = os.lstat(parent)
    metadata = os.lstat(work)
    if (
        work.parent != parent
        or NONCE_RE.fullmatch(work.name) is None
        or not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_uid != 0
        or parent_metadata.st_dev != os.lstat("/").st_dev
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_nlink < 2
        or metadata.st_dev != parent_metadata.st_dev
    ):
        raise HarnessError("recovery work directory identity differs")
    return metadata


def _exclusion_condition_path(nonce: str) -> Path:
    if re.fullmatch(r"[0-9a-f]{12}", nonce) is None:
        raise HarnessError("runtime exclusion nonce differs")
    return Path("/run") / f"dashcam-m10-exclusion-{nonce}-absent"


def _exclusion_content(nonce: str) -> bytes:
    condition = _exclusion_condition_path(nonce)
    return (f"[Unit]\nRefuseManualStart=yes\nConditionPathExists={condition.as_posix()}\n").encode(
        "ascii"
    )


def _open_exclusion_parent() -> int:
    return os.open(
        EXCLUSION_DIRECTORY.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )


def _exclusion_facts_at(
    parent_descriptor: int,
    nonce: str,
) -> dict[str, object]:
    parent_metadata = os.fstat(parent_descriptor)
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_uid != 0
        or parent_metadata.st_gid != 0
    ):
        raise HarnessError("runtime exclusion parent identity differs")
    directory_descriptor = os.open(
        EXCLUSION_DIRECTORY.name,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_descriptor,
    )
    try:
        directory_metadata = os.fstat(directory_descriptor)
        if (
            not stat.S_ISDIR(directory_metadata.st_mode)
            or directory_metadata.st_uid != 0
            or directory_metadata.st_gid != 0
            or stat.S_IMODE(directory_metadata.st_mode) != 0o755
            or directory_metadata.st_nlink < 2
            or directory_metadata.st_dev != parent_metadata.st_dev
        ):
            raise HarnessError("owned runtime exclusion directory identity differs")
        names = os.listdir(directory_descriptor)
        if names != [EXCLUSION_FILE_NAME]:
            raise HarnessError("owned runtime exclusion directory members differ")
        file_descriptor = os.open(
            EXCLUSION_FILE_NAME,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_descriptor,
        )
        try:
            file_metadata = os.fstat(file_descriptor)
            expected_content = _exclusion_content(nonce)
            if (
                not stat.S_ISREG(file_metadata.st_mode)
                or file_metadata.st_uid != 0
                or file_metadata.st_gid != 0
                or stat.S_IMODE(file_metadata.st_mode) != 0o644
                or file_metadata.st_nlink != 1
                or file_metadata.st_dev != directory_metadata.st_dev
                or file_metadata.st_size != len(expected_content)
            ):
                raise HarnessError("owned runtime exclusion file identity differs")
            chunks = bytearray()
            while len(chunks) <= len(expected_content):
                chunk = os.read(file_descriptor, len(expected_content) + 1 - len(chunks))
                if not chunk:
                    break
                chunks.extend(chunk)
            if bytes(chunks) != expected_content:
                raise HarnessError("owned runtime exclusion content differs")
        finally:
            os.close(file_descriptor)
    finally:
        os.close(directory_descriptor)
    return {
        "directory_device": directory_metadata.st_dev,
        "directory_inode": directory_metadata.st_ino,
        "directory_uid": directory_metadata.st_uid,
        "directory_gid": directory_metadata.st_gid,
        "directory_mode": stat.S_IMODE(directory_metadata.st_mode),
        "directory_nlink": directory_metadata.st_nlink,
        "file_device": file_metadata.st_dev,
        "file_inode": file_metadata.st_ino,
        "file_uid": file_metadata.st_uid,
        "file_gid": file_metadata.st_gid,
        "file_mode": stat.S_IMODE(file_metadata.st_mode),
        "file_nlink": file_metadata.st_nlink,
        "file_size": file_metadata.st_size,
        "content_sha256": _sha256(expected_content),
        "content": expected_content.decode("ascii"),
        "condition_path": _exclusion_condition_path(nonce).as_posix(),
    }


def _owned_exclusion_facts(nonce: str) -> dict[str, object]:
    parent_descriptor = _open_exclusion_parent()
    try:
        return _exclusion_facts_at(parent_descriptor, nonce)
    finally:
        os.close(parent_descriptor)


def _create_runtime_exclusion(nonce: str) -> dict[str, object]:
    condition = _exclusion_condition_path(nonce)
    if condition.exists() or condition.is_symlink():
        raise HarnessError("runtime exclusion impossible condition path exists")
    parent_descriptor = _open_exclusion_parent()
    try:
        parent_metadata = os.fstat(parent_descriptor)
        if (
            not stat.S_ISDIR(parent_metadata.st_mode)
            or parent_metadata.st_uid != 0
            or parent_metadata.st_gid != 0
        ):
            raise HarnessError("runtime exclusion parent identity differs")
        os.mkdir(EXCLUSION_DIRECTORY.name, 0o755, dir_fd=parent_descriptor)
        directory_descriptor = os.open(
            EXCLUSION_DIRECTORY.name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptor,
        )
        try:
            payload = _exclusion_content(nonce)
            file_descriptor = os.open(
                EXCLUSION_FILE_NAME,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o644,
                dir_fd=directory_descriptor,
            )
            try:
                view = memoryview(payload)
                while view:
                    written = os.write(file_descriptor, view)
                    if written <= 0:
                        raise HarnessError("runtime exclusion write made no progress")
                    view = view[written:]
                fchown = getattr(os, "fchown", None)
                if not callable(fchown):
                    raise HarnessError("runtime exclusion ownership control is unavailable")
                fchown(file_descriptor, 0, 0)
                os.fsync(file_descriptor)
            finally:
                os.close(file_descriptor)
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        os.fsync(parent_descriptor)
        return _exclusion_facts_at(parent_descriptor, nonce)
    finally:
        os.close(parent_descriptor)


def _remove_owned_runtime_exclusion(nonce: str, expected: Mapping[str, object]) -> None:
    parent_descriptor = _open_exclusion_parent()
    try:
        try:
            directory_descriptor = os.open(
                EXCLUSION_DIRECTORY.name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_descriptor,
            )
        except FileNotFoundError:
            return
        try:
            directory_metadata = os.fstat(directory_descriptor)
            directory_facts = {
                "directory_device": directory_metadata.st_dev,
                "directory_inode": directory_metadata.st_ino,
                "directory_uid": directory_metadata.st_uid,
                "directory_gid": directory_metadata.st_gid,
                "directory_mode": stat.S_IMODE(directory_metadata.st_mode),
                "directory_nlink": directory_metadata.st_nlink,
            }
            if any(expected.get(key) != value for key, value in directory_facts.items()):
                raise HarnessError("owned runtime exclusion directory changed before removal")
            names = os.listdir(directory_descriptor)
            if names == [EXCLUSION_FILE_NAME]:
                if _exclusion_facts_at(parent_descriptor, nonce) != expected:
                    raise HarnessError("owned runtime exclusion changed before removal")
                os.unlink(EXCLUSION_FILE_NAME, dir_fd=directory_descriptor)
                os.fsync(directory_descriptor)
                names = os.listdir(directory_descriptor)
            if names:
                raise HarnessError("owned runtime exclusion directory is not empty")
        finally:
            os.close(directory_descriptor)
        os.rmdir(EXCLUSION_DIRECTORY.name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)


def _write_recovery_journal(work: Path, nonce: str, prior: Mapping[str, str]) -> None:
    if work != Path("/var/tmp") / f"dashcam-m10-private.{nonce}":
        raise HarnessError("recovery journal work identity differs")
    _validate_work_identity(work)
    _write_exclusive(
        work / "RECOVERY.json",
        canonical_json(
            {
                "schema_version": 2,
                "nonce": nonce,
                "work": work.as_posix(),
                "ordinary_unit": "dashcamd.service",
                "phase": "PREPARED",
                "exclusion_owner": str(uuid4()),
                "prior_exclusion_present": False,
                "prior_unit": _unit_restore_facts(prior),
                "owned_exclusion": None,
            }
        ),
        0o600,
    )
    descriptor = os.open(work, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _owned_exclusion_document_valid(value: object, nonce: str) -> bool:
    if not isinstance(value, dict):
        return False
    expected_keys = {
        "directory_device",
        "directory_inode",
        "directory_uid",
        "directory_gid",
        "directory_mode",
        "directory_nlink",
        "file_device",
        "file_inode",
        "file_uid",
        "file_gid",
        "file_mode",
        "file_nlink",
        "file_size",
        "content_sha256",
        "content",
        "condition_path",
    }
    content = _exclusion_content(nonce)
    integer_keys = expected_keys - {"content_sha256", "content", "condition_path"}
    return (
        set(value) == expected_keys
        and all(
            isinstance(value.get(key), int) and not isinstance(value.get(key), bool)
            for key in integer_keys
        )
        and value.get("directory_device") == value.get("file_device")
        and cast(int, value.get("directory_device")) > 0
        and cast(int, value.get("directory_inode")) > 0
        and cast(int, value.get("file_inode")) > 0
        and value.get("directory_uid") == 0
        and value.get("directory_gid") == 0
        and value.get("directory_mode") == 0o755
        and cast(int, value.get("directory_nlink")) >= 2
        and value.get("file_uid") == 0
        and value.get("file_gid") == 0
        and value.get("file_mode") == 0o644
        and value.get("file_nlink") == 1
        and value.get("file_size") == len(content)
        and value.get("content_sha256") == _sha256(content)
        and value.get("content") == content.decode("ascii")
        and value.get("condition_path") == _exclusion_condition_path(nonce).as_posix()
    )


def _read_recovery_journal(work: Path) -> dict[str, object]:
    work_metadata = _validate_work_identity(work)
    work_descriptor = os.open(
        work,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened_work_metadata = os.fstat(work_descriptor)
        if (
            opened_work_metadata.st_dev != work_metadata.st_dev
            or opened_work_metadata.st_ino != work_metadata.st_ino
            or not stat.S_ISDIR(opened_work_metadata.st_mode)
            or opened_work_metadata.st_uid != 0
            or opened_work_metadata.st_gid != 0
            or stat.S_IMODE(opened_work_metadata.st_mode) != 0o700
            or opened_work_metadata.st_nlink < 2
        ):
            raise HarnessError("recovery work directory changed while opening journal")
        journal_descriptor = os.open(
            "RECOVERY.json",
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=work_descriptor,
        )
        try:
            journal_metadata = os.fstat(journal_descriptor)
            if journal_metadata.st_size > 4096:
                raise HarnessError("recovery journal is oversized")
            chunks = bytearray()
            while len(chunks) <= 4096:
                chunk = os.read(journal_descriptor, 4097 - len(chunks))
                if not chunk:
                    break
                chunks.extend(chunk)
            payload = bytes(chunks)
            if len(payload) != journal_metadata.st_size:
                raise HarnessError("recovery journal changed while reading")
        finally:
            os.close(journal_descriptor)
    finally:
        os.close(work_descriptor)
    document = _strict_json(payload, "recovery journal")
    if (
        not stat.S_ISREG(journal_metadata.st_mode)
        or journal_metadata.st_uid != 0
        or journal_metadata.st_gid != 0
        or stat.S_IMODE(journal_metadata.st_mode) != 0o600
        or journal_metadata.st_nlink != 1
        or journal_metadata.st_dev != work_metadata.st_dev
        or payload != canonical_json(document)
        or set(document)
        != {
            "schema_version",
            "nonce",
            "work",
            "ordinary_unit",
            "phase",
            "exclusion_owner",
            "prior_exclusion_present",
            "prior_unit",
            "owned_exclusion",
        }
        or document.get("schema_version") != 2
        or document.get("nonce") != work.name.removeprefix("dashcam-m10-private.")
        or document.get("work") != work.as_posix()
        or document.get("ordinary_unit") != "dashcamd.service"
        or document.get("phase")
        not in {
            "PREPARED",
            "EXCLUSION_INTENT",
            "EXCLUSION_OWNED",
            "CLEANED_EXCLUSION",
            "RESTORED",
        }
        or not isinstance(document.get("exclusion_owner"), str)
        or str(UUID(cast(str, document["exclusion_owner"]))) != document["exclusion_owner"]
        or document.get("prior_exclusion_present") is not False
        or not isinstance(document.get("prior_unit"), dict)
        or set(cast(dict[str, object], document["prior_unit"]))
        != {
            "LoadState",
            "ActiveState",
            "SubState",
            "UnitFileState",
            "NRestarts",
            "FragmentPath",
            "DropInPaths",
            "RefuseManualStart",
            "Conditions",
        }
        or (
            document.get("owned_exclusion") is not None
            and not _owned_exclusion_document_valid(
                document.get("owned_exclusion"), cast(str, document.get("nonce"))
            )
        )
        or (
            document.get("phase") in {"PREPARED", "EXCLUSION_INTENT"}
            and document.get("owned_exclusion") is not None
        )
        or (
            document.get("phase") in {"EXCLUSION_OWNED", "CLEANED_EXCLUSION"}
            and not isinstance(document.get("owned_exclusion"), dict)
        )
    ):
        raise HarnessError("recovery journal content differs")
    return document


def _validate_runtime_recovery_identity(runtime: Path, dashcam_uid: int) -> os.stat_result | None:
    if runtime.parent != Path("/run") or NONCE_RE.fullmatch(runtime.name) is None:
        raise HarnessError("recovery runtime path differs")
    run_metadata = os.lstat("/run")
    if (
        not stat.S_ISDIR(run_metadata.st_mode)
        or run_metadata.st_uid != 0
        or run_metadata.st_gid != 0
    ):
        raise HarnessError("recovery runtime parent identity differs")
    try:
        runtime_metadata = os.lstat(runtime)
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISDIR(runtime_metadata.st_mode)
        or runtime_metadata.st_uid != dashcam_uid
        or runtime_metadata.st_gid != EXPECTED_API_GID
        or stat.S_IMODE(runtime_metadata.st_mode) != 0o750
        or runtime_metadata.st_nlink < 2
        or runtime_metadata.st_dev != run_metadata.st_dev
    ):
        raise HarnessError("recovery runtime identity differs")
    return runtime_metadata


def _consume_preflight_diagnostic_at(
    runtime_descriptor: int,
    runtime_metadata: os.stat_result,
    dashcam_uid: int,
) -> dict[str, object]:
    import pwd

    name = "preflight-diagnostic.json"
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=runtime_descriptor)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != dashcam_uid
            or metadata.st_gid != pwd.getpwnam("dashcam").pw_gid  # type: ignore[attr-defined]
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or metadata.st_dev != runtime_metadata.st_dev
            or metadata.st_size <= 0
            or metadata.st_size > 4096
        ):
            raise HarnessError("preflight diagnostic member identity differs")
        chunks = bytearray()
        while len(chunks) <= 4096:
            chunk = os.read(descriptor, min(4097 - len(chunks), 4096))
            if not chunk:
                break
            chunks.extend(chunk)
        payload = bytes(chunks)
        document = _strict_json(payload, "preflight diagnostic")
        if (
            len(payload) > 4096
            or set(document) != {"schema_version", "stage"}
            or document.get("schema_version") != 1
            or document.get("stage") not in PREFLIGHT_DIAGNOSTIC_STAGES
        ):
            raise HarnessError("preflight diagnostic result differs")
        current = os.stat(name, dir_fd=runtime_descriptor, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise HarnessError("preflight diagnostic member changed")
    finally:
        os.close(descriptor)
    os.unlink(name, dir_fd=runtime_descriptor)
    os.fsync(runtime_descriptor)
    return document


def _cleanup_runtime_recovery_directory(runtime: Path, dashcam_uid: int) -> bool:
    """Remove one exact private runtime tree through verified parent descriptors."""

    if runtime.parent != Path("/run") or NONCE_RE.fullmatch(runtime.name) is None:
        raise HarnessError("recovery runtime path differs")
    run_descriptor = os.open(
        runtime.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        run_metadata = os.fstat(run_descriptor)
        if (
            not stat.S_ISDIR(run_metadata.st_mode)
            or run_metadata.st_uid != 0
            or run_metadata.st_gid != 0
        ):
            raise HarnessError("recovery runtime parent identity differs")
        try:
            runtime_descriptor = os.open(
                runtime.name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=run_descriptor,
            )
        except FileNotFoundError:
            return False
        try:
            runtime_metadata = os.fstat(runtime_descriptor)
            if (
                not stat.S_ISDIR(runtime_metadata.st_mode)
                or runtime_metadata.st_uid != dashcam_uid
                or runtime_metadata.st_gid != EXPECTED_API_GID
                or stat.S_IMODE(runtime_metadata.st_mode) != 0o750
                or runtime_metadata.st_nlink < 2
                or runtime_metadata.st_dev != run_metadata.st_dev
            ):
                raise HarnessError("recovery runtime identity differs")
            for name in os.listdir(runtime_descriptor):
                metadata = os.stat(name, dir_fd=runtime_descriptor, follow_symlinks=False)
                if metadata.st_uid != dashcam_uid or metadata.st_dev != runtime_metadata.st_dev:
                    raise HarnessError("recovery runtime member identity differs")
                if name == "control.sock":
                    if (
                        not stat.S_ISSOCK(metadata.st_mode)
                        or metadata.st_gid != EXPECTED_API_GID
                        or stat.S_IMODE(metadata.st_mode) != 0o660
                    ):
                        raise HarnessError("recovery control endpoint type differs")
                elif name == "preflight-diagnostic.json":
                    _consume_preflight_diagnostic_at(
                        runtime_descriptor,
                        runtime_metadata,
                        dashcam_uid,
                    )
                    continue
                elif name not in {
                    "bind-proof.json",
                    "status.json",
                    "rollback-quiesce-0.json",
                    "rollback-quiesce-1.json",
                    "rollback-quiesce-2.json",
                } or not stat.S_ISREG(metadata.st_mode):
                    raise HarnessError("recovery runtime member differs")
                os.unlink(name, dir_fd=runtime_descriptor)
            os.fsync(runtime_descriptor)
        finally:
            os.close(runtime_descriptor)
        os.rmdir(runtime.name, dir_fd=run_descriptor)
        os.fsync(run_descriptor)
        return True
    finally:
        os.close(run_descriptor)


def _transition_recovery_journal(
    work: Path,
    expected: str,
    target: str,
    *,
    owned_exclusion: Mapping[str, object] | None = None,
) -> None:
    allowed = {
        ("PREPARED", "EXCLUSION_INTENT"),
        ("EXCLUSION_INTENT", "EXCLUSION_OWNED"),
        ("EXCLUSION_OWNED", "CLEANED_EXCLUSION"),
        ("EXCLUSION_INTENT", "RESTORED"),
        ("PREPARED", "RESTORED"),
        ("CLEANED_EXCLUSION", "RESTORED"),
    }
    if (expected, target) not in allowed:
        raise HarnessError("recovery journal transition differs")
    document = _read_recovery_journal(work)
    if document["phase"] != expected:
        raise HarnessError("recovery journal phase changed")
    document["phase"] = target
    if owned_exclusion is not None:
        if expected != "EXCLUSION_INTENT" or target != "EXCLUSION_OWNED":
            raise HarnessError("exclusion ownership may only be bound at acquisition")
        document["owned_exclusion"] = dict(owned_exclusion)
    temporary = work / f".RECOVERY.{uuid4().hex}.tmp"
    _write_exclusive(temporary, canonical_json(document), 0o600)
    try:
        os.replace(temporary, work / "RECOVERY.json")
        descriptor = os.open(work, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def _remove_recovery_authority(work: Path) -> None:
    if _read_recovery_journal(work).get("phase") != "RESTORED":
        raise HarnessError("recovery authority is not restorable")
    journal = work / "RECOVERY.json"
    journal.unlink()
    descriptor = os.open(work, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if any(work.iterdir()):
        raise HarnessError("work remains after recovery authority removal")
    work.rmdir()
    parent = os.open("/var/tmp", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def _fully_allocate(path: Path, size: int) -> None:
    _require_root_identity(minimum_free=ROOT_PRESERVED_FREE_BYTES + size)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.posix_fallocate(descriptor, 0, size)  # type: ignore[attr-defined]
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size != size
            or metadata.st_blocks * 512 < size  # type: ignore[attr-defined]
        ):
            raise HarnessError("loop image is not fully allocated")
    finally:
        os.close(descriptor)


def _require_dense_image(path: Path, expected_size: int) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size != expected_size
            or metadata.st_blocks * 512 < expected_size  # type: ignore[attr-defined]
        ):
            raise HarnessError("formatted loop image is not densely allocated")
    finally:
        os.close(descriptor)


def _loop_backing(loop: Path) -> Path:
    if LOOP_RE.fullmatch(loop.as_posix()) is None:
        raise HarnessError("loop device name differs")
    raw = _bounded_virtual_read(Path("/sys/block") / loop.name / "loop/backing_file", 4096)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise HarnessError("loop backing path is not UTF-8") from error
    if "\0" in text or text.count("\n") > 1 or not text.endswith("\n"):
        raise HarnessError("loop backing virtual-file shape differs")
    value = text.removesuffix("\n")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise HarnessError("loop backing path is not absolute and normalized")
    return path


def _attach(image: Path) -> Path:
    result = _command(("/usr/sbin/losetup", "--find", "--show", image))
    loop = Path(result.stdout.decode("ascii").strip())
    if (
        LOOP_RE.fullmatch(loop.as_posix()) is None
        or _loop_backing(loop).resolve() != image.resolve()
    ):
        raise HarnessError("attached loop backing identity differs")
    return loop


def _require_owned_loop(loop: Path, image: Path, expected_size: int) -> None:
    _require_dense_image(image, expected_size)
    if LOOP_RE.fullmatch(loop.as_posix()) is None or _loop_backing(loop).resolve(
        strict=True
    ) != image.resolve(strict=True):
        raise HarnessError("loop backing identity differs")


def _require_mount(
    target: Path,
    loop: Path,
    image: Path,
    *,
    expected_size: int,
    filesystem: str,
    label: str,
) -> dict[str, str]:
    _require_owned_loop(loop, image, expected_size)
    facts = _blkid(loop)
    row = _findmnt(target)
    if (
        facts.get("TYPE") != filesystem
        or facts.get("LABEL") != label
        or row.get("source") != loop.as_posix()
        or row.get("target") != target.resolve(strict=True).as_posix()
        or row.get("fstype") != filesystem
        or row.get("label") != label
        or row.get("uuid") != facts.get("UUID")
        or row.get("maj:min") != _block_device_id(loop)
    ):
        raise HarnessError("private mounted filesystem identity differs")
    return facts


def _detach(loop: Path, image: Path) -> None:
    if _loop_backing(loop).resolve() != image.resolve():
        raise HarnessError("refusing to detach a foreign loop")
    _command(("/usr/sbin/losetup", "--detach", loop))


def _unmount(target: Path, loop: Path) -> None:
    row = _findmnt(target)
    if (
        row.get("source") != loop.as_posix()
        or Path(cast(str, row.get("target"))).resolve() != target.resolve()
    ):
        raise HarnessError("refusing to unmount a foreign target")
    _command(("/usr/bin/umount", target))


def _mount_fixture(
    *,
    work: Path,
    dashcam_uid: int,
    storage_gid: int,
    api_gid: int,
) -> dict[str, Path]:
    import pwd

    recording_image = work / "recording.exfat.img"
    state_image = work / "state.ext4.img"
    recording = work / "recording"
    state = work / "state"
    runtime = Path("/run") / work.name
    recording.mkdir(mode=0o700)
    state.mkdir(mode=0o700)
    runtime.mkdir(mode=0o750)
    os.chown(runtime, dashcam_uid, api_gid)  # type: ignore[attr-defined]
    _fully_allocate(recording_image, EXFAT_IMAGE_BYTES)
    _fully_allocate(state_image, EXT4_IMAGE_BYTES)
    recording_loop = _attach(recording_image)
    state_loop = _attach(state_image)
    try:
        _require_owned_loop(recording_loop, recording_image, EXFAT_IMAGE_BYTES)
        _require_owned_loop(state_loop, state_image, EXT4_IMAGE_BYTES)
        _command(("/usr/sbin/mkfs.exfat", "-n", RECORDING_LABEL, recording_loop), timeout=90)
        _require_owned_loop(recording_loop, recording_image, EXFAT_IMAGE_BYTES)
        _command(
            (
                "/usr/sbin/mkfs.ext4",
                "-F",
                "-m",
                "0",
                "-E",
                "nodiscard,lazy_itable_init=0,lazy_journal_init=0",
                "-L",
                CATALOG_LABEL,
                state_loop,
            ),
            timeout=90,
        )
        _require_owned_loop(state_loop, state_image, EXT4_IMAGE_BYTES)
        _command(
            (
                "/usr/bin/mount",
                "-t",
                "exfat",
                "-o",
                f"rw,nosuid,nodev,noexec,uid={dashcam_uid},gid={storage_gid},fmask=0137,dmask=0027",
                recording_loop,
                recording,
            )
        )
        _require_mount(
            recording,
            recording_loop,
            recording_image,
            expected_size=EXFAT_IMAGE_BYTES,
            filesystem="exfat",
            label=RECORDING_LABEL,
        )
        _command(
            (
                "/usr/bin/mount",
                "-t",
                "ext4",
                "-o",
                "rw,nosuid,nodev,noexec,noatime,nodiscard",
                state_loop,
                state,
            )
        )
        _require_mount(
            state,
            state_loop,
            state_image,
            expected_size=EXT4_IMAGE_BYTES,
            filesystem="ext4",
            label=CATALOG_LABEL,
        )
        _require_owned_loop(recording_loop, recording_image, EXFAT_IMAGE_BYTES)
        _require_owned_loop(state_loop, state_image, EXT4_IMAGE_BYTES)
        os.chown(  # type: ignore[attr-defined]
            state,
            dashcam_uid,
            pwd.getpwnam("dashcam").pw_gid,  # type: ignore[attr-defined]
        )
        os.chmod(state, 0o750)
    except BaseException:
        with contextlib.suppress(Exception):
            if _findmnt(recording).get("source") == recording_loop.as_posix():
                _unmount(recording, recording_loop)
        with contextlib.suppress(Exception):
            if _findmnt(state).get("source") == state_loop.as_posix():
                _unmount(state, state_loop)
        with contextlib.suppress(Exception):
            _detach(recording_loop, recording_image)
        with contextlib.suppress(Exception):
            _detach(state_loop, state_image)
        for target in (recording, state, runtime):
            with contextlib.suppress(Exception):
                metadata = os.lstat(target)
                if (
                    stat.S_ISDIR(metadata.st_mode)
                    and not target.is_symlink()
                    and not any(target.iterdir())
                ):
                    target.rmdir()
        for image, expected in (
            (recording_image, EXFAT_IMAGE_BYTES),
            (state_image, EXT4_IMAGE_BYTES),
        ):
            with contextlib.suppress(Exception):
                metadata = os.lstat(image)
                if (
                    stat.S_ISREG(metadata.st_mode)
                    and metadata.st_nlink == 1
                    and metadata.st_size == expected
                    and image.parent == work
                ):
                    image.unlink()
        raise
    return {
        "recording_image": recording_image,
        "state_image": state_image,
        "recording_loop": recording_loop,
        "state_loop": state_loop,
        "recording": recording,
        "state": state,
        "runtime": runtime,
    }


def _cleanup_fixture(paths: Mapping[str, Path]) -> None:
    recording = paths["recording"]
    state = paths["state"]
    recording_loop = paths["recording_loop"]
    state_loop = paths["state_loop"]
    _require_mount(
        recording,
        recording_loop,
        paths["recording_image"],
        expected_size=EXFAT_IMAGE_BYTES,
        filesystem="exfat",
        label=RECORDING_LABEL,
    )
    _require_mount(
        state,
        state_loop,
        paths["state_image"],
        expected_size=EXT4_IMAGE_BYTES,
        filesystem="ext4",
        label=CATALOG_LABEL,
    )
    _unmount(recording, recording_loop)
    _unmount(state, state_loop)
    _detach(recording_loop, paths["recording_image"])
    _detach(state_loop, paths["state_image"])


def _discard_fixture(paths: Mapping[str, Path]) -> None:
    import pwd

    work = paths["recording"].parent
    if work.parent != Path("/var/tmp") or NONCE_RE.fullmatch(work.name) is None:
        raise HarnessError("fixture discard root differs")
    runtime = paths["runtime"]
    if runtime.parent != Path("/run") or runtime.name != work.name or runtime.is_symlink():
        raise HarnessError("runtime discard root differs")
    allowed_runtime = {
        "bind-proof.json",
        "status.json",
        "rollback-quiesce-0.json",
        "rollback-quiesce-1.json",
        "rollback-quiesce-2.json",
    }
    runtime_metadata = _validate_runtime_recovery_identity(
        runtime,
        pwd.getpwnam("dashcam").pw_uid,  # type: ignore[attr-defined]
    )
    if runtime_metadata is None:
        raise HarnessError("runtime directory disappeared before fixture discard")
    runtime_descriptor = os.open(
        runtime,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        if "preflight-diagnostic.json" in os.listdir(runtime_descriptor):
            _consume_preflight_diagnostic_at(
                runtime_descriptor,
                runtime_metadata,
                pwd.getpwnam("dashcam").pw_uid,  # type: ignore[attr-defined]
            )
    finally:
        os.close(runtime_descriptor)
    for child in runtime.iterdir():
        if child.name not in allowed_runtime or child.is_dir() or child.is_symlink():
            raise HarnessError("runtime directory contains an unexpected entry")
        child.unlink()
    runtime.rmdir()
    for key in ("recording", "state"):
        target = paths[key]
        if target.is_symlink() or any(target.iterdir()):
            raise HarnessError("unmounted fixture target is not an empty real directory")
        target.rmdir()
    for key in ("recording_image", "state_image"):
        image = paths[key]
        metadata = image.stat()
        expected = EXFAT_IMAGE_BYTES if key == "recording_image" else EXT4_IMAGE_BYTES
        if image.parent != work or image.is_symlink() or metadata.st_size != expected:
            raise HarnessError("fixture image identity differs before removal")
        _require_root_identity()
        image.unlink()


def _blkid(loop: Path) -> dict[str, str]:
    result = _command(("/usr/sbin/blkid", "-o", "export", loop))
    values: dict[str, str] = {}
    for line in result.stdout.decode("ascii").splitlines():
        if line.count("=") != 1:
            raise HarnessError("blkid output differs")
        key, value = line.split("=", 1)
        values[key] = value
    uuid = values.get("UUID")
    if uuid is None or not 4 <= len(uuid) <= 128 or "TYPE" not in values or "LABEL" not in values:
        raise HarnessError("filesystem identity is incomplete")
    return values


def _install_private_state(
    frozen: Path,
    paths: Mapping[str, Path],
    *,
    dashcam_uid: int,
    dashcam_gid: int,
    storage_gid: int,
) -> dict[str, object]:
    recording = paths["recording"]
    state = paths["state"]
    values = _blkid(paths["recording_loop"])
    if values.get("TYPE") != "exfat" or values.get("LABEL") != RECORDING_LABEL:
        raise HarnessError("recording filesystem identity differs")
    uuid = values["UUID"]
    fingerprint = "4" * 64
    sentinel = canonical_json(
        {
            "layout_version": 1,
            "serial": "M10PRIVATE",
            "dashcam_uuid": uuid,
            "source_table_fingerprint": fingerprint,
            "root_end_sector": 4095,
            "data_start_sector": 4096,
            "data_end_sector": 999999,
        }
    )
    identity = (
        "DASHCAM_STORAGE_SCHEMA_VERSION=1\n"
        "DASHCAM_STORAGE_LAYOUT_VERSION=1\n"
        "DASHCAM_STORAGE_MOUNT=/srv/dashcam\n"
        f"DASHCAM_STORAGE_UUID={uuid}\n"
        "DASHCAM_STORAGE_CID=M10PRIVATE\n"
        f"DASHCAM_STORAGE_SOURCE_MBR_SHA256={fingerprint}\n"
        "DASHCAM_STORAGE_ROOT_END_SECTOR=4095\n"
        "DASHCAM_STORAGE_DATA_START_SECTOR=4096\n"
        "DASHCAM_STORAGE_DATA_END_SECTOR=999999\n"
        f"DASHCAM_STORAGE_MINIMUM_CAPACITY_BYTES={PRIVATE_MINIMUM_CAPACITY_BYTES}\n"
    ).encode("ascii")
    _write_exfat_sentinel_exclusive(
        recording / ".dashcam-volume",
        sentinel,
        dashcam_uid=dashcam_uid,
        storage_gid=storage_gid,
    )
    for directory in (recording / "clips", recording / "protected", recording / "pending"):
        directory.mkdir(mode=0o750)
    _write_exclusive(state / "config.toml", _config(rollback=False), 0o640, gid=dashcam_gid)
    _write_exclusive(state / "rollback-config.toml", _config(rollback=True), 0o640, gid=dashcam_gid)
    _write_exclusive(state / "storage-volume.env", identity, 0o640, gid=dashcam_gid)
    _write_exclusive(state / "source-launcher.py", LAUNCHER, 0o750, gid=dashcam_gid)
    _write_exclusive(
        state / "preflight-diagnostic.py",
        PREFLIGHT_DIAGNOSTIC,
        0o750,
        gid=dashcam_gid,
    )
    for name in ("candidate-source.zip", "rollback-source.zip"):
        _write_exclusive(
            state / name,
            _bounded_read(frozen / name, MAX_BUNDLE_FILE_BYTES),
            0o640,
            gid=dashcam_gid,
        )
    return {
        "uuid": uuid,
        "device_id": _device_id(recording),
        "capacity_bytes": _space(recording)[0],
    }


BIND_PROBE = b"""import json,os,subprocess
observed=os.stat("/srv/dashcam").st_dev
device=f"{os.major(observed)}:{os.minor(observed)}"
result=subprocess.run(("/usr/bin/findmnt","--json","--mountpoint","/srv/dashcam","--output","TARGET,SOURCE,FSTYPE,LABEL,UUID,OPTIONS,MAJ:MIN"),check=False,capture_output=True,timeout=3,env={"LC_ALL":"C","PATH":"/usr/sbin:/usr/bin:/sbin:/bin"})
value=(
 json.loads(result.stdout.decode("ascii"))
 if result.returncode==0 and not result.stderr
 else None
)
rows=value.get("filesystems") if isinstance(value,dict) else None
p={
 "srv_dev":observed,
 "state_dev":os.stat("/var/lib/dashcam").st_dev,
 "run_dev":os.stat("/run/dashcam").st_dev,
 "srv_w":os.access("/srv/dashcam",os.W_OK),
 "state_w":os.access("/var/lib/dashcam",os.W_OK),
 "run_w":os.access("/run/dashcam",os.W_OK),
 "findmnt_single":isinstance(rows,list) and len(rows)==1,
 "findmnt_device":isinstance(rows,list) and len(rows)==1 and rows[0].get("maj:min")==device,
}
f=os.open("/run/dashcam/bind-proof.json",os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
os.write(f,(json.dumps(p,sort_keys=True,separators=(",",":"))+"\\n").encode("ascii"));os.fsync(f);os.close(f)
"""


def _systemd_run(
    unit: str,
    properties: Sequence[str],
    command: Sequence[str | Path],
    *,
    client_timeout_s: float = SYSTEMD_RUN_CLIENT_TIMEOUT_S,
) -> None:
    if UNIT_RE.fullmatch(unit) is None:
        raise HarnessError("transient unit name differs")
    if (
        isinstance(client_timeout_s, bool)
        or not isinstance(client_timeout_s, (int, float))
        or not math.isfinite(client_timeout_s)
        or client_timeout_s <= 0
        or client_timeout_s > SYSTEMD_RUN_NOTIFY_TIMEOUT_S
    ):
        raise HarnessError("systemd-run client timeout differs")
    arguments: list[str | Path] = ["/usr/bin/systemd-run", "--unit", unit.removesuffix(".service")]
    for prop in properties:
        arguments.extend(("--property", prop))
    arguments.append("--")
    arguments.extend(command)
    try:
        _command(arguments, timeout=client_timeout_s)
    except BaseException:
        # systemd-run may lose its reply after PID 1 accepted the unit. Always
        # reconcile the deterministic name before allowing caller cleanup.
        with contextlib.suppress(Exception):
            _remove_unit(unit)
        raise


def _wait_unit_terminal(unit: str, timeout: float) -> dict[str, str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        values = _service_properties(unit)
        if values.get("ActiveState") in {"inactive", "failed"} and values.get("MainPID") == "0":
            return values
        time.sleep(0.1)
    raise HarnessError("transient unit did not reach a terminal state")


def _wait_recording(
    runtime: Path,
    timeout: float = UNIT_START_TIMEOUT_S,
    *,
    preflight_failure: Callable[[], None] | None = None,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        path = runtime / "status.json"
        if path.is_file() and not path.is_symlink():
            status = _strict_json(_bounded_read(path, 128 * 1024), "runtime status")
            lifecycle = status.get("lifecycle")
            if isinstance(lifecycle, dict) and lifecycle.get("state") in {"RECORDING", "DEGRADED"}:
                return status
        time.sleep(0.1)
    _phase_a_launch_failure_status(runtime, preflight_failure=preflight_failure)
    raise HarnessError("candidate recording status classification unexpectedly returned")


def _remove_unit(unit: str) -> None:
    values = _service_properties(unit)
    if values.get("MainPID") not in {None, "0"} or values.get("ActiveState") not in {
        "inactive",
        "failed",
    }:
        _command(
            ("/usr/bin/systemctl", "stop", unit),
            timeout=UNIT_STOP_TIMEOUT_S,
            allowed=frozenset({0, 5}),
        )
    _command(("/usr/bin/systemctl", "reset-failed", unit), allowed=frozenset({0, 1}))
    drained = _service_properties(unit)
    if drained.get("MainPID") not in {None, "0"} or drained.get("ControlGroup") not in {
        None,
        "",
    }:
        raise HarnessError("transient unit/cgroup did not drain before fixture cleanup")


def _run_bind_probe(nonce: str, paths: Mapping[str, Path]) -> dict[str, object]:
    import pwd

    state = paths["state"]
    runtime = paths["runtime"]
    probe = state / "bind-probe.py"
    _write_exclusive(
        probe,
        BIND_PROBE,
        0o750,
        uid=pwd.getpwnam("dashcam").pw_uid,  # type: ignore[attr-defined]
        gid=pwd.getpwnam("dashcam").pw_gid,  # type: ignore[attr-defined]
    )
    unit = f"dashcam-m10-private-{nonce}-bind.service"
    properties = render_transient_properties(
        recording_source=paths["recording"],
        state_source=state,
        runtime_source=runtime,
        role="bind",
    )
    _systemd_run(
        unit,
        properties,
        ("/usr/bin/python3", "-I", "/var/lib/dashcam/bind-probe.py"),
        client_timeout_s=SYSTEMD_RUN_CLIENT_TIMEOUT_S,
    )
    values = _wait_unit_terminal(unit, 20)
    try:
        if values.get("Result") != "success" or values.get("ExecMainStatus") != "0":
            raise HarnessError("private BindPaths qualification failed")
        proof = _strict_json(_bounded_read(runtime / "bind-proof.json", 4096), "bind proof")
        if (
            set(proof)
            != {
                "srv_dev",
                "state_dev",
                "run_dev",
                "srv_w",
                "state_w",
                "run_w",
                "findmnt_single",
                "findmnt_device",
            }
            or proof.get("srv_dev") != paths["recording"].stat().st_dev
            or proof.get("state_dev") != state.stat().st_dev
            or proof.get("run_dev") != runtime.stat().st_dev
            or any(
                proof.get(key) is not True
                for key in (
                    "srv_w",
                    "state_w",
                    "run_w",
                    "findmnt_single",
                    "findmnt_device",
                )
            )
        ):
            raise HarnessError("private bind device/writeability proof differs")
        return proof
    finally:
        _remove_unit(unit)


def _source_environment(archive: Path) -> None:
    resolved = archive.resolve(strict=True)
    sys.path.insert(0, str(resolved))
    for name in tuple(sys.modules):
        if name == "dashcam" or name.startswith("dashcam."):
            del sys.modules[name]


def _fixture_subprocess(
    action: str,
    root: Path,
    catalog: Path,
    boot_id: str,
    source_archive: Path,
) -> dict[str, object]:
    import pwd

    result = _command(
        (
            EXPECTED_INTERPRETER,
            "-I",
            Path(__file__).resolve(strict=True),
            "--fixture",
            action,
            "--fixture-root",
            root,
            "--fixture-catalog",
            catalog,
            "--fixture-boot-id",
            boot_id,
            "--fixture-source",
            source_archive,
        ),
        timeout=60,
        safe_fixture_refusal=True,
    )
    seeded = _strict_json(result.stdout, "fixture seed result")
    owner = pwd.getpwnam("dashcam")  # type: ignore[attr-defined]
    for path in (catalog, Path(f"{catalog}-wal"), Path(f"{catalog}-shm")):
        if not path.exists():
            continue
        if path.is_symlink() or path.parent != catalog.parent:
            raise HarnessError("fixture catalog sibling identity differs")
        os.chown(path, owner.pw_uid, owner.pw_gid)  # type: ignore[attr-defined]
        os.chmod(path, 0o640)
    return seeded


def _member(root: Path, relative: str, size: int) -> None:
    target = root / PurePosixPath(relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    try:
        os.posix_fallocate(descriptor, 0, size)  # type: ignore[attr-defined]
        os.pwrite(descriptor, b"M10PRIVATE\n", 0)  # type: ignore[attr-defined]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fixture_clip(order: int, size: int, *, protected: bool = False) -> Any:
    from dashcam.catalog.models import CatalogClip
    from dashcam.state import ClipLifecycle

    directory = "protected" if protected else "clips"
    return CatalogClip(
        clip_id=UUID(int=order + 1),
        lifecycle=ClipLifecycle.FINALIZED,
        video_path=f"{directory}/fixture-{order:04d}.mp4",
        sidecar_path=f"{directory}/fixture-{order:04d}.json",
        start_monotonic_ns=order * 1_000_000_000,
        end_monotonic_ns=(order + 1) * 1_000_000_000,
        retention_order=order,
        size_bytes=size,
        protected=protected,
        protection_reason="fixture:protected" if protected else None,
        pair_reconciled=True,
        managed=True,
    )


def _materialize(root: Path, clip: Any, size: int) -> None:
    _member(root, cast(str, clip.video_path), size)
    _member(root, cast(str, clip.sidecar_path), 4096)


def _finalizing_fixture(order: int, size: int) -> tuple[Any, Any, bytes]:
    from dashcam.catalog.models import CatalogClip
    from dashcam.metadata.schema import AudioSummary, ClipSidecar, GpsSummary, VideoSummary
    from dashcam.state import ClipLifecycle, GpsTimeState, SystemClockState, TimestampQuality
    from dashcam.storage.intents import PairPaths
    from dashcam.storage.naming import finalized_unsynced_clip_pair, provisional_clip_pair

    source = provisional_clip_pair(boot_id="m10private", sequence=order)
    target = finalized_unsynced_clip_pair(boot_id="m10private", sequence=order)
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
        size_bytes=size,
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
        software_version="m10-private-fixture",
    )
    return clip, paths, sidecar.to_canonical_json()


def _seed_fixture(action: str, root: Path, catalog_path: Path, boot_id: str) -> dict[str, object]:
    from dashcam.catalog.database import ClipCatalog

    if action not in {"A", "B", "C", "D"}:
        raise HarnessError("unknown fixture phase")
    file_size = 12 * 1024**2
    result: dict[str, object] = {"phase": action}
    with ClipCatalog(catalog_path) as catalog:
        if action == "A":
            eligible: list[str] = []
            for order in range(12):
                clip = _fixture_clip(order, file_size)
                _materialize(root, clip, file_size)
                catalog.register_clip(clip, catalog_now_ns=order)
                eligible.append(str(clip.clip_id))
            protected = _fixture_clip(20, file_size, protected=True)
            _materialize(root, protected, file_size)
            catalog.register_clip(protected, catalog_now_ns=20)
            leased = _fixture_clip(21, file_size)
            _materialize(root, leased, file_size)
            catalog.register_clip(leased, catalog_now_ns=21)
            lease_id = "1" * 32
            catalog.acquire_download_lease(
                leased.clip_id,
                holder=f"control-{lease_id}",
                monotonic_now_ns=time.monotonic_ns(),
                duration_ns=300 * 1_000_000_000,
                boot_id=boot_id,
                max_active_leases=8,
            )
            finalizing, paths, sidecar = _finalizing_fixture(22, file_size)
            _member(root, finalizing.video_path, file_size)
            dashcam_uid, storage_gid = _fixture_storage_identity()
            _write_exfat_pending_finalizing_sidecar(
                root,
                cast(str, finalizing.sidecar_path),
                sidecar,
                dashcam_uid=dashcam_uid,
                storage_gid=storage_gid,
            )
            intent_id = catalog.register_finalizing_clip(
                finalizing,
                promotion_paths=paths,
                monotonic_now_ns=time.monotonic_ns(),
            )
            result.update(
                {
                    "eligible_ids": eligible,
                    "protected_id": str(protected.clip_id),
                    "leased_id": str(leased.clip_id),
                    "lease_id": lease_id,
                    "finalizing_id": str(finalizing.clip_id),
                    "finalize_intent_id": str(intent_id),
                }
            )
        elif action == "B":
            protected_ids: list[str] = []
            protected_size = 16 * 1024**2
            for order in range(8):
                clip = _fixture_clip(100 + order, protected_size, protected=True)
                _materialize(root, clip, protected_size)
                catalog.register_clip(clip, catalog_now_ns=order)
                protected_ids.append(str(clip.clip_id))
            result["protected_ids"] = protected_ids
        elif action == "C":
            pending: list[dict[str, object]] = []
            for order in range(65):
                clip = _fixture_clip(200 + order, 4096)
                _materialize(root, clip, 4096)
                catalog.register_clip(clip, catalog_now_ns=order)
                intent = catalog.prepare_delete(
                    clip.clip_id,
                    monotonic_now_ns=order + 1_000,
                    boot_id=boot_id,
                )
                if intent is None:
                    raise HarnessError("DELETE backlog fixture was not created")
                pending.append(
                    {
                        "intent_id": str(intent),
                        "clip_id": str(clip.clip_id),
                        "video_path": clip.video_path,
                        "sidecar_path": clip.sidecar_path,
                        "video_sha256": _sha256(_bounded_read(root / clip.video_path, 8192)),
                        "sidecar_sha256": _sha256(_bounded_read(root / clip.sidecar_path, 8192)),
                    }
                )
            result["pending_delete_ids"] = pending
        else:
            result["schema5_latch_absent"] = catalog.retention_threshold_latch() is None
    return result


def _query_catalog(catalog_path: Path, root: Path) -> dict[str, object]:
    if (
        catalog_path.is_symlink()
        or catalog_path.parent.is_symlink()
        or root.is_symlink()
        or catalog_path.name != "catalog.sqlite3"
        or catalog_path.parent.name != "state"
        or root.name != "recording"
        or catalog_path.parent.parent != root.parent
    ):
        raise HarnessError("catalog observer path differs")
    connection = sqlite3.connect(f"file:{catalog_path.as_posix()}?mode=ro", uri=True, timeout=1)
    connection.execute("PRAGMA query_only=ON")
    try:
        rows = connection.execute(
            """
            SELECT clip_id,lifecycle,video_path,sidecar_path,protected,lease_holder,
                   start_monotonic_ns,end_monotonic_ns,retention_order,size_bytes,
                   protection_reason,pair_reconciled,managed
            FROM clips ORDER BY retention_order,clip_id LIMIT ?
            """,
            (MAX_FIXTURE_ROWS + 1,),
        ).fetchall()
        if len(rows) > MAX_FIXTURE_ROWS:
            raise HarnessError("catalog observer row bound exceeded")
        intents = connection.execute(
            """
            SELECT intent_id,kind,status,clip_id,completed_monotonic_ns FROM operation_intents
            ORDER BY created_monotonic_ns,intent_id LIMIT ?
            """,
            (MAX_FIXTURE_ROWS + 1,),
        ).fetchall()
        if len(intents) > MAX_FIXTURE_ROWS:
            raise HarnessError("intent observer row bound exceeded")
        events = connection.execute(
            """
            SELECT event_id,source,current_clip_id,requested_previous,requested_next,
                   missing_previous,remaining_next
            FROM protection_events ORDER BY triggered_monotonic_ns,event_id LIMIT ?
            """,
            (MAX_FIXTURE_ROWS + 1,),
        ).fetchall()
        targets = connection.execute(
            """
            SELECT event_id,clip_id,role,ordinal
            FROM protection_event_targets ORDER BY event_id,role,ordinal,clip_id LIMIT ?
            """,
            (MAX_FIXTURE_ROWS + 1,),
        ).fetchall()
        if len(events) > MAX_FIXTURE_ROWS or len(targets) > MAX_FIXTURE_ROWS:
            raise HarnessError("event observer row bound exceeded")
    finally:
        connection.close()
    delete_ids = {str(row[3]) for row in intents if row[1] == "DELETE"}
    clips: list[dict[str, object]] = []
    for (
        clip_id,
        lifecycle,
        video_path,
        sidecar_path,
        protected,
        lease_holder,
        start_monotonic_ns,
        end_monotonic_ns,
        retention_order,
        size_bytes,
        protection_reason,
        pair_reconciled,
        managed,
    ) in rows:
        if (
            not isinstance(video_path, str)
            or not isinstance(sidecar_path, str)
            or PurePosixPath(video_path).parts[0] not in {"clips", "protected", "pending"}
            or PurePosixPath(sidecar_path).parts[0] not in {"clips", "protected", "pending"}
        ):
            raise HarnessError("catalog observer found an unsafe managed path")
        video = Path(root, video_path)
        sidecar = Path(root, sidecar_path)
        clips.append(
            {
                "clip_id": str(clip_id),
                "lifecycle": str(lifecycle),
                "video_path": video_path,
                "sidecar_path": str(sidecar_path),
                "protected": bool(protected),
                "leased": lease_holder is not None,
                "video_present": video.is_file() and not video.is_symlink(),
                "sidecar_present": sidecar.is_file() and not sidecar.is_symlink(),
                "delete_intent": str(clip_id) in delete_ids,
                "start_monotonic_ns": start_monotonic_ns,
                "end_monotonic_ns": end_monotonic_ns,
                "retention_order": retention_order,
                "size_bytes": size_bytes,
                "protection_reason": protection_reason,
                "pair_reconciled": bool(pair_reconciled),
                "managed": bool(managed),
            }
        )
    return {
        "clips": clips,
        "intents": [
            {
                "intent_id": str(row[0]),
                "kind": row[1],
                "status": row[2],
                "clip_id": str(row[3]),
                "completed_monotonic_ns": row[4],
            }
            for row in intents
        ],
        "events": [
            {
                "event_id": str(row[0]),
                "source": row[1],
                "current_clip_id": str(row[2]),
                "requested_previous": row[3],
                "requested_next": row[4],
                "missing_previous": row[5],
                "remaining_next": row[6],
            }
            for row in events
        ],
        "targets": [
            {"event_id": str(row[0]), "clip_id": str(row[1]), "role": row[2], "ordinal": row[3]}
            for row in targets
        ],
    }


def _catalog_counts(catalog_path: Path) -> dict[str, int]:
    connection = sqlite3.connect(f"file:{catalog_path.as_posix()}?mode=ro", uri=True, timeout=1)
    connection.execute("PRAGMA query_only=ON")
    try:
        return {
            "delete_complete": int(
                connection.execute(
                    "SELECT COUNT(*) FROM operation_intents "
                    "WHERE kind='DELETE' AND status='COMPLETE'"
                ).fetchone()[0]
            ),
            "delete_pending": int(
                connection.execute(
                    "SELECT COUNT(*) FROM operation_intents "
                    "WHERE kind='DELETE' AND status='PENDING'"
                ).fetchone()[0]
            ),
            "writing": int(
                connection.execute(
                    "SELECT COUNT(*) FROM clips WHERE lifecycle='WRITING'"
                ).fetchone()[0]
            ),
            "finalizing": int(
                connection.execute(
                    "SELECT COUNT(*) FROM clips WHERE lifecycle='FINALIZING'"
                ).fetchone()[0]
            ),
            "leases": int(
                connection.execute(
                    "SELECT COUNT(*) FROM clips WHERE lease_holder IS NOT NULL"
                ).fetchone()[0]
            ),
            "next_windows": int(
                connection.execute(
                    "SELECT COALESCE(SUM(remaining_next),0) FROM protection_events"
                ).fetchone()[0]
            ),
        }
    finally:
        connection.close()


def _managed_manifest(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for directory_name in ("clips", "protected", "pending"):
        directory = root / directory_name
        for member in directory.iterdir():
            if member.is_symlink() or not member.is_file():
                raise HarnessError("managed fixture contains a non-regular member")
            relative = member.relative_to(root).as_posix()
            result[relative] = _sha256(_bounded_read(member, 32 * 1024**2))
            if len(result) > MAX_FIXTURE_ROWS * 2:
                raise HarnessError("managed fixture member bound exceeded")
    return result


def _allocate_filler(
    root: Path,
    target_free: int,
    *,
    allow_concurrent_reclaim: bool = False,
) -> tuple[Path, int]:
    if not isinstance(allow_concurrent_reclaim, bool):
        raise TypeError("concurrent reclaim allowance must be boolean")
    facts = os.statvfs(root)  # type: ignore[attr-defined]
    free = facts.f_bavail * facts.f_frsize
    target_free = (target_free // facts.f_frsize) * facts.f_frsize
    if target_free < 2 * facts.f_frsize or free <= target_free:
        raise HarnessError("filler target is not below current free space")
    path = root / "M10-PRIVATE-UNKNOWN-FILLER.bin"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    allocated = 0
    steps = 0
    step_bytes = LIVE_FILLER_CHUNK_BYTES if allow_concurrent_reclaim else FILLER_CHUNK_BYTES
    try:
        while True:
            current = os.statvfs(root)  # type: ignore[attr-defined]
            free = current.f_bavail * current.f_frsize
            if current.f_frsize != facts.f_frsize or current.f_bavail > current.f_blocks:
                raise HarnessError("filler filesystem observation changed identity")
            if free <= target_free + 2 * current.f_frsize:
                break
            required = free - target_free
            amount = min(step_bytes, required)
            amount = ((amount + current.f_frsize - 1) // current.f_frsize) * current.f_frsize
            if allocated + amount > MAX_FILLER_BYTES:
                raise HarnessError("filler exceeds its allocation bound")
            os.posix_fallocate(descriptor, allocated, amount)  # type: ignore[attr-defined]
            allocated += amount
            steps += 1
            if steps > MAX_FILLER_STEPS:
                raise HarnessError("filler allocation step bound exceeded")
            if allow_concurrent_reclaim:
                time.sleep(0.02)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    observed = os.statvfs(root)  # type: ignore[attr-defined]
    final_free = observed.f_bavail * observed.f_frsize
    if (
        not allow_concurrent_reclaim
        and final_free > target_free + 2 * observed.f_frsize
    ):
        raise HarnessError("filler ended above its bounded target")
    if final_free < target_free - FILLER_CHUNK_BYTES:
        raise HarnessError("filler ended below its bounded target")
    return path, allocated


def _remove_filler(path: Path, root: Path) -> None:
    if path.parent != root or path.name != "M10-PRIVATE-UNKNOWN-FILLER.bin" or path.is_symlink():
        raise HarnessError("refusing to remove an unknown filler")
    path.unlink()
    descriptor = os.open(root, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _raw_control(runtime: Path, command: str, arguments: Mapping[str, object]) -> dict[str, object]:
    request_id = uuid4()
    frame = canonical_json(
        {
            "version": 1,
            "request_id": str(request_id),
            "command": command,
            "arguments": dict(arguments),
        }
    )
    if len(frame) > 4096:
        raise HarnessError("control request exceeds its bound")
    endpoint = runtime / "control.sock"
    with socket.socket(
        socket.AF_UNIX,  # type: ignore[attr-defined]
        socket.SOCK_STREAM,
    ) as client:
        client.settimeout(CONTROL_TIMEOUT_S)
        client.connect(endpoint.as_posix())
        client.sendall(frame)
        chunks = bytearray()
        while not chunks.endswith(b"\n"):
            payload = client.recv(min(4096, CONTROL_RESPONSE_BYTES + 1 - len(chunks)))
            if not payload:
                raise HarnessError("control endpoint closed before one response")
            chunks.extend(payload)
            if len(chunks) > CONTROL_RESPONSE_BYTES:
                raise HarnessError("control response exceeds its bound")
    response = _strict_json(bytes(chunks), "control response")
    if response.get("request_id") != str(request_id) or response.get("ok") is not True:
        raise HarnessError(f"control operation failed: {command}")
    result = response.get("result")
    if not isinstance(result, dict):
        raise HarnessError("control result shape differs")
    encoded = canonical_json(result)
    if b"/srv/dashcam" in encoded or b"video_path" in encoded or b"sidecar_path" in encoded:
        raise HarnessError("control response exposed a filesystem path")
    return cast(dict[str, object], result)


def _release_download_converged(
    runtime: Path,
    *,
    clip_id: object,
    lease_id: object,
) -> dict[str, object]:
    arguments = {"clip_id": clip_id, "lease_id": lease_id}
    try:
        result = _raw_control(runtime, "release_download", arguments)
    except HarnessError:
        result = _raw_control(runtime, "release_download", arguments)
    if result.get("clip_id") != clip_id or not isinstance(result.get("released"), bool):
        raise HarnessError("download release convergence response differs")
    return result


def _listener_identity(runtime: Path, dashcam_uid: int) -> dict[str, object]:
    endpoint = runtime / "control.sock"
    metadata = os.lstat(endpoint)
    if (
        not stat.S_ISSOCK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o660
        or metadata.st_uid != dashcam_uid
        or metadata.st_gid != EXPECTED_API_GID
    ):
        raise HarnessError("production control socket identity differs")
    return {"uid": metadata.st_uid, "gid": metadata.st_gid, "mode": "0660"}


def _storage_fault_classification(
    runtime: object,
    lifecycle_detail: object,
) -> tuple[str, str | None]:
    if not isinstance(runtime, dict):
        return "RUNTIME_MALFORMED", None
    if "storage_preflight" not in runtime:
        return "PREFLIGHT_MISSING", None
    preflight = runtime["storage_preflight"]
    if preflight is None:
        if lifecycle_detail == "storage preflight failed":
            return "PREFLIGHT_FAILED", None
        if lifecycle_detail == "storage preflight exceeded deadline":
            return "PREFLIGHT_TIMEOUT", None
    if not isinstance(preflight, dict):
        return "PREFLIGHT_NOT_MAPPING", None
    if set(preflight) != PREFLIGHT_STATUS_KEYS:
        return "PREFLIGHT_KEYS", None
    state = preflight.get("state")
    reasons = preflight.get("reasons")
    ready = preflight.get("ready")
    if type(state) is not str:
        return "PREFLIGHT_STATE_TYPE", None
    if state not in PREFLIGHT_STATES:
        return "PREFLIGHT_STATE_DOMAIN", None
    if not isinstance(reasons, list):
        return "PREFLIGHT_REASONS_TYPE", None
    if len(reasons) > len(PREFLIGHT_REASONS):
        return "PREFLIGHT_REASONS_LENGTH", None
    if any(type(reason) is not str for reason in reasons):
        return "PREFLIGHT_REASON_TYPE", None
    typed_reasons = cast(list[str], reasons)
    if len(set(typed_reasons)) != len(typed_reasons):
        return "PREFLIGHT_REASON_DUPLICATE", None
    if any(reason not in PREFLIGHT_REASONS for reason in typed_reasons):
        return "PREFLIGHT_REASON_DOMAIN", None
    if type(ready) is not bool:
        return "PREFLIGHT_READY_TYPE", None
    if not typed_reasons:
        if state != "READY" or ready is not True:
            return "PREFLIGHT_EMPTY_RELATION", None
    else:
        non_faulted = {"READ_ONLY", "CONFLICTING_MOUNT_OPTIONS", "RESERVE_EXHAUSTED"}
        if any(reason not in non_faulted for reason in typed_reasons):
            expected_state = "FAULTED"
        elif any(
            reason in {"READ_ONLY", "CONFLICTING_MOUNT_OPTIONS"}
            for reason in typed_reasons
        ):
            expected_state = "READ_ONLY"
        else:
            expected_state = "EMERGENCY"
        if state != expected_state or ready is not False:
            return "PREFLIGHT_NONEMPTY_RELATION", None
        return "PREFLIGHT_REASON", typed_reasons[0]
    if "storage_retention" not in runtime:
        return "RETENTION_MISSING", None
    retention = runtime["storage_retention"]
    if not isinstance(retention, dict) or set(retention) != RETENTION_STATUS_KEYS:
        return "RETENTION_MALFORMED", None
    mode = retention.get("mode")
    fault = retention.get("fault")
    trigger = retention.get("trigger")
    stop_required = retention.get("stop_required")
    non_stop_faults = {"OBSERVATION_FAILED", "INVALID_OBSERVATION"}
    stop_faults = set(SPACE_OBSERVATION_FAULTS) - non_stop_faults
    if (
        (mode is not None and (type(mode) is not str or mode not in RETENTION_MODES))
        or (
            fault is not None
            and (type(fault) is not str or fault not in SPACE_OBSERVATION_FAULTS)
        )
        or (
            trigger is not None
            and (type(trigger) is not str or trigger != "NO_SPACE_WRITE")
        )
        or type(stop_required) is not bool
        or (mode == "NORMAL" and trigger is not None)
        or (mode == "NORMAL" and fault is None and stop_required is not False)
        or (mode is None and trigger is not None)
        or (
            trigger == "NO_SPACE_WRITE"
            and mode not in {"RECLAIMING", "EMERGENCY"}
        )
        or (fault is None and stop_required and mode != "EMERGENCY")
        or (fault in non_stop_faults and stop_required is not False)
        or (fault in stop_faults and stop_required is not True)
        or (
            fault == "NO_SPACE_WRITE"
            and (mode != "EMERGENCY" or trigger != "NO_SPACE_WRITE" or not stop_required)
        )
    ):
        return "RETENTION_MALFORMED", None
    if fault is not None:
        return "RETENTION_FAULT", fault
    if stop_required:
        return "RETENTION_STOP", None
    return "RETENTION_MODE", mode


def _phase_a_launch_failure_status(
    runtime: Path,
    *,
    preflight_failure: Callable[[], None] | None = None,
) -> None:
    if runtime.parent != Path("/run") or NONCE_RE.fullmatch(runtime.name) is None:
        raise HarnessError("phase A launch status runtime identity differs")
    status = runtime / "status.json"
    try:
        present = status.is_file() and not status.is_symlink()
    except OSError:
        present = False
    if not present:
        raise HarnessError("phase A launch status is missing")
    document: dict[str, object] | None = None
    with contextlib.suppress(Exception):
        document = _strict_json(_bounded_read(status, RUNTIME_STATUS_BYTES), "runtime status")
    lifecycle = document.get("lifecycle") if document is not None else None
    if (
        document is None
        or set(document) != {"schema_version", "lifecycle", "runtime"}
        or document.get("schema_version") != 3
        or not isinstance(lifecycle, dict)
        or set(lifecycle)
        != {
            "state",
            "reason",
            "detail",
            "sequence",
            "config_schema_version",
            "notification_failures",
        }
        or type(lifecycle.get("sequence")) is not int
        or cast(int, lifecycle.get("sequence")) < 0
        or (
            lifecycle.get("config_schema_version") is not None
            and (
                type(lifecycle.get("config_schema_version")) is not int
                or cast(int, lifecycle.get("config_schema_version")) < 1
            )
        )
        or type(lifecycle.get("notification_failures")) is not int
        or cast(int, lifecycle.get("notification_failures")) < 0
        or (
            lifecycle.get("detail") is not None
            and (
                not isinstance(lifecycle.get("detail"), str)
                or not cast(str, lifecycle.get("detail"))
                or len(cast(str, lifecycle.get("detail"))) > 512
                or not cast(str, lifecycle.get("detail")).isprintable()
            )
        )
        or not isinstance(lifecycle.get("state"), str)
        or (
            lifecycle.get("reason") is not None
            and not isinstance(lifecycle.get("reason"), str)
        )
    ):
        raise HarnessError("phase A launch status is malformed")
    state = cast(str, lifecycle["state"])
    reason = cast(str | None, lifecycle["reason"])
    if state not in RECORDER_STATES:
        raise HarnessError("phase A launch status state is unknown")
    if reason is not None and reason not in RECORDER_REASONS:
        raise HarnessError("phase A launch status reason is unknown")
    if state == "RECORDING" and reason is not None:
        raise HarnessError("phase A launch status state/reason pair is malformed")
    if reason == "CONFIG_ERROR":
        raise HarnessError("phase A launch status matched classified branch")
    if reason == "STARTUP_FAILED":
        raise HarnessError("phase A launch status matched classified branch")
    if reason == "STARTUP_TIMEOUT":
        raise HarnessError("phase A launch status matched classified branch")
    if reason == "RUNTIME_EXITED":
        raise HarnessError("phase A launch status matched classified branch")
    if reason == "RUNTIME_FAILED":
        raise HarnessError("phase A launch status matched classified branch")
    if reason == "PIPELINE_RECOVERING":
        raise HarnessError("phase A launch status matched classified branch")
    if reason == "PIPELINE_RECOVERY_EXHAUSTED":
        raise HarnessError("phase A launch status matched classified branch")
    if reason == "PIPELINE_NO_PROGRESS":
        raise HarnessError("phase A launch status matched classified branch")
    if reason == "FINALIZATION_FAILED":
        raise HarnessError("phase A launch status matched classified branch")
    if reason == "STORAGE_FAULT":
        classification, storage_reason = _storage_fault_classification(
            document["runtime"],
            lifecycle["detail"],
        )
        if classification == "RUNTIME_MALFORMED":
            raise HarnessError("phase A storage status runtime is malformed")
        if classification == "PREFLIGHT_MISSING":
            raise HarnessError("phase A storage preflight status is missing")
        if classification == "PREFLIGHT_FAILED":
            if preflight_failure is not None:
                preflight_failure()
            raise HarnessError("phase A storage preflight execution failed")
        if classification == "PREFLIGHT_TIMEOUT":
            raise HarnessError("phase A storage preflight execution timed out")
        if classification == "PREFLIGHT_NOT_MAPPING":
            raise HarnessError("phase A storage preflight status type differs")
        if classification == "PREFLIGHT_KEYS":
            raise HarnessError("phase A storage preflight status fields differ")
        if classification == "PREFLIGHT_STATE_TYPE":
            raise HarnessError("phase A storage preflight state type differs")
        if classification == "PREFLIGHT_STATE_DOMAIN":
            raise HarnessError("phase A storage preflight state domain differs")
        if classification == "PREFLIGHT_REASONS_TYPE":
            raise HarnessError("phase A storage preflight reasons type differs")
        if classification == "PREFLIGHT_REASONS_LENGTH":
            raise HarnessError("phase A storage preflight reasons bound differs")
        if classification == "PREFLIGHT_REASON_TYPE":
            raise HarnessError("phase A storage preflight reason type differs")
        if classification == "PREFLIGHT_REASON_DUPLICATE":
            raise HarnessError("phase A storage preflight reasons are duplicated")
        if classification == "PREFLIGHT_REASON_DOMAIN":
            raise HarnessError("phase A storage preflight reason domain differs")
        if classification == "PREFLIGHT_READY_TYPE":
            raise HarnessError("phase A storage preflight ready type differs")
        if classification == "PREFLIGHT_EMPTY_RELATION":
            raise HarnessError("phase A storage preflight empty relation differs")
        if classification == "PREFLIGHT_NONEMPTY_RELATION":
            raise HarnessError("phase A storage preflight reason relation differs")
        if storage_reason == "MALFORMED_FACTS":
            raise HarnessError("phase A storage status matched classified branch")
        if storage_reason == "WRONG_TARGET":
            raise HarnessError("phase A storage status matched classified branch")
        if storage_reason == "UNMOUNTED":
            raise HarnessError("phase A storage status matched classified branch")
        if storage_reason == "MISSING_MOUNT_IDENTITY":
            raise HarnessError("phase A storage status matched classified branch")
        if storage_reason == "ROOTFS_ALIAS":
            raise HarnessError("phase A storage status matched classified branch")
        if storage_reason == "WRONG_FILESYSTEM":
            raise HarnessError("phase A storage status matched classified branch")
        if storage_reason == "WRONG_LABEL":
            raise HarnessError("phase A storage status matched classified branch")
        if storage_reason == "WRONG_UUID":
            raise HarnessError("phase A storage status matched classified branch")
        if storage_reason == "WRONG_UUID_SUFFIX":
            raise HarnessError("phase A storage status matched classified branch")
        if storage_reason == "READ_ONLY":
            raise HarnessError("phase A storage status matched classified branch")
        if storage_reason == "CONFLICTING_MOUNT_OPTIONS":
            raise HarnessError("phase A storage status matched classified branch")
        if storage_reason == "MISSING_SENTINEL":
            raise HarnessError("phase A storage status matched classified branch")
        if storage_reason == "WRONG_SENTINEL_VERSION":
            raise HarnessError("phase A storage status matched classified branch")
        if storage_reason == "WRONG_SENTINEL_IDENTITY":
            raise HarnessError("phase A storage status matched classified branch")
        if storage_reason == "WRONG_SENTINEL_UUID":
            raise HarnessError("phase A storage status matched classified branch")
        if storage_reason == "INVALID_SENTINEL_GEOMETRY":
            raise HarnessError("phase A storage status matched classified branch")
        if storage_reason == "INVALID_SPACE":
            raise HarnessError("phase A storage status matched classified branch")
        if storage_reason == "INSUFFICIENT_CAPACITY":
            raise HarnessError("phase A storage status matched classified branch")
        if storage_reason == "RESERVE_EXHAUSTED":
            raise HarnessError("phase A storage status matched classified branch")
        if storage_reason == "WRITE_PROBE_FAILED":
            raise HarnessError("phase A storage status matched classified branch")
        if classification == "RETENTION_MISSING":
            raise HarnessError("phase A storage retention status is missing")
        if classification == "RETENTION_MALFORMED":
            raise HarnessError("phase A storage retention status is malformed")
        if storage_reason == "OBSERVATION_FAILED":
            raise HarnessError("phase A storage status matched classified branch")
        if storage_reason == "INVALID_OBSERVATION":
            raise HarnessError("phase A storage status matched classified branch")
        if storage_reason == "OBSERVATION_STALE":
            raise HarnessError("phase A storage status matched classified branch")
        if storage_reason == "IDENTITY_DRIFT":
            raise HarnessError("phase A storage status matched classified branch")
        if storage_reason == "CAPACITY_DRIFT":
            raise HarnessError("phase A storage status matched classified branch")
        if storage_reason == "LATCH_LOAD_FAILED":
            raise HarnessError("phase A storage status matched classified branch")
        if storage_reason == "LATCH_BINDING_MISMATCH":
            raise HarnessError("phase A storage status matched classified branch")
        if storage_reason == "LATCH_STORE_FAILED":
            raise HarnessError("phase A storage status matched classified branch")
        if storage_reason == "NO_SPACE_WRITE":
            raise HarnessError("phase A storage status matched classified branch")
        if classification == "RETENTION_STOP":
            raise HarnessError("phase A storage status requires a stop without a fault")
        if classification == "RETENTION_MODE" and storage_reason is None:
            raise HarnessError("phase A storage status has no mode or fault")
        if storage_reason == "NORMAL":
            raise HarnessError("phase A storage status matched classified branch")
        if storage_reason == "RECLAIMING":
            raise HarnessError("phase A storage status matched classified branch")
        if storage_reason == "EMERGENCY":
            raise HarnessError("phase A storage status matched classified branch")
        raise HarnessError("phase A storage status classification is unreachable")
    if reason == "OPTIONAL_SUBSYSTEM":
        raise HarnessError("phase A launch status matched classified branch")
    if reason == "SHUTDOWN_FAILED":
        raise HarnessError("phase A launch status matched classified branch")
    if reason == "SHUTDOWN_TIMEOUT":
        raise HarnessError("phase A launch status matched classified branch")
    if state == "STARTING":
        raise HarnessError("phase A launch status matched classified branch")
    if state == "RECORDING":
        raise HarnessError("phase A launch status matched classified branch")
    if state == "STOPPING":
        raise HarnessError("phase A launch status matched classified branch")
    if state == "DEGRADED":
        raise HarnessError("phase A launch status lacks a required reason")
    if state == "FAULTED":
        raise HarnessError("phase A launch status lacks a required reason")
    raise HarnessError("phase A launch status classification is unreachable")


def _run_preflight_diagnostic(nonce: str, paths: Mapping[str, Path]) -> None:
    import pwd

    output = paths["runtime"] / "preflight-diagnostic.json"
    if output.exists() or output.is_symlink():
        raise HarnessError("preflight diagnostic output already exists")
    unit = f"dashcam-m10-private-{nonce}-preflight.service"
    properties = render_transient_properties(
        recording_source=paths["recording"],
        state_source=paths["state"],
        runtime_source=paths["runtime"],
        role="preflight-diagnostic",
    )
    try:
        _systemd_run(
            unit,
            properties,
            (EXPECTED_INTERPRETER, "-I", "/var/lib/dashcam/preflight-diagnostic.py"),
        )
        values = _wait_unit_terminal(unit, 20)
        if values.get("Result") != "success" or values.get("ExecMainStatus") != "0":
            raise HarnessError("preflight diagnostic unit failed")
        runtime_metadata = _validate_runtime_recovery_identity(
            paths["runtime"],
            pwd.getpwnam("dashcam").pw_uid,  # type: ignore[attr-defined]
        )
        if runtime_metadata is None:
            raise HarnessError("preflight diagnostic runtime disappeared")
        runtime_descriptor = os.open(
            paths["runtime"],
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            result = _consume_preflight_diagnostic_at(
                runtime_descriptor,
                runtime_metadata,
                pwd.getpwnam("dashcam").pw_uid,  # type: ignore[attr-defined]
            )
        finally:
            os.close(runtime_descriptor)
    finally:
        _remove_unit(unit)
    if set(result) != {"schema_version", "stage"} or result.get("schema_version") != 1:
        raise HarnessError("preflight diagnostic result differs")
    stage = result.get("stage")
    if stage == "IMPORT":
        raise HarnessError("preflight diagnostic classified import")
    if stage == "CONFIG":
        raise HarnessError("preflight diagnostic classified config")
    if stage == "IDENTITY":
        raise HarnessError("preflight diagnostic classified identity")
    if stage == "POLICY":
        raise HarnessError("preflight diagnostic classified policy")
    if stage == "COLLECT_ROOT":
        raise HarnessError("preflight diagnostic classified root device collection")
    if stage == "FINDMNT_EXEC":
        raise HarnessError("preflight diagnostic classified findmnt execution")
    if stage == "FINDMNT_BOUND":
        raise HarnessError("preflight diagnostic classified findmnt output bound")
    if stage == "FINDMNT_RETURN":
        raise HarnessError("preflight diagnostic classified findmnt return code")
    if stage == "FINDMNT_JSON":
        raise HarnessError("preflight diagnostic classified findmnt JSON")
    if stage == "FINDMNT_ROOT":
        raise HarnessError("preflight diagnostic classified findmnt root")
    if stage == "FINDMNT_ROWS":
        raise HarnessError("preflight diagnostic classified findmnt rows")
    if stage == "FINDMNT_ROW":
        raise HarnessError("preflight diagnostic classified findmnt row")
    if stage == "FINDMNT_DIFFERING":
        raise HarnessError("preflight diagnostic classified differing findmnt rows")
    if stage == "COLLECT_TARGET":
        raise HarnessError("preflight diagnostic classified mount target collection")
    if stage == "COLLECT_SOURCE":
        raise HarnessError("preflight diagnostic classified mount source collection")
    if stage == "COLLECT_FIELDS":
        raise HarnessError("preflight diagnostic classified mount field collection")
    if stage == "COLLECT_DIRECTORY":
        raise HarnessError("preflight diagnostic classified mount directory collection")
    if stage == "COLLECT_DEVICE":
        raise HarnessError("preflight diagnostic classified mount device collection")
    if stage == "COLLECT_RELATION":
        raise HarnessError("preflight diagnostic classified mount relation")
    if stage == "COLLECT_SPACE":
        raise HarnessError("preflight diagnostic classified filesystem space collection")
    if stage == "COLLECT_SENTINEL":
        raise HarnessError("preflight diagnostic classified sentinel collection")
    if stage == "PARSE":
        raise HarnessError("preflight diagnostic classified fact parsing")
    if stage == "FILESYSTEM":
        raise HarnessError("preflight diagnostic classified filesystem construction")
    if stage == "RUN":
        raise HarnessError("preflight diagnostic classified decision or write probe")
    if stage == "RESULT":
        raise HarnessError("preflight diagnostic unexpectedly returned a result")
    raise HarnessError("preflight diagnostic stage differs")


def _candidate_unit(
    nonce: str,
    suffix: str,
    paths: Mapping[str, Path],
    *,
    archive: Path = CANDIDATE_ARCHIVE_PATH,
    config: Path = CONFIG_PATH,
    role: str = "candidate",
    single_start: bool = False,
    before_launch: Callable[[], None] | None = None,
    launch_failure: Callable[[], None] | None = None,
) -> str:
    unit = f"dashcam-m10-private-{nonce}-{suffix}.service"
    properties = list(
        render_transient_properties(
            recording_source=paths["recording"],
            state_source=paths["state"],
            runtime_source=paths["runtime"],
            role=role,
        )
    )
    if single_start:
        properties.extend(("StartLimitBurst=1", "StartLimitIntervalSec=300s"))
    command = (
        EXPECTED_INTERPRETER,
        "-I",
        LAUNCHER_PATH,
        "--archive",
        archive,
        "--module",
        "dashcam.daemon",
        "--config",
        config,
        "--identity",
        IDENTITY_PATH,
    )
    if before_launch is not None:
        before_launch()
    try:
        _systemd_run(
            unit,
            properties,
            command,
            client_timeout_s=(
                SYSTEMD_RUN_NOTIFY_TIMEOUT_S
                if role in {"candidate", "rollback-recorder"}
                else SYSTEMD_RUN_CLIENT_TIMEOUT_S
            ),
        )
    except BaseException:
        if launch_failure is not None:
            launch_failure()
        raise
    return unit


def _stop_clean(unit: str) -> dict[str, str]:
    values = _service_properties(unit)
    pid = values.get("MainPID")
    if pid is None or not pid.isdigit() or int(pid) <= 0:
        raise HarnessError("transient recorder has no live main PID")
    _command(("/usr/bin/systemctl", "kill", "--signal=SIGTERM", "--kill-who=main", unit))
    terminal = _wait_unit_terminal(unit, UNIT_STOP_TIMEOUT_S)
    if (
        terminal.get("Result") != "success"
        or terminal.get("ExecMainStatus") != "0"
        or terminal.get("NRestarts") != "0"
    ):
        raise HarnessError("transient recorder did not stop cleanly")
    return terminal


def _writing_clip(snapshot: Mapping[str, object]) -> dict[str, object] | None:
    clips = snapshot.get("clips")
    if not isinstance(clips, list):
        raise HarnessError("catalog snapshot clip shape differs")
    rows = [row for row in clips if isinstance(row, dict) and row.get("lifecycle") == "WRITING"]
    if len(rows) > 1:
        raise HarnessError("production runtime exposed multiple WRITING clips")
    return None if not rows else cast(dict[str, object], rows[0])


def _wait_writing(catalog: Path, root: Path, timeout: float = 12) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = _writing_clip(_query_catalog(catalog, root))
        if row is not None and row.get("video_present") is True:
            return row
        time.sleep(0.05)
    raise HarnessError("durable current WRITING identity was not observed")


def _wait_delete_progress(
    catalog: Path,
    root: Path,
    before_count: int,
    writing: Mapping[str, object],
    *,
    timeout: float = 15,
) -> tuple[dict[str, object], dict[str, object]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = _query_catalog(catalog, root)
        intents = snapshot["intents"]
        assert isinstance(intents, list)
        complete = sum(
            1
            for row in intents
            if isinstance(row, dict)
            and row.get("kind") == "DELETE"
            and row.get("status") == "COMPLETE"
        )
        active = next(
            (
                cast(dict[str, object], row)
                for row in cast(list[object], snapshot["clips"])
                if isinstance(row, dict) and row.get("clip_id") == writing.get("clip_id")
            ),
            None,
        )
        if complete > before_count and active is not None:
            validate_writing_interval(writing, active)
            return snapshot, active
        time.sleep(OBSERVATION_INTERVAL_S)
    raise HarnessError("runtime reclaim made no progress during one WRITING interval")


def _wait_reclaim_quiescent(
    runtime: Path,
    catalog: Path,
    root: Path,
    timeout: float = 15,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        document = _strict_json(
            _bounded_read(runtime / "status.json", 128 * 1024),
            "runtime status",
        )
        runtime_status = document.get("runtime")
        retention = (
            runtime_status.get("storage_retention")
            if isinstance(runtime_status, dict)
            else None
        )
        if not isinstance(retention, dict) or set(retention) != RETENTION_STATUS_KEYS:
            raise HarnessError("runtime retention quiescence status differs")
        if (
            retention.get("mode") == "NORMAL"
            and retention.get("fault") is None
            and retention.get("trigger") is None
            and retention.get("stop_required") is False
            and _catalog_counts(catalog)["delete_pending"] == 0
        ):
            return _query_catalog(catalog, root)
        time.sleep(0.1)
    raise HarnessError("runtime reclaimer did not quiesce after filler removal")


def _canonical_media_row(root: Path, row: Mapping[str, object]) -> dict[str, object]:
    from dashcam.metadata.reconcile import parse_sidecar_bytes

    relative = row.get("sidecar_path")
    if not isinstance(relative, str):
        raise HarnessError("catalog sidecar path differs")
    sidecar_path = root / PurePosixPath(relative)
    payload = _bounded_read(sidecar_path, 512 * 1024)
    value = _strict_json(payload, "clip sidecar")
    parsed = parse_sidecar_bytes(payload)
    if payload != parsed.to_canonical_json():
        raise HarnessError("clip sidecar is not canonical JSON")
    video_summary = value.get("video")
    warnings = value.get("warnings", [])
    if not isinstance(video_summary, dict):
        raise HarnessError("canonical sidecar video summary differs")
    if value.get("clip_id") != row.get("clip_id"):
        raise HarnessError("canonical sidecar clip identity differs")
    if value.get("start_monotonic_ns") != row.get("start_monotonic_ns"):
        raise HarnessError("canonical sidecar start binding differs")
    if value.get("end_monotonic_ns") != row.get("end_monotonic_ns"):
        raise HarnessError("canonical sidecar end binding differs")
    if value.get("protected") is not row.get("protected"):
        raise HarnessError("canonical sidecar protection binding differs")
    if (
        not isinstance(value.get("video_file"), str)
        or not isinstance(value.get("metadata_file"), str)
        or PurePosixPath(relative).name != value.get("metadata_file")
    ):
        raise HarnessError("canonical sidecar filename binding differs")
    if (
        video_summary.get("codec") != "h264"
        or video_summary.get("width") != 1920
        or video_summary.get("height") != 1080
        or video_summary.get("fps_nominal") != 30
    ):
        raise HarnessError("canonical sidecar video profile differs")
    if (
        not isinstance(video_summary.get("frames_written"), int)
        or isinstance(video_summary.get("frames_written"), bool)
        or cast(int, video_summary.get("frames_written")) <= 0
    ):
        raise HarnessError("canonical sidecar frame counter differs")
    if not isinstance(warnings, list):
        raise HarnessError("canonical sidecar warnings shape differs")
    counter_warning = any(
        text in str(item).casefold()
        for item in warnings
        for text in (
            "dropped-frame observation was unavailable",
            "frame and drop counters are unavailable",
        )
    )
    dropped_frames = video_summary.get("dropped_frames")
    if (
        not isinstance(dropped_frames, int)
        or isinstance(dropped_frames, bool)
        or dropped_frames < 0
    ):
        raise HarnessError("canonical sidecar drop counter shape differs")
    if counter_warning and dropped_frames != 0:
        raise HarnessError("canonical sidecar drop sentinel contradicts its warning")
    if not counter_warning and dropped_frames != 0:
        raise HarnessError("canonical sidecar recorded dropped frames")
    directory = PurePosixPath(relative).parent
    video_relative = (directory / cast(str, value["video_file"])).as_posix()
    video = root / PurePosixPath(video_relative)
    metadata = video.stat()
    if row.get("video_path") != video_relative:
        raise HarnessError("finalized media catalog path differs")
    if not stat.S_ISREG(metadata.st_mode) or video.is_symlink():
        raise HarnessError("finalized media member type differs")
    if metadata.st_dev != root.stat().st_dev:
        raise HarnessError("finalized media device differs")
    if row.get("size_bytes") != metadata.st_size:
        raise HarnessError("finalized media size differs")
    if row.get("lifecycle") != "FINALIZED":
        raise HarnessError("finalized media lifecycle differs")
    if row.get("managed") is not True:
        raise HarnessError("finalized media ownership differs")
    if row.get("pair_reconciled") is not True:
        raise HarnessError("finalized media reconciliation differs")
    return {
        **row,
        "sidecar": sidecar_path,
        "video": video,
        "start": row["start_monotonic_ns"],
        "end": row["end_monotonic_ns"],
        "sequence": value.get("sequence"),
        "frames_written": video_summary["frames_written"],
        "sidecar_drop_counter_available": not counter_warning,
        "sidecar_dropped_frames": dropped_frames if not counter_warning else None,
    }


def _wait_event_media(
    catalog: Path,
    root: Path,
    event_id: str,
    timeout: float = 230,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = _query_catalog(catalog, root)
        events = [
            row
            for row in cast(list[dict[str, object]], snapshot["events"])
            if row.get("event_id") == event_id
        ]
        targets = [
            row
            for row in cast(list[dict[str, object]], snapshot["targets"])
            if row.get("event_id") == event_id
        ]
        if len(events) != 1:
            time.sleep(0.2)
            continue
        event = events[0]
        current = [
            row for row in targets if row.get("role") == "CURRENT" and row.get("ordinal") == 0
        ]
        next_rows = [
            row for row in targets if row.get("role") == "NEXT" and row.get("ordinal") == 1
        ]
        previous = [row for row in targets if row.get("role") == "PREVIOUS"]
        if (
            event.get("source") != "web"
            or event.get("requested_previous") != 2
            or event.get("requested_next") != 1
            or event.get("remaining_next") != 0
            or len(current) != 1
            or len(next_rows) != 1
            or current[0].get("clip_id") != event.get("current_clip_id")
            or len(previous) != 2 - cast(int, event.get("missing_previous", -1))
        ):
            time.sleep(0.2)
            continue
        by_id = {row["clip_id"]: row for row in cast(list[dict[str, object]], snapshot["clips"])}
        current_row = by_id.get(current[0]["clip_id"])
        next_row = by_id.get(next_rows[0]["clip_id"])
        if current_row is None or next_row is None:
            time.sleep(0.2)
            continue
        following = next(
            (
                row
                for row in by_id.values()
                if row.get("retention_order") == cast(int, next_row.get("retention_order", -2)) + 1
            ),
            None,
        )
        selected = [current_row, next_row, following]
        if (
            following is None
            or any(row.get("lifecycle") != "FINALIZED" for row in selected if row is not None)
            or current_row.get("protected") is not True
            or next_row.get("protected") is not True
            or not str(current_row.get("protection_reason", "")).startswith("event:")
            or not str(next_row.get("protection_reason", "")).startswith("event:")
            or PurePosixPath(cast(str, current_row.get("video_path", ""))).parts[0] != "protected"
            or PurePosixPath(cast(str, next_row.get("video_path", ""))).parts[0] != "protected"
            or cast(int, next_row.get("retention_order", -1))
            != cast(int, current_row.get("retention_order", -3)) + 1
        ):
            time.sleep(0.2)
            continue
        protect_complete = {
            row.get("clip_id")
            for row in cast(list[dict[str, object]], snapshot["intents"])
            if row.get("kind") == "PROTECT" and row.get("status") == "COMPLETE"
        }
        if next_row.get("clip_id") not in protect_complete:
            time.sleep(0.2)
            continue
        media = [_canonical_media_row(root, cast(dict[str, object], row)) for row in selected]
        return media, {
            "event_id": event_id,
            "current_clip_id": current_row["clip_id"],
            "next_clip_id": next_row["clip_id"],
            "next_protect_intent_complete": True,
            "next_pair_preserved": True,
            "immediate_target_ids": sorted(
                cast(str, row["clip_id"])
                for row in targets
                if row.get("role") in {"PREVIOUS", "CURRENT"}
            ),
        }
        time.sleep(0.5)
    raise HarnessError("event current/NEXT/following media did not converge")


def _packet_payload(data: str) -> bytes:
    payload = bytearray()
    for line in data.splitlines():
        match = re.fullmatch(r"[0-9a-fA-F]{8}: ([0-9a-fA-F ]+?)(?:  .*)?", line)
        if match is None:
            continue
        compact = match.group(1).replace(" ", "")
        if len(compact) % 2:
            raise HarnessError("ffprobe packet hexdump differs")
        payload.extend(bytes.fromhex(compact))
    if not payload:
        raise HarnessError("ffprobe first-packet payload is absent")
    return bytes(payload)


def _contains_idr(payload: bytes) -> bool:
    for marker in (b"\x00\x00\x01", b"\x00\x00\x00\x01"):
        start = 0
        while (index := payload.find(marker, start)) >= 0:
            header = index + len(marker)
            if header < len(payload) and payload[header] & 0x1F == 5:
                return True
            start = header
    offset = 0
    while offset + 4 < len(payload):
        length = int.from_bytes(payload[offset : offset + 4], "big")
        if length <= 0 or offset + 4 + length > len(payload):
            break
        if payload[offset + 4] & 0x1F == 5:
            return True
        offset += 4 + length
    return False


def _media_evidence(rows: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    evidence: list[dict[str, object]] = []
    previous_end: int | None = None
    for row in rows:
        video = cast(Path, row["video"])
        if not video.is_file() or video.is_symlink():
            raise HarnessError("finalized production video is absent")
        _command(
            (
                "/usr/bin/ffmpeg",
                "-v",
                "error",
                "-xerror",
                "-c:v",
                "h264_v4l2m2m",
                "-i",
                video,
                "-map",
                "0:v:0",
                "-f",
                "null",
                "-",
            ),
            timeout=30,
        )
        probe = _command(
            (
                "/usr/bin/ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-count_packets",
                "-show_entries",
                "stream=codec_name,width,height,duration,nb_read_packets,avg_frame_rate",
                "-of",
                "json",
                video,
            ),
            timeout=15,
        )
        document = _strict_json(probe.stdout, "ffprobe stream")
        streams = document.get("streams")
        if not isinstance(streams, list) or len(streams) != 1 or not isinstance(streams[0], dict):
            raise HarnessError("ffprobe stream shape differs")
        stream = cast(dict[str, object], streams[0])
        try:
            duration = float(cast(str, stream["duration"]))
            packets = int(cast(str, stream["nb_read_packets"]))
        except (KeyError, TypeError, ValueError) as error:
            raise HarnessError("ffprobe packet metrics differ") from error
        first_document = _strict_json(
            _command(
                (
                    "/usr/bin/ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-read_intervals",
                    "%+#1",
                    "-show_packets",
                    "-show_data",
                    "-show_entries",
                    "packet=codec_type,flags,data",
                    "-of",
                    "json",
                    video,
                )
            ).stdout,
            "ffprobe first packet",
        )
        first_packets = first_document.get("packets")
        if not isinstance(first_packets, list) or len(first_packets) != 1:
            raise HarnessError("ffprobe first packet shape differs")
        first = first_packets[0]
        if (
            not isinstance(first, dict)
            or set(first) != {"codec_type", "flags", "data"}
            or first.get("codec_type") != "video"
            or "K" not in str(first.get("flags", ""))
            or not isinstance(first.get("data"), str)
            or not _contains_idr(_packet_payload(cast(str, first["data"])))
        ):
            raise HarnessError("first video packet is not a strict H.264 IDR")
        packets_document = _strict_json(
            _command(
                (
                    "/usr/bin/ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_packets",
                    "-show_entries",
                    "packet=codec_type,pts,dts,flags",
                    "-of",
                    "json",
                    video,
                )
            ).stdout,
            "ffprobe packet timeline",
        )
        timeline = packets_document.get("packets")
        if not isinstance(timeline, list) or not 1 <= len(timeline) <= 4096:
            raise HarnessError("ffprobe packet timeline bound differs")
        prior_pts: int | None = None
        prior_dts: int | None = None
        minimum_pts_delta: int | None = None
        minimum_dts_delta: int | None = None
        for packet in timeline:
            if not isinstance(packet, dict) or set(packet) != {"codec_type", "pts", "dts", "flags"}:
                raise HarnessError("ffprobe packet row shape differs")
            pts, dts = packet.get("pts"), packet.get("dts")
            if (
                packet.get("codec_type") != "video"
                or not isinstance(pts, int)
                or isinstance(pts, bool)
                or not isinstance(dts, int)
                or isinstance(dts, bool)
                or (prior_pts is not None and pts <= prior_pts)
                or (prior_dts is not None and dts <= prior_dts)
            ):
                raise HarnessError("video packet PTS/DTS is not strictly monotonic")
            if prior_pts is not None:
                minimum_pts_delta = min(pts - prior_pts, minimum_pts_delta or pts - prior_pts)
            if prior_dts is not None:
                minimum_dts_delta = min(dts - prior_dts, minimum_dts_delta or dts - prior_dts)
            prior_pts, prior_dts = pts, dts
        start = cast(int, row["start"])
        end = cast(int, row["end"])
        gap = None if previous_end is None else start - previous_end
        if (
            stream.get("codec_name") != "h264"
            or stream.get("width") != 1920
            or stream.get("height") != 1080
            or duration < 59
            or packets / duration < 29.9
            or len(timeline) != packets
            or (gap is not None and gap != 0)
        ):
            raise HarnessError("ordinary clip decode/rate/IDR/gap gate failed")
        evidence.append(
            {
                "sequence": row.get("sequence"),
                "duration_s": round(duration, 6),
                "video_packets": packets,
                "sidecar_frames_written": row.get("frames_written"),
                "observer_minus_packets": cast(int, row.get("frames_written")) - packets,
                "sidecar_drop_counter_available": row.get("sidecar_drop_counter_available"),
                "packet_fps": round(packets / duration, 6),
                "minimum_pts_delta": minimum_pts_delta,
                "minimum_dts_delta": minimum_dts_delta,
                "boundary_gap_ns": gap,
                "idr_first": True,
                "hardware_h264_1080p": True,
                "hardware_decoded": True,
                "dropped_frames": (
                    0 if row.get("sidecar_drop_counter_available") is True else None
                ),
            }
        )
        previous_end = end
    return evidence


def _runtime_health(status: Mapping[str, object]) -> dict[str, object]:
    runtime = status.get("runtime")
    if not isinstance(runtime, dict):
        raise HarnessError("runtime status shape differs")
    frames = runtime.get("frames")
    overlay = runtime.get("overlay")
    if not isinstance(frames, dict) or not isinstance(overlay, dict):
        raise HarnessError("runtime status lacks frame/overlay evidence")
    renderer = overlay.get("renderer")
    video = runtime.get("video")
    if not isinstance(renderer, dict) or not isinstance(video, dict):
        raise HarnessError("runtime status lacks renderer/video evidence")
    caps = video.get("effective_caps")
    encoder = video.get("encoder_identity")
    dropped, raw, encoded, drop_source = (
        frames.get("dropped"),
        frames.get("raw"),
        frames.get("encoded"),
        frames.get("drop_source"),
    )
    zero_renderer = (
        "update_rejections",
        "contract_mismatches",
        "transform_failures",
        "mapping_limit_rejections",
        "sync_failures",
    )
    if not isinstance(dropped, int) or isinstance(dropped, bool) or dropped < 0:
        raise HarnessError("runtime aggregate drop counter shape differs")
    if dropped != 0:
        raise HarnessError("runtime recorded aggregate dropped frames")
    if not isinstance(raw, int) or isinstance(raw, bool) or raw <= 0:
        raise HarnessError("runtime raw frame progress differs")
    if not isinstance(encoded, int) or isinstance(encoded, bool) or encoded <= 0:
        raise HarnessError("runtime encoded frame progress differs")
    if not isinstance(drop_source, str) or not drop_source:
        raise HarnessError("runtime drop counter source differs")
    if runtime.get("pipeline_restart_count") != 0:
        raise HarnessError("runtime pipeline restart count differs")
    if overlay.get("last_error") is not None:
        raise HarnessError("runtime overlay reported an error")
    if renderer.get("last_error") is not None:
        raise HarnessError("runtime renderer reported an error")
    if renderer.get("update_rejections") != 0:
        raise HarnessError("runtime renderer update rejection count differs")
    if renderer.get("contract_mismatches") != 0:
        raise HarnessError("runtime renderer contract mismatch count differs")
    if renderer.get("transform_failures") != 0:
        raise HarnessError("runtime renderer transform failure count differs")
    if renderer.get("mapping_limit_rejections") != 0:
        raise HarnessError("runtime renderer mapping rejection count differs")
    if renderer.get("sync_failures") != 0:
        raise HarnessError("runtime renderer synchronization failure count differs")
    if (
        video.get("width") != 1920
        or video.get("height") != 1080
        or video.get("frames_per_second") != 30
        or video.get("codec") != "h264"
        or video.get("hardware_encoded") is not True
        or not isinstance(caps, dict)
        or caps
        != {
            "raw_format": "NV12",
            "fps_numerator": 30,
            "fps_denominator": 1,
            "h264_profile": "high",
            "h264_level": "4.1",
        }
        or not isinstance(encoder, dict)
        or encoder.get("factory_name") != "v4l2h264enc"
        or "hardware"
        not in {part.casefold() for part in str(encoder.get("factory_class", "")).split("/")}
        or re.fullmatch(r"/dev/video[0-9]+", str(encoder.get("device_path", ""))) is None
    ):
        raise HarnessError("runtime hardware encoder identity differs")
    return {
        "raw_frames": raw,
        "encoded_frames": encoded,
        "dropped_frames": dropped,
        "drop_source": drop_source,
        "pipeline_restarts": 0,
        "encoder_factory": "v4l2h264enc",
        "hardware_encoded": True,
        "renderer_failures": {name: 0 for name in zero_renderer},
    }


def _phase_a(
    nonce: str,
    paths: Mapping[str, Path],
    identity: Mapping[str, object],
    *,
    dashcam_uid: int,
) -> dict[str, object]:
    root, state, runtime = paths["recording"], paths["state"], paths["runtime"]
    _source_environment(state / "candidate-source.zip")
    catalog = state / "catalog.sqlite3"
    boot_id = _read_boot_id()
    fixture = _fixture_subprocess("A", root, catalog, boot_id, state / "candidate-source.zip")
    low, high, _emergency = resolved_thresholds(cast(int, identity["capacity_bytes"]))
    reserve = math.ceil(MINIMUM_FREE_GIB * 1024**3)
    startup_filler, startup_filler_bytes = _allocate_filler(root, (low + reserve) // 2)
    unit = _candidate_unit(
        nonce,
        "a",
        paths,
        launch_failure=lambda: _phase_a_launch_failure_status(
            runtime,
            preflight_failure=lambda: _run_preflight_diagnostic(nonce, paths),
        ),
    )
    started_ns = time.monotonic_ns()
    stopped = False
    try:
        _wait_recording(
            runtime,
            preflight_failure=lambda: _run_preflight_diagnostic(nonce, paths),
        )
        ready_ns = time.monotonic_ns()
        if ready_ns - started_ns >= 40_000_000_000 or _space(root)[1] < high:
            raise HarnessError(
                "startup reclaim/reconciliation exceeded its bound or high watermark"
            )
        listener = _listener_identity(runtime, dashcam_uid)
        after_start = _query_catalog(catalog, root)
        excluded = {
            cast(str, fixture["protected_id"]),
            cast(str, fixture["leased_id"]),
            cast(str, fixture["finalizing_id"]),
        }
        intents = cast(list[dict[str, object]], after_start["intents"])
        completed_deletes = [
            row for row in intents if row["kind"] == "DELETE" and row["status"] == "COMPLETE"
        ]
        finalize = next(
            (row for row in intents if row["intent_id"] == fixture["finalize_intent_id"]),
            None,
        )
        if not completed_deletes:
            raise HarnessError("startup reclaim produced no durable DELETE completion")
        if any(row["clip_id"] in excluded for row in completed_deletes):
            raise HarnessError("startup reclaim deleted an excluded clip")
        if finalize is None:
            raise HarnessError("startup FINALIZE intent was not observed")
        if finalize["status"] != "COMPLETE":
            raise HarnessError("startup FINALIZE intent did not complete")
        if not isinstance(finalize["completed_monotonic_ns"], int) or any(
            not isinstance(row["completed_monotonic_ns"], int)
            for row in completed_deletes
        ):
            raise HarnessError("startup intent completion timestamps differ")
        if any(
            cast(int, row["completed_monotonic_ns"])
            <= finalize["completed_monotonic_ns"]
            for row in completed_deletes
        ):
            raise HarnessError("startup DELETE did not follow FINALIZE convergence")
        excluded_rows = {
            row["clip_id"]: row
            for row in cast(list[dict[str, object]], after_start["clips"])
            if row["clip_id"] in excluded
        }
        if (
            len(excluded_rows) != 3
            or excluded_rows[cast(str, fixture["protected_id"])]["protected"] is not True
            or excluded_rows[cast(str, fixture["leased_id"])]["leased"] is not True
            or excluded_rows[cast(str, fixture["finalizing_id"])]["lifecycle"] != "FINALIZED"
        ):
            raise HarnessError("startup protected/lease/finalizing convergence differs")
        _remove_filler(startup_filler, root)

        control_status = _raw_control(runtime, "status", {})
        _release_download_converged(
            runtime,
            clip_id=fixture["leased_id"],
            lease_id=fixture["lease_id"],
        )
        writing_before = _wait_writing(catalog, root)
        before_live_count = _catalog_counts(catalog)["delete_complete"]
        live_filler, live_filler_bytes = _allocate_filler(
            root,
            low - 2 * 1024**2,
            allow_concurrent_reclaim=True,
        )
        live_snapshot, writing_after = _wait_delete_progress(
            catalog, root, before_live_count, writing_before
        )
        _remove_filler(live_filler, root)
        live_snapshot = _wait_reclaim_quiescent(runtime, catalog, root)

        candidates = [
            row
            for row in cast(list[dict[str, object]], live_snapshot["clips"])
            if row["lifecycle"] == "FINALIZED"
            and row["protected"] is False
            and row["leased"] is False
            and row["video_present"] is True
        ]
        if len(candidates) < 2:
            raise HarnessError("too few retained candidates for listener exclusion phase")
        leased = candidates[-1]
        manual = candidates[-2]
        acquired = _raw_control(
            runtime,
            "acquire_download",
            {"clip_id": leased["clip_id"], "member": "video", "holder": "m10-private"},
        )
        lease_id = acquired.get("lease_id")
        if not isinstance(lease_id, str):
            raise HarnessError("production listener did not return opaque lease authority")
        _raw_control(runtime, "protect_clip", {"clip_id": manual["clip_id"]})
        event_id = str(uuid4())
        event = _raw_control(runtime, "event", {"source": "web", "event_id": event_id})
        retried = _raw_control(runtime, "event", {"source": "web", "event_id": event_id})
        if (
            event.get("event_id") != event_id
            or retried.get("event_id") != event_id
            or retried != event
        ):
            raise HarnessError("production event retry identity differs")

        second_writing = _wait_writing(catalog, root)
        second_before = _catalog_counts(catalog)["delete_complete"]
        second_filler, second_filler_bytes = _allocate_filler(
            root,
            low - 2 * 1024**2,
            allow_concurrent_reclaim=True,
        )
        second_snapshot, _second_after = _wait_delete_progress(
            catalog, root, second_before, second_writing
        )
        protected_ids = {
            cast(str, manual["clip_id"]),
            *cast(list[str], event.get("protected_clip_ids", [])),
        }
        new_deletes = {
            cast(str, row["clip_id"])
            for row in cast(list[dict[str, object]], second_snapshot["intents"])
            if row["kind"] == "DELETE" and row["status"] == "COMPLETE"
        }
        if cast(str, leased["clip_id"]) in new_deletes or protected_ids & new_deletes:
            raise HarnessError("runtime deleted a leased/protected/event clip")
        _remove_filler(second_filler, root)

        full_rows, event_window = _wait_event_media(catalog, root, event_id)
        if sorted(cast(list[str], event.get("protected_clip_ids", []))) != event_window.get(
            "immediate_target_ids"
        ):
            raise HarnessError("event response/target identities differ")
        counts = _catalog_counts(catalog)
        if counts["next_windows"] != 0:
            raise HarnessError("event NEXT window did not converge online")
        following_lease = _raw_control(
            runtime,
            "acquire_download",
            {
                "clip_id": full_rows[2]["clip_id"],
                "member": "video",
                "holder": "m10-private-media",
            },
        )
        following_lease_id = following_lease.get("lease_id")
        if not isinstance(following_lease_id, str):
            raise HarnessError("media preservation lease identity differs")
        next_before = _catalog_counts(catalog)["delete_complete"]
        next_writing = _wait_writing(catalog, root)
        next_filler, next_filler_bytes = _allocate_filler(
            root,
            low - 2 * 1024**2,
            allow_concurrent_reclaim=True,
        )
        next_snapshot, _next_after = _wait_delete_progress(
            catalog,
            root,
            next_before,
            next_writing,
        )
        all_completed_deletes = {
            row["clip_id"]
            for row in cast(list[dict[str, object]], next_snapshot["intents"])
            if row["kind"] == "DELETE" and row["status"] == "COMPLETE"
        }
        if event_window["next_clip_id"] in all_completed_deletes:
            raise HarnessError("exact event NEXT successor was reclaimed")
        _remove_filler(next_filler, root)
        _release_download_converged(
            runtime,
            clip_id=full_rows[2]["clip_id"],
            lease_id=following_lease_id,
        )

        _release_download_converged(
            runtime,
            clip_id=leased["clip_id"],
            lease_id=lease_id,
        )
        final_status = _strict_json(
            _bounded_read(runtime / "status.json", 128 * 1024), "runtime status"
        )
        health = _runtime_health(final_status)
        terminal = _stop_clean(unit)
        stopped = True
        if (runtime / "control.sock").exists() or (runtime / "control.sock").is_symlink():
            raise HarnessError("listener remained after joined recorder shutdown")
        media = _media_evidence(full_rows)
        final_counts = _catalog_counts(catalog)
        if any(
            final_counts[key] != 0 for key in ("writing", "finalizing", "leases", "next_windows")
        ):
            raise HarnessError("candidate did not quiesce active lifecycle/lease/NEXT state")
        return {
            "passed": True,
            "startup_duration_ns": ready_ns - started_ns,
            "startup_finalize_before_reclaim": True,
            "startup_delete_count": len(completed_deletes),
            "startup_delete_before_finalize_count": 0,
            "startup_delete_after_finalize_count": len(completed_deletes),
            "protected_excluded": True,
            "leased_excluded": True,
            "integrated_pending_finalize_converged": True,
            "active_writing_interval": {
                "clip_id": writing_before["clip_id"],
                "preserved_until": writing_after["lifecycle"],
                "delete_progress": _catalog_counts(catalog)["delete_complete"] - before_live_count,
            },
            "listener": listener,
            "listener_status_reachable": bool(control_status),
            "event_idempotent": True,
            "next_window_converged": True,
            "event_window": event_window,
            "startup_filler_bytes": startup_filler_bytes,
            "live_filler_bytes": live_filler_bytes,
            "second_filler_bytes": second_filler_bytes,
            "next_successor_filler_bytes": next_filler_bytes,
            "runtime_health": health,
            "media": media,
            "unit": {key: terminal.get(key) for key in ("Result", "ExecMainStatus", "NRestarts")},
        }
    finally:
        if not stopped:
            with contextlib.suppress(Exception):
                _command(
                    ("/usr/bin/systemctl", "stop", unit),
                    timeout=UNIT_STOP_TIMEOUT_S,
                    allowed=frozenset({0, 5}),
                )
        _remove_unit(unit)


def _rollback_phase(nonce: str, paths: Mapping[str, Path]) -> dict[str, object]:
    runtime = paths["runtime"]
    boot_id = _read_boot_id()
    absent_catalog = paths["state"] / "rollback-absent.sqlite3"
    absent_fixture = _fixture_subprocess(
        "D",
        paths["recording"],
        absent_catalog,
        boot_id,
        paths["state"] / "candidate-source.zip",
    )
    if absent_fixture.get("schema5_latch_absent") is not True:
        raise HarnessError("rollback absent-latch fixture differs")
    quiesce_results: list[dict[str, object]] = []
    catalogs = (
        Path("/var/lib/dashcam/rollback-absent.sqlite3"),
        Path("/var/lib/dashcam/rollback-absent.sqlite3"),
        CATALOG_PATH,
    )
    expected_initialization = (True, False, False)
    for index, private_catalog in enumerate(catalogs):
        output = runtime / f"rollback-quiesce-{index}.json"
        unit = f"dashcam-m10-private-{nonce}-rollback{index}.service"
        properties = render_transient_properties(
            recording_source=paths["recording"],
            state_source=paths["state"],
            runtime_source=runtime,
            role="rollback-recovery",
        )
        _systemd_run(
            unit,
            properties,
            (
                EXPECTED_INTERPRETER,
                "-I",
                LAUNCHER_PATH,
                "--archive",
                ROLLBACK_ARCHIVE_PATH,
                "--module",
                "dashcam.rollback",
                "--output",
                f"/run/dashcam/{output.name}",
                "quiesce",
                "--config",
                "/var/lib/dashcam/rollback-config.toml",
                "--identity",
                IDENTITY_PATH,
                "--catalog",
                private_catalog,
                "--control-socket",
                CONTROL_SOCKET,
            ),
            client_timeout_s=SYSTEMD_RUN_CLIENT_TIMEOUT_S,
        )
        terminal = _wait_unit_terminal(unit, ROLLBACK_TIMEOUT_S)
        try:
            if terminal.get("Result") != "success" or terminal.get("ExecMainStatus") != "0":
                raise HarnessError("rollback quiesce refused private schema5 state")
            report = _strict_json(_bounded_read(output, 32 * 1024), "rollback quiesce report")
            guard = report.get("guard")
            if (
                set(report)
                != {
                    "schema_before",
                    "schema_after",
                    "passes",
                    "intents_examined",
                    "expired_leases_cleared",
                    "orphaned_writing_demoted",
                    "latch_initialized",
                    "guard",
                    "ready",
                }
                or report.get("ready") is not True
                or report.get("schema_before") != 5
                or report.get("schema_after") != 5
                or not isinstance(report.get("passes"), int)
                or not 1 <= cast(int, report["passes"]) <= 8
                or any(
                    not isinstance(report.get(key), int) or cast(int, report[key]) < 0
                    for key in (
                        "intents_examined",
                        "expired_leases_cleared",
                        "orphaned_writing_demoted",
                    )
                )
                or report.get("latch_initialized") is not expected_initialization[index]
                or not isinstance(guard, dict)
                or set(guard) != {"volume_uuid", "device_id", "capacity_bytes", "free_bytes"}
                or guard.get("volume_uuid") != _blkid(paths["recording_loop"])["UUID"]
                or guard.get("device_id") != _device_id(paths["recording"])
                or guard.get("capacity_bytes") != _space(paths["recording"])[0]
                or not isinstance(guard.get("free_bytes"), int)
                or cast(int, guard.get("free_bytes"))
                < resolved_thresholds(_space(paths["recording"])[0])[1]
            ):
                raise HarnessError("rollback quiesce/guard report contract differs")
            quiesce_results.append(report)
        finally:
            _remove_unit(unit)
    first_absent = dict(quiesce_results[0])
    second_absent = dict(quiesce_results[1])
    for document in (first_absent, second_absent):
        guard = dict(cast(dict[str, object], document["guard"]))
        guard.pop("free_bytes", None)
        document["guard"] = guard
        document["latch_initialized"] = False
    if first_absent != second_absent:
        raise HarnessError("second rollback quiesce was not idempotent")

    unit = _candidate_unit(
        nonce,
        "rollback3",
        paths,
        archive=ROLLBACK_ARCHIVE_PATH,
        config=Path("/var/lib/dashcam/rollback-config.toml"),
        role="rollback-recorder",
        single_start=True,
    )
    stopped = False
    try:
        _wait_recording(runtime)
        if (runtime / "control.sock").exists() or (runtime / "control.sock").is_symlink():
            raise HarnessError("rollback companion unexpectedly exposed a control listener")
        time.sleep(3)
        rollback_status = _strict_json(
            _bounded_read(runtime / "status.json", 128 * 1024), "rollback recorder status"
        )
        rollback_health = _runtime_health(rollback_status)
        terminal = _stop_clean(unit)
        stopped = True
        return {
            "passed": True,
            "quiesce_idempotent": True,
            "absent_latch_initialized_once": True,
            "fresh_post_recovery_preflight": True,
            "read_only_guard_admitted": True,
            "camera_encoder_started": True,
            "runtime_health": rollback_health,
            "control_listener_absent": True,
            "unit": {key: terminal.get(key) for key in ("Result", "ExecMainStatus", "NRestarts")},
        }
    finally:
        if not stopped:
            with contextlib.suppress(Exception):
                _command(
                    ("/usr/bin/systemctl", "stop", unit),
                    timeout=UNIT_STOP_TIMEOUT_S,
                    allowed=frozenset({0, 5}),
                )
        _remove_unit(unit)


def _protected_emergency_phase(
    nonce: str,
    paths: Mapping[str, Path],
    identity: Mapping[str, object],
) -> dict[str, object]:
    root, state, runtime = paths["recording"], paths["state"], paths["runtime"]
    catalog = state / "catalog.sqlite3"
    boot_id = _read_boot_id()
    fixture = _fixture_subprocess("B", root, catalog, boot_id, state / "candidate-source.zip")
    before_catalog = _query_catalog(catalog, root)
    before_manifest = _managed_manifest(root)
    before_hashes = {
        path.relative_to(root).as_posix(): _sha256(_bounded_read(path, 16 * 1024**2))
        for path in (root / "protected").iterdir()
        if path.is_file() and not path.is_symlink()
    }
    _low, _high, emergency = resolved_thresholds(cast(int, identity["capacity_bytes"]))
    filler, filler_bytes = _allocate_filler(root, emergency - 2 * 1024**2)
    observer = _CameraAbsenceObserver(catalog, root, runtime)
    unit = _candidate_unit(
        nonce,
        "b",
        paths,
        single_start=True,
        before_launch=observer.start,
        launch_failure=observer.stop,
    )
    try:
        terminal = _wait_unit_terminal(unit, UNIT_START_TIMEOUT_S)
        observer.stop()
        validate_clean_safety_stop(terminal)
        after = _query_catalog(catalog, root)
        after_hashes = {
            path.relative_to(root).as_posix(): _sha256(_bounded_read(path, 16 * 1024**2))
            for path in (root / "protected").iterdir()
            if path.is_file() and not path.is_symlink()
        }
        if (
            before_hashes != after_hashes
            or _managed_manifest(root) != before_manifest
            or after["clips"] != before_catalog["clips"]
            or before_catalog["intents"] != []
            or any(
                row["kind"] == "DELETE" for row in cast(list[dict[str, object]], after["intents"])
            )
            or (runtime / "control.sock").exists()
            or _catalog_counts(catalog)["writing"] != 0
        ):
            raise HarnessError("protected-only emergency changed protected data or opened runtime")
        return {
            "passed": True,
            "storage_safety_stop": True,
            "protected_ids": fixture["protected_ids"],
            "protected_hashes_unchanged": True,
            "no_delete_intent": True,
            "camera_opened": False,
            "listener_present": False,
            "filler_bytes": filler_bytes,
            "unit": {key: terminal.get(key) for key in ("Result", "ExecMainStatus", "NRestarts")},
        }
    finally:
        with contextlib.suppress(Exception):
            observer.stop()
        _remove_unit(unit)
        if filler.exists():
            _remove_filler(filler, root)


def _startup_bound_phase(nonce: str, paths: Mapping[str, Path]) -> dict[str, object]:
    root, state, runtime = paths["recording"], paths["state"], paths["runtime"]
    catalog = state / "catalog.sqlite3"
    boot_id = _read_boot_id()
    fixture = _fixture_subprocess("C", root, catalog, boot_id, state / "candidate-source.zip")
    observer = _CameraAbsenceObserver(catalog, root, runtime)
    unit = _candidate_unit(
        nonce,
        "c",
        paths,
        single_start=True,
        before_launch=observer.start,
        launch_failure=observer.stop,
    )
    try:
        terminal = _wait_unit_terminal(unit, UNIT_START_TIMEOUT_S)
        observer.stop()
        validate_clean_safety_stop(terminal)
        counts = _catalog_counts(catalog)
        snapshot: dict[str, object] = {
            **counts,
            "camera_opened": counts["writing"] > 0,
            "listener_present": (runtime / "control.sock").exists(),
            "catalog_worker_count": 0 if terminal.get("MainPID") == "0" else 1,
        }
        validate_startup_delete_bound(snapshot)
        exact = _query_catalog(catalog, root)
        _validate_c_exact_transition(fixture, exact, root)
        oracle = cast(list[dict[str, object]], fixture["pending_delete_ids"])
        expected_manifest = {
            cast(str, oracle[-1]["video_path"]): cast(str, oracle[-1]["video_sha256"]),
            cast(str, oracle[-1]["sidecar_path"]): cast(str, oracle[-1]["sidecar_sha256"]),
        }
        if _managed_manifest(root) != expected_manifest:
            raise HarnessError("bounded startup refusal left an unexpected managed member")
        first = dict(counts)
        time.sleep(1)
        second = _catalog_counts(catalog)
        if first != second:
            raise HarnessError("catalog mutation continued after bounded startup refusal")
        return {
            "passed": True,
            "storage_safety_stop": True,
            "delete_complete": counts["delete_complete"],
            "delete_pending": counts["delete_pending"],
            "camera_opened": False,
            "listener_present": False,
            "detached_work": False,
            "unit": {key: terminal.get(key) for key in ("Result", "ExecMainStatus", "NRestarts")},
        }
    finally:
        with contextlib.suppress(Exception):
            observer.stop()
        _remove_unit(unit)


def _validate_pi(expected_board_serial: str) -> tuple[int, int, int]:
    import grp
    import pwd

    if sys.platform != "linux" or os.geteuid() != 0:
        raise HarnessError("exact-Pi qualification requires Linux root")
    if Path(sys.executable).resolve(strict=True) != EXPECTED_INTERPRETER.resolve(strict=True):
        raise HarnessError("qualification interpreter differs from accepted release")
    model = _read_board_model()
    if model != EXPECTED_BOARD_MODEL:
        raise HarnessError("board model differs")
    serial = _read_cpu_serial()
    if (
        serial != expected_board_serial
        or re.fullmatch(r"[0-9a-f]{16}", expected_board_serial) is None
    ):
        raise HarnessError("board serial differs")
    root = _findmnt(Path("/"))
    if root.get("source") != EXPECTED_ROOT_SOURCE or root.get("fstype") != "ext4":
        raise HarnessError("root backing differs")
    dashcam = pwd.getpwnam("dashcam")
    storage = grp.getgrnam("dashcam-storage")
    api = grp.getgrnam("dashcam-api")
    if api.gr_gid != EXPECTED_API_GID:
        raise HarnessError("reviewed inert dashcam-api gid differs")
    for executable in (
        EXPECTED_INTERPRETER,
        Path("/usr/bin/systemd-run"),
        Path("/usr/bin/systemctl"),
        Path("/usr/bin/findmnt"),
        Path("/usr/bin/mount"),
        Path("/usr/bin/umount"),
        Path("/usr/sbin/losetup"),
        Path("/usr/sbin/mkfs.exfat"),
        Path("/usr/sbin/mkfs.ext4"),
        Path("/usr/sbin/blkid"),
        Path("/usr/bin/ffprobe"),
        Path("/usr/bin/ffmpeg"),
        Path("/usr/bin/vcgencmd"),
    ):
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise HarnessError(f"required exact-Pi executable is absent: {executable.name}")
    return dashcam.pw_uid, dashcam.pw_gid, storage.gr_gid


def _fresh_phase(
    work: Path,
    frozen: Path,
    *,
    dashcam_uid: int,
    dashcam_gid: int,
    storage_gid: int,
) -> tuple[dict[str, Path], dict[str, object]]:
    free = _space(Path("/"))[1]
    if not root_budget_satisfied(free):
        raise HarnessError("fresh root reserve gate refused a phase allocation")
    paths = _mount_fixture(
        work=work,
        dashcam_uid=dashcam_uid,
        storage_gid=storage_gid,
        api_gid=EXPECTED_API_GID,
    )
    after_images = _space(Path("/"))[1]
    if after_images < ROOT_PRESERVED_FREE_BYTES + ROOT_BOUNDED_OVERHEAD_BYTES:
        raise HarnessError("root reserve fell below the post-image safety gate")
    try:
        identity = _install_private_state(
            frozen,
            paths,
            dashcam_uid=dashcam_uid,
            dashcam_gid=dashcam_gid,
            storage_gid=storage_gid,
        )
        _run_bind_probe(work.name.removeprefix("dashcam-m10-private."), paths)
        return paths, identity
    except BaseException:
        with contextlib.suppress(Exception):
            _cleanup_fixture(paths)
        with contextlib.suppress(Exception):
            _discard_fixture(paths)
        raise


def _remove_frozen(work: Path, frozen: Path) -> None:
    if work.parent != Path("/var/tmp") or NONCE_RE.fullmatch(work.name) is None:
        raise HarnessError("work cleanup root differs")
    if frozen.parent != work or frozen.name != "bundle" or frozen.is_symlink():
        raise HarnessError("frozen bundle cleanup root differs")
    for child in frozen.iterdir():
        if (
            child.name not in MANIFEST_MEMBERS | {"SHA256SUMS"}
            or child.is_dir()
            or child.is_symlink()
        ):
            raise HarnessError("frozen bundle contains an unexpected entry")
        _require_root_identity()
        child.unlink()
    _require_root_identity()
    frozen.rmdir()
    if set(entry.name for entry in work.iterdir()) != {"RECOVERY.json"}:
        raise HarnessError("owned work remains before exclusion restoration")


def _same_production(before: Mapping[str, object], after: Mapping[str, object]) -> None:
    for key in ("root", "recording", "catalog_sha256", "sentinel_sha256"):
        if before.get(key) != after.get(key):
            raise HarnessError(f"production host snapshot changed: {key}")
    before_unit, after_unit = before.get("dashcamd"), after.get("dashcamd")
    if not isinstance(before_unit, dict) or not isinstance(after_unit, dict):
        raise HarnessError("ordinary dashcamd snapshot shape differs")
    for key in _unit_restore_facts({}):
        if before_unit.get(key) != after_unit.get(key):
            raise HarnessError(f"ordinary dashcamd changed: {key}")
    if before.get("throttled") != "throttled=0x0" or after.get("throttled") != "throttled=0x0":
        raise HarnessError("Pi throttling gate failed")


def _mountpoint_or_none(path: Path) -> dict[str, object] | None:
    result = _command(
        (
            "/usr/bin/findmnt",
            "--json",
            "--output",
            "TARGET,SOURCE,FSTYPE,LABEL,UUID,OPTIONS,MAJ:MIN",
            "--mountpoint",
            path,
        ),
        allowed=frozenset({0, 1}),
    )
    if result.returncode == 1:
        return None
    value = _strict_json(result.stdout, "findmnt recovery")
    rows = value.get("filesystems")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise HarnessError("recovery mount observation differs")
    return cast(dict[str, object], rows[0])


def _cleaned_exclusion_layout(nonce: str, expected: Mapping[str, object]) -> bool:
    parent_descriptor = _open_exclusion_parent()
    try:
        try:
            directory_descriptor = os.open(
                EXCLUSION_DIRECTORY.name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_descriptor,
            )
        except FileNotFoundError:
            return False
        try:
            metadata = os.fstat(directory_descriptor)
            directory_values = {
                "directory_device": metadata.st_dev,
                "directory_inode": metadata.st_ino,
                "directory_uid": metadata.st_uid,
                "directory_gid": metadata.st_gid,
                "directory_mode": stat.S_IMODE(metadata.st_mode),
                "directory_nlink": metadata.st_nlink,
            }
            if any(expected.get(key) != value for key, value in directory_values.items()):
                raise HarnessError("cleaned exclusion directory identity differs")
            names = os.listdir(directory_descriptor)
            if names == [EXCLUSION_FILE_NAME]:
                if _exclusion_facts_at(parent_descriptor, nonce) != expected:
                    raise HarnessError("cleaned exclusion file identity differs")
            elif names:
                raise HarnessError("cleaned exclusion directory members differ")
            return True
        finally:
            os.close(directory_descriptor)
    finally:
        os.close(parent_descriptor)


def validate_recovery_exclusion_authority(
    phase: str,
    *,
    nonce: str,
    exclusion_present: bool,
    owned_exclusion: Mapping[str, object] | None,
) -> bool:
    """Return whether exact owned exclusion cleanup remains; refuse ambiguity."""

    condition = _exclusion_condition_path(nonce)
    if condition.exists() or condition.is_symlink():
        raise HarnessError("runtime exclusion impossible condition path exists")
    if phase in {"PREPARED", "RESTORED"}:
        if exclusion_present:
            raise HarnessError("pre-exclusion/restored journal cannot own a drop-in")
        return False
    if phase == "EXCLUSION_INTENT":
        if exclusion_present:
            raise HarnessError("exclusion intent does not prove ownership of a present drop-in")
        return False
    if phase == "EXCLUSION_OWNED":
        if (
            not exclusion_present
            or owned_exclusion is None
            or _owned_exclusion_facts(nonce) != owned_exclusion
        ):
            raise HarnessError("owned exclusion journal identity differs")
        return True
    if phase == "CLEANED_EXCLUSION":
        if owned_exclusion is None:
            raise HarnessError("cleaned exclusion evidence is absent")
        return _cleaned_exclusion_layout(nonce, owned_exclusion)
    raise HarnessError("unknown recovery exclusion phase")


def _associated_loop(image: Path) -> Path | None:
    result = _command(
        ("/usr/sbin/losetup", "--associated", image, "--output", "NAME", "--noheadings")
    )
    names = [line.strip() for line in result.stdout.decode("ascii").splitlines() if line.strip()]
    if len(names) > 1:
        raise HarnessError("recovery image has multiple loop associations")
    return None if not names else Path(names[0])


def _restore_recovery_exclusion(
    work: Path,
    journal: Mapping[str, object],
    phase: str,
    nonce: str,
    may_remove: bool,
) -> None:
    if phase == "PREPARED":
        ordinary = _service_properties("dashcamd.service")
        if _unit_restore_facts(ordinary) != journal["prior_unit"]:
            raise HarnessError("pre-exclusion recovery prior state differs")
        _transition_recovery_journal(work, "PREPARED", "RESTORED")
        return
    if phase == "EXCLUSION_INTENT":
        if may_remove:
            raise HarnessError("exclusion intent unexpectedly authorized removal")
        _command(("/usr/bin/systemctl", "daemon-reload"))
        ordinary = _service_properties("dashcamd.service")
        if (
            EXCLUSION_DIRECTORY.exists()
            or EXCLUSION_DIRECTORY.is_symlink()
            or _unit_restore_facts(ordinary) != journal["prior_unit"]
        ):
            raise HarnessError("ordinary recorder state differs after intent-only recovery")
        _transition_recovery_journal(work, "EXCLUSION_INTENT", "RESTORED")
        return
    if phase in {"EXCLUSION_OWNED", "CLEANED_EXCLUSION"}:
        owned_exclusion = journal.get("owned_exclusion")
        if not isinstance(owned_exclusion, dict):
            raise HarnessError("owned recovery exclusion evidence is absent")
        if phase == "EXCLUSION_OWNED":
            if _owned_exclusion_facts(nonce) != owned_exclusion:
                raise HarnessError("owned exclusion changed before recovery cleanup commit")
            _transition_recovery_journal(work, "EXCLUSION_OWNED", "CLEANED_EXCLUSION")
        if may_remove:
            _remove_owned_runtime_exclusion(nonce, owned_exclusion)
        _command(("/usr/bin/systemctl", "daemon-reload"))
        ordinary = _service_properties("dashcamd.service")
        if (
            EXCLUSION_DIRECTORY.exists()
            or EXCLUSION_DIRECTORY.is_symlink()
            or _unit_restore_facts(ordinary) != journal["prior_unit"]
        ):
            raise HarnessError("ordinary recorder state differs after recovery")
        _transition_recovery_journal(work, "CLEANED_EXCLUSION", "RESTORED")
        return
    if phase == "RESTORED":
        ordinary = _service_properties("dashcamd.service")
        if _unit_restore_facts(ordinary) != journal["prior_unit"]:
            raise HarnessError("restored recovery prior state differs")
        return
    raise HarnessError("unknown recovery exclusion phase")


def recover_owned_work(raw_work: Path) -> dict[str, object]:
    import pwd

    if sys.platform != "linux" or os.geteuid() != 0:
        raise HarnessError("owned recovery requires Linux root")
    work = raw_work
    if not work.is_absolute() or ".." in work.parts:
        raise HarnessError("recovery work path differs")
    _validate_work_identity(work)
    journal = _read_recovery_journal(work)
    nonce = work.name.removeprefix("dashcam-m10-private.")
    phase = cast(str, journal["phase"])
    exclusion_present = EXCLUSION_DIRECTORY.exists() or EXCLUSION_DIRECTORY.is_symlink()
    owned_exclusion_raw = journal.get("owned_exclusion")
    owned_exclusion = (
        cast(dict[str, object], owned_exclusion_raw)
        if isinstance(owned_exclusion_raw, dict)
        else None
    )
    may_remove = validate_recovery_exclusion_authority(
        phase,
        nonce=nonce,
        exclusion_present=exclusion_present,
        owned_exclusion=owned_exclusion,
    )
    _require_root_identity()
    for suffix in (
        "bind",
        "a",
        "rollback0",
        "rollback1",
        "rollback2",
        "rollback3",
        "b",
        "c",
    ):
        _remove_unit(f"dashcam-m10-private-{nonce}-{suffix}.service")

    specifications = (
        ("recording", "recording.exfat.img", EXFAT_IMAGE_BYTES, "exfat", RECORDING_LABEL),
        ("state", "state.ext4.img", EXT4_IMAGE_BYTES, "ext4", CATALOG_LABEL),
    )
    for target_name, image_name, expected_size, filesystem, label in specifications:
        target, image = work / target_name, work / image_name
        row = _mountpoint_or_none(target) if target.exists() else None
        loop = _associated_loop(image) if image.exists() else None
        if row is not None:
            source = row.get("source")
            if not isinstance(source, str) or LOOP_RE.fullmatch(source) is None:
                raise HarnessError("recovery mount source is not an owned loop")
            mounted_loop = Path(source)
            facts = _blkid(mounted_loop)
            if (
                loop is None
                or mounted_loop != loop
                or _loop_backing(loop).resolve() != image
                or row.get("target") != target.as_posix()
                or row.get("fstype") != filesystem
                or row.get("label") != label
                or row.get("uuid") != facts.get("UUID")
                or row.get("maj:min") != _block_device_id(loop)
            ):
                raise HarnessError("recovery mount/loop/backing identity differs")
            _unmount(target, loop)
        if loop is not None:
            metadata = os.lstat(image)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_size != expected_size
                or image.parent != work
                or _loop_backing(loop).resolve() != image
            ):
                raise HarnessError("recovery loop image identity differs")
            _detach(loop, image)

    runtime = Path("/run") / work.name
    _cleanup_runtime_recovery_directory(runtime, pwd.getpwnam("dashcam").pw_uid)
    for target_name, image_name, expected_size, _filesystem, _label in specifications:
        target, image = work / target_name, work / image_name
        if target.exists():
            if target.is_symlink() or any(target.iterdir()):
                raise HarnessError("recovery mountpoint is not empty")
            target.rmdir()
        if image.exists():
            metadata = os.lstat(image)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_size != expected_size
            ):
                raise HarnessError("recovery image identity differs")
            image.unlink()
    frozen = work / "bundle"
    if frozen.exists():
        if frozen.is_symlink() or not frozen.is_dir():
            raise HarnessError("recovery frozen bundle identity differs")
        for child in frozen.iterdir():
            if (
                child.name not in MANIFEST_MEMBERS | {"SHA256SUMS"}
                or child.is_symlink()
                or child.is_dir()
            ):
                raise HarnessError("recovery frozen bundle member differs")
            child.unlink()
        frozen.rmdir()
    if set(entry.name for entry in work.iterdir()) != {"RECOVERY.json"}:
        raise HarnessError("recovery work contains an unexpected member")

    _restore_recovery_exclusion(work, journal, phase, nonce, may_remove)
    _remove_recovery_authority(work)
    return {
        "schema_version": 2,
        "recovered": True,
        "work_removed": True,
        "exclusion_restored": True,
    }


def qualify(arguments: argparse.Namespace) -> dict[str, object]:
    bundle = Path(arguments.bundle).resolve(strict=True)
    output = _validate_result_destination(Path(arguments.output).resolve(strict=False))
    source = verify_bundle(
        bundle,
        arguments.expected_manifest_sha256,
        arguments.expected_harness_commit,
        arguments.expected_candidate_commit,
        arguments.rollback_commit,
    )
    dashcam_uid, dashcam_gid, storage_gid = _validate_pi(arguments.expected_board_serial)
    with _qualification_locks():
        _require_root_identity(minimum_free=ROOT_REQUIRED_FREE_BYTES)
        before = _host_snapshot()
        if EXCLUSION_DIRECTORY.exists() or EXCLUSION_DIRECTORY.is_symlink():
            raise HarnessError("ordinary recorder has a preexisting runtime drop-in")
        root_free_before = _space(Path("/"))[1]
        if not root_budget_satisfied(root_free_before):
            raise HarnessError("root lacks the reviewed image/overhead/reserve budget")
        nonce = uuid4().hex[:12]
        _require_root_identity(minimum_free=ROOT_REQUIRED_FREE_BYTES)
        work = Path(tempfile.mkdtemp(prefix="dashcam-m10-private.", dir="/var/tmp"))
        if work.name != f"dashcam-m10-private.{nonce}":
            # tempfile chooses its own suffix; normalize only before any child/image exists.
            requested = Path("/var/tmp") / f"dashcam-m10-private.{nonce}"
            work.rename(requested)
            work = requested
        prior_unit = before.get("dashcamd")
        if not isinstance(prior_unit, dict):
            raise HarnessError("ordinary recorder prior-state shape differs")
        _write_recovery_journal(work, nonce, cast(dict[str, str], prior_unit))
        try:
            frozen = _freeze_bundle(bundle, work)
        except BaseException:
            with contextlib.suppress(Exception):
                recover_owned_work(work)
            raise
        root_sampler = _RootSpaceSampler(root_free_before)
        root_sampler.start()
        paths: dict[str, Path] | None = None
        phase_results: dict[str, object] = {}
        minimum_root_free = _space(Path("/"))[1]
        try:
            with _runtime_exclusion(work) as exclusion_state:
                try:
                    _require_excluded(nonce, cast(dict[str, str], prior_unit))
                    paths, identity = _fresh_phase(
                        work,
                        frozen,
                        dashcam_uid=dashcam_uid,
                        dashcam_gid=dashcam_gid,
                        storage_gid=storage_gid,
                    )
                    minimum_root_free = min(minimum_root_free, _space(Path("/"))[1])
                    phase_results["A"] = _phase_a(
                        nonce,
                        paths,
                        identity,
                        dashcam_uid=dashcam_uid,
                    )
                    _require_excluded(nonce, cast(dict[str, str], prior_unit))
                    phase_results["D"] = _rollback_phase(nonce, paths)
                    _require_excluded(nonce, cast(dict[str, str], prior_unit))
                    _cleanup_fixture(paths)
                    _discard_fixture(paths)
                    paths = None

                    paths, identity = _fresh_phase(
                        work,
                        frozen,
                        dashcam_uid=dashcam_uid,
                        dashcam_gid=dashcam_gid,
                        storage_gid=storage_gid,
                    )
                    minimum_root_free = min(minimum_root_free, _space(Path("/"))[1])
                    phase_results["B"] = _protected_emergency_phase(
                        nonce,
                        paths,
                        identity,
                    )
                    _require_excluded(nonce, cast(dict[str, str], prior_unit))
                    _cleanup_fixture(paths)
                    _discard_fixture(paths)
                    paths = None

                    paths, _identity = _fresh_phase(
                        work,
                        frozen,
                        dashcam_uid=dashcam_uid,
                        dashcam_gid=dashcam_gid,
                        storage_gid=storage_gid,
                    )
                    minimum_root_free = min(minimum_root_free, _space(Path("/"))[1])
                    phase_results["C"] = _startup_bound_phase(nonce, paths)
                    _require_excluded(nonce, cast(dict[str, str], prior_unit))
                    _cleanup_fixture(paths)
                    _discard_fixture(paths)
                    paths = None
                finally:
                    if paths is not None:
                        with contextlib.suppress(Exception):
                            _cleanup_fixture(paths)
                        with contextlib.suppress(Exception):
                            _discard_fixture(paths)
                _remove_frozen(work, frozen)
                exclusion_state["restore_authorized"] = True
        finally:
            minimum_root_free = min(minimum_root_free, root_sampler.stop())
        root_free_after = _space(Path("/"))[1]
        after = _host_snapshot()
        _same_production(before, after)
        if root_free_after < ROOT_PRESERVED_FREE_BYTES:
            raise HarnessError("post-cleanup root reserve is below 2 GiB")
        non_image_delta = max(
            0,
            root_free_before - minimum_root_free - EXFAT_IMAGE_BYTES - EXT4_IMAGE_BYTES,
        )
        if non_image_delta > MAX_NON_IMAGE_ROOT_DELTA_BYTES:
            raise HarnessError("observed non-image root overhead exceeded 64 MiB")
        result: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "phase": "milestone10_private_production_runtime",
            "passed": all(
                isinstance(value, dict) and value.get("passed") is True
                for value in phase_results.values()
            ),
            "source": {
                "harness_commit": cast(dict[str, object], source["bundle"])["harness_commit"],
                "harness_tree": cast(dict[str, object], source["bundle"])["harness_tree"],
                "candidate_commit": cast(dict[str, object], source["candidate"])["git_commit"],
                "candidate_tree": cast(dict[str, object], source["candidate"])["git_tree"],
                "rollback_commit": cast(dict[str, object], source["rollback"])["git_commit"],
                "rollback_tree": cast(dict[str, object], source["rollback"])["git_tree"],
                "manifest_sha256": arguments.expected_manifest_sha256,
            },
            "fixtures": {
                "recording_image_bytes": EXFAT_IMAGE_BYTES,
                "state_image_bytes": EXT4_IMAGE_BYTES,
                "root_required_free_bytes": ROOT_REQUIRED_FREE_BYTES,
                "root_preserved_free_bytes": ROOT_PRESERVED_FREE_BYTES,
                "maximum_non_image_overhead_bytes": MAX_NON_IMAGE_ROOT_DELTA_BYTES,
                "observed_non_image_overhead_bytes": non_image_delta,
                "root_free_before_bytes": root_free_before,
                "root_free_after_bytes": root_free_after,
                "private_bind_paths": True,
                "findmnt_active_row_adapter_used": True,
                "production_paths_read_only_and_unchanged": True,
            },
            "phases": phase_results,
            "ordinary_recorder_exclusion": {
                "mechanism": "owned_runtime_drop_in",
                "drop_in_path_and_content_verified": True,
                "refuse_manual_start_observed": True,
                "condition_marker_absent": True,
                "conditions_property": "[unprintable]",
                "conditions_property_parsed": False,
            },
            "claims": {
                "production_candidate_runtime_tested": True,
                "production_camera_tested": True,
                "hardware_h264_tested": True,
                "production_listener_tested": True,
                "active_writing_reclaim_tested": True,
                "integrated_pending_finalize_startup_convergence_tested": True,
                "integrated_finalizing_startup_exclusion_tested": False,
                "camera_generated_finalizing_overlap_tested": False,
                "protected_only_emergency_tested": True,
                "startup_bound_tested": True,
                "rollback_quiesce_and_guard_tested": True,
                "download_data_plane_tested": False,
                "http_or_ui_tested": False,
                "physical_gps_tested": False,
                "physical_audio_tested": False,
                "physical_power_loss_tested": False,
            },
            "privacy": {
                "coordinates_retained": False,
                "raw_nmea_retained": False,
                "media_exported": False,
                "absolute_managed_paths_retained": False,
            },
        }
        if result["passed"] is not True:
            raise HarnessError("one private runtime phase did not pass")
        payload = canonical_json(result)
        if len(payload) > MAX_RESULT_BYTES:
            raise HarnessError("result exceeds its publication bound")
        _require_root_identity()
        _publish_result(output, payload)
        return result


def _exception_category(error: Exception) -> str:
    for expected, category in (
        (HarnessError, "HARNESS"),
        (sqlite3.Error, "SQLITE"),
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
    try:
        reviewed_path = Path(__file__).resolve(strict=True)
    except (OSError, RuntimeError):
        return result
    for function_code in DIAGNOSTIC_FUNCTIONS:
        function = globals().get("_" + function_code, globals().get(function_code))
        code = getattr(function, "__code__", None)
        if code is None:
            continue
        try:
            code_path = Path(code.co_filename).resolve(strict=True)
        except (OSError, RuntimeError):
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
    try:
        reviewed_path = Path(__file__).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    line_map = _reviewed_function_lines()
    selected: tuple[str, int] | None = None
    traceback = error.__traceback__
    while traceback is not None:
        try:
            frame_path = Path(traceback.tb_frame.f_code.co_filename).resolve(strict=True)
        except (OSError, RuntimeError):
            frame_path = Path()
        function_name = traceback.tb_frame.f_code.co_name.removeprefix("_")
        if frame_path == reviewed_path and traceback.tb_lineno in line_map.get(
            function_name, frozenset()
        ):
            selected = (function_name, traceback.tb_lineno)
        traceback = traceback.tb_next
    return selected


def _validated_refusal_location(payload: bytes) -> bytes | None:
    match = REFUSAL_LOCATION_RE.fullmatch(payload)
    if match is None:
        return None
    function = match.group(2).decode("ascii")
    line = int(match.group(3))
    if line not in _reviewed_function_lines().get(function, frozenset()):
        return None
    return payload


def _validated_fixture_child_refusal(payload: bytes) -> bytes | None:
    match = FIXTURE_CHILD_REFUSAL_RE.fullmatch(payload)
    if match is None:
        return None
    child = b"REFUSED: " + match.group(1) + b"\n"
    if _validated_refusal_location(child) != child:
        return None
    return payload


def _refusal_line(error: Exception) -> bytes:
    if isinstance(error, FixtureChildRefusal):
        child = _validated_refusal_location(error.child_refusal)
        if child is not None:
            payload = b"REFUSED: H_FIXTURE_CHILD child=" + child[9:-1] + b"\n"
            validated_child = _validated_fixture_child_refusal(payload)
            if validated_child is not None:
                return validated_child
    category = _exception_category(error)
    location = _reviewed_exception_location(error)
    if location is not None:
        function, line = location
        payload = f"REFUSED: H_{category}_F{function}_L{line}\n".encode("ascii")
        validated = _validated_refusal_location(payload)
        if validated is not None:
            return validated
    plain = {
        "HARNESS": "HarnessError",
        "SQLITE": "SQLiteError",
        "UNICODE": "UnicodeError",
        "ZIP": "BadZipFile",
        "OS": "OSError",
        "ASSERT": "AssertionError",
        "ATTRIBUTE": "AttributeError",
        "KEY": "KeyError",
        "RUNTIME": "RuntimeError",
        "TYPE": "TypeError",
        "VALUE": "ValueError",
        "EXCEPTION": "Exception",
    }[category]
    return f"REFUSED: {plain}\n".encode("ascii")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle")
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--expected-harness-commit")
    parser.add_argument("--expected-candidate-commit", default=EXPECTED_CANDIDATE)
    parser.add_argument("--rollback-commit", default=EXPECTED_ROLLBACK)
    parser.add_argument("--expected-board-serial")
    parser.add_argument("--output")
    parser.add_argument("--recover-work", type=Path)
    parser.add_argument("--fixture", choices=("A", "B", "C", "D"))
    parser.add_argument("--fixture-root", type=Path)
    parser.add_argument("--fixture-catalog", type=Path)
    parser.add_argument("--fixture-boot-id")
    parser.add_argument("--fixture-source", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    global _qualification_deadline_ns
    if Path(sys.argv[0]).name == "findmnt":
        return _findmnt_adapter(sys.argv[1:] if argv is None else argv)
    arguments = _parser().parse_args(argv)
    try:
        if arguments.recover_work is not None:
            if arguments.fixture is not None or arguments.bundle is not None:
                raise HarnessError("recovery mode is exclusive")
            with _qualification_locks():
                result = recover_owned_work(arguments.recover_work)
        elif arguments.fixture is not None:
            if any(
                value is None
                for value in (
                    arguments.fixture_root,
                    arguments.fixture_catalog,
                    arguments.fixture_boot_id,
                    arguments.fixture_source,
                )
            ):
                raise HarnessError("fixture subcommand arguments are incomplete")
            _source_environment(arguments.fixture_source)
            result = _seed_fixture(
                arguments.fixture,
                arguments.fixture_root.resolve(strict=True),
                arguments.fixture_catalog.resolve(strict=False),
                arguments.fixture_boot_id,
            )
        else:
            if any(
                value is None
                for value in (
                    arguments.bundle,
                    arguments.expected_manifest_sha256,
                    arguments.expected_harness_commit,
                    arguments.expected_board_serial,
                    arguments.output,
                )
            ):
                raise HarnessError("qualification arguments are incomplete")
            _qualification_deadline_ns = (
                time.monotonic_ns() + QUALIFICATION_TIMEOUT_S * 1_000_000_000
            )
            try:
                result = qualify(arguments)
            finally:
                _qualification_deadline_ns = None
    except Exception as error:
        sys.stderr.write(_refusal_line(error).decode("ascii"))
        sys.stderr.flush()
        return 2
    print(canonical_json(result).decode("ascii"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
