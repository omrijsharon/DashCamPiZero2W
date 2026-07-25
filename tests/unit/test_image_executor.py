from __future__ import annotations

import hashlib
import json
import lzma
import os
import shutil
import stat
import subprocess
import sys
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import dashcam.provisioning.image_executor as image_executor_module
from dashcam.provisioning.image_builder import (
    ImageBuildPlan,
    ImageBuildRefusalCode,
    ImageBuildRefused,
    SourceImageManifest,
    author_image_build_plan,
    load_source_manifest,
)
from dashcam.provisioning.image_executor import (
    AUTHORIZATION_STATEMENT,
    BOUNDED_PROVISION_TOKEN,
    EXACT_CARD_CID,
    EXACT_CARD_MARKER_CONTENT,
    EXACT_CARD_SIZE_BYTES,
    FileToolResult,
    ImageExecutionRefusalCode,
    ImageExecutionRefused,
    bind_payload,
    decompress_verified_image,
    execute_file_image,
    load_exact_card_authorization,
    probe_execution_dependencies,
    refuse_block_device_execution,
    transform_cmdline,
    validate_boot_target_config,
)
from dashcam.provisioning.initramfs_customizer import PI_ZERO_2_W_ARMV7_PROFILE

ROOT = Path(__file__).parents[2]
OFFICIAL_MANIFEST = ROOT / "deploy" / "image" / "source-manifest-v1.json"
PAYLOAD = ROOT / "deploy" / "image" / "payload"
VALID_BOOT_CONFIG = b"camera_auto_detect=1\nauto_initramfs=1\n[all]\n"


def _closure(artifacts: object) -> SimpleNamespace:
    return SimpleNamespace(
        artifacts=artifacts,
        target=SimpleNamespace(
            profile=PI_ZERO_2_W_ARMV7_PROFILE,
            initramfs_filename="initramfs7",
        ),
    )


def _official() -> SourceImageManifest:
    return load_source_manifest(OFFICIAL_MANIFEST.read_bytes())


def _raw_bytes(*, valid_identity: bool = True) -> tuple[bytes, SourceImageManifest]:
    official = _official()
    sector_size = 512
    boot = replace(
        official.image.partitions[0],
        start_sector=1,
        size_sectors=4,
        filesystem_uuid="1234-ABCD",
    )
    root = replace(
        official.image.partitions[1],
        start_sector=5,
        size_sectors=27,
        filesystem_uuid="12345678-1234-5678-9abc-def012345678",
    )
    image = replace(
        official.image,
        size_bytes=32 * sector_size,
        total_sectors=32,
        mbr_disk_id="0x12345678",
        partitions=(boot, root),
    )
    raw = bytearray(image.size_bytes)
    raw[440:444] = int(image.mbr_disk_id[2:], 16).to_bytes(4, "little")
    for partition in image.partitions:
        offset = 446 + (partition.number - 1) * 16
        raw[offset] = 0x80 if partition.bootable else 0
        raw[offset + 4] = int(partition.partition_type[2:], 16)
        raw[offset + 8 : offset + 12] = partition.start_sector.to_bytes(4, "little")
        raw[offset + 12 : offset + 16] = partition.size_sectors.to_bytes(4, "little")
    raw[510:512] = b"\x55\xaa"
    fat_serial = int(boot.filesystem_uuid.replace("-", ""), 16)
    raw[boot.start_sector * sector_size + 67 : boot.start_sector * sector_size + 71] = (
        fat_serial.to_bytes(4, "little")
    )
    root_uuid = uuid.UUID(root.filesystem_uuid).bytes
    root_uuid_offset = root.start_sector * sector_size + 1024 + 104
    raw[root_uuid_offset : root_uuid_offset + 16] = root_uuid
    if not valid_identity:
        raw[440] ^= 0x01
    return bytes(raw), replace(official, image=image)


def _compressed_fixture(
    tmp_path: Path, *, valid_identity: bool = True
) -> tuple[Path, SourceImageManifest, bytes]:
    raw, manifest = _raw_bytes(valid_identity=valid_identity)
    archive_payload = lzma.compress(raw, format=lzma.FORMAT_XZ)
    source = tmp_path / "source.img.xz"
    source.write_bytes(archive_payload)
    archive = replace(
        manifest.archive,
        filename=source.name,
        size_bytes=len(archive_payload),
        sha256=hashlib.sha256(archive_payload).hexdigest(),
    )
    return source, replace(manifest, archive=archive), raw


def _plan(
    tmp_path: Path,
    source: Path,
    manifest: SourceImageManifest,
    payload: Path = PAYLOAD,
    closure_manifest_path: Path | None = None,
) -> ImageBuildPlan:
    return author_image_build_plan(
        manifest=manifest,
        manifest_path=OFFICIAL_MANIFEST,
        source_archive=source,
        output_image=(tmp_path / "release.img").resolve(),
        payload_root=payload,
        closure_manifest_path=closure_manifest_path,
    )


def test_cmdline_transform_replaces_only_exact_resize_and_preserves_imager_tokens() -> None:
    original = (
        b"console=serial0,115200 root=PARTUUID=4f2c9ea0-02 resize "
        b"systemd.run=/boot/firstrun.sh systemd.run_success_action=reboot quiet\n"
    )

    transformed = transform_cmdline(original)

    assert transformed == original.replace(b"resize", BOUNDED_PROVISION_TOKEN.encode(), 1)
    assert b"systemd.run=/boot/firstrun.sh" in transformed
    assert transformed.endswith(b"\n")


@pytest.mark.parametrize(
    "payload",
    [
        b"root=x",
        b"resize resize",
        b"resize dashcam.bounded_provision=v1",
        b"foo  resize",
        b"foo\r resize\n",
        b"\xff resize",
    ],
)
def test_cmdline_transform_refuses_ambiguous_or_malformed_input(payload: bytes) -> None:
    with pytest.raises(ImageExecutionRefused) as caught:
        transform_cmdline(payload)
    assert caught.value.code is ImageExecutionRefusalCode.CMDLINE_INVALID


@pytest.mark.parametrize(
    "payload",
    [
        VALID_BOOT_CONFIG,
        b"AUTO_INITRAMFS \t= \t1 # exact global setting\n",
        b"[pi5]\ndtoverlay=dwc2\n[ALL]\nAuto_Initramfs = 1\n",
    ],
)
def test_boot_target_config_admits_one_global_or_all_assignment_without_overrides(
    payload: bytes,
) -> None:
    validate_boot_target_config(payload)


@pytest.mark.parametrize(
    "payload",
    [
        b"camera_auto_detect=1\n",
        b"auto_initramfs=0\n",
        b"auto_initramfs=1\nauto_initramfs=1\n",
        b"auto_initramfs=1\ninitramfs initramfs followkernel\n",
        b"auto_initramfs=1\nInItRaMfS\talt followkernel # mixed case and tab\n",
        b"auto_initramfs=1\nkernel=kernel8.img\n",
        b"auto_initramfs=1\nKERNEL \t= kernel8.img\n",
        b"auto_initramfs=1\narm_64bit=1\n",
        b"auto_initramfs=1\nos_prefix=alt/\n",
        b"auto_initramfs=1\ninclude alt-config.txt\n",
        b"[pi5]\nauto_initramfs=1\n",
        b"[future-board]\nauto_initramfs=1\n",
        b"auto_initramfs=1\n[pi5]\nauto_initramfs=1\n",
        b"auto_initramfs=1\n[all]\nauto_initramfs=1\n",
        b"[pi5\nauto_initramfs=1\n",
        b"auto_initramfs=1\r\n",
        b"\xffauto_initramfs=1\n",
    ],
)
def test_boot_target_config_refuses_missing_disabled_ambiguous_or_overridden_target(
    payload: bytes,
) -> None:
    with pytest.raises(ImageExecutionRefused) as caught:
        validate_boot_target_config(payload)
    assert caught.value.code is ImageExecutionRefusalCode.INITRAMFS_CLOSURE_UNPROVEN


def test_decompression_exclusively_creates_and_verifies_regular_raw_image(
    tmp_path: Path,
) -> None:
    source, manifest, raw = _compressed_fixture(tmp_path)
    source_before = source.read_bytes()
    output = (tmp_path / "release.img").resolve()

    result = decompress_verified_image(
        manifest=manifest,
        source_archive=source,
        output_image=output,
    )

    assert result.path == str(output)
    assert output.read_bytes() == raw
    assert source.read_bytes() == source_before


def test_decompression_removes_output_after_raw_verification_failure(tmp_path: Path) -> None:
    source, manifest, _ = _compressed_fixture(tmp_path, valid_identity=False)
    output = (tmp_path / "release.img").resolve()

    with pytest.raises(ImageBuildRefused) as caught:
        decompress_verified_image(
            manifest=manifest,
            source_archive=source,
            output_image=output,
        )

    assert caught.value.code is ImageBuildRefusalCode.RAW_GEOMETRY_MISMATCH
    assert not output.exists()
    assert source.exists()


def test_failure_cleanup_never_deletes_a_replaced_owner_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, manifest, _ = _compressed_fixture(tmp_path)
    output = (tmp_path / "release.img").resolve()

    def replace_then_fail(path: Path, _manifest: SourceImageManifest) -> None:
        path.unlink()
        path.write_bytes(b"replacement owner data")
        raise ImageExecutionRefused(
            ImageExecutionRefusalCode.OUTPUT_VERIFICATION_FAILED,
            "injected verification failure",
        )

    monkeypatch.setattr(image_executor_module, "verify_raw_image", replace_then_fail)

    with pytest.raises(ImageExecutionRefused):
        decompress_verified_image(
            manifest=manifest,
            source_archive=source,
            output_image=output,
        )

    assert output.read_bytes() == b"replacement owner data"


def test_decompression_removes_output_after_xz_failure(tmp_path: Path) -> None:
    source = tmp_path / "source.img.xz"
    source.write_bytes(b"not xz")
    manifest = replace(
        _official(),
        archive=replace(
            _official().archive,
            filename=source.name,
            size_bytes=source.stat().st_size,
            sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        ),
    )
    output = (tmp_path / "release.img").resolve()

    with pytest.raises(ImageExecutionRefused) as caught:
        decompress_verified_image(
            manifest=manifest,
            source_archive=source,
            output_image=output,
        )

    assert caught.value.code is ImageExecutionRefusalCode.COMMAND_FAILED
    assert not output.exists()
    assert source.read_bytes() == b"not xz"


def test_decompression_never_overwrites_existing_output(tmp_path: Path) -> None:
    source, manifest, _ = _compressed_fixture(tmp_path)
    output = (tmp_path / "release.img").resolve()
    output.write_bytes(b"owner data")

    with pytest.raises(ImageBuildRefused) as caught:
        decompress_verified_image(
            manifest=manifest,
            source_archive=source,
            output_image=output,
        )

    assert caught.value.code is ImageBuildRefusalCode.OUTPUT_EXISTS
    assert output.read_bytes() == b"owner data"


def test_decompression_refuses_device_target_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, manifest, _ = _compressed_fixture(tmp_path)
    output = (tmp_path / "device.img").resolve()
    original_lstat = Path.lstat

    def device_lstat(path: Path) -> os.stat_result | SimpleNamespace:
        if path == output:
            return SimpleNamespace(st_mode=stat.S_IFBLK)
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", device_lstat)
    with pytest.raises(ImageBuildRefused) as caught:
        decompress_verified_image(
            manifest=manifest,
            source_archive=source,
            output_image=output,
        )
    assert caught.value.code is ImageBuildRefusalCode.OUTPUT_PATH_UNSAFE
    assert not output.exists()


def test_payload_is_bound_to_plan_and_runtime_enable_gate_is_not_present(
    tmp_path: Path,
) -> None:
    source, manifest, _ = _compressed_fixture(tmp_path)
    payload = tmp_path / "payload"
    shutil.copytree(PAYLOAD, payload)
    plan = _plan(tmp_path, source, manifest, payload)

    assert bind_payload(plan, payload) == plan.payload_files

    candidate = payload / "runtime" / "UNVERIFIED-RUNTIME-GATE.txt"
    candidate.write_text(candidate.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")
    with pytest.raises(ImageExecutionRefused) as caught:
        bind_payload(plan, payload)
    assert caught.value.code is ImageExecutionRefusalCode.PAYLOAD_BINDING_MISMATCH


def test_payload_binding_refuses_enable_gate_even_when_it_was_in_planned_payload(
    tmp_path: Path,
) -> None:
    source, manifest, _ = _compressed_fixture(tmp_path)
    payload = tmp_path / "payload"
    shutil.copytree(PAYLOAD, payload)
    forbidden = payload / "runtime" / "firstboot-runtime-v1.enabled"
    forbidden.write_text("EXACT_IMAGE_RUNTIME_VALIDATED=v1\n", encoding="ascii")
    plan = _plan(tmp_path, source, manifest, payload)

    with pytest.raises(ImageExecutionRefused) as caught:
        bind_payload(plan, payload)

    assert caught.value.code is ImageExecutionRefusalCode.PAYLOAD_BINDING_MISMATCH


def test_file_execute_refuses_unproven_dependencies_without_creating_output(
    tmp_path: Path,
) -> None:
    source, manifest, _ = _compressed_fixture(tmp_path)
    output = (tmp_path / "release.img").resolve()
    plan = author_image_build_plan(
        manifest=manifest,
        manifest_path=OFFICIAL_MANIFEST,
        source_archive=source,
        output_image=output,
        payload_root=PAYLOAD,
    )

    with pytest.raises(ImageExecutionRefused) as caught:
        execute_file_image(
            plan=plan,
            manifest=manifest,
            source_archive=source,
            output_image=output,
            payload_root=PAYLOAD,
        )

    assert caught.value.code in {
        ImageExecutionRefusalCode.DEPENDENCY_MISSING,
        ImageExecutionRefusalCode.INITRAMFS_CLOSURE_UNPROVEN,
    }
    assert "dependencies" in caught.value.details
    assert not output.exists()


def test_dependency_probe_admits_the_proven_native_file_tool_closure() -> None:
    report = probe_execution_dependencies(
        host_system="Linux",
        path_exists=lambda _path: True,
    )

    assert {item.name for item in report.observations} == {
        "xz",
        "mcopy",
        "mtype",
        "mdir",
        "debugfs",
        "zstd",
    }
    assert all(item.available for item in report.observations)
    assert report.initramfs_architecture_closure_proven
    assert report.execution_supported
    assert report.refusal_codes == ()


def test_windows_probe_records_exact_wsl_dependency_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: r"C:\Windows\System32\wsl.exe")

    report = probe_execution_dependencies(
        host_system="Windows",
        wsl_path_exists=lambda _wsl, path: path in {"/usr/bin/xz", "/usr/sbin/debugfs"},
    )

    by_name = {item.name: item for item in report.observations}
    assert by_name["xz"].available
    assert by_name["debugfs"].available
    assert not by_name["mcopy"].available
    assert not by_name["zstd"].available
    assert not report.execution_supported


def _fake_tool_paths(tmp_path: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    xz = shutil.which("xz")
    assert xz is not None
    result["xz"] = Path(xz)
    for name in ("mcopy", "mtype", "mdir", "debugfs", "zstd"):
        path = tmp_path / (f"{name}.exe" if os.name == "nt" else name)
        path.write_bytes(b"fake executable")
        if os.name != "nt":
            path.chmod(0o755)
        result[name] = path
    return result


def _authorization_file(
    tmp_path: Path,
    *,
    cid: str = EXACT_CARD_CID,
    size_bytes: int = EXACT_CARD_SIZE_BYTES,
    statement: str = AUTHORIZATION_STATEMENT,
) -> Path:
    path = tmp_path / "exact-card-authorization.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "cid": cid,
                "size_bytes": size_bytes,
                "statement": statement,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path


def test_exact_card_authorization_is_closed_and_bound_to_cid_size_and_statement(
    tmp_path: Path,
) -> None:
    authorized = load_exact_card_authorization(_authorization_file(tmp_path))
    assert authorized.cid == EXACT_CARD_CID
    assert authorized.size_bytes == EXACT_CARD_SIZE_BYTES

    for field in ("cid", "size", "statement"):
        path = _authorization_file(
            tmp_path,
            cid="00" if field == "cid" else EXACT_CARD_CID,
            size_bytes=1 if field == "size" else EXACT_CARD_SIZE_BYTES,
            statement="yes" if field == "statement" else AUTHORIZATION_STATEMENT,
        )
        with pytest.raises(ImageExecutionRefused) as caught:
            load_exact_card_authorization(path)
        assert caught.value.code is ImageExecutionRefusalCode.AUTHORIZATION_MISMATCH


def test_authorized_mode_requires_authorization_before_output_creation(tmp_path: Path) -> None:
    source, manifest, _ = _compressed_fixture(tmp_path)
    output = (tmp_path / "release.img").resolve()

    with pytest.raises(ImageExecutionRefused) as caught:
        execute_file_image(
            plan=_plan(tmp_path, source, manifest),
            manifest=manifest,
            source_archive=source,
            output_image=output,
            payload_root=PAYLOAD,
            authorized_exact_card_trial=True,
        )

    assert caught.value.code is ImageExecutionRefusalCode.AUTHORIZATION_REQUIRED
    assert not output.exists()


def test_disabled_mode_refuses_an_authorization_file_and_never_creates_output(
    tmp_path: Path,
) -> None:
    source, manifest, _ = _compressed_fixture(tmp_path)
    output = (tmp_path / "release.img").resolve()

    with pytest.raises(ImageExecutionRefused) as caught:
        execute_file_image(
            plan=_plan(tmp_path, source, manifest),
            manifest=manifest,
            source_archive=source,
            output_image=output,
            payload_root=PAYLOAD,
            authorization_file=_authorization_file(tmp_path),
        )

    assert caught.value.code is ImageExecutionRefusalCode.AUTHORIZATION_MISMATCH
    assert not output.exists()


@pytest.mark.parametrize("target_profile", [None, "pi-4-aarch64"])
def test_file_execute_refuses_missing_or_wrong_target_before_output_creation(
    tmp_path: Path, target_profile: str | None
) -> None:
    source, manifest, _raw = _compressed_fixture(tmp_path)
    output = (tmp_path / "release.img").resolve()

    with pytest.raises(ImageExecutionRefused) as caught:
        execute_file_image(
            plan=_plan(tmp_path, source, manifest),
            manifest=manifest,
            source_archive=source,
            output_image=output,
            payload_root=PAYLOAD,
            tool_paths=_fake_tool_paths(tmp_path),
            host_system="Linux",
            target_profile=target_profile,
        )

    assert caught.value.code is ImageExecutionRefusalCode.INITRAMFS_CLOSURE_UNPROVEN
    assert not output.exists()


def test_file_execute_refuses_closure_mutation_since_plan_before_output_creation(
    tmp_path: Path,
) -> None:
    source, manifest, _raw = _compressed_fixture(tmp_path)
    output = (tmp_path / "release.img").resolve()
    closure_path = tmp_path / "closure.json"
    closure_path.write_bytes(b'{"version":1}\n')
    plan = _plan(tmp_path, source, manifest, closure_manifest_path=closure_path)
    closure_path.write_bytes(b'{"version":2}\n')

    with pytest.raises(ImageExecutionRefused) as caught:
        execute_file_image(
            plan=plan,
            manifest=manifest,
            source_archive=source,
            output_image=output,
            payload_root=PAYLOAD,
            tool_paths=_fake_tool_paths(tmp_path),
            host_system="Linux",
            closure_manifest_path=closure_path,
            target_profile=PI_ZERO_2_W_ARMV7_PROFILE,
        )

    assert caught.value.code is ImageExecutionRefusalCode.INITRAMFS_CLOSURE_UNPROVEN
    assert not output.exists()


def test_file_execute_refuses_alternate_closure_path_even_with_identical_bytes(
    tmp_path: Path,
) -> None:
    source, manifest, _raw = _compressed_fixture(tmp_path)
    output = (tmp_path / "release.img").resolve()
    planned_closure = tmp_path / "planned-closure.json"
    alternate_closure = tmp_path / "alternate-closure.json"
    planned_closure.write_bytes(b'{"same":true}\n')
    alternate_closure.write_bytes(planned_closure.read_bytes())
    plan = _plan(tmp_path, source, manifest, closure_manifest_path=planned_closure)

    with pytest.raises(ImageExecutionRefused) as caught:
        execute_file_image(
            plan=plan,
            manifest=manifest,
            source_archive=source,
            output_image=output,
            payload_root=PAYLOAD,
            tool_paths=_fake_tool_paths(tmp_path),
            host_system="Linux",
            closure_manifest_path=alternate_closure,
            target_profile=PI_ZERO_2_W_ARMV7_PROFILE,
        )

    assert caught.value.code is ImageExecutionRefusalCode.INITRAMFS_CLOSURE_UNPROVEN
    assert not output.exists()


def test_file_execute_customizes_regular_image_with_injected_file_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, manifest, _raw = _compressed_fixture(tmp_path)
    output = (tmp_path / "release.img").resolve()
    tool_paths = _fake_tool_paths(tmp_path)
    closure_path = tmp_path / "closure.json"
    closure_path.write_bytes(b"{}")
    plan = _plan(tmp_path, source, manifest, closure_manifest_path=closure_path)
    artifacts = tuple(
        SimpleNamespace(key=name, source_path=f"/source/{name}")
        for name in ("resize2fs", "dumpe2fs", "sfdisk", "libfdisk")
    )
    monkeypatch.setattr(
        image_executor_module,
        "load_initramfs_closure_manifest",
        lambda _payload: _closure(artifacts),
    )
    monkeypatch.setattr(
        image_executor_module,
        "customize_initramfs",
        lambda *_args, **_kwargs: b"customized-initramfs",
    )
    files = {
        "::config.txt": VALID_BOOT_CONFIG,
        "::initramfs": b"generic-stock-initramfs",
        "::initramfs7": b"armv7-stock-initramfs",
        "::cmdline.txt": b"root=PARTUUID=12345678-02 resize quiet\n",
    }
    calls: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...], cwd: Path | None, _timeout: int) -> FileToolResult:
        calls.append(argv)
        name = Path(argv[0]).stem
        if name == "debugfs":
            assert cwd is not None
            destination = argv[2].split(" ")[-1]
            (cwd / destination).write_bytes(f"data-{destination}".encode())
        elif name == "mcopy":
            if "-o" in argv:
                files[argv[-1]] = Path(argv[-2]).read_bytes()
            else:
                Path(argv[-1]).write_bytes(files[argv[-2]])
        elif name == "mtype":
            return FileToolResult(0, files[argv[-1]], b"")
        return FileToolResult(0)

    result = execute_file_image(
        plan=plan,
        manifest=manifest,
        source_archive=source,
        output_image=output,
        payload_root=PAYLOAD,
        tool_paths=tool_paths,
        command_runner=runner,
        zstd_runner=lambda *_args, **_kwargs: 0,
        host_system="Linux",
        closure_manifest_path=closure_path,
        target_profile=PI_ZERO_2_W_ARMV7_PROFILE,
    )

    assert result.customized
    assert not result.runtime_gate_created
    assert output.exists()
    assert files["::initramfs7"] == b"customized-initramfs"
    assert files["::initramfs"] == b"generic-stock-initramfs"
    assert BOUNDED_PROVISION_TOKEN.encode() in files["::cmdline.txt"]
    names = [Path(argv[0]).stem for argv in calls]
    fat_initramfs_names = [
        argument
        for argv in calls
        if Path(argv[0]).stem in {"mcopy", "mdir"}
        for argument in argv
        if argument.startswith("::initramfs")
    ]
    assert fat_initramfs_names
    assert set(fat_initramfs_names) == {"::initramfs7"}
    assert names[:4] == ["debugfs"] * 4
    assert all("-w" not in argv for argv in calls)
    assert names.count("mcopy") == 5
    assert names[-3:] == ["mdir", "mtype", "mcopy"]


def test_authorized_file_execute_installs_and_reopens_exact_root_and_boot_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, manifest, _raw = _compressed_fixture(tmp_path)
    output = (tmp_path / "release.img").resolve()
    tool_paths = _fake_tool_paths(tmp_path)
    closure_path = tmp_path / "closure.json"
    closure_path.write_bytes(b"{}")
    artifacts = tuple(
        SimpleNamespace(key=name, source_path=f"/source/{name}")
        for name in ("resize2fs", "dumpe2fs", "sfdisk", "libfdisk")
    )
    monkeypatch.setattr(
        image_executor_module,
        "load_initramfs_closure_manifest",
        lambda _payload: _closure(artifacts),
    )
    gate_flags: list[bool] = []

    def customize(*_args: object, **kwargs: object) -> bytes:
        gate_flags.append(bool(kwargs["runtime_gate_enabled"]))
        return b"authorized-initramfs"

    monkeypatch.setattr(image_executor_module, "customize_initramfs", customize)
    fat_files = {
        "::config.txt": VALID_BOOT_CONFIG,
        "::initramfs": b"generic-stock-initramfs",
        "::initramfs7": b"armv7-stock-initramfs",
        "::cmdline.txt": b"root=PARTUUID=12345678-02 resize quiet\n",
    }
    root_files: dict[str, bytes] = {}
    root_modes: dict[str, int] = {}
    calls: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...], cwd: Path | None, _timeout: int) -> FileToolResult:
        calls.append(argv)
        name = Path(argv[0]).stem
        if name == "debugfs":
            command = argv[argv.index("-R") + 1]
            if command.startswith("dump -p /source/"):
                assert cwd is not None
                destination = command.split(" ")[-1]
                (cwd / destination).write_bytes(f"data-{destination}".encode())
            elif command.startswith("write "):
                assert cwd is not None
                _, source_name, destination = command.split(" ")
                root_files[destination] = (cwd / source_name).read_bytes()
            elif command.startswith("set_inode_field "):
                _, destination, _, mode = command.split(" ")
                root_modes[destination] = int(mode, 8) & 0o7777
            elif command.startswith("dump -p "):
                assert cwd is not None
                _, _, source_name, destination = command.split(" ")
                (cwd / destination).write_bytes(root_files[source_name])
            elif command.startswith("stat "):
                destination = command.removeprefix("stat ")
                if destination.endswith("dashcam-firstboot-storage.service") and (
                    "local-fs.target.wants" in destination
                ):
                    return FileToolResult(
                        0,
                        b'Mode:  0777\nFast link dest: "../dashcam-firstboot-storage.service"\n',
                    )
                return FileToolResult(
                    0,
                    f"Type: regular    Mode:  0{root_modes[destination]:o}\n".encode(),
                )
        elif name == "mcopy":
            if "-o" in argv:
                fat_files[argv[-1]] = Path(argv[-2]).read_bytes()
            else:
                Path(argv[-1]).write_bytes(fat_files[argv[-2]])
        elif name == "mtype":
            return FileToolResult(0, fat_files[argv[-1]])
        return FileToolResult(0)

    result = execute_file_image(
        plan=_plan(tmp_path, source, manifest, closure_manifest_path=closure_path),
        manifest=manifest,
        source_archive=source,
        output_image=output,
        payload_root=PAYLOAD,
        tool_paths=tool_paths,
        command_runner=runner,
        zstd_runner=lambda *_args, **_kwargs: 0,
        host_system="Linux",
        closure_manifest_path=closure_path,
        authorized_exact_card_trial=True,
        authorization_file=_authorization_file(tmp_path),
        target_profile=PI_ZERO_2_W_ARMV7_PROFILE,
    )

    assert result.runtime_gate_created
    assert gate_flags == [True]
    assert root_files["/etc/dashcam/firstboot-runtime-v1.enabled"] == (
        b"EXACT_IMAGE_RUNTIME_VALIDATED=v1\n"
    )
    assert root_files["/etc/dashcam/expendable-card-v1.authorized"] == (EXACT_CARD_MARKER_CONTENT)
    assert (
        root_files["/usr/lib/dashcam/dashcam-firstboot-storage"]
        == (PAYLOAD / "runtime" / "post-root" / "dashcam-firstboot-storage").read_bytes()
    )
    assert fat_files["::initramfs7"] == b"authorized-initramfs"
    assert fat_files["::initramfs"] == b"generic-stock-initramfs"
    first_root_write = next(
        index
        for index, argv in enumerate(calls)
        if Path(argv[0]).stem == "debugfs" and "-w" in argv
    )
    first_fat_write = next(
        index for index, argv in enumerate(calls) if Path(argv[0]).stem == "mcopy" and "-o" in argv
    )
    assert first_root_write < first_fat_write
    assert all("-w" not in argv for argv in calls[:4])
    mode_commands = [
        argv[argv.index("-R") + 1]
        for argv in calls
        if Path(argv[0]).stem == "debugfs"
        and "-R" in argv
        and argv[argv.index("-R") + 1].startswith("set_inode_field ")
    ]
    assert mode_commands
    assert all(" mode 0100" in command for command in mode_commands)
    mkdir_commands = [
        argv[argv.index("-R") + 1]
        for argv in calls
        if Path(argv[0]).stem == "debugfs"
        and "-R" in argv
        and argv[argv.index("-R") + 1].startswith("mkdir ")
    ]
    assert "mkdir /etc/systemd/system/local-fs.target.wants" in mkdir_commands


def test_authorized_root_write_failure_removes_only_the_created_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, manifest, _raw = _compressed_fixture(tmp_path)
    output = (tmp_path / "release.img").resolve()
    closure_path = tmp_path / "closure.json"
    closure_path.write_bytes(b"{}")
    monkeypatch.setattr(
        image_executor_module,
        "load_initramfs_closure_manifest",
        lambda _payload: _closure(
            (SimpleNamespace(key="resize2fs", source_path="/source/resize2fs"),)
        ),
    )
    monkeypatch.setattr(
        image_executor_module,
        "customize_initramfs",
        lambda *_args, **_kwargs: b"authorized-initramfs",
    )
    fat_files = {
        "::config.txt": VALID_BOOT_CONFIG,
        "::initramfs7": b"stock",
        "::cmdline.txt": b"root=PARTUUID=12345678-02 resize quiet\n",
    }

    def runner(argv: tuple[str, ...], cwd: Path | None, _timeout: int) -> FileToolResult:
        name = Path(argv[0]).stem
        if name == "debugfs":
            if "-w" in argv:
                return FileToolResult(9, b"", b"injected root write failure")
            assert cwd is not None
            destination = argv[2].split(" ")[-1]
            (cwd / destination).write_bytes(f"data-{destination}".encode())
        elif name == "mcopy":
            Path(argv[-1]).write_bytes(fat_files[argv[-2]])
        elif name == "mtype":
            return FileToolResult(0, fat_files[argv[-1]])
        return FileToolResult(0)

    with pytest.raises(ImageExecutionRefused) as caught:
        execute_file_image(
            plan=_plan(tmp_path, source, manifest, closure_manifest_path=closure_path),
            manifest=manifest,
            source_archive=source,
            output_image=output,
            payload_root=PAYLOAD,
            tool_paths=_fake_tool_paths(tmp_path),
            command_runner=runner,
            host_system="Linux",
            closure_manifest_path=closure_path,
            authorized_exact_card_trial=True,
            authorization_file=_authorization_file(tmp_path),
            target_profile=PI_ZERO_2_W_ARMV7_PROFILE,
        )

    assert caught.value.code is ImageExecutionRefusalCode.COMMAND_FAILED
    assert not output.exists()


@pytest.mark.parametrize(
    "destination",
    ["/etc/dashcam/../passwd", "../etc/passwd", r"/etc\dashcam\gate", "//etc/dashcam/gate"],
)
def test_authorized_root_destination_path_traversal_is_refused(destination: str) -> None:
    with pytest.raises(ImageExecutionRefused) as caught:
        image_executor_module._validate_ext4_destination(destination)
    assert caught.value.code is ImageExecutionRefusalCode.PAYLOAD_BINDING_MISMATCH


def test_authorized_root_verification_refuses_non_regular_file_type(tmp_path: Path) -> None:
    filesystem_uuid = "12345678-1234-5678-9abc-def012345678"
    root = bytearray(2048)
    root[1024 + 104 : 1024 + 120] = uuid.UUID(filesystem_uuid).bytes
    root_path = tmp_path / "root.ext4"
    root_path.write_bytes(root)
    expected = image_executor_module.RootInstallFile(
        None,
        "/etc/dashcam/gate",
        b"gate\n",
        0o400,
    )

    def runner(argv: tuple[str, ...], cwd: Path | None, _timeout: int) -> FileToolResult:
        command = argv[argv.index("-R") + 1]
        if command.startswith("dump -p "):
            assert cwd is not None
            (cwd / command.split(" ")[-1]).write_bytes(expected.content)
            return FileToolResult(0)
        return FileToolResult(0, b"Type: fifo    Mode:  0400\n")

    with pytest.raises(ImageExecutionRefused) as caught:
        image_executor_module._verify_authorized_root(
            runner,
            Path("debugfs"),
            root_path,
            (expected,),
            expected_uuid=filesystem_uuid,
            work=tmp_path,
            prefix="verify",
        )
    assert caught.value.code is ImageExecutionRefusalCode.OUTPUT_VERIFICATION_FAILED


def test_file_execute_removes_only_created_output_after_tool_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, manifest, _raw = _compressed_fixture(tmp_path)
    output = (tmp_path / "release.img").resolve()
    closure_path = tmp_path / "closure.json"
    closure_path.write_bytes(b"{}")
    monkeypatch.setattr(
        image_executor_module,
        "load_initramfs_closure_manifest",
        lambda _payload: _closure(
            (SimpleNamespace(key="resize2fs", source_path="/source/resize2fs"),)
        ),
    )

    with pytest.raises(ImageExecutionRefused) as caught:
        execute_file_image(
            plan=_plan(tmp_path, source, manifest, closure_manifest_path=closure_path),
            manifest=manifest,
            source_archive=source,
            output_image=output,
            payload_root=PAYLOAD,
            tool_paths=_fake_tool_paths(tmp_path),
            command_runner=lambda _argv, _cwd, _timeout: FileToolResult(9, b"", b"injected"),
            host_system="Linux",
            closure_manifest_path=closure_path,
            target_profile=PI_ZERO_2_W_ARMV7_PROFILE,
        )

    assert caught.value.code is ImageExecutionRefusalCode.COMMAND_FAILED
    assert not output.exists()


@pytest.mark.parametrize(
    "target",
    ["/dev/sda", r"\\.\PhysicalDrive2", "/dev/mmcblk0", "E:"],
)
def test_block_device_flashing_is_a_separate_unconditional_refusal(target: str) -> None:
    with pytest.raises(ImageExecutionRefused) as caught:
        refuse_block_device_execution(target)
    assert caught.value.code is ImageExecutionRefusalCode.BLOCK_DEVICE_EXECUTION_DISABLED


def test_cli_separates_file_execution_from_block_device_refusal(
    tmp_path: Path,
) -> None:
    output = (tmp_path / "release.img").resolve()

    result = subprocess.run(
        (
            sys.executable,
            str(ROOT / "scripts" / "build_release_image.py"),
            "--source",
            str(tmp_path / "not-read.img.xz"),
            "--output",
            str(output),
            "--flash-device",
            r"\\.\PhysicalDrive2",
        ),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert '"code": "block_device_execution_disabled"' in result.stderr
    assert not output.exists()


def test_cli_has_no_ambiguous_generic_execute_flag(tmp_path: Path) -> None:
    result = subprocess.run(
        (
            sys.executable,
            str(ROOT / "scripts" / "build_release_image.py"),
            "--source",
            str(tmp_path / "source.img.xz"),
            "--output",
            str((tmp_path / "release.img").resolve()),
            "--execute",
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "unrecognized arguments: --execute" in result.stderr


def test_cli_exact_card_mode_requires_a_separate_authorization_file(
    tmp_path: Path,
) -> None:
    result = subprocess.run(
        (
            sys.executable,
            str(ROOT / "scripts" / "build_release_image.py"),
            "--source",
            str(tmp_path / "source.img.xz"),
            "--output",
            str((tmp_path / "release.img").resolve()),
            "--execute-authorized-exact-card-image",
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "requires --authorization-file" in result.stderr
    assert not (tmp_path / "release.img").exists()


def test_cli_file_execution_requires_explicit_target_profile(tmp_path: Path) -> None:
    result = subprocess.run(
        (
            sys.executable,
            str(ROOT / "scripts" / "build_release_image.py"),
            "--source",
            str(tmp_path / "source.img.xz"),
            "--output",
            str((tmp_path / "release.img").resolve()),
            "--execute-file-image",
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "requires --target-profile" in result.stderr
    assert not (tmp_path / "release.img").exists()
