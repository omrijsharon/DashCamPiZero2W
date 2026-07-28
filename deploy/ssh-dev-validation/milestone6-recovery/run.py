#!/usr/bin/env python3
"""Hash-closed exact-Pi recorder recovery injection.

This is deliberately a validation entry point, not a production mode.  It wraps
only the first otherwise-real GStreamer backend and raises one reviewed
``RecoverablePipelineError`` after bounded, measured real encoding.  Every
replacement backend is returned unmodified.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import json
import math
import os
import re
import stat
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import Protocol, cast
from uuid import UUID

import dashcam
from dashcam.catalog import ClipCatalog
from dashcam.config import load_config
from dashcam.diagnostics.media import (
    CommandResult,
    MediaThresholds,
    Outcome,
    TimelineEvidence,
    probe_media_file,
    run_fixed_argv,
)
from dashcam.metadata.reconcile import parse_sidecar_bytes
from dashcam.recorder.daemon import DaemonLimits, DaemonOutcome, RecorderDaemon
from dashcam.recorder.finalizer import (
    DurableRootedFinalizationFilesystem,
    RecorderClipFinalizer,
)
from dashcam.recorder.gstreamer import (
    EffectiveCaps,
    EncoderIdentity,
    FinalizedFragment,
    FrameCounters,
    GStreamerBackend,
    OpenedFragment,
    SegmentedOutputConfig,
)
from dashcam.recorder.pipeline import (
    CameraOwnership,
    RecoverablePipelineError,
    VideoProfile,
)
from dashcam.recorder.runtime import GStreamerRecorderRuntime, RuntimeBackend
from dashcam.state import ClipLifecycle
from dashcam.storage.naming import finalized_unsynced_clip_pair, provisional_clip_pair

CONFIG_PATH = Path("/etc/dashcam/config.toml")
IDENTITY_PATH = Path("/etc/dashcam/storage-volume.env")
CATALOG_PATH = Path("/var/lib/dashcam/catalog.sqlite3")
RECORDING_ROOT = Path("/srv/dashcam")
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
EXPECTED_USER = "dashcam"
EXPECTED_UNIT = "dashcamd.service"
INJECTION_SECONDS = 5.0
INJECTION_FRAMES = 150
POST_RECOVERY_FRAMES = 150
ACTIVE_SCENARIO_DEADLINE_SECONDS = 90.0
SCENARIO_TIMEOUT_SECONDS = 119.0
PROOF_MAX_AGE_SECONDS = 120.0
MAX_JSON_BYTES = 1024 * 1024
MAX_STATUS_EVENTS = 32
MAX_DIRECTORY_ENTRIES = 4096
MAX_SIDECAR_BYTES = 1024 * 1024
SHA256_RE = re.compile(r"[0-9a-f]{64}")
RELEASE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
BOOT_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
MANIFEST_MEMBERS = ("README.md", "run.py")
PROOF_KEYS = frozenset(
    {
        "schema_version",
        "unit",
        "active_state",
        "sub_state",
        "main_pid",
        "boot_id",
        "observed_monotonic_ns",
    }
)
REQUIRED_MEDIA_CHECKS = frozenset(
    {
        "video_codec_h264",
        "decoder_run",
        "first_video_packet_keyframe",
        "first_video_frame_keyframe",
        "first_video_packet_idr",
    }
)


class HarnessError(RuntimeError):
    """A live acceptance precondition or result failed closed."""


class ScenarioFailure(HarnessError):
    """A live scenario refusal carrying bounded machine-readable diagnostics."""

    def __init__(self, message: str, diagnostics: Mapping[str, object]) -> None:
        super().__init__(message)
        self.diagnostics = dict(diagnostics)


class BackendLike(Protocol):
    @property
    def effective_caps(self) -> EffectiveCaps | None: ...

    @property
    def encoder_identity(self) -> EncoderIdentity | None: ...

    def frame_counters(self) -> FrameCounters: ...

    async def start(self, requested_profile: VideoProfile) -> VideoProfile: ...

    async def run(self, stop_requested: asyncio.Event) -> None: ...

    async def stop(self) -> None: ...

    async def wait_for_first_fragment_opened(self) -> OpenedFragment: ...

    async def next_finalized_fragment(self) -> FinalizedFragment: ...

    def mark_finalized_fragment_processed(self) -> None: ...

    async def wait_for_finalized_fragments_processed(self) -> None: ...


def _bounded_regular_bytes(path: Path, maximum: int, *, root_owned: bool = False) -> bytes:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise HarnessError(f"{path} is not a regular file")
        if root_owned and (info.st_uid != 0 or info.st_mode & 0o022):
            raise HarnessError("inactive-unit proof is not root-owned and write-protected")
        retained = bytearray()
        while len(retained) <= maximum:
            chunk = os.read(descriptor, min(65_536, maximum + 1 - len(retained)))
            if not chunk:
                break
            retained.extend(chunk)
        if len(retained) > maximum:
            raise HarnessError(f"{path} exceeded its read bound")
        return bytes(retained)
    finally:
        os.close(descriptor)


def _sha256_file(path: Path, *, maximum: int | None = None) -> str:
    digest = hashlib.sha256()
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags)
    total = 0
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise HarnessError(f"{path} is not a regular file")
        while chunk := os.read(descriptor, 1024 * 1024):
            total += len(chunk)
            if maximum is not None and total > maximum:
                raise HarnessError(f"{path} exceeded its hash bound")
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _strict_json_object(payload: bytes, description: str) -> Mapping[str, object]:
    def no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise HarnessError(f"{description} contains a duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(
            payload,
            object_pairs_hook=no_duplicates,
            parse_constant=lambda value: (_ for _ in ()).throw(
                HarnessError(f"{description} contains {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HarnessError(f"{description} is not strict JSON") from error
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise HarnessError(f"{description} root is not an object")
    return cast(Mapping[str, object], value)


def verify_manifest(expected_sha256: str, directory: Path | None = None) -> dict[str, str]:
    """Require one closed manifest containing only this README and script."""

    if SHA256_RE.fullmatch(expected_sha256) is None:
        raise HarnessError("expected manifest SHA-256 is not canonical")
    root = (directory or Path(__file__).resolve().parent).resolve(strict=True)
    manifest = root / "SHA256SUMS"
    if _sha256_file(manifest, maximum=4096) != expected_sha256:
        raise HarnessError("reviewed manifest hash differs from the supplied hash")
    entries: dict[str, str] = {}
    for line in _bounded_regular_bytes(manifest, 4096).decode("ascii").splitlines():
        pieces = line.split("  ")
        if len(pieces) != 2 or SHA256_RE.fullmatch(pieces[0]) is None:
            raise HarnessError("manifest contains an invalid entry")
        digest, name = pieces
        if name in entries or name not in MANIFEST_MEMBERS:
            raise HarnessError("manifest member set is not closed")
        entries[name] = digest
    if tuple(sorted(entries)) != tuple(sorted(MANIFEST_MEMBERS)):
        raise HarnessError("manifest omits a required member")
    for name, digest in entries.items():
        member = root / name
        if member.parent != root or _sha256_file(member, maximum=2 * 1024 * 1024) != digest:
            raise HarnessError(f"manifest member {name} failed verification")
    return entries


def verify_inactive_proof(
    path: Path,
    expected_sha256: str,
    *,
    boot_id: str,
    monotonic_ns: int,
    require_root_owner: bool = True,
) -> dict[str, object]:
    """Validate a fresh, externally produced read-only systemd-state proof."""

    if SHA256_RE.fullmatch(expected_sha256) is None:
        raise HarnessError("inactive-proof SHA-256 is not canonical")
    payload = _bounded_regular_bytes(path, 4096, root_owned=require_root_owner)
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise HarnessError("inactive-unit proof hash differs from the supplied hash")
    document = _strict_json_object(payload, "inactive-unit proof")
    if frozenset(document) != PROOF_KEYS:
        raise HarnessError("inactive-unit proof schema is not closed")
    if (
        document["schema_version"] != 1
        or document["unit"] != EXPECTED_UNIT
        or document["active_state"] != "inactive"
        or document["sub_state"] != "dead"
        or document["main_pid"] != 0
        or document["boot_id"] != boot_id
    ):
        raise HarnessError("inactive-unit proof does not prove this boot's inactive unit")
    observed = document["observed_monotonic_ns"]
    if isinstance(observed, bool) or not isinstance(observed, int) or observed < 0:
        raise HarnessError("inactive-unit proof monotonic timestamp is invalid")
    age_ns = monotonic_ns - observed
    if age_ns < 0 or age_ns > round(PROOF_MAX_AGE_SECONDS * 1_000_000_000):
        raise HarnessError("inactive-unit proof is stale or from a future observation")
    return dict(document)


def verify_release_layout(
    *,
    executable: Path,
    prefix: Path,
    module_path: Path,
    releases_root: Path = Path("/opt/dashcam/releases"),
    executable_lstat: Callable[[Path], os.stat_result] = os.lstat,
    executable_realpath: Callable[[Path], str] = os.path.realpath,
) -> dict[str, str]:
    """Bind lexical venv provenance without following its interpreter symlink."""

    if (
        not executable.is_absolute()
        or not prefix.is_absolute()
        or not module_path.is_absolute()
        or not releases_root.is_absolute()
        or ".." in executable.parts
        or ".." in prefix.parts
        or ".." in module_path.parts
        or ".." in releases_root.parts
    ):
        raise HarnessError("installed release paths must be absolute and lexical")
    try:
        relative = executable.relative_to(releases_root)
    except ValueError as error:
        raise HarnessError("invoked interpreter is outside the release root") from error
    if (
        len(relative.parts) != 4
        or RELEASE_NAME_RE.fullmatch(relative.parts[0]) is None
        or relative.parts[1:] != ("venv", "bin", "python")
    ):
        raise HarnessError("invoked interpreter is not an exact release venv executable")
    release_root = releases_root / relative.parts[0]
    expected_prefix = release_root / "venv"
    if prefix != expected_prefix:
        raise HarnessError("sys.prefix is not bound to the invoked release venv")
    canonical_releases_root = releases_root.resolve(strict=True)
    canonical_release_root = release_root.resolve(strict=True)
    if (
        canonical_release_root != release_root
        or canonical_release_root.parent != canonical_releases_root
    ):
        raise HarnessError("release directory provenance is not canonical")
    executable_info = executable_lstat(executable)
    if not (
        stat.S_ISREG(executable_info.st_mode)
        or stat.S_ISLNK(executable_info.st_mode)
    ):
        raise HarnessError("invoked venv interpreter is not a regular file or symlink")
    interpreter_target = Path(executable_realpath(executable))
    if not interpreter_target.is_absolute() or not interpreter_target.is_file():
        raise HarnessError("invoked venv interpreter has no regular target")
    canonical_module = module_path.resolve(strict=True)
    if canonical_release_root not in canonical_module.parents:
        raise HarnessError("harness is not importing the installed release interpreter/package")
    return {
        "release": relative.parts[0],
        "interpreter": executable.as_posix(),
        "interpreter_target": interpreter_target.as_posix(),
        "venv_prefix": prefix.as_posix(),
        "dashcam_module": canonical_module.as_posix(),
    }


def verify_installed_release() -> dict[str, str]:
    """Prove the invocation, venv prefix, and package share one exact release."""

    module_file = dashcam.__file__
    if not isinstance(module_file, str) or not module_file:
        raise HarnessError("installed dashcam package has no module path")
    return verify_release_layout(
        executable=Path(os.path.abspath(sys.executable)),
        prefix=Path(os.path.abspath(sys.prefix)),
        module_path=Path(os.path.abspath(module_file)),
    )


def _read_boot_id() -> str:
    payload = _bounded_regular_bytes(BOOT_ID_PATH, 64).decode("ascii").strip()
    if BOOT_ID_RE.fullmatch(payload) is None:
        raise HarnessError("kernel boot ID is not canonical")
    return payload


def verify_live_identity() -> dict[str, object]:
    """Require the unprivileged service account and no competing daemon process."""

    geteuid = getattr(os, "geteuid", None)
    if not callable(geteuid):
        raise HarnessError("POSIX effective-user lookup is unavailable")
    effective_uid = int(geteuid())
    pwd_module = importlib.import_module("pwd")
    getpwuid = getattr(pwd_module, "getpwuid", None)
    if not callable(getpwuid):
        raise HarnessError("POSIX account lookup is unavailable")
    account = getpwuid(effective_uid)
    account_name = getattr(account, "pw_name", None)
    if account_name != EXPECTED_USER:
        raise HarnessError("recovery harness must run as the dashcam service user")
    examined = 0
    competitors: list[int] = []
    for candidate in sorted(Path("/proc").iterdir(), key=lambda item: item.name):
        if not candidate.name.isdecimal():
            continue
        examined += 1
        if examined > MAX_DIRECTORY_ENTRIES:
            raise HarnessError("process scan exceeded its bound")
        try:
            payload = _bounded_regular_bytes(candidate / "cmdline", 4096)
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        arguments = tuple(part for part in payload.split(b"\0") if part)
        if any(
            arguments[index : index + 2] == (b"-m", b"dashcam.daemon")
            for index in range(max(0, len(arguments) - 1))
        ):
            competitors.append(int(candidate.name))
    if competitors:
        raise HarnessError("a competing dashcam daemon process is still present")
    return {"user": account_name, "uid": effective_uid, "processes_examined": examined}


class InjectedRecoveryBackend:
    """Delegate one real backend while injecting one measured run-task failure."""

    def __init__(
        self,
        inner: BackendLike,
        *,
        injection_seconds: float = INJECTION_SECONDS,
        injection_frames: int = INJECTION_FRAMES,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        if (
            isinstance(injection_seconds, bool)
            or not isinstance(injection_seconds, int | float)
            or not math.isfinite(injection_seconds)
            or injection_seconds < 0
            or isinstance(injection_frames, bool)
            or not isinstance(injection_frames, int)
            or injection_frames < 0
        ):
            raise ValueError("injection thresholds must be finite and non-negative")
        self._inner = inner
        self._injection_seconds = float(injection_seconds)
        self._injection_frames = injection_frames
        self._monotonic = monotonic
        self._sleep = sleep
        self._inner_run_task: asyncio.Task[None] | None = None
        self.injected = False
        self.inner_run_joined = False
        self.stop_delegated = False
        self.opened: OpenedFragment | None = None
        self.encoded_delta_at_injection: int | None = None
        self.elapsed_at_injection_s: float | None = None

    @property
    def effective_caps(self) -> EffectiveCaps | None:
        return self._inner.effective_caps

    @property
    def encoder_identity(self) -> EncoderIdentity | None:
        return self._inner.encoder_identity

    def frame_counters(self) -> FrameCounters:
        return self._inner.frame_counters()

    async def start(self, requested_profile: VideoProfile) -> VideoProfile:
        return await self._inner.start(requested_profile)

    async def _pause(self) -> None:
        if self._sleep is None:
            await asyncio.sleep(0.05)
            return
        await self._sleep(0.05)

    async def run(self, stop_requested: asyncio.Event) -> None:
        if self._inner_run_task is not None:
            raise HarnessError("injection wrapper is single-use")
        task = asyncio.create_task(
            self._inner.run(stop_requested),
            name="recovery-injection-inner-gstreamer",
        )
        self._inner_run_task = task
        try:
            self.opened = await self._inner.wait_for_first_fragment_opened()
            baseline = self._inner.frame_counters().encoded_access_units
            started = self._monotonic()
            while True:
                if task.done():
                    await task
                    raise HarnessError("real backend exited before the reviewed injection")
                current = self._inner.frame_counters().encoded_access_units
                elapsed = self._monotonic() - started
                encoded_delta = current - baseline
                if elapsed >= self._injection_seconds and encoded_delta >= self._injection_frames:
                    self.elapsed_at_injection_s = elapsed
                    self.encoded_delta_at_injection = encoded_delta
                    self.injected = True
                    raise RecoverablePipelineError(
                        "reviewed exact-Pi Milestone 6 recovery injection"
                    )
                await self._pause()
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.inner_run_joined = True

    async def stop(self) -> None:
        self.stop_delegated = True
        await self._inner.stop()
        task = self._inner_run_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if task is not None:
            self.inner_run_joined = task.done()

    async def wait_for_first_fragment_opened(self) -> OpenedFragment:
        return await self._inner.wait_for_first_fragment_opened()

    async def next_finalized_fragment(self) -> FinalizedFragment:
        return await self._inner.next_finalized_fragment()

    def mark_finalized_fragment_processed(self) -> None:
        self._inner.mark_finalized_fragment_processed()

    async def wait_for_finalized_fragments_processed(self) -> None:
        await self._inner.wait_for_finalized_fragments_processed()

    def diagnostic_snapshot(self) -> dict[str, object]:
        task = self._inner_run_task
        if task is None:
            task_state = "not_created"
            task_error = None
        elif not task.done():
            task_state = "running"
            task_error = None
        elif task.cancelled():
            task_state = "cancelled"
            task_error = None
        else:
            task_state = "finished"
            error = task.exception()
            task_error = (
                None
                if error is None
                else f"{type(error).__name__}: {error}"[:512]
            )
        try:
            counters = asdict(self._inner.frame_counters())
        except Exception as error:
            counters = {"error": f"{type(error).__name__}: {error}"[:512]}
        return {
            "injected": self.injected,
            "inner_run_joined": self.inner_run_joined,
            "stop_delegated": self.stop_delegated,
            "opened_sequence": None if self.opened is None else self.opened.sequence,
            "elapsed_at_injection_s": self.elapsed_at_injection_s,
            "encoded_delta_at_injection": self.encoded_delta_at_injection,
            "inner_run_task_state": task_state,
            "inner_run_task_error": task_error,
            "inner_counters": counters,
        }


class FirstAttemptRecoveryFactory:
    """Wrap exactly the first backend and return every replacement unchanged."""

    def __init__(
        self,
        inner_factory: Callable[[SegmentedOutputConfig], BackendLike] | None = None,
        *,
        injection_seconds: float = INJECTION_SECONDS,
        injection_frames: int = INJECTION_FRAMES,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._inner_factory = inner_factory or (
            lambda output: GStreamerBackend(output=output)
        )
        self._injection_seconds = injection_seconds
        self._injection_frames = injection_frames
        self._monotonic = monotonic
        self._sleep = sleep
        self.outputs: list[SegmentedOutputConfig] = []
        self.backends: list[BackendLike] = []
        self.wrapper: InjectedRecoveryBackend | None = None

    def __call__(self, output: SegmentedOutputConfig) -> RuntimeBackend:
        inner = self._inner_factory(output)
        self.outputs.append(output)
        self.backends.append(inner)
        if len(self.backends) == 1:
            wrapper = InjectedRecoveryBackend(
                inner,
                injection_seconds=self._injection_seconds,
                injection_frames=self._injection_frames,
                monotonic=self._monotonic,
                sleep=self._sleep,
            )
            self.wrapper = wrapper
            return wrapper
        return cast(RuntimeBackend, inner)


class BoundedRecordingNotifier:
    """Constant-space observer used instead of the environment's notify socket."""

    def __init__(self) -> None:
        self.events: list[dict[str, str]] = []

    def _record(self, operation: str, value: str = "") -> bool:
        if len(self.events) >= MAX_STATUS_EVENTS:
            raise HarnessError("notifier event bound was exceeded")
        if len(value) > 512 or "\0" in value:
            raise HarnessError("notifier status is not bounded")
        self.events.append({"operation": operation, "status": value})
        return True

    def ready(self, status: str) -> bool:
        return self._record("ready", status)

    def status(self, status: str) -> bool:
        return self._record("status", status)

    def watchdog(self) -> bool:
        return self._record("watchdog")

    def stopping(self, status: str) -> bool:
        return self._record("stopping", status)


def _notified_states(events: Sequence[Mapping[str, str]]) -> tuple[str, ...]:
    states: list[str] = []
    for event in events:
        match = re.search(r"(?:^| )state=([A-Z_]+)(?: |$)", event["status"])
        if match is not None:
            states.append(match.group(1))
    return tuple(states)


def _has_recovery_order(states: Sequence[str]) -> bool:
    expected = iter(("FAULTED", "STARTING", "RECORDING"))
    sought = next(expected)
    for state in states:
        if state == sought:
            try:
                sought = next(expected)
            except StopIteration:
                return True
    return False


def _backend_encoded(backend: BackendLike) -> int:
    counters = backend.frame_counters()
    if not isinstance(counters, FrameCounters):
        raise HarnessError("replacement backend lacks exact frame counters")
    return counters.encoded_access_units


async def _wait_until(
    predicate: Callable[[], bool],
    *,
    deadline: float,
    detail: str,
    terminal: Callable[[], bool] | None = None,
    diagnostics: Callable[[], Mapping[str, object]] | None = None,
) -> None:
    while not predicate():
        if terminal is not None and terminal():
            evidence = {} if diagnostics is None else diagnostics()
            raise ScenarioFailure(
                f"{detail}: recorder daemon terminated first",
                evidence,
            )
        if time.monotonic() >= deadline:
            evidence = {} if diagnostics is None else diagnostics()
            raise ScenarioFailure(detail, evidence)
        await asyncio.sleep(0.05)


def _bounded_error(error: BaseException) -> str:
    detail = " ".join(
        f"{type(error).__name__}: {error}".replace("\0", " ").splitlines()
    )
    return (detail or type(error).__name__)[:512]


def _daemon_task_diagnostic(task: asyncio.Task[object]) -> dict[str, object]:
    if not task.done():
        return {"state": "running", "result": None, "error": None}
    if task.cancelled():
        return {"state": "cancelled", "result": None, "error": None}
    error = task.exception()
    if error is not None:
        return {
            "state": "failed",
            "result": None,
            "error": _bounded_error(error),
        }
    result = task.result()
    outcome = getattr(result, "outcome", None)
    final_status = getattr(result, "final_status", None)
    outcome_value = getattr(outcome, "value", None)
    status_mapping = (
        final_status.as_dict()
        if final_status is not None and callable(getattr(final_status, "as_dict", None))
        else None
    )
    return {
        "state": "finished",
        "result": {
            "type": type(result).__name__,
            "outcome": outcome_value,
            "final_status": status_mapping,
        },
        "error": None,
    }


def _scenario_diagnostics(
    *,
    factory: FirstAttemptRecoveryFactory,
    notifier: BoundedRecordingNotifier,
    runtime: GStreamerRecorderRuntime,
    ownership: CameraOwnership,
    daemon_task: asyncio.Task[object],
) -> dict[str, object]:
    backends: list[dict[str, object]] = []
    for index, backend in enumerate(factory.backends[:4]):
        try:
            counters: object = asdict(backend.frame_counters())
        except Exception as error:
            counters = {"error": _bounded_error(error)}
        backends.append(
            {
                "index": index,
                "type": type(backend).__name__[:128],
                "counters": counters,
            }
        )
    try:
        runtime_snapshot: object = runtime.runtime_snapshot()
    except Exception as error:
        runtime_snapshot = {"error": _bounded_error(error)}
    return {
        "daemon_task": _daemon_task_diagnostic(daemon_task),
        "notifier_events": list(notifier.events),
        "notified_states": list(_notified_states(notifier.events)),
        "factory": {
            "backend_count": len(factory.backends),
            "output_sequences": [
                output.start_index for output in factory.outputs[:4]
            ],
            "wrapper": (
                None
                if factory.wrapper is None
                else factory.wrapper.diagnostic_snapshot()
            ),
            "backends": backends,
        },
        "runtime_snapshot": runtime_snapshot,
        "camera_owner": ownership.owner,
    }


def _build_finalizer(
    recording_root: Path,
    expected_device_id: str,
) -> RecorderClipFinalizer:
    if recording_root != RECORDING_ROOT:
        raise HarnessError("runtime requested a non-production recording root")
    return RecorderClipFinalizer(
        catalog=ClipCatalog(CATALOG_PATH),
        filesystem=DurableRootedFinalizationFilesystem(
            recording_root,
            expected_device_id=expected_device_id,
        ),
        monotonic_ns=time.monotonic_ns,
    )


def _absolute_media_runner(
    argv: Sequence[str],
    *,
    timeout_seconds: float,
    max_output_bytes: int,
) -> CommandResult:
    if not argv or argv[0] not in {"ffprobe", "ffmpeg"}:
        raise HarnessError("media validator requested an unexpected executable")
    executable = Path("/usr/bin") / argv[0]
    if not executable.is_file():
        raise HarnessError(f"{executable} is unavailable")
    return run_fixed_argv(
        (executable.as_posix(), *argv[1:]),
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
    )


def _bounded_directory_names(directory: Path) -> tuple[str, ...]:
    names: list[str] = []
    with os.scandir(directory) as entries:
        for entry in entries:
            if len(names) == MAX_DIRECTORY_ENTRIES:
                raise HarnessError(f"{directory} exceeded its scan bound")
            names.append(entry.name)
    return tuple(names)


def _verify_pair(
    *,
    boot_id: UUID,
    boot_short_id: str,
    sequence: int,
    recording_device: int,
) -> dict[str, object]:
    pair = finalized_unsynced_clip_pair(boot_id=boot_short_id, sequence=sequence)
    video_path = RECORDING_ROOT / "clips" / pair.video_name
    sidecar_path = RECORDING_ROOT / "clips" / pair.metadata_name
    for member in (video_path, sidecar_path):
        info = os.lstat(member)
        if not stat.S_ISREG(info.st_mode) or info.st_dev != recording_device:
            raise HarnessError("finalized pair member is not a regular exact-volume file")
    sidecar_payload = _bounded_regular_bytes(sidecar_path, MAX_SIDECAR_BYTES)
    sidecar = parse_sidecar_bytes(sidecar_payload)
    if (
        sidecar.boot_id != boot_id
        or sidecar.sequence != sequence
        or sidecar.video_file != pair.video_name
        or sidecar.metadata_file != pair.metadata_name
    ):
        raise HarnessError("canonical sidecar identity differs from the generated pair")
    catalog = ClipCatalog(CATALOG_PATH)
    try:
        clip = catalog.get_clip(sidecar.clip_id)
    finally:
        catalog.close()
    if (
        clip.lifecycle is not ClipLifecycle.FINALIZED
        or not clip.pair_reconciled
        or not clip.managed
        or clip.video_path != PurePosixPath("clips", pair.video_name).as_posix()
        or clip.sidecar_path != PurePosixPath("clips", pair.metadata_name).as_posix()
    ):
        raise HarnessError("catalog row is not a reconciled managed FINALIZED pair")
    duration_s = (sidecar.end_monotonic_ns - sidecar.start_monotonic_ns) / 1_000_000_000
    validation = probe_media_file(
        video_path,
        runner=_absolute_media_runner,
        thresholds=MediaThresholds(
            nominal_duration_seconds=duration_s,
            duration_tolerance_seconds=2.0,
            target_video_bitrate_bps=sidecar.video.target_bitrate_bps,
            bitrate_tolerance_fraction=0.99,
            frame_rate=sidecar.video.fps_nominal,
        ),
        timeline=TimelineEvidence(
            sidecar.start_monotonic_ns,
            sidecar.end_monotonic_ns,
        ),
        timeout_seconds=45.0,
        max_output_bytes=8 * 1024 * 1024,
    )
    observed = {check.code: check.outcome for check in validation.checks}
    if any(observed.get(code) is not Outcome.PASS for code in REQUIRED_MEDIA_CHECKS):
        raise HarnessError("finalized MP4 failed independent decode/IDR validation")
    provisional = provisional_clip_pair(boot_id=boot_short_id, sequence=sequence)
    pending_names = set(_bounded_directory_names(RECORDING_ROOT / "pending"))
    if provisional.video_name in pending_names or provisional.metadata_name in pending_names:
        raise HarnessError("generated sequence retained a pending pair member")
    return {
        "sequence": sequence,
        "clip_id": str(sidecar.clip_id),
        "video": video_path.as_posix(),
        "sidecar": sidecar_path.as_posix(),
        "duration_seconds": duration_s,
        "catalog_lifecycle": clip.lifecycle.value,
        "catalog_pair_reconciled": clip.pair_reconciled,
        "canonical_sidecar_sha256": hashlib.sha256(sidecar_payload).hexdigest(),
        "media": validation.to_dict(),
        "required_media_checks": sorted(REQUIRED_MEDIA_CHECKS),
        "required_media_checks_passed": True,
        "generated_pending_members_absent": True,
    }


def _validate_storage_snapshot(snapshot: Mapping[str, object]) -> None:
    storage = snapshot.get("storage_preflight")
    if not isinstance(storage, Mapping):
        raise HarnessError("runtime storage snapshot is absent")
    mount = storage.get("mount")
    if (
        storage.get("state") != "READY"
        or storage.get("ready") is not True
        or not isinstance(mount, Mapping)
        or mount.get("target") != RECORDING_ROOT.as_posix()
        or mount.get("filesystem") != "exfat"
        or mount.get("label") != "DASHCAM"
        or mount.get("read_write") is not True
    ):
        raise HarnessError("runtime storage snapshot is not exact READY exFAT evidence")


async def _run_scenario(
    *,
    manifest_hash: str,
    manifest_entries: Mapping[str, str],
    proof: Mapping[str, object],
    proof_hash: str,
    release: Mapping[str, str],
    identity: Mapping[str, object],
    boot_id: UUID,
) -> dict[str, object]:
    started_ns = time.monotonic_ns()
    deadline = time.monotonic() + ACTIVE_SCENARIO_DEADLINE_SECONDS
    config_before = _sha256_file(CONFIG_PATH, maximum=1024 * 1024)
    identity_before = _sha256_file(IDENTITY_PATH, maximum=1024 * 1024)
    config = load_config(CONFIG_PATH)
    if Path(config.storage.recording_root) != RECORDING_ROOT:
        raise HarnessError("installed config does not select the exact recording root")
    root_info = os.lstat(RECORDING_ROOT)
    if not stat.S_ISDIR(root_info.st_mode):
        raise HarnessError("recording root is not a real directory")

    factory = FirstAttemptRecoveryFactory()
    ownership = CameraOwnership()
    notifier = BoundedRecordingNotifier()
    runtime = GStreamerRecorderRuntime(
        config_path=CONFIG_PATH,
        identity_path=IDENTITY_PATH,
        backend_factory=factory,
        finalizer_factory=_build_finalizer,
        ownership=ownership,
    )
    daemon = RecorderDaemon(
        config_path=CONFIG_PATH,
        runtime=runtime,
        storage_gate=runtime,
        notifier=notifier,
        limits=DaemonLimits(watchdog_interval_s=10.0),
    )
    daemon_task = asyncio.create_task(daemon.run(), name="m6-recovery-daemon")
    def diagnostic_provider() -> Mapping[str, object]:
        return _scenario_diagnostics(
            factory=factory,
            notifier=notifier,
            runtime=runtime,
            ownership=ownership,
            daemon_task=cast(asyncio.Task[object], daemon_task),
        )
    try:
        await _wait_until(
            lambda: (
                factory.wrapper is not None
                and factory.wrapper.injected
                and len(factory.backends) >= 2
                and _has_recovery_order(_notified_states(notifier.events))
            ),
            deadline=deadline,
            detail="replacement did not reach RECOVERED after the reviewed injection",
            terminal=daemon_task.done,
            diagnostics=diagnostic_provider,
        )
        assert factory.wrapper is not None
        replacement = factory.backends[1]
        recovery_baseline = _backend_encoded(replacement)
        await _wait_until(
            lambda: _backend_encoded(replacement)
            >= recovery_baseline + POST_RECOVERY_FRAMES,
            deadline=deadline,
            detail="replacement did not encode 150 new frames after RECOVERED",
            terminal=daemon_task.done,
            diagnostics=diagnostic_provider,
        )
        replacement_after = _backend_encoded(replacement)
        daemon.request_stop()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HarnessError("scenario exceeded its overall deadline before shutdown")
        result = await asyncio.wait_for(asyncio.shield(daemon_task), timeout=remaining)
    finally:
        if not daemon_task.done():
            daemon.request_stop()
            try:
                await asyncio.wait_for(asyncio.shield(daemon_task), timeout=24.0)
            except (TimeoutError, Exception):
                daemon_task.cancel()
                await asyncio.gather(daemon_task, return_exceptions=True)

    if result.outcome is not DaemonOutcome.STOPPED or not result.clean:
        raise HarnessError(f"daemon did not stop cleanly: {result.outcome.value}")
    wrapper = factory.wrapper
    if (
        wrapper is None
        or not wrapper.injected
        or not wrapper.inner_run_joined
        or not wrapper.stop_delegated
        or wrapper.elapsed_at_injection_s is None
        or wrapper.elapsed_at_injection_s < INJECTION_SECONDS
        or wrapper.encoded_delta_at_injection is None
        or wrapper.encoded_delta_at_injection < INJECTION_FRAMES
    ):
        raise HarnessError("injection wrapper did not preserve its measured cleanup contract")
    if len(factory.outputs) != 2 or len(factory.backends) != 2:
        raise HarnessError("runtime did not create exactly one unmodified replacement backend")
    if ownership.owner is not None:
        raise HarnessError("camera process ownership was not released")
    snapshot = runtime.runtime_snapshot()
    _validate_storage_snapshot(snapshot)
    if snapshot.get("pipeline_restart_count") != 1:
        raise HarnessError("runtime restart count is not exactly one")
    states = _notified_states(notifier.events)
    if not _has_recovery_order(states):
        raise HarnessError("FAULTED/STARTING/RECORDING recovery status order was not observed")
    if config_before != _sha256_file(CONFIG_PATH, maximum=1024 * 1024):
        raise HarnessError("installed config changed during validation")
    if identity_before != _sha256_file(IDENTITY_PATH, maximum=1024 * 1024):
        raise HarnessError("storage identity changed during validation")

    sequences = tuple(output.start_index for output in factory.outputs)
    if len(set(sequences)) != 2:
        raise HarnessError("replacement reused the failed attempt sequence")
    pairs = [
        _verify_pair(
            boot_id=boot_id,
            boot_short_id=boot_id.hex[:12],
            sequence=sequence,
            recording_device=root_info.st_dev,
        )
        for sequence in sequences
    ]
    catalog = ClipCatalog(CATALOG_PATH)
    try:
        pending_intents = catalog.list_pending_intents(limit=1)
    finally:
        catalog.close()
    if pending_intents:
        raise HarnessError("catalog retains a pending operation intent")
    ended_ns = time.monotonic_ns()
    return {
        "schema_version": 1,
        "phase": "milestone6_exact_pi_recovery",
        "passed": True,
        "manifest": {"sha256": manifest_hash, "members": dict(manifest_entries)},
        "installed_release": dict(release),
        "inactive_unit_proof": {
            "sha256": proof_hash,
            "document": dict(proof),
        },
        "execution_identity": dict(identity),
        "immutable_inputs": {
            "config": CONFIG_PATH.as_posix(),
            "config_sha256": config_before,
            "storage_identity": IDENTITY_PATH.as_posix(),
            "storage_identity_sha256": identity_before,
            "catalog": CATALOG_PATH.as_posix(),
            "recording_root": RECORDING_ROOT.as_posix(),
            "recording_st_dev": root_info.st_dev,
        },
        "timing": {
            "started_monotonic_ns": started_ns,
            "ended_monotonic_ns": ended_ns,
            "elapsed_seconds": (ended_ns - started_ns) / 1_000_000_000,
            "overall_limit_seconds": SCENARIO_TIMEOUT_SECONDS,
        },
        "injection": {
            "count": 1,
            "minimum_seconds": INJECTION_SECONDS,
            "observed_seconds": wrapper.elapsed_at_injection_s,
            "minimum_encoded_frames": INJECTION_FRAMES,
            "observed_encoded_frames": wrapper.encoded_delta_at_injection,
            "first_fragment_sequence": (
                None if wrapper.opened is None else wrapper.opened.sequence
            ),
            "inner_run_joined": wrapper.inner_run_joined,
            "cleanup_stop_delegated_for_eos_and_null": wrapper.stop_delegated,
        },
        "recovery": {
            "replacement_backend_unmodified": not isinstance(
                factory.backends[1], InjectedRecoveryBackend
            ),
            "replacement_encoded_baseline_at_recovered": recovery_baseline,
            "replacement_encoded_after_wait": replacement_after,
            "replacement_new_frames": replacement_after - recovery_baseline,
            "pipeline_restart_count": snapshot["pipeline_restart_count"],
            "notifier_events": notifier.events,
            "notified_states": list(states),
            "required_order_observed": True,
        },
        "shutdown": {
            "daemon_outcome": result.outcome.value,
            "final_status": result.final_status.as_dict(),
            "camera_owner_after_shutdown": ownership.owner,
        },
        "runtime_snapshot": snapshot,
        "finalized_pairs": pairs,
        "catalog_pending_intents": 0,
    }


def _write_exclusive_json(path: Path, document: Mapping[str, object]) -> None:
    parent = path.parent.resolve(strict=True)
    recording_root = RECORDING_ROOT.resolve(strict=True)
    if parent == recording_root or recording_root in parent.parents:
        raise HarnessError("evidence output must be on rootfs, not the recording volume")
    if not path.name or path.name in {".", ".."}:
        raise HarnessError("output filename is invalid")
    destination = parent / path.name
    payload = (
        json.dumps(
            document,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    if len(payload) > MAX_JSON_BYTES:
        raise HarnessError("evidence JSON exceeds its byte bound")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(destination, flags, 0o600)
    try:
        position = 0
        while position < len(payload):
            written = os.write(descriptor, payload[position:])
            if written <= 0:
                raise OSError("short evidence write")
            position += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if os.name != "nt":
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="milestone6-recovery")
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--inactive-proof", type=Path, required=True)
    parser.add_argument("--expected-inactive-proof-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        entries = verify_manifest(arguments.expected_manifest_sha256)
    except (HarnessError, OSError, ValueError) as error:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "phase": "milestone6_exact_pi_recovery",
                    "passed": False,
                    "error": f"{type(error).__name__}: {error}"[:512],
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            flush=True,
        )
        return 2

    try:
        release = verify_installed_release()
        boot_id_text = _read_boot_id()
        proof = verify_inactive_proof(
            arguments.inactive_proof,
            arguments.expected_inactive_proof_sha256,
            boot_id=boot_id_text,
            monotonic_ns=time.monotonic_ns(),
        )
        identity = verify_live_identity()
        report = asyncio.run(
            asyncio.wait_for(
                _run_scenario(
                    manifest_hash=arguments.expected_manifest_sha256,
                    manifest_entries=entries,
                    proof=proof,
                    proof_hash=arguments.expected_inactive_proof_sha256,
                    release=release,
                    identity=identity,
                    boot_id=UUID(boot_id_text),
                ),
                timeout=SCENARIO_TIMEOUT_SECONDS,
            )
        )
        exit_code = 0
    except (HarnessError, OSError, ValueError, RuntimeError, TimeoutError) as error:
        report = {
            "schema_version": 1,
            "phase": "milestone6_exact_pi_recovery",
            "passed": False,
            "error": f"{type(error).__name__}: {error}"[:512],
            "manifest": {
                "sha256": arguments.expected_manifest_sha256,
                "members": entries,
            },
        }
        if isinstance(error, ScenarioFailure):
            report["diagnostics"] = error.diagnostics
        exit_code = 1
    try:
        _write_exclusive_json(arguments.output, report)
        output_hash = _sha256_file(arguments.output, maximum=MAX_JSON_BYTES)
    except (HarnessError, OSError, ValueError) as error:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "phase": "milestone6_exact_pi_recovery",
                    "passed": False,
                    "error": f"{type(error).__name__}: {error}"[:512],
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            flush=True,
        )
        return 2
    print(
        json.dumps(
            {
                "schema_version": 1,
                "phase": "milestone6_exact_pi_recovery",
                "passed": report["passed"],
                "output": arguments.output.as_posix(),
                "output_sha256": output_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        flush=True,
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
