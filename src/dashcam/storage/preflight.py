"""Fail-closed recording-root preflight with a narrow Linux adapter.

The decision core remains effects-injected and host independent. The command
line adapter only observes an existing mount and performs one exclusive,
bounded create/write/fsync/unlink probe after every static check passes. It
never mounts, repairs, partitions, or formats storage.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol, cast

from dashcam.config import ConfigError, DashcamConfig, load_config
from dashcam.state import StorageState

RECORDING_ROOT: Final = "/srv/dashcam"
SENTINEL_NAME: Final = ".dashcam-volume"
MAX_MOUNT_OPTIONS: Final = 32
MAX_FACT_STRING_CHARS: Final = 256
MAX_PROBE_BYTES: Final = 4 * 1024
MAX_SENTINEL_BYTES: Final = 4 * 1024
MAX_IDENTITY_BYTES: Final = 8 * 1024
MAX_FINDMNT_BYTES: Final = 16 * 1024
MAX_FINDMNT_ROWS: Final = 2
MAX_FACT_INTEGER: Final = 9_223_372_036_854_775_807
COMMAND_TIMEOUT_SECONDS: Final = 3.0
PROBE_NAME: Final = ".dashcam-preflight-v1.tmp"
PROBE_PAYLOAD: Final = b"dashcam-storage-preflight-v1\n"
IDENTITY_PATH: Final = "/etc/dashcam/storage-volume.env"
GIB: Final = 1024**3
_FINDMNT: Final = "/usr/bin/findmnt"

_TOKEN_RE: Final = re.compile(r"[A-Za-z0-9._:+-]{1,128}")
_DEVICE_RE: Final = re.compile(r"/dev/[A-Za-z0-9._/+-]{1,192}")
_DEVICE_ID_RE: Final = re.compile(r"\d{1,10}:\d{1,10}")
_FINGERPRINT_RE: Final = re.compile(r"[0-9a-f]{64}")
_MOUNT_OPTION_RE: Final = re.compile(r"[A-Za-z0-9._=:+-]{1,128}")
_IDENTITY_KEY_RE: Final = re.compile(r"[A-Z][A-Z0-9_]{1,63}")
_FINDMNT_ROW_KEYS: Final = frozenset(
    {"target", "source", "fstype", "label", "uuid", "options", "maj:min"}
)

_ROOT_KEYS: Final = frozenset({"mount", "space", "sentinel"})
_MOUNT_KEYS: Final = frozenset(
    {
        "target",
        "mounted",
        "source",
        "filesystem",
        "label",
        "uuid",
        "mount_options",
        "device_id",
        "os_root_device_id",
    }
)
_SPACE_KEYS: Final = frozenset({"capacity_bytes", "free_bytes"})
_SENTINEL_KEYS: Final = frozenset(
    {
        "layout_version",
        "serial",
        "dashcam_uuid",
        "source_table_fingerprint",
        "root_end_sector",
        "data_start_sector",
        "data_end_sector",
    }
)
_IDENTITY_KEYS: Final = frozenset(
    {
        "DASHCAM_STORAGE_SCHEMA_VERSION",
        "DASHCAM_STORAGE_LAYOUT_VERSION",
        "DASHCAM_STORAGE_MOUNT",
        "DASHCAM_STORAGE_UUID",
        "DASHCAM_STORAGE_CID",
        "DASHCAM_STORAGE_SOURCE_MBR_SHA256",
        "DASHCAM_STORAGE_ROOT_END_SECTOR",
        "DASHCAM_STORAGE_DATA_START_SECTOR",
        "DASHCAM_STORAGE_DATA_END_SECTOR",
        "DASHCAM_STORAGE_MINIMUM_CAPACITY_BYTES",
    }
)


class PreflightFactsError(ValueError):
    """Raised when injected structured facts violate their closed contract."""


class PreflightRuntimeError(RuntimeError):
    """Raised when the local observation or probe adapter cannot operate safely."""


class StorageIdentityError(ValueError):
    """Raised when the rootfs identity handoff violates its closed contract."""


class PreflightReason(StrEnum):
    """Stable preflight refusal reasons suitable for health reporting."""

    MALFORMED_FACTS = "MALFORMED_FACTS"
    WRONG_TARGET = "WRONG_TARGET"
    UNMOUNTED = "UNMOUNTED"
    MISSING_MOUNT_IDENTITY = "MISSING_MOUNT_IDENTITY"
    ROOTFS_ALIAS = "ROOTFS_ALIAS"
    WRONG_FILESYSTEM = "WRONG_FILESYSTEM"
    WRONG_LABEL = "WRONG_LABEL"
    WRONG_UUID = "WRONG_UUID"
    WRONG_UUID_SUFFIX = "WRONG_UUID_SUFFIX"
    READ_ONLY = "READ_ONLY"
    CONFLICTING_MOUNT_OPTIONS = "CONFLICTING_MOUNT_OPTIONS"
    MISSING_SENTINEL = "MISSING_SENTINEL"
    WRONG_SENTINEL_VERSION = "WRONG_SENTINEL_VERSION"
    WRONG_SENTINEL_IDENTITY = "WRONG_SENTINEL_IDENTITY"
    WRONG_SENTINEL_UUID = "WRONG_SENTINEL_UUID"
    INVALID_SENTINEL_GEOMETRY = "INVALID_SENTINEL_GEOMETRY"
    INVALID_SPACE = "INVALID_SPACE"
    INSUFFICIENT_CAPACITY = "INSUFFICIENT_CAPACITY"
    RESERVE_EXHAUSTED = "RESERVE_EXHAUSTED"
    WRITE_PROBE_FAILED = "WRITE_PROBE_FAILED"


@dataclass(frozen=True, slots=True)
class MountFacts:
    """Resolved findmnt-equivalent facts for the recording target and OS root."""

    target: str
    mounted: bool
    source: str | None
    filesystem: str | None
    label: str | None
    uuid: str | None
    mount_options: tuple[str, ...]
    device_id: str | None
    os_root_device_id: str


@dataclass(frozen=True, slots=True)
class SpaceFacts:
    """Capacity facts for the mounted recording filesystem."""

    capacity_bytes: int | None
    free_bytes: int | None


@dataclass(frozen=True, slots=True)
class VolumeSentinel:
    """Closed version-1 contents of ``.dashcam-volume``."""

    layout_version: int
    serial: str
    dashcam_uuid: str
    source_table_fingerprint: str
    root_end_sector: int
    data_start_sector: int
    data_end_sector: int


@dataclass(frozen=True, slots=True)
class StorageIdentity:
    """Closed rootfs handoff written only after Bootstrap v1 configures storage."""

    schema_version: int
    layout_version: int
    mount: str
    uuid: str
    cid: str
    source_mbr_sha256: str
    root_end_sector: int
    data_start_sector: int
    data_end_sector: int
    minimum_capacity_bytes: int

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.layout_version != 1:
            raise StorageIdentityError("unsupported storage identity version")
        if self.mount != RECORDING_ROOT:
            raise StorageIdentityError(f"storage identity mount must be {RECORDING_ROOT}")
        _token(self.uuid, "storage identity UUID")
        _token(self.cid, "storage identity CID")
        if _FINGERPRINT_RE.fullmatch(self.source_mbr_sha256) is None:
            raise StorageIdentityError("storage identity source fingerprint is invalid")
        _positive_int(self.minimum_capacity_bytes, "minimum_capacity_bytes")
        if not (
            0
            <= self.root_end_sector
            < self.data_start_sector
            <= self.data_end_sector
            <= MAX_FACT_INTEGER
        ):
            raise StorageIdentityError("storage identity geometry is invalid")


@dataclass(frozen=True, slots=True)
class RecordingRootFacts:
    """All read-only facts required before attempting a write probe."""

    mount: MountFacts
    space: SpaceFacts
    sentinel: VolumeSentinel | None


@dataclass(frozen=True, slots=True)
class PreflightPolicy:
    """Configured identity and space requirements for one provisioned volume."""

    expected_uuid: str
    expected_uuid_suffix: str
    expected_serial: str
    expected_source_table_fingerprint: str
    expected_root_end_sector: int
    expected_data_start_sector: int
    expected_data_end_sector: int
    minimum_capacity_bytes: int
    reserve_bytes: int
    expected_layout_version: int = 1
    recording_root: str = RECORDING_ROOT
    required_filesystem: str = "exfat"
    required_label: str = "DASHCAM"

    def __post_init__(self) -> None:
        if self.recording_root != RECORDING_ROOT:
            raise ValueError(f"recording_root must be exactly {RECORDING_ROOT}")
        if self.required_filesystem != "exfat":
            raise ValueError("required_filesystem must be exfat")
        if self.required_label != "DASHCAM":
            raise ValueError("required_label must be DASHCAM")
        _token(self.expected_uuid, "expected_uuid")
        _token(self.expected_uuid_suffix, "expected_uuid_suffix")
        _token(self.expected_serial, "expected_serial")
        if not self.expected_uuid.endswith(self.expected_uuid_suffix):
            raise ValueError("expected UUID does not end with its configured suffix")
        if _FINGERPRINT_RE.fullmatch(self.expected_source_table_fingerprint) is None:
            raise ValueError("expected source-table fingerprint must be lowercase SHA-256")
        if not (
            0
            <= self.expected_root_end_sector
            < self.expected_data_start_sector
            <= self.expected_data_end_sector
            <= MAX_FACT_INTEGER
        ):
            raise ValueError("expected sentinel geometry is invalid")
        _positive_int(self.expected_layout_version, "expected_layout_version")
        _positive_int(self.minimum_capacity_bytes, "minimum_capacity_bytes")
        _positive_int(self.reserve_bytes, "reserve_bytes")
        if self.reserve_bytes >= self.minimum_capacity_bytes:
            raise ValueError("reserve_bytes must be below minimum_capacity_bytes")


class ProbeFile(Protocol):
    """Exclusive temporary file returned only by an injected test/target adapter."""

    def write(self, payload: bytes) -> int:
        """Write the bounded payload and return the number of bytes written."""

    def fsync(self) -> None:
        """Flush file contents through the adapter."""

    def close(self) -> None:
        """Close the probe file."""


class PreflightFilesystem(Protocol):
    """Narrow effects needed to prove create/write/fsync/unlink behavior."""

    def create_exclusive(self, recording_root: str, relative_name: str) -> ProbeFile:
        """Create a new probe file without overwriting an existing entry."""

    def unlink(self, recording_root: str, relative_name: str) -> None:
        """Remove exactly the probe file created by this invocation."""


class RecordingFactsCollector(Protocol):
    """Collect one fresh bounded observation of the recording root."""

    def collect(self, recording_root: str) -> Mapping[str, object]:
        """Return structured facts suitable for the strict parser."""


class PreflightFilesystemFactory(Protocol):
    """Construct the one-shot probe after mount identity is parsed."""

    def __call__(
        self,
        *,
        recording_root: str,
        expected_device_id: str,
    ) -> PreflightFilesystem:
        """Return a probe bound to the freshly observed device."""


class _StatVfsResult(Protocol):
    f_blocks: int
    f_bavail: int
    f_frsize: int


@dataclass(frozen=True, slots=True)
class PreflightResult:
    """Storage state, stable reasons, parsed facts, and probe evidence."""

    state: StorageState
    reasons: tuple[PreflightReason, ...]
    facts: RecordingRootFacts | None
    probe_attempted: bool
    probe_succeeded: bool

    @property
    def ready(self) -> bool:
        return (
            self.state is StorageState.READY
            and not self.reasons
            and self.probe_attempted
            and self.probe_succeeded
        )


def recording_root_facts_from_mapping(raw: Mapping[str, object]) -> RecordingRootFacts:
    """Strictly parse bounded structured facts; shell text is never accepted."""

    root = _closed_mapping(raw, _ROOT_KEYS, "facts")
    mount_raw = _closed_mapping(root["mount"], _MOUNT_KEYS, "mount")
    options_value = mount_raw["mount_options"]
    if (
        not isinstance(options_value, list)
        or len(options_value) > MAX_MOUNT_OPTIONS
        or not all(isinstance(option, str) for option in options_value)
    ):
        raise PreflightFactsError("mount.mount_options must be a bounded string array")
    options = tuple(cast(list[str], options_value))
    for option in options:
        if _MOUNT_OPTION_RE.fullmatch(option) is None:
            raise PreflightFactsError("mount.mount_options contains an invalid option")

    mount = MountFacts(
        target=_absolute_path(mount_raw["target"], "mount.target"),
        mounted=_boolean(mount_raw["mounted"], "mount.mounted"),
        source=_optional_device(mount_raw["source"], "mount.source"),
        filesystem=_optional_token(mount_raw["filesystem"], "mount.filesystem"),
        label=_optional_token(mount_raw["label"], "mount.label"),
        uuid=_optional_token(mount_raw["uuid"], "mount.uuid"),
        mount_options=options,
        device_id=_optional_device_id(mount_raw["device_id"], "mount.device_id"),
        os_root_device_id=_device_id(mount_raw["os_root_device_id"], "mount.os_root_device_id"),
    )

    space_raw = _closed_mapping(root["space"], _SPACE_KEYS, "space")
    space = SpaceFacts(
        capacity_bytes=_optional_non_negative_int(
            space_raw["capacity_bytes"], "space.capacity_bytes"
        ),
        free_bytes=_optional_non_negative_int(space_raw["free_bytes"], "space.free_bytes"),
    )

    sentinel_value = root["sentinel"]
    sentinel: VolumeSentinel | None
    if sentinel_value is None:
        sentinel = None
    else:
        sentinel_raw = _closed_mapping(sentinel_value, _SENTINEL_KEYS, "sentinel")
        fingerprint = _string(
            sentinel_raw["source_table_fingerprint"],
            "sentinel.source_table_fingerprint",
        )
        if _FINGERPRINT_RE.fullmatch(fingerprint) is None:
            raise PreflightFactsError("sentinel.source_table_fingerprint must be lowercase SHA-256")
        sentinel = VolumeSentinel(
            layout_version=_positive_integer(
                sentinel_raw["layout_version"], "sentinel.layout_version"
            ),
            serial=_bounded_token(sentinel_raw["serial"], "sentinel.serial"),
            dashcam_uuid=_bounded_token(sentinel_raw["dashcam_uuid"], "sentinel.dashcam_uuid"),
            source_table_fingerprint=fingerprint,
            root_end_sector=_non_negative_integer(
                sentinel_raw["root_end_sector"], "sentinel.root_end_sector"
            ),
            data_start_sector=_non_negative_integer(
                sentinel_raw["data_start_sector"], "sentinel.data_start_sector"
            ),
            data_end_sector=_non_negative_integer(
                sentinel_raw["data_end_sector"], "sentinel.data_end_sector"
            ),
        )
    return RecordingRootFacts(mount=mount, space=space, sentinel=sentinel)


def run_storage_preflight(
    raw_facts: Mapping[str, object],
    *,
    policy: PreflightPolicy,
    filesystem: PreflightFilesystem,
) -> PreflightResult:
    """Fail closed unless identity, space, and an injected write probe all pass."""

    if not isinstance(policy, PreflightPolicy):
        raise TypeError("policy must be a PreflightPolicy")
    try:
        facts = recording_root_facts_from_mapping(raw_facts)
    except (PreflightFactsError, TypeError, ValueError):
        return PreflightResult(
            StorageState.FAULTED,
            (PreflightReason.MALFORMED_FACTS,),
            None,
            False,
            False,
        )

    reasons = _static_refusals(facts, policy)
    if reasons:
        return PreflightResult(
            _state_for_reasons(reasons),
            reasons,
            facts,
            False,
            False,
        )

    probe_succeeded = _write_probe(filesystem, policy.recording_root)
    if not probe_succeeded:
        return PreflightResult(
            StorageState.FAULTED,
            (PreflightReason.WRITE_PROBE_FAILED,),
            facts,
            True,
            False,
        )
    return PreflightResult(StorageState.READY, (), facts, True, True)


def _static_refusals(
    facts: RecordingRootFacts, policy: PreflightPolicy
) -> tuple[PreflightReason, ...]:
    reasons: list[PreflightReason] = []
    mount = facts.mount
    if mount.target != policy.recording_root:
        reasons.append(PreflightReason.WRONG_TARGET)
    if not mount.mounted:
        reasons.append(PreflightReason.UNMOUNTED)
    if (
        mount.source is None
        or mount.device_id is None
        or mount.filesystem is None
        or mount.label is None
        or mount.uuid is None
    ):
        reasons.append(PreflightReason.MISSING_MOUNT_IDENTITY)
    if mount.device_id is not None and mount.device_id == mount.os_root_device_id:
        reasons.append(PreflightReason.ROOTFS_ALIAS)
    if mount.filesystem is not None and mount.filesystem != policy.required_filesystem:
        reasons.append(PreflightReason.WRONG_FILESYSTEM)
    if mount.label is not None and mount.label != policy.required_label:
        reasons.append(PreflightReason.WRONG_LABEL)
    if mount.uuid is not None:
        if mount.uuid != policy.expected_uuid:
            reasons.append(PreflightReason.WRONG_UUID)
        if not mount.uuid.endswith(policy.expected_uuid_suffix):
            reasons.append(PreflightReason.WRONG_UUID_SUFFIX)

    option_set = frozenset(mount.mount_options)
    if "ro" in option_set and "rw" in option_set:
        reasons.append(PreflightReason.CONFLICTING_MOUNT_OPTIONS)
    elif "ro" in option_set or "rw" not in option_set:
        reasons.append(PreflightReason.READ_ONLY)

    sentinel = facts.sentinel
    if sentinel is None:
        reasons.append(PreflightReason.MISSING_SENTINEL)
    else:
        if sentinel.layout_version != policy.expected_layout_version:
            reasons.append(PreflightReason.WRONG_SENTINEL_VERSION)
        if (
            sentinel.serial != policy.expected_serial
            or sentinel.source_table_fingerprint != policy.expected_source_table_fingerprint
        ):
            reasons.append(PreflightReason.WRONG_SENTINEL_IDENTITY)
        if sentinel.dashcam_uuid != policy.expected_uuid:
            reasons.append(PreflightReason.WRONG_SENTINEL_UUID)
        if (
            sentinel.root_end_sector != policy.expected_root_end_sector
            or sentinel.data_start_sector != policy.expected_data_start_sector
            or sentinel.data_end_sector != policy.expected_data_end_sector
        ):
            reasons.append(PreflightReason.INVALID_SENTINEL_GEOMETRY)

    capacity = facts.space.capacity_bytes
    free = facts.space.free_bytes
    if capacity is None or free is None or capacity <= 0 or free > capacity:
        reasons.append(PreflightReason.INVALID_SPACE)
    else:
        if capacity < policy.minimum_capacity_bytes:
            reasons.append(PreflightReason.INSUFFICIENT_CAPACITY)
        if free <= policy.reserve_bytes:
            reasons.append(PreflightReason.RESERVE_EXHAUSTED)
    return tuple(reasons)


def _state_for_reasons(reasons: tuple[PreflightReason, ...]) -> StorageState:
    non_state_reasons = {
        PreflightReason.READ_ONLY,
        PreflightReason.CONFLICTING_MOUNT_OPTIONS,
        PreflightReason.RESERVE_EXHAUSTED,
    }
    if any(reason not in non_state_reasons for reason in reasons):
        return StorageState.FAULTED
    if any(
        reason in {PreflightReason.READ_ONLY, PreflightReason.CONFLICTING_MOUNT_OPTIONS}
        for reason in reasons
    ):
        return StorageState.READ_ONLY
    if PreflightReason.RESERVE_EXHAUSTED in reasons:
        return StorageState.EMERGENCY
    return StorageState.FAULTED


def _write_probe(filesystem: PreflightFilesystem, recording_root: str) -> bool:
    payload = PROBE_PAYLOAD
    if not payload or len(payload) > MAX_PROBE_BYTES:
        return False
    handle: ProbeFile | None = None
    created = False
    failed = False
    try:
        handle = filesystem.create_exclusive(recording_root, PROBE_NAME)
        created = True
        written = handle.write(payload)
        if isinstance(written, bool) or not isinstance(written, int) or written != len(payload):
            failed = True
        else:
            handle.fsync()
    except Exception:
        failed = True
    finally:
        if handle is not None:
            try:
                handle.close()
            except Exception:
                failed = True
        if created:
            try:
                filesystem.unlink(recording_root, PROBE_NAME)
            except Exception:
                failed = True
    return not failed


def _closed_mapping(value: object, expected: frozenset[str], field: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise PreflightFactsError(f"{field} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise PreflightFactsError(f"{field} keys must be strings")
    table = dict(cast(Mapping[str, object], value))
    actual = set(table)
    if actual != expected:
        raise PreflightFactsError(
            f"{field} keys differ; missing={sorted(expected - actual)}, "
            f"unknown={sorted(actual - expected)}"
        )
    return table


def _string(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_FACT_STRING_CHARS
        or not value.isascii()
        or not value.isprintable()
    ):
        raise PreflightFactsError(f"{field} must be bounded printable ASCII")
    return value


def _bounded_token(value: object, field: str) -> str:
    text = _string(value, field)
    if _TOKEN_RE.fullmatch(text) is None:
        raise PreflightFactsError(f"{field} must be a safe token")
    return text


def _optional_token(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _bounded_token(value, field)


def _token(value: str, field: str) -> str:
    if not isinstance(value, str) or _TOKEN_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a safe bounded token")
    return value


def _absolute_path(value: object, field: str) -> str:
    text = _string(value, field)
    if (
        not text.startswith("/")
        or "//" in text
        or "\\" in text
        or any(part in {"", ".", ".."} for part in text.split("/")[1:])
    ):
        raise PreflightFactsError(f"{field} must be a normalized absolute path")
    return text


def _optional_device(value: object, field: str) -> str | None:
    if value is None:
        return None
    text = _string(value, field)
    if (
        _DEVICE_RE.fullmatch(text) is None
        or "//" in text
        or any(part in {"", ".", ".."} for part in text.split("/")[1:])
    ):
        raise PreflightFactsError(f"{field} must be a resolved /dev path")
    return text


def _device_id(value: object, field: str) -> str:
    text = _string(value, field)
    if _DEVICE_ID_RE.fullmatch(text) is None:
        raise PreflightFactsError(f"{field} must be a major:minor device ID")
    return text


def _optional_device_id(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _device_id(value, field)


def _boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise PreflightFactsError(f"{field} must be boolean")
    return value


def _non_negative_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_FACT_INTEGER:
        raise PreflightFactsError(f"{field} must be a non-negative integer")
    return value


def _positive_integer(value: object, field: str) -> int:
    result = _non_negative_integer(value, field)
    if result == 0:
        raise PreflightFactsError(f"{field} must be positive")
    return result


def _optional_non_negative_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    return _non_negative_integer(value, field)


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def storage_identity_from_env(payload: bytes) -> StorageIdentity:
    """Parse the closed, non-shell rootfs handoff format."""

    if not payload or len(payload) > MAX_IDENTITY_BYTES:
        raise StorageIdentityError("storage identity has an invalid size")
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as error:
        raise StorageIdentityError("storage identity must be ASCII") from error
    if not text.endswith("\n") or "\r" in text or "\x00" in text:
        raise StorageIdentityError("storage identity must use canonical LF records")
    values: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line.count("=") != 1:
            raise StorageIdentityError("storage identity contains a malformed record")
        key, value = line.split("=", 1)
        if _IDENTITY_KEY_RE.fullmatch(key) is None or not value:
            raise StorageIdentityError("storage identity contains an invalid key or value")
        if key in values:
            raise StorageIdentityError("storage identity contains a duplicate key")
        values[key] = value
    if set(values) != _IDENTITY_KEYS:
        raise StorageIdentityError("storage identity keys differ from schema v1")
    try:
        return StorageIdentity(
            schema_version=_strict_decimal(
                values["DASHCAM_STORAGE_SCHEMA_VERSION"], "schema version"
            ),
            layout_version=_strict_decimal(
                values["DASHCAM_STORAGE_LAYOUT_VERSION"], "layout version"
            ),
            mount=values["DASHCAM_STORAGE_MOUNT"],
            uuid=values["DASHCAM_STORAGE_UUID"],
            cid=values["DASHCAM_STORAGE_CID"],
            source_mbr_sha256=values["DASHCAM_STORAGE_SOURCE_MBR_SHA256"],
            root_end_sector=_strict_decimal(
                values["DASHCAM_STORAGE_ROOT_END_SECTOR"], "root end sector"
            ),
            data_start_sector=_strict_decimal(
                values["DASHCAM_STORAGE_DATA_START_SECTOR"], "data start sector"
            ),
            data_end_sector=_strict_decimal(
                values["DASHCAM_STORAGE_DATA_END_SECTOR"], "data end sector"
            ),
            minimum_capacity_bytes=_strict_decimal(
                values["DASHCAM_STORAGE_MINIMUM_CAPACITY_BYTES"],
                "minimum capacity",
            ),
        )
    except ValueError as error:
        raise StorageIdentityError("storage identity contains an invalid value") from error


def load_storage_identity(
    path: str | os.PathLike[str],
    *,
    require_root_owner: bool = True,
) -> StorageIdentity:
    """Securely load a regular, non-symlink, root-owned Bootstrap handoff."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise StorageIdentityError("could not open storage identity") from error
    try:
        metadata = os.fstat(descriptor)
        mode = stat.S_IMODE(metadata.st_mode)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise StorageIdentityError("storage identity must be one regular file")
        if require_root_owner and metadata.st_uid != 0:
            raise StorageIdentityError("storage identity must be root-owned")
        if mode != 0o640:
            raise StorageIdentityError("storage identity mode must be 0640")
        payload = _read_limited(descriptor, MAX_IDENTITY_BYTES)
    finally:
        os.close(descriptor)
    return storage_identity_from_env(payload)


def policy_from_identity(config: DashcamConfig, identity: StorageIdentity) -> PreflightPolicy:
    """Bind editable product settings to immutable provisioned-volume identity."""

    storage = config.storage
    if (
        storage.recording_root != RECORDING_ROOT
        or storage.required_filesystem != "exfat"
        or storage.required_volume_label != "DASHCAM"
        or not storage.require_distinct_mount
    ):
        raise ValueError("configuration weakens the fixed storage safety contract")
    reserve_bytes = math.ceil(storage.minimum_free_gib * GIB)
    return PreflightPolicy(
        expected_uuid=identity.uuid,
        expected_uuid_suffix=identity.uuid[-4:],
        expected_serial=identity.cid,
        expected_source_table_fingerprint=identity.source_mbr_sha256,
        expected_root_end_sector=identity.root_end_sector,
        expected_data_start_sector=identity.data_start_sector,
        expected_data_end_sector=identity.data_end_sector,
        minimum_capacity_bytes=identity.minimum_capacity_bytes,
        reserve_bytes=reserve_bytes,
    )


class PosixFactsCollector:
    """Collect bounded mount, space, and canonical sentinel facts on Linux."""

    def __init__(self, *, findmnt: str = _FINDMNT) -> None:
        if not findmnt.startswith("/"):
            raise ValueError("findmnt path must be absolute")
        self._findmnt = findmnt

    def collect(self, recording_root: str = RECORDING_ROOT) -> dict[str, object]:
        if recording_root != RECORDING_ROOT:
            raise PreflightRuntimeError("recording root differs from the fixed contract")
        root_id = _directory_device_id("/")
        row = self._find_mount(recording_root)
        if row is None:
            return {
                "mount": {
                    "target": recording_root,
                    "mounted": False,
                    "source": None,
                    "filesystem": None,
                    "label": None,
                    "uuid": None,
                    "mount_options": [],
                    "device_id": None,
                    "os_root_device_id": root_id,
                },
                "space": {"capacity_bytes": None, "free_bytes": None},
                "sentinel": None,
            }

        target = _required_findmnt_string(row, "target")
        if target != recording_root:
            raise PreflightRuntimeError("findmnt returned a different target")
        source = _canonical_block_source(_required_findmnt_string(row, "source"))
        filesystem = _optional_findmnt_string(row, "fstype")
        label = _optional_findmnt_string(row, "label")
        uuid = _optional_findmnt_string(row, "uuid")
        options_text = _required_findmnt_string(row, "options")
        options = options_text.split(",") if options_text else []
        if len(options) > MAX_MOUNT_OPTIONS:
            raise PreflightRuntimeError("findmnt returned excessive mount options")
        device_id = _directory_device_id(recording_root)
        observed_device_id = _required_findmnt_string(row, "maj:min")
        if observed_device_id != device_id:
            raise PreflightRuntimeError("findmnt and stat disagree on the mounted device")
        statvfs = cast(
            Callable[[str], object],
            getattr(os, "statvfs"),  # noqa: B009 - absent from Windows typeshed
        )
        space = cast(_StatVfsResult, statvfs(recording_root))
        capacity_bytes = _checked_product(space.f_blocks, space.f_frsize)
        free_bytes = _checked_product(space.f_bavail, space.f_frsize)
        sentinel = _read_canonical_sentinel(recording_root, device_id)
        return {
            "mount": {
                "target": target,
                "mounted": True,
                "source": source,
                "filesystem": filesystem,
                "label": label,
                "uuid": uuid,
                "mount_options": options,
                "device_id": device_id,
                "os_root_device_id": root_id,
            },
            "space": {
                "capacity_bytes": capacity_bytes,
                "free_bytes": free_bytes,
            },
            "sentinel": sentinel,
        }

    def _find_mount(self, target: str) -> Mapping[str, object] | None:
        command = (
            self._findmnt,
            "--json",
            "--mountpoint",
            target,
            "--output",
            "TARGET,SOURCE,FSTYPE,LABEL,UUID,OPTIONS,MAJ:MIN",
        )
        try:
            result = subprocess.run(
                command,
                check=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=COMMAND_TIMEOUT_SECONDS,
                env={"LC_ALL": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise PreflightRuntimeError("findmnt observation failed") from error
        if len(result.stdout) > MAX_FINDMNT_BYTES or len(result.stderr) > MAX_FINDMNT_BYTES:
            raise PreflightRuntimeError("findmnt output exceeded its bound")
        if result.returncode == 1 and not result.stdout and not result.stderr:
            return None
        if result.returncode != 0:
            raise PreflightRuntimeError("findmnt returned an observation error")
        try:
            raw = json.loads(
                result.stdout.decode("utf-8"),
                object_pairs_hook=_unique_json_object,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise PreflightRuntimeError("findmnt returned malformed JSON") from error
        if not isinstance(raw, Mapping) or set(raw) != {"filesystems"}:
            raise PreflightRuntimeError("findmnt JSON root differs from its contract")
        filesystems = raw["filesystems"]
        if (
            not isinstance(filesystems, list)
            or not filesystems
            or len(filesystems) > MAX_FINDMNT_ROWS
        ):
            raise PreflightRuntimeError("findmnt mount row count is outside its bound")
        rows = tuple(_parse_findmnt_row(row, target=target) for row in filesystems)
        first = rows[0]
        if any(row != first for row in rows[1:]):
            raise PreflightRuntimeError("findmnt returned differing mount rows")
        return first


class PosixProbeFile:
    """One bounded descriptor created by :class:`PosixPreflightFilesystem`."""

    def __init__(self, descriptor: int) -> None:
        self._descriptor = descriptor

    def write(self, payload: bytes) -> int:
        if self._descriptor < 0 or not payload or len(payload) > MAX_PROBE_BYTES:
            raise PreflightRuntimeError("invalid probe write")
        return os.write(self._descriptor, payload)

    def fsync(self) -> None:
        if self._descriptor < 0:
            raise PreflightRuntimeError("probe descriptor is closed")
        os.fsync(self._descriptor)

    def close(self) -> None:
        if self._descriptor >= 0:
            descriptor = self._descriptor
            self._descriptor = -1
            os.close(descriptor)


class PosixPreflightFilesystem:
    """Exclusive, no-follow write probe constrained to one verified mount."""

    def __init__(self, *, recording_root: str, expected_device_id: str) -> None:
        if recording_root != RECORDING_ROOT:
            raise ValueError("probe recording root differs from the fixed contract")
        _device_id(expected_device_id, "expected_device_id")
        self._recording_root = recording_root
        self._expected_device_id = expected_device_id
        self._directory_fd = -1
        self._created_identity: tuple[int, int] | None = None

    def create_exclusive(self, recording_root: str, relative_name: str) -> ProbeFile:
        self._validate_target(recording_root, relative_name)
        if self._directory_fd >= 0:
            raise PreflightRuntimeError("probe adapter is single-use")
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        directory_fd = os.open(recording_root, directory_flags)
        try:
            root_metadata = os.fstat(directory_fd)
            if (
                not stat.S_ISDIR(root_metadata.st_mode)
                or _device_id_from_dev(root_metadata.st_dev) != self._expected_device_id
                or _device_id_from_dev(os.stat("/").st_dev) == self._expected_device_id
            ):
                raise PreflightRuntimeError("probe root is not the verified distinct mount")
            file_flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = os.open(relative_name, file_flags, 0o600, dir_fd=directory_fd)
            try:
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or metadata.st_dev != root_metadata.st_dev
                ):
                    raise PreflightRuntimeError("probe file identity is unsafe")
            except Exception:
                os.close(descriptor)
                try:
                    os.unlink(relative_name, dir_fd=directory_fd)
                finally:
                    raise
            self._directory_fd = directory_fd
            self._created_identity = (metadata.st_dev, metadata.st_ino)
            return PosixProbeFile(descriptor)
        except Exception:
            os.close(directory_fd)
            raise

    def unlink(self, recording_root: str, relative_name: str) -> None:
        self._validate_target(recording_root, relative_name)
        directory_fd = self._directory_fd
        expected = self._created_identity
        self._directory_fd = -1
        self._created_identity = None
        if directory_fd < 0 or expected is None:
            raise PreflightRuntimeError("probe cleanup has no created file")
        try:
            metadata = os.stat(relative_name, dir_fd=directory_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or (metadata.st_dev, metadata.st_ino) != expected
            ):
                raise PreflightRuntimeError("probe entry changed before cleanup")
            os.unlink(relative_name, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)

    def _validate_target(self, recording_root: str, relative_name: str) -> None:
        if recording_root != self._recording_root or relative_name != PROBE_NAME:
            raise PreflightRuntimeError("probe target differs from the closed contract")


def _read_canonical_sentinel(
    recording_root: str, expected_device_id: str
) -> Mapping[str, object] | None:
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    directory_fd = os.open(recording_root, directory_flags)
    try:
        directory_metadata = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(directory_metadata.st_mode)
            or _device_id_from_dev(directory_metadata.st_dev) != expected_device_id
        ):
            raise PreflightRuntimeError("sentinel parent is not the observed mount")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(SENTINEL_NAME, flags, dir_fd=directory_fd)
        except FileNotFoundError:
            return None
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_dev != directory_metadata.st_dev
            ):
                raise PreflightRuntimeError("sentinel is not one regular mount-local file")
            payload = _read_limited(descriptor, MAX_SENTINEL_BYTES)
        finally:
            os.close(descriptor)
    finally:
        os.close(directory_fd)
    value = _strict_json_mapping(payload, "sentinel")
    canonical = (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode("ascii")
    if payload != canonical:
        raise PreflightRuntimeError("sentinel JSON is not canonical")
    return value


def _strict_json_mapping(payload: bytes, description: str) -> dict[str, object]:
    duplicates = False

    def pairs_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        nonlocal duplicates
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                duplicates = True
            result[key] = value
        return result

    try:
        value = json.loads(payload.decode("ascii"), object_pairs_hook=pairs_hook)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PreflightRuntimeError(f"{description} JSON is malformed") from error
    if duplicates or not isinstance(value, dict):
        raise PreflightRuntimeError(f"{description} JSON must be one object without duplicates")
    return cast(dict[str, object], value)


def _canonical_block_source(source: str) -> str:
    if _DEVICE_RE.fullmatch(source) is None:
        raise PreflightRuntimeError("mount source is not a /dev path")
    resolved = os.path.realpath(source)
    if _DEVICE_RE.fullmatch(resolved) is None:
        raise PreflightRuntimeError("resolved mount source is not a /dev path")
    try:
        metadata = os.stat(resolved, follow_symlinks=False)
    except OSError as error:
        raise PreflightRuntimeError("could not inspect mounted block source") from error
    if not stat.S_ISBLK(metadata.st_mode):
        raise PreflightRuntimeError("mount source is not a block device")
    return resolved


def _directory_device_id(path: str) -> str:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise PreflightRuntimeError("could not open observed directory") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise PreflightRuntimeError("observed path is not a directory")
        return _device_id_from_dev(metadata.st_dev)
    finally:
        os.close(descriptor)


def _device_id_from_dev(device: int) -> str:
    major = cast(
        Callable[[int], int],
        getattr(os, "major"),  # noqa: B009 - absent from Windows typeshed
    )
    minor = cast(
        Callable[[int], int],
        getattr(os, "minor"),  # noqa: B009 - absent from Windows typeshed
    )
    return f"{major(device)}:{minor(device)}"


def _checked_product(left: int, right: int) -> int:
    if (
        isinstance(left, bool)
        or isinstance(right, bool)
        or left < 0
        or right <= 0
        or left > MAX_FACT_INTEGER // right
    ):
        raise PreflightRuntimeError("filesystem size is outside the supported range")
    return left * right


def _required_findmnt_string(row: Mapping[str, object], key: str) -> str:
    value = row.get(key)
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_FACT_STRING_CHARS
        or not value.isascii()
        or not value.isprintable()
    ):
        raise PreflightRuntimeError(f"findmnt {key} is missing or excessive")
    return value


def _optional_findmnt_string(row: Mapping[str, object], key: str) -> str | None:
    value = row.get(key)
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_FACT_STRING_CHARS
        or not value.isascii()
        or not value.isprintable()
    ):
        raise PreflightRuntimeError(f"findmnt {key} is malformed")
    return value


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    table: dict[str, object] = {}
    for key, value in pairs:
        if key in table:
            raise ValueError("duplicate JSON object key")
        table[key] = value
    return table


def _parse_findmnt_row(value: object, *, target: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise PreflightRuntimeError("findmnt mount row is malformed")
    row = dict(cast(Mapping[str, object], value))
    if set(row) != _FINDMNT_ROW_KEYS:
        raise PreflightRuntimeError("findmnt mount row schema differs from its contract")

    parsed_target = _required_findmnt_string(row, "target")
    source = _required_findmnt_string(row, "source")
    filesystem = _optional_findmnt_string(row, "fstype")
    label = _optional_findmnt_string(row, "label")
    uuid = _optional_findmnt_string(row, "uuid")
    options = _required_findmnt_string(row, "options")
    device_id = _required_findmnt_string(row, "maj:min")

    if parsed_target != target:
        raise PreflightRuntimeError("findmnt returned a different target")
    if (
        _DEVICE_RE.fullmatch(source) is None
        or "//" in source
        or any(part in {"", ".", ".."} for part in source.split("/")[1:])
    ):
        raise PreflightRuntimeError("findmnt source is malformed")
    for key, optional_token in (
        ("fstype", filesystem),
        ("label", label),
        ("uuid", uuid),
    ):
        if optional_token is not None and _TOKEN_RE.fullmatch(optional_token) is None:
            raise PreflightRuntimeError(f"findmnt {key} is malformed")
    option_values = options.split(",")
    if (
        len(option_values) > MAX_MOUNT_OPTIONS
        or any(_MOUNT_OPTION_RE.fullmatch(option) is None for option in option_values)
    ):
        raise PreflightRuntimeError("findmnt options are malformed or excessive")
    if _DEVICE_ID_RE.fullmatch(device_id) is None:
        raise PreflightRuntimeError("findmnt maj:min is malformed")

    return {
        "target": parsed_target,
        "source": source,
        "fstype": filesystem,
        "label": label,
        "uuid": uuid,
        "options": options,
        "maj:min": device_id,
    }


def _read_limited(descriptor: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    remaining = limit + 1
    while remaining:
        chunk = os.read(descriptor, min(remaining, 4096))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    if len(payload) > limit:
        raise PreflightRuntimeError("file exceeded its size bound")
    return payload


def _strict_decimal(value: str, field: str) -> int:
    if not value.isdecimal():
        raise ValueError(f"{field} is not decimal")
    result = int(value)
    if not 0 <= result <= MAX_FACT_INTEGER:
        raise ValueError(f"{field} is outside the supported range")
    return result


def _redacted_status(result: PreflightResult) -> dict[str, object]:
    facts = result.facts
    mount = facts.mount if facts is not None else None
    space = facts.space if facts is not None else None
    uuid_suffix = mount.uuid[-4:] if mount is not None and mount.uuid is not None else None
    return {
        "schema_version": 1,
        "state": result.state.value,
        "ready": result.ready,
        "reasons": [reason.value for reason in result.reasons],
        "mount": {
            "target": mount.target if mount is not None else RECORDING_ROOT,
            "mounted": mount.mounted if mount is not None else False,
            "filesystem": mount.filesystem if mount is not None else None,
            "label": mount.label if mount is not None else None,
            "uuid_suffix": uuid_suffix,
            "capacity_bytes": space.capacity_bytes if space is not None else None,
            "free_bytes": space.free_bytes if space is not None else None,
        },
        "probe_attempted": result.probe_attempted,
        "probe_succeeded": result.probe_succeeded,
    }


def _emit_status(status: Mapping[str, object]) -> None:
    payload = json.dumps(status, sort_keys=True, separators=(",", ":"))
    if len(payload.encode("utf-8")) > 2048:
        raise PreflightRuntimeError("status exceeded its output bound")
    print(payload, flush=True)


def run_live_storage_preflight(
    config: DashcamConfig,
    *,
    identity_path: str = IDENTITY_PATH,
    identity_loader: Callable[[str], StorageIdentity] = load_storage_identity,
    facts_collector: RecordingFactsCollector | None = None,
    filesystem_factory: PreflightFilesystemFactory = PosixPreflightFilesystem,
) -> PreflightResult:
    """Run the exact production observation and write-probe sequence once."""

    if not isinstance(config, DashcamConfig):
        raise TypeError("config must be a DashcamConfig")
    if not isinstance(identity_path, str) or not identity_path.startswith("/"):
        raise ValueError("identity_path must be an absolute POSIX path")
    identity = identity_loader(identity_path)
    policy = policy_from_identity(config, identity)
    collector = facts_collector or PosixFactsCollector()
    raw_facts = collector.collect(policy.recording_root)
    parsed = recording_root_facts_from_mapping(raw_facts)
    filesystem = filesystem_factory(
        recording_root=policy.recording_root,
        expected_device_id=parsed.mount.device_id or "0:0",
    )
    return run_storage_preflight(raw_facts, policy=policy, filesystem=filesystem)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the production fail-closed preflight and emit bounded status JSON."""

    parser = argparse.ArgumentParser(prog="python -m dashcam.storage.preflight")
    parser.add_argument("--config", required=True)
    parser.add_argument("--identity", default=IDENTITY_PATH)
    arguments = parser.parse_args(argv)
    try:
        config = load_config(arguments.config)
        result = run_live_storage_preflight(config, identity_path=arguments.identity)
        _emit_status(_redacted_status(result))
        return 0 if result.ready else 2
    except (
        ConfigError,
        OSError,
        PreflightFactsError,
        PreflightRuntimeError,
        StorageIdentityError,
        TypeError,
        ValueError,
    ):
        _emit_status(
            {
                "schema_version": 1,
                "state": StorageState.FAULTED.value,
                "ready": False,
                "reasons": ["RUNTIME_ERROR"],
                "mount": {"target": RECORDING_ROOT, "mounted": False},
                "probe_attempted": False,
                "probe_succeeded": False,
            }
        )
        return 3


if __name__ == "__main__":
    sys.exit(main())
