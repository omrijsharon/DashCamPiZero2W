#!/usr/bin/env python3
"""Build the dual-source, hash-closed M10 private-runtime bundle."""

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
MAX_GIT_OUTPUT: Final = 24 * 1024 * 1024
MAX_SOURCE_MEMBERS: Final = 768
SOURCE_PREFIX: Final = "src/dashcam/"
HARNESS_MEMBERS: Final = ("README.md", "run.py")


class BundleError(RuntimeError):
    """The exact-source bundle could not be built safely."""


def _git(repository: Path, *arguments: str) -> bytes:
    if any(not value or "\0" in value or len(value) > 4096 for value in arguments):
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


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")


def _outside_repository(repository: Path, output: Path) -> None:
    repository = repository.resolve(strict=True)
    output = output.resolve(strict=False)
    if os.name == "posix":
        for forbidden in (Path("/srv/dashcam"), Path("/var/lib/dashcam"), Path("/run/dashcam")):
            try:
                output.relative_to(forbidden)
            except ValueError:
                continue
            raise BundleError("bundle output is inside a production path")
    try:
        output.relative_to(repository)
    except ValueError:
        return
    raise BundleError("bundle output must be outside the repository")


def _clean_exact_head(repository: Path, expected: str) -> tuple[str, str]:
    if COMMIT_RE.fullmatch(expected) is None:
        raise BundleError("harness commit must be a full lowercase SHA-1")
    head = _git(repository, "rev-parse", "--verify", "HEAD").decode("ascii").strip()
    if head != expected:
        raise BundleError("HEAD differs from the reviewed harness commit")
    for arguments in (("diff", "--quiet"), ("diff", "--cached", "--quiet")):
        result = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
            env={**os.environ, "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"},
        )
        if result.returncode != 0:
            raise BundleError("tracked harness worktree or index is dirty")
    tree = _git(repository, "rev-parse", f"{expected}^{{tree}}").decode("ascii").strip()
    if COMMIT_RE.fullmatch(tree) is None:
        raise BundleError("harness tree identity is malformed")
    return head, tree


def _commit_tree(repository: Path, commit: str) -> str:
    if COMMIT_RE.fullmatch(commit) is None:
        raise BundleError("rollback commit must be a full lowercase SHA-1")
    resolved = (
        _git(repository, "rev-parse", "--verify", f"{commit}^{{commit}}").decode("ascii").strip()
    )
    if resolved != commit:
        raise BundleError("rollback commit identity differs")
    tree = _git(repository, "rev-parse", f"{commit}^{{tree}}").decode("ascii").strip()
    if COMMIT_RE.fullmatch(tree) is None:
        raise BundleError("rollback tree identity is malformed")
    return tree


def _source_members(repository: Path, commit: str, *, rollback: bool) -> dict[str, bytes]:
    raw_names = _git(
        repository, "ls-tree", "-r", "-z", "--name-only", commit, "--", "src/dashcam"
    ).split(b"\0")
    members: dict[str, bytes] = {}
    for raw in raw_names:
        if not raw:
            continue
        try:
            name = raw.decode("utf-8")
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
        members[archive_name] = _git(repository, "show", f"{commit}:{name}")
    required = {"dashcam/__init__.py", "dashcam/recorder/runtime.py"}
    required.add("dashcam/rollback.py" if rollback else "dashcam/control/runtime_server.py")
    if not required.issubset(members):
        raise BundleError("required exact-source modules are absent")
    if len(members) > MAX_SOURCE_MEMBERS or sum(map(len, members.values())) > MAX_GIT_OUTPUT:
        raise BundleError("commit source exceeds the closed bundle bounds")
    return members


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    with tempfile.SpooledTemporaryFile(max_size=MAX_GIT_OUTPUT + 1) as stream:
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, payload in sorted(members.items()):
                info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, payload)
        stream.seek(0)
        payload = stream.read(MAX_GIT_OUTPUT + 1)
    if len(payload) > MAX_GIT_OUTPUT:
        raise BundleError("source archive exceeds its byte bound")
    return payload


def _source_metadata(
    commit: str, tree: str, archive_name: str, members: dict[str, bytes]
) -> tuple[bytes, bytes]:
    archive = _zip_bytes(members)
    metadata = {
        "schema_version": 1,
        "git_commit": commit,
        "git_tree": tree,
        "archive_name": archive_name,
        "archive_sha256": _sha256(archive),
        "archive_size": len(archive),
        "members": {
            name: {"sha256": _sha256(payload), "size": len(payload)}
            for name, payload in sorted(members.items())
        },
    }
    return archive, _canonical_json(metadata)


def _write_exclusive(path: Path, payload: bytes, mode: int = 0o644) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def build_bundle(
    repository: Path,
    output: Path,
    harness_commit: str,
    candidate_commit: str,
    rollback_commit: str,
) -> dict[str, object]:
    repository = repository.resolve(strict=True)
    _outside_repository(repository, output)
    harness_commit, harness_tree = _clean_exact_head(repository, harness_commit)
    candidate_tree = _commit_tree(repository, candidate_commit)
    rollback_tree = _commit_tree(repository, rollback_commit)
    candidate_archive, candidate_metadata = _source_metadata(
        candidate_commit,
        candidate_tree,
        "candidate-source.zip",
        _source_members(repository, candidate_commit, rollback=False),
    )
    rollback_archive, rollback_metadata = _source_metadata(
        rollback_commit,
        rollback_tree,
        "rollback-source.zip",
        _source_members(repository, rollback_commit, rollback=True),
    )
    prefix = "deploy/ssh-dev-validation/milestone10-private-runtime"
    payloads = {
        name: _git(repository, "show", f"{harness_commit}:{prefix}/{name}")
        for name in HARNESS_MEMBERS
    }
    payloads.update(
        {
            "BUNDLE.json": _canonical_json(
                {
                    "schema_version": 1,
                    "harness_commit": harness_commit,
                    "harness_tree": harness_tree,
                    "candidate_commit": candidate_commit,
                    "candidate_tree": candidate_tree,
                    "rollback_commit": rollback_commit,
                    "rollback_tree": rollback_tree,
                }
            ),
            "CANDIDATE_SOURCE.json": candidate_metadata,
            "ROLLBACK_SOURCE.json": rollback_metadata,
            "candidate-source.zip": candidate_archive,
            "rollback-source.zip": rollback_archive,
        }
    )
    if output.exists():
        raise BundleError("bundle output already exists")
    output.mkdir(mode=0o700, parents=False)
    for name, payload in payloads.items():
        _write_exclusive(output / name, payload, 0o755 if name == "run.py" else 0o644)
    manifest = b"".join(
        f"{_sha256(payloads[name])}  {name}\n".encode("ascii") for name in sorted(payloads)
    )
    _write_exclusive(output / "SHA256SUMS", manifest)
    if os.name == "posix":
        descriptor = os.open(output, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return {
        "schema_version": 1,
        "harness_commit": harness_commit,
        "harness_tree": harness_tree,
        "candidate_commit": candidate_commit,
        "candidate_tree": candidate_tree,
        "rollback_commit": rollback_commit,
        "rollback_tree": rollback_tree,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-harness-commit", required=True)
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--rollback-commit", required=True)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        result = build_bundle(
            arguments.repository,
            arguments.output,
            arguments.expected_harness_commit,
            arguments.candidate_commit,
            arguments.rollback_commit,
        )
    except (BundleError, OSError, UnicodeError, subprocess.SubprocessError) as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return 2
    print(_canonical_json(result).decode("ascii"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
