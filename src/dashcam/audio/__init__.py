"""Audio selection contracts that do not access ALSA devices directly."""

from dashcam.audio.alsa import (
    AlsaCaptureDevice,
    AlsaIdentity,
    AlsaMatchError,
    AlsaMatchOutcome,
    AlsaMatchStatus,
    AlsaSelector,
    parse_alsa_selector,
    resolve_capture_device,
)
from dashcam.audio.linux import (
    AudioDiscoveryError,
    AudioDiscoveryOutcome,
    AudioDiscoveryStatus,
    CapturePcmNode,
    discover_capture_device,
    enumerate_capture_pcm_nodes,
    parse_udev_properties,
)

__all__ = [
    "AlsaCaptureDevice",
    "AlsaIdentity",
    "AlsaMatchError",
    "AlsaMatchOutcome",
    "AlsaMatchStatus",
    "AlsaSelector",
    "AudioDiscoveryError",
    "AudioDiscoveryOutcome",
    "AudioDiscoveryStatus",
    "CapturePcmNode",
    "discover_capture_device",
    "enumerate_capture_pcm_nodes",
    "parse_alsa_selector",
    "parse_udev_properties",
    "resolve_capture_device",
]
