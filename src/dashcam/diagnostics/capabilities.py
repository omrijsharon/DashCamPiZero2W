"""Bounded, read-only Raspberry Pi capability evidence collection.

The collector deliberately knows every command and file it may inspect.  It does
not use a shell, discover device nodes dynamically, open media/audio/UART devices
directly, or interpret fixture results as proof of hardware capability.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Final, Literal, Protocol, cast

from dashcam.version import get_build_info

SectionName = Literal[
    "target_identity",
    "os",
    "hardware",
    "media",
    "audio",
    "uart",
    "storage",
    "thermal",
]
ProbeState = Literal[
    "observed",
    "not_probed",
    "unknown",
    "unavailable",
    "error",
    "not_applicable",
]
TargetKind = Literal["raspberry_pi", "local_fixture", "windows_host", "other"]

SECTION_NAMES: Final[tuple[SectionName, ...]] = (
    "target_identity",
    "os",
    "hardware",
    "media",
    "audio",
    "uart",
    "storage",
    "thermal",
)
COMMAND_TIMEOUT_SECONDS: Final = 5.0
MAX_COMMAND_OUTPUT_BYTES: Final = 16 * 1024
MAX_FILE_READ_BYTES: Final = 16 * 1024
MAX_FACT_VALUE_CHARS: Final = 16 * 1024
MAX_WARNING_CHARS: Final = 512


@dataclass(frozen=True, slots=True)
class CommandProbe:
    """One fixed, allowlisted command probe."""

    probe_id: str
    section: SectionName
    argv: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FileProbe:
    """One fixed, allowlisted regular/pseudo-file read."""

    probe_id: str
    section: SectionName
    path: str


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Bounded raw result returned by a probe adapter."""

    state: ProbeState
    value: str
    note: str | None = None
    truncated: bool = False


class CommandRunner(Protocol):
    """Injectable command boundary used by the collector."""

    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> ProbeResult:
        """Run one fixed command without a shell."""


class FileReader(Protocol):
    """Injectable bounded file-read boundary used by the collector."""

    def read(self, path: str, *, max_bytes: int) -> ProbeResult:
        """Read one fixed path without following application-controlled input."""


COMMAND_PROBES: Final[tuple[CommandProbe, ...]] = (
    CommandProbe("os.uname", "os", ("uname", "-srvmo")),
    CommandProbe("os.architecture", "os", ("uname", "-m")),
    CommandProbe("os.python", "os", ("python3", "--version")),
    CommandProbe("os.systemd", "os", ("systemctl", "--version")),
    CommandProbe("media.rpicam.cameras", "media", ("rpicam-hello", "--list-cameras")),
    CommandProbe("media.rpicam.version", "media", ("rpicam-hello", "--version")),
    CommandProbe("media.libcamera.cameras", "media", ("libcamera-hello", "--list-cameras")),
    CommandProbe("media.gstreamer.version", "media", ("gst-inspect-1.0", "--version")),
    CommandProbe(
        "media.gstreamer.libcamera",
        "media",
        ("gst-inspect-1.0", "--exists", "libcamerasrc"),
    ),
    CommandProbe(
        "media.gstreamer.h264",
        "media",
        ("gst-inspect-1.0", "--exists", "v4l2h264enc"),
    ),
    CommandProbe("media.ffmpeg.version", "media", ("ffmpeg", "-version")),
    CommandProbe(
        "media.ffmpeg.encoders",
        "media",
        ("ffmpeg", "-hide_banner", "-encoders"),
    ),
    CommandProbe("media.ffprobe.version", "media", ("ffprobe", "-version")),
    CommandProbe("media.v4l2.nodes", "media", ("v4l2-ctl", "--list-devices")),
    CommandProbe("audio.capture.devices", "audio", ("arecord", "--list-devices")),
    CommandProbe("audio.capture.pcms", "audio", ("arecord", "--list-pcms")),
    CommandProbe("uart.serial0.mapping", "uart", ("readlink", "-e", "/dev/serial0")),
    CommandProbe(
        "storage.block.layout",
        "storage",
        (
            "lsblk",
            "--json",
            "--output",
            "NAME,KNAME,TYPE,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINTS,RO",
        ),
    ),
    CommandProbe(
        "storage.recording.mount",
        "storage",
        (
            "findmnt",
            "--json",
            "--output",
            "SOURCE,TARGET,FSTYPE,OPTIONS,LABEL,UUID",
            "/srv/dashcam",
        ),
    ),
    CommandProbe(
        "storage.recording.space",
        "storage",
        (
            "df",
            "--output=source,fstype,size,used,avail,pcent,target",
            "-B1",
            "/srv/dashcam",
        ),
    ),
    CommandProbe("storage.exfat.module", "storage", ("modinfo", "exfat")),
    CommandProbe("storage.exfat.fsck", "storage", ("fsck.exfat", "-V")),
    CommandProbe("storage.exfat.mkfs", "storage", ("mkfs.exfat", "-V")),
    CommandProbe("thermal.throttled", "thermal", ("vcgencmd", "get_throttled")),
    CommandProbe("thermal.temperature", "thermal", ("vcgencmd", "measure_temp")),
)

_EMPTY_SUCCESS_COMMANDS: Final[frozenset[tuple[str, ...]]] = frozenset(
    {
        ("gst-inspect-1.0", "--exists", "libcamerasrc"),
        ("gst-inspect-1.0", "--exists", "v4l2h264enc"),
    }
)

FILE_PROBES: Final[tuple[FileProbe, ...]] = (
    FileProbe("target.model", "target_identity", "/proc/device-tree/model"),
    FileProbe("os.release", "os", "/etc/os-release"),
    FileProbe("hardware.cpuinfo", "hardware", "/proc/cpuinfo"),
    FileProbe("hardware.meminfo", "hardware", "/proc/meminfo"),
    FileProbe("uart.kernel.cmdline", "uart", "/proc/cmdline"),
    FileProbe("uart.boot.config", "uart", "/boot/firmware/config.txt"),
    FileProbe("uart.legacy.boot.config", "uart", "/boot/config.txt"),
    FileProbe("storage.filesystems", "storage", "/proc/filesystems"),
    FileProbe("thermal.sysfs.temperature", "thermal", "/sys/class/thermal/thermal_zone0/temp"),
)

_SECRET_ASSIGNMENT: Final = re.compile(
    r"(?i)\b(password|passwd|passphrase|psk|token|secret|api[_-]?key)"
    r"(\s*[:=]\s*)([^\r\n,;]+)"
)
_URI_CREDENTIALS: Final = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")
_PEM_BLOCK: Final = re.compile(
    r"-----BEGIN [^-]*(?:PRIVATE KEY|SECRET)[^-]*-----.*?"
    r"-----END [^-]*(?:PRIVATE KEY|SECRET)[^-]*-----",
    re.IGNORECASE | re.DOTALL,
)


def redact_text(value: str) -> str:
    """Remove common secret forms before evidence enters the report."""

    value = _PEM_BLOCK.sub("[REDACTED PRIVATE MATERIAL]", value)
    value = _URI_CREDENTIALS.sub(r"\1[REDACTED]@", value)
    return _SECRET_ASSIGNMENT.sub(r"\1\2[REDACTED]", value)


def _decode_bounded(data: bytes, maximum: int) -> tuple[str, bool]:
    truncated = len(data) > maximum
    return data[:maximum].decode("utf-8", errors="replace"), truncated


class SubprocessCommandRunner:
    """Production adapter with a deadline and bounded stdout/stderr buffers."""

    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> ProbeResult:
        if argv not in {probe.argv for probe in COMMAND_PROBES}:
            return ProbeResult("error", "", "command is not allowlisted")
        if timeout_seconds <= 0 or timeout_seconds > COMMAND_TIMEOUT_SECONDS:
            return ProbeResult("error", "", "invalid command timeout")
        if max_output_bytes <= 0 or max_output_bytes > MAX_COMMAND_OUTPUT_BYTES:
            return ProbeResult("error", "", "invalid output bound")

        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                close_fds=True,
            )
        except FileNotFoundError:
            return ProbeResult("unavailable", "", f"executable not found: {argv[0]}")
        except OSError as error:
            return ProbeResult("error", "", f"command start failed: {type(error).__name__}")

        stdout_parts: list[bytes] = []
        stderr_parts: list[bytes] = []
        overflow = threading.Event()

        def drain(stream: IO[bytes], destination: list[bytes]) -> None:
            remaining = max_output_bytes
            while remaining >= 0:
                chunk = stream.read(min(4096, remaining + 1))
                if not chunk:
                    return
                keep = chunk[:remaining]
                if keep:
                    destination.append(keep)
                    remaining -= len(keep)
                if len(chunk) > len(keep):
                    overflow.set()
                    with suppress(OSError):
                        process.kill()
                    return

        assert process.stdout is not None
        assert process.stderr is not None
        threads = (
            threading.Thread(target=drain, args=(process.stdout, stdout_parts), daemon=True),
            threading.Thread(target=drain, args=(process.stderr, stderr_parts), daemon=True),
        )
        for thread in threads:
            thread.start()

        timed_out = False
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            with suppress(OSError):
                process.kill()
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=1.0)
        finally:
            for thread in threads:
                thread.join(timeout=1.0)
            process.stdout.close()
            process.stderr.close()

        stdout, stdout_truncated = _decode_bounded(b"".join(stdout_parts), max_output_bytes)
        stderr, stderr_truncated = _decode_bounded(b"".join(stderr_parts), max_output_bytes)
        combined = stdout
        if stderr:
            combined = f"{combined}\n[stderr]\n{stderr}" if combined else f"[stderr]\n{stderr}"
        combined = redact_text(combined.strip("\x00\r\n"))
        truncated = overflow.is_set() or stdout_truncated or stderr_truncated

        if timed_out:
            return ProbeResult("error", combined, "command timed out", truncated)
        if truncated:
            return ProbeResult("error", combined, "command output exceeded bound", True)
        if process.returncode != 0:
            return ProbeResult(
                "error",
                combined,
                f"command exited with status {process.returncode}",
            )
        if not combined and argv in _EMPTY_SUCCESS_COMMANDS:
            return ProbeResult("observed", "")
        if not combined:
            return ProbeResult("unknown", "", "command returned no output")
        return ProbeResult("observed", combined)


class BoundedFileReader:
    """Production adapter that reads only allowlisted paths and never writes."""

    def read(self, path: str, *, max_bytes: int) -> ProbeResult:
        if path not in {probe.path for probe in FILE_PROBES}:
            return ProbeResult("error", "", "file path is not allowlisted")
        if max_bytes <= 0 or max_bytes > MAX_FILE_READ_BYTES:
            return ProbeResult("error", "", "invalid file-read bound")
        try:
            with Path(path).open("rb") as source:
                data = source.read(max_bytes + 1)
        except FileNotFoundError:
            return ProbeResult("unavailable", "", "file not found")
        except (OSError, ValueError) as error:
            return ProbeResult("error", "", f"file read failed: {type(error).__name__}")

        value, truncated = _decode_bounded(data, max_bytes)
        value = redact_text(value.strip("\x00\r\n"))
        if truncated:
            return ProbeResult("error", value, "file content exceeded bound", True)
        if not value:
            return ProbeResult("unknown", "", "file was empty")
        return ProbeResult("observed", value)


def _bounded(value: str, maximum: int) -> tuple[str, bool]:
    if len(value) <= maximum:
        return value, False
    return value[:maximum], True


def _warning(code: str, message: str) -> dict[str, str]:
    bounded_message, _ = _bounded(redact_text(message), MAX_WARNING_CHARS)
    return {"code": code, "message": bounded_message or "unspecified probe warning"}


def _observation(
    probe_id: str,
    result: ProbeResult,
    source: str,
) -> tuple[dict[str, object], dict[str, str] | None]:
    value, report_truncated = _bounded(redact_text(result.value), MAX_FACT_VALUE_CHARS)
    note = result.note
    warning: dict[str, str] | None = None
    if result.truncated or report_truncated:
        suffix = "raw evidence truncated to report bounds"
        note = f"{note}; {suffix}" if note else suffix
    if result.state in {"error", "unavailable"}:
        warning = _warning(f"{probe_id}.state", note or result.state)

    observation: dict[str, object] = {
        "id": probe_id,
        "state": result.state,
        "key": "raw_output",
        "value": value or None,
        "source": source[:256],
    }
    if note:
        bounded_note, _ = _bounded(redact_text(note), 256)
        observation["note"] = bounded_note
    return observation, warning


def _section_state(facts: Sequence[Mapping[str, object]]) -> ProbeState:
    states = {str(fact["state"]) for fact in facts}
    if "observed" in states:
        return "observed"
    if "error" in states:
        return "error"
    if "unknown" in states:
        return "unknown"
    if "unavailable" in states:
        return "unavailable"
    return "not_probed"


def _iso_utc(now: datetime) -> str:
    if now.tzinfo is None:
        raise ValueError("generated timestamp must be timezone-aware")
    return now.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _safe_identifier(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._:-]", ".", value)
    cleaned = cleaned[:128].strip(".")
    return cleaned if cleaned and cleaned[0].isalnum() else "unknown"


def _capability_decisions(
    sections: Mapping[str, Mapping[str, object]],
) -> list[dict[str, object]]:
    definitions = (
        ("camera.enumeration", "media", "Camera modes require target inspection."),
        ("encoder.hardware_h264", "media", "Hardware H.264 requires a measured target test."),
        ("audio.capture", "audio", "Audio compatibility requires the selected USB device."),
        ("uart.gps", "uart", "UART mapping and stability require the target boot configuration."),
        ("storage.exfat", "storage", "exFAT safety requires the final card and mount identity."),
        ("thermal.power", "thermal", "Thermal and power health require a target measurement."),
    )
    decisions: list[dict[str, object]] = []
    for capability_id, section_name, rationale in definitions:
        section = sections[section_name]
        state = str(section["state"])
        decision = "not_probed" if state == "not_probed" else "unknown"
        raw_facts = cast(list[Mapping[str, object]], section["facts"])
        fact_ids = [str(fact["id"]) for fact in raw_facts]
        decisions.append(
            {
                "id": capability_id,
                "decision": decision,
                "basis_observation_ids": fact_ids[:64],
                "rationale": rationale,
            }
        )
    return decisions


def collect_capability_report(
    command_runner: CommandRunner,
    file_reader: FileReader,
    *,
    target_kind: TargetKind = "other",
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Collect a schema-v1 report through bounded, injectable read-only adapters."""

    facts: dict[SectionName, list[dict[str, object]]] = {section: [] for section in SECTION_NAMES}
    warnings: dict[SectionName, list[dict[str, str]]] = {section: [] for section in SECTION_NAMES}

    for command_probe in COMMAND_PROBES:
        result = command_runner.run(
            command_probe.argv,
            timeout_seconds=COMMAND_TIMEOUT_SECONDS,
            max_output_bytes=MAX_COMMAND_OUTPUT_BYTES,
        )
        observation, warning = _observation(
            command_probe.probe_id,
            result,
            " ".join(command_probe.argv),
        )
        facts[command_probe.section].append(observation)
        if warning:
            warnings[command_probe.section].append(warning)

    for file_probe in FILE_PROBES:
        result = file_reader.read(file_probe.path, max_bytes=MAX_FILE_READ_BYTES)
        observation, warning = _observation(
            file_probe.probe_id,
            result,
            file_probe.path,
        )
        facts[file_probe.section].append(observation)
        if warning:
            warnings[file_probe.section].append(warning)

    sections: dict[str, dict[str, object]] = {}
    for section in SECTION_NAMES:
        sections[section] = {
            "state": _section_state(facts[section]),
            "facts": facts[section],
            "evidence_artifact_paths": [],
            "warnings": warnings[section][:64],
        }

    model = next(
        (
            str(fact["value"])
            for fact in facts["target_identity"]
            if fact["id"] == "target.model"
            and fact["state"] == "observed"
            and fact["value"] is not None
        ),
        None,
    )
    inferred_kind: TargetKind = target_kind
    if target_kind == "other" and model and "raspberry pi" in model.casefold():
        inferred_kind = "raspberry_pi"

    fact_states = {str(fact["state"]) for section_facts in facts.values() for fact in section_facts}
    if fact_states == {"not_probed"}:
        report_status = "not_probed"
    elif fact_states <= {"observed"}:
        report_status = "complete"
    elif "observed" in fact_states:
        report_status = "partial"
    else:
        report_status = "failed"

    build = get_build_info()
    producer: dict[str, str] = {
        "name": "dashcam-capability-probe",
        "version": _safe_identifier(build.version),
        "build_id": _safe_identifier(build.build_id),
    }
    if build.git_commit:
        producer["source_revision"] = _safe_identifier(build.git_commit)

    target: dict[str, object] = {
        "kind": inferred_kind,
        "identity_state": sections["target_identity"]["state"],
    }
    if model:
        target["declared_model"] = model[:256]

    global_warnings = [warning for section in SECTION_NAMES for warning in warnings[section]][:128]
    return {
        "schema_version": 1,
        "generated_at_utc": _iso_utc(now()),
        "producer": producer,
        "status": report_status,
        "target": target,
        "raw_observations": sections,
        "evaluated_capabilities": _capability_decisions(sections),
        "evidence_artifacts": [],
        "warnings": global_warnings,
    }


def write_report_exclusive(path: Path, content: str) -> None:
    """Write a new JSON report only at an explicit, non-special path."""

    if path.suffix.casefold() != ".json":
        raise ValueError("output path must end in .json")
    if not path.is_absolute():
        raise ValueError("output path must be absolute")
    resolved = path.resolve(strict=False)
    forbidden_roots = tuple(Path(root) for root in ("/dev", "/proc", "/sys", "/run"))
    if os.name == "posix" and any(
        resolved == root or root in resolved.parents for root in forbidden_roots
    ):
        raise ValueError("output path may not target a special filesystem")
    if not resolved.parent.is_dir():
        raise ValueError("output parent directory must already exist")
    if path.is_symlink() or resolved.exists():
        raise FileExistsError("output path already exists or is a symbolic link")

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(resolved, flags, 0o600)
    try:
        data = content.encode("utf-8")
        offset = 0
        while offset < len(data):
            offset += os.write(descriptor, data[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
