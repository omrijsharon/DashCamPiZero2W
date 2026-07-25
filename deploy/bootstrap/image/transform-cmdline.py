#!/usr/bin/env python3
"""Apply the tested Bootstrap v1 cmdline transform to one regular file."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

from dashcam.provisioning.bootstrap_image import transform_cmdline


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: transform-cmdline.py CMDLINE", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode):
        print(f"cmdline is not a regular file: {path}", file=sys.stderr)
        return 2
    original = path.read_text(encoding="utf-8")
    transformed = transform_cmdline(original)
    temporary = path.with_name(f".{path.name}.dashcam-bootstrap-v1.tmp")
    if temporary.exists() or temporary.is_symlink():
        print(f"temporary output exists: {temporary}", file=sys.stderr)
        return 2
    with temporary.open("x", encoding="utf-8", newline="") as stream:
        stream.write(transformed)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
