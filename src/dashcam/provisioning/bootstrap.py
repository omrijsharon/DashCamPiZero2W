"""DashCam Bootstrap v1 post-root storage transaction.

The planner in this module is pure.  The small POSIX runtime is deliberately
closed over a short command allow-list and is only entered by ``main`` on
Linux.  Stage A and Stage B are separate systemd invocations on separate
boots; neither asks the kernel to reread a partition table.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Final, Protocol, cast

TRIGGER: Final = "dashcam.bootstrap=v1"
SSH_DEV_TRIGGER: Final = "dashcam.bootstrap=ssh-dev-v1"
_SUPPORTED_TRIGGERS: Final = frozenset({TRIGGER, SSH_DEV_TRIGGER})
STATE_PATH: Final = "/var/lib/dashcam/provisioning/bootstrap-v1.json"
COMPLETE_PATH: Final = "/var/lib/dashcam/provisioning/layout-v1.complete.json"
BOOT_MIRROR: Final = "/boot/firmware/dashcam-bootstrap"
MOUNT_POINT: Final = "/srv/dashcam"
SENTINEL_PATH: Final = f"{MOUNT_POINT}/.dashcam-volume"
ENV_PATH: Final = "/etc/dashcam/storage-volume.env"
GROUP_PATH: Final = "/etc/group"
FSTAB_PATH: Final = "/etc/fstab"
CONTRACT_PATH: Final = "/etc/dashcam/bootstrap-v1-authorization.json"
AUTHORIZED_CID: Final = "fe34325344000000200000031a0192d1"
AUTHORIZED_SIZE_BYTES: Final = 31_457_280_000
MIB: Final = 1024**2
GIB: Final = 1024**3
MBR_SIZE: Final = 512
MAX_STATE_BYTES: Final = 64 * 1024
DATA_ZERO_PREFIX_BYTES: Final = 4 * MIB
KNOWN_CLOUD_INIT_WARNING: Final = (
    "Could not find module named cc_netplan_nm_patch "
    "(searched ['cc_netplan_nm_patch', 'cloudinit.config.cc_netplan_nm_patch'])"
)

_SFDISK: Final = "/usr/sbin/sfdisk"
_RESIZE2FS: Final = "/usr/sbin/resize2fs"
_DUMPE2FS: Final = "/usr/sbin/dumpe2fs"
_MKFS_EXFAT: Final = "/usr/sbin/mkfs.exfat"
_BLKID: Final = "/usr/sbin/blkid"
_MOUNT: Final = "/usr/bin/mount"
_ID: Final = "/usr/bin/id"
_SYSTEMCTL: Final = "/usr/bin/systemctl"
_SYNC: Final = "/usr/bin/sync"
_FINDMNT: Final = "/usr/bin/findmnt"
_LSBLK: Final = "/usr/bin/lsblk"
_WIPEFS: Final = "/usr/sbin/wipefs"
_ALLOWED = frozenset(
    {
        _SFDISK,
        _RESIZE2FS,
        _DUMPE2FS,
        _MKFS_EXFAT,
        _BLKID,
        _MOUNT,
        _ID,
        _SYSTEMCTL,
        _SYNC,
        _FINDMNT,
        _LSBLK,
        _WIPEFS,
    }
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_CID_RE = re.compile(r"[0-9a-f]{32}")
_UUID_RE = re.compile(r"[A-Za-z0-9-]{3,64}")
_DEVICE_RE = re.compile(r"/dev/[A-Za-z0-9._/+:-]{1,120}")
_SYSFS_COMPONENT_RE = re.compile(r"[A-Za-z0-9._:+@-]{1,255}")


class BootstrapError(RuntimeError):
    """Malformed evidence, an unsafe runtime, or an execution failure."""


class Refusal(BootstrapError):
    """A stable fail-closed provisioning outcome."""

    def __init__(self, code: RefusalCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class RefusalCode(StrEnum):
    TRIGGER_MISSING = "trigger_missing"
    FIRST_RUN_ACTIVE = "first_run_active"
    IDENTITY_MISMATCH = "identity_mismatch"
    SOURCE_LAYOUT_MISMATCH = "source_layout_mismatch"
    TARGET_LAYOUT_MISMATCH = "target_layout_mismatch"
    UNDERSIZED_MEDIA = "undersized_media"
    WRONG_BOOT = "wrong_boot"
    TORN_TABLE = "torn_table"
    FOREIGN_FILESYSTEM = "foreign_filesystem"
    FORMAT_NOT_BLANK = "format_not_blank"
    JOURNAL_CONFLICT = "journal_conflict"
    EXECUTION_FAILED = "execution_failed"
    NON_LINUX = "non_linux"


class Phase(StrEnum):
    STAGE_A_INTENT = "stage_a_intent"
    TABLE_WRITE_STARTED = "table_write_started"
    TABLE_COMMITTED = "table_committed"
    ROOT_RESIZED = "root_resized"
    FORMAT_INTENT = "format_intent"
    DATA_FORMATTED = "data_formatted"
    CONFIGURED = "configured"
    COMPLETE = "complete"
    REFUSED = "refused"


class ActionKind(StrEnum):
    BACKUP = "backup"
    WRITE_STATE = "write_state"
    COMMAND = "command"
    VERIFY_RAW_MBR = "verify_raw_mbr"
    VERIFY_FILESYSTEM = "verify_filesystem"
    CONFIGURE = "configure"
    COMPLETE = "complete"
    REBOOT = "reboot"
    DEFER = "defer"
    NOOP = "noop"
    LATCH = "latch"


@dataclass(frozen=True, slots=True)
class Partition:
    number: int
    start_sector: int
    size_sectors: int
    type_code: int
    bootable: bool = False

    @property
    def end_sector(self) -> int:
        return self.start_sector + self.size_sectors - 1


@dataclass(frozen=True, slots=True)
class Geometry:
    total_sectors: int
    sector_size: int
    root: Partition
    data: Partition


@dataclass(frozen=True, slots=True)
class CapacityPolicy:
    root_target_bytes: int = 6 * GIB
    minimum_device_bytes: int = 28 * GIB
    minimum_data_bytes: int = 8 * GIB
    alignment_bytes: int = MIB
    trailing_reserve_bytes: int = MIB


DEFAULT_CAPACITY_POLICY: Final = CapacityPolicy()


@dataclass(frozen=True, slots=True)
class Authorization:
    cid: str
    size_bytes: int
    boot_start: int = 16_384
    boot_size: int = 1_048_576
    root_start: int = 1_064_960
    root_source_size: int = 8_388_608
    sector_size: int = 512
    mbr_disk_id: int | None = None
    bootstrap_trigger: str = TRIGGER
    journal_schema_version: int = 1
    require_authored_zero_prefix: bool = True


EXACT_CARD_AUTHORIZATION: Final = Authorization(
    cid=AUTHORIZED_CID,
    size_bytes=AUTHORIZED_SIZE_BYTES,
)

EXACT_STOCK_CARD_AUTHORIZATION: Final = Authorization(
    cid=AUTHORIZED_CID,
    size_bytes=AUTHORIZED_SIZE_BYTES,
    root_source_size=4_161_536,
    bootstrap_trigger=SSH_DEV_TRIGGER,
    journal_schema_version=2,
    require_authored_zero_prefix=False,
)


@dataclass(frozen=True, slots=True)
class Evidence:
    cmdline: tuple[str, ...]
    boot_id: str
    root_partition: str
    disk: str
    cid: str
    size_bytes: int
    sector_size: int
    mbr: bytes
    partitions: tuple[Partition, ...]
    root_filesystem_bytes: int = 0
    root_filesystem: str | None = None
    root_uuid: str | None = None
    root_partuuid: str | None = None
    boot_partition: str | None = None
    boot_mounted_source: str | None = None
    boot_filesystem: str | None = None
    boot_uuid: str | None = None
    boot_partuuid: str | None = None
    data_partuuid: str | None = None
    firstrun_active: bool = False
    cloud_init_status: str = "unknown"
    data_filesystem: str | None = None
    data_label: str | None = None
    data_uuid: str | None = None
    data_signatures: tuple[str, ...] = ()
    data_zero_prefix_bytes: int = 0
    data_prefix_sha256: str | None = None
    complete_identity: Mapping[str, object] | None = None
    mounted_source: str | None = None
    mounted_filesystem: str | None = None
    mounted_uuid: str | None = None
    sentinel_identity: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class Journal:
    schema_version: int
    phase: Phase
    disk: str
    root_partition: str
    data_partition: str
    cid: str
    size_bytes: int
    stage_a_boot_id: str
    source_mbr_sha256: str
    target: Geometry
    committed_mbr_sha256: str | None = None
    data_uuid: str | None = None
    refusal_code: str | None = None
    refusal_message: str | None = None
    data_prefix_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class Action:
    kind: ActionKind
    argv: tuple[str, ...] = ()
    stdin: str | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Plan:
    actions: tuple[Action, ...]
    journal: Journal | None

    @property
    def mutating_commands(self) -> tuple[Action, ...]:
        return tuple(
            action
            for action in self.actions
            if action.kind is ActionKind.COMMAND
            and action.argv
            and action.argv[0] in {_SFDISK, _RESIZE2FS, _MKFS_EXFAT}
        )


def partition_path(disk: str, number: int) -> str:
    """Derive a partition node without assuming an mmc device name."""

    _validate_device(disk)
    if number not in {1, 2, 3}:
        raise BootstrapError("partition number is outside the Bootstrap v1 contract")
    separator = "p" if disk[-1].isdigit() else ""
    return f"{disk}{separator}{number}"


def compute_geometry(
    *, size_bytes: int, sector_size: int, root_start_sector: int, policy: CapacityPolicy
) -> Geometry:
    """Compute the fixed root and remainder data geometry for any declared capacity."""

    if sector_size not in {512, 4096} or size_bytes % sector_size:
        raise Refusal(RefusalCode.SOURCE_LAYOUT_MISMATCH, "unsupported sector geometry")
    if size_bytes < policy.minimum_device_bytes:
        raise Refusal(RefusalCode.UNDERSIZED_MEDIA, "card is below the declared minimum")
    alignment = policy.alignment_bytes // sector_size
    if alignment <= 0 or policy.alignment_bytes % sector_size:
        raise BootstrapError("alignment is not a whole number of sectors")
    root_size = policy.root_target_bytes // sector_size
    if policy.root_target_bytes % sector_size:
        raise BootstrapError("root target is not a whole number of sectors")
    root = Partition(2, root_start_sector, root_size, 0x83)
    data_start = _align_up(root.end_sector + 1, alignment)
    total_sectors = size_bytes // sector_size
    last_exclusive = _align_down(
        total_sectors - policy.trailing_reserve_bytes // sector_size, alignment
    )
    data_size = last_exclusive - data_start
    if data_size * sector_size < policy.minimum_data_bytes:
        raise Refusal(RefusalCode.UNDERSIZED_MEDIA, "recording partition would be too small")
    return Geometry(
        total_sectors=total_sectors,
        sector_size=sector_size,
        root=root,
        data=Partition(3, data_start, data_size, 0x07),
    )


def parse_mbr(mbr: bytes) -> tuple[int, tuple[Partition, ...]]:
    """Parse identity-bearing MBR fields from a raw LBA0 read."""

    if len(mbr) != MBR_SIZE or mbr[510:512] != b"\x55\xaa":
        raise Refusal(RefusalCode.TORN_TABLE, "raw LBA0 is not a valid MBR")
    disk_id = int.from_bytes(mbr[440:444], "little")
    parts: list[Partition] = []
    for index in range(4):
        entry = mbr[446 + index * 16 : 462 + index * 16]
        start = int.from_bytes(entry[8:12], "little")
        size = int.from_bytes(entry[12:16], "little")
        type_code = entry[4]
        if type_code == 0 and start == 0 and size == 0:
            continue
        if type_code == 0 or size == 0:
            raise Refusal(RefusalCode.TORN_TABLE, "raw MBR contains a partial partition entry")
        parts.append(Partition(index + 1, start, size, type_code, entry[0] == 0x80))
    return disk_id, tuple(parts)


def table_matches(partitions: Sequence[Partition], boot: Partition, geometry: Geometry) -> bool:
    expected = (boot, geometry.root, geometry.data)
    actual = tuple(sorted(partitions, key=lambda item: item.number))
    return actual == expected


def plan_stage_a(
    evidence: Evidence,
    journal: Journal | None,
    *,
    authorization: Authorization = EXACT_CARD_AUTHORIZATION,
    policy: CapacityPolicy = DEFAULT_CAPACITY_POLICY,
) -> Plan:
    """Plan Stage A, including power-cut reconciliation without a second table write."""

    if journal is not None and journal.phase is Phase.REFUSED:
        return Plan((Action(ActionKind.NOOP, detail="latched refusal"),), journal)
    if evidence.firstrun_active or any(
        token.startswith("systemd.run=") for token in evidence.cmdline
    ):
        return Plan(
            (Action(ActionKind.DEFER, detail="Raspberry Pi Imager first-run active"),),
            journal,
        )
    if not _cloud_init_ready(evidence.cloud_init_status, authorization):
        return Plan(
            (
                Action(
                    ActionKind.DEFER,
                    detail=f"cloud-init customization is {evidence.cloud_init_status}",
                ),
            ),
            journal,
        )
    trigger_error = _trigger_error(evidence.cmdline, authorization)
    if trigger_error is not None:
        return _refusal_plan(evidence, journal, RefusalCode.TRIGGER_MISSING, trigger_error)

    try:
        geometry, boot = _validate_identity_and_geometry(evidence, authorization, policy)
    except Refusal as exc:
        return _refusal_plan(evidence, journal, exc.code, str(exc))
    target_is_present = table_matches(evidence.partitions, boot, geometry)
    source_is_present = _source_matches(evidence.partitions, authorization)

    if journal is not None:
        conflict = _journal_conflict(journal, evidence, geometry, authorization)
        if conflict is not None:
            return _refusal_plan(evidence, journal, RefusalCode.JOURNAL_CONFLICT, conflict)
        if journal.phase in {Phase.STAGE_A_INTENT, Phase.TABLE_WRITE_STARTED}:
            if not target_is_present:
                code = (
                    RefusalCode.TORN_TABLE
                    if not source_is_present
                    else RefusalCode.EXECUTION_FAILED
                )
                return _refusal_plan(
                    evidence,
                    journal,
                    code,
                    "table-write intent exists but the exact target table is absent; no retry",
                )
            if authorization.journal_schema_version == 2 and (
                not _is_sha256(evidence.data_prefix_sha256)
                or evidence.data_prefix_sha256 != journal.data_prefix_sha256
            ):
                return _refusal_plan(
                    evidence,
                    journal,
                    RefusalCode.FORMAT_NOT_BLANK,
                    "post-table raw prefix hash drifted from pre-Stage-A provenance",
                )
            committed = replace(
                journal,
                phase=Phase.TABLE_COMMITTED,
                committed_mbr_sha256=_sha256(evidence.mbr),
            )
            return Plan(
                (
                    Action(ActionKind.VERIFY_RAW_MBR, detail="reconciled exact target from LBA0"),
                    Action(ActionKind.WRITE_STATE, detail="commit exact raw-MBR readback"),
                    Action(ActionKind.COMMAND, (_SYNC,), detail="durable table commit"),
                    Action(
                        ActionKind.REBOOT,
                        (_SYSTEMCTL, "reboot"),
                        detail="single controlled reboot",
                    ),
                ),
                committed,
            )
        if journal.phase is Phase.TABLE_COMMITTED:
            if not target_is_present:
                return _refusal_plan(
                    evidence, journal, RefusalCode.TORN_TABLE, "committed target table drifted"
                )
            return Plan((Action(ActionKind.NOOP, detail="Stage A already committed"),), journal)
        if journal.phase in {
            Phase.ROOT_RESIZED,
            Phase.FORMAT_INTENT,
            Phase.DATA_FORMATTED,
            Phase.CONFIGURED,
            Phase.COMPLETE,
        }:
            if not target_is_present:
                return _refusal_plan(
                    evidence, journal, RefusalCode.TORN_TABLE, "later-phase target table drifted"
                )
            if evidence.complete_identity is not None and not _completion_is_exact(
                evidence, journal
            ):
                return _refusal_plan(
                    evidence,
                    journal,
                    RefusalCode.JOURNAL_CONFLICT,
                    "completion marker is not bound to the current exact target",
                )
            return Plan(
                (Action(ActionKind.NOOP, detail="later phase owns reconciliation"),),
                journal,
            )
        return Plan((Action(ActionKind.NOOP, detail="later phase owns reconciliation"),), journal)

    if not source_is_present:
        code = (
            RefusalCode.TARGET_LAYOUT_MISMATCH
            if target_is_present
            else RefusalCode.SOURCE_LAYOUT_MISMATCH
        )
        return _refusal_plan(evidence, None, code, "neither authorized source nor journaled target")
    if authorization.journal_schema_version == 2 and not _is_sha256(evidence.data_prefix_sha256):
        return _refusal_plan(
            evidence,
            None,
            RefusalCode.FORMAT_NOT_BLANK,
            "future partition 3 lacks a bounded pre-Stage-A prefix hash",
        )
    data_partition = partition_path(evidence.disk, 3)
    intent = Journal(
        schema_version=authorization.journal_schema_version,
        phase=Phase.STAGE_A_INTENT,
        disk=evidence.disk,
        root_partition=evidence.root_partition,
        data_partition=data_partition,
        cid=evidence.cid,
        size_bytes=evidence.size_bytes,
        stage_a_boot_id=evidence.boot_id,
        source_mbr_sha256=_sha256(evidence.mbr),
        target=geometry,
        data_prefix_sha256=evidence.data_prefix_sha256,
    )
    source_disk_id, _parts = parse_mbr(evidence.mbr)
    sfdisk_input = _sfdisk_input(evidence.disk, boot, geometry, source_disk_id)
    return Plan(
        (
            Action(ActionKind.BACKUP, detail="mirror raw MBR and sfdisk dump to ext4 and FAT"),
            Action(ActionKind.WRITE_STATE, detail="durable Stage A intent"),
            Action(ActionKind.WRITE_STATE, detail="durable table-write-started boundary"),
            Action(
                ActionKind.COMMAND,
                (_SFDISK, "--no-reread", "--force", evidence.disk),
                sfdisk_input,
                "one full-table write",
            ),
            Action(ActionKind.VERIFY_RAW_MBR, detail="read raw LBA0 and compare exact target"),
            Action(ActionKind.WRITE_STATE, detail="durable committed raw-MBR identity"),
            Action(ActionKind.COMMAND, (_SYNC,), detail="sync all durable state"),
            Action(ActionKind.REBOOT, (_SYSTEMCTL, "reboot"), detail="single controlled reboot"),
        ),
        intent,
    )


def plan_stage_b(
    evidence: Evidence,
    journal: Journal | None,
    *,
    authorization: Authorization = EXACT_CARD_AUTHORIZATION,
    policy: CapacityPolicy = DEFAULT_CAPACITY_POLICY,
) -> Plan:
    """Plan Stage B with format-intent reconciliation and completion last."""

    if evidence.firstrun_active or any(
        token.startswith("systemd.run=") for token in evidence.cmdline
    ):
        return Plan(
            (Action(ActionKind.DEFER, detail="Raspberry Pi Imager first-run active"),),
            journal,
        )
    if not _cloud_init_ready(evidence.cloud_init_status, authorization):
        return Plan(
            (
                Action(
                    ActionKind.DEFER,
                    detail=f"cloud-init customization is {evidence.cloud_init_status}",
                ),
            ),
            journal,
        )
    trigger_error = _trigger_error(evidence.cmdline, authorization)
    if trigger_error is not None:
        return _refusal_plan(evidence, journal, RefusalCode.TRIGGER_MISSING, trigger_error)
    if journal is None:
        return _refusal_plan(
            evidence,
            None,
            RefusalCode.JOURNAL_CONFLICT,
            "Stage B requires Stage A state",
        )
    if journal.phase is Phase.REFUSED:
        return Plan((Action(ActionKind.NOOP, detail="latched refusal"),), journal)
    try:
        geometry, boot = _validate_identity_and_geometry(evidence, authorization, policy)
    except Refusal as exc:
        return _refusal_plan(evidence, journal, exc.code, str(exc))
    conflict = _journal_conflict(journal, evidence, geometry, authorization)
    if conflict is not None:
        return _refusal_plan(evidence, journal, RefusalCode.JOURNAL_CONFLICT, conflict)
    if evidence.boot_id == journal.stage_a_boot_id:
        return Plan(
            (Action(ActionKind.DEFER, detail="Stage B requires a different boot ID"),),
            journal,
        )
    if not table_matches(evidence.partitions, boot, geometry):
        return _refusal_plan(
            evidence, journal, RefusalCode.TORN_TABLE, "raw target table is not exact"
        )
    if evidence.complete_identity is not None:
        if _completion_is_exact(evidence, journal):
            return Plan(
                (Action(ActionKind.NOOP, detail="verified completion is a no-op"),),
                journal,
            )
        return _refusal_plan(
            evidence, journal, RefusalCode.JOURNAL_CONFLICT, "completion identities disagree"
        )
    if journal.phase is Phase.TABLE_COMMITTED:
        if evidence.data_filesystem is not None:
            return _refusal_plan(
                evidence,
                journal,
                RefusalCode.FOREIGN_FILESYSTEM,
                "partition 3 has a filesystem before format intent",
            )
        if evidence.data_signatures:
            return _refusal_plan(
                evidence,
                journal,
                RefusalCode.FORMAT_NOT_BLANK,
                "partition 3 has a wipefs signature before root resize",
            )
        if not authorization.require_authored_zero_prefix and (
            not _is_sha256(evidence.data_prefix_sha256)
            or evidence.data_prefix_sha256 != journal.data_prefix_sha256
        ):
            return _refusal_plan(
                evidence,
                journal,
                RefusalCode.FORMAT_NOT_BLANK,
                "post-Stage-A raw prefix hash drifted from stock provenance",
            )
        target_bytes = geometry.root.size_sectors * geometry.sector_size
        if evidence.root_filesystem_bytes > target_bytes:
            return _refusal_plan(
                evidence,
                journal,
                RefusalCode.TARGET_LAYOUT_MISMATCH,
                "root filesystem exceeds the exact target partition size",
            )
        actions: tuple[Action, ...]
        if evidence.root_filesystem_bytes == target_bytes:
            actions = (Action(ActionKind.WRITE_STATE, detail="exact root resize already observed"),)
        else:
            actions = (
                Action(
                    ActionKind.COMMAND,
                    (_RESIZE2FS, evidence.root_partition),
                    detail="online mounted-root growth only",
                ),
                Action(ActionKind.VERIFY_FILESYSTEM, detail="recollect exact ext4 size"),
                Action(ActionKind.WRITE_STATE, detail="root resize observed complete"),
            )
        return Plan(actions, replace(journal, phase=Phase.ROOT_RESIZED))
    if journal.phase is Phase.ROOT_RESIZED:
        if evidence.data_filesystem is not None or evidence.data_signatures:
            return _refusal_plan(
                evidence,
                journal,
                RefusalCode.FORMAT_NOT_BLANK,
                "partition 3 has a recognized filesystem or wipefs signature",
            )
        if (
            authorization.require_authored_zero_prefix
            and evidence.data_zero_prefix_bytes != DATA_ZERO_PREFIX_BYTES
        ):
            return _refusal_plan(
                evidence,
                journal,
                RefusalCode.FORMAT_NOT_BLANK,
                "partition 3 lacks the exact image-authored zero prefix",
            )
        if not authorization.require_authored_zero_prefix and (
            not _is_sha256(evidence.data_prefix_sha256)
            or evidence.data_prefix_sha256 != journal.data_prefix_sha256
        ):
            return _refusal_plan(
                evidence,
                journal,
                RefusalCode.FORMAT_NOT_BLANK,
                "bounded raw prefix hash drifted from exact pre-Stage-A stock provenance",
            )
        return Plan(
            (
                Action(ActionKind.WRITE_STATE, detail="durable exact format intent"),
                Action(
                    ActionKind.COMMAND,
                    (_MKFS_EXFAT, "-n", "DASHCAM", journal.data_partition),
                    detail="single exFAT format",
                ),
            ),
            replace(journal, phase=Phase.FORMAT_INTENT),
        )
    if journal.phase is Phase.FORMAT_INTENT:
        if (
            evidence.data_filesystem == "exfat"
            and evidence.data_label == "DASHCAM"
            and evidence.data_uuid is not None
            and _has_exact_exfat_wipefs_signatures(evidence.data_signatures)
        ):
            return Plan(
                (Action(ActionKind.WRITE_STATE, detail="capture intended exFAT UUID"),),
                replace(journal, phase=Phase.DATA_FORMATTED, data_uuid=evidence.data_uuid),
            )
        return _refusal_plan(
            evidence,
            journal,
            RefusalCode.FOREIGN_FILESYSTEM,
            "format intent does not reconcile to exact exFAT DASHCAM; never reformat",
        )
    if journal.phase is Phase.DATA_FORMATTED:
        if (
            evidence.data_filesystem != "exfat"
            or evidence.data_label != "DASHCAM"
            or evidence.data_uuid != journal.data_uuid
            or not _has_exact_exfat_wipefs_signatures(evidence.data_signatures)
        ):
            return _refusal_plan(
                evidence, journal, RefusalCode.FOREIGN_FILESYSTEM, "formatted identity drifted"
            )
        if evidence.mounted_uuid is not None and evidence.mounted_uuid != journal.data_uuid:
            return _refusal_plan(
                evidence,
                journal,
                RefusalCode.JOURNAL_CONFLICT,
                "another filesystem is mounted at the recording path",
            )
        return Plan(
            (
                Action(
                    ActionKind.CONFIGURE,
                    detail="UUID fstab, mount, sentinel, directories, env",
                ),
                Action(ActionKind.WRITE_STATE, detail="configuration durable"),
            ),
            replace(journal, phase=Phase.CONFIGURED),
        )
    if journal.phase is Phase.CONFIGURED:
        if not _storage_mount_is_exact(evidence, journal) or not _sentinel_is_exact(
            evidence, journal
        ):
            return _refusal_plan(
                evidence, journal, RefusalCode.JOURNAL_CONFLICT, "mount/sentinel not exact"
            )
        return Plan(
            (Action(ActionKind.COMPLETE, detail="write completion marker last"),),
            replace(journal, phase=Phase.COMPLETE),
        )
    if journal.phase is Phase.COMPLETE:
        return _refusal_plan(
            evidence,
            journal,
            RefusalCode.JOURNAL_CONFLICT,
            "journal claims complete without the exact completion marker",
        )
    return _refusal_plan(
        evidence, journal, RefusalCode.JOURNAL_CONFLICT, "Stage B received an invalid phase"
    )


class RuntimeIO(Protocol):
    def read_bytes(self, path: str, *, limit: int | None = None) -> bytes: ...
    def read_text(self, path: str, *, limit: int = MAX_STATE_BYTES) -> str: ...
    def exists(self, path: str) -> bool: ...
    def atomic_write(self, path: str, data: bytes, mode: int = 0o600) -> None: ...
    def mkdir(self, path: str, mode: int = 0o750) -> None: ...
    def set_owner(self, path: str, uid: int, gid: int, mode: int) -> None: ...
    def run(self, argv: tuple[str, ...], *, stdin: str | None = None, timeout: int = 30) -> str: ...
    def sync(self) -> None: ...


class PosixRuntime:
    """Minimal real runtime; construction itself is side-effect free."""

    def read_bytes(self, path: str, *, limit: int | None = None) -> bytes:
        descriptor = self._open_readonly(path, allow_block=True)
        with os.fdopen(descriptor, "rb", buffering=0) as stream:
            return stream.read() if limit is None else stream.read(limit)

    def sha256_region(self, path: str, *, offset: int, length: int) -> str:
        """Hash one exact bounded raw region without retaining it in memory."""

        if (
            isinstance(offset, bool)
            or isinstance(length, bool)
            or not isinstance(offset, int)
            or not isinstance(length, int)
            or offset < 0
            or length != DATA_ZERO_PREFIX_BYTES
            or offset > 2**63 - 1 - length
        ):
            raise BootstrapError("raw prefix hash range is outside its exact bound")
        descriptor = self._open_readonly(path, allow_block=True)
        digest = hashlib.sha256()
        remaining = length
        try:
            if os.lseek(descriptor, offset, os.SEEK_SET) != offset:
                raise BootstrapError("raw prefix hash seek did not reach the exact offset")
            while remaining:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    raise BootstrapError("raw prefix hash read was short")
                digest.update(chunk)
                remaining -= len(chunk)
        finally:
            os.close(descriptor)
        return digest.hexdigest()

    def read_text(self, path: str, *, limit: int = MAX_STATE_BYTES) -> str:
        descriptor = self._open_readonly(path, allow_block=False)
        with os.fdopen(descriptor, "rb", buffering=0) as stream:
            result = stream.read(limit + 1)
        if len(result) > limit:
            raise BootstrapError(f"{path} exceeds the read bound")
        return result.decode("utf-8")

    def exists(self, path: str) -> bool:
        _validate_absolute_path(path)
        if not os.path.lexists(path):
            return False
        metadata = os.lstat(path)
        if stat.S_ISLNK(metadata.st_mode):
            raise BootstrapError(f"refusing symbolic-link state path: {path}")
        return True

    def atomic_write(self, path: str, data: bytes, mode: int = 0o600) -> None:
        if len(data) > 1024 * 1024:
            raise BootstrapError("atomic write payload exceeds its bound")
        _validate_absolute_path(path)
        target = Path(path)
        self.mkdir(str(target.parent), 0o750)
        _assert_existing_path_safe(str(target.parent), require_directory=True)
        if os.path.lexists(target):
            metadata = os.lstat(target)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise BootstrapError(f"refusing unsafe atomic-write target: {target}")
        temporary = target.with_name(f".{target.name}.tmp-{secrets.token_hex(8)}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, mode)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, target)
            directory = os.open(
                target.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.lexists(temporary):
                os.unlink(temporary)

    def mkdir(self, path: str, mode: int = 0o750) -> None:
        _validate_absolute_path(path)
        current = "/"
        for component in PurePosixPath(path).parts[1:]:
            current = str(PurePosixPath(current) / component)
            if os.path.lexists(current):
                _assert_existing_path_safe(current, require_directory=True)
                continue
            parent = str(PurePosixPath(current).parent)
            _assert_existing_path_safe(parent, require_directory=True)
            os.mkdir(current, mode)
            descriptor = os.open(
                parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def set_owner(self, path: str, uid: int, gid: int, mode: int) -> None:
        if (
            path != ENV_PATH
            or isinstance(uid, bool)
            or isinstance(gid, bool)
            or not 0 <= uid <= 2**31 - 1
            or not 0 <= gid <= 2**31 - 1
            or mode != 0o640
        ):
            raise BootstrapError("ownership operation is outside the storage handoff contract")
        _validate_absolute_path(path)
        _assert_safe_parent_chain(path)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise BootstrapError("storage handoff is not one regular file")
            fchown = cast(
                object,
                getattr(os, "fchown"),  # noqa: B009 - absent from Windows typeshed
            )
            cast(Callable[[int, int, int], None], fchown)(descriptor, uid, gid)
            fchmod = cast(
                object,
                getattr(os, "fchmod"),  # noqa: B009 - absent from Windows typeshed
            )
            cast(Callable[[int, int], None], fchmod)(descriptor, mode)
            os.fsync(descriptor)
            verified = os.fstat(descriptor)
            if (
                verified.st_uid != uid
                or verified.st_gid != gid
                or stat.S_IMODE(verified.st_mode) != mode
            ):
                raise BootstrapError("storage handoff ownership verification failed")
        finally:
            os.close(descriptor)

    def run(self, argv: tuple[str, ...], *, stdin: str | None = None, timeout: int = 30) -> str:
        if not argv or argv[0] not in _ALLOWED:
            raise BootstrapError("runtime command is outside the closed allow-list")
        completed = subprocess.run(
            argv,
            input=stdin,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.strip()[:1000]
            raise BootstrapError(f"{argv[0]} failed ({completed.returncode}): {stderr}")
        if len(completed.stdout.encode()) > 256 * 1024:
            raise BootstrapError("runtime command output exceeded its bound")
        return completed.stdout

    def sync(self) -> None:
        sync = getattr(os, "sync", None)
        if sync is None:
            raise BootstrapError("POSIX sync is unavailable")
        sync()

    def _open_readonly(self, path: str, *, allow_block: bool) -> int:
        _validate_absolute_path(path)
        _assert_safe_parent_chain(path)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) and not (
            allow_block and stat.S_ISBLK(metadata.st_mode)
        ):
            os.close(descriptor)
            raise BootstrapError(f"refusing unsafe read target: {path}")
        return descriptor


def journal_json(journal: Journal) -> bytes:
    raw = asdict(journal)
    raw["phase"] = journal.phase.value
    return (json.dumps(raw, sort_keys=True, separators=(",", ":")) + "\n").encode()


def journal_from_json(payload: str) -> Journal:
    if len(payload.encode()) > MAX_STATE_BYTES:
        raise BootstrapError("journal is oversized")
    raw_value = json.loads(payload)
    if not isinstance(raw_value, Mapping):
        raise BootstrapError("journal must be an object")
    raw = cast(Mapping[str, object], raw_value)
    legacy_required = {
        "schema_version",
        "phase",
        "disk",
        "root_partition",
        "data_partition",
        "cid",
        "size_bytes",
        "stage_a_boot_id",
        "source_mbr_sha256",
        "target",
        "committed_mbr_sha256",
        "data_uuid",
        "refusal_code",
        "refusal_message",
    }
    current_required = legacy_required | {"data_prefix_sha256"}
    if frozenset(raw) not in {frozenset(legacy_required), frozenset(current_required)}:
        raise BootstrapError("journal keys are not the closed v1 schema")
    target_raw = raw["target"]
    if not isinstance(target_raw, Mapping):
        raise BootstrapError("journal target is malformed")
    geometry = _geometry_from_mapping(cast(Mapping[str, object], target_raw))
    journal = Journal(
        schema_version=_int(raw["schema_version"], "schema_version"),
        phase=Phase(_str(raw["phase"], "phase")),
        disk=_str(raw["disk"], "disk"),
        root_partition=_str(raw["root_partition"], "root_partition"),
        data_partition=_str(raw["data_partition"], "data_partition"),
        cid=_str(raw["cid"], "cid"),
        size_bytes=_int(raw["size_bytes"], "size_bytes"),
        stage_a_boot_id=_str(raw["stage_a_boot_id"], "stage_a_boot_id"),
        source_mbr_sha256=_str(raw["source_mbr_sha256"], "source_mbr_sha256"),
        target=geometry,
        committed_mbr_sha256=_optional_str(raw["committed_mbr_sha256"]),
        data_uuid=_optional_str(raw["data_uuid"]),
        refusal_code=_optional_str(raw["refusal_code"]),
        refusal_message=_optional_str(raw["refusal_message"]),
        data_prefix_sha256=(
            _optional_str(raw["data_prefix_sha256"]) if "data_prefix_sha256" in raw else None
        ),
    )
    _validate_journal(journal)
    return journal


def execute_stage_a(
    evidence: Evidence,
    journal: Journal | None,
    runtime: RuntimeIO,
    *,
    authorization: Authorization = EXACT_CARD_AUTHORIZATION,
    policy: CapacityPolicy = DEFAULT_CAPACITY_POLICY,
) -> Plan:
    """Execute one Stage A invocation with durable pre-command boundaries."""

    plan = plan_stage_a(evidence, journal, authorization=authorization, policy=policy)
    if plan.journal is None:
        return plan
    if (
        journal is not None
        and journal.phase in {Phase.STAGE_A_INTENT, Phase.TABLE_WRITE_STARTED}
        and plan.journal.phase is Phase.TABLE_COMMITTED
    ):
        runtime.atomic_write(STATE_PATH, journal_json(plan.journal))
        runtime.sync()
        runtime.run((_SYSTEMCTL, "reboot"), timeout=15)
        return plan
    if not plan.mutating_commands:
        if plan.journal is not None and plan.journal.phase is Phase.REFUSED:
            runtime.atomic_write(STATE_PATH, journal_json(plan.journal))
        return plan
    intent = plan.journal
    state_dir = str(PurePosixPath(STATE_PATH).parent)
    runtime.mkdir(state_dir)
    _assert_live_boot_mount(runtime, evidence)
    runtime.mkdir(BOOT_MIRROR)
    sfdisk_dump = runtime.run((_SFDISK, "--dump", evidence.disk), timeout=15)
    for base in (state_dir, BOOT_MIRROR):
        runtime.atomic_write(f"{base}/bootstrap-v1-mbr-lba0.bin", evidence.mbr)
        runtime.atomic_write(f"{base}/bootstrap-v1-source-table.sfdisk", sfdisk_dump.encode())
        runtime.atomic_write(
            f"{base}/bootstrap-v1-backup.sha256",
            (
                f"{_sha256(evidence.mbr)}  bootstrap-v1-mbr-lba0.bin\n"
                f"{_sha256(sfdisk_dump.encode())}  bootstrap-v1-source-table.sfdisk\n"
            ).encode(),
        )
    runtime.atomic_write(STATE_PATH, journal_json(intent))
    started = replace(intent, phase=Phase.TABLE_WRITE_STARTED)
    runtime.atomic_write(STATE_PATH, journal_json(started))
    committed: Journal | None = None
    try:
        write = next(
            action
            for action in plan.actions
            if action.kind is ActionKind.COMMAND and action.argv[:1] == (_SFDISK,)
        )
        runtime.run(write.argv, stdin=write.stdin, timeout=30)
        raw = runtime.read_bytes(evidence.disk, limit=MBR_SIZE)
        disk_id, actual = parse_mbr(raw)
        expected_disk_id, _ = parse_mbr(evidence.mbr)
        boot = _boot_partition(evidence.partitions)
        if disk_id != expected_disk_id or not table_matches(actual, boot, intent.target):
            raise Refusal(RefusalCode.TORN_TABLE, "raw LBA0 readback is not the exact target")
        committed = replace(started, phase=Phase.TABLE_COMMITTED, committed_mbr_sha256=_sha256(raw))
        runtime.atomic_write(STATE_PATH, journal_json(committed))
        runtime.sync()
    except Exception as exc:
        if committed is not None:
            # Once exact LBA0 readback and TABLE_COMMITTED are durable, never
            # replace that recoverable boundary with a destructive-refusal
            # latch merely because sync/reboot orchestration failed.
            raise
        refused = _latch(started, _code_for_exception(exc), str(exc))
        runtime.atomic_write(STATE_PATH, journal_json(refused))
        runtime.sync()
        raise
    runtime.run((_SYSTEMCTL, "reboot"), timeout=15)
    return replace(plan, journal=committed)


def execute_stage_b(
    evidence: Evidence,
    journal: Journal,
    runtime: RuntimeIO,
    *,
    authorization: Authorization = EXACT_CARD_AUTHORIZATION,
    policy: CapacityPolicy = DEFAULT_CAPACITY_POLICY,
) -> Plan:
    """Execute exactly one bounded Stage B transition."""

    current_evidence = evidence
    current_journal = journal
    last_plan: Plan | None = None
    try:
        for _transition in range(6):
            plan = plan_stage_b(
                current_evidence,
                current_journal,
                authorization=authorization,
                policy=policy,
            )
            last_plan = plan
            next_journal = plan.journal
            if next_journal is None:
                raise BootstrapError("Stage B planner lost its journal")
            if next_journal.phase is Phase.REFUSED:
                runtime.atomic_write(STATE_PATH, journal_json(next_journal))
                runtime.sync()
                return plan
            if all(action.kind in {ActionKind.NOOP, ActionKind.DEFER} for action in plan.actions):
                return plan
            for action in plan.actions:
                if action.kind is ActionKind.COMMAND:
                    runtime.run(action.argv, stdin=action.stdin, timeout=120)
                    if action.argv[:1] == (_RESIZE2FS,):
                        observed_size = _observe_root_filesystem_size(
                            runtime, current_evidence.root_partition
                        )
                        expected_size = (
                            current_journal.target.root.size_sectors
                            * current_journal.target.sector_size
                        )
                        if observed_size != expected_size:
                            raise Refusal(
                                RefusalCode.TARGET_LAYOUT_MISMATCH,
                                "resize2fs returned but exact ext4 target size was not observed",
                            )
                        current_evidence = replace(
                            current_evidence, root_filesystem_bytes=observed_size
                        )
                    elif action.argv[:1] == (_MKFS_EXFAT,):
                        fields = _parse_blkid_export(
                            runtime.run((_BLKID, "-o", "export", current_journal.data_partition))
                        )
                        signatures = _parse_wipefs_json(
                            runtime.run(
                                (_WIPEFS, "--json", current_journal.data_partition),
                                timeout=15,
                            )
                        )
                        current_evidence = replace(
                            current_evidence,
                            data_filesystem=fields.get("TYPE"),
                            data_label=fields.get("LABEL"),
                            data_uuid=fields.get("UUID"),
                            data_signatures=signatures,
                            data_zero_prefix_bytes=0,
                        )
                elif action.kind is ActionKind.CONFIGURE:
                    _configure_volume(
                        runtime,
                        next_journal,
                        current_evidence,
                        policy=policy,
                        already_mounted=current_evidence.mounted_uuid == next_journal.data_uuid,
                    )
                    mounted_source, mounted_filesystem, mounted_uuid = _observe_mount(
                        runtime, MOUNT_POINT
                    )
                    sentinel = _read_optional_json(runtime, SENTINEL_PATH)
                    current_evidence = replace(
                        current_evidence,
                        mounted_source=mounted_source,
                        mounted_filesystem=mounted_filesystem,
                        mounted_uuid=mounted_uuid,
                        sentinel_identity=sentinel,
                    )
                elif action.kind is ActionKind.COMPLETE:
                    # This is deliberately the final durable write.  The
                    # journal may remain CONFIGURED; the independently verified
                    # completion marker makes every later boot a no-op.
                    _write_completion(runtime, next_journal, current_evidence)
                    runtime.sync()
                    return plan
                elif action.kind is ActionKind.WRITE_STATE:
                    runtime.atomic_write(STATE_PATH, journal_json(next_journal))
                    runtime.sync()
            current_journal = next_journal
    except Exception as exc:
        refused = _latch(current_journal, _code_for_exception(exc), str(exc))
        runtime.atomic_write(STATE_PATH, journal_json(refused))
        runtime.sync()
        raise
    raise BootstrapError(f"Stage B exceeded its transition bound: {last_plan!r}")


def main(argv: Sequence[str] | None = None) -> int:
    """Systemd entry point.  It refuses before any write outside Linux/root."""

    parser = argparse.ArgumentParser(prog="python3 -m dashcam.provisioning.bootstrap")
    parser.add_argument("--stage", required=True, choices=("a", "b"))
    parser.add_argument("--contract", default=CONTRACT_PATH)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if sys.platform != "linux":
        print("DashCam Bootstrap v1 refuses to run outside Linux", file=sys.stderr)
        return 2
    if os.geteuid() != 0:
        print("DashCam Bootstrap v1 requires root", file=sys.stderr)
        return 2
    runtime = PosixRuntime()
    try:
        authorization, policy = load_bootstrap_contract(runtime.read_text(args.contract))
        journal = (
            journal_from_json(runtime.read_text(STATE_PATH)) if runtime.exists(STATE_PATH) else None
        )
        evidence = collect_evidence(
            runtime,
            journal,
            authorization=authorization,
            policy=policy,
        )
        if args.dry_run:
            result = (
                plan_stage_a(
                    evidence,
                    journal,
                    authorization=authorization,
                    policy=policy,
                )
                if args.stage == "a"
                else plan_stage_b(
                    evidence,
                    journal,
                    authorization=authorization,
                    policy=policy,
                )
            )
            ready = _dry_run_ready(stage=args.stage, plan=result)
            print(
                _dry_run_json(
                    stage=args.stage,
                    authorization=authorization,
                    evidence=evidence,
                    plan=result,
                    ready=ready,
                )
            )
            return 0 if ready else 3
        if args.stage == "a":
            result = execute_stage_a(
                evidence,
                journal,
                runtime,
                authorization=authorization,
                policy=policy,
            )
        else:
            if journal is None:
                if (
                    evidence.firstrun_active
                    or any(token.startswith("systemd.run=") for token in evidence.cmdline)
                    or not _cloud_init_ready(evidence.cloud_init_status, authorization)
                ):
                    return 0
                raise Refusal(RefusalCode.JOURNAL_CONFLICT, "Stage B requires Stage A state")
            result = execute_stage_b(
                evidence,
                journal,
                runtime,
                authorization=authorization,
                policy=policy,
            )
        if result.journal is not None and result.journal.phase is Phase.REFUSED:
            print(
                f"bootstrap refused [{result.journal.refusal_code}]: "
                f"{result.journal.refusal_message}",
                file=sys.stderr,
            )
            return 3
    except Refusal as exc:
        print(f"bootstrap refused [{exc.code}]: {exc}", file=sys.stderr)
        return 3
    except (BootstrapError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"bootstrap failed closed: {exc}", file=sys.stderr)
        return 4
    return 0


def _dry_run_json(
    *,
    stage: str,
    authorization: Authorization,
    evidence: Evidence,
    plan: Plan,
    ready: bool,
) -> str:
    journal = plan.journal
    refused = journal is not None and journal.phase is Phase.REFUSED
    deferred = any(action.kind is ActionKind.DEFER for action in plan.actions)
    latched = any(action.kind is ActionKind.LATCH for action in plan.actions)
    outcome = (
        "refused"
        if refused or latched
        else "deferred"
        if deferred
        else "ready"
        if ready
        else "not_ready"
    )
    target = (
        {
            "total_sectors": journal.target.total_sectors,
            "sector_size": journal.target.sector_size,
            "root": asdict(journal.target.root),
            "data": asdict(journal.target.data),
        }
        if journal is not None
        else None
    )
    report = {
        "schema_version": 1,
        "dry_run": True,
        "ready": ready,
        "outcome": outcome,
        "stage": stage,
        "authorization": {
            "bootstrap_trigger": authorization.bootstrap_trigger,
            "journal_schema_version": authorization.journal_schema_version,
            "cid": authorization.cid,
            "size_bytes": authorization.size_bytes,
            "sector_size": authorization.sector_size,
            "source": {
                "boot_start_sector": authorization.boot_start,
                "boot_size_sectors": authorization.boot_size,
                "root_start_sector": authorization.root_start,
                "root_size_sectors": authorization.root_source_size,
            },
        },
        "evidence": {
            "boot_id": evidence.boot_id,
            "cloud_init_status": evidence.cloud_init_status,
            "root_partition": evidence.root_partition,
            "disk": evidence.disk,
            "cid": evidence.cid,
            "size_bytes": evidence.size_bytes,
            "sector_size": evidence.sector_size,
            "mbr_sha256": _sha256(evidence.mbr),
            "data_prefix_sha256": evidence.data_prefix_sha256,
            "partitions": [asdict(partition) for partition in evidence.partitions],
        },
        "actions": [
            {
                "kind": action.kind.value,
                "argv": list(action.argv),
                "has_stdin": action.stdin is not None,
                "detail": action.detail,
            }
            for action in plan.actions
        ],
        "journal": (
            {
                "schema_version": journal.schema_version,
                "phase": journal.phase.value,
                "source_mbr_sha256": journal.source_mbr_sha256,
                "data_prefix_sha256": journal.data_prefix_sha256,
                "committed_mbr_sha256": journal.committed_mbr_sha256,
                "refusal_code": journal.refusal_code,
                "refusal_message": journal.refusal_message,
                "target": target,
            }
            if journal is not None
            else None
        ),
        "refusal": (
            {
                "code": journal.refusal_code,
                "message": journal.refusal_message,
            }
            if refused and journal is not None
            else None
        ),
    }
    payload = json.dumps(report, sort_keys=True, separators=(",", ":"))
    if len(payload.encode()) > MAX_STATE_BYTES:
        raise BootstrapError("dry-run report exceeded its output bound")
    return payload


def _dry_run_ready(*, stage: str, plan: Plan) -> bool:
    if stage not in {"a", "b"}:
        raise BootstrapError("dry-run stage is invalid")
    if plan.journal is None:
        return False
    if plan.journal.phase is Phase.REFUSED:
        return False
    if any(action.kind in {ActionKind.DEFER, ActionKind.LATCH} for action in plan.actions):
        return False
    return bool(plan.actions)


def collect_evidence(
    runtime: RuntimeIO,
    journal: Journal | None,
    *,
    authorization: Authorization = EXACT_CARD_AUTHORIZATION,
    policy: CapacityPolicy = DEFAULT_CAPACITY_POLICY,
) -> Evidence:
    """Collect bounded live evidence while deriving root/disk from the mounted root."""

    cmdline = tuple(runtime.read_text("/proc/cmdline", limit=16 * 1024).split())
    boot_id = runtime.read_text("/proc/sys/kernel/random/boot_id", limit=128).strip()
    root_mount = _findmnt_fields("/")
    if root_mount is None:
        raise BootstrapError("mounted root is absent")
    root_partition = os.path.realpath(root_mount["source"])
    # findmnt is discovery-only and intentionally outside RuntimeIO's mutation allow-list.
    # PosixRuntime handles it through this explicitly bounded helper.
    if not isinstance(runtime, PosixRuntime):
        raise BootstrapError("live evidence collection requires the POSIX runtime")
    lsblk = _run_discovery(
        (
            "/usr/bin/lsblk",
            "-J",
            "-b",
            "-o",
            "PATH,TYPE,PKNAME,SIZE,LOG-SEC,PARTN,FSTYPE,LABEL,UUID,PARTUUID,START",
        )
    )
    disk, size_bytes, sector_size, nodes = _derive_disk(lsblk, root_partition)
    disk_name = PurePosixPath(disk).name
    cid_path = _canonical_sysfs_cid_path(disk_name)
    cid = runtime.read_text(cid_path, limit=128).strip().lower()
    mbr = runtime.read_bytes(disk, limit=MBR_SIZE)
    _disk_id, partitions = parse_mbr(mbr)
    data_node = partition_path(disk, 3)
    boot_node = partition_path(disk, 1)
    boot_mount = _findmnt_fields("/boot/firmware")
    if boot_mount is None:
        raise BootstrapError("/boot/firmware is not mounted")
    root_node = nodes[root_partition]
    boot_lsblk = nodes.get(boot_node)
    data_lsblk = nodes.get(data_node)
    if boot_lsblk is None:
        raise BootstrapError("boot partition is absent from lsblk")
    fs_fields = _blkid_fields(data_node) if any(part.number == 3 for part in partitions) else {}
    signatures = (
        _wipefs_signatures(data_node) if any(part.number == 3 for part in partitions) else ()
    )
    zero_prefix_bytes = (
        _zero_prefix_bytes(runtime, data_node, DATA_ZERO_PREFIX_BYTES)
        if (
            authorization.require_authored_zero_prefix
            and any(part.number == 3 for part in partitions)
        )
        else 0
    )
    prefix_sha256: str | None = None
    if authorization.journal_schema_version == 2:
        geometry = compute_geometry(
            size_bytes=size_bytes,
            sector_size=sector_size,
            root_start_sector=authorization.root_start,
            policy=policy,
        )
        prefix_offset = geometry.data.start_sector * sector_size
        if prefix_offset < 0 or prefix_offset + DATA_ZERO_PREFIX_BYTES > size_bytes:
            raise BootstrapError("future partition 3 prefix is outside the exact disk")
        prefix_sha256 = runtime.sha256_region(
            disk,
            offset=prefix_offset,
            length=DATA_ZERO_PREFIX_BYTES,
        )
    complete = _read_optional_json(runtime, COMPLETE_PATH)
    sentinel = _read_optional_json(runtime, SENTINEL_PATH)
    mounted = _findmnt_fields(MOUNT_POINT, allow_absent=True)
    return Evidence(
        cmdline=cmdline,
        boot_id=boot_id,
        root_partition=root_partition,
        disk=disk,
        cid=cid,
        size_bytes=size_bytes,
        sector_size=sector_size,
        mbr=mbr,
        partitions=partitions,
        root_filesystem_bytes=_observe_root_filesystem_size(runtime, root_partition),
        root_filesystem=_optional_mapping_str(root_node, "fstype"),
        root_uuid=_optional_mapping_str(root_node, "uuid"),
        root_partuuid=_optional_mapping_str(root_node, "partuuid"),
        boot_partition=boot_node,
        boot_mounted_source=os.path.realpath(boot_mount["source"]),
        boot_filesystem=boot_mount["fstype"],
        boot_uuid=boot_mount["uuid"],
        boot_partuuid=_optional_mapping_str(boot_lsblk, "partuuid"),
        data_partuuid=(
            _optional_mapping_str(data_lsblk, "partuuid") if data_lsblk is not None else None
        ),
        firstrun_active=_firstrun_active(cmdline),
        cloud_init_status=_cloud_init_status(),
        data_filesystem=fs_fields.get("TYPE"),
        data_label=fs_fields.get("LABEL"),
        data_uuid=fs_fields.get("UUID"),
        data_signatures=signatures,
        data_zero_prefix_bytes=zero_prefix_bytes,
        data_prefix_sha256=prefix_sha256,
        complete_identity=complete,
        mounted_source=(os.path.realpath(mounted["source"]) if mounted is not None else None),
        mounted_filesystem=mounted["fstype"] if mounted is not None else None,
        mounted_uuid=mounted["uuid"] if mounted is not None else None,
        sentinel_identity=sentinel,
    )


def load_bootstrap_contract(payload: str) -> tuple[Authorization, CapacityPolicy]:
    """Load one closed, checked exact-card destructive authorization."""

    if len(payload.encode()) > MAX_STATE_BYTES:
        raise BootstrapError("Bootstrap contract is oversized")
    value = json.loads(payload)
    if not isinstance(value, Mapping):
        raise BootstrapError("Bootstrap contract must be an object")
    raw = cast(Mapping[str, object], value)
    required = {
        "schema_version",
        "bootstrap_trigger",
        "cid",
        "size_bytes",
        "sector_size",
        "source",
        "target",
    }
    if set(raw) != required:
        raise BootstrapError("Bootstrap contract keys are not the closed v1 schema")
    if _int(raw["schema_version"], "schema_version") != 1:
        raise BootstrapError("unsupported Bootstrap contract version")
    trigger = _str(raw["bootstrap_trigger"], "bootstrap_trigger")
    if trigger == TRIGGER:
        expected_authorization = EXACT_CARD_AUTHORIZATION
    elif trigger == SSH_DEV_TRIGGER:
        expected_authorization = EXACT_STOCK_CARD_AUTHORIZATION
    else:
        raise BootstrapError("Bootstrap contract trigger does not match a reviewed runtime")
    source_value = raw["source"]
    target_value = raw["target"]
    if not isinstance(source_value, Mapping) or not isinstance(target_value, Mapping):
        raise BootstrapError("Bootstrap contract source/target must be objects")
    source = cast(Mapping[str, object], source_value)
    target = cast(Mapping[str, object], target_value)
    source_keys = {
        "boot_start_sector",
        "boot_size_sectors",
        "root_start_sector",
        "root_size_sectors",
    }
    target_keys = {
        "root_size_bytes",
        "minimum_device_bytes",
        "minimum_data_bytes",
        "alignment_bytes",
        "trailing_reserve_bytes",
    }
    if set(source) != source_keys or set(target) != target_keys:
        raise BootstrapError("Bootstrap contract geometry keys are not closed")
    authorization = Authorization(
        cid=_str(raw["cid"], "cid"),
        size_bytes=_int(raw["size_bytes"], "size_bytes"),
        boot_start=_int(source["boot_start_sector"], "boot_start_sector"),
        boot_size=_int(source["boot_size_sectors"], "boot_size_sectors"),
        root_start=_int(source["root_start_sector"], "root_start_sector"),
        root_source_size=_int(source["root_size_sectors"], "root_size_sectors"),
        sector_size=_int(raw["sector_size"], "sector_size"),
        bootstrap_trigger=expected_authorization.bootstrap_trigger,
        journal_schema_version=expected_authorization.journal_schema_version,
        require_authored_zero_prefix=(expected_authorization.require_authored_zero_prefix),
    )
    policy = CapacityPolicy(
        root_target_bytes=_int(target["root_size_bytes"], "root_size_bytes"),
        minimum_device_bytes=_int(target["minimum_device_bytes"], "minimum_device_bytes"),
        minimum_data_bytes=_int(target["minimum_data_bytes"], "minimum_data_bytes"),
        alignment_bytes=_int(target["alignment_bytes"], "alignment_bytes"),
        trailing_reserve_bytes=_int(target["trailing_reserve_bytes"], "trailing_reserve_bytes"),
    )
    if authorization != expected_authorization:
        raise BootstrapError(
            "checked contract is not the authorized exact trial card "
            "or reviewed stock source layout"
        )
    if policy != DEFAULT_CAPACITY_POLICY:
        raise BootstrapError("checked contract geometry differs from Bootstrap v1")
    return authorization, policy


def _validate_identity_and_geometry(
    evidence: Evidence, authorization: Authorization, policy: CapacityPolicy
) -> tuple[Geometry, Partition]:
    _validate_evidence(evidence)
    if (
        evidence.cid != authorization.cid
        or evidence.size_bytes != authorization.size_bytes
        or evidence.sector_size != authorization.sector_size
    ):
        raise Refusal(RefusalCode.IDENTITY_MISMATCH, "CID/size/sector identity is not authorized")
    disk_id, raw_parts = parse_mbr(evidence.mbr)
    if tuple(evidence.partitions) != raw_parts:
        raise Refusal(RefusalCode.TORN_TABLE, "collected partitions disagree with raw LBA0")
    if authorization.mbr_disk_id is not None and disk_id != authorization.mbr_disk_id:
        raise Refusal(RefusalCode.IDENTITY_MISMATCH, "MBR disk ID is not authorized")
    geometry = compute_geometry(
        size_bytes=evidence.size_bytes,
        sector_size=evidence.sector_size,
        root_start_sector=authorization.root_start,
        policy=policy,
    )
    return geometry, Partition(1, authorization.boot_start, authorization.boot_size, 0x0C, False)


def _source_matches(parts: Sequence[Partition], authorization: Authorization) -> bool:
    return tuple(parts) == (
        Partition(1, authorization.boot_start, authorization.boot_size, 0x0C, False),
        Partition(2, authorization.root_start, authorization.root_source_size, 0x83, False),
    )


def _journal_conflict(
    journal: Journal,
    evidence: Evidence,
    geometry: Geometry,
    authorization: Authorization,
) -> str | None:
    if (
        journal.schema_version != authorization.journal_schema_version
        or journal.disk != evidence.disk
        or journal.root_partition != evidence.root_partition
        or journal.data_partition != partition_path(evidence.disk, 3)
        or journal.cid != evidence.cid
        or journal.size_bytes != evidence.size_bytes
        or journal.target != geometry
        or (
            authorization.journal_schema_version == 2 and not _is_sha256(journal.data_prefix_sha256)
        )
        or (authorization.journal_schema_version == 1 and journal.data_prefix_sha256 is not None)
    ):
        return "journal is not bound to the current exact target"
    return None


def _trigger_error(cmdline: Sequence[str], authorization: Authorization) -> str | None:
    present = tuple(token for token in cmdline if token in _SUPPORTED_TRIGGERS)
    if present == (authorization.bootstrap_trigger,):
        return None
    if not present:
        return "reviewed Bootstrap trigger is absent"
    if authorization.bootstrap_trigger not in present:
        return "runtime trigger belongs to a different Bootstrap contract"
    return "multiple Bootstrap contract triggers are present"


def _refusal_plan(
    evidence: Evidence,
    journal: Journal | None,
    code: RefusalCode,
    message: str,
) -> Plan:
    if code is RefusalCode.FIRST_RUN_ACTIVE:
        return Plan((Action(ActionKind.DEFER, detail=message),), journal)
    base = journal or Journal(
        schema_version=1,
        phase=Phase.REFUSED,
        disk=evidence.disk,
        root_partition=evidence.root_partition,
        data_partition=partition_path(evidence.disk, 3),
        cid=evidence.cid,
        size_bytes=evidence.size_bytes,
        stage_a_boot_id=evidence.boot_id,
        source_mbr_sha256=_sha256(evidence.mbr),
        target=Geometry(
            0,
            evidence.sector_size,
            Partition(2, 0, 1, 0x83),
            Partition(3, 1, 1, 0x07),
        ),
    )
    refused = _latch(base, code, message)
    return Plan((Action(ActionKind.LATCH, detail=message),), refused)


def _latch(journal: Journal, code: RefusalCode, message: str) -> Journal:
    return replace(
        journal,
        phase=Phase.REFUSED,
        refusal_code=code.value,
        refusal_message=message[:1000],
    )


def _sfdisk_input(disk: str, boot: Partition, geometry: Geometry, mbr_disk_id: int) -> str:
    if not 0 <= mbr_disk_id <= 0xFFFFFFFF:
        raise BootstrapError("MBR disk ID is outside the 32-bit range")
    lines = ["label: dos", f"label-id: 0x{mbr_disk_id:08x}", "unit: sectors", ""]
    for partition in (boot, geometry.root, geometry.data):
        node = partition_path(disk, partition.number)
        bootable = ", bootable" if partition.bootable else ""
        lines.append(
            f"{node} : start={partition.start_sector}, size={partition.size_sectors}, "
            f"type={partition.type_code:02x}{bootable}"
        )
    return "\n".join(lines) + "\n"


def _configure_volume(
    runtime: RuntimeIO,
    journal: Journal,
    evidence: Evidence,
    *,
    policy: CapacityPolicy,
    already_mounted: bool = False,
) -> None:
    uuid = _required_uuid(journal)
    if already_mounted and not _storage_mount_is_exact(evidence, journal):
        raise Refusal(
            RefusalCode.JOURNAL_CONFLICT,
            "recording mount exists but is not the exact intended exFAT partition",
        )
    runtime.mkdir(MOUNT_POINT, 0o750)
    uid = _decimal_identity(runtime.run((_ID, "-u", "dashcam"), timeout=10), "dashcam UID")
    root_uid = _decimal_identity(runtime.run((_ID, "-u", "root"), timeout=10), "root UID")
    storage_gid = _group_gid(
        runtime.read_text(GROUP_PATH),
        "dashcam-storage",
    )
    if root_uid != "0":
        raise BootstrapError("root account resolved to a nonzero UID")
    fstab = runtime.read_text(FSTAB_PATH)
    marker = "# dashcam-bootstrap-v1"
    line = (
        f"UUID={uuid} {MOUNT_POINT} exfat "
        f"noatime,nosuid,nodev,noexec,uid={uid},gid={storage_gid},"
        "fmask=0137,dmask=0027,nofail,x-systemd.device-timeout=10s 0 0 "
        f"{marker}"
    )
    existing: list[str] = []
    for item in fstab.splitlines():
        stripped = item.strip()
        fields = stripped.split()
        if not stripped or stripped.startswith("#"):
            existing.append(item)
            continue
        targets_recording_path = len(fields) >= 2 and fields[1] == MOUNT_POINT
        if marker in item:
            if (
                not targets_recording_path
                or fields[0] != f"UUID={uuid}"
                or len(fields) < 3
                or fields[2] != "exfat"
            ):
                raise Refusal(
                    RefusalCode.JOURNAL_CONFLICT,
                    "existing Bootstrap fstab entry conflicts with the exact target",
                )
            continue
        if targets_recording_path:
            raise Refusal(
                RefusalCode.JOURNAL_CONFLICT,
                "foreign fstab entry already owns the recording mount path",
            )
        existing.append(item)
    runtime.atomic_write(FSTAB_PATH, ("\n".join((*existing, line)) + "\n").encode(), 0o644)
    if not already_mounted:
        runtime.run((_MOUNT, MOUNT_POINT), timeout=30)
    mounted_source, mounted_filesystem, mounted_uuid = _observe_mount(runtime, MOUNT_POINT)
    if (
        mounted_source != journal.data_partition
        or mounted_filesystem != "exfat"
        or mounted_uuid != uuid
    ):
        raise Refusal(
            RefusalCode.JOURNAL_CONFLICT,
            "mount command did not produce the exact intended recording filesystem",
        )
    for name in ("pending", "clips", "protected", "quarantine"):
        runtime.mkdir(f"{MOUNT_POINT}/{name}", 0o750)
    sentinel = (
        json.dumps(
            _sentinel_mapping(journal),
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    runtime.atomic_write(SENTINEL_PATH, sentinel, 0o640)
    runtime.mkdir(str(PurePosixPath(ENV_PATH).parent), 0o750)
    runtime.atomic_write(ENV_PATH, _storage_identity_env(journal, policy), 0o640)
    runtime.set_owner(ENV_PATH, int(root_uid), int(storage_gid), 0o640)


def _group_gid(group_file: str, name: str) -> str:
    if not name or ":" in name or "\n" in name:
        raise BootstrapError("group name is invalid")
    matches: list[str] = []
    for line in group_file.splitlines():
        if not line or line.startswith("#"):
            continue
        fields = line.split(":")
        if len(fields) == 4 and fields[0] == name:
            matches.append(fields[2])
    if len(matches) != 1:
        raise BootstrapError(f"group identity is missing or ambiguous: {name}")
    return _decimal_identity(matches[0], f"{name} GID")


def _storage_identity_env(journal: Journal, policy: CapacityPolicy) -> bytes:
    uuid = _required_uuid(journal)
    payload = (
        "DASHCAM_STORAGE_SCHEMA_VERSION=1\n"
        "DASHCAM_STORAGE_LAYOUT_VERSION=1\n"
        f"DASHCAM_STORAGE_MOUNT={MOUNT_POINT}\n"
        f"DASHCAM_STORAGE_UUID={uuid}\n"
        f"DASHCAM_STORAGE_CID={journal.cid}\n"
        f"DASHCAM_STORAGE_SOURCE_MBR_SHA256={journal.source_mbr_sha256}\n"
        f"DASHCAM_STORAGE_ROOT_END_SECTOR={journal.target.root.end_sector}\n"
        f"DASHCAM_STORAGE_DATA_START_SECTOR={journal.target.data.start_sector}\n"
        f"DASHCAM_STORAGE_DATA_END_SECTOR={journal.target.data.end_sector}\n"
        f"DASHCAM_STORAGE_MINIMUM_CAPACITY_BYTES={policy.minimum_data_bytes}\n"
    )
    try:
        return payload.encode("ascii")
    except UnicodeEncodeError as error:
        raise BootstrapError("storage identity handoff is not ASCII") from error


def _write_completion(runtime: RuntimeIO, journal: Journal, evidence: Evidence) -> None:
    if not _storage_mount_is_exact(evidence, journal) or not _sentinel_is_exact(evidence, journal):
        raise Refusal(
            RefusalCode.JOURNAL_CONFLICT,
            "completion preconditions do not match the exact mounted target",
        )
    runtime.atomic_write(COMPLETE_PATH, _identity_payload(journal, evidence), 0o600)


def _identity_payload(journal: Journal, evidence: Evidence) -> bytes:
    return (
        json.dumps(
            _identity_mapping(journal, evidence),
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def _identity_mapping(journal: Journal, evidence: Evidence) -> Mapping[str, object]:
    data_uuid = _required_uuid(journal)
    if (
        journal.committed_mbr_sha256 is None
        or journal.committed_mbr_sha256 != _sha256(evidence.mbr)
        or evidence.boot_uuid is None
        or evidence.root_uuid is None
        or evidence.boot_partuuid is None
        or evidence.root_partuuid is None
        or evidence.data_partuuid is None
        or evidence.root_filesystem_bytes
        != journal.target.root.size_sectors * journal.target.sector_size
    ):
        raise BootstrapError("identity payload lacks exact MBR/partition/UUID evidence")
    disk_id, parts = parse_mbr(evidence.mbr)
    if tuple(parts) != (_boot_partition(parts), journal.target.root, journal.target.data):
        raise BootstrapError("identity payload target table is not exact")
    return {
        "schema_version": 1,
        "layout_version": 1,
        "cid": journal.cid,
        "size_bytes": journal.size_bytes,
        "sector_size": journal.target.sector_size,
        "mbr_disk_id": disk_id,
        "source_mbr_sha256": journal.source_mbr_sha256,
        "committed_mbr_sha256": journal.committed_mbr_sha256,
        "target": asdict(journal.target),
        "partitions": {
            "boot": {
                "device": partition_path(journal.disk, 1),
                "partuuid": evidence.boot_partuuid,
                "filesystem": "vfat",
                "uuid": evidence.boot_uuid,
            },
            "root": {
                "device": journal.root_partition,
                "partuuid": evidence.root_partuuid,
                "filesystem": "ext4",
                "uuid": evidence.root_uuid,
                "filesystem_bytes": evidence.root_filesystem_bytes,
            },
            "data": {
                "device": journal.data_partition,
                "partuuid": evidence.data_partuuid,
                "filesystem": "exfat",
                "label": "DASHCAM",
                "uuid": data_uuid,
            },
        },
    }


def _sentinel_is_exact(evidence: Evidence, journal: Journal) -> bool:
    return evidence.sentinel_identity == _sentinel_mapping(journal)


def _sentinel_mapping(journal: Journal) -> Mapping[str, object]:
    return {
        "layout_version": 1,
        "serial": journal.cid,
        "dashcam_uuid": _required_uuid(journal),
        "source_table_fingerprint": journal.source_mbr_sha256,
        "root_end_sector": journal.target.root.end_sector,
        "data_start_sector": journal.target.data.start_sector,
        "data_end_sector": journal.target.data.end_sector,
    }


def _storage_mount_is_exact(evidence: Evidence, journal: Journal) -> bool:
    return (
        evidence.data_filesystem == "exfat"
        and evidence.data_label == "DASHCAM"
        and evidence.data_uuid == journal.data_uuid
        and evidence.mounted_source == journal.data_partition
        and evidence.mounted_filesystem == "exfat"
        and evidence.mounted_uuid == journal.data_uuid
    )


def _completion_is_exact(evidence: Evidence, journal: Journal) -> bool:
    try:
        expected = _identity_mapping(journal, evidence)
    except BootstrapError:
        return False
    return (
        evidence.complete_identity == expected
        and _sentinel_is_exact(evidence, journal)
        and _storage_mount_is_exact(evidence, journal)
    )


def _required_uuid(journal: Journal) -> str:
    if journal.data_uuid is None or _UUID_RE.fullmatch(journal.data_uuid) is None:
        raise BootstrapError("journal lacks a valid exFAT UUID")
    return journal.data_uuid


def _decimal_identity(value: str, description: str) -> str:
    result = value.strip()
    if not result.isdecimal() or not 0 <= int(result) <= 2**31 - 1:
        raise BootstrapError(f"{description} is invalid")
    return result


def _observe_root_filesystem_size(runtime: RuntimeIO, root_partition: str) -> int:
    _validate_device(root_partition)
    output = runtime.run(
        (_DUMPE2FS, "-h", root_partition),
        timeout=15,
    )
    return _parse_dumpe2fs_header(output)


def _parse_dumpe2fs_header(output: str) -> int:
    """Return ext4's total geometry, not lsblk's usable-byte estimate."""

    if len(output.encode()) > 256 * 1024:
        raise BootstrapError("dumpe2fs output exceeded its bound")
    required: dict[str, str] = {}
    wanted = {"Filesystem magic number", "Block count", "Block size"}
    for line in output.splitlines():
        key, separator, value = line.partition(":")
        key = key.strip()
        if not separator or key not in wanted:
            continue
        if key in required:
            raise BootstrapError(f"dumpe2fs repeated {key}")
        required[key] = value.strip()
    if set(required) != wanted or required["Filesystem magic number"].lower() != "0xef53":
        raise BootstrapError("dumpe2fs did not report one ext4 filesystem header")
    block_count = required["Block count"]
    block_size = required["Block size"]
    if not block_count.isdecimal() or not block_size.isdecimal():
        raise BootstrapError("dumpe2fs ext4 geometry is not decimal")
    count = int(block_count)
    size = int(block_size)
    if (
        count <= 0
        or size not in {1024, 2048, 4096, 8192, 16384, 32768, 65536}
        or count > (2**63 - 1) // size
    ):
        raise BootstrapError("dumpe2fs ext4 geometry is outside its bound")
    return count * size


def _observe_mount(runtime: RuntimeIO, target: str) -> tuple[str, str, str]:
    output = runtime.run(
        (_FINDMNT, "-J", "-o", "SOURCE,FSTYPE,UUID,TARGET", target),
        timeout=15,
    )
    fields = _parse_findmnt_json(output, target)
    source = (
        os.path.realpath(fields["source"])
        if isinstance(runtime, PosixRuntime)
        else fields["source"]
    )
    return source, fields["fstype"], fields["uuid"]


def _assert_live_boot_mount(runtime: RuntimeIO, evidence: Evidence) -> None:
    source, filesystem, uuid = _observe_mount(runtime, "/boot/firmware")
    if (
        source != partition_path(evidence.disk, 1)
        or source != evidence.boot_mounted_source
        or filesystem != "vfat"
        or uuid != evidence.boot_uuid
    ):
        raise Refusal(
            RefusalCode.IDENTITY_MISMATCH,
            "live /boot/firmware mount changed before the FAT backup write",
        )


def _validate_absolute_path(path: str) -> None:
    pure = PurePosixPath(path)
    if not pure.is_absolute() or ".." in pure.parts or str(pure) != path:
        raise BootstrapError(f"unsafe non-canonical absolute path: {path}")


def _assert_safe_parent_chain(path: str) -> None:
    _validate_absolute_path(path)
    current = "/"
    for component in PurePosixPath(path).parts[1:-1]:
        current = str(PurePosixPath(current) / component)
        _assert_existing_path_safe(current, require_directory=True)


def _assert_existing_path_safe(path: str, *, require_directory: bool) -> None:
    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode):
        raise BootstrapError(f"refusing symbolic-link path component: {path}")
    if require_directory and not stat.S_ISDIR(metadata.st_mode):
        raise BootstrapError(f"required directory path is not a directory: {path}")


def _validate_evidence(evidence: Evidence) -> None:
    _validate_device(evidence.disk)
    _validate_device(evidence.root_partition)
    if evidence.root_partition != partition_path(evidence.disk, 2):
        raise Refusal(RefusalCode.IDENTITY_MISMATCH, "mounted root is not partition 2")
    if _CID_RE.fullmatch(evidence.cid) is None:
        raise Refusal(RefusalCode.IDENTITY_MISMATCH, "CID is malformed")
    if not evidence.boot_id or len(evidence.boot_id) > 128:
        raise BootstrapError("boot ID is malformed")
    expected_boot = partition_path(evidence.disk, 1)
    if (
        evidence.boot_partition != expected_boot
        or evidence.boot_mounted_source != expected_boot
        or evidence.boot_filesystem != "vfat"
        or evidence.boot_uuid is None
        or _UUID_RE.fullmatch(evidence.boot_uuid) is None
        or evidence.boot_partuuid is None
        or _UUID_RE.fullmatch(evidence.boot_partuuid) is None
    ):
        raise Refusal(
            RefusalCode.IDENTITY_MISMATCH,
            "/boot/firmware is not the exact mounted VFAT partition 1",
        )
    if (
        evidence.root_filesystem != "ext4"
        or evidence.root_filesystem_bytes <= 0
        or evidence.root_uuid is None
        or _UUID_RE.fullmatch(evidence.root_uuid) is None
        or evidence.root_partuuid is None
        or _UUID_RE.fullmatch(evidence.root_partuuid) is None
    ):
        raise Refusal(
            RefusalCode.IDENTITY_MISMATCH,
            "mounted root lacks exact ext4 UUID/PARTUUID/size evidence",
        )
    if evidence.cloud_init_status not in {
        "absent",
        "running",
        "done",
        "done_known_degraded",
        "error",
        "unknown",
    }:
        raise BootstrapError("cloud-init status is outside the closed evidence schema")


def _validate_journal(journal: Journal) -> None:
    if journal.schema_version not in {1, 2}:
        raise BootstrapError("unsupported journal version")
    _validate_device(journal.disk)
    _validate_device(journal.root_partition)
    _validate_device(journal.data_partition)
    if _CID_RE.fullmatch(journal.cid) is None:
        raise BootstrapError("journal CID is malformed")
    if _SHA256_RE.fullmatch(journal.source_mbr_sha256) is None:
        raise BootstrapError("journal source MBR hash is malformed")
    if (
        journal.committed_mbr_sha256 is not None
        and _SHA256_RE.fullmatch(journal.committed_mbr_sha256) is None
    ):
        raise BootstrapError("journal committed MBR hash is malformed")
    if journal.schema_version == 1 and journal.data_prefix_sha256 is not None:
        raise BootstrapError("release journal cannot contain stock-prefix provenance")
    if (
        journal.schema_version == 2
        and journal.phase is not Phase.REFUSED
        and not _is_sha256(journal.data_prefix_sha256)
    ):
        raise BootstrapError("stock journal prefix hash is missing or malformed")


def _boot_partition(parts: Sequence[Partition]) -> Partition:
    matches = [part for part in parts if part.number == 1]
    if len(matches) != 1:
        raise Refusal(RefusalCode.TORN_TABLE, "boot partition identity is absent")
    return matches[0]


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _is_sha256(value: str | None) -> bool:
    return value is not None and _SHA256_RE.fullmatch(value) is not None


def _align_up(value: int, alignment: int) -> int:
    return (value + alignment - 1) // alignment * alignment


def _align_down(value: int, alignment: int) -> int:
    return value // alignment * alignment


def _validate_device(value: str) -> None:
    if _DEVICE_RE.fullmatch(value) is None or ".." in PurePosixPath(value).parts:
        raise BootstrapError("unsafe device path")


def _code_for_exception(exc: Exception) -> RefusalCode:
    return exc.code if isinstance(exc, Refusal) else RefusalCode.EXECUTION_FAILED


def _int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BootstrapError(f"{name} must be an integer")
    return value


def _str(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise BootstrapError(f"{name} must be bounded text")
    return value


def _optional_str(value: object) -> str | None:
    return None if value is None else _str(value, "optional journal field")


def _partition_from_mapping(raw: Mapping[str, object]) -> Partition:
    return Partition(
        number=_int(raw.get("number"), "partition.number"),
        start_sector=_int(raw.get("start_sector"), "partition.start_sector"),
        size_sectors=_int(raw.get("size_sectors"), "partition.size_sectors"),
        type_code=_int(raw.get("type_code"), "partition.type_code"),
        bootable=bool(raw.get("bootable", False)),
    )


def _geometry_from_mapping(raw: Mapping[str, object]) -> Geometry:
    root = raw.get("root")
    data = raw.get("data")
    if not isinstance(root, Mapping) or not isinstance(data, Mapping):
        raise BootstrapError("journal target partitions are malformed")
    return Geometry(
        total_sectors=_int(raw.get("total_sectors"), "target.total_sectors"),
        sector_size=_int(raw.get("sector_size"), "target.sector_size"),
        root=_partition_from_mapping(cast(Mapping[str, object], root)),
        data=_partition_from_mapping(cast(Mapping[str, object], data)),
    )


def _run_discovery(argv: tuple[str, ...], *, allow_absent_mount: bool = False) -> str:
    allowed = {"/usr/bin/lsblk", "/usr/bin/findmnt", "/usr/sbin/blkid", "/usr/sbin/wipefs"}
    if argv[0] not in allowed:
        raise BootstrapError("discovery command is not allowed")
    result = subprocess.run(argv, text=True, capture_output=True, check=False, timeout=15)
    accepted = {0}
    if argv[0] == _BLKID:
        accepted.add(2)  # util-linux: no identifiable signature
    if argv[0] == _FINDMNT and allow_absent_mount:
        accepted.add(1)  # findmnt: requested mount target is absent
    if result.returncode not in accepted:
        stderr = result.stderr.strip()[:1000]
        raise BootstrapError(f"discovery command failed ({result.returncode}): {argv[0]}: {stderr}")
    if len(result.stdout.encode()) > 256 * 1024:
        raise BootstrapError("discovery command output exceeded its bound")
    return result.stdout


def _derive_disk(
    lsblk_json: str, root_partition: str
) -> tuple[str, int, int, dict[str, Mapping[str, object]]]:
    document = json.loads(lsblk_json)
    devices = document.get("blockdevices") if isinstance(document, dict) else None
    if not isinstance(devices, list):
        raise BootstrapError("lsblk output is malformed")
    flat: list[Mapping[str, object]] = []
    by_path: dict[str, Mapping[str, object]] = {}

    def visit(items: list[object]) -> None:
        for item in items:
            if not isinstance(item, Mapping):
                raise BootstrapError("lsblk node is malformed")
            node = cast(Mapping[str, object], item)
            flat.append(node)
            path = node.get("path")
            if isinstance(path, str):
                if path in by_path:
                    raise BootstrapError("lsblk contains duplicate device paths")
                by_path[path] = node
            children = node.get("children", [])
            if isinstance(children, list):
                visit(children)

    visit(cast(list[object], devices))
    root = next((node for node in flat if node.get("path") == root_partition), None)
    if root is None:
        raise BootstrapError("mounted root source is absent from lsblk")
    pkname = root.get("pkname")
    if not isinstance(pkname, str) or not pkname:
        raise BootstrapError("root source has no backing disk")
    disk_path = pkname if pkname.startswith("/dev/") else f"/dev/{pkname}"
    disk = next((node for node in flat if node.get("path") == disk_path), None)
    if disk is None or disk.get("type") != "disk":
        raise BootstrapError("root backing device is not one disk")
    return (
        disk_path,
        _int(disk.get("size"), "disk.size"),
        _int(disk.get("log-sec"), "disk.log-sec"),
        by_path,
    )


def _canonical_sysfs_cid_path(disk_name: str) -> str:
    """Resolve one mmc disk's class symlinks to a bound canonical CID file."""

    if not disk_name or "/" in disk_name or _SYSFS_COMPONENT_RE.fullmatch(disk_name) is None:
        raise Refusal(RefusalCode.IDENTITY_MISMATCH, "derived disk name is unsafe")
    class_root = f"/sys/class/block/{disk_name}"
    block_path = _resolved_sysfs_path(class_root, basename=disk_name)
    device_path = _resolved_sysfs_path(f"{class_root}/device")
    cid_path = _resolved_sysfs_path(f"{class_root}/device/cid", basename="cid")
    block = PurePosixPath(block_path)
    device = PurePosixPath(device_path)
    cid = PurePosixPath(cid_path)
    if device not in block.parents:
        raise Refusal(
            RefusalCode.IDENTITY_MISMATCH,
            "canonical sysfs device is not bound to the derived disk",
        )
    if cid.parent != device:
        raise Refusal(
            RefusalCode.IDENTITY_MISMATCH,
            "canonical CID is not bound to the derived disk device",
        )
    return cid_path


def _resolved_sysfs_path(path: str, *, basename: str | None = None) -> str:
    resolved = os.path.realpath(path)
    pure = PurePosixPath(resolved)
    if (
        not pure.is_absolute()
        or str(pure) != resolved
        or resolved == "/sys/devices"
        or not resolved.startswith("/sys/devices/")
        or any(_SYSFS_COMPONENT_RE.fullmatch(component) is None for component in pure.parts[3:])
        or (basename is not None and pure.name != basename)
        or os.path.realpath(resolved) != resolved
    ):
        raise Refusal(
            RefusalCode.IDENTITY_MISMATCH,
            "sysfs CID resolution is non-canonical or escaped /sys/devices",
        )
    return resolved


def _blkid_fields(path: str) -> dict[str, str]:
    output = _run_discovery((_BLKID, "-o", "export", path))
    return _parse_blkid_export(output)


def _parse_blkid_export(output: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            fields[key] = value
    return fields


def _wipefs_signatures(path: str) -> tuple[str, ...]:
    output = _run_discovery(("/usr/sbin/wipefs", "--json", path))
    return _parse_wipefs_json(output)


def _zero_prefix_bytes(runtime: RuntimeIO, path: str, required_bytes: int) -> int:
    if required_bytes <= 0 or required_bytes > 16 * MIB:
        raise BootstrapError("zero-prefix read is outside its closed bound")
    payload = runtime.read_bytes(path, limit=required_bytes)
    if len(payload) != required_bytes:
        raise BootstrapError("partition 3 zero-prefix read was short")
    return required_bytes if not any(payload) else 0


def _parse_wipefs_json(output: str) -> tuple[str, ...]:
    if not output.strip():
        return ()
    document = json.loads(output)
    signatures = document.get("signatures") if isinstance(document, dict) else None
    if not isinstance(signatures, list):
        raise BootstrapError("wipefs output is malformed")
    return tuple(
        str(item.get("type"))
        for item in signatures
        if isinstance(item, Mapping) and item.get("type")
    )


def _has_exact_exfat_wipefs_signatures(signatures: Sequence[str]) -> bool:
    """Recognize the exact pair wipefs reports for a normal exFAT volume.

    util-linux reports both the exFAT identity at offset 0x3 and the DOS boot
    signature at offset 0x1fe.  This predicate is deliberately used only
    after durable format intent; pre-format blankness still requires no
    signatures at all.
    """

    return len(signatures) == 2 and set(signatures) == {"exfat", "dos"}


def _firstrun_active(cmdline: Sequence[str]) -> bool:
    if any(token.startswith("systemd.run=") for token in cmdline):
        return True
    for unit in ("firstrun.service", "userconfig.service"):
        result = subprocess.run(
            ("/usr/bin/systemctl", "is-active", "--quiet", unit),
            capture_output=True,
            check=False,
            timeout=5,
        )
        if result.returncode == 0:
            return True
    return False


def _cloud_init_status() -> str:
    executable = "/usr/bin/cloud-init"
    if not os.path.exists(executable):
        return "absent"
    try:
        result = subprocess.run(
            (executable, "status", "--format", "json"),
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if len(result.stdout.encode()) > 64 * 1024:
        return "unknown"
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError:
        return "unknown"
    if not isinstance(value, Mapping):
        return "unknown"
    return _classify_cloud_init_status(cast(Mapping[str, object], value), result.returncode)


def _classify_cloud_init_status(value: Mapping[str, object], returncode: int) -> str:
    status = value.get("status")
    errors = value.get("errors", [])
    recoverable = value.get("recoverable_errors", {})
    if returncode == 0 and status == "done" and errors in ([], None) and recoverable in ({}, None):
        return "done"
    if (
        returncode == 2
        and status == "done"
        and value.get("extended_status") == "degraded done"
        and errors == []
        and recoverable == {"WARNING": [KNOWN_CLOUD_INIT_WARNING]}
        and "stage" in value
        and value["stage"] is None
    ):
        return "done_known_degraded"
    if status in {"running", "not run"} or returncode == 2:
        return "running"
    if status in {"error", "degraded done"} or returncode == 1:
        return "error"
    return "unknown"


def _cloud_init_ready(status: str, authorization: Authorization) -> bool:
    return status == "done" or (
        authorization.journal_schema_version == 2 and status == "done_known_degraded"
    )


def _read_optional_json(runtime: RuntimeIO, path: str) -> Mapping[str, object] | None:
    if not runtime.exists(path):
        return None
    value = json.loads(runtime.read_text(path))
    if not isinstance(value, Mapping):
        raise BootstrapError(f"{path} is not a JSON object")
    return cast(Mapping[str, object], value)


def _optional_mapping_str(value: Mapping[str, object], key: str) -> str | None:
    item = value.get(key)
    return item if isinstance(item, str) and item else None


def _parse_findmnt_json(output: str, expected_target: str) -> dict[str, str]:
    value = json.loads(output)
    filesystems = value.get("filesystems") if isinstance(value, Mapping) else None
    if not isinstance(filesystems, list) or len(filesystems) != 1:
        raise BootstrapError("findmnt did not return exactly one mounted filesystem")
    item = filesystems[0]
    if not isinstance(item, Mapping):
        raise BootstrapError("findmnt filesystem entry is malformed")
    fields: dict[str, str] = {}
    for key in ("source", "fstype", "uuid", "target"):
        field = item.get(key)
        if not isinstance(field, str) or not field:
            raise BootstrapError(f"findmnt field is missing: {key}")
        fields[key] = field
    if fields["target"] != expected_target:
        raise BootstrapError("findmnt returned a different target")
    return fields


def _findmnt_fields(target: str, *, allow_absent: bool = False) -> dict[str, str] | None:
    output = _run_discovery(
        (_FINDMNT, "-J", "-o", "SOURCE,FSTYPE,UUID,TARGET", target),
        allow_absent_mount=allow_absent,
    )
    if allow_absent and not output.strip():
        return None
    return _parse_findmnt_json(output, target)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AUTHORIZED_CID",
    "AUTHORIZED_SIZE_BYTES",
    "BOOT_MIRROR",
    "COMPLETE_PATH",
    "CONTRACT_PATH",
    "ENV_PATH",
    "EXACT_CARD_AUTHORIZATION",
    "EXACT_STOCK_CARD_AUTHORIZATION",
    "FSTAB_PATH",
    "MOUNT_POINT",
    "SSH_DEV_TRIGGER",
    "STATE_PATH",
    "TRIGGER",
    "Action",
    "ActionKind",
    "Authorization",
    "BootstrapError",
    "CapacityPolicy",
    "Evidence",
    "Geometry",
    "Journal",
    "Partition",
    "Phase",
    "Plan",
    "PosixRuntime",
    "Refusal",
    "RefusalCode",
    "RuntimeIO",
    "compute_geometry",
    "execute_stage_a",
    "execute_stage_b",
    "journal_from_json",
    "journal_json",
    "load_bootstrap_contract",
    "main",
    "parse_mbr",
    "partition_path",
    "plan_stage_a",
    "plan_stage_b",
    "table_matches",
]
