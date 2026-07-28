from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "deploy/ssh-dev-validation/exfat-fsck/run.sh"
BENCHMARK = ROOT / "deploy/ssh-dev-validation/exfat-fsck/benchmark.sh"
README = ROOT / "deploy/ssh-dev-validation/exfat-fsck/README.md"


def test_fsck_harness_is_private_bounded_and_loop_only() -> None:
    script = HARNESS.read_text(encoding="utf-8")
    assert "unshare --mount --propagation private" in script
    assert "mount --make-rprivate /" in script
    assert "mktemp -d /var/tmp/dashcam-exfat-fsck." in script
    assert "EXPECTED_LABEL=DCFSCKTEST" in script
    assert "trap cleanup EXIT" in script
    assert "trap 'exit 130' HUP INT TERM" in script
    for option in ("losetup", "--find", "--show", "--nooverlap"):
        assert option in script
    assert 'case "$ACTIVE_LOOP" in' in script
    assert "/dev/loop[0-9]*" in script
    assert "BACK-FILE" in script
    assert "/usr/sbin/blockdev --getsize64" in script
    assert "/usr/bin/timeout -k" in script
    assert "ulimit -f 128" in script
    assert "externally supplied targets are forbidden" in script
    for forbidden in ("/dev/mmcblk0", "/srv/dashcam"):
        assert forbidden not in script


def test_fsck_matrix_records_hashes_signatures_and_closed_outcomes() -> None:
    script = HARNESS.read_text(encoding="utf-8")
    assert "for case_name in clean repairable failed" in script
    assert "image_sha256=" in script
    assert "wipefs --json" in script
    assert "blkid -p -o export" in script
    assert "fsck.exfat" in script
    assert "run_fsck clean -n" in script
    assert "run_fsck repairable -n" in script
    assert "seek=5632" in script
    assert "run_fsck repairable -y" in script
    assert "run_fsck repairable-final -n" in script
    assert "run_fsck failed -p" in script
    assert '"$failed_before" = "$failed_after"' in script
    assert "unrepairable image unexpectedly passed/repaired" in script


def test_formatter_is_seed_only_and_never_an_fsck_recovery_action() -> None:
    script = HARNESS.read_text(encoding="utf-8")
    assert script.count("/usr/sbin/mkfs.exfat") == 1
    mkfs = script.index("/usr/sbin/mkfs.exfat")
    clones = script.index("for case_name in clean repairable failed")
    first_fsck = script.index("run_fsck clean -n")
    assert mkfs < clones < first_fsck
    assert "seed_mkfs_count=1" in script
    assert "auto_format_count=0_for_all_fsck_cases" in script
    assert "mkfs" not in script[first_fsck:]


def test_benchmark_is_unique_reserve_gated_bounded_and_exactly_cleaned() -> None:
    script = BENCHMARK.read_text(encoding="utf-8")
    assert 'readonly TEST_DIR="$ROOT/.validation-write-$TOKEN"' in script
    assert "TEST_BYTES=$((640 * 1024 * 1024))" in script
    assert "capacity * 15 / 100" in script
    assert "TEST_BYTES + required_reserve" in script
    assert "externally supplied paths are forbidden" in script
    assert "systemctl is-active --quiet dashcamd.service" in script
    assert '"/dev/mmcblk0p3"' in script
    assert '"fstype": "exfat"' in script
    assert '"label": "DASHCAM"' in script
    assert "conv=fdatasync" in script
    assert "/usr/bin/sync -f" in script
    assert "write_latency_ns=" in script
    assert "finalize_latency_ns=" in script
    assert "trap cleanup EXIT" in script
    assert "trap 'exit 130' HUP INT TERM" in script
    assert "/usr/bin/rm -rf" not in script
    assert '/usr/bin/rmdir -- "$TEST_DIR"' in script
    assert 'stat -c %d -- "$TEST_DIR"' in script
    assert 'stat -c %i -- "$TEST_DIR"' in script
    assert "for index in 0 1 2 3 4 5 6 7" in script


def test_readme_keeps_loop_harness_and_real_volume_benchmark_separate() -> None:
    text = README.read_text(encoding="utf-8")
    assert "destructive only to three 64 MiB regular image copies" in text
    assert "No fsck case invokes a formatter" in text
    assert "never a loop image" in text
    assert "caps total test data at 640 MiB" in text
    assert "tee /tmp/dashcam-exfat-fsck-evidence.txt" in text
    assert "tee /tmp/dashcam-exfat-write-evidence.txt" in text
