#!/usr/bin/env python3
"""Author a deterministic provisioning plan from captured JSON observations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import cast

from dashcam.provisioning.layout import (
    DeviceIdentity,
    LayoutError,
    load_layout_toml,
    observation_from_mapping,
)
from dashcam.provisioning.planner import ProvisioningError, author_provisioning_plan

MAX_INPUT_BYTES = 256 * 1024


def _json_object(path: Path) -> dict[str, object]:
    with path.open("rb") as stream:
        payload = stream.read(MAX_INPUT_BYTES + 1)
    if len(payload) > MAX_INPUT_BYTES:
        raise LayoutError(f"{path} exceeds {MAX_INPUT_BYTES} bytes")
    decoded = json.loads(payload)
    if not isinstance(decoded, dict):
        raise LayoutError(f"{path} must contain a JSON object")
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
        raise LayoutError("identity JSON has missing or unknown keys")
    return DeviceIdentity(
        resolved_path=_required(raw, "resolved_path", str),
        serial=_required(raw, "serial", str),
        size_bytes=_required_int(raw, "size_bytes"),
        partition_table_fingerprint=_required(raw, "partition_table_fingerprint", str),
    )


def _required(raw: dict[str, object], key: str, kind: type[str]) -> str:
    value = raw[key]
    if not isinstance(value, kind):
        raise LayoutError(f"identity.{key} has the wrong type")
    return value


def _required_int(raw: dict[str, object], key: str) -> int:
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise LayoutError(f"identity.{key} has the wrong type")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dry-run-only provisioning plan authoring; never executes commands."
    )
    parser.add_argument("--layout", type=Path, default=Path("deploy/storage/layout-v1.toml"))
    parser.add_argument("--observation", action="append", type=Path, required=True)
    parser.add_argument("--expected-identity", type=Path, required=True)
    parser.add_argument("--recheck", type=Path)
    parser.add_argument(
        "--non-dry-run",
        action="store_true",
        help="always refused in v1; present only to make the execution gate explicit",
    )
    parser.add_argument("--typed-confirmation")
    args = parser.parse_args(argv)
    try:
        spec = load_layout_toml(args.layout.read_bytes())
        observations = [observation_from_mapping(_json_object(path)) for path in args.observation]
        recheck = (
            None if args.recheck is None else observation_from_mapping(_json_object(args.recheck))
        )
        plan = author_provisioning_plan(
            spec=spec,
            observations=observations,
            expected_identity=_identity(args.expected_identity),
            recheck=recheck,
            dry_run=not args.non_dry_run,
            typed_confirmation=args.typed_confirmation,
        )
    except (OSError, json.JSONDecodeError, LayoutError, ProvisioningError) as exc:
        code = getattr(exc, "code", "invalid_input")
        code_value = code.value if hasattr(code, "value") else str(code)
        print(json.dumps({"planned": False, "code": code_value, "error": str(exc)}))
        return 2
    print(json.dumps(plan.to_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
