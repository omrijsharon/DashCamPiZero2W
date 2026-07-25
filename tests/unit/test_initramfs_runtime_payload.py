from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
PAYLOAD = (
    ROOT / "deploy" / "image" / "payload" / "runtime" / "initramfs" / "dashcam-bounded-provision"
)
CONTRACTS = PAYLOAD.parent / "contracts"


def _script() -> str:
    return PAYLOAD.read_text(encoding="utf-8")


def _posix_shell_command() -> tuple[str, ...]:
    shell = shutil.which("sh")
    if shell is not None:
        return (shell, "-e")
    wsl = shutil.which("wsl.exe")
    if wsl is not None:
        return (wsl, "sh", "-e")
    pytest.skip("no POSIX shell is available for the initramfs runtime harness")


def _run_posix_harness(harness: str) -> None:
    completed = subprocess.run(
        _posix_shell_command(),
        input=harness.encode("utf-8"),
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")


def test_payload_is_posix_shell_and_supports_initramfs_prereqs() -> None:
    script = _script()
    assert script.startswith("#!/bin/sh\n")
    assert "PREREQ=" in script
    assert "prereqs()" in script
    assert 'case "${1:-}" in' in script
    assert ". /scripts/functions" in script
    assert 'resolve_device "$ROOT"' in script
    assert "/usr/bin/python" not in script


def test_payload_binds_the_exact_image_card_and_partition_geometry() -> None:
    script = _script()
    for value in (
        "fe34325344000000200000031a0192d1",
        "61440000",
        "4f2c9ea0-01",
        "4f2c9ea0-02",
        "89F4-4546",
        "e9ef4083-101b-46b4-b87d-de84fe1169f8",
        "/dev/mmcblk0",
        "/dev/mmcblk0p1",
        "/dev/mmcblk0p2",
        "start=16384,size=1048576,type=c",
        "start=1064960,size=4161536,type=83",
        "start=1064960,size=12582912,type=83",
        "start=13647872,size=47790080,type=7",
        "6442450944",
    ):
        assert value in script


def test_payload_requires_exact_trigger_gate_config_and_contracts() -> None:
    script = _script()
    assert "dashcam.bounded_provision=v1" in script
    assert "trigger_count" in script
    assert "resize_count" in script
    assert "stock resize token is forbidden" in script
    assert "/etc/dashcam/firstboot-runtime-v1.enabled" in script
    assert "EXACT_IMAGE_RUNTIME_VALIDATED=v1" in script
    assert "/etc/dashcam/firstboot-initramfs-v1.conf" in script
    assert "/etc/dashcam/source-table-v1.sfdisk" in script
    assert "/etc/dashcam/target-table-v1.sfdisk" in script
    assert "require_regular_exact" in script
    assert "normalize_table" in script


def test_payload_requires_absolute_executable_closure() -> None:
    script = _script()
    for executable in (
        "/usr/bin/busybox",
        "/usr/bin/cat",
        "/usr/bin/dd",
        "/usr/bin/findmnt",
        "/usr/bin/mount",
        "/usr/bin/sha256sum",
        "/usr/bin/sync",
        "/usr/bin/timeout",
        "/usr/bin/umount",
        "/usr/sbin/sfdisk",
        "/usr/sbin/blockdev",
        "/usr/sbin/blkid",
        "/usr/sbin/e2fsck",
        "/usr/sbin/resize2fs",
        "/usr/sbin/partprobe",
        "/usr/sbin/dumpe2fs",
    ):
        assert executable in script
    assert "for program in \\\n" in script
    assert '[ -x "$program" ]' in script


def test_payload_has_durable_backup_journal_and_one_reboot_bound() -> None:
    script = _script()
    for token in (
        "set -C",
        "source-first-sector-v1.bin",
        "source-table-v1.sfdisk",
        "source-backup-v1.sha256",
        "sha256sum -c",
        "source_backup_validated",
        "table_committed",
        "early_complete",
        "reboot_count",
        "0|1",
        "write_journal table_committed 1",
        "one permitted forced reboot",
        "/usr/bin/mv",
        "bounded_sync",
        "journal temporary file does not match the retried transition",
        "initramfs-v1.progress",
        "before_e2fsck:running",
        "before_resize2fs:running",
        "complete:success",
        "refusal:refused_?*",
        "phase=$progress_phase",
        "status=$progress_status",
        "/usr/bin/busybox rm -f --",
    ):
        assert token in script


def test_payload_has_exact_table_write_recheck_and_ext4_bounds() -> None:
    script = _script()
    for token in (
        "sfdisk --dump /dev/mmcblk0",
        "sfdisk --no-reread --force /dev/mmcblk0",
        '< "$TARGET_CONTRACT"',
        "sfdisk --verify /dev/mmcblk0",
        "blockdev --rereadpt /dev/mmcblk0",
        "partprobe /dev/mmcblk0",
        "e2fsck -p /dev/mmcblk0p2",
        "0|1)",
        "resize2fs /dev/mmcblk0p2",
        "dumpe2fs -h /dev/mmcblk0p2",
        "blockdev --getsize64 /dev/mmcblk0p2",
        "1572864:4096",
        "identity_before_table_$IDENTITY_FAILURE",
        "identity_after_target_$IDENTITY_FAILURE",
        "identity_before_ext4_$IDENTITY_FAILURE",
        "mounted boot filesystem is not vfat",
        "mounted boot filesystem lacks required option",
    ):
        assert token in script
    before_e2fsck = script.index("write_progress before_e2fsck running")
    e2fsck = script.index("/usr/sbin/e2fsck -p /dev/mmcblk0p2")
    before_resize = script.index("write_progress before_resize2fs running")
    resize = script.index("/usr/sbin/resize2fs /dev/mmcblk0p2")
    complete = script.rindex("write_progress complete success")
    assert before_e2fsck < e2fsck < before_resize < resize < complete
    assert "/usr/bin/timeout -k 10 120 /usr/sbin/e2fsck -p /dev/mmcblk0p2 >/dev/null 2>&1" in script
    assert "/usr/bin/timeout -k 10 120 /usr/sbin/resize2fs /dev/mmcblk0p2 >/dev/null 2>&1" in script
    assert "bounded ext4 growth failed with status $resize2fs_status" in script
    assert "fs_blocks * fs_block_size" not in script


def test_payload_reports_granular_identity_failures_and_retries_only_with_bounds() -> None:
    script = _script()
    identity_check_codes = (
        "root_token",
        "root_resolve",
        "root_readlink",
        "root_device_path",
        "root_partition",
        "root_start",
        "root_sysfs_parent",
        "card_cid",
        "card_capacity",
        "boot_partuuid",
        "root_partuuid",
        "boot_uuid",
        "boot_type",
        "root_uuid",
        "root_type",
    )
    for code in identity_check_codes:
        assert f"identity_fail {code}\n        return 1" in script
    for code in (
        "retry_sleep",
        "retry_bound",
    ):
        assert f"identity_fail {code}" in script
    assert "invalid_identity_check" in script
    assert "IDENTITY_RETRY_MAX=10" in script
    assert "IDENTITY_RETRY_SLEEP_SECONDS=1" in script
    assert "IDENTITY_RETRY_ATTEMPT=0" in script
    assert 'while [ "$identity_attempt" -le "$IDENTITY_RETRY_MAX" ]; do' in script
    assert "IDENTITY_RETRY_ATTEMPT=$identity_attempt" in script
    assert '[ "$identity_attempt" -lt "$IDENTITY_RETRY_MAX" ] || return 1' in script
    assert "/usr/bin/timeout 2 /usr/bin/sleep \"$IDENTITY_RETRY_SLEEP_SECONDS\"" in script
    assert (
        'identity_is_exact_with_retry ||\n'
        '        refuse "identity_settle_${IDENTITY_FAILURE}_a${IDENTITY_RETRY_ATTEMPT}'
        '_r${rereadpt_status}_p${partprobe_status}"'
        in script
    )
    assert 'identity_is_exact || refuse "identity_after_target_$IDENTITY_FAILURE"' in script
    assert 'identity_is_exact || refuse "identity_before_ext4_$IDENTITY_FAILURE"' in script
    assert script.count("identity_is_exact_with_retry ||") == 1
    assert "while true" not in script

    reread = script.index("blockdev --rereadpt /dev/mmcblk0")
    partprobe = script.index("partprobe /dev/mmcblk0")
    retry = script.index("identity_is_exact_with_retry ||")
    post_target = script.index('identity_is_exact || refuse "identity_after_target_')
    before_ext4 = script.index('identity_is_exact || refuse "identity_before_ext4_')
    e2fsck = script.index("/usr/sbin/e2fsck -p /dev/mmcblk0p2")
    assert reread < partprobe < retry < post_target < before_ext4 < e2fsck
    assert "rereadpt_status=$?" in script
    assert "if [ \"$rereadpt_status\" -ne 0 ]; then" in script
    assert "partprobe_status=none" in script
    assert "partprobe_status=$?" in script


def test_identity_first_failure_is_fail_closed_and_preserves_its_code() -> None:
    script = _script()
    identity_runtime = script[
        script.index("identity_is_exact()") : script.index("capture_actual_table()")
    ]
    harness = "\n".join(
        (
            "set -eu",
            "EXPECTED_ROOT_PARTUUID=4f2c9ea0-02",
            "ROOT=unexpected-root-token",
            identity_runtime,
            "if identity_is_exact; then exit 90; fi",
            '[ "$IDENTITY_FAILURE" = root_token ]',
        )
    )
    _run_posix_harness(harness)


def test_identity_retry_stops_on_success_and_at_its_exact_failure_bound() -> None:
    script = _script()
    retry_runtime = script[
        script.index("identity_fail()") : script.index("capture_actual_table()")
    ]

    transient_harness = "\n".join(
        (
            "set -eu",
            "IDENTITY_RETRY_MAX=10",
            "IDENTITY_RETRY_SLEEP_SECONDS=0",
            "IDENTITY_RETRY_ATTEMPT=0",
            retry_runtime,
            "transient_calls=0",
            "identity_is_exact() {",
            "    transient_calls=$((transient_calls + 1))",
            "    if [ \"$transient_calls\" -lt 3 ]; then",
            "        IDENTITY_FAILURE=root_start",
            "        return 1",
            "    fi",
            "    return 0",
            "}",
            "identity_is_exact_with_retry",
            '[ "$transient_calls" -eq 3 ]',
            '[ "$IDENTITY_RETRY_ATTEMPT" -eq 3 ]',
        )
    )
    _run_posix_harness(transient_harness)

    persistent_harness = "\n".join(
        (
            "set -eu",
            "IDENTITY_RETRY_MAX=10",
            "IDENTITY_RETRY_SLEEP_SECONDS=0",
            "IDENTITY_RETRY_ATTEMPT=0",
            retry_runtime,
            "persistent_calls=0",
            "identity_is_exact() {",
            "    persistent_calls=$((persistent_calls + 1))",
            "    IDENTITY_FAILURE=root_partuuid",
            "    return 1",
            "}",
            "if identity_is_exact_with_retry; then exit 90; fi",
            '[ "$persistent_calls" -eq 10 ]',
            '[ "$IDENTITY_RETRY_ATTEMPT" -eq 10 ]',
            '[ "$IDENTITY_FAILURE" = root_partuuid ]',
        )
    )
    _run_posix_harness(persistent_harness)


def test_payload_forbids_unbounded_or_out_of_scope_behaviour() -> None:
    script = _script()
    forbidden = (
        "eval ",
        "sh -c",
        "bash",
        "curl",
        "wget",
        "mkfs",
        "fsck.exfat",
        "mount /dev/mmcblk0p3",
        "mount -t exfat",
        "/srv/dashcam",
        "while true",
    )
    for token in forbidden:
        assert token not in script
    assert "exit 125" in script
    assert "/usr/bin/timeout -k 10 120 /usr/sbin/e2fsck" in script
    assert "/usr/sbin/e2fsck -f" not in script
    assert "/usr/bin/timeout -k 10 120 /usr/sbin/resize2fs" in script


def test_progress_refusal_is_bounded_best_effort_and_cannot_recurse() -> None:
    script = _script()
    refusal = script[script.index("refuse()") : script.index("require_regular_exact()")]
    progress = script[script.index("write_progress()") : script.index("validate_backup()")]
    assert 'write_progress refusal "refused_$refusal_status" || :' in refusal
    assert "return 1" in progress
    assert "refuse " not in progress
    assert "JOURNAL=$STATE_DIR/initramfs-v1.state" in script
    assert "PROGRESS=$STATE_DIR/initramfs-v1.progress" in script


def test_payload_does_not_create_or_ship_the_enable_gate() -> None:
    gate = PAYLOAD.parents[1] / "firstboot-runtime-v1.enabled"
    assert not gate.exists()
    assert "Deliberately disabled unless" in _script()


def test_checked_in_contracts_match_the_exact_embedded_contracts() -> None:
    script = _script()
    config = (CONTRACTS / "firstboot-initramfs-v1.conf").read_text(encoding="ascii").strip()
    source = (CONTRACTS / "source-table-v1.sfdisk").read_text(encoding="ascii")
    target = (CONTRACTS / "target-table-v1.sfdisk").read_text(encoding="ascii")

    def normalized_table(value: str) -> str:
        return "\n".join("".join(line.split()) for line in value.splitlines() if line.strip())

    assert config in script
    assert normalized_table(source) in script
    assert normalized_table(target) in script
    assert not (CONTRACTS / "firstboot-runtime-v1.enabled").exists()


def test_payload_shell_syntax_when_posix_shell_is_available() -> None:
    shell = shutil.which("sh")
    if shell is None:
        pytest.skip("no POSIX shell is available on this host")
    completed = subprocess.run(
        (shell, "-n", str(PAYLOAD)),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert completed.returncode == 0, completed.stderr
