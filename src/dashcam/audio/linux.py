"""Bounded, fail-closed Linux discovery for a configured USB capture microphone.

This adapter observes ALSA capture PCM nodes and their card-control udev
properties. It does not open an ALSA device, start capture, or use a shell. A
node is usable only after its exact ``/dev/snd/pcmC*D*c`` name,
``controlC<card>`` character-device type, udev ``DEVNAME``, USB VID/PID,
product, and physical path all agree.
"""

from __future__ import annotations

import os
import re
import stat as stat_module
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePath
from typing import Final, Protocol

from dashcam.audio.alsa import (
    AlsaCaptureDevice,
    AlsaIdentity,
    AlsaMatchError,
    AlsaMatchStatus,
    AlsaSelector,
    resolve_capture_device,
)
from dashcam.diagnostics.media import CommandResult, run_fixed_argv

UDEVADM: Final = "/usr/bin/udevadm"
DEFAULT_SOUND_ROOT: Final = Path("/dev/snd")
DEFAULT_UDEV_TIMEOUT_SECONDS: Final = 5.0
MAX_UDEV_OUTPUT_BYTES: Final = 16 * 1024
MAX_CAPTURE_NODES: Final = 128
MAX_UDEV_LINES: Final = 256

_CAPTURE_PCM_NAME: Final = re.compile(r"pcmC([0-9]{1,3})D([0-9]{1,3})c")
_PROPERTY_KEY: Final = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_PROPERTY_VALUE: Final = re.compile(r"[ -~]{1,128}")
_USB_PATH: Final = re.compile(r"[A-Za-z0-9_.:/-]{1,128}")
_KNOWN_PROPERTIES: Final = frozenset(
    {
        "DEVNAME",
        "ID_MODEL",
        "ID_MODEL_ID",
        "ID_PATH",
        "ALSA_CARD_NUMBER",
        "ID_VENDOR_ID",
    }
)
_REQUIRED_PROPERTIES: Final = frozenset(
    {"ALSA_CARD_NUMBER", "DEVNAME", "ID_MODEL", "ID_MODEL_ID", "ID_PATH", "ID_VENDOR_ID"}
)


class AudioDiscoveryError(ValueError):
    """Raised for an unsafe or malformed local audio-device observation."""


class AudioDiscoveryStatus(StrEnum):
    """The only outcomes callers may act on for optional microphone capture."""

    MATCHED = "MATCHED"
    NOT_FOUND = "NOT_FOUND"
    AMBIGUOUS = "AMBIGUOUS"
    REFUSED = "REFUSED"


@dataclass(frozen=True, slots=True)
class CapturePcmNode:
    """One exact ALSA capture endpoint observed beneath ``/dev/snd``."""

    path: PurePath
    card_index: int
    pcm_device_index: int

    def __post_init__(self) -> None:
        if (
            self.path.parent.as_posix() != "/dev/snd"
            or _CAPTURE_PCM_NAME.fullmatch(self.path.name) is None
        ):
            raise AudioDiscoveryError("capture node must have an exact /dev/snd/pcmC*D*c path")
        fields = (("card_index", self.card_index), ("pcm_device_index", self.pcm_device_index))
        for name, value in fields:
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 999:
                raise AudioDiscoveryError(f"{name} must be a bounded non-negative integer")
        match = _CAPTURE_PCM_NAME.fullmatch(self.path.name)
        assert match is not None
        if (int(match.group(1)), int(match.group(2))) != (self.card_index, self.pcm_device_index):
            raise AudioDiscoveryError("capture node indexes must agree with its exact node name")

    @property
    def control_path(self) -> PurePath:
        """Return the exact control node whose udev ancestry describes this card."""

        return self.path.with_name(f"controlC{self.card_index}")


@dataclass(frozen=True, slots=True)
class AudioDiscoveryOutcome:
    """A fail-closed discovery result; failed outcomes expose no endpoint."""

    status: AudioDiscoveryStatus
    device: AlsaCaptureDevice | None = None

    def __post_init__(self) -> None:
        if self.status is AudioDiscoveryStatus.MATCHED and self.device is None:
            raise AudioDiscoveryError("a matched discovery outcome requires a device")
        if self.status is not AudioDiscoveryStatus.MATCHED and self.device is not None:
            raise AudioDiscoveryError("an unavailable discovery outcome cannot expose a device")

    @property
    def available(self) -> bool:
        """Whether it is safe for a later capture layer to use ``device``."""

        return self.status is AudioDiscoveryStatus.MATCHED


class UdevRunner(Protocol):
    """Run an already-tokenized udev command with fixed resource limits."""

    def __call__(
        self, argv: Sequence[str], *, timeout_seconds: float, max_output_bytes: int
    ) -> CommandResult: ...


CaptureNodeEnumerator = Callable[[Path], tuple[CapturePcmNode, ...]]


def enumerate_capture_pcm_nodes(
    sound_root: Path = DEFAULT_SOUND_ROOT,
) -> tuple[CapturePcmNode, ...]:
    """Return all actual character-device capture nodes from one sound directory.

    Any matching symlink or non-character node is an unsafe local state rather
    than an opportunity to follow it.  The result is sorted and bounded so
    discovery never depends on directory enumeration order.
    """

    if not sound_root.is_absolute():
        raise AudioDiscoveryError("sound root must be absolute")
    try:
        root_mode = os.lstat(sound_root).st_mode
    except OSError as exc:
        raise AudioDiscoveryError("cannot inspect sound root") from exc
    if stat_module.S_ISLNK(root_mode) or not stat_module.S_ISDIR(root_mode):
        raise AudioDiscoveryError("sound root must be a real directory")
    try:
        entries = tuple(sound_root.iterdir())
    except OSError as exc:
        raise AudioDiscoveryError("cannot enumerate sound root") from exc
    if len(entries) > MAX_CAPTURE_NODES * 4:
        raise AudioDiscoveryError("sound root contains too many entries")

    nodes: list[CapturePcmNode] = []
    for entry in entries:
        match = _CAPTURE_PCM_NAME.fullmatch(entry.name)
        if match is None:
            continue
        try:
            node_mode = os.lstat(entry).st_mode
        except OSError as exc:
            raise AudioDiscoveryError("cannot inspect capture PCM node") from exc
        if stat_module.S_ISLNK(node_mode) or not stat_module.S_ISCHR(node_mode):
            raise AudioDiscoveryError("capture PCM node is not a real character device")
        node = CapturePcmNode(entry, int(match.group(1)), int(match.group(2)))
        _validate_control_node(node)
        nodes.append(node)
    if len(nodes) > MAX_CAPTURE_NODES:
        raise AudioDiscoveryError("capture PCM node count exceeds bound")
    return tuple(
        sorted(nodes, key=lambda node: (node.card_index, node.pcm_device_index, str(node.path)))
    )


def discover_capture_device(
    selector: AlsaSelector,
    *,
    sound_root: Path = DEFAULT_SOUND_ROOT,
    node_enumerator: CaptureNodeEnumerator = enumerate_capture_pcm_nodes,
    runner: UdevRunner = run_fixed_argv,
) -> AudioDiscoveryOutcome:
    """Resolve exactly one configured USB capture endpoint, or refuse safely.

    Discovery failures intentionally collapse to ``REFUSED``.  Optional audio
    callers can record video-only for any non-matched result, while retaining a
    distinction between a normal absence, duplicate stable identity, and an
    untrustworthy local observation.
    """

    if not isinstance(selector, AlsaSelector):
        raise AudioDiscoveryError("selector must be an AlsaSelector")
    if not callable(node_enumerator) or not callable(runner):
        raise AudioDiscoveryError("discovery collaborators must be callable")
    try:
        nodes = node_enumerator(sound_root)
        if not isinstance(nodes, tuple) or len(nodes) > MAX_CAPTURE_NODES:
            raise AudioDiscoveryError("capture-node enumeration violated its bound")
        if not all(isinstance(node, CapturePcmNode) for node in nodes):
            raise AudioDiscoveryError("capture-node enumeration returned an invalid node")
        devices = tuple(_capture_device_from_udev(node, runner) for node in nodes)
        resolved = resolve_capture_device(selector, devices)
    except (AudioDiscoveryError, AlsaMatchError, OSError, ValueError):
        return AudioDiscoveryOutcome(AudioDiscoveryStatus.REFUSED)

    if resolved.status is AlsaMatchStatus.MATCHED:
        return AudioDiscoveryOutcome(AudioDiscoveryStatus.MATCHED, resolved.device)
    if resolved.status is AlsaMatchStatus.NOT_FOUND:
        return AudioDiscoveryOutcome(AudioDiscoveryStatus.NOT_FOUND)
    return AudioDiscoveryOutcome(AudioDiscoveryStatus.AMBIGUOUS)


def parse_udev_properties(raw: bytes) -> dict[str, str]:
    """Parse only bounded, printable udev ``KEY=VALUE`` properties.

    Unknown valid udev keys are deliberately ignored.  Every recognized key is
    accepted at most once; a duplicate required identity field is refused
    rather than depending on producer order.
    """

    if not isinstance(raw, bytes) or len(raw) > MAX_UDEV_OUTPUT_BYTES:
        raise AudioDiscoveryError("udev property output exceeds its byte bound")
    try:
        document = raw.decode("ascii", "strict")
    except UnicodeDecodeError as exc:
        raise AudioDiscoveryError("udev property output is not ASCII") from exc
    lines = document.splitlines()
    if len(lines) > MAX_UDEV_LINES:
        raise AudioDiscoveryError("udev property output exceeds its line bound")

    properties: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition("=")
        if not separator or _PROPERTY_KEY.fullmatch(key) is None:
            raise AudioDiscoveryError("udev property output contains an invalid line")
        if key not in _KNOWN_PROPERTIES:
            continue
        if _PROPERTY_VALUE.fullmatch(value) is None:
            raise AudioDiscoveryError("udev identity field is invalid")
        if key in properties:
            raise AudioDiscoveryError("udev property output repeats an identity field")
        properties[key] = value
    missing = _REQUIRED_PROPERTIES - properties.keys()
    if missing:
        raise AudioDiscoveryError("udev property output omits required identity fields")
    if _USB_PATH.fullmatch(properties["ID_PATH"]) is None:
        raise AudioDiscoveryError("udev physical path is unsafe")
    return properties


def _capture_device_from_udev(node: CapturePcmNode, runner: UdevRunner) -> AlsaCaptureDevice:
    result = runner(
        (UDEVADM, "info", "--query=property", "--name", str(node.control_path)),
        timeout_seconds=DEFAULT_UDEV_TIMEOUT_SECONDS,
        max_output_bytes=MAX_UDEV_OUTPUT_BYTES,
    )
    if not isinstance(result, CommandResult):
        raise AudioDiscoveryError("udev runner returned an invalid result")
    if result.timed_out or result.output_truncated or result.returncode != 0:
        raise AudioDiscoveryError("udev query did not complete safely")
    properties = parse_udev_properties(result.stdout)
    if properties["DEVNAME"] != str(node.control_path):
        raise AudioDiscoveryError("udev DEVNAME does not match the observed control node")
    if properties["ALSA_CARD_NUMBER"] != str(node.card_index):
        raise AudioDiscoveryError("udev ALSA card number does not match the observed capture node")
    try:
        identity = AlsaIdentity(
            vendor_id=properties["ID_VENDOR_ID"],
            product_id=properties["ID_MODEL_ID"],
            physical_path=properties["ID_PATH"],
            alsa_card_id=properties["ALSA_CARD_NUMBER"],
            product=properties["ID_MODEL"],
        )
    except AlsaMatchError as exc:
        raise AudioDiscoveryError("udev identity is unsafe") from exc
    return AlsaCaptureDevice(identity, node.card_index, node.pcm_device_index)


def _validate_control_node(node: CapturePcmNode) -> None:
    """Require the derived card-control endpoint to be an exact character node."""

    try:
        control_mode = os.lstat(node.control_path).st_mode
    except OSError as exc:
        raise AudioDiscoveryError("cannot inspect derived ALSA control node") from exc
    if stat_module.S_ISLNK(control_mode) or not stat_module.S_ISCHR(control_mode):
        raise AudioDiscoveryError("derived ALSA control node is not a real character device")
