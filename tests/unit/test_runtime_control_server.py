from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from dashcam.catalog import CatalogClip, ClipCatalog, EventProtectionResult, EventSource
from dashcam.config import default_config, write_config_atomic
from dashcam.control.runtime_server import (
    CONTROL_CATALOG_BUSY_TIMEOUT_MS,
    CONTROL_DISPATCHER_TIMEOUT_S,
    CONTROL_DURABLE_WORKER_TIMEOUT_S,
    CONTROL_HANDLER_TIMEOUT_S,
    build_runtime_control_endpoint,
)
from dashcam.control.socket_server import BoundedConnectionHandler
from dashcam.state import ClipLifecycle
from dashcam_web.recorder_client import DEFAULT_TIMEOUT_S


def _clip(number: int) -> CatalogClip:
    return CatalogClip(
        clip_id=UUID(int=number),
        lifecycle=ClipLifecycle.FINALIZED,
        video_path=f"clips/clip-{number}.mp4",
        sidecar_path=f"clips/clip-{number}.json",
        start_monotonic_ns=number * 1_000,
        end_monotonic_ns=number * 1_000 + 900,
        retention_order=number,
        size_bytes=100,
        protected=False,
        protection_reason=None,
        pair_reconciled=True,
        managed=True,
    )


class Runtime:
    def runtime_snapshot(self) -> dict[str, object]:
        return {"recording": True}

    async def execute_control_intent(self, intent_id: UUID) -> None:
        del intent_id

    async def trigger_control_event(
        self,
        source: EventSource,
        monotonic_now_ns: int,
        previous_count: int,
        next_count: int,
        event_id: UUID,
    ) -> EventProtectionResult:
        del source, monotonic_now_ns, previous_count
        return EventProtectionResult(event_id, (), 0, next_count, ())


@dataclass
class Endpoint:
    handler: BoundedConnectionHandler
    started: bool = False
    stopped: bool = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    def snapshot(self) -> dict[str, object]:
        return {"started": self.started, "stopped": self.stopped}


class Writer:
    def __init__(self) -> None:
        self.value = bytearray()

    def write(self, value: bytes) -> None:
        self.value.extend(value)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


async def _request(handler: BoundedConnectionHandler, command: str, arguments: object) -> dict:
    reader = asyncio.StreamReader()
    request = {
        "version": 1,
        "request_id": str(UUID(int=900)),
        "command": command,
        "arguments": arguments,
    }
    reader.feed_data(json.dumps(request).encode() + b"\n")
    reader.feed_eof()
    writer = Writer()
    await handler(reader, writer)  # type: ignore[arg-type]
    return json.loads(writer.value)


def test_production_control_deadlines_are_strictly_nested() -> None:
    assert (
        CONTROL_CATALOG_BUSY_TIMEOUT_MS / 1_000
        < CONTROL_DURABLE_WORKER_TIMEOUT_S
        < CONTROL_DISPATCHER_TIMEOUT_S
        < CONTROL_HANDLER_TIMEOUT_S
        < DEFAULT_TIMEOUT_S
    )


def test_runtime_composition_releases_durable_lease_after_endpoint_restart(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        config_path = tmp_path / "config.toml"
        write_config_atomic(config_path, default_config())
        with ClipCatalog(tmp_path / "catalog.sqlite3") as catalog:
            catalog.register_clip(_clip(1), catalog_now_ns=1)
            endpoints: list[Endpoint] = []

            def server_factory(
                handler: BoundedConnectionHandler,
                group_id: int,
                owner_uid: int,
                fault_callback: object,
            ) -> Endpoint:
                assert (group_id, owner_uid) == (123, 456)
                assert callable(fault_callback)
                endpoint = Endpoint(handler)
                endpoints.append(endpoint)
                return endpoint

            def build() -> Endpoint:
                return build_runtime_control_endpoint(
                    runtime=Runtime(),
                    catalog=catalog,
                    config_path=config_path,
                    boot_id="boot-a",
                    status_provider=lambda: {"state": "RECORDING"},
                    fault_callback=lambda _detail: None,
                    group_id_resolver=lambda: 123,
                    owner_uid_provider=lambda: 456,
                    server_factory=server_factory,
                )  # type: ignore[return-value]

            first = build()
            await first.start()
            unsupported = await _request(
                first.handler,
                "update_config",
                {"video": {"bitrate_bps": 7_000_000}},
            )
            assert unsupported["error"]["code"] == "UNSUPPORTED_CONFIGURATION"
            acquired = await _request(
                first.handler,
                "acquire_download",
                {"clip_id": str(UUID(int=1)), "member": "video", "holder": "session"},
            )
            lease_id = acquired["result"]["lease_id"]
            assert catalog.get_clip(UUID(int=1)).download_lease is not None
            await first.stop()

            restarted = build()
            await restarted.start()
            released = await _request(
                restarted.handler,
                "release_download",
                {"clip_id": str(UUID(int=1)), "lease_id": lease_id},
            )
            assert released["result"]["released"] is True
            assert catalog.get_clip(UUID(int=1)).download_lease is None
            await restarted.stop()

        assert endpoints[0].stopped and endpoints[1].stopped

    asyncio.run(scenario())
