from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

from dashcam.provisioning.collector import (
    COMMAND_TIMEOUT_SECONDS,
    MAX_COMMAND_OUTPUT_BYTES,
    StorageCollectorError,
    SubprocessCommandRunner,
    collect_storage_observation,
)
from dashcam.provisioning.layout import observation_from_mapping

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "collector" / "current-pi-like.json"
LSBLK_KEY = (
    "/usr/bin/lsblk --json --bytes --output PATH,KNAME,PKNAME,TYPE,SERIAL,SIZE,LOG-SEC,MOUNTPOINTS"
)


class FixtureRunner:
    def __init__(self, responses: dict[str, str]) -> None:
        self.responses = responses
        self.calls: list[tuple[tuple[str, ...], float, int]] = []

    def run(self, argv: tuple[str, ...], *, timeout_seconds: float, max_output_bytes: int) -> str:
        self.calls.append((argv, timeout_seconds, max_output_bytes))
        return self.responses[" ".join(argv)]


def _fixture() -> dict[str, str]:
    raw = cast(dict[str, Any], json.loads(FIXTURE.read_text("utf-8")))
    return cast(dict[str, str], raw["responses"])


def test_collects_current_pi_like_source_as_exact_layout_schema() -> None:
    runner = FixtureRunner(_fixture())
    observed = collect_storage_observation("/dev/mmcblk9", runner)

    parsed = observation_from_mapping(observed)
    assert parsed.identity.resolved_path == "/dev/mmcblk9"
    assert parsed.identity.serial == "fe34325344000000200000031a0192d1"
    assert parsed.identity.size_bytes == 31_457_280_000
    assert parsed.identity.partition_table_fingerprint == (
        "17eee4a5eb7d0641bf6ea6a2013ff5203c09aa72b7420ff990bd82ec08406ae6"
    )
    assert not parsed.is_root_disk
    assert not parsed.is_system_disk
    assert parsed.partitions[0].end_sector == 1_064_959
    assert parsed.partitions[1].partuuid == "4f2c9ea0-02"
    assert all(
        call[1:] == (COMMAND_TIMEOUT_SECONDS, MAX_COMMAND_OUTPUT_BYTES) for call in runner.calls
    )
    assert ("/usr/sbin/sfdisk", "--json", "/dev/mmcblk9") in [call[0] for call in runner.calls]
    assert not any(
        "mkfs" in argument or argument == "mount" for call in runner.calls for argument in call[0]
    )


@pytest.mark.parametrize(
    ("mutator", "message"),
    (
        (
            lambda responses: responses.__setitem__(
                LSBLK_KEY,
                "not-json",
            ),
            "lsblk output is not valid JSON",
        ),
        (
            lambda responses: responses.__setitem__(
                "/usr/bin/findmnt --noheadings --output SOURCE /",
                "/dev/mmcblk0p2\n/dev/mmcblk9p2\n",
            ),
            "root source must contain exactly one",
        ),
        (
            lambda responses: responses.__setitem__(
                "/usr/sbin/blkid --probe --output export /dev/mmcblk9p1",
                "TYPE=vfat\nLABEL=bootfs\n",
            ),
            "blkid device identity",
        ),
    ),
)
def test_rejects_malformed_text_and_missing_required_fields(mutator: Any, message: str) -> None:
    responses = _fixture()
    mutator(responses)
    with pytest.raises(StorageCollectorError, match=message):
        collect_storage_observation("/dev/mmcblk9", FixtureRunner(responses))


def test_rejects_ambiguous_selected_device() -> None:
    responses = _fixture()
    document = json.loads(responses[LSBLK_KEY])
    document["blockdevices"].append(document["blockdevices"][1].copy())
    responses[LSBLK_KEY] = json.dumps(document)
    with pytest.raises(StorageCollectorError, match="duplicate device paths"):
        collect_storage_observation("/dev/mmcblk9", FixtureRunner(responses))


def test_partition_number_comes_from_node_not_sfdisk_list_order() -> None:
    responses = _fixture()
    document = json.loads(responses["/usr/sbin/sfdisk --json /dev/mmcblk9"])
    document["partitiontable"]["partitions"].reverse()
    responses["/usr/sbin/sfdisk --json /dev/mmcblk9"] = json.dumps(document)

    parsed = observation_from_mapping(
        collect_storage_observation("/dev/mmcblk9", FixtureRunner(responses))
    )

    assert [partition.number for partition in parsed.partitions] == [1, 2]
    assert parsed.partitions[0].partuuid == "4f2c9ea0-01"


def test_rejects_oversized_runner_output() -> None:
    responses = _fixture()
    responses[LSBLK_KEY] = "x" * (MAX_COMMAND_OUTPUT_BYTES + 1)
    with pytest.raises(StorageCollectorError, match="exceeds bound"):
        collect_storage_observation("/dev/mmcblk9", FixtureRunner(responses))


def test_production_runner_times_out_and_never_uses_a_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class TimeoutProcess:
        stdout = io.BytesIO()
        stderr = io.BytesIO()
        returncode = 0
        killed = False

        def wait(self, timeout: float | None = None) -> int:
            if not self.killed:
                raise subprocess.TimeoutExpired(("lsblk",), timeout or 0.0)
            return self.returncode

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

    process = TimeoutProcess()

    def fake_popen(argv: tuple[str, ...], **kwargs: object) -> TimeoutProcess:
        captured["argv"] = argv
        captured.update(kwargs)
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    with pytest.raises(StorageCollectorError, match="timed out"):
        SubprocessCommandRunner().run(
            (
                "/usr/bin/lsblk",
                "--json",
                "--bytes",
                "--output",
                "PATH,KNAME,PKNAME,TYPE,SERIAL,SIZE,LOG-SEC,MOUNTPOINTS",
            ),
            timeout_seconds=0.1,
            max_output_bytes=100,
        )
    assert process.killed
    assert captured["shell"] is False
    assert captured["stdin"] is subprocess.DEVNULL


def test_production_runner_rejects_oversized_output(monkeypatch: pytest.MonkeyPatch) -> None:
    class OverflowProcess:
        stdout = io.BytesIO(b"x" * 60)
        stderr = io.BytesIO(b"y" * 60)
        returncode = 0

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: OverflowProcess())
    with pytest.raises(StorageCollectorError, match="exceeded bound"):
        SubprocessCommandRunner().run(
            (
                "/usr/bin/lsblk",
                "--json",
                "--bytes",
                "--output",
                "PATH,KNAME,PKNAME,TYPE,SERIAL,SIZE,LOG-SEC,MOUNTPOINTS",
            ),
            timeout_seconds=1.0,
            max_output_bytes=100,
        )
