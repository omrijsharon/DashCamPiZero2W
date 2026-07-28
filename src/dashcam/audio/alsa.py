"""Stable ALSA capture-device matching.

ALSA card and PCM device numbers are observations used only to construct the
capture endpoint after a stable identity matched.  They are never selector
inputs, so an unplug/replug cannot silently substitute the current ``hw:1,0``
occupant for the configured microphone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

_HEX_ID: Final = re.compile(r"[0-9A-Fa-f]{4}")
_SAFE_TEXT: Final = re.compile(r"[ -~]{1,128}")
_USB_PATH: Final = re.compile(r"[A-Za-z0-9_.:/-]{1,128}")


class AlsaMatchError(ValueError):
    """Raised when an identity or selector could permit unsafe matching."""


class AlsaMatchStatus(StrEnum):
    """Explicit result of resolving one configured microphone."""

    MATCHED = "MATCHED"
    NOT_FOUND = "NOT_FOUND"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True, slots=True)
class AlsaIdentity:
    """Stable USB/udev evidence for an ALSA card.

    Product text and physical topology are required alongside USB VID/PID.
    ``serial`` is retained as additional evidence when supplied, but it is not
    a substitute for the configured topology: the reference microphone has no
    unique serial number.  Vendor/product IDs alone are never unique enough
    for automatic selection.
    """

    vendor_id: str
    product_id: str
    serial: str | None = None
    physical_path: str | None = None
    alsa_card_id: str | None = None
    product: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "vendor_id", _normalise_hex(self.vendor_id, "vendor_id"))
        object.__setattr__(self, "product_id", _normalise_hex(self.product_id, "product_id"))
        if self.serial is not None:
            _validate_text(self.serial, "serial")
        if self.product is None:
            raise AlsaMatchError("identity requires a USB product name")
        _validate_text(self.product, "product")
        if self.physical_path is None or _USB_PATH.fullmatch(self.physical_path) is None:
            raise AlsaMatchError("identity requires a bounded safe USB physical_path")
        if self.alsa_card_id is not None:
            _validate_text(self.alsa_card_id, "alsa_card_id")


@dataclass(frozen=True, slots=True)
class AlsaCaptureDevice:
    """One observed capture PCM; numeric indexes are output-only facts."""

    identity: AlsaIdentity
    card_index: int
    pcm_device_index: int
    pcm_subdevice_index: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.identity, AlsaIdentity):
            raise AlsaMatchError("identity must be an AlsaIdentity")
        for name, value in (
            ("card_index", self.card_index),
            ("pcm_device_index", self.pcm_device_index),
            ("pcm_subdevice_index", self.pcm_subdevice_index),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise AlsaMatchError(f"{name} must be a non-negative integer")

    @property
    def capture_endpoint(self) -> str:
        """Return the endpoint only after a stable selection has succeeded."""

        return f"hw:{self.card_index},{self.pcm_device_index},{self.pcm_subdevice_index}"


@dataclass(frozen=True, slots=True)
class AlsaSelector:
    """Configured stable match criteria, deliberately excluding ALSA indexes."""

    vendor_id: str
    product_id: str
    serial: str | None = None
    physical_path: str | None = None
    alsa_card_id: str | None = None
    product: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "vendor_id", _normalise_hex(self.vendor_id, "vendor_id"))
        object.__setattr__(self, "product_id", _normalise_hex(self.product_id, "product_id"))
        if self.serial is not None:
            _validate_text(self.serial, "serial")
        if self.product is None:
            raise AlsaMatchError("selector requires a USB product name")
        _validate_text(self.product, "product")
        if self.physical_path is None or _USB_PATH.fullmatch(self.physical_path) is None:
            raise AlsaMatchError("selector requires a bounded safe USB physical_path")
        if self.alsa_card_id is not None:
            _validate_text(self.alsa_card_id, "alsa_card_id")

    def matches(self, identity: AlsaIdentity) -> bool:
        """Return true only when every configured stable field agrees."""

        if not isinstance(identity, AlsaIdentity):
            raise AlsaMatchError("identity must be an AlsaIdentity")
        return (
            self.vendor_id == identity.vendor_id
            and self.product_id == identity.product_id
            and self.product == identity.product
            and (self.serial is None or self.serial == identity.serial)
            and self.physical_path == identity.physical_path
            and (self.alsa_card_id is None or self.alsa_card_id == identity.alsa_card_id)
        )


@dataclass(frozen=True, slots=True)
class AlsaMatchOutcome:
    """A bounded resolution result; no fallback endpoint is supplied on failure."""

    status: AlsaMatchStatus
    device: AlsaCaptureDevice | None = None

    def __post_init__(self) -> None:
        if self.status is AlsaMatchStatus.MATCHED and self.device is None:
            raise AlsaMatchError("a matched outcome requires a device")
        if self.status is not AlsaMatchStatus.MATCHED and self.device is not None:
            raise AlsaMatchError("a failed outcome must not expose a device")

    @property
    def available(self) -> bool:
        return self.status is AlsaMatchStatus.MATCHED


def parse_alsa_selector(device_match: str) -> AlsaSelector:
    """Parse the bounded stable-selector form stored in ``audio.device_match``.

    The only accepted form is, for example,
    ``usb:vid=08bb,pid=2902,product=USB_PnP_Sound_Device,path=1-1.2``.
    ``serial`` and ``card_id`` are optional additional constraints. Numeric
    ALSA endpoint strings are refused rather than interpreted as a fallback
    selector.
    """

    if not isinstance(device_match, str) or len(device_match) > 256 or not device_match.isascii():
        raise AlsaMatchError("device_match must be bounded printable ASCII")
    if device_match.startswith("hw:"):
        raise AlsaMatchError("numeric ALSA hardware endpoints are not stable selectors")
    if not device_match.startswith("usb:"):
        raise AlsaMatchError("device_match must use the usb: stable-selector form")
    fields: dict[str, str] = {}
    for item in device_match.removeprefix("usb:").split(","):
        key, separator, value = item.partition("=")
        if not separator or not key or not value or key in fields:
            raise AlsaMatchError("device_match contains an invalid or duplicate selector field")
        fields[key] = value
    if set(fields) - {"vid", "pid", "product", "serial", "path", "card_id"}:
        raise AlsaMatchError("device_match contains an unknown selector field")
    try:
        return AlsaSelector(
            vendor_id=fields["vid"],
            product_id=fields["pid"],
            serial=fields.get("serial"),
            physical_path=fields.get("path"),
            alsa_card_id=fields.get("card_id"),
            product=fields.get("product"),
        )
    except KeyError as exc:
        raise AlsaMatchError("device_match requires vid, pid, product, and path fields") from exc


def resolve_capture_device(
    selector: AlsaSelector, devices: tuple[AlsaCaptureDevice, ...]
) -> AlsaMatchOutcome:
    """Resolve exactly one stable match without inspecting index ordering.

    Callers should treat ``NOT_FOUND`` and ``AMBIGUOUS`` as video-only audio
    states.  The finite tuple requirement keeps the pure contract bounded even
    if its discovery adapter is faulty.
    """

    if not isinstance(selector, AlsaSelector):
        raise AlsaMatchError("selector must be an AlsaSelector")
    if not isinstance(devices, tuple):
        raise AlsaMatchError("devices must be an immutable tuple")
    if len(devices) > 128:
        raise AlsaMatchError("devices exceeds the 128-device discovery bound")
    if not all(isinstance(device, AlsaCaptureDevice) for device in devices):
        raise AlsaMatchError("devices must contain AlsaCaptureDevice values")

    matches = tuple(device for device in devices if selector.matches(device.identity))
    if len(matches) == 1:
        return AlsaMatchOutcome(AlsaMatchStatus.MATCHED, matches[0])
    if not matches:
        return AlsaMatchOutcome(AlsaMatchStatus.NOT_FOUND)
    return AlsaMatchOutcome(AlsaMatchStatus.AMBIGUOUS)


def _normalise_hex(value: str, name: str) -> str:
    if not isinstance(value, str) or _HEX_ID.fullmatch(value) is None:
        raise AlsaMatchError(f"{name} must be exactly four hexadecimal characters")
    return value.lower()


def _validate_text(value: str, name: str) -> None:
    if not isinstance(value, str) or _SAFE_TEXT.fullmatch(value) is None:
        raise AlsaMatchError(f"{name} must be 1 to 128 printable ASCII characters")
