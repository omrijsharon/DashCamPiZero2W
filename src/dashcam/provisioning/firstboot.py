"""Fail-closed first-boot storage transaction model.

This module models the destructive early-boot transaction and the post-root
reconciliation separately.  It does not discover devices and execution is
disabled unless the caller supplies both an executor and an explicit enable
flag.  Every mutating phase has an observable postcondition so a later boot can
recover from a command succeeding before its journal update.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Final, NoReturn

from dashcam.provisioning.layout import (
    ComputedLayout,
    DeviceIdentity,
    DeviceObservation,
    LayoutError,
    LayoutSpec,
    PartitionObservation,
    compute_layout,
    fingerprint_partition_table,
    mbr_partuuid,
    observation_from_mapping,
)

MAX_ACTIONS: Final = 8
MAX_RETRIES: Final = 3
MAX_REBOOTS: Final = 1
MAX_ARGV_ITEMS: Final = 16
MAX_ARGUMENT_BYTES: Final = 1024
MAX_STDIN_BYTES: Final = 8192
ROOT_TRIGGER: Final = "dashcam.bounded_provision=v1"
MOUNT_OPTIONS_PREFIX: Final = "noatime,nosuid,nodev,noexec"

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_UUID_RE = re.compile(r"[A-Za-z0-9-]{3,64}")
_SAFE_TEXT_RE = re.compile(r"[\x20-\x7e]{1,1024}")
_MOUNT: Final = "/usr/bin/mount"
_DD: Final = "/usr/bin/dd"
_SFDISK: Final = "/usr/sbin/sfdisk"
_BLOCKDEV: Final = "/usr/sbin/blockdev"
_E2FSCK: Final = "/usr/sbin/e2fsck"
_RESIZE2FS: Final = "/usr/sbin/resize2fs"
_WIPEFS: Final = "/usr/sbin/wipefs"
_MKFS_EXFAT: Final = "/usr/sbin/mkfs.exfat"
_BLKID: Final = "/usr/sbin/blkid"
_FSCK_EXFAT: Final = "/usr/sbin/fsck.exfat"
_FINDMNT: Final = "/usr/bin/findmnt"
_SYSTEMCTL: Final = "/usr/bin/systemctl"
_ALLOWED_PROGRAMS = frozenset(
    {
        _BLKID,
        _BLOCKDEV,
        _DD,
        _E2FSCK,
        _FINDMNT,
        _FSCK_EXFAT,
        _MKFS_EXFAT,
        _MOUNT,
        _RESIZE2FS,
        _SFDISK,
        _SYSTEMCTL,
        _WIPEFS,
    }
)


class FirstbootError(ValueError):
    """Base error for malformed or refused first-boot requests."""


class RefusalCode(StrEnum):
    EXECUTION_DISABLED = "execution_disabled"
    WRONG_STAGE = "wrong_stage"
    TRIGGER_MISSING = "trigger_missing"
    ROOT_RESOLUTION_FAILED = "root_resolution_failed"
    IDENTITY_CHANGED = "identity_changed"
    LAYOUT_REFUSED = "layout_refused"
    EXISTING_SIGNATURE = "existing_signature"
    EXISTING_DATA_PARTITION = "existing_data_partition"
    SHRINK_FORBIDDEN = "shrink_forbidden"
    BACKUP_REQUIRED = "backup_required"
    BACKUP_INVALID = "backup_invalid"
    MARKER_INCONSISTENT = "marker_inconsistent"
    MOUNT_INCONSISTENT = "mount_inconsistent"
    RETRY_LIMIT = "retry_limit"
    REBOOT_LIMIT = "reboot_limit"
    TRANSITION_NOT_OBSERVED = "transition_not_observed"
    COMMAND_FAILED = "command_failed"
    UNVERIFIED_RUNTIME = "unverified_runtime"


class FirstbootRefused(FirstbootError):
    """Stable fail-closed outcome."""

    def __init__(self, code: RefusalCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class RuntimeStage(StrEnum):
    INITRAMFS = "initramfs"
    POST_ROOT = "post_root"


class RuntimePhase(StrEnum):
    SOURCE_VERIFIED = "source_verified"
    BACKUP_VALIDATED = "backup_validated"
    TABLE_COMMITTED = "table_committed"
    ROOT_CHECKED = "root_checked"
    ROOT_READY = "root_ready"
    SIGNATURE_VERIFIED = "signature_verified"
    DATA_FORMATTED = "data_formatted"
    EARLY_COMPLETE = "early_complete"
    VOLUME_MOUNTED = "volume_mounted"
    SENTINEL_DURABLE = "sentinel_durable"
    COMPLETE = "complete"


class ActionKind(StrEnum):
    COMMAND = "command"
    DURABLE_COMMAND_OUTPUT = "durable_command_output"
    DURABLE_JSON = "durable_json"
    ENSURE_DIRECTORIES = "ensure_directories"
    UPDATE_FSTAB = "update_fstab"
    WRITE_ENV = "write_env"


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    def __post_init__(self) -> None:
        if len(self.stdout.encode()) > 256 * 1024 or len(self.stderr.encode()) > 64 * 1024:
            raise FirstbootError("executor output exceeds the runtime bound")


@dataclass(frozen=True, slots=True)
class RuntimeAction:
    kind: ActionKind
    description: str
    argv: tuple[str, ...] = ()
    stdin_text: str | None = None
    output_path: str | None = None
    accepted_returncodes: tuple[int, ...] = (0,)
    timeout_seconds: int = 30
    mutates_storage: bool = False

    def __post_init__(self) -> None:
        if not self.description or len(self.description) > 240:
            raise FirstbootError("action description is empty or too long")
        if not 1 <= self.timeout_seconds <= 120:
            raise FirstbootError("action timeout is outside 1..120 seconds")
        if len(self.argv) > MAX_ARGV_ITEMS:
            raise FirstbootError("action argv has too many items")
        if self.kind in {ActionKind.COMMAND, ActionKind.DURABLE_COMMAND_OUTPUT}:
            if not self.argv or self.argv[0] not in _ALLOWED_PROGRAMS:
                raise FirstbootError("command is not in the closed first-boot allowlist")
        elif self.argv:
            raise FirstbootError("built-in filesystem actions cannot carry argv")
        for argument in self.argv:
            if (
                len(argument.encode()) > MAX_ARGUMENT_BYTES
                or "\x00" in argument
                or _SAFE_TEXT_RE.fullmatch(argument) is None
            ):
                raise FirstbootError("command argument is unsafe or unbounded")
        if self.stdin_text is not None and (
            not self.stdin_text
            or len(self.stdin_text.encode()) > MAX_STDIN_BYTES
            or "\x00" in self.stdin_text
        ):
            raise FirstbootError("action input is unsafe or unbounded")
        if self.output_path is not None:
            _validated_runtime_path(self.output_path)
        if not self.accepted_returncodes or any(
            isinstance(code, bool) or not 0 <= code <= 255 for code in self.accepted_returncodes
        ):
            raise FirstbootError("accepted return codes are invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "description": self.description,
            "argv": list(self.argv),
            "stdin_text": self.stdin_text,
            "output_path": self.output_path,
            "accepted_returncodes": list(self.accepted_returncodes),
            "timeout_seconds": self.timeout_seconds,
            "mutates_storage": self.mutates_storage,
        }


Executor = Callable[[RuntimeAction], CommandResult]
JournalStore = Callable[["RuntimeJournal"], None]


@dataclass(frozen=True, slots=True)
class RuntimeEvidence:
    """Bounded evidence collected outside this pure model."""

    observation: DeviceObservation
    cmdline_tokens: tuple[str, ...]
    partuuid_devices: Mapping[str, str]
    fstab_root_partuuid: str
    fstab_boot_partuuid: str
    root_filesystem_size_bytes: int
    data_region_signatures: tuple[str, ...] = ()
    backup_sha256: str | None = None
    backup_validated: bool = False
    kernel_table_adopted: bool = False
    root_check_passed: bool = False
    signature_scan_clean: bool = False
    signature_scan_device: str | None = None
    signature_scan_table_fingerprint: str | None = None
    mounted_data_uuid: str | None = None
    mounted_data_fstype: str | None = None
    mounted_data_source: str | None = None
    mounted_data_options: tuple[str, ...] = ()
    dashcam_uid: int = 0
    dashcam_storage_gid: int = 0

    def __post_init__(self) -> None:
        if not 1 <= len(self.cmdline_tokens) <= 256:
            raise FirstbootError("kernel command line token count is invalid")
        if len(self.partuuid_devices) > 16:
            raise FirstbootError("too many PARTUUID resolution entries")
        if self.root_filesystem_size_bytes <= 0:
            raise FirstbootError("root filesystem size must be positive")
        if len(self.data_region_signatures) > 16:
            raise FirstbootError("too many data-region signatures")
        if self.backup_sha256 is not None and _SHA256_RE.fullmatch(self.backup_sha256) is None:
            raise FirstbootError("backup digest must be lowercase SHA-256")
        if self.signature_scan_device is not None:
            _validated_device_path(self.signature_scan_device)
        if (
            self.signature_scan_table_fingerprint is not None
            and _SHA256_RE.fullmatch(self.signature_scan_table_fingerprint) is None
        ):
            raise FirstbootError("signature scan fingerprint must be lowercase SHA-256")
        for value in (self.fstab_root_partuuid, self.fstab_boot_partuuid):
            if not value or len(value) > 64:
                raise FirstbootError("fstab PARTUUID is invalid")
        for partuuid, device in self.partuuid_devices.items():
            if len(partuuid) > 64:
                raise FirstbootError("PARTUUID resolution key is too long")
            _validated_device_path(device)
        for signature in self.data_region_signatures:
            if _SAFE_TEXT_RE.fullmatch(signature) is None:
                raise FirstbootError("data signature name is unsafe")
        for numeric_id in (self.dashcam_uid, self.dashcam_storage_gid):
            if isinstance(numeric_id, bool) or not 0 <= numeric_id <= 2**31 - 1:
                raise FirstbootError("mount ownership ID is invalid")


@dataclass(frozen=True, slots=True)
class RuntimeJournal:
    schema_version: int
    transaction_id: str
    phase: RuntimePhase
    source_identity: DeviceIdentity
    mbr_disk_id: str
    boot_uuid: str
    root_uuid: str
    boot_partuuid: str
    root_partuuid: str
    root_start_sector: int
    root_end_sector: int
    data_start_sector: int
    data_end_sector: int
    backup_sha256: str | None = None
    data_uuid: str | None = None
    consecutive_failures: int = 0
    reboot_count: int = 0
    last_error: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise FirstbootError("only first-boot journal version 1 is supported")
        if _SHA256_RE.fullmatch(self.transaction_id) is None:
            raise FirstbootError("transaction ID must be lowercase SHA-256")
        if not 0 <= self.consecutive_failures <= MAX_RETRIES:
            raise FirstbootError("journal failure count is invalid")
        if not 0 <= self.reboot_count <= MAX_REBOOTS:
            raise FirstbootError("journal reboot count is invalid")
        if self.backup_sha256 is not None and _SHA256_RE.fullmatch(self.backup_sha256) is None:
            raise FirstbootError("journal backup digest is invalid")
        if self.data_uuid is not None and _UUID_RE.fullmatch(self.data_uuid) is None:
            raise FirstbootError("journal recording UUID is invalid")
        if self.last_error is not None and len(self.last_error) > 256:
            raise FirstbootError("journal error is too long")
        if (
            self.root_start_sector < 0
            or self.root_end_sector < self.root_start_sector
            or self.data_start_sector <= self.root_end_sector
            or self.data_end_sector < self.data_start_sector
        ):
            raise FirstbootError("journal target geometry is invalid")

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["phase"] = self.phase.value
        return result


@dataclass(frozen=True, slots=True)
class RuntimePlan:
    stage: RuntimeStage
    phase: RuntimePhase
    next_phase: RuntimePhase
    journal: RuntimeJournal
    actions: tuple[RuntimeAction, ...]
    complete: bool
    commit_before_actions: bool = False
    precondition_table_fingerprint: str = ""

    def __post_init__(self) -> None:
        if len(self.actions) > MAX_ACTIONS:
            raise FirstbootError("runtime plan exceeds its action bound")
        if self.complete and self.actions:
            raise FirstbootError("complete plans cannot contain actions")

    def to_dict(self) -> dict[str, object]:
        return {
            "stage": self.stage.value,
            "phase": self.phase.value,
            "next_phase": self.next_phase.value,
            "journal": self.journal.to_dict(),
            "actions": [action.to_dict() for action in self.actions],
            "complete": self.complete,
            "commit_before_actions": self.commit_before_actions,
            "precondition_table_fingerprint": self.precondition_table_fingerprint,
        }


def evidence_from_mapping(raw: Mapping[str, object]) -> RuntimeEvidence:
    """Parse a closed JSON-compatible runtime evidence object."""

    table = _closed_mapping(
        raw,
        {
            "schema_version",
            "observation",
            "cmdline_tokens",
            "partuuid_devices",
            "fstab_root_partuuid",
            "fstab_boot_partuuid",
            "root_filesystem_size_bytes",
            "data_region_signatures",
            "backup_sha256",
            "backup_validated",
            "kernel_table_adopted",
            "root_check_passed",
            "signature_scan_clean",
            "signature_scan_device",
            "signature_scan_table_fingerprint",
            "mounted_data_uuid",
            "mounted_data_fstype",
            "mounted_data_source",
            "mounted_data_options",
            "dashcam_uid",
            "dashcam_storage_gid",
        },
        "evidence",
    )
    if _integer(table["schema_version"], "evidence.schema_version") != 1:
        raise FirstbootError("only runtime evidence version 1 is supported")
    observation_raw = table["observation"]
    if not isinstance(observation_raw, Mapping):
        raise FirstbootError("evidence.observation must be an object")
    links_raw = table["partuuid_devices"]
    if not isinstance(links_raw, Mapping) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in links_raw.items()
    ):
        raise FirstbootError("evidence.partuuid_devices must be a string mapping")
    return RuntimeEvidence(
        observation=observation_from_mapping(observation_raw),
        cmdline_tokens=_string_tuple(table["cmdline_tokens"], "evidence.cmdline_tokens"),
        partuuid_devices={str(key): str(value) for key, value in links_raw.items()},
        fstab_root_partuuid=_text(table["fstab_root_partuuid"], "evidence.fstab_root_partuuid"),
        fstab_boot_partuuid=_text(table["fstab_boot_partuuid"], "evidence.fstab_boot_partuuid"),
        root_filesystem_size_bytes=_integer(
            table["root_filesystem_size_bytes"], "evidence.root_filesystem_size_bytes"
        ),
        data_region_signatures=_string_tuple(
            table["data_region_signatures"], "evidence.data_region_signatures"
        ),
        backup_sha256=_optional_text(table["backup_sha256"], "evidence.backup_sha256"),
        backup_validated=_boolean(table["backup_validated"], "evidence.backup_validated"),
        kernel_table_adopted=_boolean(
            table["kernel_table_adopted"], "evidence.kernel_table_adopted"
        ),
        root_check_passed=_boolean(table["root_check_passed"], "evidence.root_check_passed"),
        signature_scan_clean=_boolean(
            table["signature_scan_clean"], "evidence.signature_scan_clean"
        ),
        signature_scan_device=_optional_text(
            table["signature_scan_device"], "evidence.signature_scan_device"
        ),
        signature_scan_table_fingerprint=_optional_text(
            table["signature_scan_table_fingerprint"],
            "evidence.signature_scan_table_fingerprint",
        ),
        mounted_data_uuid=_optional_text(table["mounted_data_uuid"], "evidence.mounted_data_uuid"),
        mounted_data_fstype=_optional_text(
            table["mounted_data_fstype"], "evidence.mounted_data_fstype"
        ),
        mounted_data_source=_optional_text(
            table["mounted_data_source"], "evidence.mounted_data_source"
        ),
        mounted_data_options=_string_tuple(
            table["mounted_data_options"], "evidence.mounted_data_options"
        ),
        dashcam_uid=_integer(table["dashcam_uid"], "evidence.dashcam_uid"),
        dashcam_storage_gid=_integer(table["dashcam_storage_gid"], "evidence.dashcam_storage_gid"),
    )


def journal_from_mapping(raw: Mapping[str, object]) -> RuntimeJournal:
    """Parse a closed JSON-compatible durable journal."""

    table = _closed_mapping(
        raw,
        {
            "schema_version",
            "transaction_id",
            "phase",
            "source_identity",
            "mbr_disk_id",
            "boot_uuid",
            "root_uuid",
            "boot_partuuid",
            "root_partuuid",
            "root_start_sector",
            "root_end_sector",
            "data_start_sector",
            "data_end_sector",
            "backup_sha256",
            "data_uuid",
            "consecutive_failures",
            "reboot_count",
            "last_error",
        },
        "journal",
    )
    identity_raw = _closed_mapping(
        _mapping(table["source_identity"], "journal.source_identity"),
        {"resolved_path", "serial", "size_bytes", "partition_table_fingerprint"},
        "journal.source_identity",
    )
    try:
        phase = RuntimePhase(_text(table["phase"], "journal.phase"))
    except ValueError as exc:
        raise FirstbootError("journal.phase is unknown") from exc
    return RuntimeJournal(
        schema_version=_integer(table["schema_version"], "journal.schema_version"),
        transaction_id=_text(table["transaction_id"], "journal.transaction_id"),
        phase=phase,
        source_identity=DeviceIdentity(
            resolved_path=_text(
                identity_raw["resolved_path"], "journal.source_identity.resolved_path"
            ),
            serial=_text(identity_raw["serial"], "journal.source_identity.serial"),
            size_bytes=_integer(identity_raw["size_bytes"], "journal.source_identity.size_bytes"),
            partition_table_fingerprint=_text(
                identity_raw["partition_table_fingerprint"],
                "journal.source_identity.partition_table_fingerprint",
            ),
        ),
        mbr_disk_id=_text(table["mbr_disk_id"], "journal.mbr_disk_id"),
        boot_uuid=_text(table["boot_uuid"], "journal.boot_uuid"),
        root_uuid=_text(table["root_uuid"], "journal.root_uuid"),
        boot_partuuid=_text(table["boot_partuuid"], "journal.boot_partuuid"),
        root_partuuid=_text(table["root_partuuid"], "journal.root_partuuid"),
        root_start_sector=_integer(table["root_start_sector"], "journal.root_start_sector"),
        root_end_sector=_integer(table["root_end_sector"], "journal.root_end_sector"),
        data_start_sector=_integer(table["data_start_sector"], "journal.data_start_sector"),
        data_end_sector=_integer(table["data_end_sector"], "journal.data_end_sector"),
        backup_sha256=_optional_text(table["backup_sha256"], "journal.backup_sha256"),
        data_uuid=_optional_text(table["data_uuid"], "journal.data_uuid"),
        consecutive_failures=_integer(
            table["consecutive_failures"], "journal.consecutive_failures"
        ),
        reboot_count=_integer(table["reboot_count"], "journal.reboot_count"),
        last_error=_optional_text(table["last_error"], "journal.last_error"),
    )


def start_journal(
    spec: LayoutSpec,
    evidence: RuntimeEvidence,
    expected_identity: DeviceIdentity,
) -> RuntimeJournal:
    """Validate an exact source image/card identity and start a transaction."""

    observed = evidence.observation
    if observed.identity != expected_identity:
        _refuse(RefusalCode.IDENTITY_CHANGED, "source path/CID/size/table identity changed")
    computed = _validate_common(spec, evidence, None)
    state, _ = _classify_layout(spec, evidence, computed)
    if state != "source":
        _refuse(RefusalCode.LAYOUT_REFUSED, "a new transaction requires the exact source layout")
    boot, root = _boot_root(spec, observed)
    assert observed.mbr_disk_id is not None
    payload = (
        f"{observed.identity.serial}\0{observed.identity.size_bytes}\0"
        f"{observed.identity.partition_table_fingerprint}\0{observed.mbr_disk_id}"
    ).encode()
    return RuntimeJournal(
        schema_version=1,
        transaction_id=hashlib.sha256(payload).hexdigest(),
        phase=RuntimePhase.SOURCE_VERIFIED,
        source_identity=observed.identity,
        mbr_disk_id=observed.mbr_disk_id,
        boot_uuid=_required_uuid(boot, "boot"),
        root_uuid=_required_uuid(root, "root"),
        boot_partuuid=_required_partuuid(boot, "boot"),
        root_partuuid=_required_partuuid(root, "root"),
        root_start_sector=computed.root_start_sector,
        root_end_sector=computed.root_end_sector,
        data_start_sector=computed.data_start_sector,
        data_end_sector=computed.data_end_sector,
    )


def plan_next(
    spec: LayoutSpec,
    stage: RuntimeStage,
    evidence: RuntimeEvidence,
    journal: RuntimeJournal,
) -> RuntimePlan:
    """Reconcile observable state and return one bounded, restartable phase."""

    computed = _validate_common(spec, evidence, journal)
    state, data = _classify_layout(spec, evidence, computed)
    current = _reconcile_observed_phase(spec, evidence, journal, state, data)
    if current.consecutive_failures >= MAX_RETRIES:
        _refuse(RefusalCode.RETRY_LIMIT, "the bounded retry count is exhausted")

    if stage is RuntimeStage.INITRAMFS:
        result = _plan_initramfs(spec, evidence, current, state, data)
    else:
        result = _plan_post_root(spec, evidence, current, state, data)
    return replace(
        result,
        precondition_table_fingerprint=evidence.observation.identity.partition_table_fingerprint,
    )


def record_failure(journal: RuntimeJournal, message: str) -> RuntimeJournal:
    """Persist a bounded failure without advancing the transaction."""

    if journal.consecutive_failures >= MAX_RETRIES:
        _refuse(RefusalCode.RETRY_LIMIT, "the bounded retry count is already exhausted")
    clean = " ".join(message.split())[:256] or "unspecified failure"
    return replace(
        journal,
        consecutive_failures=journal.consecutive_failures + 1,
        last_error=clean,
    )


def apply_observed_success(
    spec: LayoutSpec,
    plan: RuntimePlan,
    evidence_after: RuntimeEvidence,
) -> RuntimeJournal:
    """Advance only after the planned postcondition is independently observed."""

    computed = _validate_common(spec, evidence_after, plan.journal)
    state, data = _classify_layout(spec, evidence_after, computed)
    candidate = _reconcile_observed_phase(spec, evidence_after, plan.journal, state, data)
    required = plan.next_phase
    if required is RuntimePhase.BACKUP_VALIDATED:
        if not evidence_after.backup_validated or evidence_after.backup_sha256 is None:
            _refuse(RefusalCode.BACKUP_INVALID, "the durable backup was not read-back validated")
        candidate = replace(
            candidate,
            phase=required,
            backup_sha256=evidence_after.backup_sha256,
        )
    elif required is RuntimePhase.ROOT_CHECKED:
        if not evidence_after.root_check_passed:
            _refuse(RefusalCode.TRANSITION_NOT_OBSERVED, "bounded ext4 check did not pass")
        candidate = replace(candidate, phase=required)
    elif required is RuntimePhase.SIGNATURE_VERIFIED:
        _validate_signature_scan(spec, evidence_after, plan.journal, state, data)
        candidate = replace(candidate, phase=required)
    elif required is RuntimePhase.EARLY_COMPLETE and not plan.actions:
        candidate = replace(candidate, phase=required)
    elif _phase_rank(candidate.phase) < _phase_rank(required):
        _refuse(
            RefusalCode.TRANSITION_NOT_OBSERVED,
            f"postcondition for {required.value} was not observed",
        )
    if required is RuntimePhase.DATA_FORMATTED:
        if data is None or data.uuid is None:
            _refuse(RefusalCode.TRANSITION_NOT_OBSERVED, "recording UUID was not observed")
        candidate = replace(candidate, data_uuid=data.uuid)
    return replace(candidate, consecutive_failures=0, last_error=None)


def execute_plan(
    plan: RuntimePlan,
    *,
    spec: LayoutSpec | None = None,
    executor: Executor | None = None,
    journal_store: JournalStore | None = None,
    identity_recheck: Callable[[], RuntimeEvidence] | None = None,
    execution_enabled: bool = False,
) -> tuple[CommandResult, ...]:
    """Execute a validated plan only behind an explicit injected gate.

    The checked-in CLI and deploy candidates do not enable this path.  Built-in
    filesystem actions are deliberately left to a reviewed executor so tests can
    exercise ordering without this module writing the host filesystem.
    """

    if (
        not execution_enabled
        or executor is None
        or journal_store is None
        or identity_recheck is None
        or spec is None
    ):
        _refuse(RefusalCode.EXECUTION_DISABLED, "first-boot execution is disabled")
    if _SHA256_RE.fullmatch(plan.precondition_table_fingerprint) is None:
        _refuse(RefusalCode.IDENTITY_CHANGED, "plan lacks a bound table fingerprint")
    if plan.commit_before_actions:
        journal_store(plan.journal)
    results: list[CommandResult] = []
    for action in plan.actions:
        if action.mutates_storage:
            fresh = identity_recheck()
            _validate_common(spec, fresh, plan.journal)
            if (
                fresh.observation.identity.partition_table_fingerprint
                != plan.precondition_table_fingerprint
            ):
                _refuse(
                    RefusalCode.IDENTITY_CHANGED,
                    "partition identity changed at the destructive executor boundary",
                )
        result = executor(action)
        results.append(result)
        if result.returncode not in action.accepted_returncodes:
            _refuse(
                RefusalCode.COMMAND_FAILED,
                f"{action.description} returned {result.returncode}",
            )
    return tuple(results)


def _plan_initramfs(
    spec: LayoutSpec,
    evidence: RuntimeEvidence,
    journal: RuntimeJournal,
    state: str,
    data: PartitionObservation | None,
) -> RuntimePlan:
    phase = journal.phase
    if _phase_rank(phase) >= _phase_rank(RuntimePhase.EARLY_COMPLETE):
        return RuntimePlan(
            RuntimeStage.INITRAMFS,
            phase,
            phase,
            journal,
            (),
            True,
        )
    if phase is RuntimePhase.SOURCE_VERIFIED:
        if state != "source":
            _refuse(RefusalCode.BACKUP_REQUIRED, "table changed before a validated backup")
        backup_dir = f"/run/dashcam-firstboot/boot/dashcam-provision/{journal.transaction_id}"
        device = evidence.observation.identity.resolved_path
        backup_actions = (
            RuntimeAction(
                ActionKind.COMMAND,
                "Mount only the identity-verified boot filesystem for durable backup.",
                (
                    _MOUNT,
                    "-t",
                    "vfat",
                    "-o",
                    "rw,nosuid,nodev,noexec,umask=0077",
                    f"PARTUUID={journal.boot_partuuid}",
                    "/run/dashcam-firstboot/boot",
                ),
                mutates_storage=True,
            ),
            RuntimeAction(
                ActionKind.DURABLE_COMMAND_OUTPUT,
                "Save, fsync, read back, and digest the complete DOS/MBR table dump.",
                (_SFDISK, "--dump", device),
                output_path=f"{backup_dir}/partition-table.sfdisk",
            ),
            RuntimeAction(
                ActionKind.COMMAND,
                "Save the first sector containing the MBR and partition entries.",
                (
                    _DD,
                    f"if={device}",
                    f"of={backup_dir}/first-sector.bin",
                    "bs=512",
                    "count=1",
                    "conv=fsync",
                    "status=none",
                ),
            ),
        )
        return RuntimePlan(
            RuntimeStage.INITRAMFS,
            phase,
            RuntimePhase.BACKUP_VALIDATED,
            journal,
            backup_actions,
            False,
        )
    if phase is RuntimePhase.BACKUP_VALIDATED:
        if not evidence.backup_validated or evidence.backup_sha256 != journal.backup_sha256:
            _refuse(RefusalCode.BACKUP_INVALID, "validated backup identity is absent or changed")
        if state != "source":
            _refuse(RefusalCode.LAYOUT_REFUSED, "pre-write source layout changed")
        device = evidence.observation.identity.resolved_path
        table_input = _sfdisk_input(spec, evidence.observation, journal)
        table_actions = (
            RuntimeAction(
                ActionKind.COMMAND,
                "Write the complete bounded DOS/MBR table while preserving its disk ID.",
                (_SFDISK, "--no-reread", "--force", device),
                stdin_text=table_input,
                mutates_storage=True,
            ),
            RuntimeAction(
                ActionKind.COMMAND,
                "Verify the just-written partition table.",
                (_SFDISK, "--verify", device),
            ),
            RuntimeAction(
                ActionKind.COMMAND,
                "Request a kernel partition-table reread.",
                (_BLOCKDEV, "--rereadpt", device),
            ),
        )
        return RuntimePlan(
            RuntimeStage.INITRAMFS,
            phase,
            RuntimePhase.TABLE_COMMITTED,
            journal,
            table_actions,
            False,
        )
    if phase is RuntimePhase.TABLE_COMMITTED:
        if state not in {"table", "formatted"}:
            _refuse(RefusalCode.LAYOUT_REFUSED, "target table is not exact")
        if not evidence.kernel_table_adopted:
            if journal.reboot_count >= MAX_REBOOTS:
                _refuse(RefusalCode.REBOOT_LIMIT, "kernel table reread failed after one reboot")
            committed = replace(journal, reboot_count=journal.reboot_count + 1)
            return RuntimePlan(
                RuntimeStage.INITRAMFS,
                phase,
                phase,
                committed,
                (
                    RuntimeAction(
                        ActionKind.COMMAND,
                        "Perform the single permitted controlled reboot after journaling it.",
                        (_SYSTEMCTL, "reboot"),
                        mutates_storage=True,
                    ),
                ),
                False,
                commit_before_actions=True,
            )
        root = _partition_path(evidence.observation.identity.resolved_path, spec.root.number)
        root_check_actions = (
            RuntimeAction(
                ActionKind.COMMAND,
                "Run bounded automatic ext4 checking on the unmounted root filesystem.",
                (_E2FSCK, "-f", "-p", root),
                accepted_returncodes=(0, 1),
                timeout_seconds=120,
                mutates_storage=True,
            ),
        )
        return RuntimePlan(
            RuntimeStage.INITRAMFS,
            phase,
            RuntimePhase.ROOT_CHECKED,
            journal,
            root_check_actions,
            False,
        )
    if phase is RuntimePhase.ROOT_CHECKED:
        if state not in {"table", "formatted"} or not evidence.root_check_passed:
            _refuse(RefusalCode.LAYOUT_REFUSED, "ext4 check evidence is absent or stale")
        root = _partition_path(evidence.observation.identity.resolved_path, spec.root.number)
        grow_actions = (
            RuntimeAction(
                ActionKind.COMMAND,
                "Grow ext4 to the exact six-GiB partition boundary; never shrink.",
                (_RESIZE2FS, root),
                timeout_seconds=120,
                mutates_storage=True,
            ),
        )
        return RuntimePlan(
            RuntimeStage.INITRAMFS,
            phase,
            RuntimePhase.ROOT_READY,
            journal,
            grow_actions,
            False,
        )
    if phase is RuntimePhase.ROOT_READY:
        if state == "formatted":
            assert data is not None and data.uuid is not None
            reconciled = replace(journal, phase=RuntimePhase.DATA_FORMATTED, data_uuid=data.uuid)
            return _plan_initramfs(spec, evidence, reconciled, state, data)
        if state != "table" or data is None:
            _refuse(RefusalCode.LAYOUT_REFUSED, "new partition 3 is not the exact blank target")
        if evidence.data_region_signatures or data.has_data_signature:
            _refuse(RefusalCode.EXISTING_SIGNATURE, "partition 3 contains a data signature")
        data_path = _partition_path(evidence.observation.identity.resolved_path, spec.data.number)
        signature_actions = (
            RuntimeAction(
                ActionKind.COMMAND,
                "Produce structured signature evidence for the exact new partition.",
                (_WIPEFS, "--json", "--no-act", data_path),
            ),
        )
        return RuntimePlan(
            RuntimeStage.INITRAMFS,
            phase,
            RuntimePhase.SIGNATURE_VERIFIED,
            journal,
            signature_actions,
            False,
        )
    if phase is RuntimePhase.SIGNATURE_VERIFIED:
        _validate_signature_scan(spec, evidence, journal, state, data)
        data_path = _partition_path(evidence.observation.identity.resolved_path, spec.data.number)
        format_actions = (
            RuntimeAction(
                ActionKind.COMMAND,
                "Format only the verified-new signature-free partition as DASHCAM exFAT.",
                (_MKFS_EXFAT, "-n", spec.data.label, data_path),
                timeout_seconds=120,
                mutates_storage=True,
            ),
            RuntimeAction(
                ActionKind.COMMAND,
                "Capture the new exFAT UUID.",
                (_BLKID, "--output", "value", "--match-tag", "UUID", data_path),
            ),
        )
        return RuntimePlan(
            RuntimeStage.INITRAMFS,
            phase,
            RuntimePhase.DATA_FORMATTED,
            journal,
            format_actions,
            False,
        )
    if phase is RuntimePhase.DATA_FORMATTED:
        if state != "formatted" or data is None or data.uuid != journal.data_uuid:
            _refuse(RefusalCode.LAYOUT_REFUSED, "formatted data identity changed")
        return RuntimePlan(
            RuntimeStage.INITRAMFS,
            phase,
            RuntimePhase.EARLY_COMPLETE,
            journal,
            (),
            False,
        )
    _refuse(RefusalCode.WRONG_STAGE, f"{phase.value} is not an initramfs phase")


def _plan_post_root(
    spec: LayoutSpec,
    evidence: RuntimeEvidence,
    journal: RuntimeJournal,
    state: str,
    data: PartitionObservation | None,
) -> RuntimePlan:
    phase = journal.phase
    if _phase_rank(phase) < _phase_rank(RuntimePhase.EARLY_COMPLETE):
        _refuse(RefusalCode.WRONG_STAGE, "post-root cannot run before early completion")
    if state != "formatted" or data is None or data.uuid != journal.data_uuid:
        _refuse(RefusalCode.LAYOUT_REFUSED, "post-root recording identity is not exact")
    if phase is RuntimePhase.COMPLETE:
        return RuntimePlan(RuntimeStage.POST_ROOT, phase, phase, journal, (), True)
    mount_options = _mount_options(evidence)
    data_path = _partition_path(evidence.observation.identity.resolved_path, spec.data.number)
    if phase is RuntimePhase.EARLY_COMPLETE:
        if _mounted_exactly(evidence, journal):
            reconciled = replace(journal, phase=RuntimePhase.VOLUME_MOUNTED)
            return _plan_post_root(spec, evidence, reconciled, state, data)
        mount_actions = (
            RuntimeAction(
                ActionKind.COMMAND,
                "Run a bounded non-destructive exFAT check before the first mount.",
                (_FSCK_EXFAT, "-n", data_path),
                accepted_returncodes=(0, 1),
                timeout_seconds=120,
            ),
            RuntimeAction(
                ActionKind.COMMAND,
                "Mount the recording filesystem by UUID with the closed option set.",
                (
                    _MOUNT,
                    "-t",
                    "exfat",
                    "-o",
                    mount_options,
                    f"UUID={journal.data_uuid}",
                    "/srv/dashcam",
                ),
                mutates_storage=True,
            ),
            RuntimeAction(
                ActionKind.COMMAND,
                "Verify the mounted source, filesystem type, and target.",
                (
                    _FINDMNT,
                    "--noheadings",
                    "--output",
                    "SOURCE,FSTYPE,TARGET,OPTIONS",
                    "--target",
                    "/srv/dashcam",
                ),
            ),
        )
        return RuntimePlan(
            RuntimeStage.POST_ROOT,
            phase,
            RuntimePhase.VOLUME_MOUNTED,
            journal,
            mount_actions,
            False,
        )
    payload = _marker_payload(journal)
    if phase is RuntimePhase.VOLUME_MOUNTED:
        if not _mounted_exactly(evidence, journal):
            _refuse(RefusalCode.MOUNT_INCONSISTENT, "recording mount is absent or mismatched")
        sentinel_actions = (
            RuntimeAction(
                ActionKind.ENSURE_DIRECTORIES,
                "Create only the closed recording directory set on the verified mount.",
                stdin_text=json.dumps(
                    [
                        "/srv/dashcam/pending",
                        "/srv/dashcam/clips",
                        "/srv/dashcam/protected",
                        "/srv/dashcam/quarantine",
                    ],
                    separators=(",", ":"),
                ),
                mutates_storage=True,
            ),
            RuntimeAction(
                ActionKind.DURABLE_JSON,
                "Atomically write and fsync the exFAT identity sentinel and its directory.",
                stdin_text=payload,
                output_path=f"/srv/dashcam/{spec.volume_sentinel_name}",
                mutates_storage=True,
            ),
        )
        return RuntimePlan(
            RuntimeStage.POST_ROOT,
            phase,
            RuntimePhase.SENTINEL_DURABLE,
            journal,
            sentinel_actions,
            False,
        )
    if phase is RuntimePhase.SENTINEL_DURABLE:
        _validate_marker_state(spec, evidence, journal, require_sentinel=True)
        fstab_line = (
            f"UUID={journal.data_uuid} /srv/dashcam exfat {_mount_options(evidence)},nofail 0 0\n"
        )
        completion_actions = (
            RuntimeAction(
                ActionKind.UPDATE_FSTAB,
                "Idempotently install the single UUID-bound DASHCAM fstab entry.",
                stdin_text=fstab_line,
                output_path="/etc/fstab",
                mutates_storage=True,
            ),
            RuntimeAction(
                ActionKind.WRITE_ENV,
                "Durably persist the expected recording UUID for recorder preflight.",
                stdin_text=f"DASHCAM_STORAGE_UUID={journal.data_uuid}\n",
                output_path="/etc/dashcam/storage-volume.env",
                mutates_storage=True,
            ),
            RuntimeAction(
                ActionKind.DURABLE_JSON,
                "Atomically write and fsync the ext4 completion marker last.",
                stdin_text=payload,
                output_path="/var/lib/dashcam/provisioning/layout-v1.complete.json",
                mutates_storage=True,
            ),
        )
        return RuntimePlan(
            RuntimeStage.POST_ROOT,
            phase,
            RuntimePhase.COMPLETE,
            journal,
            completion_actions,
            False,
        )
    _refuse(RefusalCode.WRONG_STAGE, f"{phase.value} is not a post-root phase")


def _validate_common(
    spec: LayoutSpec,
    evidence: RuntimeEvidence,
    journal: RuntimeJournal | None,
) -> ComputedLayout:
    observed = evidence.observation
    if evidence.cmdline_tokens.count(ROOT_TRIGGER) != 1:
        _refuse(RefusalCode.TRIGGER_MISSING, "exactly one bounded-provision trigger is required")
    root_values = [
        token.removeprefix("root=PARTUUID=")
        for token in evidence.cmdline_tokens
        if token.startswith("root=PARTUUID=")
    ]
    if len(root_values) != 1:
        _refuse(RefusalCode.ROOT_RESOLUTION_FAILED, "one root=PARTUUID reference is required")
    root_partuuid = root_values[0]
    root_device = evidence.partuuid_devices.get(root_partuuid)
    if root_device is None:
        _refuse(RefusalCode.ROOT_RESOLUTION_FAILED, "root PARTUUID did not resolve")
    expected_root_device = _partition_path(observed.identity.resolved_path, spec.root.number)
    if root_device != expected_root_device:
        _refuse(RefusalCode.ROOT_RESOLUTION_FAILED, "root PARTUUID resolved to another disk")
    if observed.mbr_disk_id is None:
        _refuse(RefusalCode.LAYOUT_REFUSED, "DOS/MBR disk ID is required")
    expected_boot_partuuid = mbr_partuuid(observed.mbr_disk_id, spec.boot.number)
    expected_root_partuuid = mbr_partuuid(observed.mbr_disk_id, spec.root.number)
    if (
        root_partuuid != expected_root_partuuid
        or evidence.fstab_root_partuuid != expected_root_partuuid
        or evidence.fstab_boot_partuuid != expected_boot_partuuid
    ):
        _refuse(RefusalCode.IDENTITY_CHANGED, "boot/root PARTUUID references are inconsistent")
    if (
        observed.table_type != "dos"
        or observed.sector_size_bytes != 512
        or not observed.device_path_is_resolved
        or observed.identity.size_bytes != observed.total_sectors * 512
    ):
        _refuse(RefusalCode.LAYOUT_REFUSED, "device is not the exact resolved 512-byte DOS disk")
    actual_fingerprint = fingerprint_partition_table(
        table_type=observed.table_type,
        mbr_disk_id=observed.mbr_disk_id,
        sector_size_bytes=observed.sector_size_bytes,
        partitions=observed.partitions,
    )
    if actual_fingerprint != observed.identity.partition_table_fingerprint:
        _refuse(RefusalCode.IDENTITY_CHANGED, "observed table fingerprint is not authentic")
    if observed.unpartitioned_data_signatures or evidence.data_region_signatures:
        _refuse(RefusalCode.EXISTING_SIGNATURE, "target free space contains a signature")
    if observed.identity.size_bytes < spec.minimum_device_bytes:
        _refuse(RefusalCode.LAYOUT_REFUSED, "device is below minimum capacity")
    if observed.is_system_disk and not observed.is_root_disk:
        _refuse(RefusalCode.LAYOUT_REFUSED, "ambiguous system/root disk evidence")
    try:
        computed = compute_layout(spec, observed)
    except LayoutError as exc:
        raise FirstbootRefused(RefusalCode.LAYOUT_REFUSED, str(exc)) from exc
    if journal is not None and (
        observed.identity.resolved_path != journal.source_identity.resolved_path
        or observed.identity.serial != journal.source_identity.serial
        or observed.identity.size_bytes != journal.source_identity.size_bytes
        or observed.mbr_disk_id != journal.mbr_disk_id
        or computed.root_start_sector != journal.root_start_sector
        or computed.root_end_sector != journal.root_end_sector
        or computed.data_start_sector != journal.data_start_sector
        or computed.data_end_sector != journal.data_end_sector
    ):
        _refuse(RefusalCode.IDENTITY_CHANGED, "transaction-bound device identity changed")
    return computed


def _classify_layout(
    spec: LayoutSpec,
    evidence: RuntimeEvidence,
    computed: ComputedLayout,
) -> tuple[str, PartitionObservation | None]:
    observed = evidence.observation
    numbers = [partition.number for partition in observed.partitions]
    if len(numbers) != len(set(numbers)) or any(number not in {1, 2, 3} for number in numbers):
        _refuse(RefusalCode.LAYOUT_REFUSED, "only exact p1/p2 or p1/p2/p3 layouts are accepted")
    by_number = {partition.number: partition for partition in observed.partitions}
    if set(by_number) not in ({1, 2}, {1, 2, 3}):
        _refuse(RefusalCode.LAYOUT_REFUSED, "p1 and p2 are required and p4 must be absent")
    boot, root = _boot_root(spec, observed)
    expected_boot_partuuid = mbr_partuuid(observed.mbr_disk_id or "", spec.boot.number)
    expected_root_partuuid = mbr_partuuid(observed.mbr_disk_id or "", spec.root.number)
    if (
        boot.start_sector != spec.boot.source_start_sector
        or boot.size_sectors != spec.boot.source_size_sectors
        or boot.partition_type != spec.boot.partition_type
        or boot.bootable is not spec.boot.bootable
        or boot.filesystem not in {"vfat", "fat32"}
        or boot.label != spec.boot.label
        or boot.uuid != spec.boot.filesystem_uuid
        or boot.partuuid != expected_boot_partuuid
        or boot.has_data_signature
        or root.start_sector != spec.root.source_start_sector
        or root.partition_type != spec.root.partition_type
        or root.bootable is not spec.root.bootable
        or root.filesystem != spec.root.filesystem
        or root.label != spec.root.label
        or root.uuid != spec.root.filesystem_uuid
        or root.partuuid != expected_root_partuuid
        or root.has_data_signature
    ):
        _refuse(RefusalCode.IDENTITY_CHANGED, "p1/p2 image identities are not preserved")
    if root.end_sector > computed.root_end_sector:
        _refuse(RefusalCode.SHRINK_FORBIDDEN, "root partition exceeds six GiB; never shrink")
    if evidence.root_filesystem_size_bytes > (root.size_sectors * 512):
        _refuse(RefusalCode.SHRINK_FORBIDDEN, "ext4 is larger than its partition")
    data = by_number.get(spec.data.number)
    if data is None:
        if root.size_sectors != spec.root.source_size_sectors:
            _refuse(
                RefusalCode.IDENTITY_CHANGED,
                "source root size differs from the exact selected image",
            )
        if root.end_sector == computed.root_end_sector:
            _refuse(RefusalCode.LAYOUT_REFUSED, "target-sized p2 without p3 is a partial table")
        return "source", None
    if (
        root.end_sector != computed.root_end_sector
        or data.start_sector != computed.data_start_sector
        or data.end_sector != computed.data_end_sector
        or data.partition_type != spec.data.partition_type
        or data.bootable is not spec.data.bootable
        or data.partuuid != mbr_partuuid(observed.mbr_disk_id or "", spec.data.number)
    ):
        _refuse(
            RefusalCode.EXISTING_DATA_PARTITION,
            "existing p3 is not the exact transaction target",
        )
    if data.filesystem is None and data.label is None and data.uuid is None:
        if data.has_data_signature:
            _refuse(RefusalCode.EXISTING_SIGNATURE, "new p3 contains a signature")
        return "table", data
    if (
        data.filesystem == spec.data.filesystem
        and data.label == spec.data.label
        and data.uuid is not None
        and _UUID_RE.fullmatch(data.uuid) is not None
        and not data.has_data_signature
    ):
        return "formatted", data
    _refuse(RefusalCode.EXISTING_DATA_PARTITION, "existing p3 filesystem identity is foreign")


def _reconcile_observed_phase(
    spec: LayoutSpec,
    evidence: RuntimeEvidence,
    journal: RuntimeJournal,
    state: str,
    data: PartitionObservation | None,
) -> RuntimeJournal:
    current = journal
    if state in {"table", "formatted"}:
        if current.backup_sha256 is None:
            _refuse(RefusalCode.BACKUP_REQUIRED, "target table exists without a validated backup")
        if evidence.backup_sha256 != current.backup_sha256 or not evidence.backup_validated:
            _refuse(RefusalCode.BACKUP_INVALID, "backup cannot be validated during recovery")
        if _phase_rank(current.phase) < _phase_rank(RuntimePhase.TABLE_COMMITTED):
            current = replace(current, phase=RuntimePhase.TABLE_COMMITTED)
    if (
        evidence.root_check_passed
        and state in {"table", "formatted"}
        and _phase_rank(current.phase) < _phase_rank(RuntimePhase.ROOT_CHECKED)
    ):
        current = replace(current, phase=RuntimePhase.ROOT_CHECKED)
    root_target_bytes = (journal.root_end_sector - journal.root_start_sector + 1) * 512
    if evidence.root_filesystem_size_bytes == root_target_bytes and _phase_rank(
        current.phase
    ) < _phase_rank(RuntimePhase.ROOT_READY):
        if state not in {"table", "formatted"}:
            _refuse(RefusalCode.LAYOUT_REFUSED, "ext4 target size without target table")
        current = replace(current, phase=RuntimePhase.ROOT_READY)
    if (
        state == "table"
        and evidence.signature_scan_clean
        and _phase_rank(current.phase) >= _phase_rank(RuntimePhase.ROOT_READY)
        and _phase_rank(current.phase) < _phase_rank(RuntimePhase.SIGNATURE_VERIFIED)
    ):
        _validate_signature_scan(spec, evidence, current, state, data)
        current = replace(current, phase=RuntimePhase.SIGNATURE_VERIFIED)
    if state == "formatted":
        assert data is not None and data.uuid is not None
        if current.data_uuid is not None and current.data_uuid != data.uuid:
            _refuse(RefusalCode.IDENTITY_CHANGED, "recording filesystem UUID changed")
        if _phase_rank(current.phase) < _phase_rank(RuntimePhase.DATA_FORMATTED):
            current = replace(
                current,
                phase=RuntimePhase.DATA_FORMATTED,
                data_uuid=data.uuid,
            )
    sentinel_present = any(
        value is not None
        for value in (
            evidence.observation.volume_sentinel_layout_version,
            evidence.observation.volume_sentinel_serial,
            evidence.observation.volume_sentinel_uuid,
            evidence.observation.volume_sentinel_source_table_fingerprint,
        )
    )
    marker_present = any(
        value is not None
        for value in (
            evidence.observation.state_marker_layout_version,
            evidence.observation.state_marker_serial,
            evidence.observation.state_marker_uuid,
            evidence.observation.state_marker_source_table_fingerprint,
        )
    )
    if sentinel_present or marker_present:
        _validate_marker_state(spec, evidence, current, require_sentinel=sentinel_present)
    if sentinel_present and _phase_rank(current.phase) < _phase_rank(RuntimePhase.SENTINEL_DURABLE):
        current = replace(current, phase=RuntimePhase.SENTINEL_DURABLE)
    if marker_present:
        if not sentinel_present:
            _refuse(RefusalCode.MARKER_INCONSISTENT, "ext4 marker exists without exFAT sentinel")
        current = replace(current, phase=RuntimePhase.COMPLETE)
    if _mounted_exactly(evidence, current) and _phase_rank(current.phase) < _phase_rank(
        RuntimePhase.VOLUME_MOUNTED
    ):
        current = replace(current, phase=RuntimePhase.VOLUME_MOUNTED)
    return current


def _validate_marker_state(
    spec: LayoutSpec,
    evidence: RuntimeEvidence,
    journal: RuntimeJournal,
    *,
    require_sentinel: bool,
) -> None:
    observed = evidence.observation
    expected = (
        spec.schema_version,
        journal.source_identity.serial,
        journal.data_uuid,
        journal.source_identity.partition_table_fingerprint,
    )
    sentinel = (
        observed.volume_sentinel_layout_version,
        observed.volume_sentinel_serial,
        observed.volume_sentinel_uuid,
        observed.volume_sentinel_source_table_fingerprint,
    )
    marker = (
        observed.state_marker_layout_version,
        observed.state_marker_serial,
        observed.state_marker_uuid,
        observed.state_marker_source_table_fingerprint,
    )
    if require_sentinel and sentinel != expected:
        _refuse(RefusalCode.MARKER_INCONSISTENT, "volume sentinel does not bind the transaction")
    if observed.state_marker_layout_version is not None and marker != expected:
        _refuse(RefusalCode.MARKER_INCONSISTENT, "ext4 marker does not bind the transaction")


def _validate_signature_scan(
    spec: LayoutSpec,
    evidence: RuntimeEvidence,
    journal: RuntimeJournal,
    state: str,
    data: PartitionObservation | None,
) -> None:
    expected_device = _partition_path(
        evidence.observation.identity.resolved_path,
        spec.data.number,
    )
    if (
        state != "table"
        or data is None
        or data.has_data_signature
        or evidence.data_region_signatures
        or not evidence.signature_scan_clean
        or evidence.signature_scan_device != expected_device
        or evidence.signature_scan_table_fingerprint
        != evidence.observation.identity.partition_table_fingerprint
        or journal.backup_sha256 is None
    ):
        _refuse(
            RefusalCode.EXISTING_SIGNATURE,
            "a clean structured signature scan bound to the current p3/table is required",
        )


def _mounted_exactly(evidence: RuntimeEvidence, journal: RuntimeJournal) -> bool:
    supplied = (
        evidence.mounted_data_uuid,
        evidence.mounted_data_fstype,
        evidence.mounted_data_source,
    )
    if supplied == (None, None, None):
        return False
    expected_source = _partition_path(
        evidence.observation.identity.resolved_path,
        3,
    )
    expected_options = set(_mount_options(evidence).split(","))
    if supplied != (journal.data_uuid, "exfat", expected_source) or not expected_options.issubset(
        set(evidence.mounted_data_options)
    ):
        _refuse(RefusalCode.MOUNT_INCONSISTENT, "mounted volume identity/options are mismatched")
    return True


def _mount_options(evidence: RuntimeEvidence) -> str:
    if evidence.dashcam_uid <= 0 or evidence.dashcam_storage_gid <= 0:
        _refuse(RefusalCode.MOUNT_INCONSISTENT, "resolved non-root service UID/GID are required")
    return (
        f"{MOUNT_OPTIONS_PREFIX},uid={evidence.dashcam_uid},"
        f"gid={evidence.dashcam_storage_gid},umask=0007"
    )


def _marker_payload(journal: RuntimeJournal) -> str:
    return json.dumps(
        {
            "boot_filesystem_uuid": journal.boot_uuid,
            "boot_partuuid": journal.boot_partuuid,
            "dashcam_uuid": journal.data_uuid,
            "data_end_sector": journal.data_end_sector,
            "data_partuuid": mbr_partuuid(journal.mbr_disk_id, 3),
            "data_start_sector": journal.data_start_sector,
            "layout_version": journal.schema_version,
            "mbr_disk_id": journal.mbr_disk_id,
            "root_end_sector": journal.root_end_sector,
            "root_filesystem_uuid": journal.root_uuid,
            "root_partuuid": journal.root_partuuid,
            "serial": journal.source_identity.serial,
            "source_table_fingerprint": journal.source_identity.partition_table_fingerprint,
            "transaction_id": journal.transaction_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _sfdisk_input(
    spec: LayoutSpec,
    observed: DeviceObservation,
    journal: RuntimeJournal,
) -> str:
    boot, _ = _boot_root(spec, observed)
    device = observed.identity.resolved_path
    return "\n".join(
        (
            "label: dos",
            f"label-id: {journal.mbr_disk_id}",
            f"device: {device}",
            "unit: sectors",
            "sector-size: 512",
            "",
            _partition_line(
                _partition_path(device, 1),
                boot.start_sector,
                boot.size_sectors,
                spec.boot.partition_type,
                spec.boot.bootable,
            ),
            _partition_line(
                _partition_path(device, 2),
                journal.root_start_sector,
                journal.root_end_sector - journal.root_start_sector + 1,
                spec.root.partition_type,
                spec.root.bootable,
            ),
            _partition_line(
                _partition_path(device, 3),
                journal.data_start_sector,
                journal.data_end_sector - journal.data_start_sector + 1,
                spec.data.partition_type,
                spec.data.bootable,
            ),
            "",
        )
    )


def _partition_line(
    path: str,
    start: int,
    size: int,
    partition_type: str,
    bootable: bool,
) -> str:
    flag = ", bootable" if bootable else ""
    return f"{path} : start={start}, size={size}, type={partition_type.removeprefix('0x')}{flag}"


def _boot_root(
    spec: LayoutSpec, observed: DeviceObservation
) -> tuple[PartitionObservation, PartitionObservation]:
    by_number = {partition.number: partition for partition in observed.partitions}
    boot = by_number.get(spec.boot.number)
    root = by_number.get(spec.root.number)
    if boot is None or root is None:
        _refuse(RefusalCode.LAYOUT_REFUSED, "boot and root partitions are required")
    return boot, root


def _required_uuid(partition: PartitionObservation, name: str) -> str:
    if partition.uuid is None:
        _refuse(RefusalCode.IDENTITY_CHANGED, f"{name} filesystem UUID is missing")
    return partition.uuid


def _required_partuuid(partition: PartitionObservation, name: str) -> str:
    if partition.partuuid is None:
        _refuse(RefusalCode.IDENTITY_CHANGED, f"{name} PARTUUID is missing")
    return partition.partuuid


def _partition_path(device: str, number: int) -> str:
    _validated_device_path(device)
    separator = "p" if device[-1].isdigit() else ""
    return f"{device}{separator}{number}"


def _validated_device_path(path: str) -> None:
    parsed = PurePosixPath(path)
    if (
        not path.startswith("/dev/")
        or ".." in parsed.parts
        or len(path) > 128
        or _SAFE_TEXT_RE.fullmatch(path) is None
    ):
        raise FirstbootError("device path is unsafe")


def _validated_runtime_path(path: str) -> None:
    parsed = PurePosixPath(path)
    allowed = (
        "/run/dashcam-firstboot/",
        "/srv/dashcam/",
        "/var/lib/dashcam/provisioning/",
        "/etc/dashcam/",
    )
    if path != "/etc/fstab" and not path.startswith(allowed):
        raise FirstbootError("runtime output path is outside the closed allowlist")
    if ".." in parsed.parts or len(path) > 256 or _SAFE_TEXT_RE.fullmatch(path) is None:
        raise FirstbootError("runtime output path is unsafe")


def _phase_rank(phase: RuntimePhase) -> int:
    return tuple(RuntimePhase).index(phase)


def _closed_mapping(
    value: object,
    keys: set[str],
    name: str,
) -> Mapping[str, object]:
    result = _mapping(value, name)
    if set(result) != keys:
        raise FirstbootError(f"{name} has missing or unknown keys")
    return result


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise FirstbootError(f"{name} must be an object")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024 or "\x00" in value:
        raise FirstbootError(f"{name} must be bounded text")
    return value


def _optional_text(value: object, name: str) -> str | None:
    return None if value is None else _text(value, name)


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FirstbootError(f"{name} must be an integer")
    return value


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise FirstbootError(f"{name} must be a boolean")
    return value


def _string_tuple(value: object, name: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or len(value) > 256
        or not all(isinstance(item, str) for item in value)
    ):
        raise FirstbootError(f"{name} must be a bounded string list")
    return tuple(value)


def _refuse(code: RefusalCode, message: str) -> NoReturn:
    raise FirstbootRefused(code, message)


__all__ = [
    "MAX_REBOOTS",
    "MAX_RETRIES",
    "ROOT_TRIGGER",
    "ActionKind",
    "CommandResult",
    "Executor",
    "FirstbootError",
    "FirstbootRefused",
    "JournalStore",
    "RefusalCode",
    "RuntimeAction",
    "RuntimeEvidence",
    "RuntimeJournal",
    "RuntimePhase",
    "RuntimePlan",
    "RuntimeStage",
    "apply_observed_success",
    "evidence_from_mapping",
    "execute_plan",
    "journal_from_mapping",
    "plan_next",
    "record_failure",
    "start_journal",
]
