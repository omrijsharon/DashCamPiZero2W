from __future__ import annotations

import sys
from collections.abc import Callable

from dashcam.network_fallback import (
    AP_ADDRESS,
    BOOT_CREDENTIALS,
    IP,
    NETWORK_MANAGER_PROFILE,
    NMCLI,
    PRIVATE_CREDENTIALS,
    CommandResult,
    CredentialState,
    LocalCommandRunner,
    NetworkFallbackController,
    NetworkMode,
    NetworkReason,
    _main,
    profile_for_interface,
)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Files:
    def __init__(self) -> None:
        self.contents: dict[str, str] = {}
        self.writes: list[str] = []

    def read_text(self, path: str) -> str | None:
        return self.contents.get(path)

    def private_file_is_safe(self, path: str) -> bool:
        return path in {PRIVATE_CREDENTIALS, NETWORK_MANAGER_PROFILE}

    def boot_credentials_path_is_safe(self) -> bool:
        return True

    def atomic_write(self, path: str, content: str, *, mode: int) -> None:
        self.contents[path] = content
        self.writes.append(path)


class Runner:
    def __init__(
        self,
        responder: Callable[[tuple[str, ...]], CommandResult],
        *,
        clock: Clock | None = None,
        consume_seconds: float = 0,
    ) -> None:
        self.responder = responder
        self.clock = clock
        self.consume_seconds = consume_seconds
        self.calls: list[tuple[tuple[str, ...], float, float]] = []

    def run(self, argv: tuple[str, ...], *, timeout_seconds: float) -> CommandResult:
        start = self.clock.now if self.clock is not None else 0.0
        self.calls.append((argv, timeout_seconds, start))
        if self.clock is not None:
            self.clock.now += min(self.consume_seconds, timeout_seconds)
        return self.responder(argv)


def _controller(
    runner: Runner, files: Files, clock: Clock, *, window: int = 1
) -> NetworkFallbackController:
    return NetworkFallbackController(
        runner=runner,
        files=files,
        device_id="0123456789abcdef",
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
        client_window_seconds=window,
        poll_seconds=1,
    )


def _ap_responder(
    *,
    load_returncode: int = 0,
    active_uuid: str | None = None,
    active_interface: str = "wlan0",
    address: str = AP_ADDRESS,
) -> tuple[Runner, list[str]]:
    requested_uuid: list[str] = []

    def respond(argv: tuple[str, ...]) -> CommandResult:
        if argv[-2:] == ("device", "status"):
            return CommandResult(0, "wlan0:wifi\n")
        if "GENERAL.STATE" in argv:
            return CommandResult(0, "30 (disconnected)\n")
        if "load" in argv:
            return CommandResult(load_returncode)
        if "up" in argv:
            requested_uuid.append(argv[argv.index("uuid") + 1])
            return CommandResult(0)
        if argv[-2:] == ("show", "--active"):
            selected_uuid = active_uuid or requested_uuid[-1]
            return CommandResult(0, f"{selected_uuid}:{active_interface}\n")
        if argv[:3] == (IP, "-4", "-o"):
            return CommandResult(
                0,
                f"2: wlan0 inet {address} scope global wlan0\n",
            )
        raise AssertionError(argv)

    return Runner(respond), requested_uuid


def test_load_failure_stops_before_activation() -> None:
    runner, _ = _ap_responder(load_returncode=1)
    files = Files()
    result = _controller(runner, files, Clock()).select_for_boot()

    assert result.mode is NetworkMode.REFUSED
    assert result.reason is NetworkReason.AP_ACTIVATION_FAILED
    assert not any("up" in argv for argv, _, _ in runner.calls)
    assert BOOT_CREDENTIALS not in files.contents


def test_activation_uses_uuid_and_rejects_stale_active_uuid() -> None:
    stale = "11111111-1111-4111-8111-111111111111"
    files = Files()
    runner, requested = _ap_responder(active_uuid=stale)

    result = _controller(runner, files, Clock()).select_for_boot()

    assert result.mode is NetworkMode.REFUSED
    assert len(requested) == 1
    up = next(argv for argv, _, _ in runner.calls if "up" in argv)
    assert up[2:4] == ("up", "uuid")
    assert "id" not in up
    assert requested[0] != stale
    assert BOOT_CREDENTIALS not in files.contents


def test_activation_verifies_interface_and_exact_address() -> None:
    wrong_interface_runner, _ = _ap_responder(active_interface="wlan9")
    wrong_address_runner, _ = _ap_responder(address="192.168.50.2/24")

    interface_result = _controller(
        wrong_interface_runner, Files(), Clock()
    ).select_for_boot()
    address_result = _controller(wrong_address_runner, Files(), Clock()).select_for_boot()

    assert interface_result.mode is NetworkMode.REFUSED
    assert address_result.mode is NetworkMode.REFUSED


def test_client_commands_share_one_real_deadline() -> None:
    clock = Clock()

    def respond(argv: tuple[str, ...]) -> CommandResult:
        if argv[-2:] == ("device", "status"):
            return CommandResult(0, "wlan0:wifi\n")
        if "GENERAL.STATE" in argv:
            return CommandResult(0, "100 (connected)\n")
        if "load" in argv:
            return CommandResult(1)
        raise AssertionError(argv)

    runner = Runner(respond, clock=clock, consume_seconds=4)
    result = _controller(runner, Files(), clock, window=6).select_for_boot()

    client_calls = [
        call
        for call in runner.calls
        if "device" in call[0] and ("status" in call[0] or "GENERAL.STATE" in call[0])
    ]
    assert result.mode is NetworkMode.REFUSED
    assert [timeout for _, timeout, _ in client_calls] == [5.0, 2.0]
    assert [started for _, _, started in client_calls] == [0.0, 4.0]
    assert not any("GENERAL.CON-UUID" in argv for argv, _, _ in runner.calls)
    assert not any(
        started >= 6 and ("device" in argv or "route" in argv)
        for argv, _, started in runner.calls
    )


def test_retry_cli_uses_persisted_profile_uuid_across_processes() -> None:
    clock = Clock()
    files = Files()
    credentials = CredentialState(
        ssid="Dashcam-A1B2C3D4",
        secret="a" * 32,
        profile_uuid="22222222-2222-4222-8222-222222222222",
    )
    files.contents["/etc/machine-id"] = "0123456789abcdef\n"
    files.contents[PRIVATE_CREDENTIALS] = credentials.to_private_json()
    files.contents[NETWORK_MANAGER_PROFILE] = profile_for_interface(credentials, "wlan0")

    def respond(argv: tuple[str, ...]) -> CommandResult:
        if "down" in argv:
            return CommandResult(0)
        if argv[-2:] == ("device", "status"):
            return CommandResult(0, "wlan0:wifi\n")
        if "GENERAL.STATE" in argv:
            return CommandResult(0, "100 (connected)\n")
        if "GENERAL.CON-UUID" in argv:
            return CommandResult(0, "33333333-3333-4333-8333-333333333333\n")
        if "802-11-wireless.mode" in argv:
            return CommandResult(0, "infrastructure\n")
        if argv[-4:] == ("route", "show", "dev", "wlan0"):
            return CommandResult(0, "192.168.1.0/24 proto kernel\n")
        raise AssertionError(argv)

    runner = Runner(respond)
    exit_code = _main(
        ["--retry-client"],
        runner=runner,
        files=files,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )

    down = next(argv for argv, _, _ in runner.calls if "down" in argv)
    assert exit_code == 0
    assert down == (
        NMCLI,
        "connection",
        "down",
        "uuid",
        credentials.profile_uuid,
    )
    assert "id" not in down
    assert credentials.secret not in repr(credentials)


def test_local_runner_rejects_oversized_output_without_returning_a_prefix() -> None:
    result = LocalCommandRunner().run(
        (sys.executable, "-c", "print('x' * 20000)"),
        timeout_seconds=5,
    )

    assert result.returncode == 125
    assert result.stdout == ""
    assert result.stderr == ""
