from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from dashcam.provisioning.bootstrap import EXACT_STOCK_CARD_AUTHORIZATION, load_bootstrap_contract

ROOT = Path(__file__).resolve().parents[2]
PAYLOAD = ROOT / "deploy" / "bootstrap" / "ssh-dev"


def _load_armer() -> ModuleType:
    return _load_module("ssh_dev_armer", PAYLOAD / "arm-cmdline.py")


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_armer(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PAYLOAD / "arm-cmdline.py"), *args],
        check=False,
        text=True,
        capture_output=True,
    )


def test_contract_is_exactly_the_reviewed_stock_development_contract() -> None:
    authorization, policy = load_bootstrap_contract(
        (PAYLOAD / "authorized-exact-card-ssh-dev-v1.json").read_text(encoding="ascii")
    )
    assert authorization == EXACT_STOCK_CARD_AUTHORIZATION
    assert policy.root_target_bytes == 6 * 1024**3
    assert policy.minimum_device_bytes == 28 * 1024**3
    assert policy.minimum_data_bytes == 8 * 1024**3
    assert policy.alignment_bytes == policy.trailing_reserve_bytes == 1024**2


def test_armer_dry_run_apply_and_verify_preserve_newline_and_make_backup(tmp_path: Path) -> None:
    cmdline = tmp_path / "cmdline.txt"
    before = b"console=serial0,115200 root=PARTUUID=abcd-02 quiet\n"
    cmdline.write_bytes(before)
    backup = tmp_path / "dashcam-bootstrap"

    dry = _run_armer("--cmdline", str(cmdline), "--backup-dir", str(backup), "--dry-run")
    assert dry.returncode == 0, dry.stderr
    dry_value = json.loads(dry.stdout)
    assert dry_value["outcome"] == "would_apply"
    assert dry_value["before_sha256"] == hashlib.sha256(before).hexdigest()
    assert cmdline.read_bytes() == before

    applied = _run_armer(
        "--cmdline",
        str(cmdline),
        "--backup-dir",
        str(backup),
        "--apply",
        "--expected-before-sha256",
        hashlib.sha256(before).hexdigest(),
    )
    assert applied.returncode == 0, applied.stdout
    assert cmdline.read_bytes() == before[:-1] + b" dashcam.bootstrap=ssh-dev-v1\n"
    assert next(iter(backup.glob("cmdline.before-*.bak"))).read_bytes() == before

    verified = _run_armer("--cmdline", str(cmdline), "--verify")
    assert verified.returncode == 0
    assert json.loads(verified.stdout)["ready"] is True


@pytest.mark.parametrize(
    ("contents", "reason"),
    [
        (b"root=PARTUUID=abcd-02 resize", "forbidden"),
        (b"root=PARTUUID=abcd-02 dashcam.bootstrap=v1", "forbidden"),
        (b"root=PARTUUID=abcd-02 dashcam.bootstrap=ssh-dev-v1", "forbidden"),
        (b"console=serial0,115200", "lacks"),
    ],
)
def test_armer_refuses_unsafe_preconditions(tmp_path: Path, contents: bytes, reason: str) -> None:
    cmdline = tmp_path / "cmdline.txt"
    cmdline.write_bytes(contents)
    result = _run_armer("--cmdline", str(cmdline), "--dry-run")
    assert result.returncode == 2
    assert reason in json.loads(result.stdout)["reason"]
    assert cmdline.read_bytes() == contents


def test_armer_verify_rejects_an_unarmed_cmdline(tmp_path: Path) -> None:
    cmdline = tmp_path / "cmdline.txt"
    cmdline.write_bytes(b"root=PARTUUID=abcd-02")
    result = _run_armer("--cmdline", str(cmdline), "--verify")
    value = json.loads(result.stdout)
    assert result.returncode == 2
    assert value["outcome"] == "refused"
    assert value["counts"]["dev_trigger_count"] == 0


def test_armer_recovers_an_exact_backup_left_by_an_interrupted_run(tmp_path: Path) -> None:
    cmdline = tmp_path / "cmdline.txt"
    before = b"root=PARTUUID=abcd-02 quiet\n"
    cmdline.write_bytes(before)
    backup = tmp_path / "dashcam-bootstrap"
    backup.mkdir()
    name = f"cmdline.before-{hashlib.sha256(before).hexdigest()}.bak"
    (backup / name).write_bytes(before)

    result = _run_armer(
        "--cmdline",
        str(cmdline),
        "--backup-dir",
        str(backup),
        "--apply",
        "--expected-before-sha256",
        hashlib.sha256(before).hexdigest(),
    )
    assert result.returncode == 0, result.stdout
    assert json.loads(result.stdout)["outcome"] == "applied"


def _minimal_payload_repo(tmp_path: Path) -> Path:
    repository = tmp_path / "repo"
    source = repository / "deploy" / "bootstrap" / "ssh-dev"
    source.mkdir(parents=True)
    (repository / "src" / "dashcam" / "provisioning").mkdir(parents=True)
    for name in (
        "README.md",
        "arm-cmdline.py",
        "authorized-exact-card-ssh-dev-v1.json",
        "install.sh",
        "recover-exfat-reconciliation-refusal.py",
        "recover-fssize-refusal.py",
    ):
        shutil.copyfile(PAYLOAD / name, source / name)
    shutil.copyfile(
        ROOT / "src" / "dashcam" / "provisioning" / "bootstrap.py",
        repository / "src" / "dashcam" / "provisioning" / "bootstrap.py",
    )
    return repository


def test_prepare_payload_is_allowlisted_and_hash_closed(tmp_path: Path) -> None:
    output = tmp_path / "transfer"
    result = subprocess.run(
        [sys.executable, str(PAYLOAD / "prepare-payload.py"), str(ROOT), str(output.resolve())],
        check=False,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    expected = {
        "README.md",
        "SHA256SUMS",
        "arm-cmdline.py",
        "authorized-exact-card-ssh-dev-v1.json",
        "bootstrap.py",
        "install.sh",
        "recover-exfat-reconciliation-refusal.py",
        "recover-fssize-refusal.py",
    }
    assert {item.name for item in output.iterdir()} == expected
    for line in (output / "SHA256SUMS").read_text(encoding="ascii").splitlines():
        digest, name = line.split("  ")
        assert digest == hashlib.sha256((output / name).read_bytes()).hexdigest()


def test_prepare_refuses_unsafe_source_link(tmp_path: Path) -> None:
    prepare = _load_module("ssh_dev_prepare", PAYLOAD / "prepare-payload.py")
    repository = _minimal_payload_repo(tmp_path)
    arm = repository / "deploy" / "bootstrap" / "ssh-dev" / "arm-cmdline.py"
    arm.unlink()
    try:
        arm.symlink_to(PAYLOAD / "arm-cmdline.py")
    except OSError as exc:
        pytest.skip(f"host cannot create symlinks: {exc}")
    with pytest.raises(ValueError, match="non-symlink"):
        prepare.prepare(repository, (tmp_path / "transfer").resolve())


def test_prepare_refuses_repo_output_overlap(tmp_path: Path) -> None:
    prepare = _load_module("ssh_dev_prepare_overlap", PAYLOAD / "prepare-payload.py")
    clean_repository = _minimal_payload_repo(tmp_path / "clean")
    with pytest.raises(ValueError, match="overlap"):
        prepare.prepare(clean_repository, clean_repository / "transfer")


def test_prepare_refuses_a_multilink_source(tmp_path: Path) -> None:
    prepare = _load_module("ssh_dev_prepare_hardlink", PAYLOAD / "prepare-payload.py")
    repository = _minimal_payload_repo(tmp_path)
    arm = repository / "deploy" / "bootstrap" / "ssh-dev" / "arm-cmdline.py"
    try:
        os.link(arm, arm.with_name("arm-cmdline-copy.py"))
    except OSError as exc:
        pytest.skip(f"host cannot create hard links: {exc}")
    with pytest.raises(ValueError, match="unsafe type or links"):
        prepare.prepare(repository, (tmp_path / "transfer").resolve())


def test_installer_has_closed_manifest_and_no_runtime_or_storage_actions() -> None:
    installer = (PAYLOAD / "install.sh").read_text(encoding="utf-8")
    assert "SHA256SUMS" in installer
    assert "sha256sum -c --status" in installer
    assert "install -d -o root -g dashcam-storage -m 0550 /srv/dashcam" in installer
    assert "install -d -o root -g dashcam-storage -m 0770 /srv/dashcam" not in installer
    for forbidden in ("apt-get", "systemctl", "reboot", "sfdisk", "mkfs", "bootstrap.py --"):
        assert forbidden not in installer
    assert "DASHCAM_OFFLINE_ROOT" in installer
    assert "--cmdline" not in installer
    assert "safe_payload_file" in installer
    assert "/etc/group" in installer and "/etc/passwd" in installer
    assert 'install -o root -g dashcam-storage -m 0640 "$PAYLOAD/bootstrap.py"' in installer
    assert 'install -o root -g dashcam-storage -m 0750 "$PAYLOAD/arm-cmdline.py"' in installer
    assert (
        'install -o root -g dashcam-storage -m 0750 "$PAYLOAD/recover-fssize-refusal.py"'
        in installer
    )
    assert '"$PAYLOAD/recover-exfat-reconciliation-refusal.py"' in installer


def test_readme_reboots_and_reconnects_before_stage_a_dry_run() -> None:
    readme = (PAYLOAD / "README.md").read_text(encoding="utf-8")
    reboot = readme.index("sudo reboot")
    reconnect = readme.index("StrictHostKeyChecking=yes", reboot)
    stage_a = readme.index("bootstrap.py --stage a", reconnect)
    assert reboot < reconnect < stage_a
    assert "--stage a --contract" in readme
    assert "DO NOT RUN YET" in readme
    assert "/usr/bin/python3 /opt/dashcam-bootstrap/bootstrap.py --stage a" in readme
    assert "sudo /opt/dashcam-bootstrap/bootstrap.py" not in readme


def test_armer_module_exports_the_reviewed_default_path() -> None:
    armer = _load_armer()
    assert Path("/boot/firmware/cmdline.txt") == armer.DEFAULT_CMDLINE
