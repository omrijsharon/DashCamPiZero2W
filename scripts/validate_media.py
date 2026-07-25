#!/usr/bin/env python3
"""Validate explicit media files with bounded ffprobe and decoder runs."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from dashcam.diagnostics.media import (
    MAX_PROBE_JSON_BYTES,
    MediaThresholds,
    Outcome,
    TimelineEvidence,
    probe_media_file,
    validate_boundaries,
)


def _timeline_manifest(path: Path | None) -> dict[str, TimelineEvidence]:
    if path is None:
        return {}
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError("timeline manifest must be a regular file")
    with resolved.open("rb") as handle:
        raw = handle.read(MAX_PROBE_JSON_BYTES + 1)
    if len(raw) > MAX_PROBE_JSON_BYTES:
        raise ValueError("timeline manifest is oversized")
    document = json.loads(raw)
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("timeline manifest must be a schema_version 1 object")
    clips = document.get("clips")
    if not isinstance(clips, list) or len(clips) > 10_000:
        raise ValueError("timeline clips must be a bounded list")
    result: dict[str, TimelineEvidence] = {}
    for item in clips:
        if not isinstance(item, dict) or set(item) != {
            "path",
            "start_monotonic_ns",
            "end_monotonic_ns",
        }:
            raise ValueError("timeline clip entries have an invalid shape")
        clip_path = str(Path(str(item["path"])).resolve(strict=False))
        if clip_path in result:
            raise ValueError(f"duplicate timeline entry: {clip_path}")
        result[clip_path] = TimelineEvidence(
            int(item["start_monotonic_ns"]), int(item["end_monotonic_ns"])
        )
    return result


def _write_json(path: Path, value: dict[str, Any], *, overwrite: bool) -> None:
    resolved = path.resolve(strict=False)
    if resolved.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite output: {resolved}")
    if not resolved.parent.is_dir():
        raise ValueError("output parent directory does not exist")
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{resolved.name}.", suffix=".tmp", dir=resolved.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, resolved)
    finally:
        temporary.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("media", type=Path, nargs="+", help="explicit media file(s)")
    parser.add_argument("--output", type=Path, required=True, help="JSON report path")
    parser.add_argument(
        "--timeline",
        type=Path,
        help="optional schema-version 1 monotonic timeline manifest",
    )
    parser.add_argument("--timeout", type=float, default=120.0, help="per-command seconds")
    parser.add_argument("--max-output-bytes", type=int, default=8 * 1024 * 1024)
    parser.add_argument("--target-bitrate", type=int, default=8_000_000)
    parser.add_argument("--bitrate-tolerance", type=float, default=0.25)
    parser.add_argument("--frame-rate", type=float, default=30.0)
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--duration-tolerance", type=float, default=1.0)
    parser.add_argument("--maximum-av-skew", type=float, default=0.100)
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        timeline = _timeline_manifest(args.timeline)
        thresholds = MediaThresholds(
            nominal_duration_seconds=args.duration,
            duration_tolerance_seconds=args.duration_tolerance,
            target_video_bitrate_bps=args.target_bitrate,
            bitrate_tolerance_fraction=args.bitrate_tolerance,
            maximum_av_skew_seconds=args.maximum_av_skew,
            frame_rate=args.frame_rate,
        )
        validations = tuple(
            probe_media_file(
                path,
                thresholds=thresholds,
                timeline=timeline.get(str(path.resolve(strict=True))),
                timeout_seconds=args.timeout,
                max_output_bytes=args.max_output_bytes,
            )
            for path in args.media
        )
        boundaries = validate_boundaries(validations, frame_rate=args.frame_rate)
        outcomes = [validation.overall for validation in validations] + [
            boundary.outcome for boundary in boundaries
        ]
        overall = (
            Outcome.FAIL
            if Outcome.FAIL in outcomes
            else (Outcome.INDETERMINATE if Outcome.INDETERMINATE in outcomes else Outcome.PASS)
        )
        report = {
            "schema_version": 1,
            "overall": overall.value,
            "validations": [validation.to_dict() for validation in validations],
            "boundaries": [boundary.to_dict() for boundary in boundaries],
        }
        _write_json(args.output, report, overwrite=args.overwrite)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"media validation failed: {error}", file=sys.stderr)
        return 2
    return 0 if overall is Outcome.PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())
