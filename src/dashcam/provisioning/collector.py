"""Bounded, read-only Linux storage observation collection.

This module is deliberately separate from layout verification.  It only reads
kernel and userspace inventory; it never opens a block device itself, mounts a
filesystem, or invokes a state-changing storage command.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import threading
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import PurePosixPath
from typing import IO, Final, Protocol, cast

from dashcam.provisioning.layout import (
    LayoutError,
    PartitionObservation,
    fingerprint_partition_table,
    mbr_partuuid,
    observation_from_mapping,
)

COMMAND_TIMEOUT_SECONDS: Final = 5.0
MAX_COMMAND_OUTPUT_BYTES: Final = 128 * 1024
MAX_PARTITIONS: Final = 16
_DEVICE_PATH_RE: Final = re.compile(r"/dev/[A-Za-z0-9._/-]{1,120}\Z")
_KNAME_RE: Final = re.compile(r"[A-Za-z0-9._-]{1,120}\Z")
_MMC_KNAME_RE: Final = re.compile(r"mmcblk[0-9]+\Z")
_SERIAL_RE: Final = re.compile(r"[A-Za-z0-9._:+-]{1,128}\Z")
_PARTITION_PATH_RE: Final = re.compile(r"/dev/[A-Za-z0-9._/-]{1,120}\Z")
_TABLE_SIGNATURES: Final = frozenset({"dos", "gpt", "pmbr"})

_READLINK: Final = "/usr/bin/readlink"
_LSBLK_BIN: Final = "/usr/bin/lsblk"
_SFDISK: Final = "/usr/sbin/sfdisk"
_FINDMNT: Final = "/usr/bin/findmnt"
_BLKID: Final = "/usr/sbin/blkid"
_WIPEFS: Final = "/usr/sbin/wipefs"
_CAT: Final = "/usr/bin/cat"
_LSBLK: Final = (
    _LSBLK_BIN,
    "--json",
    "--bytes",
    "--output",
    "PATH,KNAME,PKNAME,TYPE,SERIAL,SIZE,LOG-SEC,MOUNTPOINTS",
)


class StorageCollectorError(ValueError):
    """Raised when read-only evidence is absent, malformed, or ambiguous."""


class CommandRunner(Protocol):
    """Injectable, bounded command boundary."""

    def run(self, argv: tuple[str, ...], *, timeout_seconds: float, max_output_bytes: int) -> str:
        """Return stdout for one allowlisted command or raise ``StorageCollectorError``."""


class SubprocessCommandRunner:
    """Production command adapter; it accepts only collector command shapes."""

    def run(self, argv: tuple[str, ...], *, timeout_seconds: float, max_output_bytes: int) -> str:
        if not _allowed_argv(argv):
            raise StorageCollectorError("command is not allowlisted")
        if not 0 < timeout_seconds <= COMMAND_TIMEOUT_SECONDS:
            raise StorageCollectorError("invalid command timeout")
        if not 0 < max_output_bytes <= MAX_COMMAND_OUTPUT_BYTES:
            raise StorageCollectorError("invalid command output bound")
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                close_fds=True,
            )
        except FileNotFoundError as exc:
            raise StorageCollectorError(f"executable unavailable: {argv[0]}") from exc
        except OSError as exc:
            raise StorageCollectorError(f"command start failed: {type(exc).__name__}") from exc

        stdout_parts: list[bytes] = []
        stderr_parts: list[bytes] = []
        overflow = threading.Event()
        output_budget = [max_output_bytes]
        output_lock = threading.Lock()
        assert process.stdout is not None
        assert process.stderr is not None
        readers = (
            threading.Thread(
                target=_drain,
                args=(
                    process,
                    process.stdout,
                    stdout_parts,
                    output_budget,
                    output_lock,
                    overflow,
                ),
                daemon=True,
            ),
            threading.Thread(
                target=_drain,
                args=(
                    process,
                    process.stderr,
                    stderr_parts,
                    output_budget,
                    output_lock,
                    overflow,
                ),
                daemon=True,
            ),
        )
        for reader in readers:
            reader.start()
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            with suppress(OSError):
                process.kill()
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=1.0)
            raise StorageCollectorError(f"command timed out: {argv[0]}") from exc
        finally:
            for reader in readers:
                reader.join(timeout=1.0)
            process.stdout.close()
            process.stderr.close()
        if overflow.is_set():
            with suppress(OSError):
                process.kill()
            raise StorageCollectorError(f"command output exceeded bound: {argv[0]}")
        if process.returncode != 0:
            detail = b"".join(stderr_parts).decode("utf-8", errors="replace").strip()[:256]
            suffix = f": {detail}" if detail else ""
            raise StorageCollectorError(f"command failed ({process.returncode}): {argv[0]}{suffix}")
        return b"".join(stdout_parts).decode("utf-8", errors="strict")


def _drain(
    process: subprocess.Popen[bytes],
    stream: IO[bytes],
    result: list[bytes],
    remaining: list[int],
    lock: threading.Lock,
    overflow: threading.Event,
) -> None:
    while True:
        chunk = stream.read(4096)
        if not chunk:
            return
        with lock:
            if len(chunk) > remaining[0]:
                result.append(chunk[: remaining[0]])
                remaining[0] = 0
                overflow.set()
            else:
                result.append(chunk)
                remaining[0] -= len(chunk)
        if overflow.is_set():
            with suppress(OSError):
                process.kill()
            return


def collect_storage_observation(
    device_path: str, runner: CommandRunner | None = None
) -> dict[str, object]:
    """Collect one complete, validated ``DeviceObservation`` mapping.

    ``device_path`` may name a disk or one of its partitions, but the emitted
    identity is always the canonical whole-disk path.  Any disagreement between
    sources is treated as an error instead of being guessed around.
    """

    _validate_candidate_path(device_path)
    active_runner = SubprocessCommandRunner() if runner is None else runner
    resolved_input = _one_line(
        _run(active_runner, (_READLINK, "-f", device_path)), "resolved device path"
    )
    _validate_candidate_path(resolved_input)
    block_tree = _json_object(_run(active_runner, _LSBLK), "lsblk")
    nodes = _parse_lsblk(block_tree)
    selected = _single_node(nodes, "path", resolved_input, "requested device")
    disk = _disk_ancestor(selected, nodes)
    disk_path = _required_text(disk, "path")
    disk_kname = _required_text(disk, "kname")

    sfdisk = _json_object(_run(active_runner, (_SFDISK, "--json", disk_path)), "sfdisk")
    table, parts = _parse_sfdisk(sfdisk, disk_path)
    sector_size = _required_positive_int(disk, "log-sec")
    size_bytes = _required_positive_int(disk, "size")
    if size_bytes % sector_size:
        raise StorageCollectorError("disk size is not an exact number of logical sectors")
    total_sectors = size_bytes // sector_size
    table_sector_size = table.get("sectorsize")
    if (
        table_sector_size is not None
        and _positive_int(table_sector_size, "sfdisk sectorsize") != sector_size
    ):
        raise StorageCollectorError("sfdisk and lsblk logical sector sizes disagree")

    cid = None
    if _MMC_KNAME_RE.fullmatch(disk_kname):
        cid_result = _run_optional(
            active_runner, (_CAT, f"/sys/class/block/{disk_kname}/device/cid")
        )
        if cid_result is not None:
            cid = _canonical_serial(_one_line(cid_result, "MMC CID"), "MMC CID")
    serial = cid or _canonical_serial(_required_text(disk, "serial"), "disk serial")

    table_type = _canonical_table_type(table)
    mbr_disk_id = _canonical_mbr_id(table.get("id")) if table_type == "dos" else None
    if mbr_disk_id is None:
        raise StorageCollectorError("a canonical DOS/MBR disk ID is required")
    root_source = _one_line(
        _run(active_runner, (_FINDMNT, "--noheadings", "--output", "SOURCE", "/")),
        "root source",
    )
    root_disk = _root_disk(root_source, nodes)
    is_root_disk = root_disk == disk_path
    partition_observations = _partition_observations(
        active_runner, disk_path, mbr_disk_id, parts, nodes
    )
    unpartitioned = _unpartitioned_signatures(active_runner, disk_path)
    is_system_disk = is_root_disk or any(part.mount_points for part in partition_observations)
    fingerprint = fingerprint_partition_table(
        table_type=table_type,
        mbr_disk_id=mbr_disk_id,
        sector_size_bytes=sector_size,
        partitions=partition_observations,
    )
    mapping: dict[str, object] = {
        "identity": {
            "resolved_path": disk_path,
            "serial": serial,
            "size_bytes": size_bytes,
            "partition_table_fingerprint": fingerprint,
        },
        "device_path_is_resolved": resolved_input == device_path,
        "table_type": table_type,
        "mbr_disk_id": mbr_disk_id,
        "sector_size_bytes": sector_size,
        "total_sectors": total_sectors,
        "is_system_disk": is_system_disk,
        "is_root_disk": is_root_disk,
        "unpartitioned_data_signatures": list(unpartitioned),
        "partitions": [_partition_mapping(partition) for partition in partition_observations],
        "state_marker_layout_version": None,
        "state_marker_serial": None,
        "state_marker_uuid": None,
        "state_marker_source_table_fingerprint": None,
        "volume_sentinel_layout_version": None,
        "volume_sentinel_serial": None,
        "volume_sentinel_uuid": None,
        "volume_sentinel_source_table_fingerprint": None,
    }
    try:
        observation_from_mapping(mapping)
    except LayoutError as exc:
        raise StorageCollectorError(f"collected observation violates layout schema: {exc}") from exc
    return mapping


def _partition_observations(
    runner: CommandRunner,
    disk_path: str,
    mbr_disk_id: str,
    parts: Sequence[dict[str, object]],
    nodes: Sequence[dict[str, object]],
) -> tuple[PartitionObservation, ...]:
    result: list[PartitionObservation] = []
    numbered_parts: list[tuple[int, dict[str, object]]] = []
    for part in parts:
        node_path = _required_text(part, "node")
        _validate_partition_path(node_path)
        numbered_parts.append((_partition_number(disk_path, node_path), part))
    numbers = [number for number, _ in numbered_parts]
    if len(numbers) != len(set(numbers)):
        raise StorageCollectorError("sfdisk reports duplicate partition numbers")

    for number, part in sorted(numbered_parts):
        node_path = _required_text(part, "node")
        node = _single_node(nodes, "path", node_path, f"partition {number}")
        if _required_text(node, "type") != "part":
            raise StorageCollectorError(f"sfdisk partition {number} is not an lsblk partition")
        blkid = _parse_blkid_export(
            _run(runner, (_BLKID, "--probe", "--output", "export", node_path))
        )
        if blkid.get("DEVNAME") != node_path:
            raise StorageCollectorError(f"blkid device identity disagrees for partition {number}")
        filesystem = _optional_field(blkid, "TYPE")
        label = _optional_field(blkid, "LABEL")
        uuid = _optional_field(blkid, "UUID")
        exported_partuuid = _optional_field(blkid, "PARTUUID")
        entry_partuuid = _optional_field(blkid, "PART_ENTRY_UUID")
        if (
            exported_partuuid is not None
            and entry_partuuid is not None
            and exported_partuuid.casefold() != entry_partuuid.casefold()
        ):
            raise StorageCollectorError(f"blkid PARTUUID fields disagree for partition {number}")
        partuuid = _canonical_partuuid(exported_partuuid or entry_partuuid, number)
        expected_partuuid = mbr_partuuid(mbr_disk_id, number)
        sfdisk_uuid_raw = _optional_text(part.get("uuid"), "sfdisk partition UUID")
        sfdisk_uuid = (
            None if sfdisk_uuid_raw is None else _canonical_partuuid(sfdisk_uuid_raw, number)
        )
        if partuuid != expected_partuuid or (
            sfdisk_uuid is not None and sfdisk_uuid != expected_partuuid
        ):
            raise StorageCollectorError(
                f"partition {number} PARTUUID disagrees with the MBR disk ID"
            )
        partition_type = _canonical_partition_type(part.get("type"))
        entry_type = _optional_field(blkid, "PART_ENTRY_TYPE")
        if entry_type is not None and _canonical_partition_type(entry_type) != partition_type:
            raise StorageCollectorError(f"blkid and sfdisk types disagree for partition {number}")
        entry_number = _optional_field(blkid, "PART_ENTRY_NUMBER")
        if entry_number is not None and entry_number != str(number):
            raise StorageCollectorError(f"blkid partition number disagrees for partition {number}")
        start = _positive_int(part.get("start"), f"partition {number} start")
        size = _positive_int(part.get("size"), f"partition {number} size")
        result.append(
            PartitionObservation(
                number=number,
                start_sector=start,
                end_sector=start + size - 1,
                filesystem=filesystem,
                label=label,
                uuid=uuid,
                mount_points=_mount_points(node),
                has_data_signature=False,
                partition_type=partition_type,
                bootable=_bootable(part.get("bootable")),
                partuuid=partuuid,
            )
        )
    return tuple(result)


def _partition_number(disk_path: str, partition_path: str) -> int:
    separator = "p" if disk_path[-1].isdigit() else ""
    prefix = f"{disk_path}{separator}"
    if not partition_path.startswith(prefix):
        raise StorageCollectorError("partition node does not belong to the selected disk")
    suffix = partition_path[len(prefix) :]
    if re.fullmatch(r"[1-9][0-9]?", suffix) is None:
        raise StorageCollectorError("partition node has no canonical partition number")
    number = int(suffix)
    if number > MAX_PARTITIONS:
        raise StorageCollectorError("partition number exceeds the collector bound")
    return number


def _parse_lsblk(document: Mapping[str, object]) -> tuple[dict[str, object], ...]:
    devices = document.get("blockdevices")
    if not isinstance(devices, list):
        raise StorageCollectorError("lsblk JSON lacks a blockdevices list")
    flattened: list[dict[str, object]] = []

    def visit(value: object) -> None:
        if not isinstance(value, dict):
            raise StorageCollectorError("lsblk contains a non-object device")
        node = cast(dict[str, object], value)
        path = _required_text(node, "path")
        _validate_candidate_path(path)
        kname = _required_text(node, "kname")
        if _KNAME_RE.fullmatch(kname) is None:
            raise StorageCollectorError("lsblk KNAME is malformed")
        _required_text(node, "type")
        flattened.append(node)
        children = node.get("children", [])
        if not isinstance(children, list):
            raise StorageCollectorError("lsblk children is not a list")
        for child in children:
            visit(child)

    for device in devices:
        visit(device)
    if not flattened or len(flattened) > 256:
        raise StorageCollectorError("lsblk device count is invalid")
    paths = [_required_text(node, "path") for node in flattened]
    if len(paths) != len(set(paths)):
        raise StorageCollectorError("lsblk reports duplicate device paths")
    return tuple(flattened)


def _parse_sfdisk(
    document: Mapping[str, object], expected_device: str
) -> tuple[dict[str, object], tuple[dict[str, object], ...]]:
    raw = document.get("partitiontable")
    if not isinstance(raw, dict):
        raise StorageCollectorError("sfdisk JSON lacks a partitiontable object")
    table = cast(dict[str, object], raw)
    if _required_text(table, "device") != expected_device:
        raise StorageCollectorError("sfdisk table device disagrees with selected disk")
    if _required_text(table, "unit") != "sectors":
        raise StorageCollectorError("sfdisk table unit must be sectors")
    _required_text(table, "label")
    _required_text(table, "id")
    raw_parts = table.get("partitions")
    if not isinstance(raw_parts, list) or not raw_parts or len(raw_parts) > MAX_PARTITIONS:
        raise StorageCollectorError("sfdisk partitions must be a non-empty bounded list")
    parts: list[dict[str, object]] = []
    for value in raw_parts:
        if not isinstance(value, dict):
            raise StorageCollectorError("sfdisk partition is not an object")
        parts.append(cast(dict[str, object], value))
    return table, tuple(parts)


def _parse_blkid_export(value: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    try:
        tokens = shlex.split(value, posix=True)
    except ValueError as exc:
        raise StorageCollectorError("blkid export output is malformed") from exc
    for token in tokens:
        if "=" not in token:
            raise StorageCollectorError("blkid export contains a malformed field")
        key, field_value = token.split("=", 1)
        if not key or not field_value or key in fields:
            raise StorageCollectorError("blkid export contains a missing or duplicate field")
        fields[key] = field_value
    if not fields:
        raise StorageCollectorError("blkid export is empty")
    return fields


def _unpartitioned_signatures(runner: CommandRunner, disk_path: str) -> tuple[str, ...]:
    document = _json_object(_run(runner, (_WIPEFS, "--noheadings", "--json", disk_path)), "wipefs")
    raw = document.get("signatures", [])
    if not isinstance(raw, list) or len(raw) > MAX_PARTITIONS:
        raise StorageCollectorError("wipefs signatures must be a bounded list")
    found: list[str] = []
    for value in raw:
        if not isinstance(value, dict):
            raise StorageCollectorError("wipefs signature is not an object")
        signature = _required_text(cast(dict[str, object], value), "type").lower()
        if _SERIAL_RE.fullmatch(signature) is None:
            raise StorageCollectorError("wipefs signature type is malformed")
        if signature not in _TABLE_SIGNATURES:
            found.append(signature)
    if len(found) != len(set(found)):
        raise StorageCollectorError("wipefs reports duplicate unpartitioned signatures")
    return tuple(found)


def _root_disk(root_source: str, nodes: Sequence[dict[str, object]]) -> str:
    if not root_source.startswith("/dev/"):
        raise StorageCollectorError("root source is not a block-device path")
    node = _single_node(nodes, "path", root_source, "root source")
    return _required_text(_disk_ancestor(node, nodes), "path")


def _disk_ancestor(
    node: dict[str, object], nodes: Sequence[dict[str, object]]
) -> dict[str, object]:
    current = node
    visited: set[str] = set()
    while _required_text(current, "type") != "disk":
        path = _required_text(current, "path")
        if path in visited:
            raise StorageCollectorError("lsblk parent graph has a cycle")
        visited.add(path)
        parent = _optional_text(current.get("pkname"), "lsblk PKNAME")
        if parent is None:
            raise StorageCollectorError("selected device has no whole-disk ancestor")
        current = _single_node(nodes, "kname", parent, "parent disk")
    return current


def _single_node(
    nodes: Sequence[dict[str, object]], key: str, expected: str, description: str
) -> dict[str, object]:
    matches = [node for node in nodes if node.get(key) == expected]
    if len(matches) != 1:
        raise StorageCollectorError(f"{description} is absent or ambiguous in lsblk")
    return matches[0]


def _mount_points(node: Mapping[str, object]) -> tuple[str, ...]:
    raw = node.get("mountpoints", [])
    if raw is None:
        return ()
    if not isinstance(raw, list) or len(raw) > MAX_PARTITIONS:
        raise StorageCollectorError("lsblk mountpoints must be a bounded list")
    mounts: list[str] = []
    for value in raw:
        if value is None:
            continue
        if (
            not isinstance(value, str)
            or not value.startswith("/")
            or ".." in PurePosixPath(value).parts
        ):
            raise StorageCollectorError("lsblk reports a malformed mount point")
        mounts.append(value)
    if len(mounts) != len(set(mounts)):
        raise StorageCollectorError("lsblk reports duplicate mount points")
    return tuple(mounts)


def _allowed_argv(argv: tuple[str, ...]) -> bool:
    if argv == _LSBLK:
        return True
    if len(argv) == 3 and argv[:2] == (_READLINK, "-f"):
        return _is_safe_device_path(argv[2])
    if len(argv) == 3 and argv[:2] == (_SFDISK, "--json"):
        return _is_safe_device_path(argv[2])
    if argv == (_FINDMNT, "--noheadings", "--output", "SOURCE", "/"):
        return True
    if len(argv) == 5 and argv[:3] == (_BLKID, "--probe", "--output") and argv[3] == "export":
        return _is_safe_device_path(argv[4])
    if len(argv) == 4 and argv[:3] == (_WIPEFS, "--noheadings", "--json"):
        return _is_safe_device_path(argv[3])
    return (
        len(argv) == 2
        and argv[0] == _CAT
        and re.fullmatch(r"/sys/class/block/mmcblk[0-9]+/device/cid", argv[1]) is not None
    )


def _run(runner: CommandRunner, argv: tuple[str, ...]) -> str:
    return runner.run(
        argv, timeout_seconds=COMMAND_TIMEOUT_SECONDS, max_output_bytes=MAX_COMMAND_OUTPUT_BYTES
    )


def _run_optional(runner: CommandRunner, argv: tuple[str, ...]) -> str | None:
    try:
        return _run(runner, argv)
    except StorageCollectorError as exc:
        if "unavailable" in str(exc):
            return None
        raise


def _json_object(value: str, source: str) -> Mapping[str, object]:
    if len(value.encode("utf-8")) > MAX_COMMAND_OUTPUT_BYTES:
        raise StorageCollectorError(f"{source} output exceeds bound")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise StorageCollectorError(f"{source} output is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise StorageCollectorError(f"{source} JSON must be an object")
    return cast(Mapping[str, object], parsed)


def _one_line(value: str, description: str) -> str:
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if len(lines) != 1:
        raise StorageCollectorError(f"{description} must contain exactly one non-empty line")
    return lines[0]


def _required_text(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value or len(value) > 256:
        raise StorageCollectorError(f"missing or malformed required field: {key}")
    return value


def _optional_text(value: object, description: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 256:
        raise StorageCollectorError(f"missing or malformed field: {description}")
    return value


def _optional_field(fields: Mapping[str, str], name: str) -> str | None:
    value = fields.get(name)
    if value is None:
        return None
    if len(value) > 128 or _SERIAL_RE.fullmatch(value) is None:
        raise StorageCollectorError(f"blkid {name} is malformed")
    return value


def _positive_int(value: object, description: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise StorageCollectorError(f"{description} must be a positive integer")
    return value


def _required_positive_int(mapping: Mapping[str, object], key: str) -> int:
    return _positive_int(mapping.get(key), key)


def _canonical_serial(value: str, description: str) -> str:
    canonical = value.lower()
    if _SERIAL_RE.fullmatch(canonical) is None:
        raise StorageCollectorError(f"{description} is malformed")
    return canonical


def _canonical_table_type(table: Mapping[str, object]) -> str:
    table_type = _required_text(table, "label").lower()
    if table_type != "dos":
        raise StorageCollectorError("only canonical DOS/MBR tables can be observed")
    return table_type


def _canonical_mbr_id(value: object) -> str:
    raw = _optional_text(value, "MBR disk ID")
    if raw is None:
        raise StorageCollectorError("MBR disk ID is missing")
    canonical = raw.lower()
    if re.fullmatch(r"0x[0-9a-f]{8}", canonical) is None:
        raise StorageCollectorError("MBR disk ID is not canonical")
    return canonical


def _canonical_partition_type(value: object) -> str:
    raw = _optional_text(value, "partition type")
    if raw is None:
        raise StorageCollectorError("partition type is missing")
    digits = raw.lower().removeprefix("0x")
    if re.fullmatch(r"[0-9a-f]{1,2}", digits) is None:
        raise StorageCollectorError("partition type is malformed")
    return f"0x{int(digits, 16):02x}"


def _canonical_partuuid(value: str | None, number: int) -> str:
    if value is None:
        raise StorageCollectorError(f"partition {number} PARTUUID is missing")
    canonical = value.lower()
    if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{2}", canonical) is None:
        raise StorageCollectorError(f"partition {number} PARTUUID is malformed")
    return canonical


def _bootable(value: object) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise StorageCollectorError("partition bootable flag is malformed")
    return value


def _partition_mapping(partition: PartitionObservation) -> dict[str, object]:
    return {
        "number": partition.number,
        "start_sector": partition.start_sector,
        "end_sector": partition.end_sector,
        "filesystem": partition.filesystem,
        "label": partition.label,
        "uuid": partition.uuid,
        "mount_points": list(partition.mount_points),
        "has_data_signature": partition.has_data_signature,
        "partition_type": partition.partition_type,
        "bootable": partition.bootable,
        "partuuid": partition.partuuid,
    }


def _validate_candidate_path(path: str) -> None:
    if not _is_safe_device_path(path):
        raise StorageCollectorError("device path must be a bounded canonical /dev path")


def _validate_partition_path(path: str) -> None:
    if _PARTITION_PATH_RE.fullmatch(path) is None or ".." in PurePosixPath(path).parts:
        raise StorageCollectorError("partition path is malformed")


def _is_safe_device_path(path: str) -> bool:
    return _DEVICE_PATH_RE.fullmatch(path) is not None and ".." not in PurePosixPath(path).parts


__all__ = [
    "COMMAND_TIMEOUT_SECONDS",
    "MAX_COMMAND_OUTPUT_BYTES",
    "CommandRunner",
    "StorageCollectorError",
    "SubprocessCommandRunner",
    "collect_storage_observation",
]
