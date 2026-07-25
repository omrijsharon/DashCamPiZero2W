"""Non-destructive regular-file validation for the storage partition contract.

This is deliberately a test harness, not a provisioning executor.  It only ever
passes a newly-created sparse *regular file* to ``sfdisk``; it does not allocate
loop devices, mount filesystems, invoke privilege escalation, or inspect host
block devices.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from dashcam.provisioning.layout import LayoutSpec

SECTOR_BYTES: Final = 512
MAX_OUTPUT_BYTES: Final = 128 * 1024
COMMAND_TIMEOUT_SECONDS: Final = 15
ALLOWED_TOOLS: Final = ("sfdisk",)
SUPPORTED_CAPACITY_BYTES: Final = (31_457_280_000, 64_000_000_000)


class LoopbackStatus(StrEnum):
    PASSED = "passed"
    SKIPPED = "skipped"
    REFUSED = "refused"


class LoopbackRefusal(ValueError):
    """A caller or filesystem safety gate rejected the harness request."""


class InjectedFault(RuntimeError):
    """A deliberately injected stop after a named transaction phase."""


@dataclass(frozen=True, slots=True)
class Toolset:
    sfdisk: str


@dataclass(frozen=True, slots=True)
class LoopbackReport:
    status: LoopbackStatus
    code: str
    messages: tuple[str, ...]
    cases: tuple[dict[str, object], ...] = ()

    def to_dict(self) -> dict[str, object]:
        return asdict(self) | {"status": self.status.value}


@dataclass(frozen=True, slots=True)
class Geometry:
    capacity_bytes: int
    boot_start: int
    boot_size: int
    root_start: int
    root_size: int
    data_start: int
    data_size: int
    data_end: int


def discover_tools() -> Toolset | None:
    """Return the exact allowed Linux tools, or ``None`` without probing devices."""

    if platform.system() != "Linux":
        return None
    resolved: dict[str, str] = {}
    for name in ALLOWED_TOOLS:
        candidate = shutil.which(name)
        if candidate is None:
            return None
        path = Path(candidate)
        try:
            info = path.stat()
        except OSError:
            return None
        if path.is_symlink() or not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK):
            return None
        resolved[name] = str(path.resolve(strict=True))
    return Toolset(sfdisk=resolved["sfdisk"])


def prepare_output_directory(output_dir: Path) -> Path:
    """Accept only a caller-created, empty, real directory for harness output."""

    if not output_dir.is_absolute():
        raise LoopbackRefusal("output directory must be an explicit absolute path")
    try:
        info = output_dir.lstat()
    except OSError as exc:
        raise LoopbackRefusal("output directory must be created by the caller") from exc
    if output_dir.is_symlink() or not stat.S_ISDIR(info.st_mode):
        raise LoopbackRefusal("output directory must be a real directory")
    if output_dir.absolute() != output_dir.resolve(strict=True):
        raise LoopbackRefusal("output directory may not traverse a symlink")
    if any(output_dir.iterdir()):
        raise LoopbackRefusal("output directory already contains owner output")
    return output_dir.resolve(strict=True)


def assert_disposable_regular_file(path: Path, output_dir: Path, *, must_exist: bool) -> Path:
    """Reject all special files and every path outside the caller-owned output tree."""

    root = output_dir.resolve(strict=True)
    candidate = path if path.is_absolute() else root / path
    # ``resolve`` may follow a malicious existing symlink, so check every existing
    # component first and then require the lexical destination to remain in root.
    if any(part == ".." for part in candidate.parts):
        raise LoopbackRefusal("parent traversal is refused")
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise LoopbackRefusal("host root/system paths are refused") from exc
    if candidate.exists() or candidate.is_symlink():
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise LoopbackRefusal("symlink targets are refused")
        if not stat.S_ISREG(info.st_mode):
            raise LoopbackRefusal(
                "block, character, directory, FIFO, and socket targets are refused"
            )
        if not must_exist:
            raise LoopbackRefusal("existing owner output is refused")
    elif must_exist:
        raise LoopbackRefusal("required regular image does not exist")
    return candidate


def geometry_for(spec: LayoutSpec, capacity_bytes: int) -> Geometry:
    """Compute a reviewed nominal-card target without involving a host device."""

    if capacity_bytes not in SUPPORTED_CAPACITY_BYTES:
        raise LoopbackRefusal("only the declared nominal-card byte capacities are allowed")
    if capacity_bytes % spec.sector_size_bytes:
        raise LoopbackRefusal("capacity is not an exact number of sectors")
    total_sectors = capacity_bytes // spec.sector_size_bytes
    root_size = spec.root_target_sectors
    root_start = spec.root.source_start_sector
    boot_start = spec.boot.source_start_sector
    boot_size = spec.boot.source_size_sectors
    if root_start is None or boot_start is None or boot_size is None:
        raise LoopbackRefusal("layout source geometry is incomplete")
    data_start = _align_up(root_start + root_size, spec.alignment_sectors)
    last_usable = total_sectors - spec.trailing_reserve_sectors - 1
    data_end = _align_down(last_usable + 1, spec.alignment_sectors) - 1
    data_size = data_end - data_start + 1
    if data_size < spec.minimum_data_sectors:
        raise LoopbackRefusal("computed data partition does not meet the contract minimum")
    return Geometry(
        capacity_bytes=capacity_bytes,
        boot_start=boot_start,
        boot_size=boot_size,
        root_start=root_start,
        root_size=root_size,
        data_start=data_start,
        data_size=data_size,
        data_end=data_end,
    )


def run_validation(
    output_dir: Path, spec: LayoutSpec, *, fault_matrix: bool = True
) -> LoopbackReport:
    """Run regular-file-only contract checks, or emit explicit skipped evidence."""

    try:
        root = prepare_output_directory(output_dir)
    except LoopbackRefusal as exc:
        return LoopbackReport(LoopbackStatus.REFUSED, "unsafe_output", (str(exc),))
    tools = discover_tools()
    if tools is None:
        return LoopbackReport(
            LoopbackStatus.SKIPPED,
            "dependencies_unavailable",
            (
                "Linux sfdisk was not available as an allow-listed regular executable; "
                "no image was created.",
            ),
        )
    try:
        cases = [
            _run_case(root / f"case-{capacity}", spec, capacity, tools)
            for capacity in SUPPORTED_CAPACITY_BYTES
        ]
        if fault_matrix:
            fault_cases = _run_fault_matrix(root / "faults", spec, tools)
            cases.extend(fault_cases)
    except (LoopbackRefusal, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        return LoopbackReport(LoopbackStatus.REFUSED, "validation_failed", (str(exc),))
    return LoopbackReport(
        LoopbackStatus.PASSED,
        "regular_file_contract_validated",
        ("No loop device, mount, root privilege, or host block device was used.",),
        tuple(cases),
    )


def _run_case(
    case_dir: Path, spec: LayoutSpec, capacity_bytes: int, tools: Toolset
) -> dict[str, object]:
    case_dir.mkdir(mode=0o700)
    image = assert_disposable_regular_file(case_dir / "disk.img", case_dir, must_exist=False)
    source = _create_source_image(image, spec, capacity_bytes, tools)
    backup = case_dir / "partition-table.sfdisk"
    phases: list[str] = []
    _backup(tools, image, backup)
    phases.append("backup")
    _validate_backup(backup, source, spec)
    _assert_backup_identity_refusal(case_dir, backup, source, spec)
    phases.append("validate_backup")
    backup_digest = _backup_digest(backup.read_bytes())
    target = geometry_for(spec, capacity_bytes)
    _assert_data_signature_refusal(image, target)
    _write_table(tools, image, source, target)
    phases.append("write_target")
    _validate_target(tools, image, source, target)
    phases.append("verify_target")
    _restore(tools, image, backup)
    phases.append("restore_backup")
    _validate_source(tools, image, source)
    restored_dump = _run(tools.sfdisk, ("--dump", str(image))).stdout
    if _backup_digest(restored_dump) != backup_digest:
        raise LoopbackRefusal("backup restore digest differs from the saved partition table")
    phases.append("verify_restore")
    _write_table(tools, image, source, target)
    phases.append("write_final")
    _validate_target(tools, image, source, target)
    phases.append("verify_final")
    # A recheck observes the completed table and takes no mutation branch.
    _validate_target(tools, image, source, target)
    return {
        "case": f"{capacity_bytes}B",
        "phases": phases,
        "geometry": asdict(target),
        "backup_restore_sha256": backup_digest,
        "backup_identity_refusal": True,
        "data_signature_refusal": True,
        "idempotent": True,
    }


def _run_fault_matrix(root: Path, spec: LayoutSpec, tools: Toolset) -> list[dict[str, object]]:
    root.mkdir(mode=0o700)
    result: list[dict[str, object]] = []
    for phase in _transaction_phases():
        case = root / phase
        case.mkdir(mode=0o700)
        image = assert_disposable_regular_file(case / "disk.img", case, must_exist=False)
        try:
            _transaction_with_fault(image, case / "backup.sfdisk", spec, tools, phase)
        except InjectedFault:
            # The file remains a harness-owned regular file; intentionally do not
            # attempt cleanup that could mask the transaction boundary evidence.
            assert_disposable_regular_file(image, case, must_exist=True)
            result.append({"fault_after": phase, "status": "injected_and_contained"})
        else:  # pragma: no cover - this catches errors in the fault injector itself.
            raise LoopbackRefusal(f"fault injection did not stop after {phase}")
    return result


def _transaction_phases() -> tuple[str, ...]:
    return (
        "backup",
        "validate_backup",
        "write_target",
        "verify_target",
        "restore_backup",
        "verify_restore",
        "write_final",
        "verify_final",
    )


def _transaction_with_fault(
    image: Path, backup: Path, spec: LayoutSpec, tools: Toolset, stop_after: str
) -> None:
    source = _create_source_image(image, spec, SUPPORTED_CAPACITY_BYTES[0], tools)
    for phase in _transaction_phases():
        target = geometry_for(spec, SUPPORTED_CAPACITY_BYTES[0])
        if phase == "backup":
            _backup(tools, image, backup)
        elif phase == "validate_backup":
            _validate_backup(backup, source, spec)
        elif phase in {"write_target", "write_final"}:
            _write_table(tools, image, source, target)
        elif phase in {"verify_target", "verify_final"}:
            _validate_target(tools, image, source, target)
        elif phase == "restore_backup":
            _restore(tools, image, backup)
        else:
            _validate_source(tools, image, source)
        if phase == stop_after:
            raise InjectedFault(phase)


def _create_source_image(
    image: Path, spec: LayoutSpec, capacity_bytes: int, tools: Toolset
) -> Geometry:
    image = assert_disposable_regular_file(image, image.parent, must_exist=False)
    with image.open("xb") as stream:
        stream.truncate(capacity_bytes)
    source_root_size = spec.root.source_size_sectors
    boot_start = spec.boot.source_start_sector
    boot_size = spec.boot.source_size_sectors
    root_start = spec.root.source_start_sector
    if source_root_size is None or boot_start is None or boot_size is None or root_start is None:
        raise LoopbackRefusal("layout source geometry is incomplete")
    source = Geometry(
        capacity_bytes,
        boot_start,
        boot_size,
        root_start,
        source_root_size,
        0,
        0,
        0,
    )
    _run(tools.sfdisk, ("--no-reread", "--force", str(image)), _table_text(spec, source, None))
    _validate_source(tools, image, source)
    return source


def _backup(tools: Toolset, image: Path, backup: Path) -> None:
    completed = _run(tools.sfdisk, ("--dump", str(image)))
    with backup.open("xb") as stream:
        stream.write(completed.stdout)


def _validate_backup(backup: Path, source: Geometry, spec: LayoutSpec) -> None:
    payload = backup.read_bytes()
    if len(payload) == 0 or len(payload) > MAX_OUTPUT_BYTES:
        raise LoopbackRefusal("partition-table backup has an invalid bounded size")
    expected = _backup_digest(payload)
    if expected != _backup_digest(backup.read_bytes()):
        raise LoopbackRefusal("partition-table backup digest is unstable")
    text = re.sub(r"=\s+", "=", payload.decode("utf-8", "strict"))
    required = (
        "label: dos",
        f"label-id: {_MBR_ID}",
        f"start={source.boot_start}",
        f"size={source.boot_size}",
        f"start={source.root_start}",
        f"size={source.root_size}",
    )
    if any(item not in text for item in required) or "start=0" in text:
        raise LoopbackRefusal("partition-table backup signature or identity is invalid")
    if spec.root_target_sectors <= source.root_size:
        raise LoopbackRefusal("validation would shrink or retain the source root")


def _assert_backup_identity_refusal(
    case_dir: Path, backup: Path, source: Geometry, spec: LayoutSpec
) -> None:
    """Prove a modified backup is rejected before it could be restored."""

    corrupted = case_dir / "partition-table-corrupted.sfdisk"
    payload = backup.read_bytes().replace(_MBR_ID.encode("ascii"), b"0x00000000", 1)
    with corrupted.open("xb") as stream:
        stream.write(payload)
    try:
        _validate_backup(corrupted, source, spec)
    except LoopbackRefusal:
        return
    raise LoopbackRefusal("corrupted backup signature was accepted")


def _assert_data_signature_refusal(image: Path, target: Geometry) -> None:
    """Prove a recognized exFAT signature in the proposed p3 region is refused."""

    signature_offset = target.data_start * SECTOR_BYTES + 3
    with image.open("r+b") as stream:
        stream.seek(signature_offset)
        original = stream.read(8)
        stream.seek(signature_offset)
        stream.write(b"EXFAT   ")
        stream.flush()
        os.fsync(stream.fileno())
    try:
        try:
            _require_blank_data_region(image, target)
        except LoopbackRefusal:
            return
        raise LoopbackRefusal("recognized data-region signature was accepted")
    finally:
        with image.open("r+b") as stream:
            stream.seek(signature_offset)
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())


def _restore(tools: Toolset, image: Path, backup: Path) -> None:
    _run(tools.sfdisk, ("--no-reread", "--force", str(image)), backup.read_text(encoding="utf-8"))


def _write_table(tools: Toolset, image: Path, source: Geometry, target: Geometry) -> None:
    if target.root_size < source.root_size or target.root_start != source.root_start:
        raise LoopbackRefusal("root partition shrink or movement is refused")
    _require_blank_data_region(image, target)
    _run(tools.sfdisk, ("--no-reread", "--force", str(image)), _table_text(None, source, target))


def _require_blank_data_region(image: Path, target: Geometry) -> None:
    """Reject the bounded set of filesystem signatures relevant to fresh p3."""

    base = target.data_start * SECTOR_BYTES
    probes = (
        (3, b"EXFAT   ", "exFAT"),
        (54, b"FAT", "FAT"),
        (82, b"FAT32", "FAT32"),
        (1024 + 56, b"\x53\xef", "ext"),
        (0, b"LUKS\xba\xbe", "LUKS"),
    )
    with image.open("rb") as stream:
        for relative, signature, name in probes:
            stream.seek(base + relative)
            if stream.read(len(signature)) == signature:
                raise LoopbackRefusal(
                    f"recognized {name} signature exists in the proposed data region"
                )


def _validate_source(tools: Toolset, image: Path, source: Geometry) -> None:
    table = _read_table(tools, image)
    partitions = _partitions(table)
    if len(partitions) != 2 or _MBR_ID not in str(table.get("id", "")).lower():
        raise LoopbackRefusal("backup restore did not preserve the DOS/MBR signature")
    _expect_partition(partitions[0], source.boot_start, source.boot_size, "c")
    _expect_partition(partitions[1], source.root_start, source.root_size, "83")


def _validate_target(tools: Toolset, image: Path, source: Geometry, target: Geometry) -> None:
    table = _read_table(tools, image)
    partitions = _partitions(table)
    if len(partitions) != 3 or _MBR_ID not in str(table.get("id", "")).lower():
        raise LoopbackRefusal("written DOS/MBR identity was not preserved")
    _expect_partition(partitions[0], source.boot_start, source.boot_size, "c")
    _expect_partition(partitions[1], target.root_start, target.root_size, "83")
    _expect_partition(partitions[2], target.data_start, target.data_size, "7")
    if target.root_size != 6 * 1024**3 // SECTOR_BYTES:
        raise LoopbackRefusal("root partition is not exactly 6GiB")
    if target.data_start % 2048 or (target.data_end + 1) % 2048:
        raise LoopbackRefusal("recording partition is not 1MiB aligned")


def _read_table(tools: Toolset, image: Path) -> dict[str, object]:
    completed = _run(tools.sfdisk, ("--json", str(image)))
    raw = json.loads(completed.stdout)
    if not isinstance(raw, dict) or not isinstance(raw.get("partitiontable"), dict):
        raise LoopbackRefusal("sfdisk JSON output has an unexpected shape")
    table = raw["partitiontable"]
    if not isinstance(table, dict):
        raise LoopbackRefusal("sfdisk partition table has an unexpected shape")
    return table


def _partitions(table: dict[str, object]) -> list[dict[str, object]]:
    raw = table.get("partitions")
    if not isinstance(raw, list) or len(raw) > 4 or not all(isinstance(item, dict) for item in raw):
        raise LoopbackRefusal("sfdisk partition list is invalid")
    return raw


def _expect_partition(partition: dict[str, object], start: int, size: int, kind: str) -> None:
    if partition.get("start") != start or partition.get("size") != size:
        raise LoopbackRefusal("partition geometry differs from the contract")
    observed_type = str(partition.get("type", "")).lower().removeprefix("0x")
    if observed_type != kind:
        raise LoopbackRefusal("partition type differs from the contract")


_MBR_ID: Final = "0x4f2c9ea0"


def _table_text(spec: LayoutSpec | None, source: Geometry, target: Geometry | None) -> str:
    del spec
    root = source if target is None else target
    lines = [
        "label: dos",
        f"label-id: {_MBR_ID}",
        "unit: sectors",
        "",
        f"start={source.boot_start}, size={source.boot_size}, type=c",
        f"start={root.root_start}, size={root.root_size}, type=83",
    ]
    if target is not None:
        lines.append(f"start={target.data_start}, size={target.data_size}, type=7")
    return "\n".join(lines) + "\n"


def _backup_digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _run(
    executable: str, arguments: Iterable[str], stdin: str | None = None
) -> subprocess.CompletedProcess[bytes]:
    argv = (executable, *arguments)
    completed = subprocess.run(
        argv,
        input=None if stdin is None else stdin.encode("utf-8"),
        capture_output=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
        check=False,
    )
    if len(completed.stdout) + len(completed.stderr) > MAX_OUTPUT_BYTES:
        raise LoopbackRefusal("allow-listed tool exceeded the bounded output budget")
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()[:1024]
        raise LoopbackRefusal(f"allow-listed command failed: {Path(executable).name}: {detail}")
    return completed


def _align_up(value: int, alignment: int) -> int:
    return ((value + alignment - 1) // alignment) * alignment


def _align_down(value: int, alignment: int) -> int:
    return (value // alignment) * alignment


__all__ = [
    "SUPPORTED_CAPACITY_BYTES",
    "Geometry",
    "InjectedFault",
    "LoopbackRefusal",
    "LoopbackReport",
    "LoopbackStatus",
    "assert_disposable_regular_file",
    "discover_tools",
    "geometry_for",
    "prepare_output_directory",
    "run_validation",
]
