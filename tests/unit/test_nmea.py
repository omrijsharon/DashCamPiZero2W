from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from dashcam.gps.nmea import (
    MAX_NMEA_LINE_BYTES,
    NmeaError,
    SentenceType,
    TimeTrust,
    is_stale,
    parse_nmea_line,
)

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "nmea"


def _fixture_lines(name: str) -> list[str]:
    return (_FIXTURES / name).read_text(encoding="ascii").splitlines()


def _sentence(body: str) -> str:
    checksum = 0
    for character in body:
        checksum ^= ord(character)
    return f"${body}*{checksum:02X}"


def test_parse_valid_rmc_with_units_and_utc() -> None:
    outcome = parse_nmea_line(_fixture_lines("valid.nmea")[0], received_monotonic_ns=42)

    assert outcome.ok
    assert outcome.sentence is not None
    parsed = outcome.sentence
    assert parsed.sentence_type is SentenceType.RMC
    assert parsed.talker == "GP"
    assert parsed.received_monotonic_ns == 42
    assert parsed.utc_datetime == datetime(2026, 7, 23, 12, 35, 19, 250_000, tzinfo=UTC)
    assert parsed.time_trust is TimeTrust.RMC_STATUS_VALID
    assert parsed.time_anchor_candidate
    assert parsed.navigation_valid
    assert parsed.latitude_deg == pytest.approx(48.1173)
    assert parsed.longitude_deg == pytest.approx(11.5166667)
    assert parsed.speed_knots == pytest.approx(22.4)
    assert parsed.course_deg == pytest.approx(84.4)
    assert parsed.altitude_m is None


def test_rmc_invalid_status_does_not_establish_time_or_navigation_trust() -> None:
    outcome = parse_nmea_line(_fixture_lines("invalid_fix.nmea")[0], received_monotonic_ns=100)

    assert outcome.sentence is not None
    assert outcome.sentence.utc_datetime == datetime(2026, 7, 23, 23, 59, 59, 900_000, tzinfo=UTC)
    assert outcome.sentence.time_trust is TimeTrust.RMC_STATUS_INVALID
    assert not outcome.sentence.time_anchor_candidate
    assert not outcome.sentence.navigation_valid
    assert is_stale(outcome.sentence, now_monotonic_ns=106, stale_after_ns=5)


def test_zda_has_complete_utc_but_requires_external_plausibility_policy() -> None:
    outcome = parse_nmea_line(_fixture_lines("valid.nmea")[1])

    assert outcome.sentence is not None
    assert outcome.sentence.sentence_type is SentenceType.ZDA
    assert outcome.sentence.utc_datetime == datetime(2026, 7, 23, 12, 35, 20, tzinfo=UTC)
    assert outcome.sentence.time_trust is TimeTrust.ZDA_REQUIRES_PLAUSIBILITY
    assert outcome.sentence.time_anchor_candidate
    assert not outcome.sentence.navigation_valid


def test_gga_provides_navigation_but_not_a_date_anchor() -> None:
    outcome = parse_nmea_line(_fixture_lines("valid.nmea")[2])

    assert outcome.sentence is not None
    parsed = outcome.sentence
    assert parsed.sentence_type is SentenceType.GGA
    assert parsed.utc_datetime is None
    assert parsed.utc_time is not None
    assert parsed.time_trust is TimeTrust.UNAVAILABLE
    assert not parsed.time_anchor_candidate
    assert parsed.navigation_valid
    assert parsed.fix_quality == 1
    assert parsed.satellites == 8
    assert parsed.hdop == pytest.approx(0.9)
    assert parsed.altitude_m == pytest.approx(545.4)


def test_common_mixed_constellation_talkers_are_supported() -> None:
    outcomes = [parse_nmea_line(line) for line in _fixture_lines("mixed_talkers.nmea")]

    assert all(outcome.ok for outcome in outcomes)
    assert [outcome.sentence.talker for outcome in outcomes if outcome.sentence] == [
        "GL",
        "GA",
        "BD",
    ]


@pytest.mark.parametrize("talker", ["GP", "GN", "GL", "GA", "GB", "BD", "GQ", "GI"])
def test_common_gnss_talker_prefixes(talker: str) -> None:
    outcome = parse_nmea_line(_sentence(f"{talker}ZDA,120000.00,23,07,2026,00,00"))

    assert outcome.ok
    assert outcome.sentence is not None
    assert outcome.sentence.talker == talker


def test_checksum_failure_is_distinct_from_malformed_fields() -> None:
    bad_checksum = parse_nmea_line(_fixture_lines("bad_checksum.nmea")[0])
    bad_date = parse_nmea_line(
        _sentence("GPRMC,123519,A,4807.038,N,01131.000,E,0.0,0.0,310226,,,A")
    )

    assert bad_checksum.error is NmeaError.CHECKSUM_MISMATCH
    assert bad_checksum.sentence is None
    assert bad_date.error is NmeaError.MALFORMED_FIELDS


@pytest.mark.parametrize("line", _fixture_lines("malformed.nmea"))
def test_malformed_fixture_is_rejected_without_exception(line: str) -> None:
    outcome = parse_nmea_line(line)

    assert not outcome.ok
    assert outcome.error is not None
    assert outcome.detail
    assert len(outcome.detail) <= 160


@pytest.mark.parametrize(
    ("body", "expected_error"),
    [
        ("XXRMC,123519,A,,,,,0.0,0.0,230726", NmeaError.UNSUPPORTED_TALKER),
        ("GPTXT,01,01,02,test", NmeaError.UNSUPPORTED_SENTENCE),
        ("GPRMC,123519,X,,,,,0.0,0.0,230726", NmeaError.MALFORMED_FIELDS),
        ("GPGGA,123519,,,,,9,00,99.9,,M,,,", NmeaError.MALFORMED_FIELDS),
        ("GNRMC,123519,A,9000.001,N,00000.000,E,0,0,230726", NmeaError.MALFORMED_FIELDS),
    ],
)
def test_semantic_errors_have_explicit_outcomes(body: str, expected_error: NmeaError) -> None:
    assert parse_nmea_line(_sentence(body)).error is expected_error


def test_empty_nullable_fields_are_preserved_without_invented_zeroes() -> None:
    outcome = parse_nmea_line(_sentence("GPGGA,,,,,,0,,,,M,,,"))

    assert outcome.ok
    assert outcome.sentence is not None
    assert outcome.sentence.latitude_deg is None
    assert outcome.sentence.longitude_deg is None
    assert outcome.sentence.satellites is None
    assert outcome.sentence.hdop is None
    assert outcome.sentence.altitude_m is None
    assert not outcome.sentence.navigation_valid


def test_input_is_ascii_checked_and_bounded_before_parsing() -> None:
    assert parse_nmea_line(b"\xff").error is NmeaError.NON_ASCII
    assert parse_nmea_line("$GPRMC,\N{SNOWMAN}*00").error is NmeaError.NON_ASCII
    too_long = b"$" + b"A" * MAX_NMEA_LINE_BYTES
    outcome = parse_nmea_line(too_long)

    assert len(too_long) == MAX_NMEA_LINE_BYTES + 1
    assert outcome.error is NmeaError.TOO_LONG


def test_field_count_is_bounded_independently_of_byte_count() -> None:
    outcome = parse_nmea_line(_sentence("GPRMC" + "," * 20))

    assert outcome.error is NmeaError.TOO_MANY_FIELDS


def test_line_endings_count_toward_bound_and_bytes_are_accepted() -> None:
    encoded = (_fixture_lines("valid.nmea")[1] + "\r\n").encode("ascii")
    outcome = parse_nmea_line(encoded)

    assert len(encoded) <= MAX_NMEA_LINE_BYTES
    assert outcome.ok


def test_staleness_is_strictly_after_timeout_and_bad_timing_is_conservative() -> None:
    outcome = parse_nmea_line(_fixture_lines("stale.nmea")[0], received_monotonic_ns=10)
    assert outcome.sentence is not None

    assert not is_stale(outcome.sentence, now_monotonic_ns=15, stale_after_ns=5)
    assert is_stale(outcome.sentence, now_monotonic_ns=16, stale_after_ns=5)
    assert is_stale(outcome.sentence, now_monotonic_ns=9, stale_after_ns=5)
    assert is_stale(outcome.sentence, now_monotonic_ns=15, stale_after_ns=-1)


def test_bad_monotonic_timestamp_has_explicit_error() -> None:
    outcome = parse_nmea_line(
        _fixture_lines("valid.nmea")[0],
        received_monotonic_ns=-1,
    )

    assert outcome.error is NmeaError.INVALID_MONOTONIC
