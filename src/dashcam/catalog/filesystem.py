"""Constrained filesystem interface used by catalog reconciliation."""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Callable
from contextlib import suppress
from ctypes import CDLL, c_char_p, c_int, get_errno
from errno import EEXIST
from pathlib import Path, PurePosixPath
from typing import Protocol, cast

from dashcam.storage.intents import PairPaths


class CatalogFilesystem(Protocol):
    """Minimal filesystem behavior needed for pair reconciliation."""

    def exists(self, relative_path: str) -> bool:
        """Return whether one managed relative path exists."""

    def move(self, source: str, target: str) -> None:
        """Move one member without replacing an existing target."""

    def unlink(self, relative_path: str) -> None:
        """Remove one member; absence is an idempotent success."""

    def iter_files(self, directory: str, *, limit: int) -> tuple[tuple[str, ...], int, bool]:
        """Return paths, directory entries examined, and a truncation flag."""

    def read_bytes(self, relative_path: str, *, maximum_bytes: int) -> bytes:
        """Read a bounded file, rejecting values larger than ``maximum_bytes``."""

    def file_size(self, relative_path: str) -> int:
        """Return the size of one managed regular file."""

    def replace_bytes_atomic(
        self,
        relative_path: str,
        payload: bytes,
        *,
        maximum_bytes: int,
    ) -> None:
        """Durably replace one bounded regular file without changing its path."""


class RootedFilesystem:
    """Real filesystem adapter confined below one explicitly supplied root."""

    _DIRECTORIES = frozenset({"pending", "clips", "protected", "quarantine"})

    def __init__(self, root: Path, *, expected_device_id: str | None = None) -> None:
        if not isinstance(root, Path):
            raise TypeError("root must be a pathlib.Path")
        if root.is_symlink():
            raise ValueError("recording root cannot be a symbolic link")
        self._root = root.resolve(strict=True)
        if not self._root.is_dir():
            raise ValueError("recording root must be a directory")
        self._expected_device_id = expected_device_id or _device_id(self._root.stat().st_dev)
        if self._expected_device_id != _device_id(self._root.stat().st_dev):
            raise ValueError("recording root device identity differs")

    @property
    def root(self) -> Path:
        return self._root

    def exists(self, relative_path: str) -> bool:
        return self._resolve(relative_path).is_file()

    def move(self, source: str, target: str) -> None:
        source_path = self._resolve(source)
        target_path = self._resolve(target)
        _rename_no_replace(source_path, target_path)
        _fsync_directory(source_path.parent)
        if target_path.parent != source_path.parent:
            _fsync_directory(target_path.parent)

    def unlink(self, relative_path: str) -> None:
        with suppress(FileNotFoundError):
            self._resolve(relative_path).unlink()

    def iter_files(self, directory: str, *, limit: int) -> tuple[tuple[str, ...], int, bool]:
        if directory not in self._DIRECTORIES:
            raise ValueError("directory is outside the managed namespace")
        _positive_bound(limit, "limit")
        directory_path = self._resolve_directory(directory)
        if not directory_path.exists():
            return (), 0, False
        paths: list[str] = []
        entries_examined = 0
        truncated = False
        with os.scandir(directory_path) as entries:
            for entry in entries:
                if entries_examined == limit:
                    # The one-entry lookahead is not processed; it only tells the
                    # caller another bounded pass is required.
                    truncated = True
                    break
                entries_examined += 1
                if entry.is_file(follow_symlinks=False):
                    paths.append(PurePosixPath(directory, entry.name).as_posix())
        paths.sort(key=str.casefold)
        return tuple(paths), entries_examined, truncated

    def read_bytes(self, relative_path: str, *, maximum_bytes: int) -> bytes:
        _positive_bound(maximum_bytes, "maximum_bytes")
        path = self._resolve(relative_path)
        if path.stat().st_size > maximum_bytes:
            raise ValueError("file exceeds recovery size bound")
        with path.open("rb") as stream:
            value = stream.read(maximum_bytes + 1)
        if len(value) > maximum_bytes:
            raise ValueError("file exceeds recovery size bound")
        return value

    def file_size(self, relative_path: str) -> int:
        path = self._resolve(relative_path)
        if not path.is_file():
            raise FileNotFoundError(path)
        return path.stat().st_size

    def replace_bytes_atomic(
        self,
        relative_path: str,
        payload: bytes,
        *,
        maximum_bytes: int,
    ) -> None:
        """Replace one managed file through a flushed sibling and atomic rename."""

        _positive_bound(maximum_bytes, "maximum_bytes")
        if not isinstance(payload, bytes) or not payload or len(payload) > maximum_bytes:
            raise ValueError("replacement payload is empty or exceeds its bound")
        path = self._resolve(relative_path)
        if not path.is_file():
            raise FileNotFoundError(path)
        descriptor = -1
        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.",
                suffix=".tmp",
                dir=path.parent,
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
            _fsync_directory(path.parent)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_path is not None:
                with suppress(FileNotFoundError):
                    temporary_path.unlink()

    def _resolve_directory(self, directory: str) -> Path:
        self._assert_bound()
        if directory not in self._DIRECTORIES:
            raise ValueError("directory is outside the managed namespace")
        path = self._root / directory
        if path.exists():
            resolved = path.resolve(strict=True)
            try:
                resolved.relative_to(self._root)
            except ValueError as exc:
                raise ValueError("managed directory escapes recording root") from exc
            if path.is_symlink():
                raise ValueError("managed directories cannot be symbolic links")
        return path

    def _resolve(self, relative_path: str) -> Path:
        # PairPaths supplies the repository's single strict relative-path
        # validator without duplicating its Windows/exFAT portability rules.
        PairPaths(relative_path, "__validation__.json")
        directory = PurePosixPath(relative_path).parts[0]
        self._resolve_directory(directory)
        path = self._root.joinpath(*PurePosixPath(relative_path).parts)
        resolved_parent = path.parent.resolve(strict=True)
        try:
            resolved_parent.relative_to(self._root)
        except ValueError as exc:  # defensive if the validator changes
            raise ValueError("path escapes recording root") from exc
        if path.is_symlink():
            raise ValueError("managed paths cannot be symbolic links")
        return path

    def _assert_bound(self) -> None:
        info = self._root.lstat()
        if (
            not self._root.is_dir()
            or self._root.is_symlink()
            or _device_id(info.st_dev) != self._expected_device_id
        ):
            raise OSError("recording root is no longer bound to the verified device")


def _positive_bound(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _device_id(device: int) -> str:
    major = cast(Callable[[int], int] | None, getattr(os, "major", None))
    minor = cast(Callable[[int], int] | None, getattr(os, "minor", None))
    if major is None or minor is None:
        return str(device)
    return f"{major(device)}:{minor(device)}"


def _rename_no_replace(source: Path, target: Path) -> None:
    """Atomically rename without replacement on the Linux production target."""

    if sys.platform.startswith("linux"):
        library = CDLL(None, use_errno=True)
        renameat2 = library.renameat2
        renameat2.argtypes = (c_int, c_char_p, c_int, c_char_p, c_int)
        renameat2.restype = c_int
        at_fdcwd = -100
        rename_noreplace = 1
        result = renameat2(
            at_fdcwd,
            os.fsencode(source),
            at_fdcwd,
            os.fsencode(target),
            rename_noreplace,
        )
        if result != 0:
            error_number = get_errno()
            if error_number == EEXIST:
                raise FileExistsError(error_number, os.strerror(error_number), target)
            raise OSError(error_number, os.strerror(error_number), source)
        return
    if target.exists():
        raise FileExistsError(target)
    source.rename(target)


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
