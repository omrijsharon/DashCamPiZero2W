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

__all__ = [
    "AlsaCaptureDevice",
    "AlsaIdentity",
    "AlsaMatchError",
    "AlsaMatchOutcome",
    "AlsaMatchStatus",
    "AlsaSelector",
    "parse_alsa_selector",
    "resolve_capture_device",
]
