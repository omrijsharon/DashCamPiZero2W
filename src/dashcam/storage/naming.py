"""Pure, Windows-safe dashcam clip filename generation and parsing."""

from __future__ import annotations

import re
from collections.abc import Set
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from uuid import UUID


class ClipNameError(ValueError):
    """Raised for an unsafe, malformed, or colliding clip name."""


_MAX_COMPONENT_LENGTH = 255
_BOOT_ID_RE = re.compile(r"[a-z0-9]{5,16}")
_PROVISIONAL_RE = re.compile(r"boot-([a-z0-9]{5,16})-(\d{6})\.partial\.(mp4|json)")
_FINAL_UNSYNCED_RE = re.compile(r"boot-([a-z0-9]{5,16})-(\d{6})\.(mp4|json)")
_FINAL_RE = re.compile(r"(\d{8}T\d{6}\.\d{3}Z)_([a-z0-9]{5,16})_s(\d{6})\.(mp4|json)")
_SAFE_COMPONENT_RE = re.compile(r"[A-Za-z0-9._-]+")
_RESERVED_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)


@dataclass(frozen=True, slots=True)
class ClipFilePair:
    """A deterministic MP4/JSON pair sharing one filename stem."""

    video_name: str
    metadata_name: str

    def __post_init__(self) -> None:
        validate_filename_component(self.video_name)
        validate_filename_component(self.metadata_name)
        if not self.video_name.endswith(".mp4") or not self.metadata_name.endswith(".json"):
            raise ClipNameError("a clip pair requires .mp4 and .json members")
        if self.video_name.removesuffix(".mp4") != self.metadata_name.removesuffix(".json"):
            raise ClipNameError("MP4 and JSON names must have the same stem")

    def relative_paths(self, directory: str) -> tuple[PurePosixPath, PurePosixPath]:
        """Map only an application-owned directory to safe relative paths."""

        if directory not in {"pending", "clips", "protected", "quarantine"}:
            raise ClipNameError("unrecognized application directory")
        return (
            PurePosixPath(directory, self.video_name),
            PurePosixPath(directory, self.metadata_name),
        )


@dataclass(frozen=True, slots=True)
class ParsedClipName:
    boot_id: str
    sequence: int
    utc_started_at: datetime | None
    extension: str
    provisional: bool
    partial: bool


def validate_filename_component(name: str) -> str:
    """Validate a single portable filename component without touching disk."""

    if not isinstance(name, str) or not name or len(name) > _MAX_COMPONENT_LENGTH:
        raise ClipNameError("filename must be a non-empty bounded string")
    if not name.isascii() or name != name.strip() or name.endswith((".", " ")):
        raise ClipNameError("filename must be ASCII and cannot end in a dot or space")
    if "/" in name or "\\" in name or not _SAFE_COMPONENT_RE.fullmatch(name):
        raise ClipNameError("filename contains a path separator or Windows-reserved character")
    device_stem = name.split(".", 1)[0].upper()
    if device_stem in _RESERVED_DEVICE_NAMES:
        raise ClipNameError("filename uses a reserved Windows device name")
    return name


def parse_clip_id(value: str) -> UUID:
    """Accept only a canonical UUID API identifier, never a filename or path."""

    if not isinstance(value, str):
        raise ClipNameError("clip ID must be a canonical UUID")
    try:
        clip_id = UUID(value)
    except ValueError as exc:
        raise ClipNameError("clip ID must be a canonical UUID") from exc
    if str(clip_id) != value:
        raise ClipNameError("clip ID must be canonical")
    return clip_id


def provisional_clip_pair(
    *, boot_id: str, sequence: int, existing_names: Set[str] = frozenset()
) -> ClipFilePair:
    """Return a collision-free, UTC-independent pending pair name."""

    stem = f"boot-{_validated_boot_id(boot_id)}-{_sequence(sequence):06d}.partial"
    return _new_pair(stem, existing_names)


def finalized_unsynced_clip_pair(
    *, boot_id: str, sequence: int, existing_names: Set[str] = frozenset()
) -> ClipFilePair:
    """Return a closed clip pair whose civil timestamp is not known yet."""

    stem = f"boot-{_validated_boot_id(boot_id)}-{_sequence(sequence):06d}"
    return _new_pair(stem, existing_names)


def finalized_clip_pair(
    *,
    utc_started_at: datetime,
    boot_id: str,
    sequence: int,
    existing_names: Set[str] = frozenset(),
) -> ClipFilePair:
    """Return a collision-free final pair name using canonical UTC milliseconds."""

    utc_value = _as_utc(utc_started_at)
    stem = (
        f"{utc_value:%Y%m%dT%H%M%S}.{utc_value.microsecond // 1000:03d}Z_"
        f"{_validated_boot_id(boot_id)}_s{_sequence(sequence):06d}"
    )
    return _new_pair(stem, existing_names)


def parse_clip_filename(name: str) -> ParsedClipName:
    """Parse a generated clip filename after strict single-component validation."""

    validate_filename_component(name)
    provisional_match = _PROVISIONAL_RE.fullmatch(name)
    if provisional_match is not None:
        boot_id, sequence_text, extension = provisional_match.groups()
        return ParsedClipName(
            boot_id=boot_id,
            sequence=int(sequence_text),
            utc_started_at=None,
            extension=extension,
            provisional=True,
            partial=True,
        )
    final_unsynced_match = _FINAL_UNSYNCED_RE.fullmatch(name)
    if final_unsynced_match is not None:
        boot_id, sequence_text, extension = final_unsynced_match.groups()
        return ParsedClipName(
            boot_id=boot_id,
            sequence=int(sequence_text),
            utc_started_at=None,
            extension=extension,
            provisional=True,
            partial=False,
        )
    final_match = _FINAL_RE.fullmatch(name)
    if final_match is None:
        raise ClipNameError("filename is not a dashcam clip name")
    timestamp_text, boot_id, sequence_text, extension = final_match.groups()
    try:
        utc_started_at = datetime.strptime(timestamp_text, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ClipNameError("filename has an invalid UTC timestamp") from exc
    return ParsedClipName(
        boot_id=boot_id,
        sequence=int(sequence_text),
        utc_started_at=utc_started_at,
        extension=extension,
        provisional=False,
        partial=False,
    )


def _new_pair(stem: str, existing_names: Set[str]) -> ClipFilePair:
    pair = ClipFilePair(video_name=f"{stem}.mp4", metadata_name=f"{stem}.json")
    existing_casefolded = {name.casefold() for name in existing_names}
    collides = (
        pair.video_name.casefold() in existing_casefolded
        or pair.metadata_name.casefold() in existing_casefolded
    )
    if collides:
        raise ClipNameError("refusing filename collision")
    return pair


def _validated_boot_id(boot_id: str) -> str:
    if not isinstance(boot_id, str) or _BOOT_ID_RE.fullmatch(boot_id) is None:
        raise ClipNameError("boot ID must be 5-16 lowercase ASCII alphanumeric characters")
    return boot_id


def _sequence(sequence: int) -> int:
    if isinstance(sequence, bool) or not isinstance(sequence, int) or not 0 <= sequence <= 999_999:
        raise ClipNameError("sequence must be an integer between 0 and 999999")
    return sequence


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ClipNameError("final timestamps must be timezone-aware")
    return value.astimezone(UTC)
