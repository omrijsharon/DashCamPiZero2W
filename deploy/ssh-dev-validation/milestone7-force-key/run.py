#!/usr/bin/env python3
"""Hash-closed exact-Pi upstream-force-key capability harness."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import stat
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

import dashcam
from dashcam.config import load_config
from dashcam.diagnostics.media import CommandResult, run_fixed_argv
from dashcam.storage.preflight import run_live_storage_preflight

RECORDING_ROOT: Final = Path("/srv/dashcam")
QUARANTINE_ROOT: Final = RECORDING_ROOT / "quarantine"
CONFIG_PATH: Final = Path("/etc/dashcam/config.toml")
SYSTEMCTL: Final = "/usr/bin/systemctl"
FFPROBE: Final = "/usr/bin/ffprobe"
FFMPEG: Final = "/usr/bin/ffmpeg"
VCGENCMD: Final = "/usr/bin/vcgencmd"
MANIFEST_MEMBERS: Final = ("README.md", "run.py")
SHA256_RE: Final = re.compile(r"[0-9a-f]{64}")
RUN_NAME_RE: Final = re.compile(r"m7-forcekey-[a-z0-9]{8,32}")
MEDIA_NAME_RE: Final = re.compile(r"forcekey-([0-9]{2})[.]mp4")
MAX_MANIFEST_BYTES: Final = 4096
MAX_RESULT_BYTES: Final = 512 * 1024
MAX_COMMAND_OUTPUT_BYTES: Final = 256 * 1024
MAX_IDR_OUTPUT_BYTES: Final = 8 * 1024 * 1024
MAX_EVENTS: Final = 64
FRAME_NS: Final = 33_333_333
GOP_NS: Final = 1_000_000_000
REQUEST_OFFSETS_NS: Final = (130_000_000, 470_000_000, 790_000_000)
# This capability gate is the literal product media-time ceiling. Production
# must separately prove forced-IDR minus the last AAC access-unit end.
MAX_FORCE_LATENCY_NS: Final = 100_000_000
MAX_RUN_SECONDS: Final = 12.0
MIN_MEDIA: Final = 3
MAX_MEDIA: Final = 7


class HarnessError(RuntimeError):
    """The exact force-key capability contract could not be proved."""


def _bounded_detail(value: object, maximum: int = 512) -> str:
    text = " ".join(str(value).replace("\0", " ").splitlines())
    return "".join(char if char.isprintable() else " " for char in text)[:maximum]


def _sha256_file(path: Path, *, maximum: int) -> str:
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    digest = hashlib.sha256()
    total = 0
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise HarnessError(f"{path} is not a regular file")
        while chunk := os.read(descriptor, 1024 * 1024):
            total += len(chunk)
            if total > maximum:
                raise HarnessError(f"{path} exceeded its hash bound")
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _read_regular(path: Path, maximum: int) -> bytes:
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise HarnessError(f"{path} is not a bounded regular file")
        payload = bytearray()
        while chunk := os.read(descriptor, min(65536, maximum + 1 - len(payload))):
            payload.extend(chunk)
            if len(payload) > maximum:
                raise HarnessError(f"{path} exceeded its read bound")
        return bytes(payload)
    finally:
        os.close(descriptor)


def verify_manifest(expected_sha256: str, directory: Path | None = None) -> dict[str, str]:
    if SHA256_RE.fullmatch(expected_sha256) is None:
        raise HarnessError("expected manifest SHA-256 is not canonical")
    root = (directory or Path(__file__).resolve().parent).resolve(strict=True)
    manifest = root / "SHA256SUMS"
    if _sha256_file(manifest, maximum=MAX_MANIFEST_BYTES) != expected_sha256:
        raise HarnessError("reviewed manifest hash differs from supplied hash")
    entries: dict[str, str] = {}
    for line in _read_regular(manifest, MAX_MANIFEST_BYTES).decode("ascii").splitlines():
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
        if _sha256_file(root / name, maximum=2 * 1024 * 1024) != digest:
            raise HarnessError(f"manifest member {name} failed verification")
    return entries


def _write_atomic_exclusive_json(path: Path, value: Mapping[str, object]) -> None:
    if not path.is_absolute():
        raise HarnessError("evidence output path must be absolute")
    try:
        path.resolve(strict=False).relative_to(RECORDING_ROOT)
    except ValueError:
        pass
    else:
        raise HarnessError("evidence output must be outside /srv/dashcam")
    path.parent.mkdir(parents=True, exist_ok=True)
    parent = path.parent.resolve(strict=True)
    if path.parent != parent or path.exists() or path.is_symlink():
        raise HarnessError("evidence output must be one new direct regular file")
    encoded = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(encoded) > MAX_RESULT_BYTES:
        raise HarnessError("evidence JSON exceeds its bound")
    descriptor, temporary = tempfile.mkstemp(prefix=".m7-forcekey-", dir=parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary_path, path)
        except FileExistsError as error:
            raise HarnessError("evidence output must be a new file") from error
        try:
            directory_descriptor = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            if os.name != "nt":
                raise
            return
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
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
        raise HarnessError("interpreter and imported package are not one installed release")
    release = parts[4]
    if re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._-]{0,127}", release) is None:
        raise HarnessError("installed release identity is unsafe")
    return {"release": release, "venv": str(prefix), "package": str(package)}


def _read_unit_state() -> dict[str, object]:
    values: dict[str, str] = {}
    for field in ("ActiveState", "SubState", "MainPID", "NRestarts"):
        result = run_fixed_argv(
            (SYSTEMCTL, "show", "--no-pager", f"--property={field}", "--value", "dashcamd.service"),
            timeout_seconds=5.0,
            max_output_bytes=1024,
        )
        if result.returncode or result.timed_out or result.output_truncated:
            raise HarnessError("read-only dashcamd state query failed")
        values[field] = result.stdout.decode("ascii", "strict").strip()
    if (
        values["ActiveState"] != "inactive"
        or values["SubState"] != "dead"
        or values["MainPID"] != "0"
        or not values["NRestarts"].isdigit()
    ):
        raise HarnessError("dashcamd.service is not exactly inactive/dead with MainPID 0")
    return {
        "active_state": "inactive",
        "sub_state": "dead",
        "main_pid": 0,
        "restarts": int(values["NRestarts"]),
    }


def _read_throttle() -> str:
    result = run_fixed_argv((VCGENCMD, "get_throttled"), timeout_seconds=5.0, max_output_bytes=1024)
    if result.returncode or result.timed_out or result.output_truncated:
        raise HarnessError("throttle query failed")
    value = result.stdout.decode("ascii", "strict").strip()
    if re.fullmatch(r"throttled=0x[0-9a-fA-F]+", value) is None:
        raise HarnessError("throttle query shape differs")
    return value


def _prepare_output_directory(selected: Path) -> dict[str, object]:
    if (
        not selected.is_absolute()
        or selected.parent != QUARANTINE_ROOT
        or RUN_NAME_RE.fullmatch(selected.name) is None
    ):
        raise HarnessError("media target is not one safe quarantine child")
    root = RECORDING_ROOT.resolve(strict=True)
    root_info = os.lstat(root)
    if (
        root != RECORDING_ROOT
        or not stat.S_ISDIR(root_info.st_mode)
        or selected.exists()
        or selected.is_symlink()
    ):
        raise HarnessError("recording root or fresh media target is invalid")
    if QUARANTINE_ROOT.exists() or QUARANTINE_ROOT.is_symlink():
        quarantine = QUARANTINE_ROOT.resolve(strict=True)
        info = os.lstat(quarantine)
        if (
            quarantine != QUARANTINE_ROOT
            or not stat.S_ISDIR(info.st_mode)
            or info.st_dev != root_info.st_dev
        ):
            raise HarnessError("quarantine root left the exact recording device")
    else:
        os.mkdir(QUARANTINE_ROOT, mode=0o750)
    os.mkdir(selected, mode=0o750)
    info = os.lstat(selected)
    if not stat.S_ISDIR(info.st_mode) or info.st_dev != root_info.st_dev:
        raise HarnessError("diagnostic media target left recording device")
    return {
        "directory": str(selected),
        "recording_device": root_info.st_dev,
        "created_exclusive": True,
    }


@dataclass
class PadCounter:
    count: int = 0
    first_pts_ns: int | None = None
    last_pts_ns: int | None = None
    non_monotonic: int = 0
    large_gaps: int = 0

    def observe(self, buffer: Any) -> None:
        pts = int(buffer.pts)
        if not 0 <= pts < (1 << 63):
            return
        if self.first_pts_ns is None:
            self.first_pts_ns = pts
        if self.last_pts_ns is not None:
            delta = pts - self.last_pts_ns
            if delta <= 0:
                self.non_monotonic += 1
            elif delta > FRAME_NS * 2:
                self.large_gaps += 1
        self.last_pts_ns = pts
        self.count += 1

    def snapshot(self) -> dict[str, int | None]:
        return {
            "count": self.count,
            "first_pts_ns": self.first_pts_ns,
            "last_pts_ns": self.last_pts_ns,
            "non_monotonic": self.non_monotonic,
            "large_gaps": self.large_gaps,
        }


def _parse_force_key_event(gstvideo: Any, event: Any) -> tuple[int, bool] | None:
    if not bool(gstvideo.video_event_is_force_key_unit(event)):
        return None
    try:
        parsed = gstvideo.video_event_parse_downstream_force_key_unit(event)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, tuple) or len(parsed) != 6 or parsed[0] is not True:
        return None
    _ok, _timestamp, _stream_time, _running_time, all_headers, count = parsed
    if isinstance(count, bool) or not isinstance(count, int):
        raise HarnessError("downstream force-key event count is invalid")
    return count, bool(all_headers)


def _clock_wait_for_offset(last_pts_ns: int, offset_ns: int) -> bool:
    """True only in the small frame-sized window at a selected GOP phase."""
    phase_ns = last_pts_ns % GOP_NS
    return offset_ns <= phase_ns < offset_ns + (2 * FRAME_NS)


def _contains_h264_idr_bytes(payload: bytes) -> bool:
    """Recognize NAL type 5 in either Annex-B or 4-byte AVC access units."""

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


class Experiment:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.gst, self.gstvideo = self._load_gst()
        self.pipeline = self._build_pipeline()
        self.bus = self.pipeline.get_bus()
        if self.bus is None:
            raise HarnessError("pipeline has no bus")
        self.camera = self._element("camera")
        self.encoder = self._element("encoder")
        self.parser = self._element("parser")
        self.raw_counter, self.encoded_counter = PadCounter(), PadCounter()
        self.events: list[dict[str, object]] = []
        self.requests: dict[int, dict[str, object]] = {}
        self.natural_force_events: list[dict[str, int | bool]] = []
        self.natural_idr_count = 0
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self.clock: Any | None = None
        self.base_time_ns: int | None = None
        self.eos_seen = False
        self._add_probes()

    @staticmethod
    def _load_gst() -> tuple[Any, Any]:
        gi = importlib.import_module("gi")
        gi.require_version("Gst", "1.0")
        gi.require_version("GstVideo", "1.0")
        gst = importlib.import_module("gi.repository.Gst")
        gstvideo = importlib.import_module("gi.repository.GstVideo")
        gst.init(None)
        return gst, gstvideo

    def _build_pipeline(self) -> Any:
        location = self.directory / "forcekey-%02d.mp4"
        # send-keyframe-requests is false: all observed requests must be ours.
        description = (
            "libcamerasrc name=camera ! video/x-raw,width=(int)1920,height=(int)1080,format=(string)NV12,framerate=(fraction)30/1 ! "  # noqa: E501
            'v4l2h264enc name=encoder extra-controls="controls,repeat_sequence_header=1,video_bitrate=8000000,h264_i_frame_period=30" ! '  # noqa: E501
            "video/x-h264,profile=(string)high,level=(string)4.1 ! h264parse name=parser config-interval=-1 ! "  # noqa: E501
            "queue name=record_queue max-size-buffers=60 max-size-bytes=4000000 max-size-time=2000000000 leaky=no ! "  # noqa: E501
            "splitmuxsink name=output location="
            + str(location)
            + " max-size-time=2000000000 max-size-bytes=0 send-keyframe-requests=false async-finalize=false "  # noqa: E501
            'muxer-factory=mp4mux sink-factory=filesink muxer-properties="properties,fragment-duration=(uint)1000,fragment-mode=(int)0"'  # noqa: E501
        )
        pipeline = self.gst.parse_launch(description)
        if pipeline is None:
            raise HarnessError("GStreamer did not construct diagnostic pipeline")
        return pipeline

    def _element(self, name: str) -> Any:
        element = self.pipeline.get_by_name(name)
        if element is None:
            raise HarnessError(f"required element {name} is absent")
        return element

    def _add_probes(self) -> None:
        camera_pad = self.camera.get_static_pad("src")
        parser_pad = self.parser.get_static_pad("src")
        if camera_pad is None or parser_pad is None:
            raise HarnessError("required source pad is absent")

        def raw(_pad: Any, info: Any) -> Any:
            buffer = info.get_buffer()
            if buffer is not None:
                self.raw_counter.observe(buffer)
            return self.gst.PadProbeReturn.OK

        def encoded(_pad: Any, info: Any) -> Any:
            buffer = info.get_buffer()
            if buffer is None:
                return self.gst.PadProbeReturn.OK
            self.encoded_counter.observe(buffer)
            if not buffer.has_flags(self.gst.BufferFlags.DELTA_UNIT):
                pts = int(buffer.pts)
                now = time.monotonic_ns()
                for request in self.requests.values():
                    if (
                        request.get("idr_pts_ns") is None
                        and request.get("downstream_event_ns") is not None
                    ):
                        mapped, map_info = buffer.map(self.gst.MapFlags.READ)
                        if not mapped:
                            raise HarnessError("encoded key buffer could not be mapped")
                        try:
                            if not _contains_h264_idr_bytes(bytes(map_info.data)):
                                raise HarnessError(
                                    "first encoded key buffer after force-key event "
                                    "lacks NAL type 5"
                                )
                        finally:
                            buffer.unmap(map_info)
                        request["idr_pts_ns"] = pts
                        request["idr_monotonic_ns"] = now
                        request["request_to_idr_ns"] = now - cast(
                            int, request["request_monotonic_ns"]
                        )
                        request["request_to_idr_media_ns"] = pts - cast(
                            int, request["before_pts_ns"]
                        )
                        request["event_to_idr_ns"] = now - cast(int, request["downstream_event_ns"])
                        request["first_key_after_event_nal5"] = True
                        break
                else:
                    self.natural_idr_count += 1
            return self.gst.PadProbeReturn.OK

        def downstream(_pad: Any, info: Any) -> Any:
            event = info.get_event()
            if event is None:
                return self.gst.PadProbeReturn.OK
            parsed = _parse_force_key_event(self.gstvideo, event)
            if parsed is not None:
                count, all_headers = parsed
                request = self.requests.get(count)
                if request is None:
                    self.natural_force_events.append(
                        {
                            "count": count,
                            "all_headers": all_headers,
                            "seqnum": int(event.get_seqnum()),
                        }
                    )
                    return self.gst.PadProbeReturn.OK
                if request.get("downstream_event_ns") is not None:
                    raise HarnessError("duplicate downstream force-key event")
                downstream_seqnum = int(event.get_seqnum())
                request["downstream_seqnum"] = downstream_seqnum
                request["seqnum_preserved"] = (
                    request["request_seqnum"] == downstream_seqnum
                )
                request["downstream_event_ns"] = time.monotonic_ns()
                request["downstream_all_headers"] = all_headers
            return self.gst.PadProbeReturn.OK

        if not camera_pad.add_probe(self.gst.PadProbeType.BUFFER, raw):
            raise HarnessError("camera counter probe was refused")
        if not parser_pad.add_probe(self.gst.PadProbeType.BUFFER, encoded):
            raise HarnessError("encoded counter probe was refused")
        if not parser_pad.add_probe(self.gst.PadProbeType.EVENT_DOWNSTREAM, downstream):
            raise HarnessError("downstream event probe was refused")

    def _record(self, kind: str, **values: object) -> None:
        if len(self.events) >= MAX_EVENTS:
            raise HarnessError("event evidence exceeded its bound")
        self.events.append({"kind": kind, "monotonic_ns": time.monotonic_ns(), **values})

    def _drain_bus_once(self, timeout_ns: int = 10_000_000) -> bool:
        message = self.bus.timed_pop_filtered(timeout_ns, self.gst.MessageType.ANY)
        if message is None:
            return False
        source = message.src.get_name() if message.src is not None else "unknown"
        if message.type == self.gst.MessageType.ERROR:
            error, detail = message.parse_error()
            self.errors.append(_bounded_detail(f"{source}: {error}; {detail}"))
            raise HarnessError("pipeline posted an error")
        if message.type == self.gst.MessageType.WARNING:
            error, detail = message.parse_warning()
            self.warnings.append(_bounded_detail(f"{source}: {error}; {detail}"))
            raise HarnessError("pipeline posted a warning")
        if message.type == self.gst.MessageType.QOS:
            raise HarnessError("pipeline posted QoS")
        if message.type == self.gst.MessageType.CLOCK_LOST:
            raise HarnessError("pipeline clock was lost")
        if message.type == self.gst.MessageType.EOS:
            self.eos_seen = True
            self._record("pipeline_eos", source=source)
        return True

    def _wait_for(self, predicate: Any, seconds: float, reason: str) -> None:
        deadline = time.monotonic() + seconds
        while not bool(predicate()):
            if time.monotonic() >= deadline:
                raise HarnessError(f"bounded wait expired: {reason}")
            self._drain_bus_once()

    def _assert_identity(self) -> None:
        if (
            self.pipeline.get_by_name("camera") is not self.camera
            or self.pipeline.get_by_name("encoder") is not self.encoder
            or self.pipeline.get_by_name("parser") is not self.parser
        ):
            raise HarnessError("camera/encoder/parser object identity changed")
        if (
            self.pipeline.get_clock() != self.clock
            or int(self.pipeline.get_base_time()) != self.base_time_ns
        ):
            raise HarnessError("pipeline clock/base-time changed")
        for name, element in (("camera", self.camera), ("encoder", self.encoder)):
            returned, state, _pending = element.get_state(0)
            if returned == self.gst.StateChangeReturn.FAILURE or state != self.gst.State.PLAYING:
                raise HarnessError(f"{name} stopped PLAYING")

    def start(self) -> None:
        returned = self.pipeline.set_state(self.gst.State.PLAYING)
        if returned == self.gst.StateChangeReturn.FAILURE:
            raise HarnessError("pipeline refused PLAYING")
        waited, state, _pending = self.pipeline.get_state(20 * self.gst.SECOND)
        if waited == self.gst.StateChangeReturn.FAILURE or state != self.gst.State.PLAYING:
            raise HarnessError("pipeline did not reach PLAYING")
        self.clock, self.base_time_ns = (
            self.pipeline.get_clock(),
            int(self.pipeline.get_base_time()),
        )
        if self.clock is None or self.base_time_ns <= 0:
            raise HarnessError("pipeline clock/base-time is absent")
        self._wait_for(lambda: self.encoded_counter.count >= 45, 8.0, "initial encoded frames")
        self._assert_identity()

    def request_force_key(self, count: int, offset_ns: int) -> None:
        self._wait_for(
            lambda: (
                self.encoded_counter.last_pts_ns is not None
                and _clock_wait_for_offset(self.encoded_counter.last_pts_ns, offset_ns)
            ),
            2.0,
            f"GOP offset {offset_ns}",
        )
        source = self.encoder.get_static_pad("src")
        if source is None:
            raise HarnessError("encoder source pad is absent")
        before = cast(int, self.encoded_counter.last_pts_ns)
        request_time = time.monotonic_ns()
        self.requests[count] = {
            "count": count,
            "offset_ns": offset_ns,
            "before_pts_ns": before,
            "request_monotonic_ns": request_time,
        }
        # Official GstVideo helper. CLOCK_TIME_NONE means earliest possible upstream key unit.
        event = self.gstvideo.video_event_new_upstream_force_key_unit(
            self.gst.CLOCK_TIME_NONE, True, count
        )
        if event is None:
            raise HarnessError("GstVideo did not create upstream GstForceKeyUnit")
        self.requests[count]["request_seqnum"] = int(event.get_seqnum())
        if not bool(source.send_event(event)):
            raise HarnessError("encoder source refused upstream GstForceKeyUnit")
        self._record(
            "upstream_force_key_requested", count=count, offset_ns=offset_ns, before_pts_ns=before
        )
        self._wait_for(
            lambda: self.requests[count].get("idr_pts_ns") is not None,
            2.0,
            f"force-key {count} downstream event and IDR",
        )
        request = self.requests[count]
        if request.get("downstream_all_headers") is not True:
            raise HarnessError("force-key downstream event did not request all headers")
        media_latency_ns = cast(int, request["request_to_idr_media_ns"])
        if not 0 < media_latency_ns < MAX_FORCE_LATENCY_NS:
            raise HarnessError(
                "force-key request exceeded bounded media-time IDR latency "
                f"({media_latency_ns} ns; limit {MAX_FORCE_LATENCY_NS} ns)"
            )
        self._assert_identity()

    def stop(self) -> None:
        if not bool(self.pipeline.send_event(self.gst.Event.new_eos())):
            raise HarnessError("pipeline refused final EOS")
        self._wait_for(lambda: self.eos_seen, 20.0, "pipeline EOS")
        returned = self.pipeline.set_state(self.gst.State.NULL)
        waited, state, _pending = self.pipeline.get_state(15 * self.gst.SECOND)
        if (
            returned == self.gst.StateChangeReturn.FAILURE
            or waited == self.gst.StateChangeReturn.FAILURE
            or state != self.gst.State.NULL
        ):
            raise HarnessError("pipeline did not cleanly reach NULL")

    def run(self) -> dict[str, object]:
        try:
            self.start()
            for count, offset in enumerate(REQUEST_OFFSETS_NS, start=1):
                self.request_force_key(count, offset)
            self._wait_for(
                lambda: (
                    time.monotonic_ns() - cast(int, self.requests[3]["idr_monotonic_ns"])
                    > 2_100_000_000
                ),
                4.0,
                "post-request diagnostic fragment",
            )
            self.stop()
        finally:
            # A failing run must still relinquish camera/encoder without service mutation.
            if self.pipeline.get_state(0)[1] != self.gst.State.NULL:
                self.pipeline.set_state(self.gst.State.NULL)
                self.pipeline.get_state(15 * self.gst.SECOND)
        if self.warnings or self.errors:
            raise HarnessError("pipeline reported warnings/errors")
        if any(
            counter.non_monotonic or counter.large_gaps
            for counter in (self.raw_counter, self.encoded_counter)
        ):
            raise HarnessError("measured raw/encoded PTS continuity has gaps or regressions")
        if (
            self.raw_counter.count < self.encoded_counter.count
            or self.raw_counter.count - self.encoded_counter.count > 1
        ):
            raise HarnessError("raw/encoded frame accounting indicates a drop")
        return {
            "requests": [self.requests[number] for number in sorted(self.requests)],
            "natural_force_events": self.natural_force_events,
            "natural_idr_count": self.natural_idr_count,
            "events": self.events,
            "raw_counter": self.raw_counter.snapshot(),
            "encoded_counter": self.encoded_counter.snapshot(),
            "warnings": self.warnings,
            "errors": self.errors,
            "pipeline_cleanup": "NULL",
        }


def _checked(result: CommandResult, label: str) -> bytes:
    if result.returncode or result.timed_out or result.output_truncated or result.stderr:
        raise HarnessError(f"{label} failed: {_bounded_detail(result.stderr)}")
    return result.stdout


def _contains_h264_idr(data: str) -> bool:
    words: list[str] = []
    for line in data.splitlines():
        column = (line.split(":", 1)[1] if ":" in line else line).strip().split("  ", 1)[0]
        words.extend(
            word
            for word in column.split()
            if len(word) % 2 == 0 and re.fullmatch(r"[0-9A-Fa-f]{2,8}", word)
        )
    try:
        raw = bytes.fromhex("".join(words))
    except ValueError:
        return False
    return _contains_h264_idr_bytes(raw)


def validate_media(directory: Path) -> list[dict[str, object]]:
    files = sorted(path for path in directory.iterdir() if path.suffix == ".mp4")
    if not MIN_MEDIA <= len(files) <= MAX_MEDIA:
        raise HarnessError("diagnostic media count left its bound")
    evidence: list[dict[str, object]] = []
    for path in files:
        if (
            path.parent != directory
            or path.is_symlink()
            or not path.is_file()
            or MEDIA_NAME_RE.fullmatch(path.name) is None
        ):
            raise HarnessError("diagnostic directory contains a foreign media member")
        packet = run_fixed_argv(
            (
                FFPROBE,
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
                str(path),
            ),
            timeout_seconds=8.0,
            max_output_bytes=MAX_IDR_OUTPUT_BYTES,
        )
        document = json.loads(_checked(packet, f"IDR probe {path.name}").decode("utf-8", "strict"))
        packets = document.get("packets") if isinstance(document, Mapping) else None
        if (
            not isinstance(packets, list)
            or len(packets) != 1
            or not isinstance(packets[0], Mapping)
            or "K" not in str(packets[0].get("flags", ""))
            or not _contains_h264_idr(str(packets[0].get("data", "")))
        ):
            raise HarnessError("diagnostic MP4 is not IDR-started")
        decode = run_fixed_argv(
            (
                FFMPEG,
                "-v",
                "error",
                "-xerror",
                "-c:v",
                "h264_v4l2m2m",
                "-i",
                str(path),
                "-map",
                "0:v:0",
                "-f",
                "null",
                "-",
            ),
            timeout_seconds=20.0,
            max_output_bytes=MAX_COMMAND_OUTPUT_BYTES,
        )
        _checked(decode, f"hardware decode {path.name}")
        evidence.append(
            {
                "name": path.name,
                "bytes": path.stat().st_size,
                "idr_first": True,
                "hardware_decode": "passed",
            }
        )
    return evidence


def execute(directory: Path) -> dict[str, object]:
    release = _release_identity()
    before_unit = _read_unit_state()
    config = load_config(CONFIG_PATH)
    storage = run_live_storage_preflight(config)
    if not storage.ready:
        raise HarnessError("verified production storage is not READY")
    destination = _prepare_output_directory(directory)
    throttle_before = _read_throttle()
    experiment = Experiment(directory)
    result = experiment.run()
    media = validate_media(directory)
    after_unit = _read_unit_state()
    throttle_after = _read_throttle()
    if before_unit != after_unit:
        raise HarnessError("dashcamd service state/restart count changed")
    if throttle_before != "throttled=0x0" or throttle_after != "throttled=0x0":
        raise HarnessError("throttle flag is nonzero")
    return {
        "schema_version": 1,
        "passed": True,
        "safe_to_integrate_production": False,
        "release": release,
        "dashcamd_before": before_unit,
        "dashcamd_after": after_unit,
        "storage": {"state": storage.state.value, "write_probe_succeeded": storage.probe_succeeded},
        "destination": destination,
        "throttle_before": throttle_before,
        "throttle_after": throttle_after,
        "force_key": result,
        "media": media,
        "mutations": {
            "service_operations": 0,
            "audio_operations": 0,
            "sysfs_operations": 0,
            "production_catalog_writes": 0,
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-manifest-sha256", required=True)
    commands = parser.add_subparsers(dest="phase", required=True)
    run = commands.add_parser("run-experiment")
    run.add_argument("--output-directory", required=True, type=Path)
    run.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    verify_manifest(cast(str, arguments.expected_manifest_sha256))
    started = time.monotonic_ns()
    try:
        result = execute(cast(Path, arguments.output_directory))
    except BaseException as error:
        result = {
            "schema_version": 1,
            "passed": False,
            "safe_to_integrate_production": False,
            "error_type": type(error).__name__,
            "error": _bounded_detail(error),
        }
    result["started_monotonic_ns"] = started
    result["ended_monotonic_ns"] = time.monotonic_ns()
    _write_atomic_exclusive_json(cast(Path, arguments.output), result)
    return 0 if result["passed"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
