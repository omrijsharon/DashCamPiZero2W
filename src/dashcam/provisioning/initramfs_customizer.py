"""Deterministic, manifest-bound customization of the pinned initramfs bytes."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, Protocol, cast

from dashcam.provisioning.initramfs_archive import (
    NewcArchive,
    NewcEntry,
    add_newc_directory,
    add_newc_regular_file,
    parse_newc_archive,
    recombine_initramfs,
    replace_newc_member,
    serialize_newc_archive,
    split_initramfs,
)

SHA256_HEX_LENGTH: Final = 64
MAX_DIAGNOSTIC_BYTES: Final = 64 * 1024
MAX_COMPRESSED_BYTES: Final = 32 * 1024 * 1024
MAX_DECOMPRESSED_BYTES: Final = 128 * 1024 * 1024
COMMAND_TIMEOUT_SECONDS: Final = 120
RUNTIME_GATE_PATH: Final = "etc/dashcam/firstboot-runtime-v1.enabled"
RUNTIME_GATE_CONTENT: Final = b"EXACT_IMAGE_RUNTIME_VALIDATED=v1\n"
PI_ZERO_2_W_ARMV7_PROFILE: Final = "pi-zero-2-w-armv7l"
PI_ZERO_2_W_ARMV7_ARCHITECTURE: Final = "armv7l"
PI_ZERO_2_W_ARMV7_KERNEL: Final = "kernel7.img"
PI_ZERO_2_W_ARMV7_INITRAMFS: Final = "initramfs7"


class InitramfsCustomizationError(RuntimeError):
    """Raised when exact customization cannot be proven."""


@dataclass(frozen=True, slots=True)
class ContractFile:
    destination: str
    content: bytes
    sha256: str
    mode: int
    mtime: int


@dataclass(frozen=True, slots=True)
class RootArtifact:
    key: str
    source_path: str
    destination: str
    sha256: str
    mode: int
    mtime: int


@dataclass(frozen=True, slots=True)
class RuntimeGate:
    destination: str
    content: bytes
    sha256: str
    mode: int
    mtime: int


@dataclass(frozen=True, slots=True)
class StockLibrary:
    destination: str
    sha256: str
    mode: int


@dataclass(frozen=True, slots=True)
class InitramfsTarget:
    profile: str
    architecture: str
    kernel_filename: str
    initramfs_filename: str
    auto_initramfs_required: bool


@dataclass(frozen=True, slots=True)
class InitramfsClosureManifest:
    schema_version: int
    target: InitramfsTarget
    input_size_bytes: int
    input_sha256: str
    main_offset: int
    main_compressed_size_bytes: int
    main_compressed_sha256: str
    main_uncompressed_size_bytes: int
    main_uncompressed_sha256: str
    early_entry_count: int
    main_entry_count: int
    stock_hook_path: str
    stock_hook_count: int
    replacement_hook_sha256: str
    injected_directory: str
    injected_directory_mode: int
    injected_mtime: int
    contracts: tuple[ContractFile, ...]
    artifacts: tuple[RootArtifact, ...]
    elf_root_artifact_keys: tuple[str, ...]
    required_stock_libraries: tuple[StockLibrary, ...]
    runtime_gate: RuntimeGate
    compression_level: int
    maximum_output_bytes: int


class ZstdRunner(Protocol):
    """Injectable exact-argv runner used for bounded tests."""

    def __call__(
        self,
        argv: tuple[str, ...],
        *,
        stdout_path: Path,
        stderr_path: Path,
        timeout_seconds: int,
    ) -> int: ...


def load_initramfs_closure_manifest(payload: bytes) -> InitramfsClosureManifest:
    """Load the closed version-one customization manifest."""

    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InitramfsCustomizationError("closure manifest is not valid JSON") from exc
    raw = _closed_mapping(
        decoded,
        {
            "schema_version",
            "target",
            "input",
            "stock_hook",
            "injection",
            "contracts",
            "artifacts",
            "elf_closure",
            "runtime_gate",
            "zstd",
            "maximum_output_bytes",
        },
        "manifest",
    )
    target_raw = _closed_mapping(
        raw["target"],
        {
            "profile",
            "architecture",
            "kernel_filename",
            "initramfs_filename",
            "auto_initramfs_required",
        },
        "target",
    )
    input_raw = _closed_mapping(
        raw["input"],
        {
            "size_bytes",
            "sha256",
            "main_offset",
            "main_compressed_size_bytes",
            "main_compressed_sha256",
            "main_uncompressed_size_bytes",
            "main_uncompressed_sha256",
            "early_entry_count",
            "main_entry_count",
        },
        "input",
    )
    hook_raw = _closed_mapping(
        raw["stock_hook"], {"path", "required_count", "replacement_sha256"}, "stock_hook"
    )
    injection_raw = _closed_mapping(
        raw["injection"], {"directory", "directory_mode", "mtime"}, "injection"
    )
    zstd_raw = _closed_mapping(raw["zstd"], {"compression_level", "threads", "checksum"}, "zstd")
    if zstd_raw["threads"] != 1 or zstd_raw["checksum"] is not True:
        raise InitramfsCustomizationError("zstd must use one thread and frame checksum")
    contracts = tuple(_contract(item, index) for index, item in enumerate(_list(raw["contracts"])))
    artifacts = tuple(_artifact(item, index) for index, item in enumerate(_list(raw["artifacts"])))
    elf_raw = _closed_mapping(
        raw["elf_closure"],
        {"root_artifact_keys", "required_stock_entries"},
        "elf_closure",
    )
    elf_root_artifact_keys = tuple(
        _text(item, f"elf_closure.root_artifact_keys[{index}]")
        for index, item in enumerate(_list(elf_raw["root_artifact_keys"]))
    )
    required_stock_libraries = tuple(
        _stock_library(item, index)
        for index, item in enumerate(_list(elf_raw["required_stock_entries"]))
    )
    runtime_gate_raw = _closed_mapping(
        raw["runtime_gate"], {"destination", "content", "sha256", "mode", "mtime"}, "runtime_gate"
    )
    runtime_gate_content = _text(runtime_gate_raw["content"], "runtime_gate.content").encode()
    runtime_gate = RuntimeGate(
        _safe_path(runtime_gate_raw["destination"], "runtime_gate.destination"),
        runtime_gate_content,
        _sha(runtime_gate_raw["sha256"], "runtime_gate.sha256"),
        _mode(runtime_gate_raw["mode"], "runtime_gate.mode"),
        _uint32(runtime_gate_raw["mtime"], "runtime_gate.mtime"),
    )
    _require_hash(runtime_gate.content, runtime_gate.sha256, "runtime_gate")
    manifest = InitramfsClosureManifest(
        _integer(raw["schema_version"], "schema_version"),
        InitramfsTarget(
            _text(target_raw["profile"], "target.profile"),
            _text(target_raw["architecture"], "target.architecture"),
            _boot_filename(target_raw["kernel_filename"], "target.kernel_filename"),
            _boot_filename(target_raw["initramfs_filename"], "target.initramfs_filename"),
            _boolean(target_raw["auto_initramfs_required"], "target.auto_initramfs_required"),
        ),
        _positive(input_raw["size_bytes"], "input.size_bytes"),
        _sha(input_raw["sha256"], "input.sha256"),
        _positive(input_raw["main_offset"], "input.main_offset"),
        _positive(input_raw["main_compressed_size_bytes"], "input.main_compressed_size_bytes"),
        _sha(input_raw["main_compressed_sha256"], "input.main_compressed_sha256"),
        _positive(input_raw["main_uncompressed_size_bytes"], "input.main_uncompressed_size_bytes"),
        _sha(input_raw["main_uncompressed_sha256"], "input.main_uncompressed_sha256"),
        _positive(input_raw["early_entry_count"], "input.early_entry_count"),
        _positive(input_raw["main_entry_count"], "input.main_entry_count"),
        _safe_path(hook_raw["path"], "stock_hook.path"),
        _positive(hook_raw["required_count"], "stock_hook.required_count"),
        _sha(hook_raw["replacement_sha256"], "stock_hook.replacement_sha256"),
        _safe_path(injection_raw["directory"], "injection.directory"),
        _mode(injection_raw["directory_mode"], "injection.directory_mode"),
        _uint32(injection_raw["mtime"], "injection.mtime"),
        contracts,
        artifacts,
        elf_root_artifact_keys,
        required_stock_libraries,
        runtime_gate,
        _positive(zstd_raw["compression_level"], "zstd.compression_level"),
        _positive(raw["maximum_output_bytes"], "maximum_output_bytes"),
    )
    if manifest.schema_version != 1:
        raise InitramfsCustomizationError("closure manifest schema_version must be 1")
    if manifest.target != InitramfsTarget(
        PI_ZERO_2_W_ARMV7_PROFILE,
        PI_ZERO_2_W_ARMV7_ARCHITECTURE,
        PI_ZERO_2_W_ARMV7_KERNEL,
        PI_ZERO_2_W_ARMV7_INITRAMFS,
        True,
    ):
        raise InitramfsCustomizationError(
            "initramfs target is not the exact Pi Zero 2 W armv7l boot target"
        )
    if manifest.stock_hook_count != 1:
        raise InitramfsCustomizationError("stock hook cardinality must be exactly one")
    if not 1 <= manifest.compression_level <= 19:
        raise InitramfsCustomizationError("zstd compression level is outside 1..19")
    destinations = [item.destination for item in manifest.contracts]
    destinations.extend(item.destination for item in manifest.artifacts)
    destinations.append(manifest.injected_directory)
    if len(destinations) != len(set(destinations)) or RUNTIME_GATE_PATH in destinations:
        raise InitramfsCustomizationError("injected destinations are duplicate or include the gate")
    if (
        manifest.runtime_gate.destination != RUNTIME_GATE_PATH
        or manifest.runtime_gate.content != RUNTIME_GATE_CONTENT
        or manifest.runtime_gate.mode != stat.S_IFREG | 0o400
    ):
        raise InitramfsCustomizationError("runtime gate contract is not exact")
    artifact_keys = [item.key for item in manifest.artifacts]
    if len(artifact_keys) != len(set(artifact_keys)):
        raise InitramfsCustomizationError("artifact keys must be unique")
    if manifest.elf_root_artifact_keys != ("resize2fs", "dumpe2fs", "sfdisk", "libfdisk") or set(
        manifest.elf_root_artifact_keys
    ) != set(artifact_keys):
        raise InitramfsCustomizationError("ELF closure roots do not match the exact artifacts")
    library_destinations = [item.destination for item in manifest.required_stock_libraries]
    if (
        not library_destinations
        or len(library_destinations) != len(set(library_destinations))
        or set(library_destinations).intersection(destinations)
    ):
        raise InitramfsCustomizationError("required stock library destinations are not exact")
    return manifest


def customize_initramfs(
    input_bytes: bytes,
    *,
    manifest: InitramfsClosureManifest,
    zstd_executable: Path,
    replacement_hook: bytes,
    root_artifacts: Mapping[str, Path],
    runtime_gate_enabled: bool = False,
    runner: ZstdRunner | None = None,
    timeout_seconds: int = COMMAND_TIMEOUT_SECONDS,
) -> bytes:
    """Customize exact bytes and re-open the result to prove its closure."""

    if not 1 <= timeout_seconds <= COMMAND_TIMEOUT_SECONDS:
        raise InitramfsCustomizationError("zstd timeout is outside the admitted bound")
    _require_blob(input_bytes, manifest.input_size_bytes, manifest.input_sha256, "initramfs")
    _require_hash(replacement_hook, manifest.replacement_hook_sha256, "replacement hook")
    tool = _exact_zstd(zstd_executable)
    artifacts = _bind_artifacts(manifest, root_artifacts)
    parts = split_initramfs(input_bytes)
    if parts.main_offset != manifest.main_offset:
        raise InitramfsCustomizationError("main archive offset does not match the manifest")
    if len(parts.early_archive.entries) != manifest.early_entry_count:
        raise InitramfsCustomizationError("early archive entry count does not match the manifest")
    _require_blob(
        parts.main_compressed,
        manifest.main_compressed_size_bytes,
        manifest.main_compressed_sha256,
        "compressed main archive",
    )
    command_runner = _subprocess_runner if runner is None else runner
    with tempfile.TemporaryDirectory(prefix="dashcam-initramfs-") as temporary:
        work = Path(temporary)
        main_cpio = _run_zstd(
            tool,
            parts.main_compressed,
            decompress=True,
            work=work,
            label="decompress-input",
            level=manifest.compression_level,
            runner=command_runner,
            timeout_seconds=timeout_seconds,
        )
        _require_blob(
            main_cpio,
            manifest.main_uncompressed_size_bytes,
            manifest.main_uncompressed_sha256,
            "decompressed main archive",
        )
        parsed = parse_newc_archive(main_cpio)
        if len(parsed.entries) != manifest.main_entry_count:
            raise InitramfsCustomizationError(
                "main archive entry count does not match the manifest"
            )
        if sum(entry.name == manifest.stock_hook_path for entry in parsed.entries) != 1:
            raise InitramfsCustomizationError("stock resize hook cardinality is not exactly one")
        if any(entry.name == RUNTIME_GATE_PATH for entry in parsed.entries):
            raise InitramfsCustomizationError("input main archive contains the forbidden gate")
        _verify_stock_libraries(parsed.entries, manifest.required_stock_libraries)

        rebuilt = replace_newc_member(
            main_cpio,
            target=manifest.stock_hook_path,
            replacement_data=replacement_hook,
        )
        rebuilt = add_newc_directory(
            rebuilt,
            target=manifest.injected_directory,
            mode=manifest.injected_directory_mode,
            uid=0,
            gid=0,
            mtime=manifest.injected_mtime,
        )
        for contract in manifest.contracts:
            rebuilt = _add_data_file(
                rebuilt,
                destination=contract.destination,
                data=contract.content,
                mode=contract.mode,
                mtime=contract.mtime,
            )
        for artifact, data in artifacts:
            if artifact.mode == stat.S_IFREG | 0o755:
                rebuilt = add_newc_regular_file(
                    rebuilt,
                    target=artifact.destination,
                    data=data,
                    mode=artifact.mode,
                    uid=0,
                    gid=0,
                    mtime=artifact.mtime,
                )
            else:
                rebuilt = _add_data_file(
                    rebuilt,
                    destination=artifact.destination,
                    data=data,
                    mode=artifact.mode,
                    mtime=artifact.mtime,
                )
        if runtime_gate_enabled:
            rebuilt = _add_data_file(
                rebuilt,
                destination=manifest.runtime_gate.destination,
                data=manifest.runtime_gate.content,
                mode=manifest.runtime_gate.mode,
                mtime=manifest.runtime_gate.mtime,
            )
        compressed = _run_zstd(
            tool,
            rebuilt,
            decompress=False,
            work=work,
            label="compress-output",
            level=manifest.compression_level,
            runner=command_runner,
            timeout_seconds=timeout_seconds,
        )
        output = recombine_initramfs(parts, compressed)
        if len(output) > manifest.maximum_output_bytes:
            raise InitramfsCustomizationError("customized initramfs exceeds the manifest bound")
        _verify_output(
            output,
            manifest=manifest,
            replacement_hook=replacement_hook,
            artifacts=artifacts,
            runtime_gate_enabled=runtime_gate_enabled,
            tool=tool,
            work=work,
            runner=command_runner,
            timeout_seconds=timeout_seconds,
            early_bytes=parts.early_bytes,
        )
        return output


def _verify_output(
    output: bytes,
    *,
    manifest: InitramfsClosureManifest,
    replacement_hook: bytes,
    artifacts: tuple[tuple[RootArtifact, bytes], ...],
    runtime_gate_enabled: bool,
    tool: Path,
    work: Path,
    runner: ZstdRunner,
    timeout_seconds: int,
    early_bytes: bytes,
) -> None:
    parts = split_initramfs(output)
    if parts.early_bytes != early_bytes:
        raise InitramfsCustomizationError("early archive bytes changed during customization")
    rebuilt = _run_zstd(
        tool,
        parts.main_compressed,
        decompress=True,
        work=work,
        label="verify-output",
        level=manifest.compression_level,
        runner=runner,
        timeout_seconds=timeout_seconds,
    )
    parsed = parse_newc_archive(rebuilt)
    _verify_stock_libraries(parsed.entries, manifest.required_stock_libraries)
    by_name = {entry.name: entry for entry in parsed.entries}
    expected_count = (
        manifest.main_entry_count
        + 1
        + len(manifest.contracts)
        + len(artifacts)
        + int(runtime_gate_enabled)
    )
    if len(parsed.entries) != expected_count or len(by_name) != expected_count:
        raise InitramfsCustomizationError("customized archive entry count is not exact")
    _entry_hash(by_name, manifest.stock_hook_path, replacement_hook)
    directory = by_name.get(manifest.injected_directory)
    if directory is None or directory.mode != manifest.injected_directory_mode or directory.data:
        raise InitramfsCustomizationError("injected directory metadata is not exact")
    for contract in manifest.contracts:
        _entry_hash(by_name, contract.destination, contract.content, mode=contract.mode)
    for artifact, data in artifacts:
        _entry_hash(by_name, artifact.destination, data, mode=artifact.mode)
    if runtime_gate_enabled:
        _entry_hash(
            by_name,
            manifest.runtime_gate.destination,
            manifest.runtime_gate.content,
            mode=manifest.runtime_gate.mode,
        )
    elif RUNTIME_GATE_PATH in by_name:
        raise InitramfsCustomizationError("customized archive contains the forbidden gate")


def _run_zstd(
    tool: Path,
    payload: bytes,
    *,
    decompress: bool,
    work: Path,
    label: str,
    level: int,
    runner: ZstdRunner,
    timeout_seconds: int,
) -> bytes:
    input_limit = MAX_COMPRESSED_BYTES if decompress else MAX_DECOMPRESSED_BYTES
    output_limit = MAX_DECOMPRESSED_BYTES if decompress else MAX_COMPRESSED_BYTES
    if len(payload) > input_limit:
        raise InitramfsCustomizationError("zstd input exceeds the admitted bound")
    input_path = work / f"{label}.input"
    output_path = work / f"{label}.output"
    stderr_path = work / f"{label}.stderr"
    input_path.write_bytes(payload)
    if decompress:
        argv: tuple[str, ...] = (
            str(tool),
            "--decompress",
            "--stdout",
            "--quiet",
            "--",
            str(input_path),
        )
    else:
        argv = (
            str(tool),
            "--compress",
            f"-{level}",
            "--threads=1",
            "--check",
            "--stdout",
            "--quiet",
            "--",
            str(input_path),
        )
    try:
        returncode = runner(
            argv,
            stdout_path=output_path,
            stderr_path=stderr_path,
            timeout_seconds=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise InitramfsCustomizationError("zstd command exceeded its timeout") from exc
    if stderr_path.exists() and stderr_path.stat().st_size > MAX_DIAGNOSTIC_BYTES:
        raise InitramfsCustomizationError("zstd diagnostics exceed the admitted bound")
    diagnostic = (
        stderr_path.read_bytes()[:MAX_DIAGNOSTIC_BYTES].decode("utf-8", errors="replace")
        if stderr_path.exists()
        else ""
    )
    if returncode != 0:
        raise InitramfsCustomizationError(
            f"zstd command failed with status {returncode}: {diagnostic.strip()}"
        )
    if not output_path.is_file() or output_path.stat().st_size > output_limit:
        raise InitramfsCustomizationError("zstd output is absent or exceeds the admitted bound")
    return output_path.read_bytes()


def _subprocess_runner(
    argv: tuple[str, ...],
    *,
    stdout_path: Path,
    stderr_path: Path,
    timeout_seconds: int,
) -> int:
    with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            check=False,
            shell=False,
            timeout=timeout_seconds,
        )
    return completed.returncode


def _add_data_file(
    payload: bytes, *, destination: str, data: bytes, mode: int, mtime: int
) -> bytes:
    archive = parse_newc_archive(payload)
    if any(entry.name == destination for entry in archive.entries):
        raise InitramfsCustomizationError(f"injected destination already exists: {destination}")
    parent = str(PurePosixPath(destination).parent)
    parents = [entry for entry in archive.entries if entry.name == parent]
    if len(parents) != 1 or stat.S_IFMT(parents[0].mode) != stat.S_IFDIR:
        raise InitramfsCustomizationError(f"injected destination parent is not exact: {parent}")
    used = {
        entry.inode
        for entry in archive.entries
        if entry.device_major == 0 and entry.device_minor == 0 and entry.inode > 0
    }
    inode = min(candidate for candidate in range(1, len(used) + 2) if candidate not in used)
    entry = NewcEntry(
        destination,
        inode,
        mode,
        0,
        0,
        1,
        mtime,
        0,
        0,
        0,
        0,
        data,
        0,
        0,
    )
    return serialize_newc_archive(NewcArchive((*archive.entries, entry), 0, 0))


def _bind_artifacts(
    manifest: InitramfsClosureManifest, provided: Mapping[str, Path]
) -> tuple[tuple[RootArtifact, bytes], ...]:
    expected = {artifact.key for artifact in manifest.artifacts}
    if set(provided) != expected:
        raise InitramfsCustomizationError("root artifact keys do not match the manifest")
    bound: list[tuple[RootArtifact, bytes]] = []
    for artifact in manifest.artifacts:
        path = provided[artifact.key]
        try:
            item = path.lstat()
        except OSError as exc:
            raise InitramfsCustomizationError(f"cannot inspect artifact {artifact.key}") from exc
        if stat.S_ISLNK(item.st_mode) or not stat.S_ISREG(item.st_mode):
            raise InitramfsCustomizationError(
                f"artifact {artifact.key} is not an exact regular file"
            )
        data = path.read_bytes()
        _require_hash(data, artifact.sha256, f"artifact {artifact.key}")
        bound.append((artifact, data))
    return tuple(bound)


def _exact_zstd(path: Path) -> Path:
    try:
        item = path.lstat()
    except OSError as exc:
        raise InitramfsCustomizationError("cannot inspect zstd executable") from exc
    if (
        stat.S_ISLNK(item.st_mode)
        or not stat.S_ISREG(item.st_mode)
        or path.name not in {"zstd", "zstd.exe"}
        or (os.name != "nt" and not os.access(path, os.X_OK))
    ):
        raise InitramfsCustomizationError("zstd must be an exact executable regular file")
    return path.resolve(strict=True)


def _entry_hash(
    entries: Mapping[str, NewcEntry],
    name: str,
    expected: bytes,
    *,
    mode: int | None = None,
) -> None:
    entry = entries.get(name)
    if entry is None or entry.data != expected or (mode is not None and entry.mode != mode):
        raise InitramfsCustomizationError(f"customized entry is not exact: {name}")


def _verify_stock_libraries(
    entries: tuple[NewcEntry, ...], required: tuple[StockLibrary, ...]
) -> None:
    by_name = {entry.name: entry for entry in entries}
    if len(by_name) != len(entries):
        raise InitramfsCustomizationError("initramfs entries are not uniquely named")
    for library in required:
        entry = by_name.get(library.destination)
        if (
            entry is None
            or entry.mode != library.mode
            or hashlib.sha256(entry.data).hexdigest() != library.sha256
        ):
            raise InitramfsCustomizationError(
                f"required stock library is missing or changed: {library.destination}"
            )


def _require_blob(payload: bytes, size: int, digest: str, label: str) -> None:
    if len(payload) != size:
        raise InitramfsCustomizationError(f"{label} size does not match the manifest")
    _require_hash(payload, digest, label)


def _require_hash(payload: bytes, digest: str, label: str) -> None:
    if hashlib.sha256(payload).hexdigest() != digest:
        raise InitramfsCustomizationError(f"{label} SHA-256 does not match the manifest")


def _contract(value: object, index: int) -> ContractFile:
    raw = _closed_mapping(
        value, {"destination", "content", "sha256", "mode", "mtime"}, f"contracts[{index}]"
    )
    content = _text(raw["content"], f"contracts[{index}].content").encode()
    digest = _sha(raw["sha256"], f"contracts[{index}].sha256")
    _require_hash(content, digest, f"contracts[{index}]")
    return ContractFile(
        _safe_path(raw["destination"], f"contracts[{index}].destination"),
        content,
        digest,
        _mode(raw["mode"], f"contracts[{index}].mode"),
        _uint32(raw["mtime"], f"contracts[{index}].mtime"),
    )


def _artifact(value: object, index: int) -> RootArtifact:
    raw = _closed_mapping(
        value,
        {"key", "source_path", "destination", "sha256", "mode", "mtime"},
        f"artifacts[{index}]",
    )
    source = _text(raw["source_path"], f"artifacts[{index}].source_path")
    if not source.startswith("/"):
        raise InitramfsCustomizationError("artifact source path must be absolute")
    return RootArtifact(
        _text(raw["key"], f"artifacts[{index}].key"),
        source,
        _safe_path(raw["destination"], f"artifacts[{index}].destination"),
        _sha(raw["sha256"], f"artifacts[{index}].sha256"),
        _mode(raw["mode"], f"artifacts[{index}].mode"),
        _uint32(raw["mtime"], f"artifacts[{index}].mtime"),
    )


def _stock_library(value: object, index: int) -> StockLibrary:
    raw = _closed_mapping(
        value,
        {"destination", "sha256", "mode"},
        f"elf_closure.required_stock_entries[{index}]",
    )
    mode = _uint32(raw["mode"], f"elf_closure.required_stock_entries[{index}].mode")
    if stat.S_IFMT(mode) not in {stat.S_IFREG, stat.S_IFLNK}:
        raise InitramfsCustomizationError(
            f"elf_closure.required_stock_entries[{index}].mode is not regular or symlink"
        )
    if mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
        raise InitramfsCustomizationError(
            f"elf_closure.required_stock_entries[{index}].mode has unsafe privilege bits"
        )
    return StockLibrary(
        _safe_path(
            raw["destination"],
            f"elf_closure.required_stock_entries[{index}].destination",
        ),
        _sha(raw["sha256"], f"elf_closure.required_stock_entries[{index}].sha256"),
        mode,
    )


def _closed_mapping(value: object, keys: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise InitramfsCustomizationError(f"{label} must be an object with text keys")
    typed = cast(dict[str, object], value)
    if set(typed) != keys:
        raise InitramfsCustomizationError(f"{label} keys do not match the closed schema")
    return typed


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise InitramfsCustomizationError("manifest value must be a list")
    return cast(list[object], value)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise InitramfsCustomizationError(f"{label} must be non-empty text")
    return value


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise InitramfsCustomizationError(f"{label} must be a boolean")
    return value


def _boot_filename(value: object, label: str) -> str:
    name = _text(value, label)
    if (
        not name.isascii()
        or len(name) > 64
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or any(character.isspace() for character in name)
    ):
        raise InitramfsCustomizationError(f"{label} is not a safe FAT root filename")
    return name


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InitramfsCustomizationError(f"{label} must be an integer")
    return value


def _positive(value: object, label: str) -> int:
    result = _integer(value, label)
    if result <= 0:
        raise InitramfsCustomizationError(f"{label} must be positive")
    return result


def _uint32(value: object, label: str) -> int:
    result = _integer(value, label)
    if not 0 <= result <= 0xFFFFFFFF:
        raise InitramfsCustomizationError(f"{label} must be unsigned 32-bit")
    return result


def _mode(value: object, label: str) -> int:
    mode = _uint32(value, label)
    if stat.S_IFMT(mode) not in {stat.S_IFREG, stat.S_IFDIR}:
        raise InitramfsCustomizationError(f"{label} has an unsupported file type")
    if mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
        raise InitramfsCustomizationError(f"{label} has unsafe privilege bits")
    return mode


def _sha(value: object, label: str) -> str:
    text = _text(value, label)
    if len(text) != SHA256_HEX_LENGTH or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise InitramfsCustomizationError(f"{label} must be canonical SHA-256")
    return text


def _safe_path(value: object, label: str) -> str:
    text = _text(value, label)
    path = PurePosixPath(text)
    if (
        text.startswith("/")
        or "\\" in text
        or any(part in {"", ".", ".."} for part in text.split("/"))
    ):
        raise InitramfsCustomizationError(f"{label} is not a safe relative POSIX path")
    if str(path) != text:
        raise InitramfsCustomizationError(f"{label} is not canonical")
    return text
