"""Production process entry point for the single-owner recorder daemon.

The media/runtime composition is intentionally imported only after command-line
arguments are parsed.  This keeps ``python -m dashcam.daemon --help`` and all
control-plane unit tests independent of PyGObject and Pi device nodes.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import signal
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol, cast

from dashcam.recorder.daemon import (
    DaemonResult,
    RecorderDaemon,
    RecorderRuntime,
    StorageGate,
)
from dashcam.recorder.metrics import RuntimeSnapshotPublisher
from dashcam.recorder.notifier import ServiceNotifier, SystemdNotifier

DEFAULT_CONFIG_PATH = Path("/etc/dashcam/config.toml")
DEFAULT_IDENTITY_PATH = Path("/etc/dashcam/storage-volume.env")


class ProductionRuntime(RecorderRuntime, StorageGate, Protocol):
    """Target runtime that also owns the mandatory fresh storage gate."""


class ProductionRuntimeFactory(Protocol):
    """Build the target-only runtime and its fresh storage gate."""

    def __call__(
        self,
        *,
        config_path: Path,
        identity_path: Path,
    ) -> ProductionRuntime:
        """Return one single-use recorder runtime and storage gate."""


class RecorderDaemonFactory(Protocol):
    """Construct the hardware-independent lifecycle coordinator."""

    def __call__(
        self,
        *,
        config_path: Path,
        runtime: RecorderRuntime,
        storage_gate: StorageGate,
        notifier: ServiceNotifier,
        snapshot_publisher: RuntimeSnapshotPublisher,
    ) -> RecorderDaemon:
        """Return one single-use recorder daemon."""


def _build_production_runtime(
    *,
    config_path: Path,
    identity_path: Path,
) -> ProductionRuntime:
    """Lazily load the Pi-only runtime/storage composition.

    ``dashcam.recorder.runtime.build_production_runtime`` is deliberately not
    imported at module load time.  Its required future interface is::

        def build_production_runtime(
            *, config_path: Path, identity_path: Path
        ) -> ProductionRuntime: ...
    """

    runtime_module = importlib.import_module("dashcam.recorder.runtime")
    factory_name = "build_production_runtime"
    factory = cast(
        ProductionRuntimeFactory,
        getattr(runtime_module, factory_name),
    )
    return factory(config_path=config_path, identity_path=identity_path)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m dashcam.daemon")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="path to the validated dashcam TOML configuration",
    )
    parser.add_argument(
        "--identity",
        type=Path,
        default=DEFAULT_IDENTITY_PATH,
        help="path to the provisioned storage identity file",
    )
    return parser


def _install_stop_handlers(
    loop: asyncio.AbstractEventLoop,
    daemon: RecorderDaemon,
) -> tuple[signal.Signals, ...]:
    """Install cooperative termination handlers where the event loop supports them."""

    installed: list[signal.Signals] = []
    for stop_signal in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(stop_signal, daemon.request_stop)
        except NotImplementedError:
            # Windows event loops lack signal-handler integration.  The service
            # contract is Linux/systemd, and unit tests must remain portable.
            continue
        installed.append(stop_signal)
    return tuple(installed)


def _remove_stop_handlers(
    loop: asyncio.AbstractEventLoop,
    installed: tuple[signal.Signals, ...],
) -> None:
    for stop_signal in installed:
        loop.remove_signal_handler(stop_signal)


async def run_daemon(
    *,
    config_path: Path,
    identity_path: Path,
    runtime_factory: ProductionRuntimeFactory = _build_production_runtime,
    notifier_factory: Callable[[], ServiceNotifier] = SystemdNotifier.from_environment,
    daemon_factory: RecorderDaemonFactory = RecorderDaemon,
) -> int:
    """Run the recorder once and map its terminal lifecycle outcome to an exit code."""

    runtime = runtime_factory(config_path=config_path, identity_path=identity_path)
    daemon = daemon_factory(
        config_path=config_path,
        runtime=runtime,
        storage_gate=runtime,
        notifier=notifier_factory(),
        snapshot_publisher=RuntimeSnapshotPublisher(),
    )
    loop = asyncio.get_running_loop()
    installed = _install_stop_handlers(loop, daemon)
    try:
        result: DaemonResult = await daemon.run()
    except asyncio.CancelledError:
        daemon.request_stop()
        raise
    finally:
        _remove_stop_handlers(loop, installed)
    return 0 if result.clean else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and run the recorder under one bounded asyncio loop."""

    arguments = _build_parser().parse_args(argv)
    return asyncio.run(run_daemon(config_path=arguments.config, identity_path=arguments.identity))


if __name__ == "__main__":
    raise SystemExit(main())
