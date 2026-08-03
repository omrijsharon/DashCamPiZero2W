"""Bounded GStreamer backend for the selected Raspberry Pi video graph.

Importing this module does not import PyGObject or initialize GStreamer.  The
default driver loads both only when :meth:`GStreamerBackend.start` is called;
tests and non-Pi control-plane processes can therefore import the recorder
package without target media dependencies.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import re
import stat
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from pathlib import Path
from threading import Event, Lock, Thread, get_ident
from types import MappingProxyType
from typing import Any, Final, Protocol, SupportsInt, cast

from dashcam.audio.alsa import AlsaCaptureDevice, AlsaIdentity
from dashcam.audio.linux import AudioDiscoveryOutcome, AudioDiscoveryStatus
from dashcam.config import AudioConfig
from dashcam.overlay.formatting import OVERLAY_1080P_LAYOUT
from dashcam.overlay.native_nv12 import (
    GstDmabufOverlayRenderer,
    NativeOverlayContractError,
    validate_native_overlay_dependencies,
    validate_native_overlay_text,
)
from dashcam.recorder.pipeline import (
    PipelineContractError,
    ProfileValidationError,
    RecoverablePipelineError,
    VideoProfile,
)
from dashcam.storage.naming import ClipNameError, parse_clip_filename, provisional_clip_pair

# On the target GStreamer 1.26 stack, GstStructure cannot deserialize the enum
# type name. GstQTMuxFragmentMode value 0 is the supported fragmented
# dash-or-mss mode. Value 1 (first-moov-then-finalise) reproducibly fails to
# deliver EOS even with a 25-second deadline, so it is not a shutdown-safe
# production choice on this image.
PIPELINE_DESCRIPTION = (
    "libcamerasrc name=camera ! "
    'capsfilter name=overlay_input caps="video/x-raw,width=(int)1920,'
    "height=(int)1080,format=(string)NV12,framerate=(fraction)30/1\" ! "
    "v4l2h264enc name=encoder "
    'extra-controls="controls,repeat_sequence_header=1,video_bitrate=8000000,'
    'h264_i_frame_period=30" ! '
    "video/x-h264,profile=(string)high,level=(string)4.1 ! "
    "h264parse name=parser config-interval=-1 ! "
    "queue name=record_queue max-size-buffers=60 max-size-bytes=4000000 "
    "max-size-time=2000000000 leaky=no ! "
    "splitmuxsink name=output max-size-time=60000000000 max-size-bytes=0 "
    "send-keyframe-requests=true async-finalize=true muxer-factory=mp4mux "
    'sink-factory=filesink muxer-properties="properties,fragment-duration=(uint)1000,'
    'fragment-mode=(int)0"'
)
_SPLITMUX_DESCRIPTION: Final = (
    "splitmuxsink name=output max-size-time=60000000000 max-size-bytes=0 "
    "send-keyframe-requests=true async-finalize=true muxer-factory=mp4mux "
    'sink-factory=filesink muxer-properties="properties,fragment-duration=(uint)1000,'
    'fragment-mode=(int)0"'
)
_ALSA_ENDPOINT: Final = re.compile(r"hw:[0-9]{1,3},[0-9]{1,3},[0-9]{1,3}")
_EXPECTED_AUDIO: Final = AudioConfig()
_MAX_PENDING_GENERATION_MESSAGES: Final = 64
_MAX_AUDIO_TIMESTAMPS: Final = 8192
_MAX_GENERATION_CONTEXTS: Final = 4
_MAX_QUARANTINED_AUDIO_ERRORS: Final = 4
_FORCED_IDR_EDGE_BOUND_NS: Final = 100_000_000
_MAX_OVERLAY_LINE_CHARS: Final = 96
_MAX_OVERLAY_LINES: Final = 2
_OVERLAY_UNSET: Final = object()
_MAX_FOREIGN_FORCE_KEY_EVENTS: Final = 8
_EXPECTED_QUARANTINED_AUDIO_ERROR_MARKERS: Final = (
    "Internal data stream error",
    "GstAlsaSrc:audio_source",
    "streaming stopped, reason error (-5)",
)
_POST_ROUTE_RESTORATION_PHASES: Final = frozenset(
    {
        "routed",
        "retiring_eos",
        "media_proof",
        "state_convergence",
        "continuity",
        "retired_closure",
        "recycle",
        "identity",
    }
)
AUDIO_BRANCH_ELEMENT_NAMES: Final = frozenset(
    {
        "audio_source",
        "audio_input_queue",
        "audio_convert",
        "audio_resample",
        "audio_encoder",
        "audio_parser",
        "audio_record_queue",
    }
)


class GStreamerDriverError(RuntimeError):
    """The target GStreamer binding failed or returned an invalid result."""


class GStreamerShutdownError(PipelineContractError):
    """The backend could not complete its bounded EOS/NULL shutdown."""


class AudioStartupError(RecoverablePipelineError):
    """A startup failure proven to be confined to the optional audio branch."""


class AudioRestorationCriticalError(GStreamerDriverError):
    """Restoration crossed the route boundary without a proven safe rollback."""

    def __init__(self, detail: str, *, phase: str = "unknown") -> None:
        super().__init__(detail)
        self.phase = phase


class BusMessageKind(Enum):
    NONE = "none"
    ERROR = "error"
    AUDIO_ERROR = "audio_error"
    EOS = "eos"
    FRAGMENT_OPENED = "fragment_opened"
    FRAGMENT_FINALIZED = "fragment_finalized"


@dataclass(frozen=True, slots=True)
class FragmentMessage:
    """Untrusted splitmuxsink closure fields read from the GStreamer bus."""

    location: str
    running_time_ns: int
    start_running_time_ns: int | None = None
    media_contract: FragmentMediaContract | None = None


@dataclass(frozen=True, slots=True)
class BusMessage:
    kind: BusMessageKind
    detail: str = ""
    fragment: FragmentMessage | None = None
    source_name: str | None = None


@dataclass(frozen=True, slots=True)
class EffectiveCaps:
    width: int
    height: int
    frames_per_second_numerator: int
    frames_per_second_denominator: int
    raw_format: str
    codec: str
    profile: str
    level: str


@dataclass(frozen=True, slots=True)
class AudioCapturePlan:
    """One immutable, stable-identity-derived ALSA capture allocation."""

    endpoint: str
    identity: AlsaIdentity
    sample_rate_hz: int
    channels: int
    codec: str
    bitrate_bps: int

    @classmethod
    def from_match(cls, device: AlsaCaptureDevice, config: AudioConfig) -> AudioCapturePlan:
        if not isinstance(device, AlsaCaptureDevice):
            raise ValueError("audio capture plan requires one matched ALSA device")
        if (
            not config.enabled
            or config.sample_rate_hz != _EXPECTED_AUDIO.sample_rate_hz
            or config.channels != _EXPECTED_AUDIO.channels
            or config.codec != _EXPECTED_AUDIO.codec
            or config.bitrate_bps != _EXPECTED_AUDIO.bitrate_bps
        ):
            raise ValueError("audio configuration differs from the production contract")
        return cls(
            device.capture_endpoint,
            device.identity,
            config.sample_rate_hz,
            config.channels,
            config.codec,
            config.bitrate_bps,
        )

    def __post_init__(self) -> None:
        if _ALSA_ENDPOINT.fullmatch(self.endpoint) is None:
            raise ValueError("audio endpoint must be an exact ALSA hw:C,D,S endpoint")
        if not isinstance(self.identity, AlsaIdentity):
            raise ValueError("audio capture plan identity is invalid")
        if (
            self.sample_rate_hz,
            self.channels,
            self.codec,
            self.bitrate_bps,
        ) != (48_000, 1, "aac", 128_000):
            raise ValueError("audio capture plan differs from the production contract")


@dataclass(frozen=True, slots=True)
class EffectiveAudioCaps:
    raw_format: str
    sample_rate_hz: int
    channels: int
    codec: str
    mpeg_version: int
    stream_format: str
    encoder_factory: str
    parser_factory: str
    bitrate_bps: int


@dataclass(frozen=True, slots=True)
class FragmentMediaContract:
    """Immutable generation-bound media truth for one fragment.

    Immutable recording generations attach this contract when they create
    fragment events.  Late asynchronous closures therefore retain the media
    shape of the generation which actually muxed them.
    """

    generation_id: int
    audio_caps: EffectiveAudioCaps | None
    encoded_audio_access_units: int | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.generation_id, bool)
            or not isinstance(self.generation_id, int)
            or not 1 <= self.generation_id <= 2**31 - 1
        ):
            raise ValueError("fragment generation_id must be a positive 32-bit integer")
        caps = self.audio_caps
        units = self.encoded_audio_access_units
        if units is not None and (
            isinstance(units, bool) or not isinstance(units, int) or units < 0
        ):
            raise ValueError("fragment audio access-unit count must be non-negative")
        if caps is None:
            if units not in (None, 0):
                raise ValueError("video-only fragment cannot report audio access units")
            return
        if not isinstance(caps, EffectiveAudioCaps):
            raise ValueError("fragment audio_caps must be EffectiveAudioCaps or None")
        if (
            caps.raw_format,
            caps.sample_rate_hz,
            caps.channels,
            caps.codec,
            caps.mpeg_version,
            caps.stream_format,
            caps.encoder_factory,
            caps.parser_factory,
            caps.bitrate_bps,
        ) != (
            "S16LE",
            48_000,
            1,
            "aac",
            4,
            "raw",
            "voaacenc",
            "aacparse",
            128_000,
        ):
            raise ValueError("fragment audio_caps differ from the production contract")


@dataclass(frozen=True, slots=True)
class AudioCounters:
    encoded_access_units: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.encoded_access_units, bool)
            or not isinstance(self.encoded_access_units, int)
            or self.encoded_access_units < 0
        ):
            raise ValueError("audio encoded-access-unit count must be non-negative")


@dataclass(frozen=True, slots=True)
class AudioLossArmProof:
    """Proof that exact ingress containment precedes disappearance confirmation."""

    activation_id: int
    slot_id: int
    source_name: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.activation_id, bool)
            or not isinstance(self.activation_id, int)
            or self.activation_id < 1
            or self.slot_id != 1
            or self.source_name != "audio_source"
        ):
            raise ValueError("audio-loss arm proof is invalid")


@dataclass(frozen=True, slots=True)
class ForcedIdrProof:
    """Exact ownership and timing proof for one forced loss boundary."""

    request_count: int
    request_seqnum: int
    downstream_seqnum: int
    seqnum_preserved: bool
    all_headers: bool
    nal5: bool
    request_monotonic_ns: int
    downstream_event_monotonic_ns: int
    idr_arrival_monotonic_ns: int
    downstream_running_time_ns: int
    forced_idr_running_time_ns: int
    event_to_idr_media_ns: int
    request_to_downstream_ns: int
    downstream_to_idr_ns: int
    request_to_idr_ns: int
    last_audio_end_running_time_ns: int
    edge_skew_ns: int
    edge_bound_ns: int = _FORCED_IDR_EDGE_BOUND_NS

    def __post_init__(self) -> None:
        integer_fields = (
            self.request_count,
            self.request_seqnum,
            self.downstream_seqnum,
            self.request_monotonic_ns,
            self.downstream_event_monotonic_ns,
            self.idr_arrival_monotonic_ns,
            self.downstream_running_time_ns,
            self.forced_idr_running_time_ns,
            self.event_to_idr_media_ns,
            self.request_to_downstream_ns,
            self.downstream_to_idr_ns,
            self.request_to_idr_ns,
            self.last_audio_end_running_time_ns,
            self.edge_skew_ns,
            self.edge_bound_ns,
        )
        time_fields = integer_fields[3:]
        if (
            any(isinstance(value, bool) or not isinstance(value, int) for value in integer_fields)
            or not 1 <= self.request_count <= 2**32 - 1
            or not 0 <= self.request_seqnum <= 2**32 - 1
            or not 0 <= self.downstream_seqnum <= 2**32 - 1
            or any(value < 0 or value == 2**64 - 1 for value in time_fields)
            or not isinstance(self.seqnum_preserved, bool)
            or self.all_headers is not True
            or self.nal5 is not True
            or self.downstream_event_monotonic_ns < self.request_monotonic_ns
            or self.idr_arrival_monotonic_ns < self.downstream_event_monotonic_ns
            or self.event_to_idr_media_ns < 0
            or self.request_to_downstream_ns
            != self.downstream_event_monotonic_ns - self.request_monotonic_ns
            or self.downstream_to_idr_ns
            != self.idr_arrival_monotonic_ns - self.downstream_event_monotonic_ns
            or self.request_to_idr_ns
            != self.idr_arrival_monotonic_ns - self.request_monotonic_ns
            or self.event_to_idr_media_ns
            != self.forced_idr_running_time_ns - self.downstream_running_time_ns
            or self.edge_skew_ns
            != self.forced_idr_running_time_ns - self.last_audio_end_running_time_ns
            or self.edge_bound_ns != _FORCED_IDR_EDGE_BOUND_NS
            or not 0 <= self.edge_skew_ns < self.edge_bound_ns
            or self.seqnum_preserved != (self.request_seqnum == self.downstream_seqnum)
        ):
            raise ValueError("forced-IDR proof violates the production contract")


@dataclass(frozen=True, slots=True)
class AudioLossHandoff:
    """Proof returned by one driver-owned immutable activation handoff.

    ``*_generation_id`` are monotonically increasing media activation IDs.
    ``*_slot_id`` identify the fixed recording slots which carried them.
    """

    retired_generation_id: int
    active_generation_id: int
    boundary_running_time_ns: int
    camera_identity_unchanged: bool
    encoder_identity_unchanged: bool
    successor_first_buffer_is_idr: bool
    successor_sticky_events_present: bool
    successor_observed_video_buffers: int
    successor_state_converged: bool
    forced_idr: ForcedIdrProof
    retired_slot_id: int = 1
    active_slot_id: int = 2
    retired_fragment_closed: bool = True
    request_pads_constant: bool = True

    def __post_init__(self) -> None:
        if (
            isinstance(self.retired_generation_id, bool)
            or not isinstance(self.retired_generation_id, int)
            or self.retired_generation_id < 1
            or isinstance(self.active_generation_id, bool)
            or not isinstance(self.active_generation_id, int)
            or self.active_generation_id <= self.retired_generation_id
            or isinstance(self.retired_slot_id, bool)
            or not isinstance(self.retired_slot_id, int)
            or self.retired_slot_id not in {1, 2, 3}
            or isinstance(self.active_slot_id, bool)
            or not isinstance(self.active_slot_id, int)
            or self.active_slot_id not in {1, 2, 3}
            or self.active_slot_id == self.retired_slot_id
            or not self.retired_fragment_closed
            or not self.request_pads_constant
            or isinstance(self.boundary_running_time_ns, bool)
            or not isinstance(self.boundary_running_time_ns, int)
            or self.boundary_running_time_ns < 0
            or not self.camera_identity_unchanged
            or not self.encoder_identity_unchanged
            or not self.successor_first_buffer_is_idr
            or not self.successor_sticky_events_present
            or isinstance(self.successor_observed_video_buffers, bool)
            or not isinstance(self.successor_observed_video_buffers, int)
            or self.successor_observed_video_buffers < 31
            or not self.successor_state_converged
            or not isinstance(self.forced_idr, ForcedIdrProof)
            or self.forced_idr.forced_idr_running_time_ns != self.boundary_running_time_ns
        ):
            raise ValueError("audio-loss handoff proof violates the production contract")


@dataclass(frozen=True, slots=True)
class AudioRestoreHandoff:
    """Proof of one bounded video-only to restored-A/V activation handoff."""

    retired_generation_id: int
    active_generation_id: int
    boundary_running_time_ns: int
    retired_slot_id: int
    active_slot_id: int
    camera_identity_unchanged: bool
    encoder_identity_unchanged: bool
    successor_first_buffer_is_idr: bool
    successor_sticky_events_present: bool
    successor_observed_video_buffers: int
    successor_observed_audio_units: int
    successor_state_converged: bool
    fixed_slot_count: int
    retired_fragment_closed: bool = True
    retired_slot_recycled: bool = True
    request_pads_constant: bool = True

    def __post_init__(self) -> None:
        if (
            isinstance(self.retired_generation_id, bool)
            or not isinstance(self.retired_generation_id, int)
            or self.retired_generation_id < 1
            or isinstance(self.active_generation_id, bool)
            or not isinstance(self.active_generation_id, int)
            or self.active_generation_id <= self.retired_generation_id
            or self.retired_slot_id not in {2, 3}
            or self.active_slot_id != 1
            or isinstance(self.boundary_running_time_ns, bool)
            or not isinstance(self.boundary_running_time_ns, int)
            or self.boundary_running_time_ns < 0
            or not self.camera_identity_unchanged
            or not self.encoder_identity_unchanged
            or not self.successor_first_buffer_is_idr
            or not self.successor_sticky_events_present
            or isinstance(self.successor_observed_video_buffers, bool)
            or not isinstance(self.successor_observed_video_buffers, int)
            or self.successor_observed_video_buffers < 31
            or isinstance(self.successor_observed_audio_units, bool)
            or not isinstance(self.successor_observed_audio_units, int)
            or self.successor_observed_audio_units < 1
            or not self.successor_state_converged
            or self.fixed_slot_count != 3
            or not self.retired_fragment_closed
            or not self.retired_slot_recycled
            or not self.request_pads_constant
        ):
            raise ValueError("audio-restoration handoff proof violates the production contract")


class _AudioEosArbiter:
    """Serialize exactly one forwarded audio EOS for retired A/V closure."""

    def __init__(self) -> None:
        self._lock = Lock()
        self.state = "OPEN"
        self.eos_count = 0
        self.eos_seqnum: int | None = None
        self.manual_seqnum: int | None = None
        self.barrier_seqnum: int | None = None
        self.barrier_seen = False
        self.duplicate = False
        self.retirement_armed = False
        self.generation_seqnum: int | None = None
        self.generation_foreign_eos_count = 0

    def arm_retirement(self) -> None:
        with self._lock:
            pristine_open = bool(
                self.state == "OPEN"
                and self.eos_count == 0
                and self.eos_seqnum is None
                and self.manual_seqnum is None
                and self.barrier_seqnum is None
                and not self.barrier_seen
                and not self.duplicate
                and self.generation_seqnum is None
                and self.generation_foreign_eos_count == 0
            )
            pristine_prearm_natural = bool(
                self.state == "NATURAL"
                and self.eos_count == 1
                and self.eos_seqnum is not None
                and self.manual_seqnum is None
                and self.barrier_seqnum is None
                and not self.barrier_seen
                and not self.duplicate
                and self.generation_seqnum is None
                and self.generation_foreign_eos_count == 0
            )
            if self.retirement_armed or not (pristine_open or pristine_prearm_natural):
                raise GStreamerDriverError(
                    "audio EOS containment cannot arm from its current state"
                )
            self.retirement_armed = True

    def is_retirement_armed(self) -> bool:
        with self._lock:
            return self.retirement_armed

    def has_forwarded_eos(self) -> bool:
        with self._lock:
            return bool(
                self.retirement_armed
                and not self.duplicate
                and self.eos_count == 1
                and self.eos_seqnum is not None
                and self.state in {"NATURAL", "MANUAL", "GENERATION"}
            )

    def reserve_generation_eos(self, seqnum: int) -> None:
        """Atomically prefer one exact whole-generation EOS over a late source EOS."""

        with self._lock:
            if (
                not self.retirement_armed
                or self.state != "OPEN"
                or self.eos_count != 0
                or self.eos_seqnum is not None
                or self.manual_seqnum is not None
                or self.barrier_seqnum is not None
                or self.barrier_seen
                or self.duplicate
                or self.generation_seqnum is not None
                or self.generation_foreign_eos_count != 0
                or isinstance(seqnum, bool)
                or not isinstance(seqnum, int)
                or not 0 <= seqnum <= 2**32 - 1
            ):
                raise GStreamerDriverError("generation EOS cannot reserve")
            self.generation_seqnum = seqnum
            self.state = "GENERATION_ARMED"

    def arm_barrier(self, seqnum: int) -> None:
        with self._lock:
            if not self.retirement_armed or self.state != "OPEN" or self.barrier_seqnum is not None:
                raise GStreamerDriverError("audio EOS barrier cannot be armed")
            self.barrier_seqnum = seqnum

    def observe_barrier(self, seqnum: int) -> bool:
        with self._lock:
            if self.barrier_seqnum != seqnum:
                return False
            self.barrier_seen = True
            if self.state == "OPEN":
                self.state = "BARRIER"
            return True

    def reserve_manual_eos(self, seqnum: int) -> None:
        with self._lock:
            if (
                not self.retirement_armed
                or self.state != "BARRIER"
                or not self.barrier_seen
                or self.eos_count != 0
                or self.eos_seqnum is not None
                or self.manual_seqnum is not None
                or isinstance(seqnum, bool)
                or not isinstance(seqnum, int)
                or not 0 <= seqnum <= 2**32 - 1
            ):
                raise GStreamerDriverError("manual audio EOS cannot reserve")
            self.manual_seqnum = seqnum

    def observe_eos(self, seqnum: int) -> bool:
        with self._lock:
            if self.state == "GENERATION_ARMED":
                if self.generation_seqnum == seqnum and self.eos_count == 0:
                    self.eos_count = 1
                    self.eos_seqnum = seqnum
                    self.state = "GENERATION"
                    return True
                self.generation_foreign_eos_count += 1
                if self.generation_foreign_eos_count > 1:
                    self.state = "REFUSED"
                    self.duplicate = True
                return False
            self.eos_count += 1
            if self.state == "OPEN" and self.manual_seqnum is None:
                self.state = "NATURAL"
                self.eos_seqnum = seqnum
                return True
            if (
                self.state == "BARRIER"
                and self.barrier_seen
                and self.eos_count == 1
                and self.manual_seqnum is None
            ):
                self.state = "NATURAL"
                self.eos_seqnum = seqnum
                return True
            if (
                self.state == "BARRIER"
                and self.barrier_seen
                and self.eos_count == 1
                and self.manual_seqnum == seqnum
            ):
                self.state = "MANUAL"
                self.eos_seqnum = seqnum
                return True
            self.state = "REFUSED"
            self.duplicate = True
            return False

    def boundary_kind(self) -> str | None:
        with self._lock:
            if not self.retirement_armed or self.duplicate:
                return None
            if self.state == "NATURAL" and self.eos_count == 1 and self.eos_seqnum is not None:
                return "NATURAL"
            if (
                self.state == "MANUAL"
                and self.barrier_seqnum is not None
                and self.barrier_seen
                and self.manual_seqnum is not None
                and self.eos_count == 1
                and self.eos_seqnum == self.manual_seqnum
            ):
                return "MANUAL"
            if (
                self.state == "GENERATION"
                and self.generation_seqnum is not None
                and self.eos_count == 1
                and self.eos_seqnum == self.generation_seqnum
                and self.generation_foreign_eos_count <= 1
            ):
                return "GENERATION"
            return None

    def snapshot(self) -> tuple[str, int, int | None, int | None, bool, bool]:
        with self._lock:
            return (
                self.state,
                self.eos_count,
                self.eos_seqnum,
                self.manual_seqnum,
                self.barrier_seen,
                self.duplicate,
            )

    def generation_snapshot(
        self,
    ) -> tuple[str, int, int | None, int | None, int, bool, bool]:
        with self._lock:
            return (
                self.state,
                self.eos_count,
                self.eos_seqnum,
                self.generation_seqnum,
                self.generation_foreign_eos_count,
                self.duplicate,
                self.retirement_armed,
            )


class AudioBranchState(Enum):
    """Pure dynamic-audio lifecycle; video ownership is deliberately absent."""

    ACTIVE = "active"
    QUIESCING = "quiescing"
    UNAVAILABLE = "unavailable"
    RESTORE_PENDING = "restore_pending"
    RESTORING = "restoring"
    FAULTED = "faulted"


class AudioBranchActionKind(Enum):
    QUIESCE = "quiesce"
    REDISCOVER = "rediscover"
    RESTORE = "restore"


class AudioReconnectObservation(Enum):
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    REFUSED = "refused"


@dataclass(frozen=True, slots=True)
class AudioBranchAction:
    kind: AudioBranchActionKind
    generation: int
    plan: AudioCapturePlan | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.kind, AudioBranchActionKind)
            or isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 1
        ):
            raise ValueError("audio branch action is invalid")
        if self.kind is AudioBranchActionKind.RESTORE:
            if not isinstance(self.plan, AudioCapturePlan):
                raise ValueError("audio restore action requires a capture plan")
        elif self.plan is not None:
            raise ValueError("only an audio restore action may carry a plan")


@dataclass(frozen=True, slots=True)
class AudioReconnectPolicy:
    interval_ns: int = 5_000_000_000
    max_attempts: int = 12
    campaign_cooldown_ns: int = 30_000_000_000

    def __post_init__(self) -> None:
        if (
            isinstance(self.interval_ns, bool)
            or not isinstance(self.interval_ns, int)
            or not 100_000_000 <= self.interval_ns <= 60_000_000_000
        ):
            raise ValueError("audio reconnect interval must be 0.1 to 60 seconds")
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or not 1 <= self.max_attempts <= 120
        ):
            raise ValueError("audio reconnect attempts must be between 1 and 120")
        if (
            isinstance(self.campaign_cooldown_ns, bool)
            or not isinstance(self.campaign_cooldown_ns, int)
            or not self.interval_ns <= self.campaign_cooldown_ns <= 600_000_000_000
        ):
            raise ValueError(
                "audio reconnect campaign cooldown must be at least the interval "
                "and at most 600 seconds"
            )


class AudioHotplugCoordinator:
    """Bounded pure controller for a future target-proven dynamic audio branch.

    This class never touches GStreamer or starts tasks.  The caller may have at
    most one rediscovery or mutation in flight because every transition is
    explicit and generation-bound.
    """

    def __init__(
        self,
        plan: AudioCapturePlan,
        *,
        policy: AudioReconnectPolicy | None = None,
    ) -> None:
        if not isinstance(plan, AudioCapturePlan):
            raise ValueError("audio hotplug coordinator requires a capture plan")
        self._original_plan = plan
        self._pending_plan: AudioCapturePlan | None = None
        self._policy = policy or AudioReconnectPolicy()
        self._state = AudioBranchState.ACTIVE
        self._generation = 0
        self._attempts = 0
        self._campaigns = 0
        self._next_attempt_ns: int | None = None
        self._rediscovery_in_flight = False
        self._stable_candidate: AudioCapturePlan | None = None
        self._stable_confirmations = 0
        self._reason = "active"

    @property
    def state(self) -> AudioBranchState:
        return self._state

    @property
    def active(self) -> bool:
        return self._state is AudioBranchState.ACTIVE

    def snapshot(self) -> dict[str, object]:
        return {
            "state": self._state.value,
            "generation": self._generation,
            "rediscovery_attempts": self._attempts,
            "rediscovery_campaigns": self._campaigns,
            "rediscovery_in_flight": self._rediscovery_in_flight,
            "stable_confirmations": self._stable_confirmations,
            "restore_pending": self._pending_plan is not None,
            "reason": self._reason,
        }

    def observe_loss(self, source_name: str) -> AudioBranchAction | None:
        if source_name not in AUDIO_BRANCH_ELEMENT_NAMES:
            raise ValueError("audio loss source is not an exact named audio element")
        if self._state is not AudioBranchState.ACTIVE:
            return None
        self._generation += 1
        self._attempts = 0
        self._pending_plan = None
        self._stable_candidate = None
        self._stable_confirmations = 0
        self._next_attempt_ns = None
        self._rediscovery_in_flight = False
        self._state = AudioBranchState.QUIESCING
        self._reason = f"loss:{source_name}"
        return AudioBranchAction(AudioBranchActionKind.QUIESCE, self._generation)

    def observe_quiesced(self, now_ns: int) -> None:
        self._validate_time(now_ns)
        if self._state is not AudioBranchState.QUIESCING:
            raise ValueError("audio branch is not awaiting quiesce completion")
        self._state = AudioBranchState.UNAVAILABLE
        self._next_attempt_ns = now_ns
        self._reason = "quiesced"

    def poll(self, now_ns: int) -> AudioBranchAction | None:
        self._validate_time(now_ns)
        if (
            self._state is not AudioBranchState.UNAVAILABLE
            or self._rediscovery_in_flight
            or self._next_attempt_ns is None
            or now_ns < self._next_attempt_ns
        ):
            return None
        if self._attempts >= self._policy.max_attempts:
            # Exhaustion is bounded per campaign, not permanent.  A missing
            # optional microphone must never require a recorder restart before
            # it can be considered again.
            self._attempts = 0
            self._campaigns += 1
            self._stable_candidate = None
            self._stable_confirmations = 0
            self._next_attempt_ns = now_ns + self._policy.campaign_cooldown_ns
            self._reason = "rediscovery_campaign_cooldown"
            return None
        self._attempts += 1
        self._rediscovery_in_flight = True
        self._reason = "rediscovering"
        return AudioBranchAction(AudioBranchActionKind.REDISCOVER, self._generation)

    def observe_rediscovery(
        self,
        generation: int,
        observation: AlsaCaptureDevice | AudioReconnectObservation,
        *,
        now_ns: int,
    ) -> None:
        self._validate_generation(generation)
        self._validate_time(now_ns)
        if self._state is not AudioBranchState.UNAVAILABLE or not self._rediscovery_in_flight:
            raise ValueError("audio rediscovery result is not currently expected")
        self._rediscovery_in_flight = False
        if isinstance(observation, AlsaCaptureDevice) and self._stable_match(observation):
            candidate = AudioCapturePlan(
                observation.capture_endpoint,
                observation.identity,
                self._original_plan.sample_rate_hz,
                self._original_plan.channels,
                self._original_plan.codec,
                self._original_plan.bitrate_bps,
            )
            if self._same_candidate(self._stable_candidate, candidate):
                self._stable_confirmations += 1
            else:
                self._stable_candidate = candidate
                self._stable_confirmations = 1
            if self._stable_confirmations >= 2:
                self._pending_plan = candidate
                self._state = AudioBranchState.RESTORE_PENDING
                self._next_attempt_ns = None
                self._reason = "stable_match_confirmed"
            else:
                self._next_attempt_ns = now_ns + self._policy.interval_ns
                self._reason = "stable_match_unconfirmed"
            return
        if not isinstance(observation, AlsaCaptureDevice | AudioReconnectObservation):
            raise ValueError("audio rediscovery observation is invalid")
        self._stable_candidate = None
        self._stable_confirmations = 0
        self._next_attempt_ns = now_ns + self._policy.interval_ns
        self._reason = (
            "wrong_identity" if isinstance(observation, AlsaCaptureDevice) else observation.value
        )

    def observe_fragment_boundary(self) -> AudioBranchAction | None:
        if self._state is not AudioBranchState.RESTORE_PENDING:
            return None
        plan = self._pending_plan
        if plan is None:
            raise RuntimeError("audio restore state lost its capture plan")
        self._state = AudioBranchState.RESTORING
        self._reason = "restoring_at_boundary"
        return AudioBranchAction(
            AudioBranchActionKind.RESTORE,
            self._generation,
            plan,
        )

    def observe_restored(self, generation: int) -> None:
        self._validate_generation(generation)
        if self._state is not AudioBranchState.RESTORING:
            raise ValueError("audio branch is not awaiting restoration")
        self._original_plan = cast(AudioCapturePlan, self._pending_plan)
        self._pending_plan = None
        self._stable_candidate = None
        self._stable_confirmations = 0
        self._next_attempt_ns = None
        self._rediscovery_in_flight = False
        self._attempts = 0
        self._state = AudioBranchState.ACTIVE
        self._reason = "active"

    def observe_restore_failed(self, generation: int, *, now_ns: int) -> None:
        self._validate_generation(generation)
        self._validate_time(now_ns)
        if self._state is not AudioBranchState.RESTORING:
            raise ValueError("audio branch is not awaiting restoration")
        self._pending_plan = None
        self._stable_candidate = None
        self._stable_confirmations = 0
        self._state = AudioBranchState.UNAVAILABLE
        self._next_attempt_ns = now_ns + self._policy.interval_ns
        self._reason = "restore_failed"

    def _stable_match(self, device: AlsaCaptureDevice) -> bool:
        expected = self._original_plan.identity
        observed = device.identity
        return (
            expected.vendor_id == observed.vendor_id
            and expected.product_id == observed.product_id
            and expected.product == observed.product
            and expected.physical_path == observed.physical_path
            and (expected.serial is None or expected.serial == observed.serial)
        )

    @staticmethod
    def _same_candidate(
        previous: AudioCapturePlan | None,
        current: AudioCapturePlan,
    ) -> bool:
        """Require two identical endpoint-and-stable-identity observations."""

        return previous == current

    def _validate_generation(self, generation: int) -> None:
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation != self._generation
        ):
            raise ValueError("audio action generation is stale")

    @staticmethod
    def _validate_time(now_ns: int) -> None:
        if isinstance(now_ns, bool) or not isinstance(now_ns, int) or now_ns < 0:
            raise ValueError("audio coordinator time must be non-negative nanoseconds")


def build_audio_pipeline_description(plan: AudioCapturePlan) -> str:
    """Build the parent video graph, EOS guard, and audio-ingress anchor.

    The owned ALSA/AAC ingress is installed as one named bin by the driver.
    Keeping it out of ``parse_launch`` prevents anonymous capsfilter children
    from escaping later replacement.  The permanently linked, bounded video
    continuity sink prevents branch-local retirement EOS from satisfying the
    parent bin's all-sinks EOS aggregation rule; genuine common-video EOS still
    reaches it and remains fatal.
    """

    if not isinstance(plan, AudioCapturePlan):
        raise ValueError("audio graph requires a validated capture plan")
    suffix = " ! " + _SPLITMUX_DESCRIPTION
    if not PIPELINE_DESCRIPTION.endswith(suffix):
        raise RuntimeError("video-only pipeline contract drifted unexpectedly")
    video = (
        PIPELINE_DESCRIPTION.removesuffix(suffix)
        + " ! identity name=video_generation_counter silent=true ! "
        "tee name=video_tee allow-not-linked=true "
        "video_tee. ! queue name=video_continuity_queue "
        "max-size-buffers=2 max-size-bytes=0 max-size-time=0 "
        "leaky=downstream ! fakesink name=video_continuity_sink "
        "sync=false async=false enable-last-sample=false qos=false "
    )
    # ``plan`` is still validated here and is separately handed to the driver;
    # never encode it into an unowned parse-launch child.
    audio = "tee name=audio_tee allow-not-linked=true"
    return video + audio


def build_audio_ingress_description(plan: AudioCapturePlan) -> str:
    """Build one independently replaceable ALSA-to-AAC ingress bin."""

    if not isinstance(plan, AudioCapturePlan):
        raise ValueError("audio ingress requires a validated capture plan")
    return (
        f"alsasrc name=audio_source device={plan.endpoint} provide-clock=false "
        "slave-method=resample use-driver-timestamps=false do-timestamp=true ! "
        "audio/x-raw,format=(string)S16LE,rate=(int)48000,channels=(int)1,"
        "layout=(string)interleaved ! "
        "queue name=audio_input_queue max-size-buffers=96 max-size-bytes=1048576 "
        "max-size-time=2000000000 leaky=downstream ! "
        "audioconvert name=audio_convert ! "
        "audioresample name=audio_resample ! "
        "audio/x-raw,format=(string)S16LE,rate=(int)48000,channels=(int)1,"
        "layout=(string)interleaved ! "
        "voaacenc name=audio_encoder bitrate=128000 ! "
        "aacparse name=audio_parser ! "
        "queue name=audio_record_queue max-size-buffers=192 max-size-bytes=1048576 "
        "max-size-time=2000000000 leaky=downstream ! "
        "identity name=audio_generation_counter silent=true"
    )


def build_legacy_audio_pipeline_description(plan: AudioCapturePlan) -> str:
    """Retain the accepted single-generation graph while qualification is gated."""

    if not isinstance(plan, AudioCapturePlan):
        raise ValueError("audio graph requires a validated capture plan")
    suffix = " ! " + _SPLITMUX_DESCRIPTION
    if not PIPELINE_DESCRIPTION.endswith(suffix):
        raise RuntimeError("video-only pipeline contract drifted unexpectedly")
    video = PIPELINE_DESCRIPTION.removesuffix(suffix) + " ! output.video "
    audio = (
        f"alsasrc name=audio_source device={plan.endpoint} provide-clock=false "
        "slave-method=resample use-driver-timestamps=false do-timestamp=true ! "
        "audio/x-raw,format=(string)S16LE,rate=(int)48000,channels=(int)1,"
        "layout=(string)interleaved ! "
        "queue name=audio_input_queue max-size-buffers=96 max-size-bytes=1048576 "
        "max-size-time=2000000000 leaky=downstream ! "
        "audioconvert name=audio_convert ! "
        "audioresample name=audio_resample ! "
        "audio/x-raw,format=(string)S16LE,rate=(int)48000,channels=(int)1,"
        "layout=(string)interleaved ! "
        "voaacenc name=audio_encoder bitrate=128000 ! "
        "aacparse name=audio_parser ! "
        "queue name=audio_record_queue max-size-buffers=192 max-size-bytes=1048576 "
        "max-size-time=2000000000 leaky=downstream ! output.audio_0 "
    )
    return video + audio + _SPLITMUX_DESCRIPTION


@dataclass(frozen=True, slots=True)
class EncoderIdentity:
    factory_name: str
    factory_class: str
    device_path: str


@dataclass(frozen=True, slots=True)
class FrameCounters:
    raw_frames: int
    encoded_access_units: int
    dropped_frames: int | None
    drop_source: str | None


class PipelineCounters:
    """O(1), lock-protected observations callable from GStreamer streaming threads."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._raw_frames = 0
        self._encoded_access_units = 0
        self._audio_encoded_access_units = 0
        self._qos_dropped_frames = 0
        self._pts_dropped_frames = 0
        self._qos_observed = False
        self._pts_seen = 0
        self._pts_unavailable = False
        self._last_pts_ns: int | None = None
        self._fps_numerator: int | None = None
        self._fps_denominator: int | None = None

    def configure_pts_cadence(self, numerator: int, denominator: int) -> None:
        if (
            isinstance(numerator, bool)
            or isinstance(denominator, bool)
            or not isinstance(numerator, int)
            or not isinstance(denominator, int)
            or numerator < 1
            or denominator < 1
        ):
            raise ValueError("PTS cadence must be positive integers")
        with self._lock:
            self._fps_numerator = numerator
            self._fps_denominator = denominator

    def observe_raw_buffer(self) -> None:
        with self._lock:
            self._raw_frames += 1

    def observe_raw_pts(self, pts_ns: int | None) -> None:
        """Observe encoder-input PTS; invalid/regressing timing stays unavailable."""

        missing_periods = 0
        previous_pts = 0
        with self._lock:
            if self._pts_unavailable or pts_ns is None or isinstance(pts_ns, bool) or pts_ns < 0:
                self._pts_unavailable = True
                return
            numerator = self._fps_numerator
            denominator = self._fps_denominator
            previous = self._last_pts_ns
            if numerator is None or denominator is None or previous is None:
                self._last_pts_ns = pts_ns
                self._pts_seen += 1
                return
            gap = pts_ns - previous
            scaled_gap = gap * numerator
            # Nearest integer frame count. Permit at most one quarter of a
            # frame period as bounded sensor/clock jitter; a missing whole
            # period cannot fit inside that tolerance.
            periods = (scaled_gap + 500_000_000 * denominator) // (1_000_000_000 * denominator)
            error = abs(scaled_gap - periods * 1_000_000_000 * denominator)
            if periods < 1 or error * 4 > 1_000_000_000 * denominator:
                self._pts_unavailable = True
                return
            self._pts_seen += 1
            self._last_pts_ns = pts_ns
            if periods > 1:
                missing_periods = periods - 1
                previous_pts = previous
                self._pts_dropped_frames += missing_periods
        if missing_periods and os.environ.get("DASHCAM_HANDOFF_TRACE") == "1":
            with suppress(OSError):
                os.write(
                    2,
                    (
                        "dashcam-encoder-input-pts-gap"
                        f" monotonic_ns={time.monotonic_ns()}"
                        f" missing_frames={missing_periods}"
                        f" previous_pts_ns={previous_pts}"
                        f" current_pts_ns={pts_ns}\n"
                    ).encode("ascii"),
                )

    def observe_encoded_buffer(self) -> None:
        with self._lock:
            self._encoded_access_units += 1

    def observe_audio_encoded_buffer(self) -> None:
        with self._lock:
            self._audio_encoded_access_units += 1

    def audio_snapshot(self) -> AudioCounters:
        with self._lock:
            return AudioCounters(self._audio_encoded_access_units)

    def observe_qos_drop(self, dropped: int) -> None:
        if isinstance(dropped, bool) or not isinstance(dropped, int) or dropped < 0:
            raise ValueError("QoS dropped-frame count must be non-negative")
        with self._lock:
            self._qos_observed = True
            self._qos_dropped_frames += dropped

    def snapshot(self) -> FrameCounters:
        with self._lock:
            if self._qos_observed and (
                self._pts_seen < 2
                or self._pts_unavailable
                or self._qos_dropped_frames >= self._pts_dropped_frames
            ):
                dropped, source = self._qos_dropped_frames, "gstreamer-qos"
            elif self._pts_seen >= 2 and not self._pts_unavailable:
                dropped, source = self._pts_dropped_frames, "encoder-input-pts-gap"
            else:
                dropped, source = None, None
            return FrameCounters(
                self._raw_frames,
                self._encoded_access_units,
                dropped,
                source,
            )


@dataclass(frozen=True, slots=True)
class FinalizedFragment:
    """A validated, asynchronously finalized provisional MP4 fragment."""

    path: Path
    sequence: int
    running_time_ns: int
    start_running_time_ns: int | None = None
    media_contract: FragmentMediaContract | None = None

    def __post_init__(self) -> None:
        start = self.start_running_time_ns
        if start is not None and (
            isinstance(start, bool)
            or not isinstance(start, int)
            or start < 0
            or start >= self.running_time_ns
        ):
            raise ValueError("finalized fragment start_running_time_ns must precede its end")
        if self.media_contract is not None and not isinstance(
            self.media_contract, FragmentMediaContract
        ):
            raise ValueError(
                "finalized fragment media_contract must be FragmentMediaContract or None"
            )


@dataclass(frozen=True, slots=True)
class OpenedFragment:
    """The first validated provisional MP4 opened by splitmuxsink."""

    path: Path
    sequence: int
    running_time_ns: int
    start_running_time_ns: int | None = None
    media_contract: FragmentMediaContract | None = None

    def __post_init__(self) -> None:
        start = self.start_running_time_ns
        if start is not None and (
            isinstance(start, bool)
            or not isinstance(start, int)
            or start < 0
            or start != self.running_time_ns
        ):
            raise ValueError("opened fragment start_running_time_ns must equal its running time")
        if self.media_contract is not None and not isinstance(
            self.media_contract, FragmentMediaContract
        ):
            raise ValueError("opened fragment media_contract must be FragmentMediaContract or None")


@dataclass(frozen=True, slots=True)
class SegmentedOutputConfig:
    """Application-owned provisional output namespace for one backend run."""

    output_directory: Path
    boot_id: str
    start_index: int = 0
    event_capacity: int = 16
    expected_st_dev: int | None = None

    def __post_init__(self) -> None:
        directory = self.output_directory
        if not isinstance(directory, Path):
            raise ValueError("output_directory must be a pathlib Path")
        directory_text = directory.as_posix()
        if (
            directory_text != "/srv/dashcam/pending"
            or ".." in directory.parts
            or "%" in directory_text
            or not directory_text.isascii()
            or not directory_text.isprintable()
            or len(directory_text) > 4096
        ):
            raise ValueError(
                "output_directory must be an absolute ASCII path to an "
                "application pending directory"
            )
        if (
            isinstance(self.start_index, bool)
            or not isinstance(self.start_index, int)
            or not 0 <= self.start_index <= 999_999
        ):
            raise ValueError("start_index must be an integer between 0 and 999999")
        if (
            isinstance(self.event_capacity, bool)
            or not isinstance(self.event_capacity, int)
            or not 1 <= self.event_capacity <= 1024
        ):
            raise ValueError("event_capacity must be an integer between 1 and 1024")
        if self.expected_st_dev is not None and (
            isinstance(self.expected_st_dev, bool)
            or not isinstance(self.expected_st_dev, int)
            or self.expected_st_dev < 0
        ):
            raise ValueError("expected_st_dev must be a non-negative integer")
        try:
            provisional_clip_pair(boot_id=self.boot_id, sequence=self.start_index)
        except ClipNameError as error:
            raise ValueError(f"invalid provisional output identity: {error}") from error

    @property
    def location_pattern(self) -> str:
        """Return the only filename pattern passed to splitmuxsink."""

        return (self.output_directory / f"boot-{self.boot_id}-%06d.partial.mp4").as_posix()

    @property
    def initial_paths(self) -> tuple[Path, Path]:
        """Return the first MP4/JSON names which this backend must not overwrite."""

        pair = provisional_clip_pair(boot_id=self.boot_id, sequence=self.start_index)
        return (
            self.output_directory / pair.video_name,
            self.output_directory / pair.metadata_name,
        )

    def after_reconciling(self, existing_names: Sequence[str]) -> SegmentedOutputConfig:
        """Choose the next same-boot sequence from one bounded directory snapshot."""

        if (
            isinstance(existing_names, str | bytes)
            or not isinstance(existing_names, Sequence)
            or len(existing_names) > 100_000
        ):
            raise ValueError("existing_names must be a bounded filename sequence")
        maximum = -1
        seen: set[str] = set()
        for name in existing_names:
            if not isinstance(name, str):
                raise ValueError("existing_names contains a non-string entry")
            folded = name.casefold()
            if folded in seen:
                raise ValueError("pending directory contains a case-insensitive collision")
            seen.add(folded)
            try:
                parsed = parse_clip_filename(name)
            except ClipNameError as error:
                raise ValueError("pending directory contains an unrecognized entry") from error
            if not parsed.provisional or not parsed.partial:
                raise ValueError("pending directory contains a non-partial clip name")
            if parsed.boot_id == self.boot_id:
                maximum = max(maximum, parsed.sequence)
        next_index = max(self.start_index, maximum + 1)
        if next_index > 999_999:
            raise ValueError("pending sequence space is exhausted")
        return replace(self, start_index=next_index)

    def _validated_fragment_fields(self, message: FragmentMessage) -> tuple[Path, int, int]:
        if (
            not isinstance(message.location, str)
            or not message.location
            or len(message.location) > 4096
            or not message.location.isascii()
            or not message.location.isprintable()
        ):
            raise ValueError("fragment location is not a bounded ASCII path")
        if (
            isinstance(message.running_time_ns, bool)
            or not isinstance(message.running_time_ns, int)
            or not 0 <= message.running_time_ns <= 2**64 - 1
        ):
            raise ValueError("fragment running time is invalid")

        path = Path(message.location)
        if (
            not path.as_posix().startswith("/")
            or ".." in path.parts
            or path.parent != self.output_directory
        ):
            raise ValueError("fragment escaped the configured output directory")
        try:
            parsed = parse_clip_filename(path.name)
        except ClipNameError as error:
            raise ValueError(f"fragment filename is invalid: {error}") from error
        if (
            not parsed.provisional
            or not parsed.partial
            or parsed.extension != "mp4"
            or parsed.boot_id != self.boot_id
            or parsed.sequence < self.start_index
            or path
            != self.output_directory / f"boot-{self.boot_id}-{parsed.sequence:06d}.partial.mp4"
        ):
            raise ValueError("fragment does not match the configured output identity")
        return path, parsed.sequence, message.running_time_ns

    def finalized_fragment(self, message: FragmentMessage) -> FinalizedFragment:
        """Validate one untrusted closure message against this output namespace."""

        return FinalizedFragment(
            *self._validated_fragment_fields(message),
            message.start_running_time_ns,
            message.media_contract,
        )

    def opened_fragment(self, message: FragmentMessage) -> OpenedFragment:
        """Validate one untrusted open message against this output namespace."""

        return OpenedFragment(
            *self._validated_fragment_fields(message),
            message.start_running_time_ns,
            message.media_contract,
        )


class GStreamerDriver(Protocol):
    """Small synchronous seam around the target GStreamer binding."""

    def create_pipeline(
        self,
        description: str,
        location_pattern: str,
        start_index: int,
        audio_plan: AudioCapturePlan | None = None,
    ) -> object:
        """Parse and configure one pipeline without changing its state."""

    def set_playing(self, pipeline: object, timeout_s: float) -> None:
        """Enter and verify PLAYING within ``timeout_s``."""

    def set_overlay_text(self, pipeline: object, text: str | None) -> None:
        """Update or silence the bounded burned-in overlay."""

    def overlay_snapshot(self, pipeline: object) -> dict[str, object]:
        """Return bounded transform counters without frame or telemetry data."""

    def effective_caps(self, pipeline: object) -> EffectiveCaps:
        """Read the negotiated encoder sink/source caps."""

    def encoder_identity(self, pipeline: object) -> EncoderIdentity:
        """Read the selected encoder factory and dynamically chosen device."""

    def poll_bus(self, pipeline: object, timeout_s: float) -> BusMessage:
        """Return one ERROR/EOS message, or NONE when the poll expires."""

    def send_eos(self, pipeline: object) -> bool:
        """Request end-of-stream processing."""

    def set_null(self, pipeline: object, timeout_s: float) -> None:
        """Enter and verify NULL within ``timeout_s``."""

    def arm_audio_loss(
        self,
        pipeline: object,
        source_name: str,
    ) -> AudioLossArmProof:
        """Arm exact ingress containment before disappearance confirmation."""

    def isolate_audio_loss(
        self,
        pipeline: object,
        timeout_s: float,
    ) -> AudioLossHandoff:
        """Switch one proven-lost A/V generation to its video-only successor."""

    def restore_audio(
        self,
        pipeline: object,
        plan: AudioCapturePlan,
        timeout_s: float,
    ) -> AudioRestoreHandoff:
        """Rebuild audio ingress and switch to the reusable A/V slot."""

    def generation_snapshot(self, pipeline: object) -> dict[str, object]:
        """Return one bounded, read-only three-slot ownership snapshot."""


@dataclass(frozen=True, slots=True)
class GStreamerLimits:
    start_timeout_s: float = 15.0
    bus_poll_s: float = 0.1
    eos_timeout_s: float = 8.0
    null_timeout_s: float = 3.0
    audio_loss_poll_interval_s: float = 0.5
    audio_handoff_timeout_s: float = 20.0
    audio_restore_poll_interval_s: float = 5.0
    audio_restore_attempts_per_campaign: int = 12
    audio_restore_campaign_cooldown_s: float = 30.0
    audio_restore_handoff_timeout_s: float = 20.0

    def __post_init__(self) -> None:
        for name, value, maximum in (
            ("start_timeout_s", self.start_timeout_s, 120.0),
            ("bus_poll_s", self.bus_poll_s, 5.0),
            ("eos_timeout_s", self.eos_timeout_s, 60.0),
            ("null_timeout_s", self.null_timeout_s, 60.0),
            (
                "audio_loss_poll_interval_s",
                self.audio_loss_poll_interval_s,
                5.0,
            ),
            ("audio_handoff_timeout_s", self.audio_handoff_timeout_s, 30.0),
            (
                "audio_restore_poll_interval_s",
                self.audio_restore_poll_interval_s,
                60.0,
            ),
            (
                "audio_restore_campaign_cooldown_s",
                self.audio_restore_campaign_cooldown_s,
                600.0,
            ),
            (
                "audio_restore_handoff_timeout_s",
                self.audio_restore_handoff_timeout_s,
                30.0,
            ),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not 0 < value <= maximum
            ):
                raise ValueError(f"{name} must be greater than zero and at most {maximum}")
        if (
            isinstance(self.audio_restore_attempts_per_campaign, bool)
            or not isinstance(self.audio_restore_attempts_per_campaign, int)
            or not 1 <= self.audio_restore_attempts_per_campaign <= 120
        ):
            raise ValueError("audio_restore_attempts_per_campaign must be between 1 and 120")
        if self.audio_restore_campaign_cooldown_s < self.audio_restore_poll_interval_s:
            raise ValueError(
                "audio restore campaign cooldown cannot be shorter than its poll interval"
            )


def _bounded_detail(error: BaseException | str) -> str:
    value = str(error)
    detail = " ".join(value.replace("\0", " ").splitlines())
    return detail[:512] or type(error).__name__


def _validate_overlay_text(text: str | None) -> None:
    """Validate one fixed-region overlay payload before touching GStreamer."""

    if text is None:
        return
    if not isinstance(text, str):
        raise ValueError("overlay text must be a string or None")
    lines = text.split("\n")
    if (
        len(lines) > _MAX_OVERLAY_LINES
        or any(
            len(line) > _MAX_OVERLAY_LINE_CHARS
            or not line.isascii()
            or any(not character.isprintable() for character in line)
            for line in lines
        )
    ):
        raise ValueError("overlay text exceeds the fixed two-line ASCII bounds")
    validate_native_overlay_text(text, OVERLAY_1080P_LAYOUT)


def _device_path_is_dynamic_video_node(value: str) -> bool:
    prefix = "/dev/video"
    return value.startswith(prefix) and value[len(prefix) :].isdecimal()


def _dynamic_attribute(target: object, name: str) -> object:
    try:
        return getattr(target, name)
    except AttributeError as error:
        raise GStreamerDriverError(f"dynamic object lacks {name}") from error


class GStreamerBackend:
    """One single-use implementation of the recorder ``PipelineBackend``."""

    def __init__(
        self,
        *,
        output: SegmentedOutputConfig,
        audio_plan: AudioCapturePlan | None = None,
        driver: GStreamerDriver | None = None,
        limits: GStreamerLimits | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        path_exists: Callable[[Path], bool] = Path.exists,
        path_lstat: Callable[[Path], os.stat_result] = os.lstat,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        enable_audio_loss_isolation: bool = False,
        enable_audio_restoration: bool = False,
    ) -> None:
        self._output = output
        if audio_plan is not None and not isinstance(audio_plan, AudioCapturePlan):
            raise ValueError("audio_plan must be an AudioCapturePlan or None")
        self._audio_plan = audio_plan
        self._driver = driver
        self._limits = limits or GStreamerLimits()
        self._monotonic = monotonic
        self._path_exists = path_exists
        self._path_lstat = path_lstat
        self._sleep = sleep
        if not isinstance(enable_audio_loss_isolation, bool):
            raise ValueError("audio-loss isolation feature gate must be boolean")
        self._enable_audio_loss_isolation = enable_audio_loss_isolation
        if not isinstance(enable_audio_restoration, bool):
            raise ValueError("audio restoration feature gate must be boolean")
        if enable_audio_restoration and not enable_audio_loss_isolation:
            raise ValueError("audio restoration requires audio-loss isolation")
        self._enable_audio_restoration = enable_audio_restoration
        self._pipeline: object | None = None
        self._started_once = False
        self._driver_lock = asyncio.Lock()
        self._shutdown_task: asyncio.Task[None] | None = None
        self._null_cleanup_pending = False
        self._finalized_fragments: asyncio.Queue[FinalizedFragment] = asyncio.Queue(
            maxsize=output.event_capacity
        )
        self._first_fragment_opened = asyncio.Event()
        self._opened_fragment: OpenedFragment | None = None
        self._active_fragment: OpenedFragment | None = None
        self._effective_caps: EffectiveCaps | None = None
        self._effective_audio_caps: EffectiveAudioCaps | None = None
        self._encoder_identity: EncoderIdentity | None = None
        self._counters = PipelineCounters()
        self._audio_loss_probe: Callable[[], AudioDiscoveryOutcome] | None = None
        self._audio_loss_isolated = False
        self._audio_loss_messages = 0
        self._audio_hotplug: AudioHotplugCoordinator | None = None
        self._audio_loss_count = 0
        self._audio_restoration_count = 0
        self._last_loss_handoff: AudioLossHandoff | None = None
        self._last_restore_handoff: AudioRestoreHandoff | None = None
        self._audio_restoration_failure: dict[str, object] | None = None
        self._audio_loss_classification = "not_observed"
        self._audio_loss_observations: tuple[dict[str, object], ...] = ()
        self._initial_media_contract: FragmentMediaContract | None = None
        self._fragment_audio_counter_baseline = 0
        self._configured_overlay_text: str | object | None = _OVERLAY_UNSET
        self._last_overlay_text: str | object | None = _OVERLAY_UNSET

    @property
    def audio_loss_isolated(self) -> bool:
        return self._audio_loss_isolated

    @property
    def audio_restoration_snapshot(self) -> dict[str, object]:
        """Expose bounded reconnect/topology evidence without internal reach-through."""

        coordinator = self._audio_hotplug
        topology: dict[str, object] = {
            "topology_observation": "unavailable",
            "topology_observation_stale": False,
            "active_slot_id": None,
            "active_activation_id": None,
            "slot_count": 0,
            "slot_activations": {},
            "request_pad_invariant": "unavailable",
            "video_tee_request_pads": 0,
            "audio_tee_request_pads": 0,
            "splitmux_video_request_pads": 0,
            "splitmux_audio_request_pads": 0,
            "request_pad_counts_measured": False,
            "request_pad_peer_ownership_proven": False,
            "tee_pad_routes": {},
            "audio_ingress": {
                "current_count": 0,
                "current_descendant_count": 0,
                "stale_descendant_count": 0,
                "replacement_count": 0,
            },
        }
        pipeline = self._pipeline
        driver = self._driver
        snapshot = getattr(driver, "generation_snapshot", None)
        if pipeline is not None and callable(snapshot):
            observed = snapshot(pipeline)
            if isinstance(observed, dict):
                topology = observed
        reconnect = (
            {
                "state": "disabled",
                "generation": 0,
                "rediscovery_attempts": 0,
                "rediscovery_campaigns": 0,
                "rediscovery_in_flight": False,
                "stable_confirmations": 0,
                "restore_pending": False,
                "reason": "restoration_disabled",
            }
            if coordinator is None
            else coordinator.snapshot()
        )
        return {
            "restoration_enabled": self._enable_audio_restoration,
            "state": reconnect["state"],
            "retry_attempts": reconnect["rediscovery_attempts"],
            "retry_campaigns": reconnect["rediscovery_campaigns"],
            "retry_in_flight": reconnect["rediscovery_in_flight"],
            "stable_confirmations": reconnect["stable_confirmations"],
            "reason": reconnect["reason"],
            "topology_observation": topology.get("topology_observation"),
            "topology_observation_stale": topology.get("topology_observation_stale"),
            "topology_observed_monotonic_ns": topology.get("topology_observed_monotonic_ns"),
            "active_slot_id": topology.get("active_slot_id"),
            "active_activation_id": topology.get("active_activation_id"),
            "slot_count": topology.get("slot_count"),
            "slot_activations": topology.get("slot_activations"),
            "request_pad_invariant": topology.get("request_pad_invariant"),
            "request_pad_counts_measured": topology.get("request_pad_counts_measured"),
            "request_pad_peer_ownership_proven": topology.get("request_pad_peer_ownership_proven"),
            "request_pad_counts": {
                "video_tee": topology.get("video_tee_request_pads"),
                "audio_tee": topology.get("audio_tee_request_pads"),
                "splitmux_video": topology.get("splitmux_video_request_pads"),
                "splitmux_audio": topology.get("splitmux_audio_request_pads"),
            },
            "tee_pad_routes": topology.get("tee_pad_routes"),
            "audio_ingress": topology.get("audio_ingress"),
            "loss_count": self._audio_loss_count,
            "restoration_count": self._audio_restoration_count,
            "matched_endpoint": (None if self._audio_plan is None else self._audio_plan.endpoint),
            "matched_identity": (
                None
                if self._audio_plan is None
                else {
                    "vendor_id": self._audio_plan.identity.vendor_id,
                    "product_id": self._audio_plan.identity.product_id,
                    "product": self._audio_plan.identity.product,
                    "physical_path": self._audio_plan.identity.physical_path,
                    "serial": self._audio_plan.identity.serial,
                }
            ),
            "last_loss_handoff": self._handoff_snapshot(self._last_loss_handoff),
            "last_restore_handoff": self._handoff_snapshot(self._last_restore_handoff),
            "loss_classification": self._audio_loss_classification,
            "loss_observations": list(self._audio_loss_observations),
            "last_failure": self._audio_restoration_failure,
        }

    @staticmethod
    def _handoff_snapshot(
        handoff: AudioLossHandoff | AudioRestoreHandoff | None,
    ) -> dict[str, object] | None:
        if handoff is None:
            return None
        return cast(dict[str, object], asdict(handoff))

    def bind_audio_loss_probe(
        self,
        probe: Callable[[], AudioDiscoveryOutcome],
    ) -> None:
        """Bind exact-device loss confirmation and gated restoration discovery."""

        if (
            self._started_once
            or self._audio_plan is None
            or self._audio_loss_probe is not None
            or not callable(probe)
        ):
            raise PipelineContractError("audio-loss probe cannot be bound in this state")
        self._audio_loss_probe = probe

    @property
    def effective_caps(self) -> EffectiveCaps | None:
        return self._effective_caps

    @property
    def encoder_identity(self) -> EncoderIdentity | None:
        return self._encoder_identity

    @property
    def audio_capture_plan(self) -> AudioCapturePlan | None:
        return self._audio_plan

    @property
    def effective_audio_caps(self) -> EffectiveAudioCaps | None:
        return self._effective_audio_caps

    def frame_counters(self) -> FrameCounters:
        return self._counters.snapshot()

    def audio_counters(self) -> AudioCounters:
        return self._counters.audio_snapshot()

    def overlay_snapshot(self) -> dict[str, object]:
        """Expose bounded native-renderer evidence without retaining frame data."""

        pipeline = self._pipeline
        driver = self._driver
        observe = getattr(driver, "overlay_snapshot", None)
        if pipeline is None or not callable(observe):
            return {
                "state": "INACTIVE" if pipeline is None else "UNAVAILABLE",
                "caps_accepted": False,
                "enabled": False,
                "updates": 0,
                "update_rejections": 0,
                "frames_seen": 0,
                "frames_rendered": 0,
                "frames_passthrough": 0,
                "bytes_written": 0,
                "contract_mismatches": 0,
                "transform_failures": 0,
                "mappings_cached": 0,
                "mappings_created": 0,
                "mappings_closed": 0,
                "mapping_limit_rejections": 0,
                "sync_starts": 0,
                "sync_ends": 0,
                "sync_failures": 0,
                "render_latency_samples": 0,
                "render_latency_last_ns": None,
                "render_latency_max_ns": 0,
                "render_latency_total_ns": 0,
                "render_latency_bucket_bounds_ns": [],
                "render_latency_bucket_counts": [],
                "last_error": None,
            }
        observed = observe(pipeline)
        if not isinstance(observed, dict):
            raise GStreamerDriverError("native overlay returned an invalid snapshot")
        return observed

    async def next_finalized_fragment(self) -> FinalizedFragment:
        """Wait for one validated closure event; callers should impose their own timeout."""

        return await self._finalized_fragments.get()

    def mark_finalized_fragment_processed(self) -> None:
        self._finalized_fragments.task_done()

    async def wait_for_finalized_fragments_processed(self) -> None:
        await self._finalized_fragments.join()

    async def wait_for_first_fragment_opened(self) -> OpenedFragment:
        """Wait until splitmuxsink reports its first validated output open."""

        await self._first_fragment_opened.wait()
        opened = self._opened_fragment
        if opened is None:
            raise PipelineContractError("fragment-opened signal lost its validated event")
        return opened

    def configure_overlay_text(self, text: str | None) -> None:
        """Bind initial overlay state before any camera buffer can flow."""

        if self._started_once:
            raise PipelineContractError("initial overlay is already bound")
        _validate_overlay_text(text)
        self._configured_overlay_text = text

    async def set_overlay_text(self, text: str | None) -> None:
        """Set one bounded overlay payload without queueing native work."""

        _validate_overlay_text(text)
        pipeline = self._pipeline
        if pipeline is None:
            raise PipelineContractError("overlay update requires an active pipeline")
        if text == self._last_overlay_text:
            return
        driver = self._load_driver()
        await self._driver_call(driver.set_overlay_text, pipeline, text)
        self._last_overlay_text = text

    def _load_driver(self) -> GStreamerDriver:
        if self._driver is None:
            self._driver = PyGObjectGStreamerDriver.load()
        return self._driver

    async def _driver_call(self, callback: Callable[..., object], *arguments: object) -> object:
        """Serialize Python calls into a pipeline while native media threads continue."""

        async with self._driver_lock:
            task = asyncio.create_task(asyncio.to_thread(callback, *arguments))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError as cancellation:
                # A cancelled await cannot stop a native/default-executor call.
                # Keep serialization until the bounded driver operation exits so
                # shutdown cannot overlap the same pipeline.
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                with suppress(BaseException):
                    task.result()
                raise cancellation

    @staticmethod
    def _validate_requested_profile(profile: VideoProfile) -> None:
        if profile != VideoProfile():
            raise ProfileValidationError(
                "GStreamer backend supports only the exact production video profile"
            )

    @staticmethod
    def _effective_profile(caps: EffectiveCaps) -> VideoProfile:
        if caps.frames_per_second_denominator != 1:
            raise ProfileValidationError("effective frame rate is not exactly 30/1")
        if caps.raw_format != "NV12":
            raise ProfileValidationError("effective raw format is not NV12")
        if caps.profile.casefold() != "high" or caps.level != "4.1":
            raise ProfileValidationError("effective H.264 caps are not High Profile Level 4.1")
        try:
            profile = VideoProfile(
                width=caps.width,
                height=caps.height,
                frames_per_second=caps.frames_per_second_numerator,
                codec=caps.codec,
                hardware_encoded=True,
            )
        except ProfileValidationError as error:
            raise ProfileValidationError(f"effective video profile mismatch: {error}") from error
        if profile != VideoProfile():
            raise ProfileValidationError("effective video profile differs from production")
        return profile

    @staticmethod
    def _validate_encoder(identity: EncoderIdentity) -> None:
        classes = {part.casefold() for part in identity.factory_class.split("/")}
        if identity.factory_name != "v4l2h264enc" or "hardware" not in classes:
            raise ProfileValidationError("effective encoder is not the selected hardware encoder")
        if not _device_path_is_dynamic_video_node(identity.device_path):
            raise ProfileValidationError(
                "hardware encoder did not report a dynamically selected video device"
            )

    @staticmethod
    def _validate_audio(caps: EffectiveAudioCaps, plan: AudioCapturePlan) -> None:
        if (
            caps.raw_format,
            caps.sample_rate_hz,
            caps.channels,
            caps.codec,
            caps.mpeg_version,
            caps.stream_format,
            caps.encoder_factory,
            caps.parser_factory,
            caps.bitrate_bps,
        ) != (
            "S16LE",
            plan.sample_rate_hz,
            plan.channels,
            plan.codec,
            4,
            "raw",
            "voaacenc",
            "aacparse",
            plan.bitrate_bps,
        ):
            raise ProfileValidationError(
                "effective audio caps, factories, or bitrate differ from production"
            )

    def _observe_finalized_fragment(self, message: BusMessage) -> FinalizedFragment:
        fragment = message.fragment
        if fragment is None:
            raise ValueError("finalized-fragment bus message omitted its fields")
        fragment = self._bind_fragment_contract(fragment, finalized=True)
        finalized = self._output.finalized_fragment(fragment)
        try:
            self._finalized_fragments.put_nowait(finalized)
        except asyncio.QueueFull as error:
            raise ValueError(
                "finalized-fragment event queue exceeded its configured bound"
            ) from error
        active = self._active_fragment
        if (
            active is not None
            and finalized.path == active.path
            and finalized.sequence == active.sequence
        ):
            self._active_fragment = None
        return finalized

    def _verify_output_device(self, opened_path: Path | None = None) -> None:
        """Fail if the output namespace is no longer on its preflight device."""

        expected_st_dev = self._output.expected_st_dev
        if expected_st_dev is None:
            return
        directory_info = self._path_lstat(self._output.output_directory)
        if not stat.S_ISDIR(directory_info.st_mode) or directory_info.st_dev != expected_st_dev:
            raise ValueError("pending output directory left the verified mount device")
        if opened_path is None:
            return
        opened_info = self._path_lstat(opened_path)
        if not stat.S_ISREG(opened_info.st_mode) or opened_info.st_dev != expected_st_dev:
            raise ValueError("opened fragment left the verified mount device")

    def _observe_fragment_opened(self, message: BusMessage) -> OpenedFragment:
        fragment = message.fragment
        if fragment is None:
            raise ValueError("fragment-opened bus message omitted its fields")
        fragment = self._bind_fragment_contract(fragment, finalized=False)
        opened = self._output.opened_fragment(fragment)
        # splitmuxsink emits this only after filesink opens the path, so this
        # check cannot eliminate the first-open race by itself.  The unit's
        # same-path BindPaths mount pins the verified filesystem, while this
        # per-open check detects namespace/device drift and fails promptly.
        self._verify_output_device(opened.path)
        self._active_fragment = opened
        if self._opened_fragment is None:
            self._opened_fragment = opened
            self._first_fragment_opened.set()
        return opened

    def _bind_fragment_contract(
        self,
        fragment: FragmentMessage,
        *,
        finalized: bool,
    ) -> FragmentMessage:
        """Require source-bound media truth once more than one generation can report."""

        contract = fragment.media_contract
        if contract is not None:
            if finalized and contract.encoded_audio_access_units is None:
                raise ValueError("finalized fragment omitted its generation-bound audio count")
            return fragment
        if self._audio_loss_isolated:
            raise ValueError("post-handoff fragment omitted its immutable generation contract")
        contract = self._initial_media_contract
        if contract is None:
            raise ValueError("fragment arrived before media generation was bound")
        if finalized:
            units = 0
            if contract.audio_caps is not None:
                observed = self._counters.audio_snapshot().encoded_access_units
                units = observed - self._fragment_audio_counter_baseline
                if units < 0:
                    raise ValueError("fragment audio counter regressed")
                self._fragment_audio_counter_baseline = observed
            contract = FragmentMediaContract(
                contract.generation_id,
                contract.audio_caps,
                units,
            )
        return replace(fragment, media_contract=contract)

    async def start(self, requested_profile: VideoProfile) -> VideoProfile:
        """Start the exact graph and prove its effective target profile."""

        if self._started_once:
            raise PipelineContractError("GStreamer backend instances are single-use")
        self._started_once = True
        self._validate_requested_profile(requested_profile)

        pipeline: object | None = None
        try:
            driver = self._load_driver()
            self._verify_output_device()
            if any(self._path_exists(path) for path in self._output.initial_paths):
                raise GStreamerDriverError(
                    "refusing to overwrite the initial provisional clip pair"
                )
            pipeline = await self._driver_call(
                driver.create_pipeline,
                (
                    PIPELINE_DESCRIPTION
                    if self._audio_plan is None
                    else (
                        build_audio_pipeline_description(self._audio_plan)
                        if self._enable_audio_loss_isolation
                        else build_legacy_audio_pipeline_description(self._audio_plan)
                    )
                ),
                self._output.location_pattern,
                self._output.start_index,
                self._audio_plan,
            )
            self._pipeline = pipeline
            configured_overlay = self._configured_overlay_text
            if configured_overlay is not _OVERLAY_UNSET:
                await self._driver_call(
                    driver.set_overlay_text,
                    pipeline,
                    cast(str | None, configured_overlay),
                )
                self._last_overlay_text = configured_overlay
            await self._driver_call(
                driver.set_playing,
                pipeline,
                self._limits.start_timeout_s,
            )
            caps = cast(
                EffectiveCaps,
                await self._driver_call(driver.effective_caps, pipeline),
            )
            identity = cast(
                EncoderIdentity,
                await self._driver_call(driver.encoder_identity, pipeline),
            )
            effective = self._effective_profile(caps)
            self._validate_encoder(identity)
            audio_caps: EffectiveAudioCaps | None = None
            if self._audio_plan is not None:
                try:
                    observe_audio = getattr(driver, "effective_audio_caps", None)
                    if not callable(observe_audio):
                        raise GStreamerDriverError(
                            "selected driver cannot validate effective audio"
                        )
                    audio_caps = cast(
                        EffectiveAudioCaps,
                        await self._driver_call(observe_audio, pipeline),
                    )
                    if not isinstance(audio_caps, EffectiveAudioCaps):
                        raise GStreamerDriverError(
                            "selected driver returned invalid effective audio"
                        )
                    self._validate_audio(audio_caps, self._audio_plan)
                except (GStreamerDriverError, ProfileValidationError) as audio_error:
                    raise AudioStartupError(
                        f"audio startup validation failed: {_bounded_detail(audio_error)}"
                    ) from audio_error
            self._effective_caps = caps
            self._effective_audio_caps = audio_caps
            self._encoder_identity = identity
            self._counters.configure_pts_cadence(
                caps.frames_per_second_numerator,
                caps.frames_per_second_denominator,
            )
            install_metrics = getattr(driver, "install_metrics", None)
            if callable(install_metrics):
                await self._driver_call(install_metrics, pipeline, self._counters)
            if self._audio_plan is not None:
                try:
                    install_audio_metrics = getattr(driver, "install_audio_metrics", None)
                    if not callable(install_audio_metrics):
                        raise GStreamerDriverError("selected driver cannot observe encoded audio")
                    await self._driver_call(
                        install_audio_metrics,
                        pipeline,
                        self._counters,
                    )
                except GStreamerDriverError as audio_error:
                    raise AudioStartupError(
                        f"audio metrics startup failed: {_bounded_detail(audio_error)}"
                    ) from audio_error
            self._initial_media_contract = FragmentMediaContract(1, audio_caps)
            if self._audio_plan is not None and self._enable_audio_restoration:
                self._audio_hotplug = AudioHotplugCoordinator(
                    self._audio_plan,
                    policy=AudioReconnectPolicy(
                        interval_ns=int(self._limits.audio_restore_poll_interval_s * 1_000_000_000),
                        max_attempts=self._limits.audio_restore_attempts_per_campaign,
                        campaign_cooldown_ns=int(
                            self._limits.audio_restore_campaign_cooldown_s * 1_000_000_000
                        ),
                    ),
                )
            return effective
        except (Exception, asyncio.CancelledError) as error:
            cleanup_error: BaseException | None = None
            if pipeline is not None:
                try:
                    await asyncio.shield(
                        self._driver_call(
                            self._load_driver().set_null,
                            pipeline,
                            self._limits.null_timeout_s,
                        )
                    )
                except BaseException as caught:
                    cleanup_error = caught
                else:
                    if self._pipeline is pipeline:
                        self._pipeline = None
            if cleanup_error is not None:
                raise GStreamerShutdownError(
                    "GStreamer startup rollback could not release the pipeline: "
                    f"{_bounded_detail(cleanup_error)}"
                ) from error
            if isinstance(error, asyncio.CancelledError):
                raise
            if isinstance(error, AudioStartupError | ProfileValidationError):
                raise
            raise RecoverablePipelineError(
                f"GStreamer startup failed: {_bounded_detail(error)}"
            ) from error

    async def _confirm_and_isolate_audio_loss(
        self,
        pipeline: object,
        driver: GStreamerDriver,
        message: BusMessage,
    ) -> None:
        """Corroborate one exact-source error and execute one bounded handoff."""

        if self._audio_loss_isolated:
            self._audio_loss_messages += 1
            if self._audio_loss_messages > 4:
                raise RecoverablePipelineError(
                    "GStreamer audio-loss error burst exceeded its bound"
                )
            return
        if self._audio_plan is None or message.source_name != "audio_source":
            raise RecoverablePipelineError(
                "GStreamer audio error lacked an exact active audio source"
            )
        if not self._enable_audio_loss_isolation:
            raise RecoverablePipelineError(
                "immutable audio-loss isolation is not production-enabled"
            )
        probe = self._audio_loss_probe
        arm = getattr(driver, "arm_audio_loss", None)
        isolate = getattr(driver, "isolate_audio_loss", None)
        if probe is None or not callable(arm) or not callable(isolate):
            raise PipelineContractError("immutable audio-loss isolation capability is unavailable")
        try:
            arm_proof = cast(
                AudioLossArmProof,
                await self._driver_call(
                    arm,
                    pipeline,
                    message.source_name,
                ),
            )
        except Exception as error:
            raise RecoverablePipelineError(
                f"audio-loss containment arm failed: {_bounded_detail(error)}"
            ) from error
        if not isinstance(arm_proof, AudioLossArmProof):
            raise RecoverablePipelineError("audio-loss containment arm returned invalid proof")
        try:
            result = cast(
                AudioLossHandoff,
                await self._driver_call(
                    isolate,
                    pipeline,
                    self._limits.audio_handoff_timeout_s,
                ),
            )
        except Exception as error:
            raise RecoverablePipelineError(
                f"immutable audio-loss handoff failed: {_bounded_detail(error)}"
            ) from error
        if not isinstance(result, AudioLossHandoff):
            raise RecoverablePipelineError("immutable audio-loss handoff returned invalid proof")
        self._audio_loss_isolated = True
        self._audio_loss_messages = 1
        self._audio_loss_count += 1
        self._last_loss_handoff = result
        self._effective_audio_caps = None
        self._audio_loss_classification = "audio_fault_isolated_pending_classification"
        coordinator = self._audio_hotplug
        if coordinator is not None:
            action = coordinator.observe_loss(message.source_name)
            if action is None or action.kind is not AudioBranchActionKind.QUIESCE:
                raise PipelineContractError("audio reconnect coordinator refused the proven loss")
            coordinator.observe_quiesced(time.monotonic_ns())
        observations: list[dict[str, object]] = []
        exact_match_count = 0
        stable_not_found = True
        for attempt in range(2):
            observed_at = time.monotonic_ns()
            try:
                outcome = await asyncio.to_thread(probe)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                stable_not_found = False
                observations.append(
                    {
                        "status": "probe_error",
                        "monotonic_ns": observed_at,
                        "detail": _bounded_detail(error),
                    }
                )
            else:
                if not isinstance(outcome, AudioDiscoveryOutcome):
                    stable_not_found = False
                    observations.append(
                        {
                            "status": "invalid_result",
                            "monotonic_ns": observed_at,
                        }
                    )
                else:
                    exact_match = (
                        outcome.status is AudioDiscoveryStatus.MATCHED
                        and outcome.device is not None
                    )
                    exact_match_count += int(exact_match)
                    not_found = (
                        outcome.status is AudioDiscoveryStatus.NOT_FOUND
                        and outcome.device is None
                    )
                    stable_not_found = stable_not_found and not_found
                    observations.append(
                        {
                            "status": outcome.status.value,
                            "monotonic_ns": observed_at,
                            "exact_match": exact_match,
                        }
                    )
            if attempt == 0:
                await self._sleep(self._limits.audio_loss_poll_interval_s)
        self._audio_loss_observations = tuple(observations)
        if stable_not_found and len(observations) == 2:
            self._audio_loss_classification = "microphone_loss_isolated"
        elif exact_match_count == 2:
            self._audio_loss_classification = "microphone_stream_fault_isolated"
        else:
            self._audio_loss_classification = "audio_discovery_degraded"

    @staticmethod
    def _reconnect_observation(
        outcome: AudioDiscoveryOutcome,
    ) -> AlsaCaptureDevice | AudioReconnectObservation:
        if outcome.status is AudioDiscoveryStatus.MATCHED and outcome.device is not None:
            return outcome.device
        mapping = {
            AudioDiscoveryStatus.NOT_FOUND: AudioReconnectObservation.NOT_FOUND,
            AudioDiscoveryStatus.AMBIGUOUS: AudioReconnectObservation.AMBIGUOUS,
            AudioDiscoveryStatus.REFUSED: AudioReconnectObservation.REFUSED,
        }
        try:
            return mapping[outcome.status]
        except KeyError as error:
            raise RecoverablePipelineError(
                "audio rediscovery returned inconsistent matched state"
            ) from error

    async def _poll_audio_restoration(
        self,
        pipeline: object,
        driver: GStreamerDriver,
    ) -> None:
        """Advance at most one bounded reconnect action per ordinary bus poll."""

        coordinator = self._audio_hotplug
        if coordinator is None or not self._audio_loss_isolated:
            return
        now_ns = time.monotonic_ns()
        action = coordinator.poll(now_ns)
        if action is not None:
            probe = self._audio_loss_probe
            if action.kind is not AudioBranchActionKind.REDISCOVER or probe is None:
                raise PipelineContractError("audio rediscovery action is invalid")
            try:
                outcome = await asyncio.to_thread(probe)
            except asyncio.CancelledError:
                raise
            except Exception:
                coordinator.observe_rediscovery(
                    action.generation,
                    AudioReconnectObservation.REFUSED,
                    now_ns=time.monotonic_ns(),
                )
                return
            if not isinstance(outcome, AudioDiscoveryOutcome):
                coordinator.observe_rediscovery(
                    action.generation,
                    AudioReconnectObservation.REFUSED,
                    now_ns=time.monotonic_ns(),
                )
                return
            coordinator.observe_rediscovery(
                action.generation,
                self._reconnect_observation(outcome),
                now_ns=time.monotonic_ns(),
            )
        restore_action = coordinator.observe_fragment_boundary()
        if restore_action is None:
            return
        restore = getattr(driver, "restore_audio", None)
        if (
            restore_action.kind is not AudioBranchActionKind.RESTORE
            or restore_action.plan is None
            or not callable(restore)
        ):
            coordinator.observe_restore_failed(
                restore_action.generation,
                now_ns=time.monotonic_ns(),
            )
            return
        route_completed = False
        try:
            proof = cast(
                AudioRestoreHandoff,
                await self._driver_call(
                    restore,
                    pipeline,
                    restore_action.plan,
                    self._limits.audio_restore_handoff_timeout_s,
                ),
            )
            route_completed = True
            if not isinstance(proof, AudioRestoreHandoff):
                raise GStreamerDriverError("audio restoration returned invalid proof")
            observe_audio = getattr(driver, "effective_audio_caps", None)
            if not callable(observe_audio):
                raise GStreamerDriverError("restored audio driver cannot validate effective caps")
            restored_caps = cast(
                EffectiveAudioCaps,
                await self._driver_call(observe_audio, pipeline),
            )
            if not isinstance(restored_caps, EffectiveAudioCaps):
                raise GStreamerDriverError("restored audio driver returned invalid effective caps")
            self._validate_audio(restored_caps, restore_action.plan)
        except asyncio.CancelledError:
            raise
        except AudioRestorationCriticalError as error:
            self._audio_restoration_failure = {
                "critical": True,
                "phase": error.phase,
                "detail": _bounded_detail(error),
                "monotonic_ns": time.monotonic_ns(),
            }
            raise RecoverablePipelineError(
                "audio restoration crossed its route boundary without a safe "
                f"video-only rollback: {_bounded_detail(error)}"
            ) from error
        except Exception as error:
            if route_completed:
                self._audio_restoration_failure = {
                    "critical": True,
                    "phase": "post_route_caps_validation",
                    "detail": _bounded_detail(error),
                    "monotonic_ns": time.monotonic_ns(),
                }
                raise RecoverablePipelineError(
                    f"audio restoration post-route validation failed: {_bounded_detail(error)}"
                ) from error
            coordinator.observe_restore_failed(
                restore_action.generation,
                now_ns=time.monotonic_ns(),
            )
            return
        coordinator.observe_restored(restore_action.generation)
        self._audio_plan = restore_action.plan
        self._effective_audio_caps = restored_caps
        self._audio_loss_isolated = False
        self._audio_loss_messages = 0
        self._audio_restoration_count += 1
        self._last_restore_handoff = proof

    async def run(self, stop_requested: asyncio.Event) -> None:
        """Poll the critical bus until stopped or a recoverable failure occurs."""

        pipeline = self._pipeline
        if pipeline is None:
            raise PipelineContractError("GStreamer backend has not started")
        driver = self._load_driver()

        while not stop_requested.is_set():
            try:
                message = cast(
                    BusMessage,
                    await self._driver_call(
                        driver.poll_bus,
                        pipeline,
                        self._limits.bus_poll_s,
                    ),
                )
            except Exception as error:
                raise RecoverablePipelineError(
                    f"GStreamer bus poll failed: {_bounded_detail(error)}"
                ) from error
            if message.kind is BusMessageKind.NONE:
                await self._poll_audio_restoration(pipeline, driver)
                continue
            if message.kind is BusMessageKind.ERROR:
                raise RecoverablePipelineError(
                    f"GStreamer pipeline error: {_bounded_detail(message.detail)}"
                )
            if message.kind is BusMessageKind.AUDIO_ERROR:
                await self._confirm_and_isolate_audio_loss(
                    pipeline,
                    driver,
                    message,
                )
                continue
            if message.kind is BusMessageKind.EOS:
                if stop_requested.is_set():
                    return
                raise RecoverablePipelineError("GStreamer pipeline reached unexpected EOS")
            if message.kind is BusMessageKind.FRAGMENT_FINALIZED:
                try:
                    self._observe_finalized_fragment(message)
                except ValueError as error:
                    raise RecoverablePipelineError(
                        f"GStreamer finalized-fragment contract failed: {_bounded_detail(error)}"
                    ) from error
                continue
            if message.kind is BusMessageKind.FRAGMENT_OPENED:
                try:
                    self._observe_fragment_opened(message)
                except ValueError as error:
                    raise RecoverablePipelineError(
                        f"GStreamer fragment-opened contract failed: {_bounded_detail(error)}"
                    ) from error
                continue
            raise RecoverablePipelineError("GStreamer driver returned an unknown bus message")

    async def _shutdown_pipeline(self, pipeline: object) -> None:
        """Finalize the active fragment and always attempt the final NULL state.

        The exact Pi's splitmuxsink posts a validated fragment-closed message
        for the active output after accepting EOS, but does not post a
        pipeline-level EOS. Either signal is sufficient only after the active
        output identity has been proven.  A parent EOS observed while an active
        fragment is known does not substitute for that fragment's exact
        closure; cached retired-branch EOS may combine with the continuity
        guard's shutdown EOS before splitmux posts its closure.
        """

        driver = self._load_driver()
        shutdown_error: GStreamerShutdownError | None = None
        shutdown_target = self._active_fragment
        parent_eos_seen = False

        try:
            accepted = bool(await self._driver_call(driver.send_eos, pipeline))
            if not accepted:
                shutdown_error = GStreamerShutdownError(
                    "GStreamer pipeline rejected the EOS request"
                )
            else:
                deadline = self._monotonic() + self._limits.eos_timeout_s
                while shutdown_error is None:
                    remaining = deadline - self._monotonic()
                    if remaining <= 0:
                        shutdown_error = GStreamerShutdownError(
                            "GStreamer pipeline reached EOS before the exact "
                            "active fragment closure deadline"
                            if parent_eos_seen and shutdown_target is not None
                            else "GStreamer pipeline exceeded the EOS shutdown deadline"
                        )
                        break
                    message = cast(
                        BusMessage,
                        await self._driver_call(
                            driver.poll_bus,
                            pipeline,
                            min(self._limits.bus_poll_s, remaining),
                        ),
                    )
                    if message.kind is BusMessageKind.EOS:
                        parent_eos_seen = True
                        if shutdown_target is None:
                            break
                        continue
                    if message.kind is BusMessageKind.ERROR or (
                        message.kind is BusMessageKind.AUDIO_ERROR and not self._audio_loss_isolated
                    ):
                        shutdown_error = GStreamerShutdownError(
                            f"GStreamer shutdown error: {_bounded_detail(message.detail)}"
                        )
                    elif message.kind is BusMessageKind.AUDIO_ERROR:
                        self._audio_loss_messages += 1
                        if self._audio_loss_messages > 4:
                            shutdown_error = GStreamerShutdownError(
                                "GStreamer shutdown audio-loss burst exceeded its bound"
                            )
                    elif message.kind is BusMessageKind.FRAGMENT_FINALIZED:
                        try:
                            finalized = self._observe_finalized_fragment(message)
                        except ValueError as error:
                            shutdown_error = GStreamerShutdownError(
                                "GStreamer finalized-fragment shutdown contract failed: "
                                f"{_bounded_detail(error)}"
                            )
                        else:
                            if (
                                shutdown_target is not None
                                and finalized.path == shutdown_target.path
                                and finalized.sequence == shutdown_target.sequence
                            ):
                                break
                    elif message.kind is BusMessageKind.FRAGMENT_OPENED:
                        try:
                            opened = self._observe_fragment_opened(message)
                        except ValueError as error:
                            shutdown_error = GStreamerShutdownError(
                                "GStreamer fragment-opened shutdown contract failed: "
                                f"{_bounded_detail(error)}"
                            )
                        else:
                            if (
                                shutdown_target is None
                                or opened.sequence >= shutdown_target.sequence
                            ):
                                shutdown_target = opened
                    elif message.kind is not BusMessageKind.NONE:
                        shutdown_error = GStreamerShutdownError(
                            "GStreamer driver returned an unknown shutdown message"
                        )
        except Exception as error:
            if isinstance(error, GStreamerShutdownError):
                shutdown_error = error
            else:
                shutdown_error = GStreamerShutdownError(
                    f"GStreamer EOS shutdown failed: {_bounded_detail(error)}"
                )

        self._null_cleanup_pending = shutdown_error is None
        try:
            await self._driver_call(
                driver.set_null,
                pipeline,
                self._limits.null_timeout_s,
            )
        except Exception as error:
            null_error = GStreamerShutdownError(
                f"GStreamer NULL transition failed: {_bounded_detail(error)}"
            )
            if shutdown_error is not None:
                raise null_error from shutdown_error
            raise null_error from error
        if self._pipeline is pipeline:
            self._pipeline = None
        self._null_cleanup_pending = False

        if shutdown_error is not None:
            raise shutdown_error

    async def stop(self) -> None:
        """Finalize once and keep forcing NULL even if a caller is cancelled."""

        task = self._shutdown_task
        if task is None:
            pipeline = self._pipeline
            if pipeline is None:
                return
            task = asyncio.create_task(
                (
                    self._retry_null_cleanup(pipeline)
                    if self._null_cleanup_pending
                    else self._shutdown_pipeline(pipeline)
                ),
                name="gstreamer-pipeline-shutdown",
            )
            self._shutdown_task = task
        try:
            await asyncio.shield(task)
        finally:
            if task.done() and self._shutdown_task is task:
                self._shutdown_task = None

    async def _retry_null_cleanup(self, pipeline: object) -> None:
        """Retry only the idempotent NULL cleanup after media drain succeeded."""

        driver = self._load_driver()
        try:
            await self._driver_call(
                driver.set_null,
                pipeline,
                self._limits.null_timeout_s,
            )
        except Exception as error:
            raise GStreamerShutdownError(
                f"GStreamer NULL cleanup retry failed: {_bounded_detail(error)}"
            ) from error
        if self._pipeline is pipeline:
            self._pipeline = None
        self._null_cleanup_pending = False


@dataclass(slots=True)
class _RecordingGeneration:
    # ``generation_id`` is the immutable fixed slot ID (1=A/V, 2/3=video).
    # ``activation_id`` is assigned anew every time that slot carries media.
    generation_id: int
    has_audio: bool
    bin: Any
    output: Any
    video_valve: Any
    video_queue: Any
    video_ghost: Any
    video_tee_pad: Any
    output_video_pad: Any
    video_gate_queue: Any | None = None
    audio_valve: Any | None = None
    audio_queue: Any | None = None
    audio_ghost: Any | None = None
    audio_tee_pad: Any | None = None
    output_audio_pad: Any | None = None
    linked: bool = False
    retired: bool = False
    opened: dict[str, int] = field(default_factory=dict)
    closed: set[str] = field(default_factory=set)
    audio_units: int = 0
    audio_running_times: deque[int] = field(default_factory=deque)
    last_audio_end_running_time_ns: int | None = None
    streaming_error: str | None = None
    last_closed_location: str | None = None
    first_video_seen: Event = field(default_factory=Event)
    first_video_is_idr: bool | None = None
    first_video_had_sticky_contract: bool | None = None
    video_units: int = 0
    audio_eos: _AudioEosArbiter = field(default_factory=_AudioEosArbiter)
    video_tee_pad_released: bool = False
    audio_tee_pad_released: bool = False
    output_video_pad_released: bool = False
    output_audio_pad_released: bool = False
    removed_from_parent: bool = False
    activation_id: int | None = None
    reusable: bool = True
    video_retirement_eos_sent: bool = False
    generation_retirement_eos_seqnum: int | None = None


@dataclass(slots=True)
class _RetirementDispatch:
    label: str
    thread: Thread
    done: Event
    accepted: bool | None = None
    error: BaseException | None = None
    generation: _RecordingGeneration | None = None
    activation_id: int | None = None
    pad: Any | None = None
    branch: str | None = None
    eos_seqnum: int | None = None


@dataclass(slots=True)
class _BoundedEventDispatch:
    label: str
    thread: Thread | None
    done: Event
    pad: Any
    activation_id: int
    accepted: bool | None = None
    error: BaseException | None = None
    caller_thread_ident: int | None = None


@dataclass(slots=True)
class _AudioIngressQuarantine:
    ingress: Any
    source: Any
    activation_id: int
    ingress_generation: int = 0
    error_count: int = 0
    eos_count: int = 0


@dataclass(slots=True)
class _RestorationParentFailureProvenance:
    context: _GenerationPipeline
    pipeline: Any
    camera: Any
    encoder: Any
    retiring: _RecordingGeneration
    retiring_activation_id: int
    retiring_location: str
    original_ingress: Any
    original_elements: Mapping[str, Any]
    original_quarantine: _AudioIngressQuarantine
    original_source: Any
    replacement_count: int
    successor: _RecordingGeneration
    expected_successor_activation_id: int
    failure_state: Any
    playing_state: Any
    void_pending_state: Any
    consumed: bool = False


@dataclass(slots=True)
class _GenerationPipeline:
    pipeline: Any
    gst: Any
    output_directory: str
    boot_id: str
    next_sequence: int
    video_tee: Any
    audio_tee: Any
    camera: Any
    encoder: Any
    generations: dict[int, _RecordingGeneration]
    sequence_lock: Lock
    location_generation: dict[str, tuple[int, int]]
    pending_messages: deque[BusMessage]
    video_continuity_queue: Any | None = None
    video_continuity_sink: Any | None = None
    video_continuity_tee_pad: Any | None = None
    video_continuity_tee_pad_released: bool = False
    active_generation_id: int = 1
    next_activation_id: int = 2
    next_video_slot_id: int = 2
    isolated: bool = False
    routing_phase: str = "AV_ACTIVE"
    loss_verified: bool = False
    cleanup_complete: bool = False
    retirement_dispatches: list[_RetirementDispatch] = field(default_factory=list)
    audio_retirement_dispatches: list[_BoundedEventDispatch] = field(default_factory=list)
    force_key_dispatches: list[_BoundedEventDispatch] = field(default_factory=list)
    handoff_lock: Lock = field(default_factory=Lock)
    initial_camera: Any = None
    initial_encoder: Any = None
    audio_ingress_bin: Any | None = None
    audio_ingress_elements: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    audio_ingress_replacement_count: int = 0
    audio_ingress_quarantine: _AudioIngressQuarantine | None = None
    last_stable_topology: dict[str, object] | None = None
    published_topology: dict[str, object] | None = None
    next_force_key_count: int = 1

    def __post_init__(self) -> None:
        self.initial_camera = self.camera
        self.initial_encoder = self.encoder


@dataclass(frozen=True, slots=True)
class _HeldForcedIdr:
    """Validated forced-IDR observation awaiting the final drained AAC edge."""

    request_count: int
    request_seqnum: int
    downstream_seqnum: int
    request_monotonic_ns: int
    downstream_event_monotonic_ns: int
    idr_arrival_monotonic_ns: int
    downstream_running_time_ns: int
    forced_idr_running_time_ns: int
    event_to_idr_media_ns: int


@dataclass(slots=True)
class _ForcedIdrGate:
    video_pad: Any
    video_probe_id: Any
    event_pad: Any
    event_probe_id: Any
    release: Event
    reached: Event
    completed: Event
    held: _HeldForcedIdr | None
    dispatch: _BoundedEventDispatch
    observed: dict[str, int | bool | str]
    failed: Event
    request_count: int
    request_seqnum: int
    request_monotonic_ns: int
    proof: ForcedIdrProof | None = None


class PyGObjectGStreamerDriver:
    """Late-bound adapter for the target's PyGObject GStreamer API."""

    def __init__(
        self,
        gst: object,
        gstvideo: object | None = None,
        gstallocators: object | None = None,
    ) -> None:
        self._gst = gst
        self._gstvideo = gstvideo
        self._gstallocators = gstallocators
        self._metrics: dict[int, tuple[PipelineCounters, int]] = {}
        self._generation_pipelines: dict[int, _GenerationPipeline] = {}
        self._overlay_renderers: dict[int, GstDmabufOverlayRenderer] = {}

    @classmethod
    def load(cls) -> PyGObjectGStreamerDriver:
        try:
            gi = importlib.import_module("gi")
            require_version = cast(
                Callable[[str, str], None],
                _dynamic_attribute(gi, "require_version"),
            )
            require_version("Gst", "1.0")
            require_version("GstAllocators", "1.0")
            require_version("GstVideo", "1.0")
            gst = importlib.import_module("gi.repository.Gst")
            gstallocators = importlib.import_module("gi.repository.GstAllocators")
            gstvideo = importlib.import_module("gi.repository.GstVideo")
            init = cast(Callable[[object | None], None], _dynamic_attribute(gst, "init"))
            init(None)
            validate_native_overlay_dependencies(gst, gstallocators, gstvideo)
        except NativeOverlayContractError as error:
            raise GStreamerDriverError(
                f"native NV12 overlay is unavailable: {_bounded_detail(error)}"
            ) from error
        except (ImportError, AttributeError, TypeError, ValueError) as error:
            raise GStreamerDriverError(
                f"PyGObject GStreamer 1.0 is unavailable: {_bounded_detail(error)}"
            ) from error
        return cls(gst, gstvideo, gstallocators)

    def _gst_member(self, name: str) -> object:
        try:
            return getattr(self._gst, name)
        except AttributeError as error:
            raise GStreamerDriverError(f"GStreamer binding lacks {name}") from error

    @staticmethod
    def _classify_restoration_failure(
        phase: str,
        error: BaseException,
    ) -> BaseException:
        """Make every post-route failure terminal to the current pipeline."""

        if phase in _POST_ROUTE_RESTORATION_PHASES:
            return AudioRestorationCriticalError(
                f"audio restoration failed during post-route phase {phase}: "
                f"{_bounded_detail(error)}",
                phase=phase,
            )
        return error

    @staticmethod
    def _trace_handoff(phase: str, **measurements: int) -> None:
        if os.environ.get("DASHCAM_HANDOFF_TRACE") == "1":
            fields = {
                "monotonic_ns": time.monotonic_ns(),
                **measurements,
            }
            suffix = "".join(f" {name}={value}" for name, value in sorted(fields.items()))
            with suppress(OSError):
                os.write(
                    2,
                    f"dashcam-handoff-phase={phase}{suffix}\n".encode("ascii"),
                )

    @staticmethod
    def _trace_handoff_failure(phase: str, error: BaseException) -> None:
        """Preserve a bounded exact failure even when later cleanup also fails."""

        PyGObjectGStreamerDriver._trace_handoff_text(
            phase,
            _bounded_detail(error),
        )

    @staticmethod
    def _trace_handoff_text(
        phase: str,
        detail: str,
        **measurements: int,
    ) -> None:
        """Emit bounded UTF-8 text as hex so one trace line stays parseable."""

        if os.environ.get("DASHCAM_HANDOFF_TRACE") == "1":
            detail_hex = detail[:512].encode("utf-8").hex()
            suffix = "".join(f" {name}={value}" for name, value in sorted(measurements.items()))
            with suppress(OSError):
                os.write(
                    2,
                    (
                        f"dashcam-handoff-phase={phase} "
                        f"monotonic_ns={time.monotonic_ns()} "
                        f"detail_utf8_hex={detail_hex}{suffix}\n"
                    ).encode("ascii"),
                )

    def _generation_description(self, generation_id: int, has_audio: bool) -> str:
        """Build one reusable slot with non-detaching splitmux finalization."""

        prefix = f"g{generation_id:02d}"
        gate = (
            ""
            if has_audio
            else (
                f"queue name={prefix}_video_gate_queue max-size-buffers=2 "
                "max-size-bytes=4000000 max-size-time=100000000 "
                "leaky=downstream ! "
            )
        )
        video = (
            gate
            + f"valve name={prefix}_video_valve drop=true "
            "drop-mode=forward-sticky-events ! "
            f"queue name={prefix}_video_queue max-size-buffers=60 "
            "max-size-bytes=4000000 max-size-time=2000000000 leaky=no ! "
            f"{prefix}_output.video "
        )
        output = (
            f"splitmuxsink name={prefix}_output max-size-time=60000000000 "
            "max-size-bytes=0 send-keyframe-requests=true async-finalize=false "
            "reset-muxer=true muxer=mp4mux sink=filesink"
        )
        if not has_audio:
            return video + output
        return (
            video + output + f" valve name={prefix}_audio_valve drop=true "
            "drop-mode=forward-sticky-events ! "
            f"queue name={prefix}_audio_queue max-size-buffers=96 "
            "max-size-bytes=2097152 max-size-time=2000000000 leaky=no ! "
            f"{prefix}_output.audio_0"
        )

    def _make_generation(
        self,
        context: _GenerationPipeline,
        generation_id: int,
        has_audio: bool,
    ) -> _RecordingGeneration:
        parse_bin = cast(
            Callable[[str, bool], Any],
            _dynamic_attribute(self._gst, "parse_bin_from_description"),
        )
        generation_bin = parse_bin(
            self._generation_description(generation_id, has_audio),
            False,
        )
        if generation_bin is None:
            raise GStreamerDriverError("immutable recording generation construction failed")
        prefix = f"g{generation_id:02d}"
        output = self._method(generation_bin, "get_by_name")(f"{prefix}_output")
        video_valve = self._method(generation_bin, "get_by_name")(f"{prefix}_video_valve")
        video_queue = self._method(generation_bin, "get_by_name")(f"{prefix}_video_queue")
        video_gate_queue = (
            None
            if has_audio
            else self._method(generation_bin, "get_by_name")(
                f"{prefix}_video_gate_queue"
            )
        )
        audio_valve = (
            self._method(generation_bin, "get_by_name")(f"{prefix}_audio_valve")
            if has_audio
            else None
        )
        audio_queue = (
            self._method(generation_bin, "get_by_name")(f"{prefix}_audio_queue")
            if has_audio
            else None
        )
        if (
            output is None
            or video_valve is None
            or video_queue is None
            or (not has_audio and video_gate_queue is None)
        ):
            raise GStreamerDriverError("immutable recording generation is incomplete")
        if has_audio and (audio_valve is None or audio_queue is None):
            raise GStreamerDriverError("immutable A/V generation is incomplete")
        self._configure_sync_generation_output(output)
        output_video_pad = self._method(output, "get_static_pad")("video")
        output_audio_pad = self._method(output, "get_static_pad")("audio_0") if has_audio else None
        if output_video_pad is None or (has_audio and output_audio_pad is None):
            raise GStreamerDriverError("generation splitmux request pads were not preconstructed")
        video_sink = self._method(
            video_valve if video_gate_queue is None else video_gate_queue,
            "get_static_pad",
        )("sink")
        ghost_type = self._gst_member("GhostPad")
        video_ghost = self._method(ghost_type, "new")("video_sink", video_sink)
        if video_ghost is None or not self._method(generation_bin, "add_pad")(video_ghost):
            raise GStreamerDriverError("generation video ghost pad creation failed")
        audio_ghost = None
        if has_audio:
            audio_sink = self._method(audio_valve, "get_static_pad")("sink")
            audio_ghost = self._method(ghost_type, "new")("audio_sink", audio_sink)
            if audio_ghost is None or not self._method(generation_bin, "add_pad")(audio_ghost):
                raise GStreamerDriverError("generation audio ghost pad creation failed")
        self._method(generation_bin, "set_name")(f"{prefix}_generation")
        if not self._method(generation_bin, "set_locked_state")(True):
            raise GStreamerDriverError("standby generation could not lock NULL")
        add_result = self._method(context.pipeline, "add")(generation_bin)
        if (
            add_result is False
            or self._method(generation_bin, "get_parent")() is not context.pipeline
        ):
            raise GStreamerDriverError("generation could not join parent pipeline")
        video_tee_pad = self._method(context.video_tee, "request_pad_simple")("src_%u")
        audio_tee_pad = (
            self._method(context.audio_tee, "request_pad_simple")("src_%u") if has_audio else None
        )
        if video_tee_pad is None or (has_audio and audio_tee_pad is None):
            raise GStreamerDriverError("generation tee request-pad allocation failed")
        generation = _RecordingGeneration(
            generation_id=generation_id,
            has_audio=has_audio,
            bin=generation_bin,
            output=output,
            video_valve=video_valve,
            video_queue=video_queue,
            video_ghost=video_ghost,
            video_tee_pad=video_tee_pad,
            output_video_pad=output_video_pad,
            video_gate_queue=video_gate_queue,
            audio_valve=audio_valve,
            audio_queue=audio_queue,
            audio_ghost=audio_ghost,
            audio_tee_pad=audio_tee_pad,
            output_audio_pad=output_audio_pad,
        )

        def format_location(_output: Any, _fragment_id: int) -> str:
            with context.sequence_lock:
                sequence = context.next_sequence
                activation_id = generation.activation_id
                if activation_id is None:
                    raise GStreamerDriverError(
                        "inactive recording slot requested a fragment location"
                    )
                if sequence > 999_999:
                    raise GStreamerDriverError("fragment sequence space is exhausted")
                if len(context.location_generation) >= 4:
                    raise GStreamerDriverError("active generation location map exceeded its bound")
                context.next_sequence += 1
            location = (
                f"{context.output_directory}/boot-{context.boot_id}-{sequence:06d}.partial.mp4"
            )
            context.location_generation[location] = (generation_id, activation_id)
            return location

        self._method(output, "connect")("format-location", format_location)
        video_src = self._method(video_queue, "get_static_pad")("src")
        probe_type = self._enum_member(self._gst_member("PadProbeType"), "BUFFER")
        probe_return = self._enum_member(self._gst_member("PadProbeReturn"), "OK")
        delta_flag = self._enum_member(self._gst_member("BufferFlags"), "DELTA_UNIT")

        def observe_video(_pad: Any, info: Any) -> Any:
            buffer = info.get_buffer()
            if buffer is not None:
                generation.video_units += 1
                if not generation.first_video_seen.is_set():
                    generation.first_video_is_idr = not bool(buffer.has_flags(delta_flag))
                    try:
                        event_type = self._gst_member("EventType")
                        generation.first_video_had_sticky_contract = all(
                            self._method(_pad, "get_sticky_event")(
                                self._enum_member(event_type, event_name),
                                0,
                            )
                            is not None
                            for event_name in ("STREAM_START", "CAPS", "SEGMENT")
                        )
                    except GStreamerDriverError:
                        generation.first_video_had_sticky_contract = False
                    generation.first_video_seen.set()
            return probe_return

        if not self._method(video_src, "add_probe")(probe_type, observe_video):
            raise GStreamerDriverError("generation video observation probe failed")
        if has_audio:
            audio_src = self._method(audio_queue, "get_static_pad")("src")

            def observe_audio(_pad: Any, info: Any) -> Any:
                buffer = info.get_buffer()
                if buffer is not None:
                    generation.audio_units += 1
                    pts = int(buffer.pts)
                    duration = int(buffer.duration)
                    clock_none = int(cast(SupportsInt, self._gst_member("CLOCK_TIME_NONE")))
                    if pts < 0 or pts == clock_none:
                        generation.streaming_error = "audio buffer PTS is invalid"
                    elif duration <= 0 or duration == clock_none:
                        generation.streaming_error = "audio buffer duration is invalid"
                    else:
                        segment_event = self._method(_pad, "get_sticky_event")(
                            self._enum_member(self._gst_member("EventType"), "SEGMENT"),
                            0,
                        )
                        segment = (
                            None
                            if segment_event is None
                            else self._method(segment_event, "parse_segment")()
                        )
                        running_time = (
                            -1
                            if segment is None
                            else int(
                                cast(
                                    SupportsInt,
                                    self._method(segment, "to_running_time")(
                                        self._enum_member(self._gst_member("Format"), "TIME"),
                                        pts,
                                    ),
                                )
                            )
                        )
                        if running_time < 0:
                            generation.streaming_error = "audio buffer running time is invalid"
                        else:
                            end_running_time = int(
                                cast(
                                    SupportsInt,
                                    self._method(segment, "to_running_time")(
                                        self._enum_member(self._gst_member("Format"), "TIME"),
                                        pts + duration,
                                    ),
                                )
                            )
                            if end_running_time <= running_time:
                                generation.streaming_error = (
                                    "audio buffer end running time is invalid"
                                )
                            else:
                                generation.last_audio_end_running_time_ns = end_running_time
                        if generation.streaming_error is None and len(
                            generation.audio_running_times
                        ) >= _MAX_AUDIO_TIMESTAMPS:
                            generation.streaming_error = (
                                "audio running-time observation exceeded its bound"
                            )
                        elif generation.streaming_error is None:
                            generation.audio_running_times.append(running_time)
                return probe_return

            if not self._method(audio_src, "add_probe")(probe_type, observe_audio):
                raise GStreamerDriverError("generation audio observation probe failed")
            output_audio = output_audio_pad
            event_probe = self._enum_member(self._gst_member("PadProbeType"), "EVENT_DOWNSTREAM")
            eos_type = self._enum_member(self._gst_member("EventType"), "EOS")
            custom_type = self._enum_member(self._gst_member("EventType"), "CUSTOM_DOWNSTREAM")
            drop_return = self._enum_member(self._gst_member("PadProbeReturn"), "DROP")

            def arbitrate_eos(_pad: Any, info: Any) -> Any:
                event = info.get_event()
                if event is not None and event.type == custom_type:
                    structure = event.get_structure()
                    if (
                        structure is not None
                        and structure.get_name() == "dashcam-audio-loss-barrier"
                        and generation.audio_eos.observe_barrier(int(event.get_seqnum()))
                    ):
                        return drop_return
                if event is not None and event.type == eos_type:
                    accepted = generation.audio_eos.observe_eos(int(event.get_seqnum()))
                    if not accepted:
                        return drop_return
                    return probe_return
                return probe_return

            if not self._method(output_audio, "add_probe")(event_probe, arbitrate_eos):
                raise GStreamerDriverError("generation audio EOS arbiter failed")
        return generation

    def _configure_sync_generation_output(
        self,
        output: object,
    ) -> None:
        """Bind reusable non-detaching mp4mux/filesink children and prove them."""

        muxer = self._method(output, "get_property")("muxer")
        sink = self._method(output, "get_property")("sink")
        if muxer is None or sink is None:
            raise GStreamerDriverError("synchronous generation muxer/sink is absent")
        self._method(muxer, "set_property")("fragment-duration", 1000)
        observed_muxer = self._method(output, "get_property")("muxer")
        observed_sink = self._method(output, "get_property")("sink")
        fragment_duration = self._method(muxer, "get_property")("fragment-duration")
        fragment_mode = self._method(muxer, "get_property")("fragment-mode")
        try:
            duration_value = int(cast(SupportsInt, fragment_duration))
            mode_value = int(cast(SupportsInt, fragment_mode))
        except (TypeError, ValueError) as error:
            raise GStreamerDriverError(
                "synchronous generation muxer properties are invalid"
            ) from error
        if (
            self._method(output, "get_property")("async-finalize") is not False
            or self._method(output, "get_property")("reset-muxer") is not True
            or observed_muxer is not muxer
            or observed_sink is not sink
            or self._factory_name(muxer) != "mp4mux"
            or self._factory_name(sink) != "filesink"
            or duration_value != 1000
            or mode_value != 0
        ):
            raise GStreamerDriverError("synchronous generation output contract differs")

    def _set_generation_linked(
        self,
        context: _GenerationPipeline,
        generation: _RecordingGeneration,
        linked: bool,
    ) -> None:
        link_ok = self._enum_member(self._gst_member("PadLinkReturn"), "OK")
        if linked:
            if generation.linked:
                raise GStreamerDriverError("generation is already externally linked")
            if self._method(generation.video_tee_pad, "link")(generation.video_ghost) != link_ok:
                raise GStreamerDriverError("generation video tee link failed")
            if generation.has_audio and (
                self._method(generation.audio_tee_pad, "link")(generation.audio_ghost) != link_ok
            ):
                self._method(generation.video_tee_pad, "unlink")(generation.video_ghost)
                raise GStreamerDriverError("generation audio tee link failed")
        else:
            if not generation.linked:
                raise GStreamerDriverError("generation is not externally linked")
            if not self._method(generation.video_tee_pad, "unlink")(generation.video_ghost):
                raise GStreamerDriverError("generation video tee unlink failed")
            if generation.has_audio and not self._method(generation.audio_tee_pad, "unlink")(
                generation.audio_ghost
            ):
                raise GStreamerDriverError("generation audio tee unlink failed")
        generation.linked = linked

    def _set_generation_open(
        self,
        generation: _RecordingGeneration,
        opened: bool,
    ) -> None:
        self._method(generation.video_valve, "set_property")("drop", not opened)
        if generation.audio_valve is not None:
            self._method(generation.audio_valve, "set_property")("drop", not opened)

    def _audio_loss_route_is_contained(
        self,
        generation: _RecordingGeneration,
    ) -> bool:
        """Prove audio cannot re-enter a committed failed A/V route."""

        if generation.linked or generation.audio_valve is None:
            return False
        try:
            return (
                bool(self._method(generation.audio_valve, "get_property")("drop"))
                is True
            )
        except GStreamerDriverError:
            return False

    def _prewarm_generation(
        self,
        context: _GenerationPipeline,
        generation: _RecordingGeneration,
    ) -> None:
        """Start one closed standby bin before the common encoded stream is blocked."""

        activation_id = generation.activation_id
        owner = (generation.generation_id, activation_id)
        if (
            context.generations.get(generation.generation_id) is not generation
            or activation_id is None
            or generation.linked
            or generation.retired
            or generation.opened
            or bool(self._method(generation.video_valve, "get_property")("drop")) is not True
            or (
                generation.audio_valve is not None
                and bool(self._method(generation.audio_valve, "get_property")("drop")) is not True
            )
            or owner in context.location_generation.values()
        ):
            raise GStreamerDriverError(
                "standby generation cannot prewarm from its current ownership state"
            )
        self._trace_handoff(
            "successor_prewarm_started",
            activation_id=activation_id,
            slot_id=generation.generation_id,
        )
        if not self._method(generation.bin, "set_locked_state")(False):
            raise GStreamerDriverError("standby generation could not unlock before IDR")
        if not self._method(generation.bin, "sync_state_with_parent")():
            raise GStreamerDriverError("standby generation could not synchronize before IDR")
        if (
            generation.linked
            or generation.opened
            or owner in context.location_generation.values()
            or bool(self._method(generation.video_valve, "get_property")("drop")) is not True
            or (
                generation.audio_valve is not None
                and bool(self._method(generation.audio_valve, "get_property")("drop")) is not True
            )
        ):
            raise GStreamerDriverError(
                "standby generation changed media ownership while prewarming"
            )
        self._trace_handoff(
            "successor_prewarm_complete",
            activation_id=activation_id,
            slot_id=generation.generation_id,
        )

    def _release_block_probe(
        self,
        pad: Any,
        probe_id: Any,
        *,
        reached: Event,
        completed: Event,
        release: Event,
        timeout_s: float,
    ) -> None:
        """Prove a callback-owned probe exited, or remove a never-entered probe."""

        release.set()
        deadline = time.monotonic() + timeout_s
        if reached.is_set():
            if not completed.wait(max(deadline - time.monotonic(), 0)):
                raise GStreamerDriverError("blocking probe callback did not exit")
        else:
            self._method(pad, "remove_probe")(probe_id)
        while bool(self._method(pad, "is_blocked")()) or bool(self._method(pad, "is_blocking")()):
            if time.monotonic() >= deadline:
                raise GStreamerDriverError("stream pad remained blocked after probe release")
            time.sleep(0.005)

    def _remove_retained_probe(
        self,
        pad: Any,
        probe_id: Any,
        timeout_s: float,
    ) -> None:
        self._method(pad, "remove_probe")(probe_id)
        deadline = time.monotonic() + timeout_s
        while bool(self._method(pad, "is_blocked")()) or bool(self._method(pad, "is_blocking")()):
            if time.monotonic() >= deadline:
                raise GStreamerDriverError("retained stream probe did not release its pad")
            time.sleep(0.005)

    def _start_retired_video_eos_dispatch(
        self,
        context: _GenerationPipeline,
        generation: _RecordingGeneration,
        label: str,
    ) -> _RetirementDispatch:
        """Send one bounded EOS only to one exact inactive retired video branch."""

        registered = [
            candidate for candidate in context.generations.values() if candidate is generation
        ]
        activation_owner = (generation.generation_id, generation.activation_id)
        generation_eos_already_closed = bool(
            generation.video_retirement_eos_sent
            and not generation.opened
            and generation.last_closed_location is not None
            and activation_owner not in context.location_generation.values()
        )
        if (
            len(registered) != 1
            or generation.linked
            or generation.activation_id is None
            or (len(generation.opened) != 1 and not generation_eos_already_closed)
        ):
            raise GStreamerDriverError(
                "retiring video EOS has no exact inactive open-slot ownership"
            )
        if self._method(generation.video_queue, "get_parent")() is not generation.bin:
            raise GStreamerDriverError("retiring video EOS queue ancestry differs")
        video_sink = self._method(generation.video_queue, "get_static_pad")("sink")
        if video_sink is None:
            raise GStreamerDriverError("retiring video EOS sink pad is absent")
        if self._method(generation.video_queue, "get_static_pad")("sink") is not video_sink:
            raise GStreamerDriverError("retiring video EOS sink pad identity is unstable")
        event_type = self._gst_member("Event")
        new_eos = cast(
            Callable[[], object],
            _dynamic_attribute(event_type, "new_eos"),
        )
        event = new_eos()
        if event is None:
            raise GStreamerDriverError("retiring video EOS event construction failed")
        try:
            event_seqnum = int(cast(SupportsInt, self._method(event, "get_seqnum")()))
        except (TypeError, ValueError) as error:
            raise GStreamerDriverError("retiring video EOS sequence identity is invalid") from error
        if event_seqnum < 0:
            raise GStreamerDriverError("retiring video EOS sequence identity is invalid")
        activation_id = generation.activation_id
        if activation_id is None:
            raise GStreamerDriverError("retiring video EOS activation identity disappeared")
        context.retirement_dispatches[:] = [
            dispatch
            for dispatch in context.retirement_dispatches
            if not dispatch.done.is_set() or dispatch.thread.is_alive()
        ]
        if context.retirement_dispatches:
            raise GStreamerDriverError("retiring video EOS dispatch count exceeded its bound")
        if (
            context.generations.get(generation.generation_id) is not generation
            or generation.activation_id != activation_id
            or generation.linked
        ):
            raise GStreamerDriverError("retiring video EOS ownership changed before dispatch")
        if generation.video_retirement_eos_sent:
            generation_seqnum = generation.generation_retirement_eos_seqnum
            if (
                not generation.has_audio
                or generation_seqnum is None
                or generation.audio_eos.generation_snapshot()
                not in {
                    (
                        "GENERATION",
                        1,
                        generation_seqnum,
                        generation_seqnum,
                        0,
                        False,
                        True,
                    ),
                    (
                        "GENERATION",
                        1,
                        generation_seqnum,
                        generation_seqnum,
                        1,
                        False,
                        True,
                    ),
                }
            ):
                raise GStreamerDriverError(
                    "retiring generation EOS has no exact A/V closure proof"
                )
            done = Event()
            done.set()
            completed_thread = Thread(
                target=lambda: None,
                name=f"dashcam-{label}",
                daemon=True,
            )
            completed_thread.start()
            completed_thread.join()
            dispatch = _RetirementDispatch(
                label,
                completed_thread,
                done,
                accepted=True,
                generation=generation,
                activation_id=activation_id,
                pad=video_sink,
                branch="video",
                eos_seqnum=generation_seqnum,
            )
            context.retirement_dispatches.append(dispatch)
            self._trace_handoff(
                "retiring_generation_eos_reused_for_video_closure",
                activation_id=activation_id,
                eos_seqnum=generation_seqnum,
                slot_id=generation.generation_id,
            )
            return dispatch
        generation.video_retirement_eos_sent = True
        done = Event()
        dispatch = _RetirementDispatch(
            label,
            Thread(target=lambda: None, name=f"dashcam-{label}", daemon=True),
            done,
            generation=generation,
            activation_id=activation_id,
            pad=video_sink,
            branch="video",
            eos_seqnum=event_seqnum,
        )
        self._trace_handoff(
            "retiring_video_eos_created",
            activation_id=activation_id,
            eos_seqnum=event_seqnum,
            slot_id=generation.generation_id,
        )

        def worker() -> None:
            try:
                dispatch.accepted = bool(self._method(video_sink, "send_event")(event))
            except BaseException as error:
                dispatch.error = error
            finally:
                done.set()

        dispatch.thread = Thread(
            target=worker,
            name=f"dashcam-{label}",
            daemon=True,
        )
        context.retirement_dispatches.append(dispatch)
        dispatch.thread.start()
        return dispatch

    def _start_force_key_dispatch(
        self,
        context: _GenerationPipeline,
        generation: _RecordingGeneration,
        pad: Any,
        event: Any,
    ) -> _BoundedEventDispatch:
        """Dispatch an upstream force-key event without an unkillable executor wait."""

        context.force_key_dispatches[:] = [
            dispatch
            for dispatch in context.force_key_dispatches
            if not dispatch.done.is_set()
            or (dispatch.thread is not None and dispatch.thread.is_alive())
        ]
        if context.force_key_dispatches:
            raise GStreamerDriverError("forced-IDR dispatch count exceeded its bound")
        activation_id = generation.activation_id
        if (
            activation_id is None
            or context.generations.get(generation.generation_id) is not generation
            or not generation.linked
        ):
            raise GStreamerDriverError("forced-IDR dispatch activation ownership differs")
        done = Event()
        dispatch = _BoundedEventDispatch(
            "forced-idr-request",
            Thread(target=lambda: None, name="dashcam-forced-idr-request", daemon=True),
            done,
            pad,
            activation_id,
        )

        def worker() -> None:
            try:
                dispatch.accepted = bool(self._method(pad, "send_event")(event))
            except BaseException as error:
                dispatch.error = error
            finally:
                done.set()

        dispatch.thread = Thread(
            target=worker,
            name="dashcam-forced-idr-request",
            daemon=True,
        )
        context.force_key_dispatches.append(dispatch)
        dispatch.thread.start()
        return dispatch

    def _dispatch_audio_retirement_event(
        self,
        context: _GenerationPipeline,
        generation: _RecordingGeneration,
        pad: Any,
        event: Any,
        *,
        label: str,
        deadline: float,
    ) -> bool:
        """Send one branch-local audio event with survivor-audited bounds."""

        if len(context.audio_retirement_dispatches) >= 2:
            raise GStreamerDriverError("audio retirement dispatch count exceeded its bound")
        activation_id = generation.activation_id
        if (
            activation_id is None
            or context.generations.get(generation.generation_id) is not generation
            or not generation.linked
        ):
            raise GStreamerDriverError("audio retirement dispatch ownership differs")
        done = Event()
        dispatch = _BoundedEventDispatch(
            label,
            Thread(target=lambda: None, name=f"dashcam-{label}", daemon=True),
            done,
            pad,
            activation_id,
        )

        def worker() -> None:
            try:
                dispatch.accepted = bool(self._method(pad, "send_event")(event))
            except BaseException as error:
                dispatch.error = error
            finally:
                done.set()

        dispatch.thread = Thread(target=worker, name=f"dashcam-{label}", daemon=True)
        context.audio_retirement_dispatches.append(dispatch)
        dispatch.thread.start()
        if not done.wait(max(deadline - time.monotonic(), 0)):
            raise GStreamerDriverError(f"{label} worker survived its deadline")
        dispatch.thread.join(timeout=0)
        if dispatch.thread.is_alive():
            raise GStreamerDriverError(f"{label} worker remained alive after completion")
        if (
            context.generations.get(generation.generation_id) is not generation
            or generation.activation_id != activation_id
            or dispatch.pad is not pad
        ):
            raise GStreamerDriverError(f"{label} ownership drifted")
        if dispatch.error is not None:
            raise GStreamerDriverError(
                f"{label} dispatch failed: {_bounded_detail(dispatch.error)}"
            )
        return dispatch.accepted is True

    def _send_force_key_synchronously(
        self,
        context: _GenerationPipeline,
        generation: _RecordingGeneration,
        pad: Any,
        event: Any,
    ) -> _BoundedEventDispatch:
        """Invoke send_event on the serialized owning driver thread."""

        context.force_key_dispatches[:] = [
            dispatch
            for dispatch in context.force_key_dispatches
            if not dispatch.done.is_set()
            or (dispatch.thread is not None and dispatch.thread.is_alive())
        ]
        if context.force_key_dispatches:
            raise GStreamerDriverError("forced-IDR dispatch count exceeded its bound")
        activation_id = generation.activation_id
        if (
            activation_id is None
            or context.generations.get(generation.generation_id) is not generation
            or not generation.linked
        ):
            raise GStreamerDriverError("forced-IDR dispatch activation ownership differs")
        dispatch = _BoundedEventDispatch(
            "forced-idr-request",
            None,
            Event(),
            pad,
            activation_id,
            caller_thread_ident=get_ident(),
        )
        context.force_key_dispatches.append(dispatch)
        try:
            dispatch.accepted = bool(self._method(pad, "send_event")(event))
        except BaseException as error:
            dispatch.error = error
        finally:
            dispatch.done.set()
        return dispatch

    @staticmethod
    def _await_force_key_dispatch(
        context: _GenerationPipeline,
        generation: _RecordingGeneration,
        dispatch: _BoundedEventDispatch,
        deadline: float,
    ) -> None:
        if not dispatch.done.wait(max(deadline - time.monotonic(), 0)):
            raise GStreamerDriverError("forced-IDR request dispatch exceeded its deadline")
        if time.monotonic() > deadline:
            raise GStreamerDriverError("forced-IDR request dispatch exceeded its deadline")
        if dispatch.thread is not None:
            dispatch.thread.join(timeout=0)
        if dispatch.thread is not None and dispatch.thread.is_alive():
            raise GStreamerDriverError("forced-IDR request worker remained alive")
        if (
            generation.activation_id != dispatch.activation_id
            or context.generations.get(generation.generation_id) is not generation
        ):
            raise GStreamerDriverError("forced-IDR request activation ownership drifted")
        if dispatch.error is not None:
            raise GStreamerDriverError(
                "forced-IDR request dispatch failed: "
                f"{_bounded_detail(dispatch.error)}"
            )
        if dispatch.accepted is not True:
            raise GStreamerDriverError("encoder source refused forced-IDR request")

    @staticmethod
    def _prove_retirement_has_no_successor_fragment(
        context: _GenerationPipeline,
        generation: _RecordingGeneration,
        preserve_bus: Callable[[float], None],
        deadline: float,
    ) -> None:
        """Observe a bounded quiet interval and refuse retired-slot reuse on reopen."""

        quiet_deadline = time.monotonic() + 0.1
        if quiet_deadline > deadline:
            raise GStreamerDriverError(
                "retirement successor-fragment observation exceeded its deadline"
            )
        while time.monotonic() < quiet_deadline:
            preserve_bus(min(0.02, max(quiet_deadline - time.monotonic(), 0.001)))
        owner = (generation.generation_id, generation.activation_id)
        if generation.opened or owner in context.location_generation.values():
            raise GStreamerDriverError("retirement EOS opened an unexpected successor fragment")

    @staticmethod
    def _await_retirement_dispatch(
        context: _GenerationPipeline,
        dispatch: _RetirementDispatch,
        deadline: float,
    ) -> None:
        remaining = max(deadline - time.monotonic(), 0)
        if not dispatch.done.wait(remaining):
            raise GStreamerDriverError(f"{dispatch.label} video EOS dispatch exceeded its deadline")
        dispatch.thread.join(timeout=0)
        if dispatch.thread.is_alive():
            raise GStreamerDriverError(
                f"{dispatch.label} video EOS worker remained alive after completion"
            )
        generation = dispatch.generation
        if (
            generation is None
            or dispatch.activation_id is None
            or dispatch.pad is None
            or dispatch.branch != "video"
            or context.generations.get(generation.generation_id) is not generation
            or generation.activation_id != dispatch.activation_id
            or generation.linked
        ):
            raise GStreamerDriverError(
                f"{dispatch.label} video EOS activation/pad ownership drifted"
            )
        current_pad = PyGObjectGStreamerDriver._method(
            generation.video_queue,
            "get_static_pad",
        )("sink")
        if current_pad is not dispatch.pad:
            raise GStreamerDriverError(
                f"{dispatch.label} video EOS activation/pad ownership drifted"
            )
        if dispatch.error is not None:
            raise GStreamerDriverError(
                f"{dispatch.label} video EOS dispatch failed: {_bounded_detail(dispatch.error)}"
            )
        if dispatch.accepted is not True:
            raise GStreamerDriverError(f"{dispatch.label} video EOS dispatch was refused")

    def _establish_audio_retirement_boundary(
        self,
        context: _GenerationPipeline,
        generation: _RecordingGeneration,
        audio_queue: object,
        deadline: float,
    ) -> str:
        """Use natural EOS or one exact generation EOS to close retiring A/V."""

        natural = generation.audio_eos.boundary_kind()
        if natural == "NATURAL":
            self._trace_handoff("audio_natural_boundary_observed")
            return natural
        before = generation.audio_eos.snapshot()
        if before != ("OPEN", 0, None, None, False, False):
            raise GStreamerDriverError("audio retirement has no exact open or natural boundary")
        event = self._gst_member("Event")
        generation_eos = self._method(event, "new_eos")()
        generation_seqnum_raw = self._method(generation_eos, "get_seqnum")()
        if (
            isinstance(generation_seqnum_raw, bool)
            or not isinstance(generation_seqnum_raw, int)
            or not 0 <= generation_seqnum_raw <= 2**32 - 1
        ):
            raise GStreamerDriverError("generation EOS seqnum is invalid")
        try:
            generation.audio_eos.reserve_generation_eos(generation_seqnum_raw)
        except GStreamerDriverError:
            raced = generation.audio_eos.boundary_kind()
            if raced == "NATURAL":
                self._trace_handoff("audio_natural_boundary_won_generation_reservation")
                return raced
            raise
        generation_sent = self._dispatch_audio_retirement_event(
            context,
            generation,
            generation.output,
            generation_eos,
            label="av-generation-retirement-eos",
            deadline=deadline,
        )
        boundary = generation.audio_eos.boundary_kind()
        after = generation.audio_eos.generation_snapshot()
        if (
            boundary != "GENERATION"
            or after
            not in {
                (
                    "GENERATION",
                    1,
                    generation_seqnum_raw,
                    generation_seqnum_raw,
                    0,
                    False,
                    True,
                ),
                (
                    "GENERATION",
                    1,
                    generation_seqnum_raw,
                    generation_seqnum_raw,
                    1,
                    False,
                    True,
                ),
            }
        ):
            raise GStreamerDriverError(
                "generation EOS has no exact A/V retirement acceptance"
            )
        generation.video_retirement_eos_sent = True
        generation.generation_retirement_eos_seqnum = generation_seqnum_raw
        self._trace_handoff(
            "av_generation_retirement_eos_observed",
            eos_seqnum=generation_seqnum_raw,
            foreign_eos_count=after[4],
            send_return=int(generation_sent),
        )
        return boundary

    def _wait_for_audio_queue_drain(
        self,
        audio_queue: object,
        deadline: float,
        *,
        on_first_empty: Callable[[], None] | None = None,
    ) -> None:
        """Detect empty promptly, then prove it with a second sample 50 ms later."""

        consecutive_empty = 0
        first_empty_action_ran = False
        while True:
            snapshot = tuple(
                int(
                    cast(
                        SupportsInt,
                        self._method(audio_queue, "get_property")(property_name),
                    )
                )
                for property_name in (
                    "current-level-buffers",
                    "current-level-bytes",
                    "current-level-time",
                )
            )
            if any(value < 0 for value in snapshot):
                raise GStreamerDriverError("retiring audio queue reported a negative level")
            if snapshot == (0, 0, 0):
                consecutive_empty += 1
                if consecutive_empty == 1 and on_first_empty is not None:
                    on_first_empty()
                    first_empty_action_ran = True
                if consecutive_empty == 2:
                    return
            else:
                if first_empty_action_ran:
                    raise GStreamerDriverError(
                        "retiring audio queue refilled after first-empty action"
                    )
                consecutive_empty = 0
            sleep_s = 0.05 if consecutive_empty == 1 else 0.01
            now = time.monotonic()
            if now + sleep_s > deadline:
                raise GStreamerDriverError("retiring audio queue drain timed out")
            time.sleep(sleep_s)

    def create_pipeline(
        self,
        description: str,
        location_pattern: str,
        start_index: int,
        audio_plan: AudioCapturePlan | None = None,
    ) -> object:
        try:
            parse_launch = cast(
                Callable[[str], object],
                _dynamic_attribute(self._gst, "parse_launch"),
            )
            pipeline = parse_launch(description)
            output = self._method(pipeline, "get_by_name")("output")
            if output is not None:
                self._method(output, "set_property")("location", location_pattern)
                self._method(output, "set_property")("start-index", start_index)
                self._attach_overlay_renderer(pipeline)
                return pipeline
            video_tee = self._method(pipeline, "get_by_name")("video_tee")
            audio_tee = self._method(pipeline, "get_by_name")("audio_tee")
            video_continuity_queue = self._method(pipeline, "get_by_name")("video_continuity_queue")
            video_continuity_sink = self._method(pipeline, "get_by_name")("video_continuity_sink")
            camera = self._method(pipeline, "get_by_name")("camera")
            encoder = self._method(pipeline, "get_by_name")("encoder")
            match = re.fullmatch(
                r"(?P<directory>/srv/dashcam/pending)/boot-"
                r"(?P<boot>[a-z0-9]{5,16})-%06d\.partial\.mp4",
                location_pattern,
            )
            if (
                video_tee is None
                or audio_tee is None
                or video_continuity_queue is None
                or video_continuity_sink is None
                or camera is None
                or encoder is None
                or match is None
            ):
                raise GStreamerDriverError("selected pipeline has no named split muxer")
            video_continuity_sink_pad = self._method(
                video_continuity_queue,
                "get_static_pad",
            )("sink")
            if video_continuity_sink_pad is None:
                raise GStreamerDriverError("video continuity queue has no sink pad")
            video_continuity_tee_pad = self._method(
                video_continuity_sink_pad,
                "get_peer",
            )()
            if (
                video_continuity_tee_pad is None
                or self._method(video_continuity_tee_pad, "get_peer")()
                is not video_continuity_sink_pad
            ):
                raise GStreamerDriverError("video continuity tee ownership is absent or asymmetric")
            context = _GenerationPipeline(
                pipeline,
                self._gst,
                match.group("directory"),
                match.group("boot"),
                start_index,
                video_tee,
                audio_tee,
                camera,
                encoder,
                {},
                Lock(),
                {},
                deque(),
                video_continuity_queue=video_continuity_queue,
                video_continuity_sink=video_continuity_sink,
                video_continuity_tee_pad=video_continuity_tee_pad,
            )
            if audio_plan is None:
                raise GStreamerDriverError("immutable generation pipeline omitted its audio plan")
            self._install_audio_ingress(
                context,
                audio_plan,
                synchronize=False,
                bind_metrics=False,
            )
            if len(self._generation_pipelines) >= _MAX_GENERATION_CONTEXTS:
                raise GStreamerDriverError("immutable generation pipeline count exceeded its bound")
            first = self._make_generation(context, 1, True)
            second = self._make_generation(context, 2, False)
            third = self._make_generation(context, 3, False)
            first.activation_id = 1
            context.generations = {1: first, 2: second, 3: third}
            self._set_generation_linked(context, first, True)
            self._set_generation_open(first, True)
            if not self._method(first.bin, "set_locked_state")(False):
                raise GStreamerDriverError("initial generation could not unlock")
            self._attach_overlay_renderer(pipeline)
            self._generation_pipelines[id(pipeline)] = context
            return pipeline
        except Exception as error:
            if isinstance(error, GStreamerDriverError):
                raise
            raise GStreamerDriverError(
                f"could not construct the selected pipeline: {_bounded_detail(error)}"
            ) from error

    def _attach_overlay_renderer(self, pipeline: object) -> None:
        """Attach exactly one renderer after the named exact-caps filter."""

        if id(pipeline) in self._overlay_renderers:
            raise GStreamerDriverError("pipeline already owns a native overlay probe")
        if self._gstvideo is None or self._gstallocators is None:
            # Dependency-less construction is retained only for pure unit fakes;
            # :meth:`load` always supplies and validates both target bindings.
            return
        capsfilter = self._method(pipeline, "get_by_name")("overlay_input")
        if capsfilter is None:
            raise GStreamerDriverError("selected pipeline has no exact overlay capsfilter")
        renderer = GstDmabufOverlayRenderer(
            self._gst,
            self._gstallocators,
            self._gstvideo,
        )
        try:
            renderer.attach(capsfilter)
        except Exception as error:
            with suppress(Exception):
                renderer.close()
            raise GStreamerDriverError(
                f"could not attach native overlay probe: {_bounded_detail(error)}"
            ) from error
        self._overlay_renderers[id(pipeline)] = renderer

    def set_overlay_text(self, pipeline: object, text: str | None) -> None:
        """Apply one validated live text update to the pipeline-owned probe."""

        try:
            _validate_overlay_text(text)
            renderer = self._overlay_renderers.get(id(pipeline))
            if renderer is None:
                raise GStreamerDriverError("selected pipeline has no native overlay probe")
            renderer.set_text(text)
        except Exception as error:
            if isinstance(error, GStreamerDriverError | ValueError):
                raise
            raise GStreamerDriverError(
                f"could not update the burned overlay: {_bounded_detail(error)}"
            ) from error

    def overlay_snapshot(self, pipeline: object) -> dict[str, object]:
        """Read coordinate-free native-DMABUF accounting."""

        try:
            renderer = self._overlay_renderers.get(id(pipeline))
            if renderer is None:
                raise GStreamerDriverError("selected pipeline has no native overlay probe")
            snapshot = renderer.snapshot()
            if not isinstance(snapshot, dict):
                raise GStreamerDriverError("native overlay returned an invalid snapshot")
            return snapshot
        except Exception as error:
            if isinstance(error, GStreamerDriverError):
                raise
            raise GStreamerDriverError(
                f"could not inspect the native overlay: {_bounded_detail(error)}"
            ) from error

    @staticmethod
    def _method(target: object, name: str) -> Callable[..., object]:
        try:
            return cast(Callable[..., object], getattr(target, name))
        except AttributeError as error:
            raise GStreamerDriverError(f"GStreamer object lacks {name}") from error

    @staticmethod
    def _enum_member(container: object, name: str) -> object:
        try:
            return getattr(container, name)
        except AttributeError as error:
            raise GStreamerDriverError(f"GStreamer enum lacks {name}") from error

    @staticmethod
    def _timeout_ns(timeout_s: float) -> int:
        return int(timeout_s * 1_000_000_000)

    def _state(self, name: str) -> object:
        return self._enum_member(self._gst_member("State"), name)

    def _state_return(self, name: str) -> object:
        return self._enum_member(self._gst_member("StateChangeReturn"), name)

    def _set_and_verify_state(self, pipeline: object, state_name: str, timeout_s: float) -> None:
        state = self._state(state_name)
        result = self._method(pipeline, "set_state")(state)
        if result == self._state_return("FAILURE"):
            raise GStreamerDriverError(f"GStreamer rejected {state_name} state")
        outcome = self._method(pipeline, "get_state")(self._timeout_ns(timeout_s))
        if not isinstance(outcome, tuple) or len(outcome) < 2:
            raise GStreamerDriverError("GStreamer returned an invalid state result")
        change, current = outcome[0], outcome[1]
        if change == self._state_return("FAILURE") or current != state:
            raise GStreamerDriverError(
                f"GStreamer did not reach {state_name} within {timeout_s:g} seconds"
            )

    def _capture_restoration_parent_failure_provenance(
        self,
        context: _GenerationPipeline,
        retiring: _RecordingGeneration,
        successor: _RecordingGeneration,
    ) -> _RestorationParentFailureProvenance | None:
        """Bind a pre-existing parent failure before restoration mutates the graph."""

        self._drain_handoff_fatal_bus(
            context,
            context.pipeline,
            "restoration provenance capture",
        )
        playing = self._state("PLAYING")
        void_pending = self._state("VOID_PENDING")
        failure = self._state_return("FAILURE")
        parent_outcome = self._method(context.pipeline, "get_state")(0)
        if not isinstance(parent_outcome, tuple) or len(parent_outcome) < 3:
            raise GStreamerDriverError("restoration provenance parent state is invalid")
        if (
            parent_outcome[0] != failure
            and parent_outcome[1] == playing
            and parent_outcome[2] == void_pending
        ):
            self._drain_handoff_fatal_bus(
                context,
                context.pipeline,
                "restoration normal-parent capture",
            )
            return None
        quarantine = context.audio_ingress_quarantine
        ingress = context.audio_ingress_bin
        source = context.audio_ingress_elements.get("audio_source")
        retiring_activation = retiring.activation_id
        locations = set(retiring.opened)
        if (
            parent_outcome[:3] != (failure, playing, void_pending)
            or not context.loss_verified
            or not context.isolated
            or context.routing_phase != "VIDEO_ONLY_ACTIVE"
            or context.active_generation_id not in {2, 3}
            or context.generations.get(context.active_generation_id) is not retiring
            or retiring.has_audio
            or not retiring.linked
            or retiring_activation is None
            or len(locations) != 1
            or context.location_generation.get(next(iter(locations)))
            != (retiring.generation_id, retiring_activation)
            or context.generations.get(1) is not successor
            or successor.has_audio is not True
            or successor.linked
            or successor.activation_id is not None
            or successor.opened
            or not successor.reusable
            or quarantine is None
            or ingress is None
            or quarantine.ingress is not ingress
            or quarantine.source is not source
            or quarantine.ingress_generation != context.audio_ingress_replacement_count
            or quarantine.error_count < 1
            or not 0 < quarantine.activation_id < context.next_activation_id
            or context.next_activation_id > 2**31 - 1
            or self._method(context.pipeline, "get_by_name")("camera") is not context.initial_camera
            or self._method(context.pipeline, "get_by_name")("encoder")
            is not context.initial_encoder
        ):
            raise GStreamerDriverError(
                "restoration parent failure lacks exact pre-mutation provenance"
            )
        self._measure_audio_ingress(context)
        self._drain_handoff_fatal_bus(
            context,
            context.pipeline,
            "restoration provenance capture",
        )
        return _RestorationParentFailureProvenance(
            context=context,
            pipeline=context.pipeline,
            camera=context.initial_camera,
            encoder=context.initial_encoder,
            retiring=retiring,
            retiring_activation_id=retiring_activation,
            retiring_location=next(iter(locations)),
            original_ingress=ingress,
            original_elements=context.audio_ingress_elements,
            original_quarantine=quarantine,
            original_source=source,
            replacement_count=context.audio_ingress_replacement_count,
            successor=successor,
            expected_successor_activation_id=context.next_activation_id,
            failure_state=failure,
            playing_state=playing,
            void_pending_state=void_pending,
        )

    def _restoration_parent_failure_provenance_matches(
        self,
        context: _GenerationPipeline,
        generation: _RecordingGeneration,
        provenance: _RestorationParentFailureProvenance | None,
        *,
        require_media: bool,
    ) -> bool:
        if (
            provenance is None
            or provenance.consumed
            or provenance.context is not context
            or provenance.pipeline is not context.pipeline
            or provenance.camera is not context.initial_camera
            or provenance.encoder is not context.initial_encoder
            or provenance.successor is not generation
            or context.generations.get(provenance.retiring.generation_id) is not provenance.retiring
            or provenance.retiring.activation_id != provenance.retiring_activation_id
            or provenance.retiring.linked
            or provenance.retiring_location not in provenance.retiring.opened
            or context.location_generation.get(provenance.retiring_location)
            != (
                provenance.retiring.generation_id,
                provenance.retiring_activation_id,
            )
            or context.generations.get(1) is not generation
            or context.active_generation_id != 1
            or context.routing_phase != "AV_RESTORING"
            or not context.isolated
            or not context.loss_verified
            or not generation.has_audio
            or not generation.linked
            or generation.activation_id != provenance.expected_successor_activation_id
            or context.next_activation_id != provenance.expected_successor_activation_id + 1
            or context.audio_ingress_quarantine is not None
            or context.audio_ingress_bin is None
            or context.audio_ingress_bin is provenance.original_ingress
            or set(context.audio_ingress_elements) != AUDIO_BRANCH_ELEMENT_NAMES
            or context.audio_ingress_elements is provenance.original_elements
            or any(
                element is original
                for element in context.audio_ingress_elements.values()
                for original in provenance.original_elements.values()
            )
            or context.audio_ingress_elements.get("audio_source") is provenance.original_source
            or context.audio_ingress_replacement_count != provenance.replacement_count + 1
            or self._method(context.pipeline, "get_by_name")("camera") is not provenance.camera
            or self._method(context.pipeline, "get_by_name")("encoder") is not provenance.encoder
            or any(
                message.kind
                in {
                    BusMessageKind.ERROR,
                    BusMessageKind.AUDIO_ERROR,
                    BusMessageKind.EOS,
                }
                for message in context.pending_messages
            )
        ):
            return False
        if not require_media:
            return True
        activation = generation.activation_id
        locations = set(generation.opened)
        video_src = self._method(generation.video_queue, "get_static_pad")("src")
        audio_queue = generation.audio_queue
        audio_src = (
            None if audio_queue is None else self._method(audio_queue, "get_static_pad")("src")
        )
        return bool(
            activation is not None
            and generation.first_video_seen.is_set()
            and generation.first_video_is_idr is True
            and generation.first_video_had_sticky_contract is True
            and generation.video_units >= 1
            and generation.audio_units >= 1
            and len(locations) == 1
            and context.location_generation.get(next(iter(locations)))
            == (generation.generation_id, activation)
            and not bool(self._method(generation.video_valve, "get_property")("drop"))
            and generation.audio_valve is not None
            and not bool(self._method(generation.audio_valve, "get_property")("drop"))
            and self._method(generation.video_tee_pad, "get_peer")() is generation.video_ghost
            and generation.audio_tee_pad is not None
            and self._method(generation.audio_tee_pad, "get_peer")() is generation.audio_ghost
            and video_src is not None
            and self._method(video_src, "get_peer")() is generation.output_video_pad
            and self._method(generation.output_video_pad, "get_peer")() is video_src
            and self._method(generation.output, "get_static_pad")("video")
            is generation.output_video_pad
            and audio_src is not None
            and generation.output_audio_pad is not None
            and self._method(audio_src, "get_peer")() is generation.output_audio_pad
            and self._method(generation.output_audio_pad, "get_peer")() is audio_src
            and self._method(generation.output, "get_static_pad")("audio_0")
            is generation.output_audio_pad
        )

    def _generation_playing_converged(
        self,
        context: _GenerationPipeline,
        generation: _RecordingGeneration,
        timeout_s: float,
        restoration_provenance: _RestorationParentFailureProvenance | None = None,
    ) -> bool:
        """Prove the active successor topology and child state after preroll."""

        if timeout_s <= 0:
            return False
        if restoration_provenance is not None:
            try:
                self._drain_handoff_fatal_bus(
                    context,
                    context.pipeline,
                    "restoration convergence preflight",
                )
            except GStreamerDriverError:
                return False
        playing = self._state("PLAYING")
        void_pending = self._state("VOID_PENDING")
        failure = self._state_return("FAILURE")
        deadline = time.monotonic() + timeout_s

        def state_of(element: object, timeout: float = 0.0) -> tuple[object, ...] | None:
            remaining = max(deadline - time.monotonic(), 0)
            if remaining <= 0:
                return None
            outcome = self._method(element, "get_state")(self._timeout_ns(min(remaining, timeout)))
            return outcome if isinstance(outcome, tuple) and len(outcome) >= 3 else None

        def is_playing(element: object, timeout: float = 0.0) -> bool:
            outcome = state_of(element, timeout)
            return bool(
                outcome is not None
                and outcome[0] != failure
                and outcome[1] == playing
                and outcome[2] == void_pending
            )

        parent_outcome = self._method(context.pipeline, "get_state")(0)
        if not isinstance(parent_outcome, tuple) or len(parent_outcome) < 3:
            return False
        parent_normal = (
            parent_outcome[0] != failure
            and parent_outcome[1] == playing
            and parent_outcome[2] == void_pending
        )
        parent_known_degraded = (
            parent_outcome[0] == failure
            and parent_outcome[1] == playing
            and parent_outcome[2] == void_pending
            and (
                self._accept_current_loss_parent_failure(context, generation)
                or (
                    restoration_provenance is not None
                    and parent_outcome[:3]
                    == (
                        restoration_provenance.failure_state,
                        restoration_provenance.playing_state,
                        restoration_provenance.void_pending_state,
                    )
                    and self._restoration_parent_failure_provenance_matches(
                        context,
                        generation,
                        restoration_provenance,
                        require_media=False,
                    )
                )
            )
            and self._method(context.pipeline, "get_by_name")("camera") is context.initial_camera
            and self._method(context.pipeline, "get_by_name")("encoder") is context.initial_encoder
        )
        if not (parent_normal or parent_known_degraded):
            return False
        if not is_playing(context.camera) or not is_playing(context.encoder):
            return False

        initial_generation_state = state_of(generation.bin)
        if not (
            initial_generation_state is not None
            and initial_generation_state[0] != failure
            and initial_generation_state[1] == playing
            and initial_generation_state[2] == void_pending
        ):
            if not (
                initial_generation_state is not None
                and initial_generation_state[0] != failure
                and initial_generation_state[1] == self._state("PAUSED")
                and initial_generation_state[2] == void_pending
            ):
                return False
            remaining = max(deadline - time.monotonic(), 0)
            if remaining <= 0:
                return False
            self._set_and_verify_state(
                generation.bin,
                "PLAYING",
                min(remaining, 1.0),
            )
        if (
            not generation.linked
            or bool(self._method(generation.bin, "is_locked_state")())
            or bool(self._method(generation.video_valve, "get_property")("drop"))
            or self._method(generation.video_tee_pad, "get_peer")() is not generation.video_ghost
        ):
            return False
        generation_elements = [
            generation.bin,
            generation.output,
            generation.video_valve,
            generation.video_queue,
        ]
        if generation.video_gate_queue is not None:
            generation_elements.append(generation.video_gate_queue)
        if generation.has_audio:
            if generation.audio_valve is None or generation.audio_queue is None:
                return False
            generation_elements.extend([generation.audio_valve, generation.audio_queue])
        for element in generation_elements:
            if not is_playing(element, 0.5):
                return False
        iterator = self._method(generation.output, "iterate_recurse")()
        iterator_result = self._gst_member("IteratorResult")
        iterator_ok = self._enum_member(iterator_result, "OK")
        iterator_done = self._enum_member(iterator_result, "DONE")
        descendants: list[object] = []
        for _ in range(32):
            item = self._method(iterator, "next")()
            if not isinstance(item, tuple) or len(item) != 2:
                return False
            if item[0] == iterator_done:
                break
            if item[0] != iterator_ok:
                return False
            descendants.append(item[1])
        else:
            return False
        factories: dict[str, list[object]] = {"mp4mux": [], "filesink": []}
        for descendant in descendants:
            factory = self._method(descendant, "get_factory")()
            if factory is None:
                continue
            name = str(self._method(factory, "get_name")())
            if name in factories:
                factories[name].append(descendant)
        if any(len(factories[name]) != 1 for name in factories):
            return False
        if not all(
            is_playing(descendant, 0.5) for values in factories.values() for descendant in values
        ):
            return False
        for current, maximum in (
            ("current-level-buffers", "max-size-buffers"),
            ("current-level-bytes", "max-size-bytes"),
            ("current-level-time", "max-size-time"),
        ):
            current_value = int(
                cast(
                    SupportsInt,
                    self._method(generation.video_queue, "get_property")(current),
                )
            )
            maximum_value = int(
                cast(
                    SupportsInt,
                    self._method(generation.video_queue, "get_property")(maximum),
                )
            )
            if maximum_value > 0 and current_value >= maximum_value:
                return False
        if generation.has_audio:
            try:
                self._measure_audio_ingress(context)
            except GStreamerDriverError:
                return False
            ingress = context.audio_ingress_bin
            if ingress is None or not is_playing(ingress, 0.5):
                return False
            try:
                ingress_children = self._iterate_bounded(
                    self._method(ingress, "iterate_recurse")(),
                    label="active audio ingress descendant",
                    maximum=16,
                )
            except GStreamerDriverError:
                return False
            if len(ingress_children) != 10 or not all(
                is_playing(child, 0.5) for child in ingress_children
            ):
                return False
        if restoration_provenance is not None:
            if not self._restoration_parent_failure_provenance_matches(
                context,
                generation,
                restoration_provenance,
                require_media=True,
            ):
                return False
            try:
                self._drain_handoff_fatal_bus(
                    context,
                    context.pipeline,
                    "restoration convergence proof",
                )
            except GStreamerDriverError:
                return False
            if not self._restoration_parent_failure_provenance_matches(
                context,
                generation,
                restoration_provenance,
                require_media=True,
            ):
                return False
            restoration_provenance.consumed = True
        return True

    @staticmethod
    def _accept_current_loss_parent_failure(
        context: _GenerationPipeline,
        generation: _RecordingGeneration,
    ) -> bool:
        """Accept only the current in-flight loss route, never restoration."""

        if (
            not context.loss_verified
            or context.generations.get(context.active_generation_id) is not generation
            or not generation.linked
            or generation.activation_id is None
            or any(
                message.kind
                in {
                    BusMessageKind.ERROR,
                    BusMessageKind.AUDIO_ERROR,
                    BusMessageKind.EOS,
                }
                for message in context.pending_messages
            )
        ):
            return False
        initial = context.generations.get(1)
        quarantine = context.audio_ingress_quarantine
        return bool(
            context.routing_phase == "VIDEO_ONLY_ACTIVE"
            and context.active_generation_id in {2, 3}
            and not generation.has_audio
            and initial is not None
            and quarantine is not None
            and initial.activation_id == quarantine.activation_id
            and quarantine.ingress is context.audio_ingress_bin
            and quarantine.source is context.audio_ingress_elements.get("audio_source")
            and quarantine.ingress_generation == context.audio_ingress_replacement_count
            and quarantine.error_count >= 1
        )

    def set_playing(self, pipeline: object, timeout_s: float) -> None:
        self._set_and_verify_state(pipeline, "PLAYING", timeout_s)
        context = self._generation_pipelines.get(id(pipeline))
        if context is not None:
            measured = self._measure_generation_topology(context)
            self._publish_stable_topology(context, measured)

    def set_null(self, pipeline: object, timeout_s: float) -> None:
        renderer = self._overlay_renderers.get(id(pipeline))
        renderer_cleanup_error: Exception | None = None
        if renderer is not None:
            try:
                renderer.close(min(timeout_s, 2.0))
            except Exception as error:
                # The probe has already been removed, but one callback may be
                # completing during the narrow post-EOS interval.  Camera
                # ownership must still be forced to NULL before reporting it.
                renderer_cleanup_error = error
        try:
            self._set_and_verify_state(pipeline, "NULL", timeout_s)
        except Exception as error:
            if renderer_cleanup_error is not None:
                raise GStreamerDriverError(
                    "GStreamer NULL transition failed after native overlay "
                    f"cleanup delay: {_bounded_detail(error)}"
                ) from renderer_cleanup_error
            raise
        if renderer is not None and renderer_cleanup_error is not None:
            try:
                renderer.close(min(timeout_s, 2.0))
            except Exception as error:
                renderer_cleanup_error = error
        context = self._generation_pipelines.get(id(pipeline))
        if context is not None:
            dispatch_deadline = time.monotonic() + min(timeout_s, 2.0)
            for dispatch in context.retirement_dispatches:
                if not dispatch.done.wait(max(dispatch_deadline - time.monotonic(), 0)):
                    raise GStreamerDriverError(
                        f"{dispatch.label} retirement worker survived parent NULL"
                    )
                dispatch.thread.join(timeout=0)
                if dispatch.thread.is_alive():
                    raise GStreamerDriverError(
                        f"{dispatch.label} retirement worker did not terminate"
                    )
                if dispatch.error is not None:
                    raise GStreamerDriverError(
                        f"{dispatch.label} retirement worker failed: "
                        f"{_bounded_detail(dispatch.error)}"
                    )
            context.retirement_dispatches.clear()
            for audio_dispatch in context.audio_retirement_dispatches:
                if not audio_dispatch.done.wait(
                    max(dispatch_deadline - time.monotonic(), 0)
                ):
                    raise GStreamerDriverError(
                        f"{audio_dispatch.label} worker survived parent NULL"
                    )
                if audio_dispatch.thread is not None:
                    audio_dispatch.thread.join(timeout=0)
                if (
                    audio_dispatch.thread is not None
                    and audio_dispatch.thread.is_alive()
                ):
                    raise GStreamerDriverError(
                        f"{audio_dispatch.label} worker did not terminate"
                    )
                if audio_dispatch.error is not None:
                    raise GStreamerDriverError(
                        f"{audio_dispatch.label} worker failed: "
                        f"{_bounded_detail(audio_dispatch.error)}"
                    )
            context.audio_retirement_dispatches.clear()
            for force_dispatch in context.force_key_dispatches:
                if not force_dispatch.done.wait(
                    max(dispatch_deadline - time.monotonic(), 0)
                ):
                    raise GStreamerDriverError(
                        f"{force_dispatch.label} worker survived parent NULL"
                    )
                if force_dispatch.thread is not None:
                    force_dispatch.thread.join(timeout=0)
                if (
                    force_dispatch.thread is not None
                    and force_dispatch.thread.is_alive()
                ):
                    raise GStreamerDriverError(
                        f"{force_dispatch.label} worker did not terminate"
                    )
                if force_dispatch.error is not None:
                    raise GStreamerDriverError(
                        f"{force_dispatch.label} worker failed: "
                        f"{_bounded_detail(force_dispatch.error)}"
                    )
            context.force_key_dispatches.clear()
            for generation in context.generations.values():
                if generation.removed_from_parent:
                    continue
                if generation.linked:
                    self._set_generation_linked(context, generation, False)
                if not generation.video_tee_pad_released:
                    self._method(context.video_tee, "release_request_pad")(generation.video_tee_pad)
                    generation.video_tee_pad_released = True
                if generation.audio_tee_pad is not None and not generation.audio_tee_pad_released:
                    self._method(context.audio_tee, "release_request_pad")(generation.audio_tee_pad)
                    generation.audio_tee_pad_released = True
                if not generation.output_video_pad_released:
                    video_src = self._method(generation.video_queue, "get_static_pad")("src")
                    peer = self._method(video_src, "get_peer")()
                    if peer is generation.output_video_pad and not self._method(
                        video_src, "unlink"
                    )(generation.output_video_pad):
                        raise GStreamerDriverError(
                            "generation video splitmux unlink failed after parent NULL"
                        )
                    if peer not in (None, generation.output_video_pad):
                        raise GStreamerDriverError(
                            "generation video splitmux peer identity changed"
                        )
                    self._method(generation.output, "release_request_pad")(
                        generation.output_video_pad
                    )
                    generation.output_video_pad_released = True
                if (
                    generation.audio_queue is not None
                    and generation.output_audio_pad is not None
                    and not generation.output_audio_pad_released
                ):
                    audio_src = self._method(generation.audio_queue, "get_static_pad")("src")
                    peer = self._method(audio_src, "get_peer")()
                    if peer is generation.output_audio_pad and not self._method(
                        audio_src, "unlink"
                    )(generation.output_audio_pad):
                        raise GStreamerDriverError(
                            "generation audio splitmux unlink failed after parent NULL"
                        )
                    if peer not in (None, generation.output_audio_pad):
                        raise GStreamerDriverError(
                            "generation audio splitmux peer identity changed"
                        )
                    self._method(generation.output, "release_request_pad")(
                        generation.output_audio_pad
                    )
                    generation.output_audio_pad_released = True
                if not self._method(context.pipeline, "remove")(generation.bin):
                    raise GStreamerDriverError("generation could not be released after parent NULL")
                generation.removed_from_parent = True
            if not context.video_continuity_tee_pad_released:
                continuity_queue = context.video_continuity_queue
                continuity_tee_pad = context.video_continuity_tee_pad
                if continuity_queue is None or continuity_tee_pad is None:
                    raise GStreamerDriverError("video continuity cleanup ownership is absent")
                continuity_queue_sink = self._method(
                    continuity_queue,
                    "get_static_pad",
                )("sink")
                if continuity_queue_sink is None:
                    raise GStreamerDriverError("video continuity cleanup sink pad is absent")
                tee_peer = self._method(continuity_tee_pad, "get_peer")()
                queue_peer = self._method(continuity_queue_sink, "get_peer")()
                if tee_peer is not continuity_queue_sink or queue_peer is not continuity_tee_pad:
                    raise GStreamerDriverError("video continuity cleanup peer identity changed")
                if not self._method(continuity_tee_pad, "unlink")(continuity_queue_sink):
                    raise GStreamerDriverError(
                        "video continuity cleanup unlink failed after parent NULL"
                    )
                self._method(context.video_tee, "release_request_pad")(continuity_tee_pad)
                context.video_continuity_tee_pad_released = True
            if (
                not all(
                    generation.removed_from_parent for generation in context.generations.values()
                )
                or not context.video_continuity_tee_pad_released
            ):
                raise GStreamerDriverError("generation cleanup remains incomplete")
            context.cleanup_complete = True
            self._generation_pipelines.pop(id(pipeline), None)
        self._overlay_renderers.pop(id(pipeline), None)
        self._metrics.pop(id(pipeline), None)
        if renderer_cleanup_error is not None:
            raise GStreamerDriverError(
                "native overlay probe cleanup exceeded its pre-NULL bound: "
                f"{_bounded_detail(renderer_cleanup_error)}"
            ) from renderer_cleanup_error

    def _encoder(self, pipeline: object) -> object:
        encoder = self._method(pipeline, "get_by_name")("encoder")
        if encoder is None:
            raise GStreamerDriverError("selected pipeline has no named encoder")
        return encoder

    @staticmethod
    def _structure_value(structure: object, field: str) -> object:
        value = PyGObjectGStreamerDriver._method(structure, "get_value")(field)
        if value is None:
            raise GStreamerDriverError(f"negotiated caps omit {field}")
        return value

    def _pad_structure(self, element: object, pad_name: str) -> object:
        pad = self._method(element, "get_static_pad")(pad_name)
        if pad is None:
            raise GStreamerDriverError(f"encoder has no {pad_name} pad")
        caps = self._method(pad, "get_current_caps")()
        if caps is None:
            raise GStreamerDriverError(f"encoder {pad_name} caps are not negotiated")
        structure = self._method(caps, "get_structure")(0)
        if structure is None:
            raise GStreamerDriverError(f"encoder {pad_name} caps are empty")
        return structure

    @staticmethod
    def _fraction(value: object) -> tuple[int, int]:
        try:
            numerator = int(cast(SupportsInt, _dynamic_attribute(value, "num")))
            denominator = int(cast(SupportsInt, _dynamic_attribute(value, "denom")))
        except (AttributeError, TypeError, ValueError) as error:
            raise GStreamerDriverError("negotiated frame rate is invalid") from error
        return numerator, denominator

    def effective_caps(self, pipeline: object) -> EffectiveCaps:
        encoder = self._encoder(pipeline)
        raw = self._pad_structure(encoder, "sink")
        encoded = self._pad_structure(encoder, "src")
        numerator, denominator = self._fraction(self._structure_value(raw, "framerate"))
        raw_name = str(self._method(raw, "get_name")())
        encoded_name = str(self._method(encoded, "get_name")())
        if raw_name != "video/x-raw" or encoded_name != "video/x-h264":
            raise GStreamerDriverError("encoder negotiated unexpected media types")
        try:
            return EffectiveCaps(
                width=int(cast(SupportsInt, self._structure_value(raw, "width"))),
                height=int(cast(SupportsInt, self._structure_value(raw, "height"))),
                frames_per_second_numerator=numerator,
                frames_per_second_denominator=denominator,
                raw_format=str(self._structure_value(raw, "format")),
                codec="h264",
                profile=str(self._structure_value(encoded, "profile")),
                level=str(self._structure_value(encoded, "level")),
            )
        except (TypeError, ValueError) as error:
            raise GStreamerDriverError("negotiated caps contain invalid values") from error

    def encoder_identity(self, pipeline: object) -> EncoderIdentity:
        encoder = self._encoder(pipeline)
        factory = self._method(encoder, "get_factory")()
        if factory is None:
            raise GStreamerDriverError("encoder has no factory identity")
        factory_name = str(self._method(factory, "get_name")())
        metadata_key = self._gst_member("ELEMENT_METADATA_KLASS")
        factory_class = str(self._method(factory, "get_metadata")(metadata_key))
        device_path = str(self._method(encoder, "get_property")("device"))
        return EncoderIdentity(factory_name, factory_class, device_path)

    def _named_element(self, pipeline: object, name: str) -> object:
        element = self._method(pipeline, "get_by_name")(name)
        if element is None:
            raise GStreamerDriverError(f"selected pipeline has no named {name}")
        return element

    def _factory_name(self, element: object) -> str:
        factory = self._method(element, "get_factory")()
        if factory is None:
            raise GStreamerDriverError("audio element has no factory identity")
        return str(self._method(factory, "get_name")())

    def effective_audio_caps(self, pipeline: object) -> EffectiveAudioCaps:
        encoder = self._named_element(pipeline, "audio_encoder")
        parser = self._named_element(pipeline, "audio_parser")
        raw = self._pad_structure(encoder, "sink")
        encoded = self._pad_structure(parser, "src")
        if (
            str(self._method(raw, "get_name")()) != "audio/x-raw"
            or str(self._method(encoded, "get_name")()) != "audio/mpeg"
        ):
            raise GStreamerDriverError("audio branch negotiated unexpected media types")
        try:
            bitrate = int(cast(SupportsInt, self._method(encoder, "get_property")("bitrate")))
            return EffectiveAudioCaps(
                raw_format=str(self._structure_value(raw, "format")),
                sample_rate_hz=int(cast(SupportsInt, self._structure_value(encoded, "rate"))),
                channels=int(cast(SupportsInt, self._structure_value(encoded, "channels"))),
                codec="aac",
                mpeg_version=int(cast(SupportsInt, self._structure_value(encoded, "mpegversion"))),
                stream_format=str(self._structure_value(encoded, "stream-format")),
                encoder_factory=self._factory_name(encoder),
                parser_factory=self._factory_name(parser),
                bitrate_bps=bitrate,
            )
        except (TypeError, ValueError) as error:
            raise GStreamerDriverError("negotiated audio caps contain invalid values") from error

    def install_metrics(self, pipeline: object, counters: PipelineCounters) -> None:
        """Attach constant-work buffer probes after the encoder has negotiated."""

        probe_type = self._enum_member(self._gst_member("PadProbeType"), "BUFFER")
        encoder = self._encoder(pipeline)
        for pad_name in ("sink", "src"):
            pad = self._method(encoder, "get_static_pad")(pad_name)
            if pad is None:
                raise GStreamerDriverError(f"encoder has no {pad_name} metrics pad")

            def probe(_pad: object, info: object, name: str = pad_name) -> object:
                if name == "sink":
                    counters.observe_raw_buffer()
                    buffer = getattr(info, "get_buffer", lambda: None)()
                    pts = None if buffer is None else getattr(buffer, "pts", None)
                    clock_none = getattr(self._gst, "CLOCK_TIME_NONE", None)
                    counters.observe_raw_pts(None if pts == clock_none else pts)
                else:
                    counters.observe_encoded_buffer()
                return self._enum_member(self._gst_member("PadProbeReturn"), "OK")

            self._method(pad, "add_probe")(probe_type, probe)
        self._metrics[id(pipeline)] = (counters, 0)

    def install_audio_metrics(self, pipeline: object, counters: PipelineCounters) -> None:
        """Observe parsed AAC access units with constant work per buffer."""

        probe_type = self._enum_member(self._gst_member("PadProbeType"), "BUFFER")
        queue = self._named_element(pipeline, "audio_record_queue")
        pad = self._method(queue, "get_static_pad")("src")
        if pad is None:
            raise GStreamerDriverError("audio output queue has no src metrics pad")

        def probe(_pad: object, _info: object) -> object:
            counters.observe_audio_encoded_buffer()
            return self._enum_member(self._gst_member("PadProbeReturn"), "OK")

        self._method(pad, "add_probe")(probe_type, probe)

    def _observe_qos(self, pipeline: object, message: object, buffers_format: object) -> None:
        tracked = self._metrics.get(id(pipeline))
        if tracked is None:
            return
        parsed = self._method(message, "parse_qos_stats")()
        if not isinstance(parsed, tuple) or len(parsed) != 3:
            raise GStreamerDriverError("GStreamer QoS stats shape is invalid")
        if parsed[0] != buffers_format:
            return
        dropped = parsed[2]
        if isinstance(dropped, bool):
            raise GStreamerDriverError("GStreamer QoS dropped count is invalid")
        try:
            cumulative = int(cast(SupportsInt, dropped))
        except (TypeError, ValueError) as error:
            raise GStreamerDriverError("GStreamer QoS dropped count is invalid") from error
        counters, previous = tracked
        if cumulative < previous:
            raise GStreamerDriverError("GStreamer QoS dropped count regressed")
        counters.observe_qos_drop(cumulative - previous)
        self._metrics[id(pipeline)] = (counters, cumulative)

    def _exact_audio_message_source(
        self,
        pipeline: object,
        message: object,
        context: _GenerationPipeline | None,
    ) -> str | None:
        """Return a name only when GstMessage.src is that exact pipeline element."""

        source = getattr(message, "src", None)
        if source is None:
            return None
        if context is not None:
            for name, element in context.audio_ingress_elements.items():
                if source is element:
                    return name
            return None
        get_by_name = self._method(pipeline, "get_by_name")
        for name in sorted(AUDIO_BRANCH_ELEMENT_NAMES):
            if get_by_name(name) is source:
                return name
        return None

    def _trace_audio_error_source(
        self,
        context: _GenerationPipeline,
        source: object | None,
    ) -> None:
        """Emit bounded identity/ancestry facts without changing classification."""

        if os.environ.get("DASHCAM_HANDOFF_TRACE") != "1":
            return
        retained = context.audio_ingress_elements.get("audio_source")
        fresh: Any | None = None
        with suppress(Exception):
            fresh = self._method(context.pipeline, "get_by_name")("audio_source")
        source_name_matches = 0
        source_owned_by_ingress = 0
        if source is not None:
            with suppress(Exception):
                source_name_matches = int(str(self._method(source, "get_name")()) == "audio_source")
            with suppress(Exception):
                parent = self._method(source, "get_parent")()
                for _ in range(8):
                    if parent is context.audio_ingress_bin:
                        source_owned_by_ingress = 1
                        break
                    if parent is None or parent is context.pipeline:
                        break
                    parent = self._method(parent, "get_parent")()
        self._trace_handoff(
            "audio_error_source_observed",
            active_generation_id=context.active_generation_id,
            current_exact=int(source is not None and source is retained),
            fresh_lookup_matches_retained=int(fresh is retained),
            ingress_generation=context.audio_ingress_replacement_count,
            message_source_id=0 if source is None else id(source),
            message_source_matches_fresh=int(source is not None and source is fresh),
            quarantine_present=int(context.audio_ingress_quarantine is not None),
            retained_source_id=0 if retained is None else id(retained),
            source_name_audio_source=source_name_matches,
            source_owned_by_ingress=source_owned_by_ingress,
        )

    def _trace_audio_eos_arm_state(
        self,
        phase: str,
        arbiter: _AudioEosArbiter,
    ) -> None:
        if os.environ.get("DASHCAM_HANDOFF_TRACE") != "1":
            return
        state, count, eos_seqnum, manual_seqnum, barrier_seen, duplicate = arbiter.snapshot()
        self._trace_handoff(
            phase,
            barrier_seen=int(barrier_seen),
            boundary_kind={
                None: 0,
                "NATURAL": 1,
                "BARRIER": 2,
                "GENERATION": 3,
            }.get(arbiter.boundary_kind(), -1),
            duplicate=int(duplicate),
            eos_count=count,
            eos_seqnum=-1 if eos_seqnum is None else eos_seqnum,
            manual_seqnum=-1 if manual_seqnum is None else manual_seqnum,
            retirement_armed=int(arbiter.is_retirement_armed()),
            state={
                "OPEN": 0,
                "NATURAL": 1,
                "BARRIER": 2,
                "REFUSED": 3,
                "GENERATION_ARMED": 4,
                "GENERATION": 5,
            }.get(state, -1),
        )

    def _trace_eos_message_source(
        self,
        context: _GenerationPipeline,
        source: object | None,
        message_seqnum: int,
    ) -> None:
        """Describe an EOS source without changing the fail-closed EOS rule."""

        if os.environ.get("DASHCAM_HANDOFF_TRACE") != "1":
            return
        source_slot = -1
        ancestor_slot = -1
        source_role = 0
        matched_generation: _RecordingGeneration | None = None
        source_name = ""
        if source is not None:
            with suppress(Exception):
                source_name = str(self._method(source, "get_path_string")())
            if not source_name:
                with suppress(Exception):
                    source_name = str(self._method(source, "get_name")())
        for slot_id, generation in sorted(context.generations.items()):
            candidates: list[tuple[object | None, int]] = [
                (generation.bin, 1),
                (generation.output, 2),
                (generation.video_queue, 3),
                (generation.audio_queue, 4),
            ]
            with suppress(Exception):
                candidates.extend(
                    [
                        (
                            self._method(generation.output, "get_property")("muxer"),
                            5,
                        ),
                        (
                            self._method(generation.output, "get_property")("sink"),
                            6,
                        ),
                    ]
                )
            for candidate, role in candidates:
                if source is not None and source is candidate:
                    source_slot = slot_id
                    source_role = role
                    matched_generation = generation
                    break
            if source is not None:
                with suppress(Exception):
                    parent = self._method(source, "get_parent")()
                    for _ in range(12):
                        if parent is generation.bin:
                            ancestor_slot = slot_id
                            if matched_generation is None:
                                matched_generation = generation
                            break
                        if parent is None or parent is context.pipeline:
                            break
                        parent = self._method(parent, "get_parent")()
            if source_slot >= 0:
                break

        def is_playing(element: object | None) -> int:
            if element is None:
                return -1
            try:
                outcome = self._method(element, "get_state")(0)
                if not isinstance(outcome, tuple) or len(outcome) < 2:
                    return -1
                return int(outcome[1] == self._state("PLAYING"))
            except Exception:
                return -1

        active = context.generations.get(context.active_generation_id)
        observed_generation = matched_generation
        self._trace_handoff_text(
            "eos_message_source_observed",
            source_name,
            active_activation_id=(
                -1 if active is None or active.activation_id is None else active.activation_id
            ),
            active_audio_units=0 if active is None else active.audio_units,
            active_slot_id=context.active_generation_id,
            active_video_units=0 if active is None else active.video_units,
            ancestor_slot_id=ancestor_slot,
            ingress_generation=context.audio_ingress_replacement_count,
            message_seqnum=message_seqnum,
            parent_pipeline_exact=int(source is context.pipeline),
            retirement_dispatch_count=len(context.retirement_dispatches),
            routing_phase={
                "AV_ACTIVE": 1,
                "SWITCHING": 2,
                "VIDEO_ONLY_ACTIVE": 3,
                "AV_RESTORING": 4,
                "RESTORATION_CRITICAL": 5,
            }.get(context.routing_phase, 0),
            source_id=0 if source is None else id(source),
            source_playing=is_playing(source),
            source_role=source_role,
            source_slot_activation_id=(
                -1
                if observed_generation is None or observed_generation.activation_id is None
                else observed_generation.activation_id
            ),
            source_slot_audio_units=(
                0 if observed_generation is None else observed_generation.audio_units
            ),
            source_slot_bin_playing=is_playing(
                None if observed_generation is None else observed_generation.bin
            ),
            source_slot_has_audio=int(
                observed_generation is not None and observed_generation.has_audio
            ),
            source_slot_last_closed=int(
                observed_generation is not None
                and observed_generation.last_closed_location is not None
            ),
            source_slot_linked=int(observed_generation is not None and observed_generation.linked),
            source_slot_open_count=(
                0 if observed_generation is None else len(observed_generation.opened)
            ),
            source_slot_output_playing=is_playing(
                None if observed_generation is None else observed_generation.output
            ),
            source_slot_retired=int(
                observed_generation is not None and observed_generation.retired
            ),
            source_slot_reusable=int(
                observed_generation is not None and observed_generation.reusable
            ),
            source_slot_video_eos_sent=int(
                observed_generation is not None and observed_generation.video_retirement_eos_sent
            ),
            source_slot_video_units=(
                0 if observed_generation is None else observed_generation.video_units
            ),
            source_slot_id=source_slot,
        )

    def poll_bus(self, pipeline: object, timeout_s: float) -> BusMessage:
        context = self._generation_pipelines.get(id(pipeline))
        if context is not None and context.pending_messages:
            return context.pending_messages.popleft()
        return self._poll_bus_native(pipeline, timeout_s)

    def generation_snapshot(self, pipeline: object) -> dict[str, object]:
        """Return the last atomically published coherent topology observation."""

        context = self._generation_pipelines.get(id(pipeline))
        if context is None:
            return {
                "topology_observation": "unavailable",
                "topology_observation_stale": False,
                "active_slot_id": None,
                "active_activation_id": None,
                "slot_count": 0,
                "slot_activations": {},
            }
        published = context.published_topology
        if published is None:
            return {
                "topology_observation": "unavailable",
                "topology_observation_stale": False,
                "active_slot_id": None,
                "active_activation_id": None,
                "slot_count": 0,
                "slot_activations": {},
                "request_pad_invariant": "unavailable",
                "request_pad_counts_measured": False,
                "request_pad_peer_ownership_proven": False,
            }
        return deepcopy(published)

    @staticmethod
    def _publish_stable_topology(
        context: _GenerationPipeline,
        measured: Mapping[str, object],
    ) -> None:
        active = context.generations.get(context.active_generation_id)
        snapshot: dict[str, object] = {
            "topology_observation": "stable",
            "topology_observation_stale": False,
            "topology_observed_monotonic_ns": time.monotonic_ns(),
            "active_slot_id": context.active_generation_id,
            "active_activation_id": (None if active is None else active.activation_id),
            "slot_count": len(context.generations),
            "slot_activations": {
                str(slot_id): generation.activation_id
                for slot_id, generation in sorted(context.generations.items())
            },
            **measured,
        }
        stable = deepcopy(snapshot)
        context.last_stable_topology = stable
        context.published_topology = deepcopy(stable)

    @staticmethod
    def _publish_topology_transition(
        context: _GenerationPipeline,
        observation: str,
        *,
        phase: str | None = None,
    ) -> None:
        stable = context.last_stable_topology
        if stable is None:
            snapshot: dict[str, object] = {
                "active_slot_id": None,
                "active_activation_id": None,
                "slot_count": 0,
                "slot_activations": {},
                "request_pad_invariant": "unavailable",
                "request_pad_counts_measured": False,
                "request_pad_peer_ownership_proven": False,
            }
        else:
            snapshot = deepcopy(stable)
        snapshot["topology_observation"] = observation
        snapshot["topology_observation_stale"] = True
        if phase is not None:
            snapshot["topology_transition_phase"] = phase
        else:
            snapshot.pop("topology_transition_phase", None)
        context.published_topology = snapshot

    def _iterate_bounded(
        self,
        iterator: Any,
        *,
        label: str,
        maximum: int,
    ) -> list[Any]:
        iterator_result = self._gst_member("IteratorResult")
        iterator_ok = self._enum_member(iterator_result, "OK")
        iterator_done = self._enum_member(iterator_result, "DONE")
        observed: list[Any] = []
        for _ in range(maximum + 1):
            item = self._method(iterator, "next")()
            if not isinstance(item, tuple) or len(item) != 2:
                raise GStreamerDriverError(f"{label} iterator shape is invalid")
            if item[0] == iterator_done:
                return observed
            if item[0] != iterator_ok:
                raise GStreamerDriverError(f"{label} iterator did not converge")
            observed.append(item[1])
            if len(observed) > maximum:
                break
        raise GStreamerDriverError(f"{label} iterator exceeded its bound")

    @staticmethod
    def _same_identity_set(observed: Sequence[Any], expected: Sequence[Any]) -> bool:
        return (
            len(observed) == len(expected)
            and len({id(value) for value in observed}) == len(observed)
            and {id(value) for value in observed} == {id(value) for value in expected}
        )

    def _measure_generation_topology(
        self,
        context: _GenerationPipeline,
    ) -> dict[str, object]:
        """Measure actual pads, peers, and the single owned audio ingress."""

        generations = tuple(generation for _, generation in sorted(context.generations.items()))
        if len(generations) != 3:
            raise GStreamerDriverError("recording topology does not have three slots")
        continuity_queue = context.video_continuity_queue
        continuity_sink = context.video_continuity_sink
        continuity_tee_pad = context.video_continuity_tee_pad
        if (
            continuity_queue is None
            or continuity_sink is None
            or continuity_tee_pad is None
            or context.video_continuity_tee_pad_released
        ):
            raise GStreamerDriverError("video continuity route ownership is absent")
        video_tee_pads = self._iterate_bounded(
            self._method(context.video_tee, "iterate_src_pads")(),
            label="video tee pad",
            maximum=5,
        )
        expected_video_tee = [
            *(generation.video_tee_pad for generation in generations),
            continuity_tee_pad,
        ]
        audio_tee_pads = self._iterate_bounded(
            self._method(context.audio_tee, "iterate_src_pads")(),
            label="audio tee pad",
            maximum=2,
        )
        expected_audio_tee = [
            generation.audio_tee_pad
            for generation in generations
            if generation.audio_tee_pad is not None
        ]
        if not self._same_identity_set(video_tee_pads, expected_video_tee):
            raise GStreamerDriverError(
                "video tee request pads differ from registered fixed-slot ownership"
            )
        if not self._same_identity_set(audio_tee_pads, expected_audio_tee):
            raise GStreamerDriverError(
                "audio tee request pads differ from registered A/V-slot ownership"
            )
        if (
            self._method(continuity_queue, "get_parent")() is not context.pipeline
            or self._method(continuity_sink, "get_parent")() is not context.pipeline
            or self._factory_name(continuity_queue) != "queue"
            or self._factory_name(continuity_sink) != "fakesink"
        ):
            raise GStreamerDriverError("video continuity route ancestry or factory differs")
        continuity_queue_sink = self._method(
            continuity_queue,
            "get_static_pad",
        )("sink")
        continuity_queue_src = self._method(
            continuity_queue,
            "get_static_pad",
        )("src")
        continuity_sink_pad = self._method(
            continuity_sink,
            "get_static_pad",
        )("sink")
        if (
            continuity_queue_sink is None
            or continuity_queue_src is None
            or continuity_sink_pad is None
            or self._method(continuity_tee_pad, "get_peer")() is not continuity_queue_sink
            or self._method(continuity_queue_sink, "get_peer")() is not continuity_tee_pad
            or self._method(continuity_queue_src, "get_peer")() is not continuity_sink_pad
            or self._method(continuity_sink_pad, "get_peer")() is not continuity_queue_src
        ):
            raise GStreamerDriverError("video continuity route peer ownership differs")
        queue_properties = tuple(
            int(
                cast(
                    SupportsInt,
                    self._method(continuity_queue, "get_property")(name),
                )
            )
            for name in (
                "max-size-buffers",
                "max-size-bytes",
                "max-size-time",
            )
        )
        leaky = self._method(continuity_queue, "get_property")("leaky")
        leaky_nick = str(getattr(leaky, "value_nick", "")).lower()
        try:
            leaky_value = int(cast(SupportsInt, leaky))
        except (TypeError, ValueError):
            leaky_value = -1
        sink_properties = tuple(
            bool(self._method(continuity_sink, "get_property")(name))
            for name in ("sync", "async", "enable-last-sample", "qos")
        )
        if (
            queue_properties != (2, 0, 0)
            or (leaky_nick != "downstream" and leaky_value != 2)
            or sink_properties != (False, False, False, False)
        ):
            raise GStreamerDriverError("video continuity route bounded properties differ")
        playing = self._state("PLAYING")
        void_pending = self._state("VOID_PENDING")
        failure = self._state_return("FAILURE")
        for element in (continuity_queue, continuity_sink):
            outcome = self._method(element, "get_state")(0)
            if (
                not isinstance(outcome, tuple)
                or len(outcome) < 3
                or outcome[0] == failure
                or outcome[1] != playing
                or outcome[2] != void_pending
            ):
                raise GStreamerDriverError("video continuity route is not stably PLAYING")
        video_linked = 0
        video_standby = 0
        audio_linked = 0
        audio_standby = 0
        splitmux_video = 0
        splitmux_audio = 0
        for generation in generations:
            video_peer = self._method(generation.video_tee_pad, "get_peer")()
            if generation.linked:
                if video_peer is not generation.video_ghost:
                    raise GStreamerDriverError("linked video tee pad has a foreign or absent peer")
                video_linked += 1
            else:
                if video_peer is not None:
                    raise GStreamerDriverError("standby video tee pad has an unexpected peer")
                video_standby += 1
            if generation.audio_tee_pad is not None:
                audio_peer = self._method(generation.audio_tee_pad, "get_peer")()
                if generation.linked:
                    if audio_peer is not generation.audio_ghost:
                        raise GStreamerDriverError(
                            "linked audio tee pad has a foreign or absent peer"
                        )
                    audio_linked += 1
                else:
                    if audio_peer is not None:
                        raise GStreamerDriverError("standby audio tee pad has an unexpected peer")
                    audio_standby += 1
            output_pads = self._iterate_bounded(
                self._method(generation.output, "iterate_sink_pads")(),
                label=f"slot {generation.generation_id} splitmux pad",
                maximum=3,
            )
            expected_output = [generation.output_video_pad]
            if generation.output_audio_pad is not None:
                expected_output.append(generation.output_audio_pad)
            if not self._same_identity_set(output_pads, expected_output):
                raise GStreamerDriverError(
                    "splitmux request pads differ from registered slot ownership"
                )
            video_queue_src = self._method(generation.video_queue, "get_static_pad")("src")
            if (
                video_queue_src is None
                or self._method(video_queue_src, "get_peer")() is not generation.output_video_pad
            ):
                raise GStreamerDriverError("splitmux video request pad peer ownership differs")
            splitmux_video += 1
            if generation.output_audio_pad is not None:
                if generation.audio_queue is None:
                    raise GStreamerDriverError("A/V slot lost its audio queue")
                audio_queue_src = self._method(generation.audio_queue, "get_static_pad")("src")
                if (
                    audio_queue_src is None
                    or self._method(audio_queue_src, "get_peer")()
                    is not generation.output_audio_pad
                ):
                    raise GStreamerDriverError("splitmux audio request pad peer ownership differs")
                splitmux_audio += 1
        if (
            len(video_tee_pads),
            len(audio_tee_pads),
            splitmux_video,
            splitmux_audio,
        ) != (4, 1, 3, 1):
            raise GStreamerDriverError("measured request-pad counts differ from 4/1/3/1")
        ingress = self._measure_audio_ingress(context)
        return {
            "request_pad_invariant": "constant_preallocated",
            "request_pad_counts_measured": True,
            "request_pad_peer_ownership_proven": True,
            "video_tee_request_pads": 4,
            "audio_tee_request_pads": 1,
            "splitmux_video_request_pads": 3,
            "splitmux_audio_request_pads": 1,
            "tee_pad_routes": {
                "video_active_linked": video_linked,
                "video_standby_unlinked": video_standby,
                "video_continuity_linked": 1,
                "audio_active_linked": audio_linked,
                "audio_standby_unlinked": audio_standby,
            },
            "audio_ingress": ingress,
        }

    def _measure_audio_ingress(
        self,
        context: _GenerationPipeline,
    ) -> dict[str, int]:
        ingress = context.audio_ingress_bin
        if ingress is None:
            raise GStreamerDriverError("owned audio ingress is absent")
        elements = self._iterate_bounded(
            self._method(context.pipeline, "iterate_recurse")(),
            label="pipeline descendant",
            maximum=96,
        )
        ingress_name = str(self._method(ingress, "get_name")())
        named_ingresses = [
            candidate
            for candidate in elements
            if str(self._method(candidate, "get_name")()) == ingress_name
        ]
        current_count = len(named_ingresses)
        descendants = 0
        required = AUDIO_BRANCH_ELEMENT_NAMES | {"audio_generation_counter"}
        required_current_counts = {name: 0 for name in required}
        stale_required_count = 0
        other_current_descendants: list[Any] = []
        current_source: Any | None = None
        for candidate in elements:
            candidate_name = str(self._method(candidate, "get_name")())
            parent = self._method(candidate, "get_parent")()
            belongs_to_current = False
            for _ in range(8):
                if parent is ingress:
                    descendants += 1
                    belongs_to_current = True
                    break
                if parent is None or parent is context.pipeline:
                    break
                parent = self._method(parent, "get_parent")()
            if candidate_name in required:
                if belongs_to_current:
                    required_current_counts[candidate_name] += 1
                    retained = context.audio_ingress_elements.get(candidate_name)
                    if candidate_name in AUDIO_BRANCH_ELEMENT_NAMES and retained is not candidate:
                        stale_required_count += 1
                    if candidate_name == "audio_source":
                        current_source = candidate
                else:
                    stale_required_count += 1
            elif belongs_to_current:
                other_current_descendants.append(candidate)
        capsfilter_count = sum(
            self._factory_name(candidate) == "capsfilter" for candidate in other_current_descendants
        )
        if (
            current_count != 1
            or named_ingresses[0] is not ingress
            or descendants != 10
            or any(count != 1 for count in required_current_counts.values())
            or stale_required_count != 0
            or len(other_current_descendants) != 2
            or capsfilter_count != 2
            or self._method(ingress, "get_parent")() is not context.pipeline
            or set(context.audio_ingress_elements) != AUDIO_BRANCH_ELEMENT_NAMES
            or current_source is not context.audio_ingress_elements.get("audio_source")
        ):
            raise GStreamerDriverError("owned audio ingress count/descendant contract differs")
        return {
            "current_count": 1,
            "current_descendant_count": descendants,
            "stale_descendant_count": 0,
            "replacement_count": context.audio_ingress_replacement_count,
        }

    def _arm_audio_ingress_quarantine(
        self,
        context: _GenerationPipeline,
        generation: _RecordingGeneration,
    ) -> None:
        """Bind a bounded loss quarantine to one exact ingress activation."""

        self._trace_handoff(
            "audio_ingress_quarantine_arm_entered",
            active_generation_id=context.active_generation_id,
            ingress_generation=context.audio_ingress_replacement_count,
        )
        if context.audio_ingress_quarantine is not None:
            raise GStreamerDriverError("audio ingress quarantine is already armed")
        if (
            context.generations.get(1) is not generation
            or context.active_generation_id != 1
            or generation.activation_id is None
        ):
            raise GStreamerDriverError(
                "audio ingress quarantine has no exact active A/V activation"
            )
        self._trace_handoff(
            "audio_ingress_quarantine_activation_validated",
            activation_id=generation.activation_id,
            ingress_generation=context.audio_ingress_replacement_count,
        )
        self._trace_handoff(
            "audio_ingress_quarantine_measure_started",
            ingress_generation=context.audio_ingress_replacement_count,
        )
        self._measure_audio_ingress(context)
        self._trace_handoff(
            "audio_ingress_quarantine_measure_complete",
            ingress_generation=context.audio_ingress_replacement_count,
        )
        ingress = context.audio_ingress_bin
        source = context.audio_ingress_elements.get("audio_source")
        if ingress is None or source is None:
            raise GStreamerDriverError("audio ingress quarantine source ownership is absent")
        parent = self._method(source, "get_parent")()
        owned = False
        for _ in range(8):
            if parent is ingress:
                owned = True
                break
            if parent is None or parent is context.pipeline:
                break
            parent = self._method(parent, "get_parent")()
        if not owned:
            raise GStreamerDriverError("audio ingress quarantine source has foreign ancestry")
        self._trace_handoff(
            "audio_ingress_quarantine_source_validated",
            ingress_generation=context.audio_ingress_replacement_count,
            source_id=id(source),
        )
        context.audio_ingress_quarantine = _AudioIngressQuarantine(
            ingress,
            source,
            generation.activation_id,
            ingress_generation=context.audio_ingress_replacement_count,
        )
        self._trace_handoff("audio_ingress_quarantine_armed")

    def _quarantine_audio_bus_message(
        self,
        context: _GenerationPipeline,
        source: object | None,
        *,
        kind: str,
        detail: str | None = None,
    ) -> bool | None:
        """Consume only exact expected messages from the retiring ALSA source."""

        quarantine = context.audio_ingress_quarantine
        if quarantine is None:
            return None
        generation = context.generations.get(1)
        active = context.generations.get(context.active_generation_id)
        current_source = context.audio_ingress_elements.get("audio_source")
        retired_slot_recycled = bool(
            context.isolated
            and context.active_generation_id in {2, 3}
            and active is not None
            and active.linked
            and not active.has_audio
            and active.activation_id is not None
            and generation is not None
            and not generation.linked
            and generation.reusable
            and generation.activation_id is None
        )
        if (
            quarantine.ingress is not context.audio_ingress_bin
            or generation is None
            or (generation.activation_id != quarantine.activation_id and not retired_slot_recycled)
            or current_source is not quarantine.source
            or quarantine.ingress_generation != context.audio_ingress_replacement_count
        ):
            raise GStreamerDriverError("audio ingress quarantine ownership drifted")
        if source is not quarantine.source:
            return False
        if kind == "eos":
            quarantine.eos_count += 1
            if quarantine.eos_count > 1:
                raise GStreamerDriverError("retiring audio ingress EOS exceeded its bound")
            self._trace_handoff("audio_ingress_eos_quarantined")
            return True
        if (
            kind != "error"
            or detail is None
            or not all(marker in detail for marker in _EXPECTED_QUARANTINED_AUDIO_ERROR_MARKERS)
        ):
            return False
        quarantine.error_count += 1
        if quarantine.error_count > _MAX_QUARANTINED_AUDIO_ERRORS:
            raise GStreamerDriverError("retiring audio ingress error burst exceeded its bound")
        self._trace_handoff(f"audio_ingress_error_quarantined_{quarantine.error_count}")
        return True

    @staticmethod
    def _queue_pending(
        context: _GenerationPipeline,
        message: BusMessage,
    ) -> None:
        if len(context.pending_messages) >= _MAX_PENDING_GENERATION_MESSAGES:
            raise GStreamerDriverError("generation pending-message queue exceeded its bound")
        context.pending_messages.append(message)

    def _preserve_handoff_bus(
        self,
        context: _GenerationPipeline,
        pipeline: object,
        timeout_s: float,
        operation: str,
    ) -> None:
        message = self._poll_bus_native(pipeline, timeout_s)
        if message.kind is BusMessageKind.EOS:
            raise GStreamerDriverError(f"parent pipeline reached unexpected EOS during {operation}")
        if message.kind is not BusMessageKind.NONE:
            self._queue_pending(context, message)

    def _drain_handoff_fatal_bus(
        self,
        context: _GenerationPipeline,
        pipeline: object,
        operation: str,
    ) -> None:
        fatal = {
            BusMessageKind.ERROR,
            BusMessageKind.AUDIO_ERROR,
            BusMessageKind.EOS,
        }
        if any(message.kind in fatal for message in context.pending_messages):
            raise GStreamerDriverError(f"{operation} has a pending fatal bus message")
        consecutive_empty = 0
        required_empty = _MAX_QUARANTINED_AUDIO_ERRORS + 2
        for _ in range(32):
            message = self._poll_bus_native(pipeline, 0)
            if message.kind is BusMessageKind.NONE:
                consecutive_empty += 1
                if consecutive_empty >= required_empty:
                    return
                continue
            consecutive_empty = 0
            if message.kind in fatal:
                raise GStreamerDriverError(f"{operation} observed a new fatal bus message")
            self._queue_pending(context, message)
        raise GStreamerDriverError(f"{operation} bus drain exceeded its bound")

    @staticmethod
    def _consume_fragment_audio_units(
        generation: _RecordingGeneration,
        start_running_time_ns: int,
        end_running_time_ns: int,
    ) -> int:
        if generation.streaming_error is not None:
            raise GStreamerDriverError(generation.streaming_error)
        if end_running_time_ns <= start_running_time_ns:
            raise GStreamerDriverError("generation media running-time boundary is invalid")
        units = sum(
            1
            for observed in generation.audio_running_times
            if start_running_time_ns <= observed < end_running_time_ns
        )
        while (
            generation.audio_running_times
            and generation.audio_running_times[0] < end_running_time_ns
        ):
            generation.audio_running_times.popleft()
        return units

    def _poll_bus_native(self, pipeline: object, timeout_s: float) -> BusMessage:
        bus = self._method(pipeline, "get_bus")()
        if bus is None:
            raise GStreamerDriverError("pipeline has no bus")
        message_type = self._gst_member("MessageType")
        error_type = self._enum_member(message_type, "ERROR")
        eos_type = self._enum_member(message_type, "EOS")
        element_type = self._enum_member(message_type, "ELEMENT")
        qos_type = self._enum_member(message_type, "QOS")
        buffers_format = self._enum_member(self._gst_member("Format"), "BUFFERS")
        message = self._method(bus, "timed_pop_filtered")(
            self._timeout_ns(timeout_s),
            error_type | eos_type | element_type | qos_type,  # type: ignore[operator]
        )
        if message is None:
            return BusMessage(BusMessageKind.NONE)
        kind = getattr(message, "type", None)
        context = self._generation_pipelines.get(id(pipeline))
        if kind == eos_type:
            if context is not None:
                message_seqnum = -1
                with suppress(Exception):
                    message_seqnum = int(
                        cast(
                            SupportsInt,
                            self._method(message, "get_seqnum")(),
                        )
                    )
                self._trace_eos_message_source(
                    context,
                    getattr(message, "src", None),
                    message_seqnum,
                )
                quarantined = self._quarantine_audio_bus_message(
                    context,
                    getattr(message, "src", None),
                    kind="eos",
                )
                if quarantined is True:
                    return BusMessage(BusMessageKind.NONE)
            return BusMessage(BusMessageKind.EOS)
        if kind == error_type:
            parsed = self._method(message, "parse_error")()
            if isinstance(parsed, tuple) and parsed:
                error = parsed[0]
                debug = parsed[1] if len(parsed) > 1 else ""
                detail = _bounded_detail(f"{error}; debug={debug}")
                if context is not None:
                    self._trace_audio_error_source(
                        context,
                        getattr(message, "src", None),
                    )
                    quarantined = self._quarantine_audio_bus_message(
                        context,
                        getattr(message, "src", None),
                        kind="error",
                        detail=detail,
                    )
                    if quarantined is True:
                        return BusMessage(BusMessageKind.NONE)
                    if quarantined is False:
                        return BusMessage(BusMessageKind.ERROR, detail)
                source_name = self._exact_audio_message_source(
                    pipeline,
                    message,
                    context,
                )
                self._trace_handoff(
                    (
                        "audio_error_classified"
                        if source_name is not None
                        else "audio_error_unclassified"
                    ),
                    ingress_generation=(
                        -1 if context is None else context.audio_ingress_replacement_count
                    ),
                )
                return BusMessage(
                    (
                        BusMessageKind.AUDIO_ERROR
                        if source_name is not None
                        else BusMessageKind.ERROR
                    ),
                    detail,
                    source_name=source_name,
                )
            if context is not None and context.audio_ingress_quarantine is not None:
                return BusMessage(
                    BusMessageKind.ERROR,
                    "unparseable quarantined-source GStreamer error",
                )
            source_name = self._exact_audio_message_source(
                pipeline,
                message,
                context,
            )
            return BusMessage(
                (BusMessageKind.AUDIO_ERROR if source_name is not None else BusMessageKind.ERROR),
                "unparseable GStreamer error",
                source_name=source_name,
            )
        if kind == qos_type:
            self._observe_qos(pipeline, message, buffers_format)
            return BusMessage(BusMessageKind.NONE)
        if kind == element_type:
            structure = self._method(message, "get_structure")()
            if structure is None:
                return BusMessage(BusMessageKind.NONE)
            name = str(self._method(structure, "get_name")())
            if name not in {
                "splitmuxsink-fragment-opened",
                "splitmuxsink-fragment-closed",
            }:
                return BusMessage(BusMessageKind.NONE)
            location = self._structure_value(structure, "location")
            running_time = self._structure_value(structure, "running-time")
            if not isinstance(location, str):
                raise GStreamerDriverError("fragment closure location is not a string")
            try:
                running_time_ns = int(cast(SupportsInt, running_time))
            except (TypeError, ValueError) as error:
                raise GStreamerDriverError("fragment closure running time is invalid") from error
            fragment = FragmentMessage(location, running_time_ns)
            if context is not None:
                generation = next(
                    (
                        candidate
                        for candidate in context.generations.values()
                        if candidate.output is getattr(message, "src", None)
                    ),
                    None,
                )
                if generation is None:
                    raise GStreamerDriverError(
                        "generation fragment message has foreign source identity"
                    )
                ownership = context.location_generation.get(location)
                if (
                    ownership is None
                    or ownership[0] != generation.generation_id
                    or ownership[1] != generation.activation_id
                ):
                    raise GStreamerDriverError(
                        "recording slot fragment activation/source identity differs"
                    )
                if name == "splitmuxsink-fragment-opened":
                    if location in generation.opened or len(generation.opened) >= 2:
                        raise GStreamerDriverError("generation reported duplicate fragment open")
                    generation.opened[location] = running_time_ns
                    units: int | None = None
                else:
                    if location not in generation.opened or location in generation.closed:
                        raise GStreamerDriverError("generation closure has no unique matching open")
                    start_running_time = generation.opened[location]
                    units = self._consume_fragment_audio_units(
                        generation,
                        start_running_time,
                        running_time_ns,
                    )
                    generation.closed.add(location)
                audio_caps = (
                    EffectiveAudioCaps(
                        "S16LE",
                        48_000,
                        1,
                        "aac",
                        4,
                        "raw",
                        "voaacenc",
                        "aacparse",
                        128_000,
                    )
                    if generation.has_audio
                    else None
                )
                fragment = FragmentMessage(
                    location,
                    running_time_ns,
                    generation.opened.get(location),
                    FragmentMediaContract(
                        ownership[1],
                        audio_caps,
                        units,
                    ),
                )
                if name == "splitmuxsink-fragment-closed":
                    generation.last_closed_location = location
                    generation.opened.pop(location, None)
                    generation.closed.discard(location)
                    context.location_generation.pop(location, None)
            return BusMessage(
                (
                    BusMessageKind.FRAGMENT_OPENED
                    if name == "splitmuxsink-fragment-opened"
                    else BusMessageKind.FRAGMENT_FINALIZED
                ),
                fragment=fragment,
            )
        raise GStreamerDriverError("filtered bus returned an unexpected message")

    def send_eos(self, pipeline: object) -> bool:
        event_type = self._gst_member("Event")
        new_eos = cast(Callable[[], object], _dynamic_attribute(event_type, "new_eos"))
        context = self._generation_pipelines.get(id(pipeline))
        if context is not None:
            critical = self._critical_loss_shutdown_generation(context)
            if critical is not None:
                dispatch = self._start_retired_video_eos_dispatch(
                    context,
                    critical,
                    "critical-loss-retired-av-video-eos",
                )
                self._await_retirement_dispatch(
                    context,
                    dispatch,
                    time.monotonic() + 2.0,
                )
                self._trace_handoff(
                    "critical_loss_retired_video_eos_complete",
                    activation_id=cast(int, critical.activation_id),
                    slot_id=critical.generation_id,
                )
                return True
            active = self._shutdown_generation(context)
            if active is None:
                return bool(self._method(pipeline, "send_event")(new_eos()))
            if not active.linked or active.has_audio:
                raise GStreamerDriverError("video-only shutdown generation ownership drifted")
            release = Event()
            reached = Event()
            probe_completed = Event()
            video_sink = self._method(context.video_tee, "get_static_pad")("sink")
            probe_type = self._enum_member(self._gst_member("PadProbeType"), "BLOCK")
            probe_type = probe_type | self._enum_member(  # type: ignore[operator]
                self._gst_member("PadProbeType"), "BUFFER"
            )
            probe_pass = self._enum_member(self._gst_member("PadProbeReturn"), "PASS")
            probe_remove = self._enum_member(self._gst_member("PadProbeReturn"), "REMOVE")
            delta_flag = self._enum_member(self._gst_member("BufferFlags"), "DELTA_UNIT")

            def hold_terminal_idr(_pad: Any, info: Any) -> Any:
                buffer = info.get_buffer()
                if buffer is None or buffer.has_flags(delta_flag):
                    return probe_pass
                reached.set()
                release.wait(3.0)
                probe_completed.set()
                return probe_remove

            probe_id = self._method(video_sink, "add_probe")(
                probe_type,
                hold_terminal_idr,
            )
            if not probe_id:
                raise GStreamerDriverError("terminal IDR probe was refused")
            try:
                if not reached.wait(3.0):
                    raise GStreamerDriverError("terminal IDR wait timed out")
                self._set_generation_open(active, False)
                self._set_generation_linked(context, active, False)
                sink = self._method(active.video_queue, "get_static_pad")("sink")
                return bool(self._method(sink, "send_event")(new_eos()))
            finally:
                self._release_block_probe(
                    video_sink,
                    probe_id,
                    reached=reached,
                    completed=probe_completed,
                    release=release,
                    timeout_s=0.5,
                )
        return bool(self._method(pipeline, "send_event")(new_eos()))

    def _critical_loss_shutdown_generation(
        self,
        context: _GenerationPipeline,
    ) -> _RecordingGeneration | None:
        """Resolve only the exact post-cut failure shape retained for shutdown."""

        if context.routing_phase != "LOSS_CONTAINMENT_CRITICAL":
            return None
        retiring = context.generations.get(1)
        if (
            retiring is None
            or not retiring.has_audio
            or retiring.activation_id is None
            or retiring.linked
            or not bool(self._method(retiring.video_valve, "get_property")("drop"))
            or len(retiring.opened) != 1
            or not retiring.audio_eos.has_forwarded_eos()
            or retiring.audio_eos.boundary_kind() not in {"NATURAL", "GENERATION"}
            or (
                retiring.video_retirement_eos_sent
                and (
                    retiring.generation_retirement_eos_seqnum is None
                    or retiring.audio_eos.generation_snapshot()
                    not in {
                        (
                            "GENERATION",
                            1,
                            retiring.generation_retirement_eos_seqnum,
                            retiring.generation_retirement_eos_seqnum,
                            0,
                            False,
                            True,
                        ),
                        (
                            "GENERATION",
                            1,
                            retiring.generation_retirement_eos_seqnum,
                            retiring.generation_retirement_eos_seqnum,
                            1,
                            False,
                            True,
                        ),
                    }
                )
            )
            or context.active_generation_id != retiring.generation_id
        ):
            raise GStreamerDriverError(
                "critical loss shutdown has no exact retired A/V provenance"
            )
        location = next(iter(retiring.opened))
        owner = (retiring.generation_id, retiring.activation_id)
        start_running_time_ns = retiring.opened.get(location)
        if (
            isinstance(start_running_time_ns, bool)
            or not isinstance(start_running_time_ns, int)
            or start_running_time_ns < 0
            or context.location_generation != {location: owner}
        ):
            raise GStreamerDriverError(
                "critical loss shutdown location ownership differs"
            )
        linked = [
            candidate
            for candidate in context.generations.values()
            if candidate.linked
        ]
        if len(linked) != 1:
            raise GStreamerDriverError(
                "critical loss shutdown has no unique closed successor"
            )
        successor = linked[0]
        if (
            successor.generation_id not in {2, 3}
            or successor.has_audio
            or successor.activation_id is None
            or not bool(self._method(successor.video_valve, "get_property")("drop"))
            or successor.opened
            or any(
                candidate.opened
                for candidate in context.generations.values()
                if candidate is not retiring
            )
        ):
            raise GStreamerDriverError(
                "critical loss shutdown successor ownership differs"
            )
        return retiring

    def _shutdown_generation(
        self,
        context: _GenerationPipeline,
    ) -> _RecordingGeneration | None:
        """Resolve shutdown from actual links/gates, including partial handoff."""

        routed = [
            candidate
            for candidate in context.generations.values()
            if candidate.linked
            and not bool(self._method(candidate.video_valve, "get_property")("drop"))
        ]
        if len(routed) != 1:
            raise GStreamerDriverError(
                "generation shutdown has no unique authoritative active route"
            )
        active = routed[0]
        context.active_generation_id = active.generation_id
        if not active.has_audio:
            context.routing_phase = "VIDEO_ONLY_CLOSING"
            return active
        initial = context.generations[1]
        if active is initial:
            context.routing_phase = "AV_ACTIVE"
            return None
        raise GStreamerDriverError("generation shutdown has no authoritative active route")

    @staticmethod
    def _allocate_slot_activation(
        context: _GenerationPipeline,
        generation: _RecordingGeneration,
    ) -> int:
        if generation.activation_id is not None:
            raise GStreamerDriverError("recording slot already has an activation")
        activation_id = context.next_activation_id
        if not 1 <= activation_id <= 2**31 - 1:
            raise GStreamerDriverError("recording activation ID space is exhausted")
        generation.activation_id = activation_id
        context.next_activation_id += 1
        if generation.generation_id in {2, 3}:
            context.next_video_slot_id = 5 - generation.generation_id
        return activation_id

    @staticmethod
    def _select_video_successor(
        context: _GenerationPipeline,
    ) -> _RecordingGeneration:
        eligible = [
            candidate
            for slot_id, candidate in sorted(context.generations.items())
            if slot_id in {2, 3}
            and candidate.reusable
            and not candidate.retired
            and not candidate.linked
            and candidate.activation_id is None
        ]
        selected = next(
            (
                candidate
                for candidate in eligible
                if candidate.generation_id == context.next_video_slot_id
            ),
            eligible[0] if eligible else None,
        )
        if selected is None:
            raise GStreamerDriverError("no prepared video-only fallback slot is reusable")
        return selected

    @staticmethod
    def _commit_active_route(
        context: _GenerationPipeline,
        generation: _RecordingGeneration,
    ) -> None:
        if generation.activation_id is None:
            raise GStreamerDriverError("active route has no activation identity")
        context.active_generation_id = generation.generation_id

    def _recycle_generation(
        self,
        generation: _RecordingGeneration,
        timeout_s: float,
    ) -> None:
        """Reset one exactly closed slot without releasing any request pad."""

        if (
            generation.linked
            or generation.opened
            or generation.closed
            or generation.last_closed_location is None
            or generation.activation_id is None
        ):
            raise GStreamerDriverError(
                "recording slot is not exactly closed and eligible for recycling"
            )
        if (
            generation.video_tee_pad_released
            or generation.output_video_pad_released
            or (
                generation.has_audio
                and (generation.audio_tee_pad_released or generation.output_audio_pad_released)
            )
        ):
            raise GStreamerDriverError(
                "recording slot request-pad ownership changed before recycling"
            )
        if not self._method(generation.bin, "set_locked_state")(True):
            raise GStreamerDriverError("retired recording slot could not lock")
        self._set_and_verify_state(generation.bin, "NULL", timeout_s)
        generation.retired = False
        generation.reusable = True
        generation.activation_id = None
        generation.audio_units = 0
        generation.audio_running_times.clear()
        generation.last_audio_end_running_time_ns = None
        generation.streaming_error = None
        generation.last_closed_location = None
        generation.first_video_seen = Event()
        generation.first_video_is_idr = None
        generation.first_video_had_sticky_contract = None
        generation.video_units = 0
        generation.audio_eos = _AudioEosArbiter()
        generation.video_retirement_eos_sent = False
        generation.generation_retirement_eos_seqnum = None

    def _reset_unrouted_generation(
        self,
        context: _GenerationPipeline,
        generation: _RecordingGeneration,
        *,
        next_video_slot_id: int,
        timeout_s: float,
    ) -> None:
        """Return a prewarmed but never-routed slot to bounded standby."""

        if generation.linked or generation.opened or generation.last_closed_location is not None:
            raise GStreamerDriverError("unrouted successor acquired media ownership")
        if not self._method(generation.bin, "set_locked_state")(True):
            raise GStreamerDriverError("unrouted successor could not relock")
        self._set_and_verify_state(generation.bin, "NULL", timeout_s)
        generation.activation_id = None
        generation.reusable = True
        generation.retired = False
        generation.first_video_seen = Event()
        generation.first_video_is_idr = None
        generation.first_video_had_sticky_contract = None
        generation.video_units = 0
        generation.audio_units = 0
        generation.audio_running_times.clear()
        generation.last_audio_end_running_time_ns = None
        generation.streaming_error = None
        context.next_video_slot_id = next_video_slot_id
        context.loss_verified = False
        context.routing_phase = "AV_ACTIVE"

    def _capture_audio_ingress_elements(
        self,
        ingress: object,
    ) -> Mapping[str, Any]:
        """Retain one exact wrapper for every named element in an ingress."""

        descendants = self._iterate_bounded(
            self._method(ingress, "iterate_recurse")(),
            label="new audio ingress descendant",
            maximum=16,
        )
        retained: dict[str, Any] = {}
        for candidate in descendants:
            name = str(self._method(candidate, "get_name")())
            if name not in AUDIO_BRANCH_ELEMENT_NAMES:
                continue
            if name in retained:
                raise GStreamerDriverError("replacement audio ingress has duplicate named elements")
            parent = self._method(candidate, "get_parent")()
            owned = False
            for _ in range(8):
                if parent is ingress:
                    owned = True
                    break
                if parent is None:
                    break
                parent = self._method(parent, "get_parent")()
            if not owned:
                raise GStreamerDriverError(
                    "replacement audio ingress named element has foreign ancestry"
                )
            retained[name] = candidate
        if set(retained) != AUDIO_BRANCH_ELEMENT_NAMES:
            raise GStreamerDriverError("replacement audio ingress named element set is incomplete")
        return MappingProxyType(retained)

    def _install_audio_ingress(
        self,
        context: _GenerationPipeline,
        plan: AudioCapturePlan,
        *,
        synchronize: bool,
        bind_metrics: bool,
    ) -> None:
        """Install exactly one owned ALSA/AAC bin and bind its sole output."""

        if context.audio_ingress_bin is not None or context.audio_ingress_elements:
            raise GStreamerDriverError("audio ingress ownership is already populated")
        parse_bin = cast(
            Callable[[str, bool], Any],
            _dynamic_attribute(self._gst, "parse_bin_from_description"),
        )
        ingress = parse_bin(build_audio_ingress_description(plan), True)
        self._method(ingress, "set_name")("audio_ingress")
        elements = self._capture_audio_ingress_elements(ingress)
        source = elements["audio_source"]
        if source is None or self._method(source, "get_property")("device") != plan.endpoint:
            raise GStreamerDriverError("replacement audio ingress endpoint identity is not exact")
        if not self._method(ingress, "set_locked_state")(True):
            raise GStreamerDriverError("replacement audio ingress could not lock")
        add_result = self._method(context.pipeline, "add")(ingress)
        if add_result is False or self._method(ingress, "get_parent")() is not context.pipeline:
            raise GStreamerDriverError("replacement audio ingress could not join pipeline")
        # Bind ownership immediately so a later preparation failure can remove
        # and retry this incomplete ingress rather than leaking a child.
        context.audio_ingress_bin, context.audio_ingress_elements = (
            ingress,
            elements,
        )
        ingress_src = self._method(ingress, "get_static_pad")("src")
        tee_sink = self._method(context.audio_tee, "get_static_pad")("sink")
        link_ok = self._enum_member(self._gst_member("PadLinkReturn"), "OK")
        if (
            ingress_src is None
            or tee_sink is None
            or self._method(ingress_src, "link")(tee_sink) != link_ok
        ):
            raise GStreamerDriverError("replacement audio ingress could not link")
        if not self._method(ingress, "set_locked_state")(False):
            raise GStreamerDriverError("replacement audio ingress could not unlock")
        if synchronize and not self._method(ingress, "sync_state_with_parent")():
            raise GStreamerDriverError("replacement audio ingress could not synchronize")
        if bind_metrics:
            tracked_metrics = self._metrics.get(id(context.pipeline))
            if tracked_metrics is None:
                raise GStreamerDriverError(
                    "replacement audio ingress lost the bound recorder metrics"
                )
            self.install_audio_metrics(context.pipeline, tracked_metrics[0])
        if synchronize:
            context.audio_ingress_replacement_count += 1

    def _rebuild_audio_ingress(
        self,
        context: _GenerationPipeline,
        plan: AudioCapturePlan,
        timeout_s: float,
    ) -> None:
        """Replace exactly one owned ingress while camera/encoder keep PLAYING."""

        ingress = context.audio_ingress_bin
        elements = context.audio_ingress_elements
        source = elements.get("audio_source")
        if ingress is None or source is None or set(elements) != AUDIO_BRANCH_ELEMENT_NAMES:
            raise GStreamerDriverError("audio ingress ownership is absent")
        before_remove = self._iterate_bounded(
            self._method(context.pipeline, "iterate_recurse")(),
            label="pre-removal pipeline descendant",
            maximum=96,
        )
        retired_objects = [ingress]
        for candidate in before_remove:
            parent = self._method(candidate, "get_parent")()
            for _ in range(8):
                if parent is ingress:
                    retired_objects.append(candidate)
                    break
                if parent is None or parent is context.pipeline:
                    break
                parent = self._method(parent, "get_parent")()
        if (
            len(retired_objects) != 11
            or len({id(value) for value in retired_objects}) != 11
            or any(
                all(value is not element for value in retired_objects)
                for element in elements.values()
            )
        ):
            raise GStreamerDriverError(
                "retired audio ingress object ownership differs before removal"
            )
        self._set_and_verify_state(ingress, "NULL", timeout_s)
        ingress_src = self._method(ingress, "get_static_pad")("src")
        tee_sink = self._method(context.audio_tee, "get_static_pad")("sink")
        if (
            ingress_src is None
            or tee_sink is None
            or self._method(ingress_src, "get_peer")() is not tee_sink
            or not self._method(ingress_src, "unlink")(tee_sink)
        ):
            raise GStreamerDriverError("retired audio ingress exact tee link could not be removed")
        if not self._method(context.pipeline, "remove")(ingress):
            raise GStreamerDriverError("retired audio ingress could not be removed")
        after_remove = self._iterate_bounded(
            self._method(context.pipeline, "iterate_recurse")(),
            label="post-removal pipeline descendant",
            maximum=96,
        )
        retired_identities = {id(value) for value in retired_objects}
        if any(id(value) in retired_identities for value in after_remove):
            raise GStreamerDriverError(
                "retired audio ingress or descendant remained in the pipeline"
            )
        quarantine = context.audio_ingress_quarantine
        if quarantine is not None:
            if (
                quarantine.ingress is not ingress
                or quarantine.source is not source
                or quarantine.ingress_generation != context.audio_ingress_replacement_count
            ):
                raise GStreamerDriverError("retired audio ingress quarantine identity differs")
            context.audio_ingress_quarantine = None
        context.audio_ingress_bin, context.audio_ingress_elements = (
            None,
            MappingProxyType({}),
        )
        self._install_audio_ingress(
            context,
            plan,
            synchronize=True,
            bind_metrics=True,
        )

    def restore_audio(
        self,
        pipeline: object,
        plan: AudioCapturePlan,
        timeout_s: float,
    ) -> AudioRestoreHandoff:
        """Re-prime audio and hand off from video-only to the fixed A/V slot."""

        context = self._generation_pipelines.get(id(pipeline))
        if context is None:
            raise GStreamerDriverError("pipeline has no immutable generation context")
        if not isinstance(plan, AudioCapturePlan) or not 0 < timeout_s <= 30:
            raise GStreamerDriverError("audio restoration request is invalid")
        if not context.handoff_lock.acquire(timeout=timeout_s):
            raise GStreamerDriverError("audio restoration serialization timed out")
        deadline = time.monotonic() + timeout_s
        release = Event()
        reached = Event()
        completed = Event()
        held: dict[str, int] = {}
        video_sink: Any | None = None
        probe_id: Any | None = None
        successor: _RecordingGeneration | None = None
        restoration_provenance: _RestorationParentFailureProvenance | None = None
        phase = "pre_route"
        transition_published = False

        def preserve_bus(timeout: float) -> None:
            self._preserve_handoff_bus(
                context,
                pipeline,
                timeout,
                "audio restoration",
            )

        try:
            if (
                not context.isolated
                or context.active_generation_id not in {2, 3}
                or context.audio_ingress_quarantine is None
            ):
                raise GStreamerDriverError(
                    "audio restoration has no quarantined active video-only slot"
                )
            retiring = context.generations[context.active_generation_id]
            successor = context.generations[1]
            if not retiring.linked or retiring.has_audio or successor.linked:
                raise GStreamerDriverError("audio restoration slot ownership drifted")
            retiring_activation = retiring.activation_id
            if retiring_activation is None:
                raise GStreamerDriverError("active video-only slot has no activation identity")
            restoration_provenance = self._capture_restoration_parent_failure_provenance(
                context,
                retiring,
                successor,
            )
            self._publish_topology_transition(
                context,
                "handoff_in_progress",
                phase="audio_restoration",
            )
            transition_published = True
            if successor.retired:
                self._recycle_generation(
                    successor,
                    min(max(deadline - time.monotonic(), 0.1), 3.0),
                )
            elif not (
                successor.reusable and successor.activation_id is None and not successor.opened
            ):
                raise GStreamerDriverError(
                    "A/V slot is neither retired nor safely prepared for retry"
                )
            self._rebuild_audio_ingress(
                context,
                plan,
                min(max(deadline - time.monotonic(), 0.1), 3.0),
            )
            restored_activation = self._allocate_slot_activation(context, successor)
            successor.reusable = False
            self._prewarm_generation(context, successor)

            video_sink = self._method(context.video_tee, "get_static_pad")("sink")
            probe_type = self._enum_member(self._gst_member("PadProbeType"), "BLOCK")
            probe_type = probe_type | self._enum_member(  # type: ignore[operator]
                self._gst_member("PadProbeType"), "BUFFER"
            )
            probe_pass = self._enum_member(self._gst_member("PadProbeReturn"), "PASS")
            probe_remove = self._enum_member(self._gst_member("PadProbeReturn"), "REMOVE")
            delta_flag = self._enum_member(self._gst_member("BufferFlags"), "DELTA_UNIT")

            def hold_idr(_pad: Any, info: Any) -> Any:
                buffer = info.get_buffer()
                if buffer is None or buffer.has_flags(delta_flag):
                    return probe_pass
                pts = int(buffer.pts)
                if pts < 0:
                    return probe_pass
                held["running_time_ns"] = pts
                held["entered_monotonic_ns"] = time.monotonic_ns()
                reached.set()
                release.wait(timeout_s)
                released_ns = time.monotonic_ns()
                self._trace_handoff(
                    "restoration_idr_callback_released",
                    held_ns=max(
                        released_ns - held["entered_monotonic_ns"],
                        0,
                    ),
                )
                completed.set()
                return probe_remove

            probe_id = self._method(video_sink, "add_probe")(probe_type, hold_idr)
            if not probe_id:
                raise GStreamerDriverError("restoration IDR probe was refused")
            while not reached.is_set():
                if time.monotonic() >= deadline:
                    raise GStreamerDriverError("restoration IDR wait timed out")
                preserve_bus(min(0.05, max(deadline - time.monotonic(), 0.001)))
            active_locations = set(retiring.opened)
            if len(active_locations) != 1:
                raise GStreamerDriverError("retiring video-only slot has no unique active fragment")
            active_location = next(iter(active_locations))
            self._set_generation_linked(context, successor, True)
            # From the first mutation of the authoritative video-only route,
            # failure is a critical recorder fault unless a future target-
            # proven rollback is added.
            phase = "routed"
            self._set_generation_open(retiring, False)
            self._set_generation_linked(context, retiring, False)
            self._set_generation_open(successor, True)
            self._commit_active_route(context, successor)
            context.routing_phase = "AV_RESTORING"
            release_started_ns = time.monotonic_ns()
            release.set()
            self._trace_handoff(
                "restoration_successor_released",
                held_ns=max(
                    release_started_ns - held["entered_monotonic_ns"],
                    0,
                ),
            )
            phase = "media_proof"
            while (
                not successor.first_video_seen.is_set()
                or successor.audio_units < 1
                or len(successor.opened) != 1
            ):
                if time.monotonic() >= deadline:
                    raise GStreamerDriverError("restored A/V slot did not produce video and audio")
                preserve_bus(min(0.05, max(deadline - time.monotonic(), 0.001)))
            first_video_count = successor.video_units
            phase = "state_convergence"
            if not self._generation_playing_converged(
                context,
                successor,
                max(deadline - time.monotonic(), 0),
                restoration_provenance,
            ):
                raise GStreamerDriverError("restored A/V slot state did not converge")
            dispatch = self._start_retired_video_eos_dispatch(
                context,
                retiring,
                "retiring-video-only-eos",
            )
            self._trace_handoff("restoration_video_eos_dispatched")
            phase = "retiring_eos"
            self._await_retirement_dispatch(
                context,
                dispatch,
                min(deadline, time.monotonic() + 2.0),
            )
            phase = "retired_closure"
            while retiring.last_closed_location != active_location:
                if time.monotonic() >= deadline:
                    raise GStreamerDriverError("retiring video-only fragment closure timed out")
                preserve_bus(min(0.05, max(deadline - time.monotonic(), 0.001)))
            self._prove_retirement_has_no_successor_fragment(
                context,
                retiring,
                preserve_bus,
                deadline,
            )
            phase = "continuity"
            while successor.video_units < first_video_count + 30:
                if time.monotonic() >= deadline:
                    raise GStreamerDriverError(
                        "restored A/V slot did not sustain 30 additional buffers"
                    )
                preserve_bus(min(0.05, max(deadline - time.monotonic(), 0.001)))
            retiring.retired = True
            retiring.reusable = False
            phase = "recycle"
            self._trace_handoff("restoration_recycle_started")
            self._recycle_generation(
                retiring,
                min(max(deadline - time.monotonic(), 0.1), 3.0),
            )
            self._trace_handoff("restoration_recycle_complete")
            phase = "identity"
            if (
                successor.first_video_is_idr is not True
                or successor.first_video_had_sticky_contract is not True
                or self._method(pipeline, "get_by_name")("camera") is not context.initial_camera
                or self._method(pipeline, "get_by_name")("encoder") is not context.initial_encoder
                or len(context.generations) != 3
            ):
                raise GStreamerDriverError("audio restoration continuity/resource contract failed")
            context.isolated = False
            context.routing_phase = "AV_ACTIVE"
            topology_proof = self._measure_generation_topology(context)
            if (
                topology_proof.get("request_pad_counts_measured") is not True
                or topology_proof.get("request_pad_peer_ownership_proven") is not True
            ):
                raise GStreamerDriverError("audio restoration measured topology proof is absent")
            self._publish_stable_topology(context, topology_proof)
            return AudioRestoreHandoff(
                retiring_activation,
                restored_activation,
                held["running_time_ns"],
                retiring.generation_id,
                successor.generation_id,
                True,
                True,
                True,
                True,
                successor.video_units,
                successor.audio_units,
                True,
                3,
            )
        except Exception as error:
            classified = self._classify_restoration_failure(phase, error)
            if classified is error and successor is not None and successor.opened:
                classified = AudioRestorationCriticalError(
                    "audio restoration opened its A/V slot before the route "
                    f"boundary could be proven: {_bounded_detail(error)}",
                    phase="pre_route_opened",
                )
            if classified is not error:
                context.routing_phase = "RESTORATION_CRITICAL"
                if transition_published:
                    self._publish_topology_transition(
                        context,
                        "faulted_handoff",
                        phase=phase,
                    )
                raise classified from error
            if transition_published:
                self._publish_topology_transition(
                    context,
                    "faulted_handoff",
                    phase=phase,
                )
            raise
        finally:
            cleanup_error: BaseException | None = None
            try:
                if video_sink is not None and probe_id:
                    self._release_block_probe(
                        video_sink,
                        probe_id,
                        reached=reached,
                        completed=completed,
                        release=release,
                        timeout_s=min(max(deadline - time.monotonic(), 0.1), 0.5),
                    )
                else:
                    release.set()
                if (
                    successor is not None
                    and context.active_generation_id != 1
                    and not successor.opened
                    and successor.activation_id is not None
                ):
                    # Preparation failed before routing.  Retain the currently
                    # active video-only slot and make the A/V slot retryable.
                    self._set_generation_open(successor, False)
                    if successor.linked:
                        self._set_generation_linked(context, successor, False)
                    self._method(successor.bin, "set_locked_state")(True)
                    self._set_and_verify_state(successor.bin, "NULL", 0.5)
                    successor.activation_id = None
                    successor.reusable = True
                    successor.first_video_seen = Event()
                    successor.first_video_is_idr = None
                    successor.first_video_had_sticky_contract = None
                    successor.video_units = 0
                    successor.audio_units = 0
            except BaseException as error:
                cleanup_error = error
            finally:
                context.handoff_lock.release()
            if cleanup_error is not None:
                context.routing_phase = "RESTORATION_CRITICAL"
                if transition_published:
                    self._publish_topology_transition(
                        context,
                        "faulted_handoff",
                        phase="cleanup",
                    )
                raise AudioRestorationCriticalError(
                    "audio restoration cleanup could not prove a safe video-only route: "
                    f"{_bounded_detail(cleanup_error)}",
                    phase="cleanup",
                ) from cleanup_error

    def arm_audio_loss(
        self,
        pipeline: object,
        source_name: str,
    ) -> AudioLossArmProof:
        """Arm one exact active ingress before loss confirmation can yield."""

        context = self._generation_pipelines.get(id(pipeline))
        self._trace_handoff(
            "audio_loss_arm_entered",
            active_generation_id=(-1 if context is None else context.active_generation_id),
            ingress_generation=(-1 if context is None else context.audio_ingress_replacement_count),
        )
        try:
            if context is None or source_name != "audio_source":
                raise GStreamerDriverError("audio-loss containment arm lacks its exact source")
            self._trace_handoff(
                "audio_loss_arm_source_validated",
                ingress_generation=context.audio_ingress_replacement_count,
            )
            if context.isolated or context.active_generation_id != 1:
                raise GStreamerDriverError("audio-loss containment arm has no active A/V route")
            self._trace_handoff(
                "audio_loss_arm_route_validated",
                ingress_generation=context.audio_ingress_replacement_count,
            )
            generation = context.generations.get(1)
            if generation is None or generation.activation_id is None or not generation.linked:
                raise GStreamerDriverError(
                    "audio-loss containment arm activation ownership differs"
                )
            self._trace_handoff(
                "audio_loss_arm_activation_validated",
                activation_id=generation.activation_id,
                ingress_generation=context.audio_ingress_replacement_count,
            )
            self._trace_audio_eos_arm_state(
                "audio_loss_arm_eos_pre",
                generation.audio_eos,
            )
            generation.audio_eos.arm_retirement()
            self._trace_audio_eos_arm_state(
                "audio_loss_arm_eos_post",
                generation.audio_eos,
            )
            self._trace_handoff(
                "audio_loss_arm_eos_armed",
                activation_id=generation.activation_id,
                ingress_generation=context.audio_ingress_replacement_count,
            )
            self._arm_audio_ingress_quarantine(context, generation)
            self._trace_handoff(
                "audio_loss_arm_complete",
                activation_id=generation.activation_id,
                ingress_generation=context.audio_ingress_replacement_count,
            )
            return AudioLossArmProof(
                generation.activation_id,
                generation.generation_id,
                source_name,
            )
        except BaseException as error:
            self._trace_handoff_failure("audio_loss_arm_failed", error)
            raise

    @staticmethod
    def _contains_h264_idr_bytes(payload: bytes) -> bool:
        """Recognize NAL type 5 in Annex-B or four-byte AVC access units."""

        for marker in (b"\x00\x00\x01", b"\x00\x00\x00\x01"):
            start = 0
            while (index := payload.find(marker, start)) >= 0:
                position = index + len(marker)
                if position < len(payload) and payload[position] & 0x1F == 5:
                    return True
                start = position
        offset = 0
        while offset + 4 <= len(payload):
            size = int.from_bytes(payload[offset : offset + 4], "big")
            offset += 4
            if size <= 0 or offset + size > len(payload):
                return False
            if payload[offset] & 0x1F == 5:
                return True
            offset += size
        return False

    def _arm_forced_idr_gate(
        self,
        context: _GenerationPipeline,
        generation: _RecordingGeneration,
        *,
        deadline: float,
        timeout_s: float,
        await_response: bool = True,
    ) -> _ForcedIdrGate:
        """Arm one exact forced NAL5 request, optionally awaiting its held IDR."""

        if self._gstvideo is None:
            raise GStreamerDriverError("GstVideo forced-key capability is unavailable")
        pipeline = context.pipeline
        encoder = self._method(pipeline, "get_by_name")("encoder")
        parser = self._method(pipeline, "get_by_name")("parser")
        if (
            encoder is not context.encoder
            or encoder is not context.initial_encoder
            or parser is None
        ):
            raise GStreamerDriverError("forced-IDR element ownership differs")
        encoder_source = self._method(encoder, "get_static_pad")("src")
        parser_source = self._method(parser, "get_static_pad")("src")
        gate_queue = generation.video_gate_queue
        video_valve_sink = self._method(generation.video_valve, "get_static_pad")("sink")
        video_sink = (
            None
            if gate_queue is None
            else self._method(gate_queue, "get_static_pad")("src")
        )
        if (
            encoder_source is None
            or parser_source is None
            or gate_queue is None
            or video_sink is None
            or video_valve_sink is None
        ):
            raise GStreamerDriverError("forced-IDR exact source/output pad is absent")
        if (
            context.generations.get(generation.generation_id) is not generation
            or generation.activation_id is None
            or not generation.linked
            or generation.has_audio
            or bool(self._method(generation.video_valve, "get_property")("drop"))
            is not True
        ):
            raise GStreamerDriverError("forced-IDR successor ownership differs")
        if (
            self._method(encoder_source, "get_parent")() is not encoder
            or self._method(parser_source, "get_parent")() is not parser
            or self._method(encoder, "get_parent")() is not pipeline
            or self._method(parser, "get_parent")() is not pipeline
            or self._method(gate_queue, "get_parent")() is not generation.bin
            or self._method(video_sink, "get_parent")() is not gate_queue
            or self._method(video_sink, "get_peer")() is not video_valve_sink
            or self._method(video_valve_sink, "get_peer")() is not video_sink
        ):
            raise GStreamerDriverError("forced-IDR pad ancestry differs")
        count = context.next_force_key_count
        if not 1 <= count <= 2**32 - 1:
            raise GStreamerDriverError("forced-IDR request count space is exhausted")
        context.next_force_key_count += 1
        release = Event()
        reached = Event()
        completed = Event()
        failed = Event()
        observed: dict[str, int | bool | str] = {"foreign_events": 0}
        event_probe_id: Any | None = None
        video_probe_id: Any | None = None
        dispatch: _BoundedEventDispatch | None = None
        request_monotonic_ns = time.monotonic_ns()
        probe_ok = self._enum_member(self._gst_member("PadProbeReturn"), "OK")
        probe_remove = self._enum_member(self._gst_member("PadProbeReturn"), "REMOVE")
        event_probe_type = self._enum_member(
            self._gst_member("PadProbeType"),
            "EVENT_DOWNSTREAM",
        )
        # A BLOCK probe deadlocks this target in either placement: before the
        # request it can own the encoder stream lock before send_event, while
        # installing one from the correlated event callback blocks that
        # callback before the event can finish propagating.  A plain DATA
        # observer returns immediately for every unrelated buffer.  Its
        # callback holds only the exact correlated NAL5 by waiting on
        # ``release`` before returning REMOVE.
        buffer_probe_type = self._enum_member(self._gst_member("PadProbeType"), "BUFFER")
        delta_flag = self._enum_member(self._gst_member("BufferFlags"), "DELTA_UNIT")

        def fail(detail: str) -> None:
            observed.setdefault("failure", detail)
            failed.set()

        def observe_force_key(_pad: Any, info: Any) -> Any:
            try:
                event = info.get_event()
                if event is None:
                    return probe_ok
                is_force_key = cast(
                    Callable[[object], bool],
                    _dynamic_attribute(self._gstvideo, "video_event_is_force_key_unit"),
                )
                if not bool(is_force_key(event)):
                    return probe_ok
                parse = cast(
                    Callable[[object], object],
                    _dynamic_attribute(
                        self._gstvideo,
                        "video_event_parse_downstream_force_key_unit",
                    ),
                )
                parsed = parse(event)
                if not isinstance(parsed, tuple) or len(parsed) != 6 or parsed[0] is not True:
                    fail("downstream forced-IDR event shape is invalid")
                    return probe_ok
                _, _timestamp, _stream_time, running_time, all_headers, event_count = parsed
                if (
                    isinstance(event_count, bool)
                    or not isinstance(event_count, int)
                    or not 0 <= event_count <= 2**32 - 1
                    or isinstance(running_time, bool)
                    or not isinstance(running_time, int)
                    or running_time < 0
                    or running_time == 2**64 - 1
                    or not isinstance(all_headers, bool)
                ):
                    fail("downstream forced-IDR event values are invalid")
                    return probe_ok
                if event_count != count:
                    foreign = int(cast(int, observed["foreign_events"])) + 1
                    observed["foreign_events"] = foreign
                    if foreign > _MAX_FOREIGN_FORCE_KEY_EVENTS:
                        fail("foreign downstream force-key events exceeded their bound")
                    return probe_ok
                if "downstream_seqnum" in observed:
                    fail("duplicate correlated downstream force-key event")
                    return probe_ok
                downstream_seqnum = event.get_seqnum()
                if (
                    isinstance(downstream_seqnum, bool)
                    or not isinstance(downstream_seqnum, int)
                    or not 0 <= downstream_seqnum <= 2**32 - 1
                ):
                    fail("downstream forced-IDR seqnum is invalid")
                    return probe_remove
                observed["downstream_seqnum"] = downstream_seqnum
                observed["downstream_running_time_ns"] = running_time
                observed["downstream_event_monotonic_ns"] = time.monotonic_ns()
                observed["all_headers"] = all_headers
                observed["event_probe_removed"] = True
                if all_headers is not True:
                    fail("correlated downstream force-key event omitted all headers")
                    return probe_remove
                return probe_remove
            except BaseException as error:
                fail(f"downstream forced-IDR observation failed: {_bounded_detail(error)}")
            return probe_ok

        def hold_idr(_pad: Any, info: Any) -> Any:
            try:
                buffer = info.get_buffer()
                if (
                    buffer is None
                    or buffer.has_flags(delta_flag)
                    or "downstream_seqnum" not in observed
                ):
                    return probe_ok
                pts = int(buffer.pts)
                clock_none = int(cast(SupportsInt, self._gst_member("CLOCK_TIME_NONE")))
                if pts < 0 or pts == clock_none:
                    fail("forced-IDR PTS is invalid")
                    return probe_ok
                segment_event = self._method(_pad, "get_sticky_event")(
                    self._enum_member(self._gst_member("EventType"), "SEGMENT"),
                    0,
                )
                segment = (
                    None
                    if segment_event is None
                    else self._method(segment_event, "parse_segment")()
                )
                running_time = (
                    -1
                    if segment is None
                    else int(
                        cast(
                            SupportsInt,
                            self._method(segment, "to_running_time")(
                                self._enum_member(self._gst_member("Format"), "TIME"),
                                pts,
                            ),
                        )
                    )
                )
                if running_time < 0 or running_time == clock_none:
                    fail("forced-IDR running time is invalid")
                    return probe_ok
                mapped, map_info = buffer.map(
                    self._enum_member(self._gst_member("MapFlags"), "READ")
                )
                if not mapped:
                    fail("forced-IDR access unit could not be mapped")
                    return probe_ok
                try:
                    nal5 = self._contains_h264_idr_bytes(bytes(map_info.data))
                finally:
                    buffer.unmap(map_info)
                if not nal5:
                    fail("correlated non-delta access unit lacks NAL type 5")
                    return probe_ok
                downstream_running_time = int(
                    cast(int, observed["downstream_running_time_ns"])
                )
                event_to_idr_media = running_time - downstream_running_time
                if event_to_idr_media < 0:
                    fail("forced-IDR precedes its correlated downstream event in media time")
                    return probe_ok
                observed["forced_idr_running_time_ns"] = running_time
                observed["event_to_idr_media_ns"] = event_to_idr_media
                observed["idr_arrival_monotonic_ns"] = time.monotonic_ns()
                reached.set()
                if not release.wait(timeout_s):
                    fail("held forced-IDR release wait timed out")
                completed.set()
                return probe_remove
            except BaseException as error:
                fail(f"forced-IDR buffer observation failed: {_bounded_detail(error)}")
                return probe_ok

        try:
            # On the exact production graph h264parse consumes the encoder's
            # downstream force-key response: it is observable at encoder.src
            # and parser.sink, but not parser.src.  Observe the authoritative
            # encoder-produced edge rather than depending on parser forwarding.
            event_probe_id = self._method(encoder_source, "add_probe")(
                event_probe_type,
                observe_force_key,
            )
            if not event_probe_id:
                raise GStreamerDriverError("forced-IDR event probe was refused")
            self._trace_handoff("forced_idr_event_probe_armed", request_count=count)
            video_probe_id = self._method(video_sink, "add_probe")(
                buffer_probe_type,
                hold_idr,
            )
            if not video_probe_id:
                raise GStreamerDriverError("forced-IDR buffer observer was refused")
            self._trace_handoff(
                "forced_idr_buffer_observer_armed",
                request_count=count,
            )
            new_force_key = cast(
                Callable[[int, bool, int], object],
                _dynamic_attribute(
                    self._gstvideo,
                    "video_event_new_upstream_force_key_unit",
                ),
            )
            event = new_force_key(
                int(cast(SupportsInt, self._gst_member("CLOCK_TIME_NONE"))),
                True,
                count,
            )
            if event is None:
                raise GStreamerDriverError("GstVideo did not create forced-IDR request")
            request_seqnum_raw = self._method(event, "get_seqnum")()
            if (
                isinstance(request_seqnum_raw, bool)
                or not isinstance(request_seqnum_raw, int)
                or not 0 <= request_seqnum_raw <= 2**32 - 1
            ):
                raise GStreamerDriverError("forced-IDR request seqnum is invalid")
            request_seqnum = request_seqnum_raw
            request_monotonic_ns = time.monotonic_ns()
            dispatch = self._send_force_key_synchronously(
                context,
                generation,
                encoder_source,
                event,
            )
            self._trace_handoff(
                "forced_idr_request_dispatched_synchronously",
                request_count=count,
            )
            gate = _ForcedIdrGate(
                video_sink,
                video_probe_id,
                encoder_source,
                (
                    None
                    if observed.get("event_probe_removed") is True
                    else event_probe_id
                ),
                release,
                reached,
                completed,
                None,
                dispatch,
                observed,
                failed,
                count,
                request_seqnum,
                request_monotonic_ns,
            )
            if await_response:
                self._await_forced_idr_gate(
                    context,
                    generation,
                    gate,
                    deadline,
                )
            return gate
        except BaseException as primary:
            cleanup_errors: list[str] = []
            release.set()
            if event_probe_id and observed.get("event_probe_removed") is not True:
                try:
                    self._remove_retained_probe(
                        encoder_source,
                        event_probe_id,
                        timeout_s=0.5,
                    )
                except BaseException as error:
                    cleanup_errors.append(f"event probe: {_bounded_detail(error)}")
            if video_probe_id:
                try:
                    self._release_block_probe(
                        video_sink,
                        video_probe_id,
                        reached=reached,
                        completed=completed,
                        release=release,
                        timeout_s=0.5,
                    )
                except BaseException as error:
                    cleanup_errors.append(f"buffer probe: {_bounded_detail(error)}")
            if dispatch is not None:
                if dispatch.thread is not None:
                    dispatch.thread.join(timeout=0)
                if not dispatch.done.is_set() or (
                    dispatch.thread is not None and dispatch.thread.is_alive()
                ):
                    cleanup_errors.append("force-key dispatch remains alive")
            if cleanup_errors:
                context.routing_phase = "LOSS_CONTAINMENT_CRITICAL"
                raise GStreamerDriverError(
                    "forced-IDR failure cleanup is incomplete: "
                    + "; ".join(cleanup_errors)
                    + f"; primary={_bounded_detail(primary)}"
                ) from primary
            raise

    def _await_forced_idr_gate(
        self,
        context: _GenerationPipeline,
        generation: _RecordingGeneration,
        gate: _ForcedIdrGate,
        deadline: float,
    ) -> None:
        """Finish correlation after any old route blocking common video is gone."""

        if gate.held is not None:
            raise GStreamerDriverError("forced-IDR gate was already awaited")
        self._await_force_key_dispatch(
            context,
            generation,
            gate.dispatch,
            deadline,
        )
        observed = gate.observed
        if gate.failed.is_set():
            raise GStreamerDriverError(str(observed["failure"]))
        while not gate.reached.is_set():
            if gate.failed.is_set():
                raise GStreamerDriverError(str(observed["failure"]))
            if time.monotonic() >= deadline:
                raise GStreamerDriverError("forced-IDR response/IDR wait timed out")
            self._preserve_handoff_bus(
                context,
                context.pipeline,
                min(0.05, max(deadline - time.monotonic(), 0.001)),
                "forced-IDR loss containment",
            )
        if gate.failed.is_set():
            raise GStreamerDriverError(str(observed["failure"]))
        gate.held = _HeldForcedIdr(
            request_count=gate.request_count,
            request_seqnum=gate.request_seqnum,
            downstream_seqnum=int(cast(int, observed["downstream_seqnum"])),
            request_monotonic_ns=gate.request_monotonic_ns,
            downstream_event_monotonic_ns=int(
                cast(int, observed["downstream_event_monotonic_ns"])
            ),
            idr_arrival_monotonic_ns=int(
                cast(int, observed["idr_arrival_monotonic_ns"])
            ),
            downstream_running_time_ns=int(
                cast(int, observed["downstream_running_time_ns"])
            ),
            forced_idr_running_time_ns=int(
                cast(int, observed["forced_idr_running_time_ns"])
            ),
            event_to_idr_media_ns=int(
                cast(int, observed["event_to_idr_media_ns"])
            ),
        )
        self._trace_handoff(
            "forced_idr_held_after_route_unblock",
            downstream_running_time_ns=gate.held.downstream_running_time_ns,
            event_to_idr_media_ns=gate.held.event_to_idr_media_ns,
            forced_idr_running_time_ns=gate.held.forced_idr_running_time_ns,
            request_count=gate.held.request_count,
        )

    def _finalize_forced_idr_gate(
        self,
        generation: _RecordingGeneration,
        gate: _ForcedIdrGate,
        final_audio_end_running_time_ns: int,
    ) -> ForcedIdrProof:
        """Bind a held IDR to the immutable AAC edge observed after drain."""

        if gate.proof is not None:
            raise GStreamerDriverError("forced-IDR proof was already finalized")
        if generation.streaming_error is not None:
            raise GStreamerDriverError(generation.streaming_error)
        if (
            isinstance(final_audio_end_running_time_ns, bool)
            or not isinstance(final_audio_end_running_time_ns, int)
            or final_audio_end_running_time_ns < 0
            or generation.last_audio_end_running_time_ns
            != final_audio_end_running_time_ns
        ):
            raise GStreamerDriverError(
                "final AAC access-unit end is unavailable or changed after audio drain"
            )
        held = gate.held
        if held is None:
            raise GStreamerDriverError("forced-IDR gate has no held IDR")
        edge_skew_ns = (
            held.forced_idr_running_time_ns - final_audio_end_running_time_ns
        )
        if not 0 <= edge_skew_ns < _FORCED_IDR_EDGE_BOUND_NS:
            raise GStreamerDriverError(
                "forced-IDR/audio edge skew violates the 100 ms production bound "
                f"({edge_skew_ns} ns)"
            )
        try:
            proof = ForcedIdrProof(
                request_count=held.request_count,
                request_seqnum=held.request_seqnum,
                downstream_seqnum=held.downstream_seqnum,
                seqnum_preserved=held.request_seqnum == held.downstream_seqnum,
                all_headers=True,
                nal5=True,
                request_monotonic_ns=held.request_monotonic_ns,
                downstream_event_monotonic_ns=held.downstream_event_monotonic_ns,
                idr_arrival_monotonic_ns=held.idr_arrival_monotonic_ns,
                downstream_running_time_ns=held.downstream_running_time_ns,
                forced_idr_running_time_ns=held.forced_idr_running_time_ns,
                event_to_idr_media_ns=held.event_to_idr_media_ns,
                request_to_downstream_ns=(
                    held.downstream_event_monotonic_ns - held.request_monotonic_ns
                ),
                downstream_to_idr_ns=(
                    held.idr_arrival_monotonic_ns
                    - held.downstream_event_monotonic_ns
                ),
                request_to_idr_ns=(
                    held.idr_arrival_monotonic_ns - held.request_monotonic_ns
                ),
                last_audio_end_running_time_ns=final_audio_end_running_time_ns,
                edge_skew_ns=edge_skew_ns,
            )
        except ValueError as error:
            raise GStreamerDriverError(
                f"forced-IDR proof finalization failed: {_bounded_detail(error)}"
            ) from error
        gate.proof = proof
        self._trace_handoff(
            "forced_idr_audio_edge_finalized",
            edge_skew_ns=edge_skew_ns,
            request_count=proof.request_count,
        )
        return proof

    def _discard_early_forced_idr_gate(
        self,
        context: _GenerationPipeline,
        generation: _RecordingGeneration,
        gate: _ForcedIdrGate,
        deadline: float,
    ) -> None:
        """Release one unusably early correlated IDR before the sole retry."""

        held = gate.held
        if held is None or gate.proof is not None:
            raise GStreamerDriverError("early forced-IDR discard lacks one unproven held IDR")
        self._await_force_key_dispatch(
            context,
            generation,
            gate.dispatch,
            deadline,
        )
        if gate.event_probe_id and gate.observed.get("event_probe_removed") is not True:
            self._remove_retained_probe(
                gate.event_pad,
                gate.event_probe_id,
                timeout_s=min(max(deadline - time.monotonic(), 0.1), 0.5),
            )
        gate.event_probe_id = None
        if gate.video_probe_id:
            self._release_block_probe(
                gate.video_pad,
                gate.video_probe_id,
                reached=gate.reached,
                completed=gate.completed,
                release=gate.release,
                timeout_s=min(max(deadline - time.monotonic(), 0.1), 0.5),
            )
        gate.video_probe_id = None
        if gate.failed.is_set():
            raise GStreamerDriverError(
                str(gate.observed.get("failure", "early forced-IDR release failed"))
            )
        self._trace_handoff(
            "early_forced_idr_released_for_single_retry",
            forced_idr_running_time_ns=held.forced_idr_running_time_ns,
            request_count=held.request_count,
        )

    @staticmethod
    def _revalidate_forced_idr_audio_edge(
        generation: _RecordingGeneration,
        proof: ForcedIdrProof,
    ) -> None:
        if (
            generation.streaming_error is not None
            or generation.last_audio_end_running_time_ns
            != proof.last_audio_end_running_time_ns
        ):
            raise GStreamerDriverError(
                generation.streaming_error
                or "AAC access-unit end changed after forced-IDR edge proof"
            )

    def isolate_audio_loss(
        self,
        pipeline: object,
        timeout_s: float,
    ) -> AudioLossHandoff:
        """Perform one bounded IDR handoff to the prebuilt video-only generation."""

        context = self._generation_pipelines.get(id(pipeline))
        if context is None:
            raise GStreamerDriverError("pipeline has no immutable generation context")
        if not 0 < timeout_s <= 30:
            raise GStreamerDriverError("audio-loss handoff timeout is invalid")
        if not context.handoff_lock.acquire(timeout=timeout_s):
            raise GStreamerDriverError("audio-loss handoff serialization timed out")
        deadline = time.monotonic() + timeout_s
        force_gate: _ForcedIdrGate | None = None
        force_gates: list[_ForcedIdrGate] = []
        force_proof: ForcedIdrProof | None = None
        audio_idle_pad: Any | None = None
        audio_idle_probe: Any | None = None
        audio_idle_reached = Event()
        transition_published = False
        stable_published = False
        route_mutated = False
        reset_unrouted_successor = False
        successor: _RecordingGeneration | None = None
        retiring: _RecordingGeneration | None = None
        retirement_boundary: str | None = None
        frozen_audio_end: int | None = None
        audio_route_contained = False
        prior_next_video_slot_id = context.next_video_slot_id
        try:
            if context.isolated or context.active_generation_id != 1:
                raise GStreamerDriverError("audio-loss handoff has no active A/V slot")
            old = context.generations[1]
            retiring = old
            successor = self._select_video_successor(context)
            quarantine = context.audio_ingress_quarantine
            if (
                not old.audio_eos.is_retirement_armed()
                or quarantine is None
                or quarantine.activation_id != old.activation_id
            ):
                raise GStreamerDriverError("audio-loss handoff containment was not pre-armed")
            retired_activation = old.activation_id
            if retired_activation is None:
                raise GStreamerDriverError("active A/V slot has no activation identity")
            self._publish_topology_transition(
                context,
                "handoff_in_progress",
                phase="audio_loss",
            )
            transition_published = True
            successor_activation = self._allocate_slot_activation(context, successor)
            successor.reusable = False
            context.routing_phase = "SWITCHING"
            if old.retired or successor.retired or not old.linked or successor.linked:
                raise GStreamerDriverError("immutable generation ownership drifted")
            self._prewarm_generation(context, successor)
            self._set_generation_linked(context, successor, True)
            self._trace_handoff(
                "successor_linked_closed",
                activation_id=successor_activation,
                slot_id=successor.generation_id,
            )
            if old.audio_valve is None or old.audio_queue is None:
                raise GStreamerDriverError("retiring generation lacks its audio route")
            active_locations = set(old.opened)
            if len(active_locations) != 1:
                raise GStreamerDriverError("retiring generation has no unique active fragment")
            active_location = next(iter(active_locations))
            # Request the correlated successor IDR immediately after exact
            # successor/retiring ownership is proven.  Physical USB loss can
            # leave no more AAC units; deferring this request until after the
            # audio-idle/drain work produced a measured 115,252,589 ns edge on
            # the exact Pi.  The successor remains closed, so this early held
            # IDR cannot take media ownership before the final AAC edge is
            # frozen and validated below.
            force_gate = self._arm_forced_idr_gate(
                context,
                successor,
                deadline=deadline,
                timeout_s=timeout_s,
                await_response=False,
            )
            force_gates.append(force_gate)
            self._await_force_key_dispatch(
                context,
                successor,
                force_gate.dispatch,
                deadline,
            )
            self._trace_handoff(
                "forced_idr_armed_before_audio_idle",
                request_count=force_gate.request_count,
            )
            initial_force_gate = force_gate
            probe_ok = self._enum_member(self._gst_member("PadProbeReturn"), "OK")
            audio_idle_pad = self._method(old.audio_valve, "get_static_pad")("src")
            idle_probe_type = self._enum_member(self._gst_member("PadProbeType"), "IDLE")
            idle_probe_type = idle_probe_type | self._enum_member(  # type: ignore[operator]
                self._gst_member("PadProbeType"),
                "BLOCK",
            )

            def hold_audio_idle(_pad: Any, _info: Any) -> Any:
                audio_idle_reached.set()
                return probe_ok

            audio_idle_probe = self._method(audio_idle_pad, "add_probe")(
                idle_probe_type,
                hold_audio_idle,
            )
            if not audio_idle_probe or not audio_idle_reached.wait(
                min(max(deadline - time.monotonic(), 0), 0.5)
            ):
                raise GStreamerDriverError("retiring audio branch did not reach its idle barrier")
            self._trace_handoff("audio_idle_before_retirement_boundary")
            audio_queue = old.audio_queue

            def freeze_inside_drain_proof_gap() -> None:
                nonlocal frozen_audio_end, route_mutated
                if (
                    old.streaming_error is not None
                    or old.last_audio_end_running_time_ns is None
                ):
                    raise GStreamerDriverError(
                        old.streaming_error
                        or "final AAC access-unit end is unavailable at first empty sample"
                    )
                frozen_audio_end = old.last_audio_end_running_time_ns
                # The already accepted request and first empty queue sample
                # now form the cut boundary.  Close the old gates before
                # waiting for the encoder response so video cannot grow an
                # unbounded A/V tail.  The successor remains closed until the
                # correlated IDR and exact audio boundary both pass.
                route_mutated = True
                context.routing_phase = "LOSS_CUT_PENDING_IDR_PROOF"
                self._set_generation_open(old, False)
                self._trace_handoff(
                    "audio_edge_frozen_after_early_forced_idr_arm",
                    last_audio_end_running_time_ns=frozen_audio_end,
                    request_count=initial_force_gate.request_count,
                )
                self._trace_handoff("retiring_generation_gates_closed_after_force_accept")

            self._wait_for_audio_queue_drain(
                audio_queue,
                deadline,
                on_first_empty=freeze_inside_drain_proof_gap,
            )
            if (
                force_gate is None
                or frozen_audio_end is None
                or old.streaming_error is not None
                or old.last_audio_end_running_time_ns != frozen_audio_end
            ):
                raise GStreamerDriverError(
                    old.streaming_error
                    or "AAC access-unit end changed during drain proof gap"
                )
            self._trace_handoff(
                "audio_queue_drained_before_retirement_boundary",
                last_audio_end_running_time_ns=frozen_audio_end,
            )
            self._await_forced_idr_gate(
                context,
                successor,
                force_gate,
                deadline,
            )
            held = force_gate.held
            if held is None:
                raise GStreamerDriverError("early forced-IDR response proof is absent")
            if held.forced_idr_running_time_ns < frozen_audio_end:
                self._trace_handoff(
                    "early_forced_idr_precedes_frozen_audio_edge",
                    forced_idr_running_time_ns=held.forced_idr_running_time_ns,
                    last_audio_end_running_time_ns=frozen_audio_end,
                    request_count=held.request_count,
                )
                self._discard_early_forced_idr_gate(
                    context,
                    successor,
                    force_gate,
                    deadline,
                )
                # Exactly one retry is permitted, only because the first
                # correlated IDR is mathematically unusable (negative edge).
                # It is requested after the AAC edge is frozen, so no further
                # timing retry can be justified or attempted.
                force_gate = self._arm_forced_idr_gate(
                    context,
                    successor,
                    deadline=deadline,
                    timeout_s=timeout_s,
                    await_response=False,
                )
                force_gates.append(force_gate)
                self._await_force_key_dispatch(
                    context,
                    successor,
                    force_gate.dispatch,
                    deadline,
                )
                self._trace_handoff(
                    "forced_idr_single_retry_armed_after_frozen_audio_edge",
                    request_count=force_gate.request_count,
                )
                self._await_forced_idr_gate(
                    context,
                    successor,
                    force_gate,
                    deadline,
                )
            force_proof = self._finalize_forced_idr_gate(
                old,
                force_gate,
                frozen_audio_end,
            )
            self._trace_handoff(
                "forced_idr_held_at_frozen_audio_edge",
                edge_skew_ns=force_proof.edge_skew_ns,
                request_count=force_proof.request_count,
            )
            context.routing_phase = "LOSS_BOUNDARY_COMMITTED"
            retirement_boundary = self._establish_audio_retirement_boundary(
                context,
                old,
                audio_queue,
                deadline,
            )
            if (
                not old.audio_eos.has_forwarded_eos()
                or old.audio_eos.boundary_kind() != retirement_boundary
            ):
                raise GStreamerDriverError(
                    "audio retirement boundary did not close the splitmux audio stream"
                )
            self._wait_for_audio_queue_drain(audio_queue, deadline)
            if (
                old.streaming_error is not None
                or old.last_audio_end_running_time_ns != frozen_audio_end
            ):
                raise GStreamerDriverError(
                    old.streaming_error
                    or "AAC access-unit end changed after audio retirement boundary"
            )
            self._trace_handoff(
                "audio_retirement_boundary_after_forced_idr_finalization",
                last_audio_end_running_time_ns=frozen_audio_end,
            )
            self._revalidate_forced_idr_audio_edge(old, force_proof)
            self._set_generation_linked(context, old, False)
            audio_route_contained = self._audio_loss_route_is_contained(old)
            if not audio_route_contained:
                raise GStreamerDriverError(
                    "retiring audio route containment could not be proven"
                )
            self._trace_handoff("retiring_generation_unlinked_with_forced_idr_armed")
            context.loss_verified = True
            self._trace_handoff(
                "forced_idr_held_after_retirement_boundary",
                edge_skew_ns=force_proof.edge_skew_ns,
                request_count=force_proof.request_count,
            )

            def preserve_bus(timeout: float) -> None:
                self._preserve_handoff_bus(
                    context,
                    pipeline,
                    timeout,
                    "audio-loss handoff",
                )

            self._trace_handoff(
                "forced_idr_held",
                edge_skew_ns=force_proof.edge_skew_ns,
                request_count=force_proof.request_count,
            )
            self._set_generation_open(successor, True)
            self._commit_active_route(context, successor)
            context.routing_phase = "VIDEO_ONLY_ACTIVE"
            self._trace_handoff("successor_routed")
            release_started_ns = time.monotonic_ns()
            force_gate.release.set()
            self._await_force_key_dispatch(
                context,
                successor,
                force_gate.dispatch,
                deadline,
            )
            self._trace_handoff(
                "successor_released",
                held_ns=max(
                    release_started_ns - force_proof.idr_arrival_monotonic_ns,
                    0,
                ),
            )
            while not successor.first_video_seen.is_set() or len(successor.opened) != 1:
                if time.monotonic() >= deadline:
                    raise GStreamerDriverError("video-only successor produced no data")
                preserve_bus(min(0.05, max(deadline - time.monotonic(), 0.001)))
            self._trace_handoff("successor_first_video")
            first_successor_count = successor.video_units
            if not self._generation_playing_converged(
                context,
                successor,
                max(deadline - time.monotonic(), 0),
            ):
                raise GStreamerDriverError("video-only successor state did not converge")

            self._wait_for_audio_queue_drain(audio_queue, deadline)
            self._revalidate_forced_idr_audio_edge(old, force_proof)
            self._trace_handoff("audio_queue_still_drained_after_route")
            video_eos_dispatch = self._start_retired_video_eos_dispatch(
                context,
                old,
                "retiring-av-video-eos",
            )
            self._trace_handoff("loss_video_eos_dispatched")
            eos_deadline = min(deadline, time.monotonic() + 2.0)
            self._await_retirement_dispatch(
                context,
                video_eos_dispatch,
                eos_deadline,
            )
            self._trace_handoff("video_eos_dispatch_complete")
            while old.last_closed_location != active_location:
                if time.monotonic() >= deadline:
                    raise GStreamerDriverError("retiring A/V fragment closure timed out")
                preserve_bus(min(0.05, max(deadline - time.monotonic(), 0.001)))
            self._prove_retirement_has_no_successor_fragment(
                context,
                old,
                preserve_bus,
                deadline,
            )
            self._trace_handoff("retiring_closed")
            required_successor_count = first_successor_count + 30
            while successor.video_units < required_successor_count:
                if time.monotonic() >= deadline:
                    raise GStreamerDriverError(
                        "video-only successor did not sustain 30 additional buffers"
                    )
                preserve_bus(min(0.05, max(deadline - time.monotonic(), 0.001)))
            self._trace_handoff("successor_continuous")
            final_audio_eos = old.audio_eos.snapshot()
            final_generation_eos = old.audio_eos.generation_snapshot()
            exact_audio_boundary = bool(
                (
                    retirement_boundary == "NATURAL"
                    and final_audio_eos[0] == "NATURAL"
                    and final_audio_eos[3] is None
                )
                or (
                    retirement_boundary == "MANUAL"
                    and final_audio_eos[0] == "MANUAL"
                    and final_audio_eos[2] is not None
                    and final_audio_eos[3] == final_audio_eos[2]
                )
                or (
                    retirement_boundary == "GENERATION"
                    and old.generation_retirement_eos_seqnum is not None
                    and final_generation_eos
                    in {
                        (
                            "GENERATION",
                            1,
                            old.generation_retirement_eos_seqnum,
                            old.generation_retirement_eos_seqnum,
                            0,
                            False,
                            True,
                        ),
                        (
                            "GENERATION",
                            1,
                            old.generation_retirement_eos_seqnum,
                            old.generation_retirement_eos_seqnum,
                            1,
                            False,
                            True,
                        ),
                    }
                )
            )
            if (
                successor.first_video_is_idr is not True
                or successor.first_video_had_sticky_contract is not True
                or old.audio_eos.boundary_kind() != retirement_boundary
                or not exact_audio_boundary
                or final_audio_eos[5]
                or not old.audio_eos.has_forwarded_eos()
            ):
                raise GStreamerDriverError("immutable handoff IDR/audio-retirement contract failed")
            if (
                self._method(pipeline, "get_by_name")("camera") is not context.initial_camera
                or self._method(pipeline, "get_by_name")("encoder") is not context.initial_encoder
            ):
                raise GStreamerDriverError("camera or encoder identity changed at handoff")
            old.retired = True
            old.reusable = False
            self._trace_handoff("loss_recycle_started")
            self._recycle_generation(
                old,
                min(max(deadline - time.monotonic(), 0.1), 3.0),
            )
            self._trace_handoff("loss_recycle_complete")
            context.isolated = True
            topology_proof = self._measure_generation_topology(context)
            if (
                topology_proof.get("request_pad_counts_measured") is not True
                or topology_proof.get("request_pad_peer_ownership_proven") is not True
            ):
                raise GStreamerDriverError("audio-loss measured topology proof is absent")
            self._publish_stable_topology(context, topology_proof)
            stable_published = True
            return AudioLossHandoff(
                retired_activation,
                successor_activation,
                force_proof.forced_idr_running_time_ns,
                True,
                True,
                True,
                True,
                successor.video_units,
                True,
                force_proof,
                old.generation_id,
                successor.generation_id,
            )
        except BaseException as primary:
            self._trace_handoff_failure("audio_loss_handoff_failed", primary)
            if route_mutated:
                context.routing_phase = "LOSS_CONTAINMENT_CRITICAL"
                if retiring is not None and not audio_route_contained:
                    try:
                        self._set_generation_open(retiring, False)
                        if retiring.linked:
                            self._set_generation_linked(context, retiring, False)
                        audio_route_contained = (
                            self._audio_loss_route_is_contained(retiring)
                        )
                    except BaseException as containment_error:
                        self._trace_handoff_failure(
                            "audio_loss_route_containment_failed",
                            containment_error,
                        )
            elif successor is not None and successor.linked:
                self._set_generation_linked(context, successor, False)
            if (
                not route_mutated
                and successor is not None
                and successor.activation_id is not None
                and context.routing_phase != "LOSS_CONTAINMENT_CRITICAL"
                and all(
                    dispatch.done.is_set()
                    and (
                        dispatch.thread is None
                        or not dispatch.thread.is_alive()
                    )
                    for dispatch in context.force_key_dispatches
                )
            ):
                reset_unrouted_successor = True
            raise
        finally:
            try:
                if (
                    audio_idle_pad is not None
                    and audio_idle_probe
                    and (not route_mutated or audio_route_contained)
                ):
                    self._remove_retained_probe(
                        audio_idle_pad,
                        audio_idle_probe,
                        timeout_s=0.5,
                    )
                elif audio_idle_pad is not None and audio_idle_probe:
                    self._trace_handoff("audio_idle_probe_retained_until_parent_null")
                for candidate in force_gates:
                    if candidate.event_probe_id:
                        self._remove_retained_probe(
                            candidate.event_pad,
                            candidate.event_probe_id,
                            timeout_s=0.5,
                        )
                    if candidate.video_probe_id:
                        self._release_block_probe(
                            candidate.video_pad,
                            candidate.video_probe_id,
                            reached=candidate.reached,
                            completed=candidate.completed,
                            release=candidate.release,
                            timeout_s=min(
                                max(deadline - time.monotonic(), 0.1),
                                0.5,
                            ),
                        )
                if reset_unrouted_successor and successor is not None:
                    self._reset_unrouted_generation(
                        context,
                        successor,
                        next_video_slot_id=prior_next_video_slot_id,
                        timeout_s=min(
                            max(deadline - time.monotonic(), 0.1),
                            1.0,
                        ),
                    )
            except BaseException:
                if transition_published:
                    self._publish_topology_transition(
                        context,
                        "faulted_handoff",
                        phase="audio_loss_cleanup",
                    )
                raise
            finally:
                if transition_published and not stable_published:
                    self._publish_topology_transition(
                        context,
                        "faulted_handoff",
                        phase="audio_loss",
                    )
                context.handoff_lock.release()
