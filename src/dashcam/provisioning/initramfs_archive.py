"""Strict parsing and composition helpers for the pinned initramfs layout.

Raspberry Pi OS concatenates an uncompressed ``newc`` archive containing the
early firmware/module payload with a zstd-compressed main archive.  This module
does not decompress zstd.  It only establishes the exact, bounded boundary and
preserves the early archive byte-for-byte when a caller supplies a replacement
already-compressed main payload.
"""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final

NEWC_MAGIC: Final = b"070701"
NEWC_CRC_MAGIC: Final = b"070702"
NEWC_HEADER_SIZE: Final = 110
NEWC_TRAILER: Final = "TRAILER!!!"
ZSTD_MAGIC: Final = b"\x28\xb5\x2f\xfd"
EARLY_ARCHIVE_ALIGNMENT: Final = 512
MAX_ARCHIVE_BYTES: Final = 256 * 1024 * 1024
MAX_ENTRY_BYTES: Final = 128 * 1024 * 1024
MAX_NAME_BYTES: Final = 4096
MAX_ENTRIES: Final = 100_000


class InitramfsArchiveError(ValueError):
    """Raised when an initramfs or ``newc`` invariant is invalid."""


@dataclass(frozen=True, slots=True)
class NewcEntry:
    """One validated ``newc`` member."""

    name: str
    inode: int
    mode: int
    uid: int
    gid: int
    link_count: int
    mtime: int
    device_major: int
    device_minor: int
    rdevice_major: int
    rdevice_minor: int
    data: bytes
    header_offset: int
    end_offset: int


@dataclass(frozen=True, slots=True)
class NewcArchive:
    """A parsed ``newc`` archive through its required trailer."""

    entries: tuple[NewcEntry, ...]
    start_offset: int
    trailer_end_offset: int


@dataclass(frozen=True, slots=True)
class InitramfsParts:
    """The exact early bytes and opaque zstd main payload."""

    early_archive: NewcArchive
    early_bytes: bytes
    main_compressed: bytes
    main_offset: int


def parse_newc_archive(
    payload: bytes,
    *,
    offset: int = 0,
    max_archive_bytes: int = MAX_ARCHIVE_BYTES,
    max_entry_bytes: int = MAX_ENTRY_BYTES,
    max_name_bytes: int = MAX_NAME_BYTES,
    max_entries: int = MAX_ENTRIES,
) -> NewcArchive:
    """Parse one ``newc`` archive with strict bounds and safe member names."""

    _validate_limits(
        offset=offset,
        payload_size=len(payload),
        max_archive_bytes=max_archive_bytes,
        max_entry_bytes=max_entry_bytes,
        max_name_bytes=max_name_bytes,
        max_entries=max_entries,
    )
    archive_limit = min(len(payload), offset + max_archive_bytes)
    cursor = offset
    entries: list[NewcEntry] = []
    seen: set[str] = set()

    while True:
        header_end = _checked_add(cursor, NEWC_HEADER_SIZE, archive_limit, "truncated newc header")
        header = payload[cursor:header_end]
        magic = header[:6]
        if magic not in {NEWC_MAGIC, NEWC_CRC_MAGIC}:
            raise InitramfsArchiveError(f"invalid newc magic at offset {cursor}")
        fields = tuple(_parse_hex_field(header, 6 + index * 8, cursor) for index in range(13))
        inode, mode, uid, gid, link_count, mtime = fields[:6]
        file_size, name_size, checksum = fields[6], fields[11], fields[12]
        device_major, device_minor, rdevice_major, rdevice_minor = fields[7:11]
        if name_size < 2 or name_size > max_name_bytes:
            raise InitramfsArchiveError("newc member name length is outside the configured bound")
        if file_size > max_entry_bytes:
            raise InitramfsArchiveError("newc member data exceeds the configured bound")

        name_end = _checked_add(header_end, name_size, archive_limit, "truncated newc name")
        raw_name = payload[header_end:name_end]
        if not raw_name.endswith(b"\0") or b"\0" in raw_name[:-1]:
            raise InitramfsArchiveError("newc member name must have one trailing NUL")
        try:
            name = raw_name[:-1].decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise InitramfsArchiveError("newc member name is not valid UTF-8") from exc
        _validate_member_name(name)

        data_start = _align_up(name_end, 4)
        _require_zero_padding(payload, name_end, data_start, archive_limit)
        data_end = _checked_add(data_start, file_size, archive_limit, "truncated newc data")
        member_end = _align_up(data_end, 4)
        _require_zero_padding(payload, data_end, member_end, archive_limit)
        data = payload[data_start:data_end]
        if magic == NEWC_CRC_MAGIC and sum(data) & 0xFFFFFFFF != checksum:
            raise InitramfsArchiveError(f"newc CRC mismatch for {name!r}")

        if name == NEWC_TRAILER:
            if file_size != 0:
                raise InitramfsArchiveError("newc trailer must have an empty payload")
            return NewcArchive(tuple(entries), offset, member_end)
        if len(entries) >= max_entries:
            raise InitramfsArchiveError("newc entry count exceeds the configured bound")
        if name in seen:
            raise InitramfsArchiveError(f"duplicate newc member {name!r}")
        seen.add(name)
        entries.append(
            NewcEntry(
                name,
                inode,
                mode,
                uid,
                gid,
                link_count,
                mtime,
                device_major,
                device_minor,
                rdevice_major,
                rdevice_minor,
                data,
                cursor,
                member_end,
            )
        )
        cursor = member_end


def serialize_newc_archive(
    archive: NewcArchive,
    *,
    max_archive_bytes: int = MAX_ARCHIVE_BYTES,
    max_entry_bytes: int = MAX_ENTRY_BYTES,
    max_name_bytes: int = MAX_NAME_BYTES,
    max_entries: int = MAX_ENTRIES,
) -> bytes:
    """Serialize semantic entries as canonical ``070701`` with zero padding."""

    _validate_serialization_limits(
        max_archive_bytes=max_archive_bytes,
        max_entry_bytes=max_entry_bytes,
        max_name_bytes=max_name_bytes,
        max_entries=max_entries,
    )
    if len(archive.entries) > max_entries:
        raise InitramfsArchiveError("newc entry count exceeds the configured bound")
    chunks: list[bytes] = []
    output_size = 0
    seen: set[str] = set()
    for entry in archive.entries:
        _validate_member_name(entry.name)
        if entry.name == NEWC_TRAILER:
            raise InitramfsArchiveError("newc entries must not contain the trailer member")
        if entry.name in seen:
            raise InitramfsArchiveError(f"duplicate newc member {entry.name!r}")
        seen.add(entry.name)
        _validate_serializable_entry(entry, max_entry_bytes=max_entry_bytes)
        encoded = _serialize_entry(entry, max_name_bytes=max_name_bytes)
        output_size = _checked_serialized_size(
            output_size, len(encoded), max_archive_bytes=max_archive_bytes
        )
        chunks.append(encoded)

    trailer = _serialize_trailer()
    _checked_serialized_size(output_size, len(trailer), max_archive_bytes=max_archive_bytes)
    chunks.append(trailer)
    return b"".join(chunks)


def replace_newc_member(
    payload: bytes,
    *,
    target: str,
    replacement_data: bytes,
    max_archive_bytes: int = MAX_ARCHIVE_BYTES,
    max_entry_bytes: int = MAX_ENTRY_BYTES,
    max_name_bytes: int = MAX_NAME_BYTES,
    max_entries: int = MAX_ENTRIES,
) -> bytes:
    """Replace exactly one safe regular-file member in decompressed ``newc``."""

    _validate_member_name(target)
    if target in {".", NEWC_TRAILER}:
        raise InitramfsArchiveError("replacement target is reserved")
    if len(replacement_data) > max_entry_bytes:
        raise InitramfsArchiveError("replacement data exceeds the configured bound")
    archive = parse_newc_archive(
        payload,
        max_archive_bytes=max_archive_bytes,
        max_entry_bytes=max_entry_bytes,
        max_name_bytes=max_name_bytes,
        max_entries=max_entries,
    )
    matches = [index for index, entry in enumerate(archive.entries) if entry.name == target]
    if len(matches) != 1:
        raise InitramfsArchiveError(
            f"replacement target must exist exactly once; observed {len(matches)}"
        )
    index = matches[0]
    original = archive.entries[index]
    if stat.S_IFMT(original.mode) != stat.S_IFREG:
        raise InitramfsArchiveError("replacement target must be a regular file")
    if original.mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
        raise InitramfsArchiveError("replacement target has unsafe privilege mode bits")

    replaced = list(archive.entries)
    replaced[index] = NewcEntry(
        original.name,
        original.inode,
        original.mode,
        original.uid,
        original.gid,
        original.link_count,
        original.mtime,
        original.device_major,
        original.device_minor,
        original.rdevice_major,
        original.rdevice_minor,
        bytes(replacement_data),
        original.header_offset,
        original.end_offset,
    )
    return serialize_newc_archive(
        NewcArchive(tuple(replaced), 0, 0),
        max_archive_bytes=max_archive_bytes,
        max_entry_bytes=max_entry_bytes,
        max_name_bytes=max_name_bytes,
        max_entries=max_entries,
    )


def add_newc_regular_file(
    payload: bytes,
    *,
    target: str,
    data: bytes,
    mode: int,
    uid: int,
    gid: int,
    mtime: int,
    max_inode: int = 0xFFFFFFFF,
    max_archive_bytes: int = MAX_ARCHIVE_BYTES,
    max_entry_bytes: int = MAX_ENTRY_BYTES,
    max_name_bytes: int = MAX_NAME_BYTES,
    max_entries: int = MAX_ENTRIES,
) -> bytes:
    """Append one deterministic executable regular file before the trailer.

    Injected files always use link count one and device/rdevice tuple ``0:0``.
    The lowest unused positive inode on device ``0:0`` is selected so repeated
    builds from the same semantic archive produce identical bytes.
    """

    _validate_member_name(target)
    if target in {".", NEWC_TRAILER}:
        raise InitramfsArchiveError("new member target is reserved")
    encoded_name_size = len(target.encode("utf-8")) + 1
    if encoded_name_size > max_name_bytes:
        raise InitramfsArchiveError("newc member name length is outside the configured bound")
    allowed_modes = {stat.S_IFREG | 0o555, stat.S_IFREG | 0o755}
    if mode not in allowed_modes:
        raise InitramfsArchiveError(
            "new regular-file mode must be exactly 0555 or 0755 without privilege bits"
        )
    for label, value in (("uid", uid), ("gid", gid), ("mtime", mtime)):
        _require_uint32(value, f"newc {label}")
    if isinstance(max_inode, bool) or not 1 <= max_inode <= 0xFFFFFFFF:
        raise InitramfsArchiveError("maximum inode must be an unsigned positive 32-bit value")
    if len(data) > max_entry_bytes:
        raise InitramfsArchiveError("new regular-file data exceeds the configured bound")

    archive = parse_newc_archive(
        payload,
        max_archive_bytes=max_archive_bytes,
        max_entry_bytes=max_entry_bytes,
        max_name_bytes=max_name_bytes,
        max_entries=max_entries,
    )
    if any(entry.name == target for entry in archive.entries):
        raise InitramfsArchiveError(f"newc member {target!r} already exists")
    parent = str(PurePosixPath(target).parent)
    parents = [entry for entry in archive.entries if entry.name == parent]
    if len(parents) != 1:
        raise InitramfsArchiveError(
            f"new member parent must exist exactly once; observed {len(parents)}"
        )
    if stat.S_IFMT(parents[0].mode) != stat.S_IFDIR:
        raise InitramfsArchiveError("new member parent must be a directory")
    if len(archive.entries) >= max_entries:
        raise InitramfsArchiveError("newc entry count exceeds the configured bound")

    inode = _select_unused_inode(archive, max_inode=max_inode)
    added = NewcEntry(
        target,
        inode,
        mode,
        uid,
        gid,
        1,
        mtime,
        0,
        0,
        0,
        0,
        bytes(data),
        0,
        0,
    )
    return serialize_newc_archive(
        NewcArchive((*archive.entries, added), 0, 0),
        max_archive_bytes=max_archive_bytes,
        max_entry_bytes=max_entry_bytes,
        max_name_bytes=max_name_bytes,
        max_entries=max_entries,
    )


def add_newc_directory(
    payload: bytes,
    *,
    target: str,
    mode: int,
    uid: int,
    gid: int,
    mtime: int,
    max_inode: int = 0xFFFFFFFF,
    max_archive_bytes: int = MAX_ARCHIVE_BYTES,
    max_entry_bytes: int = MAX_ENTRY_BYTES,
    max_name_bytes: int = MAX_NAME_BYTES,
    max_entries: int = MAX_ENTRIES,
) -> bytes:
    """Append one deterministic empty directory before the trailer."""

    _validate_member_name(target)
    if target in {".", NEWC_TRAILER}:
        raise InitramfsArchiveError("new directory target is reserved")
    encoded_name_size = len(target.encode("utf-8")) + 1
    if encoded_name_size > max_name_bytes:
        raise InitramfsArchiveError("newc member name length is outside the configured bound")
    allowed_modes = {stat.S_IFDIR | 0o555, stat.S_IFDIR | 0o755}
    if mode not in allowed_modes:
        raise InitramfsArchiveError(
            "new directory mode must be exactly 0555 or 0755 without privilege bits"
        )
    for label, value in (("uid", uid), ("gid", gid), ("mtime", mtime)):
        _require_uint32(value, f"newc {label}")
    if isinstance(max_inode, bool) or not 1 <= max_inode <= 0xFFFFFFFF:
        raise InitramfsArchiveError("maximum inode must be an unsigned positive 32-bit value")

    archive = parse_newc_archive(
        payload,
        max_archive_bytes=max_archive_bytes,
        max_entry_bytes=max_entry_bytes,
        max_name_bytes=max_name_bytes,
        max_entries=max_entries,
    )
    if any(entry.name == target for entry in archive.entries):
        raise InitramfsArchiveError(f"newc member {target!r} already exists")
    parent = str(PurePosixPath(target).parent)
    parents = [entry for entry in archive.entries if entry.name == parent]
    if len(parents) != 1:
        raise InitramfsArchiveError(
            f"new directory parent must exist exactly once; observed {len(parents)}"
        )
    if stat.S_IFMT(parents[0].mode) != stat.S_IFDIR:
        raise InitramfsArchiveError("new directory parent must be a directory")
    if len(archive.entries) >= max_entries:
        raise InitramfsArchiveError("newc entry count exceeds the configured bound")

    added = NewcEntry(
        target,
        _select_unused_inode(archive, max_inode=max_inode),
        mode,
        uid,
        gid,
        1,
        mtime,
        0,
        0,
        0,
        0,
        b"",
        0,
        0,
    )
    return serialize_newc_archive(
        NewcArchive((*archive.entries, added), 0, 0),
        max_archive_bytes=max_archive_bytes,
        max_entry_bytes=max_entry_bytes,
        max_name_bytes=max_name_bytes,
        max_entries=max_entries,
    )


def split_initramfs(
    payload: bytes,
    *,
    early_alignment: int = EARLY_ARCHIVE_ALIGNMENT,
    max_archive_bytes: int = MAX_ARCHIVE_BYTES,
) -> InitramfsParts:
    """Split the exact early-newc + zstd-main layout without decompressing zstd."""

    if early_alignment != EARLY_ARCHIVE_ALIGNMENT:
        raise InitramfsArchiveError("the pinned initramfs requires 512-byte early alignment")
    early = parse_newc_archive(payload, max_archive_bytes=max_archive_bytes)
    main_offset = _align_up(early.trailer_end_offset, early_alignment)
    if main_offset > len(payload):
        raise InitramfsArchiveError("initramfs ends before the aligned main archive")
    _require_zero_padding(payload, early.trailer_end_offset, main_offset, len(payload))
    main = payload[main_offset:]
    _validate_zstd_main(main)
    return InitramfsParts(early, payload[:main_offset], main, main_offset)


def recombine_initramfs(parts: InitramfsParts, main_compressed: bytes) -> bytes:
    """Return the unchanged early bytes followed by a validated zstd payload."""

    if parts.main_offset != len(parts.early_bytes):
        raise InitramfsArchiveError("recorded main offset does not match the early byte length")
    if parts.main_offset % EARLY_ARCHIVE_ALIGNMENT:
        raise InitramfsArchiveError("recorded main offset is not 512-byte aligned")
    if parts.early_archive.start_offset != 0:
        raise InitramfsArchiveError("early archive must start at byte zero")
    if parts.early_archive.trailer_end_offset > parts.main_offset:
        raise InitramfsArchiveError("early archive trailer extends past the main offset")
    _require_zero_padding(
        parts.early_bytes,
        parts.early_archive.trailer_end_offset,
        parts.main_offset,
        len(parts.early_bytes),
    )
    _validate_zstd_main(main_compressed)
    if len(parts.early_bytes) + len(main_compressed) > MAX_ARCHIVE_BYTES:
        raise InitramfsArchiveError("recombined initramfs exceeds the configured bound")
    return parts.early_bytes + main_compressed


def _validate_limits(
    *,
    offset: int,
    payload_size: int,
    max_archive_bytes: int,
    max_entry_bytes: int,
    max_name_bytes: int,
    max_entries: int,
) -> None:
    if offset < 0 or offset >= payload_size:
        raise InitramfsArchiveError("newc offset is outside the payload")
    if not 1 <= max_archive_bytes <= MAX_ARCHIVE_BYTES:
        raise InitramfsArchiveError("invalid newc archive-size bound")
    if not 0 <= max_entry_bytes <= MAX_ENTRY_BYTES:
        raise InitramfsArchiveError("invalid newc entry-size bound")
    if not 2 <= max_name_bytes <= MAX_NAME_BYTES:
        raise InitramfsArchiveError("invalid newc name-size bound")
    if not 1 <= max_entries <= MAX_ENTRIES:
        raise InitramfsArchiveError("invalid newc entry-count bound")


def _validate_serialization_limits(
    *,
    max_archive_bytes: int,
    max_entry_bytes: int,
    max_name_bytes: int,
    max_entries: int,
) -> None:
    if not 1 <= max_archive_bytes <= MAX_ARCHIVE_BYTES:
        raise InitramfsArchiveError("invalid newc archive-size bound")
    if not 0 <= max_entry_bytes <= MAX_ENTRY_BYTES:
        raise InitramfsArchiveError("invalid newc entry-size bound")
    if not 2 <= max_name_bytes <= MAX_NAME_BYTES:
        raise InitramfsArchiveError("invalid newc name-size bound")
    if not 1 <= max_entries <= MAX_ENTRIES:
        raise InitramfsArchiveError("invalid newc entry-count bound")


def _validate_serializable_entry(entry: NewcEntry, *, max_entry_bytes: int) -> None:
    for label, value in (
        ("inode", entry.inode),
        ("mode", entry.mode),
        ("uid", entry.uid),
        ("gid", entry.gid),
        ("link count", entry.link_count),
        ("mtime", entry.mtime),
        ("device major", entry.device_major),
        ("device minor", entry.device_minor),
        ("rdevice major", entry.rdevice_major),
        ("rdevice minor", entry.rdevice_minor),
    ):
        if isinstance(value, bool) or not 0 <= value <= 0xFFFFFFFF:
            raise InitramfsArchiveError(f"newc {label} is outside the unsigned 32-bit range")
    if entry.link_count == 0:
        raise InitramfsArchiveError("newc link count must be positive")
    if len(entry.data) > max_entry_bytes:
        raise InitramfsArchiveError("newc member data exceeds the configured bound")
    file_type = stat.S_IFMT(entry.mode)
    allowed_types = {
        stat.S_IFREG,
        stat.S_IFDIR,
        stat.S_IFLNK,
        stat.S_IFCHR,
        stat.S_IFBLK,
        stat.S_IFIFO,
        stat.S_IFSOCK,
    }
    if file_type not in allowed_types:
        raise InitramfsArchiveError(f"newc member {entry.name!r} has an unsafe file type")
    if file_type not in {stat.S_IFREG, stat.S_IFLNK} and entry.data:
        raise InitramfsArchiveError(
            f"newc special member {entry.name!r} must have an empty payload"
        )


def _require_uint32(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFFFFFFFF:
        raise InitramfsArchiveError(f"{label} is outside the unsigned 32-bit range")


def _select_unused_inode(archive: NewcArchive, *, max_inode: int) -> int:
    used_inodes = {
        entry.inode
        for entry in archive.entries
        if entry.device_major == 0 and entry.device_minor == 0 and entry.inode > 0
    }
    inode = next(
        (candidate for candidate in range(1, max_inode + 1) if candidate not in used_inodes),
        None,
    )
    if inode is None:
        raise InitramfsArchiveError("no unused positive inode remains on device 0:0")
    return inode


def _serialize_entry(entry: NewcEntry, *, max_name_bytes: int) -> bytes:
    encoded_name = entry.name.encode("utf-8") + b"\0"
    if len(encoded_name) > max_name_bytes:
        raise InitramfsArchiveError("newc member name length is outside the configured bound")
    fields = (
        entry.inode,
        entry.mode,
        entry.uid,
        entry.gid,
        entry.link_count,
        entry.mtime,
        len(entry.data),
        entry.device_major,
        entry.device_minor,
        entry.rdevice_major,
        entry.rdevice_minor,
        len(encoded_name),
        0,
    )
    header = NEWC_MAGIC + b"".join(f"{value:08x}".encode("ascii") for value in fields)
    name_section = header + encoded_name
    return (
        name_section
        + b"\0" * (-len(name_section) % 4)
        + entry.data
        + b"\0" * (-len(entry.data) % 4)
    )


def _serialize_trailer() -> bytes:
    trailer = NewcEntry(
        NEWC_TRAILER,
        0,
        0,
        0,
        0,
        1,
        0,
        0,
        0,
        0,
        0,
        b"",
        0,
        0,
    )
    return _serialize_entry(trailer, max_name_bytes=MAX_NAME_BYTES)


def _checked_serialized_size(current: int, added: int, *, max_archive_bytes: int) -> int:
    result = current + added
    if result < current or result > max_archive_bytes:
        raise InitramfsArchiveError("serialized newc archive exceeds the configured bound")
    return result


def _parse_hex_field(header: bytes, start: int, header_offset: int) -> int:
    raw = header[start : start + 8]
    try:
        text = raw.decode("ascii", errors="strict")
        if any(character not in "0123456789abcdefABCDEF" for character in text):
            raise ValueError
        return int(text, 16)
    except (UnicodeDecodeError, ValueError) as exc:
        raise InitramfsArchiveError(
            f"invalid newc hexadecimal field at offset {header_offset + start}"
        ) from exc


def _validate_member_name(name: str) -> None:
    # GNU cpio conventionally emits one explicit archive-root directory.  It
    # is safe as metadata, but callers must never interpret it as an
    # extraction target outside the selected destination.
    if name == ".":
        return
    if not name or name.startswith("/") or "\\" in name:
        raise InitramfsArchiveError(f"unsafe newc member name {name!r}")
    if any(part in {"", ".", ".."} for part in name.split("/")):
        raise InitramfsArchiveError(f"unsafe newc member name {name!r}")
    path = PurePosixPath(name)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise InitramfsArchiveError(f"unsafe newc member name {name!r}")


def _checked_add(start: int, size: int, limit: int, message: str) -> int:
    end = start + size
    if end < start or end > limit:
        raise InitramfsArchiveError(message)
    return end


def _require_zero_padding(payload: bytes, start: int, end: int, limit: int) -> None:
    if start < 0 or end < start or end > limit:
        raise InitramfsArchiveError("truncated newc padding")
    if any(payload[start:end]):
        raise InitramfsArchiveError("newc padding must contain only zero bytes")


def _align_up(value: int, alignment: int) -> int:
    return ((value + alignment - 1) // alignment) * alignment


def _validate_zstd_main(payload: bytes) -> None:
    if len(payload) < len(ZSTD_MAGIC) or not payload.startswith(ZSTD_MAGIC):
        raise InitramfsArchiveError("main initramfs payload does not start with zstd magic")
