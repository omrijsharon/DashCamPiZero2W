from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SCRIPT = (
    ROOT / "deploy" / "image" / "payload" / "runtime" / "post-root" / "dashcam-firstboot-storage"
)
UNIT = SCRIPT.with_suffix(".service")
CONTRACTS = SCRIPT.parents[1] / "initramfs" / "contracts"


def _script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_post_root_runtime_is_posix_shell_and_has_no_python_or_eval() -> None:
    script = _script()
    assert script.startswith("#!/bin/sh\n")
    assert "set -efu" in script
    assert "/usr/bin/python" not in script
    assert not re.search(r"(^|[;\s])eval([;\s]|$)", script)
    assert "curl" not in script
    assert "wget" not in script


def test_post_root_requires_exact_card_contracts_and_early_journal() -> None:
    script = _script()
    for value in (
        "/etc/dashcam/firstboot-runtime-v1.enabled",
        "EXACT_IMAGE_RUNTIME_VALIDATED=v1",
        "/etc/dashcam/expendable-card-v1.authorized",
        "fe34325344000000200000031a0192d1",
        "EXPECTED_DISK_SECTORS=61440000",
        "EARLY_STATE=$EARLY_DIR/initramfs-v1.state",
        "phase=early_complete",
        "early journal must contain exactly five lines",
        "/etc/dashcam/source-table-v1.sfdisk",
        "/etc/dashcam/target-table-v1.sfdisk",
        "partition table is not the exact target geometry",
        "require_exact_contents",
    ):
        assert value in script
    assert script.index("validate_authorization") < script.index("/usr/sbin/mkfs.exfat -L DASHCAM")
    for contract_name in (
        "firstboot-initramfs-v1.conf",
        "source-table-v1.sfdisk",
        "target-table-v1.sfdisk",
    ):
        exact_contract = (CONTRACTS / contract_name).read_text(encoding="ascii").rstrip("\n")
        assert exact_contract in script


def test_post_root_format_path_is_bounded_and_fail_closed() -> None:
    script = _script()
    assert "/usr/bin/timeout 15 /usr/sbin/wipefs --no-act" in script
    assert "/usr/bin/timeout 30 /usr/sbin/wipefs --all /dev/mmcblk0p3" in script
    assert "/usr/bin/timeout 120 /usr/sbin/mkfs.exfat -L DASHCAM /dev/mmcblk0p3" in script
    assert "pre-wipe-signatures-v1.txt" in script
    assert "existing formatted volume lacks an exact creation journal" in script
    assert script.count("mkfs.exfat -L DASHCAM") == 1
    assert "exit 125" in script


def test_post_root_retry_contract_covers_both_format_power_loss_windows() -> None:
    script = _script()
    assert "write_post_state format_intent none" in script
    assert 'write_post_state formatted "$DATA_UUID"' in script
    assert "require_format_recovery_binding" in script
    recovery = script[script.index("require_format_recovery_binding()") :]
    assert "format_intent)" in recovery
    assert "formatted)" in recovery
    assert "validate_authorization" in recovery
    assert 'require_regular_exact "$PREWIPE"' in recovery
    assert "formatted journal UUID does not match" in recovery
    assert "post-root journal contradicts the existing exFAT UUID" in script
    assert "retry signatures contradict the durable pre-wipe evidence" in script
    assert script.index("write_post_state format_intent none") < script.index(
        "/usr/sbin/wipefs --all /dev/mmcblk0p3"
    )
    assert script.index("require_format_recovery_binding") < script.index(
        'durable_replace "$SENTINEL"'
    )


def test_foreign_preexisting_exfat_without_exact_creation_state_is_refused() -> None:
    script = _script()
    assert "POST_PHASE=none" in script
    assert "existing formatted volume lacks an exact creation journal" in script
    assert "an existing data filesystem is not the exact DASHCAM exFAT volume" in script
    assert "root marker exists without the volume sentinel" in script
    existing_branch = script[script.index('if [ -n "$data_type" ]') :]
    assert existing_branch.index(
        "existing formatted volume lacks an exact creation journal"
    ) < existing_branch.index("ensure_accounts")


def test_post_root_journal_does_not_regress_and_markers_match_the_phase() -> None:
    script = _script()
    assert "root marker contradicts the post-root journal phase" in script
    assert "complete journal exists without the durable root marker" in script
    assert "durable journal claims a missing volume sentinel" in script
    assert "post-root journal cannot advance after sentinel verification" in script
    assert "post-root journal cannot advance after marker verification" in script
    assert "POST_PHASE=sentinel_durable" in script
    assert "POST_PHASE=complete" in script


def test_post_root_creates_and_reconciles_closed_service_identities() -> None:
    script = _script()
    assert "/usr/sbin/groupadd --system dashcam" in script
    assert "/usr/sbin/groupadd --system dashcam-storage" in script
    assert "--home-dir /nonexistent --shell /usr/sbin/nologin dashcam" in script
    assert "/usr/sbin/usermod -a -G dashcam-storage dashcam" in script
    assert "existing dashcam user conflicts with the closed service identity" in script
    assert "declared groups are not system-group identities" in script
    assert "dashcam is not a system-user identity" in script
    assert "uid=$DASHCAM_UID,gid=$STORAGE_GID" in script


def test_post_root_mount_marker_fstab_and_cmdline_contracts_are_exact() -> None:
    script = _script()
    expected_options = (
        "noatime,nosuid,nodev,noexec,uid=$DASHCAM_UID,gid=$STORAGE_GID,"
        "umask=0007,nofail,x-systemd.device-timeout=10s"
    )
    assert expected_options in script
    assert "SENTINEL=$MOUNT_POINT/.dashcam-volume" in script
    assert "ROOT_MARKER=$STATE_DIR/layout-v1.complete.json" in script
    assert (
        "EXPECTED_SOURCE_FINGERPRINT="
        "17eee4a5eb7d0641bf6ea6a2013ff5203c09aa72b7420ff990bd82ec08406ae6"
    ) in script
    assert "e80659000879b7afe6b6efac1ce091e6c518c83dbfa759345a0fb6bff90af8eb" not in script
    assert '\\"root_end_sector\\":13647871' in script
    assert '\\"data_start_sector\\":13647872' in script
    assert '\\"data_end_sector\\":61437951' in script
    assert "durable_replace" in script
    assert script.rindex('validate_identity_payload "$ROOT_MARKER"') < script.rindex(
        "remove_bounded_trigger"
    )
    assert '[ "$token" = dashcam.bounded_provision=v1 ]' in script


def test_post_root_unit_is_bounded_gated_and_ordered_before_recorder() -> None:
    unit = UNIT.read_text(encoding="utf-8")
    assert "ConditionKernelCommandLine=dashcam.bounded_provision=v1" in unit
    assert "ConditionPathExists=/etc/dashcam/firstboot-runtime-v1.enabled" in unit
    assert "RequiresMountsFor=/boot/firmware" in unit
    assert "DefaultDependencies=no" in unit
    assert "Before=local-fs.target srv-dashcam.mount" in unit
    assert "Before=dashcamd.service" in unit
    assert "TimeoutStartSec=5min" in unit
    assert "WantedBy=local-fs.target" in unit


def test_post_root_script_passes_wsl_dash_syntax_check() -> None:
    result = subprocess.run(
        [
            "wsl.exe",
            "-e",
            "sh",
            "-n",
            str(SCRIPT).replace("\\", "/").replace("C:", "/mnt/c"),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode == 127:
        pytest.skip("WSL is unavailable")
    assert result.returncode == 0, result.stderr
