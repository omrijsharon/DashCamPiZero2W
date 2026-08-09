#!/usr/bin/env python3
"""Build an exact-commit, hash-closed Milestone 10 loop-test bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Final

COMMIT_RE: Final = re.compile(r"[0-9a-f]{40}")
MAX_GIT_OUTPUT: Final = 16 * 1024 * 1024
HARNESS_MEMBERS: Final = ("README.md", "run.py")
SOURCE_PREFIX: Final = "src/dashcam/"


class BundleError(RuntimeError):
    """A closed-source bundle could not be built safely."""


def _run_git(repository: Path, *arguments: str) -> bytes:
    if any(not item or "\x00" in item or len(item) > 4096 for item in arguments):
        raise BundleError("unsafe git argument")
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=30,
            env={**os.environ, "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BundleError("bounded git command failed") from error
    if result.returncode != 0:
        raise BundleError("git refused the requested exact source")
    if len(result.stdout) > MAX_GIT_OUTPUT or len(result.stderr) > 65536:
        raise BundleError("git output exceeded its bound")
    return result.stdout


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _require_outside(repository: Path, output: Path) -> None:
    repository = repository.resolve(strict=True)
    output = output.resolve(strict=False)
    if os.name == "posix":
        for forbidden in (Path("/srv/dashcam"), Path("/var/lib/dashcam")):
            try:
                output.relative_to(forbidden)
            except ValueError:
                continue
            raise BundleError("bundle output is inside production storage")
    try:
        output.relative_to(repository)
    except ValueError:
        return
    raise BundleError("bundle output must be outside the repository")


def _require_clean_exact_head(repository: Path, expected_commit: str) -> tuple[str, str]:
    if COMMIT_RE.fullmatch(expected_commit) is None:
        raise BundleError("expected commit must be a full lowercase SHA-1")
    head = _run_git(repository, "rev-parse", "--verify", "HEAD").decode("ascii").strip()
    if head != expected_commit:
        raise BundleError("HEAD differs from the reviewed expected commit")
    for arguments in (("diff", "--quiet"), ("diff", "--cached", "--quiet")):
        try:
            result = subprocess.run(
                ["git", "-C", str(repository), *arguments],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=30,
                env={**os.environ, "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"},
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise BundleError("bounded git cleanliness check failed") from error
        if result.returncode != 0:
            raise BundleError("tracked worktree or index differs from exact HEAD")
    tree = _run_git(repository, "rev-parse", f"{expected_commit}^{{tree}}").decode("ascii").strip()
    if COMMIT_RE.fullmatch(tree) is None:
        raise BundleError("commit tree identity is malformed")
    return head, tree


def _source_members(repository: Path, commit: str) -> dict[str, bytes]:
    names = _run_git(
        repository, "ls-tree", "-r", "-z", "--name-only", commit, "--", "src/dashcam"
    ).split(b"\0")
    members: dict[str, bytes] = {}
    for raw_name in names:
        if not raw_name:
            continue
        try:
            name = raw_name.decode("utf-8")
        except UnicodeDecodeError as error:
            raise BundleError("source path is not UTF-8") from error
        path = PurePosixPath(name)
        if (
            not name.startswith(SOURCE_PREFIX)
            or path.is_absolute()
            or ".." in path.parts
            or not (name.endswith(".py") or name.endswith("/py.typed"))
        ):
            continue
        archive_name = name.removeprefix("src/")
        if archive_name in members:
            raise BundleError("duplicate source archive member")
        members[archive_name] = _run_git(repository, "show", f"{commit}:{name}")
    if "dashcam/__init__.py" not in members or "dashcam/storage/reclaimer.py" not in members:
        raise BundleError("required commit-source modules are absent")
    if len(members) > 512 or sum(map(len, members.values())) > MAX_GIT_OUTPUT:
        raise BundleError("commit source exceeds the closed bundle bounds")
    return members


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    with tempfile.SpooledTemporaryFile(max_size=MAX_GIT_OUTPUT + 1) as stream:
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
            for name in sorted(members):
                info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, members[name])
        stream.seek(0)
        payload = stream.read(MAX_GIT_OUTPUT + 1)
    if len(payload) > MAX_GIT_OUTPUT:
        raise BundleError("source archive exceeds its byte bound")
    return payload


def _write_exclusive(path: Path, payload: bytes, mode: int = 0o644) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def build_bundle(repository: Path, output: Path, expected_commit: str) -> dict[str, object]:
    repository = repository.resolve(strict=True)
    _require_outside(repository, output)
    commit, tree = _require_clean_exact_head(repository, expected_commit)
    source = _source_members(repository, commit)
    archive_payload = _zip_bytes(source)
    member_facts = {
        name: {"sha256": _sha256(payload), "size": len(payload)}
        for name, payload in sorted(source.items())
    }
    source_metadata: dict[str, object] = {
        "schema_version": 1,
        "git_commit": commit,
        "git_tree": tree,
        "archive_name": "dashcam-source.zip",
        "archive_sha256": _sha256(archive_payload),
        "archive_size": len(archive_payload),
        "members": member_facts,
    }
    payloads = {
        name: _run_git(
            repository,
            "show",
            f"{commit}:deploy/ssh-dev-validation/milestone10-retention-loop/{name}",
        )
        for name in HARNESS_MEMBERS
    }
    payloads["SOURCE.json"] = _canonical_json(source_metadata)
    payloads["dashcam-source.zip"] = archive_payload
    if output.exists():
        raise BundleError("bundle output already exists")
    output.mkdir(mode=0o700, parents=False)
    try:
        for name, payload in payloads.items():
            _write_exclusive(output / name, payload, 0o755 if name == "run.py" else 0o644)
        manifest = b"".join(
            f"{_sha256(payloads[name])}  {name}\n".encode("ascii") for name in sorted(payloads)
        )
        _write_exclusive(output / "SHA256SUMS", manifest)
        if os.name == "posix":
            directory_fd = os.open(output, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except BaseException:
        # The output was new and is outside the source tree; retain partial bytes for diagnosis.
        raise
    return source_metadata


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-commit", required=True)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        metadata = build_bundle(arguments.repository, arguments.output, arguments.expected_commit)
    except (BundleError, OSError, UnicodeError, json.JSONDecodeError) as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return 2
    print(_canonical_json(metadata).decode("ascii"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
