#!/usr/bin/env python3
"""Create the small, hash-closed SSH-development transfer payload."""

from __future__ import annotations

import argparse
import hashlib
import os
import stat
from pathlib import Path

PAYLOAD_FILES = (
    "README.md",
    "arm-cmdline.py",
    "authorized-exact-card-ssh-dev-v1.json",
    "install.sh",
    "recover-exfat-reconciliation-refusal.py",
    "recover-fssize-refusal.py",
)
BOOTSTRAP_SOURCE = Path("src/dashcam/provisioning/bootstrap.py")
MAX_PAYLOAD_FILE_BYTES = 512 * 1024


def _safe_directory(path: Path) -> None:
    if not path.is_absolute():
        raise ValueError("repository and output paths must be absolute")
    current = Path(path.anchor)
    for component in path.parts[1:]:
        if component in {"", ".", ".."}:
            raise ValueError("path has an unsafe component")
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError as exc:
            raise ValueError("required directory is absent") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("directory chain contains a symlink or non-directory")


def _checked_read(path: Path) -> bytes:
    _safe_directory(path.parent)
    try:
        listed = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError(f"payload source is absent: {path.name}") from exc
    if stat.S_ISLNK(listed.st_mode) or not stat.S_ISREG(listed.st_mode):
        raise ValueError(f"payload source is not a regular non-symlink file: {path.name}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"payload source could not be safely opened: {path.name}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError(f"payload source has unsafe type or links: {path.name}")
        if info.st_size > MAX_PAYLOAD_FILE_BYTES:
            raise ValueError(f"payload source is oversized: {path.name}")
        chunks: list[bytes] = []
        remaining = MAX_PAYLOAD_FILE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(4096, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        value = b"".join(chunks)
    finally:
        os.close(descriptor)
    if len(value) > MAX_PAYLOAD_FILE_BYTES:
        raise ValueError(f"payload source is oversized: {path.name}")
    return value


def _exclusive_write(path: Path, value: bytes) -> None:
    _safe_directory(path.parent)
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as exc:
        raise ValueError(f"payload output could not be exclusively created: {path.name}") from exc
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def prepare(repository: Path, output: Path) -> None:
    _safe_directory(repository)
    source = repository / "deploy" / "bootstrap" / "ssh-dev"
    _safe_directory(source)
    try:
        output.lstat()
    except FileNotFoundError:
        pass
    else:
        raise ValueError("output directory must not already exist")
    _safe_directory(output.parent)
    if _is_within(output, repository) or _is_within(repository, output):
        raise ValueError("repository and output directories must not overlap")
    output.mkdir(mode=0o700, parents=True)
    copied = list(PAYLOAD_FILES)
    for name in PAYLOAD_FILES:
        _exclusive_write(output / name, _checked_read(source / name))
    _exclusive_write(output / "bootstrap.py", _checked_read(repository / BOOTSTRAP_SOURCE))
    copied.append("bootstrap.py")
    lines = [
        f"{hashlib.sha256(_checked_read(output / name)).hexdigest()}  {name}\n"
        for name in sorted(copied)
    ]
    _exclusive_write(output / "SHA256SUMS", "".join(lines).encode("ascii"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    try:
        prepare(args.repository, args.output)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
