#!/usr/bin/env python3
"""Author a Bootstrap v1 build plan.

The script never flashes media and never invokes a large/privileged build.  The
checked recipe under ``deploy/bootstrap/image`` is executed deliberately on a
reviewed Linux builder after this local contract and its readback verifier pass.
Only ``execute_bootstrap_image.py`` may emit a release manifest, in the same
invocation that runs the independent verifier.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from dashcam.provisioning.bootstrap_image import (
    BootstrapImageRefused,
    BuildPaths,
    SourceMetadata,
    author_build_plan,
    verify_pinned_source,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        allow_abbrev=False,
        description="Author a regular-file-only DashCam Bootstrap v1 image plan.",
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--compressed", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--app-commit", required=True)
    parser.add_argument("--package-lock-sha256", required=True)
    parser.add_argument("--app-wheel-sha256", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    paths = BuildPaths(
        source_archive=args.source,
        work_root=args.work_root,
        raw_image=args.raw,
        compressed_image=args.compressed,
        imager_manifest=args.manifest,
    )
    metadata = SourceMetadata(
        app_commit=args.app_commit,
        package_lock_sha256=args.package_lock_sha256,
        app_wheel_sha256=args.app_wheel_sha256,
    )
    try:
        source_verification = verify_pinned_source(paths.source_archive)
        plan = author_build_plan(paths, metadata, source_verification)
    except (BootstrapImageRefused, FileExistsError, OSError) as exc:
        code = getattr(exc, "code", "io_error")
        value = code.value if hasattr(code, "value") else str(code)
        print(json.dumps({"planned": False, "code": value, "error": str(exc)}), file=sys.stderr)
        return 2
    print(plan.canonical_bytes().decode(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
