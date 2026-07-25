#!/usr/bin/env python3
"""Independent fresh-process readback verifier for Bootstrap v1 regular images."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from dashcam.provisioning.bootstrap_image import (
    BootstrapImageRefused,
    SourceMetadata,
    decompress_pinned_source,
    default_command_runner,
    load_builder_requirements,
    mount_readonly_image,
    unmount_readonly,
    validate_new_retained_output,
    verify_builder_host,
    verify_mounted_readback,
    write_new_file,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        allow_abbrev=False,
        description="Read back a Bootstrap v1 raw image through fresh read-only FUSE mounts.",
    )
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--requirements", type=Path, required=True)
    parser.add_argument("--app-commit", required=True)
    parser.add_argument("--package-lock-sha256", required=True)
    parser.add_argument("--app-wheel-sha256", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    metadata = SourceMetadata(
        app_commit=args.app_commit,
        package_lock_sha256=args.package_lock_sha256,
        app_wheel_sha256=args.app_wheel_sha256,
    )
    mounted: tuple[Path, ...] = ()
    source_raw = args.work_root / "official-source.img"
    try:
        requirements = load_builder_requirements(args.requirements.read_bytes())
        verify_builder_host(
            requirements,
            runner=default_command_runner,
            platform_name=sys.platform,
            effective_uid=getattr(os, "geteuid", lambda: -1)(),
            actual_container_digest=os.environ.get(
                "DASHCAM_BUILDER_CONTAINER_DIGEST", ""
            ),
        )
        if args.work_root.exists() or args.work_root.is_symlink():
            raise FileExistsError(f"verifier work root exists: {args.work_root}")
        validate_new_retained_output(args.evidence, suffix=".verification.json")
        args.work_root.mkdir()
        decompress_pinned_source(args.source, source_raw)
        source_boot = args.work_root / "source-boot"
        built_boot = args.work_root / "built-boot"
        built_root = args.work_root / "built-root"
        source_mounts = mount_readonly_image(
            raw_image=source_raw,
            mounts=(("/dev/sda1", source_boot),),
            requirements=requirements,
            runner=default_command_runner,
        )
        mounted = source_mounts
        built_mounts = mount_readonly_image(
            raw_image=args.raw,
            mounts=(("/dev/sda1", built_boot), ("/dev/sda2", built_root)),
            requirements=requirements,
            runner=default_command_runner,
        )
        mounted = (*source_mounts, *built_mounts)
        evidence = verify_mounted_readback(
            raw_image=args.raw,
            source_boot=source_boot,
            built_boot=built_boot,
            built_root=built_root,
            metadata=metadata,
            builder_requirements=requirements,
        )
        write_new_file(args.evidence, evidence.canonical_bytes(), suffix=".verification.json")
        if not evidence.passed:
            print(evidence.canonical_bytes().decode(), file=sys.stderr, end="")
            return 2
        print(evidence.canonical_bytes().decode(), end="")
        return 0
    except (BootstrapImageRefused, FileExistsError, OSError) as exc:
        code = getattr(exc, "code", "io_error")
        value = code.value if hasattr(code, "value") else str(code)
        print(json.dumps({"verified": False, "code": value, "error": str(exc)}), file=sys.stderr)
        return 2
    finally:
        if mounted:
            unmount_readonly(
                mounted=mounted,
                requirements=requirements,
                runner=default_command_runner,
            )
        if source_raw.is_file() and not source_raw.is_symlink():
            source_raw.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
