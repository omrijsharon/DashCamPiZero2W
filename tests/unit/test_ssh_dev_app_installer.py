from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "deploy" / "ssh-dev-app"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _wheel(path: Path, *, name: str, version: str) -> None:
    normalized = name.replace("-", "_")
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\n"

    def write(archive: zipfile.ZipFile, member: str, payload: str) -> None:
        info = zipfile.ZipInfo(member, date_time=(2020, 1, 1, 0, 0, 0))
        archive.writestr(info, payload)

    with zipfile.ZipFile(path, "w") as archive:
        write(archive, f"{normalized}-{version}.dist-info/METADATA", metadata)
        write(
            archive,
            f"{normalized}-{version}.dist-info/WHEEL",
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )


def _minimal_repo(tmp_path: Path) -> Path:
    repository = tmp_path / "repo"
    for relative in (
        "deploy/ssh-dev-app/README.md",
        "deploy/ssh-dev-app/install.py",
        "deploy/ssh-dev-app/dashcam-network-fallback.service",
        "deploy/ssh-dev-app/dashcam-storage-check.service",
        "systemd/dashcamd.service",
        "deploy/bootstrap/image/pi-gen-stage/00-packages",
        "config/default.toml",
    ):
        target = repository / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    return repository


def _bundle(tmp_path: Path, *, config_suffix: bytes = b"") -> tuple[ModuleType, Path]:
    builder = _load(f"app_builder_{tmp_path.name}", SOURCE / "prepare-bundle.py")
    repository = _minimal_repo(tmp_path)
    if config_suffix:
        config = repository / "config/default.toml"
        config.write_bytes(config.read_bytes() + config_suffix)
    app = tmp_path / "dashcam_pizero2w-0.1.0.dev0-py3-none-any.whl"
    tzdata = tmp_path / "tzdata-2026.3-py2.py3-none-any.whl"
    _wheel(app, name="dashcam-pizero2w", version="0.1.0.dev0")
    _wheel(tzdata, name="tzdata", version="2026.3")
    tzdata_payload = tzdata.read_bytes()
    builder.__dict__["TZDATA_WHEEL_SHA256"] = hashlib.sha256(tzdata_payload).hexdigest()
    builder.__dict__["TZDATA_WHEEL_SIZE"] = len(tzdata_payload)
    output = tmp_path / "bundle"
    builder.prepare(
        repository.resolve(), output.resolve(), tzdata.resolve(), app_wheel=app.resolve()
    )
    return builder, output


def _rehash_bundle(bundle: Path) -> None:
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="ascii"))
    for name in manifest["files"]:
        payload = (bundle / name).read_bytes()
        manifest["files"][name] = {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }
    manifest_payload = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "ascii"
    )
    manifest_path.write_bytes(manifest_payload)
    sums = {
        **{name: details["sha256"] for name, details in manifest["files"].items()},
        "manifest.json": hashlib.sha256(manifest_payload).hexdigest(),
    }
    (bundle / "SHA256SUMS").write_text(
        "".join(f"{digest}  {name}\n" for name, digest in sorted(sums.items())),
        encoding="ascii",
        newline="\n",
    )


def test_builder_creates_exact_hash_closed_bundle_from_canonical_packages(
    tmp_path: Path,
) -> None:
    _, bundle = _bundle(tmp_path)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="ascii"))
    expected_packages = [
        line
        for line in (ROOT / "deploy/bootstrap/image/pi-gen-stage/00-packages")
        .read_text(encoding="ascii")
        .splitlines()
        if line
    ]
    assert manifest["apt_packages"] == expected_packages
    assert manifest["tzdata"]["version"] == "2026.3"
    assert manifest["install_budget_bytes"] == 512 * 1024**2
    assert set(manifest["files"]) == {
        "README.md",
        "apt-packages.txt",
        "config.toml",
        "dashcam-network-fallback.service",
        "dashcam-storage-check.service",
        "dashcamd.service",
        "install.py",
        manifest["application"]["wheel"],
        manifest["tzdata"]["wheel"],
    }
    sums = {}
    for line in (bundle / "SHA256SUMS").read_text(encoding="ascii").splitlines():
        digest, name = line.split("  ")
        sums[name] = digest
        assert hashlib.sha256((bundle / name).read_bytes()).hexdigest() == digest
    assert set(sums) == {*manifest["files"], "manifest.json"}
    for source, relative in (
        (
            tmp_path / "dashcam_pizero2w-0.1.0.dev0-py3-none-any.whl",
            manifest["application"]["wheel"],
        ),
        (
            tmp_path / "tzdata-2026.3-py2.py3-none-any.whl",
            manifest["tzdata"]["wheel"],
        ),
    ):
        source_payload = source.read_bytes()
        copied_payload = (bundle / relative).read_bytes()
        assert len(copied_payload) == len(source_payload)
        assert hashlib.sha256(copied_payload).digest() == hashlib.sha256(source_payload).digest()
        assert manifest["files"][relative] == {
            "sha256": hashlib.sha256(source_payload).hexdigest(),
            "size": len(source_payload),
        }


def test_current_repository_real_wheel_imports_the_production_smoke_modules(
    tmp_path: Path,
) -> None:
    """Exercise a real built wheel, rather than the metadata-only wheel fixture."""

    wheelhouse = tmp_path / "wheelhouse"
    build = subprocess.run(
        ["uv", "build", "--offline", "--wheel", "--out-dir", str(wheelhouse)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert build.returncode == 0, build.stderr
    wheels = list(wheelhouse.glob("dashcam_pizero2w-*.whl"))
    assert len(wheels) == 1

    venv = tmp_path / "venv"
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    venv_python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run(
        [str(venv_python), "-m", "pip", "install", "--no-index", "--no-deps", str(wheels[0])],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    installer = _load("app_installer_real_wheel_smoke", SOURCE / "install.py")
    smoke = subprocess.run(
        [str(venv_python), "-c", installer.APPLICATION_IMPORT_SMOKE],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert smoke.returncode == 0, smoke.stderr


def test_canonical_packages_include_sorted_pygobject_gstreamer_runtime() -> None:
    packages = (
        (ROOT / "deploy/bootstrap/image/pi-gen-stage/00-packages")
        .read_text(encoding="ascii")
        .splitlines()
    )
    assert packages == sorted(packages)
    assert {
        "gir1.2-gst-plugins-base-1.0",
        "gir1.2-gstreamer-1.0",
        # M7's audio factory contract is already covered by the reviewed
        # package declaration: ALSA capture, base conversion/resampling,
        # AAC parsing, and the selected vo-aac encoder.  Adding packages here
        # would alter the bounded deployment plan without a missing-factory
        # refusal on the exact target.
        "gstreamer1.0-alsa",
        "gstreamer1.0-plugins-base",
        "gstreamer1.0-plugins-good",
        "gstreamer1.0-plugins-ugly",
        "python3-gst-1.0",
        "python3-gi",
    } <= set(packages)
    assert "gstreamer1.0-x" not in packages


def test_builder_pins_exact_uv_lock_tzdata_wheel_identity() -> None:
    builder = _load("app_builder_tzdata_pin", SOURCE / "prepare-bundle.py")
    assert builder.__dict__["TZDATA_VERSION"] == "2026.3"
    assert builder.__dict__["TZDATA_WHEEL_SIZE"] == 348_168
    assert (
        builder.__dict__["TZDATA_WHEEL_SHA256"]
        == "dc096730c87af6cab1b171c9d532be840741ff5d459015e7f6947bd7d7e54931"
    )


def test_release_id_binds_config_and_all_closed_manifest_inputs(tmp_path: Path) -> None:
    _, first = _bundle(tmp_path / "first")
    _, second = _bundle(tmp_path / "second", config_suffix=b"\n# changed input\n")
    first_manifest = json.loads((first / "manifest.json").read_text(encoding="ascii"))
    second_manifest = json.loads((second / "manifest.json").read_text(encoding="ascii"))
    assert first_manifest["application"] == second_manifest["application"]
    assert {
        key: value for key, value in first_manifest["files"].items() if key != "config.toml"
    } == {key: value for key, value in second_manifest["files"].items() if key != "config.toml"}
    assert first_manifest["release_id"] != second_manifest["release_id"]


def test_checked_read_loops_until_eof_when_os_returns_short_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder = _load("app_builder_short_reads", SOURCE / "prepare-bundle.py")
    source = tmp_path / "large.whl"
    payload = bytes(range(251)) * 4096
    source.write_bytes(payload)
    real_read = cast(Callable[[int, int], bytes], builder.os.read)

    def short_read(descriptor: int, count: int) -> bytes:
        return real_read(descriptor, min(count, 17))

    monkeypatch.setattr(builder.os, "read", short_read)
    observed = builder.checked_read(source)
    assert len(observed) == len(payload)
    assert hashlib.sha256(observed).digest() == hashlib.sha256(payload).digest()


def test_builder_requires_exact_offline_tzdata_wheel(tmp_path: Path) -> None:
    builder = _load("app_builder_wrong_tz", SOURCE / "prepare-bundle.py")
    repository = _minimal_repo(tmp_path)
    app = tmp_path / "dashcam.whl"
    wrong = tmp_path / "tzdata.whl"
    _wheel(app, name="dashcam-pizero2w", version="0.1")
    _wheel(wrong, name="tzdata", version="2026.2")
    with pytest.raises(ValueError, match=r"exactly tzdata 2026\.3"):
        builder.prepare(
            repository.resolve(),
            (tmp_path / "out").resolve(),
            wrong.resolve(),
            app_wheel=app.resolve(),
        )


def test_builder_rejects_same_version_tzdata_with_wrong_locked_identity(
    tmp_path: Path,
) -> None:
    builder = _load("app_builder_wrong_tz_identity", SOURCE / "prepare-bundle.py")
    repository = _minimal_repo(tmp_path)
    app = tmp_path / "dashcam.whl"
    wrong = tmp_path / "tzdata-2026.3-py2.py3-none-any.whl"
    _wheel(app, name="dashcam-pizero2w", version="0.1")
    _wheel(wrong, name="tzdata", version="2026.3")
    with pytest.raises(ValueError, match=r"exact uv\.lock wheel identity"):
        builder.prepare(
            repository.resolve(),
            (tmp_path / "out").resolve(),
            wrong.resolve(),
            app_wheel=app.resolve(),
        )


def test_installer_manifest_accepts_bundle_and_refuses_tampering(tmp_path: Path) -> None:
    _, bundle = _bundle(tmp_path)
    installer = _load("app_installer_manifest", SOURCE / "install.py")
    value = installer._manifest(bundle.resolve())
    assert value["application"]["name"] == "dashcam-pizero2w"

    config = bundle / "config.toml"
    config.write_bytes(config.read_bytes() + b"\n# changed\n")
    with pytest.raises(installer.Refusal, match="hash/size mismatch"):
        installer._manifest(bundle.resolve())


def test_installer_manifest_refuses_rehashed_network_fallback_contract_drift(
    tmp_path: Path,
) -> None:
    _, bundle = _bundle(tmp_path)
    installer = _load("app_installer_network_fallback_contract", SOURCE / "install.py")
    unit_name = "dashcam-network-fallback.service"
    unit = bundle / unit_name
    unit.write_text(
        unit.read_text(encoding="ascii").replace("TimeoutStartSec=120s", "TimeoutStartSec=121s"),
        encoding="ascii",
        newline="\n",
    )
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="ascii"))
    manifest["files"][unit_name] = {
        "sha256": hashlib.sha256(unit.read_bytes()).hexdigest(),
        "size": unit.stat().st_size,
    }
    manifest_payload = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "ascii"
    )
    manifest_path.write_bytes(manifest_payload)
    sums = {
        **{name: details["sha256"] for name, details in manifest["files"].items()},
        "manifest.json": hashlib.sha256(manifest_payload).hexdigest(),
    }
    (bundle / "SHA256SUMS").write_text(
        "".join(f"{digest}  {name}\n" for name, digest in sorted(sums.items())),
        encoding="ascii",
        newline="\n",
    )
    with pytest.raises(installer.Refusal, match="network fallback unit contract differs"):
        installer._manifest(bundle.resolve())


def test_installer_refuses_a_symlinked_payload_file(tmp_path: Path) -> None:
    _, bundle = _bundle(tmp_path)
    installer = _load("app_installer_symlink", SOURCE / "install.py")
    config = bundle / "config.toml"
    original = tmp_path / "original-config.toml"
    config.replace(original)
    try:
        config.symlink_to(original)
    except OSError as exc:
        pytest.skip(f"host cannot create symlinks: {exc}")
    with pytest.raises(installer.Refusal, match="unsafe type"):
        installer._manifest(bundle.resolve())


def test_headroom_gate_requires_two_gib_after_fixed_budget() -> None:
    installer = _load("app_installer_headroom", SOURCE / "install.py")
    gib = 1024**3
    assert installer._projected_headroom(3 * gib, gib) == 2 * gib
    with pytest.raises(installer.Refusal, match="below 2 GiB"):
        installer._projected_headroom(3 * gib - 1, gib)


def test_os_release_target_accepts_only_exact_stock_relative_symlink() -> None:
    installer = _load("app_installer_os_release_shape", SOURCE / "install.py")
    assert str(installer._os_release_target("../usr/lib/os-release")) == "/usr/lib/os-release"
    for foreign in (
        "/usr/lib/os-release",
        "../usr/lib/./os-release",
        "../../usr/lib/os-release",
        "../usr/lib/other",
        "",
    ):
        with pytest.raises(installer.Refusal, match="symlink target differs"):
            installer._os_release_target(foreign)


def test_os_release_reader_accepts_exact_symlink_and_refuses_broken_target(
    tmp_path: Path,
) -> None:
    installer = _load("app_installer_os_release_reader", SOURCE / "install.py")
    etc = tmp_path / "etc"
    canonical = tmp_path / "usr/lib/os-release"
    etc.mkdir()
    canonical.parent.mkdir(parents=True)
    canonical.write_text(
        'ID=raspbian\nVERSION_ID="13"\nVERSION_CODENAME=trixie\n',
        encoding="ascii",
    )
    link = etc / "os-release"
    try:
        link.symlink_to("../usr/lib/os-release")
    except OSError as exc:
        pytest.skip(f"host cannot create symlinks: {exc}")
    payload = installer._read_os_release(tmp_path)
    assert installer._os_release(payload)["ID"] == "raspbian"
    canonical.unlink()
    with pytest.raises(installer.Refusal, match="required file is absent"):
        installer._read_os_release(tmp_path)


def test_os_release_reader_refuses_regular_noncanonical_path(tmp_path: Path) -> None:
    installer = _load("app_installer_os_release_regular", SOURCE / "install.py")
    path = tmp_path / "etc/os-release"
    path.parent.mkdir()
    path.write_text("ID=raspbian\n", encoding="ascii")
    with pytest.raises(installer.Refusal, match="not the reviewed symlink"):
        installer._read_os_release(tmp_path)


def test_sysfs_cid_reader_ignores_nominal_size_and_handles_short_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = _load("app_installer_sysfs_cid", SOURCE / "install.py")
    cid = tmp_path / "sys/class/block/mmcblk0/device/cid"
    cid.parent.mkdir(parents=True)
    cid.write_bytes(b"fe34325344000000200000031a0192d1\n")
    real_fstat = cast(Callable[[int], os.stat_result], installer.os.fstat)
    real_read = cast(Callable[[int, int], bytes], installer.os.read)

    def nominal_fstat(descriptor: int) -> os.stat_result:
        values = list(real_fstat(descriptor))
        values[6] = 4096
        return os.stat_result(values)

    def short_read(descriptor: int, count: int) -> bytes:
        return real_read(descriptor, min(count, 5))

    monkeypatch.setattr(installer.os, "fstat", nominal_fstat)
    monkeypatch.setattr(installer.os, "read", short_read)
    assert installer._read_sysfs_cid(tmp_path) == "fe34325344000000200000031a0192d1"


def test_sysfs_pseudo_reader_refuses_oversized_content_and_unsafe_type(
    tmp_path: Path,
) -> None:
    installer = _load("app_installer_sysfs_bounds", SOURCE / "install.py")
    oversized = tmp_path / "oversized"
    oversized.write_bytes(b"x" * 129)
    with pytest.raises(installer.Refusal, match="exceeds its content bound"):
        installer._safe_pseudo_read(oversized, 128)
    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(installer.Refusal, match="unsafe type"):
        installer._safe_pseudo_read(directory, 128)


def test_sysfs_pseudo_reader_refuses_symlink(tmp_path: Path) -> None:
    installer = _load("app_installer_sysfs_link", SOURCE / "install.py")
    target = tmp_path / "target"
    target.write_bytes(b"value")
    link = tmp_path / "link"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"host cannot create symlinks: {exc}")
    with pytest.raises(installer.Refusal, match="unsafe type"):
        installer._safe_pseudo_read(link, 128)


def test_current_release_switch_is_idempotent_and_refuses_foreign_paths(tmp_path: Path) -> None:
    installer = _load("app_installer_current", SOURCE / "install.py")
    opt = tmp_path / "opt/dashcam"
    release = opt / "releases/one"
    release.mkdir(parents=True)
    (release / "installed.json").write_text("{}\n", encoding="ascii")
    try:
        installer._switch_current(tmp_path, release)
    except OSError as exc:
        pytest.skip(f"host cannot create symlinks: {exc}")
    assert os.readlink(opt / "current") == "releases/one"
    installer._switch_current(tmp_path, release)
    assert os.readlink(opt / "current") == "releases/one"

    (opt / "current").unlink()
    (opt / "current").write_text("foreign", encoding="ascii")
    with pytest.raises(installer.Refusal, match="foreign"):
        installer._switch_current(tmp_path, release)


def test_managed_file_install_is_idempotent_and_refuses_foreign_content(
    tmp_path: Path,
) -> None:
    installer = _load("app_installer_managed_file", SOURCE / "install.py")
    source = tmp_path / "source"
    target = tmp_path / "managed/target"
    source.write_bytes(b"reviewed\n")
    desired = hashlib.sha256(source.read_bytes()).hexdigest()
    installer._install_managed_file(
        source, target, 0o640, expected_sha256=None, desired_sha256=desired
    )
    first = target.read_bytes()
    installer._install_managed_file(
        source, target, 0o640, expected_sha256=desired, desired_sha256=desired
    )
    assert target.read_bytes() == first
    target.write_bytes(b"foreign\n")
    with pytest.raises(installer.Refusal, match="prestate drifted"):
        installer._install_managed_file(
            source, target, 0o640, expected_sha256=desired, desired_sha256=desired
        )


def test_managed_file_rechecks_the_bound_hash_immediately_before_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = _load("app_installer_managed_file_toc_tou", SOURCE / "install.py")
    source = tmp_path / "source"
    target = tmp_path / "managed/target"
    source.write_bytes(b"new\n")
    target.parent.mkdir()
    target.write_bytes(b"old\n")
    expected = hashlib.sha256(b"old\n").hexdigest()
    desired = hashlib.sha256(b"new\n").hexdigest()
    original_hash = installer._managed_target_hash
    calls = 0

    def drift_before_replace(path: Path) -> str | None:
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_bytes(b"drift\n")
        return cast(str | None, original_hash(path))

    monkeypatch.setattr(installer, "_managed_target_hash", drift_before_replace)
    with pytest.raises(installer.Refusal, match="prestate drifted"):
        installer._install_managed_file(
            source, target, 0o640, expected_sha256=expected, desired_sha256=desired
        )
    assert target.read_bytes() == b"drift\n"


def test_managed_config_uses_storage_group_and_release_tree_is_service_traversable(
    tmp_path: Path,
) -> None:
    installer = _load("app_installer_permissions", SOURCE / "install.py")
    owners: list[tuple[Path, int, int, bool]] = []

    def record_owner(path: Path, uid: int, gid: int, *, follow_symlinks: bool = True) -> None:
        owners.append((path, uid, gid, follow_symlinks))

    installer.__dict__["_set_owner"] = record_owner
    source = tmp_path / "config.source"
    target = tmp_path / "etc/dashcam/config.toml"
    source.write_bytes(b"schema_version = 1\n")
    installer._install_managed_file(
        source,
        target,
        0o640,
        expected_sha256=None,
        desired_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        uid=0,
        gid=984,
    )
    if os.name == "posix":
        assert target.stat().st_mode & 0o777 == 0o640
    assert owners[-1] == (target, 0, 984, True)

    staging = tmp_path / "release"
    executable = staging / "venv/bin/python"
    package = staging / "venv/lib/python3/site-packages/dashcam.py"
    marker = staging / "installed.json"
    package.parent.mkdir(parents=True)
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"python")
    package.write_bytes(b"module")
    marker.write_bytes(b"{}\n")
    executable.chmod(0o700)
    package.chmod(0o600)
    marker.chmod(0o600)
    owners.clear()
    installer._normalize_release_tree(staging)
    if os.name == "posix":
        assert staging.stat().st_mode & 0o777 == 0o755
        assert executable.stat().st_mode & 0o777 == 0o755
        assert package.stat().st_mode & 0o777 == 0o644
        assert marker.stat().st_mode & 0o777 == 0o644
    assert owners
    assert all(uid == 0 and gid == 0 for _, uid, gid, _ in owners)


class _SystemdRunner:
    def __init__(
        self,
        installer: ModuleType,
        states: dict[str, str],
        activity: dict[str, str] | None = None,
    ) -> None:
        self.installer = installer
        self.states = states
        self.activity = activity or {
            installer.UNIT_NAME: "inactive/dead",
            installer.NETWORK_FALLBACK_UNIT_NAME: "inactive/dead",
            installer.RECORDER_UNIT_NAME: "inactive/dead",
        }
        self.commands: list[tuple[str, ...]] = []

    def run(
        self,
        command: list[str],
        *,
        accepted: frozenset[int] = frozenset({0}),
    ) -> Any:
        del accepted
        self.commands.append(tuple(command))
        if command[1] == "show":
            active, substate = self.activity[command[-1]].split("/", 1)
            return self.installer.CommandResult(0, f"{active}\n{substate}\n", "")
        state = self.states[command[-1]]
        return self.installer.CommandResult(0 if state == "enabled" else 1, state + "\n", "")


def test_systemd_gate_keeps_incomplete_services_absent_and_refuses_foreign_unit(
    tmp_path: Path,
) -> None:
    installer = _load("app_installer_systemd", SOURCE / "install.py")
    unit_root = tmp_path / "etc/systemd/system"
    unit_root.mkdir(parents=True)
    states = {
        **{name: "not-found" for name in installer.MANAGED_UNITS},
        **{name: "not-found" for name in installer.DORMANT_UNITS},
    }
    runner = _SystemdRunner(installer, states)
    observed = installer._systemd_state(tmp_path, runner)
    assert observed == states
    assert all(command[:2] == ("/usr/bin/systemctl", "is-enabled") for command in runner.commands)

    (unit_root / "dashcam-web.service").write_text("[Unit]\n", encoding="ascii")
    with pytest.raises(installer.Refusal, match="dormant unit"):
        installer._systemd_state(tmp_path, runner)


@pytest.mark.parametrize(
    ("activity", "reason"),
    [
        ({"dashcamd.service": "active/running"}, "recorder must be inactive"),
        (
            {"dashcam-network-fallback.service": "active/exited"},
            "network fallback must be inactive",
        ),
        (
            {"dashcam-storage-check.service": "failed/failed"},
            "storage check activity",
        ),
    ],
)
def test_systemd_activity_refuses_running_or_failed_managed_services(
    activity: dict[str, str], reason: str
) -> None:
    installer = _load(f"app_installer_activity_{reason[:8]}", SOURCE / "install.py")
    enabled = {name: "not-found" for name in (*installer.MANAGED_UNITS, *installer.DORMANT_UNITS)}
    default_activity = _SystemdRunner(installer, enabled).activity
    runner = _SystemdRunner(installer, enabled, {**default_activity, **activity})

    with pytest.raises(installer.Refusal, match=reason):
        installer._systemd_activity(enabled, runner)


def test_systemd_activity_allows_completed_storage_check_and_records_exact_shape() -> None:
    installer = _load("app_installer_activity_completed", SOURCE / "install.py")
    enabled = {name: "not-found" for name in (*installer.MANAGED_UNITS, *installer.DORMANT_UNITS)}
    runner = _SystemdRunner(
        installer,
        enabled,
        {
            installer.UNIT_NAME: "active/exited",
            installer.NETWORK_FALLBACK_UNIT_NAME: "inactive/dead",
            installer.RECORDER_UNIT_NAME: "inactive/dead",
        },
    )

    assert installer._systemd_activity(enabled, runner) == {
        installer.UNIT_NAME: "active/exited",
        installer.NETWORK_FALLBACK_UNIT_NAME: "inactive/dead",
        installer.RECORDER_UNIT_NAME: "inactive/dead",
    }
    assert all(command[1] == "show" for command in runner.commands)


def test_systemd_activity_refuses_an_inactive_existing_storage_check() -> None:
    installer = _load("app_installer_activity_inactive_storage", SOURCE / "install.py")
    enabled = {name: "not-found" for name in (*installer.MANAGED_UNITS, *installer.DORMANT_UNITS)}
    enabled[installer.UNIT_NAME] = "enabled"
    runner = _SystemdRunner(installer, enabled)

    with pytest.raises(installer.Refusal, match="storage check activity"):
        installer._systemd_activity(enabled, runner)


@pytest.mark.parametrize(
    "unit_name",
    (
        "dashcam-storage-check.service",
        "dashcam-network-fallback.service",
    ),
)
def test_managed_prestate_refuses_unattributable_managed_unit(
    tmp_path: Path, unit_name: str
) -> None:
    _, bundle = _bundle(tmp_path)
    installer = _load("app_installer_prestate", SOURCE / "install.py")
    manifest = installer._manifest(bundle.resolve())
    unit = tmp_path / "etc/systemd/system" / unit_name
    unit.parent.mkdir(parents=True)
    unit.write_text("[Unit]\nDescription=foreign\n", encoding="ascii")
    with pytest.raises(installer.Refusal, match="unattributable"):
        installer._managed_prestate(bundle.resolve(), tmp_path, manifest)


def _write_applied_journal(
    root: Path,
    *,
    release_id: str,
    manifest_sha256: str,
    managed_file_hashes: dict[str, str] | None = None,
) -> None:
    payload: dict[str, object] = {
        "schema_version": 1,
        "mode": "applied",
        "ready": True,
        "release_id": release_id,
        "manifest_sha256": manifest_sha256,
    }
    if managed_file_hashes is not None:
        payload["managed_file_hashes"] = managed_file_hashes
    journal = root / "var/lib/dashcam/app-install-v1.json"
    journal.parent.mkdir(parents=True)
    journal.write_text(json.dumps(payload), encoding="ascii")


def test_managed_prestate_authorizes_the_exact_legacy_applied_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, bundle = _bundle(tmp_path)
    installer = _load("app_installer_legacy_upgrade", SOURCE / "install.py")
    manifest = installer._manifest(bundle.resolve())
    _write_applied_journal(
        tmp_path,
        release_id=installer.LEGACY_RELEASE_ID,
        manifest_sha256=installer.LEGACY_MANIFEST_SHA256,
    )
    monkeypatch.setattr(
        installer,
        "_current_release_identity",
        lambda _root: (installer.LEGACY_RELEASE_ID, installer.LEGACY_MANIFEST_SHA256),
    )
    monkeypatch.setattr(
        installer,
        "_observed_managed_file_hashes",
        lambda _files: dict(installer.LEGACY_MANAGED_FILE_HASHES),
    )

    before, desired = installer._managed_prestate(bundle.resolve(), tmp_path, manifest)

    assert before == installer.LEGACY_MANAGED_FILE_HASHES
    assert set(desired) == set(installer.LEGACY_MANAGED_FILE_HASHES)
    assert before != desired


def test_managed_prestate_authorizes_a_journal_bound_future_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, bundle = _bundle(tmp_path)
    installer = _load("app_installer_future_upgrade", SOURCE / "install.py")
    manifest = installer._manifest(bundle.resolve())
    current = ("0.1.0.dev0-future", "a" * 64)
    observed = {
        name: chr(98 + index) * 64
        for index, name in enumerate(installer.LEGACY_MANAGED_FILE_HASHES)
    }
    _write_applied_journal(
        tmp_path,
        release_id=current[0],
        manifest_sha256=current[1],
        managed_file_hashes=observed,
    )
    monkeypatch.setattr(installer, "_current_release_identity", lambda _root: current)
    monkeypatch.setattr(installer, "_observed_managed_file_hashes", lambda _files: observed)

    before, desired = installer._managed_prestate(bundle.resolve(), tmp_path, manifest)

    assert before == observed
    assert before != desired


def test_managed_prestate_refuses_arbitrary_hash_drift_despite_a_future_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, bundle = _bundle(tmp_path)
    installer = _load("app_installer_future_drift", SOURCE / "install.py")
    manifest = installer._manifest(bundle.resolve())
    current = ("0.1.0.dev0-future", "a" * 64)
    recorded = {
        name: chr(98 + index) * 64
        for index, name in enumerate(installer.LEGACY_MANAGED_FILE_HASHES)
    }
    observed = {**recorded, "dashcamd.service": "f" * 64}
    _write_applied_journal(
        tmp_path,
        release_id=current[0],
        manifest_sha256=current[1],
        managed_file_hashes=recorded,
    )
    monkeypatch.setattr(installer, "_current_release_identity", lambda _root: current)
    monkeypatch.setattr(installer, "_observed_managed_file_hashes", lambda _files: observed)

    with pytest.raises(installer.Refusal, match="differ from the applied journal"):
        installer._managed_prestate(bundle.resolve(), tmp_path, manifest)


def test_managed_prestate_refuses_a_non_applied_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, bundle = _bundle(tmp_path)
    installer = _load("app_installer_non_applied_journal", SOURCE / "install.py")
    manifest = installer._manifest(bundle.resolve())
    current = ("0.1.0.dev0-future", "a" * 64)
    observed = {
        name: chr(98 + index) * 64
        for index, name in enumerate(installer.LEGACY_MANAGED_FILE_HASHES)
    }
    _write_applied_journal(
        tmp_path,
        release_id=current[0],
        manifest_sha256=current[1],
        managed_file_hashes=observed,
    )
    journal = tmp_path / "var/lib/dashcam/app-install-v1.json"
    payload = json.loads(journal.read_text(encoding="ascii"))
    payload["mode"] = "dry-run"
    journal.write_text(json.dumps(payload), encoding="ascii")
    monkeypatch.setattr(installer, "_current_release_identity", lambda _root: current)
    monkeypatch.setattr(installer, "_observed_managed_file_hashes", lambda _files: observed)

    with pytest.raises(installer.Refusal, match="journal differs"):
        installer._managed_prestate(bundle.resolve(), tmp_path, manifest)


def test_command_runner_rejects_nonallowlisted_or_unbounded_commands() -> None:
    installer = _load("app_installer_commands", SOURCE / "install.py")
    runner = installer.Runner()
    with pytest.raises(installer.Refusal, match="allowlist"):
        runner.run(["/bin/sh", "-c", "true"])
    with pytest.raises(installer.Refusal, match="allowlist"):
        runner.run(["/usr/bin/dpkg", "x" * 5000])
    with pytest.raises(installer.Refusal, match="closed path"):
        runner.run_release_python("/tmp/venv/bin/python", ["-m", "pip"])


class _ReleaseRunner:
    def __init__(self, installer: ModuleType) -> None:
        self.installer = installer
        self.commands: list[tuple[str, ...]] = []
        self.release_commands: list[tuple[str, tuple[str, ...], int, int]] = []
        self.service_release_commands: list[tuple[str, tuple[str, ...], int, int]] = []

    def run(self, command: list[str], **_: Any) -> Any:
        self.commands.append(tuple(command))
        if command[:4] != ["/usr/bin/python3", "-m", "venv", "--system-site-packages"]:
            raise AssertionError(f"unexpected command: {command}")
        venv = Path(command[-1])
        executable = venv / "bin/python"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"python")
        executable.chmod(0o700)
        (venv / "pyvenv.cfg").write_text(
            "home = /usr/bin\ninclude-system-site-packages = true\n",
            encoding="ascii",
        )
        return self.installer.CommandResult(0, "", "")

    def run_release_python(
        self,
        executable: str,
        arguments: list[str],
        *,
        timeout: int = 180,
        output_limit: int = 256 * 1024,
    ) -> Any:
        self.release_commands.append((executable, tuple(arguments), timeout, output_limit))
        return self.installer.CommandResult(0, "", "")

    def run_release_python_as_service_user(
        self,
        executable: str,
        arguments: list[str],
        *,
        timeout: int = 180,
        output_limit: int = 256 * 1024,
    ) -> Any:
        self.service_release_commands.append((executable, tuple(arguments), timeout, output_limit))
        return self.installer.CommandResult(0, "", "")


def test_release_uses_system_site_packages_and_smokes_application_and_gstreamer_before_finalize(
    tmp_path: Path,
) -> None:
    _, bundle = _bundle(tmp_path)
    installer = _load("app_installer_release_smoke", SOURCE / "install.py")
    manifest = installer._manifest(bundle.resolve())
    runner = _ReleaseRunner(installer)

    release = installer._install_release(bundle.resolve(), tmp_path, manifest, runner)

    assert runner.commands == [
        (
            "/usr/bin/python3",
            "-m",
            "venv",
            "--system-site-packages",
            str(release.parent / f".staging-{manifest['release_id']}-{os.getpid()}" / "venv"),
        )
    ]
    assert runner.release_commands[0][1][:2] == ("-m", "pip")
    executable, arguments, timeout, output_limit = runner.service_release_commands[0]
    assert executable.replace("\\", "/").endswith("/venv/bin/python")
    assert arguments == ("-c", installer.STAGING_RELEASE_SMOKE)
    assert timeout == installer.GSTREAMER_SMOKE_TIMEOUT_SECONDS
    assert output_limit == installer.GSTREAMER_SMOKE_MAX_OUTPUT_BYTES
    assert "gi.require_version('Gst','1.0')" in installer.GSTREAMER_IMPORT_SMOKE
    assert "gi.require_version('GstAllocators','1.0')" in installer.GSTREAMER_IMPORT_SMOKE
    assert "gi.require_version('GstVideo','1.0')" in installer.GSTREAMER_IMPORT_SMOKE
    assert "validate_native_overlay_dependencies(Gst,GstAllocators,GstVideo)" in (
        installer.GSTREAMER_IMPORT_SMOKE
    )
    for module in (
        "dashcam.daemon",
        "dashcam.recorder.runtime",
        "dashcam.catalog.database",
        "dashcam.recorder.finalizer",
    ):
        assert module in installer.APPLICATION_IMPORT_SMOKE
    assert (
        "Path(module.__file__).resolve().is_relative_to(venv)" in installer.APPLICATION_IMPORT_SMOKE
    )
    assert "Gst.init(None)" in installer.GSTREAMER_IMPORT_SMOKE
    assert "Gst.Caps.from_string('video/x-raw,framerate=30/1')" in (
        installer.GSTREAMER_IMPORT_SMOKE
    )
    assert "int(rate.num)==30 and int(rate.denom)==1" in installer.GSTREAMER_IMPORT_SMOKE
    assert "Gst.ElementFactory.find" in installer.GSTREAMER_IMPORT_SMOKE
    assert "Gst.ElementFactory.make" not in installer.GSTREAMER_IMPORT_SMOKE
    for factory in (
        "queue",
        "capsfilter",
        "libcamerasrc",
        "v4l2h264enc",
        "alsasrc",
        "audioconvert",
        "audioresample",
        "voaacenc",
        "aacparse",
        "fakesink",
    ):
        assert repr(factory) in installer.GSTREAMER_IMPORT_SMOKE
    assert "overlay.set_text('TIME UNSYNCED\\nGPS INVALID')" in (
        installer.GSTREAMER_IMPORT_SMOKE
    )
    assert "overlay.snapshot()" in installer.GSTREAMER_IMPORT_SMOKE
    assert "initial.mappings_cached==0" in installer.GSTREAMER_IMPORT_SMOKE
    assert "GstBase" not in installer.GSTREAMER_IMPORT_SMOKE
    assert "GObject" not in installer.GSTREAMER_IMPORT_SMOKE
    assert "textoverlay" not in installer.GSTREAMER_IMPORT_SMOKE
    assert (release / "installed.json").is_file()


def test_gstreamer_smoke_fails_closed_and_existing_release_requires_system_packages(
    tmp_path: Path,
) -> None:
    _, bundle = _bundle(tmp_path)
    installer = _load("app_installer_release_smoke_refusal", SOURCE / "install.py")
    manifest = installer._manifest(bundle.resolve())

    class FailingSmokeRunner(_ReleaseRunner):
        def run_release_python_as_service_user(
            self,
            executable: str,
            arguments: list[str],
            *,
            timeout: int = 180,
            output_limit: int = 256 * 1024,
        ) -> Any:
            super().run_release_python_as_service_user(
                executable,
                arguments,
                timeout=timeout,
                output_limit=output_limit,
            )
            if arguments == ["-c", installer.STAGING_RELEASE_SMOKE]:
                raise installer.Refusal("command failed with exit 1: staging release smoke")
            return self.installer.CommandResult(0, "", "")

    with pytest.raises(installer.Refusal, match="staging release smoke"):
        installer._install_release(
            bundle.resolve(), tmp_path, manifest, FailingSmokeRunner(installer)
        )
    assert not list((tmp_path / "opt/dashcam/releases").glob(".staging-*"))

    runner = _ReleaseRunner(installer)
    release = installer._install_release(bundle.resolve(), tmp_path, manifest, runner)
    (release / "venv/pyvenv.cfg").write_text(
        "include-system-site-packages = false\n", encoding="ascii"
    )
    with pytest.raises(installer.Refusal, match="lacks reviewed system site packages"):
        installer._install_release(bundle.resolve(), tmp_path, manifest, runner)


class _VideoMembershipRunner:
    def __init__(self, installer: ModuleType, *, member: bool = False) -> None:
        self.installer = installer
        self.member = member
        self.commands: list[tuple[str, ...]] = []
        self.account_payload = (
            "dashcam:x:987:987:Dashcam service:/var/lib/dashcam:/usr/sbin/nologin\n"
        )
        self.service_group_payload = "dashcam:x:987:\n"
        self.group_payload = "video:x:44:other" + (",dashcam" if member else "") + "\n"

    def run(self, command: list[str], **_: Any) -> Any:
        self.commands.append(tuple(command))
        if command == ["/usr/bin/getent", "passwd", self.installer.SERVICE_USER]:
            return self.installer.CommandResult(0, self.account_payload, "")
        if command == ["/usr/bin/getent", "group", self.installer.SERVICE_USER]:
            return self.installer.CommandResult(0, self.service_group_payload, "")
        if command == ["/usr/bin/getent", "group", self.installer.VIDEO_GROUP]:
            return self.installer.CommandResult(0, self.group_payload, "")
        if command == [
            "/usr/sbin/usermod",
            "--append",
            "--groups",
            self.installer.VIDEO_GROUP,
            self.installer.SERVICE_USER,
        ]:
            self.member = True
            self.group_payload = "video:x:44:other,dashcam\n"
            return self.installer.CommandResult(0, "", "")
        raise AssertionError(f"unexpected command: {command}")


def test_dashcam_video_membership_is_idempotently_added_when_absent() -> None:
    installer = _load("app_installer_video_membership_add", SOURCE / "install.py")
    runner = _VideoMembershipRunner(installer)

    assert installer._dashcam_video_membership(runner) is False
    installer._ensure_dashcam_video_membership(runner)
    installer._ensure_dashcam_video_membership(runner)

    usermod = (
        "/usr/sbin/usermod",
        "--append",
        "--groups",
        "video",
        "dashcam",
    )
    assert runner.commands.count(usermod) == 1
    assert installer._dashcam_video_membership(runner) is True


def test_dashcam_video_membership_does_not_mutate_when_already_present() -> None:
    installer = _load("app_installer_video_membership_present", SOURCE / "install.py")
    runner = _VideoMembershipRunner(installer, member=True)

    installer._ensure_dashcam_video_membership(runner)

    assert not any(command[0] == "/usr/sbin/usermod" for command in runner.commands)


def test_dashcam_video_membership_refuses_primary_group_mismatch() -> None:
    installer = _load("app_installer_video_primary_group", SOURCE / "install.py")
    runner = _VideoMembershipRunner(installer)
    runner.service_group_payload = "dashcam:x:988:\n"

    with pytest.raises(installer.Refusal, match="primary group"):
        installer._ensure_dashcam_video_membership(runner)
    assert not any(command[0] == "/usr/sbin/usermod" for command in runner.commands)


@pytest.mark.parametrize(
    ("account_payload", "group_payload", "reason"),
    [
        (
            "dashcam:x:bad:987:Dashcam:/var/lib/dashcam:/usr/sbin/nologin\n",
            "video:x:44:\n",
            "service account",
        ),
        (
            "dashcam:x:987:987:Dashcam:/var/lib/dashcam:/usr/sbin/nologin\n",
            "video:x:nope:\n",
            "group identity",
        ),
        (
            "dashcam:x:987:987:Dashcam:/var/lib/dashcam:/usr/sbin/nologin\n",
            "video:x:44:dashcam,dashcam\n",
            "group identity",
        ),
        (
            "dashcam::987:987:Dashcam:/var/lib/dashcam:/usr/sbin/nologin\n",
            "video:x:44:\n",
            "service account",
        ),
        (
            "dashcam:x:987:987:Dashcam:/var/lib/dashcam:/usr/sbin/nologin\ndashcam:x:987:987:Dashcam:/var/lib/dashcam:/usr/sbin/nologin\n",
            "video:x:44:\n",
            "service account",
        ),
    ],
)
def test_dashcam_video_membership_refuses_unsafe_account_or_group_state(
    account_payload: str, group_payload: str, reason: str
) -> None:
    installer = _load("app_installer_video_membership_refusal", SOURCE / "install.py")
    runner = _VideoMembershipRunner(installer)
    runner.account_payload = account_payload
    runner.group_payload = group_payload

    with pytest.raises(installer.Refusal, match=reason):
        installer._ensure_dashcam_video_membership(runner)
    assert not any(command[0] == "/usr/sbin/usermod" for command in runner.commands)


def test_service_user_smoke_uses_setpriv_with_initialized_groups() -> None:
    installer = _load("app_installer_service_smoke_runner", SOURCE / "install.py")
    runner = installer.Runner()
    observed: list[tuple[str, ...]] = []

    def execute(
        command: list[str], *, timeout: int, output_limit: int, accepted: frozenset[int]
    ) -> Any:
        observed.append(tuple(command))
        assert timeout == installer.GSTREAMER_SMOKE_TIMEOUT_SECONDS
        assert output_limit == installer.GSTREAMER_SMOKE_MAX_OUTPUT_BYTES
        assert accepted == frozenset({0})
        return installer.CommandResult(0, "", "")

    runner.__dict__["_execute"] = execute
    installer._staging_release_smoke(
        runner,
        "/opt/dashcam/releases/.staging-one-123/venv/bin/python",
    )

    assert observed == [
        (
            "/usr/bin/setpriv",
            "--reuid=dashcam",
            "--regid=dashcam",
            "--init-groups",
            "/opt/dashcam/releases/.staging-one-123/venv/bin/python",
            "-c",
            installer.STAGING_RELEASE_SMOKE,
        )
    ]


class _AptRunner:
    def __init__(
        self,
        installer: ModuleType,
        *,
        summary: str = "0 upgraded, 2 newly installed, 0 to remove and 0 not upgraded.",
        metadata_complete: bool = True,
        include_beta: bool = True,
        beta_version: str = "2.0",
    ) -> None:
        self.installer = installer
        self.summary = summary
        self.metadata_complete = metadata_complete
        self.include_beta = include_beta
        self.beta_version = beta_version
        self.commands: list[tuple[list[str], dict[str, Any]]] = []

    def run(self, command: list[str], **kwargs: Any) -> Any:
        self.commands.append((command, kwargs))
        if command[0:2] == ["/usr/bin/apt-get", "--simulate"]:
            output = f"{self.summary}\nInst alpha (1.0 Debian:13/stable [armhf])\n"
            if self.include_beta:
                output += f"Inst beta ({self.beta_version} Debian:13/stable [armhf])\n"
        else:
            size = "Size: 12500000\n" if self.metadata_complete else ""
            output = (
                "Package: alpha\nVersion: 1.0\nInstalled-Size: 10000\n"
                f"{size}\n"
                "Package: beta\nVersion: 2.0\nInstalled-Size: 24200\n"
                "Size: 5000000\n"
            )
        return self.installer.CommandResult(0, output, "")


def test_apt_simulation_pins_versions_and_produces_conservative_peak_bound() -> None:
    installer = _load("app_installer_apt_plan", SOURCE / "install.py")
    runner = _AptRunner(installer)
    result = installer._apt_simulation({"beta": "2.0", "alpha": "1.0"}, runner)
    assert result == {
        "solver_packages": {"alpha": "1.0", "beta": "2.0"},
        "download_bytes": 17_500_000,
        "installed_bytes": 35_020_800,
        "peak_bytes": 52_520_800,
    }
    command, kwargs = runner.commands[0]
    assert "--simulate" in command
    assert "--no-upgrade" not in command
    assert command[-2:] == ["alpha=1.0", "beta=2.0"]
    assert kwargs["timeout"] == installer.APT_TIMEOUT_SECONDS
    assert kwargs["output_limit"] == installer.MAX_APT_COMMAND_BYTES
    metadata_command, _ = runner.commands[1]
    assert metadata_command == [
        "/usr/bin/apt-cache",
        "show",
        "alpha=1.0",
        "beta=2.0",
    ]


@pytest.mark.parametrize(
    "summary",
    [
        "1 upgraded, 2 newly installed, 0 to remove and 0 not upgraded.",
        "0 upgraded, 2 newly installed, 1 to remove and 0 not upgraded.",
    ],
)
def test_apt_simulation_refuses_upgrades_and_removals(summary: str) -> None:
    installer = _load(f"app_installer_apt_refusal_{summary[0:3]}", SOURCE / "install.py")
    with pytest.raises(installer.Refusal, match="upgrade/remove"):
        installer._apt_simulation(
            {"alpha": "1.0", "beta": "2.0"},
            _AptRunner(installer, summary=summary),
        )


def test_apt_simulation_refuses_missing_exact_size_metadata() -> None:
    installer = _load("app_installer_apt_missing_metadata", SOURCE / "install.py")
    with pytest.raises(installer.Refusal, match="lacks exact size identity"):
        installer._apt_simulation(
            {"alpha": "1.0", "beta": "2.0"},
            _AptRunner(installer, metadata_complete=False),
        )


@pytest.mark.parametrize(
    ("runner_options", "reason"),
    [
        ({"include_beta": False}, "differs from its summary"),
        ({"beta_version": "2.1"}, "changed an exact requested"),
    ],
)
def test_apt_simulation_refuses_malformed_or_drifted_solver_plan(
    runner_options: dict[str, Any], reason: str
) -> None:
    installer = _load(f"app_installer_apt_solver_{reason[0:5]}", SOURCE / "install.py")
    with pytest.raises(installer.Refusal, match=reason):
        installer._apt_simulation(
            {"alpha": "1.0", "beta": "2.0"},
            _AptRunner(installer, **runner_options),
        )


def test_apply_requires_exact_approved_dry_run_and_refuses_plan_drift(
    tmp_path: Path,
) -> None:
    installer = _load("app_installer_approval", SOURCE / "install.py")
    current: dict[str, object] = {
        "schema_version": 1,
        "mode": "apply",
        "ready": True,
        "manifest_sha256": "a" * 64,
        "root_free_before_bytes": 5 * 1024**3,
        "projected_root_free_bytes": 3 * 1024**3,
        "required_root_free_before_bytes": 4 * 1024**3,
        "missing_package_candidates": {"alpha": "1.0"},
        "apt_simulation": {"peak_bytes": 100},
        "systemd_activity_before": {
            "dashcam-storage-check.service": "active/exited",
            "dashcam-network-fallback.service": "inactive/dead",
            "dashcamd.service": "inactive/dead",
        },
        "managed_file_hashes_before": {
            "config.toml": "b" * 64,
            "dashcam-storage-check.service": "c" * 64,
            "dashcam-network-fallback.service": "d" * 64,
            "dashcamd.service": "e" * 64,
        },
        "managed_file_hashes": {
            "config.toml": "f" * 64,
            "dashcam-storage-check.service": "a" * 64,
            "dashcam-network-fallback.service": "b" * 64,
            "dashcamd.service": "c" * 64,
        },
    }
    approved = {**current, "mode": "dry-run"}
    path = tmp_path / "approved.json"
    path.write_text(json.dumps(approved), encoding="ascii")
    installer._validate_approved_plan(path, current)

    approved["missing_package_candidates"] = {"alpha": "1.1"}
    path.write_text(json.dumps(approved), encoding="ascii")
    with pytest.raises(installer.Refusal, match="differs"):
        installer._validate_approved_plan(path, current)

    approved["systemd_activity_before"] = current["systemd_activity_before"]
    approved["managed_file_hashes_before"] = {
        **cast(dict[str, str], current["managed_file_hashes_before"]),
        "dashcamd.service": "a" * 64,
    }
    path.write_text(json.dumps(approved), encoding="ascii")
    with pytest.raises(installer.Refusal, match="differs"):
        installer._validate_approved_plan(path, current)

    approved["missing_package_candidates"] = {"alpha": "1.0"}
    approved["systemd_activity_before"] = {
        **cast(dict[str, str], current["systemd_activity_before"]),
        "dashcamd.service": "active/running",
    }
    path.write_text(json.dumps(approved), encoding="ascii")
    with pytest.raises(installer.Refusal, match="differs"):
        installer._validate_approved_plan(path, current)


def test_units_and_installer_enable_without_starting_or_restarting_services() -> None:
    storage_unit = (SOURCE / "dashcam-storage-check.service").read_text(encoding="ascii")
    network_unit = (SOURCE / "dashcam-network-fallback.service").read_text(encoding="ascii")
    recorder_unit = (ROOT / "systemd/dashcamd.service").read_text(encoding="ascii")
    installer = (SOURCE / "install.py").read_text(encoding="utf-8")
    assert (
        "ExecStart=/opt/dashcam/current/venv/bin/python -m dashcam.storage.preflight"
        in storage_unit
    )
    recorder_lines = recorder_unit.splitlines()
    for line in (
        "Requires=NetworkManager.service",
        "Wants=cloud-final.service",
        "After=NetworkManager.service cloud-final.service",
        "Type=oneshot",
        "User=root",
        "Group=root",
        "ExecStart=/opt/dashcam/current/venv/bin/python -m dashcam.network_fallback",
        "TimeoutStartSec=120s",
        "RemainAfterExit=yes",
        "UMask=0077",
        "WantedBy=multi-user.target",
    ):
        assert network_unit.count(line) == 1
    for line in (
        "After=local-fs.target dashcam-storage-check.service",
        "Wants=dashcam-storage-check.service",
        "Type=notify",
        "User=dashcam",
        "Group=dashcam",
        "SupplementaryGroups=audio video render dialout dashcam-storage",
        "ExecStart=/opt/dashcam/current/venv/bin/python -m dashcam.daemon "
        "--config /etc/dashcam/config.toml --identity /etc/dashcam/storage-volume.env",
        "Restart=on-failure",
        "RestartSec=1s",
        "RestartSteps=5",
        "RestartMaxDelaySec=60s",
        "RestartMode=normal",
        "StartLimitAction=none",
        "TimeoutStartSec=45s",
        "TimeoutStopSec=30s",
        "BindPaths=/srv/dashcam",
        "WatchdogSec=20s",
        "WantedBy=multi-user.target",
    ):
        assert recorder_lines.count(line) == 1
    assert "Requires=" not in recorder_unit
    assert "RequiresMountsFor=" not in recorder_unit
    assert 'systemctl", "start' not in installer
    assert 'systemctl", "restart' not in installer
    assert '"services_to_start": []' in installer
    assert 'command_runner.run(["/usr/bin/systemctl", "enable", unit_name])' in installer
    assert 'command_runner.run(["/usr/bin/apt-get", "update"])' not in installer
    assert 'f"{package}={version}"' in installer
    assert "gid=storage_gid" in installer
    assert "_normalize_release_tree(staging)" in installer
    assert "_verify_installed_permissions(root, release, storage_gid, command_runner)" in installer
    assert '"/usr/bin/setpriv"' in installer
    assert ".dashcam-volume" in installer
    assert "EXPECTED_CARD_CID" in installer
    assert '"rw" not in options.split' in installer
    assert "dashcamd.service" in installer
    for name in ("dashcam-web.service", "dashcam-prepare-removal.service"):
        assert name in installer
