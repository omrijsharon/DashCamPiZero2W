"""Application and build identity reporting."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Final

_DISTRIBUTION_NAME: Final = "dashcam-pizero2w"
_FALLBACK_VERSION: Final = "0+unknown"
_IDENTIFIER_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}")


@dataclass(frozen=True, slots=True)
class BuildInfo:
    """Non-secret identity fields suitable for status and diagnostics."""

    version: str
    build_id: str
    git_commit: str | None

    def as_dict(self) -> dict[str, str | None]:
        """Return a JSON-serializable representation."""

        return {
            "version": self.version,
            "build_id": self.build_id,
            "git_commit": self.git_commit,
        }


def get_version() -> str:
    """Return the installed distribution version."""

    try:
        return version(_DISTRIBUTION_NAME)
    except PackageNotFoundError:
        return _FALLBACK_VERSION


def _read_identifier(name: str, default: str | None) -> str | None:
    value = os.getenv(name)
    if value is None:
        return default

    value = value.strip()
    if not _IDENTIFIER_PATTERN.fullmatch(value):
        return default
    return value


def get_build_info() -> BuildInfo:
    """Return bounded build metadata supplied by the release environment."""

    package_version = get_version()
    return BuildInfo(
        version=package_version,
        build_id=_read_identifier("DASHCAM_BUILD_ID", package_version) or package_version,
        git_commit=_read_identifier("DASHCAM_GIT_COMMIT", None),
    )
