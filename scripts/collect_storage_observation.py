#!/usr/bin/env python3
"""Collect a bounded, read-only partition observation for one Linux device."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from dashcam.provisioning.collector import StorageCollectorError, collect_storage_observation


def _write_exclusive(path: Path, content: str) -> None:
    if path.suffix.casefold() != ".json" or not path.is_absolute():
        raise ValueError("output must be an absolute new .json path")
    resolved = path.resolve(strict=False)
    forbidden = tuple(Path(root) for root in ("/dev", "/proc", "/sys", "/run"))
    if os.name == "posix" and any(
        resolved == root or root in resolved.parents for root in forbidden
    ):
        raise ValueError("output path may not be on a special filesystem")
    if not resolved.parent.is_dir() or path.is_symlink() or resolved.exists():
        raise ValueError("output parent must exist and output must be a new non-symlink")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(resolved, flags, 0o600)
    try:
        os.write(descriptor, content.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Linux storage observation collector.")
    parser.add_argument("device", help="a canonical /dev disk or partition path")
    parser.add_argument("--output", type=Path, help="optional absolute new JSON output path")
    args = parser.parse_args(argv)
    try:
        observation = collect_storage_observation(args.device)
        document = json.dumps(observation, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
        if args.output is not None:
            _write_exclusive(args.output, document)
    except (OSError, StorageCollectorError, ValueError) as exc:
        print(f"storage collector: refusing observation: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(document)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
