#!/usr/bin/env python3
"""Execute the reviewed Linux regular-file-only Bootstrap v1 build."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from dashcam.provisioning.bootstrap_image import (
    BootstrapImageRefusalCode,
    BootstrapImageRefused,
    BuildPaths,
    SourceMetadata,
    author_build_plan,
    cleanup_owned_work_files,
    compress_verified_raw,
    customize_regular_image,
    decompress_pinned_source,
    default_command_runner,
    grow_root_offline,
    load_builder_requirements,
    load_verification_evidence,
    make_imager_manifest,
    resolve_clean_app_commit,
    validate_build_paths,
    validate_new_retained_output,
    verify_builder_host,
    verify_pinned_source,
    write_new_file,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        allow_abbrev=False,
        description="Build only a new regular Bootstrap v1 image; never flash media.",
    )
    parser.add_argument("--repository", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--compressed", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument(
        "--requirements",
        type=Path,
        default=REPOSITORY_ROOT / "deploy/bootstrap/image/build-requirements.json",
    )
    parser.add_argument("--artifact-url", required=True)
    parser.add_argument("--release-date", required=True)
    return parser


def _app_wheel(wheelhouse: Path) -> tuple[Path, str]:
    if not wheelhouse.is_absolute() or not wheelhouse.is_dir() or wheelhouse.is_symlink():
        raise BootstrapImageRefused(
            BootstrapImageRefusalCode.PATH_UNSAFE,
            "wheelhouse must be an absolute real directory",
        )
    wheels = tuple(sorted(wheelhouse.glob("dashcam_pizero2w-*.whl")))
    if len(wheels) != 1 or wheels[0].is_symlink() or not wheels[0].is_file():
        raise ValueError("wheelhouse must contain exactly one regular DashCam wheel")
    return wheels[0], hashlib.sha256(wheels[0].read_bytes()).hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    raw = args.work_root / "dashcam-bootstrap-v1.img"
    paths = BuildPaths(args.source, args.work_root, raw, args.compressed, args.manifest)
    try:
        # This intentionally precedes all work creation. The current unborn
        # repository therefore fails explicitly without consuming image space.
        app_commit = resolve_clean_app_commit(args.repository, runner=default_command_runner)
        _, wheel_hash = _app_wheel(args.wheelhouse)
        lock_hash = hashlib.sha256((args.repository / "uv.lock").read_bytes()).hexdigest()
        metadata = SourceMetadata(app_commit, lock_hash, wheel_hash)
        source_verification = verify_pinned_source(args.source)
        validate_build_paths(paths)
        validate_new_retained_output(args.compressed, suffix=".img.xz")
        validate_new_retained_output(args.manifest, suffix=".rpi-imager-manifest")
        validate_new_retained_output(args.evidence, suffix=".verification.json")
        plan = author_build_plan(paths, metadata, source_verification)
        del plan
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
        args.work_root.mkdir()
        stage = args.work_root / "prepared-stage"
        prepare = args.repository / "deploy/bootstrap/image/prepare-stage.sh"
        prepare_result = default_command_runner(
            (
                requirements.tool("bash").executable,
                str(prepare),
                str(args.repository),
                str(stage),
                str(args.wheelhouse),
            )
        )
        if prepare_result.returncode != 0:
            raise RuntimeError(f"stage preparation failed: {prepare_result.stderr.strip()}")
        decompress_pinned_source(args.source, raw)
        grow_root_offline(
            raw,
            guestfish=Path(requirements.tool("guestfish").executable),
            runner=default_command_runner,
        )
        customize_regular_image(
            raw_image=raw,
            stage=stage,
            work_root=args.work_root,
            requirements=requirements,
            runner=default_command_runner,
        )
        verifier_work = args.work_root / "independent-verifier"
        verifier = args.repository / "scripts/verify_bootstrap_image.py"
        verify_result = default_command_runner(
            (
                sys.executable,
                str(verifier),
                "--raw",
                str(raw),
                "--source",
                str(args.source),
                "--work-root",
                str(verifier_work),
                "--evidence",
                str(args.evidence),
                "--requirements",
                str(args.requirements),
                "--app-commit",
                app_commit,
                "--package-lock-sha256",
                lock_hash,
                "--app-wheel-sha256",
                wheel_hash,
            )
        )
        if verify_result.returncode != 0:
            raise RuntimeError(f"independent verifier failed: {verify_result.stderr.strip()}")
        evidence = load_verification_evidence(args.evidence.read_bytes())
        proof = compress_verified_raw(raw, args.compressed)
        manifest = make_imager_manifest(
            proof=proof,
            evidence=evidence,
            artifact_url=args.artifact_url,
            release_date=args.release_date,
            metadata=metadata,
        )
        write_new_file(args.manifest, manifest, suffix=".rpi-imager-manifest")
        cleanup_owned_work_files(
            work_root=args.work_root,
            owned_files=(raw,),
            compressed_verified=True,
        )
        print(
            json.dumps(
                {
                    "built": True,
                    "compressed": str(args.compressed),
                    "manifest": str(args.manifest),
                    "evidence": str(args.evidence),
                    "raw_removed": True,
                },
                sort_keys=True,
            )
        )
        return 0
    except (BootstrapImageRefused, FileExistsError, OSError, RuntimeError, ValueError) as exc:
        code = getattr(exc, "code", "build_failed")
        value = code.value if hasattr(code, "value") else str(code)
        print(json.dumps({"built": False, "code": value, "error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
