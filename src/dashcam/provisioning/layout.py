"""Pure partition-layout models and read-only verification.

This module deliberately does not discover devices or run commands.  Callers must
provide an observation collected by a separately reviewed, read-only probe.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from itertools import pairwise
from pathlib import PurePosixPath
from typing import Final, cast

MAX_LAYOUT_BYTES: Final = 64 * 1024
MAX_PARTITIONS: Final = 16
MAX_MOUNT_POINTS: Final = 16
GIB: Final = 1024**3
MIB: Final = 1024**2

_DEVICE_RE = re.compile(r"/dev/[a-zA-Z0-9._/-]{1,120}")
_TOKEN_RE = re.compile(r"[A-Za-z0-9._:+-]{1,128}")
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")
_MBR_DISK_ID_RE = re.compile(r"0x[0-9a-f]{8}")
_MBR_PARTITION_TYPE_RE = re.compile(r"0x[0-9a-f]{2}")
_MBR_PARTUUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{2}")
_FILESYSTEMS = frozenset({"vfat", "fat32", "ext4", "exfat"})


class LayoutError(ValueError):
    """Raised when a layout or observation is malformed."""


class LayoutState(StrEnum):
    SOURCE_READY = "source_ready"
    ALREADY_PROVISIONED = "already_provisioned"
    REFUSED = "refused"


class RefusalCode(StrEnum):
    UNRESOLVED_DEVICE = "unresolved_device"
    SYSTEM_DISK = "system_disk"
    ROOT_DISK = "root_disk"
    UNDERSIZED_MEDIA = "undersized_media"
    WRONG_TABLE_TYPE = "wrong_table_type"
    WRONG_SECTOR_SIZE = "wrong_sector_size"
    AMBIGUOUS_LAYOUT = "ambiguous_layout"
    UNEXPECTED_PARTITION = "unexpected_partition"
    UNEXPECTED_MOUNT = "unexpected_mount"
    EXISTING_FILESYSTEM_OR_DATA = "existing_filesystem_or_data"
    ROOT_ALREADY_TOO_LARGE = "root_already_too_large"
    PARTITION_OVERLAP = "partition_overlap"
    MISALIGNED_PARTITION = "misaligned_partition"
    INSUFFICIENT_DATA_SPACE = "insufficient_data_space"
    PARTITION_IDENTITY_MISMATCH = "partition_identity_mismatch"
    INVALID_PROVISIONING_MARKER = "invalid_provisioning_marker"


@dataclass(frozen=True, slots=True)
class PartitionPolicy:
    number: int
    role: str
    filesystem: str
    label: str
    preserve: bool
    source_start_sector: int | None
    source_size_sectors: int | None
    partition_type: str
    bootable: bool
    filesystem_uuid: str | None


@dataclass(frozen=True, slots=True)
class LayoutSpec:
    schema_version: int
    table_type: str
    sector_size_bytes: int
    alignment_mib: int
    minimum_device_gib: float
    root_target_gib: float
    minimum_data_gib: float
    trailing_reserve_mib: int
    boot: PartitionPolicy
    root: PartitionPolicy
    data: PartitionPolicy
    state_marker_path: str
    volume_sentinel_name: str

    @property
    def alignment_sectors(self) -> int:
        return self.alignment_mib * MIB // self.sector_size_bytes

    @property
    def root_target_sectors(self) -> int:
        return math.ceil(self.root_target_gib * GIB / self.sector_size_bytes)

    @property
    def minimum_device_bytes(self) -> int:
        return math.ceil(self.minimum_device_gib * GIB)

    @property
    def minimum_data_sectors(self) -> int:
        return math.ceil(self.minimum_data_gib * GIB / self.sector_size_bytes)

    @property
    def trailing_reserve_sectors(self) -> int:
        return math.ceil(self.trailing_reserve_mib * MIB / self.sector_size_bytes)


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    resolved_path: str
    serial: str
    size_bytes: int
    partition_table_fingerprint: str

    def __post_init__(self) -> None:
        _validate_device_path(self.resolved_path)
        _validate_token(self.serial, "serial")
        if _FINGERPRINT_RE.fullmatch(self.partition_table_fingerprint) is None:
            raise LayoutError("partition-table fingerprint must be lowercase SHA-256")
        if self.size_bytes <= 0:
            raise LayoutError("device size must be positive")


@dataclass(frozen=True, slots=True)
class PartitionObservation:
    number: int
    start_sector: int
    end_sector: int
    filesystem: str | None
    label: str | None
    uuid: str | None
    mount_points: tuple[str, ...] = ()
    has_data_signature: bool = False
    partition_type: str | None = None
    bootable: bool | None = None
    partuuid: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.number, bool) or not 1 <= self.number <= MAX_PARTITIONS:
            raise LayoutError(f"partition number must be in 1..{MAX_PARTITIONS}")
        if self.start_sector < 0 or self.end_sector < self.start_sector:
            raise LayoutError("partition sector bounds are invalid")
        if self.filesystem is not None:
            _validate_token(self.filesystem, "filesystem")
        if self.label is not None:
            _validate_token(self.label, "filesystem label")
        if self.uuid is not None:
            _validate_token(self.uuid, "filesystem UUID")
        if (
            self.partition_type is not None
            and _MBR_PARTITION_TYPE_RE.fullmatch(self.partition_type) is None
        ):
            raise LayoutError("MBR partition type must be canonical 0xNN lowercase hex")
        if self.bootable is not None and not isinstance(self.bootable, bool):
            raise LayoutError("partition bootable flag must be boolean")
        if self.partuuid is not None and _MBR_PARTUUID_RE.fullmatch(self.partuuid) is None:
            raise LayoutError("MBR PARTUUID must be lowercase DDDDDDDD-NN")
        if len(self.mount_points) > MAX_MOUNT_POINTS:
            raise LayoutError("too many mount points in observation")
        for mount_point in self.mount_points:
            _validate_absolute_path(mount_point, "mount point")

    @property
    def size_sectors(self) -> int:
        return self.end_sector - self.start_sector + 1


@dataclass(frozen=True, slots=True)
class DeviceObservation:
    identity: DeviceIdentity
    device_path_is_resolved: bool
    table_type: str
    mbr_disk_id: str | None
    sector_size_bytes: int
    total_sectors: int
    is_system_disk: bool
    is_root_disk: bool
    unpartitioned_data_signatures: tuple[str, ...]
    partitions: tuple[PartitionObservation, ...]
    state_marker_layout_version: int | None = None
    state_marker_serial: str | None = None
    state_marker_uuid: str | None = None
    state_marker_source_table_fingerprint: str | None = None
    volume_sentinel_layout_version: int | None = None
    volume_sentinel_serial: str | None = None
    volume_sentinel_uuid: str | None = None
    volume_sentinel_source_table_fingerprint: str | None = None

    def __post_init__(self) -> None:
        _validate_token(self.table_type, "partition-table type")
        if self.mbr_disk_id is not None and _MBR_DISK_ID_RE.fullmatch(self.mbr_disk_id) is None:
            raise LayoutError("MBR disk ID must be canonical 0xDDDDDDDD lowercase hex")
        if self.sector_size_bytes <= 0 or self.total_sectors <= 0:
            raise LayoutError("sector size and count must be positive")
        if self.identity.size_bytes != self.sector_size_bytes * self.total_sectors:
            raise LayoutError("identity size does not match observed sector geometry")
        if len(self.partitions) > MAX_PARTITIONS:
            raise LayoutError("too many partitions in observation")
        if len(self.unpartitioned_data_signatures) > MAX_PARTITIONS:
            raise LayoutError("too many unpartitioned signatures in observation")
        for signature in self.unpartitioned_data_signatures:
            _validate_token(signature, "unpartitioned data signature")
        for value, description in (
            (self.state_marker_serial, "state-marker serial"),
            (self.state_marker_uuid, "state-marker UUID"),
            (self.volume_sentinel_serial, "volume-sentinel serial"),
            (self.volume_sentinel_uuid, "volume-sentinel UUID"),
        ):
            if value is not None:
                _validate_token(value, description)
        for fingerprint in (
            self.state_marker_source_table_fingerprint,
            self.volume_sentinel_source_table_fingerprint,
        ):
            if fingerprint is not None and _FINGERPRINT_RE.fullmatch(fingerprint) is None:
                raise LayoutError("marker source-table fingerprint must be lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class ComputedLayout:
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
class VerificationReport:
    state: LayoutState
    identity: DeviceIdentity
    computed: ComputedLayout | None
    refusal_codes: tuple[RefusalCode, ...]
    messages: tuple[str, ...]

    @property
    def accepted(self) -> bool:
        return self.state is not LayoutState.REFUSED

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "accepted": self.accepted,
            "identity": asdict(self.identity),
            "computed": None if self.computed is None else asdict(self.computed),
            "refusal_codes": [code.value for code in self.refusal_codes],
            "messages": list(self.messages),
        }


def load_layout_toml(payload: bytes) -> LayoutSpec:
    """Parse a bounded, closed version-1 layout document."""

    if len(payload) > MAX_LAYOUT_BYTES:
        raise LayoutError(f"layout exceeds {MAX_LAYOUT_BYTES} bytes")
    try:
        raw = tomllib.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise LayoutError("layout must be valid UTF-8 TOML") from exc
    root = _closed_mapping(
        raw,
        {
            "schema_version",
            "table_type",
            "sector_size_bytes",
            "alignment_mib",
            "minimum_device_gib",
            "root_target_gib",
            "minimum_data_gib",
            "trailing_reserve_mib",
            "marker",
            "boot",
            "root",
            "data",
        },
        "layout",
    )
    marker = _closed_mapping(root["marker"], {"state_path", "volume_sentinel_name"}, "marker")
    spec = LayoutSpec(
        schema_version=_integer(root["schema_version"], "schema_version"),
        table_type=_text(root["table_type"], "table_type"),
        sector_size_bytes=_integer(root["sector_size_bytes"], "sector_size_bytes"),
        alignment_mib=_integer(root["alignment_mib"], "alignment_mib"),
        minimum_device_gib=_number(root["minimum_device_gib"], "minimum_device_gib"),
        root_target_gib=_number(root["root_target_gib"], "root_target_gib"),
        minimum_data_gib=_number(root["minimum_data_gib"], "minimum_data_gib"),
        trailing_reserve_mib=_integer(root["trailing_reserve_mib"], "trailing_reserve_mib"),
        boot=_partition_policy(root["boot"], "boot"),
        root=_partition_policy(root["root"], "root"),
        data=_partition_policy(root["data"], "data"),
        state_marker_path=_text(marker["state_path"], "marker.state_path"),
        volume_sentinel_name=_text(marker["volume_sentinel_name"], "marker.volume_sentinel_name"),
    )
    _validate_spec(spec)
    return spec


def observation_from_mapping(raw: Mapping[str, object]) -> DeviceObservation:
    """Build a bounded observation from a decoded machine-readable probe."""

    table = _closed_mapping(
        raw,
        {
            "identity",
            "device_path_is_resolved",
            "table_type",
            "mbr_disk_id",
            "sector_size_bytes",
            "total_sectors",
            "is_system_disk",
            "is_root_disk",
            "unpartitioned_data_signatures",
            "partitions",
            "state_marker_layout_version",
            "state_marker_serial",
            "state_marker_uuid",
            "state_marker_source_table_fingerprint",
            "volume_sentinel_layout_version",
            "volume_sentinel_serial",
            "volume_sentinel_uuid",
            "volume_sentinel_source_table_fingerprint",
        },
        "observation",
    )
    identity_raw = _closed_mapping(
        table["identity"],
        {"resolved_path", "serial", "size_bytes", "partition_table_fingerprint"},
        "identity",
    )
    partition_values = table["partitions"]
    if not isinstance(partition_values, list) or len(partition_values) > MAX_PARTITIONS:
        raise LayoutError("partitions must be a bounded list")
    partitions: list[PartitionObservation] = []
    for index, value in enumerate(partition_values):
        part = _closed_mapping(
            value,
            {
                "number",
                "start_sector",
                "end_sector",
                "filesystem",
                "label",
                "uuid",
                "mount_points",
                "has_data_signature",
                "partition_type",
                "bootable",
                "partuuid",
            },
            f"partitions[{index}]",
        )
        mounts = part["mount_points"]
        if not isinstance(mounts, list) or not all(isinstance(item, str) for item in mounts):
            raise LayoutError(f"partitions[{index}].mount_points must be a string list")
        partitions.append(
            PartitionObservation(
                number=_integer(part["number"], f"partitions[{index}].number"),
                start_sector=_integer(part["start_sector"], f"partitions[{index}].start_sector"),
                end_sector=_integer(part["end_sector"], f"partitions[{index}].end_sector"),
                filesystem=_optional_text(part["filesystem"], f"partitions[{index}].filesystem"),
                label=_optional_text(part["label"], f"partitions[{index}].label"),
                uuid=_optional_text(part["uuid"], f"partitions[{index}].uuid"),
                mount_points=tuple(cast(list[str], mounts)),
                has_data_signature=_boolean(
                    part["has_data_signature"], f"partitions[{index}].has_data_signature"
                ),
                partition_type=_optional_text(
                    part["partition_type"], f"partitions[{index}].partition_type"
                ),
                bootable=_optional_boolean(part["bootable"], f"partitions[{index}].bootable"),
                partuuid=_optional_text(part["partuuid"], f"partitions[{index}].partuuid"),
            )
        )
    signatures = table["unpartitioned_data_signatures"]
    if not isinstance(signatures, list) or not all(isinstance(item, str) for item in signatures):
        raise LayoutError("unpartitioned_data_signatures must be a string list")
    return DeviceObservation(
        identity=DeviceIdentity(
            resolved_path=_text(identity_raw["resolved_path"], "identity.resolved_path"),
            serial=_text(identity_raw["serial"], "identity.serial"),
            size_bytes=_integer(identity_raw["size_bytes"], "identity.size_bytes"),
            partition_table_fingerprint=_text(
                identity_raw["partition_table_fingerprint"],
                "identity.partition_table_fingerprint",
            ),
        ),
        device_path_is_resolved=_boolean(
            table["device_path_is_resolved"], "device_path_is_resolved"
        ),
        table_type=_text(table["table_type"], "table_type"),
        mbr_disk_id=_optional_text(table["mbr_disk_id"], "mbr_disk_id"),
        sector_size_bytes=_integer(table["sector_size_bytes"], "sector_size_bytes"),
        total_sectors=_integer(table["total_sectors"], "total_sectors"),
        is_system_disk=_boolean(table["is_system_disk"], "is_system_disk"),
        is_root_disk=_boolean(table["is_root_disk"], "is_root_disk"),
        unpartitioned_data_signatures=tuple(cast(list[str], signatures)),
        partitions=tuple(partitions),
        state_marker_layout_version=_optional_integer(
            table["state_marker_layout_version"], "state_marker_layout_version"
        ),
        state_marker_serial=_optional_text(table["state_marker_serial"], "state_marker_serial"),
        state_marker_uuid=_optional_text(table["state_marker_uuid"], "state_marker_uuid"),
        state_marker_source_table_fingerprint=_optional_text(
            table["state_marker_source_table_fingerprint"],
            "state_marker_source_table_fingerprint",
        ),
        volume_sentinel_layout_version=_optional_integer(
            table["volume_sentinel_layout_version"], "volume_sentinel_layout_version"
        ),
        volume_sentinel_serial=_optional_text(
            table["volume_sentinel_serial"], "volume_sentinel_serial"
        ),
        volume_sentinel_uuid=_optional_text(table["volume_sentinel_uuid"], "volume_sentinel_uuid"),
        volume_sentinel_source_table_fingerprint=_optional_text(
            table["volume_sentinel_source_table_fingerprint"],
            "volume_sentinel_source_table_fingerprint",
        ),
    )


def verify_layout(spec: LayoutSpec, observed: DeviceObservation) -> VerificationReport:
    """Verify an observation without accessing or changing the device."""

    failures: list[tuple[RefusalCode, str]] = []
    if not observed.device_path_is_resolved:
        failures.append((RefusalCode.UNRESOLVED_DEVICE, "device path is not fully resolved"))
    if observed.is_system_disk:
        failures.append((RefusalCode.SYSTEM_DISK, "live system disks are refused"))
    if observed.is_root_disk:
        failures.append((RefusalCode.ROOT_DISK, "the live root disk is refused"))
    if observed.identity.size_bytes < spec.minimum_device_bytes:
        failures.append(
            (RefusalCode.UNDERSIZED_MEDIA, "device is below the declared minimum capacity")
        )
    if observed.table_type != spec.table_type:
        failures.append(
            (RefusalCode.WRONG_TABLE_TYPE, f"expected {spec.table_type} partition table")
        )
    if observed.mbr_disk_id is None:
        failures.append(
            (
                RefusalCode.PARTITION_IDENTITY_MISMATCH,
                "a valid observed MBR disk ID is required",
            )
        )
    if observed.sector_size_bytes != spec.sector_size_bytes:
        failures.append((RefusalCode.WRONG_SECTOR_SIZE, "sector size differs from layout contract"))
    if observed.mbr_disk_id is not None:
        observed_fingerprint = fingerprint_partition_table(
            table_type=observed.table_type,
            mbr_disk_id=observed.mbr_disk_id,
            sector_size_bytes=observed.sector_size_bytes,
            partitions=observed.partitions,
        )
        if observed.identity.partition_table_fingerprint != observed_fingerprint:
            failures.append(
                (
                    RefusalCode.PARTITION_IDENTITY_MISMATCH,
                    "partition-table fingerprint does not match the full observation",
                )
            )
    if observed.unpartitioned_data_signatures:
        failures.append(
            (
                RefusalCode.EXISTING_FILESYSTEM_OR_DATA,
                "unpartitioned space contains recognized data",
            )
        )
    failures.extend(_structural_failures(spec, observed))
    computed = None
    try:
        computed = compute_layout(spec, observed)
    except LayoutError as exc:
        failures.append((RefusalCode.INSUFFICIENT_DATA_SPACE, str(exc)))

    if failures:
        return _refused(observed.identity, computed, failures)

    by_number = {partition.number: partition for partition in observed.partitions}
    data = by_number.get(spec.data.number)
    if data is None:
        return VerificationReport(
            state=LayoutState.SOURCE_READY,
            identity=observed.identity,
            computed=computed,
            refusal_codes=(),
            messages=("source image layout is safe to plan; no changes were made",),
        )

    provisioned_failures = _provisioned_failures(spec, observed, data, computed)
    if provisioned_failures:
        return _refused(observed.identity, computed, provisioned_failures)
    return VerificationReport(
        state=LayoutState.ALREADY_PROVISIONED,
        identity=observed.identity,
        computed=computed,
        refusal_codes=(),
        messages=("layout and both idempotency identities match; no changes are required",),
    )


def compute_layout(spec: LayoutSpec, observed: DeviceObservation) -> ComputedLayout:
    """Compute aligned target bounds while preserving boot and root starts."""

    by_number = {partition.number: partition for partition in observed.partitions}
    root = by_number.get(spec.root.number)
    if root is None:
        raise LayoutError("required image root partition is missing")
    alignment = spec.alignment_sectors
    root_end = root.start_sector + spec.root_target_sectors - 1
    data_start = _align_up(root_end + 1, alignment)
    last_usable = observed.total_sectors - spec.trailing_reserve_sectors - 1
    data_end = _align_down(last_usable + 1, alignment) - 1
    if data_end < data_start:
        raise LayoutError("no aligned recording partition fits")
    if data_end - data_start + 1 < spec.minimum_data_sectors:
        raise LayoutError("remaining recording partition is below the configured minimum")
    return ComputedLayout(
        root_start_sector=root.start_sector,
        root_end_sector=root_end,
        data_start_sector=data_start,
        data_end_sector=data_end,
    )


def fingerprint_partition_table(
    *,
    table_type: str,
    mbr_disk_id: str,
    sector_size_bytes: int,
    partitions: Sequence[PartitionObservation],
) -> str:
    """Return a deterministic observation fingerprint, never a device identifier alone."""

    canonical = {
        "table_type": table_type,
        "mbr_disk_id": mbr_disk_id,
        "sector_size_bytes": sector_size_bytes,
        "partitions": [
            {
                "number": partition.number,
                "start_sector": partition.start_sector,
                "end_sector": partition.end_sector,
                "filesystem": partition.filesystem,
                "label": partition.label,
                "uuid": partition.uuid,
                "partition_type": partition.partition_type,
                "bootable": partition.bootable,
                "partuuid": partition.partuuid,
            }
            for partition in sorted(partitions, key=lambda item: item.number)
        ],
    }
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def mbr_partuuid(mbr_disk_id: str, partition_number: int) -> str:
    """Derive Linux's DOS/MBR PARTUUID from an observed disk ID and partition number."""

    if _MBR_DISK_ID_RE.fullmatch(mbr_disk_id) is None:
        raise LayoutError("MBR disk ID must be canonical 0xDDDDDDDD lowercase hex")
    if isinstance(partition_number, bool) or not 1 <= partition_number <= MAX_PARTITIONS:
        raise LayoutError(f"partition number must be in 1..{MAX_PARTITIONS}")
    return f"{mbr_disk_id[2:]}-{partition_number:02x}"


def _structural_failures(
    spec: LayoutSpec, observed: DeviceObservation
) -> list[tuple[RefusalCode, str]]:
    failures: list[tuple[RefusalCode, str]] = []
    numbers = [partition.number for partition in observed.partitions]
    if len(set(numbers)) != len(numbers):
        failures.append((RefusalCode.AMBIGUOUS_LAYOUT, "partition numbers are duplicated"))
        return failures
    sorted_parts = sorted(observed.partitions, key=lambda item: item.start_sector)
    if any(partition.end_sector >= observed.total_sectors for partition in sorted_parts):
        failures.append(
            (RefusalCode.AMBIGUOUS_LAYOUT, "a partition extends beyond the observed device")
        )
    for previous, current in pairwise(sorted_parts):
        if current.start_sector <= previous.end_sector:
            failures.append((RefusalCode.PARTITION_OVERLAP, "partition bounds overlap"))
            break
    allowed = {spec.boot.number, spec.root.number, spec.data.number}
    if any(number not in allowed for number in numbers):
        failures.append((RefusalCode.UNEXPECTED_PARTITION, "an unknown partition is present"))
    by_number = {partition.number: partition for partition in observed.partitions}
    boot = by_number.get(spec.boot.number)
    root = by_number.get(spec.root.number)
    if boot is None or root is None:
        failures.append(
            (RefusalCode.AMBIGUOUS_LAYOUT, "boot and root partitions must both be present")
        )
        return failures
    data = by_number.get(spec.data.number)
    if data is not None and (
        data.filesystem != spec.data.filesystem
        or data.label != spec.data.label
        or data.uuid is None
        or data.has_data_signature
    ):
        failures.append(
            (
                RefusalCode.EXISTING_FILESYSTEM_OR_DATA,
                "partition 3 exists with a foreign or conflicting data identity",
            )
        )
    if boot.filesystem not in {"vfat", "fat32"} or root.filesystem != spec.root.filesystem:
        failures.append(
            (
                RefusalCode.EXISTING_FILESYSTEM_OR_DATA,
                "boot/root filesystems do not match the supported source image",
            )
        )
    policies = {
        spec.boot.number: spec.boot,
        spec.root.number: spec.root,
        spec.data.number: spec.data,
    }
    for partition in observed.partitions:
        policy = policies.get(partition.number)
        if policy is None:
            continue
        expected_uuid = policy.filesystem_uuid
        expected_partuuid = (
            None
            if observed.mbr_disk_id is None
            else mbr_partuuid(observed.mbr_disk_id, partition.number)
        )
        identity_matches = (
            (
                policy.source_start_sector is None
                or partition.start_sector == policy.source_start_sector
            )
            and partition.partition_type == policy.partition_type
            and partition.bootable is policy.bootable
            and partition.partuuid == expected_partuuid
            and (expected_uuid is None or partition.uuid == expected_uuid)
        )
        if policy.source_size_sectors is not None and (data is None or policy.preserve):
            identity_matches = (
                identity_matches and partition.size_sectors == policy.source_size_sectors
            )
        if not identity_matches:
            failures.append(
                (
                    RefusalCode.PARTITION_IDENTITY_MISMATCH,
                    f"partition {partition.number} identity differs from the selected image",
                )
            )
    if boot.label != spec.boot.label or root.label != spec.root.label:
        failures.append(
            (
                RefusalCode.PARTITION_IDENTITY_MISMATCH,
                "boot/root labels differ from the selected image",
            )
        )
    if boot.has_data_signature or root.has_data_signature:
        # Expected filesystems contain data by definition; the flag denotes an
        # additional/conflicting signature reported by the probe.
        failures.append(
            (
                RefusalCode.EXISTING_FILESYSTEM_OR_DATA,
                "boot/root contains a conflicting data signature",
            )
        )
    allowed_mounts = {
        spec.boot.number: {"/boot", "/boot/firmware"},
        spec.root.number: {"/"},
        spec.data.number: {"/srv/dashcam"},
    }
    for partition in observed.partitions:
        expected = allowed_mounts.get(partition.number, set())
        if any(mount not in expected for mount in partition.mount_points):
            failures.append(
                (
                    RefusalCode.UNEXPECTED_MOUNT,
                    f"partition {partition.number} has an unexpected mount point",
                )
            )
    if spec.data.number not in by_number and any(
        partition.mount_points for partition in observed.partitions
    ):
        failures.append(
            (
                RefusalCode.UNEXPECTED_MOUNT,
                "source-image partitions must be unmounted before a plan is authored",
            )
        )
    if root.size_sectors > spec.root_target_sectors:
        failures.append(
            (
                RefusalCode.ROOT_ALREADY_TOO_LARGE,
                "root partition exceeds target; shrinking is never planned",
            )
        )
    alignment = spec.alignment_sectors
    if root.start_sector % alignment:
        failures.append((RefusalCode.MISALIGNED_PARTITION, "root partition start is not aligned"))
    return failures


def _provisioned_failures(
    spec: LayoutSpec,
    observed: DeviceObservation,
    data: PartitionObservation,
    computed: ComputedLayout | None,
) -> list[tuple[RefusalCode, str]]:
    failures: list[tuple[RefusalCode, str]] = []
    if computed is None:
        return [(RefusalCode.AMBIGUOUS_LAYOUT, "target layout could not be computed")]
    root = next(part for part in observed.partitions if part.number == spec.root.number)
    exact_bounds = (
        root.start_sector == computed.root_start_sector
        and root.end_sector == computed.root_end_sector
        and data.start_sector == computed.data_start_sector
        and data.end_sector == computed.data_end_sector
    )
    if not exact_bounds:
        failures.append(
            (RefusalCode.AMBIGUOUS_LAYOUT, "existing provisioned bounds do not match layout")
        )
    if (
        data.filesystem != spec.data.filesystem
        or data.label != spec.data.label
        or data.uuid is None
    ):
        failures.append(
            (
                RefusalCode.EXISTING_FILESYSTEM_OR_DATA,
                "partition 3 exists but is not the expected identified DASHCAM filesystem",
            )
        )
    if data.has_data_signature:
        failures.append(
            (
                RefusalCode.EXISTING_FILESYSTEM_OR_DATA,
                "recording partition has a conflicting data signature",
            )
        )
    if (
        observed.state_marker_layout_version != spec.schema_version
        or observed.state_marker_serial != observed.identity.serial
        or observed.state_marker_uuid != data.uuid
        or observed.volume_sentinel_layout_version != spec.schema_version
        or observed.volume_sentinel_serial != observed.identity.serial
        or observed.volume_sentinel_uuid != data.uuid
        or observed.state_marker_source_table_fingerprint is None
        or observed.state_marker_source_table_fingerprint
        != observed.volume_sentinel_source_table_fingerprint
    ):
        failures.append(
            (
                RefusalCode.INVALID_PROVISIONING_MARKER,
                "ext4 marker and exFAT sentinel do not bind the expected layout/device/UUID",
            )
        )
    return failures


def _refused(
    identity: DeviceIdentity,
    computed: ComputedLayout | None,
    failures: Sequence[tuple[RefusalCode, str]],
) -> VerificationReport:
    unique: dict[RefusalCode, str] = {}
    for code, message in failures:
        unique.setdefault(code, message)
    return VerificationReport(
        state=LayoutState.REFUSED,
        identity=identity,
        computed=computed,
        refusal_codes=tuple(unique),
        messages=tuple(unique.values()),
    )


def _validate_spec(spec: LayoutSpec) -> None:
    if spec.schema_version != 1:
        raise LayoutError("only layout schema version 1 is supported")
    if spec.table_type != "dos":
        raise LayoutError("version 1 requires the selected image's DOS/MBR table")
    if spec.sector_size_bytes not in {512, 4096}:
        raise LayoutError("sector_size_bytes must be 512 or 4096")
    if not 1 <= spec.alignment_mib <= 64:
        raise LayoutError("alignment_mib must be in 1..64")
    if spec.alignment_mib * MIB % spec.sector_size_bytes:
        raise LayoutError("alignment must contain a whole number of sectors")
    if not 8 <= spec.minimum_device_gib <= 1024:
        raise LayoutError("minimum_device_gib must be in 8..1024")
    if not 4 <= spec.root_target_gib <= 64:
        raise LayoutError("root_target_gib must be in 4..64")
    if not 1 <= spec.minimum_data_gib <= 1024:
        raise LayoutError("minimum_data_gib must be in 1..1024")
    if not 1 <= spec.trailing_reserve_mib <= 64:
        raise LayoutError("trailing_reserve_mib must be in 1..64")
    if {spec.boot.number, spec.root.number, spec.data.number} != {1, 2, 3}:
        raise LayoutError("version 1 requires partition numbers 1, 2, and 3")
    if not spec.boot.preserve or spec.root.preserve or spec.data.preserve:
        raise LayoutError("only the image boot partition may be preserved")
    if spec.boot.filesystem not in {"vfat", "fat32"}:
        raise LayoutError("boot filesystem must be vfat/fat32")
    if spec.root.filesystem != "ext4" or spec.data.filesystem != "exfat":
        raise LayoutError("root/data filesystems must be ext4/exfat")
    if spec.data.label != "DASHCAM":
        raise LayoutError("recording partition label must be DASHCAM")
    if spec.boot.source_start_sector is None or spec.root.source_start_sector is None:
        raise LayoutError("boot/root source starts must be explicit")
    if spec.boot.source_size_sectors is None or spec.root.source_size_sectors is None:
        raise LayoutError("boot/root source sizes must be explicit")
    if spec.data.source_start_sector is not None:
        raise LayoutError("recording partition start is computed, not a source identity")
    for policy in (spec.boot, spec.root, spec.data):
        if policy.source_start_sector is not None and policy.source_start_sector < 0:
            raise LayoutError("partition source starts must be non-negative")
        if (
            policy.source_start_sector is not None
            and policy.source_start_sector % spec.alignment_sectors
        ):
            raise LayoutError("partition source starts must honor alignment_mib")
        if policy.source_size_sectors is not None and policy.source_size_sectors <= 0:
            raise LayoutError("partition source sizes must be positive")
        if _MBR_PARTITION_TYPE_RE.fullmatch(policy.partition_type) is None:
            raise LayoutError("partition types must be canonical 0xNN lowercase hex")
        if policy.filesystem_uuid is not None:
            _validate_token(policy.filesystem_uuid, "preserved filesystem UUID")
    _validate_absolute_path(spec.state_marker_path, "marker.state_path")
    if PurePosixPath(spec.state_marker_path).parts[:3] != ("/", "var", "lib"):
        raise LayoutError("state marker must live below /var/lib")
    if "/" in spec.volume_sentinel_name or spec.volume_sentinel_name != ".dashcam-volume":
        raise LayoutError("volume sentinel name must be .dashcam-volume")


def _partition_policy(value: object, path: str) -> PartitionPolicy:
    required = {
        "number",
        "role",
        "filesystem",
        "label",
        "preserve",
        "partition_type",
        "bootable",
    }
    raw = _closed_mapping_with_optional(
        value,
        required,
        {"source_start_sector", "source_size_sectors", "filesystem_uuid"},
        path,
    )
    filesystem = _text(raw["filesystem"], f"{path}.filesystem")
    if filesystem not in _FILESYSTEMS:
        raise LayoutError(f"{path}.filesystem is unsupported")
    role = _text(raw["role"], f"{path}.role")
    label = _text(raw["label"], f"{path}.label")
    _validate_token(role, f"{path}.role")
    _validate_token(label, f"{path}.label")
    return PartitionPolicy(
        number=_integer(raw["number"], f"{path}.number"),
        role=role,
        filesystem=filesystem,
        label=label,
        preserve=_boolean(raw["preserve"], f"{path}.preserve"),
        source_start_sector=(
            None
            if "source_start_sector" not in raw
            else _integer(raw["source_start_sector"], f"{path}.source_start_sector")
        ),
        source_size_sectors=(
            None
            if "source_size_sectors" not in raw
            else _integer(raw["source_size_sectors"], f"{path}.source_size_sectors")
        ),
        partition_type=_text(raw["partition_type"], f"{path}.partition_type"),
        bootable=_boolean(raw["bootable"], f"{path}.bootable"),
        filesystem_uuid=(
            None
            if "filesystem_uuid" not in raw
            else _text(raw["filesystem_uuid"], f"{path}.filesystem_uuid")
        ),
    )


def _closed_mapping(value: object, keys: set[str], path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise LayoutError(f"{path} must be a string-keyed table")
    result = cast(Mapping[str, object], value)
    unknown = sorted(set(result) - keys)
    missing = sorted(keys - set(result))
    if unknown:
        raise LayoutError(f"{path} has unknown key: {unknown[0]}")
    if missing:
        raise LayoutError(f"{path} is missing key: {missing[0]}")
    return result


def _closed_mapping_with_optional(
    value: object,
    required_keys: set[str],
    optional_keys: set[str],
    path: str,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise LayoutError(f"{path} must be a string-keyed table")
    result = cast(Mapping[str, object], value)
    unknown = sorted(set(result) - required_keys - optional_keys)
    missing = sorted(required_keys - set(result))
    if unknown:
        raise LayoutError(f"{path} has unknown key: {unknown[0]}")
    if missing:
        raise LayoutError(f"{path} is missing key: {missing[0]}")
    return result


def _integer(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LayoutError(f"{path} must be an integer")
    return value


def _optional_integer(value: object, path: str) -> int | None:
    return None if value is None else _integer(value, path)


def _number(value: object, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise LayoutError(f"{path} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise LayoutError(f"{path} must be finite")
    return result


def _boolean(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise LayoutError(f"{path} must be a boolean")
    return value


def _optional_boolean(value: object, path: str) -> bool | None:
    return None if value is None else _boolean(value, path)


def _text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise LayoutError(f"{path} must be a non-empty bounded string")
    return value


def _optional_text(value: object, path: str) -> str | None:
    return None if value is None else _text(value, path)


def _validate_token(value: str, description: str) -> None:
    if _TOKEN_RE.fullmatch(value) is None:
        raise LayoutError(f"{description} is malformed or unbounded")


def _validate_device_path(path: str) -> None:
    if _DEVICE_RE.fullmatch(path) is None or ".." in PurePosixPath(path).parts:
        raise LayoutError("resolved device path must be a bounded absolute /dev path")


def _validate_absolute_path(path: str, description: str) -> None:
    parsed = PurePosixPath(path)
    if len(path) > 256 or not path.startswith("/") or ".." in parsed.parts or "\x00" in path:
        raise LayoutError(f"{description} must be a bounded absolute path")


def _align_up(value: int, alignment: int) -> int:
    return ((value + alignment - 1) // alignment) * alignment


def _align_down(value: int, alignment: int) -> int:
    return value // alignment * alignment


__all__ = [
    "MAX_LAYOUT_BYTES",
    "ComputedLayout",
    "DeviceIdentity",
    "DeviceObservation",
    "LayoutError",
    "LayoutSpec",
    "LayoutState",
    "PartitionObservation",
    "PartitionPolicy",
    "RefusalCode",
    "VerificationReport",
    "compute_layout",
    "fingerprint_partition_table",
    "load_layout_toml",
    "mbr_partuuid",
    "observation_from_mapping",
    "verify_layout",
]
