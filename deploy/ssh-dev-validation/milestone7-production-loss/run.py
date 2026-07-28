#!/usr/bin/env python3
"""Hash-closed exact-Pi qualification of production audio-loss isolation."""

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
from dashcam.audio.alsa import AlsaIdentity, parse_alsa_selector
from dashcam.audio.linux import (
    AudioDiscoveryOutcome,
    AudioDiscoveryStatus,
    discover_capture_device,
)
from dashcam.config import load_config
from dashcam.diagnostics.media import CommandResult, run_fixed_argv
from dashcam.metadata.reconcile import parse_sidecar_bytes
from dashcam.metadata.schema import ClipSidecar
from dashcam.recorder.runtime import GStreamerRecorderRuntime, build_production_runtime

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
MAX_NEW_PAIRS: Final = 8
MAX_SNAPSHOTS: Final = 128


class HarnessError(RuntimeError):
    """The qualification contract could not be proved."""


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
        if not stat.S_ISREG(info.st_mode):
            raise HarnessError(f"{path} is not a regular file")
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
        raise HarnessError("reviewed manifest hash differs from supplied hash")
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
        raise HarnessError("evidence output must be absolute")
    try:
        path.resolve(strict=False).relative_to(RECORDING_ROOT)
    except ValueError:
        pass
    else:
        raise HarnessError("evidence output must be outside recording storage")
    path.parent.mkdir(parents=True, exist_ok=True)
    parent = path.parent.resolve(strict=True)
    if parent != path.parent or path.exists() or path.is_symlink():
        raise HarnessError("evidence output must be one new direct file")
    encoded = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(encoded) > MAX_RESULT_BYTES:
        raise HarnessError("evidence JSON exceeded its bound")
    descriptor, temporary = tempfile.mkstemp(prefix=".m7-production-loss-", dir=parent)
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
            raise HarnessError("evidence output already exists") from error
        try:
            directory_descriptor = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            if os.name != "nt":
                raise
        else:
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
        raise HarnessError("interpreter and dashcam package are not one installed release")
    return {"release": parts[4], "venv": str(prefix), "package": str(package)}


def _unit_state() -> dict[str, object]:
    values: dict[str, str] = {}
    for name in ("ActiveState", "SubState", "MainPID", "NRestarts", "UnitFileState"):
        result = run_fixed_argv(
            (
                SYSTEMCTL,
                "show",
                "--no-pager",
                f"--property={name}",
                "--value",
                "dashcamd.service",
            ),
            timeout_seconds=5.0,
            max_output_bytes=1024,
        )
        if result.returncode != 0 or result.timed_out or result.output_truncated:
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
        "active": values["ActiveState"],
        "sub": values["SubState"],
        "main_pid": 0,
        "restarts": int(values["NRestarts"]),
        "unit_file_state": values["UnitFileState"],
    }


def _throttle() -> str:
    result = run_fixed_argv(
        (VCGENCMD, "get_throttled"),
        timeout_seconds=5.0,
        max_output_bytes=1024,
    )
    if result.returncode != 0 or result.timed_out or result.output_truncated:
        raise HarnessError("throttle query failed")
    value = result.stdout.decode("ascii", "strict").strip()
    if re.fullmatch(r"throttled=0x[0-9a-fA-F]+", value) is None:
        raise HarnessError("throttle query shape differs")
    return value


def _identity_dict(identity: AlsaIdentity) -> dict[str, object]:
    return {
        "vendor_id": identity.vendor_id,
        "product_id": identity.product_id,
        "product": identity.product,
        "physical_path": identity.physical_path,
        "serial": identity.serial,
        "alsa_card_id": identity.alsa_card_id,
    }


def _normal_product(value: str) -> str:
    return "_".join(value.strip().split())


def resolve_usb_authorization_path(
    udev_device_path: str,
    identity: AlsaIdentity,
    *,
    sys_devices_root: Path = SYS_DEVICES_ROOT,
) -> Path:
    """Resolve one real USB device parent from an exact udev device path."""

    if UDEV_PATH_RE.fullmatch(udev_device_path) is None:
        raise HarnessError("udev device path is unsafe")
    relative = Path(udev_device_path.removeprefix("/devices/"))
    root = sys_devices_root.resolve(strict=True)
    current = (root / relative).resolve(strict=True)
    if not current.is_relative_to(root):
        raise HarnessError("udev path escaped sysfs devices")
    candidates: list[Path] = []
    for _ in range(16):
        required = tuple(
            current / name for name in ("idVendor", "idProduct", "product", "authorized")
        )
        if all(path.exists() and not path.is_symlink() for path in required):
            vendor = _bounded_regular_bytes(required[0], 32).decode("ascii").strip().casefold()
            product_id = _bounded_regular_bytes(required[1], 32).decode("ascii").strip().casefold()
            product = _normal_product(
                _bounded_regular_bytes(required[2], 256).decode("utf-8", "strict")
            )
            if (
                vendor == identity.vendor_id
                and product_id == identity.product_id
                and product == identity.product
            ):
                candidates.append(current)
        if current == root:
            break
        current = current.parent
    if len(candidates) != 1:
        raise HarnessError("udev ancestry did not identify exactly one USB authorization node")
    return candidates[0] / "authorized"


def _udev_device_path(control_node: Path) -> str:
    result = run_fixed_argv(
        (UDEVADM, "info", "--query=path", "--name", str(control_node)),
        timeout_seconds=5.0,
        max_output_bytes=4096,
    )
    if result.returncode != 0 or result.timed_out or result.output_truncated:
        raise HarnessError("udev device-path query failed")
    value = result.stdout.decode("ascii", "strict").strip()
    if result.stderr or UDEV_PATH_RE.fullmatch(value) is None:
        raise HarnessError("udev device-path query returned unsafe evidence")
    return value


def read_authorized(path: Path) -> int:
    value = _bounded_regular_bytes(path, 8)
    if value not in (b"0", b"0\n", b"1", b"1\n"):
        raise HarnessError("USB authorization attribute shape differs")
    return int(value.strip())


def _write_authorized_byte(path: Path, value: int) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise HarnessError("USB authorization target is not a regular sysfs attribute")
        written = os.write(descriptor, str(value).encode("ascii"))
        if written != 1:
            raise HarnessError("USB authorization write was short")
    finally:
        os.close(descriptor)


def write_authorized(path: Path, value: int, *, expected: int) -> dict[str, object]:
    if value not in (0, 1) or expected not in (0, 1) or value == expected:
        raise HarnessError("USB authorization transition is invalid")
    if read_authorized(path) != expected:
        raise HarnessError("USB authorization precondition differs")
    _write_authorized_byte(path, value)
    deadline = time.monotonic() + 5.0
    while True:
        try:
            observed = read_authorized(path)
        except OSError:
            observed = -1
        if observed == value:
            return {
                "path": str(path),
                "from": expected,
                "to": value,
                "confirmed": True,
                "monotonic_ns": time.monotonic_ns(),
            }
        if time.monotonic() >= deadline:
            raise HarnessError("USB authorization readback timed out")
        time.sleep(0.05)


def restore_authorized(path: Path) -> dict[str, object]:
    """Best-effort exact-target restoration, even after ambiguous write readback."""

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
    while True:
        try:
            observed = read_authorized(path)
        except (HarnessError, OSError):
            observed = -1
        if observed == 1:
            return {
                "path": str(path),
                "from": 0,
                "to": 1,
                "confirmed": True,
                "write_required": True,
                "monotonic_ns": time.monotonic_ns(),
            }
        if time.monotonic() >= deadline:
            raise HarnessError("USB authorization restoration readback timed out")
        time.sleep(0.05)


def _scan_clip_names() -> set[str]:
    root_info = os.lstat(RECORDING_ROOT)
    clips_info = os.lstat(CLIPS_ROOT)
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or not stat.S_ISDIR(clips_info.st_mode)
        or clips_info.st_dev != root_info.st_dev
    ):
        raise HarnessError("clips directory left the recording device")
    names: set[str] = set()
    with os.scandir(CLIPS_ROOT) as entries:
        for entry in entries:
            if len(names) >= MAX_CLIP_ENTRIES:
                raise HarnessError("clips directory exceeded its scan bound")
            if (
                not entry.name
                or len(entry.name) > 255
                or not entry.name.isascii()
                or not entry.name.isprintable()
            ):
                raise HarnessError("clips directory contains an unsafe name")
            names.add(entry.name)
    return names


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


def _probe(path: Path) -> Mapping[str, object]:
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
            str(path.resolve(strict=True)),
        ),
        timeout_seconds=15.0,
        max_output_bytes=MAX_COMMAND_OUTPUT_BYTES,
    )
    return _strict_json(_checked_command(result, f"ffprobe {path.name}"), path.name)


def _contains_h264_idr(data: str) -> bool:
    words: list[str] = []
    for line in data.splitlines():
        payload = line.split(":", 1)[1] if ":" in line else line
        words.extend(word for word in payload.split() if re.fullmatch(r"[0-9A-Fa-f]{2,8}", word))
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


def _first_packet_idr(path: Path) -> None:
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
    document = _strict_json(_checked_command(result, f"IDR probe {path.name}"), path.name)
    packets = document.get("packets")
    if not isinstance(packets, Sequence) or len(packets) != 1:
        raise HarnessError("IDR probe schema differs")
    packet = packets[0]
    if (
        not isinstance(packet, Mapping)
        or set(packet) != {"codec_type", "flags", "data"}
        or packet.get("codec_type") != "video"
        or "K" not in str(packet.get("flags", ""))
        or not isinstance(packet.get("data"), str)
        or not _contains_h264_idr(cast(str, packet["data"]))
    ):
        raise HarnessError("first video packet is not an H.264 IDR")


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
    result = run_fixed_argv(
        tuple(arguments),
        timeout_seconds=30.0,
        max_output_bytes=MAX_COMMAND_OUTPUT_BYTES,
    )
    _checked_command(result, f"hardware decode {path.name}")


def _stream_set(
    document: Mapping[str, object],
) -> tuple[Mapping[str, object], list[Mapping[str, object]]]:
    allowed = {"streams", "format", "programs", "stream_groups"}
    if (
        not {"streams", "format"}.issubset(document)
        or not set(document).issubset(allowed)
        or any(
            name in document and document.get(name) != []
            for name in ("programs", "stream_groups")
        )
    ):
        raise HarnessError("ffprobe top-level schema differs")
    streams = document.get("streams")
    if not isinstance(streams, Sequence) or isinstance(streams, str | bytes):
        raise HarnessError("ffprobe streams schema differs")
    typed = [value for value in streams if isinstance(value, Mapping)]
    if len(typed) != len(streams):
        raise HarnessError("ffprobe stream member is invalid")
    video = [value for value in typed if value.get("codec_type") == "video"]
    audio = [value for value in typed if value.get("codec_type") == "audio"]
    if len(video) != 1:
        raise HarnessError("media does not contain exactly one video stream")
    return video[0], audio


def validate_new_media(before: set[str], after: set[str]) -> dict[str, object]:
    new_names = after - before
    metadata_names = sorted(name for name in new_names if name.endswith(".json"))
    if not 2 <= len(metadata_names) <= MAX_NEW_PAIRS:
        raise HarnessError("qualification did not finalize 2-8 new clip pairs")
    evidence: list[dict[str, object]] = []
    expected_names: set[str] = set()
    audio_states: list[bool] = []
    last_sequence = -1
    for name in metadata_names:
        path = CLIPS_ROOT / name
        sidecar = parse_sidecar_bytes(_bounded_regular_bytes(path, MAX_SIDECAR_BYTES))
        if not isinstance(sidecar, ClipSidecar) or sidecar.metadata_file != name:
            raise HarnessError("sidecar identity differs")
        if sidecar.sequence <= last_sequence:
            raise HarnessError("new sidecar sequence order is not increasing")
        last_sequence = sidecar.sequence
        video_path = CLIPS_ROOT / sidecar.video_file
        for member in (path, video_path):
            info = os.lstat(member)
            if not stat.S_ISREG(info.st_mode) or info.st_dev != os.lstat(RECORDING_ROOT).st_dev:
                raise HarnessError("new media member left recording storage")
        expected_names.update((name, sidecar.video_file))
        document = _probe(video_path)
        video, audio = _stream_set(document)
        expected_audio = sidecar.audio.available
        if (
            video.get("codec_name") != "h264"
            or video.get("profile") != "High"
            or video.get("width") != 1920
            or video.get("height") != 1080
            or video.get("r_frame_rate") != "30/1"
            or len(audio) != int(expected_audio)
        ):
            raise HarnessError("media stream set differs from truthful sidecar")
        if expected_audio:
            stream = audio[0]
            if (
                stream.get("codec_name") != "aac"
                or stream.get("profile") != "LC"
                or str(stream.get("sample_rate")) != "48000"
                or stream.get("channels") != 1
            ):
                raise HarnessError("A/V clip audio stream contract differs")
        _first_packet_idr(video_path)
        _decode(video_path, expected_audio)
        audio_states.append(expected_audio)
        evidence.append(
            {
                "sequence": sidecar.sequence,
                "video_file": sidecar.video_file,
                "metadata_file": name,
                "audio_available": expected_audio,
                "video_sha256": _sha256_file(video_path, maximum=512 * 1024 * 1024),
                "metadata_sha256": _sha256_file(path, maximum=MAX_SIDECAR_BYTES),
                "idr": True,
                "hardware_decode": True,
            }
        )
    if new_names != expected_names:
        raise HarnessError("new recording output is not exactly complete MP4+JSON pairs")
    if audio_states[0] is not True or False not in audio_states:
        raise HarnessError("new media does not prove A/V then video-only truth")
    first_video_only = audio_states.index(False)
    if any(audio_states[first_video_only:]):
        raise HarnessError("audio unexpectedly returned after one-way loss isolation")
    return {"pair_count": len(evidence), "audio_states": audio_states, "pairs": evidence}


async def _wait_snapshot(
    runtime: GStreamerRecorderRuntime,
    run_task: asyncio.Task[None],
    snapshots: list[dict[str, object]],
    predicate: Callable[[Mapping[str, object]], bool],
    *,
    timeout_s: float,
    phase: str,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_s
    while True:
        if run_task.done():
            await run_task
            raise HarnessError(f"production runtime ended during {phase}")
        snapshot = runtime.runtime_snapshot()
        if len(snapshots) >= MAX_SNAPSHOTS:
            raise HarnessError("runtime snapshot evidence exceeded its bound")
        snapshots.append(
            {
                "phase": phase,
                "monotonic_ns": time.monotonic_ns(),
                "snapshot": snapshot,
            }
        )
        if predicate(snapshot):
            return snapshot
        if time.monotonic() >= deadline:
            raise HarnessError(f"runtime snapshot wait timed out: {phase}")
        await asyncio.sleep(0.25)


def _frames(snapshot: Mapping[str, object]) -> int:
    value = snapshot.get("frames")
    if not isinstance(value, Mapping):
        return -1
    encoded = value.get("encoded")
    return encoded if isinstance(encoded, int) and not isinstance(encoded, bool) else -1


def _audio(snapshot: Mapping[str, object]) -> Mapping[str, object]:
    value = snapshot.get("audio")
    return value if isinstance(value, Mapping) else {}


def _drops(snapshot: Mapping[str, object]) -> int:
    value = snapshot.get("frames")
    if not isinstance(value, Mapping):
        return -1
    dropped = value.get("dropped")
    return dropped if isinstance(dropped, int) and not isinstance(dropped, bool) else -1


def _same_stable_identity(left: AlsaIdentity, right: AlsaIdentity) -> bool:
    return (
        left.vendor_id,
        left.product_id,
        left.product,
        left.physical_path,
        left.serial,
    ) == (
        right.vendor_id,
        right.product_id,
        right.product,
        right.physical_path,
        right.serial,
    )


async def qualify() -> dict[str, object]:
    os.environ["DASHCAM_HANDOFF_TRACE"] = "1"
    evidence: dict[str, object] = {
        "schema_version": 1,
        "passed": False,
        "runtime_construction": {
            "factory": "build_production_runtime",
            "ordinary_defaults": True,
            "audio_loss_override_supplied": False,
        },
        "service_mutations": 0,
        "authorization_restored": False,
    }
    failures: list[str] = []
    runtime: GStreamerRecorderRuntime | None = None
    run_task: asyncio.Task[None] | None = None
    stop_requested = asyncio.Event()
    authorized_path: Path | None = None
    authorization_transition_attempted = False
    runtime_started = False
    snapshots: list[dict[str, object]] = []
    before_names: set[str] = set()
    initial_identity: AlsaIdentity | None = None
    before_unit: dict[str, object] | None = None
    try:
        geteuid = getattr(os, "geteuid", None)
        if not callable(geteuid) or geteuid() != 0:
            raise HarnessError("qualification requires root only for exact USB authorization")
        evidence["release"] = _release_identity()
        before_unit = _unit_state()
        evidence["unit_before"] = before_unit
        before_throttle = _throttle()
        evidence["throttle_before"] = before_throttle
        if before_throttle != "throttled=0x0":
            raise HarnessError("Pi was throttled before qualification")
        config = load_config(CONFIG_PATH)
        selector = parse_alsa_selector(config.audio.device_match)
        discovery = discover_capture_device(selector)
        if discovery.status is not AudioDiscoveryStatus.MATCHED or discovery.device is None:
            raise HarnessError("exact microphone is not initially matched")
        initial_identity = discovery.device.identity
        control = Path(f"/dev/snd/controlC{discovery.device.card_index}")
        udev_path = _udev_device_path(control)
        authorized_path = resolve_usb_authorization_path(udev_path, initial_identity)
        if read_authorized(authorized_path) != 1:
            raise HarnessError("exact microphone USB device is not initially authorized")
        evidence["initial_microphone"] = {
            "identity": _identity_dict(initial_identity),
            "endpoint": discovery.device.capture_endpoint,
            "control_node": str(control),
            "udev_device_path": udev_path,
            "authorization_path": str(authorized_path),
        }
        before_names = _scan_clip_names()
        runtime = build_production_runtime(
            config_path=CONFIG_PATH,
            identity_path=IDENTITY_PATH,
        )
        preflight = await runtime.check(config)
        if not preflight.ready:
            raise HarnessError("production storage preflight is not READY")
        await runtime.start(config)
        runtime_started = True
        run_task = asyncio.create_task(runtime.run(stop_requested))

        def initial_ready(snapshot: Mapping[str, object]) -> bool:
            audio = _audio(snapshot)
            units = audio.get("encoded_access_units")
            return (
                audio.get("state") == "MATCHED"
                and isinstance(audio.get("effective"), Mapping)
                and isinstance(units, int)
                and units > 0
                and _frames(snapshot) >= 90
                and snapshot.get("pipeline_restart_count") == 0
                and _drops(snapshot) >= 0
            )

        initial = await _wait_snapshot(
            runtime,
            run_task,
            snapshots,
            initial_ready,
            timeout_s=20.0,
            phase="initial_av",
        )
        evidence["initial_snapshot"] = initial
        initial_drops = _drops(initial)
        evidence["drop_baseline"] = initial_drops
        authorization_transition_attempted = True
        evidence["deauthorization"] = await asyncio.to_thread(
            write_authorized, authorized_path, 0, expected=1
        )

        def isolated(snapshot: Mapping[str, object]) -> bool:
            audio = _audio(snapshot)
            return (
                audio.get("state") == "UNAVAILABLE"
                and audio.get("reason") == "microphone_loss_isolated"
                and audio.get("loss_isolated_without_video_restart") is True
                and audio.get("effective") is None
                and snapshot.get("pipeline_restart_count") == 0
                and _drops(snapshot) == initial_drops
            )

        isolated_snapshot = await _wait_snapshot(
            runtime,
            run_task,
            snapshots,
            isolated,
            timeout_s=20.0,
            phase="isolated_video_only",
        )
        evidence["isolated_snapshot"] = isolated_snapshot
        isolated_frames = _frames(isolated_snapshot)
        if isolated_frames < 0:
            raise HarnessError("isolated snapshot lacks encoded frame truth")
        progressed = await _wait_snapshot(
            runtime,
            run_task,
            snapshots,
            lambda snapshot: isolated(snapshot) and _frames(snapshot) >= isolated_frames + 180,
            timeout_s=12.0,
            phase="post_loss_progress",
        )
        evidence["post_loss_snapshot"] = progressed
    except BaseException as error:
        failures.append(_bounded_detail(f"{type(error).__name__}: {error}"))
    finally:
        stop_requested.set()
        if authorization_transition_attempted and authorized_path is not None:
            try:
                evidence["reauthorization"] = await asyncio.to_thread(
                    restore_authorized, authorized_path
                )
                evidence["authorization_restored"] = True
            except BaseException as error:
                failures.append(
                    _bounded_detail(
                        f"authorization restore failed: {type(error).__name__}: {error}"
                    )
                )
        elif authorized_path is not None:
            try:
                evidence["authorization_unchanged"] = (
                    read_authorized(authorized_path) == 1
                )
                evidence["authorization_restored"] = evidence[
                    "authorization_unchanged"
                ]
            except BaseException as error:
                failures.append(
                    _bounded_detail(
                        "unchanged authorization proof failed: "
                        f"{type(error).__name__}: {error}"
                    )
                )
        if runtime_started and runtime is not None:
            if run_task is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(run_task), timeout=20.0)
                except BaseException as error:
                    failures.append(
                        _bounded_detail(
                            f"runtime run cleanup failed: {type(error).__name__}: {error}"
                        )
                    )
            try:
                await asyncio.wait_for(runtime.stop(), timeout=30.0)
            except BaseException as error:
                failures.append(
                    _bounded_detail(f"runtime stop failed: {type(error).__name__}: {error}")
                )
    evidence["snapshots"] = snapshots
    try:
        after_unit = _unit_state()
        evidence["unit_after"] = after_unit
        if before_unit is None or after_unit != before_unit or after_unit["restarts"] != 0:
            raise HarnessError("dashcamd service state changed")
        after_throttle = _throttle()
        evidence["throttle_after"] = after_throttle
        if after_throttle != "throttled=0x0":
            raise HarnessError("Pi throttled during qualification")
        if not evidence["authorization_restored"] or initial_identity is None:
            raise HarnessError("USB authorization was not proven restored")
        selector = parse_alsa_selector(load_config(CONFIG_PATH).audio.device_match)
        deadline = time.monotonic() + 10.0
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
            or not _same_stable_identity(
                rediscovery.device.identity,
                initial_identity,
            )
        ):
            raise HarnessError("restored microphone stable identity did not rematch")
        evidence["restored_microphone"] = {
            "identity": _identity_dict(rediscovery.device.identity),
            "endpoint": rediscovery.device.capture_endpoint,
        }
        after_names = _scan_clip_names()
        evidence["media"] = validate_new_media(before_names, after_names)
    except BaseException as error:
        failures.append(_bounded_detail(f"{type(error).__name__}: {error}"))
    evidence["failures"] = failures
    evidence["passed"] = not failures
    return evidence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-manifest-sha256", required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    qualify_parser = subparsers.add_parser("qualify")
    qualify_parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    started = time.monotonic_ns()
    verify_manifest(arguments.expected_manifest_sha256)
    try:
        result = asyncio.run(qualify())
    except BaseException as error:
        result = {
            "schema_version": 1,
            "passed": False,
            "runtime_construction": {
                "factory": "build_production_runtime",
                "ordinary_defaults": True,
                "audio_loss_override_supplied": False,
            },
            "authorization_restored": False,
            "failures": [_bounded_detail(f"{type(error).__name__}: {error}")],
        }
    document = {
        **result,
        "started_monotonic_ns": started,
        "ended_monotonic_ns": time.monotonic_ns(),
    }
    _write_atomic_exclusive_json(arguments.output, document)
    return 0 if document["passed"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
