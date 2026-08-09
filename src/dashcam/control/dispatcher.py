"""Hardware-independent implementation of the recorder control command set.

All durable or privileged effects are supplied through narrow injected
interfaces.  This module validates the closed socket command vocabulary,
coordinates bounded operations, and converts catalog/configuration failures to
stable public errors.
"""

from __future__ import annotations

import asyncio
import copy
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Final, Protocol, cast
from uuid import UUID, uuid4

from dashcam.catalog import (
    MAX_QUERY_ROWS,
    CatalogClip,
    CatalogConflictError,
    CatalogError,
    ClipNotFoundError,
    EventProtectionResult,
    EventSource,
)
from dashcam.config import (
    ConfigError,
    DashcamConfig,
    config_from_mapping,
    config_to_mapping,
)
from dashcam.control.api import ErrorCode, RedactedSecret, parse_clip_id
from dashcam.control.socket_server import (
    ControlCommand,
    ControlOperationError,
    JsonValue,
)
from dashcam.state import ClipLifecycle, DownloadLease, DownloadLeaseError

MAX_LIST_LIMIT: Final = 200
MAX_LIST_OFFSET: Final = 1_000_000
MAX_ACTIVE_DOWNLOAD_LEASES: Final = 32
DEFAULT_DOWNLOAD_LEASE_NS: Final = 5 * 60 * 1_000_000_000
DEFAULT_OPERATION_TIMEOUT_S: Final = 5.0
_MANAGED_DOWNLOAD_DIRECTORIES: Final = frozenset({"clips", "protected"})
_LEASE_ID_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{15,127}")
_BOOT_ID_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SECRET_MARKERS: Final = ("password", "passphrase", "secret", "credential", "token")


class ControlOperationState(StrEnum):
    """One explicit recorder-side administrative-operation state."""

    IDLE = "IDLE"
    RESTARTING = "RESTARTING"
    PREPARING_REMOVAL = "PREPARING_REMOVAL"
    SHUTTING_DOWN = "SHUTTING_DOWN"


class CatalogBackend(Protocol):
    """Subset of the durable catalog used by the control plane."""

    def get_clip(self, clip_id: UUID) -> CatalogClip: ...

    def list_clips(self, *, limit: int, after_order: int = -1) -> tuple[CatalogClip, ...]: ...

    def acquire_download_lease(
        self,
        clip_id: UUID,
        *,
        holder: str,
        monotonic_now_ns: int,
        duration_ns: int,
        boot_id: str,
    ) -> DownloadLease: ...

    def release_download_lease(self, clip_id: UUID, *, holder: str) -> None: ...

    def prepare_protect(
        self, clip_id: UUID, *, reason: str, monotonic_now_ns: int
    ) -> UUID | None: ...

    def prepare_unprotect(self, clip_id: UUID, *, monotonic_now_ns: int) -> UUID | None: ...

    def prepare_delete(self, clip_id: UUID, *, monotonic_now_ns: int, boot_id: str) -> UUID: ...

    def trigger_event(
        self,
        current_clip_id: UUID,
        *,
        source: EventSource,
        monotonic_now_ns: int,
        previous_count: int = 2,
        next_count: int = 1,
        event_id: UUID | None = None,
    ) -> EventProtectionResult: ...


class PublicSnapshot(Protocol):
    """A provider-owned immutable snapshot with a JSON-safe representation."""

    def as_dict(self) -> dict[str, object]: ...


ConfigProvider = Callable[[], DashcamConfig]
ConfigWriter = Callable[[DashcamConfig], object]
SnapshotProvider = Callable[[], Mapping[str, object] | PublicSnapshot]
IntentExecutor = Callable[[UUID], Awaitable[None]]
EventExecutor = Callable[
    [EventSource, int, int, int, UUID],
    Awaitable[EventProtectionResult],
]
OperationCallback = Callable[[], Awaitable[Mapping[str, object] | None]]


@dataclass(frozen=True, slots=True)
class _IssuedLease:
    clip_id: UUID
    catalog_holder: str
    expires_at_monotonic_ns: int


class RecorderControlDispatcher:
    """Validate and execute the complete version-1 recorder command set."""

    def __init__(
        self,
        *,
        catalog: CatalogBackend,
        config_provider: ConfigProvider,
        config_writer: ConfigWriter,
        status_provider: SnapshotProvider,
        health_provider: SnapshotProvider,
        intent_executor: IntentExecutor,
        event_executor: EventExecutor,
        restart_callback: OperationCallback,
        prepare_removal_callback: OperationCallback,
        monotonic_ns: Callable[[], int],
        boot_id: str,
        download_lease_duration_ns: int | None = None,
        max_active_download_leases: int = MAX_ACTIVE_DOWNLOAD_LEASES,
        operation_timeout_s: float = DEFAULT_OPERATION_TIMEOUT_S,
    ) -> None:
        if not isinstance(boot_id, str) or _BOOT_ID_RE.fullmatch(boot_id) is None:
            raise ValueError("boot_id must be a bounded safe identifier")
        if download_lease_duration_ns is not None and (
            isinstance(download_lease_duration_ns, bool)
            or not isinstance(download_lease_duration_ns, int)
            or not 1_000_000_000 <= download_lease_duration_ns <= 15 * 60 * 1_000_000_000
        ):
            raise ValueError("download lease duration must be between 1 and 900 seconds")
        if (
            isinstance(max_active_download_leases, bool)
            or not isinstance(max_active_download_leases, int)
            or not 1 <= max_active_download_leases <= MAX_ACTIVE_DOWNLOAD_LEASES
        ):
            raise ValueError(
                f"max active leases must be between 1 and {MAX_ACTIVE_DOWNLOAD_LEASES}"
            )
        if (
            isinstance(operation_timeout_s, bool)
            or not isinstance(operation_timeout_s, int | float)
            or not math.isfinite(operation_timeout_s)
            or not 0.05 <= operation_timeout_s <= 30
        ):
            raise ValueError("operation timeout must be between 0.05 and 30 seconds")

        self._catalog = catalog
        self._config_provider = config_provider
        self._config_writer = config_writer
        self._status_provider = status_provider
        self._health_provider = health_provider
        self._intent_executor = intent_executor
        self._event_executor = event_executor
        self._restart_callback = restart_callback
        self._prepare_removal_callback = prepare_removal_callback
        self._monotonic_ns = monotonic_ns
        self._boot_id = boot_id
        self._lease_duration_override_ns = download_lease_duration_ns
        self._max_leases = max_active_download_leases
        self._operation_timeout_s = float(operation_timeout_s)
        self._leases: dict[str, _IssuedLease] = {}
        self._lease_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._operation_state = ControlOperationState.IDLE
        self._active_mutations = 0

    async def dispatch(
        self, command: ControlCommand, arguments: Mapping[str, JsonValue]
    ) -> Mapping[str, object]:
        """Execute one validated command and return a secret-safe mapping."""

        if not isinstance(command, ControlCommand):
            raise ControlOperationError(ErrorCode.INVALID_REQUEST, "Invalid recorder command")
        if not isinstance(arguments, Mapping):
            raise ControlOperationError(ErrorCode.INVALID_REQUEST, "Arguments must be an object")
        try:
            return await self._dispatch(command, arguments)
        except ControlOperationError:
            raise
        except ClipNotFoundError as error:
            raise ControlOperationError(ErrorCode.NOT_FOUND, "Clip not found") from error
        except DownloadLeaseError as error:
            raise ControlOperationError(
                ErrorCode.CLIP_BUSY, "Clip is unavailable for download", retryable=True
            ) from error
        except CatalogConflictError as error:
            raise ControlOperationError(
                ErrorCode.CONFLICT, "Clip operation conflicts with current state"
            ) from error
        except CatalogError as error:
            raise ControlOperationError(
                ErrorCode.RECORDER_FAULT, "Recorder catalog operation failed", retryable=True
            ) from error
        except ConfigError as error:
            raise ControlOperationError(
                ErrorCode.UNSUPPORTED_CONFIGURATION, "Configuration update is invalid"
            ) from error
        except OSError as error:
            raise ControlOperationError(
                ErrorCode.STORAGE_FAULT, "Recorder storage operation failed", retryable=True
            ) from error

    async def _dispatch(
        self, command: ControlCommand, arguments: Mapping[str, JsonValue]
    ) -> Mapping[str, object]:
        if command is ControlCommand.STATUS:
            _exact_arguments(arguments, set())
            return await self._snapshot(self._status_provider)
        if command is ControlCommand.HEALTH:
            _exact_arguments(arguments, set())
            return await self._snapshot(self._health_provider)
        if command is ControlCommand.GET_CONFIG:
            _exact_arguments(arguments, set())
            config = await asyncio.to_thread(self._config_provider)
            return cast(dict[str, object], _public_value(config_to_mapping(config)))
        if command is ControlCommand.UPDATE_CONFIG:
            return await self._update_config(arguments)
        if command is ControlCommand.LIST_CLIPS:
            return await self._list_clips(arguments)
        if command is ControlCommand.GET_CLIP:
            _exact_arguments(arguments, {"clip_id"})
            clip = await asyncio.to_thread(self._catalog.get_clip, _clip_id(arguments))
            return _clip_payload(clip)
        if command is ControlCommand.ACQUIRE_DOWNLOAD:
            await self._begin_mutation()
            try:
                return await self._acquire_download(arguments)
            finally:
                await self._end_mutation()
        if command is ControlCommand.RELEASE_DOWNLOAD:
            return await self._release_download(arguments)
        if command in {
            ControlCommand.PROTECT_CLIP,
            ControlCommand.UNPROTECT_CLIP,
            ControlCommand.DELETE_CLIP,
            ControlCommand.EVENT,
        }:
            return await self._mutate(command, arguments)
        if command is ControlCommand.RESTART:
            _exact_arguments(arguments, set())
            return await self._run_operation(
                ControlOperationState.RESTARTING,
                self._restart_callback,
                terminal_state=ControlOperationState.IDLE,
            )
        if command is ControlCommand.PREPARE_REMOVAL:
            _exact_arguments(arguments, set())
            return await self._run_operation(
                ControlOperationState.PREPARING_REMOVAL,
                self._prepare_removal_callback,
                terminal_state=ControlOperationState.SHUTTING_DOWN,
            )
        raise ControlOperationError(ErrorCode.INVALID_REQUEST, "Invalid recorder command")

    async def _snapshot(self, provider: SnapshotProvider) -> dict[str, object]:
        snapshot = await asyncio.to_thread(provider)
        raw = snapshot if isinstance(snapshot, Mapping) else snapshot.as_dict()
        public = _public_mapping(raw)
        async with self._state_lock:
            public["operation_state"] = self._operation_state.value
        return public

    async def _update_config(self, arguments: Mapping[str, JsonValue]) -> dict[str, object]:
        if not arguments:
            raise _invalid("Configuration update cannot be empty")
        await self._begin_mutation()
        try:
            current = await asyncio.to_thread(self._config_provider)
            current_mapping = config_to_mapping(current)
            candidate_mapping = _merge_closed(current_mapping, arguments, path="configuration")
            candidate = config_from_mapping(candidate_mapping)
            try:
                await asyncio.to_thread(self._config_writer, candidate)
            except ConfigError as error:
                raise ControlOperationError(
                    ErrorCode.STORAGE_FAULT,
                    "Configuration could not be stored atomically",
                    retryable=True,
                ) from error
            return cast(dict[str, object], _public_value(config_to_mapping(candidate)))
        finally:
            await self._end_mutation()

    async def _list_clips(self, arguments: Mapping[str, JsonValue]) -> dict[str, object]:
        _allowed_arguments(arguments, {"limit", "offset", "protected"})
        limit = _bounded_int(arguments.get("limit", 50), "limit", 1, MAX_LIST_LIMIT)
        offset = _bounded_int(arguments.get("offset", 0), "offset", 0, MAX_LIST_OFFSET)
        protected_value = arguments.get("protected", "all")
        if protected_value not in {"all", "true", "false"}:
            raise _invalid("protected must be all, true, or false")
        rows = await asyncio.to_thread(self._catalog.list_clips, limit=MAX_QUERY_ROWS)
        finalized = [clip for clip in rows if clip.lifecycle is ClipLifecycle.FINALIZED]
        if protected_value != "all":
            required = protected_value == "true"
            finalized = [clip for clip in finalized if clip.protected is required]
        finalized.sort(key=lambda clip: (clip.retention_order, str(clip.clip_id)), reverse=True)
        selected = finalized[offset : offset + limit]
        return {
            "clips": [_clip_payload(clip) for clip in selected],
            "limit": limit,
            "offset": offset,
            "total": len(finalized),
            "truncated": len(rows) == MAX_QUERY_ROWS,
        }

    async def _acquire_download(self, arguments: Mapping[str, JsonValue]) -> dict[str, object]:
        _exact_arguments(arguments, {"clip_id", "member", "holder"})
        clip_id = _clip_id(arguments)
        member = _required_string(arguments, "member", maximum=16)
        if member not in {"video", "metadata"}:
            raise _invalid("member must be video or metadata")
        # Validate the caller label but never use it as catalog lease authority.
        _safe_identifier(arguments, "holder", minimum=1, maximum=128)
        now_ns = _now(self._monotonic_ns)

        async with self._lease_lock:
            self._expire_local_leases(now_ns)
            if len(self._leases) >= self._max_leases:
                raise ControlOperationError(
                    ErrorCode.CONFLICT, "Download lease limit reached", retryable=True
                )
            clip = await asyncio.to_thread(self._catalog.get_clip, clip_id)
            relative_path = _approved_member_path(clip, member)
            config = await asyncio.to_thread(self._config_provider)
            approved_path = _absolute_managed_path(config, relative_path)
            lease_duration_ns = self._lease_duration_override_ns
            if lease_duration_ns is None:
                lease_duration_ns = config.storage.download_lease_timeout_s * 1_000_000_000
            lease_id = uuid4().hex
            catalog_holder = f"control-{lease_id}"
            lease = await asyncio.to_thread(
                self._catalog.acquire_download_lease,
                clip_id,
                holder=catalog_holder,
                monotonic_now_ns=now_ns,
                duration_ns=lease_duration_ns,
                boot_id=self._boot_id,
            )
            self._leases[lease_id] = _IssuedLease(
                clip_id=clip_id,
                catalog_holder=catalog_holder,
                expires_at_monotonic_ns=lease.expires_at_monotonic_ns,
            )
        return {
            "clip_id": str(clip_id),
            "lease_id": lease_id,
            "approved_path": approved_path,
            "expires_at_monotonic_ns": lease.expires_at_monotonic_ns,
        }

    async def _release_download(self, arguments: Mapping[str, JsonValue]) -> dict[str, object]:
        _exact_arguments(arguments, {"clip_id", "lease_id"})
        clip_id = _clip_id(arguments)
        lease_id = _safe_identifier(arguments, "lease_id", minimum=16, maximum=128)
        if _LEASE_ID_RE.fullmatch(lease_id) is None:
            raise _invalid("lease_id must be a bounded safe identifier")
        now_ns = _now(self._monotonic_ns)
        async with self._lease_lock:
            self._expire_local_leases(now_ns)
            issued = self._leases.get(lease_id)
            if issued is None:
                return {"clip_id": str(clip_id), "released": False}
            if issued.clip_id != clip_id:
                raise ControlOperationError(
                    ErrorCode.CONFLICT, "Download lease does not belong to clip"
                )
            await asyncio.to_thread(
                self._catalog.release_download_lease,
                clip_id,
                holder=issued.catalog_holder,
            )
            del self._leases[lease_id]
        return {"clip_id": str(clip_id), "released": True}

    async def _mutate(
        self, command: ControlCommand, arguments: Mapping[str, JsonValue]
    ) -> dict[str, object]:
        await self._begin_mutation()
        try:
            now_ns = _now(self._monotonic_ns)
            if command is ControlCommand.EVENT:
                _exact_arguments(arguments, {"source", "event_id"})
                if arguments["source"] != EventSource.WEB.value:
                    raise _invalid("event source must be web")
                event_id = _canonical_uuid(arguments, "event_id")
                config = await asyncio.to_thread(self._config_provider)
                event = await self._event_executor(
                    EventSource.WEB,
                    now_ns,
                    config.storage.protect_previous_clips,
                    config.storage.protect_next_clips,
                    event_id,
                )
                return _event_payload(event)

            _exact_arguments(arguments, {"clip_id"})
            clip_id = _clip_id(arguments)
            intent_id: UUID | None
            if command is ControlCommand.PROTECT_CLIP:
                intent_id = await asyncio.to_thread(
                    self._catalog.prepare_protect,
                    clip_id,
                    reason="manual:web",
                    monotonic_now_ns=now_ns,
                )
            elif command is ControlCommand.UNPROTECT_CLIP:
                intent_id = await asyncio.to_thread(
                    self._catalog.prepare_unprotect,
                    clip_id,
                    monotonic_now_ns=now_ns,
                )
            else:
                intent_id = await asyncio.to_thread(
                    self._catalog.prepare_delete,
                    clip_id,
                    monotonic_now_ns=now_ns,
                    boot_id=self._boot_id,
                )
            if intent_id is not None:
                await self._execute_intent(intent_id)
            if command is ControlCommand.DELETE_CLIP:
                return {
                    "clip_id": str(clip_id),
                    "intent_id": str(intent_id),
                    "accepted": True,
                }
            clip = await asyncio.to_thread(self._catalog.get_clip, clip_id)
            payload = _clip_payload(clip)
            payload["intent_id"] = None if intent_id is None else str(intent_id)
            return payload
        finally:
            await self._end_mutation()

    async def _execute_intent(self, intent_id: UUID) -> None:
        try:
            await asyncio.wait_for(
                self._intent_executor(intent_id), timeout=self._operation_timeout_s
            )
        except TimeoutError as error:
            raise ControlOperationError(
                ErrorCode.OPERATION_TIMEOUT,
                "Clip operation timed out; durable recovery remains pending",
                retryable=True,
            ) from error
        except (ControlOperationError, CatalogError, OSError):
            raise
        except Exception as error:
            raise ControlOperationError(
                ErrorCode.RECORDER_FAULT,
                "Clip operation could not be completed",
                retryable=True,
            ) from error

    async def _run_operation(
        self,
        state: ControlOperationState,
        callback: OperationCallback,
        *,
        terminal_state: ControlOperationState,
    ) -> dict[str, object]:
        async with self._state_lock:
            if self._operation_state is not ControlOperationState.IDLE or self._active_mutations:
                raise ControlOperationError(
                    ErrorCode.CONFLICT, "Another recorder operation is active", retryable=True
                )
            self._operation_state = state
        try:
            try:
                detail = await asyncio.wait_for(callback(), timeout=self._operation_timeout_s)
            except TimeoutError as error:
                raise ControlOperationError(
                    ErrorCode.OPERATION_TIMEOUT,
                    "Recorder operation timed out",
                    retryable=True,
                ) from error
            result = {} if detail is None else _public_mapping(detail)
        except asyncio.CancelledError:
            async with self._state_lock:
                self._operation_state = ControlOperationState.IDLE
            raise
        except ControlOperationError:
            async with self._state_lock:
                self._operation_state = ControlOperationState.IDLE
            raise
        except Exception as error:
            async with self._state_lock:
                self._operation_state = ControlOperationState.IDLE
            raise ControlOperationError(
                ErrorCode.RECORDER_FAULT,
                "Recorder operation failed",
                retryable=True,
            ) from error
        async with self._state_lock:
            self._operation_state = terminal_state
        result["operation_state"] = terminal_state.value
        return result

    async def _begin_mutation(self) -> None:
        async with self._state_lock:
            if self._operation_state is not ControlOperationState.IDLE:
                raise ControlOperationError(
                    ErrorCode.CONFLICT,
                    "Recorder is not accepting new operations",
                    retryable=True,
                )
            self._active_mutations += 1

    async def _end_mutation(self) -> None:
        async with self._state_lock:
            self._active_mutations -= 1

    def _expire_local_leases(self, monotonic_now_ns: int) -> None:
        expired = [
            lease_id
            for lease_id, lease in self._leases.items()
            if lease.expires_at_monotonic_ns <= monotonic_now_ns
        ]
        for lease_id in expired:
            del self._leases[lease_id]


def _invalid(message: str) -> ControlOperationError:
    return ControlOperationError(ErrorCode.INVALID_REQUEST, message)


def _allowed_arguments(arguments: Mapping[str, JsonValue], allowed: set[str]) -> None:
    unknown = set(arguments) - allowed
    if unknown:
        raise _invalid("Command contains unknown arguments")


def _exact_arguments(arguments: Mapping[str, JsonValue], expected: set[str]) -> None:
    _allowed_arguments(arguments, expected)
    if set(arguments) != expected:
        raise _invalid("Command is missing required arguments")


def _required_string(arguments: Mapping[str, JsonValue], name: str, *, maximum: int) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value or len(value) > maximum or not value.isprintable():
        raise _invalid(f"{name} must be a bounded printable string")
    return value


def _safe_identifier(
    arguments: Mapping[str, JsonValue], name: str, *, minimum: int, maximum: int
) -> str:
    value = _required_string(arguments, name, maximum=maximum)
    if (
        len(value) < minimum
        or not value.isascii()
        or not value[0].isalnum()
        or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
            for character in value
        )
    ):
        raise _invalid(f"{name} must be a bounded safe identifier")
    return value


def _bounded_int(value: JsonValue, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise _invalid(f"{name} must be an integer between {low} and {high}")
    return value


def _clip_id(arguments: Mapping[str, JsonValue]) -> UUID:
    value = arguments.get("clip_id")
    if not isinstance(value, str):
        raise _invalid("clip_id must be a canonical UUID")
    try:
        return parse_clip_id(value)
    except ValueError as error:
        raise _invalid("clip_id must be a canonical UUID") from error


def _canonical_uuid(arguments: Mapping[str, JsonValue], name: str) -> UUID:
    value = arguments.get(name)
    if not isinstance(value, str):
        raise _invalid(f"{name} must be a canonical UUID")
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise _invalid(f"{name} must be a canonical UUID") from error
    if str(parsed) != value:
        raise _invalid(f"{name} must be a canonical UUID")
    return parsed


def _now(provider: Callable[[], int]) -> int:
    value = provider()
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError("monotonic clock returned an invalid value")
    return value


def _merge_closed(
    current: Mapping[str, object],
    updates: Mapping[str, object],
    *,
    path: str,
) -> dict[str, object]:
    result = copy.deepcopy(dict(current))
    for key, value in updates.items():
        if key not in current or (path == "configuration" and key == "schema_version"):
            raise ConfigError(f"{path} contains an unknown or immutable key")
        existing = current[key]
        if isinstance(existing, Mapping):
            if not isinstance(value, Mapping):
                raise ConfigError(f"{path}.{key} must be a table")
            result[key] = _merge_closed(existing, value, path=f"{path}.{key}")
        else:
            if isinstance(value, Mapping | list) or value is None:
                raise ConfigError(f"{path}.{key} has an invalid value type")
            result[key] = copy.deepcopy(value)
    return result


def _public_mapping(value: Mapping[str, object]) -> dict[str, object]:
    converted = _public_value(value)
    if not isinstance(converted, dict):
        raise TypeError("public snapshot must be a mapping")
    return converted


def _public_value(value: object, *, key: str | None = None, depth: int = 0) -> object:
    if depth > 12:
        raise ValueError("public value exceeds nesting bound")
    if key is not None and any(marker in key.casefold() for marker in _SECRET_MARKERS):
        is_set = value is not None and value != "" and value is not False
        return RedactedSecret(is_set=is_set).as_dict()
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("public numbers must be finite")
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for item_key, item in value.items():
            if not isinstance(item_key, str) or not item_key or len(item_key) > 128:
                raise ValueError("public mapping key is invalid")
            result[item_key] = _public_value(item, key=item_key, depth=depth + 1)
        return result
    if isinstance(value, tuple | list):
        return [_public_value(item, depth=depth + 1) for item in value]
    raise TypeError("public value is not JSON-compatible")


def _clip_payload(clip: CatalogClip) -> dict[str, object]:
    duration_ns = (
        None if clip.end_monotonic_ns is None else clip.end_monotonic_ns - clip.start_monotonic_ns
    )
    return {
        "clip_id": str(clip.clip_id),
        "lifecycle": clip.lifecycle.value,
        "start_monotonic_ns": clip.start_monotonic_ns,
        "end_monotonic_ns": clip.end_monotonic_ns,
        "duration_ns": duration_ns,
        "retention_order": clip.retention_order,
        "size_bytes": clip.size_bytes,
        "protected": clip.protected,
        "protection_reason": clip.protection_reason,
        "pair_reconciled": clip.pair_reconciled,
        "managed": clip.managed,
        "download_active": clip.download_lease is not None,
    }


def _approved_member_path(clip: CatalogClip, member: str) -> str:
    if (
        clip.lifecycle is not ClipLifecycle.FINALIZED
        or not clip.managed
        or not clip.pair_reconciled
    ):
        raise DownloadLeaseError("clip is not a downloadable managed pair")
    relative = clip.video_path if member == "video" else clip.sidecar_path
    path = PurePosixPath(relative)
    expected_suffix = ".mp4" if member == "video" else ".json"
    if (
        path.is_absolute()
        or len(path.parts) != 2
        or path.parts[0] not in _MANAGED_DOWNLOAD_DIRECTORIES
        or path.name in {"", ".", ".."}
        or path.suffix != expected_suffix
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise DownloadLeaseError("catalog path is outside the managed namespace")
    return path.as_posix()


def _absolute_managed_path(config: DashcamConfig, relative_path: str) -> str:
    root = PurePosixPath(config.storage.recording_root)
    if not root.is_absolute() or ".." in root.parts:
        raise ConfigError("recording root is invalid")
    result = root.joinpath(*PurePosixPath(relative_path).parts)
    return result.as_posix()


def _event_payload(event: EventProtectionResult) -> dict[str, object]:
    return {
        "event_id": str(event.event_id),
        "protected_clip_ids": [str(clip_id) for clip_id in event.protected_clip_ids],
        "missing_previous_count": event.missing_previous_count,
        "pending_next_count": event.pending_next_count,
        "queued_intent_ids": [str(intent_id) for intent_id in event.queued_intent_ids],
    }


__all__ = [
    "DEFAULT_DOWNLOAD_LEASE_NS",
    "DEFAULT_OPERATION_TIMEOUT_S",
    "MAX_ACTIVE_DOWNLOAD_LEASES",
    "MAX_LIST_LIMIT",
    "MAX_LIST_OFFSET",
    "CatalogBackend",
    "ConfigProvider",
    "ConfigWriter",
    "ControlOperationState",
    "IntentExecutor",
    "OperationCallback",
    "PublicSnapshot",
    "RecorderControlDispatcher",
    "SnapshotProvider",
]
