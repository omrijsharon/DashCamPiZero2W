"""Fail-closed schema-5 quiescence and rollback admission.

The normal recorder calls only :func:`run_pre_camera_guard`.  It never repairs
catalog state and refuses a schema-4 or missing-latch catalog.  The explicit
``quiesce`` command is the sole recovery entry point: it replays already
durable work within hard bounds, preserves orphaned partials, and may create a
missing false latch only after a second transaction-bound safety proof.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import stat
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

from dashcam.catalog.database import (
    SCHEMA_VERSION,
    ClipCatalog,
    RetentionThresholdLatch,
    RollbackCatalogState,
    catalog_schema_layout_matches,
    inspect_rollback_state_read_only,
)
from dashcam.catalog.filesystem import RootedFilesystem
from dashcam.catalog.models import ReconciliationBounds
from dashcam.config import DashcamConfig, load_config
from dashcam.state import StorageState
from dashcam.storage.preflight import (
    PreflightReason,
    PreflightResult,
    run_live_storage_preflight,
)
from dashcam.storage.retention import StorageThresholds

DEFAULT_CATALOG_PATH: Final = Path("/var/lib/dashcam/catalog.sqlite3")
DEFAULT_CONTROL_SOCKET_PATH: Final = Path("/run/dashcam/control.sock")
DEFAULT_CONFIG_PATH: Final = Path("/etc/dashcam/config.toml")
DEFAULT_IDENTITY_PATH: Final = Path("/etc/dashcam/storage-volume.env")
DEFAULT_MAX_PASSES: Final = 8
MAX_MAX_PASSES: Final = 32
MAX_WRITING_PER_PASS: Final = 1_024
MAX_FINALIZED_CLIPS: Final = 10_000
GIB: Final = 1024**3
MIB: Final = 1024**2
_BOOT_ID_PATH: Final = Path("/proc/sys/kernel/random/boot_id")
_BOOT_ID_RE: Final = re.compile(
    rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\n?"
)
_SCHEMA_HISTORY: Final = (
    (1, "create_clip_catalog"),
    (2, "add_event_protection"),
    (3, "add_protection_revisions"),
    (4, "add_name_reconciliation_payload"),
    (5, "add_retention_threshold_latch"),
)


class RollbackSafetyError(RuntimeError):
    """The rollback state could not be proven safe without destructive repair."""


@dataclass(frozen=True, slots=True)
class RollbackGuardReport:
    volume_uuid: str
    device_id: str
    capacity_bytes: int
    free_bytes: int
    high_free_bytes: int
    catalog_schema: int
    finalized_clips_examined: int


@dataclass(frozen=True, slots=True)
class RollbackQuiesceReport:
    schema_before: int
    schema_after: int
    passes: int
    intents_examined: int
    actions_attempted: int
    expired_leases_cleared: int
    orphaned_writing_demoted: int
    latch_initialized: bool
    guard: RollbackGuardReport


def run_pre_camera_guard(
    config: DashcamConfig,
    preflight: PreflightResult,
    *,
    catalog_path: Path = DEFAULT_CATALOG_PATH,
    control_socket_path: Path = DEFAULT_CONTROL_SOCKET_PATH,
    space_observer: Callable[[], tuple[int, int]] | None = None,
) -> RollbackGuardReport:
    """Prove schema-5 rollback safety without repairing catalog/media state."""

    facts, volume_uuid, device_id, capacity, free = _checked_storage(config, preflight)
    del facts
    _require_control_socket_absent(control_socket_path)
    schema = read_catalog_schema_version(catalog_path)
    if schema != SCHEMA_VERSION:
        raise RollbackSafetyError(
            f"catalog schema must already be {SCHEMA_VERSION}; run quiesce first"
        )
    filesystem = _rooted_filesystem(config, device_id)
    observed_capacity, observed_free = (
        filesystem.space_bytes() if space_observer is None else space_observer()
    )
    if (
        isinstance(observed_capacity, bool)
        or not isinstance(observed_capacity, int)
        or isinstance(observed_free, bool)
        or not isinstance(observed_free, int)
        or observed_capacity != capacity
        or not 0 <= observed_free <= observed_capacity
    ):
        raise RollbackSafetyError("recording capacity changed after preflight")
    high = _high_free_bytes(config, capacity)
    if free < high or observed_free < high:
        raise RollbackSafetyError("recording volume is below the rollback high watermark")
    try:
        state = inspect_rollback_state_read_only(
            catalog_path,
            filesystem,
            max_finalized_clips=MAX_FINALIZED_CLIPS,
        )
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        raise RollbackSafetyError("catalog inspection failed") from exc
    _require_quiescent_state(state)
    expected_latch = RetentionThresholdLatch(volume_uuid, capacity, False)
    if state.latch != expected_latch:
        if state.latch is None:
            raise RollbackSafetyError("retention threshold latch is missing")
        if state.latch.volume_uuid != volume_uuid:
            raise RollbackSafetyError("retention threshold latch UUID differs")
        if state.latch.capacity_bytes != capacity:
            raise RollbackSafetyError("retention threshold latch capacity differs")
        raise RollbackSafetyError("retention threshold latch remains set")
    return RollbackGuardReport(
        volume_uuid=volume_uuid,
        device_id=device_id,
        capacity_bytes=capacity,
        free_bytes=observed_free,
        high_free_bytes=high,
        catalog_schema=schema,
        finalized_clips_examined=state.finalized_clips_examined,
    )


def quiesce_for_rollback(
    config: DashcamConfig,
    preflight: PreflightResult,
    *,
    catalog_path: Path = DEFAULT_CATALOG_PATH,
    control_socket_path: Path = DEFAULT_CONTROL_SOCKET_PATH,
    boot_id: str,
    monotonic_ns: Callable[[], int] = time.monotonic_ns,
    preflight_refresh: Callable[[], PreflightResult] | None = None,
    space_observer: Callable[[], tuple[int, int]] | None = None,
    max_passes: int = DEFAULT_MAX_PASSES,
    bounds: ReconciliationBounds | None = None,
) -> RollbackQuiesceReport:
    """Boundedly converge durable state without constructing a media backend."""

    if isinstance(max_passes, bool) or not isinstance(max_passes, int):
        raise ValueError("max_passes must be an integer")
    if not 1 <= max_passes <= MAX_MAX_PASSES:
        raise ValueError(f"max_passes must be between 1 and {MAX_MAX_PASSES}")
    if not isinstance(boot_id, str):
        raise ValueError("boot_id must be one canonical UUID")
    try:
        boot_id_payload = boot_id.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("boot_id must be one canonical UUID") from exc
    if _BOOT_ID_RE.fullmatch(boot_id_payload) is None:
        raise ValueError("boot_id must be one canonical UUID")
    facts, volume_uuid, device_id, capacity, free = _checked_storage(
        config,
        preflight,
        allow_reserve_exhausted=True,
    )
    del facts, free
    _require_control_socket_absent(control_socket_path)
    schema_before, history_before, layout_before = _read_catalog_metadata(catalog_path)
    if schema_before not in {4, SCHEMA_VERSION}:
        raise RollbackSafetyError("rollback understands only catalog schema 4 or exact schema 5")
    if history_before != _SCHEMA_HISTORY[:schema_before]:
        raise RollbackSafetyError("catalog migration history is not exact")
    if not layout_before:
        raise RollbackSafetyError("catalog schema layout is not exact")
    filesystem = _rooted_filesystem(config, device_id)
    reconciliation_bounds = bounds or ReconciliationBounds()
    passes = 0
    intents_examined = 0
    actions_attempted = 0
    expired_leases = 0
    orphaned = 0
    latch_initialized = False
    with ClipCatalog(catalog_path) as catalog:
        for pass_index in range(1, max_passes + 1):
            passes = pass_index
            writing = catalog.list_writing_clips(limit=MAX_WRITING_PER_PASS + 1)
            for clip in writing[:MAX_WRITING_PER_PASS]:
                orphaned += int(
                    catalog.mark_writing_clip_orphaned(
                        clip.clip_id,
                        monotonic_now_ns=monotonic_ns(),
                    )
                )
            writing_more = len(writing) > MAX_WRITING_PER_PASS
            recovery = catalog.reconcile_startup(
                filesystem,
                monotonic_now_ns=monotonic_ns(),
                boot_id=boot_id.rstrip("\n"),
                bounds=reconciliation_bounds,
            )
            intents_examined += recovery.intents_examined
            actions_attempted += recovery.actions_attempted
            expired_leases += recovery.expired_leases_cleared
            if not writing_more and not recovery.more_work:
                break
        else:
            raise RollbackSafetyError("rollback recovery exceeded its bounded pass budget")

        before_refresh = catalog.inspect_rollback_state(
            filesystem,
            max_finalized_clips=MAX_FINALIZED_CLIPS,
        )
        if before_refresh.latch is None and preflight_refresh is None:
            raise RollbackSafetyError(
                "missing latch requires a fresh post-recovery storage preflight"
            )
        final_preflight = preflight if preflight_refresh is None else preflight_refresh()
        _, final_uuid, final_device, final_capacity, final_free = _checked_storage(
            config,
            final_preflight,
        )
        if (final_uuid, final_device, final_capacity) != (
            volume_uuid,
            device_id,
            capacity,
        ):
            raise RollbackSafetyError("recording volume identity changed during quiesce")
        if final_free < _high_free_bytes(config, final_capacity):
            raise RollbackSafetyError("recording volume is below the rollback high watermark")
        state = catalog.inspect_rollback_state(
            filesystem,
            max_finalized_clips=MAX_FINALIZED_CLIPS,
        )
        if state.latch is None:
            _require_quiescent_state(state)
            initialized = catalog.initialize_rollback_latch_if_quiescent(
                RetentionThresholdLatch(volume_uuid, capacity, False),
                filesystem,
                minimum_free_bytes=_high_free_bytes(config, capacity),
                space_observer=space_observer,
                max_finalized_clips=MAX_FINALIZED_CLIPS,
            )
            if initialized.latch != RetentionThresholdLatch(volume_uuid, capacity, False):
                raise RollbackSafetyError("rollback latch initialization was not exact")
            latch_initialized = True
        schema_after = catalog.schema_version

    guard = run_pre_camera_guard(
        config,
        final_preflight,
        catalog_path=catalog_path,
        control_socket_path=control_socket_path,
        space_observer=space_observer,
    )
    return RollbackQuiesceReport(
        schema_before=schema_before,
        schema_after=schema_after,
        passes=passes,
        intents_examined=intents_examined,
        actions_attempted=actions_attempted,
        expired_leases_cleared=expired_leases,
        orphaned_writing_demoted=orphaned,
        latch_initialized=latch_initialized,
        guard=guard,
    )


def read_catalog_schema_version(path: Path) -> int:
    """Read PRAGMA user_version without creating or migrating a catalog."""

    version, history, layout_valid = _read_catalog_metadata(path)
    if version in {4, SCHEMA_VERSION}:
        if history != _SCHEMA_HISTORY[:version]:
            raise RollbackSafetyError("catalog migration history is not exact")
        if not layout_valid:
            raise RollbackSafetyError("catalog schema layout is not exact")
    return version


def _read_catalog_metadata(
    path: Path,
) -> tuple[int, tuple[tuple[int, str], ...], bool]:
    """Read version/history without creating or migrating a catalog."""

    if not isinstance(path, Path):
        raise TypeError("catalog path must be a pathlib.Path")
    try:
        info = path.lstat()
    except OSError as exc:
        raise RollbackSafetyError("catalog is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RollbackSafetyError("catalog must be one regular non-symlink file")
    uri = f"file:{path.as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, isolation_level=None)
        try:
            row = connection.execute("PRAGMA user_version").fetchone()
            history_rows = connection.execute(
                "SELECT version, name FROM schema_migrations ORDER BY version"
            ).fetchall()
            version = int(row[0]) if row is not None else -1
            layout_valid = catalog_schema_layout_matches(connection, version=version)
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise RollbackSafetyError("catalog schema could not be read") from exc
    if row is None:
        raise RollbackSafetyError("catalog omitted its schema version")
    history = tuple((int(item[0]), str(item[1])) for item in history_rows)
    return int(row[0]), history, layout_valid


def read_boot_id(path: Path = _BOOT_ID_PATH) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        value = os.read(descriptor, 64)
    finally:
        os.close(descriptor)
    if _BOOT_ID_RE.fullmatch(value) is None:
        raise RollbackSafetyError("boot ID is not canonical")
    return value.decode("ascii").rstrip("\n")


def _checked_storage(
    config: DashcamConfig,
    preflight: PreflightResult,
    *,
    allow_reserve_exhausted: bool = False,
) -> tuple[object, str, str, int, int]:
    if not isinstance(config, DashcamConfig):
        raise TypeError("config must be a DashcamConfig")
    if not isinstance(preflight, PreflightResult):
        raise RollbackSafetyError("fresh storage preflight is required")
    reserve_only = (
        allow_reserve_exhausted
        and preflight.state is StorageState.EMERGENCY
        and preflight.reasons == (PreflightReason.RESERVE_EXHAUSTED,)
        and not preflight.probe_attempted
        and not preflight.probe_succeeded
    )
    if not preflight.ready and not reserve_only:
        raise RollbackSafetyError("fresh READY storage preflight is required")
    facts = preflight.facts
    if facts is None:
        raise RollbackSafetyError("storage preflight omitted facts")
    mount = facts.mount
    capacity = facts.space.capacity_bytes
    free = facts.space.free_bytes
    if (
        mount.target != config.storage.recording_root
        or mount.uuid is None
        or mount.device_id is None
        or capacity is None
        or free is None
        or capacity <= 0
        or free < 0
        or free > capacity
    ):
        raise RollbackSafetyError("storage identity/capacity evidence is incomplete")
    return facts, mount.uuid, mount.device_id, capacity, free


def _high_free_bytes(config: DashcamConfig, capacity_bytes: int) -> int:
    thresholds = StorageThresholds(
        low_watermark_percent=config.storage.low_watermark_percent,
        high_watermark_percent=config.storage.high_watermark_percent,
        minimum_free_bytes=math.ceil(config.storage.minimum_free_gib * GIB),
        emergency_free_bytes=config.storage.emergency_free_mib * MIB,
    )
    return thresholds.resolve(capacity_bytes).stop_deletion_at_bytes


def _rooted_filesystem(config: DashcamConfig, device_id: str) -> RootedFilesystem:
    try:
        return RootedFilesystem(
            Path(config.storage.recording_root),
            expected_device_id=device_id,
        )
    except (OSError, ValueError) as exc:
        raise RollbackSafetyError("recording root device identity differs") from exc


def _require_control_socket_absent(path: Path) -> None:
    if not isinstance(path, Path) or not path.is_absolute():
        raise ValueError("control socket path must be absolute")
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RollbackSafetyError("control socket absence could not be proven") from exc
    raise RollbackSafetyError("recorder control socket path still exists")


def _require_quiescent_state(state: RollbackCatalogState) -> None:
    if not state.quiescent:
        reasons: list[str] = []
        if state.catalog_schema_version != SCHEMA_VERSION:
            reasons.append("schema version")
        if not state.latch_rows_valid:
            reasons.append("retention latch rows")
        if state.pending_intents:
            reasons.append("pending intents")
        reasons.extend(
            f"{name} lifecycle" for name, count in state.active_lifecycles if count
        )
        if state.unknown_lifecycles:
            reasons.append("unknown lifecycle")
        if state.download_leases:
            reasons.append("download leases")
        if state.pending_next_windows:
            reasons.append("NEXT event windows")
        if state.finalized_clips_truncated:
            reasons.append("finalized clip audit bound")
        if state.pair_problems:
            reasons.append(state.pair_problems[0])
        if not reasons:
            reasons.append("schema history")
        raise RollbackSafetyError("catalog is not rollback-quiescent: " + ", ".join(reasons))


def _main() -> int:
    parser = argparse.ArgumentParser(prog="python -m dashcam.rollback")
    subcommands = parser.add_subparsers(dest="command", required=True)
    quiesce = subcommands.add_parser("quiesce")
    quiesce.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    quiesce.add_argument("--identity", type=Path, default=DEFAULT_IDENTITY_PATH)
    quiesce.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG_PATH)
    quiesce.add_argument("--control-socket", type=Path, default=DEFAULT_CONTROL_SOCKET_PATH)
    quiesce.add_argument("--max-passes", type=int, default=DEFAULT_MAX_PASSES)
    arguments = parser.parse_args()
    try:
        config = load_config(arguments.config)
        preflight = run_live_storage_preflight(config, identity_path=arguments.identity)
        report = quiesce_for_rollback(
            config,
            preflight,
            catalog_path=arguments.catalog,
            control_socket_path=arguments.control_socket,
            boot_id=read_boot_id(),
            preflight_refresh=lambda: run_live_storage_preflight(
                config,
                identity_path=arguments.identity,
            ),
            max_passes=arguments.max_passes,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"ready": False, "error": str(exc)}, sort_keys=True))
        return 1
    payload = asdict(report)
    payload["ready"] = True
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = [
    "RollbackGuardReport",
    "RollbackQuiesceReport",
    "RollbackSafetyError",
    "quiesce_for_rollback",
    "read_catalog_schema_version",
    "run_pre_camera_guard",
]
