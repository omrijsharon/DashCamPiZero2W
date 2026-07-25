from __future__ import annotations

import json

from pytest import CaptureFixture, MonkeyPatch

from dashcam.cli import main
from dashcam.version import get_build_info


def test_build_info_uses_bounded_release_identifiers(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("DASHCAM_BUILD_ID", "release-2026.07.24")
    monkeypatch.setenv("DASHCAM_GIT_COMMIT", "abc1234")

    build = get_build_info()

    assert build.build_id == "release-2026.07.24"
    assert build.git_commit == "abc1234"


def test_build_info_rejects_unbounded_or_unsafe_identifiers(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("DASHCAM_BUILD_ID", "contains a space")
    monkeypatch.setenv("DASHCAM_GIT_COMMIT", "x" * 129)

    build = get_build_info()

    assert build.build_id == build.version
    assert build.git_commit is None


def test_cli_emits_machine_readable_build_info(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DASHCAM_BUILD_ID", raising=False)
    monkeypatch.delenv("DASHCAM_GIT_COMMIT", raising=False)

    assert main(["--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["version"]
    assert payload["build_id"] == payload["version"]
    assert payload["git_commit"] is None
