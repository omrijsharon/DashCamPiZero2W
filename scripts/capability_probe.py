#!/usr/bin/env python3
"""Emit a bounded, read-only target capability report."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from dashcam.diagnostics.capabilities import (
    BoundedFileReader,
    SubprocessCommandRunner,
    collect_capability_report,
    write_report_exclusive,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect read-only Raspberry Pi capability evidence as schema-v1 JSON."
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="optional absolute path for a new .json file; existing files are never replaced",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Collect and emit the report; no target state is changed."""

    args = _parser().parse_args(argv)
    report = collect_capability_report(SubprocessCommandRunner(), BoundedFileReader())
    document = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    if args.output is not None:
        try:
            write_report_exclusive(args.output, document)
        except (OSError, ValueError) as error:
            print(f"capability probe: refusing output: {error}", file=sys.stderr)
            return 2
    sys.stdout.write(document)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
