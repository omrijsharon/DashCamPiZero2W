#!/usr/bin/env python3
"""Owner-assisted exact-Pi microphone-loss immutable-generation harness."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
import tempfile
import threading
import time
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from types import ModuleType
from typing import Any, Final, NoReturn, cast

from dashcam.audio.alsa import AlsaCaptureDevice, AlsaSelector, parse_alsa_selector
from dashcam.audio.linux import AudioDiscoveryStatus, discover_capture_device
from dashcam.config import load_config
from dashcam.storage.preflight import run_live_storage_preflight

RECORDING_ROOT: Final = Path("/srv/dashcam")
QUARANTINE_ROOT: Final = RECORDING_ROOT / "quarantine"
RUN_NAME_RE: Final = re.compile(r"m7-physical-loss-[a-z0-9]{8,32}")
RESULT_ROOT: Final = Path("/var/lib/dashcam")
MEDIA_NAME_RE: Final = re.compile(r"g(01|02)-([0-9]{2})[.]mp4")
SHARED_MANIFEST_SHA256: Final = "ba780c442491ee0f278daaadd2df11d48c3f5ac7adce802d3634a536e07c1013"
SHARED_MEMBERS: Final = ("README.md", "run.py")
EXPECTED_RELEASE: Final = "0.1.0.dev0-011a148e085da278"
FRAME_PERIOD_NS: Final = round(1_000_000_000 / 30)
MIN_AV_FRAGMENTS: Final = 2
MIN_VIDEO_ONLY_FRAGMENTS: Final = 3
MAX_PRELOSS_FRAGMENTS: Final = 32
MAX_MEDIA_COUNT: Final = 40
MIN_LOSS_TIMEOUT_SECONDS: Final = 30
MAX_LOSS_TIMEOUT_SECONDS: Final = 60
DEFAULT_LOSS_TIMEOUT_SECONDS: Final = 60
MAX_AUDIO_LOSS_ERRORS: Final = 4
MAX_LOSS_LATENCY_REFUSALS: Final = 4
LOSS_DISCOVERY_POLL_SECONDS: Final = 0.5
STABLE_NOT_FOUND_SEPARATION_NS: Final = 500_000_000
MAX_LOSS_DISCOVERY_OBSERVATIONS: Final = 128
MAX_MANIFEST_BYTES: Final = 4096
MAX_SHARED_MEMBER_BYTES: Final = 2 * 1024 * 1024
MAX_RESULT_BYTES: Final = 2 * 1024 * 1024
EOS_DISPATCH_TIMEOUT_SECONDS: Final = 2.0
FALLBACK_AUDIO_STABILITY_SECONDS: Final = 0.05
EOS_OBSERVATION_GRACE_SECONDS: Final = 0.05
MAX_OUTPUT_AUDIO_EOS_OBSERVATIONS: Final = 8
AUDIO_EOS_BARRIER_STRUCTURE: Final = "dashcam-m7-audio-eos-barrier"
NULL_TRANSITION_TIMEOUT_SECONDS: Final = 17.0
# The exact-Pi shutdown path retains one callback-owned IDR at the common tee
# and keeps the parent encoder live until NULL. Bound the resulting closed-
# valve tail by elapsed 30-fps media periods, rather than by an unexplained
# frame count.
MAX_FINAL_SHUTDOWN_MEDIA_PERIODS: Final = 7
MAX_FINAL_COMMON_TEE_RING_BUFFERS: Final = 64
MAX_FINAL_FROZEN_MEDIA_BYTES: Final = 16 * 1024 * 1024
VIDEO_IDR_RELEASE_TIMEOUT_SECONDS: Final = 3.0
SUCCESSOR_STATE_GRACE_SECONDS: Final = 0.2
SUCCESSOR_STATE_CONVERGENCE_TIMEOUT_SECONDS: Final = 1.0
MAX_VIDEO_PATH_DIAGNOSTICS: Final = 16
SHA256_RE: Final = re.compile(r"[0-9a-f]{64}")


def _bounded_regular_bytes(path: Path, maximum: int) -> bytes:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise RuntimeError(f"{path} is not a bounded regular file")
        payload = bytearray()
        while chunk := os.read(descriptor, min(65536, maximum + 1 - len(payload))):
            payload.extend(chunk)
            if len(payload) > maximum:
                raise RuntimeError(f"{path} exceeded its read bound")
        return bytes(payload)
    finally:
        os.close(descriptor)


def _verified_shared_members(directory: Path) -> dict[str, bytes]:
    expected = directory
    resolved = directory.resolve(strict=True)
    info = os.lstat(resolved)
    if resolved != expected or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("shared generation-handoff directory identity differs")
    manifest_bytes = _bounded_regular_bytes(resolved / "SHA256SUMS", MAX_MANIFEST_BYTES)
    if hashlib.sha256(manifest_bytes).hexdigest() != SHARED_MANIFEST_SHA256:
        raise RuntimeError("shared generation-handoff manifest identity differs")
    entries: dict[str, str] = {}
    for line in manifest_bytes.decode("ascii").splitlines():
        digest, separator, name = line.partition("  ")
        if (
            not separator
            or SHA256_RE.fullmatch(digest) is None
            or name in entries
            or name not in SHARED_MEMBERS
            or Path(name).name != name
        ):
            raise RuntimeError("shared generation-handoff manifest is not closed")
        entries[name] = digest
    if tuple(sorted(entries)) != SHARED_MEMBERS:
        raise RuntimeError("shared generation-handoff manifest omits a required member")
    members: dict[str, bytes] = {}
    for name, digest in entries.items():
        payload = _bounded_regular_bytes(resolved / name, MAX_SHARED_MEMBER_BYTES)
        if hashlib.sha256(payload).hexdigest() != digest:
            raise RuntimeError(f"shared generation-handoff member {name} failed verification")
        members[name] = payload
    return members


def _load_hash_closed_shared_harness() -> ModuleType:
    directory = Path(__file__).resolve().parent.parent / "milestone7-generation-handoff"
    members = _verified_shared_members(directory)
    path = directory / "run.py"
    name = "dashcam_m7_hash_closed_generation_handoff"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("shared generation-handoff harness could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    code = compile(members["run.py"], str(path), "exec")
    exec(code, module.__dict__)
    module.verify_manifest(SHARED_MANIFEST_SHA256, directory)
    return module


_shared = _load_hash_closed_shared_harness()
HarnessError = _shared.HarnessError


@dataclass
class _EosDispatch:
    label: str
    pad: Any
    done: threading.Event = field(default_factory=threading.Event)
    accepted: bool | None = None
    error: BaseException | None = None
    thread: threading.Thread | None = None
    started_monotonic_ns: int = 0
    ended_monotonic_ns: int | None = None
    event_seqnum: int | None = None


@dataclass(frozen=True)
class _PadEosObservation:
    pad: Any
    pad_path: str
    pad_name: str
    parent_path: str
    peer_path: str
    generation_number: int
    generation_external_linked: bool
    generation_valve_drop: bool
    generation_retired: bool
    active_identity_verified: bool
    forwarded_to_splitmux: bool
    duplicate_refused: bool
    seqnum: int
    observed_monotonic_ns: int


@dataclass
class _NullTransition:
    done: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    error: BaseException | None = None
    null_return: Any | None = None
    waited: Any | None = None
    state: Any | None = None
    started_monotonic_ns: int = 0
    ended_monotonic_ns: int | None = None


class PhysicalExperimentFailure(RuntimeError):
    """A failed live experiment with bounded, JSON-safe bus evidence."""

    def __init__(self, message: str, diagnostic: Mapping[str, object]) -> None:
        super().__init__(message)
        self.diagnostic = dict(diagnostic)


def verify_manifest(expected_sha256: str, directory: Path | None = None) -> dict[str, str]:
    return cast(
        dict[str, str],
        _shared.verify_manifest(
            expected_sha256,
            directory or Path(__file__).resolve().parent,
        ),
    )


def _validated_result_path(path: Path, output_directory: Path) -> Path:
    if (
        not path.is_absolute()
        or path.parent != RESULT_ROOT
        or path.name != f"{output_directory.name}.json"
        or RUN_NAME_RE.fullmatch(path.stem) is None
    ):
        raise HarnessError(
            "evidence output must match the media run identity under /var/lib/dashcam"
        )
    parent = RESULT_ROOT.resolve(strict=True)
    parent_info = os.lstat(parent)
    if parent != RESULT_ROOT or not stat.S_ISDIR(parent_info.st_mode):
        raise HarnessError("evidence output parent identity differs")
    if path.exists() or path.is_symlink():
        raise HarnessError("evidence output must be one fresh direct regular file")
    return path


def _write_atomic_exclusive_json(path: Path, value: Mapping[str, object]) -> None:
    parent = path.parent
    if parent != RESULT_ROOT or parent.resolve(strict=True) != RESULT_ROOT:
        raise HarnessError("evidence output parent identity changed")
    if path.exists() or path.is_symlink():
        raise HarnessError("evidence output must be one fresh direct regular file")
    encoded = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(encoded) > MAX_RESULT_BYTES:
        raise HarnessError("evidence JSON exceeds its bound")
    descriptor, temporary = tempfile.mkstemp(prefix=".m7-physical-loss-", dir=parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary_path, path)
        except FileExistsError as error:
            raise HarnessError("evidence output must be a new file") from error
        try:
            directory_descriptor = os.open(
                parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
            )
        except OSError:
            if os.name != "nt":
                raise
            return
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)


def _prepare_output_directory(selected: Path) -> dict[str, object]:
    if (
        not selected.is_absolute()
        or selected.parent != QUARANTINE_ROOT
        or RUN_NAME_RE.fullmatch(selected.name) is None
    ):
        raise HarnessError("media target is not one safe physical-loss quarantine child")
    root = RECORDING_ROOT.resolve(strict=True)
    root_info = os.lstat(root)
    if root != RECORDING_ROOT or not stat.S_ISDIR(root_info.st_mode):
        raise HarnessError("recording root identity differs")
    if selected.exists() or selected.is_symlink():
        raise HarnessError("media target already exists")
    if QUARANTINE_ROOT.exists() or QUARANTINE_ROOT.is_symlink():
        quarantine = QUARANTINE_ROOT.resolve(strict=True)
        info = os.lstat(quarantine)
        if (
            quarantine != QUARANTINE_ROOT
            or not stat.S_ISDIR(info.st_mode)
            or info.st_dev != root_info.st_dev
        ):
            raise HarnessError("quarantine root left the exact recording device")
    else:
        os.mkdir(QUARANTINE_ROOT, mode=0o750)
    os.mkdir(selected, mode=0o750)
    info = os.lstat(selected)
    if not stat.S_ISDIR(info.st_mode) or info.st_dev != root_info.st_dev:
        raise HarnessError("created media target left the exact recording device")
    return {
        "recording_device": root_info.st_dev,
        "directory": str(selected),
        "created_exclusive": True,
    }


class PhysicalLossExperiment(_shared.Experiment):  # type: ignore[name-defined,misc]
    """A/V to video-only handoff triggered by stable-identity disappearance."""

    def __init__(
        self,
        output_directory: Path,
        selector: AlsaSelector,
        initial_device: AlsaCaptureDevice,
        loss_timeout_seconds: int,
    ) -> None:
        super().__init__(output_directory, initial_device.capture_endpoint)
        self.selector = selector
        self.initial_device = initial_device
        self.loss_timeout_seconds = loss_timeout_seconds
        self.audio_source = self._element("audio_source")
        self.registered_audio_source_path = self.audio_source.get_path_string()
        self.initial_new_clock_seen: bool = False
        self.loss_wait_armed = False
        self.loss_wait_armed_ns: int | None = None
        self.expected_audio_error: dict[str, object] | None = None
        self.audio_loss_errors: list[dict[str, object]] = []
        self.unexpected_bus_errors: list[dict[str, object]] = []
        self.loss_latency_refusals: list[dict[str, object]] = []
        self.loss_discovery_observations: list[dict[str, object]] = []
        self.confirmed_physical_loss: dict[str, object] | None = None
        self.discovery_window_open = False
        self.audio_loss_burst_first_ns: int | None = None
        self.audio_loss_burst_closed = False
        self._eos_dispatches: list[_EosDispatch] = []
        self.eos_dispatch_evidence: list[dict[str, object]] = []
        self.audio_eos_fallback_evidence: list[dict[str, object]] = []
        self.audio_eos_branch_decision_evidence: list[dict[str, object]] = []
        self.natural_eos_final_absence_checks: list[dict[str, object]] = []
        self._output_audio_eos_lock = threading.Lock()
        self._output_audio_eos_observations: list[_PadEosObservation] = []
        self._output_audio_eos_arbiter_states: dict[int, dict[str, object]] = {}
        self._retained_audio_idle_probes: list[tuple[Any, int]] = []
        self._null_transition: _NullTransition | None = None
        self.final_unrouted_video_frames: int | None = None
        self.final_shutdown_tail_evidence: dict[str, object] | None = None
        self.camera_source_counter = _shared.PadCounter()
        self._add_counter_probe("camera", self.camera_source_counter)
        self.video_path_diagnostics: list[dict[str, object]] = []
        self.successor_state_convergence: list[dict[str, object]] = []
        self.terminal_shutdown_phase = "INACTIVE"
        self.terminal_shutdown_context: dict[str, object] | None = None
        self.terminal_parent_eos_observations: list[dict[str, object]] = []

    def create_generation(self, number: int, audio: bool) -> Any:
        generation = super().create_generation(number, audio)
        if not audio:
            return generation
        pad = generation.output_audio_pad
        if pad is None:
            raise HarnessError("A/V generation lacks its exact output audio pad")
        with self._output_audio_eos_lock:
            self._output_audio_eos_arbiter_states[id(pad)] = {
                "mode": "OPEN",
                "barrier_seqnum": None,
                "barrier_observed": False,
                "barrier_observed_monotonic_ns": None,
                "barrier_event": threading.Event(),
                "manual_eos_seqnum": None,
            }

        def observe_exact_eos(observed_pad: Any, info: Any) -> Any:
            event = info.get_event()
            if event is not None and event.type == self.gst.EventType.EOS:
                return self._arbitrate_output_audio_eos(generation, observed_pad, event)
            if event is not None and event.type == self.gst.EventType.CUSTOM_DOWNSTREAM:
                structure = event.get_structure()
                if structure is not None and structure.get_name() == AUDIO_EOS_BARRIER_STRUCTURE:
                    return self._arbitrate_output_audio_barrier(
                        generation,
                        observed_pad,
                        event,
                    )
            return self.gst.PadProbeReturn.OK

        probe = pad.add_probe(self.gst.PadProbeType.EVENT_DOWNSTREAM, observe_exact_eos)
        if not probe:
            raise HarnessError("exact output audio EOS probe was refused")
        return generation

    def _arbitrate_output_audio_eos(
        self,
        generation: Any,
        observed_pad: Any,
        event: Any,
    ) -> Any:
        parent = observed_pad.get_parent_element()
        peer = observed_pad.get_peer()
        pad_name = _shared._bounded_detail(observed_pad.get_name())
        pad_path = _shared._bounded_detail(observed_pad.get_path_string())
        parent_path = _shared._bounded_detail(
            parent.get_path_string() if parent is not None else None
        )
        peer_path = _shared._bounded_detail(peer.get_path_string() if peer is not None else None)
        external_linked = bool(generation.external_linked)
        valve_drop = bool(generation.audio_valve.get_property("drop"))
        retired = bool(generation.retired)
        with self._output_audio_eos_lock:
            state = self._output_audio_eos_arbiter_states.get(id(observed_pad))
            existing = [
                observation
                for observation in self._output_audio_eos_observations
                if observation.pad is observed_pad
            ]
            event_seqnum = int(event.get_seqnum())
            mode = state.get("mode") if state is not None else None
            natural_first = not existing and mode == "OPEN"
            reserved_manual = (
                not existing
                and mode == "MANUAL_RESERVED"
                and state is not None
                and state.get("manual_eos_seqnum") == event_seqnum
            )
            forwarded = natural_first or reserved_manual
            duplicate = not forwarded
            observation = _PadEosObservation(
                pad=observed_pad,
                pad_path=pad_path,
                pad_name=pad_name,
                parent_path=parent_path,
                peer_path=peer_path,
                generation_number=int(generation.number),
                generation_external_linked=external_linked,
                generation_valve_drop=valve_drop,
                generation_retired=retired,
                active_identity_verified=(
                    generation.audio
                    and generation.output_audio_pad is observed_pad
                    and generation.output.get_static_pad("audio_0") is observed_pad
                    and pad_name == "audio_0"
                    and parent is generation.output
                    and peer is generation.audio_queue.get_static_pad("src")
                    and peer is not None
                    and observed_pad.get_peer() is peer
                    and peer.get_peer() is observed_pad
                    and external_linked
                    and not valve_drop
                    and not retired
                ),
                forwarded_to_splitmux=forwarded,
                duplicate_refused=duplicate,
                seqnum=event_seqnum,
                observed_monotonic_ns=time.monotonic_ns(),
            )
            if len(self._output_audio_eos_observations) >= MAX_OUTPUT_AUDIO_EOS_OBSERVATIONS:
                generation.ingress_event_error = "exact output audio EOS observation bound exceeded"
                return self.gst.PadProbeReturn.DROP
            self._output_audio_eos_observations.append(observation)
            if duplicate:
                generation.ingress_event_error = (
                    "unexpected/duplicate exact output audio EOS was refused before splitmux"
                )
                if state is not None:
                    state["mode"] = "FATAL"
                return self.gst.PadProbeReturn.DROP
            if state is None:
                generation.ingress_event_error = "exact output audio EOS arbiter state is absent"
                return self.gst.PadProbeReturn.DROP
            state["mode"] = "NATURAL" if natural_first else "MANUAL_DELIVERED"
        return self.gst.PadProbeReturn.OK

    def _arbitrate_output_audio_barrier(
        self,
        generation: Any,
        observed_pad: Any,
        event: Any,
    ) -> Any:
        with self._output_audio_eos_lock:
            state = self._output_audio_eos_arbiter_states.get(id(observed_pad))
            seqnum = int(event.get_seqnum())
            if (
                state is None
                or state.get("barrier_seqnum") != seqnum
                or state.get("barrier_observed") is True
                or state.get("mode") not in ("OPEN", "NATURAL")
            ):
                generation.ingress_event_error = (
                    "exact output audio EOS serialization barrier differs"
                )
                if state is not None:
                    state["mode"] = "FATAL"
                return self.gst.PadProbeReturn.DROP
            state["barrier_observed"] = True
            state["barrier_observed_monotonic_ns"] = time.monotonic_ns()
            if state["mode"] == "OPEN":
                state["mode"] = "BARRIER_OPEN"
            barrier_event = state.get("barrier_event")
            if not isinstance(barrier_event, threading.Event):
                generation.ingress_event_error = "audio EOS barrier signal identity differs"
                state["mode"] = "FATAL"
                return self.gst.PadProbeReturn.DROP
            barrier_event.set()
        return self.gst.PadProbeReturn.DROP

    def _output_audio_eos_snapshot(self, pad: Any) -> tuple[_PadEosObservation, ...]:
        with self._output_audio_eos_lock:
            return tuple(
                observation
                for observation in self._output_audio_eos_observations
                if observation.pad is pad
            )

    def _await_dispatch_eos_observation(
        self,
        pad: Any,
        dispatch: Mapping[str, object],
        *,
        observation_cursor: int,
        deadline: float,
    ) -> dict[str, object] | None:
        seqnum = dispatch.get("event_seqnum")
        started_ns = dispatch.get("started_monotonic_ns")
        ended_ns = dispatch.get("ended_monotonic_ns")
        if (
            not isinstance(seqnum, int)
            or isinstance(seqnum, bool)
            or not isinstance(started_ns, int)
            or isinstance(started_ns, bool)
            or not isinstance(ended_ns, int)
            or isinstance(ended_ns, bool)
            or ended_ns < started_ns
        ):
            raise HarnessError("EOS dispatch timing/seqnum evidence differs")
        grace_deadline = min(
            deadline,
            ended_ns / 1_000_000_000 + EOS_OBSERVATION_GRACE_SECONDS,
        )
        while True:
            observations = self._output_audio_eos_snapshot(pad)
            if len(observations) < observation_cursor:
                raise HarnessError("exact audio EOS observation cursor regressed")
            new = observations[observation_cursor:]
            mismatched = [
                observation
                for observation in new
                if observation.seqnum != seqnum
                or observation.observed_monotonic_ns < started_ns
                or observation.observed_monotonic_ns
                > ended_ns + int(EOS_OBSERVATION_GRACE_SECONDS * 1_000_000_000)
            ]
            matching = [
                observation
                for observation in new
                if observation.seqnum == seqnum
                and started_ns
                <= observation.observed_monotonic_ns
                <= ended_ns + int(EOS_OBSERVATION_GRACE_SECONDS * 1_000_000_000)
            ]
            if mismatched:
                raise HarnessError("stale/mismatched exact audio EOS observation")
            if len(matching) > 1 or len(new) > 1:
                raise HarnessError("exact audio EOS dispatch was observed more than once")
            if matching:
                observation = matching[0]
                return {
                    "pad_path": observation.pad_path,
                    "pad_name": observation.pad_name,
                    "parent_path": observation.parent_path,
                    "peer_path": observation.peer_path,
                    "generation_number": observation.generation_number,
                    "active_identity_verified": observation.active_identity_verified,
                    "forwarded_to_splitmux": observation.forwarded_to_splitmux,
                    "duplicate_refused": observation.duplicate_refused,
                    "seqnum": observation.seqnum,
                    "observed_monotonic_ns": observation.observed_monotonic_ns,
                    "delta_from_dispatch_start_ns": (
                        observation.observed_monotonic_ns - started_ns
                    ),
                    "delta_from_dispatch_end_ns": (observation.observed_monotonic_ns - ended_ns),
                }
            remaining = grace_deadline - time.monotonic()
            if remaining <= 0:
                return None
            threading.Event().wait(min(remaining, 0.005))

    def _accept_terminal_parent_eos(self, message: Any, source: str) -> bool:
        context = self.terminal_shutdown_context
        if (
            self.terminal_shutdown_phase != "FINAL_FRAGMENT_CLOSED"
            or not isinstance(context, Mapping)
            or self.terminal_parent_eos_observations
            or message.src is not self.pipeline
            or source != self.pipeline.get_name()
        ):
            return False
        generation_number = context.get("final_generation")
        active_location = context.get("active_location")
        dispatch = context.get("final_video_eos_dispatch")
        closure_ns = context.get("fragment_closed_phase_monotonic_ns")
        generation = (
            self.generations.get(generation_number)
            if isinstance(generation_number, int) and not isinstance(generation_number, bool)
            else None
        )
        exact_shutdown_cause = (
            generation is not None
            and not generation.audio
            and not generation.retired
            and generation.external_linked
            and bool(generation.video_valve.get_property("drop"))
            and isinstance(active_location, str)
            and active_location in generation.closed_locations
            and set(generation.opened_locations) == set(generation.closed_locations)
            and generation.video_eos_seen
            and isinstance(dispatch, Mapping)
            and dispatch.get("label") == "final-video-only"
            and dispatch.get("completed") is True
            and dispatch.get("accepted") is True
            and dispatch.get("timed_out") is False
            and dispatch.get("error") is None
            and isinstance(dispatch.get("ended_monotonic_ns"), int)
            and not isinstance(dispatch.get("ended_monotonic_ns"), bool)
            and isinstance(closure_ns, int)
            and not isinstance(closure_ns, bool)
            and cast(int, dispatch["ended_monotonic_ns"]) <= closure_ns
            and len(self.transitions) == 1
            and self.transitions[0].get("new_generation") == generation_number
            and self.transitions[0].get("within_one_frame") is True
            and self.transitions[0].get("new_first_video_is_idr") is True
            and bool(self.video_path_diagnostics)
            and self.video_path_diagnostics[-1].get("stage") == "post_loss_fragment_wait_completed"
            and len(self.successor_state_convergence) == 1
            and self.successor_state_convergence[0].get("converged") is True
        )
        if not exact_shutdown_cause:
            return False
        typed_dispatch = cast(Mapping[str, object], dispatch)
        observed_ns = time.monotonic_ns()
        observation = {
            "sequence": 1,
            "source": source,
            "source_path": _shared._bounded_detail(message.src.get_path_string()),
            "exact_parent_object": True,
            "phase": self.terminal_shutdown_phase,
            "observed_monotonic_ns": observed_ns,
            "final_generation": generation_number,
            "active_location": active_location,
            "fragment_closed_phase_monotonic_ns": closure_ns,
            "delta_from_fragment_closed_phase_ns": observed_ns - cast(int, closure_ns),
            "final_video_eos_event_seqnum": typed_dispatch.get("event_seqnum"),
            "final_video_eos_dispatch_ended_monotonic_ns": typed_dispatch.get("ended_monotonic_ns"),
        }
        self.terminal_parent_eos_observations.append(observation)
        self.terminal_shutdown_phase = "TERMINAL_PARENT_EOS_ACCEPTED"
        self._record_event("terminal_parent_eos_accepted", **observation)
        return True

    def _drain_bus_once(self, timeout_ns: int = 0) -> bool:
        types = (
            self.gst.MessageType.ERROR
            | self.gst.MessageType.WARNING
            | self.gst.MessageType.EOS
            | self.gst.MessageType.ELEMENT
            | self.gst.MessageType.LATENCY
            | self.gst.MessageType.NEW_CLOCK
            | self.gst.MessageType.CLOCK_LOST
            | self.gst.MessageType.QOS
        )
        message = self.bus.timed_pop_filtered(timeout_ns, types)
        if message is None:
            return False
        source = message.src.get_name() if message.src is not None else "unknown"
        if message.type == self.gst.MessageType.ERROR:
            error, debug = message.parse_error()
            detail = f"{source}: {_shared._bounded_detail(error)}; {_shared._bounded_detail(debug)}"
            observed_ns = time.monotonic_ns()
            exact_source = (
                message.src is self.audio_source
                and self.pipeline.get_by_name("audio_source") is self.audio_source
                and message.src.get_path_string() == self.registered_audio_source_path
            )
            first_ns = self.audio_loss_burst_first_ns
            accepted = (
                self.loss_wait_armed
                and exact_source
                and not self.audio_loss_burst_closed
                and len(self.audio_loss_errors) < MAX_AUDIO_LOSS_ERRORS
            )
            domain = getattr(error, "domain", None)
            code = getattr(error, "code", None)
            evidence: dict[str, object] = {
                "sequence": len(self.audio_loss_errors) + 1,
                "source": source,
                "source_path": (
                    message.src.get_path_string() if message.src is not None else "unknown"
                ),
                "registered_source_path": self.registered_audio_source_path,
                "exact_registered_audio_source": exact_source,
                "error_domain": (
                    domain
                    if isinstance(domain, (int, str)) and not isinstance(domain, bool)
                    else _shared._bounded_detail(domain)
                ),
                "error_code": (
                    code
                    if isinstance(code, (int, str)) and not isinstance(code, bool)
                    else _shared._bounded_detail(code)
                ),
                "error_message": _shared._bounded_detail(getattr(error, "message", error)),
                "error_rendered": _shared._bounded_detail(error),
                "debug": _shared._bounded_detail(debug),
                "detail": detail,
                "observed_monotonic_ns": observed_ns,
                "delta_from_first_ns": 0 if first_ns is None else observed_ns - first_ns,
                "delta_from_loss_window_armed_ns": (
                    observed_ns - self.loss_wait_armed_ns
                    if self.loss_wait_armed_ns is not None
                    else None
                ),
                "accepted_loss_burst": accepted,
            }
            if not accepted:
                evidence["rejection"] = (
                    "foreign_source"
                    if not exact_source
                    else "burst_closed"
                    if self.audio_loss_burst_closed
                    else "loss_not_armed"
                    if not self.loss_wait_armed
                    else "burst_count_exceeded"
                    if len(self.audio_loss_errors) >= MAX_AUDIO_LOSS_ERRORS
                    else "loss_session_closed"
                )
                self.unexpected_bus_errors.append(evidence)
                self.errors.append(detail)
                self._record_event(
                    "unexpected_bus_error",
                    source=source,
                    exact_registered_audio_source=exact_source,
                    error_domain=evidence["error_domain"],
                    error_code=evidence["error_code"],
                    rejection=evidence["rejection"],
                    detail=detail,
                )
                raise HarnessError(f"unexpected GStreamer error: {detail}")
            if first_ns is None:
                self.audio_loss_burst_first_ns = observed_ns
            self.audio_loss_errors.append(evidence)
            if self.expected_audio_error is None:
                self.expected_audio_error = evidence
            self._record_event(
                "expected_audio_source_error",
                source=source,
                source_path=message.src.get_path_string(),
                sequence=evidence["sequence"],
                error_domain=evidence["error_domain"],
                error_code=evidence["error_code"],
                delta_from_first_ns=evidence["delta_from_first_ns"],
                detail=detail,
            )
            return True
        if message.type == self.gst.MessageType.WARNING:
            warning, debug = message.parse_warning()
            detail = (
                f"{source}: {_shared._bounded_detail(warning)}; {_shared._bounded_detail(debug)}"
            )
            self.warnings.append(detail)
            self._record_event("bus_warning", source=source, detail=detail)
            raise HarnessError(f"GStreamer warning: {detail}")
        if message.type == self.gst.MessageType.LATENCY:
            if not self.pipeline.recalculate_latency():
                observed_ns = time.monotonic_ns()
                armed_ns = self.loss_wait_armed_ns
                accepted = (
                    self.loss_wait_armed
                    and armed_ns is not None
                    and not self.audio_loss_burst_closed
                    and len(self.loss_latency_refusals) < MAX_LOSS_LATENCY_REFUSALS
                )
                refusal: dict[str, object] = {
                    "sequence": len(self.loss_latency_refusals) + 1,
                    "source": _shared._bounded_detail(source),
                    "source_path": _shared._bounded_detail(
                        message.src.get_path_string() if message.src is not None else "unknown"
                    ),
                    "observed_monotonic_ns": observed_ns,
                    "delta_from_loss_window_armed_ns": (
                        observed_ns - armed_ns if armed_ns is not None else None
                    ),
                    "accepted_loss_window": accepted,
                }
                if not accepted:
                    refusal["rejection"] = (
                        "burst_closed"
                        if self.audio_loss_burst_closed
                        else "loss_not_armed"
                        if not self.loss_wait_armed or armed_ns is None
                        else "refusal_count_exceeded"
                    )
                self.loss_latency_refusals.append(refusal)
                self._record_event(
                    "loss_window_latency_recalculation_refused"
                    if accepted
                    else "unexpected_latency_recalculation_refused",
                    **refusal,
                )
                if not accepted:
                    raise HarnessError("bounded latency recalculation failed")
                return True
            self._record_event("latency_recalculated", source=source)
            return True
        if message.type == self.gst.MessageType.CLOCK_LOST:
            raise HarnessError("pipeline clock was lost")
        if message.type == self.gst.MessageType.QOS:
            raise HarnessError(f"unexpected QoS message from {source}")
        if message.type == self.gst.MessageType.NEW_CLOCK:
            announced = message.parse_new_clock()
            if (
                self.initial_new_clock_seen
                or announced is None
                or (self.clock is not None and announced != self.clock)
            ):
                raise HarnessError("pipeline announced a post-start/foreign clock")
            self.initial_new_clock_seen = True
            self._record_event("initial_new_clock", source=source)
            return True
        if message.type == self.gst.MessageType.EOS:
            if self._accept_terminal_parent_eos(message, source):
                return True
            self._record_event("unexpected_pipeline_eos", source=source)
            raise HarnessError(f"unexpected parent pipeline EOS from {source}")
        structure = message.get_structure()
        if structure is None:
            return True
        name = structure.get_name()
        if name not in (
            "splitmuxsink-fragment-opened",
            "splitmuxsink-fragment-closed",
        ):
            return True
        location = structure.get_string("location")
        if location is None:
            raise HarnessError("splitmux message omitted location")
        media = Path(location)
        match = MEDIA_NAME_RE.fullmatch(media.name)
        if media.parent != self.output_directory or match is None:
            raise HarnessError("splitmux reported a foreign media location")
        generation = self.generations[int(match.group(1))]
        if source != generation.output.get_name():
            raise HarnessError("splitmux message source differs from its generation")
        self._record_event(name, generation=generation.number, location=media.name)
        if name == "splitmuxsink-fragment-opened":
            if media.name in generation.opened_locations:
                raise HarnessError("duplicate fragment open was reported")
            generation.opened_locations.append(media.name)
        else:
            if (
                media.name not in generation.opened_locations
                or media.name in generation.closed_locations
            ):
                raise HarnessError("fragment closure identity differs")
            generation.closed_locations.append(media.name)
        return True

    def _audio_loss_evidence(self) -> dict[str, object]:
        first_ns = (
            cast(int, self.audio_loss_errors[0]["observed_monotonic_ns"])
            if self.audio_loss_errors
            else None
        )
        last_ns = (
            cast(int, self.audio_loss_errors[-1]["observed_monotonic_ns"])
            if self.audio_loss_errors
            else None
        )
        return {
            "accepted_count": len(self.audio_loss_errors),
            "maximum_count": MAX_AUDIO_LOSS_ERRORS,
            "closed": self.audio_loss_burst_closed,
            "corroborated": bool(self.audio_loss_errors),
            "first_observed_monotonic_ns": first_ns,
            "last_observed_monotonic_ns": last_ns,
            "messages": [dict(message) for message in self.audio_loss_errors],
        }

    def _latency_refusal_evidence(self) -> dict[str, object]:
        first_ns = (
            cast(int, self.loss_latency_refusals[0]["observed_monotonic_ns"])
            if self.loss_latency_refusals
            else None
        )
        last_ns = (
            cast(int, self.loss_latency_refusals[-1]["observed_monotonic_ns"])
            if self.loss_latency_refusals
            else None
        )
        return {
            "count": len(self.loss_latency_refusals),
            "maximum_count": MAX_LOSS_LATENCY_REFUSALS,
            "first_observed_monotonic_ns": first_ns,
            "last_observed_monotonic_ns": last_ns,
            "messages": [dict(refusal) for refusal in self.loss_latency_refusals],
        }

    @staticmethod
    def _capture_device_evidence(device: AlsaCaptureDevice) -> dict[str, object]:
        identity = device.identity
        return {
            "endpoint": device.capture_endpoint,
            "identity": {
                "vendor_id": identity.vendor_id,
                "product_id": identity.product_id,
                "product": identity.product,
                "physical_path": identity.physical_path,
                "serial": identity.serial,
                "alsa_card_id": identity.alsa_card_id,
            },
        }

    def _loss_discovery_evidence(self) -> dict[str, object]:
        return {
            "poll_interval_seconds": LOSS_DISCOVERY_POLL_SECONDS,
            "stable_not_found_separation_ns": STABLE_NOT_FOUND_SEPARATION_NS,
            "maximum_observations": MAX_LOSS_DISCOVERY_OBSERVATIONS,
            "observation_count": len(self.loss_discovery_observations),
            "window_open": self.discovery_window_open,
            "initial_device": self._capture_device_evidence(self.initial_device),
            "confirmed_loss": (
                dict(self.confirmed_physical_loss)
                if self.confirmed_physical_loss is not None
                else None
            ),
            "observations": [dict(observation) for observation in self.loss_discovery_observations],
        }

    def _observe_loss_identity(self, *, require_initial_match: bool = False) -> None:
        if (
            not self.discovery_window_open
            or len(self.loss_discovery_observations) >= MAX_LOSS_DISCOVERY_OBSERVATIONS
        ):
            raise HarnessError("physical-loss discovery window/count is not available")
        outcome = discover_capture_device(self.selector)
        observed_ns = time.monotonic_ns()
        observation: dict[str, object] = {
            "sequence": len(self.loss_discovery_observations) + 1,
            "observed_monotonic_ns": observed_ns,
            "delta_from_loss_window_armed_ns": (
                observed_ns - self.loss_wait_armed_ns
                if self.loss_wait_armed_ns is not None
                else None
            ),
            "status": outcome.status.value,
            "device_exposed": outcome.device is not None,
        }
        if outcome.device is not None:
            observation.update(self._capture_device_evidence(outcome.device))
        self.loss_discovery_observations.append(observation)
        self._record_event("loss_identity_observation", **observation)
        if require_initial_match:
            if (
                outcome.status is not AudioDiscoveryStatus.MATCHED
                or outcome.device != self.initial_device
            ):
                raise HarnessError(
                    "armed loss monitor did not begin at the initial exact MATCHED device"
                )
            return
        if outcome.status in (
            AudioDiscoveryStatus.AMBIGUOUS,
            AudioDiscoveryStatus.REFUSED,
        ):
            raise HarnessError(f"physical-loss discovery became {outcome.status.value}")
        if outcome.status is AudioDiscoveryStatus.MATCHED:
            if outcome.device != self.initial_device:
                raise HarnessError("physical-loss discovery matched a changed identity or endpoint")
            return
        if outcome.status is not AudioDiscoveryStatus.NOT_FOUND or outcome.device is not None:
            raise HarnessError("physical-loss discovery outcome schema differs")
        previous = (
            self.loss_discovery_observations[-2]
            if len(self.loss_discovery_observations) >= 2
            else None
        )
        if (
            previous is not None
            and previous.get("status") == AudioDiscoveryStatus.NOT_FOUND.value
            and isinstance(previous.get("observed_monotonic_ns"), int)
            and observed_ns - cast(int, previous["observed_monotonic_ns"])
            >= STABLE_NOT_FOUND_SEPARATION_NS
        ):
            self.confirmed_physical_loss = {
                "trigger": "stable_identity_not_found",
                "first_not_found_sequence": previous["sequence"],
                "second_not_found_sequence": observation["sequence"],
                "first_not_found_monotonic_ns": previous["observed_monotonic_ns"],
                "second_not_found_monotonic_ns": observed_ns,
                "separation_ns": (observed_ns - cast(int, previous["observed_monotonic_ns"])),
            }
            self._record_event(
                "stable_identity_not_found_confirmed",
                **self.confirmed_physical_loss,
            )

    def _wait_for_confirmed_physical_loss(self, first: Any) -> None:
        deadline = time.monotonic() + float(self.loss_timeout_seconds)
        next_poll = time.monotonic() + LOSS_DISCOVERY_POLL_SECONDS
        while self.confirmed_physical_loss is None:
            if len(first.opened_locations) >= MAX_PRELOSS_FRAGMENTS:
                raise HarnessError("pre-loss media cap reached before stable identity loss")
            now = time.monotonic()
            if now >= deadline:
                raise HarnessError("bounded stable-identity loss wait expired")
            if now >= next_poll:
                self._observe_loss_identity()
                next_poll = time.monotonic() + LOSS_DISCOVERY_POLL_SECONDS
                continue
            wait_ns = int(max(0.0, min(0.1, next_poll - now, deadline - now)) * self.gst.SECOND)
            self._drain_bus_once(wait_ns)

    def _failure_diagnostic(self) -> dict[str, object]:
        with self._output_audio_eos_lock:
            eos_observations = [
                {
                    "pad_path": observation.pad_path,
                    "pad_name": observation.pad_name,
                    "parent_path": observation.parent_path,
                    "peer_path": observation.peer_path,
                    "generation_number": observation.generation_number,
                    "generation_external_linked": observation.generation_external_linked,
                    "generation_valve_drop": observation.generation_valve_drop,
                    "generation_retired": observation.generation_retired,
                    "active_identity_verified": observation.active_identity_verified,
                    "forwarded_to_splitmux": observation.forwarded_to_splitmux,
                    "duplicate_refused": observation.duplicate_refused,
                    "seqnum": observation.seqnum,
                    "observed_monotonic_ns": observation.observed_monotonic_ns,
                }
                for observation in self._output_audio_eos_observations
            ]
        return {
            "registered_audio_source_path": self.registered_audio_source_path,
            "loss_wait_armed": self.loss_wait_armed,
            "audio_loss_error_burst": self._audio_loss_evidence(),
            "unexpected_bus_errors": [dict(message) for message in self.unexpected_bus_errors],
            "latency_recalculation_refusals": self._latency_refusal_evidence(),
            "physical_loss_discovery": self._loss_discovery_evidence(),
            "eos_dispatches": [dict(dispatch) for dispatch in self.eos_dispatch_evidence],
            "audio_eos_fallbacks": [
                dict(fallback) for fallback in self.audio_eos_fallback_evidence
            ],
            "audio_eos_branch_decisions": [
                dict(decision) for decision in self.audio_eos_branch_decision_evidence
            ],
            "natural_eos_final_absence_checks": [
                dict(check) for check in self.natural_eos_final_absence_checks
            ],
            "video_path_diagnostics": [
                dict(diagnostic) for diagnostic in self.video_path_diagnostics
            ],
            "successor_state_convergence": [
                dict(convergence) for convergence in self.successor_state_convergence
            ],
            "terminal_shutdown": {
                "phase": self.terminal_shutdown_phase,
                "context": (
                    dict(self.terminal_shutdown_context)
                    if self.terminal_shutdown_context is not None
                    else None
                ),
                "parent_eos_observations": [
                    dict(observation) for observation in self.terminal_parent_eos_observations
                ],
                "video_tail": (
                    dict(tail)
                    if (
                        tail := getattr(self, "final_shutdown_tail_evidence", None)
                    )
                    is not None
                    else None
                ),
            },
            "output_audio_eos_observations": eos_observations,
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            "events": list(self.events),
        }

    def _output_audio_eos_evidence(self) -> list[dict[str, object]]:
        with self._output_audio_eos_lock:
            return [
                {
                    "pad_path": observation.pad_path,
                    "pad_name": observation.pad_name,
                    "parent_path": observation.parent_path,
                    "peer_path": observation.peer_path,
                    "generation_number": observation.generation_number,
                    "generation_external_linked": observation.generation_external_linked,
                    "generation_valve_drop": observation.generation_valve_drop,
                    "generation_retired": observation.generation_retired,
                    "active_identity_verified": observation.active_identity_verified,
                    "forwarded_to_splitmux": observation.forwarded_to_splitmux,
                    "duplicate_refused": observation.duplicate_refused,
                    "seqnum": observation.seqnum,
                    "observed_monotonic_ns": observation.observed_monotonic_ns,
                }
                for observation in self._output_audio_eos_observations
            ]

    def _close_discovery_trigger_window(self) -> None:
        if not self.discovery_window_open or self.confirmed_physical_loss is None:
            raise HarnessError("stable-identity trigger window cannot be closed")
        self.discovery_window_open = False
        self._record_event(
            "stable_identity_trigger_window_closed",
            observation_count=len(self.loss_discovery_observations),
        )

    def _close_audio_corroboration_window(self) -> None:
        if self.audio_loss_burst_closed or self.discovery_window_open:
            raise HarnessError("audio-loss corroboration window cannot be closed")
        self.loss_wait_armed = False
        self.audio_loss_burst_closed = True
        if len(self.transitions) != 1:
            raise HarnessError("audio corroboration closure lacks one physical transition")
        self.transitions[0]["gst_error_corroborated"] = bool(self.audio_loss_errors)
        self.transitions[0]["audio_loss_error_count"] = len(self.audio_loss_errors)
        self.transitions[0]["first_audio_error_monotonic_ns"] = (
            self.audio_loss_errors[0]["observed_monotonic_ns"] if self.audio_loss_errors else None
        )
        self.transitions[0]["last_audio_error_monotonic_ns"] = (
            self.audio_loss_errors[-1]["observed_monotonic_ns"] if self.audio_loss_errors else None
        )
        self._record_event(
            "audio_loss_corroboration_window_closed",
            accepted_count=len(self.audio_loss_errors),
            corroborated=bool(self.audio_loss_errors),
            duration_ns=(
                cast(int, self.audio_loss_errors[-1]["observed_monotonic_ns"])
                - cast(int, self.audio_loss_errors[0]["observed_monotonic_ns"])
                if self.audio_loss_errors
                else 0
            ),
        )

    def _assert_parent_identity(self) -> None:
        super()._assert_parent_identity()
        if (
            self.pipeline.get_by_name("audio_source") is not self.audio_source
            or self.audio_source.get_path_string() != self.registered_audio_source_path
        ):
            raise HarnessError("registered audio-source object identity changed")

    def _install_audio_idle_block(
        self,
        old: Any,
        *,
        deadline: float,
    ) -> dict[str, object]:
        pad = old.audio_valve.get_static_pad("src")
        if pad is None:
            raise HarnessError("retired audio valve source pad is absent")
        reached = threading.Event()
        observed: dict[str, object] = {}

        def idle_block(observed_pad: Any, _info: Any) -> Any:
            observed["pad_path"] = _shared._bounded_detail(observed_pad.get_path_string())
            observed["observed_monotonic_ns"] = time.monotonic_ns()
            observed["exact_pad_identity"] = observed_pad is pad
            reached.set()
            return self.gst.PadProbeReturn.OK

        probe = int(pad.add_probe(self.gst.PadProbeType.IDLE, idle_block))
        if not probe:
            raise HarnessError("retired audio valve IDLE block probe was refused")
        self._retained_audio_idle_probes.append((pad, probe))
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not reached.wait(remaining):
            raise HarnessError("retired audio valve IDLE block probe timed out")
        if observed.get("exact_pad_identity") is not True:
            raise HarnessError("retired audio valve IDLE block pad identity differs")
        return {
            "probe": probe,
            "required": True,
            "retained_until_terminal_decision_and_exact_old_fragment_closure": True,
            "released_after_exact_unlink_and_old_fragment_closure": False,
            **observed,
        }

    def _release_audio_idle_block_after_old_closure(
        self,
        old: Any,
        decision: Mapping[str, object],
    ) -> None:
        idle_block = decision.get("idle_block")
        if not isinstance(idle_block, dict):
            raise HarnessError("audio EOS branch decision omitted IDLE-block evidence")
        if idle_block.get("required") is False:
            if self._retained_audio_idle_probes:
                raise HarnessError("natural-before-topology path unexpectedly retained IDLE block")
            return
        probe = idle_block.get("probe")
        pad = old.audio_valve.get_static_pad("src")
        if (
            not isinstance(probe, int)
            or isinstance(probe, bool)
            or pad is None
            or (pad, probe) not in self._retained_audio_idle_probes
            or old.external_linked
            or set(old.opened_locations) != set(old.closed_locations)
            or not old.video_eos_seen
            or not old.audio_eos_seen
        ):
            raise HarnessError("audio IDLE block release preconditions differ")
        pad.remove_probe(probe)
        self._retained_audio_idle_probes.remove((pad, probe))
        idle_block["released_after_exact_unlink_and_old_fragment_closure"] = True
        idle_block["released_monotonic_ns"] = time.monotonic_ns()
        idle_block["permanent_output_arbiter_remains"] = True
        self._record_event(
            "audio_idle_block_released_after_exact_old_fragment_closure",
            generation=old.number,
            probe=probe,
        )

    def _serialize_audio_eos_branch(
        self,
        old: Any,
        *,
        deadline: float,
    ) -> tuple[dict[str, object], bool, Any | None]:
        pad, _identity = self._retired_audio_pad_identity(old)
        with self._output_audio_eos_lock:
            state = self._output_audio_eos_arbiter_states.get(id(pad))
            mode_before_barrier = state.get("mode") if state is not None else None
        idle_block: dict[str, object]
        if mode_before_barrier == "NATURAL":
            idle_block = {
                "required": False,
                "reason": "natural_eos_already_admitted_before_topology",
                "permanent_output_arbiter_remains": True,
            }
        elif mode_before_barrier == "OPEN":
            idle_block = self._install_audio_idle_block(old, deadline=deadline)
            with self._output_audio_eos_lock:
                state = self._output_audio_eos_arbiter_states.get(id(pad))
                mode_before_barrier = state.get("mode") if state is not None else None
        else:
            raise HarnessError("audio EOS arbiter was unavailable before serialization")
        decision: dict[str, object] = {
            "deadline_seconds": EOS_DISPATCH_TIMEOUT_SECONDS,
            "idle_block": idle_block,
            "audio_barrier": None,
            "selected_natural_audio_eos": None,
            "manual_eos_reserved_seqnum": None,
        }
        self.audio_eos_branch_decision_evidence.append(decision)
        if mode_before_barrier == "NATURAL":
            queue_proof: dict[str, object] = {}
            self._prove_retired_audio_queue_stable(old, queue_proof, deadline=deadline)
            decision.update(
                {
                    "audio_barrier": {
                        "required": False,
                        "reason": "natural_eos_already_admitted_before_barrier",
                    },
                    "post_barrier_queue_proof": queue_proof,
                    "selected_natural_audio_eos": True,
                    "decision_monotonic_ns": time.monotonic_ns(),
                }
            )
            return decision, True, None
        if mode_before_barrier != "OPEN":
            raise HarnessError("audio EOS arbiter changed while installing serialization block")
        structure = self.gst.Structure.new_empty(AUDIO_EOS_BARRIER_STRUCTURE)
        barrier = self.gst.Event.new_custom(
            self.gst.EventType.CUSTOM_DOWNSTREAM,
            structure,
        )
        barrier_seqnum = int(barrier.get_seqnum())
        with self._output_audio_eos_lock:
            state = self._output_audio_eos_arbiter_states.get(id(pad))
            if (
                state is None
                or state.get("mode") != "OPEN"
                or state.get("barrier_seqnum") is not None
            ):
                raise HarnessError("audio EOS arbiter was unavailable for serialization")
            state["barrier_seqnum"] = barrier_seqnum
            barrier_signal = state.get("barrier_event")
        audio_sink_pad = old.audio_queue.get_static_pad("sink")
        if audio_sink_pad is None:
            raise HarnessError("retired dead-audio queue sink is absent")
        barrier_dispatch = self._await_eos_dispatches(
            (
                self._start_downstream_event(
                    audio_sink_pad,
                    "loss-retired-audio-serialization-barrier",
                    barrier,
                ),
            ),
            allow_refused_labels=("loss-retired-audio-serialization-barrier",),
            deadline=deadline,
        )[0]
        if not isinstance(barrier_signal, threading.Event):
            raise HarnessError("audio EOS serialization barrier signal identity differs")
        while True:
            with self._output_audio_eos_lock:
                state = self._output_audio_eos_arbiter_states.get(id(pad))
                if state is None or state.get("barrier_seqnum") != barrier_seqnum:
                    raise HarnessError("audio EOS serialization barrier state differs")
                barrier_observed = state.get("barrier_observed") is True
                barrier_observed_ns = state.get("barrier_observed_monotonic_ns")
                mode_after_barrier = state.get("mode")
            if mode_after_barrier == "NATURAL" or (
                barrier_observed and mode_after_barrier == "BARRIER_OPEN"
            ):
                break
            if mode_after_barrier == "FATAL":
                raise HarnessError("audio EOS serialization arbiter latched refusal")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HarnessError("audio EOS serialization barrier observation timed out")
            barrier_signal.wait(min(remaining, 0.005))
        decision["audio_barrier"] = {
            "dispatch": dict(barrier_dispatch),
            "seqnum": barrier_seqnum,
            "observed_monotonic_ns": barrier_observed_ns,
            "consumed_before_splitmux": barrier_observed,
            "mode_after_barrier": mode_after_barrier,
        }
        serialized_queue_proof: dict[str, object] = {}
        self._prove_retired_audio_queue_stable(
            old,
            serialized_queue_proof,
            deadline=deadline,
        )
        decision["post_barrier_queue_proof"] = serialized_queue_proof
        if mode_after_barrier == "NATURAL":
            decision["selected_natural_audio_eos"] = True
            decision["manual_eos_reserved_seqnum"] = None
            decision["decision_monotonic_ns"] = time.monotonic_ns()
            return decision, True, None
        manual_eos = self.gst.Event.new_eos()
        manual_seqnum = int(manual_eos.get_seqnum())
        with self._output_audio_eos_lock:
            state = self._output_audio_eos_arbiter_states.get(id(pad))
            if state is None or state.get("mode") != "BARRIER_OPEN":
                raise HarnessError("audio EOS arbiter changed before manual reservation")
            state["manual_eos_seqnum"] = manual_seqnum
            state["mode"] = "MANUAL_RESERVED"
        decision.update(
            {
                "selected_natural_audio_eos": False,
                "manual_eos_reserved_seqnum": manual_seqnum,
                "decision_monotonic_ns": time.monotonic_ns(),
            }
        )
        return decision, False, manual_eos

    def _start_downstream_event(
        self,
        pad: Any,
        label: str,
        event: Any,
    ) -> _EosDispatch:
        dispatch = _EosDispatch(label=label, pad=pad)
        dispatch.event_seqnum = int(event.get_seqnum())

        def worker() -> None:
            dispatch.started_monotonic_ns = time.monotonic_ns()
            try:
                dispatch.accepted = bool(pad.send_event(event))
            except BaseException as error:
                dispatch.error = error
            finally:
                dispatch.ended_monotonic_ns = time.monotonic_ns()
                dispatch.done.set()

        thread = threading.Thread(
            target=worker,
            name=f"m7-{label}-eos",
            daemon=True,
        )
        dispatch.thread = thread
        self._eos_dispatches.append(dispatch)
        thread.start()
        return dispatch

    def _start_downstream_eos(
        self,
        pad: Any,
        label: str,
        *,
        event: Any | None = None,
    ) -> _EosDispatch:
        return self._start_downstream_event(
            pad,
            label,
            event if event is not None else self.gst.Event.new_eos(),
        )

    def _await_eos_dispatches(
        self,
        dispatches: Sequence[_EosDispatch],
        *,
        allow_refused_labels: Sequence[str] = (),
        deadline: float | None = None,
    ) -> list[dict[str, object]]:
        absolute_deadline = (
            deadline if deadline is not None else time.monotonic() + EOS_DISPATCH_TIMEOUT_SECONDS
        )
        evidence: list[dict[str, object]] = []
        for dispatch in dispatches:
            remaining = absolute_deadline - time.monotonic()
            if remaining <= 0 or not dispatch.done.wait(remaining):
                timed_out = {
                    "label": dispatch.label,
                    "completed": False,
                    "accepted": None,
                    "timed_out": True,
                    "started_monotonic_ns": dispatch.started_monotonic_ns,
                    "event_seqnum": dispatch.event_seqnum,
                }
                self.eos_dispatch_evidence.append(timed_out)
                raise HarnessError(f"{dispatch.label} downstream EOS dispatch timed out")
            thread = dispatch.thread
            if thread is None:
                raise HarnessError(f"{dispatch.label} downstream EOS worker identity was lost")
            thread.join(timeout=0)
            if thread.is_alive():
                raise HarnessError(f"{dispatch.label} downstream EOS worker did not terminate")
            completed = {
                "label": dispatch.label,
                "completed": True,
                "accepted": dispatch.accepted,
                "timed_out": False,
                "started_monotonic_ns": dispatch.started_monotonic_ns,
                "event_seqnum": dispatch.event_seqnum,
                "ended_monotonic_ns": dispatch.ended_monotonic_ns,
                "duration_ns": (
                    dispatch.ended_monotonic_ns - dispatch.started_monotonic_ns
                    if dispatch.ended_monotonic_ns is not None
                    else None
                ),
                "error": (
                    _shared._bounded_detail(dispatch.error) if dispatch.error is not None else None
                ),
            }
            self.eos_dispatch_evidence.append(completed)
            evidence.append(completed)
            self._eos_dispatches.remove(dispatch)
            if dispatch.error is not None:
                raise HarnessError(
                    f"{dispatch.label} downstream EOS dispatch failed: "
                    f"{_shared._bounded_detail(dispatch.error)}"
                )
            if (
                dispatch.accepted is not True or dispatch.ended_monotonic_ns is None
            ) and dispatch.label not in allow_refused_labels:
                raise HarnessError(f"{dispatch.label} downstream EOS was refused")
        return evidence

    def _resolve_retired_audio_eos(
        self,
        old: Any,
        primary: Mapping[str, object],
        *,
        deadline: float,
        observation_count_before_primary: int = 0,
    ) -> dict[str, object]:
        if primary.get("label") != "loss-retired-audio":
            raise HarnessError("retired audio EOS primary evidence identity differs")
        record: dict[str, object] = {
            "primary": dict(primary),
            "fallback_used": False,
            "stable_not_found_confirmed": self.confirmed_physical_loss is not None,
            "exact_audio_error_count": len(self.audio_loss_errors),
            "output_audio_eos_observation_count_before_primary": (observation_count_before_primary),
            "primary_exact_eos_observation": None,
            "fallback_exact_eos_observation": None,
            "fallback": None,
        }
        self.audio_eos_fallback_evidence.append(record)
        clean_primary_refusal = (
            primary.get("accepted") is False
            and primary.get("completed") is True
            and primary.get("timed_out") is False
            and primary.get("error") is None
        )
        clean_primary_acceptance = (
            primary.get("accepted") is True
            and primary.get("completed") is True
            and primary.get("timed_out") is False
            and primary.get("error") is None
        )
        if not clean_primary_acceptance and not clean_primary_refusal:
            raise HarnessError("retired audio EOS primary dispatch shape differs")
        eligible = (
            clean_primary_refusal
            and self.confirmed_physical_loss is not None
            and bool(self.audio_loss_errors)
            and not any(
                evidence is not record and evidence.get("fallback_used") is True
                for evidence in self.audio_eos_fallback_evidence
            )
        )
        record["eligible"] = eligible
        if clean_primary_refusal and not eligible:
            raise HarnessError(
                "retired audio EOS refusal lacks stable-loss exact-error corroboration"
            )
        if old.external_linked or not bool(old.audio_valve.get_property("drop")):
            raise HarnessError("retired audio path is not gated and unlinked")
        pad, initial_pad_identity = self._retired_audio_pad_identity(old)
        record["initial_pad_identity"] = initial_pad_identity
        observations_before_resolution = self._output_audio_eos_snapshot(pad)
        record["output_audio_eos_observation_count_before_resolution"] = len(
            observations_before_resolution
        )
        if (
            observation_count_before_primary != 0
            or observations_before_resolution[:observation_count_before_primary]
        ):
            raise HarnessError("pre-primary exact audio EOS observation is stale")
        primary_observation = self._await_dispatch_eos_observation(
            pad,
            primary,
            observation_cursor=observation_count_before_primary,
            deadline=deadline,
        )
        record["primary_exact_eos_observation"] = primary_observation
        record["output_audio_eos_observation_count_after_primary"] = len(
            self._output_audio_eos_snapshot(pad)
        )
        if clean_primary_acceptance:
            if primary_observation is None:
                raise HarnessError(
                    "accepted retired audio EOS lacked exact output-pad seqnum observation"
                )
            record.update(
                {
                    "delivery_mode": "primary_queue_sink_accepted",
                    "effective_delivery_observed": True,
                }
            )
            return record
        self._prove_retired_audio_queue_stable(old, record, deadline=deadline)
        if primary_observation is not None:
            record.update(
                {
                    "delivery_mode": "exact_output_pad_eos_observed_after_primary_refusal",
                    "effective_delivery_observed": True,
                    "direct_fallback_suppressed_to_avoid_duplicate_eos": True,
                    "attempt_count": 0,
                }
            )
            self._record_event(
                "retired_audio_eos_observation_accepted",
                event_seqnum=primary.get("event_seqnum"),
                observed_monotonic_ns=primary_observation["observed_monotonic_ns"],
                primary_accepted=primary.get("accepted"),
                direct_fallback_suppressed=True,
            )
            return record
        raise HarnessError("refused retired audio EOS lacked exact reserved arbiter observation")

    def _retired_audio_pad_identity(self, old: Any) -> tuple[Any, dict[str, object]]:
        pad = old.output_audio_pad
        peer = old.audio_queue.get_static_pad("src")
        if (
            pad is None
            or old.output.get_static_pad("audio_0") is not pad
            or pad.get_name() != "audio_0"
            or pad.get_parent_element() is not old.output
            or peer is None
            or pad.get_peer() is not peer
            or peer.get_peer() is not pad
        ):
            raise HarnessError("existing splitmux audio request-pad identity differs")
        return pad, {
            "path": _shared._bounded_detail(pad.get_path_string()),
            "name": _shared._bounded_detail(pad.get_name()),
            "parent_path": _shared._bounded_detail(pad.get_parent_element().get_path_string()),
            "peer_path": _shared._bounded_detail(peer.get_path_string()),
        }

    def _prove_retired_audio_queue_stable(
        self,
        old: Any,
        record: dict[str, object],
        *,
        deadline: float,
    ) -> None:
        first_snapshot = {
            "observed_monotonic_ns": time.monotonic_ns(),
            "current_level_buffers": int(old.audio_queue.get_property("current-level-buffers")),
            "current_level_bytes": int(old.audio_queue.get_property("current-level-bytes")),
            "current_level_time_ns": int(old.audio_queue.get_property("current-level-time")),
            "audio_counter": int(old.audio_counter.count),
        }
        if any(
            first_snapshot[name] != 0
            for name in (
                "current_level_buffers",
                "current_level_bytes",
                "current_level_time_ns",
            )
        ):
            record["queue_snapshots"] = [first_snapshot]
            raise HarnessError("retired audio queue was nonempty before serialized EOS")
        remaining = deadline - time.monotonic()
        if remaining < FALLBACK_AUDIO_STABILITY_SECONDS:
            record["queue_snapshots"] = [first_snapshot]
            raise HarnessError("retired audio EOS fallback lacked stability-deadline time")
        stability_target_ns = first_snapshot["observed_monotonic_ns"] + int(
            FALLBACK_AUDIO_STABILITY_SECONDS * 1_000_000_000
        )
        while time.monotonic_ns() < stability_target_ns:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                record["queue_snapshots"] = [first_snapshot]
                raise HarnessError("retired audio EOS fallback exhausted its shared deadline")
            wait_seconds = min(
                remaining,
                (stability_target_ns - time.monotonic_ns()) / 1_000_000_000,
            )
            threading.Event().wait(max(wait_seconds, 0))
        second_snapshot = {
            "observed_monotonic_ns": time.monotonic_ns(),
            "current_level_buffers": int(old.audio_queue.get_property("current-level-buffers")),
            "current_level_bytes": int(old.audio_queue.get_property("current-level-bytes")),
            "current_level_time_ns": int(old.audio_queue.get_property("current-level-time")),
            "audio_counter": int(old.audio_counter.count),
        }
        record["queue_snapshots"] = [first_snapshot, second_snapshot]
        separation_ns = (
            second_snapshot["observed_monotonic_ns"] - first_snapshot["observed_monotonic_ns"]
        )
        record["queue_snapshot_separation_ns"] = separation_ns
        record["audio_counter_stable"] = (
            first_snapshot["audio_counter"] == second_snapshot["audio_counter"]
        )
        if (
            separation_ns < int(FALLBACK_AUDIO_STABILITY_SECONDS * 1_000_000_000)
            or any(
                second_snapshot[name] != 0
                for name in (
                    "current_level_buffers",
                    "current_level_bytes",
                    "current_level_time_ns",
                )
            )
            or record["audio_counter_stable"] is not True
        ):
            raise HarnessError("retired audio queue/counter stability proof failed")

    def _resolve_natural_retired_audio_eos(
        self,
        old: Any,
        *,
        deadline: float,
    ) -> dict[str, object]:
        pad, initial_pad_identity = self._retired_audio_pad_identity(old)
        observations = self._output_audio_eos_snapshot(pad)
        record: dict[str, object] = {
            "primary": None,
            "fallback": None,
            "fallback_used": False,
            "attempt_count": 0,
            "audio_dispatch_attempted": False,
            "audio_dispatch_return": None,
            "audio_dispatch_attempt_count": 0,
            "delivery_mode": "loss_attributed_natural_upstream_eos",
            "effective_delivery_observed": False,
            "stable_not_found_confirmed": self.confirmed_physical_loss is not None,
            "exact_audio_error_count": len(self.audio_loss_errors),
            "natural_eos_observation_count": len(observations),
            "natural_exact_eos_observation": None,
            "initial_pad_identity": initial_pad_identity,
        }
        self.audio_eos_fallback_evidence.append(record)
        if old.external_linked or not bool(old.audio_valve.get_property("drop")):
            raise HarnessError("retired audio path is not gated and unlinked")
        if len(observations) != 1:
            raise HarnessError("natural retired audio EOS observation count differs")
        observation = observations[0]
        observation_evidence = {
            "pad_path": observation.pad_path,
            "pad_name": observation.pad_name,
            "parent_path": observation.parent_path,
            "peer_path": observation.peer_path,
            "generation_number": observation.generation_number,
            "generation_external_linked": observation.generation_external_linked,
            "generation_valve_drop": observation.generation_valve_drop,
            "generation_retired": observation.generation_retired,
            "active_identity_verified": observation.active_identity_verified,
            "forwarded_to_splitmux": observation.forwarded_to_splitmux,
            "duplicate_refused": observation.duplicate_refused,
            "seqnum": observation.seqnum,
            "observed_monotonic_ns": observation.observed_monotonic_ns,
        }
        record["natural_exact_eos_observation"] = observation_evidence
        armed_ns = self.loss_wait_armed_ns
        confirmed = self.confirmed_physical_loss
        if (
            not self.loss_wait_armed
            or self.audio_loss_burst_closed
            or not isinstance(armed_ns, int)
            or isinstance(armed_ns, bool)
            or confirmed is None
            or not self.audio_loss_errors
        ):
            raise HarnessError("natural retired audio EOS lacks an armed exact-loss session")
        first_error_ns = self.audio_loss_errors[0].get("observed_monotonic_ns")
        first_not_found_ns = confirmed.get("first_not_found_monotonic_ns")
        second_not_found_ns = confirmed.get("second_not_found_monotonic_ns")
        recognized_error_shapes: list[str] = []
        for error in self.audio_loss_errors:
            shape = None
            if (
                error.get("error_domain") == "gst-resource-error-quark"
                and error.get("error_code") == 9
                and error.get("error_message")
                == "Error recording from audio device. The device has been disconnected."
                and "gst_alsasrc_read" in cast(str, error.get("debug", ""))
            ):
                shape = "alsa_device_disconnected"
            elif (
                error.get("error_domain") == "gst-stream-error-quark"
                and error.get("error_code") == 1
                and error.get("error_message") == "Internal data stream error."
                and "gst_base_src_loop" in cast(str, error.get("debug", ""))
                and "reason error (-5)" in cast(str, error.get("debug", ""))
            ):
                shape = "base_source_stream_error"
            if shape is not None:
                recognized_error_shapes.append(shape)
        exact_errors = all(
            error.get("accepted_loss_burst") is True
            and error.get("exact_registered_audio_source") is True
            and error.get("source_path") == self.registered_audio_source_path
            and isinstance(error.get("observed_monotonic_ns"), int)
            and not isinstance(error.get("observed_monotonic_ns"), bool)
            for error in self.audio_loss_errors
        )
        recognized_errors = (
            len(recognized_error_shapes) == len(self.audio_loss_errors)
            and "alsa_device_disconnected" in recognized_error_shapes
        )
        all_exact_errors_precede_eos = all(
            isinstance(error.get("observed_monotonic_ns"), int)
            and not isinstance(error.get("observed_monotonic_ns"), bool)
            and cast(int, error["observed_monotonic_ns"]) < observation.observed_monotonic_ns
            for error in self.audio_loss_errors
        )
        post_error_discovery = [
            item
            for item in self.loss_discovery_observations
            if isinstance(item.get("observed_monotonic_ns"), int)
            and not isinstance(item.get("observed_monotonic_ns"), bool)
            and isinstance(first_error_ns, int)
            and cast(int, item["observed_monotonic_ns"]) > first_error_ns
        ]
        first_not_found_sequence = confirmed.get("first_not_found_sequence")
        second_not_found_sequence = confirmed.get("second_not_found_sequence")
        first_pair_indexes = [
            index
            for index, item in enumerate(self.loss_discovery_observations)
            if item.get("sequence") == first_not_found_sequence
        ]
        second_pair_indexes = [
            index
            for index, item in enumerate(self.loss_discovery_observations)
            if item.get("sequence") == second_not_found_sequence
        ]
        pair_indexes = (
            (first_pair_indexes[0], second_pair_indexes[0])
            if len(first_pair_indexes) == 1 and len(second_pair_indexes) == 1
            else None
        )
        first_pair_observation = (
            self.loss_discovery_observations[pair_indexes[0]] if pair_indexes is not None else None
        )
        second_pair_observation = (
            self.loss_discovery_observations[pair_indexes[1]] if pair_indexes is not None else None
        )
        stable_pair_verified = (
            confirmed.get("trigger") == "stable_identity_not_found"
            and pair_indexes is not None
            and pair_indexes[1] == pair_indexes[0] + 1
            and isinstance(first_pair_observation, Mapping)
            and isinstance(second_pair_observation, Mapping)
            and first_pair_observation.get("status") == AudioDiscoveryStatus.NOT_FOUND.value
            and first_pair_observation.get("device_exposed") is False
            and second_pair_observation.get("status") == AudioDiscoveryStatus.NOT_FOUND.value
            and second_pair_observation.get("device_exposed") is False
            and first_pair_observation.get("observed_monotonic_ns") == first_not_found_ns
            and second_pair_observation.get("observed_monotonic_ns") == second_not_found_ns
            and self.loss_discovery_observations[-1] is second_pair_observation
            and self.discovery_window_open is False
        )
        no_invalid_status_after_error = all(
            item.get("status") == AudioDiscoveryStatus.NOT_FOUND.value
            and item.get("device_exposed") is False
            for item in post_error_discovery
        )
        post_eos_discovery = [
            item
            for item in self.loss_discovery_observations
            if isinstance(item.get("observed_monotonic_ns"), int)
            and not isinstance(item.get("observed_monotonic_ns"), bool)
            and cast(int, item["observed_monotonic_ns"]) > observation.observed_monotonic_ns
        ]
        no_rematch_after_eos = all(
            item.get("status") == AudioDiscoveryStatus.NOT_FOUND.value
            and item.get("device_exposed") is False
            for item in post_eos_discovery
        )
        video_dispatches = [
            dispatch
            for dispatch in self.eos_dispatch_evidence
            if dispatch.get("label") == "loss-retired-video"
        ]
        video_dispatch_started_ns = (
            video_dispatches[0].get("started_monotonic_ns") if len(video_dispatches) == 1 else None
        )
        natural_eos_timing_class = (
            "after_error_before_first_not_found"
            if (
                isinstance(first_not_found_ns, int)
                and observation.observed_monotonic_ns < first_not_found_ns
            )
            else "between_stable_not_found_pair"
            if (
                isinstance(first_not_found_ns, int)
                and isinstance(second_not_found_ns, int)
                and first_not_found_ns < observation.observed_monotonic_ns < second_not_found_ns
            )
            else "after_stable_not_found_pair_before_handoff"
            if (
                isinstance(second_not_found_ns, int)
                and second_not_found_ns < observation.observed_monotonic_ns
            )
            else None
        )
        final_absence_check = (
            self.natural_eos_final_absence_checks[-1]
            if self.natural_eos_final_absence_checks
            else None
        )
        final_absence_check_verified = (
            natural_eos_timing_class
            in {
                "after_error_before_first_not_found",
                "between_stable_not_found_pair",
            }
            and not self.natural_eos_final_absence_checks
        ) or (
            natural_eos_timing_class == "after_stable_not_found_pair_before_handoff"
            and len(self.natural_eos_final_absence_checks) == 1
            and isinstance(final_absence_check, Mapping)
            and final_absence_check.get("attempts") == 1
            and final_absence_check.get("required") is True
            and final_absence_check.get("status") == AudioDiscoveryStatus.NOT_FOUND.value
            and final_absence_check.get("device_exposed") is False
            and final_absence_check.get("before_topology_mutation") is True
            and isinstance(final_absence_check.get("observed_monotonic_ns"), int)
            and not isinstance(
                final_absence_check.get("observed_monotonic_ns"),
                bool,
            )
            and cast(int, final_absence_check["observed_monotonic_ns"])
            > observation.observed_monotonic_ns
            and isinstance(video_dispatch_started_ns, int)
            and cast(int, final_absence_check["observed_monotonic_ns"]) < video_dispatch_started_ns
        )
        timing_verified = (
            exact_errors
            and recognized_errors
            and all_exact_errors_precede_eos
            and isinstance(first_error_ns, int)
            and not isinstance(first_error_ns, bool)
            and isinstance(first_not_found_ns, int)
            and not isinstance(first_not_found_ns, bool)
            and isinstance(second_not_found_ns, int)
            and not isinstance(second_not_found_ns, bool)
            and armed_ns <= first_error_ns
            and first_error_ns < observation.observed_monotonic_ns
            and first_not_found_ns < second_not_found_ns
            and natural_eos_timing_class is not None
            and stable_pair_verified
            and no_invalid_status_after_error
            and final_absence_check_verified
            and observation.pad_path == initial_pad_identity["path"]
            and observation.pad_name == initial_pad_identity["name"]
            and observation.parent_path == initial_pad_identity["parent_path"]
            and observation.peer_path == initial_pad_identity["peer_path"]
            and observation.generation_number == old.number
            and observation.generation_external_linked
            and not observation.generation_valve_drop
            and not observation.generation_retired
            and observation.active_identity_verified
            and observation.forwarded_to_splitmux
            and not observation.duplicate_refused
            and no_rematch_after_eos
            and isinstance(video_dispatch_started_ns, int)
            and not isinstance(video_dispatch_started_ns, bool)
            and observation.observed_monotonic_ns < video_dispatch_started_ns
            and old.audio_eos_seen
        )
        record.update(
            {
                "loss_attribution_timing_verified": timing_verified,
                "loss_window_armed_monotonic_ns": armed_ns,
                "first_exact_audio_error_monotonic_ns": first_error_ns,
                "first_not_found_monotonic_ns": first_not_found_ns,
                "second_not_found_monotonic_ns": second_not_found_ns,
                "exact_audio_errors_verified": exact_errors,
                "recognized_audio_error_shapes": recognized_error_shapes,
                "recognized_audio_errors_verified": recognized_errors,
                "all_exact_audio_errors_precede_natural_eos": (all_exact_errors_precede_eos),
                "stable_not_found_pair_verified": stable_pair_verified,
                "discovery_closed_at_stable_pair": stable_pair_verified,
                "no_invalid_discovery_status_after_first_error": (no_invalid_status_after_error),
                "no_rematch_after_natural_eos": no_rematch_after_eos,
                "post_natural_eos_discovery_count": len(post_eos_discovery),
                "natural_eos_timing_class": natural_eos_timing_class,
                "final_post_eos_absence_check_required": (
                    natural_eos_timing_class == "after_stable_not_found_pair_before_handoff"
                ),
                "final_post_eos_absence_check_verified": (final_absence_check_verified),
                "final_post_eos_absence_check": (
                    dict(final_absence_check) if isinstance(final_absence_check, Mapping) else None
                ),
                "video_dispatch_started_monotonic_ns": video_dispatch_started_ns,
                "delta_from_first_exact_audio_error_ns": (
                    observation.observed_monotonic_ns - first_error_ns
                    if isinstance(first_error_ns, int)
                    else None
                ),
                "delta_to_first_not_found_ns": (
                    first_not_found_ns - observation.observed_monotonic_ns
                    if isinstance(first_not_found_ns, int)
                    else None
                ),
                "delta_from_first_not_found_ns": (
                    observation.observed_monotonic_ns - first_not_found_ns
                    if isinstance(first_not_found_ns, int)
                    else None
                ),
                "delta_from_second_not_found_ns": (
                    observation.observed_monotonic_ns - second_not_found_ns
                    if isinstance(second_not_found_ns, int)
                    else None
                ),
                "audio_probe_observed_before_handoff": bool(old.audio_eos_seen),
            }
        )
        if not timing_verified:
            raise HarnessError("natural retired audio EOS loss attribution differs")
        if any(
            dispatch.get("label") == "loss-retired-audio" for dispatch in self.eos_dispatch_evidence
        ):
            raise HarnessError("natural retired audio EOS path had an audio dispatch attempt")
        self._prove_retired_audio_queue_stable(old, record, deadline=deadline)
        record["effective_delivery_observed"] = True
        self._record_event(
            "loss_attributed_natural_audio_eos_accepted",
            event_seqnum=observation.seqnum,
            observed_monotonic_ns=observation.observed_monotonic_ns,
            first_exact_audio_error_monotonic_ns=first_error_ns,
            first_not_found_monotonic_ns=first_not_found_ns,
            audio_dispatch_attempt_count=0,
        )
        return record

    def _join_eos_workers_after_null(self) -> None:
        deadline = time.monotonic() + EOS_DISPATCH_TIMEOUT_SECONDS
        for dispatch in tuple(self._eos_dispatches):
            remaining = deadline - time.monotonic()
            if remaining > 0:
                dispatch.done.wait(remaining)
            thread = dispatch.thread
            if thread is not None:
                thread.join(timeout=max(deadline - time.monotonic(), 0))
            if thread is None or thread.is_alive():
                raise HarnessError(f"{dispatch.label} EOS worker survived forced parent NULL")
            self._eos_dispatches.remove(dispatch)

    def _release_audio_idle_blocks_after_parent_null(self) -> None:
        for pad, probe in self._retained_audio_idle_probes:
            pad.remove_probe(probe)
        if self._retained_audio_idle_probes:
            self._record_event(
                "retained_audio_idle_blocks_released_after_parent_null",
                count=len(self._retained_audio_idle_probes),
            )
        self._retained_audio_idle_probes.clear()

    def _release_audio_idle_blocks_before_failure_null(self) -> None:
        released = 0
        for pad, probe in tuple(self._retained_audio_idle_probes):
            pad.remove_probe(probe)
            self._retained_audio_idle_probes.remove((pad, probe))
            released += 1
        if released:
            self._record_event(
                "audio_idle_blocks_released_before_failure_parent_null",
                count=released,
                permanent_output_arbiter_remains=True,
            )

    def _transition_parent_null_bounded(self) -> tuple[Any, Any]:
        transition = self._null_transition
        if transition is None:
            transition = _NullTransition(started_monotonic_ns=time.monotonic_ns())

            def worker() -> None:
                try:
                    transition.null_return = self.pipeline.set_state(self.gst.State.NULL)
                    transition.waited, transition.state, _pending = self.pipeline.get_state(
                        15 * self.gst.SECOND
                    )
                except BaseException as error:
                    transition.error = error
                finally:
                    transition.ended_monotonic_ns = time.monotonic_ns()
                    transition.done.set()

            created_thread = threading.Thread(
                target=worker,
                name="m7-parent-null",
                daemon=True,
            )
            transition.thread = created_thread
            self._null_transition = transition
            created_thread.start()
        elapsed_seconds = (time.monotonic_ns() - transition.started_monotonic_ns) / 1e9
        remaining = NULL_TRANSITION_TIMEOUT_SECONDS - elapsed_seconds
        if remaining <= 0 or not transition.done.wait(remaining):
            raise HarnessError(
                "parent NULL transition worker exceeded its bounded deadline; "
                "external process timeout is the final containment"
            )
        observed_thread = transition.thread
        if observed_thread is None:
            raise HarnessError("parent NULL transition worker identity was lost")
        observed_thread.join(timeout=0)
        if observed_thread.is_alive():
            raise HarnessError("parent NULL transition worker did not terminate")
        if transition.error is not None:
            raise HarnessError(
                f"parent NULL transition failed: {_shared._bounded_detail(transition.error)}"
            )
        if (
            transition.null_return == self.gst.StateChangeReturn.FAILURE
            or transition.waited == self.gst.StateChangeReturn.FAILURE
            or transition.state != self.gst.State.NULL
        ):
            raise HarnessError("parent pipeline did not reach NULL")
        return transition.null_return, transition.waited

    def _assert_control_workers_stopped(self) -> None:
        if self._eos_dispatches:
            raise HarnessError("EOS dispatch worker ownership remained at success")
        transition = self._null_transition
        if (
            transition is None
            or transition.thread is None
            or transition.thread.is_alive()
            or not transition.done.is_set()
        ):
            raise HarnessError("parent NULL worker ownership remained at success")

    def _video_path_state(self, element: Any) -> dict[str, object]:
        try:
            change, current, pending = element.get_state(0)
            return {
                "change_return": int(change),
                "current": int(current),
                "pending": int(pending),
                "locked": bool(element.is_locked_state()),
            }
        except BaseException as error:
            return {"snapshot_error": _shared._bounded_detail(error)}

    def _strict_element_state(self, element: Any, timeout_ns: int = 0) -> dict[str, object]:
        try:
            change, current, pending = element.get_state(timeout_ns)
        except BaseException as error:
            raise HarnessError(
                f"successor state query failed: {_shared._bounded_detail(error)}"
            ) from error
        return {
            "change_return": int(change),
            "current": int(current),
            "pending": int(pending),
            "current_name": _shared._bounded_detail(current),
            "pending_name": _shared._bounded_detail(pending),
        }

    def _known_degraded_parent_state_evidence(self) -> dict[str, object] | None:
        confirmed = self.confirmed_physical_loss
        stable_pair = False
        if (
            isinstance(confirmed, Mapping)
            and confirmed.get("trigger") == "stable_identity_not_found"
            and self.discovery_window_open is False
        ):
            first_sequence = confirmed.get("first_not_found_sequence")
            second_sequence = confirmed.get("second_not_found_sequence")
            first = [
                item
                for item in self.loss_discovery_observations
                if item.get("sequence") == first_sequence
            ]
            second = [
                item
                for item in self.loss_discovery_observations
                if item.get("sequence") == second_sequence
            ]
            stable_pair = (
                len(first) == 1
                and len(second) == 1
                and first[0].get("status") == AudioDiscoveryStatus.NOT_FOUND.value
                and first[0].get("device_exposed") is False
                and second[0].get("status") == AudioDiscoveryStatus.NOT_FOUND.value
                and second[0].get("device_exposed") is False
                and first[0].get("observed_monotonic_ns")
                == confirmed.get("first_not_found_monotonic_ns")
                and second[0].get("observed_monotonic_ns")
                == confirmed.get("second_not_found_monotonic_ns")
                and isinstance(confirmed.get("separation_ns"), int)
                and not isinstance(confirmed.get("separation_ns"), bool)
                and cast(int, confirmed["separation_ns"]) >= STABLE_NOT_FOUND_SEPARATION_NS
            )
        recognized_shapes: list[str] = []
        exact_errors = bool(self.audio_loss_errors)
        for error in self.audio_loss_errors:
            shape = None
            if (
                error.get("error_domain") == "gst-resource-error-quark"
                and error.get("error_code") == 9
                and error.get("error_message")
                == "Error recording from audio device. The device has been disconnected."
                and "gst_alsasrc_read" in cast(str, error.get("debug", ""))
            ):
                shape = "alsa_device_disconnected"
            elif (
                error.get("error_domain") == "gst-stream-error-quark"
                and error.get("error_code") == 1
                and error.get("error_message") == "Internal data stream error."
                and "gst_base_src_loop" in cast(str, error.get("debug", ""))
                and "reason error (-5)" in cast(str, error.get("debug", ""))
            ):
                shape = "base_source_stream_error"
            if shape is not None:
                recognized_shapes.append(shape)
            exact_errors = (
                exact_errors
                and error.get("accepted_loss_burst") is True
                and error.get("exact_registered_audio_source") is True
                and error.get("source_path") == self.registered_audio_source_path
                and isinstance(error.get("observed_monotonic_ns"), int)
                and not isinstance(error.get("observed_monotonic_ns"), bool)
            )
        recognized_errors = (
            exact_errors
            and len(recognized_shapes) == len(self.audio_loss_errors)
            and "alsa_device_disconnected" in recognized_shapes
        )
        self._assert_parent_identity()
        clean_safety_state = (
            not self.unexpected_bus_errors
            and not self.warnings
            and not self.errors
            and self.clock is not None
            and isinstance(self.base_time_ns, int)
            and not isinstance(self.base_time_ns, bool)
            and self.base_time_ns > 0
            and self.initial_new_clock_seen
        )
        if not (stable_pair and recognized_errors and clean_safety_state):
            return None
        return {
            "stable_identity_loss_verified": True,
            "recognized_exact_audio_errors": True,
            "recognized_audio_error_shapes": recognized_shapes,
            "exact_audio_error_count": len(self.audio_loss_errors),
            "parent_object_identity_verified": True,
            "unexpected_bus_error_count": 0,
            "warning_count": 0,
            "error_count": 0,
            "clock_identity_present": True,
        }

    def _active_splitmux_child_states(self, successor: Any) -> list[dict[str, object]]:
        for _attempt in range(3):
            iterator = successor.output.iterate_recurse()
            children: list[dict[str, object]] = []
            while True:
                result, child = iterator.next()
                if result == self.gst.IteratorResult.DONE:
                    return sorted(
                        children,
                        key=lambda item: cast(str, item["path"]),
                    )
                if result == self.gst.IteratorResult.RESYNC:
                    break
                if result != self.gst.IteratorResult.OK or child is None:
                    raise HarnessError("splitmux child iterator failed")
                factory = child.get_factory()
                factory_name = (
                    _shared._bounded_detail(factory.get_name()) if factory is not None else ""
                )
                if factory_name in ("mp4mux", "filesink"):
                    children.append(
                        {
                            "factory": factory_name,
                            "path": _shared._bounded_detail(child.get_path_string()),
                            "state": self._strict_element_state(child),
                        }
                    )
        raise HarnessError("splitmux child iterator did not stabilize")

    def _successor_state_bundle(self, successor: Any) -> dict[str, object]:
        return {
            "parent": self._strict_element_state(self.pipeline),
            "bin": self._strict_element_state(successor.bin),
            "output": self._strict_element_state(successor.output),
            "video_valve": self._strict_element_state(successor.video_valve),
            "video_queue": self._strict_element_state(successor.video_queue),
            "active_splitmux_children": self._active_splitmux_child_states(successor),
            "video_queue_levels": {
                "current_buffers": int(successor.video_queue.get_property("current-level-buffers")),
                "current_bytes": int(successor.video_queue.get_property("current-level-bytes")),
                "current_time_ns": int(successor.video_queue.get_property("current-level-time")),
                "maximum_buffers": int(successor.video_queue.get_property("max-size-buffers")),
                "maximum_bytes": int(successor.video_queue.get_property("max-size-bytes")),
                "maximum_time_ns": int(successor.video_queue.get_property("max-size-time")),
            },
            "video_counters": {
                "camera_raw": self.camera_source_counter.snapshot(),
                "parent_encoded": self.video_source_counter.snapshot(),
                "successor": successor.video_counter.snapshot(),
            },
            "bin_locked": bool(successor.bin.is_locked_state()),
            "external_linked": bool(successor.external_linked),
            "video_valve_drop": bool(successor.video_valve.get_property("drop")),
            "tee_peer_is_exact_ghost": (
                successor.video_tee_pad.get_peer() is successor.video_ghost
            ),
        }

    def _converge_successor_state_after_preroll(
        self,
        successor: Any,
        *,
        initial_sync: Mapping[str, object],
    ) -> dict[str, object]:
        if self.successor_state_convergence:
            raise HarnessError("successor state convergence was attempted more than once")
        observed_ns = time.monotonic_ns()
        deadline = time.monotonic() + SUCCESSOR_STATE_CONVERGENCE_TIMEOUT_SECONDS
        self._assert_parent_identity()
        initial_bundle = self._successor_state_bundle(successor)
        record: dict[str, object] = {
            "sequence": 1,
            "observed_after_first_buffer_monotonic_ns": observed_ns,
            "initial_sync": dict(initial_sync),
            "post_preroll": initial_bundle,
            "parent_object_identity_verified": True,
            "parent_state_query_known_degraded_after_audio_source_failure": False,
            "parent_known_degraded_evidence": None,
            "grace_attempted": False,
            "grace_seconds": 0.0,
            "post_grace": None,
            "correction_required": None,
            "correction_count": 0,
            "set_playing_return": None,
            "set_playing_return_name": None,
            "set_playing_started_monotonic_ns": None,
            "set_playing_ended_monotonic_ns": None,
            "final": None,
            "video_progress": None,
            "converged": False,
        }
        self.successor_state_convergence.append(record)

        def refuse(reason: str) -> NoReturn:
            record["refusal"] = reason
            raise HarnessError(reason)

        void_pending = int(self.gst.State.VOID_PENDING)
        paused = int(self.gst.State.PAUSED)
        playing = int(self.gst.State.PLAYING)
        async_return = int(self.gst.StateChangeReturn.ASYNC)
        failure_return = int(self.gst.StateChangeReturn.FAILURE)

        def terminal_shape(state: object, expected: int) -> bool:
            return (
                isinstance(state, Mapping)
                and state.get("current") == expected
                and state.get("pending") == void_pending
                and state.get("change_return") not in (async_return, failure_return)
            )

        parent_state = initial_bundle.get("parent")
        parent_known_degraded = (
            isinstance(parent_state, Mapping)
            and parent_state.get("current") == playing
            and parent_state.get("pending") == void_pending
            and parent_state.get("change_return") == failure_return
        )
        degraded_evidence = (
            self._known_degraded_parent_state_evidence() if parent_known_degraded else None
        )
        if parent_known_degraded:
            record["parent_state_query_known_degraded_after_audio_source_failure"] = (
                degraded_evidence is not None
            )
            record["parent_known_degraded_evidence"] = degraded_evidence

        def parent_playing(state: object) -> bool:
            return terminal_shape(state, playing) or (
                degraded_evidence is not None
                and isinstance(state, Mapping)
                and state.get("current") == playing
                and state.get("pending") == void_pending
                and state.get("change_return") == failure_return
                and not self.unexpected_bus_errors
                and not self.warnings
                and not self.errors
            )

        def bundle_playing(bundle: Mapping[str, object]) -> bool:
            children = bundle.get("active_splitmux_children")
            return (
                parent_playing(bundle.get("parent"))
                and terminal_shape(bundle.get("bin"), playing)
                and terminal_shape(bundle.get("output"), playing)
                and terminal_shape(bundle.get("video_valve"), playing)
                and terminal_shape(bundle.get("video_queue"), playing)
                and isinstance(children, list)
                and len(children) >= 2
                and {child.get("factory") for child in children if isinstance(child, Mapping)}
                == {"mp4mux", "filesink"}
                and all(
                    isinstance(child, Mapping) and terminal_shape(child.get("state"), playing)
                    for child in children
                )
                and not queue_full(bundle)
            )

        def queue_full(bundle: Mapping[str, object]) -> bool:
            levels = bundle.get("video_queue_levels")
            if not isinstance(levels, Mapping):
                return True
            return any(
                isinstance(levels.get(current), int)
                and not isinstance(levels.get(current), bool)
                and isinstance(levels.get(maximum), int)
                and not isinstance(levels.get(maximum), bool)
                and cast(int, levels[maximum]) > 0
                and cast(int, levels[current]) >= cast(int, levels[maximum])
                for current, maximum in (
                    ("current_buffers", "maximum_buffers"),
                    ("current_bytes", "maximum_bytes"),
                    ("current_time_ns", "maximum_time_ns"),
                )
            )

        def exact_paused_void(bundle: Mapping[str, object]) -> bool:
            children = bundle.get("active_splitmux_children")
            return (
                terminal_shape(bundle.get("bin"), paused)
                and terminal_shape(bundle.get("output"), paused)
                and terminal_shape(bundle.get("video_valve"), paused)
                and terminal_shape(bundle.get("video_queue"), paused)
                and isinstance(children, list)
                and len(children) >= 2
                and {child.get("factory") for child in children if isinstance(child, Mapping)}
                == {"mp4mux", "filesink"}
                and all(
                    isinstance(child, Mapping) and terminal_shape(child.get("state"), paused)
                    for child in children
                )
            )

        initial_counters = initial_bundle.get("video_counters")
        if not isinstance(initial_counters, Mapping):
            refuse("post-preroll video counters are absent")
        initial_counts: dict[str, int] = {}
        for name in ("camera_raw", "parent_encoded", "successor"):
            counter = initial_counters.get(name)
            count = counter.get("count") if isinstance(counter, Mapping) else None
            if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
                refuse("post-preroll video counters were not freshly observed")
            initial_counts[name] = count
        record["video_progress"] = {
            "initial_counts": dict(initial_counts),
            "final_counts": None,
            "deltas": None,
            "verified": False,
        }

        def finish_with_progress(candidate: Mapping[str, object]) -> dict[str, object]:
            last_bundle = candidate
            while True:
                counters = last_bundle.get("video_counters")
                final_counts: dict[str, int] = {}
                if isinstance(counters, Mapping):
                    for name in ("camera_raw", "parent_encoded", "successor"):
                        counter = counters.get(name)
                        count = counter.get("count") if isinstance(counter, Mapping) else None
                        if isinstance(count, int) and not isinstance(count, bool):
                            final_counts[name] = count
                if len(final_counts) == 3 and all(
                    final_counts[name] > initial_counts[name] for name in initial_counts
                ):
                    self._assert_parent_identity()
                    if not bundle_playing(last_bundle):
                        refuse("successor/parent state drifted during video progress proof")
                    progress = cast(dict[str, object], record["video_progress"])
                    progress["final_counts"] = final_counts
                    progress["deltas"] = {
                        name: final_counts[name] - initial_counts[name] for name in initial_counts
                    }
                    progress["verified"] = True
                    record["final"] = last_bundle
                    converged_ns = time.monotonic_ns()
                    record["converged_monotonic_ns"] = converged_ns
                    record["convergence_duration_ns"] = converged_ns - observed_ns
                    record["converged"] = True
                    return record
                if time.monotonic() >= deadline:
                    record["final"] = last_bundle
                    refuse("parent/shared/successor video progress proof timed out")
                self._drain_bus_once(
                    min(
                        50_000_000,
                        max(0, int((deadline - time.monotonic()) * self.gst.SECOND)),
                    )
                )
                last_bundle = self._successor_state_bundle(successor)
                if queue_full(last_bundle):
                    refuse("successor video queue filled during video progress proof")

        if (
            initial_bundle.get("bin_locked") is not False
            or initial_bundle.get("external_linked") is not True
            or initial_bundle.get("video_valve_drop") is not False
            or initial_bundle.get("tee_peer_is_exact_ghost") is not True
            or not parent_playing(initial_bundle.get("parent"))
        ):
            refuse("successor topology/parent state differed at post-preroll gate")
        if queue_full(initial_bundle):
            refuse("successor video queue reached its configured bound before convergence")
        if bundle_playing(initial_bundle):
            record["correction_required"] = False
            return finish_with_progress(initial_bundle)

        initial_bin = initial_bundle.get("bin")
        if (
            isinstance(initial_bin, Mapping)
            and initial_bin.get("current") == paused
            and initial_bin.get("pending") == playing
            and initial_bin.get("change_return") == async_return
        ):
            record["grace_attempted"] = True
            record["grace_seconds"] = SUCCESSOR_STATE_GRACE_SECONDS
            grace_deadline = min(
                deadline,
                time.monotonic() + SUCCESSOR_STATE_GRACE_SECONDS,
            )
            while time.monotonic() < grace_deadline:
                self._drain_bus_once(
                    min(
                        50_000_000,
                        max(0, int((grace_deadline - time.monotonic()) * self.gst.SECOND)),
                    )
                )
                grace_bundle = self._successor_state_bundle(successor)
                record["post_grace"] = grace_bundle
                if queue_full(grace_bundle):
                    refuse("successor video queue filled during state grace")
                if bundle_playing(grace_bundle):
                    record["correction_required"] = False
                    return finish_with_progress(grace_bundle)
            candidate = cast(Mapping[str, object], record["post_grace"])
        else:
            candidate = initial_bundle
        if not exact_paused_void(candidate):
            refuse("successor state was not PLAYING or exact PAUSED/VOID after grace")

        record["correction_required"] = True
        record["correction_count"] = 1
        correction_started_ns = time.monotonic_ns()
        record["set_playing_started_monotonic_ns"] = correction_started_ns
        set_playing_return = successor.bin.set_state(self.gst.State.PLAYING)
        correction_ended_ns = time.monotonic_ns()
        record["set_playing_ended_monotonic_ns"] = correction_ended_ns
        record["set_playing_duration_ns"] = correction_ended_ns - correction_started_ns
        record["set_playing_return"] = int(set_playing_return)
        record["set_playing_return_name"] = _shared._bounded_detail(set_playing_return)
        if set_playing_return == self.gst.StateChangeReturn.FAILURE:
            refuse("successor explicit PLAYING correction was refused")

        while True:
            final_bundle = self._successor_state_bundle(successor)
            record["final"] = final_bundle
            if queue_full(final_bundle):
                refuse("successor video queue filled before PLAYING convergence")
            if bundle_playing(final_bundle):
                break
            if time.monotonic() >= deadline:
                refuse("successor state convergence to PLAYING timed out")
            self._drain_bus_once(
                min(
                    50_000_000,
                    max(0, int((deadline - time.monotonic()) * self.gst.SECOND)),
                )
            )
        return finish_with_progress(final_bundle)

    def _video_pad_state(self, pad: Any | None) -> dict[str, object]:
        if pad is None:
            return {"present": False}
        try:
            peer = pad.get_peer()
            return {
                "present": True,
                "path": _shared._bounded_detail(pad.get_path_string()),
                "active": bool(pad.is_active()),
                "blocked": bool(pad.is_blocked()),
                "blocking": bool(pad.is_blocking()),
                "linked": bool(pad.is_linked()),
                "peer_path": _shared._bounded_detail(
                    peer.get_path_string() if peer is not None else None
                ),
            }
        except BaseException as error:
            return {
                "present": True,
                "snapshot_error": _shared._bounded_detail(error),
            }

    def _capture_video_path_diagnostic(
        self,
        stage: str,
        old: Any,
        successor: Any,
        *,
        handoff: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        if len(self.video_path_diagnostics) >= MAX_VIDEO_PATH_DIAGNOSTICS:
            raise HarnessError("video-path diagnostic evidence exceeded its bound")
        files: list[dict[str, object]] = []
        for name in successor.opened_locations:
            path = self.output_directory / name
            try:
                info = os.lstat(path)
                files.append(
                    {
                        "name": name,
                        "regular": stat.S_ISREG(info.st_mode),
                        "size_bytes": info.st_size,
                    }
                )
            except OSError as error:
                files.append(
                    {
                        "name": name,
                        "lstat_error": _shared._bounded_detail(error),
                    }
                )
        video_tee_sink = self.video_tee.get_static_pad("sink")
        successor_tee_peer = successor.video_tee_pad.get_peer()
        diagnostic: dict[str, object] = {
            "sequence": len(self.video_path_diagnostics) + 1,
            "stage": stage,
            "observed_monotonic_ns": time.monotonic_ns(),
            "camera_raw": self.camera_source_counter.snapshot(),
            "parent_encoded": self.video_source_counter.snapshot(),
            "old_generation": {
                "number": old.number,
                "video": old.video_counter.snapshot(),
                "external_linked": old.external_linked,
                "video_valve_drop": bool(old.video_valve.get_property("drop")),
                "state": self._video_path_state(old.bin),
            },
            "successor_generation": {
                "number": successor.number,
                "video": successor.video_counter.snapshot(),
                "external_linked": successor.external_linked,
                "video_valve_drop": bool(successor.video_valve.get_property("drop")),
                "opened_locations": list(successor.opened_locations),
                "closed_locations": list(successor.closed_locations),
                "state": self._video_path_state(successor.bin),
                "tee_peer_is_exact_ghost": successor_tee_peer is successor.video_ghost,
            },
            "pipeline_state": self._video_path_state(self.pipeline),
            "video_tee_sink": self._video_pad_state(video_tee_sink),
            "successor_video_tee_pad": self._video_pad_state(successor.video_tee_pad),
            "retained_audio_idle_probe_count": len(self._retained_audio_idle_probes),
            "files": files,
        }
        if handoff is not None:
            held = cast(Mapping[str, object], handoff["held"])
            diagnostic["idr_release"] = {
                "probe": handoff.get("probe"),
                "release_requested": cast(Any, handoff["release"]).is_set(),
                "callback_returning": cast(Any, handoff["callback_returning"]).is_set(),
                "callback_returning_monotonic_ns": held.get("callback_returning_monotonic_ns"),
                "release_requested_monotonic_ns": handoff.get("release_requested_monotonic_ns"),
            }
        self.video_path_diagnostics.append(diagnostic)
        self._record_event(
            "video_path_diagnostic",
            sequence=diagnostic["sequence"],
            stage=stage,
        )
        return diagnostic

    def _block_next_video_idr(self) -> dict[str, object]:
        video_sink = self.video_tee.get_static_pad("sink")
        if video_sink is None:
            raise HarnessError("common encoded-video tee input is absent")
        segment_event = video_sink.get_sticky_event(self.gst.EventType.SEGMENT, 0)
        if segment_event is None:
            raise HarnessError("encoded-video input has no sticky segment")
        segment = segment_event.parse_segment()
        if segment is None or segment.format != self.gst.Format.TIME:
            raise HarnessError("encoded-video input sticky segment is not TIME")
        reached = threading.Event()
        release = threading.Event()
        callback_returning = threading.Event()
        held: dict[str, int] = {}
        probe_errors: list[str] = []

        def block_video(_pad: Any, info: Any) -> Any:
            buffer = info.get_buffer()
            if buffer is None or buffer.has_flags(self.gst.BufferFlags.DELTA_UNIT):
                return self.gst.PadProbeReturn.PASS
            pts_ns = int(buffer.pts)
            try:
                running_ns = int(segment.to_running_time(self.gst.Format.TIME, pts_ns))
                if not 0 <= pts_ns < (1 << 63) or not 0 <= running_ns < (1 << 63):
                    raise HarnessError("loss-handoff video time is invalid")
            except BaseException as error:
                probe_errors.append(_shared._bounded_detail(error))
                reached.set()
                return self.gst.PadProbeReturn.OK
            held["pts_ns"] = pts_ns
            held["running_time_ns"] = running_ns
            held["blocked_monotonic_ns"] = time.monotonic_ns()
            reached.set()
            if not release.wait(VIDEO_IDR_RELEASE_TIMEOUT_SECONDS):
                probe_errors.append("video IDR release request exceeded its bounded deadline")
            held["callback_returning_monotonic_ns"] = time.monotonic_ns()
            callback_returning.set()
            return self.gst.PadProbeReturn.REMOVE

        probe = video_sink.add_probe(
            self.gst.PadProbeType.BLOCK | self.gst.PadProbeType.BUFFER,
            block_video,
        )
        if not probe:
            raise HarnessError("video IDR block probe was refused")
        try:
            self._wait_for(
                reached.is_set,
                timeout_seconds=3.0,
                reason="post-loss encoded IDR block",
            )
            if probe_errors:
                raise HarnessError(f"video IDR probe failed: {probe_errors[0]}")
        except BaseException:
            video_sink.remove_probe(probe)
            raise
        return {
            "sink": video_sink,
            "probe": probe,
            "pts_ns": held["pts_ns"],
            "running_time_ns": held["running_time_ns"],
            "blocked_monotonic_ns": held["blocked_monotonic_ns"],
            "release": release,
            "callback_returning": callback_returning,
            "probe_errors": probe_errors,
            "held": held,
        }

    def _release_blocked_video_idr(self, handoff: dict[str, object]) -> None:
        release = cast(threading.Event, handoff["release"])
        callback_returning = cast(threading.Event, handoff["callback_returning"])
        handoff["release_requested_monotonic_ns"] = time.monotonic_ns()
        release.set()
        deadline = time.monotonic() + 1.0
        if not callback_returning.wait(1.0):
            cast(Any, handoff["sink"]).remove_probe(cast(int, handoff["probe"]))
            raise HarnessError("video IDR block callback did not accept its release")
        sink = cast(Any, handoff["sink"])
        while bool(sink.is_blocked()) or bool(sink.is_blocking()):
            if time.monotonic() >= deadline:
                sink.remove_probe(cast(int, handoff["probe"]))
                raise HarnessError("common encoded-video pad remained blocked after IDR release")
            threading.Event().wait(0.005)
        probe_errors = cast(list[str], handoff["probe_errors"])
        if probe_errors:
            raise HarnessError(f"video IDR probe failed: {probe_errors[0]}")
        handoff["pad_unblocked_monotonic_ns"] = time.monotonic_ns()

    def _verify_final_post_eos_absence_before_topology(self, old: Any) -> None:
        pad = old.output_audio_pad
        if pad is None:
            raise HarnessError("final post-EOS absence check lacks exact audio pad")
        observations = self._output_audio_eos_snapshot(pad)
        if len(observations) != 1:
            return
        eos_ns = observations[0].observed_monotonic_ns
        recorded_after_eos = [
            item
            for item in self.loss_discovery_observations
            if isinstance(item.get("observed_monotonic_ns"), int)
            and not isinstance(item.get("observed_monotonic_ns"), bool)
            and cast(int, item["observed_monotonic_ns"]) > eos_ns
        ]
        if recorded_after_eos:
            return
        try:
            outcome = discover_capture_device(self.selector)
        except BaseException as error:
            evidence = {
                "attempts": 1,
                "required": True,
                "reason": "no_recorded_discovery_observation_after_natural_eos",
                "observed_monotonic_ns": time.monotonic_ns(),
                "natural_eos_observed_monotonic_ns": eos_ns,
                "status": "MALFORMED",
                "device_exposed": None,
                "before_topology_mutation": True,
                "error": _shared._bounded_detail(error),
            }
            self.natural_eos_final_absence_checks.append(evidence)
            raise HarnessError("final post-EOS exact-device discovery failed") from error
        status = getattr(outcome, "status", None)
        device = getattr(outcome, "device", object())
        evidence = {
            "attempts": 1,
            "required": True,
            "reason": "no_recorded_discovery_observation_after_natural_eos",
            "observed_monotonic_ns": time.monotonic_ns(),
            "natural_eos_observed_monotonic_ns": eos_ns,
            "status": (status.value if isinstance(status, AudioDiscoveryStatus) else "MALFORMED"),
            "device_exposed": device is not None,
            "before_topology_mutation": True,
        }
        if not isinstance(status, AudioDiscoveryStatus):
            evidence["status_detail"] = _shared._bounded_detail(status)
        self.natural_eos_final_absence_checks.append(evidence)
        if status is not AudioDiscoveryStatus.NOT_FOUND or device is not None:
            raise HarnessError("final post-EOS exact-device absence check did not prove NOT_FOUND")

    def switch_after_physical_loss(self, old: Any, successor: Any) -> None:
        if self.confirmed_physical_loss is None:
            raise HarnessError("loss handoff lacks stable-identity NOT_FOUND")
        if old.retired or successor.retired or not old.audio or successor.audio:
            raise HarnessError("physical-loss generation roles differ")
        if (
            old.ingress_event_error is not None
            or not old.video_first_buffer_had_sticky_contract
            or not old.audio_first_buffer_had_sticky_contract
        ):
            raise HarnessError("active A/V generation sticky/data contract failed")
        self._assert_parent_identity()
        handoff = self._block_next_video_idr()
        try:
            self._drain_safety_bus_quiet()
            self._close_discovery_trigger_window()
            active_locations = [
                location
                for location in old.opened_locations
                if location not in old.closed_locations
            ]
            if len(active_locations) != 1:
                raise HarnessError("retiring A/V generation has no unique active fragment")
            active_location = active_locations[0]
            started_ns = time.monotonic_ns()
            self._verify_final_post_eos_absence_before_topology(old)
            video_sink_pad = old.video_queue.get_static_pad("sink")
            if video_sink_pad is None:
                raise HarnessError("retired video queue sink is absent")
            if old.audio_queue is None:
                raise HarnessError("retired A/V audio queue is absent")
            audio_pad = old.output_audio_pad
            if audio_pad is None:
                raise HarnessError("retired exact output audio pad is absent")
            eos_deadline = time.monotonic() + EOS_DISPATCH_TIMEOUT_SECONDS
            (
                decision_evidence,
                natural_audio_eos_selected,
                reserved_manual_audio_eos,
            ) = self._serialize_audio_eos_branch(
                old,
                deadline=eos_deadline,
            )
            if not successor.bin.set_locked_state(False):
                raise HarnessError("video-only successor could not unlock")
            self._link_external(successor)
            initial_sync_started_ns = time.monotonic_ns()
            successor_sync_return = bool(successor.bin.sync_state_with_parent())
            initial_sync_ended_ns = time.monotonic_ns()
            initial_sync_evidence = {
                "count": 1,
                "started_monotonic_ns": initial_sync_started_ns,
                "ended_monotonic_ns": initial_sync_ended_ns,
                "duration_ns": initial_sync_ended_ns - initial_sync_started_ns,
                "return": successor_sync_return,
            }
            if not successor_sync_return:
                raise HarnessError("video-only successor could not follow parent state")
            self._set_generation_open(old, False)
            self._unlink_external(old)
            self._set_generation_open(successor, True)
            video_dispatch = self._start_downstream_eos(
                video_sink_pad,
                "loss-retired-video",
            )
            if natural_audio_eos_selected:
                eos_dispatches = self._await_eos_dispatches(
                    (video_dispatch,),
                    deadline=eos_deadline,
                )
                audio_eos_resolution = self._resolve_natural_retired_audio_eos(
                    old,
                    deadline=eos_deadline,
                )
                audio_eos_resolution["serialization_decision"] = decision_evidence
                audio_eos_primary_return: bool | None = None
            else:
                if reserved_manual_audio_eos is None:
                    raise HarnessError("manual audio EOS reservation identity was lost")
                audio_sink_pad = old.audio_queue.get_static_pad("sink")
                if audio_sink_pad is None:
                    raise HarnessError("retired dead-audio queue sink is absent")
                audio_dispatch = self._start_downstream_eos(
                    audio_sink_pad,
                    "loss-retired-audio",
                    event=reserved_manual_audio_eos,
                )
                eos_dispatches = self._await_eos_dispatches(
                    (video_dispatch, audio_dispatch),
                    allow_refused_labels=("loss-retired-audio",),
                    deadline=eos_deadline,
                )
                audio_eos_resolution = self._resolve_retired_audio_eos(
                    old,
                    eos_dispatches[1],
                    deadline=eos_deadline,
                    observation_count_before_primary=0,
                )
                audio_eos_resolution["serialization_decision"] = decision_evidence
                audio_eos_primary_return = cast(bool, eos_dispatches[1]["accepted"])
        finally:
            try:
                self._capture_video_path_diagnostic(
                    "handoff_before_idr_release",
                    old,
                    successor,
                    handoff=handoff,
                )
            finally:
                self._release_blocked_video_idr(handoff)
        probes_removed_ns = time.monotonic_ns()
        self._capture_video_path_diagnostic(
            "handoff_idr_released",
            old,
            successor,
            handoff=handoff,
        )
        self._wait_for(
            lambda: (
                active_location in old.closed_locations
                and set(old.opened_locations) == set(old.closed_locations)
                and old.video_eos_seen
                and old.audio_eos_seen
            ),
            timeout_seconds=15.0,
            reason="loss-retired A/V generation closure",
        )
        self._release_audio_idle_block_after_old_closure(old, decision_evidence)
        post_closure_audio_observations = self._output_audio_eos_snapshot(old.output_audio_pad)
        audio_eos_resolution["post_closure_output_audio_eos_observation_count"] = len(
            post_closure_audio_observations
        )
        audio_eos_resolution["post_closure_forwarded_audio_eos_count"] = sum(
            observation.forwarded_to_splitmux for observation in post_closure_audio_observations
        )
        audio_eos_resolution["post_closure_duplicate_audio_eos_refusal_count"] = sum(
            observation.duplicate_refused for observation in post_closure_audio_observations
        )
        if (
            old.ingress_event_error is not None
            or len(post_closure_audio_observations) != 1
            or post_closure_audio_observations[0].forwarded_to_splitmux is not True
            or post_closure_audio_observations[0].duplicate_refused is not False
        ):
            raise HarnessError("exact output audio EOS arbiter/closure contract failed")
        if audio_eos_resolution.get("delivery_mode") != "primary_queue_sink_accepted":
            pad = old.output_audio_pad
            peer = old.audio_queue.get_static_pad("src")
            post_identity = {
                "path": _shared._bounded_detail(pad.get_path_string()),
                "name": _shared._bounded_detail(pad.get_name()),
                "parent_path": _shared._bounded_detail(pad.get_parent_element().get_path_string()),
                "peer_path": _shared._bounded_detail(
                    peer.get_path_string() if peer is not None else None
                ),
            }
            audio_eos_resolution["post_closure_pad_identity"] = post_identity
            audio_eos_resolution["post_closure_pad_identity_verified"] = (
                pad is not None
                and old.output.get_static_pad("audio_0") is pad
                and pad.get_name() == "audio_0"
                and pad.get_parent_element() is old.output
                and peer is not None
                and pad.get_peer() is peer
                and peer.get_peer() is pad
                and post_identity == audio_eos_resolution.get("initial_pad_identity")
                and old.audio_eos_seen
            )
            if audio_eos_resolution["post_closure_pad_identity_verified"] is not True:
                raise HarnessError("direct audio EOS post-closure pad identity/probe differs")
        old_last = old.video_counter.last_pts_ns
        self._wait_for(
            lambda: successor.video_counter.count > 0,
            timeout_seconds=3.0,
            reason="video-only successor sticky/first data",
        )
        self._capture_video_path_diagnostic(
            "successor_first_video_buffer",
            old,
            successor,
            handoff=handoff,
        )
        if (
            successor.ingress_event_error is not None
            or not successor.video_first_buffer_had_sticky_contract
            or successor.video_counter.first_pts_ns is None
            or old_last is None
        ):
            raise HarnessError("video-only successor sticky/data contract failed")
        successor_state_convergence = self._converge_successor_state_after_preroll(
            successor,
            initial_sync=initial_sync_evidence,
        )
        first_successor_count = successor.video_counter.count
        try:
            self._wait_for(
                lambda: successor.video_counter.count >= first_successor_count + 30,
                timeout_seconds=3.0,
                reason="continuous post-handoff successor video",
            )
        except BaseException:
            self._capture_video_path_diagnostic(
                "successor_video_stall",
                old,
                successor,
                handoff=handoff,
            )
            raise
        self._capture_video_path_diagnostic(
            "successor_continuous_video_proven",
            old,
            successor,
            handoff=handoff,
        )
        raw_gap = successor.video_counter.first_pts_ns - old_last
        normalized_gap = abs(raw_gap - FRAME_PERIOD_NS)
        blocked_duration_ns = probes_removed_ns - cast(int, handoff["blocked_monotonic_ns"])
        transition = {
            "old_generation": old.number,
            "new_generation": successor.number,
            "trigger": "stable_identity_not_found",
            "stable_identity_loss": dict(self.confirmed_physical_loss),
            "gst_error_corroborated": bool(self.audio_loss_errors),
            "audio_loss_error_count": len(self.audio_loss_errors),
            "retired_active_location": active_location,
            "blocked_idr_pts_ns": handoff["pts_ns"],
            "blocked_video_running_time_ns": handoff["running_time_ns"],
            "old_last_video_pts_ns": old_last,
            "new_first_video_pts_ns": successor.video_counter.first_pts_ns,
            "raw_video_gap_ns": raw_gap,
            "normalized_video_gap_ns": normalized_gap,
            "within_one_frame": normalized_gap <= FRAME_PERIOD_NS,
            "new_first_video_is_idr": successor.video_counter.first_delta is False,
            "blocked_duration_ns": blocked_duration_ns,
            "closure_latency_ns": time.monotonic_ns() - started_ns,
            "eos_dispatches": eos_dispatches,
            "video_eos_return": eos_dispatches[0]["accepted"],
            "audio_eos_primary_return": audio_eos_primary_return,
            "audio_eos_dispatch_attempted": audio_eos_primary_return is not None,
            "audio_eos_dispatch_count": sum(
                dispatch.get("label") == "loss-retired-audio" for dispatch in eos_dispatches
            ),
            "audio_eos_effective_return": True,
            "audio_eos_fallback": audio_eos_resolution,
            "successor_sync_return": successor_sync_return,
            "successor_state_convergence": successor_state_convergence,
            "old_video_eos_observed": old.video_eos_seen,
            "old_audio_eos_observed": old.audio_eos_seen,
            "no_post_loss_audio_buffer_wait": True,
        }
        self.transitions.append(transition)
        if (
            transition["within_one_frame"] is not True
            or transition["new_first_video_is_idr"] is not True
            or blocked_duration_ns >= 2_000_000_000
        ):
            raise HarnessError("physical-loss handoff continuity/IDR bound failed")
        old.retired = True
        self._record_event(
            "loss_generation_drained",
            generation=old.number,
            left_attached_until_parent_null=True,
        )
        self._assert_parent_identity()

    def _wait_for_post_loss_video_only_fragments(
        self,
        old: Any,
        successor: Any,
    ) -> None:
        self._capture_video_path_diagnostic(
            "post_loss_fragment_wait_started",
            old,
            successor,
        )
        try:
            self._wait_for(
                lambda: len(successor.closed_locations) >= MIN_VIDEO_ONLY_FRAGMENTS,
                timeout_seconds=25.0,
                reason="post-loss video-only diagnostic fragments",
            )
        except BaseException:
            self._capture_video_path_diagnostic(
                "post_loss_fragment_stall",
                old,
                successor,
            )
            raise
        self._capture_video_path_diagnostic(
            "post_loss_fragment_wait_completed",
            old,
            successor,
        )

    def stop_video_only(self, final: Any) -> None:
        if (
            final.audio
            or final.ingress_event_error is not None
            or not final.video_first_buffer_had_sticky_contract
        ):
            raise HarnessError("final video-only generation contract failed")
        # Drain ordinary bus traffic while the recording path is still open.
        # Draining after the common tee is blocked would fill the bounded
        # upstream record queue and manufacture a larger shutdown tail.
        self._drain_safety_bus_quiet()
        common_tee_sink = self.video_tee.get_static_pad("sink")
        if common_tee_sink is None:
            raise HarnessError("common encoded-video tee input is absent")
        common_tee_ring: deque[dict[str, object]] = deque(
            maxlen=MAX_FINAL_COMMON_TEE_RING_BUFFERS
        )
        common_tee_state = {"total_count": 0, "evicted_count": 0}
        terminal_counter_probe_errors: list[str] = []

        def observe_common_tee(_pad: Any, info: Any) -> Any:
            buffer = info.get_buffer()
            if buffer is not None:
                common_tee_state["total_count"] += 1
                if len(common_tee_ring) == MAX_FINAL_COMMON_TEE_RING_BUFFERS:
                    common_tee_state["evicted_count"] += 1
                common_tee_ring.append(
                    {
                        "sequence": common_tee_state["total_count"],
                        "pts_ns": int(buffer.pts),
                        "observed_monotonic_ns": time.monotonic_ns(),
                        "delta_unit": bool(
                            buffer.has_flags(self.gst.BufferFlags.DELTA_UNIT)
                        ),
                    }
                )
            return self.gst.PadProbeReturn.OK

        common_tee_probe = common_tee_sink.add_probe(
            self.gst.PadProbeType.BUFFER,
            observe_common_tee,
        )
        if not common_tee_probe:
            raise HarnessError("common-tee terminal counter probe was refused")
        handoff = self._block_next_video_idr()
        common_tee_ring_at_block = list(common_tee_ring)
        held_tee_record = common_tee_ring_at_block[-1] if common_tee_ring_at_block else None
        common_tee_baseline_valid = (
            isinstance(held_tee_record, Mapping)
            and held_tee_record.get("pts_ns") == handoff["pts_ns"]
            and held_tee_record.get("delta_unit") is False
            and isinstance(held_tee_record.get("sequence"), int)
            and not isinstance(held_tee_record.get("sequence"), bool)
            and held_tee_record.get("sequence") == common_tee_state["total_count"]
        )
        common_tee_held_sequence = (
            cast(int, held_tee_record["sequence"])
            if common_tee_baseline_valid and held_tee_record is not None
            else None
        )
        common_tee_total_at_block = common_tee_state["total_count"]
        common_tee_evicted_at_block = common_tee_state["evicted_count"]
        common_tee_total_at_release: int | None = None
        parent_snapshot_at_block = self.video_source_counter.snapshot()
        parent_count_at_block = cast(int, parent_snapshot_at_block["count"])
        routed_count_at_block = sum(
            generation.video_counter.count for generation in self.generations.values()
        )
        terminal_drop_counter = _shared.PadCounter()
        terminal_drop_timing: dict[str, int | None] = {
            "first_observed_monotonic_ns": None,
            "last_observed_monotonic_ns": None,
        }
        parent_post_block_buffers: list[dict[str, object]] = []
        closed_valve_buffers: list[dict[str, object]] = []
        frozen_media_before_null: dict[str, object] | None = None
        try:
            parent_counter_pad = self._element("video_counter").get_static_pad("src")
            if parent_counter_pad is None:
                raise HarnessError("stable parent encoded-video boundary is absent")

            def observe_parent_post_block(_pad: Any, info: Any) -> Any:
                buffer = info.get_buffer()
                if buffer is not None:
                    if len(parent_post_block_buffers) >= MAX_FINAL_SHUTDOWN_MEDIA_PERIODS + 2:
                        if not terminal_counter_probe_errors:
                            terminal_counter_probe_errors.append(
                                "parent post-block buffer evidence exceeded its bound"
                            )
                    else:
                        parent_post_block_buffers.append(
                            {
                                "pts_ns": int(buffer.pts),
                                "observed_monotonic_ns": time.monotonic_ns(),
                                "delta_unit": bool(
                                    buffer.has_flags(self.gst.BufferFlags.DELTA_UNIT)
                                ),
                            }
                        )
                return self.gst.PadProbeReturn.OK

            parent_tail_probe = parent_counter_pad.add_probe(
                self.gst.PadProbeType.BUFFER,
                observe_parent_post_block,
            )
            if not parent_tail_probe:
                raise HarnessError("parent post-block counter probe was refused")
            video_sink_pad = final.video_valve.get_static_pad("sink")
            if video_sink_pad is None:
                raise HarnessError("final video-only valve sink is absent")

            def observe_terminal_drop(_pad: Any, info: Any) -> Any:
                buffer = info.get_buffer()
                if buffer is not None:
                    observed_ns = time.monotonic_ns()
                    if terminal_drop_timing["first_observed_monotonic_ns"] is None:
                        terminal_drop_timing["first_observed_monotonic_ns"] = observed_ns
                    terminal_drop_timing["last_observed_monotonic_ns"] = observed_ns
                    terminal_drop_counter.observe(buffer)
                    if len(closed_valve_buffers) >= MAX_FINAL_SHUTDOWN_MEDIA_PERIODS + 2:
                        if not terminal_counter_probe_errors:
                            terminal_counter_probe_errors.append(
                                "closed-valve buffer evidence exceeded its bound"
                            )
                    else:
                        closed_valve_buffers.append(
                            {
                                "pts_ns": int(buffer.pts),
                                "observed_monotonic_ns": observed_ns,
                                "delta_unit": bool(
                                    buffer.has_flags(self.gst.BufferFlags.DELTA_UNIT)
                                ),
                            }
                        )
                return self.gst.PadProbeReturn.OK

            terminal_drop_probe = video_sink_pad.add_probe(
                self.gst.PadProbeType.BUFFER,
                observe_terminal_drop,
            )
            if not terminal_drop_probe:
                raise HarnessError("final closed-valve counter probe was refused")
            active_locations = [
                location
                for location in final.opened_locations
                if location not in final.closed_locations
            ]
            if len(active_locations) != 1:
                raise HarnessError("final generation has no unique active fragment")
            active_location = active_locations[0]
            if (
                self.terminal_shutdown_phase != "INACTIVE"
                or self.terminal_shutdown_context is not None
                or self.terminal_parent_eos_observations
                or len(self.transitions) != 1
                or self.transitions[0].get("new_generation") != final.number
                or self.transitions[0].get("within_one_frame") is not True
                or self.transitions[0].get("new_first_video_is_idr") is not True
                or not self.video_path_diagnostics
                or self.video_path_diagnostics[-1].get("stage")
                != "post_loss_fragment_wait_completed"
                or len(self.successor_state_convergence) != 1
                or self.successor_state_convergence[0].get("converged") is not True
            ):
                raise HarnessError("terminal shutdown lacks proven post-loss successor state")
            self.terminal_shutdown_context = {
                "final_generation": final.number,
                "active_location": active_location,
                "prepared_monotonic_ns": time.monotonic_ns(),
                "final_video_eos_dispatch": None,
                "fragment_closed_phase_monotonic_ns": None,
            }
            self.terminal_shutdown_phase = "FINAL_BRANCH_PREPARED"
            self._record_event(
                "terminal_shutdown_phase_entered",
                phase=self.terminal_shutdown_phase,
                final_generation=final.number,
                active_location=active_location,
            )
            self._set_generation_open(final, False)
            final_queue_sink_pad = final.video_queue.get_static_pad("sink")
            if final_queue_sink_pad is None:
                raise HarnessError("final video-only queue sink is absent")
            eos_dispatches = self._await_eos_dispatches(
                (self._start_downstream_eos(final_queue_sink_pad, "final-video-only"),)
            )
            self.terminal_shutdown_context["final_video_eos_dispatch"] = dict(eos_dispatches[0])
            self.terminal_shutdown_phase = "FINAL_BRANCH_EOS_DISPATCHED"
            self._wait_for(
                lambda: (
                    active_location in final.closed_locations
                    and set(final.opened_locations) == set(final.closed_locations)
                    and final.video_eos_seen
                ),
                timeout_seconds=20.0,
                reason="final video-only active fragment closure",
            )
            fragment_closed_phase_ns = time.monotonic_ns()
            self.terminal_shutdown_context["fragment_closed_phase_monotonic_ns"] = (
                fragment_closed_phase_ns
            )
            self.terminal_shutdown_phase = "FINAL_FRAGMENT_CLOSED"
            self._record_event(
                "terminal_shutdown_fragment_closed",
                phase=self.terminal_shutdown_phase,
                final_generation=final.number,
                active_location=active_location,
            )
            self._drain_safety_bus_quiet()
            final_media_path = self.output_directory / active_location
            final_media_info = os.lstat(final_media_path)
            if (
                final_media_path.parent != self.output_directory
                or final_media_path.is_symlink()
                or not stat.S_ISREG(final_media_info.st_mode)
                or not 0 < final_media_info.st_size <= MAX_FINAL_FROZEN_MEDIA_BYTES
            ):
                raise HarnessError("final closed media is not one bounded regular file")
            frozen_media_before_null = {
                "name": active_location,
                "device": final_media_info.st_dev,
                "inode": final_media_info.st_ino,
                "size_bytes": final_media_info.st_size,
                "sha256": _shared._sha256_file(
                    final_media_path,
                    maximum=MAX_FINAL_FROZEN_MEDIA_BYTES,
                ),
            }
            self.terminal_shutdown_phase = "FINAL_BUS_DRAINED"
        finally:
            common_tee_total_at_release = common_tee_state["total_count"]
            self._release_blocked_video_idr(handoff)
        # The final valve stays closed and externally linked. Releasing the
        # held IDR therefore cannot create an unlinked tee; it is dropped at
        # the closed valve before the independently bounded NULL transition.
        self.terminal_shutdown_phase = "PARENT_NULL_REQUESTED"
        null_return, waited = self._transition_parent_null_bounded()
        self._join_eos_workers_after_null()
        self._release_audio_idle_blocks_after_parent_null()
        final_parent_snapshot = self.video_source_counter.snapshot()
        parent_count = cast(int, final_parent_snapshot["count"])
        routed_count = sum(
            generation.video_counter.count for generation in self.generations.values()
        )
        self.final_unrouted_video_frames = parent_count - routed_count
        null_transition = self._null_transition
        if (
            null_transition is None
            or null_transition.ended_monotonic_ns is None
            or not isinstance(handoff.get("pad_unblocked_monotonic_ns"), int)
        ):
            raise HarnessError("final shutdown tail lacks complete bounded timing evidence")
        blocked_ns = cast(int, handoff["blocked_monotonic_ns"])
        release_requested_ns = cast(int, handoff["release_requested_monotonic_ns"])
        pad_unblocked_ns = cast(int, handoff["pad_unblocked_monotonic_ns"])
        null_started_ns = null_transition.started_monotonic_ns
        null_ended_ns = null_transition.ended_monotonic_ns
        if frozen_media_before_null is None:
            raise HarnessError("final closed-media freeze baseline is absent")
        final_media_path = self.output_directory / cast(
            str, frozen_media_before_null["name"]
        )
        try:
            final_media_info = os.lstat(final_media_path)
            frozen_media_after_null = {
                "name": final_media_path.name,
                "device": final_media_info.st_dev,
                "inode": final_media_info.st_ino,
                "size_bytes": final_media_info.st_size,
                "sha256": _shared._sha256_file(
                    final_media_path,
                    maximum=MAX_FINAL_FROZEN_MEDIA_BYTES,
                ),
            }
        except BaseException as error:
            frozen_media_after_null = {
                "name": final_media_path.name,
                "snapshot_error": _shared._bounded_detail(error),
            }
        drop_snapshot = terminal_drop_counter.snapshot()
        first_drop_ns = terminal_drop_timing["first_observed_monotonic_ns"]
        last_drop_ns = terminal_drop_timing["last_observed_monotonic_ns"]
        media_window_ns = (
            last_drop_ns - release_requested_ns if isinstance(last_drop_ns, int) else -1
        )
        media_window_frame_budget = (
            1 + (media_window_ns + FRAME_PERIOD_NS - 1) // FRAME_PERIOD_NS
            if media_window_ns >= 0
            else -1
        )
        additional_parent_frames = parent_count - parent_count_at_block
        initial_unrouted_frames = parent_count_at_block - routed_count_at_block
        delivered_after_held_count = max(cast(int, drop_snapshot["count"]) - 1, 0)
        parent_only_buffers = parent_post_block_buffers[delivered_after_held_count:]
        common_tee_final_total = common_tee_state["total_count"]
        common_tee_terminal_count = (
            common_tee_final_total - common_tee_held_sequence + 1
            if common_tee_held_sequence is not None
            else -1
        )
        common_tee_buffers = (
            [
                dict(record)
                for record in common_tee_ring
                if cast(int, record["sequence"]) >= common_tee_held_sequence
            ]
            if common_tee_held_sequence is not None
            else []
        )
        if common_tee_terminal_count > MAX_FINAL_SHUTDOWN_MEDIA_PERIODS + 2:
            terminal_counter_probe_errors.append(
                "common-tee terminal suffix evidence exceeded its bound"
            )
        media_pts_span_ns = (
            final_parent_snapshot["last_pts_ns"] - cast(int, handoff["pts_ns"])
            if isinstance(final_parent_snapshot["last_pts_ns"], int)
            else -1
        )
        media_pts_frame_budget = (
            1 + (media_pts_span_ns + FRAME_PERIOD_NS - 1) // FRAME_PERIOD_NS
            if media_pts_span_ns >= 0
            else -1
        )
        self.final_shutdown_tail_evidence = {
            "frame_period_ns": FRAME_PERIOD_NS,
            "maximum_media_periods": MAX_FINAL_SHUTDOWN_MEDIA_PERIODS,
            "maximum_media_window_ns": (
                MAX_FINAL_SHUTDOWN_MEDIA_PERIODS * FRAME_PERIOD_NS
            ),
            "blocked_monotonic_ns": blocked_ns,
            "held_idr_pts_ns": handoff["pts_ns"],
            "release_requested_monotonic_ns": release_requested_ns,
            "pad_unblocked_monotonic_ns": pad_unblocked_ns,
            "null_started_monotonic_ns": null_started_ns,
            "null_ended_monotonic_ns": null_ended_ns,
            "null_request_after_unblock_ns": null_started_ns - pad_unblocked_ns,
            "first_closed_valve_buffer_monotonic_ns": first_drop_ns,
            "last_closed_valve_buffer_monotonic_ns": last_drop_ns,
            "media_window_ns": media_window_ns,
            "media_window_frame_budget": media_window_frame_budget,
            "media_pts_span_ns": media_pts_span_ns,
            "media_pts_frame_budget": media_pts_frame_budget,
            "held_idr_closure_wait_ns": release_requested_ns - blocked_ns,
            "shutdown_control_window_ns": null_started_ns - release_requested_ns,
            "closed_valve_counter": drop_snapshot,
            "closed_valve_buffers": [dict(record) for record in closed_valve_buffers],
            "common_tee_buffers": [dict(record) for record in common_tee_buffers],
            "common_tee_baseline": {
                "ring_capacity": MAX_FINAL_COMMON_TEE_RING_BUFFERS,
                "retained_count_at_block": len(common_tee_ring_at_block),
                "evicted_count_at_block": common_tee_evicted_at_block,
                "total_count_at_block": common_tee_total_at_block,
                "held_ring_index": (
                    len(common_tee_ring_at_block) - 1
                    if common_tee_ring_at_block
                    else None
                ),
                "held_sequence": common_tee_held_sequence,
                "held_is_exact_last_record": common_tee_baseline_valid,
                "total_count_at_release": common_tee_total_at_release,
                "final_total_count": common_tee_final_total,
                "terminal_suffix_count": common_tee_terminal_count,
                "terminal_suffix_retained": (
                    common_tee_terminal_count == len(common_tee_buffers)
                ),
            },
            "parent_post_block_buffers": [
                dict(record) for record in parent_post_block_buffers
            ],
            "post_null_parent_only_buffers": [
                dict(record) for record in parent_only_buffers
            ],
            "terminal_counter_probe_errors": list(terminal_counter_probe_errors),
            "frozen_media_before_null": dict(frozen_media_before_null),
            "frozen_media_after_null": frozen_media_after_null,
            "fragment_closed_phase_monotonic_ns": fragment_closed_phase_ns,
            "final_valve_drop_after_null": bool(final.video_valve.get_property("drop")),
            "final_generation_linked_after_null": final.external_linked,
            "all_final_fragments_closed_after_null": (
                set(final.opened_locations) == set(final.closed_locations)
            ),
            "parent_count_at_block": parent_count_at_block,
            "parent_last_pts_at_block": parent_snapshot_at_block["last_pts_ns"],
            "routed_count_at_block": routed_count_at_block,
            "initial_unrouted_frames": initial_unrouted_frames,
            "final_parent_count": parent_count,
            "final_parent_last_pts_ns": final_parent_snapshot["last_pts_ns"],
            "final_routed_count": routed_count,
            "routed_count_stable": routed_count == routed_count_at_block,
            "additional_parent_frames": additional_parent_frames,
            "allowed_final_unrouted_video_frames": (
                media_pts_frame_budget
            ),
            "measured_final_unrouted_video_frames": self.final_unrouted_video_frames,
            "tail_identity_verified": (
                self.final_unrouted_video_frames
                == initial_unrouted_frames + additional_parent_frames
            ),
            "within_time_frame_contract": (
                0
                <= media_window_ns
                <= MAX_FINAL_SHUTDOWN_MEDIA_PERIODS * FRAME_PERIOD_NS
                and 0 <= null_started_ns - pad_unblocked_ns <= FRAME_PERIOD_NS
                and 0
                <= null_started_ns - release_requested_ns
                <= MAX_FINAL_SHUTDOWN_MEDIA_PERIODS * FRAME_PERIOD_NS
                and 0
                <= release_requested_ns - blocked_ns
                <= int(VIDEO_IDR_RELEASE_TIMEOUT_SECONDS * 1_000_000_000)
                and 0
                <= media_pts_span_ns
                <= MAX_FINAL_SHUTDOWN_MEDIA_PERIODS * FRAME_PERIOD_NS
                and self.final_unrouted_video_frames <= media_pts_frame_budget
            ),
        }
        if not _final_shutdown_tail_contract(self.final_shutdown_tail_evidence):
            raise HarnessError(
                "measured final shutdown video tail left its 30-fps/time contract: "
                f"{self.final_unrouted_video_frames} frames in {media_window_ns} ns"
            )
        self._drain_safety_bus_quiet()
        self._close_audio_corroboration_window()
        for generation in self.generations.values():
            self.release_after_parent_null(generation)
        self._assert_control_workers_stopped()
        self._record_event(
            "pipeline_stopped_after_dead_audio",
            final_branch_eos_return=eos_dispatches[0]["accepted"],
            eos_dispatches=eos_dispatches,
            parent_eos_required=False,
            null_return=int(null_return),
            null_wait_return=int(waited),
            measured_final_unrouted_video_frames=self.final_unrouted_video_frames,
            final_shutdown_tail=dict(self.final_shutdown_tail_evidence),
            terminal_parent_eos_observation_count=len(self.terminal_parent_eos_observations),
        )
        self.terminal_shutdown_phase = "COMPLETE"

    def _bounded_failure_cleanup(self, original: BaseException) -> None:
        cleanup_errors: list[str] = []
        try:
            self._release_audio_idle_blocks_before_failure_null()
        except BaseException as cleanup_error:
            cleanup_errors.append(
                f"pre-null audio IDLE release: {_shared._bounded_detail(cleanup_error)}"
            )
        try:
            self._transition_parent_null_bounded()
            self._join_eos_workers_after_null()
            self._release_audio_idle_blocks_after_parent_null()
        except BaseException as cleanup_error:
            cleanup_errors.append(_shared._bounded_detail(cleanup_error))
        if cleanup_errors:
            raise HarnessError(
                "run failed and bounded cleanup did not reach parent NULL: "
                f"original={_shared._bounded_detail(original)}; "
                f"cleanup={'; '.join(cleanup_errors)}"
            ) from original
        for generation in tuple(self.generations.values()):
            if generation.bin.get_parent() is self.pipeline:
                self.release_after_parent_null(generation)

    def run(self) -> dict[str, object]:
        try:
            first = self.create_generation(1, True)
            second = self.create_generation(2, False)
            self.start(first)
            self._wait_for(
                lambda: len(first.closed_locations) >= MIN_AV_FRAGMENTS,
                timeout_seconds=20.0,
                reason="pre-loss A/V diagnostic fragments",
            )
            armed_ns = time.monotonic_ns()
            self.loss_wait_armed = True
            self.loss_wait_armed_ns = armed_ns
            self.discovery_window_open = True
            self._observe_loss_identity(require_initial_match=True)
            self._record_event(
                "owner_unplug_window_open",
                timeout_seconds=self.loss_timeout_seconds,
                registered_audio_source_path=self.registered_audio_source_path,
            )
            print(
                "OWNER_ACTION_REQUIRED: unplug the exact USB microphone now",
                flush=True,
            )
            self._wait_for_confirmed_physical_loss(first)
            if self.expected_audio_error is not None:
                self.expected_audio_error["wait_from_armed_ns"] = (
                    cast(int, self.expected_audio_error["observed_monotonic_ns"]) - armed_ns
                )
            self.switch_after_physical_loss(first, second)
            self._wait_for_post_loss_video_only_fragments(first, second)
            final_count = second.video_counter.count
            self._wait_for(
                lambda: second.video_counter.count >= final_count + 30,
                timeout_seconds=3.0,
                reason="bounded final video-only fragment media",
            )
            self.stop_video_only(second)
        except BaseException as original:
            self._bounded_failure_cleanup(original)
            raise
        return {
            "events": self.events,
            "transitions": self.transitions,
            "warnings": self.warnings,
            "errors": self.errors,
            "expected_audio_error": self.expected_audio_error,
            "audio_loss_error_burst": self._audio_loss_evidence(),
            "latency_recalculation_refusals": self._latency_refusal_evidence(),
            "physical_loss_discovery": self._loss_discovery_evidence(),
            "eos_dispatches": [dict(dispatch) for dispatch in self.eos_dispatch_evidence],
            "audio_eos_fallbacks": [
                dict(fallback) for fallback in self.audio_eos_fallback_evidence
            ],
            "audio_eos_branch_decisions": [
                dict(decision) for decision in self.audio_eos_branch_decision_evidence
            ],
            "natural_eos_final_absence_checks": [
                dict(check) for check in self.natural_eos_final_absence_checks
            ],
            "video_path_diagnostics": [
                dict(diagnostic) for diagnostic in self.video_path_diagnostics
            ],
            "successor_state_convergence": [
                dict(convergence) for convergence in self.successor_state_convergence
            ],
            "terminal_shutdown": {
                "phase": self.terminal_shutdown_phase,
                "context": (
                    dict(self.terminal_shutdown_context)
                    if self.terminal_shutdown_context is not None
                    else None
                ),
                "parent_eos_observations": [
                    dict(observation) for observation in self.terminal_parent_eos_observations
                ],
                "video_tail": (
                    dict(self.final_shutdown_tail_evidence)
                    if self.final_shutdown_tail_evidence is not None
                    else None
                ),
            },
            "output_audio_eos_observations": self._output_audio_eos_evidence(),
            "parent": {
                "camera_object_preserved": self.pipeline.get_by_name("camera") is self.camera,
                "encoder_object_preserved": self.pipeline.get_by_name("encoder") is self.encoder,
                "parser_object_preserved": self.pipeline.get_by_name("parser") is self.parser,
                "audio_source_object_preserved": self.pipeline.get_by_name("audio_source")
                is self.audio_source,
                "audio_source_path": self.registered_audio_source_path,
                "base_time_ns": self.base_time_ns,
                "camera_raw": self.camera_source_counter.snapshot(),
                "video_source": self.video_source_counter.snapshot(),
                "audio_source": self.audio_source_counter.snapshot(),
                "measured_final_unrouted_video_frames": self.final_unrouted_video_frames,
            },
            "generations": {
                str(number): {
                    "audio": generation.audio,
                    "retired": generation.retired,
                    "opened_locations": generation.opened_locations,
                    "closed_locations": generation.closed_locations,
                    "video_eos_seen": generation.video_eos_seen,
                    "audio_eos_seen": generation.audio_eos_seen,
                    "video_events": sorted(generation.video_events),
                    "audio_events": sorted(generation.audio_events),
                    "video_first_buffer_had_sticky_contract": (
                        generation.video_first_buffer_had_sticky_contract
                    ),
                    "audio_first_buffer_had_sticky_contract": (
                        generation.audio_first_buffer_had_sticky_contract
                    ),
                    "ingress_event_error": generation.ingress_event_error,
                    "external_linked": generation.external_linked,
                    "video": generation.video_counter.snapshot(),
                    "audio_counter": generation.audio_counter.snapshot(),
                }
                for number, generation in sorted(self.generations.items())
            },
        }


def _stream_contract(path: Path, audio_expected: bool) -> dict[str, object]:
    document = _shared._probe_media(path)
    allowed_top_level = {"streams", "format", "programs", "stream_groups"}
    if not {"streams", "format"} <= set(document) or not set(document) <= allowed_top_level:
        raise HarnessError("ffprobe top-level schema differs")
    for optional in ("programs", "stream_groups"):
        if optional in document and document[optional] != []:
            raise HarnessError("ffprobe optional collection is not empty")
    streams = document.get("streams")
    format_value = document.get("format")
    if not isinstance(format_value, Mapping) or set(format_value) != {
        "duration",
        "size",
    }:
        raise HarnessError("ffprobe format schema differs")
    try:
        format_duration = float(cast(str, format_value["duration"]))
        format_size = int(cast(str, format_value["size"]))
    except (TypeError, ValueError) as error:
        raise HarnessError("ffprobe format values are invalid") from error
    if not 0.10 <= format_duration <= 5.0 or not 1 <= format_size <= 128 * 1024 * 1024:
        raise HarnessError("diagnostic media size/duration left its bound")
    if not isinstance(streams, Sequence) or isinstance(streams, str | bytes):
        raise HarnessError("ffprobe stream schema differs")
    typed = [stream for stream in streams if isinstance(stream, Mapping)]
    video = [stream for stream in typed if stream.get("codec_type") == "video"]
    audio = [stream for stream in typed if stream.get("codec_type") == "audio"]
    if (
        len(typed) != len(streams)
        or len(typed) != 1 + int(audio_expected)
        or len(video) != 1
        or len(audio) != int(audio_expected)
    ):
        raise HarnessError("generation stream set differs")
    video_stream = video[0]
    if set(video_stream) != {
        "index",
        "codec_type",
        "codec_name",
        "profile",
        "width",
        "height",
        "r_frame_rate",
        "start_time",
        "duration",
        "bit_rate",
    }:
        raise HarnessError("video ffprobe schema differs")
    try:
        video_bitrate = int(cast(str, video_stream["bit_rate"]))
        video_duration = float(cast(str, video_stream["duration"]))
    except (KeyError, TypeError, ValueError) as error:
        raise HarnessError("video stream values are invalid") from error
    if (
        video_stream.get("codec_name") != "h264"
        or video_stream.get("profile") != "High"
        or video_stream.get("width") != 1920
        or video_stream.get("height") != 1080
        or video_stream.get("r_frame_rate") != "30/1"
        or not 6_000_000 <= video_bitrate <= 10_000_000
        or not 0.10 <= video_duration <= 5.0
    ):
        raise HarnessError("video stream contract differs")
    skew: float | None = None
    if audio_expected:
        audio_stream = audio[0]
        if set(audio_stream) != {
            "index",
            "codec_type",
            "codec_name",
            "profile",
            "sample_rate",
            "channels",
            "r_frame_rate",
            "start_time",
            "duration",
            "bit_rate",
        }:
            raise HarnessError("audio ffprobe schema differs")
        try:
            audio_bitrate = int(cast(str, audio_stream["bit_rate"]))
            audio_duration = float(cast(str, audio_stream["duration"]))
            audio_start = float(cast(str, audio_stream["start_time"]))
            video_start = float(cast(str, video_stream["start_time"]))
        except (KeyError, TypeError, ValueError) as error:
            raise HarnessError("audio stream values are invalid") from error
        if (
            audio_stream.get("codec_name") != "aac"
            or audio_stream.get("profile") != "LC"
            or str(audio_stream.get("sample_rate")) != "48000"
            or audio_stream.get("channels") != 1
            or audio_stream.get("r_frame_rate") != "0/0"
            or not 120_000 <= audio_bitrate <= 136_000
            or not 0.10 <= audio_duration <= 5.0
        ):
            raise HarnessError("audio stream contract differs")
        skew = max(
            abs(audio_start - video_start),
            abs((audio_start + audio_duration) - (video_start + video_duration)),
        )
        if skew >= 0.100:
            raise HarnessError("pre-loss A/V stream-edge skew exceeded 100 ms")
    return {"audio": audio_expected, "stream_edge_skew_seconds": skew}


def validate_media(directory: Path) -> dict[str, object]:
    files = sorted(directory.iterdir())
    if not MIN_AV_FRAGMENTS + MIN_VIDEO_ONLY_FRAGMENTS <= len(files) <= MAX_MEDIA_COUNT:
        raise HarnessError("diagnostic media count left its 5-40 bound")
    counts = {1: 0, 2: 0}
    members: list[dict[str, object]] = []
    for path in files:
        if path.parent != directory or path.is_symlink() or not path.is_file():
            raise HarnessError("diagnostic media member is not one direct regular file")
        match = MEDIA_NAME_RE.fullmatch(path.name)
        if match is None:
            raise HarnessError("foreign member exists in fresh diagnostic directory")
        generation = int(match.group(1))
        audio_expected = generation == 1
        counts[generation] += 1
        stream = _stream_contract(path, audio_expected)
        idr = bool(_shared._first_packet_is_idr(path))
        if not idr:
            raise HarnessError(f"diagnostic MP4 does not start with an IDR: {path.name}")
        _shared._decode_media(path, audio_expected)
        members.append(
            {
                "file": path.name,
                "sha256": _shared._sha256_file(
                    path,
                    maximum=128 * 1024 * 1024,
                ),
                "generation": generation,
                "audio": audio_expected,
                "first_packet_idr": True,
                "hardware_decode": True,
                **stream,
            }
        )
    if counts[1] < MIN_AV_FRAGMENTS or counts[2] < MIN_VIDEO_ONLY_FRAGMENTS:
        raise HarnessError("required per-generation fragment counts were not met")
    return {"count": len(files), "generation_counts": counts, "members": members}


def _validate_runtime_media_binding(
    runtime: Mapping[str, object],
    media: Mapping[str, object],
) -> None:
    generations = cast(Mapping[str, Mapping[str, object]], runtime["generations"])
    if set(generations) != {"1", "2"}:
        raise HarnessError("runtime generation set differs")
    closed_media: set[str] = set()
    for number in (1, 2):
        generation = generations[str(number)]
        opened = generation.get("opened_locations")
        closed = generation.get("closed_locations")
        if (
            not isinstance(opened, list)
            or not isinstance(closed, list)
            or not all(isinstance(value, str) for value in (*opened, *closed))
            or len(opened) != len(set(opened))
            or len(closed) != len(set(closed))
            or set(opened) != set(closed)
        ):
            raise HarnessError("runtime fragment lifecycle set differs")
        for name in closed:
            match = MEDIA_NAME_RE.fullmatch(name)
            if match is None or int(match.group(1)) != number or name in closed_media:
                raise HarnessError("runtime closed fragment identity differs")
            closed_media.add(name)
    members = media.get("members")
    if not isinstance(members, list):
        raise HarnessError("validated media member set is absent")
    validated_media: set[str] = set()
    for member in members:
        if not isinstance(member, Mapping):
            raise HarnessError("validated media member schema differs")
        name = member.get("file")
        if not isinstance(name, str) or name in validated_media:
            raise HarnessError("validated media member identity differs")
        validated_media.add(name)
    if closed_media != validated_media:
        raise HarnessError("runtime closed fragments differ from validated media")


def _physical_failure(
    experiment: PhysicalLossExperiment,
    error: BaseException,
) -> PhysicalExperimentFailure:
    if isinstance(error, PhysicalExperimentFailure):
        return error
    return PhysicalExperimentFailure(
        _shared._bounded_detail(error),
        experiment._failure_diagnostic(),
    )


def _loss_burst_contract(burst: Mapping[str, object]) -> bool:
    messages = burst.get("messages")
    first_ns = burst.get("first_observed_monotonic_ns")
    last_ns = burst.get("last_observed_monotonic_ns")
    if (
        burst.get("closed") is not True
        or not isinstance(messages, list)
        or not 0 <= len(messages) <= MAX_AUDIO_LOSS_ERRORS
        or burst.get("accepted_count") != len(messages)
    ):
        return False
    if not messages:
        return burst.get("corroborated") is False and first_ns is None and last_ns is None
    if (
        burst.get("corroborated") is not True
        or not isinstance(first_ns, int)
        or isinstance(first_ns, bool)
        or not isinstance(last_ns, int)
        or isinstance(last_ns, bool)
        or last_ns < first_ns
    ):
        return False
    previous_ns = first_ns
    for sequence, message in enumerate(messages, start=1):
        if not isinstance(message, Mapping):
            return False
        observed_ns = message.get("observed_monotonic_ns")
        if (
            message.get("sequence") != sequence
            or message.get("exact_registered_audio_source") is not True
            or message.get("accepted_loss_burst") is not True
            or not isinstance(message.get("error_code"), (int, str))
            or isinstance(message.get("error_code"), bool)
            or not isinstance(message.get("error_domain"), (int, str))
            or isinstance(message.get("error_domain"), bool)
            or not isinstance(message.get("debug"), str)
            or not isinstance(observed_ns, int)
            or isinstance(observed_ns, bool)
            or observed_ns < previous_ns
            or message.get("delta_from_first_ns") != observed_ns - first_ns
        ):
            return False
        previous_ns = observed_ns
    return previous_ns == last_ns


def _physical_loss_discovery_contract(evidence: Mapping[str, object]) -> bool:
    observations = evidence.get("observations")
    initial = evidence.get("initial_device")
    confirmed = evidence.get("confirmed_loss")
    if (
        evidence.get("window_open") is not False
        or evidence.get("poll_interval_seconds") != LOSS_DISCOVERY_POLL_SECONDS
        or evidence.get("stable_not_found_separation_ns") != STABLE_NOT_FOUND_SEPARATION_NS
        or not isinstance(observations, list)
        or not 3 <= len(observations) <= MAX_LOSS_DISCOVERY_OBSERVATIONS
        or evidence.get("observation_count") != len(observations)
        or not isinstance(initial, Mapping)
        or not isinstance(confirmed, Mapping)
        or confirmed.get("trigger") != "stable_identity_not_found"
        or not isinstance(observations[0], Mapping)
        or cast(Mapping[str, object], observations[0]).get("status")
        != AudioDiscoveryStatus.MATCHED.value
    ):
        return False
    initial_endpoint = initial.get("endpoint")
    initial_identity = initial.get("identity")
    previous_ns = -1
    for sequence, observation in enumerate(observations, start=1):
        if not isinstance(observation, Mapping):
            return False
        observed_ns = observation.get("observed_monotonic_ns")
        delta_ns = observation.get("delta_from_loss_window_armed_ns")
        status = observation.get("status")
        if (
            observation.get("sequence") != sequence
            or status
            not in (
                AudioDiscoveryStatus.MATCHED.value,
                AudioDiscoveryStatus.NOT_FOUND.value,
            )
            or not isinstance(observed_ns, int)
            or isinstance(observed_ns, bool)
            or observed_ns < previous_ns
            or not isinstance(delta_ns, int)
            or isinstance(delta_ns, bool)
            or delta_ns < 0
        ):
            return False
        if status == AudioDiscoveryStatus.MATCHED.value:
            if (
                observation.get("device_exposed") is not True
                or observation.get("endpoint") != initial_endpoint
                or observation.get("identity") != initial_identity
            ):
                return False
        elif observation.get("device_exposed") is not False:
            return False
        previous_ns = observed_ns
    first_sequence = confirmed.get("first_not_found_sequence")
    second_sequence = confirmed.get("second_not_found_sequence")
    if (
        not isinstance(first_sequence, int)
        or isinstance(first_sequence, bool)
        or not isinstance(second_sequence, int)
        or isinstance(second_sequence, bool)
        or second_sequence != first_sequence + 1
        or not 1 <= first_sequence < second_sequence <= len(observations)
    ):
        return False
    first = cast(Mapping[str, object], observations[first_sequence - 1])
    second = cast(Mapping[str, object], observations[second_sequence - 1])
    separation = second.get("observed_monotonic_ns")
    first_time = first.get("observed_monotonic_ns")
    return (
        first.get("status") == AudioDiscoveryStatus.NOT_FOUND.value
        and second.get("status") == AudioDiscoveryStatus.NOT_FOUND.value
        and isinstance(separation, int)
        and not isinstance(separation, bool)
        and isinstance(first_time, int)
        and not isinstance(first_time, bool)
        and separation - first_time >= STABLE_NOT_FOUND_SEPARATION_NS
        and second_sequence == len(observations)
        and confirmed.get("first_not_found_monotonic_ns") == first_time
        and confirmed.get("second_not_found_monotonic_ns") == separation
        and confirmed.get("separation_ns") == separation - first_time
    )


def _loss_latency_refusal_contract(evidence: Mapping[str, object]) -> bool:
    messages = evidence.get("messages")
    first_ns = evidence.get("first_observed_monotonic_ns")
    last_ns = evidence.get("last_observed_monotonic_ns")
    if (
        not isinstance(messages, list)
        or not 0 <= len(messages) <= MAX_LOSS_LATENCY_REFUSALS
        or evidence.get("count") != len(messages)
    ):
        return False
    if not messages:
        return first_ns is None and last_ns is None
    if (
        not isinstance(first_ns, int)
        or isinstance(first_ns, bool)
        or not isinstance(last_ns, int)
        or isinstance(last_ns, bool)
    ):
        return False
    previous_ns = first_ns
    for sequence, message in enumerate(messages, start=1):
        if not isinstance(message, Mapping):
            return False
        observed_ns = message.get("observed_monotonic_ns")
        delta_ns = message.get("delta_from_loss_window_armed_ns")
        if (
            message.get("sequence") != sequence
            or message.get("accepted_loss_window") is not True
            or not isinstance(message.get("source"), str)
            or not cast(str, message.get("source"))
            or not isinstance(message.get("source_path"), str)
            or not cast(str, message.get("source_path"))
            or not isinstance(observed_ns, int)
            or isinstance(observed_ns, bool)
            or observed_ns < previous_ns
            or not isinstance(delta_ns, int)
            or isinstance(delta_ns, bool)
            or delta_ns < 0
        ):
            return False
        previous_ns = observed_ns
    return previous_ns == last_ns


def _video_path_diagnostic_contract(value: object) -> bool:
    if not isinstance(value, list) or len(value) != 6:
        return False
    expected_stages = (
        "handoff_before_idr_release",
        "handoff_idr_released",
        "successor_first_video_buffer",
        "successor_continuous_video_proven",
        "post_loss_fragment_wait_started",
        "post_loss_fragment_wait_completed",
    )
    previous_camera = -1
    previous_parent = -1
    previous_successor = -1
    successor_counts: list[int] = []
    typed: list[Mapping[str, object]] = []
    for sequence, (item, expected_stage) in enumerate(
        zip(value, expected_stages, strict=True),
        start=1,
    ):
        if not isinstance(item, Mapping):
            return False
        camera = item.get("camera_raw")
        parent = item.get("parent_encoded")
        successor = item.get("successor_generation")
        tee_sink = item.get("video_tee_sink")
        successor_pad = item.get("successor_video_tee_pad")
        pipeline_state = item.get("pipeline_state")
        if (
            item.get("sequence") != sequence
            or item.get("stage") != expected_stage
            or not isinstance(item.get("observed_monotonic_ns"), int)
            or isinstance(item.get("observed_monotonic_ns"), bool)
            or not isinstance(camera, Mapping)
            or not isinstance(parent, Mapping)
            or not isinstance(successor, Mapping)
            or not isinstance(tee_sink, Mapping)
            or not isinstance(successor_pad, Mapping)
            or not isinstance(pipeline_state, Mapping)
            or "snapshot_error" in pipeline_state
            or successor.get("external_linked") is not True
            or successor.get("video_valve_drop") is not False
            or successor.get("tee_peer_is_exact_ghost") is not True
            or successor_pad.get("linked") is not True
            or not isinstance(item.get("files"), list)
        ):
            return False
        successor_video = successor.get("video")
        if not isinstance(successor_video, Mapping):
            return False
        camera_count = camera.get("count")
        parent_count = parent.get("count")
        successor_count = successor_video.get("count")
        if (
            not isinstance(camera_count, int)
            or isinstance(camera_count, bool)
            or not isinstance(parent_count, int)
            or isinstance(parent_count, bool)
            or not isinstance(successor_count, int)
            or isinstance(successor_count, bool)
            or camera_count < previous_camera
            or parent_count < previous_parent
            or successor_count < previous_successor
        ):
            return False
        previous_camera = camera_count
        previous_parent = parent_count
        previous_successor = successor_count
        successor_counts.append(successor_count)
        typed.append(item)
    before_release = typed[0]
    after_release = typed[1]
    before_idr = before_release.get("idr_release")
    after_idr = after_release.get("idr_release")
    first_successor = cast(Mapping[str, object], typed[2]["successor_generation"])
    completed_successor = cast(Mapping[str, object], typed[5]["successor_generation"])
    return (
        isinstance(before_idr, Mapping)
        and before_idr.get("release_requested") is False
        and before_idr.get("callback_returning") is False
        and cast(Mapping[str, object], before_release["video_tee_sink"]).get("blocked") is True
        and cast(Mapping[str, object], before_release["video_tee_sink"]).get("blocking") is True
        and isinstance(after_idr, Mapping)
        and after_idr.get("release_requested") is True
        and after_idr.get("callback_returning") is True
        and isinstance(after_idr.get("release_requested_monotonic_ns"), int)
        and not isinstance(after_idr.get("release_requested_monotonic_ns"), bool)
        and isinstance(after_idr.get("callback_returning_monotonic_ns"), int)
        and not isinstance(after_idr.get("callback_returning_monotonic_ns"), bool)
        and cast(Mapping[str, object], after_release["video_tee_sink"]).get("blocked") is False
        and cast(Mapping[str, object], after_release["video_tee_sink"]).get("blocking") is False
        and successor_counts[2] > 0
        and successor_counts[3] >= successor_counts[2] + 30
        and typed[2].get("retained_audio_idle_probe_count") == 0
        and isinstance(first_successor.get("opened_locations"), list)
        and len(cast(list[object], first_successor["opened_locations"])) >= 1
        and isinstance(completed_successor.get("closed_locations"), list)
        and len(cast(list[object], completed_successor["closed_locations"]))
        >= MIN_VIDEO_ONLY_FRAGMENTS
    )


def _successor_state_convergence_contract(value: object) -> bool:
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], Mapping):
        return False
    record = cast(Mapping[str, object], value[0])
    initial_sync = record.get("initial_sync")
    final = record.get("final")
    if (
        record.get("sequence") != 1
        or record.get("converged") is not True
        or not isinstance(record.get("observed_after_first_buffer_monotonic_ns"), int)
        or isinstance(record.get("observed_after_first_buffer_monotonic_ns"), bool)
        or not isinstance(record.get("converged_monotonic_ns"), int)
        or isinstance(record.get("converged_monotonic_ns"), bool)
        or not isinstance(initial_sync, Mapping)
        or initial_sync.get("count") != 1
        or initial_sync.get("return") is not True
        or not isinstance(initial_sync.get("started_monotonic_ns"), int)
        or isinstance(initial_sync.get("started_monotonic_ns"), bool)
        or not isinstance(initial_sync.get("ended_monotonic_ns"), int)
        or isinstance(initial_sync.get("ended_monotonic_ns"), bool)
        or not isinstance(initial_sync.get("duration_ns"), int)
        or isinstance(initial_sync.get("duration_ns"), bool)
        or cast(int, initial_sync["ended_monotonic_ns"])
        < cast(int, initial_sync["started_monotonic_ns"])
        or initial_sync.get("duration_ns")
        != cast(int, initial_sync["ended_monotonic_ns"])
        - cast(int, initial_sync["started_monotonic_ns"])
        or not isinstance(final, Mapping)
        or final.get("bin_locked") is not False
        or final.get("external_linked") is not True
        or final.get("video_valve_drop") is not False
        or final.get("tee_peer_is_exact_ghost") is not True
    ):
        return False

    def terminal_playing(state: object) -> bool:
        return (
            isinstance(state, Mapping)
            and state.get("current") == 4
            and state.get("pending") == 0
            and state.get("change_return") not in (0, 2)
        )

    children = final.get("active_splitmux_children")
    parent_state = final.get("parent")
    degraded_evidence = record.get("parent_known_degraded_evidence")
    parent_playing = terminal_playing(parent_state) or (
        record.get("parent_state_query_known_degraded_after_audio_source_failure") is True
        and isinstance(degraded_evidence, Mapping)
        and degraded_evidence.get("stable_identity_loss_verified") is True
        and degraded_evidence.get("recognized_exact_audio_errors") is True
        and degraded_evidence.get("parent_object_identity_verified") is True
        and isinstance(parent_state, Mapping)
        and parent_state.get("current") == 4
        and parent_state.get("pending") == 0
        and parent_state.get("change_return") == 0
    )
    queue_levels = final.get("video_queue_levels")
    queue_below_bounds = isinstance(queue_levels, Mapping) and all(
        isinstance(queue_levels.get(current), int)
        and not isinstance(queue_levels.get(current), bool)
        and isinstance(queue_levels.get(maximum), int)
        and not isinstance(queue_levels.get(maximum), bool)
        and (
            cast(int, queue_levels[maximum]) == 0
            or cast(int, queue_levels[current]) < cast(int, queue_levels[maximum])
        )
        for current, maximum in (
            ("current_buffers", "maximum_buffers"),
            ("current_bytes", "maximum_bytes"),
            ("current_time_ns", "maximum_time_ns"),
        )
    )
    final_playing = (
        parent_playing
        and terminal_playing(final.get("bin"))
        and terminal_playing(final.get("output"))
        and terminal_playing(final.get("video_valve"))
        and terminal_playing(final.get("video_queue"))
        and isinstance(children, list)
        and len(children) >= 2
        and {child.get("factory") for child in children if isinstance(child, Mapping)}
        == {"mp4mux", "filesink"}
        and all(
            isinstance(child, Mapping) and terminal_playing(child.get("state"))
            for child in children
        )
        and queue_below_bounds
    )
    if not final_playing:
        return False
    progress = record.get("video_progress")
    progress_verified = (
        isinstance(progress, Mapping)
        and progress.get("verified") is True
        and isinstance(progress.get("initial_counts"), Mapping)
        and isinstance(progress.get("final_counts"), Mapping)
        and isinstance(progress.get("deltas"), Mapping)
        and all(
            isinstance(cast(Mapping[str, object], progress["deltas"]).get(name), int)
            and not isinstance(
                cast(Mapping[str, object], progress["deltas"]).get(name),
                bool,
            )
            and cast(int, cast(Mapping[str, object], progress["deltas"])[name]) > 0
            for name in ("camera_raw", "parent_encoded", "successor")
        )
    )
    if not progress_verified:
        return False
    correction_required = record.get("correction_required")
    if correction_required is False:
        return record.get("correction_count") == 0 and record.get("set_playing_return") is None
    return (
        correction_required is True
        and record.get("correction_count") == 1
        and isinstance(record.get("set_playing_return"), int)
        and not isinstance(record.get("set_playing_return"), bool)
        and record.get("set_playing_return") != 0
        and isinstance(record.get("set_playing_started_monotonic_ns"), int)
        and not isinstance(record.get("set_playing_started_monotonic_ns"), bool)
        and isinstance(record.get("set_playing_ended_monotonic_ns"), int)
        and not isinstance(record.get("set_playing_ended_monotonic_ns"), bool)
        and isinstance(record.get("convergence_duration_ns"), int)
        and not isinstance(record.get("convergence_duration_ns"), bool)
        and cast(int, record["convergence_duration_ns"])
        < int(SUCCESSOR_STATE_CONVERGENCE_TIMEOUT_SECONDS * 1_000_000_000)
    )


def _terminal_shutdown_contract(value: object) -> bool:
    if not isinstance(value, Mapping) or value.get("phase") != "COMPLETE":
        return False
    context = value.get("context")
    observations = value.get("parent_eos_observations")
    if (
        not isinstance(context, Mapping)
        or context.get("final_generation") != 2
        or not isinstance(context.get("active_location"), str)
        or not isinstance(context.get("prepared_monotonic_ns"), int)
        or isinstance(context.get("prepared_monotonic_ns"), bool)
        or not isinstance(context.get("fragment_closed_phase_monotonic_ns"), int)
        or isinstance(context.get("fragment_closed_phase_monotonic_ns"), bool)
        or not isinstance(context.get("final_video_eos_dispatch"), Mapping)
        or not isinstance(observations, list)
        or len(observations) > 1
    ):
        return False
    dispatch = cast(Mapping[str, object], context["final_video_eos_dispatch"])
    if (
        dispatch.get("label") != "final-video-only"
        or dispatch.get("completed") is not True
        or dispatch.get("accepted") is not True
        or dispatch.get("timed_out") is not False
        or dispatch.get("error") is not None
    ):
        return False
    if not observations:
        return True
    observation = observations[0]
    return (
        isinstance(observation, Mapping)
        and observation.get("sequence") == 1
        and observation.get("source") == "pipeline0"
        and observation.get("exact_parent_object") is True
        and observation.get("phase") == "FINAL_FRAGMENT_CLOSED"
        and observation.get("final_generation") == context.get("final_generation")
        and observation.get("active_location") == context.get("active_location")
        and observation.get("fragment_closed_phase_monotonic_ns")
        == context.get("fragment_closed_phase_monotonic_ns")
        and isinstance(observation.get("delta_from_fragment_closed_phase_ns"), int)
        and not isinstance(observation.get("delta_from_fragment_closed_phase_ns"), bool)
        and cast(int, observation["delta_from_fragment_closed_phase_ns"]) >= 0
        and observation.get("final_video_eos_event_seqnum") == dispatch.get("event_seqnum")
        and observation.get("final_video_eos_dispatch_ended_monotonic_ns")
        == dispatch.get("ended_monotonic_ns")
    )


def _final_shutdown_tail_contract(value: object) -> bool:
    """Attribute terminal frames to the closed valve or post-NULL encoder output."""

    if not isinstance(value, Mapping):
        return False

    def integer(name: str) -> int | None:
        item = value.get(name)
        return item if isinstance(item, int) and not isinstance(item, bool) else None

    integer_names = (
        "frame_period_ns",
        "maximum_media_periods",
        "maximum_media_window_ns",
        "blocked_monotonic_ns",
        "held_idr_pts_ns",
        "release_requested_monotonic_ns",
        "pad_unblocked_monotonic_ns",
        "null_started_monotonic_ns",
        "null_ended_monotonic_ns",
        "null_request_after_unblock_ns",
        "first_closed_valve_buffer_monotonic_ns",
        "last_closed_valve_buffer_monotonic_ns",
        "media_window_ns",
        "media_window_frame_budget",
        "media_pts_span_ns",
        "media_pts_frame_budget",
        "held_idr_closure_wait_ns",
        "shutdown_control_window_ns",
        "fragment_closed_phase_monotonic_ns",
        "parent_count_at_block",
        "parent_last_pts_at_block",
        "routed_count_at_block",
        "initial_unrouted_frames",
        "final_parent_count",
        "final_parent_last_pts_ns",
        "final_routed_count",
        "additional_parent_frames",
        "allowed_final_unrouted_video_frames",
        "measured_final_unrouted_video_frames",
    )
    fields = {name: integer(name) for name in integer_names}
    if any(item is None for item in fields.values()):
        return False
    numbers = cast(dict[str, int], fields)
    counter = value.get("closed_valve_counter")
    if not isinstance(counter, Mapping):
        return False

    def counter_integer(name: str) -> int | None:
        item = counter.get(name)
        return item if isinstance(item, int) and not isinstance(item, bool) else None

    drop_count = counter_integer("count")
    first_pts = counter_integer("first_pts_ns")
    last_pts = counter_integer("last_pts_ns")
    non_monotonic = counter_integer("non_monotonic")
    large_gaps = counter_integer("large_gaps")
    if None in (drop_count, first_pts, last_pts, non_monotonic, large_gaps):
        return False
    drop_count = cast(int, drop_count)
    first_pts = cast(int, first_pts)
    last_pts = cast(int, last_pts)
    blocked_ns = numbers["blocked_monotonic_ns"]
    release_ns = numbers["release_requested_monotonic_ns"]
    unblocked_ns = numbers["pad_unblocked_monotonic_ns"]
    null_started_ns = numbers["null_started_monotonic_ns"]
    null_ended_ns = numbers["null_ended_monotonic_ns"]
    first_drop_ns = numbers["first_closed_valve_buffer_monotonic_ns"]
    last_drop_ns = numbers["last_closed_valve_buffer_monotonic_ns"]
    media_window_ns = numbers["media_window_ns"]
    expected_wall_budget = 1 + (
        media_window_ns + FRAME_PERIOD_NS - 1
    ) // FRAME_PERIOD_NS
    media_pts_span_ns = numbers["media_pts_span_ns"]
    expected_media_budget = 1 + (
        media_pts_span_ns + FRAME_PERIOD_NS - 1
    ) // FRAME_PERIOD_NS
    initial_unrouted = numbers["initial_unrouted_frames"]
    additional_parent = numbers["additional_parent_frames"]
    measured_tail = numbers["measured_final_unrouted_video_frames"]

    def buffer_records(name: str) -> list[tuple[int, int, bool]] | None:
        records = value.get(name)
        if (
            not isinstance(records, list)
            or len(records) > MAX_FINAL_SHUTDOWN_MEDIA_PERIODS + 2
        ):
            return None
        parsed: list[tuple[int, int, bool]] = []
        for record in records:
            if not isinstance(record, Mapping):
                return None
            pts_ns = record.get("pts_ns")
            observed_ns = record.get("observed_monotonic_ns")
            delta_unit = record.get("delta_unit")
            if (
                not isinstance(pts_ns, int)
                or isinstance(pts_ns, bool)
                or pts_ns < 0
                or not isinstance(observed_ns, int)
                or isinstance(observed_ns, bool)
                or observed_ns < 0
                or not isinstance(delta_unit, bool)
            ):
                return None
            parsed.append((pts_ns, observed_ns, delta_unit))
        if any(
            current[0] <= previous[0] or current[1] < previous[1]
            for previous, current in pairwise(parsed)
        ):
            return None
        return parsed

    closed_records = buffer_records("closed_valve_buffers")
    common_tee_records = buffer_records("common_tee_buffers")
    parent_records = buffer_records("parent_post_block_buffers")
    parent_only_records = buffer_records("post_null_parent_only_buffers")
    probe_errors = value.get("terminal_counter_probe_errors")
    if (
        closed_records is None
        or common_tee_records is None
        or parent_records is None
        or parent_only_records is None
        or not isinstance(probe_errors, list)
        or probe_errors
    ):
        return False
    common_raw = value.get("common_tee_buffers")
    common_sequence_values = (
        [
            record.get("sequence")
            for record in common_raw
            if isinstance(record, Mapping)
        ]
        if isinstance(common_raw, list)
        else []
    )
    if (
        not isinstance(common_raw, list)
        or len(common_sequence_values) != len(common_raw)
        or any(
            not isinstance(sequence, int) or isinstance(sequence, bool)
            for sequence in common_sequence_values
        )
    ):
        return False
    common_sequences = [cast(int, sequence) for sequence in common_sequence_values]
    held_pts = numbers["held_idr_pts_ns"]
    full_tail_pts = [held_pts, *(record[0] for record in parent_records)]
    closed_pts = [record[0] for record in closed_records]
    common_tee_pts = [record[0] for record in common_tee_records]
    parent_only_pts = [record[0] for record in parent_only_records]
    delivered_parent_count = len(closed_records) - 1
    if delivered_parent_count < 0 or delivered_parent_count > len(parent_records):
        return False
    expected_parent_only = parent_records[delivered_parent_count:]
    fragment_closed_ns = numbers["fragment_closed_phase_monotonic_ns"]
    baseline = value.get("common_tee_baseline")
    if not isinstance(baseline, Mapping):
        return False
    baseline_mapping = cast(Mapping[str, object], baseline)

    def baseline_integer(name: str) -> int | None:
        item = baseline_mapping.get(name)
        return item if isinstance(item, int) and not isinstance(item, bool) else None

    baseline_names = (
        "ring_capacity",
        "retained_count_at_block",
        "evicted_count_at_block",
        "total_count_at_block",
        "held_ring_index",
        "held_sequence",
        "total_count_at_release",
        "final_total_count",
        "terminal_suffix_count",
    )
    baseline_fields = {name: baseline_integer(name) for name in baseline_names}
    if any(item is None for item in baseline_fields.values()):
        return False
    baseline_numbers = cast(dict[str, int], baseline_fields)
    baseline_contract = (
        baseline_numbers["ring_capacity"] == MAX_FINAL_COMMON_TEE_RING_BUFFERS
        and 1
        <= baseline_numbers["retained_count_at_block"]
        <= MAX_FINAL_COMMON_TEE_RING_BUFFERS
        and baseline_numbers["evicted_count_at_block"] >= 0
        and baseline_numbers["total_count_at_block"]
        == baseline_numbers["retained_count_at_block"]
        + baseline_numbers["evicted_count_at_block"]
        and baseline_numbers["held_ring_index"]
        == baseline_numbers["retained_count_at_block"] - 1
        and baseline_numbers["held_sequence"] == baseline_numbers["total_count_at_block"]
        and baseline_mapping.get("held_is_exact_last_record") is True
        and baseline_numbers["total_count_at_release"]
        == baseline_numbers["total_count_at_block"]
        and baseline_numbers["final_total_count"]
        >= baseline_numbers["total_count_at_release"]
        and baseline_numbers["terminal_suffix_count"]
        == baseline_numbers["final_total_count"] - baseline_numbers["held_sequence"] + 1
        and 1
        <= baseline_numbers["terminal_suffix_count"]
        <= MAX_FINAL_SHUTDOWN_MEDIA_PERIODS + 2
        and baseline_numbers["terminal_suffix_count"] == len(common_tee_records)
        and baseline_mapping.get("terminal_suffix_retained") is True
        and common_sequences
        == list(
            range(
                baseline_numbers["held_sequence"],
                baseline_numbers["final_total_count"] + 1,
            )
        )
    )
    frozen_before = value.get("frozen_media_before_null")
    frozen_after = value.get("frozen_media_after_null")
    frozen_media_contract = (
        isinstance(frozen_before, Mapping)
        and isinstance(frozen_after, Mapping)
        and dict(frozen_before) == dict(frozen_after)
        and isinstance(frozen_before.get("name"), str)
        and MEDIA_NAME_RE.fullmatch(cast(str, frozen_before["name"])) is not None
        and isinstance(frozen_before.get("device"), int)
        and not isinstance(frozen_before.get("device"), bool)
        and isinstance(frozen_before.get("inode"), int)
        and not isinstance(frozen_before.get("inode"), bool)
        and isinstance(frozen_before.get("size_bytes"), int)
        and not isinstance(frozen_before.get("size_bytes"), bool)
        and 0 < cast(int, frozen_before["size_bytes"]) <= MAX_FINAL_FROZEN_MEDIA_BYTES
        and isinstance(frozen_before.get("sha256"), str)
        and SHA256_RE.fullmatch(cast(str, frozen_before["sha256"])) is not None
    )
    return (
        numbers["frame_period_ns"] == FRAME_PERIOD_NS
        and numbers["maximum_media_periods"] == MAX_FINAL_SHUTDOWN_MEDIA_PERIODS
        and numbers["maximum_media_window_ns"]
        == MAX_FINAL_SHUTDOWN_MEDIA_PERIODS * FRAME_PERIOD_NS
        and 0
        <= blocked_ns
        <= release_ns
        <= first_drop_ns
        <= last_drop_ns
        <= null_ended_ns
        and release_ns <= unblocked_ns <= null_started_ns <= null_ended_ns
        and numbers["null_request_after_unblock_ns"] == null_started_ns - unblocked_ns
        and 0 <= numbers["null_request_after_unblock_ns"] <= FRAME_PERIOD_NS
        and media_window_ns == last_drop_ns - release_ns
        and 0
        <= media_window_ns
        <= MAX_FINAL_SHUTDOWN_MEDIA_PERIODS * FRAME_PERIOD_NS
        and numbers["media_window_frame_budget"] == expected_wall_budget
        and numbers["held_idr_closure_wait_ns"] == release_ns - blocked_ns
        and 0
        <= numbers["held_idr_closure_wait_ns"]
        <= int(VIDEO_IDR_RELEASE_TIMEOUT_SECONDS * 1_000_000_000)
        and fragment_closed_ns <= release_ns
        and numbers["shutdown_control_window_ns"] == null_started_ns - release_ns
        and 0
        <= numbers["shutdown_control_window_ns"]
        <= MAX_FINAL_SHUTDOWN_MEDIA_PERIODS * FRAME_PERIOD_NS
        and media_pts_span_ns
        == numbers["final_parent_last_pts_ns"] - numbers["held_idr_pts_ns"]
        and 0
        <= media_pts_span_ns
        <= MAX_FINAL_SHUTDOWN_MEDIA_PERIODS * FRAME_PERIOD_NS
        and numbers["media_pts_frame_budget"] == expected_media_budget
        and numbers["allowed_final_unrouted_video_frames"] == expected_media_budget
        and numbers["parent_last_pts_at_block"] == numbers["held_idr_pts_ns"]
        and numbers["parent_count_at_block"] - numbers["routed_count_at_block"] == 1
        and initial_unrouted == 1
        and numbers["final_routed_count"] == numbers["routed_count_at_block"]
        and value.get("routed_count_stable") is True
        and additional_parent
        == numbers["final_parent_count"] - numbers["parent_count_at_block"]
        and additional_parent >= 0
        and measured_tail
        == numbers["final_parent_count"] - numbers["final_routed_count"]
        and measured_tail == initial_unrouted + additional_parent
        and len(parent_records) == additional_parent
        and len(full_tail_pts) == measured_tail
        and 1 <= measured_tail <= expected_media_budget
        and 1 <= drop_count == len(closed_records) <= measured_tail
        and len(common_tee_records) == drop_count
        and first_pts == numbers["held_idr_pts_ns"]
        and last_pts == closed_pts[-1]
        and first_drop_ns == closed_records[0][1]
        and last_drop_ns == closed_records[-1][1]
        and full_tail_pts[-1] == numbers["final_parent_last_pts_ns"]
        and common_tee_pts == closed_pts
        and closed_pts == full_tail_pts[:drop_count]
        and parent_only_pts == full_tail_pts[drop_count:]
        and parent_only_records == expected_parent_only
        and len(parent_only_records) <= 1
        and common_tee_records[0][0] == held_pts
        and common_tee_records[0][1] <= blocked_ns
        and common_tee_records[0][2] is False
        and all(record[2] is True for record in common_tee_records[1:])
        and all(record[2] is True for record in parent_records)
        and closed_records[0][2] is False
        and all(record[2] is True for record in closed_records[1:])
        and all(
            0 < current - previous <= FRAME_PERIOD_NS * 2
            for previous, current in pairwise(full_tail_pts)
        )
        and fragment_closed_ns <= release_ns
        and all(
            null_started_ns <= record[1] <= null_ended_ns
            and fragment_closed_ns <= record[1]
            for record in parent_only_records
        )
        and value.get("final_valve_drop_after_null") is True
        and value.get("final_generation_linked_after_null") is True
        and value.get("all_final_fragments_closed_after_null") is True
        and baseline_contract
        and frozen_media_contract
        and cast(int, non_monotonic) == 0
        and cast(int, large_gaps) == 0
        and counter.get("first_delta") is False
        and value.get("tail_identity_verified") is True
        and value.get("within_time_frame_contract") is True
    )


def _audio_eos_fallback_contract(transition: Mapping[str, object]) -> bool:
    resolution = transition.get("audio_eos_fallback")
    if (
        transition.get("video_eos_return") is not True
        or transition.get("audio_eos_effective_return") is not True
        or transition.get("old_audio_eos_observed") is not True
        or not isinstance(resolution, Mapping)
    ):
        return False
    branch_decision = resolution.get("serialization_decision")
    branch_decision_mapping = (
        cast(Mapping[str, object], branch_decision) if isinstance(branch_decision, Mapping) else {}
    )
    idle_block = branch_decision_mapping.get("idle_block")
    barrier = branch_decision_mapping.get("audio_barrier")
    post_barrier_queue = branch_decision_mapping.get("post_barrier_queue_proof")
    barrier_contract = isinstance(barrier, Mapping) and (
        (
            branch_decision_mapping.get("selected_natural_audio_eos") is True
            and (
                (
                    barrier.get("required") is False
                    and barrier.get("reason") == "natural_eos_already_admitted_before_barrier"
                )
                or (
                    barrier.get("mode_after_barrier") == "NATURAL"
                    and isinstance(barrier.get("seqnum"), int)
                    and not isinstance(barrier.get("seqnum"), bool)
                    and isinstance(barrier.get("dispatch"), Mapping)
                )
            )
        )
        or (
            branch_decision_mapping.get("selected_natural_audio_eos") is False
            and isinstance(barrier.get("seqnum"), int)
            and not isinstance(barrier.get("seqnum"), bool)
            and isinstance(barrier.get("observed_monotonic_ns"), int)
            and not isinstance(barrier.get("observed_monotonic_ns"), bool)
            and barrier.get("consumed_before_splitmux") is True
        )
    )
    arbiter_contract = (
        isinstance(branch_decision, Mapping)
        and isinstance(idle_block, Mapping)
        and (
            (
                idle_block.get("required") is False
                and idle_block.get("reason") == "natural_eos_already_admitted_before_topology"
                and idle_block.get("permanent_output_arbiter_remains") is True
            )
            or (
                idle_block.get("required") is True
                and idle_block.get("exact_pad_identity") is True
                and idle_block.get(
                    "retained_until_terminal_decision_and_exact_old_fragment_closure"
                )
                is True
                and idle_block.get("released_after_exact_unlink_and_old_fragment_closure") is True
                and isinstance(idle_block.get("released_monotonic_ns"), int)
                and not isinstance(idle_block.get("released_monotonic_ns"), bool)
                and idle_block.get("permanent_output_arbiter_remains") is True
            )
        )
        and barrier_contract
        and isinstance(post_barrier_queue, Mapping)
        and isinstance(post_barrier_queue.get("queue_snapshots"), list)
        and len(cast(list[object], post_barrier_queue["queue_snapshots"])) == 2
        and post_barrier_queue.get("audio_counter_stable") is True
        and resolution.get("post_closure_output_audio_eos_observation_count") == 1
        and resolution.get("post_closure_forwarded_audio_eos_count") == 1
        and resolution.get("post_closure_duplicate_audio_eos_refusal_count") == 0
    )
    if not arbiter_contract:
        return False
    branch_decision = cast(Mapping[str, object], branch_decision)
    primary = resolution.get("primary")
    if resolution.get("delivery_mode") == "loss_attributed_natural_upstream_eos":
        observation = resolution.get("natural_exact_eos_observation")
        snapshots = resolution.get("queue_snapshots")
        initial_identity = resolution.get("initial_pad_identity")
        dispatches = transition.get("eos_dispatches")
        observation_ns = (
            observation.get("observed_monotonic_ns") if isinstance(observation, Mapping) else None
        )
        second_not_found_ns = resolution.get("second_not_found_monotonic_ns")
        queue_proof = (
            isinstance(snapshots, list)
            and len(snapshots) == 2
            and all(
                isinstance(snapshot, Mapping)
                and snapshot.get("current_level_buffers") == 0
                and snapshot.get("current_level_bytes") == 0
                and snapshot.get("current_level_time_ns") == 0
                and isinstance(snapshot.get("observed_monotonic_ns"), int)
                and not isinstance(snapshot.get("observed_monotonic_ns"), bool)
                for snapshot in snapshots
            )
            and resolution.get("audio_counter_stable") is True
            and isinstance(resolution.get("queue_snapshot_separation_ns"), int)
            and not isinstance(resolution.get("queue_snapshot_separation_ns"), bool)
            and cast(int, resolution["queue_snapshot_separation_ns"])
            >= int(FALLBACK_AUDIO_STABILITY_SECONDS * 1_000_000_000)
            and isinstance(second_not_found_ns, int)
            and not isinstance(second_not_found_ns, bool)
            and cast(int, cast(Mapping[str, object], snapshots[0])["observed_monotonic_ns"])
            > second_not_found_ns
        )
        return (
            transition.get("audio_eos_primary_return") is None
            and transition.get("audio_eos_dispatch_attempted") is False
            and transition.get("audio_eos_dispatch_count") == 0
            and branch_decision.get("selected_natural_audio_eos") is True
            and branch_decision.get("manual_eos_reserved_seqnum") is None
            and resolution.get("primary") is None
            and resolution.get("fallback") is None
            and resolution.get("fallback_used") is False
            and resolution.get("attempt_count") == 0
            and resolution.get("audio_dispatch_attempted") is False
            and resolution.get("audio_dispatch_return") is None
            and resolution.get("audio_dispatch_attempt_count") == 0
            and resolution.get("effective_delivery_observed") is True
            and resolution.get("natural_eos_observation_count") == 1
            and resolution.get("loss_attribution_timing_verified") is True
            and resolution.get("exact_audio_errors_verified") is True
            and resolution.get("recognized_audio_errors_verified") is True
            and resolution.get("all_exact_audio_errors_precede_natural_eos") is True
            and resolution.get("stable_not_found_pair_verified") is True
            and resolution.get("discovery_closed_at_stable_pair") is True
            and resolution.get("no_invalid_discovery_status_after_first_error") is True
            and resolution.get("no_rematch_after_natural_eos") is True
            and resolution.get("natural_eos_timing_class")
            in (
                "after_error_before_first_not_found",
                "between_stable_not_found_pair",
                "after_stable_not_found_pair_before_handoff",
            )
            and resolution.get("final_post_eos_absence_check_verified") is True
            and isinstance(resolution.get("delta_from_first_not_found_ns"), int)
            and not isinstance(resolution.get("delta_from_first_not_found_ns"), bool)
            and isinstance(resolution.get("delta_from_second_not_found_ns"), int)
            and not isinstance(resolution.get("delta_from_second_not_found_ns"), bool)
            and resolution.get("audio_probe_observed_before_handoff") is True
            and isinstance(observation, Mapping)
            and isinstance(observation.get("seqnum"), int)
            and not isinstance(observation.get("seqnum"), bool)
            and isinstance(observation_ns, int)
            and not isinstance(observation_ns, bool)
            and observation.get("active_identity_verified") is True
            and observation.get("forwarded_to_splitmux") is True
            and observation.get("duplicate_refused") is False
            and observation.get("generation_external_linked") is True
            and observation.get("generation_valve_drop") is False
            and observation.get("generation_retired") is False
            and isinstance(initial_identity, Mapping)
            and observation.get("pad_path") == initial_identity.get("path")
            and observation.get("pad_name") == initial_identity.get("name")
            and observation.get("parent_path") == initial_identity.get("parent_path")
            and observation.get("peer_path") == initial_identity.get("peer_path")
            and queue_proof
            and resolution.get("post_closure_pad_identity") == initial_identity
            and resolution.get("post_closure_pad_identity_verified") is True
            and isinstance(dispatches, list)
            and len(dispatches) == 1
            and isinstance(dispatches[0], Mapping)
            and dispatches[0].get("label") == "loss-retired-video"
            and dispatches[0].get("accepted") is True
        )
    if (
        not isinstance(primary, Mapping)
        or primary.get("label") != "loss-retired-audio"
        or primary.get("completed") is not True
        or primary.get("timed_out") is not False
        or primary.get("error") is not None
        or not isinstance(primary.get("event_seqnum"), int)
        or isinstance(primary.get("event_seqnum"), bool)
        or resolution.get("output_audio_eos_observation_count_before_primary") != 0
    ):
        return False
    primary_observation = resolution.get("primary_exact_eos_observation")
    primary_observation_matches = (
        isinstance(primary_observation, Mapping)
        and primary_observation.get("seqnum") == primary.get("event_seqnum")
        and isinstance(primary_observation.get("pad_path"), str)
        and bool(primary_observation.get("pad_path"))
        and isinstance(primary_observation.get("observed_monotonic_ns"), int)
        and not isinstance(primary_observation.get("observed_monotonic_ns"), bool)
    )
    if primary.get("accepted") is True:
        return (
            transition.get("audio_eos_primary_return") is True
            and branch_decision.get("selected_natural_audio_eos") is False
            and branch_decision.get("manual_eos_reserved_seqnum") == primary.get("event_seqnum")
            and resolution.get("fallback_used") is False
            and resolution.get("fallback") is None
            and resolution.get("delivery_mode") == "primary_queue_sink_accepted"
            and resolution.get("effective_delivery_observed") is True
            and primary_observation_matches
        )
    snapshots = resolution.get("queue_snapshots")
    queue_proof = (
        isinstance(snapshots, list)
        and len(snapshots) == 2
        and all(
            isinstance(snapshot, Mapping)
            and snapshot.get("current_level_buffers") == 0
            and snapshot.get("current_level_bytes") == 0
            and snapshot.get("current_level_time_ns") == 0
            for snapshot in snapshots
        )
        and resolution.get("audio_counter_stable") is True
        and isinstance(resolution.get("queue_snapshot_separation_ns"), int)
        and not isinstance(resolution.get("queue_snapshot_separation_ns"), bool)
        and cast(int, resolution["queue_snapshot_separation_ns"])
        >= int(FALLBACK_AUDIO_STABILITY_SECONDS * 1_000_000_000)
    )
    common_refusal_proof = (
        resolution.get("eligible") is True
        and resolution.get("stable_not_found_confirmed") is True
        and isinstance(resolution.get("exact_audio_error_count"), int)
        and not isinstance(resolution.get("exact_audio_error_count"), bool)
        and cast(int, resolution["exact_audio_error_count"]) >= 1
        and isinstance(resolution.get("initial_pad_identity"), Mapping)
        and queue_proof
    )
    if not common_refusal_proof:
        return False
    if primary_observation_matches:
        return (
            primary.get("accepted") is False
            and transition.get("audio_eos_primary_return") is False
            and branch_decision.get("selected_natural_audio_eos") is False
            and branch_decision.get("manual_eos_reserved_seqnum") == primary.get("event_seqnum")
            and resolution.get("fallback_used") is False
            and resolution.get("fallback") is None
            and resolution.get("effective_delivery_observed") is True
            and resolution.get("direct_fallback_suppressed_to_avoid_duplicate_eos") is True
            and resolution.get("attempt_count") == 0
            and resolution.get("delivery_mode")
            == "exact_output_pad_eos_observed_after_primary_refusal"
            and resolution.get("post_closure_pad_identity")
            == resolution.get("initial_pad_identity")
            and resolution.get("post_closure_pad_identity_verified") is True
        )
    return False


def execute(output_directory: Path, loss_timeout_seconds: int) -> dict[str, object]:
    if not MIN_LOSS_TIMEOUT_SECONDS <= loss_timeout_seconds <= MAX_LOSS_TIMEOUT_SECONDS:
        raise HarnessError("loss timeout must be within 30-60 seconds")
    release = _shared._release_identity()
    before_unit = _shared._read_unit_state()
    before_throttle = _shared._read_throttle()
    if release.get("release") != EXPECTED_RELEASE:
        raise HarnessError("installed release differs from the reviewed exact release")
    if before_unit.get("restarts") != 0:
        raise HarnessError("dashcamd restart counter was not zero before the experiment")
    if before_throttle != "throttled=0x0":
        raise HarnessError("Pi was throttled before the experiment")
    config = load_config(_shared.CONFIG_PATH)
    storage = run_live_storage_preflight(config)
    if (
        not storage.ready
        or not storage.probe_attempted
        or not storage.probe_succeeded
        or storage.facts is None
        or storage.facts.mount.target != str(RECORDING_ROOT)
        or storage.facts.mount.filesystem != "exfat"
        or storage.facts.mount.label != "DASHCAM"
    ):
        raise HarnessError("production exact exFAT/sentinel preflight is not READY")
    selector = parse_alsa_selector(config.audio.device_match)
    discovery = discover_capture_device(selector)
    if discovery.status is not AudioDiscoveryStatus.MATCHED or discovery.device is None:
        raise HarnessError(f"exact microphone was not matched: {discovery.status.value}")
    microphone_identity = discovery.device.identity
    if (
        microphone_identity.vendor_id.lower() != "08bb"
        or microphone_identity.product_id.lower() != "2902"
        or not microphone_identity.physical_path
    ):
        raise HarnessError("matched microphone is not the reviewed exact USB identity")
    directory_evidence = _prepare_output_directory(output_directory)
    experiment = PhysicalLossExperiment(
        output_directory,
        selector,
        discovery.device,
        loss_timeout_seconds,
    )
    try:
        runtime = experiment.run()
    except BaseException as error:
        raise _physical_failure(experiment, error) from error
    try:
        post_loss_discovery = discover_capture_device(selector)
        post_loss_evidence = {
            "attempts": 1,
            "status": post_loss_discovery.status.value,
            "device_exposed": post_loss_discovery.device is not None,
        }
        if (
            post_loss_discovery.status is not AudioDiscoveryStatus.NOT_FOUND
            or post_loss_discovery.device is not None
        ):
            raise HarnessError("post-loss exact microphone discovery did not prove NOT_FOUND")
        media = validate_media(output_directory)
        _validate_runtime_media_binding(runtime, media)
        after_unit = _shared._read_unit_state()
        after_throttle = _shared._read_throttle()
    except BaseException as error:
        raise _physical_failure(experiment, error) from error
    if after_unit != before_unit or after_unit["restarts"] != 0:
        refusal = HarnessError("dashcamd state/restart counter changed")
        raise _physical_failure(experiment, refusal) from refusal
    if after_throttle != "throttled=0x0":
        refusal = HarnessError("Pi throttled during the experiment")
        raise _physical_failure(experiment, refusal) from refusal
    transitions = cast(list[Mapping[str, object]], runtime["transitions"])
    parent = cast(Mapping[str, object], runtime["parent"])
    generations = cast(Mapping[str, Mapping[str, object]], runtime["generations"])
    generation_video = [
        cast(Mapping[str, object], generations[str(number)]["video"]) for number in (1, 2)
    ]
    video_source = cast(Mapping[str, object], parent["video_source"])
    routed_video_count = sum(cast(int, counter["count"]) for counter in generation_video)
    final_unrouted = parent.get("measured_final_unrouted_video_frames")
    loss_burst = cast(Mapping[str, object], runtime["audio_loss_error_burst"])
    latency_refusals = cast(
        Mapping[str, object],
        runtime["latency_recalculation_refusals"],
    )
    physical_loss_discovery = cast(
        Mapping[str, object],
        runtime["physical_loss_discovery"],
    )
    expected_audio_error = runtime.get("expected_audio_error")
    video_path_diagnostics = runtime.get("video_path_diagnostics")
    successor_state_convergence = runtime.get("successor_state_convergence")
    terminal_shutdown = runtime.get("terminal_shutdown")
    final_shutdown_tail = (
        terminal_shutdown.get("video_tail")
        if isinstance(terminal_shutdown, Mapping)
        else None
    )
    passed = (
        len(transitions) == 1
        and transitions[0].get("trigger") == "stable_identity_not_found"
        and transitions[0].get("within_one_frame") is True
        and transitions[0].get("new_first_video_is_idr") is True
        and transitions[0].get("no_post_loss_audio_buffer_wait") is True
        and _loss_burst_contract(loss_burst)
        and (
            (
                expected_audio_error is None
                and loss_burst.get("corroborated") is False
                and transitions[0].get("gst_error_corroborated") is False
            )
            or (
                isinstance(expected_audio_error, Mapping)
                and expected_audio_error.get("exact_registered_audio_source") is True
                and loss_burst.get("corroborated") is True
                and transitions[0].get("gst_error_corroborated") is True
            )
        )
        and _physical_loss_discovery_contract(physical_loss_discovery)
        and _loss_latency_refusal_contract(latency_refusals)
        and _video_path_diagnostic_contract(video_path_diagnostics)
        and _successor_state_convergence_contract(successor_state_convergence)
        and _terminal_shutdown_contract(terminal_shutdown)
        and _final_shutdown_tail_contract(final_shutdown_tail)
        and _audio_eos_fallback_contract(transitions[0])
        and not runtime["warnings"]
        and not runtime["errors"]
        and all(
            counter.get("non_monotonic") == 0
            and counter.get("large_gaps") == 0
            and cast(int, counter.get("count", 0)) > 0
            for counter in generation_video
        )
        and video_source.get("non_monotonic") == 0
        and video_source.get("large_gaps") == 0
        and isinstance(final_unrouted, int)
        and not isinstance(final_unrouted, bool)
        and isinstance(final_shutdown_tail, Mapping)
        and final_unrouted
        == final_shutdown_tail.get("measured_final_unrouted_video_frames")
        and routed_video_count + final_unrouted == video_source.get("count")
        and parent.get("camera_object_preserved") is True
        and parent.get("encoder_object_preserved") is True
        and parent.get("parser_object_preserved") is True
        and parent.get("audio_source_object_preserved") is True
    )
    if not passed:
        refusal = HarnessError("physical-loss immutable-generation acceptance failed")
        raise _physical_failure(experiment, refusal) from refusal
    return {
        "schema_version": 1,
        "passed": True,
        "safe_to_integrate_production": False,
        "scope": "owner_assisted_physical_loss_capability_only",
        "shared_generation_manifest_sha256": SHARED_MANIFEST_SHA256,
        "release": release,
        "unit_before": before_unit,
        "unit_after": after_unit,
        "throttle_before": before_throttle,
        "throttle_after": after_throttle,
        "microphone": {
            "status": discovery.status.value,
            "endpoint": discovery.device.capture_endpoint,
            "identity": {
                "vendor_id": discovery.device.identity.vendor_id,
                "product_id": discovery.device.identity.product_id,
                "product": discovery.device.identity.product,
                "physical_path": discovery.device.identity.physical_path,
                "serial": discovery.device.identity.serial,
                "alsa_card_id": discovery.device.identity.alsa_card_id,
            },
        },
        "post_loss_microphone_discovery": post_loss_evidence,
        "storage_preflight": {
            "ready": storage.ready,
            "probe_attempted": storage.probe_attempted,
            "probe_succeeded": storage.probe_succeeded,
            "target": storage.facts.mount.target,
            "filesystem": storage.facts.mount.filesystem,
            "label": storage.facts.mount.label,
            "device_id": storage.facts.mount.device_id,
        },
        "directory": directory_evidence,
        "runtime": runtime,
        "media": media,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-manifest-sha256", required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run-experiment")
    run.add_argument("--output-directory", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument(
        "--loss-timeout-seconds",
        type=int,
        default=DEFAULT_LOSS_TIMEOUT_SECONDS,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    started = time.monotonic_ns()
    verify_manifest(arguments.expected_manifest_sha256)
    result_path = _validated_result_path(arguments.output, arguments.output_directory)
    try:
        result = execute(
            arguments.output_directory,
            arguments.loss_timeout_seconds,
        )
        status = 0
    except BaseException as error:
        result = {
            "schema_version": 1,
            "passed": False,
            "safe_to_integrate_production": False,
            "scope": "owner_assisted_physical_loss_capability_only",
            "shared_generation_manifest_sha256": SHARED_MANIFEST_SHA256,
            "error_type": type(error).__name__,
            "error": _shared._bounded_detail(error),
        }
        if isinstance(error, PhysicalExperimentFailure):
            result["diagnostic"] = error.diagnostic
        status = 1
    document = {
        **result,
        "started_monotonic_ns": started,
        "ended_monotonic_ns": time.monotonic_ns(),
    }
    _write_atomic_exclusive_json(result_path, document)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
