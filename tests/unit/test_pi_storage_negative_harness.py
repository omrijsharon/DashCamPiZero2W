from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
HARNESS_PATH = ROOT / "deploy/ssh-dev-validation/storage-preflight-negative/run.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("pi_storage_negative_harness", HARNESS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_matrix_covers_every_required_negative_state_without_probe() -> None:
    harness = _load()
    cases = {case.name: case for case in harness.CASES}

    assert set(cases) == {
        "unmounted_rootfs_fallback",
        "wrong_filesystem",
        "wrong_label",
        "wrong_uuid",
        "wrong_sentinel",
        "read_only",
    }
    assert cases["unmounted_rootfs_fallback"].filesystem is None
    assert cases["wrong_filesystem"].filesystem == "ext4"
    assert cases["wrong_label"].label != "DASHCAM"
    assert cases["wrong_uuid"].identity_mode == "wrong_uuid"
    assert cases["wrong_sentinel"].sentinel_mode == "wrong_identity"
    assert cases["read_only"].read_only
    assert all(case.expected_reasons for case in cases.values())


@pytest.mark.parametrize(
    ("case_name", "state", "reasons"),
    [
        ("wrong_filesystem", "FAULTED", ["WRONG_FILESYSTEM"]),
        ("wrong_label", "FAULTED", ["WRONG_LABEL"]),
        ("wrong_uuid", "FAULTED", ["WRONG_UUID"]),
        ("wrong_sentinel", "FAULTED", ["WRONG_SENTINEL_IDENTITY"]),
        ("read_only", "READ_ONLY", ["READ_ONLY"]),
    ],
)
def test_expected_production_refusals_are_strict(
    case_name: str,
    state: str,
    reasons: list[str],
) -> None:
    harness = _load()
    status = {
        "schema_version": 1,
        "ready": False,
        "state": state,
        "reasons": reasons,
        "probe_attempted": False,
        "probe_succeeded": False,
    }

    harness._validate_case_status(
        harness.CASE_BY_NAME[case_name],
        2,
        status,
        probe_absent=True,
    )

    with pytest.raises(harness.HarnessError):
        harness._validate_case_status(
            harness.CASE_BY_NAME[case_name],
            2,
            {**status, "probe_attempted": True},
            probe_absent=False,
        )


def test_unmounted_rootfs_fallback_requires_full_fail_closed_shape() -> None:
    harness = _load()
    case = harness.CASE_BY_NAME["unmounted_rootfs_fallback"]
    status = {
        "schema_version": 1,
        "ready": False,
        "state": "FAULTED",
        "reasons": sorted(case.expected_reasons),
        "probe_attempted": False,
        "probe_succeeded": False,
    }

    harness._validate_case_status(case, 2, status, probe_absent=True)


def test_namespace_guard_requires_private_mount_and_unchanged_network() -> None:
    harness = _load()

    harness._validate_namespace_isolation(
        "mnt:[2]",
        "mnt:[1]",
        "net:[7]",
        "net:[7]",
    )
    with pytest.raises(harness.HarnessError, match="private mount"):
        harness._validate_namespace_isolation(
            "mnt:[1]",
            "mnt:[1]",
            "net:[7]",
            "net:[7]",
        )
    with pytest.raises(harness.HarnessError, match="network namespace"):
        harness._validate_namespace_isolation(
            "mnt:[2]",
            "mnt:[1]",
            "net:[8]",
            "net:[7]",
        )


def test_worker_command_unshares_only_the_mount_namespace() -> None:
    harness = _load()
    command = harness._unshare_command(
        HARNESS_PATH,
        harness.CASE_BY_NAME["wrong_label"],
        "mnt:[1]",
        "net:[7]",
        "a" * 64,
    )

    assert command[:4] == (
        "/usr/bin/unshare",
        "--mount",
        "--fork",
        "--kill-child",
    )
    assert "--net" not in command
    assert "--pid" not in command
    assert command.count("--worker-case") == 1
    assert command.count("--host-snapshot-sha256") == 1


def test_workers_detach_the_cloned_mount_before_modeling_each_case() -> None:
    source = HARNESS_PATH.read_text(encoding="utf-8")

    assert source.count("_unmount_private_recording_root()") == 3
    assert 'raise HarnessError("private recording root still appears as a mountpoint")' in source
    assert source.index("_unmount_private_recording_root()") < source.index(
        "_mount(loop, RECORDING_ROOT"
    )


def test_parent_accepts_bounded_worker_refusals_for_diagnostics() -> None:
    source = HARNESS_PATH.read_text(encoding="utf-8")

    assert "accepted=frozenset({0, 2})" in source
    assert "worker refused for {case.name}: {reason}" in source
    assert "len(reason) > 1024" in source
    assert "(completed.stderr or completed.stdout)[:1024]" in source
    assert '" | ".join(diagnostic.splitlines())' in source


def test_mkfs_builder_accepts_only_numbered_loop_devices() -> None:
    harness = _load()
    exfat = harness.CASE_BY_NAME["wrong_label"]
    ext4 = harness.CASE_BY_NAME["wrong_filesystem"]

    assert harness._mkfs_command(exfat, Path("/dev/loop7")) == (
        "/usr/sbin/mkfs.exfat",
        "-n",
        "NOTDASHCAM",
        "/dev/loop7",
    )
    assert harness._mkfs_command(ext4, Path("/dev/loop12"))[-1] == "/dev/loop12"
    for unsafe in (
        Path("/dev/mmcblk0p3"),
        Path("/dev/loop-control"),
        Path("/dev/loop7/child"),
        Path("/tmp/image"),
    ):
        with pytest.raises(harness.HarnessError, match="loop"):
            harness._mkfs_command(exfat, unsafe)


def test_loop_setup_is_nooverlap_and_cleanup_is_explicit() -> None:
    source = HARNESS_PATH.read_text(encoding="utf-8")

    assert '(LOSETUP, "--find", "--show", "--nooverlap", str(image))' in source
    assert '(LOSETUP, "--detach", loop.as_posix())' in source
    assert "_wait_for_loop_snapshot(before_loops)" in source
    assert "for _ in range(50)" in source
    assert "time.sleep(0.1)" in source
    assert '"--json", "--list", "--output", "NAME,BACK-FILE,AUTOCLEAR"' in source
    assert "--autoclear" not in source


def test_harness_source_has_no_network_or_real_device_mutators() -> None:
    source = HARNESS_PATH.read_text(encoding="utf-8")

    assert "recording_root_facts_from_mapping(PosixFactsCollector().collect" in source
    assert "set_metadata=False" in source
    assert '_run((SYNC, "-f", str(mountpoint)), timeout=30)' in source
    assert 'systemctl", "stop' not in source
    assert 'systemctl", "restart' not in source
    assert 'nmcli", "connection", "up' not in source
    assert 'nmcli", "connection", "down' not in source
    assert 'ip", "link' not in source
    assert 'mkfs.exfat", "/dev/mmcblk' not in source
    assert 'mkfs.ext4", "/dev/mmcblk' not in source
    assert 'real_recording_device_formatted": False' in source
    assert 'real_recording_mount_mutated": False' in source


def test_readme_declares_prerequisites_and_private_namespace_safety() -> None:
    readme = HARNESS_PATH.with_name("README.md").read_text(encoding="utf-8")

    assert "does **not** unmount, remount, format, write to" in readme
    assert "private mount namespace" in readme
    assert "loop-backed sparse image" in readme
    assert "NetworkManager.service" in readme
    assert "ssh.service" in readme
    assert "/opt/dashcam/current/venv/bin/python" in readme
