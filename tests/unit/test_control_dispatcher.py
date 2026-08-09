from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from dashcam.catalog import (
    CatalogClip,
    CatalogConflictError,
    ClipCatalog,
    ClipNotFoundError,
    EventProtectionResult,
    EventSource,
)
from dashcam.config import (
    ConfigError,
    DashcamConfig,
    default_config,
    load_config,
    write_config_atomic,
)
from dashcam.control.api import ErrorCode
from dashcam.control.dispatcher import OperationCallback, RecorderControlDispatcher
from dashcam.control.socket_server import (
    ControlCommand,
    ControlOperationError,
    ControlRequest,
    JsonValue,
    execute_request,
)
from dashcam.state import ClipLifecycle, DownloadLease, DownloadLeaseError

CURRENT_CLIP_ID = UUID(int=1)


def _clip(
    number: int,
    *,
    protected: bool = False,
    lifecycle: ClipLifecycle = ClipLifecycle.FINALIZED,
    managed: bool = True,
    reconciled: bool = True,
    directory: str = "clips",
) -> CatalogClip:
    return CatalogClip(
        clip_id=UUID(int=number),
        lifecycle=lifecycle,
        video_path=f"{directory}/clip-{number}.mp4",
        sidecar_path=f"{directory}/clip-{number}.json",
        start_monotonic_ns=number * 1_000,
        end_monotonic_ns=number * 1_000 + 900,
        retention_order=number,
        size_bytes=number * 100,
        protected=protected,
        protection_reason="fixture" if protected else None,
        pair_reconciled=reconciled,
        managed=managed,
    )


class FakeCatalog:
    def __init__(self, clips: tuple[CatalogClip, ...] = ()) -> None:
        self.clips = {clip.clip_id: clip for clip in clips}
        self.acquisitions: list[dict[str, object]] = []
        self.releases: list[tuple[UUID, str]] = []
        self.event_calls: list[dict[str, object]] = []
        self.fail_get: Exception | None = None

    def get_clip(self, clip_id: UUID) -> CatalogClip:
        if self.fail_get is not None:
            raise self.fail_get
        try:
            return self.clips[clip_id]
        except KeyError as error:
            raise ClipNotFoundError(str(clip_id)) from error

    def list_clips(self, *, limit: int, after_order: int = -1) -> tuple[CatalogClip, ...]:
        return tuple(
            sorted(
                (clip for clip in self.clips.values() if clip.retention_order > after_order),
                key=lambda clip: (clip.retention_order, str(clip.clip_id)),
            )[:limit]
        )

    def acquire_download_lease(
        self,
        clip_id: UUID,
        *,
        holder: str,
        monotonic_now_ns: int,
        duration_ns: int,
        boot_id: str,
    ) -> DownloadLease:
        clip = self.get_clip(clip_id)
        if clip.download_lease is not None:
            raise DownloadLeaseError("already leased")
        lease = DownloadLease.issue(
            holder=holder,
            monotonic_now_ns=monotonic_now_ns,
            duration_ns=duration_ns,
        )
        self.clips[clip_id] = replace(clip, download_lease=lease, lease_boot_id=boot_id)
        self.acquisitions.append(
            {
                "clip_id": clip_id,
                "holder": holder,
                "duration_ns": duration_ns,
                "boot_id": boot_id,
            }
        )
        return lease

    def release_download_lease(self, clip_id: UUID, *, holder: str) -> None:
        clip = self.get_clip(clip_id)
        if clip.download_lease is not None and clip.download_lease.holder != holder:
            raise DownloadLeaseError("wrong owner")
        self.clips[clip_id] = replace(clip, download_lease=None, lease_boot_id=None)
        self.releases.append((clip_id, holder))

    def prepare_protect(self, clip_id: UUID, *, reason: str, monotonic_now_ns: int) -> UUID | None:
        clip = self.get_clip(clip_id)
        self.clips[clip_id] = replace(clip, protected=True, protection_reason=reason)
        return UUID(int=101)

    def prepare_unprotect(self, clip_id: UUID, *, monotonic_now_ns: int) -> UUID | None:
        clip = self.get_clip(clip_id)
        self.clips[clip_id] = replace(clip, protected=False, protection_reason=None)
        return UUID(int=102)

    def prepare_delete(self, clip_id: UUID, *, monotonic_now_ns: int, boot_id: str) -> UUID:
        clip = self.get_clip(clip_id)
        if clip.protected:
            raise CatalogConflictError("protected")
        self.clips[clip_id] = replace(clip, lifecycle=ClipLifecycle.DELETING)
        return UUID(int=103)

    def trigger_event(
        self,
        current_clip_id: UUID,
        *,
        source: EventSource,
        monotonic_now_ns: int,
        previous_count: int = 2,
        next_count: int = 1,
    ) -> EventProtectionResult:
        self.event_calls.append(
            {
                "current_clip_id": current_clip_id,
                "source": source,
                "monotonic_now_ns": monotonic_now_ns,
                "previous_count": previous_count,
                "next_count": next_count,
            }
        )
        return EventProtectionResult(
            event_id=UUID(int=201),
            protected_clip_ids=(current_clip_id,),
            missing_previous_count=1,
            pending_next_count=next_count,
            queued_intent_ids=(UUID(int=202),),
        )


class ConfigStore:
    def __init__(self) -> None:
        self.value = default_config()
        self.reads = 0
        self.writes: list[DashcamConfig] = []
        self.failure: Exception | None = None

    def read(self) -> DashcamConfig:
        self.reads += 1
        return self.value

    def write(self, value: DashcamConfig) -> None:
        if self.failure is not None:
            raise self.failure
        self.writes.append(value)
        self.value = value


class Clock:
    def __init__(self, value: int = 10_000) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


async def _nothing() -> None:
    return None


def _dispatcher(
    *,
    catalog: FakeCatalog | None = None,
    configs: ConfigStore | None = None,
    clock: Clock | None = None,
    current_clip: UUID | None = CURRENT_CLIP_ID,
    intents: list[UUID] | None = None,
    restart: OperationCallback | None = None,
    prepare_removal: OperationCallback | None = None,
    max_leases: int = 32,
    timeout_s: float = 1,
    lease_duration_ns: int | None = None,
) -> RecorderControlDispatcher:
    selected_catalog = catalog or FakeCatalog((_clip(1),))
    selected_configs = configs or ConfigStore()
    selected_clock = clock or Clock()
    executed_intents = intents if intents is not None else []

    async def execute_intent(intent_id: UUID) -> None:
        executed_intents.append(intent_id)

    async def restart_default() -> None:
        return None

    async def removal_default() -> None:
        return None

    return RecorderControlDispatcher(
        catalog=selected_catalog,
        config_provider=selected_configs.read,
        config_writer=selected_configs.write,
        status_provider=lambda: {
            "state": "RECORDING",
            "session_token": "must-not-leak",
        },
        health_provider=lambda: {"healthy": True, "nested": {"password": "hidden"}},
        current_clip_provider=lambda: current_clip,
        intent_executor=execute_intent,
        restart_callback=restart if restart is not None else restart_default,
        prepare_removal_callback=(
            prepare_removal if prepare_removal is not None else removal_default
        ),
        monotonic_ns=selected_clock,
        boot_id="boot-test",
        download_lease_duration_ns=lease_duration_ns,
        max_active_download_leases=max_leases,
        operation_timeout_s=timeout_s,
    )


def _run(
    dispatcher: RecorderControlDispatcher,
    command: ControlCommand,
    arguments: Mapping[str, object] | None = None,
) -> MappingResult:
    return cast(
        MappingResult,
        asyncio.run(
            dispatcher.dispatch(
                command,
                cast(Mapping[str, JsonValue], arguments or {}),
            )
        ),
    )


MappingResult = dict[str, object]


def test_status_and_health_use_injected_snapshots_and_redact_secrets() -> None:
    dispatcher = _dispatcher()

    status = _run(dispatcher, ControlCommand.STATUS)
    health = _run(dispatcher, ControlCommand.HEALTH)

    assert status == {
        "state": "RECORDING",
        "session_token": {"is_set": True},
        "operation_state": "IDLE",
    }
    assert health == {
        "healthy": True,
        "nested": {"password": {"is_set": True}},
        "operation_state": "IDLE",
    }
    assert "must-not-leak" not in json.dumps(status)
    assert "hidden" not in json.dumps(health)


@pytest.mark.parametrize(
    "command",
    [
        ControlCommand.STATUS,
        ControlCommand.HEALTH,
        ControlCommand.GET_CONFIG,
        ControlCommand.RESTART,
        ControlCommand.PREPARE_REMOVAL,
    ],
)
def test_no_argument_commands_reject_unknown_fields(command: ControlCommand) -> None:
    with pytest.raises(ControlOperationError) as captured:
        _run(_dispatcher(), command, {"path": "../../etc/shadow"})
    assert captured.value.code is ErrorCode.INVALID_REQUEST


def test_get_and_partial_update_config_are_closed_validated_and_atomic() -> None:
    configs = ConfigStore()
    dispatcher = _dispatcher(configs=configs)

    before = _run(dispatcher, ControlCommand.GET_CONFIG)
    updated = _run(
        dispatcher,
        ControlCommand.UPDATE_CONFIG,
        {
            "video": {"bitrate_bps": 9_000_000},
            "time": {"timezone": "UTC"},
            "storage": {"download_lease_timeout_s": 7},
        },
    )

    assert cast(dict[str, object], before["storage"])["download_lease_timeout_s"] == 300
    assert cast(dict[str, object], updated["video"])["bitrate_bps"] == 9_000_000
    assert cast(dict[str, object], updated["storage"])["download_lease_timeout_s"] == 7
    assert configs.value.video.bitrate_bps == 9_000_000
    assert configs.value.time.timezone == "UTC"
    assert configs.value.storage.download_lease_timeout_s == 7
    assert len(configs.writes) == 1
    assert before["schema_version"] == updated["schema_version"] == 1

    for invalid in (
        {"schema_version": 2},
        {"video": {"software_encoder": True}},
        {"video": {"bitrate_bps": True}},
        {"storage": {"recording_root": "../../tmp"}},
    ):
        with pytest.raises(ControlOperationError) as captured:
            _run(dispatcher, ControlCommand.UPDATE_CONFIG, invalid)
        assert captured.value.code is ErrorCode.UNSUPPORTED_CONFIGURATION
    assert len(configs.writes) == 1


def test_failed_atomic_config_writer_preserves_previous_config() -> None:
    configs = ConfigStore()
    configs.failure = ConfigError("simulated atomic write failure at /private/path")
    dispatcher = _dispatcher(configs=configs)

    with pytest.raises(ControlOperationError) as captured:
        _run(
            dispatcher,
            ControlCommand.UPDATE_CONFIG,
            {"video": {"bitrate_bps": 9_000_000}},
        )

    assert captured.value.code is ErrorCode.STORAGE_FAULT
    assert captured.value.retryable
    assert "private" not in str(captured.value)
    assert configs.value == default_config()


def test_clip_list_is_finalized_newest_first_filtered_and_never_exposes_paths() -> None:
    catalog = FakeCatalog(
        (
            _clip(1),
            _clip(2, protected=True, directory="protected"),
            _clip(3),
            _clip(4, lifecycle=ClipLifecycle.WRITING),
        )
    )
    dispatcher = _dispatcher(catalog=catalog)

    page = _run(
        dispatcher,
        ControlCommand.LIST_CLIPS,
        {"limit": 1, "offset": 0, "protected": "true"},
    )
    all_clips = _run(dispatcher, ControlCommand.LIST_CLIPS)

    assert [item["clip_id"] for item in cast(list[dict[str, object]], page["clips"])] == [
        str(UUID(int=2))
    ]
    assert [
        item["retention_order"] for item in cast(list[dict[str, object]], all_clips["clips"])
    ] == [3, 2, 1]
    assert all_clips["total"] == 3
    assert "video_path" not in json.dumps(all_clips)
    assert "sidecar_path" not in json.dumps(all_clips)


@pytest.mark.parametrize(
    "arguments",
    [
        {"limit": True},
        {"limit": 201},
        {"offset": -1},
        {"protected": True},
        {"protected": "yes"},
        {"path": "/srv/dashcam"},
    ],
)
def test_clip_list_rejects_wrong_types_bounds_and_unknowns(
    arguments: dict[str, object],
) -> None:
    with pytest.raises(ControlOperationError) as captured:
        _run(_dispatcher(), ControlCommand.LIST_CLIPS, arguments)
    assert captured.value.code is ErrorCode.INVALID_REQUEST


def test_clip_detail_uses_only_canonical_stable_id_and_hides_managed_paths() -> None:
    dispatcher = _dispatcher()
    result = _run(dispatcher, ControlCommand.GET_CLIP, {"clip_id": str(UUID(int=1))})

    assert result["clip_id"] == str(UUID(int=1))
    assert result["duration_ns"] == 900
    assert "path" not in json.dumps(result)

    for clip_id in ("../etc/passwd", f"{{{UUID(int=1)}}}", f"{UUID(int=1)}/x"):
        with pytest.raises(ControlOperationError) as captured:
            _run(dispatcher, ControlCommand.GET_CLIP, {"clip_id": clip_id})
        assert captured.value.code is ErrorCode.INVALID_REQUEST


def test_download_lease_approves_catalog_pair_path_and_uses_opaque_authority() -> None:
    catalog = FakeCatalog((_clip(1),))
    dispatcher = _dispatcher(catalog=catalog)

    approval = _run(
        dispatcher,
        ControlCommand.ACQUIRE_DOWNLOAD,
        {
            "clip_id": str(UUID(int=1)),
            "member": "video",
            "holder": "web-session",
        },
    )

    assert approval["approved_path"] == "/srv/dashcam/clips/clip-1.mp4"
    lease_id = cast(str, approval["lease_id"])
    assert len(lease_id) == 32
    assert catalog.acquisitions[0]["holder"] != "web-session"
    assert catalog.acquisitions[0]["duration_ns"] == 300_000_000_000
    assert "path" not in catalog.acquisitions[0]

    released = _run(
        dispatcher,
        ControlCommand.RELEASE_DOWNLOAD,
        {"clip_id": str(UUID(int=1)), "lease_id": lease_id},
    )
    repeated = _run(
        dispatcher,
        ControlCommand.RELEASE_DOWNLOAD,
        {"clip_id": str(UUID(int=1)), "lease_id": lease_id},
    )
    assert released["released"] is True
    assert repeated["released"] is False
    assert len(catalog.releases) == 1


def test_download_lease_uses_one_current_config_snapshot_per_acquisition() -> None:
    catalog = FakeCatalog((_clip(1), _clip(2)))
    configs = ConfigStore()
    configs.value = replace(
        configs.value,
        storage=replace(configs.value.storage, download_lease_timeout_s=1),
    )
    dispatcher = _dispatcher(catalog=catalog, configs=configs)

    first = _run(
        dispatcher,
        ControlCommand.ACQUIRE_DOWNLOAD,
        {"clip_id": str(UUID(int=1)), "member": "video", "holder": "first"},
    )
    configs.value = replace(
        configs.value,
        storage=replace(configs.value.storage, download_lease_timeout_s=900),
    )
    second = _run(
        dispatcher,
        ControlCommand.ACQUIRE_DOWNLOAD,
        {"clip_id": str(UUID(int=2)), "member": "video", "holder": "second"},
    )

    assert configs.reads == 2
    assert [item["duration_ns"] for item in catalog.acquisitions] == [
        1_000_000_000,
        900_000_000_000,
    ]
    assert first["expires_at_monotonic_ns"] == 1_000_010_000
    assert second["expires_at_monotonic_ns"] == 900_000_010_000


def test_explicit_download_lease_duration_override_is_preserved() -> None:
    catalog = FakeCatalog((_clip(1),))
    configs = ConfigStore()
    dispatcher = _dispatcher(
        catalog=catalog,
        configs=configs,
        lease_duration_ns=7_000_000_000,
    )

    _run(
        dispatcher,
        ControlCommand.ACQUIRE_DOWNLOAD,
        {"clip_id": str(UUID(int=1)), "member": "video", "holder": "override"},
    )

    assert configs.reads == 1
    assert catalog.acquisitions[0]["duration_ns"] == 7_000_000_000


@pytest.mark.parametrize(
    "clip",
    [
        _clip(1, managed=False),
        _clip(1, reconciled=False),
        _clip(1, lifecycle=ClipLifecycle.WRITING),
        replace(_clip(1), video_path="../escape.mp4"),
        replace(_clip(1), video_path="quarantine/clip-1.mp4"),
    ],
)
def test_download_rejects_unmanaged_unready_and_unsafe_catalog_paths(
    clip: CatalogClip,
) -> None:
    dispatcher = _dispatcher(catalog=FakeCatalog((clip,)))
    with pytest.raises(ControlOperationError) as captured:
        _run(
            dispatcher,
            ControlCommand.ACQUIRE_DOWNLOAD,
            {
                "clip_id": str(UUID(int=1)),
                "member": "video",
                "holder": "web-session",
            },
        )
    assert captured.value.code is ErrorCode.CLIP_BUSY


def test_download_rejects_client_paths_wrong_members_and_bounds_active_leases() -> None:
    catalog = FakeCatalog((_clip(1), _clip(2)))
    dispatcher = _dispatcher(catalog=catalog, max_leases=1)

    for arguments in (
        {
            "clip_id": str(UUID(int=1)),
            "member": "video",
            "holder": "web-session",
            "path": "../../etc/passwd",
        },
        {
            "clip_id": str(UUID(int=1)),
            "member": "sidecar",
            "holder": "web-session",
        },
        {
            "clip_id": str(UUID(int=1)),
            "member": "video",
            "holder": "../unsafe",
        },
    ):
        with pytest.raises(ControlOperationError) as captured:
            _run(dispatcher, ControlCommand.ACQUIRE_DOWNLOAD, arguments)
        assert captured.value.code is ErrorCode.INVALID_REQUEST

    _run(
        dispatcher,
        ControlCommand.ACQUIRE_DOWNLOAD,
        {
            "clip_id": str(UUID(int=1)),
            "member": "metadata",
            "holder": "web-session",
        },
    )
    with pytest.raises(ControlOperationError) as captured:
        _run(
            dispatcher,
            ControlCommand.ACQUIRE_DOWNLOAD,
            {
                "clip_id": str(UUID(int=2)),
                "member": "video",
                "holder": "web-session-2",
            },
        )
    assert captured.value.code is ErrorCode.CONFLICT
    assert captured.value.retryable


@pytest.mark.parametrize(
    ("command", "expected_intent", "protected"),
    [
        (ControlCommand.PROTECT_CLIP, UUID(int=101), True),
        (ControlCommand.UNPROTECT_CLIP, UUID(int=102), False),
    ],
)
def test_protect_and_unprotect_execute_durable_intents(
    command: ControlCommand, expected_intent: UUID, protected: bool
) -> None:
    catalog = FakeCatalog((_clip(1, protected=command is ControlCommand.UNPROTECT_CLIP),))
    intents: list[UUID] = []
    result = _run(
        _dispatcher(catalog=catalog, intents=intents),
        command,
        {"clip_id": str(UUID(int=1))},
    )

    assert intents == [expected_intent]
    assert result["protected"] is protected
    assert result["intent_id"] == str(expected_intent)


def test_delete_is_prepared_and_executed_without_accepting_a_path() -> None:
    catalog = FakeCatalog((_clip(1),))
    intents: list[UUID] = []
    dispatcher = _dispatcher(catalog=catalog, intents=intents)

    result = _run(dispatcher, ControlCommand.DELETE_CLIP, {"clip_id": str(UUID(int=1))})
    assert result == {
        "clip_id": str(UUID(int=1)),
        "intent_id": str(UUID(int=103)),
        "accepted": True,
    }
    assert intents == [UUID(int=103)]

    with pytest.raises(ControlOperationError) as captured:
        _run(
            dispatcher,
            ControlCommand.DELETE_CLIP,
            {"clip_id": str(UUID(int=1)), "path": "clips/clip-1.mp4"},
        )
    assert captured.value.code is ErrorCode.INVALID_REQUEST


def test_event_uses_recorder_current_clip_and_configured_window() -> None:
    catalog = FakeCatalog((_clip(1),))
    dispatcher = _dispatcher(catalog=catalog)

    result = _run(dispatcher, ControlCommand.EVENT, {"source": "web"})

    assert result["event_id"] == str(UUID(int=201))
    assert result["protected_clip_ids"] == [str(UUID(int=1))]
    assert catalog.event_calls == [
        {
            "current_clip_id": UUID(int=1),
            "source": EventSource.WEB,
            "monotonic_now_ns": 10_000,
            "previous_count": 2,
            "next_count": 1,
        }
    ]

    with pytest.raises(ControlOperationError) as captured:
        _run(_dispatcher(current_clip=None), ControlCommand.EVENT, {"source": "web"})
    assert captured.value.code is ErrorCode.CONFLICT

    with pytest.raises(ControlOperationError) as captured:
        _run(dispatcher, ControlCommand.EVENT, {"source": "gpio"})
    assert captured.value.code is ErrorCode.INVALID_REQUEST


def test_restart_has_explicit_in_progress_state_and_conflicts() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def restart() -> dict[str, object]:
            entered.set()
            await release.wait()
            return {"accepted": True}

        dispatcher = _dispatcher(restart=restart)
        task = asyncio.create_task(dispatcher.dispatch(ControlCommand.RESTART, {}))
        await entered.wait()
        status = await dispatcher.dispatch(ControlCommand.STATUS, {})
        assert status["operation_state"] == "RESTARTING"
        with pytest.raises(ControlOperationError) as captured:
            await dispatcher.dispatch(ControlCommand.PREPARE_REMOVAL, {})
        assert captured.value.code is ErrorCode.CONFLICT
        assert captured.value.retryable
        release.set()
        result = await task
        assert result == {"accepted": True, "operation_state": "IDLE"}

    asyncio.run(scenario())


def test_restart_timeout_maps_to_stable_error_and_returns_to_idle() -> None:
    async def restart() -> None:
        await asyncio.Event().wait()

    dispatcher = _dispatcher(restart=restart, timeout_s=0.05)
    with pytest.raises(ControlOperationError) as captured:
        _run(dispatcher, ControlCommand.RESTART)
    assert captured.value.code is ErrorCode.OPERATION_TIMEOUT
    assert captured.value.retryable
    assert _run(dispatcher, ControlCommand.STATUS)["operation_state"] == "IDLE"


def test_prepare_removal_enters_terminal_state_and_rejects_new_mutations() -> None:
    async def prepare() -> dict[str, object]:
        return {"accepted": True, "helper_secret": "do-not-leak"}

    dispatcher = _dispatcher(prepare_removal=prepare)
    result = _run(dispatcher, ControlCommand.PREPARE_REMOVAL)

    assert result == {
        "accepted": True,
        "helper_secret": {"is_set": True},
        "operation_state": "SHUTTING_DOWN",
    }
    with pytest.raises(ControlOperationError) as captured:
        _run(
            dispatcher,
            ControlCommand.PROTECT_CLIP,
            {"clip_id": str(UUID(int=1))},
        )
    assert captured.value.code is ErrorCode.CONFLICT
    with pytest.raises(ControlOperationError) as captured:
        _run(
            dispatcher,
            ControlCommand.ACQUIRE_DOWNLOAD,
            {
                "clip_id": str(UUID(int=1)),
                "member": "video",
                "holder": "web-session",
            },
        )
    assert captured.value.code is ErrorCode.CONFLICT
    assert _run(dispatcher, ControlCommand.HEALTH)["operation_state"] == "SHUTTING_DOWN"


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (ClipNotFoundError("private/path"), ErrorCode.NOT_FOUND),
        (DownloadLeaseError("private/path"), ErrorCode.CLIP_BUSY),
        (CatalogConflictError("private/path"), ErrorCode.CONFLICT),
        (OSError("private/path"), ErrorCode.STORAGE_FAULT),
    ],
)
def test_domain_failures_map_to_stable_non_leaking_errors(
    failure: Exception, expected: ErrorCode
) -> None:
    catalog = FakeCatalog((_clip(1),))
    catalog.fail_get = failure
    with pytest.raises(ControlOperationError) as captured:
        _run(
            _dispatcher(catalog=catalog),
            ControlCommand.GET_CLIP,
            {"clip_id": str(UUID(int=1))},
        )
    assert captured.value.code is expected
    assert "private" not in str(captured.value)


def test_dispatcher_round_trips_through_socket_error_contract() -> None:
    dispatcher = _dispatcher(catalog=FakeCatalog())
    request = ControlRequest(
        request_id=UUID(int=501),
        command=ControlCommand.GET_CLIP,
        arguments={"clip_id": str(UUID(int=999))},
    )

    response = json.loads(asyncio.run(execute_request(request, dispatcher)))

    assert response == {
        "version": 1,
        "request_id": str(UUID(int=501)),
        "ok": False,
        "error": {
            "code": "NOT_FOUND",
            "message": "Clip not found",
            "retryable": False,
        },
    }


@pytest.mark.integration
def test_dispatcher_integrates_real_atomic_config_and_catalog_contracts(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "dashcam.toml"
    write_config_atomic(config_path, default_config())
    catalog = ClipCatalog(tmp_path / "catalog.sqlite3")
    catalog.register_clip(_clip(1), catalog_now_ns=1)
    catalog.register_clip(_clip(2), catalog_now_ns=2)

    async def execute_intent(_intent_id: UUID) -> None:
        return None

    async def operation() -> None:
        return None

    dispatcher = RecorderControlDispatcher(
        catalog=catalog,
        config_provider=lambda: load_config(config_path),
        config_writer=lambda config: write_config_atomic(config_path, config),
        status_provider=lambda: {"state": "RECORDING"},
        health_provider=lambda: {"healthy": True},
        current_clip_provider=lambda: UUID(int=2),
        intent_executor=execute_intent,
        restart_callback=operation,
        prepare_removal_callback=operation,
        monotonic_ns=lambda: 100,
        boot_id="boot-real",
    )
    try:
        updated = _run(
            dispatcher,
            ControlCommand.UPDATE_CONFIG,
            {"video": {"bitrate_bps": 9_500_000}},
        )
        listed = _run(dispatcher, ControlCommand.LIST_CLIPS)
        approved = _run(
            dispatcher,
            ControlCommand.ACQUIRE_DOWNLOAD,
            {
                "clip_id": str(UUID(int=2)),
                "member": "metadata",
                "holder": "web-integration",
            },
        )
        released = _run(
            dispatcher,
            ControlCommand.RELEASE_DOWNLOAD,
            {
                "clip_id": str(UUID(int=2)),
                "lease_id": cast(str, approved["lease_id"]),
            },
        )
    finally:
        catalog.close()

    assert cast(dict[str, object], updated["video"])["bitrate_bps"] == 9_500_000
    assert load_config(config_path).video.bitrate_bps == 9_500_000
    assert [item["clip_id"] for item in cast(list[dict[str, object]], listed["clips"])] == [
        str(UUID(int=2)),
        str(UUID(int=1)),
    ]
    assert approved["approved_path"] == "/srv/dashcam/clips/clip-2.json"
    assert released["released"] is True
