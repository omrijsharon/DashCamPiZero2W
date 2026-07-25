#!/usr/bin/env python3
"""Verify a previously captured layout observation without touching devices."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import cast

from dashcam.provisioning.layout import (
    LayoutError,
    load_layout_toml,
    observation_from_mapping,
    verify_layout,
)

MAX_OBSERVATION_BYTES = 256 * 1024


def _bounded_read(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        payload = stream.read(limit + 1)
    if len(payload) > limit:
        raise LayoutError(f"{path} exceeds {limit} bytes")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only verification of a captured partition observation."
    )
    parser.add_argument(
        "--layout",
        type=Path,
        default=Path("deploy/storage/layout-v1.toml"),
        help="declarative layout TOML",
    )
    parser.add_argument("--observation", type=Path, required=True, help="captured observation JSON")
    args = parser.parse_args(argv)
    try:
        spec = load_layout_toml(_bounded_read(args.layout, 64 * 1024))
        decoded = json.loads(_bounded_read(args.observation, MAX_OBSERVATION_BYTES))
        if not isinstance(decoded, dict):
            raise LayoutError("observation JSON must contain an object")
        observed = observation_from_mapping(cast(dict[str, object], decoded))
        report = verify_layout(spec, observed)
    except (OSError, json.JSONDecodeError, LayoutError) as exc:
        print(json.dumps({"accepted": False, "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    return 0 if report.accepted else 2


if __name__ == "__main__":
    raise SystemExit(main())
