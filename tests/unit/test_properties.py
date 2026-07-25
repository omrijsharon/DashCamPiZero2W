from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dashcam.config import ConfigError, config_from_mapping, config_to_mapping, default_config
from dashcam.gps.nmea import NmeaParseOutcome, parse_nmea_line
from dashcam.state import ClipLifecycle, ClipRecord, StateTransitionError
from dashcam.storage.intents import PairPaths
from dashcam.storage.naming import (
    finalized_clip_pair,
    parse_clip_filename,
    provisional_clip_pair,
    validate_filename_component,
)

_BOOT_IDS = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=5, max_size=16)
_SEQUENCES = st.integers(min_value=0, max_value=999_999)
_UTC_DATETIMES = st.datetimes(
    min_value=datetime(2000, 1, 1),
    max_value=datetime(2099, 12, 31, 23, 59, 59, 999999),
    timezones=st.just(UTC),
)


@given(boot_id=_BOOT_IDS, sequence=_SEQUENCES)
def test_provisional_names_round_trip_for_the_complete_input_domain(
    boot_id: str, sequence: int
) -> None:
    pair = provisional_clip_pair(boot_id=boot_id, sequence=sequence)

    parsed_video = parse_clip_filename(pair.video_name)
    parsed_sidecar = parse_clip_filename(pair.metadata_name)
    assert parsed_video.boot_id == parsed_sidecar.boot_id == boot_id
    assert parsed_video.sequence == parsed_sidecar.sequence == sequence
    assert parsed_video.provisional and parsed_sidecar.provisional
    assert validate_filename_component(pair.video_name) == pair.video_name


@given(started_at=_UTC_DATETIMES, boot_id=_BOOT_IDS, sequence=_SEQUENCES)
def test_final_names_round_trip_utc_to_millisecond_precision(
    started_at: datetime, boot_id: str, sequence: int
) -> None:
    pair = finalized_clip_pair(
        utc_started_at=started_at,
        boot_id=boot_id,
        sequence=sequence,
    )

    parsed = parse_clip_filename(pair.video_name)
    expected = started_at.replace(microsecond=(started_at.microsecond // 1000) * 1000)
    assert parsed.utc_started_at == expected
    assert parsed.boot_id == boot_id
    assert parsed.sequence == sequence


@given(
    prefix=st.sampled_from(("../", "./", "/", "clips//", "clips/../", "clips/./")),
    name=st.text(
        alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-",
        min_size=1,
        max_size=30,
    ),
)
def test_intent_paths_reject_generated_traversal_and_ambiguous_forms(
    prefix: str, name: str
) -> None:
    with pytest.raises(ValueError):
        PairPaths(
            video_source=f"{prefix}{name}.mp4",
            sidecar_source="clips/safe.json",
        )


@given(
    field=st.sampled_from(
        (
            ("video", "fps", 1, 60),
            ("overlay", "coordinate_decimals", 0, 8),
            ("preview", "max_clients", 1, 2),
            ("storage", "low_watermark_percent", 1, 99),
            ("service", "watchdog_s", 1, 300),
        )
    ),
    choose_below=st.booleans(),
)
def test_configuration_rejects_values_outside_declared_integer_bounds(
    field: tuple[str, str, int, int], choose_below: bool
) -> None:
    section_name, key, low, high = field
    raw = cast(dict[str, object], config_to_mapping(default_config()))
    section = raw[section_name]
    assert isinstance(section, dict)
    section[key] = low - 1 if choose_below else high + 1

    with pytest.raises(ConfigError):
        config_from_mapping(raw)


@given(
    source=st.sampled_from(tuple(ClipLifecycle)),
    target=st.sampled_from(tuple(ClipLifecycle)),
)
def test_lifecycle_transitions_are_immutable_and_never_change_identity(
    source: ClipLifecycle, target: ClipLifecycle
) -> None:
    clip_id = uuid4()
    original = ClipRecord(clip_id=clip_id, lifecycle=source)

    try:
        transitioned = original.transition_to(target)
    except StateTransitionError:
        assert original.lifecycle is source
        assert original.clip_id == clip_id
    else:
        assert transitioned is not original
        assert transitioned.lifecycle is target
        assert transitioned.clip_id == original.clip_id
        assert original.lifecycle is source


@given(raw_line=st.binary(min_size=0, max_size=256))
def test_arbitrary_bounded_or_oversized_nmea_bytes_never_escape_an_exception(
    raw_line: bytes,
) -> None:
    outcome = parse_nmea_line(raw_line)

    assert isinstance(outcome, NmeaParseOutcome)
    assert outcome.ok is (outcome.sentence is not None and outcome.error is None)
