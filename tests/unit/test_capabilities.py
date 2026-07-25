from __future__ import annotations

import io
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from dashcam.diagnostics import capabilities
from dashcam.diagnostics.capabilities import (
    COMMAND_PROBES,
    COMMAND_TIMEOUT_SECONDS,
    FILE_PROBES,
    MAX_COMMAND_OUTPUT_BYTES,
    MAX_FACT_VALUE_CHARS,
    MAX_FILE_READ_BYTES,
    BoundedFileReader,
    ProbeResult,
    SubprocessCommandRunner,
    collect_capability_report,
    redact_text,
    write_report_exclusive,
)

_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE = _ROOT / "tests" / "fixtures" / "capabilities" / "pi_zero2w_sample.json"
_SCHEMA = _ROOT / "schemas" / "capability-report-v1.schema.json"

_EXPECTED_COMMANDS = (
    ("uname", "-srvmo"),
    ("uname", "-m"),
    ("python3", "--version"),
    ("systemctl", "--version"),
    ("rpicam-hello", "--list-cameras"),
    ("rpicam-hello", "--version"),
    ("libcamera-hello", "--list-cameras"),
    ("gst-inspect-1.0", "--version"),
    ("gst-inspect-1.0", "--exists", "libcamerasrc"),
    ("gst-inspect-1.0", "--exists", "v4l2h264enc"),
    ("ffmpeg", "-version"),
    ("ffmpeg", "-hide_banner", "-encoders"),
    ("ffprobe", "-version"),
    ("v4l2-ctl", "--list-devices"),
    ("arecord", "--list-devices"),
    ("arecord", "--list-pcms"),
    ("readlink", "-e", "/dev/serial0"),
    (
        "lsblk",
        "--json",
        "--output",
        "NAME,KNAME,TYPE,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINTS,RO",
    ),
    (
        "findmnt",
        "--json",
        "--output",
        "SOURCE,TARGET,FSTYPE,OPTIONS,LABEL,UUID",
        "/srv/dashcam",
    ),
    (
        "df",
        "--output=source,fstype,size,used,avail,pcent,target",
        "-B1",
        "/srv/dashcam",
    ),
    ("modinfo", "exfat"),
    ("fsck.exfat", "-V"),
    ("mkfs.exfat", "-V"),
    ("vcgencmd", "get_throttled"),
    ("vcgencmd", "measure_temp"),
)

_EXPECTED_FILES = (
    "/proc/device-tree/model",
    "/etc/os-release",
    "/proc/cpuinfo",
    "/proc/meminfo",
    "/proc/cmdline",
    "/boot/firmware/config.txt",
    "/boot/config.txt",
    "/proc/filesystems",
    "/sys/class/thermal/thermal_zone0/temp",
)


def _load_fixture() -> dict[str, dict[str, dict[str, Any]]]:
    return cast(dict[str, dict[str, dict[str, Any]]], json.loads(_FIXTURE.read_text("utf-8")))


class FixtureCommandRunner:
    def __init__(self, fixture: dict[str, dict[str, dict[str, Any]]]) -> None:
        self._results = fixture["commands"]
        self._ids_by_argv = {probe.argv: probe.probe_id for probe in COMMAND_PROBES}
        self.calls: list[tuple[tuple[str, ...], float, int]] = []

    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> ProbeResult:
        self.calls.append((argv, timeout_seconds, max_output_bytes))
        raw = self._results[self._ids_by_argv[argv]]
        return ProbeResult(
            cast(capabilities.ProbeState, raw["state"]),
            cast(str, raw["value"]),
            cast(str | None, raw.get("note")),
            cast(bool, raw.get("truncated", False)),
        )


class FixtureFileReader:
    def __init__(self, fixture: dict[str, dict[str, dict[str, Any]]]) -> None:
        self._results = fixture["files"]
        self._ids_by_path = {probe.path: probe.probe_id for probe in FILE_PROBES}
        self.calls: list[tuple[str, int]] = []

    def read(self, path: str, *, max_bytes: int) -> ProbeResult:
        self.calls.append((path, max_bytes))
        raw = self._results[self._ids_by_path[path]]
        return ProbeResult(
            cast(capabilities.ProbeState, raw["state"]),
            cast(str, raw["value"]),
            cast(str | None, raw.get("note")),
            cast(bool, raw.get("truncated", False)),
        )


def _collect() -> tuple[
    dict[str, object],
    FixtureCommandRunner,
    FixtureFileReader,
]:
    fixture = _load_fixture()
    commands = FixtureCommandRunner(fixture)
    files = FixtureFileReader(fixture)
    report = collect_capability_report(
        commands,
        files,
        target_kind="local_fixture",
        now=lambda: datetime(2026, 7, 24, 12, 30, tzinfo=UTC),
    )
    return report, commands, files


def test_probe_allowlists_are_exact_and_fixed() -> None:
    assert tuple(probe.argv for probe in COMMAND_PROBES) == _EXPECTED_COMMANDS
    assert tuple(probe.path for probe in FILE_PROBES) == _EXPECTED_FILES
    assert len({probe.probe_id for probe in COMMAND_PROBES + FILE_PROBES}) == (
        len(COMMAND_PROBES) + len(FILE_PROBES)
    )
    assert all("|" not in arg and ";" not in arg for argv in _EXPECTED_COMMANDS for arg in argv)


def test_platform_probe_commands_require_systemctl_and_existing_serial0_mapping() -> None:
    commands_by_id = {probe.probe_id: probe.argv for probe in COMMAND_PROBES}
    assert commands_by_id["os.systemd"] == ("systemctl", "--version")
    assert commands_by_id["uart.serial0.mapping"] == ("readlink", "-e", "/dev/serial0")
    assert commands_by_id["media.gstreamer.libcamera"] == (
        "gst-inspect-1.0",
        "--exists",
        "libcamerasrc",
    )
    assert commands_by_id["media.gstreamer.h264"] == (
        "gst-inspect-1.0",
        "--exists",
        "v4l2h264enc",
    )


def test_absent_serial0_mapping_is_not_reported_as_observed() -> None:
    fixture = _load_fixture()
    fixture["commands"]["uart.serial0.mapping"] = {
        "state": "error",
        "value": "",
        "note": "command exited with status 1",
    }
    report = collect_capability_report(
        FixtureCommandRunner(fixture),
        FixtureFileReader(fixture),
        target_kind="local_fixture",
        now=lambda: datetime(2026, 7, 24, tzinfo=UTC),
    )

    uart = cast(dict[str, object], cast(dict[str, object], report["raw_observations"])["uart"])
    serial0 = next(
        fact
        for fact in cast(list[dict[str, object]], uart["facts"])
        if fact["id"] == "uart.serial0.mapping"
    )
    assert serial0["state"] == "error"
    assert serial0["value"] is None
    assert cast(list[dict[str, str]], uart["warnings"])[0]["code"] == "uart.serial0.mapping.state"


def test_fixture_report_matches_schema_and_preserves_unknown_decisions() -> None:
    report, commands, files = _collect()
    schema = cast(dict[str, Any], json.loads(_SCHEMA.read_text("utf-8")))
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(report)

    assert report["status"] == "partial"
    assert cast(dict[str, object], report["target"]) == {
        "kind": "local_fixture",
        "identity_state": "observed",
        "declared_model": "Raspberry Pi Zero 2 W Rev 1.0",
    }
    decisions = cast(list[dict[str, object]], report["evaluated_capabilities"])
    assert {decision["decision"] for decision in decisions} == {"unknown"}
    assert len(commands.calls) == len(COMMAND_PROBES)
    assert len(files.calls) == len(FILE_PROBES)


def test_collector_passes_exact_deadlines_and_bounds() -> None:
    _, commands, files = _collect()
    assert [call[0] for call in commands.calls] == list(_EXPECTED_COMMANDS)
    assert all(call[1] == COMMAND_TIMEOUT_SECONDS for call in commands.calls)
    assert all(call[2] == MAX_COMMAND_OUTPUT_BYTES for call in commands.calls)
    assert files.calls == [(path, MAX_FILE_READ_BYTES) for path in _EXPECTED_FILES]


def test_report_fields_are_bounded_and_secrets_are_redacted() -> None:
    fixture = _load_fixture()
    fixture["commands"]["os.uname"] = {
        "state": "observed",
        "value": (
            "password=hunter2\ntoken:abc123\nurl=https://alice:secret@example.test/\n" + "x" * 1_000
        ),
    }
    report = collect_capability_report(
        FixtureCommandRunner(fixture),
        FixtureFileReader(fixture),
        target_kind="local_fixture",
        now=lambda: datetime(2026, 7, 24, tzinfo=UTC),
    )
    encoded = json.dumps(report)
    assert "hunter2" not in encoded
    assert "abc123" not in encoded
    assert "alice:secret" not in encoded
    assert "[REDACTED]" in encoded

    os_section = cast(dict[str, object], cast(dict[str, object], report["raw_observations"])["os"])
    facts = cast(list[dict[str, object]], os_section["facts"])
    uname = next(fact for fact in facts if fact["id"] == "os.uname")
    assert 512 < len(cast(str, uname["value"])) <= MAX_FACT_VALUE_CHARS


def test_redaction_removes_private_material() -> None:
    source = (
        "-----BEGIN PRIVATE KEY-----\nfixture-secret\n-----END PRIVATE KEY-----\n"
        'passphrase = "fixture pass with spaces"'
    )
    redacted = redact_text(source)
    assert "fixture-secret" not in redacted
    assert "fixture pass with spaces" not in redacted


def test_production_adapters_reject_non_allowlisted_inputs_without_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_popen(*args: object, **kwargs: object) -> None:
        raise AssertionError("Popen must not be reached")

    monkeypatch.setattr(subprocess, "Popen", forbidden_popen)
    command_result = SubprocessCommandRunner().run(
        ("sh", "-c", "id"),
        timeout_seconds=COMMAND_TIMEOUT_SECONDS,
        max_output_bytes=MAX_COMMAND_OUTPUT_BYTES,
    )
    file_result = BoundedFileReader().read("/etc/shadow", max_bytes=MAX_FILE_READ_BYTES)
    assert command_result.state == "error"
    assert file_result.state == "error"


def test_subprocess_adapter_explicitly_disables_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeProcess:
        stdout = io.BytesIO(b"armv7l\n")
        stderr = io.BytesIO()
        returncode = 0

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def kill(self) -> None:
            self.returncode = -9

    def fake_popen(argv: tuple[str, ...], **kwargs: object) -> FakeProcess:
        captured["argv"] = argv
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    result = SubprocessCommandRunner().run(
        ("uname", "-m"),
        timeout_seconds=1.0,
        max_output_bytes=100,
    )
    assert result == ProbeResult("observed", "armv7l")
    assert captured["argv"] == ("uname", "-m")
    assert captured["shell"] is False
    assert captured["stdin"] is subprocess.DEVNULL


@pytest.mark.parametrize(
    ("argv", "returncode", "expected"),
    (
        (
            ("gst-inspect-1.0", "--exists", "libcamerasrc"),
            0,
            ProbeResult("observed", ""),
        ),
        (
            ("gst-inspect-1.0", "--exists", "v4l2h264enc"),
            1,
            ProbeResult("error", "", "command exited with status 1"),
        ),
    ),
)
def test_gstreamer_exists_commands_distinguish_empty_success_from_missing_element(
    monkeypatch: pytest.MonkeyPatch,
    argv: tuple[str, ...],
    returncode: int,
    expected: ProbeResult,
) -> None:
    class FakeProcess:
        stdout = io.BytesIO()
        stderr = io.BytesIO()

        def __init__(self, code: int) -> None:
            self.returncode = code

        def wait(self, timeout: float | None = None) -> int:
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: FakeProcess(returncode))
    result = SubprocessCommandRunner().run(
        argv,
        timeout_seconds=COMMAND_TIMEOUT_SECONDS,
        max_output_bytes=MAX_COMMAND_OUTPUT_BYTES,
    )
    assert result == expected


def test_subprocess_adapter_terminates_over_limit_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OverflowProcess:
        stdout = io.BytesIO(b"x" * 101)
        stderr = io.BytesIO()
        returncode = 0
        killed = False

        def wait(self, timeout: float | None = None) -> int:
            return self.returncode

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

    process = OverflowProcess()
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    result = SubprocessCommandRunner().run(
        ("uname", "-m"),
        timeout_seconds=1.0,
        max_output_bytes=100,
    )
    assert process.killed
    assert result.state == "error"
    assert result.truncated
    assert len(result.value) == 100


def test_subprocess_adapter_terminates_at_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TimeoutProcess:
        stdout = io.BytesIO()
        stderr = io.BytesIO()
        returncode = 0
        waits = 0
        killed = False

        def wait(self, timeout: float | None = None) -> int:
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired(
                    ("uname", "-m"),
                    timeout if timeout is not None else 0.0,
                )
            return self.returncode

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

    process = TimeoutProcess()
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    result = SubprocessCommandRunner().run(
        ("uname", "-m"),
        timeout_seconds=0.25,
        max_output_bytes=100,
    )
    assert process.killed
    assert process.waits == 2
    assert result.state == "error"
    assert result.note == "command timed out"


def test_collection_performs_no_writes(tmp_path: Path) -> None:
    before = list(tmp_path.iterdir())
    _collect()
    assert list(tmp_path.iterdir()) == before


def test_explicit_output_is_exclusive_and_refuses_unsafe_paths(tmp_path: Path) -> None:
    output = (tmp_path / "capabilities.json").resolve()
    write_report_exclusive(output, '{"schema_version":1}\n')
    assert output.read_text("utf-8") == '{"schema_version":1}\n'

    with pytest.raises(FileExistsError):
        write_report_exclusive(output, "{}\n")
    with pytest.raises(ValueError, match="absolute"):
        write_report_exclusive(Path("relative.json"), "{}\n")
    with pytest.raises(ValueError, match=r"\.json"):
        write_report_exclusive((tmp_path / "report.txt").resolve(), "{}\n")
