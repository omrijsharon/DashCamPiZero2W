"""Production composition for the recorder-owned local control listener."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID

from dashcam.catalog import EventProtectionResult, EventSource
from dashcam.config import DashcamConfig, load_config
from dashcam.control.api import ErrorCode
from dashcam.control.dispatcher import CatalogBackend, RecorderControlDispatcher
from dashcam.control.socket_server import (
    DEFAULT_CONTROL_SOCKET_PATH,
    BoundedConnectionHandler,
    ControlOperationError,
    RecorderUnixServer,
)

CONTROL_CATALOG_BUSY_TIMEOUT_MS = 2_000
CONTROL_DURABLE_WORKER_TIMEOUT_S = 6.0
CONTROL_DISPATCHER_TIMEOUT_S = 8.0
CONTROL_HANDLER_TIMEOUT_S = 10.0


class RuntimeControlOwner(Protocol):
    """Narrow recorder-owned capabilities exposed to local control composition."""

    def runtime_snapshot(self) -> dict[str, object]: ...

    async def execute_control_intent(self, intent_id: UUID) -> None: ...

    async def trigger_control_event(
        self,
        source: EventSource,
        monotonic_now_ns: int,
        previous_count: int,
        next_count: int,
        event_id: UUID,
    ) -> EventProtectionResult: ...


class _GroupEntry(Protocol):
    gr_gid: int


class ControlEndpoint(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def snapshot(self) -> dict[str, object]: ...


ServerFactory = Callable[
    [BoundedConnectionHandler, int, int, Callable[[str], None]],
    ControlEndpoint,
]


def _dashcam_api_gid() -> int:
    try:
        import grp

        lookup = cast(Callable[[str], _GroupEntry], getattr(grp, "getgrnam"))  # noqa: B009
        return int(lookup("dashcam-api").gr_gid)
    except (ImportError, KeyError, OSError) as error:
        raise OSError("dashcam-api group is unavailable") from error


def _default_server_factory(
    handler: BoundedConnectionHandler,
    group_id: int,
    owner_uid: int,
    fault_callback: Callable[[str], None],
) -> ControlEndpoint:
    return RecorderUnixServer(
        handler,
        path=DEFAULT_CONTROL_SOCKET_PATH,
        socket_group_id=group_id,
        owner_uid=owner_uid,
        fault_callback=fault_callback,
    )


def _effective_uid() -> int:
    provider = cast(Callable[[], int], getattr(os, "geteuid"))  # noqa: B009
    return provider()


async def _unsupported_operation() -> Mapping[str, object] | None:
    raise ControlOperationError(
        ErrorCode.CONFLICT,
        "Operation is not available in the recorder-owned M10 control endpoint",
    )


def _unsupported_config_write(_config: DashcamConfig) -> None:
    raise ControlOperationError(
        ErrorCode.UNSUPPORTED_CONFIGURATION,
        "Live configuration updates are not enabled",
    )


def build_runtime_control_endpoint(
    *,
    runtime: RuntimeControlOwner,
    catalog: CatalogBackend,
    config_path: Path,
    boot_id: str,
    status_provider: Callable[[], Mapping[str, object]],
    fault_callback: Callable[[str], None],
    group_id_resolver: Callable[[], int] = _dashcam_api_gid,
    owner_uid_provider: Callable[[], int] = _effective_uid,
    server_factory: ServerFactory = _default_server_factory,
) -> ControlEndpoint:
    """Bind the dispatcher only to the active runtime's catalog and mutation seams."""

    group_id = group_id_resolver()
    owner_uid = owner_uid_provider()
    dispatcher = RecorderControlDispatcher(
        catalog=catalog,
        config_provider=lambda: load_config(config_path),
        config_writer=_unsupported_config_write,
        status_provider=status_provider,
        health_provider=runtime.runtime_snapshot,
        intent_executor=runtime.execute_control_intent,
        event_executor=runtime.trigger_control_event,
        restart_callback=_unsupported_operation,
        prepare_removal_callback=_unsupported_operation,
        monotonic_ns=time.monotonic_ns,
        boot_id=boot_id,
        operation_timeout_s=CONTROL_DISPATCHER_TIMEOUT_S,
    )
    return server_factory(
        BoundedConnectionHandler(dispatcher, request_timeout_s=CONTROL_HANDLER_TIMEOUT_S),
        group_id,
        owner_uid,
        fault_callback,
    )


__all__ = [
    "CONTROL_CATALOG_BUSY_TIMEOUT_MS",
    "CONTROL_DISPATCHER_TIMEOUT_S",
    "CONTROL_DURABLE_WORKER_TIMEOUT_S",
    "CONTROL_HANDLER_TIMEOUT_S",
    "ControlEndpoint",
    "RuntimeControlOwner",
    "build_runtime_control_endpoint",
]
