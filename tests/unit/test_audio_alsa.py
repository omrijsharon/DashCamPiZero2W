from __future__ import annotations

import pytest

from dashcam.audio.alsa import (
    AlsaCaptureDevice,
    AlsaIdentity,
    AlsaMatchError,
    AlsaMatchStatus,
    AlsaSelector,
    parse_alsa_selector,
    resolve_capture_device,
)


def _device(*, card: int, serial: str = "mic-serial", path: str = "1-1.2") -> AlsaCaptureDevice:
    return AlsaCaptureDevice(
        AlsaIdentity("1234", "aBcD", serial, path, "USBMic", "USB_PnP_Sound_Device"),
        card,
        0,
    )


def test_stable_identity_match_uses_current_index_only_after_selection() -> None:
    selector = AlsaSelector(
        "1234",
        "abcd",
        serial="mic-serial",
        physical_path="1-1.2",
        product="USB_PnP_Sound_Device",
    )

    first = resolve_capture_device(selector, (_device(card=1),))
    reindexed = resolve_capture_device(selector, (_device(card=7),))

    assert first.status is AlsaMatchStatus.MATCHED
    assert first.device is not None
    assert first.device.capture_endpoint == "hw:1,0,0"
    assert reindexed.device is not None
    assert reindexed.device.capture_endpoint == "hw:7,0,0"


def test_same_volatile_card_index_cannot_substitute_a_different_microphone() -> None:
    selector = AlsaSelector(
        "1234", "abcd", serial="wanted", physical_path="1-1.2", product="USB_PnP_Sound_Device"
    )
    impostor = AlsaCaptureDevice(
        AlsaIdentity("1234", "abcd", "other", "1-1.2", product="USB_PnP_Sound_Device"), 1, 0
    )

    outcome = resolve_capture_device(selector, (impostor,))

    assert outcome.status is AlsaMatchStatus.NOT_FOUND
    assert outcome.device is None


def test_selector_can_bind_a_serialless_microphone_to_its_configured_usb_path() -> None:
    selector = AlsaSelector("1a2b", "3c4d", physical_path="1-1.4", product="USBMic")
    expected = AlsaCaptureDevice(
        AlsaIdentity("1A2B", "3C4D", physical_path="1-1.4", product="USBMic"), 3, 2
    )
    elsewhere = AlsaCaptureDevice(
        AlsaIdentity("1a2b", "3c4d", physical_path="1-1.5", product="USBMic"), 1, 0
    )

    outcome = resolve_capture_device(selector, (elsewhere, expected))

    assert outcome.status is AlsaMatchStatus.MATCHED
    assert outcome.device == expected


def test_configured_stable_selector_parser_rejects_volatile_alsa_endpoint() -> None:
    selector = parse_alsa_selector(
        "usb:vid=1234,pid=abcd,product=USB_PnP_Sound_Device,path=1-1.2,serial=mic-serial"
    )
    assert selector == AlsaSelector(
        "1234",
        "abcd",
        serial="mic-serial",
        physical_path="1-1.2",
        product="USB_PnP_Sound_Device",
    )

    with pytest.raises(AlsaMatchError, match="numeric ALSA"):
        parse_alsa_selector("hw:1,0")


def test_multiple_stable_matches_fail_closed() -> None:
    selector = AlsaSelector(
        "1234",
        "abcd",
        serial="mic-serial",
        physical_path="1-1.2",
        product="USB_PnP_Sound_Device",
    )

    outcome = resolve_capture_device(selector, (_device(card=1), _device(card=2)))

    assert outcome.status is AlsaMatchStatus.AMBIGUOUS
    assert outcome.device is None


@pytest.mark.parametrize(
    "factory",
    [
        lambda: AlsaIdentity("1234", "abcd"),
        lambda: AlsaSelector("1234", "abcd"),
        lambda: AlsaIdentity("123", "abcd", serial="x", physical_path="1-1.2", product="mic"),
        lambda: AlsaSelector(
            "1234", "abcd", serial="\n", physical_path="1-1.2", product="mic"
        ),
    ],
)
def test_unsafe_or_incomplete_stable_identity_is_refused(factory: object) -> None:
    with pytest.raises(AlsaMatchError):
        factory()  # type: ignore[operator]


def test_resolution_rejects_unbounded_or_mutable_discovery_inputs() -> None:
    selector = AlsaSelector(
        "1234",
        "abcd",
        serial="mic-serial",
        physical_path="1-1.2",
        product="USB_PnP_Sound_Device",
    )
    with pytest.raises(AlsaMatchError, match="immutable tuple"):
        resolve_capture_device(selector, [_device(card=1)])  # type: ignore[arg-type]
    with pytest.raises(AlsaMatchError, match="128-device"):
        resolve_capture_device(selector, tuple(_device(card=index) for index in range(129)))
