#!/usr/bin/env python3
"""Hash-closed, non-mutating splitmux dynamic-audio refusal probe."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import stat
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, cast

import dashcam
from dashcam.audio.alsa import parse_alsa_selector
from dashcam.audio.linux import AudioDiscoveryStatus, discover_capture_device
from dashcam.config import load_config
from dashcam.diagnostics.media import run_fixed_argv
from dashcam.recorder.gstreamer import (
    AudioCapturePlan,
    build_audio_ingress_description,
    build_audio_pipeline_description,
)

RECORDING_ROOT: Final = Path("/srv/dashcam")
QUARANTINE_ROOT: Final = RECORDING_ROOT / "quarantine"
CONFIG_PATH: Final = Path("/etc/dashcam/config.toml")
SYSTEMCTL: Final = "/usr/bin/systemctl"
GST_INSPECT: Final = "/usr/bin/gst-inspect-1.0"
MANIFEST_MEMBERS: Final = ("README.md", "run.py")
SHA256_RE: Final = re.compile(r"[0-9a-f]{64}")
ELEMENT_NAME_RE: Final = re.compile(r"(?:^|\s)name=([A-Za-z0-9_-]{1,64})(?:\s|$)")
RUN_NAME_RE: Final = re.compile(r"m7-hotplug-[a-z0-9]{8,32}")
MAX_MANIFEST_BYTES: Final = 4096
MAX_RESULT_BYTES: Final = 512 * 1024
MAX_INSPECT_BYTES: Final = 512 * 1024

BLOCKERS: Final = (
    "NO_PUBLIC_PRE_SWITCH_TRACK_BARRIER",
    "NO_PUBLIC_REQUEST_PAD_DRAIN_COMPLETION",
    "FRAGMENT_OPENED_IS_POST_SWITCH_NOTIFICATION",
    "ASYNC_FINALIZE_CONTEXT_RACE_UNRESOLVED",
    "FIRST_RESTORED_AAC_RUNNING_TIME_BARRIER_UNPROVEN",
)
EXPECTED_ACTION_SIGNALS: Final = frozenset(
    {"split-now", "split-after", "split-at-running-time"}
)
UNSAFE_BARRIER_NAMES: Final = frozenset(
    {
        "drain-request-pad",
        "prepare-fragment-switch",
        "fragment-switch-ready",
        "new-mux-ready",
        "track-switch-barrier",
    }
)


class HarnessError(RuntimeError):
    """The reviewed non-mutating probe contract could not be established."""


def _bounded_detail(value: object) -> str:
    text = " ".join(str(value).replace("\0", " ").splitlines())
    return "".join(character if character.isprintable() else " " for character in text)[:512]


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
            raise HarnessError(f"{path} is not a bounded regular file")
        payload = bytearray()
        while chunk := os.read(descriptor, min(65536, maximum + 1 - len(payload))):
            payload.extend(chunk)
            if len(payload) > maximum:
                raise HarnessError(f"{path} exceeded its read bound")
        return bytes(payload)
    finally:
        os.close(descriptor)


def _sha256_file(path: Path, *, maximum: int) -> str:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    digest = hashlib.sha256()
    total = 0
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise HarnessError(f"{path} is not a regular file")
        while chunk := os.read(descriptor, 1024 * 1024):
            total += len(chunk)
            if total > maximum:
                raise HarnessError(f"{path} exceeded its hash bound")
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def verify_manifest(expected_sha256: str, directory: Path | None = None) -> dict[str, str]:
    if SHA256_RE.fullmatch(expected_sha256) is None:
        raise HarnessError("expected manifest SHA-256 is not canonical")
    root = (directory or Path(__file__).resolve().parent).resolve(strict=True)
    manifest = root / "SHA256SUMS"
    if _sha256_file(manifest, maximum=MAX_MANIFEST_BYTES) != expected_sha256:
        raise HarnessError("reviewed manifest hash differs from the supplied hash")
    entries: dict[str, str] = {}
    for line in _bounded_regular_bytes(manifest, MAX_MANIFEST_BYTES).decode("ascii").splitlines():
        digest, separator, name = line.partition("  ")
        if (
            not separator
            or SHA256_RE.fullmatch(digest) is None
            or name in entries
            or name not in MANIFEST_MEMBERS
            or Path(name).name != name
        ):
            raise HarnessError("manifest member set is not closed")
        entries[name] = digest
    if tuple(sorted(entries)) != MANIFEST_MEMBERS:
        raise HarnessError("manifest omits a required member")
    for name, digest in entries.items():
        if _sha256_file(root / name, maximum=2 * 1024 * 1024) != digest:
            raise HarnessError(f"manifest member {name} failed verification")
    return entries


def _write_atomic_exclusive_json(path: Path, value: Mapping[str, object]) -> None:
    if not path.is_absolute():
        raise HarnessError("evidence output path must be absolute")
    try:
        path.resolve(strict=False).relative_to(RECORDING_ROOT)
    except ValueError:
        pass
    else:
        raise HarnessError("evidence output must be outside /srv/dashcam")
    path.parent.mkdir(parents=True, exist_ok=True)
    parent = path.parent.resolve(strict=True)
    if path.parent != parent or path.exists() or path.is_symlink():
        raise HarnessError("evidence output must be one new direct regular file")
    encoded = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(encoded) > MAX_RESULT_BYTES:
        raise HarnessError("evidence JSON exceeds its bound")
    descriptor, temporary = tempfile.mkstemp(prefix=".m7-hotplug-refusal-", dir=parent)
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
                parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
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


def validate_absent_quarantine_target(
    selected: Path,
    *,
    recording_root: Path = RECORDING_ROOT,
    quarantine_root: Path = QUARANTINE_ROOT,
) -> dict[str, object]:
    """Validate the proposed target lexically and observationally without creating it."""

    if (
        not selected.is_absolute()
        or selected.parent != quarantine_root
        or RUN_NAME_RE.fullmatch(selected.name) is None
    ):
        raise HarnessError("proposed media target is not one safe quarantine child")
    root = recording_root.resolve(strict=True)
    root_info = os.lstat(root)
    if root != recording_root or not stat.S_ISDIR(root_info.st_mode):
        raise HarnessError("recording root identity differs")
    if selected.exists() or selected.is_symlink():
        raise HarnessError("proposed quarantine target already exists")
    quarantine_state = "absent"
    if quarantine_root.exists() or quarantine_root.is_symlink():
        quarantine = quarantine_root.resolve(strict=True)
        info = os.lstat(quarantine)
        if (
            quarantine != quarantine_root
            or not stat.S_ISDIR(info.st_mode)
            or info.st_dev != root_info.st_dev
        ):
            raise HarnessError("quarantine root left the exact recording device")
        quarantine_state = "existing_exact_directory"
    return {
        "recording_root": str(recording_root),
        "proposed_directory": str(selected),
        "proposed_directory_absent": True,
        "quarantine_state": quarantine_state,
        "created": False,
    }


def _read_only_unit_inactive() -> dict[str, object]:
    observed: dict[str, str] = {}
    for property_name in ("ActiveState", "SubState", "MainPID"):
        result = run_fixed_argv(
            (
                SYSTEMCTL,
                "show",
                "--no-pager",
                f"--property={property_name}",
                "--value",
                "dashcamd.service",
            ),
            timeout_seconds=5.0,
            max_output_bytes=1024,
        )
        if result.returncode != 0 or result.timed_out or result.output_truncated:
            raise HarnessError("read-only dashcamd state query failed")
        try:
            observed[property_name] = result.stdout.decode("ascii").strip()
        except UnicodeDecodeError as error:
            raise HarnessError("dashcamd state query was not ASCII") from error
    if observed != {"ActiveState": "inactive", "SubState": "dead", "MainPID": "0"}:
        raise HarnessError("dashcamd.service is not exactly inactive/dead with MainPID 0")
    return {"active_state": "inactive", "sub_state": "dead", "main_pid": 0}


def _release_identity() -> dict[str, str]:
    prefix = Path(sys.prefix).resolve(strict=True)
    package_path = Path(dashcam.__file__).resolve(strict=True)
    parts = prefix.as_posix().split("/")
    if (
        len(parts) < 6
        or parts[:4] != ["", "opt", "dashcam", "releases"]
        or parts[-1] != "venv"
        or not package_path.is_relative_to(prefix)
    ):
        raise HarnessError("interpreter and imported dashcam package are not one release")
    release = parts[4]
    if re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._-]{0,127}", release) is None:
        raise HarnessError("installed release identity is unsafe")
    return {"release": release, "venv": str(prefix), "package": str(package_path)}


def analyze_gst_inspect(payload: bytes) -> dict[str, object]:
    try:
        text = payload.decode("utf-8", "strict")
    except UnicodeDecodeError as error:
        raise HarnessError("gst-inspect output is not UTF-8") from error
    if not text or len(payload) > MAX_INSPECT_BYTES:
        raise HarnessError("gst-inspect output is empty or oversized")
    lowered = text.casefold()
    expected_tokens = {
        "audio_%u": "audio_%u" in text,
        "split-now": "split-now" in lowered,
        "split-after": "split-after" in lowered,
        "split-at-running-time": "split-at-running-time" in lowered,
        "async-finalize": "async-finalize" in lowered,
    }
    if not all(expected_tokens.values()):
        raise HarnessError("splitmuxsink public API differs from the reviewed shape")
    barrier_tokens = {
        name: name in lowered
        for name in sorted(UNSAFE_BARRIER_NAMES)
    }
    return {
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
        "expected_tokens": expected_tokens,
        "candidate_barrier_tokens": barrier_tokens,
        "candidate_barrier_present": any(barrier_tokens.values()),
    }


def _string_names(values: object, name: str) -> list[str]:
    if not isinstance(values, Sequence) or isinstance(values, str | bytes):
        raise HarnessError(f"{name} introspection returned an invalid sequence")
    result = sorted({str(value) for value in values})
    if len(result) > 256 or any(not value or len(value) > 128 for value in result):
        raise HarnessError(f"{name} introspection exceeded its bound")
    return result


def probe_splitmux_public_api() -> dict[str, object]:
    """Inspect public target APIs only; never construct a media pipeline."""

    try:
        gi = importlib.import_module("gi")
        gi.require_version("Gst", "1.0")
        gst = importlib.import_module("gi.repository.Gst")
        gobject = importlib.import_module("gi.repository.GObject")
        gst.init(None)
    except (ImportError, AttributeError, TypeError, ValueError) as error:
        raise HarnessError("PyGObject GStreamer introspection is unavailable") from error

    factory_type = getattr(gst, "ElementFactory", None)
    find = None if factory_type is None else getattr(factory_type, "find", None)
    if not callable(find):
        raise HarnessError("GStreamer lacks ElementFactory.find")
    factory = find("splitmuxsink")
    if factory is None:
        raise HarnessError("splitmuxsink factory is unavailable")
    create = getattr(factory, "create", None)
    templates_reader = getattr(factory, "get_static_pad_templates", None)
    if not callable(create) or not callable(templates_reader):
        raise HarnessError("splitmuxsink factory introspection is incomplete")
    element = create(None)
    if element is None:
        raise HarnessError("splitmuxsink factory could not create an inert element")

    pad_templates: list[dict[str, str]] = []
    for template in cast(Sequence[object], templates_reader()):
        name_template = str(getattr(template, "name_template", ""))
        direction = str(getattr(template, "direction", ""))
        presence = str(getattr(template, "presence", ""))
        pad_templates.append(
            {
                "name_template": name_template,
                "direction": direction,
                "presence": presence,
            }
        )
    if len(pad_templates) > 32 or not any(
        template["name_template"] == "audio_%u" for template in pad_templates
    ):
        raise HarnessError("splitmuxsink omits the expected audio_%u request template")

    list_properties = getattr(element, "list_properties", None)
    if not callable(list_properties):
        raise HarnessError("splitmuxsink property introspection is unavailable")
    properties = _string_names(
        [getattr(item, "name", "") for item in cast(Sequence[object], list_properties())],
        "property",
    )
    signal_list_names = getattr(gobject, "signal_list_names", None)
    gtype = getattr(element, "__gtype__", None)
    if not callable(signal_list_names) or gtype is None:
        raise HarnessError("splitmuxsink signal introspection is unavailable")
    signals = _string_names(signal_list_names(gtype), "signal")
    if not set(signals) >= EXPECTED_ACTION_SIGNALS or "async-finalize" not in properties:
        raise HarnessError("splitmuxsink public action/property shape differs")

    inspect_result = run_fixed_argv(
        (GST_INSPECT, "splitmuxsink"),
        timeout_seconds=10.0,
        max_output_bytes=MAX_INSPECT_BYTES,
    )
    if (
        inspect_result.returncode != 0
        or inspect_result.timed_out
        or inspect_result.output_truncated
    ):
        raise HarnessError("gst-inspect splitmuxsink did not complete")
    inspect = analyze_gst_inspect(inspect_result.stdout)
    public_names = set(properties) | set(signals) | {
        template["name_template"] for template in pad_templates
    }
    candidate_barriers = sorted(public_names & UNSAFE_BARRIER_NAMES)
    if candidate_barriers or inspect["candidate_barrier_present"] is True:
        raise HarnessError(
            "an unreviewed candidate barrier appeared; do not infer that it is safe"
        )

    version_string = getattr(gst, "version_string", None)
    version = str(version_string()) if callable(version_string) else "unavailable"
    return {
        "gstreamer_version": version[:128],
        "pad_templates": pad_templates,
        "properties": properties,
        "signals": signals,
        "gst_inspect": inspect,
        "request_pad_api_present": callable(getattr(element, "request_pad_simple", None)),
        "release_request_pad_api_present": callable(
            getattr(element, "release_request_pad", None)
        ),
        "public_atomic_track_switch_barrier_present": False,
    }


def execute_refusal(output_directory: Path) -> dict[str, object]:
    release = _release_identity()
    unit = _read_only_unit_inactive()
    target = validate_absent_quarantine_target(output_directory)
    config = load_config(CONFIG_PATH)
    if not config.audio.enabled:
        raise HarnessError("production audio is disabled")
    selector = parse_alsa_selector(config.audio.device_match)
    discovery = discover_capture_device(selector)
    if discovery.status is not AudioDiscoveryStatus.MATCHED or discovery.device is None:
        raise HarnessError("configured microphone is not one exact stable match")
    plan = AudioCapturePlan.from_match(discovery.device, config.audio)
    # Production deliberately installs the replaceable ALSA/AAC ingress as a
    # separately owned bin; bind the refusal evidence to both graph components.
    description = "\n".join(
        (
            build_audio_pipeline_description(plan),
            build_audio_ingress_description(plan),
        )
    )
    element_names = sorted(set(ELEMENT_NAME_RE.findall(description)))
    if not {"audio_source", "audio_encoder", "audio_parser", "audio_record_queue"} <= set(
        element_names
    ):
        raise HarnessError("installed production audio graph lacks required named elements")
    api = probe_splitmux_public_api()
    return {
        "schema_version": 1,
        "passed": False,
        "outcome": "refused",
        "safe_to_enable_production_hotplug": False,
        "blockers": list(BLOCKERS),
        "release": release,
        "dashcamd": unit,
        "proposed_target": target,
        "production_graph": {
            "sha256": hashlib.sha256(description.encode()).hexdigest(),
            "bytes": len(description.encode()),
            "audio_element_names": element_names,
            "camera_opened": False,
            "encoder_opened": False,
            "pipeline_constructed": False,
        },
        "audio_match": {
            "vendor_id": plan.identity.vendor_id,
            "product_id": plan.identity.product_id,
            "product": plan.identity.product,
            "physical_path": plan.identity.physical_path,
            "endpoint": plan.endpoint,
        },
        "public_api_probe": api,
        "media": [],
        "mutations": {
            "recording_volume_writes": 0,
            "camera_opens": 0,
            "encoder_opens": 0,
            "request_pad_operations": 0,
            "service_operations": 0,
            "network_operations": 0,
        },
        "required_future_gate": {
            "transaction": (
                "final audio src block+IDLE; EOS drain observed and dropped; encoded-video "
                "input block; exact keyframe split plus old closure/new mux readiness; "
                "application-thread request-pad mutation outside switching; first restored "
                "AAC AU/current-running-time proof before video unblock"
            ),
            "acceptance": (
                "same pipeline/clock/camera/encoder identities, monotonic video counters, "
                "no bus warnings/errors, exact A/V then video-only then restored-A/V shapes, "
                "IDR hardware decode, continuity, and A/V skew below 100 ms"
            ),
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-manifest-sha256", required=True)
    subparsers = parser.add_subparsers(dest="phase", required=True)
    probe = subparsers.add_parser("probe-refusal")
    probe.add_argument("--output-directory", type=Path, required=True)
    probe.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    output = cast(Path, arguments.output)
    output_directory = cast(Path, arguments.output_directory)
    verify_manifest(cast(str, arguments.expected_manifest_sha256))
    started_ns = time.monotonic_ns()
    try:
        result = execute_refusal(output_directory)
    except BaseException as error:
        result = {
            "schema_version": 1,
            "passed": False,
            "outcome": "probe_failed",
            "safe_to_enable_production_hotplug": False,
            "error_type": type(error).__name__,
            "error": _bounded_detail(error),
            "proposed_directory": str(output_directory),
            "media": [],
        }
    result["started_monotonic_ns"] = started_ns
    result["ended_monotonic_ns"] = time.monotonic_ns()
    _write_atomic_exclusive_json(output, result)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
