#!/usr/bin/env python3
# ruff: noqa: E501
"""Bounded exact-Pi benchmark for the rejected pre-rendered RGBA candidate.

This is deliberately *not* a production ``dashcamd`` qualification.  The
previous NV12 ``textoverlay`` candidate failed the 1080p30 gate, so this
standalone harness refuses an active recorder and benchmarks
``gdkpixbufoverlay`` with a small, pre-rendered RGBA region before the real
V4L2 H.264 encoder.  It does not touch the production service, configuration,
GPS, network, clock, partition table, or media catalog.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import struct
import sys
import tempfile
import time
import zlib
from collections.abc import Iterator, Mapping, Sequence, Set
from contextlib import contextmanager
from pathlib import Path
from typing import Final, cast

from dashcam.diagnostics.media import CommandResult, parse_probe_json, run_fixed_argv

SYSTEMCTL: Final = "/usr/bin/systemctl"
FINDMNT: Final = "/usr/bin/findmnt"
VCGENCMD: Final = "/usr/bin/vcgencmd"
FFPROBE: Final = "/usr/bin/ffprobe"
FFMPEG: Final = "/usr/bin/ffmpeg"
SERVICE: Final = "dashcamd.service"
AP_SERVICE: Final = "dashcam-network-fallback.service"
RECORDING_ROOT: Final = Path("/srv/dashcam")
QUARANTINE_ROOT: Final = RECORDING_ROOT / "quarantine"
LOCK_PATH: Final = Path("/run/lock/dashcam-m9-overlay.lock")

EXPECTED_MODEL_PREFIX: Final = "Raspberry Pi Zero 2 W"
SHA256_RE: Final = re.compile(r"[0-9a-f]{64}")
SERIAL_RE: Final = re.compile(r"[0-9a-f]{16}")
UUID_RE: Final = re.compile(r"[0-9A-F]{4}-[0-9A-F]{4}")
RELEASE_RE: Final = re.compile(r"0\.1\.0\.dev0-[0-9a-f]{16}")
CURRENT_RELEASE: Final = Path("/opt/dashcam/current")

MAX_COMMAND_OUTPUT_BYTES: Final = 64 * 1024
MAX_RESULT_BYTES: Final = 256 * 1024
MAX_SCRIPT_BYTES: Final = 2 * 1024 * 1024
MAX_MEDIA_BYTES: Final = 256 * 1024 * 1024
MAX_DURATION_S: Final = 90.0
DEFAULT_DURATION_S: Final = 20.0
DEFAULT_MINIMUM_FPS: Final = 29.5
MAX_DYNAMIC_UPDATES: Final = 30
OVERLAY_WIDTH: Final = 1536
OVERLAY_HEIGHT: Final = 64
OVERLAY_X: Final = 40
OVERLAY_Y: Final = 40
POLL_INTERVAL_S: Final = 0.05
STOP_TIMEOUT_S: Final = 15.0


class HarnessError(RuntimeError):
    """The exact-Pi candidate benchmark could not prove its closed contract."""


def _detail(value: object, maximum: int = 512) -> str:
    text = " ".join(str(value).replace("\0", " ").splitlines())
    return "".join(char if char.isprintable() else " " for char in text)[:maximum]


def _regular_bytes(path: Path, maximum: int) -> bytes:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise HarnessError(f"{path} is not a regular file")
        retained = bytearray()
        while chunk := os.read(descriptor, min(65_536, maximum + 1 - len(retained))):
            retained.extend(chunk)
            if len(retained) > maximum:
                raise HarnessError(f"{path} exceeded its read bound")
        return bytes(retained)
    finally:
        os.close(descriptor)


def _script_sha256() -> str:
    return hashlib.sha256(_regular_bytes(Path(__file__).resolve(), MAX_SCRIPT_BYTES)).hexdigest()


def _command(
    argv: Sequence[str],
    *,
    timeout: float = 10.0,
    allowed_returncodes: Set[int] = frozenset({0}),
) -> CommandResult:
    result = run_fixed_argv(
        argv, timeout_seconds=timeout, max_output_bytes=MAX_COMMAND_OUTPUT_BYTES
    )
    if result.returncode not in allowed_returncodes or result.timed_out or result.output_truncated:
        raise HarnessError(
            f"bounded command failed: {_detail(result.argv)}: {_detail(result.stderr)}"
        )
    return result


def _systemctl(*arguments: str, timeout: float = 10.0) -> CommandResult:
    return _command((SYSTEMCTL, *arguments), timeout=timeout)


def _unit_properties(unit: str) -> dict[str, str]:
    names = ("LoadState", "ActiveState", "SubState", "MainPID", "NRestarts", "Result")
    output = _systemctl("show", "--no-pager", *(f"--property={name}" for name in names), unit)
    values: dict[str, str] = {}
    for line in output.stdout.decode("ascii", "strict").splitlines():
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


def _require_inactive(unit: str) -> dict[str, str]:
    value = _unit_properties(unit)
    if (value["LoadState"], value["ActiveState"], value["SubState"], value["MainPID"]) != (
        "loaded",
        "inactive",
        "dead",
        "0",
    ):
        raise HarnessError(
            f"{unit} must be loaded/inactive/dead; benchmark will not compete for camera ownership"
        )
    return value


def _throttle() -> str:
    value = _command((VCGENCMD, "get_throttled")).stdout.decode("ascii", "strict").strip()
    if re.fullmatch(r"throttled=0x[0-9a-fA-F]+", value) is None:
        raise HarnessError("throttle result shape differs")
    return value


def _temperature_c() -> float:
    raw = (
        _regular_bytes(Path("/sys/class/thermal/thermal_zone0/temp"), 64)
        .decode("ascii", "strict")
        .strip()
    )
    if not raw.isdecimal():
        raise HarnessError("thermal reading shape differs")
    value = int(raw) / 1000.0
    if not -100.0 <= value <= 250.0:
        raise HarnessError("thermal reading is outside its physical bound")
    return value


def _board_identity(expected_serial: str) -> dict[str, str]:
    if SERIAL_RE.fullmatch(expected_serial) is None:
        raise HarnessError("expected board serial is not canonical")
    model = _regular_bytes(Path("/proc/device-tree/model"), 256).rstrip(b"\0").decode("ascii")
    serial = None
    for line in _regular_bytes(Path("/proc/cpuinfo"), 32 * 1024).decode("ascii").splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() == "Serial":
            serial = value.strip().casefold()
            break
    if not model.startswith(EXPECTED_MODEL_PREFIX) or serial != expected_serial:
        raise HarnessError("exact Pi model or serial differs")
    return {"model": model, "serial": serial}


def _release_identity(expected_release: str) -> dict[str, str]:
    if RELEASE_RE.fullmatch(expected_release) is None:
        raise HarnessError("expected release is not canonical")
    if (
        not CURRENT_RELEASE.is_symlink()
        or CURRENT_RELEASE.resolve(strict=True) != Path("/opt/dashcam/releases") / expected_release
    ):
        raise HarnessError("current installed release differs")
    return {"release": expected_release, "path": CURRENT_RELEASE.resolve().as_posix()}


def _storage_identity(expected_uuid: str) -> dict[str, str]:
    if UUID_RE.fullmatch(expected_uuid) is None:
        raise HarnessError("expected storage UUID is not canonical")
    result = _command(
        (FINDMNT, "-n", "-o", "TARGET,FSTYPE,LABEL,UUID,SOURCE", "--target", str(RECORDING_ROOT))
    )
    fields = result.stdout.decode("ascii", "strict").strip().split()
    if len(fields) != 5:
        raise HarnessError("recording mount identity shape differs")
    target, filesystem, label, uuid, source = fields
    if (target, filesystem.casefold(), label, uuid, source) != (
        str(RECORDING_ROOT),
        "exfat",
        "DASHCAM",
        expected_uuid,
        "/dev/mmcblk0p3",
    ):
        raise HarnessError("recording mount is not the declared exact exFAT volume")
    if os.stat("/").st_dev == os.stat(RECORDING_ROOT).st_dev:
        raise HarnessError("recording root is not a distinct filesystem")
    return {
        "target": target,
        "filesystem": filesystem,
        "label": label,
        "uuid": uuid,
        "source": source,
    }


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _write_rgba_png(path: Path, *, accent: tuple[int, int, int]) -> None:
    """Create a fixed 1536x64 RGBA bitmap without text rendering or PIL."""

    rows = bytearray()
    red, green, blue = accent
    for y in range(OVERLAY_HEIGHT):
        rows.append(0)
        for x in range(OVERLAY_WIDTH):
            border = x < 2 or y < 2 or x >= OVERLAY_WIDTH - 2 or y >= OVERLAY_HEIGHT - 2
            marker = 16 <= x < 112 and 16 <= y < 48
            rows.extend((red, green, blue, 224) if border or marker else (0, 0, 0, 144))
    payload = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", OVERLAY_WIDTH, OVERLAY_HEIGHT, 8, 6, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(bytes(rows), level=9))
        + _png_chunk(b"IEND", b"")
    )
    path.write_bytes(payload)


class _Counters:
    def __init__(self) -> None:
        self.raw = 0
        self.encoded = 0
        self.estimated_drops = 0
        self._last_pts: int | None = None

    def raw_probe(self, _pad: object, info: object, gst: object) -> object:
        buffer = info.get_buffer()
        if buffer is not None:
            self.raw += 1
            pts = int(buffer.pts)
            none = int(gst.CLOCK_TIME_NONE)
            if pts != none and self._last_pts is not None and pts > self._last_pts:
                periods = round((pts - self._last_pts) / (1_000_000_000 / 30))
                if periods > 1:
                    self.estimated_drops += periods - 1
            if pts != none:
                self._last_pts = pts
        return gst.PadProbeReturn.OK

    def encoded_probe(self, _pad: object, info: object, gst: object) -> object:
        if info.get_buffer() is not None:
            self.encoded += 1
        return gst.PadProbeReturn.OK


def _load_gst() -> tuple[object, object]:
    try:
        import gi

        gi.require_version("Gst", "1.0")
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf, Gst

        Gst.init(None)
    except (ImportError, AttributeError, ValueError) as error:
        raise HarnessError(
            f"required GStreamer/GdkPixbuf binding is unavailable: {_detail(error)}"
        ) from error
    return Gst, GdkPixbuf


def _pipeline_description(location: Path, image: Path) -> str:
    if not location.is_absolute() or not image.is_absolute():
        raise HarnessError("candidate paths must be absolute")
    return (
        "libcamerasrc name=camera ! video/x-raw,width=(int)1920,height=(int)1080,format=(string)NV12,framerate=(fraction)30/1 ! "
        f"gdkpixbufoverlay name=overlay location={image.as_posix()} offset-x={OVERLAY_X} offset-y={OVERLAY_Y} overlay-width={OVERLAY_WIDTH} overlay-height={OVERLAY_HEIGHT} ! "
        'v4l2h264enc name=encoder extra-controls="controls,repeat_sequence_header=1,video_bitrate=8000000,h264_i_frame_period=30" ! '
        "video/x-h264,profile=(string)high,level=(string)4.1 ! h264parse config-interval=-1 ! "
        f'splitmuxsink name=output location={location.as_posix()} max-size-time=60000000000 max-size-bytes=0 send-keyframe-requests=true async-finalize=true muxer-factory=mp4mux sink-factory=filesink muxer-properties="properties,fragment-duration=(uint)1000,fragment-mode=(int)0"'
    )


def _factory_name(element: object) -> str:
    factory = element.get_factory()
    if factory is None:
        raise HarnessError("encoder has no factory")
    return str(factory.get_name())


def _effective_encoder(element: object) -> dict[str, object]:
    sink = element.get_static_pad("sink")
    source = element.get_static_pad("src")
    if (
        sink is None
        or source is None
        or sink.get_current_caps() is None
        or source.get_current_caps() is None
    ):
        raise HarnessError("encoder caps are not negotiated")
    raw = sink.get_current_caps().get_structure(0)
    encoded = source.get_current_caps().get_structure(0)
    if (
        raw is None
        or encoded is None
        or raw.get_name() != "video/x-raw"
        or encoded.get_name() != "video/x-h264"
    ):
        raise HarnessError("encoder negotiated unexpected media types")
    return {
        "factory_name": _factory_name(element),
        "raw_format": str(raw.get_value("format")),
        "width": int(raw.get_value("width")),
        "height": int(raw.get_value("height")),
        "framerate": str(raw.get_value("framerate")),
        "profile": str(encoded.get_value("profile")),
        "level": str(encoded.get_value("level")),
    }


def _set_playing(pipeline: object, gst: object) -> None:
    if pipeline.set_state(gst.State.PLAYING) == gst.StateChangeReturn.FAILURE:
        raise HarnessError("candidate pipeline rejected PLAYING")
    outcome = pipeline.get_state(15 * 1_000_000_000)
    if (
        not isinstance(outcome, tuple)
        or outcome[0] == gst.StateChangeReturn.FAILURE
        or outcome[1] != gst.State.PLAYING
    ):
        raise HarnessError("candidate pipeline did not reach PLAYING")


def _run_pipeline(work: Path, *, duration_s: float, dynamic_updates: bool) -> dict[str, object]:
    gst, pixbuf = _load_gst()
    primary, alternate = work / "overlay-a.png", work / "overlay-b.png"
    media = work / "candidate-%02d.mp4"
    _write_rgba_png(primary, accent=(255, 32, 32))
    _write_rgba_png(alternate, accent=(32, 255, 32))
    pipeline = gst.parse_launch(_pipeline_description(media, primary))
    overlay, encoder = pipeline.get_by_name("overlay"), pipeline.get_by_name("encoder")
    if overlay is None or encoder is None:
        raise HarnessError("candidate pipeline omitted named overlay or encoder")
    counters = _Counters()
    raw_pad, encoded_pad = encoder.get_static_pad("sink"), encoder.get_static_pad("src")
    if raw_pad is None or encoded_pad is None:
        raise HarnessError("candidate encoder pads are absent")
    raw_pad.add_probe(gst.PadProbeType.BUFFER, counters.raw_probe, gst)
    encoded_pad.add_probe(gst.PadProbeType.BUFFER, counters.encoded_probe, gst)
    updates = 0
    start_wall, start_cpu = time.monotonic(), time.process_time()
    start_rss = _rss_bytes()
    start_temp = _temperature_c()
    _set_playing(pipeline, gst)
    encoder_evidence = _effective_encoder(encoder)
    if encoder_evidence != {
        **encoder_evidence,
        "factory_name": "v4l2h264enc",
        "raw_format": "NV12",
        "width": 1920,
        "height": 1080,
        "framerate": "30/1",
        "profile": "high",
        "level": "4.1",
    }:
        raise HarnessError("candidate lost the exact hardware H.264/NV12 profile")
    bus = pipeline.get_bus()
    deadline, next_update = time.monotonic() + duration_s, time.monotonic() + 1.0
    eos_seen = False
    try:
        while time.monotonic() < deadline:
            message = bus.timed_pop_filtered(
                int(POLL_INTERVAL_S * 1_000_000_000), gst.MessageType.ERROR | gst.MessageType.EOS
            )
            if message is not None:
                if message.type == gst.MessageType.ERROR:
                    error, debug = message.parse_error()
                    raise HarnessError(
                        f"candidate pipeline error: {_detail(error)}; {_detail(debug)}"
                    )
                if message.type == gst.MessageType.EOS:
                    raise HarnessError("candidate pipeline reached EOS before requested stop")
            if dynamic_updates and time.monotonic() >= next_update:
                if updates >= MAX_DYNAMIC_UPDATES:
                    raise HarnessError("dynamic pixbuf updates exceeded their bound")
                image = alternate if updates % 2 == 0 else primary
                overlay.set_property("pixbuf", pixbuf.Pixbuf.new_from_file(str(image)))
                updates += 1
                next_update += 1.0
        if not pipeline.send_event(gst.Event.new_eos()):
            raise HarnessError("candidate pipeline rejected EOS")
        stop_deadline = time.monotonic() + STOP_TIMEOUT_S
        while time.monotonic() < stop_deadline:
            message = bus.timed_pop_filtered(
                int(POLL_INTERVAL_S * 1_000_000_000), gst.MessageType.ERROR | gst.MessageType.EOS
            )
            if message is not None:
                if message.type == gst.MessageType.ERROR:
                    error, debug = message.parse_error()
                    raise HarnessError(f"candidate EOS error: {_detail(error)}; {_detail(debug)}")
                if message.type == gst.MessageType.EOS:
                    eos_seen = True
                    break
        if not eos_seen:
            raise HarnessError("candidate EOS did not complete boundedly")
    finally:
        pipeline.set_state(gst.State.NULL)
        pipeline.get_state(STOP_TIMEOUT_S * 1_000_000_000)
    elapsed = time.monotonic() - start_wall
    if elapsed <= 0:
        raise HarnessError("candidate elapsed time is invalid")
    return {
        "encoder": encoder_evidence,
        "raw_frames": counters.raw,
        "encoded_frames": counters.encoded,
        "estimated_drops": counters.estimated_drops,
        "measured_fps": counters.encoded / elapsed,
        "elapsed_seconds": elapsed,
        "cpu_percent_one_process": (time.process_time() - start_cpu) * 100.0 / elapsed,
        "rss_start_bytes": start_rss,
        "rss_end_bytes": _rss_bytes(),
        "temperature_start_c": start_temp,
        "temperature_end_c": _temperature_c(),
        "dynamic_pixbuf_updates": updates,
        "media": _single_media(work),
    }


def _rss_bytes() -> int:
    fields = _regular_bytes(Path("/proc/self/statm"), 256).decode("ascii", "strict").split()
    if len(fields) < 2 or not fields[1].isdecimal():
        raise HarnessError("process RSS shape differs")
    return int(fields[1]) * os.sysconf("SC_PAGE_SIZE")


def _single_media(work: Path) -> Path:
    files = sorted(
        path for path in work.glob("candidate-*.mp4") if path.is_file() and not path.is_symlink()
    )
    if len(files) != 1:
        raise HarnessError("candidate did not produce exactly one bounded media file")
    if files[0].stat().st_size <= 0 or files[0].stat().st_size > MAX_MEDIA_BYTES:
        raise HarnessError("candidate media size is outside its bound")
    return files[0]


def _validate_media(path: Path) -> dict[str, object]:
    probe = _command(
        (
            FFPROBE,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(path),
        ),
        timeout=30.0,
    )
    document = parse_probe_json(probe.stdout)
    streams = document.get("streams")
    if not isinstance(streams, list):
        raise HarnessError("candidate ffprobe has no stream list")
    video = next(
        (
            item
            for item in streams
            if isinstance(item, Mapping) and item.get("codec_type") == "video"
        ),
        None,
    )
    if not isinstance(video, Mapping) or (
        video.get("codec_name"),
        video.get("profile"),
        video.get("width"),
        video.get("height"),
        video.get("r_frame_rate"),
    ) != ("h264", "High", 1920, 1080, "30/1"):
        raise HarnessError("candidate media is not High 1080p30 H.264")
    decode = _command(
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
        timeout=120.0,
    )
    del decode
    return {
        "file_name": path.name,
        "sha256": hashlib.sha256(_regular_bytes(path, MAX_MEDIA_BYTES)).hexdigest(),
        "size_bytes": path.stat().st_size,
        "ffprobe_high_1080p30_h264": True,
        "independent_hardware_h264_decode": True,
    }


def _assert_privacy_safe(value: object, path: str = "result") -> None:
    forbidden = {"latitude", "longitude", "coordinates", "raw_nmea", "overlay_text", "text"}
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold() in forbidden:
                raise HarnessError(f"privacy-forbidden evidence key at {path}.{key}")
            _assert_privacy_safe(item, f"{path}.{key}")
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _assert_privacy_safe(item, f"{path}[{index}]")
    elif isinstance(value, str) and ("$GP" in value or "$GN" in value):
        raise HarnessError(f"raw NMEA appeared in evidence at {path}")


def _write_result(path: Path, value: Mapping[str, object]) -> str:
    if (
        not path.is_absolute()
        or path == RECORDING_ROOT
        or RECORDING_ROOT in path.parents
        or path.exists()
        or path.is_symlink()
        or path.parent.is_symlink()
        or not path.parent.is_dir()
        or os.stat(path.parent).st_dev == os.stat(RECORDING_ROOT).st_dev
    ):
        raise HarnessError("evidence output must be a new real rootfs file outside exFAT")
    _assert_privacy_safe(value)
    payload = (
        json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()
    if len(payload) > MAX_RESULT_BYTES:
        raise HarnessError("evidence result exceeds its bound")
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0), 0o600
    )
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return hashlib.sha256(payload).hexdigest()


@contextmanager
def _qualification_lock() -> Iterator[None]:
    try:
        import fcntl
    except ImportError as error:
        raise HarnessError("kernel qualification lock is unavailable") from error
    descriptor = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise HarnessError("another live qualification already holds the camera") from error
        yield
    finally:
        os.close(descriptor)


def qualify(arguments: argparse.Namespace) -> dict[str, object]:
    evidence: dict[str, object] = {
        "schema_version": 1,
        "phase": "milestone9_gdkpixbufoverlay_candidate",
        "passed": False,
        "privacy": {
            "coordinates_retained_in_result": False,
            "raw_nmea_retained_in_result": False,
            "overlay_text_retained_in_result": False,
        },
        "service_starts": 0,
        "configuration_mutations": 0,
        "storage_format_or_partition_mutations": 0,
        "network_or_ap_mutations": 0,
    }
    failures: list[str] = []
    work: Path | None = None
    try:
        if not hasattr(os, "geteuid") or os.geteuid() != 0:
            raise HarnessError("exact-Pi benchmark requires root")
        if (
            SHA256_RE.fullmatch(arguments.expected_script_sha256) is None
            or arguments.expected_script_sha256 != _script_sha256()
        ):
            raise HarnessError("reviewed standalone script hash differs")
        evidence["script_sha256"] = arguments.expected_script_sha256
        evidence["board"] = _board_identity(arguments.expected_board_serial)
        evidence["release"] = _release_identity(arguments.expected_release)
        evidence["storage"] = _storage_identity(arguments.expected_storage_uuid)
        evidence["recorder_before"] = _require_inactive(SERVICE)
        evidence["ap_before"] = _require_inactive(AP_SERVICE)
        evidence["throttle_before"] = _throttle()
        if evidence["throttle_before"] != "throttled=0x0":
            raise HarnessError("Pi was throttled before benchmark")
        if (
            not QUARANTINE_ROOT.is_dir()
            or QUARANTINE_ROOT.is_symlink()
            or os.stat(QUARANTINE_ROOT).st_dev != os.stat(RECORDING_ROOT).st_dev
        ):
            raise HarnessError("exact exFAT quarantine root is unavailable")
        work = Path(tempfile.mkdtemp(prefix="m9-gdkpixbuf-", dir=QUARANTINE_ROOT))
        if (
            work.parent != QUARANTINE_ROOT
            or work.is_symlink()
            or os.stat(work).st_dev != os.stat(RECORDING_ROOT).st_dev
        ):
            raise HarnessError("candidate workspace escaped exact exFAT quarantine")
        run = _run_pipeline(
            work, duration_s=arguments.duration_s, dynamic_updates=arguments.dynamic_pixbuf_updates
        )
        media = cast(Path, run.pop("media"))
        run["media_validation"] = _validate_media(media)
        run["sustained_minimum_fps"] = cast(float, run["measured_fps"]) >= arguments.minimum_fps
        run["zero_estimated_drops"] = run["estimated_drops"] == 0
        run["dynamic_update_contract"] = (not arguments.dynamic_pixbuf_updates) or cast(
            int, run["dynamic_pixbuf_updates"]
        ) >= 1
        if (
            not run["sustained_minimum_fps"]
            or not run["zero_estimated_drops"]
            or not run["dynamic_update_contract"]
        ):
            raise HarnessError("candidate missed the declared 1080p30 frame/drop/update gate")
        evidence["benchmark"] = run
    except BaseException as error:
        failures.append(_detail(f"{type(error).__name__}: {error}"))
    finally:
        if work is not None:
            try:
                for path in work.iterdir():
                    if path.is_symlink() or not path.is_file():
                        raise HarnessError("refusing to remove foreign candidate workspace member")
                    path.unlink()
                work.rmdir()
            except BaseException as error:
                failures.append(_detail(f"candidate workspace cleanup failed: {error}"))
        try:
            evidence["recorder_final"] = _require_inactive(SERVICE)
            evidence["ap_final"] = _require_inactive(AP_SERVICE)
            evidence["throttle_after"] = _throttle()
            if evidence["throttle_after"] != "throttled=0x0":
                failures.append("Pi throttled during candidate benchmark")
        except BaseException as error:
            failures.append(_detail(f"final environment check failed: {error}"))
    evidence["failures"] = failures
    evidence["passed"] = not failures
    return evidence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="milestone9-gdkpixbufoverlay")
    parser.add_argument("--expected-script-sha256", required=True)
    parser.add_argument("--expected-release", required=True)
    parser.add_argument("--expected-board-serial", required=True)
    parser.add_argument("--expected-storage-uuid", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration-s", type=float, default=DEFAULT_DURATION_S)
    parser.add_argument("--minimum-fps", type=float, default=DEFAULT_MINIMUM_FPS)
    parser.add_argument("--dynamic-pixbuf-updates", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if (
        not 5.0 <= arguments.duration_s <= MAX_DURATION_S
        or not 1.0 <= arguments.minimum_fps <= 30.0
    ):
        print(
            "milestone9-gdkpixbufoverlay refused: duration/fps bounds are invalid", file=sys.stderr
        )
        return 2
    try:
        with _qualification_lock():
            result = qualify(arguments)
        digest = _write_result(arguments.output, result)
    except BaseException as error:
        print(f"milestone9-gdkpixbufoverlay refused: {_detail(error)}", file=sys.stderr)
        return 2
    print(json.dumps({"passed": result["passed"], "result_sha256": digest}, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
