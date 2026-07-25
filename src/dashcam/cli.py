"""Small diagnostic command-line entry points."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from dashcam.version import get_build_info


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Show dashcam build identity.")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Print build identity and return a process exit code."""

    args = _build_parser().parse_args(argv)
    build = get_build_info()

    if args.json:
        print(json.dumps(build.as_dict(), sort_keys=True, separators=(",", ":")))
    else:
        commit = build.git_commit or "unknown"
        print(f"dashcam {build.version} (build {build.build_id}, commit {commit})")
    return 0
