#!/usr/bin/env python3
"""Analyze an explicit, bounded endurance sample file into a JSON report."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from dashcam.diagnostics.endurance import (
    EnduranceOutcome,
    EnduranceThresholds,
    analyze_samples,
    load_samples,
)


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
    parser.add_argument("--input", type=Path, required=True, help="versioned sample JSON")
    parser.add_argument("--output", type=Path, required=True, help="JSON report path")
    parser.add_argument("--maximum-samples", type=int, default=43_200)
    parser.add_argument("--maximum-temperature", type=float, default=80.0)
    parser.add_argument("--maximum-cpu", type=float, default=100.0)
    parser.add_argument("--maximum-rss-growth", type=int, default=32 * 1024 * 1024)
    parser.add_argument("--minimum-available-memory", type=int, default=32 * 1024 * 1024)
    parser.add_argument("--maximum-swap-used", type=int, default=0)
    parser.add_argument("--maximum-dropped-frame-increase", type=int, default=0)
    parser.add_argument("--maximum-restart-increase", type=int, default=0)
    parser.add_argument("--target-bitrate", type=int, default=8_000_000)
    parser.add_argument("--bitrate-tolerance", type=float, default=0.25)
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        samples = load_samples(args.input)
        thresholds = EnduranceThresholds(
            maximum_samples=args.maximum_samples,
            maximum_temperature_c=args.maximum_temperature,
            maximum_cpu_percent=args.maximum_cpu,
            maximum_rss_growth_bytes=args.maximum_rss_growth,
            minimum_available_memory_bytes=args.minimum_available_memory,
            maximum_swap_used_bytes=args.maximum_swap_used,
            maximum_dropped_frame_increase=args.maximum_dropped_frame_increase,
            maximum_restart_increase=args.maximum_restart_increase,
            target_bitrate_bps=args.target_bitrate,
            bitrate_tolerance_fraction=args.bitrate_tolerance,
        )
        report = analyze_samples(samples, thresholds)
        _write_json(args.output, report.to_dict(), overwrite=args.overwrite)
    except (OSError, ValueError) as error:
        print(f"endurance analysis failed: {error}", file=sys.stderr)
        return 2
    return 0 if report.outcome is EnduranceOutcome.PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())
