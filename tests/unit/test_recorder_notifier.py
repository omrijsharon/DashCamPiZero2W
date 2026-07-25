from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from dashcam.recorder.notifier import (
    MAX_NOTIFY_STATUS_CHARS,
    NullNotifier,
    SystemdNotifier,
    UnixDatagramSender,
)


@dataclass
class RecordingSender:
    sent: list[tuple[str, bytes]] = field(default_factory=list)
    error: OSError | None = None

    def send(self, address: str, payload: bytes) -> None:
        if self.error is not None:
            raise self.error
        self.sent.append((address, payload))


def test_systemd_notifier_emits_protocol_messages() -> None:
    sender = RecordingSender()
    notifier = SystemdNotifier("/run/systemd/notify", sender=sender)

    assert notifier.enabled
    assert notifier.status("state=STARTING")
    assert notifier.ready("state=RECORDING")
    assert notifier.watchdog()
    assert notifier.stopping("state=STOPPING")

    assert sender.sent == [
        ("/run/systemd/notify", b"STATUS=state=STARTING"),
        ("/run/systemd/notify", b"READY=1\nSTATUS=state=RECORDING"),
        ("/run/systemd/notify", b"WATCHDOG=1"),
        ("/run/systemd/notify", b"STOPPING=1\nSTATUS=state=STOPPING"),
    ]


def test_abstract_notify_socket_is_translated_for_python() -> None:
    sender = RecordingSender()
    notifier = SystemdNotifier.from_environment(
        {"NOTIFY_SOCKET": "@dashcam-notify"},
        sender=sender,
    )

    assert notifier.ready("recording")
    assert sender.sent == [("\0dashcam-notify", b"READY=1\nSTATUS=recording")]


def test_absent_socket_is_a_successful_no_op() -> None:
    sender = RecordingSender()
    notifier = SystemdNotifier.from_environment({}, sender=sender)

    assert not notifier.enabled
    assert notifier.status("development host")
    assert notifier.ready("development host")
    assert notifier.watchdog()
    assert notifier.stopping("development host")
    assert sender.sent == []


def test_transport_failure_is_reported_without_raising() -> None:
    notifier = SystemdNotifier(
        "/run/systemd/notify",
        sender=RecordingSender(error=OSError("socket unavailable")),
    )

    assert not notifier.ready("recording")
    assert not notifier.status("recording")
    assert not notifier.watchdog()
    assert not notifier.stopping("stopping")


def test_status_text_is_sanitized_and_bounded() -> None:
    sender = RecordingSender()
    notifier = SystemdNotifier("/notify", sender=sender)

    notifier.status(f"one\ntwo\0three{'x' * 600}")

    payload = sender.sent[0][1]
    assert b"\0" not in payload
    assert payload.count(b"\n") == 0
    assert len(payload.removeprefix(b"STATUS=")) == MAX_NOTIFY_STATUS_CHARS


def test_notifier_constructor_and_sender_validate_bounds() -> None:
    with pytest.raises(ValueError, match="notify socket"):
        SystemdNotifier("")
    with pytest.raises(ValueError, match="timeout"):
        UnixDatagramSender(timeout_s=0)


def test_null_notifier_implements_successful_no_op() -> None:
    notifier = NullNotifier()

    assert notifier.ready("ready")
    assert notifier.status("status")
    assert notifier.watchdog()
    assert notifier.stopping("stopping")
