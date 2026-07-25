#!/usr/bin/env python3
"""Inspect one first-boot storage transition; execution is intentionally gated off."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import cast

from dashcam.provisioning.firstboot import (
    FirstbootError,
    FirstbootRefused,
    RefusalCode,
    RuntimeStage,
    evidence_from_mapping,
    journal_from_mapping,
    plan_next,
    start_journal,
)
from dashcam.provisioning.layout import DeviceIdentity, LayoutError, load_layout_toml

MAX_JSON_BYTES = 256 * 1024


def _json_object(path: Path) -> dict[str, object]:
    with path.open("rb") as stream:
        payload = stream.read(MAX_JSON_BYTES + 1)
    if len(payload) > MAX_JSON_BYTES:
        raise FirstbootError(f"{path} exceeds {MAX_JSON_BYTES} bytes")
    decoded = json.loads(payload)
    if not isinstance(decoded, dict):
        raise FirstbootError(f"{path} must contain a JSON object")
    return cast(dict[str, object], decoded)


def _identity(path: Path) -> DeviceIdentity:
    raw = _json_object(path)
    required = {
        "resolved_path",
        "serial",
        "size_bytes",
        "partition_table_fingerprint",
    }
    if set(raw) != required:
        raise FirstbootError("expected identity has missing or unknown keys")
    resolved_path = raw["resolved_path"]
    serial = raw["serial"]
    size_bytes = raw["size_bytes"]
    fingerprint = raw["partition_table_fingerprint"]
    if (
        not isinstance(resolved_path, str)
        or not isinstance(serial, str)
        or isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or not isinstance(fingerprint, str)
    ):
        raise FirstbootError("expected identity has an invalid value")
    return DeviceIdentity(resolved_path, serial, size_bytes, fingerprint)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate and render one bounded first-boot phase. "
            "The checked-in command never executes storage actions."
        )
    )
    parser.add_argument("--stage", choices=tuple(RuntimeStage), required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--expected-identity", type=Path)
    parser.add_argument("--layout", type=Path, default=Path("deploy/storage/layout-v1.toml"))
    parser.add_argument(
        "--execute",
        action="store_true",
        help="refused until the exact initramfs/runtime gate is validated on the Pi",
    )
    args = parser.parse_args(argv)
    try:
        if args.execute:
            raise FirstbootRefused(
                RefusalCode.UNVERIFIED_RUNTIME,
                "checked-in CLI execution is blocked pending exact-image runtime validation",
            )
        spec = load_layout_toml(args.layout.read_bytes())
        evidence = evidence_from_mapping(_json_object(args.evidence))
        if args.journal is None:
            if args.expected_identity is None:
                raise FirstbootError("--expected-identity is required when starting a journal")
            journal = start_journal(spec, evidence, _identity(args.expected_identity))
        else:
            if args.expected_identity is not None:
                raise FirstbootError("--expected-identity is only valid for a new journal")
            journal = journal_from_mapping(_json_object(args.journal))
        plan = plan_next(spec, RuntimeStage(args.stage), evidence, journal)
    except (OSError, json.JSONDecodeError, LayoutError, FirstbootError) as exc:
        code = exc.code.value if isinstance(exc, FirstbootRefused) else "invalid_input"
        print(json.dumps({"accepted": False, "code": code, "error": str(exc)}))
        return 2
    print(json.dumps({"accepted": True, "plan": plan.to_dict()}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
