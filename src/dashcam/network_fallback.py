"""Bounded, client-first NetworkManager policy for the bootstrap image.

This deliberately small adapter owns only the Wi-Fi mode choice for one boot.
It does not start, stop, or inspect dashcam/storage services.  The policy is
testable without NetworkManager through narrow runner and filesystem protocols.
Secrets are kept out of :class:`NetworkResult` and command diagnostics.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat as stat_module
import subprocess
import tempfile
import time
from argparse import ArgumentParser
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol
from uuid import UUID, uuid4, uuid5

NMCLI: Final = "/usr/bin/nmcli"
IP: Final = "/usr/sbin/ip"
NETWORK_MANAGER_PROFILE: Final = (
    "/etc/NetworkManager/system-connections/dashcam-fallback-ap.nmconnection"
)
PRIVATE_CREDENTIALS: Final = "/var/lib/dashcam/network/ap-credentials.json"
BOOT_CREDENTIALS: Final = "/boot/firmware/dashcam-ap-credentials.txt"
PROFILE_NAME: Final = "dashcam-fallback-ap"
AP_ADDRESS: Final = "192.168.50.1/24"
AP_DHCP_RANGE: Final = "192.168.50.20,192.168.50.100"
CLIENT_WINDOW_SECONDS: Final = 60
POLL_SECONDS: Final = 5
COMMAND_TIMEOUT_SECONDS: Final = 5
AP_ACTIVATION_WINDOW_SECONDS: Final = 30
MAX_OUTPUT_BYTES: Final = 16 * 1024
MAX_FILE_BYTES: Final = 64 * 1024

_DEVICE_ID_RE: Final = re.compile(r"[A-Za-z0-9]{4,128}")
_SECRET_RE: Final = re.compile(r"[A-Za-z0-9_-]{20,128}")
_SSID_RE: Final = re.compile(r"Dashcam-[A-F0-9]{8}")
_PROFILE_UUID_NAMESPACE: Final = UUID("50f13cf7-504c-4f8f-80b1-01c3fdc2ab4f")


class NetworkError(ValueError):
    """Raised when a supplied policy input is outside this closed contract."""


class NetworkMode(StrEnum):
    CLIENT = "client"
    AP = "ap"
    REFUSED = "refused"


class NetworkReason(StrEnum):
    CLIENT_READY = "client_ready"
    CLIENT_WINDOW_EXPIRED = "client_window_expired"
    AMBIGUOUS_WIFI = "ambiguous_wifi"
    COMMAND_FAILED = "command_failed"
    AP_ACTIVATION_FAILED = "ap_activation_failed"
    ALREADY_SELECTED = "already_selected"


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Bounded result of one non-shell process invocation."""

    returncode: int
    stdout: str = ""
    stderr: str = ""

    def __post_init__(self) -> None:
        if (
            len(self.stdout.encode()) > MAX_OUTPUT_BYTES
            or len(self.stderr.encode()) > MAX_OUTPUT_BYTES
        ):
            raise NetworkError("command output exceeds the network policy bound")


class CommandRunner(Protocol):
    def run(self, argv: tuple[str, ...], *, timeout_seconds: float) -> CommandResult:
        """Run an exact argv with a bounded timeout and no shell."""


class FileStore(Protocol):
    def read_text(self, path: str) -> str | None:
        """Read a regular, non-symlink file, or return ``None`` if absent."""

    def private_file_is_safe(self, path: str) -> bool:
        """Return true only for a root-owned regular mode-0600 private file."""

    def boot_credentials_path_is_safe(self) -> bool:
        """Return true only for the expected mounted, root-owned boot filesystem."""

    def atomic_write(self, path: str, content: str, *, mode: int) -> None:
        """Atomically replace a regular file and fsync the containing directory."""


@dataclass(frozen=True, slots=True)
class CredentialState:
    ssid: str
    secret: str = field(repr=False)
    profile_uuid: str

    def __post_init__(self) -> None:
        if _SSID_RE.fullmatch(self.ssid) is None:
            raise NetworkError("fallback SSID is invalid")
        if _SECRET_RE.fullmatch(self.secret) is None:
            raise NetworkError("fallback WPA secret is invalid")
        try:
            canonical_uuid = str(UUID(self.profile_uuid))
        except (AttributeError, ValueError) as exc:
            raise NetworkError("fallback profile UUID is invalid") from exc
        if self.profile_uuid != canonical_uuid:
            raise NetworkError("fallback profile UUID must be canonical lowercase")

    def to_private_json(self) -> str:
        return json.dumps(
            {
                "profile_uuid": self.profile_uuid,
                "schema_version": 2,
                "secret": self.secret,
                "ssid": self.ssid,
            },
            sort_keys=True,
            separators=(",", ":"),
        ) + "\n"


@dataclass(frozen=True, slots=True)
class NetworkResult:
    """A redacted, stable result suitable for health reporting."""

    mode: NetworkMode
    reason: NetworkReason
    interface: str | None
    ssid: str | None = None
    credentials_published: bool = False

    def __post_init__(self) -> None:
        if self.mode is NetworkMode.AP and self.ssid is None:
            raise NetworkError("AP results require an SSID")
        if self.mode is not NetworkMode.AP and self.ssid is not None:
            raise NetworkError("only AP results may identify an SSID")


def short_device_id(device_id: str) -> str:
    """Derive an opaque fixed-size identifier without exposing machine identity."""

    import hashlib

    if _DEVICE_ID_RE.fullmatch(device_id) is None:
        raise NetworkError("device identity is invalid")
    return hashlib.sha256(device_id.encode()).hexdigest()[:8].upper()


def new_credentials(device_id: str) -> CredentialState:
    """Create per-image credentials; the secret never appears in a public result."""

    # token_urlsafe's alphabet is accepted by the strict PSK formatter below.
    return CredentialState(
        ssid=f"Dashcam-{short_device_id(device_id)}",
        secret=secrets.token_urlsafe(32),
        profile_uuid=str(uuid4()),
    )


def render_ap_profile(credentials: CredentialState) -> str:
    """Render the exact NetworkManager keyfile used for the fallback AP."""

    profile = (
        "[connection]\n"
        f"id={PROFILE_NAME}\n"
        f"uuid={credentials.profile_uuid}\n"
        "type=wifi\n"
        "interface-name=${INTERFACE}\n"
        "autoconnect=false\n\n"
        "[wifi]\n"
        f"ssid={credentials.ssid}\n"
        "mode=ap\n\n"
        "[wifi-security]\n"
        "key-mgmt=wpa-psk\n"
        f"psk={credentials.secret}\n\n"
        "[ipv4]\n"
        "method=shared\n"
        f"address1={AP_ADDRESS}\n"
        f"shared-dhcp-range={AP_DHCP_RANGE}\n\n"
        "[ipv6]\n"
        "method=disabled\n"
    )
    validate_ap_profile(profile, credentials=credentials, interface_placeholder=True)
    return profile


def validate_ap_profile(
    profile: str, *, credentials: CredentialState, interface_placeholder: bool
) -> None:
    """Reject profile drift and unresolved/unexpected template variables."""

    if profile.count("${INTERFACE}") != (1 if interface_placeholder else 0):
        raise NetworkError("AP profile interface placeholder is invalid")
    if "\x00" in profile or "\r" in profile:
        raise NetworkError("AP profile contains unsafe control characters")
    unresolved = profile.replace("${INTERFACE}", "")
    if "${" in unresolved:
        raise NetworkError("AP profile contains an unresolved placeholder")
    required = (
        f"id={PROFILE_NAME}",
        f"uuid={credentials.profile_uuid}",
        "type=wifi",
        "autoconnect=false",
        f"ssid={credentials.ssid}",
        "mode=ap",
        "key-mgmt=wpa-psk",
        f"psk={credentials.secret}",
        "method=shared",
        f"address1={AP_ADDRESS}",
        f"shared-dhcp-range={AP_DHCP_RANGE}",
        "method=disabled",
    )
    if any(line not in profile for line in required):
        raise NetworkError("AP profile is missing a required closed setting")
    interface_lines = [line for line in profile.splitlines() if line.startswith("interface-name=")]
    if len(interface_lines) != 1:
        raise NetworkError("AP profile interface binding is invalid")
    interface_value = interface_lines[0].removeprefix("interface-name=")
    if interface_placeholder:
        if interface_value != "${INTERFACE}":
            raise NetworkError("AP profile does not preserve its interface placeholder")
    elif re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", interface_value) is None:
        raise NetworkError("AP profile has an unsafe interface binding")


def profile_for_interface(credentials: CredentialState, interface: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", interface):
        raise NetworkError("Wi-Fi interface name is unsafe")
    profile = render_ap_profile(credentials).replace("${INTERFACE}", interface)
    validate_ap_profile(profile, credentials=credentials, interface_placeholder=False)
    return profile


class NetworkFallbackController:
    """One boot-scoped mode selector; an AP decision cannot oscillate to client."""

    def __init__(
        self,
        *,
        runner: CommandRunner,
        files: FileStore,
        device_id: str,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        client_window_seconds: int = CLIENT_WINDOW_SECONDS,
        poll_seconds: int = POLL_SECONDS,
    ) -> None:
        if not 1 <= client_window_seconds <= CLIENT_WINDOW_SECONDS:
            raise NetworkError("client window must be within 1..60 seconds")
        if not 1 <= poll_seconds <= client_window_seconds:
            raise NetworkError("poll interval must be bounded by the client window")
        short_device_id(device_id)
        self._runner = runner
        self._files = files
        self._device_id = device_id
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._window = client_window_seconds
        self._poll = poll_seconds
        self._selected: NetworkResult | None = None

    def select_for_boot(self) -> NetworkResult:
        """Try client connectivity once, then select a stable fallback AP."""

        if self._selected is not None:
            return NetworkResult(
                mode=self._selected.mode,
                reason=NetworkReason.ALREADY_SELECTED,
                interface=self._selected.interface,
                ssid=self._selected.ssid,
                credentials_published=self._selected.credentials_published,
            )
        deadline = self._monotonic() + self._window
        interface, discovery_failed = self._find_one_wifi_interface(deadline)
        if interface is None:
            reason = (
                NetworkReason.COMMAND_FAILED
                if discovery_failed
                else NetworkReason.AMBIGUOUS_WIFI
            )
            self._selected = NetworkResult(NetworkMode.REFUSED, reason, None)
            return self._selected
        while self._monotonic() < deadline:
            if self._client_is_usable(interface, deadline):
                self._selected = NetworkResult(
                    NetworkMode.CLIENT, NetworkReason.CLIENT_READY, interface
                )
                return self._selected
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                break
            self._sleeper(min(float(self._poll), remaining))
        self._selected = self._activate_ap(interface)
        return self._selected

    def retry_client(self) -> NetworkResult:
        """Leave the persisted AP by UUID, then perform one fresh bounded retry.

        This intentionally does not rely on ``self._selected``: the operator
        command normally runs in a process separate from the boot service.
        """

        try:
            credentials, interface = self._persisted_ap_identity()
        except (NetworkError, OSError):
            return NetworkResult(NetworkMode.REFUSED, NetworkReason.COMMAND_FAILED, None)
        deadline = self._monotonic() + AP_ACTIVATION_WINDOW_SECONDS
        result = self._run_before(
            (NMCLI, "connection", "down", "uuid", credentials.profile_uuid),
            deadline=deadline,
        )
        if result is None or result.returncode != 0:
            return NetworkResult(
                NetworkMode.AP,
                NetworkReason.COMMAND_FAILED,
                interface,
                ssid=credentials.ssid,
            )
        self._selected = None
        return self.select_for_boot()

    def _run_before(
        self, argv: tuple[str, ...], *, deadline: float
    ) -> CommandResult | None:
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            return None
        return self._runner.run(
            argv, timeout_seconds=min(float(COMMAND_TIMEOUT_SECONDS), remaining)
        )

    def _find_one_wifi_interface(self, deadline: float) -> tuple[str | None, bool]:
        result = self._run_before(
            (NMCLI, "--terse", "--fields", "DEVICE,TYPE", "device", "status"),
            deadline=deadline,
        )
        if result is None:
            return None, True
        if result.returncode != 0:
            return None, True
        interfaces: list[str] = []
        for line in result.stdout.splitlines():
            fields = line.split(":")
            if len(fields) == 2 and fields[1] == "wifi" and re.fullmatch(
                r"[A-Za-z0-9_.-]{1,32}", fields[0]
            ):
                interfaces.append(fields[0])
        return (interfaces[0], False) if len(interfaces) == 1 else (None, False)

    def _client_is_usable(self, interface: str, deadline: float) -> bool:
        state = self._run_before(
            (NMCLI, "--get-values", "GENERAL.STATE", "device", "show", interface),
            deadline=deadline,
        )
        if (
            state is None
            or state.returncode != 0
            or not state.stdout.strip().startswith("100")
        ):
            return False
        connection = self._run_before(
            (NMCLI, "--get-values", "GENERAL.CON-UUID", "device", "show", interface),
            deadline=deadline,
        )
        if connection is None:
            return False
        active_uuid = connection.stdout.strip()
        try:
            canonical_uuid = str(UUID(active_uuid))
        except ValueError:
            return False
        if connection.returncode != 0 or active_uuid != canonical_uuid:
            return False
        mode = self._run_before(
            (
                NMCLI,
                "--get-values",
                "802-11-wireless.mode",
                "connection",
                "show",
                "uuid",
                active_uuid,
            ),
            deadline=deadline,
        )
        if (
            mode is None
            or mode.returncode != 0
            or mode.stdout.strip() not in {"", "infrastructure"}
        ):
            return False
        routes = self._run_before(
            (IP, "-4", "route", "show", "dev", interface), deadline=deadline
        )
        return routes is not None and routes.returncode == 0 and any(
            line.strip() and not line.lstrip().startswith("unreachable")
            for line in routes.stdout.splitlines()
        )

    def _credentials(self) -> CredentialState:
        raw = self._files.read_text(PRIVATE_CREDENTIALS)
        if raw is not None:
            if not self._files.private_file_is_safe(PRIVATE_CREDENTIALS):
                raise NetworkError("existing private AP credentials are unsafe")
            try:
                parsed = json.loads(raw)
                if not isinstance(parsed, dict):
                    raise NetworkError("private AP credential schema is invalid")
                if set(parsed) == {"schema_version", "secret", "ssid"} and parsed[
                    "schema_version"
                ] == 1:
                    if not all(isinstance(parsed[key], str) for key in ("secret", "ssid")):
                        raise NetworkError("private AP credential values are invalid")
                    # V1 persisted no NM profile UUID. Derive a stable replacement,
                    # then durably migrate the private state before using it.
                    credentials = CredentialState(
                        ssid=parsed["ssid"],
                        secret=parsed["secret"],
                        profile_uuid=str(
                            uuid5(_PROFILE_UUID_NAMESPACE, f"{parsed['ssid']}:{parsed['secret']}")
                        ),
                    )
                    self._files.atomic_write(
                        PRIVATE_CREDENTIALS, credentials.to_private_json(), mode=0o600
                    )
                    return credentials
                if set(parsed) != {"profile_uuid", "schema_version", "secret", "ssid"}:
                    raise NetworkError("private AP credential schema is invalid")
                if parsed["schema_version"] != 2 or not all(
                    isinstance(parsed[key], str) for key in ("secret", "ssid")
                ):
                    raise NetworkError("private AP credential values are invalid")
                if not isinstance(parsed["profile_uuid"], str):
                    raise NetworkError("private AP credential profile UUID is invalid")
                return CredentialState(
                    ssid=parsed["ssid"],
                    secret=parsed["secret"],
                    profile_uuid=parsed["profile_uuid"],
                )
            except (json.JSONDecodeError, TypeError) as exc:
                raise NetworkError("private AP credentials are malformed") from exc
        credentials = new_credentials(self._device_id)
        self._files.atomic_write(PRIVATE_CREDENTIALS, credentials.to_private_json(), mode=0o600)
        return credentials

    def _persisted_ap_identity(self) -> tuple[CredentialState, str]:
        """Return a closed credentials/profile pair suitable for operator retry."""

        credentials = self._credentials()
        profile = self._files.read_text(NETWORK_MANAGER_PROFILE)
        if (
            profile is None
            or not self._files.private_file_is_safe(NETWORK_MANAGER_PROFILE)
        ):
            raise NetworkError("persisted AP profile is absent or unsafe")
        interface_lines = [
            line.removeprefix("interface-name=")
            for line in profile.splitlines()
            if line.startswith("interface-name=")
        ]
        if len(interface_lines) != 1:
            raise NetworkError("persisted AP profile interface is ambiguous")
        interface = interface_lines[0]
        expected = profile_for_interface(credentials, interface)
        if not secrets.compare_digest(profile, expected):
            raise NetworkError("persisted AP profile does not match private credentials")
        return credentials, interface

    def _activate_ap(self, interface: str) -> NetworkResult:
        deadline = self._monotonic() + AP_ACTIVATION_WINDOW_SECONDS
        try:
            credentials = self._credentials()
            self._files.atomic_write(
                NETWORK_MANAGER_PROFILE, profile_for_interface(credentials, interface), mode=0o600
            )
        except (NetworkError, OSError):
            return NetworkResult(NetworkMode.REFUSED, NetworkReason.AP_ACTIVATION_FAILED, interface)
        load = self._run_before(
            (NMCLI, "connection", "load", NETWORK_MANAGER_PROFILE),
            deadline=deadline,
        )
        if load is None or load.returncode != 0:
            return NetworkResult(NetworkMode.REFUSED, NetworkReason.AP_ACTIVATION_FAILED, interface)
        up = self._run_before(
            (
                NMCLI,
                "connection",
                "up",
                "uuid",
                credentials.profile_uuid,
                "ifname",
                interface,
            ),
            deadline=deadline,
        )
        if up is None or up.returncode != 0:
            return NetworkResult(NetworkMode.REFUSED, NetworkReason.AP_ACTIVATION_FAILED, interface)
        if not self._ap_is_exactly_active(
            credentials=credentials, interface=interface, deadline=deadline
        ):
            return NetworkResult(NetworkMode.REFUSED, NetworkReason.AP_ACTIVATION_FAILED, interface)
        published = False
        if self._files.boot_credentials_path_is_safe():
            body = (
                f"SSID={credentials.ssid}\n"
                f"WPA_PASSPHRASE={credentials.secret}\n"
                "ADDRESS=192.168.50.1\n"
            )
            try:
                # VFAT does not retain Unix modes.  This is an intentional physical-card
                # recovery handoff, unlike the root-only credential state above.
                self._files.atomic_write(BOOT_CREDENTIALS, body, mode=0o644)
                published = True
            except OSError:
                published = False
        return NetworkResult(
            NetworkMode.AP,
            NetworkReason.CLIENT_WINDOW_EXPIRED,
            interface,
            ssid=credentials.ssid,
            credentials_published=published,
        )

    def _ap_is_exactly_active(
        self, *, credentials: CredentialState, interface: str, deadline: float
    ) -> bool:
        active = self._run_before(
            (
                NMCLI,
                "--terse",
                "--fields",
                "UUID,DEVICE",
                "connection",
                "show",
                "--active",
            ),
            deadline=deadline,
        )
        if active is None or active.returncode != 0:
            return False
        exact_binding = f"{credentials.profile_uuid}:{interface}"
        if [line for line in active.stdout.splitlines() if line == exact_binding] != [
            exact_binding
        ]:
            return False
        addresses = self._run_before(
            (IP, "-4", "-o", "address", "show", "dev", interface),
            deadline=deadline,
        )
        if addresses is None or addresses.returncode != 0:
            return False
        return any(
            len(fields := line.split()) >= 4
            and fields[1] == interface
            and fields[2] == "inet"
            and fields[3] == AP_ADDRESS
            for line in addresses.stdout.splitlines()
        )


class LocalCommandRunner:
    """Target-only adapter: no shell and bounded captured output."""

    def run(self, argv: tuple[str, ...], *, timeout_seconds: float) -> CommandResult:
        if timeout_seconds <= 0:
            return CommandResult(returncode=124)
        # Temporary files keep child output out of Python memory.  Reading one
        # byte beyond the contract lets us reject truncation rather than parse
        # a potentially misleading prefix.
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            try:
                process = subprocess.Popen(
                    argv,
                    shell=False,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                )
                try:
                    process.wait(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    return CommandResult(returncode=124)
            except OSError:
                return CommandResult(returncode=124)
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read(MAX_OUTPUT_BYTES + 1)
            stderr = stderr_file.read(MAX_OUTPUT_BYTES + 1)
            if len(stdout) > MAX_OUTPUT_BYTES or len(stderr) > MAX_OUTPUT_BYTES:
                return CommandResult(returncode=125)
            return CommandResult(
                process.returncode,
                stdout.decode(errors="replace"),
                stderr.decode(errors="replace"),
            )


class LocalFileStore:
    """Target-only filesystem adapter with atomic regular-file replacement."""

    def read_text(self, path: str) -> str | None:
        target = Path(path)
        descriptor: int | None = None
        parent_descriptor: int | None = None
        try:
            parent_descriptor = _open_safe_parent(target)
            descriptor = os.open(
                target.name,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_descriptor,
            )
            target_stat = os.fstat(descriptor)
            if (
                not stat_module.S_ISREG(target_stat.st_mode)
                or target_stat.st_uid != 0
                or target_stat.st_nlink != 1
            ):
                return None
            data = _read_bounded(descriptor, MAX_FILE_BYTES)
            return data.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if parent_descriptor is not None:
                os.close(parent_descriptor)

    def private_file_is_safe(self, path: str) -> bool:
        target = Path(path)
        parent_descriptor: int | None = None
        try:
            parent_descriptor = _open_safe_parent(target)
            target_stat = os.stat(
                target.name, dir_fd=parent_descriptor, follow_symlinks=False
            )
        except OSError:
            return False
        finally:
            if parent_descriptor is not None:
                os.close(parent_descriptor)
        return bool(
            stat_module.S_ISREG(target_stat.st_mode)
            and target_stat.st_uid == 0
            and target_stat.st_nlink == 1
            and (target_stat.st_mode & 0o777) == 0o600
        )

    def boot_credentials_path_is_safe(self) -> bool:
        boot = Path("/boot/firmware")
        try:
            _validate_safe_directory(boot)
            boot_stat = os.lstat(boot)
        except OSError:
            return False
        return (
            stat_module.S_ISDIR(boot_stat.st_mode)
            and boot_stat.st_uid == 0
            and not (boot_stat.st_mode & 0o022)
            and self._is_expected_boot_mount()
        )

    @staticmethod
    def _is_expected_boot_mount() -> bool:
        """Require an actual VFAT boot mount backed by a direct block-device node."""

        try:
            source = _mounted_boot_source(Path("/proc/mounts").read_text(encoding="utf-8"))
        except OSError:
            return False
        if source is None:
            return False
        try:
            source_path = Path(source)
            source_stat = os.lstat(source_path)
            resolved = source_path.resolve(strict=True)
            resolved_stat = os.stat(resolved)
        except OSError:
            return False
        return (
            source_path == resolved
            and stat_module.S_ISBLK(source_stat.st_mode)
            and stat_module.S_ISBLK(resolved_stat.st_mode)
        )

    def atomic_write(self, path: str, content: str, *, mode: int) -> None:
        target = Path(path)
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_FILE_BYTES or mode not in {0o600, 0o644}:
            raise OSError("unsafe destination content or mode")
        parent_descriptor = _open_safe_parent(target)
        temporary = f".{target.name}.{secrets.token_hex(8)}.tmp"
        descriptor: int | None = None
        try:
            try:
                existing = os.stat(
                    target.name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                existing = None
            if existing is not None and (
                not stat_module.S_ISREG(existing.st_mode)
                or existing.st_uid != 0
                or existing.st_nlink != 1
            ):
                raise OSError("unsafe existing destination")
            descriptor = os.open(
                temporary,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                mode,
                dir_fd=parent_descriptor,
            )
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                descriptor = None
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(
                temporary,
                target.name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
            descriptor = os.open(
                target.name,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_descriptor,
            )
            descriptor_chmod = getattr(os, "fchmod", None)
            if not callable(descriptor_chmod):
                raise OSError("descriptor chmod is unavailable")
            descriptor_chmod(descriptor, mode)
            os.close(descriptor)
            descriptor = None
            os.fsync(parent_descriptor)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=parent_descriptor)
            finally:
                os.close(parent_descriptor)


def _main(
    argv: Sequence[str] | None = None,
    *,
    runner: CommandRunner | None = None,
    files: FileStore | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    parser = ArgumentParser(prog="python -m dashcam.network_fallback")
    parser.add_argument(
        "--retry-client",
        action="store_true",
        help="leave the persisted fallback AP and retry client mode once",
    )
    arguments = parser.parse_args(argv)
    selected_files = files if files is not None else LocalFileStore()
    device_id = selected_files.read_text("/etc/machine-id")
    if device_id is None:
        return 2
    controller = NetworkFallbackController(
        runner=runner if runner is not None else LocalCommandRunner(),
        files=selected_files,
        device_id=device_id.strip(),
        monotonic=monotonic,
        sleeper=sleeper,
    )
    result = (
        controller.retry_client()
        if arguments.retry_client
        else controller.select_for_boot()
    )
    # Deliberately no command output, SSID, or WPA credential is written to stdout.
    return 0 if result.mode is not NetworkMode.REFUSED else 1


def _validate_safe_directory(path: Path) -> None:
    """Require every absolute directory component to be root-owned and immutable to peers."""

    if not path.is_absolute() or path == Path("/"):
        raise OSError("unsafe directory path")
    current = Path("/")
    for component in path.parts[1:]:
        current /= component
        directory_stat = os.lstat(current)
        if (
            not stat_module.S_ISDIR(directory_stat.st_mode)
            or stat_module.S_ISLNK(directory_stat.st_mode)
            or directory_stat.st_uid != 0
            or directory_stat.st_mode & 0o022
        ):
            raise OSError("unsafe directory ancestry")


def _open_safe_parent(target: Path) -> int:
    if not target.is_absolute() or target.name in {"", ".", ".."}:
        raise OSError("unsafe destination path")
    _validate_safe_directory(target.parent)
    descriptor = os.open(
        target.parent,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    directory_stat = os.fstat(descriptor)
    if (
        not stat_module.S_ISDIR(directory_stat.st_mode)
        or directory_stat.st_uid != 0
        or directory_stat.st_mode & 0o022
    ):
        os.close(descriptor)
        raise OSError("unsafe destination directory")
    return descriptor


def _read_bounded(descriptor: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    remaining = limit + 1
    while remaining:
        chunk = os.read(descriptor, min(4096, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if len(data) > limit:
        raise OSError("file exceeds the network policy bound")
    return data


def _mounted_boot_source(mounts_text: str) -> str | None:
    """Parse one exact /proc/mounts boot entry without assuming a node name.

    The source must be a direct, unescaped ``/dev`` path.  The caller resolves
    it and verifies the block-device identity before accepting it.
    """

    candidates: list[str] = []
    for line in mounts_text.splitlines():
        fields = line.split()
        if len(fields) < 3 or fields[1] != "/boot/firmware":
            continue
        source, filesystem = fields[0], fields[2]
        if (
            filesystem in {"vfat", "msdos"}
            and source.startswith("/dev/")
            and "\\" not in source
            and "\x00" not in source
        ):
            candidates.append(source)
    return candidates[0] if len(candidates) == 1 else None


if __name__ == "__main__":  # pragma: no cover - target entrypoint
    raise SystemExit(_main())
