"""Safe release-image planning and source verification.

The separate executor accepts only a new regular ``.img`` output and currently
fails closed at the unproven initramfs/tool closure.  This module remains the
non-mutating planner and contains no block-device operation.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Final, NoReturn, cast

from dashcam.provisioning.initramfs_customizer import (
    PI_ZERO_2_W_ARMV7_INITRAMFS,
    PI_ZERO_2_W_ARMV7_PROFILE,
)

MIB: Final = 1024**2
GIB: Final = 1024**3
MAX_MANIFEST_BYTES: Final = 64 * 1024
MAX_PAYLOAD_FILES: Final = 64
MAX_PAYLOAD_ENTRIES: Final = 128
MAX_PAYLOAD_FILE_BYTES: Final = MIB
MAX_INITRAMFS_CLOSURE_BYTES: Final = 64 * 1024
HASH_CHUNK_BYTES: Final = MIB

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_MBR_ID_RE = re.compile(r"0x[0-9a-f]{8}")
_PARTITION_TYPE_RE = re.compile(r"0x[0-9a-f]{2}")
_FAT_UUID_RE = re.compile(r"[0-9A-F]{4}-[0-9A-F]{4}")
_EXT_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class ImageBuildRefusalCode(StrEnum):
    """Stable reason codes for image-builder refusals."""

    INVALID_MANIFEST = "invalid_manifest"
    SOURCE_NOT_REGULAR = "source_not_regular"
    SOURCE_SIZE_MISMATCH = "source_size_mismatch"
    SOURCE_HASH_MISMATCH = "source_hash_mismatch"
    RAW_SIZE_MISMATCH = "raw_size_mismatch"
    RAW_GEOMETRY_MISMATCH = "raw_geometry_mismatch"
    OUTPUT_PATH_UNSAFE = "output_path_unsafe"
    OUTPUT_EXISTS = "output_exists"
    PAYLOAD_UNSAFE = "payload_unsafe"
    INSUFFICIENT_CAPACITY = "insufficient_capacity"
    EXECUTION_DISABLED = "execution_disabled"


class ImageBuildRefused(ValueError):
    """Raised when a closed safety invariant is not satisfied."""

    def __init__(self, code: ImageBuildRefusalCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ArchiveManifest:
    filename: str
    url: str
    format: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class SourcePartition:
    number: int
    start_sector: int
    size_sectors: int
    partition_type: str
    bootable: bool
    filesystem: str
    filesystem_uuid: str

    @property
    def end_sector(self) -> int:
        return self.start_sector + self.size_sectors - 1


@dataclass(frozen=True, slots=True)
class RawImageManifest:
    size_bytes: int
    sector_size_bytes: int
    total_sectors: int
    table_type: str
    mbr_disk_id: str
    partitions: tuple[SourcePartition, ...]
    empty_partition_entries: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class TargetLayoutPolicy:
    root_size_bytes: int
    alignment_bytes: int
    trailing_reserve_bytes: int
    minimum_device_bytes: int
    minimum_data_bytes: int


@dataclass(frozen=True, slots=True)
class SourceImageManifest:
    schema_version: int
    archive: ArchiveManifest
    image: RawImageManifest
    target: TargetLayoutPolicy
    supported_capacity_examples_bytes: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class SourceVerification:
    path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class RawImageVerification:
    path: str
    size_bytes: int
    mbr_disk_id: str
    partitions: tuple[SourcePartition, ...]


@dataclass(frozen=True, slots=True)
class TargetLayout:
    capacity_bytes: int
    total_sectors: int
    root_start_sector: int
    root_end_sector: int
    data_start_sector: int
    data_end_sector: int

    @property
    def root_size_sectors(self) -> int:
        return self.root_end_sector - self.root_start_sector + 1

    @property
    def data_size_sectors(self) -> int:
        return self.data_end_sector - self.data_start_sector + 1


@dataclass(frozen=True, slots=True)
class PayloadFile:
    relative_path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class InitramfsClosureFile:
    path: str
    size_bytes: int
    sha256: str


class ImageBuildActionKind(StrEnum):
    VERIFY_SOURCE_ARCHIVE = "verify_source_archive"
    CREATE_OUTPUT_EXCLUSIVE = "create_output_exclusive"
    DECOMPRESS_SOURCE = "decompress_source"
    VERIFY_RAW_IMAGE = "verify_raw_image"
    OPEN_OUTPUT_IMAGE = "open_output_image"
    TRANSFORM_CMDLINE = "transform_cmdline"
    INSTALL_GATED_PAYLOAD = "install_gated_payload"
    REBUILD_INITRAMFS = "rebuild_initramfs"
    VERIFY_CUSTOMIZED_IMAGE = "verify_customized_image"


@dataclass(frozen=True, slots=True)
class ImageBuildAction:
    sequence: int
    kind: ImageBuildActionKind
    argv: tuple[str, ...]
    mutates_output: bool
    stdout_path: str | None = None
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "kind": self.kind.value,
            "argv": list(self.argv),
            "mutates_output": self.mutates_output,
            "stdout_path": self.stdout_path,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class ImageBuildPlan:
    schema_version: int
    dry_run: bool
    execution_supported: bool
    block_device_executor_included: bool
    source: SourceVerification
    output_path: str
    target_profile: str
    initramfs_closure: InitramfsClosureFile
    payload_files: tuple[PayloadFile, ...]
    target_layout_examples: tuple[TargetLayout, ...]
    actions: tuple[ImageBuildAction, ...]
    unresolved_runtime_gate: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "dry_run": self.dry_run,
            "execution_supported": self.execution_supported,
            "block_device_executor_included": self.block_device_executor_included,
            "source": asdict(self.source),
            "output_path": self.output_path,
            "target_profile": self.target_profile,
            "initramfs_closure": asdict(self.initramfs_closure),
            "payload_files": [asdict(item) for item in self.payload_files],
            "target_layout_examples": [
                {
                    **asdict(item),
                    "root_size_sectors": item.root_size_sectors,
                    "data_size_sectors": item.data_size_sectors,
                }
                for item in self.target_layout_examples
            ],
            "actions": [action.to_dict() for action in self.actions],
            "unresolved_runtime_gate": self.unresolved_runtime_gate,
        }


def load_source_manifest(payload: bytes) -> SourceImageManifest:
    """Parse a bounded and closed source-image manifest."""

    if len(payload) > MAX_MANIFEST_BYTES:
        _refuse(ImageBuildRefusalCode.INVALID_MANIFEST, "manifest is too large")
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ImageBuildRefused(
            ImageBuildRefusalCode.INVALID_MANIFEST, "manifest must be valid UTF-8 JSON"
        ) from exc
    root = _closed_mapping(
        decoded,
        {
            "schema_version",
            "archive",
            "image",
            "target",
            "supported_capacity_examples_bytes",
        },
        "manifest",
    )
    archive_raw = _closed_mapping(
        root["archive"], {"filename", "url", "format", "size_bytes", "sha256"}, "archive"
    )
    image_raw = _closed_mapping(
        root["image"],
        {
            "size_bytes",
            "sector_size_bytes",
            "total_sectors",
            "table_type",
            "mbr_disk_id",
            "partitions",
            "empty_partition_entries",
        },
        "image",
    )
    target_raw = _closed_mapping(
        root["target"],
        {
            "root_size_bytes",
            "alignment_bytes",
            "trailing_reserve_bytes",
            "minimum_device_bytes",
            "minimum_data_bytes",
        },
        "target",
    )
    partition_values = image_raw["partitions"]
    if not isinstance(partition_values, list):
        _refuse(ImageBuildRefusalCode.INVALID_MANIFEST, "image.partitions must be a list")
    partitions = tuple(
        _source_partition(value, f"image.partitions[{index}]")
        for index, value in enumerate(partition_values)
    )
    empty_entries = _integer_list(
        image_raw["empty_partition_entries"], "image.empty_partition_entries"
    )
    capacities = _integer_list(
        root["supported_capacity_examples_bytes"], "supported_capacity_examples_bytes"
    )
    manifest = SourceImageManifest(
        schema_version=_integer(root["schema_version"], "schema_version"),
        archive=ArchiveManifest(
            filename=_text(archive_raw["filename"], "archive.filename"),
            url=_text(archive_raw["url"], "archive.url"),
            format=_text(archive_raw["format"], "archive.format"),
            size_bytes=_positive_integer(archive_raw["size_bytes"], "archive.size_bytes"),
            sha256=_text(archive_raw["sha256"], "archive.sha256"),
        ),
        image=RawImageManifest(
            size_bytes=_positive_integer(image_raw["size_bytes"], "image.size_bytes"),
            sector_size_bytes=_positive_integer(
                image_raw["sector_size_bytes"], "image.sector_size_bytes"
            ),
            total_sectors=_positive_integer(image_raw["total_sectors"], "image.total_sectors"),
            table_type=_text(image_raw["table_type"], "image.table_type"),
            mbr_disk_id=_text(image_raw["mbr_disk_id"], "image.mbr_disk_id"),
            partitions=partitions,
            empty_partition_entries=empty_entries,
        ),
        target=TargetLayoutPolicy(
            root_size_bytes=_positive_integer(
                target_raw["root_size_bytes"], "target.root_size_bytes"
            ),
            alignment_bytes=_positive_integer(
                target_raw["alignment_bytes"], "target.alignment_bytes"
            ),
            trailing_reserve_bytes=_positive_integer(
                target_raw["trailing_reserve_bytes"], "target.trailing_reserve_bytes"
            ),
            minimum_device_bytes=_positive_integer(
                target_raw["minimum_device_bytes"], "target.minimum_device_bytes"
            ),
            minimum_data_bytes=_positive_integer(
                target_raw["minimum_data_bytes"], "target.minimum_data_bytes"
            ),
        ),
        supported_capacity_examples_bytes=capacities,
    )
    _validate_manifest(manifest)
    return manifest


def verify_source_archive(path: Path, manifest: SourceImageManifest) -> SourceVerification:
    """Verify the exact compressed source without modifying it."""

    source = _regular_source(path)
    if source.name != manifest.archive.filename or not source.name.endswith(".img.xz"):
        _refuse(
            ImageBuildRefusalCode.SOURCE_NOT_REGULAR,
            f"source filename must be the pinned {manifest.archive.filename!r}",
        )
    size = source.stat().st_size
    if size != manifest.archive.size_bytes:
        _refuse(
            ImageBuildRefusalCode.SOURCE_SIZE_MISMATCH,
            f"source size {size} does not match manifest size {manifest.archive.size_bytes}",
        )
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for chunk in iter(lambda: stream.read(HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != manifest.archive.sha256:
        _refuse(
            ImageBuildRefusalCode.SOURCE_HASH_MISMATCH,
            f"source SHA-256 {actual} does not match manifest SHA-256",
        )
    return SourceVerification(str(source), size, actual)


def verify_raw_image(path: Path, manifest: SourceImageManifest) -> RawImageVerification:
    """Verify exact raw-image size, MBR geometry, and filesystem identities."""

    raw_path = _regular_source(path)
    size = raw_path.stat().st_size
    if size != manifest.image.size_bytes:
        _refuse(
            ImageBuildRefusalCode.RAW_SIZE_MISMATCH,
            f"raw image size {size} does not match manifest size {manifest.image.size_bytes}",
        )
    with raw_path.open("rb") as stream:
        sector = stream.read(manifest.image.sector_size_bytes)
        if len(sector) != manifest.image.sector_size_bytes or sector[510:512] != b"\x55\xaa":
            _geometry_refusal("missing DOS/MBR signature")
        disk_id = f"0x{int.from_bytes(sector[440:444], 'little'):08x}"
        if disk_id != manifest.image.mbr_disk_id:
            _geometry_refusal(f"unexpected MBR disk ID {disk_id}")
        observed: list[SourcePartition] = []
        expected_by_number = {item.number: item for item in manifest.image.partitions}
        for number in range(1, 5):
            entry = sector[446 + (number - 1) * 16 : 446 + number * 16]
            status = entry[0]
            partition_type = entry[4]
            start = int.from_bytes(entry[8:12], "little")
            count = int.from_bytes(entry[12:16], "little")
            if number in manifest.image.empty_partition_entries:
                if any(entry):
                    _geometry_refusal(f"MBR partition entry {number} is not empty")
                continue
            expected = expected_by_number.get(number)
            if expected is None:
                _geometry_refusal(f"MBR partition entry {number} is unexpected")
            bootable = status == 0x80
            if status not in (0, 0x80):
                _geometry_refusal(f"partition {number} has invalid boot flag 0x{status:02x}")
            assert expected is not None
            if (
                start != expected.start_sector
                or count != expected.size_sectors
                or partition_type != int(expected.partition_type[2:], 16)
                or bootable != expected.bootable
            ):
                _geometry_refusal(f"partition {number} geometry or flags do not match manifest")
            filesystem_uuid = _read_filesystem_uuid(stream, expected, manifest.image)
            if filesystem_uuid != expected.filesystem_uuid:
                _geometry_refusal(
                    f"partition {number} filesystem UUID {filesystem_uuid!r} does not match"
                )
            observed.append(expected)
    return RawImageVerification(str(raw_path), size, disk_id, tuple(observed))


def calculate_target_layout(manifest: SourceImageManifest, capacity_bytes: int) -> TargetLayout:
    """Calculate the bounded 6-GiB-root/remainder-data geometry for a card."""

    sector_size = manifest.image.sector_size_bytes
    policy = manifest.target
    if capacity_bytes < policy.minimum_device_bytes:
        _refuse(
            ImageBuildRefusalCode.INSUFFICIENT_CAPACITY,
            f"capacity {capacity_bytes} is below minimum {policy.minimum_device_bytes}",
        )
    if capacity_bytes % sector_size != 0:
        _refuse(
            ImageBuildRefusalCode.INSUFFICIENT_CAPACITY,
            "capacity is not an exact number of logical sectors",
        )
    alignment_sectors = policy.alignment_bytes // sector_size
    reserve_sectors = policy.trailing_reserve_bytes // sector_size
    root_size_sectors = policy.root_size_bytes // sector_size
    root = manifest.image.partitions[1]
    root_end = root.start_sector + root_size_sectors - 1
    if root_end < root.end_sector:
        _refuse(
            ImageBuildRefusalCode.INVALID_MANIFEST,
            "target root would shrink the source root partition",
        )
    data_start = _align_up(root_end + 1, alignment_sectors)
    total_sectors = capacity_bytes // sector_size
    data_end_exclusive = _align_down(total_sectors - reserve_sectors, alignment_sectors)
    data_end = data_end_exclusive - 1
    if data_end < data_start:
        _refuse(ImageBuildRefusalCode.INSUFFICIENT_CAPACITY, "no aligned data partition fits")
    data_bytes = (data_end - data_start + 1) * sector_size
    if data_bytes < policy.minimum_data_bytes:
        _refuse(
            ImageBuildRefusalCode.INSUFFICIENT_CAPACITY,
            f"data partition {data_bytes} is below minimum {policy.minimum_data_bytes}",
        )
    return TargetLayout(
        capacity_bytes=capacity_bytes,
        total_sectors=total_sectors,
        root_start_sector=root.start_sector,
        root_end_sector=root_end,
        data_start_sector=data_start,
        data_end_sector=data_end,
    )


def validate_output_path(source_path: Path, output_path: Path) -> Path:
    """Validate a path intended for a future exclusive regular-file create."""

    source = _regular_source(source_path)
    if not output_path.is_absolute():
        _refuse(
            ImageBuildRefusalCode.OUTPUT_PATH_UNSAFE,
            "output path must be absolute and explicit",
        )
    if _is_device_namespace(output_path):
        _refuse(
            ImageBuildRefusalCode.OUTPUT_PATH_UNSAFE,
            "output path may not use a device namespace",
        )
    try:
        output_stat = output_path.lstat()
    except FileNotFoundError:
        output_stat = None
    except OSError as exc:
        raise ImageBuildRefused(
            ImageBuildRefusalCode.OUTPUT_PATH_UNSAFE,
            f"cannot inspect output path: {exc}",
        ) from exc
    if output_stat is not None:
        if stat.S_ISLNK(output_stat.st_mode):
            _refuse(ImageBuildRefusalCode.OUTPUT_PATH_UNSAFE, "output path is a symlink")
        if stat.S_ISBLK(output_stat.st_mode) or stat.S_ISCHR(output_stat.st_mode):
            _refuse(ImageBuildRefusalCode.OUTPUT_PATH_UNSAFE, "output path is a device")
        _refuse(
            ImageBuildRefusalCode.OUTPUT_EXISTS,
            "output already exists; exclusive creation is required",
        )
    parent = output_path.parent
    try:
        parent_stat = parent.lstat()
    except OSError as exc:
        raise ImageBuildRefused(
            ImageBuildRefusalCode.OUTPUT_PATH_UNSAFE,
            f"output parent must already exist: {exc}",
        ) from exc
    if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
        _refuse(
            ImageBuildRefusalCode.OUTPUT_PATH_UNSAFE,
            "output parent must be a real directory, not a symlink",
        )
    resolved_output = output_path.resolve(strict=False)
    if resolved_output == source:
        _refuse(
            ImageBuildRefusalCode.OUTPUT_PATH_UNSAFE,
            "input and output paths must be different",
        )
    if output_path.suffix.lower() != ".img":
        _refuse(
            ImageBuildRefusalCode.OUTPUT_PATH_UNSAFE,
            "output must have an explicit .img filename",
        )
    return resolved_output


def inventory_payload(root: Path) -> tuple[PayloadFile, ...]:
    """Hash a small, symlink-free payload in deterministic path order."""

    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise ImageBuildRefused(
            ImageBuildRefusalCode.PAYLOAD_UNSAFE, f"cannot inspect payload root: {exc}"
        ) from exc
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        _refuse(ImageBuildRefusalCode.PAYLOAD_UNSAFE, "payload root must be a real directory")
    paths = sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())
    if len(paths) > MAX_PAYLOAD_ENTRIES:
        _refuse(ImageBuildRefusalCode.PAYLOAD_UNSAFE, "too many payload entries")
    files: list[PayloadFile] = []
    for path in paths:
        item_stat = path.lstat()
        if stat.S_ISLNK(item_stat.st_mode):
            _refuse(ImageBuildRefusalCode.PAYLOAD_UNSAFE, f"payload symlink refused: {path}")
        if stat.S_ISDIR(item_stat.st_mode):
            continue
        if not stat.S_ISREG(item_stat.st_mode):
            _refuse(ImageBuildRefusalCode.PAYLOAD_UNSAFE, f"non-regular payload refused: {path}")
        if item_stat.st_size > MAX_PAYLOAD_FILE_BYTES:
            _refuse(ImageBuildRefusalCode.PAYLOAD_UNSAFE, f"payload file too large: {path}")
        relative = path.relative_to(root).as_posix()
        if PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts:
            _refuse(ImageBuildRefusalCode.PAYLOAD_UNSAFE, f"unsafe payload path: {relative}")
        files.append(
            PayloadFile(relative, item_stat.st_size, hashlib.sha256(path.read_bytes()).hexdigest())
        )
        if len(files) > MAX_PAYLOAD_FILES:
            _refuse(ImageBuildRefusalCode.PAYLOAD_UNSAFE, "too many payload files")
    if not files:
        _refuse(ImageBuildRefusalCode.PAYLOAD_UNSAFE, "payload is empty")
    return tuple(files)


def bind_initramfs_closure(path: Path) -> tuple[InitramfsClosureFile, bytes]:
    """Bind exact, bounded, symlink-free closure-manifest bytes."""

    try:
        item = path.lstat()
    except OSError as exc:
        raise ImageBuildRefused(
            ImageBuildRefusalCode.PAYLOAD_UNSAFE,
            f"cannot inspect initramfs closure: {exc}",
        ) from exc
    if stat.S_ISLNK(item.st_mode) or not stat.S_ISREG(item.st_mode):
        _refuse(
            ImageBuildRefusalCode.PAYLOAD_UNSAFE,
            "initramfs closure must be a real regular file",
        )
    if item.st_size > MAX_INITRAMFS_CLOSURE_BYTES:
        _refuse(ImageBuildRefusalCode.PAYLOAD_UNSAFE, "initramfs closure is too large")
    resolved = path.resolve(strict=True)
    payload = resolved.read_bytes()
    try:
        after = path.lstat()
    except OSError as exc:
        raise ImageBuildRefused(
            ImageBuildRefusalCode.PAYLOAD_UNSAFE,
            f"cannot re-inspect initramfs closure: {exc}",
        ) from exc
    if (
        stat.S_ISLNK(after.st_mode)
        or not stat.S_ISREG(after.st_mode)
        or (after.st_dev, after.st_ino) != (item.st_dev, item.st_ino)
        or after.st_size != item.st_size
        or len(payload) != item.st_size
    ):
        _refuse(
            ImageBuildRefusalCode.PAYLOAD_UNSAFE,
            "initramfs closure changed while it was being bound",
        )
    return (
        InitramfsClosureFile(
            str(resolved),
            len(payload),
            hashlib.sha256(payload).hexdigest(),
        ),
        payload,
    )


def author_image_build_plan(
    *,
    manifest: SourceImageManifest,
    manifest_path: Path,
    source_archive: Path,
    output_image: Path,
    payload_root: Path,
    closure_manifest_path: Path | None = None,
    dry_run: bool = True,
    executor: Callable[[tuple[str, ...]], None] | None = None,
) -> ImageBuildPlan:
    """Verify inputs and author an inert deterministic customization plan."""

    del executor  # An executor is intentionally not reachable in this implementation.
    source = verify_source_archive(source_archive, manifest)
    output = validate_output_path(source_archive, output_image)
    payload = inventory_payload(payload_root)
    selected_closure_path = (
        Path(__file__).parents[3] / "deploy" / "image" / "initramfs-closure-v1.json"
        if closure_manifest_path is None
        else closure_manifest_path
    )
    closure, _closure_payload = bind_initramfs_closure(selected_closure_path)
    examples = tuple(
        calculate_target_layout(manifest, capacity)
        for capacity in manifest.supported_capacity_examples_bytes
    )
    if not dry_run:
        _refuse(
            ImageBuildRefusalCode.EXECUTION_DISABLED,
            "execution is disabled: the bounded runtime gate has not passed exact-image validation",
        )
    manifest_name = str(manifest_path.resolve(strict=True))
    payload_name = str(payload_root.resolve(strict=True))
    output_name = str(output)
    source_name = source.path
    internal = "dashcam-image-internal"
    actions = (
        ImageBuildAction(
            1,
            ImageBuildActionKind.VERIFY_SOURCE_ARCHIVE,
            (internal, "verify-source-archive", "--manifest", manifest_name, "--", source_name),
            False,
            note="Already performed by the planner before emitting this action.",
        ),
        ImageBuildAction(
            2,
            ImageBuildActionKind.CREATE_OUTPUT_EXCLUSIVE,
            (internal, "create-regular-output-exclusive", "--", output_name),
            True,
            note="Future executor must use O_CREAT|O_EXCL and reject symlinks/devices.",
        ),
        ImageBuildAction(
            3,
            ImageBuildActionKind.DECOMPRESS_SOURCE,
            ("xz", "--decompress", "--keep", "--stdout", "--", source_name),
            True,
            stdout_path=output_name,
            note="Stdout may target only the newly created regular output image.",
        ),
        ImageBuildAction(
            4,
            ImageBuildActionKind.VERIFY_RAW_IMAGE,
            (internal, "verify-raw-image", "--manifest", manifest_name, "--", output_name),
            False,
            note="Must run before any filesystem or partition customization.",
        ),
        ImageBuildAction(
            5,
            ImageBuildActionKind.OPEN_OUTPUT_IMAGE,
            (internal, "open-output-image-only", "--read-write", "--", output_name),
            True,
            note="No block-device target is accepted by this interface.",
        ),
        ImageBuildAction(
            6,
            ImageBuildActionKind.TRANSFORM_CMDLINE,
            (
                internal,
                "transform-cmdline",
                "--remove-exact-token",
                "resize",
                "--add-exact-token",
                "dashcam.bounded_provision=v1",
                "--preserve-all-other-tokens",
                "--",
                output_name,
            ),
            True,
            note="Preserves Raspberry Pi Imager firstrun tokens and refuses ambiguity.",
        ),
        ImageBuildAction(
            7,
            ImageBuildActionKind.INSTALL_GATED_PAYLOAD,
            (
                internal,
                "install-gated-payload",
                "--require-runtime-gate-absent",
                "--require-placeholder-refusal",
                "--payload",
                payload_name,
                "--",
                output_name,
            ),
            True,
            note="Candidates remain disabled because the exact-image gate file is absent.",
        ),
        ImageBuildAction(
            8,
            ImageBuildActionKind.REBUILD_INITRAMFS,
            (
                internal,
                "customize-exact-selected-initramfs",
                "--target-profile",
                PI_ZERO_2_W_ARMV7_PROFILE,
                "--closure",
                closure.path,
                "--fat-file",
                f"::{PI_ZERO_2_W_ARMV7_INITRAMFS}",
                "--",
                output_name,
            ),
            True,
            note=(
                "Customize only the manifest-bound initramfs7 regular-image bytes; "
                "never invoke update-initramfs or select the generic initramfs."
            ),
        ),
        ImageBuildAction(
            9,
            ImageBuildActionKind.VERIFY_CUSTOMIZED_IMAGE,
            (
                internal,
                "verify-customized-image",
                "--require-no-token",
                "resize",
                "--require-token",
                "dashcam.bounded_provision=v1",
                "--require-runtime-gate-absent",
                "--",
                output_name,
            ),
            False,
            note=(
                "A release cannot pass until the runtime gate is validated "
                "and intentionally enabled."
            ),
        ),
    )
    return ImageBuildPlan(
        schema_version=2,
        dry_run=True,
        execution_supported=False,
        block_device_executor_included=False,
        source=source,
        output_path=output_name,
        target_profile=PI_ZERO_2_W_ARMV7_PROFILE,
        initramfs_closure=closure,
        payload_files=payload,
        target_layout_examples=examples,
        actions=actions,
        unresolved_runtime_gate=(
            "REFUSED_PENDING_PI_VALIDATION: enable the gated runtime only after loopback "
            "review and explicitly authorized expendable-card tests prove backup, geometry, "
            "signature, idempotency, failure, and reboot behavior."
        ),
    )


def _validate_manifest(manifest: SourceImageManifest) -> None:
    if manifest.schema_version != 1:
        _refuse(ImageBuildRefusalCode.INVALID_MANIFEST, "schema_version must be 1")
    if manifest.archive.format != "xz":
        _refuse(ImageBuildRefusalCode.INVALID_MANIFEST, "archive format must be xz")
    if _SHA256_RE.fullmatch(manifest.archive.sha256) is None:
        _refuse(ImageBuildRefusalCode.INVALID_MANIFEST, "archive SHA-256 is not canonical")
    if not manifest.archive.url.startswith("https://downloads.raspberrypi.com/"):
        _refuse(ImageBuildRefusalCode.INVALID_MANIFEST, "archive URL is not the official host")
    image = manifest.image
    if image.table_type != "dos" or _MBR_ID_RE.fullmatch(image.mbr_disk_id) is None:
        _refuse(ImageBuildRefusalCode.INVALID_MANIFEST, "image must declare a canonical DOS MBR")
    if image.sector_size_bytes != 512:
        _refuse(ImageBuildRefusalCode.INVALID_MANIFEST, "only verified 512-byte sectors are valid")
    if image.size_bytes != image.sector_size_bytes * image.total_sectors:
        _refuse(ImageBuildRefusalCode.INVALID_MANIFEST, "raw size and sector count disagree")
    if tuple(item.number for item in image.partitions) != (1, 2):
        _refuse(ImageBuildRefusalCode.INVALID_MANIFEST, "source must contain only p1 and p2")
    if image.empty_partition_entries != (3, 4):
        _refuse(ImageBuildRefusalCode.INVALID_MANIFEST, "source MBR entries 3 and 4 must be empty")
    if image.partitions[0].end_sector >= image.partitions[1].start_sector:
        _refuse(ImageBuildRefusalCode.INVALID_MANIFEST, "source partitions overlap")
    if image.partitions[1].end_sector != image.total_sectors - 1:
        _refuse(ImageBuildRefusalCode.INVALID_MANIFEST, "source root must end at image boundary")
    target = manifest.target
    for name, value in (
        ("root size", target.root_size_bytes),
        ("alignment", target.alignment_bytes),
        ("trailing reserve", target.trailing_reserve_bytes),
        ("minimum device", target.minimum_device_bytes),
        ("minimum data", target.minimum_data_bytes),
    ):
        if value % image.sector_size_bytes:
            _refuse(ImageBuildRefusalCode.INVALID_MANIFEST, f"{name} is not sector-aligned")
    if target.root_size_bytes != 6 * GIB:
        _refuse(ImageBuildRefusalCode.INVALID_MANIFEST, "target root must be exactly 6 GiB")
    if target.alignment_bytes != MIB or target.trailing_reserve_bytes != MIB:
        _refuse(
            ImageBuildRefusalCode.INVALID_MANIFEST,
            "alignment and trailing reserve must both be exactly 1 MiB",
        )
    if len(manifest.supported_capacity_examples_bytes) != 2:
        _refuse(
            ImageBuildRefusalCode.INVALID_MANIFEST,
            "manifest must contain the reviewed 32 GB and 64 GB examples",
        )
    for capacity in manifest.supported_capacity_examples_bytes:
        calculate_target_layout(manifest, capacity)


def _source_partition(value: object, label: str) -> SourcePartition:
    raw = _closed_mapping(
        value,
        {
            "number",
            "start_sector",
            "size_sectors",
            "partition_type",
            "bootable",
            "filesystem",
            "filesystem_uuid",
        },
        label,
    )
    bootable = raw["bootable"]
    if not isinstance(bootable, bool):
        _refuse(ImageBuildRefusalCode.INVALID_MANIFEST, f"{label}.bootable must be boolean")
    partition = SourcePartition(
        number=_positive_integer(raw["number"], f"{label}.number"),
        start_sector=_positive_integer(raw["start_sector"], f"{label}.start_sector"),
        size_sectors=_positive_integer(raw["size_sectors"], f"{label}.size_sectors"),
        partition_type=_text(raw["partition_type"], f"{label}.partition_type"),
        bootable=bootable,
        filesystem=_text(raw["filesystem"], f"{label}.filesystem"),
        filesystem_uuid=_text(raw["filesystem_uuid"], f"{label}.filesystem_uuid"),
    )
    if _PARTITION_TYPE_RE.fullmatch(partition.partition_type) is None:
        _refuse(ImageBuildRefusalCode.INVALID_MANIFEST, f"{label} has invalid partition type")
    if partition.filesystem == "vfat":
        valid_uuid = _FAT_UUID_RE.fullmatch(partition.filesystem_uuid) is not None
    elif partition.filesystem == "ext4":
        valid_uuid = _EXT_UUID_RE.fullmatch(partition.filesystem_uuid) is not None
    else:
        valid_uuid = False
    if not valid_uuid:
        _refuse(ImageBuildRefusalCode.INVALID_MANIFEST, f"{label} has invalid filesystem identity")
    return partition


def _read_filesystem_uuid(
    stream: BinaryIO, partition: SourcePartition, image: RawImageManifest
) -> str:
    base = partition.start_sector * image.sector_size_bytes
    if partition.filesystem == "vfat":
        stream.seek(base + 67)
        serial = stream.read(4)
        if len(serial) != 4:
            _geometry_refusal("cannot read FAT volume identity")
        value = f"{int.from_bytes(serial, 'little'):08X}"
        return f"{value[:4]}-{value[4:]}"
    stream.seek(base + 1024 + 104)
    raw_uuid = stream.read(16)
    if len(raw_uuid) != 16:
        _geometry_refusal("cannot read ext4 UUID")
    return str(uuid.UUID(bytes=raw_uuid))


def _regular_source(path: Path) -> Path:
    try:
        item_stat = path.lstat()
    except OSError as exc:
        raise ImageBuildRefused(
            ImageBuildRefusalCode.SOURCE_NOT_REGULAR, f"cannot inspect source: {exc}"
        ) from exc
    if stat.S_ISLNK(item_stat.st_mode) or not stat.S_ISREG(item_stat.st_mode):
        _refuse(
            ImageBuildRefusalCode.SOURCE_NOT_REGULAR,
            "source must be a regular file, never a symlink or device",
        )
    return path.resolve(strict=True)


def _is_device_namespace(path: Path) -> bool:
    text = str(path)
    normalized = text.replace("\\", "/").lower()
    return (
        normalized == "/dev"
        or normalized.startswith("/dev/")
        or normalized.startswith("//./")
        or normalized.startswith("//?/globalroot/device/")
    )


def _closed_mapping(value: object, keys: set[str], label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        _refuse(ImageBuildRefusalCode.INVALID_MANIFEST, f"{label} must be an object")
    mapping = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in mapping):
        _refuse(ImageBuildRefusalCode.INVALID_MANIFEST, f"{label} keys must be strings")
    typed = cast(dict[str, object], mapping)
    missing = keys - set(typed)
    unknown = set(typed) - keys
    if missing or unknown:
        _refuse(
            ImageBuildRefusalCode.INVALID_MANIFEST,
            f"{label} has missing keys {sorted(missing)} or unknown keys {sorted(unknown)}",
        )
    return typed


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        _refuse(ImageBuildRefusalCode.INVALID_MANIFEST, f"{label} must be non-empty text")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _refuse(ImageBuildRefusalCode.INVALID_MANIFEST, f"{label} must be an integer")
    return value


def _positive_integer(value: object, label: str) -> int:
    result = _integer(value, label)
    if result <= 0:
        _refuse(ImageBuildRefusalCode.INVALID_MANIFEST, f"{label} must be positive")
    return result


def _integer_list(value: object, label: str) -> tuple[int, ...]:
    if not isinstance(value, list):
        _refuse(ImageBuildRefusalCode.INVALID_MANIFEST, f"{label} must be a list")
    return tuple(_positive_integer(item, f"{label}[]") for item in value)


def _align_up(value: int, alignment: int) -> int:
    return ((value + alignment - 1) // alignment) * alignment


def _align_down(value: int, alignment: int) -> int:
    return value // alignment * alignment


def _geometry_refusal(message: str) -> NoReturn:
    _refuse(ImageBuildRefusalCode.RAW_GEOMETRY_MISMATCH, message)


def _refuse(code: ImageBuildRefusalCode, message: str) -> NoReturn:
    raise ImageBuildRefused(code, message)
