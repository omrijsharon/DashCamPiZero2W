#!/usr/bin/env python3
"""Hash-closed exact-Pi qualification of production audio restoration.

The ordinary ``qualify`` command performs two logical sysfs cycles.  The
owner-assisted ``qualify-physical`` command performs one real cable-unplug and
replug cycle without ever writing a USB ``authorized`` attribute.  Neither
command claims wrong-device evidence.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Final, cast

import dashcam
from dashcam.audio.alsa import AlsaIdentity, AlsaSelector, parse_alsa_selector
from dashcam.audio.linux import AudioDiscoveryOutcome, AudioDiscoveryStatus, discover_capture_device
from dashcam.config import load_config
from dashcam.diagnostics.media import CommandResult, run_fixed_argv
from dashcam.metadata.reconcile import parse_sidecar_bytes
from dashcam.metadata.schema import ClipSidecar
from dashcam.recorder.runtime import (
    GStreamerRecorderRuntime,
    RuntimeLifecycleEvent,
    build_production_runtime,
)

CONFIG_PATH: Final = Path("/etc/dashcam/config.toml")
IDENTITY_PATH: Final = Path("/etc/dashcam/storage-volume.env")
RECORDING_ROOT: Final = Path("/srv/dashcam")
CLIPS_ROOT: Final = RECORDING_ROOT / "clips"
SYS_DEVICES_ROOT: Final = Path("/sys/devices")
SYSTEMCTL: Final = "/usr/bin/systemctl"
UDEVADM: Final = "/usr/bin/udevadm"
VCGENCMD: Final = "/usr/bin/vcgencmd"
FFPROBE: Final = "/usr/bin/ffprobe"
FFMPEG: Final = "/usr/bin/ffmpeg"
MANIFEST_MEMBERS: Final = ("README.md", "run.py")
SHA256_RE: Final = re.compile(r"[0-9a-f]{64}")
UDEV_PATH_RE: Final = re.compile(r"/devices/[A-Za-z0-9_.:/-]{1,1000}")
MAX_MANIFEST_BYTES: Final = 4096
MAX_RESULT_BYTES: Final = 4 * 1024 * 1024
MAX_COMMAND_OUTPUT_BYTES: Final = 256 * 1024
MAX_IDR_OUTPUT_BYTES: Final = 8 * 1024 * 1024
MAX_SIDECAR_BYTES: Final = 1024 * 1024
MAX_CLIP_ENTRIES: Final = 4096
MAX_NEW_PAIRS: Final = 16
MAX_SNAPSHOTS: Final = 256
MAX_CYCLES: Final = 2
MAX_PHYSICAL_CYCLES: Final = 1
EDGE_BOUND_NS: Final = 100_000_000
INITIAL_TIMEOUT: Final = 20.0
LOSS_TIMEOUT: Final = 20.0
VIDEO_PROGRESS_TIMEOUT: Final = 12.0
REAUTHORIZE_TIMEOUT: Final = 5.0
REDISCOVERY_TIMEOUT: Final = 15.0
RESTORE_TIMEOUT: Final = 25.0
RESTORED_PROGRESS_TIMEOUT: Final = 12.0
CYCLE_TIMEOUT: Final = 90.0
CLEANUP_TIMEOUT: Final = 30.0
DEFAULT_OWNER_ACTION_TIMEOUT: Final = 1800.0
MIN_OWNER_ACTION_TIMEOUT: Final = 30.0
MAX_OWNER_ACTION_TIMEOUT: Final = 1800.0
MAX_LIFECYCLE_EVENTS: Final = 32
OWNER_UNPLUG_MARKER: Final = "OWNER_ACTION_REQUIRED: UNPLUG_MICROPHONE"
OWNER_RECONNECT_MARKER: Final = "OWNER_ACTION_REQUIRED: RECONNECT_MICROPHONE"
EXPECTED_REQUEST_PAD_COUNTS: Final = {
    "video_tee": 4,
    "audio_tee": 1,
    "splitmux_video": 3,
    "splitmux_audio": 1,
}


class HarnessError(RuntimeError):
    """Qualification evidence is malformed, missing, or refused."""


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
    digest = hashlib.sha256()
    size = 0
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


def _write_result(path: Path, value: Mapping[str, object]) -> None:
    if not path.is_absolute() or path == RECORDING_ROOT or RECORDING_ROOT in path.parents:
        raise HarnessError("evidence output must be an absolute rootfs path")
    if not path.parent.is_dir():
        raise HarnessError("evidence output parent must already exist")
    parent = path.parent.resolve(strict=True)
    if path.parent != parent or path.exists() or path.is_symlink() or not _is_rootfs_parent(parent):
        raise HarnessError("evidence output must be a new direct file")
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(payload) > MAX_RESULT_BYTES:
        raise HarnessError("evidence JSON exceeded its bound")
    descriptor, temporary = tempfile.mkstemp(prefix=".m7-production-restoration-", dir=parent)
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
        raise HarnessError("interpreter and dashcam package are not one installed release")
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
    result = run_fixed_argv((VCGENCMD, "get_throttled"), timeout_seconds=5.0, max_output_bytes=1024)
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
            vendor = _regular_bytes(fields[0], 32).decode("ascii").strip().casefold()
            product_id = _regular_bytes(fields[1], 32).decode("ascii").strip().casefold()
            product = "_".join(
                _regular_bytes(fields[2], 256).decode("utf-8", "strict").strip().split()
            )
            if (vendor, product_id, product) == (
                identity.vendor_id,
                identity.product_id,
                identity.product,
            ):
                candidates.append(current)
        if current == root:
            break
        current = current.parent
    if len(candidates) != 1:
        raise HarnessError("udev ancestry did not identify one exact USB authorization node")
    return candidates[0] / "authorized"


def resolve_usb_authorization_path(
    udev_device_path: str,
    identity: AlsaIdentity,
    *,
    sys_devices_root: Path = SYS_DEVICES_ROOT,
) -> Path:
    """Compatibility-facing exact sysfs resolver used by focused validation."""

    return _usb_authorized_path(udev_device_path, identity, root=sys_devices_root)


def _is_rootfs_parent(parent: Path) -> bool:
    """Only rootfs result directories are allowed; evidence never uses exFAT."""

    try:
        info = os.lstat(parent)
        recording = os.lstat(RECORDING_ROOT)
    except OSError:
        return False
    return (
        stat.S_ISDIR(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and info.st_dev != recording.st_dev
    )


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
    if (
        value not in (0, 1)
        or expected not in (0, 1)
        or value == expected
        or read_authorized(path) != expected
    ):
        raise HarnessError("USB authorization transition precondition differs")
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
            return {
                "path": str(path),
                "from": 1,
                "to": 1,
                "confirmed": True,
                "write_required": False,
                "monotonic_ns": time.monotonic_ns(),
            }
    except (HarnessError, OSError):
        pass
    _write_authorized_byte(path, 1)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            if read_authorized(path) == 1:
                return {
                    "path": str(path),
                    "from": 0,
                    "to": 1,
                    "confirmed": True,
                    "write_required": True,
                    "monotonic_ns": time.monotonic_ns(),
                }
        except (HarnessError, OSError):
            pass
        time.sleep(0.05)
    raise HarnessError("USB authorization restoration readback timed out")


def _clip_names() -> set[str]:
    root, clips = os.lstat(RECORDING_ROOT), os.lstat(CLIPS_ROOT)
    if (
        not stat.S_ISDIR(root.st_mode)
        or not stat.S_ISDIR(clips.st_mode)
        or root.st_dev != clips.st_dev
    ):
        raise HarnessError("clips directory left recording device")
    names: set[str] = set()
    with os.scandir(CLIPS_ROOT) as entries:
        for item in entries:
            if (
                len(names) >= MAX_CLIP_ENTRIES
                or not item.name.isascii()
                or not item.name.isprintable()
                or not item.name
                or len(item.name) > 255
            ):
                raise HarnessError("clips directory has unsafe/bounded name set")
            names.add(item.name)
    return names


def _strict_json(data: bytes, label: str) -> Mapping[str, object]:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise HarnessError(f"{label} contains duplicate keys")
            result[key] = value
        return result

    try:
        document = json.loads(data, object_pairs_hook=unique)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HarnessError(f"{label} is invalid JSON") from error
    if not isinstance(document, Mapping):
        raise HarnessError(f"{label} is not an object")
    return cast(Mapping[str, object], document)


def _command(result: CommandResult, label: str) -> bytes:
    if result.returncode or result.timed_out or result.output_truncated or result.stderr:
        raise HarnessError(f"{label} failed: {_detail(result.stderr)}")
    return result.stdout


def _probe(path: Path) -> Mapping[str, object]:
    result = run_fixed_argv(
        (
            FFPROBE,
            "-v",
            "error",
            "-show_entries",
            "stream=index,codec_type,codec_name,profile,width,height,r_frame_rate,sample_rate,channels,start_time,duration,bit_rate:format=duration,size",
            "-of",
            "json",
            str(path.resolve(strict=True)),
        ),
        timeout_seconds=15.0,
        max_output_bytes=MAX_COMMAND_OUTPUT_BYTES,
    )
    return _strict_json(_command(result, f"ffprobe {path.name}"), path.name)


def _contains_idr(data: str) -> bool:
    words = [
        word
        for line in data.splitlines()
        for word in (line.split(":", 1)[1] if ":" in line else line).split()
        if re.fullmatch(r"[0-9A-Fa-f]{2,8}", word)
    ]
    try:
        raw = bytes.fromhex("".join(words))
    except ValueError:
        return False
    for marker in (b"\x00\x00\x01", b"\x00\x00\x00\x01"):
        offset = 0
        while (index := raw.find(marker, offset)) >= 0:
            start = index + len(marker)
            if start < len(raw) and raw[start] & 0x1F == 5:
                return True
            offset = start
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


def _first_idr(path: Path) -> None:
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
            str(path.resolve(strict=True)),
        ),
        timeout_seconds=15.0,
        max_output_bytes=MAX_IDR_OUTPUT_BYTES,
    )
    packets = _strict_json(_command(result, f"IDR {path.name}"), path.name).get("packets")
    if (
        not isinstance(packets, Sequence)
        or isinstance(packets, str | bytes)
        or len(packets) != 1
        or not isinstance(packets[0], Mapping)
    ):
        raise HarnessError("IDR packet schema differs")
    packet = packets[0]
    if (
        set(packet) != {"codec_type", "flags", "data"}
        or packet.get("codec_type") != "video"
        or "K" not in str(packet.get("flags"))
        or not isinstance(packet.get("data"), str)
        or not _contains_idr(cast(str, packet["data"]))
    ):
        raise HarnessError("first video packet is not H.264 NAL type 5")


def _decode(path: Path, audio: bool) -> None:
    arguments = [
        FFMPEG,
        "-v",
        "error",
        "-xerror",
        "-c:v",
        "h264_v4l2m2m",
        "-i",
        str(path.resolve(strict=True)),
        "-map",
        "0:v:0",
    ]
    if audio:
        arguments.extend(("-map", "0:a:0"))
    arguments.extend(("-f", "null", "-"))
    _command(
        run_fixed_argv(
            tuple(arguments), timeout_seconds=30.0, max_output_bytes=MAX_COMMAND_OUTPUT_BYTES
        ),
        f"hardware decode {path.name}",
    )


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise HarnessError(f"{name} is not numeric")
    try:
        number = float(value)
    except ValueError as error:
        raise HarnessError(f"{name} is not numeric") from error
    if number < 0:
        raise HarnessError(f"{name} is negative")
    return number


def _subsequence(values: Sequence[bool], expected: Sequence[bool]) -> bool:
    position = 0
    for value in values:
        if position < len(expected) and value is expected[position]:
            position += 1
    return position == len(expected)


def _subsequence_indexes(
    values: Sequence[bool], expected: Sequence[bool]
) -> tuple[int, ...] | None:
    indexes: list[int] = []
    position = 0
    for index, value in enumerate(values):
        if position < len(expected) and value is expected[position]:
            indexes.append(index)
            position += 1
    return tuple(indexes) if position == len(expected) else None


def collect_media(
    before: set[str],
    after: set[str],
    *,
    expected_audio_states: Sequence[bool] = (True, False, True, False, True),
    minimum_pairs: int = 5,
) -> dict[str, object]:
    """Collect every measured pair first; semantic media refusals are returned."""
    new_names = after - before
    metadata = sorted(name for name in new_names if name.endswith(".json"))
    if not minimum_pairs <= len(metadata) <= MAX_NEW_PAIRS:
        raise HarnessError(
            f"qualification did not finalize {minimum_pairs}-{MAX_NEW_PAIRS} new pairs"
        )
    pairs: list[dict[str, object]] = []
    expected_names: set[str] = set()
    previous = -1
    structural_failure: str | None = None
    for name in metadata:
        sidecar = parse_sidecar_bytes(_regular_bytes(CLIPS_ROOT / name, MAX_SIDECAR_BYTES))
        if (
            not isinstance(sidecar, ClipSidecar)
            or sidecar.metadata_file != name
            or sidecar.sequence <= previous
        ):
            raise HarnessError("sidecar ordering/identity differs")
        previous = sidecar.sequence
        video = CLIPS_ROOT / sidecar.video_file
        for member in (CLIPS_ROOT / name, video):
            info = os.lstat(member)
            if not stat.S_ISREG(info.st_mode) or info.st_dev != os.lstat(RECORDING_ROOT).st_dev:
                raise HarnessError("media escaped recording storage")
        expected_names.update((name, sidecar.video_file))
        document = _probe(video)
        streams = document.get("streams")
        if (
            not isinstance(streams, Sequence)
            or isinstance(streams, str | bytes)
            or not all(isinstance(item, Mapping) for item in streams)
        ):
            raise HarnessError("ffprobe stream schema differs")
        typed = cast(Sequence[Mapping[str, object]], streams)
        videos = [item for item in typed if item.get("codec_type") == "video"]
        audios = [item for item in typed if item.get("codec_type") == "audio"]
        available = sidecar.audio.available
        if len(videos) != 1 or len(audios) != int(available):
            raise HarnessError("stream set differs from sidecar truth")
        video_stream = videos[0]
        if (
            video_stream.get("codec_name") != "h264"
            or video_stream.get("profile") != "High"
            or video_stream.get("width") != 1920
            or video_stream.get("height") != 1080
            or video_stream.get("r_frame_rate") != "30/1"
        ):
            raise HarnessError("video stream contract differs")
        pair: dict[str, object] = {
            "sequence": sidecar.sequence,
            "video_file": sidecar.video_file,
            "metadata_file": name,
            "audio_available": available,
            "video_sha256": _sha256(video, 512 * 1024 * 1024),
            "metadata_sha256": _sha256(CLIPS_ROOT / name, MAX_SIDECAR_BYTES),
            "idr": False,
            "hardware_decode": False,
            "av_stream_edge_skew_ns": None,
        }
        if available:
            audio = audios[0]
            if (
                audio.get("codec_name") != "aac"
                or audio.get("profile") != "LC"
                or str(audio.get("sample_rate")) != "48000"
                or audio.get("channels") != 1
            ):
                raise HarnessError("A/V audio stream contract differs")
            video_start, video_duration = (
                _number(video_stream.get("start_time"), "video start"),
                _number(video_stream.get("duration"), "video duration"),
            )
            audio_start, audio_duration = (
                _number(audio.get("start_time"), "audio start"),
                _number(audio.get("duration"), "audio duration"),
            )
            pair["av_stream_edge_skew_ns"] = round(
                max(
                    abs(video_start - audio_start),
                    abs((video_start + video_duration) - (audio_start + audio_duration)),
                )
                * 1_000_000_000
            )
        _first_idr(video)
        pair["idr"] = True
        _decode(video, available)
        pair["hardware_decode"] = True
        pairs.append(pair)
    if new_names != expected_names:
        raise HarnessError("new output is not exact complete MP4+JSON pairs")
    audio_states = [cast(bool, pair["audio_available"]) for pair in pairs]
    for pair in pairs:
        skew = pair["av_stream_edge_skew_ns"]
        if isinstance(skew, int) and skew >= EDGE_BOUND_NS:
            structural_failure = "A/V stream-edge skew reached 100 ms bound"
    restored = [
        cast(int, pair["av_stream_edge_skew_ns"])
        for pair in pairs
        if pair["av_stream_edge_skew_ns"] is not None
    ]
    if len(restored) >= 3 and restored[-1] > restored[-2] + 25_000_000:
        structural_failure = structural_failure or "restored A/V skew growth exceeded 25 ms"
    qualifying_indexes = _subsequence_indexes(audio_states, expected_audio_states)
    if qualifying_indexes is None:
        cycle_label = (
            "two A/V-video-A/V cycles"
            if tuple(expected_audio_states) == (True, False, True, False, True)
            else "one physical A/V-video-A/V cycle"
        )
        structural_failure = structural_failure or f"media lacks {cycle_label}"
    return {
        "pair_count": len(pairs),
        "audio_states": audio_states,
        "pairs": pairs,
        "qualifying_subsequence": None
        if qualifying_indexes is None
        else {
            "indexes": list(qualifying_indexes),
            "sequences": [cast(int, pairs[index]["sequence"]) for index in qualifying_indexes],
            "audio_states": list(expected_audio_states),
        },
        "passed": structural_failure is None,
        "failure": structural_failure,
    }


def validate_new_media(before: set[str], after: set[str]) -> dict[str, object]:
    """Strict compatibility wrapper; use :func:`collect_media` to retain failures."""

    result = collect_media(before, after)
    if result["passed"] is not True:
        raise HarnessError(cast(str, result["failure"]))
    pairs = cast(Sequence[Mapping[str, object]], result["pairs"])
    return {
        **result,
        "restored_skew_seconds": tuple(
            cast(int, pair["av_stream_edge_skew_ns"]) / 1_000_000_000
            for pair in pairs
            if pair["av_stream_edge_skew_ns"] is not None
        ),
    }


def _topology(snapshot: Mapping[str, object]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Validate the public fixed-slot ownership snapshot without driver reach-through."""

    audio = snapshot.get("audio")
    if not isinstance(audio, Mapping) or not isinstance(audio.get("restoration"), Mapping):
        raise HarnessError("measured restoration topology is missing")
    restoration = cast(Mapping[str, object], audio["restoration"])
    slots = restoration.get("slot_activations")
    routes = restoration.get("tee_pad_routes")
    ingress = restoration.get("audio_ingress")
    if (
        restoration.get("restoration_enabled") is not True
        or restoration.get("slot_count") != 3
        or not isinstance(slots, Mapping)
        or set(slots) != {"1", "2", "3"}
        or restoration.get("request_pad_invariant") != "constant_preallocated"
        or restoration.get("request_pad_counts_measured") is not True
        or restoration.get("request_pad_peer_ownership_proven") is not True
        or not isinstance(routes, Mapping)
        or set(routes)
        != {
            "video_active_linked",
            "video_standby_unlinked",
            "video_continuity_linked",
            "audio_active_linked",
            "audio_standby_unlinked",
        }
        or not all(value is True for value in routes.values())
        or not isinstance(ingress, Mapping)
        or set(ingress)
        != {
            "current_count",
            "current_descendant_count",
            "stale_descendant_count",
            "replacement_count",
        }
        or ingress.get("current_count") != 1
        or ingress.get("current_descendant_count") != 1
        or ingress.get("stale_descendant_count") != 0
        or not isinstance(ingress.get("replacement_count"), int)
        or cast(int, ingress["replacement_count"]) > 2
    ):
        raise HarnessError("measured restoration topology differs")
    if restoration.get("request_pad_counts") != EXPECTED_REQUEST_PAD_COUNTS:
        raise HarnessError("constant request-pad counts differ")
    values = tuple(
        _integer(slots[str(number)], f"slot {number}", minimum=0) for number in (1, 2, 3)
    )
    active_slot = _integer(restoration.get("active_slot_id"), "active slot", minimum=1, maximum=3)
    active_activation = _integer(
        restoration.get("active_activation_id"), "active activation", minimum=1
    )
    if values[active_slot - 1] != active_activation or len(
        {value for value in values if value}
    ) != len([value for value in values if value]):
        raise HarnessError("slot sequence/activation ownership differs")
    return (1, 2, 3), values


def _lifecycle_event_record(event: RuntimeLifecycleEvent) -> dict[str, object]:
    """Capture public lifecycle facts without retaining unsafe diagnostics."""

    if not isinstance(event, RuntimeLifecycleEvent):
        raise HarnessError("lifecycle observer received an invalid event")
    detail = event.detail or ""
    encoded = detail.encode("utf-8", "replace")
    unsafe = any(marker in detail for marker in ("/", "\\", "password", "token", "secret"))
    return {
        "kind": event.kind.value,
        "pipeline_restart_count": event.restart_count,
        "recovery_attempt": event.recovery_attempt,
        "detail": None if unsafe else detail[:256],
        "detail_redacted": unsafe,
        "detail_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _capture_lifecycle_event(
    records: list[dict[str, object]], event: RuntimeLifecycleEvent
) -> None:
    if len(records) >= MAX_LIFECYCLE_EVENTS:
        raise HarnessError("lifecycle evidence exceeded its bound")
    records.append(_lifecycle_event_record(event))


def _integer(value: object, name: str, *, minimum: int = 0, maximum: int = 2**64 - 2) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise HarnessError(f"{name} has invalid integer shape")
    return value


def _forced_idr(value: object, cycle: int) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise HarnessError("loss proof omits forced_idr")
    required = {
        "request_count",
        "request_seqnum",
        "downstream_seqnum",
        "seqnum_preserved",
        "all_headers",
        "nal5",
        "request_monotonic_ns",
        "downstream_event_monotonic_ns",
        "idr_arrival_monotonic_ns",
        "downstream_running_time_ns",
        "forced_idr_running_time_ns",
        "event_to_idr_media_ns",
        "request_to_downstream_ns",
        "downstream_to_idr_ns",
        "request_to_idr_ns",
        "last_audio_end_running_time_ns",
        "edge_skew_ns",
        "edge_bound_ns",
    }
    if set(value) != required:
        raise HarnessError("forced_idr field set differs")
    request = _integer(value["request_count"], "request_count", minimum=1, maximum=2**32 - 1)
    if request != cycle:
        raise HarnessError("forced_idr request_count differs from cycle")
    request_seq = _integer(value["request_seqnum"], "request_seqnum", maximum=2**32 - 1)
    downstream_seq = _integer(value["downstream_seqnum"], "downstream_seqnum", maximum=2**32 - 1)
    if (
        not isinstance(value["seqnum_preserved"], bool)
        or value["seqnum_preserved"] is not (request_seq == downstream_seq)
        or value["all_headers"] is not True
        or value["nal5"] is not True
    ):
        raise HarnessError("forced_idr boolean/seqnum proof differs")
    request_ns = _integer(value["request_monotonic_ns"], "request monotonic")
    downstream_ns = _integer(value["downstream_event_monotonic_ns"], "downstream monotonic")
    idr_ns = _integer(value["idr_arrival_monotonic_ns"], "IDR monotonic")
    downstream_rt = _integer(value["downstream_running_time_ns"], "downstream running")
    forced_rt = _integer(value["forced_idr_running_time_ns"], "forced running")
    last_audio = _integer(value["last_audio_end_running_time_ns"], "last audio end")
    if downstream_ns < request_ns or idr_ns < downstream_ns:
        raise HarnessError("forced_idr monotonic order differs")
    checks = (
        ("request_to_downstream_ns", downstream_ns - request_ns),
        ("downstream_to_idr_ns", idr_ns - downstream_ns),
        ("request_to_idr_ns", idr_ns - request_ns),
        ("event_to_idr_media_ns", forced_rt - downstream_rt),
        ("edge_skew_ns", forced_rt - last_audio),
    )
    for name, expected in checks:
        if _integer(value[name], name) != expected:
            raise HarnessError(f"forced_idr {name} identity differs")
    if (
        _integer(value["edge_bound_ns"], "edge bound") != EDGE_BOUND_NS
        or not 0 <= cast(int, value["edge_skew_ns"]) < EDGE_BOUND_NS
    ):
        raise HarnessError("forced_idr audio edge bound differs")
    return value


def _handoff_proof(value: object, cycle: int, *, restore: bool) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise HarnessError("handoff proof is missing")
    common = {
        "retired_generation_id",
        "active_generation_id",
        "boundary_running_time_ns",
        "retired_slot_id",
        "active_slot_id",
        "camera_identity_unchanged",
        "encoder_identity_unchanged",
        "successor_first_buffer_is_idr",
        "successor_sticky_events_present",
        "successor_observed_video_buffers",
        "successor_state_converged",
        "retired_fragment_closed",
        "request_pads_constant",
    }
    expected = common | (
        {"successor_observed_audio_units", "fixed_slot_count", "retired_slot_recycled"}
        if restore
        else {"forced_idr"}
    )
    if set(value) != expected:
        raise HarnessError("handoff proof field set differs")
    retired, active = (
        _integer(value["retired_generation_id"], "retired activation", minimum=1),
        _integer(value["active_generation_id"], "active activation", minimum=2),
    )
    if active <= retired or _integer(
        value["retired_slot_id"], "retired slot", minimum=1, maximum=3
    ) == _integer(value["active_slot_id"], "active slot", minimum=1, maximum=3):
        raise HarnessError("handoff activation/slot relationship differs")
    for name in (
        "camera_identity_unchanged",
        "encoder_identity_unchanged",
        "successor_first_buffer_is_idr",
        "successor_sticky_events_present",
        "successor_state_converged",
        "retired_fragment_closed",
        "request_pads_constant",
    ):
        if value[name] is not True:
            raise HarnessError(f"handoff {name} differs")
    if _integer(value["successor_observed_video_buffers"], "successor video") < 31:
        raise HarnessError("successor video proof is insufficient")
    boundary = _integer(value["boundary_running_time_ns"], "handoff boundary")
    if restore:
        if (
            _integer(value["successor_observed_audio_units"], "successor audio", minimum=1) < 1
            or value["retired_slot_recycled"] is not True
            or _integer(value["fixed_slot_count"], "fixed slots") != 3
            or value["active_slot_id"] != 1
            or value["retired_slot_id"] not in (2, 3)
        ):
            raise HarnessError("restore proof differs")
    else:
        forced = _forced_idr(value["forced_idr"], cycle)
        if _integer(forced["forced_idr_running_time_ns"], "forced IDR running") != boundary:
            raise HarnessError("loss boundary does not equal forced IDR")
    return value


def _restoration(
    snapshot: Mapping[str, object], cycle: int, *, restored: bool
) -> Mapping[str, object]:
    audio = snapshot.get("audio")
    if not isinstance(audio, Mapping) or not isinstance(audio.get("restoration"), Mapping):
        raise HarnessError("runtime omits public audio restoration snapshot")
    state = cast(Mapping[str, object], audio["restoration"])
    required = {
        "restoration_enabled",
        "state",
        "retry_attempts",
        "retry_campaigns",
        "retry_in_flight",
        "stable_confirmations",
        "reason",
        "topology_observation",
        "topology_observation_stale",
        "topology_observed_monotonic_ns",
        "active_slot_id",
        "active_activation_id",
        "slot_count",
        "slot_activations",
        "request_pad_invariant",
        "request_pad_counts_measured",
        "request_pad_peer_ownership_proven",
        "request_pad_counts",
        "tee_pad_routes",
        "audio_ingress",
        "loss_count",
        "restoration_count",
        "matched_endpoint",
        "matched_identity",
        "last_loss_handoff",
        "last_restore_handoff",
        "loss_classification",
        "loss_observations",
        "last_failure",
    }
    if (
        set(state) != required
        or state["restoration_enabled"] is not True
        or state["topology_observation"] != "stable"
        or state["topology_observation_stale"] is not False
        or state["slot_count"] != 3
        or state["request_pad_invariant"] != "constant_preallocated"
        or state["request_pad_counts_measured"] is not True
        or state["request_pad_peer_ownership_proven"] is not True
    ):
        raise HarnessError("public restoration topology snapshot differs")
    counts = state["request_pad_counts"]
    if (
        not isinstance(counts, Mapping)
        or set(counts) != {"video_tee", "audio_tee", "splitmux_video", "splitmux_audio"}
        or tuple(_integer(counts[key], key) for key in sorted(counts)) != (1, 1, 3, 4)
    ):
        raise HarnessError("constant request-pad counts differ")
    if _integer(state["loss_count"], "loss count") < cycle:
        raise HarnessError("loss count differs")
    if restored:
        if (
            audio.get("state") != "MATCHED"
            or state["state"] != "active"
            or _integer(state["restoration_count"], "restore count") < cycle
        ):
            raise HarnessError("restored A/V snapshot differs")
        if cycle == 0:
            if (
                state["last_restore_handoff"] is not None
                or _integer(state["restoration_count"], "initial restore count") != 0
            ):
                raise HarnessError("initial A/V snapshot contains a restore handoff")
        else:
            _handoff_proof(state["last_restore_handoff"], cycle, restore=True)
    else:
        if audio.get("state") != "UNAVAILABLE" or audio.get("reason") != "microphone_loss_isolated":
            raise HarnessError("video-only loss snapshot differs")
        _handoff_proof(state["last_loss_handoff"], cycle, restore=False)
    return state


def _frames(snapshot: Mapping[str, object]) -> int:
    frames = snapshot.get("frames")
    return _integer(frames.get("encoded"), "encoded frames") if isinstance(frames, Mapping) else -1


def _drops(snapshot: Mapping[str, object]) -> int:
    frames = snapshot.get("frames")
    return _integer(frames.get("dropped"), "dropped frames") if isinstance(frames, Mapping) else -1


def _audio_state(snapshot: Mapping[str, object]) -> object:
    audio = snapshot.get("audio")
    return audio.get("state") if isinstance(audio, Mapping) else None


def _restoration_when_settled(
    snapshot: Mapping[str, object], cycle: int, *, restored: bool
) -> Mapping[str, object] | None:
    """Wait through only the exact public in-progress topology observation."""

    audio = snapshot.get("audio")
    state = audio.get("restoration") if isinstance(audio, Mapping) else None
    if (
        isinstance(state, Mapping)
        and state.get("topology_observation") == "handoff_in_progress"
        and state.get("topology_observation_stale") is True
    ):
        return None
    if (
        restored
        and isinstance(audio, Mapping)
        and audio.get("state") == "UNAVAILABLE"
        and isinstance(state, Mapping)
        and state.get("state") == "unavailable"
        and state.get("topology_observation") == "stable"
        and state.get("topology_observation_stale") is False
        and state.get("last_failure") is None
        and state.get("loss_count") == cycle
        and state.get("restoration_count") == cycle - 1
    ):
        # Reauthorization does not make ALSA discovery synchronous.  Keep
        # polling through the bounded unavailable/rediscovery states; only the
        # exact active proof below can satisfy restoration acceptance.
        return None
    if (
        restored
        and isinstance(audio, Mapping)
        and audio.get("state") == "UNAVAILABLE"
        and isinstance(state, Mapping)
        and state.get("state") == "restoring"
        and state.get("reason") == "restoring_at_boundary"
        and state.get("topology_observation") == "stable"
        and state.get("topology_observation_stale") is False
        and state.get("last_failure") is None
        and state.get("loss_count") == cycle
        and state.get("restoration_count") == cycle - 1
        and state.get("stable_confirmations") == 2
    ):
        # The coordinator publishes this bounded boundary transition before
        # the serialized driver call publishes handoff_in_progress.  It is
        # still in progress and can never satisfy restored-A/V acceptance.
        return None
    return _restoration(snapshot, cycle, restored=restored)


async def _wait(
    runtime: GStreamerRecorderRuntime,
    task: asyncio.Task[None],
    snapshots: list[dict[str, object]],
    predicate: Callable[[Mapping[str, object]], bool],
    *,
    timeout: float,
    phase: str,
) -> Mapping[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if task.done():
            await task
            raise HarnessError(f"runtime ended during {phase}")
        snapshot = runtime.runtime_snapshot()
        if len(snapshots) >= MAX_SNAPSHOTS:
            raise HarnessError("snapshot evidence exceeded bound")
        snapshots.append(
            {"phase": phase, "monotonic_ns": time.monotonic_ns(), "snapshot": snapshot}
        )
        if predicate(snapshot):
            return snapshot
        await asyncio.sleep(0.25)
    raise HarnessError(f"runtime snapshot wait timed out: {phase}")


def _owner_action_timeout(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("owner action timeout must be numeric") from error
    if not MIN_OWNER_ACTION_TIMEOUT <= seconds <= MAX_OWNER_ACTION_TIMEOUT:
        raise argparse.ArgumentTypeError(
            "owner action timeout must be between "
            f"{MIN_OWNER_ACTION_TIMEOUT:g} and {MAX_OWNER_ACTION_TIMEOUT:g} seconds"
        )
    return seconds


def _request_owner_action(marker: str) -> dict[str, object]:
    if marker not in (OWNER_UNPLUG_MARKER, OWNER_RECONNECT_MARKER):
        raise HarnessError("owner action marker is not recognized")
    requested = time.monotonic_ns()
    print(marker, flush=True)
    return {
        "marker": marker,
        "required_monotonic_ns": requested,
        "observed_monotonic_ns": None,
        "completed": False,
    }


async def _wait_for_physical_unplug(
    authorized: Path,
    task: asyncio.Task[None],
    *,
    timeout: float,
) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if task.done():
            await task
            raise HarnessError("runtime ended while awaiting physical microphone unplug")
        if not os.path.lexists(authorized):
            return time.monotonic_ns()
        await asyncio.sleep(0.25)
    raise HarnessError("owner physical microphone unplug timed out")


async def _wait_for_physical_reconnect(
    selector: AlsaSelector,
    initial_identity: AlsaIdentity,
    task: asyncio.Task[None],
    *,
    timeout: float,
) -> tuple[AudioDiscoveryOutcome, Path, int]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if task.done():
            await task
            raise HarnessError("runtime ended while awaiting physical microphone reconnect")
        discovery = await asyncio.to_thread(discover_capture_device, selector)
        if discovery.status is AudioDiscoveryStatus.MATCHED and discovery.device is not None:
            if not _same_identity(discovery.device.identity, initial_identity):
                raise HarnessError("reconnected microphone stable identity differs")
            control = Path("/dev/snd") / f"controlC{discovery.device.card_index}"
            try:
                authorized = await asyncio.to_thread(
                    _usb_authorized_path,
                    await asyncio.to_thread(_udev_path, control),
                    discovery.device.identity,
                )
                authorization = await asyncio.to_thread(read_authorized, authorized)
            except (HarnessError, OSError):
                await asyncio.sleep(0.25)
                continue
            if os.path.lexists(authorized) and authorization == 1:
                return discovery, authorized, time.monotonic_ns()
        await asyncio.sleep(0.25)
    raise HarnessError("owner physical microphone reconnect timed out")


async def _final_microphone_observation(
    selector: AlsaSelector,
    initial_identity: AlsaIdentity | None,
) -> dict[str, object]:
    discovery = await asyncio.to_thread(discover_capture_device, selector)
    observation: dict[str, object] = {
        "observed": True,
        "present": False,
        "matched": False,
        "same_stable_identity": False,
        "authorized": None,
    }
    if discovery.status is not AudioDiscoveryStatus.MATCHED or discovery.device is None:
        observation["discovery_status"] = discovery.status.value
        return observation
    observation.update(
        {
            "present": True,
            "matched": True,
            "identity": _identity(discovery.device.identity),
            "endpoint": discovery.device.capture_endpoint,
            "card_index": discovery.device.card_index,
            "same_stable_identity": initial_identity is not None
            and _same_identity(discovery.device.identity, initial_identity),
        }
    )
    control = Path("/dev/snd") / f"controlC{discovery.device.card_index}"
    try:
        authorized = await asyncio.to_thread(
            _usb_authorized_path,
            await asyncio.to_thread(_udev_path, control),
            discovery.device.identity,
        )
        observation["authorization_path"] = str(authorized)
        observation["authorization_path_exists"] = os.path.lexists(authorized)
        observation["authorized"] = await asyncio.to_thread(read_authorized, authorized)
    except (HarnessError, OSError) as error:
        observation["authorization_observation_failure"] = _detail(error)
    return observation


async def qualify() -> dict[str, object]:
    os.environ["DASHCAM_HANDOFF_TRACE"] = "1"
    evidence: dict[str, object] = {
        "schema_version": 1,
        "passed": False,
        "logical_sysfs_cycles_only": True,
        "physical_unplug_proven": False,
        "wrong_device_proven": False,
        "runtime_construction": {
            "factory": "build_production_runtime",
            "ordinary_defaults": True,
            "audio_loss_override_supplied": False,
            "audio_restoration_override_supplied": False,
        },
        "authorization_restored": False,
        "service_mutations": 0,
    }
    failures: list[str] = []
    runtime: GStreamerRecorderRuntime | None = None
    task: asyncio.Task[None] | None = None
    stop = asyncio.Event()
    authorized: Path | None = None
    runtime_started = False
    initial_identity: AlsaIdentity | None = None
    before_names: set[str] = set()
    before_unit: dict[str, object] | None = None
    snapshots: list[dict[str, object]] = []
    lifecycle_events: list[dict[str, object]] = []
    try:
        geteuid = getattr(os, "geteuid", None)
        if not callable(geteuid) or cast(Callable[[], int], geteuid)() != 0:
            raise HarnessError("qualification requires root for exact USB authorization")
        evidence["release"] = _release_identity()
        before_unit = _unit_state()
        evidence["unit_before"] = before_unit
        evidence["throttle_before"] = _throttle()
        if evidence["throttle_before"] != "throttled=0x0":
            raise HarnessError("Pi was throttled before qualification")
        config = load_config(CONFIG_PATH)
        selector = parse_alsa_selector(config.audio.device_match)
        discovery = discover_capture_device(selector)
        if discovery.status is not AudioDiscoveryStatus.MATCHED or discovery.device is None:
            raise HarnessError("configured microphone is not exactly matched")
        initial_identity = discovery.device.identity
        control = Path("/dev/snd") / f"controlC{discovery.device.card_index}"
        authorized = _usb_authorized_path(_udev_path(control), initial_identity)
        if read_authorized(authorized) != 1:
            raise HarnessError("exact microphone is not initially authorized")
        evidence["initial_microphone"] = {
            "identity": _identity(initial_identity),
            "endpoint": discovery.device.capture_endpoint,
            "card_index": discovery.device.card_index,
            "authorization_path": str(authorized),
        }
        before_names = _clip_names()
        runtime = build_production_runtime(
            config_path=CONFIG_PATH,
            identity_path=IDENTITY_PATH,
        )
        if not (await runtime.check(config)).ready:
            raise HarnessError("production storage preflight is not READY")
        runtime.bind_lifecycle_observer(
            lambda event: _capture_lifecycle_event(lifecycle_events, event)
        )
        await runtime.start(config)
        runtime_started = True
        task = asyncio.create_task(runtime.run(stop))
        initial = await _wait(
            runtime,
            task,
            snapshots,
            lambda item: (
                _audio_state(item) == "MATCHED"
                and _frames(item) >= 90
                and item.get("pipeline_restart_count") == 0
                and _drops(item) >= 0
                and _restoration(item, 0, restored=True) is not None
            ),
            timeout=20.0,
            phase="initial_av",
        )
        baseline = _drops(initial)
        evidence["initial_snapshot"] = initial
        evidence["drop_baseline"] = baseline
        cycles: list[dict[str, object]] = []
        previous_request = 0
        for cycle in range(1, MAX_CYCLES + 1):
            record: dict[str, object] = {"cycle": cycle}

            def loss_ready(item: Mapping[str, object], current_cycle: int = cycle) -> bool:
                return (
                    item.get("pipeline_restart_count") == 0
                    and _drops(item) == baseline
                    and _restoration_when_settled(item, current_cycle, restored=False) is not None
                )

            record["deauthorization"] = await asyncio.to_thread(
                write_authorized, authorized, 0, expected=1
            )
            loss = await _wait(
                runtime,
                task,
                snapshots,
                loss_ready,
                timeout=20.0,
                phase=f"cycle_{cycle}_video_only",
            )
            loss_state = _restoration(loss, cycle, restored=False)
            forced = cast(
                Mapping[str, object],
                cast(Mapping[str, object], loss_state["last_loss_handoff"])["forced_idr"],
            )
            request = _integer(forced["request_count"], "request count")
            if request <= previous_request:
                raise HarnessError("forced-IDR request counts are not distinct/increasing")
            previous_request = request
            record["loss_snapshot"] = loss
            record["loss_handoff"] = loss_state["last_loss_handoff"]
            start_frames = _frames(loss)

            def video_progress(
                item: Mapping[str, object],
                current_cycle: int = cycle,
                minimum_frames: int = start_frames + 180,
            ) -> bool:
                return (
                    _restoration_when_settled(item, current_cycle, restored=False) is not None
                    and _frames(item) >= minimum_frames
                    and _drops(item) == baseline
                )

            record["video_only_progress_snapshot"] = await _wait(
                runtime,
                task,
                snapshots,
                video_progress,
                timeout=12.0,
                phase=f"cycle_{cycle}_video_progress",
            )
            record["reauthorization"] = await asyncio.to_thread(restore_authorized, authorized)

            def restore_ready(item: Mapping[str, object], current_cycle: int = cycle) -> bool:
                return (
                    item.get("pipeline_restart_count") == 0
                    and _drops(item) == baseline
                    and _restoration_when_settled(item, current_cycle, restored=True) is not None
                )

            restored = await _wait(
                runtime,
                task,
                snapshots,
                restore_ready,
                timeout=25.0,
                phase=f"cycle_{cycle}_restored_av",
            )
            restore_state = _restoration(restored, cycle, restored=True)
            record["restored_snapshot"] = restored
            record["restore_handoff"] = restore_state["last_restore_handoff"]
            start_frames = _frames(restored)

            def restored_progress(
                item: Mapping[str, object],
                current_cycle: int = cycle,
                minimum_frames: int = start_frames + 180,
            ) -> bool:
                return (
                    _restoration_when_settled(item, current_cycle, restored=True) is not None
                    and _frames(item) >= minimum_frames
                    and _drops(item) == baseline
                )

            record["post_restore_snapshot"] = await _wait(
                runtime,
                task,
                snapshots,
                restored_progress,
                timeout=12.0,
                phase=f"cycle_{cycle}_restored_progress",
            )
            cycles.append(record)
        evidence["cycles"] = cycles
    except BaseException as error:
        failures.append(_detail(f"{type(error).__name__}: {error}"))
    finally:
        stop.set()
        if authorized is not None:
            try:
                evidence["reauthorization_finally"] = await asyncio.to_thread(
                    restore_authorized, authorized
                )
                evidence["authorization_restored"] = True
            except BaseException as error:
                failures.append(
                    _detail(f"authorization restore failed: {type(error).__name__}: {error}")
                )
        if runtime_started and runtime is not None:
            if task is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=30.0)
                except BaseException as error:
                    failures.append(
                        _detail(f"runtime cleanup failed: {type(error).__name__}: {error}")
                    )
            try:
                await asyncio.wait_for(runtime.stop(), timeout=30.0)
            except BaseException as error:
                failures.append(_detail(f"runtime stop failed: {type(error).__name__}: {error}"))
    evidence["snapshots"] = snapshots
    evidence["lifecycle_events"] = lifecycle_events
    try:
        after_unit = _unit_state()
        evidence["unit_after"] = after_unit
        if before_unit is None or after_unit != before_unit or after_unit["restarts"] != 0:
            raise HarnessError("dashcamd service state changed")
        evidence["throttle_after"] = _throttle()
        if (
            evidence["throttle_after"] != "throttled=0x0"
            or not evidence["authorization_restored"]
            or initial_identity is None
        ):
            raise HarnessError("post-run throttle/authorization proof failed")
        selector = parse_alsa_selector(load_config(CONFIG_PATH).audio.device_match)
        deadline = time.monotonic() + 15.0
        rediscovery: AudioDiscoveryOutcome | None = None
        while time.monotonic() < deadline:
            rediscovery = discover_capture_device(selector)
            if rediscovery.status is AudioDiscoveryStatus.MATCHED:
                break
            await asyncio.sleep(0.25)
        if (
            rediscovery is None
            or rediscovery.status is not AudioDiscoveryStatus.MATCHED
            or rediscovery.device is None
            or not _same_identity(rediscovery.device.identity, initial_identity)
        ):
            raise HarnessError("restored microphone stable identity did not rematch")
        evidence["final_microphone"] = {
            "identity": _identity(rediscovery.device.identity),
            "endpoint": rediscovery.device.capture_endpoint,
            "card_index": rediscovery.device.card_index,
            "card_index_changed_naturally": rediscovery.device.card_index
            != cast(Mapping[str, object], evidence["initial_microphone"])["card_index"],
        }
        media = collect_media(before_names, _clip_names())
        evidence["media"] = media
        if media["passed"] is not True:
            raise HarnessError(cast(str, media["failure"]))
    except BaseException as error:
        failures.append(_detail(f"{type(error).__name__}: {error}"))
    evidence["failures"] = failures
    evidence["passed"] = not failures
    return evidence


async def qualify_physical(owner_action_timeout: float) -> dict[str, object]:
    """Qualify one real USB cable-unplug/replug cycle without sysfs writes."""

    os.environ["DASHCAM_HANDOFF_TRACE"] = "1"
    evidence: dict[str, object] = {
        "schema_version": 1,
        "mode": "owner_assisted_physical_hotplug",
        "passed": False,
        "logical_sysfs_cycles_only": False,
        "physical_unplug_proven": False,
        "physical_reconnect_proven": False,
        "wrong_device_proven": False,
        "runtime_construction": {
            "factory": "build_production_runtime",
            "ordinary_defaults": True,
            "audio_loss_override_supplied": False,
            "audio_restoration_override_supplied": False,
        },
        "owner_action_timeout_seconds": owner_action_timeout,
        "owner_actions": [],
        "usb_authorization_writes": 0,
        "authorization_restored": False,
        "service_mutations": 0,
        "microphone_present_final": {
            "observed": False,
            "present": None,
        },
    }
    failures: list[str] = []
    runtime: GStreamerRecorderRuntime | None = None
    task: asyncio.Task[None] | None = None
    stop = asyncio.Event()
    runtime_started = False
    initial_identity: AlsaIdentity | None = None
    selector: AlsaSelector | None = None
    before_names: set[str] = set()
    before_unit: dict[str, object] | None = None
    snapshots: list[dict[str, object]] = []
    lifecycle_events: list[dict[str, object]] = []
    try:
        geteuid = getattr(os, "geteuid", None)
        if not callable(geteuid) or cast(Callable[[], int], geteuid)() != 0:
            raise HarnessError("physical qualification requires the reviewed root environment")
        evidence["release"] = _release_identity()
        before_unit = _unit_state()
        evidence["unit_before"] = before_unit
        evidence["throttle_before"] = _throttle()
        if evidence["throttle_before"] != "throttled=0x0":
            raise HarnessError("Pi was throttled before qualification")
        config = load_config(CONFIG_PATH)
        selector = parse_alsa_selector(config.audio.device_match)
        discovery = discover_capture_device(selector)
        if discovery.status is not AudioDiscoveryStatus.MATCHED or discovery.device is None:
            raise HarnessError("configured microphone is not exactly matched")
        initial_identity = discovery.device.identity
        control = Path("/dev/snd") / f"controlC{discovery.device.card_index}"
        authorized = _usb_authorized_path(_udev_path(control), initial_identity)
        if read_authorized(authorized) != 1:
            raise HarnessError("exact microphone is not initially authorized")
        evidence["initial_microphone"] = {
            "identity": _identity(initial_identity),
            "endpoint": discovery.device.capture_endpoint,
            "card_index": discovery.device.card_index,
            "authorization_path": str(authorized),
            "authorization_path_exists": os.path.lexists(authorized),
            "authorized": 1,
        }
        before_names = _clip_names()
        runtime = build_production_runtime(
            config_path=CONFIG_PATH,
            identity_path=IDENTITY_PATH,
        )
        if not (await runtime.check(config)).ready:
            raise HarnessError("production storage preflight is not READY")
        runtime.bind_lifecycle_observer(
            lambda event: _capture_lifecycle_event(lifecycle_events, event)
        )
        await runtime.start(config)
        runtime_started = True
        task = asyncio.create_task(runtime.run(stop))
        initial = await _wait(
            runtime,
            task,
            snapshots,
            lambda item: (
                _audio_state(item) == "MATCHED"
                and _frames(item) >= 90
                and item.get("pipeline_restart_count") == 0
                and _drops(item) >= 0
                and _restoration(item, 0, restored=True) is not None
            ),
            timeout=INITIAL_TIMEOUT,
            phase="initial_av",
        )
        baseline = _drops(initial)
        evidence["initial_snapshot"] = initial
        evidence["drop_baseline"] = baseline

        unplug_action = _request_owner_action(OWNER_UNPLUG_MARKER)
        cast(list[dict[str, object]], evidence["owner_actions"]).append(unplug_action)
        unplug_observed = await _wait_for_physical_unplug(
            authorized,
            task,
            timeout=owner_action_timeout,
        )
        unplug_action.update(
            {
                "observed_monotonic_ns": unplug_observed,
                "completed": True,
            }
        )
        if os.path.lexists(authorized):
            raise HarnessError("old USB authorization path still exists after physical unplug")
        evidence["physical_hardware_proof"] = {
            "old_authorization_path": str(authorized),
            "old_authorization_path_absent": True,
            "old_authorization_path_absent_monotonic_ns": unplug_observed,
        }

        def loss_ready(item: Mapping[str, object]) -> bool:
            return (
                item.get("pipeline_restart_count") == 0
                and _drops(item) == baseline
                and _restoration_when_settled(item, 1, restored=False) is not None
            )

        loss = await _wait(
            runtime,
            task,
            snapshots,
            loss_ready,
            timeout=LOSS_TIMEOUT,
            phase="physical_cycle_video_only",
        )
        loss_state = _restoration(loss, 1, restored=False)
        start_frames = _frames(loss)

        def video_progress(item: Mapping[str, object]) -> bool:
            return (
                _restoration_when_settled(item, 1, restored=False) is not None
                and _frames(item) >= start_frames + 180
                and _drops(item) == baseline
                and item.get("pipeline_restart_count") == 0
            )

        video_only_progress = await _wait(
            runtime,
            task,
            snapshots,
            video_progress,
            timeout=VIDEO_PROGRESS_TIMEOUT,
            phase="physical_cycle_video_progress",
        )
        evidence["physical_unplug_proven"] = True

        reconnect_action = _request_owner_action(OWNER_RECONNECT_MARKER)
        cast(list[dict[str, object]], evidence["owner_actions"]).append(reconnect_action)
        (
            rediscovery,
            reconnected_authorized,
            reconnect_observed,
        ) = await _wait_for_physical_reconnect(
            selector,
            initial_identity,
            task,
            timeout=owner_action_timeout,
        )
        reconnect_action.update(
            {
                "observed_monotonic_ns": reconnect_observed,
                "completed": True,
            }
        )
        assert rediscovery.device is not None
        evidence["reconnected_microphone"] = {
            "identity": _identity(rediscovery.device.identity),
            "endpoint": rediscovery.device.capture_endpoint,
            "card_index": rediscovery.device.card_index,
            "card_index_changed_naturally": rediscovery.device.card_index
            != discovery.device.card_index,
            "authorization_path": str(reconnected_authorized),
            "authorization_path_exists": os.path.lexists(reconnected_authorized),
            "authorized": read_authorized(reconnected_authorized),
            "same_stable_identity": _same_identity(rediscovery.device.identity, initial_identity),
            "resolved_after_reconnect": True,
            "observed_monotonic_ns": reconnect_observed,
        }

        def restore_ready(item: Mapping[str, object]) -> bool:
            return (
                item.get("pipeline_restart_count") == 0
                and _drops(item) == baseline
                and _restoration_when_settled(item, 1, restored=True) is not None
            )

        restored = await _wait(
            runtime,
            task,
            snapshots,
            restore_ready,
            timeout=RESTORE_TIMEOUT,
            phase="physical_cycle_restored_av",
        )
        restore_state = _restoration(restored, 1, restored=True)
        restored_start_frames = _frames(restored)

        def restored_progress(item: Mapping[str, object]) -> bool:
            return (
                _restoration_when_settled(item, 1, restored=True) is not None
                and _frames(item) >= restored_start_frames + 180
                and _drops(item) == baseline
                and item.get("pipeline_restart_count") == 0
            )

        post_restore = await _wait(
            runtime,
            task,
            snapshots,
            restored_progress,
            timeout=RESTORED_PROGRESS_TIMEOUT,
            phase="physical_cycle_restored_progress",
        )
        evidence["cycles"] = [
            {
                "cycle": 1,
                "physical": True,
                "loss_snapshot": loss,
                "loss_handoff": loss_state["last_loss_handoff"],
                "video_only_progress_snapshot": video_only_progress,
                "restored_snapshot": restored,
                "restore_handoff": restore_state["last_restore_handoff"],
                "post_restore_snapshot": post_restore,
            }
        ]
        if len(cast(Sequence[object], evidence["cycles"])) != MAX_PHYSICAL_CYCLES:
            raise HarnessError("physical qualification cycle bound differs")
        evidence["physical_reconnect_proven"] = True
    except BaseException as error:
        failures.append(_detail(f"{type(error).__name__}: {error}"))
    finally:
        stop.set()
        if runtime_started and runtime is not None:
            if task is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=CLEANUP_TIMEOUT)
                except BaseException as error:
                    failures.append(
                        _detail(f"runtime cleanup failed: {type(error).__name__}: {error}")
                    )
            try:
                await asyncio.wait_for(runtime.stop(), timeout=CLEANUP_TIMEOUT)
            except BaseException as error:
                failures.append(_detail(f"runtime stop failed: {type(error).__name__}: {error}"))
    evidence["snapshots"] = snapshots
    evidence["lifecycle_events"] = lifecycle_events
    try:
        after_unit = _unit_state()
        evidence["unit_after"] = after_unit
        if before_unit is None or after_unit != before_unit or after_unit["restarts"] != 0:
            raise HarnessError("dashcamd service state changed")
        if any(
            _integer(event.get("pipeline_restart_count"), "lifecycle restart count") != 0
            for event in lifecycle_events
        ):
            raise HarnessError("runtime lifecycle recorded a pipeline restart")
        evidence["throttle_after"] = _throttle()
        if evidence["throttle_after"] != "throttled=0x0":
            raise HarnessError("Pi was throttled during physical qualification")
        if selector is not None:
            final_microphone = await _final_microphone_observation(selector, initial_identity)
            evidence["microphone_present_final"] = final_microphone
            evidence["authorization_restored"] = (
                final_microphone.get("present") is True
                and final_microphone.get("matched") is True
                and final_microphone.get("same_stable_identity") is True
                and final_microphone.get("authorization_path_exists") is True
                and final_microphone.get("authorized") == 1
            )
        if (
            evidence["physical_reconnect_proven"] is not True
            or evidence["authorization_restored"] is not True
        ):
            raise HarnessError("physical microphone reconnect was not proven at cleanup")
        media = collect_media(
            before_names,
            _clip_names(),
            expected_audio_states=(True, False, True),
            minimum_pairs=3,
        )
        evidence["media"] = media
        if media["passed"] is not True:
            raise HarnessError(cast(str, media["failure"]))
        witness = media.get("qualifying_subsequence")
        if (
            not isinstance(witness, Mapping)
            or witness.get("audio_states") != [True, False, True]
            or not isinstance(witness.get("indexes"), Sequence)
            or len(cast(Sequence[object], witness["indexes"])) != 3
        ):
            raise HarnessError("physical media witness is not exact A/V-video-A/V")
    except BaseException as error:
        failures.append(_detail(f"{type(error).__name__}: {error}"))
    evidence["failures"] = failures
    evidence["passed"] = not failures
    return evidence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-manifest-sha256", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("qualify")
    command.add_argument("--output", type=Path, required=True)
    physical = commands.add_parser("qualify-physical")
    physical.add_argument("--output", type=Path, required=True)
    physical.add_argument(
        "--owner-action-timeout-seconds",
        type=_owner_action_timeout,
        default=DEFAULT_OWNER_ACTION_TIMEOUT,
        help=(
            "bounded wait for each physical owner action "
            f"({MIN_OWNER_ACTION_TIMEOUT:g}-{MAX_OWNER_ACTION_TIMEOUT:g} seconds)"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    started = time.monotonic_ns()
    verify_manifest(arguments.expected_manifest_sha256)
    try:
        if arguments.command == "qualify-physical":
            result = asyncio.run(qualify_physical(arguments.owner_action_timeout_seconds))
        else:
            result = asyncio.run(qualify())
    except BaseException as error:
        physical = arguments.command == "qualify-physical"
        result = {
            "schema_version": 1,
            "passed": False,
            "mode": "owner_assisted_physical_hotplug" if physical else "logical_sysfs",
            "logical_sysfs_cycles_only": not physical,
            "physical_unplug_proven": False,
            "physical_reconnect_proven": False,
            "wrong_device_proven": False,
            "authorization_restored": False,
            "usb_authorization_writes": 0 if physical else None,
            "microphone_present_final": {
                "observed": False,
                "present": None,
            },
            "failures": [_detail(f"{type(error).__name__}: {error}")],
        }
    document = {
        **result,
        "started_monotonic_ns": started,
        "ended_monotonic_ns": time.monotonic_ns(),
    }
    _write_result(arguments.output, document)
    return 0 if document["passed"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
