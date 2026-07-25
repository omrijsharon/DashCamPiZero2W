"""Constrained filesystem interface used by catalog reconciliation."""

from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Protocol

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


class RootedFilesystem:
    """Real filesystem adapter confined below one explicitly supplied root."""

    _DIRECTORIES = frozenset({"pending", "clips", "protected", "quarantine"})

    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path):
            raise TypeError("root must be a pathlib.Path")
        self._root = root.resolve(strict=True)
        if not self._root.is_dir():
            raise ValueError("recording root must be a directory")

    @property
    def root(self) -> Path:
        return self._root

    def exists(self, relative_path: str) -> bool:
        return self._resolve(relative_path).is_file()

    def move(self, source: str, target: str) -> None:
        source_path = self._resolve(source)
        target_path = self._resolve(target)
        if target_path.exists():
            raise FileExistsError(target_path)
        source_path.rename(target_path)

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

    def _resolve_directory(self, directory: str) -> Path:
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
        path = self._root.joinpath(*PurePosixPath(relative_path).parts)
        resolved_parent = path.parent.resolve(strict=True)
        try:
            resolved_parent.relative_to(self._root)
        except ValueError as exc:  # defensive if the validator changes
            raise ValueError("path escapes recording root") from exc
        if path.is_symlink():
            raise ValueError("managed paths cannot be symbolic links")
        return path


def _positive_bound(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
