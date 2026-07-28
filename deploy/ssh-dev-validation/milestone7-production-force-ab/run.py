#!/usr/bin/env python3
"""Bounded exact-Pi force-key A/B diagnostic on the production three-slot graph."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, cast

import dashcam
from dashcam.audio.alsa import AlsaIdentity, parse_alsa_selector
from dashcam.audio.linux import AudioDiscoveryStatus, discover_capture_device
from dashcam.config import load_config
from dashcam.diagnostics.media import run_fixed_argv
from dashcam.recorder.gstreamer import (
    AudioCapturePlan,
    BusMessageKind,
    PyGObjectGStreamerDriver,
    build_audio_pipeline_description,
)

CONFIG_PATH: Final = Path("/etc/dashcam/config.toml")
RECORDING_ROOT: Final = Path("/srv/dashcam")
PENDING_ROOT: Final = RECORDING_ROOT / "pending"
SYS_DEVICES_ROOT: Final = Path("/sys/devices")
SYSTEMCTL: Final = "/usr/bin/systemctl"
UDEVADM: Final = "/usr/bin/udevadm"
VCGENCMD: Final = "/usr/bin/vcgencmd"
MANIFEST_MEMBERS: Final = ("README.md", "run.py")
SHA256_RE: Final = re.compile(r"[0-9a-f]{64}")
UDEV_PATH_RE: Final = re.compile(r"/devices/[A-Za-z0-9_.:/-]{1,1000}")
MAX_MANIFEST_BYTES: Final = 4096
MAX_RESULT_BYTES: Final = 4 * 1024 * 1024
MAX_EVENTS: Final = 32
MAX_PROBE_FAILURES: Final = 16
MAX_MEDIA_MEMBERS: Final = 32
MAX_MEDIA_BYTES: Final = 256 * 1024 * 1024
REQUEST_A: Final = 3_601_000_001
REQUEST_B: Final = 3_601_000_002
START_TIMEOUT_S: Final = 20.0
BASELINE_TIMEOUT_S: Final = 10.0
REQUEST_TIMEOUT_S: Final = 3.0
AUDIO_ERROR_TIMEOUT_S: Final = 12.0
POST_ERROR_FLOW_S: Final = 1.0
HEALTHY_SETTLE_S: Final = 0.5
GATE_PROBE_TIMEOUT_S: Final = 0.5
GATE_WORKER_TIMEOUT_S: Final = 5.0
NULL_TIMEOUT_S: Final = 5.0


class HarnessError(RuntimeError):
    """The diagnostic was unsafe, inconclusive, or violated a strict bound."""


def _detail(value: object, maximum: int = 512) -> str:
    text = " ".join(str(value).replace("\0", " ").splitlines())
    return "".join(char if char.isprintable() else " " for char in text)[:maximum]


def _regular_bytes(path: Path, maximum: int) -> bytes:
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise HarnessError(f"{path} is not a regular file")
        payload = bytearray()
        while chunk := os.read(descriptor, min(65536, maximum + 1 - len(payload))):
            payload.extend(chunk)
            if len(payload) > maximum:
                raise HarnessError(f"{path} exceeded read bound")
        return bytes(payload)
    finally:
        os.close(descriptor)


def _sha256(path: Path, maximum: int) -> str:
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    digest, size = hashlib.sha256(), 0
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise HarnessError(f"{path} is not regular")
        while chunk := os.read(descriptor, 1024 * 1024):
            size += len(chunk)
            if size > maximum:
                raise HarnessError(f"{path} exceeded hash bound")
            digest.update(chunk)
    finally:
        os.close(descriptor)
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
        if _sha256(root / name, 2 * 1024 * 1024) != digest:
            raise HarnessError(f"manifest member {name} failed verification")
    return entries


def _is_rootfs_parent(parent: Path) -> bool:
    try:
        info, recording = os.lstat(parent), os.lstat(RECORDING_ROOT)
    except OSError:
        return False
    return (
        stat.S_ISDIR(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and info.st_dev != recording.st_dev
    )


def _write_result(path: Path, value: Mapping[str, object]) -> None:
    if not path.is_absolute() or path == RECORDING_ROOT or RECORDING_ROOT in path.parents:
        raise HarnessError("evidence output must be an absolute rootfs path")
    if not path.parent.is_dir():
        raise HarnessError("evidence output parent must already exist")
    parent = path.parent.resolve(strict=True)
    if path.parent != parent or path.exists() or path.is_symlink() or not _is_rootfs_parent(parent):
        raise HarnessError("evidence output must be a new direct rootfs file")
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(payload) > MAX_RESULT_BYTES:
        raise HarnessError("evidence JSON exceeded its bound")
    descriptor, temporary = tempfile.mkstemp(prefix=".m7-force-ab-", dir=parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary_path, path)
        except FileExistsError as error:
            raise HarnessError("evidence output already exists") from error
        try:
            directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            if os.name != "nt":
                raise
        else:
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)


def _release_identity() -> dict[str, str]:
    prefix = Path(sys.prefix).resolve(strict=True)
    package = Path(dashcam.__file__).resolve(strict=True)
    parts = prefix.as_posix().split("/")
    if (
        len(parts) < 6
        or parts[:4] != ["", "opt", "dashcam", "releases"]
        or parts[-1] != "venv"
        or not package.is_relative_to(prefix)
    ):
        raise HarnessError("interpreter and package are not one installed release")
    return {"release": parts[4], "venv": str(prefix), "package": str(package)}


def _unit_state() -> dict[str, object]:
    values: dict[str, str] = {}
    for name in ("ActiveState", "SubState", "MainPID", "NRestarts", "UnitFileState"):
        result = run_fixed_argv(
            (SYSTEMCTL, "show", "--no-pager", f"--property={name}", "--value", "dashcamd.service"),
            timeout_seconds=5.0,
            max_output_bytes=1024,
        )
        if result.returncode or result.timed_out or result.output_truncated:
            raise HarnessError("dashcamd read-only state query failed")
        values[name] = result.stdout.decode("ascii", "strict").strip()
    if (
        values["ActiveState"] != "inactive"
        or values["SubState"] != "dead"
        or values["MainPID"] != "0"
        or not values["NRestarts"].isdigit()
    ):
        raise HarnessError("dashcamd must be exactly inactive/dead")
    return {
        "active": "inactive",
        "sub": "dead",
        "main_pid": 0,
        "restarts": int(values["NRestarts"]),
        "unit_file_state": values["UnitFileState"],
    }


def _throttle() -> str:
    result = run_fixed_argv(
        (VCGENCMD, "get_throttled"), timeout_seconds=5.0, max_output_bytes=1024
    )
    value = result.stdout.decode("ascii", "strict").strip()
    if (
        result.returncode
        or result.timed_out
        or result.output_truncated
        or re.fullmatch(r"throttled=0x[0-9a-fA-F]+", value) is None
    ):
        raise HarnessError("throttle query failed")
    return value


def _identity(identity: AlsaIdentity) -> dict[str, object]:
    return {
        "vendor_id": identity.vendor_id,
        "product_id": identity.product_id,
        "product": identity.product,
        "physical_path": identity.physical_path,
        "serial": identity.serial,
        "alsa_card_id": identity.alsa_card_id,
    }


def _same_identity(left: AlsaIdentity, right: AlsaIdentity) -> bool:
    return (left.vendor_id, left.product_id, left.product, left.physical_path, left.serial) == (
        right.vendor_id,
        right.product_id,
        right.product,
        right.physical_path,
        right.serial,
    )


def _usb_authorized_path(
    udev_device_path: str, identity: AlsaIdentity, *, root: Path = SYS_DEVICES_ROOT
) -> Path:
    if UDEV_PATH_RE.fullmatch(udev_device_path) is None:
        raise HarnessError("udev device path is unsafe")
    root = root.resolve(strict=True)
    current = (root / udev_device_path.removeprefix("/devices/")).resolve(strict=True)
    if not current.is_relative_to(root):
        raise HarnessError("udev path escaped sysfs")
    candidates: list[Path] = []
    for _ in range(16):
        fields = tuple(
            current / name for name in ("idVendor", "idProduct", "product", "authorized")
        )
        if all(item.exists() and not item.is_symlink() for item in fields):
            observed = (
                _regular_bytes(fields[0], 32).decode("ascii").strip().casefold(),
                _regular_bytes(fields[1], 32).decode("ascii").strip().casefold(),
                "_".join(_regular_bytes(fields[2], 256).decode().strip().split()),
            )
            if observed == (identity.vendor_id, identity.product_id, identity.product):
                candidates.append(current)
        if current == root:
            break
        current = current.parent
    if len(candidates) != 1:
        raise HarnessError("udev ancestry did not identify one exact USB authorization node")
    return candidates[0] / "authorized"


def _udev_path(control: Path) -> str:
    result = run_fixed_argv(
        (UDEVADM, "info", "--query=path", "--name", str(control)),
        timeout_seconds=5.0,
        max_output_bytes=4096,
    )
    value = result.stdout.decode("ascii", "strict").strip()
    if (
        result.returncode
        or result.timed_out
        or result.output_truncated
        or result.stderr
        or UDEV_PATH_RE.fullmatch(value) is None
    ):
        raise HarnessError("udev device-path query failed")
    return value


def read_authorized(path: Path) -> int:
    value = _regular_bytes(path, 8)
    if value not in (b"0", b"0\n", b"1", b"1\n"):
        raise HarnessError("USB authorization attribute shape differs")
    return int(value.strip())


def _write_authorized_byte(path: Path, value: int) -> None:
    descriptor = os.open(
        path, os.O_WRONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        if (
            not stat.S_ISREG(os.fstat(descriptor).st_mode)
            or os.write(descriptor, str(value).encode("ascii")) != 1
        ):
            raise HarnessError("USB authorization write failed")
    finally:
        os.close(descriptor)


def write_authorized(path: Path, value: int, *, expected: int) -> dict[str, object]:
    if value not in (0, 1) or expected not in (0, 1) or value == expected:
        raise HarnessError("USB authorization transition is invalid")
    if read_authorized(path) != expected:
        raise HarnessError("USB authorization precondition differs")
    _write_authorized_byte(path, value)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            if read_authorized(path) == value:
                return {
                    "path": str(path),
                    "from": expected,
                    "to": value,
                    "confirmed": True,
                    "monotonic_ns": time.monotonic_ns(),
                }
        except OSError:
            pass
        time.sleep(0.05)
    raise HarnessError("USB authorization readback timed out")


def restore_authorized(path: Path) -> dict[str, object]:
    try:
        if read_authorized(path) == 1:
            return {"confirmed": True, "write_required": False, "monotonic_ns": time.monotonic_ns()}
    except (HarnessError, OSError):
        pass
    _write_authorized_byte(path, 1)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            if read_authorized(path) == 1:
                return {
                    "confirmed": True,
                    "write_required": True,
                    "monotonic_ns": time.monotonic_ns(),
                }
        except (HarnessError, OSError):
            pass
        time.sleep(0.05)
    raise HarnessError("USB authorization restoration readback timed out")


def _contains_h264_idr_bytes(payload: bytes) -> bool:
    for marker in (b"\x00\x00\x01", b"\x00\x00\x00\x01"):
        start = 0
        while (index := payload.find(marker, start)) >= 0:
            position = index + len(marker)
            if position < len(payload) and payload[position] & 0x1F == 5:
                return True
            start = position
    offset = 0
    while offset + 4 <= len(payload):
        size = int.from_bytes(payload[offset : offset + 4], "big")
        offset += 4
        if size <= 0 or offset + size > len(payload):
            return False
        if payload[offset] & 0x1F == 5:
            return True
        offset += size
    return False


def _parse_force_key_event(gstvideo: Any, event: Any, direction: str) -> tuple[int, bool] | None:
    if not bool(gstvideo.video_event_is_force_key_unit(event)):
        return None
    parser = (
        gstvideo.video_event_parse_upstream_force_key_unit
        if direction == "upstream"
        else gstvideo.video_event_parse_downstream_force_key_unit
    )
    try:
        parsed = parser(event)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, tuple) or len(parsed) < 4 or parsed[0] is not True:
        return None
    all_headers, count = parsed[-2], parsed[-1]
    if not isinstance(all_headers, bool) or isinstance(count, bool) or not isinstance(count, int):
        raise HarnessError("force-key parser returned invalid fields")
    return count, all_headers


@dataclass
class FlowCounter:
    count: int = 0
    non_delta: int = 0
    nal5: int = 0
    first_pts_ns: int | None = None
    last_pts_ns: int | None = None

    def observe(self, buffer: Any, gst: Any, *, scan: bool) -> None:
        self.count += 1
        pts = int(buffer.pts)
        if 0 <= pts < 2**63:
            if self.first_pts_ns is None:
                self.first_pts_ns = pts
            self.last_pts_ns = pts
        if not buffer.has_flags(gst.BufferFlags.DELTA_UNIT):
            self.non_delta += 1
            if scan:
                mapped, info = buffer.map(gst.MapFlags.READ)
                if mapped:
                    try:
                        self.nal5 += int(_contains_h264_idr_bytes(bytes(info.data)))
                    finally:
                        buffer.unmap(info)

    def snapshot(self) -> dict[str, int | None]:
        return {
            "count": self.count,
            "non_delta": self.non_delta,
            "nal5": self.nal5,
            "first_pts_ns": self.first_pts_ns,
            "last_pts_ns": self.last_pts_ns,
        }


@dataclass
class Diagnostic:
    driver: PyGObjectGStreamerDriver
    gst: Any
    gstvideo: Any
    pipeline: Any
    construction_thread_id: int
    events: list[dict[str, object]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    counters: dict[str, FlowCounter] = field(default_factory=dict)
    probes: list[tuple[Any, int]] = field(default_factory=list)
    requests: dict[int, dict[str, object]] = field(default_factory=dict)
    clock_identity: int | None = None
    base_time_ns: int | None = None

    def element(self, name: str) -> Any:
        result = self.pipeline.get_by_name(name)
        if result is None:
            raise HarnessError(f"required production element {name} is absent")
        return result

    def _record_failure(self, error: BaseException) -> None:
        if len(self.failures) < MAX_PROBE_FAILURES:
            self.failures.append(_detail(f"{type(error).__name__}: {error}"))

    def install_probes(self) -> None:
        flow_pads = {
            "encoder_src": ("encoder", "src", True),
            "parser_src": ("parser", "src", True),
            "video_tee_sink": ("video_tee", "sink", False),
            "continuity_src": ("video_continuity_queue", "src", False),
            "g01_valve_src": ("g01_video_valve", "src", False),
            "g01_queue_src": ("g01_video_queue", "src", False),
            "g02_valve_src": ("g02_video_valve", "src", False),
            "g02_queue_src": ("g02_video_queue", "src", False),
            "g03_valve_src": ("g03_video_valve", "src", False),
            "g03_queue_src": ("g03_video_queue", "src", False),
        }
        for label, (element_name, pad_name, scan) in flow_pads.items():
            pad = self.element(element_name).get_static_pad(pad_name)
            if pad is None:
                raise HarnessError(f"flow pad {label} is absent")
            counter = self.counters.setdefault(label, FlowCounter())

            def observe(
                _pad: Any,
                info: Any,
                item: FlowCounter = counter,
                do_scan: bool = scan,
            ) -> Any:
                try:
                    buffer = info.get_buffer()
                    if buffer is not None:
                        item.observe(buffer, self.gst, scan=do_scan)
                except BaseException as error:
                    self._record_failure(error)
                return self.gst.PadProbeReturn.OK

            probe_id = int(pad.add_probe(self.gst.PadProbeType.BUFFER, observe))
            if probe_id <= 0:
                raise HarnessError(f"flow probe {label} was refused")
            self.probes.append((pad, probe_id))

        for element_name in ("encoder", "parser"):
            for pad_name in ("sink", "src"):
                pad = self.element(element_name).get_static_pad(pad_name)
                if pad is None:
                    raise HarnessError(f"event pad {element_name}.{pad_name} is absent")
                for direction, probe_type in (
                    ("upstream", self.gst.PadProbeType.EVENT_UPSTREAM),
                    ("downstream", self.gst.PadProbeType.EVENT_DOWNSTREAM),
                ):
                    label = f"{element_name}.{pad_name}"

                    def event_probe(
                        _pad: Any,
                        info: Any,
                        observed_direction: str = direction,
                        observed_label: str = label,
                    ) -> Any:
                        try:
                            event = info.get_event()
                            if event is None:
                                return self.gst.PadProbeReturn.OK
                            parsed = _parse_force_key_event(
                                self.gstvideo, event, observed_direction
                            )
                            if parsed is None:
                                return self.gst.PadProbeReturn.OK
                            count, all_headers = parsed
                            if len(self.events) >= MAX_EVENTS:
                                raise HarnessError("force-event evidence exceeded bound")
                            self.events.append(
                                {
                                    "pad": observed_label,
                                    "direction": observed_direction,
                                    "count": count,
                                    "all_headers": all_headers,
                                    "seqnum": int(event.get_seqnum()),
                                    "thread_id": threading.get_ident(),
                                    "monotonic_ns": time.monotonic_ns(),
                                }
                            )
                        except BaseException as error:
                            self._record_failure(error)
                        return self.gst.PadProbeReturn.OK

                    probe_id = int(pad.add_probe(probe_type, event_probe))
                    if probe_id <= 0:
                        raise HarnessError(f"event probe {label}/{direction} was refused")
                    self.probes.append((pad, probe_id))

    def flow_snapshot(self) -> dict[str, dict[str, int | None]]:
        return {name: counter.snapshot() for name, counter in sorted(self.counters.items())}

    def graph_snapshot(self, phase: str) -> dict[str, object]:
        elements: dict[str, object] = {}
        valve_drop: dict[str, bool] = {}
        for name in (
            "camera",
            "encoder",
            "parser",
            "video_tee",
            "video_continuity_queue",
            "audio_source",
            "g01_video_valve",
            "g02_video_valve",
            "g03_video_valve",
        ):
            element = self.element(name)
            outcome = element.get_state(0)
            if name in {"g01_video_valve", "g02_video_valve", "g03_video_valve"}:
                valve_drop[name] = bool(element.get_property("drop"))
            pads: dict[str, object] = {}
            for pad_name in ("sink", "src"):
                pad = element.get_static_pad(pad_name)
                if pad is None:
                    continue
                peer = pad.get_peer()
                pads[pad_name] = {
                    "active": bool(pad.is_active()),
                    "blocked": bool(pad.is_blocked()),
                    "blocking": bool(pad.is_blocking()),
                    "linked": bool(pad.is_linked()),
                    "peer": None if peer is None else str(peer.get_path_string()),
                }
            elements[name] = {
                "state_return": str(outcome[0].value_nick),
                "state": str(outcome[1].value_nick),
                "pending": str(outcome[2].value_nick),
                "pads": pads,
            }
        clock = self.pipeline.get_clock()
        topology = self.driver.generation_snapshot(self.pipeline)
        if (
            topology.get("topology_observation") != "stable"
            or topology.get("topology_observation_stale") is not False
            or topology.get("active_slot_id") != 1
            or topology.get("slot_count") != 3
            or valve_drop
            != {
                "g01_video_valve": False,
                "g02_video_valve": True,
                "g03_video_valve": True,
            }
        ):
            raise HarnessError("original three-slot A/V routing is not exactly intact")
        return {
            "phase": phase,
            "thread_id": threading.get_ident(),
            "monotonic_ns": time.monotonic_ns(),
            "clock_identity": None if clock is None else id(clock),
            "base_time_ns": int(self.pipeline.get_base_time()),
            "elements": elements,
            "topology": topology,
            "valve_drop": valve_drop,
            "flow": self.flow_snapshot(),
        }

    def start(self) -> dict[str, object]:
        start_thread = threading.get_ident()
        self.driver.set_playing(self.pipeline, START_TIMEOUT_S)
        self.clock_identity = id(self.pipeline.get_clock())
        self.base_time_ns = int(self.pipeline.get_base_time())
        deadline = time.monotonic() + BASELINE_TIMEOUT_S
        while self.counters["parser_src"].count < 45:
            if time.monotonic() >= deadline:
                raise HarnessError("initial production video flow timed out")
            message = self.driver.poll_bus(self.pipeline, 0.05)
            if message.kind in {
                BusMessageKind.ERROR,
                BusMessageKind.AUDIO_ERROR,
                BusMessageKind.EOS,
            }:
                raise HarnessError(f"pipeline ended during baseline: {message.kind.value}")
        return {
            "construction_thread_id": self.construction_thread_id,
            "start_thread_id": start_thread,
            "same_thread": start_thread == self.construction_thread_id,
        }

    def force(self, count: int, phase: str) -> dict[str, object]:
        source = self.element("encoder").get_static_pad("src")
        if source is None:
            raise HarnessError("encoder source pad is absent")
        before_events, before_flow = len(self.events), self.flow_snapshot()
        event = self.gstvideo.video_event_new_upstream_force_key_unit(
            self.gst.CLOCK_TIME_NONE, True, count
        )
        if event is None:
            raise HarnessError("GstVideo did not construct upstream force-key event")
        request = {
            "phase": phase,
            "count": count,
            "seqnum": int(event.get_seqnum()),
            "thread_id": threading.get_ident(),
            "monotonic_ns": time.monotonic_ns(),
            "before_flow": before_flow,
        }
        self.requests[count] = request
        started = time.monotonic_ns()
        request["send_result"] = bool(source.send_event(event))
        request["send_duration_ns"] = time.monotonic_ns() - started
        if request["send_result"] is not True:
            raise HarnessError(f"force request {phase} was refused synchronously")
        deadline = time.monotonic() + REQUEST_TIMEOUT_S
        while time.monotonic() < deadline:
            correlated = [item for item in self.events if item["count"] == count]
            after = self.flow_snapshot()
            before_parser = cast(int, before_flow["parser_src"]["nal5"])
            if correlated and cast(int, after["parser_src"]["nal5"]) > before_parser:
                break
            message = self.driver.poll_bus(self.pipeline, 0.02)
            if message.kind is BusMessageKind.ERROR:
                raise HarnessError(f"non-audio pipeline error during request {phase}")
            if message.kind is BusMessageKind.EOS:
                raise HarnessError(f"pipeline EOS during request {phase}")
        request["after_flow"] = self.flow_snapshot()
        request["events"] = self.events[before_events:]
        request["correlated_event_count"] = sum(
            int(item["count"] == count) for item in self.events[before_events:]
        )
        request["nal5_delta"] = (
            cast(int, cast(Mapping[str, object], request["after_flow"])["parser_src"]["nal5"])  # type: ignore[index]
            - cast(int, before_flow["parser_src"]["nal5"])
        )
        request["completed_with_event_and_nal5"] = (
            cast(int, request["correlated_event_count"]) > 0
            and cast(int, request["nal5_delta"]) > 0
        )
        return request

    def wait_audio_error(self) -> dict[str, object]:
        deadline = time.monotonic() + AUDIO_ERROR_TIMEOUT_S
        observed: list[dict[str, object]] = []
        while time.monotonic() < deadline:
            message = self.driver.poll_bus(self.pipeline, 0.05)
            if message.kind is BusMessageKind.NONE:
                continue
            record = {
                "kind": message.kind.value,
                "source_name": message.source_name,
                "detail": _detail(message.detail),
                "monotonic_ns": time.monotonic_ns(),
            }
            observed.append(record)
            if len(observed) > 16:
                raise HarnessError("bus evidence exceeded bound")
            if message.kind is BusMessageKind.AUDIO_ERROR:
                if message.source_name != "audio_source":
                    raise HarnessError("terminal audio error source differs")
                return {"terminal": record, "messages": observed}
            if message.kind in {BusMessageKind.ERROR, BusMessageKind.EOS}:
                raise HarnessError("parent pipeline failed before exact terminal audio error")
        raise HarnessError("exact terminal audio error timed out")

    def stop(self) -> None:
        self.driver.set_null(self.pipeline, NULL_TIMEOUT_S)
        for pad, probe_id in self.probes:
            pad.remove_probe(probe_id)
        self.probes.clear()


def _release_production_gate(
    driver: PyGObjectGStreamerDriver,
    gate: Any,
    *,
    timeout_s: float,
) -> None:
    if gate.event_probe_id:
        driver._remove_retained_probe(gate.event_pad, gate.event_probe_id, timeout_s)
    driver._release_block_probe(
        gate.video_pad,
        gate.video_probe_id,
        reached=gate.reached,
        completed=gate.completed,
        release=gate.release,
        timeout_s=timeout_s,
    )


def _parser_blind_spot_shape(
    events: Sequence[Mapping[str, object]],
    count: int,
) -> dict[str, int]:
    shape = {"encoder.src": 0, "parser.sink": 0, "parser.src": 0}
    for item in events:
        if item.get("count") != count or item.get("direction") != "downstream":
            continue
        pad = item.get("pad")
        if pad in shape:
            shape[pad] += 1
    if shape != {"encoder.src": 1, "parser.sink": 1, "parser.src": 0}:
        raise HarnessError("current gate refusal lacks the exact parser blind-spot evidence")
    return shape


def _diagnostic_encoder_edge_gate(
    diagnostic: Diagnostic,
    context: Any,
    generation: Any,
    *,
    timeout_s: float,
) -> dict[str, object]:
    """Correlate at encoder.src and briefly hold the exact NAL5 at tee.sink."""

    driver, gst, gstvideo = diagnostic.driver, diagnostic.gst, diagnostic.gstvideo
    encoder = diagnostic.element("encoder")
    encoder_source = encoder.get_static_pad("src")
    video_sink = diagnostic.element("video_tee").get_static_pad("sink")
    if (
        encoder_source is None
        or video_sink is None
        or encoder_source.get_parent() is not encoder
        or generation.activation_id is None
        or context.generations.get(generation.generation_id) is not generation
        or not generation.linked
    ):
        raise HarnessError("diagnostic edge-gate ownership differs")
    count = context.next_force_key_count
    if count != 1:
        raise HarnessError("diagnostic edge gate did not receive exact low count 1")
    context.next_force_key_count += 1
    observed: dict[str, object] = {
        "count": count,
        "worker_thread_id": threading.get_ident(),
        "foreign_downstream_events": 0,
    }
    failed = threading.Event()
    correlated = threading.Event()
    reached = threading.Event()
    completed = threading.Event()
    release = threading.Event()
    event_removed = False
    event_probe_id: int | None = None
    video_probe_id: int | None = None
    dispatch: Any | None = None
    deadline = time.monotonic() + timeout_s

    def fail(detail: str) -> None:
        observed.setdefault("failure", detail)
        failed.set()

    def event_probe(_pad: Any, info: Any) -> Any:
        nonlocal event_removed
        try:
            event = info.get_event()
            if event is None or not bool(gstvideo.video_event_is_force_key_unit(event)):
                return gst.PadProbeReturn.OK
            parsed = gstvideo.video_event_parse_downstream_force_key_unit(event)
            if not isinstance(parsed, tuple) or len(parsed) != 6 or parsed[0] is not True:
                fail("diagnostic downstream force-key event shape is invalid")
                return gst.PadProbeReturn.OK
            _, _timestamp, _stream_time, running_time, all_headers, event_count = parsed
            if event_count != count:
                foreign = cast(int, observed["foreign_downstream_events"]) + 1
                observed["foreign_downstream_events"] = foreign
                if foreign > 8:
                    fail("diagnostic foreign downstream force events exceeded bound")
                return gst.PadProbeReturn.OK
            if (
                isinstance(running_time, bool)
                or not isinstance(running_time, int)
                or running_time < 0
                or running_time == 2**64 - 1
                or all_headers is not True
                or "response_seqnum" in observed
            ):
                fail("diagnostic correlated downstream force event is invalid")
                return gst.PadProbeReturn.OK
            observed["response_seqnum"] = int(event.get_seqnum())
            observed["response_running_time_ns"] = running_time
            observed["response_monotonic_ns"] = time.monotonic_ns()
            observed["response_thread_id"] = threading.get_ident()
            event_removed = True
            correlated.set()
            return gst.PadProbeReturn.REMOVE
        except BaseException as error:
            fail(f"diagnostic event probe failed: {_detail(error)}")
            return gst.PadProbeReturn.OK

    def hold_nal5(_pad: Any, info: Any) -> Any:
        try:
            buffer = info.get_buffer()
            if (
                buffer is None
                or not correlated.is_set()
                or buffer.has_flags(gst.BufferFlags.DELTA_UNIT)
            ):
                return gst.PadProbeReturn.OK
            mapped, map_info = buffer.map(gst.MapFlags.READ)
            if not mapped:
                fail("diagnostic tee access unit could not be mapped")
                return gst.PadProbeReturn.OK
            try:
                nal5 = _contains_h264_idr_bytes(bytes(map_info.data))
            finally:
                buffer.unmap(map_info)
            if not nal5:
                fail("diagnostic correlated non-delta access unit lacks NAL5")
                return gst.PadProbeReturn.OK
            observed["nal5"] = True
            observed["nal5_pts_ns"] = int(buffer.pts)
            observed["nal5_monotonic_ns"] = time.monotonic_ns()
            observed["nal5_thread_id"] = threading.get_ident()
            reached.set()
            if not release.wait(timeout_s):
                fail("diagnostic held NAL5 release timed out")
            completed.set()
            return gst.PadProbeReturn.REMOVE
        except BaseException as error:
            fail(f"diagnostic NAL5 probe failed: {_detail(error)}")
            return gst.PadProbeReturn.OK

    try:
        event_probe_id = int(
            encoder_source.add_probe(gst.PadProbeType.EVENT_DOWNSTREAM, event_probe)
        )
        video_probe_id = int(video_sink.add_probe(gst.PadProbeType.BUFFER, hold_nal5))
        if event_probe_id <= 0 or video_probe_id <= 0:
            raise HarnessError("diagnostic edge-gate probe was refused")
        event = gstvideo.video_event_new_upstream_force_key_unit(
            gst.CLOCK_TIME_NONE, True, count
        )
        if event is None:
            raise HarnessError("diagnostic force-key construction failed")
        observed["request_seqnum"] = int(event.get_seqnum())
        observed["request_monotonic_ns"] = time.monotonic_ns()
        dispatch = driver._send_force_key_synchronously(
            context, generation, encoder_source, event
        )
        driver._await_force_key_dispatch(context, generation, dispatch, deadline)
        while not reached.wait(0.01):
            if failed.is_set():
                raise HarnessError(cast(str, observed["failure"]))
            if time.monotonic() >= deadline:
                raise HarnessError("diagnostic encoder-edge response/NAL5 timed out")
        release.set()
        if not completed.wait(max(deadline - time.monotonic(), 0)):
            raise HarnessError("diagnostic held NAL5 callback did not complete")
        if failed.is_set():
            raise HarnessError(cast(str, observed["failure"]))
        observed["dispatch_caller_thread_id"] = dispatch.caller_thread_ident
        observed["dispatch_synchronous"] = dispatch.thread is None
        observed["request_to_response_ns"] = cast(
            int, observed["response_monotonic_ns"]
        ) - cast(int, observed["request_monotonic_ns"])
        observed["response_to_nal5_ns"] = cast(
            int, observed["nal5_monotonic_ns"]
        ) - cast(int, observed["response_monotonic_ns"])
        observed["passed"] = True
        return observed
    finally:
        release.set()
        cleanup_failures: list[str] = []
        if event_probe_id and not event_removed:
            try:
                driver._remove_retained_probe(encoder_source, event_probe_id, 0.2)
            except BaseException as error:
                cleanup_failures.append(f"event probe: {_detail(error)}")
        if video_probe_id:
            try:
                driver._release_block_probe(
                    video_sink,
                    video_probe_id,
                    reached=reached,
                    completed=completed,
                    release=release,
                    timeout_s=0.2,
                )
            except BaseException as error:
                cleanup_failures.append(f"video probe: {_detail(error)}")
        if dispatch is not None and (
            not dispatch.done.is_set()
            or (dispatch.thread is not None and dispatch.thread.is_alive())
        ):
            cleanup_failures.append("dispatch survived cleanup")
        if cleanup_failures:
            raise HarnessError(
                "diagnostic edge-gate cleanup failed: " + "; ".join(cleanup_failures)
            )


def _production_gate_v2(diagnostic: Diagnostic) -> dict[str, object]:
    """Expected-refuse the current gate, then prove the encoder-edge alternative."""

    context = diagnostic.driver._generation_pipelines.get(id(diagnostic.pipeline))
    if context is None:
        raise HarnessError("production generation context is absent")
    old = context.generations.get(1)
    successor = context.generations.get(2)
    if (
        old is None
        or successor is None
        or context.active_generation_id != 1
        or not old.linked
        or successor.linked
        or successor.activation_id is not None
    ):
        raise HarnessError("production gate-v2 initial routing differs")
    prior_next_slot = context.next_video_slot_id
    main_thread_id = threading.get_ident()

    def worker() -> dict[str, object]:
        worker_thread_id = threading.get_ident()
        if worker_thread_id == main_thread_id:
            raise HarnessError("gate-v2 did not run on a dedicated driver worker")
        activation = diagnostic.driver._allocate_slot_activation(context, successor)
        successor.reusable = False
        diagnostic.driver._prewarm_generation(context, successor)
        diagnostic.driver._set_generation_linked(context, successor, True)
        result: dict[str, object] = {
            "main_thread_id": main_thread_id,
            "worker_thread_id": worker_thread_id,
            "single_worker_thread": True,
            "successor_slot_id": successor.generation_id,
            "successor_activation_id": activation,
            "successor_prewarmed": True,
            "successor_linked_closed": bool(
                successor.linked and successor.video_valve.get_property("drop")
            ),
            "old_route_left_active": bool(old.linked and context.active_generation_id == 1),
        }
        expected_start = len(diagnostic.events)
        expected_flow = diagnostic.flow_snapshot()
        context.next_force_key_count = 1
        expected_started_ns = time.monotonic_ns()
        returned_gate: Any | None = None
        try:
            returned_gate = diagnostic.driver._arm_forced_idr_gate(
                context,
                successor,
                deadline=time.monotonic() + GATE_PROBE_TIMEOUT_S,
                timeout_s=GATE_PROBE_TIMEOUT_S,
            )
        except BaseException as error:
            result["current_gate_error"] = _detail(f"{type(error).__name__}: {error}")
        else:
            _release_production_gate(
                diagnostic.driver,
                returned_gate,
                timeout_s=0.2,
            )
            raise HarnessError("current parser.src production gate unexpectedly succeeded")
        result["current_gate_elapsed_ns"] = time.monotonic_ns() - expected_started_ns
        result["current_gate_events"] = diagnostic.events[expected_start:]
        result["current_gate_flow_before"] = expected_flow
        result["current_gate_flow_after"] = diagnostic.flow_snapshot()
        current_error = cast(str, result["current_gate_error"])
        if (
            "forced-IDR response/IDR wait timed out" not in current_error
            or "cleanup is incomplete" in current_error
        ):
            raise HarnessError("current gate did not produce the exact clean expected refusal")
        parser_source = diagnostic.element("parser").get_static_pad("src")
        video_sink = diagnostic.element("video_tee").get_static_pad("sink")
        if (
            parser_source is None
            or video_sink is None
            or parser_source.is_blocked()
            or parser_source.is_blocking()
            or video_sink.is_blocked()
            or video_sink.is_blocking()
        ):
            raise HarnessError("current production gate left a retained blocking probe")
        current_events = cast(list[dict[str, object]], result["current_gate_events"])
        result["current_gate_response_shape"] = _parser_blind_spot_shape(
            current_events, 1
        )
        flow_after = cast(Mapping[str, Mapping[str, int]], result["current_gate_flow_after"])
        flow_before = cast(Mapping[str, Mapping[str, int]], result["current_gate_flow_before"])
        nal5_delta = flow_after["parser_src"]["nal5"] - flow_before["parser_src"]["nal5"]
        result["current_gate_nal5_delta"] = nal5_delta
        if nal5_delta < 1:
            raise HarnessError("current parser.src gate refused despite no observed NAL5")
        elapsed = cast(int, result["current_gate_elapsed_ns"])
        if not 350_000_000 <= elapsed <= 1_000_000_000:
            raise HarnessError("current gate expected-refusal timing escaped its bound")
        dispatches = context.force_key_dispatches
        if (
            len(dispatches) != 1
            or not dispatches[0].done.is_set()
            or dispatches[0].thread is not None
            or dispatches[0].accepted is not True
            or dispatches[0].caller_thread_ident != worker_thread_id
        ):
            raise HarnessError("current gate dispatch cleanup/worker identity differs")
        result["current_gate_dispatch_cleanup"] = {
            "count": 1,
            "done": True,
            "synchronous": True,
            "caller_thread_id": dispatches[0].caller_thread_ident,
        }
        result["current_gate_expected_refusal_proven"] = True
        diagnostic_start = len(diagnostic.events)
        context.next_force_key_count = 1
        result["encoder_edge_gate"] = _diagnostic_encoder_edge_gate(
            diagnostic,
            context,
            successor,
            timeout_s=GATE_PROBE_TIMEOUT_S,
        )
        result["encoder_edge_events"] = diagnostic.events[diagnostic_start:]
        if threading.get_ident() != worker_thread_id:
            raise HarnessError("gate-v2 driver worker identity changed")
        return result

    result: dict[str, object] | None = None
    worker_error: BaseException | None = None
    try:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="dashcam-gate-v2"
        ) as executor:
            future = executor.submit(worker)
            try:
                result = future.result(timeout=GATE_WORKER_TIMEOUT_S)
            except concurrent.futures.TimeoutError as error:
                worker_error = HarnessError("gate-v2 worker exceeded its outer bound")
                raise worker_error from error
    finally:
        cleanup_failures: list[str] = []
        try:
            if successor.linked:
                diagnostic.driver._set_generation_linked(context, successor, False)
            if successor.activation_id is not None:
                diagnostic.driver._reset_unrouted_generation(
                    context,
                    successor,
                    next_video_slot_id=prior_next_slot,
                    timeout_s=1.0,
                )
        except BaseException as error:
            cleanup_failures.append(_detail(error))
        if (
            not old.linked
            or context.active_generation_id != 1
            or successor.linked
            or successor.activation_id is not None
        ):
            cleanup_failures.append("three-slot routing was not restored")
        if cleanup_failures:
            raise HarnessError("gate-v2 successor cleanup failed: " + "; ".join(cleanup_failures))
    if worker_error is not None:
        raise worker_error
    if result is None:
        raise HarnessError("gate-v2 worker returned no evidence")
    result["successor_cleanup_proven"] = True
    result["final_topology"] = diagnostic.driver.generation_snapshot(diagnostic.pipeline)
    return result


def _make_media_namespace() -> tuple[Path, str]:
    root = RECORDING_ROOT.resolve(strict=True)
    pending = PENDING_ROOT.resolve(strict=True)
    root_info, pending_info = os.lstat(root), os.lstat(pending)
    if (
        root != RECORDING_ROOT
        or pending != PENDING_ROOT
        or not stat.S_ISDIR(root_info.st_mode)
        or not stat.S_ISDIR(pending_info.st_mode)
        or root_info.st_dev != pending_info.st_dev
    ):
        raise HarnessError("pending namespace left exact recording device")
    token = f"fab{os.getpid():x}{time.monotonic_ns() & 0xFFFFFF:x}"[-16:]
    if re.fullmatch(r"[a-z0-9]{5,16}", token) is None:
        raise HarnessError("diagnostic boot token is invalid")
    pattern = PENDING_ROOT / f"boot-{token}-%06d.partial.mp4"
    return pattern, token


def _cleanup_media(token: str) -> dict[str, object]:
    pattern = re.compile(rf"boot-{re.escape(token)}-[0-9]{{6}}\.partial\.mp4")
    members: list[dict[str, object]] = []
    total = 0
    with os.scandir(PENDING_ROOT) as entries:
        for entry in entries:
            if not pattern.fullmatch(entry.name):
                continue
            info = entry.stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise HarnessError("diagnostic media member is not regular")
            total += info.st_size
            members.append({"name": entry.name, "bytes": info.st_size})
            if len(members) > MAX_MEDIA_MEMBERS or total > MAX_MEDIA_BYTES:
                raise HarnessError("diagnostic media cleanup bound exceeded")
    for member in members:
        path = PENDING_ROOT / cast(str, member["name"])
        if path.parent != PENDING_ROOT or path.is_symlink():
            raise HarnessError("diagnostic cleanup target escaped pending")
        path.unlink()
    return {"members": members, "bytes": total, "removed": len(members)}


def run_diagnostic() -> dict[str, object]:
    evidence: dict[str, object] = {
        "schema_version": 1,
        "diagnostic_revision": 2,
        "passed": False,
        "diagnostic_only": True,
        "safe_to_integrate_production": False,
        "production_isolation_invoked": False,
        "production_restoration_invoked": False,
        "physical_unplug_proven": False,
        "service_mutations": 0,
        "authorization_restored": False,
    }
    failures: list[str] = []
    before_unit: dict[str, object] | None = None
    authorized: Path | None = None
    initial_identity: AlsaIdentity | None = None
    diagnostic: Diagnostic | None = None
    token: str | None = None
    try:
        geteuid = getattr(os, "geteuid", None)
        if not callable(geteuid) or cast(Callable[[], int], geteuid)() != 0:
            raise HarnessError("diagnostic requires root for exact USB authorization")
        evidence["release"] = _release_identity()
        before_unit = _unit_state()
        evidence["unit_before"] = before_unit
        evidence["throttle_before"] = _throttle()
        if evidence["throttle_before"] != "throttled=0x0":
            raise HarnessError("Pi was throttled before diagnostic")
        config = load_config(CONFIG_PATH)
        discovery = discover_capture_device(parse_alsa_selector(config.audio.device_match))
        if discovery.status is not AudioDiscoveryStatus.MATCHED or discovery.device is None:
            raise HarnessError("configured microphone is not exactly matched")
        initial_identity = discovery.device.identity
        authorized = _usb_authorized_path(
            _udev_path(Path("/dev/snd") / f"controlC{discovery.device.card_index}"),
            initial_identity,
        )
        if read_authorized(authorized) != 1:
            raise HarnessError("exact microphone is not initially authorized")
        evidence["initial_microphone"] = {
            "identity": _identity(initial_identity),
            "endpoint": discovery.device.capture_endpoint,
            "card_index": discovery.device.card_index,
            "authorization_path": str(authorized),
        }
        plan = AudioCapturePlan.from_match(discovery.device, config.audio)
        pattern, token = _make_media_namespace()
        driver = PyGObjectGStreamerDriver.load()
        gst, gstvideo = driver._gst, driver._gstvideo  # exact modules owned by selected driver
        construction_thread = threading.get_ident()
        pipeline = driver.create_pipeline(
            build_audio_pipeline_description(plan), str(pattern), 900000, plan
        )
        diagnostic = Diagnostic(driver, gst, gstvideo, pipeline, construction_thread)
        diagnostic.install_probes()
        evidence["threads"] = diagnostic.start()
        evidence["baseline"] = diagnostic.graph_snapshot("baseline")
        healthy_start = diagnostic.counters["parser_src"].count
        healthy_deadline = time.monotonic() + HEALTHY_SETTLE_S
        while time.monotonic() < healthy_deadline:
            message = diagnostic.driver.poll_bus(diagnostic.pipeline, 0.02)
            if message.kind in {
                BusMessageKind.ERROR,
                BusMessageKind.AUDIO_ERROR,
                BusMessageKind.EOS,
            }:
                raise HarnessError("production graph failed during healthy A settle")
        if diagnostic.counters["parser_src"].count <= healthy_start:
            raise HarnessError("video did not flow during healthy A settle")
        request_a = diagnostic.force(REQUEST_A, "A_before_deauthorization")
        evidence["request_a"] = request_a
        evidence["after_a"] = diagnostic.graph_snapshot("after_A")
        if request_a["completed_with_event_and_nal5"] is not True:
            raise HarnessError("control request A did not produce event plus NAL5")
        evidence["deauthorization"] = write_authorized(authorized, 0, expected=1)
        evidence["audio_error"] = diagnostic.wait_audio_error()
        evidence["pre_b"] = diagnostic.graph_snapshot("terminal_audio_error_before_B")
        before = diagnostic.counters["parser_src"].count
        deadline = time.monotonic() + POST_ERROR_FLOW_S
        while time.monotonic() < deadline:
            message = diagnostic.driver.poll_bus(diagnostic.pipeline, 0.02)
            if message.kind in {BusMessageKind.ERROR, BusMessageKind.EOS}:
                raise HarnessError("parent pipeline failed after terminal audio error")
        if diagnostic.counters["parser_src"].count <= before:
            raise HarnessError("video did not continue after terminal audio error")
        evidence["request_b"] = diagnostic.force(REQUEST_B, "B_after_terminal_audio_error")
        evidence["after_b"] = diagnostic.graph_snapshot("after_B")
        evidence["production_gate_v2"] = _production_gate_v2(diagnostic)
        evidence["after_gate_v2"] = diagnostic.graph_snapshot("after_gate_v2")
        evidence["all_force_events"] = diagnostic.events
        evidence["foreign_force_events"] = [
            item for item in diagnostic.events if item["count"] not in {REQUEST_A, REQUEST_B}
        ]
        evidence["probe_failures"] = diagnostic.failures
        if diagnostic.failures:
            raise HarnessError("one or more pad probes failed")
        b_ok = cast(Mapping[str, object], evidence["request_b"])[
            "completed_with_event_and_nal5"
        ]
        evidence["classification"] = (
            "B_EVENT_AND_NAL5"
            if b_ok is True
            else "B_MISSING_EVENT_OR_NAL5_AFTER_EXACT_AUDIO_ERROR"
        )
    except BaseException as error:
        failures.append(_detail(f"{type(error).__name__}: {error}"))
    finally:
        if authorized is not None:
            try:
                evidence["reauthorization_finally"] = restore_authorized(authorized)
                evidence["authorization_restored"] = True
            except BaseException as error:
                failures.append(_detail(f"authorization restore failed: {error}"))
        if diagnostic is not None:
            try:
                diagnostic.stop()
                evidence["pipeline_null"] = True
            except BaseException as error:
                failures.append(_detail(f"pipeline NULL failed: {error}"))
        if token is not None:
            try:
                evidence["media_cleanup"] = _cleanup_media(token)
            except BaseException as error:
                failures.append(_detail(f"media cleanup failed: {error}"))
    try:
        after_unit = _unit_state()
        evidence["unit_after"] = after_unit
        if before_unit is None or after_unit != before_unit or after_unit["restarts"] != 0:
            raise HarnessError("dashcamd service state changed")
        evidence["throttle_after"] = _throttle()
        if evidence["throttle_after"] != "throttled=0x0":
            raise HarnessError("Pi throttled during diagnostic")
        if evidence["authorization_restored"] is not True or initial_identity is None:
            raise HarnessError("USB authorization restoration was not proven")
        deadline = time.monotonic() + 15.0
        rediscovery = None
        selector = parse_alsa_selector(load_config(CONFIG_PATH).audio.device_match)
        while time.monotonic() < deadline:
            rediscovery = discover_capture_device(selector)
            if rediscovery.status is AudioDiscoveryStatus.MATCHED:
                break
            time.sleep(0.25)
        if (
            rediscovery is None
            or rediscovery.status is not AudioDiscoveryStatus.MATCHED
            or rediscovery.device is None
            or not _same_identity(rediscovery.device.identity, initial_identity)
        ):
            raise HarnessError("restored microphone did not rematch exact stable identity")
        evidence["final_microphone"] = {
            "identity": _identity(rediscovery.device.identity),
            "endpoint": rediscovery.device.capture_endpoint,
        }
    except BaseException as error:
        failures.append(_detail(f"{type(error).__name__}: {error}"))
    evidence["failures"] = failures
    evidence["passed"] = not failures
    return evidence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-manifest-sha256", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("run-diagnostic")
    command.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    started = time.monotonic_ns()
    verify_manifest(arguments.expected_manifest_sha256)
    try:
        result = run_diagnostic()
    except BaseException as error:
        result = {
            "schema_version": 1,
            "passed": False,
            "diagnostic_only": True,
            "safe_to_integrate_production": False,
            "authorization_restored": False,
            "failures": [_detail(f"{type(error).__name__}: {error}")],
        }
    document = {
        **result,
        "started_monotonic_ns": started,
        "ended_monotonic_ns": time.monotonic_ns(),
        "manifest_sha256": arguments.expected_manifest_sha256,
    }
    _write_result(arguments.output, document)
    return 0 if document["passed"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
