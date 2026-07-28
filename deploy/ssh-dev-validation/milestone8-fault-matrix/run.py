#!/usr/bin/env python3
"""Bounded exact-Pi Milestone 8 GPS/time/reconciliation fault matrix.

The harness runs the installed production daemon in one transient systemd unit
against a PTY-backed NMEA source.  The ordinary ``dashcamd.service`` remains
inactive.  Product clips and their durable catalog evidence are preserved;
only the temporary unit, configuration, PTY link, and an isolated collision
fixture are removed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence, Set
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Final, cast
from uuid import UUID, uuid4

import dashcam
from dashcam.config import (
    DashcamConfig,
    load_config,
    write_config_atomic,
)
from dashcam.diagnostics.media import CommandResult, run_fixed_argv
from dashcam.metadata.reconcile import (
    MetadataReconciliationError,
    parse_sidecar_bytes,
    plan_post_anchor_reconciliation,
)
from dashcam.metadata.schema import (
    AudioSummary,
    ClipSidecar,
    GpsSummary,
    TimeAnchor,
    TimeAnchorSource,
    VideoSummary,
)
from dashcam.state import (
    GpsTimeState,
    SystemClockState,
    TimestampQuality,
)
from dashcam.storage.naming import finalized_unsynced_clip_pair

CONFIG_PATH: Final = Path("/etc/dashcam/config.toml")
IDENTITY_PATH: Final = Path("/etc/dashcam/storage-volume.env")
RECORDING_ROOT: Final = Path("/srv/dashcam")
CLIPS_ROOT: Final = RECORDING_ROOT / "clips"
QUARANTINE_ROOT: Final = RECORDING_ROOT / "quarantine"
CATALOG_PATH: Final = Path("/var/lib/dashcam/catalog.sqlite3")
STATUS_PATH: Final = Path("/run/dashcam/status.json")
TEMP_ROOT: Final = Path("/run/dashcam-m8-fault-matrix")
TEMP_CONFIG_PATH: Final = TEMP_ROOT / "config.toml"
GPS_LINK: Final = Path("/dev/dashcam-m8-fault-matrix")
UNIT_PATH: Final = Path("/run/systemd/system/dashcam-m8-fault-matrix.service")
UNIT_NAME: Final = UNIT_PATH.name
LOCK_PATH: Final = Path("/run/lock/dashcam-m8-fault-matrix.lock")
SYSTEMCTL: Final = "/usr/bin/systemctl"
FINDMNT: Final = "/usr/bin/findmnt"
VCGENCMD: Final = "/usr/bin/vcgencmd"
MANIFEST_MEMBERS: Final = ("README.md", "run.py")

EXPECTED_MODEL_PREFIX: Final = "Raspberry Pi Zero 2 W"
RELEASE_RE: Final = re.compile(r"0\.1\.0\.dev0-[0-9a-f]{16}")
SERIAL_RE: Final = re.compile(r"[0-9a-f]{16}")
UUID_RE: Final = re.compile(r"[0-9A-F]{4}-[0-9A-F]{4}")
SHA256_RE: Final = re.compile(r"[0-9a-f]{64}")
SAFE_DEVICE_RE: Final = re.compile(r"/dev/pts/[0-9]{1,6}")
SAFE_RELEASE_PATH_RE: Final = re.compile(
    r"/opt/dashcam/releases/0\.1\.0\.dev0-[0-9a-f]{16}/venv/bin/python"
)

MAX_MANIFEST_BYTES: Final = 4 * 1024
MAX_SCRIPT_BYTES: Final = 2 * 1024 * 1024
MAX_RESULT_BYTES: Final = 1024 * 1024
MAX_STATUS_BYTES: Final = 64 * 1024
MAX_SIDECAR_BYTES: Final = 512 * 1024
MAX_COMMAND_OUTPUT_BYTES: Final = 16 * 1024
MAX_CLIP_ENTRIES: Final = 4096
MAX_NEW_SIDECARS: Final = 16
MAX_SENTENCES: Final = 2048
CLIP_DURATION_S: Final = 60
WATCHDOG_S: Final = 2
STALE_AFTER_S: Final = 2.0
POLL_INTERVAL_S: Final = 0.25
START_TIMEOUT_S: Final = 45.0
PHASE_TIMEOUT_S: Final = 75.0
SCENARIO_TIMEOUT_S: Final = 200.0
STOP_TIMEOUT_S: Final = 35.0
FEED_INTERVAL_S: Final = 0.2
BASE_UTC: Final = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
COLLISION_CLIP_ID: Final = UUID("82345678-1234-5678-9234-567812345678")
COLLISION_BOOT_ID: Final = UUID("87654321-4321-6789-a234-678943216789")
COLLISION_INTENT_ID: Final = UUID("8aaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
PRIVACY_FORBIDDEN_KEYS: Final = frozenset(
    {
        "lat",
        "latitude",
        "latitude_deg",
        "lat_deg",
        "lon",
        "longitude",
        "longitude_deg",
        "lon_deg",
        "coordinates",
        "raw_nmea",
        "samples",
    }
)


class HarnessError(RuntimeError):
    """The requested exact-Pi qualification is unsafe, incomplete, or refused."""


def _detail(value: object, maximum: int = 512) -> str:
    text = " ".join(str(value).replace("\0", " ").splitlines())
    return "".join(character if character.isprintable() else " " for character in text)[
        :maximum
    ]


def _regular_bytes(path: Path, maximum: int) -> bytes:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise HarnessError(f"{path} is not a regular file")
        payload = bytearray()
        while chunk := os.read(descriptor, min(65536, maximum + 1 - len(payload))):
            payload.extend(chunk)
            if len(payload) > maximum:
                raise HarnessError(f"{path} exceeded its read bound")
        return bytes(payload)
    finally:
        os.close(descriptor)


def _sha256(path: Path, maximum: int) -> str:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    digest = hashlib.sha256()
    size = 0
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise HarnessError(f"{path} is not a regular file")
        while chunk := os.read(descriptor, 1024 * 1024):
            size += len(chunk)
            if size > maximum:
                raise HarnessError(f"{path} exceeded its hash bound")
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def verify_manifest(expected_sha256: str, directory: Path | None = None) -> dict[str, str]:
    """Verify the reviewed, closed two-member harness bundle."""

    if SHA256_RE.fullmatch(expected_sha256) is None:
        raise HarnessError("expected manifest SHA-256 is not canonical")
    root = (directory or Path(__file__).resolve().parent).resolve(strict=True)
    manifest = root / "SHA256SUMS"
    if _sha256(manifest, MAX_MANIFEST_BYTES) != expected_sha256:
        raise HarnessError("reviewed manifest hash differs from supplied hash")
    entries: dict[str, str] = {}
    for line in _regular_bytes(manifest, MAX_MANIFEST_BYTES).decode("ascii").splitlines():
        digest, separator, name = line.partition("  ")
        if (
            not separator
            or SHA256_RE.fullmatch(digest) is None
            or name in entries
            or name not in MANIFEST_MEMBERS
            or Path(name).name != name
        ):
            raise HarnessError("manifest member set is not closed")
        entries[name] = digest
    if tuple(sorted(entries)) != MANIFEST_MEMBERS:
        raise HarnessError("manifest omits a required member")
    for name, digest in entries.items():
        if _sha256(root / name, MAX_SCRIPT_BYTES) != digest:
            raise HarnessError(f"manifest member {name} failed verification")
    return entries


def _command(
    argv: Sequence[str],
    *,
    timeout: float = 5.0,
    allowed_returncodes: Set[int] = frozenset({0}),
) -> CommandResult:
    result = run_fixed_argv(
        argv,
        timeout_seconds=timeout,
        max_output_bytes=MAX_COMMAND_OUTPUT_BYTES,
    )
    if (
        result.returncode not in allowed_returncodes
        or result.timed_out
        or result.output_truncated
    ):
        raise HarnessError(
            f"bounded command failed: {_detail(result.argv)}: {_detail(result.stderr)}"
        )
    return result


def _systemctl(*arguments: str, timeout: float = 10.0) -> CommandResult:
    return _command((SYSTEMCTL, *arguments), timeout=timeout)


def _unit_properties(unit: str) -> dict[str, str]:
    names = (
        "LoadState",
        "ActiveState",
        "SubState",
        "MainPID",
        "NRestarts",
        "Result",
        "ExecMainStatus",
    )
    result = _systemctl(
        "show",
        "--no-pager",
        *(f"--property={name}" for name in names),
        unit,
    )
    values = {}
    for line in result.stdout.decode("ascii", "strict").splitlines():
        key, separator, value = line.partition("=")
        if separator and key in names and key not in values:
            values[key] = value
    if set(values) != set(names):
        raise HarnessError(f"systemd property shape differs for {unit}")
    if not values["MainPID"].isdigit() or not values["NRestarts"].isdigit():
        raise HarnessError(f"systemd numeric property shape differs for {unit}")
    return values


def _inactive_unit(unit: str) -> dict[str, str]:
    values = _unit_properties(unit)
    if (
        values["LoadState"] != "loaded"
        or values["ActiveState"] != "inactive"
        or values["SubState"] != "dead"
        or values["MainPID"] != "0"
    ):
        raise HarnessError(f"{unit} must be exactly loaded/inactive/dead")
    return values


def _throttle() -> str:
    result = _command((VCGENCMD, "get_throttled"))
    value = result.stdout.decode("ascii", "strict").strip()
    if re.fullmatch(r"throttled=0x[0-9a-fA-F]+", value) is None:
        raise HarnessError("throttle result shape differs")
    return value


def _strict_json(payload: bytes, label: str) -> Mapping[str, object]:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise HarnessError(f"{label} contains duplicate keys")
            result[key] = value
        return result

    try:
        decoded = json.loads(payload, object_pairs_hook=unique)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HarnessError(f"{label} is invalid JSON") from error
    if not isinstance(decoded, Mapping):
        raise HarnessError(f"{label} is not a JSON object")
    return cast(Mapping[str, object], decoded)


def _status() -> Mapping[str, object]:
    document = _strict_json(_regular_bytes(STATUS_PATH, MAX_STATUS_BYTES), "runtime status")
    if set(document) != {"schema_version", "lifecycle", "runtime"}:
        raise HarnessError("runtime status top-level schema differs")
    if document["schema_version"] != 2:
        raise HarnessError("runtime status schema version differs")
    if not isinstance(document["lifecycle"], Mapping) or not isinstance(
        document["runtime"], Mapping
    ):
        raise HarnessError("runtime status sections are absent")
    return document


def _runtime(document: Mapping[str, object]) -> Mapping[str, object]:
    runtime = document.get("runtime")
    if not isinstance(runtime, Mapping):
        raise HarnessError("runtime status has no runtime object")
    return cast(Mapping[str, object], runtime)


def _nested(document: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = document.get(key)
    if not isinstance(value, Mapping):
        raise HarnessError(f"runtime status has no {key} object")
    return cast(Mapping[str, object], value)


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise HarnessError(f"{label} is not a non-negative integer")
    return value


def _summary(document: Mapping[str, object], phase: str) -> dict[str, object]:
    """Retain only privacy-safe state/counters; never retain navigation values."""

    lifecycle = _nested(document, "lifecycle")
    runtime = _runtime(document)
    frames = _nested(runtime, "frames")
    gps = _nested(runtime, "gps")
    gps_time = _nested(gps, "time")
    counters = _nested(gps, "counters")
    metadata = _nested(runtime, "metadata_reconciliation")
    return {
        "phase": phase,
        "observed_monotonic_ns": time.monotonic_ns(),
        "lifecycle_state": lifecycle.get("state"),
        "frames": {
            "encoded": frames.get("encoded"),
            "dropped": frames.get("dropped"),
        },
        "pipeline_restart_count": runtime.get("pipeline_restart_count"),
        "gps": {
            "state": gps.get("state"),
            "connected": gps.get("connected"),
            "navigation_present": gps.get("navigation") is not None,
            "time_state": gps_time.get("state"),
            "anchor_present": gps_time.get("anchor") is not None,
            "last_anchor_status": gps_time.get("last_status"),
            "last_anchor_error": gps_time.get("last_error"),
            "last_parse_error": gps.get("last_parse_error"),
            "counters": {
                name: counters.get(name)
                for name in (
                    "connections",
                    "reconnects",
                    "disconnects",
                    "transport_errors",
                    "parse_errors",
                    "checksum_failures",
                    "valid_fixes",
                    "stale_transitions",
                    "anchor_acceptances",
                    "anchor_confirmations",
                    "anchor_rejections",
                )
            },
        },
        "metadata_reconciliation": {
            name: metadata.get(name)
            for name in ("completed", "failures", "backlog", "overflows")
        },
    }


def _startup_failure_summary(document: Mapping[str, object]) -> dict[str, object]:
    """Retain bounded daemon failure state even before a runtime snapshot exists."""

    lifecycle = document.get("lifecycle")
    if not isinstance(lifecycle, Mapping):
        raise HarnessError("startup failure status has no lifecycle object")
    return {
        "phase": "transient_start_failure",
        "observed_monotonic_ns": time.monotonic_ns(),
        "lifecycle_state": lifecycle.get("state"),
        "reason": lifecycle.get("reason"),
        "detail": lifecycle.get("detail"),
        "runtime_snapshot_present": isinstance(document.get("runtime"), Mapping),
    }


def _wait_status(
    predicate: Callable[[Mapping[str, object]], bool],
    *,
    timeout: float,
    phase: str,
) -> Mapping[str, object]:
    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        unit = _unit_properties(UNIT_NAME)
        if unit["ActiveState"] == "failed" or (
            unit["ActiveState"] == "inactive" and unit["SubState"] == "dead"
        ):
            raise HarnessError(f"transient recorder terminated during {phase}: {unit['Result']}")
        try:
            observed = _status()
            if predicate(observed):
                return observed
        except (HarnessError, FileNotFoundError, PermissionError) as error:
            last_error = error
        time.sleep(POLL_INTERVAL_S)
    detail = "" if last_error is None else f": {_detail(last_error)}"
    raise HarnessError(f"runtime status wait timed out during {phase}{detail}")


def _recording(document: Mapping[str, object]) -> bool:
    lifecycle = document.get("lifecycle")
    runtime = document.get("runtime")
    if not isinstance(lifecycle, Mapping) or not isinstance(runtime, Mapping):
        return False
    frames = runtime.get("frames")
    return (
        lifecycle.get("state") == "RECORDING"
        and isinstance(frames, Mapping)
        and isinstance(frames.get("encoded"), int)
        and cast(int, frames["encoded"]) >= 1
        and runtime.get("pipeline_restart_count") == 0
    )


def _gps(document: Mapping[str, object]) -> Mapping[str, object]:
    return _nested(_runtime(document), "gps")


def _gps_counters(document: Mapping[str, object]) -> Mapping[str, object]:
    return _nested(_gps(document), "counters")


def _gps_time(document: Mapping[str, object]) -> Mapping[str, object]:
    return _nested(_gps(document), "time")


def _frame_counts(document: Mapping[str, object]) -> tuple[int, int | None]:
    frames = _nested(_runtime(document), "frames")
    dropped = frames.get("dropped")
    return (
        _integer(frames.get("encoded"), "encoded frames"),
        None if dropped is None else _integer(dropped, "dropped frames"),
    )


def _nmea(body: str) -> bytes:
    if (
        not body
        or len(body) > 76
        or not body.isascii()
        or not body.isprintable()
        or "$" in body
        or "*" in body
    ):
        raise HarnessError("NMEA body is outside the closed synthetic bound")
    checksum = 0
    for character in body:
        checksum ^= ord(character)
    return f"${body}*{checksum:02X}\r\n".encode("ascii")


def _rmc(utc: datetime, *, valid: bool = True) -> bytes:
    value = utc.astimezone(UTC)
    status = "A" if valid else "V"
    body = (
        f"GNRMC,{value:%H%M%S}.00,{status},0000.0000,N,00000.0000,E,"
        f"0.0,0.0,{value:%d%m%y},,,A"
    )
    return _nmea(body)


def _bad_checksum(record: bytes) -> bytes:
    replacement = ord("1") if len(record) < 5 or record[-4] == ord("0") else ord("0")
    changed = bytearray(record)
    changed[-4] = replacement
    return bytes(changed)


class PtyEndpoint:
    """One exact master/slave PTY generation installed below ``/dev``."""

    def __init__(self, *, group_id: int) -> None:
        if not hasattr(os, "openpty"):
            raise HarnessError("PTY support is unavailable")
        master, slave = os.openpty()
        self.master = master
        self.slave_name = os.ttyname(slave)
        try:
            if SAFE_DEVICE_RE.fullmatch(self.slave_name) is None:
                raise HarnessError("PTY slave path shape differs")
            os.fchown(slave, 0, group_id)
            os.fchmod(slave, 0o660)
        finally:
            os.close(slave)
        self.closed = False

    def install(self) -> None:
        temporary = GPS_LINK.with_name(f".{GPS_LINK.name}.{os.getpid()}.tmp")
        if os.path.lexists(temporary):
            raise HarnessError("temporary GPS link unexpectedly exists")
        try:
            os.symlink(self.slave_name, temporary)
            os.replace(temporary, GPS_LINK)
        finally:
            if os.path.lexists(temporary):
                os.unlink(temporary)
        if os.readlink(GPS_LINK) != self.slave_name:
            raise HarnessError("GPS PTY link readback differs")

    def write(self, payload: bytes) -> None:
        if self.closed or not payload or len(payload) > 82:
            raise HarnessError("PTY write request is invalid")
        position = 0
        while position < len(payload):
            written = os.write(self.master, payload[position:])
            if written <= 0:
                raise HarnessError("PTY write made no progress")
            position += written

    def close(self) -> None:
        if not self.closed:
            os.close(self.master)
            self.closed = True


class NmeaFeeder:
    """Bounded synthetic RMC feeder whose UTC follows monotonic elapsed time."""

    def __init__(
        self,
        endpoint: PtyEndpoint,
        *,
        origin_monotonic: float | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._origin = time.monotonic() if origin_monotonic is None else origin_monotonic
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.sent = 0
        self.error: str | None = None

    @property
    def origin_monotonic(self) -> float:
        return self._origin

    def current_utc(self, offset_s: float = 0.0) -> datetime:
        return BASE_UTC + timedelta(seconds=time.monotonic() - self._origin + offset_s)

    def send(self, payload: bytes) -> None:
        if self.sent >= MAX_SENTENCES:
            raise HarnessError("synthetic sentence count exceeded its hard bound")
        self._endpoint.write(payload)
        self.sent += 1

    def send_current(self, offset_s: float = 0.0) -> None:
        self.send(_rmc(self.current_utc(offset_s)))

    def start(self) -> None:
        if self._thread is not None:
            raise HarnessError("NMEA feeder is already started")
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="m8-nmea-feeder",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        try:
            while not self._stop.wait(FEED_INTERVAL_S):
                self.send_current()
        except BaseException as error:
            self.error = _detail(f"{type(error).__name__}: {error}")

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
            if thread.is_alive():
                raise HarnessError("NMEA feeder failed to stop boundedly")
        self._thread = None
        if self.error is not None:
            raise HarnessError(f"NMEA feeder failed: {self.error}")


def _dashcam_group_id() -> int:
    if os.name != "posix":
        raise HarnessError("exact-Pi qualification requires POSIX")
    import grp

    return grp.getgrnam("dashcam").gr_gid


def _temporary_config(base: DashcamConfig) -> DashcamConfig:
    """Change only bounded validation timing/device fields."""

    return replace(
        base,
        video=replace(base.video, clip_duration_s=CLIP_DURATION_S),
        gps=replace(base.gps, device=str(GPS_LINK), stale_after_s=STALE_AFTER_S),
        service=replace(base.service, watchdog_s=WATCHDOG_S),
    )


def render_transient_unit(*, interpreter: Path, config_path: Path) -> str:
    """Render the closed transient unit used only by this harness."""

    interpreter_text = interpreter.as_posix()
    if SAFE_RELEASE_PATH_RE.fullmatch(interpreter_text) is None:
        raise HarnessError("transient unit interpreter path is not one installed release")
    if config_path != TEMP_CONFIG_PATH:
        raise HarnessError("transient unit config path differs")
    return f"""[Unit]
Description=Dashcam Milestone 8 transient fault-matrix recorder
After=local-fs.target

[Service]
Type=notify
NotifyAccess=main
User=dashcam
Group=dashcam
SupplementaryGroups=audio video render dialout dashcam-storage
WorkingDirectory=/var/lib/dashcam
ExecStart={interpreter_text} -m dashcam.daemon --config {config_path} --identity {IDENTITY_PATH}
Restart=no
TimeoutStartSec=45s
TimeoutStopSec=30s
WatchdogSec=5s
RuntimeDirectory=dashcam
RuntimeDirectoryMode=0750
RuntimeDirectoryPreserve=yes
UMask=0027
NoNewPrivileges=yes
CapabilityBoundingSet=
AmbientCapabilities=
PrivateTmp=yes
PrivateDevices=no
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
RestrictSUIDSGID=yes
RestrictNamespaces=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
RestrictAddressFamilies=AF_UNIX
ReadWritePaths=/var/lib/dashcam /srv/dashcam /run/dashcam
BindPaths=/srv/dashcam
"""


def _write_exclusive(path: Path, payload: bytes, mode: int) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags, mode)
    try:
        position = 0
        while position < len(payload):
            written = os.write(descriptor, payload[position:])
            if written <= 0:
                raise HarnessError("exclusive write made no progress")
            position += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _prepare_transient_files(config: DashcamConfig, interpreter: Path, group_id: int) -> None:
    if (
        TEMP_ROOT.exists()
        or TEMP_ROOT.is_symlink()
        or UNIT_PATH.exists()
        or UNIT_PATH.is_symlink()
        or os.path.lexists(GPS_LINK)
        or STATUS_PATH.parent.exists()
    ):
        raise HarnessError("a transient validation path already exists")
    TEMP_ROOT.mkdir(mode=0o750)
    os.chown(TEMP_ROOT, 0, group_id)
    write_config_atomic(TEMP_CONFIG_PATH, config)
    os.chown(TEMP_CONFIG_PATH, 0, group_id)
    os.chmod(TEMP_CONFIG_PATH, 0o640)
    unit = render_transient_unit(
        interpreter=interpreter,
        config_path=TEMP_CONFIG_PATH,
    ).encode("ascii")
    _write_exclusive(UNIT_PATH, unit, 0o644)
    _systemctl("daemon-reload")


def _remove_transient_files(endpoint: PtyEndpoint | None) -> list[str]:
    failures: list[str] = []
    try:
        properties = _unit_properties(UNIT_NAME)
        if properties["LoadState"] == "loaded" and properties["ActiveState"] != "inactive":
            _systemctl("stop", UNIT_NAME, timeout=STOP_TIMEOUT_S)
    except BaseException as error:
        failures.append(_detail(f"temporary unit stop failed: {error}"))
    try:
        if os.path.lexists(GPS_LINK):
            target = os.readlink(GPS_LINK)
            if SAFE_DEVICE_RE.fullmatch(target) is None:
                raise HarnessError("refusing to remove a foreign GPS link")
            os.unlink(GPS_LINK)
    except BaseException as error:
        failures.append(_detail(f"GPS link cleanup failed: {error}"))
    if endpoint is not None:
        try:
            endpoint.close()
        except BaseException as error:
            failures.append(_detail(f"PTY cleanup failed: {error}"))
    try:
        properties = _unit_properties(UNIT_NAME)
        if properties["LoadState"] == "loaded" and properties["ActiveState"] == "failed":
            _systemctl("reset-failed", UNIT_NAME)
    except BaseException as error:
        failures.append(_detail(f"temporary unit reset failed: {error}"))
    try:
        if UNIT_PATH.exists() and not UNIT_PATH.is_symlink():
            UNIT_PATH.unlink()
        elif UNIT_PATH.is_symlink():
            raise HarnessError("refusing to remove a symlink unit")
        _systemctl("daemon-reload")
    except BaseException as error:
        failures.append(_detail(f"temporary unit-file cleanup failed: {error}"))
    try:
        if TEMP_CONFIG_PATH.exists() and not TEMP_CONFIG_PATH.is_symlink():
            TEMP_CONFIG_PATH.unlink()
        elif TEMP_CONFIG_PATH.is_symlink():
            raise HarnessError("refusing to remove a symlink config")
        if TEMP_ROOT.exists():
            TEMP_ROOT.rmdir()
    except BaseException as error:
        failures.append(_detail(f"temporary config cleanup failed: {error}"))
    try:
        runtime_root = STATUS_PATH.parent
        if runtime_root.exists():
            if runtime_root.is_symlink() or not runtime_root.is_dir():
                raise HarnessError("refusing to remove a foreign status runtime path")
            entries = tuple(runtime_root.iterdir())
            if any(entry != STATUS_PATH for entry in entries):
                raise HarnessError("status runtime directory contains a foreign entry")
            if STATUS_PATH.exists():
                if STATUS_PATH.is_symlink() or not STATUS_PATH.is_file():
                    raise HarnessError("refusing to remove a foreign status file")
                STATUS_PATH.unlink()
            runtime_root.rmdir()
    except BaseException as error:
        failures.append(_detail(f"status runtime cleanup failed: {error}"))
    return failures


def _release_identity(expected_release: str) -> dict[str, str]:
    if RELEASE_RE.fullmatch(expected_release) is None:
        raise HarnessError("expected release ID is not canonical")
    prefix = Path(sys.prefix).resolve(strict=True)
    package = Path(dashcam.__file__).resolve(strict=True)
    release_root = Path("/opt/dashcam/releases") / expected_release
    expected_prefix = release_root / "venv"
    if prefix != expected_prefix or not package.is_relative_to(prefix):
        raise HarnessError("interpreter/package are not the expected installed release")
    marker = _strict_json(
        _regular_bytes(release_root / "installed.json", 8 * 1024),
        "installed release marker",
    )
    if (
        set(marker) != {"schema_version", "release_id", "manifest_sha256"}
        or marker.get("schema_version") != 1
        or marker.get("release_id") != expected_release
        or not isinstance(marker.get("manifest_sha256"), str)
        or SHA256_RE.fullmatch(cast(str, marker["manifest_sha256"])) is None
    ):
        raise HarnessError("installed release marker differs from the expected release")
    applied = _strict_json(
        _regular_bytes(Path("/var/lib/dashcam/app-install-v1.json"), 256 * 1024),
        "application installation journal",
    )
    if (
        applied.get("schema_version") != 1
        or applied.get("mode") != "applied"
        or applied.get("ready") is not True
        or applied.get("release_id") != expected_release
        or applied.get("manifest_sha256") != marker["manifest_sha256"]
    ):
        raise HarnessError("application installation journal differs from the release marker")
    interpreter = expected_prefix / "bin/python"
    if not interpreter.exists():
        raise HarnessError("installed release interpreter is absent")
    current = Path("/opt/dashcam/current")
    if current.resolve(strict=True) != release_root:
        raise HarnessError("current release symlink differs from expected release")
    return {
        "release": expected_release,
        "interpreter": interpreter.as_posix(),
        "package": package.as_posix(),
        "package_version": dashcam.__version__,
        "manifest_sha256": cast(str, marker["manifest_sha256"]),
    }


def _board_identity(expected_serial: str) -> dict[str, str]:
    if SERIAL_RE.fullmatch(expected_serial) is None:
        raise HarnessError("expected board serial is not canonical")
    model = _regular_bytes(Path("/proc/device-tree/model"), 256).rstrip(b"\0").decode("ascii")
    if not model.startswith(EXPECTED_MODEL_PREFIX):
        raise HarnessError("board model is not the declared Pi Zero 2 W")
    serial: str | None = None
    for line in _regular_bytes(Path("/proc/cpuinfo"), 256 * 1024).decode("ascii").splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() == "Serial":
            serial = value.strip().casefold()
            break
    if serial != expected_serial:
        raise HarnessError("board serial differs from the declared exact Pi")
    return {"model": model, "serial": expected_serial}


def _storage_identity(expected_uuid: str) -> dict[str, str]:
    if UUID_RE.fullmatch(expected_uuid) is None:
        raise HarnessError("expected exFAT UUID is not canonical")
    result = _command(
        (
            FINDMNT,
            "-n",
            "-o",
            "TARGET,FSTYPE,LABEL,UUID,SOURCE",
            "--target",
            str(RECORDING_ROOT),
        )
    )
    fields = result.stdout.decode("ascii", "strict").strip().split()
    if len(fields) != 5:
        raise HarnessError("recording mount identity shape differs")
    target, filesystem, label, uuid, source = fields
    if (
        target != str(RECORDING_ROOT)
        or filesystem.casefold() != "exfat"
        or label != "DASHCAM"
        or uuid != expected_uuid
        or source != "/dev/mmcblk0p3"
    ):
        raise HarnessError("recording mount is not the declared exact exFAT partition")
    root_stat = os.stat("/")
    recording_stat = os.stat(RECORDING_ROOT)
    if root_stat.st_dev == recording_stat.st_dev:
        raise HarnessError("recording root is not a distinct filesystem")
    return {
        "target": target,
        "filesystem": filesystem,
        "label": label,
        "uuid": uuid,
        "source": source,
        "device_id": f"{os.major(recording_stat.st_dev)}:{os.minor(recording_stat.st_dev)}",
    }


def _clock_owner() -> dict[str, str]:
    timesyncd = _unit_properties("systemd-timesyncd.service")
    if timesyncd["ActiveState"] != "active" or timesyncd["SubState"] != "running":
        raise HarnessError("systemd-timesyncd is not the active wall-clock owner")
    inactive = {}
    for unit in ("chrony.service", "ntp.service", "ntpsec.service", "gpsd.service"):
        result = _command(
            (SYSTEMCTL, "is-active", unit),
            allowed_returncodes=frozenset({0, 3, 4}),
        )
        state = result.stdout.decode("ascii", "strict").strip()
        if state == "active":
            raise HarnessError(f"competing clock service is active: {unit}")
        inactive[unit] = state
    return {"owner": "systemd-timesyncd", **inactive}


def _clip_names() -> set[str]:
    root = os.lstat(CLIPS_ROOT)
    recording = os.lstat(RECORDING_ROOT)
    if (
        not stat.S_ISDIR(root.st_mode)
        or stat.S_ISLNK(root.st_mode)
        or root.st_dev != recording.st_dev
    ):
        raise HarnessError("clips directory is not a real directory on the recording volume")
    names: set[str] = set()
    with os.scandir(CLIPS_ROOT) as entries:
        for entry in entries:
            if (
                len(names) >= MAX_CLIP_ENTRIES
                or not entry.name
                or not entry.name.isascii()
                or not entry.name.isprintable()
                or len(entry.name) > 255
            ):
                raise HarnessError("clips directory entry set exceeds its safety bound")
            names.add(entry.name)
    return names


def _new_provisional_sidecars(before: Set[str]) -> list[ClipSidecar]:
    names = sorted(
        name
        for name in (_clip_names() - before)
        if name.startswith("boot-") and name.endswith(".json")
    )
    if len(names) > MAX_NEW_SIDECARS:
        raise HarnessError("new provisional sidecar count exceeded its hard bound")
    return [
        parse_sidecar_bytes(_regular_bytes(CLIPS_ROOT / name, MAX_SIDECAR_BYTES))
        for name in names
    ]


def _wait_provisional(before: Set[str]) -> ClipSidecar:
    deadline = time.monotonic() + PHASE_TIMEOUT_S
    while time.monotonic() < deadline:
        for sidecar in _new_provisional_sidecars(before):
            if (
                sidecar.start_utc is None
                and sidecar.end_utc is None
                and sidecar.start_local is None
                and sidecar.timestamp_quality is TimestampQuality.MONOTONIC_ONLY
                and sidecar.time_anchor is None
                and sidecar.video_file.startswith("boot-")
            ):
                return sidecar
        time.sleep(POLL_INTERVAL_S)
    raise HarnessError("no finalized provisional production sidecar appeared")


def _catalog_sidecar(clip_id: UUID) -> ClipSidecar | None:
    uri = f"file:{CATALOG_PATH.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=1.0) as connection:
        rows = connection.execute(
            "SELECT sidecar_path FROM clips WHERE clip_id = ?",
            (str(clip_id),),
        ).fetchall()
    if not rows:
        return None
    if len(rows) != 1 or len(rows[0]) != 1 or not isinstance(rows[0][0], str):
        raise HarnessError("stable clip UUID catalog lookup has an invalid shape")
    relative = PurePosixPath(rows[0][0])
    if (
        len(relative.parts) != 2
        or relative.parts[0] != "clips"
        or relative.name in {"", ".", ".."}
        or not relative.name.isascii()
        or not relative.name.isprintable()
        or len(relative.name) > 255
        or not relative.name.endswith(".json")
    ):
        raise HarnessError("stable clip UUID catalog path is outside the clips contract")
    try:
        sidecar_bytes = _regular_bytes(CLIPS_ROOT / relative.name, MAX_SIDECAR_BYTES)
    except FileNotFoundError:
        # Reconciliation renames the pair before committing its catalog path.
        # A bounded caller retry observes either the old pair or the new row.
        return None
    sidecar = parse_sidecar_bytes(sidecar_bytes)
    if sidecar.clip_id != clip_id:
        raise HarnessError("stable clip UUID differs between catalog and sidecar")
    return sidecar


def _wait_reconciled(clip_id: UUID) -> ClipSidecar:
    deadline = time.monotonic() + PHASE_TIMEOUT_S
    while time.monotonic() < deadline:
        sidecar = _catalog_sidecar(clip_id)
        if sidecar is not None and sidecar.start_utc is not None:
            return sidecar
        time.sleep(POLL_INTERVAL_S)
    raise HarnessError("provisional sidecar was not durably reconciled")


def _sidecar_evidence(sidecar: ClipSidecar, source: ClipSidecar) -> dict[str, object]:
    sample_monotonic = [sample.monotonic_ns for sample in sidecar.gps.samples]
    return {
        "clip_id": str(sidecar.clip_id),
        "source_clip_id": str(source.clip_id),
        "stable_uuid": sidecar.clip_id == source.clip_id,
        "sequence": sidecar.sequence,
        "source_video": source.video_file,
        "source_metadata": source.metadata_file,
        "target_video": sidecar.video_file,
        "target_metadata": sidecar.metadata_file,
        "source_members_absent": not (CLIPS_ROOT / source.video_file).exists()
        and not (CLIPS_ROOT / source.metadata_file).exists(),
        "target_members_present": (CLIPS_ROOT / sidecar.video_file).is_file()
        and (CLIPS_ROOT / sidecar.metadata_file).is_file(),
        "pair_names_match": (
            sidecar.video_file.removesuffix(".mp4")
            == sidecar.metadata_file.removesuffix(".json")
        ),
        "start_utc_present": sidecar.start_utc is not None,
        "end_utc_present": sidecar.end_utc is not None,
        "start_local_present": sidecar.start_local is not None,
        "timezone": sidecar.timezone,
        "timestamp_quality": sidecar.timestamp_quality.value,
        "gps_time_state": sidecar.gps_time_state.value,
        "anchor_source": None if sidecar.time_anchor is None else sidecar.time_anchor.source.value,
        "gps_available": sidecar.gps.available,
        "gps_sample_count": len(sidecar.gps.samples),
        "gps_samples_ordered_unique": sample_monotonic == sorted(set(sample_monotonic)),
        "gps_sample_utc_null_count": sum(
            sample.utc is None for sample in sidecar.gps.samples
        ),
        "canonical_sidecar_sha256": hashlib.sha256(
            sidecar.to_canonical_json()
        ).hexdigest(),
    }


def _durable_intent(clip_id: UUID) -> dict[str, object]:
    uri = f"file:{CATALOG_PATH.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT intent_id, kind, status, last_problem, video_source, sidecar_source, "
            "video_target, sidecar_target FROM operation_intents "
            "WHERE clip_id = ? AND kind = 'RECONCILE_NAME' ORDER BY intent_id",
            (str(clip_id),),
        ).fetchall()
    if len(rows) != 1:
        raise HarnessError("durable reconciliation intent count differs")
    row = rows[0]
    if row["status"] != "COMPLETE" or row["last_problem"] is not None:
        raise HarnessError("durable reconciliation intent is not complete")
    return {
        "intent_id": row["intent_id"],
        "kind": row["kind"],
        "status": row["status"],
        "last_problem": row["last_problem"],
        "source_video": row["video_source"],
        "source_sidecar": row["sidecar_source"],
        "target_video": row["video_target"],
        "target_sidecar": row["sidecar_target"],
    }


def _idempotent_plan(sidecar: ClipSidecar) -> dict[str, object]:
    if sidecar.time_anchor is None:
        raise HarnessError("reconciled sidecar has no durable anchor")
    conflicting = replace(
        sidecar.time_anchor,
        utc=sidecar.time_anchor.utc + timedelta(hours=6),
    )
    plan = plan_post_anchor_reconciliation(
        sidecar,
        anchor=conflicting,
        intent_id=uuid4(),
        created_monotonic_ns=time.monotonic_ns(),
    )
    if not plan.already_reconciled or plan.intent is not None or plan.sidecar != sidecar:
        raise HarnessError("reconciliation replay was not an exact stable no-op")
    return {
        "already_reconciled": True,
        "intent_created": False,
        "sidecar_unchanged": True,
        "stable_clip_id": str(plan.sidecar.clip_id),
        "conflicting_later_anchor_ignored": True,
    }


def _fixture_sidecar() -> ClipSidecar:
    pair = finalized_unsynced_clip_pair(
        boot_id=COLLISION_BOOT_ID.hex[:12],
        sequence=82,
    )
    return ClipSidecar(
        schema_version=1,
        clip_id=COLLISION_CLIP_ID,
        boot_id=COLLISION_BOOT_ID,
        sequence=82,
        video_file=pair.video_name,
        metadata_file=pair.metadata_name,
        start_utc=None,
        end_utc=None,
        start_monotonic_ns=10_000_000_000,
        end_monotonic_ns=20_000_000_000,
        gps_time_state=GpsTimeState.UNSYNCED,
        system_clock_state=SystemClockState.UNSET,
        timestamp_quality=TimestampQuality.MONOTONIC_ONLY,
        time_anchor=None,
        timezone="Asia/Jerusalem",
        start_local=None,
        video=VideoSummary("h264", 1920, 1080, 30.0, 8_000_000, 8_000_000, 300, 0),
        audio=AudioSummary(False, None, None, None, None),
        gps=GpsSummary(False, None),
        protected=False,
        protection_reason=None,
        software_version="m8-fault-matrix-fixture",
    )


def _collision_probe() -> dict[str, object]:
    """Exercise case-insensitive refusal in an isolated exact-exFAT fixture."""

    if not QUARANTINE_ROOT.is_dir() or QUARANTINE_ROOT.is_symlink():
        raise HarnessError("exact exFAT quarantine directory is unavailable")
    catalog_before = _sha256(CATALOG_PATH, 64 * 1024 * 1024)
    fixture = Path(
        tempfile.mkdtemp(prefix="m8-fault-matrix-collision-", dir=QUARANTINE_ROOT)
    )
    created_files: list[Path] = []
    created_directories: list[Path] = []
    result: dict[str, object] = {}
    try:
        if (
            fixture.resolve().parent != QUARANTINE_ROOT.resolve(strict=True)
            or fixture.is_symlink()
            or os.stat(fixture).st_dev != os.stat(RECORDING_ROOT).st_dev
        ):
            raise HarnessError("collision fixture escaped the exact exFAT quarantine")
        for name in ("pending", "clips", "protected", "quarantine"):
            directory = fixture / name
            directory.mkdir()
            created_directories.append(directory)
        sidecar = _fixture_sidecar()
        source_video = fixture / "clips" / sidecar.video_file
        source_sidecar = fixture / "clips" / sidecar.metadata_file
        source_video.write_bytes(b"bounded-disposable-video-fixture")
        source_sidecar.write_bytes(sidecar.to_canonical_json())
        created_files.extend((source_video, source_sidecar))
        anchor = TimeAnchor(
            TimeAnchorSource.GPS,
            10_000_000_000,
            BASE_UTC,
            250_000_000,
            "NMEA:GNRMC:active-valid:complete-utc",
        )
        first = plan_post_anchor_reconciliation(
            sidecar,
            anchor=anchor,
            intent_id=COLLISION_INTENT_ID,
            created_monotonic_ns=21_000_000_000,
        )
        collision = fixture / "clips" / first.sidecar.metadata_file.upper()
        collision.write_bytes(b"foreign-collision-must-survive-refusal")
        created_files.append(collision)
        hashes_before = {
            path.name: _sha256(path, MAX_SIDECAR_BYTES) for path in created_files
        }
        refusal = None
        try:
            plan_post_anchor_reconciliation(
                sidecar,
                anchor=anchor,
                intent_id=COLLISION_INTENT_ID,
                created_monotonic_ns=21_000_000_000,
                existing_names={path.name for path in (fixture / "clips").iterdir()},
            )
        except MetadataReconciliationError as error:
            refusal = _detail(error)
        if refusal is None or "collision" not in refusal.casefold():
            raise HarnessError("case-variant collision was not refused")
        hashes_after = {
            path.name: _sha256(path, MAX_SIDECAR_BYTES) for path in created_files
        }
        if hashes_before != hashes_after:
            raise HarnessError("collision refusal changed a fixture member")
        if _sha256(CATALOG_PATH, 64 * 1024 * 1024) != catalog_before:
            raise HarnessError("isolated collision probe changed the production catalog")
        result = {
            "refused": True,
            "detail": refusal,
            "case_variant": True,
            "source_members_unchanged": True,
            "collision_member_unchanged": True,
            "production_catalog_unchanged": True,
            "fixture_same_exfat_device": True,
        }
    finally:
        for path in reversed(created_files):
            if path.exists() and not path.is_symlink():
                path.unlink()
        for directory in reversed(created_directories):
            if directory.exists() and not directory.is_symlink():
                directory.rmdir()
        if fixture.exists() and not fixture.is_symlink():
            fixture.rmdir()
    result["fixture_absent_after_cleanup"] = not fixture.exists()
    return result


def _assert_privacy_safe(value: object, path: str = "result") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).casefold()
            if normalized in PRIVACY_FORBIDDEN_KEYS:
                raise HarnessError(f"privacy-forbidden evidence key at {path}.{key}")
            _assert_privacy_safe(item, f"{path}.{key}")
        return
    if isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _assert_privacy_safe(item, f"{path}[{index}]")
        return
    if isinstance(value, str) and ("$GP" in value or "$GN" in value):
        raise HarnessError(f"raw NMEA appeared in evidence at {path}")


def _validate_output(path: Path) -> Path:
    if not path.is_absolute() or path == RECORDING_ROOT or RECORDING_ROOT in path.parents:
        raise HarnessError("evidence output must be an absolute rootfs path")
    if not path.name or path.name in {".", ".."} or path.exists() or path.is_symlink():
        raise HarnessError("evidence output must be a new direct file")
    parent = path.parent.resolve(strict=True)
    if parent != path.parent or not parent.is_dir() or parent.is_symlink():
        raise HarnessError("evidence output parent must be an existing real directory")
    if os.stat(parent).st_dev == os.stat(RECORDING_ROOT).st_dev:
        raise HarnessError("evidence output must not use the recording filesystem")
    return parent / path.name


def _write_result(path: Path, document: Mapping[str, object]) -> str:
    _assert_privacy_safe(document)
    payload = (
        json.dumps(
            document,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    if len(payload) > MAX_RESULT_BYTES:
        raise HarnessError("evidence result exceeds its byte bound")
    _write_exclusive(path, payload, 0o600)
    return hashlib.sha256(payload).hexdigest()


@contextmanager
def _qualification_lock() -> Iterator[None]:
    """Hold one kernel-released lock before any live qualification mutation."""

    try:
        import fcntl
    except ImportError as error:
        raise HarnessError("exclusive qualification locking is unavailable") from error

    descriptor = os.open(
        LOCK_PATH,
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise HarnessError("another Milestone 8 qualification is already active") from error
        yield
    finally:
        os.close(descriptor)


def _run_scenario(expected_release: str) -> dict[str, object]:
    global _ACTIVE_ENDPOINT, _ACTIVE_FEEDER

    deadline = time.monotonic() + SCENARIO_TIMEOUT_S

    def remaining(maximum: float = PHASE_TIMEOUT_S) -> float:
        value = min(maximum, deadline - time.monotonic())
        if value <= 0:
            raise HarnessError("fault-matrix scenario exceeded its global deadline")
        return value

    before_names = _clip_names()
    initial = _wait_status(_recording, timeout=remaining(START_TIMEOUT_S), phase="startup_silence")
    initial_runtime = _runtime(initial)
    initial_gps = _gps(initial)
    initial_frames, drop_baseline = _frame_counts(initial)
    if (
        initial_gps.get("navigation") is not None
        or _gps_time(initial).get("state") != "UNSYNCED"
        or initial_runtime.get("pipeline_restart_count") != 0
    ):
        raise HarnessError("startup silence did not remain unsynced/navigation-hidden")
    provisional = _wait_provisional(before_names)

    endpoint = _ACTIVE_ENDPOINT
    if endpoint is None:
        raise HarnessError("active PTY endpoint was not installed")
    feeder = NmeaFeeder(endpoint)
    counters_before = _gps_counters(initial)
    bad_record = _bad_checksum(_rmc(BASE_UTC))
    feeder.send(b"$not-nmea\r\n")
    feeder.send(bad_record)
    feeder.send(_rmc(datetime(1980, 1, 1, tzinfo=UTC)))
    malformed = _wait_status(
        lambda item: (
            _recording(item)
            and _integer(_gps_counters(item).get("parse_errors"), "parse errors")
            >= _integer(counters_before.get("parse_errors"), "initial parse errors") + 2
            and _integer(_gps_counters(item).get("checksum_failures"), "checksum failures")
            >= _integer(counters_before.get("checksum_failures"), "initial checksum failures") + 1
            and _integer(_gps_counters(item).get("anchor_rejections"), "anchor rejections")
            >= _integer(counters_before.get("anchor_rejections"), "initial anchor rejections") + 1
        ),
        timeout=remaining(),
        phase="malformed_checksum_implausible",
    )

    _ACTIVE_FEEDER = feeder
    feeder.start()
    locked = _wait_status(
        lambda item: (
            _recording(item)
            and _gps(item).get("state") == "NAVIGATION_VALID"
            and _gps(item).get("navigation") is not None
            and _gps_time(item).get("state") == "GPS_TIME_VALID"
            and _integer(_gps_counters(item).get("anchor_acceptances"), "anchor acceptances") >= 1
        ),
        timeout=remaining(),
        phase="late_valid_lock",
    )
    reconciled = _wait_reconciled(provisional.clip_id)
    reconciliation_status = _wait_status(
        lambda item: (
            _recording(item)
            and _integer(
                _nested(_runtime(item), "metadata_reconciliation").get("completed"),
                "metadata reconciliations",
            )
            >= 1
        ),
        timeout=remaining(),
        phase="durable_reconciliation",
    )

    feeder.stop()
    _ACTIVE_FEEDER = None
    conflict_before = _integer(
        _gps_counters(reconciliation_status).get("anchor_rejections"),
        "pre-conflict anchor rejections",
    )
    feeder.send_current(offset_s=120.0)
    conflict = _wait_status(
        lambda item: (
            _recording(item)
            and _integer(_gps_counters(item).get("anchor_rejections"), "anchor rejections")
            >= conflict_before + 1
            and _gps_time(item).get("last_error") == "CLOCK:CONFLICT"
        ),
        timeout=remaining(),
        phase="anchor_conflict",
    )

    stale_before = _integer(
        _gps_counters(conflict).get("stale_transitions"), "stale transitions"
    )
    stale = _wait_status(
        lambda item: (
            _recording(item)
            and _gps(item).get("state") == "STALE"
            and _gps(item).get("navigation") is None
            and _gps_time(item).get("state") == "GPS_TIME_STALE"
            and _integer(_gps_counters(item).get("stale_transitions"), "stale transitions")
            >= stale_before + 1
        ),
        timeout=remaining(),
        phase="silence_stale",
    )

    old_endpoint = endpoint
    new_endpoint = PtyEndpoint(group_id=_dashcam_group_id())
    new_endpoint.install()
    _ACTIVE_ENDPOINT = new_endpoint
    disconnect_before = _integer(
        _gps_counters(stale).get("disconnects"), "disconnects"
    )
    connection_before = _integer(
        _gps_counters(stale).get("connections"), "connections"
    )
    old_endpoint.close()
    recovery_feeder = NmeaFeeder(
        new_endpoint,
        origin_monotonic=feeder.origin_monotonic,
    )
    _ACTIVE_FEEDER = recovery_feeder
    recovery_feeder.start()
    recovered = _wait_status(
        lambda item: (
            _recording(item)
            and _gps(item).get("state") == "NAVIGATION_VALID"
            and _gps(item).get("navigation") is not None
            and _gps_time(item).get("state") == "GPS_TIME_VALID"
            and _integer(_gps_counters(item).get("disconnects"), "disconnects")
            >= disconnect_before + 1
            and _integer(_gps_counters(item).get("connections"), "connections")
            >= connection_before + 1
            and _integer(_gps_counters(item).get("reconnects"), "reconnects") >= 1
        ),
        timeout=remaining(),
        phase="transport_reopen_valid_recovery",
    )
    recovered_frames, _recovered_drops = _frame_counts(recovered)
    progress = _wait_status(
        lambda item: (
            _recording(item)
            and _frame_counts(item)[0] >= recovered_frames + 90
            and _frame_counts(item)[1] is not None
            and _frame_counts(item)[1] == (0 if drop_baseline is None else drop_baseline)
            and _runtime(item).get("pipeline_restart_count") == 0
        ),
        timeout=remaining(12.0),
        phase="post_recovery_recording_progress",
    )
    recovery_feeder.stop()
    _ACTIVE_FEEDER = None

    return {
        "initial_snapshot": _summary(initial, "startup_silence"),
        "provisional_clip": {
            "clip_id": str(provisional.clip_id),
            "sequence": provisional.sequence,
            "video_file": provisional.video_file,
            "metadata_file": provisional.metadata_file,
            "utc_fields_null": provisional.start_utc is None
            and provisional.end_utc is None
            and provisional.start_local is None,
            "timestamp_quality": provisional.timestamp_quality.value,
            "anchor_absent": provisional.time_anchor is None,
        },
        "malformed_snapshot": _summary(malformed, "malformed_checksum_implausible"),
        "late_lock_snapshot": _summary(locked, "late_valid_lock"),
        "reconciliation_snapshot": _summary(
            reconciliation_status, "durable_reconciliation"
        ),
        "reconciled_clip": _sidecar_evidence(reconciled, provisional),
        "durable_intent": _durable_intent(reconciled.clip_id),
        "idempotent_replay": _idempotent_plan(reconciled),
        "conflict_snapshot": _summary(conflict, "anchor_conflict"),
        "stale_snapshot": _summary(stale, "silence_stale"),
        "recovery_snapshot": _summary(recovered, "transport_reopen_valid_recovery"),
        "progress_snapshot": _summary(progress, "post_recovery_recording_progress"),
        "recording_invariants": {
            "initial_encoded_frames": initial_frames,
            "final_encoded_frames": _frame_counts(progress)[0],
            "drop_baseline": drop_baseline,
            "drop_final": _frame_counts(progress)[1],
            "pipeline_restarts": _runtime(progress).get("pipeline_restart_count"),
            "release": expected_release,
        },
        "synthetic_sentences_sent": feeder.sent + recovery_feeder.sent,
    }


_ACTIVE_ENDPOINT: PtyEndpoint | None = None
_ACTIVE_FEEDER: NmeaFeeder | None = None


def qualify(arguments: argparse.Namespace, manifest: Mapping[str, str]) -> dict[str, object]:
    global _ACTIVE_ENDPOINT, _ACTIVE_FEEDER

    evidence: dict[str, object] = {
        "schema_version": 1,
        "phase": "milestone8_exact_pi_fault_matrix",
        "passed": False,
        "manifest": {
            "sha256": arguments.expected_manifest_sha256,
            "members": dict(manifest),
        },
        "privacy": {
            "synthetic_zero_coordinate_source_only": True,
            "coordinates_retained_in_result": False,
            "raw_nmea_retained_in_result": False,
            "product_sidecars_remain_only_on_pi_exfat": True,
        },
        "temporary_unit": UNIT_NAME,
        "ordinary_service_started": False,
        "network_or_ap_mutations": 0,
        "storage_format_or_partition_mutations": 0,
    }
    failures: list[str] = []
    endpoint: PtyEndpoint | None = None
    ordinary_before: dict[str, str] | None = None
    network_before: dict[str, str] | None = None
    try:
        if not hasattr(os, "geteuid") or os.geteuid() != 0:
            raise HarnessError("exact-Pi qualification requires root")
        release = _release_identity(arguments.expected_release)
        evidence["release"] = release
        evidence["board"] = _board_identity(arguments.expected_board_serial)
        evidence["storage"] = _storage_identity(arguments.expected_storage_uuid)
        evidence["clock_services"] = _clock_owner()
        ordinary_before = _inactive_unit("dashcamd.service")
        network_before = _inactive_unit("dashcam-network-fallback.service")
        evidence["ordinary_unit_before"] = ordinary_before
        evidence["network_unit_before"] = network_before
        evidence["throttle_before"] = _throttle()
        if evidence["throttle_before"] != "throttled=0x0":
            raise HarnessError("Pi was throttled before qualification")
        evidence["collision_probe"] = _collision_probe()
        group_id = _dashcam_group_id()
        base_config = load_config(CONFIG_PATH)
        temporary_config = _temporary_config(base_config)
        if (
            temporary_config.time.discipline_system_clock
            or temporary_config.time.system_clock_owner != "systemd-timesyncd"
        ):
            raise HarnessError("temporary config would violate the wall-clock ownership contract")
        interpreter = Path(cast(Mapping[str, str], evidence["release"])["interpreter"])
        _prepare_transient_files(temporary_config, interpreter, group_id)
        endpoint = PtyEndpoint(group_id=group_id)
        endpoint.install()
        _ACTIVE_ENDPOINT = endpoint
        try:
            _systemctl("start", UNIT_NAME, timeout=START_TIMEOUT_S)
        except BaseException:
            if STATUS_PATH.is_file() and not STATUS_PATH.is_symlink():
                evidence["transient_start_failure_status"] = _startup_failure_summary(
                    _status()
                )
            else:
                evidence["transient_start_failure_status"] = {"status_absent": True}
            raise
        evidence["scenario"] = _run_scenario(arguments.expected_release)
    except BaseException as error:
        failures.append(_detail(f"{type(error).__name__}: {error}"))
    finally:
        feeder = _ACTIVE_FEEDER
        if feeder is not None:
            try:
                feeder.stop()
            except BaseException as error:
                failures.append(_detail(f"feeder cleanup failed: {error}"))
        active_endpoint = _ACTIVE_ENDPOINT or endpoint
        failures.extend(_remove_transient_files(active_endpoint))
        _ACTIVE_FEEDER = None
        _ACTIVE_ENDPOINT = None

    try:
        transient_after = _unit_properties(UNIT_NAME)
        evidence["temporary_unit_after"] = transient_after
        evidence["temporary_artifacts_absent"] = {
            "unit": not os.path.lexists(UNIT_PATH),
            "config_root": not os.path.lexists(TEMP_ROOT),
            "gps_link": not os.path.lexists(GPS_LINK),
            "status_runtime_directory": not STATUS_PATH.parent.exists(),
        }
        ordinary_after = _inactive_unit("dashcamd.service")
        network_after = _inactive_unit("dashcam-network-fallback.service")
        evidence["ordinary_unit_after"] = ordinary_after
        evidence["network_unit_after"] = network_after
        evidence["ordinary_service_started"] = False
        if (
            ordinary_before is not None
            and ordinary_after["NRestarts"] != ordinary_before["NRestarts"]
        ):
            failures.append("ordinary dashcamd NRestarts changed")
        if network_before is not None and network_after != network_before:
            failures.append("network fallback service state changed")
        evidence["throttle_after"] = _throttle()
        if evidence["throttle_after"] != "throttled=0x0":
            failures.append("Pi was throttled during qualification")
        cleanup = cast(Mapping[str, object], evidence["temporary_artifacts_absent"])
        if not all(value is True for value in cleanup.values()):
            failures.append("one or more temporary artifacts remain")
    except BaseException as error:
        failures.append(_detail(f"post-cleanup verification failed: {error}"))
    evidence["failures"] = failures
    evidence["passed"] = not failures
    _assert_privacy_safe(evidence)
    return evidence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="milestone8-fault-matrix")
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--expected-release", required=True)
    parser.add_argument("--expected-board-serial", required=True)
    parser.add_argument("--expected-storage-uuid", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        output = _validate_output(arguments.output)
        manifest = verify_manifest(arguments.expected_manifest_sha256)
    except (HarnessError, OSError, UnicodeError, ValueError) as error:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "phase": "milestone8_exact_pi_fault_matrix",
                    "passed": False,
                    "error": _detail(f"{type(error).__name__}: {error}"),
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            flush=True,
        )
        return 2
    try:
        with _qualification_lock():
            report = qualify(arguments, manifest)
            output_sha256 = _write_result(output, report)
    except (HarnessError, OSError, UnicodeError, ValueError) as error:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "phase": "milestone8_exact_pi_fault_matrix",
                    "passed": False,
                    "error": _detail(f"{type(error).__name__}: {error}"),
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            flush=True,
        )
        return 2
    print(
        json.dumps(
            {
                "schema_version": 1,
                "phase": "milestone8_exact_pi_fault_matrix",
                "passed": report["passed"],
                "output": output.as_posix(),
                "output_sha256": output_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        flush=True,
    )
    return 0 if report["passed"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
