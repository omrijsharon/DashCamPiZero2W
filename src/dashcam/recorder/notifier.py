"""Minimal, dependency-free systemd notification boundary."""

from __future__ import annotations

import os
import socket
from collections.abc import Mapping
from typing import Final, Protocol

MAX_NOTIFY_STATUS_CHARS: Final = 512


class ServiceNotifier(Protocol):
    """Supervisor notifications used by the recorder daemon."""

    def ready(self, status: str) -> bool:
        """Publish readiness and the current human-readable status."""

    def status(self, status: str) -> bool:
        """Update the human-readable service status."""

    def watchdog(self) -> bool:
        """Publish one watchdog heartbeat."""

    def stopping(self, status: str) -> bool:
        """Publish that orderly shutdown has started."""


class DatagramSender(Protocol):
    """Injectable local datagram transport used by :class:`SystemdNotifier`."""

    def send(self, address: str, payload: bytes) -> None:
        """Send one datagram or raise ``OSError``."""


class UnixDatagramSender:
    """Bounded Unix datagram sender for the systemd notify socket."""

    def __init__(self, timeout_s: float = 0.1) -> None:
        if not 0 < timeout_s <= 1:
            raise ValueError("notification timeout must be between 0 and 1 second")
        self._timeout_s = timeout_s

    def send(self, address: str, payload: bytes) -> None:
        address_family = getattr(socket, "AF_UNIX", None)
        if address_family is None:
            raise OSError("Unix-domain sockets are unavailable")
        with socket.socket(address_family, socket.SOCK_DGRAM) as notify_socket:
            notify_socket.settimeout(self._timeout_s)
            notify_socket.connect(address)
            notify_socket.sendall(payload)


class NullNotifier:
    """No-op notifier for tests and non-systemd development hosts."""

    def ready(self, status: str) -> bool:
        return True

    def status(self, status: str) -> bool:
        return True

    def watchdog(self) -> bool:
        return True

    def stopping(self, status: str) -> bool:
        return True


def _clean_status(status: str) -> str:
    if not isinstance(status, str):
        raise TypeError("status must be a string")
    cleaned = " ".join(status.replace("\0", " ").splitlines()).strip()
    if not cleaned:
        cleaned = "unknown"
    return cleaned[:MAX_NOTIFY_STATUS_CHARS]


class SystemdNotifier:
    """Best-effort implementation of the ``sd_notify`` datagram protocol."""

    def __init__(
        self,
        notify_socket: str | None,
        *,
        sender: DatagramSender | None = None,
    ) -> None:
        if notify_socket is not None and (not notify_socket or "\0" in notify_socket):
            raise ValueError("notify socket must be a non-empty filesystem or abstract path")
        self._address = (
            f"\0{notify_socket[1:]}"
            if notify_socket is not None and notify_socket.startswith("@")
            else notify_socket
        )
        self._sender = sender or UnixDatagramSender()

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
        *,
        sender: DatagramSender | None = None,
    ) -> SystemdNotifier:
        """Create a notifier from ``NOTIFY_SOCKET`` without retaining the environment."""

        values = os.environ if environment is None else environment
        return cls(values.get("NOTIFY_SOCKET"), sender=sender)

    @property
    def enabled(self) -> bool:
        return self._address is not None

    def _send(self, payload: str) -> bool:
        if self._address is None:
            return True
        try:
            self._sender.send(self._address, payload.encode("utf-8"))
        except OSError:
            return False
        return True

    def ready(self, status: str) -> bool:
        return self._send(f"READY=1\nSTATUS={_clean_status(status)}")

    def status(self, status: str) -> bool:
        return self._send(f"STATUS={_clean_status(status)}")

    def watchdog(self) -> bool:
        return self._send("WATCHDOG=1")

    def stopping(self, status: str) -> bool:
        return self._send(f"STOPPING=1\nSTATUS={_clean_status(status)}")
