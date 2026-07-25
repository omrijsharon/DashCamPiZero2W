#!/usr/bin/env python3
"""Plan or safely probe the regular-file-only release-image executor."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from dashcam.provisioning.image_builder import (
    ImageBuildRefused,
    author_image_build_plan,
    load_source_manifest,
)
from dashcam.provisioning.image_executor import (
    ImageExecutionRefused,
    execute_file_image,
    probe_execution_dependencies,
    refusal_json,
    refuse_block_device_execution,
)
from dashcam.provisioning.initramfs_customizer import PI_ZERO_2_W_ARMV7_PROFILE

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        allow_abbrev=False,
        description=(
            "Verify the pinned Raspberry Pi OS archive and emit an argv-only plan, "
            "or invoke the separately gated regular-file-only executor."
        ),
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=REPOSITORY_ROOT / "deploy" / "image" / "source-manifest-v1.json",
    )
    parser.add_argument("--source", type=Path, required=True, help="exact downloaded .img.xz")
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="absolute, nonexistent .img path reserved for exclusive future creation",
    )
    parser.add_argument(
        "--payload",
        type=Path,
        default=REPOSITORY_ROOT / "deploy" / "image" / "payload",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--execute-file-image",
        action="store_true",
        help="execute only the fail-closed new-regular-.img path",
    )
    mode.add_argument(
        "--execute-authorized-exact-card-image",
        action="store_true",
        help=(
            "create the CID/size-bound trial image; requires the exact closed --authorization-file"
        ),
    )
    mode.add_argument(
        "--probe-file-executor",
        action="store_true",
        help="report exact local dependency/refusal facts without creating output",
    )
    parser.add_argument(
        "--authorization-file",
        type=Path,
        help="closed JSON authorization bound to the reviewed exact card CID and size",
    )
    parser.add_argument(
        "--target-profile",
        choices=(PI_ZERO_2_W_ARMV7_PROFILE,),
        help="explicit firmware/kernel/initramfs target required for file-image execution",
    )
    mode.add_argument(
        "--flash-device",
        metavar="TARGET",
        help="always refused; block-device flashing is a separate destructive workflow",
    )
    for name in ("xz", "mcopy", "mtype", "mdir", "debugfs", "zstd"):
        parser.add_argument(
            f"--{name}-path",
            type=Path,
            help=f"exact {name} executable; accepted only with an explicit file-image mode",
        )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Plan, probe, or create only the explicitly requested regular-file image."""

    args = _parser().parse_args(argv)
    tool_paths = {
        name: value
        for name in ("xz", "mcopy", "mtype", "mdir", "debugfs", "zstd")
        if (value := getattr(args, f"{name}_path")) is not None
    }
    execution_requested = args.execute_file_image or args.execute_authorized_exact_card_image
    if tool_paths and not execution_requested:
        _parser().error("explicit tool paths require an explicit file-image execution mode")
    if args.execute_authorized_exact_card_image and args.authorization_file is None:
        _parser().error("--execute-authorized-exact-card-image requires --authorization-file")
    if args.authorization_file is not None and not args.execute_authorized_exact_card_image:
        _parser().error("--authorization-file requires --execute-authorized-exact-card-image")
    if execution_requested and args.target_profile is None:
        _parser().error("file-image execution requires --target-profile")
    if args.target_profile is not None and not execution_requested:
        _parser().error("--target-profile requires an explicit file-image execution mode")
    if args.flash_device is not None:
        try:
            refuse_block_device_execution(args.flash_device)
        except ImageExecutionRefused as exc:
            print(refusal_json(exc), file=sys.stderr)
            return 2
    if args.probe_file_executor:
        print(json.dumps(probe_execution_dependencies().to_dict(), indent=2, sort_keys=True))
        return 0
    try:
        manifest = load_source_manifest(args.manifest.read_bytes())
        plan = author_image_build_plan(
            manifest=manifest,
            manifest_path=args.manifest,
            source_archive=args.source,
            output_image=args.output,
            payload_root=args.payload,
            dry_run=True,
        )
        if execution_requested:
            result = execute_file_image(
                plan=plan,
                manifest=manifest,
                source_archive=args.source,
                output_image=args.output,
                payload_root=args.payload,
                tool_paths=tool_paths or None,
                authorized_exact_card_trial=args.execute_authorized_exact_card_image,
                authorization_file=args.authorization_file,
                target_profile=args.target_profile,
            )
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
            return 0
    except ImageExecutionRefused as exc:
        print(refusal_json(exc), file=sys.stderr)
        return 2
    except (OSError, ImageBuildRefused) as exc:
        code = getattr(exc, "code", "invalid_input")
        value = code.value if hasattr(code, "value") else str(code)
        print(
            json.dumps({"planned": False, "code": value, "error": str(exc)}, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(plan.to_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
