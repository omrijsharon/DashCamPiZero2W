"""Dry-run-only provisioning planner with identity and refusal gates."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Final

from dashcam.provisioning.layout import (
    ComputedLayout,
    DeviceIdentity,
    DeviceObservation,
    LayoutSpec,
    LayoutState,
    VerificationReport,
    mbr_partuuid,
    verify_layout,
)

MAX_CANDIDATE_DEVICES: Final = 16
MAX_ACTIONS: Final = 16
_SAFE_ARG_RE = re.compile(r"[\x20-\x7e]{1,512}")

Executor = Callable[[tuple[str, ...]], None]


class ProvisioningError(ValueError):
    """Base class for a refused or unsupported provisioning request."""


class PlannerRefusalCode(StrEnum):
    AMBIGUOUS_DEVICE = "ambiguous_device"
    IDENTITY_MISMATCH = "identity_mismatch"
    IDENTITY_CHANGED = "identity_changed"
    LAYOUT_REFUSED = "layout_refused"
    CONFIRMATION_REQUIRED = "confirmation_required"
    EXECUTION_DISABLED = "execution_disabled"


class ProvisioningRefused(ProvisioningError):
    """A fail-closed planner outcome with a stable reason code."""

    def __init__(self, code: PlannerRefusalCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class ActionKind(StrEnum):
    BACKUP_PARTITION_TABLE = "backup_partition_table"
    VALIDATE_PARTITION_TABLE_BACKUP = "validate_partition_table_backup"
    WRITE_PARTITION_TABLE = "write_partition_table"
    VERIFY_WRITTEN_PARTITION_TABLE = "verify_written_partition_table"
    REREAD_PARTITION_TABLE = "reread_partition_table"
    CHECK_ROOT_FILESYSTEM = "check_root_filesystem"
    GROW_ROOT_FILESYSTEM = "grow_root_filesystem"
    FORMAT_NEW_RECORDING_FILESYSTEM = "format_new_recording_filesystem"
    CAPTURE_RECORDING_UUID = "capture_recording_uuid"
    CONFIGURE_UUID_MOUNT = "configure_uuid_mount"
    CREATE_VOLUME_LAYOUT = "create_volume_layout"
    CREATE_VOLUME_SENTINEL = "create_volume_sentinel"
    WRITE_STATE_MARKER = "write_state_marker"
    CONTROLLED_REBOOT = "controlled_reboot_if_required"


@dataclass(frozen=True, slots=True)
class PlannedAction:
    sequence: int
    kind: ActionKind
    description: str
    argv: tuple[str, ...]
    destructive: bool
    requires_previous_success: bool = True
    stdin_text: str | None = None
    stdout_path: str | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.sequence <= MAX_ACTIONS:
            raise ProvisioningError("action sequence is out of bounds")
        if not self.description or len(self.description) > 256:
            raise ProvisioningError("action description must be bounded")
        for argument in self.argv:
            if _SAFE_ARG_RE.fullmatch(argument) is None or "\x00" in argument:
                raise ProvisioningError("action argv contains unsafe or unbounded data")
        if self.stdin_text is not None and (
            not self.stdin_text or len(self.stdin_text) > 4096 or "\x00" in self.stdin_text
        ):
            raise ProvisioningError("action stdin is unsafe or unbounded")
        if self.stdout_path is not None:
            _validate_artifact_path(self.stdout_path)


@dataclass(frozen=True, slots=True)
class ProvisioningPlan:
    schema_version: int
    dry_run: bool
    execution_supported: bool
    identity: DeviceIdentity
    verification: VerificationReport
    confirmation_phrase: str
    actions: tuple[PlannedAction, ...]
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "dry_run": self.dry_run,
            "execution_supported": self.execution_supported,
            "identity": asdict(self.identity),
            "verification": self.verification.to_dict(),
            "confirmation_phrase": self.confirmation_phrase,
            "actions": [
                {
                    "sequence": action.sequence,
                    "kind": action.kind.value,
                    "description": action.description,
                    "argv": list(action.argv),
                    "destructive": action.destructive,
                    "requires_previous_success": action.requires_previous_success,
                    "stdin_text": action.stdin_text,
                    "stdout_path": action.stdout_path,
                }
                for action in self.actions
            ],
            "notes": list(self.notes),
        }


def confirmation_phrase(identity: DeviceIdentity, mbr_disk_id: str | None = None) -> str:
    """Bind any future confirmation to more than a mutable device path."""

    phrase = (
        f"PROVISION {identity.resolved_path} SERIAL={identity.serial} "
        f"SIZE={identity.size_bytes} TABLE={identity.partition_table_fingerprint}"
    )
    return phrase if mbr_disk_id is None else f"{phrase} MBR={mbr_disk_id}"


def author_provisioning_plan(
    *,
    spec: LayoutSpec,
    observations: Sequence[DeviceObservation],
    expected_identity: DeviceIdentity,
    recheck: DeviceObservation | None = None,
    dry_run: bool = True,
    typed_confirmation: str | None = None,
    executor: Executor | None = None,
) -> ProvisioningPlan:
    """Return a deterministic plan or fail closed.

    Version 1 is intentionally authoring-only.  Even with the exact confirmation
    phrase, ``dry_run=False`` raises ``EXECUTION_DISABLED`` and never calls the
    optional executor.  Keeping the executor parameter injectable makes that
    safety property directly testable.
    """

    del executor  # Execution is not an available code path in the local v1 tool.
    if len(observations) > MAX_CANDIDATE_DEVICES:
        raise ProvisioningRefused(PlannerRefusalCode.AMBIGUOUS_DEVICE, "too many candidate devices")
    matches = [item for item in observations if item.identity == expected_identity]
    if len(matches) != 1:
        code = (
            PlannerRefusalCode.IDENTITY_MISMATCH
            if not matches
            else PlannerRefusalCode.AMBIGUOUS_DEVICE
        )
        raise ProvisioningRefused(code, "expected identity did not select exactly one device")
    selected = matches[0]
    if recheck is not None and recheck.identity != selected.identity:
        raise ProvisioningRefused(
            PlannerRefusalCode.IDENTITY_CHANGED,
            "device identity changed between observations",
        )
    current = selected if recheck is None else recheck
    report = verify_layout(spec, current)
    if not report.accepted:
        refusal_list = ",".join(code.value for code in report.refusal_codes)
        raise ProvisioningRefused(
            PlannerRefusalCode.LAYOUT_REFUSED,
            f"layout verifier refused the device: {refusal_list}",
        )

    required_confirmation = confirmation_phrase(current.identity, current.mbr_disk_id)
    if not dry_run:
        if typed_confirmation != required_confirmation:
            raise ProvisioningRefused(
                PlannerRefusalCode.CONFIRMATION_REQUIRED,
                "non-dry-run requires the exact identity-bound confirmation phrase",
            )
        raise ProvisioningRefused(
            PlannerRefusalCode.EXECUTION_DISABLED,
            "version 1 only authors plans; no executor is enabled",
        )

    actions = (
        ()
        if report.state is LayoutState.ALREADY_PROVISIONED
        else _source_actions(spec, current, report)
    )
    return ProvisioningPlan(
        schema_version=1,
        dry_run=True,
        execution_supported=False,
        identity=current.identity,
        verification=report,
        confirmation_phrase=required_confirmation,
        actions=actions,
        notes=(
            "No command was executed; argv entries are review artifacts.",
            "A future executor must repeat identity and layout verification immediately "
            "before every destructive phase.",
            "Partition-table backup is ordered before the first mutation.",
            "The saved sfdisk dump must validate against the selected MBR identity "
            "before mutation.",
            "Formatting is planned only because the verified source has no partition 3.",
            "The observed MBR disk ID is preserved; boot/root starts, DOS types, boot flags, "
            "filesystem UUIDs, and derived PARTUUID references are identity-bound.",
            "UUID capture must complete before binding the ext4 marker and exFAT sentinel.",
        ),
    )


def _source_actions(
    spec: LayoutSpec, observed: DeviceObservation, report: VerificationReport
) -> tuple[PlannedAction, ...]:
    computed = report.computed
    if computed is None:
        raise ProvisioningRefused(
            PlannerRefusalCode.LAYOUT_REFUSED, "layout bounds were not computed"
        )
    device = observed.identity.resolved_path
    root_device = _partition_path(device, spec.root.number)
    data_device = _partition_path(device, spec.data.number)
    fingerprint = observed.identity.partition_table_fingerprint
    mbr_disk_id = _required_mbr_disk_id(observed)
    backup_path = f"/var/lib/dashcam/provisioning/partition-table-{fingerprint}.sfdisk"
    sfdisk_input = _sfdisk_input(spec, observed, computed)
    boot = next(part for part in observed.partitions if part.number == spec.boot.number)
    root = next(part for part in observed.partitions if part.number == spec.root.number)
    marker_payload = json.dumps(
        {
            "boot_filesystem_uuid": boot.uuid,
            "boot_partuuid": boot.partuuid,
            "dashcam_uuid": "${CAPTURED_DASHCAM_UUID}",
            "data_end_sector": computed.data_end_sector,
            "data_partuuid": mbr_partuuid(mbr_disk_id, spec.data.number),
            "data_start_sector": computed.data_start_sector,
            "layout_version": spec.schema_version,
            "mbr_disk_id": mbr_disk_id,
            "root_end_sector": computed.root_end_sector,
            "root_filesystem_uuid": root.uuid,
            "root_partuuid": root.partuuid,
            "serial": observed.identity.serial,
            "source_table_fingerprint": fingerprint,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    actions = (
        PlannedAction(
            1,
            ActionKind.BACKUP_PARTITION_TABLE,
            "Capture the verified DOS/MBR table as an sfdisk dump before any mutation.",
            ("sfdisk", "--dump", device),
            False,
            stdout_path=backup_path,
        ),
        PlannedAction(
            2,
            ActionKind.VALIDATE_PARTITION_TABLE_BACKUP,
            "Validate the saved dump against the observed path, MBR disk ID, and fingerprint.",
            (
                "dashcam-provision-internal",
                "validate-sfdisk-backup",
                backup_path,
                device,
                mbr_disk_id,
                fingerprint,
            ),
            False,
        ),
        PlannedAction(
            3,
            ActionKind.WRITE_PARTITION_TABLE,
            "Write the complete identity-preserving DOS/MBR table from bounded sfdisk input.",
            ("sfdisk", "--no-reread", "--force", device),
            True,
            stdin_text=sfdisk_input,
        ),
        PlannedAction(
            4,
            ActionKind.VERIFY_WRITTEN_PARTITION_TABLE,
            "Verify the written partition table before requesting a kernel reread.",
            ("sfdisk", "--verify", device),
            False,
        ),
        PlannedAction(
            5,
            ActionKind.REREAD_PARTITION_TABLE,
            "Request a kernel partition-table reread in the proven-safe early-boot environment.",
            ("partprobe", device),
            True,
        ),
        PlannedAction(
            6,
            ActionKind.CHECK_ROOT_FILESYSTEM,
            "Check the unmounted ext4 filesystem before growing it.",
            ("e2fsck", "-f", "-p", root_device),
            True,
        ),
        PlannedAction(
            7,
            ActionKind.GROW_ROOT_FILESYSTEM,
            "Grow ext4 to its verified partition boundary; never shrink it.",
            ("resize2fs", root_device),
            True,
        ),
        PlannedAction(
            8,
            ActionKind.FORMAT_NEW_RECORDING_FILESYSTEM,
            "Format only the newly created, previously absent partition 3.",
            ("mkfs.exfat", "-n", spec.data.label, data_device),
            True,
        ),
        PlannedAction(
            9,
            ActionKind.CAPTURE_RECORDING_UUID,
            "Capture and validate the exFAT UUID for UUID-based mounting.",
            ("blkid", "--output", "value", "--match-tag", "UUID", data_device),
            False,
        ),
        PlannedAction(
            10,
            ActionKind.CONFIGURE_UUID_MOUNT,
            "Persist the captured UUID and configure the restricted exFAT mount.",
            (
                "dashcam-provision-internal",
                "configure-uuid-mount",
                "/etc/dashcam/storage-volume.env",
                "/etc/fstab",
                "${CAPTURED_DASHCAM_UUID}",
                "/srv/dashcam",
                "exfat",
                (
                    "noatime,nosuid,nodev,noexec,"
                    "uid=${DASHCAM_UID},gid=${DASHCAM_STORAGE_GID},umask=0007"
                ),
            ),
            True,
        ),
        PlannedAction(
            11,
            ActionKind.CREATE_VOLUME_LAYOUT,
            "Mount by captured UUID and create the application-owned volume directories.",
            (
                "dashcam-provision-internal",
                "create-volume-layout",
                "/srv/dashcam",
                "pending",
                "clips",
                "protected",
                "quarantine",
            ),
            True,
        ),
        PlannedAction(
            12,
            ActionKind.CREATE_VOLUME_SENTINEL,
            "Atomically create the UUID/device-bound identity sentinel after mounting by UUID.",
            (
                "dashcam-provision-internal",
                "write-volume-sentinel",
                f"/srv/dashcam/{spec.volume_sentinel_name}",
                marker_payload,
            ),
            True,
        ),
        PlannedAction(
            13,
            ActionKind.WRITE_STATE_MARKER,
            "Atomically mark provisioning complete on ext4 after sentinel verification.",
            (
                "dashcam-provision-internal",
                "write-state-marker",
                spec.state_marker_path,
                marker_payload,
            ),
            True,
        ),
        PlannedAction(
            14,
            ActionKind.CONTROLLED_REBOOT,
            "Reboot only if the kernel could not safely adopt the new table.",
            ("systemctl", "reboot"),
            True,
        ),
    )
    if len(actions) > MAX_ACTIONS:
        raise ProvisioningError("generated action plan exceeds its bound")
    return actions


def _partition_path(device: str, number: int) -> str:
    """Derive a bounded review-only partition path for common Linux block names."""

    if not device.startswith("/dev/") or ".." in PurePosixPath(device).parts:
        raise ProvisioningError("device path is not a validated /dev path")
    separator = "p" if device[-1].isdigit() else ""
    result = f"{device}{separator}{number}"
    if len(result) > 128:
        raise ProvisioningError("derived partition path is too long")
    return result


def _sfdisk_input(
    spec: LayoutSpec,
    observed: DeviceObservation,
    computed: ComputedLayout,
) -> str:
    """Build bounded sector-exact DOS/MBR input while retaining source identities."""

    by_number = {partition.number: partition for partition in observed.partitions}
    boot = by_number[spec.boot.number]
    mbr_disk_id = _required_mbr_disk_id(observed)
    lines = [
        "label: dos",
        f"label-id: {mbr_disk_id}",
        f"device: {observed.identity.resolved_path}",
        "unit: sectors",
        f"sector-size: {spec.sector_size_bytes}",
        "",
        _sfdisk_partition_line(
            _partition_path(observed.identity.resolved_path, spec.boot.number),
            boot.start_sector,
            boot.size_sectors,
            spec.boot.partition_type,
            spec.boot.bootable,
        ),
        _sfdisk_partition_line(
            _partition_path(observed.identity.resolved_path, spec.root.number),
            computed.root_start_sector,
            computed.root_size_sectors,
            spec.root.partition_type,
            spec.root.bootable,
        ),
        _sfdisk_partition_line(
            _partition_path(observed.identity.resolved_path, spec.data.number),
            computed.data_start_sector,
            computed.data_size_sectors,
            spec.data.partition_type,
            spec.data.bootable,
        ),
        "",
    ]
    return "\n".join(lines)


def _required_mbr_disk_id(observed: DeviceObservation) -> str:
    if observed.mbr_disk_id is None:
        raise ProvisioningRefused(
            PlannerRefusalCode.LAYOUT_REFUSED,
            "a verified observed MBR disk ID is required",
        )
    return observed.mbr_disk_id


def _sfdisk_partition_line(
    path: str,
    start_sector: int,
    size_sectors: int,
    partition_type: str,
    bootable: bool,
) -> str:
    boot_flag = ", bootable" if bootable else ""
    return (
        f"{path} : start={start_sector}, size={size_sectors}, "
        f"type={partition_type.removeprefix('0x')}{boot_flag}"
    )


def _validate_artifact_path(path: str) -> None:
    parsed = PurePosixPath(path)
    if (
        len(path) > 256
        or not path.startswith("/var/lib/dashcam/provisioning/")
        or ".." in parsed.parts
        or "\x00" in path
    ):
        raise ProvisioningError("action output path is outside the provisioning backup directory")


__all__ = [
    "ActionKind",
    "Executor",
    "PlannedAction",
    "PlannerRefusalCode",
    "ProvisioningError",
    "ProvisioningPlan",
    "ProvisioningRefused",
    "author_provisioning_plan",
    "confirmation_phrase",
]
