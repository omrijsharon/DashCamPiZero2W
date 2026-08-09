#!/usr/bin/env python3
"""Bounded exact-Pi Milestone 9 functional burned-overlay qualification."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence, Set
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Final, SupportsInt, cast
from uuid import UUID

import dashcam
from dashcam.config import DashcamConfig, OverlayConfig, load_config, write_config_atomic
from dashcam.diagnostics.media import CommandResult, run_fixed_argv
from dashcam.gps.clock import to_local_time
from dashcam.metadata.reconcile import parse_sidecar_bytes
from dashcam.metadata.schema import ClipSidecar
from dashcam.overlay import OverlayOptions, OverlayTelemetry, build_overlay, render_luma_bitmap
from dashcam.state import GpsState, GpsTimeState, TimestampQuality

ACCEPTED_RELEASE: Final = "0.1.0.dev0-5f95dd806342ac9e"
EXPECTED_INSTALLED_MANIFEST_SHA256: Final = (
    "619fe30e8123e0ceaec55269de0a6faf6ec88ccb4859a98bbef2d87776dbb655"
)
EXPECTED_PRODUCTION_CONFIG_SHA256: Final = (
    "1276363286475bccf85e70332ec893846e3fe3572e8184991843400ac4d6c4b8"
)
CONFIG_PATH: Final = Path("/etc/dashcam/config.toml")
IDENTITY_PATH: Final = Path("/etc/dashcam/storage-volume.env")
RECORDING_ROOT: Final = Path("/srv/dashcam")
CLIPS_ROOT: Final = RECORDING_ROOT / "clips"
CATALOG_PATH: Final = Path("/var/lib/dashcam/catalog.sqlite3")
STATUS_PATH: Final = Path("/run/dashcam/status.json")
TEMP_ROOT: Final = Path("/run/dashcam-m9-overlay")
TEMP_CONFIG_PATH: Final = TEMP_ROOT / "config.toml"
GPS_LINK: Final = Path("/dev/dashcam-m9-overlay")
UNIT_PATH: Final = Path("/run/systemd/system/dashcam-m9-overlay.service")
UNIT_NAME: Final = UNIT_PATH.name
LOCK_PATH: Final = Path("/run/lock/dashcam-live-qualification.lock")
SYSTEMCTL: Final = "/usr/bin/systemctl"
FINDMNT: Final = "/usr/bin/findmnt"
VCGENCMD: Final = "/usr/bin/vcgencmd"
FFMPEG: Final = "/usr/bin/ffmpeg"
FFPROBE: Final = "/usr/bin/ffprobe"
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

MAX_MANIFEST_BYTES: Final = 4096
MAX_SCRIPT_BYTES: Final = 2 * 1024 * 1024
MAX_RESULT_BYTES: Final = 1024 * 1024
MAX_STATUS_BYTES: Final = 64 * 1024
MAX_SIDECAR_BYTES: Final = 512 * 1024
MAX_COMMAND_OUTPUT_BYTES: Final = 16 * 1024
MAX_DECODE_OUTPUT_BYTES: Final = 128 * 1024
MAX_CLIP_ENTRIES: Final = 4096
MAX_RECONCILIATION_BACKLOG: Final = 64
MAX_SCENARIO_CANONICAL_SIDECARS: Final = 2
MAX_NEW_SIDECARS: Final = MAX_RECONCILIATION_BACKLOG + MAX_SCENARIO_CANONICAL_SIDECARS
MAX_SENTENCES: Final = 2048
CLIP_DURATION_S: Final = 60
WATCHDOG_S: Final = 2
STALE_AFTER_S: Final = 2.0
OVERLAY_INTERVAL_S: Final = 0.5
PHASE_DWELL_S: Final = 1.25
POLL_INTERVAL_S: Final = 0.25
START_TIMEOUT_S: Final = 45.0
PHASE_TIMEOUT_S: Final = 20.0
BOUNDARY_TIMEOUT_S: Final = 80.0
SCENARIO_TIMEOUT_S: Final = 175.0
STOP_TIMEOUT_S: Final = 35.0
FEED_INTERVAL_S: Final = 0.2
BASE_UTC: Final = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
OVERLAY_X: Final = 40
OVERLAY_Y: Final = 40
OVERLAY_WIDTH: Final = 1152
OVERLAY_HEIGHT: Final = 64
OVERLAY_BYTES: Final = OVERLAY_WIDTH * OVERLAY_HEIGHT
BINARY_THRESHOLD: Final = 128
MIN_TEMPLATE_F1: Final = 0.88
MIN_WRONG_TEMPLATE_MARGIN: Final = 0.08
TIMESTAMP_CANDIDATE_RADIUS_S: Final = 2
MIN_COMPLETE_FPS: Final = 29.9
MIN_COMPLETE_DURATION_S: Final = 59.0
MAX_COMPLETE_DURATION_S: Final = 61.0
FRAME_PERIOD_S: Final = 1.0 / 30.0
PTS_MAPPING_TOLERANCE_S: Final = FRAME_PERIOD_S + 0.005
FIRST_PTS_TOLERANCE_S: Final = 0.040
MAX_PROJECTION_ERROR_NS: Final = 1_000_000
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
        "overlay_text",
        "rendered_text",
        "frame",
        "crop",
        "media_bytes",
    }
)


class HarnessError(RuntimeError):
    """The requested qualification is unsafe, incomplete, or ambiguous."""


def _member(value: object, name: str) -> object:
    return getattr(value, name)


@dataclass(frozen=True, slots=True)
class PhaseObservation:
    name: str
    monotonic_ns: int
    status: Mapping[str, object]
    dwell_start_ns: int | None = None
    dwell_end_ns: int | None = None
    start_status: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class DecodedOverlayFrame:
    luma: bytes
    first_pts_s: float
    selected_pts_s: float


def _detail(value: object, maximum: int = 512) -> str:
    text = " ".join(str(value).replace("\0", " ").splitlines())
    return "".join(c if c.isprintable() else " " for c in text)[:maximum]


def _regular_bytes(path: Path, maximum: int) -> bytes:
    fd = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise HarnessError(f"{path} is not a regular file")
        payload = bytearray()
        while chunk := os.read(fd, min(65536, maximum + 1 - len(payload))):
            payload.extend(chunk)
            if len(payload) > maximum:
                raise HarnessError(f"{path} exceeded its read bound")
        return bytes(payload)
    finally:
        os.close(fd)


def _sha256(path: Path, maximum: int) -> str:
    fd = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    digest = hashlib.sha256()
    size = 0
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise HarnessError(f"{path} is not a regular file")
        while chunk := os.read(fd, 1024 * 1024):
            size += len(chunk)
            if size > maximum:
                raise HarnessError(f"{path} exceeded its hash bound")
            digest.update(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest()


def verify_manifest(expected_sha256: str, directory: Path | None = None) -> dict[str, str]:
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
    maximum_output: int = MAX_COMMAND_OUTPUT_BYTES,
) -> CommandResult:
    result = run_fixed_argv(
        argv,
        timeout_seconds=timeout,
        max_output_bytes=maximum_output,
    )
    if result.returncode not in allowed_returncodes or result.timed_out or result.output_truncated:
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
    result = _systemctl("show", "--no-pager", *(f"--property={name}" for name in names), unit)
    values: dict[str, str] = {}
    for line in result.stdout.decode("ascii", "strict").splitlines():
        key, separator, value = line.partition("=")
        if separator and key in names and key not in values:
            values[key] = value
    if (
        set(values) != set(names)
        or not values["MainPID"].isdigit()
        or not values["NRestarts"].isdigit()
    ):
        raise HarnessError(f"systemd property shape differs for {unit}")
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


def _stop_transient_for_offline_analysis(*, timeout: float = STOP_TIMEOUT_S) -> dict[str, object]:
    """Stop the recorder cleanly before CPU-intensive media inspection."""
    _systemctl("stop", UNIT_NAME, timeout=timeout)
    values = _unit_properties(UNIT_NAME)
    if (
        values["LoadState"] != "loaded"
        or values["ActiveState"] != "inactive"
        or values["SubState"] != "dead"
        or values["MainPID"] != "0"
        or values["NRestarts"] != "0"
        or values["Result"] != "success"
        or values["ExecMainStatus"] != "0"
    ):
        raise HarnessError("transient recorder did not stop cleanly before offline analysis")
    return {
        "inactive": True,
        "main_pid_zero": True,
        "restart_count": 0,
        "result_success": True,
        "exit_status_zero": True,
    }


def _throttle() -> str:
    value = _command((VCGENCMD, "get_throttled")).stdout.decode("ascii", "strict").strip()
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
        value = json.loads(payload, object_pairs_hook=unique)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HarnessError(f"{label} is invalid JSON") from error
    if not isinstance(value, Mapping):
        raise HarnessError(f"{label} is not a JSON object")
    return cast(Mapping[str, object], value)


def _status() -> Mapping[str, object]:
    value = _strict_json(_regular_bytes(STATUS_PATH, MAX_STATUS_BYTES), "runtime status")
    if set(value) != {"schema_version", "lifecycle", "runtime"} or value["schema_version"] != 2:
        raise HarnessError("runtime status top-level schema differs")
    return value


def _nested(document: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = document.get(key)
    if not isinstance(value, Mapping):
        raise HarnessError(f"status has no {key} object")
    return cast(Mapping[str, object], value)


def _runtime(document: Mapping[str, object]) -> Mapping[str, object]:
    return _nested(document, "runtime")


def _gps(document: Mapping[str, object]) -> Mapping[str, object]:
    return _nested(_runtime(document), "gps")


def _gps_time(document: Mapping[str, object]) -> Mapping[str, object]:
    return _nested(_gps(document), "time")


def _frames(document: Mapping[str, object]) -> tuple[int, int | None]:
    values = _nested(_runtime(document), "frames")
    encoded, dropped = values.get("encoded"), values.get("dropped")
    if isinstance(encoded, bool) or not isinstance(encoded, int) or encoded < 0:
        raise HarnessError("encoded frame counter differs")
    if dropped is not None and (
        isinstance(dropped, bool) or not isinstance(dropped, int) or dropped < 0
    ):
        raise HarnessError("dropped frame counter differs")
    return encoded, dropped


def _overlay(document: Mapping[str, object]) -> Mapping[str, object]:
    return _nested(_runtime(document), "overlay")


def _renderer(document: Mapping[str, object]) -> Mapping[str, object]:
    return _nested(_overlay(document), "renderer")


def _recording(document: Mapping[str, object]) -> bool:
    lifecycle = document.get("lifecycle")
    if not isinstance(lifecycle, Mapping):
        return False
    try:
        encoded, _ = _frames(document)
    except HarnessError:
        return False
    return (
        lifecycle.get("state") == "RECORDING"
        and encoded >= 1
        and _runtime(document).get("pipeline_restart_count") == 0
    )


def _wait_status(
    predicate: Callable[[Mapping[str, object]], bool], *, timeout: float, phase: str
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
    suffix = "" if last_error is None else f": {_detail(last_error)}"
    raise HarnessError(f"status wait timed out during {phase}{suffix}")


def _renderer_healthy(document: Mapping[str, object]) -> bool:
    overlay = _overlay(document)
    renderer = _renderer(document)
    zero_fields = (
        "update_rejections",
        "contract_mismatches",
        "transform_failures",
        "mapping_limit_rejections",
        "sync_failures",
    )
    return (
        overlay.get("enabled") is True
        and overlay.get("state") == "ACTIVE"
        and overlay.get("last_error") is None
        and renderer.get("state") == "ACTIVE"
        and renderer.get("enabled") is True
        and renderer.get("last_error") is None
        and all(renderer.get(name) == 0 for name in zero_fields)
        and isinstance(renderer.get("frames_rendered"), int)
        and cast(int, renderer["frames_rendered"]) >= 1
    )


def _phase(
    name: str,
    start_status: Mapping[str, object],
    end_status: Mapping[str, object] | None = None,
    start_ns: int | None = None,
    end_ns: int | None = None,
) -> PhaseObservation:
    if end_status is None or start_ns is None or end_ns is None:
        return PhaseObservation(name=name, monotonic_ns=time.monotonic_ns(), status=start_status)
    if end_ns - start_ns < round(2 * OVERLAY_INTERVAL_S * 1_000_000_000):
        raise HarnessError(f"{name} dwell was shorter than two overlay update intervals")
    start_renderer = _renderer(start_status).get("frames_rendered")
    end_renderer = _renderer(end_status).get("frames_rendered")
    if (
        not isinstance(start_renderer, int)
        or not isinstance(end_renderer, int)
        or end_renderer <= start_renderer
    ):
        raise HarnessError(f"{name} renderer made no frame progress during dwell")
    start_updates = _overlay(start_status).get("updates")
    end_updates = _overlay(end_status).get("updates")
    if name != "startup_unsynced" and (
        not isinstance(start_updates, int)
        or not isinstance(end_updates, int)
        or end_updates <= start_updates
    ):
        raise HarnessError(f"{name} overlay text made no update progress during dwell")
    return PhaseObservation(
        name=name,
        monotonic_ns=(start_ns + end_ns) // 2,
        status=end_status,
        dwell_start_ns=start_ns,
        dwell_end_ns=end_ns,
        start_status=start_status,
    )


def _phase_summary(observation: PhaseObservation) -> dict[str, object]:
    document = observation.status
    gps = _gps(document)
    renderer = _renderer(document)
    start = observation.start_status
    start_overlay_updates = None if start is None else _overlay(start).get("updates")
    start_renderer_frames = None if start is None else _renderer(start).get("frames_rendered")
    return {
        "phase": observation.name,
        "observed_monotonic_ns": observation.monotonic_ns,
        "gps_state": gps.get("state"),
        "navigation_present": gps.get("navigation") is not None,
        "gps_time_state": _gps_time(document).get("state"),
        "encoded_frames": _frames(document)[0],
        "dropped_frames": _frames(document)[1],
        "pipeline_restarts": _runtime(document).get("pipeline_restart_count"),
        "overlay_updates": _overlay(document).get("updates"),
        "renderer_state": renderer.get("state"),
        "renderer_frames": renderer.get("frames_rendered"),
        "dwell_start_monotonic_ns": observation.dwell_start_ns,
        "dwell_end_monotonic_ns": observation.dwell_end_ns,
        "dwell_duration_ns": (
            None
            if observation.dwell_start_ns is None or observation.dwell_end_ns is None
            else observation.dwell_end_ns - observation.dwell_start_ns
        ),
        "overlay_updates_at_dwell_start": start_overlay_updates,
        "renderer_frames_at_dwell_start": start_renderer_frames,
        "renderer_frame_progress": (
            isinstance(start_renderer_frames, int)
            and isinstance(renderer.get("frames_rendered"), int)
            and cast(int, renderer["frames_rendered"]) > start_renderer_frames
        ),
        "renderer_failures": {
            name: renderer.get(name)
            for name in (
                "update_rejections",
                "contract_mismatches",
                "transform_failures",
                "sync_failures",
            )
        },
    }


def _startup_failure_summary(document: Mapping[str, object]) -> dict[str, object]:
    """Retain only bounded lifecycle diagnostics from a failed daemon start."""

    lifecycle = document.get("lifecycle")
    if not isinstance(lifecycle, Mapping):
        raise HarnessError("startup failure status has no lifecycle object")

    def bounded_field(name: str) -> str | None:
        value = lifecycle.get(name)
        return None if value is None else _detail(value)

    return {
        "phase": "transient_start_failure",
        "observed_monotonic_ns": time.monotonic_ns(),
        "lifecycle_state": bounded_field("state"),
        "reason": bounded_field("reason"),
        "detail": bounded_field("detail"),
        "runtime_snapshot_present": isinstance(document.get("runtime"), Mapping),
    }


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


def _rmc(utc: datetime) -> bytes:
    value = utc.astimezone(UTC)
    return _nmea(f"GNRMC,{value:%H%M%S}.00,A,0000.0000,N,00000.0000,E,0.0,0.0,{value:%d%m%y},,,A")


class PtyEndpoint:
    def __init__(self, *, group_id: int) -> None:
        if not hasattr(os, "openpty"):
            raise HarnessError("PTY support is unavailable")
        master, slave = os.openpty()
        self.master = master
        ttyname = cast(Callable[[int], str], _member(os, "ttyname"))
        fchown = cast(Callable[[int, int, int], None], _member(os, "fchown"))
        fchmod = cast(Callable[[int, int], None], _member(os, "fchmod"))
        self.slave_name = ttyname(slave)
        try:
            if SAFE_DEVICE_RE.fullmatch(self.slave_name) is None:
                raise HarnessError("PTY slave path shape differs")
            fchown(slave, 0, group_id)
            fchmod(slave, 0o660)
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

    def send_current(self) -> None:
        if self.sent >= MAX_SENTENCES:
            raise HarnessError("synthetic sentence count exceeded its hard bound")
        utc = BASE_UTC + timedelta(seconds=time.monotonic() - self._origin)
        self._endpoint.write(_rmc(utc))
        self.sent += 1

    def start(self) -> None:
        if self._thread is not None:
            raise HarnessError("NMEA feeder is already started")
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="m9-nmea-feeder", daemon=True)
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

    getgrnam = cast(Callable[[str], object], _member(grp, "getgrnam"))
    return int(cast(SupportsInt, _member(getgrnam("dashcam"), "gr_gid")))


def _temporary_config(base: DashcamConfig) -> DashcamConfig:
    if not base.overlay.enabled:
        raise HarnessError("production overlay must already be enabled")
    return replace(
        base,
        video=replace(base.video, clip_duration_s=CLIP_DURATION_S),
        gps=replace(base.gps, device=str(GPS_LINK), stale_after_s=STALE_AFTER_S),
        service=replace(base.service, watchdog_s=WATCHDOG_S),
    )


def render_transient_unit(*, interpreter: Path, config_path: Path) -> str:
    interpreter_text = interpreter.as_posix()
    if SAFE_RELEASE_PATH_RE.fullmatch(interpreter_text) is None:
        raise HarnessError("transient unit interpreter path is not one installed release")
    if config_path != TEMP_CONFIG_PATH:
        raise HarnessError("transient unit config path differs")
    return f"""[Unit]
Description=Dashcam Milestone 9 transient functional overlay recorder
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
StateDirectory=dashcam
StateDirectoryMode=0750
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
ReadOnlyPaths={TEMP_CONFIG_PATH}
BindPaths=/srv/dashcam
"""


def _write_exclusive(path: Path, payload: bytes, mode: int) -> None:
    fd = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        position = 0
        while position < len(payload):
            written = os.write(fd, payload[position:])
            if written <= 0:
                raise HarnessError("exclusive write made no progress")
            position += written
        os.fsync(fd)
    finally:
        os.close(fd)


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
    chown = cast(Callable[[Path, int, int], None], _member(os, "chown"))
    chown(TEMP_ROOT, 0, group_id)
    write_config_atomic(TEMP_CONFIG_PATH, config)
    chown(TEMP_CONFIG_PATH, 0, group_id)
    os.chmod(TEMP_CONFIG_PATH, 0o640)
    _write_exclusive(
        UNIT_PATH,
        render_transient_unit(interpreter=interpreter, config_path=TEMP_CONFIG_PATH).encode(
            "ascii"
        ),
        0o644,
    )
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
    if expected_release != ACCEPTED_RELEASE or RELEASE_RE.fullmatch(expected_release) is None:
        raise HarnessError("only the accepted Milestone 9 release may be qualified")
    prefix = Path(sys.prefix).resolve(strict=True)
    package = Path(dashcam.__file__).resolve(strict=True)
    release_root = Path("/opt/dashcam/releases") / expected_release
    expected_prefix = release_root / "venv"
    if prefix != expected_prefix or not package.is_relative_to(prefix):
        raise HarnessError("interpreter/package are not the expected installed release")
    marker = _strict_json(
        _regular_bytes(release_root / "installed.json", 8192), "installed release marker"
    )
    if (
        set(marker) != {"schema_version", "release_id", "manifest_sha256"}
        or marker.get("schema_version") != 1
        or marker.get("release_id") != expected_release
        or not isinstance(marker.get("manifest_sha256"), str)
        or marker.get("manifest_sha256") != EXPECTED_INSTALLED_MANIFEST_SHA256
    ):
        raise HarnessError("installed release marker differs")
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
        raise HarnessError("application installation journal differs")
    interpreter = expected_prefix / "bin/python"
    if (
        Path("/opt/dashcam/current").resolve(strict=True) != release_root
        or not interpreter.exists()
    ):
        raise HarnessError("current release link/interpreter differs")
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
    fields = (
        _command(
            (
                FINDMNT,
                "-n",
                "-o",
                "TARGET,FSTYPE,LABEL,UUID,SOURCE",
                "--target",
                str(RECORDING_ROOT),
            )
        )
        .stdout.decode("ascii", "strict")
        .strip()
        .split()
    )
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
    if os.stat("/").st_dev == os.stat(RECORDING_ROOT).st_dev:
        raise HarnessError("recording root is not a distinct filesystem")
    return {
        "target": target,
        "filesystem": filesystem,
        "label": label,
        "uuid": uuid,
        "source": source,
    }


def _clock_owner() -> dict[str, str]:
    timesyncd = _unit_properties("systemd-timesyncd.service")
    if timesyncd["ActiveState"] != "active" or timesyncd["SubState"] != "running":
        raise HarnessError("systemd-timesyncd is not the active wall-clock owner")
    inactive: dict[str, str] = {}
    for unit in ("chrony.service", "ntp.service", "ntpsec.service", "gpsd.service"):
        result = _command((SYSTEMCTL, "is-active", unit), allowed_returncodes=frozenset({0, 3, 4}))
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
        raise HarnessError("clips directory is not a real recording-volume directory")
    names: set[str] = set()
    with os.scandir(CLIPS_ROOT) as entries:
        for entry in entries:
            if (
                len(names) >= MAX_CLIP_ENTRIES
                or not entry.name.isascii()
                or not entry.name.isprintable()
                or len(entry.name) > 255
            ):
                raise HarnessError("clips directory exceeds its safety bound")
            names.add(entry.name)
    return names


def _catalog_sidecar(clip_id: UUID) -> ClipSidecar | None:
    uri = f"file:{CATALOG_PATH.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=1.0) as connection:
        rows = connection.execute(
            "SELECT sidecar_path FROM clips WHERE clip_id = ?", (str(clip_id),)
        ).fetchall()
    if not rows:
        return None
    if len(rows) != 1 or len(rows[0]) != 1 or not isinstance(rows[0][0], str):
        raise HarnessError("catalog UUID lookup shape differs")
    relative = PurePosixPath(rows[0][0])
    if (
        len(relative.parts) != 2
        or relative.parts[0] != "clips"
        or not relative.name.endswith(".json")
    ):
        raise HarnessError("catalog sidecar path is outside the clips contract")
    try:
        sidecar = parse_sidecar_bytes(_regular_bytes(CLIPS_ROOT / relative.name, MAX_SIDECAR_BYTES))
    except FileNotFoundError:
        return None
    if sidecar.clip_id != clip_id:
        raise HarnessError("catalog/sidecar UUID differs")
    return sidecar


def _new_canonical_sidecars(before: set[str]) -> list[ClipSidecar]:
    names = sorted(
        name
        for name in (_clip_names() - before)
        if name.endswith(".json") and not name.startswith("boot-")
    )
    if len(names) > MAX_NEW_SIDECARS:
        raise HarnessError("new sidecar set exceeded its hard bound")
    sidecars = [
        parse_sidecar_bytes(_regular_bytes(CLIPS_ROOT / name, MAX_SIDECAR_BYTES)) for name in names
    ]
    canonical: list[ClipSidecar] = []
    for item in sidecars:
        catalogued = _catalog_sidecar(item.clip_id)
        if catalogued is not None and catalogued == item:
            canonical.append(item)
    return canonical


def _wait_covering_sidecar(
    before: set[str], observations: Sequence[PhaseObservation], *, timeout: float
) -> ClipSidecar:
    deadline = time.monotonic() + min(timeout, BOUNDARY_TIMEOUT_S)
    while time.monotonic() < deadline:
        for sidecar in _new_canonical_sidecars(before):
            if all(
                sidecar.start_monotonic_ns <= item.monotonic_ns < sidecar.end_monotonic_ns
                for item in observations
            ):
                return sidecar
        time.sleep(POLL_INTERVAL_S)
    raise HarnessError("no canonical finalized clip covers every functional phase")


def _overlay_options(config: OverlayConfig) -> OverlayOptions:
    return OverlayOptions(
        show_local_datetime=config.show_local_datetime,
        show_utc_offset=config.show_utc_offset,
        show_rec=config.show_rec,
        show_speed=config.show_speed,
        speed_unit=config.speed_unit,
        show_coordinates=config.show_coordinates,
        coordinate_decimals=config.coordinate_decimals,
        show_altitude=config.show_altitude,
        show_satellites=config.show_satellites,
        show_hdop=config.show_hdop,
    )


def _project_utc(sidecar: ClipSidecar, monotonic_ns: int) -> datetime:
    anchor = sidecar.time_anchor
    if anchor is None:
        raise HarnessError("canonical sidecar lacks its stable anchor")
    return anchor.utc + timedelta(microseconds=(monotonic_ns - anchor.monotonic_ns) / 1000)


def _expected_bitmap(
    sidecar: ClipSidecar,
    config: DashcamConfig,
    monotonic_ns: int,
    state: str,
) -> bytes:
    if state == "unsynced":
        telemetry = OverlayTelemetry(
            gps_time_state=GpsTimeState.UNSYNCED,
            timestamp_quality=TimestampQuality.MONOTONIC_ONLY,
            gps_state=GpsState.UART_UNAVAILABLE,
        )
    else:
        local = to_local_time(_project_utc(sidecar, monotonic_ns), config.time.timezone)
        if not local.ok or local.local is None:
            raise HarnessError("stable-anchor local-time projection failed")
        telemetry = OverlayTelemetry(
            gps_time_state=(
                GpsTimeState.GPS_TIME_STALE if state == "stale" else GpsTimeState.GPS_TIME_VALID
            ),
            timestamp_quality=TimestampQuality.GPS_ANCHORED,
            gps_state=(GpsState.STALE if state == "stale" else GpsState.NAVIGATION_VALID),
            local_time=local.local,
            latitude_deg=(None if state == "stale" else 0.0),
            longitude_deg=(None if state == "stale" else 0.0),
            speed_mps=(None if state == "stale" else 0.0),
        )
    frame = build_overlay(telemetry, _overlay_options(config.overlay))
    return render_luma_bitmap(f"{frame.top_line}\n{frame.bottom_line}")


def _parse_single_pts(payload: bytes, label: str) -> float:
    lines = [
        line.strip() for line in payload.decode("ascii", "strict").splitlines() if line.strip()
    ]
    if len(lines) != 1:
        raise HarnessError(f"{label} PTS output shape differs")
    try:
        value = float(lines[0])
    except ValueError as error:
        raise HarnessError(f"{label} PTS is not numeric") from error
    if not 0 <= value <= CLIP_DURATION_S + 1:
        raise HarnessError(f"{label} PTS is outside its bound")
    return value


def _parse_showinfo_pts(payload: bytes) -> float:
    matches = re.findall(rb"\bpts_time:([0-9]+(?:\.[0-9]+)?)\b", payload)
    if len(matches) != 1:
        raise HarnessError("selected-frame showinfo PTS shape differs")
    return _parse_single_pts(matches[0] + b"\n", "selected frame")


def _validate_pts_mapping(
    sidecar: ClipSidecar,
    target_monotonic_ns: int,
    *,
    first_pts_s: float,
    selected_pts_s: float,
) -> tuple[int, int]:
    if abs(first_pts_s) > FIRST_PTS_TOLERANCE_S:
        raise HarnessError("clip first video PTS exceeds its explicit tolerance")
    target_offset_s = (target_monotonic_ns - sidecar.start_monotonic_ns) / 1_000_000_000
    expected_pts_s = first_pts_s + target_offset_s
    error_ns = abs(round((selected_pts_s - expected_pts_s) * 1_000_000_000))
    if error_ns > round(PTS_MAPPING_TOLERANCE_S * 1_000_000_000):
        raise HarnessError("selected-frame PTS differs from sidecar monotonic mapping")
    selected_monotonic_ns = sidecar.start_monotonic_ns + round(
        (selected_pts_s - first_pts_s) * 1_000_000_000
    )
    if not sidecar.start_monotonic_ns <= selected_monotonic_ns < sidecar.end_monotonic_ns:
        raise HarnessError("selected-frame PTS maps outside its canonical sidecar")
    return selected_monotonic_ns, error_ns


def _decode_overlay_frame(
    video: Path,
    offset_ns: int,
    *,
    timeout: float = 20.0,
) -> DecodedOverlayFrame:
    if offset_ns < 0:
        raise HarnessError("decode offset precedes the clip")
    resolved = video.resolve(strict=True)
    clips = CLIPS_ROOT.resolve(strict=True)
    if resolved.parent != clips or resolved.suffix.casefold() != ".mp4" or resolved.is_symlink():
        raise HarnessError("decode target is not one finalized clip member")
    first = _command(
        (
            FFPROBE,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-read_intervals",
            "%+#1",
            "-show_entries",
            "frame=best_effort_timestamp_time",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            resolved.as_posix(),
        ),
        timeout=min(timeout, 10.0),
    )
    first_pts_s = _parse_single_pts(first.stdout, "first frame")
    result = _command(
        (
            FFMPEG,
            "-v",
            "info",
            "-copyts",
            "-ss",
            f"{offset_ns / 1_000_000_000:.6f}",
            "-i",
            resolved.as_posix(),
            "-frames:v",
            "1",
            "-vf",
            f"crop={OVERLAY_WIDTH}:{OVERLAY_HEIGHT}:{OVERLAY_X}:{OVERLAY_Y},showinfo",
            "-pix_fmt",
            "gray",
            "-f",
            "rawvideo",
            "pipe:1",
        ),
        timeout=min(timeout, 15.0),
        maximum_output=MAX_DECODE_OUTPUT_BYTES,
    )
    if len(result.stdout) != OVERLAY_BYTES:
        raise HarnessError("decoded overlay crop byte count differs")
    return DecodedOverlayFrame(
        luma=result.stdout,
        first_pts_s=first_pts_s,
        selected_pts_s=_parse_showinfo_pts(result.stderr),
    )


def _bitmap_f1(observed: bytes, expected: bytes) -> float:
    if len(observed) != OVERLAY_BYTES or len(expected) != OVERLAY_BYTES:
        raise HarnessError("bitmap comparison dimensions differ")
    true_positive = false_positive = false_negative = 0
    for actual, wanted in zip(observed, expected, strict=True):
        actual_on = actual >= BINARY_THRESHOLD
        wanted_on = wanted >= BINARY_THRESHOLD
        true_positive += int(actual_on and wanted_on)
        false_positive += int(actual_on and not wanted_on)
        false_negative += int(not actual_on and wanted_on)
    denominator = 2 * true_positive + false_positive + false_negative
    if denominator == 0:
        raise HarnessError("bitmap comparison contains no foreground")
    return 2 * true_positive / denominator


def _classify_burn_in(
    sidecar: ClipSidecar,
    config: DashcamConfig,
    observation: PhaseObservation,
    expected_state: str,
    wrong_state: str,
    *,
    timeout: float = 20.0,
) -> dict[str, object]:
    offset_ns = observation.monotonic_ns - sidecar.start_monotonic_ns
    if not 0 <= offset_ns < sidecar.end_monotonic_ns - sidecar.start_monotonic_ns:
        raise HarnessError("phase PTS is outside its bound sidecar window")
    decoded = _decode_overlay_frame(
        CLIPS_ROOT / sidecar.video_file,
        offset_ns,
        timeout=timeout,
    )
    selected_monotonic_ns, pts_error_ns = _validate_pts_mapping(
        sidecar,
        observation.monotonic_ns,
        first_pts_s=decoded.first_pts_s,
        selected_pts_s=decoded.selected_pts_s,
    )
    if (
        observation.dwell_start_ns is not None
        and observation.dwell_end_ns is not None
        and not observation.dwell_start_ns <= selected_monotonic_ns < observation.dwell_end_ns
    ):
        raise HarnessError("selected frame falls outside its qualified phase dwell")
    shifts = (
        (0,)
        if expected_state == "unsynced"
        else tuple(range(-TIMESTAMP_CANDIDATE_RADIUS_S, TIMESTAMP_CANDIDATE_RADIUS_S + 1))
    )
    correct_candidates = [
        _expected_bitmap(
            sidecar, config, selected_monotonic_ns + shift * 1_000_000_000, expected_state
        )
        for shift in shifts
    ]
    wrong_candidates = [
        _expected_bitmap(
            sidecar, config, selected_monotonic_ns + shift * 1_000_000_000, wrong_state
        )
        for shift in shifts
    ]
    correct_scores = [_bitmap_f1(decoded.luma, candidate) for candidate in correct_candidates]
    wrong_scores = [_bitmap_f1(decoded.luma, candidate) for candidate in wrong_candidates]
    correct_index = max(range(len(correct_scores)), key=correct_scores.__getitem__)
    correct = correct_scores[correct_index]
    wrong = max(wrong_scores)
    margin = correct - wrong
    if correct < MIN_TEMPLATE_F1 or margin < MIN_WRONG_TEMPLATE_MARGIN:
        raise HarnessError(
            f"{observation.name} burned-overlay classifier refused: "
            f"f1={correct:.6f}, margin={margin:.6f}"
        )
    expected = correct_candidates[correct_index]
    return {
        "phase": observation.name,
        "expected_state": expected_state,
        "sidecar_clip_id": str(sidecar.clip_id),
        "phase_in_half_open_window": True,
        "pts_offset_ns": offset_ns,
        "clip_first_pts_s": round(decoded.first_pts_s, 6),
        "selected_pts_s": round(decoded.selected_pts_s, 6),
        "pts_mapping_error_ns": pts_error_ns,
        "pts_mapping_tolerance_ns": round(PTS_MAPPING_TOLERANCE_S * 1_000_000_000),
        "selected_frame_inside_phase_dwell": True,
        "threshold": BINARY_THRESHOLD,
        "minimum_f1": MIN_TEMPLATE_F1,
        "minimum_wrong_template_margin": MIN_WRONG_TEMPLATE_MARGIN,
        "best_projection_shift_s": shifts[correct_index],
        "correct_template_f1": round(correct, 6),
        "wrong_template_best_f1": round(wrong, 6),
        "wrong_template_margin": round(margin, 6),
        "wrong_state_discriminated": wrong_state,
        "gps_lost_marker_discriminated": expected_state == "stale",
        "navigation_fields_hidden": expected_state == "stale",
        "expected_bitmap_sha256": hashlib.sha256(expected).hexdigest(),
        "decoded_data_retained": False,
        "passed": True,
    }


def _time_model_evidence(
    sidecar: ClipSidecar, observations: Sequence[PhaseObservation]
) -> dict[str, object]:
    anchor = sidecar.time_anchor
    if (
        anchor is None
        or sidecar.start_utc is None
        or sidecar.end_utc is None
        or sidecar.start_local is None
    ):
        raise HarnessError("canonical sidecar lacks reconciled stable-anchor fields")
    projected_start = _project_utc(sidecar, sidecar.start_monotonic_ns)
    projected_end = _project_utc(sidecar, sidecar.end_monotonic_ns)
    start_error = abs(int((sidecar.start_utc - projected_start).total_seconds() * 1_000_000_000))
    end_error = abs(int((sidecar.end_utc - projected_end).total_seconds() * 1_000_000_000))
    local_error = abs(
        int(
            (
                sidecar.start_local - sidecar.start_utc.astimezone(sidecar.start_local.tzinfo)
            ).total_seconds()
            * 1_000_000_000
        )
    )
    if max(start_error, end_error, local_error) > MAX_PROJECTION_ERROR_NS:
        raise HarnessError("canonical sidecar does not follow stable-anchor monotonic projection")
    if sidecar.timestamp_quality is not TimestampQuality.GPS_ANCHORED:
        raise HarnessError("canonical sidecar timestamp quality is not GPS_ANCHORED")
    samples = sidecar.gps.samples
    sample_monotonic = [sample.monotonic_ns for sample in samples]
    ordered_unique = sample_monotonic == sorted(set(sample_monotonic))
    all_in_window = all(
        sidecar.start_monotonic_ns <= value < sidecar.end_monotonic_ns for value in sample_monotonic
    )
    sample_projection_errors = [
        abs(
            int(
                (sample.utc - _project_utc(sidecar, sample.monotonic_ns)).total_seconds()
                * 1_000_000_000
            )
        )
        for sample in samples
        if sample.utc is not None
    ]
    maximum_sample_error = max(sample_projection_errors, default=0)
    if not ordered_unique or not all_in_window:
        raise HarnessError("sidecar samples violate ordered half-open window ownership")
    if maximum_sample_error > MAX_PROJECTION_ERROR_NS:
        raise HarnessError("sidecar sample UTC differs from stable-anchor projection")
    return {
        "clip_id": str(sidecar.clip_id),
        "sequence": sidecar.sequence,
        "anchor_source": anchor.source.value,
        "timestamp_quality": sidecar.timestamp_quality.value,
        "timezone": sidecar.timezone,
        "all_phase_pts_in_half_open_window": all(
            sidecar.start_monotonic_ns <= item.monotonic_ns < sidecar.end_monotonic_ns
            for item in observations
        ),
        "start_projection_error_ns": start_error,
        "end_projection_error_ns": end_error,
        "local_projection_error_ns": local_error,
        "maximum_projection_error_ns": MAX_PROJECTION_ERROR_NS,
        "gps_sample_count": len(samples),
        "gps_samples_ordered_unique": ordered_unique,
        "gps_samples_in_half_open_window": all_in_window,
        "gps_samples_with_projected_utc": len(sample_projection_errors),
        "gps_sample_maximum_projection_error_ns": maximum_sample_error,
        "shared_gps_producer_and_stable_anchor_model": True,
        "literal_snapshot_identity_claimed": False,
        "canonical_sidecar_sha256": hashlib.sha256(sidecar.to_canonical_json()).hexdigest(),
    }


def _parse_video_packet_metrics(payload: bytes) -> tuple[int, float]:
    document = _strict_json(payload, "ffprobe video packet metrics")
    streams = document.get("streams")
    if not isinstance(streams, list) or len(streams) != 1:
        raise HarnessError("ffprobe video packet metrics stream shape differs")
    stream = streams[0]
    if not isinstance(stream, Mapping):
        raise HarnessError("ffprobe video packet metrics stream shape differs")
    packets_raw = stream.get("nb_read_packets")
    duration_raw = stream.get("duration")
    if (
        not isinstance(packets_raw, str)
        or re.fullmatch(r"[1-9][0-9]{0,6}", packets_raw) is None
        or not isinstance(duration_raw, str)
        or re.fullmatch(r"[0-9]{1,4}\.[0-9]{1,9}", duration_raw) is None
    ):
        raise HarnessError("ffprobe video packet metrics values differ")
    packets = int(packets_raw)
    duration_s = float(duration_raw)
    if not 0 < duration_s <= MAX_COMPLETE_DURATION_S + 1:
        raise HarnessError("ffprobe video duration is outside its parser bound")
    return packets, duration_s


def _probe_video_packet_metrics(video: Path, *, timeout: float = 10.0) -> tuple[int, float]:
    result = _command(
        (
            FFPROBE,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-count_packets",
            "-show_entries",
            "stream=duration,nb_read_packets",
            "-of",
            "json",
            str(video),
        ),
        timeout=timeout,
    )
    return _parse_video_packet_metrics(result.stdout)


def _complete_clip_metrics(
    *,
    sidecar_duration_s: float,
    sidecar_frames_written: int,
    actual_duration_s: float,
    actual_video_packets: int,
    dropped_frames: int,
    hardware_profile: bool,
) -> dict[str, object]:
    actual_packet_fps = actual_video_packets / actual_duration_s
    if (
        not MIN_COMPLETE_DURATION_S <= sidecar_duration_s <= MAX_COMPLETE_DURATION_S
        or not MIN_COMPLETE_DURATION_S <= actual_duration_s <= MAX_COMPLETE_DURATION_S
        or actual_packet_fps < MIN_COMPLETE_FPS
        or dropped_frames != 0
        or not hardware_profile
    ):
        raise HarnessError("complete boundary clip missed its duration/fps/drop gate")
    return {
        "sidecar_duration_s": round(sidecar_duration_s, 6),
        "actual_media_duration_s": round(actual_duration_s, 6),
        "sidecar_frames_written": sidecar_frames_written,
        "actual_video_packets": actual_video_packets,
        "frame_observer_packet_delta": sidecar_frames_written - actual_video_packets,
        "sidecar_frame_counter_matches_actual_packets": (
            sidecar_frames_written == actual_video_packets
        ),
        "sidecar_counter_used_for_functional_fps": False,
        "actual_packet_fps": round(actual_packet_fps, 6),
        "minimum_actual_packet_fps": MIN_COMPLETE_FPS,
        "dropped_frames": dropped_frames,
        "hardware_h264_profile_retained": hardware_profile,
    }


def _media_evidence(sidecar: ClipSidecar, *, timeout: float = 10.0) -> dict[str, object]:
    sidecar_duration_s = (sidecar.end_monotonic_ns - sidecar.start_monotonic_ns) / 1_000_000_000
    hardware_profile = (
        sidecar.video.codec.casefold() == "h264"
        and sidecar.video.width == 1920
        and sidecar.video.height == 1080
    )
    video = CLIPS_ROOT / sidecar.video_file
    if not video.is_file() or video.is_symlink():
        raise HarnessError("finalized video member is absent or unsafe")
    actual_video_packets, actual_duration_s = _probe_video_packet_metrics(video, timeout=timeout)
    evidence = _complete_clip_metrics(
        sidecar_duration_s=sidecar_duration_s,
        sidecar_frames_written=sidecar.video.frames_written,
        actual_duration_s=actual_duration_s,
        actual_video_packets=actual_video_packets,
        dropped_frames=sidecar.video.dropped_frames,
        hardware_profile=hardware_profile,
    )
    return {
        "clip_id": str(sidecar.clip_id),
        "sequence": sidecar.sequence,
        **evidence,
        "video_sha256": _sha256(video, 1024 * 1024 * 1024),
    }


def _phase_sample_evidence(
    sidecar: ClipSidecar,
    observation: PhaseObservation,
    *,
    expect_samples: bool,
) -> dict[str, object]:
    if observation.dwell_start_ns is None or observation.dwell_end_ns is None:
        raise HarnessError("phase sample qualification lacks its dwell interval")
    samples = tuple(
        sample
        for sample in sidecar.gps.samples
        if observation.dwell_start_ns <= sample.monotonic_ns < observation.dwell_end_ns
    )
    if expect_samples and not samples:
        raise HarnessError(f"{observation.name} dwell contains no exact synthetic sample")
    if not expect_samples and samples:
        raise HarnessError(f"{observation.name} dwell unexpectedly contains a GPS sample")
    zero_facts = all(
        sample.lat_deg == 0.0 and sample.lon_deg == 0.0 and sample.speed_mps == 0.0
        for sample in samples
    )
    projected = all(
        sample.utc is not None
        and abs(
            int(
                (sample.utc - _project_utc(sidecar, sample.monotonic_ns)).total_seconds()
                * 1_000_000_000
            )
        )
        <= MAX_PROJECTION_ERROR_NS
        for sample in samples
    )
    if expect_samples and (not zero_facts or not projected):
        raise HarnessError(f"{observation.name} synthetic sample facts differ")
    return {
        "phase": observation.name,
        "clip_id": str(sidecar.clip_id),
        "sample_count": len(samples),
        "expected_sample_presence": expect_samples,
        "presence_gate_passed": bool(samples) is expect_samples,
        "all_samples_zero_location": zero_facts,
        "all_samples_zero_speed": zero_facts,
        "all_samples_stable_anchor_projected": projected,
        "raw_sample_values_retained": False,
    }


def _boundary_evidence(first: ClipSidecar, second: ClipSidecar) -> dict[str, object]:
    if first.clip_id == second.clip_id or first.sequence + 1 != second.sequence:
        raise HarnessError("boundary sidecar identity/sequence differs")
    delta_ns = second.start_monotonic_ns - first.end_monotonic_ns
    if delta_ns != 0:
        raise HarnessError("canonical sidecar half-open boundary is not exactly adjacent")
    first_samples = {sample.monotonic_ns for sample in first.gps.samples}
    second_samples = {sample.monotonic_ns for sample in second.gps.samples}
    if first_samples & second_samples:
        raise HarnessError("canonical sidecars overlap in sample ownership")
    return {
        "first_clip_id": str(first.clip_id),
        "second_clip_id": str(second.clip_id),
        "consecutive_sequences": True,
        "boundary_delta_ns": delta_ns,
        "exact_half_open_adjacency": True,
        "sample_ownership_overlap_count": 0,
        "no_sample_overlap": True,
    }


def _run_scenario(config: DashcamConfig, expected_release: str) -> dict[str, object]:
    deadline = time.monotonic() + SCENARIO_TIMEOUT_S

    def remaining(maximum: float) -> float:
        value = min(maximum, deadline - time.monotonic())
        if value <= 0:
            raise HarnessError("scenario exceeded its global deadline")
        return value

    def dwell(
        name: str,
        predicate: Callable[[Mapping[str, object]], bool],
    ) -> PhaseObservation:
        start_status = _wait_status(
            lambda item: predicate(item) and _renderer_healthy(item),
            timeout=remaining(PHASE_TIMEOUT_S),
            phase=f"{name}_start",
        )
        start_ns = time.monotonic_ns()
        if time.monotonic() + PHASE_DWELL_S > deadline:
            raise HarnessError("scenario exceeded its global deadline")
        time.sleep(PHASE_DWELL_S)
        end_status = _wait_status(
            lambda item: predicate(item) and _renderer_healthy(item),
            timeout=remaining(PHASE_TIMEOUT_S),
            phase=f"{name}_end",
        )
        end_ns = time.monotonic_ns()
        return _phase(name, start_status, end_status, start_ns, end_ns)

    before_names = _clip_names()
    unsynced = dwell(
        "startup_unsynced",
        lambda item: (
            _recording(item)
            and _gps_time(item).get("state") == "UNSYNCED"
            and _gps(item).get("navigation") is None
        ),
    )
    initial_frames, initial_drops = _frames(unsynced.status)

    endpoint = _ACTIVE_ENDPOINT
    if endpoint is None:
        raise HarnessError("PTY endpoint was not installed")
    feeder = NmeaFeeder(endpoint)
    _set_active_feeder(feeder)
    feeder.start()
    valid = dwell(
        "valid_lock",
        lambda item: (
            _recording(item)
            and _gps(item).get("state") == "NAVIGATION_VALID"
            and _gps(item).get("navigation") is not None
            and _gps_time(item).get("state") == "GPS_TIME_VALID"
        ),
    )
    feeder.stop()
    _set_active_feeder(None)
    _wait_status(
        lambda item: (
            _recording(item)
            and _gps(item).get("state") == "STALE"
            and _gps(item).get("navigation") is None
            and _gps_time(item).get("state") == "GPS_TIME_STALE"
        ),
        timeout=remaining(PHASE_TIMEOUT_S),
        phase="stale_transition",
    )
    near_boundary_target = initial_frames + 1_560
    _wait_status(
        lambda item: (
            _recording(item)
            and _frames(item)[0] >= near_boundary_target
            and _gps(item).get("state") == "STALE"
        ),
        timeout=remaining(BOUNDARY_TIMEOUT_S),
        phase="near_first_boundary_stale",
    )
    stale_before = dwell(
        "gps_lost_before_boundary",
        lambda item: (
            _recording(item)
            and _gps(item).get("state") == "STALE"
            and _gps(item).get("navigation") is None
        ),
    )
    first = _wait_covering_sidecar(
        before_names,
        (unsynced, valid, stale_before),
        timeout=remaining(BOUNDARY_TIMEOUT_S),
    )
    stale_after = dwell(
        "gps_lost_after_boundary",
        lambda item: (
            _recording(item)
            and _gps(item).get("state") == "STALE"
            and _gps(item).get("navigation") is None
        ),
    )

    recovery_feeder = NmeaFeeder(endpoint, origin_monotonic=feeder.origin_monotonic)
    _set_active_feeder(recovery_feeder)
    recovery_feeder.start()
    recovered = dwell(
        "gps_recovered",
        lambda item: (
            _recording(item)
            and _gps(item).get("state") == "NAVIGATION_VALID"
            and _gps(item).get("navigation") is not None
            and _gps_time(item).get("state") == "GPS_TIME_VALID"
        ),
    )
    second = _wait_covering_sidecar(
        before_names,
        (stale_after, recovered),
        timeout=remaining(BOUNDARY_TIMEOUT_S),
    )
    recovery_feeder.stop()
    _set_active_feeder(None)
    boundary = _boundary_evidence(first, second)

    final_status = _wait_status(
        lambda item: _recording(item) and _renderer_healthy(item),
        timeout=remaining(PHASE_TIMEOUT_S),
        phase="final_status",
    )
    final_frames, final_drops = _frames(final_status)
    drop_baseline = 0 if initial_drops is None else initial_drops
    drop_final = 0 if final_drops is None else final_drops
    if drop_final != drop_baseline or _runtime(final_status).get("pipeline_restart_count") != 0:
        raise HarnessError("recording counters regressed through two-clip scenario")

    # Keep all software decoding, packet probing, and media hashing off the live
    # Pi recorder's CPU budget. The final live status above is the functional
    # zero-drop gate; only a verified clean service stop permits offline work.
    pre_analysis_stop = _stop_transient_for_offline_analysis(timeout=remaining(STOP_TIMEOUT_S))

    classifications = (
        _classify_burn_in(first, config, unsynced, "unsynced", "stale", timeout=remaining(12.0)),
        _classify_burn_in(first, config, valid, "valid", "stale", timeout=remaining(12.0)),
        _classify_burn_in(first, config, stale_before, "stale", "valid", timeout=remaining(12.0)),
        _classify_burn_in(second, config, stale_after, "stale", "valid", timeout=remaining(12.0)),
        _classify_burn_in(second, config, recovered, "valid", "stale", timeout=remaining(12.0)),
    )
    sample_evidence = (
        _phase_sample_evidence(first, unsynced, expect_samples=False),
        _phase_sample_evidence(first, valid, expect_samples=True),
        _phase_sample_evidence(first, stale_before, expect_samples=False),
        _phase_sample_evidence(second, stale_after, expect_samples=False),
        _phase_sample_evidence(second, recovered, expect_samples=True),
    )
    remaining(1.0)
    return {
        "release": expected_release,
        "phases": [
            _phase_summary(item) for item in (unsynced, valid, stale_before, stale_after, recovered)
        ],
        "burn_in_classification": list(classifications),
        "phase_sample_ownership": list(sample_evidence),
        "first_time_model": _time_model_evidence(first, (unsynced, valid, stale_before)),
        "second_time_model": _time_model_evidence(second, (stale_after, recovered)),
        "boundary": boundary,
        "pre_analysis_stop": pre_analysis_stop,
        "first_complete_clip": _media_evidence(first, timeout=remaining(10.0)),
        "second_complete_clip": _media_evidence(second, timeout=remaining(10.0)),
        "stress": {
            "initial_encoded_frames": initial_frames,
            "final_encoded_frames": final_frames,
            "drop_baseline": drop_baseline,
            "drop_final": drop_final,
            "pipeline_restarts": 0,
            "renderer_healthy": _renderer_healthy(final_status),
            "stale_burn_in_proved_on_both_sides": True,
        },
        "synthetic_sentences_sent": feeder.sent + recovery_feeder.sent,
        "phase_dwell_s": PHASE_DWELL_S,
        "phase_pts_mapping": {
            "actual_selected_pts_measured": True,
            "first_pts_tolerance_s": FIRST_PTS_TOLERANCE_S,
            "mapping_tolerance_s": PTS_MAPPING_TOLERANCE_S,
            "sidecar_monotonic_offset_used_as_media_running_time": True,
        },
    }


_ACTIVE_ENDPOINT: PtyEndpoint | None = None
_ACTIVE_FEEDER: NmeaFeeder | None = None


def _set_active_feeder(feeder: NmeaFeeder | None) -> None:
    global _ACTIVE_FEEDER
    _ACTIVE_FEEDER = feeder


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
            document, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
        )
        + "\n"
    ).encode()
    if len(payload) > MAX_RESULT_BYTES:
        raise HarnessError("evidence result exceeds its byte bound")
    _write_exclusive(path, payload, 0o600)
    return hashlib.sha256(payload).hexdigest()


@contextmanager
def _qualification_lock() -> Iterator[None]:
    try:
        import fcntl
    except ImportError as error:
        raise HarnessError("exclusive qualification locking is unavailable") from error
    fd = os.open(
        LOCK_PATH,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        try:
            flock = cast(Callable[[int, int], None], _member(fcntl, "flock"))
            lock_ex = int(cast(SupportsInt, _member(fcntl, "LOCK_EX")))
            lock_nb = int(cast(SupportsInt, _member(fcntl, "LOCK_NB")))
            flock(fd, lock_ex | lock_nb)
        except BlockingIOError as error:
            raise HarnessError("another live qualification is already active") from error
        yield
    finally:
        os.close(fd)


def qualify(arguments: argparse.Namespace, manifest: Mapping[str, str]) -> dict[str, object]:
    global _ACTIVE_ENDPOINT, _ACTIVE_FEEDER
    evidence: dict[str, object] = {
        "schema_version": 1,
        "phase": "milestone9_exact_pi_functional_overlay",
        "passed": False,
        "manifest": {"sha256": arguments.expected_manifest_sha256, "members": dict(manifest)},
        "privacy": {
            "synthetic_zero_coordinate_source_only": True,
            "coordinates_retained_in_result": False,
            "raw_nmea_retained_in_result": False,
            "decoded_pixels_retained_in_result": False,
            "product_media_remains_only_on_pi_exfat": True,
        },
        "ordinary_service_started": False,
        "network_or_ap_mutations": 0,
        "storage_format_or_partition_mutations": 0,
        "wall_clock_mutations": 0,
    }
    failures: list[str] = []
    endpoint: PtyEndpoint | None = None
    ordinary_before: dict[str, str] | None = None
    network_before: dict[str, str] | None = None
    config_before: str | None = None
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
        config_before = _sha256(CONFIG_PATH, 256 * 1024)
        evidence["production_config_sha256_before"] = config_before
        if config_before != EXPECTED_PRODUCTION_CONFIG_SHA256:
            raise HarnessError("production config hash differs from the accepted release")
        evidence["throttle_before"] = _throttle()
        if evidence["throttle_before"] != "throttled=0x0":
            raise HarnessError("Pi was throttled before qualification")
        config = _temporary_config(load_config(CONFIG_PATH))
        group_id = _dashcam_group_id()
        interpreter = Path(cast(Mapping[str, str], evidence["release"])["interpreter"])
        _prepare_transient_files(config, interpreter, group_id)
        endpoint = PtyEndpoint(group_id=group_id)
        endpoint.install()
        _ACTIVE_ENDPOINT = endpoint
        try:
            _systemctl("start", UNIT_NAME, timeout=START_TIMEOUT_S)
        except BaseException:
            if STATUS_PATH.is_file() and not STATUS_PATH.is_symlink():
                try:
                    evidence["transient_start_failure_status"] = _startup_failure_summary(_status())
                except BaseException as capture_error:
                    evidence["transient_start_failure_status"] = {
                        "status_present": True,
                        "capture_error": _detail(
                            f"{type(capture_error).__name__}: {capture_error}"
                        ),
                    }
            else:
                evidence["transient_start_failure_status"] = {"status_absent": True}
            raise
        evidence["scenario"] = _run_scenario(config, arguments.expected_release)
    except BaseException as error:
        failures.append(_detail(f"{type(error).__name__}: {error}"))
    finally:
        feeder = _ACTIVE_FEEDER
        if feeder is not None:
            try:
                feeder.stop()
            except BaseException as error:
                failures.append(_detail(f"feeder cleanup failed: {error}"))
        failures.extend(_remove_transient_files(_ACTIVE_ENDPOINT or endpoint))
        _ACTIVE_FEEDER = None
        _ACTIVE_ENDPOINT = None

    try:
        evidence["temporary_unit_after"] = _unit_properties(UNIT_NAME)
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
        if (
            ordinary_before is not None
            and ordinary_after["NRestarts"] != ordinary_before["NRestarts"]
        ):
            failures.append("ordinary dashcamd NRestarts changed")
        if network_before is not None and network_after != network_before:
            failures.append("network fallback service state changed")
        config_after = _sha256(CONFIG_PATH, 256 * 1024)
        evidence["production_config_sha256_after"] = config_after
        evidence["production_config_unchanged"] = (
            config_before is not None and config_after == config_before
        )
        if evidence["production_config_unchanged"] is not True:
            failures.append("production config hash changed")
        evidence["throttle_after"] = _throttle()
        if evidence["throttle_after"] != "throttled=0x0":
            failures.append("Pi was throttled during qualification")
        cleanup = cast(Mapping[str, object], evidence["temporary_artifacts_absent"])
        if not all(item is True for item in cleanup.values()):
            failures.append("one or more temporary artifacts remain")
    except BaseException as error:
        failures.append(_detail(f"post-cleanup verification failed: {error}"))
    evidence["failures"] = failures
    evidence["passed"] = not failures
    _assert_privacy_safe(evidence)
    return evidence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="milestone9-overlay")
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
                    "phase": "milestone9_exact_pi_functional_overlay",
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
                    "phase": "milestone9_exact_pi_functional_overlay",
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
                "phase": "milestone9_exact_pi_functional_overlay",
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
