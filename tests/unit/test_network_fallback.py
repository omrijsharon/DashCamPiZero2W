from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from dashcam.network_fallback import (
    AP_ADDRESS,
    BOOT_CREDENTIALS,
    COMMAND_TIMEOUT_SECONDS,
    NETWORK_MANAGER_PROFILE,
    PRIVATE_CREDENTIALS,
    CommandResult,
    NetworkError,
    NetworkFallbackController,
    NetworkMode,
    NetworkReason,
    _mounted_boot_source,
    new_credentials,
    profile_for_interface,
    render_ap_profile,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeRunner:
    def __init__(self, responder: Callable[[tuple[str, ...]], CommandResult]) -> None:
        self.responder = responder
        self.calls: list[tuple[tuple[str, ...], float]] = []

    def run(self, argv: tuple[str, ...], *, timeout_seconds: float) -> CommandResult:
        self.calls.append((argv, timeout_seconds))
        return self.responder(argv)


class FakeFiles:
    def __init__(self, *, boot_safe: bool = True) -> None:
        self.contents: dict[str, str] = {}
        self.writes: list[tuple[str, str, int]] = []
        self.safe_private = True
        self.boot_safe = boot_safe

    def read_text(self, path: str) -> str | None:
        return self.contents.get(path)

    def private_file_is_safe(self, path: str) -> bool:
        return self.safe_private and path in {
            PRIVATE_CREDENTIALS,
            NETWORK_MANAGER_PROFILE,
        }

    def boot_credentials_path_is_safe(self) -> bool:
        return self.boot_safe

    def atomic_write(self, path: str, content: str, *, mode: int) -> None:
        self.contents[path] = content
        self.writes.append((path, content, mode))


def _client_runner(*, connected: bool = True, route: bool = True) -> FakeRunner:
    active_uuid: str | None = None
    active_interface: str | None = None

    def respond(argv: tuple[str, ...]) -> CommandResult:
        nonlocal active_interface, active_uuid
        if argv[-2:] == ("device", "status"):
            return CommandResult(0, "wlan0:wifi\neth0:ethernet\n")
        if "GENERAL.STATE" in argv:
            return CommandResult(0, "100 (connected)\n" if connected else "30 (disconnected)\n")
        if "GENERAL.CON-UUID" in argv:
            return CommandResult(0, "33333333-3333-4333-8333-333333333333\n")
        if "802-11-wireless.mode" in argv:
            return CommandResult(0, "infrastructure\n")
        if argv[-4:] == ("route", "show", "dev", "wlan0"):
            return CommandResult(
                0, "192.168.1.0/24 proto kernel scope link src 192.168.1.9\n" if route else ""
            )
        if argv[-5:-3] == ("up", "uuid"):
            active_uuid = argv[-3]
            active_interface = argv[-1]
            return CommandResult(0)
        if argv[-2:] == ("show", "--active"):
            assert active_uuid is not None and active_interface is not None
            return CommandResult(0, f"{active_uuid}:{active_interface}\n")
        if argv[-5:] == ("-o", "address", "show", "dev", "wlan0"):
            return CommandResult(0, "2: wlan0 inet 192.168.50.1/24 scope global wlan0\n")
        if "connection" in argv and ("load" in argv or "down" in argv):
            return CommandResult(0)
        raise AssertionError(argv)

    return FakeRunner(respond)


def _controller(
    runner: FakeRunner, files: FakeFiles, clock: FakeClock, *, window: int = 10
) -> NetworkFallbackController:
    return NetworkFallbackController(
        runner=runner,
        files=files,
        device_id="0123456789abcdef",
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
        client_window_seconds=window,
        poll_seconds=5,
    )


def test_association_and_local_route_succeed_without_internet_probe() -> None:
    runner = _client_runner()
    result = _controller(runner, FakeFiles(), FakeClock()).select_for_boot()

    assert result.mode is NetworkMode.CLIENT
    assert result.reason is NetworkReason.CLIENT_READY
    assert not any("ping" in argv or "curl" in argv for argv, _ in runner.calls)


def test_client_wait_is_bounded_by_60_seconds_and_command_timeout() -> None:
    clock = FakeClock()
    runner = _client_runner(connected=False)
    files = FakeFiles(boot_safe=False)

    result = _controller(runner, files, clock, window=10).select_for_boot()

    assert result.mode is NetworkMode.AP
    assert sum(clock.sleeps) == 10
    assert all(0 < delay <= 5 for delay in clock.sleeps)
    assert all(timeout == COMMAND_TIMEOUT_SECONDS for _, timeout in runner.calls)
    assert BOOT_CREDENTIALS not in files.contents


def test_ap_fallback_writes_private_profile_and_safe_boot_credentials() -> None:
    files = FakeFiles()
    result = _controller(_client_runner(connected=False), files, FakeClock()).select_for_boot()

    assert result.mode is NetworkMode.AP
    assert result.ssid is not None
    assert result.credentials_published
    assert [path for path, _, _ in files.writes] == [
        PRIVATE_CREDENTIALS,
        NETWORK_MANAGER_PROFILE,
        BOOT_CREDENTIALS,
    ]
    assert [mode for _, _, mode in files.writes] == [0o600, 0o600, 0o644]
    assert AP_ADDRESS in files.contents[NETWORK_MANAGER_PROFILE]
    assert "shared-dhcp-range=192.168.50.20,192.168.50.100" in files.contents[
        NETWORK_MANAGER_PROFILE
    ]
    assert "WPA_PASSPHRASE=" not in repr(result)
    assert json.loads(files.contents[PRIVATE_CREDENTIALS])["secret"] not in repr(result)
    private_state = json.loads(files.contents[PRIVATE_CREDENTIALS])
    assert private_state["schema_version"] == 2
    assert f"uuid={private_state['profile_uuid']}" in files.contents[NETWORK_MANAGER_PROFILE]


def test_generated_secret_is_unique_and_never_in_result() -> None:
    first = new_credentials("0123456789abcdef")
    second = new_credentials("0123456789abcdef")

    assert first.ssid == second.ssid
    assert first.secret != second.secret
    assert first.profile_uuid != second.profile_uuid
    result = _controller(
        _client_runner(connected=False), FakeFiles(), FakeClock()
    ).select_for_boot()
    assert first.secret not in repr(result)


def test_profile_replaces_only_the_closed_interface_placeholder() -> None:
    credentials = new_credentials("0123456789abcdef")
    template = render_ap_profile(credentials)
    profile = profile_for_interface(credentials, "wlan9")

    assert template.count("${INTERFACE}") == 1
    assert "interface-name=wlan9" in profile
    assert f"uuid={credentials.profile_uuid}" in profile
    assert "${" not in profile
    with pytest.raises(NetworkError):
        profile_for_interface(credentials, "wlan0; unsafe")


def test_v1_private_credentials_migrate_to_a_stable_uuid_before_ap_use() -> None:
    files = FakeFiles(boot_safe=False)
    files.contents[PRIVATE_CREDENTIALS] = json.dumps(
        {"schema_version": 1, "ssid": "Dashcam-A1B2C3D4", "secret": "a" * 32}
    )

    result = _controller(_client_runner(connected=False), files, FakeClock()).select_for_boot()

    private_state = json.loads(files.contents[PRIVATE_CREDENTIALS])
    assert result.mode is NetworkMode.AP
    assert private_state["schema_version"] == 2
    assert private_state["secret"] == "a" * 32
    assert private_state["profile_uuid"] in files.contents[NETWORK_MANAGER_PROFILE]
    assert "a" * 32 not in repr(result)


def test_boot_mount_parser_accepts_only_one_direct_vfat_device_source() -> None:
    valid = "/dev/sda1 /boot/firmware vfat rw 0 0\n"

    assert _mounted_boot_source(valid) == "/dev/sda1"
    assert _mounted_boot_source("overlay /boot/firmware overlay rw 0 0\n") is None
    assert _mounted_boot_source("/dev/disk\\040name /boot/firmware vfat rw 0 0\n") is None
    assert _mounted_boot_source(valid + "/dev/sdb1 /boot/firmware vfat rw 0 0\n") is None


def test_ambiguous_wifi_refuses_without_profile_mutation() -> None:
    runner = FakeRunner(lambda _: CommandResult(0, "wlan0:wifi\nwlan1:wifi\n"))
    files = FakeFiles()

    result = _controller(runner, files, FakeClock()).select_for_boot()

    assert result.mode is NetworkMode.REFUSED
    assert result.reason is NetworkReason.AMBIGUOUS_WIFI
    assert files.writes == []


def test_networkmanager_failure_is_bounded_and_never_raises() -> None:
    runner = FakeRunner(lambda _: CommandResult(1))
    result = _controller(runner, FakeFiles(), FakeClock()).select_for_boot()

    assert result.mode is NetworkMode.REFUSED
    assert result.reason is NetworkReason.COMMAND_FAILED


def test_same_boot_does_not_oscillate_and_new_boot_retries_client() -> None:
    files = FakeFiles(boot_safe=False)
    first = _controller(_client_runner(connected=False), files, FakeClock())
    selected = first.select_for_boot()
    repeat = first.select_for_boot()
    fresh = _controller(_client_runner(connected=True), files, FakeClock())

    assert selected.mode is NetworkMode.AP
    assert repeat.reason is NetworkReason.ALREADY_SELECTED
    assert fresh.select_for_boot().mode is NetworkMode.CLIENT


def test_explicit_retry_can_leave_ap_and_try_client_again() -> None:
    connected = False
    active_uuid: str | None = None
    active_interface: str | None = None

    def respond(argv: tuple[str, ...]) -> CommandResult:
        nonlocal active_interface, active_uuid, connected
        if argv[-2:] == ("device", "status"):
            return CommandResult(0, "wlan0:wifi\n")
        if "GENERAL.STATE" in argv:
            return CommandResult(0, "100 (connected)\n" if connected else "30 (disconnected)\n")
        if "GENERAL.CON-UUID" in argv:
            return CommandResult(0, "33333333-3333-4333-8333-333333333333\n")
        if "802-11-wireless.mode" in argv:
            return CommandResult(0, "infrastructure\n")
        if argv[-4:] == ("route", "show", "dev", "wlan0"):
            return CommandResult(0, "192.168.1.0/24 proto kernel\n")
        if argv[-5:-3] == ("up", "uuid"):
            active_uuid = argv[-3]
            active_interface = argv[-1]
            return CommandResult(0)
        if argv[-2:] == ("show", "--active"):
            assert active_uuid is not None and active_interface is not None
            return CommandResult(0, f"{active_uuid}:{active_interface}\n")
        if argv[-5:] == ("-o", "address", "show", "dev", "wlan0"):
            return CommandResult(0, "2: wlan0 inet 192.168.50.1/24 scope global wlan0\n")
        if "connection" in argv and ("load" in argv or "down" in argv):
            return CommandResult(0)
        raise AssertionError(argv)

    controller = _controller(FakeRunner(respond), FakeFiles(), FakeClock(), window=5)
    assert controller.select_for_boot().mode is NetworkMode.AP
    connected = True
    assert controller.retry_client().mode is NetworkMode.CLIENT


def test_service_is_networkmanager_only_and_has_no_dashcam_or_storage_coupling() -> None:
    service = (
        Path(__file__).parents[2]
        / "deploy/bootstrap/network/dashcam-network-fallback.service"
    ).read_text(encoding="utf-8")

    assert "Requires=NetworkManager.service" in service
    assert "Wants=cloud-final.service" in service
    assert "After=NetworkManager.service cloud-final.service" in service
    assert "ExecStart=/opt/dashcam/venv/bin/python -m dashcam.network_fallback" in service
    assert "TimeoutStartSec=120" in service
    assert "dashcam.service" not in service.lower()
    assert "storage" not in service.lower()
