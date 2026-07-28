#!/usr/bin/env python3
"""Hash-closed exact-Pi immutable recording-generation capability harness."""

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
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, cast

import dashcam
from dashcam.audio.alsa import parse_alsa_selector
from dashcam.audio.linux import AudioDiscoveryStatus, discover_capture_device
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
RUN_NAME_RE: Final = re.compile(r"m7-generation-[a-z0-9]{8,32}")
MEDIA_NAME_RE: Final = re.compile(r"g(01|02|03)-([0-9]{2})[.]mp4")
MAX_MANIFEST_BYTES: Final = 4096
MAX_RESULT_BYTES: Final = 2 * 1024 * 1024
MAX_COMMAND_OUTPUT_BYTES: Final = 256 * 1024
MAX_IDR_OUTPUT_BYTES: Final = 8 * 1024 * 1024
MAX_EVENT_COUNT: Final = 256
# A coarse pre-mutation sanity bound only. Encoded-ingress running-time
# separation is not the product A/V-skew metric; every restored MP4 is still
# required below 100 ms by validate_media().
MAX_INPUT_ALIGNMENT_NS: Final = 250_000_000
FRAME_PERIOD_NS: Final = round(1_000_000_000 / 30)
SWITCH_CLOSURES: Final = 2
FINAL_CLOSURES: Final = 3
MIN_MEDIA_COUNT: Final = 8
MAX_MEDIA_COUNT: Final = 12
SEGMENT_NS: Final = 3_000_000_000


class HarnessError(RuntimeError):
    """The exact capability contract could not be proved."""


def _bounded_detail(value: object, maximum: int = 512) -> str:
    text = " ".join(str(value).replace("\0", " ").splitlines())
    return "".join(character if character.isprintable() else " " for character in text)[:maximum]


def _bounded_regular_bytes(path: Path, maximum: int) -> bytes:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
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


def _sha256_file(path: Path, *, maximum: int) -> str:
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
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise HarnessError(f"{path} is not a regular file")
        while chunk := os.read(descriptor, 1024 * 1024):
            total += len(chunk)
            if total > maximum:
                raise HarnessError(f"{path} exceeded its hash bound")
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def verify_manifest(expected_sha256: str, directory: Path | None = None) -> dict[str, str]:
    if SHA256_RE.fullmatch(expected_sha256) is None:
        raise HarnessError("expected manifest SHA-256 is not canonical")
    root = (directory or Path(__file__).resolve().parent).resolve(strict=True)
    manifest = root / "SHA256SUMS"
    if _sha256_file(manifest, maximum=MAX_MANIFEST_BYTES) != expected_sha256:
        raise HarnessError("reviewed manifest hash differs from the supplied hash")
    entries: dict[str, str] = {}
    for line in _bounded_regular_bytes(manifest, MAX_MANIFEST_BYTES).decode("ascii").splitlines():
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
    descriptor, temporary = tempfile.mkstemp(prefix=".m7-generation-", dir=parent)
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
    package_path = Path(dashcam.__file__).resolve(strict=True)
    parts = prefix.as_posix().split("/")
    if (
        len(parts) < 6
        or parts[:4] != ["", "opt", "dashcam", "releases"]
        or parts[-1] != "venv"
        or not package_path.is_relative_to(prefix)
    ):
        raise HarnessError("interpreter and imported dashcam package are not one release")
    release = parts[4]
    if re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._-]{0,127}", release) is None:
        raise HarnessError("installed release identity is unsafe")
    return {"release": release, "venv": str(prefix), "package": str(package_path)}


def _read_unit_state() -> dict[str, object]:
    observed: dict[str, str] = {}
    for property_name in ("ActiveState", "SubState", "MainPID", "NRestarts"):
        result = run_fixed_argv(
            (
                SYSTEMCTL,
                "show",
                "--no-pager",
                f"--property={property_name}",
                "--value",
                "dashcamd.service",
            ),
            timeout_seconds=5.0,
            max_output_bytes=1024,
        )
        if result.returncode != 0 or result.timed_out or result.output_truncated:
            raise HarnessError("read-only dashcamd state query failed")
        try:
            observed[property_name] = result.stdout.decode("ascii").strip()
        except UnicodeDecodeError as error:
            raise HarnessError("dashcamd state query was not ASCII") from error
    if observed["ActiveState"] != "inactive" or observed["SubState"] != "dead":
        raise HarnessError("dashcamd.service is not exactly inactive/dead")
    if observed["MainPID"] != "0" or not observed["NRestarts"].isdigit():
        raise HarnessError("dashcamd numeric state is invalid")
    return {
        "active_state": observed["ActiveState"],
        "sub_state": observed["SubState"],
        "main_pid": 0,
        "restarts": int(observed["NRestarts"]),
    }


def _read_throttle() -> str:
    result = run_fixed_argv(
        (VCGENCMD, "get_throttled"),
        timeout_seconds=5.0,
        max_output_bytes=1024,
    )
    if result.returncode != 0 or result.timed_out or result.output_truncated:
        raise HarnessError("throttle query failed")
    try:
        value = result.stdout.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise HarnessError("throttle query was not ASCII") from error
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
    if root != RECORDING_ROOT or not stat.S_ISDIR(root_info.st_mode):
        raise HarnessError("recording root identity differs")
    if selected.exists() or selected.is_symlink():
        raise HarnessError("media target already exists")
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
        raise HarnessError("created media target left the exact recording device")
    return {
        "recording_device": root_info.st_dev,
        "directory": str(selected),
        "created_exclusive": True,
    }


@dataclass
class PadCounter:
    count: int = 0
    first_pts_ns: int | None = None
    last_pts_ns: int | None = None
    non_monotonic: int = 0
    large_gaps: int = 0
    first_delta: bool | None = None

    def observe(self, buffer: Any) -> None:
        pts = int(buffer.pts)
        if pts < 0:
            return
        if self.first_pts_ns is None:
            self.first_pts_ns = pts
            self.first_delta = bool(int(buffer.get_flags()) & (1 << 13))
        if self.last_pts_ns is not None:
            delta = pts - self.last_pts_ns
            if delta <= 0:
                self.non_monotonic += 1
            elif delta > FRAME_PERIOD_NS * 2:
                self.large_gaps += 1
        self.last_pts_ns = pts
        self.count += 1

    def snapshot(self) -> dict[str, object]:
        return {
            "count": self.count,
            "first_pts_ns": self.first_pts_ns,
            "last_pts_ns": self.last_pts_ns,
            "non_monotonic": self.non_monotonic,
            "large_gaps": self.large_gaps,
            "first_delta": self.first_delta,
        }


@dataclass(frozen=True)
class HandoffInputs:
    video_sink: Any
    video_probe: int
    audio_sink: Any
    audio_probe: int
    video_pts_ns: int
    audio_pts_ns: int
    video_running_time_ns: int
    audio_running_time_ns: int
    video_blocked_monotonic_ns: int
    audio_blocked_monotonic_ns: int


@dataclass
class Generation:
    number: int
    audio: bool
    bin: Any
    output: Any
    video_valve: Any
    audio_valve: Any | None
    video_queue: Any
    audio_queue: Any | None
    video_ghost: Any
    audio_ghost: Any | None
    video_tee_pad: Any
    audio_tee_pad: Any | None
    output_video_pad: Any
    output_audio_pad: Any | None
    video_counter: PadCounter = field(default_factory=PadCounter)
    audio_counter: PadCounter = field(default_factory=PadCounter)
    opened_locations: list[str] = field(default_factory=list)
    closed_locations: list[str] = field(default_factory=list)
    retired: bool = False
    video_eos_seen: bool = False
    audio_eos_seen: bool = False
    video_events: set[str] = field(default_factory=set)
    audio_events: set[str] = field(default_factory=set)
    video_first_buffer_had_sticky_contract: bool = False
    audio_first_buffer_had_sticky_contract: bool = False
    ingress_event_error: str | None = None
    external_linked: bool = False


class Experiment:
    """One bounded immutable-generation experiment on the exact target."""

    def __init__(self, output_directory: Path, endpoint: str) -> None:
        self.output_directory = output_directory
        self.endpoint = endpoint
        self.events: list[dict[str, object]] = []
        self.transitions: list[dict[str, object]] = []
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self.gst = self._load_gst()
        self.pipeline = self._build_parent()
        self.bus = self.pipeline.get_bus()
        if self.bus is None:
            raise HarnessError("pipeline has no bus")
        self.video_tee = self._element("video_tee")
        self.audio_tee = self._element("audio_tee")
        self.camera = self._element("camera")
        self.encoder = self._element("encoder")
        self.parser = self._element("parser")
        self.video_source_counter = PadCounter()
        self.audio_source_counter = PadCounter()
        self._add_counter_probe("video_counter", self.video_source_counter)
        self._add_counter_probe("audio_counter", self.audio_source_counter)
        self.generations: dict[int, Generation] = {}
        self.clock: Any | None = None
        self.base_time_ns: int | None = None
        self.initial_new_clock_seen = False

    @staticmethod
    def _load_gst() -> Any:
        gi = importlib.import_module("gi")
        gi.require_version("Gst", "1.0")
        gst = importlib.import_module("gi.repository.Gst")
        gst.init(None)
        return gst

    def _build_parent(self) -> Any:
        if re.fullmatch(r"hw:[0-9]{1,3},[0-9]{1,3},[0-9]{1,3}", self.endpoint) is None:
            raise HarnessError("discovered ALSA endpoint is not exact")
        description = (
            "libcamerasrc name=camera ! "
            "video/x-raw,width=(int)1920,height=(int)1080,format=(string)NV12,"
            "framerate=(fraction)30/1 ! "
            "v4l2h264enc name=encoder "
            'extra-controls="controls,repeat_sequence_header=1,video_bitrate=8000000,'
            'h264_i_frame_period=30" ! '
            "video/x-h264,profile=(string)high,level=(string)4.1 ! "
            "h264parse name=parser config-interval=-1 ! "
            "queue name=record_queue max-size-buffers=60 max-size-bytes=4000000 "
            "max-size-time=2000000000 leaky=no ! "
            "identity name=video_counter silent=true ! "
            "tee name=video_tee allow-not-linked=true "
            f"alsasrc name=audio_source device={self.endpoint} provide-clock=false "
            "slave-method=resample use-driver-timestamps=false do-timestamp=true ! "
            "queue max-size-buffers=96 max-size-bytes=2097152 "
            "max-size-time=2000000000 leaky=no ! "
            "audio/x-raw,format=(string)S16LE,rate=(int)48000,channels=(int)1 ! "
            "audioconvert ! audioresample ! "
            "audio/x-raw,format=(string)S16LE,rate=(int)48000,channels=(int)1 ! "
            "voaacenc bitrate=128000 ! aacparse ! "
            "queue name=audio_record_queue max-size-buffers=96 "
            "max-size-bytes=2097152 max-size-time=2000000000 leaky=no ! "
            "identity name=audio_counter silent=true ! "
            "tee name=audio_tee allow-not-linked=true"
        )
        pipeline = self.gst.parse_launch(description)
        if pipeline is None:
            raise HarnessError("GStreamer did not construct the parent pipeline")
        return pipeline

    def _element(self, name: str) -> Any:
        element = self.pipeline.get_by_name(name)
        if element is None:
            raise HarnessError(f"required element {name} is absent")
        return element

    def _record_event(self, kind: str, **fields: object) -> None:
        if len(self.events) >= MAX_EVENT_COUNT:
            raise HarnessError("event evidence exceeded its bound")
        self.events.append({"kind": kind, "monotonic_ns": time.monotonic_ns(), **fields})

    def _add_counter_probe(self, element_name: str, counter: PadCounter) -> None:
        element = self._element(element_name)
        pad = element.get_static_pad("src")
        if pad is None:
            raise HarnessError(f"{element_name} source pad is absent")

        def observe(_pad: Any, info: Any, _counter: PadCounter = counter) -> Any:
            buffer = info.get_buffer()
            if buffer is not None:
                _counter.observe(buffer)
            return self.gst.PadProbeReturn.OK

        probe_id = pad.add_probe(self.gst.PadProbeType.BUFFER, observe)
        if not probe_id:
            raise HarnessError(f"{element_name} counter probe was refused")

    def _generation_description(self, number: int, audio: bool) -> str:
        prefix = f"g{number:02d}"
        location = self.output_directory / f"{prefix}-%02d.mp4"
        splitmux = (
            f"splitmuxsink name={prefix}_output location={location} "
            f"max-size-time={SEGMENT_NS} max-size-bytes=0 "
            "send-keyframe-requests=true async-finalize=true "
            "muxer-factory=mp4mux sink-factory=filesink "
            'muxer-properties="properties,fragment-duration=(uint)1000,'
            'fragment-mode=(int)0"'
        )
        video = (
            f"valve name={prefix}_video_valve drop=true "
            "drop-mode=forward-sticky-events ! "
            f"queue name={prefix}_video_queue max-size-buffers=60 "
            "max-size-bytes=4000000 max-size-time=2000000000 leaky=no ! "
            f"{prefix}_output.video "
        )
        if not audio:
            return video + splitmux
        audio_branch = (
            f" valve name={prefix}_audio_valve drop=true "
            "drop-mode=forward-sticky-events ! "
            f"queue name={prefix}_audio_queue max-size-buffers=96 "
            "max-size-bytes=2097152 max-size-time=2000000000 leaky=no ! "
            f"{prefix}_output.audio_0"
        )
        return video + splitmux + audio_branch

    def create_generation(self, number: int, audio: bool) -> Generation:
        if number in self.generations or number not in (1, 2, 3):
            raise HarnessError("fixed three-generation ownership was violated")
        prefix = f"g{number:02d}"
        generation_bin = self.gst.parse_bin_from_description(
            self._generation_description(number, audio), False
        )
        if generation_bin is None:
            raise HarnessError("generation bin construction failed")
        output = generation_bin.get_by_name(f"{prefix}_output")
        video_queue = generation_bin.get_by_name(f"{prefix}_video_queue")
        video_valve = generation_bin.get_by_name(f"{prefix}_video_valve")
        audio_queue = generation_bin.get_by_name(f"{prefix}_audio_queue") if audio else None
        audio_valve = generation_bin.get_by_name(f"{prefix}_audio_valve") if audio else None
        if output is None or video_queue is None or video_valve is None:
            raise HarnessError("generation video topology is incomplete")
        if audio and (audio_queue is None or audio_valve is None):
            raise HarnessError("generation audio topology is incomplete")
        video_sink = video_valve.get_static_pad("sink")
        if video_sink is None:
            raise HarnessError("generation video sink is absent")
        video_ghost = self.gst.GhostPad.new("video_sink", video_sink)
        if video_ghost is None or not generation_bin.add_pad(video_ghost):
            raise HarnessError("generation video ghost pad creation failed")
        audio_ghost = None
        if audio:
            audio_sink = cast(Any, audio_valve).get_static_pad("sink")
            if audio_sink is None:
                raise HarnessError("generation audio sink is absent")
            audio_ghost = self.gst.GhostPad.new("audio_sink", audio_sink)
            if audio_ghost is None or not generation_bin.add_pad(audio_ghost):
                raise HarnessError("generation audio ghost pad creation failed")
        output_video_pad = output.get_static_pad("video")
        output_audio_pad = output.get_static_pad("audio_0") if audio else None
        if output_video_pad is None or (audio and output_audio_pad is None):
            raise HarnessError("all splitmux request pads were not created before data")
        generation_bin.set_name(f"{prefix}_generation")
        if not generation_bin.set_locked_state(True):
            raise HarnessError("standby generation could not lock NULL state")
        self.pipeline.add(generation_bin)
        if generation_bin.get_parent() is not self.pipeline:
            raise HarnessError("generation bin could not be added")
        video_tee_pad = self.video_tee.request_pad_simple("src_%u")
        if video_tee_pad is None:
            raise HarnessError("video tee request pad failed")
        audio_tee_pad = None
        if audio:
            audio_tee_pad = self.audio_tee.request_pad_simple("src_%u")
            if audio_tee_pad is None:
                raise HarnessError("audio tee request pad failed")
        generation = Generation(
            number,
            audio,
            generation_bin,
            output,
            video_valve,
            audio_valve,
            video_queue,
            audio_queue,
            video_ghost,
            audio_ghost,
            video_tee_pad,
            audio_tee_pad,
            output_video_pad,
            output_audio_pad,
        )
        video_admitted = video_queue.get_static_pad("src")
        if video_admitted is None:
            raise HarnessError("generation video admitted-data pad is absent")
        self._add_generation_probe(generation, video_admitted, generation.video_counter)
        self._add_eos_probe(generation, output_video_pad, audio=False)
        if audio and audio_queue is not None:
            audio_admitted = audio_queue.get_static_pad("src")
            if audio_admitted is None:
                raise HarnessError("generation audio admitted-data pad is absent")
            self._add_generation_probe(generation, audio_admitted, generation.audio_counter)
            self._add_eos_probe(generation, cast(Any, output_audio_pad), audio=True)
        self.generations[number] = generation
        self._record_event(
            "generation_created",
            generation=number,
            audio=audio,
            all_splitmux_pads_created=True,
            pipeline_state="pre-data",
            external_tee_pads="requested_unlinked",
        )
        return generation

    def _link_external(self, generation: Generation) -> None:
        if generation.external_linked:
            raise HarnessError("generation external tee pads were already linked")
        if generation.video_tee_pad.link(generation.video_ghost) != self.gst.PadLinkReturn.OK:
            raise HarnessError("video tee could not link to generation")
        if (
            generation.audio_tee_pad is not None
            and generation.audio_ghost is not None
            and generation.audio_tee_pad.link(generation.audio_ghost) != self.gst.PadLinkReturn.OK
        ):
            generation.video_tee_pad.unlink(generation.video_ghost)
            raise HarnessError("audio tee could not link to generation")
        generation.external_linked = True

    def _unlink_external(self, generation: Generation) -> None:
        if not generation.external_linked:
            raise HarnessError("generation external tee pads were not linked")
        if not generation.video_tee_pad.unlink(generation.video_ghost):
            raise HarnessError("video tee external unlink failed")
        if (
            generation.audio_tee_pad is not None
            and generation.audio_ghost is not None
            and not generation.audio_tee_pad.unlink(generation.audio_ghost)
        ):
            raise HarnessError("audio tee external unlink failed")
        generation.external_linked = False

    def _add_eos_probe(self, generation: Generation, pad: Any, *, audio: bool) -> None:
        def observe_event(_pad: Any, info: Any) -> Any:
            events = generation.audio_events if audio else generation.video_events
            event = info.get_event()
            if event is not None:
                if event.type == self.gst.EventType.STREAM_START:
                    events.add("stream_start")
                elif event.type == self.gst.EventType.CAPS:
                    caps = event.parse_caps()
                    text = caps.to_string() if caps is not None else ""
                    required = (
                        ("audio/mpeg", "mpegversion=(int)4", "stream-format=(string)raw")
                        if audio
                        else (
                            "video/x-h264",
                            "profile=(string)high",
                            "level=(string)4.1",
                        )
                    )
                    if not all(token in text for token in required):
                        generation.ingress_event_error = "generation splitmux input CAPS differ"
                        return self.gst.PadProbeReturn.DROP
                    events.add("caps")
                elif event.type == self.gst.EventType.SEGMENT:
                    segment = event.parse_segment()
                    if segment is None or segment.format != self.gst.Format.TIME:
                        generation.ingress_event_error = (
                            "generation splitmux input segment is not TIME"
                        )
                        return self.gst.PadProbeReturn.DROP
                    events.add("time_segment")
                elif event.type == self.gst.EventType.EOS:
                    if audio:
                        generation.audio_eos_seen = True
                    else:
                        generation.video_eos_seen = True
            return self.gst.PadProbeReturn.OK

        def observe_buffer(_pad: Any, info: Any) -> Any:
            events = generation.audio_events if audio else generation.video_events
            buffer = info.get_buffer()
            if buffer is not None:
                ready = events == {"stream_start", "caps", "time_segment"}
                if not ready:
                    generation.ingress_event_error = (
                        "generation admitted data before its sticky-event contract"
                    )
                    return self.gst.PadProbeReturn.DROP
                if audio:
                    generation.audio_first_buffer_had_sticky_contract = True
                else:
                    generation.video_first_buffer_had_sticky_contract = True
            return self.gst.PadProbeReturn.OK

        event_probe_id = pad.add_probe(
            self.gst.PadProbeType.EVENT_DOWNSTREAM,
            observe_event,
        )
        if not event_probe_id:
            raise HarnessError("generation event observation probe was refused")
        buffer_probe_id = pad.add_probe(self.gst.PadProbeType.BUFFER, observe_buffer)
        if not buffer_probe_id:
            pad.remove_probe(event_probe_id)
            raise HarnessError("generation buffer observation probe was refused")

    def _add_generation_probe(self, generation: Generation, pad: Any, counter: PadCounter) -> None:
        def observe(
            _pad: Any,
            info: Any,
            _counter: PadCounter = counter,
            _number: int = generation.number,
        ) -> Any:
            buffer = info.get_buffer()
            if buffer is not None:
                _counter.observe(buffer)
            return self.gst.PadProbeReturn.OK

        probe_id = pad.add_probe(self.gst.PadProbeType.BUFFER, observe)
        if not probe_id:
            raise HarnessError("generation counter probe was refused")

    def _set_generation_open(self, generation: Generation, opened: bool) -> None:
        generation.video_valve.set_property("drop", not opened)
        if generation.audio_valve is not None:
            generation.audio_valve.set_property("drop", not opened)
        if bool(generation.video_valve.get_property("drop")) == opened:
            raise HarnessError("generation video gate did not reach requested state")
        if (
            generation.audio_valve is not None
            and bool(generation.audio_valve.get_property("drop")) == opened
        ):
            raise HarnessError("generation audio gate did not reach requested state")
        self._record_event("generation_gate", generation=generation.number, opened=opened)

    def _drain_bus_once(self, timeout_ns: int = 0) -> bool:
        types = (
            self.gst.MessageType.ERROR
            | self.gst.MessageType.WARNING
            | self.gst.MessageType.EOS
            | self.gst.MessageType.ELEMENT
            | self.gst.MessageType.LATENCY
            | self.gst.MessageType.NEW_CLOCK
            | self.gst.MessageType.CLOCK_LOST
            | self.gst.MessageType.QOS
        )
        message = self.bus.timed_pop_filtered(timeout_ns, types)
        if message is None:
            return False
        source = message.src.get_name() if message.src is not None else "unknown"
        if message.type == self.gst.MessageType.ERROR:
            error, debug = message.parse_error()
            detail = f"{source}: {_bounded_detail(error)}; {_bounded_detail(debug)}"
            self.errors.append(detail)
            self._record_event("bus_error", source=source, detail=detail)
            raise HarnessError(f"GStreamer error: {detail}")
        if message.type == self.gst.MessageType.WARNING:
            warning, debug = message.parse_warning()
            detail = f"{source}: {_bounded_detail(warning)}; {_bounded_detail(debug)}"
            self.warnings.append(detail)
            self._record_event("bus_warning", source=source, detail=detail)
            raise HarnessError(f"GStreamer warning: {detail}")
        if message.type == self.gst.MessageType.LATENCY:
            if not self.pipeline.recalculate_latency():
                raise HarnessError("bounded latency recalculation failed")
            self._record_event("latency_recalculated", source=source)
            return True
        if message.type == self.gst.MessageType.CLOCK_LOST:
            raise HarnessError("pipeline clock was lost")
        if message.type == self.gst.MessageType.QOS:
            raise HarnessError(f"unexpected QoS message from {source}")
        if message.type == self.gst.MessageType.NEW_CLOCK:
            announced = message.parse_new_clock()
            if (
                self.initial_new_clock_seen
                or announced is None
                or (self.clock is not None and announced != self.clock)
            ):
                raise HarnessError("pipeline announced a post-start/foreign clock")
            self.initial_new_clock_seen = True
            self._record_event("initial_new_clock", source=source)
            return True
        if message.type == self.gst.MessageType.EOS:
            self._record_event("pipeline_eos", source=source)
            return True
        structure = message.get_structure()
        if structure is None:
            return True
        name = structure.get_name()
        if name not in (
            "splitmuxsink-fragment-opened",
            "splitmuxsink-fragment-closed",
        ):
            return True
        location = structure.get_string("location")
        if location is None:
            raise HarnessError("splitmux message omitted location")
        media = Path(location)
        if media.parent != self.output_directory or MEDIA_NAME_RE.fullmatch(media.name) is None:
            raise HarnessError("splitmux reported a foreign media location")
        match = cast(re.Match[str], MEDIA_NAME_RE.fullmatch(media.name))
        generation = self.generations[int(match.group(1))]
        if source != generation.output.get_name():
            raise HarnessError("splitmux message source differs from its generation")
        self._record_event(name, generation=generation.number, location=media.name)
        if name == "splitmuxsink-fragment-opened":
            if media.name in generation.opened_locations:
                raise HarnessError("duplicate fragment open was reported")
            generation.opened_locations.append(media.name)
        else:
            if media.name not in generation.opened_locations:
                raise HarnessError("fragment closure has no matching open")
            if media.name in generation.closed_locations:
                raise HarnessError("duplicate fragment closure was reported")
            generation.closed_locations.append(media.name)
        return True

    def _drain_safety_bus_quiet(
        self,
        *,
        quiet_seconds: float = 0.1,
        timeout_seconds: float = 0.75,
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        quiet_deadline = time.monotonic() + quiet_seconds
        while True:
            now = time.monotonic()
            if now >= deadline:
                raise HarnessError("bounded safety-bus drain did not become quiet")
            wait_ns = int(
                max(
                    0.0,
                    min(0.05, quiet_deadline - now, deadline - now),
                )
                * self.gst.SECOND
            )
            if self._drain_bus_once(wait_ns):
                quiet_deadline = time.monotonic() + quiet_seconds
                continue
            if time.monotonic() >= quiet_deadline:
                return

    def _wait_for(self, predicate: Any, *, timeout_seconds: float, reason: str) -> None:
        deadline = time.monotonic() + timeout_seconds
        while not bool(predicate()):
            if time.monotonic() >= deadline:
                raise HarnessError(f"bounded wait expired: {reason}")
            self._drain_bus_once(100_000_000)

    def start(self, initial: Generation) -> None:
        self._link_external(initial)
        self._set_generation_open(initial, True)
        if not initial.bin.set_locked_state(False):
            raise HarnessError("initial generation could not unlock")
        result = self.pipeline.set_state(self.gst.State.PLAYING)
        if result == self.gst.StateChangeReturn.FAILURE:
            raise HarnessError("parent pipeline refused PLAYING")
        state_return, state, _pending = self.pipeline.get_state(20 * self.gst.SECOND)
        if state_return == self.gst.StateChangeReturn.FAILURE or state != self.gst.State.PLAYING:
            raise HarnessError("parent pipeline did not reach PLAYING")
        self.clock = self.pipeline.get_clock()
        self.base_time_ns = int(self.pipeline.get_base_time())
        if self.clock is None or self.base_time_ns <= 0:
            raise HarnessError("pipeline clock/base-time contract is absent")
        self._record_event(
            "pipeline_started",
            set_state_return=int(result),
            get_state_return=int(state_return),
            base_time_ns=self.base_time_ns,
            clock_name=self.clock.get_name(),
        )

    def _assert_parent_identity(self) -> None:
        if (
            self.pipeline.get_by_name("camera") is not self.camera
            or self.pipeline.get_by_name("encoder") is not self.encoder
            or self.pipeline.get_by_name("parser") is not self.parser
        ):
            raise HarnessError("camera/encoder/parser object identity changed")
        if self.pipeline.get_clock() != self.clock:
            raise HarnessError("pipeline clock identity changed")
        if int(self.pipeline.get_base_time()) != self.base_time_ns:
            raise HarnessError("pipeline base-time changed")
        for name, element in (("camera", self.camera), ("encoder", self.encoder)):
            state_return, state, _pending = element.get_state(0)
            if (
                state_return == self.gst.StateChangeReturn.FAILURE
                or state != self.gst.State.PLAYING
            ):
                raise HarnessError(f"{name} is not continuously PLAYING")

    def _block_handoff_inputs(self) -> HandoffInputs:
        video_sink = self.video_tee.get_static_pad("sink")
        audio_sink = self.audio_tee.get_static_pad("sink")
        if video_sink is None or audio_sink is None:
            raise HarnessError("common tee input pad is absent")
        video_segment_event = video_sink.get_sticky_event(self.gst.EventType.SEGMENT, 0)
        audio_segment_event = audio_sink.get_sticky_event(self.gst.EventType.SEGMENT, 0)
        if video_segment_event is None or audio_segment_event is None:
            raise HarnessError("common input has no sticky segment")
        video_segment = video_segment_event.parse_segment()
        audio_segment = audio_segment_event.parse_segment()
        if (
            video_segment is None
            or audio_segment is None
            or video_segment.format != self.gst.Format.TIME
            or audio_segment.format != self.gst.Format.TIME
        ):
            raise HarnessError("common input sticky segment is not TIME")
        video_reached = threading.Event()
        audio_reached = threading.Event()
        target_ready = threading.Event()
        held: dict[str, int] = {}
        probe_errors: list[str] = []

        def running_time_ns(segment: Any, pts_ns: int) -> int:
            running_ns = int(segment.to_running_time(self.gst.Format.TIME, pts_ns))
            if not 0 <= running_ns < (1 << 63):
                raise HarnessError("handoff input running time is invalid")
            return running_ns

        def block_audio(_pad: Any, info: Any) -> Any:
            buffer = info.get_buffer()
            if buffer is None:
                return self.gst.PadProbeReturn.PASS
            pts_ns = int(buffer.pts)
            if not target_ready.is_set() or not 0 <= pts_ns < (1 << 63):
                return self.gst.PadProbeReturn.PASS
            try:
                audio_running_ns = running_time_ns(audio_segment, pts_ns)
            except BaseException as error:
                probe_errors.append(_bounded_detail(error))
                audio_reached.set()
                return self.gst.PadProbeReturn.OK
            if audio_running_ns < held["video_running_time_ns"]:
                return self.gst.PadProbeReturn.PASS
            held["audio_pts_ns"] = pts_ns
            held["audio_running_time_ns"] = audio_running_ns
            held["audio_blocked_monotonic_ns"] = time.monotonic_ns()
            audio_reached.set()
            return self.gst.PadProbeReturn.OK

        def block_video(_pad: Any, info: Any) -> Any:
            buffer = info.get_buffer()
            if buffer is None or buffer.has_flags(self.gst.BufferFlags.DELTA_UNIT):
                return self.gst.PadProbeReturn.PASS
            pts_ns = int(buffer.pts)
            if not 0 <= pts_ns < (1 << 63):
                return self.gst.PadProbeReturn.PASS
            try:
                video_running_ns = running_time_ns(video_segment, pts_ns)
            except BaseException as error:
                probe_errors.append(_bounded_detail(error))
                video_reached.set()
                return self.gst.PadProbeReturn.OK
            held["video_pts_ns"] = pts_ns
            held["video_running_time_ns"] = video_running_ns
            held["video_blocked_monotonic_ns"] = time.monotonic_ns()
            target_ready.set()
            video_reached.set()
            return self.gst.PadProbeReturn.OK

        # Install the audio selector first, but pass each audio buffer until
        # the video callback publishes the exact IDR PTS. The first audio
        # buffer at/after that target is then held, avoiding the sequential
        # probe-install delay observed on the exact Pi.
        audio_probe = audio_sink.add_probe(
            self.gst.PadProbeType.BLOCK | self.gst.PadProbeType.BUFFER,
            block_audio,
        )
        if not audio_probe:
            raise HarnessError("audio handoff block probe was refused")
        video_probe = video_sink.add_probe(
            self.gst.PadProbeType.BLOCK | self.gst.PadProbeType.BUFFER,
            block_video,
        )
        if not video_probe:
            audio_sink.remove_probe(audio_probe)
            raise HarnessError("video IDR block probe was refused")
        try:
            self._wait_for(
                video_reached.is_set,
                timeout_seconds=3.0,
                reason="encoded IDR block",
            )
            if probe_errors:
                raise HarnessError(f"video handoff probe failed: {probe_errors[0]}")
            self._wait_for(
                audio_reached.is_set,
                timeout_seconds=2.0,
                reason="audio block aligned to encoded IDR",
            )
            if probe_errors:
                raise HarnessError(f"audio handoff probe failed: {probe_errors[0]}")
        except BaseException:
            video_sink.remove_probe(video_probe)
            audio_sink.remove_probe(audio_probe)
            raise
        return HandoffInputs(
            video_sink=video_sink,
            video_probe=video_probe,
            audio_sink=audio_sink,
            audio_probe=audio_probe,
            video_pts_ns=held["video_pts_ns"],
            audio_pts_ns=held["audio_pts_ns"],
            video_running_time_ns=held["video_running_time_ns"],
            audio_running_time_ns=held["audio_running_time_ns"],
            video_blocked_monotonic_ns=held["video_blocked_monotonic_ns"],
            audio_blocked_monotonic_ns=held["audio_blocked_monotonic_ns"],
        )

    def switch(self, old: Generation, successor: Generation) -> None:
        if old.retired or successor.retired:
            raise HarnessError("retired generation cannot participate in handoff")
        if (
            old.ingress_event_error is not None
            or not old.video_first_buffer_had_sticky_contract
            or (old.audio and not old.audio_first_buffer_had_sticky_contract)
        ):
            raise HarnessError("active generation sticky-event/data contract failed")
        self._assert_parent_identity()
        handoff = self._block_handoff_inputs()
        input_alignment_ns = (
            handoff.audio_running_time_ns - handoff.video_running_time_ns
        )
        try:
            try:
                if not 0 <= input_alignment_ns < MAX_INPUT_ALIGNMENT_NS:
                    raise HarnessError(
                        "blocked inputs exceed the running-time alignment bound: "
                        f"{input_alignment_ns}"
                    )
                # Fragment-open/close messages can race the two input blocks.
                # Resolve the retiring fragment only after the filtered bus is
                # quiet while both streams are held.
                self._drain_safety_bus_quiet()
                active_locations = [
                    location
                    for location in old.opened_locations
                    if location not in old.closed_locations
                ]
                if len(active_locations) != 1:
                    raise HarnessError("retired generation has no unique active fragment")
                active_location = active_locations[0]
                started_ns = time.monotonic_ns()
                if not successor.bin.set_locked_state(False):
                    raise HarnessError("successor generation could not unlock")
                self._link_external(successor)
                successor_sync_return = bool(successor.bin.sync_state_with_parent())
                if not successor_sync_return:
                    raise HarnessError("successor generation could not follow parent state")
                self._set_generation_open(old, False)
                self._unlink_external(old)
                self._set_generation_open(successor, True)
                video_sink_pad = old.video_queue.get_static_pad("sink")
                if video_sink_pad is None or not video_sink_pad.send_event(
                    self.gst.Event.new_eos()
                ):
                    raise HarnessError("retired video branch refused downstream EOS")
                audio_eos = None
                if old.audio_queue is not None:
                    audio_sink_pad = old.audio_queue.get_static_pad("sink")
                    if audio_sink_pad is None:
                        raise HarnessError("retired audio branch source is absent")
                    audio_eos = bool(audio_sink_pad.send_event(self.gst.Event.new_eos()))
                    if not audio_eos:
                        raise HarnessError("retired audio branch refused downstream EOS")
            finally:
                handoff.audio_sink.remove_probe(handoff.audio_probe)
        finally:
            handoff.video_sink.remove_probe(handoff.video_probe)
        probes_removed_monotonic_ns = time.monotonic_ns()
        blocked_duration_ns = (
            probes_removed_monotonic_ns - handoff.video_blocked_monotonic_ns
        )
        audio_blocked_duration_ns = (
            probes_removed_monotonic_ns - handoff.audio_blocked_monotonic_ns
        )
        self._wait_for(
            lambda: (
                active_location in old.closed_locations
                and set(old.opened_locations) == set(old.closed_locations)
                and old.video_eos_seen
                and (not old.audio or old.audio_eos_seen)
            ),
            timeout_seconds=15.0,
            reason=f"generation {old.number} closure",
        )
        old_last_before = old.video_counter.last_pts_ns
        self._wait_for(
            lambda: (
                successor.video_counter.count > 0
                and (not successor.audio or successor.audio_counter.count > 0)
            ),
            timeout_seconds=3.0,
            reason=f"generation {successor.number} first data",
        )
        if (
            successor.ingress_event_error is not None
            or not successor.video_first_buffer_had_sticky_contract
            or (successor.audio and not successor.audio_first_buffer_had_sticky_contract)
        ):
            raise HarnessError(
                "successor sticky-event/data ordering gate failed: "
                f"generation={successor.number},"
                f"ingress_error={successor.ingress_event_error},"
                f"video_events={sorted(successor.video_events)},"
                f"audio_events={sorted(successor.audio_events)},"
                f"video_first={successor.video_first_buffer_had_sticky_contract},"
                f"audio_first={successor.audio_first_buffer_had_sticky_contract},"
                f"video_count={successor.video_counter.count},"
                f"audio_count={successor.audio_counter.count}"
            )
        if old_last_before is None or successor.video_counter.first_pts_ns is None:
            raise HarnessError("handoff video boundary counters are incomplete")
        raw_gap = successor.video_counter.first_pts_ns - old_last_before
        normalized_gap = abs(raw_gap - FRAME_PERIOD_NS)
        transition: dict[str, object] = {
            "old_generation": old.number,
            "new_generation": successor.number,
            "retired_active_location": active_location,
            "blocked_idr_pts_ns": handoff.video_pts_ns,
            "blocked_audio_pts_ns": handoff.audio_pts_ns,
            "blocked_video_running_time_ns": handoff.video_running_time_ns,
            "blocked_audio_running_time_ns": handoff.audio_running_time_ns,
            "blocked_input_alignment_ns": input_alignment_ns,
            "old_last_video_pts_ns": old_last_before,
            "new_first_video_pts_ns": successor.video_counter.first_pts_ns,
            "raw_video_gap_ns": raw_gap,
            "normalized_video_gap_ns": normalized_gap,
            "within_one_frame": normalized_gap <= FRAME_PERIOD_NS,
            "new_first_video_is_idr": successor.video_counter.first_delta is False,
            "blocked_duration_ns": blocked_duration_ns,
            "audio_blocked_duration_ns": audio_blocked_duration_ns,
            "closure_latency_ns": time.monotonic_ns() - started_ns,
            "video_eos_return": True,
            "audio_eos_return": audio_eos,
            "successor_sync_return": successor_sync_return,
            "old_video_eos_observed": old.video_eos_seen,
            "old_audio_eos_observed": old.audio_eos_seen if old.audio else None,
        }
        self.transitions.append(transition)
        if (
            not transition["within_one_frame"]
            or not transition["new_first_video_is_idr"]
            or not 0 <= input_alignment_ns < MAX_INPUT_ALIGNMENT_NS
            or cast(int, transition["blocked_duration_ns"]) >= 2_000_000_000
        ):
            raise HarnessError(
                "generation handoff continuity/IDR gate failed: "
                f"old={old.number},new={successor.number},"
                f"raw_gap_ns={raw_gap},normalized_gap_ns={normalized_gap},"
                f"input_alignment_ns={input_alignment_ns},"
                f"new_first_is_idr={transition['new_first_video_is_idr']},"
                f"blocked_duration_ns={blocked_duration_ns}"
            )
        old.retired = True
        self._record_event(
            "generation_drained",
            generation=old.number,
            left_attached_until_parent_null=True,
        )
        self._assert_parent_identity()

    def release_after_parent_null(self, generation: Generation) -> None:
        waited, state, _pending = generation.bin.get_state(2 * self.gst.SECOND)
        if waited == self.gst.StateChangeReturn.FAILURE or state != self.gst.State.NULL:
            raise HarnessError("generation did not inherit parent NULL")
        if generation.external_linked:
            self._unlink_external(generation)
        self.video_tee.release_request_pad(generation.video_tee_pad)
        if generation.audio_tee_pad is not None:
            self.audio_tee.release_request_pad(generation.audio_tee_pad)
        video_peer = generation.video_queue.get_static_pad("src")
        if video_peer is None or not video_peer.unlink(generation.output_video_pad):
            raise HarnessError("retired splitmux video pad unlink failed")
        generation.output.release_request_pad(generation.output_video_pad)
        if generation.output_audio_pad is not None and generation.audio_queue is not None:
            audio_peer = generation.audio_queue.get_static_pad("src")
            if audio_peer is None or not audio_peer.unlink(generation.output_audio_pad):
                raise HarnessError("retired splitmux audio pad unlink failed")
            generation.output.release_request_pad(generation.output_audio_pad)
        self.pipeline.remove(generation.bin)
        if generation.bin.get_parent() is not None:
            raise HarnessError("retired generation bin removal failed")
        self._record_event(
            "generation_released_after_parent_null",
            generation=generation.number,
            null_wait_return=int(waited),
            request_pads_released_after_null=True,
        )

    def stop(self, final: Generation) -> None:
        if (
            final.ingress_event_error is not None
            or not final.video_first_buffer_had_sticky_contract
            or not final.audio_first_buffer_had_sticky_contract
        ):
            raise HarnessError("final generation sticky-event/data contract failed")
        active_locations = [
            location
            for location in final.opened_locations
            if location not in final.closed_locations
        ]
        if len(active_locations) != 1:
            raise HarnessError("final generation has no unique active fragment")
        active_location = active_locations[0]
        eos_return = bool(self.pipeline.send_event(self.gst.Event.new_eos()))
        if not eos_return:
            raise HarnessError("parent pipeline refused final EOS")
        self._wait_for(
            lambda: (
                active_location in final.closed_locations
                and set(final.opened_locations) == set(final.closed_locations)
                and final.video_eos_seen
                and final.audio_eos_seen
            ),
            timeout_seconds=20.0,
            reason="final active fragment closure",
        )
        self._drain_safety_bus_quiet()
        null_return = self.pipeline.set_state(self.gst.State.NULL)
        waited, state, _pending = self.pipeline.get_state(15 * self.gst.SECOND)
        if (
            null_return == self.gst.StateChangeReturn.FAILURE
            or waited == self.gst.StateChangeReturn.FAILURE
            or state != self.gst.State.NULL
        ):
            raise HarnessError("parent pipeline did not stop cleanly")
        self._drain_safety_bus_quiet()
        for generation in self.generations.values():
            self.release_after_parent_null(generation)
        self._record_event(
            "pipeline_stopped",
            eos_return=eos_return,
            null_return=int(null_return),
            null_wait_return=int(waited),
        )

    def run(self) -> dict[str, object]:
        first = self.create_generation(1, True)
        second = self.create_generation(2, False)
        third = self.create_generation(3, True)
        try:
            self.start(first)
            self._wait_for(
                lambda: len(first.closed_locations) >= SWITCH_CLOSURES,
                timeout_seconds=20.0,
                reason="first A/V diagnostic fragments",
            )
            self.switch(first, second)
            self._wait_for(
                lambda: len(second.closed_locations) >= SWITCH_CLOSURES,
                timeout_seconds=20.0,
                reason="video-only diagnostic fragments",
            )
            self.switch(second, third)
            self._wait_for(
                lambda: len(third.closed_locations) >= FINAL_CLOSURES,
                timeout_seconds=25.0,
                reason="restored A/V diagnostic fragments",
            )
            final_count = third.video_counter.count
            self._wait_for(
                lambda: third.video_counter.count >= final_count + 30,
                timeout_seconds=3.0,
                reason="bounded final-fragment media",
            )
            self.stop(third)
        except BaseException as original:
            null_return = self.pipeline.set_state(self.gst.State.NULL)
            waited, state, _pending = self.pipeline.get_state(15 * self.gst.SECOND)
            if (
                null_return == self.gst.StateChangeReturn.FAILURE
                or waited == self.gst.StateChangeReturn.FAILURE
                or state != self.gst.State.NULL
            ):
                raise HarnessError(
                    "run failed and bounded cleanup did not reach parent NULL: "
                    f"{_bounded_detail(original)}"
                ) from original
            raise
        return {
            "events": self.events,
            "transitions": self.transitions,
            "warnings": self.warnings,
            "errors": self.errors,
            "parent": {
                "camera_object_preserved": self.pipeline.get_by_name("camera") is self.camera,
                "encoder_object_preserved": self.pipeline.get_by_name("encoder") is self.encoder,
                "parser_object_preserved": self.pipeline.get_by_name("parser") is self.parser,
                "base_time_ns": self.base_time_ns,
                "video_source": self.video_source_counter.snapshot(),
                "audio_source": self.audio_source_counter.snapshot(),
            },
            "generations": {
                str(number): {
                    "audio": generation.audio,
                    "retired": generation.retired,
                    "opened_locations": generation.opened_locations,
                    "closed_locations": generation.closed_locations,
                    "video_eos_seen": generation.video_eos_seen,
                    "audio_eos_seen": generation.audio_eos_seen,
                    "video_events": sorted(generation.video_events),
                    "audio_events": sorted(generation.audio_events),
                    "video_first_buffer_had_sticky_contract": (
                        generation.video_first_buffer_had_sticky_contract
                    ),
                    "audio_first_buffer_had_sticky_contract": (
                        generation.audio_first_buffer_had_sticky_contract
                    ),
                    "ingress_event_error": generation.ingress_event_error,
                    "external_linked": generation.external_linked,
                    "video": generation.video_counter.snapshot(),
                    "audio_counter": generation.audio_counter.snapshot(),
                }
                for number, generation in sorted(self.generations.items())
            },
        }


def _strict_json(payload: bytes, name: str) -> Mapping[str, object]:
    def no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise HarnessError(f"{name} contains duplicate keys")
            result[key] = value
        return result

    try:
        value = json.loads(payload, object_pairs_hook=no_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HarnessError(f"{name} is invalid JSON") from error
    if not isinstance(value, Mapping):
        raise HarnessError(f"{name} is not a JSON object")
    return cast(Mapping[str, object], value)


def _checked_command(result: CommandResult, name: str) -> bytes:
    if result.returncode != 0 or result.timed_out or result.output_truncated or result.stderr:
        raise HarnessError(f"{name} failed: {_bounded_detail(result.stderr)}")
    return result.stdout


def _probe_media(path: Path) -> Mapping[str, object]:
    result = run_fixed_argv(
        (
            FFPROBE,
            "-v",
            "error",
            "-show_entries",
            "stream=index,codec_type,codec_name,profile,width,height,r_frame_rate,"
            "sample_rate,channels,start_time,duration,bit_rate:"
            "format=duration,size",
            "-of",
            "json",
            str(path),
        ),
        timeout_seconds=10.0,
        max_output_bytes=MAX_COMMAND_OUTPUT_BYTES,
    )
    return _strict_json(_checked_command(result, f"ffprobe {path.name}"), path.name)


def _first_packet_is_idr(path: Path) -> bool:
    result = run_fixed_argv(
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
    document = _strict_json(_checked_command(result, f"IDR probe {path.name}"), path.name)
    packets = document.get("packets")
    if not isinstance(packets, Sequence) or len(packets) != 1:
        return False
    packet = packets[0]
    if (
        not isinstance(packet, Mapping)
        or set(packet) != {"codec_type", "flags", "data"}
        or packet.get("codec_type") != "video"
        or "K" not in str(packet.get("flags", ""))
    ):
        return False
    data = packet.get("data")
    return isinstance(data, str) and _contains_h264_idr(data)


def _contains_h264_idr(data: str) -> bool:
    words: list[str] = []
    for line in data.splitlines():
        payload = line.split(":", 1)[1] if ":" in line else line
        # ffprobe appends a printable-ASCII column after two spaces. Never
        # interpret an ASCII word made only of a-f as additional packet bytes.
        hex_column = payload.strip().split("  ", 1)[0]
        words.extend(
            word
            for word in hex_column.split()
            if len(word) % 2 == 0 and re.fullmatch(r"[0-9A-Fa-f]{2,8}", word)
        )
    try:
        raw = bytes.fromhex("".join(words))
    except ValueError:
        return False
    for marker in (b"\x00\x00\x01", b"\x00\x00\x00\x01"):
        offset = 0
        while (index := raw.find(marker, offset)) >= 0:
            position = index + len(marker)
            if position < len(raw) and raw[position] & 0x1F == 5:
                return True
            offset = position
    offset = 0
    while offset + 4 <= len(raw):
        size = int.from_bytes(raw[offset : offset + 4], "big")
        offset += 4
        if size <= 0 or offset + size > len(raw):
            return False
        if raw[offset] & 0x1F == 5:
            return True
        offset += size
    return False


def _decode_media(path: Path, audio: bool) -> None:
    arguments = [
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
    ]
    if audio:
        arguments.extend(("-map", "0:a:0"))
    arguments.extend(("-f", "null", "-"))
    result = run_fixed_argv(
        tuple(arguments),
        timeout_seconds=20.0,
        max_output_bytes=MAX_COMMAND_OUTPUT_BYTES,
    )
    _checked_command(result, f"hardware decode {path.name}")


def validate_media(directory: Path) -> dict[str, object]:
    files = sorted(path for path in directory.iterdir() if path.suffix == ".mp4")
    if not MIN_MEDIA_COUNT <= len(files) <= MAX_MEDIA_COUNT:
        raise HarnessError("diagnostic media count left its 8-12 bound")
    evidence: list[dict[str, object]] = []
    restored_skews: list[float] = []
    expected_generations = {1: True, 2: False, 3: True}
    observed_generations: dict[int, int] = {1: 0, 2: 0, 3: 0}
    for path in files:
        if path.parent != directory or path.is_symlink() or not path.is_file():
            raise HarnessError("diagnostic media member is not one direct regular file")
        match = MEDIA_NAME_RE.fullmatch(path.name)
        if match is None:
            raise HarnessError("foreign MP4 exists in fresh diagnostic directory")
        generation = int(match.group(1))
        audio_expected = expected_generations[generation]
        observed_generations[generation] += 1
        document = _probe_media(path)
        allowed_top_level = {"streams", "format", "programs", "stream_groups"}
        if (
            not {"streams", "format"} <= set(document)
            or not set(document) <= allowed_top_level
        ):
            raise HarnessError("ffprobe top-level schema differs")
        for optional_name in ("programs", "stream_groups"):
            if optional_name in document and document[optional_name] != []:
                raise HarnessError("ffprobe optional collection is not empty")
        format_value = document.get("format")
        if not isinstance(format_value, Mapping) or set(format_value) != {
            "duration",
            "size",
        }:
            raise HarnessError("ffprobe format schema differs")
        try:
            format_duration = float(cast(str, format_value["duration"]))
            format_size = int(cast(str, format_value["size"]))
        except (TypeError, ValueError) as error:
            raise HarnessError("ffprobe format values are invalid") from error
        if not 0.10 <= format_duration <= 5.0 or not 1 <= format_size <= 128 * 1024 * 1024:
            raise HarnessError("diagnostic media size/duration left its bound")
        streams = document.get("streams")
        if not isinstance(streams, Sequence) or isinstance(streams, str | bytes):
            raise HarnessError("ffprobe stream schema differs")
        typed = [stream for stream in streams if isinstance(stream, Mapping)]
        video = [stream for stream in typed if stream.get("codec_type") == "video"]
        audio = [stream for stream in typed if stream.get("codec_type") == "audio"]
        if len(video) != 1 or len(audio) != int(audio_expected) or len(typed) != len(streams):
            raise HarnessError("generation stream set differs")
        video_stream = video[0]
        if set(video_stream) != {
            "index",
            "codec_type",
            "codec_name",
            "profile",
            "width",
            "height",
            "r_frame_rate",
            "start_time",
            "duration",
            "bit_rate",
        }:
            raise HarnessError("video ffprobe schema differs")
        try:
            video_bitrate = int(cast(str, video_stream["bit_rate"]))
            video_duration = float(cast(str, video_stream["duration"]))
        except (TypeError, ValueError) as error:
            raise HarnessError("video bitrate/duration evidence is invalid") from error
        if (
            video_stream.get("codec_name") != "h264"
            or video_stream.get("profile") != "High"
            or video_stream.get("width") != 1920
            or video_stream.get("height") != 1080
            or video_stream.get("r_frame_rate") != "30/1"
            or not 6_000_000 <= video_bitrate <= 10_000_000
            or not 0.10 <= video_duration <= 5.0
        ):
            raise HarnessError("video stream contract differs")
        skew: float | None = None
        if audio_expected:
            audio_stream = audio[0]
            if set(audio_stream) != {
                "index",
                "codec_type",
                "codec_name",
                "profile",
                "sample_rate",
                "channels",
                "r_frame_rate",
                "start_time",
                "duration",
                "bit_rate",
            }:
                raise HarnessError("audio ffprobe schema differs")
            try:
                audio_bitrate = int(cast(str, audio_stream["bit_rate"]))
                audio_duration = float(cast(str, audio_stream["duration"]))
            except (TypeError, ValueError) as error:
                raise HarnessError("audio bitrate/duration evidence is invalid") from error
            if (
                audio_stream.get("codec_name") != "aac"
                or audio_stream.get("profile") != "LC"
                or str(audio_stream.get("sample_rate")) != "48000"
                or audio_stream.get("channels") != 1
                or audio_stream.get("r_frame_rate") != "0/0"
                or not 120_000 <= audio_bitrate <= 136_000
                or not 0.10 <= audio_duration <= 5.0
            ):
                raise HarnessError("audio stream contract differs")
            try:
                audio_start = float(cast(str, audio_stream.get("start_time")))
                video_start = float(cast(str, video_stream.get("start_time")))
                skew = max(
                    abs(audio_start - video_start),
                    abs(
                        (audio_start + audio_duration)
                        - (video_start + video_duration)
                    ),
                )
            except (TypeError, ValueError) as error:
                raise HarnessError("stream edge-time evidence is invalid") from error
            if generation == 3:
                restored_skews.append(skew)
        idr = _first_packet_is_idr(path)
        if not idr:
            raise HarnessError(f"diagnostic MP4 does not start with an IDR: {path.name}")
        _decode_media(path, audio_expected)
        evidence.append(
            {
                "file": path.name,
                "sha256": _sha256_file(path, maximum=128 * 1024 * 1024),
                "generation": generation,
                "audio": audio_expected,
                "first_packet_idr": idr,
                "hardware_decode": True,
                "stream_edge_skew_seconds": skew,
            }
        )
    if any(count < 2 for count in observed_generations.values()):
        raise HarnessError("each immutable generation did not produce at least two clips")
    stabilized_skews = restored_skews[1:]
    if (
        len(restored_skews) < 3
        or max(restored_skews) >= 0.100
        or max(stabilized_skews) - min(stabilized_skews) > 0.050
    ):
        raise HarnessError(
            "restored A/V skew/drift contract failed: "
            f"restored_skew_seconds={restored_skews}"
        )
    return {
        "count": len(files),
        "generation_counts": observed_generations,
        "restored_skew_seconds": restored_skews,
        "stabilized_skew_spread_seconds": (
            max(stabilized_skews) - min(stabilized_skews)
        ),
        "members": evidence,
    }


def execute(output_directory: Path) -> dict[str, object]:
    release = _release_identity()
    before_unit = _read_unit_state()
    before_throttle = _read_throttle()
    if before_throttle != "throttled=0x0":
        raise HarnessError("Pi was throttled before the experiment")
    config = load_config(CONFIG_PATH)
    storage = run_live_storage_preflight(config)
    if (
        not storage.ready
        or not storage.probe_attempted
        or not storage.probe_succeeded
        or storage.facts is None
        or storage.facts.mount.target != str(RECORDING_ROOT)
        or storage.facts.mount.filesystem != "exfat"
        or storage.facts.mount.label != "DASHCAM"
    ):
        raise HarnessError("production exact exFAT/sentinel preflight is not READY")
    selector = parse_alsa_selector(config.audio.device_match)
    discovery = discover_capture_device(selector)
    if discovery.status is not AudioDiscoveryStatus.MATCHED or discovery.device is None:
        raise HarnessError(f"exact microphone was not matched: {discovery.status.value}")
    directory_evidence = _prepare_output_directory(output_directory)
    experiment = Experiment(output_directory, discovery.device.capture_endpoint)
    runtime = experiment.run()
    media = validate_media(output_directory)
    after_unit = _read_unit_state()
    after_throttle = _read_throttle()
    if after_unit != before_unit or after_unit["restarts"] != 0:
        raise HarnessError("dashcamd state/restart counter changed")
    if after_throttle != "throttled=0x0":
        raise HarnessError("Pi throttled during the experiment")
    transitions = cast(list[Mapping[str, object]], runtime["transitions"])
    parent = cast(Mapping[str, object], runtime["parent"])
    video_source = cast(Mapping[str, object], parent["video_source"])
    generations = cast(Mapping[str, Mapping[str, object]], runtime["generations"])
    generation_video = [
        cast(Mapping[str, object], generations[str(number)]["video"])
        for number in (1, 2, 3)
    ]
    generation_video_clean = all(
        isinstance(counter.get("count"), int)
        and not isinstance(counter.get("count"), bool)
        and cast(int, counter["count"]) > 0
        and counter.get("non_monotonic") == 0
        and counter.get("large_gaps") == 0
        for counter in generation_video
    )
    routed_video_count = sum(cast(int, counter["count"]) for counter in generation_video)
    passed = (
        len(transitions) == 2
        and all(transition.get("within_one_frame") is True for transition in transitions)
        and all(transition.get("new_first_video_is_idr") is True for transition in transitions)
        and not runtime["warnings"]
        and not runtime["errors"]
        and video_source.get("non_monotonic") == 0
        and video_source.get("large_gaps") == 0
        and generation_video_clean
        and routed_video_count == video_source.get("count")
        and parent.get("camera_object_preserved") is True
        and parent.get("encoder_object_preserved") is True
        and parent.get("parser_object_preserved") is True
    )
    if not passed:
        raise HarnessError("immutable-generation runtime acceptance failed")
    return {
        "schema_version": 1,
        "passed": True,
        "safe_to_integrate_production": False,
        "scope": "isolated_programmatic_capability_only",
        "release": release,
        "unit_before": before_unit,
        "unit_after": after_unit,
        "throttle_before": before_throttle,
        "throttle_after": after_throttle,
        "microphone": {
            "status": discovery.status.value,
            "endpoint": discovery.device.capture_endpoint,
            "identity": {
                "vendor_id": discovery.device.identity.vendor_id,
                "product_id": discovery.device.identity.product_id,
                "product": discovery.device.identity.product,
                "physical_path": discovery.device.identity.physical_path,
                "serial": discovery.device.identity.serial,
                "alsa_card_id": discovery.device.identity.alsa_card_id,
            },
        },
        "storage_preflight": {
            "ready": storage.ready,
            "probe_attempted": storage.probe_attempted,
            "probe_succeeded": storage.probe_succeeded,
            "target": storage.facts.mount.target,
            "filesystem": storage.facts.mount.filesystem,
            "label": storage.facts.mount.label,
            "device_id": storage.facts.mount.device_id,
        },
        "directory": directory_evidence,
        "runtime": runtime,
        "media": media,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-manifest-sha256", required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run-experiment")
    run.add_argument("--output-directory", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    started = time.monotonic_ns()
    verify_manifest(arguments.expected_manifest_sha256)
    try:
        result = execute(arguments.output_directory)
        status = 0
    except BaseException as error:
        result = {
            "schema_version": 1,
            "passed": False,
            "safe_to_integrate_production": False,
            "scope": "isolated_programmatic_capability_only",
            "error_type": type(error).__name__,
            "error": _bounded_detail(error),
        }
        status = 1
    document = {
        **result,
        "started_monotonic_ns": started,
        "ended_monotonic_ns": time.monotonic_ns(),
    }
    _write_atomic_exclusive_json(arguments.output, document)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
