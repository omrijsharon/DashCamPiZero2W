from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from dashcam.audio.alsa import AlsaMatchError, AlsaSelector, parse_alsa_selector
from dashcam.audio.linux import (
    DEFAULT_UDEV_TIMEOUT_SECONDS,
    MAX_UDEV_OUTPUT_BYTES,
    AudioDiscoveryError,
    AudioDiscoveryStatus,
    CapturePcmNode,
    discover_capture_device,
    enumerate_capture_pcm_nodes,
    parse_udev_properties,
)
from dashcam.diagnostics.media import CommandResult


def _selector() -> AlsaSelector:
    return AlsaSelector(
        "08bb",
        "2902",
        physical_path="platform-3f980000.usb-usb-0:1.2:1.0-sound-card1",
        product="USB_PnP_Sound_Device",
    )


def _node(card: int = 1, device: int = 0) -> CapturePcmNode:
    return CapturePcmNode(PurePosixPath(f"/dev/snd/pcmC{card}D{device}c"), card, device)


def _properties(node: CapturePcmNode, **overrides: str) -> bytes:
    values = {
        "DEVNAME": str(node.control_path),
        "ID_VENDOR_ID": "08bb",
        "ID_MODEL_ID": "2902",
        "ID_MODEL": "USB_PnP_Sound_Device",
        "ID_PATH": "platform-3f980000.usb-usb-0:1.2:1.0-sound-card1",
        "ALSA_CARD_NUMBER": str(node.card_index),
    }
    values.update(overrides)
    return "\n".join(f"{key}={value}" for key, value in values.items()).encode("ascii")


def _result(node: CapturePcmNode, **overrides: str) -> CommandResult:
    return CommandResult(
        argv=(), returncode=0, stdout=_properties(node, **overrides), stderr=b""
    )


@dataclass
class _PropertyRunner:
    results: dict[str, CommandResult]
    calls: list[tuple[tuple[str, ...], float, int]] = field(default_factory=list)

    def __call__(
        self, argv: Sequence[str], *, timeout_seconds: float, max_output_bytes: int
    ) -> CommandResult:
        self.calls.append((tuple(argv), timeout_seconds, max_output_bytes))
        return self.results[argv[-1]]


def test_discovery_observes_one_exact_usb_capture_node_with_fixed_udev_argv() -> None:
    node = _node()
    runner = _PropertyRunner({str(node.control_path): _result(node)})

    outcome = discover_capture_device(
        _selector(), node_enumerator=lambda _root: (node,), runner=runner
    )

    assert outcome.status is AudioDiscoveryStatus.MATCHED
    assert outcome.device is not None
    assert outcome.device.capture_endpoint == "hw:1,0,0"
    assert outcome.device.identity.product == "USB_PnP_Sound_Device"
    assert runner.calls == [
        (
            ("/usr/bin/udevadm", "info", "--query=property", "--name", "/dev/snd/controlC1"),
            DEFAULT_UDEV_TIMEOUT_SECONDS,
            MAX_UDEV_OUTPUT_BYTES,
        )
    ]


def test_discovery_distinguishes_absent_ambiguous_and_wrong_index_occupant() -> None:
    expected = _node(card=3)
    impostor = _node(card=1)

    absent = discover_capture_device(
        _selector(), node_enumerator=lambda _root: (), runner=_PropertyRunner({})
    )
    duplicate = _node(card=4)
    ambiguous = discover_capture_device(
        _selector(),
        node_enumerator=lambda _root: (expected, duplicate),
        runner=_PropertyRunner(
            {
                str(expected.control_path): _result(expected),
                str(duplicate.control_path): _result(duplicate),
            }
        ),
    )
    wrong_index = discover_capture_device(
        _selector(),
        node_enumerator=lambda _root: (impostor,),
        runner=_PropertyRunner(
            {
                str(impostor.control_path): _result(
                    impostor,
                    ID_PATH="platform-3f980000.usb-usb-0:1.3:1.0-sound-card1",
                )
            }
        ),
    )

    assert absent.status is AudioDiscoveryStatus.NOT_FOUND
    assert ambiguous.status is AudioDiscoveryStatus.AMBIGUOUS
    assert wrong_index.status is AudioDiscoveryStatus.NOT_FOUND


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            CommandResult(argv=(), returncode=1, stdout=b"", stderr=b"failure"),
            AudioDiscoveryStatus.REFUSED,
        ),
        (
            CommandResult(argv=(), returncode=-9, stdout=b"", stderr=b"", timed_out=True),
            AudioDiscoveryStatus.REFUSED,
        ),
        (
            CommandResult(argv=(), returncode=0, stdout=b"x", stderr=b"", output_truncated=True),
            AudioDiscoveryStatus.REFUSED,
        ),
    ],
)
def test_discovery_refuses_udev_timeout_nonzero_or_truncated_output(
    result: CommandResult, expected: AudioDiscoveryStatus
) -> None:
    node = _node()

    outcome = discover_capture_device(
        _selector(), node_enumerator=lambda _root: (node,), runner=lambda *_args, **_kwargs: result
    )

    assert outcome.status is expected
    assert outcome.device is None


def test_property_parser_ignores_long_unknown_udev_metadata_but_refuses_bad_identity() -> None:
    node = _node()
    parsed = parse_udev_properties(_properties(node) + b"\nDEVLINKS=" + (b"x" * 1024))
    assert parsed["DEVNAME"] == str(node.control_path)
    assert "DEVLINKS" not in parsed
    with pytest.raises(AudioDiscoveryError, match="invalid line"):
        parse_udev_properties(b"ID_VENDOR_ID=08bb\nnot-a-property")
    with pytest.raises(AudioDiscoveryError, match="invalid line"):
        parse_udev_properties(b"invalid-key=value")
    with pytest.raises(AudioDiscoveryError, match="byte bound"):
        parse_udev_properties(b"x" * (MAX_UDEV_OUTPUT_BYTES + 1))
    with pytest.raises(AudioDiscoveryError, match="repeats"):
        parse_udev_properties(_properties(node) + b"\nID_VENDOR_ID=9999")
    with pytest.raises(AudioDiscoveryError, match="identity field"):
        parse_udev_properties(_properties(node, ID_MODEL="x" * 129))
    with pytest.raises(AudioDiscoveryError, match="identity field"):
        parse_udev_properties(_properties(node, ID_MODEL="unsafe\x01"))


@pytest.mark.parametrize("unsafe_mode", [0o120000, 0o100000])
def test_default_enumeration_refuses_symlink_and_non_character_capture_nodes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unsafe_mode: int
) -> None:
    node = tmp_path / "pcmC1D0c"
    node.touch()

    def fake_lstat(path: Path) -> SimpleNamespace:
        if path == tmp_path:
            return SimpleNamespace(st_mode=0o040000)
        return SimpleNamespace(st_mode=unsafe_mode)

    monkeypatch.setattr("dashcam.audio.linux.os.lstat", fake_lstat)

    with pytest.raises(AudioDiscoveryError, match="not a real character device"):
        enumerate_capture_pcm_nodes(tmp_path)


def test_discovery_refuses_udev_node_substitution_and_invalid_selector() -> None:
    node = _node()
    substituted = discover_capture_device(
        _selector(),
        node_enumerator=lambda _root: (node,),
        runner=lambda *_args, **_kwargs: _result(node, DEVNAME="/dev/snd/controlC9"),
    )

    assert substituted.status is AudioDiscoveryStatus.REFUSED
    mismatched_card = discover_capture_device(
        _selector(),
        node_enumerator=lambda _root: (node,),
        runner=lambda *_args, **_kwargs: _result(node, ALSA_CARD_NUMBER="2"),
    )
    assert mismatched_card.status is AudioDiscoveryStatus.REFUSED
    with pytest.raises(AlsaMatchError, match="requires a USB product"):
        parse_alsa_selector("usb:vid=08bb,pid=2902,path=1-1.2")
