#!/usr/bin/env python3
"""Install one reviewed application bundle on the live SSH-development Pi."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, Protocol, cast

MINIMUM_REMAINING_BYTES: Final = 2 * 1024**3
MAX_FILE_BYTES: Final = 64 * 1024 * 1024
MAX_MANIFEST_BYTES: Final = 256 * 1024
MAX_COMMAND_BYTES: Final = 256 * 1024
MAX_APT_COMMAND_BYTES: Final = 1024 * 1024
COMMAND_TIMEOUT_SECONDS: Final = 180
APT_TIMEOUT_SECONDS: Final = 3600
GSTREAMER_SMOKE_TIMEOUT_SECONDS: Final = 30
GSTREAMER_SMOKE_MAX_OUTPUT_BYTES: Final = 4096
APT_METADATA_BATCH_SIZE: Final = 32
EXPECTED_OS: Final = {"ID": "raspbian", "VERSION_ID": "13", "VERSION_CODENAME": "trixie"}
APP_NAME: Final = "dashcam-pizero2w"
TZDATA_VERSION: Final = "2026.3"
EXPECTED_CARD_CID: Final = "fe34325344000000200000031a0192d1"
UNIT_NAME: Final = "dashcam-storage-check.service"
NETWORK_FALLBACK_UNIT_NAME: Final = "dashcam-network-fallback.service"
RECORDER_UNIT_NAME: Final = "dashcamd.service"
SERVICE_USER: Final = "dashcam"
VIDEO_GROUP: Final = "video"
SERVICE_HOME: Final = "/var/lib/dashcam"
SERVICE_SHELL: Final = "/usr/sbin/nologin"
MANAGED_UNITS: Final = (UNIT_NAME, NETWORK_FALLBACK_UNIT_NAME, RECORDER_UNIT_NAME)
DORMANT_UNITS: Final = (
    "dashcam-web.service",
    "dashcam-prepare-removal.service",
)
LEGACY_RELEASE_ID: Final = "0.1.0.dev0-cd6b7bfb566787ac"
LEGACY_MANIFEST_SHA256: Final = "2b930485cd9435b8cff8581e4aff056eeab7d1e7885791af17b69daa07e2d2e0"
LEGACY_MANAGED_FILE_HASHES: Final = {
    "config.toml": "c5d20f05655235744d98c5275250558fd9978a30f74fa19eeef746a4ee780853",
    "dashcam-storage-check.service": (
        "15b8a4e0f1313df72f5cb2a77455ea45a80a7f9597bf8989508d79709c55e5d6"
    ),
    "dashcam-network-fallback.service": (
        "990f21227cc2b9789565128c61e7301230fed6188ff6981e9a8f5c5f6f46d0e8"
    ),
    "dashcamd.service": "2878464e97bcfa4e3e42f1eae6a558d24fb5c8170461b1cafc41a3747e617520",
}
EXPECTED_TOP_LEVEL: Final = {
    "README.md",
    "SHA256SUMS",
    "apt-packages.txt",
    "config.toml",
    "dashcam-network-fallback.service",
    "dashcam-storage-check.service",
    "dashcamd.service",
    "install.py",
    "manifest.json",
    "wheels",
}
PACKAGE_RE: Final = re.compile(r"[a-z0-9][a-z0-9+.-]{0,79}")
ACCOUNT_RE: Final = re.compile(r"[a-z_][a-z0-9_-]{0,31}\$?")
RELEASE_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,95}")
SHA256_RE: Final = re.compile(r"[0-9a-f]{64}")
APPLICATION_IMPORT_SMOKE: Final = (
    "import importlib,sys;"
    "from pathlib import Path;"
    "venv=Path(sys.prefix).resolve();"
    "modules=tuple(importlib.import_module(name) for name in ("
    "'dashcam.daemon','dashcam.recorder.runtime','dashcam.catalog.database',"
    "'dashcam.recorder.finalizer'));"
    "assert all(Path(module.__file__).resolve().is_relative_to(venv) for module in modules);"
)
GSTREAMER_IMPORT_SMOKE: Final = (
    "import gi;"
    "gi.require_version('Gst','1.0');"
    "gi.require_version('GstBase','1.0');"
    "gi.require_version('GstVideo','1.0');"
    "from gi.repository import GObject,Gst,GstBase,GstVideo;"
    "from dashcam.overlay.native_nv12 import register_native_nv12_overlay;"
    "Gst.init(None);"
    "register_native_nv12_overlay(Gst,GstBase,GstVideo,GObject);"
    "caps=Gst.Caps.from_string('video/x-raw,framerate=30/1');"
    "rate=caps.get_structure(0).get_value('framerate');"
    "assert int(rate.num)==30 and int(rate.denom)==1;"
    "missing=[name for name in ('queue','libcamerasrc','dashcamnv12overlay','v4l2h264enc',"
    "'alsasrc','audioconvert','audioresample','voaacenc','aacparse','videotestsrc','fakesink') "
    "if Gst.ElementFactory.find(name) is None];"
    "__import__('sys').exit('missing GStreamer factories: '+','.join(missing)) "
    "if missing else None;"
    "pipeline=Gst.parse_launch('videotestsrc num-buffers=1 ! "
    "video/x-raw,format=(string)NV12,width=(int)1920,height=(int)1080,"
    "framerate=(fraction)30/1 ! dashcamnv12overlay name=overlay ! fakesink');"
    "overlay=pipeline.get_by_name('overlay');"
    "assert overlay is not None;"
    "overlay.set_overlay_text('TIME UNSYNCED\\nGPS INVALID');"
    "initial=overlay.overlay_snapshot();"
    "assert initial['updates']==1 and initial['enabled'] is True;"
    "assert pipeline.set_state(Gst.State.PLAYING)!=Gst.StateChangeReturn.FAILURE;"
    "message=pipeline.get_bus().timed_pop_filtered("
    "5*Gst.SECOND,Gst.MessageType.ERROR|Gst.MessageType.EOS);"
    "assert message is not None and message.type==Gst.MessageType.EOS;"
    "assert pipeline.set_state(Gst.State.NULL)!=Gst.StateChangeReturn.FAILURE;"
    "final=overlay.overlay_snapshot();"
    "assert final['caps_accepted'] is True and final['frames_rendered']==1;"
    "assert final['transform_failures']==0 and final['short_writes']==0;"
    "overlay.set_overlay_text(None);"
    "silent=overlay.overlay_snapshot();"
    "assert silent['enabled'] is False and silent['frames_seen']==1"
)
STAGING_RELEASE_SMOKE: Final = APPLICATION_IMPORT_SMOKE + GSTREAMER_IMPORT_SMOKE


class Refusal(RuntimeError):
    """A fail-closed installation refusal."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class _StatVfs(Protocol):
    f_bavail: int
    f_frsize: int


class Runner:
    """Bounded command runner with a closed absolute executable allowlist."""

    ALLOWED: Final = {
        "/usr/bin/apt-cache",
        "/usr/bin/apt-get",
        "/usr/bin/dpkg",
        "/usr/bin/dpkg-query",
        "/usr/bin/getent",
        "/usr/bin/python3",
        "/usr/bin/setpriv",
        "/usr/bin/systemctl",
        "/usr/bin/systemd-analyze",
        "/usr/bin/findmnt",
        "/usr/sbin/usermod",
    }

    def _execute(
        self,
        command: Sequence[str],
        *,
        timeout: int,
        output_limit: int,
        accepted: frozenset[int],
    ) -> CommandResult:
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                list(command),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={
                    "LC_ALL": "C",
                    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                    "DEBIAN_FRONTEND": "noninteractive",
                },
                start_new_session=True,
            )
            stdout, stderr = self._communicate_bounded(
                process, timeout=timeout, output_limit=output_limit
            )
            returncode = process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
            if process is not None:
                self._terminate_group(process)
            raise Refusal("bounded command execution failed") from exc
        if returncode not in accepted:
            raise Refusal(f"command failed with exit {returncode}: {command[0]}")
        try:
            return CommandResult(
                returncode,
                stdout.decode("utf-8", errors="strict"),
                stderr.decode("utf-8", errors="strict"),
            )
        except UnicodeDecodeError as exc:
            raise Refusal("command output was not UTF-8") from exc

    @staticmethod
    def _communicate_bounded(
        process: subprocess.Popen[bytes], *, timeout: int, output_limit: int
    ) -> tuple[bytes, bytes]:
        if process.stdout is None or process.stderr is None:
            raise Refusal("command pipes were not created")
        selector = selectors.DefaultSelector()
        outputs: dict[int, list[bytes]] = {
            process.stdout.fileno(): [],
            process.stderr.fileno(): [],
        }
        stdout_descriptor = process.stdout.fileno()
        stderr_descriptor = process.stderr.fileno()
        totals = {descriptor: 0 for descriptor in outputs}
        set_blocking = cast(
            Callable[[int, bool], None],
            getattr(os, "set_blocking"),  # noqa: B009 - target-only API
        )
        for stream in (process.stdout, process.stderr):
            set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    Runner._terminate_group(process)
                    raise Refusal("command exceeded its time bound")
                for key, _ in selector.select(min(remaining, 0.25)):
                    descriptor = key.fd
                    chunk = os.read(descriptor, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    totals[descriptor] += len(chunk)
                    if totals[descriptor] > output_limit:
                        Runner._terminate_group(process)
                        raise Refusal("command output exceeded its bound")
                    outputs[descriptor].append(chunk)
        finally:
            selector.close()
            process.stdout.close()
            process.stderr.close()
        return (
            b"".join(outputs[stdout_descriptor]),
            b"".join(outputs[stderr_descriptor]),
        )

    @staticmethod
    def _terminate_group(process: subprocess.Popen[bytes]) -> None:
        killpg = cast(Callable[[int, int], None] | None, getattr(os, "killpg", None))
        try:
            if killpg is not None:
                sigkill = cast(int, signal.__dict__.get("SIGKILL", 9))
                killpg(process.pid, sigkill)
            elif process.poll() is None:
                process.kill()
            if process.poll() is None:
                process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            if process.poll() is None:
                process.kill()

    def run(
        self,
        command: Sequence[str],
        *,
        timeout: int = COMMAND_TIMEOUT_SECONDS,
        output_limit: int = MAX_COMMAND_BYTES,
        accepted: frozenset[int] = frozenset({0}),
    ) -> CommandResult:
        if (
            not command
            or command[0] not in self.ALLOWED
            or timeout < 1
            or timeout > APT_TIMEOUT_SECONDS
            or output_limit < 1
            or output_limit > MAX_APT_COMMAND_BYTES
            or any("\x00" in item or len(item) > 4096 for item in command)
        ):
            raise Refusal("command differs from the closed allowlist")
        return self._execute(command, timeout=timeout, output_limit=output_limit, accepted=accepted)

    def run_release_python(
        self,
        executable: str,
        arguments: Sequence[str],
        *,
        timeout: int = COMMAND_TIMEOUT_SECONDS,
        output_limit: int = MAX_COMMAND_BYTES,
    ) -> CommandResult:
        """Run only a just-created, closed staging venv interpreter."""

        path = PurePosixPath(executable)
        if (
            not path.is_absolute()
            or path.parts[:4] != ("/", "opt", "dashcam", "releases")
            or len(path.parts) != 8
            or not path.parts[4].startswith(".staging-")
            or path.parts[5:] != ("venv", "bin", "python")
            or not arguments
            or timeout < 1
            or timeout > COMMAND_TIMEOUT_SECONDS
            or output_limit < 1
            or output_limit > MAX_COMMAND_BYTES
            or any("\x00" in item or len(item) > 4096 for item in arguments)
        ):
            raise Refusal("release interpreter differs from the closed path")
        return self._execute(
            [executable, *arguments],
            timeout=timeout,
            output_limit=output_limit,
            accepted=frozenset({0}),
        )

    def run_release_python_as_service_user(
        self,
        executable: str,
        arguments: Sequence[str],
        *,
        timeout: int = COMMAND_TIMEOUT_SECONDS,
        output_limit: int = MAX_COMMAND_BYTES,
    ) -> CommandResult:
        """Run only a closed staging interpreter as dashcam with all its groups."""

        path = PurePosixPath(executable)
        if (
            not path.is_absolute()
            or path.parts[:4] != ("/", "opt", "dashcam", "releases")
            or len(path.parts) != 8
            or not path.parts[4].startswith(".staging-")
            or path.parts[5:] != ("venv", "bin", "python")
            or not arguments
            or timeout < 1
            or timeout > COMMAND_TIMEOUT_SECONDS
            or output_limit < 1
            or output_limit > MAX_COMMAND_BYTES
            or any("\x00" in item or len(item) > 4096 for item in arguments)
        ):
            raise Refusal("service-user release interpreter differs from the closed path")
        return self._execute(
            [
                "/usr/bin/setpriv",
                f"--reuid={SERVICE_USER}",
                f"--regid={SERVICE_USER}",
                "--init-groups",
                executable,
                *arguments,
            ],
            timeout=timeout,
            output_limit=output_limit,
            accepted=frozenset({0}),
        )


def _safe_read(path: Path, limit: int = MAX_FILE_BYTES) -> bytes:
    try:
        listed = path.lstat()
    except FileNotFoundError as exc:
        raise Refusal(f"required file is absent: {path}") from exc
    if stat.S_ISLNK(listed.st_mode) or not stat.S_ISREG(listed.st_mode) or listed.st_nlink != 1:
        raise Refusal(f"file has an unsafe type or link count: {path}")
    if listed.st_size > limit:
        raise Refusal(f"file exceeds its size bound: {path}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        current = os.fstat(descriptor)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or (current.st_dev, current.st_ino) != (listed.st_dev, listed.st_ino)
        ):
            raise Refusal(f"file identity changed while opening: {path}")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(descriptor)
    value = b"".join(chunks)
    if len(value) > limit:
        raise Refusal(f"file exceeds its size bound: {path}")
    return value


def _safe_pseudo_read(path: Path, limit: int) -> bytes:
    if not 1 <= limit <= 4096:
        raise Refusal("pseudo-file read bound is invalid")
    try:
        listed = path.lstat()
    except FileNotFoundError as exc:
        raise Refusal(f"required pseudo-file is absent: {path}") from exc
    if stat.S_ISLNK(listed.st_mode) or not stat.S_ISREG(listed.st_mode) or listed.st_nlink != 1:
        raise Refusal(f"pseudo-file has an unsafe type or link count: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        current = os.fstat(descriptor)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or (current.st_dev, current.st_ino) != (listed.st_dev, listed.st_ino)
        ):
            raise Refusal(f"pseudo-file identity changed while opening: {path}")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 128))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(descriptor)
    payload = b"".join(chunks)
    if len(payload) > limit:
        raise Refusal(f"pseudo-file exceeds its content bound: {path}")
    return payload


def _read_sysfs_cid(root: Path) -> str:
    payload = _safe_pseudo_read(root / "sys/class/block/mmcblk0/device/cid", 128)
    if payload != f"{EXPECTED_CARD_CID}\n".encode("ascii"):
        raise Refusal("sysfs CID content is noncanonical or differs")
    return EXPECTED_CARD_CID


def _strict_json(payload: bytes, description: str) -> dict[str, object]:
    duplicate = False

    def hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        nonlocal duplicate
        value: dict[str, object] = {}
        for key, item in pairs:
            duplicate |= key in value
            value[key] = item
        return value

    try:
        value = json.loads(payload.decode("ascii"), object_pairs_hook=hook)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Refusal(f"{description} is not strict ASCII JSON") from exc
    if duplicate or not isinstance(value, dict):
        raise Refusal(f"{description} must be one object without duplicate keys")
    return value


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _manifest(bundle: Path) -> dict[str, object]:
    if not bundle.is_absolute():
        raise Refusal("bundle path must be absolute")
    try:
        bundle_info = bundle.lstat()
    except FileNotFoundError as exc:
        raise Refusal("bundle is absent") from exc
    if stat.S_ISLNK(bundle_info.st_mode) or not stat.S_ISDIR(bundle_info.st_mode):
        raise Refusal("bundle directory is unsafe")
    if {entry.name for entry in bundle.iterdir()} != EXPECTED_TOP_LEVEL:
        raise Refusal("bundle top-level allowlist differs")
    wheels = bundle / "wheels"
    wheel_info = wheels.lstat()
    if stat.S_ISLNK(wheel_info.st_mode) or not stat.S_ISDIR(wheel_info.st_mode):
        raise Refusal("wheel directory is unsafe")

    manifest_payload = _safe_read(bundle / "manifest.json", MAX_MANIFEST_BYTES)
    value = _strict_json(manifest_payload, "manifest")
    if (
        set(value)
        != {
            "schema_version",
            "release_id",
            "application",
            "tzdata",
            "apt_packages",
            "install_budget_bytes",
            "files",
        }
        or value["schema_version"] != 1
    ):
        raise Refusal("manifest schema differs")
    release_id = value["release_id"]
    budget = value["install_budget_bytes"]
    packages = value["apt_packages"]
    files = value["files"]
    application = value["application"]
    tzdata = value["tzdata"]
    if not isinstance(release_id, str) or RELEASE_RE.fullmatch(release_id) is None:
        raise Refusal("release ID is unsafe")
    if not isinstance(budget, int) or isinstance(budget, bool) or not 0 < budget <= 2 * 1024**3:
        raise Refusal("install budget is unsafe")
    if (
        not isinstance(packages, list)
        or not packages
        or packages != sorted(set(packages))
        or any(not isinstance(item, str) or PACKAGE_RE.fullmatch(item) is None for item in packages)
    ):
        raise Refusal("APT package allowlist is invalid")
    if not isinstance(files, dict) or not files:
        raise Refusal("file manifest is invalid")
    if (
        not isinstance(application, dict)
        or set(application) != {"name", "version", "wheel"}
        or application["name"] != APP_NAME
        or not isinstance(application["version"], str)
        or not isinstance(application["wheel"], str)
    ):
        raise Refusal("application identity differs")
    if (
        not isinstance(tzdata, dict)
        or set(tzdata) != {"name", "version", "wheel"}
        or tzdata["name"] != "tzdata"
        or tzdata["version"] != TZDATA_VERSION
        or not isinstance(tzdata["wheel"], str)
    ):
        raise Refusal("tzdata identity differs")

    expected_files = {
        "README.md",
        "apt-packages.txt",
        "config.toml",
        "dashcam-network-fallback.service",
        "dashcam-storage-check.service",
        "dashcamd.service",
        "install.py",
        str(application["wheel"]),
        str(tzdata["wheel"]),
    }
    if set(files) != expected_files:
        raise Refusal("manifest file allowlist differs")
    if {f"wheels/{entry.name}" for entry in wheels.iterdir()} != {
        str(application["wheel"]),
        str(tzdata["wheel"]),
    }:
        raise Refusal("wheel allowlist differs")
    for relative, details in files.items():
        if (
            not isinstance(relative, str)
            or PurePosixPath(relative).is_absolute()
            or ".." in PurePosixPath(relative).parts
            or not isinstance(details, dict)
            or set(details) != {"sha256", "size"}
        ):
            raise Refusal("manifest file entry is unsafe")
        digest = details["sha256"]
        size = details["size"]
        payload = _safe_read(bundle / relative)
        if (
            not isinstance(digest, str)
            or SHA256_RE.fullmatch(digest) is None
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size != len(payload)
            or digest != _sha256(payload)
        ):
            raise Refusal(f"manifest hash/size mismatch: {relative}")

    package_text = _safe_read(bundle / "apt-packages.txt").decode("ascii").splitlines()
    parsed_packages = [
        line.strip() for line in package_text if line.strip() and not line.lstrip().startswith("#")
    ]
    if parsed_packages != packages:
        raise Refusal("APT package source and manifest differ")
    unit = _safe_read(bundle / "dashcam-storage-check.service").decode("ascii")
    expected_exec = (
        "ExecStart=/opt/dashcam/current/venv/bin/python -m dashcam.storage.preflight "
        "--config /etc/dashcam/config.toml --identity /etc/dashcam/storage-volume.env"
    )
    if unit.count(expected_exec) != 1 or "ExecStart=" in unit.replace(expected_exec, ""):
        raise Refusal("storage unit entry point differs")
    fallback_unit = _safe_read(bundle / "dashcam-network-fallback.service").decode("ascii")
    fallback_exec = "ExecStart=/opt/dashcam/current/venv/bin/python -m dashcam.network_fallback"
    required_fallback_lines = (
        "Description=DashCam bounded Wi-Fi client-first fallback AP",
        "Requires=NetworkManager.service",
        "Wants=cloud-final.service",
        "After=NetworkManager.service cloud-final.service",
        "Type=oneshot",
        "User=root",
        "Group=root",
        fallback_exec,
        "TimeoutStartSec=120s",
        "RemainAfterExit=yes",
        "UMask=0077",
        "WantedBy=multi-user.target",
    )
    if any(fallback_unit.count(line) != 1 for line in required_fallback_lines) or any(
        line.startswith("Exec") and line != fallback_exec for line in fallback_unit.splitlines()
    ):
        raise Refusal("network fallback unit contract differs")
    recorder_unit = _safe_read(bundle / RECORDER_UNIT_NAME).decode("ascii")
    recorder_exec = (
        "ExecStart=/opt/dashcam/current/venv/bin/python -m dashcam.daemon "
        "--config /etc/dashcam/config.toml --identity /etc/dashcam/storage-volume.env"
    )
    required_recorder_lines = (
        "Description=Dashcam recorder (single camera owner)",
        "After=local-fs.target dashcam-storage-check.service",
        "Wants=dashcam-storage-check.service",
        "StartLimitIntervalSec=300",
        "StartLimitBurst=5",
        "StartLimitAction=none",
        "Type=notify",
        "NotifyAccess=main",
        "User=dashcam",
        "Group=dashcam",
        "SupplementaryGroups=audio video render dialout dashcam-storage",
        "WorkingDirectory=/var/lib/dashcam",
        recorder_exec,
        "Restart=on-failure",
        "RestartSec=1s",
        "RestartSteps=5",
        "RestartMaxDelaySec=60s",
        "RestartMode=normal",
        "TimeoutStartSec=45s",
        "TimeoutStopSec=30s",
        "WatchdogSec=20s",
        "RuntimeDirectory=dashcam",
        "RuntimeDirectoryMode=0750",
        "StateDirectory=dashcam",
        "StateDirectoryMode=0750",
        "UMask=0027",
        "NoNewPrivileges=yes",
        "CapabilityBoundingSet=",
        "AmbientCapabilities=",
        "PrivateTmp=yes",
        "PrivateDevices=no",
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
        "ReadWritePaths=/var/lib/dashcam /srv/dashcam",
        "WantedBy=multi-user.target",
    )
    recorder_lines = recorder_unit.splitlines()
    if (
        any(recorder_lines.count(line) != 1 for line in required_recorder_lines)
        or any(line.startswith("Exec") and line != recorder_exec for line in recorder_lines)
        or any(line.startswith("Requires=") for line in recorder_lines)
        or sum(line.startswith("Wants=") for line in recorder_lines) != 1
        or sum(line.startswith("After=") for line in recorder_lines) != 1
        or sum(line.startswith("SupplementaryGroups=") for line in recorder_lines) != 1
    ):
        raise Refusal("recorder unit contract differs")
    sums = _safe_read(bundle / "SHA256SUMS", MAX_MANIFEST_BYTES).decode("ascii").splitlines()
    expected_sums = {
        **{name: str(details["sha256"]) for name, details in files.items()},
        "manifest.json": _sha256(manifest_payload),
    }
    actual_sums: dict[str, str] = {}
    for line in sums:
        parts = line.split("  ")
        if len(parts) != 2 or parts[1] in actual_sums:
            raise Refusal("SHA256SUMS is malformed")
        actual_sums[parts[1]] = parts[0]
    if actual_sums != expected_sums:
        raise Refusal("SHA256SUMS differs from the manifest")
    value["_manifest_sha256"] = _sha256(manifest_payload)
    return value


def _os_release(payload: bytes) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in payload.decode("ascii").splitlines():
        if not raw_line or raw_line.startswith("#"):
            continue
        key, separator, raw_value = raw_line.partition("=")
        if not separator or not key.replace("_", "").isalnum():
            raise Refusal("os-release is malformed")
        value = raw_value.strip('"')
        if key in result:
            raise Refusal("os-release has duplicate keys")
        result[key] = value
    if any(result.get(key) != expected for key, expected in EXPECTED_OS.items()):
        raise Refusal("target is not Raspberry Pi OS 32-bit Trixie")
    return result


def _os_release_target(link_target: str) -> PurePosixPath:
    if link_target != "../usr/lib/os-release":
        raise Refusal("os-release symlink target differs from the reviewed stock image")
    return PurePosixPath("/usr/lib/os-release")


def _read_os_release(root: Path) -> bytes:
    link = root / "etc/os-release"
    try:
        link_info = link.lstat()
    except FileNotFoundError as exc:
        raise Refusal("stock os-release symlink is absent") from exc
    if not stat.S_ISLNK(link_info.st_mode):
        raise Refusal("stock os-release path is not the reviewed symlink")
    target = _os_release_target(os.readlink(link))
    target_path = root / str(target).lstrip("/")
    payload = _safe_read(target_path, 16384)
    target_info = target_path.lstat()
    if os.name == "posix" and (
        stat.S_IMODE(target_info.st_mode) != 0o644
        or target_info.st_uid != 0
        or target_info.st_gid != 0
    ):
        raise Refusal("canonical os-release target ownership or mode differs")
    return payload


def _identity(payload: bytes) -> dict[str, str]:
    expected_keys = {
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
    result: dict[str, str] = {}
    try:
        lines = payload.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise Refusal("storage identity is not ASCII") from exc
    if not payload.endswith(b"\n"):
        raise Refusal("storage identity is noncanonical")
    for line in lines:
        key, separator, value = line.partition("=")
        if not separator or key in result or key not in expected_keys or not value:
            raise Refusal("storage identity is malformed")
        result[key] = value
    if (
        set(result) != expected_keys
        or result["DASHCAM_STORAGE_SCHEMA_VERSION"] != "1"
        or result["DASHCAM_STORAGE_LAYOUT_VERSION"] != "1"
        or result["DASHCAM_STORAGE_MOUNT"] != "/srv/dashcam"
        or SHA256_RE.fullmatch(result["DASHCAM_STORAGE_SOURCE_MBR_SHA256"]) is None
    ):
        raise Refusal("storage identity differs")
    return result


def _verify_storage(root: Path, runner: Runner, storage_gid: int) -> dict[str, str]:
    identity_path = root / "etc/dashcam/storage-volume.env"
    identity_info = identity_path.lstat()
    if (
        stat.S_IMODE(identity_info.st_mode) != 0o640
        or identity_info.st_uid != 0
        or identity_info.st_gid != storage_gid
    ):
        raise Refusal("storage identity ownership or mode differs")
    identity = _identity(_safe_read(identity_path, 8192))
    completion = _strict_json(
        _safe_read(root / "var/lib/dashcam/provisioning/layout-v1.complete.json", 65536),
        "storage completion",
    )
    partitions = completion.get("partitions")
    if (
        completion.get("schema_version") != 1
        or completion.get("layout_version") != 1
        or completion.get("cid") != identity["DASHCAM_STORAGE_CID"]
        or completion.get("source_mbr_sha256") != identity["DASHCAM_STORAGE_SOURCE_MBR_SHA256"]
        or not isinstance(partitions, dict)
        or not isinstance(partitions.get("data"), dict)
    ):
        raise Refusal("storage completion and identity differ")
    data = partitions["data"]
    if (
        identity["DASHCAM_STORAGE_CID"] != EXPECTED_CARD_CID
        or data.get("device") != "/dev/mmcblk0p3"
        or data.get("filesystem") != "exfat"
        or data.get("label") != "DASHCAM"
        or data.get("uuid") != identity["DASHCAM_STORAGE_UUID"]
    ):
        raise Refusal("storage completion data identity differs")
    observed = runner.run(
        [
            "/usr/bin/findmnt",
            "--json",
            "--mountpoint",
            "/srv/dashcam",
            "--output",
            "TARGET,SOURCE,FSTYPE,LABEL,UUID,OPTIONS,MAJ:MIN",
        ]
    )
    facts = _strict_json(observed.stdout.encode("utf-8"), "findmnt output")
    rows = facts.get("filesystems")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise Refusal("storage mount observation is ambiguous")
    row = rows[0]
    options = row.get("options")
    if (
        row.get("target") != "/srv/dashcam"
        or row.get("fstype") != "exfat"
        or row.get("label") != "DASHCAM"
        or row.get("uuid") != identity["DASHCAM_STORAGE_UUID"]
        or row.get("source") != data.get("device")
        or not isinstance(options, str)
        or "rw" not in options.split(",")
        or "ro" in options.split(",")
    ):
        raise Refusal("live storage mount differs from completion")
    major_minor = row.get("maj:min")
    mount_info = (root / "srv/dashcam").stat()
    os_root_info = root.stat()
    major = cast(Callable[[int], int], getattr(os, "major"))  # noqa: B009
    minor = cast(Callable[[int], int], getattr(os, "minor"))  # noqa: B009
    if (
        not isinstance(major_minor, str)
        or major_minor != f"{major(mount_info.st_dev)}:{minor(mount_info.st_dev)}"
        or mount_info.st_dev == os_root_info.st_dev
    ):
        raise Refusal("live storage device identity is not a distinct exact mount")
    observed_cid = _read_sysfs_cid(root)
    if observed_cid != identity["DASHCAM_STORAGE_CID"]:
        raise Refusal("live card CID differs from storage identity")
    sentinel = _strict_json(
        _safe_read(root / "srv/dashcam/.dashcam-volume", 8192),
        "storage sentinel",
    )
    expected_sentinel = {
        "layout_version": 1,
        "serial": identity["DASHCAM_STORAGE_CID"],
        "dashcam_uuid": identity["DASHCAM_STORAGE_UUID"],
        "source_table_fingerprint": identity["DASHCAM_STORAGE_SOURCE_MBR_SHA256"],
        "root_end_sector": int(identity["DASHCAM_STORAGE_ROOT_END_SECTOR"]),
        "data_start_sector": int(identity["DASHCAM_STORAGE_DATA_START_SECTOR"]),
        "data_end_sector": int(identity["DASHCAM_STORAGE_DATA_END_SECTOR"]),
    }
    if sentinel != expected_sentinel:
        raise Refusal("storage sentinel differs from the exact identity")
    return identity


def _systemd_state(root: Path, runner: Runner) -> dict[str, str]:
    unit_root = root / "etc/systemd/system"
    bundle_states: dict[str, str] = {}
    for name in (*MANAGED_UNITS, *DORMANT_UNITS):
        result = runner.run(
            ["/usr/bin/systemctl", "is-enabled", name],
            accepted=frozenset({0, 1, 3, 4}),
        )
        state = result.stdout.strip()
        if state not in {"enabled", "disabled", "not-found"}:
            raise Refusal(f"unexpected systemd state for {name}")
        bundle_states[name] = state
    for name in DORMANT_UNITS:
        exact = unit_root / name
        if exact.exists() or exact.is_symlink() or bundle_states[name] != "not-found":
            raise Refusal(f"dormant unit is preexisting or enabled: {name}")
        for wants in unit_root.glob("*.wants"):
            candidate = wants / name
            if candidate.exists() or candidate.is_symlink():
                raise Refusal(f"dormant unit has a preexisting dependency link: {name}")
    for name in MANAGED_UNITS:
        expected_unit = unit_root / name
        if expected_unit.exists() or expected_unit.is_symlink():
            info = expected_unit.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise Refusal(f"managed unit path is foreign: {name}")
        if bundle_states[name] == "enabled" and not expected_unit.is_file():
            raise Refusal(f"managed unit is enabled from a foreign location: {name}")
    return bundle_states


def _systemd_activity(systemd_states: Mapping[str, str], runner: Runner) -> dict[str, str]:
    """Read and constrain managed service activity before a reviewed apply."""

    states: dict[str, str] = {}
    for name in MANAGED_UNITS:
        result = runner.run(
            [
                "/usr/bin/systemctl",
                "show",
                "--property=ActiveState",
                "--property=SubState",
                "--value",
                name,
            ]
        )
        values = result.stdout.splitlines()
        if len(values) != 2 or any(not value for value in values):
            raise Refusal(f"unexpected systemd activity shape for {name}")
        state = "/".join(values)
        states[name] = state
    if states[RECORDER_UNIT_NAME] != "inactive/dead":
        raise Refusal("dashcam recorder must be inactive before installation")
    if states[NETWORK_FALLBACK_UNIT_NAME] != "inactive/dead":
        raise Refusal("network fallback must be inactive before installation")
    if states[UNIT_NAME] == "inactive/dead" and systemd_states.get(UNIT_NAME) == "not-found":
        return states
    if states[UNIT_NAME] != "active/exited":
        raise Refusal("storage check activity is not a reviewed completed state")
    return states


@dataclass(frozen=True)
class ManagedFile:
    name: str
    source: Path
    target: Path
    mode: int
    uid: int = 0
    gid: int = 0


def _managed_files(bundle: Path, root: Path) -> tuple[ManagedFile, ...]:
    return (
        ManagedFile("config.toml", bundle / "config.toml", root / "etc/dashcam/config.toml", 0o640),
        ManagedFile(
            "dashcam-storage-check.service",
            bundle / "dashcam-storage-check.service",
            root / "etc/systemd/system" / UNIT_NAME,
            0o644,
        ),
        ManagedFile(
            "dashcam-network-fallback.service",
            bundle / "dashcam-network-fallback.service",
            root / "etc/systemd/system" / NETWORK_FALLBACK_UNIT_NAME,
            0o644,
        ),
        ManagedFile(
            "dashcamd.service",
            bundle / RECORDER_UNIT_NAME,
            root / "etc/systemd/system" / RECORDER_UNIT_NAME,
            0o644,
        ),
    )


def _managed_target_hash(path: Path) -> str | None:
    if not (path.exists() or path.is_symlink()):
        return None
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise Refusal(f"managed file path is foreign: {path}")
    return _sha256(_safe_read(path))


def _managed_file_hashes(files: Sequence[ManagedFile]) -> dict[str, str]:
    return {file.name: _sha256(_safe_read(file.source)) for file in files}


def _observed_managed_file_hashes(files: Sequence[ManagedFile]) -> dict[str, str | None]:
    return {file.name: _managed_target_hash(file.target) for file in files}


def _current_release_identity(root: Path) -> tuple[str, str] | None:
    current = root / "opt/dashcam/current"
    if not (current.exists() or current.is_symlink()):
        return None
    info = current.lstat()
    if not stat.S_ISLNK(info.st_mode):
        raise Refusal("current release path is foreign")
    symlink_target = os.readlink(current)
    release_name = symlink_target[len("releases/") :]
    if not symlink_target.startswith("releases/") or not release_name or "/" in release_name:
        raise Refusal("current release symlink is foreign")
    release = current.parent / symlink_target
    marker = _strict_json(_safe_read(release / "installed.json", 8192), "current release marker")
    release_id = marker.get("release_id")
    manifest_sha256 = marker.get("manifest_sha256")
    if (
        marker.get("schema_version") != 1
        or set(marker) != {"schema_version", "release_id", "manifest_sha256"}
        or not isinstance(release_id, str)
        or release_id != release.name
        or not isinstance(manifest_sha256, str)
        or SHA256_RE.fullmatch(manifest_sha256) is None
    ):
        raise Refusal("current release marker differs")
    return release_id, manifest_sha256


def _journal_managed_file_hashes(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != set(LEGACY_MANAGED_FILE_HASHES):
        raise Refusal("applied journal managed-file hashes differ")
    if any(
        not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None
        for digest in value.values()
    ):
        raise Refusal("applied journal managed-file hashes differ")
    return cast(dict[str, str], value)


def _authorize_managed_file_upgrade(root: Path, observed: Mapping[str, str | None]) -> None:
    """Accept only exact managed files attributable to the active applied release."""

    current = _current_release_identity(root)
    if current is None or any(value is None for value in observed.values()):
        raise Refusal("managed-file upgrade prestate is incomplete or unattributable")
    journal = _strict_json(
        _safe_read(root / "var/lib/dashcam/app-install-v1.json", MAX_MANIFEST_BYTES),
        "applied install journal",
    )
    release_id = journal.get("release_id")
    manifest_sha256 = journal.get("manifest_sha256")
    if (
        journal.get("schema_version") != 1
        or journal.get("mode") != "applied"
        or journal.get("ready") is not True
        or (release_id, manifest_sha256) != current
    ):
        raise Refusal("applied journal differs from the current release")
    observed_hashes = cast(dict[str, str], observed)
    if "managed_file_hashes" in journal:
        if _journal_managed_file_hashes(journal["managed_file_hashes"]) != observed_hashes:
            raise Refusal("managed files differ from the applied journal")
        return
    if (
        current == (LEGACY_RELEASE_ID, LEGACY_MANIFEST_SHA256)
        and observed_hashes == LEGACY_MANAGED_FILE_HASHES
    ):
        return
    raise Refusal("managed files are not attributable to the applied release")


def _managed_prestate(
    bundle: Path, root: Path, manifest: Mapping[str, object]
) -> tuple[dict[str, str | None], dict[str, str]]:
    """Return exact managed-file prestate and refuse foreign upgrade sources."""

    for path in (root / "opt/dashcam", root / "opt/dashcam/releases"):
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise Refusal(f"managed directory path is foreign: {path}")
    files = _managed_files(bundle, root)
    observed = _observed_managed_file_hashes(files)
    desired = _managed_file_hashes(files)
    if any(value is not None for value in observed.values()) and observed != desired:
        _authorize_managed_file_upgrade(root, observed)

    release = root / "opt/dashcam/releases" / str(manifest["release_id"])
    if release.exists() or release.is_symlink():
        info = release.lstat()
        expected_marker = (
            json.dumps(
                {
                    "schema_version": 1,
                    "release_id": manifest["release_id"],
                    "manifest_sha256": manifest["_manifest_sha256"],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("ascii")
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or _safe_read(release / "installed.json", 8192) != expected_marker
        ):
            raise Refusal("existing release is foreign or incomplete")
        _verify_release_venv(release)

    _current_release_identity(root)
    return observed, desired


def _free_bytes(path: Path) -> int:
    statvfs = cast(Callable[[Path], _StatVfs], getattr(os, "statvfs"))  # noqa: B009
    facts = statvfs(path)
    return facts.f_bavail * facts.f_frsize


def _projected_headroom(free_bytes: int, install_budget_bytes: int, apt_peak_bytes: int = 0) -> int:
    if (
        isinstance(free_bytes, bool)
        or isinstance(install_budget_bytes, bool)
        or free_bytes < 0
        or isinstance(apt_peak_bytes, bool)
        or apt_peak_bytes < 0
        or not 0 < install_budget_bytes <= 2 * 1024**3
    ):
        raise Refusal("headroom inputs are invalid")
    projected = free_bytes - install_budget_bytes - apt_peak_bytes
    if projected < MINIMUM_REMAINING_BYTES:
        raise Refusal("projected root headroom is below 2 GiB")
    return projected


def _package_plan(packages: Sequence[str], runner: Runner) -> tuple[dict[str, str], dict[str, str]]:
    installed: dict[str, str] = {}
    missing: dict[str, str] = {}
    for package in packages:
        result = runner.run(
            ["/usr/bin/dpkg-query", "-W", "-f=${Status}\\t${Version}\\n", package],
            accepted=frozenset({0, 1}),
        )
        prefix = "install ok installed\t"
        if result.returncode == 0 and result.stdout.startswith(prefix):
            installed[package] = result.stdout[len(prefix) :].strip()
            continue
        policy = runner.run(
            ["/usr/bin/apt-cache", "policy", package],
            accepted=frozenset({0, 100}),
        )
        candidates = [
            line.split(":", 1)[1].strip()
            for line in policy.stdout.splitlines()
            if line.strip().startswith("Candidate:")
        ]
        if len(candidates) != 1 or candidates[0] in {"", "(none)"}:
            raise Refusal(f"APT package has no candidate: {package}")
        missing[package] = candidates[0]
    return installed, missing


def _apt_decimal(value: str, description: str) -> int:
    if not value.isdecimal():
        raise Refusal(f"APT metadata {description} is not decimal")
    result = int(value)
    if result < 0 or result > 16 * 1024**3:
        raise Refusal(f"APT metadata {description} is outside its bound")
    return result


def _apt_metadata(solver: Mapping[str, str], runner: Runner) -> tuple[int, int]:
    requests = list(sorted(solver.items()))
    observed: dict[tuple[str, str], tuple[int, int]] = {}
    for offset in range(0, len(requests), APT_METADATA_BATCH_SIZE):
        batch = requests[offset : offset + APT_METADATA_BATCH_SIZE]
        result = runner.run(
            [
                "/usr/bin/apt-cache",
                "show",
                *[f"{package}={version}" for package, version in batch],
            ],
            timeout=COMMAND_TIMEOUT_SECONDS,
            output_limit=MAX_APT_COMMAND_BYTES,
        )
        for paragraph in re.split(r"\n[ \t]*\n", result.stdout.strip()):
            if not paragraph:
                continue
            fields: dict[str, str] = {}
            for line in paragraph.splitlines():
                if line.startswith((" ", "\t")):
                    continue
                key, separator, value = line.partition(":")
                if separator and key in {"Package", "Version", "Installed-Size", "Size"}:
                    if key in fields:
                        raise Refusal("APT metadata record has duplicate identity fields")
                    fields[key] = value.strip()
            if set(fields) != {"Package", "Version", "Installed-Size", "Size"}:
                raise Refusal("APT metadata record lacks exact size identity")
            identity = (fields["Package"], fields["Version"])
            if identity in observed:
                raise Refusal("APT metadata returned duplicate package/version records")
            observed[identity] = (
                _apt_decimal(fields["Size"], "archive size"),
                _apt_decimal(fields["Installed-Size"], "installed size") * 1024,
            )

    expected = {(package.split(":", 1)[0], version) for package, version in requests}
    if len(expected) != len(requests) or set(observed) != expected:
        raise Refusal("APT metadata differs from the exact solver plan")
    download_bytes = sum(value[0] for value in observed.values())
    installed_bytes = sum(value[1] for value in observed.values())
    if download_bytes > 16 * 1024**3 or installed_bytes > 16 * 1024**3:
        raise Refusal("APT aggregate size is outside its bound")
    return download_bytes, installed_bytes


def _apt_simulation(missing: Mapping[str, str], runner: Runner) -> dict[str, object]:
    if not missing:
        return {
            "solver_packages": {},
            "download_bytes": 0,
            "installed_bytes": 0,
            "peak_bytes": 0,
        }
    arguments = [f"{package}={version}" for package, version in sorted(missing.items())]
    result = runner.run(
        [
            "/usr/bin/apt-get",
            "--simulate",
            "--no-install-recommends",
            "install",
            *arguments,
        ],
        timeout=APT_TIMEOUT_SECONDS,
        output_limit=MAX_APT_COMMAND_BYTES,
    )
    output = f"{result.stdout}\n{result.stderr}"
    summaries = re.findall(
        r"([0-9]+) upgraded, ([0-9]+) newly installed, ([0-9]+) to remove",
        output,
    )
    if len(summaries) != 1 or summaries[0][0] != "0" or summaries[0][2] != "0":
        raise Refusal("APT simulation would upgrade/remove packages or lacks one summary")
    solver_pairs = re.findall(
        r"^Inst ([a-z0-9][a-z0-9+.-]*(?::[a-z0-9]+)?) \((\S+)(?: [^)]*)?\)$",
        output,
        flags=re.MULTILINE,
    )
    solver: dict[str, str] = {}
    for package, version in solver_pairs:
        if package in solver:
            raise Refusal("APT simulation returned a duplicate solver package")
        solver[package] = version
    newly_installed = int(summaries[0][1])
    if not solver or len(solver) != newly_installed:
        raise Refusal("APT simulation package plan differs from its summary")
    normalized_solver = {package.split(":", 1)[0]: version for package, version in solver.items()}
    if len(normalized_solver) != len(solver) or any(
        normalized_solver.get(package) != version for package, version in missing.items()
    ):
        raise Refusal("APT solver changed an exact requested package/version")
    download_bytes, installed_bytes = _apt_metadata(solver, runner)
    return {
        "solver_packages": dict(sorted(solver.items())),
        "download_bytes": download_bytes,
        "installed_bytes": installed_bytes,
        "peak_bytes": download_bytes + installed_bytes,
    }


def _validate_approved_plan(approved_path: Path, current: Mapping[str, object]) -> None:
    approved = _strict_json(_safe_read(approved_path, MAX_MANIFEST_BYTES), "approved dry-run")
    if approved.get("mode") != "dry-run" or approved.get("ready") is not True:
        raise Refusal("approved plan is not one successful dry-run")
    ignored = {"mode", "root_free_before_bytes", "projected_root_free_bytes"}
    if {key: value for key, value in approved.items() if key not in ignored} != {
        key: value for key, value in current.items() if key not in ignored
    }:
        raise Refusal("approved dry-run differs from the current exact plan")
    required = approved.get("required_root_free_before_bytes")
    current_free = current.get("root_free_before_bytes")
    if (
        not isinstance(required, int)
        or isinstance(required, bool)
        or not isinstance(current_free, int)
        or isinstance(current_free, bool)
        or current_free < required
    ):
        raise Refusal("current root headroom no longer satisfies the approved plan")


def _atomic_write(path: Path, payload: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        fchmod = cast(
            Callable[[int, int], None] | None,
            getattr(os, "fchmod", None),
        )
        if fchmod is not None:
            fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if fchmod is None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)
        raise


def _atomic_write_managed_file(
    path: Path, payload: bytes, mode: int, expected_sha256: str | None
) -> None:
    """Atomically replace a managed file only after a final bound-state check."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        fchmod = cast(
            Callable[[int, int], None] | None,
            getattr(os, "fchmod", None),
        )
        if fchmod is not None:
            fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if fchmod is None:
            os.chmod(temporary, mode)
        if _managed_target_hash(path) != expected_sha256:
            raise Refusal(f"managed file prestate drifted before replacement: {path}")
        os.replace(temporary, path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)
        raise


def _set_owner(path: Path, uid: int, gid: int, *, follow_symlinks: bool = True) -> None:
    if os.name != "posix":
        return
    chown = cast(
        Callable[..., None],
        getattr(os, "chown"),  # noqa: B009 - target-only API in Windows typeshed
    )
    chown(path, uid, gid, follow_symlinks=follow_symlinks)


def _ensure_directory(path: Path, mode: int) -> None:
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise Refusal(f"managed directory path is foreign: {path}")
    else:
        path.mkdir(mode=mode)
    os.chmod(path, mode)
    _set_owner(path, 0, 0)


def _normalize_release_tree(staging: Path) -> None:
    """Make a root-owned release traversable, but never mutable, by services."""

    for directory, child_directories, child_files in os.walk(staging, followlinks=False):
        directory_path = Path(directory)
        info = directory_path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise Refusal("release directory tree contains an unsafe entry")
        os.chmod(directory_path, 0o755)
        _set_owner(directory_path, 0, 0)
        for name in [*child_directories, *child_files]:
            path = directory_path / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                relative = path.relative_to(staging).as_posix()
                target = os.readlink(path)
                allowed = (
                    relative.startswith("venv/bin/")
                    and (
                        (
                            not target.startswith("/")
                            and "/" not in target
                            and target not in {"", ".", ".."}
                        )
                        or re.fullmatch(r"/usr/bin/python3(?:\.[0-9]+)?", target) is not None
                    )
                ) or (relative == "venv/lib64" and target == "lib")
                if not allowed:
                    raise Refusal("release tree contains an unexpected symlink")
                _set_owner(path, 0, 0, follow_symlinks=False)
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                if stat.S_ISDIR(info.st_mode):
                    continue
                raise Refusal("release tree contains a special or multiply-linked file")
            mode = 0o755 if info.st_mode & 0o111 else 0o644
            os.chmod(path, mode)
            _set_owner(path, 0, 0)


def _verify_release_venv(release: Path) -> None:
    venv = release / "venv"
    interpreter = venv / "bin/python"
    if not interpreter.is_file():
        raise Refusal("existing release environment is incomplete")
    try:
        config = _safe_read(venv / "pyvenv.cfg", 8192).decode("ascii")
    except UnicodeDecodeError as exc:
        raise Refusal("release environment configuration is not ASCII") from exc
    if config.splitlines().count("include-system-site-packages = true") != 1:
        raise Refusal("release environment lacks reviewed system site packages")


def _staging_release_smoke(runner: Runner, venv_python: str) -> None:
    """Import the installed application and verify GI/Gst without opening hardware."""

    runner.run_release_python_as_service_user(
        venv_python,
        ["-c", STAGING_RELEASE_SMOKE],
        timeout=GSTREAMER_SMOKE_TIMEOUT_SECONDS,
        output_limit=GSTREAMER_SMOKE_MAX_OUTPUT_BYTES,
    )


def _install_release(
    bundle: Path, root: Path, manifest: Mapping[str, object], runner: Runner
) -> Path:
    opt = root / "opt/dashcam"
    releases = opt / "releases"
    _ensure_directory(root / "opt", 0o755)
    _ensure_directory(opt, 0o755)
    _ensure_directory(releases, 0o755)
    release_id = str(manifest["release_id"])
    release = releases / release_id
    marker_payload = (
        json.dumps(
            {
                "schema_version": 1,
                "release_id": release_id,
                "manifest_sha256": manifest["_manifest_sha256"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("ascii")
    marker = release / "installed.json"
    if release.exists() or release.is_symlink():
        info = release.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise Refusal("release path is foreign")
        if _safe_read(marker, 8192) != marker_payload:
            raise Refusal("existing release marker differs")
        _verify_release_venv(release)
        _normalize_release_tree(release)
        return release

    staging = releases / f".staging-{release_id}-{os.getpid()}"
    if staging.exists() or staging.is_symlink():
        raise Refusal("release staging path already exists")
    staging.mkdir(mode=0o700)
    try:
        wheelhouse = staging / "wheelhouse"
        wheelhouse.mkdir(mode=0o700)
        application = manifest["application"]
        tzdata = manifest["tzdata"]
        assert isinstance(application, Mapping) and isinstance(tzdata, Mapping)
        wheel_relatives = (str(tzdata["wheel"]), str(application["wheel"]))
        installed_wheels: list[str] = []
        for relative in wheel_relatives:
            source = bundle / relative
            target = wheelhouse / Path(relative).name
            shutil.copyfile(source, target, follow_symlinks=False)
            os.chmod(target, 0o600)
            installed_wheels.append(str(target))
        runner.run(
            [
                "/usr/bin/python3",
                "-m",
                "venv",
                "--system-site-packages",
                str(staging / "venv"),
            ]
        )
        venv_python = str(staging / "venv/bin/python")
        # The venv interpreter is the only dynamic executable allowed here.
        runner.run_release_python(
            venv_python,
            [
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-index",
                "--no-deps",
                "--no-cache-dir",
                *installed_wheels,
            ],
        )
        shutil.rmtree(wheelhouse)
        _verify_release_venv(staging)
        # The smoke runs as ``dashcam``: make the just-built immutable tree
        # service-traversable before proving its imports, never after activation.
        _normalize_release_tree(staging)
        _staging_release_smoke(runner, venv_python)
        _atomic_write(staging / "installed.json", marker_payload, 0o600)
        _normalize_release_tree(staging)
        os.replace(staging, release)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return release


def _switch_current(root: Path, release: Path) -> None:
    current = root / "opt/dashcam/current"
    target = f"releases/{release.name}"
    if current.exists() or current.is_symlink():
        info = current.lstat()
        if not stat.S_ISLNK(info.st_mode):
            raise Refusal("current release path is foreign")
        existing = os.readlink(current)
        if existing == target:
            _set_owner(current, 0, 0, follow_symlinks=False)
            return
        if (
            not existing.startswith("releases/")
            or "/" in existing[len("releases/") :]
            or not (current.parent / existing / "installed.json").is_file()
        ):
            raise Refusal("current release symlink is foreign")
    temporary = current.with_name(f".current-{os.getpid()}")
    if temporary.exists() or temporary.is_symlink():
        raise Refusal("current symlink staging path exists")
    os.symlink(target, temporary)
    _set_owner(temporary, 0, 0, follow_symlinks=False)
    os.replace(temporary, current)


def _install_managed_file(
    source: Path,
    target: Path,
    mode: int,
    *,
    expected_sha256: str | None,
    desired_sha256: str,
    uid: int = 0,
    gid: int = 0,
) -> None:
    payload = _safe_read(source)
    if _sha256(payload) != desired_sha256:
        raise Refusal(f"managed source hash differs from the approved plan: {source}")
    observed_sha256 = _managed_target_hash(target)
    if observed_sha256 != expected_sha256:
        raise Refusal(f"managed file prestate drifted before replacement: {target}")
    if observed_sha256 == desired_sha256:
        os.chmod(target, mode)
        _set_owner(target, uid, gid)
        return
    _atomic_write_managed_file(target, payload, mode, expected_sha256)
    _set_owner(target, uid, gid)


def _verify_installed_permissions(
    root: Path, release: Path, storage_gid: int, runner: Runner
) -> None:
    expected = (
        (release, 0o755, 0, 0, True),
        (root / "var/lib/dashcam/network", 0o755, 0, 0, True),
        (root / "etc/dashcam/config.toml", 0o640, 0, storage_gid, False),
        *((root / "etc/systemd/system" / name, 0o644, 0, 0, False) for name in MANAGED_UNITS),
    )
    for path, mode, uid, gid, directory in expected:
        info = path.lstat()
        if (
            stat.S_IMODE(info.st_mode) != mode
            or info.st_uid != uid
            or info.st_gid != gid
            or (directory and not stat.S_ISDIR(info.st_mode))
            or (not directory and not stat.S_ISREG(info.st_mode))
        ):
            raise Refusal(f"installed ownership or mode differs: {path}")
    current = root / "opt/dashcam/current"
    current_info = current.lstat()
    if (
        not stat.S_ISLNK(current_info.st_mode)
        or current_info.st_uid != 0
        or current_info.st_gid != 0
    ):
        raise Refusal("current release link ownership differs")
    for option, access_path in (
        ("-x", "/opt/dashcam/current/venv/bin/python"),
        ("-r", "/etc/dashcam/config.toml"),
        ("-r", "/etc/dashcam/storage-volume.env"),
    ):
        runner.run(
            [
                "/usr/bin/setpriv",
                "--reuid=dashcam",
                "--regid=dashcam",
                "--init-groups",
                "/usr/bin/test",
                option,
                access_path,
            ]
        )


def _group_gid(payload: str, name: str) -> int:
    lines = [line for line in payload.splitlines() if line]
    if len(lines) != 1:
        raise Refusal(f"group identity is ambiguous: {name}")
    fields = lines[0].split(":")
    if (
        len(fields) != 4
        or fields[0] != name
        or not fields[2].isdecimal()
        or not 1 <= int(fields[2]) <= 2**31 - 1
    ):
        raise Refusal(f"group identity differs: {name}")
    return int(fields[2])


def _service_user_primary_gid(payload: str) -> int:
    lines = [line for line in payload.splitlines() if line]
    if len(lines) != 1:
        raise Refusal("service account identity is ambiguous")
    fields = lines[0].split(":")
    if (
        len(fields) != 7
        or fields[0] != SERVICE_USER
        or not fields[1]
        or not fields[2].isdecimal()
        or not fields[3].isdecimal()
        or not 1 <= int(fields[2]) <= 2**31 - 1
        or not 1 <= int(fields[3]) <= 2**31 - 1
        or fields[5] != SERVICE_HOME
        or fields[6] != SERVICE_SHELL
    ):
        raise Refusal("service account identity differs")
    return int(fields[3])


def _group_members(payload: str, name: str) -> set[str]:
    lines = [line for line in payload.splitlines() if line]
    if len(lines) != 1:
        raise Refusal(f"group identity is ambiguous: {name}")
    fields = lines[0].split(":")
    members = [] if fields[-1:] == [""] else fields[-1].split(",")
    if (
        len(fields) != 4
        or fields[0] != name
        or not fields[2].isdecimal()
        or not 1 <= int(fields[2]) <= 2**31 - 1
        or len(members) != len(set(members))
        or any(ACCOUNT_RE.fullmatch(member) is None for member in members)
    ):
        raise Refusal(f"group identity differs: {name}")
    return set(members)


def _dashcam_video_membership(runner: Runner) -> bool:
    account = runner.run(["/usr/bin/getent", "passwd", SERVICE_USER])
    primary_gid = _service_user_primary_gid(account.stdout)
    service_group = runner.run(["/usr/bin/getent", "group", SERVICE_USER])
    if _group_gid(service_group.stdout, SERVICE_USER) != primary_gid:
        raise Refusal("service account primary group differs")
    video = runner.run(["/usr/bin/getent", "group", VIDEO_GROUP])
    return SERVICE_USER in _group_members(video.stdout, VIDEO_GROUP)


def _ensure_dashcam_video_membership(runner: Runner) -> None:
    if _dashcam_video_membership(runner):
        return
    runner.run(["/usr/sbin/usermod", "--append", "--groups", VIDEO_GROUP, SERVICE_USER])
    if not _dashcam_video_membership(runner):
        raise Refusal("service account remains absent from the video group")


def install(
    bundle: Path,
    *,
    apply: bool,
    approved_plan: Path | None = None,
    root: Path = Path("/"),
    runner: Runner | None = None,
) -> dict[str, object]:
    command_runner = runner or Runner()
    if root != Path("/"):
        raise Refusal("alternate target roots are not supported")
    geteuid = cast(Callable[[], int], getattr(os, "geteuid", lambda: -1))
    if os.name != "posix" or not Path("/proc/1/stat").is_file() or geteuid() != 0:
        raise Refusal("installer requires root on the live Linux Pi")
    manifest = _manifest(bundle)
    _os_release(_read_os_release(root))
    architecture = command_runner.run(["/usr/bin/dpkg", "--print-architecture"]).stdout.strip()
    if architecture != "armhf":
        raise Refusal("target architecture is not armhf")
    dashcam_video_member = _dashcam_video_membership(command_runner)
    storage_group = command_runner.run(["/usr/bin/getent", "group", "dashcam-storage"])
    storage_gid = _group_gid(storage_group.stdout, "dashcam-storage")
    storage = _verify_storage(root, command_runner, storage_gid)
    systemd_states = _systemd_state(root, command_runner)
    systemd_activity = _systemd_activity(systemd_states, command_runner)
    managed_file_hashes_before, managed_file_hashes = _managed_prestate(bundle, root, manifest)
    packages_value = manifest["apt_packages"]
    assert isinstance(packages_value, list) and all(
        isinstance(item, str) for item in packages_value
    )
    packages = cast(list[str], packages_value)
    installed, missing = _package_plan(packages, command_runner)
    apt_simulation = _apt_simulation(missing, command_runner)
    free_before = _free_bytes(root)
    budget_value = manifest["install_budget_bytes"]
    assert isinstance(budget_value, int)
    budget = budget_value
    apt_peak_value = apt_simulation["peak_bytes"]
    if not isinstance(apt_peak_value, int) or isinstance(apt_peak_value, bool):
        raise Refusal("APT simulation peak size is malformed")
    apt_peak_bytes = apt_peak_value
    projected = _projected_headroom(free_before, budget, apt_peak_bytes)
    required_free = MINIMUM_REMAINING_BYTES + budget + apt_peak_bytes
    result: dict[str, object] = {
        "schema_version": 1,
        "mode": "apply" if apply else "dry-run",
        "ready": True,
        "release_id": manifest["release_id"],
        "manifest_sha256": manifest["_manifest_sha256"],
        "storage_uuid_suffix": storage["DASHCAM_STORAGE_UUID"][-4:],
        "root_free_before_bytes": free_before,
        "projected_root_free_bytes": projected,
        "required_root_free_before_bytes": required_free,
        "apt_simulation": apt_simulation,
        "installed_packages": installed,
        "missing_package_candidates": missing,
        "systemd_states_before": systemd_states,
        "systemd_activity_before": systemd_activity,
        "managed_file_hashes_before": managed_file_hashes_before,
        "managed_file_hashes": managed_file_hashes,
        "services_to_enable": list(MANAGED_UNITS),
        "services_to_start": [],
        "dormant_services": list(DORMANT_UNITS),
        "dashcam_video_group_member_before": dashcam_video_member,
    }
    if not apply:
        if approved_plan is not None:
            raise Refusal("dry-run does not accept an approved plan")
        return result
    if approved_plan is None or not approved_plan.is_absolute():
        raise Refusal("apply requires one absolute approved dry-run plan")
    _validate_approved_plan(approved_plan, result)

    if missing:
        current_installed, current_missing = _package_plan(packages, command_runner)
        current_simulation = _apt_simulation(current_missing, command_runner)
        if (
            current_installed != installed
            or current_missing != missing
            or current_simulation != apt_simulation
        ):
            raise Refusal("APT state drifted after the approved preflight")
        try:
            command_runner.run(
                [
                    "/usr/bin/apt-get",
                    "--quiet=2",
                    "install",
                    "--yes",
                    "--no-install-recommends",
                    "--no-upgrade",
                    *[f"{package}={version}" for package, version in sorted(missing.items())],
                ],
                timeout=APT_TIMEOUT_SECONDS,
                output_limit=MAX_APT_COMMAND_BYTES,
            )
        finally:
            command_runner.run(
                ["/usr/bin/apt-get", "clean"],
                timeout=APT_TIMEOUT_SECONDS,
                output_limit=MAX_APT_COMMAND_BYTES,
            )
    _ensure_dashcam_video_membership(command_runner)
    release = _install_release(bundle, root, manifest, command_runner)
    for managed in _managed_files(bundle, root):
        _install_managed_file(
            managed.source,
            managed.target,
            managed.mode,
            expected_sha256=managed_file_hashes_before[managed.name],
            desired_sha256=managed_file_hashes[managed.name],
            uid=managed.uid,
            gid=storage_gid if managed.name == "config.toml" else managed.gid,
        )
    _ensure_directory(root / "var/lib", 0o755)
    _ensure_directory(root / "var/lib/dashcam", 0o755)
    _ensure_directory(root / "var/lib/dashcam/network", 0o755)
    _switch_current(root, release)
    for unit_name in MANAGED_UNITS:
        command_runner.run(
            ["/usr/bin/systemd-analyze", "verify", str(root / "etc/systemd/system" / unit_name)]
        )
    _verify_installed_permissions(root, release, storage_gid, command_runner)
    command_runner.run(["/usr/bin/systemctl", "daemon-reload"])
    for unit_name in MANAGED_UNITS:
        command_runner.run(["/usr/bin/systemctl", "enable", unit_name])

    final_installed, final_missing = _package_plan(packages, command_runner)
    expected_final = {**installed, **missing}
    if final_missing or final_installed != expected_final:
        raise Refusal("final APT inventory is incomplete")
    free_after = _free_bytes(root)
    if free_after < MINIMUM_REMAINING_BYTES:
        raise Refusal("final root headroom is below 2 GiB")
    evidence = {
        **result,
        "mode": "applied",
        "root_free_after_bytes": free_after,
        "installed_packages": final_installed,
        "missing_package_candidates": {},
    }
    _atomic_write(
        root / "var/lib/dashcam/app-install-v1.json",
        (json.dumps(evidence, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii"),
        0o600,
    )
    _set_owner(root / "var/lib/dashcam/app-install-v1.json", 0, 0)
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--apply", action="store_true")
    parser.add_argument("--bundle", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--approved-plan", type=Path)
    arguments = parser.parse_args(argv)
    if arguments.apply != (arguments.approved_plan is not None):
        parser.error("--apply requires --approved-plan; --dry-run forbids it")
    if arguments.approved_plan is not None and not arguments.approved_plan.is_absolute():
        parser.error("--approved-plan must be absolute")
    try:
        result = install(
            arguments.bundle.resolve(),
            apply=arguments.apply,
            approved_plan=(
                arguments.approved_plan if arguments.approved_plan is not None else None
            ),
        )
    except (OSError, Refusal, ValueError) as exc:
        print(
            json.dumps(
                {"schema_version": 1, "ready": False, "outcome": "refused", "reason": str(exc)},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
