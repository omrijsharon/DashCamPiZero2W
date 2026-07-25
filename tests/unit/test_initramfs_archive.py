from __future__ import annotations

import stat
from dataclasses import replace

import pytest

from dashcam.provisioning.initramfs_archive import (
    EARLY_ARCHIVE_ALIGNMENT,
    NEWC_CRC_MAGIC,
    NEWC_MAGIC,
    ZSTD_MAGIC,
    InitramfsArchiveError,
    NewcArchive,
    NewcEntry,
    add_newc_directory,
    add_newc_regular_file,
    parse_newc_archive,
    recombine_initramfs,
    replace_newc_member,
    serialize_newc_archive,
    split_initramfs,
)


def _align(payload: bytes, boundary: int) -> bytes:
    return payload + b"\0" * (-len(payload) % boundary)


def _entry(
    name: str,
    data: bytes = b"",
    *,
    magic: bytes = NEWC_MAGIC,
    checksum: int | None = None,
    mode: int = 0o100644,
) -> bytes:
    encoded_name = name.encode("utf-8") + b"\0"
    actual_checksum = sum(data) & 0xFFFFFFFF if checksum is None else checksum
    values = (
        1,
        mode,
        0,
        0,
        1,
        1_700_000_000,
        len(data),
        0,
        0,
        0,
        0,
        len(encoded_name),
        actual_checksum if magic == NEWC_CRC_MAGIC else 0,
    )
    header = magic + b"".join(f"{value:08x}".encode("ascii") for value in values)
    return _align(header + encoded_name, 4) + _align(data, 4)


def _archive(*entries: bytes) -> bytes:
    return b"".join(entries) + _entry("TRAILER!!!")


def _initramfs() -> bytes:
    early = _align(
        _archive(
            _entry("kernel", mode=0o040755),
            _entry("kernel/module.ko", b"arm-module"),
        ),
        EARLY_ARCHIVE_ALIGNMENT,
    )
    return early + ZSTD_MAGIC + b"opaque-compressed-main"


def test_parse_newc_returns_validated_entries_and_offsets() -> None:
    payload = _archive(
        _entry(".", mode=0o040755),
        _entry("etc", mode=0o040755),
        _entry("etc/config", b"value"),
    )

    parsed = parse_newc_archive(payload)

    assert [entry.name for entry in parsed.entries] == [".", "etc", "etc/config"]
    assert parsed.entries[2].data == b"value"
    assert parsed.entries[2].mode == 0o100644
    assert parsed.start_offset == 0
    assert parsed.trailer_end_offset == len(payload)


def test_crc_newc_member_is_checked() -> None:
    valid = _archive(_entry("file", b"abc", magic=NEWC_CRC_MAGIC))
    assert parse_newc_archive(valid).entries[0].data == b"abc"

    invalid = _archive(_entry("file", b"abc", magic=NEWC_CRC_MAGIC, checksum=1))
    with pytest.raises(InitramfsArchiveError, match="CRC mismatch"):
        parse_newc_archive(invalid)


@pytest.mark.parametrize(
    "name",
    [
        "/absolute",
        "../escape",
        "safe/../../escape",
        "./relative",
        "windows\\escape",
    ],
)
def test_parser_refuses_unsafe_member_paths(name: str) -> None:
    with pytest.raises(InitramfsArchiveError, match="unsafe newc member"):
        parse_newc_archive(_archive(_entry(name)))


def test_parser_refuses_duplicate_members() -> None:
    with pytest.raises(InitramfsArchiveError, match="duplicate newc member"):
        parse_newc_archive(_archive(_entry("same"), _entry("same")))


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"not-newc", "truncated newc header"),
        (_archive(_entry("file"))[:-1], "truncated newc"),
        (_entry("TRAILER!!!", b"not-empty"), "trailer must have an empty payload"),
    ],
)
def test_parser_refuses_malformed_or_truncated_archives(payload: bytes, message: str) -> None:
    with pytest.raises(InitramfsArchiveError, match=message):
        parse_newc_archive(payload)


def test_parser_enforces_entry_name_count_and_archive_bounds() -> None:
    payload = _archive(_entry("first", b"1234"), _entry("second"))

    assert len(parse_newc_archive(_archive(_entry("only")), max_entries=1).entries) == 1
    with pytest.raises(InitramfsArchiveError, match="data exceeds"):
        parse_newc_archive(payload, max_entry_bytes=3)
    with pytest.raises(InitramfsArchiveError, match="name length"):
        parse_newc_archive(payload, max_name_bytes=5)
    with pytest.raises(InitramfsArchiveError, match="entry count"):
        parse_newc_archive(payload, max_entries=1)
    with pytest.raises(InitramfsArchiveError, match="truncated"):
        parse_newc_archive(payload, max_archive_bytes=len(payload) - 1)


def test_split_and_recombine_round_trip_preserves_early_bytes_exactly() -> None:
    payload = _initramfs()

    parts = split_initramfs(payload)
    rebuilt = recombine_initramfs(parts, parts.main_compressed)

    assert parts.main_offset == len(parts.early_bytes)
    assert parts.main_offset % EARLY_ARCHIVE_ALIGNMENT == 0
    assert parts.main_compressed.startswith(ZSTD_MAGIC)
    assert rebuilt == payload


def test_recombine_accepts_an_already_compressed_replacement() -> None:
    parts = split_initramfs(_initramfs())
    replacement = ZSTD_MAGIC + b"replacement-frame"

    rebuilt = recombine_initramfs(parts, replacement)

    assert rebuilt[: parts.main_offset] == parts.early_bytes
    assert rebuilt[parts.main_offset :] == replacement


def test_split_refuses_nonzero_early_padding_and_non_zstd_main() -> None:
    payload = bytearray(_initramfs())
    parsed = parse_newc_archive(bytes(payload))
    payload[parsed.trailer_end_offset] = 1
    with pytest.raises(InitramfsArchiveError, match="padding"):
        split_initramfs(bytes(payload))

    early = _align(_archive(_entry("file")), EARLY_ARCHIVE_ALIGNMENT)
    with pytest.raises(InitramfsArchiveError, match="zstd magic"):
        split_initramfs(early + b"not-zstd")


def test_recombine_refuses_tampered_parts_and_oversize_output() -> None:
    parts = split_initramfs(_initramfs())
    with pytest.raises(InitramfsArchiveError, match="main offset"):
        recombine_initramfs(replace(parts, main_offset=parts.main_offset + 1), ZSTD_MAGIC)
    with pytest.raises(InitramfsArchiveError, match="zstd magic"):
        recombine_initramfs(parts, b"not-zstd")


def _semantic(entry: NewcEntry) -> tuple[object, ...]:
    return (
        entry.name,
        entry.inode,
        entry.mode,
        entry.uid,
        entry.gid,
        entry.link_count,
        entry.mtime,
        entry.device_major,
        entry.device_minor,
        entry.rdevice_major,
        entry.rdevice_minor,
        entry.data,
    )


def test_canonical_serialization_preserves_semantic_entries_and_order() -> None:
    original = parse_newc_archive(
        _archive(
            _entry(".", mode=stat.S_IFDIR | 0o755),
            _entry("etc", mode=stat.S_IFDIR | 0o755),
            _entry("etc/config", b"value", mode=stat.S_IFREG | 0o640),
            _entry("link", b"etc/config", mode=stat.S_IFLNK | 0o777),
        )
    )

    serialized = serialize_newc_archive(original)
    reparsed = parse_newc_archive(serialized)

    assert [_semantic(entry) for entry in reparsed.entries] == [
        _semantic(entry) for entry in original.entries
    ]
    assert serialized.startswith(NEWC_MAGIC)
    assert serialized.endswith(b"\0" * (-len(_entry("TRAILER!!!")) % 4))


def test_replacement_changes_only_exact_member_data() -> None:
    original_payload = _archive(
        _entry("scripts", mode=stat.S_IFDIR | 0o755),
        _entry("scripts/target", b"old", mode=stat.S_IFREG | 0o755),
        _entry("scripts/other", b"unchanged", mode=stat.S_IFREG | 0o644),
    )
    before = parse_newc_archive(original_payload)

    rebuilt = replace_newc_member(
        original_payload,
        target="scripts/target",
        replacement_data=b"new deterministic content\n",
    )
    after = parse_newc_archive(rebuilt)

    assert [entry.name for entry in after.entries] == [entry.name for entry in before.entries]
    for old, new in zip(before.entries, after.entries, strict=True):
        old_semantic = _semantic(old)
        new_semantic = _semantic(new)
        if old.name == "scripts/target":
            assert new.data == b"new deterministic content\n"
            assert new_semantic[:-1] == old_semantic[:-1]
        else:
            assert new_semantic == old_semantic


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        (stat.S_IFDIR | 0o755, "regular file"),
        (stat.S_IFLNK | 0o777, "regular file"),
        (stat.S_IFCHR | 0o600, "regular file"),
        (stat.S_IFBLK | 0o600, "regular file"),
        (stat.S_IFIFO | 0o600, "regular file"),
        (stat.S_IFREG | stat.S_ISUID | 0o755, "unsafe privilege"),
        (stat.S_IFREG | stat.S_ISGID | 0o755, "unsafe privilege"),
        (stat.S_IFREG | stat.S_ISVTX | 0o755, "unsafe privilege"),
    ],
)
def test_replacement_refuses_special_or_unsafe_targets(mode: int, message: str) -> None:
    payload = _archive(_entry("target", b"old", mode=mode))
    with pytest.raises(InitramfsArchiveError, match=message):
        replace_newc_member(payload, target="target", replacement_data=b"new")


@pytest.mark.parametrize("target", [".", "TRAILER!!!", "../escape", "/absolute"])
def test_replacement_refuses_reserved_or_unsafe_target_names(target: str) -> None:
    with pytest.raises(InitramfsArchiveError):
        replace_newc_member(
            _archive(_entry("safe", b"old")),
            target=target,
            replacement_data=b"new",
        )


def test_replacement_requires_exactly_one_existing_target() -> None:
    with pytest.raises(InitramfsArchiveError, match="observed 0"):
        replace_newc_member(
            _archive(_entry("other", b"old")),
            target="missing",
            replacement_data=b"new",
        )
    duplicate = _archive(_entry("target", b"one"), _entry("target", b"two"))
    with pytest.raises(InitramfsArchiveError, match="duplicate"):
        replace_newc_member(duplicate, target="target", replacement_data=b"new")


def test_serialization_refuses_duplicate_unsafe_type_and_output_bounds() -> None:
    parsed = parse_newc_archive(_archive(_entry("safe", b"data")))
    entry = parsed.entries[0]
    duplicate = NewcArchive((entry, entry), 0, 0)
    with pytest.raises(InitramfsArchiveError, match="duplicate"):
        serialize_newc_archive(duplicate)

    unsafe = replace(entry, mode=0o755)
    with pytest.raises(InitramfsArchiveError, match="unsafe file type"):
        serialize_newc_archive(NewcArchive((unsafe,), 0, 0))

    with pytest.raises(InitramfsArchiveError, match="exceeds"):
        serialize_newc_archive(parsed, max_archive_bytes=120)
    with pytest.raises(InitramfsArchiveError, match="replacement data exceeds"):
        replace_newc_member(
            _archive(_entry("safe", b"old")),
            target="safe",
            replacement_data=b"12345",
            max_entry_bytes=4,
        )


def _archive_with_sbin_parent() -> bytes:
    return _archive(
        _entry(".", mode=stat.S_IFDIR | 0o755),
        _entry("usr", mode=stat.S_IFDIR | 0o755),
        _entry("usr/sbin", mode=stat.S_IFDIR | 0o755),
        _entry("usr/sbin/existing", b"old", mode=stat.S_IFREG | 0o755),
    )


def test_add_regular_file_appends_exact_member_and_preserves_old_semantics() -> None:
    payload = _archive_with_sbin_parent()
    before = parse_newc_archive(payload)

    rebuilt = add_newc_regular_file(
        payload,
        target="usr/sbin/resize2fs",
        data=b"armhf executable",
        mode=stat.S_IFREG | 0o755,
        uid=0,
        gid=0,
        mtime=1_753_305_600,
    )
    after = parse_newc_archive(rebuilt)

    assert [_semantic(entry) for entry in after.entries[:-1]] == [
        _semantic(entry) for entry in before.entries
    ]
    added = after.entries[-1]
    assert added.name == "usr/sbin/resize2fs"
    assert added.data == b"armhf executable"
    assert added.mode == stat.S_IFREG | 0o755
    assert (added.uid, added.gid, added.mtime, added.link_count) == (
        0,
        0,
        1_753_305_600,
        1,
    )
    assert (
        added.device_major,
        added.device_minor,
        added.rdevice_major,
        added.rdevice_minor,
    ) == (0, 0, 0, 0)
    used = {
        entry.inode
        for entry in before.entries
        if entry.device_major == 0 and entry.device_minor == 0 and entry.inode > 0
    }
    assert added.inode == min(candidate for candidate in range(1, 10) if candidate not in used)


@pytest.mark.parametrize("target", [".", "TRAILER!!!", "../escape", "/absolute", "a//b"])
def test_add_regular_file_refuses_reserved_or_unsafe_paths(target: str) -> None:
    with pytest.raises(InitramfsArchiveError):
        add_newc_regular_file(
            _archive_with_sbin_parent(),
            target=target,
            data=b"x",
            mode=stat.S_IFREG | 0o755,
            uid=0,
            gid=0,
            mtime=0,
        )


def test_add_regular_file_refuses_existing_missing_or_nondirectory_parent() -> None:
    with pytest.raises(InitramfsArchiveError, match="already exists"):
        add_newc_regular_file(
            _archive_with_sbin_parent(),
            target="usr/sbin/existing",
            data=b"x",
            mode=stat.S_IFREG | 0o755,
            uid=0,
            gid=0,
            mtime=0,
        )
    with pytest.raises(InitramfsArchiveError, match="observed 0"):
        add_newc_regular_file(
            _archive_with_sbin_parent(),
            target="missing/file",
            data=b"x",
            mode=stat.S_IFREG | 0o755,
            uid=0,
            gid=0,
            mtime=0,
        )
    nondirectory = _archive(
        _entry(".", mode=stat.S_IFDIR | 0o755),
        _entry("usr", mode=stat.S_IFREG | 0o755),
    )
    with pytest.raises(InitramfsArchiveError, match="parent must be a directory"):
        add_newc_regular_file(
            nondirectory,
            target="usr/file",
            data=b"x",
            mode=stat.S_IFREG | 0o755,
            uid=0,
            gid=0,
            mtime=0,
        )


@pytest.mark.parametrize(
    "mode",
    [
        stat.S_IFREG | 0o555 | stat.S_ISUID,
        stat.S_IFREG | 0o775,
        stat.S_IFREG | 0o644,
        stat.S_IFDIR | 0o755,
        0o755,
    ],
)
def test_add_regular_file_refuses_noncanonical_mode(mode: int) -> None:
    with pytest.raises(InitramfsArchiveError, match="exactly 0555 or 0755"):
        add_newc_regular_file(
            _archive_with_sbin_parent(),
            target="usr/sbin/resize2fs",
            data=b"x",
            mode=mode,
            uid=0,
            gid=0,
            mtime=0,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("uid", -1),
        ("uid", 0x1_0000_0000),
        ("gid", True),
        ("gid", -1),
        ("mtime", -1),
        ("mtime", 0x1_0000_0000),
    ],
)
def test_add_regular_file_refuses_invalid_explicit_ids_and_time(field: str, value: int) -> None:
    arguments = {"uid": 0, "gid": 0, "mtime": 0}
    arguments[field] = value
    with pytest.raises(InitramfsArchiveError, match="unsigned 32-bit"):
        add_newc_regular_file(
            _archive_with_sbin_parent(),
            target="usr/sbin/resize2fs",
            data=b"x",
            mode=stat.S_IFREG | 0o755,
            **arguments,
        )


def test_add_regular_file_refuses_inode_exhaustion_entry_and_output_bounds() -> None:
    payload = _archive(
        _entry(".", mode=stat.S_IFDIR | 0o755),
        _entry("usr", mode=stat.S_IFDIR | 0o755),
    )
    with pytest.raises(InitramfsArchiveError, match="no unused positive inode"):
        add_newc_regular_file(
            payload,
            target="usr/file",
            data=b"x",
            mode=stat.S_IFREG | 0o555,
            uid=0,
            gid=0,
            mtime=0,
            max_inode=1,
        )
    with pytest.raises(InitramfsArchiveError, match="data exceeds"):
        add_newc_regular_file(
            payload,
            target="usr/file",
            data=b"12345",
            mode=stat.S_IFREG | 0o555,
            uid=0,
            gid=0,
            mtime=0,
            max_entry_bytes=4,
        )
    with pytest.raises(InitramfsArchiveError, match="serialized newc archive exceeds"):
        add_newc_regular_file(
            payload,
            target="usr/file",
            data=b"x",
            mode=stat.S_IFREG | 0o555,
            uid=0,
            gid=0,
            mtime=0,
            max_archive_bytes=len(payload) + 1,
        )


def _archive_with_etc_parent() -> bytes:
    return _archive(
        _entry(".", mode=stat.S_IFDIR | 0o755),
        _entry("etc", mode=stat.S_IFDIR | 0o755),
        _entry("etc/existing", mode=stat.S_IFDIR | 0o755),
    )


def test_add_directory_appends_exact_empty_member_and_preserves_old_entries() -> None:
    payload = _archive_with_etc_parent()
    before = parse_newc_archive(payload)

    rebuilt = add_newc_directory(
        payload,
        target="etc/dashcam",
        mode=stat.S_IFDIR | 0o755,
        uid=0,
        gid=0,
        mtime=1_753_305_600,
    )
    after = parse_newc_archive(rebuilt)

    assert [_semantic(entry) for entry in after.entries[:-1]] == [
        _semantic(entry) for entry in before.entries
    ]
    added = after.entries[-1]
    assert added.name == "etc/dashcam"
    assert added.data == b""
    assert added.mode == stat.S_IFDIR | 0o755
    assert (added.uid, added.gid, added.mtime, added.link_count) == (
        0,
        0,
        1_753_305_600,
        1,
    )
    assert (
        added.device_major,
        added.device_minor,
        added.rdevice_major,
        added.rdevice_minor,
    ) == (0, 0, 0, 0)
    assert added.inode > 0
    assert added.inode not in {
        entry.inode
        for entry in before.entries
        if entry.device_major == 0 and entry.device_minor == 0 and entry.inode > 0
    }


@pytest.mark.parametrize("target", [".", "TRAILER!!!", "../escape", "/absolute", "a//b"])
def test_add_directory_refuses_reserved_or_unsafe_paths(target: str) -> None:
    with pytest.raises(InitramfsArchiveError):
        add_newc_directory(
            _archive_with_etc_parent(),
            target=target,
            mode=stat.S_IFDIR | 0o755,
            uid=0,
            gid=0,
            mtime=0,
        )


def test_add_directory_refuses_existing_missing_or_nondirectory_parent() -> None:
    with pytest.raises(InitramfsArchiveError, match="already exists"):
        add_newc_directory(
            _archive_with_etc_parent(),
            target="etc/existing",
            mode=stat.S_IFDIR | 0o755,
            uid=0,
            gid=0,
            mtime=0,
        )
    with pytest.raises(InitramfsArchiveError, match="observed 0"):
        add_newc_directory(
            _archive_with_etc_parent(),
            target="missing/dashcam",
            mode=stat.S_IFDIR | 0o755,
            uid=0,
            gid=0,
            mtime=0,
        )
    nondirectory = _archive(
        _entry(".", mode=stat.S_IFDIR | 0o755),
        _entry("etc", mode=stat.S_IFREG | 0o755),
    )
    with pytest.raises(InitramfsArchiveError, match="parent must be a directory"):
        add_newc_directory(
            nondirectory,
            target="etc/dashcam",
            mode=stat.S_IFDIR | 0o755,
            uid=0,
            gid=0,
            mtime=0,
        )


@pytest.mark.parametrize(
    "mode",
    [
        stat.S_IFDIR | 0o755 | stat.S_ISUID,
        stat.S_IFDIR | 0o775,
        stat.S_IFDIR | 0o700,
        stat.S_IFREG | 0o755,
        0o755,
    ],
)
def test_add_directory_refuses_noncanonical_mode(mode: int) -> None:
    with pytest.raises(InitramfsArchiveError, match="exactly 0555 or 0755"):
        add_newc_directory(
            _archive_with_etc_parent(),
            target="etc/dashcam",
            mode=mode,
            uid=0,
            gid=0,
            mtime=0,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("uid", -1),
        ("uid", 0x1_0000_0000),
        ("gid", True),
        ("mtime", -1),
    ],
)
def test_add_directory_refuses_invalid_ids_and_time(field: str, value: int) -> None:
    arguments = {"uid": 0, "gid": 0, "mtime": 0}
    arguments[field] = value
    with pytest.raises(InitramfsArchiveError, match="unsigned 32-bit"):
        add_newc_directory(
            _archive_with_etc_parent(),
            target="etc/dashcam",
            mode=stat.S_IFDIR | 0o555,
            **arguments,
        )


def test_add_directory_refuses_inode_entry_name_and_output_bounds() -> None:
    payload = _archive_with_etc_parent()
    with pytest.raises(InitramfsArchiveError, match="no unused positive inode"):
        add_newc_directory(
            payload,
            target="etc/dashcam",
            mode=stat.S_IFDIR | 0o555,
            uid=0,
            gid=0,
            mtime=0,
            max_inode=1,
        )
    with pytest.raises(InitramfsArchiveError, match="entry count"):
        add_newc_directory(
            payload,
            target="etc/dashcam",
            mode=stat.S_IFDIR | 0o555,
            uid=0,
            gid=0,
            mtime=0,
            max_entries=3,
        )
    with pytest.raises(InitramfsArchiveError, match="name length"):
        add_newc_directory(
            payload,
            target="etc/dashcam",
            mode=stat.S_IFDIR | 0o555,
            uid=0,
            gid=0,
            mtime=0,
            max_name_bytes=10,
        )
    with pytest.raises(InitramfsArchiveError, match="serialized newc archive exceeds"):
        add_newc_directory(
            payload,
            target="etc/dashcam",
            mode=stat.S_IFDIR | 0o555,
            uid=0,
            gid=0,
            mtime=0,
            max_archive_bytes=len(payload) + 1,
        )
