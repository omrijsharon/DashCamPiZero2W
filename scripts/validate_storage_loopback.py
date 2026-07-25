#!/usr/bin/env python3
"""Run the regular-file-only storage layout validation harness."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from dashcam.provisioning.layout import LayoutError, load_layout_toml
from dashcam.provisioning.loopback import run_validation


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate storage geometry on disposable sparse files."
    )
    parser.add_argument(
        "--output-dir", type=Path, required=True, help="new empty directory created by caller"
    )
    parser.add_argument("--layout", type=Path, default=Path("deploy/storage/layout-v1.toml"))
    parser.add_argument("--no-fault-matrix", action="store_true")
    args = parser.parse_args(argv)
    try:
        spec = load_layout_toml(args.layout.read_bytes())
    except (OSError, LayoutError) as exc:
        print(json.dumps({"status": "refused", "code": "invalid_layout", "error": str(exc)}))
        return 2
    report = run_validation(args.output_dir, spec, fault_matrix=not args.no_fault_matrix)
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    if report.status.value == "passed":
        return 0
    if report.status.value == "skipped":
        return 3
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
