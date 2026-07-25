from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from dashcam.storage.naming import (
    ClipNameError,
    finalized_clip_pair,
    parse_clip_filename,
    parse_clip_id,
    provisional_clip_pair,
    validate_filename_component,
)


def test_provisional_pair_is_windows_safe_and_deterministic() -> None:
    pair = provisional_clip_pair(boot_id="ba1b2c3d4", sequence=123)

    assert pair.video_name == "boot-ba1b2c3d4-000123.partial.mp4"
    assert pair.metadata_name == "boot-ba1b2c3d4-000123.partial.json"
    assert (
        pair.relative_paths("pending")[0].as_posix() == "pending/boot-ba1b2c3d4-000123.partial.mp4"
    )


def test_final_name_is_utc_and_independent_of_local_dst() -> None:
    local_instant = datetime(2026, 7, 1, 3, 4, 5, 123456, tzinfo=ZoneInfo("Asia/Jerusalem"))

    pair = finalized_clip_pair(utc_started_at=local_instant, boot_id="ba1b2c3d4", sequence=123)
    parsed = parse_clip_filename(pair.video_name)

    assert pair.video_name == "20260701T000405.123Z_ba1b2c3d4_s000123.mp4"
    assert parsed.utc_started_at == datetime(2026, 7, 1, 0, 4, 5, 123000, tzinfo=UTC)
    assert parsed.provisional is False


def test_pairing_and_collision_refusal_cover_both_members() -> None:
    pair = finalized_clip_pair(
        utc_started_at=datetime(2026, 1, 1, tzinfo=UTC), boot_id="ba1b2c3d4", sequence=1
    )

    assert pair.video_name.removesuffix(".mp4") == pair.metadata_name.removesuffix(".json")
    with pytest.raises(ClipNameError, match="collision"):
        finalized_clip_pair(
            utc_started_at=datetime(2026, 1, 1, tzinfo=UTC),
            boot_id="ba1b2c3d4",
            sequence=1,
            existing_names={pair.metadata_name.upper()},
        )


@pytest.mark.parametrize(
    "name",
    [
        "CON.mp4",
        "aux.json",
        "LPT1.txt",
        "clip:bad.mp4",
        "clip?.mp4",
        "clip<bad>.mp4",
        "clip. ",
        "../clip.mp4",
        "subdir/clip.mp4",
        "subdir\\clip.mp4",
    ],
)
def test_windows_reserved_characters_and_traversal_are_refused(name: str) -> None:
    with pytest.raises(ClipNameError):
        validate_filename_component(name)


def test_clip_id_parser_never_treats_paths_as_identifiers() -> None:
    clip_id = "12345678-1234-5678-1234-567812345678"
    assert str(parse_clip_id(clip_id)) == clip_id
    with pytest.raises(ClipNameError):
        parse_clip_id("../12345678-1234-5678-1234-567812345678")


def test_parse_rejects_names_that_are_not_generated_clip_names() -> None:
    with pytest.raises(ClipNameError):
        parse_clip_filename("not-a-dashcam-file.mp4")
