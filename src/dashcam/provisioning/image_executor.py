"""Fail-closed, regular-file-only release-image customization helpers."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, NoReturn

from dashcam.provisioning.image_builder import (
    ImageBuildPlan,
    PayloadFile,
    RawImageVerification,
    SourceImageManifest,
    bind_initramfs_closure,
    inventory_payload,
    validate_output_path,
    verify_raw_image,
    verify_source_archive,
)
from dashcam.provisioning.initramfs_customizer import (
    PI_ZERO_2_W_ARMV7_PROFILE,
    RUNTIME_GATE_CONTENT,
    ZstdRunner,
    customize_initramfs,
    load_initramfs_closure_manifest,
)

DECOMPRESS_TIMEOUT_SECONDS: Final = 15 * 60
MAX_DIAGNOSTIC_BYTES: Final = 64 * 1024
MAX_CMDLINE_BYTES: Final = 16 * 1024
MAX_BOOT_CONFIG_BYTES: Final = 64 * 1024
MAX_COMMAND_OUTPUT_BYTES: Final = 64 * 1024
FILE_COPY_CHUNK_BYTES: Final = 8 * 1024 * 1024
FILE_TOOL_TIMEOUT_SECONDS: Final = 120
STOCK_RESIZE_TOKEN: Final = "resize"
BOUNDED_PROVISION_TOKEN: Final = "dashcam.bounded_provision=v1"
RUNTIME_GATE_RELATIVE_PATH: Final = "etc/dashcam/firstboot-runtime-v1.enabled"
EXACT_CARD_CID: Final = "fe34325344000000200000031a0192d1"
EXACT_CARD_SIZE_BYTES: Final = 31_457_280_000
EXACT_CARD_SECTORS: Final = 61_440_000
AUTHORIZATION_STATEMENT: Final = (
    f"CID {EXACT_CARD_CID} is expendable and may be completely erased, "
    "reflashed, repartitioned, and formatted."
)
MAX_AUTHORIZATION_BYTES: Final = 4 * 1024
EXACT_CARD_MARKER_CONTENT: Final = (
    f"schema=v1\ncid={EXACT_CARD_CID}\ndevice_sectors={EXACT_CARD_SECTORS}\n".encode()
)
SERVICE_LINK_PATH: Final = (
    "/etc/systemd/system/local-fs.target.wants/dashcam-firstboot-storage.service"
)
SERVICE_LINK_TARGET: Final = "../dashcam-firstboot-storage.service"
_MODE_PATTERN: Final = re.compile(rb"\bMode:\s+0*([0-7]{3,6})\b")


class ImageExecutionRefusalCode(StrEnum):
    """Stable refusal codes for the file-only executor boundary."""

    BLOCK_DEVICE_EXECUTION_DISABLED = "block_device_execution_disabled"
    OUTPUT_PATH_UNSAFE = "output_path_unsafe"
    PAYLOAD_BINDING_MISMATCH = "payload_binding_mismatch"
    DEPENDENCY_MISSING = "dependency_missing"
    HOST_EXECUTION_UNPROVEN = "host_execution_unproven"
    INITRAMFS_CLOSURE_UNPROVEN = "initramfs_closure_unproven"
    COMMAND_NOT_ALLOWED = "command_not_allowed"
    COMMAND_FAILED = "command_failed"
    COMMAND_TIMEOUT = "command_timeout"
    CMDLINE_INVALID = "cmdline_invalid"
    OUTPUT_VERIFICATION_FAILED = "output_verification_failed"
    AUTHORIZATION_REQUIRED = "authorization_required"
    AUTHORIZATION_MISMATCH = "authorization_mismatch"


class ImageExecutionRefused(RuntimeError):
    """Raised when a file-image execution invariant is not proven."""

    def __init__(
        self,
        code: ImageExecutionRefusalCode,
        message: str,
        *,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = {} if details is None else details


@dataclass(frozen=True, slots=True)
class ToolRequirement:
    name: str
    accepted_paths: tuple[str, ...]
    purpose: str


@dataclass(frozen=True, slots=True)
class ToolObservation:
    name: str
    path: str | None
    available: bool
    purpose: str


@dataclass(frozen=True, slots=True)
class ExecutionDependencyReport:
    schema_version: int
    host_system: str
    execution_mode: str
    observations: tuple[ToolObservation, ...]
    initramfs_architecture_closure_proven: bool
    execution_supported: bool
    refusal_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "host_system": self.host_system,
            "execution_mode": self.execution_mode,
            "observations": [asdict(item) for item in self.observations],
            "initramfs_architecture_closure_proven": (self.initramfs_architecture_closure_proven),
            "execution_supported": self.execution_supported,
            "refusal_codes": list(self.refusal_codes),
        }


@dataclass(frozen=True, slots=True)
class FileImageExecutionResult:
    schema_version: int
    output: RawImageVerification
    customized: bool
    runtime_gate_created: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "output": asdict(self.output),
            "customized": self.customized,
            "runtime_gate_created": self.runtime_gate_created,
        }


@dataclass(frozen=True, slots=True)
class FileToolResult:
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""


FileToolRunner = Callable[[tuple[str, ...], Path | None, int], FileToolResult]


@dataclass(frozen=True, slots=True)
class ExactCardAuthorization:
    schema_version: int
    cid: str
    size_bytes: int
    statement: str
    file_sha256: str


@dataclass(frozen=True, slots=True)
class RootInstallFile:
    source: Path | None
    destination: str
    content: bytes
    mode: int


_POSIX_REQUIREMENTS: Final = (
    ToolRequirement("xz", ("/usr/bin/xz", "/bin/xz"), "bounded source decompression"),
    ToolRequirement("mcopy", ("/usr/bin/mcopy",), "FAT file extraction and replacement"),
    ToolRequirement("mtype", ("/usr/bin/mtype",), "FAT cmdline verification"),
    ToolRequirement("mdir", ("/usr/bin/mdir",), "FAT directory inspection"),
    ToolRequirement(
        "debugfs",
        ("/usr/sbin/debugfs",),
        "offline regular-file ext4 artifact extraction and authorized customization",
    ),
    ToolRequirement("zstd", ("/usr/bin/zstd", "/bin/zstd"), "deterministic initramfs rebuild"),
)

_AUTHORIZED_PAYLOAD_FILES: Final = (
    (
        "runtime/post-root/dashcam-firstboot-storage",
        "/usr/lib/dashcam/dashcam-firstboot-storage",
        0o755,
    ),
    (
        "runtime/post-root/dashcam-firstboot-storage.service",
        "/etc/systemd/system/dashcam-firstboot-storage.service",
        0o644,
    ),
    (
        "runtime/initramfs/contracts/firstboot-initramfs-v1.conf",
        "/etc/dashcam/firstboot-initramfs-v1.conf",
        0o444,
    ),
    (
        "runtime/initramfs/contracts/source-table-v1.sfdisk",
        "/etc/dashcam/source-table-v1.sfdisk",
        0o444,
    ),
    (
        "runtime/initramfs/contracts/target-table-v1.sfdisk",
        "/etc/dashcam/target-table-v1.sfdisk",
        0o444,
    ),
    (
        "firstboot-contract-v1.json",
        "/etc/dashcam/firstboot-contract-v1.json",
        0o444,
    ),
)
_LAYOUT_SHA256: Final = "4c082acbde590383c28e7a4c6ad26e2e117e61744788796ed7f73c24e65eb1fa"


def probe_execution_dependencies(
    *,
    host_system: str | None = None,
    path_exists: Callable[[Path], bool] | None = None,
    wsl_path_exists: Callable[[str, str], bool] | None = None,
    tool_paths: Mapping[str, Path] | None = None,
) -> ExecutionDependencyReport:
    """Return exact dependency facts without creating or modifying any file.

    Windows reports the WSL tools that can be checked at fixed paths, but still
    refuses execution from the Windows Python process.  The executor must be
    launched inside WSL/Linux so every path and subprocess remains in one
    namespace.
    """

    observed_system = platform.system() if host_system is None else host_system
    exists = Path.is_file if path_exists is None else path_exists
    observations: list[ToolObservation] = []
    if observed_system == "Linux":
        for requirement in _POSIX_REQUIREMENTS:
            explicit = None if tool_paths is None else tool_paths.get(requirement.name)
            selected = (
                str(explicit)
                if explicit is not None and exists(explicit)
                else next(
                    (
                        candidate
                        for candidate in requirement.accepted_paths
                        if exists(Path(candidate))
                    ),
                    None,
                )
            )
            if explicit is not None and not exists(explicit):
                selected = None
            observations.append(
                ToolObservation(
                    requirement.name,
                    selected,
                    selected is not None,
                    requirement.purpose,
                )
            )
        missing = any(not item.available for item in observations)
        refusal_codes: tuple[str, ...] = (
            (ImageExecutionRefusalCode.DEPENDENCY_MISSING.value,) if missing else ()
        )
        return ExecutionDependencyReport(
            1,
            observed_system,
            "native_linux_regular_files_only",
            tuple(observations),
            True,
            not missing,
            refusal_codes,
        )

    if observed_system == "Windows":
        wsl = shutil.which("wsl.exe")
        observations.append(
            ToolObservation("wsl.exe", wsl, wsl is not None, "read-only WSL dependency probe")
        )
        for requirement in _POSIX_REQUIREMENTS:
            selected = None
            if wsl is not None:
                probe = _wsl_path_is_executable if wsl_path_exists is None else wsl_path_exists
                selected = next(
                    (
                        candidate
                        for candidate in requirement.accepted_paths
                        if probe(wsl, candidate)
                    ),
                    None,
                )
            observations.append(
                ToolObservation(
                    requirement.name,
                    selected,
                    selected is not None,
                    f"{requirement.purpose}; observed in the default WSL distribution",
                )
            )
        return ExecutionDependencyReport(
            1,
            observed_system,
            "windows_probe_only_run_executor_inside_wsl",
            tuple(observations),
            False,
            False,
            (
                ImageExecutionRefusalCode.HOST_EXECUTION_UNPROVEN.value,
                ImageExecutionRefusalCode.DEPENDENCY_MISSING.value,
            ),
        )

    return ExecutionDependencyReport(
        1,
        observed_system,
        "unsupported_host",
        (),
        False,
        False,
        (ImageExecutionRefusalCode.HOST_EXECUTION_UNPROVEN.value,),
    )


def bind_payload(plan: ImageBuildPlan, payload_root: Path) -> tuple[PayloadFile, ...]:
    """Re-hash the payload and require byte-for-byte equality with the plan."""

    observed = inventory_payload(payload_root)
    if observed != plan.payload_files:
        _refuse(
            ImageExecutionRefusalCode.PAYLOAD_BINDING_MISMATCH,
            "payload changed after the verified plan was authored",
            details={
                "planned": [asdict(item) for item in plan.payload_files],
                "observed": [asdict(item) for item in observed],
            },
        )
    if any(
        item.relative_path == RUNTIME_GATE_RELATIVE_PATH
        or item.relative_path.endswith("/firstboot-runtime-v1.enabled")
        or item.relative_path == "firstboot-runtime-v1.enabled"
        for item in observed
    ):
        _refuse(
            ImageExecutionRefusalCode.PAYLOAD_BINDING_MISMATCH,
            "payload contains the forbidden exact-image runtime enable gate",
        )
    return observed


def load_exact_card_authorization(path: Path) -> ExactCardAuthorization:
    """Read one closed authorization record bound to the reviewed card."""

    try:
        item = path.lstat()
    except OSError as exc:
        raise ImageExecutionRefused(
            ImageExecutionRefusalCode.AUTHORIZATION_REQUIRED,
            f"cannot inspect exact-card authorization file: {exc}",
        ) from exc
    if stat.S_ISLNK(item.st_mode) or not stat.S_ISREG(item.st_mode):
        _refuse(
            ImageExecutionRefusalCode.AUTHORIZATION_MISMATCH,
            "exact-card authorization must be a regular non-symlink file",
        )
    if item.st_size <= 0 or item.st_size > MAX_AUTHORIZATION_BYTES:
        _refuse(
            ImageExecutionRefusalCode.AUTHORIZATION_MISMATCH,
            "exact-card authorization size is outside its admitted bound",
        )
    payload = path.read_bytes()
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ImageExecutionRefused(
            ImageExecutionRefusalCode.AUTHORIZATION_MISMATCH,
            "exact-card authorization is not valid UTF-8 JSON",
        ) from exc
    if not isinstance(decoded, dict) or set(decoded) != {
        "schema_version",
        "cid",
        "size_bytes",
        "statement",
    }:
        _refuse(
            ImageExecutionRefusalCode.AUTHORIZATION_MISMATCH,
            "exact-card authorization fields are not closed and exact",
        )
    if (
        type(decoded["schema_version"]) is not int
        or decoded["schema_version"] != 1
        or decoded["cid"] != EXACT_CARD_CID
        or type(decoded["size_bytes"]) is not int
        or decoded["size_bytes"] != EXACT_CARD_SIZE_BYTES
        or decoded["statement"] != AUTHORIZATION_STATEMENT
    ):
        _refuse(
            ImageExecutionRefusalCode.AUTHORIZATION_MISMATCH,
            "exact-card authorization does not match the reviewed CID, size, and statement",
        )
    return ExactCardAuthorization(
        1,
        EXACT_CARD_CID,
        EXACT_CARD_SIZE_BYTES,
        AUTHORIZATION_STATEMENT,
        hashlib.sha256(payload).hexdigest(),
    )


def _bind_authorized_root_files(
    plan: ImageBuildPlan,
    payload_root: Path,
    layout_path: Path,
) -> tuple[RootInstallFile, ...]:
    """Bind every root-install byte to the plan or the closed layout digest."""

    planned = {item.relative_path: item for item in plan.payload_files}
    bound: list[RootInstallFile] = []
    for relative, destination, mode in _AUTHORIZED_PAYLOAD_FILES:
        item = planned.get(relative)
        if item is None:
            _refuse(
                ImageExecutionRefusalCode.PAYLOAD_BINDING_MISMATCH,
                f"authorized root payload is absent from the plan: {relative}",
            )
        source = payload_root / Path(*relative.split("/"))
        data = _read_exact_install_source(source, item.sha256, relative)
        bound.append(RootInstallFile(source, destination, data, mode))

    layout = _read_exact_install_source(layout_path, _LAYOUT_SHA256, "layout-v1.toml")
    bound.append(RootInstallFile(layout_path, "/etc/dashcam/layout-v1.toml", layout, 0o444))
    bound.extend(
        (
            RootInstallFile(
                None,
                f"/{RUNTIME_GATE_RELATIVE_PATH}",
                RUNTIME_GATE_CONTENT,
                0o400,
            ),
            RootInstallFile(
                None,
                "/etc/dashcam/expendable-card-v1.authorized",
                EXACT_CARD_MARKER_CONTENT,
                0o400,
            ),
        )
    )
    destinations = [item.destination for item in bound]
    if len(destinations) != len(set(destinations)):
        _refuse(
            ImageExecutionRefusalCode.PAYLOAD_BINDING_MISMATCH,
            "authorized root destinations are not unique",
        )
    return tuple(bound)


def _read_exact_install_source(path: Path, expected_sha256: str, label: str) -> bytes:
    try:
        item = path.lstat()
    except OSError as exc:
        raise ImageExecutionRefused(
            ImageExecutionRefusalCode.PAYLOAD_BINDING_MISMATCH,
            f"cannot inspect authorized root source {label}: {exc}",
        ) from exc
    if stat.S_ISLNK(item.st_mode) or not stat.S_ISREG(item.st_mode):
        _refuse(
            ImageExecutionRefusalCode.PAYLOAD_BINDING_MISMATCH,
            f"authorized root source is not a regular file: {label}",
        )
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        _refuse(
            ImageExecutionRefusalCode.PAYLOAD_BINDING_MISMATCH,
            f"authorized root source SHA-256 drifted: {label}",
        )
    return data


def validate_boot_target_config(payload: bytes) -> None:
    """Require the unambiguous firmware auto-initramfs path used by the target."""

    if len(payload) > MAX_BOOT_CONFIG_BYTES:
        _refuse(
            ImageExecutionRefusalCode.INITRAMFS_CLOSURE_UNPROVEN,
            "config.txt exceeds the admitted bound",
        )
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ImageExecutionRefused(
            ImageExecutionRefusalCode.INITRAMFS_CLOSURE_UNPROVEN,
            "config.txt must be ASCII",
        ) from exc
    if "\x00" in text or "\r" in text:
        _refuse(
            ImageExecutionRefusalCode.INITRAMFS_CLOSURE_UNPROVEN,
            "config.txt contains a NUL or carriage return",
        )
    active_scope = "global"
    auto_initramfs: list[tuple[str, str]] = []
    for source_line in text.splitlines():
        line = source_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("["):
            if not line.endswith("]") or line.count("[") != 1 or line.count("]") != 1:
                _refuse(
                    ImageExecutionRefusalCode.INITRAMFS_CLOSURE_UNPROVEN,
                    "config.txt contains a malformed conditional section",
                )
            section = line[1:-1].strip().casefold()
            if not section:
                _refuse(
                    ImageExecutionRefusalCode.INITRAMFS_CLOSURE_UNPROVEN,
                    "config.txt contains an empty conditional section",
                )
            active_scope = "all" if section == "all" else f"conditional:{section}"
            continue
        if re.match(r"^initramfs(?=$|\s|=)", line, flags=re.IGNORECASE):
            _refuse(
                ImageExecutionRefusalCode.INITRAMFS_CLOSURE_UNPROVEN,
                "config.txt contains an explicit initramfs override",
            )
        if re.match(r"^include(?=$|\s|=)", line, flags=re.IGNORECASE):
            _refuse(
                ImageExecutionRefusalCode.INITRAMFS_CLOSURE_UNPROVEN,
                "config.txt contains an unclosed include directive",
            )
        assignment = re.fullmatch(r"([A-Za-z0-9_]+)\s*=\s*(.*)", line)
        if assignment is None:
            continue
        key = assignment.group(1).casefold()
        value = assignment.group(2).strip()
        if key in {"arm_64bit", "kernel", "os_prefix"}:
            _refuse(
                ImageExecutionRefusalCode.INITRAMFS_CLOSURE_UNPROVEN,
                f"config.txt contains an explicit {key} boot-target override",
            )
        if key == "auto_initramfs":
            if active_scope not in {"global", "all"}:
                _refuse(
                    ImageExecutionRefusalCode.INITRAMFS_CLOSURE_UNPROVEN,
                    "config.txt contains auto_initramfs in a conditional board scope",
                    details={"scope": active_scope, "value": value},
                )
            auto_initramfs.append((active_scope, value))
    if len(auto_initramfs) != 1 or auto_initramfs[0] not in {
        ("global", "1"),
        ("all", "1"),
    }:
        _refuse(
            ImageExecutionRefusalCode.INITRAMFS_CLOSURE_UNPROVEN,
            "config.txt must contain exactly one global or [all] auto_initramfs=1 assignment",
            details={
                "auto_initramfs": [
                    {"scope": scope, "value": value} for scope, value in auto_initramfs
                ]
            },
        )


def transform_cmdline(payload: bytes) -> bytes:
    """Replace exactly one stock resize token and preserve every other token."""

    if len(payload) > MAX_CMDLINE_BYTES:
        _refuse(ImageExecutionRefusalCode.CMDLINE_INVALID, "cmdline.txt is too large")
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ImageExecutionRefused(
            ImageExecutionRefusalCode.CMDLINE_INVALID,
            "cmdline.txt must be ASCII",
        ) from exc
    if "\x00" in text or "\r" in text:
        _refuse(
            ImageExecutionRefusalCode.CMDLINE_INVALID,
            "cmdline.txt contains a NUL or carriage return",
        )
    newline_count = text.count("\n")
    if newline_count > 1 or (newline_count == 1 and not text.endswith("\n")):
        _refuse(
            ImageExecutionRefusalCode.CMDLINE_INVALID,
            "cmdline.txt must contain one logical line",
        )
    had_newline = text.endswith("\n")
    body = text[:-1] if had_newline else text
    tokens = body.split(" ")
    if not body or any(not token for token in tokens):
        _refuse(
            ImageExecutionRefusalCode.CMDLINE_INVALID,
            "cmdline.txt must use non-empty single-space-separated tokens",
        )
    if tokens.count(STOCK_RESIZE_TOKEN) != 1:
        _refuse(
            ImageExecutionRefusalCode.CMDLINE_INVALID,
            "cmdline.txt must contain exactly one standalone resize token",
        )
    if BOUNDED_PROVISION_TOKEN in tokens:
        _refuse(
            ImageExecutionRefusalCode.CMDLINE_INVALID,
            "cmdline.txt already contains the bounded-provision trigger",
        )
    transformed = [
        BOUNDED_PROVISION_TOKEN if token == STOCK_RESIZE_TOKEN else token for token in tokens
    ]
    suffix = "\n" if had_newline else ""
    return (" ".join(transformed) + suffix).encode("ascii")


def decompress_verified_image(
    *,
    manifest: SourceImageManifest,
    source_archive: Path,
    output_image: Path,
    xz_executable: Path | None = None,
    timeout_seconds: int = DECOMPRESS_TIMEOUT_SECONDS,
) -> RawImageVerification:
    """Decompress into an exclusive regular file and remove it on every failure."""

    verify_source_archive(source_archive, manifest)
    output = validate_output_path(source_archive, output_image)
    executable = _resolve_xz(xz_executable)
    if not 1 <= timeout_seconds <= DECOMPRESS_TIMEOUT_SECONDS:
        _refuse(ImageExecutionRefusalCode.COMMAND_NOT_ALLOWED, "invalid xz timeout")
    descriptor: int | None = None
    created = False
    created_identity: tuple[int, int] | None = None
    try:
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            _refuse(
                ImageExecutionRefusalCode.OUTPUT_PATH_UNSAFE,
                "exclusive output did not resolve to a regular file",
            )
        created_identity = (opened.st_dev, opened.st_ino)
        argv = (
            str(executable),
            "--decompress",
            "--keep",
            "--stdout",
            "--",
            str(source_archive.resolve(strict=True)),
        )
        with os.fdopen(descriptor, "wb", closefd=True) as output_stream:
            descriptor = None
            with tempfile.TemporaryFile() as diagnostics:
                try:
                    completed = subprocess.run(
                        argv,
                        stdin=subprocess.DEVNULL,
                        stdout=output_stream,
                        stderr=diagnostics,
                        check=False,
                        shell=False,
                        timeout=timeout_seconds,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise ImageExecutionRefused(
                        ImageExecutionRefusalCode.COMMAND_TIMEOUT,
                        f"xz exceeded the {timeout_seconds}-second limit",
                    ) from exc
                output_stream.flush()
                os.fsync(output_stream.fileno())
                if completed.returncode != 0:
                    diagnostics.seek(0)
                    diagnostic = diagnostics.read(MAX_DIAGNOSTIC_BYTES).decode(
                        "utf-8", errors="replace"
                    )
                    _refuse(
                        ImageExecutionRefusalCode.COMMAND_FAILED,
                        f"xz exited {completed.returncode}: {diagnostic.strip()}",
                    )
        verification = verify_raw_image(output, manifest)
        _fsync_directory(output.parent)
        return verification
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        if created and created_identity is not None:
            _unlink_created_output(output, created_identity)
        raise


def execute_file_image(
    *,
    plan: ImageBuildPlan,
    manifest: SourceImageManifest,
    source_archive: Path,
    output_image: Path,
    payload_root: Path,
    tool_paths: Mapping[str, Path] | None = None,
    command_runner: FileToolRunner | None = None,
    zstd_runner: ZstdRunner | None = None,
    host_system: str | None = None,
    closure_manifest_path: Path | None = None,
    authorized_exact_card_trial: bool = False,
    authorization_file: Path | None = None,
    layout_path: Path | None = None,
    target_profile: str | None = None,
) -> FileImageExecutionResult:
    """Build a disabled image, or one explicitly authorized exact-card trial image."""

    if Path(plan.output_path) != output_image.resolve(strict=False):
        _refuse(
            ImageExecutionRefusalCode.OUTPUT_PATH_UNSAFE,
            "execution output does not match the verified plan",
        )
    verify_source_archive(source_archive, manifest)
    validate_output_path(source_archive, output_image)
    bind_payload(plan, payload_root)
    if authorized_exact_card_trial:
        if authorization_file is None:
            _refuse(
                ImageExecutionRefusalCode.AUTHORIZATION_REQUIRED,
                "authorized exact-card mode requires --authorization-file",
            )
        load_exact_card_authorization(authorization_file)
    elif authorization_file is not None:
        _refuse(
            ImageExecutionRefusalCode.AUTHORIZATION_MISMATCH,
            "authorization file is accepted only in explicit exact-card trial mode",
        )
    observed_system = platform.system() if host_system is None else host_system
    dependencies = probe_execution_dependencies(
        host_system=observed_system,
        tool_paths=tool_paths,
    )
    if not dependencies.execution_supported:
        missing = [item.name for item in dependencies.observations if not item.available]
        code = (
            ImageExecutionRefusalCode.DEPENDENCY_MISSING
            if missing
            else ImageExecutionRefusalCode.HOST_EXECUTION_UNPROVEN
        )
        _refuse(
            code,
            "file-image execution refused because its native tool closure is unavailable",
            details={"dependencies": dependencies.to_dict(), "missing": missing},
        )
    paths = _resolve_toolchain(dependencies, tool_paths)
    runner = _default_file_tool_runner if command_runner is None else command_runner
    root_install_files: tuple[RootInstallFile, ...] = ()
    if authorized_exact_card_trial:
        selected_layout = (
            Path(__file__).parents[3] / "deploy" / "storage" / "layout-v1.toml"
            if layout_path is None
            else layout_path
        )
        root_install_files = _bind_authorized_root_files(plan, payload_root, selected_layout)
    hook = payload_root / "runtime" / "initramfs" / "dashcam-bounded-provision"
    replacement_hook = hook.read_bytes()
    closure_path = (
        Path(__file__).parents[3] / "deploy" / "image" / "initramfs-closure-v1.json"
        if closure_manifest_path is None
        else closure_manifest_path
    )
    observed_closure, closure_payload = bind_initramfs_closure(closure_path)
    if observed_closure != plan.initramfs_closure:
        _refuse(
            ImageExecutionRefusalCode.INITRAMFS_CLOSURE_UNPROVEN,
            "initramfs closure path or bytes differ from the authored plan",
            details={
                "planned": asdict(plan.initramfs_closure),
                "observed": asdict(observed_closure),
            },
        )
    closure = load_initramfs_closure_manifest(closure_payload)
    if (
        target_profile != plan.target_profile
        or target_profile != closure.target.profile
        or target_profile != PI_ZERO_2_W_ARMV7_PROFILE
    ):
        _refuse(
            ImageExecutionRefusalCode.INITRAMFS_CLOSURE_UNPROVEN,
            "execution target does not match the plan and closed Pi Zero 2 W armv7l target",
            details={
                "requested_target_profile": target_profile,
                "planned_target_profile": plan.target_profile,
                "closure_target_profile": closure.target.profile,
            },
        )
    verification = decompress_verified_image(
        manifest=manifest,
        source_archive=source_archive,
        output_image=output_image,
        xz_executable=paths["xz"],
    )
    created = output_image.lstat()
    created_identity = (created.st_dev, created.st_ino)
    try:
        with tempfile.TemporaryDirectory(prefix="dashcam-file-image-") as temporary:
            work = Path(temporary)
            root_partition = work / "rootfs.ext4"
            _copy_partition_to_regular_file(
                output_image,
                root_partition,
                start_bytes=manifest.image.partitions[1].start_sector
                * manifest.image.sector_size_bytes,
                size_bytes=manifest.image.partitions[1].size_sectors
                * manifest.image.sector_size_bytes,
            )
            artifacts: dict[str, Path] = {}
            for artifact in closure.artifacts:
                destination = work / f"artifact-{artifact.key}"
                command = f"dump -p {artifact.source_path} {destination.name}"
                _checked_file_tool(
                    runner,
                    (str(paths["debugfs"]), "-R", command, str(root_partition)),
                    cwd=work,
                )
                artifacts[artifact.key] = destination

            fat_offset = (
                manifest.image.partitions[0].start_sector * manifest.image.sector_size_bytes
            )
            image_spec = f"{output_image}@@{fat_offset}"
            selected_initramfs = f"::{closure.target.initramfs_filename}"
            observed_config = _checked_file_tool(
                runner,
                (str(paths["mtype"]), "-i", image_spec, "::config.txt"),
            ).stdout
            validate_boot_target_config(observed_config)
            extracted_initramfs = work / f"{closure.target.initramfs_filename}.stock"
            extracted_cmdline = work / "cmdline.stock"
            _checked_file_tool(
                runner,
                (
                    str(paths["mcopy"]),
                    "-i",
                    image_spec,
                    selected_initramfs,
                    str(extracted_initramfs),
                ),
            )
            _checked_file_tool(
                runner,
                (str(paths["mcopy"]), "-i", image_spec, "::cmdline.txt", str(extracted_cmdline)),
            )
            customized_initramfs = customize_initramfs(
                extracted_initramfs.read_bytes(),
                manifest=closure,
                zstd_executable=paths["zstd"],
                replacement_hook=replacement_hook,
                root_artifacts=artifacts,
                runtime_gate_enabled=authorized_exact_card_trial,
                runner=zstd_runner,
            )
            transformed_cmdline = transform_cmdline(extracted_cmdline.read_bytes())
            if authorized_exact_card_trial:
                _install_and_verify_authorized_root(
                    runner,
                    paths["debugfs"],
                    root_partition,
                    root_install_files,
                    expected_uuid=manifest.image.partitions[1].filesystem_uuid,
                    work=work,
                )
                _copy_regular_file_into_partition(
                    root_partition,
                    output_image,
                    start_bytes=manifest.image.partitions[1].start_sector
                    * manifest.image.sector_size_bytes,
                    size_bytes=manifest.image.partitions[1].size_sectors
                    * manifest.image.sector_size_bytes,
                    output_identity=created_identity,
                )
                copied_root = work / "rootfs.reopened.ext4"
                _copy_partition_to_regular_file(
                    output_image,
                    copied_root,
                    start_bytes=manifest.image.partitions[1].start_sector
                    * manifest.image.sector_size_bytes,
                    size_bytes=manifest.image.partitions[1].size_sectors
                    * manifest.image.sector_size_bytes,
                )
                _verify_authorized_root(
                    runner,
                    paths["debugfs"],
                    copied_root,
                    root_install_files,
                    expected_uuid=manifest.image.partitions[1].filesystem_uuid,
                    work=work,
                    prefix="root-reopened",
                )
            new_initramfs = work / "initramfs.custom"
            new_cmdline = work / "cmdline.custom"
            new_initramfs.write_bytes(customized_initramfs)
            new_cmdline.write_bytes(transformed_cmdline)
            _checked_file_tool(
                runner,
                (
                    str(paths["mcopy"]),
                    "-o",
                    "-i",
                    image_spec,
                    str(new_initramfs),
                    selected_initramfs,
                ),
            )
            _checked_file_tool(
                runner,
                (
                    str(paths["mcopy"]),
                    "-o",
                    "-i",
                    image_spec,
                    str(new_cmdline),
                    "::cmdline.txt",
                ),
            )
            _checked_file_tool(
                runner,
                (str(paths["mdir"]), "-i", image_spec, selected_initramfs),
            )
            observed_cmdline = _checked_file_tool(
                runner,
                (str(paths["mtype"]), "-i", image_spec, "::cmdline.txt"),
            ).stdout
            if observed_cmdline != transformed_cmdline:
                _refuse(
                    ImageExecutionRefusalCode.OUTPUT_VERIFICATION_FAILED,
                    "re-opened FAT cmdline does not match the transformed bytes",
                )
            verify_initramfs = work / "initramfs.verify"
            _checked_file_tool(
                runner,
                (
                    str(paths["mcopy"]),
                    "-i",
                    image_spec,
                    selected_initramfs,
                    str(verify_initramfs),
                ),
            )
            if verify_initramfs.read_bytes() != customized_initramfs:
                _refuse(
                    ImageExecutionRefusalCode.OUTPUT_VERIFICATION_FAILED,
                    "re-opened FAT initramfs does not match the customized bytes",
                )
        with output_image.open("r+b") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        verification = verify_raw_image(output_image, manifest)
        _fsync_directory(output_image.parent)
        return FileImageExecutionResult(
            1,
            verification,
            True,
            authorized_exact_card_trial,
        )
    except BaseException:
        _unlink_created_output(output_image, created_identity)
        raise


def refuse_block_device_execution(target: str) -> NoReturn:
    """Always refuse flashing or block/character-device targets."""

    _refuse(
        ImageExecutionRefusalCode.BLOCK_DEVICE_EXECUTION_DISABLED,
        f"block-device flashing is not implemented by the file-image executor: {target!r}",
    )


def refusal_json(exc: ImageExecutionRefused) -> str:
    """Serialize a stable executor refusal."""

    return json.dumps(
        {
            "executed": False,
            "code": exc.code.value,
            "error": str(exc),
            "details": exc.details,
        },
        sort_keys=True,
    )


def _resolve_xz(explicit: Path | None) -> Path:
    if explicit is None:
        found = shutil.which("xz")
        if found is None:
            _refuse(ImageExecutionRefusalCode.DEPENDENCY_MISSING, "xz is not installed")
        candidate = Path(found)
    else:
        candidate = explicit
    try:
        item_stat = candidate.lstat()
    except OSError as exc:
        raise ImageExecutionRefused(
            ImageExecutionRefusalCode.DEPENDENCY_MISSING,
            f"cannot inspect xz executable: {exc}",
        ) from exc
    if stat.S_ISLNK(item_stat.st_mode):
        candidate = candidate.resolve(strict=True)
        item_stat = candidate.stat()
    if not stat.S_ISREG(item_stat.st_mode) or candidate.name.lower() not in {"xz", "xz.exe"}:
        _refuse(
            ImageExecutionRefusalCode.COMMAND_NOT_ALLOWED,
            "xz executable must be a regular file named xz or xz.exe",
        )
    return candidate.resolve(strict=True)


def _resolve_toolchain(
    report: ExecutionDependencyReport,
    explicit: Mapping[str, Path] | None,
) -> dict[str, Path]:
    observations = {item.name: item for item in report.observations}
    resolved: dict[str, Path] = {}
    for requirement in _POSIX_REQUIREMENTS:
        selected = None if explicit is None else explicit.get(requirement.name)
        if selected is None:
            observed = observations.get(requirement.name)
            selected = None if observed is None or observed.path is None else Path(observed.path)
        if selected is None:
            _refuse(
                ImageExecutionRefusalCode.DEPENDENCY_MISSING,
                f"required file tool is absent: {requirement.name}",
            )
        try:
            item = selected.lstat()
        except OSError as exc:
            raise ImageExecutionRefused(
                ImageExecutionRefusalCode.DEPENDENCY_MISSING,
                f"cannot inspect {requirement.name}: {exc}",
            ) from exc
        accepted_names = {Path(path).name for path in requirement.accepted_paths}
        accepted_names.update(f"{name}.exe" for name in tuple(accepted_names))
        if (
            stat.S_ISLNK(item.st_mode)
            or not stat.S_ISREG(item.st_mode)
            or selected.name.lower() not in accepted_names
            or (os.name != "nt" and not os.access(selected, os.X_OK))
        ):
            _refuse(
                ImageExecutionRefusalCode.COMMAND_NOT_ALLOWED,
                f"{requirement.name} must be an exact executable regular file",
            )
        resolved[requirement.name] = selected.resolve(strict=True)
    if explicit is not None and set(explicit) - {item.name for item in _POSIX_REQUIREMENTS}:
        _refuse(ImageExecutionRefusalCode.COMMAND_NOT_ALLOWED, "unknown explicit tool path")
    return resolved


def _copy_partition_to_regular_file(
    image: Path,
    destination: Path,
    *,
    start_bytes: int,
    size_bytes: int,
) -> None:
    if start_bytes < 0 or size_bytes <= 0:
        _refuse(ImageExecutionRefusalCode.OUTPUT_VERIFICATION_FAILED, "invalid root byte range")
    remaining = size_bytes
    with image.open("rb") as source, destination.open("xb") as target:
        source.seek(start_bytes)
        while remaining:
            chunk = source.read(min(FILE_COPY_CHUNK_BYTES, remaining))
            if not chunk:
                _refuse(
                    ImageExecutionRefusalCode.OUTPUT_VERIFICATION_FAILED,
                    "raw image ended inside the exact root partition range",
                )
            target.write(chunk)
            remaining -= len(chunk)
        target.flush()
        os.fsync(target.fileno())
    if destination.stat().st_size != size_bytes:
        _refuse(
            ImageExecutionRefusalCode.OUTPUT_VERIFICATION_FAILED,
            "extracted root partition has the wrong size",
        )


def _install_and_verify_authorized_root(
    runner: FileToolRunner,
    debugfs: Path,
    root_partition: Path,
    files: tuple[RootInstallFile, ...],
    *,
    expected_uuid: str,
    work: Path,
) -> None:
    """Mutate only the temporary regular ext4 copy, then reopen every installed item."""

    if _read_ext4_uuid(root_partition) != expected_uuid:
        _refuse(
            ImageExecutionRefusalCode.OUTPUT_VERIFICATION_FAILED,
            "temporary root ext4 UUID changed before authorized installation",
        )
    for directory in (
        "/etc/dashcam",
        "/usr/lib/dashcam",
        "/etc/systemd/system/local-fs.target.wants",
    ):
        _checked_file_tool(
            runner,
            (str(debugfs), "-w", "-R", f"mkdir {directory}", str(root_partition)),
        )
    for index, item in enumerate(files):
        _validate_ext4_destination(item.destination)
        source = work / f"root-install-{index:02d}.payload"
        source.write_bytes(item.content)
        _checked_file_tool(
            runner,
            (
                str(debugfs),
                "-w",
                "-R",
                f"write {source.name} {item.destination}",
                str(root_partition),
            ),
            cwd=work,
        )
        _checked_file_tool(
            runner,
            (
                str(debugfs),
                "-w",
                "-R",
                f"set_inode_field {item.destination} mode 0100{item.mode:03o}",
                str(root_partition),
            ),
        )
    _checked_file_tool(
        runner,
        (
            str(debugfs),
            "-w",
            "-R",
            f"symlink {SERVICE_LINK_PATH} {SERVICE_LINK_TARGET}",
            str(root_partition),
        ),
    )
    _verify_authorized_root(
        runner,
        debugfs,
        root_partition,
        files,
        expected_uuid=expected_uuid,
        work=work,
        prefix="root-verify",
    )


def _verify_authorized_root(
    runner: FileToolRunner,
    debugfs: Path,
    root_partition: Path,
    files: tuple[RootInstallFile, ...],
    *,
    expected_uuid: str,
    work: Path,
    prefix: str,
) -> None:
    if _read_ext4_uuid(root_partition) != expected_uuid:
        _refuse(
            ImageExecutionRefusalCode.OUTPUT_VERIFICATION_FAILED,
            "authorized root installation changed the ext4 UUID",
        )
    for index, item in enumerate(files):
        observed = work / f"{prefix}-{index:02d}.payload"
        _checked_file_tool(
            runner,
            (
                str(debugfs),
                "-R",
                f"dump -p {item.destination} {observed.name}",
                str(root_partition),
            ),
            cwd=work,
        )
        if not observed.is_file() or observed.read_bytes() != item.content:
            _refuse(
                ImageExecutionRefusalCode.OUTPUT_VERIFICATION_FAILED,
                f"re-opened authorized root file is not exact: {item.destination}",
            )
        stat_result = _checked_file_tool(
            runner,
            (str(debugfs), "-R", f"stat {item.destination}", str(root_partition)),
        )
        mode_match = _MODE_PATTERN.search(stat_result.stdout)
        if (
            b"Type: regular" not in stat_result.stdout
            or mode_match is None
            or int(mode_match.group(1), 8) & 0o7777 != item.mode
        ):
            _refuse(
                ImageExecutionRefusalCode.OUTPUT_VERIFICATION_FAILED,
                f"re-opened authorized root mode is not exact: {item.destination}",
            )
    link_stat = _checked_file_tool(
        runner,
        (str(debugfs), "-R", f"stat {SERVICE_LINK_PATH}", str(root_partition)),
    ).stdout
    expected_link = f'Fast link dest: "{SERVICE_LINK_TARGET}"'.encode()
    if expected_link not in link_stat:
        _refuse(
            ImageExecutionRefusalCode.OUTPUT_VERIFICATION_FAILED,
            "enabled service symlink target is not exact",
        )


def _validate_ext4_destination(destination: str) -> None:
    path = Path(destination)
    if (
        not destination.startswith("/")
        or "\\" in destination
        or "\x00" in destination
        or any(part in {"", ".", ".."} for part in destination.split("/")[1:])
        or str(path).startswith("//")
    ):
        _refuse(
            ImageExecutionRefusalCode.PAYLOAD_BINDING_MISMATCH,
            f"unsafe authorized root destination: {destination!r}",
        )


def _read_ext4_uuid(path: Path) -> str:
    with path.open("rb") as stream:
        stream.seek(1024 + 104)
        raw = stream.read(16)
    if len(raw) != 16:
        _refuse(
            ImageExecutionRefusalCode.OUTPUT_VERIFICATION_FAILED,
            "temporary root is too short for an ext4 UUID",
        )
    import uuid

    return str(uuid.UUID(bytes=raw))


def _copy_regular_file_into_partition(
    source_path: Path,
    output_image: Path,
    *,
    start_bytes: int,
    size_bytes: int,
    output_identity: tuple[int, int],
) -> None:
    """Bounded-stream the verified regular p2 copy into the owned output inode."""

    source_item = source_path.lstat()
    if stat.S_ISLNK(source_item.st_mode) or not stat.S_ISREG(source_item.st_mode):
        _refuse(
            ImageExecutionRefusalCode.OUTPUT_VERIFICATION_FAILED,
            "authorized root source is no longer a regular file",
        )
    if source_item.st_size != size_bytes:
        _refuse(
            ImageExecutionRefusalCode.OUTPUT_VERIFICATION_FAILED,
            "authorized root source size changed",
        )
    with source_path.open("rb") as source, output_image.open("r+b") as output:
        opened = os.fstat(output.fileno())
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != output_identity:
            _refuse(
                ImageExecutionRefusalCode.OUTPUT_PATH_UNSAFE,
                "output inode changed before the authorized root write",
            )
        output.seek(start_bytes)
        remaining = size_bytes
        while remaining:
            chunk = source.read(min(FILE_COPY_CHUNK_BYTES, remaining))
            if not chunk:
                _refuse(
                    ImageExecutionRefusalCode.OUTPUT_VERIFICATION_FAILED,
                    "authorized root source ended inside the exact p2 range",
                )
            output.write(chunk)
            remaining -= len(chunk)
        if source.read(1):
            _refuse(
                ImageExecutionRefusalCode.OUTPUT_VERIFICATION_FAILED,
                "authorized root source exceeds the exact p2 range",
            )
        output.flush()
        os.fsync(output.fileno())


def _checked_file_tool(
    runner: FileToolRunner,
    argv: tuple[str, ...],
    *,
    cwd: Path | None = None,
) -> FileToolResult:
    if not argv or any(not item or "\x00" in item for item in argv):
        _refuse(ImageExecutionRefusalCode.COMMAND_NOT_ALLOWED, "invalid file-tool argv")
    try:
        result = runner(argv, cwd, FILE_TOOL_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise ImageExecutionRefused(
            ImageExecutionRefusalCode.COMMAND_TIMEOUT,
            f"{Path(argv[0]).name} exceeded the bounded timeout",
        ) from exc
    if len(result.stdout) > MAX_COMMAND_OUTPUT_BYTES or len(result.stderr) > MAX_DIAGNOSTIC_BYTES:
        _refuse(ImageExecutionRefusalCode.COMMAND_FAILED, "file-tool output exceeds its bound")
    if result.returncode != 0:
        diagnostic = result.stderr.decode("utf-8", errors="replace").strip()
        _refuse(
            ImageExecutionRefusalCode.COMMAND_FAILED,
            f"{Path(argv[0]).name} exited {result.returncode}: {diagnostic}",
        )
    return result


def _default_file_tool_runner(
    argv: tuple[str, ...], cwd: Path | None, timeout_seconds: int
) -> FileToolResult:
    completed = subprocess.run(
        argv,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        shell=False,
        timeout=timeout_seconds,
    )
    return FileToolResult(completed.returncode, completed.stdout, completed.stderr)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _unlink_created_output(path: Path, identity: tuple[int, int]) -> None:
    """Remove only the same regular file created by this executor."""

    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if (
        stat.S_ISLNK(current.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or (current.st_dev, current.st_ino) != identity
    ):
        return
    path.unlink()
    _fsync_directory(path.parent)


def _wsl_path_is_executable(wsl: str, candidate: str) -> bool:
    try:
        completed = subprocess.run(
            (wsl, "--exec", "/usr/bin/test", "-x", candidate),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            shell=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _refuse(
    code: ImageExecutionRefusalCode,
    message: str,
    *,
    details: dict[str, object] | None = None,
) -> NoReturn:
    raise ImageExecutionRefused(code, message, details=details)
