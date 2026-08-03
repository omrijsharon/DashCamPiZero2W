"""Bounded, exception-safe parsing for the NMEA sentences used by the dashcam."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from enum import StrEnum
from functools import reduce
from operator import xor
from typing import Final

MAX_NMEA_LINE_BYTES: Final = 82
MAX_NMEA_FIELDS: Final = 20
_SUPPORTED_TALKERS: Final = frozenset({"BD", "GA", "GB", "GI", "GL", "GN", "GP", "GQ"})
_CHECKSUM_RE: Final = re.compile(r"[0-9A-Fa-f]{2}")
_IDENTIFIER_RE: Final = re.compile(r"[A-Z0-9]{5}")
_TIME_RE: Final = re.compile(
    r"(?P<hour>\d{2})(?P<minute>\d{2})(?P<second>\d{2})(?:\.(?P<fraction>\d{1,9}))?"
)
_DATE_RE: Final = re.compile(r"\d{6}")
_DECIMAL_RE: Final = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")
_INTEGER_RE: Final = re.compile(r"[+-]?\d+")


class SentenceType(StrEnum):
    """Supported NMEA formatter types."""

    RMC = "RMC"
    ZDA = "ZDA"
    GGA = "GGA"


class TimeTrust(StrEnum):
    """What a sentence alone establishes about its UTC fields."""

    UNAVAILABLE = "UNAVAILABLE"
    RMC_STATUS_VALID = "RMC_STATUS_VALID"
    RMC_STATUS_INVALID = "RMC_STATUS_INVALID"
    ZDA_REQUIRES_PLAUSIBILITY = "ZDA_REQUIRES_PLAUSIBILITY"


class NmeaError(StrEnum):
    """Stable reasons why an input line was not accepted."""

    EMPTY = "EMPTY"
    TOO_LONG = "TOO_LONG"
    NON_ASCII = "NON_ASCII"
    INVALID_MONOTONIC = "INVALID_MONOTONIC"
    MALFORMED_ENVELOPE = "MALFORMED_ENVELOPE"
    BAD_CHECKSUM_FORMAT = "BAD_CHECKSUM_FORMAT"
    CHECKSUM_MISMATCH = "CHECKSUM_MISMATCH"
    TOO_MANY_FIELDS = "TOO_MANY_FIELDS"
    UNSUPPORTED_TALKER = "UNSUPPORTED_TALKER"
    UNSUPPORTED_SENTENCE = "UNSUPPORTED_SENTENCE"
    MALFORMED_FIELDS = "MALFORMED_FIELDS"


@dataclass(frozen=True, slots=True)
class NmeaSentence:
    """Normalized values from one checksum-verified NMEA sentence.

    Unit-bearing names are intentional. A missing value stays ``None`` instead
    of being confused with a numeric zero.
    """

    sentence_type: SentenceType
    talker: str
    received_monotonic_ns: int | None
    utc_time: time | None = None
    utc_datetime: datetime | None = None
    time_trust: TimeTrust = TimeTrust.UNAVAILABLE
    latitude_deg: float | None = None
    longitude_deg: float | None = None
    speed_knots: float | None = None
    course_deg: float | None = None
    altitude_m: float | None = None
    fix_quality: int | None = None
    satellites: int | None = None
    hdop: float | None = None
    navigation_valid: bool = False

    @property
    def time_anchor_candidate(self) -> bool:
        """Whether complete UTC fields can proceed to anchor-policy validation."""

        return self.utc_datetime is not None and self.time_trust in {
            TimeTrust.RMC_STATUS_VALID,
            TimeTrust.ZDA_REQUIRES_PLAUSIBILITY,
        }


@dataclass(frozen=True, slots=True)
class NmeaParseOutcome:
    """Explicit success/error result; malformed input never raises publicly."""

    sentence: NmeaSentence | None = None
    error: NmeaError | None = None
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.sentence is not None and self.error is None


def parse_nmea_line(
    raw_line: str | bytes,
    *,
    received_monotonic_ns: int | None = None,
) -> NmeaParseOutcome:
    """Parse one bounded NMEA line without allowing data errors to escape.

    The maximum applies to the raw serial record including a possible CR/LF.
    Only printable ASCII is accepted after the line ending is removed.
    """

    if received_monotonic_ns is not None and (
        isinstance(received_monotonic_ns, bool)
        or not isinstance(received_monotonic_ns, int)
        or received_monotonic_ns < 0
    ):
        return _failure(NmeaError.INVALID_MONOTONIC, "monotonic timestamp must be non-negative")

    decoded = _decode_bounded_ascii(raw_line)
    if isinstance(decoded, NmeaParseOutcome):
        return decoded
    line = decoded

    if not line:
        return _failure(NmeaError.EMPTY, "empty NMEA record")
    if not line.isprintable():
        return _failure(NmeaError.NON_ASCII, "record contains non-printable ASCII")
    if not line.startswith("$") or line.count("*") != 1:
        return _failure(NmeaError.MALFORMED_ENVELOPE, "expected one '$...*HH' envelope")

    body, transmitted_checksum = line[1:].split("*", 1)
    if not _CHECKSUM_RE.fullmatch(transmitted_checksum):
        return _failure(NmeaError.BAD_CHECKSUM_FORMAT, "checksum must contain two hex digits")
    if _checksum(body) != int(transmitted_checksum, 16):
        return _failure(NmeaError.CHECKSUM_MISMATCH, "checksum does not match sentence body")

    fields = body.split(",")
    if len(fields) > MAX_NMEA_FIELDS:
        return _failure(NmeaError.TOO_MANY_FIELDS, "sentence exceeds the field limit")
    identifier = fields[0]
    if not _IDENTIFIER_RE.fullmatch(identifier):
        return _failure(NmeaError.MALFORMED_ENVELOPE, "invalid talker/formatter identifier")
    talker, formatter = identifier[:2], identifier[2:]
    if talker not in _SUPPORTED_TALKERS:
        return _failure(NmeaError.UNSUPPORTED_TALKER, f"unsupported talker {talker}")

    try:
        sentence_type = SentenceType(formatter)
    except ValueError:
        return _failure(NmeaError.UNSUPPORTED_SENTENCE, f"unsupported formatter {formatter}")

    try:
        if sentence_type is SentenceType.RMC:
            sentence = _parse_rmc(talker, fields[1:], received_monotonic_ns)
        elif sentence_type is SentenceType.ZDA:
            sentence = _parse_zda(talker, fields[1:], received_monotonic_ns)
        else:
            sentence = _parse_gga(talker, fields[1:], received_monotonic_ns)
    except (ArithmeticError, IndexError, ValueError) as exc:
        return _failure(NmeaError.MALFORMED_FIELDS, str(exc))
    return NmeaParseOutcome(sentence=sentence)


def is_stale(
    sentence: NmeaSentence,
    *,
    now_monotonic_ns: int,
    stale_after_ns: int,
) -> bool:
    """Return whether a received sample is stale, conservatively on bad timing input."""

    received = sentence.received_monotonic_ns
    if (
        received is None
        or isinstance(now_monotonic_ns, bool)
        or isinstance(stale_after_ns, bool)
        or not isinstance(now_monotonic_ns, int)
        or not isinstance(stale_after_ns, int)
        or now_monotonic_ns < received
        or stale_after_ns < 0
    ):
        return True
    return now_monotonic_ns - received > stale_after_ns


def _decode_bounded_ascii(raw_line: str | bytes) -> str | NmeaParseOutcome:
    if isinstance(raw_line, bytes):
        if len(raw_line) > MAX_NMEA_LINE_BYTES:
            return _failure(NmeaError.TOO_LONG, "raw record exceeds 82 bytes")
        try:
            line = raw_line.decode("ascii")
        except UnicodeDecodeError:
            return _failure(NmeaError.NON_ASCII, "record is not ASCII")
    elif isinstance(raw_line, str):
        try:
            encoded = raw_line.encode("ascii")
        except UnicodeEncodeError:
            return _failure(NmeaError.NON_ASCII, "record is not ASCII")
        if len(encoded) > MAX_NMEA_LINE_BYTES:
            return _failure(NmeaError.TOO_LONG, "raw record exceeds 82 bytes")
        line = raw_line
    else:
        return _failure(NmeaError.NON_ASCII, "record must be str or bytes")

    if line.endswith("\r\n"):
        return line[:-2]
    if line.endswith(("\r", "\n")):
        return line[:-1]
    return line


def _failure(error: NmeaError, detail: str) -> NmeaParseOutcome:
    return NmeaParseOutcome(error=error, detail=detail[:160])


def _checksum(body: str) -> int:
    return reduce(xor, body.encode("ascii"), 0)


def _field(fields: list[str], index: int) -> str:
    return fields[index] if index < len(fields) else ""


def _parse_rmc(
    talker: str,
    fields: list[str],
    received_monotonic_ns: int | None,
) -> NmeaSentence:
    utc_time = _optional_utc_time(_field(fields, 0))
    status = _field(fields, 1)
    if status not in {"A", "V"}:
        raise ValueError("RMC status must be A or V")
    latitude, longitude = _coordinates(
        _field(fields, 2),
        _field(fields, 3),
        _field(fields, 4),
        _field(fields, 5),
    )
    speed_knots = _optional_float(_field(fields, 6), minimum=0.0, maximum=2_000.0)
    course_deg = _optional_float(_field(fields, 7), minimum=0.0, maximum=360.0)
    utc_date = _optional_nmea_date(_field(fields, 8))
    utc_datetime = _combine_utc(utc_date, utc_time)

    if status == "A" and utc_datetime is not None:
        trust = TimeTrust.RMC_STATUS_VALID
    elif status == "V":
        trust = TimeTrust.RMC_STATUS_INVALID
    else:
        trust = TimeTrust.UNAVAILABLE

    return NmeaSentence(
        sentence_type=SentenceType.RMC,
        talker=talker,
        received_monotonic_ns=received_monotonic_ns,
        utc_time=utc_time,
        utc_datetime=utc_datetime,
        time_trust=trust,
        latitude_deg=latitude,
        longitude_deg=longitude,
        speed_knots=speed_knots,
        course_deg=course_deg,
        navigation_valid=status == "A" and latitude is not None and longitude is not None,
    )


def _parse_zda(
    talker: str,
    fields: list[str],
    received_monotonic_ns: int | None,
) -> NmeaSentence:
    utc_time = _required_utc_time(_field(fields, 0))
    day = _required_int(_field(fields, 1), minimum=1, maximum=31)
    month = _required_int(_field(fields, 2), minimum=1, maximum=12)
    year = _required_int(_field(fields, 3), minimum=1980, maximum=9999)
    utc_date = date(year, month, day)

    local_hour = _field(fields, 4)
    local_minute = _field(fields, 5)
    if bool(local_hour) != bool(local_minute):
        raise ValueError("ZDA local-zone fields must both be present or empty")
    if local_hour:
        _required_int(local_hour, minimum=-13, maximum=13)
        _required_int(local_minute, minimum=0, maximum=59)

    return NmeaSentence(
        sentence_type=SentenceType.ZDA,
        talker=talker,
        received_monotonic_ns=received_monotonic_ns,
        utc_time=utc_time,
        utc_datetime=_combine_utc(utc_date, utc_time),
        time_trust=TimeTrust.ZDA_REQUIRES_PLAUSIBILITY,
    )


def _parse_gga(
    talker: str,
    fields: list[str],
    received_monotonic_ns: int | None,
) -> NmeaSentence:
    utc_time = _optional_utc_time(_field(fields, 0))
    latitude, longitude = _coordinates(
        _field(fields, 1),
        _field(fields, 2),
        _field(fields, 3),
        _field(fields, 4),
    )
    fix_quality = _required_int(_field(fields, 5), minimum=0, maximum=8)
    satellites = _optional_int(_field(fields, 6), minimum=0, maximum=99)
    hdop = _optional_float(_field(fields, 7), minimum=0.0, maximum=100.0)
    altitude_m = _optional_float(_field(fields, 8), minimum=-20_000.0, maximum=20_000.0)
    altitude_unit = _field(fields, 9)
    if altitude_m is not None and altitude_unit != "M":
        raise ValueError("GGA altitude must use metres")

    return NmeaSentence(
        sentence_type=SentenceType.GGA,
        talker=talker,
        received_monotonic_ns=received_monotonic_ns,
        utc_time=utc_time,
        latitude_deg=latitude,
        longitude_deg=longitude,
        altitude_m=altitude_m,
        fix_quality=fix_quality,
        satellites=satellites,
        hdop=hdop,
        navigation_valid=fix_quality > 0 and latitude is not None and longitude is not None,
    )


def _optional_utc_time(value: str) -> time | None:
    if not value:
        return None
    return _required_utc_time(value)


def _required_utc_time(value: str) -> time:
    match = _TIME_RE.fullmatch(value)
    if match is None:
        raise ValueError("invalid UTC time")
    fraction = match.group("fraction") or ""
    microsecond = int((fraction + "000000")[:6])
    return time(
        int(match.group("hour")),
        int(match.group("minute")),
        int(match.group("second")),
        microsecond,
        tzinfo=UTC,
    )


def _optional_nmea_date(value: str) -> date | None:
    if not value:
        return None
    if _DATE_RE.fullmatch(value) is None:
        raise ValueError("invalid RMC date")
    day, month, two_digit_year = int(value[:2]), int(value[2:4]), int(value[4:])
    year = 1900 + two_digit_year if two_digit_year >= 80 else 2000 + two_digit_year
    return date(year, month, day)


def _combine_utc(utc_date: date | None, utc_time: time | None) -> datetime | None:
    if utc_date is None or utc_time is None:
        return None
    return datetime.combine(utc_date, utc_time).astimezone(UTC)


def _coordinates(
    latitude_value: str,
    latitude_hemisphere: str,
    longitude_value: str,
    longitude_hemisphere: str,
) -> tuple[float | None, float | None]:
    values = (
        latitude_value,
        latitude_hemisphere,
        longitude_value,
        longitude_hemisphere,
    )
    if not any(values):
        return None, None
    if not all(values):
        raise ValueError("coordinate fields must be complete")
    latitude = _coordinate(latitude_value, latitude_hemisphere, degree_digits=2, limit=90)
    longitude = _coordinate(longitude_value, longitude_hemisphere, degree_digits=3, limit=180)
    return latitude, longitude


def _coordinate(value: str, hemisphere: str, *, degree_digits: int, limit: int) -> float:
    if hemisphere not in ({"N", "S"} if degree_digits == 2 else {"E", "W"}):
        raise ValueError("invalid coordinate hemisphere")
    if (
        len(value) < degree_digits + 3
        or value[degree_digits : degree_digits + 2].isdigit() is False
    ):
        raise ValueError("invalid coordinate format")
    degrees_text, minutes_text = value[:degree_digits], value[degree_digits:]
    if not degrees_text.isdigit():
        raise ValueError("invalid coordinate degrees")
    if not re.fullmatch(r"\d{2}(?:\.\d+)?", minutes_text):
        raise ValueError("invalid coordinate minutes")
    minutes = _required_float(minutes_text, minimum=0.0, maximum=59.999999999)
    degrees = int(degrees_text)
    if degrees > limit or (degrees == limit and minutes != 0.0):
        raise ValueError("coordinate exceeds legal range")
    result = degrees + minutes / 60.0
    return -result if hemisphere in {"S", "W"} else result


def _required_float(value: str, *, minimum: float, maximum: float) -> float:
    if not value or _DECIMAL_RE.fullmatch(value) is None:
        raise ValueError("required numeric field is invalid")
    parsed = float(value)
    if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
        raise ValueError("numeric field is out of range")
    return parsed


def _optional_float(value: str, *, minimum: float, maximum: float) -> float | None:
    if not value:
        return None
    return _required_float(value, minimum=minimum, maximum=maximum)


def _required_int(value: str, *, minimum: int, maximum: int) -> int:
    if not value or _INTEGER_RE.fullmatch(value) is None:
        raise ValueError("required integer field is invalid")
    parsed = int(value)
    if not minimum <= parsed <= maximum:
        raise ValueError("integer field is out of range")
    return parsed


def _optional_int(value: str, *, minimum: int, maximum: int) -> int | None:
    if not value:
        return None
    return _required_int(value, minimum=minimum, maximum=maximum)
