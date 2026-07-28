#!/usr/bin/env python3
"""Fail-closed, byte-preserving armer for the SSH-development bootstrap trigger."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

DEV_TRIGGER = b"dashcam.bootstrap=ssh-dev-v1"
RELEASE_TRIGGER = b"dashcam.bootstrap=v1"
RESIZE_TOKEN = b"resize"
DEFAULT_CMDLINE = Path("/boot/firmware/cmdline.txt")
MAX_CMDLINE_BYTES = 16 * 1024


class ArmError(RuntimeError):
    """A fail-closed cmdline validation or write failure."""


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_parent_chain(path: Path) -> None:
    if not path.is_absolute():
        raise ArmError("cmdline path must be absolute")
    current = Path(path.anchor)
    for component in path.parent.parts[1:]:
        if component in {"", ".", ".."}:
            raise ArmError("cmdline path has an unsafe parent component")
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError as exc:
            raise ArmError("cmdline parent is absent") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ArmError("cmdline parent chain is unsafe")


def _open_regular(path: Path) -> tuple[int, os.stat_result]:
    _safe_parent_chain(path)
    try:
        before_open = path.lstat()
    except FileNotFoundError as exc:
        raise ArmError("cmdline file is absent") from exc
    if stat.S_ISLNK(before_open.st_mode) or not stat.S_ISREG(before_open.st_mode):
        raise ArmError("cmdline path is not a regular non-symlink file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ArmError("cmdline file could not be safely opened") from exc
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        os.close(descriptor)
        raise ArmError("cmdline path is not a regular file after open")
    if info.st_nlink != 1:
        os.close(descriptor)
        raise ArmError("cmdline file has multiple links")
    if info.st_size > MAX_CMDLINE_BYTES:
        os.close(descriptor)
        raise ArmError("cmdline file is oversized")
    return descriptor, info


def _read_descriptor(descriptor: int, maximum: int) -> bytes:
    chunks: list[bytes] = []
    remaining = maximum + 1
    while remaining:
        piece = os.read(descriptor, min(4096, remaining))
        if not piece:
            break
        chunks.append(piece)
        remaining -= len(piece)
    value = b"".join(chunks)
    if len(value) > maximum:
        raise ArmError("cmdline file is oversized")
    return value


def _read_cmdline(path: Path) -> tuple[bytes, list[bytes], os.stat_result]:
    descriptor, info = _open_regular(path)
    try:
        value = _read_descriptor(descriptor, MAX_CMDLINE_BYTES)
    finally:
        os.close(descriptor)
    if b"\r" in value or value.count(b"\n") > 1 or (b"\n" in value and not value.endswith(b"\n")):
        raise ArmError("cmdline must be one ASCII line with an optional final newline")
    try:
        body = value[:-1] if value.endswith(b"\n") else value
        body.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ArmError("cmdline is not ASCII") from exc
    tokens = body.split()
    if not any(
        token.startswith(b"root=PARTUUID=") and len(token) > len(b"root=PARTUUID=")
        for token in tokens
    ):
        raise ArmError("cmdline lacks a root=PARTUUID token")
    return value, tokens, info


def _counts(tokens: list[bytes]) -> dict[str, int]:
    return {
        "resize_count": tokens.count(RESIZE_TOKEN),
        "release_trigger_count": tokens.count(RELEASE_TRIGGER),
        "dev_trigger_count": tokens.count(DEV_TRIGGER),
    }


def _proposed(before: bytes) -> bytes:
    suffix = b"\n" if before.endswith(b"\n") else b""
    body = before[:-1] if suffix else before
    return body + (b" " if body else b"") + DEV_TRIGGER + suffix


def _result(
    *,
    operation: str,
    outcome: str,
    ready: bool,
    before: bytes,
    tokens: list[bytes],
    after: bytes | None = None,
) -> dict[str, object]:
    output: dict[str, object] = {
        "after_sha256": _digest(after) if after is not None else None,
        "before_sha256": _digest(before),
        "counts": _counts(tokens),
        "operation": operation,
        "outcome": outcome,
        "ready": ready,
    }
    return output


def _emit(value: dict[str, object]) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


def _backup_dir_for(path: Path, override: Path | None) -> Path:
    directory = override if override is not None else path.parent / "dashcam-bootstrap"
    if not directory.is_absolute():
        raise ArmError("backup directory must be absolute")
    return directory


def _durable_backup(directory: Path, before: bytes) -> None:
    _safe_parent_chain(directory)
    directory.mkdir(mode=0o700, parents=False, exist_ok=True)
    info = directory.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ArmError("backup directory is unsafe")
    name = "cmdline.before-" + _digest(before) + ".bak"
    target = directory / name
    try:
        fd = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except FileExistsError:
        descriptor, _info = _open_regular(target)
        try:
            if _read_descriptor(descriptor, MAX_CMDLINE_BYTES) != before:
                raise ArmError("existing cmdline backup differs")
        finally:
            os.close(descriptor)
        _fsync_directory(directory)
        return
    except OSError as exc:
        raise ArmError("exclusive cmdline backup could not be created") from exc
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(before)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            target.unlink(missing_ok=True)
        finally:
            raise
    descriptor, _info = _open_regular(target)
    try:
        readback = _read_descriptor(descriptor, MAX_CMDLINE_BYTES)
    finally:
        os.close(descriptor)
    if readback != before:
        raise ArmError("cmdline backup readback differs")
    _fsync_directory(directory)


def _fsync_directory(directory: Path) -> None:
    """Durably flush a directory where the host OS supports directory fds."""

    if os.name == "nt":  # Windows is a test host; the Pi runtime is Linux.
        return
    directory_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _atomic_replace(path: Path, value: bytes, mode: int) -> None:
    _safe_parent_chain(path)
    temporary = path.parent / ("." + path.name + ".dashcam-arm.tmp")
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            mode,
        )
    except FileExistsError as exc:
        raise ArmError("cmdline temporary path already exists") from exc
    except OSError as exc:
        raise ArmError("cmdline temporary file could not be safely created") from exc
    try:
        with os.fdopen(fd, "wb") as stream:
            temporary_info = os.fstat(stream.fileno())
            if not stat.S_ISREG(temporary_info.st_mode) or temporary_info.st_nlink != 1:
                raise ArmError("cmdline temporary file is unsafe after creation")
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
        if hasattr(os, "sync"):
            os.sync()
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _validate_pre_arm(tokens: list[bytes]) -> None:
    counts = _counts(tokens)
    if any(counts.values()):
        raise ArmError("cmdline already contains a forbidden or bootstrap trigger token")


def run(args: argparse.Namespace) -> dict[str, object]:
    path = Path(args.cmdline)
    before, tokens, before_info = _read_cmdline(path)
    if args.verify:
        counts = _counts(tokens)
        ready = counts == {"resize_count": 0, "release_trigger_count": 0, "dev_trigger_count": 1}
        return _result(
            operation="verify",
            outcome="verified" if ready else "refused",
            ready=ready,
            before=before,
            tokens=tokens,
        )
    _validate_pre_arm(tokens)
    after = _proposed(before)
    if args.dry_run:
        return _result(
            operation="dry-run",
            outcome="would_apply",
            ready=True,
            before=before,
            tokens=tokens,
            after=after,
        )
    expected = args.expected_before_sha256.lower()
    if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
        raise ArmError("expected-before-sha256 must be lowercase SHA-256")
    if _digest(before) != expected:
        raise ArmError("cmdline digest no longer matches expected-before-sha256")
    backup_override = Path(args.backup_dir) if args.backup_dir else None
    _durable_backup(_backup_dir_for(path, backup_override), before)
    current, current_tokens, current_info = _read_cmdline(path)
    if current != before or _counts(current_tokens) != _counts(tokens):
        raise ArmError("cmdline changed after its exclusive backup was written")
    if current_info.st_ino != before_info.st_ino:
        raise ArmError("cmdline inode changed after its exclusive backup was written")
    _atomic_replace(path, after, stat.S_IMODE(current_info.st_mode))
    observed, observed_tokens, _observed_info = _read_cmdline(path)
    if observed != after:
        raise ArmError("cmdline readback differs after atomic replacement")
    counts = _counts(observed_tokens)
    if counts != {"resize_count": 0, "release_trigger_count": 0, "dev_trigger_count": 1}:
        raise ArmError("post-apply cmdline does not have the exact development trigger")
    return _result(
        operation="apply",
        outcome="applied",
        ready=True,
        before=before,
        tokens=tokens,
        after=observed,
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cmdline", default=str(DEFAULT_CMDLINE))
    parser.add_argument("--backup-dir")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--verify", action="store_true")
    parser.add_argument("--expected-before-sha256")
    args = parser.parse_args(argv)
    if args.apply != (args.expected_before_sha256 is not None):
        parser.error("--apply requires --expected-before-sha256 and vice versa")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        result = run(args)
        _emit(result)
        return 0 if result["ready"] is True else 2
    except ArmError as exc:
        _emit({"operation": "refused", "outcome": "refused", "ready": False, "reason": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
